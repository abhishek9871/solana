#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# v30 — executable shadow lab dry-live.
# Same broker route shape as live (direct_pump), but quote mode keeps send=0
# so no real SOL can move. Frozen lanes (rug_bounce, curve_lag_reveal) are
# diagnostics-only via the shadow lab. priced_snap is the only actual-entry
# lane, capped at 0.020 SOL, with shadow taps for parallel comparison.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_v30_shadowlab_drylive}"
export PGG2_EXECUTION_MODE=quote
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=1
export PGG2_LIVE_BROKER=direct_pump
export PGG2_WALLET_KEYPAIR="${PGG2_WALLET_KEYPAIR:-/root/piggy/live_wallet.key}"

# Quote-shadow: broker builds quotes but does not send.
export PGG2_QUOTE_SHADOW_POSITIONS=1
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
export PGG2_LIVE_SKIP_PREFLIGHT=1
export PGG2_LIVE_PREFLIGHT_ROUNDTRIP_ENABLED=0
export PGG2_LIVE_FINAL_ENTRY_GUARD_ENABLED=0
export PGG2_DIRECT_SELECT_BUYBACK_BY_SIM=0
export PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK=0
export PGG2_DIRECT_OBSERVED_PAIR_FROM_RAW=1
# v30 quote-coverage repair — dry-live mode lets the direct builder fall
# back to default pair + sim-select rather than raising on every fresh mint.
# Strict live safety is preserved because the live launcher (start_pgg2_direct_live_candidate.sh)
# does NOT inherit this and sets =1 explicitly.
export PGG2_DIRECT_REQUIRE_OBSERVED_BUYBACK_PAIR=0
export PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK=0
export PGG2_DIRECT_SELECT_BUYBACK_BY_SIM=1
export PGG2_DIRECT_SIM_SELECT_MAX_CANDIDATES=3
export PGG2_QUOTE_SIMULATE=1
# v30 — fallback economic quote via Raptor (Solana Tracker swap) so the lab
# can record whether *some* venue could quote even if direct could not.
export PGG2_SHADOW_LAB_RAPTOR_FALLBACK=1

# v30 — curve_missing retry ladder. Fresh mints without a bonding curve
# account get re-quoted at +100/250/500/1000/2000ms.
export PGG2_SHADOW_LAB_CURVE_MISSING_RETRY=1
export PGG2_SHADOW_LAB_CURVE_RETRY_MS="0,100,250,500,1000,2000"

# v30 — Pump v2 probe. Quote/sim only. The probe currently records that v2
# IDL is unavailable; flip to 1 to surface that explicitly in lab records.
export PGG2_DIRECT_PUMP_V2_PROBE="${PGG2_DIRECT_PUMP_V2_PROBE:-1}"

# v30 — GLOBAL ACTUAL-ENTRY MASTER KILL SWITCH. Default OFF. When off,
# every queue_or_fill / canary / pilot path refuses to open. Shadow lab
# still runs.
export PGG2_ACTUAL_ENTRY_MASTER_ENABLED="${PGG2_ACTUAL_ENTRY_MASTER_ENABLED:-0}"
# v30 — quote-locked entry max age (ms). Quotes older than this are rejected.
export PGG2_MAX_ENTRY_QUOTE_AGE_MS="${PGG2_MAX_ENTRY_QUOTE_AGE_MS:-150}"
# v30 — risk supervisor (asyncio wall-clock task per open position)
export PGG2_RISK_SUPERVISOR_INTERVAL_MS="${PGG2_RISK_SUPERVISOR_INTERVAL_MS:-100}"
export PGG2_RISK_SUPERVISOR_FIRST_TICK_MS="${PGG2_RISK_SUPERVISOR_FIRST_TICK_MS:-50}"
export PGG2_RISK_SUPERVISOR_MAX_LIFETIME_MS="${PGG2_RISK_SUPERVISOR_MAX_LIFETIME_MS:-11000}"

