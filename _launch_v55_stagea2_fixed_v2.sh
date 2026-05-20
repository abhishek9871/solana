#!/usr/bin/env bash
# V55 Stage A2-fixed-v2: target=3 with both fixes
#  Fix #1: PGG2_V48_LIVE_SELL_SEND_MIN_PNL_SOL=0.0 (restored sanity check)
#  Fix #2: V48 code now blocks brand-new mints with <2 curve ticks
set -euo pipefail
export PGG2_V50B_MAX_CLOSES=3
export PGG2_V50B_MAX_SECONDS=1800
export PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL=0.0100
export RUNID="pgg2_v55_stagea2_fixed_v2_$(date +%Y%m%d_%H%M%S)"
exec bash /root/piggy/start_v55_stagea.sh
