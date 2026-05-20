"""V47 Phase 2 - Adaptive size selector.

Sweep candidate sizes through Phase-1 evaluate_size, apply ordered
rejection rules, then pick the smallest viable size with tiebreaks.

Selection rules (ordered):
  1. Reject stress_all_in_pnl < 0           -> "stress_negative"
  2. Reject self_impact_bps > 250 (2.5%)    -> "self_impact_too_high"
  3. Reject NOT guards_encodable             -> "guards_not_encodable"
  4. Reject NOT meets_required_profit        -> "below_required_profit"
  5. Among accepted sizes, tiebreak in this order:
     a. smallest size with stress_all_in_pnl >= required_profit_sol
        (i.e., even the worst-case stress still nets the required edge)
     b. ties -> highest (all_in_pnl / max(stress_all_in_pnl, eps)) ratio
        (best expected/stress edge)
     c. ties -> lowest self_impact_bps

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import os
import re as _re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple


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
            f"V47-SELECT-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47_size_selector")


def _import_evaluator():
    sys.path.insert(0, "/root/piggy")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pgg2_v47_size_normalized_edge import (  # type: ignore
        evaluate_size, SELF_IMPACT_CAP_BPS,
    )
    return evaluate_size, SELF_IMPACT_CAP_BPS


DEFAULT_SIZES_SOL = [0.001, 0.002, 0.003, 0.005, 0.0075, 0.010, 0.015]


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _reject_reason_for(ev: Dict[str, Any], self_impact_cap_bps: float) -> Optional[str]:
    """Apply ordered rejection rules; return reject_reason or None if accepted."""
    if not ev.get("meets_zero_loss_stress", False):
        return "stress_negative"
    if abs(float(ev.get("self_impact_bps", 0.0))) > float(self_impact_cap_bps):
        return "self_impact_too_high"
    if not bool(ev.get("guards_encodable", False)):
        return "guards_not_encodable"
    if not bool(ev.get("meets_required_profit", False)):
        return "below_required_profit"
    return None


def select_size_for_candidate(
    latest_curve_state,
    pending_buys: List[Tuple[int, float, str, int]],
    pending_sells: List[Tuple[int, int, str, int]],
    sizes_to_try: Optional[List[float]] = None,
    our_priority_fee_lamports: int = 0,
    ata_rent_sol: float = 0.0,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Dict[str, Any]:
    """Run size sweep + selection. Returns dict per module docstring.

    `sizes_to_try` defaults to DEFAULT_SIZES_SOL = [0.001, 0.002, 0.003,
    0.005, 0.0075, 0.010, 0.015]. Sweep order is ASCENDING by size.
    """
    evaluate_size, SELF_IMPACT_CAP_BPS = _import_evaluator()
    if sizes_to_try is None:
        sizes_to_try = list(DEFAULT_SIZES_SOL)
    # Ensure ascending unique
    sizes_to_try = sorted(set(float(s) for s in sizes_to_try if float(s) > 0))

    size_eval_table: List[Dict[str, Any]] = []
    accepted: List[Tuple[float, Dict[str, Any]]] = []
    for size_sol in sizes_to_try:
        ev = evaluate_size(
            float(size_sol),
            latest_curve_state,
            pending_buys,
            pending_sells,
            our_priority_fee_lamports=int(our_priority_fee_lamports),
            ata_rent_sol=float(ata_rent_sol),
        )
        reject = _reject_reason_for(ev, SELF_IMPACT_CAP_BPS)
        row = {
            "size_sol": float(size_sol),
            "stress_pnl": float(ev.get("stress_all_in_pnl", -1e9)),
            "all_in_pnl": float(ev.get("all_in_pnl", -1e9)),
            "edge_bps": float(ev.get("edge_bps", -1e9)),
            "impact_bps": float(ev.get("self_impact_bps", 0.0)),
            "fee_drag_bps": float(ev.get("fee_drag_bps", 0.0)),
            "guards_encodable": bool(ev.get("guards_encodable", False)),
            "meets_zero_loss_stress": bool(ev.get("meets_zero_loss_stress", False)),
            "meets_required_profit": bool(ev.get("meets_required_profit", False)),
            "required_profit_sol": float(ev.get("required_profit_sol", 0.0)),
            "projected_sell_out_sol": float(ev.get("projected_sell_out_sol", 0.0)),
            "stress_sell_out_sol": float(ev.get("stress_sell_out_sol", 0.0)),
            "buy_tokens_raw": int(ev.get("buy_tokens_raw", 0)),
            "min_token_buy_guard": int(ev.get("min_token_buy_guard", 0)),
            "min_sol_sell_guard": int(ev.get("min_sol_sell_guard", 0)),
            "reject_reason": reject,
        }
        size_eval_table.append(row)
        if reject is None:
            accepted.append((float(size_sol), ev))

    if not accepted:
        # Determine the dominant blocker for reason text.
        from collections import Counter
        reasons = Counter(
            (r["reject_reason"] or "unknown") for r in size_eval_table
        )
        reason_txt = (
            f"no_size_qualified (n={len(sizes_to_try)}, "
            + ", ".join(f"{k}={v}" for k, v in reasons.most_common()) + ")"
        )
        result = {
            "selected_size_sol": None,
            "expected_pnl": 0.0,
            "stress_pnl": 0.0,
            "reason": reason_txt,
            "size_eval_table": size_eval_table,
        }
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47-SIZE-SELECT mint={_short(mint_for_log)} "
                    f"selected=None reason=\"{reason_txt}\""
                )
            except Exception:
                pass
        return result

    # ---- Tiebreak (a): smallest size whose stress_pnl >= required_profit_sol ----
    fully_safe = [
        (s, ev) for (s, ev) in accepted
        if float(ev.get("stress_all_in_pnl", -1e9))
           >= float(ev.get("required_profit_sol", 0.0))
    ]
    if fully_safe:
        candidate_pool = fully_safe
        tiebreak_path = "smallest_with_stress_ge_required"
    else:
        # No accepted size has stress_pnl >= required_profit_sol; fall back to
        # all accepted (already pass stress >= 0, which is the hard floor).
        candidate_pool = accepted
        tiebreak_path = "smallest_accepted"

    # Sort by size ascending; pick smallest within the pool.
    candidate_pool.sort(key=lambda x: float(x[0]))
    smallest_size = float(candidate_pool[0][0])
    ties_at_smallest = [x for x in candidate_pool if float(x[0]) == smallest_size]

    # ---- Tiebreak (b): highest (all_in / stress) ratio ----
    if len(ties_at_smallest) > 1:
        def _ratio(item):
            s, ev = item
            ai = float(ev.get("all_in_pnl", 0.0))
            sp = max(float(ev.get("stress_all_in_pnl", 1e-12)), 1e-12)
            return ai / sp
        ties_at_smallest.sort(key=lambda x: -_ratio(x))
        best_ratio = _ratio(ties_at_smallest[0])
        ties_at_smallest = [
            x for x in ties_at_smallest if abs(_ratio(x) - best_ratio) < 1e-12
        ]

    # ---- Tiebreak (c): lowest self_impact_bps ----
    if len(ties_at_smallest) > 1:
        ties_at_smallest.sort(
            key=lambda x: abs(float(x[1].get("self_impact_bps", 0.0)))
        )

    selected_size, selected_ev = ties_at_smallest[0]
    expected_pnl = float(selected_ev.get("all_in_pnl", 0.0))
    stress_pnl = float(selected_ev.get("stress_all_in_pnl", 0.0))
    reason = (
        f"selected via {tiebreak_path}; size={selected_size} "
        f"stress={stress_pnl:+.6f} all_in={expected_pnl:+.6f} "
        f"req={float(selected_ev.get('required_profit_sol', 0.0)):.6f}"
    )

    if logger is not None:
        try:
            logger(
                f"PGG2-V47-SIZE-SELECT mint={_short(mint_for_log)} "
                f"selected={selected_size} expected_pnl={expected_pnl:+.6f} "
                f"stress_pnl={stress_pnl:+.6f} reason=\"{reason}\""
            )
        except Exception:
            pass

    return {
        "selected_size_sol": float(selected_size),
        "expected_pnl": float(expected_pnl),
        "stress_pnl": float(stress_pnl),
        "reason": reason,
        "size_eval_table": size_eval_table,
        # Full evaluator output for downstream (guards, fees, etc.)
        "selected_evaluation": selected_ev,
    }


__all__ = [
    "select_size_for_candidate",
    "DEFAULT_SIZES_SOL",
]
