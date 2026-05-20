"""V46 Phase 3 - Pending-Flow Buffer.

Per-mint sliding-window state for raw Pump BUY/SELL shred events. A buy is
"pending" if its shred-receive timestamp is GREATER than the latest
accountSubscribe curve-update timestamp for the same mint (not yet reflected
in curve state).

PURE STATE. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple


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
            f"V46-BUFFER-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v46_pending_flow_buffer")


PENDING_WINDOWS_MS = (50, 100, 250, 500, 1000)
RETENTION_MS = 2000


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class _BuyEvent:
    ts_ms: int
    slot: int
    sol_in: float
    signer: str


@dataclass
class _SellEvent:
    ts_ms: int
    slot: int
    tokens_in: int
    sol_out_hint: float  # 0 if unknown; sell shred carries token amount
    signer: str


@dataclass
class _MintState:
    mint: str
    buys: Deque[_BuyEvent] = field(default_factory=lambda: deque(maxlen=512))
    sells: Deque[_SellEvent] = field(default_factory=lambda: deque(maxlen=512))
    latest_curve_update_ts_ms: int = 0
    latest_buy_ts_ms: int = 0
    latest_sell_ts_ms: int = 0


class V46PendingFlowBuffer:
    """Per-mint sliding-window aggregator for raw Pump BUY/SELL shred events.

    Causality contract: get_state(mint, now_ts_ms, latest_curve_update_ts_ms)
    uses ONLY events with ts <= now_ts_ms. The buffer also caches the latest
    curve update ts so that pending = (event_ts > latest_curve_update_ts).
    """

    def __init__(
        self,
        logger: Optional[Callable[[str], None]] = None,
        emit_sample_denom: int = 100,
    ) -> None:
        self._states: Dict[str, _MintState] = {}
        self._log = logger or (lambda _msg: None)
        self._emit_sample_denom = max(1, int(emit_sample_denom))
        self._emit_counter = 0

    def ingest_pump_buy(
        self,
        mint: str,
        sol_in: float,
        signer: str,
        slot: int,
        ts_ms: int,
    ) -> None:
        if not mint:
            return
        st = self._states.get(mint)
        if st is None:
            st = _MintState(mint=mint)
            self._states[mint] = st
        st.buys.append(
            _BuyEvent(
                ts_ms=int(ts_ms),
                slot=int(slot),
                sol_in=float(sol_in),
                signer=str(signer or ""),
            )
        )
        if int(ts_ms) > st.latest_buy_ts_ms:
            st.latest_buy_ts_ms = int(ts_ms)
        self._prune(st, int(ts_ms))

    def ingest_pump_sell(
        self,
        mint: str,
        tokens_in: int,
        signer: str,
        slot: int,
        ts_ms: int,
        sol_out_hint: float = 0.0,
    ) -> None:
        if not mint:
            return
        st = self._states.get(mint)
        if st is None:
            st = _MintState(mint=mint)
            self._states[mint] = st
        st.sells.append(
            _SellEvent(
                ts_ms=int(ts_ms),
                slot=int(slot),
                tokens_in=int(tokens_in),
                sol_out_hint=float(sol_out_hint),
                signer=str(signer or ""),
            )
        )
        if int(ts_ms) > st.latest_sell_ts_ms:
            st.latest_sell_ts_ms = int(ts_ms)
        self._prune(st, int(ts_ms))

    def mark_curve_update(self, mint: str, ts_ms: int) -> None:
        """Record the wall-clock arrival ts of a curve account update."""
        if not mint:
            return
        st = self._states.get(mint)
        if st is None:
            st = _MintState(mint=mint)
            self._states[mint] = st
        if int(ts_ms) > st.latest_curve_update_ts_ms:
            st.latest_curve_update_ts_ms = int(ts_ms)

    def latest_curve_update_ts(self, mint: str) -> int:
        st = self._states.get(mint)
        return int(st.latest_curve_update_ts_ms) if st is not None else 0

    def latest_buy_ts(self, mint: str) -> int:
        st = self._states.get(mint)
        return int(st.latest_buy_ts_ms) if st is not None else 0

    def _prune(self, st: _MintState, now_ts_ms: int) -> None:
        cutoff = int(now_ts_ms) - RETENTION_MS
        while st.buys and st.buys[0].ts_ms < cutoff:
            st.buys.popleft()
        while st.sells and st.sells[0].ts_ms < cutoff:
            st.sells.popleft()

    def get_state(
        self,
        mint: str,
        ts_ms_now: int,
        latest_curve_update_ts_ms: int,
    ) -> Dict[str, Any]:
        """Return per-mint pending-flow snapshot. CAUSAL: every input event
        used has ts <= ts_ms_now. `latest_curve_update_ts_ms` is the
        timestamp at which the most recent accountSubscribe curve update for
        this mint was observed; "pending" means event_ts > that ts AND
        event_ts <= ts_ms_now.
        """
        st = self._states.get(mint)
        if st is None:
            return _empty_state(int(latest_curve_update_ts_ms))

        cu_ts = max(
            int(latest_curve_update_ts_ms or 0),
            int(st.latest_curve_update_ts_ms or 0),
        )

        out: Dict[str, Any] = {}
        # Sliding-window aggregates.
        for w in PENDING_WINDOWS_MS:
            win_lo = int(ts_ms_now) - w
            buys_w = [
                b for b in st.buys
                if b.ts_ms > win_lo
                and b.ts_ms <= int(ts_ms_now)
                and b.ts_ms > cu_ts
            ]
            sells_w = [
                s for s in st.sells
                if s.ts_ms > win_lo
                and s.ts_ms <= int(ts_ms_now)
                and s.ts_ms > cu_ts
            ]
            out[f"pending_buy_count_{w}ms"] = len(buys_w)
            out[f"pending_buy_sol_{w}ms"] = float(
                sum(b.sol_in for b in buys_w)
            )
            out[f"pending_sell_count_{w}ms"] = len(sells_w)
            out[f"pending_sell_sol_{w}ms"] = float(
                sum(s.sol_out_hint for s in sells_w)
            )

        # Extra fields keyed off the 250ms window (spec).
        win_250_lo = int(ts_ms_now) - 250
        buys_250 = [
            b for b in st.buys
            if b.ts_ms > win_250_lo
            and b.ts_ms <= int(ts_ms_now)
            and b.ts_ms > cu_ts
        ]
        sells_250 = [
            s for s in st.sells
            if s.ts_ms > win_250_lo
            and s.ts_ms <= int(ts_ms_now)
            and s.ts_ms > cu_ts
        ]
        buy_sol_250 = sum(b.sol_in for b in buys_250)
        sell_sol_250 = sum(s.sol_out_hint for s in sells_250)
        out["net_pending_sol_250ms"] = float(buy_sol_250 - sell_sol_250)
        out["unique_buyers_250ms"] = len({
            b.signer for b in buys_250 if b.signer
        })
        out["largest_pending_buy_sol_250ms"] = float(
            max((b.sol_in for b in buys_250), default=0.0)
        )
        # buy_cluster_speed_250ms in buys/sec; if window only has 1 buy,
        # speed is 1/0.25=4.0; if 0 buys, 0.0.
        out["buy_cluster_speed_250ms"] = (
            float(len(buys_250)) / 0.25 if buys_250 else 0.0
        )

        latest_raw_buy_ts = max(
            (b.ts_ms for b in st.buys if b.ts_ms <= int(ts_ms_now)),
            default=0,
        )
        raw_buy_lead_ms = (
            float(cu_ts - latest_raw_buy_ts) if (cu_ts > 0 and latest_raw_buy_ts > 0)
            else 0.0
        )
        out["raw_buy_lead_ms_latest"] = raw_buy_lead_ms
        # "reflected_in_curve" means the latest curve update is newer than
        # the latest raw buy => no pending buy edge.
        out["reflected_in_curve"] = bool(cu_ts >= latest_raw_buy_ts and latest_raw_buy_ts > 0)
        out["latest_curve_update_ts_ms"] = int(cu_ts)
        out["latest_raw_buy_ts_ms"] = int(latest_raw_buy_ts)

        self._maybe_emit(mint, out)
        return out

    def _maybe_emit(self, mint: str, snap: Dict[str, Any]) -> None:
        self._emit_counter += 1
        if self._emit_counter % self._emit_sample_denom != 0:
            return
        try:
            self._log(
                f"PGG2-V46-PENDING-FLOW-BUFFER mint={_short(mint)} "
                f"pb250={snap.get('pending_buy_count_250ms', 0)} "
                f"pb_sol250={float(snap.get('pending_buy_sol_250ms', 0.0)):.6f} "
                f"ps250={snap.get('pending_sell_count_250ms', 0)} "
                f"net250={float(snap.get('net_pending_sol_250ms', 0.0)):+.6f} "
                f"ub250={snap.get('unique_buyers_250ms', 0)} "
                f"lpb250={float(snap.get('largest_pending_buy_sol_250ms', 0.0)):.6f} "
                f"lead={float(snap.get('raw_buy_lead_ms_latest', 0.0)):+.0f} "
                f"refl={int(bool(snap.get('reflected_in_curve', False)))}"
            )
        except Exception:
            pass

    def pending_buys(
        self,
        mint: str,
        ts_ms_now: int,
        latest_curve_update_ts_ms: int,
        window_ms: int = 250,
    ) -> List[Tuple[int, float, str, int]]:
        """Return list of (ts_ms, sol_in, signer, slot) for pending buys in
        window. Used by the projector."""
        st = self._states.get(mint)
        if st is None:
            return []
        cu_ts = max(
            int(latest_curve_update_ts_ms or 0),
            int(st.latest_curve_update_ts_ms or 0),
        )
        win_lo = int(ts_ms_now) - int(window_ms)
        return [
            (b.ts_ms, b.sol_in, b.signer, b.slot)
            for b in st.buys
            if b.ts_ms > win_lo
            and b.ts_ms <= int(ts_ms_now)
            and b.ts_ms > cu_ts
        ]

    def pending_sells(
        self,
        mint: str,
        ts_ms_now: int,
        latest_curve_update_ts_ms: int,
        window_ms: int = 250,
    ) -> List[Tuple[int, int, str, int]]:
        st = self._states.get(mint)
        if st is None:
            return []
        cu_ts = max(
            int(latest_curve_update_ts_ms or 0),
            int(st.latest_curve_update_ts_ms or 0),
        )
        win_lo = int(ts_ms_now) - int(window_ms)
        return [
            (s.ts_ms, s.tokens_in, s.signer, s.slot)
            for s in st.sells
            if s.ts_ms > win_lo
            and s.ts_ms <= int(ts_ms_now)
            and s.ts_ms > cu_ts
        ]


def _empty_state(cu_ts: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for w in PENDING_WINDOWS_MS:
        out[f"pending_buy_count_{w}ms"] = 0
        out[f"pending_buy_sol_{w}ms"] = 0.0
        out[f"pending_sell_count_{w}ms"] = 0
        out[f"pending_sell_sol_{w}ms"] = 0.0
    out["net_pending_sol_250ms"] = 0.0
    out["unique_buyers_250ms"] = 0
    out["largest_pending_buy_sol_250ms"] = 0.0
    out["buy_cluster_speed_250ms"] = 0.0
    out["raw_buy_lead_ms_latest"] = 0.0
    out["reflected_in_curve"] = False
    out["latest_curve_update_ts_ms"] = int(cu_ts)
    out["latest_raw_buy_ts_ms"] = 0
    return out


__all__ = ["V46PendingFlowBuffer", "PENDING_WINDOWS_MS"]
