#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# v30 — CANARY MECHANICAL TEST.
# Quote-locked open + wall-clock risk supervisor. NO strategy rule.
# Max 1 canary entry; refuses real-live; uses the same quote object for
# decision and execution.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-pgg2_v30_canary_mech}"

# Enable master switch + canary only. Pilot OFF.
export PGG2_ACTUAL_ENTRY_MASTER_ENABLED=1
export PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=1
export PGG2_SHADOW_LAB_CANARY_MAX_ENTRIES=1
export PGG2_SHADOW_LAB_CANARY_MAX_IMMEDIATE_LOSS_SOL=0.005
export PGG2_DRYLIVE_PILOT_ENABLED=0

# Quote freshness + risk worker cadence.
# NOTE: 150ms is the spec default. In observed runs the direct-pump broker's
# build_swap + reverse build_swap round-trip is ~300-1600ms (RPC bound), so
# 150ms would reject every candidate. For mechanical validation we widen to
# 1500ms so the locked-quote path actually opens. This is NOT for strategy
# promotion — only to exercise token-equality + risk worker.
export PGG2_MAX_ENTRY_QUOTE_AGE_MS=1500

# v31 — threaded risk worker (replaces async supervisor)
export PGG2_RISK_WORKER_ENABLED=1
export PGG2_RISK_WORKER_POLL_SEC=0.020
export PGG2_RISK_QUOTE_STALE_MS=2000
export PGG2_RISK_ALLOW_OVERLAP_QUOTES=0

# v31 — latency feasibility gate. Lower bar for canary mechanical to allow
# at least one open while we measure the latency distribution. Strategy
# pilots should use a tighter ceiling derived from the report.
export PGG2_LATENCY_FEASIBILITY_ENABLED=1
export PGG2_LATENCY_MAX_P95_MS=2500
export PGG2_QUOTE_LATENCY_RING_SIZE=500

# v32 — QuoteManager (centralized quote service)
export PGG2_QUOTE_MGR_CACHE_TTL_MS=300
export PGG2_QUOTE_MGR_REFRESH_AFTER_MS=200

# v32 — risk worker is the SINGLE OWNER of quote-based exits for canary/pilot
export PGG2_RISK_WORKER_OWNS_QUOTE_EXIT=1

# v32 — fast quote: actual entries require cached/prewarmed pair (no sim-select)
export PGG2_ACTUAL_ENTRY_REQUIRE_FAST_QUOTE=1
export PGG2_DIRECT_SKIP_SIM_IF_CACHED=1

# v32 — route-aware economic block. canary may only open when immediate
# all-in pnl (via quote_all_in_pnl) is >= configured min. Setting to 0
# requires AT LEAST scratch; positive values require profitable entry.
export PGG2_CANARY_MIN_ALL_IN_PNL_SOL=0.0
export PGG2_PREENTRY_MIN_ALL_IN_PNL_SOL=0.0
export PGG2_PREENTRY_LATENCY_ADJUSTED_FLOOR_SOL=0.0

exec ./start_pgg2_v30_shadowlab_drylive.sh
