"""Dry-only scout-and-scale futures scalper.

This never sends Binance orders. It uses live USD-M futures WebSocket data and
paper trades with fee-aware accounting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from sniper_paper_bot import SKIP, SymbolState, TAKER_FEE, fmt, qdown, select_symbols
from trading_bot.binance_client import BinanceApiError, BinanceFuturesClient


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


@dataclass
class ScoutPosition:
    symbol: str
    side: str
    stage: str
    mode: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_fee: Decimal
    entry_ts: float
    best_net: Decimal = Decimal("-999999")
    worst_net: Decimal = Decimal("999999")


class ScoutScalper:
    def __init__(self, args: argparse.Namespace, symbols: list[str], out_path: Path) -> None:
        self.args = args
        self.symbols = symbols
        self.states = {s: SymbolState(s) for s in symbols}
        self.wallet: Decimal = args.wallet
        self.start_wallet: Decimal = args.wallet
        self.position: ScoutPosition | None = None
        self.trades = 0
        self.wins = 0
        self.stop = False
        self.last_status = 0.0
        self.last_entry_ts = 0.0
        self.cooldown_until: dict[tuple[str, str], float] = {}
        self.recent_loss_flip_until: dict[str, tuple[float, str]] = {}
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

    def clean(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {str(k): self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value

    def write_event(self, event: str, payload: dict[str, Any]) -> None:
        row = self.clean({"ts": time.time(), "event": event, **payload})
        with self.out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def exit_price(self, st: SymbolState, side: str) -> Decimal:
        return st.bid if side == "LONG" else st.ask

    def entry_price(self, st: SymbolState, side: str) -> Decimal:
        return st.ask if side == "LONG" else st.bid

    def net_at(self, pos: ScoutPosition, px: Decimal) -> Decimal:
        gross = (px - pos.entry) * pos.qty if pos.side == "LONG" else (pos.entry - px) * pos.qty
        return gross - pos.entry_fee - px * pos.qty * TAKER_FEE

    def compute_order(self, st: SymbolState, side: str, stage: str, entry: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        sl_pct = self.args.scout_sl_pct if stage == "SCOUT" else self.args.full_sl_pct
        tp_pct = self.args.scout_tp_pct if stage == "SCOUT" else self.args.full_tp_pct
        risk_usdt = self.args.scout_risk_usdt if stage == "SCOUT" else self.args.full_risk_usdt
        if side == "LONG":
            stop = qdown(entry * (Decimal("1") - sl_pct), st.tick_size)
            if stop >= entry:
                stop = entry - st.tick_size
            target = qdown(entry * (Decimal("1") + tp_pct), st.tick_size)
            if target <= entry:
                target = entry + st.tick_size
            stop_move = (entry - stop) / entry
        else:
            stop = qdown(entry * (Decimal("1") + sl_pct), st.tick_size)
            if stop <= entry:
                stop = entry + st.tick_size
            target = qdown(entry * (Decimal("1") - tp_pct), st.tick_size)
            if target >= entry:
                target = entry - st.tick_size
            stop_move = (stop - entry) / entry
        risk_per_notional = stop_move + TAKER_FEE * Decimal("2")
        max_notional = self.wallet * self.args.margin_fraction * Decimal(self.args.leverage)
        max_notional = min(max_notional, risk_usdt / risk_per_notional)
        qty = qdown(max_notional / entry, st.step_size)
        fee = qty * entry * TAKER_FEE
        return qty, stop, target, fee

    def signal(self, st: SymbolState) -> tuple[str | None, dict[str, Any]]:
        now = time.time()
        if not st.first_tick_ts or now - st.first_tick_ts < 4:
            return None, {}
        if not st.first_trade_ts or now - st.first_trade_ts < 3:
            return None, {}
        if not st.last_book_ts or now - st.last_book_ts > 1.5:
            return None, {}
        if st.spread_pct() > self.args.max_spread_pct or len(st.ticks) < 6:
            return None, {}
        v1 = st.velocity_pct(1.0)
        v3 = st.velocity_pct(3.0)
        f1, n1 = st.flow_ratio(1.0)
        f3, n3 = st.flow_ratio(3.0)
        book = st.book_imbalance()
        features = {"v1": v1, "v3": v3, "f1": f1, "f3": f3, "n1": n1, "n3": n3, "book": book}
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

    def choose_trade_side(self, signal_side: str, features: dict[str, Any]) -> tuple[str, str]:
        direction = Decimal("1") if signal_side == "LONG" else Decimal("-1")
        crowd = float(direction) * float(features.get("book", 0.0))
        flow = float(direction) * float(features.get("f1", 0.0))
        velocity = abs(float(features.get("v1", 0.0)))
        if self.args.allow_fade and crowd >= self.args.fade_book_threshold and flow >= self.args.fade_flow_threshold and velocity >= self.args.fade_v1_pct:
            return ("SHORT" if signal_side == "LONG" else "LONG"), "fade"
        return signal_side, "follow"

    def open_position(self, st: SymbolState, side: str, stage: str, mode: str) -> bool:
        if self.position:
            return False
        now = time.time()
        if now < self.cooldown_until.get((st.symbol, side), 0.0):
            return False
        if now - self.last_entry_ts < self.args.entry_cooldown_sec:
            return False
        entry = self.entry_price(st, side)
        if entry <= 0:
            return False
        qty, stop, target, fee = self.compute_order(st, side, stage, entry)
        if qty * entry < Decimal("5"):
            return False
        pos = ScoutPosition(st.symbol, side, stage, mode, qty, entry, stop, target, fee, now)
        live_net = self.net_at(pos, self.exit_price(st, side))
        if live_net <= -self.args.max_entry_loss_usdt:
            log(f"SKIP  {stage:<5} {mode:<6} {st.symbol:<14} {side:<5} live_net=${live_net:+.4f}")
            self.cooldown_until[(st.symbol, side)] = now + self.args.reject_cooldown_sec
            return False
        self.wallet -= fee
        self.position = pos
        self.last_entry_ts = now
        self.write_event("open", asdict(pos) | {"live_net": live_net, "wallet": self.wallet})
        log(f"OPEN  {stage:<5} {mode:<6} {st.symbol:<14} {side:<5} entry={fmt(entry):>10} qty={fmt(qty):>10} fee=${fee:.4f} live=${live_net:+.4f}")
        return True

    def close_position(self, reason: str) -> Decimal:
        if not self.position:
            return Decimal("0")
        pos = self.position
        st = self.states[pos.symbol]
        px = self.exit_price(st, pos.side)
        net = self.net_at(pos, px)
        pos.best_net = max(pos.best_net, net)
        pos.worst_net = min(pos.worst_net, net)
        gross = (px - pos.entry) * pos.qty if pos.side == "LONG" else (pos.entry - px) * pos.qty
        exit_fee = px * pos.qty * TAKER_FEE
        self.wallet += gross - exit_fee
        self.trades += 1
        self.wins += 1 if net > 0 else 0
        if net < 0:
            self.cooldown_until[(pos.symbol, pos.side)] = time.time() + self.args.loss_cooldown_sec
        self.write_event("close", asdict(pos) | {"exit": px, "net": net, "wallet": self.wallet, "reason": reason})
        log(f"CLOSE {pos.stage:<5} {pos.mode:<6} {pos.symbol:<14} {pos.side:<5} net=${net:+.4f} wallet=${self.wallet:.4f} best=${pos.best_net:+.4f} reason={reason}")
        self.position = None
        return net

    def maybe_scale_or_flip(self, closed: ScoutPosition, net: Decimal) -> None:
        if closed.stage != "SCOUT":
            return
        st = self.states[closed.symbol]
        if net > 0 and self.args.scale_after_scout_win:
            self.open_position(st, closed.side, "FULL", closed.mode)
        elif net < 0 and self.args.flip_after_scout_loss:
            side = "SHORT" if closed.side == "LONG" else "LONG"
            self.open_position(st, side, "SCOUT", "flip")

    def manage_position(self) -> None:
        if not self.position:
            return
        pos = self.position
        st = self.states[pos.symbol]
        px = self.exit_price(st, pos.side)
        if px <= 0:
            return
        net = self.net_at(pos, px)
        pos.best_net = max(pos.best_net, net)
        pos.worst_net = min(pos.worst_net, net)
        age = time.time() - pos.entry_ts
        reason = ""
        if pos.stage == "SCOUT":
            if net >= self.args.scout_take_usdt:
                reason = "scout-win"
            elif net <= -self.args.scout_stop_usdt:
                reason = "scout-loss"
            elif pos.best_net >= self.args.scout_trail_arm_usdt and pos.best_net - net >= self.args.scout_trail_giveback_usdt:
                reason = "scout-trail"
            elif age >= self.args.scout_max_sec:
                reason = "scout-time"
        else:
            if net >= self.args.full_take_usdt:
                reason = "full-win"
            elif net <= -self.args.full_stop_usdt:
                reason = "full-loss"
            elif pos.best_net >= self.args.full_trail_arm_usdt and pos.best_net - net >= self.args.full_trail_giveback_usdt:
                reason = "full-trail"
            elif age >= self.args.full_max_sec:
                reason = "full-time"
        if not reason:
            if pos.side == "LONG" and px <= pos.stop:
                reason = "price-stop"
            elif pos.side == "SHORT" and px >= pos.stop:
                reason = "price-stop"
            elif pos.side == "LONG" and px >= pos.target:
                reason = "price-target"
            elif pos.side == "SHORT" and px <= pos.target:
                reason = "price-target"
        if reason:
            closed = pos
            net = self.close_position(reason)
            self.maybe_scale_or_flip(closed, net)

    def process_signal(self, st: SymbolState) -> None:
        if self.position or self.trades >= self.args.max_trades:
            return
        side, features = self.signal(st)
        if not side:
            return
        trade_side, mode = self.choose_trade_side(side, features)
        self.write_event("signal", {"symbol": st.symbol, "signal_side": side, "trade_side": trade_side, "mode": mode, "features": features})
        self.open_position(st, trade_side, "SCOUT", mode)

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

    def equity(self) -> Decimal:
        if not self.position:
            return self.wallet
        st = self.states[self.position.symbol]
        px = self.exit_price(st, self.position.side)
        gross = (px - self.position.entry) * self.position.qty if self.position.side == "LONG" else (self.position.entry - px) * self.position.qty
        return self.wallet + gross if px > 0 else self.wallet

    async def run(self) -> int:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10000)
        lower = [s.lower() for s in self.symbols]
        public = "/".join(f"{s}@bookTicker" for s in lower)
        market = "/".join(f"{s}@aggTrade" for s in lower)
        tasks = [
            asyncio.create_task(self.consumer(f"wss://fstream.binance.com/public/stream?streams={public}", queue, "book")),
            asyncio.create_task(self.consumer(f"wss://fstream.binance.com/market/stream?streams={market}", queue, "trade")),
        ]
        start = time.time()
        log(f"SCOUT START symbols={len(self.symbols)} wallet=${self.wallet:.4f} target=${self.args.target:+.2f} out={self.out_path}")
        try:
            while time.time() - start < self.args.seconds:
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
                    pos = "flat" if not self.position else f"{self.position.stage} {self.position.mode} {self.position.symbol} {self.position.side}"
                    log(f"STATUS wallet=${self.wallet:.4f} equity=${self.equity():.4f} trades={self.trades} pos={pos}")
        finally:
            if self.position:
                self.close_position("session-end")
            self.stop = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        pnl = self.wallet - self.start_wallet
        win_rate = self.wins / self.trades * 100 if self.trades else 0
        log(f"SCOUT FINAL start=${self.start_wallet:.4f} end=${self.wallet:.4f} pnl=${pnl:+.4f} trades={self.trades} wins={self.wins} winrate={win_rate:.0f}%")
        self.write_event("final", {"start": self.start_wallet, "end": self.wallet, "pnl": pnl, "trades": self.trades, "wins": self.wins})
        return 0 if pnl >= self.args.target else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--skip-symbols", default="")
    ap.add_argument("--wallet", type=Decimal, default=Decimal("24"))
    ap.add_argument("--target", type=Decimal, default=Decimal("10"))
    ap.add_argument("--loss", type=Decimal, default=Decimal("-8"))
    ap.add_argument("--max-trades", type=int, default=12)
    ap.add_argument("--leverage", type=int, default=180)
    ap.add_argument("--margin-fraction", type=Decimal, default=Decimal("1"))
    ap.add_argument("--scout-risk-usdt", type=Decimal, default=Decimal("0.85"))
    ap.add_argument("--full-risk-usdt", type=Decimal, default=Decimal("4.0"))
    ap.add_argument("--scout-take-usdt", type=Decimal, default=Decimal("0.55"))
    ap.add_argument("--scout-stop-usdt", type=Decimal, default=Decimal("0.75"))
    ap.add_argument("--full-take-usdt", type=Decimal, default=Decimal("3.0"))
    ap.add_argument("--full-stop-usdt", type=Decimal, default=Decimal("3.5"))
    ap.add_argument("--scout-sl-pct", type=Decimal, default=Decimal("0.0013"))
    ap.add_argument("--scout-tp-pct", type=Decimal, default=Decimal("0.0022"))
    ap.add_argument("--full-sl-pct", type=Decimal, default=Decimal("0.0018"))
    ap.add_argument("--full-tp-pct", type=Decimal, default=Decimal("0.0038"))
    ap.add_argument("--scout-trail-arm-usdt", type=Decimal, default=Decimal("0.45"))
    ap.add_argument("--scout-trail-giveback-usdt", type=Decimal, default=Decimal("0.18"))
    ap.add_argument("--full-trail-arm-usdt", type=Decimal, default=Decimal("1.25"))
    ap.add_argument("--full-trail-giveback-usdt", type=Decimal, default=Decimal("0.45"))
    ap.add_argument("--scout-max-sec", type=float, default=8)
    ap.add_argument("--full-max-sec", type=float, default=35)
    ap.add_argument("--scale-after-scout-win", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--flip-after-scout-loss", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--allow-fade", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--fade-book-threshold", type=float, default=0.75)
    ap.add_argument("--fade-flow-threshold", type=float, default=0.70)
    ap.add_argument("--fade-v1-pct", type=float, default=0.10)
    ap.add_argument("--min-flow-notional", type=Decimal, default=Decimal("4000"))
    ap.add_argument("--min-v1-pct", type=float, default=0.035)
    ap.add_argument("--min-v3-pct", type=float, default=0.07)
    ap.add_argument("--min-f1", type=float, default=0.30)
    ap.add_argument("--min-f3", type=float, default=0.15)
    ap.add_argument("--required-streak", type=int, default=2)
    ap.add_argument("--required-age-sec", type=float, default=0.03)
    ap.add_argument("--max-spread-pct", type=float, default=0.18)
    ap.add_argument("--entry-cooldown-sec", type=float, default=0.8)
    ap.add_argument("--loss-cooldown-sec", type=float, default=15)
    ap.add_argument("--reject-cooldown-sec", type=float, default=4)
    ap.add_argument("--max-entry-loss-usdt", type=Decimal, default=Decimal("0.55"))
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
    out = Path(args.out) if args.out else PROJECT_ROOT / "logs" / f"scout_scalper_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    bot = ScoutScalper(args, symbols, out)
    try:
        bot.set_filters(client)
    except BinanceApiError as exc:
        log(f"Filter load failed: {exc}")
    return asyncio.run(bot.run())


if __name__ == "__main__":
    raise SystemExit(main())
