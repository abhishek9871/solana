#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy
old=$(cat /root/piggy/current_pgg2_runid.txt 2>/dev/null || true)
echo "OLD_RUN=$old"
open=0
if [[ -n "$old" && -f "/root/piggy/data/${old}_state.json" ]]; then
  open=$(python3 -c "import json; s=json.load(open('/root/piggy/data/${old}_state.json'))['session']; print(len(s.get('open_positions') or s.get('positions') or []))")
fi
echo "OPEN_POSITIONS=$open"
if [[ "$open" != "0" ]]; then
  echo "REFUSE_RESTART_OPEN_POSITION"
  exit 2
fi
tmux kill-session -t experimentalji 2>/dev/null || true
tmux new-session -d -s experimentalji 'cd /root/piggy && ./start_experimentalji_direct_drylive.sh'
sleep 4
new=$(cat /root/piggy/current_pgg2_runid.txt)
echo "NEW_RUN=$new"
pgrep -af experimentalji.py || true
tail -10 "/root/piggy/logs/${new}.log"
