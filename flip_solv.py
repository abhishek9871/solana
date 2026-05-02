"""Close SOLVUSDT long, open SOLVUSDT short. Single switch."""
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "SOLVUSDT"
LEVERAGE = 10
MARGIN_USDT = Decimal("18.0")
TP_PCT = Decimal("0.05")
SL_PCT = Decimal("0.012")


def quantize_down(v, s):
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def quantize_price(v, t):
    return (v / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# Step 1: cancel all SOLV orders + algos
print("=== STEP 1: Cancel all SOLVUSDT orders ===")
try:
    c.cancel_all_orders(SYMBOL)
except BinanceApiError as e:
    print(f"cancel_all_orders: {e}")
try:
    c.cancel_all_algo_orders(SYMBOL)
except BinanceApiError as e:
    print(f"cancel_algo: {e}")

# Step 2: close existing long position
print("\n=== STEP 2: Close existing long ===")
positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])
if amt != 0:
    qty = abs(amt)
    side = "SELL" if amt > 0 else "BUY"
    qty_q = quantize_down(Decimal(str(qty)), step)
    qty_str = fmt(qty_q)
    try:
        c.place_market_order(SYMBOL, side, quantity=qty_str, reduce_only=True)
        print(f"Closed long: {side} {qty_str}")
    except BinanceApiError as e:
        print(f"Close fail: {e}")
        raise SystemExit(1)
    time.sleep(1)
else:
    print("No position to close")

# Step 3: open SHORT
print("\n=== STEP 3: Open SOLVUSDT SHORT ===")
bals = c.get_balance()
usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
wallet = float(usdt.get("availableBalance", 0)) if usdt else 0
print(f"Available: ${wallet:.4f}")
if wallet < float(MARGIN_USDT) + 1:
    print("INSUFFICIENT")
    raise SystemExit(1)

try:
    c.set_leverage(SYMBOL, LEVERAGE)
except BinanceApiError as e:
    print(f"set_leverage: {e}")

bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
ref_px = bid  # SHORT entry at bid
print(f"Bid {bid} Ask {ask}")

notional = MARGIN_USDT * Decimal(LEVERAGE)
qty = quantize_down(notional / ref_px, step)
qty_str = fmt(qty)
print(f"Qty {qty_str} notional ${qty * ref_px:.2f}")

print(f">>> FIRING MARKET SELL {SYMBOL} <<<")
try:
    order = c.place_market_order(SYMBOL, "SELL", quantity=qty_str)
    avg = order.get("avgPrice") or order.get("price") or ref_px
    fill_px = Decimal(str(avg)) if avg and float(avg) > 0 else ref_px
    print(f"FILLED at ${fill_px}")
except BinanceApiError as e:
    print(f"FAIL: {e}")
    raise SystemExit(1)

# SHORT brackets: TP below entry, SL above entry
tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), tick)
sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), tick)

try:
    tp_r = c.place_take_profit_order(SYMBOL, "BUY", stop_price=fmt(tp_px), quantity=qty_str,
                                       close_position=False, reduce_only=True)
    print(f"TP placed: ${tp_px} algoId={tp_r.get('algoId')}")
except BinanceApiError as e:
    print(f"TP FAIL: {e}")
    try:
        c.place_market_order(SYMBOL, "BUY", quantity=qty_str, reduce_only=True)
    except Exception:
        pass
    raise SystemExit(1)

try:
    sl_r = c.place_stop_market_order(SYMBOL, "BUY", stop_price=fmt(sl_px), quantity=qty_str,
                                       close_position=False, reduce_only=True)
    print(f"SL placed: ${sl_px} algoId={sl_r.get('algoId')}")
except BinanceApiError as e:
    print(f"SL FAIL: {e}")
    try:
        c.place_market_order(SYMBOL, "BUY", quantity=qty_str, reduce_only=True)
    except Exception:
        pass
    raise SystemExit(1)

print(f"\n=== SOLV SHORT LIVE ===")
print(f"  Entry: ${fill_px}")
print(f"  TP:    ${tp_px} (-5% on price = ~+$8.82 win)")
print(f"  SL:    ${sl_px} (+1.2% adverse = ~-$2.34 loss)")
