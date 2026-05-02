"""Forward-test sniper signals on live Binance futures data.

This is dry-only telemetry. It never places orders. It watches the same
WebSocket streams as the sniper bot, records candidate signals, and evaluates
whether each candidate would have hit a target or stop first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from sniper_paper_bot import SymbolState, TAKER_FEE, fmt, qdown, select_symbols
from trading_bot.binance_client import BinanceFuturesClient


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


@dataclass
class Probe:
    id: int
    symbol: str
    side: str
    qty: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    entry_fee: Decimal
    start_ts: float
    v1: float
    v3: float
    v5: float
    f1: float
    f3: float
    n1: Decimal
    n3: Decimal
    book: float
    best_net: Decimal = Decimal("-999999")
    worst_net: Decimal = Decimal("999999")
    best_move_pct: Decimal = Decimal("0")
    worst_move_pct: Decimal = Decimal("0")
    closed: bool = False
    reason: str = ""
    close_ts: float = 0.0
    close_price: Decimal = Decimal("0")
    net: Decimal = Decimal("0")

    def age(self) -> float:
        return time.time() - self.start_ts


class SignalProbe:
    def __init__(
        self,
        symbols: list[str],
        seconds: int,
        wallet: Decimal,
        leverage: int,
        margin_fraction: Decimal,
        tp_pct: Decimal,
        sl_pct: Decimal,
        horizon_sec: float,
        min_flow_notional: Decimal,
        min_v1_pct: float,
        min_v3_pct: float,
        min_f1: float,
        min_f3: float,
        required_streak: int,
        required_age_sec: float,
        min_book_imbalance: float,
        max_stop_risk_usdt: Decimal,
        snap_profit_usdt: Decimal,
        net_trail_arm_usdt: Decimal,
        net_trail_giveback_usdt: Decimal,
        out_path: Path,
    ) -> None:
        self.symbols = symbols
        self.seconds = seconds
        self.wallet = wallet
        self.leverage = leverage
        self.margin_fraction = margin_fraction
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.horizon_sec = horizon_sec
        self.min_flow_notional = min_flow_notional
        self.min_v1_pct = min_v1_pct
        self.min_v3_pct = min_v3_pct
        self.min_f1 = min_f1
        self.min_f3 = min_f3
        self.required_streak = required_streak
        self.required_age_sec = required_age_sec
        self.min_book_imbalance = min_book_imbalance
        self.max_stop_risk_usdt = max_stop_risk_usdt
        self.snap_profit_usdt = snap_profit_usdt
        self.net_trail_arm_usdt = net_trail_arm_usdt
        self.net_trail_giveback_usdt = net_trail_giveback_usdt
        self.states = {s: SymbolState(s) for s in symbols}
        self.out_path = out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.probes: list[Probe] = []
        self.closed: list[Probe] = []
        self.next_id = 1
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

    def current_exit(self, st: SymbolState, probe: Probe) -> Decimal:
        return st.bid if probe.side == "LONG" else st.ask

    def candidate_side(self, st: SymbolState) -> tuple[str | None, dict[str, Any]]:
        now = time.time()
        features: dict[str, Any] = {}
        if not st.first_tick_ts or now - st.first_tick_ts < 5.0:
            return None, features
        if not st.first_trade_ts or now - st.first_trade_ts < 3.0:
            return None, features
        if not st.last_book_ts or now - st.last_book_ts > 1.5:
            return None, features
        if st.spread_pct() > 0.18 or len(st.ticks) < 6:
            return None, features

        v1 = st.velocity_pct(1.0)
        v3 = st.velocity_pct(3.0)
        v5 = st.velocity_pct(5.0)
        f1, n1 = st.flow_ratio(1.0)
        f3, n3 = st.flow_ratio(3.0)
        book = st.book_imbalance()
        features = {"v1": v1, "v3": v3, "v5": v5, "f1": f1, "f3": f3, "n1": n1, "n3": n3, "book": book}
        if n3 < self.min_flow_notional:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return None, features

        side: str | None = None
        if v1 >= self.min_v1_pct and v3 >= self.min_v3_pct and f1 >= self.min_f1 and f3 >= self.min_f3:
            side = "LONG"
        elif v1 <= -self.min_v1_pct and v3 <= -self.min_v3_pct and f1 <= -self.min_f1 and f3 <= -self.min_f3:
            side = "SHORT"
        if not side:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return None, features

        direction = 1 if side == "LONG" else -1
        if direction * v5 < self.min_v3_pct * 0.35:
            st.signal_streak = 0
            st.signal_first_ts = 0.0
            return None, features
        if direction * book < self.min_book_imbalance:
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
        if st.signal_streak < self.required_streak or now - st.signal_first_ts < self.required_age_sec:
            return None, features
        return side, features

    def maybe_open_probe(self, st: SymbolState) -> None:
        if any(not p.closed and p.symbol == st.symbol for p in self.probes):
            return
        side, features = self.candidate_side(st)
        if not side:
            return
        entry = st.ask if side == "LONG" else st.bid
        if entry <= 0:
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
        max_notional = self.wallet * self.margin_fraction * Decimal(self.leverage)
        if self.max_stop_risk_usdt > 0:
            risk_per_notional = stop_move + TAKER_FEE * Decimal("2")
            max_notional = min(max_notional, self.max_stop_risk_usdt / risk_per_notional)
        qty = qdown(max_notional / entry, st.step_size)
        if qty * entry < Decimal("5"):
            return
        fee = qty * entry * TAKER_FEE
        probe = Probe(
            id=self.next_id,
            symbol=st.symbol,
            side=side,
            qty=qty,
            entry=entry,
            stop=stop,
            target=target,
            entry_fee=fee,
            start_ts=time.time(),
            v1=features["v1"],
            v3=features["v3"],
            v5=features["v5"],
            f1=features["f1"],
            f3=features["f3"],
            n1=features["n1"],
            n3=features["n3"],
            book=features["book"],
        )
        self.next_id += 1
        self.probes.append(probe)
        log(
            f"PROBE OPEN {probe.id} {probe.symbol} {probe.side} entry=${fmt(entry)} "
            f"target=${fmt(target)} stop=${fmt(stop)} v3={probe.v3:+.3f}% "
            f"flow3={probe.f3:+.2f} n3=${fmt(probe.n3)} book={probe.book:+.2f}"
        )

    def update_probe(self, st: SymbolState, probe: Probe) -> None:
        if probe.closed:
            return
        exit_price = self.current_exit(st, probe)
        if exit_price <= 0:
            return
        gross = (exit_price - probe.entry) * probe.qty if probe.side == "LONG" else (probe.entry - exit_price) * probe.qty
        exit_fee = exit_price * probe.qty * TAKER_FEE
        net = gross - probe.entry_fee - exit_fee
        move = (exit_price / probe.entry - 1) * Decimal("100")
        if probe.side == "SHORT":
            move = -move
        probe.best_net = max(probe.best_net, net)
        probe.worst_net = min(probe.worst_net, net)
        probe.best_move_pct = max(probe.best_move_pct, move)
        probe.worst_move_pct = min(probe.worst_move_pct, move)

        reason = ""
        if self.snap_profit_usdt > 0 and net >= self.snap_profit_usdt:
            reason = "net-profit"
        if (
            not reason
            and self.net_trail_arm_usdt > 0
            and self.net_trail_giveback_usdt > 0
            and probe.best_net >= self.net_trail_arm_usdt
            and probe.best_net - net >= self.net_trail_giveback_usdt
        ):
            reason = "net-trail"
        if probe.side == "LONG":
            if not reason and exit_price <= probe.stop:
                reason = "stop"
            elif not reason and exit_price >= probe.target:
                reason = "target"
        else:
            if not reason and exit_price >= probe.stop:
                reason = "stop"
            elif not reason and exit_price <= probe.target:
                reason = "target"
        if not reason and probe.age() >= self.horizon_sec:
            reason = "horizon"
        if not reason:
            return
        probe.closed = True
        probe.reason = reason
        probe.close_ts = time.time()
        probe.close_price = exit_price
        probe.net = net
        self.closed.append(probe)
        self.write_probe(probe)
        log(
            f"PROBE CLOSE {probe.id} {probe.symbol} {probe.side} reason={reason} "
            f"net=${net:+.4f} best=${probe.best_net:+.4f} worst=${probe.worst_net:+.4f} "
            f"move={move:+.3f}%"
        )

    def close_open_probe(self, probe: Probe, reason: str) -> None:
        if probe.closed:
            return
        st = self.states[probe.symbol]
        exit_price = self.current_exit(st, probe)
        if exit_price <= 0:
            return
        gross = (exit_price - probe.entry) * probe.qty if probe.side == "LONG" else (probe.entry - exit_price) * probe.qty
        exit_fee = exit_price * probe.qty * TAKER_FEE
        net = gross - probe.entry_fee - exit_fee
        move = (exit_price / probe.entry - 1) * Decimal("100")
        if probe.side == "SHORT":
            move = -move
        probe.best_net = max(probe.best_net, net)
        probe.worst_net = min(probe.worst_net, net)
        probe.best_move_pct = max(probe.best_move_pct, move)
        probe.worst_move_pct = min(probe.worst_move_pct, move)
        probe.closed = True
        probe.reason = reason
        probe.close_ts = time.time()
        probe.close_price = exit_price
        probe.net = net
        self.closed.append(probe)
        self.write_probe(probe)
        log(
            f"PROBE CLOSE {probe.id} {probe.symbol} {probe.side} reason={reason} "
            f"net=${net:+.4f} best=${probe.best_net:+.4f} worst=${probe.worst_net:+.4f} "
            f"move={move:+.3f}%"
        )

    def write_probe(self, probe: Probe) -> None:
        payload = asdict(probe)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = str(value)
        with self.out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def on_event(self, data: dict[str, Any]) -> None:
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
                self.update_probe(st, probe)
        self.probes = [p for p in self.probes if not p.closed]
        self.maybe_open_probe(st)

    async def consumer(self, url: str, queue: asyncio.Queue[dict[str, Any]], label: str) -> None:
        while not self.stop:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10, max_queue=4096) as ws:
                    log(f"PROBE WS {label} connected")
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
                    log(f"PROBE WS {label} reconnect: {exc}")
                    await asyncio.sleep(1)

    def summarize(self) -> None:
        total = len(self.closed)
        targets = sum(1 for p in self.closed if p.reason == "target")
        stops = sum(1 for p in self.closed if p.reason == "stop")
        horizons = sum(1 for p in self.closed if p.reason == "horizon")
        net = sum((p.net for p in self.closed), Decimal("0"))
        best = max((p.best_net for p in self.closed), default=Decimal("0"))
        log(
            f"PROBE FINAL closed={total} target={targets} stop={stops} horizon={horizons} "
            f"sum_net=${net:+.4f} best_seen=${best:+.4f} out={self.out_path}"
        )
        by_symbol: dict[str, list[Probe]] = {}
        for probe in self.closed:
            by_symbol.setdefault(probe.symbol, []).append(probe)
        rows = []
        for symbol, probes in by_symbol.items():
            rows.append((sum((p.net for p in probes), Decimal("0")), symbol, len(probes), sum(1 for p in probes if p.reason == "target")))
        for net_sum, symbol, count, target_count in sorted(rows, reverse=True)[:10]:
            log(f"PROBE SYMBOL {symbol} count={count} targets={target_count} net_sum=${net_sum:+.4f}")

    async def run(self) -> int:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8000)
        stream_symbols = [s.lower() for s in self.symbols]
        public = "/".join(f"{s}@bookTicker" for s in stream_symbols)
        market = "/".join([*(f"{s}@aggTrade" for s in stream_symbols), "!forceOrder@arr"])
        tasks = [
            asyncio.create_task(self.consumer(f"wss://fstream.binance.com/public/stream?streams={public}", queue, "book")),
            asyncio.create_task(self.consumer(f"wss://fstream.binance.com/market/stream?streams={market}", queue, "trade")),
        ]
        start = time.time()
        log(f"PROBE START symbols={self.symbols} seconds={self.seconds} lev={self.leverage}x out={self.out_path}")
        try:
            while time.time() - start < self.seconds:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1)
                    if isinstance(data, dict):
                        self.on_event(data)
                except asyncio.TimeoutError:
                    pass
                now = time.time()
                if now - self.last_status >= 15:
                    self.last_status = now
                    log(f"PROBE STATUS closed={len(self.closed)} open={len(self.probes)}")
        finally:
            self.stop = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for probe in list(self.probes):
                self.close_open_probe(probe, "session-end")
            self.summarize()
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--count", type=int, default=24)
    ap.add_argument("--skip-symbols", default="")
    ap.add_argument("--wallet", type=Decimal, default=Decimal("24"))
    ap.add_argument("--leverage", type=int, default=125)
    ap.add_argument("--margin-fraction", type=Decimal, default=Decimal("0.95"))
    ap.add_argument("--tp-pct", type=Decimal, default=Decimal("0.0055"))
    ap.add_argument("--sl-pct", type=Decimal, default=Decimal("0.0009"))
    ap.add_argument("--horizon-sec", type=float, default=30.0)
    ap.add_argument("--min-flow-notional", type=Decimal, default=Decimal("12000"))
    ap.add_argument("--min-v1-pct", type=float, default=0.07)
    ap.add_argument("--min-v3-pct", type=float, default=0.13)
    ap.add_argument("--min-f1", type=float, default=0.50)
    ap.add_argument("--min-f3", type=float, default=0.35)
    ap.add_argument("--required-streak", type=int, default=2)
    ap.add_argument("--required-age-sec", type=float, default=0.10)
    ap.add_argument("--min-book-imbalance", type=float, default=0.0)
    ap.add_argument("--max-stop-risk-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--snap-profit-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--net-trail-arm-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--net-trail-giveback-usdt", type=Decimal, default=Decimal("0"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    client = BinanceFuturesClient(timeout=3)
    skip = {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip() and s.strip().upper() not in skip]
    else:
        symbols = select_symbols(client, args.count, skip)
    if not symbols:
        log("No symbols selected.")
        return 2
    out = Path(args.out) if args.out else PROJECT_ROOT / "logs" / f"signal_probe_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    probe = SignalProbe(
        symbols=symbols,
        seconds=args.seconds,
        wallet=args.wallet,
        leverage=args.leverage,
        margin_fraction=args.margin_fraction,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct,
        horizon_sec=args.horizon_sec,
        min_flow_notional=args.min_flow_notional,
        min_v1_pct=args.min_v1_pct,
        min_v3_pct=args.min_v3_pct,
        min_f1=args.min_f1,
        min_f3=args.min_f3,
        required_streak=args.required_streak,
        required_age_sec=args.required_age_sec,
        min_book_imbalance=args.min_book_imbalance,
        max_stop_risk_usdt=args.max_stop_risk_usdt,
        snap_profit_usdt=args.snap_profit_usdt,
        net_trail_arm_usdt=args.net_trail_arm_usdt,
        net_trail_giveback_usdt=args.net_trail_giveback_usdt,
        out_path=out,
    )
    probe.set_filters(client)
    return asyncio.run(probe.run())


if __name__ == "__main__":
    raise SystemExit(main())
