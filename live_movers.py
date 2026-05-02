"""
LIVE MOVERS scanner — finds coins ACTIVELY moving fast RIGHT NOW.
Sorts by absolute 1m velocity, confirms with 5m direction, filters by liquidity.
"""
import os
from decimal import Decimal
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# Pull all tickers
tickers = c.get_24hr_tickers()
prems = c.public_get("/fapi/v1/premiumIndex", {})
prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

# Filter to liquid USDT pairs
candidates = [t for t in tickers if t.get("symbol", "").endswith("USDT")
              and float(t.get("quoteVolume", 0)) > 30_000_000]

print(f"Scanning {len(candidates)} pairs for ACTIVE velocity...\n")

movers = []
for t in candidates:
    sym = t["symbol"]
    try:
        # Get last 2 1m candles
        k1 = c.get_klines(sym, "1m", limit=2)
        if len(k1) < 2:
            continue
        prev_close = float(k1[-2][4])
        curr_close = float(k1[-1][4])
        if prev_close <= 0:
            continue
        vel_1m = (curr_close / prev_close - 1) * 100

        # Get 5m for trend confirmation
        k5 = c.get_klines(sym, "5m", limit=2)
        if len(k5) < 2:
            continue
        prev_5m = float(k5[-2][4])
        curr_5m = float(k5[-1][4])
        vel_5m = (curr_5m / prev_5m - 1) * 100 if prev_5m > 0 else 0

        # Spread
        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(bt["bidPrice"])
        ask = float(bt["askPrice"])
        mid = (bid + ask) / 2 if bid + ask > 0 else 0
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 99

        if spread_pct > 0.15:
            continue

        # Funding context
        fund = 0.0
        p = prem_map.get(sym)
        if p:
            fund = float(p.get("lastFundingRate", "0")) * 100

        chg_24h = float(t["priceChangePercent"])

        movers.append({
            "sym": sym,
            "vel_1m": vel_1m,
            "vel_5m": vel_5m,
            "fund": fund,
            "chg_24h": chg_24h,
            "spread": spread_pct,
            "vol": float(t["quoteVolume"]),
            "mark": mid,
            "abs_vel": abs(vel_1m),
        })
    except Exception:
        continue

# Sort by absolute 1m velocity (pure speed)
movers.sort(key=lambda x: x["abs_vel"], reverse=True)

print(f"{'rank':<5}{'symbol':<14}{'1m%':>8}{'5m%':>8}{'24h%':>9}{'fund%':>9}{'spread%':>9}{'vol_M$':>10}")
print("-" * 90)
for i, m in enumerate(movers[:15]):
    line = (f"{i+1:<5}{m['sym']:<14}{m['vel_1m']:>+8.3f}{m['vel_5m']:>+8.3f}"
            f"{m['chg_24h']:>+9.2f}{m['fund']:>+9.4f}{m['spread']:>+9.4f}{m['vol']/1e6:>9.1f}M")
    print(line.encode("ascii", "replace").decode("ascii"))

if movers:
    top = movers[0]
    direction = "BUY" if top["vel_1m"] > 0 else "SELL"
    aligned_5m = (top["vel_1m"] > 0 and top["vel_5m"] > 0) or (top["vel_1m"] < 0 and top["vel_5m"] < 0)
    print(f"\n=== TOP ACTIVE MOVER: {top['sym']} ===")
    print(f"  Direction:    {direction} (ride the move)")
    print(f"  1m velocity:  {top['vel_1m']:+.3f}%  (active {'pump' if top['vel_1m']>0 else 'dump'})")
    print(f"  5m velocity:  {top['vel_5m']:+.3f}%  ({'aligned' if aligned_5m else 'DIVERGENT - caution'})")
    print(f"  24h:          {top['chg_24h']:+.2f}%")
    print(f"  Funding:      {top['fund']:+.4f}%")
    print(f"  Spread:       {top['spread']:+.4f}%")
    print(f"  Mark:         ${top['mark']:.6f}")
    print(f"  Volume:       ${top['vol']/1e6:.1f}M")
