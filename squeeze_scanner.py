"""
Manual squeeze scanner — find coins with squeeze setup forming RIGHT NOW.

Looks for:
  LONG SQUEEZE (shorts trapped, price about to rip up):
    - Negative or very low funding rate (shorts paying)
    - Price near 24h low but recovering (UP-candle reversal confirmation)
    - Recent green candles with volume
    - 5m / 15m timeframe alignment

  SHORT SQUEEZE (longs trapped, price about to dump):
    - Very positive funding rate (longs paying)
    - Price near 24h high but rolling over
    - Recent red candles with volume

Per memory: AT_LOD + NEG_FUND alone is NOT a setup. Need UP-candle reversal.
"""
import os
import sys
import time
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

MIN_VOL_USD = 5_000_000        # min 24h volume to consider liquid
MAX_RESULTS = 8                # show top N candidates per side


def log(msg):
    print(msg, flush=True)


def fetch_all_data():
    """Bulk fetch: tickers, funding rates, mark prices."""
    tickers = c.public_get("/fapi/v1/ticker/24hr")
    premiums = c.public_get("/fapi/v1/premiumIndex")
    return tickers, premiums


def get_klines(symbol, interval="5m", limit=20):
    try:
        return c.public_get("/fapi/v1/klines",
                            {"symbol": symbol, "interval": interval, "limit": limit})
    except Exception:
        return None


def analyze_symbol(symbol, ticker, funding):
    """Return scoring dict if symbol has squeeze potential, else None."""
    try:
        last = float(ticker["lastPrice"])
        high = float(ticker["highPrice"])
        low = float(ticker["lowPrice"])
        volume_usd = float(ticker["quoteVolume"])
        change_pct = float(ticker["priceChangePercent"])
        if volume_usd < MIN_VOL_USD:
            return None
        if last <= 0 or high <= low:
            return None

        # position in 24h range (0 = at LOD, 1 = at HOD)
        range_pos = (last - low) / (high - low)

        return {
            "symbol": symbol,
            "last": last,
            "high": high,
            "low": low,
            "volume_usd": volume_usd,
            "change_pct": change_pct,
            "range_pos": range_pos,
            "funding": funding,
        }
    except Exception:
        return None


def check_long_squeeze(s):
    """Setup: shorts trapped near LOD, UP-candle reversal in progress."""
    # need price near LOD
    if s["range_pos"] > 0.30:  # not near low
        return None, ""
    # need negative or near-zero funding (shorts being charged)
    if s["funding"] > 0.0001:  # > 0.01% positive = no shorts trapped
        return None, ""
    # Check recent klines for UP-candle reversal (last close > prev close on 5m)
    kl = get_klines(s["symbol"], "5m", 6)
    if not kl or len(kl) < 4:
        return None, ""
    closes = [float(k[4]) for k in kl[-4:]]
    opens  = [float(k[1]) for k in kl[-4:]]
    vols   = [float(k[5]) for k in kl[-4:]]
    last_close = closes[-1]
    last_open = opens[-1]
    prev_close = closes[-2]
    # last candle must be green AND breaking above prev close
    if last_close <= last_open or last_close <= prev_close:
        return None, ""
    # volume on last candle should be >= avg of prior 3
    avg_prev_vol = sum(vols[:-1]) / 3
    if vols[-1] < avg_prev_vol * 0.8:
        return None, ""
    # bullish reversal magnitude
    reversal_pct = (last_close - prev_close) / prev_close * 100
    score = (
        (1 - s["range_pos"]) * 30  # closer to LOD = more shorts trapped
        + max(0, -s["funding"] * 100000) * 5  # more negative funding = more pressure
        + min(reversal_pct * 10, 30)  # reversal magnitude
        + (vols[-1] / avg_prev_vol) * 10  # volume confirmation
    )
    note = (f"range_pos={s['range_pos']:.2f}, funding={s['funding']*100:.4f}%, "
            f"5m reversal +{reversal_pct:.2f}%, vol_ratio={vols[-1]/avg_prev_vol:.2f}x")
    return score, note


