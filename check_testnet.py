"""Check actual testnet wallet state and recent trades."""
import os
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

bals = c.get_balance()
usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
print(f"USDT wallet:    {usdt.get('balance')}")
print(f"USDT available: {usdt.get('availableBalance')}")
print(f"unrealized PnL: {usdt.get('crossUnPnl')}")

positions = c.get_positions()
open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
print(f"\nOpen positions: {len(open_pos)}")
for p in open_pos:
    print(f"  {p['symbol']:12} amt={p['positionAmt']:>15} entry={p['entryPrice']:>10} mark={p['markPrice']:>10} unPnL={p['unRealizedProfit']}")

orders = c.get_open_orders()
print(f"\nOpen regular orders: {len(orders)}")

try:
    algos = c.get_open_algo_orders()
    al = algos if isinstance(algos, list) else algos.get("orders", [])
    print(f"Open algo orders: {len(al)}")
    for a in al:
        print(f"  {a.get('symbol'):12} {a.get('side'):4} {a.get('algoType'):20} trigger={a.get('triggerPrice')}")
except Exception as e:
    print(f"Algo orders fetch: {e}")
