#!/usr/bin/env bash
# v36c-3 ONE-ENTRY LIVE SMOKE LAUNCHER — DO NOT RUN WITHOUT EXPLICIT
# OPERATOR AUTHORIZATION. THIS SCRIPT SUBMITS REAL TRANSACTIONS.
#
# Audit context (PRODUCTION_LIVE_READINESS_V36C3.md):
#   - Entry Snapshot Bank live-equivalence is NOT proven.
#   - This smoke therefore runs the PROTECTED-HOLD PRIMARY RULE ONLY
#     (v33_quote_edge_150_C, holdout-validated 10W/0L independently).
#   - Scalp is disabled. ESB is left at default (blocked in live mode
#     by PGG2-LIVE-EQUIVALENCE-BLOCK).
#   - Size: 0.015 SOL, max entries: 1, max open: 1, session loss cap:
#     0.005 SOL. Stop manually after first close.
#
# Required operator actions before launch:
#   1. Confirm git release commit + tag are pushed:
#        git log -1 --oneline
#        git tag --contains HEAD     # must include v36c3-drylive-10w0l
#   2. Diff remote checksums against RELEASE_V36C3_CHECKSUMS.txt:
#        ssh root@<host> "cd /root/piggy && sha256sum <files>"
#   3. Confirm wallet balance >= 0.020 SOL:
#        solana balance <pubkey>
#   4. Confirm PGG2_LIVE_CONFIRM is unset in the parent shell BEFORE
#      starting this script. It is set INSIDE the script for one
#      invocation only.
#
# After the one entry closes (W or L), STOP THE BOT:
#   tmux kill-session -t bot
#   pkill -9 -f "python -u PGG2.py"
#
# Then reconcile actual wallet delta vs PGG2-QUOTE-SHADOW-SELL all_in_pnl
# emitted in the log. Difference must be <= 0.0005 SOL to proceed to a
# 3-entry smoke in a separate session.

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

# Quote-shadow-positions must be OFF for live mode (positions are real now).
export PGG2_QUOTE_SHADOW_POSITIONS=0

# Master + primary pilot ON for ONE entry. Scalp OFF (ESB-dependent, not live-equivalent).
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

# Scalp + ESB explicitly disabled for live smoke.
export PGG2_SCALP_ENABLED=0
export PGG2_SCALP_MAX_ENTRIES=0
export PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=0
export PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0

# Concurrency: ONE position only for smoke.
export PIGGY_MAX_OPEN_POSITIONS=1

# SLA hard config so the bot logs target on boot.
export PGG2_SLA_TARGET_ENTRIES_20M=10
export PGG2_SLA_ZERO_NEGATIVE_REQUIRED=1

# Live execution tolerances (kept conservative for smoke).
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
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_HTTP_TIMEOUT_SEC=4.0
export PGG2_LIVE_HTTP_RETRIES=2

# Shadow lab off to keep the smoke focused on the one live entry.
export PGG2_SHADOW_LAB_ENABLED=0
export PGG2_SHADOW_OBSERVE_ENABLED=0
export PGG2_SHADOW_DELAYED_ENTRY_SCANNER=0
export PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=0

# Risk worker tight monitoring.
export PGG2_RISK_WORKER_POLL_SEC=0.020
export PGG2_RISK_QUOTE_STALE_MS=2000
export PGG2_RISK_ALLOW_OVERLAP_QUOTES=0

# Dry-live launcher chain inherits the rest of the config but everything
# above takes precedence because of the `${VAR:-default}` pattern in the
# base launcher.

echo "=== v36c3 LIVE SMOKE — REAL TRANSACTIONS WILL BE SUBMITTED ==="
echo "    confirm env: PGG2_LIVE_CONFIRM=$PGG2_LIVE_CONFIRM"
echo "    wallet keypair: $PGG2_WALLET_KEYPAIR"
echo "    max entries: $PGG2_DRYLIVE_PILOT_MAX_ENTRIES"
echo "    max open positions: $PIGGY_MAX_OPEN_POSITIONS"
echo "    trade size: $PGG2_LIVE_MAX_TRADE_SOL SOL"
echo "    session loss cap: $PGG2_LIVE_MAX_SESSION_LOSS_SOL SOL"
echo "    Entry Snapshot Bank: $PGG2_ENTRY_SNAPSHOT_BANK_ENABLED (live eligibility: $PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE)"
echo "    Scalp: $PGG2_SCALP_ENABLED"
echo "==============================================================="
echo "PGG2-LIVE-SMOKE-START launcher=v36c3 mode=$PGG2_EXECUTION_MODE size=$PGG2_LIVE_MAX_TRADE_SOL"

exec ./start_pgg2_attack_paper.sh
