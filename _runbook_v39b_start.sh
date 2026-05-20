#!/bin/bash
# Step 3 — Start One-Entry Live Smoke (verbatim from LIVE_SMOKE_V39B_TEAM_RUNBOOK.md)
cd /root/piggy || exit 2
RUNID=pgg2_v39b_quote_rescue_live_smoke_$(date +%Y%m%d_%H%M%S)
echo "$RUNID" > current_pgg2_live_smoke_runid.txt
LOG="logs/${RUNID}.log"

tmux new -ds v39b_live_smoke "cd /root/piggy && timeout 2100 ./start_pgg2_v39b_quote_rescue_live_smoke.sh 2>&1 | tee -a ${LOG}"
echo "RUNID=$RUNID"
echo "LOG=/root/piggy/$LOG"
