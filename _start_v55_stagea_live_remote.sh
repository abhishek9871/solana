#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy

tmux kill-session -t v55_stagea_live 2>/dev/null || true

RUNID="v56d_tick1_stagea_live_$(date +%Y%m%d_%H%M%S)"
echo "$RUNID" > current_v55_stagea_live_runid.txt

tmux new -ds v55_stagea_live "cd /root/piggy && RUNID=$RUNID ./start_v55_stagea.sh"
sleep 2

echo "RUNID=$RUNID"
echo "LOG=/root/piggy/logs/${RUNID}.log"
tmux ls 2>/dev/null || true
tail -n 60 "logs/${RUNID}.log" 2>/dev/null || true
