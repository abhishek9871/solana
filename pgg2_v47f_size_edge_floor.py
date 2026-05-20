"""V47F - Size-tiered Expected-PnL Floor.

Addresses the FScZ dry-live loss:
  FScZ..pump size=0.030, ub_250=5, tbs_250=0.343,
  expected_pnl=+0.001024, observed -0.011972 at lag=4108ms.

V47E admitted FScZ because expected_pnl > +0.000600 (its single floor).
V47F enforces a size-tiered floor:

  size <= 0.010     -> exp_pnl >= +0.000600
  0.010 < size <= 0.020 -> exp_pnl >= +0.001000
  0.020 < size <= 0.030 -> exp_pnl >= +0.002000
  0.030 < size <= 0.050 -> exp_pnl >= +0.003000
  size > 0.050      -> exp_pnl >= min(size_sol * 0.06, +0.003000)

Additional rule for size >= 0.030:
  (expected_pnl / size_sol) >= 0.06 (= 600 bps)
    OR exp_pnl >= +0.003000

Both rules must pass for size >= 0.030. The smaller tiers (<=0.030) check only
the floor (since ratio criterion is auto-satisfied by their own floor at those
small sizes).

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Callable, Optional, Tuple


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
            f"V47F-SIZE-EDGE-FLOOR-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47f_size_edge_floor")


# Floors per tier (SOL of expected_pnl).
TIER_FLOORS = (
    (0.010, 0.000600, "tier_le010"),
    (0.020, 0.001000, "tier_010_020"),
    (0.030, 0.002000, "tier_020_030"),
    (0.050, 0.003000, "tier_030_050"),
)
# size > 0.050: dynamic floor min(size*0.06, 0.003000)
LARGE_SIZE_THRESHOLD = 0.050
LARGE_SIZE_RATIO = 0.06
LARGE_SIZE_FLOOR_CAP = 0.003000

# Ratio rule (applies when size >= 0.030)
RATIO_RULE_SIZE_THRESHOLD = 0.030
RATIO_RULE_MIN = 0.06            # 600 bps
RATIO_RULE_BYPASS_PNL = 0.003000  # if exp_pnl >= 3000u, ratio is waived


def required_floor_for_size(size_sol: float) -> float:
    """Return the minimum exp_pnl floor for the given size."""
    s = float(size_sol)
    for upper, floor, _tag in TIER_FLOORS:
        if s <= upper + 1e-9:
            return float(floor)
    # size > 0.050
    return float(min(s * LARGE_SIZE_RATIO, LARGE_SIZE_FLOOR_CAP))


def _tier_tag(size_sol: float) -> str:
    s = float(size_sol)
    for upper, _floor, tag in TIER_FLOORS:
        if s <= upper + 1e-9:
            return tag
    return "tier_gt050"


def evaluate_size_edge_floor(
    size_sol: float,
    expected_pnl: float,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[bool, Optional[str]]:
    """Return (pass, reason).

    pass=True  -> (True, None)
    pass=False -> (False, "<reason_string>")
    """
    s = float(size_sol)
    pnl = float(expected_pnl)
    floor = required_floor_for_size(s)
    tier = _tier_tag(s)

    if pnl < floor:
        reason = f"size_{int(round(s*1000)):04d}m_exp_pnl_lt_{int(round(floor*1e6))}u"
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47F-SIZE-EDGE-FLOOR mint={_short(mint_for_log)} "
                    f"size={s:.4f} exp_pnl={pnl:+.6f} "
                    f"required_floor={floor:+.6f} tier={tier} "
                    f"pass=0 blocker={reason}"
                )
            except Exception:
                pass
        return (False, reason)

    # Ratio rule for size >= 0.030.
    if s >= RATIO_RULE_SIZE_THRESHOLD - 1e-9:
        ratio = pnl / s if s > 0 else 0.0
        if ratio < RATIO_RULE_MIN and pnl < RATIO_RULE_BYPASS_PNL - 1e-12:
            reason = (
                f"size_{int(round(s*1000)):04d}m_ratio_"
                f"{int(round(ratio*10000)):04d}bps_lt_"
                f"{int(round(RATIO_RULE_MIN*10000))}bps_AND_pnl_lt_"
                f"{int(round(RATIO_RULE_BYPASS_PNL*1e6))}u"
            )
            if logger is not None:
                try:
                    logger(
                        f"PGG2-V47F-SIZE-EDGE-FLOOR mint={_short(mint_for_log)} "
                        f"size={s:.4f} exp_pnl={pnl:+.6f} ratio={ratio:.4f} "
                        f"required_floor={floor:+.6f} tier={tier} "
                        f"pass=0 blocker={reason}"
                    )
                except Exception:
                    pass
            return (False, reason)

    if logger is not None:
        try:
            logger(
                f"PGG2-V47F-SIZE-EDGE-FLOOR mint={_short(mint_for_log)} "
                f"size={s:.4f} exp_pnl={pnl:+.6f} "
                f"required_floor={floor:+.6f} tier={tier} "
                f"pass=1 blocker=-"
            )
        except Exception:
            pass
    return (True, None)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


__all__ = [
    "evaluate_size_edge_floor",
    "required_floor_for_size",
    "TIER_FLOORS",
    "LARGE_SIZE_THRESHOLD",
    "LARGE_SIZE_RATIO",
    "LARGE_SIZE_FLOOR_CAP",
    "RATIO_RULE_SIZE_THRESHOLD",
    "RATIO_RULE_MIN",
    "RATIO_RULE_BYPASS_PNL",
]


# ----- self-tests (executed at import) -------------------------------
def _self_test() -> None:
    # FScZ scenario: size=0.030, exp_pnl=+0.001024 -> must FAIL.
    p, r = evaluate_size_edge_floor(0.030, 0.001024)
    if p or r is None or "exp_pnl_lt" not in r:
        raise RuntimeError(f"v47f_floor_selftest_fscz_failed: {p} {r}")

    # Hjt5 scenario: size=0.050, exp_pnl=+0.001758 -> must FAIL.
    # (At size=0.050 tier floor is 0.003.)
    p, r = evaluate_size_edge_floor(0.050, 0.001758)
    if p:
        raise RuntimeError(f"v47f_floor_selftest_hjt5_should_block: {p} {r}")

    # Small winners at size=0.005 with exp_pnl >= 0.0006 -> must PASS.
    for epnl in (0.0007, 0.0010, 0.0015, 0.0020):
        p, r = evaluate_size_edge_floor(0.005, epnl)
        if not p:
            raise RuntimeError(f"v47f_floor_selftest_005_winner_blocked: {epnl} {r}")

    # Boundary: size=0.020 exp_pnl=0.001 -> PASS (= floor).
    p, r = evaluate_size_edge_floor(0.020, 0.001000)
    if not p:
        raise RuntimeError("v47f_floor_selftest_020_boundary_must_pass")

    # size=0.020 exp_pnl=0.000999 -> FAIL.
    p, r = evaluate_size_edge_floor(0.020, 0.000999)
    if p:
        raise RuntimeError("v47f_floor_selftest_020_just_below_must_fail")

    # size=0.030 exp_pnl=0.0035 (>=3000u) bypasses ratio rule -> PASS.
    p, r = evaluate_size_edge_floor(0.030, 0.0035)
    if not p:
        raise RuntimeError(f"v47f_floor_selftest_030_bypass_pnl_blocked: {p} {r}")

    # size=0.075 exp_pnl=0.003 -> PASS (cap=0.003, 0.075*0.06=0.0045 -> floor=0.003).
    p, r = evaluate_size_edge_floor(0.075, 0.003)
    if not p:
        raise RuntimeError(f"v47f_floor_selftest_075_floor_pass: {p} {r}")

    # size=0.075 exp_pnl=0.002 -> FAIL.
    p, r = evaluate_size_edge_floor(0.075, 0.002)
    if p:
        raise RuntimeError("v47f_floor_selftest_075_below_must_fail")

    # size=0.100 exp_pnl=0.0029 -> FAIL (floor=min(0.006, 0.003)=0.003).
    p, r = evaluate_size_edge_floor(0.100, 0.0029)
    if p:
        raise RuntimeError("v47f_floor_selftest_100_below_must_fail")


_self_test()
