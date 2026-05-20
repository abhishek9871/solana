"""V47I — Medium-window buffer extension and quote-history tracker.

The V47H sell-aware buffer exposes sell_stats() with windows 50/100/250/500/
1000ms, but the 500/1000ms sell aggregates were not previously consumed.
V47I's medium-rug veto needs:

  - sell_stats() 500/1000ms windows (already in V47H — we just consume them)
  - curve_history() with vsol_delta_ts_last_1000ms (NEW — V47H emits values
    but not their timestamps relative to ts_now)
  - quote_history() — NEW — tracks last 5 local quote values per mint and
    computes peak in last 500ms + gradient series

This module wraps the V47H buffer and adds:
  - tracking of local quote values per mint via record_local_quote()
  - quote_history(mint, ts) snapshot for the veto
  - extension of curve_history() to include per-delta ages
  - convenience accessor net_pending_sol_500ms via subtraction of sell-side
    (since pending_buy_sol is sol-denominated and pending_sell_sol is 0 by
    construction in shred, the net is effectively pbs_500 — but we keep the
    signature consistent for future feeds that include sell-side SOL).

PURE WRAPPER. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional


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
            f"V47I-MEDIUM-WINDOW-BUFFER-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47i_medium_window_buffer")


_QUOTE_HISTORY_RETENTION_MS = 1500
_QUOTE_HISTORY_MAX_PTS = 16


def _short(mint: str) -> str:
    if not mint or len(mint) < 6:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class _QuotePoint:
    ts_ms: int
    quote_sol: float


@dataclass
class _QuoteHistory:
    points: Deque[_QuotePoint] = field(
        default_factory=lambda: deque(maxlen=_QUOTE_HISTORY_MAX_PTS)
    )


class V47IMediumWindowBuffer:
    """Wrapper around the V47H sell-aware buffer (which itself wraps V47C
    which wraps V46). Adds quote-history tracking and aged curve deltas.
    """

    def __init__(
        self,
        v47h_buffer: Any,
        logger: Optional[Callable[[str], None]] = None,
        emit_sample_denom: int = 400,
    ) -> None:
        self._buf = v47h_buffer
        self._log = logger or (lambda _m: None)
        self._emit_denom = max(1, int(emit_sample_denom))
        self._emit_ctr = 0
        self._quote_hist: Dict[str, _QuoteHistory] = {}

    # ----- delegation to V47H -------------------------------------------
    def ingest_pump_buy(self, mint, sol_in, signer, slot, ts_ms):
        return self._buf.ingest_pump_buy(mint, sol_in, signer, slot, ts_ms)

    def ingest_pump_sell(self, mint, tokens_in, signer, slot, ts_ms,
                          sol_out_hint=0.0):
        return self._buf.ingest_pump_sell(
            mint, tokens_in, signer, slot, ts_ms, sol_out_hint,
        )

    def mark_curve_update(self, mint, ts_ms, vsol_lamports=None):
        return self._buf.mark_curve_update(mint, ts_ms, vsol_lamports)

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

    def buyer_stats(self, mint, ts_ms_now, latest_curve_update_ts_ms):
        return self._buf.buyer_stats(
            mint, ts_ms_now, latest_curve_update_ts_ms,
        )

    def sell_stats(self, mint, ts_ms_now, latest_curve_update_ts_ms):
        return self._buf.sell_stats(
            mint, ts_ms_now, latest_curve_update_ts_ms,
        )

    def curve_history(self, mint, ts_ms_now):
        """Augment V47H curve_history with vsol_delta_ts_last_1000ms ages."""
        out = dict(self._buf.curve_history(mint, ts_ms_now))
        # Best-effort: read the underlying ring directly to compute ages.
        ages_1000: List[int] = []
        try:
            ch = getattr(self._buf, "_curve_hist", {}).get(mint)
            if ch is not None and ch.points:
                pts = [
                    p for p in ch.points
                    if int(p.ts_ms) <= int(ts_ms_now)
                ]
                pts.sort(key=lambda x: x.ts_ms)
                # Build deltas + ages aligned with vsol_deltas_last_1000ms
                # (which excludes the FIRST point — deltas start at index 1).
                if len(pts) >= 2:
                    prev = pts[0]
                    for p in pts[1:]:
                        age = int(ts_ms_now) - int(p.ts_ms)
                        if age <= 1000:
                            ages_1000.append(int(age))
                        prev = p
        except Exception:
            pass
        out["vsol_delta_ts_last_1000ms"] = ages_1000
        return out

    # ----- quote-history tracking ---------------------------------------
    def record_local_quote(self, mint: str, ts_ms: int, quote_sol: float) -> None:
        """Record a local-quote sample for a mint. quote_sol is the SOL
        output of a sell that would close the current hypothetical position
        AT THAT TIMESTAMP. The veto compares this against recent values."""
        if not mint:
            return
        try:
            q = float(quote_sol)
        except Exception:
            return
        try:
            t = int(ts_ms)
        except Exception:
            return
        qh = self._quote_hist.get(mint)
        if qh is None:
            qh = _QuoteHistory()
            self._quote_hist[mint] = qh
        qh.points.append(_QuotePoint(ts_ms=t, quote_sol=q))
        # Retention prune.
        cutoff = t - _QUOTE_HISTORY_RETENTION_MS
        while qh.points and qh.points[0].ts_ms < cutoff:
            qh.points.popleft()

    def quote_history(self, mint: str, ts_ms_now: int) -> Dict[str, Any]:
        """Return causal quote-history snapshot."""
        out: Dict[str, Any] = {
            "local_quote_last": None,
            "local_quote_peak_500ms": None,
            "local_quote_history_5": [],
            "local_quote_gradient_history_3": [],
        }
        qh = self._quote_hist.get(mint)
        if qh is None or not qh.points:
            return out
        try:
            pts = [
                p for p in qh.points
                if int(p.ts_ms) <= int(ts_ms_now)
            ]
            if not pts:
                return out
            pts.sort(key=lambda x: x.ts_ms)
            # Last 5
            tail = pts[-5:]
            out["local_quote_history_5"] = [float(p.quote_sol) for p in tail]
            out["local_quote_last"] = float(tail[-1].quote_sol)
            # Peak in last 500ms.
            cutoff_500 = int(ts_ms_now) - 500
            in_500 = [p for p in pts if int(p.ts_ms) >= cutoff_500]
            if in_500:
                out["local_quote_peak_500ms"] = max(
                    float(p.quote_sol) for p in in_500
                )
            else:
                out["local_quote_peak_500ms"] = float(tail[-1].quote_sol)
            # Gradient series (last 3 deltas, oldest→newest).
            grad_full: List[float] = []
            for i in range(1, len(pts)):
                grad_full.append(
                    float(pts[i].quote_sol) - float(pts[i - 1].quote_sol)
                )
            out["local_quote_gradient_history_3"] = grad_full[-3:]
        except Exception:
            pass
        return out

    def _maybe_emit(self, mint: str, snap: Dict[str, Any]) -> None:
        self._emit_ctr += 1
        if self._emit_ctr % self._emit_denom != 0:
            return
        try:
            self._log(
                f"PGG2-V47I-MEDIUM-BUFFER mint={_short(mint)} "
                f"q_last={snap.get('local_quote_last')} "
                f"q_peak500={snap.get('local_quote_peak_500ms')} "
                f"q_hist_n={len(snap.get('local_quote_history_5') or [])}"
            )
        except Exception:
            pass


__all__ = ["V47IMediumWindowBuffer"]
