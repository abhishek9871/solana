#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 10 2026-05-08 — research-derived ENTRY QUALITY filter.
# Marino arXiv 2602.14860 + every Phase 5/6/7/8/9 loss autopsy: bundle-and-bail
# pattern. Bundlers buy hard (triggering our buy1500 filter), then bail.
# We enter AT THE TOP. Buyers have already vanished 1-2s before we strike.
#
# Phase 10 fixes this with two NEW filters:
# (1) MAX_LAST_BUY_AGE_MS=600 — reject strikes when buying has already paused
# (2) vSol sweet-spot 28-50 SOL — Marino: 16x graduation odds in this band
#
# Combined with Phase 8/9 architecture (full-size, scale-out, min-hold, tight trail).

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_phase10_drylive}"

# Phase 2A — adaptive guards + filter pruning
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0
# Tighter hour block per research: skip 17-19:30 UTC + dead zones
export PGG2_BLOCK_HOURS_UTC="${PGG2_BLOCK_HOURS_UTC:-17,18,19}"
export PGG2_CIRCUIT_BREAKER_LOSSES="${PGG2_CIRCUIT_BREAKER_LOSSES:-5}"
export PGG2_CIRCUIT_BREAKER_PAUSE_SEC="${PGG2_CIRCUIT_BREAKER_PAUSE_SEC:-300}"

# Phase 9 loose concentration filters
export PGG2_PRICED_SNAP_MIN_UNIQ1500=5
export PGG2_PRICED_SNAP_MAX_TOP1500=0.50
export PGG2_PRICED_SNAP_MIN_BUY1500=5.0
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=8
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.30
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=12.0

# Phase 11 (loosened from 600 → 1500ms): still rejects fully-dead waves
# but allows the typical 0.5-1.5s buyer pauses that real moonshots have
export PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS=1500
# vSol sweet-spot kept enabled — Marino's 16x edge band
export PGG2_PRICED_SNAP_MIN_VSOL_SWEET=28.0
export PGG2_PRICED_SNAP_MAX_VSOL_SWEET=50.0

# Phase 9 — RAISE max_open
export PIGGY_MAX_OPEN=5

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

# Phase 7C — tightened moonshot trail
export PGG2_MOONSHOT_RIDE_ENABLED=1
export PGG2_MOONSHOT_RIDE_PEAK=1.15
export PGG2_MOONSHOT_RIDE_WINDOW_SEC=90.0
export PGG2_MOONSHOT_RIDE_HARD_TIMEOUT_SEC=300.0
export PGG2_MOONSHOT_RIDE_TIER0_TRAIL=0.90
export PGG2_MOONSHOT_RIDE_TIER1_PEAK=1.60
export PGG2_MOONSHOT_RIDE_TIER1_TRAIL=0.85
export PGG2_MOONSHOT_RIDE_TIER2_PEAK=2.00
export PGG2_MOONSHOT_RIDE_TIER2_TRAIL=0.80
export PGG2_MOONSHOT_RIDE_TIER3_PEAK=3.00
export PGG2_MOONSHOT_RIDE_TIER3_TRAIL=0.75
export PGG2_MOONSHOT_RIDE_MIN_HOLD_SEC=5.0
export PGG2_MOONSHOT_RIDE_PANIC_TRAIL=0.30

# Phase 9 scale-out
export PGG2_SCALE_OUT_ENABLED=1
export PGG2_SCALE_OUT_TIER1_PEAK=1.50
export PGG2_SCALE_OUT_TIER1_FRACTION=0.50
export PGG2_SCALE_OUT_TIER2_PEAK=2.50
export PGG2_SCALE_OUT_TIER2_FRACTION=0.50

# Phase 8 stall exit
export PGG2_STALL_EXIT_ENABLED=1
export PGG2_STALL_EXIT_SEC=20.0
export PGG2_STALL_EXIT_MIN_MULT=1.15

# Phase 5 hard_break grace
export PGG2_HARD_BREAK_GRACE_ENABLED=1
export PGG2_HARD_BREAK_GRACE_SEC=8.0
export PGG2_HARD_BREAK_GRACE_BUY_AGE_MS=1500

# Phase 5 entry window
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
export PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC=60.0

# Phase 6/7 min-hold
export PGG2_MIN_HOLD_ENABLED=1
export PGG2_MIN_HOLD_SEC=12.0
export PGG2_MIN_HOLD_LIFE_WINDOW_SEC=10.0
export PGG2_MIN_HOLD_LIFE_PEAK=1.10
export PGG2_MIN_HOLD_EXTENDED_SEC=90.0
export PGG2_MIN_HOLD_PANIC_FLOOR=0.50

# Phase 7 disable moonshot_failed_no_pop
export PIGGY_MOON_FAIL_SEC=999.0

exec ./start_pgg2_direct_drylive_candidate.sh
