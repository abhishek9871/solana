"""
NEXT-1-5-MINUTE PREDICTOR — layered on top of gold_scanner picks.

For each candidate, fetches forward-looking signals:
  1. ORDER BOOK IMBALANCE (top 20 bids vs asks): if bid_volume >> ask_volume,
     buyers are stacked = price unlikely to drop. And vice versa.
  2. AGGRESSOR FLOW (last 200 trades): buyer-taker ratio. >60% = bulls actively eating asks.
  3. OPEN INTEREST DELTA (last 5 min vs prior 5 min): rising OI + rising price = real
     positioning, not short-covering. Rising OI + falling price = new shorts piling in.
  4. PREMIUM INDEX vs FUNDING: if mark > index, futures premium = bullish positioning.

Combined score = direction_alignment of these signals. >=3/4 aligned = HIGH conviction.

Usage: pass a list of (symbol, side) candidates from gold scanner. Returns ranked list with
predictor scores attached.
"""
import os
import sys
import time
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")


def predict_next_5min(sym, side):
    """Returns (predictor_score 0-100, signals dict). Higher = more aligned with side."""
    score = 0
    signals = {}

    # 1. ORDER BOOK IMBALANCE
    try:
        depth = c.public_get("/fapi/v1/depth", {"symbol": sym, "limit": 20})
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        bid_vol = sum(float(p) * float(q) for p, q in bids)
        ask_vol = sum(float(p) * float(q) for p, q in asks)
        if bid_vol + ask_vol > 0:
            ob_imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)  # -1 to +1
        else:
            ob_imbalance = 0
        signals["ob_imbalance"] = ob_imbalance
        # Imbalance favors direction?
        if side == "BUY":
            if ob_imbalance > 0.20:
                score += 25; signals["ob_call"] = "STRONG_BID_WALL"
            elif ob_imbalance > 0.05:
                score += 12; signals["ob_call"] = "MILD_BID"
            elif ob_imbalance < -0.20:
                score -= 15; signals["ob_call"] = "ASK_WALL_HEAVY"
            else:
                signals["ob_call"] = "neutral"
        else:  # SELL
            if ob_imbalance < -0.20:
                score += 25; signals["ob_call"] = "STRONG_ASK_WALL"
            elif ob_imbalance < -0.05:
                score += 12; signals["ob_call"] = "MILD_ASK"
            elif ob_imbalance > 0.20:
                score -= 15; signals["ob_call"] = "BID_WALL_HEAVY"
            else:
                signals["ob_call"] = "neutral"
    except Exception as e:
        signals["ob_call"] = f"err {e}"

    # 2. AGGRESSOR FLOW (last 200 trades)
    try:
        trades = c.public_get("/fapi/v1/aggTrades", {"symbol": sym, "limit": 200})
        if isinstance(trades, list) and trades:
            buyer_taker_vol = 0
            seller_taker_vol = 0
            for t in trades:
                qty = float(t.get("q", 0)) * float(t.get("p", 0))
                # m=true means buyer is market maker (so taker is SELLER)
                if t.get("m"):
                    seller_taker_vol += qty
                else:
                    buyer_taker_vol += qty
            total = buyer_taker_vol + seller_taker_vol
            buy_ratio = buyer_taker_vol / total if total > 0 else 0.5
            signals["buy_ratio"] = buy_ratio
            if side == "BUY":
                if buy_ratio > 0.65:
                    score += 25; signals["flow_call"] = f"STRONG_BUY_FLOW_{buy_ratio:.2f}"
                elif buy_ratio > 0.55:
                    score += 12; signals["flow_call"] = f"BUY_TILT_{buy_ratio:.2f}"
                elif buy_ratio < 0.35:
                    score -= 15; signals["flow_call"] = f"SELL_DOMINANT_{buy_ratio:.2f}"
                else:
                    signals["flow_call"] = f"mixed_{buy_ratio:.2f}"
            else:
                if buy_ratio < 0.35:
                    score += 25; signals["flow_call"] = f"STRONG_SELL_FLOW_{buy_ratio:.2f}"
                elif buy_ratio < 0.45:
                    score += 12; signals["flow_call"] = f"SELL_TILT_{buy_ratio:.2f}"
                elif buy_ratio > 0.65:
                    score -= 15; signals["flow_call"] = f"BUY_DOMINANT_{buy_ratio:.2f}"
                else:
                    signals["flow_call"] = f"mixed_{buy_ratio:.2f}"
    except Exception as e:
        signals["flow_call"] = f"err {e}"

    # 3. OPEN INTEREST DELTA
    try:
        oi_now = c.public_get("/fapi/v1/openInterest", {"symbol": sym})
        oi_curr = float(oi_now.get("openInterest", 0))
        # Get OI history — 5min interval, last 6 points
        oi_hist = c.public_get("/futures/data/openInterestHist",
                                {"symbol": sym, "period": "5m", "limit": 6})
        if isinstance(oi_hist, list) and len(oi_hist) >= 3:
            oi_5m_ago = float(oi_hist[-2].get("sumOpenInterest", 0))
            oi_15m_ago = float(oi_hist[-4].get("sumOpenInterest", 0)) if len(oi_hist) >= 4 else oi_5m_ago
            oi_change_5m = (oi_curr - oi_5m_ago) / oi_5m_ago * 100 if oi_5m_ago > 0 else 0
            oi_change_15m = (oi_curr - oi_15m_ago) / oi_15m_ago * 100 if oi_15m_ago > 0 else 0
            signals["oi_5m_pct"] = oi_change_5m
            signals["oi_15m_pct"] = oi_change_15m
            # Rising OI in our direction = real positioning. For LONG, OI rising while price rising = bullish.
            # For SHORT, OI rising while price falling = real shorts piling in (bearish).
            # We can't directly know direction from OI; but rising OI + favorable trend = momentum.
            if side == "BUY":
                if oi_change_5m > 1.0:
                    score += 20; signals["oi_call"] = f"OI_BUILDING_+{oi_change_5m:.2f}%"
                elif oi_change_5m > 0.3:
                    score += 10; signals["oi_call"] = f"OI_mild_+{oi_change_5m:.2f}%"
                elif oi_change_5m < -1.0:
                    # OI dropping while wanting to long = positions closing = potentially top
                    score -= 5; signals["oi_call"] = f"OI_FADING_{oi_change_5m:.2f}%"
                else:
                    signals["oi_call"] = f"oi_flat_{oi_change_5m:.2f}%"
            else:  # SELL
                if oi_change_5m > 1.0:
                    score += 20; signals["oi_call"] = f"OI_BUILDING_+{oi_change_5m:.2f}% (shorts piling)"
                elif oi_change_5m > 0.3:
                    score += 10; signals["oi_call"] = f"OI_mild_+{oi_change_5m:.2f}%"
                elif oi_change_5m < -1.0:
                    # OI dropping while wanting to short = positions covering = bottom risk
                    score -= 5; signals["oi_call"] = f"OI_FADING_{oi_change_5m:.2f}% (shorts cover)"
                else:
                    signals["oi_call"] = f"oi_flat_{oi_change_5m:.2f}%"
    except Exception as e:
        signals["oi_call"] = f"err {e}"

    # 4. PREMIUM (mark - index) -- mostly funding-related, gives current pressure
    try:
        prem = c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
        mark_p = float(prem.get("markPrice", 0))
        index_p = float(prem.get("indexPrice", 0)) if prem.get("indexPrice") else mark_p
        last_funding = float(prem.get("lastFundingRate", 0)) * 100
        if mark_p > 0 and index_p > 0:
            premium_pct = (mark_p - index_p) / index_p * 100
        else:
            premium_pct = 0
        signals["premium_pct"] = premium_pct
        signals["last_fund"] = last_funding
        # Premium > 0 = futures pricier than spot = bullish positioning
        if side == "BUY":
            if premium_pct > 0.05 and last_funding < -0.05:
                # Bullish premium WITH heavy negative funding = squeeze active
                score += 15; signals["prem_call"] = "BULLISH_PREM_SQUEEZE"
            elif last_funding < -0.05:
                score += 8; signals["prem_call"] = "NEG_FUND_OK"
            else:
                signals["prem_call"] = f"prem_{premium_pct:.3f}_fund_{last_funding:.3f}"
        else:
            if premium_pct < -0.02 and last_funding > 0.05:
                score += 15; signals["prem_call"] = "BEARISH_PREM_TRAP"
            elif last_funding > 0.05:
                score += 8; signals["prem_call"] = "POS_FUND_OK"
            else:
                signals["prem_call"] = f"prem_{premium_pct:.3f}_fund_{last_funding:.3f}"
    except Exception as e:
        signals["prem_call"] = f"err {e}"

    return score, signals


