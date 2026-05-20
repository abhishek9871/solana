"""V57 near-miss watchlist.

Stores V67 near-miss candidates emitted from v48 harness V67 early-block points.
Each entry has a TTL (default 3000ms). Promotion engine consults this watchlist
on every local curve update and decides whether to promote the candidate for
live entry.

Spec admit rule:
- route=pump_bc
- sim_needed=0
- pair_source in {current_sig, cache, observed, prewarmed}
- best_expected_pnl is near threshold:
    required_pnl - 0.00045 <= best_expected_pnl < required_pnl
    OR best_expected_pnl >= -0.00025
- not single-buyer
- not Token-2022 legacy path
- not stale

Spec drop rule:
- TTL exceeded (3000ms default)
- first hard negative curve reversal
- local quote remains negative after TTL

Logs (per spec):
  PGG2-V57-NEARMISS-SEEN
  PGG2-V57-WATCHLIST-ADD
  PGG2-V57-WATCHLIST-DROP
  PGG2-V57-WATCHLIST-REFRESH
"""
from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class V67NearMiss:
    """One V67 early-block event with full state snapshot."""
    mint: str
    ts_ms: int
    best_expected_pnl: float
    required_pnl: float
    selected_size: float
    size_tier_results: dict[float, dict[str, Any]] = field(default_factory=dict)
    route: str = ""  # pump_bc / pumpswap / ...
    sim_needed: int = 0
    pair_source: str = ""  # current_sig, cache, observed, prewarmed, ...
    # V47C multi-buyer fields
    unique_buyers_250ms: int = 0
    unique_buyers_500ms: int = 0
    top_buyer_share_250ms: float = 0.0
    pending_buy_sol_250ms: float = 0.0
    pending_buy_count_250ms: int = 0
    pending_buy_sol_1000ms: float = 0.0
    pending_buy_count_1000ms: int = 0
    # Pending flow / curve state
    vsol_now: float = 0.0
    vtok_now: float = 0.0
    price_now: float = 0.0
    # V47C/D/E/F fields
    multi_buyer_passed: bool = False
    boundary_passed: bool = False
    concentration_ok: bool = False
    edge_floor_ok: bool = False
    source_lead_ms: int = 0
    # V47H ratio if available
    v47h_ratio: Optional[float] = None
    # Block reason
    blocker_reason: str = ""
    blocker_detail: str = ""
    # Token-2022 marker if known
    is_token_2022: Optional[bool] = None


@dataclass
class WatchlistEntry:
    nm: V67NearMiss
    added_ts_ms: int
    last_refreshed_ms: int
    # Snapshots taken on curve updates after add
    quote_history: list[tuple[int, float, float]] = field(default_factory=list)
    # ^ list of (ts_ms, buy_quote_sol, sell_quote_sol)
    consecutive_negative_curve: int = 0
    promoted: bool = False
    dropped: bool = False
    drop_reason: str = ""


