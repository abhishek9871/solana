"""Quick account inventory before launching ORCA bot."""
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

api_key, secret = load_credentials_from_env()
if not api_key or not secret:
    print("ERROR: credentials missing")
    raise SystemExit(2)

c = BinanceFuturesClient(api_key=api_key, secret_key=secret)

# Balance
bals = c.get_balance()
usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
if usdt:
    print(f"USDT wallet: ${float(usdt.get('balance', 0)):.4f}  available: ${float(usdt.get('availableBalance', 0)):.4f}")

# Open positions (any symbol)
positions = c.get_positions()
open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
print(f"Open positions: {len(open_pos)}")
for p in open_pos:
    print(f"  {p['symbol']}  qty={p['positionAmt']}  entry={p['entryPrice']}  unrPnL={p['unRealizedProfit']}")

# Open regular orders
orders = c.get_open_orders()
print(f"Open regular orders: {len(orders)}")
for o in orders:
    print(f"  {o['symbol']}  {o['side']} {o['type']}  qty={o['origQty']}  price={o['price']}")

# Open algo orders
try:
    algos = c.get_open_algo_orders()
    algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
    print(f"Open algo orders: {len(algo_list)}")
    for a in algo_list:
        print(f"  {a.get('symbol')}  {a.get('side')} {a.get('algoType')}  trigger={a.get('triggerPrice')}")
except Exception as e:
    print(f"Algo orders fetch: {e}")