# v33 — DRY-LIVE PILOT for rule `v33_quote_edge_150_C`.
# OFF by default. Opt in via PGG2_DRYLIVE_PILOT_ENABLED=1.
# Hard-refuses real-live mode in code; ALSO requires master switch.
export PGG2_DRYLIVE_PILOT_ENABLED="${PGG2_DRYLIVE_PILOT_ENABLED:-0}"
export PGG2_DRYLIVE_PILOT_MAX_ENTRIES="${PGG2_DRYLIVE_PILOT_MAX_ENTRIES:-3}"
export PGG2_DRYLIVE_PILOT_SOL="${PGG2_DRYLIVE_PILOT_SOL:-0.015}"
export PGG2_DRYLIVE_PILOT_MIN_IMMEDIATE_PNL_SOL="${PGG2_DRYLIVE_PILOT_MIN_IMMEDIATE_PNL_SOL:--0.00150}"
export PGG2_DRYLIVE_PILOT_MAX_BUY_IMPACT="${PGG2_DRYLIVE_PILOT_MAX_BUY_IMPACT:-0.005}"
export PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL="${PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL:-0.006}"
export PGG2_DRYLIVE_PILOT_TIMEBOX_MS="${PGG2_DRYLIVE_PILOT_TIMEBOX_MS:-5000}"
export PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS="${PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS:-10000}"
export PGG2_DRYLIVE_PILOT_MARK_INTERVAL_MS="${PGG2_DRYLIVE_PILOT_MARK_INTERVAL_MS:-250}"
# Exit policy thresholds (broker-side env consumed by clamp/bank for pilot lane)
export PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_RATIO="${PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_RATIO:-0.10}"
export PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_FLOOR_SOL="${PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_FLOOR_SOL:-0.00150}"
export PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_CAP_SOL="${PGG2_LIVE_DRYLIVE_PILOT_MAX_EXECUTABLE_LOSS_CAP_SOL:-0.00150}"
export PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_PNL_SOL="${PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_PNL_SOL:-0.00060}"
export PGG2_LIVE_DRYLIVE_PILOT_ANY_PROFIT_BANK_MIN_PNL_SOL="${PGG2_LIVE_DRYLIVE_PILOT_ANY_PROFIT_BANK_MIN_PNL_SOL:-0.00060}"
export PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_AGE_SEC="${PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_AGE_SEC:-0.10}"
export PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_MULT="${PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_MULT:-1.00}"
export PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_PEAK="${PGG2_LIVE_DRYLIVE_PILOT_PROFIT_BANK_MIN_PEAK:-1.00}"
export PGG2_DIRECT_ACCOUNT_COMMITMENT=processed
export PGG2_DIRECT_BLOCKHASH_COMMITMENT=processed
export PGG2_DIRECT_CURVE_ACCOUNT_TTL_SEC=0

# P1 — FREEZE bad lanes (diagnostics-only via shadow lab)
export PGG2_RUG_BOUNCE_ACTUAL_ENTRY_ENABLED=0
export PGG2_CURVE_LAG_REVEAL_ACTUAL_ENTRY_ENABLED=0

# P2 — shadow lab on, tap priced_snap so all three lane families get records
export PGG2_SHADOW_LAB_ENABLED=1
export PGG2_SHADOW_LAB_TAP_PRICED_SNAP=1
export PGG2_SHADOW_LAB_COOLDOWN_MS=30000
export PGG2_SHADOW_LAB_MAX_CONCURRENT=4
# v30 — dense timeline (per clamp-breach audit). Early-window points catch
# fast pnl reversals that the old 250ms-first sparse timeline missed.
export PGG2_SHADOW_LAB_SELL_DELAYS_MS=100,200,300,500,750,1000,1500,2000,3000,5000,7500,10000
export PGG2_SHADOW_LAB_PATH=/root/piggy/data/pgg2_executable_shadow_lab.jsonl

# v30 — permissive observation tap. Runs on every buy with event_sol>=threshold
# and age within window, regardless of whether any lane fires. Produces the
# executable-quote roundtrip rows the report needs.
export PGG2_SHADOW_OBSERVE_ENABLED=1
export PGG2_SHADOW_OBSERVE_MIN_EVENT_SOL=2.0
export PGG2_SHADOW_OBSERVE_MAX_AGE_MS=30000
export PGG2_SHADOW_OBSERVE_SCOUT_SOL=0.015

