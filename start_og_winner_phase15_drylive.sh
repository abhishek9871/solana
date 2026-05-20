#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 15 2026-05-08 — OG WINNER + signal-scraping layer (RugCheck + engagement).
# Sources OG winner's strategy config. Adds RugCheck pre-buy gate (rejects rugs)
# and pump.fun engagement poller (relaxes filters for livestream/replied tokens).

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-og_phase15_drylive}"

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
export SMART_WALLET_WS_ENABLED=0  # disable smart-wallet (memory says lossy)

# Phase 15A — RugCheck pre-buy gate
export RUGCHECK_ENABLED=1
export RUGCHECK_REJECT_SCORE=4              # reject score >= 4 (1-10 scale)
export RUGCHECK_TIMEOUT_SEC=0.45            # fail-open if slower
export RUGCHECK_CACHE_TTL_SEC=300           # 5 min cache
export PGG2_RUGCHECK_GATE_ENABLED=1         # enable gate in priced_snap_ready

# Phase 15B — Pump.fun engagement poller
export ENGAGEMENT_POLL_ENABLED=1
export ENGAGEMENT_POLL_SEC=4.0              # poll every 4s
export ENGAGEMENT_POLL_LIMIT=50             # currently-live top 50

# Phase 15 — Engagement-based filter relaxation
export PGG2_ENGAGEMENT_RELAX=1.30           # 30% looser filters when engaged
export PGG2_KOTH_RELAX=1.50                 # 50% looser when KOTH (highest tier)

# Source OG strategy config
source <(sed '/^exec \.\/start_pgg2_attack_paper.sh/,$d' ./start_pgg2_direct_live_candidate.sh)

# Re-assert dry-live after sourcing
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

mkdir -p /root/piggy/logs /root/piggy/data
RUNID="${PGG2_RUN_PREFIX}_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

echo "PHASE15-DRYLIVE RUN_ID=$RUNID rugcheck=$RUGCHECK_ENABLED engagement=$ENGAGEMENT_POLL_ENABLED"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
