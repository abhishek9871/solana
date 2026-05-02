"""
orca_squeeze_bot.py - implements report1.md verbatim.

ONE entry on 5m close > $1.660 with funding still <= -0.15%/8h.
Market buy with chase-guard (ask <= $1.682, else skip this candle).
Stop-market at $1.555. Trail to $1.670 at $1.830, 8% below highest 5m close from $2.050.
Exit at $2.280 or stop. NO re-entries. NO parameter tweaking mid-run.

Default mode is a live-data dry-run. Pass --live --confirm-live to place
real Binance USD-M futures orders after the same filters pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError

try:
    import websockets
except ImportError:  # pragma: no cover - runtime dependency guard
    websockets = None

SYMBOL = "ORCAUSDT"
LEVERAGE = 10
WALLET_UTILIZATION = Decimal("0.90")  # margin = 90% of free balance
MIN_WALLET_USDT = Decimal("15.0")

# Trade levels per report1.md
ENTRY_TRIGGER = Decimal("1.660")
ENTRY_MAX_CHASE = Decimal("1.682")
HARD_STOP = Decimal("1.555")
STOP1_TRIGGER = Decimal("1.830")
STOP1_NEW = Decimal("1.670")
TRAIL_ARM = Decimal("2.050")
TRAIL_FACTOR = Decimal("0.92")  # 8% below highest 5m close
TARGET = Decimal("2.280")

# Eligibility / abort filters
ABORT_FUND_PCT_8H = -0.05
ENTRY_FUND_PCT_8H = -0.15
PRICE_FLOOR_15M = Decimal("1.500")
ENTRY_SPREAD_MAX_PCT = Decimal("0.12")
ABORT_SPREAD_MAX_PCT = Decimal("0.15")
MAX_ENTRY_BTC_15M_DROP_PCT = 1.5
MAX_ABORT_BTC_15M_DROP_PCT = 2.0
MIN_24H_CHG_PCT = 15.0
MIN_OUTPERF_BTC_PCT = 15.0
MIN_24H_FUT_VOL_USD = Decimal("250000000")
CHASE_FAIL_LEVEL = Decimal("1.75")
MAX_2_STOPS = 2  # enforce no revenge

POLL_SEC = 2
STATUS_EVERY_SEC = 30
DEFAULT_DRY_WALLET = Decimal("24.0")
ADAPTIVE_LOOKBACK_5M = 6
ADAPTIVE_CHASE_PCT = Decimal("0.01325")  # same width as 1.660 -> 1.682
ADAPTIVE_STOP_FACTOR = HARD_STOP / ENTRY_TRIGGER
ADAPTIVE_STOP1_FACTOR = STOP1_NEW / ENTRY_TRIGGER
AGGRESSIVE_LOOKBACK_5M = 3
AGGRESSIVE_TARGET_FACTOR = Decimal("1.066")
SLOW_FILTER_REFRESH_SEC = 60
POSITION_CHECK_SEC = 3
WS_RECONNECT_SEC = 3

# Active paper mode: live WebSocket data, simulated futures execution.
ACTIVE_LEVERAGE = 10
ACTIVE_MARGIN_FRACTION = Decimal("0.45")
ACTIVE_TAKER_FEE = Decimal("0.0005")
ACTIVE_MAX_SPREAD_PCT = Decimal("0.18")
ACTIVE_TAKE_PROFIT_PCT = Decimal("0.0065")
ACTIVE_STOP_LOSS_PCT = Decimal("0.0032")
ACTIVE_TRAIL_ARM_PCT = Decimal("0.0045")
ACTIVE_TRAIL_GIVEBACK_PCT = Decimal("0.0022")
ACTIVE_COOLDOWN_SEC = 8.0
ACTIVE_MAX_HOLD_SEC = 120.0
ACTIVE_DEFAULT_SECONDS = 180
ACTIVE_FORCE_AFTER_SEC = 25.0
ACTIVE_SESSION_TARGET_USDT = Decimal("0.25")
ACTIVE_SESSION_LOSS_USDT = Decimal("-0.75")
ACTIVE_MIN_V2_PCT = 0.12
ACTIVE_MIN_V8_PCT = 0.20
ACTIVE_MIN_FLOW3 = 0.45
ACTIVE_MIN_FLOW8 = 0.20
ACTIVE_MAX_V8_PCT = 0.85
ACTIVE_WHIPSAW_GUARD_SEC = 12.0
ACTIVE_CONFIRM_MOVE_PCT = 0.06
ACTIVE_CONFIRM_TIMEOUT_SEC = 5.0


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def fmt(v: Decimal | float | int) -> str:
    s = format(Decimal(str(v)).normalize(), "f")
    return s


def ema_decimal(values: list[Decimal], period: int) -> Decimal:
    if not values:
        return Decimal("0")
    if len(values) < period:
        return values[-1]
    k = Decimal("2") / Decimal(period + 1)
    seed = sum(values[:period]) / Decimal(period)
    out = seed
    for value in values[period:]:
        out = value * k + out * (Decimal("1") - k)
    return out


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


@dataclass
class LiveState:
    mark: Decimal
    funding_pct_8h_normalized: float
    bid: Decimal
    ask: Decimal
    last_5m_open_time: int
    last_5m_close: Decimal
    recent_5m_high: Decimal
    recent_5m_low: Decimal
    recent_5m_close_high: Decimal
    last_15m_close: Decimal
    btc_15m_pct: float
    spread_pct: float
    chg_24h_pct: float
    btc_chg_24h_pct: float
    outperf_btc_24h_pct: float
    quote_volume_24h: Decimal
    open_interest_usd: Decimal
    ema20_5m: Decimal


@dataclass
class ActivePaperPosition:
    side: str
    qty: Decimal
    entry_price: Decimal
    entry_ts: float
    margin: Decimal
    notional: Decimal
    entry_fee: Decimal
    stop_price: Decimal
    target_price: Decimal
    best_exit_price: Decimal
    trailing_stop: Decimal | None = None


class OrcaBot:
    def __init__(
        self,
        client: BinanceFuturesClient,
        dry_run: bool,
        adaptive: bool = False,
        aggressive: bool = False,
        dry_wallet: Decimal = DEFAULT_DRY_WALLET,
        max_cycles: int = 0,
        poll_sec: int = POLL_SEC,
        status_every_sec: int = STATUS_EVERY_SEC,
        use_websocket: bool = True,
    ):
        self.c = client
        self.dry_run = dry_run
        self.adaptive = adaptive
        self.aggressive = aggressive
        self.dry_wallet = dry_wallet
        self.max_cycles = max_cycles
        self.poll_sec = max(1, poll_sec)
        self.status_every_sec = max(5, status_every_sec)
        self.use_websocket = use_websocket
        self.tick_size = Decimal("0.001")
        self.step_size = Decimal("0.1")
        self.funding_interval_hours = 8
        self.qty: Decimal = Decimal("0")
        self.entry_price: Decimal | None = None
        self.stop_algo_id: int | None = None
        self.state = "WAITING_TRIGGER"
        self.stop_level = HARD_STOP
        self.high_5m_close: Decimal = Decimal("0")
        self.last_seen_5m_open_time: int = 0
        self.starting_wallet: Decimal = Decimal("0")
        self.first_stop_moved = False
        self.cycles = 0
        self.entry_trigger = ENTRY_TRIGGER
        self.entry_max_chase = ENTRY_MAX_CHASE
        self.hard_stop = HARD_STOP
        self.stop1_trigger = STOP1_TRIGGER
        self.stop1_new = STOP1_NEW
        self.trail_arm = TRAIL_ARM
        self.target = TARGET
        self.chase_fail_level = CHASE_FAIL_LEVEL
        self.last_status_ts = 0.0
        self.last_slow_refresh_ts = 0.0
        self.last_position_check_ts = 0.0
        self.closed_5m_closes: deque[Decimal] = deque(maxlen=40)
        self.closed_5m_highs: deque[Decimal] = deque(maxlen=ADAPTIVE_LOOKBACK_5M)
        self.closed_5m_lows: deque[Decimal] = deque(maxlen=ADAPTIVE_LOOKBACK_5M)

    # ---------- pre-flight ----------
    def preflight(self) -> bool:
        mode = "LIVE TRADING" if not self.dry_run else "LIVE-DATA DRY-RUN"
        log(f"=== ORCA squeeze bot {mode} ===")

        info = self.c.get_symbol_info(SYMBOL)
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                self.tick_size = Decimal(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                self.step_size = Decimal(f["stepSize"])
        sym_status = info.get("contractStatus") or info.get("status")
        if sym_status != "TRADING":
            log(f"FATAL: symbol status = {sym_status}")
            return False
        log(f"Symbol OK: tick={fmt(self.tick_size)}, step={fmt(self.step_size)}")

        # Funding interval
        try:
            fi = self.c.public_get("/fapi/v1/fundingInfo", {})
            for entry in fi if isinstance(fi, list) else []:
                if entry.get("symbol") == SYMBOL:
                    self.funding_interval_hours = int(entry.get("fundingIntervalHours", 8))
                    break
        except Exception:
            pass
        log(f"Funding interval: {self.funding_interval_hours}h")

        if self.dry_run:
            log("DRY-RUN: skipping account checks, leverage set, balance check.")
            self.starting_wallet = self.dry_wallet
            return True

        # Live: balance + position check
        try:
            balances = self.c.get_balance()
            usdt = next((b for b in balances if b.get("asset") == "USDT"), None)
            if not usdt:
                log("FATAL: USDT balance not found.")
                return False
            free = Decimal(usdt.get("availableBalance") or usdt.get("balance", "0"))
            self.starting_wallet = free
            log(f"USDT available: ${fmt(free)}")
            if free < MIN_WALLET_USDT:
                log(f"FATAL: balance ${fmt(free)} below minimum ${fmt(MIN_WALLET_USDT)}.")
                return False
        except BinanceApiError as e:
            log(f"FATAL: balance fetch failed: {e}")
            return False

        # Existing position?
        try:
            positions = self.c.get_positions(SYMBOL)
            for p in positions:
                amt = Decimal(p.get("positionAmt", "0"))
                if amt != 0:
                    log(f"FATAL: existing {SYMBOL} position {amt}. Close manually first.")
                    return False
        except BinanceApiError as e:
            log(f"WARN: positions fetch failed: {e}")

        # Cancel any existing ORCA orders (regular + algo)
        try:
            opens = self.c.get_open_orders(SYMBOL)
            if opens:
                log(f"Cancelling {len(opens)} stale {SYMBOL} regular orders.")
                self.c.cancel_all_orders(SYMBOL)
        except BinanceApiError as e:
            log(f"WARN: open-orders fetch failed: {e}")
        try:
            self.c.cancel_all_algo_orders(SYMBOL)
        except BinanceApiError as e:
            log(f"WARN: algo-cancel failed: {e}")

        # Leverage + margin type
        try:
            self.c.set_margin_type(SYMBOL, "ISOLATED")
        except BinanceApiError as e:
            msg = str(e)
            if "-4046" in msg or "No need to change margin type" in msg:
                log(f"set_margin_type note: {e}")
            else:
                log(f"FATAL: cannot confirm isolated margin for {SYMBOL}: {e}")
                log("Disable Multi-Assets mode or set ORCAUSDT to isolated manually before live trading.")
                return False
        try:
            r = self.c.set_leverage(SYMBOL, LEVERAGE)
            log(f"Leverage set to {r.get('leverage', LEVERAGE)}x.")
        except BinanceApiError as e:
            log(f"FATAL: set_leverage failed: {e}")
            return False

        return True

    # ---------- live state ----------
    def fetch(self) -> LiveState | None:
        try:
            pi = self.c.public_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
            k5 = self.c.get_klines(SYMBOL, "5m", limit=30)
            k15 = self.c.get_klines(SYMBOL, "15m", limit=2)
            bk = self.c.get_klines("BTCUSDT", "15m", limit=2)
            t24 = self.c.public_get("/fapi/v1/ticker/24hr", {"symbol": SYMBOL})
            btc24 = self.c.public_get("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"})
            oi = self.c.public_get("/fapi/v1/openInterest", {"symbol": SYMBOL})
        except BinanceApiError as e:
            log(f"fetch error: {e}")
            return None

        closed_5m = k5[:-1] if len(k5) >= 2 else k5
        last5_closed = closed_5m[-1]
        recent = closed_5m[-ADAPTIVE_LOOKBACK_5M:]
        closes_5m = [Decimal(c[4]) for c in closed_5m]
        highs_5m = [Decimal(c[2]) for c in closed_5m]
        lows_5m = [Decimal(c[3]) for c in closed_5m]
        self.closed_5m_closes.clear()
        self.closed_5m_closes.extend(closes_5m[-40:])
        self.closed_5m_highs.clear()
        self.closed_5m_highs.extend(highs_5m[-ADAPTIVE_LOOKBACK_5M:])
        self.closed_5m_lows.clear()
        self.closed_5m_lows.extend(lows_5m[-ADAPTIVE_LOOKBACK_5M:])
        recent_high = max(Decimal(c[2]) for c in recent)
        recent_low = min(Decimal(c[3]) for c in recent)
        recent_close_high = max(Decimal(c[4]) for c in recent)
        last15_closed = k15[-2] if len(k15) >= 2 else k15[-1]
        btc_last = bk[-1]
        bid = Decimal(bt["bidPrice"])
        ask = Decimal(bt["askPrice"])
        mid = (bid + ask) / 2
        funding_raw_pct = float(pi.get("lastFundingRate", "0")) * 100
        funding_8h = funding_raw_pct * (8.0 / self.funding_interval_hours)
        spread_pct = float((ask - bid) / mid * 100) if mid > 0 else 999.0
        btc_open = Decimal(btc_last[1])
        btc_close = Decimal(btc_last[4])
        btc_15m = float((btc_close / btc_open - 1) * 100) if btc_open > 0 else 0.0
        chg_24h_pct = float(t24["priceChangePercent"])
        btc_chg_24h_pct = float(btc24["priceChangePercent"])
        quote_volume_24h = Decimal(t24["quoteVolume"])
        open_interest_usd = Decimal(oi["openInterest"]) * Decimal(pi["markPrice"])
        self.last_slow_refresh_ts = time.time()

        return LiveState(
            mark=Decimal(pi["markPrice"]),
            funding_pct_8h_normalized=funding_8h,
            bid=bid, ask=ask,
            last_5m_open_time=int(last5_closed[0]),
            last_5m_close=Decimal(last5_closed[4]),
            recent_5m_high=recent_high,
            recent_5m_low=recent_low,
            recent_5m_close_high=recent_close_high,
            last_15m_close=Decimal(last15_closed[4]),
            btc_15m_pct=btc_15m,
            spread_pct=spread_pct,
            chg_24h_pct=chg_24h_pct,
            btc_chg_24h_pct=btc_chg_24h_pct,
            outperf_btc_24h_pct=chg_24h_pct - btc_chg_24h_pct,
            quote_volume_24h=quote_volume_24h,
            open_interest_usd=open_interest_usd,
            ema20_5m=ema_decimal(closes_5m, 20),
        )

    def refresh_slow_filters(self, s: LiveState, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_slow_refresh_ts < SLOW_FILTER_REFRESH_SEC:
            return
        try:
            t24 = self.c.public_get("/fapi/v1/ticker/24hr", {"symbol": SYMBOL})
            btc24 = self.c.public_get("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"})
            oi = self.c.public_get("/fapi/v1/openInterest", {"symbol": SYMBOL})
        except BinanceApiError as e:
            log(f"slow-filter refresh error: {e}")
            return
        s.chg_24h_pct = float(t24["priceChangePercent"])
        s.btc_chg_24h_pct = float(btc24["priceChangePercent"])
        s.outperf_btc_24h_pct = s.chg_24h_pct - s.btc_chg_24h_pct
        s.quote_volume_24h = Decimal(t24["quoteVolume"])
        s.open_interest_usd = Decimal(oi["openInterest"]) * s.mark
        self.last_slow_refresh_ts = now

    def maybe_reanchor(self, s: LiveState) -> None:
        if not self.adaptive or self.state != "WAITING_TRIGGER":
            return
        if self.entry_trigger != ENTRY_TRIGGER:
            return
        if s.ask <= ENTRY_MAX_CHASE:
            return

        if self.aggressive:
            closed_5m = self.c.get_klines(SYMBOL, "5m", limit=max(AGGRESSIVE_LOOKBACK_5M + 2, 6))
            closed_recent = (closed_5m[:-1] if len(closed_5m) >= 2 else closed_5m)[-AGGRESSIVE_LOOKBACK_5M:]
            recent_close_high = max(Decimal(c[4]) for c in closed_recent)
            new_trigger = quantize_down(recent_close_high + self.tick_size, self.tick_size)
        else:
            new_trigger = quantize_down(s.recent_5m_high + self.tick_size, self.tick_size)
        if new_trigger <= s.ask:
            new_trigger = quantize_down(s.ask * Decimal("1.006"), self.tick_size)
        self.entry_trigger = new_trigger
        self.entry_max_chase = quantize_down(new_trigger * (Decimal("1") + ADAPTIVE_CHASE_PCT), self.tick_size)
        self.hard_stop = quantize_down(max(PRICE_FLOOR_15M, new_trigger * ADAPTIVE_STOP_FACTOR), self.tick_size)
        self.stop1_trigger = max(STOP1_TRIGGER, quantize_down(new_trigger * Decimal("1.072"), self.tick_size))
        self.stop1_new = quantize_down(max(new_trigger, new_trigger * ADAPTIVE_STOP1_FACTOR), self.tick_size)
        self.trail_arm = max(TRAIL_ARM, quantize_down(new_trigger * Decimal("1.185"), self.tick_size))
        if self.aggressive:
            self.target = quantize_down(max(self.stop1_trigger, new_trigger * AGGRESSIVE_TARGET_FACTOR), self.tick_size)
        else:
            self.target = TARGET
        self.chase_fail_level = quantize_down(self.entry_max_chase * Decimal("1.04"), self.tick_size)
        log(
            f"{'AGGRESSIVE' if self.aggressive else 'ADAPTIVE'}: original entry missed; re-anchored "
            f"trigger>${fmt(self.entry_trigger)} chase<=${fmt(self.entry_max_chase)} "
            f"stop=${fmt(self.hard_stop)} stop1@${fmt(self.stop1_trigger)} "
            f"stop1_new=${fmt(self.stop1_new)} trail@${fmt(self.trail_arm)} "
            f"target=${fmt(self.target)}"
        )
        self._log_wait_ticket()

    def _position_plan(self, entry_price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        margin_usd = (self.starting_wallet * WALLET_UTILIZATION).quantize(Decimal("0.01"))
        notional = margin_usd * LEVERAGE
        qty = quantize_down(notional / entry_price, self.step_size)
        return margin_usd, notional, qty

    def _log_wait_ticket(self) -> None:
        entry = self.entry_trigger
        margin_usd, _, qty = self._position_plan(max(entry, Decimal("0.0001")))
        stop_pnl = self._estimate_pnl_at(entry, qty, self.hard_stop)
        target_pnl = self._estimate_pnl_at(entry, qty, self.target)
        mode = "AGGRESSIVE" if self.aggressive else "WAIT"
        log(
            f"TICKET {mode}: BUY only after 5m close>"
            f"${fmt(self.entry_trigger)} and ask<=${fmt(self.entry_max_chase)} | "
            f"qty~{fmt(qty)} margin=${fmt(margin_usd)} stop=${fmt(self.hard_stop)} "
            f"target=${fmt(self.target)} est_stop={stop_pnl:+.2f} "
            f"est_target={target_pnl:+.2f} USDT"
        )

    # ---------- abort logic (always-on) ----------
    def check_aborts(self, s: LiveState) -> str | None:
        if s.last_15m_close < PRICE_FLOOR_15M:
            return f"15m close ${fmt(s.last_15m_close)} below floor ${fmt(PRICE_FLOOR_15M)}"
        if s.funding_pct_8h_normalized > ABORT_FUND_PCT_8H:
            return f"funding normalized to {s.funding_pct_8h_normalized:+.3f}%/8h (squeeze gone)"
        if s.btc_15m_pct < -MAX_ABORT_BTC_15M_DROP_PCT:
            return f"BTC dumping {s.btc_15m_pct:+.2f}% in 15m"
        if Decimal(str(s.spread_pct)) > ABORT_SPREAD_MAX_PCT:
            return f"spread {s.spread_pct:+.3f}% above {fmt(ABORT_SPREAD_MAX_PCT)}%"
        if s.quote_volume_24h < MIN_24H_FUT_VOL_USD:
            return f"24h futures volume ${fmt(s.quote_volume_24h)} below ${fmt(MIN_24H_FUT_VOL_USD)}"
        # Chase fail only when we're still WAITING
        if self.state == "WAITING_TRIGGER" and s.mark > self.chase_fail_level:
            return f"mark ${fmt(s.mark)} above chase-fail ${fmt(self.chase_fail_level)} (don't chase mid-candle)"
        return None

    # ---------- entry ----------
    def try_entry(self, s: LiveState) -> bool:
        # Only act on a NEW 5m candle close
        if s.last_5m_open_time <= self.last_seen_5m_open_time:
            return False
        self.last_seen_5m_open_time = s.last_5m_open_time

        log(f"new 5m close ${fmt(s.last_5m_close)}  mark ${fmt(s.mark)}  "
            f"funding {s.funding_pct_8h_normalized:+.3f}%/8h  ask ${fmt(s.ask)}")

        if s.last_5m_close <= self.entry_trigger:
            log(f"  trigger NOT hit ({fmt(s.last_5m_close)} <= {fmt(self.entry_trigger)})")
            return False
        if s.funding_pct_8h_normalized > ENTRY_FUND_PCT_8H:
            log(f"  funding {s.funding_pct_8h_normalized:+.3f}%/8h > {ENTRY_FUND_PCT_8H}%/8h")
            return False
        if s.chg_24h_pct <= MIN_24H_CHG_PCT:
            log(f"  24h change {s.chg_24h_pct:+.2f}% <= {MIN_24H_CHG_PCT:.2f}%")
            return False
        if s.outperf_btc_24h_pct < MIN_OUTPERF_BTC_PCT:
            log(f"  ORCA outperformance {s.outperf_btc_24h_pct:+.2f}% < {MIN_OUTPERF_BTC_PCT:.2f}%")
            return False
        if s.quote_volume_24h < MIN_24H_FUT_VOL_USD:
            log(f"  24h futures volume ${fmt(s.quote_volume_24h)} < ${fmt(MIN_24H_FUT_VOL_USD)}")
            return False
        if Decimal(str(s.spread_pct)) > ENTRY_SPREAD_MAX_PCT:
            log(f"  spread {s.spread_pct:+.3f}% > {fmt(ENTRY_SPREAD_MAX_PCT)}%")
            return False
        if s.last_5m_close <= s.ema20_5m:
            log(f"  5m close ${fmt(s.last_5m_close)} <= EMA20 ${fmt(s.ema20_5m)}")
            return False
        if s.btc_15m_pct < -MAX_ENTRY_BTC_15M_DROP_PCT:
            log(f"  BTC 15m {s.btc_15m_pct:+.2f}% < -{MAX_ENTRY_BTC_15M_DROP_PCT:.2f}%")
            return False
        if s.ask > self.entry_max_chase:
            log(f"  ask ${fmt(s.ask)} > chase guard ${fmt(self.entry_max_chase)} -- skip this candle")
            return False

        margin_usd, _, qty = self._position_plan(s.ask)
        if qty * s.ask < Decimal("5"):
            log(f"  notional ${fmt(qty * s.ask)} below $5 minNotional - cannot enter")
            return False

        log(f"  ENTRY: BUY {fmt(qty)} ORCA  margin=${fmt(margin_usd)}  notional=${fmt(qty*s.ask)}")

        if self.dry_run:
            self.entry_price = s.ask
            self.qty = qty
            self.state = "ENTERED"
            self.stop_level = self.hard_stop
            self.high_5m_close = max(self.high_5m_close, s.last_5m_close)
            log(f"  [DRY] simulated fill at ${fmt(s.ask)}; placing simulated stop at ${fmt(self.hard_stop)}")
            self._log_entry_ticket(s)
            return True

        try:
            order = self.c.place_market_order(SYMBOL, "BUY", quantity=fmt(qty))
            self.qty = qty
            avg = order.get("avgPrice") or order.get("price") or s.ask
            self.entry_price = Decimal(str(avg)) if avg else s.ask
            log(f"  filled at ${fmt(self.entry_price)}")
        except BinanceApiError as e:
            log(f"  ENTRY FAILED: {e}")
            return False

        # Place stop-market reduce-only
        try:
            stop = self.c.place_stop_market_order(
                SYMBOL, "SELL",
                stop_price=fmt(self.hard_stop),
                quantity=fmt(qty),
                close_position=False,
                reduce_only=True,
            )
            self.stop_algo_id = int(stop.get("algoId") or stop.get("orderId") or 0)
            self.stop_level = self.hard_stop
            log(f"  stop placed @ ${fmt(self.hard_stop)} algoId={self.stop_algo_id}")
        except BinanceApiError as e:
            log(f"  CRITICAL: stop placement failed: {e} -- closing position immediately")
            try:
                self.c.place_market_order(SYMBOL, "SELL", quantity=fmt(qty), reduce_only=True)
                log("  position emergency-closed")
            except BinanceApiError as e2:
                log(f"  EMERGENCY CLOSE ALSO FAILED: {e2} -- MANUAL INTERVENTION")
            self.state = "EXITED"
            return True

        self.state = "ENTERED"
        return True

    # ---------- management ----------
    def manage(self, s: LiveState) -> None:
        if self.dry_run and self.entry_price and s.mark <= self.stop_level:
            pnl = self._estimate_current_pnl(s.mark)
            log(f"STOP HIT mark=${fmt(s.mark)} stop=${fmt(self.stop_level)}  [DRY pnl={pnl:+.2f} USDT]")
            self.state = "EXITED"
            return

        # Detect close (position == 0)
        now = time.time()
        if not self.dry_run and now - self.last_position_check_ts >= POSITION_CHECK_SEC:
            self.last_position_check_ts = now
            try:
                positions = self.c.get_positions(SYMBOL)
                amt = Decimal("0")
                for p in positions:
                    amt = Decimal(p.get("positionAmt", "0"))
                    break
                if amt == 0:
                    self._finalize()
                    return
            except BinanceApiError as e:
                log(f"position check error: {e}")

        # Track 5m highs
        if s.last_5m_close > self.high_5m_close:
            self.high_5m_close = s.last_5m_close

        # Target hit?
        if s.mark >= self.target:
            log(f"TARGET HIT mark=${fmt(s.mark)} -- closing position")
            self._market_close(exit_price=s.mark, reason="target")
            return

        # First stop bump (1.830 trigger -> 1.670 stop)
        if not self.first_stop_moved and s.mark >= self.stop1_trigger:
            self._reset_stop(self.stop1_new, reason=f"mark hit {fmt(self.stop1_trigger)}")
            self.first_stop_moved = True
            return

        # Trailing (above 2.05)
        if s.mark >= self.trail_arm and self.high_5m_close > 0:
            new_trail = (self.high_5m_close * TRAIL_FACTOR).quantize(self.tick_size, rounding=ROUND_DOWN)
            if new_trail > self.stop_level + self.tick_size:
                self._reset_stop(new_trail, reason=f"trail at 8% below 5m high {fmt(self.high_5m_close)}")
        self._log_status(s)

    def _estimate_pnl_at(self, entry_price: Decimal, qty: Decimal, exit_price: Decimal) -> float:
        if not entry_price or not qty:
            return 0.0
        gross = (exit_price - entry_price) * qty
        fees = (entry_price * qty + exit_price * qty) * Decimal("0.0005")
        return float(gross - fees)

    def _estimate_current_pnl(self, exit_price: Decimal) -> float:
        if not self.entry_price:
            return 0.0
        return self._estimate_pnl_at(self.entry_price, self.qty, exit_price)

    def _dry_equity(self, exit_price: Decimal) -> float:
        return float(self.starting_wallet) + self._estimate_current_pnl(exit_price)

    def _log_entry_ticket(self, s: LiveState) -> None:
        stop_pnl = self._estimate_current_pnl(self.stop_level)
        target_pnl = self._estimate_current_pnl(self.target)
        log(
            "TICKET ENTERED: "
            f"entry=${fmt(self.entry_price or s.ask)} qty={fmt(self.qty)} "
            f"stop=${fmt(self.stop_level)} target=${fmt(self.target)} "
            f"stop_pnl={stop_pnl:+.2f} target_pnl={target_pnl:+.2f} USDT "
            f"dry_equity_now=${self._dry_equity(s.mark):.2f}"
        )

    def _log_status(self, s: LiveState) -> None:
        now = time.time()
        if now - self.last_status_ts < self.status_every_sec:
            return
        self.last_status_ts = now
        if self.state == "ENTERED":
            log(
                "STATUS ENTERED: "
                f"mark=${fmt(s.mark)} entry=${fmt(self.entry_price or 0)} "
                f"stop=${fmt(self.stop_level)} target=${fmt(self.target)} "
                f"unrealized={self._estimate_current_pnl(s.mark):+.2f} USDT "
                f"dry_equity=${self._dry_equity(s.mark):.2f}"
            )
        else:
            gap = (self.entry_trigger / s.mark - Decimal("1")) * Decimal("100") if s.mark > 0 else Decimal("0")
            log(
                "STATUS WAIT: "
                f"mark=${fmt(s.mark)} 5m_close=${fmt(s.last_5m_close)} "
                f"trigger>${fmt(self.entry_trigger)} chase<=${fmt(self.entry_max_chase)} "
                f"gap={float(gap):+.2f}% funding={s.funding_pct_8h_normalized:+.3f}%/8h"
            )

    def _reset_stop(self, new_level: Decimal, reason: str) -> None:
        log(f"raising stop to ${fmt(new_level)}  ({reason})")
        if self.dry_run:
            self.stop_level = new_level
            return
        old_algo_id = self.stop_algo_id
        try:
            r = self.c.place_stop_market_order(
                SYMBOL, "SELL",
                stop_price=fmt(new_level),
                quantity=fmt(self.qty),
                close_position=False,
                reduce_only=True,
            )
            self.stop_algo_id = int(r.get("algoId") or r.get("orderId") or 0)
            self.stop_level = new_level
        except BinanceApiError as e:
            log(f"  CRITICAL: stop replacement failed; old stop remains active: {e}")
            return
        try:
            if old_algo_id:
                self.c.cancel_algo_order(SYMBOL, algo_id=old_algo_id)
        except BinanceApiError as e:
            log(f"  old stop cancel error (new stop is active): {e}")

    def _market_close(self, exit_price: Decimal | None = None, reason: str = "close") -> None:
        if self.dry_run:
            price = exit_price or self.target
            pnl_txt = ""
            if self.entry_price:
                pnl_txt = f" price=${fmt(price)} pnl={self._estimate_current_pnl(price):+.2f} dry_equity=${self._dry_equity(price):.2f}"
            log(f"[DRY] simulated close reason={reason}{pnl_txt}")
            self.state = "EXITED"
            return
        try:
            self.c.place_market_order(SYMBOL, "SELL", quantity=fmt(self.qty), reduce_only=True)
        except BinanceApiError as e:
            log(f"  close failed; protective stop left active: {e}")
            return
        try:
            self.c.cancel_all_algo_orders(SYMBOL)
        except BinanceApiError:
            pass
        self.state = "EXITED"
        self._finalize()

    def _finalize(self) -> None:
        if self.dry_run:
            return
        try:
            balances = self.c.get_balance()
            usdt = next((b for b in balances if b.get("asset") == "USDT"), None)
            now = Decimal(usdt.get("availableBalance") or usdt.get("balance", "0")) if usdt else Decimal("0")
            pnl = now - self.starting_wallet
            log(f"=== EXITED  start=${fmt(self.starting_wallet)} end=${fmt(now)} pnl=${fmt(pnl)} ===")
        except Exception as e:
            log(f"finalize error: {e}")
        self.state = "EXITED"

    # ---------- main ----------
    def _log_start(self, engine: str) -> None:
        log(f"start  state={self.state}  trigger=close>${fmt(self.entry_trigger)}  "
            f"chase<=${fmt(self.entry_max_chase)}  stop=${fmt(self.hard_stop)}  target=${fmt(self.target)}")
        self._log_wait_ticket()
        log(engine)

    def _handle_abort(self, s: LiveState, abort_reason: str) -> None:
        log(f"ABORT: {abort_reason}")
        if self.state == "ENTERED":
            self._market_close(exit_price=s.mark, reason=f"abort: {abort_reason}")
        else:
            self.state = "EXITED"

    def _process_state(self, s: LiveState, new_5m_close: bool = False) -> None:
        self.refresh_slow_filters(s)
        self.maybe_reanchor(s)

        abort_reason = self.check_aborts(s)
        if abort_reason:
            self._handle_abort(s, abort_reason)
            return

        if self.state == "WAITING_TRIGGER":
            if new_5m_close:
                self.try_entry(s)
            else:
                self._log_status(s)
        elif self.state == "ENTERED":
            self.manage(s)

    def _run_polling(self) -> int:
        if not self.preflight():
            return 2

        self._log_start(f"REST poll every {self.poll_sec}s")

        while self.state != "EXITED":
            self.cycles += 1
            s = self.fetch()
            if s is None:
                time.sleep(self.poll_sec)
                continue

            self._process_state(s, new_5m_close=True)

            if self.max_cycles and self.cycles >= self.max_cycles and self.state != "EXITED":
                log(f"max cycles {self.max_cycles} reached; stopping run.")
                if self.state == "ENTERED":
                    self._market_close(exit_price=s.mark, reason="max-cycles")
                else:
                    self.state = "EXITED"
                break

            time.sleep(self.poll_sec)

        log("bot stopped.")
        return 0

    def _update_spread(self, s: LiveState) -> None:
        mid = (s.bid + s.ask) / 2
        s.spread_pct = float((s.ask - s.bid) / mid * 100) if mid > 0 else 999.0

    def _apply_ws_event(self, s: LiveState, data: dict[str, Any]) -> bool:
        if not isinstance(data, dict):
            return False
        event = data.get("e")
        symbol = data.get("s")

        if (event == "bookTicker" or "u" in data) and symbol == self.symbol:
            bid = Decimal(data.get("b", "0"))
            ask = Decimal(data.get("a", "0"))
            if bid > 0 and ask > 0:
                s.bid = bid
                s.ask = ask
                self._update_spread(s)
            return False

        if event == "markPriceUpdate" and symbol == self.symbol:
            mark = Decimal(data.get("p", "0"))
            if mark > 0:
                s.mark = mark
            funding_raw_pct = float(data.get("r", "0")) * 100
            s.funding_pct_8h_normalized = funding_raw_pct * (8.0 / self.funding_interval_hours)
            return False

        if event == "kline":
            kline = data.get("k", {})
            k_symbol = kline.get("s")
            interval = kline.get("i")
            close = Decimal(kline.get("c", "0"))
            if k_symbol == "BTCUSDT" and interval == "15m":
                open_price = Decimal(kline.get("o", "0"))
                if open_price > 0:
                    s.btc_15m_pct = float((close / open_price - 1) * 100)
                return False

            if k_symbol != SYMBOL:
                return False

            if interval == "15m" and kline.get("x"):
                s.last_15m_close = close
                return False

            if interval == "5m" and kline.get("x"):
                high = Decimal(kline.get("h", "0"))
                low = Decimal(kline.get("l", "0"))
                s.last_5m_open_time = int(kline.get("t", 0))
                s.last_5m_close = close
                self.closed_5m_closes.append(close)
                self.closed_5m_highs.append(high)
                self.closed_5m_lows.append(low)
                if self.closed_5m_highs:
                    s.recent_5m_high = max(self.closed_5m_highs)
                if self.closed_5m_lows:
                    s.recent_5m_low = min(self.closed_5m_lows)
                if self.closed_5m_closes:
                    recent_closes = list(self.closed_5m_closes)[-ADAPTIVE_LOOKBACK_5M:]
                    s.recent_5m_close_high = max(recent_closes)
                    s.ema20_5m = ema_decimal(list(self.closed_5m_closes), 20)
                return True

        if event == "forceOrder":
            order = data.get("o", {})
            if order.get("s") == SYMBOL:
                qty = Decimal(order.get("q", "0"))
                price = Decimal(order.get("ap") or order.get("p") or "0")
                usd = qty * price
                if usd >= Decimal("50000"):
                    log(f"LIQ {SYMBOL} {order.get('S')} ${fmt(usd)} @ ${fmt(price)}")
            return False

        return False

    async def _ws_consumer(self, url: str, queue: asyncio.Queue[dict[str, Any]], label: str) -> None:
        assert websockets is not None
        while self.state != "EXITED":
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=20, max_queue=1024) as ws:
                    log(f"WS {label} connected.")
                    while self.state != "EXITED":
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
                            queue.put_nowait(data)
                        except asyncio.QueueFull:
                            log("WARN: WebSocket queue full; dropping market event.")
                        except Exception as exc:
                            log(f"WS {label} parse error: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.state != "EXITED":
                    log(f"WS {label} error: {exc}; reconnecting in {WS_RECONNECT_SEC}s")
                    await asyncio.sleep(WS_RECONNECT_SEC)

    async def _run_websocket(self) -> int:
        if websockets is None:
            log("ERROR: websockets package is not installed. Run `pip install -r requirements.txt` or use --rest-poll.")
            return 2
        if not self.preflight():
            return 2

        s = self.fetch()
        if s is None:
            return 2

        self._log_start("market data engine: WebSocket (/public bookTicker + /market mark/kline/flow)")
        self._process_state(s, new_5m_close=True)

        if self.state == "EXITED":
            log("bot stopped.")
            return 0

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        public_streams = f"{SYMBOL.lower()}@bookTicker"
        market_streams = "/".join(
            [
                f"{SYMBOL.lower()}@markPrice@1s",
                f"{SYMBOL.lower()}@kline_5m",
                f"{SYMBOL.lower()}@kline_15m",
                "btcusdt@kline_15m",
                f"{SYMBOL.lower()}@aggTrade",
                "!forceOrder@arr",
            ]
        )
        public_url = f"wss://fstream.binance.com/public/stream?streams={public_streams}"
        market_url = f"wss://fstream.binance.com/market/stream?streams={market_streams}"
        tasks = [
            asyncio.create_task(self._ws_consumer(public_url, queue, "/public")),
            asyncio.create_task(self._ws_consumer(market_url, queue, "/market")),
        ]

        try:
            while self.state != "EXITED":
                if self.max_cycles and self.cycles >= self.max_cycles:
                    log(f"max events {self.max_cycles} reached; stopping run.")
                    if self.state == "ENTERED":
                        self._market_close(exit_price=s.mark, reason="max-cycles")
                    else:
                        self.state = "EXITED"
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    self._log_status(s)
                    continue
                self.cycles += 1
                new_5m_close = self._apply_ws_event(s, data)
                self._process_state(s, new_5m_close=new_5m_close)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        log("bot stopped.")
        return 0

    def run(self) -> int:
        if self.use_websocket:
            return asyncio.run(self._run_websocket())
        return self._run_polling()


class ActivePaperBot:
    """Dry-only, live-data futures simulator for active WebSocket trading."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        symbol: str = SYMBOL,
        dry_wallet: Decimal = DEFAULT_DRY_WALLET,
        max_seconds: int = ACTIVE_DEFAULT_SECONDS,
        max_trades: int = 0,
        status_every_sec: int = STATUS_EVERY_SEC,
        force_after_sec: float = ACTIVE_FORCE_AFTER_SEC,
        session_target: Decimal = ACTIVE_SESSION_TARGET_USDT,
        session_loss: Decimal = ACTIVE_SESSION_LOSS_USDT,
        leverage: int = ACTIVE_LEVERAGE,
        margin_fraction: Decimal = ACTIVE_MARGIN_FRACTION,
        take_profit_pct: Decimal = ACTIVE_TAKE_PROFIT_PCT,
        stop_loss_pct: Decimal = ACTIVE_STOP_LOSS_PCT,
        trail_arm_pct: Decimal = ACTIVE_TRAIL_ARM_PCT,
        trail_giveback_pct: Decimal = ACTIVE_TRAIL_GIVEBACK_PCT,
        max_hold_sec: float = ACTIVE_MAX_HOLD_SEC,
        use_flow_flip_exit: bool = True,
        confirm_move_pct: float = ACTIVE_CONFIRM_MOVE_PCT,
    ):
        self.c = client
        self.symbol = symbol.upper()
        self.cash = dry_wallet
        self.starting_cash = dry_wallet
        self.max_seconds = max(10, max_seconds)
        self.max_trades = max_trades
        self.status_every_sec = max(5, status_every_sec)
        self.force_after_sec = max(0.0, force_after_sec)
        self.session_target = session_target
        self.session_loss = session_loss
        self.leverage = max(1, leverage)
        self.margin_fraction = min(max(margin_fraction, Decimal("0.01")), Decimal("1.00"))
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trail_arm_pct = trail_arm_pct
        self.trail_giveback_pct = trail_giveback_pct
        self.max_hold_sec = max_hold_sec
        self.use_flow_flip_exit = use_flow_flip_exit
        self.confirm_move_pct = max(0.0, confirm_move_pct)
        self.tick_size = Decimal("0.001")
        self.step_size = Decimal("0.1")
        self.bid = Decimal("0")
        self.ask = Decimal("0")
        self.mark = Decimal("0")
        self.spread_pct = 999.0
        self.position: ActivePaperPosition | None = None
        self.ticks: deque[tuple[float, Decimal]] = deque(maxlen=3000)
        self.trade_flow: deque[tuple[float, Decimal, Decimal]] = deque(maxlen=3000)
        self.start_ts = 0.0
        self.last_entry_ts = 0.0
        self.last_status_ts = 0.0
        self.last_book_ts = 0.0
        self.pending_side: str | None = None
        self.pending_reason = ""
        self.pending_price = Decimal("0")
        self.pending_ts = 0.0
        self.trades = 0
        self.wins = 0
        self.stop_flag = False

    def preflight(self) -> bool:
        log(f"=== {self.symbol} active paper bot LIVE-DATA DRY-RUN ===")
        info = self.c.get_symbol_info(self.symbol)
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                self.tick_size = Decimal(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                self.step_size = Decimal(f["stepSize"])
        sym_status = info.get("contractStatus") or info.get("status")
        if sym_status != "TRADING":
            log(f"FATAL: symbol status = {sym_status}")
            return False
        try:
            book = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": self.symbol})
            pi = self.c.public_get("/fapi/v1/premiumIndex", {"symbol": self.symbol})
        except BinanceApiError as e:
            log(f"FATAL: seed fetch failed: {e}")
            return False
        self.bid = Decimal(book["bidPrice"])
        self.ask = Decimal(book["askPrice"])
        self.mark = Decimal(pi["markPrice"])
        self._update_spread()
        now = time.time()
        self.ticks.append((now, self.mid_price()))
        log(
            f"seed {self.symbol} bid=${fmt(self.bid)} ask=${fmt(self.ask)} mark=${fmt(self.mark)} "
            f"spread={self.spread_pct:.3f}% wallet=${fmt(self.cash)} "
            f"lev={self.leverage}x margin={self.margin_fraction * 100}%"
        )
        return True

    def mid_price(self) -> Decimal:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.mark

    def _update_spread(self) -> None:
        mid = self.mid_price()
        self.spread_pct = float((self.ask - self.bid) / mid * 100) if mid > 0 else 999.0

    def _apply_synthetic_book_from_trade(self, price: Decimal) -> None:
        if price <= 0 or time.time() - self.last_book_ts <= 2.0:
            return
        spread_pct = Decimal(str(min(max(self.spread_pct, 0.02), 0.20)))
        half_spread = price * spread_pct / Decimal("200")
        bid = quantize_down(price - half_spread, self.tick_size)
        ask = quantize_down(price + half_spread + self.tick_size, self.tick_size)
        if bid > 0 and ask > bid:
            self.bid = bid
            self.ask = ask
            self._update_spread()

    def _equity(self) -> Decimal:
        if not self.position:
            return self.cash
        return self.cash + self._gross_unrealized(self.current_exit_price())

    def current_exit_price(self) -> Decimal:
        if not self.position:
            return self.mid_price()
        return self.bid if self.position.side == "LONG" else self.ask

    def _gross_unrealized(self, exit_price: Decimal) -> Decimal:
        if not self.position:
            return Decimal("0")
        p = self.position
        if p.side == "LONG":
            return (exit_price - p.entry_price) * p.qty
        return (p.entry_price - exit_price) * p.qty

    def _velocity_pct(self, window_sec: float) -> float:
        if len(self.ticks) < 2:
            return 0.0
        now = time.time()
        latest = self.ticks[-1][1]
        oldest = None
        for ts, price in reversed(self.ticks):
            if now - ts >= window_sec:
                oldest = price
                break
        if oldest is None:
            oldest = self.ticks[0][1]
        if oldest <= 0:
            return 0.0
        return float((latest / oldest - 1) * 100)

    def _flow_ratio(self, window_sec: float) -> float:
        now = time.time()
        signed = Decimal("0")
        total = Decimal("0")
        for ts, sign, notional in reversed(self.trade_flow):
            if now - ts > window_sec:
                break
            signed += sign * notional
            total += notional
        return float(signed / total) if total > 0 else 0.0

    def _price_range_pct(self, window_sec: float) -> float:
        now = time.time()
        prices: list[Decimal] = []
        for ts, price in reversed(self.ticks):
            if now - ts > window_sec:
                break
            prices.append(price)
        if len(prices) < 2:
            return 0.0
        low = min(prices)
        high = max(prices)
        mid = self.mid_price()
        return float((high - low) / mid * 100) if mid > 0 else 0.0

    def _entry_signal(self) -> tuple[str | None, str]:
        if self.spread_pct > float(ACTIVE_MAX_SPREAD_PCT):
            return None, f"spread {self.spread_pct:.3f}%"
        if len(self.ticks) < 8 or len(self.trade_flow) < 3:
            return None, "warming up"

        v2 = self._velocity_pct(2)
        v8 = self._velocity_pct(8)
        flow3 = self._flow_ratio(3)
        flow8 = self._flow_ratio(8)
        range12 = self._price_range_pct(ACTIVE_WHIPSAW_GUARD_SEC)
        waited = time.time() - max(self.start_ts, self.last_entry_ts)

        if abs(v8) > ACTIVE_MAX_V8_PCT:
            return None, f"extended v8={v8:+.3f}%"
        if range12 > 0 and range12 < 0.18:
            return None, f"range too tight {range12:.3f}%"

        if v2 >= ACTIVE_MIN_V2_PCT and v8 >= ACTIVE_MIN_V8_PCT and flow3 >= ACTIVE_MIN_FLOW3 and flow8 >= ACTIVE_MIN_FLOW8:
            return "LONG", f"momentum v2={v2:+.3f}% v8={v8:+.3f}% flow3={flow3:+.2f} flow8={flow8:+.2f}"
        if v2 <= -ACTIVE_MIN_V2_PCT and v8 <= -ACTIVE_MIN_V8_PCT and flow3 <= -ACTIVE_MIN_FLOW3 and flow8 <= -ACTIVE_MIN_FLOW8:
            return "SHORT", f"momentum v2={v2:+.3f}% v8={v8:+.3f}% flow3={flow3:+.2f} flow8={flow8:+.2f}"

        if waited >= self.force_after_sec:
            score = v8 + flow8 * 0.08
            if abs(score) >= 0.18 and abs(flow8) >= ACTIVE_MIN_FLOW8 and ((score > 0 and flow8 > 0) or (score < 0 and flow8 < 0)):
                side = "LONG" if score > 0 else "SHORT"
                return side, f"forced best-current-edge score={score:+.3f} v8={v8:+.3f}% flow8={flow8:+.2f}"
        return None, f"no edge v2={v2:+.3f}% v8={v8:+.3f}% flow3={flow3:+.2f} flow8={flow8:+.2f}"

    def _open_position(self, side: str, reason: str) -> None:
        price = self.ask if side == "LONG" else self.bid
        if price <= 0:
            return
        margin = (self.cash * self.margin_fraction).quantize(Decimal("0.01"))
        notional = margin * self.leverage
        qty = quantize_down(notional / price, self.step_size)
        if qty * price < Decimal("5"):
            log(f"ACTIVE skip: notional ${fmt(qty * price)} below $5")
            return
        entry_fee = qty * price * ACTIVE_TAKER_FEE
        self.cash -= entry_fee
        if side == "LONG":
            stop_price = quantize_down(price * (Decimal("1") - self.stop_loss_pct), self.tick_size)
            target_price = quantize_down(price * (Decimal("1") + self.take_profit_pct), self.tick_size)
        else:
            stop_price = quantize_down(price * (Decimal("1") + self.stop_loss_pct), self.tick_size)
            target_price = quantize_down(price * (Decimal("1") - self.take_profit_pct), self.tick_size)
        self.position = ActivePaperPosition(
            side=side,
            qty=qty,
            entry_price=price,
            entry_ts=time.time(),
            margin=margin,
            notional=qty * price,
            entry_fee=entry_fee,
            stop_price=stop_price,
            target_price=target_price,
            best_exit_price=price,
        )
        self.last_entry_ts = time.time()
        log(
            f"ACTIVE OPEN {self.symbol} {side} qty={fmt(qty)} entry=${fmt(price)} margin=${fmt(margin)} "
            f"notional=${fmt(qty * price)} stop=${fmt(stop_price)} target=${fmt(target_price)} "
            f"fee=${entry_fee:.4f} reason={reason}"
        )

    def _close_position(self, reason: str) -> None:
        if not self.position:
            return
        p = self.position
        exit_price = self.bid if p.side == "LONG" else self.ask
        if exit_price <= 0:
            exit_price = self.mark
        gross = self._gross_unrealized(exit_price)
        exit_fee = p.qty * exit_price * ACTIVE_TAKER_FEE
        realized_after_entry_fee = gross - exit_fee
        total_trade_pnl = gross - p.entry_fee - exit_fee
        self.cash += realized_after_entry_fee
        self.trades += 1
        if total_trade_pnl > 0:
            self.wins += 1
        self.position = None
        self.last_entry_ts = time.time()
        log(
            f"ACTIVE CLOSE {self.symbol} {p.side} exit=${fmt(exit_price)} gross=${gross:+.4f} "
            f"fees=${(p.entry_fee + exit_fee):.4f} pnl=${total_trade_pnl:+.4f} "
            f"equity=${self.cash:.4f} reason={reason}"
        )

    def _manage_position(self) -> None:
        if not self.position:
            return
        p = self.position
        exit_price = self.current_exit_price()
        if exit_price <= 0:
            return

        if p.side == "LONG":
            if exit_price > p.best_exit_price:
                p.best_exit_price = exit_price
            if p.best_exit_price >= p.entry_price * (Decimal("1") + self.trail_arm_pct):
                trail = quantize_down(p.best_exit_price * (Decimal("1") - self.trail_giveback_pct), self.tick_size)
                p.trailing_stop = max(p.trailing_stop or trail, trail)
            if exit_price <= p.stop_price:
                self._close_position("stop")
                return
            if p.trailing_stop and exit_price <= p.trailing_stop:
                self._close_position("trail")
                return
            if exit_price >= p.target_price:
                self._close_position("target")
                return
            if self.use_flow_flip_exit and (
                self._gross_unrealized(exit_price) < -p.entry_fee
                and time.time() - p.entry_ts >= 5
                and self._velocity_pct(2) < -0.18
                and self._flow_ratio(3) < -0.25
            ):
                self._close_position("flow-flip")
                return
        else:
            if exit_price < p.best_exit_price:
                p.best_exit_price = exit_price
            if p.best_exit_price <= p.entry_price * (Decimal("1") - self.trail_arm_pct):
                trail = quantize_down(p.best_exit_price * (Decimal("1") + self.trail_giveback_pct), self.tick_size)
                p.trailing_stop = min(p.trailing_stop or trail, trail)
            if exit_price >= p.stop_price:
                self._close_position("stop")
                return
            if p.trailing_stop and exit_price >= p.trailing_stop:
                self._close_position("trail")
                return
            if exit_price <= p.target_price:
                self._close_position("target")
                return
            if self.use_flow_flip_exit and (
                self._gross_unrealized(exit_price) < -p.entry_fee
                and time.time() - p.entry_ts >= 5
                and self._velocity_pct(2) > 0.18
                and self._flow_ratio(3) > 0.25
            ):
                self._close_position("flow-flip")
                return

        if time.time() - p.entry_ts >= self.max_hold_sec:
            self._close_position("max-hold")

    def _maybe_enter(self) -> None:
        if self.position or time.time() - self.last_entry_ts < ACTIVE_COOLDOWN_SEC:
            return
        now = time.time()
        mid = self.mid_price()
        if self.pending_side:
            if now - self.pending_ts > ACTIVE_CONFIRM_TIMEOUT_SEC:
                log(f"ACTIVE pending {self.pending_side} expired without continuation")
                self.pending_side = None
            else:
                side_now, _ = self._entry_signal()
                if self.pending_side == "LONG":
                    move = float((mid / self.pending_price - 1) * 100) if self.pending_price > 0 else 0.0
                else:
                    move = float((self.pending_price / mid - 1) * 100) if mid > 0 else 0.0
                if move >= self.confirm_move_pct and side_now == self.pending_side:
                    reason = f"confirmed +{move:.3f}% after pending: {self.pending_reason}"
                    side = self.pending_side
                    self.pending_side = None
                    self._open_position(side, reason)
                    return
                if move <= -self.confirm_move_pct:
                    log(f"ACTIVE pending {self.pending_side} cancelled move={move:+.3f}%")
                    self.pending_side = None
            return
        side, reason = self._entry_signal()
        if side:
            if self.confirm_move_pct <= 0:
                self._open_position(side, reason)
                return
            self.pending_side = side
            self.pending_reason = reason
            self.pending_price = mid
            self.pending_ts = now
            log(f"ACTIVE PENDING {side} price=${fmt(mid)} confirm>={self.confirm_move_pct:.3f}% reason={reason}")

    def _log_status(self) -> None:
        now = time.time()
        if now - self.last_status_ts < self.status_every_sec:
            return
        self.last_status_ts = now
        v2 = self._velocity_pct(2)
        v8 = self._velocity_pct(8)
        flow3 = self._flow_ratio(3)
        pos = "flat"
        if self.position:
            pos = (
                f"{self.position.side} entry=${fmt(self.position.entry_price)} "
                f"unrl={self._gross_unrealized(self.current_exit_price()):+.4f}"
            )
        log(
            f"ACTIVE STATUS equity=${self._equity():.4f} realized=${(self.cash - self.starting_cash):+.4f} "
            f"bid=${fmt(self.bid)} ask=${fmt(self.ask)} spread={self.spread_pct:.3f}% "
            f"v2={v2:+.3f}% v8={v8:+.3f}% flow3={flow3:+.2f} trades={self.trades} pos={pos}"
        )

    def _apply_event(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        event = data.get("e")
        symbol = data.get("s")
        now = time.time()

        if (event == "bookTicker" or "u" in data) and symbol == SYMBOL:
            bid = Decimal(data.get("b", "0"))
            ask = Decimal(data.get("a", "0"))
            if bid > 0 and ask > 0:
                self.bid = bid
                self.ask = ask
                self.last_book_ts = now
                self._update_spread()
                self.ticks.append((now, self.mid_price()))
            return

        if event == "markPriceUpdate" and symbol == SYMBOL:
            mark = Decimal(data.get("p", "0"))
            if mark > 0:
                self.mark = mark
            return

        if event == "aggTrade" and symbol == self.symbol:
            price = Decimal(data.get("p", "0"))
            qty = Decimal(data.get("q", "0"))
            if price <= 0 or qty <= 0:
                return
            self.mark = price
            self._apply_synthetic_book_from_trade(price)
            self.ticks.append((now, price))
            is_buyer_maker = bool(data.get("m", False))
            sign = Decimal("-1") if is_buyer_maker else Decimal("1")
            self.trade_flow.append((now, sign, price * qty))

    async def _ws_consumer(self, url: str, queue: asyncio.Queue[dict[str, Any]], label: str) -> None:
        assert websockets is not None
        while not self.stop_flag:
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=20, max_queue=1024) as ws:
                    log(f"ACTIVE WS {label} connected.")
                    while not self.stop_flag:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
                            queue.put_nowait(data)
                        except asyncio.QueueFull:
                            log("WARN: active paper queue full; dropping market event.")
                        except Exception as exc:
                            log(f"ACTIVE WS {label} parse error: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stop_flag:
                    log(f"ACTIVE WS {label} error: {exc}; reconnecting in {WS_RECONNECT_SEC}s")
                    await asyncio.sleep(WS_RECONNECT_SEC)

    async def run_async(self) -> int:
        if websockets is None:
            log("ERROR: websockets package is not installed.")
            return 2
        if not self.preflight():
            return 2

        self.start_ts = time.time()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=3000)
        public_url = f"wss://fstream.binance.com/public/stream?streams={self.symbol.lower()}@bookTicker"
        market_streams = f"{self.symbol.lower()}@markPrice@1s/{self.symbol.lower()}@aggTrade"
        market_url = f"wss://fstream.binance.com/market/stream?streams={market_streams}"
        tasks = [
            asyncio.create_task(self._ws_consumer(public_url, queue, "/public")),
            asyncio.create_task(self._ws_consumer(market_url, queue, "/market")),
        ]
        log(
            f"ACTIVE paper run: seconds={self.max_seconds} target={self.session_target:+.2f} "
            f"loss={self.session_loss:+.2f} max_trades={self.max_trades or 'unlimited'}"
        )

        try:
            while not self.stop_flag:
                elapsed = time.time() - self.start_ts
                realized = self.cash - self.starting_cash
                if elapsed >= self.max_seconds:
                    self.stop_flag = True
                    break
                if realized >= self.session_target:
                    log(f"ACTIVE session target hit: {realized:+.4f} USDT")
                    self.stop_flag = True
                    break
                if realized <= self.session_loss:
                    log(f"ACTIVE session loss limit hit: {realized:+.4f} USDT")
                    self.stop_flag = True
                    break
                if self.max_trades and self.trades >= self.max_trades:
                    self.stop_flag = True
                    break

                try:
                    data = await asyncio.wait_for(queue.get(), timeout=1)
                    self._apply_event(data)
                except asyncio.TimeoutError:
                    pass

                self._manage_position()
                self._maybe_enter()
                self._log_status()
        finally:
            if self.position:
                self._close_position("session-end")
            self.stop_flag = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        realized = self.cash - self.starting_cash
        win_rate = (self.wins / self.trades * 100) if self.trades else 0.0
        log(
            f"ACTIVE FINAL start=${self.starting_cash:.4f} end=${self.cash:.4f} "
            f"pnl=${realized:+.4f} return={float(realized / self.starting_cash * 100):+.2f}% "
            f"trades={self.trades} wins={self.wins} winrate={win_rate:.0f}%"
        )
        return 0

    def run(self) -> int:
        return asyncio.run(self.run_async())


def select_active_paper_symbol(client: BinanceFuturesClient, scan_limit: int = 30) -> tuple[str, str]:
    """Pick a liquid USD-M symbol with the strongest fresh 1m movement."""
    skip = {"B3USDT", "DEGENUSDT", "BOBUSDT", "ZKJUSDT", "IRUSDT", "DAMUSDT"}
    try:
        tickers = client.public_get("/fapi/v1/ticker/24hr", {})
        books_raw = client.public_get("/fapi/v1/ticker/bookTicker", {})
    except BinanceApiError as e:
        log(f"symbol scan failed: {e}; falling back to {SYMBOL}")
        return SYMBOL, "scan failed"
    books = {b.get("symbol"): b for b in books_raw if isinstance(b, dict)}
    candidates: list[dict[str, Any]] = []
    for t in tickers if isinstance(tickers, list) else []:
        sym = str(t.get("symbol", ""))
        if not sym.endswith("USDT") or sym in skip:
            continue
        try:
            quote_volume = Decimal(t.get("quoteVolume", "0"))
            pct24 = abs(float(t.get("priceChangePercent", "0")))
        except Exception:
            continue
        if quote_volume < Decimal("30000000") or pct24 < 1.0:
            continue
        book = books.get(sym)
        if not book:
            continue
        try:
            bid = Decimal(book["bidPrice"])
            ask = Decimal(book["askPrice"])
            mid = (bid + ask) / 2
            spread_pct = float((ask - bid) / mid * 100) if mid > 0 else 999.0
        except Exception:
            continue
        if spread_pct > float(ACTIVE_MAX_SPREAD_PCT):
            continue
        candidates.append({"symbol": sym, "quoteVolume": quote_volume, "pct24": pct24, "spread": spread_pct})

    candidates.sort(key=lambda x: x["quoteVolume"], reverse=True)
    best: tuple[float, str, str] | None = None
    for item in candidates[:scan_limit]:
        sym = item["symbol"]
        try:
            kl = client.get_klines(sym, "1m", limit=6)
        except Exception:
            continue
        if len(kl) < 4:
            continue
        try:
            open_3m = Decimal(kl[-4][1])
            close_now = Decimal(kl[-1][4])
            high = max(Decimal(k[2]) for k in kl[-4:])
            low = min(Decimal(k[3]) for k in kl[-4:])
            momentum = float((close_now / open_3m - 1) * 100) if open_3m > 0 else 0.0
            range_pct = float((high - low) / close_now * 100) if close_now > 0 else 0.0
        except Exception:
            continue
        score = abs(momentum) * 3.0 + range_pct + min(float(item["quoteVolume"] / Decimal("100000000")), 5.0) * 0.08
        reason = (
            f"scan score={score:.3f} 3m={momentum:+.3f}% range={range_pct:.3f}% "
            f"24h={item['pct24']:.2f}% qVol=${item['quoteVolume']:.0f} spread={item['spread']:.3f}%"
        )
        if best is None or score > best[0]:
            best = (score, sym, reason)
    if best is None:
        return SYMBOL, "no scanner candidate"
    return best[1], best[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="place real Binance USD-M futures orders")
    ap.add_argument("--confirm-live", action="store_true", help="required with --live unless BINANCE_LIVE_CONFIRM is set")
    ap.add_argument("--adaptive", action="store_true", help="re-anchor after original entry is missed")
    ap.add_argument("--aggressive", action="store_true", help="dry-run only: faster-entry profile using recent close breakout and nearer target")
    ap.add_argument("--active-paper", action="store_true", help="dry-only active WebSocket paper trader using current live data")
    ap.add_argument("--paper-symbol", default=SYMBOL, help="symbol for active paper mode")
    ap.add_argument("--paper-auto-symbol", action="store_true", help="scan liquid futures and pick the freshest active paper symbol")
    ap.add_argument("--paper-challenge-10", action="store_true", help="dry-only high-risk preset targeting +10 USDT")
    ap.add_argument("--paper-seconds", type=int, default=ACTIVE_DEFAULT_SECONDS, help="active paper run length")
    ap.add_argument("--paper-max-trades", type=int, default=0, help="active paper max closed trades; 0 means unlimited")
    ap.add_argument("--paper-force-after", type=float, default=ACTIVE_FORCE_AFTER_SEC, help="seconds before active paper takes the best current micro edge")
    ap.add_argument("--paper-target-usdt", type=Decimal, default=ACTIVE_SESSION_TARGET_USDT, help="active paper session profit target")
    ap.add_argument("--paper-loss-usdt", type=Decimal, default=ACTIVE_SESSION_LOSS_USDT, help="active paper session loss limit")
    ap.add_argument("--paper-leverage", type=int, default=ACTIVE_LEVERAGE, help="active paper simulated leverage")
    ap.add_argument("--paper-margin-fraction", type=Decimal, default=ACTIVE_MARGIN_FRACTION, help="active paper fraction of wallet used as margin")
    ap.add_argument("--paper-tp-pct", type=Decimal, default=ACTIVE_TAKE_PROFIT_PCT, help="active paper take-profit price fraction")
    ap.add_argument("--paper-sl-pct", type=Decimal, default=ACTIVE_STOP_LOSS_PCT, help="active paper stop-loss price fraction")
    ap.add_argument("--paper-trail-arm-pct", type=Decimal, default=ACTIVE_TRAIL_ARM_PCT, help="active paper trailing arm price fraction")
    ap.add_argument("--paper-trail-giveback-pct", type=Decimal, default=ACTIVE_TRAIL_GIVEBACK_PCT, help="active paper trailing giveback price fraction")
    ap.add_argument("--paper-max-hold-sec", type=float, default=ACTIVE_MAX_HOLD_SEC, help="active paper max hold seconds")
    ap.add_argument("--paper-no-flow-flip-exit", action="store_true", help="disable active paper flow-flip exits")
    ap.add_argument("--paper-confirm-move-pct", type=float, default=ACTIVE_CONFIRM_MOVE_PCT, help="price continuation required before active paper entry")
    ap.add_argument("--rest-poll", action="store_true", help="disable WebSockets and use REST polling fallback")
    ap.add_argument("--dry-wallet", type=Decimal, default=DEFAULT_DRY_WALLET, help="simulated wallet size in USDT")
    ap.add_argument("--max-cycles", type=int, default=0, help="stop after N poll cycles/WebSocket events; 0 means run until exit")
    ap.add_argument("--poll-seconds", type=int, default=POLL_SEC, help="live-data polling interval")
    ap.add_argument("--status-every-seconds", type=int, default=STATUS_EVERY_SEC, help="status log interval")
    args = ap.parse_args()

    if args.live and args.aggressive:
        print("ERROR: --aggressive changes the report target profile and is dry-run only.")
        return 2
    if args.live and args.active_paper:
        print("ERROR: --active-paper is dry-run only and never places live orders.")
        return 2
    if args.live and args.paper_challenge_10:
        print("ERROR: --paper-challenge-10 is dry-run only and never places live orders.")
        return 2

    if args.live:
        from trading_bot.live_executor import confirm_live_trading_intent, load_credentials_from_env

        if not args.confirm_live and not confirm_live_trading_intent():
            print("ERROR: live trading requires --confirm-live or BINANCE_LIVE_CONFIRM=yes-i-understand-risk in .env")
            return 2
        api_key, secret_key = load_credentials_from_env()
        if not api_key or not secret_key:
            print("ERROR: BINANCE_API_KEY and BINANCE_SECRET_KEY are missing from .env/environment.")
            return 2
        client = BinanceFuturesClient(api_key=api_key, secret_key=secret_key, timeout=3)
    else:
        client = BinanceFuturesClient(timeout=3)

    if args.active_paper:
        paper_symbol = args.paper_symbol.strip().upper()
        paper_seconds = args.paper_seconds
        paper_max_trades = args.paper_max_trades
        paper_force_after = args.paper_force_after
        paper_target = args.paper_target_usdt
        paper_loss = args.paper_loss_usdt
        paper_leverage = args.paper_leverage
        paper_margin_fraction = args.paper_margin_fraction
        paper_tp = args.paper_tp_pct
        paper_sl = args.paper_sl_pct
        paper_trail_arm = args.paper_trail_arm_pct
        paper_trail_giveback = args.paper_trail_giveback_pct
        paper_max_hold = args.paper_max_hold_sec
        paper_flow_flip_exit = not args.paper_no_flow_flip_exit
        paper_confirm_move = args.paper_confirm_move_pct

        if args.paper_challenge_10:
            paper_target = Decimal("10")
            paper_loss = Decimal("-4.5")
            paper_leverage = 50
            paper_margin_fraction = Decimal("0.90")
            paper_tp = Decimal("0.0125")
            paper_sl = Decimal("0.0032")
            paper_trail_arm = Decimal("0.009")
            paper_trail_giveback = Decimal("0.004")
            paper_max_hold = 180.0
            paper_seconds = max(paper_seconds, 600)
            paper_max_trades = paper_max_trades or 10
            paper_force_after = 9999.0
            paper_flow_flip_exit = False
            paper_confirm_move = max(paper_confirm_move, 0.08)
            args.paper_auto_symbol = True
            log("DRY CHALLENGE: high-risk +10 USDT paper preset enabled (50x simulated leverage).")

        if args.paper_auto_symbol:
            paper_symbol, scan_reason = select_active_paper_symbol(client)
            log(f"ACTIVE scanner selected {paper_symbol}: {scan_reason}")

        bot = ActivePaperBot(
            client,
            symbol=paper_symbol,
            dry_wallet=args.dry_wallet,
            max_seconds=paper_seconds,
            max_trades=paper_max_trades,
            status_every_sec=args.status_every_seconds,
            force_after_sec=paper_force_after,
            session_target=paper_target,
            session_loss=paper_loss,
            leverage=paper_leverage,
            margin_fraction=paper_margin_fraction,
            take_profit_pct=paper_tp,
            stop_loss_pct=paper_sl,
            trail_arm_pct=paper_trail_arm,
            trail_giveback_pct=paper_trail_giveback,
            max_hold_sec=paper_max_hold,
            use_flow_flip_exit=paper_flow_flip_exit,
            confirm_move_pct=paper_confirm_move,
        )
        return bot.run()

    bot = OrcaBot(
        client,
        dry_run=not args.live,
        adaptive=args.adaptive,
        aggressive=args.aggressive,
        dry_wallet=args.dry_wallet,
        max_cycles=args.max_cycles,
        poll_sec=args.poll_seconds,
        status_every_sec=args.status_every_seconds,
        use_websocket=not args.rest_poll,
    )
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
