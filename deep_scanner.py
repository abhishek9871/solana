"""
Deeper scanner — looks at multi-factor confluence:
1. Funding extremity (8h normalized)
2. 24h price move + alignment with funding crowd direction
3. 5m/15m trend agreement
4. Volume spike (1h vs 24h avg ratio)
5. Open interest change (last 1h vs 4h)
6. Spread (must be tight)

Score = weighted composite. Top setup is the one with confluence across all factors.
"""
import os
import time
from decimal import Decimal
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

print("=== Deep Scanner: multi-factor confluence ===\n")

tickers = c.get_24hr_tickers()
prems = c.public_get("/fapi/v1/premiumIndex", {})
prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

# Filter to liquid USDT pairs
usdt = [t for t in tickers if t.get("symbol", "").endswith("USDT")
        and float(t.get("quoteVolume", 0)) > 30_000_000]
print(f"Scanning {len(usdt)} liquid USDT pairs (>$30M 24h vol)...\n")

candidates = []
for i, t in enumerate(usdt):
    sym = t["symbol"]
    try:
        vol_24h = float(t["quoteVolume"])
        chg_24h = float(t["priceChangePercent"])
        last = float(t["lastPrice"])
        prem = prem_map.get(sym)
        if not prem:
            continue
        funding_raw = float(prem.get("lastFundingRate", "0")) * 100
        funding_8h = funding_raw * 8 if abs(funding_raw) > 0.3 else funding_raw

        # Multi-timeframe trend
        try:
            k_5m = c.get_klines(sym, "5m", limit=4)
            k_15m = c.get_klines(sym, "15m", limit=4)
        except BinanceApiError:
            continue
        if len(k_5m) < 4 or len(k_15m) < 4:
            continue
        # 5m direction over last 3 closed bars
        c5 = [float(b[4]) for b in k_5m[:-1]]
        if len(c5) < 3:
            continue
        trend_5m = "UP" if c5[-1] > c5[0] else "DOWN" if c5[-1] < c5[0] else "FLAT"
        trend_5m_pct = (c5[-1] / c5[0] - 1) * 100 if c5[0] > 0 else 0
        c15 = [float(b[4]) for b in k_15m[:-1]]
        trend_15m = "UP" if c15[-1] > c15[0] else "DOWN" if c15[-1] < c15[0] else "FLAT"
        trend_15m_pct = (c15[-1] / c15[0] - 1) * 100 if c15[0] > 0 else 0

        # Volume spike: last 1h vs 24h avg per-hour
        try:
            k_1h = c.get_klines(sym, "1h", limit=2)
            vol_1h = float(k_1h[-2][7])  # quote volume
        except (BinanceApiError, IndexError, KeyError):
            vol_1h = 0
        avg_per_hour = vol_24h / 24
        vol_spike_ratio = vol_1h / avg_per_hour if avg_per_hour > 0 else 0

        # Open Interest change
        try:
            oi_now = c.public_get("/fapi/v1/openInterest", {"symbol": sym})
            oi_curr = float(oi_now.get("openInterest", 0)) * last
        except BinanceApiError:
            continue

        # Spread
        try:
            bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid = float(bt["bidPrice"])
            ask = float(bt["askPrice"])
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100 if mid > 0 else 99
        except BinanceApiError:
            continue
        if spread_pct > 0.10:
            continue

        # Direction logic
        if funding_8h < -0.05 and chg_24h > 0:
            direction = "BUY"
            funding_aligned = True
        elif funding_8h > 0.05 and chg_24h < 0:
            direction = "SELL"
            funding_aligned = True
        elif chg_24h > 5 and trend_5m == "UP":
            direction = "BUY"
            funding_aligned = False
        elif chg_24h < -5 and trend_5m == "DOWN":
            direction = "SELL"
            funding_aligned = False
        else:
            continue

        # Confirm trend alignment
        if direction == "BUY" and trend_5m != "UP":
            continue
        if direction == "SELL" and trend_5m != "DOWN":
            continue

        # Composite score
        funding_score = abs(funding_8h) * 3.0  # heavy weight
        chg_score = abs(chg_24h) * 0.3
        trend_5m_score = abs(trend_5m_pct) * 0.5
        trend_15m_agree = 1.0 if (
            (direction == "BUY" and trend_15m == "UP") or
            (direction == "SELL" and trend_15m == "DOWN")
        ) else 0.0
        trend_15m_score = trend_15m_agree * abs(trend_15m_pct) * 0.5
        vol_score = max(0, vol_spike_ratio - 1) * 1.0  # bonus if 1h vol > 24h avg
        funding_bonus = 2.0 if funding_aligned else 0.0

        score = funding_score + chg_score + trend_5m_score + trend_15m_score + vol_score + funding_bonus

        candidates.append({
            "sym": sym, "score": score, "dir": direction,
            "fund": funding_8h, "chg24": chg_24h,
            "t5m": trend_5m, "t5m_pct": trend_5m_pct,
            "t15m": trend_15m, "t15m_pct": trend_15m_pct,
            "vol_spike": vol_spike_ratio,
            "vol_24h": vol_24h, "spread": spread_pct, "mark": mid,
            "f_align": funding_aligned, "oi_usd": oi_curr,
        })
    except Exception as e:
        continue

candidates.sort(key=lambda x: x["score"], reverse=True)

print(f"{'rank':<5}{'symbol':<14}{'dir':<5}{'fund%':>9}{'24h%':>9}{'5m%':>7}{'15m%':>7}{'vol_spike':>10}{'spread%':>8}{'score':>8}")
print("-" * 100)
for i, cand in enumerate(candidates[:15]):
    line = (f"{i+1:<5}{cand['sym']:<14}{cand['dir']:<5}"
            f"{cand['fund']:>+9.3f}{cand['chg24']:>+9.2f}{cand['t5m_pct']:>+7.2f}{cand['t15m_pct']:>+7.2f}"
            f"{cand['vol_spike']:>10.2f}{cand['spread']:>+8.4f}{cand['score']:>+8.2f}")
    print(line.encode("ascii", "replace").decode("ascii"))

if candidates:
    top = candidates[0]
    print(f"\n=== BEST CONFLUENCE: {top['sym']} ===")
    print(f"  Direction:        {top['dir']}")
    print(f"  Funding:          {top['fund']:+.3f}%/8h{'  (ALIGNED)' if top['f_align'] else '  (not crowded)'}")
    print(f"  24h move:         {top['chg24']:+.2f}%")
    print(f"  5m trend:         {top['t5m']} {top['t5m_pct']:+.2f}%")
    print(f"  15m trend:        {top['t15m']} {top['t15m_pct']:+.2f}%")
    print(f"  Volume spike:     {top['vol_spike']:.2f}x average per-hour")
    print(f"  Spread:           {top['spread']:+.4f}%")
    print(f"  Mark:             ${top['mark']:.6f}")
    print(f"  OI:               ${top['oi_usd']/1e6:.1f}M")
