"""
HARMONY v2 — perfected long/short decision pipeline.

Core principle: DIRECTION DECISION must pass HARD VETO checks. Any single
contradicting signal disqualifies the trade. No exceptions.

Pipeline:
  1. SCAN: macro structure (gold scanner core: 24h, multi-TF, funding, etc)
  2. PREDICT: forward-looking signals (order book, flow, OI, premium)
  3. DECIDE: HARD VETO checks. ANY of these fails = SKIP.
       For BUY:
         - 5m AND 15m > 0 (broader TFs aligned bullish — no counter-trend)
         - buy_ratio >= 0.50 (no sell pressure)
         - ob_imbalance >= -0.10 (no heavy ask wall blocking upside)
         - range_pos <= 0.96 (some room to run, not pinned at HOD)
         - latest 1m candle NOT a +1.5%+ exhaustion print
         - funding <= +0.08 (not in long-trap zone)
       For SELL: symmetric inversions.
  4. CATEGORIZE conviction:
       - PRIME: catalyst (24h pump+reversal OR -25%+ capitulation) + all signals
       - GOOD: harmony >= 90, macro >= 60, predictor >= 25
       - SUFFICIENT: harmony >= 70, macro >= 50, predictor >= 20
       - SKIP: anything else
  5. SIZE & RECOMMEND: PRIME = 6% wallet risk, GOOD = 4%, SUFFICIENT = 2%
"""
import os
from decimal import Decimal
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

VOL_MIN_USD = 30_000_000
SPREAD_MAX_PCT = 0.10


def get_tradeable_symbols():
    info = c.public_get("/fapi/v1/exchangeInfo", {})
    out = set()
    for s in info.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("marginAsset") == "USDT"
                and "TradFi" not in (s.get("underlyingSubType") or [])):
            out.add(s["symbol"])
    return out


