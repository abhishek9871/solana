"""
ZEREBRO rider v2 — dynamic high-watermark trail (no fixed-rung noise).
Once position goes +0.6% favorable, SL trails 0.5% BEHIND the highest mark seen.
Wider buffer = won't get whipsaw-stopped on noise.

Same coin we squeezed twice yesterday. Currently 7/9 candles UP, +5.42% over 9 min.
"""
import os
import time
import sys
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "API3USDT"
SIDE = "BUY"
MARGIN_USD = Decimal("5.71")       # $114 notional / 20x
LEVERAGE = 20

TP_PCT = Decimal("0.020")          # 2.0% TP per harmony
SL_PCT = Decimal("0.005")          # 0.5% SL (1.5x ATR, floor)
TRAIL_ARM_PCT = Decimal("0.004")
TRAIL_BUFFER = Decimal("0.003")
TRAIL_STEP_PCT = Decimal("0.002")

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

bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
mark0 = (bid + ask) / 2

notional = MARGIN_USD * LEVERAGE
qty = notional / mark0
qty_q = quantize_down(qty, step)
qty_str = fmt(qty_q)
actual_notional = qty_q * mark0

log(f"=== {SYMBOL} {SIDE} RIDER v2 ===")
log(f"  Mark0: {mark0}, Margin: ${MARGIN_USD}, Lev: {LEVERAGE}x")
log(f"  Notional: ${actual_notional:.2f}, Qty: {qty_str}")
log(f"  TP: {TP_PCT*100}%, SL: {SL_PCT*100}%, Arm trail: +{TRAIL_ARM_PCT*100}%, Trail buffer: {TRAIL_BUFFER*100}%")

if actual_notional < min_notional:
    log(f"FAIL: notional ${actual_notional} < min ${min_notional}")
    sys.exit(1)

try:
    r = c.place_market_order(SYMBOL, SIDE, quantity=qty_str, reduce_only=False)
    log(f"  ENTRY filled: avgPrice={r.get('avgPrice')}")
except BinanceApiError as e:
    log(f"ENTRY FAIL: {e}")
    sys.exit(1)

time.sleep(1.5)

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

log("  Brackets active. Watching for armed trail...")

# Trail state — current_lock_pct is "% favorable locked in by SL"
# Starts negative (SL allows up to SL_PCT loss). Grows positive as SL trails into profit territory.
highest_fav_pct = Decimal("0")
trail_armed = False
current_sl_px = sl_px
current_lock_pct = -SL_PCT  # negative = SL still in losing territory

while True:
    try:
        positions = c.get_positions(SYMBOL)
        amt = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            break
        if amt == 0:
            log("Position closed. Done.")
            break

        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
        bid = float(bt["bidPrice"])
        ask = float(bt["askPrice"])
        mark = (bid + ask) / 2

        if SIDE == "BUY":
            fav_pct = Decimal(str((mark - float(entry)) / float(entry)))
        else:
            fav_pct = Decimal(str((float(entry) - mark) / float(entry)))

        if fav_pct > highest_fav_pct:
            highest_fav_pct = fav_pct

        if not trail_armed and highest_fav_pct >= TRAIL_ARM_PCT:
            trail_armed = True
            log(f"  *** TRAIL ARMED at +{float(highest_fav_pct)*100:.3f}% peak ***")

        if trail_armed:
            target_lock_pct = highest_fav_pct - TRAIL_BUFFER
            # Cap target_lock at current_fav minus a safety margin. Binance uses MARK_PRICE
            # (smoother than last) for trigger — during fast moves, mark lags, so we need
            # buffer 0.5%+ to avoid -2021 immediate-trigger error.
            safety = Decimal("0.005")
            cap = fav_pct - safety
            if cap < target_lock_pct:
                target_lock_pct = cap
            # Only advance — never loosen
            if target_lock_pct - current_lock_pct >= TRAIL_STEP_PCT:
                if SIDE == "BUY":
                    new_sl_px = quantize_price(entry * (Decimal("1") + target_lock_pct), tick)
                else:
                    new_sl_px = quantize_price(entry * (Decimal("1") - target_lock_pct), tick)

                algos = c.get_open_algo_orders(SYMBOL)
                algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
                sl_id = None
                for a in algo_list:
                    typ = (a.get("type") or a.get("origType") or "").upper()
                    a_side = a.get("side")
                    if a_side == close_side and "STOP" in typ and "PROFIT" not in typ:
                        sl_id = int(a.get("algoId") or a.get("orderId") or 0)
                        break
                if sl_id:
                    try:
                        c.cancel_algo_order(SYMBOL, algo_id=sl_id)
                    except BinanceApiError as e:
                        log(f"  cancel warn: {e}")
                try:
                    qty_q2 = quantize_down(Decimal(str(abs(amt))), step)
                    r2 = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(new_sl_px),
                                                     quantity=fmt(qty_q2), close_position=False, reduce_only=True)
                    direction_word = "UP" if SIDE == "BUY" else "DOWN"
                    log(f"  TRAIL {direction_word}: SL ${current_sl_px} -> ${new_sl_px} (lock {float(target_lock_pct)*100:+.2f}%, peak {float(highest_fav_pct)*100:+.2f}%)")
                    current_sl_px = new_sl_px
                    current_lock_pct = target_lock_pct
                except BinanceApiError as e:
                    log(f"  trail SL fail: {e}")

        status = "ARMED" if trail_armed else f"arm@+{float(TRAIL_ARM_PCT)*100:.2f}%"
        log(f"  mark={mark:.6f} fav={float(fav_pct)*100:+.3f}% peak={float(highest_fav_pct)*100:+.3f}% lock={float(current_lock_pct)*100:+.2f}% [{status}]")

        time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        log("Interrupted (server-side brackets remain).")
        sys.exit(0)
    except Exception as e:
        log(f"loop error (recovered): {e}")
        time.sleep(POLL_SEC)
