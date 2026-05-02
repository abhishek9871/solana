"""
JACKPOT MODE — single concentrated bet on rare CATALYST setups only.

Philosophy: stop trading chop. Wait patiently. When a real catalyst emerges
(pump exhaustion, capitulation, breakout, breakdown), commit BIG capital.

Per trade:
  - Position: 15% wallet risk (~$4 risk on $30 wallet)
  - Notional: $4 / 0.5% SL = $800 (margin $40 at 20x)
  - TP +3.5% = +$28
  - SL -0.5% = -$4
  - R:R 7:1

Filters (ALL must hit):
  - Catalyst tag (PUMP_EXHAUSTION_SHORT, CAPITULATION_LONG, BREAKOUT_HIGH_LONG, BREAKDOWN_LOW_SHORT)
  - Harmony >= 95 (very high)
  - Macro >= 60 AND predictor >= 30
  - ATR >= 0.5%
  - Pre-trade tape STRONGLY agrees (br > 0.60 for BUY, < 0.40 for SELL)
  - Pre-trade top5 OB clear

Single position at a time. ONE flip max. After close, scan again.
Max 2 consecutive losses then halt for the day.
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
from perfect_harmony import (
    micro_tape_check, topk_orderbook_check,
)

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# === JACKPOT CONFIG ===
SL_PCT = Decimal("0.005")
TP_PCT = Decimal("0.035")
RISK_PCT = Decimal("0.15")           # 15% wallet per trade (was 4%)
MIN_HARMONY = 95
MIN_MACRO = 60
MIN_PREDICTOR = 30
MIN_ATR = 0.5
TAPE_STRONG_BUY_MIN = 0.60
TAPE_STRONG_SELL_MAX = 0.40
TRAIL_POLL_SEC = 1.5
SCAN_INTERVAL_SEC = 20
MAX_SESSION_LOSS = Decimal("8.0")
MAX_CONSEC = 2
COOLDOWN_AFTER_TRADE = 30
FLIP_TRIGGER = Decimal("-0.002")
FLIP_PRED_MIN = 40
MAX_FLIPS = 1
TRAIL_LADDER = [
    (Decimal("0.008"), Decimal("0.001")),
    (Decimal("0.015"), Decimal("0.009")),
    (Decimal("0.022"), Decimal("0.016")),
    (Decimal("0.030"), Decimal("0.024")),
]
SAFETY_BUFFER = Decimal("0.005")
STOP_FLAG_FILE = os.path.join(os.path.dirname(__file__), "STOP_AUTO.flag")


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


def is_jackpot(setup, side, predictor_score):
    """JACKPOT criteria — only rare catalyst setups."""
    # Volatility floor
    if setup.get("atr_14", 0) < MIN_ATR:
        return False, None, 0
    # Catalyst required
    catalyst = setup.get("catalyst")
    has_catalyst = catalyst and (
        (side == "SELL" and catalyst in ("PUMP_EXHAUSTION_SHORT", "BREAKDOWN_LOW_SHORT")) or
        (side == "BUY" and catalyst in ("CAPITULATION_LONG", "BREAKOUT_HIGH_LONG"))
    )
    if not has_catalyst:
        return False, None, 0
    macro = setup["long_score"] if side == "BUY" else setup["short_score"]
    harmony = macro + predictor_score
    if harmony < MIN_HARMONY: return False, None, harmony
    if macro < MIN_MACRO: return False, None, harmony
    if predictor_score < MIN_PREDICTOR: return False, None, harmony
    return True, catalyst, harmony


def find_jackpot():
    log("Scanning for catalyst...")
    try:
        tradeable = get_tradeable_symbols()
        tickers = c.get_24hr_tickers()
        prems = c.public_get("/fapi/v1/premiumIndex", {})
        prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}
        candidates = [t for t in tickers if t.get("symbol") in tradeable
                      and float(t.get("quoteVolume", 0)) > VOL_MIN_USD]

        winners = []
        for t in candidates:
            s = score_macro(t["symbol"], t, prem_map)
            if not s: continue
            # Only consider if catalyst tag exists
            if not s.get("catalyst"): continue

            # Determine side from catalyst
            cat = s["catalyst"]
            if cat in ("PUMP_EXHAUSTION_SHORT", "BREAKDOWN_LOW_SHORT"):
                side = "SELL"
            elif cat in ("CAPITULATION_LONG", "BREAKOUT_HIGH_LONG"):
                side = "BUY"
            else: continue

            p_score, p_sig = predict(s["sym"], side)
            passed, _ = hard_veto(s, side, p_sig)
            if not passed: continue
            ok, sub, h = is_jackpot(s, side, p_score)
            if not ok: continue
            winners.append({"setup": s, "side": side, "p_score": p_score,
                             "p_sig": p_sig, "subtype": sub, "harmony": h})
        winners.sort(key=lambda x: -x["harmony"])
        if not winners:
            log("  -> no JACKPOT catalyst")
            return None
        return winners[0]
    except Exception as e:
        log(f"  scan err: {e}")
        return None


def execute_jackpot(winner, wallet):
    s = winner["setup"]; side = winner["side"]; sym = s["sym"]

    # Strong tape required for JACKPOT
    tape_ok, br, tape_msg = micro_tape_check(sym, side)
    if not tape_ok:
        log(f"ABORT: {tape_msg}"); return False
    if side == "BUY" and br < TAPE_STRONG_BUY_MIN:
        log(f"ABORT: tape br={br:.2f} not strong enough for BUY (need >{TAPE_STRONG_BUY_MIN})")
        return False
    if side == "SELL" and br > TAPE_STRONG_SELL_MAX:
        log(f"ABORT: tape br={br:.2f} not strong enough for SELL (need <{TAPE_STRONG_SELL_MAX})")
        return False
    log(f"  Tape STRONG: {tape_msg}")

    top5_ok, top5_msg = topk_orderbook_check(sym, side)
    if not top5_ok:
        log(f"ABORT: {top5_msg}"); return False
    log(f"  Top5 OK")

    # Sizing
    max_risk = Decimal(str(wallet)) * RISK_PCT
    notional = max_risk / SL_PCT
    notional = max(notional, Decimal("50"))

    info = c.get_symbol_info(sym)
    tick = step = Decimal("0")
    min_notional = Decimal("5")
    for f in info.get("filters", []):
        if f["filterType"] == "PRICE_FILTER": tick = Decimal(f["tickSize"])
        elif f["filterType"] == "LOT_SIZE": step = Decimal(f["stepSize"])
        elif f["filterType"] == "MIN_NOTIONAL": min_notional = Decimal(f["notional"])

    leverage = 20
    try:
        c.set_leverage(sym, 20)
    except BinanceApiError:
        try:
            c.set_leverage(sym, 10); leverage = 10
        except BinanceApiError as e:
            log(f"  set_leverage err: {e}")

    bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
    mark = (Decimal(bt["bidPrice"]) + Decimal(bt["askPrice"])) / 2
    qty = notional / mark
    qty_q = quantize_down(qty, step)
    qty_str = fmt(qty_q)
    actual_notional = qty_q * mark
    if actual_notional < min_notional:
        log(f"  notional ${actual_notional} < min ${min_notional}"); return False

    log(f"=== JACKPOT EXEC {sym} {side} [{winner['subtype']}] harmony={winner['harmony']} ===")
    log(f"  notional=${actual_notional:.2f} margin=${actual_notional/leverage:.2f} qty={qty_str}")
    log(f"  TP +{TP_PCT*100:.1f}% / SL -{SL_PCT*100:.2f}% = ${actual_notional*TP_PCT:.2f} / -${actual_notional*SL_PCT:.2f}")

    try:
        r = c.place_market_order(sym, side, quantity=qty_str, reduce_only=False)
        log(f"  ENTRY @ ${r.get('avgPrice')}")
    except BinanceApiError as e:
        log(f"  ENTRY FAIL: {e}"); return False

    time.sleep(1.0)
    positions = c.get_positions(sym)
    entry = Decimal("0")
    for p in positions:
        if float(p.get("positionAmt", 0)) != 0:
            entry = Decimal(p["entryPrice"]); break
    if entry == 0:
        log("  no position after entry"); return False

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
    except BinanceApiError as e: log(f"  TP fail: {e}")
    try:
        c.place_stop_market_order(sym, close_side, stop_price=fmt(sl_px),
                                    quantity=qty_str, close_position=False, reduce_only=True)
        log(f"  SL @ ${sl_px}")
    except BinanceApiError as e: log(f"  SL fail: {e}")

    # Trail + 1-flip loop
    current_sl_px = sl_px
    current_lock_pct = -SL_PCT
    peak_fav = Decimal("0")
    rung = 0
    flipped = False
    current_side = side
    current_close_side = close_side
    current_qty_str = qty_str

    while True:
        try:
            positions = c.get_positions(sym)
            amt = 0
            for p in positions:
                amt = float(p.get("positionAmt", 0)); break
            if amt == 0:
                log(f"  {sym} closed.")
                try: c.cancel_all_orders(sym)
                except: pass
                try: c.cancel_all_algo_orders(sym)
                except: pass
                return True

            bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
            mark_f = (float(bt["bidPrice"]) + float(bt["askPrice"])) / 2
            entry_f = float(entry)
            fav = Decimal(str((mark_f - entry_f) / entry_f if current_side == "BUY"
                              else (entry_f - mark_f) / entry_f))
            if fav > peak_fav: peak_fav = fav

            # 1-flip
            if not flipped and fav <= FLIP_TRIGGER:
                opp = "SELL" if current_side == "BUY" else "BUY"
                p_score_opp, p_sig_opp = predict(sym, opp)
                br_opp = p_sig_opp.get("buy_ratio", 0.5)
                ob_opp = p_sig_opp.get("ob", 0)
                hard_block = (
                    (opp == "BUY" and (br_opp < 0.50 or ob_opp < -0.20)) or
                    (opp == "SELL" and (br_opp > 0.50 or ob_opp > 0.20))
                )
                if p_score_opp >= FLIP_PRED_MIN and not hard_block:
                    log(f"  *** FLIP: {current_side}@{fav*100:+.2f}% -> {opp} (pred={p_score_opp})")
                    try: c.cancel_all_orders(sym)
                    except: pass
                    try: c.cancel_all_algo_orders(sym)
                    except: pass
                    try:
                        c.place_market_order(sym, current_close_side, quantity=current_qty_str, reduce_only=True)
                    except BinanceApiError as e:
                        log(f"  flip close fail: {e}"); break
                    time.sleep(0.8)
                    try:
                        r = c.place_market_order(sym, opp, quantity=current_qty_str, reduce_only=False)
                        log(f"  opened {opp} @ ${r.get('avgPrice')}")
                        time.sleep(0.8)
                        pos_new = c.get_positions(sym)
                        new_entry = Decimal("0")
                        for p in pos_new:
                            if float(p.get("positionAmt", 0)) != 0:
                                new_entry = Decimal(p["entryPrice"]); break
                        if new_entry == 0: break
                        entry = new_entry
                        current_side = opp
                        current_close_side = "SELL" if opp == "BUY" else "BUY"
                        if opp == "BUY":
                            new_tp_px = quantize_price(entry * (Decimal("1") + TP_PCT), tick)
                            new_sl_px = quantize_price(entry * (Decimal("1") - SL_PCT), tick)
                        else:
                            new_tp_px = quantize_price(entry * (Decimal("1") - TP_PCT), tick)
                            new_sl_px = quantize_price(entry * (Decimal("1") + SL_PCT), tick)
                        try:
                            c.place_take_profit_order(sym, current_close_side, stop_price=fmt(new_tp_px),
                                                        quantity=current_qty_str, close_position=False, reduce_only=True)
                            log(f"  flip TP @ ${new_tp_px}")
                        except BinanceApiError as e: log(f"  flip TP fail: {e}")
                        try:
                            c.place_stop_market_order(sym, current_close_side, stop_price=fmt(new_sl_px),
                                                        quantity=current_qty_str, close_position=False, reduce_only=True)
                            log(f"  flip SL @ ${new_sl_px}")
                        except BinanceApiError as e: log(f"  flip SL fail: {e}")
                        current_sl_px = new_sl_px
                        current_lock_pct = -SL_PCT
                        peak_fav = Decimal("0")
                        rung = 0
                        flipped = True
                    except BinanceApiError as e:
                        log(f"  flip open fail: {e}"); break
                    continue

            # Trail ladder
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
                        except Exception: pass
                        try:
                            c.place_stop_market_order(sym, current_close_side, stop_price=fmt(new_sl_px),
                                                       quantity=current_qty_str, close_position=False, reduce_only=True)
                            log(f"  TRAIL r{rung+1}: SL ${current_sl_px} -> ${new_sl_px} (lock {float(target_lock)*100:+.2f}%, peak {float(peak_fav)*100:+.2f}%)")
                            current_sl_px = new_sl_px
                            current_lock_pct = target_lock
                            rung += 1
                        except BinanceApiError as e: log(f"  trail fail: {e}")
            time.sleep(TRAIL_POLL_SEC)
        except Exception as e:
            log(f"  loop err: {e}")
            time.sleep(3)


def main():
    log(f"=== JACKPOT MODE START ===")
    log(f"  Catalyst-only. Min harmony {MIN_HARMONY}. Risk {RISK_PCT*100}% wallet/trade. TP +{TP_PCT*100}% / SL -{SL_PCT*100}%.")
    session_pnl = Decimal("0")
    consec = 0
    while True:
        if os.path.exists(STOP_FLAG_FILE):
            log("STOP flag. Exiting."); break
        if session_pnl <= -MAX_SESSION_LOSS:
            log(f"Max loss ${MAX_SESSION_LOSS} hit."); break
        if consec >= MAX_CONSEC:
            log(f"Max consec {MAX_CONSEC} hit."); break

        winner = find_jackpot()
        if not winner:
            time.sleep(SCAN_INTERVAL_SEC); continue

        s = winner["setup"]
        log(f"  JACKPOT: {s['sym']} {winner['side']} [{winner['subtype']}] harmony={winner['harmony']}")
        wallet_pre = Decimal(str(get_wallet()))
        ok = execute_jackpot(winner, float(wallet_pre))
        if not ok:
            time.sleep(SCAN_INTERVAL_SEC); continue
        time.sleep(2)
        wallet_post = Decimal(str(get_wallet()))
        pnl = wallet_post - wallet_pre
        session_pnl += pnl
        if pnl < 0: consec += 1
        else: consec = 0
        log(f"  TRADE PNL ${pnl:+.4f}  session=${session_pnl:+.4f}  consec={consec}")
        log(f"  Cooldown {COOLDOWN_AFTER_TRADE}s")
        time.sleep(COOLDOWN_AFTER_TRADE)


if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: log("Interrupted.")
