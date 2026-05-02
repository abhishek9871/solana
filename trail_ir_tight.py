"""Move IRUSDT SL down to lock minimum +$45 profit. Keep TP at +5%."""
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

SYMBOL = "IRUSDT"
NEW_SL_PRICE = Decimal("0.02033")  # locks ~+$46 (entry $0.02065 → SL $0.02033 = -1.55% favorable)

# Get position
positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
if amt == 0:
    print("No IRUSDT position!")
    raise SystemExit(1)
qty = abs(amt)
close_side = "BUY" if amt < 0 else "SELL"
print(f"Position: {amt:+}, qty {qty}, close-side {close_side}")

# Get tick/step
info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])

# Find existing SL algo (it's a BUY trigger above entry)
algos = c.get_open_algo_orders(SYMBOL)
algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
print(f"Open algos on {SYMBOL}: {len(algo_list)}")
sl_algo_id = None
for a in algo_list:
    trig = float(a.get("triggerPrice", 0) or a.get("stopPrice", 0))
    if a.get("side") == "BUY" and trig > 0.0205:  # the SL (above entry for short close)
        sl_algo_id = int(a.get("algoId") or a.get("orderId") or 0)
        print(f"Found SL algo: id={sl_algo_id}, triggerPrice={trig}")
        break

# Cancel old SL
if sl_algo_id:
    try:
        c.cancel_algo_order(SYMBOL, algo_id=sl_algo_id)
        print(f"Cancelled old SL")
    except BinanceApiError as e:
        print(f"cancel warn: {e}")

# Place new SL at tighter level
qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
qty_str = format(qty_q.normalize(), "f") if qty_q != qty_q.to_integral() else format(qty_q, "f")
sl_q = (NEW_SL_PRICE / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
sl_str = format(sl_q.normalize(), "f") if sl_q != sl_q.to_integral() else format(sl_q, "f")
print(f"Placing new SL: BUY at {sl_str}, qty {qty_str}")
try:
    r = c.place_stop_market_order(SYMBOL, close_side, stop_price=sl_str, quantity=qty_str,
                                   close_position=False, reduce_only=True)
    print(f"NEW SL placed: algoId={r.get('algoId') or r.get('orderId')}")
except BinanceApiError as e:
    print(f"NEW SL FAILED: {e}")
    raise SystemExit(1)

print("\n=== Done ===")
print(f"  Worst case (price reverses): SL at {sl_str} hits, lock ~+$46 minimum")
print(f"  Best case (continues down):  TP at $0.01962 hits, +$75 win")
print(f"  Current unrealized: ~+$59")
