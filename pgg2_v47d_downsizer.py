"""V47D - Downsize-before-block router.

When a candidate's originally-selected size fails the V47D boundary guard,
this module attempts to find a smaller admissible size BEFORE giving up
entirely. The intention is to keep candidate flow at a safer size rather
than throwing away the whole signal.

API:
  downsize_candidate(
    initial_selected_size,
    buyer_stats,
    expected_pnl_fn,        # callable: size_sol -> expected_pnl
    no_negative_curve_update_250ms,
    adverse_branch_outcome,
    branch_check_fn=None,   # callable: size_sol -> (selectable: bool, reason)
    multi_buyer_pass=True,  # multi-buyer gate is size-agnostic; passed in
    logger=None,
    mint_for_log="",
  ) -> (final_size or None, action: str, reason: str)

Where action in ("original", "downsized", "blocked").

Descending size retry order: [original, 0.015, 0.010, 0.005] filtered to
sizes <= original. The hard rule is enforced: size==0.020 with ub_250<3 is
SKIPPED at every retry step (mirrors V47D rule A).

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Callable, Dict, Optional, Tuple


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
            f"V47D-DOWNSIZER-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47d_downsizer")


# Candidate retry sizes (descending order). Subset to sizes <= original.
DOWNSIZE_RETRY_ORDER = (0.020, 0.015, 0.010, 0.005)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "-"
    return mint[:4] + ".." + mint[-4:]


def downsize_candidate(
    initial_selected_size: float,
    buyer_stats: Dict[str, Any],
    expected_pnl_fn: Callable[[float], float],
    no_negative_curve_update_250ms: bool,
    adverse_branch_outcome: str,
    branch_check_fn: Optional[Callable[[float], Tuple[bool, Optional[str]]]] = None,
    multi_buyer_pass: bool = True,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[Optional[float], str, str]:
    """Try the original size first; if it fails, walk down candidate sizes.

    Returns (final_size_or_None, action, reason).
      action: "original" | "downsized" | "blocked"
      reason: "ok" on success; the last-attempted blocker reason on failure.
    """
    from pgg2_v47d_boundary_guard import (  # type: ignore
        evaluate_boundary_guard,
    )

    original = float(initial_selected_size)
    ub_250 = int(buyer_stats.get("unique_buyers_250ms", 0) or 0)

    # Build retry order: original first, then descending sizes <= original
    # excluding any > original. Also enforce ub_250<3 -> skip 0.020.
    retry: list = []
    if original > 0:
        retry.append(original)
    for s in DOWNSIZE_RETRY_ORDER:
        if abs(s - original) < 1e-9:
            continue
        if s > original + 1e-9:
            continue
        retry.append(float(s))
    # Hard rule: ub_250 < 3 forbids 0.020 SOL at any retry.
    if ub_250 < 3:
        retry = [s for s in retry if s < 0.020 - 1e-9]

    if not multi_buyer_pass:
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47D-DOWNSIZE mint={_short(mint_for_log)} "
                    f"original={original:.4f} downsized=- "
                    f"reason=multi_buyer_gate_failed exp_pnl_after=- pass=0"
                )
            except Exception:
                pass
        return (None, "blocked", "multi_buyer_gate_failed")

    last_reason: str = "no_smaller_size_works"

    for candidate_size in retry:
        # branch sim re-check
        br_ok = True
        br_reason: Optional[str] = None
        if branch_check_fn is not None:
            try:
                br_ok, br_reason = branch_check_fn(candidate_size)
            except Exception as exc:
                br_ok = False
                br_reason = f"branch_check_exc:{type(exc).__name__}"
        if not br_ok:
            last_reason = (
                f"branch_check_fail:{br_reason}"
                if br_reason else "branch_check_fail"
            )
            if logger is not None:
                try:
                    logger(
                        f"PGG2-V47D-DOWNSIZE-STEP mint={_short(mint_for_log)} "
                        f"try={candidate_size:.4f} "
                        f"branch_pass=0 reason={last_reason}"
                    )
                except Exception:
                    pass
            continue

        # recompute expected_pnl at this size
        try:
            exp_pnl_at_size = float(expected_pnl_fn(candidate_size))
        except Exception as exc:
            last_reason = f"expected_pnl_fn_exc:{type(exc).__name__}"
            continue

        passes, blocker = evaluate_boundary_guard(
            size_sol=candidate_size,
            buyer_stats=buyer_stats,
            expected_pnl=exp_pnl_at_size,
            no_negative_curve_update_250ms=no_negative_curve_update_250ms,
            adverse_branch_outcome=adverse_branch_outcome,
            logger=None,  # don't double-log
            mint_for_log=mint_for_log,
        )

        if logger is not None:
            try:
                logger(
                    f"PGG2-V47D-DOWNSIZE-STEP mint={_short(mint_for_log)} "
                    f"try={candidate_size:.4f} "
                    f"exp_pnl={exp_pnl_at_size:+.6f} "
                    f"branch_pass=1 boundary_pass={int(passes)} "
                    f"blocker={blocker or '-'}"
                )
            except Exception:
                pass

        if passes:
            action = (
                "original" if abs(candidate_size - original) < 1e-9
                else "downsized"
            )
            if logger is not None:
                try:
                    logger(
                        f"PGG2-V47D-DOWNSIZE mint={_short(mint_for_log)} "
                        f"original={original:.4f} "
                        f"downsized={candidate_size:.4f} "
                        f"reason=ok exp_pnl_after={exp_pnl_at_size:+.6f} "
                        f"pass=1"
                    )
                except Exception:
                    pass
            return (float(candidate_size), action, "ok")

        last_reason = str(blocker or "unknown")

    if logger is not None:
        try:
            logger(
                f"PGG2-V47D-DOWNSIZE mint={_short(mint_for_log)} "
                f"original={original:.4f} downsized=- "
                f"reason={last_reason} exp_pnl_after=- pass=0"
            )
        except Exception:
            pass

    return (None, "blocked", last_reason)


__all__ = ["downsize_candidate", "DOWNSIZE_RETRY_ORDER"]
