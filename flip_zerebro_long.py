"""
FLIP: close ZEREBROUSDT short, immediately open LONG.

Cancels all existing orders, market-closes the short, then opens a fresh long
with new SL/TP brackets. Pays fees + slippage twice. Use only if you're
confident the trend has flipped.
"""
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "ZEREBROUSDT"
LEVERAGE = 10

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
client = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def quantize_down(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value, tick):
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def main():
    # 1. Get current state
    log("Reading position state...")
    poss = client.get_positions(symbol=SYMBOL)
    pos = next((p for p in poss if abs(float(p.get("positionAmt", "0"))) > 0), None)
    if not pos:
        log("No open position — nothing to flip. Will open fresh long.")
        short_qty = 0
    else:
        amt = float(pos["positionAmt"])
        if amt > 0:
            log(f"ERR: position is LONG ({amt}), not SHORT. Aborting.")
            return
        short_qty = abs(amt)
        log(f"  Currently SHORT {short_qty} @ entry {pos['entryPrice']}, uPnL {pos['unRealizedProfit']}")

    # 2. Get filters
    info = client.get_exchange_info()
    filters = None
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            filters = {f["filterType"]: f for f in s["filters"]}
            break
    step = Decimal(filters["LOT_SIZE"]["stepSize"])
    tick = Decimal(filters["PRICE_FILTER"]["tickSize"])

    # 3. Show plan
    book = client.get_book_ticker_one(SYMBOL)
    last = (Decimal(book["bidPrice"]) + Decimal(book["askPrice"])) / 2
    bals = client.get_balance()
    usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
    avail = Decimal(usdt.get("availableBalance", "0"))

    margin = Decimal("4.00")     # smaller margin since we already lost on short
    notional = margin * LEVERAGE
    long_qty = quantize_down(notional / last, step)
    sl_p = quantize_price(last * Decimal("0.980"), tick)    # -2% SL
    tp1_p = quantize_price(last * Decimal("1.020"), tick)   # +2%
    tp2_p = quantize_price(last * Decimal("1.040"), tick)   # +4%

    log("=" * 60)
    log("FLIP PLAN")
    log("=" * 60)
    log(f"  1. Cancel all open orders on {SYMBOL}")
    if short_qty > 0:
        log(f"  2. Close short by buying {int(short_qty)} {SYMBOL} (reduce-only)")
    log(f"  3. Open LONG {long_qty} {SYMBOL} @ {LEVERAGE}x")
    log(f"     Margin ${margin}  Notional ${notional}  Entry ~${last:.6f}")
    log(f"     SL ${sl_p}  TP1 ${tp1_p}  TP2 ${tp2_p}")
    log(f"  Wallet: ${avail:.2f}")
    log("=" * 60)

    confirm = input("Type FLIP to execute, anything else aborts: ").strip().upper()
    if confirm != "FLIP":
        log("Aborted.")
        return

    # 4. Cancel all open orders (limit + algo)
    log("Cancelling all orders...")
    try:
        client.cancel_all_orders(SYMBOL)
        log("  cancel_all_orders OK")
    except BinanceApiError as e:
        log(f"  cancel_all_orders warn: {e}")
    try:
        client.cancel_all_algo_orders(SYMBOL)
        log("  cancel_all_algo_orders OK")
    except BinanceApiError as e:
        log(f"  cancel_all_algo warn: {e}")

    # 5. Close short
    if short_qty > 0:
        log(f"Closing short: BUY {int(short_qty)} {SYMBOL} (reduce-only)...")
        try:
            r = client.place_market_order(SYMBOL, "BUY", str(int(short_qty)), reduce_only=True)
            log(f"  CLOSE: orderId={r.get('orderId')} avgPrice={r.get('avgPrice')}")
        except BinanceApiError as e:
            log(f"  CLOSE FAILED: {e}")
            return

    # 6. Open long
    log(f"Opening long: BUY {long_qty} {SYMBOL}...")
    try:
        r = client.place_market_order(SYMBOL, "BUY", fmt(long_qty))
        log(f"  ENTRY: orderId={r.get('orderId')} avgPrice={r.get('avgPrice')}")
    except BinanceApiError as e:
        log(f"  ENTRY FAILED: {e}")
        return

    # 7. SL + TPs
    log(f"Placing SL {sl_p} (close all)...")
    try:
        client.place_stop_market_order(SYMBOL, "SELL", fmt(sl_p), close_position=True)
        log("  SL placed")
    except BinanceApiError as e:
        log(f"  SL FAILED: {e}")

    qty1 = quantize_down(long_qty * Decimal("0.5"), step)
    qty2 = long_qty - qty1
    for label, price, q in [("TP1", tp1_p, qty1), ("TP2", tp2_p, qty2)]:
        log(f"Placing {label} limit SELL {q}@${price}...")
        try:
            client.place_limit_order(SYMBOL, "SELL", fmt(price), fmt(q), time_in_force="GTC", reduce_only=True)
            log(f"  {label} placed")
        except BinanceApiError as e:
            log(f"  {label} FAILED: {e}")

    log("=" * 60)
    log("FLIP COMPLETE — long position with brackets")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted.")
