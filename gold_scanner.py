"""
GOLD SCANNER v2 — biased toward MASSIVE PROFIT setups, not "safest" trades.
Filters TradFi-Perps automatically (we can't trade those).

Scoring (max ~140):
  1. Macro_momentum (0-40):  24h % move — coins doing 30%+ are the squeeze coins
  2. TF_alignment (0-25):    1m/5m/15m all same direction with magnitude
  3. Funding_squeeze (-10 to +20): squeeze fuel
  4. Volatility_expansion (0-15): recent candles > typical (range expansion = breakout)
  5. Continuation_or_pullback (-25 to +20):
        - Pullback in trend = +20 (CLEAN entry)
        - Just-printed monster candle = -25 (EXHAUSTION chase)
  6. Vol_surge (0-15): fresh flow

Threshold: 70+ = GOLD. Below = WAIT.
"""
import os
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

VOL_MIN_USD = 30_000_000
SPREAD_MAX_PCT = 0.12

# Pull tradeable USDT perps, filter out TradFi
einfo = c.public_get("/fapi/v1/exchangeInfo", {})
tradeable = set()
for s in einfo.get("symbols", []):
    if (s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("marginAsset") == "USDT"
            and "TradFi" not in (s.get("underlyingSubType") or [])):
        tradeable.add(s["symbol"])
print(f"Tradeable non-TradFi USDT perps: {len(tradeable)}\n")


