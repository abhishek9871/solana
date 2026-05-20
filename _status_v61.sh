#!/usr/bin/env bash
set +e
cd /root/piggy || exit 2
RUNID="$(cat current_v61_live_runid.txt 2>/dev/null)"
LOG="logs/${RUNID}.log"
echo "RUNID=${RUNID}"
echo "LOG=/root/piggy/${LOG}"
date -u '+UTC=%Y-%m-%dT%H:%M:%SZ'
for p in \
  'PGG2-V42C-WS-DISABLED-RPC-FALLBACK' \
  'PGG2-V48-SHRED-RECONNECT' \
  'PGG2-V42C-CURVE-UPDATE' \
  'source=rpc_fallback' \
  'PGG2-V61-LEGACY-GATE-BYPASS' \
  'PGG2-V61-PREENTRY-BLOCK' \
  'PGG2-V61-FANOUT-LEAD-CHECK' \
  'pass=1 blockers=-' \
  'PGG2-V61-FANOUT-LANE-PASS' \
  'PGG2-V61-LIVE-ACTUAL-ENTRY-ALLOW' \
  'PGG2-V48-LIVE-BUY-SEND' \
  'PGG2-V48-LIVE-BUY-FAILED-SAFE' \
  'PGG2-V48-LIVE-BUY-CONFIRMED' \
  'PGG2-V48-LIVE-CLOSE-CONFIRMED' \
  'actual_all_in_pnl=-' \
  'TOKEN-MISMATCH' \
  'CLOSE-FAIL' \
  'Traceback'
do
  printf '%s=' "$p"
  grep -c "$p" "$LOG" 2>/dev/null
done
echo "--- tail ---"
grep -E 'V42C-WS-DISABLED|V48-SHRED-RECONNECT|V42C-CURVE-UPDATE|V61-LEGACY-GATE-BYPASS|V61-PREENTRY-BLOCK|V61-FANOUT-LEAD-CHECK|V61-FANOUT-LANE-PASS|V61-LIVE-ACTUAL-ENTRY-ALLOW|V48-LIVE-BUY-SEND|V48-LIVE-BUY-FAILED|V48-LIVE-BUY-CONFIRMED|V48-LIVE-CLOSE-CONFIRMED|actual_all_in_pnl|V48-STOP|Traceback|TOKEN-MISMATCH|CLOSE-FAIL' "$LOG" 2>/dev/null | tail -n 80
echo "--- procs ---"
pgrep -af 'pgg2_v50b_stagea_live.py|pgg2_v48_drylive_harness.py|start_v55_stagea.sh' 2>/dev/null || echo "NO_BOT_PROCS"
tmux ls 2>/dev/null || echo "NO_TMUX"