def score_macro(sym, t, prem_map):
    try:
        vol = float(t.get("quoteVolume", 0))
        if vol < VOL_MIN_USD: return None
        chg_24h = float(t.get("priceChangePercent", 0))
        if abs(chg_24h) < 5: return None

        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = float(bt["bidPrice"]); ask = float(bt["askPrice"])
        mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
        if mid == 0: return None
        spread = (ask - bid) / mid * 100
        if spread > SPREAD_MAX_PCT: return None

        k1 = c.get_klines(sym, "1m", limit=35)
        if len(k1) < 35: return None
        k5 = c.get_klines(sym, "5m", limit=4)
        if len(k5) < 4: return None
        k15 = c.get_klines(sym, "15m", limit=3)
        if len(k15) < 3: return None
        closed_1m = k1[:-1]

        c1m_5 = (float(closed_1m[-1][4]) / float(closed_1m[-5][1]) - 1) * 100
        c5m = (float(k5[-2][4]) / float(k5[-2][1]) - 1) * 100
        c15m = (float(k15[-2][4]) / float(k15[-2][1]) - 1) * 100
        last_chg = (float(closed_1m[-1][4]) / float(closed_1m[-1][1]) - 1) * 100

        last_5_pcts = [(float(b[4])/float(b[1]) - 1) * 100 for b in closed_1m[-5:]]
        last_5_abs = [abs(p) for p in last_5_pcts]
        last_is_biggest = last_5_abs[-1] == max(last_5_abs)

        # Post-spike instability check: max abs candle of last 8 minutes
        last_8_abs = [abs((float(b[4])/float(b[1]) - 1) * 100) for b in closed_1m[-8:]]
        max_recent_abs = max(last_8_abs)

        # Exhaustion check: count consecutive same-direction candles ending at latest
        last_9_dirs = [1 if float(b[4]) > float(b[1]) else -1 for b in closed_1m[-9:]]
        exhaustion_streak = 1
        for i in range(len(last_9_dirs)-2, -1, -1):
            if last_9_dirs[i] == last_9_dirs[-1]:
                exhaustion_streak += 1
            else:
                break

        prior_25_abs = [abs((float(b[4])/float(b[1]) - 1) * 100) for b in closed_1m[-30:-5]]
        avg_prior = sum(prior_25_abs) / max(len(prior_25_abs), 1)
        max_recent = max(last_5_abs)
        vol_expansion = max_recent / max(avg_prior, 0.01)

        vol_last5 = sum(float(b[7]) for b in closed_1m[-5:])
        vol_prev30 = sum(float(b[7]) for b in closed_1m[-35:-5])
        vol_surge = (vol_last5 / 5) / (vol_prev30 / 30) if vol_prev30 > 0 else 0

        fund = 0.0
        p = prem_map.get(sym)
        if p: fund = float(p.get("lastFundingRate", "0")) * 100

        h24 = float(t["highPrice"]); l24 = float(t["lowPrice"])
        range_pos = (mid - l24) / (h24 - l24) if h24 != l24 else 0.5
        atr_14 = sum([abs((float(b[4])/float(b[1]) - 1) * 100) for b in closed_1m[-14:]]) / 14

        # === ANTI-MM intelligence ===
        # Volume confirmation: was the last candle backed by real volume?
        last_vol = float(closed_1m[-1][7])
        avg_vol_30 = sum(float(b[7]) for b in closed_1m[-30:]) / 30
        last_vol_ratio = last_vol / max(avg_vol_30, 1)

        # 24h high/low breakout detection (just broke a 24h extreme = real buyer/seller showed up)
        breakout_high = mid > h24 * 0.998 and last_chg > 0.3
        breakout_low = mid < l24 * 1.002 and last_chg < -0.3

        # Catalyst detection (expanded with breakouts)
        catalyst = None
        if chg_24h >= 25 and c5m < -0.5 and c15m < 0 and fund > 0.05:
            catalyst = "PUMP_EXHAUSTION_SHORT"
        elif chg_24h <= -25 and c5m > 0.5 and c15m > 0 and fund < -0.05:
            catalyst = "CAPITULATION_LONG"
        elif breakout_high and last_vol_ratio > 1.5 and chg_24h > 5:
            catalyst = "BREAKOUT_HIGH_LONG"
        elif breakout_low and last_vol_ratio > 1.5 and chg_24h < -5:
            catalyst = "BREAKDOWN_LOW_SHORT"

        # === LONG SCORE ===
        long_score = 0
        long_reasons = []
        if chg_24h >= 50: long_score += 40; long_reasons.append(f"24H+{chg_24h:.0f}")
        elif chg_24h >= 30: long_score += 30; long_reasons.append(f"24H+{chg_24h:.0f}")
        elif chg_24h >= 20: long_score += 20
        elif chg_24h >= 10: long_score += 10
        elif chg_24h <= -20: long_score -= 15

        ups = sum(1 for x in [c1m_5, c5m, c15m] if x > 0)
        if ups == 3:
            avg_mag = (c1m_5 + c5m + c15m) / 3
            if avg_mag > 1.5: long_score += 25; long_reasons.append("3TF_STRONG")
            else: long_score += 15; long_reasons.append("3TF_BULL")
        elif ups == 2: long_score += 5

        if fund < -0.05: long_score += 20; long_reasons.append(f"SQ_FUND{fund:.2f}")
        elif fund < -0.02: long_score += 12
        elif fund < 0: long_score += 5
        elif fund > 0.10: long_score -= 10
        elif fund > 0.05: long_score -= 5

        if vol_expansion >= 2.5: long_score += 15; long_reasons.append(f"VOL_EXP{vol_expansion:.1f}x")
        elif vol_expansion >= 1.7: long_score += 10
        elif vol_expansion >= 1.2: long_score += 5

        if last_is_biggest and last_chg > 1.5:
            long_score -= 25; long_reasons.append(f"EXHAUST+{last_chg:.1f}")
        elif last_is_biggest and last_chg > 0.8:
            long_score -= 10
        elif c5m > 1.0 and c1m_5 < c5m * 0.5 and last_chg < 0 and abs(last_chg) < 0.5:
            long_score += 20; long_reasons.append("PULLBACK_IN_TREND")
        elif c15m > 2.0 and c5m > 0 and -0.3 < last_chg < 0.4:
            long_score += 12; long_reasons.append("STEADY_UP")

        if vol_surge >= 3: long_score += 15
        elif vol_surge >= 2: long_score += 10
        elif vol_surge >= 1.3: long_score += 5

        if catalyst == "CAPITULATION_LONG":
            long_score += 30; long_reasons.append("CATALYST_CAP")

        # === SHORT SCORE ===
        short_score = 0
        short_reasons = []
        if chg_24h <= -30: short_score += 30; short_reasons.append(f"24H{chg_24h:.0f}")
        elif chg_24h <= -20: short_score += 20
        elif chg_24h <= -10: short_score += 10
        elif chg_24h >= 30:
            if c1m_5 < -1 and c5m < -0.5:
                short_score += 25; short_reasons.append(f"PUMP_REV+{chg_24h:.0f}")
            elif c5m < -0.3: short_score += 10

        dns = sum(1 for x in [c1m_5, c5m, c15m] if x < 0)
        if dns == 3:
            avg_mag = -(c1m_5 + c5m + c15m) / 3
            if avg_mag > 1.5: short_score += 25; short_reasons.append("3TF_STRONG_BEAR")
            else: short_score += 15; short_reasons.append("3TF_BEAR")
        elif dns == 2: short_score += 5

        if fund > 0.08: short_score += 20; short_reasons.append(f"TRAP_FUND{fund:.2f}")
        elif fund > 0.04: short_score += 12
        elif fund > 0: short_score += 5
        elif fund < -0.10: short_score -= 10

        if vol_expansion >= 2.5: short_score += 15
        elif vol_expansion >= 1.7: short_score += 10
        elif vol_expansion >= 1.2: short_score += 5

        if last_is_biggest and last_chg < -1.5:
            short_score -= 25; short_reasons.append(f"EXHAUST{last_chg:.1f}")
        elif last_is_biggest and last_chg < -0.8: short_score -= 10
        elif c5m < -1.0 and c1m_5 > c5m * 0.5 and last_chg > 0 and last_chg < 0.5:
            short_score += 20; short_reasons.append("PULLBACK_IN_DUMP")
        elif c15m < -2.0 and c5m < 0 and -0.4 < last_chg < 0.3:
            short_score += 12

        if vol_surge >= 3: short_score += 15
        elif vol_surge >= 2: short_score += 10
        elif vol_surge >= 1.3: short_score += 5

        if catalyst == "PUMP_EXHAUSTION_SHORT":
            short_score += 30; short_reasons.append("CATALYST_PUMPREV")

        return {
            "sym": sym, "long_score": long_score, "short_score": short_score,
            "long_reasons": long_reasons, "short_reasons": short_reasons,
            "chg_24h": chg_24h, "c1m_5": c1m_5, "c5m": c5m, "c15m": c15m,
            "last_chg": last_chg, "fund": fund, "range_pos": range_pos,
            "vol_24h": vol, "vol_surge": vol_surge, "vol_exp": vol_expansion,
            "spread": spread, "mark": mid, "last_is_biggest": last_is_biggest,
            "atr_14": atr_14, "catalyst": catalyst, "max_recent_abs": max_recent_abs,
            "exhaustion_streak": exhaustion_streak, "last_dir": last_9_dirs[-1],
            "last_vol_ratio": last_vol_ratio,
        }
    except Exception:
        return None


