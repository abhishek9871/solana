"""Maker Edge Binance Futures testnet bot.

The previous testnet bots lost mostly by crossing the spread and entering
after weak micro-moves. This bot does the opposite:
- testnet-only real Binance USD-M orders
- post-only maker entries at bid/ask
- one position at a time
- mean-reversion setup: fade short micro-bursts only after 1s flow turns
- reduce-only target limit, market stop if premise breaks
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.binance_client import BinanceApiError
from trading_bot.live_executor import LiveExecutor, load_credentials_from_env


LOG_FILE = PROJECT_ROOT / "logs" / "maker_edge_testnet.log"
TAKER_FEE = 0.0005
MAKER_FEE = 0.0002


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    line = f"[{ts()}] {msg}"
    safe = line.encode("ascii", errors="replace").decode("ascii")
    print(safe, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class BookPoint:
    ts_ms: int
    bid: float
    ask: float
    mid: float


@dataclass
class TradePoint:
    ts_ms: int
    signed_notional: float


@dataclass
class State:
    symbol: str
    books: deque[BookPoint] = field(default_factory=lambda: deque(maxlen=800))
    trades: deque[TradePoint] = field(default_factory=lambda: deque(maxlen=2000))

    def add_book(self, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        self.books.append(BookPoint(now_ms(), bid, ask, (bid + ask) / 2))

    def add_trade(self, price: float, qty: float, buyer_is_maker: bool) -> None:
        notional = price * qty
        if notional <= 0:
            return
        self.trades.append(TradePoint(now_ms(), -notional if buyer_is_maker else notional))

    def book(self) -> BookPoint | None:
        return self.books[-1] if self.books else None

    def spread_bps(self) -> float:
        b = self.book()
        if not b or b.mid <= 0:
            return 9999.0
        return (b.ask - b.bid) / b.mid * 10_000

    def velocity_pct(self, seconds: float) -> float | None:
        if len(self.books) < 2:
            return None
        latest = self.books[-1]
        cutoff = latest.ts_ms - int(seconds * 1000)
        anchor = None
        for p in reversed(self.books):
            anchor = p
            if p.ts_ms <= cutoff:
                break
        if not anchor or anchor.mid <= 0:
            return None
        return (latest.mid / anchor.mid - 1) * 100

    def flow(self, seconds: float) -> tuple[float, float]:
        cutoff = now_ms() - int(seconds * 1000)
        signed = 0.0
        total = 0.0
        for t in reversed(self.trades):
            if t.ts_ms < cutoff:
                break
            signed += t.signed_notional
            total += abs(t.signed_notional)
        if total <= 0:
            return 0.0, 0.0
        return signed / total, total


@dataclass
class Candidate:
    symbol: str
    side: str
    entry: float
    score: float
    spread_bps: float
    v1: float
    v3: float
    flow1: float
    flow3: float
    notional3: float


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry: float
    opened_at: float
    target_order_id: int | None = None
    peak_net: float = -999999.0


class MakerEdgeBot:
    def __init__(
        self,
        executor: LiveExecutor,
        symbols: list[str],
        margin: float,
        leverage: int,
        target_usdt: float,
        loss_usdt: float,
        entry_timeout: float,
        target_bps: float,
        stop_bps: float,
        max_hold: float,
        min_v3: float,
        min_flow1: float,
        min_notional3: float,
        max_spread_bps: float,
        score_min: float,
    ):
        self.executor = executor
        self.client = executor.client
        self.symbols = symbols
        self.states = {s: State(s) for s in symbols}
        self.margin = margin
        self.leverage = leverage
        self.target_usdt = target_usdt
        self.loss_usdt = loss_usdt
        self.entry_timeout = entry_timeout
        self.target_bps = target_bps
        self.stop_bps = stop_bps
        self.max_hold = max_hold
        self.min_v3 = min_v3
        self.min_flow1 = min_flow1
        self.min_notional3 = min_notional3
        self.max_spread_bps = max_spread_bps
        self.score_min = score_min
        self.start_balance = 0.0
        self.balance = 0.0
        self.pending_order: tuple[str, str, int, float, float] | None = None
        self.position: Position | None = None
        self.stop = False
        self.trades = 0
        self.wins = 0
        self.last_action = 0.0
        self.last_hb = 0.0

    def pnl(self) -> float:
        return self.balance - self.start_balance

    def refresh_balance(self) -> float:
        account = self.client.get_account()
        self.balance = float(account.get("totalWalletBalance", 0))
        return self.balance

    def open_exchange_positions(self) -> list[dict]:
        return [p for p in self.client.get_positions() if abs(float(p.get("positionAmt", 0))) > 0]

    def reconcile_before_entry(self) -> bool:
        if self.position is not None:
            return True
        positions = self.open_exchange_positions()
        if not positions:
            return True
        log("RECONCILE unknown exchange position; emergency closing before new entry")
        self.executor.emergency_close_all()
        time.sleep(0.4)
        self.refresh_balance()
        self.last_action = time.time()
        if self.pnl() <= self.loss_usdt:
            log(f"LOSS_HIT ${self.pnl():+.4f}")
            self.stop = True
        return False

    def candidate(self) -> Candidate | None:
        best = None
        for sym, st in self.states.items():
            b = st.book()
            if not b:
                continue
            spread = st.spread_bps()
            if spread > self.max_spread_bps:
                continue
            v1 = st.velocity_pct(1.0)
            v3 = st.velocity_pct(3.0)
            if v1 is None or v3 is None:
                continue
            flow1, _ = st.flow(1.0)
            flow3, notional3 = st.flow(3.0)
            if notional3 < self.min_notional3:
                continue
            # Fade micro-bursts only after the most recent taker flow turns.
            long_ok = v3 <= -self.min_v3 and flow1 >= self.min_flow1 and flow3 > -0.25
            short_ok = v3 >= self.min_v3 and flow1 <= -self.min_flow1 and flow3 < 0.25
            if not long_ok and not short_ok:
                continue
            side = "LONG" if long_ok else "SHORT"
            entry = b.bid if side == "LONG" else b.ask
            score = abs(v3) * 12.0 + abs(flow1) * 1.3 + min(notional3 / 300_000.0, 1.0) * 0.4 - spread / 40.0
            if score < self.score_min:
                continue
            c = Candidate(sym, side, entry, score, spread, v1, v3, flow1, flow3, notional3)
            if best is None or c.score > best.score:
                best = c
        return best

    def setup_symbol(self, symbol: str) -> None:
        try:
            self.client.set_leverage(symbol, self.leverage)
        except BinanceApiError as exc:
            log(f"SET_LEVERAGE_WARN {symbol}: {exc}")
        try:
            self.client.set_margin_type(symbol, "ISOLATED")
        except BinanceApiError:
            pass

    def order_qty(self, symbol: str, price: float) -> str:
        notional = self.margin * self.leverage
        return self.executor.round_quantity(symbol, notional / price)

    def place_entry(self, c: Candidate) -> None:
        if time.time() - self.last_action < 1.0:
            return
        if not self.reconcile_before_entry():
            return
        self.setup_symbol(c.symbol)
        qty = self.order_qty(c.symbol, c.entry)
        if qty == "0":
            return
        order_side = "BUY" if c.side == "LONG" else "SELL"
        price = self.executor.round_price(c.symbol, c.entry)
        log(
            f"ENTRY_POST {c.symbol} {c.side} {order_side} qty={qty} price={price} "
            f"score={c.score:.2f} v1={c.v1:+.3f}% v3={c.v3:+.3f}% "
            f"flow1={c.flow1:+.2f} flow3={c.flow3:+.2f} spread={c.spread_bps:.2f}bps"
        )
        try:
            order = self.client.place_limit_order(c.symbol, order_side, price, qty, time_in_force="GTX")
        except BinanceApiError as exc:
            log(f"ENTRY_REJECT {c.symbol}: {exc}")
            self.last_action = time.time()
            return
        oid = int(order.get("orderId", 0))
        if oid <= 0:
            log(f"ENTRY_NO_ORDER_ID {c.symbol}: {order}")
            self.last_action = time.time()
            return
        self.pending_order = (c.symbol, c.side, oid, time.time(), float(price))
        self.last_action = time.time()

    def check_pending(self) -> None:
        if not self.pending_order:
            return
        symbol, side, oid, created, entry_price = self.pending_order
        try:
            order = self.client.query_order(symbol, order_id=oid)
        except BinanceApiError as exc:
            log(f"ENTRY_QUERY_FAIL {symbol}: {exc}")
            positions = [p for p in self.open_exchange_positions() if p.get("symbol") == symbol]
            if positions:
                pos = positions[0]
                amt = float(pos.get("positionAmt", 0))
                side = "LONG" if amt > 0 else "SHORT"
                qty = abs(amt)
                entry = float(pos.get("entryPrice", entry_price))
                self.position = Position(symbol, side, qty, entry, time.time())
                self.pending_order = None
                self.trades += 1
                log(f"ENTRY_ADOPTED {symbol} {side} qty={qty} entry={entry:.8f}")
                self.place_target()
            return
        status = order.get("status")
        executed = float(order.get("executedQty", 0) or 0)
        if status == "FILLED" and executed > 0:
            avg = float(order.get("avgPrice", 0) or entry_price)
            self.position = Position(symbol, side, executed, avg, time.time())
            self.pending_order = None
            self.trades += 1
            log(f"ENTRY_FILLED {symbol} {side} qty={executed} entry={avg:.8f}")
            self.place_target()
            return
        if time.time() - created >= self.entry_timeout:
            try:
                self.client.cancel_order(symbol, order_id=oid)
            except BinanceApiError:
                pass
            time.sleep(0.2)
            positions = [p for p in self.open_exchange_positions() if p.get("symbol") == symbol]
            if positions:
                pos = positions[0]
                amt = float(pos.get("positionAmt", 0))
                side = "LONG" if amt > 0 else "SHORT"
                qty = abs(amt)
                entry = float(pos.get("entryPrice", entry_price))
                self.position = Position(symbol, side, qty, entry, time.time())
                self.pending_order = None
                self.trades += 1
                log(f"ENTRY_ADOPTED_AFTER_CANCEL {symbol} {side} qty={qty} entry={entry:.8f}")
                self.place_target()
                return
            self.pending_order = None
            log(f"ENTRY_CANCEL {symbol} {side} timeout")

    def place_target(self) -> None:
        p = self.position
        if not p:
            return
        if p.side == "LONG":
            price = p.entry * (1 + self.target_bps / 10_000)
            order_side = "SELL"
        else:
            price = p.entry * (1 - self.target_bps / 10_000)
            order_side = "BUY"
        price_str = self.executor.round_price(p.symbol, price)
        qty_str = self.executor.round_quantity(p.symbol, p.qty)
        try:
            order = self.client.place_limit_order(p.symbol, order_side, price_str, qty_str, time_in_force="GTX", reduce_only=True)
            p.target_order_id = int(order.get("orderId", 0))
            log(f"TARGET_POST {p.symbol} {p.side} qty={qty_str} price={price_str}")
        except BinanceApiError as exc:
            log(f"TARGET_REJECT {p.symbol}: {exc}")

    def current_mid(self, symbol: str) -> float:
        b = self.states[symbol].book()
        return b.mid if b else 0.0

    def net_if_closed(self, p: Position, mid: float) -> float:
        if mid <= 0:
            return 0.0
        gross = (mid - p.entry) * p.qty if p.side == "LONG" else (p.entry - mid) * p.qty
        return gross - (p.entry * p.qty * MAKER_FEE) - (mid * p.qty * TAKER_FEE)

    def check_position(self) -> None:
        p = self.position
        if not p:
            return
        try:
            pos = [x for x in self.client.get_positions(p.symbol) if x.get("symbol") == p.symbol]
            amt = abs(float(pos[0].get("positionAmt", 0))) if pos else 0.0
        except BinanceApiError as exc:
            log(f"POSITION_QUERY_FAIL {p.symbol}: {exc}")
            return
        if amt == 0:
            self.finish_closed("target-fill")
            return
        mid = self.current_mid(p.symbol)
        net = self.net_if_closed(p, mid)
        p.peak_net = max(p.peak_net, net)
        age = time.time() - p.opened_at
        move_bps = ((mid / p.entry - 1) if p.side == "LONG" else (p.entry / mid - 1)) * 10_000 if mid > 0 else 0
        st = self.states[p.symbol]
        flow1, _ = st.flow(1.0)
        flow3, _ = st.flow(3.0)
        if move_bps <= -self.stop_bps:
            self.market_close(f"stop {move_bps:.1f}bps net=${net:.2f}")
        elif age >= 1.0 and ((p.side == "LONG" and flow1 < -0.75 and flow3 < -0.50) or (p.side == "SHORT" and flow1 > 0.75 and flow3 > 0.50)):
            self.market_close(f"flow-break flow1={flow1:+.2f} flow3={flow3:+.2f} net=${net:.2f}")
        elif p.peak_net >= 4.0 and p.peak_net - net >= 1.2:
            self.market_close(f"trail peak=${p.peak_net:.2f} net=${net:.2f}")
        elif age >= self.max_hold:
            self.market_close(f"max-hold net=${net:.2f}")

    def market_close(self, reason: str) -> None:
        p = self.position
        if not p:
            return
        try:
            if p.target_order_id:
                self.client.cancel_order(p.symbol, order_id=p.target_order_id)
        except BinanceApiError:
            pass
        if p.side == "LONG":
            result = self.executor.close_long_position(p.symbol, p.qty)
        else:
            result = self.executor.close_short_position(p.symbol, p.qty)
        self.finish_closed(reason, result.success)

    def finish_closed(self, reason: str, close_ok: bool = True) -> None:
        old = self.balance
        time.sleep(0.25)
        self.refresh_balance()
        delta = self.balance - old
        if delta > 0:
            self.wins += 1
        sym = self.position.symbol if self.position else "-"
        side = self.position.side if self.position else "-"
        log(f"CLOSED {sym} {side} reason={reason} delta=${delta:+.4f} session=${self.pnl():+.4f} ok={close_ok}")
        self.position = None
        self.last_action = time.time()
        if self.pnl() >= self.target_usdt:
            log(f"TARGET_HIT ${self.pnl():+.4f}")
            self.stop = True
        elif self.pnl() <= self.loss_usdt:
            log(f"LOSS_HIT ${self.pnl():+.4f}")
            self.stop = True

    async def on_event(self, raw: str) -> None:
        event = json.loads(raw)
        data = event.get("data", event)
        symbol = data.get("s")
        if symbol not in self.states:
            return
        if data.get("e") == "bookTicker" or ("b" in data and "a" in data):
            self.states[symbol].add_book(float(data["b"]), float(data["a"]))
        elif data.get("e") == "aggTrade":
            self.states[symbol].add_trade(float(data["p"]), float(data["q"]), bool(data.get("m")))
        self.check_pending()
        self.check_position()
        if not self.position and not self.pending_order and not self.stop:
            c = self.candidate()
            if c:
                self.place_entry(c)
        self.heartbeat()

    def heartbeat(self) -> None:
        if time.time() - self.last_hb < 10:
            return
        self.last_hb = time.time()
        rows = []
        for sym, st in self.states.items():
            v3 = st.velocity_pct(3.0)
            flow1, _ = st.flow(1.0)
            if v3 is not None:
                rows.append((abs(v3), f"{sym}:{v3:+.3f}%/{flow1:+.2f}"))
        rows.sort(reverse=True)
        pos = "-"
        if self.position:
            pos = f"{self.position.symbol} {self.position.side}"
        pending = self.pending_order[0] if self.pending_order else "-"
        log(f"HB pnl=${self.pnl():+.4f} trades={self.trades} wins={self.wins} pos={pos} pending={pending} top={' | '.join(x[1] for x in rows[:5])}")

    def start(self) -> None:
        log("START cleanup")
        self.executor.emergency_close_all()
        self.start_balance = self.refresh_balance()
        log(f"START balance=${self.start_balance:.4f} margin=${self.margin} lev={self.leverage}x target=${self.target_usdt:+.2f} loss=${self.loss_usdt:+.2f}")

    async def run(self, seconds: float, ws_url: str) -> None:
        streams = []
        for s in self.symbols:
            streams.append(f"{s.lower()}@bookTicker")
            streams.append(f"{s.lower()}@aggTrade")
        url = f"{ws_url.rstrip('/')}/stream?streams={'/'.join(streams)}"
        started = time.time()
        while not self.stop and time.time() - started < seconds:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=2048) as ws:
                    log(f"WS_CONNECTED symbols={len(self.symbols)}")
                    while not self.stop and time.time() - started < seconds:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        await self.on_event(raw)
            except Exception as exc:
                log(f"WS_RECONNECT {exc}")
                await asyncio.sleep(1)
        log(f"TIMED_STOP {seconds:.1f}s")

    def shutdown(self) -> None:
        if self.pending_order:
            symbol, _, oid, _, _ = self.pending_order
            try:
                self.client.cancel_order(symbol, order_id=oid)
            except BinanceApiError:
                pass
            self.pending_order = None
        if self.position:
            self.market_close("shutdown")
        self.refresh_balance()
        winrate = self.wins / self.trades * 100 if self.trades else 0.0
        log(f"FINAL balance=${self.balance:.4f} pnl=${self.pnl():+.4f} trades={self.trades} wins={self.wins} winrate={winrate:.0f}%")


BOT: MakerEdgeBot | None = None


def stop_handler(signum, frame) -> None:
    if BOT:
        BOT.stop = True
        BOT.shutdown()
    raise SystemExit(0)


def main() -> int:
    global BOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT")
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--margin", type=float, default=250)
    ap.add_argument("--leverage", type=int, default=20)
    ap.add_argument("--target", type=float, default=10)
    ap.add_argument("--loss", type=float, default=-10)
    ap.add_argument("--entry-timeout", type=float, default=3.0)
    ap.add_argument("--target-bps", type=float, default=28)
    ap.add_argument("--stop-bps", type=float, default=18)
    ap.add_argument("--max-hold", type=float, default=45)
    ap.add_argument("--min-v3", type=float, default=0.025)
    ap.add_argument("--min-flow1", type=float, default=0.60)
    ap.add_argument("--min-notional3", type=float, default=1000)
    ap.add_argument("--max-spread-bps", type=float, default=8)
    ap.add_argument("--score-min", type=float, default=1.2)
    ap.add_argument("--base-url", default="https://testnet.binancefuture.com")
    ap.add_argument("--ws-url", default="wss://stream.binancefuture.com")
    args = ap.parse_args()

    key, secret = load_credentials_from_env("BINANCE_TESTNET")
    if not key or not secret:
        log("ERROR missing BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET_KEY")
        return 1
    executor = LiveExecutor(key, secret, max_margin_per_trade=args.margin, base_url=args.base_url)
    BOT = MakerEdgeBot(
        executor=executor,
        symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
        margin=args.margin,
        leverage=args.leverage,
        target_usdt=args.target,
        loss_usdt=args.loss,
        entry_timeout=args.entry_timeout,
        target_bps=args.target_bps,
        stop_bps=args.stop_bps,
        max_hold=args.max_hold,
        min_v3=args.min_v3,
        min_flow1=args.min_flow1,
        min_notional3=args.min_notional3,
        max_spread_bps=args.max_spread_bps,
        score_min=args.score_min,
    )
    signal.signal(signal.SIGINT, stop_handler)
    try:
        signal.signal(signal.SIGTERM, stop_handler)
    except Exception:
        pass
    try:
        BOT.start()
        asyncio.run(BOT.run(args.seconds, args.ws_url))
    finally:
        BOT.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
