"""Swift Edge testnet bot.

Fresh Binance USD-M Futures testnet trader:
- real Binance testnet REST orders
- real Binance testnet WebSocket market data
- no local paper fills
- one position at a time
- exits from live price, trailing PnL, target, loss, or timeout

This is testnet-only by default. It will not use mainnet keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
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

from trading_bot.live_executor import LiveExecutor, load_credentials_from_env


LOG_FILE = PROJECT_ROOT / "logs" / "swift_edge_testnet.log"


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
class Signal:
    symbol: str
    side: str
    score: float
    mid: float
    spread_bps: float
    v1: float
    v3: float
    v8: float
    flow1: float
    flow3: float
    notional3: float


@dataclass
class LocalPosition:
    symbol: str
    side: str
    qty: float
    entry: float
    opened_ms: int
    peak_net: float = -1_000_000.0
    last_net: float = 0.0


@dataclass
class SymbolState:
    symbol: str
    books: deque[BookPoint] = field(default_factory=lambda: deque(maxlen=600))
    trades: deque[TradePoint] = field(default_factory=lambda: deque(maxlen=1500))

    def add_book(self, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        self.books.append(BookPoint(now_ms(), bid, ask, (bid + ask) / 2.0))

    def add_trade(self, price: float, qty: float, buyer_is_maker: bool) -> None:
        notional = price * qty
        if notional <= 0:
            return
        # buyer_is_maker=True means seller was taker, so flow is negative.
        signed = -notional if buyer_is_maker else notional
        self.trades.append(TradePoint(now_ms(), signed))

    def mid(self) -> float:
        return self.books[-1].mid if self.books else 0.0

    def spread_bps(self) -> float:
        if not self.books:
            return 9999.0
        b = self.books[-1]
        if b.mid <= 0:
            return 9999.0
        return (b.ask - b.bid) / b.mid * 10_000.0

    def velocity_pct(self, seconds: float) -> float | None:
        if len(self.books) < 2:
            return None
        latest = self.books[-1]
        cutoff = latest.ts_ms - int(seconds * 1000)
        anchor = None
        for b in reversed(self.books):
            anchor = b
            if b.ts_ms <= cutoff:
                break
        if anchor is None or anchor.mid <= 0:
            return None
        return (latest.mid / anchor.mid - 1.0) * 100.0

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


class SwiftEdgeBot:
    def __init__(
        self,
        executor: LiveExecutor,
        symbols: list[str],
        margin: float,
        leverage: int,
        session_target: float,
        session_loss: float,
        tp_pct: float,
        sl_pct: float,
        max_hold: float,
        score_min: float,
        min_v1: float,
        min_v3: float,
        min_flow: float,
        max_spread_bps: float,
        trail_arm: float,
        trail_giveback: float,
        confirm_ms: int,
    ):
        self.executor = executor
        self.symbols = symbols
        self.states = {s: SymbolState(s) for s in symbols}
        self.margin = margin
        self.leverage = leverage
        self.session_target = session_target
        self.session_loss = session_loss
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold = max_hold
        self.score_min = score_min
        self.min_v1 = min_v1
        self.min_v3 = min_v3
        self.min_flow = min_flow
        self.max_spread_bps = max_spread_bps
        self.trail_arm = trail_arm
        self.trail_giveback = trail_giveback
        self.confirm_ms = confirm_ms
        self.position: LocalPosition | None = None
        self.pending_key: tuple[str, str] | None = None
        self.pending_since_ms = 0
        self.stop = False
        self.start_balance = 0.0
        self.last_balance = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.last_close_ts = 0.0
        self.last_heartbeat = 0.0
        self.last_decision = 0.0

    def pnl_now(self) -> float:
        return self.last_balance - self.start_balance

    def best_signal(self) -> Signal | None:
        best: Signal | None = None
        for symbol, st in self.states.items():
            if len(st.books) < 4:
                continue
            spread = st.spread_bps()
            if spread > self.max_spread_bps:
                continue
            v1 = st.velocity_pct(1.0)
            v3 = st.velocity_pct(3.0)
            v8 = st.velocity_pct(8.0)
            if v1 is None or v3 is None or v8 is None:
                continue
            flow1, _ = st.flow(1.0)
            flow3, notional3 = st.flow(3.0)
            long_ok = v1 >= self.min_v1 and v3 >= self.min_v3 and flow1 > 0 and flow3 >= self.min_flow
            short_ok = v1 <= -self.min_v1 and v3 <= -self.min_v3 and flow1 < 0 and flow3 <= -self.min_flow
            if not long_ok and not short_ok:
                continue
            side = "LONG" if long_ok else "SHORT"
            score = (
                abs(v1) * 9.0
                + abs(v3) * 5.0
                + abs(v8) * 1.5
                + abs(flow3) * 1.25
                + min(notional3 / 250_000.0, 1.0) * 0.35
                - spread / 30.0
            )
            if score < self.score_min:
                continue
            sig = Signal(symbol, side, score, st.mid(), spread, v1, v3, v8, flow1, flow3, notional3)
            if best is None or sig.score > best.score:
                best = sig
        return best

    def mark_to_market(self, p: LocalPosition, mid: float) -> float:
        if mid <= 0:
            return p.last_net
        gross = (mid - p.entry) * p.qty if p.side == "LONG" else (p.entry - mid) * p.qty
        fees = (p.qty * p.entry + p.qty * mid) * 0.0005
        return gross - fees

    def refresh_balance(self) -> float:
        self.last_balance = self.executor.get_usdt_balance()
        return self.last_balance

    def open_position(self, sig: Signal) -> None:
        if self.position is not None or time.time() - self.last_close_ts < 2.0:
            return
        log(
            f"OPEN_SIGNAL {sig.symbol} {sig.side} score={sig.score:.2f} "
            f"v1={sig.v1:+.3f}% v3={sig.v3:+.3f}% v8={sig.v8:+.3f}% "
            f"flow1={sig.flow1:+.2f} flow3={sig.flow3:+.2f} spread={sig.spread_bps:.2f}bps"
        )
        if sig.side == "LONG":
            result = self.executor.open_long_position(sig.symbol, self.margin, self.leverage, self.sl_pct, self.tp_pct)
        else:
            result = self.executor.open_short_position(sig.symbol, self.margin, self.leverage, self.sl_pct, self.tp_pct)
        if not result.success or result.executed_qty <= 0:
            log(f"OPEN_FAILED {sig.symbol} {sig.side}: {result.error}")
            return
        self.trade_count += 1
        self.position = LocalPosition(sig.symbol, sig.side, result.executed_qty, result.avg_fill_price or sig.mid, now_ms())
        log(f"OPENED {sig.symbol} {sig.side} qty={result.executed_qty} entry={self.position.entry:.8f}")

    def close_position(self, reason: str) -> None:
        p = self.position
        if p is None:
            return
        if p.side == "LONG":
            result = self.executor.close_long_position(p.symbol, p.qty)
        else:
            result = self.executor.close_short_position(p.symbol, p.qty)
        time.sleep(0.35)
        old = self.last_balance
        bal = self.refresh_balance()
        delta = bal - old
        if delta > 0:
            self.win_count += 1
        log(
            f"CLOSED {p.symbol} {p.side} reason={reason} balance_delta=${delta:+.4f} "
            f"session=${self.pnl_now():+.4f} close_ok={result.success}"
        )
        self.position = None
        self.last_close_ts = time.time()
        if self.pnl_now() >= self.session_target:
            log(f"SESSION_TARGET_HIT ${self.pnl_now():+.4f}")
            self.stop = True
        elif self.pnl_now() <= self.session_loss:
            log(f"SESSION_LOSS_HIT ${self.pnl_now():+.4f}")
            self.stop = True

    def manage_position(self) -> None:
        p = self.position
        if p is None:
            return
        st = self.states.get(p.symbol)
        mid = st.mid() if st else 0.0
        if mid <= 0:
            return
        net = self.mark_to_market(p, mid)
        p.last_net = net
        p.peak_net = max(p.peak_net, net)
        age = (now_ms() - p.opened_ms) / 1000.0
        move = (mid / p.entry - 1.0) if p.side == "LONG" else (p.entry / mid - 1.0)
        if net >= self.session_target - self.pnl_now():
            self.close_position("target-reachable")
            return
        if move >= self.tp_pct:
            self.close_position("tp-price")
            return
        if move <= -self.sl_pct:
            self.close_position("sl-price")
            return
        if p.peak_net >= self.trail_arm and p.peak_net - net >= self.trail_giveback:
            self.close_position(f"trail peak=${p.peak_net:.2f}")
            return
        if age >= self.max_hold:
            self.close_position("max-hold")
            return
        sig = self.best_signal()
        flow1, _ = st.flow(1.0)
        flow3, _ = st.flow(3.0)
        v1 = st.velocity_pct(1.0) or 0.0
        if age >= 1.0:
            long_bad = p.side == "LONG" and flow1 < -0.60 and flow3 < -0.40 and v1 <= 0
            short_bad = p.side == "SHORT" and flow1 > 0.60 and flow3 > 0.40 and v1 >= 0
            if long_bad or short_bad:
                self.close_position(f"flow-flip flow1={flow1:+.2f} flow3={flow3:+.2f} v1={v1:+.3f}%")
                return
        if sig and sig.symbol == p.symbol and sig.side != p.side and sig.score >= self.score_min * 1.25:
            self.close_position(f"opposite-signal {sig.side} score={sig.score:.2f}")

    def heartbeat(self) -> None:
        if time.time() - self.last_heartbeat < 10:
            return
        self.last_heartbeat = time.time()
        rows = []
        for symbol, st in self.states.items():
            v3 = st.velocity_pct(3.0)
            flow3, _ = st.flow(3.0)
            if v3 is None:
                continue
            rows.append((abs(v3), f"{symbol}:{v3:+.3f}%/{flow3:+.2f}"))
        rows.sort(reverse=True)
        pos = "-"
        if self.position:
            pos = f"{self.position.symbol} {self.position.side} net=${self.position.last_net:+.2f}"
        log(
            f"HB pnl=${self.pnl_now():+.4f} trades={self.trade_count} wins={self.win_count} "
            f"pos={pos} top={' | '.join(x[1] for x in rows[:5])}"
        )

    async def on_event(self, event: dict) -> None:
        data = event.get("data", event)
        symbol = data.get("s")
        if symbol not in self.states:
            return
        st = self.states[symbol]
        event_type = data.get("e")
        if event_type == "bookTicker" or ("b" in data and "a" in data):
            try:
                st.add_book(float(data["b"]), float(data["a"]))
            except Exception:
                return
        elif event_type == "aggTrade":
            try:
                st.add_trade(float(data["p"]), float(data["q"]), bool(data.get("m")))
            except Exception:
                return
        self.manage_position()
        self.heartbeat()
        if self.position is None and not self.stop and time.time() - self.last_decision >= 0.20:
            self.last_decision = time.time()
            sig = self.best_signal()
            if sig:
                key = (sig.symbol, sig.side)
                if self.pending_key != key:
                    self.pending_key = key
                    self.pending_since_ms = now_ms()
                    return
                if now_ms() - self.pending_since_ms >= self.confirm_ms:
                    self.pending_key = None
                    self.open_position(sig)
            else:
                self.pending_key = None

    async def run_ws(self, ws_base: str, max_seconds: float) -> None:
        streams = []
        for s in self.symbols:
            streams.append(f"{s.lower()}@bookTicker")
            streams.append(f"{s.lower()}@aggTrade")
        url = f"{ws_base.rstrip('/')}/stream?streams={'/'.join(streams)}"
        started = time.time()
        while not self.stop:
            if max_seconds > 0 and time.time() - started >= max_seconds:
                log(f"TIMED_STOP {max_seconds:.1f}s")
                break
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=2048) as ws:
                    log(f"WS_CONNECTED symbols={len(self.symbols)}")
                    while not self.stop:
                        if max_seconds > 0 and time.time() - started >= max_seconds:
                            log(f"TIMED_STOP {max_seconds:.1f}s")
                            return
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        await self.on_event(json.loads(raw))
            except asyncio.TimeoutError:
                log("WS_TIMEOUT reconnecting")
            except Exception as exc:
                log(f"WS_ERROR {exc}; reconnecting")
                await asyncio.sleep(1)

    def start(self) -> None:
        log("Closing any pre-existing testnet positions before start")
        self.executor.emergency_close_all()
        time.sleep(0.5)
        self.start_balance = self.refresh_balance()
        log(f"START balance=${self.start_balance:.4f} margin=${self.margin} lev={self.leverage}x target=${self.session_target:+.2f} loss=${self.session_loss:+.2f}")

    def shutdown(self) -> None:
        if self.position:
            self.close_position("shutdown")
        self.refresh_balance()
        win_rate = (self.win_count / self.trade_count * 100.0) if self.trade_count else 0.0
        log(f"FINAL balance=${self.last_balance:.4f} pnl=${self.pnl_now():+.4f} trades={self.trade_count} wins={self.win_count} winrate={win_rate:.0f}%")


_BOT: SwiftEdgeBot | None = None


def handle_signal(signum, frame) -> None:
    log(f"STOP_SIGNAL {signum}")
    if _BOT:
        _BOT.stop = True
        _BOT.shutdown()
    raise SystemExit(0)


def main() -> int:
    global _BOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT")
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--margin", type=float, default=100.0)
    ap.add_argument("--leverage", type=int, default=20)
    ap.add_argument("--target", type=float, default=10.0)
    ap.add_argument("--loss", type=float, default=-8.0)
    ap.add_argument("--tp-pct", type=float, default=0.005)
    ap.add_argument("--sl-pct", type=float, default=0.0035)
    ap.add_argument("--max-hold", type=float, default=90.0)
    ap.add_argument("--score-min", type=float, default=1.55)
    ap.add_argument("--min-v1", type=float, default=0.015)
    ap.add_argument("--min-v3", type=float, default=0.030)
    ap.add_argument("--min-flow", type=float, default=0.65)
    ap.add_argument("--max-spread-bps", type=float, default=10.0)
    ap.add_argument("--trail-arm", type=float, default=4.0)
    ap.add_argument("--trail-giveback", type=float, default=1.5)
    ap.add_argument("--confirm-ms", type=int, default=400)
    ap.add_argument("--base-url", default="https://testnet.binancefuture.com")
    ap.add_argument("--ws-url", default="wss://stream.binancefuture.com")
    args = ap.parse_args()

    api_key, secret = load_credentials_from_env("BINANCE_TESTNET")
    if not api_key or not secret:
        log("ERROR missing BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_SECRET_KEY")
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    executor = LiveExecutor(
        api_key=api_key,
        secret_key=secret,
        max_margin_per_trade=args.margin,
        base_url=args.base_url,
    )
    bot = SwiftEdgeBot(
        executor=executor,
        symbols=symbols,
        margin=args.margin,
        leverage=args.leverage,
        session_target=args.target,
        session_loss=args.loss,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        max_hold=args.max_hold,
        score_min=args.score_min,
        min_v1=args.min_v1,
        min_v3=args.min_v3,
        min_flow=args.min_flow,
        max_spread_bps=args.max_spread_bps,
        trail_arm=args.trail_arm,
        trail_giveback=args.trail_giveback,
        confirm_ms=args.confirm_ms,
    )
    _BOT = bot
    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass
    try:
        bot.start()
        asyncio.run(bot.run_ws(args.ws_url, args.seconds))
    finally:
        bot.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
