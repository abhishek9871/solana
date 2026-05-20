"""V42I Phase 2 — Rule evaluator for active-ticket entry rules.

Loads `/root/piggy/data/v42i_active_ticket_rules.json` and exposes:
    evaluate_rules(state_dict, route='pump_bc', sim_needed=0) -> list[(rule_id, ok, reason)]

V42I rules operate on the ActiveTicketState dict produced by
`pgg2_v42i_active_ticket_state.ActiveTicketStateTracker.get_state(...)`.

The four rules:
    v42i_one_bank_active_positive  — one bank done, second ticket open,
                                       positive + improving, controlled
                                       drawdown.
    v42i_strong_bank_active_scratch — strong bank done, active ticket at
                                       least scratch-positive and not
                                       deteriorating.
    v42i_fast_second_wave          — first bank completed fast; the next
                                       ticket opened quickly after that
                                       bank and is currently improving.
    v42i_high_edge_active          — large bank done; active ticket
                                       above a high pnl bar; gradient >= 0.

PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced at module load.
"""
from __future__ import annotations

import json
import os
import re as _re
import sys
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
        sys.stderr.write(f"V42I-RULE-EVAL-ABORT forbidden_call_pattern={_pat}\n")
        raise RuntimeError("forbidden_call_pattern_in_v42i_rule_evaluator")


DEFAULT_RULES_PATH = "/root/piggy/data/v42i_active_ticket_rules.json"
_RULES_CACHE: Dict[str, Any] = {}


def load_rules(path: Optional[str] = None) -> Dict[str, Any]:
    p = path or DEFAULT_RULES_PATH
    if p in _RULES_CACHE:
        return _RULES_CACHE[p]
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    _RULES_CACHE[p] = data
    return data


# ----- single-rule evaluators ----------------------------------------

def _eval_one_bank_active_positive(
    pre: Dict[str, Any], st: Dict[str, Any], route: str, sim_needed: int
) -> Tuple[bool, str]:
    if int(st.get("completed_virtual_banks_last_3000ms") or 0) < int(
        pre.get("completed_virtual_banks_last_3000ms_gte", 1)
    ):
        return False, "completed_virtual_banks_last_3000ms_lt"
    if int(st.get("completed_virtual_losses_after_latest_bank") or 0) != int(
        pre.get("completed_virtual_losses_after_latest_bank_eq", 0)
    ):
        return False, "completed_virtual_losses_after_latest_bank_ne"
    if pre.get("active_ticket_id_present", True):
        if not st.get("active_ticket_id"):
            return False, "no_active_ticket"
    age = st.get("active_ticket_age_ms")
    if age is None:
        return False, "no_active_ticket"
    if int(age) < int(pre.get("active_ticket_age_ms_min", 0)):
        return False, "active_ticket_age_lt_min"
    if int(age) > int(pre.get("active_ticket_age_ms_max", 10**9)):
        return False, "active_ticket_age_gt_max"
    cur_pnl = st.get("active_ticket_current_pnl")
    if cur_pnl is None or float(cur_pnl) < float(
        pre.get("active_ticket_current_pnl_gte_sol", 0.0)
    ):
        return False, "active_ticket_pnl_below_min"
    grad = st.get("active_ticket_pnl_gradient")
    if grad is None or float(grad) <= float(
        pre.get("active_ticket_pnl_gradient_gt", 0.0)
    ):
        return False, "active_ticket_gradient_not_positive"
    madv = st.get("active_ticket_max_adverse")
    if madv is None or float(madv) <= float(
        pre.get("active_ticket_max_adverse_gt_sol", -0.00020)
    ):
        return False, "active_ticket_max_adverse_too_deep"
    lqg = st.get("latest_local_quote_gradient")
    if lqg is None or float(lqg) < float(
        pre.get("latest_local_quote_gradient_gte", 0.0)
    ):
        return False, "latest_local_quote_gradient_negative"
    if pre.get("latest_curve_delta_or_no_negative_after_bank", True):
        if bool(st.get("negative_curve_after_latest_bank")):
            cd = st.get("latest_curve_delta")
            if cd is None or float(cd) < 0.0:
                return False, "negative_curve_after_bank_and_delta_neg"
    if str(route) != str(pre.get("route_eq", "pump_bc")):
        return False, "route_not_match"
    if int(sim_needed) != int(pre.get("sim_needed_eq", 0)):
        return False, "sim_needed_nonzero"
    return True, "ok"


def _eval_strong_bank_active_scratch(
    pre: Dict[str, Any], st: Dict[str, Any], route: str, sim_needed: int
) -> Tuple[bool, str]:
    lcbpnl = st.get("latest_completed_bank_pnl")
    if lcbpnl is None or float(lcbpnl) < float(
        pre.get("latest_completed_bank_pnl_gte_sol", 0.0)
    ):
        return False, "latest_bank_pnl_below_min"
    cur_pnl = st.get("active_ticket_current_pnl")
    if cur_pnl is None or float(cur_pnl) < float(
        pre.get("active_ticket_current_pnl_gte_sol", 0.0)
    ):
        return False, "active_ticket_pnl_below_min"
    if pre.get("active_ticket_not_deteriorating", True):
        grad = st.get("active_ticket_pnl_gradient")
        if grad is None or float(grad) < 0.0:
            return False, "active_ticket_deteriorating"
    if int(st.get("completed_virtual_losses_after_latest_bank") or 0) != int(
        pre.get("completed_virtual_losses_after_latest_bank_eq", 0)
    ):
        return False, "completed_virtual_losses_after_latest_bank_ne"
    lqg = st.get("latest_local_quote_gradient")
    if lqg is None or float(lqg) < float(
        pre.get("latest_local_quote_gradient_gte", 0.0)
    ):
        return False, "latest_local_quote_gradient_negative"
    if str(route) != str(pre.get("route_eq", "pump_bc")):
        return False, "route_not_match"
    if int(sim_needed) != int(pre.get("sim_needed_eq", 0)):
        return False, "sim_needed_nonzero"
    return True, "ok"


