#!/bin/bash
# Auto-kills the bot if session realized P&L drops to LOSS_CAP_SOL or worse.
LOSS_CAP_SOL='-0.107'   # ~$10 at SOL=$93.49
LOG_FILE='/root/piggy/loss_cap_monitor.log'
echo "[$(date)] MONITOR STARTED  cap=${LOSS_CAP_SOL} SOL" | tee -a $LOG_FILE
while true; do
  LOG=$(ls -t /root/piggy/logs/phase20_survivor_*.log 2>/dev/null | head -1)
  if [ -z "$LOG" ]; then sleep 5; continue; fi
  REALIZED=$(grep 'PIGGY-STATUS' $LOG | tail -1 | grep -oE 'realized=[+-][0-9.]+' | sed 's/realized=//')
  if [ -n "$REALIZED" ]; then
    BREACH=$(awk -v r="$REALIZED" -v t="$LOSS_CAP_SOL" 'BEGIN { print (r+0 <= t+0) ? 1 : 0 }')
    if [ "$BREACH" = '1' ]; then
      echo "[$(date)] *** LOSS CAP BREACHED: realized=$REALIZED SOL <= $LOSS_CAP_SOL — KILLING BOT ***" | tee -a $LOG_FILE
      pkill -9 -f PGG2.py
      tmux kill-session -t bot 2>/dev/null
      echo "[$(date)] Bot killed. Monitor exiting." | tee -a $LOG_FILE
      exit 0
    fi
    echo "[$(date)] realized=$REALIZED SOL  (cap=$LOSS_CAP_SOL) OK" >> $LOG_FILE
  fi
  sleep 8
done
