"""V47B - Adverse-Fail Guard.

Computes the `min_tokens_out` value for the Pump.fun buy instruction such
that:
- in the expected branch (continuation): tokens received >= guard -> buy lands
- in the adverse branch (we land last): tokens received < guard -> tx reverts
  with TooMuchSolRequired, costing only the ~5e-6 SOL signature fee

The guard is also constrained to be at most expected_tokens * max_guard_fraction
(default 0.995) to avoid being so tight that the expected branch itself
fails (rounding / impact slippage).

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Callable, Dict, Optional


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
            f"V47B-ADVERSE-GUARD-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47b_adverse_fail_guard")


def compute_guard_for_adverse_fail_or_profit(
    expected_tokens: int,
    adverse_tokens: int,
    min_tokens_for_nonnegative_exit: int,
    strategy_min_tokens: int,
    max_guard_fraction: float = 0.995,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Dict[str, Any]:
    """Compute the `min_tokens_out` guard for V47B.

    Args:
      expected_tokens: tokens we receive in the expected branch (we land first).
      adverse_tokens:  tokens we receive in the adverse branch (we land last).
                       This is LESS than expected_tokens because pending flow
                       raised the price.
      min_tokens_for_nonnegative_exit: the smallest token count such that
                       selling them yields >= our_buy_sol + fees (non-negative
                       round-trip).
      strategy_min_tokens: a rule-level floor (e.g., 0.95 * expected_tokens).
      max_guard_fraction: cap on guard as fraction of expected_tokens (default
                       0.995).

    Logic:
      candidate_guard = max(strategy_min_tokens,
                            min_tokens_for_nonnegative_exit,
                            adverse_tokens + 1)
      max_allowed_guard = int(expected_tokens * max_guard_fraction)

      if candidate_guard > max_allowed_guard:
          -> guard_too_tight: would block the expected branch too.
             We cannot distinguish; pass = False.
      else:
          final_min_tokens = candidate_guard
          pass = True

    Returns dict per spec.
    """
    expected_tokens = int(max(0, expected_tokens))
    adverse_tokens = int(max(0, adverse_tokens))
    min_nonneg = int(max(0, min_tokens_for_nonnegative_exit))
    strategy_min = int(max(0, strategy_min_tokens))

    candidate_guard = int(max(strategy_min, min_nonneg, adverse_tokens + 1))
    max_allowed_guard = int(expected_tokens * float(max_guard_fraction))

    guard_too_tight = bool(candidate_guard > max_allowed_guard)
    final_min_tokens = (
        int(candidate_guard) if not guard_too_tight else int(max_allowed_guard)
    )
    pass_ = bool(not guard_too_tight)

    guard_fraction = (
        float(candidate_guard) / float(expected_tokens)
        if expected_tokens > 0 else 0.0
    )

    out = {
        "expected_tokens": int(expected_tokens),
        "adverse_tokens": int(adverse_tokens),
        "min_tokens_for_nonnegative_exit": int(min_nonneg),
        "strategy_min_tokens": int(strategy_min),
        "max_guard_fraction": float(max_guard_fraction),
        "candidate_guard": int(candidate_guard),
        "max_allowed_guard": int(max_allowed_guard),
        "final_min_tokens": int(final_min_tokens),
        "guard_fraction": float(guard_fraction),
        "guard_too_tight": bool(guard_too_tight),
        "pass": bool(pass_),
    }

    if logger is not None:
        try:
            logger(
                f"PGG2-V47B-ADVERSE-FAIL-GUARD "
                f"mint={mint_for_log[:8] if mint_for_log else '-'} "
                f"exp_tok={out['expected_tokens']} "
                f"adv_tok={out['adverse_tokens']} "
                f"min_nonneg={out['min_tokens_for_nonnegative_exit']} "
                f"candidate={out['candidate_guard']} "
                f"max_allowed={out['max_allowed_guard']} "
                f"final={out['final_min_tokens']} "
                f"frac={out['guard_fraction']:.4f} "
                f"tight={int(out['guard_too_tight'])} "
                f"pass={int(out['pass'])}"
            )
        except Exception:
            pass

    return out


def compute_min_tokens_for_nonnegative_exit(
    size_sol: float,
    curve_state,
    tx_fee_sol: float,
    safety_buffer_sol: float = 0.0,
) -> int:
    """Compute the smallest token count such that selling at the SAME curve
    state yields gross_sol_net >= size_sol + 2*tx_fee_sol + safety_buffer.

    This is a CONSERVATIVE estimate; the actual sell will be at a different
    state. We use it as the floor: if even at the same state we can't break
    even, that's a strong indicator the trade is structurally negative.

    Returns 0 if no positive token count satisfies the predicate within a
    reasonable scan (we cap at expected sell-out, otherwise use binary
    search). For simplicity here we use a direct compute via algebra: solve
    tokens_out * V_S / (V_T + tokens_out) - fee >= target_sol_lamports.

    For Pump bonding curve with virtual reserves, the inverse is:
      need_lams_gross = (size_sol + 2*tx_fee + buf) / (1 - fee_bps/10000)
      tokens_required = need_lams_gross * V_T / (V_S - need_lams_gross)
    Returns ceil(tokens_required).
    """
    if curve_state is None:
        return 0
    LAMPORTS_PER_SOL = 1_000_000_000
    fee_bps = int(getattr(curve_state, "fee_bps", 0))
    creator_fee_bps = int(getattr(curve_state, "creator_fee_bps", 0))
    total_fee_bps = max(0, fee_bps + creator_fee_bps)
    target_sol = float(size_sol) + 2.0 * float(tx_fee_sol) + float(safety_buffer_sol)
    target_lams = int(round(target_sol * LAMPORTS_PER_SOL))
    if target_lams <= 0:
        return 0
    # gross_sol_net = gross_sol * (1 - total_fee_bps/10000)
    # => gross_sol = target_lams / (1 - total_fee_bps/10000)
    denom = max(1, 10_000 - total_fee_bps)
    gross_sol_lams_needed = (target_lams * 10_000 + denom - 1) // denom
    V_S = int(getattr(curve_state, "virtual_sol_reserves", 0))
    V_T = int(getattr(curve_state, "virtual_token_reserves", 0))
    if V_S <= gross_sol_lams_needed or V_T <= 0:
        # Can't satisfy at this curve state.
        return int(V_T)
    # tokens_required from sell formula: gross_sol = T_in * V_S / (V_T + T_in)
    # => T_in * V_S = gross_sol * (V_T + T_in)
    # => T_in * (V_S - gross_sol) = gross_sol * V_T
    # => T_in = gross_sol * V_T / (V_S - gross_sol)
    numerator = int(gross_sol_lams_needed) * int(V_T)
    denominator = max(1, int(V_S) - int(gross_sol_lams_needed))
    tokens_required = (numerator + denominator - 1) // denominator
    return int(tokens_required)


__all__ = [
    "compute_guard_for_adverse_fail_or_profit",
    "compute_min_tokens_for_nonnegative_exit",
]
