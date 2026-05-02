"""
Move SL on a winning position to breakeven (entry price).
Safely cancels existing SL algo order and places new STOP_MARKET at the new price.
"""
import os
import sys
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

# === EDIT THESE ===
SYMBOL = "ZEREBROUSDT"
NEW_SL_PRICE = Decimal("0.02344")  # entry = breakeven
ORIGINAL_SL_PRICE = Decimal("0.02372")  # the one to cancel (matches existing trigger)
# ==================

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")


def quantize_price(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt_price(value: Decimal, tick: Decimal) -> str:
    q = quantize_price(value, tick)
    return format(q.normalize(), "f") if q != q.to_integral() else format(q, "f")


def fmt_qty(value: Decimal, step: Decimal) -> str:
    if step <= 0:
        return str(value)
    q = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    return format(q.normalize(), "f") if q != q.to_integral() else format(q, "f")


# 1. Find current position to determine close-side & qty
positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
if amt == 0:
    print(f"ERROR: no open position on {SYMBOL}")
    sys.exit(1)
qty = abs(amt)
close_side = "SELL" if amt > 0 else "BUY"
print(f"Position: {SYMBOL} {amt:+}, close-side={close_side}, qty={qty}")

# 2. Get tick/step
info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])
print(f"Tick: {tick}, Step: {step}")

# 3. Find the SL algo order (the one matching ORIGINAL_SL_PRICE)
algos = c.get_open_algo_orders(SYMBOL)
algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
sl_algo_id = None
for a in algo_list:
    trig = Decimal(str(a.get("triggerPrice", 0) or a.get("stopPrice", 0)))
    if abs(trig - ORIGINAL_SL_PRICE) < tick:
        sl_algo_id = int(a.get("algoId") or a.get("orderId") or 0)
        print(f"Found SL algo: algoId={sl_algo_id} trigger={trig}")
        break
if not sl_algo_id:
    print(f"ERROR: could not find SL algo at {ORIGINAL_SL_PRICE}")
    print(f"Existing algos:")
    for a in algo_list:
        print(f"  {a}")
    sys.exit(1)

# 4. Cancel old SL
try:
    c.cancel_algo_order(SYMBOL, algo_id=sl_algo_id)
    print(f"Cancelled old SL algoId={sl_algo_id}")
except BinanceApiError as e:
    print(f"WARN: cancel failed (maybe already filled): {e}")

# 5. Place new STOP_MARKET at NEW_SL_PRICE
qty_str = fmt_qty(Decimal(str(qty)), step)
new_price_str = fmt_price(NEW_SL_PRICE, tick)
print(f"Placing new STOP_MARKET {close_side} qty={qty_str} stopPrice={new_price_str}")
try:
    r = c.place_stop_market_order(
        SYMBOL, close_side,
        stop_price=new_price_str,
        quantity=qty_str,
        close_position=False,
        reduce_only=True,
    )
    new_id = r.get("algoId") or r.get("orderId")
    print(f"NEW SL placed at ${new_price_str}, algoId={new_id}")
except BinanceApiError as e:
    print(f"ERROR: failed to place new SL: {e}")
    print("WARNING: position is now WITHOUT SL — exposure unprotected!")
    sys.exit(1)

print("DONE — SL moved successfully")
