"""Place GTC LIMIT at the max allowed price to close stuck SWARMS."""
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

SYMBOL = "SWARMSUSDT"

positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
if amt == 0:
    print("No SWARMS position open")
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

# The error "Limit price can't be higher than X" tells us the cap. Use that.
# Mark = ~$0.0252, max allowed BUY limit = $0.02646
# Place at exactly the cap, GTC, reduce_only — sits in book until fill
bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
mark_pi = c.public_get("/fapi/v1/premiumIndex", {"symbol": SYMBOL})
mark = Decimal(mark_pi["markPrice"])

# Cap is mark × 1.05 typically — use 1.04 to stay safely inside, round DOWN to tick
raw_cap = mark * Decimal("1.04")
cap = (raw_cap / tick).to_integral_value(rounding=ROUND_DOWN) * tick
print(f"bid={bid} ask={ask} mark={mark} computed cap={cap}")

qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
qty_str = format(qty_q.normalize(), "f") if qty_q != qty_q.to_integral() else format(qty_q, "f")
px_str = format(cap.normalize(), "f") if cap != cap.to_integral() else format(cap, "f")

print(f"Placing GTC LIMIT {side} qty={qty_str} px={px_str} reduce_only=True")
try:
    r = c.place_limit_order(SYMBOL, side, price=px_str, quantity=qty_str,
                            time_in_force="GTX", reduce_only=True)
    status = r.get("status")
    order_id = r.get("orderId")
    print(f"  GTC LIMIT placed: orderId={order_id}, status={status}")
    print(f"  Order will sit in book until ask drops to ${px_str} or below")
except BinanceApiError as e:
    msg = str(e)
    print(f"  GTC LIMIT failed: {e}")
    # If error tells us the actual cap, use it
    if "can't be higher than" in msg:
        # Parse the cap from the error
        try:
            actual_cap_str = msg.split("can't be higher than")[1].strip().rstrip(".'}").strip()
            actual_cap = Decimal(actual_cap_str)
            actual_cap_q = (actual_cap / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            px_str = format(actual_cap_q.normalize(), "f") if actual_cap_q != actual_cap_q.to_integral() else format(actual_cap_q, "f")
            print(f"  retrying at exchange-reported cap: {px_str}")
            r = c.place_limit_order(SYMBOL, side, price=px_str, quantity=qty_str,
                                    time_in_force="GTC", reduce_only=True)
            print(f"  GTC LIMIT placed: orderId={r.get('orderId')}, status={r.get('status')}")
        except Exception as e2:
            print(f"  retry also failed: {e2}")
