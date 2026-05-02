"""
Aggressive trailer for ORCAUSDT live trade.
The MOMENT price is +0.5% favorable, moves SL to entry (breakeven).
After that, position can't lose — only TP or breakeven.
"""
import os
import time
import sys
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "1000LUNCUSDT"
ENTRY = Decimal("0.0704145")
SIDE = "BUY"
TRAIL_TRIGGER_PCT_ENV = Decimal("0.003")  # tighter trail trigger: 0.3% to lock fast
TRAIL_TRIGGER_PCT = Decimal("0.005")  # +0.5% favorable
POLL_SEC = 5  # very fast polling for "money in seconds"


def quantize_price(v, t):
    return (v / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t


def quantize_down(v, s):
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])

trailed = False
log(f"=== ORCAUSDT trailer started, entry=${ENTRY}, trigger={TRAIL_TRIGGER_PCT*100}% favorable ===")

while True:
    try:
        positions = c.get_positions(SYMBOL)
        amt = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            break
        if amt == 0:
            log("Position closed (TP or SL fired). Exiting.")
            break
        qty = abs(amt)
        qty_q = quantize_down(Decimal(str(qty)), step)
        qty_str = fmt(qty_q)

        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
        bid = float(bt["bidPrice"])
        ask = float(bt["askPrice"])
        mark = (bid + ask) / 2

        # Favorable percentage: for LONG, price up = favorable
        if SIDE == "BUY":
            fav_pct = (mark - float(ENTRY)) / float(ENTRY) * 100
        else:
            fav_pct = (float(ENTRY) - mark) / float(ENTRY) * 100

        log(f"mark={mark:.6f} fav={fav_pct:+.3f}% trailed={trailed}")

        if not trailed and fav_pct >= float(TRAIL_TRIGGER_PCT) * 100:
            # Move SL to entry
            log(f"  Triggered! Moving SL to breakeven ${ENTRY}")
            # Find existing SL algo
            algos = c.get_open_algo_orders(SYMBOL)
            algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
            sl_id = None
            for a in algo_list:
                trig = float(a.get("triggerPrice", 0) or a.get("stopPrice", 0))
                # SL for LONG = SELL trigger BELOW entry
                if a.get("side") == "SELL" and trig < float(ENTRY):
                    sl_id = int(a.get("algoId") or a.get("orderId") or 0)
                    break
            if sl_id:
                try:
                    c.cancel_algo_order(SYMBOL, algo_id=sl_id)
                    log(f"  Cancelled old SL algoId={sl_id}")
                except BinanceApiError as e:
                    log(f"  cancel warn: {e}")
            # Place new SL at entry
            new_sl = quantize_price(ENTRY, tick)
            close_side = "SELL" if SIDE == "BUY" else "BUY"
            try:
                r = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(new_sl),
                                                quantity=qty_str, close_position=False, reduce_only=True)
                log(f"  NEW SL at breakeven ${new_sl} placed, algoId={r.get('algoId')}")
                trailed = True
                log(f"  *** BREAKEVEN LOCKED — POSITION CAN ONLY WIN OR EXIT FLAT ***")
            except BinanceApiError as e:
                log(f"  NEW SL FAIL: {e}")

        time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        log("Interrupted — exiting (server-side brackets remain on Binance)")
        sys.exit(0)
    except Exception as e:
        log(f"loop error (recovered): {e}")
        time.sleep(POLL_SEC)
