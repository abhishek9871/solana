@echo off
setlocal
title KILL PGG2 HETZNER
echo.
echo EMERGENCY STOP: killing PGG2 on Hetzner...
echo.
ssh -i "%USERPROFILE%\.ssh\hetzner_sniper" -o StrictHostKeyChecking=no root@87.99.151.70 "tmux -S /tmp/pgg2attack.sock kill-session -t pgg2attack 2>/dev/null || true; tmux kill-session -t pgg2attack 2>/dev/null || true; pkill -f 'python -u PGG2.py' 2>/dev/null || true; pkill -f 'PGG2.py' 2>/dev/null || true; echo PGG2_KILL_SENT_UTC=$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)"
echo.
echo Done. If a tail window is still open, it may keep showing old buffered lines.
pause
