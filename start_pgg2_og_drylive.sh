#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy
set -a
source .env 2>/dev/null || true
set +a

RUN_ID="${RUN_ID:-pgg2_og_drylive_$(date -u +%Y%m%d_%H%M%S)}"

# Keep the OG paper-winning signal path. Dry-live only changes accounting.
export PGG2_EXECUTION_MODE="${PGG2_EXECUTION_MODE:-dry_live}"
export PGG2_DRY_LIVE_MODE="${PGG2_DRY_LIVE_MODE:-1}"
export PIGGY_PAPER_TRADING="${PIGGY_PAPER_TRADING:-1}"

# Paper-winning live-sized bankroll configuration.
export PIGGY_SCOUT_SOL="${PIGGY_SCOUT_SOL:-0.0500}"
export PIGGY_MAX_POSITION_SOL="${PIGGY_MAX_POSITION_SOL:-0.0500}"
export PIGGY_MAX_OPEN_POSITIONS="${PIGGY_MAX_OPEN_POSITIONS:-3}"
export PIGGY_PAPER_DRAG_BPS="${PIGGY_PAPER_DRAG_BPS:-280}"

# Cost-elimination assumptions from 2026-05-06 research:
# direct execution removes Solana Tracker's 0.5% platform fee, and closing
# empty token accounts recovers ATA rent after a full sell.
export PGG2_DRY_LIVE_DIRECT_EXECUTION="${PGG2_DRY_LIVE_DIRECT_EXECUTION:-1}"
export PGG2_DRY_LIVE_PLATFORM_FEE_BPS="${PGG2_DRY_LIVE_PLATFORM_FEE_BPS:-0}"
export PGG2_DRY_LIVE_RECOVER_ATA_RENT="${PGG2_DRY_LIVE_RECOVER_ATA_RENT:-1}"
export PGG2_DRY_LIVE_ATA_RENT_SOL="${PGG2_DRY_LIVE_ATA_RENT_SOL:-0.002039280}"
export PGG2_DRY_LIVE_BASE_TX_FEE_SOL="${PGG2_DRY_LIVE_BASE_TX_FEE_SOL:-0.000005}"
export PGG2_DRY_LIVE_PRIORITY_FEE_SOL="${PGG2_DRY_LIVE_PRIORITY_FEE_SOL:-0.000000}"
export PGG2_DRY_LIVE_CLOSE_ACCOUNT_FEE_SOL="${PGG2_DRY_LIVE_CLOSE_ACCOUNT_FEE_SOL:-0.000005}"
export PGG2_DRY_LIVE_EXTRA_FEE_BPS="${PGG2_DRY_LIVE_EXTRA_FEE_BPS:-0}"

export PIGGY_STATE_FILE="/root/piggy/data/${RUN_ID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUN_ID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUN_ID}_decisions.jsonl"

echo "${RUN_ID}" > /root/piggy/current_pgg2_og_drylive_runid.txt
echo "RUN_ID=${RUN_ID}"
echo "LOG=/root/piggy/logs/${RUN_ID}.log"

exec ./venv/bin/python -u /root/piggy/PGG2_OG_DRYLIVE.py "$@" 2>&1 | tee "/root/piggy/logs/${RUN_ID}.log"