def _eval_fast_second_wave(
    pre: Dict[str, Any], st: Dict[str, Any], route: str, sim_needed: int
) -> Tuple[bool, str]:
    ttc = st.get("first_bank_time_to_completion_ms")
    if ttc is None or int(ttc) > int(
        pre.get("first_completed_virtual_bank_time_to_completion_ms_max", 1500)
    ):
        return False, "first_bank_too_slow"
    open_after = st.get("active_ticket_open_after_first_bank_ms")
    if open_after is None or int(open_after) > int(
        pre.get("active_ticket_open_within_after_first_bank_ms_max", 1500)
    ):
        return False, "active_ticket_not_opened_quickly_after_first_bank"
    # active_ticket must have opened AFTER (or simultaneously with) the bank
    if int(open_after) < 0:
        return False, "active_ticket_opened_before_first_bank"
    cur_pnl = st.get("active_ticket_current_pnl")
    if cur_pnl is None or float(cur_pnl) < float(
        pre.get("active_ticket_current_pnl_gte_sol", 0.0)
    ):
        return False, "active_ticket_pnl_below_min"
    if pre.get("active_ticket_is_improving", True):
        if not bool(st.get("active_ticket_is_improving")):
            return False, "active_ticket_not_improving"
    if pre.get("no_negative_curve_update_after_first_bank", True):
        if bool(st.get("negative_curve_after_latest_bank")):
            return False, "negative_curve_update_after_first_bank"
    if str(route) != str(pre.get("route_eq", "pump_bc")):
        return False, "route_not_match"
    if int(sim_needed) != int(pre.get("sim_needed_eq", 0)):
        return False, "sim_needed_nonzero"
    return True, "ok"


def _eval_high_edge_active(
    pre: Dict[str, Any], st: Dict[str, Any], route: str, sim_needed: int
) -> Tuple[bool, str]:
    lcbpnl = st.get("latest_completed_bank_pnl")
    if lcbpnl is None or float(lcbpnl) < float(
        pre.get("latest_completed_bank_pnl_gte_sol", 0.0)
    ):
        return False, "latest_bank_pnl_below_min"
    cur_pnl = st.get("active_ticket_current_pnl")
    if cur_pnl is None or float(cur_pnl) < float(
        pre.get("active_ticket_current_pnl_gte_sol", 0.0)
    ):
        return False, "active_ticket_pnl_below_min"
    grad = st.get("active_ticket_pnl_gradient")
    if grad is None or float(grad) < float(
        pre.get("active_ticket_pnl_gradient_gte", 0.0)
    ):
        return False, "active_ticket_gradient_negative"
    if int(st.get("completed_virtual_losses_after_latest_bank") or 0) != int(
        pre.get("completed_virtual_losses_after_latest_bank_eq", 0)
    ):
        return False, "completed_virtual_losses_after_latest_bank_ne"
    if str(route) != str(pre.get("route_eq", "pump_bc")):
        return False, "route_not_match"
    if int(sim_needed) != int(pre.get("sim_needed_eq", 0)):
        return False, "sim_needed_nonzero"
    return True, "ok"


_RULE_DISPATCH = {
    "v42i_one_bank_active_positive": _eval_one_bank_active_positive,
    "v42i_strong_bank_active_scratch": _eval_strong_bank_active_scratch,
    "v42i_fast_second_wave": _eval_fast_second_wave,
    "v42i_high_edge_active": _eval_high_edge_active,
}


def evaluate_rules(
    state_dict: Dict[str, Any],
    rules_path: Optional[str] = None,
    route: str = "pump_bc",
    sim_needed: int = 0,
) -> List[Tuple[str, bool, str]]:
    """Returns list of (rule_id, passed, reason). Order = rules file order."""
    cfg = load_rules(rules_path)
    out: List[Tuple[str, bool, str]] = []
    for r in cfg.get("rules", []):
        rid = str(r.get("rule_id", ""))
        if not rid:
            continue
        if str(r.get("mode", "actual")) != "actual":
            # Shadow rules are skipped for actual decisions but reported as
            # passed=False with reason=shadow_mode so callers can still see
            # them.
            out.append((rid, False, "shadow_mode_skipped"))
            continue
        fn = _RULE_DISPATCH.get(rid)
        if fn is None:
            out.append((rid, False, "no_evaluator"))
            continue
        pre = dict(r.get("preconditions", {}) or {})
        try:
            ok, reason = fn(pre, state_dict, route, sim_needed)
        except Exception as exc:
            out.append((rid, False, f"eval_error:{type(exc).__name__}"))
            continue
        out.append((rid, bool(ok), str(reason)))
    return out


def passed_rules(
    state_dict: Dict[str, Any],
    rules_path: Optional[str] = None,
    route: str = "pump_bc",
    sim_needed: int = 0,
) -> List[str]:
    return [
        rid for (rid, ok, _r) in evaluate_rules(
            state_dict, rules_path, route, sim_needed
        ) if ok
    ]


__all__ = [
    "load_rules",
    "evaluate_rules",
    "passed_rules",
    "DEFAULT_RULES_PATH",
]
