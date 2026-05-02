"""
PERFECT HARMONY MULTI — concurrent multi-position auto-trader.

Architecture:
  - Main scanner: continuously finds PERFECT setups, dispatches new positions
  - Position workers: each trade runs in its own thread with full lifecycle
    (entry -> brackets -> trail -> multi-flip -> close)
  - Each worker INDEPENDENT: own trail state, own flip counter
  - Shared state behind lock: active positions, session PnL, recent closes

Safety:
  - Slow-mover filter (ATR >= 0.4%)
  - Pre-trade tape check (last 30s flow)
  - Pre-trade top-5 OB check (no tight wall)
  - Multi-flip max 3 per trade
  - Trail-to-breakeven ladder
  - Same-symbol cooldown 30s after close
  - Session loss limit -$12 -> halt new entries
  - Existing positions ride on server-side TP/SL even if scanner halts
"""
import os
import sys
import time
import threading
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

sys.path.insert(0, os.path.dirname(__file__))
from harmony import (
    get_tradeable_symbols, score_macro, predict, hard_veto,
    VOL_MIN_USD,
)
from perfect_harmony import (
    is_perfect, micro_tape_check, topk_orderbook_check,
)

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# === CONFIG ===
SL_PCT = Decimal("0.005")
TP_PCT = Decimal("0.025")
RISK_PER_TRADE = Decimal("0.04")
TRAIL_POLL_SEC = 2.0
SCAN_INTERVAL_SEC = 12
COOLDOWN_PER_SYMBOL_SEC = 30
MAX_SESSION_LOSS = Decimal("12.0")
FLIP_TRIGGER = Decimal("-0.002")
FLIP_PRED_MIN = 35
MAX_FLIPS = 1   # 1 flip max — multi-flip destroyed us in chop
FLIP_COOLDOWN_SEC = 5
SAFETY_BUFFER = Decimal("0.005")
TRAIL_LADDER = [
    (Decimal("0.006"), Decimal("0.001")),
    (Decimal("0.012"), Decimal("0.007")),
    (Decimal("0.018"), Decimal("0.013")),
    (Decimal("0.022"), Decimal("0.018")),
]
STOP_FLAG_FILE = os.path.join(os.path.dirname(__file__), "STOP_AUTO.flag")

# === SHARED STATE (lock-protected) ===
state_lock = threading.Lock()
active_positions = {}      # sym -> {'thread': Thread, 'started': ts}
recent_closes = {}         # sym -> close_ts
session_pnl_total = Decimal("0")
shutdown_flag = False


def log(msg, tag=""):
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    prefix = f"[{tag}]" if tag else ""
    print(f"[{time.strftime('%H:%M:%S')}]{prefix} {safe}", flush=True)


