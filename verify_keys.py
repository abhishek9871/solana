"""Verify which keys load_credentials_from_env actually loads."""
import os
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()

prod_key = os.environ.get("BINANCE_API_KEY", "").strip()
prod_sec = os.environ.get("BINANCE_SECRET_KEY", "").strip()
test_key = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
test_sec = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()

print("Loaded environment keys:\n")
print(f"BINANCE_API_KEY (prod):         {'set' if prod_key else 'EMPTY'}, length={len(prod_key)}, starts={prod_key[:10] if prod_key else '-'}")
print(f"BINANCE_SECRET_KEY (prod):      {'set' if prod_sec else 'EMPTY'}, length={len(prod_sec)}, starts={prod_sec[:10] if prod_sec else '-'}")
print(f"BINANCE_TESTNET_API_KEY:        {'set' if test_key else 'EMPTY'}, length={len(test_key)}, starts={test_key[:10] if test_key else '-'}")
print(f"BINANCE_TESTNET_SECRET_KEY:     {'set' if test_sec else 'EMPTY'}, length={len(test_sec)}, starts={test_sec[:10] if test_sec else '-'}")

print("\nSame as testnet?", prod_key == test_key)
