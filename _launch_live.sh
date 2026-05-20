#!/bin/bash
set +e
cd /root/piggy
tmux kill-session -t experimentaljilive 2>/dev/null
pkill -9 -f experimentalji 2>/dev/null
sleep 2
tmux new-session -d -s experimentaljilive "cd /root/piggy && ./start_experimentalji_direct_live.sh"
sleep 8
echo "=== tmux ==="
tmux ls 2>&1
echo "=== process ==="
pgrep -af experimentalji 2>&1 | head -3
echo "=== run id ==="
cat /root/piggy/current_pgg2_runid.txt 2>&1
echo "=== first 40 log lines ==="
RUNID=$(cat /root/piggy/current_pgg2_runid.txt 2>/dev/null)
if [ -n "$RUNID" ] && [ -f /root/piggy/logs/${RUNID}.log ]; then
  tail -40 /root/piggy/logs/${RUNID}.log
fi
echo "=== DONE ==="
