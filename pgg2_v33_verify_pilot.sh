#!/usr/bin/env bash
# v33 — one-entry mechanical verification pilot.
# Strict thresholds matching the pre-registered v33_quote_edge_150_C rule.
# Real-live OFF; master ON for one pilot only. 45-minute soft cap is enforced
# by the caller (this script just starts the bot).
set -u
cd /root/piggy

export PGG2_ACTUAL_ENTRY_MASTER_ENABLED=1
export PGG2_DRYLIVE_PILOT_ENABLED=1
export PGG2_DRYLIVE_PILOT_MAX_ENTRIES=${PGG2_DRYLIVE_PILOT_MAX_ENTRIES:-3}
export PGG2_DRYLIVE_PILOT_SOL=0.015
export PGG2_DRYLIVE_PILOT_MIN_IMMEDIATE_PNL_SOL=-0.005
export PGG2_DRYLIVE_PILOT_TIMEBOX_MS=5000
export PGG2_DRYLIVE_PILOT_ABSOLUTE_MAX_HOLD_MS=10000
export PGG2_DRYLIVE_PILOT_SESSION_LOSS_CAP_SOL=0.0015
export PGG2_DRYLIVE_PILOT_MAX_BUY_IMPACT=0.005
export PGG2_DRYLIVE_PILOT_MARK_INTERVAL_MS=250
export PGG2_MAX_ENTRY_QUOTE_AGE_MS=1500
# v33 pre-registered rule v33_quote_edge_150_C: minimum all_in PnL must be
# at or above +0.00150 SOL at the entry quote. The latency-adjusted floor
# adds a same-size guard. Without these, the pre-entry econ block defaults
# to 0.0 and any near-flat candidate would pass.
export PGG2_PREENTRY_MIN_ALL_IN_PNL_SOL=0.00150
export PGG2_PREENTRY_LATENCY_ADJUSTED_FLOOR_SOL=0.00150
# Latency feasibility gate is unrelated to the holdout gate; matching the
# holdout-measured p90s with a small headroom keeps verification consistent.
export PGG2_LATENCY_MAX_P95_MS=1100.0
# v35 — high-frequency scalp entry path (parallel to primary pilot).
# Lower edge +0.00060, tighter exit policy (bank +0.00020, clamp -0.00030,
# timebox 3000ms, abs_max 3000ms). Share mints_seen with pilot so no
# duplicate-mint entry across rules.
export PGG2_SCALP_ENABLED="${PGG2_SCALP_ENABLED:-1}"
export PGG2_SCALP_MAX_ENTRIES="${PGG2_SCALP_MAX_ENTRIES:-10}"
export PGG2_SCALP_SOL=0.015
export PGG2_SCALP_MIN_ALL_IN_PNL_SOL=0.00060
export PGG2_SCALP_MAX_QUOTE_AGE_MS=750
export PGG2_SCALP_BANK_MIN_PNL_SOL=0.00020
export PGG2_SCALP_CLAMP_MAX_LOSS_SOL=0.00030
export PGG2_SCALP_TIMEBOX_MS=3000
export PGG2_SCALP_ABS_MAX_HOLD_MS=3000
export PGG2_SCALP_SESSION_LOSS_CAP_SOL=0.0015
# v36 — SLA hard config + concurrency. Oracle showed 107/187 windows hit
# >=10 zero-neg entries when max_open=5 with all lanes admitted.
export PGG2_SLA_TARGET_ENTRIES_20M=10
export PGG2_SLA_ZERO_NEGATIVE_REQUIRED=1
export PIGGY_MAX_OPEN_POSITIONS=5
export PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=0
export PGG2_SHADOW_DELAYED_ENTRY_SCANNER=1
export PGG2_SHADOW_OBSERVE_MIN_EVENT_SOL=1.0
export PGG2_SHADOW_OBSERVE_MIN_SOL_PRICED_SNAP=0.5
export PGG2_SHADOW_OBSERVE_MIN_SOL_CURVE_LAG=0.5

exec ./start_pgg2_v30_shadowlab_drylive.sh
