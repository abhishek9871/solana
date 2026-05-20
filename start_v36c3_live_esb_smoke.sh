#!/usr/bin/env bash
# v36c-3 ONE-ENTRY LIVE ESB SMOKE LAUNCHER — single-tx atomic mode.
#
# This launcher mirrors the EXACT v36c-3 dry-live strategy in live mode
# WITHOUT requiring a Jito tip. Buy + sell + cleanup are packed into one
# atomic Solana transaction; if any instruction fails, all revert.
#
# Strategy is UNCHANGED from dry-live:
#   - Entry Snapshot Bank ON
#   - scalp/no-hold bank/scratch ON
#   - v33 route-aware all-in PnL ON
#   - QuoteManager ON
#   - direct pump_bc only
#   - sim_needed=0 only
#   - cost_model_confidence=proven only
#   - pair_source current_sig / cache / prewarmed / observed_raw_rpc only
#
# Execution mode hierarchy (NO silent fallbacks):
#   1. single_tx_atomic — preferred, no Jito tip required (THIS LAUNCHER)
#   2. jito_bundle     — optional; user opt-in only
#   3. sequential_buy_sell — NEVER for ESB (does not mirror dry-live)
#   4. protected_hold   — different strategy; not allowed as substitute
#
# DO NOT RUN WITHOUT EXPLICIT OPERATOR AUTHORIZATION. REAL SOL WILL MOVE.
#
# Required preflight:
#   1. git log -1 --oneline                       # v36c3 release commit
#   2. git tag --contains HEAD                    # v36c3-drylive-10w0l
#   3. sha256sum diff vs RELEASE_V36C3_CHECKSUMS.txt → zero mismatches
#   4. Wallet balance >= 0.025 SOL
#   5. AST clean both ends
#   6. AtomicSingleTxESBExecutor sim-only pass on >= 10 candidates with
#      zero negative simulated wallet deltas (Phase 6).
#
# After the one atomic tx confirms (or fails safely), STOP THE BOT:
#   tmux kill-session -t bot
#   pkill -9 -f "python -u PGG2.py"
# Then reconcile actual wallet delta vs predicted PnL. Difference must be
# <= PGG2_LIVE_ESB_MAX_PNL_DRIFT_SOL=0.00050.

set -u
cd /root/piggy

# Live execution mode — REAL TRANSACTIONS WILL BE SUBMITTED.
export PGG2_EXECUTION_MODE=live
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=0
export PGG2_LIVE_BROKER=direct_pump
export PGG2_WALLET_KEYPAIR="${PGG2_WALLET_KEYPAIR:-/root/piggy/live_wallet.key}"
export PGG2_LIVE_CONFIRM="I_ACCEPT_REAL_SOL_RISK"
export PGG2_QUOTE_SHADOW_POSITIONS=0

# ===== EXACT v36c-3 STRATEGY (UNCHANGED) =====
export PGG2_ACTUAL_ENTRY_MASTER_ENABLED=1
export PGG2_DRYLIVE_PILOT_ENABLED=1
export PGG2_DRYLIVE_PILOT_MAX_ENTRIES=1
export PGG2_DRYLIVE_PILOT_SOL=0.015
export PGG2_DRYLIVE_PILOT_MIN_IMMEDIATE_PNL_SOL=-0.005
export PGG2_DRYLIVE_PILOT_TIMEBOX_MS=5000
export PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS=10000
export PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL=0.005
export PGG2_DRYLIVE_PILOT_MAX_BUY_IMPACT=0.005
export PGG2_DRYLIVE_PILOT_MARK_INTERVAL_MS=250
export PGG2_MAX_ENTRY_QUOTE_AGE_MS=1500
export PGG2_PREENTRY_MIN_ALL_IN_PNL_SOL=0.00150
export PGG2_PREENTRY_LATENCY_ADJUSTED_FLOOR_SOL=0.00150
export PGG2_LATENCY_MAX_P95_MS=1100.0

# Scalp + ESB — ON (the live equivalent is now atomic single-tx).
export PGG2_SCALP_ENABLED=1
export PGG2_SCALP_MAX_ENTRIES=1
export PGG2_SCALP_SOL=0.015
export PGG2_SCALP_MIN_ALL_IN_PNL_SOL=0.00060
export PGG2_SCALP_MAX_QUOTE_AGE_MS=750
export PGG2_SCALP_BANK_MIN_PNL_SOL=0.00020
export PGG2_SCALP_CLAMP_MAX_LOSS_SOL=0.00030
export PGG2_SCALP_TIMEBOX_MS=3000
export PGG2_SCALP_ABS_MAX_HOLD_MS=3000
export PGG2_SCALP_SESSION_LOSS_CAP_SOL=0.005