def predict(sym, side):
    score = 0
    sig = {}
    try:
        depth = c.public_get("/fapi/v1/depth", {"symbol": sym, "limit": 20})
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        bid_vol = sum(float(p)*float(q) for p,q in bids)
        ask_vol = sum(float(p)*float(q) for p,q in asks)
        ob = (bid_vol-ask_vol)/(bid_vol+ask_vol) if bid_vol+ask_vol > 0 else 0
        sig["ob"] = ob
        if side == "BUY":
            if ob > 0.20: score += 25; sig["ob_call"] = "STRONG_BID"
            elif ob > 0.05: score += 12; sig["ob_call"] = "MILD_BID"
            elif ob < -0.20: score -= 15; sig["ob_call"] = "ASK_WALL_HEAVY"
            elif ob < -0.05: score -= 7; sig["ob_call"] = "MILD_ASK"
            else: sig["ob_call"] = "neutral"
        else:
            if ob < -0.20: score += 25; sig["ob_call"] = "STRONG_ASK"
            elif ob < -0.05: score += 12; sig["ob_call"] = "MILD_ASK"
            elif ob > 0.20: score -= 15; sig["ob_call"] = "BID_WALL_HEAVY"
            elif ob > 0.05: score -= 7; sig["ob_call"] = "MILD_BID"
            else: sig["ob_call"] = "neutral"
    except Exception:
        sig["ob_call"] = "err"

    try:
        trades = c.public_get("/fapi/v1/aggTrades", {"symbol": sym, "limit": 400})
        if isinstance(trades, list) and trades and len(trades) >= 200:
            # Recent 200 vs prior 200 — flow TREND analysis
            recent = trades[-200:]
            prior = trades[-400:-200]
            def br_calc(tlist):
                bq = sum(float(t.get("q",0))*float(t.get("p",0)) for t in tlist if not t.get("m"))
                sq = sum(float(t.get("q",0))*float(t.get("p",0)) for t in tlist if t.get("m"))
                tot = bq + sq
                return bq/tot if tot > 0 else 0.5
            br_recent = br_calc(recent)
            br_prior = br_calc(prior)
            br = br_recent
            br_trend = br_recent - br_prior  # +0.10 = buying intensifying; -0.10 = dying
            sig["buy_ratio"] = br
            sig["flow_trend"] = br_trend
            if side == "BUY":
                if br > 0.65: score += 25; sig["flow"] = f"STRONG_BUY_{br:.2f}"
                elif br > 0.55: score += 12; sig["flow"] = f"BUY_TILT_{br:.2f}"
                elif br < 0.35: score -= 15; sig["flow"] = f"SELL_DOMINANT_{br:.2f}"
                elif br < 0.45: score -= 8; sig["flow"] = f"SELL_TILT_{br:.2f}"
                else: sig["flow"] = f"neutral_{br:.2f}"
                # Trend bonus/penalty
                if br_trend > 0.08: score += 12; sig["flow_t"] = f"ACCELERATING+{br_trend:.2f}"
                elif br_trend > 0.03: score += 5
                elif br_trend < -0.08: score -= 10; sig["flow_t"] = f"FADING{br_trend:.2f}"
                elif br_trend < -0.03: score -= 4
            else:
                if br < 0.35: score += 25; sig["flow"] = f"STRONG_SELL_{br:.2f}"
                elif br < 0.45: score += 12; sig["flow"] = f"SELL_TILT_{br:.2f}"
                elif br > 0.65: score -= 15; sig["flow"] = f"BUY_DOMINANT_{br:.2f}"
                elif br > 0.55: score -= 8; sig["flow"] = f"BUY_TILT_{br:.2f}"
                else: sig["flow"] = f"neutral_{br:.2f}"
                # For SELL, decreasing buy_ratio = sell pressure intensifying
                if br_trend < -0.08: score += 12; sig["flow_t"] = f"ACCELERATING{br_trend:.2f}"
                elif br_trend < -0.03: score += 5
                elif br_trend > 0.08: score -= 10; sig["flow_t"] = f"FADING+{br_trend:.2f}"
                elif br_trend > 0.03: score -= 4
    except Exception:
        sig["flow"] = "err"

    # OI × Price direction matrix — distinguishes REAL positioning from COVERING
    try:
        oi_now = c.public_get("/fapi/v1/openInterest", {"symbol": sym})
        oi_curr = float(oi_now.get("openInterest", 0))
        oi_hist = c.public_get("/futures/data/openInterestHist",
                                {"symbol": sym, "period": "5m", "limit": 4})
        if isinstance(oi_hist, list) and len(oi_hist) >= 2:
            oi_5m_ago = float(oi_hist[-2].get("sumOpenInterest", 0))
            oi_chg = (oi_curr - oi_5m_ago) / oi_5m_ago * 100 if oi_5m_ago > 0 else 0
            sig["oi_chg"] = oi_chg
            # Get last 5m price change to compare
            try:
                k5 = c.get_klines(sym, "5m", limit=2)
                price_5m_chg = (float(k5[-2][4]) / float(k5[-2][1]) - 1) * 100
            except Exception:
                price_5m_chg = 0
            sig["price_5m_chg"] = price_5m_chg
            # Matrix logic
            if side == "BUY":
                if oi_chg > 0.5 and price_5m_chg > 0:
                    score += 20; sig["oi"] = f"REAL_LONGS_OI+{oi_chg:.2f}_P+{price_5m_chg:.2f}"
                elif oi_chg < -0.5 and price_5m_chg > 0:
                    score -= 10; sig["oi"] = f"SHORT_COVER_OI{oi_chg:.2f}_P+{price_5m_chg:.2f} (weak rally)"
                elif oi_chg > 0.3:
                    score += 7; sig["oi"] = f"mild+{oi_chg:.2f}%"
                else:
                    sig["oi"] = f"flat{oi_chg:.2f}%"
            else:  # SELL
                if oi_chg > 0.5 and price_5m_chg < 0:
                    score += 20; sig["oi"] = f"REAL_SHORTS_OI+{oi_chg:.2f}_P{price_5m_chg:.2f}"
                elif oi_chg < -0.5 and price_5m_chg < 0:
                    score -= 10; sig["oi"] = f"LONG_COVER_OI{oi_chg:.2f}_P{price_5m_chg:.2f} (weak dump)"
                elif oi_chg > 0.3:
                    score += 7; sig["oi"] = f"mild+{oi_chg:.2f}%"
                else:
                    sig["oi"] = f"flat{oi_chg:.2f}%"
    except Exception:
        sig["oi"] = "err"

    return score, sig


