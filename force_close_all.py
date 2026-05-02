"""
Force-close every open position and cancel every order on testnet.
Use to clean up after orphan-position bug.
"""
import argparse
import os
import sys

from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true", help="Use prod (real money). Default: testnet.")
    args = ap.parse_args()

    load_credentials_from_env()
    if args.prod:
        api = os.environ.get("BINANCE_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
        base = "https://fapi.binance.com"
        mode = "PROD"
    else:
        api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
        secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
        base = "https://testnet.binancefuture.com"
        mode = "TESTNET"
    if not api or not secret:
        print(f"ERROR: keys missing for {mode}")
        return 2

    c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url=base)
    print(f"=== force_close_all on {mode} ===")

    # 1. Cancel ALL open orders, all symbols
    try:
        opens = c.get_open_orders()
        symbols_with_orders = sorted({o["symbol"] for o in opens})
        for sym in symbols_with_orders:
            try:
                c.cancel_all_orders(sym)
                print(f"cancelled regular orders for {sym}")
            except BinanceApiError as e:
                print(f"cancel regular orders {sym} failed: {e}")
            try:
                c.cancel_all_algo_orders(sym)
                print(f"cancelled algo orders for {sym}")
            except BinanceApiError as e:
                print(f"cancel algo orders {sym} failed: {e}")
        if not symbols_with_orders:
            print("no open orders")
    except BinanceApiError as e:
        print(f"open orders fetch failed: {e}")

    # 2. Close every open position
    try:
        positions = c.get_positions()
    except BinanceApiError as e:
        print(f"positions fetch failed: {e}")
        return 1
    open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
    print(f"found {len(open_pos)} open positions")
    for p in open_pos:
        sym = p["symbol"]
        amt = float(p["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"  # opposite to flatten
        qty = abs(amt)
        # Format qty per step size
        try:
            info = c.get_symbol_info(sym)
            step = "1"
            for f in info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step = f["stepSize"]
            from decimal import Decimal, ROUND_DOWN
            step_d = Decimal(step)
            qty_q = (Decimal(str(qty)) / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
            qty_str = format(qty_q.normalize(), "f")
        except Exception as e:
            qty_str = str(qty)
            print(f"qty fmt fallback for {sym}: {e}")
        # Robust close: market → LIMIT IOC at safe price → GTX LIMIT at cap
        from decimal import ROUND_HALF_UP
        try:
            c.place_market_order(sym, side, quantity=qty_str, reduce_only=True)
            print(f"closed {sym} {side} qty={qty_str} via MARKET")
            continue
        except BinanceApiError as e:
            if "-4131" not in str(e):
                print(f"close {sym} failed: {e}")
                continue
            print(f"  {sym} market hit PERCENT_PRICE -- trying LIMIT IOC")
        # LIMIT IOC at mark ± 4%
        try:
            pi = c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
            mark = Decimal(pi["markPrice"])
            try:
                tick_d = Decimal(c.get_symbol_info(sym)["filters"][0]["tickSize"])
            except Exception:
                tick_d = Decimal("0.0001")
            for f in c.get_symbol_info(sym).get("filters", []):
                if f["filterType"] == "PRICE_FILTER":
                    tick_d = Decimal(f["tickSize"])
                    break
            if side == "BUY":
                px = (mark * Decimal("1.04") / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
            else:
                px = (mark * Decimal("0.96") / tick_d).to_integral_value(rounding=ROUND_HALF_UP) * tick_d
            px_str = format(px.normalize(), "f") if px != px.to_integral() else format(px, "f")
            r = c.place_limit_order(sym, side, price=px_str, quantity=qty_str,
                                    time_in_force="IOC", reduce_only=True)
            print(f"  IOC at {px_str}: status={r.get('status')} executed={r.get('executedQty')}")
            if float(r.get("executedQty", 0)) > 0:
                continue
        except BinanceApiError as e:
            print(f"  IOC failed: {e}")
        # GTX LIMIT at cap
        try:
            if side == "BUY":
                px = (mark * Decimal("1.04") / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
            else:
                px = (mark * Decimal("0.96") / tick_d).to_integral_value(rounding=ROUND_HALF_UP) * tick_d
            px_str = format(px.normalize(), "f") if px != px.to_integral() else format(px, "f")
            r = c.place_limit_order(sym, side, price=px_str, quantity=qty_str,
                                    time_in_force="GTX", reduce_only=True)
            print(f"  GTX at {px_str}: orderId={r.get('orderId')} -- sitting in book")
        except BinanceApiError as e:
            print(f"  GTX failed: {e}")

    # Final check
    try:
        positions2 = c.get_positions()
        still_open = [p for p in positions2 if float(p.get("positionAmt", 0)) != 0]
        print(f"after close: {len(still_open)} positions still open")
        for p in still_open:
            print(f"  STILL OPEN: {p['symbol']} amt={p['positionAmt']}")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
