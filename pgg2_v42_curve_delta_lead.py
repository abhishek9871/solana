"""V42B — Curve Delta Lead Oracle (MANDATORY primary differentiator).

Polls the on-chain bonding-curve account state via the broker's existing
`bonding_curve(mint)` (Solana Tracker RPC, processed commitment, TTL=0 by
default). Computes rolling deltas over {100,250,500,1000,1500,2000}ms.

Why this is the primary differentiator: the V42 phase-4 forensic showed 5 of
10 dry-live winners had VISIBLE curve movement but NO individual shred-trade
event — the shred feed is mint-filtered and incomplete. The bonding-curve
account state is the canonical truth: whoever traded, however they routed,
the curve moves. So as long as we can RPC-poll the curve, we don't need the
trade feed for *detection*.

Features per snapshot:
  curve_price                       (vSOL / vTOK)
  virtual_sol_reserves
  virtual_token_reserves
  curve_price_delta_{w}ms           (over window w)
  vsol_delta_{w}ms                  (raw lamports)
  vtok_delta_{w}ms                  (raw token base units)
  positive_delta_streak_length      (consecutive +ve curve_price_delta_1000ms)
  quote_out_delta_{w}ms             (projected sell SOL out for a fixed
                                     test_sell_tokens; gradient)
  sell_quote_gradient               (most recent quote_out_delta_1000ms)
  buy_sell_imbalance_1000ms         (sourced from FeedFusionOracle if present)
  source_agreement_count            (likewise)
  curve_delta_is_leading            (bool — curve_price moved up BEFORE
                                     feed observed a buy)
  source_latency_p50 / p95          (per-poll cost incl. RPC RTT)
  is_late                           (bool — last poll older than max_age_ms)

Cost: bonding_curve() is a single getAccountInfo RPC call ~100-500ms RTT
under load. We rate-limit per-mint polling and reuse the bot's existing curve
cache (PGG2_DIRECT_CURVE_ACCOUNT_TTL_SEC=0 means always fresh — good for our
purpose but expensive; we therefore cap to ~3 polls/sec/mint and keep at
most N hot mints concurrently).

Emits `PGG2-V42-CURVE-DELTA-LEAD` per snapshot.
"""
from __future__ import annotations

import math
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


CURVE_WINDOWS_MS = (100, 250, 500, 1000, 1500, 2000)
RETENTION_MS = 30000  # 30s of history — see comment in module docstring
TEST_SELL_TOKENS_RAW = 1_000_000_000  # ~1k base-unit tokens for sell-gradient sample
LAMPORTS_PER_SOL = 1_000_000_000


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class CurvePoint:
    ts_ms: int
    rtt_ms: int
    virtual_sol_reserves: int
    virtual_token_reserves: int
    real_sol_reserves: int
    real_token_reserves: int
    curve_price: float
    quote_out_sol_for_sample: float  # projected sell of TEST_SELL_TOKENS_RAW
    error: str = ""


@dataclass
class _CurveMintState:
    mint: str
    points: deque = field(default_factory=lambda: deque(maxlen=128))
    last_poll_ts_ms: int = 0
    last_feed_event_ts_ms: int = 0
    last_emit_ts_ms: int = 0
    poll_rtt_history: deque = field(default_factory=lambda: deque(maxlen=64))
    positive_delta_streak: int = 0
    last_curve_price_delta_1000ms: float = 0.0


