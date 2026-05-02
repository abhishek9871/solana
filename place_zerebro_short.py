"""
ZEREBROUSDT SHORT — top-of-pump breakdown play.

Setup at scan time:
  - 24h +13.40% (heavily pumped)
  - Just printed -1.91% on 5m with vol 9.9M (huge sell candle)
  - 1m showing continuation (last 4: GR, RD, RD, RD)
  - High of $0.029814 made earlier, now $0.0285 (-4.4% off high)

Aggressive sizing: $5 margin x 50x = $250 notional. Risk ~$4.5, max reward ~$14 if TP3 hits.
"""
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

SYMBOL = "ZEREBROUSDT"
SIDE_OPEN = "SELL"          # short
SIDE_CLOSE = "BUY"
LEVERAGE = 50
MARGIN_USDT = Decimal("5.00")
SL_PRICE = Decimal("0.0294")    # above local high $0.0292 + buffer
TP1_PRICE = Decimal("0.0275")   # -3.5%
TP2_PRICE = Decimal("0.0260")   # -8.8%
TP3_PRICE = Decimal("0.0245")   # -14.0%
TP1_FRAC = Decimal("0.33")
TP2_FRAC = Decimal("0.33")

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
        log(f"ERR: need ${MARGIN_USDT}, have ${avail}")
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

    actual_lev = LEVERAGE
    try:
        client.set_leverage(SYMBOL, LEVERAGE)
        log(f"Leverage {LEVERAGE}x set")
    except BinanceApiError as e:
        log(f"  {LEVERAGE}x rejected: {e}")
        for try_lev in [25, 20, 10]:
            try:
                client.set_leverage(SYMBOL, try_lev)
                actual_lev = try_lev
                log(f"  Fell back to {try_lev}x")
                break
            except BinanceApiError:
                continue
        else:
            log("ERR: no leverage set")
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
        log(f"ERR: split too small")
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
    log(f"  {SYMBOL} {SIDE_OPEN} (SHORT) {qty} contracts at {actual_lev}x")
    log(f"  Margin: ${MARGIN_USDT}  Notional: ${notional}  Entry: ~${last:.6f}")
    log(f"  SL:    ${sl_p}  ({sl_dist:+.2f}%)  closes all")
    log(f"  TP1 33%:  ${tp1_p}  ({tp1_dist:+.2f}%)  qty={qty_tp1}")
    log(f"  TP2 33%:  ${tp2_p}  ({tp2_dist:+.2f}%)  qty={qty_tp2}")
    log(f"  TP3 rest: ${tp3_p}  ({tp3_dist:+.2f}%)  qty={qty_tp3}")
    log(f"  Risk:    ${risk:.2f}  ({risk/avail*100:.0f}% wallet)")
    log(f"  Reward TP1/TP2/TP3: ${rew_tp1:.2f} / ${rew_tp2:.2f} / ${rew_tp3:.2f}")
    log(f"  Total potential: ${rew_tp1+rew_tp2+rew_tp3:.2f}")
    log("=" * 60)

    confirm = input("Type YES to fire SHORT, anything else aborts: ").strip().upper()
    if confirm != "YES":
        log("Aborted.")
        return

    try:
        client.set_margin_type(SYMBOL, "ISOLATED")
        log("ISOLATED set")
    except BinanceApiError as e:
        if "4046" in str(e): log("Already isolated")
        elif "4168" in str(e): log("Multi-Assets -> CROSS")
        else: log(f"margin warn: {e}")

    log("Placing MARKET SHORT...")
    try:
        entry = client.place_market_order(SYMBOL, SIDE_OPEN, fmt(qty))
        log(f"  ENTRY orderId={entry.get('orderId')} avgPrice={entry.get('avgPrice')} qty={entry.get('executedQty')}")
    except BinanceApiError as e:
        log(f"ENTRY FAILED: {e}")
        return

    log("Placing SL stop-market (close all)...")
    try:
        sl = client.place_stop_market_order(SYMBOL, SIDE_CLOSE, fmt(sl_p), close_position=True)
        log(f"  SL algoId={sl.get('algoId') or sl.get('orderId')}")
    except BinanceApiError as e:
        log(f"SL FAILED: {e} -- POSITION NAKED!")
        return

    for label, price, qty_part in [("TP1", tp1_p, qty_tp1), ("TP2", tp2_p, qty_tp2), ("TP3", tp3_p, qty_tp3)]:
        log(f"Placing {label} limit BUY {qty_part}@${price} reduce-only...")
        try:
            o = client.place_limit_order(SYMBOL, SIDE_CLOSE, fmt(price), fmt(qty_part), time_in_force="GTC", reduce_only=True)
            log(f"  {label} orderId={o.get('orderId')}")
        except BinanceApiError as e:
            log(f"  {label} FAILED: {e}")

    log("=" * 60)
    log("ALL ORDERS SUBMITTED — watch from Binance UI, don't touch.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted.")
