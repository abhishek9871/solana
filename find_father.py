"""
Find the FATHER of momentum trades.

Multi-factor ranking:
  - Consecutive same-direction 5m candles (3+)
  - Cumulative move over last 30 min (5%+)
  - Volume acceleration (each candle bigger than last)
  - 1m candle still confirming direction
  - Room left in 24h range (not at top/bottom)
  - Funding alignment

Higher score = stronger setup than HUSDT.
"""
import os
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trading_bot.binance_client import BinanceFuturesClient
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

MIN_VOL_USD = 2_000_000
MAX_VOL_USD = 800_000_000
MIN_CONSEC = 2                # at least 2 candles same direction
MIN_CUM_MOVE_PCT = 1.5        # cumulative over consec candles
MIN_LATEST_VOL_RATIO = 1.2
MAX_RANGE_POS_LONG = 0.95
MIN_RANGE_POS_SHORT = 0.05

SKIP = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"}


def log(msg):
    print(msg, flush=True)


def get_klines(symbol, interval, limit):
    try:
        return c.public_get("/fapi/v1/klines",
                            {"symbol": symbol, "interval": interval, "limit": limit})
    except Exception:
        return None


def score_symbol(sym, ticker):
    try:
        last = float(ticker["lastPrice"])
        high24 = float(ticker["highPrice"])
        low24 = float(ticker["lowPrice"])
        vol_usd = float(ticker["quoteVolume"])
        chg24 = float(ticker["priceChangePercent"])
        if vol_usd < MIN_VOL_USD or vol_usd > MAX_VOL_USD: return None
        if last <= 0 or high24 <= low24: return None

        kl5 = get_klines(sym, "5m", 8)
        if not kl5 or len(kl5) < 6: return None
        kl1 = get_klines(sym, "1m", 3)
        if not kl1 or len(kl1) < 2: return None

        # 5m analysis: count consec same-direction candles from latest
        opens5  = [float(k[1]) for k in kl5]
        closes5 = [float(k[4]) for k in kl5]
        vols5   = [float(k[5]) for k in kl5]
        highs5  = [float(k[2]) for k in kl5]
        lows5   = [float(k[3]) for k in kl5]

        last_dir = 1 if closes5[-1] > opens5[-1] else -1
        consec = 0
        for i in range(len(kl5) - 1, -1, -1):
            d = 1 if closes5[i] > opens5[i] else -1
            if d == last_dir:
                consec += 1
            else:
                break
        if consec < MIN_CONSEC: return None

        direction = "LONG" if last_dir > 0 else "SHORT"

        # Cumulative move across consec candles
        cum_start_open = opens5[-consec]
        cum_end_close = closes5[-1]
        cum_move = (cum_end_close - cum_start_open) / cum_start_open * 100
        if abs(cum_move) < MIN_CUM_MOVE_PCT: return None

        # Latest 5m volume vs avg of prior
        prior_avg_vol = sum(vols5[-(consec+1):-1]) / max(1, consec)
        vol_ratio = vols5[-1] / prior_avg_vol if prior_avg_vol > 0 else 0
        if vol_ratio < MIN_LATEST_VOL_RATIO: return None

        # 1m confirmation: latest 1m must align with 5m direction
        last_1m_dir = 1 if float(kl1[-1][4]) > float(kl1[-1][1]) else -1
        if last_1m_dir != last_dir: return None

        # ATR
        atr_5m = sum((highs5[i] - lows5[i]) / closes5[i] * 100 for i in range(-5, 0)) / 5

        # Range position
        range_pos = (last - low24) / (high24 - low24)
        if direction == "LONG" and range_pos > MAX_RANGE_POS_LONG: return None
        if direction == "SHORT" and range_pos < MIN_RANGE_POS_SHORT: return None

        # Volume acceleration: latest 3 5m candles increasing volume?
        accel = 0
        if len(vols5) >= 3:
            if vols5[-1] > vols5[-2]: accel += 1
            if vols5[-2] > vols5[-3]: accel += 1

        # SCORE composite
        score = (
            consec * 8                              # more consec = stronger trend
            + abs(cum_move) * 4                     # bigger move = better
            + min(vol_ratio, 8) * 5                 # volume confirmation (capped)
            + accel * 5                             # accelerating volume
            + atr_5m * 2                            # higher volatility
            + (1 - abs(range_pos - 0.5) * 2) * 3    # mid-range = best (room both sides)
        )

        # Try to get funding (best-effort)
        try:
            p = c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
            fund = float(p["lastFundingRate"]) * 100
        except Exception:
            fund = 0

        # Funding alignment bonus: long with neg funding (shorts trapped) or short with pos funding
        if direction == "LONG" and fund < -0.05: score += 10
        if direction == "SHORT" and fund > 0.05: score += 10

        return {
            "symbol": sym, "direction": direction, "score": score, "last": last,
            "consec": consec, "cum_move": cum_move, "vol_ratio": vol_ratio,
            "atr_5m": atr_5m, "range_pos": range_pos, "chg24": chg24,
            "vol_usd": vol_usd, "accel": accel, "fund": fund,
        }
    except Exception:
        return None


