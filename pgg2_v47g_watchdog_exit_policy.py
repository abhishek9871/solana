"""V47G - Watchdog exit policy.

Pure decision logic for the V47G open-position quote watchdog. Given a
position state and the latest local-CPMM sell quote, decide one of:

  bank                  - pnl >= bank threshold
  scratch               - pnl >= scratch min AND quote deteriorating
  abort_*               - V47F mid-hold abort triggered (carry forward action)
  max_hold_*            - position age exceeds size-tier max_hold cap
  hold                  - none of the above; keep position

This module is consumed by pgg2_v47g_position_quote_watchdog.py. It performs
NO network calls, NO transactions, NO state I/O beyond its inputs.

Static-grep clean: forbidden token patterns checked at import.
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
            f"V47G-EXIT-POLICY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47g_watchdog_exit_policy")


# Mirror V47F thresholds (kept local to avoid cross-import cycles).
BANK_TH = 0.00060
SCRATCH_TH = 0.00005
LOSS_TH = -0.00050
SCRATCH_QUOTE_DROP_FROM_PEAK = 0.000500
NEG_GRADIENT_CONSECUTIVE_FOR_SCRATCH = 2

# Action constants.
ACTION_BANK = "bank"
ACTION_SCRATCH = "scratch"
ACTION_ABORT_SCRATCH = "abort_scratch"
ACTION_ABORT_FLAT = "abort_flat"
ACTION_ABORT_EMERGENCY = "abort_emergency"
ACTION_MAX_HOLD_BANK = "max_hold_bank"
ACTION_MAX_HOLD_SCRATCH = "max_hold_scratch"
ACTION_MAX_HOLD_NEUTRAL = "max_hold_neutral"
ACTION_MAX_HOLD_EXPIRED_LOSS = "max_hold_expired_loss"
ACTION_MAX_HOLD_CLAMP_LOSS = "max_hold_clamp_loss"
ACTION_HOLD = "hold"

# Non-negative-close action set (callers may use this to drive accounting).
NON_NEG_ACTIONS = {
    ACTION_BANK, ACTION_SCRATCH,
    ACTION_ABORT_SCRATCH, ACTION_ABORT_FLAT,
    ACTION_MAX_HOLD_BANK, ACTION_MAX_HOLD_SCRATCH, ACTION_MAX_HOLD_NEUTRAL,
}
NEG_ACTIONS = {
    ACTION_ABORT_EMERGENCY,
    ACTION_MAX_HOLD_EXPIRED_LOSS, ACTION_MAX_HOLD_CLAMP_LOSS,
}


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def _safe_midhold_abort(
    pos_state: Dict[str, Any],
    logger: Optional[Callable[[str], None]],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Lazy-import V47F midhold abort. Returns (fired, action, reason)."""
    try:
        from pgg2_v47f_midhold_dump_abort import should_abort_midhold  # type: ignore
    except Exception:
        return (False, None, None)
    try:
        return should_abort_midhold(pos_state, {}, logger=logger)
    except Exception as exc:
        if logger is not None:
            try:
                logger(
                    "PGG2-V47G-EXIT-POLICY midhold_abort_err="
                    f"{type(exc).__name__}:{exc}"
                )
            except Exception:
                pass
        return (False, None, None)


def _safe_hold_caps(size_sol: float) -> Dict[str, Any]:
    """Lazy-import V47F hold caps. Returns
    {max_hold_ms, max_extend_ms, extend_allowed, extend_conditions}.

    On failure: returns the most conservative tier (size>0.020 default).
    """
    try:
        from pgg2_v47f_hold_caps import get_hold_caps  # type: ignore
        return get_hold_caps(float(size_sol))
    except Exception:
        return {
            "max_hold_ms": 1000,
            "max_extend_ms": 0,
            "extend_allowed": False,
            "extend_conditions": None,
        }


