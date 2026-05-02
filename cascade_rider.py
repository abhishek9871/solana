"""
cascade_rider.py — high-leverage liquidation cascade rider.

Strategy: monitor !forceOrder@arr WebSocket for cascade events. When same-symbol
same-direction liquidations exceed $1M in 60 seconds, enter WITH the cascade
direction at 50x leverage with server-side TP/SL brackets. 15-min time stop.

Wins by riding the documented 60-70% cascade-continuation effect. Concentrated
size, few trades, big per-trade outcomes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import websockets
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env


# ===== CONFIG =====
LEVERAGE = 10
MARGIN_PER_SHOT_TESTNET = Decimal("50.0")     # $50 × 10x = $500 notional → ~$25 win / ~$7 loss per trade
MARGIN_PER_SHOT_PROD = Decimal("4.0")         # prod: $24 wallet, 5 slots × $4 = $20 active
MAX_CONCURRENT = 8                             # 8 × $50 = $400 active (gives more diversification)

# Session-level profit target: close all positions the millisecond it's hit
SESSION_PROFIT_TARGET_USD = 30.0               # cash out at +$30 session gain (fast lock)

CASCADE_WINDOW_SEC = 60
CASCADE_THRESHOLD_USD = 15_000   # very aggressive — fire often, $1 SL caps each loss
LIQ_RATIO_THRESHOLD = 0.70

# Velocity scanner: keep only the angles that matter most for edge, drop the restrictive ones.
# - 1m strength threshold (catches real moves)
# - 5m direction must agree (kills whipsaws)
# - Funding alignment (structural edge — long crowded shorts, short crowded longs)
# - Liquidity tier (no micro-caps where ZEREBRO-style losses came from)
VELOCITY_SCAN_INTERVAL_SEC = 10
VELOCITY_THRESHOLD_PCT_1M = 0.4     # lower bar — fire many shots
VELOCITY_REQUIRE_5M_AGREE = False
VELOCITY_MIN_24H_VOL_USD = 20_000_000   # match liquidity filter
VELOCITY_REQUIRE_FUNDING_ALIGN = True   # RESTORED — this is THE edge that made ORCA/ZEREBRO/XRP win

# Whitelist removed: empty set = no whitelist filter (rely on volume/spread filters instead).
# The big winner (ORCA +$100) wasn't in our majors-only list, proving whitelists are too narrow.
SYMBOL_WHITELIST: set = set()

# Hard blacklist: known-broken testnet symbols
# DAMUSDT/ZKJUSDT/BOBUSDT — PERCENT_PRICE filter / max-qty errors
# XAUUSDT/XAGUSDT/CLUSDT/BZUSDT/NATGASUSDT — TradFi perps need agreement
SYMBOL_BLACKLIST = {
    "DAMUSDT", "ZKJUSDT", "BOBUSDT", "1000000BOBUSDT", "FIGHTUSDT",
    "XAUUSDT", "XAGUSDT", "CLUSDT", "BZUSDT", "NATGASUSDT",
}

TP_PCT = Decimal("0.05")          # +5% TP
SL_PCT = Decimal("0.012")          # -1.2% SL
TRAIL_TRIGGER_PCT = Decimal("0.005")  # AGGRESSIVE: trail to breakeven at +0.5% favorable (was 1.2%)
TIME_STOP_SEC = 15 * 60

MIN_24H_VOL_USD = 20_000_000      # very broad — only filter dust pairs
MAX_SPREAD_PCT = Decimal("1.00")  # wide — $1 loss cap absorbs any spread, want trades to FIRE

SYMBOL_COOLDOWN_SEC = 60   # was 300 — cycle faster to actively pursue $30 target

# High-conviction tier: fire 3x size on these (the ORCA-class setups)
HIGH_CONVICTION_FUNDING_PCT = 0.40   # |funding|/8h > this
HIGH_CONVICTION_VEL_PCT = 1.0        # 1m velocity move > this
HIGH_CONVICTION_MARGIN_MULT = 3.0    # 3x normal margin

# BTC trend bias: DISABLED — was blocking ORCA-class wins like AIOT +30% pumps
# (AIOT pumped +30% in 1 min on funding -0.34%, BTC trend filter said "no" -- catastrophic miss)
BTC_TREND_LOOKBACK_BARS = 3
BTC_TREND_REQUIRE = False      # never block on BTC trend — alts have own dynamics

WS_URL_PROD = "wss://fstream.binance.com/market/stream?streams=!forceOrder@arr"

STATE_FILE = "cascade_rider_state.json"
LOG_FILE = "cascade_rider.log"


@dataclass
class Liq:
    ts: float
    symbol: str
    side: str
    qty_usd: float


@dataclass
class Position:
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_time: float
    margin: float
    notional: float
    tp_price: float
    sl_price: float
    tp_order_id: int = 0
    sl_order_id: int = 0
    cascade_volume_usd: float = 0
    cascade_dominant_side: str = ""
    sl_trailed: bool = False  # SL has been moved to breakeven


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    line = "[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] " + safe
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}, "trade_log": []}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log("state save error: " + str(e))


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt_qty(value: Decimal, step: Decimal) -> str:
    q = quantize_down(value, step)
    return format(q.normalize(), "f") if q != q.to_integral() else format(q, "f")


def fmt_price(value: Decimal, tick: Decimal) -> str:
    q = quantize_price(value, tick)
    return format(q.normalize(), "f") if q != q.to_integral() else format(q, "f")


class CascadeRider:
    def __init__(self, client: BinanceFuturesClient, prod: bool):
        self.c = client
        self.prod = prod
        self.margin_per_shot = MARGIN_PER_SHOT_PROD if prod else MARGIN_PER_SHOT_TESTNET
        self.symbol_info_cache: dict = {}
        self.liq_buffer: dict = {}
        self.positions: dict = {}
        self.symbol_cooldown_until: dict = {}
        self.trade_log: list = []
        self.starting_wallet = Decimal("0")
        self.session_start = time.time()
        self.cascade_count = 0
        self.signal_count = 0
        self.liq_received = 0
        self.liq_total_usd = 0.0
        self.pending_opens: set = set()  # symbols with in-flight open_position calls
        self._target_hit: bool = False    # set True when SESSION_PROFIT_TARGET reached

    def _persist(self) -> None:
        save_state({
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "trade_log": self.trade_log,
        })

    def get_symbol_info(self, symbol: str) -> dict:
        if symbol not in self.symbol_info_cache:
            self.symbol_info_cache[symbol] = self.c.get_symbol_info(symbol)
        return self.symbol_info_cache[symbol]

    def get_filter_specs(self, symbol: str):
        info = self.get_symbol_info(symbol)
        tick = step = Decimal("0")
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = Decimal(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                step = Decimal(f["stepSize"])
        return tick, step

    def get_max_qty(self, symbol: str) -> Decimal:
        """Returns the max market order quantity for the symbol."""
        info = self.get_symbol_info(symbol)
        max_q = Decimal("0")
        for f in info.get("filters", []):
            if f["filterType"] == "MARKET_LOT_SIZE":
                max_q = Decimal(f.get("maxQty", "0"))
                break
        if max_q == 0:
            for f in info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    max_q = Decimal(f.get("maxQty", "0"))
                    break
        return max_q

    def get_balance(self) -> Decimal:
        bals = self.c.get_balance()
        usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
        return Decimal(usdt.get("availableBalance") or usdt.get("balance", "0")) if usdt else Decimal("0")

    def record_liq(self, sym: str, side: str, qty_usd: float) -> None:
        now = time.time()
        if sym not in self.liq_buffer:
            self.liq_buffer[sym] = deque()
        cutoff = now - CASCADE_WINDOW_SEC
        while self.liq_buffer[sym] and self.liq_buffer[sym][0].ts < cutoff:
            self.liq_buffer[sym].popleft()
        self.liq_buffer[sym].append(Liq(ts=now, symbol=sym, side=side, qty_usd=qty_usd))

    def detect_cascade(self, sym: str):
        liqs = self.liq_buffer.get(sym)
        if not liqs:
            return None
        buy_usd = sum(l.qty_usd for l in liqs if l.side == "BUY")
        sell_usd = sum(l.qty_usd for l in liqs if l.side == "SELL")
        total = buy_usd + sell_usd
        if total < CASCADE_THRESHOLD_USD:
            return None
        if buy_usd >= total * LIQ_RATIO_THRESHOLD:
            return ("BUY", total)
        if sell_usd >= total * LIQ_RATIO_THRESHOLD:
            return ("SELL", total)
        return None

    def liq_eligible_to_trade(self, sym: str) -> bool:
        if getattr(self, "_target_hit", False):
            return False  # stop signaling once session target reached
        if SYMBOL_WHITELIST and sym not in SYMBOL_WHITELIST:
            return False
        if sym in SYMBOL_BLACKLIST:
            return False
        if sym in self.positions:
            return False
        if sym in self.pending_opens:  # in-flight open — prevent duplicate fire
            return False
        if time.time() < self.symbol_cooldown_until.get(sym, 0):
            return False
        if len(self.positions) >= MAX_CONCURRENT:
            return False
        return True

    def liquidity_filter(self, sym: str) -> bool:
        try:
            t = self.c.public_get("/fapi/v1/ticker/24hr", {"symbol": sym})
            vol = float(t["quoteVolume"])
            if vol < MIN_24H_VOL_USD:
                return False
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = float(bt["bidPrice"])
            ask = float(bt["askPrice"])
            mid = (bid + ask) / 2
            if mid <= 0:
                return False
            spread_pct = Decimal(str((ask - bid) / mid * 100))
            if spread_pct > MAX_SPREAD_PCT:
                return False
        except BinanceApiError:
            return False
        return True

    def _robust_close(self, sym: str, side: str, qty_str: str) -> bool:
        """Multi-strategy close: market -> LIMIT IOC at safe price -> GTC LIMIT at price cap.
        Returns True if order accepted (may not fill immediately on GTC fallback)."""
        try:
            tick, _step = self.get_filter_specs(sym)
        except Exception:
            tick = Decimal("0.00001")
        # 1. Market reduce_only
        try:
            self.c.place_market_order(sym, side, quantity=qty_str, reduce_only=True)
            return True
        except BinanceApiError as e:
            err = str(e)
            if "-4131" not in err and "PERCENT_PRICE" not in err:
                log("  " + sym + " market close failed: " + err)
                return False
            log("  " + sym + " market hit PERCENT_PRICE -- trying LIMIT IOC")
        # 2. LIMIT IOC at mark ± 4% (within typical PERCENT_PRICE band)
        try:
            pi = self.c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
            mark = Decimal(pi["markPrice"])
            if side == "BUY":
                px = (mark * Decimal("1.04") / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            else:
                px = (mark * Decimal("0.96") / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
            px_str = fmt_price(px, tick)
            r = self.c.place_limit_order(sym, side, price=px_str, quantity=qty_str,
                                         time_in_force="IOC", reduce_only=True)
            if int(r.get("executedQty", "0")) > 0 or float(r.get("executedQty", "0")) > 0:
                log("  " + sym + " LIMIT IOC filled at " + px_str)
                return True
        except BinanceApiError as e:
            log("  " + sym + " LIMIT IOC failed: " + str(e))
        # 3. GTX LIMIT at mark × 1.04 (BUY) or × 0.96 (SELL) — post-only, sits at cap
        try:
            pi = self.c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
            mark = Decimal(pi["markPrice"])
            if side == "BUY":
                px = (mark * Decimal("1.04") / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            else:
                px = (mark * Decimal("0.96") / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
            px_str = fmt_price(px, tick)
            r = self.c.place_limit_order(sym, side, price=px_str, quantity=qty_str,
                                         time_in_force="GTX", reduce_only=True)
            log("  " + sym + " GTX LIMIT at " + px_str + " — will fill when book moves into range")
            return True
        except BinanceApiError as e:
            log("  " + sym + " GTX LIMIT failed: " + str(e))
        return False

    def _emergency_close(self, sym: str, close_side: str, qty_str: str) -> None:
        for attempt in range(3):
            if self._robust_close(sym, close_side, qty_str):
                return
            time.sleep(0.5)
        # final verification
        try:
            time.sleep(0.5)
            poss = self.c.get_positions(sym)
            for p in poss:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    side2 = "SELL" if amt > 0 else "BUY"
                    try:
                        _t, step = self.get_filter_specs(sym)
                        qty2 = fmt_qty(quantize_down(Decimal(str(abs(amt))), step), step)
                    except Exception:
                        qty2 = str(abs(amt))
                    self._robust_close(sym, side2, qty2)
        except BinanceApiError:
            pass

    def _cancel_brackets(self, sym: str, pos: Position) -> None:
        for oid in (pos.tp_order_id, pos.sl_order_id):
            if not oid:
                continue
            try:
                self.c.cancel_algo_order(sym, algo_id=oid)
            except BinanceApiError:
                pass

    def get_btc_trend(self) -> str:
        """Returns 'UP', 'DOWN', or 'CHOP' for BTC's last 3 × 5min direction."""
        try:
            k = self.c.get_klines("BTCUSDT", "5m", limit=BTC_TREND_LOOKBACK_BARS + 1)
            if len(k) < BTC_TREND_LOOKBACK_BARS + 1:
                return "CHOP"
            closes = [float(b[4]) for b in k[:-1]]  # closed bars only
            if all(closes[i] < closes[i+1] for i in range(len(closes)-1)):
                return "UP"
            if all(closes[i] > closes[i+1] for i in range(len(closes)-1)):
                return "DOWN"
        except Exception:
            pass
        return "CHOP"

    def is_high_conviction(self, sym: str, funding_pct_8h: float, velocity_pct: float) -> bool:
        """High-conviction = extreme funding AND strong velocity (ORCA-class setup)."""
        return abs(funding_pct_8h) >= HIGH_CONVICTION_FUNDING_PCT and \
               abs(velocity_pct) >= HIGH_CONVICTION_VEL_PCT

    def open_cascade_shot(self, sym: str, side: str, cascade_usd: float,
                          high_conviction: bool = False) -> None:
        if not self.liq_eligible_to_trade(sym):
            return
        # BTC trend bias: skip new trades that fight the trend
        if BTC_TREND_REQUIRE:
            btc_dir = self.get_btc_trend()
            if btc_dir == "UP" and side == "SELL":
                log("  " + sym + " skipped — BTC UP-trend, declining counter-SELL")
                return
            if btc_dir == "DOWN" and side == "BUY":
                log("  " + sym + " skipped — BTC DOWN-trend, declining counter-BUY")
                return
        # Mark as pending immediately so concurrent triggers on same symbol skip
        self.pending_opens.add(sym)
        try:
            self._open_cascade_shot_inner(sym, side, cascade_usd, high_conviction)
        finally:
            self.pending_opens.discard(sym)

    def _open_cascade_shot_inner(self, sym: str, side: str, cascade_usd: float,
                                 high_conviction: bool = False) -> None:
        if not self.liquidity_filter(sym):
            log("  " + sym + " filtered out (liquidity)")
            return
        try:
            tick, step = self.get_filter_specs(sym)
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = Decimal(bt["bidPrice"])
            ask = Decimal(bt["askPrice"])
        except BinanceApiError as e:
            log("  " + sym + " pre-entry error: " + str(e))
            return

        ref_px = ask if side == "BUY" else bid
        if ref_px <= 0:
            return

        try:
            self.c.set_leverage(sym, LEVERAGE)
        except BinanceApiError as e:
            log("  " + sym + " leverage warn: " + str(e))

        # 3x size on high-conviction setups (ORCA-class)
        margin = self.margin_per_shot * Decimal(str(HIGH_CONVICTION_MARGIN_MULT)) if high_conviction else self.margin_per_shot
        notional = margin * Decimal(LEVERAGE)
        if high_conviction:
            log("  " + sym + " HIGH-CONVICTION sizing: $" + format(float(margin), ".0f") + " margin")
        qty = quantize_down(notional / ref_px, step)
        if qty * ref_px < Decimal("5"):
            log("  " + sym + " notional below $5")
            return
        # Cap at exchange max-qty for the symbol
        try:
            max_qty = self.get_max_qty(sym)
            if max_qty > 0 and qty > max_qty:
                qty = quantize_down(max_qty, step)
                log("  " + sym + " qty capped at exchange max " + str(max_qty))
        except Exception:
            pass

        qty_str = fmt_qty(qty, step)
        close_side = "SELL" if side == "BUY" else "BUY"
        log("CASCADE OPEN " + sym + " " + side + " qty=" + qty_str +
            " cascade_usd=$" + format(cascade_usd, ",.0f") + " ref=" + str(ref_px))

        try:
            order = self.c.place_market_order(sym, side, quantity=qty_str)
        except BinanceApiError as e:
            log("  " + sym + " entry failed: " + str(e))
            self.symbol_cooldown_until[sym] = time.time() + SYMBOL_COOLDOWN_SEC
            return

        avg = order.get("avgPrice") or order.get("price")
        try:
            fill_px = Decimal(str(avg)) if avg and Decimal(str(avg)) > 0 else ref_px
        except Exception:
            fill_px = ref_px

        if side == "BUY":
            tp_px = quantize_price(fill_px * (Decimal("1") + TP_PCT), tick)
            sl_px = quantize_price(fill_px * (Decimal("1") - SL_PCT), tick)
        else:
            tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), tick)
            sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), tick)

        tp_order_id = 0
        try:
            r = self.c.place_take_profit_order(
                sym, close_side,
                stop_price=fmt_price(tp_px, tick),
                quantity=qty_str,
                close_position=False,
                reduce_only=True,
            )
            tp_order_id = int(r.get("algoId") or r.get("orderId") or 0)
        except BinanceApiError as e:
            log("  TP failed " + sym + ": " + str(e) + " -- emergency closing")
            self._emergency_close(sym, close_side, qty_str)
            self.symbol_cooldown_until[sym] = time.time() + SYMBOL_COOLDOWN_SEC
            return

        sl_order_id = 0
        try:
            r = self.c.place_stop_market_order(
                sym, close_side,
                stop_price=fmt_price(sl_px, tick),
                quantity=qty_str,
                close_position=False,
                reduce_only=True,
            )
            sl_order_id = int(r.get("algoId") or r.get("orderId") or 0)
        except BinanceApiError as e:
            log("  SL failed " + sym + ": " + str(e) + " -- cancelling TP, emergency closing")
            try:
                if tp_order_id:
                    self.c.cancel_algo_order(sym, algo_id=tp_order_id)
            except Exception:
                pass
            self._emergency_close(sym, close_side, qty_str)
            self.symbol_cooldown_until[sym] = time.time() + SYMBOL_COOLDOWN_SEC
            return

        pos = Position(
            symbol=sym, side=side, qty=float(qty), entry_price=float(fill_px),
            entry_time=time.time(),
            margin=float(margin), notional=float(qty * fill_px),
            tp_price=float(tp_px), sl_price=float(sl_px),
            tp_order_id=tp_order_id, sl_order_id=sl_order_id,
            cascade_volume_usd=cascade_usd, cascade_dominant_side=side,
        )
        self.positions[sym] = pos
        self.signal_count += 1
        self._persist()
        log("  FILLED " + sym + " " + side + " qty=" + qty_str +
            " px=" + format(float(fill_px), ".6f") +
            " TP=" + format(float(tp_px), ".6f") +
            " SL=" + format(float(sl_px), ".6f"))

    def _finalize_close(self, sym: str, pos: Position) -> None:
        self._cancel_brackets(sym, pos)
        try:
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            mark = (float(bt["bidPrice"]) + float(bt["askPrice"])) / 2
        except Exception:
            mark = pos.entry_price
        if pos.side == "BUY":
            reason = "TP" if mark >= (pos.tp_price + pos.sl_price) / 2 else "SL"
        else:
            reason = "TP" if mark <= (pos.tp_price + pos.sl_price) / 2 else "SL"
        trigger_px = pos.tp_price if reason == "TP" else pos.sl_price
        if pos.side == "BUY":
            pnl_gross = (trigger_px - pos.entry_price) * pos.qty
        else:
            pnl_gross = (pos.entry_price - trigger_px) * pos.qty
        fees_est = pos.notional * 0.0009
        pnl_net = pnl_gross - fees_est
        self.trade_log.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym, "side": pos.side, "qty": pos.qty,
            "entry_px": pos.entry_price, "exit_px": trigger_px,
            "pnl_gross": pnl_gross, "fees_est": fees_est, "pnl_net": pnl_net,
            "reason": reason, "duration_sec": int(time.time() - pos.entry_time),
            "cascade_volume_usd": pos.cascade_volume_usd,
        })
        log("CLOSE " + sym + " " + reason + " entry=" + format(pos.entry_price, ".6f") +
            " trigger=" + format(trigger_px, ".6f") +
            " pnl_net=$" + format(pnl_net, "+.4f") +
            " dur=" + str(int(time.time()-pos.entry_time)) + "s")
        self.positions.pop(sym, None)
        self.symbol_cooldown_until[sym] = time.time() + SYMBOL_COOLDOWN_SEC
        self._persist()

    async def manage_positions_loop(self) -> None:
        while True:
            try:
                # Check session profit target — close everything if hit
                if self._check_session_target():
                    log("=== SESSION PROFIT TARGET HIT — closing all positions and stopping signals ===")
                    self._target_hit = True
                    for sym in list(self.positions.keys()):
                        pos = self.positions[sym]
                        self._cancel_brackets(sym, pos)
                        close_side = "SELL" if pos.side == "BUY" else "BUY"
                        try:
                            _tk, step = self.get_filter_specs(sym)
                            qs = fmt_qty(Decimal(str(pos.qty)), step)
                        except Exception:
                            qs = str(pos.qty)
                        self._robust_close(sym, close_side, qs)
                        self._finalize_close(sym, pos)
                    await asyncio.sleep(5)
                    continue
                for sym in list(self.positions.keys()):
                    pos = self.positions[sym]
                    try:
                        poss = self.c.get_positions(sym)
                    except BinanceApiError:
                        continue
                    amt = 0.0
                    for p in poss:
                        amt = abs(float(p.get("positionAmt", 0)))
                        break
                    if amt == 0:
                        self._finalize_close(sym, pos)
                        continue
                    # Progressive trailing: continuously raise SL behind price as it runs
                    # Triggers once position is +TRAIL_TRIGGER_PCT favorable, then keeps stepping up
                    try:
                        bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
                        mark = (float(bt["bidPrice"]) + float(bt["askPrice"])) / 2
                        if pos.side == "BUY":
                            fav_pct_pct = (mark - pos.entry_price) / pos.entry_price * 100
                        else:
                            fav_pct_pct = (pos.entry_price - mark) / pos.entry_price * 100
                        if fav_pct_pct >= float(TRAIL_TRIGGER_PCT) * 100:
                            self._trail_sl_progressive(sym, pos, fav_pct_pct / 100)
                    except Exception:
                        pass
                    if time.time() - pos.entry_time > TIME_STOP_SEC:
                        log(sym + " TIME_STOP at " + str(int(time.time()-pos.entry_time)) + "s")
                        self._cancel_brackets(sym, pos)
                        close_side = "SELL" if pos.side == "BUY" else "BUY"
                        try:
                            _tk, step = self.get_filter_specs(sym)
                            qs = fmt_qty(Decimal(str(pos.qty)), step)
                        except Exception:
                            qs = str(pos.qty)
                        self._robust_close(sym, close_side, qs)
                        self._finalize_close(sym, pos)
            except Exception as e:
                log("manage loop error (recovered): " + str(e))
            await asyncio.sleep(1)  # check every 1s for fast session-target detection

    def _check_session_target(self) -> bool:
        """Returns True if session profit target reached (close-all triggered)."""
        if getattr(self, "_target_hit", False):
            return True
        try:
            current = self.get_balance()
            session_pnl = float(current - self.starting_wallet)
            # add unrealized
            try:
                positions = self.c.get_positions()
                for p in positions:
                    session_pnl += float(p.get("unRealizedProfit", 0))
            except BinanceApiError:
                pass
            if session_pnl >= SESSION_PROFIT_TARGET_USD:
                log("session pnl=$" + format(session_pnl, "+.2f") +
                    " >= target $" + format(SESSION_PROFIT_TARGET_USD, ".2f"))
                return True
        except Exception:
            pass
        return False

    def _trail_sl_progressive(self, sym: str, pos: Position, fav_pct: float) -> None:
        """Progressive trail: lock more profit as price runs further.
        Locks SL at fav_pct - TRAIL_LOCK_OFFSET. Only moves SL UP (favorable direction).
        This fixes the 'profit then loss' pattern by capturing peaks."""
        # Determine target SL price: lock current_fav_pct - 0.5% behind
        # e.g., +1% favorable → SL at +0.5%, +5% favorable → SL at +4.5%
        TRAIL_LOCK_OFFSET = 0.005  # always trail 0.5% behind current favorable
        new_sl_pct = fav_pct - TRAIL_LOCK_OFFSET  # in fraction (e.g., 0.005 for 0.5%)
        if new_sl_pct <= 0:
            new_sl_pct = 0  # at minimum, breakeven
        try:
            tick, step = self.get_filter_specs(sym)
            close_side = "SELL" if pos.side == "BUY" else "BUY"
            qty_str = fmt_qty(Decimal(str(pos.qty)), step)
            entry = Decimal(str(pos.entry_price))
            if pos.side == "BUY":
                new_sl = entry * (Decimal("1") + Decimal(str(new_sl_pct)))
            else:
                new_sl = entry * (Decimal("1") - Decimal(str(new_sl_pct)))
            new_sl_q = quantize_price(new_sl, tick)
            # Only move SL in favorable direction (never backward)
            current_sl = Decimal(str(pos.sl_price))
            if pos.side == "BUY" and new_sl_q <= current_sl:
                return  # don't move SL down for a long
            if pos.side == "SELL" and new_sl_q >= current_sl:
                return  # don't move SL up for a short
            # Cancel old SL
            if pos.sl_order_id:
                try:
                    self.c.cancel_algo_order(sym, algo_id=pos.sl_order_id)
                except BinanceApiError:
                    pass
            r = self.c.place_stop_market_order(
                sym, close_side,
                stop_price=fmt_price(new_sl_q, tick),
                quantity=qty_str,
                close_position=False,
                reduce_only=True,
            )
            new_id = int(r.get("algoId") or r.get("orderId") or 0)
            pos.sl_order_id = new_id
            pos.sl_price = float(new_sl_q)
            pos.sl_trailed = True
            self._persist()
            log("  TRAILED " + sym + " SL up to $" + format(float(new_sl_q), ".6f") +
                " (locking " + format(new_sl_pct * 100, ".2f") + "% favorable)")
        except BinanceApiError as e:
            log("  trail SL failed " + sym + ": " + str(e))

    # Backward-compat alias used elsewhere
    def _trail_sl_to_breakeven(self, sym: str, pos: Position) -> None:
        self._trail_sl_progressive(sym, pos, float(TRAIL_TRIGGER_PCT))

    async def velocity_scanner_loop(self) -> None:
        """Smarter velocity trigger: multi-timeframe + volume spike + funding alignment.
        Only fires on confluence of all 4 angles, not just single-bar move."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                await asyncio.sleep(VELOCITY_SCAN_INTERVAL_SEC)
                if len(self.positions) >= MAX_CONCURRENT:
                    continue
                try:
                    tickers = self.c.get_24hr_tickers()
                except BinanceApiError:
                    continue
                usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
                usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
                top = usdt[:30]
                # Fetch funding rates for all top symbols at once
                try:
                    premium_list = self.c.public_get("/fapi/v1/premiumIndex", {})
                    funding_map = {p["symbol"]: float(p.get("lastFundingRate", 0)) * 100
                                   for p in premium_list} if isinstance(premium_list, list) else {}
                except BinanceApiError:
                    funding_map = {}

                for t in top:
                    sym = t["symbol"]
                    vol = float(t.get("quoteVolume", 0))
                    if vol < VELOCITY_MIN_24H_VOL_USD:
                        continue
                    if not self.liq_eligible_to_trade(sym):
                        continue
                    # 1m bar move strength
                    try:
                        k1 = self.c.get_klines(sym, "1m", limit=2)
                        k5 = self.c.get_klines(sym, "5m", limit=2) if VELOCITY_REQUIRE_5M_AGREE else []
                    except BinanceApiError:
                        continue
                    if len(k1) < 2:
                        continue
                    prev_1m = float(k1[-2][4])
                    curr_1m = float(k1[-1][4])
                    if prev_1m <= 0:
                        continue
                    pct_1m = (curr_1m / prev_1m - 1.0) * 100
                    if abs(pct_1m) < VELOCITY_THRESHOLD_PCT_1M:
                        continue
                    # 5m direction agreement (no magnitude — just same sign)
                    pct_5m = 0.0
                    if VELOCITY_REQUIRE_5M_AGREE and len(k5) >= 2:
                        open_5m = float(k5[-2][1])
                        close_5m = float(k5[-2][4])
                        if open_5m > 0:
                            pct_5m = (close_5m / open_5m - 1.0) * 100
                            if (pct_1m > 0 and pct_5m < 0) or (pct_1m < 0 and pct_5m > 0):
                                continue  # whipsaw: skip
                    # Funding alignment — structural edge
                    funding = funding_map.get(sym, 0)
                    ride_side = "BUY" if pct_1m > 0 else "SELL"
                    if VELOCITY_REQUIRE_FUNDING_ALIGN:
                        if ride_side == "BUY" and funding > 0:
                            continue
                        if ride_side == "SELL" and funding < 0:
                            continue
                    # High-conviction check: extreme funding + strong velocity
                    hc = self.is_high_conviction(sym, funding, pct_1m)
                    log("VELOCITY " + sym + " 1m=" + format(pct_1m, "+.2f") + "%" +
                        " 5m=" + format(pct_5m, "+.2f") + "%" +
                        " fund=" + format(funding, "+.4f") + "%" +
                        " -> ride " + ride_side + (" [HIGH-CONVICTION]" if hc else ""))
                    loop.run_in_executor(None, self.open_cascade_shot, sym, ride_side, vol, hc)
                    if len(self.positions) >= MAX_CONCURRENT:
                        break
            except Exception as e:
                # Don't let exceptions kill the velocity loop (the previous bot died this way)
                log("velocity loop error (recovered): " + str(e))
                await asyncio.sleep(5)

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                bal = self.get_balance()
                wins = [t for t in self.trade_log if t["pnl_net"] > 0]
                total_pnl = sum(t["pnl_net"] for t in self.trade_log)
                n = len(self.trade_log)
                wr = len(wins) / n * 100 if n else 0
                log("HB: bal=$" + format(float(bal), ".2f") +
                    " pos=" + str(len(self.positions)) +
                    " liqs_recv=" + str(self.liq_received) +
                    " liqs_total=$" + format(self.liq_total_usd, ",.0f") +
                    " cascades=" + str(self.cascade_count) +
                    " signals=" + str(self.signal_count) +
                    " trades=" + str(n) +
                    " wr=" + format(wr, ".1f") + "%" +
                    " pnl=$" + format(total_pnl, "+.4f"))
            except Exception as e:
                log("heartbeat error (recovered): " + str(e))

    async def ws_loop(self) -> None:
        url = WS_URL_PROD
        log("connecting WS: " + url)
        loop = asyncio.get_event_loop()
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    log("WS connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data") if "data" in msg else msg
                            if not data:
                                continue
                            if data.get("e") != "forceOrder":
                                continue
                            o = data.get("o", {})
                            sym = o.get("s")
                            side = o.get("S")
                            avg = float(o.get("ap", 0))
                            qty = float(o.get("q", 0))
                            qty_usd = avg * qty
                            if not sym or not side or qty_usd <= 0:
                                continue
                            self.liq_received += 1
                            self.liq_total_usd += qty_usd
                            self.record_liq(sym, side, qty_usd)
                            cascade = self.detect_cascade(sym)
                            if cascade:
                                self.cascade_count += 1
                                dom_side, total = cascade
                                ride_side = dom_side
                                if self.liq_eligible_to_trade(sym):
                                    log("CASCADE " + sym + " dominant=" + dom_side +
                                        " total=$" + format(total, ",.0f") +
                                        " -> ride " + ride_side)
                                    loop.run_in_executor(
                                        None, self.open_cascade_shot, sym, ride_side, total)
                        except Exception as e:
                            log("WS msg parse err: " + str(e))
            except Exception as e:
                log("WS conn err: " + str(e) + " -- reconnecting in 5s")
                await asyncio.sleep(5)

    async def run(self, max_runtime_sec: int) -> int:
        log("=== cascade_rider " + ("PROD" if self.prod else "TESTNET") + " starting ===")
        log("margin/shot=$" + format(float(self.margin_per_shot), ".0f") +
            " leverage=" + str(LEVERAGE) + "x" +
            " TP=+" + format(float(TP_PCT)*100, ".1f") + "%" +
            " SL=-" + format(float(SL_PCT)*100, ".2f") + "%" +
            " cascade_threshold=$" + format(CASCADE_THRESHOLD_USD, ",") +
            "/" + str(CASCADE_WINDOW_SEC) + "s")
        try:
            self.starting_wallet = self.get_balance()
            log("starting wallet $" + format(float(self.starting_wallet), ".4f"))
        except Exception as e:
            log("balance fetch err: " + str(e))

        try:
            opens = self.c.get_open_orders()
            syms_to_clean = {o["symbol"] for o in opens}
            try:
                positions = self.c.get_positions()
                for p in positions:
                    if float(p.get("positionAmt", 0)) != 0:
                        syms_to_clean.add(p["symbol"])
            except BinanceApiError:
                positions = []
            for s in syms_to_clean:
                try:
                    self.c.cancel_all_orders(s)
                except BinanceApiError:
                    pass
                try:
                    self.c.cancel_all_algo_orders(s)
                except BinanceApiError:
                    pass
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                sym = p["symbol"]
                side = "SELL" if amt > 0 else "BUY"
                try:
                    _t, step = self.get_filter_specs(sym)
                    qs = fmt_qty(quantize_down(Decimal(str(abs(amt))), step), step)
                except Exception:
                    qs = str(abs(amt))
                if self._robust_close(sym, side, qs):
                    log("startup: closed orphan " + sym + " " + side + " qty=" + qs)
                else:
                    log("startup: orphan close failed " + sym)
        except BinanceApiError as e:
            log("startup cleanup err: " + str(e))

        tasks = [
            asyncio.create_task(self.ws_loop()),
            asyncio.create_task(self.manage_positions_loop()),
            asyncio.create_task(self.velocity_scanner_loop()),
            asyncio.create_task(self.heartbeat_loop()),
        ]
        if max_runtime_sec > 0:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=max_runtime_sec)
            except asyncio.TimeoutError:
                log("=== runtime limit reached ===")
                for sym in list(self.positions.keys()):
                    pos = self.positions[sym]
                    self._cancel_brackets(sym, pos)
                    close_side = "SELL" if pos.side == "BUY" else "BUY"
                    try:
                        _t, step = self.get_filter_specs(sym)
                        qs = fmt_qty(Decimal(str(pos.qty)), step)
                    except Exception:
                        qs = str(pos.qty)
                    self._robust_close(sym, close_side, qs)
                    self._finalize_close(sym, pos)
                for t in tasks:
                    t.cancel()
        else:
            await asyncio.gather(*tasks)

        n = len(self.trade_log)
        if n:
            wins = [t for t in self.trade_log if t["pnl_net"] > 0]
            total = sum(t["pnl_net"] for t in self.trade_log)
            log("=== FINAL n=" + str(n) +
                " wins=" + str(len(wins)) +
                " wr=" + format(len(wins)/n*100, ".1f") + "%" +
                " pnl=$" + format(total, "+.4f") + " ===")
        try:
            ending = self.get_balance()
            log("=== ending wallet $" + format(float(ending), ".4f") +
                " (delta $" + format(float(ending-self.starting_wallet), "+.4f") + ") ===")
        except Exception:
            pass
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true", help="Use prod (real money). Default: testnet.")
    ap.add_argument("--minutes", type=int, default=0, help="Max runtime in min (0 = forever)")
    args = ap.parse_args()

    load_credentials_from_env()

    if args.prod:
        api = os.environ.get("BINANCE_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
        base = "https://fapi.binance.com"
    else:
        api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
        base = "https://testnet.binancefuture.com"

    if not api or not secret:
        print("ERROR: API keys missing")
        return 2

    client = BinanceFuturesClient(api_key=api, secret_key=secret, base_url=base)
    rider = CascadeRider(client, prod=args.prod)
    runtime = args.minutes * 60 if args.minutes > 0 else 0
    try:
        return asyncio.run(rider.run(max_runtime_sec=runtime))
    except KeyboardInterrupt:
        log("KeyboardInterrupt")
        return 0


if __name__ == "__main__":
    sys.exit(main())
