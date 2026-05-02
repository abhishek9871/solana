"""
TEST: simulate the mid-trade flip decision logic without actually placing orders.

For each tradeable coin with macro alignment, simulates:
  - We're long at hypothetical entry
  - Current mark is X% adverse
  - Run the flip decision logic
  - Show: would we flip? what predictor scores? would the flip be safe?

Validates the flip logic against real current market state.
"""
import os
import sys
from decimal import Decimal
from trading_bot.binance_client import BinanceFuturesClient, BinanceApiError
from trading_bot.live_executor import load_credentials_from_env

sys.path.insert(0, os.path.dirname(__file__))
from harmony import (
    get_tradeable_symbols, score_macro, predict, hard_veto, VOL_MIN_USD
)

load_credentials_from_env()
api = os.environ.get("BINANCE_API_KEY", "").strip()
secret = os.environ.get("BINANCE_SECRET_KEY", "").strip()
c = BinanceFuturesClient(api_key=api, secret_key=secret, base_url="https://fapi.binance.com")

FLIP_TRIGGER_ADVERSE = -0.003   # -0.3% adverse
FLIP_PRED_MIN = 30


def simulate_flip(sym, hypothetical_side):
    """Simulate MID-TRADE flip (predictor-only, no macro veto, fast)."""
    print(f"\n=== {sym} simulating mid-trade flip from {hypothetical_side} ===")
    try:
        opp = "SELL" if hypothetical_side == "BUY" else "BUY"
        p_score, p_sig = predict(sym, opp)
        br = p_sig.get("buy_ratio", 0.5)
        ob = p_sig.get("ob", 0)
        print(f"  Opposite ({opp}) predictor: {p_score}")
        print(f"    ob: {ob:+.3f} ({p_sig.get('ob_call')})")
        print(f"    buy_ratio: {br:.3f} ({p_sig.get('flow')})")
        print(f"    flow_trend: {p_sig.get('flow_t', 'n/a')}")
        print(f"    oi: {p_sig.get('oi')}")

        if p_score < 35:
            print(f"  DECISION: SKIP (predictor {p_score} < 35)")
            return False

        if opp == "BUY":
            if br < 0.50:
                print(f"  DECISION: SKIP (buy_ratio {br:.2f} < 0.50 for BUY)")
                return False
            if ob < -0.20:
                print(f"  DECISION: SKIP (ob {ob:.2f} < -0.20, heavy ask wall)")
                return False
        else:
            if br > 0.50:
                print(f"  DECISION: SKIP (buy_ratio {br:.2f} > 0.50 for SELL)")
                return False
            if ob > 0.20:
                print(f"  DECISION: SKIP (ob {ob:.2f} > 0.20, heavy bid wall)")
                return False

        print(f"  DECISION: *** FLIP TO {opp} *** (predictor {p_score}, br {br:.2f}, ob {ob:.2f})")
        return True
    except Exception as e:
        print(f"  ERR: {e}")
        return False


def main():
    print("=" * 80)
    print("FLIP LOGIC TEST — runs on real market, simulates flip decisions")
    print("=" * 80)

    # Find currently active coins
    tradeable = get_tradeable_symbols()
    tickers = c.get_24hr_tickers()
    candidates = [t for t in tickers if t.get("symbol") in tradeable
                  and float(t.get("quoteVolume", 0)) > VOL_MIN_USD]
    print(f"Sampling {len(candidates)} liquid coins\n")

    # Pick top 3 by 24h move (most volatile)
    candidates.sort(key=lambda t: -abs(float(t.get("priceChangePercent", 0))))
    sample = candidates[:3]

    for t in sample:
        sym = t["symbol"]
        chg_24h = float(t.get("priceChangePercent", 0))
        print(f"\n{'#'*80}")
        sym_safe = sym.encode('ascii', 'replace').decode('ascii')
        print(f"{sym_safe} — 24h: {chg_24h:+.2f}%")
        print(f"{'#'*80}")

        # Test: simulate we entered LONG, now adverse → would we flip to SHORT?
        simulate_flip(sym, "BUY")
        # Test: simulate we entered SHORT, now adverse → would we flip to LONG?
        simulate_flip(sym, "SELL")

    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
