#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Uses the production-candidate strategy config from start_pgg2_attack_paper.sh.
# This wrapper changes only the execution layer: direct Pump/PumpSwap tx builder,
# real live mode, and no third-party swap transaction builder.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_direct_live}"
export PGG2_EXECUTION_MODE=live
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=0
export PGG2_LIVE_BROKER=direct_pump

# Two explicit gates are required before any real SOL can move.
export PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK
export PGG2_DIRECT_LIVE_CONFIRM=I_ACCEPT_DIRECT_PUMP_RISK

export PGG2_WALLET_KEYPAIR="${PGG2_WALLET_KEYPAIR:-/root/piggy/live_wallet.key}"

# Match dry-run timing: build the direct Pump/PumpSwap tx and send immediately.
# Do not add a live-only simulate/preflight round trip at entry time.
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
export PGG2_LIVE_SKIP_PREFLIGHT=1
export PGG2_LIVE_PREFLIGHT_ROUNDTRIP_ENABLED=0
export PGG2_LIVE_FINAL_ENTRY_GUARD_ENABLED=0
export PGG2_DIRECT_SELECT_BUYBACK_BY_SIM=0
export PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK=0
export PGG2_DIRECT_OBSERVED_PAIR_FROM_RAW=1
export PGG2_DIRECT_OBSERVED_PAIR_MAX_SIGS="${PGG2_DIRECT_OBSERVED_PAIR_MAX_SIGS:-10}"
export PGG2_DIRECT_OBSERVED_PAIR_TAIL_BYTES="${PGG2_DIRECT_OBSERVED_PAIR_TAIL_BYTES:-8388608}"
export PGG2_DIRECT_PUMP_REMAINING_CACHE="${PGG2_DIRECT_PUMP_REMAINING_CACHE:-/root/piggy/data/pgg2_pump_remaining_cache.json}"
export PGG2_DIRECT_REQUIRE_OBSERVED_BUYBACK_PAIR=1
export PGG2_DIRECT_ACCOUNT_COMMITMENT=processed
export PGG2_DIRECT_BLOCKHASH_COMMITMENT=processed
export PGG2_DIRECT_CURVE_ACCOUNT_TTL_SEC=0

# Live-priced snaps have real rent, priority fee, and wallet fill drag. Keep
# the fast lane active, but only let broad, clean snaps through; weak standard
# snaps are minimum-size while elite broad snaps keep the dry-run attack size.
export PGG2_PRICED_SNAP_MIN_BUY1500="${PGG2_PRICED_SNAP_MIN_BUY1500:-7.0}"
export PGG2_PRICED_SNAP_MIN_UNIQ1500="${PGG2_PRICED_SNAP_MIN_UNIQ1500:-8}"
export PGG2_PRICED_SNAP_MAX_TOP1500="${PGG2_PRICED_SNAP_MAX_TOP1500:-0.37}"
export PGG2_PRICED_SNAP_MAX_AGE_SEC="${PGG2_PRICED_SNAP_MAX_AGE_SEC:-12.0}"
export PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION="${PGG2_PRICED_SNAP_STANDARD_ENTRY_FRACTION:-0.30}"
export PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION="${PGG2_PRICED_SNAP_ELITE_ENTRY_FRACTION:-0.80}"
export PGG2_PRICED_SNAP_ELITE_MIN_BUY1500="${PGG2_PRICED_SNAP_ELITE_MIN_BUY1500:-12.0}"
export PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500="${PGG2_PRICED_SNAP_ELITE_MIN_UNIQ1500:-10}"
export PGG2_PRICED_SNAP_ELITE_MAX_TOP1500="${PGG2_PRICED_SNAP_ELITE_MAX_TOP1500:-0.30}"
export PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC="${PGG2_PRICED_SNAP_ELITE_MAX_AGE_SEC:-12.0}"
export PGG2_PRICED_SNAP_ELITE_MAX_SLOT_TOP="${PGG2_PRICED_SNAP_ELITE_MAX_SLOT_TOP:-0.60}"

# Live execution must tolerate fast pump.fun price movement. The strategy logic
# already chose the trade; these only stop stale min-out failures during send.
export PGG2_LIVE_BUY_SLIPPAGE_PCT="${PGG2_LIVE_BUY_SLIPPAGE_PCT:-65}"
export PGG2_LIVE_SELL_SLIPPAGE_PCT="${PGG2_LIVE_SELL_SLIPPAGE_PCT:-99}"
export PGG2_DIRECT_EXIT_ANY_EXECUTABLE_PRICE=1
export PGG2_DIRECT_EXIT_MIN_LAMPORTS=1

# Keep the candidate stake profile unless explicitly overridden at launch.
export PGG2_LIVE_MAX_TRADE_SOL="${PGG2_LIVE_MAX_TRADE_SOL:-0.050}"
export PGG2_LIVE_MIN_TRADE_SOL="${PGG2_LIVE_MIN_TRADE_SOL:-0.015}"
export PGG2_LIVE_MIN_WALLET_RESERVE_SOL="${PGG2_LIVE_MIN_WALLET_RESERVE_SOL:-0.080}"
# Mirror dry-live attack behavior: do not add live-only entry halts after
# short-term drawdown. These guards stopped the run after two losses even
# though the dry candidate keeps taking its next setup.
export PGG2_LIVE_MAX_SESSION_LOSS_SOL="${PGG2_LIVE_MAX_SESSION_LOSS_SOL:-999}"
export PGG2_LIVE_MAX_CONSECUTIVE_LOSSES="${PGG2_LIVE_MAX_CONSECUTIVE_LOSSES:-999}"

# Authenticated SolanaTracker RPC is loaded from .env by Python if no explicit
# PGG2_LIVE_RPC_URL is provided. HTTP retries cover transient 429/5xx responses.
export PGG2_LIVE_HTTP_RETRIES="${PGG2_LIVE_HTTP_RETRIES:-2}"
export PGG2_LIVE_HTTP_RETRY_BASE_SEC="${PGG2_LIVE_HTTP_RETRY_BASE_SEC:-0.25}"

exec ./start_pgg2_attack_paper.sh
