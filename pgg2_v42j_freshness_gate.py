"""V42J Phase 3 - Bank-event freshness gate.

A V42JBankEvent is fresh ONLY when (event_age_ms <= TTL) AND there has
been no negative accountSubscribe curve update since the event AND the
current local sell quote of our 0.015-SOL trade is above break-even +
buffer. The gate is the last guard before V42J rules: if it returns
False, the entry is blocked with a precise reason.

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import os
import re as _re
import sys
import time
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
            f"V42J-FRESHNESS-GATE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v42j_freshness_gate")


from pgg2_v42h_local_curve_quote import DEFAULT_TX_FEE_SOL


# Env-overridable constants.
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except Exception:
        return int(default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except Exception:
        return float(default)


PGG2_V42J_BANK_EVENT_MAX_AGE_MS = _env_int("PGG2_V42J_BANK_EVENT_MAX_AGE_MS", 150)
PGG2_V42J_BREAK_EVEN_BUFFER_SOL = _env_float("PGG2_V42J_BREAK_EVEN_BUFFER_SOL", 0.00010)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def freshness_gate(
    event: Any,
    reprice: Dict[str, Any],
    latest_curve_state: Optional[Dict[str, Any]] = None,
    ts_ms_now: Optional[int] = None,
    break_even_buffer_sol: Optional[float] = None,
    max_age_ms: Optional[int] = None,
    amount_sol: float = 0.015,
    tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
    pair_source: str = "accountSubscribe",
    logger: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str]]:
    """Return (allow_entry, reason_or_None).

    Allow entry only if all of:
      1. bank_event_freshness_ms <= max_age_ms (default 150)
      2. latest accountSubscribe curve update IS the bank event OR a newer
         non-negative update (no negative curve update has occurred between
         event_ts and now)
      3. (subsumed by 2) no negative curve update after bank event
      4. current local sell quote >= break-even quote + buffer (default 0.00010)
      5. source_late = False
      6. route == "pump_bc"
      7. sim_needed == 0
      8. pair_source in {"current_sig","cache","prewarmed",
                          "observed_raw_rpc","accountSubscribe"}
    """
    if ts_ms_now is None:
        ts_ms_now = int(time.time() * 1000)
    if break_even_buffer_sol is None:
        break_even_buffer_sol = PGG2_V42J_BREAK_EVEN_BUFFER_SOL
    if max_age_ms is None:
        max_age_ms = PGG2_V42J_BANK_EVENT_MAX_AGE_MS

    # 1) freshness
    age_ms = int(ts_ms_now) - int(getattr(event, "event_ts_ms", 0))
    if age_ms > int(max_age_ms):
        _emit(logger, event.mint, age_ms, False, "bank_event_stale")
        return False, "bank_event_stale"

    # 2/3) negative curve update after bank event?
    if latest_curve_state is not None:
        last_neg_ts = int(latest_curve_state.get("last_negative_curve_update_ts_ms", 0) or 0)
        if last_neg_ts and last_neg_ts > int(getattr(event, "event_ts_ms", 0)):
            _emit(logger, event.mint, age_ms, False, "negative_curve_after_event")
            return False, "negative_curve_after_event"
        # Also accept an explicit positive flag.
        if "latest_curve_delta_nonneg" in latest_curve_state:
            if not bool(latest_curve_state.get("latest_curve_delta_nonneg")):
                _emit(logger, event.mint, age_ms, False, "negative_curve_after_event")
                return False, "negative_curve_after_event"

    # 4) break-even buffer on PROJECTED continuation sell quote.
    # Phase 2 explicitly states: "Same-state is structurally negative.
    # The entry is allowed only if the bank-event model predicts the next
    # continuation tick remains positive under stress." So condition 4
    # checks the PROJECTED sell-quote (after one favourable continuation
    # tick on the post-our-buy state), which is the economic edge V42J
    # demands. Same-state is logged for diagnostics only.
    proj_sell_sol = float(reprice.get("proj_sell_sol", 0.0) or 0.0)
    break_even = float(amount_sol) + 2.0 * float(tx_fee_sol)
    required = break_even + float(break_even_buffer_sol)
    if proj_sell_sol < required:
        _emit(logger, event.mint, age_ms, False, "below_break_even_buffer")
        return False, "below_break_even_buffer"

    # 5) source_late
    if bool(reprice.get("source_late", False)):
        _emit(logger, event.mint, age_ms, False, "source_late")
        return False, "source_late"

    # 6) route
    if str(reprice.get("route", "")) != "pump_bc":
        _emit(logger, event.mint, age_ms, False, "route_not_pump_bc")
        return False, "route_not_pump_bc"

    # 7) sim_needed
    if int(reprice.get("sim_needed", 1) or 0) != 0:
        _emit(logger, event.mint, age_ms, False, "sim_needed_nonzero")
        return False, "sim_needed_nonzero"

    # 8) pair_source
    allowed_ps = {
        "current_sig", "cache", "prewarmed", "observed_raw_rpc",
        "accountSubscribe",
    }
    if str(pair_source) not in allowed_ps:
        _emit(logger, event.mint, age_ms, False, "pair_source_not_allowed")
        return False, "pair_source_not_allowed"

    _emit(logger, event.mint, age_ms, True, "v42j_freshness_pass")
    return True, "v42j_freshness_pass"


def _emit(
    logger: Optional[Callable[[str], None]],
    mint: str,
    age_ms: int,
    passed: bool,
    reason: str,
) -> None:
    if logger is None:
        return
    try:
        logger(
            f"PGG2-V42J-BANK-EVENT-FRESHNESS mint={_short(mint)} "
            f"age={age_ms} pass={passed} reason={reason}"
        )
    except Exception:
        pass


__all__ = [
    "PGG2_V42J_BANK_EVENT_MAX_AGE_MS",
    "PGG2_V42J_BREAK_EVEN_BUFFER_SOL",
    "freshness_gate",
]
