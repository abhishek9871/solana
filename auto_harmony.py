"""
AUTO-HARMONY — scans, decides, and executes in one continuous pipeline.
No human-in-the-loop delay between decision and execution.

Loop:
  1. Run harmony scan (PRIME / GOOD / SUFFICIENT tiers) — ~30s
  2. If qualified WINNER found:
     a. INSTANT freshness re-check on that one symbol (~3s)
     b. If all vetos still pass → place market order + TP/SL brackets (~3s)
     c. Wait for position close via server-side brackets
     d. 60s cooldown
  3. If no winner: sleep 90s, rescan

Safety:
  - Max session loss: $5 → stop trading
  - Max consecutive losses: 2 → stop trading
  - Position size capped by harmony recommendation
  - Cooldown after each trade

NOTE: This auto-trades. Stop it via Ctrl+C or by setting STOP_FLAG_FILE.
"""
import os
import sys
import time
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

# Import harmony functions
sys.path.insert(0, os.path.dirname(__file__))
from harmony import (
    get_tradeable_symbols, score_macro, predict, hard_veto, categorize, recommend_order,
    VOL_MIN_USD,
)

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

# === SAFETY LIMITS ===
MAX_SESSION_LOSS_USD = Decimal("5.0")
MAX_CONSEC_LOSSES = 3
COOLDOWN_NORMAL_SEC = 60   # cooldown after PRIME/GOOD/SUFFICIENT trades
COOLDOWN_SCOUT_SEC = 20    # cooldown after SCOUT trades
SCAN_LOOP_SLEEP_SEC = 30   # was 90 — faster rescan when nothing qualifies
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


def fetch_setup(sym, t, prem_map):
    """Lightweight re-scan for ONE symbol — used in freshness check."""
    return score_macro(sym, t, prem_map)


def run_full_scan(wallet):
    """Run full harmony scan. Returns (winner_dict | None)."""
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
    long_top = sorted(setups, key=lambda x: -x["long_score"])[:6]
    short_top = sorted(setups, key=lambda x: -x["short_score"])[:6]

    qualified = []
    for s in long_top:
        if s["long_score"] < 30:
            continue
        p_score, p_sig = predict(s["sym"], "BUY")
        passed, reason = hard_veto(s, "BUY", p_sig)
        if not passed:
            continue
        conviction, harmony_total = categorize(s, "BUY", p_score)
        if conviction == "SKIP":
            continue
        qualified.append({
            "setup": s, "side": "BUY", "p_score": p_score, "p_sig": p_sig,
            "conviction": conviction, "harmony_total": harmony_total,
            "ticker": next(t for t in tickers if t.get("symbol") == s["sym"]),
            "prem_map": prem_map,
        })
    for s in short_top:
        if s["short_score"] < 30:
            continue
        p_score, p_sig = predict(s["sym"], "SELL")
        passed, reason = hard_veto(s, "SELL", p_sig)
        if not passed:
            continue
        conviction, harmony_total = categorize(s, "SELL", p_score)
        if conviction == "SKIP":
            continue
        qualified.append({
            "setup": s, "side": "SELL", "p_score": p_score, "p_sig": p_sig,
            "conviction": conviction, "harmony_total": harmony_total,
            "ticker": next(t for t in tickers if t.get("symbol") == s["sym"]),
            "prem_map": prem_map,
        })

    qualified.sort(key=lambda x: -x["harmony_total"])
    if not qualified:
        log("  -> no qualified trade")
        return None
    return qualified[0]


