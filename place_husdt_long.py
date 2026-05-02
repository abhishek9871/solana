"""
HUSDT LONG — momentum continuation play.

Pattern: 5 consecutive green 5m candles, big volume spike (2M, 1.4M on breakouts),
funding slightly positive but not extreme, price building above $0.184.

Aggressive sizing — uses ~$5 margin at 50x leverage on $7 wallet.
If TP3 hits, ~$50 profit. If SL hits, wallet drops to ~$2.
"""
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "HUSDT"
SIDE_OPEN = "BUY"          # long
SIDE_CLOSE = "SELL"
LEVERAGE = 50              # max aggressive (will downgrade if not allowed)
MARGIN_USDT = Decimal("5.00")
SL_PRICE = Decimal("0.1840")    # -2.1%
TP1_PRICE = Decimal("0.1950")   # +3.8% (R:R 1.8)
TP2_PRICE = Decimal("0.2050")   # +9.1% (R:R 4.3)
TP3_PRICE = Decimal("0.2200")   # +17.1% (R:R 8.1) -> ~$50 profit
TP1_FRAC = Decimal("0.33")
TP2_FRAC = Decimal("0.33")
# rest goes to TP3

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
client = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_filters(symbol):
    info = client.get_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return {f["filterType"]: f for f in s["filters"]}
    raise RuntimeError(f"{symbol} not found")


