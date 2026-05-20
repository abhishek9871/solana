#!/bin/bash
# Steps 6/7 stop + Step 8 summary (verbatim user sequence)
set +e
cd /root/piggy || exit 2

echo "=== STEP 7: STOP ==="
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython[0-9.]* PGG2.py' 2>/dev/null || true

sleep 2
echo "TMUX=$(tmux ls 2>/dev/null | wc -l)"
echo "BOT_PROCS=$(pgrep -af '[p]ython[0-9.]* -u PGG2.py|[p]ython[0-9.]* PGG2.py' | wc -l)"

echo ""
echo "=== STEP 8: SUMMARY ==="
RUNID=$(cat current_pgg2_live_mirror_runid.txt)
LOG="logs/${RUNID}.log"

echo "RUNID=$RUNID"
echo "LOG=/root/piggy/$LOG"
echo "V39_BUY_SEND=$(grep -c 'PGG2-V39-LIVE-BUY-SEND' "$LOG" || true)"
echo "V39_BUY_CONFIRMED=$(grep -c 'PGG2-V39-LIVE-BUY-CONFIRMED' "$LOG" || true)"
echo "V39_SELL_SEND=$(grep -c 'PGG2-V39-LIVE-SELL-SEND' "$LOG" || true)"
echo "V39_SELL_CONFIRMED=$(grep -c 'PGG2-V39-LIVE-SELL-CONFIRMED' "$LOG" || true)"
echo "MIRROR_END=$(grep -c 'PGG2-V39-LIVE-MIRROR-END' "$LOG" || true)"
echo "NEGATIVE_CLOSES=$(grep -c 'actual_all_in_pnl=-' "$LOG" || true)"
echo "TOKEN_MISMATCH=$(grep -c 'PGG2-POSITION-TOKEN-MISMATCH-FATAL' "$LOG" || true)"
echo "CLOSE_FAIL=$(grep -c 'PGG2-RISK-CLOSE-FAIL' "$LOG" || true)"
echo "TRACEBACK=$(grep -c 'Traceback' "$LOG" || true)"
echo "NON_V39_LIVE_BUY=$(grep 'PGG2-LIVE-BUY ' "$LOG" | grep -vc 'v39' || true)"

echo ""
echo "=== KEY LIVE LINES + PIGGY-STATUS (tail 160) ==="
grep -E 'PGG2-LIVE-MIRROR-START|PGG2-V39-LIVE-BUY|PGG2-V39-LIVE-SELL|PGG2-V39-LIVE-WALLET-DELTA|PGG2-V39-LIVE-PNL-RECONCILE|PGG2-V39-LIVE-MIRROR-END|PIGGY-STATUS' "$LOG" | tail -n 160
