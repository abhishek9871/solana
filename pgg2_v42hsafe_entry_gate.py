"""V42H-SAFE Phase 3 — Strict runner survival filters.

Exposes evaluate_entry_gate(...) — a pure-arithmetic gate that emits a
fail-closed pass/blocker decision for a candidate that has already passed
its rule and the V42H-SAFE late-entry blocker. The gate enforces all of:

  - consecutive_virtual_wins >= 2
  - virtual_losses_last_3000ms == 0
  - last_virtual_loss_age_ms is None OR > 3000
  - time_since_last_virtual_bank_ms <= 350
  - time_since_first_virtual_bank_ms <= 3500
  - current_local_quote >= break_even_quote + 0.00020
  - latest_quote_gradient >= 0
  - latest_account_sub_delta >= 0
  - no_negative_curve_update_after_last_bank
  - rule_id is in v42hsafe_rules.json AND mode == "actual"
  - route == "pump_bc"
  - sim_needed == 0
  - pair_source in {"current_sig","cache","prewarmed","observed_raw_rpc",
                    "accountSubscribe","direct"}

Returns {"gate_pass": bool, "blocker": <reason>|None, "fields": {...}}.

PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced at module-load.
"""
from __future__ import annotations

import json
import os
import re as _re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


# Static-grep enforcement at module-load.
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
        sys.stderr.write(f"V42HSAFE-ENTRY-GATE-ABORT forbidden_call_pattern={_pat}\n")
        raise RuntimeError("forbidden_call_pattern_in_v42hsafe_entry_gate")


_RULES_CACHE: Dict[str, Any] = {}
_RULES_PATH_DEFAULT = "/root/piggy/data/v42hsafe_rules.json"
_ACCEPTED_PAIR_SOURCES = {
    "current_sig", "cache", "prewarmed", "observed_raw_rpc",
    # The V42H engine emits "accountSubscribe" by default — accepted because
    # accountSubscribe IS the direct on-chain feed (no broker proxy).
    "accountSubscribe", "direct",
}


def _load_rules(path: Optional[str] = None) -> Dict[str, Any]:
    global _RULES_CACHE
    p = path or _RULES_PATH_DEFAULT
    cached = _RULES_CACHE.get(p)
    if cached is not None:
        return cached
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    _RULES_CACHE[p] = data
    return data


def _is_actual_rule(rule_id: str, rules_path: Optional[str] = None) -> bool:
    try:
        rules = _load_rules(rules_path)
    except Exception:
        return False
    cfg = rules.get("rules", {}).get(rule_id)
    if not isinstance(cfg, dict):
        return False
    return str(cfg.get("mode", "")) == "actual"


def _consecutive_virtual_wins_ending_at(
    ticket_history: Iterable[Dict[str, Any]],
    cutoff_ts_ms: int,
) -> int:
    """Longest run of bank_win outcomes ending at the last ticket whose
    outcome_ts_ms <= cutoff_ts_ms. Causal: no future ticket counted."""
    closed = [
        t for t in ticket_history
        if t.get("outcome_ts_ms") is not None
        and int(t["outcome_ts_ms"]) <= int(cutoff_ts_ms)
        and t.get("outcome") in ("virtual_bank_win", "virtual_loss",
                                 "virtual_scratch", "expired")
    ]
    closed.sort(key=lambda t: int(t["outcome_ts_ms"]))
    streak = 0
    for t in reversed(closed):
        if t.get("outcome") == "virtual_bank_win":
            streak += 1
        else:
            break
    return streak


def _virtual_losses_in_window(
    ticket_history: Iterable[Dict[str, Any]],
    ts_now: int,
    window_ms: int,
) -> int:
    return sum(
        1 for t in ticket_history
        if t.get("outcome") == "virtual_loss"
        and t.get("outcome_ts_ms") is not None
        and (int(ts_now) - int(t["outcome_ts_ms"])) <= int(window_ms)
        and int(t["outcome_ts_ms"]) <= int(ts_now)
    )


def _last_virtual_loss_age_ms(
    ticket_history: Iterable[Dict[str, Any]],
    ts_now: int,
) -> Optional[int]:
    losses = [
        int(t["outcome_ts_ms"]) for t in ticket_history
        if t.get("outcome") == "virtual_loss"
        and t.get("outcome_ts_ms") is not None
        and int(t["outcome_ts_ms"]) <= int(ts_now)
    ]
    if not losses:
        return None
    return int(ts_now) - max(losses)


def _virtual_bank_times(
    ticket_history: Iterable[Dict[str, Any]],
    cutoff_ts_ms: int,
) -> List[int]:
    return sorted(
        int(t["outcome_ts_ms"]) for t in ticket_history
        if t.get("outcome") == "virtual_bank_win"
        and t.get("outcome_ts_ms") is not None
        and int(t["outcome_ts_ms"]) <= int(cutoff_ts_ms)
    )