def quantize_down(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(value, tick):
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def main():
    bals = client.get_balance()
    usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
    avail = Decimal(usdt.get("availableBalance", "0"))
    log(f"Wallet: ${avail:.4f} USDT")
    if avail < MARGIN_USDT:
        log(f"ERR: need ${MARGIN_USDT} margin, have ${avail}")
        return

    filters = get_filters(SYMBOL)
    step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
    tick_size = Decimal(filters["PRICE_FILTER"]["tickSize"])
    min_qty = Decimal(filters["LOT_SIZE"]["minQty"])
    log(f"{SYMBOL} step={step_size}  tick={tick_size}  minQty={min_qty}")

    book = client.get_book_ticker_one(SYMBOL)
    bid, ask = Decimal(book["bidPrice"]), Decimal(book["askPrice"])
    last = (bid + ask) / 2
    log(f"Mid: ${last:.6f}  bid ${bid}  ask ${ask}")

    # Try max leverage, fall back if rejected
    actual_lev = LEVERAGE
    try:
        client.set_leverage(SYMBOL, LEVERAGE)
        log(f"Leverage set to {LEVERAGE}x")
    except BinanceApiError as e:
        log(f"  {LEVERAGE}x rejected: {e}")
        for try_lev in [25, 20, 10, 5]:
            try:
                client.set_leverage(SYMBOL, try_lev)
                actual_lev = try_lev
                log(f"  Fell back to {try_lev}x")
                break
            except BinanceApiError:
                continue
        else:
            log("ERR: could not set any leverage")
            return

    notional = MARGIN_USDT * actual_lev
    qty = quantize_down(notional / last, step_size)
    if qty < min_qty:
        log(f"ERR: qty {qty} < min {min_qty}")
        return

    qty_tp1 = quantize_down(qty * TP1_FRAC, step_size)
    qty_tp2 = quantize_down(qty * TP2_FRAC, step_size)
    qty_tp3 = qty - qty_tp1 - qty_tp2
    if qty_tp1 < min_qty or qty_tp2 < min_qty or qty_tp3 < min_qty:
        log(f"ERR: split too small (tp1={qty_tp1}, tp2={qty_tp2}, tp3={qty_tp3})")
        return

    sl_p = quantize_price(SL_PRICE, tick_size)
    tp1_p = quantize_price(TP1_PRICE, tick_size)
    tp2_p = quantize_price(TP2_PRICE, tick_size)
    tp3_p = quantize_price(TP3_PRICE, tick_size)

    sl_dist = (sl_p / last - 1) * 100
    tp1_dist = (tp1_p / last - 1) * 100
    tp2_dist = (tp2_p / last - 1) * 100
    tp3_dist = (tp3_p / last - 1) * 100
    risk = notional * abs(sl_dist) / 100
    rew_tp1 = notional * TP1_FRAC * abs(tp1_dist) / 100
    rew_tp2 = notional * TP2_FRAC * abs(tp2_dist) / 100
    rew_tp3 = notional * (Decimal("1") - TP1_FRAC - TP2_FRAC) * abs(tp3_dist) / 100

    log("=" * 60)
    log("PLANNED TRADE")
    log("=" * 60)
    log(f"  {SYMBOL} {SIDE_OPEN} (LONG) {qty} contracts at {actual_lev}x")
    log(f"  Margin:    ${MARGIN_USDT}")
    log(f"  Notional:  ${notional}")
    log(f"  Entry:     MARKET (~${last:.6f})")
    log(f"  SL:        ${sl_p}  ({sl_dist:+.2f}%)  closes all")
    log(f"  TP1 33%:   ${tp1_p}  ({tp1_dist:+.2f}%)  qty={qty_tp1}")
    log(f"  TP2 33%:   ${tp2_p}  ({tp2_dist:+.2f}%)  qty={qty_tp2}")
    log(f"  TP3 rest:  ${tp3_p}  ({tp3_dist:+.2f}%)  qty={qty_tp3}")
    log(f"  Risk if SL:    ${risk:.2f}  ({risk/avail*100:.0f}% wallet)")
    log(f"  Reward TP1:    ${rew_tp1:.2f}")
    log(f"  Reward TP2:    ${rew_tp2:.2f}")
    log(f"  Reward TP3:    ${rew_tp3:.2f}")
    log(f"  Total potential: ${rew_tp1+rew_tp2+rew_tp3:.2f}")
    log("=" * 60)

    confirm = input("Type YES to fire LONG, anything else aborts: ").strip().upper()
    if confirm != "YES":
        log("Aborted.")
        return

    try:
        client.set_margin_type(SYMBOL, "ISOLATED")
        log("ISOLATED margin set")
    except BinanceApiError as e:
        if "4046" in str(e):
            log("Already isolated")
        elif "4168" in str(e):
            log("Multi-Assets mode -> using CROSS")
        else:
            log(f"margin warn: {e}")

    log("Placing MARKET LONG...")
    try:
        entry = client.place_market_order(SYMBOL, SIDE_OPEN, fmt(qty))
        log(f"  ENTRY: orderId={entry.get('orderId')} avgPrice={entry.get('avgPrice')} qty={entry.get('executedQty')}")
    except BinanceApiError as e:
        log(f"ENTRY FAILED: {e}")
        return

    log("Placing SL stop-market (close all)...")
    try:
        sl = client.place_stop_market_order(SYMBOL, SIDE_CLOSE, fmt(sl_p), close_position=True)
        log(f"  SL placed algoId={sl.get('algoId') or sl.get('orderId')}")
    except BinanceApiError as e:
        log(f"SL FAILED: {e} -- POSITION NAKED, manage manually!")
        return

    log("Placing TP1 limit BUY (33%, reduce-only, GTC)...")
    try:
        tp1 = client.place_limit_order(SYMBOL, SIDE_CLOSE, fmt(tp1_p), fmt(qty_tp1), time_in_force="GTC", reduce_only=True)
        log(f"  TP1: {tp1.get('orderId')}")
    except BinanceApiError as e:
        log(f"TP1 FAILED: {e}")

    log("Placing TP2 limit (33%, reduce-only, GTC)...")
    try:
        tp2 = client.place_limit_order(SYMBOL, SIDE_CLOSE, fmt(tp2_p), fmt(qty_tp2), time_in_force="GTC", reduce_only=True)
        log(f"  TP2: {tp2.get('orderId')}")
    except BinanceApiError as e:
        log(f"TP2 FAILED: {e}")

    log("Placing TP3 limit (rest, reduce-only, GTC)...")
    try:
        tp3 = client.place_limit_order(SYMBOL, SIDE_CLOSE, fmt(tp3_p), fmt(qty_tp3), time_in_force="GTC", reduce_only=True)
        log(f"  TP3: {tp3.get('orderId')}")
    except BinanceApiError as e:
        log(f"TP3 FAILED: {e}")

    log("=" * 60)
    log("ALL ORDERS SUBMITTED")
    log(f"  Position: LONG {qty} {SYMBOL} @ market")
    log(f"  SL ${sl_p}  TP1 ${tp1_p} ({qty_tp1})  TP2 ${tp2_p} ({qty_tp2})  TP3 ${tp3_p} ({qty_tp3})")
    log("Watch from Binance UI. Don't touch.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted.")
