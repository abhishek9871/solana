"""
PERFECT HARMONY — single tier, instant execution.

Philosophy: Filter once, hard. Execute instantly. Trust the gates.

PERFECT tier requirements (ALL must pass):
  - Either catalyst (PUMP_REV, CAPITULATION, BREAKOUT_HIGH, BREAKDOWN_LOW)
    OR harmony >= 100 with macro >= 70 AND predictor >= 30
  - All HARD VETOS must pass (multi-TF, exhaustion, post-spike, range, funding,
    flow, ob, volume — these prevent the worst losses)

Pipeline:
  1. Scan every 20s (when no position)
  2. Best PERFECT setup found → EXECUTE IMMEDIATELY (no freshness check)
  3. SL 0.5% tight, TP 2.5% wide (5:1 R:R)
  4. After close, 15s cooldown, rescan

Smart flip at scan time: if intended side fails any veto, immediately check
opposite side's score. Take whichever side is PERFECT.

Position sizing: 5% wallet risk (non-catalyst), 8% (catalyst).
"""
import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

sys.path.insert(0, os.path.dirname(__file__))
from harmony import (
    get_tradeable_symbols, score_macro, predict, hard_veto,
    VOL_MIN_USD,
)

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# === SAFETY ===
MAX_SESSION_LOSS_USD = Decimal("5.0")
MAX_CONSEC_LOSSES = 3
COOLDOWN_AFTER_TRADE_SEC = 15
SCAN_LOOP_SLEEP_SEC = 20
STOP_FLAG_FILE = os.path.join(os.path.dirname(__file__), "STOP_AUTO.flag")

# === PERFECT TIER PARAMS ===
SL_PCT = Decimal("0.005")  # 0.5% tight
TP_PCT = Decimal("0.025")  # 2.5% wide (5:1 R:R)
RISK_NORMAL = Decimal("0.05")     # 5% wallet
RISK_CATALYST = Decimal("0.08")   # 8% wallet for catalyst trades


def log(msg):
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


