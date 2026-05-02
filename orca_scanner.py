"""
ORCA setup scanner — finds the single best ORCA-class candidate right now.

What made ORCA win earlier today:
  1. Funding was -1.125%/8h (extremely crowded shorts)
  2. 24h change was +29% (price already trending up)
  3. 1m velocity was +0.96% (momentum confirming squeeze)
  4. Volume was high

Strategy: rank ALL USD-M perps by ORCA-fit score. Trade only the top 1-2 with concentration.
"""
import os
from decimal import Decimal
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
secret = os.environ.get("BINANCE_TESTNET_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://testnet.binancefuture.com")
# Use TESTNET for tradeable candidates (matches what we can actually fire)
prod_c = c  # alias — both point at testnet

print("=== Scanning all Binance USD-M perps for ORCA-class setups ===\n")
print("ORCA-class = extreme funding + price aligned with squeeze + active momentum\n")

# Pull 24h tickers + premium index from PROD (real data)
tickers = prod_c.get_24hr_tickers()
premiums = prod_c.public_get("/fapi/v1/premiumIndex", {})
prem_map = {p["symbol"]: p for p in premiums} if isinstance(premiums, list) else {}

# Filter to USDT pairs with meaningful volume
usdt_tickers = []
for t in tickers:
    sym = t.get("symbol", "")
    if not sym.endswith("USDT"):
        continue
    try:
        vol = float(t["quoteVolume"])
        if vol < 50_000_000:
            continue
    except Exception:
        continue
    usdt_tickers.append(t)

# Score each for ORCA-fit
candidates = []
for t in usdt_tickers:
    sym = t["symbol"]
    p = prem_map.get(sym)
    if not p:
        continue
    try:
        chg_24h = float(t["priceChangePercent"])
        funding_raw = float(p.get("lastFundingRate", "0")) * 100
        # Normalize to 8h (assume 8h interval; some are 1h/4h)
        # For now use raw — orca-class symbols often have 1h funding so raw % is accurate per-period
        funding_8h_norm = funding_raw  # conservative; assume 8h
        # If funding is huge (>0.3% per period = likely 1h interval), normalize
        if abs(funding_raw) > 0.3:
            funding_8h_norm = funding_raw * 8  # if 1h interval, normalize
        # Get 1m velocity
        try:
            k1 = prod_c.get_klines(sym, "1m", limit=2)
            if len(k1) < 2:
                continue
            prev_close = float(k1[-2][4])
            curr_close = float(k1[-1][4])
            if prev_close <= 0:
                continue
            vel_1m = (curr_close / prev_close - 1) * 100
        except Exception:
            continue
        # Get spread
        try:
            bt = prod_c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = float(bt["bidPrice"])
            ask = float(bt["askPrice"])
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100 if mid > 0 else 99
        except Exception:
            spread_pct = 99
        if spread_pct > 0.10:
            continue  # skip illiquid

        # ORCA-fit score:
        # 1. Funding extremity (more extreme = higher score)
        funding_score = abs(funding_8h_norm)
        # 2. Direction alignment: short-crowded (negative funding) + price UP, OR long-crowded (positive funding) + price DOWN
        if funding_8h_norm < 0 and chg_24h > 0:
            direction = "BUY"  # ride the squeeze up
            alignment = chg_24h  # positive
        elif funding_8h_norm > 0 and chg_24h < 0:
            direction = "SELL"  # ride the unwind down
            alignment = -chg_24h
        else:
            continue  # not aligned
        # 3. Velocity confirming: same direction as alignment
        if direction == "BUY" and vel_1m < 0:
            continue  # no momentum confirmation
        if direction == "SELL" and vel_1m > 0:
            continue
        velocity_score = abs(vel_1m)

        # ORCA-fit composite: funding × |chg_24h| × velocity, weighted
        score = funding_score * 2.0 + alignment * 0.3 + velocity_score * 1.0

        candidates.append({
            "sym": sym,
            "score": score,
            "funding_8h": funding_8h_norm,
            "chg_24h": chg_24h,
            "vel_1m": vel_1m,
            "spread": spread_pct,
            "vol": float(t["quoteVolume"]),
            "direction": direction,
            "mark": mid,
        })
    except Exception:
        continue

candidates.sort(key=lambda x: x["score"], reverse=True)

print(f"{'rank':<5}{'symbol':<14}{'dir':<5}{'fund%':>10}{'24h%':>10}{'1m%':>8}{'spread%':>8}{'vol_M$':>10}{'score':>8}")
print("-" * 90)
for i, cand in enumerate(candidates[:15]):
    line = (f"{i+1:<5}{cand['sym']:<14}{cand['direction']:<5}"
            f"{cand['funding_8h']:>+10.3f}{cand['chg_24h']:>+10.2f}{cand['vel_1m']:>+8.2f}"
            f"{cand['spread']:>+8.4f}{cand['vol']/1e6:>9.1f}M{cand['score']:>+8.2f}")
    print(line.encode("ascii", "replace").decode("ascii"))

if candidates:
    top = candidates[0]
    print(f"\n=== TOP CANDIDATE: {top['sym']} ===")
    print(f"  Direction: {top['direction']}")
    print(f"  Funding: {top['funding_8h']:+.3f}%/8h")
    print(f"  24h:     {top['chg_24h']:+.2f}%")
    print(f"  1m:      {top['vel_1m']:+.2f}%")
    print(f"  Mark:    ${top['mark']:.6f}")
    print(f"  Volume:  ${top['vol']/1e6:.1f}M")
    print(f"\n  This is the highest-scoring ORCA-class setup right now.")
else:
    print("\nNO ORCA-class setups found. Markets in chop — wait for funding extremes to develop.")