def score_coin(sym, t, prem_map):
    try:
        vol = float(t.get("quoteVolume", 0))
        if vol < VOL_MIN_USD:
            return None

        chg_24h = float(t.get("priceChangePercent", 0))
        # Skip dead coins (no momentum to ride)
        if abs(chg_24h) < 5:
            return None

        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(bt["bidPrice"]); ask = float(bt["askPrice"])
        mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
        if mid == 0:
            return None
        spread = (ask - bid) / mid * 100
        if spread > SPREAD_MAX_PCT:
            return None

        k1 = c.get_klines(sym, "1m", limit=35)
        if len(k1) < 35:
            return None
        k5 = c.get_klines(sym, "5m", limit=4)
        if len(k5) < 4:
            return None
        k15 = c.get_klines(sym, "15m", limit=3)
        if len(k15) < 3:
            return None

        closed_1m = k1[:-1]

        # Per-TF % changes
        c1m_5 = (float(closed_1m[-1][4]) / float(closed_1m[-5][1]) - 1) * 100
        c5m = (float(k5[-2][4]) / float(k5[-2][1]) - 1) * 100
        c15m = (float(k15[-2][4]) / float(k15[-2][1]) - 1) * 100

        last_chg = (float(closed_1m[-1][4]) / float(closed_1m[-1][1]) - 1) * 100

        # Last 5 candles — need to identify "exhaustion" (biggest just printed)
        last_5_pcts = [(float(b[4]) / float(b[1]) - 1) * 100 for b in closed_1m[-5:]]
        last_5_abs = [abs(p) for p in last_5_pcts]
        last_is_biggest = last_5_abs[-1] == max(last_5_abs)

        # Volatility expansion: max abs candle in last 5 vs avg abs candle in prior 25
        prior_25_abs = [abs((float(b[4]) / float(b[1]) - 1) * 100) for b in closed_1m[-30:-5]]
        avg_prior = sum(prior_25_abs) / max(len(prior_25_abs), 1)
        max_recent = max(last_5_abs)
        vol_expansion = max_recent / max(avg_prior, 0.01)

        # Volume surge: last 5 min vs prior 30 min avg
        vol_last5 = sum(float(b[7]) for b in closed_1m[-5:])
        vol_prev30 = sum(float(b[7]) for b in closed_1m[-35:-5])
        vol_surge = (vol_last5 / 5) / (vol_prev30 / 30) if vol_prev30 > 0 else 0

        # Funding
        fund = 0.0
        p = prem_map.get(sym)
        if p:
            fund = float(p.get("lastFundingRate", "0")) * 100

        h24 = float(t["highPrice"]); l24 = float(t["lowPrice"])
        range_pos = (mid - l24) / (h24 - l24) if h24 != l24 else 0.5

        # === Score LONG ===
        long_score = 0
        long_reasons = []

        # 1. Macro momentum (only positive 24h aligns with LONG)
        if chg_24h >= 50:
            long_score += 40; long_reasons.append(f"24H_+{chg_24h:.0f}%")
        elif chg_24h >= 30:
            long_score += 30; long_reasons.append(f"24H_+{chg_24h:.0f}%")
        elif chg_24h >= 20:
            long_score += 20
        elif chg_24h >= 10:
            long_score += 10
        elif chg_24h <= -20:  # heavily down — long is counter-trend
            long_score -= 15

        # 2. TF alignment (sign + magnitude)
        ups = sum(1 for x in [c1m_5, c5m, c15m] if x > 0)
        if ups == 3:
            avg_mag = (c1m_5 + c5m + c15m) / 3
            if avg_mag > 1.5:
                long_score += 25; long_reasons.append("3TF_STRONG")
            else:
                long_score += 15; long_reasons.append("3TF_BULL")
        elif ups == 2:
            long_score += 5

        # 3. Funding squeeze
        if fund < -0.05:
            long_score += 20; long_reasons.append(f"SQUEEZE_FUND_{fund:.3f}")
        elif fund < -0.02:
            long_score += 12
        elif fund < 0:
            long_score += 5
        elif fund > 0.10:
            long_score -= 10
        elif fund > 0.05:
            long_score -= 5

        # 4. Volatility expansion
        if vol_expansion >= 2.5:
            long_score += 15; long_reasons.append(f"VOL_EXP_{vol_expansion:.1f}x")
        elif vol_expansion >= 1.7:
            long_score += 10
        elif vol_expansion >= 1.2:
            long_score += 5

        # 5. Continuation / pullback / exhaustion
        if last_is_biggest and last_chg > 1.5:
            long_score -= 25; long_reasons.append(f"EXHAUST_+{last_chg:.1f}%")
        elif last_is_biggest and last_chg > 0.8:
            long_score -= 10
        elif c5m > 1.0 and c1m_5 < c5m * 0.5 and last_chg < 0 and abs(last_chg) < 0.5:
            # 5m strong UP, last 5×1m only modestly up, latest candle small pullback
            long_score += 20; long_reasons.append("PULLBACK_IN_TREND")
        elif c15m > 2.0 and c5m > 0 and -0.3 < last_chg < 0.4:
            long_score += 12; long_reasons.append("STEADY_TREND")

        # 6. Vol surge
        if vol_surge >= 3:
            long_score += 15; long_reasons.append(f"VOL_SURGE_{vol_surge:.1f}x")
        elif vol_surge >= 2:
            long_score += 10
        elif vol_surge >= 1.3:
            long_score += 5

        # === Score SHORT ===
        short_score = 0
        short_reasons = []

        # 1. Macro for SHORT — actively dumping = primary signal
        if chg_24h <= -30:
            short_score += 30; short_reasons.append(f"24H_{chg_24h:.0f}%")
        elif chg_24h <= -20:
            short_score += 20
        elif chg_24h <= -10:
            short_score += 10
        # SHORT on a +20%+ pump can pay if trend exhausts
        elif chg_24h >= 30:
            # only worth scoring if there's actual dump signal
            if c1m_5 < -1 and c5m < -0.5:
                short_score += 25; short_reasons.append(f"PUMP_REVERSAL_24H+{chg_24h:.0f}%")
            elif c5m < -0.3:
                short_score += 10

        dns = sum(1 for x in [c1m_5, c5m, c15m] if x < 0)
        if dns == 3:
            avg_mag = -(c1m_5 + c5m + c15m) / 3
            if avg_mag > 1.5:
                short_score += 25; short_reasons.append("3TF_STRONG_BEAR")
            else:
                short_score += 15; short_reasons.append("3TF_BEAR")
        elif dns == 2:
            short_score += 5

        if fund > 0.08:
            short_score += 20; short_reasons.append(f"LONG_TRAP_FUND_{fund:.3f}")
        elif fund > 0.04:
            short_score += 12
        elif fund > 0:
            short_score += 5
        elif fund < -0.10:
            short_score -= 10

        if vol_expansion >= 2.5:
            short_score += 15
        elif vol_expansion >= 1.7:
            short_score += 10
        elif vol_expansion >= 1.2:
            short_score += 5

        if last_is_biggest and last_chg < -1.5:
            short_score -= 25; short_reasons.append(f"EXHAUST_{last_chg:.1f}%")
        elif last_is_biggest and last_chg < -0.8:
            short_score -= 10
        elif c5m < -1.0 and c1m_5 > c5m * 0.5 and last_chg > 0 and last_chg < 0.5:
            short_score += 20; short_reasons.append("PULLBACK_IN_DUMP")
        elif c15m < -2.0 and c5m < 0 and -0.4 < last_chg < 0.3:
            short_score += 12

        if vol_surge >= 3:
            short_score += 15
        elif vol_surge >= 2:
            short_score += 10
        elif vol_surge >= 1.3:
            short_score += 5

        return {
            "sym": sym,
            "long_score": long_score,
            "short_score": short_score,
            "long_reasons": long_reasons,
            "short_reasons": short_reasons,
            "chg_24h": chg_24h,
            "c1m_5": c1m_5,
            "c5m": c5m,
            "c15m": c15m,
            "last_chg": last_chg,
            "fund": fund,
            "range_pos": range_pos,
            "vol_24h": vol,
            "vol_surge": vol_surge,
            "vol_exp": vol_expansion,
            "spread": spread,
            "mark": mid,
            "last_is_biggest": last_is_biggest,
        }
    except Exception as e:
        return None


