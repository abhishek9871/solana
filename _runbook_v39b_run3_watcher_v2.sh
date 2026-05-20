#!/bin/bash
# RUN3 watcher v2 — same kill logic as user's spec, but pre-filtered so only relevant
# lines reach the consumer (avoids Monitor rate-limit suppression).
# TOKEN-DRIFT is emitted for visibility but NOT a hard-stop (user spec).
cd /root/piggy
RUNID=$(cat current_pgg2_live_mirror_runid.txt)
LOG=/root/piggy/logs/${RUNID}.log
echo "WATCHER_V2_ARMED runid=$RUNID"

end_count=0

tail -n 0 -F "$LOG" | grep -E --line-buffered 'PGG2-LIVE-MIRROR-START|PGG2-V39-ENTRY-ROUTER|PGG2-V39-LIVE-BUY|PGG2-V39-LIVE-SELL|PGG2-V39-LIVE-TOKEN-RECONCILE|PGG2-V39-LIVE-TOKEN-DRIFT|PGG2-V39-LIVE-POSTBUY-REPRICE|PGG2-V39-LIVE-POSTBUY-GATE|PGG2-V39-LIVE-WALLET-DELTA|PGG2-V39-LIVE-PNL-RECONCILE|PGG2-V39-LIVE-MIRROR-END|PGG2-V39-STOP|PGG2-POSITION-TOKEN-MISMATCH-FATAL|PGG2-RISK-CLOSE-FAIL|PGG2-LIVE-SELL-FAIL|Traceback|sim_needed=1|route_not_pump_bc' | while IFS= read -r line; do
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
