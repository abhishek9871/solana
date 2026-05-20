"""V42E — Quote-Confirmation Watchlist.

When the V42C accountSubscribe oracle emits the FIRST positive curve update
for a hot mint, V42D required a 2nd positive account-update within 750 ms.
V42E forensic (`V42D_OVERBLOCK_FORENSIC.md`) showed only 7.3 % of watchlist
ADD cycles resolved with such a 2nd update inside the TTL; 68.1 % expired
without a confirmation. The 2nd-update gate was the binding blocker.

V42E replaces it with a FAST QUOTE SNAPSHOT chain:

  - On the first positive curve update for a mint, add the mint to a
    `QuoteConfirmationWatchlist` with TTL=2000 ms.
  - Schedule snapshot capture at: 0, 150, 300, 500, 750, 1000, 1500, 2000 ms.
  - Each snapshot calls the broker quote helpers DIRECTLY:
        buy_tokens, buy_fees = broker.quote_pump_buy_tokens(...)
        sell_sol,  sell_fees = broker.quote_pump_sell_sol(buy_tokens, ...)
        all_in_pnl = (sell_sol - buy_fees - sell_fees) / LAMPORTS_PER_SOL
                     - amount_sol - 2 * tx_fee_sol
    `all_in_pnl` is the live-equivalent round-trip PnL on the CURRENT curve.
  - Track per-mint:
        snapshots: list[QuoteSnapshot]   # idx, age_ms, all_in_pnl, ...
        last_observed_negative_curve     # for the "forbid negative curve
                                           after watchlist start" rule
  - Emit:
        PGG2-V42E-QUOTE-WATCHLIST-ADD mint=... trigger_ts=...
        PGG2-V42E-QUOTE-CONFIRM-SNAPSHOT mint=... snapshot_idx=... age_ms=...
            all_in_pnl=... gradient=... stress=...
        PGG2-V42E-QUOTE-WATCHLIST-DROP mint=... reason=...

  - Hot-mint cap: PGG2_V42E_MAX_WATCHLIST (default 100).
  - Quote source: the broker's bonding_curve cache (live RPC at first call).
  - NO transactions, NO signing. Pure read-only quote math.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

LAMPORTS_PER_SOL = 1_000_000_000

# Default snapshot offsets in ms from trigger.
DEFAULT_SNAPSHOT_OFFSETS_MS = (0, 150, 300, 500, 750, 1000, 1500, 2000)

# Mean-reversion adverse mirror coefficient for stress quote.
DEFAULT_STRESS_MIRROR_COEF = 0.5

# Default tx-fee in SOL used in all-in PnL. Two transactions (buy + sell).
DEFAULT_TX_FEE_SOL = 0.000050


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _now_ms() -> int:
    return int(time.time() * 1000)


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


@dataclass
class QuoteSnapshot:
    idx: int
    taken_ts_ms: int
    age_ms: int  # ms from trigger_ts
    all_in_pnl: float
    sell_out_sol: float
    buy_tokens_raw: int
    quote_buy_latency_ms: int
    quote_sell_latency_ms: int
    quote_age_ms: int  # age of the curve account snapshot used (curve_age_ms)
    route: str
    sim_needed: int  # 0 if pure quote (pump_bc)
    pair_source: str  # "accountSubscribe" or "curve_cache"
    gradient: float  # delta vs previous snapshot's pnl / dt_seconds
    stress_pnl: float  # PnL after applying 0.5x adverse mirror sell
    is_late: bool
    error: str = ""


@dataclass
class WatchlistEntry:
    mint: str
    trigger_ts_ms: int
    last_curve_delta: float
    snapshots: list[QuoteSnapshot] = field(default_factory=list)
    pending_offsets_ms: list[int] = field(default_factory=list)
    saw_negative_curve_after_trigger: bool = False
    curve_was_unavailable_at_trigger: bool = False
    recovered_quote_seen: bool = False
    last_snapshot_pnl: Optional[float] = None
    dropped: bool = False
    drop_reason: str = ""


class QuoteConfirmationWatchlist:
    """In-process watchlist that drives the 8-snapshot quote chain."""

    def __init__(
        self,
        broker: Any,
        logger: Optional[Callable[[str], None]] = None,
        amount_sol: float = 0.015,
        ttl_ms: int = 2000,
        max_watchlist: Optional[int] = None,
        snapshot_offsets_ms: Optional[tuple[int, ...]] = None,
        tx_fee_sol: Optional[float] = None,
        stress_mirror_coef: Optional[float] = None,
    ) -> None:
        self._broker = broker
        self._log = logger or (lambda _msg: None)
        self._amount_sol = float(amount_sol)
        self._amount_lamports = int(round(self._amount_sol * LAMPORTS_PER_SOL))
        self._ttl_ms = int(ttl_ms)
        if max_watchlist is None:
            max_watchlist = _env_int("PGG2_V42E_MAX_WATCHLIST", 100)
        self._max_watchlist = max(1, int(max_watchlist))
        self._offsets = tuple(snapshot_offsets_ms or DEFAULT_SNAPSHOT_OFFSETS_MS)
        if tx_fee_sol is None:
            tx_fee_sol = _env_float("PGG2_V42E_TX_FEE_SOL", DEFAULT_TX_FEE_SOL)
        self._tx_fee_sol = float(tx_fee_sol)
        self._stress_coef = float(
            stress_mirror_coef
            if stress_mirror_coef is not None
            else _env_float("PGG2_V42E_STRESS_MIRROR_COEF", DEFAULT_STRESS_MIRROR_COEF)
        )
        self._entries: dict[str, WatchlistEntry] = {}
        self._stats = {
            "watchlist_adds": 0,
            "watchlist_drops_ttl": 0,
            "watchlist_drops_evicted": 0,
            "snapshots_taken": 0,
            "snapshots_failed": 0,
            "duplicate_adds_ignored": 0,
        }

    # ----- public surface -----------------------------------------------

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def watching(self) -> int:
        return len(self._entries)

    def has(self, mint: str) -> bool:
        return mint in self._entries

    def get(self, mint: str) -> Optional[WatchlistEntry]:
        return self._entries.get(mint)

    def add(
        self,
        mint: str,
        last_curve_delta: float,
        curve_snap_for_baseline: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Add a mint to the quote-confirmation watchlist on first positive
        curve update. Returns True if newly added.
        """
        if not mint:
            return False
        if mint in self._entries:
            self._stats["duplicate_adds_ignored"] += 1
            return False
        if len(self._entries) >= self._max_watchlist:
            # Evict the oldest entry.
            oldest = min(self._entries.items(), key=lambda kv: kv[1].trigger_ts_ms)[0]
            self._entries.pop(oldest, None)
            self._stats["watchlist_drops_evicted"] += 1
            try:
                self._log(
                    f"PGG2-V42E-QUOTE-WATCHLIST-DROP mint={_short(oldest)} "
                    f"reason=evicted_for_cap"
                )
            except Exception:
                pass
        trig = _now_ms()
        curve_unavailable = False
        if curve_snap_for_baseline is not None:
            try:
                vsol = int(curve_snap_for_baseline.get("virtual_sol_reserves", 0))
                vtok = int(curve_snap_for_baseline.get("virtual_token_reserves", 0))
                if vsol <= 0 or vtok <= 0:
                    curve_unavailable = True
            except Exception:
                curve_unavailable = True
        entry = WatchlistEntry(
            mint=mint,
            trigger_ts_ms=trig,
            last_curve_delta=float(last_curve_delta),
            pending_offsets_ms=list(self._offsets),
            curve_was_unavailable_at_trigger=curve_unavailable,
        )
        self._entries[mint] = entry
        self._stats["watchlist_adds"] += 1
        try:
            self._log(
                f"PGG2-V42E-QUOTE-WATCHLIST-ADD mint={_short(mint)} "
                f"trigger_ts={trig} last_delta={last_curve_delta:+.12f} "
                f"max_watchlist={self._max_watchlist} watching={len(self._entries)}"
            )
        except Exception:
            pass
        return True

    def mark_negative_curve(self, mint: str, delta: float) -> None:
        ent = self._entries.get(mint)
        if ent is None:
            return
        if delta < 0:
            ent.saw_negative_curve_after_trigger = True

    def mark_curve_recovery(self, mint: str) -> None:
        ent = self._entries.get(mint)
        if ent is None:
            return
        if ent.curve_was_unavailable_at_trigger:
            ent.recovered_quote_seen = True

    def expire_overdue(self, now_ms: Optional[int] = None) -> int:
        """Drop entries whose age > TTL. Returns count dropped."""
        if now_ms is None:
            now_ms = _now_ms()
        dropped = 0
        for m in list(self._entries.keys()):
            ent = self._entries[m]
            if (now_ms - ent.trigger_ts_ms) > self._ttl_ms:
                self._entries.pop(m, None)
                dropped += 1
                self._stats["watchlist_drops_ttl"] += 1
                try:
                    self._log(
                        f"PGG2-V42E-QUOTE-WATCHLIST-DROP mint={_short(m)} "
                        f"reason=ttl_expired snapshots={len(ent.snapshots)} "
                        f"age_ms={now_ms - ent.trigger_ts_ms}"
                    )
                except Exception:
                    pass
        return dropped

    def graduate(self, mint: str, reason: str = "rule_passed") -> None:
        ent = self._entries.pop(mint, None)
        if ent is None:
            return
        try:
            self._log(
                f"PGG2-V42E-QUOTE-WATCHLIST-DROP mint={_short(mint)} "
                f"reason={reason} snapshots={len(ent.snapshots)}"
            )
        except Exception:
            pass

    # ----- snapshot scheduler ------------------------------------------

    def due_snapshots(self, now_ms: Optional[int] = None) -> list[str]:
        """Return mints whose next pending snapshot offset has elapsed."""
        if now_ms is None:
            now_ms = _now_ms()
        out: list[str] = []
        for m, ent in self._entries.items():
            if not ent.pending_offsets_ms:
                continue
            elapsed = now_ms - ent.trigger_ts_ms
            if elapsed >= ent.pending_offsets_ms[0]:
                out.append(m)
        return out

    def take_snapshot(
        self,
        mint: str,
        curve_snap: Optional[dict[str, Any]],
    ) -> Optional[QuoteSnapshot]:
        """Capture a single quote snapshot at the next due offset.

        - Calls broker.quote_pump_buy_tokens + quote_pump_sell_sol DIRECTLY
          (no transaction sign or send).
        - Computes live_equiv_all_in_pnl from the round trip.
        - Computes stress_pnl by adversely mirroring last_curve_delta as a
          synthetic post-buy sell on the curve.
        - Records gradient vs previous snapshot.
        """
        ent = self._entries.get(mint)
        if ent is None or not ent.pending_offsets_ms:
            return None
        now = _now_ms()
        offset = ent.pending_offsets_ms.pop(0)
        # Build snapshot.
        try:
            from pgg2_direct_pump import as_pubkey, pda, PUMP_PROGRAM_ID, PumpBondingCurve  # type: ignore
        except Exception as exc:
            err = f"import_error:{type(exc).__name__}:{exc}"
            snap = self._failed_snapshot(ent, offset, now, err)
            ent.snapshots.append(snap)
            self._stats["snapshots_failed"] += 1
            self._emit_snapshot_log(ent, snap)
            return snap

        # Build curve override from the curve_snap if available; else use the
        # broker's RPC-cached bonding_curve.
        curve_obj: Optional[PumpBondingCurve] = None
        pair_source = "curve_cache"
        is_late = False
        quote_age_ms = 0
        if curve_snap is not None:
            try:
                vsol = int(curve_snap.get("virtual_sol_reserves", 0))
                vtok = int(curve_snap.get("virtual_token_reserves", 0))
                rsol = int(curve_snap.get("real_sol_reserves", 0))
                rtok = int(curve_snap.get("real_token_reserves", 0))
                is_late = bool(curve_snap.get("is_late", False))
                quote_age_ms = int(curve_snap.get("curve_age_ms", 0))
                if vsol > 0 and vtok > 0:
                    mint_pk = as_pubkey(mint)
                    curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint_pk))
                    curve_obj = PumpBondingCurve(
                        key=curve_key,
                        virtual_token_reserves=vtok,
                        virtual_sol_reserves=vsol,
                        real_token_reserves=rtok,
                        real_sol_reserves=rsol,
                        token_total_supply=0,
                        complete=False,
                        creator=curve_key,
                        is_mayhem=False,
                        cashback_enabled=False,
                    )
                    pair_source = "accountSubscribe"
            except Exception:
                curve_obj = None
        if curve_obj is None:
            try:
                mint_pk = as_pubkey(mint)
                curve_obj = self._broker.bonding_curve(mint_pk)
            except Exception as exc:
                err = f"bonding_curve_error:{type(exc).__name__}"
                snap = self._failed_snapshot(ent, offset, now, err)
                ent.snapshots.append(snap)
                self._stats["snapshots_failed"] += 1
                self._emit_snapshot_log(ent, snap)
                return snap

        try:
            global_cfg = self._broker.pump_global()
        except Exception as exc:
            err = f"pump_global_error:{type(exc).__name__}"
            snap = self._failed_snapshot(ent, offset, now, err)
            ent.snapshots.append(snap)
            self._stats["snapshots_failed"] += 1
            self._emit_snapshot_log(ent, snap)
            return snap

        # Quote BUY.
        buy_ts0 = _now_ms()
        try:
            buy_tokens, buy_fees = self._broker.quote_pump_buy_tokens(
                self._amount_lamports, curve_obj, global_cfg
            )
        except Exception as exc:
            err = f"quote_buy_error:{type(exc).__name__}"
            snap = self._failed_snapshot(ent, offset, now, err)
            ent.snapshots.append(snap)
            self._stats["snapshots_failed"] += 1
            self._emit_snapshot_log(ent, snap)
            return snap
        buy_ts1 = _now_ms()
        # Quote SELL (round trip).
        sell_ts0 = _now_ms()
        try:
            sell_out_lamports, sell_fees = self._broker.quote_pump_sell_sol(
                int(buy_tokens), curve_obj, global_cfg
            )
        except Exception as exc:
            err = f"quote_sell_error:{type(exc).__name__}"
            snap = self._failed_snapshot(ent, offset, now, err)
            ent.snapshots.append(snap)
            self._stats["snapshots_failed"] += 1
            self._emit_snapshot_log(ent, snap)
            return snap
        sell_ts1 = _now_ms()
        sell_out_sol = float(sell_out_lamports) / LAMPORTS_PER_SOL
        # all_in_pnl = (sell_out - amount_sol) - 2*tx_fee.  Fees on buy already
        # netted into the buy_tokens; on sell already netted into sell_out.
        all_in_pnl = sell_out_sol - self._amount_sol - (2.0 * self._tx_fee_sol)

        # Stress: simulate an adverse mirror (0.5x of last_curve_delta as a
        # SELL) eating into our position before we sell.
        stress_pnl = all_in_pnl
        try:
            stress_curve = self._apply_adverse_mirror(curve_obj, ent.last_curve_delta)
            if stress_curve is not None:
                # Recompute sell at the adverse curve using the same tokens we
                # would have bought.
                adv_sell, _adv_fees = self._broker.quote_pump_sell_sol(
                    int(buy_tokens), stress_curve, global_cfg
                )
                adv_sell_sol = float(adv_sell) / LAMPORTS_PER_SOL
                stress_pnl = adv_sell_sol - self._amount_sol - (2.0 * self._tx_fee_sol)
        except Exception:
            stress_pnl = all_in_pnl

        # Gradient vs previous snapshot.
        gradient = 0.0
        if ent.snapshots:
            prev = ent.snapshots[-1]
            dt_s = max(0.001, (now - prev.taken_ts_ms) / 1000.0)
            gradient = (all_in_pnl - prev.all_in_pnl) / dt_s

        snap = QuoteSnapshot(
            idx=len(ent.snapshots),
            taken_ts_ms=now,
            age_ms=int(max(0, now - ent.trigger_ts_ms)),
            all_in_pnl=float(all_in_pnl),
            sell_out_sol=float(sell_out_sol),
            buy_tokens_raw=int(buy_tokens),
            quote_buy_latency_ms=int(max(0, buy_ts1 - buy_ts0)),
            quote_sell_latency_ms=int(max(0, sell_ts1 - sell_ts0)),
            quote_age_ms=int(quote_age_ms),
            route="pump_bc",
            sim_needed=0,
            pair_source=pair_source,
            gradient=float(gradient),
            stress_pnl=float(stress_pnl),
            is_late=bool(is_late),
        )
        ent.snapshots.append(snap)
        ent.last_snapshot_pnl = float(all_in_pnl)
        if ent.curve_was_unavailable_at_trigger and pair_source == "accountSubscribe":
            ent.recovered_quote_seen = True
        self._stats["snapshots_taken"] += 1
        self._emit_snapshot_log(ent, snap)
        return snap

    # ----- internal helpers ---------------------------------------------

    def _apply_adverse_mirror(self, curve_obj: Any, last_delta: float) -> Optional[Any]:
        """Build a synthetic stressed curve by removing 0.5*|last_delta|*vSOL
        from virtual_sol_reserves (simulating a small mean-reversion sell
        happening between our buy and our sell).
        """
        try:
            from pgg2_direct_pump import PumpBondingCurve  # type: ignore
        except Exception:
            return None
        try:
            vsol = int(curve_obj.virtual_sol_reserves)
            vtok = int(curve_obj.virtual_token_reserves)
            if vsol <= 0 or vtok <= 0:
                return None
            # If last_delta is positive, mirror it: 0.5x the relative price
            # move down. Approximate vsol reduction by inverting CPMM math.
            # We'll simulate adversity as a vSOL reduction of:
            #   delta_lamports = 0.5 * |last_delta_price * vsol| (conservative)
            # but to keep this simple and not double-count, use:
            #   adverse_vsol_reduction = 0.5 * vsol_delta_implied
            # where vsol_delta_implied = |last_delta_price * vsol|.
            implied = abs(float(last_delta) * float(vsol))
            adverse = int(implied * self._stress_coef)
            if adverse <= 0:
                return None
            new_vsol = max(1, vsol - adverse)
            new_vtok = int(vtok + (vtok * adverse) // max(1, vsol))
            return PumpBondingCurve(
                key=curve_obj.key,
                virtual_token_reserves=new_vtok,
                virtual_sol_reserves=new_vsol,
                real_token_reserves=int(curve_obj.real_token_reserves),
                real_sol_reserves=int(curve_obj.real_sol_reserves),
                token_total_supply=int(getattr(curve_obj, "token_total_supply", 0) or 0),
                complete=bool(getattr(curve_obj, "complete", False)),
                creator=curve_obj.creator,
                is_mayhem=bool(getattr(curve_obj, "is_mayhem", False)),
                cashback_enabled=bool(getattr(curve_obj, "cashback_enabled", False)),
            )
        except Exception:
            return None

    def _failed_snapshot(
        self, ent: WatchlistEntry, offset: int, now_ms: int, err: str
    ) -> QuoteSnapshot:
        return QuoteSnapshot(
            idx=len(ent.snapshots),
            taken_ts_ms=now_ms,
            age_ms=int(max(0, now_ms - ent.trigger_ts_ms)),
            all_in_pnl=-999.0,
            sell_out_sol=0.0,
            buy_tokens_raw=0,
            quote_buy_latency_ms=0,
            quote_sell_latency_ms=0,
            quote_age_ms=0,
            route="pump_bc",
            sim_needed=0,
            pair_source="error",
            gradient=0.0,
            stress_pnl=-999.0,
            is_late=False,
            error=str(err),
        )

    def _emit_snapshot_log(self, ent: WatchlistEntry, snap: QuoteSnapshot) -> None:
        try:
            self._log(
                f"PGG2-V42E-QUOTE-CONFIRM-SNAPSHOT mint={_short(ent.mint)} "
                f"snapshot_idx={snap.idx} age_ms={snap.age_ms} "
                f"all_in_pnl={snap.all_in_pnl:+.6f} "
                f"gradient={snap.gradient:+.6f} "
                f"stress={snap.stress_pnl:+.6f} "
                f"quote_age_ms={snap.quote_age_ms} "
                f"sell_quote_lat_ms={snap.quote_sell_latency_ms} "
                f"buy_lat_ms={snap.quote_buy_latency_ms} "
                f"route={snap.route} sim_needed={snap.sim_needed} "
                f"src={snap.pair_source} late={int(snap.is_late)} "
                f"err={snap.error or '-'}"
            )
        except Exception:
            pass


async def watchlist_drive_loop(
    watchlist: QuoteConfirmationWatchlist,
    curve_snapshot_fn: Callable[[str], Optional[dict[str, Any]]],
    stop_event: asyncio.Event,
    tick_ms: int = 25,
) -> None:
    """Async loop that takes due snapshots and expires entries."""
    while not stop_event.is_set():
        try:
            due = watchlist.due_snapshots()
            for mint in due:
                cs = curve_snapshot_fn(mint)
                watchlist.take_snapshot(mint, cs)
            watchlist.expire_overdue()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick_ms / 1000.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            return


__all__ = [
    "QuoteConfirmationWatchlist",
    "QuoteSnapshot",
    "WatchlistEntry",
    "DEFAULT_SNAPSHOT_OFFSETS_MS",
    "watchlist_drive_loop",
]
