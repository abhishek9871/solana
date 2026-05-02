"""
WIDE NET scanner — looks at ALL 500+ tradeable USDT perps.
Finds the hidden gold by scanning multiple dimensions:
  1. EXTREME 24h movers (>50% up or <-30%)
  2. EXTREME funding rates (>+0.20% or <-0.20%)
  3. Open Interest acceleration (OI growing fast)
  4. Coins just broke a major level (24h high/low cross)
  5. Volume explosion (today's vol > 7d avg massively)

A coin showing up in MULTIPLE dimensions = high conviction GOLD.
"""
import os
import time
from collections import defaultdict
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# Get exchangeInfo, filter to non-TradFi perps only
einfo = c.public_get("/fapi/v1/exchangeInfo", {})
tradeable = set()
for s in einfo.get("symbols", []):
    if (s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("marginAsset") == "USDT"
            and "TradFi" not in (s.get("underlyingSubType") or [])):
        tradeable.add(s["symbol"])

# Pull all tickers and funding rates in one shot
tickers = c.get_24hr_tickers()
prems = c.public_get("/fapi/v1/premiumIndex", {})
prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

# Filter and sort
all_data = []
for t in tickers:
    sym = t.get("symbol", "")
    if sym not in tradeable:
        continue
    vol = float(t.get("quoteVolume", 0))
    if vol < 5_000_000:  # very low minimum to catch small-cap movers
        continue
    chg_24h = float(t.get("priceChangePercent", 0))
    fund = 0.0
    p = prem_map.get(sym)
    if p:
        fund = float(p.get("lastFundingRate", "0")) * 100
    high = float(t["highPrice"])
    low = float(t["lowPrice"])
    last = float(t["lastPrice"])
    rng_pos = (last - low) / (high - low) if high > low else 0.5

    all_data.append({
        "sym": sym,
        "chg_24h": chg_24h,
        "fund": fund,
        "vol": vol,
        "last": last,
        "rng_pos": rng_pos,
        "high": high, "low": low,
    })

print(f"Scanning {len(all_data)} non-TradFi perps with vol > $5M...\n")

# Tag each coin in dimensions of interest
flagged = defaultdict(list)  # sym -> list of (dimension, score, details)

# Dim 1: EXTREME 24h movers
extreme_up = sorted([d for d in all_data if d["chg_24h"] >= 30], key=lambda x: -x["chg_24h"])
extreme_dn = sorted([d for d in all_data if d["chg_24h"] <= -25], key=lambda x: x["chg_24h"])
print(f"=== EXTREME 24h GAINERS (>30%) ===")
print(f"{'sym':<14}{'24h%':>8}{'fund%':>9}{'vol_M$':>9}{'rng_pos':>9}")
for d in extreme_up[:12]:
    print(f"{d['sym']:<14}{d['chg_24h']:>+8.1f}{d['fund']:>+9.4f}{d['vol']/1e6:>8.0f}M{d['rng_pos']:>9.2f}")
    flagged[d["sym"]].append(("EXTREME_GAIN", 30 + min(d["chg_24h"]/2, 30), f"+{d['chg_24h']:.0f}% 24h"))

print(f"\n=== EXTREME 24h LOSERS (<-25%) ===")
print(f"{'sym':<14}{'24h%':>8}{'fund%':>9}{'vol_M$':>9}{'rng_pos':>9}")
for d in extreme_dn[:12]:
    print(f"{d['sym']:<14}{d['chg_24h']:>+8.1f}{d['fund']:>+9.4f}{d['vol']/1e6:>8.0f}M{d['rng_pos']:>9.2f}")
    flagged[d["sym"]].append(("EXTREME_DROP", 30 + min(abs(d["chg_24h"])/2, 30), f"{d['chg_24h']:.0f}% 24h"))

# Dim 2: EXTREME funding (squeeze fuel)
fund_neg = sorted([d for d in all_data if d["fund"] <= -0.10], key=lambda x: x["fund"])[:10]
fund_pos = sorted([d for d in all_data if d["fund"] >= 0.10], key=lambda x: -x["fund"])[:10]
print(f"\n=== EXTREME NEGATIVE FUNDING (shorts CROWDED, squeeze fuel) ===")
print(f"{'sym':<14}{'fund%':>9}{'24h%':>8}{'vol_M$':>9}")
for d in fund_neg:
    print(f"{d['sym']:<14}{d['fund']:>+9.4f}{d['chg_24h']:>+8.1f}{d['vol']/1e6:>8.0f}M")
    flagged[d["sym"]].append(("NEG_FUND_EXT", 35 + min(abs(d["fund"])*100, 30), f"fund {d['fund']:.3f}%"))