class V57NearMissWatchlist:
    def __init__(self) -> None:
        self.enabled = _envb("PGG2_V57_WATCHLIST_ENABLED", True)
        self.ttl_ms = _envi("PGG2_V57_WATCHLIST_TTL_MS", 3000)
        self.band_below = _envf("PGG2_V57_WATCHLIST_BAND_BELOW_SOL", 0.00045)
        self.absolute_floor = _envf("PGG2_V57_WATCHLIST_ABSOLUTE_FLOOR_SOL", -0.00025)
        self.required_pnl_default = _envf("PGG2_V67_MIN_EXPECTED_PNL", 0.001500)
        # Max simultaneously-watched mints (memory + CPU bound)
        self.max_entries = _envi("PGG2_V57_WATCHLIST_MAX_ENTRIES", 50)
        # Hard-block: stale-ms after which a candidate is too old to accept
        self.max_admit_age_ms = _envi("PGG2_V57_WATCHLIST_MAX_ADMIT_AGE_MS", 1500)
        # Hard reject: consecutive negative curve updates
        self.max_consec_neg_curve = _envi("PGG2_V57_WATCHLIST_MAX_CONSEC_NEG_CURVE", 1)
        # Drop if quote still negative after this many ms post-add
        self.quote_grace_ms = _envi("PGG2_V57_WATCHLIST_QUOTE_GRACE_MS", 1500)

        self._entries: dict[str, WatchlistEntry] = {}
        self._lock = threading.RLock()
        # Subscribers notified on add/drop/promote
        self._on_add: list[Callable[[WatchlistEntry], None]] = []
        self._on_drop: list[Callable[[WatchlistEntry], None]] = []
        # Counters for observe report
        self.stats = {
            "seen": 0,
            "added": 0,
            "rejected_admit": 0,
            "dropped_ttl": 0,
            "dropped_negative_curve": 0,
            "dropped_quote_grace": 0,
            "dropped_external": 0,
            "promoted": 0,
        }

    # ---- subscribers ----
    def on_add(self, cb: Callable[[WatchlistEntry], None]) -> None:
        self._on_add.append(cb)

    def on_drop(self, cb: Callable[[WatchlistEntry], None]) -> None:
        self._on_drop.append(cb)

    # ---- admit ----
    def _short(self, mint: str) -> str:
        return mint[:4] + ".." + mint[-4:] if len(mint) > 10 else mint

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _admit_check(self, nm: V67NearMiss, log_fn: Callable[[str], None]) -> tuple[bool, str]:
        if not self.enabled:
            return False, "watchlist_disabled"
        if nm.route != "pump_bc":
            return False, f"route_not_pump_bc({nm.route})"
        if nm.sim_needed != 0:
            return False, f"sim_needed({nm.sim_needed})"
        # pair_source check: only enforced if v48 actually populates it.
        # When empty (v48 doesn't expose it in snap), treat as unknown-OK.
        if nm.pair_source and nm.pair_source not in {"current_sig", "cache", "observed", "prewarmed"}:
            return False, f"pair_source({nm.pair_source})"
        # Band check
        req = nm.required_pnl if nm.required_pnl > 0 else self.required_pnl_default
        ep = nm.best_expected_pnl
        in_band_below = (req - self.band_below) <= ep < req
        in_absolute_floor = ep >= self.absolute_floor
        if not (in_band_below or in_absolute_floor):
            return False, f"ep_not_near({ep:+.6f}<{req-self.band_below:+.6f} and {ep:+.6f}<{self.absolute_floor:+.6f})"
        # V47C single-buyer reject
        if nm.unique_buyers_250ms <= 1 and nm.unique_buyers_500ms <= 1:
            return False, "single_buyer"
        # Catastrophic V47C fail: top buyer share > 0.85 means one wallet owns it
        if nm.top_buyer_share_250ms > 0.85:
            return False, f"v47c_catastrophic(top={nm.top_buyer_share_250ms:.2f})"
        # Token-2022 legacy path — only allow if we have v2 routing wired
        if nm.is_token_2022 and not _envb("PGG2_V57_ALLOW_T22", False):
            return False, "token_2022_legacy_blocked"
        # Staleness
        age = self._now_ms() - nm.ts_ms
        if age > self.max_admit_age_ms:
            return False, f"stale({age}ms>{self.max_admit_age_ms})"
        return True, ""

    def admit(self, nm: V67NearMiss, log_fn: Callable[[str], None] = print) -> Optional[WatchlistEntry]:
        with self._lock:
            self.stats["seen"] += 1
            sh = self._short(nm.mint)

            # PGG2-V57-NEARMISS-SEEN always logs
            log_fn(
                f"PGG2-V57-NEARMISS-SEEN {sh} ep={nm.best_expected_pnl:+.6f} "
                f"req={nm.required_pnl:+.6f} size={nm.selected_size:.4f} "
                f"route={nm.route} sim_needed={nm.sim_needed} pair_src={nm.pair_source} "
                f"ub250={nm.unique_buyers_250ms} top250={nm.top_buyer_share_250ms:.3f} "
                f"blocker={nm.blocker_reason}"
            )

            ok, reason = self._admit_check(nm, log_fn)
            if not ok:
                self.stats["rejected_admit"] += 1
                log_fn(f"PGG2-V57-WATCHLIST-REJECT {sh} reason={reason}")
                return None

            # If duplicate, refresh
            if nm.mint in self._entries:
                self._entries[nm.mint].nm = nm
                self._entries[nm.mint].last_refreshed_ms = self._now_ms()
                log_fn(
                    f"PGG2-V57-WATCHLIST-REFRESH {sh} ep={nm.best_expected_pnl:+.6f} "
                    f"size={nm.selected_size:.4f}"
                )
                return self._entries[nm.mint]

            # Capacity
            if len(self._entries) >= self.max_entries:
                # Evict oldest non-promoted entry
                oldest_mint = None
                oldest_ts = self._now_ms() + 1
                for m, e in self._entries.items():
                    if not e.promoted and e.added_ts_ms < oldest_ts:
                        oldest_ts = e.added_ts_ms
                        oldest_mint = m
                if oldest_mint:
                    self._drop(oldest_mint, "evicted_for_capacity", log_fn)

            entry = WatchlistEntry(
                nm=nm, added_ts_ms=self._now_ms(),
                last_refreshed_ms=self._now_ms(),
            )
            self._entries[nm.mint] = entry
            self.stats["added"] += 1
            log_fn(
                f"PGG2-V57-WATCHLIST-ADD {sh} ep={nm.best_expected_pnl:+.6f} "
                f"size={nm.selected_size:.4f} ub250={nm.unique_buyers_250ms} "
                f"top250={nm.top_buyer_share_250ms:.3f} pending_buy_sol={nm.pending_buy_sol_1000ms:.3f} "
                f"watchlist_size={len(self._entries)}"
            )
            for cb in self._on_add:
                try:
                    cb(entry)
                except Exception as e:  # noqa: BLE001
                    log_fn(f"PGG2-V57-WATCHLIST-CALLBACK-ERR err={type(e).__name__}:{e}")
            return entry

    # ---- drop ----
    def _drop(self, mint: str, reason: str, log_fn: Callable[[str], None]) -> None:
        e = self._entries.pop(mint, None)
        if e is None:
            return
        e.dropped = True
        e.drop_reason = reason
        sh = self._short(mint)
        log_fn(f"PGG2-V57-WATCHLIST-DROP {sh} reason={reason} age_ms={self._now_ms()-e.added_ts_ms}")
        if reason.startswith("ttl"):
            self.stats["dropped_ttl"] += 1
        elif reason.startswith("negative_curve"):
            self.stats["dropped_negative_curve"] += 1
        elif reason.startswith("quote_grace"):
            self.stats["dropped_quote_grace"] += 1
        else:
            self.stats["dropped_external"] += 1
        for cb in self._on_drop:
            try:
                cb(e)
            except Exception as ex:  # noqa: BLE001
                log_fn(f"PGG2-V57-WATCHLIST-DROP-CALLBACK-ERR err={type(ex).__name__}:{ex}")

    def drop(self, mint: str, reason: str, log_fn: Callable[[str], None] = print) -> None:
        with self._lock:
            self._drop(mint, reason, log_fn)

    def mark_promoted(self, mint: str) -> None:
        with self._lock:
            e = self._entries.get(mint)
            if e and not e.promoted:
                e.promoted = True
                self.stats["promoted"] += 1

    # ---- maintenance ----
    def sweep(self, log_fn: Callable[[str], None] = print) -> None:
        """Drop expired entries. Call this periodically."""
        with self._lock:
            now = self._now_ms()
            for mint, e in list(self._entries.items()):
                age = now - e.added_ts_ms
                # TTL
                if age > self.ttl_ms:
                    self._drop(mint, f"ttl_{age}ms>{self.ttl_ms}", log_fn)
                    continue
                # Quote grace period: if quote_history has any entry and the latest
                # is still negative after grace_ms, drop.
                if e.quote_history and age > self.quote_grace_ms:
                    last_ts, last_buy, last_sell = e.quote_history[-1]
                    # net = sell - buy approximates one-step PnL on a probe size
                    if last_sell - last_buy < 0.0:
                        self._drop(mint, f"quote_grace_{age}ms_net={(last_sell-last_buy):+.6f}", log_fn)
                        continue

    def on_curve_update(
        self,
        mint: str,
        new_buy_quote_sol: float,
        new_sell_quote_sol: float,
        is_curve_delta_negative: bool,
        log_fn: Callable[[str], None] = print,
    ) -> Optional[WatchlistEntry]:
        """Record a curve update for a watched mint. Drops on negative reversal."""
        with self._lock:
            e = self._entries.get(mint)
            if e is None:
                return None
            now = self._now_ms()
            e.quote_history.append((now, new_buy_quote_sol, new_sell_quote_sol))
            if is_curve_delta_negative:
                e.consecutive_negative_curve += 1
                if e.consecutive_negative_curve >= self.max_consec_neg_curve:
                    self._drop(
                        mint,
                        f"negative_curve_reversal_{e.consecutive_negative_curve}consec",
                        log_fn,
                    )
                    return None
            else:
                e.consecutive_negative_curve = 0
            return e

    def get_active(self) -> list[WatchlistEntry]:
        with self._lock:
            return [e for e in self._entries.values() if not e.dropped]

    def get(self, mint: str) -> Optional[WatchlistEntry]:
        with self._lock:
            return self._entries.get(mint)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)