def evaluate_entry_gate(
    mint: str,
    rule_id: str,
    ticket_history: Iterable[Dict[str, Any]],
    current_quote_sol: float,
    break_even_quote: float,
    latest_quote_gradient: float,
    latest_account_sub_delta: float,
    last_curve_update_kind: str,
    ts_ms_now: int,
    decision_ts_ms: int,
    route: str = "pump_bc",
    sim_needed: int = 0,
    pair_source: str = "accountSubscribe",
    rules_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return {gate_pass, blocker, fields}.

    Fail-closed: any False -> gate_pass=False, blocker=<first failing condition>.

    CAUSAL: ticket_history is filtered to outcome_ts_ms <= decision_ts_ms inside
    this function; callers must not pre-filter. ts_ms_now should equal decision_ts_ms
    for the live-equivalent check.
    """
    # Causal filter: only tickets resolved at-or-before decision_ts_ms are visible.
    th: List[Dict[str, Any]] = [
        t for t in ticket_history
        if t.get("outcome_ts_ms") is None
        or int(t.get("outcome_ts_ms") or 0) <= int(decision_ts_ms)
    ]
    cvw = _consecutive_virtual_wins_ending_at(th, decision_ts_ms)
    vll3000 = _virtual_losses_in_window(th, decision_ts_ms, 3000)
    last_loss_age = _last_virtual_loss_age_ms(th, decision_ts_ms)
    bank_ts = _virtual_bank_times(th, decision_ts_ms)
    tslb = (int(decision_ts_ms) - bank_ts[-1]) if bank_ts else None
    tsfb = (int(decision_ts_ms) - bank_ts[0]) if bank_ts else None

    fields: Dict[str, Any] = {
        "mint": mint,
        "rule_id": rule_id,
        "ts_ms_now": int(ts_ms_now),
        "decision_ts_ms": int(decision_ts_ms),
        "consecutive_virtual_wins": int(cvw),
        "virtual_losses_last_3000ms": int(vll3000),
        "last_virtual_loss_age_ms": (None if last_loss_age is None else int(last_loss_age)),
        "time_since_last_virtual_bank_ms": (None if tslb is None else int(tslb)),
        "time_since_first_virtual_bank_ms": (None if tsfb is None else int(tsfb)),
        "current_local_quote": float(current_quote_sol),
        "break_even_quote": float(break_even_quote),
        "latest_quote_gradient": float(latest_quote_gradient),
        "latest_account_sub_delta": float(latest_account_sub_delta),
        "last_curve_update_kind": str(last_curve_update_kind),
        "rule_mode": "shadow",
        "route": str(route),
        "sim_needed": int(sim_needed),
        "pair_source": str(pair_source),
    }

    is_actual = _is_actual_rule(rule_id, rules_path)
    fields["rule_mode"] = "actual" if is_actual else "shadow"

    # Ordered checks. First failure wins.
    if cvw < 2:
        return {"gate_pass": False, "blocker": "consecutive_virtual_wins_lt_2", "fields": fields}
    if vll3000 > 0:
        return {"gate_pass": False, "blocker": "virtual_losses_in_last_3000ms", "fields": fields}
    if last_loss_age is not None and last_loss_age <= 3000:
        return {"gate_pass": False, "blocker": "last_virtual_loss_within_3000ms", "fields": fields}
    if tslb is None:
        return {"gate_pass": False, "blocker": "no_virtual_bank_yet", "fields": fields}
    if tslb > 350:
        return {"gate_pass": False, "blocker": "time_since_last_virtual_bank_gt_350ms", "fields": fields}
    if tsfb is not None and tsfb > 3500:
        return {"gate_pass": False, "blocker": "time_since_first_virtual_bank_gt_3500ms", "fields": fields}
    if float(current_quote_sol) < float(break_even_quote) + 0.00020:
        return {"gate_pass": False, "blocker": "current_quote_below_break_even_plus_safety", "fields": fields}
    if float(latest_quote_gradient) < 0.0:
        return {"gate_pass": False, "blocker": "latest_quote_gradient_negative", "fields": fields}
    if float(latest_account_sub_delta) < 0.0:
        return {"gate_pass": False, "blocker": "latest_account_sub_delta_negative", "fields": fields}
    if str(last_curve_update_kind) == "negative":
        return {"gate_pass": False, "blocker": "negative_curve_update_after_last_bank", "fields": fields}
    if not is_actual:
        return {"gate_pass": False, "blocker": "rule_mode_not_actual", "fields": fields}
    if str(route) != "pump_bc":
        return {"gate_pass": False, "blocker": "route_not_pump_bc", "fields": fields}
    if int(sim_needed) != 0:
        return {"gate_pass": False, "blocker": "sim_needed_nonzero", "fields": fields}
    if str(pair_source) not in _ACCEPTED_PAIR_SOURCES:
        return {"gate_pass": False, "blocker": "pair_source_unacceptable", "fields": fields}

    return {"gate_pass": True, "blocker": None, "fields": fields}


def format_log_line(decision: Dict[str, Any]) -> str:
    """Return a single-line log emit for caller. Format spec:
        PGG2-V42HSAFE-ENTRY-GATE mint=... rule=... cvw=... vll3000=...
            tslb=... cq=... be=... grad=... cdelta=... pass=... blocker=...
    """
    f = decision.get("fields", {}) or {}
    mint = str(f.get("mint", ""))
    mshort = (mint[:4] + ".." + mint[-4:]) if len(mint) > 10 else mint
    return (
        f"PGG2-V42HSAFE-ENTRY-GATE mint={mshort} rule={f.get('rule_id','?')} "
        f"cvw={f.get('consecutive_virtual_wins','?')} "
        f"vll3000={f.get('virtual_losses_last_3000ms','?')} "
        f"tslb={f.get('time_since_last_virtual_bank_ms','?')} "
        f"cq={f.get('current_local_quote',0.0):.9f} "
        f"be={f.get('break_even_quote',0.0):.9f} "
        f"grad={f.get('latest_quote_gradient',0.0):+.9f} "
        f"cdelta={f.get('latest_account_sub_delta',0.0):+.9f} "
        f"pass={bool(decision.get('gate_pass'))} "
        f"blocker={decision.get('blocker') or 'none'}"
    )


__all__ = ["evaluate_entry_gate", "format_log_line"]
