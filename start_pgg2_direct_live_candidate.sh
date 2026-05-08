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

# =============================================================================
# Phase 2A 2026-05-08 — adaptive guards + filter pruning based on cross-run validation
# of 22 OG live runs (483 trades). Walk-forward verdict:
# - 8 filters OVERFIT (helped May 6 / hurt May 7-8): E2, N1, N2, N3, F9, F10, F11, F14
# - 10 filters GENERALIZE: E1, F6, F7, F8, F12, F13, F15v2, F16, F17, F18 (kept)
# - Hours 18:00-20:00 UTC consistently bad across all runs (-$2.06 combined)
# - Run 191401 had 12-15 consecutive-loss streaks costing $0.30-$0.81 per streak
# =============================================================================
# 1. DISABLE 8 overfit filters by setting env vars to no-op thresholds.
#    N1 was worst — sacrificed +$21 of winners including DY9TMGKg (+$7.50).
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01                    # disable E2 (top never > 1.01)
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1                       # disable N1 (hhi never < -1)
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1               # disable N2
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1              # disable N3
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999    # disable F9
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1             # disable F10
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1             # disable F11
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0                  # disable F14 (score never < 0)
# 2. ADAPTIVE: hour-of-day block (UTC). 18-20 UTC are consistently hostile across
#    22 runs (-$2.06 combined). Allow 21:00 (OG winner ran partly here).
export PGG2_BLOCK_HOURS_UTC="${PGG2_BLOCK_HOURS_UTC:-18,19,20}"
# 3. ADAPTIVE: consecutive-loss circuit breaker. Run 191401 had 5 streaks of 10+
#    consecutive losses each costing $0.30-0.81. After 5 losses in a row, pause
#    the bot for 5 minutes to break the bad-tape streak.
export PGG2_CIRCUIT_BREAKER_LOSSES="${PGG2_CIRCUIT_BREAKER_LOSSES:-5}"
export PGG2_CIRCUIT_BREAKER_PAUSE_SEC="${PGG2_CIRCUIT_BREAKER_PAUSE_SEC:-300}"

# =============================================================================
# Phase 3 2026-05-08 — strategic redesign for "destined to win big" outcomes
# Per cross-run validation (483 trades, 22 OG runs):
# - curve_lag_reveal lane was NET NEGATIVE: 30% win rate, lost more than it won
# - hard_break fired -$23.46 net on trades that had peaked at 1.428x avg
# - scout_profit_protect fired -$20.69 net (replaced with PEAK-LOCK)
# - quote_profit_bank was the +95% winner (+$37.74) — preserve at all costs
# =============================================================================
# 4. DISABLE curve_lag_reveal lane entirely. priced_snap stays as the only entry.
export PGG2_CURVE_LAG_REVEAL_ENABLED="${PGG2_CURVE_LAG_REVEAL_ENABLED:-0}"
# 5. PEAK-LOCK trailing stop (replaces scout_profit_protect). Three tiers:
#    - low:  peak >= 1.18 → floor = max(1.05, peak*0.92) → lock 5%+
#    - mid:  peak >= 1.30 → floor = max(1.15, peak*0.88) → lock 15%+
#    - high: peak >= 1.60 → floor = max(1.30, peak*0.85) → lock 30%+
export PGG2_PEAK_LOCK_ENABLED="${PGG2_PEAK_LOCK_ENABLED:-1}"
export PGG2_PEAK_LOCK_LOW_PEAK="${PGG2_PEAK_LOCK_LOW_PEAK:-1.18}"
export PGG2_PEAK_LOCK_LOW_FLOOR="${PGG2_PEAK_LOCK_LOW_FLOOR:-1.05}"
export PGG2_PEAK_LOCK_LOW_TRAIL="${PGG2_PEAK_LOCK_LOW_TRAIL:-0.92}"
export PGG2_PEAK_LOCK_MID_PEAK="${PGG2_PEAK_LOCK_MID_PEAK:-1.30}"
export PGG2_PEAK_LOCK_MID_FLOOR="${PGG2_PEAK_LOCK_MID_FLOOR:-1.15}"
export PGG2_PEAK_LOCK_MID_TRAIL="${PGG2_PEAK_LOCK_MID_TRAIL:-0.88}"
export PGG2_PEAK_LOCK_HIGH_PEAK="${PGG2_PEAK_LOCK_HIGH_PEAK:-1.60}"
export PGG2_PEAK_LOCK_HIGH_FLOOR="${PGG2_PEAK_LOCK_HIGH_FLOOR:-1.30}"
export PGG2_PEAK_LOCK_HIGH_TRAIL="${PGG2_PEAK_LOCK_HIGH_TRAIL:-0.85}"
# 6. CONDITION hard_break: only fire when peak never reached PEAK-LOCK low tier.
#    Trades that peaked above 1.18 are governed by PEAK-LOCK exclusively.
export PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW="${PGG2_HARD_BREAK_REQUIRE_PEAK_BELOW:-1.18}"
# 7. ANTI-MARTINGALE stake scaling. After 2+ consecutive losses, scale stake DOWN
#    to 50%; after 2+ consecutive wins, scale UP to 130%. Caps at min/max trade
#    SOL bounds. Disabled by default at launch — enable after live validation.
export PGG2_ANTI_MARTINGALE_ENABLED="${PGG2_ANTI_MARTINGALE_ENABLED:-1}"
export PGG2_ANTI_MARTINGALE_LOSS_STREAK="${PGG2_ANTI_MARTINGALE_LOSS_STREAK:-2}"
export PGG2_ANTI_MARTINGALE_LOSS_SCALE="${PGG2_ANTI_MARTINGALE_LOSS_SCALE:-0.50}"
export PGG2_ANTI_MARTINGALE_WIN_STREAK="${PGG2_ANTI_MARTINGALE_WIN_STREAK:-2}"
export PGG2_ANTI_MARTINGALE_WIN_SCALE="${PGG2_ANTI_MARTINGALE_WIN_SCALE:-1.30}"

exec ./start_pgg2_attack_paper.sh
