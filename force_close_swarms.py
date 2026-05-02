"""
Aggressive close attempt for SWARMS — cancel existing GTX, retry market then IOC.
"""
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

SYMBOL = "SWARMSUSDT"

# Cancel everything
try:
    c.cancel_all_orders(SYMBOL)
    print("Cancelled regular orders")
except BinanceApiError as e:
    print(f"cancel_all err: {e}")
try:
    c.cancel_all_algo_orders(SYMBOL)
    print("Cancelled algo orders")
except BinanceApiError as e:
    print(f"cancel_algo err: {e}")

# Get position
positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
if amt == 0:
    print("No position - already closed")
    raise SystemExit(0)

side = "SELL" if amt > 0 else "BUY"
qty = abs(amt)
print(f"Position: {amt:+}, closing with {side} {qty}")

info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])

qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
qty_str = format(qty_q.normalize(), "f") if qty_q != qty_q.to_integral() else format(qty_q, "f")

# 1. Try market
print("\n[1] Trying MARKET reduce_only...")
try:
    r = c.place_market_order(SYMBOL, side, quantity=qty_str, reduce_only=True)
    print(f"  MARKET OK: orderId={r.get('orderId')}, status={r.get('status')}")
except BinanceApiError as e:
    print(f"  MARKET failed: {e}")

# Check if closed
time.sleep(0.5)
positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
if amt == 0:
    print("CLOSED via MARKET")
    raise SystemExit(0)
print(f"Still open: {amt}")

# 2. Try LIMIT IOC at multiple price points
pi = c.public_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
mark = Decimal(pi["markPrice"])
bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
print(f"\nbid={bid} ask={ask} mark={mark}")

# Try IOC at progressively higher prices, capped under PERCENT_PRICE
for mult in [Decimal("1.005"), Decimal("1.015"), Decimal("1.025"), Decimal("1.035"), Decimal("1.045")]:
    raw = mark * mult
    px = (raw / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    px_str = format(px.normalize(), "f") if px != px.to_integral() else format(px, "f")
    print(f"\n[IOC] mark*{mult} = {px_str} ...")
    try:
        r = c.place_limit_order(SYMBOL, side, price=px_str, quantity=qty_str,
                                time_in_force="IOC", reduce_only=True)
        executed = r.get("executedQty", "0")
        print(f"  status={r.get('status')} executedQty={executed}")
        if float(executed) > 0:
            print(f"  PARTIAL/FULL FILL — {executed} closed at {px_str}")
            time.sleep(0.5)
            # Check remaining
            positions = c.get_positions(SYMBOL)
            amt = 0.0
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                break
            print(f"  Remaining position: {amt}")
            if amt == 0:
                print("CLOSED")
                raise SystemExit(0)
            else:
                qty = abs(amt)
                qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
                qty_str = format(qty_q.normalize(), "f") if qty_q != qty_q.to_integral() else format(qty_q, "f")
    except BinanceApiError as e:
        print(f"  failed: {e}")

# 3. Final: place GTX at safe cap
print(f"\n[GTX] sitting at safe cap...")
raw = mark * Decimal("1.04")
px = (raw / tick).to_integral_value(rounding=ROUND_DOWN) * tick
px_str = format(px.normalize(), "f") if px != px.to_integral() else format(px, "f")
try:
    r = c.place_limit_order(SYMBOL, side, price=px_str, quantity=qty_str,
                            time_in_force="GTX", reduce_only=True)
    print(f"  GTX placed at {px_str}, orderId={r.get('orderId')}")
except BinanceApiError as e:
    print(f"  GTX failed: {e}")

# Final state
positions = c.get_positions(SYMBOL)
for p in positions:
    a = float(p.get("positionAmt", 0))
    if a != 0:
        print(f"\nFinal: SWARMS amt={a}, mark={p.get('markPrice')}, unPnL={p.get('unRealizedProfit')}")
