"""V51 - Sell-quote wrapper for V47G watchdog-owned exits.

Phase 1A of V51. Wraps DirectPumpQuoteBroker.build_sell behind a single
entry point so the V47G watchdog-driven sell path always has a reliable
'unsigned tx' shape: callers do `quote = build_live_sell_quote_fast(broker,
mint, raw)`, then optionally `broker.retarget_sell_min_sol(quote, mint,
min_sol_out)` to relax the SOL floor before sign+send.

NO sendTransaction here -- pure quote build only. Static-grep clean.
"""
from __future__ import annotations

import os
import re as _re_self
import sys
import time
from typing import Any, Dict, Optional

# Static-grep self check -- forbidden send patterns must NOT appear.
_FORBIDDEN = (
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
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _src):
        sys.stderr.write(
            f"V51-SELL-QUOTE-WRAPPER-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def build_live_sell_quote_fast(
    broker: Any,
    mint: str,
    token_amount_raw: int,
    *,
    route: str = "pump_bc",
    slippage: float = 0.50,
) -> Dict[str, Any]:
    """Build a sell quote for the V47G watchdog-driven exit path.

    Returns the broker's quote dict (`{"txn": <b64>, "rate": {...}, ...}`)
    on success. Wraps broker.build_sell(mint, "raw:<int>", slippage_pct).

    Note: `slippage` is in PERCENT (matches broker convention; build_sell
    divides by 100 internally). For an emergency / max-hold exit, the
    caller should chain `broker.retarget_sell_min_sol(quote, mint,
    min_sol_out=<absolute SOL>)` to override the initial min.

    On any exception the function logs `PGG2-V51-WATCHDOG-SELL-QUOTE-FAIL`
    and re-raises -- caller decides whether to retry / fall back.
    """
    if int(token_amount_raw) <= 0:
        raise ValueError(
            f"v51_sell_quote_fast tokens_raw must be > 0 (got {token_amount_raw})"
        )
    if not hasattr(broker, "build_sell"):
        raise RuntimeError(
            f"v51_sell_quote_fast broker_missing_build_sell class="
            f"{type(broker).__name__}"
        )
    _log(
        f"PGG2-V51-WATCHDOG-SELL-QUOTE-BUILD mint={_short(mint)} "
        f"tokens_raw={int(token_amount_raw)} slippage={float(slippage):.4f} "
        f"route={route}"
    )
    try:
        quote = broker.build_sell(
            str(mint), f"raw:{int(token_amount_raw)}", float(slippage),
        )
    except Exception as exc:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-QUOTE-FAIL mint={_short(mint)} "
            f"err={type(exc).__name__}:{exc}"
        )
        raise
    # Sanity: quote must have a txn payload.
    if not isinstance(quote, dict) or not quote.get("txn"):
        _log(
            f"PGG2-V51-WATCHDOG-SELL-QUOTE-FAIL mint={_short(mint)} "
            f"err=quote_missing_txn"
        )
        raise RuntimeError("v51_sell_quote_returned_no_txn")
    # Resolve expected SOL out for telemetry.
    expected_sol_out = 0.0
    try:
        rate = dict(quote.get("rate") or {})
        expected_sol_out = float(
            rate.get("amountOut") or rate.get("amount_out") or 0.0
        )
    except Exception:
        expected_sol_out = 0.0
    unsigned_b64_len = 0
    try:
        unsigned_b64_len = len(str(quote.get("txn") or ""))
    except Exception:
        pass
    _log(
        f"PGG2-V51-WATCHDOG-SELL-QUOTE-OK mint={_short(mint)} "
        f"unsigned_b64_len={unsigned_b64_len} "
        f"expected_sol_out={expected_sol_out:.9f}"
    )
    return quote


def compute_emergency_min_sol_out(
    *,
    current_quote_sol: float,
    floor_sol: float = 0.0005,
    ratio: float = 0.40,
) -> float:
    """Pick a safe emergency minimum SOL output for a watchdog-driven sell.

    Policy:
      - target = current_quote_sol * ratio   (accept 60% slippage if ratio=0.40)
      - if target < floor_sol: target = floor_sol
      - if current_quote_sol < floor_sol: accept a tiny positive (1 lamport equiv)
        so the sell can land at all. Floor here is the broker's
        PGG2_V39_SELL_MIN_SOL_FLOOR_LAMPORTS (100 lamports default).
    """
    q = max(0.0, float(current_quote_sol))
    if q <= float(floor_sol):
        # Quote below floor — try to clear at any sane positive value.
        return max(0.0000001, float(floor_sol))
    target = q * float(ratio)
    if target < float(floor_sol):
        target = float(floor_sol)
    return float(target)


__all__ = [
    "build_live_sell_quote_fast",
    "compute_emergency_min_sol_out",
]