def quantize_price(v, t):
    return (v / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * t


def quantize_down(v, s):
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def fmt(v):
    return format(v.normalize(), "f") if v != v.to_integral() else format(v, "f")


def get_available_wallet():
    try:
        bals = c.get_balance()
        usdt = next((b for b in bals if b.get("asset") == "USDT"), None)
        return float(usdt.get("availableBalance", 0)) if usdt else 0
    except Exception:
        return 0


def position_worker(winner, wallet_at_open):
    """Runs ONE position lifecycle. Releases active_positions slot on completion."""
    s = winner["setup"]
    side = winner["side"]
    sym = s["sym"]
    tag = f"{sym}-{side}"
    trade_pnl = Decimal("0")
    wallet_pre = Decimal(str(wallet_at_open))

    try:
        # === Pre-trade direction confirmation (tape + top5 OB) ===
        tape_ok, br_30s, tape_msg = micro_tape_check(sym, side)
        if not tape_ok:
            log(f"ABORT pre-trade: {tape_msg}", tag)
            return
        log(f"Pre-trade tape OK ({tape_msg})", tag)

        top5_ok, top5_msg = topk_orderbook_check(sym, side)
        if not top5_ok:
            log(f"ABORT pre-trade: {top5_msg}", tag)
            return
        log(f"Pre-trade top5 OK", tag)

        # === Compute sizing ===
        risk_pct = RISK_PER_TRADE
        max_risk = Decimal(str(wallet_at_open)) * risk_pct
        notional = max_risk / SL_PCT
        notional = max(notional, Decimal("50"))

        # Symbol info
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
                c.set_leverage(sym, 10); leverage = 10
            except BinanceApiError as e:
                log(f"set_leverage err: {e}", tag)

        # Compute qty
        bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
        bid = Decimal(bt["bidPrice"]); ask = Decimal(bt["askPrice"])
        mark = (bid + ask) / 2
        qty = notional / mark
        qty_q = quantize_down(qty, step)
        qty_str = fmt(qty_q)
        actual_notional = qty_q * mark

        if actual_notional < min_notional:
            log(f"notional ${actual_notional:.2f} < min ${min_notional}, abort", tag)
            return

        log(f"=== EXEC notional=${actual_notional:.2f} margin=${actual_notional/leverage:.2f} qty={qty_str} ===", tag)

        # Entry
        try:
            r = c.place_market_order(sym, side, quantity=qty_str, reduce_only=False)
            log(f"ENTRY @ ${r.get('avgPrice')}", tag)
        except BinanceApiError as e:
            log(f"ENTRY FAIL: {e}", tag)
            return

        time.sleep(1.0)
        positions = c.get_positions(sym)
        entry = Decimal("0")
        for p in positions:
            if float(p.get("positionAmt", 0)) != 0:
                entry = Decimal(p["entryPrice"]); break
        if entry == 0:
            log("no position after entry", tag)
            return

        # Brackets
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
            log(f"TP @ ${tp_px}", tag)
        except BinanceApiError as e:
            log(f"TP fail: {e}", tag)
        try:
            c.place_stop_market_order(sym, close_side, stop_price=fmt(sl_px),
                                        quantity=qty_str, close_position=False, reduce_only=True)
            log(f"SL @ ${sl_px}", tag)
        except BinanceApiError as e:
            log(f"SL fail: {e}", tag)

        # === Trail + flip loop ===
        current_sl_px = sl_px
        current_lock_pct = -SL_PCT
        peak_fav = Decimal("0")
        rung = 0
        flip_count = 0
        last_flip_ts = 0
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
                    log("position closed", tag)
                    try: c.cancel_all_orders(sym)
                    except: pass
                    try: c.cancel_all_algo_orders(sym)
                    except: pass
                    break

                bt = c.public_get("/fapi/v1/ticker/bookTicker", {"symbol": sym})
                bid_f = float(bt["bidPrice"]); ask_f = float(bt["askPrice"])
                mark_f = (bid_f + ask_f) / 2
                entry_f = float(entry)
                fav = Decimal(str((mark_f - entry_f) / entry_f if current_side == "BUY"
                                  else (entry_f - mark_f) / entry_f))

                if fav > peak_fav:
                    peak_fav = fav

                # Multi-flip check
                now_ts = time.time()
                if (fav <= FLIP_TRIGGER and flip_count < MAX_FLIPS
                        and (now_ts - last_flip_ts) >= FLIP_COOLDOWN_SEC):
                    opp_side = "SELL" if current_side == "BUY" else "BUY"
                    p_score_opp, p_sig_opp = predict(sym, opp_side)
                    br_opp = p_sig_opp.get("buy_ratio", 0.5)
                    ob_opp = p_sig_opp.get("ob", 0)
                    hard_block = False
                    if opp_side == "BUY":
                        if br_opp < 0.50 or ob_opp < -0.20: hard_block = True
                    else:
                        if br_opp > 0.50 or ob_opp > 0.20: hard_block = True

                    if p_score_opp >= FLIP_PRED_MIN and not hard_block:
                        log(f"*** FLIP #{flip_count+1}: {current_side}@{fav*100:+.2f}% -> {opp_side} (pred={p_score_opp})", tag)
                        try: c.cancel_all_orders(sym)
                        except: pass
                        try: c.cancel_all_algo_orders(sym)
                        except: pass
                        try:
                            c.place_market_order(sym, current_close_side,
                                                  quantity=current_qty_str, reduce_only=True)
                        except BinanceApiError as e:
                            log(f"flip close fail: {e}", tag)
                            break
                        time.sleep(0.8)
                        try:
                            r = c.place_market_order(sym, opp_side,
                                                      quantity=current_qty_str, reduce_only=False)
                            log(f"opened {opp_side} @ ${r.get('avgPrice')}", tag)
                            time.sleep(0.8)
                            pos_new = c.get_positions(sym)
                            new_entry = Decimal("0")
                            for p in pos_new:
                                if float(p.get("positionAmt", 0)) != 0:
                                    new_entry = Decimal(p["entryPrice"]); break
                            if new_entry == 0:
                                log("flip open: no position", tag)
                                break
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
                                log(f"flip TP @ ${new_tp_px}", tag)
                            except BinanceApiError as e:
                                log(f"flip TP fail: {e}", tag)
                            try:
                                c.place_stop_market_order(sym, current_close_side,
                                                            stop_price=fmt(new_sl_px),
                                                            quantity=current_qty_str,
                                                            close_position=False, reduce_only=True)
                                log(f"flip SL @ ${new_sl_px}", tag)
                            except BinanceApiError as e:
                                log(f"flip SL fail: {e}", tag)
                            current_sl_px = new_sl_px
                            current_lock_pct = -SL_PCT
                            peak_fav = Decimal("0")
                            rung = 0
                            flip_count += 1
                            last_flip_ts = time.time()
                        except BinanceApiError as e:
                            log(f"flip open fail: {e}", tag)
                            break
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
                            except Exception:
                                pass
                            try:
                                c.place_stop_market_order(sym, current_close_side, stop_price=fmt(new_sl_px),
                                                           quantity=current_qty_str, close_position=False, reduce_only=True)
                                log(f"TRAIL r{rung+1}: SL ${current_sl_px} -> ${new_sl_px} (lock {float(target_lock)*100:+.2f}%, peak {float(peak_fav)*100:+.2f}%)", tag)
                                current_sl_px = new_sl_px
                                current_lock_pct = target_lock
                                rung += 1
                            except BinanceApiError as e:
                                log(f"trail fail: {e}", tag)

                time.sleep(TRAIL_POLL_SEC)
            except Exception as e:
                log(f"loop err: {e}", tag)
                time.sleep(3)

        # Compute PnL after close
        time.sleep(1.0)
        wallet_post = Decimal(str(get_available_wallet()))
        trade_pnl = wallet_post - wallet_pre

    except Exception as e:
        log(f"WORKER CRASH: {e}", tag)
    finally:
        with state_lock:
            global session_pnl_total
            session_pnl_total += trade_pnl
            recent_closes[sym] = time.time()
            if sym in active_positions:
                del active_positions[sym]
        log(f"WORKER DONE pnl=${trade_pnl:+.4f} session=${session_pnl_total:+.4f}", tag)


def find_perfect_setup():
    """Find best PERFECT setup, excluding active and recently-closed symbols."""
    try:
        tradeable = get_tradeable_symbols()
        with state_lock:
            blocked = set(active_positions.keys())
            now = time.time()
            for sym, ts in list(recent_closes.items()):
                if now - ts < COOLDOWN_PER_SYMBOL_SEC:
                    blocked.add(sym)
                else:
                    del recent_closes[sym]

        tickers = c.get_24hr_tickers()
        prems = c.public_get("/fapi/v1/premiumIndex", {})
        prem_map = {p["symbol"]: p for p in prems} if isinstance(prems, list) else {}

        candidates = [t for t in tickers if t.get("symbol") in tradeable
                      and t.get("symbol") not in blocked
                      and float(t.get("quoteVolume", 0)) > VOL_MIN_USD]

        setups = []
        for t in candidates:
            s = score_macro(t["symbol"], t, prem_map)
            if s: setups.append(s)

        long_top = sorted(setups, key=lambda x: -x["long_score"])[:6]
        short_top = sorted(setups, key=lambda x: -x["short_score"])[:6]

        winners = []
        for s in long_top:
            if s["long_score"] < 30: continue
            p_score, p_sig = predict(s["sym"], "BUY")
            passed, _ = hard_veto(s, "BUY", p_sig)
            if not passed:
                p_score_opp, p_sig_opp = predict(s["sym"], "SELL")
                passed_opp, _ = hard_veto(s, "SELL", p_sig_opp)
                if passed_opp:
                    ok, sub, h = is_perfect(s, "SELL", p_score_opp)
                    if ok:
                        winners.append({"setup": s, "side": "SELL", "p_score": p_score_opp,
                                         "p_sig": p_sig_opp, "subtype": sub, "harmony": h})
                continue
            ok, sub, h = is_perfect(s, "BUY", p_score)
            if ok:
                winners.append({"setup": s, "side": "BUY", "p_score": p_score,
                                 "p_sig": p_sig, "subtype": sub, "harmony": h})
        for s in short_top:
            if s["short_score"] < 30: continue
            if any(w["setup"]["sym"] == s["sym"] for w in winners): continue
            p_score, p_sig = predict(s["sym"], "SELL")
            passed, _ = hard_veto(s, "SELL", p_sig)
            if not passed:
                p_score_opp, p_sig_opp = predict(s["sym"], "BUY")
                passed_opp, _ = hard_veto(s, "BUY", p_sig_opp)
                if passed_opp:
                    ok, sub, h = is_perfect(s, "BUY", p_score_opp)
                    if ok:
                        winners.append({"setup": s, "side": "BUY", "p_score": p_score_opp,
                                         "p_sig": p_sig_opp, "subtype": sub, "harmony": h})
                continue
            ok, sub, h = is_perfect(s, "SELL", p_score)
            if ok:
                winners.append({"setup": s, "side": "SELL", "p_score": p_score,
                                 "p_sig": p_sig, "subtype": sub, "harmony": h})

        winners.sort(key=lambda x: -x["harmony"])
        return winners[0] if winners else None
    except Exception as e:
        log(f"scan err: {e}")
        return None


def main():
    global shutdown_flag, session_pnl_total
    init_w = get_available_wallet()
    log(f"=== PERFECT HARMONY MULTI START === wallet=${init_w:.4f}")
    log(f"Per-trade risk {RISK_PER_TRADE*100}%, TP {TP_PCT*100}%, SL {SL_PCT*100}%, scan {SCAN_INTERVAL_SEC}s")

    while True:
        if os.path.exists(STOP_FLAG_FILE):
            log("STOP flag detected. New entries halted (existing rides server-side).")
            shutdown_flag = True
            break

        with state_lock:
            if session_pnl_total <= -MAX_SESSION_LOSS:
                log(f"Max session loss ${MAX_SESSION_LOSS} hit. Stopping new entries.")
                shutdown_flag = True
                break
            n_active = len(active_positions)

        winner = find_perfect_setup()
        if winner is None:
            with state_lock:
                n_active = len(active_positions)
            time.sleep(SCAN_INTERVAL_SEC)
            continue

        s = winner["setup"]
        sym = s["sym"]

        # Check wallet has margin
        wallet_now = get_available_wallet()
        risk_for_trade = wallet_now * float(RISK_PER_TRADE)
        notional_for_trade = risk_for_trade / float(SL_PCT)
        margin_for_trade = notional_for_trade / 20  # at 20x
        if margin_for_trade < 1.5 or wallet_now < margin_for_trade * 1.5:
            log(f"Insufficient wallet ${wallet_now:.2f} for new trade (need ${margin_for_trade*1.5:.2f})")
            time.sleep(SCAN_INTERVAL_SEC)
            continue

        # Confirm not already taken (race-protect)
        with state_lock:
            if sym in active_positions:
                continue
            t = threading.Thread(target=position_worker, args=(winner, wallet_now), daemon=True)
            active_positions[sym] = {"thread": t, "started": time.time()}
            n_active = len(active_positions)

        log(f"PERFECT: {sym} {winner['side']} [{winner['subtype']}] harmony={winner['harmony']} | active={n_active}")
        t.start()

        # brief pause so next scan picks something different
        time.sleep(8)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted.")
