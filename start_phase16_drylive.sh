#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 16 2026-05-08 — TRUST THE GATES.
# RugCheck + engagement poller are the rug-detection and organic-interest
# gates. Strip the on-chain feature filters that were proxies for the same
# information. Drastically expand entry pool.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-phase16_drylive}"

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

# Phase 15A — RugCheck pre-buy gate (TIGHTENED — score 3 now mandatory)
export RUGCHECK_ENABLED=1
export RUGCHECK_REJECT_SCORE=3
export RUGCHECK_TIMEOUT_SEC=0.45
export RUGCHECK_CACHE_TTL_SEC=300
export PGG2_RUGCHECK_GATE_ENABLED=1

# Phase 15B — Pump.fun engagement poller (kept)
export ENGAGEMENT_POLL_ENABLED=1
export ENGAGEMENT_POLL_SEC=4.0
export ENGAGEMENT_POLL_LIMIT=50
export PGG2_ENGAGEMENT_RELAX=1.30
export PGG2_KOTH_RELAX=1.50

# Source OG strategy config
source <(sed '/^exec \.\/start_pgg2_attack_paper.sh/,$d' ./start_pgg2_direct_live_candidate.sh)

# Re-assert dry-live
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump

# Source attack paper config
source <(sed '/^exec \.\/venv\/bin\/python -u PGG2.py/,$d' ./start_pgg2_attack_paper.sh)

# Final re-assertion
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump

# ============================================================================
# Phase 16 — STRIP REDUNDANT FILTERS (override post-source)
# ============================================================================
# These were proxies for rug detection. RugCheck handles it directly.
export PGG2_PRICED_SNAP_MIN_BUY1500=2.0       # was 7.0 — just need non-zero activity
export PGG2_PRICED_SNAP_MIN_UNIQ1500=3        # was 8 — need >1 distinct buyer
export PGG2_PRICED_SNAP_MAX_TOP1500=0.95      # was 0.37 — effectively disabled
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500=5.0 # elite tier still selective
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500=6
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500=0.50
# Anti-bot disabled (covered by RugCheck top-holder check)
export PGG2_ANTIBOT_FILTER_ENABLED=0
# Volume-sustain effectively disabled (engagement signal replaces it)
export PGG2_PRICED_SNAP_MAX_LAST_BUY_AGE_MS=10000
# vSol sweet-spot disabled (was unverified)
export PGG2_PRICED_SNAP_MIN_VSOL_SWEET=0.0
export PGG2_PRICED_SNAP_MAX_VSOL_SWEET=999.0
# Keep entry-move window (price action timing, not rug detection)
export PGG2_PRICED_SNAP_MIN_MOVE=1.18
export PGG2_PRICED_SNAP_MAX_MOVE=2.50
# Keep age window (timing)
export PGG2_PRICED_SNAP_MAX_AGE_SEC=60.0

mkdir -p /root/piggy/logs /root/piggy/data
RUNID="${PGG2_RUN_PREFIX}_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

echo "PHASE16-DRYLIVE RUN_ID=$RUNID rugcheck_score=$RUGCHECK_REJECT_SCORE filters_loosened=YES"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
