#!/usr/bin/env bash
# Stage A — 1-entry guarded live run.
# Wraps start_pgg2_v39b_quote_rescue_live_mirror.sh with Stage A overrides:
#   - target_entries=1, max_open=1
#   - explicit pre-send guard knobs (slip ceilings, floor lamports)
#   - emergency override paths disabled
set -euo pipefail
cd /root/piggy

# Stage A — single entry, single open
export PGG2_V39_TARGET_ENTRIES=1
export PGG2_V39_MAX_OPEN=1
export PIGGY_MAX_OPEN=1
export PIGGY_MAX_OPEN_POSITIONS=1

# Trade size at 0.015 SOL per user spec (no 0.05).
export PGG2_LIVE_MIN_TRADE_SOL=0.015
export PGG2_LIVE_MAX_TRADE_SOL=0.015

# Phase 4 pre-send guard knobs
export PGG2_V39_LIVE_BUY_GUARD_EPSILON_UI=0.001
export PGG2_V39_SELL_MIN_SOL_FLOOR_LAMPORTS=100

# PR-Phase 1: forbid legacy EXIT_ANY override that encoded min_sol=1 lamport
export PGG2_V39_LIVE_FORBID_EXIT_ANY=1

# PR2-Phase 1+3: principal recovery exit
export PGG2_V39_PRINCIPAL_RECOVERY_EXIT=1
export PGG2_V39_RECOVERY_PROFIT_BUFFER_SOL=0.00005
export PGG2_V39_RECOVERY_MAX_RETRIES=60
export PGG2_V39_RECOVERY_RETRY_MS=500
export PGG2_V39_RECOVERY_MAX_FRACTION=0.95

# Disable emergency override paths that can re-introduce min=1 lamport sell
export PGG2_V39_LIVE_EMERGENCY_SELL_ON_HARD_LOSS=0
export PGG2_V39_LIVE_PROTECTED_SELL_ALLOW_NEGATIVE_CLAMP=0

# Tag run prefix
export PGG2_RUN_PREFIX="pgg2_v39b_stage_a_pr2_recovery"

exec ./start_pgg2_v39b_quote_rescue_live_mirror.sh
