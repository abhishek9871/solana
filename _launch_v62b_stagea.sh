#!/bin/bash
# V62B Stage A launcher — V60 firewall + V61 oracle + V62B authoritative sell router.
# Owns the entire pump_bc sell loop. Blocks V48-LIVE-SELL-SEND for pump_bc.
# Blocks Jupiter fallback for pump_bc. Bank/scratch/max_hold/emergency all V62B.

export PGG2_LIVE_RPC_URL="https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
export HELIUS_RPC_URL="https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
export PGG2_ENABLE_LIVE="1"
export PGG2_V60_OBSERVE_MODE="0"
export PGG2_V61_ENABLED="1"
export PGG2_V59_TRUE_EDGE_ENABLED="1"
export PGG2_V60_REQUIRE_RISK_PASS="0"
export PGG2_LIVE_CONFIRM="I_ACCEPT_REAL_SOL_RISK"
export PGG2_DIRECT_LIVE_CONFIRM="I_ACCEPT_DIRECT_PUMP_RISK"

# V62B authoritative sell router (owns pump_bc bank/scratch/max_hold/emergency)
export PGG2_V62B_ENABLED="1"
export PGG2_V62B_BANK_EXPECTED_FRACTION="0.85"
export PGG2_V62B_BANK_SMALL_PROFIT_FLOOR_SOL="0.000200"
export PGG2_V62B_FEES_ESTIMATE_SOL="0.000060"
export PGG2_V62B_SCRATCH_TOLERANCE_SOL="0.000050"
export PGG2_V62B_EMERGENCY_MIN_SOL="0.000020"
export PGG2_V62B_MAX_ATTEMPTS="3"
export PGG2_V62B_POLL_INTERVAL_MS="100"
export PGG2_V62B_PER_ATTEMPT_TIMEOUT_MS="300"
export PGG2_V62B_TOTAL_BUDGET_MS="1500"
export PGG2_V62B_FINAL_WAIT_MS="700"

# V63 mandatory post-sell CloseAccount (safety net)
export PGG2_V63_ENABLED="1"
export PGG2_V63_POLL_INTERVAL_MS="100"
export PGG2_V63_POLL_TIMEOUT_MS="3000"
export PGG2_V63_SWQOS_TIP_LAMPORTS="5000"
export PGG2_V63_CU_PRICE_MICRO="100000"
export PGG2_V63_CU_LIMIT="50000"

# V64 Candidate Passport — no live buy without final_pass=true.
# Closes the 4rzH-class bypass where transient SHADOW_ONLY/BLOCK was
# overridden by a later snapshot refresh or lane-OR.
export PGG2_V64_ENABLED="1"
export PGG2_V64_V67_MANDATORY="1"
export PGG2_V64_PASSPORT_TTL_MS="2000"
export PGG2_V64_MAX_SNAPSHOT_AGE_MS="2500"
# Bypass envs MUST be 0 for V64 mode
export PGG2_V67_BYPASS_LEGACY_GATES="0"
# Lane-OR union bypass MUST be 0 for V64 (lane-OR allowed 4rzH bypass)
export PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS="0"
export PGG2_V67_ALLOW_RULE_UNION_BYPASS="0"
export PGG2_V57_ALLOW_RULE_UNION_BYPASS="0"
export PGG2_V58_ALLOW_RULE_UNION_BYPASS="0"
export PGG2_V61_ALLOW_RULE_UNION_BYPASS="0"

# Defense-in-depth: block Jupiter fallback at the env level too.
# V62B's function-head gate already blocks pump_bc; this prevents any
# legacy path from accidentally re-enabling Jupiter.
export PGG2_RESCUE_JUPITER_FALLBACK="0"

# V65-RUN2 (2026-05-20): tighten micro floor +0.000050 -> +0.000150 after
# 6UMF RUN1 loss. 6UMF passed V59 with true_edge ~+0.00005, then curve
# dumped 9% in 5s post-buy. +0.000150 rejects the thinnest-edge candidates
# while staying well below the over-tightened +0.000500 that killed
# frequency entirely. Compromise between V64 RUN1 (0 entries / 30 min)
# and V65 RUN1 (-$0.11 thin-edge loser / 11 min).
export PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL="0.000150"
export PGG2_V59_BANK_TRUE_EDGE_MIN_SOL="0.000400"

# Position sizing + drawdown (Stage A: one entry only)
export PGG2_LIVE_MAX_TRADE_SOL="0.005"
export PGG2_LIVE_MIN_TRADE_SOL="0.005"
export PGG2_V50B_MAX_OPEN="1"
export PGG2_V48_MAX_OPEN="1"
export PGG2_V50B_MAX_CLOSES="1"
export PGG2_V48_TARGET_CLOSED_NONNEG="1"
export PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL="0.0030"
export PGG2_V50B_MAX_SECONDS="1800"
export PGG2_V48_MAX_SECONDS="1800"
export PGG2_V50B_STAGEA_FEE_BUDGET_SOL="0.00030"

exec bash /root/piggy/start_v55_stagea.sh
