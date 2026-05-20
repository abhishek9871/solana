#!/bin/bash
# PR2 Stage A watcher — emits new recovery markers, exits on 1× PRINCIPAL-RECOVERED
# or any hard failure.
cd /root/piggy
RUNID=$(cat current_pgg2_live_mirror_runid.txt)
LOG=/root/piggy/logs/${RUNID}.log
echo "PR2_STAGE_A_WATCHER_ARMED runid=$RUNID"

end_count=0
rec_count=0

tail -n 0 -F "$LOG" | grep -E --line-buffered \
'PGG2-LIVE-MIRROR-START|PGG2-V39-ENTRY-ROUTER|PGG2-V39-LIVE-PRESEND-LANDED-GATE|PGG2-V39-BUY-RECOVERY-MIN-TOKEN-GUARD|PGG2-V39-LIVE-BUY|PGG2-V39-LIVE-SELL|PGG2-V39-LIVE-TOKEN-RECONCILE|PGG2-V39-LIVE-POSTBUY-GATE|PGG2-V39-LIVE-MIRROR-END|PGG2-V39-STOP|PGG2-V39-LIVE-BUY-GUARD-DECODE|PGG2-V39-LIVE-SELL-GUARD-DECODE|PGG2-V39-PRINCIPAL-RECOVERY-QUOTE|PGG2-V39-RECOVERY-SELL-GUARD-ENCODED|PGG2-V39-RECOVERY-SELL-SEND|PGG2-V39-RECOVERY-SELL-CONFIRMED|PGG2-V39-RECOVERY-SELL-FAILED-SAFE|PGG2-V39-PRINCIPAL-RECOVERED|PGG2-V39-RECOVERY-NOT-POSSIBLE|PGG2-V39-RESIDUAL-LEFT-FREEBAG|PGG2-POSITION-TOKEN-MISMATCH-FATAL|PGG2-RISK-CLOSE-FAIL|PGG2-LIVE-SELL-FAIL|PGG2-V39-LIVE-UNPROTECTED-SELL-FATAL|Traceback|sim_needed=1|route_not_pump_bc' \
| while IFS= read -r line; do
  echo "$line"

  case "$line" in
    *"PGG2-V39-PRINCIPAL-RECOVERED"*)
      rec_count=$((rec_count+1))
      echo "PRINCIPAL_RECOVERED_COUNT=$rec_count"
      if [ "$rec_count" -ge 1 ]; then
        echo "STAGE_A_PRINCIPAL_RECOVERED_STOPPING"
        tmux kill-session -t v39b_stage_a 2>/dev/null || true
        pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
        pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
        break
      fi
      ;;
  esac

  case "$line" in
    *"PGG2-V39-RECOVERY-NOT-POSSIBLE"*"final=true"*)
      echo "STAGE_A_RECOVERY_FAILED_STOPPING"
      tmux kill-session -t v39b_stage_a 2>/dev/null || true
      pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
      pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
      break
      ;;
  esac

  if echo "$line" | grep -Eq 'PGG2-V39-LIVE-BUY-GUARD-DECODE-FAIL|PGG2-V39-LIVE-SELL-GUARD-DECODE-FAIL|PGG2-V39-STOP reason=negative_live_equiv_close|PGG2-POSITION-TOKEN-MISMATCH-FATAL|PGG2-RISK-CLOSE-FAIL|PGG2-LIVE-SELL-FAIL|PGG2-V39-LIVE-UNPROTECTED-SELL-FATAL|Traceback|sim_needed=1|route_not_pump_bc'; then
    echo "HARD_STOP_MATCHED"
    tmux kill-session -t v39b_stage_a 2>/dev/null || true
    pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
    pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
    break
  fi
done
