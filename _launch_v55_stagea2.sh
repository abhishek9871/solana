#!/usr/bin/env bash
# Standalone launcher to avoid polluting the bash cmdline with the idempotency-target string.
set -euo pipefail
export PGG2_V50B_MAX_CLOSES=3
export PGG2_V50B_MAX_SECONDS=1800
export PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL=0.0100
export RUNID="pgg2_v55_stagea2_$(date +%Y%m%d_%H%M%S)"
exec bash /root/piggy/start_v55_stagea.sh