print(f"\n=== EXTREME POSITIVE FUNDING (longs TRAPPED, dump fuel) ===")
print(f"{'sym':<14}{'fund%':>9}{'24h%':>8}{'vol_M$':>9}")
for d in fund_pos:
    print(f"{d['sym']:<14}{d['fund']:>+9.4f}{d['chg_24h']:>+8.1f}{d['vol']/1e6:>8.0f}M")
    flagged[d["sym"]].append(("POS_FUND_EXT", 35 + min(d["fund"]*100, 30), f"fund {d['fund']:.3f}%"))

# Dim 3: At extreme of 24h range = breakout/breakdown candidates
near_hod = sorted([d for d in all_data if d["rng_pos"] > 0.95 and d["chg_24h"] > 5],
                   key=lambda x: -x["chg_24h"])[:8]
near_lod = sorted([d for d in all_data if d["rng_pos"] < 0.05 and d["chg_24h"] < -5],
                   key=lambda x: x["chg_24h"])[:8]
print(f"\n=== AT 24h HIGH (>95% of range) — breakout candidates ===")
print(f"{'sym':<14}{'24h%':>8}{'rng_pos':>9}{'fund%':>9}{'vol_M$':>9}")
for d in near_hod:
    print(f"{d['sym']:<14}{d['chg_24h']:>+8.1f}{d['rng_pos']:>9.2f}{d['fund']:>+9.4f}{d['vol']/1e6:>8.0f}M")
    flagged[d["sym"]].append(("AT_HOD", 25, f"@HOD"))

print(f"\n=== AT 24h LOW (<5% of range) — capitulation candidates ===")
print(f"{'sym':<14}{'24h%':>8}{'rng_pos':>9}{'fund%':>9}{'vol_M$':>9}")
for d in near_lod:
    print(f"{d['sym']:<14}{d['chg_24h']:>+8.1f}{d['rng_pos']:>9.2f}{d['fund']:>+9.4f}{d['vol']/1e6:>8.0f}M")
    flagged[d["sym"]].append(("AT_LOD", 25, f"@LOD"))

# Dim 4: combo signals — coins flagged in MULTIPLE dimensions = super gold
print(f"\n{'='*78}")
print("MULTI-DIMENSIONAL FLAGS (coins appearing in 2+ dimensions = HIGH CONVICTION):")
combos = []
for sym, flags in flagged.items():
    if len(flags) >= 2:
        total_score = sum(s for _, s, _ in flags)
        dims = [f[0] for f in flags]
        details = ", ".join(f[2] for f in flags)
        combos.append((sym, total_score, dims, details))

combos.sort(key=lambda x: -x[1])
print(f"{'sym':<14}{'score':>6}  dims  details")
for sym, score, dims, details in combos[:15]:
    line = f"{sym:<14}{score:>6}  [{','.join(dims)}]  {details}"
    print(line.encode("ascii", "replace").decode("ascii"))

# For top 5 multi-flagged, fetch detailed candle action
print(f"\n{'='*78}")
print("DEEP DIVE on top 5 multi-flagged candidates (recent candle action):")
for sym, score, dims, details in combos[:5]:
    try:
        k = c.get_klines(sym, "1m", limit=8)
        if len(k) < 8:
            continue
        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(bt["bidPrice"]); ask = float(bt["askPrice"])
        spread_pct = (ask - bid) / ((ask+bid)/2) * 100 if ask+bid > 0 else 99
        print(f"\n--- {sym} (score {score}) ---")
        print(f"  flags: {dims}")
        print(f"  details: {details}")
        print(f"  bid/ask: {bid}/{ask}  spread {spread_pct:.3f}%")
        ups = 0; total_pct = 0
        last_3_str = []
        for b in k[:-1]:
            o = float(b[1]); cl = float(b[4])
            chg = (cl/o - 1) * 100
            d = "U" if cl > o else "D"
            if cl > o: ups += 1
            total_pct += chg
            last_3_str.append(f"{chg:+.2f}%{d}")
        print(f"  last 7×1m: {' | '.join(last_3_str)}")
        print(f"  9m total: {total_pct:+.2f}%, UPs: {ups}/7")
    except Exception as e:
        print(f"  detail fail: {e}")