def check_short_squeeze(s):
    """Setup: longs trapped near HOD, DOWN-candle reversal in progress."""
    if s["range_pos"] < 0.70:  # not near high
        return None, ""
    if s["funding"] < 0.0001:  # need positive funding (longs paying)
        return None, ""
    kl = get_klines(s["symbol"], "5m", 6)
    if not kl or len(kl) < 4:
        return None, ""
    closes = [float(k[4]) for k in kl[-4:]]
    opens  = [float(k[1]) for k in kl[-4:]]
    vols   = [float(k[5]) for k in kl[-4:]]
    last_close = closes[-1]
    last_open = opens[-1]
    prev_close = closes[-2]
    # last candle must be red AND breaking below prev close
    if last_close >= last_open or last_close >= prev_close:
        return None, ""
    avg_prev_vol = sum(vols[:-1]) / 3
    if vols[-1] < avg_prev_vol * 0.8:
        return None, ""
    breakdown_pct = (prev_close - last_close) / prev_close * 100
    score = (
        s["range_pos"] * 30
        + min(s["funding"] * 100000, 50) * 5
        + min(breakdown_pct * 10, 30)
        + (vols[-1] / avg_prev_vol) * 10
    )
    note = (f"range_pos={s['range_pos']:.2f}, funding={s['funding']*100:.4f}%, "
            f"5m breakdown -{breakdown_pct:.2f}%, vol_ratio={vols[-1]/avg_prev_vol:.2f}x")
    return score, note


def main():
    log(f"=== SQUEEZE SCANNER {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    log("Fetching market data...")
    tickers, premiums = fetch_all_data()

    # Build funding lookup
    fund_map = {p["symbol"]: float(p.get("lastFundingRate", 0)) for p in premiums}

    log(f"Got {len(tickers)} symbols. Filtering...")

    # Filter to USDT perps with sufficient volume
    candidates = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        if sym not in fund_map:
            continue
        analysis = analyze_symbol(sym, t, fund_map[sym])
        if analysis:
            candidates.append(analysis)

    log(f"  -> {len(candidates)} liquid USDT perps. Scanning for squeezes...\n")

    longs = []
    shorts = []
    for i, s in enumerate(candidates):
        if i % 30 == 0:
            log(f"  scanning {i}/{len(candidates)}...")
        score, note = check_long_squeeze(s)
        if score:
            longs.append((score, s, note))
        score, note = check_short_squeeze(s)
        if score:
            shorts.append((score, s, note))

    longs.sort(key=lambda x: -x[0])
    shorts.sort(key=lambda x: -x[0])

    log(f"\n{'='*70}")
    log(f"LONG SQUEEZE candidates (shorts trapped, ready to rip UP):")
    log(f"{'='*70}")
    if not longs:
        log("  (none)")
    for score, s, note in longs[:MAX_RESULTS]:
        log(f"\n  {s['symbol']:12} score={score:6.1f}  last=${s['last']:.6f}  "
            f"24h={s['change_pct']:+.2f}%  vol=${s['volume_usd']/1e6:.1f}M")
        log(f"    {note}")

    log(f"\n{'='*70}")
    log(f"SHORT SQUEEZE candidates (longs trapped, ready to DUMP):")
    log(f"{'='*70}")
    if not shorts:
        log("  (none)")
    for score, s, note in shorts[:MAX_RESULTS]:
        log(f"\n  {s['symbol']:12} score={score:6.1f}  last=${s['last']:.6f}  "
            f"24h={s['change_pct']:+.2f}%  vol=${s['volume_usd']/1e6:.1f}M")
        log(f"    {note}")

    log(f"\n{'='*70}")
    log(f"Top recommendation: {'NONE' if not (longs or shorts) else (longs[0][2] if longs and (not shorts or longs[0][0] > shorts[0][0]) else shorts[0][2])}")


if __name__ == "__main__":
    main()
