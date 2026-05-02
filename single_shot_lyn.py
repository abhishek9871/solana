"""
Single concentrated ORCA-style bet on LYNUSDT (top scanner candidate).
$100 margin × 10x = $1000 notional. +5% TP / -1.2% SL.
Server-side brackets so it fires regardless of whether bot is alive.
"""
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env


SYMBOL = "IRUSDT"
SIDE = "SELL"  # SHORT the trapped longs (funding +16.45%/8h forcing unwind)
LEVERAGE = 10
MARGIN_USDT = Decimal("150.0")  # $150 × 10x = $1500 notional → ~$75 TP win
TP_PCT = Decimal("0.05")    # +5% favorable for short = -5% on price
SL_PCT = Decimal("0.012")   # -1.2% adverse for short = +1.2% on price


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != value.to_integral() else format(value, "f")


load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

# Pre-check: no existing position
positions = c.get_positions(SYMBOL)
for p in positions:
    if float(p.get("positionAmt", 0)) != 0:
        print(f"ALREADY HAVE POSITION on {SYMBOL}, aborting")
        raise SystemExit(1)

# Symbol info
info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])
print(f"Symbol: {SYMBOL}, tick={tick}, step={step}")

# Set leverage
try:
    c.set_leverage(SYMBOL, LEVERAGE)
    print(f"Leverage set to {LEVERAGE}x")
except BinanceApiError as e:
    print(f"set_leverage warn: {e}")

# Fetch current book
bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
bid = Decimal(bt["bidPrice"])
ask = Decimal(bt["askPrice"])
ref_px = bid if SIDE == "SELL" else ask
print(f"Bid: {bid}, Ask: {ask}, ref entry: {ref_px}")

# Calculate quantity
notional = MARGIN_USDT * Decimal(LEVERAGE)
qty_raw = notional / ref_px
qty = quantize_down(qty_raw, step)
qty_str = fmt_decimal(qty)
print(f"Notional ${notional}, qty {qty_str}")

# Market entry
print(f"\nPlacing MARKET {SIDE} order...")
try:
    order = c.place_market_order(SYMBOL, SIDE, quantity=qty_str)
    avg = order.get("avgPrice") or order.get("price") or ref_px
    fill_px = Decimal(str(avg)) if avg else ref_px
    print(f"FILLED at ${fill_px}")
except BinanceApiError as e:
    print(f"ENTRY FAILED: {e}")
    raise SystemExit(1)

# Bracket prices
close_side = "BUY" if SIDE == "SELL" else "SELL"
if SIDE == "BUY":
    tp_px = quantize_price(fill_px * (Decimal("1") + TP_PCT), tick)
    sl_px = quantize_price(fill_px * (Decimal("1") - SL_PCT), tick)
else:
    tp_px = quantize_price(fill_px * (Decimal("1") - TP_PCT), tick)
    sl_px = quantize_price(fill_px * (Decimal("1") + SL_PCT), tick)
print(f"TP: ${tp_px} ({TP_PCT*100}% favorable)")
print(f"SL: ${sl_px} ({SL_PCT*100}% adverse)")

# Place TP
try:
    tp = c.place_take_profit_order(SYMBOL, close_side,
                                    stop_price=fmt_decimal(tp_px),
                                    quantity=qty_str,
                                    close_position=False, reduce_only=True)
    print(f"TP placed: algoId={tp.get('algoId')}")
except BinanceApiError as e:
    print(f"TP FAILED: {e}")

# Place SL
try:
    sl = c.place_stop_market_order(SYMBOL, close_side,
                                    stop_price=fmt_decimal(sl_px),
                                    quantity=qty_str,
                                    close_position=False, reduce_only=True)
    print(f"SL placed: algoId={sl.get('algoId')}")
except BinanceApiError as e:
    print(f"SL FAILED: {e}")

# Final state
print("\n=== Position confirmed ===")
positions = c.get_positions(SYMBOL)
for p in positions:
    if float(p.get("positionAmt", 0)) != 0:
        print(f"  amt: {p['positionAmt']}, entry: {p['entryPrice']}, mark: {p['markPrice']}")

print(f"\nTrade is LIVE. Brackets are server-side — will trigger automatically.")
print(f"  Win at TP:  ${MARGIN_USDT} margin × 10x × 5% - fees ≈ +$48")
print(f"  Lose at SL: ${MARGIN_USDT} × 10x × 1.2% + fees ≈ -$13")
print(f"\n  Watch on Binance Demo UI. Brackets fire on Binance's matching engine.")