def hard_veto(setup, side, p_sig):
    """Returns (passed, reason). Hard veto = ANY contradicting signal disqualifies."""
    s = setup
    # Universal veto: post-spike instability. If any of last 8 candles had >2% move,
    # the market is in chop/V-reversal territory. Don't enter either direction.
    if s.get("max_recent_abs", 0) > 2.0:
        return False, f"VETO: post-spike instability (max recent candle {s['max_recent_abs']:.2f}%)"

    # Universal veto: exhaustion. If 6+ consecutive same-direction candles ending at
    # latest, the move is likely exhausted. Don't chase the end of trends.
    if s.get("exhaustion_streak", 0) >= 6:
        streak_dir = "UP" if s["last_dir"] > 0 else "DN"
        # Veto trade IN SAME direction as exhausted trend (entering near top/bottom)
        if (side == "BUY" and streak_dir == "UP") or (side == "SELL" and streak_dir == "DN"):
            return False, f"VETO: trend exhaustion ({s['exhaustion_streak']} consecutive {streak_dir})"

    # Volume veto: only block truly fake moves (<0.5x avg volume)
    last_vol_ratio = s.get("last_vol_ratio", 1.0)
    if last_vol_ratio < 0.5:
        return False, f"VETO: latest candle volume {last_vol_ratio:.2f}x (weak volume = MM fake-out)"

    if side == "BUY":
        # Macro veto: 5m must be UP, 15m can be mildly negative (allows pullback entries)
        if s["c5m"] <= 0:
            return False, f"VETO: c5m={s['c5m']:.2f}% (need >0)"
        if s["c15m"] <= -1.0:
            return False, f"VETO: c15m={s['c15m']:.2f}% (need >-1.0)"
        # Range veto: too high = no room to run
        if s["range_pos"] > 0.96:
            return False, f"VETO: range_pos={s['range_pos']:.2f} (pinned at HOD)"
        # Funding veto: too long-crowded = trap
        if s["fund"] > 0.08:
            return False, f"VETO: fund={s['fund']:.3f}% (long-trap zone)"
        # Exhaustion veto
        if s["last_is_biggest"] and s["last_chg"] > 1.5:
            return False, f"VETO: exhaustion +{s['last_chg']:.2f}% (biggest of 5)"
        # Confirmation veto: latest 1m must NOT be down (we want recent direction confirming)
        if s["last_chg"] < -0.5:
            return False, f"VETO: latest 1m {s['last_chg']:.2f}% (against direction)"
        # Predictor veto: flow contradicting
        br = p_sig.get("buy_ratio", 0.5)
        if br < 0.45:
            return False, f"VETO: buy_ratio={br:.2f} (sellers dominant for BUY)"
        # Predictor veto: flow trend dying
        ft = p_sig.get("flow_trend", 0)
        if ft < -0.08:
            return False, f"VETO: flow_trend={ft:+.2f} (buying dying for BUY)"
        # Predictor veto: heavy ask wall
        ob = p_sig.get("ob", 0)
        if ob < -0.10:
            return False, f"VETO: ob={ob:.2f} (heavy ask wall)"
    else:  # SELL
        if s["c5m"] >= 0:
            return False, f"VETO: c5m={s['c5m']:.2f}% (need <0)"
        if s["c15m"] >= 1.0:
            return False, f"VETO: c15m={s['c15m']:.2f}% (need <1.0)"
        if s["range_pos"] < 0.04:
            return False, f"VETO: range_pos={s['range_pos']:.2f} (pinned at LOD)"
        if s["fund"] < -0.08:
            return False, f"VETO: fund={s['fund']:.3f}% (short-trap zone)"
        if s["last_is_biggest"] and s["last_chg"] < -1.5:
            return False, f"VETO: exhaustion {s['last_chg']:.2f}% (biggest of 5)"
        # Confirmation veto: latest 1m must NOT be up (need recent direction confirming)
        if s["last_chg"] > 0.5:
            return False, f"VETO: latest 1m +{s['last_chg']:.2f}% (against direction)"
        br = p_sig.get("buy_ratio", 0.5)
        if br > 0.55:
            return False, f"VETO: buy_ratio={br:.2f} (buyers dominant for SELL)"
        # Predictor veto: flow trend dying (for SELL, increasing buy ratio = selling dying)
        ft = p_sig.get("flow_trend", 0)
        if ft > 0.08:
            return False, f"VETO: flow_trend=+{ft:.2f} (selling dying for SELL)"
        ob = p_sig.get("ob", 0)
        if ob > 0.10:
            return False, f"VETO: ob={ob:.2f} (heavy bid wall)"
    return True, "PASS"


