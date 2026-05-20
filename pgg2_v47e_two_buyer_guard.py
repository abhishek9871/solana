"""V47E - Strict Two-Buyer Concentration Guard.

Addresses the FrSN dry-live loss (V47D):
  FrSN..pump, size=0.005, ub_250=2, top_buyer_share=0.674,
  expected_pnl=+0.006091 -> observed -0.001001 at 296ms.

V47D Rule A only constrained tbs at size>=0.015; at size=0.005 with ub=2
there was no tbs ceiling. V47E adds explicit tbs limits for ub_250==2 at
EVERY size and blocks ub_250==1 actual entries entirely.

Mode semantics:
  - "actual_pass"  : admit at requested size (subject to all V47D + V47E rules)
  - "shadow_only"  : record-only candidate (NOT routed to actual buy path)
  - "block"        : neither actual nor shadow; rejection with reason

For unique_buyers_250ms == 2:
  - size must be <= 0.010 SOL (block if size >= 0.020)
  - top_buyer_share_250ms > 0.60 -> block
  - 0.55 < top_buyer_share_250ms <= 0.60 -> shadow_only
  - top_buyer_share_250ms <= 0.55 -> continue
  - largest_buy_share_250ms > 0.55 -> block
  - pending_buy_count_250ms < 2 -> block
  - pending_buy_sol_250ms <= pending_sell_sol_250ms * 2 -> block
  - expected_pnl < +0.000900 -> block
  - no_negative_curve_update_250ms == False -> block

For unique_buyers_250ms == 1:
  - block actual entry, reason="single_buyer_actual_blocked"

For unique_buyers_250ms == 0:
  - block, reason="no_buyers"

For unique_buyers_250ms >= 3:
  - delegate to V47D boundary guard (return ("delegate_v47d", "ub_geq_3"))

Hard rule (enforce in code): FrSN-style (ub=2, tbs=0.674) -> must return
("block", "ub2_tbs_gt_060_block")

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
            f"V47E-TWO-BUYER-GUARD-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47e_two_buyer_guard")


# ---- V47E ub=2 thresholds -------------------------------------------
UB2_MAX_SIZE_SOL = 0.010
UB2_BLOCK_SIZE_THRESHOLD = 0.020  # >= 0.020 -> block
UB2_TBS_BLOCK = 0.60     # tbs > 0.60 -> block
UB2_TBS_SHADOW = 0.55    # 0.55 < tbs <= 0.60 -> shadow_only
UB2_LARGEST_SHARE_MAX = 0.55
UB2_PB_COUNT_MIN = 2
UB2_PB_SOL_MULT_OVER_SELL = 2.0
UB2_EXP_PNL_MIN = 0.000900


MODE_ACTUAL = "actual_pass"
MODE_SHADOW = "shadow_only"
MODE_BLOCK = "block"
MODE_DELEGATE_V47D = "delegate_v47d"


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "-"
    return mint[:4] + ".." + mint[-4:]


def evaluate_two_buyer_guard(
    size_sol: float,
    buyer_stats: Dict[str, Any],
    expected_pnl: float,
    no_negative_curve_update_250ms: bool,
    adverse_branch_outcome: str,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[str, str]:
    """V47E two-buyer concentration guard.

    Returns (mode, reason) where mode is one of:
      "actual_pass", "shadow_only", "block", "delegate_v47d"
    """
    s = float(size_sol)
    ub_250 = int(buyer_stats.get("unique_buyers_250ms", 0) or 0)
    tbs_250 = float(buyer_stats.get("top_buyer_share_250ms", 0.0) or 0.0)
    pbs_250 = float(buyer_stats.get("pending_buy_sol_250ms", 0.0) or 0.0)
    pss_250 = float(buyer_stats.get("pending_sell_sol_250ms", 0.0) or 0.0)
    lbs_250 = float(buyer_stats.get("largest_buy_sol_250ms", 0.0) or 0.0)
    pb_count_250 = int(buyer_stats.get("pending_buy_count_250ms", 0) or 0)
    exp_pnl = float(expected_pnl)
    no_neg_curve = bool(no_negative_curve_update_250ms)
    _ = str(adverse_branch_outcome or "")  # reserved for future cross-check

    if pbs_250 > 0.0:
        largest_buy_share_250ms = lbs_250 / pbs_250
    else:
        largest_buy_share_250ms = 0.0

    mode: str
    reason: str

    if ub_250 == 0:
        mode, reason = MODE_BLOCK, "no_buyers"
    elif ub_250 == 1:
        mode, reason = MODE_BLOCK, "single_buyer_actual_blocked"
    elif ub_250 == 2:
        # ---- Size guards FIRST -----------------------------------------
        if s >= UB2_BLOCK_SIZE_THRESHOLD - 1e-9:
            mode, reason = MODE_BLOCK, "ub2_size_geq_020"
        else:
            # tbs check FIRST (so FrSN's tbs=0.674 trips immediately)
            if tbs_250 > UB2_TBS_BLOCK + 1e-9:
                mode, reason = MODE_BLOCK, "ub2_tbs_gt_060_block"
            elif tbs_250 > UB2_TBS_SHADOW + 1e-9:
                mode, reason = MODE_SHADOW, "ub2_tbs_gt_055_shadow"
            elif s > UB2_MAX_SIZE_SOL + 1e-9:
                # tbs <= 0.55 but size > 0.010 and < 0.020:
                # explicitly block actual at sizes 0.015 onward
                mode, reason = MODE_BLOCK, "ub2_size_gt_010_actual_blocked"
            elif largest_buy_share_250ms > UB2_LARGEST_SHARE_MAX + 1e-9:
                mode, reason = MODE_BLOCK, "ub2_largest_buy_share_gt_055"
            elif pb_count_250 < UB2_PB_COUNT_MIN:
                mode, reason = MODE_BLOCK, "ub2_pbc_lt_2"
            elif not (pbs_250 > pss_250 * UB2_PB_SOL_MULT_OVER_SELL):
                mode, reason = MODE_BLOCK, "ub2_pbsol_lt_2x_pssol"
            elif exp_pnl < UB2_EXP_PNL_MIN:
                mode, reason = MODE_BLOCK, "ub2_exp_pnl_lt_0009"
            elif not no_neg_curve:
                mode, reason = MODE_BLOCK, "ub2_negative_curve_update_recent"
            else:
                mode, reason = MODE_ACTUAL, "ub2_pass"
    else:  # ub_250 >= 3
        mode, reason = MODE_DELEGATE_V47D, "ub_geq_3"

    if logger is not None:
        try:
            logger(
                f"PGG2-V47E-TWO-BUYER-GUARD mint={_short(mint_for_log)} "
                f"ub={ub_250} tbs={tbs_250:.3f} "
                f"lbs={largest_buy_share_250ms:.3f} "
                f"pbc={pb_count_250} pbs={pbs_250:.6f} "
                f"pss={pss_250:.6f} exp={exp_pnl:+.6f} "
                f"size={s:.4f} mode={mode} reason={reason}"
            )
        except Exception:
            pass

    return (mode, reason)


__all__ = [
    "evaluate_two_buyer_guard",
    "MODE_ACTUAL", "MODE_SHADOW", "MODE_BLOCK", "MODE_DELEGATE_V47D",
    "UB2_MAX_SIZE_SOL", "UB2_BLOCK_SIZE_THRESHOLD",
    "UB2_TBS_BLOCK", "UB2_TBS_SHADOW", "UB2_LARGEST_SHARE_MAX",
    "UB2_PB_COUNT_MIN", "UB2_PB_SOL_MULT_OVER_SELL", "UB2_EXP_PNL_MIN",
]


# ---- self-test for FrSN hard rule (executed at import) --------------
def _self_test_frsn() -> None:
    """Verify FrSN-style ub=2 tbs=0.674 returns block ub2_tbs_gt_060_block.

    The downsizer reduces FrSN to size=0.005, so we must verify the rule
    fires at size=0.005 specifically.
    """
    bs = {
        "unique_buyers_250ms": 2,
        "top_buyer_share_250ms": 0.6737457879406472,
        "pending_buy_count_250ms": 2,
        "pending_buy_sol_250ms": 1.0,
        "pending_sell_sol_250ms": 0.0,
        "largest_buy_sol_250ms": 0.674,
    }
    mode, reason = evaluate_two_buyer_guard(
        size_sol=0.005,
        buyer_stats=bs,
        expected_pnl=0.0060907159999999995,
        no_negative_curve_update_250ms=True,
        adverse_branch_outcome="BRANCH_SAFE_BUY_FAIL",
    )
    if not (mode == MODE_BLOCK and reason == "ub2_tbs_gt_060_block"):
        sys.stderr.write(
            f"V47E-TWO-BUYER-SELFTEST-FAIL mode={mode} reason={reason}\n"
        )
        raise RuntimeError("v47e_two_buyer_self_test_frsn_did_not_block")


_self_test_frsn()