# Entry Snapshot Bank — live executor mode: single_tx_atomic (NO Jito).
export PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=1
export PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=1
export PGG2_LIVE_ESB_MODE=single_tx_atomic
export PGG2_LIVE_ESB_MAX_PNL_DRIFT_SOL=0.00050
export PGG2_LIVE_ESB_MIN_ALL_IN_PNL_SOL=0.00060

# Mirror dry-live with atomic sim: every dry-live ESB entry must ALSO
# pass the atomic simulation, so dry-live's "ESB-valid" set equals live's.
export PGG2_DRYLIVE_REQUIRE_ATOMIC_ESB_SIM=1

# Single-tx execution knobs.
export PGG2_ATOMIC_ESB_COMPUTE_UNIT_LIMIT=600000
export PGG2_ATOMIC_ESB_COMPUTE_UNIT_PRICE_MICROLAMPORTS=22700
export PGG2_ATOMIC_ESB_CONFIRM_TIMEOUT_SEC=8.0

# Jito remains available as optional opt-in path. Default OFF here.
export PGG2_LIVE_ESB_JITO_OPTIONAL=0
export PGG2_JITO_BLOCK_ENGINE_URL="${PGG2_JITO_BLOCK_ENGINE_URL:-}"
export PGG2_JITO_TIP_ACCOUNT="${PGG2_JITO_TIP_ACCOUNT:-}"
export PGG2_JITO_TIP_LAMPORTS="${PGG2_JITO_TIP_LAMPORTS:-0}"

# Concurrency: ONE position for smoke.
export PIGGY_MAX_OPEN_POSITIONS=1

# Live execution tolerances.
export PGG2_LIVE_MAX_TRADE_SOL=0.015
export PGG2_LIVE_MIN_TRADE_SOL=0.015
export PGG2_LIVE_MIN_WALLET_RESERVE_SOL=0.080
export PGG2_LIVE_MAX_SESSION_LOSS_SOL=0.005
export PGG2_LIVE_MAX_CONSECUTIVE_LOSSES=1
export PGG2_LIVE_BUY_SLIPPAGE_PCT=18.0
export PGG2_LIVE_SELL_SLIPPAGE_PCT=22.0
export PGG2_LIVE_PRIORITY_FEE=auto
export PGG2_LIVE_PRIORITY_LEVEL=high
export PGG2_LIVE_TX_VERSION=legacy
export PGG2_LIVE_SIMULATE_BEFORE_SEND=1
export PGG2_LIVE_HTTP_TIMEOUT_SEC=4.0
export PGG2_LIVE_HTTP_RETRIES=2

# SLA hard config.
export PGG2_SLA_TARGET_ENTRIES_20M=10
export PGG2_SLA_ZERO_NEGATIVE_REQUIRED=1

# Shadow lab off for smoke focus.
export PGG2_SHADOW_LAB_ENABLED=0
export PGG2_SHADOW_OBSERVE_ENABLED=0
export PGG2_SHADOW_DELAYED_ENTRY_SCANNER=0
export PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=0

# Risk worker tight monitoring.
export PGG2_RISK_WORKER_POLL_SEC=0.020
export PGG2_RISK_QUOTE_STALE_MS=2000
export PGG2_RISK_ALLOW_OVERLAP_QUOTES=0

echo "=== v36c3 LIVE ESB SMOKE — SINGLE-TX ATOMIC ==="
echo "    mode:                  $PGG2_EXECUTION_MODE"
echo "    ESB mode:              $PGG2_LIVE_ESB_MODE  (jito optional: $PGG2_LIVE_ESB_JITO_OPTIONAL)"
echo "    confirm env:           PGG2_LIVE_CONFIRM=$PGG2_LIVE_CONFIRM"
echo "    wallet keypair:        $PGG2_WALLET_KEYPAIR"
echo "    max entries:           pilot=$PGG2_DRYLIVE_PILOT_MAX_ENTRIES  scalp=$PGG2_SCALP_MAX_ENTRIES"
echo "    max open positions:    $PIGGY_MAX_OPEN_POSITIONS"
echo "    trade size:            $PGG2_LIVE_MAX_TRADE_SOL SOL"
echo "    session loss cap:      $PGG2_LIVE_MAX_SESSION_LOSS_SOL SOL"
echo "    ESB enabled:           $PGG2_ENTRY_SNAPSHOT_BANK_ENABLED"
echo "    ESB live eligible:     $PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE"
echo "    PnL drift tolerance:   $PGG2_LIVE_ESB_MAX_PNL_DRIFT_SOL SOL"
echo "==============================================="
echo "PGG2-LIVE-ESB-MODE mode=$PGG2_LIVE_ESB_MODE jito_required=0"
echo "PGG2-LIVE-ATOMIC-ESB-SMOKE-START launcher=v36c3 mode=$PGG2_EXECUTION_MODE size=$PGG2_LIVE_MAX_TRADE_SOL"

exec ./start_pgg2_attack_paper.sh
