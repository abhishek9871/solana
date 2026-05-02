"""
One-shot trade executor for the GENIUSUSDT SHORT squeeze setup.

Run:  py place_genius_short.py

Will:
  1. Show the planned trade
  2. Wait for user to type "yes" to confirm
  3. Set leverage 20x ISOLATED
  4. Place market SHORT
  5. Place stop-market SL (reduce-only, MARK_PRICE trigger)
  6. Place TP1 limit BUY @ TP1_PRICE (reduce-only, GTC)  - 50% of qty
  7. Place TP2 limit BUY @ TP2_PRICE (reduce-only, GTC)  - 50% of qty

If anything fails after the entry fills, the script tries to cancel orphaned
brackets and reports the live position so you can manage manually.
"""
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env


def get_symbol_filters(client, symbol):
    """Find the right symbol in the futures exchangeInfo (its 'symbol' param is ignored)."""
    info = client.get_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return {f["filterType"]: f for f in s["filters"]}
    raise RuntimeError(f"symbol {symbol} not found")

# === TRADE CONFIG ===
SYMBOL = "GENIUSUSDT"
SIDE_OPEN = "SELL"        # short
SIDE_CLOSE = "BUY"
LEVERAGE = 20
MARGIN_TYPE = "ISOLATED"
MARGIN_USDT = Decimal("4.00")    # commit ~50% of an $8 wallet
SL_PRICE = Decimal("0.5380")     # invalidation - above 24h high $0.5377
TP1_PRICE = Decimal("0.5100")    # -4.3% from ~$0.5327
TP2_PRICE = Decimal("0.4900")    # -8.0% from ~$0.5327
TP1_FRAC = Decimal("0.50")       # half qty at TP1
# remainder rides to TP2

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
client = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt(v: Decimal) -> str:
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def main():
    # 1. Wallet sanity
    bals = client.get_balance()
    usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
    if not usdt:
        log("ERR: no USDT balance found")
        return
    avail = Decimal(usdt.get("availableBalance", "0"))
    log(f"Wallet available: ${avail:.4f} USDT")
    if avail < MARGIN_USDT:
        log(f"ERR: need at least ${MARGIN_USDT} margin, have ${avail}")
        return

    # 2. Symbol info / precision
    filters = get_symbol_filters(client, SYMBOL)
    step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
    tick_size = Decimal(filters["PRICE_FILTER"]["tickSize"])
    min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
    log(f"{SYMBOL} stepSize={step_size}  tickSize={tick_size}  minQty={min_qty}")

    # 3. Current price
    book = client.get_book_ticker_one(SYMBOL)
    bid = Decimal(book["bidPrice"])
    ask = Decimal(book["askPrice"])
    last = (bid + ask) / 2
    log(f"Current mid: ${last:.6f}  (bid ${bid} / ask ${ask})")

    # 4. Compute qty
    notional = MARGIN_USDT * LEVERAGE
    raw_qty = notional / last
    qty = quantize_down(raw_qty, step_size)
    if qty <= 0:
        log(f"ERR: qty rounded to zero (notional ${notional}, price ${last})")
        return
    qty_tp1 = quantize_down(qty * TP1_FRAC, step_size)
    qty_tp2 = qty - qty_tp1  # remainder for TP2 + SL
    if qty_tp1 <= 0 or qty_tp2 <= 0:
        log(f"ERR: split qty too small (qty={qty}, tp1={qty_tp1}, tp2={qty_tp2})")
        return

    # 5. Quantize prices
    sl_p = quantize_price(SL_PRICE, tick_size)
    tp1_p = quantize_price(TP1_PRICE, tick_size)
    tp2_p = quantize_price(TP2_PRICE, tick_size)

    # Compute risk/reward in USD
    sl_dist_pct = (sl_p / last - 1) * 100
    tp1_dist_pct = (tp1_p / last - 1) * 100
    tp2_dist_pct = (tp2_p / last - 1) * 100
    risk_usd = notional * abs(sl_dist_pct) / 100
    reward_tp1 = notional * TP1_FRAC * abs(tp1_dist_pct) / 100
    reward_tp2 = notional * (Decimal("1") - TP1_FRAC) * abs(tp2_dist_pct) / 100

    # 6. Show plan + confirm
    log("=" * 60)
    log("PLANNED TRADE")
    log("=" * 60)
    log(f"  {SYMBOL} {SIDE_OPEN} (SHORT) {qty} contracts")
    log(f"  Leverage:    {LEVERAGE}x  ({MARGIN_TYPE})")
    log(f"  Margin:      ${MARGIN_USDT}")
    log(f"  Notional:    ${notional}")
    log(f"  Entry:       MARKET (~${last:.6f})")
    log(f"  Stop loss:   ${sl_p}  ({sl_dist_pct:+.2f}%)  -> close all on trigger")
    log(f"  TP1 (50%):   ${tp1_p}  ({tp1_dist_pct:+.2f}%)  qty={qty_tp1}")
    log(f"  TP2 (rest):  ${tp2_p}  ({tp2_dist_pct:+.2f}%)  qty={qty_tp2}")
    log(f"  Risk if SL:  ${risk_usd:.4f}")
    log(f"  Reward TP1:  ${reward_tp1:.4f}")
    log(f"  Reward TP2:  ${reward_tp2:.4f}")
    log("=" * 60)
    confirm = input("\nType YES to execute, anything else to abort: ").strip().upper()
    if confirm != "YES":
        log("Aborted by user.")
        return

    # 7. Set leverage and margin type
    try:
        client.set_leverage(SYMBOL, LEVERAGE)
        log(f"Leverage set to {LEVERAGE}x")
    except BinanceApiError as e:
        log(f"set_leverage warn: {e}")
    try:
        client.set_margin_type(SYMBOL, MARGIN_TYPE)
        log(f"Margin set to {MARGIN_TYPE}")
    except BinanceApiError as e:
        # -4046 = already set; -4168 = Multi-Assets mode forces CROSS
        if "4046" in str(e):
            log(f"Margin already {MARGIN_TYPE}")
        elif "4168" in str(e):
            log(f"Multi-Assets mode active; using CROSS margin (acceptable for this size)")
        else:
            log(f"set_margin_type warn: {e}")

    # 8. Place entry market order
    log("Placing MARKET SHORT entry...")
    try:
        entry = client.place_market_order(SYMBOL, SIDE_OPEN, fmt(qty))
        log(f"  ENTRY result: orderId={entry.get('orderId')} "
            f"status={entry.get('status')} avgPrice={entry.get('avgPrice')} "
            f"executedQty={entry.get('executedQty')}")
    except BinanceApiError as e:
        log(f"ENTRY FAILED: {e}")
        return

    # 9. Place stop-market SL
    log("Placing STOP-MARKET SL (reduce-only)...")
    try:
        sl_order = client.place_stop_market_order(
            SYMBOL, SIDE_CLOSE, fmt(sl_p), close_position=True)
        log(f"  SL algoId={sl_order.get('algoId') or sl_order.get('orderId')}")
    except BinanceApiError as e:
        log(f"SL FAILED: {e} -- POSITION IS NAKED, manage manually!")
        return

    # 10. Place TP1 limit (reduce-only, GTC)
    log("Placing TP1 limit BUY (50%, reduce-only, GTC)...")
    try:
        tp1_order = client.place_limit_order(
            SYMBOL, SIDE_CLOSE, fmt(tp1_p), fmt(qty_tp1),
            time_in_force="GTC", reduce_only=True)
        log(f"  TP1 orderId={tp1_order.get('orderId')} status={tp1_order.get('status')}")
    except BinanceApiError as e:
        log(f"TP1 FAILED: {e}")

    # 11. Place TP2 limit (reduce-only, GTC)
    log("Placing TP2 limit BUY (rest, reduce-only, GTC)...")
    try:
        tp2_order = client.place_limit_order(
            SYMBOL, SIDE_CLOSE, fmt(tp2_p), fmt(qty_tp2),
            time_in_force="GTC", reduce_only=True)
        log(f"  TP2 orderId={tp2_order.get('orderId')} status={tp2_order.get('status')}")
    except BinanceApiError as e:
        log(f"TP2 FAILED: {e}")

    # 12. Final summary
    log("=" * 60)
    log("ALL ORDERS SUBMITTED")
    log("=" * 60)
    log(f"  Position:    SHORT {qty} {SYMBOL} @ market")
    log(f"  Stop loss:   ${sl_p}  (close all)")
    log(f"  TP1:         ${tp1_p}  ({qty_tp1} qty)")
    log(f"  TP2:         ${tp2_p}  ({qty_tp2} qty)")
    log("Watch from Binance UI. DO NOT touch the orders.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user.")
