"""V47D - Boundary-Loss Guard.

Tightens the V47C entry criteria to block the precise boundary pattern that
produced the single V47C loss (47eKt... at 0.020 SOL, ub_250=2,
top_buyer_share=0.571, expected_pnl=+0.000450, observed -0.001377 at 251ms).

Rule families (most-specific wins; first failing condition determines the
blocker reason):

  D. Universal rules (applied first):
       - expected_pnl >= max(0.000600, size_sol * 0.030)
       - pending_buy_sol_250ms > pending_sell_sol_250ms * 2.0
       - adverse_branch_outcome in (BRANCH_SAFE_BUY_FAIL, BRANCH_WIN)

  C. size_sol >= 0.050 (large size, strict):
       - unique_buyers_250ms >= 4
       - unique_buyers_500ms >= 4
       - top_buyer_share_250ms <= 0.45
       - pending_buy_sol_250ms >= size_sol * 6
       - expected_pnl >= +0.001500

  B. size_sol >= 0.020 (medium size, no narrow-cluster):
       - unique_buyers_250ms >= 3
       - top_buyer_share_250ms <= 0.55
       - pending_buy_sol_250ms >= size_sol * 5
       - expected_pnl >= +0.000800
       - largest_buy_share_250ms <= 0.55

  A. unique_buyers_250ms == 2 (narrow cluster):
       - size_sol == 0.010 always allowed (subject to D)
       - size_sol == 0.015 allowed ONLY if ALL of:
           top_buyer_share_250ms <= 0.50
           pending_buy_sol_250ms >= size_sol * 4
           expected_pnl >= +0.000900
           no_negative_curve_update_250ms == True
       - size_sol >= 0.020 BLOCKED (rule A overrides rule B for ub_250==2)
       - size_sol == 0.005 allowed (subject to D)

The V47C 47eK loss had ub_250=2, size=0.020 -> rule A blocks at
"ub_2_size_geq_020_blocked".

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
            f"V47D-BOUNDARY-GUARD-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47d_boundary_guard")


# ---- Rule constants --------------------------------------------------
# Universal (Rule D)
PROFIT_FLOOR_BASE = 0.000600
PROFIT_FLOOR_SIZE_NORM = 0.030  # required_profit = max(BASE, size * SIZE_NORM)
PB_SOL_GT_2X_SELL = 2.0

# Rule A: ub_250 == 2
A_MAX_SIZE_DEFAULT = 0.010
A_15_TOP_SHARE_MAX = 0.50
A_15_PB_SOL_MULT = 4.0
A_15_EXP_PNL_MIN = 0.000900

# Rule B: size >= 0.020
B_UB_MIN_250 = 3
B_TOP_SHARE_MAX = 0.55
B_PB_SOL_MULT = 5.0
B_EXP_PNL_MIN = 0.000800
B_LARGEST_SHARE_MAX = 0.55

# Rule C: size >= 0.050
C_UB_MIN_250 = 4
C_UB_MIN_500 = 4
C_TOP_SHARE_MAX = 0.45
C_PB_SOL_MULT = 6.0
C_EXP_PNL_MIN = 0.001500


def _required_profit_floor(size_sol: float) -> float:
    return max(PROFIT_FLOOR_BASE, float(size_sol) * PROFIT_FLOOR_SIZE_NORM)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "-"
    return mint[:4] + ".." + mint[-4:]


def evaluate_boundary_guard(
    size_sol: float,
    buyer_stats: Dict[str, Any],
    expected_pnl: float,
    no_negative_curve_update_250ms: bool,
    adverse_branch_outcome: str,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[bool, Optional[str]]:
    """Apply V47D boundary-loss guard.

    Returns (pass, blocker_reason). pass=True => admit; False => block.
    """
    s = float(size_sol)
    ub_250 = int(buyer_stats.get("unique_buyers_250ms", 0) or 0)
    ub_500 = int(buyer_stats.get("unique_buyers_500ms", 0) or 0)
    tbs_250 = float(buyer_stats.get("top_buyer_share_250ms", 0.0) or 0.0)
    pbs_250 = float(buyer_stats.get("pending_buy_sol_250ms", 0.0) or 0.0)
    pss_250 = float(buyer_stats.get("pending_sell_sol_250ms", 0.0) or 0.0)
    lbs_250 = float(buyer_stats.get("largest_buy_sol_250ms", 0.0) or 0.0)
    exp_pnl = float(expected_pnl)
    adv = str(adverse_branch_outcome or "")
    no_neg_curve = bool(no_negative_curve_update_250ms)

    if pbs_250 > 0.0:
        largest_buy_share_250ms = lbs_250 / pbs_250
    else:
        largest_buy_share_250ms = 0.0

    blocker: Optional[str] = None
    passes = True

    # ---- D: universal -------------------------------------------------
    required_floor = _required_profit_floor(s)
    if exp_pnl < required_floor:
        passes = False
        blocker = "expected_pnl_below_size_normalized_floor"
    elif not (pbs_250 > pss_250 * PB_SOL_GT_2X_SELL):
        passes = False
        blocker = "buy_sol_not_2x_sell_sol"
    elif adv not in ("BRANCH_SAFE_BUY_FAIL", "BRANCH_WIN"):
        passes = False
        blocker = "adverse_unsafe"

    # ---- C: size >= 0.050 ---------------------------------------------
    if passes and s >= 0.050 - 1e-9:
        if ub_250 < C_UB_MIN_250:
            passes = False
            blocker = "size_geq_050_ub_lt_4"
        elif ub_500 < C_UB_MIN_500:
            passes = False
            blocker = "size_geq_050_ub500_lt_4"
        elif tbs_250 > C_TOP_SHARE_MAX:
            passes = False
            blocker = "size_geq_050_top_share_gt_045"
        elif pbs_250 < s * C_PB_SOL_MULT:
            passes = False
            blocker = "size_geq_050_pbsol_lt_6x"
        elif exp_pnl < C_EXP_PNL_MIN:
            passes = False
            blocker = "size_geq_050_pnl_lt_00015"

    # ---- B: size >= 0.020 ---------------------------------------------
    if passes and s >= 0.020 - 1e-9:
        if ub_250 < B_UB_MIN_250:
            passes = False
            blocker = "size_geq_020_ub_lt_3"
        elif tbs_250 > B_TOP_SHARE_MAX:
            passes = False
            blocker = "size_geq_020_top_share_gt_055"
        elif pbs_250 < s * B_PB_SOL_MULT:
            passes = False
            blocker = "size_geq_020_pbsol_lt_5x"
        elif exp_pnl < B_EXP_PNL_MIN:
            passes = False
            blocker = "size_geq_020_pnl_lt_00008"
        elif largest_buy_share_250ms > B_LARGEST_SHARE_MAX:
            passes = False
            blocker = "size_geq_020_largest_share_gt_055"

    # ---- A: ub_250 == 2 (overrides B for the precise V47C 47eK case) --
    # IMPORTANT: A is applied LAST so its tighter ub_2 rule wins for
    # sizes >= 0.020. The "ub_2_size_geq_020_blocked" wording overrides
    # any size-band reason from B for ub_250 == 2.
    if ub_250 == 2 and s >= 0.020 - 1e-9:
        passes = False
        blocker = "ub_2_size_geq_020_blocked"
    elif passes and ub_250 == 2 and (s >= 0.015 - 1e-9) and (s < 0.020 - 1e-9):
        # size=0.015 with ub_250==2 has stricter constraints
        if tbs_250 > A_15_TOP_SHARE_MAX:
            passes = False
            blocker = "ub_2_size_015_top_share_gt_050"
        elif pbs_250 < s * A_15_PB_SOL_MULT:
            passes = False
            blocker = "ub_2_size_015_pbsol_lt_4x"
        elif exp_pnl < A_15_EXP_PNL_MIN:
            passes = False
            blocker = "ub_2_size_015_pnl_lt_00009"
        elif not no_neg_curve:
            passes = False
            blocker = "ub_2_size_015_negative_curve_update"

    if logger is not None:
        try:
            logger(
                f"PGG2-V47D-BOUNDARY-GUARD mint={_short(mint_for_log)} "
                f"size={s:.4f} exp_pnl={exp_pnl:+.6f} "
                f"ub={ub_250} tbs={tbs_250:.3f} "
                f"pbs={pbs_250:.6f} pss={pss_250:.6f} "
                f"lbs={largest_buy_share_250ms:.3f} "
                f"pass={int(passes)} blocker={blocker or '-'}"
            )
        except Exception:
            pass

    return (bool(passes), blocker)


__all__ = [
    "evaluate_boundary_guard",
    "PROFIT_FLOOR_BASE",
    "PROFIT_FLOOR_SIZE_NORM",
    "PB_SOL_GT_2X_SELL",
    "A_MAX_SIZE_DEFAULT",
    "A_15_TOP_SHARE_MAX",
    "A_15_PB_SOL_MULT",
    "A_15_EXP_PNL_MIN",
    "B_UB_MIN_250",
    "B_TOP_SHARE_MAX",
    "B_PB_SOL_MULT",
    "B_EXP_PNL_MIN",
    "B_LARGEST_SHARE_MAX",
    "C_UB_MIN_250",
    "C_UB_MIN_500",
    "C_TOP_SHARE_MAX",
    "C_PB_SOL_MULT",
    "C_EXP_PNL_MIN",
]
