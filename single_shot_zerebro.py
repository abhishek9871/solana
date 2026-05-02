"""Second concentrated bet — ZEREBROUSDT BUY (rank #2 scanner candidate)."""
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "ZEREBROUSDT"
SIDE = "BUY"
LEVERAGE = 10
MARGIN_USDT = Decimal("150.0")
TP_PCT = Decimal("0.05")
SL_PCT = Decimal("0.012")


def quantize_down(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value, tick):
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

# Don't double-up if already have ZEREBRO position
positions = c.get_positions(SYMBOL)
for p in positions:
    if float(p.get("positionAmt", 0)) != 0:
        print(f"Already have {SYMBOL} position — aborting")
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
ref_px = ask if SIDE == "BUY" else bid
print(f"Bid {bid} Ask {ask}")

notional = MARGIN_USDT * Decimal(LEVERAGE)
qty = quantize_down(notional / ref_px, step)
qty_str = fmt(qty)
print(f"qty {qty_str} notional ${qty * ref_px:.2f}")

print(f"\nMARKET {SIDE}...")
try:
    order = c.place_market_order(SYMBOL, SIDE, quantity=qty_str)
    fill_px = Decimal(str(order.get("avgPrice") or ref_px))
    print(f"FILLED {fill_px}")
except BinanceApiError as e:
    print(f"FAIL: {e}")
    raise SystemExit(1)

if SIDE == "BUY":
    tp_px = quantize_price(fill_px * (Decimal("1") + TP_PCT), tick)
    sl_px = quantize_price(fill_px * (Decimal("1") - SL_PCT), tick)
else:
    tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), tick)
    sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), tick)

close_side = "SELL" if SIDE == "BUY" else "BUY"
try:
    tp_r = c.place_take_profit_order(SYMBOL, close_side, stop_price=fmt(tp_px),
                                       quantity=qty_str, close_position=False, reduce_only=True)
    print(f"TP placed at {tp_px}: algoId={tp_r.get('algoId')}")
except BinanceApiError as e:
    print(f"TP FAIL: {e}")
try:
    sl_r = c.place_stop_market_order(SYMBOL, close_side, stop_price=fmt(sl_px),
                                       quantity=qty_str, close_position=False, reduce_only=True)
    print(f"SL placed at {sl_px}: algoId={sl_r.get('algoId')}")
except BinanceApiError as e:
    print(f"SL FAIL: {e}")

print(f"\nDONE. ZEREBROUSDT BUY live with brackets.")
print(f"  TP {tp_px} = +$75 if hits")
print(f"  SL {sl_px} = -$18 if hits")