def freshness_check(winner):
    """Re-fetch single-symbol data, re-run veto.
    On veto failure, IMMEDIATELY check if OPPOSITE side qualifies (smart flip).
    Returns (ok, reason). When ok=True, winner is updated to current direction."""
    sym = winner["setup"]["sym"]
    side = winner["side"]
    log(f"Freshness check on {sym} {side}...")
    try:
        t_list = c.public_get("/fapi/v1/ticker/24hr", {"symbol": sym})
        t = t_list if isinstance(t_list, dict) else t_list[0]
        prems = c.public_get("/fapi/v1/premiumIndex", {"symbol": sym})
        prem_map = {sym: prems} if isinstance(prems, dict) else {p["symbol"]: p for p in prems}
        s_fresh = score_macro(sym, t, prem_map)
        if s_fresh is None:
            return False, "fresh score returned None"

        p_score, p_sig = predict(sym, side)
        passed, reason = hard_veto(s_fresh, side, p_sig)
        if passed:
            conv, ht = categorize(s_fresh, side, p_score)
            if conv != "SKIP":
                # Original side still qualifies
                winner["setup"] = s_fresh
                winner["p_score"] = p_score
                winner["p_sig"] = p_sig
                winner["conviction"] = conv
                macro = s_fresh["long_score"] if side == "BUY" else s_fresh["short_score"]
                winner["harmony_total"] = macro + p_score
                return True, "fresh OK"
            else:
                first_reason = "downgraded to SKIP"
        else:
            first_reason = reason

        # === SMART FLIP === try the opposite side
        opp = "BUY" if side == "SELL" else "SELL"
        log(f"  Original {side} failed: {first_reason}. Trying flip to {opp}...")
        p_score_opp, p_sig_opp = predict(sym, opp)
        passed_opp, reason_opp = hard_veto(s_fresh, opp, p_sig_opp)
        if not passed_opp:
            return False, f"{first_reason}; flip {opp} also fails: {reason_opp}"
        conv_opp, ht_opp = categorize(s_fresh, opp, p_score_opp)
        if conv_opp == "SKIP":
            macro_opp = s_fresh["long_score"] if opp == "BUY" else s_fresh["short_score"]
            return False, f"{first_reason}; flip {opp} below tier (macro={macro_opp}, pred={p_score_opp})"

        # Flip qualifies — take it
        log(f"  *** SMART FLIP to {opp} [{conv_opp}] — opposite side INDEPENDENTLY qualifies ***")
        winner["side"] = opp
        winner["setup"] = s_fresh
        winner["p_score"] = p_score_opp
        winner["p_sig"] = p_sig_opp
        winner["conviction"] = conv_opp
        macro_opp = s_fresh["long_score"] if opp == "BUY" else s_fresh["short_score"]
        winner["harmony_total"] = macro_opp + p_score_opp
        return True, f"FLIPPED to {opp}"
    except Exception as e:
        return False, f"fresh check err: {e}"


def execute_trade(winner, wallet):
    """Place market order + brackets. Returns True if entered, False otherwise."""
    s = winner["setup"]
    side = winner["side"]
    sym = s["sym"]
    rec = recommend_order(s, side, winner["conviction"], winner["harmony_total"], wallet)

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

    # Try max leverage 20x, fallback 10x
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

    notional_dec = Decimal(str(rec["notional"]))
    qty = notional_dec / mark
    qty_q = quantize_down(qty, step)
    qty_str = fmt(qty_q)
    actual_notional = qty_q * mark

    if actual_notional < min_notional:
        log(f"  Notional ${actual_notional:.2f} < min ${min_notional}, abort")
        return False

    log(f"=== EXECUTE {sym} {side} [{winner['conviction']}] ===")
    log(f"  margin=${notional_dec/leverage:.2f} notional=${actual_notional:.2f} qty={qty_str}")
    log(f"  TP={rec['tp_pct']*100:.2f}% SL={rec['sl_pct']*100:.2f}% R:R={rec['tp_pct']/rec['sl_pct']:.1f}:1")
    log(f"  max_risk=${rec['max_risk_usd']:.2f} max_reward=${rec['expected_tp_usd']:.2f}")

    try:
        r = c.place_market_order(sym, side, quantity=qty_str, reduce_only=False)
        log(f"  ENTRY filled @ ${r.get('avgPrice')}")
    except BinanceApiError as e:
        log(f"  ENTRY FAIL: {e}")
        return False

    time.sleep(1.5)
    positions = c.get_positions(sym)
    entry = Decimal("0")
    amt = 0
    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if amt != 0:
            entry = Decimal(p["entryPrice"])
            break
    if entry == 0:
        log("  FAIL: no position after entry")
        return False

    log(f"  Actual entry: ${entry}")

    close_side = "SELL" if side == "BUY" else "BUY"
    if side == "BUY":
        tp_px = quantize_price(entry * (Decimal("1") + Decimal(str(rec["tp_pct"]))), tick)
        sl_px = quantize_price(entry * (Decimal("1") - Decimal(str(rec["sl_pct"]))), tick)
    else:
        tp_px = quantize_price(entry * (Decimal("1") - Decimal(str(rec["tp_pct"]))), tick)
        sl_px = quantize_price(entry * (Decimal("1") + Decimal(str(rec["sl_pct"]))), tick)

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

    return True