_SINGLETON: Optional[V57NearMissWatchlist] = None


def get_watchlist() -> V57NearMissWatchlist:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V57NearMissWatchlist()
    return _SINGLETON


if __name__ == "__main__":
    # Quick self-test
    wl = get_watchlist()
    print(f"watchlist created: enabled={wl.enabled} ttl_ms={wl.ttl_ms}")
    print(f"band_below={wl.band_below} absolute_floor={wl.absolute_floor}")

    nm_pass = V67NearMiss(
        mint="TESTpump" + "A" * 36, ts_ms=int(time.time() * 1000),
        best_expected_pnl=-0.00012,  # well above -0.00025 floor
        required_pnl=0.00150,
        selected_size=0.005,
        route="pump_bc", sim_needed=0, pair_source="current_sig",
        unique_buyers_250ms=3, top_buyer_share_250ms=0.20,
        pending_buy_sol_1000ms=0.5,
        blocker_reason="no_selectable_size",
    )
    e1 = wl.admit(nm_pass)
    print(f"\n--> admit result: entry={e1 is not None}, size={wl.size()}")

    nm_fail_single = V67NearMiss(
        mint="TESTpump" + "B" * 36, ts_ms=int(time.time() * 1000),
        best_expected_pnl=-0.00012, required_pnl=0.00150, selected_size=0.005,
        route="pump_bc", sim_needed=0, pair_source="current_sig",
        unique_buyers_250ms=1, top_buyer_share_250ms=0.99,
        blocker_reason="no_selectable_size",
    )
    e2 = wl.admit(nm_fail_single)
    print(f"\n--> single-buyer reject: entry={e2 is None}")

    nm_fail_band = V67NearMiss(
        mint="TESTpump" + "C" * 36, ts_ms=int(time.time() * 1000),
        best_expected_pnl=-0.00200, required_pnl=0.00150, selected_size=0.005,
        route="pump_bc", sim_needed=0, pair_source="current_sig",
        unique_buyers_250ms=3, top_buyer_share_250ms=0.20,
        blocker_reason="no_selectable_size",
    )
    e3 = wl.admit(nm_fail_band)
    print(f"\n--> band reject: entry={e3 is None}")

    print(f"\nstats={wl.stats}")
