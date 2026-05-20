"""V42B — Feed Fusion Oracle.

Multi-source pending-flow oracle. Supports up to four input feeds:
  - shred       (the bot's existing Solana Tracker shred-decode feed)
  - pumpportal  (PumpPortal WSS - requires PGG2_PUMPPORTAL_ENABLED + creds)
  - helius      (Helius Geyser gRPC - requires PGG2_HELIUS_GRPC_ENABLED + creds)
  - bitquery    (Bitquery CoreCast gRPC - requires PGG2_BITQUERY_CORECAST_ENABLED + creds)

For any source whose env-flag is unset / credentials are missing, the oracle
emits a single startup `PGG2-V42-FEED-SOURCE-STATUS source=<name>
status=unavailable_no_credentials` line and never tries to ingest from it.
Live sources emit `status=live`.

The shred path remains the supporting data line (mint-filtered, incomplete on
some hot mints — established in V42 phase-4 forensic). The curve-delta lead
oracle (separate module pgg2_v42_curve_delta_lead.py) is the *primary*
differentiator and is consulted by the projector regardless of feed
availability.

Per-event normalised struct:

@dataclass
class V42FlowEvent:
    mint: str
    source: str          # "shred" / "pumpportal" / "helius" / "bitquery"
    source_latency_ms: int
    observed_ts_ms: int
    slot: int
    is_buy: bool
    is_sell: bool
    sol_amount: float
    token_amount: float
    buyer: str
    seller: str
    curve_delta_sol: float
    curve_delta_token: float
    curve_price_delta: float
    confidence: float
    source_late: bool

Logs emitted:
  PGG2-V42-FEED-SOURCE-STATUS   (per source at startup + on transition)
  PGG2-V42-FEED-FUSION-EVENT    (per ingested event, rate-limited per mint)
  PGG2-V42-CURVE-DELTA          (per delta snapshot, deferred to curve_delta module)
  PGG2-V42-FEED-BUDGET          (periodic source utilisation summary)
"""
from __future__ import annotations

import os
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Optional


SOURCES = ("shred", "pumpportal", "helius", "bitquery")
WINDOWS_MS = (100, 250, 500, 1000)
RETENTION_MS = 1500


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class V42FlowEvent:
    mint: str
    source: str
    source_latency_ms: int
    observed_ts_ms: int
    slot: int
    is_buy: bool
    is_sell: bool
    sol_amount: float
    token_amount: float
    buyer: str = ""
    seller: str = ""
    curve_delta_sol: float = 0.0
    curve_delta_token: float = 0.0
    curve_price_delta: float = 0.0
    confidence: float = 1.0
    source_late: bool = False
    sig: str = ""


@dataclass
class _PerMintState:
    mint: str
    events: deque = field(default_factory=lambda: deque(maxlen=4096))
    last_quote_observation_ts_ms: int = 0
    last_emit_ts_ms: int = 0
    sources_seen: Counter = field(default_factory=Counter)
    cumulative_buys_seen: int = 0
    cumulative_buy_sol_seen: float = 0.0
    last_event_ts_ms: int = 0
    last_event_source: str = ""


def _credentials_for_source(source: str) -> tuple[bool, list[str]]:
    """Return (available, missing_keys). Missing means we won't ingest."""
    if source == "shred":
        # The bot's existing shred feed is the primary on-host source; the
        # raw.jsonl tap is always available because the bot writes it as it
        # runs. No external creds required.
        return True, []
    if source == "pumpportal":
        if not _env_bool("PGG2_PUMPPORTAL_ENABLED", False):
            return False, ["PGG2_PUMPPORTAL_ENABLED"]
        keys = [k for k in ("PUMPPORTAL_URL", "PUMPPORTAL_API_KEY") if not os.environ.get(k)]
        return (not keys), keys
    if source == "helius":
        if not _env_bool("PGG2_HELIUS_GRPC_ENABLED", False):
            return False, ["PGG2_HELIUS_GRPC_ENABLED"]
        keys = [k for k in ("HELIUS_GRPC_URL", "HELIUS_GRPC_API_KEY") if not os.environ.get(k)]
        return (not keys), keys
    if source == "bitquery":
        if not _env_bool("PGG2_BITQUERY_CORECAST_ENABLED", False):
            return False, ["PGG2_BITQUERY_CORECAST_ENABLED"]
        keys = [k for k in ("BITQUERY_CORECAST_URL", "BITQUERY_CORECAST_API_KEY") if not os.environ.get(k)]
        return (not keys), keys
    return False, [f"unknown_source:{source}"]


