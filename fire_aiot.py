"""Fire AIOTUSDT SHORT on PROD — best confluence setup."""
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "AIOTUSDT"
SIDE = "SELL"
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

bals = c.get_balance()
usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
wallet = float(usdt.get("availableBalance", 0)) if usdt else 0
print(f"Wallet available: ${wallet:.4f}")
if wallet < float(MARGIN_USDT) + 1:
    print("INSUFFICIENT")
    raise SystemExit(1)

positions = c.get_positions(SYMBOL)
for p in positions:
    if float(p.get("positionAmt", 0)) != 0:
        print(f"Already have {SYMBOL} — abort")
        raise SystemExit(1)

info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])

try:
    c.set_leverage(SYMBOL, LEVERAGE)
except BinanceApiError as e:
    print(f"set_leverage warn: {e}")

bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
ref_px = bid if SIDE == "SELL" else ask
print(f"Bid {bid} Ask {ask} ref {ref_px}")

notional = MARGIN_USDT * Decimal(LEVERAGE)
qty = quantize_down(notional / ref_px, step)
qty_str = fmt(qty)
print(f"Qty {qty_str} notional ${qty * ref_px:.2f}")

print(f"\n>>> FIRING MARKET {SIDE} on {SYMBOL} <<<")
try:
    order = c.place_market_order(SYMBOL, SIDE, quantity=qty_str)
    avg = order.get("avgPrice") or order.get("price") or ref_px
    fill_px = Decimal(str(avg)) if avg and float(avg) > 0 else ref_px
    print(f"FILLED at ${fill_px}")
except BinanceApiError as e:
    print(f"ENTRY FAIL: {e}")
    raise SystemExit(1)

if SIDE == "BUY":
    tp_px = quantize_price(fill_px * (Decimal("1") + TP_PCT), tick)
    sl_px = quantize_price(fill_px * (Decimal("1") - SL_PCT), tick)
else:
    tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), tick)
    sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), tick)
close_side = "BUY" if SIDE == "SELL" else "SELL"

try:
    tp_r = c.place_take_profit_order(SYMBOL, close_side, stop_price=fmt(tp_px), quantity=qty_str,
                                       close_position=False, reduce_only=True)
    print(f"TP placed at {tp_px}: algoId={tp_r.get('algoId')}")
except BinanceApiError as e:
    print(f"TP FAIL: {e}")
    try:
        c.place_market_order(SYMBOL, close_side, quantity=qty_str, reduce_only=True)
    except Exception:
        pass
    raise SystemExit(1)

try:
    sl_r = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(sl_px), quantity=qty_str,
                                       close_position=False, reduce_only=True)
    print(f"SL placed at {sl_px}: algoId={sl_r.get('algoId')}")
except BinanceApiError as e:
    print(f"SL FAIL: {e}")
    try:
        c.place_market_order(SYMBOL, close_side, quantity=qty_str, reduce_only=True)
    except Exception:
        pass
    raise SystemExit(1)

print(f"\n=== TRADE LIVE ===")
print(f"  Entry:  ${fill_px}")
print(f"  TP:     ${tp_px}  (-5% on price = +$8.55 net win)")
print(f"  SL:     ${sl_px}  (+1.2% adverse = -$2.50 net loss)")
print(f"  Brackets server-side. Walk away.")