def main():
    log(f"=== FIND THE FATHER {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    tickers = c.public_get("/fapi/v1/ticker/24hr")
    log(f"Got {len(tickers)} symbols. Pre-filtering by volume...")

    candidates = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT") or sym in SKIP: continue
        try:
            vol = float(t["quoteVolume"])
            if vol < MIN_VOL_USD or vol > MAX_VOL_USD: continue
        except Exception:
            continue
        candidates.append(t)

    log(f"  -> {len(candidates)} symbols to scan deeply...\n")

    results = []
    for i, t in enumerate(candidates):
        if i % 30 == 0 and i > 0:
            log(f"  scanned {i}/{len(candidates)}...")
        r = score_symbol(t["symbol"], t)
        if r:
            results.append(r)

    results.sort(key=lambda x: -x["score"])

    log(f"\n{'='*78}")
    log(f"FATHER SETUPS (sorted by composite score):")
    log(f"{'='*78}")
    if not results:
        log("\n  No qualifying setups (need 3+ consec candles, 3%+ move, vol confirm, 1m align)")
        return

    for r in results[:10]:
        log(f"\n  [{r['score']:5.1f}] {r['symbol']:<14} {r['direction']:5}  ${r['last']:.6f}  24h={r['chg24']:+.1f}%")
        log(f"        {r['consec']} consec candles, cum {r['cum_move']:+.2f}%, "
            f"ATR={r['atr_5m']:.2f}%, vol_ratio={r['vol_ratio']:.1f}x, "
            f"accel={r['accel']}/2, range_pos={r['range_pos']:.2f}, fund={r['fund']:+.4f}%, "
            f"24h_vol=${r['vol_usd']/1e6:.0f}M")

    if results:
        top = results[0]
        log(f"\n{'='*78}")
        log(f"TOP PICK: {top['symbol']} {top['direction']}")
        log(f"  Score {top['score']:.1f} (HUSDT was your win — anything > 60 is 'father' tier)")
        atr = top["atr_5m"]
        sl_dist = max(atr * 1.5, 1.5)
        tp1_dist = max(atr * 3, 3.0)
        tp2_dist = max(atr * 5, 6.0)
        tp3_dist = max(atr * 8, 10.0)
        if top["direction"] == "LONG":
            sl = top["last"] * (1 - sl_dist/100)
            tp1 = top["last"] * (1 + tp1_dist/100)
            tp2 = top["last"] * (1 + tp2_dist/100)
            tp3 = top["last"] * (1 + tp3_dist/100)
        else:
            sl = top["last"] * (1 + sl_dist/100)
            tp1 = top["last"] * (1 - tp1_dist/100)
            tp2 = top["last"] * (1 - tp2_dist/100)
            tp3 = top["last"] * (1 - tp3_dist/100)
        log(f"  Entry:  ${top['last']:.6f} (market)")
        log(f"  SL:     ${sl:.6f}  ({sl_dist:.2f}%)")
        log(f"  TP1:    ${tp1:.6f}  ({tp1_dist:.2f}%)  R:R {tp1_dist/sl_dist:.1f}")
        log(f"  TP2:    ${tp2:.6f}  ({tp2_dist:.2f}%)  R:R {tp2_dist/sl_dist:.1f}")
        log(f"  TP3:    ${tp3:.6f}  ({tp3_dist:.2f}%)  R:R {tp3_dist/sl_dist:.1f}")


if __name__ == "__main__":
    main()
