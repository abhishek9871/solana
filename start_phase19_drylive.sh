#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 19 2026-05-08 — LATCH-TIME SCALE-OUT (cascade-dump killer).
# Phase 18 data showed FjP7 cascade-dumped from 1.15 → 0.44 in a single tick,
# costing -$2.52 (the dead-zone problem: 1.15-1.50 peaks are unprotected).
#
# Phase 19 adds a tier 0 scale-out that fires at moonshot LATCH (peak >= 1.15):
# IMMEDIATELY sell 30% to bank profit before any cascade can hit. Plus halved
# position size (0.030 SOL = $2.65) so losses on no-pop trades shrink ~40%.
#
# Combined effect:
# - Big wins still possible (1.87x peak win = ~$1.30 instead of $1.95 at 0.05)
# - Cascade-dump losses cut substantially (FjP7 from -$2.52 to ~-$0.85)
# - No-pump duds halved (-$0.30 to -$0.20)

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-phase19_drylive}"

# DRY-LIVE OVERRIDES
export PGG2_EXECUTION_MODE=quote
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=1
export PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK
export PGG2_DIRECT_LIVE_CONFIRM=I_ACCEPT_DIRECT_PUMP_RISK
export PGG2_QUOTE_SHADOW_POSITIONS=1
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
export PGG2_LIVE_SKIP_PREFLIGHT=1
export SMART_WALLET_WS_ENABLED=0

# Phase 15 — RugCheck + engagement
export RUGCHECK_ENABLED=1
export RUGCHECK_REJECT_SCORE=4
export RUGCHECK_TIMEOUT_SEC=0.45
export RUGCHECK_CACHE_TTL_SEC=300
export PGG2_RUGCHECK_GATE_ENABLED=1
export ENGAGEMENT_POLL_ENABLED=1
export ENGAGEMENT_POLL_SEC=4.0
export ENGAGEMENT_POLL_LIMIT=50

# Phase 18 — engagement-driven lane
export PGG2_ENGAGEMENT_DRIVEN_ENABLED=1
export PGG2_ENGAGEMENT_MIN_VIEWERS=10
export PGG2_ENGAGEMENT_MIN_REPLIES=3
export PGG2_ENGAGEMENT_MIN_BUY1500=1.0
export PGG2_ENGAGEMENT_MIN_UNIQ1500=3
export PGG2_ENGAGEMENT_MAX_SELL_RATIO=0.20
export PGG2_ENGAGEMENT_LANE_SOL=0.025

# Source OG strategy config
source <(sed '/^exec \.\/start_pgg2_attack_paper.sh/,$d' ./start_pgg2_direct_live_candidate.sh)
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump
source <(sed '/^exec \.\/venv\/bin\/python -u PGG2.py/,$d' ./start_pgg2_attack_paper.sh)
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump

# Phase 18 middle-ground priced_snap filters
export PGG2_PRICED_SNAP_MIN_BUY1500=5.0
export PGG2_PRICED_SNAP_MIN_UNIQ1500=5
export PGG2_PRICED_SNAP_MAX_TOP1500=0.50
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=10.0
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=8
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.30
export PGG2_ENGAGEMENT_RELAX=1.30
export PGG2_KOTH_RELAX=1.50
export PGG2_ANTIBOT_FILTER_ENABLED=0
export PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS=2000
export PGG2_PRICED_SNAP_MIN_VSOL_SWEET=0.0
export PGG2_PRICED_SNAP_MAX_VSOL_SWEET=999.0
export PGG2_PRICED_SNAP_MIN_MOVE=1.18
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0

# ============================================================================
# PHASE 19 — POSITION SIZE REDUCED + LATCH-TIME SCALE-OUT
# ============================================================================
# Reduce max position from 0.050 → 0.030 SOL (~40% smaller losses on duds)
export PIGGY_SCOUT_SOL=0.030
export PIGGY_MAX_POSITION_SOL=0.030
export PIGGY_PROBE_SOL=0.030
# Engagement lane keeps tighter sizing
export PGG2_ENGAGEMENT_LANE_SOL=0.020

export PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION=1.0
export PGG2_LAYERED_ENTRY_FRACTION=1.0
export PGG2_ANTI_MARTINGALE_ENABLED=0

# NEW: Latch-time scale-out at peak 1.15 — bank 30% before cascade can hit
export PGG2_LATCH_SCALE_OUT_ENABLED=1
export PGG2_LATCH_SCALE_OUT_PEAK=1.15
export PGG2_LATCH_SCALE_OUT_FRACTION=0.30

# Moonshot rider — latch low for early protection
export PGG2_MOONSHOT_RIDE_ENABLED=1
export PGG2_MOONSHOT_RIDE_PEAK=1.15
export PGG2_MOONSHOT_RIDE_WINDOW_SEC=90.0
export PGG2_MOONSHOT_RIDE_HARD_TIMEOUT_SEC=300.0
export PGG2_MOONSHOT_RIDE_TIER0_TRAIL=0.92
export PGG2_MOONSHOT_RIDE_TIER1_PEAK=1.60
export PGG2_MOONSHOT_RIDE_TIER1_TRAIL=0.85
export PGG2_MOONSHOT_RIDE_TIER2_PEAK=2.00
export PGG2_MOONSHOT_RIDE_TIER2_TRAIL=0.80
export PGG2_MOONSHOT_RIDE_TIER3_PEAK=3.00
export PGG2_MOONSHOT_RIDE_TIER3_TRAIL=0.75
export PGG2_MOONSHOT_RIDE_MIN_HOLD_SEC=5.0
export PGG2_MOONSHOT_RIDE_PANIC_TRAIL=0.30

# Scale-out tiers — adjust thresholds. Tier 0 (latch lock) handles 1.15+
# Tier 1 stays at 1.50 (the second profit lock for trades that grow past latch lock)
export PGG2_SCALE_OUT_ENABLED=1
export PGG2_SCALE_OUT_TIER1_PEAK=1.50
export PGG2_SCALE_OUT_TIER1_FRACTION=0.50
export PGG2_SCALE_OUT_TIER2_PEAK=2.00
export PGG2_SCALE_OUT_TIER2_FRACTION=0.50
export PGG2_SCALE_OUT_TIER3_PEAK=3.00
export PGG2_SCALE_OUT_TIER3_FRACTION=0.50

# Stall exit
export PGG2_STALL_EXIT_ENABLED=1
export PGG2_STALL_EXIT_SEC=20.0
export PGG2_STALL_EXIT_MIN_MULT=1.15

# Hard-break grace
export PGG2_HARD_BREAK_GRACE_ENABLED=1
export PGG2_HARD_BREAK_GRACE_SEC=8.0
export PGG2_HARD_BREAK_GRACE_BUY_AGE_MS=1500
export PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW=1.18

# PEAK-LOCK + min-hold
export PGG2_PEAK_LOCK_ENABLED=1
export PGG2_MIN_HOLD_ENABLED=1
export PGG2_MIN_HOLD_SEC=12.0
export PGG2_MIN_HOLD_LIFE_WINDOW_SEC=10.0
export PGG2_MIN_HOLD_LIFE_PEAK=1.10
export PGG2_MIN_HOLD_EXTENDED_SEC=90.0
export PGG2_MIN_HOLD_PANIC_FLOOR=0.50

export PIGGY_MOON_FAIL_SEC=999.0
export PIGGY_MAX_OPEN=5
export PGG2_CURVE_LAG_REVEAL_ENABLED=1

mkdir -p /root/piggy/logs /root/piggy/data
RUNID="${PGG2_RUN_PREFIX}_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

echo "PHASE19-DRYLIVE RUN_ID=$RUNID position=$PIGGY_MAX_POSITION_SOL latch_scale_out=$PGG2_LATCH_SCALE_OUT_ENABLED"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