def decide_exit(
    pos_state: Dict[str, Any],
    logger: Optional[Callable[[str], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Decide action for the watchdog given position state.

    Required pos_state keys:
      mint                                str
      size_sol                            float
      current_all_in_pnl                  float
      peak_all_in_pnl                     float
      current_local_sell_quote            float
      peak_local_sell_quote               float
      current_curve_vsol                  float
      peak_curve_vsol                     float
      latest_pending_buy_sol_250ms        float
      latest_pending_sell_sol_250ms       float
      latest_quote_gradient               float
      consecutive_negative_gradient_count int
      had_been_above_bank_or_scratch      bool
      break_even_sell_quote               float
      position_age_ms                     int

    Returns (action, reason). Action is one of the ACTION_* constants.
    """
    mint = str(pos_state.get("mint", ""))
    size_sol = float(pos_state.get("size_sol", 0.0))
    pnl = float(pos_state.get("current_all_in_pnl", 0.0))
    cur_q = float(pos_state.get("current_local_sell_quote", 0.0))
    peak_q = float(pos_state.get("peak_local_sell_quote", 0.0))
    neg_grad = int(pos_state.get("consecutive_negative_gradient_count", 0))
    age_ms = int(pos_state.get("position_age_ms", 0))

    # Priority 1: BANK
    if pnl >= BANK_TH - 1e-12:
        reason = (
            f"pnl_{int(round(pnl*1e6))}u_geq_bank_"
            f"{int(round(BANK_TH*1e6))}u"
        )
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47G-WATCHDOG-EXIT-DECISION mint={_short(mint)} "
                    f"pnl={pnl:+.6f} action={ACTION_BANK} reason={reason}"
                )
            except Exception:
                pass
        return (ACTION_BANK, reason)

    # Priority 2: SCRATCH (positive small AND quote deteriorating)
    quote_drop = peak_q - cur_q
    quote_deteriorating = (
        peak_q > 0.0
        and (
            quote_drop >= SCRATCH_QUOTE_DROP_FROM_PEAK - 1e-12
            or neg_grad >= NEG_GRADIENT_CONSECUTIVE_FOR_SCRATCH
        )
    )
    if pnl >= SCRATCH_TH - 1e-12 and quote_deteriorating:
        reason = (
            f"pnl_{int(round(pnl*1e6))}u_scratch_drop_"
            f"{int(round(quote_drop*1e6))}u_neg_grad={neg_grad}"
        )
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47G-WATCHDOG-EXIT-DECISION mint={_short(mint)} "
                    f"pnl={pnl:+.6f} action={ACTION_SCRATCH} "
                    f"reason={reason}"
                )
            except Exception:
                pass
        return (ACTION_SCRATCH, reason)

    # Priority 3: V47F mid-hold abort
    fired, ab_action, ab_reason = _safe_midhold_abort(pos_state, logger)
    if fired:
        # Translate V47F action -> V47G action with explicit category.
        if ab_action == "scratch_exit_immediately":
            v47g_action = ACTION_ABORT_SCRATCH
        elif ab_action == "close_flat_immediately":
            v47g_action = ACTION_ABORT_FLAT
        else:
            # emergency_exit_negative OR unknown -> classify by pnl sign.
            if pnl >= SCRATCH_TH - 1e-12:
                v47g_action = ACTION_ABORT_SCRATCH
            elif pnl >= 0.0 - 1e-12:
                v47g_action = ACTION_ABORT_FLAT
            else:
                v47g_action = ACTION_ABORT_EMERGENCY
        reason = f"v47f_midhold|{ab_reason}"
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47G-WATCHDOG-EXIT-DECISION mint={_short(mint)} "
                    f"pnl={pnl:+.6f} action={v47g_action} reason={reason}"
                )
            except Exception:
                pass
        return (v47g_action, reason)

    # Priority 4: max_hold by size tier (no extend in watchdog priority chain).
    caps = _safe_hold_caps(float(size_sol))
    base_hold = int(caps.get("max_hold_ms", 1000))
    max_hold_ms = int(base_hold)
    if age_ms >= max_hold_ms:
        if pnl >= BANK_TH - 1e-12:
            v47g_action = ACTION_MAX_HOLD_BANK
        elif pnl >= SCRATCH_TH - 1e-12:
            v47g_action = ACTION_MAX_HOLD_SCRATCH
        elif pnl >= 0.0 - 1e-12:
            v47g_action = ACTION_MAX_HOLD_NEUTRAL
        elif pnl > LOSS_TH:
            v47g_action = ACTION_MAX_HOLD_EXPIRED_LOSS
        else:
            v47g_action = ACTION_MAX_HOLD_CLAMP_LOSS
        reason = f"age_{age_ms}ms_geq_max_hold_{max_hold_ms}ms"
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47G-WATCHDOG-EXIT-DECISION mint={_short(mint)} "
                    f"pnl={pnl:+.6f} action={v47g_action} reason={reason}"
                )
            except Exception:
                pass
        return (v47g_action, reason)

    # Priority 5: HOLD
    return (ACTION_HOLD, None)


def is_negative_close_action(action: str) -> bool:
    return action in NEG_ACTIONS


def close_kind_from_action(action: str) -> str:
    """Map V47G action to canonical close_kind label used by V47F drylive."""
    if action in (ACTION_BANK, ACTION_MAX_HOLD_BANK):
        return "bank"
    if action in (
        ACTION_SCRATCH, ACTION_ABORT_SCRATCH, ACTION_MAX_HOLD_SCRATCH,
        ACTION_ABORT_FLAT, ACTION_MAX_HOLD_NEUTRAL,
    ):
        return "scratch"
    if action in (ACTION_ABORT_EMERGENCY, ACTION_MAX_HOLD_EXPIRED_LOSS):
        return "expired_loss"
    if action == ACTION_MAX_HOLD_CLAMP_LOSS:
        return "clamp_loss"
    return "hold"


__all__ = [
    "decide_exit",
    "is_negative_close_action",
    "close_kind_from_action",
    "BANK_TH", "SCRATCH_TH", "LOSS_TH",
    "SCRATCH_QUOTE_DROP_FROM_PEAK", "NEG_GRADIENT_CONSECUTIVE_FOR_SCRATCH",
    "ACTION_BANK", "ACTION_SCRATCH",
    "ACTION_ABORT_SCRATCH", "ACTION_ABORT_FLAT", "ACTION_ABORT_EMERGENCY",
    "ACTION_MAX_HOLD_BANK", "ACTION_MAX_HOLD_SCRATCH",
    "ACTION_MAX_HOLD_NEUTRAL", "ACTION_MAX_HOLD_EXPIRED_LOSS",
    "ACTION_MAX_HOLD_CLAMP_LOSS", "ACTION_HOLD",
    "NON_NEG_ACTIONS", "NEG_ACTIONS",
]


# ---- self-tests at import (idempotent, fast) ----
def _self_test() -> None:
    base = {
        "mint": "TestSelfPolicyM1nt",
        "size_sol": 0.005,
        "current_all_in_pnl": 0.0,
        "peak_all_in_pnl": 0.0,
        "current_local_sell_quote": 0.005 + 0.0000574,
        "peak_local_sell_quote": 0.005 + 0.0000574,
        "current_curve_vsol": 30.0,
        "peak_curve_vsol": 30.0,
        "latest_pending_buy_sol_250ms": 0.0,
        "latest_pending_sell_sol_250ms": 0.0,
        "latest_quote_gradient": 0.0,
        "consecutive_negative_gradient_count": 0,
        "had_been_above_bank_or_scratch": False,
        "break_even_sell_quote": 0.005 + 0.0000574,
        "position_age_ms": 50,
    }
    a, _ = decide_exit(base)
    if a != ACTION_HOLD:
        raise RuntimeError(f"v47g_policy_self_test_flat_should_hold: {a}")
    bk = dict(base)
    bk["current_all_in_pnl"] = 0.001
    a, _ = decide_exit(bk)
    if a != ACTION_BANK:
        raise RuntimeError(f"v47g_policy_self_test_bank_should_fire: {a}")
    sc = dict(base)
    sc["current_all_in_pnl"] = 0.0001
    sc["peak_local_sell_quote"] = sc["current_local_sell_quote"] + 0.000600
    sc["consecutive_negative_gradient_count"] = 0
    a, _ = decide_exit(sc)
    if a != ACTION_SCRATCH:
        raise RuntimeError(f"v47g_policy_self_test_scratch_drop_should_fire: {a}")
    # Max hold expired_loss (small negative)
    mh = dict(base)
    mh["current_all_in_pnl"] = -0.0001
    mh["position_age_ms"] = 5000
    a, _ = decide_exit(mh)
    if a != ACTION_MAX_HOLD_EXPIRED_LOSS:
        raise RuntimeError(f"v47g_policy_self_test_max_hold_neg_small: {a}")
    # Max hold clamp_loss
    mh2 = dict(base)
    mh2["current_all_in_pnl"] = -0.001
    mh2["position_age_ms"] = 5000
    a, _ = decide_exit(mh2)
    if a != ACTION_MAX_HOLD_CLAMP_LOSS:
        raise RuntimeError(f"v47g_policy_self_test_max_hold_clamp: {a}")
    if close_kind_from_action(ACTION_BANK) != "bank":
        raise RuntimeError("close_kind_bank")
    if close_kind_from_action(ACTION_ABORT_EMERGENCY) != "expired_loss":
        raise RuntimeError("close_kind_emergency")
    if close_kind_from_action(ACTION_MAX_HOLD_CLAMP_LOSS) != "clamp_loss":
        raise RuntimeError("close_kind_clamp")
    if is_negative_close_action(ACTION_BANK):
        raise RuntimeError("bank_is_not_neg")
    if not is_negative_close_action(ACTION_ABORT_EMERGENCY):
        raise RuntimeError("emergency_is_neg")


_self_test()
