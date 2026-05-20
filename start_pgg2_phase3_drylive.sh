#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 3 2026-05-08 dry-live wrapper.
# Exports Phase 2A (filter pruning + adaptive guards) and Phase 3 (PEAK-LOCK,
# hard_break gating, anti-martingale, curve_lag_reveal disable) env vars, then
# execs the existing dry-live launcher so quote shadows reflect the new logic.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_phase3_drylive}"

# =============================================================================
# Phase 2A 2026-05-08 — adaptive guards + filter pruning
# =============================================================================
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

# =============================================================================
# Phase 3 2026-05-08 — strategic redesign for "destined to win big"
# =============================================================================
# 1. Disable curve_lag_reveal lane (NET NEGATIVE per cross-run validation)
export PGG2_CURVE_LAG_REVEAL_ENABLED="${PGG2_CURVE_LAG_REVEAL_ENABLED:-0}"

# 2. PEAK-LOCK trailing stop (replaces scout_profit_protect)
export PGG2_PEAK_LOCK_ENABLED="${PGG2_PEAK_LOCK_ENABLED:-1}"
export PGG2_PEAK_LOCK_LOW_PEAK="${PGG2_PEAK_LOCK_LOW_PEAK:-1.18}"
export PGG2_PEAK_LOCK_LOW_FLOOR="${PGG2_PEAK_LOCK_LOW_FLOOR:-1.05}"
export PGG2_PEAK_LOCK_LOW_TRAIL="${PGG2_PEAK_LOCK_LOW_TRAIL:-0.92}"
export PGG2_PEAK_LOCK_MID_PEAK="${PGG2_PEAK_LOCK_MID_PEAK:-1.30}"
export PGG2_PEAK_LOCK_MID_FLOOR="${PGG2_PEAK_LOCK_MID_FLOOR:-1.15}"
export PGG2_PEAK_LOCK_MID_TRAIL="${PGG2_PEAK_LOCK_MID_TRAIL:-0.88}"
export PGG2_PEAK_LOCK_HIGH_PEAK="${PGG2_PEAK_LOCK_HIGH_PEAK:-1.60}"
export PGG2_PEAK_LOCK_HIGH_FLOOR="${PGG2_PEAK_LOCK_HIGH_FLOOR:-1.30}"
export PGG2_PEAK_LOCK_HIGH_TRAIL="${PGG2_PEAK_LOCK_HIGH_TRAIL:-0.85}"

# 3. Gate hard_break: only fires when peak never reached PEAK-LOCK low tier.
export PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW="${PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW:-1.18}"

# 4. Anti-martingale stake scaling (start ENABLED per Phase 3 plan)
export PGG2_ANTI_MARTINGALE_ENABLED="${PGG2_ANTI_MARTINGALE_ENABLED:-1}"
export PGG2_ANTI_MARTINGALE_LOSS_STREAK="${PGG2_ANTI_MARTINGALE_LOSS_STREAK:-2}"
export PGG2_ANTI_MARTINGALE_LOSS_SCALE="${PGG2_ANTI_MARTINGALE_LOSS_SCALE:-0.50}"
export PGG2_ANTI_MARTINGALE_WIN_STREAK="${PGG2_ANTI_MARTINGALE_WIN_STREAK:-2}"
export PGG2_ANTI_MARTINGALE_WIN_SCALE="${PGG2_ANTI_MARTINGALE_WIN_SCALE:-1.30}"

exec ./start_pgg2_direct_drylive_candidate.sh
