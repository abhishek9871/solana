"""Unified adaptive dry sniper.

Dry-only. This never sends Binance orders.

The bot learns and trades in one WebSocket loop:
- every qualifying signal starts a shadow probe;
- recent probe outcomes score each symbol+side in real time;
- only currently positive pairs may open a paper trade;
- entries can use a short maker-first window before falling back to taker.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from sniper_paper_bot import SKIP, SymbolState, TAKER_FEE, fmt, qdown, select_symbols
from trading_bot.binance_client import BinanceApiError, BinanceFuturesClient


MAKER_FEE = Decimal("0.0002")


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


@dataclass
class FusionPosition:
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_fee: Decimal
    entry_fee_rate: Decimal
    entry_ts: float
    best_price: Decimal
    best_net: Decimal = Decimal("-999999")
    trail: Decimal | None = None
    mode: str = "follow"


@dataclass
class ShadowProbe:
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_fee: Decimal
    entry_ts: float
    v1: float
    v3: float
    f1: float
    f3: float
    n3: Decimal
    book: float
    best_net: Decimal = Decimal("-999999")
    worst_net: Decimal = Decimal("999999")
    closed: bool = False
    reason: str = ""
    net: Decimal = Decimal("0")
    mode: str = "follow"


@dataclass
class PendingEntry:
    symbol: str
    side: str
    limit_price: Decimal
    qty: Decimal
    stop: Decimal
    target: Decimal
    created_ts: float
    deadline_ts: float
    fallback_ts: float
    features: dict[str, Any]


@dataclass
class PairScore:
    symbol: str
    side: str
    mode: str
    results: deque[Decimal]
    wins: int = 0
    losses: int = 0
    last_ts: float = 0.0

    def add(self, net: Decimal, ts: float) -> None:
        if len(self.results) == self.results.maxlen:
            old = self.results[0]
            if old > 0:
                self.wins -= 1
            else:
                self.losses -= 1
        self.results.append(net)
        if net > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.last_ts = ts

    @property
    def net(self) -> Decimal:
        return sum(self.results, Decimal("0"))

    @property
    def count(self) -> int:
        return len(self.results)

    @property
    def best(self) -> Decimal:
        return max(self.results, default=Decimal("0"))

    @property
    def worst(self) -> Decimal:
        return min(self.results, default=Decimal("0"))

    @property
    def win_rate(self) -> float:
        return self.wins / self.count if self.count else 0.0


class FusionAdaptiveBot:
    def __init__(self, args: argparse.Namespace, symbols: list[str], out_path: Path) -> None:
        self.args = args
        self.symbols = symbols
        self.states = {s: SymbolState(s) for s in symbols}
        self.wallet: Decimal = args.wallet
        self.start_wallet: Decimal = args.wallet
        self.position: FusionPosition | None = None
        self.pending: PendingEntry | None = None
        self.probes: list[ShadowProbe] = []
        self.scores: dict[tuple[str, str, str], PairScore] = {}
        self.bad_until: dict[tuple[str, str], float] = {}
        self.mode_cooldown_until: dict[tuple[str, str, str], float] = {}
        self.last_probe_ts: dict[tuple[str, str], float] = {}
        self.trades = 0
        self.wins = 0
        self.stop = False
        self.last_status = 0.0
        self.last_entry_ts = 0.0
        self.out_path = out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

    def set_filters(self, client: BinanceFuturesClient) -> None:
        info = client.get_exchange_info()
        for sym in info.get("symbols", []):
            name = sym.get("symbol")
            if name not in self.states:
                continue
            st = self.states[name]
            for f in sym.get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    st.tick_size = Decimal(f.get("tickSize", "0.000001"))
                elif f.get("filterType") == "LOT_SIZE":
                    st.step_size = Decimal(f.get("stepSize", "1"))

    def write_event(self, event: str, payload: dict[str, Any]) -> None:
        def clean(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, deque):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {str(key): clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            return value

        row = clean({"ts": time.time(), "event": event, **payload})
        with self.out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def exit_price(self, st: SymbolState, side: str) -> Decimal:
        return st.bid if side == "LONG" else st.ask

    def net_at(self, side: str, qty: Decimal, entry: Decimal, entry_fee: Decimal, exit_price: Decimal) -> Decimal:
        gross = (exit_price - entry) * qty if side == "LONG" else (entry - exit_price) * qty
        return gross - entry_fee - exit_price * qty * TAKER_FEE

    def equity(self) -> Decimal:
        if not self.position:
            return self.wallet
        st = self.states[self.position.symbol]
        px = self.exit_price(st, self.position.side)
        if px <= 0:
            return self.wallet
        gross = (px - self.position.entry) * self.position.qty if self.position.side == "LONG" else (self.position.entry - px) * self.position.qty
        return self.wallet + gross

    def compute_order(self, st: SymbolState, side: str, entry: Decimal, fee_rate: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if side == "LONG":
            stop = qdown(entry * (Decimal("1") - self.args.sl_pct), st.tick_size)
            if stop >= entry:
                stop = entry - st.tick_size
            target = qdown(entry * (Decimal("1") + self.args.tp_pct), st.tick_size)
            if target <= entry:
                target = entry + st.tick_size
            stop_move = (entry - stop) / entry
        else:
            stop = qdown(entry * (Decimal("1") + self.args.sl_pct), st.tick_size)
            if stop <= entry:
                stop = entry + st.tick_size
            target = qdown(entry * (Decimal("1") - self.args.tp_pct), st.tick_size)
            if target >= entry:
                target = entry - st.tick_size
            stop_move = (stop - entry) / entry
        risk_per_notional = stop_move + fee_rate + TAKER_FEE
        max_notional = self.wallet * self.args.margin_fraction * Decimal(self.args.leverage)
        if self.args.max_stop_risk_usdt > 0:
            max_notional = min(max_notional, self.args.max_stop_risk_usdt / risk_per_notional)
        qty = qdown(max_notional / entry, st.step_size)
        fee = qty * entry * fee_rate
        return qty, stop, target, fee

    def signal(self, st: SymbolState) -> tuple[str | None, dict[str, Any]]:
        now = time.time()
        features: dict[str, Any] = {}
        if not st.first_tick_ts or now - st.first_tick_ts < 5:
            return None, features
        if not st.first_trade_ts or now - st.first_trade_ts < 3:
            return None, features
        if not st.last_book_ts or now - st.last_book_ts > 1.5:
            return None, features
        if st.spread_pct() > self.args.max_spread_pct or len(st.ticks) < 6:
            return None, features
        v1 = st.velocity_pct(1.0)
        v3 = st.velocity_pct(3.0)
        v5 = st.velocity_pct(5.0)
        f1, n1 = st.flow_ratio(1.0)
        f3, n3 = st.flow_ratio(3.0)
        book = st.book_imbalance()
        liq3, lq3 = st.liquidation_ratio(3.0)
        features = {"v1": v1, "v3": v3, "v5": v5, "f1": f1, "f3": f3, "n1": n1, "n3": n3, "book": book, "liq3": liq3, "lq3": lq3}
        if n3 < self.args.min_flow_notional:
            st.signal_streak = 0
            return None, features
        side: str | None = None
        if v1 >= self.args.min_v1_pct and v3 >= self.args.min_v3_pct and f1 >= self.args.min_f1 and f3 >= self.args.min_f3:
            side = "LONG"
        elif v1 <= -self.args.min_v1_pct and v3 <= -self.args.min_v3_pct and f1 <= -self.args.min_f1 and f3 <= -self.args.min_f3:
            side = "SHORT"
        if not side:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return None, features
        direction = 1 if side == "LONG" else -1
        if direction * v5 < self.args.min_v3_pct * 0.25:
            return None, features
        if direction * book < self.args.min_book_imbalance and not (lq3 >= Decimal("2500") and direction * liq3 >= 0.45):
            return None, features
        if lq3 >= Decimal("5000") and direction * liq3 <= -0.35:
            return None, features
        if st.last_signal_side == side and now - st.last_signal_ts <= 1.0:
            if st.signal_streak == 0:
                st.signal_first_ts = now
            st.signal_streak += 1
        else:
            st.signal_streak = 1
            st.signal_first_ts = now
        st.last_signal_side = side
        st.last_signal_ts = now
        if st.signal_streak < self.args.required_streak or now - st.signal_first_ts < self.args.required_age_sec:
            return None, features
        return side, features

    def ensure_score(self, symbol: str, side: str, mode: str = "follow") -> PairScore:
        key = (symbol, side, mode)
        if key not in self.scores:
            self.scores[key] = PairScore(symbol, side, mode, deque(maxlen=self.args.score_window))
        return self.scores[key]

    def pair_allowed(self, symbol: str, side: str, mode: str, best_override: Decimal | None = None) -> bool:
        if time.time() < self.bad_until.get((symbol, side), 0):
            return False
        score = self.scores.get((symbol, side, mode))
        if not score:
            return False
        if score.count < self.args.min_probe_count:
            return False
        if score.net < self.args.min_pair_net:
            return False
        best_seen = max(score.best, best_override or Decimal("0"))
        if best_seen < self.args.min_best_net:
            return False
        if score.losses > self.args.max_pair_losses:
            return False
        if score.win_rate < self.args.min_pair_win_rate:
            return False
        return True

    def open_probe(self, st: SymbolState, side: str, features: dict[str, Any]) -> None:
        now = time.time()
        if now < self.bad_until.get((st.symbol, side), 0):
            return
        key = (st.symbol, side)
        if now - self.last_probe_ts.get(key, 0.0) < self.args.probe_cooldown_sec:
            return
        if any(not p.closed and p.symbol == st.symbol and p.side == side for p in self.probes):
            return
        entry = st.ask if side == "LONG" else st.bid
        if entry <= 0:
            return
        qty, stop, target, fee = self.compute_order(st, side, entry, TAKER_FEE)
        if qty * entry < Decimal("5"):
            return
        probe = ShadowProbe(
            st.symbol,
            side,
            qty,
            entry,
            stop,
            target,
            fee,
            time.time(),
            features.get("v1", 0.0),
            features.get("v3", 0.0),
            features.get("f1", 0.0),
            features.get("f3", 0.0),
            features.get("n3", Decimal("0")),
            features.get("book", 0.0),
            mode=features.get("mode", "follow"),
        )
        self.last_probe_ts[key] = now
        self.probes.append(probe)
        self.write_event("probe_open", asdict(probe))
        log(
            f"LEARN open {probe.mode:<6} {st.symbol:<14} {side:<5} "
            f"entry={fmt(entry):>10} n3=${fmt(features.get('n3', 0)):<10} book={features.get('book', 0):+.2f}"
        )

    def current_features(self, st: SymbolState) -> dict[str, Any]:
        f1, n1 = st.flow_ratio(1.0)
        f3, n3 = st.flow_ratio(3.0)
        liq3, lq3 = st.liquidation_ratio(3.0)
        return {
            "v1": st.velocity_pct(1.0),
            "v3": st.velocity_pct(3.0),
            "v5": st.velocity_pct(5.0),
            "f1": f1,
            "f3": f3,
            "n1": n1,
            "n3": n3,
            "book": st.book_imbalance(),
            "liq3": liq3,
            "lq3": lq3,
        }

    def entry_from_probe_win(self, probe: ShadowProbe) -> None:
        if not self.args.entry_on_probe_win:
            return
        strong_horizon = (
            self.args.entry_on_horizon_win
            and probe.reason == "horizon"
            and probe.net > 0
            and probe.best_net >= self.args.min_best_net
        )
        if probe.reason not in {"net-profit", "target", "net-trail"} and not strong_horizon:
            return
        if time.time() - probe.entry_ts > self.args.win_entry_max_age_sec:
            return
        if self.position or self.pending:
            return
        st = self.states[probe.symbol]
        if st.spread_pct() > self.args.max_spread_pct:
            return
        features = self.current_features(st)
        features["mode"] = probe.mode
        features["entry_source"] = "probe-win"
        direction = 1 if probe.side == "LONG" else -1
        if direction * features.get("book", 0.0) > self.args.max_probe_win_dir_book:
            return
        if direction * features["v1"] < -self.args.max_win_reversal_pct:
            return
        self.start_entry(st, probe.side, features, best_override=probe.best_net)

    def update_probe(self, probe: ShadowProbe) -> None:
        if probe.closed:
            return
        st = self.states[probe.symbol]
        px = self.exit_price(st, probe.side)
        if px <= 0:
            return
        net = self.net_at(probe.side, probe.qty, probe.entry, probe.entry_fee, px)
        probe.best_net = max(probe.best_net, net)
        probe.worst_net = min(probe.worst_net, net)
        reason = ""
        if self.args.snap_profit_usdt > 0 and net >= self.args.snap_profit_usdt:
            reason = "net-profit"
        elif (
            self.args.net_trail_arm_usdt > 0
            and probe.best_net >= self.args.net_trail_arm_usdt
            and probe.best_net - net >= self.args.net_trail_giveback_usdt
        ):
            reason = "net-trail"
        elif probe.side == "LONG" and px <= probe.stop:
            reason = "stop"
        elif probe.side == "SHORT" and px >= probe.stop:
            reason = "stop"
        elif probe.side == "LONG" and px >= probe.target:
            reason = "target"
        elif probe.side == "SHORT" and px <= probe.target:
            reason = "target"
        elif time.time() - probe.entry_ts >= self.args.probe_horizon_sec:
            reason = "horizon"
        if not reason:
            return
        probe.closed = True
        probe.reason = reason
        probe.net = net
        score = self.ensure_score(probe.symbol, probe.side, probe.mode)
        score.add(net, time.time())
        if reason == "stop" or net <= -self.args.bad_loss_usdt:
            self.bad_until[(probe.symbol, probe.side)] = time.time() + self.args.bad_cooldown_sec
        self.write_event("probe_close", asdict(probe) | {"score_net": score.net, "score_count": score.count})
        mark = "WIN" if net > 0 else "LOSS"
        log(
            f"LEARN {mark:<4} {probe.mode:<6} {probe.symbol:<14} {probe.side:<5} net=${net:+.4f} "
            f"score=${score.net:+.4f}/{score.count} best=${probe.best_net:+.4f} reason={reason}"
        )
        if net > 0:
            self.entry_from_probe_win(probe)

    def start_entry(
        self,
        st: SymbolState,
        side: str,
        features: dict[str, Any],
        best_override: Decimal | None = None,
        force_score: bool = False,
    ) -> None:
        if self.position or self.pending:
            return
        if time.time() - self.last_entry_ts < self.args.entry_cooldown_sec:
            return
        mode = features.get("mode", "follow")
        score = self.ensure_score(st.symbol, side, mode)
        if time.time() < self.bad_until.get((st.symbol, side), 0):
            return
        if time.time() < self.mode_cooldown_until.get((st.symbol, side, mode), 0):
            return
        if force_score:
            if score.losses > self.args.max_force_losses or score.net < self.args.force_score_floor:
                return
        elif not self.pair_allowed(st.symbol, side, mode, best_override):
            return
        entry = st.bid if side == "LONG" else st.ask
        if entry <= 0:
            return
        qty, stop, target, _ = self.compute_order(st, side, entry, MAKER_FEE)
        if qty * entry < Decimal("5"):
            return
        now = time.time()
        features = dict(features)
        if force_score:
            features["force_score"] = True
        self.pending = PendingEntry(
            symbol=st.symbol,
            side=side,
            limit_price=entry,
            qty=qty,
            stop=stop,
            target=target,
            created_ts=now,
            deadline_ts=now + self.args.maker_wait_ms / 1000.0,
            fallback_ts=now + self.args.taker_fallback_ms / 1000.0,
            features=features,
        )
        log(
            f"ARM   {mode:<6} {st.symbol:<14} {side:<5} maker={fmt(entry):>10} "
            f"score=${score.net:+.4f}/{score.count} wr={score.win_rate:.0%}"
            f"{' force' if force_score else ''}"
        )
        self.write_event("entry_arm", asdict(self.pending) | {"score_net": score.net, "score_count": score.count, "force_score": force_score})

    def fill_pending_if_ready(self) -> None:
        if not self.pending or self.position:
            return
        st = self.states[self.pending.symbol]
        now = time.time()
        fill_price = Decimal("0")
        fee_rate = MAKER_FEE
        reason = ""
        if self.pending.side == "LONG" and st.ask <= self.pending.limit_price:
            fill_price = self.pending.limit_price
            reason = "maker"
        elif self.pending.side == "SHORT" and st.bid >= self.pending.limit_price:
            fill_price = self.pending.limit_price
            reason = "maker"
        elif now >= self.pending.fallback_ts:
            source = self.pending.features.get("entry_source")
            if self.pending.features.get("force_score"):
                fill_price = st.ask if self.pending.side == "LONG" else st.bid
                fee_rate = TAKER_FEE
                reason = "taker-force" if source != "instant-exhaustion" else "taker-exh"
            elif source == "probe-win" and self.pair_allowed(st.symbol, self.pending.side, self.pending.features.get("mode", "follow")):
                features = self.current_features(st)
                direction = 1 if self.pending.side == "LONG" else -1
                if direction * features["v1"] >= -self.args.max_win_reversal_pct:
                    fill_price = st.ask if self.pending.side == "LONG" else st.bid
                    fee_rate = TAKER_FEE
                    reason = "taker-win"
            side, _ = self.signal(st)
            if fill_price <= 0 and side == self.pending.side and self.pair_allowed(st.symbol, side, self.pending.features.get("mode", "follow")):
                fill_price = st.ask if side == "LONG" else st.bid
                fee_rate = TAKER_FEE
                reason = "taker"
            elif fill_price <= 0 and self.pending.features.get("mode") == "fade" and self.pair_allowed(st.symbol, self.pending.side, "fade"):
                features = self.current_features(st)
                direction = 1 if self.pending.side == "LONG" else -1
                if direction * features["v1"] >= -self.args.max_win_reversal_pct:
                    fill_price = st.ask if self.pending.side == "LONG" else st.bid
                    fee_rate = TAKER_FEE
                    reason = "taker-fade"
        if fill_price <= 0:
            if now >= self.pending.deadline_ts:
                log(f"DROP  {self.pending.features.get('mode', 'follow'):<6} {self.pending.symbol:<14} {self.pending.side:<5} maker expired")
                self.write_event("entry_drop", asdict(self.pending))
                self.pending = None
            return
        qty, stop, target, fee = self.compute_order(st, self.pending.side, fill_price, fee_rate)
        if qty * fill_price < Decimal("5"):
            self.pending = None
            return
        exit_px = self.exit_price(st, self.pending.side)
        initial_net = self.net_at(self.pending.side, qty, fill_price, fee, exit_px) if exit_px > 0 else Decimal("-999999")
        stop_breached = (
            (self.pending.side == "LONG" and exit_px <= stop)
            or (self.pending.side == "SHORT" and exit_px >= stop)
        )
        if stop_breached or initial_net <= -self.args.max_adverse_fill_loss_usdt:
            mode = self.pending.features.get("mode", "follow")
            log(
                f"REJECT {reason:<10} {mode:<6} {self.pending.symbol:<14} {self.pending.side:<5} "
                f"entry={fmt(fill_price):>10} live_net=${initial_net:+.4f} adverse-fill"
            )
            if self.args.reject_cooldown_sec > 0:
                self.mode_cooldown_until[(self.pending.symbol, self.pending.side, mode)] = time.time() + self.args.reject_cooldown_sec
            self.write_event(
                "entry_reject",
                asdict(self.pending) | {"fill": reason, "entry": fill_price, "live_net": initial_net, "reason": "adverse-fill"},
            )
            self.pending = None
            return
        self.wallet -= fee
        mode = self.pending.features.get("mode", "follow")
        self.position = FusionPosition(self.pending.symbol, self.pending.side, qty, fill_price, stop, target, fee, fee_rate, now, fill_price, mode=mode)
        self.last_entry_ts = now
        log(f"FILL  {reason:<10} {mode:<6} {self.position.symbol:<14} {self.position.side:<5} entry={fmt(fill_price):>10} qty={fmt(qty):>10} fee=${fee:.4f}")
        self.write_event("trade_open", asdict(self.position) | {"fill": reason})
        self.pending = None

    def close_position(self, reason: str) -> None:
        if not self.position:
            return
        pos = self.position
        st = self.states[pos.symbol]
        px = self.exit_price(st, pos.side)
        if px <= 0:
            return
        net = self.net_at(pos.side, pos.qty, pos.entry, pos.entry_fee, px)
        pos.best_net = max(pos.best_net, net)
        gross = (px - pos.entry) * pos.qty if pos.side == "LONG" else (pos.entry - px) * pos.qty
        exit_fee = px * pos.qty * TAKER_FEE
        self.wallet += gross - exit_fee
        self.trades += 1
        self.wins += 1 if net > 0 else 0
        score = self.ensure_score(pos.symbol, pos.side, pos.mode)
        score.add(net, time.time())
        if net > 0 and self.args.win_reentry_cooldown_sec > 0:
            self.mode_cooldown_until[(pos.symbol, pos.side, pos.mode)] = time.time() + self.args.win_reentry_cooldown_sec
        if net < 0:
            self.bad_until[(pos.symbol, pos.side)] = time.time() + self.args.bad_cooldown_sec
        log(f"CLOSE {pos.mode:<6} {pos.symbol:<14} {pos.side:<5} net=${net:+.4f} wallet=${self.wallet:.4f} best=${pos.best_net:+.4f} reason={reason}")
        self.write_event("trade_close", asdict(pos) | {"exit": px, "net": net, "wallet": self.wallet, "reason": reason})
        self.position = None

    def manage_position(self) -> None:
        if not self.position:
            return
        pos = self.position
        st = self.states[pos.symbol]
        px = self.exit_price(st, pos.side)
        if px <= 0:
            return
        net = self.net_at(pos.side, pos.qty, pos.entry, pos.entry_fee, px)
        pos.best_net = max(pos.best_net, net)
        age = time.time() - pos.entry_ts
        reason = ""
        if self.args.snap_profit_usdt > 0 and net >= self.args.snap_profit_usdt:
            reason = "net-profit"
        elif (
            self.args.net_trail_arm_usdt > 0
            and pos.best_net >= self.args.net_trail_arm_usdt
            and pos.best_net - net >= self.args.net_trail_giveback_usdt
        ):
            reason = "net-trail"
        elif (
            self.args.micro_trail_arm_usdt > 0
            and pos.best_net >= self.args.micro_trail_arm_usdt
            and pos.best_net - net >= self.args.micro_trail_giveback_usdt
        ):
            reason = "micro-trail"
        elif (
            self.args.entry_fail_sec > 0
            and age >= self.args.entry_fail_sec
            and pos.best_net < self.args.entry_fail_max_best_usdt
            and net <= -self.args.entry_fail_loss_usdt
        ):
            reason = "entry-fail"
        elif (
            self.args.flat_exit_sec > 0
            and age >= self.args.flat_exit_sec
            and pos.best_net <= self.args.flat_exit_max_best_usdt
            and net <= self.args.flat_exit_loss_usdt
        ):
            reason = "flat-exit"
        elif pos.side == "LONG" and px <= pos.stop:
            reason = "stop"
        elif pos.side == "SHORT" and px >= pos.stop:
            reason = "stop"
        elif pos.side == "LONG" and px >= pos.target:
            reason = "target"
        elif pos.side == "SHORT" and px <= pos.target:
            reason = "target"
        elif age >= self.args.max_hold_sec:
            reason = "max-hold"
        if not reason:
            return
        self.close_position(reason)

    def maybe_fast_exhaustion_entry(self, st: SymbolState, side: str, features: dict[str, Any]) -> None:
        if not self.args.fast_exhaustion_entry:
            return
        if side == "LONG" and not self.args.exhaustion_long_fade:
            return
        if side == "SHORT" and not self.args.exhaustion_short_fade:
            return
        direction = 1 if side == "LONG" else -1
        if direction * features.get("book", 0.0) < self.args.exhaustion_min_book:
            return
        if abs(features.get("v1", 0.0)) < self.args.exhaustion_min_v1_pct:
            return
        if abs(features.get("v3", 0.0)) < self.args.exhaustion_min_v3_pct:
            return
        if direction * features.get("f1", 0.0) < self.args.exhaustion_min_f1:
            return
        if direction * features.get("f3", 0.0) < self.args.exhaustion_min_f3:
            return
        if features.get("n3", Decimal("0")) < self.args.exhaustion_min_flow_notional:
            return
        fade_side = "SHORT" if side == "LONG" else "LONG"
        trade_features = dict(features)
        trade_features["mode"] = "exhaust"
        trade_features["entry_source"] = "instant-exhaustion"
        self.start_entry(st, fade_side, trade_features, force_score=True)

    def process_signal(self, st: SymbolState) -> None:
        side, features = self.signal(st)
        if not side:
            return
        follow_features = dict(features)
        follow_features["mode"] = "follow"
        self.open_probe(st, side, follow_features)
        self.start_entry(st, side, follow_features, force_score=self.args.must_trade_signals and self.args.must_trade_follow)
        if self.args.learn_fade:
            fade_side = "SHORT" if side == "LONG" else "LONG"
            fade_features = dict(features)
            fade_features["mode"] = "fade"
            self.open_probe(st, fade_side, fade_features)
            self.start_entry(st, fade_side, fade_features, force_score=self.args.must_trade_signals and self.args.must_trade_fade)
        self.maybe_fast_exhaustion_entry(st, side, features)

    def on_event(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        event = data.get("e")
        order = data.get("o") if isinstance(data.get("o"), dict) else None
        sym = data.get("s") or (order or {}).get("s")
        if sym not in self.states:
            return
        st = self.states[sym]
        now = time.time()
        if event == "bookTicker" or "u" in data:
            bid = Decimal(data.get("b", "0"))
            ask = Decimal(data.get("a", "0"))
            if bid > 0 and ask > 0:
                st.bid = bid
                st.ask = ask
                st.bid_qty = Decimal(data.get("B", "0"))
                st.ask_qty = Decimal(data.get("A", "0"))
                if not st.first_tick_ts:
                    st.first_tick_ts = now
                st.last_book_ts = now
                st.ticks.append((now, st.mid()))
        elif event == "aggTrade":
            price = Decimal(data.get("p", "0"))
            qty = Decimal(data.get("q", "0"))
            if price > 0 and qty > 0:
                if not st.first_trade_ts:
                    st.first_trade_ts = now
                if not st.first_tick_ts:
                    st.first_tick_ts = now
                st.ticks.append((now, price))
                sign = Decimal("-1") if data.get("m", False) else Decimal("1")
                st.flow.append((now, sign, price * qty))
        elif event == "forceOrder" and order:
            price = Decimal(order.get("ap") or order.get("p") or "0")
            qty = Decimal(order.get("z") or order.get("q") or "0")
            if price > 0 and qty > 0:
                side = str(order.get("S", "")).upper()
                sign = Decimal("1") if side == "BUY" else Decimal("-1")
                st.liquidations.append((now, sign, price * qty))

        for probe in list(self.probes):
            if probe.symbol == sym:
                self.update_probe(probe)
        self.probes = [p for p in self.probes if not p.closed]
        self.fill_pending_if_ready()
        self.manage_position()
        self.process_signal(st)

    async def consumer(self, url: str, queue: asyncio.Queue[dict[str, Any]], label: str) -> None:
        while not self.stop:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10, max_queue=4096) as ws:
                    log(f"WS    {label} connected")
                    while not self.stop:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
                        if isinstance(data, list):
                            for item in data:
                                queue.put_nowait(item)
                        else:
                            queue.put_nowait(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stop:
                    log(f"WS    {label} reconnect: {exc}")
                    await asyncio.sleep(1)

    def score_line(self) -> str:
        rows = sorted(self.scores.values(), key=lambda s: (s.net, s.best), reverse=True)[:5]
        if not rows:
            return "scores=none"
        return " | ".join(f"{r.symbol}:{r.mode[0]}:{r.side[0]} ${r.net:+.2f}/{r.count}" for r in rows)

    async def run(self) -> int:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10000)
        lower = [s.lower() for s in self.symbols]
        public = "/".join(f"{s}@bookTicker" for s in lower)
        market = "/".join([*(f"{s}@aggTrade" for s in lower), "!forceOrder@arr"])
        tasks = [
            asyncio.create_task(self.consumer(f"wss://fstream.binance.com/public/stream?streams={public}", queue, "book")),
            asyncio.create_task(self.consumer(f"wss://fstream.binance.com/market/stream?streams={market}", queue, "trade")),
        ]
        start = time.time()
        log(f"FUSION START symbols={len(self.symbols)} wallet=${self.wallet:.4f} target=${self.args.target:+.2f} out={self.out_path}")
        try:
            while time.time() - start < self.args.seconds and not self.stop:
                realized = self.wallet - self.start_wallet
                if realized >= self.args.target:
                    log(f"TARGET HIT pnl=${realized:+.4f}")
                    break
                if realized <= self.args.loss:
                    log(f"LOSS HIT pnl=${realized:+.4f}")
                    break
                if self.trades >= self.args.max_trades:
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1)
                    self.on_event(item)
                except asyncio.TimeoutError:
                    pass
                now = time.time()
                if now - self.last_status >= 15:
                    self.last_status = now
                    pos = "flat" if not self.position else f"{self.position.mode} {self.position.symbol} {self.position.side} net={self.net_at(self.position.side, self.position.qty, self.position.entry, self.position.entry_fee, self.exit_price(self.states[self.position.symbol], self.position.side)):+.4f}"
                    log(f"STATUS wallet=${self.wallet:.4f} equity=${self.equity():.4f} trades={self.trades} probes={len(self.probes)} pos={pos} :: {self.score_line()}")
        finally:
            if self.position:
                self.close_position("session-end")
            if self.pending:
                self.write_event("entry_drop", asdict(self.pending) | {"reason": "session-end"})
                self.pending = None
            self.stop = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        realized = self.wallet - self.start_wallet
        win_rate = self.wins / self.trades * 100 if self.trades else 0
        log(f"FUSION FINAL start=${self.start_wallet:.4f} end=${self.wallet:.4f} pnl=${realized:+.4f} trades={self.trades} wins={self.wins} winrate={win_rate:.0f}%")
        self.write_event("final", {"start": self.start_wallet, "end": self.wallet, "pnl": realized, "trades": self.trades, "wins": self.wins})
        return 0 if realized >= self.args.target else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=360)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--count", type=int, default=45)
    ap.add_argument("--skip-symbols", default="")
    ap.add_argument("--wallet", type=Decimal, default=Decimal("24"))
    ap.add_argument("--target", type=Decimal, default=Decimal("10"))
    ap.add_argument("--loss", type=Decimal, default=Decimal("-8"))
    ap.add_argument("--max-trades", type=int, default=8)
    ap.add_argument("--max-hold-sec", type=float, default=90)
    ap.add_argument("--leverage", type=int, default=175)
    ap.add_argument("--margin-fraction", type=Decimal, default=Decimal("0.95"))
    ap.add_argument("--tp-pct", type=Decimal, default=Decimal("0.0035"))
    ap.add_argument("--sl-pct", type=Decimal, default=Decimal("0.0018"))
    ap.add_argument("--max-stop-risk-usdt", type=Decimal, default=Decimal("2.75"))
    ap.add_argument("--snap-profit-usdt", type=Decimal, default=Decimal("2.50"))
    ap.add_argument("--net-trail-arm-usdt", type=Decimal, default=Decimal("1.50"))
    ap.add_argument("--net-trail-giveback-usdt", type=Decimal, default=Decimal("0.85"))
    ap.add_argument("--micro-trail-arm-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--micro-trail-giveback-usdt", type=Decimal, default=Decimal("0.35"))
    ap.add_argument("--entry-fail-sec", type=float, default=0.0)
    ap.add_argument("--entry-fail-max-best-usdt", type=Decimal, default=Decimal("0.20"))
    ap.add_argument("--entry-fail-loss-usdt", type=Decimal, default=Decimal("0.80"))
    ap.add_argument("--flat-exit-sec", type=float, default=0.0)
    ap.add_argument("--flat-exit-max-best-usdt", type=Decimal, default=Decimal("0.15"))
    ap.add_argument("--flat-exit-loss-usdt", type=Decimal, default=Decimal("-0.25"))
    ap.add_argument("--max-adverse-fill-loss-usdt", type=Decimal, default=Decimal("0.75"))
    ap.add_argument("--reject-cooldown-sec", type=float, default=2.0)
    ap.add_argument("--probe-horizon-sec", type=float, default=60)
    ap.add_argument("--score-window", type=int, default=4)
    ap.add_argument("--min-probe-count", type=int, default=1)
    ap.add_argument("--min-pair-net", type=Decimal, default=Decimal("0.75"))
    ap.add_argument("--min-best-net", type=Decimal, default=Decimal("1.50"))
    ap.add_argument("--min-pair-win-rate", type=float, default=0.50)
    ap.add_argument("--max-pair-losses", type=int, default=0)
    ap.add_argument("--bad-loss-usdt", type=Decimal, default=Decimal("1.50"))
    ap.add_argument("--bad-cooldown-sec", type=float, default=180)
    ap.add_argument("--probe-cooldown-sec", type=float, default=2.0)
    ap.add_argument("--entry-cooldown-sec", type=float, default=1.5)
    ap.add_argument("--win-reentry-cooldown-sec", type=float, default=0.0)
    ap.add_argument("--entry-on-probe-win", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--entry-on-horizon-win", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--learn-fade", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--must-trade-signals", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--must-trade-follow", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--must-trade-fade", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--fast-exhaustion-entry", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--exhaustion-long-fade", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--exhaustion-short-fade", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--exhaustion-min-book", type=float, default=0.75)
    ap.add_argument("--exhaustion-min-v1-pct", type=float, default=0.12)
    ap.add_argument("--exhaustion-min-v3-pct", type=float, default=0.12)
    ap.add_argument("--exhaustion-min-f1", type=float, default=0.70)
    ap.add_argument("--exhaustion-min-f3", type=float, default=0.55)
    ap.add_argument("--exhaustion-min-flow-notional", type=Decimal, default=Decimal("4000"))
    ap.add_argument("--max-force-losses", type=int, default=0)
    ap.add_argument("--force-score-floor", type=Decimal, default=Decimal("-0.01"))
    ap.add_argument("--win-entry-max-age-sec", type=float, default=45.0)
    ap.add_argument("--max-probe-win-dir-book", type=float, default=0.75)
    ap.add_argument("--max-win-reversal-pct", type=float, default=0.03)
    ap.add_argument("--maker-wait-ms", type=int, default=700)
    ap.add_argument("--taker-fallback-ms", type=int, default=350)
    ap.add_argument("--min-flow-notional", type=Decimal, default=Decimal("5000"))
    ap.add_argument("--min-v1-pct", type=float, default=0.045)
    ap.add_argument("--min-v3-pct", type=float, default=0.08)
    ap.add_argument("--min-f1", type=float, default=0.35)
    ap.add_argument("--min-f3", type=float, default=0.20)
    ap.add_argument("--required-streak", type=int, default=2)
    ap.add_argument("--required-age-sec", type=float, default=0.05)
    ap.add_argument("--min-book-imbalance", type=float, default=-0.10)
    ap.add_argument("--max-spread-pct", type=float, default=0.18)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    client = BinanceFuturesClient(timeout=3)
    skip = SKIP | {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip() and s.strip().upper() not in skip]
    else:
        symbols = select_symbols(client, args.count, skip)
    if not symbols:
        log("No symbols selected.")
        return 2
    out = Path(args.out) if args.out else PROJECT_ROOT / "logs" / f"fusion_adaptive_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    bot = FusionAdaptiveBot(args, symbols, out)
    try:
        bot.set_filters(client)
    except BinanceApiError as exc:
        log(f"Filter load failed: {exc}")
    return asyncio.run(bot.run())


if __name__ == "__main__":
    raise SystemExit(main())