class FeedFusionOracle:
    """Multi-source flow aggregation for V42B.

    Usage:
        oracle = FeedFusionOracle(logger=print)
        oracle.emit_startup_status()
        oracle.ingest_event(V42FlowEvent(mint=..., source="shred", ...))
        snap = oracle.snapshot(mint, now_ms=_now_ms())

    `snap` keys (in addition to inherited from V42 shred oracle):
        per-source buys/sells per-window
        source_agreement_count   # number of distinct sources seeing buys in 1s
        primary_source           # source contributing most events
        union_unique_buyers      # union of unique buyer pubkeys across sources
        active_sources           # set of currently-live sources
    """

    def __init__(
        self,
        logger: Optional[Any] = None,
        emit_min_interval_ms: int = 0,
        budget_every_ms: int = 30000,
    ) -> None:
        self._states: dict[str, _PerMintState] = {}
        self._logger = logger or (lambda *a, **kw: None)
        self._emit_min_interval_ms = max(0, int(emit_min_interval_ms))
        self._min_lead_ms = _env_int("PGG2_V42_MIN_LEAD_MS", 50)
        self._max_feed_age_ms = _env_int("PGG2_V42_MAX_FEED_AGE_MS", 1500)
        # Source availability snapshot - we compute once on init and on
        # explicit re-check. Note: the feed_loader (Phase 6 capture script)
        # is responsible for actually opening sockets/connections; this
        # oracle just records status and ingests whatever rows arrive.
        self._source_status: dict[str, dict[str, Any]] = {}
        for s in SOURCES:
            ok, missing = _credentials_for_source(s)
            self._source_status[s] = {
                "available": ok,
                "missing_keys": missing,
                "events_ingested": 0,
                "last_ingest_ts_ms": 0,
                "status": "live" if ok else "unavailable_no_credentials",
            }
        self._budget_every_ms = max(1000, int(budget_every_ms))
        self._last_budget_emit_ms = 0
        self._global_counters: Counter = Counter()

    def emit_startup_status(self) -> None:
        for s in SOURCES:
            row = self._source_status[s]
            missing = ",".join(row["missing_keys"]) if row["missing_keys"] else ""
            try:
                self._logger(
                    f"PGG2-V42-FEED-SOURCE-STATUS source={s} status={row['status']}"
                    + (f" missing={missing}" if missing else "")
                )
            except Exception:
                pass

    def source_available(self, source: str) -> bool:
        return bool(self._source_status.get(source, {}).get("available", False))

    def available_sources(self) -> list[str]:
        return [s for s in SOURCES if self.source_available(s)]

    # -- ingestion ---------------------------------------------------------

    def ingest_event(self, ev: V42FlowEvent) -> None:
        if not ev or not ev.mint:
            return
        if ev.source not in SOURCES:
            return
        if not self.source_available(ev.source):
            # Caller mis-routed; drop silently because emit_startup_status
            # already informed the user.
            return
        if ev.sol_amount <= 0 and ev.token_amount <= 0:
            return
        st = self._states.get(ev.mint)
        if st is None:
            st = _PerMintState(mint=ev.mint)
            self._states[ev.mint] = st
        st.events.append(ev)
        st.last_event_ts_ms = max(st.last_event_ts_ms, int(ev.observed_ts_ms))
        st.last_event_source = ev.source
        st.sources_seen[ev.source] += 1
        if ev.is_buy:
            st.cumulative_buys_seen += 1
            st.cumulative_buy_sol_seen += float(ev.sol_amount)
        self._source_status[ev.source]["events_ingested"] += 1
        self._source_status[ev.source]["last_ingest_ts_ms"] = int(ev.observed_ts_ms)
        self._global_counters[ev.source] += 1
        self._evict_old(st, int(ev.observed_ts_ms))

    def ingest_shred_row(
        self,
        mint: str,
        side: str,
        sol: float,
        signer: str,
        ts_ms: int,
        sig: str = "",
        recv_ns: int = 0,
        slot: int = 0,
        token_amount: float = 0.0,
    ) -> None:
        """Convenience wrapper around ingest_event for raw.jsonl rows."""
        if not mint or side not in ("buy", "sell"):
            return
        if sol <= 0 and token_amount <= 0:
            return
        # source_latency_ms = wall-clock now - event ts; computed at the time
        # we observe the row. For replay we feed both equal so latency=0.
        now = _now_ms()
        lat = max(0, now - int(ts_ms))
        ev = V42FlowEvent(
            mint=str(mint),
            source="shred",
            source_latency_ms=lat,
            observed_ts_ms=int(ts_ms),
            slot=int(slot or 0),
            is_buy=(side == "buy"),
            is_sell=(side == "sell"),
            sol_amount=float(sol),
            token_amount=float(token_amount or 0.0),
            buyer=str(signer or "") if side == "buy" else "",
            seller=str(signer or "") if side == "sell" else "",
            confidence=1.0,
            source_late=False,
            sig=str(sig or ""),
        )
        self.ingest_event(ev)

    def mark_quote_observation(self, mint: str, ts_ms: int) -> None:
        if not mint:
            return
        st = self._states.get(mint)
        if st is None:
            st = _PerMintState(mint=mint)
            self._states[mint] = st
        st.last_quote_observation_ts_ms = max(
            st.last_quote_observation_ts_ms, int(ts_ms)
        )

    def _evict_old(self, st: _PerMintState, now_ts_ms: int) -> None:
        cutoff = now_ts_ms - RETENTION_MS
        while st.events and st.events[0].observed_ts_ms < cutoff:
            st.events.popleft()

    # -- snapshot ----------------------------------------------------------

    def snapshot(self, mint: str, now_ms: Optional[int] = None) -> dict[str, Any]:
        now_ts_ms = int(now_ms if now_ms is not None else _now_ms())
        st = self._states.get(mint)
        if st is None:
            return self._empty_snapshot(mint, now_ts_ms)
        self._evict_old(st, now_ts_ms)
        out: dict[str, Any] = {
            "mint": mint,
            "feed_timestamp_ms": st.last_event_ts_ms,
            "feed_latency_ms": max(0, now_ts_ms - st.last_event_ts_ms) if st.last_event_ts_ms else 0,
            "pending_buy_is_confirmed": False,
            "last_quote_observation_ts_ms": st.last_quote_observation_ts_ms,
            "cumulative_buys_seen": st.cumulative_buys_seen,
            "cumulative_buy_sol_seen": st.cumulative_buy_sol_seen,
            "primary_source": st.last_event_source or "shred",
            "sources_seen_counts": dict(st.sources_seen),
            "active_sources": self.available_sources(),
        }
        signers: set[str] = set()
        largest_buy = 0.0
        per_source_buy_count: Counter = Counter()
        per_source_buy_sol: dict[str, float] = {s: 0.0 for s in SOURCES}
        for w in WINDOWS_MS:
            buy_count = 0
            buy_sol = 0.0
            sell_count = 0
            sell_sol = 0.0
            lo = now_ts_ms - w
            for ev in st.events:
                if ev.observed_ts_ms < lo:
                    continue
                if ev.is_buy:
                    buy_count += 1
                    buy_sol += ev.sol_amount
                    if w == 1000:
                        signers.add(ev.buyer)
                        if ev.sol_amount > largest_buy:
                            largest_buy = ev.sol_amount
                        per_source_buy_count[ev.source] += 1
                        per_source_buy_sol[ev.source] = per_source_buy_sol.get(ev.source, 0.0) + ev.sol_amount
                elif ev.is_sell:
                    sell_count += 1
                    sell_sol += ev.sol_amount
            out[f"pending_buy_count_{w}ms"] = buy_count
            out[f"pending_buy_sol_{w}ms"] = buy_sol
            out[f"pending_sell_count_{w}ms"] = sell_count
            out[f"pending_sell_sol_{w}ms"] = sell_sol
            out[f"net_flow_sol_{w}ms"] = buy_sol - sell_sol
        out["largest_pending_buy_sol"] = largest_buy
        out["unique_pending_buyers"] = len(signers - {""})
        # Source agreement: number of sources with at least one buy in 1s.
        agreement = sum(1 for c in per_source_buy_count.values() if c > 0)
        out["source_agreement_count"] = int(agreement)
        out["per_source_buy_count_1000ms"] = dict(per_source_buy_count)
        out["per_source_buy_sol_1000ms"] = {k: float(v) for k, v in per_source_buy_sol.items()}
        # Lead-time over quote pipeline.
        lead_ms = 0
        if st.last_event_ts_ms and st.last_quote_observation_ts_ms:
            lead_ms = max(0, st.last_quote_observation_ts_ms - st.last_event_ts_ms)
        out["lead_ms_over_quote_poll"] = lead_ms
        feed_age = out["feed_latency_ms"]
        source_late = False
        late_reason = ""
        if feed_age > self._max_feed_age_ms:
            source_late = True
            late_reason = f"feed_stale:{feed_age}ms"
        if (
            st.last_quote_observation_ts_ms > 0
            and st.last_event_ts_ms > 0
            and st.last_quote_observation_ts_ms >= st.last_event_ts_ms - self._min_lead_ms
        ):
            source_late = True
            if not late_reason:
                late_reason = (
                    f"quote_at_or_ahead:feed={st.last_event_ts_ms}"
                    f" quote={st.last_quote_observation_ts_ms}"
                )
        out["source_late"] = source_late
        out["source_late_reason"] = late_reason
        out["feed_source"] = out["primary_source"]
        return out

    def _empty_snapshot(self, mint: str, now_ts_ms: int) -> dict[str, Any]:
        d: dict[str, Any] = {
            "mint": mint,
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
            "primary_source": "",
            "feed_source": "",
            "sources_seen_counts": {},
            "active_sources": self.available_sources(),
            "source_agreement_count": 0,
            "per_source_buy_count_1000ms": {},
            "per_source_buy_sol_1000ms": {},
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
                f"PGG2-V42-FEED-FUSION-EVENT mint={short} "
                f"primary_src={snap['primary_source']} "
                f"agreement={snap['source_agreement_count']} "
                f"feed_age_ms={snap['feed_latency_ms']} "
                f"buy1000={snap['pending_buy_count_1000ms']}/{snap['pending_buy_sol_1000ms']:.3f} "
                f"sell1000={snap['pending_sell_count_1000ms']}/{snap['pending_sell_sol_1000ms']:.3f} "
                f"net1000={snap['net_flow_sol_1000ms']:+.3f} "
                f"buyers1000={snap['unique_pending_buyers']} "
                f"largest_buy={snap['largest_pending_buy_sol']:.3f} "
                f"source_late={int(bool(snap['source_late']))} "
                f"per_src={snap['per_source_buy_count_1000ms']}"
            )
        except Exception:
            pass
        self._maybe_emit_budget(now_ts_ms)
        return snap

    def _maybe_emit_budget(self, now_ts_ms: int) -> None:
        if now_ts_ms - self._last_budget_emit_ms < self._budget_every_ms:
            return
        self._last_budget_emit_ms = now_ts_ms
        try:
            parts: list[str] = []
            for s in SOURCES:
                row = self._source_status[s]
                parts.append(
                    f"{s}={row['status']}:n={row['events_ingested']}"
                )
            self._logger("PGG2-V42-FEED-BUDGET " + " ".join(parts))
        except Exception:
            pass


__all__ = [
    "FeedFusionOracle",
    "V42FlowEvent",
    "SOURCES",
    "WINDOWS_MS",
]
