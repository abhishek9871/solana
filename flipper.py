"""
FLIPPER — proactive direction-switching trader with WS reconnection.
"""
import os
import sys
import time
import asyncio
import json
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import websockets
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

DRY_RUN = os.environ.get("FLIPPER_DRY_RUN", "0") == "1"

SYMBOL = "SOLVUSDT"
INITIAL_SIDE = "BUY"  # extreme funding -0.204%/8h, structural squeeze pressure
LEVERAGE = 10
MARGIN_USDT = Decimal("5.0")  # smaller for $6.84 pot, $1.84 reserve
TP_PCT = Decimal("0.05")
SL_PCT = Decimal("0.012")
FLIP_TRIGGER_PCT = Decimal("0.003")
LOCK_BE_TRIGGER_PCT = Decimal("0.003")
MAX_FLIPS = 6
MAX_SESSION_LOSS_USD = 2.50   # ~37% of $6.84
SESSION_PROFIT_TARGET = 9.0   # ~$15.84 wallet — near your $16 goal

WS_URL = "wss://fstream.binance.com/ws/" + SYMBOL.lower() + "@bookTicker"


def quantize_down(v, s):
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def quantize_price(v, t):
    return (v / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def log(msg):
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")


class Flipper:
    def __init__(self):
        info = c.get_symbol_info(SYMBOL)
        self.tick = self.step = Decimal("0")
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                self.tick = Decimal(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                self.step = Decimal(f["stepSize"])
        self.starting_wallet = self._get_wallet()
        self.flips = 0
        self.side = INITIAL_SIDE
        self.entry_px = None
        self.qty = None
        self.qty_str = None
        self.tp_id = None
        self.sl_id = None
        self.peak_favorable_pct = 0.0
        self.locked_be = False
        log(f"=== FLIPPER initialized — symbol {SYMBOL}, max_flips {MAX_FLIPS} ===")
        log(f"  start wallet=${self.starting_wallet:.2f}")

    def _get_wallet(self):
        try:
            bals = c.get_balance()
            usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
            v = float(usdt.get("balance", 0)) if usdt else 0  # use total balance, not just available
            if v > 0:
                self._last_wallet = v
            return v
        except BinanceApiError:
            return getattr(self, "_last_wallet", self.starting_wallet)  # fall back to last known

    def session_pnl(self):
        w = self._get_wallet()
        if w <= 0:
            return 0  # API hiccup — don't trigger false circuit breaker
        return w - self.starting_wallet

    def _market_close_and_cancel(self):
        if DRY_RUN:
            log(f"  [DRY] would cancel orders + market-close {SYMBOL} position")
            return
        try:
            c.cancel_all_orders(SYMBOL)
        except BinanceApiError:
            pass
        try:
            c.cancel_all_algo_orders(SYMBOL)
        except BinanceApiError:
            pass
        try:
            positions = c.get_positions(SYMBOL)
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    side = "SELL" if amt > 0 else "BUY"
                    qty = abs(amt)
                    qty_q = quantize_down(Decimal(str(qty)), self.step)
                    qty_s = fmt(qty_q)
                    c.place_market_order(SYMBOL, side, quantity=qty_s, reduce_only=True)
        except BinanceApiError as e:
            log(f"  close fail: {e}")

    def _open_direction(self, side):
        try:
            bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
            bid = Decimal(bt["bidPrice"])
            ask = Decimal(bt["askPrice"])
        except BinanceApiError as e:
            log(f"  bookTicker fail: {e}")
            return False
        ref = ask if side == "BUY" else bid
        notional = MARGIN_USDT * Decimal(LEVERAGE)
        qty = quantize_down(notional / ref, self.step)
        qty_str = fmt(qty)
        if DRY_RUN:
            log(f"  [DRY] would OPEN {side} qty={qty_str} ref=${ref} (no real order)")
            self.side = side
            self.entry_px = ref
            self.qty = qty
            self.qty_str = qty_str
            self.peak_favorable_pct = 0.0
            self.locked_be = False
            return True
        try:
            c.set_leverage(SYMBOL, LEVERAGE)
        except BinanceApiError:
            pass
        try:
            o = c.place_market_order(SYMBOL, side, quantity=qty_str)
            avg = o.get("avgPrice") or o.get("price") or ref
            fill_px = Decimal(str(avg)) if avg and float(avg) > 0 else ref
        except BinanceApiError as e:
            log(f"  ENTRY FAIL ({side}): {e}")
            return False
        self.side = side
        self.entry_px = fill_px
        self.qty = qty
        self.qty_str = qty_str
        self.peak_favorable_pct = 0.0
        self.locked_be = False
        if side == "BUY":
            tp_px = quantize_price(fill_px * (Decimal("1") + TP_PCT), self.tick)
            sl_px = quantize_price(fill_px * (Decimal("1") - SL_PCT), self.tick)
        else:
            tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), self.tick)
            sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), self.tick)
        close_side = "SELL" if side == "BUY" else "BUY"
        try:
            tp = c.place_take_profit_order(SYMBOL, close_side, stop_price=fmt(tp_px),
                                             quantity=qty_str, close_position=False, reduce_only=True)
            self.tp_id = int(tp.get("algoId") or tp.get("orderId") or 0)
        except BinanceApiError as e:
            log(f"  TP fail: {e}")
            return False
        try:
            sl = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(sl_px),
                                             quantity=qty_str, close_position=False, reduce_only=True)
            self.sl_id = int(sl.get("algoId") or sl.get("orderId") or 0)
        except BinanceApiError as e:
            log(f"  SL fail: {e}")
        log(f"  OPEN {side} qty={qty_str} entry=${fill_px} TP=${tp_px} SL=${sl_px}")
        return True

    def _move_sl_to_breakeven(self):
        if self.locked_be or not self.entry_px:
            return
        if DRY_RUN:
            log(f"  [DRY] would MOVE SL to breakeven ${self.entry_px}")
            self.locked_be = True
            return
        try:
            if self.sl_id:
                try:
                    c.cancel_algo_order(SYMBOL, algo_id=self.sl_id)
                except BinanceApiError:
                    pass
            close_side = "SELL" if self.side == "BUY" else "BUY"
            new_sl = quantize_price(self.entry_px, self.tick)
            r = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(new_sl),
                                            quantity=self.qty_str, close_position=False, reduce_only=True)
            self.sl_id = int(r.get("algoId") or r.get("orderId") or 0)
            self.locked_be = True
            log(f"  *** BREAKEVEN LOCKED at ${new_sl} ***")
        except BinanceApiError as e:
            log(f"  BE lock fail: {e}")

    def check_position_alive(self):
        if DRY_RUN:
            return True  # always alive in dry run
        try:
            positions = c.get_positions(SYMBOL)
            for p in positions:
                amt = abs(float(p.get("positionAmt", 0)))
                if amt > 0:
                    return True
            return False
        except BinanceApiError:
            return True

    async def _handle_tick(self, msg):
        bid = float(msg.get("b", 0))
        ask = float(msg.get("a", 0))
        if bid <= 0 or ask <= 0:
            return
        mark = (bid + ask) / 2
        if not self.entry_px:
            return
        if self.side == "BUY":
            fav_pct = (mark - float(self.entry_px)) / float(self.entry_px) * 100
        else:
            fav_pct = (float(self.entry_px) - mark) / float(self.entry_px) * 100
        if fav_pct > self.peak_favorable_pct:
            self.peak_favorable_pct = fav_pct
        # Breakeven lock
        if not self.locked_be and fav_pct >= float(LOCK_BE_TRIGGER_PCT) * 100:
            self._move_sl_to_breakeven()
        # Position closed by bracket?
        loop = asyncio.get_event_loop()
        if not await loop.run_in_executor(None, self.check_position_alive):
            pnl = self.session_pnl()
            log(f"Position closed. session_pnl=${pnl:+.2f}")
            if pnl <= -MAX_SESSION_LOSS_USD:
                log(f"*** circuit: loss limit. STOPPING ***")
                return "STOP"
            if pnl >= SESSION_PROFIT_TARGET:
                log(f"*** circuit: profit target. STOPPING ***")
                return "STOP"
            if self.flips >= MAX_FLIPS:
                log(f"*** max flips. STOPPING ***")
                return "STOP"
            # Determine close direction based on price vs entry
            # If we were SHORT and price went UP past entry → SL hit (loss) → FLIP
            # If we were SHORT and price went DOWN past entry → TP hit (win) → continue
            mark_now = (float(msg.get("b", 0)) + float(msg.get("a", 0))) / 2 if msg.get("b") else float(self.entry_px)
            if self.side == "BUY":
                was_loss = mark_now < float(self.entry_px) * 0.999  # any meaningful drop
            else:
                was_loss = mark_now > float(self.entry_px) * 1.001
            if was_loss:
                new_side = "SELL" if self.side == "BUY" else "BUY"
                log(f"  Last close was a LOSS → FLIPPING to {new_side}")
                self.flips += 1
                if not await loop.run_in_executor(None, self._open_direction, new_side):
                    return "STOP"
            else:
                log(f"  Last close was a WIN → continuing {self.side}")
                if not await loop.run_in_executor(None, self._open_direction, self.side):
                    return "STOP"
            return None
        # Flip trigger: adverse before any favorable
        if (not self.locked_be) and (-fav_pct) >= float(FLIP_TRIGGER_PCT) * 100 \
                and self.peak_favorable_pct < 0.1:
            if self.flips >= MAX_FLIPS:
                return None
            pnl = self.session_pnl()
            if pnl <= -MAX_SESSION_LOSS_USD:
                log(f"*** circuit during flip check. STOPPING ***")
                return "STOP"
            log(f"FLIP TRIGGER: adverse={-fav_pct:.3f}%")
            await loop.run_in_executor(None, self._market_close_and_cancel)
            new_side = "SELL" if self.side == "BUY" else "BUY"
            log(f"  flip {self.flips+1}/{MAX_FLIPS}: {self.side} -> {new_side}")
            self.flips += 1
            await asyncio.sleep(0.5)
            if not await loop.run_in_executor(None, self._open_direction, new_side):
                return "STOP"
        return None

    async def run(self):
        if not self._open_direction(INITIAL_SIDE):
            log("Initial entry failed — exiting")
            return
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=10) as ws:
                    log(f"WS connected to {SYMBOL}")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        try:
                            result = await self._handle_tick(msg)
                            if result == "STOP":
                                return
                        except Exception as e:
                            log(f"  tick err (recovered): {e}")
            except Exception as e:
                log(f"WS error (reconnect in 5s): {e}")
                await asyncio.sleep(5)


async def main():
    f = Flipper()
    try:
        await f.run()
    except KeyboardInterrupt:
        log("Interrupted — closing")
        f._market_close_and_cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
