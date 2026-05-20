#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy

echo "=== STOP_OLD ==="
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
tmux kill-session -t v39b_live_smoke 2>/dev/null || true
tmux kill-session -t v39b_quote_recheck 2>/dev/null || true
pkill -f "[p]ython -u PGG2.py" 2>/dev/null || true
pkill -f "[p]ython3 -u PGG2.py" 2>/dev/null || true
sleep 1
pgrep -af "python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py" 2>/dev/null || true

RUNID=pgg2_v39b_quote_rescue_live_mirror_$(date +%Y%m%d_%H%M%S)
LOG=/root/piggy/logs/${RUNID}.log
echo "$RUNID" > current_pgg2_live_mirror_runid.txt
echo "RUNID=$RUNID"
echo "LOG=$LOG"

tmux new -ds v39b_live_mirror "cd /root/piggy && timeout 2100 ./start_pgg2_v39b_quote_rescue_live_mirror.sh 2>&1 | tee -a logs/${RUNID}.log"
sleep 2
tmux ls | grep v39b_live_mirror || true

echo "=== MONITOR_START ==="
START=$(date +%s)
LAST_STATUS=0
while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW-START))
  if [ -f "$LOG" ]; then
    BUY_SENT=$(grep -c "PGG2-V39-LIVE-BUY-SENT" "$LOG" || true)
    BUY_CONF=$(grep -c "PGG2-V39-LIVE-BUY-CONFIRMED" "$LOG" || true)
    SELL_CONF=$(grep -c "PGG2-V39-LIVE-SELL-CONFIRMED" "$LOG" || true)
    MIRROR_END=$(grep -c "PGG2-V39-LIVE-MIRROR-END" "$LOG" || true)
    NEG=$(grep -c "actual_all_in_pnl=-" "$LOG" || true)
    CLOSE_FAIL=$(grep -c "PGG2-RISK-CLOSE-FAIL\|Traceback\|PGG2-POSITION-TOKEN-MISMATCH-FATAL" "$LOG" || true)
    PRESEND_BLOCK=$(grep -c "presend_.*below_floor\|presend_confirm_decay_too_high" "$LOG" || true)
    CONFIRM_GATE=$(grep -c "PGG2-V39-LIVE-PRESEND-CONFIRM-GATE" "$LOG" || true)
    if [ $((ELAPSED-LAST_STATUS)) -ge 30 ]; then
      echo "STATUS elapsed=${ELAPSED}s buy_sent=$BUY_SENT buy_confirmed=$BUY_CONF sell_confirmed=$SELL_CONF mirror_end=$MIRROR_END neg_lines=$NEG hard=$CLOSE_FAIL presend_blocks=$PRESEND_BLOCK confirm_gates=$CONFIRM_GATE"
      tail -n 8 "$LOG" | sed "s/^/TAIL /"
      LAST_STATUS=$ELAPSED
    fi
    if [ "$MIRROR_END" -ge 10 ]; then
      echo "STOP_REASON=target_10_closes"
      break
    fi
    if [ "$NEG" -gt 0 ]; then
      echo "STOP_REASON=negative_close_detected"
      break
    fi
    if [ "$CLOSE_FAIL" -gt 0 ]; then
      echo "STOP_REASON=hard_failure_detected"
      break
    fi
  fi
  if ! tmux has-session -t v39b_live_mirror 2>/dev/null; then
    echo "STOP_REASON=tmux_exited"
    break
  fi
  if [ "$ELAPSED" -ge 2100 ]; then
    echo "STOP_REASON=timeout_35m"
    break
  fi
  sleep 5
done

echo "=== STOP ==="
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
pkill -f "[p]ython -u PGG2.py" 2>/dev/null || true
pkill -f "[p]ython3 -u PGG2.py" 2>/dev/null || true
sleep 2

echo "=== SUMMARY ==="
echo "LOG=$LOG"
if [ -f "$LOG" ]; then
  grep -E "PGG2-V39-LIVE-BUY|PGG2-V39-LIVE-PRESEND|PGG2-V39-LIVE-SELL|PGG2-V39-LIVE-WALLET-DELTA|PGG2-V39-LIVE-PNL-RECONCILE|PGG2-V39-LIVE-MIRROR-END|PGG2-V39-STOP|PGG2-RISK-CLOSE-FAIL|TOKEN-MISMATCH|Traceback" "$LOG" | tail -n 160 || true
  echo "COUNTS buy_sent=$(grep -c "PGG2-V39-LIVE-BUY-SENT" "$LOG" || true) buy_confirmed=$(grep -c "PGG2-V39-LIVE-BUY-CONFIRMED" "$LOG" || true) sell_confirmed=$(grep -c "PGG2-V39-LIVE-SELL-CONFIRMED" "$LOG" || true) mirror_end=$(grep -c "PGG2-V39-LIVE-MIRROR-END" "$LOG" || true) neg_lines=$(grep -c "actual_all_in_pnl=-" "$LOG" || true) close_fail=$(grep -c "PGG2-RISK-CLOSE-FAIL" "$LOG" || true) traceback=$(grep -c "Traceback" "$LOG" || true) mismatch=$(grep -c "TOKEN-MISMATCH" "$LOG" || true) confirm_gate=$(grep -c "PGG2-V39-LIVE-PRESEND-CONFIRM-GATE" "$LOG" || true)"
fi
pgrep -af "python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py" 2>/dev/null || echo NO_BOT_PROCS
tmux ls 2>/dev/null || echo NO_TMUX
