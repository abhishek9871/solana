#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy
RUNID=v60_flow_watch_shadow_$(date +%Y%m%d_%H%M%S)
echo "$RUNID" > current_v60_flow_watch_runid.txt
LOG="logs/${RUNID}.log"
echo "RUNID=$RUNID LOG=/root/piggy/$LOG"
timeout 180 env \
  PGG2_V50B_MAX_SECONDS=175 \
  PGG2_V50B_MAX_CLOSES=1 \
  PGG2_V48_V56D_FLOW_LANE_ENABLED=0 \
  PGG2_V57_IMPULSE_LANE_ENABLED=0 \
  PGG2_V58_FLOW_LANE_ENABLED=0 \
  PGG2_V56B_LIVE_ACTUAL_ENTRY_ENABLED=0 \
  PGG2_V48_V56B_MIN_EXPECTED_PNL=999 \
  PGG2_V48_V56B_MAX_V47H_RATIO=0 \
  PGG2_V60_FLOW_CONFIRM_LANE_ENABLED=1 \
  PGG2_V60_ACTUAL_ENTRY_ENABLED=0 \
  ./start_v55_stagea.sh 2>&1 | tee -a "$LOG"
