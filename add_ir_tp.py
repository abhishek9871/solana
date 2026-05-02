"""Re-add TP on IRUSDT short at +5% favorable (mark dropped, so TP needs to be at $0.01962)."""
import os
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

SYMBOL = "IRUSDT"
TP_PRICE = Decimal("0.01962")  # original TP target

positions = c.get_positions(SYMBOL)
amt = 0.0
for p in positions:
    amt = float(p.get("positionAmt", 0))
    break
qty = abs(amt)
close_side = "BUY" if amt < 0 else "SELL"

info = c.get_symbol_info(SYMBOL)
tick = step = Decimal("0")
for f in info.get("filters", []):
    if f["filterType"] == "PRICE_FILTER":
        tick = Decimal(f["tickSize"])
    elif f["filterType"] == "LOT_SIZE":
        step = Decimal(f["stepSize"])

qty_q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
qty_str = format(qty_q.normalize(), "f") if qty_q != qty_q.to_integral() else format(qty_q, "f")
tp_q = (TP_PRICE / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
tp_str = format(tp_q.normalize(), "f") if tp_q != tp_q.to_integral() else format(tp_q, "f")

print(f"Placing TP: {close_side} TAKE_PROFIT_MARKET at {tp_str}, qty {qty_str}")
try:
    r = c.place_take_profit_order(SYMBOL, close_side, stop_price=tp_str, quantity=qty_str,
                                    close_position=False, reduce_only=True)
    print(f"TP placed: algoId={r.get('algoId') or r.get('orderId')}")
except BinanceApiError as e:
    print(f"TP FAILED: {e}")

# Show all algos
algos = c.get_open_algo_orders(SYMBOL)
algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
print(f"\nAll IRUSDT algos: {len(algo_list)}")
for a in algo_list:
    print(f"  side={a.get('side')} type={a.get('algoType')} trigger={a.get('triggerPrice')}")
