"""V47C - Post-Buy Health Check (circuit breaker for dry-live + live).

After a buy lands, compute the immediate round-trip PnL at the latest curve
state for the actual tokens we received. Compare to the V47B predicted PnL
to detect divergence. Emit an action recommendation:

  - actual_post_buy_pnl >= +0.00060 SOL  -> "bank_now"
  - actual_post_buy_pnl >= +0.00005 SOL  -> "hold_with_scratch_armed"
  - actual_post_buy_pnl <= -0.00050 SOL  -> "emergency_exit"
  - drift <= -0.00030 within first 200ms -> "scratch_exit_if_possible"
  - else                                  -> "hold"

This is NOT used in no-send mode (no buy occurs). It is a contract surface
that dry-live and Stage-A live will call after a real buy lands to decide
whether to immediately exit, hold, or arm a tighter scratch.

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
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
            f"V47C-POST-BUY-HEALTH-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47c_post_buy_health")


BANK_TH_SOL = 0.00060
SCRATCH_ARM_TH_SOL = 0.00005
EMERGENCY_TH_SOL = -0.00050
DRIFT_EARLY_TH_SOL = -0.00030
DRIFT_EARLY_MS = 200


def post_buy_health_check(
    predicted_pnl: float,
    actual_tokens_received: int,
    latest_curve_state: Any,
    buy_size_sol: float,
    tx_fee_sol: float,
    elapsed_ms_since_buy: int,
    mint_for_log: str = "",
    logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Compute post-buy health-check result.

    Args:
      predicted_pnl: V47B expected_pnl in SOL (post-fees).
      actual_tokens_received: tokens we received from the buy
        (caller computes from on-chain or shred read).
      latest_curve_state: latest curve state object (v42h).
      buy_size_sol: the buy size we just executed in SOL.
      tx_fee_sol: per-transaction signature fee in SOL (one-way).
      elapsed_ms_since_buy: wall-clock since buy landed.

    Returns:
      {
        "predicted_pnl": float,
        "actual_post_buy_pnl": float,
        "drift": float,
        "action": str,
        "elapsed_ms_since_buy": int,
      }

    The function imports the local curve quote helper lazily to avoid hard
    dependencies when this module is imported in contexts that don't have
    sys.path containing /root/piggy.
    """
    sys.path.insert(0, "/root/piggy")
    try:
        from pgg2_v42h_local_curve_quote import (  # type: ignore
            LAMPORTS_PER_SOL,
            local_sell_quote_sol,
        )
    except Exception as exc:
        # If the helper isn't available we cannot reason; return "hold".
        out = {
            "predicted_pnl": float(predicted_pnl),
            "actual_post_buy_pnl": float("nan"),
            "drift": float("nan"),
            "action": "hold",
            "elapsed_ms_since_buy": int(elapsed_ms_since_buy),
            "error": f"local_curve_quote_unavailable:{type(exc).__name__}",
        }
        return out

    if actual_tokens_received <= 0 or latest_curve_state is None:
        out = {
            "predicted_pnl": float(predicted_pnl),
            "actual_post_buy_pnl": float("nan"),
            "drift": float("nan"),
            "action": "emergency_exit",
            "elapsed_ms_since_buy": int(elapsed_ms_since_buy),
            "error": "zero_tokens_or_no_curve_state",
        }
        if logger is not None:
            try:
                short = (
                    (mint_for_log[:4] + ".." + mint_for_log[-4:])
                    if mint_for_log and len(mint_for_log) > 10
                    else (mint_for_log or "-")
                )
                logger(
                    f"PGG2-V47C-POST-BUY-HEALTH mint={short} "
                    f"pred={float(predicted_pnl):+.6f} "
                    f"actual=NaN drift=NaN "
                    f"action=emergency_exit reason=zero_tokens_or_no_curve_state"
                )
            except Exception:
                pass
        return out

    sell_lams, _ = local_sell_quote_sol(
        latest_curve_state, int(actual_tokens_received),
    )
    sell_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)
    actual_post_buy_pnl = (
        sell_sol - float(buy_size_sol) - 2.0 * float(tx_fee_sol)
    )
    drift = float(actual_post_buy_pnl) - float(predicted_pnl)

    if actual_post_buy_pnl >= BANK_TH_SOL:
        action = "bank_now"
    elif actual_post_buy_pnl >= SCRATCH_ARM_TH_SOL:
        action = "hold_with_scratch_armed"
    elif actual_post_buy_pnl <= EMERGENCY_TH_SOL:
        action = "emergency_exit"
    elif (
        drift <= DRIFT_EARLY_TH_SOL
        and int(elapsed_ms_since_buy) <= DRIFT_EARLY_MS
    ):
        action = "scratch_exit_if_possible"
    else:
        action = "hold"

    out = {
        "predicted_pnl": float(predicted_pnl),
        "actual_post_buy_pnl": float(actual_post_buy_pnl),
        "drift": float(drift),
        "action": str(action),
        "elapsed_ms_since_buy": int(elapsed_ms_since_buy),
    }

    if logger is not None:
        try:
            short = (
                (mint_for_log[:4] + ".." + mint_for_log[-4:])
                if mint_for_log and len(mint_for_log) > 10
                else (mint_for_log or "-")
            )
            logger(
                f"PGG2-V47C-POST-BUY-HEALTH mint={short} "
                f"pred={float(predicted_pnl):+.6f} "
                f"actual={float(actual_post_buy_pnl):+.6f} "
                f"drift={float(drift):+.6f} "
                f"action={action} elapsed_ms={int(elapsed_ms_since_buy)}"
            )
        except Exception:
            pass

    return out


__all__ = [
    "post_buy_health_check",
    "BANK_TH_SOL",
    "SCRATCH_ARM_TH_SOL",
    "EMERGENCY_TH_SOL",
    "DRIFT_EARLY_TH_SOL",
    "DRIFT_EARLY_MS",
]
