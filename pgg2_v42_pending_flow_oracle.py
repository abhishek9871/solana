"""V42 — Pending-Flow Oracle.

Goal: detect external buy/sell flow per pump.fun mint EARLIER than the bot's
existing normal-quote pipeline can reflect it.

Inputs:
  - The bot's existing shred-stream raw trade feed (raw.jsonl rows or live
    PumpEvent objects). Each row has ts_ms, side, sol, signer, mint, recv_ns.
  - A monotonic clock (time.time() for live, or replay clock for forensic).

Per-mint rolling state, keyed by mint pubkey, windows {100,250,500,1000} ms:
  - pending_buy_count_{w}ms
  - pending_buy_sol_{w}ms
  - pending_sell_count_{w}ms
  - pending_sell_sol_{w}ms
  - net_flow_sol_{w}ms = buy_sol - sell_sol
  - largest_pending_buy_sol  (across the 1000ms window)
  - unique_pending_buyers   (set size, 1000ms window)
  - pending_buy_is_confirmed (always False at observation time; shred is pre-confirm)
  - feed_timestamp_ms       (latest event ts_ms within window)
  - feed_source             ("shred", "geyser", "rpc", "unknown")
  - feed_latency_ms         (now_ms - event ts_ms; what the feed costs you)
  - source_late             (boolean — feed did not see this faster than
                             normal-quote polling would have)

Hard rule: if the feed is no faster than the normal quote-poll latency for
this mint, mark source_late=True and refuse to use it for entry. We measure
that by tracking, per mint, (latest_shred_ts, latest_quote_observation_ts);
shred must lead quote by at least PGG2_V42_MIN_LEAD_MS to be useful.

Logging: PGG2-V42-PENDING-FLOW per rolling update for any mint with at least
one pending buy in the trailing 1000ms.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _now_ms() -> int:
    return int(time.time() * 1000)


WINDOWS_MS = (100, 250, 500, 1000)
# Largest window kept in the deque; old events are evicted when outside this.
RETENTION_MS = 1500


@dataclass
class PendingFlowEvent:
    ts_ms: int
    side: str           # "buy" / "sell"
    sol: float
    signer: str
    sig: str
    source: str = "shred"
    recv_ns: int = 0


@dataclass
class MintFlowState:
    mint: str
    events: deque = field(default_factory=lambda: deque(maxlen=4096))
    last_quote_observation_ts_ms: int = 0   # set by host bot when it sees a
                                            #  normal-pipeline quote refresh
    last_shred_ts_ms: int = 0
    last_emit_ts_ms: int = 0
    feed_source: str = "shred"
    cumulative_buys_seen: int = 0
    cumulative_buy_sol_seen: float = 0.0


class PendingFlowOracle:
    """Maintains per-mint pending-flow state and emits PGG2-V42-PENDING-FLOW.

    Usage:
        oracle = PendingFlowOracle(logger=print, feed_source="shred")
        # On every PumpEvent or raw.jsonl row arrival:
        oracle.ingest(mint, side="buy", sol=2.0, signer="...", ts_ms=..., sig=...)
        # On every normal-pipeline quote return (so we know how late shred would
        # have to be to lose its lead):
        oracle.mark_quote_observation(mint, ts_ms=...)
        # When deciding whether to enter:
        snapshot = oracle.snapshot(mint, now_ms=_now_ms())
        if snapshot["source_late"]: reject_entry()
    """

    def __init__(
        self,
        logger: Optional[Any] = None,
        feed_source: str = "shred",
        emit_min_interval_ms: int = 0,
    ) -> None:
        self._states: dict[str, MintFlowState] = {}
        self._logger = logger or (lambda *a, **kw: None)
        self._feed_source = feed_source
        self._emit_min_interval_ms = max(0, int(emit_min_interval_ms))
        # Minimum lead (shred earlier than quote-poll observation) before
        # the source is "useful". Default 50ms = below this and the feed
        # gives us no measurable lead over polling.
        self._min_lead_ms = _env_int("PGG2_V42_MIN_LEAD_MS", 50)
        # Maximum permitted feed age before we declare it late. Shred should
        # arrive within ~500ms of the network event; otherwise it's just a
        # delayed view.
        self._max_feed_age_ms = _env_int("PGG2_V42_MAX_FEED_AGE_MS", 1500)

    # -- ingestion ---------------------------------------------------------

    def ingest(
        self,
        mint: str,
        side: str,
        sol: float,
        signer: str,
        ts_ms: int,
        sig: str = "",
        source: Optional[str] = None,
        recv_ns: int = 0,
    ) -> None:
        if not mint or sol <= 0 or side not in ("buy", "sell"):
            return
        st = self._states.get(mint)
        if st is None:
            st = MintFlowState(mint=mint)
            self._states[mint] = st
        st.events.append(
            PendingFlowEvent(
                ts_ms=int(ts_ms),
                side=str(side),
                sol=float(sol),
                signer=str(signer or ""),
                sig=str(sig or ""),
                source=str(source or self._feed_source),
                recv_ns=int(recv_ns or 0),
            )
        )
        st.last_shred_ts_ms = max(st.last_shred_ts_ms, int(ts_ms))
        if side == "buy":
            st.cumulative_buys_seen += 1
            st.cumulative_buy_sol_seen += float(sol)
        self._evict_old(st, int(ts_ms))

    def mark_quote_observation(self, mint: str, ts_ms: int) -> None:
        """Host bot calls this whenever its normal-quote pipeline returns a
        quote for `mint`. We use the latency between shred reception and
        quote observation to detect a 'feed is no faster than quote' source.
        """
        if not mint:
            return
        st = self._states.get(mint)
        if st is None:
            st = MintFlowState(mint=mint)
            self._states[mint] = st
        st.last_quote_observation_ts_ms = max(
            st.last_quote_observation_ts_ms, int(ts_ms)
        )

    def _evict_old(self, st: MintFlowState, now_ts_ms: int) -> None:
        cutoff = now_ts_ms - RETENTION_MS
        while st.events and st.events[0].ts_ms < cutoff:
            st.events.popleft()

    # -- snapshot ----------------------------------------------------------

    def snapshot(self, mint: str, now_ms: Optional[int] = None) -> dict[str, Any]:
        """Return a dict with all V42 rolling-window fields.

        snapshot["source_late"] is True if the feed has NOT given us a useful
        lead over the normal-quote pipeline for this mint.
        """
        now_ts_ms = int(now_ms if now_ms is not None else _now_ms())
        st = self._states.get(mint)
        if st is None:
            return self._empty_snapshot(mint, now_ts_ms)
        self._evict_old(st, now_ts_ms)
        out: dict[str, Any] = {
            "mint": mint,
            "feed_source": self._feed_source,
            "feed_timestamp_ms": st.last_shred_ts_ms,
            "feed_latency_ms": max(0, now_ts_ms - st.last_shred_ts_ms)
            if st.last_shred_ts_ms
            else 0,
            "pending_buy_is_confirmed": False,  # shred sees txs BEFORE confirm
            "last_quote_observation_ts_ms": st.last_quote_observation_ts_ms,
            "cumulative_buys_seen": st.cumulative_buys_seen,
            "cumulative_buy_sol_seen": st.cumulative_buy_sol_seen,
        }
        largest_buy = 0.0
        signers: set[str] = set()
        for w in WINDOWS_MS:
            buy_count = 0
            buy_sol = 0.0
            sell_count = 0
            sell_sol = 0.0
            lo = now_ts_ms - w
            for ev in st.events:
                if ev.ts_ms < lo:
                    continue
                if ev.side == "buy":
                    buy_count += 1
                    buy_sol += ev.sol
                    if w == 1000:
                        signers.add(ev.signer)
                        if ev.sol > largest_buy:
                            largest_buy = ev.sol
                else:
                    sell_count += 1
                    sell_sol += ev.sol
            out[f"pending_buy_count_{w}ms"] = buy_count
            out[f"pending_buy_sol_{w}ms"] = buy_sol
            out[f"pending_sell_count_{w}ms"] = sell_count
            out[f"pending_sell_sol_{w}ms"] = sell_sol
            out[f"net_flow_sol_{w}ms"] = buy_sol - sell_sol
        out["largest_pending_buy_sol"] = largest_buy
        out["unique_pending_buyers"] = len(signers)
        # Lead-time check vs the bot's normal quote pipeline.
        lead_ms = 0
        if st.last_shred_ts_ms and st.last_quote_observation_ts_ms:
            # If shred saw the latest trade well before the latest quote
            # observation, shred is leading. The "lead" we care about is
            # (shred trade ts) - (quote observation that REFLECTS that trade).
            # Conservative: just require shred to have arrived BEFORE the
            # latest quote observation we have; the quote pipeline cannot
            # cover anything newer than its own last call.
            lead_ms = max(
                0,
                st.last_quote_observation_ts_ms - st.last_shred_ts_ms,
            )
        out["lead_ms_over_quote_poll"] = lead_ms
        # Feed-late conditions:
        #   1. The latest shred is older than max_feed_age_ms — the data is stale.
        #   2. We have a quote observation strictly newer than the latest
        #      shred trade (so the quote pipeline has already picked up
        #      anything the shred would warn about).
        feed_age = out["feed_latency_ms"]
        source_late = False
        late_reason = ""
        if feed_age > self._max_feed_age_ms:
            source_late = True
            late_reason = f"feed_stale:{feed_age}ms"
        if (
            st.last_quote_observation_ts_ms > 0
            and st.last_shred_ts_ms > 0
            and st.last_quote_observation_ts_ms >= st.last_shred_ts_ms - self._min_lead_ms
        ):
            # quote pipeline already at-or-ahead of shred for this mint
            source_late = True
            if not late_reason:
                late_reason = (
                    f"quote_at_or_ahead:shred={st.last_shred_ts_ms}"
                    f" quote={st.last_quote_observation_ts_ms}"
                )
        out["source_late"] = source_late
        out["source_late_reason"] = late_reason
        return out

    def _empty_snapshot(self, mint: str, now_ts_ms: int) -> dict[str, Any]:
        d: dict[str, Any] = {
            "mint": mint,
            "feed_source": self._feed_source,
            "feed_timestamp_ms": 0,
            "feed_latency_ms": 0,
            "pending_buy_is_confirmed": False,
            "last_quote_observation_ts_ms": 0,
            "cumulative_buys_seen": 0,
            "cumulative_buy_sol_seen": 0.0,
            "largest_pending_buy_sol": 0.0,
            "unique_pending_buyers": 0,
            "lead_ms_over_quote_poll": 0,
            "source_late": True,
            "source_late_reason": "no_data",
        }
        for w in WINDOWS_MS:
            d[f"pending_buy_count_{w}ms"] = 0
            d[f"pending_buy_sol_{w}ms"] = 0.0
            d[f"pending_sell_count_{w}ms"] = 0
            d[f"pending_sell_sol_{w}ms"] = 0.0
            d[f"net_flow_sol_{w}ms"] = 0.0
        return d

    # -- emit --------------------------------------------------------------

    def maybe_emit(self, mint: str, now_ms: Optional[int] = None) -> Optional[dict[str, Any]]:
        snap = self.snapshot(mint, now_ms=now_ms)
        if snap.get("pending_buy_count_1000ms", 0) <= 0:
            return None
        st = self._states.get(mint)
        if st is None:
            return None
        now_ts_ms = int(now_ms if now_ms is not None else _now_ms())
        if (now_ts_ms - st.last_emit_ts_ms) < self._emit_min_interval_ms:
            return snap
        st.last_emit_ts_ms = now_ts_ms
        try:
            short = (mint[:4] + ".." + mint[-4:]) if len(mint) > 10 else mint
            self._logger(
                f"PGG2-V42-PENDING-FLOW mint={short} "
                f"src={snap['feed_source']} feed_age_ms={snap['feed_latency_ms']} "
                f"lead_ms={snap['lead_ms_over_quote_poll']} source_late={int(bool(snap['source_late']))} "
                f"buy100={snap['pending_buy_count_100ms']}/{snap['pending_buy_sol_100ms']:.3f} "
                f"buy250={snap['pending_buy_count_250ms']}/{snap['pending_buy_sol_250ms']:.3f} "
                f"buy500={snap['pending_buy_count_500ms']}/{snap['pending_buy_sol_500ms']:.3f} "
                f"buy1000={snap['pending_buy_count_1000ms']}/{snap['pending_buy_sol_1000ms']:.3f} "
                f"sell1000={snap['pending_sell_count_1000ms']}/{snap['pending_sell_sol_1000ms']:.3f} "
                f"net1000={snap['net_flow_sol_1000ms']:+.3f} "
                f"buyers1000={snap['unique_pending_buyers']} "
                f"largest_buy={snap['largest_pending_buy_sol']:.3f}"
            )
        except Exception:
            pass
        return snap


__all__ = [
    "PendingFlowOracle",
    "PendingFlowEvent",
    "MintFlowState",
    "WINDOWS_MS",
]
