"""Close stuck positions using LIMIT IOC at wide prices (bypasses PERCENT_PRICE)."""
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")


def quantize_price(value, tick):
    if tick <= 0:
        return value
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def quantize_down(value, step):
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


positions = c.get_positions()
open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
print(f"Found {len(open_pos)} stuck positions")

for p in open_pos:
    sym = p["symbol"]
    amt = float(p["positionAmt"])
    side = "SELL" if amt > 0 else "BUY"  # opposite to flatten
    qty = abs(amt)
    print(f"\n--- {sym}: amt={amt:+}, closing with {side} {qty} ---")

    info = c.get_symbol_info(sym)
    tick = step = Decimal("0")
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = Decimal(f["tickSize"])
        elif f["filterType"] == "LOT_SIZE":
            step = Decimal(f["stepSize"])

    bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
    bid = Decimal(bt["bidPrice"])
    ask = Decimal(bt["askPrice"])

    # Use a price that crosses the book (taker fill) but is bounded
    # For BUY (closing short): use ask × 1.005 (0.5% above ask)
    # For SELL (closing long): use bid × 0.995 (0.5% below bid)
    if side == "BUY":
        limit_px = quantize_price(ask * Decimal("1.005"), tick)
    else:
        limit_px = quantize_price(bid * Decimal("0.995"), tick)

    qty_q = quantize_down(Decimal(str(qty)), step)
    qty_str = format(qty_q.normalize(), "f") if qty_q != qty_q.to_integral() else format(qty_q, "f")
    px_str = format(limit_px.normalize(), "f") if limit_px != limit_px.to_integral() else format(limit_px, "f")

    print(f"  bid={bid} ask={ask} limit={px_str} qty={qty_str}")
    try:
        r = c.place_limit_order(sym, side, price=px_str, quantity=qty_str,
                                time_in_force="IOC", reduce_only=True)
        status = r.get("status")
        executed = r.get("executedQty", "0")
        print(f"  LIMIT IOC placed, status={status}, executedQty={executed}")
    except BinanceApiError as e:
        print(f"  LIMIT IOC failed: {e}")
        # Try wider
        if side == "BUY":
            limit_px = quantize_price(ask * Decimal("1.05"), tick)
        else:
            limit_px = quantize_price(bid * Decimal("0.95"), tick)
        px_str = format(limit_px.normalize(), "f") if limit_px != limit_px.to_integral() else format(limit_px, "f")
        print(f"  retry wider: limit={px_str}")
        try:
            r = c.place_limit_order(sym, side, price=px_str, quantity=qty_str,
                                    time_in_force="IOC", reduce_only=True)
            status = r.get("status")
            executed = r.get("executedQty", "0")
            print(f"  LIMIT IOC (wide) placed, status={status}, executedQty={executed}")
        except BinanceApiError as e2:
            print(f"  WIDE LIMIT IOC failed: {e2}")

print("\n=== Final state ===")
positions2 = c.get_positions()
still_open = [p for p in positions2 if float(p.get("positionAmt", 0)) != 0]
print(f"{len(still_open)} positions still open")
for p in still_open:
    print(f"  {p['symbol']}: amt={p['positionAmt']}, mark={p['markPrice']}, unPnL={p['unRealizedProfit']}")
