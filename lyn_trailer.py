"""
LYNUSDT trail-stop watcher for the LIVE prod trade.
Polls every 30s. As price drops favorable for our SHORT, ratchets SL down
to lock progressive gains. Original TP at $0.06454 stays untouched.

Trail tiers (relative to entry $0.067940):
  -0.5% favorable → SL to entry (breakeven)
  -2.0% favorable → SL to entry-1% (locks ~+$2)
  -3.5% favorable → SL to entry-2.5% (locks ~+$5)
"""
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "LYNUSDT"
ENTRY = Decimal("0.067940")  # our actual fill
POLL_SEC = 30


def quantize_price(value, tick):
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# Get tick/step
info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])

# Current SL algo ID (from earlier fire log: 3000001387932271)
# We'll find it dynamically by querying open algo orders for the BUY+stop in our zone
current_sl_id = None
current_sl_price = None
trail_tier = 0  # 0 = original, 1 = BE, 2 = lock $2, 3 = lock $5

log(f"=== LYNUSDT trailer started, entry=${ENTRY} ===")

while True:
    try:
        # Check position still open
        positions = c.get_positions(SYMBOL)
        amt = 0.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            break
        if amt == 0:
            log("Position closed (TP or SL fired). Exiting.")
            break
        qty = abs(amt)
        qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        qty_str = fmt(qty_q)

        # Find current SL algo (BUY trigger above entry)
        try:
            algos = c.get_open_algo_orders(SYMBOL)
            algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
            for a in algo_list:
                trig = float(a.get("triggerPrice", 0) or a.get("stopPrice", 0))
                # SL is the BUY trigger ABOVE entry
                if a.get("side") == "BUY" and trig > float(ENTRY):
                    current_sl_id = int(a.get("algoId") or a.get("orderId") or 0)
                    current_sl_price = trig
                    break
        except BinanceApiError:
            pass

        # Get current mark
        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
        bid = float(bt["bidPrice"])
        ask = float(bt["askPrice"])
        mark = (bid + ask) / 2

        # Favorable percentage (positive = price has dropped, good for our SHORT)
        fav_pct = (float(ENTRY) - mark) / float(ENTRY) * 100

        log(f"mark={mark:.6f} fav={fav_pct:+.2f}% sl_id={current_sl_id} sl_px={current_sl_price} tier={trail_tier}")

        # Determine target tier
        new_tier = trail_tier
        target_sl_pct = None  # adverse % (where SL sits relative to entry)
        if fav_pct >= 3.5 and trail_tier < 3:
            new_tier = 3
            target_sl_pct = -2.5  # SL at 2.5% favorable from entry = locks $5
        elif fav_pct >= 2.0 and trail_tier < 2:
            new_tier = 2
            target_sl_pct = -1.0  # SL at 1% favorable = locks ~$2
        elif fav_pct >= 0.5 and trail_tier < 1:
            new_tier = 1
            target_sl_pct = 0.0   # SL at entry = breakeven

        if target_sl_pct is not None and current_sl_id:
            # For SHORT: SL is ABOVE entry. To lock favorable, SL needs to be at entry × (1 + target_sl_pct/100)
            # target_sl_pct of -2.5 means SL is at entry × (1 - 0.025) = below entry → favorable lock
            new_sl_dec = ENTRY * (Decimal("1") + Decimal(str(target_sl_pct)) / Decimal("100"))
            new_sl_q = quantize_price(new_sl_dec, tick)
            new_sl_str = fmt(new_sl_q)
            log(f"  TRAIL TIER {new_tier}: moving SL to ${new_sl_str} (lock {target_sl_pct}% favorable)")
            # Cancel old SL
            try:
                c.cancel_algo_order(SYMBOL, algo_id=current_sl_id)
            except BinanceApiError as e:
                log(f"  cancel SL warn: {e}")
            # Place new SL
            try:
                r = c.place_stop_market_order(SYMBOL, "BUY", stop_price=new_sl_str,
                                                quantity=qty_str, close_position=False, reduce_only=True)
                new_id = int(r.get("algoId") or r.get("orderId") or 0)
                current_sl_id = new_id
                current_sl_price = float(new_sl_q)
                trail_tier = new_tier
                log(f"  NEW SL: algoId={new_id}, price=${new_sl_str}")
            except BinanceApiError as e:
                log(f"  NEW SL FAILED: {e}")

        time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        log("KeyboardInterrupt — exiting trailer (position brackets remain on Binance)")
        sys.exit(0)
    except Exception as e:
        log(f"loop error (recovered): {e}")
        time.sleep(POLL_SEC)

log("=== Trailer exited ===")
