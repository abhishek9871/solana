"""
Volatility hunter — find coins making EXPLOSIVE moves RIGHT NOW.

Looking for $50-style runners (small-cap altcoins ripping 5-15% in minutes):
  - 5m candle change >= 3% (real move, not chop)
  - Volume spike >= 3x prior 5-candle avg (real interest, not thin liquidity wick)
  - Last 2 candles same direction (continuation, not wick + reverse)
  - ATR_5m >= 1.5% (proven volatility, not dead)
  - 24h volume between $1M-$500M (small-mid cap, room to move)
  - Price NOT at top/bottom of 24h range (still has runway)

Per session lessons:
  - GENIUSUSDT taught us: 1.4% breakdown was wick, full retrace happened
  - We need MULTIPLE confirmation candles, not single
  - And we need WIDE volatility (>1% ATR) to make tight stops survivable
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

# === FILTERS ===
MIN_VOL_USD = 1_000_000        # min liquidity
MAX_VOL_USD = 500_000_000      # cap to filter out BTCETH (too liquid)
MIN_5M_MOVE_PCT = 3.0          # last 5m candle must move at least this much
MIN_VOL_RATIO = 3.0            # current candle volume vs prior avg
MIN_ATR_PCT = 1.5              # 5m ATR must be at least this
MAX_RANGE_POS = 0.92           # not too close to 24h high (for longs)
MIN_RANGE_POS = 0.08           # not too close to 24h low (for shorts)
MAX_RESULTS = 12


def log(msg):
    print(msg, flush=True)


def get_klines(symbol, interval="5m", limit=10):
    try:
        return c.public_get("/fapi/v1/klines",
                            {"symbol": symbol, "interval": interval, "limit": limit})
    except Exception:
        return None


def analyze(symbol, ticker):
    try:
        last = float(ticker["lastPrice"])
        high24 = float(ticker["highPrice"])
        low24 = float(ticker["lowPrice"])
        vol_usd = float(ticker["quoteVolume"])
        chg24 = float(ticker["priceChangePercent"])
        if vol_usd < MIN_VOL_USD or vol_usd > MAX_VOL_USD:
            return None
        if last <= 0 or high24 <= low24:
            return None
        if symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"):  # too liquid
            return None
        kl = get_klines(symbol, "5m", 10)
        if not kl or len(kl) < 8:
            return None
        opens  = [float(k[1]) for k in kl]
        highs  = [float(k[2]) for k in kl]
        lows   = [float(k[3]) for k in kl]
        closes = [float(k[4]) for k in kl]
        vols   = [float(k[5]) for k in kl]

        last_o, last_c = opens[-1], closes[-1]
        prev_c = closes[-2]
        prev_o = opens[-2]

        # 5m move from open to close (current candle)
        last_chg = (last_c - last_o) / last_o * 100
        # Continuation: last 2 candles same direction
        prev_chg = (prev_c - prev_o) / prev_o * 100

        if abs(last_chg) < MIN_5M_MOVE_PCT:
            return None
        # Same direction filter
        if last_chg > 0 and prev_chg < 0:
            return None  # reversal — wait
        if last_chg < 0 and prev_chg > 0:
            return None

        # Volume ratio
        avg_prev_vol = sum(vols[:-1]) / max(1, len(vols) - 1)
        vol_ratio = vols[-1] / avg_prev_vol if avg_prev_vol > 0 else 0
        if vol_ratio < MIN_VOL_RATIO:
            return None

        # ATR_5m as % of price
        atr_pct = sum(((highs[i] - lows[i]) / closes[i]) * 100 for i in range(-5, 0)) / 5
        if atr_pct < MIN_ATR_PCT:
            return None

        # Range position
        range_pos = (last - low24) / (high24 - low24)

        # Direction
        direction = "LONG" if last_chg > 0 else "SHORT"

        # Headroom check
        if direction == "LONG" and range_pos > MAX_RANGE_POS:
            return None
        if direction == "SHORT" and range_pos < MIN_RANGE_POS:
            return None

        # Score: prioritize fresh moves with volume + ATR
        score = abs(last_chg) * 5 + min(vol_ratio, 20) * 3 + atr_pct * 2 + abs(prev_chg) * 2

        return {
            "symbol": symbol,
            "direction": direction,
            "last": last,
            "score": score,
            "last_chg": last_chg,
            "prev_chg": prev_chg,
            "vol_ratio": vol_ratio,
            "atr_pct": atr_pct,
            "range_pos": range_pos,
            "chg24": chg24,
            "vol_usd": vol_usd,
        }
    except Exception:
        return None


def show(r):
    log(f"\n  {r['symbol']:14} {r['direction']:5}  score={r['score']:5.1f}  last=${r['last']:.6f}")
    log(f"    5m candle: {r['last_chg']:+.2f}%  prev: {r['prev_chg']:+.2f}%  ATR_5m={r['atr_pct']:.2f}%")
    log(f"    vol_ratio={r['vol_ratio']:.1f}x  range_pos={r['range_pos']:.2f}  24h={r['chg24']:+.2f}%  vol=${r['vol_usd']/1e6:.1f}M")


def main():
    log(f"=== VOLATILITY HUNTER {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log(f"Filters: 5m move >= {MIN_5M_MOVE_PCT}%, vol_ratio >= {MIN_VOL_RATIO}x, "
        f"ATR >= {MIN_ATR_PCT}%, vol ${MIN_VOL_USD/1e6:.0f}M-${MAX_VOL_USD/1e6:.0f}M\n")

    tickers = c.public_get("/fapi/v1/ticker/24hr")
    log(f"Got {len(tickers)} symbols. Filtering for explosive moves...")

    candidates = []
    n_scanned = 0
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        try:
            vol = float(t["quoteVolume"])
            if vol < MIN_VOL_USD or vol > MAX_VOL_USD:
                continue
        except Exception:
            continue
        n_scanned += 1
        if n_scanned % 50 == 0:
            log(f"  scanned {n_scanned}...")
        r = analyze(sym, t)
        if r:
            candidates.append(r)

    candidates.sort(key=lambda x: -x["score"])

    log(f"\n{'='*70}")
    log(f"TOP {min(MAX_RESULTS, len(candidates))} EXPLOSIVE MOVERS (sorted by score):")
    log(f"{'='*70}")
    if not candidates:
        log("\n  No coins meet the volatility threshold right now. Try again in 1-2 min.")
        return

    for r in candidates[:MAX_RESULTS]:
        show(r)

    # Specifically flag user's favorites
    favorites = ["ORCAUSDT", "ZEBROUSDT", "ZEBRAUSDT"]
    log(f"\n{'='*70}")
    log(f"USER FAVORITES STATUS:")
    log(f"{'='*70}")
    for sym in favorites:
        match = next((t for t in tickers if t["symbol"] == sym), None)
        if not match:
            log(f"  {sym}: not found on Binance Futures")
            continue
        try:
            chg = float(match["priceChangePercent"])
            vol = float(match["quoteVolume"])
            last = float(match["lastPrice"])
            log(f"  {sym}: ${last:.6f}  24h={chg:+.2f}%  vol=${vol/1e6:.1f}M")
        except Exception:
            log(f"  {sym}: error")

    log(f"\n{'='*70}")
    if candidates:
        top = candidates[0]
        log(f"TOP PICK: {top['symbol']} {top['direction']}  score={top['score']:.1f}")
        log(f"  Entry: ${top['last']:.6f}  Direction: {top['direction']}")
        log(f"  Last 5m: {top['last_chg']:+.2f}%  ATR: {top['atr_pct']:.2f}%  Vol spike: {top['vol_ratio']:.1f}x")
        # Suggest SL/TP based on ATR
        atr_pct = top["atr_pct"]
        sl_dist = atr_pct * 1.5  # 1.5x ATR for SL
        tp1_dist = atr_pct * 3
        tp2_dist = atr_pct * 6
        if top["direction"] == "LONG":
            sl_p = top["last"] * (1 - sl_dist / 100)
            tp1_p = top["last"] * (1 + tp1_dist / 100)
            tp2_p = top["last"] * (1 + tp2_dist / 100)
        else:
            sl_p = top["last"] * (1 + sl_dist / 100)
            tp1_p = top["last"] * (1 - tp1_dist / 100)
            tp2_p = top["last"] * (1 - tp2_dist / 100)
        log(f"  Suggested SL: ${sl_p:.6f}  ({sl_dist:+.2f}%)")
        log(f"  Suggested TP1: ${tp1_p:.6f}  ({tp1_dist:+.2f}%)  R:R {tp1_dist/sl_dist:.1f}:1")
        log(f"  Suggested TP2: ${tp2_p:.6f}  ({tp2_dist:+.2f}%)  R:R {tp2_dist/sl_dist:.1f}:1")


if __name__ == "__main__":
    main()
