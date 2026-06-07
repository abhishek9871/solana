#!/usr/bin/env bash
# V287 SINGLE-LANE launcher.
#
# Runs the EXACT frozen one-entry config, but with V287_SINGLE_LANE_ONLY=1 so the
# send-site firewall in pgg2_v287_selected_band_live_smoke.py permits ONLY the
# empirically winning lane: selected_single_prior_strong_rearm on the
# single_prior_buy_continuation shape. Every other authority is shadow-blocked.
#
# Evidence (tools/v287_authority_ledger.py over _v287_all_logs, 2026-06-07):
#   selected_single_prior_strong_rearm = +0.027248 SOL, 2W/1L (522f +0.0151, 2NFj +0.0132)
#   all 26 other authorities net-negative; machine-wide realized -0.0374 SOL / 49 sends.
#
# Usage on Hetzner:
#   cd /root/piggy && git pull --ff-only origin main
#   /root/piggy/venv/bin/python -m py_compile pgg2_v287_selected_band_live_smoke.py
#   bash -n _launch_v287_single_lane.sh
#   V287_SMOKE_SECONDS=600 ./_launch_v287_single_lane.sh
set -euo pipefail
cd /root/piggy

# --- Hard single-lane firewall ON ---
export V287_SINGLE_LANE_ONLY=1

# --- Pin the winning lane + its allowlist gate ---
export V287_ENABLE_SINGLE_PRIOR_BUY_LANE=1
export V287_SEED_PRIOR_ONLY_ALLOW_SINGLE_PRIOR_STRONG=1
export V287_LIVE_ONLY_SEED_PRIOR_CARRY=1

# --- Disable every other top-level entry lane (defense in depth; firewall blocks them anyway) ---
export V287_ENABLE_FRESH_IMPULSE_LANE=0
export V287_ENABLE_TWO_PRIOR_BUY_LANE=0
export V287_ENABLE_DUST_PRIOR_CONTINUATION_LANE=0
export V287_ENABLE_LOW_TOP_LANE=0
export V287_EDGE_TOP_ENABLED=0
export V287_ENABLE_SEED_PRIOR_CARRY_LANE=0

# Distinct run id so single-lane logs are easy to find.
export RUNID="${RUNID:-v287_single_lane_$(date +%Y%m%d_%H%M%S)}"

echo "V287-SINGLE-LANE-LAUNCH armed=1 allowed_reason=selected_single_prior_strong_rearm runid=${RUNID}"

# Delegate to the frozen one-entry launcher for all proven config exports + the
# dup-process guard + the python exec. Our exports above survive its ${VAR:-default}
# pattern, and V287_SINGLE_LANE_ONLY passes straight through to the runner.
exec ./_launch_v287_oneentry_smoke.sh
