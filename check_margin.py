"""Detailed margin and balance check."""
import os
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")

# Account-level info
account = c.get_account()

print("=== ACCOUNT BALANCE OVERVIEW ===\n")
print(f"Total Wallet Balance:      ${float(account.get('totalWalletBalance', 0)):>12.4f}")
print(f"Total Unrealized PnL:      ${float(account.get('totalUnrealizedProfit', 0)):>12.4f}")
print(f"Total Margin Balance:      ${float(account.get('totalMarginBalance', 0)):>12.4f}  (wallet + unrealized)")
print(f"Total Initial Margin:      ${float(account.get('totalInitialMargin', 0)):>12.4f}  (margin tied up by positions)")
print(f"Total Maintenance Margin:  ${float(account.get('totalMaintMargin', 0)):>12.4f}  (must stay below margin balance)")
print(f"Available Balance:         ${float(account.get('availableBalance', 0)):>12.4f}  (free to open new positions)")
print(f"Total Cross Wallet Balance:${float(account.get('totalCrossWalletBalance', 0)):>12.4f}")

print("\n=== USDT ASSET ===")
for asset in account.get("assets", []):
    if asset.get("asset") == "USDT":
        print(f"  Wallet Balance:          ${float(asset.get('walletBalance', 0)):>12.4f}")
        print(f"  Unrealized Profit:       ${float(asset.get('unrealizedProfit', 0)):>12.4f}")
        print(f"  Margin Balance:          ${float(asset.get('marginBalance', 0)):>12.4f}")
        print(f"  Initial Margin:          ${float(asset.get('initialMargin', 0)):>12.4f}")
        print(f"  Maintenance Margin:      ${float(asset.get('maintMargin', 0)):>12.4f}")
        print(f"  Available Balance:       ${float(asset.get('availableBalance', 0)):>12.4f}")

print("\n=== OPEN POSITIONS ===")
positions = c.get_positions()
open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
total_initial = 0.0
total_unpnl = 0.0
for p in open_pos:
    init_margin = float(p.get("initialMargin", 0))
    unpnl = float(p.get("unRealizedProfit", 0))
    total_initial += init_margin
    total_unpnl += unpnl
    print(f"  {p['symbol']:14}  size=${float(p.get('notional', 0)):>10.2f}  margin=${init_margin:>7.2f}  unPnL=${unpnl:+8.4f}  lev={p.get('leverage')}x")

print(f"\nTotal initial margin (positions): ${total_initial:.4f}")
print(f"Total unrealized PnL:             ${total_unpnl:+.4f}")
