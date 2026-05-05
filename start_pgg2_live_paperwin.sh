#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy
RUNID="pgg2_live_paperwin_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt

export PGG2_EXECUTION_MODE=live
export PGG2_ENABLE_LIVE=1
export PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK
export PIGGY_PAPER_TRADING=0

export PGG2_WALLET_KEYPAIR="${PGG2_WALLET_KEYPAIR:-/root/piggy/live_wallet.key}"

# Mirrors the winning pgg2_artgate_hetzner_20260505_135326 paper config.
export PIGGY_NO_FOLLOW_CAP_ON=1
export PIGGY_MOON_HARD_BREAK_MULT=0.88
export PIGGY_MAX_OPEN_POSITIONS=3
export PIGGY_SCOUT_SOL=0.020
export PIGGY_MAX_POSITION_SOL=0.200

export PGG2_BIRTH_FANOUT_ENABLED=1
export PGG2_BIRTH_FANOUT_SCALE_ENABLED=0
export PGG2_BIRTH_FANOUT_MIN_LIVE_BUYERS700=7
export PGG2_BIRTH_FANOUT_MAX_LIVE_TOP700=0.62
export PGG2_REJECT_LATE_WHALE_DRAG=1
export PGG2_SCALE_MIN_ENTRY_UNIQ700=4
export PGG2_SCALE_MAX_ENTRY_TOP700=0.55
export PGG2_SECOND_WAVE_FORCE_SCOUT=1
export PGG2_SECOND_WAVE_MAX_SCOUT_SOL=0.020
export PGG2_CURVE_LAG_REVEAL_ENABLED=1
export PGG2_CURVE_LAG_SOL=0.020
export PGG2_CURVE_LAG_MIN_LIVE_BUY700_SOL=5.0
export PGG2_CURVE_LAG_MIN_LIVE_BUYERS700=5
export PGG2_CURVE_LAG_MAX_LIVE_TOP700=0.70
export PGG2_CURVE_LAG_MAX_ENTRY_MOVE=1.25
export PGG2_PROFIT_REENTRY_LOCK_MIN_PNL_SOL=0.001

# Live execution safety: same signal sizing, but no sub-paper-size trades.
export PGG2_LIVE_MAX_TRADE_SOL=0.020
export PGG2_LIVE_MIN_TRADE_SOL=0.010
export PGG2_LIVE_MIN_WALLET_RESERVE_SOL=0.020
export PGG2_LIVE_MAX_SESSION_LOSS_SOL=0.025
export PGG2_LIVE_MAX_CONSECUTIVE_LOSSES=2
export PGG2_LIVE_PRIORITY_FEE=auto
export PGG2_LIVE_PRIORITY_LEVEL=high
export PGG2_LIVE_BUY_SLIPPAGE_PCT=18
export PGG2_LIVE_SELL_SLIPPAGE_PCT=22
export PGG2_LIVE_TX_VERSION=legacy
# Paper did not do an extra RPC simulation before entry. Keep live execution fast
# and let sendTransaction's normal preflight handle invalid transactions.
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
# Enter management immediately after buy sendTransaction, matching paper timing.
# Sells still use exact wallet delta and refuse false profit exits.
export PGG2_LIVE_FAST_PAPER_ACCOUNTING=1
export PGG2_LIVE_MIN_PROFIT_EXIT_SOL=0.0000
export PGG2_LIVE_RPC_URL="${PGG2_LIVE_RPC_URL:-https://api.mainnet-beta.solana.com}"

export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
