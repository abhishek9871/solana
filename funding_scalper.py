"""
funding_scalper.py — designed by inversion of session failures.

Failure patterns inverted:
  - one symbol -> scan top 30 by funding extremity
  - one direction -> long crowded-shorts, short crowded-longs
  - one shot -> re-arm after each trade, multiple concurrent
  - $100 moonshot target -> $0.20-0.30 per win, accumulate $10 over 30+ trades
  - tight chase guard -> none, market entries with funding tailwind
  - silent crash -> outer try/except + heartbeat log + state file
  - untested code in prod -> testnet by default, --prod required for real

Goal: $24 -> $34 across many small +EV trades.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, asdict, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env


# ===== CONFIG =====
LEVERAGE = 10
MAX_POSITIONS = 4
MARGIN_PER_POSITION = Decimal("4.0")  # USDT per slot
TP_PCT = Decimal("0.006")  # +0.6% favorable -> ~+$0.24 gross @10x on $40 notional
SL_PCT = Decimal("0.004")  # -0.4% adverse  -> ~-$0.16 gross
TIME_STOP_SEC = 30 * 60   # 30 min max hold

# Funding bias (normalized to 8h)
LONG_FUNDING_MAX = -0.20   # long if funding <= -0.20%/8h
SHORT_FUNDING_MIN = 0.20   # short if funding >= +0.20%/8h
# Cap funding extremity. Symbols with |f8h| > 1.5% are usually in panic / broken-liquidity state
# (DAM-style free-fall). Order placement repeatedly fails (-2021, -4131). Skip them.
MAX_FUNDING_EXTREME = 1.5
# Cooldown after a failed open (TP/SL placement failure on broken symbol). Longer than normal cooldown
# because we don't want repeated retries on a symbol that's structurally illiquid.
FAILED_OPEN_COOLDOWN_SEC = 600  # 10 min

# Liquidity filters
MAX_SPREAD_PCT = Decimal("0.10")
MIN_VOL_RATIO = 1.30  # 1m vol vs 20-bar avg
MIN_24H_VOL_USD = 50_000_000
# Tick precision: skip symbols where 1 tick > MIN_TICK_BAND_PCT of price.
# Our SL is 0.4% / TP is 0.6%; if 1 tick is 0.5%, SL/TP ends up ON the next tick (Binance rejects -2021).
# Require TP target to be at least 4 ticks away from entry so jitter doesn't pre-trigger.
MIN_TICK_BAND_PCT = 0.0015  # 1 tick must be < 0.15% of price (TP 0.6% = 4+ ticks)

SCAN_INTERVAL_SEC = 30
MAX_SYMBOLS_TO_CHECK = 30
SYMBOL_COOLDOWN_SEC = 120  # don't re-enter same symbol within this window after close

STATE_FILE = "funding_scalper_state.json"
LOG_FILE = "funding_scalper.log"


@dataclass
class Position:
    symbol: str
    side: str  # BUY or SELL
    qty: float
    entry_price: float
    entry_time: float
    margin: float
    notional: float
    tp_price: float
    sl_price: float
    tp_order_id: int = 0  # server-side TAKE_PROFIT_MARKET algoId
    sl_order_id: int = 0  # server-side STOP_MARKET algoId


def log(msg: str) -> None:
    safe = msg.encode("ascii", errors="replace").decode("ascii")
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {safe}"
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
        log(f"state save error: {e}")


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def fmt_qty(value: Decimal, step: Decimal) -> str:
    q = quantize_down(value, step)
    return format(q.normalize(), "f") if q != q.to_integral() else format(q, "f")


def quantize_price(value: Decimal, tick: Decimal) -> Decimal:
    """Round to nearest valid tick (half-up). Used for TP/SL prices."""
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt_price(value: Decimal, tick: Decimal) -> str:
    q = quantize_price(value, tick)
    return format(q.normalize(), "f") if q != q.to_integral() else format(q, "f")


class Scalper:
    def __init__(self, client: BinanceFuturesClient, prod: bool):
        self.c = client
        self.prod = prod
        self.symbol_info_cache: dict[str, dict] = {}
        self.funding_interval_cache: dict[str, int] = {}
        self.positions: dict[str, Position] = {}
        self.trade_log: list[dict] = []
        self.starting_wallet = Decimal("0")
        self.session_start = time.time()
        self.scan_count = 0
        self.symbol_cooldown_until: dict[str, float] = {}

    def _persist(self) -> None:
        save_state({
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "trade_log": self.trade_log,
        })

    def _hydrate(self) -> None:
        s = load_state()
        for sym, pdata in s.get("positions", {}).items():
            try:
                self.positions[sym] = Position(**pdata)
            except Exception:
                pass
        self.trade_log = s.get("trade_log", [])

    def get_symbol_info(self, symbol: str) -> dict:
        if symbol not in self.symbol_info_cache:
            self.symbol_info_cache[symbol] = self.c.get_symbol_info(symbol)
        return self.symbol_info_cache[symbol]

    def get_filter_specs(self, symbol: str) -> tuple[Decimal, Decimal]:
        info = self.get_symbol_info(symbol)
        tick = step = Decimal("0")
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick = Decimal(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                step = Decimal(f["stepSize"])
        return tick, step

    def _load_funding_intervals(self) -> None:
        if self.funding_interval_cache:
            return
        # Primary: prod-only fundingInfo endpoint
        try:
            fi = self.c.public_get("/fapi/v1/fundingInfo", {})
            for entry in fi if isinstance(fi, list) else []:
                sym = entry.get("symbol")
                ih = entry.get("fundingIntervalHours")
                if sym and ih:
                    self.funding_interval_cache[sym] = int(ih)
            if self.funding_interval_cache:
                log(f"funding intervals loaded for {len(self.funding_interval_cache)} symbols (fundingInfo)")
                return
        except BinanceApiError:
            pass
        except Exception:
            pass
        # Fallback: exchangeInfo also exposes fundingIntervalHours on most clusters
        try:
            ei = self.c.get_exchange_info()
            for s in ei.get("symbols", []):
                sym = s.get("symbol")
                ih = s.get("fundingIntervalHours") or s.get("fundingInterval")
                if sym and ih:
                    self.funding_interval_cache[sym] = int(ih)
            if self.funding_interval_cache:
                log(f"funding intervals loaded for {len(self.funding_interval_cache)} symbols (exchangeInfo)")
                return
        except Exception:
            pass
        # Last resort: assume 8h for all (true for most symbols)
        log("funding intervals not available from API; defaulting all symbols to 8h")

    def normalize_funding_8h(self, raw_pct_per_interval: float, sym: str) -> float:
        interval = self.funding_interval_cache.get(sym, 8)
        return raw_pct_per_interval * (8.0 / interval)

    def get_balance(self) -> Decimal:
        bals = self.c.get_balance()
        usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
        if not usdt:
            return Decimal("0")
        return Decimal(usdt.get("availableBalance") or usdt.get("balance", "0"))

    def scan_universe(self) -> list[tuple[str, float, float]]:
        try:
            tickers = self.c.get_24hr_tickers()
            premium = self.c.public_get("/fapi/v1/premiumIndex", {})
        except BinanceApiError as e:
            log(f"scan error: {e}")
            return []
        premium_map = {p["symbol"]: p for p in premium} if isinstance(premium, list) else {}
        out: list[tuple[str, float, float]] = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT") or sym in self.positions:
                continue
            try:
                vol = float(t["quoteVolume"])
                if vol < MIN_24H_VOL_USD:
                    continue
                p = premium_map.get(sym)
                if not p:
                    continue
                raw_funding = float(p.get("lastFundingRate", "0")) * 100
                f8h = self.normalize_funding_8h(raw_funding, sym)
                if abs(f8h) < min(abs(LONG_FUNDING_MAX), abs(SHORT_FUNDING_MIN)):
                    continue
                if abs(f8h) > MAX_FUNDING_EXTREME:
                    continue  # panic/broken-liquidity zone — order placements will fail
                out.append((sym, f8h, vol))
            except Exception:
                continue
        out.sort(key=lambda x: abs(x[1]), reverse=True)
        return out[:MAX_SYMBOLS_TO_CHECK]

    def evaluate(self, sym: str, f8h: float) -> str | None:
        # Per-symbol cooldown after close to prevent whipsaw re-entries
        cd_until = self.symbol_cooldown_until.get(sym, 0)
        if time.time() < cd_until:
            return None
        if f8h <= LONG_FUNDING_MAX:
            bias = "BUY"
        elif f8h >= SHORT_FUNDING_MIN:
            bias = "SELL"
        else:
            return None
        try:
            k1 = self.c.get_klines(sym, "1m", limit=22)
        except BinanceApiError:
            return None
        if len(k1) < 22:
            return None
        closes = [float(k[4]) for k in k1[:-1]]
        volumes = [float(k[5]) for k in k1[:-1]]
        if len(closes) < 21:
            return None
        # Net direction over 3 bars (was strict monotonic; too restrictive in choppy regime)
        last3 = closes[-3:]
        if bias == "BUY" and last3[2] <= last3[0]:
            return None
        if bias == "SELL" and last3[2] >= last3[0]:
            return None
        avg_vol = sum(volumes[-21:-1]) / 20
        if avg_vol <= 0 or volumes[-1] / avg_vol < MIN_VOL_RATIO:
            return None
        try:
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = float(bt["bidPrice"])
            ask = float(bt["askPrice"])
        except BinanceApiError:
            return None
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        spread_pct = Decimal(str((ask - bid) / mid * 100))
        if spread_pct > MAX_SPREAD_PCT:
            return None
        # Tick-precision check: ensure TP target is at least 4 ticks from entry
        try:
            tick, _step = self.get_filter_specs(sym)
            if tick > 0 and float(tick) / mid > MIN_TICK_BAND_PCT:
                return None
        except Exception:
            return None
        return bias

    def open_position(self, sym: str, side: str) -> None:
        try:
            tick, step = self.get_filter_specs(sym)
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = Decimal(bt["bidPrice"])
            ask = Decimal(bt["askPrice"])
        except BinanceApiError as e:
            log(f"{sym} pre-entry fetch failed: {e}")
            return

        ref_px = ask if side == "BUY" else bid
        if ref_px <= 0:
            log(f"{sym} bad ref price")
            return

        try:
            self.c.set_leverage(sym, LEVERAGE)
        except BinanceApiError as e:
            log(f"{sym} leverage warning: {e}")

        notional = MARGIN_PER_POSITION * Decimal(LEVERAGE)
        qty_raw = notional / ref_px
        qty = quantize_down(qty_raw, step)
        if qty * ref_px < Decimal("5"):
            log(f"{sym} notional ${qty*ref_px:.2f} below $5 min")
            return

        qty_str = fmt_qty(qty, step)
        close_side = "SELL" if side == "BUY" else "BUY"
        log(f"OPEN {sym} {side} qty={qty_str} ref={ref_px}")

        # Step 1: market entry
        try:
            order = self.c.place_market_order(sym, side, quantity=qty_str)
        except BinanceApiError as e:
            log(f"{sym} market entry failed: {e}")
            return

        avg = order.get("avgPrice") or order.get("price")
        try:
            fill_px = Decimal(str(avg)) if avg and Decimal(str(avg)) > 0 else ref_px
        except Exception:
            fill_px = ref_px

        # Step 2: compute bracket levels, quantize to tick
        if side == "BUY":
            tp_px = quantize_price(fill_px * (Decimal("1") + TP_PCT), tick)
            sl_px = quantize_price(fill_px * (Decimal("1") - SL_PCT), tick)
        else:
            tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), tick)
            sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), tick)

        # Step 3: place server-side TP (TAKE_PROFIT_MARKET reduce_only)
        tp_order_id = 0
        try:
            tp_resp = self.c.place_take_profit_order(
                sym, close_side,
                stop_price=fmt_price(tp_px, tick),
                quantity=qty_str,
                close_position=False,
                reduce_only=True,
            )
            tp_order_id = int(tp_resp.get("algoId") or tp_resp.get("orderId") or 0)
        except BinanceApiError as e:
            log(f"  CRITICAL: TP placement failed for {sym}: {e} -- emergency closing position")
            self._emergency_close(sym, close_side, qty_str)
            self.symbol_cooldown_until[sym] = time.time() + FAILED_OPEN_COOLDOWN_SEC
            return

        # Step 4: place server-side SL (STOP_MARKET reduce_only)
        sl_order_id = 0
        try:
            sl_resp = self.c.place_stop_market_order(
                sym, close_side,
                stop_price=fmt_price(sl_px, tick),
                quantity=qty_str,
                close_position=False,
                reduce_only=True,
            )
            sl_order_id = int(sl_resp.get("algoId") or sl_resp.get("orderId") or 0)
        except BinanceApiError as e:
            log(f"  CRITICAL: SL placement failed for {sym}: {e} -- cancelling TP and emergency closing")
            try:
                if tp_order_id:
                    self.c.cancel_algo_order(sym, algo_id=tp_order_id)
            except Exception:
                pass
            self._emergency_close(sym, close_side, qty_str)
            self.symbol_cooldown_until[sym] = time.time() + FAILED_OPEN_COOLDOWN_SEC
            return

        # Step 5: record position with bracket order IDs
        pos = Position(
            symbol=sym, side=side, qty=float(qty), entry_price=float(fill_px),
            entry_time=time.time(),
            margin=float(MARGIN_PER_POSITION), notional=float(qty * fill_px),
            tp_price=float(tp_px), sl_price=float(sl_px),
            tp_order_id=tp_order_id, sl_order_id=sl_order_id,
        )
        self.positions[sym] = pos
        self._persist()
        log(f"  FILLED {sym} {side} qty={qty_str} px={float(fill_px):.6f} "
            f"TP={float(tp_px):.6f} (algo={tp_order_id}) "
            f"SL={float(sl_px):.6f} (algo={sl_order_id})")

    def close_position(self, sym: str, reason: str) -> None:
        pos = self.positions.get(sym)
        if not pos:
            return
        # Cancel any live bracket orders before market-closing
        self._cancel_brackets(sym, pos)
        close_side = "SELL" if pos.side == "BUY" else "BUY"
        try:
            _tick, step = self.get_filter_specs(sym)
            qty_str = fmt_qty(Decimal(str(pos.qty)), step)
        except Exception:
            qty_str = str(pos.qty)
        try:
            self.c.place_market_order(sym, close_side, quantity=qty_str, reduce_only=True)
        except BinanceApiError as e:
            log(f"{sym} close order failed: {e} -- will retry next cycle")
            return  # keep in state, retry next manage_positions

        # Post-close verification: query exchange position. If non-zero, retry next cycle.
        try:
            time.sleep(0.5)
            poss = self.c.get_positions(sym)
            for p in poss:
                amt = abs(float(p.get("positionAmt", 0)))
                if amt > 0:
                    log(f"{sym} close UNVERIFIED: exchange still has {amt} -- retrying with full qty next cycle")
                    # Update local qty to match exchange so next close sends correct size
                    pos.qty = abs(float(p.get("positionAmt", 0)))
                    self._persist()
                    return
        except BinanceApiError as e:
            log(f"{sym} post-close verify error (assuming closed): {e}")
        try:
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            exit_px = (float(bt["bidPrice"]) + float(bt["askPrice"])) / 2
        except Exception:
            exit_px = pos.entry_price

        if pos.side == "BUY":
            pnl_gross = (exit_px - pos.entry_price) * pos.qty
        else:
            pnl_gross = (pos.entry_price - exit_px) * pos.qty
        # Fee approx: 0.045% taker × 2 sides = 0.09% on notional
        fees_est = pos.notional * 0.0009
        pnl_net = pnl_gross - fees_est
        self.trade_log.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym, "side": pos.side,
            "entry_px": pos.entry_price, "exit_px": exit_px, "qty": pos.qty,
            "pnl_gross": pnl_gross, "fees_est": fees_est, "pnl_net": pnl_net,
            "reason": reason, "duration_sec": int(time.time() - pos.entry_time),
        })
        log(f"CLOSE {sym} {reason} entry={pos.entry_price:.6f} exit={exit_px:.6f} "
            f"pnl_net=${pnl_net:+.4f} dur={int(time.time()-pos.entry_time)}s")
        self.positions.pop(sym, None)
        self.symbol_cooldown_until[sym] = time.time() + SYMBOL_COOLDOWN_SEC
        self._persist()

    def manage_positions(self) -> None:
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            try:
                poss = self.c.get_positions(sym)
            except BinanceApiError as e:
                log(f"{sym} positions query error: {e}")
                continue
            amt = 0.0
            for p in poss:
                amt = abs(float(p.get("positionAmt", 0)))
                break
            if amt == 0:
                # Closed by server-side bracket (TP or SL)
                self._finalize_bracket_close(sym, pos)
                continue
            # Time-stop: cancel brackets, market-close
            if time.time() - pos.entry_time > TIME_STOP_SEC:
                log(f"{sym} TIME_STOP at {int(time.time() - pos.entry_time)}s")
                self._cancel_brackets(sym, pos)
                self.close_position(sym, "TIME_STOP")

    def _emergency_close(self, sym: str, close_side: str, qty_str: str) -> None:
        """Robust emergency close: retry up to 3 times, then verify position is flat.
        If still open after retries, force-close at any size via reduceOnly with current position qty."""
        for attempt in range(3):
            try:
                self.c.place_market_order(sym, close_side, quantity=qty_str, reduce_only=True)
                break
            except BinanceApiError as e:
                log(f"  emergency close attempt {attempt+1}/3 failed for {sym}: {e}")
                time.sleep(0.5)
        # Verify
        try:
            time.sleep(0.5)
            poss = self.c.get_positions(sym)
            for p in poss:
                amt = float(p.get("positionAmt", 0))
                if amt != 0:
                    log(f"  fallback: position still {amt}; closing with current exchange qty")
                    side2 = "SELL" if amt > 0 else "BUY"
                    try:
                        _t, step = self.get_filter_specs(sym)
                        qty2 = fmt_qty(quantize_down(Decimal(str(abs(amt))), step), step)
                    except Exception:
                        qty2 = str(abs(amt))
                    try:
                        self.c.place_market_order(sym, side2, quantity=qty2, reduce_only=True)
                        log(f"  fallback close OK")
                    except BinanceApiError as e:
                        log(f"  FALLBACK CLOSE FAILED for {sym}: {e} -- MANUAL INTERVENTION REQUIRED")
        except BinanceApiError:
            pass

    def _cancel_brackets(self, sym: str, pos: Position) -> None:
        """Cancel both bracket algo orders. If one already filled, that cancel will 404 — ignore."""
        for order_id in (pos.tp_order_id, pos.sl_order_id):
            if not order_id:
                continue
            try:
                self.c.cancel_algo_order(sym, algo_id=order_id)
            except BinanceApiError:
                pass  # likely already filled or already cancelled

    def _finalize_bracket_close(self, sym: str, pos: Position) -> None:
        """Server-side TP or SL has closed the position. Determine which, cancel leftover, record trade."""
        # Cancel any leftover (the one that didn't trigger)
        self._cancel_brackets(sym, pos)
        # Decide TP vs SL by current price
        try:
            bt = self.c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            mark = (float(bt["bidPrice"]) + float(bt["askPrice"])) / 2
        except Exception:
            mark = pos.entry_price
        if pos.side == "BUY":
            reason = "TP" if mark >= (pos.tp_price + pos.sl_price) / 2 else "SL"
        else:
            reason = "TP" if mark <= (pos.tp_price + pos.sl_price) / 2 else "SL"
        # PnL is determined by the trigger price (server-side fill at exact level, minor exchange slippage)
        trigger_px = pos.tp_price if reason == "TP" else pos.sl_price
        if pos.side == "BUY":
            pnl_gross = (trigger_px - pos.entry_price) * pos.qty
        else:
            pnl_gross = (pos.entry_price - trigger_px) * pos.qty
        fees_est = pos.notional * 0.0009  # 2x taker fees on notional
        pnl_net = pnl_gross - fees_est
        self.trade_log.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym, "side": pos.side,
            "entry_px": pos.entry_price, "exit_px": trigger_px, "qty": pos.qty,
            "pnl_gross": pnl_gross, "fees_est": fees_est, "pnl_net": pnl_net,
            "reason": reason, "duration_sec": int(time.time() - pos.entry_time),
        })
        log(f"CLOSE {sym} {reason} entry={pos.entry_price:.6f} trigger={trigger_px:.6f} "
            f"pnl_net=${pnl_net:+.4f} dur={int(time.time()-pos.entry_time)}s")
        self.positions.pop(sym, None)
        self.symbol_cooldown_until[sym] = time.time() + SYMBOL_COOLDOWN_SEC
        self._persist()

    def report(self) -> None:
        n = len(self.trade_log)
        if n == 0:
            return
        wins = [t for t in self.trade_log if t["pnl_net"] > 0]
        losses = [t for t in self.trade_log if t["pnl_net"] <= 0]
        total = sum(t["pnl_net"] for t in self.trade_log)
        wr = len(wins) / n * 100
        avg_w = sum(t["pnl_net"] for t in wins) / len(wins) if wins else 0
        avg_l = sum(t["pnl_net"] for t in losses) / len(losses) if losses else 0
        log(f"STATS: n={n} wr={wr:.1f}% avg_win=${avg_w:+.4f} avg_loss=${avg_l:+.4f} "
            f"total_pnl=${total:+.4f}")

    def heartbeat(self) -> None:
        bal_s = "?"
        try:
            bal = self.get_balance()
            bal_s = f"${float(bal):.4f}"
        except Exception:
            pass
        log(f"HB: scans={self.scan_count} pos={len(self.positions)} bal={bal_s} trades={len(self.trade_log)}")

    def _startup_orphan_cleanup(self) -> None:
        """Close any pre-existing positions and cancel ALL stale orders (regular + algo).
        Critical safety: catches orphans from prior crashes."""
        # Collect every symbol that has either an open order or open position
        symbols_to_clean: set[str] = set()
        try:
            opens = self.c.get_open_orders()
            for o in opens:
                symbols_to_clean.add(o["symbol"])
        except BinanceApiError as e:
            log(f"startup: open-orders fetch failed: {e}")
        try:
            positions = self.c.get_positions()
        except BinanceApiError as e:
            log(f"startup: positions fetch failed: {e}")
            positions = []
        open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
        for p in open_pos:
            symbols_to_clean.add(p["symbol"])

        # Cancel regular + algo orders on each affected symbol
        for s in sorted(symbols_to_clean):
            try:
                self.c.cancel_all_orders(s)
            except BinanceApiError:
                pass
            try:
                self.c.cancel_all_algo_orders(s)
            except BinanceApiError:
                pass
            if s in symbols_to_clean and s not in {p["symbol"] for p in open_pos}:
                log(f"startup: cancelled stale orders on {s}")

        if not open_pos:
            log("startup: no orphan positions, account clean")
            self.positions.clear()
            self.symbol_cooldown_until.clear()
            self._persist()
            return

        log(f"startup: found {len(open_pos)} orphan positions -- force-closing")
        for p in open_pos:
            sym = p["symbol"]
            amt = float(p["positionAmt"])
            side = "SELL" if amt > 0 else "BUY"
            try:
                _tick, step = self.get_filter_specs(sym)
                qty_q = quantize_down(Decimal(str(abs(amt))), step)
                qty_s = fmt_qty(qty_q, step)
            except Exception:
                qty_s = str(abs(amt))
            try:
                self.c.place_market_order(sym, side, quantity=qty_s, reduce_only=True)
                log(f"startup: closed orphan {sym} {side} qty={qty_s}")
            except BinanceApiError as e:
                log(f"startup: ORPHAN CLOSE FAILED {sym}: {e}")
        # Clear local state since exchange is now flat
        self.positions.clear()
        self.symbol_cooldown_until.clear()
        self._persist()

    def run(self, max_runtime_sec: int = 0) -> int:
        log(f"=== funding_scalper {'PROD' if self.prod else 'TESTNET'} starting ===")
        self._hydrate()
        self._load_funding_intervals()
        try:
            self.starting_wallet = self.get_balance()
            log(f"starting wallet: ${float(self.starting_wallet):.4f}")
        except Exception as e:
            log(f"balance fetch error: {e}")
        # Critical: clean up any pre-existing positions (orphans from prior crashes)
        self._startup_orphan_cleanup()
        # Refresh starting wallet AFTER orphan cleanup (closes might have changed it)
        try:
            self.starting_wallet = self.get_balance()
        except Exception:
            pass

        last_heartbeat = 0.0
        last_scan = 0.0

        while True:
            try:
                self.manage_positions()

                if time.time() - last_scan >= SCAN_INTERVAL_SEC:
                    last_scan = time.time()
                    self.scan_count += 1
                    if len(self.positions) < MAX_POSITIONS:
                        cands = self.scan_universe()
                        log(f"scan #{self.scan_count}: {len(cands)} extreme-funding candidates")
                        for sym, f8h, _vol in cands:
                            if len(self.positions) >= MAX_POSITIONS:
                                break
                            decision = self.evaluate(sym, f8h)
                            if decision:
                                log(f"  signal {sym} f8h={f8h:+.3f}% -> {decision}")
                                self.open_position(sym, decision)

                if time.time() - last_heartbeat >= 60:
                    last_heartbeat = time.time()
                    self.heartbeat()
                    self.report()

                if max_runtime_sec > 0 and time.time() - self.session_start >= max_runtime_sec:
                    log("=== runtime limit reached, closing all & exiting ===")
                    for sym in list(self.positions.keys()):
                        self.close_position(sym, "RUNTIME_END")
                    break

                time.sleep(5)

            except KeyboardInterrupt:
                log("KeyboardInterrupt -- closing all positions")
                for sym in list(self.positions.keys()):
                    self.close_position(sym, "USER_INTERRUPT")
                break
            except Exception as e:
                log(f"unhandled exception: {e}")
                log(traceback.format_exc())
                time.sleep(10)

        self.report()
        try:
            ending = self.get_balance()
            log(f"=== ending wallet ${float(ending):.4f} (delta ${float(ending-self.starting_wallet):+.4f}) ===")
        except Exception:
            pass
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true",
                    help="Use real Binance USD-M (real money). Default: testnet.")
    ap.add_argument("--minutes", type=int, default=0,
                    help="Max runtime in minutes (0 = forever)")
    args = ap.parse_args()

    load_credentials_from_env()

    if args.prod:
        api = os.environ.get("BINANCE_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
        base = "https://fapi.binance.com"
        mode = "PROD"
    else:
        api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
        base = "https://testnet.binancefuture.com"
        mode = "TESTNET"

    if not api or not secret:
        print(f"ERROR: API keys missing for {mode} mode in .env")
        return 2

    client = BinanceFuturesClient(api_key=api, secret_key=secret, base_url=base)
    scalper = Scalper(client, prod=args.prod)
    runtime = args.minutes * 60 if args.minutes > 0 else 0
    return scalper.run(max_runtime_sec=runtime)


if __name__ == "__main__":
    sys.exit(main())
