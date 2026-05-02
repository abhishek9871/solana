"""Pull actual trade history and income from testnet to see what really happened."""
import os
import time
from collections import defaultdict
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

# Pull last 24h income (realized PnL events)
since = int((time.time() - 24*3600) * 1000)
try:
    income = c.signed_request("GET", "/fapi/v1/income", {"startTime": since, "limit": 1000})
except Exception as e:
    print(f"income fetch err: {e}")
    income = []

print(f"=== Last 24h income events ({len(income)} total) ===\n")
if not income:
    print("(no income events)")

# Group by symbol and type
by_sym_type = defaultdict(lambda: {"count": 0, "total": 0.0})
for inc in income:
    sym = inc.get("symbol", "?")
    typ = inc.get("incomeType", "?")
    val = float(inc.get("income", 0))
    by_sym_type[(sym, typ)]["count"] += 1
    by_sym_type[(sym, typ)]["total"] += val

# Print summary by type
type_totals = defaultdict(float)
for (sym, typ), info in by_sym_type.items():
    type_totals[typ] += info["total"]

print("Summary by income type:")
for typ, total in sorted(type_totals.items(), key=lambda x: x[1]):
    print(f"  {typ:25} ${total:+10.4f}")
print(f"  {'GRAND TOTAL':25} ${sum(type_totals.values()):+10.4f}")

# Print top 30 individual events sorted by absolute value
print("\nTop 30 events by abs value:")
sorted_inc = sorted(income, key=lambda x: abs(float(x.get("income", 0))), reverse=True)[:30]
for inc in sorted_inc:
    ts = time.strftime("%H:%M:%S", time.localtime(int(inc["time"])/1000))
    print(f"  {ts}  {inc.get('symbol','?'):14}  {inc.get('incomeType','?'):20}  ${float(inc.get('income',0)):+10.4f}")

# Per-symbol realized PnL summary (REALIZED_PNL only)
print("\nPer-symbol realized PnL:")
sym_pnl = defaultdict(lambda: {"count": 0, "total": 0.0})
for inc in income:
    if inc.get("incomeType") == "REALIZED_PNL":
        sym = inc.get("symbol", "?")
        sym_pnl[sym]["count"] += 1
        sym_pnl[sym]["total"] += float(inc.get("income", 0))
for sym, info in sorted(sym_pnl.items(), key=lambda x: x[1]["total"], reverse=True):
    print(f"  {sym:14}  trades={info['count']:3}  pnl=${info['total']:+10.4f}")
