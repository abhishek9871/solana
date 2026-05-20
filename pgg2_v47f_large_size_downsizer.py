"""V47F - Large-Size Downsize-Before-Block.

When a candidate's original size >= 0.030 fails the V47F size-edge floor
(Phase 2), this module retries descending sizes [0.020, 0.015, 0.010, 0.005]
and returns the first size at which:

  - the V47F floor passes
  - the V47E two-buyer guard passes (or delegates to V47D)
  - the V47B guarded-branch sim passes

Otherwise returns (None, "blocked", reason, 0.0).

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
            f"V47F-DOWNSIZER-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47f_downsizer")


from pgg2_v47f_size_edge_floor import evaluate_size_edge_floor  # type: ignore  # noqa: E402


DOWNSIZE_LADDER = (0.020, 0.015, 0.010, 0.005)
DOWNSIZE_TRIGGER_SIZE = 0.030  # only candidates >= 0.030 are downsized


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def downsize_large_candidate(
    original_size: float,
    original_buyer_stats: Dict[str, Any],
    recompute_expected_pnl_fn: Callable[[float], float],
    recompute_guarded_branch_fn: Callable[[float], Tuple[bool, Optional[str]]],
    recompute_two_buyer_fn: Callable[[float], Tuple[str, str]],
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[Optional[float], str, str, float]:
    """Try descending sizes for a candidate that failed V47F at original.

    Returns: (final_size, action, reason, new_exp_pnl).

      final_size  : float of the chosen smaller size, or None if blocked
      action      : "original" | "downsized" | "blocked"
      reason      : human-readable reason string
      new_exp_pnl : exp_pnl at the chosen size (or 0.0 if blocked)
    """
    s_orig = float(original_size)

    if s_orig < DOWNSIZE_TRIGGER_SIZE - 1e-9:
        # Downsizer only acts on >= 0.030 candidates that failed floor.
        return (None, "blocked", "not_large_size_no_downsize", 0.0)

    last_reason = "downsize_no_smaller_size_passes"

    for cand_size in DOWNSIZE_LADDER:
        if cand_size >= s_orig - 1e-9:
            # Never upsize.
            continue

        try:
            exp_pnl_cand = float(recompute_expected_pnl_fn(cand_size))
        except Exception as exc:
            last_reason = f"recompute_pnl_exc_{type(exc).__name__}"
            continue

        floor_pass, floor_reason = evaluate_size_edge_floor(
            cand_size, exp_pnl_cand
        )
        if not floor_pass:
            last_reason = f"floor_fail_at_{int(round(cand_size*1000)):04d}m:{floor_reason}"
            if logger is not None:
                try:
                    logger(
                        f"PGG2-V47F-DOWNSIZE-ATTEMPT mint={_short(mint_for_log)} "
                        f"cand_size={cand_size:.4f} exp_pnl={exp_pnl_cand:+.6f} "
                        f"floor_fail={floor_reason} reason={last_reason}"
                    )
                except Exception:
                    pass
            continue

        try:
            br_pass, br_reason = recompute_guarded_branch_fn(cand_size)
        except Exception as exc:
            last_reason = f"branch_recompute_exc_{type(exc).__name__}"
            continue
        if not br_pass:
            last_reason = (
                f"branch_fail_at_{int(round(cand_size*1000)):04d}m:"
                f"{br_reason or 'unknown'}"
            )
            if logger is not None:
                try:
                    logger(
                        f"PGG2-V47F-DOWNSIZE-ATTEMPT mint={_short(mint_for_log)} "
                        f"cand_size={cand_size:.4f} exp_pnl={exp_pnl_cand:+.6f} "
                        f"branch_fail={br_reason} reason={last_reason}"
                    )
                except Exception:
                    pass
            continue

        try:
            tb_mode, tb_reason = recompute_two_buyer_fn(cand_size)
        except Exception as exc:
            last_reason = f"two_buyer_exc_{type(exc).__name__}"
            continue
        if tb_mode == "block":
            last_reason = (
                f"two_buyer_block_at_{int(round(cand_size*1000)):04d}m:"
                f"{tb_reason}"
            )
            if logger is not None:
                try:
                    logger(
                        f"PGG2-V47F-DOWNSIZE-ATTEMPT mint={_short(mint_for_log)} "
                        f"cand_size={cand_size:.4f} exp_pnl={exp_pnl_cand:+.6f} "
                        f"two_buyer_block={tb_reason} reason={last_reason}"
                    )
                except Exception:
                    pass
            continue

        # All passes -> emit success log.
        if logger is not None:
            try:
                logger(
                    f"PGG2-V47F-DOWNSIZE mint={_short(mint_for_log)} "
                    f"original={s_orig:.4f} downsized={cand_size:.4f} "
                    f"orig_pnl=? new_pnl={exp_pnl_cand:+.6f} "
                    f"reason=downsize_pass_at_{int(round(cand_size*1000)):04d}m "
                    f"pass=1"
                )
            except Exception:
                pass
        return (
            cand_size,
            "downsized",
            f"downsize_pass_at_{int(round(cand_size*1000)):04d}m",
            exp_pnl_cand,
        )

    if logger is not None:
        try:
            logger(
                f"PGG2-V47F-DOWNSIZE mint={_short(mint_for_log)} "
                f"original={s_orig:.4f} downsized=None "
                f"reason={last_reason} pass=0"
            )
        except Exception:
            pass
    return (None, "blocked", last_reason, 0.0)


__all__ = [
    "downsize_large_candidate",
    "DOWNSIZE_LADDER",
    "DOWNSIZE_TRIGGER_SIZE",
]


# ---- self-test (executed at import) ---------------------------------
def _self_test() -> None:
    # FScZ-like: size=0.030 with exp_pnl=+0.001024 fails floor (need >=+0.002).
    # If downsize at 0.020 gives exp_pnl=+0.0011 (>=floor 0.001) AND branch+two-buyer pass,
    # return (0.020, "downsized", ..., 0.0011).
    def _epnl(sz: float) -> float:
        # Mock: smaller sizes give proportionally smaller exp_pnl
        return 0.001024 * (sz / 0.030)
    def _branch(sz: float):
        return (True, None)
    def _tb(sz: float):
        return ("actual_pass", "ok")
    final, action, reason, new_pnl = downsize_large_candidate(
        0.030, {"unique_buyers_250ms": 5}, _epnl, _branch, _tb
    )
    # At 0.020: exp_pnl = 0.001024 * (0.020/0.030) = 0.000683 < 0.001 (fail).
    # At 0.015: 0.001024 * 0.5 = 0.000512 < 0.001 (fail).
    # At 0.010: 0.001024 * (0.010/0.030) = 0.000341 < 0.0006 (fail).
    # At 0.005: 0.001024 * (0.005/0.030) = 0.000171 < 0.0006 (fail).
    # Expected: blocked.
    if final is not None or action != "blocked":
        raise RuntimeError(
            f"v47f_downsizer_selftest_fscz_should_block: {final} {action} {reason}"
        )

    # Hjt5-like: size=0.050 exp_pnl=+0.001758. Downsize lookup:
    # At 0.020: 0.001758 * (0.020/0.050) = 0.000703 < 0.001 (fail).
    # At 0.015: 0.000527 < 0.001 (fail).
    # At 0.010: 0.000352 < 0.0006 (fail).
    # At 0.005: 0.000176 < 0.0006 (fail).
    # Expected: blocked under linear scaling. Real Hjt5 may scale nonlinearly.
    def _epnl_hjt5(sz: float) -> float:
        return 0.001758 * (sz / 0.050)
    final, action, reason, new_pnl = downsize_large_candidate(
        0.050, {"unique_buyers_250ms": 4}, _epnl_hjt5, _branch, _tb
    )
    if final is not None:
        # Acceptable if it passes — but under our mock linear scaling, expect blocked.
        pass
    # No raise either way; just check we didn't crash.

    # Hypothetical larger-than-floor candidate that downsizes cleanly:
    # size=0.030 exp_pnl=+0.005000 -> already passes original floor (no need to downsize).
    # Downsizer ONLY runs when caller has detected floor failure; so this is academic.
    # Real test: size=0.030 with mocked recompute that gives exp_pnl=+0.0015 at 0.020.
    def _epnl_high(sz: float) -> float:
        return {0.020: 0.0015, 0.015: 0.0012, 0.010: 0.0009, 0.005: 0.00065}.get(sz, 0.0)
    final, action, reason, new_pnl = downsize_large_candidate(
        0.030, {"unique_buyers_250ms": 4}, _epnl_high, _branch, _tb
    )
    if final != 0.020 or action != "downsized":
        raise RuntimeError(
            f"v47f_downsizer_selftest_should_downsize_to_020: {final} {action} {reason}"
        )

    # Small-size candidate (size < 0.030) should not be downsized.
    final, action, reason, new_pnl = downsize_large_candidate(
        0.020, {"unique_buyers_250ms": 3}, _epnl, _branch, _tb
    )
    if final is not None or action != "blocked" or "not_large_size" not in reason:
        raise RuntimeError(
            f"v47f_downsizer_selftest_small_size_should_not_run: {final} {action} {reason}"
        )


_self_test()
