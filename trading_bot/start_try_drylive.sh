#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

RUNID="try_drylive_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt

# DRY-LIVE mode (no real SOL movement, signals charged with direct-pump cost model)
export PGG2_EXECUTION_MODE="${PGG2_EXECUTION_MODE:-dry_live}"
export PGG2_ENABLE_LIVE="${PGG2_ENABLE_LIVE:-0}"
export PIGGY_PAPER_TRADING="${PIGGY_PAPER_TRADING:-1}"
export PGG2_DRY_LIVE_MODE="${PGG2_DRY_LIVE_MODE:-1}"
export PGG2_DRY_LIVE_DIRECT_EXECUTION="${PGG2_DRY_LIVE_DIRECT_EXECUTION:-1}"
export PGG2_DRY_LIVE_PLATFORM_FEE_BPS="${PGG2_DRY_LIVE_PLATFORM_FEE_BPS:-0}"
export PGG2_DRY_LIVE_PROTOCOL_FEE_LABEL="${PGG2_DRY_LIVE_PROTOCOL_FEE_LABEL:-pump_fun_bonding_curve_direct}"
export PGG2_DRY_LIVE_PROTOCOL_FEE_BPS="${PGG2_DRY_LIVE_PROTOCOL_FEE_BPS:-100}"
export PGG2_DRY_LIVE_RECOVER_ATA_RENT="${PGG2_DRY_LIVE_RECOVER_ATA_RENT:-1}"
export PGG2_DRY_LIVE_ATA_RENT_SOL="${PGG2_DRY_LIVE_ATA_RENT_SOL:-0.002039280}"
export PGG2_DRY_LIVE_BASE_TX_FEE_SOL="${PGG2_DRY_LIVE_BASE_TX_FEE_SOL:-0.000005}"
export PGG2_DRY_LIVE_PRIORITY_FEE_SOL="${PGG2_DRY_LIVE_PRIORITY_FEE_SOL:-0.000005}"
export PGG2_DRY_LIVE_CLOSE_ACCOUNT_FEE_SOL="${PGG2_DRY_LIVE_CLOSE_ACCOUNT_FEE_SOL:-0.000005}"
export PGG2_DRY_LIVE_EXTRA_FEE_BPS="${PGG2_DRY_LIVE_EXTRA_FEE_BPS:-0}"
export PIGGY_PAPER_DRAG_BPS=180

# User's spec'd env vars
export PIGGY_BANKROLL_SOL=0.21

# Active lanes
export PGG2_BIRTH_FANOUT_ENABLED=1
export PGG2_CURVE_LAG_REVEAL_ENABLED=1
export PGG2_EARLY_IGNITION_ENABLED=1

# Disabled lanes
export PGG2_SECOND_WAVE_ENABLED=0
export PIGGY_SCALING_ENABLED=0

# Hard stop and trailing
export PIGGY_HARD_STOP_MULT=0.88
export PIGGY_TRAIL_15X=0.88
export PIGGY_TRAIL_2X=0.82
export PIGGY_TRAIL_3X=0.75
export PIGGY_TRAIL_5X=0.70

# Circuit breaker
export PIGGY_CIRCUIT_BREAKER_COOLDOWN_MS=300000
export PIGGY_DAILY_LOSS_PCT=0.20

# Time stops
export PIGGY_MOON_FAIL_SEC=8.0
export PIGGY_HARD_TIME_STOP_SEC=45.0
export PIGGY_NO_FOLLOW_AFTER_SEC=4.0

# Stream / connection defaults
export BIRTH_MAX_DECODED_TRADE_SOL=250
export BIRTH_RECONNECT_BASE_SEC=3
export BIRTH_RECONNECT_CLEAN_WAIT_SEC=4
export BIRTH_RECONNECT_MAX_SEC=30
export BIRTH_RECONNECT_POLICY_LIMIT_SEC=45

# State / log paths
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

mkdir -p /root/piggy/logs /root/piggy/data
echo "TRY-LAUNCH: RUNID=$RUNID mode=$PGG2_EXECUTION_MODE bankroll=$PIGGY_BANKROLL_SOL"

exec ./venv/bin/python -u try.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
