#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

RUNID="corrected_dry_10usd_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt

# Source the $36-bankroll env profile
set -a
source ./same_block_piggyback_36usd.env
set +a

# OVERRIDE: dry-live mode (cost-modeled, no real SOL)
export PGG2_EXECUTION_MODE=dry_live
export PIGGY_PAPER_TRADING=true
export PGG2_ENABLE_LIVE=false

# Dry-live cost model (matches direct pump.fun bonding curve)
export PGG2_DRY_LIVE_MODE=1
export PGG2_DRY_LIVE_DIRECT_EXECUTION=1
export PGG2_DRY_LIVE_PLATFORM_FEE_BPS=0
export PGG2_DRY_LIVE_PROTOCOL_FEE_LABEL=pump_fun_bonding_curve_direct
export PGG2_DRY_LIVE_PROTOCOL_FEE_BPS=100
export PGG2_DRY_LIVE_RECOVER_ATA_RENT=1
export PGG2_DRY_LIVE_ATA_RENT_SOL=0.002039280
export PGG2_DRY_LIVE_BASE_TX_FEE_SOL=0.000005
export PGG2_DRY_LIVE_PRIORITY_FEE_SOL=0.000005
export PGG2_DRY_LIVE_CLOSE_ACCOUNT_FEE_SOL=0.000005
export PGG2_DRY_LIVE_EXTRA_FEE_BPS=0
export PIGGY_PAPER_DRAG_BPS=180

# OVERRIDE: $10 max position on confirmed winners (was 0.040 = $3.54)
# Scout stays at $0.53 — losses still microscopic if trade fails before scale-up
# At SOL ~ $88.52, $10 = 0.113 SOL
export PIGGY_SCOUT_SOL=0.006              # $0.53 initial probe (UNCHANGED)
export PIGGY_MAX_POSITION_SOL=0.113       # $10 target on confirmation (was 0.040)
export PIGGY_PROBE_SOL=0.006              # match scout

# Enable scale-up so probe → max actually happens on confirmation
# These were disabled in the env file. Re-enable so winners scale up to $10.
export PIGGY_SCALING_ENABLED=true
export PGG2_BIRTH_FANOUT_SCALE_ENABLED=true
export PGG2_SPARK3_BREAKOUT_SCALE_ENABLED=true
export PGG2_BREADTH_SCALE_ENABLED=true
export PGG2_SPARK3_ARM_SCALE_ENABLED=true

# Stream / connection defaults
export BIRTH_MAX_DECODED_TRADE_SOL=250
export BIRTH_RECONNECT_BASE_SEC=3
export BIRTH_RECONNECT_CLEAN_WAIT_SEC=4
export BIRTH_RECONNECT_MAX_SEC=30
export BIRTH_RECONNECT_POLICY_LIMIT_SEC=45

# Disable smart-wallet WS (background task from birth_first_sniper.py init)
export SMART_WALLET_WS_ENABLED=0

# State / log paths
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

mkdir -p /root/piggy/logs /root/piggy/data
echo "CORRECTED-DRYLIVE-10USD: RUNID=$RUNID mode=$PGG2_EXECUTION_MODE bankroll=$PGG2_BANKROLL_SOL scout=$PIGGY_SCOUT_SOL max_pos=$PIGGY_MAX_POSITION_SOL"

exec ./venv/bin/python -u same_block_piggyback_corrected.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
