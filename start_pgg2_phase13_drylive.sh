#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 13 2026-05-08 — DATA-DERIVED filters from OUR OWN 71 big winners.
# Across 1467 trades in 22+ runs, our bot has won net +$19.80. The 71 trades
# that won >$2 share specific Q1-Q3 feature signatures. Tighten to match those.
#
# Big winners' signature (from our actual winning trades):
# - top_share1500: Q1=0.19, Q3=0.35, median 0.27 (NOT 0.50 like Phase 9-12)
# - uniq1500: Q1=7, Q3=15, median 9 (NOT 5)
# - buy1500: Q1=8.58, Q3=20.05, median 12.79 (NOT 5.0)
# - vsol_sol: Q3=45.53 (NEW upper bound)
# - slot_buyers: Q1=5 (NEW)
#
# Phase 9-12 LOOSENED filters in the wrong direction. Restore selectivity.
# Anti-bot filter DISABLED (was untested theory; data says use cleaner setups).

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_phase13_drylive}"

# Disable smart-wallet WS and anti-bot filter (untested, theoretical)
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

# Phase 13 — DATA-DERIVED filters (Q1 of OUR big winners)
export PGG2_PRICED_SNAP_MIN_UNIQ1500=7        # Q1 of big winners
export PGG2_PRICED_SNAP_MAX_TOP1500=0.35      # Q3 of big winners
export PGG2_PRICED_SNAP_MIN_BUY1500=8.5       # Q1 of big winners
# Elite tier: above-median signatures
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=9
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.27
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=12.79

# Phase 10 volume-sustain filter
export PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS=1500
# Phase 13 — vSol sweet-spot: winners Q3 = 45.5 (UPPER bound matters)
export PGG2_PRICED_SNAP_MIN_VSOL_SWEET=0.0    # no lower bound (winners go to 0)
export PGG2_PRICED_SNAP_MAX_VSOL_SWEET=46.0   # Q3 of big winners

# Max-open
export PIGGY_MAX_OPEN=5

# Phase 3 PEAK-LOCK + curve_lag_reveal
export PGG2_CURVE_LAG_REVEAL_ENABLED=1
export PGG2_PEAK_LOCK_ENABLED=1
export PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW=1.18

# Phase 7D full-size positions, anti-martingale off
export PGG2_ANTI_MARTINGALE_ENABLED=0
export PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION=1.0
export PGG2_LAYERED_ENTRY_FRACTION=1.0

# Moonshot rider (proven from prior phases)
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

# Scale-out (proven)
export PGG2_SCALE_OUT_ENABLED=1
export PGG2_SCALE_OUT_TIER1_PEAK=1.50
export PGG2_SCALE_OUT_TIER1_FRACTION=0.50
export PGG2_SCALE_OUT_TIER2_PEAK=2.50
export PGG2_SCALE_OUT_TIER2_FRACTION=0.50

# Stall exit
export PGG2_STALL_EXIT_ENABLED=1
export PGG2_STALL_EXIT_SEC=20.0
export PGG2_STALL_EXIT_MIN_MULT=1.15

# Hard-break grace
export PGG2_HARD_BREAK_GRACE_ENABLED=1
export PGG2_HARD_BREAK_GRACE_SEC=8.0
export PGG2_HARD_BREAK_GRACE_BUY_AGE_MS=1500

# Entry window
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
export PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC=60.0

# Min-hold
export PGG2_MIN_HOLD_ENABLED=1
export PGG2_MIN_HOLD_SEC=12.0
export PGG2_MIN_HOLD_LIFE_WINDOW_SEC=10.0
export PGG2_MIN_HOLD_LIFE_PEAK=1.10
export PGG2_MIN_HOLD_EXTENDED_SEC=90.0
export PGG2_MIN_HOLD_PANIC_FLOOR=0.50

# Disable moonshot_failed_no_pop
export PIGGY_MOON_FAIL_SEC=999.0

exec ./start_pgg2_direct_drylive_candidate.sh