tickers = c.get_24hr_tickers()
prems = c.public_get("/fapi/v1/premiumIndex", {})
prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

candidates = [t for t in tickers
              if t.get("symbol", "") in tradeable
              and float(t.get("quoteVolume", 0)) > VOL_MIN_USD]

print(f"Scoring {len(candidates)} liquid non-TradFi pairs...\n")

results = []
for t in candidates:
    r = score_coin(t["symbol"], t, prem_map)
    if r:
        results.append(r)

results_long = sorted(results, key=lambda x: x["long_score"], reverse=True)[:10]
print("TOP 10 LONG SETUPS:")
print(f"{'rank':<5}{'sym':<14}{'score':>6}{'24h%':>8}{'1m_5':>7}{'5m':>7}{'15m':>7}{'fund%':>9}{'last%':>8}{'expand':>8}  reasons")
for i, r in enumerate(results_long):
    line = (f"{i+1:<5}{r['sym']:<14}{r['long_score']:>6}{r['chg_24h']:>+8.2f}"
            f"{r['c1m_5']:>+7.2f}{r['c5m']:>+7.2f}{r['c15m']:>+7.2f}"
            f"{r['fund']:>+9.4f}{r['last_chg']:>+8.2f}{r['vol_exp']:>7.2f}x  {','.join(r['long_reasons'])}")
    print(line.encode("ascii", "replace").decode("ascii"))

print()
results_short = sorted(results, key=lambda x: x["short_score"], reverse=True)[:10]
print("TOP 10 SHORT SETUPS:")
print(f"{'rank':<5}{'sym':<14}{'score':>6}{'24h%':>8}{'1m_5':>7}{'5m':>7}{'15m':>7}{'fund%':>9}{'last%':>8}{'expand':>8}  reasons")
for i, r in enumerate(results_short):
    line = (f"{i+1:<5}{r['sym']:<14}{r['short_score']:>6}{r['chg_24h']:>+8.2f}"
            f"{r['c1m_5']:>+7.2f}{r['c5m']:>+7.2f}{r['c15m']:>+7.2f}"
            f"{r['fund']:>+9.4f}{r['last_chg']:>+8.2f}{r['vol_exp']:>7.2f}x  {','.join(r['short_reasons'])}")
    print(line.encode("ascii", "replace").decode("ascii"))

# Pick GOLD
all_picks = []
for r in results:
    all_picks.append((r["long_score"], r, "BUY"))
    all_picks.append((r["short_score"], r, "SELL"))
all_picks.sort(key=lambda x: x[0], reverse=True)

print()
if all_picks:
    score, r, side = all_picks[0]
    reasons = r["long_reasons"] if side == "BUY" else r["short_reasons"]
    print("=" * 78)
    print(f"=== GOLD: {r['sym']} {side} (score {score}) ===")
    print(f"  24h chg:      {r['chg_24h']:+.2f}%   <- macro momentum")
    print(f"  Multi-TF:     1m_5={r['c1m_5']:+.2f}%  5m={r['c5m']:+.2f}%  15m={r['c15m']:+.2f}%")
    print(f"  Last 1m:      {r['last_chg']:+.2f}% {'[biggest of 5 — risky]' if r['last_is_biggest'] else ''}")
    print(f"  Funding:      {r['fund']:+.4f}%")
    print(f"  Vol expand:   {r['vol_exp']:.2f}x typical recent candle")
    print(f"  Vol surge:    {r['vol_surge']:.2f}x recent flow")
    print(f"  Range pos:    {r['range_pos']:.2f}")
    print(f"  Vol 24h:      ${r['vol_24h']/1e6:.0f}M")
    print(f"  Spread:       {r['spread']:.4f}%")
    print(f"  Mark:         ${r['mark']:.6f}")
    print(f"  Reasons:      {', '.join(reasons)}")
    if score < 70:
        print(f"  *** SCORE {score} BELOW 70 = wait for cleaner setup ***")
    print("=" * 78)
