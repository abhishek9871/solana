"""V42J Phase 4 - Rule evaluator for bank-interrupt entry rules.

Loads /root/piggy/data/v42j_bank_interrupt_rules.json and exposes:
    evaluate_rules(event, reprice, latest_curve_state, mint_history, ts_ms_now)
        -> list[(rule_id, ok, reason)]

Rules operate on:
    event:                V42JBankEvent (from pgg2_v42j_bank_event)
    reprice:              dict returned by pgg2_v42j_reprice.reprice_at_bank_event
    latest_curve_state:   {"latest_curve_delta_nonneg": bool,
                           "last_negative_curve_update_ts_ms": int,
                           ... optional ...}
    mint_history:         {"virtual_losses_last_2000ms": int,
                           "no_virtual_loss_after_prior_bank": bool,
                           "bank_event_count_last_3000ms": int,
                           "newest_bank_event_age_ms": int}
    ts_ms_now:            decision evaluation timestamp

PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced.
"""
from __future__ import annotations

import json
import os
import re as _re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ----- static-grep self-check ----------------------------------------
_FORBIDDEN_CALL_PATTERNS = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN_CALL_PATTERNS:
    if _re.search(_pat, _src):
        sys.stderr.write(
            f"V42J-RULE-EVAL-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v42j_rule_evaluator")


from pgg2_v42h_local_curve_quote import DEFAULT_TX_FEE_SOL


_RULES_CACHE: Dict[str, Dict[str, Any]] = {}


