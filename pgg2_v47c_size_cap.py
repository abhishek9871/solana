"""V47C - Size-by-Breadth Caps.

A causal post-multi-buyer-gate filter that limits the requested trade size
based on the breadth (unique_buyers) and concentration of the pre-curve buy
cluster. Rationale: a thin 2-buyer cluster cannot safely support 0.050 SOL
size; the catastrophic V47B loser #8 was a 0.050-SOL trade with only 1 buyer
and 0.10 SOL pending-buy total.

API:
  apply_size_cap(requested_size_sol, buyer_stats, pending_buy_sol_250ms)
  -> (capped_size_sol or None, reason)

Rules:
- unique_buyers_250ms < 2  -> (None, "no_entry_single_buyer")
- unique_buyers_250ms == 2 -> max_for_breadth = 0.020 SOL
- unique_buyers_250ms >= 3 -> max_for_breadth = 0.050 SOL
- If requested > 0.020:
    require (unique_buyers_250ms >= 3 OR unique_buyers_500ms >= 3) AND
            top_buyer_share_250ms <= 0.65 AND
            pending_buy_sol_250ms >= requested * 2
- capped = min(requested, max_for_breadth)
- If after cap, capped < 0.005 (minimum size) -> (None, "size_cap_too_restrictive")

NEVER allow 0.05 SOL on single-buyer or thin two-buyer signals.

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
            f"V47C-SIZE-CAP-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47c_size_cap")


# Configuration constants.
MIN_TRADE_SIZE_SOL = 0.005
MAX_FOR_UB2_SOL = 0.020
MAX_FOR_UB3_SOL = 0.050
TOP_SHARE_MAX_ABOVE_020 = 0.65


def apply_size_cap(
    requested_size_sol: float,
    buyer_stats: Dict[str, Any],
    pending_buy_sol_250ms: Optional[float] = None,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[Optional[float], str]:
    """Apply size-by-breadth cap. Returns (capped_size_or_None, reason)."""
    ub_250 = int(buyer_stats.get("unique_buyers_250ms", 0) or 0)
    ub_500 = int(buyer_stats.get("unique_buyers_500ms", 0) or 0)
    tbs_250 = float(buyer_stats.get("top_buyer_share_250ms", 0.0) or 0.0)
    pbs_250 = float(
        pending_buy_sol_250ms
        if pending_buy_sol_250ms is not None
        else buyer_stats.get("pending_buy_sol_250ms", 0.0) or 0.0
    )
    requested = float(requested_size_sol)

    capped: Optional[float] = None
    reason = ""

    if ub_250 < 2:
        reason = "no_entry_single_buyer"
        capped = None
    else:
        if ub_250 == 2:
            max_for_breadth = MAX_FOR_UB2_SOL
        else:  # ub_250 >= 3
            max_for_breadth = MAX_FOR_UB3_SOL

        # Stricter rules above 0.020.
        if requested > MAX_FOR_UB2_SOL:
            if ub_250 < 3 and ub_500 < 3:
                capped = None
                reason = (
                    "size_cap_strict_ub250_lt_3_and_ub500_lt_3"
                )
            elif tbs_250 > TOP_SHARE_MAX_ABOVE_020:
                capped = None
                reason = "size_cap_top_buyer_share_gt_065"
            elif pbs_250 < requested * 2.0:
                capped = None
                reason = "size_cap_pending_buy_sol_lt_2x_requested"
            else:
                c = min(requested, max_for_breadth)
                if c < MIN_TRADE_SIZE_SOL:
                    capped = None
                    reason = "size_cap_too_restrictive"
                else:
                    capped = float(c)
                    if c < requested:
                        reason = (
                            f"cap_applied:reduce_to_{c:.4f}"
                            f"_from_{requested:.4f}"
                        )
                    else:
                        reason = f"cap_applied:ok_{c:.4f}"
        else:
            # Requested <= 0.020 SOL.
            c = min(requested, max_for_breadth)
            if c < MIN_TRADE_SIZE_SOL:
                capped = None
                reason = "size_cap_too_restrictive"
            else:
                capped = float(c)
                if c < requested:
                    reason = (
                        f"cap_applied:reduce_to_{c:.4f}"
                        f"_from_{requested:.4f}"
                    )
                else:
                    reason = f"cap_applied:ok_{c:.4f}"

    if logger is not None:
        try:
            short = (
                (mint_for_log[:4] + ".." + mint_for_log[-4:])
                if mint_for_log and len(mint_for_log) > 10
                else (mint_for_log or "-")
            )
            cap_str = f"{capped:.4f}" if capped is not None else "BLOCK"
            logger(
                f"PGG2-V47C-SIZE-CAP mint={short} "
                f"requested={requested:.4f} "
                f"capped={cap_str} "
                f"ub250={ub_250} ub500={ub_500} "
                f"tbshare={tbs_250:.3f} "
                f"pbsol250={pbs_250:.6f} "
                f"pass={int(capped is not None)} reason={reason}"
            )
        except Exception:
            pass

    return (capped, reason)


__all__ = [
    "apply_size_cap",
    "MIN_TRADE_SIZE_SOL",
    "MAX_FOR_UB2_SOL",
    "MAX_FOR_UB3_SOL",
    "TOP_SHARE_MAX_ABOVE_020",
]
