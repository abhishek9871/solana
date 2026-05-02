"""Test the new pre-trade direction confirmation checks."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from trading_bot.live_executor import load_credentials_from_env
load_credentials_from_env()
from perfect_harmony import micro_tape_check, topk_orderbook_check

for sym in ["BIOUSDT", "SKYAIUSDT", "BSBUSDT"]:
    print(f"\n=== {sym} ===")
    for side in ["BUY", "SELL"]:
        tape_ok, br, tape_msg = micro_tape_check(sym, side)
        top5_ok, top5_msg = topk_orderbook_check(sym, side)
        verdict = "OK" if (tape_ok and top5_ok) else "BLOCK"
        print(f"  {side}: [{verdict}] {tape_msg} | {top5_msg}")