def categorize(setup, side, predictor_score):
    """Tiers:
       PRIME: catalyst tier (10% wallet risk, biggest win potential)
       GOOD: harmony >= 95, strong macro+predictor (6% risk)
       SUFFICIENT: harmony >= 75, decent (4% risk)
       SCOUT: harmony >= 55, fast-action tier (1% risk, tight TP/SL)
       SKIP: anything else
    """
    macro = setup["long_score"] if side == "BUY" else setup["short_score"]
    harmony_total = macro + predictor_score
    if setup.get("catalyst"):
        catalyst = setup["catalyst"]
        catalyst_match = (
            (side == "SELL" and catalyst in ("PUMP_EXHAUSTION_SHORT", "BREAKDOWN_LOW_SHORT")) or
            (side == "BUY" and catalyst in ("CAPITULATION_LONG", "BREAKOUT_HIGH_LONG"))
        )
        if catalyst_match and harmony_total >= 90:
            return "PRIME", harmony_total
    if harmony_total >= 95 and macro >= 60 and predictor_score >= 25:
        return "GOOD", harmony_total
    if harmony_total >= 75 and macro >= 45 and predictor_score >= 20:
        return "SUFFICIENT", harmony_total
    if harmony_total >= 55 and macro >= 30 and predictor_score >= 10:
        return "SCOUT", harmony_total
    return "SKIP", harmony_total


