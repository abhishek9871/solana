"""Crypto Futures Grid Trading Bot.

Built for chop markets. Places a ladder of buy/sell limit orders across a
defined price range. Each oscillation between grid levels = a small profit.

Strategy:
  - Define a price range [low, high] you expect price to oscillate within.
  - Bot divides range into N grid lines.
  - Below current price: places LIMIT BUY orders (post-only / maker).
  - Above current price: places LIMIT SELL orders (post-only / maker).
  - When a BUY fills (price dipped to that level): bot places a SELL one
    level higher (locks profit on the bounce up).
  - When a SELL fills (price rose to that level): bot places a BUY one
    level lower (re-enters at the low for next cycle).
  - Profit per cycle = grid_spacing - (2 x maker fee) - any slippage.

Stop conditions:
  - Price breaks above HIGH or below LOW (range invalidated).
  - User signals stop (Ctrl+C).
  - Cumulative loss exceeds session limit.

Usage:
  py grid_bot.py --symbol DAMUSDT --low 0.045 --high 0.055 --grids 10 \
                 --total-margin 20 --leverage 2 [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import websockets

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

LOG_FILE = PROJECT_ROOT / "logs" / "grid_bot.log"


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    line = f"[{now_str()}] {msg}"
    safe = line.encode("ascii", errors="replace").decode("ascii")
    print(safe, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def round_to_tick(price: float, tick_size: float) -> float:
    return math.floor(price / tick_size) * tick_size


def round_to_step(qty: float, step_size: float) -> float:
    return math.floor(qty / step_size) * step_size


def fmt_price(price: float, tick_size: float) -> str:
    if tick_size >= 1:
        return f"{price:.0f}"
    decimals = max(0, -int(math.floor(math.log10(tick_size))))
    return f"{price:.{decimals}f}"


def fmt_qty(qty: float, step_size: float) -> str:
    if step_size >= 1:
        return f"{qty:.0f}"
    decimals = max(0, -int(math.floor(math.log10(step_size))))
    return f"{qty:.{decimals}f}"


@dataclass
class GridLevel:
    index: int
    price: float
    side: str = "NONE"
    order_id: Optional[int] = None
    fill_qty: float = 0.0


@dataclass
class GridStats:
    cycles_completed: int = 0
    realized_pnl: float = 0.0
    total_buys_filled: int = 0
    total_sells_filled: int = 0
    fees_paid: float = 0.0


class GridBot:
    def __init__(self, client, symbol, low, high, num_grids, total_margin,
                 leverage, dry_run, max_loss, maker_fee_pct=0.0004):
        self.client = client
        self.symbol = symbol
        self.low = low
        self.high = high
        self.num_grids = num_grids
        self.total_margin = total_margin
        self.leverage = leverage
        self.dry_run = dry_run
        self.max_loss = max_loss
        self.maker_fee_pct = maker_fee_pct
        self.tick_size = 0.0
        self.step_size = 0.0
        self.min_qty = 0.0
        self.min_notional = 0.0
        self.grid: list[GridLevel] = []
        self.spacing = 0.0
        self.qty_per_level = 0.0
        self.stats = GridStats()
        self.stop_flag = False
        self.last_price = 0.0

    def fetch_symbol_filters(self):
        info = self.client.get_symbol_info(self.symbol)
        for f in info.get("filters", []):
            t = f.get("filterType")
            if t == "PRICE_FILTER":
                self.tick_size = float(f.get("tickSize", 0))
            elif t == "LOT_SIZE":
                self.step_size = float(f.get("stepSize", 0))
                self.min_qty = float(f.get("minQty", 0))
            elif t == "MIN_NOTIONAL":
                self.min_notional = float(f.get("notional", 0))
        if not self.tick_size or not self.step_size:
            raise RuntimeError(f"Missing tick/step filter for {self.symbol}")
        log(f"Filters: tick={self.tick_size} step={self.step_size} min_qty={self.min_qty} min_notional={self.min_notional}")

    def setup_leverage(self):
        try:
            self.client.set_leverage(self.symbol, self.leverage)
            log(f"Leverage set to {self.leverage}x")
        except BinanceApiError as exc:
            log(f"WARN: set_leverage: {exc}")

    def get_current_price(self) -> float:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
                         params={"symbol": self.symbol}, timeout=5)
        return float(r.json()["price"])

    def build_grid(self, current_price):
        if not (self.low < current_price < self.high):
            raise RuntimeError(f"current price {current_price} not in range [{self.low}, {self.high}]")
        self.spacing = (self.high - self.low) / self.num_grids
        for i in range(self.num_grids + 1):
            raw = self.low + i * self.spacing
            self.grid.append(GridLevel(index=i, price=round_to_tick(raw, self.tick_size)))
        mid = (self.low + self.high) / 2
        notional_per_level = (self.total_margin * self.leverage) / self.num_grids
        raw_qty = notional_per_level / mid
        self.qty_per_level = round_to_step(raw_qty, self.step_size)
        if self.qty_per_level < self.min_qty:
            raise RuntimeError(f"qty {self.qty_per_level} < min_qty {self.min_qty}. Increase margin/leverage or reduce grids.")
        if self.qty_per_level * mid < self.min_notional:
            raise RuntimeError(f"notional {self.qty_per_level * mid} < min {self.min_notional}")
        log(f"Grid: {self.num_grids} levels {self.low}-{self.high} spacing={self.spacing} qty/level={self.qty_per_level}")
        gross_pct = (self.spacing / mid) * 100
        net_pct = gross_pct - (self.maker_fee_pct * 2 * 100)
        log(f"Per cycle: gross {gross_pct:.3f}%, net {net_pct:.3f}% on notional, "
            f"~${self.qty_per_level * self.spacing - 2 * self.maker_fee_pct * self.qty_per_level * mid:.4f}")

    def place_initial_orders(self, current_price):
        for level in self.grid:
            if level.price < current_price:
                self._place_order(level, "BUY")
            elif level.price > current_price:
                self._place_order(level, "SELL")
        active = sum(1 for l in self.grid if l.order_id is not None)
        log(f"Initial: {active} active orders")

    def _place_order(self, level, side):
        price_str = fmt_price(level.price, self.tick_size)
        qty_str = fmt_qty(self.qty_per_level, self.step_size)
        if self.dry_run:
            level.side = side
            level.order_id = -level.index - 1
            log(f"  [DRY] {side} L{level.index} @ {price_str} qty={qty_str}")
            return
        try:
            r = self.client.place_limit_order(
                symbol=self.symbol, side=side,
                price=price_str, quantity=qty_str,
                time_in_force="GTX",
            )
            level.side = side
            level.order_id = int(r.get("orderId", 0))
            log(f"  {side} L{level.index} @ {price_str} qty={qty_str} id={level.order_id}")
        except BinanceApiError as exc:
            err_str = str(exc).lower()
            if "would immediately match" in err_str or "-2021" in err_str:
                log(f"  WARN GTX rejected L{level.index} @ {price_str}")
            else:
                log(f"  ERROR placing {side} @ {price_str}: {exc}")

    def on_fill(self, order_id, side, fill_price, fill_qty):
        level = next((l for l in self.grid if l.order_id == order_id), None)
        if level is None:
            return
        log(f"FILL: {side} L{level.index} @ {fill_price} qty={fill_qty}")
        if side == "BUY":
            self.stats.total_buys_filled += 1
            level.fill_qty = fill_qty
            level.side = "FILLED_LONG"
            level.order_id = None
            up = next((l for l in self.grid if l.index == level.index + 1), None)
            if up is not None and up.side == "NONE":
                self._place_order(up, "SELL")
        elif side == "SELL":
            self.stats.total_sells_filled += 1
            below = next((l for l in self.grid
                          if l.index == level.index - 1 and l.side == "FILLED_LONG"), None)
            if below is not None:
                gross = (fill_price - below.price) * fill_qty
                fees = (fill_price + below.price) * fill_qty * self.maker_fee_pct
                cycle_pnl = gross - fees
                self.stats.realized_pnl += cycle_pnl
                self.stats.fees_paid += fees
                self.stats.cycles_completed += 1
                below.side = "NONE"
                below.fill_qty = 0.0
                log(f"  CYCLE: bought @ {below.price} sold @ {fill_price} pnl=${cycle_pnl:+.4f} "
                    f"total=${self.stats.realized_pnl:+.4f} cycles={self.stats.cycles_completed}")
            level.side = "NONE"
            level.order_id = None
            self._place_order(level, "BUY")
        if self.stats.realized_pnl <= self.max_loss:
            log(f"!!! Session loss limit ${self.stats.realized_pnl:+.2f} hit — stopping")
            self.stop_flag = True

    def on_price(self, mid):
        self.last_price = mid
        if mid > self.high * 1.001:
            log(f"!!! Price {mid} above high {self.high} — stopping")
            self.stop_flag = True
        elif mid < self.low * 0.999:
            log(f"!!! Price {mid} below low {self.low} — stopping")
            self.stop_flag = True

    def cancel_all(self):
        if self.dry_run:
            log("DRY: would cancel all")
            return
        try:
            self.client.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": self.symbol})
            log("All open orders cancelled")
        except Exception as exc:
            log(f"ERROR cancel: {exc}")

    def close_open_positions(self):
        """Emergency: close any open position on this symbol via market order."""
        if self.dry_run:
            log("DRY: would close any open positions")
            return
        try:
            r = self.client.signed_request("GET", "/fapi/v2/positionRisk", {"symbol": self.symbol})
            for pos in r:
                if pos.get("symbol") != self.symbol:
                    continue
                amt = float(pos.get("positionAmt", 0))
                if abs(amt) < 1e-9:
                    continue
                side = "SELL" if amt > 0 else "BUY"
                qty_str = fmt_qty(abs(amt), self.step_size)
                log(f"Closing open position: {amt} {self.symbol} via {side} MARKET")
                self.client.place_market_order(
                    symbol=self.symbol, side=side,
                    quantity=qty_str, reduce_only=True,
                )
                log("Position closed")
        except Exception as exc:
            log(f"ERROR closing position: {exc}")


async def ws_book_ticker(bot):
    url = f"wss://fstream.binance.com/public/ws/{bot.symbol.lower()}@bookTicker"
    while not bot.stop_flag:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                log("WS bookTicker connected")
                while not bot.stop_flag:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        break
                    msg = json.loads(raw)
                    bid = float(msg.get("b", 0))
                    ask = float(msg.get("a", 0))
                    if bid > 0 and ask > 0:
                        bot.on_price((bid + ask) / 2)
        except Exception as exc:
            log(f"WS bookTicker err: {exc}")
        if bot.stop_flag:
            break
        await asyncio.sleep(3)


async def ws_user_data(bot, api_key):
    last_keepalive = time.time()

    def get_listen_key():
        try:
            r = requests.post("https://fapi.binance.com/fapi/v1/listenKey",
                              headers={"X-MBX-APIKEY": api_key}, timeout=10)
            return r.json().get("listenKey") if r.status_code == 200 else None
        except Exception:
            return None

    while not bot.stop_flag:
        listen_key = get_listen_key()
        if not listen_key:
            log("listenKey fetch failed, retry...")
            await asyncio.sleep(15)
            continue
        url = f"wss://fstream.binance.com/private/ws?listenKey={listen_key}&events=ORDER_TRADE_UPDATE"
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                log("WS userData connected")
                while not bot.stop_flag:
                    if time.time() - last_keepalive > 1500:
                        try:
                            requests.put("https://fapi.binance.com/fapi/v1/listenKey",
                                         headers={"X-MBX-APIKEY": api_key}, timeout=10)
                            last_keepalive = time.time()
                        except Exception:
                            pass
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("e") != "ORDER_TRADE_UPDATE":
                        continue
                    o = msg.get("o", {})
                    if o.get("s") != bot.symbol or o.get("X") != "FILLED":
                        continue
                    order_id = int(o.get("i", 0))
                    side = o.get("S", "")
                    fill_price = float(o.get("ap", 0))
                    fill_qty = float(o.get("z", 0))
                    bot.on_fill(order_id, side, fill_price, fill_qty)
        except Exception as exc:
            log(f"WS userData err: {exc}")
        if bot.stop_flag:
            break
        await asyncio.sleep(5)


async def dry_run_simulator(bot):
    url = f"wss://fstream.binance.com/public/ws/{bot.symbol.lower()}@bookTicker"
    while not bot.stop_flag:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=20) as ws:
                log("WS [dry] bookTicker connected — simulating fills on grid crossings")
                while not bot.stop_flag:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        break
                    msg = json.loads(raw)
                    bid = float(msg.get("b", 0))
                    ask = float(msg.get("a", 0))
                    if bid <= 0 or ask <= 0:
                        continue
                    bot.on_price((bid + ask) / 2)
                    for level in bot.grid:
                        if level.order_id is None:
                            continue
                        if level.side == "BUY" and bid <= level.price:
                            fid = level.order_id
                            level.order_id = None
                            bot.on_fill(fid, "BUY", level.price, bot.qty_per_level)
                        elif level.side == "SELL" and ask >= level.price:
                            fid = level.order_id
                            level.order_id = None
                            bot.on_fill(fid, "SELL", level.price, bot.qty_per_level)
        except Exception as exc:
            log(f"WS [dry] err: {exc}")
        if bot.stop_flag:
            break
        await asyncio.sleep(3)


_global_bot = None


def signal_handler(signum, frame):
    log(f"\n!!! Signal {signum} — stopping")
    global _global_bot
    if _global_bot:
        _global_bot.stop_flag = True


async def main_async(bot, api_key):
    if bot.dry_run:
        await dry_run_simulator(bot)
    else:
        await asyncio.gather(ws_book_ticker(bot), ws_user_data(bot, api_key))


def main():
    global _global_bot
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--low", type=float, required=True)
    parser.add_argument("--high", type=float, required=True)
    parser.add_argument("--grids", type=int, default=10)
    parser.add_argument("--total-margin", type=float, default=20.0)
    parser.add_argument("--leverage", type=int, default=2)
    parser.add_argument("--max-loss", type=float, default=-3.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.low >= args.high:
        log("ERROR: low >= high")
        return 1
    api_key, secret = load_credentials_from_env()
    if not api_key or not secret:
        log("ERROR: missing credentials")
        return 1
    client = BinanceFuturesClient(api_key=api_key, secret_key=secret)
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    log("=" * 70)
    log(f"GRID BOT v1.0 — {mode}")
    log(f"  Symbol: {args.symbol}")
    log(f"  Range: {args.low} - {args.high}")
    log(f"  Grids: {args.grids}  Margin: ${args.total_margin}  Leverage: {args.leverage}x")
    log(f"  Total notional: ${args.total_margin * args.leverage}  Max loss: ${args.max_loss}")
    log("=" * 70)
    bot = GridBot(
        client=client, symbol=args.symbol,
        low=args.low, high=args.high, num_grids=args.grids,
        total_margin=args.total_margin, leverage=args.leverage,
        dry_run=args.dry_run, max_loss=args.max_loss,
    )
    _global_bot = bot
    try:
        bot.fetch_symbol_filters()
        if not args.dry_run:
            bot.setup_leverage()
            try:
                client.signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": args.symbol})
                log(f"Cleared pre-existing open orders on {args.symbol}")
            except Exception:
                pass
        current = bot.get_current_price()
        log(f"Current price: {current}")
        bot.build_grid(current)
        bot.place_initial_orders(current)
    except Exception as exc:
        log(f"Setup failed: {exc}")
        return 1
    signal.signal(signal.SIGINT, signal_handler)
    try:
        signal.signal(signal.SIGTERM, signal_handler)
    except Exception:
        pass
    try:
        asyncio.run(main_async(bot, api_key))
    except KeyboardInterrupt:
        log("KeyboardInterrupt")
    finally:
        bot.cancel_all()
        bot.close_open_positions()
    log("")
    log(f"=== FINAL ({mode}) ===")
    log(f"  Cycles: {bot.stats.cycles_completed}")
    log(f"  Buys: {bot.stats.total_buys_filled}  Sells: {bot.stats.total_sells_filled}")
    log(f"  Fees: ${bot.stats.fees_paid:.4f}")
    log(f"  Realized PnL: ${bot.stats.realized_pnl:+.4f}")
    log("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
