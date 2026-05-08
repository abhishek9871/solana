#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 18 2026-05-08 — FINAL ARCHITECTURE.
# Combines lessons from Phase 1-17:
# - MIDDLE-GROUND on-chain filters (not Phase 16 too-loose, not Phase 13 too-strict)
# - NEW engagement-driven lane (uses pump.fun frontend signals)
# - RugCheck pre-buy gate on both lanes (with FIXED score parsing)
# - Phase 8 scale-out + tiered trail (cascade-dump protection)
# - Min-hold 12s/90s, hard-break grace, PEAK-LOCK
#
# Architecture decided from Phase 16 live data:
#   Phase 16 stripped filters → high frequency but cascade-dumps eat us
#   Phase 13 strict filters → low frequency but quality
#   Phase 18 = middle ground (priced_snap) + engagement-driven supplement
# Win path: priced_snap catches fresh launches with quality + engagement
# lane catches established livestream/KOTH mints we'd otherwise miss.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-phase18_drylive}"

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

# Phase 15A — RugCheck pre-buy gate (FIXED score_normalised parsing)
export RUGCHECK_ENABLED=1
export RUGCHECK_REJECT_SCORE=4
export RUGCHECK_TIMEOUT_SEC=0.45
export RUGCHECK_CACHE_TTL_SEC=300
export PGG2_RUGCHECK_GATE_ENABLED=1

# Phase 15B — Pump.fun engagement poller
export ENGAGEMENT_POLL_ENABLED=1
export ENGAGEMENT_POLL_SEC=4.0
export ENGAGEMENT_POLL_LIMIT=50

# Phase 18 — engagement-driven lane (NEW)
export PGG2_ENGAGEMENT_DRIVEN_ENABLED=1
export PGG2_ENGAGEMENT_MIN_VIEWERS=10        # need 10+ livestream viewers OR be KOTH
export PGG2_ENGAGEMENT_MIN_REPLIES=3
export PGG2_ENGAGEMENT_MIN_BUY1500=1.0       # very loose — need ANY buying
export PGG2_ENGAGEMENT_MIN_UNIQ1500=3        # need 3+ buyers minimum
export PGG2_ENGAGEMENT_MAX_SELL_RATIO=0.20   # not currently dumping
export PGG2_ENGAGEMENT_LANE_SOL=0.025        # smaller stake — older mint, more risk

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

# ============================================================================
# PHASE 18 PRICED_SNAP — middle-ground filters
# Phase 13 (data Q1): buy 8.5/uniq 7/top 0.35 = too strict (low freq)
# Phase 16 (stripped): buy 2.0/uniq 3/top 0.95 = too loose (low quality)
# Phase 18 middle: buy 5.0/uniq 5/top 0.50 = balance
# ============================================================================
export PGG2_PRICED_SNAP_MIN_BUY1500=5.0
export PGG2_PRICED_SNAP_MIN_UNIQ1500=5
export PGG2_PRICED_SNAP_MAX_TOP1500=0.50
# Elite (no relax needed) keeps stricter
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=10.0
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=8
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.30
# Engagement boost (relax filters when mint also engaged)
export PGG2_ENGAGEMENT_RELAX=1.30
export PGG2_KOTH_RELAX=1.50
# Anti-bot OFF (RugCheck handles)
export PGG2_ANTIBOT_FILTER_ENABLED=0
# Volume-sustain MODERATE (1500ms window — was 600 in Phase 10, 10000 in Phase 16)
export PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS=2000
# vSol sweet-spot DISABLED (was hypothesis, not verified)
export PGG2_PRICED_SNAP_MIN_VSOL_SWEET=0.0
export PGG2_PRICED_SNAP_MAX_VSOL_SWEET=999.0
# Entry move + age window
export PGG2_PRICED_SNAP_MIN_MOVE=1.18
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0
# Strip Phase 2A overfit blocks
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0

# ============================================================================
# PHASE 8 EXITS — full-size + scale-out + tiered trail
# ============================================================================
export PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION=1.0
export PGG2_PRICED_SNAP_VERTICAL_ENTRY_FRACTION=1.0
export PGG2_LAYERED_ENTRY_FRACTION=1.0
export PGG2_ANTI_MARTINGALE_ENABLED=0

# Moonshot rider — latch low for early profit lock
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

# Scale-out — KEY cascade-dump protection
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

# Disable moonshot_failed_no_pop
export PIGGY_MOON_FAIL_SEC=999.0

# Higher max-open
export PIGGY_MAX_OPEN=5

# Curve lag reveal kept on
export PGG2_CURVE_LAG_REVEAL_ENABLED=1

mkdir -p /root/piggy/logs /root/piggy/data
RUNID="${PGG2_RUN_PREFIX}_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

echo "PHASE18-DRYLIVE RUN_ID=$RUNID architecture=middle_filters+engagement_lane+scale_out+rugcheck"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
