"""Fast dry-only futures sniper.

This is a paper simulator. It never sends live Binance orders.
It watches multiple USD-M futures symbols over WebSocket and makes immediate
event-driven decisions from bookTicker + aggTrade flow.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.binance_client import BinanceApiError, BinanceFuturesClient


SKIP = {
    "B3USDT",
    "DEGENUSDT",
    "BOBUSDT",
    "ZKJUSDT",
    "IRUSDT",
    "DAMUSDT",
    # Repeated dry-run whipsaw/slippage offenders in the current session.
    "AIOTUSDT",
    "ORCAUSDT",
    "ZBTUSDT",
    "BSBUSDT",
}
TAKER_FEE = Decimal("0.0005")
MAKER_FEE = Decimal("0.0002")


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


def qdown(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def fmt(value: Decimal | float | int) -> str:
    return format(Decimal(str(value)).normalize(), "f")


@dataclass
class SymbolState:
    symbol: str
    bid: Decimal = Decimal("0")
    ask: Decimal = Decimal("0")
    bid_qty: Decimal = Decimal("0")
    ask_qty: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.000001")
    step_size: Decimal = Decimal("1")
    first_tick_ts: float = 0.0
    first_trade_ts: float = 0.0
    last_book_ts: float = 0.0
    last_signal_side: str = ""
    signal_first_ts: float = 0.0
    last_signal_ts: float = 0.0
    signal_streak: int = 0
    pending_side: str = ""
    pending_price: Decimal = Decimal("0")
    pending_ts: float = 0.0
    last_trade_price: Decimal = Decimal("0")
    ticks: deque[tuple[float, Decimal]] = field(default_factory=lambda: deque(maxlen=800))
    flow: deque[tuple[float, Decimal, Decimal]] = field(default_factory=lambda: deque(maxlen=1200))
    liquidations: deque[tuple[float, Decimal, Decimal]] = field(default_factory=lambda: deque(maxlen=300))

    def mid(self) -> Decimal:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        if self.ticks:
            return self.ticks[-1][1]
        return Decimal("0")

    def spread_pct(self) -> float:
        mid = self.mid()
        return float((self.ask - self.bid) / mid * 100) if mid > 0 and self.ask > self.bid else 999.0

    def velocity_pct(self, sec: float) -> float:
        if len(self.ticks) < 2:
            return 0.0
        now = time.time()
        latest = self.ticks[-1][1]
        base = self.ticks[0][1]
        for ts, price in reversed(self.ticks):
            if now - ts >= sec:
                base = price
                break
        return float((latest / base - 1) * 100) if base > 0 else 0.0

    def flow_ratio(self, sec: float) -> tuple[float, Decimal]:
        now = time.time()
        signed = Decimal("0")
        total = Decimal("0")
        for ts, sign, notional in reversed(self.flow):
            if now - ts > sec:
                break
            signed += sign * notional
            total += notional
        return (float(signed / total) if total > 0 else 0.0, total)

    def book_imbalance(self) -> float:
        total = self.bid_qty + self.ask_qty
        if total <= 0:
            return 0.0
        return float((self.bid_qty - self.ask_qty) / total)

    def liquidation_ratio(self, sec: float) -> tuple[float, Decimal]:
        now = time.time()
        signed = Decimal("0")
        total = Decimal("0")
        for ts, sign, notional in reversed(self.liquidations):
            if now - ts > sec:
                break
            signed += sign * notional
            total += notional
        return (float(signed / total) if total > 0 else 0.0, total)


@dataclass
class Position:
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_fee: Decimal
    entry_ts: float
    best: Decimal
    trail: Decimal | None = None
    best_net: Decimal = Decimal("-999999")


@dataclass
class PendingEntry:
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_fee: Decimal
    created_ts: float
    expire_ts: float
    signal_score: float
    v1: float
    v3: float
    v5: float
    f1: float
    f3: float
    book: float


class SniperPaperBot:
    def __init__(
        self,
        symbols: list[str],
        wallet: Decimal,
        leverage: int,
        margin_fraction: Decimal,
        target_usdt: Decimal,
        loss_usdt: Decimal,
        seconds: int,
        max_trades: int,
        max_hold_sec: float,
        tp_pct: Decimal,
        sl_pct: Decimal,
        trail_arm_pct: Decimal,
        trail_giveback_pct: Decimal,
        slow_start_sec: float,
        slow_start_fee_multiple: Decimal,
        min_flow_notional: Decimal,
        min_v1_pct: float,
        min_v3_pct: float,
        min_v5_pct: float,
        max_v5_pct: float,
        min_f1: float,
        min_f3: float,
        min_signal_score: float,
        max_book_edge: float,
        crowded_min_flow: float,
        required_streak: int,
        required_age_sec: float,
        min_book_imbalance: float,
        max_spread_pct: float,
        side_filter: str,
        confirm_delay_sec: float,
        confirm_min_move_pct: float,
        max_stop_risk_usdt: Decimal,
        snap_profit_usdt: Decimal,
        net_trail_arm_usdt: Decimal,
        net_trail_giveback_usdt: Decimal,
        symbol_sides: dict[str, str],
        turbo: bool,
        fade: bool,
        maker_entry: bool,
        maker_fee: Decimal,
        maker_fill_timeout_sec: float,
        maker_offset_ticks: int,
    ):
        self.symbols = symbols
        self.wallet = wallet
        self.start_wallet = wallet
        self.leverage = leverage
        self.margin_fraction = margin_fraction
        self.target_usdt = target_usdt
        self.loss_usdt = loss_usdt
        self.seconds = seconds
        self.max_trades = max_trades
        self.max_hold_sec = max_hold_sec
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.trail_arm_pct = trail_arm_pct
        self.trail_giveback_pct = trail_giveback_pct
        self.slow_start_sec = slow_start_sec
        self.slow_start_fee_multiple = slow_start_fee_multiple
        self.min_flow_notional = min_flow_notional
        self.min_v1_pct = min_v1_pct
        self.min_v3_pct = min_v3_pct
        self.min_v5_pct = min_v5_pct
        self.max_v5_pct = max_v5_pct
        self.min_f1 = min_f1
        self.min_f3 = min_f3
        self.min_signal_score = min_signal_score
        self.max_book_edge = max_book_edge
        self.crowded_min_flow = crowded_min_flow
        self.required_streak = required_streak
        self.required_age_sec = required_age_sec
        self.min_book_imbalance = min_book_imbalance
        self.max_spread_pct = max_spread_pct
        self.side_filter = side_filter
        self.confirm_delay_sec = confirm_delay_sec
        self.confirm_min_move_pct = confirm_min_move_pct
        self.max_stop_risk_usdt = max_stop_risk_usdt
        self.snap_profit_usdt = snap_profit_usdt
        self.net_trail_arm_usdt = net_trail_arm_usdt
        self.net_trail_giveback_usdt = net_trail_giveback_usdt
        self.symbol_sides = symbol_sides
        self.turbo = turbo
        self.fade = fade
        self.maker_entry = maker_entry
        self.maker_fee = maker_fee
        self.maker_fill_timeout_sec = maker_fill_timeout_sec
        self.maker_offset_ticks = maker_offset_ticks
        self.states = {s: SymbolState(s) for s in symbols}
        self.position: Position | None = None
        self.pending: PendingEntry | None = None
        self.trades = 0
        self.wins = 0
        self.last_entry_ts = 0.0
        self.last_loss_ts = 0.0
        self.peak_wallet = wallet
        self.lock_floor = wallet
        self.symbol_banned_until: dict[str, float] = {}
        self.stop = False
        self.last_status = 0.0

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

    def current_exit(self, st: SymbolState, pos: Position) -> Decimal:
        return st.bid if pos.side == "LONG" else st.ask

    def unrealized(self) -> Decimal:
        if not self.position:
            return Decimal("0")
        st = self.states[self.position.symbol]
        exit_price = self.current_exit(st, self.position)
        if exit_price <= 0:
            return Decimal("0")
        if self.position.side == "LONG":
            return (exit_price - self.position.entry) * self.position.qty
        return (self.position.entry - exit_price) * self.position.qty

    def equity(self) -> Decimal:
        return self.wallet + self.unrealized()

    def net_if_closed(self, pos: Position, exit_price: Decimal) -> Decimal:
        gross = (exit_price - pos.entry) * pos.qty if pos.side == "LONG" else (pos.entry - exit_price) * pos.qty
        exit_fee = exit_price * pos.qty * TAKER_FEE
        return gross - pos.entry_fee - exit_fee

    def maker_limit(self, st: SymbolState, side: str) -> Decimal:
        offset = st.tick_size * self.maker_offset_ticks
        if side == "LONG":
            limit = st.bid + offset
            if st.ask > 0 and limit >= st.ask:
                limit = st.ask - st.tick_size
            if limit <= 0:
                limit = st.bid
            return qdown(limit, st.tick_size)
        limit = st.ask - offset
        if st.bid > 0 and limit <= st.bid:
            limit = st.bid + st.tick_size
        if limit <= 0:
            limit = st.ask
        return qdown(limit, st.tick_size)

    def manage_pending(self) -> None:
        if not self.pending:
            return
        pending = self.pending
        st = self.states[pending.symbol]
        now = time.time()
        if now >= pending.expire_ts:
            log(
                f"SNIPER CANCEL {pending.symbol} {pending.side} maker entry=${fmt(pending.entry)} "
                f"age={now - pending.created_ts:.2f}s reason=unfilled"
            )
            self.pending = None
            self.last_entry_ts = now
            return
        touched = False
        if pending.side == "LONG":
            touched = (st.last_trade_price > 0 and st.last_trade_price <= pending.entry) or (st.ask > 0 and st.ask <= pending.entry)
        else:
            touched = (st.last_trade_price > 0 and st.last_trade_price >= pending.entry) or (st.bid > 0 and st.bid >= pending.entry)
        if not touched:
            return
        if self.wallet - self.start_wallet - pending.entry_fee <= self.loss_usdt:
            log(
                f"SNIPER CANCEL {pending.symbol} {pending.side}: maker fill fee would breach loss limit "
                f"realized_after_fee=${(self.wallet - self.start_wallet - pending.entry_fee):+.4f}"
            )
            self.pending = None
            self.stop = True
            return
        self.wallet -= pending.entry_fee
        self.position = Position(
            pending.symbol,
            pending.side,
            pending.qty,
            pending.entry,
            pending.stop,
            pending.target,
            pending.entry_fee,
            now,
            pending.entry,
        )
        self.pending = None
        self.last_entry_ts = now
        log(
            f"SNIPER FILL {pending.symbol} {pending.side} maker entry=${fmt(pending.entry)} "
            f"qty={fmt(pending.qty)} stop=${fmt(pending.stop)} target=${fmt(pending.target)} "
            f"score={pending.signal_score:.3f} fee=${pending.entry_fee:.4f}"
        )

    def maybe_open(self, st: SymbolState) -> None:
        if self.position or self.pending or time.time() - self.last_entry_ts < 2.0:
            return
        if time.time() - self.last_loss_ts < 20.0:
            return
        if time.time() < self.symbol_banned_until.get(st.symbol, 0.0):
            return
        now = time.time()
        if not st.first_tick_ts or now - st.first_tick_ts < 5.0:
            return
        if not st.first_trade_ts or now - st.first_trade_ts < 3.0:
            return
        if not st.last_book_ts or now - st.last_book_ts > 1.5:
            return
        if st.spread_pct() > self.max_spread_pct or len(st.ticks) < 6:
            return
        v1 = st.velocity_pct(1.0)
        v3 = st.velocity_pct(3.0)
        v5 = st.velocity_pct(5.0)
        f1, n1 = st.flow_ratio(1.0)
        f3, n3 = st.flow_ratio(3.0)
        liq3, lq3 = st.liquidation_ratio(3.0)
        if n3 < self.min_flow_notional:
            st.signal_streak = 0
            return

        side: str | None = None
        if v1 >= self.min_v1_pct and v3 >= self.min_v3_pct and f1 >= self.min_f1 and f3 >= self.min_f3:
            side = "LONG"
        elif v1 <= -self.min_v1_pct and v3 <= -self.min_v3_pct and f1 <= -self.min_f1 and f3 <= -self.min_f3:
            side = "SHORT"
        if not side:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        direction = 1 if side == "LONG" else -1
        book_imb = st.book_imbalance()
        trend1 = direction * v1
        trend3 = direction * v3
        trend5 = direction * v5
        flow1_edge = direction * f1
        flow3_edge = direction * f3
        if trend5 < self.min_v5_pct:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        if trend5 > self.max_v5_pct:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        liquidation_aligned = lq3 >= Decimal("2500") and direction * liq3 >= 0.45
        if direction * book_imb < self.min_book_imbalance and not liquidation_aligned:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        book_edge = direction * book_imb
        if book_edge > self.max_book_edge and min(flow1_edge, flow3_edge) < self.crowded_min_flow:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        signal_score = (
            trend1 * 2.0
            + trend3 * 1.5
            + trend5
            + max(0.0, flow1_edge) * 0.15
            + max(0.0, flow3_edge) * 0.15
            + max(0.0, book_edge) * 0.20
        )
        if signal_score < self.min_signal_score:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        if lq3 >= Decimal("5000") and direction * liq3 <= -0.35:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        if self.fade:
            side = "SHORT" if side == "LONG" else "LONG"
        if self.side_filter != "BOTH" and side != self.side_filter:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        allowed_side = self.symbol_sides.get(st.symbol)
        if allowed_side and side != allowed_side:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return
        now = time.time()
        if st.last_signal_side == side and now - st.last_signal_ts <= 1.0:
            if st.signal_streak == 0:
                st.signal_first_ts = now
            st.signal_streak += 1
        else:
            st.signal_streak = 1
            st.signal_first_ts = now
        st.last_signal_side = side
        st.last_signal_ts = now
        if st.signal_streak < self.required_streak or now - st.signal_first_ts < self.required_age_sec:
            return
        if self.confirm_delay_sec > 0:
            mid = st.mid()
            if mid <= 0:
                return
            if st.pending_side != side or now - st.pending_ts > max(1.5, self.confirm_delay_sec * 3):
                st.pending_side = side
                st.pending_price = mid
                st.pending_ts = now
                return
            if now - st.pending_ts < self.confirm_delay_sec:
                return
            pending_move = float((mid / st.pending_price - 1) * 100) if st.pending_price > 0 else 0.0
            if side == "SHORT":
                pending_move = -pending_move
            if pending_move < self.confirm_min_move_pct:
                st.pending_side = ""
                st.pending_price = Decimal("0")
                st.pending_ts = 0.0
                return
            st.pending_side = ""
            st.pending_price = Decimal("0")
            st.pending_ts = 0.0

        entry = self.maker_limit(st, side) if self.maker_entry else (st.ask if side == "LONG" else st.bid)
        if entry <= 0:
            return
        exit_now = st.bid if side == "LONG" else st.ask
        if exit_now <= 0:
            return
        cross_cost_pct = abs(float((entry - exit_now) / entry))
        if cross_cost_pct > float(self.sl_pct * Decimal("0.40")):
            return

        if side == "LONG":
            stop = qdown(entry * (Decimal("1") - self.sl_pct), st.tick_size)
            if stop >= entry:
                stop = entry - st.tick_size
            target = qdown(entry * (Decimal("1") + self.tp_pct), st.tick_size)
            if target <= entry:
                target = entry + st.tick_size
            stop_move = (entry - stop) / entry
        else:
            stop = qdown(entry * (Decimal("1") + self.sl_pct), st.tick_size)
            if stop <= entry:
                stop = entry + st.tick_size
            target = qdown(entry * (Decimal("1") - self.tp_pct), st.tick_size)
            if target >= entry:
                target = entry - st.tick_size
            stop_move = (stop - entry) / entry

        max_margin_notional = self.wallet * self.margin_fraction * Decimal(self.leverage)
        realized = self.wallet - self.start_wallet
        if realized > 0:
            max_stop_risk = max(Decimal("1.25"), min(Decimal("3.00"), realized * Decimal("0.45")))
        else:
            max_stop_risk = max(Decimal("1.25"), abs(self.loss_usdt + realized))
        if self.max_stop_risk_usdt > 0:
            max_stop_risk = min(max_stop_risk, self.max_stop_risk_usdt)
        entry_fee_rate = self.maker_fee if self.maker_entry else TAKER_FEE
        risk_per_notional = stop_move + entry_fee_rate + TAKER_FEE
        risk_notional = max_stop_risk / risk_per_notional
        notional = min(max_margin_notional, risk_notional)
        margin = (notional / Decimal(self.leverage)).quantize(Decimal("0.01"))
        qty = qdown(notional / entry, st.step_size)
        if qty * entry < Decimal("5"):
            return
        fee = qty * entry * entry_fee_rate
        estimated_stop_risk = qty * entry * self.sl_pct + (qty * entry * (entry_fee_rate + TAKER_FEE))
        if self.lock_floor > self.start_wallet and self.wallet - estimated_stop_risk < self.lock_floor:
            self.stop = True
            log(
                f"SNIPER ending session: next trade risk would break profit lock "
                f"symbol={st.symbol} risk=${estimated_stop_risk:.4f} floor=${self.lock_floor:.4f}"
            )
            return
        if self.wallet - self.start_wallet - fee <= self.loss_usdt:
            self.stop = True
            log(
                f"SNIPER block entry {st.symbol}: entry fee would breach loss limit "
                f"realized_after_fee=${(self.wallet - self.start_wallet - fee):+.4f}"
            )
            return
        if self.maker_entry:
            self.pending = PendingEntry(
                st.symbol,
                side,
                qty,
                entry,
                stop,
                target,
                fee,
                time.time(),
                time.time() + self.maker_fill_timeout_sec,
                signal_score,
                v1,
                v3,
                v5,
                f1,
                f3,
                book_imb,
            )
            self.last_entry_ts = time.time()
            log(
                f"SNIPER PENDING {st.symbol} {side} maker entry=${fmt(entry)} qty={fmt(qty)} "
                f"stop=${fmt(stop)} target=${fmt(target)} v1={v1:+.3f}% v3={v3:+.3f}% "
                f"v5={v5:+.3f}% flow1={f1:+.2f} flow3={f3:+.2f} book={book_imb:+.2f} "
                f"score={signal_score:.3f} expires={self.maker_fill_timeout_sec:.1f}s fee=${fee:.4f}"
            )
            return
        self.wallet -= fee
        self.position = Position(st.symbol, side, qty, entry, stop, target, fee, time.time(), entry)
        self.last_entry_ts = time.time()
        log(
            f"SNIPER OPEN {st.symbol} {side} entry=${fmt(entry)} qty={fmt(qty)} "
            f"stop=${fmt(stop)} target=${fmt(target)} v1={v1:+.3f}% v3={v3:+.3f}% "
            f"v5={v5:+.3f}% flow1={f1:+.2f} flow3={f3:+.2f} "
            f"book={book_imb:+.2f} liq3={liq3:+.2f}/${fmt(lq3)} "
            f"score={signal_score:.3f} n1=${fmt(n1)} n3=${fmt(n3)} "
            f"streak={st.signal_streak} fee=${fee:.4f}"
        )

    def close(self, reason: str) -> None:
        if not self.position:
            return
        pos = self.position
        st = self.states[pos.symbol]
        exit_price = self.current_exit(st, pos)
        if exit_price <= 0:
            return
        gross = (exit_price - pos.entry) * pos.qty if pos.side == "LONG" else (pos.entry - exit_price) * pos.qty
        exit_fee = exit_price * pos.qty * TAKER_FEE
        net = gross - pos.entry_fee - exit_fee
        self.wallet += gross - exit_fee
        self.trades += 1
        self.wins += 1 if net > 0 else 0
        if net < 0:
            self.last_loss_ts = time.time()
            self.symbol_banned_until[pos.symbol] = time.time() + 90.0
            if self.leverage >= 25 and net <= Decimal("-3.50"):
                self.stop = True
                log("SNIPER high-leverage loss lock engaged")
        if self.wallet > self.peak_wallet:
            self.peak_wallet = self.wallet
            profit = self.peak_wallet - self.start_wallet
            if self.target_usdt >= Decimal("10"):
                if profit >= Decimal("8"):
                    self.lock_floor = max(self.lock_floor, self.start_wallet + profit * Decimal("0.55"))
            else:
                if profit >= Decimal("5"):
                    self.lock_floor = max(self.lock_floor, self.start_wallet + profit * Decimal("0.50"))
                if profit >= Decimal("8"):
                    self.lock_floor = max(self.lock_floor, self.start_wallet + profit * Decimal("0.75"))
        self.position = None
        self.last_entry_ts = time.time()
        log(
            f"SNIPER CLOSE {pos.symbol} {pos.side} exit=${fmt(exit_price)} gross=${gross:+.4f} "
            f"fees=${(pos.entry_fee + exit_fee):.4f} pnl=${net:+.4f} wallet=${self.wallet:.4f} reason={reason}"
        )

    def manage(self) -> None:
        if not self.position:
            return
        pos = self.position
        st = self.states[pos.symbol]
        exit_price = self.current_exit(st, pos)
        if exit_price <= 0:
            return
        age = time.time() - pos.entry_ts
        gross_now = (exit_price - pos.entry) * pos.qty if pos.side == "LONG" else (pos.entry - exit_price) * pos.qty
        net_now = self.net_if_closed(pos, exit_price)
        if net_now > pos.best_net:
            pos.best_net = net_now
        if self.snap_profit_usdt > 0 and net_now >= self.snap_profit_usdt:
            self.close("net-profit")
            return
        if (
            self.net_trail_arm_usdt > 0
            and self.net_trail_giveback_usdt > 0
            and pos.best_net >= self.net_trail_arm_usdt
            and pos.best_net - net_now >= self.net_trail_giveback_usdt
        ):
            self.close("net-trail")
            return
        if age >= self.max_hold_sec:
            self.close("max-hold")
            return
        if age >= self.slow_start_sec and gross_now < pos.entry_fee * self.slow_start_fee_multiple:
            self.close("slow-start")
            return
        if age >= 35.0 and gross_now < pos.entry_fee * Decimal("1.20"):
            self.close("dead-trade")
            return
        if pos.side == "LONG":
            if exit_price > pos.best:
                pos.best = exit_price
            if pos.best >= pos.entry * (Decimal("1") + self.trail_arm_pct):
                trail = qdown(pos.best * (Decimal("1") - self.trail_giveback_pct), st.tick_size)
                pos.trail = max(pos.trail or trail, trail)
            if exit_price <= pos.stop:
                self.close("stop")
            elif exit_price >= pos.target:
                self.close("target")
            elif pos.trail and exit_price <= pos.trail:
                self.close("trail")
        else:
            if exit_price < pos.best:
                pos.best = exit_price
            if pos.best <= pos.entry * (Decimal("1") - self.trail_arm_pct):
                trail = qdown(pos.best * (Decimal("1") + self.trail_giveback_pct), st.tick_size)
                pos.trail = min(pos.trail or trail, trail)
            if exit_price >= pos.stop:
                self.close("stop")
            elif exit_price <= pos.target:
                self.close("target")
            elif pos.trail and exit_price >= pos.trail:
                self.close("trail")

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
                st.last_trade_price = price
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
        self.manage_pending()
        self.manage()
        self.maybe_open(st)

    async def consumer(self, url: str, queue: asyncio.Queue[dict[str, Any]], label: str) -> None:
        while not self.stop:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10, max_queue=2048) as ws:
                    log(f"SNIPER WS {label} connected")
                    while not self.stop:
                        raw = await ws.recv()
                        msg = json.loads(raw)
                        data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
                        queue.put_nowait(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stop:
                    log(f"SNIPER WS {label} reconnect: {exc}")
                    await asyncio.sleep(1)

    async def run(self) -> int:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        stream_symbols = [s.lower() for s in self.symbols]
        public = "/".join(f"{s}@bookTicker" for s in stream_symbols)
        market_streams = [f"{s}@aggTrade" for s in stream_symbols]
        market_streams.append("!forceOrder@arr")
        market = "/".join(market_streams)
        public_url = f"wss://fstream.binance.com/public/stream?streams={public}"
        market_url = f"wss://fstream.binance.com/market/stream?streams={market}"
        tasks = [
            asyncio.create_task(self.consumer(public_url, queue, "book")),
            asyncio.create_task(self.consumer(market_url, queue, "trade")),
        ]
        start = time.time()
        log(
            f"SNIPER START symbols={self.symbols} wallet=${self.wallet:.4f} lev={self.leverage}x "
            f"target=${self.target_usdt:+.2f} loss=${self.loss_usdt:+.2f} turbo={self.turbo} "
            f"fade={self.fade} maker_entry={self.maker_entry}"
        )
        try:
            while not self.stop:
                realized = self.wallet - self.start_wallet
                if realized >= self.target_usdt:
                    log(f"SNIPER target hit {realized:+.4f}")
                    break
                if realized <= self.loss_usdt:
                    log(f"SNIPER loss hit {realized:+.4f}")
                    break
                if not self.position and self.peak_wallet > self.start_wallet and self.wallet < self.lock_floor:
                    log(f"SNIPER profit lock hit wallet=${self.wallet:.4f} floor=${self.lock_floor:.4f}")
                    break
                if self.trades >= self.max_trades:
                    break
                if time.time() - start >= self.seconds:
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1)
                    self.on_event(data)
                except asyncio.TimeoutError:
                    pass
                now = time.time()
                if now - self.last_status >= 10:
                    self.last_status = now
                    pos = "flat" if not self.position else f"{self.position.symbol} {self.position.side} unrl={self.unrealized():+.4f}"
                    log(f"SNIPER STATUS equity=${self.equity():.4f} realized=${realized:+.4f} trades={self.trades} pos={pos}")
        finally:
            if self.position:
                self.close("session-end")
            if self.pending:
                log(f"SNIPER CANCEL {self.pending.symbol} {self.pending.side} maker entry=${fmt(self.pending.entry)} reason=session-end")
                self.pending = None
            self.stop = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        realized = self.wallet - self.start_wallet
        win_rate = self.wins / self.trades * 100 if self.trades else 0.0
        log(
            f"SNIPER FINAL start=${self.start_wallet:.4f} end=${self.wallet:.4f} pnl=${realized:+.4f} "
            f"return={float(realized / self.start_wallet * 100):+.2f}% trades={self.trades} wins={self.wins} winrate={win_rate:.0f}%"
        )
        return 0


def select_symbols(client: BinanceFuturesClient, count: int, extra_skip: set[str] | None = None) -> list[str]:
    skip = SKIP | (extra_skip or set())
    tickers = client.public_get("/fapi/v1/ticker/24hr", {})
    books = {b["symbol"]: b for b in client.public_get("/fapi/v1/ticker/bookTicker", {})}
    scored: list[tuple[float, str]] = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in skip:
            continue
        if not sym.isascii() or not sym.isalnum():
            continue
        try:
            qv = float(t.get("quoteVolume", 0))
            pc = abs(float(t.get("priceChangePercent", 0)))
            book = books.get(sym)
            bid = Decimal(book["bidPrice"])
            ask = Decimal(book["askPrice"])
            mid = (bid + ask) / 2
            spread = float((ask - bid) / mid * 100)
        except Exception:
            continue
        if qv < 40_000_000 or spread > 0.18:
            continue
        recent_score = 0.0
        try:
            kl = client.get_klines(sym, "1m", limit=4)
            if len(kl) >= 4:
                open_3m = Decimal(kl[-4][1])
                close_now = Decimal(kl[-1][4])
                high = max(Decimal(k[2]) for k in kl[-4:])
                low = min(Decimal(k[3]) for k in kl[-4:])
                mom3 = abs(float((close_now / open_3m - 1) * 100)) if open_3m > 0 else 0.0
                range3 = float((high - low) / close_now * 100) if close_now > 0 else 0.0
                recent_score = mom3 * 4.0 + range3 * 1.5
        except Exception:
            pass
        if recent_score < 0.35:
            continue
        score = recent_score + min(qv / 100_000_000, 8.0) * 0.4 + pc * 0.35 - spread * 10
        scored.append((score, sym))
    scored.sort(reverse=True)
    return [sym for _, sym in scored[:count]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--symbols", default="", help="comma-separated symbols, or empty for auto")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--wallet", type=Decimal, default=Decimal("24"))
    ap.add_argument("--target", type=Decimal, default=Decimal("10"))
    ap.add_argument("--loss", type=Decimal, default=Decimal("-4.5"))
    ap.add_argument("--max-trades", type=int, default=12)
    ap.add_argument("--max-hold-sec", type=float, default=70.0)
    ap.add_argument("--leverage", type=int, default=50)
    ap.add_argument("--margin-fraction", type=Decimal, default=Decimal("0.90"))
    ap.add_argument("--tp-pct", type=Decimal, default=Decimal("0.010"))
    ap.add_argument("--sl-pct", type=Decimal, default=Decimal("0.0028"))
    ap.add_argument("--trail-arm-pct", type=Decimal, default=Decimal("0.0055"))
    ap.add_argument("--trail-giveback-pct", type=Decimal, default=Decimal("0.0022"))
    ap.add_argument("--slow-start-sec", type=float, default=8.0)
    ap.add_argument("--slow-start-fee-multiple", type=Decimal, default=Decimal("1.10"))
    ap.add_argument("--min-flow-notional", type=Decimal, default=Decimal("50000"))
    ap.add_argument("--min-v1-pct", type=float, default=0.10)
    ap.add_argument("--min-v3-pct", type=float, default=0.19)
    ap.add_argument("--min-v5-pct", type=float, default=0.0)
    ap.add_argument("--max-v5-pct", type=float, default=999.0)
    ap.add_argument("--min-f1", type=float, default=0.58)
    ap.add_argument("--min-f3", type=float, default=0.45)
    ap.add_argument("--min-signal-score", type=float, default=0.0)
    ap.add_argument("--max-book-edge", type=float, default=999.0)
    ap.add_argument("--crowded-min-flow", type=float, default=1.0)
    ap.add_argument("--required-streak", type=int, default=3)
    ap.add_argument("--required-age-sec", type=float, default=0.25)
    ap.add_argument("--min-book-imbalance", type=float, default=0.05)
    ap.add_argument("--max-spread-pct", type=float, default=0.18)
    ap.add_argument("--side", choices=("BOTH", "LONG", "SHORT"), default="BOTH")
    ap.add_argument("--confirm-delay-sec", type=float, default=0.0)
    ap.add_argument("--confirm-min-move-pct", type=float, default=0.0)
    ap.add_argument("--max-stop-risk-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--snap-profit-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--net-trail-arm-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--net-trail-giveback-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--symbol-sides", default="", help="comma-separated SYMBOL:LONG or SYMBOL:SHORT filters")
    ap.add_argument("--turbo", action="store_true")
    ap.add_argument("--fade", action="store_true", help="fade qualifying bursts instead of chasing them")
    ap.add_argument("--maker-entry", action="store_true", help="dry-only simulated post-only entry before fast taker exit")
    ap.add_argument("--maker-fee", type=Decimal, default=MAKER_FEE)
    ap.add_argument("--maker-fill-timeout-sec", type=float, default=6.0)
    ap.add_argument("--maker-offset-ticks", type=int, default=0)
    ap.add_argument("--skip-symbols", default="", help="comma-separated symbols to exclude from auto selection")
    args = ap.parse_args()

    client = BinanceFuturesClient(timeout=3)
    extra_skip = {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip() and s.strip().upper() not in extra_skip]
    else:
        symbols = select_symbols(client, args.count, extra_skip)
    if not symbols:
        log("No symbols selected.")
        return 2
    symbol_sides: dict[str, str] = {}
    for item in args.symbol_sides.split(","):
        item = item.strip().upper()
        if not item:
            continue
        if ":" not in item:
            log(f"Ignoring bad symbol side filter: {item}")
            continue
        sym, side = item.split(":", 1)
        if side in {"LONG", "SHORT"}:
            symbol_sides[sym] = side
        else:
            log(f"Ignoring bad symbol side filter: {item}")
    bot = SniperPaperBot(
        symbols=symbols,
        wallet=args.wallet,
        leverage=args.leverage,
        margin_fraction=args.margin_fraction,
        target_usdt=args.target,
        loss_usdt=args.loss,
        seconds=args.seconds,
        max_trades=args.max_trades,
        max_hold_sec=args.max_hold_sec,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        trail_arm_pct=args.trail_arm_pct,
        trail_giveback_pct=args.trail_giveback_pct,
        slow_start_sec=args.slow_start_sec,
        slow_start_fee_multiple=args.slow_start_fee_multiple,
        min_flow_notional=args.min_flow_notional,
        min_v1_pct=args.min_v1_pct,
        min_v3_pct=args.min_v3_pct,
        min_v5_pct=args.min_v5_pct,
        max_v5_pct=args.max_v5_pct,
        min_f1=args.min_f1,
        min_f3=args.min_f3,
        min_signal_score=args.min_signal_score,
        max_book_edge=args.max_book_edge,
        crowded_min_flow=args.crowded_min_flow,
        required_streak=args.required_streak,
        required_age_sec=args.required_age_sec,
        min_book_imbalance=args.min_book_imbalance,
        max_spread_pct=args.max_spread_pct,
        side_filter=args.side,
        confirm_delay_sec=args.confirm_delay_sec,
        confirm_min_move_pct=args.confirm_min_move_pct,
        max_stop_risk_usdt=args.max_stop_risk_usdt,
        snap_profit_usdt=args.snap_profit_usdt,
        net_trail_arm_usdt=args.net_trail_arm_usdt,
        net_trail_giveback_usdt=args.net_trail_giveback_usdt,
        symbol_sides=symbol_sides,
        turbo=args.turbo,
        fade=args.fade,
        maker_entry=args.maker_entry,
        maker_fee=args.maker_fee,
        maker_fill_timeout_sec=args.maker_fill_timeout_sec,
        maker_offset_ticks=args.maker_offset_ticks,
    )
    try:
        bot.set_filters(client)
    except BinanceApiError as exc:
        log(f"Filter load failed: {exc}")
    return asyncio.run(bot.run())


if __name__ == "__main__":
    raise SystemExit(main())
