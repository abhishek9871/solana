"""Check ZEREBRO state in detail to see if it's dipping (short opportunity)."""
import os
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# Account
bals = c.get_balance()
usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
print(f"USDT wallet: ${float(usdt.get('balance',0)):.4f}")
print(f"Available: ${float(usdt.get('availableBalance',0)):.4f}\n")

positions = c.get_positions()
open_pos = [p for p in positions if float(p.get("positionAmt", 0)) != 0]
print(f"Open positions: {len(open_pos)}")
for p in open_pos:
    print(f"  {p['symbol']}: amt={p['positionAmt']}, entry={p['entryPrice']}, unPnL={p['unRealizedProfit']}")
print()

# ZEREBROUSDT detailed
sym = "ZEREBROUSDT"
print(f"=== {sym} DETAIL ===")
t = c.public_get("/fapi/v1/ticker/24hr", {"symbol": sym})
print(f"24h change:    {float(t['priceChangePercent']):+.2f}%")
print(f"24h high:      ${float(t['highPrice']):.6f}")
print(f"24h low:       ${float(t['lowPrice']):.6f}")
print(f"Last price:    ${float(t['lastPrice']):.6f}")
print(f"24h volume:    ${float(t['quoteVolume'])/1e6:.1f}M")

p = c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
fund = float(p.get("lastFundingRate", "0")) * 100
print(f"Funding raw:   {fund:+.4f}% per period")

bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
print(f"Bid/Ask:       {bt['bidPrice']} / {bt['askPrice']}")

# Multi-timeframe
print("\nRecent price action:")
for tf, label in [("1m", "1min"), ("5m", "5min"), ("15m", "15min"), ("1h", "1h")]:
    k = c.get_klines(sym, tf, limit=4)
    if len(k) >= 4:
        # Last 3 closed candles
        c_arr = [float(b[4]) for b in k[:-1]]
        oldest = c_arr[0]
        latest = c_arr[-1]
        chg = (latest/oldest - 1) * 100
        print(f"  {label} (last 3 closed): {chg:+.2f}%  [{c_arr[0]:.6f} -> {c_arr[-1]:.6f}]")

# 1h candle direction (is it cooling?)
print("\nLast 6 × 5min candles:")
k5 = c.get_klines(sym, "5m", limit=7)
for b in k5[:-1]:
    o = float(b[1]); cl = float(b[4]); h = float(b[2]); l = float(b[3])
    chg = (cl/o - 1) * 100
    direction = "UP" if cl > o else "DOWN"
    print(f"  open=${o:.6f} close=${cl:.6f} ({chg:+.2f}% {direction})")
