"""V47H — Sell-aware buffer extension and curve-history tracker.

V47C's signer-aware buffer wraps V46 to expose unique_buyers / top_buyer
features. V47H wraps V47C (and indirectly V46) to additionally expose
sell-side breadth and largest-seller-share, AND to track a short curve
delta history per mint for the curve-reversal veto.

It does NOT mutate the underlying buffers; it only reads from their
public APIs (`pending_sells`, `pending_buys`) and maintains its own
small in-memory ring of recent curve updates.

PURE WRAPPER. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple


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
            f"V47H-SELL-AWARE-BUFFER-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError(
            "forbidden_call_pattern_in_v47h_sell_aware_buffer"
        )


_CURVE_HISTORY_RETENTION_MS = 1500
_CURVE_HISTORY_MAX_PTS = 64


def _short(mint: str) -> str:
    if not mint or len(mint) < 6:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class _CurvePoint:
    ts_ms: int
    vsol_lamports: int


@dataclass
class _CurveHistory:
    points: Deque[_CurvePoint] = field(
        default_factory=lambda: deque(maxlen=_CURVE_HISTORY_MAX_PTS)
    )


class V47HSellAwareBuffer:
    """Wrapper around the V47C signer-aware buffer that adds:

      - unique_sellers_50/100/250/500/1000ms
      - largest_sell_sol_250ms, largest_sell_share_250ms
      - sell_count_250ms (alias for pending_sell_count_250ms; explicit
        because veto A subclause names use sell_count)
      - curve_history(mint) → dict with vsol_deltas_last_500ms /
        vsol_deltas_last_1000ms

    The wrapper exposes the same `buyer_stats(mint, ts, cu_ts)` method
    as V47C but augments it with sell_stats. Callers may also call
    `sell_stats(mint, ts, cu_ts)` directly or `curve_history(mint, ts)`.

    All ingest methods delegate to the wrapped buffer.
    """

    def __init__(
        self,
        v47c_buffer: Any,
        logger: Optional[Callable[[str], None]] = None,
        emit_sample_denom: int = 400,
    ) -> None:
        self._buf = v47c_buffer
        self._log = logger or (lambda _m: None)
        self._emit_denom = max(1, int(emit_sample_denom))
        self._emit_ctr = 0
        self._curve_hist: Dict[str, _CurveHistory] = {}

    # ----- delegation to V47C ------------------------------------------
    def ingest_pump_buy(self, mint, sol_in, signer, slot, ts_ms):
        return self._buf.ingest_pump_buy(mint, sol_in, signer, slot, ts_ms)

    def ingest_pump_sell(self, mint, tokens_in, signer, slot, ts_ms,
                          sol_out_hint=0.0):
        return self._buf.ingest_pump_sell(
            mint, tokens_in, signer, slot, ts_ms, sol_out_hint,
        )

    def mark_curve_update(self, mint, ts_ms, vsol_lamports=None):
        """Mark curve update. If `vsol_lamports` is provided, we append
        to the curve-history ring; otherwise we still mark the V47C/V46
        buffer (this keeps signature backward compatible)."""
        ret = self._buf.mark_curve_update(mint, ts_ms)
        if mint and vsol_lamports is not None:
            ch = self._curve_hist.get(mint)
            if ch is None:
                ch = _CurveHistory()
                self._curve_hist[mint] = ch
            ch.points.append(_CurvePoint(
                ts_ms=int(ts_ms), vsol_lamports=int(vsol_lamports)
            ))
            # Prune by retention.
            cutoff = int(ts_ms) - _CURVE_HISTORY_RETENTION_MS
            while ch.points and ch.points[0].ts_ms < cutoff:
                ch.points.popleft()
        return ret

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

    # ----- combined buyer+sell stats -----------------------------------
    def buyer_stats(self, mint, ts_ms_now, latest_curve_update_ts_ms):
        """Pass-through to V47C buyer_stats. The veto chain only needs
        the buyer-side aggregates from here; sell-side comes from
        `sell_stats()` below."""
        return self._buf.buyer_stats(
            mint, ts_ms_now, latest_curve_update_ts_ms,
        )

    def sell_stats(
        self,
        mint: str,
        ts_ms_now: int,
        latest_curve_update_ts_ms: int,
    ) -> Dict[str, Any]:
        """Compute per-window sell-side aggregates. Returns dict with:

          - pending_sell_count_{w}ms / pending_sell_sol_{w}ms (from V46
            base — pending_sell_sol is 0 because shred carries tokens,
            not sol; included for completeness)
          - sell_count_{w}ms (alias for pending_sell_count_{w}ms)
          - unique_sellers_{w}ms (count distinct signers in window)
          - largest_sell_tokens_{w}ms (max token amount)
          - largest_sell_sol_250ms (always 0.0 without sol hint)
          - largest_sell_share_250ms (always 0.0 unless sol hints exist)

        These are causal: only events with ts <= ts_ms_now are used.
        """
        out: Dict[str, Any] = {}
        windows = (50, 100, 250, 500, 1000)
        for w in windows:
            sells_w = self._buf.pending_sells(
                mint, ts_ms_now, latest_curve_update_ts_ms, w,
            )
            unique_signers = {s[2] for s in sells_w if s[2]}
            out[f"pending_sell_count_{w}ms"] = len(sells_w)
            out[f"sell_count_{w}ms"] = len(sells_w)
            out[f"unique_sellers_{w}ms"] = len(unique_signers)
            # token-based aggregates only — sol hints not populated.
            largest_tokens = max(
                (int(s[1]) for s in sells_w), default=0
            )
            out[f"largest_sell_tokens_{w}ms"] = int(largest_tokens)

        # 250ms-specific sol-side largest (always 0 with current shred).
        out["pending_sell_sol_250ms"] = 0.0
        out["largest_sell_sol_250ms"] = 0.0
        out["largest_sell_share_250ms"] = 0.0

        self._maybe_emit(mint, out)
        return out

    def curve_history(
        self,
        mint: str,
        ts_ms_now: int,
    ) -> Dict[str, Any]:
        """Return short causal curve-history snapshot.

        Output:
          - vsol_deltas_last_500ms : list[float] SOL deltas (oldest→newest)
          - vsol_deltas_last_1000ms : list[float]
          - peak_pos_delta_idx_500ms : int (-1 if no positive delta)
        """
        out: Dict[str, Any] = {
            "vsol_deltas_last_500ms": [],
            "vsol_deltas_last_1000ms": [],
            "peak_pos_delta_idx_500ms": -1,
        }
        ch = self._curve_hist.get(mint)
        if ch is None or not ch.points:
            return out
        # Sort by ts_ms ascending (deque is mostly sorted but mark order
        # can vary slightly).
        pts = [
            p for p in ch.points
            if int(p.ts_ms) <= int(ts_ms_now)
        ]
        if len(pts) < 2:
            return out
        pts.sort(key=lambda x: x.ts_ms)
        deltas_500: List[float] = []
        deltas_1000: List[float] = []
        prev = pts[0]
        for p in pts[1:]:
            delta_lams = int(p.vsol_lamports) - int(prev.vsol_lamports)
            delta_sol = float(delta_lams) / 1_000_000_000.0
            age_ms = int(ts_ms_now) - int(p.ts_ms)
            if age_ms <= 1000:
                deltas_1000.append(delta_sol)
            if age_ms <= 500:
                deltas_500.append(delta_sol)
            prev = p

        out["vsol_deltas_last_500ms"] = deltas_500
        out["vsol_deltas_last_1000ms"] = deltas_1000
        peak_idx = -1
        peak_v = -1e18
        for i, d in enumerate(deltas_500):
            if d > peak_v:
                peak_v = d
                peak_idx = i
        if peak_v > 0:
            out["peak_pos_delta_idx_500ms"] = int(peak_idx)
        return out

    def _maybe_emit(self, mint: str, snap: Dict[str, Any]) -> None:
        self._emit_ctr += 1
        if self._emit_ctr % self._emit_denom != 0:
            return
        try:
            self._log(
                f"PGG2-V47H-SELL-AWARE-BUFFER mint={_short(mint)} "
                f"sells250={snap.get('sell_count_250ms', 0)} "
                f"us250={snap.get('unique_sellers_250ms', 0)} "
                f"largest_tok250={snap.get('largest_sell_tokens_250ms', 0)}"
            )
        except Exception:
            pass


__all__ = ["V47HSellAwareBuffer"]
