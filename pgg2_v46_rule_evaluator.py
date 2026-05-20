"""V46 Phase 5 - Rule evaluator for shred-led pending-flow entries.

Loads data/v46_shred_pending_flow_rules.json and evaluates per-mint state.
Returns the list of rule_ids that pass.

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import json
import re as _re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


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
            f"V46-RULE-EVAL-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v46_rule_evaluator")


def load_rules(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"v46 rules file not found: {path}")
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or "rules" not in cfg:
        raise ValueError(f"v46 rules file malformed: {path}")
    return cfg


def exit_policy(path: str) -> Dict[str, Any]:
    cfg = load_rules(path)
    return dict(cfg.get("exit_policy") or {})


def _coerce_int_or_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _eval_precondition(
    key: str, expected: Any, state: Dict[str, Any]
) -> Tuple[bool, str]:
    """Return (pass, reason_if_block). reason is empty if pass."""
    if key == "raw_buy_visible_before_curve_update":
        v = bool(state.get("raw_buy_visible_before_curve_update", False))
        if bool(expected) == v:
            return True, ""
        return False, f"raw_buy_visible_before_curve_update={v}!={expected}"

    if key == "reflected_in_curve_eq":
        v = bool(state.get("reflected_in_curve", False))
        if bool(expected) == v:
            return True, ""
        return False, f"reflected_in_curve={v}!={expected}"

    if key == "pending_buy_sol_250ms_gte":
        v = _coerce_int_or_float(state.get("pending_buy_sol_250ms", 0.0))
        if v >= _coerce_int_or_float(expected):
            return True, ""
        return False, f"pending_buy_sol_250ms={v:.4f}<{expected}"

    if key == "pending_sell_sol_250ms_max":
        v = _coerce_int_or_float(state.get("pending_sell_sol_250ms", 0.0))
        if v <= _coerce_int_or_float(expected):
            return True, ""
        return False, f"pending_sell_sol_250ms={v:.4f}>{expected}"

    if key == "largest_pending_buy_sol_250ms_gte":
        v = _coerce_int_or_float(state.get("largest_pending_buy_sol_250ms", 0.0))
        if v >= _coerce_int_or_float(expected):
            return True, ""
        return False, f"largest_pending_buy_sol_250ms={v:.4f}<{expected}"

    if key == "pending_buy_count_250ms_gte":
        v = int(state.get("pending_buy_count_250ms", 0) or 0)
        if v >= int(expected):
            return True, ""
        return False, f"pending_buy_count_250ms={v}<{expected}"

    if key == "pending_buy_count_250ms_or_large_buy":
        try:
            count_gte = int(expected.get("count_gte", 0))
            or_largest_sol_gte = _coerce_int_or_float(
                expected.get("or_largest_sol_gte", 0.0)
            )
        except Exception:
            return False, "pending_buy_count_250ms_or_large_buy: malformed expected"
        cnt = int(state.get("pending_buy_count_250ms", 0) or 0)
        lpb = _coerce_int_or_float(
            state.get("largest_pending_buy_sol_250ms", 0.0)
        )
        ok = cnt >= count_gte or lpb >= or_largest_sol_gte
        if ok:
            return True, ""
        return False, (
            f"pending_buy_count_250ms_or_large_buy: cnt={cnt}<{count_gte} "
            f"AND lpb={lpb:.4f}<{or_largest_sol_gte}"
        )

    if key == "projected_pnl_gte_sol":
        v = _coerce_int_or_float(state.get("projected_pnl", 0.0))
        if v >= _coerce_int_or_float(expected):
            return True, ""
        return False, f"projected_pnl={v:+.6f}<{expected}"

    if key == "stress_pnl_gte_sol":
        v = _coerce_int_or_float(state.get("stress_pnl", 0.0))
        if v >= _coerce_int_or_float(expected):
            return True, ""
        return False, f"stress_pnl={v:+.6f}<{expected}"

    if key == "source_lead_ms_gte":
        v = _coerce_int_or_float(state.get("source_lead_ms", 0.0))
        if v >= _coerce_int_or_float(expected):
            return True, ""
        return False, f"source_lead_ms={v:+.0f}<{expected}"

    if key == "route_eq":
        v = str(state.get("route", "") or "")
        if v == str(expected):
            return True, ""
        return False, f"route={v}!={expected}"

    if key == "sim_needed_eq":
        v = int(state.get("sim_needed", 0) or 0)
        if v == int(expected):
            return True, ""
        return False, f"sim_needed={v}!={expected}"

    if key == "latest_curve_delta_positive_but_below_bank":
        v = bool(state.get("latest_curve_delta_positive_but_below_bank", False))
        if bool(expected) == v:
            return True, ""
        return False, f"latest_curve_delta_positive_but_below_bank={v}!={expected}"

    if key == "quote_available_after_missing":
        v = bool(state.get("quote_available_after_missing", False))
        if bool(expected) == v:
            return True, ""
        return False, f"quote_available_after_missing={v}!={expected}"

    if key == "raw_pending_buy_flow_continues":
        v = bool(state.get("raw_pending_buy_flow_continues", False))
        if bool(expected) == v:
            return True, ""
        return False, f"raw_pending_buy_flow_continues={v}!={expected}"

    # Unknown precondition - fail-safe block.
    return False, f"unknown_precondition_key={key}"


def evaluate_rules(
    rules_cfg: Dict[str, Any],
    state: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    """Return (passing_rule_ids, all_block_reasons).

    state must contain whatever the precondition keys reference. Missing
    keys default to zero/false (which generally blocks the rule).
    """
    rules = rules_cfg.get("rules") or []
    passing: List[str] = []
    block_reasons: List[str] = []
    for rule in rules:
        rid = str(rule.get("rule_id") or "")
        mode = str(rule.get("mode") or "")
        if mode != "actual":
            continue
        preconds = rule.get("preconditions") or {}
        rule_pass = True
        rule_block: List[str] = []
        for k, exp in preconds.items():
            ok, reason = _eval_precondition(k, exp, state)
            if not ok:
                rule_pass = False
                rule_block.append(f"{rid}:{reason}")
        if rule_pass:
            passing.append(rid)
        else:
            block_reasons.extend(rule_block)
    return passing, block_reasons


__all__ = ["load_rules", "exit_policy", "evaluate_rules"]
