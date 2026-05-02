"""Check if SWARMS GTC order is still in book."""
import os
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

# Check open orders for SWARMS specifically
opens = c.get_open_orders("SWARMSUSDT")
print(f"Open orders on SWARMSUSDT: {len(opens)}")
for o in opens:
    print(f"  orderId={o.get('orderId')} side={o.get('side')} type={o.get('type')} "
          f"price={o.get('price')} qty={o.get('origQty')} status={o.get('status')}")

# Check the specific orderId from earlier
try:
    r = c.query_order("SWARMSUSDT", order_id=137670341)
    print(f"\nQuery orderId 137670341:")
    print(f"  status: {r.get('status')}")
    print(f"  executedQty: {r.get('executedQty')}")
    print(f"  origQty: {r.get('origQty')}")
    print(f"  price: {r.get('price')}")
except Exception as e:
    print(f"Query failed: {e}")
