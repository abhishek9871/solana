#!/bin/bash
# Step 4 — Start Live Mirror (verbatim user sequence; tail line omitted, monitor handles streaming)
cd /root/piggy || exit 2

RUNID=pgg2_v39b_quote_rescue_live_mirror_$(date +%Y%m%d_%H%M%S)
echo "$RUNID" > current_pgg2_live_mirror_runid.txt
LOG="logs/${RUNID}.log"

tmux new -ds v39b_live_mirror "cd /root/piggy && timeout 2100 ./start_pgg2_v39b_quote_rescue_live_mirror.sh 2>&1 | tee -a ${LOG}"

echo "RUNID=$RUNID"
echo "LOG=/root/piggy/$LOG"
