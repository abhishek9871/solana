#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy
RUNID="$(cat current_v55_stagea_live_runid.txt 2>/dev/null || true)"
LOG="/root/piggy/logs/${RUNID}.log"
echo "RUNID=${RUNID}"
echo "LOG=${LOG}"
if [[ ! -f "$LOG" ]]; then
  echo "LOG_MISSING=1"
  exit 0
fi
echo "NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "TMUX=$(tmux ls 2>/dev/null | grep -c '^v55_stagea_live:' || true)"
echo "BOT_PROCS=$(ps -eo args | grep -E 'python.*pgg2_v|python.*PGG2.py' | grep -v grep | wc -l)"
for pat in \
  'PGG2-V48-CANDIDATE-DECISION' \
  'PGG2-V56D-FLOW-LANE-PASS' \
  'PGG2-V56D-TICK1-FAST-LANE-PASS' \
  'PGG2-V56D-TICK1-FAST-LANE-BLOCK' \
  'PGG2-V56B-SHADOW-ONLY-LIVE-BLOCK' \
  'PGG2-V48-DRYLIVE-ENTRY-OPEN' \
  'PGG2-V48-LIVE-BUY-SEND' \
  'PGG2-V48-LIVE-BUY-FAILED-SAFE' \
  'PGG2-V48-LIVE-BUY-CONFIRMED' \
  'PGG2-V48-LIVE-SELL-SEND' \
  'PGG2-V48-LIVE-SELL-CONFIRMED' \
  'PGG2-V50B-BUY-SEND' \
  'PGG2-V50B-BUY-SENT' \
  'PGG2-V50B-BUY-FAILED-SAFE' \
  'PGG2-V50B-BUY-CONFIRMED' \
  'PGG2-V50B-SELL-SEND' \
  'PGG2-V50B-SELL-CONFIRMED' \
  'PGG2-V48-LIVE-MIRROR-END' \
  'PGG2-V50B-STOP' \
  'actual_all_in_pnl=-' \
  'TOKEN-MISMATCH' \
  'CLOSE-FAIL' \
  'Traceback'
do
  key="$(echo "$pat" | tr 'A-Z-' 'a-z_' | tr -cd 'a-z0-9_')"
  echo "${key}=$(grep -c "$pat" "$LOG" 2>/dev/null || true)"
done
echo "--- recent important lines ---"
grep -E 'V56D-FLOW-LANE-PASS|V56D-TICK1|V56B-SHADOW|DRYLIVE-ENTRY-(OPEN|BLOCK)|BUY-(SEND|SENT|FAILED|CONFIRMED)|SELL-(SEND|SENT|CONFIRMED)|LIVE-MIRROR-END|V50B-STOP|actual_all_in_pnl=|TOKEN-MISMATCH|CLOSE-FAIL|Traceback' "$LOG" | tail -n 25 || true
