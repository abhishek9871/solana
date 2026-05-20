"""V47F - Hard Hold Caps By Size + Conditional Extend.

Addresses the FScZ failure where the position held to 4108ms after starting
positive, then dumped to clamp_loss. V47F enforces tighter holds for larger
sizes and conditional extend rules:

  size <= 0.010      -> max_hold=2500ms, max_extend=1500ms, extend allowed (V47E defaults)
  0.010 < size <= 0.020 -> max_hold=1800ms, max_extend=1000ms, extend allowed
  size > 0.020       -> max_hold=1000ms, max_extend=0,   extend allowed=False
                          (extend re-enabled ONLY if 5 strict conditions all hold)

Extend conditions for size > 0.020:
  1. current_all_in_pnl_sol >= +0.003000
  2. latest_curve_delta >= 0
  3. latest_quote_gradient >= 0
  4. pending_buy_sol_250ms > pending_sell_sol_250ms * 3
  5. no_negative_curve_update_last_250ms == True

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
            f"V47F-HOLD-CAPS-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47f_hold_caps")


# Per-tier caps. (size_upper_inclusive, max_hold_ms, max_extend_ms, extend_allowed)
HOLD_CAP_TIERS = (
    (0.010, 2500, 1500, True),
    (0.020, 1800, 1000, True),
)
# size > 0.020 default:
LARGE_SIZE_MAX_HOLD_MS = 1000
LARGE_SIZE_MAX_EXTEND_MS = 0
LARGE_SIZE_EXTEND_DEFAULT = False

# Extend-allowed conditions for size > 0.020
EXTEND_PNL_MIN = 0.003000
EXTEND_PB_MULT_OVER_PS = 3.0


def get_hold_caps(size_sol: float) -> Dict[str, Any]:
    """Return {max_hold_ms, max_extend_ms, extend_allowed, extend_conditions}.

    For size <= 0.020 returns V47E-style caps with extend_conditions=None
    (V47E default conditions apply: pnl > 0).

    For size > 0.020 returns a 5-condition dict in extend_conditions
    describing what must hold for extend to be enabled.
    """
    s = float(size_sol)
    for upper, hold, extend, allowed in HOLD_CAP_TIERS:
        if s <= upper + 1e-9:
            return {
                "max_hold_ms": int(hold),
                "max_extend_ms": int(extend),
                "extend_allowed": bool(allowed),
                "extend_conditions": None,  # V47E default (pnl > 0)
            }
    return {
        "max_hold_ms": int(LARGE_SIZE_MAX_HOLD_MS),
        "max_extend_ms": int(LARGE_SIZE_MAX_EXTEND_MS),
        "extend_allowed": bool(LARGE_SIZE_EXTEND_DEFAULT),
        "extend_conditions": {
            "current_all_in_pnl_sol_min": EXTEND_PNL_MIN,
            "latest_curve_delta_min": 0.0,
            "latest_quote_gradient_min": 0.0,
            "pending_buy_sol_250ms_over_sell_mult": EXTEND_PB_MULT_OVER_PS,
            "no_negative_curve_update_last_250ms": True,
        },
    }


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def check_extend_allowed(
    size_sol: float,
    current_state: Dict[str, Any],
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[bool, str]:
    """Return (extend_allowed, reason) for the current state at the given size.

    current_state keys (all floats unless noted; missing -> 0/False):
      current_all_in_pnl_sol
      latest_curve_delta
      latest_quote_gradient
      pending_buy_sol_250ms
      pending_sell_sol_250ms
      no_negative_curve_update_last_250ms (bool)
    """
    s = float(size_sol)
    caps = get_hold_caps(s)

    if s <= 0.020 + 1e-9:
        # V47E default rule: extend if pnl > 0.
        pnl = float(current_state.get("current_all_in_pnl_sol", 0.0))
        if pnl > 0.0 and caps["extend_allowed"]:
            reason = "small_size_pnl_positive_extend_ok"
            ok = True
        else:
            reason = "small_size_pnl_nonpositive_no_extend"
            ok = False
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47F-HOLD-CAP mint={_short(mint_for_log)} "
                    f"size={s:.4f} extend_allowed={int(ok)} reason={reason}"
                )
            except Exception:
                pass
        return (ok, reason)

    # size > 0.020: 5 strict conditions
    pnl = float(current_state.get("current_all_in_pnl_sol", 0.0))
    curve_delta = float(current_state.get("latest_curve_delta", 0.0))
    grad = float(current_state.get("latest_quote_gradient", 0.0))
    pbs = float(current_state.get("pending_buy_sol_250ms", 0.0))
    pss = float(current_state.get("pending_sell_sol_250ms", 0.0))
    no_neg = bool(
        current_state.get("no_negative_curve_update_last_250ms", False)
    )

    if pnl < EXTEND_PNL_MIN - 1e-12:
        reason = (
            f"large_size_pnl_{int(round(pnl*1e6))}u_lt_{int(round(EXTEND_PNL_MIN*1e6))}u"
        )
        ok = False
    elif curve_delta < 0.0:
        reason = "large_size_curve_delta_negative"
        ok = False
    elif grad < 0.0:
        reason = "large_size_quote_gradient_negative"
        ok = False
    elif not (pbs > pss * EXTEND_PB_MULT_OVER_PS):
        reason = (
            f"large_size_pbs_lte_{int(EXTEND_PB_MULT_OVER_PS)}x_pss"
        )
        ok = False
    elif not no_neg:
        reason = "large_size_negative_curve_update_recent"
        ok = False
    else:
        reason = "large_size_all_5_extend_conditions_pass"
        ok = True

    if logger is not None:
        try:
            logger(
                f"PGG2-V47F-HOLD-CAP mint={_short(mint_for_log)} "
                f"size={s:.4f} max_hold_ms={caps['max_hold_ms']} "
                f"extend_allowed={int(ok)} pnl={pnl:+.6f} "
                f"curve_d={curve_delta:+.4f} grad={grad:+.4f} "
                f"pbs={pbs:.6f} pss={pss:.6f} no_neg={no_neg} "
                f"reason={reason}"
            )
        except Exception:
            pass
    return (ok, reason)


__all__ = [
    "get_hold_caps",
    "check_extend_allowed",
    "HOLD_CAP_TIERS",
    "LARGE_SIZE_MAX_HOLD_MS",
    "LARGE_SIZE_MAX_EXTEND_MS",
    "LARGE_SIZE_EXTEND_DEFAULT",
    "EXTEND_PNL_MIN",
    "EXTEND_PB_MULT_OVER_PS",
]


# ---- self-tests (executed at import) ---------------------------------
def _self_test() -> None:
    # Small size: 0.005 -> max_hold=2500, max_extend=1500, allowed=True.
    c = get_hold_caps(0.005)
    if c["max_hold_ms"] != 2500 or c["max_extend_ms"] != 1500 or not c["extend_allowed"]:
        raise RuntimeError(f"v47f_holdcaps_005_wrong: {c}")

    # 0.020 -> max_hold=1800, max_extend=1000, allowed=True.
    c = get_hold_caps(0.020)
    if c["max_hold_ms"] != 1800 or c["max_extend_ms"] != 1000 or not c["extend_allowed"]:
        raise RuntimeError(f"v47f_holdcaps_020_wrong: {c}")

    # 0.030 -> max_hold=1000, max_extend=0, allowed=False.
    c = get_hold_caps(0.030)
    if c["max_hold_ms"] != 1000 or c["max_extend_ms"] != 0 or c["extend_allowed"]:
        raise RuntimeError(f"v47f_holdcaps_030_wrong: {c}")

    # 0.050 same as 0.030.
    c = get_hold_caps(0.050)
    if c["max_hold_ms"] != 1000 or c["extend_allowed"]:
        raise RuntimeError(f"v47f_holdcaps_050_wrong: {c}")

    # check_extend_allowed: size=0.005, pnl=+0.001 -> True.
    ok, reason = check_extend_allowed(0.005, {"current_all_in_pnl_sol": 0.001})
    if not ok:
        raise RuntimeError(f"v47f_check_extend_005_pos_should_pass: {ok} {reason}")

    # size=0.005, pnl=-0.0001 -> False.
    ok, reason = check_extend_allowed(0.005, {"current_all_in_pnl_sol": -0.0001})
    if ok:
        raise RuntimeError(f"v47f_check_extend_005_neg_should_fail: {ok} {reason}")

    # size=0.030, missing fields -> False (default 0s).
    ok, reason = check_extend_allowed(0.030, {})
    if ok:
        raise RuntimeError(f"v47f_check_extend_030_default_should_fail: {ok} {reason}")

    # size=0.030, all 5 conditions pass -> True.
    state_good = {
        "current_all_in_pnl_sol": 0.0035,
        "latest_curve_delta": 0.10,
        "latest_quote_gradient": 0.0001,
        "pending_buy_sol_250ms": 0.5,
        "pending_sell_sol_250ms": 0.10,
        "no_negative_curve_update_last_250ms": True,
    }
    ok, reason = check_extend_allowed(0.030, state_good)
    if not ok:
        raise RuntimeError(
            f"v47f_check_extend_030_good_should_pass: {ok} {reason}"
        )

    # size=0.030, pnl=+0.0035 but curve_delta negative -> False.
    state_bad1 = dict(state_good)
    state_bad1["latest_curve_delta"] = -0.01
    ok, reason = check_extend_allowed(0.030, state_bad1)
    if ok:
        raise RuntimeError("v47f_check_extend_030_neg_curve_d_should_fail")

    # size=0.030, pbs/pss ratio == 3 (boundary, must be STRICT >) -> False.
    state_bad2 = dict(state_good)
    state_bad2["pending_sell_sol_250ms"] = 0.5 / 3.0  # exactly 3x
    ok, reason = check_extend_allowed(0.030, state_bad2)
    if ok:
        raise RuntimeError(
            "v47f_check_extend_030_pbs_eq_3x_pss_should_fail"
        )


_self_test()