def load_rules(path: str) -> Dict[str, Any]:
    if path in _RULES_CACHE:
        return _RULES_CACHE[path]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"v42j_rules_not_found:{path}")
    raw = Path(path).read_text(encoding="utf-8")
    cfg = json.loads(raw)
    _RULES_CACHE[path] = cfg
    return cfg


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _check_one(
    rule_id: str,
    pre: Dict[str, Any],
    event: Any,
    reprice: Dict[str, Any],
    latest_curve_state: Dict[str, Any],
    mint_history: Dict[str, Any],
    ts_ms_now: int,
    amount_sol: float,
    tx_fee_sol: float,
) -> Tuple[bool, str]:
    # --- bank_event_present ---
    if pre.get("bank_event_present", False):
        if event is None:
            return False, "no_bank_event"

    # --- bank_event_age_ms_max ---
    if "bank_event_age_ms_max" in pre:
        max_age = int(pre["bank_event_age_ms_max"])
        age = int(ts_ms_now) - int(getattr(event, "event_ts_ms", 0))
        if age > max_age:
            return False, f"bank_event_age_too_high:{age}>{max_age}"

    # --- bank_pnl_gte_sol ---
    if "bank_pnl_gte_sol" in pre:
        thr = float(pre["bank_pnl_gte_sol"])
        if float(getattr(event, "bank_pnl", 0.0)) < thr:
            return False, f"bank_pnl_below:{event.bank_pnl}<{thr}"

    # --- no_virtual_loss_on_mint_last_2000ms ---
    if pre.get("no_virtual_loss_on_mint_last_2000ms", False):
        n_loss = int(mint_history.get("virtual_losses_last_2000ms", 0) or 0)
        if n_loss > 0:
            return False, f"virtual_loss_in_last_2000ms:{n_loss}"

    # --- current_local_sell_quote_above_break_even_plus_sol ---
    # PHASE-2-CONSISTENT INTERPRETATION:
    # "current local sell quote" in the rules refers to the PROJECTED
    # one-tick continuation sell-out (Phase 2 explicitly states same-state
    # roundtrip is structurally negative on pump_bc and must NOT be the
    # entry criterion). We use reprice["proj_sell_sol"] which is the sell
    # of our just-bought tokens after one +PROJ_TICK_FRAC continuation.
    if "current_local_sell_quote_above_break_even_plus_sol" in pre:
        buf = float(pre["current_local_sell_quote_above_break_even_plus_sol"])
        be = float(amount_sol) + 2.0 * float(tx_fee_sol)
        cur = float(reprice.get("proj_sell_sol", 0.0) or 0.0)
        if cur < (be + buf):
            return False, f"proj_sell_below_be_plus_buf:{cur:.9f}<{be + buf:.9f}"

    # --- current_local_sell_quote_above_break_even ---
    if pre.get("current_local_sell_quote_above_break_even", False):
        be = float(amount_sol) + 2.0 * float(tx_fee_sol)
        cur = float(reprice.get("proj_sell_sol", 0.0) or 0.0)
        if cur < be:
            return False, f"proj_sell_below_be:{cur:.9f}<{be:.9f}"

    # --- latest_curve_delta_nonnegative ---
    if pre.get("latest_curve_delta_nonnegative", False):
        flag = latest_curve_state.get("latest_curve_delta_nonneg", None)
        if flag is False:
            return False, "latest_curve_delta_negative"
        # If flag is None we don't have data - require last_neg_ts <= event_ts
        last_neg = int(latest_curve_state.get("last_negative_curve_update_ts_ms", 0) or 0)
        if last_neg and last_neg > int(getattr(event, "event_ts_ms", 0)):
            return False, "negative_curve_after_event"

    # --- no_negative_curve_update_after_bank_event ---
    if pre.get("no_negative_curve_update_after_bank_event", False):
        last_neg = int(latest_curve_state.get("last_negative_curve_update_ts_ms", 0) or 0)
        if last_neg and last_neg > int(getattr(event, "event_ts_ms", 0)):
            return False, "negative_curve_after_event"

    # --- bank_event_stress_pnl_gte_sol ---
    if "bank_event_stress_pnl_gte_sol" in pre:
        thr = float(pre["bank_event_stress_pnl_gte_sol"])
        if float(reprice.get("bank_event_stress_pnl", -9.9)) < thr:
            return False, (
                f"stress_pnl_below:{reprice.get('bank_event_stress_pnl')}"
                f"<{thr}"
            )

    # --- bank_event_count_last_3000ms_gte ---
    if "bank_event_count_last_3000ms_gte" in pre:
        thr = int(pre["bank_event_count_last_3000ms_gte"])
        n = int(mint_history.get("bank_event_count_last_3000ms", 0) or 0)
        if n < thr:
            return False, f"bank_event_count_low:{n}<{thr}"

    # --- newest_bank_event_age_ms_max ---
    if "newest_bank_event_age_ms_max" in pre:
        max_age = int(pre["newest_bank_event_age_ms_max"])
        age = int(mint_history.get("newest_bank_event_age_ms", 1 << 30))
        if age > max_age:
            return False, f"newest_event_age_too_high:{age}>{max_age}"

    # --- no_virtual_loss_after_prior_bank ---
    if pre.get("no_virtual_loss_after_prior_bank", False):
        if not bool(mint_history.get("no_virtual_loss_after_prior_bank", True)):
            return False, "virtual_loss_after_prior_bank"

    # --- route_eq ---
    if "route_eq" in pre:
        if str(reprice.get("route", "")) != str(pre["route_eq"]):
            return False, f"route_ne:{reprice.get('route')}!={pre['route_eq']}"

    # --- sim_needed_eq ---
    if "sim_needed_eq" in pre:
        if int(reprice.get("sim_needed", 1) or 0) != int(pre["sim_needed_eq"]):
            return False, f"sim_needed_ne:{reprice.get('sim_needed')}!={pre['sim_needed_eq']}"

    return True, "pass"


def evaluate_rules(
    event: Any,
    reprice: Dict[str, Any],
    latest_curve_state: Dict[str, Any],
    mint_history: Dict[str, Any],
    ts_ms_now: int,
    rules_path: str = "/root/piggy/data/v42j_bank_interrupt_rules.json",
    amount_sol: float = 0.015,
    tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
    mode_filter: str = "actual",
) -> List[Tuple[str, bool, str]]:
    cfg = load_rules(rules_path)
    out: List[Tuple[str, bool, str]] = []
    for r in cfg.get("rules", []):
        if str(r.get("mode", "")) != str(mode_filter):
            continue
        rid = str(r.get("rule_id", ""))
        pre = dict(r.get("preconditions", {}))
        try:
            ok, reason = _check_one(
                rid, pre, event, reprice, latest_curve_state,
                mint_history, ts_ms_now, amount_sol, tx_fee_sol,
            )
        except Exception as exc:
            ok, reason = False, f"err:{type(exc).__name__}:{exc}"
        out.append((rid, ok, reason))
    return out


def exit_policy(rules_path: str = "/root/piggy/data/v42j_bank_interrupt_rules.json") -> Dict[str, Any]:
    cfg = load_rules(rules_path)
    return dict(cfg.get("exit_policy", {}))


__all__ = ["load_rules", "evaluate_rules", "exit_policy"]