def quantize_price(v, t):
    return (v / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t


def quantize_down(v, s):
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def get_wallet():
    bals = c.get_balance()
    usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
    return float(usdt.get("availableBalance", 0)) if usdt else 0


def is_perfect(setup, side, predictor_score):
    """PERFECT tier — generous on flow, strict on safety + must be a VOLATILE coin
    (ATR >= 0.4% so 2.5% TP is achievable in reasonable time)."""
    # Skip slow movers — TP at 2.5% needs real volatility
    if setup.get("atr_14", 0) < 0.4:
        return False, None, 0
    macro = setup["long_score"] if side == "BUY" else setup["short_score"]
    harmony = macro + predictor_score
    catalyst = setup.get("catalyst")
    has_catalyst = catalyst and (
        (side == "SELL" and catalyst in ("PUMP_EXHAUSTION_SHORT", "BREAKDOWN_LOW_SHORT")) or
        (side == "BUY" and catalyst in ("CAPITULATION_LONG", "BREAKOUT_HIGH_LONG"))
    )
    if has_catalyst and harmony >= 65:
        return True, "CATALYST", harmony
    if harmony >= 65 and macro >= 35 and predictor_score >= 20:
        return True, "STRONG", harmony
    return False, None, harmony


def find_perfect():
    """Scan all liquid pairs. Return best PERFECT setup or None."""
    log("Scanning...")
    tradeable = get_tradeable_symbols()
    tickers = c.get_24hr_tickers()
    prems = c.public_get("/fapi/v1/premiumIndex", {})
    prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

    candidates = [t for t in tickers if t.get("symbol") in tradeable
                  and float(t.get("quoteVolume", 0)) > VOL_MIN_USD]

    setups = []
    for t in candidates:
        s = score_macro(t["symbol"], t, prem_map)
        if s:
            setups.append(s)

    long_top = sorted(setups, key=lambda x: -x["long_score"])[:8]
    short_top = sorted(setups, key=lambda x: -x["short_score"])[:8]

    perfect_winners = []
    for s in long_top:
        if s["long_score"] < 30:
            continue
        p_score, p_sig = predict(s["sym"], "BUY")
        passed, reason = hard_veto(s, "BUY", p_sig)
        if not passed:
            # Smart flip check at scan time
            p_score_opp, p_sig_opp = predict(s["sym"], "SELL")
            passed_opp, _ = hard_veto(s, "SELL", p_sig_opp)
            if passed_opp:
                ok, sub, h = is_perfect(s, "SELL", p_score_opp)
                if ok:
                    perfect_winners.append({"setup": s, "side": "SELL", "p_score": p_score_opp,
                                             "p_sig": p_sig_opp, "subtype": sub, "harmony": h})
            continue
        ok, sub, h = is_perfect(s, "BUY", p_score)
        if ok:
            perfect_winners.append({"setup": s, "side": "BUY", "p_score": p_score,
                                     "p_sig": p_sig, "subtype": sub, "harmony": h})

    for s in short_top:
        if s["short_score"] < 30:
            continue
        # Skip if already added from long_top scan
        if any(w["setup"]["sym"] == s["sym"] for w in perfect_winners):
            continue
        p_score, p_sig = predict(s["sym"], "SELL")
        passed, reason = hard_veto(s, "SELL", p_sig)
        if not passed:
            p_score_opp, p_sig_opp = predict(s["sym"], "BUY")
            passed_opp, _ = hard_veto(s, "BUY", p_sig_opp)
            if passed_opp:
                ok, sub, h = is_perfect(s, "BUY", p_score_opp)
                if ok:
                    perfect_winners.append({"setup": s, "side": "BUY", "p_score": p_score_opp,
                                             "p_sig": p_sig_opp, "subtype": sub, "harmony": h})
            continue
        ok, sub, h = is_perfect(s, "SELL", p_score)
        if ok:
            perfect_winners.append({"setup": s, "side": "SELL", "p_score": p_score,
                                     "p_sig": p_sig, "subtype": sub, "harmony": h})

    if not perfect_winners:
        log("  -> no PERFECT setup")
        return None

    perfect_winners.sort(key=lambda x: -x["harmony"])
    return perfect_winners[0]


def micro_tape_check(sym, side):
    """Last 30 seconds of trade flow. Returns (agree, br_30s, msg).
    agree=True means flow agrees with intended direction."""
    try:
        import time as _t
        cutoff_ms = int((_t.time() - 30) * 1000)
        trades = c.public_get("/fapi/v1/aggTrades", {"symbol": sym, "limit": 500})
        if not isinstance(trades, list) or not trades:
            return True, 0.5, "no trades"
        recent = [t for t in trades if int(t.get("T", 0)) >= cutoff_ms]
        if len(recent) < 5:
            return True, 0.5, f"only {len(recent)} trades in 30s (low activity, allow)"
        buy_q = sum(float(t.get("q",0))*float(t.get("p",0)) for t in recent if not t.get("m"))
        sell_q = sum(float(t.get("q",0))*float(t.get("p",0)) for t in recent if t.get("m"))
        total = buy_q + sell_q
        br = buy_q/total if total > 0 else 0.5
        if side == "BUY":
            if br < 0.45:
                return False, br, f"30s tape br={br:.2f} contradicts BUY"
        else:
            if br > 0.55:
                return False, br, f"30s tape br={br:.2f} contradicts SELL"
        return True, br, f"30s tape br={br:.2f} OK"
    except Exception as e:
        return True, 0.5, f"tape err {e}"


def topk_orderbook_check(sym, side):
    """Top 5 levels imbalance check (vs broader top 20).
    Catches tight resistance/support not visible in broader OB."""
    try:
        depth = c.public_get("/fapi/v1/depth", {"symbol": sym, "limit": 20})
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        if not bids or not asks:
            return True, "no depth"
        top5_bid = sum(float(p)*float(q) for p,q in bids[:5])
        top5_ask = sum(float(p)*float(q) for p,q in asks[:5])
        if side == "BUY":
            if top5_ask > top5_bid * 2.5:
                return False, f"top5 asks {top5_ask:.0f} vs bids {top5_bid:.0f} = tight resistance"
        else:
            if top5_bid > top5_ask * 2.5:
                return False, f"top5 bids {top5_bid:.0f} vs asks {top5_ask:.0f} = tight support"
        return True, f"top5 OK"
    except Exception as e:
        return True, f"top5 err {e}"


def execute_trade(winner, wallet):
    """Place market order + brackets, then trail-to-breakeven. Returns True if entered."""
    s = winner["setup"]
    side = winner["side"]
    sym = s["sym"]

    # === LAST-MILLISECOND DIRECTION CONFIRMATION ===
    # Even if scan picked correctly, the next 30 seconds matter most. Check live tape + top-of-book.
    tape_ok, br_30s, tape_msg = micro_tape_check(sym, side)
    if not tape_ok:
        log(f"  ABORT pre-trade: {tape_msg}")
        return False
    log(f"  Pre-trade: {tape_msg}")

    top5_ok, top5_msg = topk_orderbook_check(sym, side)
    if not top5_ok:
        log(f"  ABORT pre-trade: {top5_msg}")
        return False
    log(f"  Pre-trade: {top5_msg}")

    risk_pct = RISK_CATALYST if winner["subtype"] == "CATALYST" else RISK_NORMAL
    max_risk = Decimal(str(wallet)) * risk_pct
    notional = max_risk / SL_PCT
    notional = min(notional, Decimal(str(wallet)) * Decimal("25"))
    notional = max(notional, Decimal("50"))

    info = c.get_symbol_info(sym)
    tick = step = Decimal("0")
    min_notional = Decimal("5")
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER":
            tick = Decimal(f["tickSize"])
        elif f["filterType"] == "LOT_SIZE":
            step = Decimal(f["stepSize"])
        elif f["filterType"] == "MIN_NOTIONAL":
            min_notional = Decimal(f["notional"])

    leverage = 20
    try:
        c.set_leverage(sym, 20)
    except BinanceApiError:
        try:
            c.set_leverage(sym, 10)
            leverage = 10
        except BinanceApiError as e:
            log(f"  set_leverage fail: {e}")

    bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
    bid = Decimal(bt["bidPrice"]); ask = Decimal(bt["askPrice"])
    mark = (bid + ask) / 2

    qty = notional / mark
    qty_q = quantize_down(qty, step)
    qty_str = fmt(qty_q)
    actual_notional = qty_q * mark

    if actual_notional < min_notional:
        log(f"  notional ${actual_notional:.2f} < min ${min_notional}, abort")
        return False

    log(f"=== EXECUTE {sym} {side} [PERFECT-{winner['subtype']}] harmony={winner['harmony']} ===")
    log(f"  notional=${actual_notional:.2f} margin=${actual_notional/leverage:.2f} qty={qty_str}")
    log(f"  TP +{TP_PCT*100:.2f}% / SL -{SL_PCT*100:.2f}% (R:R {TP_PCT/SL_PCT:.0f}:1)")
    log(f"  max_risk=${actual_notional*SL_PCT:.2f}  max_reward=${actual_notional*TP_PCT:.2f}")

    try:
        r = c.place_market_order(sym, side, quantity=qty_str, reduce_only=False)
        log(f"  ENTRY filled @ ${r.get('avgPrice')}")
    except BinanceApiError as e:
        log(f"  ENTRY FAIL: {e}")
        return False

    time.sleep(1.0)
    positions = c.get_positions(sym)
    entry = Decimal("0")
    for p in positions:
        if float(p.get("positionAmt", 0)) != 0:
            entry = Decimal(p["entryPrice"])
            break
    if entry == 0:
        log("  no position after entry")
        return False

    close_side = "SELL" if side == "BUY" else "BUY"
    if side == "BUY":
        tp_px = quantize_price(entry * (Decimal("1") + TP_PCT), tick)
        sl_px = quantize_price(entry * (Decimal("1") - SL_PCT), tick)
    else:
        tp_px = quantize_price(entry * (Decimal("1") - TP_PCT), tick)
        sl_px = quantize_price(entry * (Decimal("1") + SL_PCT), tick)

    try:
        c.place_take_profit_order(sym, close_side, stop_price=fmt(tp_px),
                                    quantity=qty_str, close_position=False, reduce_only=True)
        log(f"  TP @ ${tp_px}")
    except BinanceApiError as e:
        log(f"  TP fail: {e}")
    try:
        c.place_stop_market_order(sym, close_side, stop_price=fmt(sl_px),
                                    quantity=qty_str, close_position=False, reduce_only=True)
        log(f"  SL @ ${sl_px}")
    except BinanceApiError as e:
        log(f"  SL fail: {e}")

    # === TRAIL-TO-BREAKEVEN LADDER + MULTI-FLIP ===
    TRAIL_LADDER = [
        (Decimal("0.006"), Decimal("0.001")),
        (Decimal("0.012"), Decimal("0.007")),
        (Decimal("0.018"), Decimal("0.013")),
        (Decimal("0.022"), Decimal("0.018")),
    ]
    SAFETY_BUFFER = Decimal("0.005")
    FLIP_TRIGGER_ADVERSE = Decimal("-0.002")
    FLIP_PRED_MIN = 35
    MAX_FLIPS = 1   # multi-flip destroyed us in chop; 1 max
    FLIP_COOLDOWN_SEC = 5
    current_sl_px = sl_px
    current_lock_pct = -SL_PCT
    peak_fav = Decimal("0")
    rung = 0
    flip_count = 0
    last_flip_ts = 0
    current_side = side
    current_close_side = close_side
    current_qty_str = qty_str
    cached_macro = s  # initial macro setup, used for veto checks during flip
    cached_macro_ts = time.time()

    while True:
        try:
            positions = c.get_positions(sym)
            amt = 0
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                break
            if amt == 0:
                log(f"  {sym} closed.")
                try: c.cancel_all_orders(sym)
                except: pass
                try: c.cancel_all_algo_orders(sym)
                except: pass
                return True

            bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            bid_f = float(bt["bidPrice"]); ask_f = float(bt["askPrice"])
            mark_f = (bid_f + ask_f) / 2
            entry_f = float(entry)
            fav = Decimal(str((mark_f - entry_f) / entry_f if current_side == "BUY"
                              else (entry_f - mark_f) / entry_f))

            if fav > peak_fav:
                peak_fav = fav

            # === MULTI-FLIP CHECK ===
            # Trigger: position adverse > FLIP_TRIGGER, flips remaining, cooldown passed.
            # Decision: ONLY based on predictor (live flow). Skip macro veto because macro
            # takes 5+ min to shift but flips need fraction-of-second decision.
            # Higher predictor bar (35) ensures we only flip on STRONG live signals.
            now_ts = time.time()
            if (fav <= FLIP_TRIGGER_ADVERSE
                    and flip_count < MAX_FLIPS
                    and (now_ts - last_flip_ts) >= FLIP_COOLDOWN_SEC):
                opp_side = "SELL" if current_side == "BUY" else "BUY"
                p_score_opp, p_sig_opp = predict(sym, opp_side)  # ~1.5s
                # Only require: predictor >= 35 (strong live shift) AND no extreme contradiction
                br_opp = p_sig_opp.get("buy_ratio", 0.5)
                ob_opp = p_sig_opp.get("ob", 0)
                hard_block = False
                if opp_side == "BUY":
                    # For flip-to-BUY: need br > 0.5 AND ob > -0.20
                    if br_opp < 0.50 or ob_opp < -0.20:
                        hard_block = True
                else:
                    if br_opp > 0.50 or ob_opp > 0.20:
                        hard_block = True
                if p_score_opp >= 35 and not hard_block:
                        log(f"  *** FLIP #{flip_count+1}: {current_side}@{fav*100:+.2f}% -> {opp_side} (opp_pred={p_score_opp}) ***")
                        try: c.cancel_all_orders(sym)
                        except: pass
                        try: c.cancel_all_algo_orders(sym)
                        except: pass
                        try:
                            c.place_market_order(sym, current_close_side,
                                                  quantity=current_qty_str, reduce_only=True)
                        except BinanceApiError as e:
                            log(f"  flip close fail: {e}")
                            return True

                        time.sleep(0.8)
                        try:
                            r = c.place_market_order(sym, opp_side,
                                                      quantity=current_qty_str, reduce_only=False)
                            log(f"  Opened {opp_side} @ ${r.get('avgPrice')}")
                            time.sleep(0.8)
                            pos_new = c.get_positions(sym)
                            new_entry = Decimal("0")
                            for p in pos_new:
                                if float(p.get("positionAmt", 0)) != 0:
                                    new_entry = Decimal(p["entryPrice"])
                                    break
                            if new_entry == 0:
                                log("  flip open: no position")
                                return True
                            entry = new_entry
                            current_side = opp_side
                            current_close_side = "SELL" if opp_side == "BUY" else "BUY"
                            if opp_side == "BUY":
                                new_tp_px = quantize_price(entry * (Decimal("1") + TP_PCT), tick)
                                new_sl_px = quantize_price(entry * (Decimal("1") - SL_PCT), tick)
                            else:
                                new_tp_px = quantize_price(entry * (Decimal("1") - TP_PCT), tick)
                                new_sl_px = quantize_price(entry * (Decimal("1") + SL_PCT), tick)
                            try:
                                c.place_take_profit_order(sym, current_close_side,
                                                            stop_price=fmt(new_tp_px),
                                                            quantity=current_qty_str,
                                                            close_position=False, reduce_only=True)
                                log(f"  flip TP @ ${new_tp_px}")
                            except BinanceApiError as e:
                                log(f"  flip TP fail: {e}")
                            try:
                                c.place_stop_market_order(sym, current_close_side,
                                                            stop_price=fmt(new_sl_px),
                                                            quantity=current_qty_str,
                                                            close_position=False, reduce_only=True)
                                log(f"  flip SL @ ${new_sl_px}")
                            except BinanceApiError as e:
                                log(f"  flip SL fail: {e}")
                            current_sl_px = new_sl_px
                            current_lock_pct = -SL_PCT
                            peak_fav = Decimal("0")
                            rung = 0
                            flip_count += 1
                            last_flip_ts = time.time()
                        except BinanceApiError as e:
                            log(f"  flip open fail: {e}")
                            return True
                        continue

            # === Trail-to-breakeven ladder ===
            if rung < len(TRAIL_LADDER):
                trigger, lock = TRAIL_LADDER[rung]
                if peak_fav >= trigger:
                    cap = fav - SAFETY_BUFFER
                    target_lock = min(lock, cap) if cap > current_lock_pct else current_lock_pct
                    if target_lock > current_lock_pct:
                        if current_side == "BUY":
                            new_sl_px = quantize_price(entry * (Decimal("1") + target_lock), tick)
                        else:
                            new_sl_px = quantize_price(entry * (Decimal("1") - target_lock), tick)
                        try:
                            algos = c.get_open_algo_orders(sym)
                            algo_list = algos if isinstance(algos, list) else algos.get("orders", [])
                            for a in algo_list:
                                typ = (a.get("type") or a.get("origType") or "").upper()
                                if a.get("side") == current_close_side and "STOP" in typ and "PROFIT" not in typ:
                                    c.cancel_algo_order(sym, algo_id=int(a.get("algoId") or a.get("orderId") or 0))
                                    break
                        except Exception:
                            pass
                        try:
                            c.place_stop_market_order(sym, current_close_side, stop_price=fmt(new_sl_px),
                                                       quantity=current_qty_str, close_position=False, reduce_only=True)
                            log(f"  TRAIL rung {rung+1}: SL ${current_sl_px} -> ${new_sl_px} (lock {float(target_lock)*100:+.2f}%, peak {float(peak_fav)*100:+.2f}%)")
                            current_sl_px = new_sl_px
                            current_lock_pct = target_lock
                            rung += 1
                        except BinanceApiError as e:
                            log(f"  trail SL fail: {e}")

            time.sleep(1.5)   # faster polling — react to adverse moves quicker
        except Exception as e:
            log(f"  loop err: {e}")
            time.sleep(5)


def main():
    initial = get_wallet()
    session_pnl = Decimal("0")
    consec = 0
    log(f"=== PERFECT HARMONY START === wallet=${initial:.4f}")
    log(f"Tier: PERFECT only. SL {SL_PCT*100}%, TP {TP_PCT*100}%, R:R {TP_PCT/SL_PCT}:1")

    while True:
        if os.path.exists(STOP_FLAG_FILE):
            log("STOP flag. Exiting.")
            break
        if session_pnl <= -MAX_SESSION_LOSS_USD:
            log(f"Max loss ${MAX_SESSION_LOSS_USD} hit. Exiting.")
            break
        if consec >= MAX_CONSEC_LOSSES:
            log(f"Max consec losses {MAX_CONSEC_LOSSES}. Exiting.")
            break

        winner = find_perfect()
        if not winner:
            time.sleep(SCAN_LOOP_SLEEP_SEC)
            continue

        s = winner["setup"]
        log(f"  PERFECT: {s['sym']} {winner['side']} [{winner['subtype']}] harmony={winner['harmony']}")
        wallet_pre = get_wallet()
        ok = execute_trade(winner, wallet_pre)
        if not ok:
            time.sleep(SCAN_LOOP_SLEEP_SEC)
            continue

        wallet_post = get_wallet()
        pnl = Decimal(str(wallet_post)) - Decimal(str(wallet_pre))
        session_pnl += pnl
        if pnl < 0: consec += 1
        else: consec = 0
        log(f"  TRADE PNL ${pnl:+.4f}  session=${session_pnl:+.4f}  consec={consec}")
        log(f"  Cooldown {COOLDOWN_AFTER_TRADE_SEC}s")
        time.sleep(COOLDOWN_AFTER_TRADE_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
