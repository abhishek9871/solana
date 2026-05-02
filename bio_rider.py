"""
BIOUSDT rider — opens ONE position, rides it with aggressive trailing.
NO flipping. Either we win on TP/trailed SL, or take small managed loss.

Setup: 5 UP candles in a row, +0.555% latest, negative funding (squeeze fuel).
"""
import os
import time
import sys
import json
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "BIOUSDT"
SIDE = "BUY"
MARGIN_USD = Decimal("5.5")
LEVERAGE = 10

TP_PCT = Decimal("0.025")    # 2.5% TP target
SL_PCT = Decimal("0.008")    # 0.8% initial SL

# Trail ladder: at fav_pct, lock SL to lock_pct (both as percentages from entry)
TRAIL_LADDER = [
    (Decimal("0.003"), Decimal("0.0005")),  # +0.3% fav -> SL @ +0.05% (covers fees, locked breakeven+)
    (Decimal("0.008"), Decimal("0.005")),   # +0.8% fav -> SL @ +0.5%
    (Decimal("0.015"), Decimal("0.012")),   # +1.5% fav -> SL @ +1.2%
    (Decimal("0.022"), Decimal("0.018")),   # +2.2% fav -> SL @ +1.8%
]

POLL_SEC = 3


def quantize_price(v, t):
    return (v / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t


def quantize_down(v, s):
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def log(msg):
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# Setup leverage
try:
    c.set_leverage(SYMBOL, LEVERAGE)
    log(f"Leverage set to {LEVERAGE}x")
except BinanceApiError as e:
    log(f"set_leverage warn: {e}")

info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
min_notional = Decimal("5")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])
    elif f["filterType"] == "MIN_NOTIONAL":
        min_notional = Decimal(f["notional"])

# Get fresh price
bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
mark = (bid + ask) / 2

# Compute position size
notional = MARGIN_USD * LEVERAGE
qty = notional / mark
qty_q = quantize_down(qty, step)
qty_str = fmt(qty_q)
actual_notional = qty_q * mark

log(f"=== {SYMBOL} {SIDE} RIDER ===")
log(f"  Mark: {mark}, Margin: ${MARGIN_USD}, Lev: {LEVERAGE}x")
log(f"  Notional: ${actual_notional:.2f}, Qty: {qty_str}")
log(f"  Initial SL: {SL_PCT*100}%, TP: {TP_PCT*100}%")

if actual_notional < min_notional:
    log(f"FAIL: notional ${actual_notional} < min ${min_notional}")
    sys.exit(1)

# Open position
try:
    r = c.place_market_order(SYMBOL, SIDE, quantity=qty_str, reduce_only=False)
    log(f"  ENTRY filled: {r}")
except BinanceApiError as e:
    log(f"ENTRY FAIL: {e}")
    sys.exit(1)

time.sleep(1.5)

# Get actual entry price
positions = c.get_positions(SYMBOL)
entry = Decimal("0")
for p in positions:
    if float(p.get("positionAmt", 0)) != 0:
        entry = Decimal(p["entryPrice"])
        break
if entry == 0:
    log("FAIL: no position after entry")
    sys.exit(1)

log(f"  Actual entry: ${entry}")

# Place TP + SL brackets
close_side = "SELL" if SIDE == "BUY" else "BUY"

if SIDE == "BUY":
    tp_px = quantize_price(entry * (1 + TP_PCT), tick)
    sl_px = quantize_price(entry * (1 - SL_PCT), tick)
else:
    tp_px = quantize_price(entry * (1 - TP_PCT), tick)
    sl_px = quantize_price(entry * (1 + SL_PCT), tick)

try:
    r_tp = c.place_take_profit_order(SYMBOL, close_side, stop_price=fmt(tp_px),
                                       quantity=qty_str, close_position=False, reduce_only=True)
    log(f"  TP placed @ ${tp_px}, algoId={r_tp.get('algoId')}")
except BinanceApiError as e:
    log(f"  TP fail: {e}")

try:
    r_sl = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(sl_px),
                                       quantity=qty_str, close_position=False, reduce_only=True)
    log(f"  SL placed @ ${sl_px}, algoId={r_sl.get('algoId')}")
except BinanceApiError as e:
    log(f"  SL fail: {e}")

log(f"  Brackets active. Now trailing...")

# Trail loop
ladder_step = 0  # which ladder rung we're on

while True:
    try:
        positions = c.get_positions(SYMBOL)
        amt = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            break
        if amt == 0:
            log("Position closed (TP or SL fired). Done.")
            break

        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
        bid = float(bt["bidPrice"])
        ask = float(bt["askPrice"])
        mark = (bid + ask) / 2

        if SIDE == "BUY":
            fav_pct = (mark - float(entry)) / float(entry)
        else:
            fav_pct = (float(entry) - mark) / float(entry)

        # Check if we should advance ladder
        next_target = None
        if ladder_step < len(TRAIL_LADDER):
            trigger, lock = TRAIL_LADDER[ladder_step]
            if Decimal(str(fav_pct)) >= trigger:
                # Move SL to lock level
                if SIDE == "BUY":
                    new_sl = quantize_price(entry * (1 + lock), tick)
                else:
                    new_sl = quantize_price(entry * (1 - lock), tick)

                # Find existing SL algo
                algos = c.get_open_algo_orders(SYMBOL)
                algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
                sl_id = None
                for a in algo_list:
                    typ = a.get("type") or a.get("origType") or ""
                    a_side = a.get("side")
                    if a_side == close_side and ("STOP" in str(typ).upper() and "PROFIT" not in str(typ).upper()):
                        sl_id = int(a.get("algoId") or a.get("orderId") or 0)
                        break

                if sl_id:
                    try:
                        c.cancel_algo_order(SYMBOL, algo_id=sl_id)
                        log(f"  Cancelled old SL {sl_id}")
                    except BinanceApiError as e:
                        log(f"  cancel warn: {e}")

                try:
                    qty_q2 = quantize_down(Decimal(str(abs(amt))), step)
                    r2 = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(new_sl),
                                                     quantity=fmt(qty_q2), close_position=False, reduce_only=True)
                    log(f"  *** RUNG {ladder_step+1}: SL moved to ${new_sl} (lock {lock*100}%) ***")
                    ladder_step += 1
                except BinanceApiError as e:
                    log(f"  trail SL fail: {e}")
            else:
                next_target = trigger

        # Status
        target_str = f" target=+{float(next_target)*100:.2f}%" if next_target else " (max trail)"
        log(f"  mark={mark:.6f} fav={fav_pct*100:+.3f}% rung={ladder_step}/{len(TRAIL_LADDER)}{target_str}")

        time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        log("Interrupted (server-side brackets remain).")
        sys.exit(0)
    except Exception as e:
        log(f"loop error (recovered): {e}")
        time.sleep(POLL_SEC)
