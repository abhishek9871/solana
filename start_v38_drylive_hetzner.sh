#!/usr/bin/env bash
# v38 DRY-LIVE on Hetzner — mirrors live exactly, NO real SOL moves.
# Uses the same broker, RPC, blockhash commitment, and account discovery
# as start_pgg2_direct_live_candidate.sh. Only difference: quote_only=True
# in DirectPumpQuoteBroker blocks sendTransaction.
set -u
cd /root/piggy

# Load Hetzner .env (current SolanaTracker key + wallet keypair path).
set -a
. ./.env
set +a

# ---- DRY-LIVE EXECUTION (no real SOL) ----
export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_v38_drylive}"
export PGG2_EXECUTION_MODE=quote
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=1
export PGG2_LIVE_BROKER=direct_pump
export PGG2_WALLET_KEYPAIR="${PGG2_WALLET_KEYPAIR:-/root/piggy/live_wallet.key}"
export PGG2_QUOTE_SIMULATE=0
export PGG2_QUOTE_SHADOW_POSITIONS=1
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
export PGG2_LIVE_SKIP_PREFLIGHT=1
export PGG2_LIVE_PREFLIGHT_ROUNDTRIP_ENABLED=0
export PGG2_LIVE_FINAL_ENTRY_GUARD_ENABLED=0
export PGG2_DIRECT_SELECT_BUYBACK_BY_SIM=0
export PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK=0
export PGG2_DIRECT_OBSERVED_PAIR_FROM_RAW=1
export PGG2_DIRECT_REQUIRE_OBSERVED_BUYBACK_PAIR=1
export PGG2_DIRECT_ACCOUNT_COMMITMENT=processed
export PGG2_DIRECT_BLOCKHASH_COMMITMENT=processed
export PGG2_DIRECT_CURVE_ACCOUNT_TTL_SEC=0

# ---- v36c-3 strategy (will be gated by v38) ----
export PGG2_ACTUAL_ENTRY_MASTER_ENABLED=1
export PGG2_DRYLIVE_PILOT_ENABLED=1
export PGG2_DRYLIVE_PILOT_MAX_ENTRIES=10
export PGG2_DRYLIVE_PILOT_SOL=0.015
export PGG2_DRYLIVE_PILOT_TIMEBOX_MS=5000
export PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS=10000
export PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL=0.005
export PGG2_SCALP_ENABLED=1
export PGG2_SCALP_MAX_ENTRIES=10
export PGG2_SCALP_SOL=0.015
export PGG2_SCALP_MIN_ALL_IN_PNL_SOL=0.00060
export PGG2_SCALP_MAX_QUOTE_AGE_MS=750
export PGG2_SCALP_BANK_MIN_PNL_SOL=0.00020
export PGG2_SCALP_CLAMP_MAX_LOSS_SOL=0.00030
export PGG2_SCALP_TIMEBOX_MS=3000
export PGG2_SCALP_ABS_MAX_HOLD_MS=3000
export PGG2_SCALP_SESSION_LOSS_CAP_SOL=0.005
export PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=1
export PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0

# ---- v38 FLOW-DELAY GATE ----
# confirmed_mode banked +0.001593 on the 4stJ..pump live observation
# (single whale-buyer at 2.0 SOL + 1 post-entry buyer @ 2.0 SOL).
# processed_mode was too fast (220ms) to see the post-entry buy; the
# confirmed_mode (750ms) gives the flow window we actually need. We
# also relax the buyer-count floor to 1 since single-whale pre-flow
# is a valid signal when accompanied by sufficient SOL.
export PGG2_DRYLIVE_LIVE_EQUIV_MODE=flow_delay_v1
export PGG2_V38_FLOW_DELAY_MODE=confirmed_mode
export PGG2_V38_MIN_PNL_SOL=0.00020
export PGG2_V38_MIN_PRE_ENTRY_BUY_SOL=1.0
export PGG2_V38_MIN_PRE_ENTRY_BUYERS=1
export PGG2_V38_MAX_TOP_BUYER_SHARE=1.0
export PGG2_V38_ATA_RECOVERABLE=1
export PGG2_V38_FLOW_STALL_MS=750
export PGG2_V38_OPTIMISTIC_LIVE_OK=0

# ---- atomic ESB / Jito OFF ----
export PGG2_DRYLIVE_REQUIRE_ATOMIC_ESB_SIM=0
export PGG2_ATOMIC_ESB_COMPUTE_UNIT_LIMIT=600000
export PGG2_ATOMIC_ESB_COMPUTE_UNIT_PRICE_MICROLAMPORTS=22700
export PGG2_LIVE_ESB_JITO_OPTIONAL=0
unset PGG2_JITO_BLOCK_ENGINE_URL
unset PGG2_JITO_TIP_ACCOUNT
export PGG2_JITO_TIP_LAMPORTS=0

# ---- SLA, risk, shadow lab ----
export PGG2_SLA_TARGET_ENTRIES_20M=10
export PGG2_SLA_ZERO_NEGATIVE_REQUIRED=1
export PIGGY_MAX_OPEN_POSITIONS=1
export PGG2_SHADOW_LAB_ENABLED=1
export PGG2_SHADOW_OBSERVE_ENABLED=1
export PGG2_SHADOW_DELAYED_ENTRY_SCANNER=1
export PGG2_SHADOW_LAB_MAX_CONCURRENT=8
export PGG2_SHADOW_LAB_COOLDOWN_MS=20000
export PGG2_RISK_WORKER_POLL_SEC=0.020
export PGG2_RISK_QUOTE_STALE_MS=2000

# ---- Output ----
mkdir -p logs data
RUNID="${PGG2_RUN_PREFIX}_$(date +%Y%m%d_%H%M%S)"
export PIGGY_STATE_FILE="data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="data/${RUNID}_decisions.jsonl"
LOGFILE="logs/${RUNID}.log"
echo "$RUNID" > current_pgg2_runid.txt

echo "=== v38 DRY-LIVE HETZNER (mirrors live exactly, no real SOL) ==="
echo "  WS:        $SOLANATRACKER_RPC_WS"
echo "  wallet:    $PGG2_WALLET_KEYPAIR"
echo "  v38 mode:  $PGG2_V38_FLOW_DELAY_MODE"
echo "  pre buy:   >=$PGG2_V38_MIN_PRE_ENTRY_BUY_SOL SOL, >=$PGG2_V38_MIN_PRE_ENTRY_BUYERS buyers"
echo "  log:       $LOGFILE"
echo "==============================================================="

# Use the venv if it exists.
if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi

exec "$PY" -u PGG2.py \
  --ws "$SOLANATRACKER_RPC_WS" \
  --state "$PIGGY_STATE_FILE" \
  --raw-log "$PIGGY_RAW_EVENTS_FILE" \
  --decisions "$PIGGY_DECISIONS_FILE" \
  --run-seconds 1200 \
  2>&1 | tee "$LOGFILE"