def main():
    # Run gold scanner output via subprocess parse — but simpler: re-run inline picks
    # For now: hardcoded top picks from latest scan; in practice this gets called per-coin
    # Let's just take a list of candidates passed in
    if len(sys.argv) > 1:
        candidates = [(s.split(":")[0], s.split(":")[1]) for s in sys.argv[1:]]
    else:
        # Default: scan everything from gold scanner top, but for this script we take them inline
        candidates = [
            ("SWARMSUSDT", "SELL"),
            ("BIOUSDT", "BUY"),
            ("ZEREBROUSDT", "BUY"),
            ("SOLVUSDT", "BUY"),
            ("AIGENSYNUSDT", "SELL"),
            ("OPGUSDT", "SELL"),
        ]

    print(f"Predicting next 1-5 min for {len(candidates)} candidates...\n")
    results = []
    for sym, side in candidates:
        try:
            score, signals = predict_next_5min(sym, side)
            results.append((sym, side, score, signals))
            print(f"=== {sym} {side} (predictor score {score}) ===")
            for k, v in signals.items():
                if isinstance(v, float):
                    print(f"  {k}: {v:+.4f}")
                else:
                    print(f"  {k}: {v}")
            print()
        except Exception as e:
            print(f"  {sym} ERR: {e}\n")

    results.sort(key=lambda x: -x[2])
    print("=" * 70)
    print("RANKING (highest predictor score first):")
    for sym, side, score, _ in results:
        marker = " <- BEST" if score == max(r[2] for r in results) else ""
        print(f"  {sym} {side}: {score}{marker}")


if __name__ == "__main__":
    main()
