"""Unit check for the V287 single-lane firewall predicate (real function, heavy deps stubbed).
Run: py -3 win287/_test_single_lane_firewall.py"""
import os
import sys
from collections import Counter
from unittest.mock import MagicMock

# Stub the server-only / native deps so the 13.6k-line runner module imports locally.
for _name in (
    "grpc",
    "solders", "solders.pubkey", "solders.signature", "solders.transaction",
    "geyser_pb2", "geyser_pb2_grpc", "solana_storage_pb2", "solana_storage_pb2_grpc",
    "birth_first_sniper", "pgg2_direct_pump",
    "pgg2_v74_sender_adapter", "pgg2_v75_sender_tx_builder",
    "pgg2_v285_grpc_buy_train_continuation_no_send",
):
    sys.modules.setdefault(_name, MagicMock())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pgg2_v287_selected_band_live_smoke as M  # noqa: E402

WIN = "selected_single_prior_strong_rearm"


def fw(reason, top_lane, no_move, single_lane="1"):
    os.environ["V287_SINGLE_LANE_ONLY"] = single_lane
    cand = {"top_lane": top_lane, "no_movement_watch_keeps": no_move}
    return M._v287_single_lane_firewall_ok(
        mint="TestMint1111111111111111111111111111111111",
        cand=cand, reason=reason, counters=Counter(), source="unit_test",
    )


cases = [
    ("winner exact", WIN, "single_prior_buy_continuation", 0, "1", True),
    ("winner w/ active no-move watch", WIN, "single_prior_buy_continuation", 1, "1", False),
    ("winner reason wrong top_lane", WIN, "fresh_impulse", 0, "1", False),
    ("bleeder fresh_impulse", "fresh_impulse", "fresh_impulse", 0, "1", False),
    ("bleeder current_band_train", "selected_current_band_train_rearm", "current_band_train", 0, "1", False),
    ("bleeder seed_prior_carry", "selected_seed_prior_carry_rearm", "seed_prior_carry_continuation", 0, "1", False),
    ("loose single_prior fallthrough", "single_prior_buy_continuation", "single_prior_buy_continuation", 0, "1", False),
    ("empty reason", "", "single_prior_buy_continuation", 0, "1", False),
    ("firewall OFF allows winner", WIN, "single_prior_buy_continuation", 0, "0", True),
    ("firewall OFF allows bleeder", "fresh_impulse", "fresh_impulse", 0, "0", True),
]

fails = 0
for desc, reason, top_lane, no_move, env, expected in cases:
    got = fw(reason, top_lane, no_move, env)
    ok = got == expected
    fails += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}: got={got} expected={expected}")

print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILED'} ({len(cases)} cases)")
sys.exit(1 if fails else 0)
