"""V47F - Mid-Hold Dump Abort.

Detects in-position degradation BEFORE clamp threshold trips and exits
preemptively. Addresses the FScZ pattern where PnL went positive, extend
fired, then quote/curve collapsed and the position rode all the way to
clamp_loss at -0.011972.

Triggers (ANY one fires):
  1. local_sell_quote drops by >= 0.000500 SOL from recent peak.
  2. local_sell_quote drops below break_even + 0.000050 after previously
     being above bank/scratch threshold.
  3. curve vsol delta falls by >= 5% from local peak (peak_vsol>0 AND
     current_vsol/peak_vsol < 0.95).
  4. pending_sell_sol_250ms >= pending_buy_sol_250ms (sell pressure flip).
  5. quote_gradient negative for 2 consecutive updates.

Actions (priority order):
  - current_all_in_pnl >= +0.000050 -> "scratch_exit_immediately"
  - current_all_in_pnl >= 0         -> "close_flat_immediately"
  - current_all_in_pnl <  0         -> "emergency_exit_negative"

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
            f"V47F-MIDHOLD-ABORT-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47f_midhold_dump_abort")


QUOTE_DROP_SOL = 0.000500
BREAKEVEN_SCRATCH_BUFFER = 0.000050
CURVE_VSOL_DROP_FRAC = 0.95
PB_OVER_PS_FLIP_THRESHOLD = 1.0
NEG_GRADIENT_CONSECUTIVE = 2

ACTION_SCRATCH = "scratch_exit_immediately"
ACTION_FLAT = "close_flat_immediately"
ACTION_EMERGENCY = "emergency_exit_negative"
SCRATCH_PNL_MIN = 0.000050


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def should_abort_midhold(
    position_state: Dict[str, Any],
    recent_trajectory: Dict[str, Any],
    logger: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Evaluate whether to abort the position mid-hold.

    position_state required keys:
      mint
      size_sol
      current_all_in_pnl
      peak_all_in_pnl
      current_local_sell_quote
      peak_local_sell_quote
      current_curve_vsol
      peak_curve_vsol
      latest_pending_buy_sol_250ms
      latest_pending_sell_sol_250ms
      latest_quote_gradient
      consecutive_negative_gradient_count
      had_been_above_bank_or_scratch (optional bool)
      break_even_sell_quote (optional float)

    recent_trajectory: reserved for future expansion. Unused for now.

    Returns:
      (abort_bool, action, reason)
        abort_bool=False -> (False, None, None)
        abort_bool=True  -> (True, "<action>", "<reason>")
    """
    mint = str(position_state.get("mint", ""))
    size_sol = float(position_state.get("size_sol", 0.0))
    pnl = float(position_state.get("current_all_in_pnl", 0.0))
    peak_pnl = float(position_state.get("peak_all_in_pnl", 0.0))
    cur_quote = float(position_state.get("current_local_sell_quote", 0.0))
    peak_quote = float(position_state.get("peak_local_sell_quote", 0.0))
    cur_vsol = float(position_state.get("current_curve_vsol", 0.0))
    peak_vsol = float(position_state.get("peak_curve_vsol", 0.0))
    pbs = float(position_state.get("latest_pending_buy_sol_250ms", 0.0))
    pss = float(position_state.get("latest_pending_sell_sol_250ms", 0.0))
    grad = float(position_state.get("latest_quote_gradient", 0.0))
    neg_grad_count = int(
        position_state.get("consecutive_negative_gradient_count", 0)
    )
    had_been_above = bool(
        position_state.get("had_been_above_bank_or_scratch", False)
    )
    bk_even_quote = float(position_state.get("break_even_sell_quote", 0.0))

    # Reserved
    _ = recent_trajectory

    fired_triggers = []
    quote_drop = peak_quote - cur_quote
    if peak_quote > 0.0 and quote_drop >= QUOTE_DROP_SOL - 1e-12:
        fired_triggers.append(
            f"quote_drop_{int(round(quote_drop*1e6))}u_geq_500u"
        )

    if had_been_above and bk_even_quote > 0.0:
        threshold = bk_even_quote + BREAKEVEN_SCRATCH_BUFFER
        if cur_quote < threshold - 1e-12:
            fired_triggers.append(
                "quote_below_breakeven_plus_50u_after_having_been_above"
            )

    curve_drop = 0.0
    if peak_vsol > 0.0:
        if cur_vsol / peak_vsol < CURVE_VSOL_DROP_FRAC - 1e-12:
            curve_drop = 1.0 - (cur_vsol / peak_vsol)
            fired_triggers.append(
                f"curve_vsol_drop_{int(round(curve_drop*100))}pct_geq_5pct"
            )

    if pss >= pbs * PB_OVER_PS_FLIP_THRESHOLD - 1e-12 and (pbs > 0.0 or pss > 0.0):
        fired_triggers.append("pending_sell_geq_pending_buy_flip")

    if neg_grad_count >= NEG_GRADIENT_CONSECUTIVE:
        fired_triggers.append(
            f"quote_gradient_negative_{neg_grad_count}_consecutive_updates"
        )

    if not fired_triggers:
        return (False, None, None)

    # Action priority
    if pnl >= SCRATCH_PNL_MIN - 1e-12:
        action = ACTION_SCRATCH
    elif pnl >= 0.0 - 1e-12:
        action = ACTION_FLAT
    else:
        action = ACTION_EMERGENCY

    reason = "|".join(fired_triggers)

    if logger is not None:
        try:
            logger(
                f"PGG2-V47F-MIDHOLD-DUMP-ABORT mint={_short(mint)} "
                f"size={size_sol:.4f} quote_drop={quote_drop:+.6f} "
                f"curve_drop={curve_drop:.4f} pb={pbs:.6f} ps={pss:.6f} "
                f"pnl={pnl:+.6f} peak_pnl={peak_pnl:+.6f} "
                f"neg_grad_count={neg_grad_count} action={action} "
                f"reason={reason}"
            )
        except Exception:
            pass

    return (True, action, reason)


