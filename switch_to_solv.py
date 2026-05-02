"""Close BSBUSDT, prep for SOLVUSDT flipper launch."""
import os
import time
from decimal import Decimal, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


# Close BSBUSDT
SYMBOL = "BSBUSDT"
print(f"Closing all {SYMBOL} orders/positions...")
try:
    c.cancel_all_orders(SYMBOL)
except BinanceApiError:
    pass
try:
    c.cancel_all_algo_orders(SYMBOL)
except BinanceApiError:
    pass
positions = c.get_positions(SYMBOL)
for p in positions:
    amt = float(p.get("positionAmt", 0))
    if amt != 0:
        side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)
        info = c.get_symbol_info(SYMBOL)
        step = Decimal("1")
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = Decimal(f["stepSize"])
        qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        qty_str = fmt(qty_q)
        try:
            c.place_market_order(SYMBOL, side, quantity=qty_str, reduce_only=True)
            print(f"Closed: {side} {qty_str}")
        except BinanceApiError as e:
            print(f"Close fail: {e}")

time.sleep(1)
bals = c.get_balance()
usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
print(f"\nWallet now: ${float(usdt.get('availableBalance',0)):.4f}")
