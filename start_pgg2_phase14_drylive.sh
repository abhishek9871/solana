#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 14 2026-05-08 — WAVE SURFER architecture.
# Insight from data: median winner and median loser have nearly identical entry
# features. Filter-based selection has a structural ceiling. The answer is
# probabilistic coverage: be in MORE positions, scale out aggressively at small
# wins, cut fast on no-pop.
#
# Architecture:
# - Loose filters (catch more attempts)
# - 0.05 SOL full positions (fee-efficient)
# - max_open=8 (high concurrency)
# - Early scale-out: 30% at 1.10, 30% at 1.40, 25% at 1.80, last 15% trails
# - Fast hard_break (5s grace then fire at 0.95)
# - No extended min-hold (fast feedback loop)
# - Moonshot rider latch lowered to 1.05 so scale-out arms early

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_phase14_drylive}"

# Disable smart-wallet WS, anti-bot filter
export SMART_WALLET_WS_ENABLED=0
export PGG2_ANTIBOT_FILTER_ENABLED=0

# Phase 2A
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0
export PGG2_BLOCK_HOURS_UTC="${PGG2_BLOCK_HOURS_UTC:-17,18,19}"
export PGG2_CIRCUIT_BREAKER_LOSSES="${PGG2_CIRCUIT_BREAKER_LOSSES:-5}"
export PGG2_CIRCUIT_BREAKER_PAUSE_SEC="${PGG2_CIRCUIT_BREAKER_PAUSE_SEC:-300}"

# Phase 14 — LOOSE filters (cast wide net, exits do the work)
export PGG2_PRICED_SNAP_MIN_UNIQ1500=4
export PGG2_PRICED_SNAP_MAX_TOP1500=0.45
export PGG2_PRICED_SNAP_MIN_BUY1500=4.0
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=8
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.30
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=10.0

# Volume-sustain filter (still useful)
export PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS=2000
# No vSol sweet-spot — let all bonded levels through
export PGG2_PRICED_SNAP_MIN_VSOL_SWEET=0.0
export PGG2_PRICED_SNAP_MAX_VSOL_SWEET=999.0

# HIGH CONCURRENCY
export PIGGY_MAX_OPEN=8

# Phase 3 PEAK-LOCK + curve_lag_reveal
export PGG2_CURVE_LAG_REVEAL_ENABLED=1
export PGG2_PEAK_LOCK_ENABLED=0          # disable PEAK-LOCK; scale-out replaces it
export PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW=1.05  # lower bar — almost always allow hard_break

# Full-size positions, anti-martingale off
export PGG2_ANTI_MARTINGALE_ENABLED=0
export PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION=1.0
export PGG2_LAYERED_ENTRY_FRACTION=1.0

# Moonshot rider — LATCH LOW so scale-out arms quickly
export PGG2_MOONSHOT_RIDE_ENABLED=1
export PGG2_MOONSHOT_RIDE_PEAK=1.05      # was 1.15 — latch on tiny up-move
export PGG2_MOONSHOT_RIDE_WINDOW_SEC=60.0
export PGG2_MOONSHOT_RIDE_HARD_TIMEOUT_SEC=180.0
export PGG2_MOONSHOT_RIDE_TIER0_TRAIL=0.92  # tighter for low peaks
export PGG2_MOONSHOT_RIDE_TIER1_PEAK=1.60
export PGG2_MOONSHOT_RIDE_TIER1_TRAIL=0.85
export PGG2_MOONSHOT_RIDE_TIER2_PEAK=2.00
export PGG2_MOONSHOT_RIDE_TIER2_TRAIL=0.80
export PGG2_MOONSHOT_RIDE_TIER3_PEAK=3.00
export PGG2_MOONSHOT_RIDE_TIER3_TRAIL=0.75
export PGG2_MOONSHOT_RIDE_MIN_HOLD_SEC=2.0  # short — let exits fire fast
export PGG2_MOONSHOT_RIDE_PANIC_TRAIL=0.40

# Phase 14 — THREE-TIER aggressive scale-out (Wave Surfer)
export PGG2_SCALE_OUT_ENABLED=1
export PGG2_SCALE_OUT_TIER1_PEAK=1.10    # sell 30% at first 10% gain
export PGG2_SCALE_OUT_TIER1_FRACTION=0.30
export PGG2_SCALE_OUT_TIER2_PEAK=1.40    # sell 30% (=21% original) at 40% gain
export PGG2_SCALE_OUT_TIER2_FRACTION=0.30
export PGG2_SCALE_OUT_TIER3_PEAK=1.80    # sell 50% of remaining (=24.5% original) at 80% gain
export PGG2_SCALE_OUT_TIER3_FRACTION=0.50
# Last ~24.5% rides the tiered trail (peak >= 2.0 → 0.80 trail)

# Stall exit — quick
export PGG2_STALL_EXIT_ENABLED=1
export PGG2_STALL_EXIT_SEC=15.0
export PGG2_STALL_EXIT_MIN_MULT=1.10

# Hard-break grace SHORT (5s, then fire normally)
export PGG2_HARD_BREAK_GRACE_ENABLED=1
export PGG2_HARD_BREAK_GRACE_SEC=5.0
export PGG2_HARD_BREAK_GRACE_BUY_AGE_MS=1500

# Entry window
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
export PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC=60.0

# Min-hold MINIMAL — fast feedback loop
export PGG2_MIN_HOLD_ENABLED=1
export PGG2_MIN_HOLD_SEC=5.0             # was 12.0
export PGG2_MIN_HOLD_LIFE_WINDOW_SEC=5.0
export PGG2_MIN_HOLD_LIFE_PEAK=1.05      # was 1.10
export PGG2_MIN_HOLD_EXTENDED_SEC=30.0   # was 90.0
export PGG2_MIN_HOLD_PANIC_FLOOR=0.70    # tighter panic — cut faster on dump

# Disable moonshot_failed_no_pop
export PIGGY_MOON_FAIL_SEC=999.0

exec ./start_pgg2_direct_drylive_candidate.sh
