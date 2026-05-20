#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 9 2026-05-08 — research-derived frequency boost.
# Marino arXiv 2602.14860: velocity dominates concentration as graduation predictor.
# MemeTrans arXiv 2602.13480: concentration filters are RUG filters, not WINNER filters.
# 2026 production bots: max_open 3-8, scale-out at lower thresholds (1.3x / 2x).
#
# Changes from Phase 8:
# - Loosen uniq1500 8→5, top_share1500 0.37→0.50 (let velocity-positive trades through)
# - Lower scale-out tier1 from 2.0x peak to 1.50x peak (sell 50% earlier)
# - Lower scale-out tier2 from 4.0x to 2.50x (sell 50% of remaining)
# - Raise max_open 3→5 for more parallel exposure
# Expected: 3-4x strike rate, 50-60% win rate maintained or improved

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_phase9_drylive}"

# Phase 2A — adaptive guards + filter pruning
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0
export PGG2_BLOCK_HOURS_UTC="${PGG2_BLOCK_HOURS_UTC:-18,19,20}"
export PGG2_CIRCUIT_BREAKER_LOSSES="${PGG2_CIRCUIT_BREAKER_LOSSES:-5}"
export PGG2_CIRCUIT_BREAKER_PAUSE_SEC="${PGG2_CIRCUIT_BREAKER_PAUSE_SEC:-300}"

# Phase 9 — LOOSEN concentration filters (research: concentration = rug filter, not winner)
export PGG2_PRICED_SNAP_MIN_UNIQ1500=5      # was 8
export PGG2_PRICED_SNAP_MAX_TOP1500=0.50    # was 0.37
export PGG2_PRICED_SNAP_MIN_BUY1500=5.0     # was 7.0
# Keep elite bar reasonable (still tier-2 quality)
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=8
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.30
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=12.0

# Phase 9 — RAISE max_open for more parallel exposure
export PIGGY_MAX_OPEN=5                     # was 3

# Phase 3 — keep PEAK-LOCK + curve_lag_reveal
export PGG2_CURVE_LAG_REVEAL_ENABLED="${PGG2_CURVE_LAG_REVEAL_ENABLED:-1}"
export PGG2_PEAK_LOCK_ENABLED="${PGG2_PEAK_LOCK_ENABLED:-1}"
export PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW="${PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW:-1.18}"

# Phase 7D — full-size positions, anti-martingale off
export PGG2_ANTI_MARTINGALE_ENABLED=0
export PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION=1.0
export PGG2_LAYERED_ENTRY_FRACTION=1.0

# Phase 7C — tightened moonshot trail (lock 75-90% of peak)
export PGG2_MOONSHOT_RIDE_ENABLED="${PGG2_MOONSHOT_RIDE_ENABLED:-1}"
export PGG2_MOONSHOT_RIDE_PEAK="${PGG2_MOONSHOT_RIDE_PEAK:-1.15}"
export PGG2_MOONSHOT_RIDE_WINDOW_SEC="${PGG2_MOONSHOT_RIDE_WINDOW_SEC:-90.0}"
export PGG2_MOONSHOT_RIDE_HARD_TIMEOUT_SEC="${PGG2_MOONSHOT_RIDE_HARD_TIMEOUT_SEC:-300.0}"
export PGG2_MOONSHOT_RIDE_TIER0_TRAIL=0.90
export PGG2_MOONSHOT_RIDE_TIER1_PEAK=1.60
export PGG2_MOONSHOT_RIDE_TIER1_TRAIL=0.85
export PGG2_MOONSHOT_RIDE_TIER2_PEAK=2.00
export PGG2_MOONSHOT_RIDE_TIER2_TRAIL=0.80
export PGG2_MOONSHOT_RIDE_TIER3_PEAK=3.00
export PGG2_MOONSHOT_RIDE_TIER3_TRAIL=0.75
export PGG2_MOONSHOT_RIDE_MIN_HOLD_SEC=5.0
export PGG2_MOONSHOT_RIDE_PANIC_TRAIL=0.30

# Phase 9 — LOWER scale-out thresholds (capture right-tail earlier)
export PGG2_SCALE_OUT_ENABLED=1
export PGG2_SCALE_OUT_TIER1_PEAK=1.50       # was 2.0 — Marino: 1.3x is meaningful
export PGG2_SCALE_OUT_TIER1_FRACTION=0.50   # was 0.60 — lighter to leave room for tier2
export PGG2_SCALE_OUT_TIER2_PEAK=2.50       # was 4.0 — earlier secondary lock
export PGG2_SCALE_OUT_TIER2_FRACTION=0.50

# Phase 8 — STALL EXIT
export PGG2_STALL_EXIT_ENABLED=1
export PGG2_STALL_EXIT_SEC=20.0
export PGG2_STALL_EXIT_MIN_MULT=1.15

# Phase 5 — hard_break grace
export PGG2_HARD_BREAK_GRACE_ENABLED="${PGG2_HARD_BREAK_GRACE_ENABLED:-1}"
export PGG2_HARD_BREAK_GRACE_SEC="${PGG2_HARD_BREAK_GRACE_SEC:-8.0}"
export PGG2_HARD_BREAK_GRACE_BUY_AGE_MS="${PGG2_HARD_BREAK_GRACE_BUY_AGE_MS:-1500}"

# Phase 5 — entry window widening
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
export PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC=60.0

# Phase 6/7 — TIERED MIN-HOLD floor
export PGG2_MIN_HOLD_ENABLED=1
export PGG2_MIN_HOLD_SEC=12.0
export PGG2_MIN_HOLD_LIFE_WINDOW_SEC=10.0
export PGG2_MIN_HOLD_LIFE_PEAK=1.10
export PGG2_MIN_HOLD_EXTENDED_SEC=90.0
export PGG2_MIN_HOLD_PANIC_FLOOR=0.50

# Phase 7 — disable moonshot_failed_no_pop
export PIGGY_MOON_FAIL_SEC=999.0

exec ./start_pgg2_direct_drylive_candidate.sh
