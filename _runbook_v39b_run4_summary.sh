#!/bin/bash
# RUN4 Step 6 — verbatim user sequence
set +e
cd /root/piggy
RUNID=$(cat current_pgg2_live_mirror_runid.txt)
LOG=/root/piggy/logs/${RUNID}.log

tmux kill-session -t v39b_live_mirror 2>/dev/null || true
pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true

echo "LOG=$LOG"

echo "BUY_SENT=$(grep -c 'PGG2-V39-LIVE-BUY-SENT' "$LOG")"
echo "SELL_CONFIRMED=$(grep -c 'PGG2-V39-LIVE-SELL-CONFIRMED' "$LOG")"
echo "MIRROR_END=$(grep -c 'PGG2-V39-LIVE-MIRROR-END' "$LOG")"
echo "NEGATIVE=$(grep -c 'actual_all_in_pnl=-' "$LOG")"
echo "FATAL_MISMATCH=$(grep -c 'PGG2-POSITION-TOKEN-MISMATCH-FATAL' "$LOG")"
echo "CLOSE_FAIL=$(grep -c 'PGG2-RISK-CLOSE-FAIL' "$LOG")"
echo "TRACEBACK=$(grep -c 'Traceback' "$LOG")"

echo ""
echo "--- PRESEND-REQUOTE + MIRROR-END + STATUS tail 40 ---"
grep -E 'PGG2-V39-LIVE-PRESEND-REQUOTE|PGG2-V39-LIVE-MIRROR-END|PIGGY-STATUS' "$LOG" | tail -n 40

echo ""
echo "--- procs/tmux ---"
pgrep -af 'python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py' || true
tmux ls 2>/dev/null || true
