#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# OG WINNER (commit 7128dc7) running in DRY-LIVE mode.
# Sources the OG winner's live-candidate strategy config WITHOUT its exec line,
# then overrides execution to dry-live (cost-modeled, no real SOL).

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-og_winner_drylive}"

# DRY-LIVE OVERRIDES (applied BEFORE sourcing so launcher defaults adopt them)
export PGG2_EXECUTION_MODE=quote
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=1

# Disable live confirmation gates (those are for live mode only)
export PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK
export PGG2_DIRECT_LIVE_CONFIRM=I_ACCEPT_DIRECT_PUMP_RISK

# Quote/dry-live shadow mode
export PGG2_QUOTE_SHADOW_POSITIONS=1
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
export PGG2_LIVE_SKIP_PREFLIGHT=1

# Disable smart-wallet WS (left over from prior phases)
export SMART_WALLET_WS_ENABLED=0

# Source the OG winner's live-candidate strategy config WITHOUT its final exec.
# That sets PRICED_SNAP filters, slippage tolerances, stake profile.
source <(sed '/^exec \.\/start_pgg2_attack_paper.sh/,$d' ./start_pgg2_direct_live_candidate.sh)

# Re-assert dry-live execution after sourcing (in case the candidate clobbered)
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump

# Source the attack paper config WITHOUT its final python exec.
source <(sed '/^exec \.\/venv\/bin\/python -u PGG2.py/,$d' ./start_pgg2_attack_paper.sh)

# Final re-assertion of dry-live mode
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

echo "OG-WINNER-DRYLIVE RUN_ID=$RUNID mode=$PGG2_EXECUTION_MODE bankroll=$(echo $PGG2_BANKROLL_SOL 2>/dev/null || echo 'inherited')"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
