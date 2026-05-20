#!/bin/bash
# RUN3 Step 5 — exact user watcher (token-drift NOT a hard-stop)
cd /root/piggy
RUNID=$(cat current_pgg2_live_mirror_runid.txt)
LOG=/root/piggy/logs/${RUNID}.log

end_count=0

tail -n 0 -F "$LOG" | while IFS= read -r line; do
  echo "$line"

  case "$line" in
    *"PGG2-V39-LIVE-MIRROR-END"*)
      end_count=$((end_count+1))
      echo "MIRROR_END_COUNT=$end_count"
      if [ "$end_count" -ge 10 ]; then
        echo "TARGET_HIT_STOPPING"
        tmux kill-session -t v39b_live_mirror 2>/dev/null || true
        pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
        pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
        break
      fi
      ;;
  esac

  if echo "$line" | grep -Eq 'actual_all_in_pnl=-|PGG2-V39-STOP reason=negative_live_equiv_close|PGG2-POSITION-TOKEN-MISMATCH-FATAL|PGG2-RISK-CLOSE-FAIL|PGG2-LIVE-SELL-FAIL|Traceback|sim_needed=1|route_not_pump_bc'; then
    echo "HARD_STOP_MATCHED"
    tmux kill-session -t v39b_live_mirror 2>/dev/null || true
    pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
    pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
    break
  fi
done