__all__ = [
    "should_abort_midhold",
    "ACTION_SCRATCH", "ACTION_FLAT", "ACTION_EMERGENCY",
    "QUOTE_DROP_SOL", "BREAKEVEN_SCRATCH_BUFFER",
    "CURVE_VSOL_DROP_FRAC", "PB_OVER_PS_FLIP_THRESHOLD",
    "NEG_GRADIENT_CONSECUTIVE", "SCRATCH_PNL_MIN",
]


# ---- self-tests (executed at import) ---------------------------------
def _self_test() -> None:
    # No abort: stable, no triggers.
    state = {
        "mint": "TestNoAbort1234567",
        "size_sol": 0.030,
        "current_all_in_pnl": 0.002,
        "peak_all_in_pnl": 0.002,
        "current_local_sell_quote": 0.033,
        "peak_local_sell_quote": 0.033,
        "current_curve_vsol": 30.0,
        "peak_curve_vsol": 30.0,
        "latest_pending_buy_sol_250ms": 0.5,
        "latest_pending_sell_sol_250ms": 0.05,
        "latest_quote_gradient": 0.0,
        "consecutive_negative_gradient_count": 0,
    }
    ab, action, reason = should_abort_midhold(state, {})
    if ab:
        raise RuntimeError(f"v47f_midhold_selftest_stable_should_not_abort: {ab} {action} {reason}")

    # Trigger 1: quote drop >= 0.000500.
    state2 = dict(state)
    state2["current_local_sell_quote"] = 0.0325  # 0.0005 drop
    ab, action, reason = should_abort_midhold(state2, {})
    if not ab or "quote_drop" not in reason:
        raise RuntimeError(f"v47f_midhold_selftest_quote_drop_fail: {ab} {action} {reason}")
    if action != ACTION_SCRATCH:
        raise RuntimeError(
            f"v47f_midhold_selftest_quote_drop_pnl_positive_should_scratch: {action}"
        )

    # Trigger 3: curve vsol drop >= 5%.
    state3 = dict(state)
    state3["current_curve_vsol"] = 28.0  # 6.67% drop
    ab, action, reason = should_abort_midhold(state3, {})
    if not ab or "curve_vsol_drop" not in reason:
        raise RuntimeError(f"v47f_midhold_selftest_curve_drop_fail: {ab} {action} {reason}")

    # Trigger 4: pss >= pbs.
    state4 = dict(state)
    state4["latest_pending_buy_sol_250ms"] = 0.05
    state4["latest_pending_sell_sol_250ms"] = 0.05
    ab, action, reason = should_abort_midhold(state4, {})
    if not ab or "pending_sell_geq_pending_buy" not in reason:
        raise RuntimeError(f"v47f_midhold_selftest_pss_flip_fail: {ab} {action} {reason}")

    # Trigger 5: 2 consecutive negative gradient updates.
    state5 = dict(state)
    state5["consecutive_negative_gradient_count"] = 2
    ab, action, reason = should_abort_midhold(state5, {})
    if not ab or "negative_2_consecutive" not in reason:
        raise RuntimeError(f"v47f_midhold_selftest_neg_grad_fail: {ab} {action} {reason}")

    # Action: pnl negative -> emergency.
    state_neg = dict(state2)  # quote drop trigger
    state_neg["current_all_in_pnl"] = -0.002
    ab, action, reason = should_abort_midhold(state_neg, {})
    if not ab or action != ACTION_EMERGENCY:
        raise RuntimeError(
            f"v47f_midhold_selftest_neg_pnl_should_emergency: {action} {reason}"
        )

    # Action: pnl small positive < scratch min -> close_flat.
    state_flat = dict(state2)
    state_flat["current_all_in_pnl"] = 0.00001  # below scratch_pnl_min=0.00005
    ab, action, reason = should_abort_midhold(state_flat, {})
    if not ab or action != ACTION_FLAT:
        raise RuntimeError(
            f"v47f_midhold_selftest_flat_pnl_should_close_flat: {action} {reason}"
        )

    # FScZ pattern simulation: pnl was 0.003 then quote dropped sharply.
    state_fscz = {
        "mint": "FScZ_simulation",
        "size_sol": 0.030,
        "current_all_in_pnl": -0.005,
        "peak_all_in_pnl": 0.003,
        "current_local_sell_quote": 0.025,
        "peak_local_sell_quote": 0.034,  # drop=0.009 >> 0.0005
        "current_curve_vsol": 25.0,
        "peak_curve_vsol": 30.0,         # 16.7% drop
        "latest_pending_buy_sol_250ms": 0.02,
        "latest_pending_sell_sol_250ms": 0.10,  # ps >> pb
        "latest_quote_gradient": -0.0005,
        "consecutive_negative_gradient_count": 3,
    }
    ab, action, reason = should_abort_midhold(state_fscz, {})
    if not ab:
        raise RuntimeError(
            f"v47f_midhold_selftest_fscz_pattern_should_abort: {ab} {action} {reason}"
        )
    if action != ACTION_EMERGENCY:
        raise RuntimeError(
            f"v47f_midhold_selftest_fscz_pattern_should_emergency: {action} {reason}"
        )


_self_test()
