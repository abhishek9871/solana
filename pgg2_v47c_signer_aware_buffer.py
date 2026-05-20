"""V47C - Signer-aware buffer wrapper around V46PendingFlowBuffer.

The V46 buffer already tracks per-event signer in its internal deques. This
wrapper exposes a `buyer_stats(mint, ts_ms_now, cu_ts)` method that returns:
  - unique_buyers_50/100/250/500/1000ms (count of distinct signers in window)
  - pending_buy_count_250ms, pending_buy_sol_250ms,
    pending_sell_count_250ms, pending_sell_sol_250ms,
    largest_pending_buy_sol_250ms
  - top_buyer_share_250ms (largest signer's pending_buy_sol fraction)
  - top_buyer_signer_250ms (string id; '' if unknown)

It does NOT modify the V46 buffer in-place. It only reads from its public
APIs (`pending_buys`, `pending_sells`).

PURE READ. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple


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
            f"V47C-SIGNER-BUFFER-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError(
            "forbidden_call_pattern_in_v47c_signer_aware_buffer"
        )


WINDOWS_MS = (50, 100, 250, 500, 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


class V47CSignerAwareBuffer:
    """Thin wrapper around V46PendingFlowBuffer that exposes per-window
    unique-signer counts and top-buyer share.

    Construction takes the V46 buffer instance; this wrapper holds a reference
    and computes statistics on demand without mutating buffer state.

    Causal contract: all returned features use ONLY events with
    ts <= ts_ms_now AND ts > latest_curve_update_ts_ms (i.e., not yet
    reflected in the curve subscriber). Identical to V46 buffer's
    pending_buys/pending_sells contract.
    """

    def __init__(
        self,
        v46_buffer: Any,
        logger: Optional[Callable[[str], None]] = None,
        emit_sample_denom: int = 200,
    ) -> None:
        self._buf = v46_buffer
        self._log = logger or (lambda _m: None)
        self._emit_denom = max(1, int(emit_sample_denom))
        self._emit_ctr = 0

    def ingest_pump_buy(self, mint, sol_in, signer, slot, ts_ms):
        return self._buf.ingest_pump_buy(mint, sol_in, signer, slot, ts_ms)

    def ingest_pump_sell(self, mint, tokens_in, signer, slot, ts_ms,
                          sol_out_hint=0.0):
        return self._buf.ingest_pump_sell(
            mint, tokens_in, signer, slot, ts_ms, sol_out_hint,
        )

    def mark_curve_update(self, mint, ts_ms):
        return self._buf.mark_curve_update(mint, ts_ms)

    def latest_curve_update_ts(self, mint):
        return self._buf.latest_curve_update_ts(mint)

    def get_state(self, mint, ts_ms_now, latest_curve_update_ts_ms):
        return self._buf.get_state(mint, ts_ms_now, latest_curve_update_ts_ms)

    def pending_buys(self, mint, ts_ms_now, latest_curve_update_ts_ms,
                     window_ms=250):
        return self._buf.pending_buys(
            mint, ts_ms_now, latest_curve_update_ts_ms, window_ms,
        )

    def pending_sells(self, mint, ts_ms_now, latest_curve_update_ts_ms,
                       window_ms=250):
        return self._buf.pending_sells(
            mint, ts_ms_now, latest_curve_update_ts_ms, window_ms,
        )

    def buyer_stats(
        self,
        mint: str,
        ts_ms_now: int,
        latest_curve_update_ts_ms: int,
    ) -> Dict[str, Any]:
        """Compute multi-window signer-aware buyer statistics.

        Returns a dict that augments V46PendingFlowBuffer.get_state with:
          unique_buyers_{w}ms for w in (50, 100, 250, 500, 1000)
          top_buyer_share_250ms (lpb_250 / pbs_250)
          top_buyer_signer_250ms (id of the largest pending buyer)
          per_signer_sol_250ms (dict signer -> sum sol_in)
        """
        out: Dict[str, Any] = {}

        # Pull the base snap so callers get consistent windows and net flow.
        base_snap = self._buf.get_state(
            mint, ts_ms_now, latest_curve_update_ts_ms,
        )
        out.update(base_snap)

        for w in WINDOWS_MS:
            buys_w = self._buf.pending_buys(
                mint, ts_ms_now, latest_curve_update_ts_ms, w,
            )
            unique_signers = {b[2] for b in buys_w if b[2]}
            out[f"unique_buyers_{w}ms"] = len(unique_signers)

        buys_250 = self._buf.pending_buys(
            mint, ts_ms_now, latest_curve_update_ts_ms, 250,
        )
        per_signer: Dict[str, float] = {}
        for ts_ms, sol_in, signer, slot in buys_250:
            if not signer:
                continue
            per_signer[signer] = per_signer.get(signer, 0.0) + float(sol_in)
        top_signer = ""
        top_sol = 0.0
        for sig, s in per_signer.items():
            if s > top_sol:
                top_sol = s
                top_signer = sig
        pb_sol_250 = float(out.get("pending_buy_sol_250ms", 0.0) or 0.0)
        top_share = float(top_sol / pb_sol_250) if pb_sol_250 > 0 else 0.0
        out["top_buyer_sol_250ms"] = float(top_sol)
        out["top_buyer_signer_250ms"] = str(top_signer)
        out["top_buyer_share_250ms"] = float(top_share)
        out["per_signer_sol_250ms"] = {
            k: float(v) for k, v in per_signer.items()
        }

        self._maybe_emit(mint, out)
        return out

    def _maybe_emit(self, mint: str, snap: Dict[str, Any]) -> None:
        self._emit_ctr += 1
        if self._emit_ctr % self._emit_denom != 0:
            return
        try:
            self._log(
                f"PGG2-V47C-SIGNER-AWARE-BUFFER mint={_short(mint)} "
                f"ub250={snap.get('unique_buyers_250ms', 0)} "
                f"pb250={snap.get('pending_buy_count_250ms', 0)} "
                f"pb_sol250={float(snap.get('pending_buy_sol_250ms', 0.0)):.6f} "
                f"top_share={float(snap.get('top_buyer_share_250ms', 0.0)):.3f}"
            )
        except Exception:
            pass


__all__ = ["V47CSignerAwareBuffer", "WINDOWS_MS"]