def wait_position_close(sym):
    """Block until position closes (server-side brackets handle TP/SL)."""
    log(f"Waiting for {sym} position to close...")
    while True:
        try:
            positions = c.get_positions(sym)
            amt = 0
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                break
            if amt == 0:
                log(f"  {sym} closed.")
                # Cleanup any leftover orders
                try: c.cancel_all_orders(sym)
                except: pass
                try: c.cancel_all_algo_orders(sym)
                except: pass
                return
            time.sleep(5)
        except Exception as e:
            log(f"  wait err: {e}")
            time.sleep(10)


def main():
    initial_wallet = get_wallet()
    session_pnl = Decimal("0")
    consec_losses = 0
    log(f"=== AUTO-HARMONY START === wallet=${initial_wallet:.4f}")

    while True:
        if os.path.exists(STOP_FLAG_FILE):
            log("STOP flag detected. Exiting.")
            break

        if session_pnl <= -MAX_SESSION_LOSS_USD:
            log(f"Max session loss ${MAX_SESSION_LOSS_USD} hit (${session_pnl:.2f}). Exiting.")
            break

        if consec_losses >= MAX_CONSEC_LOSSES:
            log(f"Max consecutive losses {MAX_CONSEC_LOSSES} hit. Exiting.")
            break

        wallet_now = get_wallet()
        winner = run_full_scan(wallet_now)

        if winner is None:
            log(f"  Sleep {SCAN_LOOP_SLEEP_SEC}s...")
            time.sleep(SCAN_LOOP_SLEEP_SEC)
            continue

        s = winner["setup"]
        log(f"  WINNER: {s['sym']} {winner['side']} [{winner['conviction']}] harmony={winner['harmony_total']}")

        # Freshness check policy varies by tier:
        # PRIME/GOOD: trust macro, execute immediately (macro strong overrides minor live noise)
        # SUFFICIENT/SCOUT: full freshness check + smart flip
        if winner["conviction"] in ("PRIME", "GOOD"):
            log(f"  HIGH-TIER: skipping freshness check (macro {winner['harmony_total']} overrides)")
        else:
            ok, reason = freshness_check(winner)
            if not ok:
                log(f"  ABORT (freshness): {reason}")
                time.sleep(SCAN_LOOP_SLEEP_SEC)
                continue

        # Execute
        wallet_pre = get_wallet()
        ok = execute_trade(winner, wallet_pre)
        if not ok:
            time.sleep(SCAN_LOOP_SLEEP_SEC)
            continue

        # Wait for close
        wait_position_close(s["sym"])

        # Compute trade PnL
        wallet_post = get_wallet()
        trade_pnl = Decimal(str(wallet_post)) - Decimal(str(wallet_pre))
        session_pnl += trade_pnl
        if trade_pnl < 0:
            consec_losses += 1
        else:
            consec_losses = 0
        log(f"  TRADE PNL: ${trade_pnl:+.4f}  session=${session_pnl:+.4f}  consec_loss={consec_losses}")

        cooldown = COOLDOWN_SCOUT_SEC if winner.get("conviction") == "SCOUT" else COOLDOWN_NORMAL_SEC
        log(f"  Cooldown {cooldown}s...")
        time.sleep(cooldown)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