# v30 — canary path. Opens exactly one dry-live/quote-shadow position so
# manage_position / quote_profit_bank / quote_loss_clamp are exercised on a
# real position. Hard-guarded against real-live mode in code. Disabled by
# default for coverage runs; opt-in by setting PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=1.
export PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY="${PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY:-0}"
export PGG2_SHADOW_LAB_CANARY_MAX_ENTRIES="${PGG2_SHADOW_LAB_CANARY_MAX_ENTRIES:-1}"
export PGG2_SHADOW_LAB_CANARY_MAX_IMMEDIATE_LOSS_SOL="${PGG2_SHADOW_LAB_CANARY_MAX_IMMEDIATE_LOSS_SOL:-0.005}"

# Strategy config — OG priced_snap profile (Phase 2b/c rules retained).
# Live-derived loss cutter and exit-side grace clauses preserved verbatim.
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
export PGG2_PRICED_SNAP_TOXIC_TOP_LOSSCUT_ENABLED="${PGG2_PRICED_SNAP_TOXIC_TOP_LOSSCUT_ENABLED:-1}"
export PGG2_PRICED_SNAP_TOXIC_TOP_MAX_TOP1500="${PGG2_PRICED_SNAP_TOXIC_TOP_MAX_TOP1500:-0.365}"
export PGG2_LIVE_LOSS_CUTTER_ENABLED="${PGG2_LIVE_LOSS_CUTTER_ENABLED:-1}"
export PGG2_LIVE_LOSSCUT_MIN_UNIQ1500="${PGG2_LIVE_LOSSCUT_MIN_UNIQ1500:-6}"
export PGG2_LIVE_LOSSCUT_LOW_BUY1500="${PGG2_LIVE_LOSSCUT_LOW_BUY1500:-10.5}"
export PGG2_LIVE_LOSSCUT_LOW_SCORE="${PGG2_LIVE_LOSSCUT_LOW_SCORE:-150}"

# P1 — actual-entry stake bounds. Hard ceiling 0.020 SOL until shadow data
# proves a larger stake is positive EV.
export PGG2_LIVE_MAX_TRADE_SOL="${PGG2_LIVE_MAX_TRADE_SOL:-0.020}"
export PGG2_LIVE_MIN_TRADE_SOL="${PGG2_LIVE_MIN_TRADE_SOL:-0.015}"
export PGG2_LIVE_MIN_WALLET_RESERVE_SOL="${PGG2_LIVE_MIN_WALLET_RESERVE_SOL:-0.080}"
# Adaptive sizing locks to the same ceiling so we cannot scale up.
export PGG2_LIVE_BUY_ADAPTIVE_ENABLED="${PGG2_LIVE_BUY_ADAPTIVE_ENABLED:-0}"

# Quote loss clamp + any-profit-bank stay enabled with low thresholds.
export PGG2_LIVE_QUOTE_LOSS_CLAMP_ENABLED=1
export PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_ENABLED=1
export PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_MIN_PNL_SOL="${PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_MIN_PNL_SOL:-0.00125}"
# Disable hold-after-peak until shadow data shows it helps.
export PGG2_LIVE_QUOTE_PROFIT_BANK_HOLD_AFTER_PEAK_MS="${PGG2_LIVE_QUOTE_PROFIT_BANK_HOLD_AFTER_PEAK_MS:-0}"

# Same executable tolerances as the direct live candidate.
export PGG2_LIVE_BUY_SLIPPAGE_PCT="${PGG2_LIVE_BUY_SLIPPAGE_PCT:-65}"
export PGG2_LIVE_SELL_SLIPPAGE_PCT="${PGG2_LIVE_SELL_SLIPPAGE_PCT:-99}"
export PGG2_DIRECT_EXIT_ANY_EXECUTABLE_PRICE=1
export PGG2_DIRECT_EXIT_MIN_LAMPORTS=1

# Session loss / consecutive losses: small for first validation. The shadow
# lab is the real instrument; actual entries are the audit, not the goal.
export PGG2_LIVE_MAX_SESSION_LOSS_SOL="${PGG2_LIVE_MAX_SESSION_LOSS_SOL:-0.060}"
export PGG2_LIVE_MAX_CONSECUTIVE_LOSSES="${PGG2_LIVE_MAX_CONSECUTIVE_LOSSES:-3}"

export PGG2_LIVE_HTTP_RETRIES="${PGG2_LIVE_HTTP_RETRIES:-2}"
export PGG2_LIVE_HTTP_RETRY_BASE_SEC="${PGG2_LIVE_HTTP_RETRY_BASE_SEC:-0.25}"

exec ./start_pgg2_attack_paper.sh
