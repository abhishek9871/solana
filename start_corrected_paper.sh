#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

RUNID="corrected_paper_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt

# Source the $36-bankroll env profile
set -a
source ./same_block_piggyback_36usd.env
set +a

# Stream / connection defaults
export BIRTH_MAX_DECODED_TRADE_SOL=250
export BIRTH_RECONNECT_BASE_SEC=3
export BIRTH_RECONNECT_CLEAN_WAIT_SEC=4
export BIRTH_RECONNECT_MAX_SEC=30
export BIRTH_RECONNECT_POLICY_LIMIT_SEC=45

# Disable the smart-wallet WS that gets auto-init'd from birth_first_sniper.py
export SMART_WALLET_WS_ENABLED=0

# State / log paths
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

mkdir -p /root/piggy/logs /root/piggy/data
echo "CORRECTED-LAUNCH: RUNID=$RUNID mode=$PGG2_EXECUTION_MODE bankroll=$PGG2_BANKROLL_SOL"

exec ./venv/bin/python -u same_block_piggyback_corrected.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
