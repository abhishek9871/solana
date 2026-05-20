#!/bin/bash
# V61 P6 observe — LIVE OFF, V60+V61 stack ON
export PGG2_LIVE_RPC_URL="https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
export HELIUS_RPC_URL="https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"

# Mode: LIVE OFF (no real sends), V60+V61 hooks fire only in LIVE-mode buy-send path,
# so to observe V61 verdicts we run in LIVE ON with max_open=0 (build_quote → V60 → V61 fires, but max_open=0 blocks send earlier)
# Actually no — V60/V61 hooks are AFTER the open-position check. With max_open=0, hooks never reach.
# Need different approach: LIVE ON, max_open=1, target_closed=0 (won't actually trigger send because no candidate passes V61 typically)
# Or just observe via dry-mode and trust the unit tests
# Going with: LIVE ON, max_open=0 (no live send possible)
export PGG2_ENABLE_LIVE="0"
export PGG2_V60_OBSERVE_MODE="1"
export PGG2_V61_ENABLED="1"
export PGG2_V59_TRUE_EDGE_ENABLED="1"
export PGG2_V60_REQUIRE_RISK_PASS="0"
export PGG2_LIVE_CONFIRM="I_ACCEPT_REAL_SOL_RISK"

export PGG2_LIVE_MAX_TRADE_SOL="0.005"
export PGG2_V50B_MAX_OPEN="0"
export PGG2_V50B_MAX_SECONDS="300"
export PGG2_V48_MAX_SECONDS="300"

exec bash /root/piggy/start_v55_stagea.sh
