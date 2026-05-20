#!/bin/bash
set -euo pipefail
cd /root/piggy
tmux kill-session -t v57_rearmed_stage1 2>/dev/null || true
pkill -f '[p]ython.*pgg2_v50b_stagea_live.py' 2>/dev/null || true
RUNID=v57_rearmed_stage1_live_$(date +%Y%m%d_%H%M%S)
echo "$RUNID" > current_v57_rearmed_stage1_runid.txt
tmux new -ds v57_rearmed_stage1 "cd /root/piggy && RUNID=$RUNID PGG2_V50B_MAX_CLOSES=1 PGG2_V50B_MAX_OPEN=1 PGG2_V50B_MAX_SECONDS=900 timeout 930 ./start_v55_stagea.sh 2>&1 | tee -a logs/${RUNID}.log"
sleep 2
echo RUNID=$RUNID
echo LOG=/root/piggy/logs/${RUNID}.log
tmux ls 2>/dev/null || true
tail -n 80 logs/${RUNID}.log 2>/dev/null || true