#!/bin/bash
export PGG2_LIVE_RPC_URL="https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
export HELIUS_RPC_URL="https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
export PGG2_ENABLE_LIVE="0"
export PGG2_V60_OBSERVE_MODE="1"
export PGG2_V59_TRUE_EDGE_ENABLED="1"
export PGG2_V60_REQUIRE_RISK_PASS="0"
export PGG2_LIVE_CONFIRM="I_ACCEPT_REAL_SOL_RISK"
export PGG2_V50B_MAX_SECONDS="300"
export PGG2_V48_MAX_SECONDS="300"
export PGG2_V50B_MAX_OPEN="0"
exec bash /root/piggy/start_v55_stagea.sh
