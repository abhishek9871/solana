#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy

tmux kill-session -t v61_live_stagea 2>/dev/null || true
sleep 1

RUNID="v61_rebound_live_stagea_$(date +%Y%m%d_%H%M%S)"
echo "$RUNID" > current_v61_live_runid.txt
LOG="logs/${RUNID}.log"

tmux new -ds v61_live_stagea "cd /root/piggy && timeout 900 ./_run_v61_live_once.sh 2>&1 | tee -a ${LOG}"
sleep 4

echo "RUNID=${RUNID}"
echo "LOG=/root/piggy/${LOG}"
tmux ls 2>/dev/null || true
tail -n 50 "${LOG}" 2>/dev/null || true