class CurveDeltaLeadOracle:
    """Polls per-mint curve and computes rolling-window deltas.

    Usage:
      oracle = CurveDeltaLeadOracle(broker=broker, logger=print)
      oracle.poll_if_due(mint)            # rate-limited
      snap = oracle.snapshot(mint)
      if snap['curve_delta_is_leading'] and snap['positive_delta_streak'] >= 2:
          ...

    Polling rate-limit is per-mint; the host (the V42B capture script) is
    responsible for selecting WHICH mints to poll (the hot set).
    """

    def __init__(
        self,
        broker: Any,
        logger: Optional[Any] = None,
        emit_min_interval_ms: int = 0,
        max_poll_rate_hz_per_mint: float = 3.0,
        max_age_ms_for_late: int = 1500,
    ) -> None:
        self._broker = broker
        self._logger = logger or (lambda *a, **kw: None)
        self._states: dict[str, _CurveMintState] = {}
        self._emit_min_interval_ms = max(0, int(emit_min_interval_ms))
        self._max_poll_rate_hz = max(0.1, float(max_poll_rate_hz_per_mint))
        self._max_age_ms_for_late = int(max_age_ms_for_late)
        # We import these lazily once to keep startup fast.
        self._as_pubkey = None
        self._pump_global_cached = None
        self._pump_global_cached_ts_ms = 0

    def _ensure_helpers(self) -> bool:
        if self._as_pubkey is not None:
            return True
        try:
            from pgg2_direct_pump import as_pubkey  # type: ignore
            self._as_pubkey = as_pubkey
            return True
        except Exception as exc:
            try:
                self._logger(f"PGG2-V42-CURVE-DELTA-LEAD-INIT-ERR {type(exc).__name__}:{exc}")
            except Exception:
                pass
            return False

    def _global_cfg(self) -> Any:
        # 30s TTL is generous; pump_global rarely changes.
        now = _now_ms()
        if self._pump_global_cached is not None and (now - self._pump_global_cached_ts_ms) < 30000:
            return self._pump_global_cached
        try:
            g = self._broker.pump_global()
            self._pump_global_cached = g
            self._pump_global_cached_ts_ms = now
            return g
        except Exception:
            return self._pump_global_cached

    def mark_feed_event(self, mint: str, ts_ms: int) -> None:
        """Called by the host whenever a trade event from any feed is seen
        for `mint`. We use this to decide whether curve-delta is LEADING the
        feed (curve moved up before any feed event was seen) or merely
        REACTING to it.
        """
        if not mint:
            return
        st = self._states.get(mint)
        if st is None:
            st = _CurveMintState(mint=mint)
            self._states[mint] = st
        st.last_feed_event_ts_ms = max(st.last_feed_event_ts_ms, int(ts_ms))

    def poll_if_due(self, mint: str, now_ms: Optional[int] = None, force: bool = False) -> Optional[CurvePoint]:
        if not self._ensure_helpers():
            return None
        if not mint:
            return None
        now_ts_ms = int(now_ms if now_ms is not None else _now_ms())
        st = self._states.get(mint)
        if st is None:
            st = _CurveMintState(mint=mint)
            self._states[mint] = st
        gap = now_ts_ms - st.last_poll_ts_ms
        min_gap = int(1000.0 / self._max_poll_rate_hz)
        if not force and st.last_poll_ts_ms and gap < min_gap:
            return None
        t0 = time.time()
        try:
            mint_pk = self._as_pubkey(mint)
            curve = self._broker.bonding_curve(mint_pk)
        except Exception as exc:
            rtt_ms = int((time.time() - t0) * 1000)
            pt = CurvePoint(
                ts_ms=now_ts_ms,
                rtt_ms=rtt_ms,
                virtual_sol_reserves=0,
                virtual_token_reserves=0,
                real_sol_reserves=0,
                real_token_reserves=0,
                curve_price=0.0,
                quote_out_sol_for_sample=0.0,
                error=f"{type(exc).__name__}:{exc}",
            )
            st.points.append(pt)
            st.last_poll_ts_ms = now_ts_ms
            st.poll_rtt_history.append(rtt_ms)
            return pt
        rtt_ms = int((time.time() - t0) * 1000)
        vsol = int(curve.virtual_sol_reserves)
        vtok = int(curve.virtual_token_reserves)
        price = vsol / max(1, vtok)
        sample_out = 0.0
        try:
            g = self._global_cfg()
            if g is not None:
                lamports, _fee = self._broker.quote_pump_sell_sol(TEST_SELL_TOKENS_RAW, curve, g)
                sample_out = float(lamports) / LAMPORTS_PER_SOL
        except Exception:
            sample_out = 0.0
        pt = CurvePoint(
            ts_ms=now_ts_ms,
            rtt_ms=rtt_ms,
            virtual_sol_reserves=vsol,
            virtual_token_reserves=vtok,
            real_sol_reserves=int(getattr(curve, "real_sol_reserves", 0)),
            real_token_reserves=int(getattr(curve, "real_token_reserves", 0)),
            curve_price=price,
            quote_out_sol_for_sample=sample_out,
        )
        st.points.append(pt)
        st.last_poll_ts_ms = now_ts_ms
        st.poll_rtt_history.append(rtt_ms)
        self._evict_old(st, now_ts_ms)
        # Maintain streak: compare price_delta over 1000ms vs previous.
        cur_delta = self._price_delta_over_window(st, 1000, now_ts_ms)
        if cur_delta > 0 and st.last_curve_price_delta_1000ms >= 0:
            st.positive_delta_streak += 1
        elif cur_delta <= 0:
            st.positive_delta_streak = 0
        st.last_curve_price_delta_1000ms = cur_delta
        return pt

    def _evict_old(self, st: _CurveMintState, now_ts_ms: int) -> None:
        cutoff = now_ts_ms - RETENTION_MS
        while st.points and st.points[0].ts_ms < cutoff:
            st.points.popleft()

    def _price_delta_over_window(self, st: _CurveMintState, w_ms: int, now_ts_ms: int) -> float:
        if len(st.points) < 2:
            return 0.0
        lo = now_ts_ms - w_ms
        baseline: Optional[CurvePoint] = None
        latest: Optional[CurvePoint] = None
        for p in st.points:
            if p.error:
                continue
            if p.ts_ms <= lo and (baseline is None or p.ts_ms > baseline.ts_ms):
                baseline = p
            latest = p
        if baseline is None:
            # If no point old enough, use the oldest available.
            for p in st.points:
                if p.error:
                    continue
                baseline = p
                break
        if baseline is None or latest is None or baseline is latest:
            return 0.0
        return float(latest.curve_price - baseline.curve_price)

    def _reserves_delta_over_window(self, st: _CurveMintState, w_ms: int, now_ts_ms: int) -> tuple[int, int, float]:
        """Return (vsol_delta_lamports, vtok_delta, quote_out_delta)."""
        if len(st.points) < 2:
            return 0, 0, 0.0
        lo = now_ts_ms - w_ms
        baseline: Optional[CurvePoint] = None
        latest: Optional[CurvePoint] = None
        for p in st.points:
            if p.error:
                continue
            if p.ts_ms <= lo and (baseline is None or p.ts_ms > baseline.ts_ms):
                baseline = p
            latest = p
        if baseline is None:
            for p in st.points:
                if p.error:
                    continue
                baseline = p
                break
        if baseline is None or latest is None or baseline is latest:
            return 0, 0, 0.0
        return (
            int(latest.virtual_sol_reserves - baseline.virtual_sol_reserves),
            int(latest.virtual_token_reserves - baseline.virtual_token_reserves),
            float(latest.quote_out_sol_for_sample - baseline.quote_out_sol_for_sample),
        )

    # -- snapshot ----------------------------------------------------------

    def snapshot(
        self,
        mint: str,
        now_ms: Optional[int] = None,
        feed_buy_count_1000ms: int = 0,
        feed_sell_count_1000ms: int = 0,
        source_agreement_count: int = 0,
        feed_last_event_ts_ms: int = 0,
    ) -> dict[str, Any]:
        now_ts_ms = int(now_ms if now_ms is not None else _now_ms())
        st = self._states.get(mint)
        if st is None or not st.points:
            return self._empty_snapshot(mint, now_ts_ms)
        self._evict_old(st, now_ts_ms)
        # latest valid point
        latest = None
        for p in reversed(st.points):
            if not p.error:
                latest = p
                break
        if latest is None:
            return self._empty_snapshot(mint, now_ts_ms)
        d: dict[str, Any] = {
            "mint": mint,
            "curve_price": float(latest.curve_price),
            "virtual_sol_reserves": int(latest.virtual_sol_reserves),
            "virtual_token_reserves": int(latest.virtual_token_reserves),
            "real_sol_reserves": int(latest.real_sol_reserves),
            "real_token_reserves": int(latest.real_token_reserves),
            "quote_out_sample_sol": float(latest.quote_out_sol_for_sample),
            "last_poll_ts_ms": int(st.last_poll_ts_ms),
            "last_poll_rtt_ms": int(latest.rtt_ms),
            "n_points": len([p for p in st.points if not p.error]),
            "positive_delta_streak": int(st.positive_delta_streak),
        }
        for w in CURVE_WINDOWS_MS:
            d[f"curve_price_delta_{w}ms"] = self._price_delta_over_window(st, w, now_ts_ms)
            vsol_d, vtok_d, q_d = self._reserves_delta_over_window(st, w, now_ts_ms)
            d[f"vsol_delta_{w}ms"] = vsol_d
            d[f"vtok_delta_{w}ms"] = vtok_d
            d[f"quote_out_delta_{w}ms"] = q_d
        d["sell_quote_gradient"] = float(d.get("quote_out_delta_1000ms", 0.0))
        # Imbalance: external signal injection from FeedFusionOracle.
        d["feed_buy_count_1000ms"] = int(feed_buy_count_1000ms)
        d["feed_sell_count_1000ms"] = int(feed_sell_count_1000ms)
        total = int(feed_buy_count_1000ms + feed_sell_count_1000ms)
        d["buy_sell_imbalance_1000ms"] = (
            (feed_buy_count_1000ms - feed_sell_count_1000ms) / float(total)
            if total > 0
            else 0.0
        )
        d["source_agreement_count"] = int(source_agreement_count)
        # Leading: curve moved up before any feed event, OR curve still moving
        # up but feed has no recent events (no event in last 1000ms).
        last_feed_ts = int(feed_last_event_ts_ms or st.last_feed_event_ts_ms)
        last_curve_ts = int(latest.ts_ms)
        cur_price_delta_1s = float(d.get("curve_price_delta_1000ms", 0.0))
        if cur_price_delta_1s > 0 and (last_feed_ts == 0 or last_curve_ts < last_feed_ts):
            curve_delta_is_leading = True
        elif cur_price_delta_1s > 0 and (last_curve_ts - last_feed_ts) <= 250:
            # essentially simultaneous — count as leading (curve confirms what
            # any feed *would* be reporting)
            curve_delta_is_leading = True
        else:
            curve_delta_is_leading = False
        d["curve_delta_is_leading"] = bool(curve_delta_is_leading)
        # Latency stats.
        rtts = [r for r in st.poll_rtt_history if r > 0]
        if rtts:
            srt = sorted(rtts)
            d["source_latency_p50_ms"] = int(statistics.median(srt))
            idx = max(0, int(len(srt) * 0.95) - 1)
            d["source_latency_p95_ms"] = int(srt[idx])
        else:
            d["source_latency_p50_ms"] = 0
            d["source_latency_p95_ms"] = 0
        feed_age_ms = max(0, now_ts_ms - last_curve_ts)
        d["curve_age_ms"] = int(feed_age_ms)
        d["is_late"] = bool(feed_age_ms > self._max_age_ms_for_late)
        return d

    def _empty_snapshot(self, mint: str, now_ts_ms: int) -> dict[str, Any]:
        d: dict[str, Any] = {
            "mint": mint,
            "curve_price": 0.0,
            "virtual_sol_reserves": 0,
            "virtual_token_reserves": 0,
            "real_sol_reserves": 0,
            "real_token_reserves": 0,
            "quote_out_sample_sol": 0.0,
            "last_poll_ts_ms": 0,
            "last_poll_rtt_ms": 0,
            "n_points": 0,
            "positive_delta_streak": 0,
            "sell_quote_gradient": 0.0,
            "feed_buy_count_1000ms": 0,
            "feed_sell_count_1000ms": 0,
            "buy_sell_imbalance_1000ms": 0.0,
            "source_agreement_count": 0,
            "curve_delta_is_leading": False,
            "source_latency_p50_ms": 0,
            "source_latency_p95_ms": 0,
            "curve_age_ms": now_ts_ms,
            "is_late": True,
        }
        for w in CURVE_WINDOWS_MS:
            d[f"curve_price_delta_{w}ms"] = 0.0
            d[f"vsol_delta_{w}ms"] = 0
            d[f"vtok_delta_{w}ms"] = 0
            d[f"quote_out_delta_{w}ms"] = 0.0
        return d

    # -- emit --------------------------------------------------------------

    def maybe_emit(
        self,
        mint: str,
        now_ms: Optional[int] = None,
        feed_buy_count_1000ms: int = 0,
        feed_sell_count_1000ms: int = 0,
        source_agreement_count: int = 0,
        feed_last_event_ts_ms: int = 0,
    ) -> Optional[dict[str, Any]]:
        snap = self.snapshot(
            mint,
            now_ms=now_ms,
            feed_buy_count_1000ms=feed_buy_count_1000ms,
            feed_sell_count_1000ms=feed_sell_count_1000ms,
            source_agreement_count=source_agreement_count,
            feed_last_event_ts_ms=feed_last_event_ts_ms,
        )
        if snap.get("n_points", 0) == 0:
            return None
        st = self._states.get(mint)
        if st is None:
            return None
        now_ts_ms = int(now_ms if now_ms is not None else _now_ms())
        if (now_ts_ms - st.last_emit_ts_ms) < self._emit_min_interval_ms:
            return snap
        st.last_emit_ts_ms = now_ts_ms
        try:
            short = _short(mint)
            self._logger(
                f"PGG2-V42-CURVE-DELTA-LEAD mint={short} "
                f"curve_price={snap['curve_price']:.12f} "
                f"streak={snap['positive_delta_streak']} "
                f"d_p1000={snap.get('curve_price_delta_1000ms', 0.0):+.12f} "
                f"d_p500={snap.get('curve_price_delta_500ms', 0.0):+.12f} "
                f"d_p250={snap.get('curve_price_delta_250ms', 0.0):+.12f} "
                f"vsol_d1000={snap.get('vsol_delta_1000ms', 0)} "
                f"vtok_d1000={snap.get('vtok_delta_1000ms', 0)} "
                f"q_out_d1000={snap.get('quote_out_delta_1000ms', 0.0):+.12f} "
                f"sell_grad={snap['sell_quote_gradient']:+.12f} "
                f"imb={snap['buy_sell_imbalance_1000ms']:+.3f} "
                f"agreement={snap['source_agreement_count']} "
                f"leading={int(bool(snap['curve_delta_is_leading']))} "
                f"rtt_p50={snap['source_latency_p50_ms']} "
                f"rtt_p95={snap['source_latency_p95_ms']} "
                f"age_ms={snap['curve_age_ms']} late={int(bool(snap['is_late']))}"
            )
            self._logger(
                f"PGG2-V42-CURVE-DELTA mint={short} "
                f"price={snap['curve_price']:.12f} "
                f"d_p_1000ms={snap.get('curve_price_delta_1000ms', 0.0):+.12f} "
                f"d_p_500ms={snap.get('curve_price_delta_500ms', 0.0):+.12f}"
            )
        except Exception:
            pass
        return snap


__all__ = [
    "CurveDeltaLeadOracle",
    "CurvePoint",
    "CURVE_WINDOWS_MS",
    "TEST_SELL_TOKENS_RAW",
]
