#!/usr/bin/env bash
# v33 run-to-gate loop. Polls the holdout accumulator and stops when the
# pre-registered rule's gates pass OR 120 minutes elapse. May boost sampler
# density at 60 min if qualifying count < 7. The bot is assumed to already
# be running (started by the caller). Writes a single-word result to
# /tmp/v33_runtogate_result.txt: GATE_PASSED | TIMEOUT | ERROR.
#
# Usage:
#   ./pgg2_v33_runtogate.sh /root/piggy/logs/<log-path>

set -u
LOG="${1:-}"
if [ -z "$LOG" ]; then
  echo "usage: $0 <log_path>" >&2
  exit 1
fi
cd /root/piggy
START_TS=$(date -u +%s)
ACC=/root/piggy/data/v33_holdout_accumulator.json
RESULT=/tmp/v33_runtogate_result.txt
BOOSTED=0
echo "" > $RESULT
while true; do
  ELAPSED=$(( $(date -u +%s) - START_TS ))
  python3 pgg2_v33_holdout_accumulator.py --lab data/pgg2_executable_shadow_lab.jsonl --log "$LOG" --gate > /tmp/v33_acc.json 2>&1
  RC=$?
  if [ -f $ACC ]; then
    Q=$(python3 -c "import json,sys;d=json.load(open('$ACC'));print(d.get('qualifying_count',0))" 2>/dev/null)
    PASS=$(python3 -c "import json,sys;d=json.load(open('$ACC'));print(1 if d.get('all_gates_passed') else 0)" 2>/dev/null)
    NET=$(python3 -c "import json,sys;d=json.load(open('$ACC'));print(d.get('net_all_in_pnl',0.0))" 2>/dev/null)
  else
    Q=0
    PASS=0
    NET=0
  fi
  echo "[$(date -u +%H:%M:%S)] elapsed=${ELAPSED}s qualifying_count=${Q} all_gates_passed=${PASS} net=${NET} rc=${RC} boosted=${BOOSTED} log=${LOG}"
  if [ "${PASS}" = "1" ]; then
    echo "GATE_PASSED" > $RESULT
    break
  fi
  if [ ${ELAPSED} -ge 7200 ]; then
    echo "TIMEOUT" > $RESULT
    break
  fi
  if [ ${ELAPSED} -ge 3600 ] && [ "${BOOSTED}" = "0" ] && [ "${Q}" -lt 7 ]; then
    echo "[boosting sampler at elapsed=${ELAPSED}s qualifying=${Q}]"
    tmux kill-session -t bot 2>/dev/null || true
    sleep 2
    pkill -9 -f "python -u PGG2.py" 2>/dev/null || true
    sleep 2
    TS2=$(date -u +%Y%m%d_%H%M%S)
    LOG="/root/piggy/logs/pgg2_v33_runtogate_${TS2}_boosted.log"
    echo "$LOG" > /tmp/v30_log_path
    tmux new -d -s bot "cd /root/piggy && PGG2_ACTUAL_ENTRY_MASTER_ENABLED=0 PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=0 PGG2_DRYLIVE_PILOT_ENABLED=0 PGG2_SHADOW_DELAYED_ENTRY_SCANNER=1 PGG2_SHADOW_OBSERVE_MIN_EVENT_SOL=1.0 PGG2_SHADOW_OBSERVE_MIN_SOL_PRICED_SNAP=0.5 PGG2_SHADOW_OBSERVE_MIN_SOL_CURVE_LAG=0.5 ./start_pgg2_v30_shadowlab_drylive.sh > $LOG 2>&1"
    BOOSTED=1
    sleep 8
  fi
  sleep 300
done
cat $RESULT
