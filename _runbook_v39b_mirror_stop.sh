#!/bin/bash
# Step 2 — Stop Anything Running (verbatim user sequence)
cd /root/piggy || exit 2

tmux kill-session -t v39b_live_smoke 2>/dev/null || true
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
pkill -f 'python -u PGG2.py' 2>/dev/null || true
pkill -f 'python3 -u PGG2.py' 2>/dev/null || true
pkill -f 'python[0-9.]* PGG2.py' 2>/dev/null || true

sleep 2
echo "TMUX=$(tmux ls 2>/dev/null | wc -l)"
echo "BOT_PROCS=$(pgrep -af 'python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py' | wc -l)"