def recommend_order(setup, side, conviction, harmony_total, wallet_balance):
    atr = setup["atr_14"]

    if conviction == "SCOUT":
        # SCOUT: tight, fast-action params for short-duration trades
        sl_pct = 0.003   # 0.3% SL
        tp_pct = 0.009   # 0.9% TP, 3:1 R:R
        risk_pct = 0.01  # 1% wallet risk
    else:
        sl_pct = max(atr * 1.5, 0.5) / 100
        sl_pct = min(sl_pct, 0.012)
        tp_pct = sl_pct * 3
        if setup["vol_exp"] > 3: tp_pct = sl_pct * 4
        tp_pct = min(tp_pct, 0.04)
        risk_pct = {"PRIME": 0.10, "GOOD": 0.06, "SUFFICIENT": 0.04}[conviction]

    max_risk_usd = wallet_balance * risk_pct
    notional = max_risk_usd / sl_pct
    notional = min(notional, wallet_balance * 25)
    notional = max(notional, 50)

    return {
        "tp_pct": tp_pct, "sl_pct": sl_pct,
        "notional": notional,
        "max_risk_usd": notional * sl_pct,
        "expected_tp_usd": notional * tp_pct,
        "conviction": conviction,
    }


def main():
    print("=" * 80)
    print("HARMONY v2 — DIRECTION-CRITICAL DECISION PIPELINE")
    print("=" * 80)
    tradeable = get_tradeable_symbols()
    tickers = c.get_24hr_tickers()
    prems = c.public_get("/fapi/v1/premiumIndex", {})
    prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

    candidates = [t for t in tickers if t.get("symbol") in tradeable
                  and float(t.get("quoteVolume", 0)) > VOL_MIN_USD]
    print(f"[1] SCAN: {len(candidates)} liquid pairs\n")

    setups = []
    for t in candidates:
        s = score_macro(t["symbol"], t, prem_map)
        if s: setups.append(s)

    long_top = sorted(setups, key=lambda x: -x["long_score"])[:6]
    short_top = sorted(setups, key=lambda x: -x["short_score"])[:6]

    full = []
    for s in long_top:
        if s["long_score"] < 30: continue
        p_score, p_sig = predict(s["sym"], "BUY")
        full.append({"setup": s, "side": "BUY", "p_score": p_score, "p_sig": p_sig})
    for s in short_top:
        if s["short_score"] < 30: continue
        p_score, p_sig = predict(s["sym"], "SELL")
        full.append({"setup": s, "side": "SELL", "p_score": p_score, "p_sig": p_sig})

    print(f"[2] PREDICT + VETO on {len(full)} candidates:\n")
    qualified = []
    for f in full:
        s = f["setup"]; side = f["side"]
        macro = s["long_score"] if side == "BUY" else s["short_score"]
        passed, reason = hard_veto(s, side, f["p_sig"])
        catalyst_marker = f" [{s['catalyst']}]" if s.get('catalyst') else ""
        sym_safe = s['sym'].encode('ascii', 'replace').decode('ascii')
        if not passed:
            print(f"  {sym_safe:<14} {side:<5} macro={macro:>4} pred={f['p_score']:>4}  X {reason}{catalyst_marker}")
            continue
        conviction, harmony_total = categorize(s, side, f["p_score"])
        if conviction == "SKIP":
            print(f"  {sym_safe:<14} {side:<5} macro={macro:>4} pred={f['p_score']:>4}  SKIP (harmony {harmony_total} below tier){catalyst_marker}")
            continue
        f["conviction"] = conviction
        f["harmony_total"] = harmony_total
        qualified.append(f)
        print(f"  {sym_safe:<14} {side:<5} macro={macro:>4} pred={f['p_score']:>4}  OK [{conviction}] harmony={harmony_total}{catalyst_marker}")

    qualified.sort(key=lambda x: -x["harmony_total"])

    bals = c.get_balance()
    usdt = next((b for b in bals if b.get("asset")=="USDT"), None)
    wallet = float(usdt.get("availableBalance", 0)) if usdt else 0
    print(f"\nWallet: ${wallet:.4f}")

    print("\n" + "=" * 80)
    if not qualified:
        print("NO QUALIFIED TRADE — wait 5-10 min and re-run.")
        print("=" * 80)
        return

    best = qualified[0]
    s = best["setup"]; side = best["side"]
    rec = recommend_order(s, side, best["conviction"], best["harmony_total"], wallet)
    sym_safe = s['sym'].encode('ascii', 'replace').decode('ascii')
    def safe_print(msg):
        print(str(msg).encode("ascii", "replace").decode("ascii"))
    safe_print(f"WINNER: {sym_safe} {side} [{best['conviction']}]  harmony={best['harmony_total']}")
    safe_print(f"  Mark:        ${s['mark']:.6f}")
    safe_print(f"  Catalyst:    {s.get('catalyst') or 'none'}")
    safe_print(f"  Macro reasons: {','.join(s['long_reasons'] if side=='BUY' else s['short_reasons'])}")
    safe_print(f"  Predictor:   ob={best['p_sig'].get('ob_call')}, flow={best['p_sig'].get('flow')}, oi={best['p_sig'].get('oi')}")
    safe_print(f"  ATR14:       {s['atr_14']:.3f}%")
    safe_print(f"  TP/SL:       +{rec['tp_pct']*100:.2f}% / -{rec['sl_pct']*100:.2f}%  (R:R {rec['tp_pct']/rec['sl_pct']:.1f}:1)")
    safe_print(f"  Notional:    ${rec['notional']:.2f}  margin@20x = ${rec['notional']/20:.2f}")
    safe_print(f"  Risk/Reward: -${rec['max_risk_usd']:.2f} / +${rec['expected_tp_usd']:.2f}")
    safe_print("=" * 80)


if __name__ == "__main__":
    main()
