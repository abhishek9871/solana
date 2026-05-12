@echo off
title Bot Logs - OG Live Winner (commit 7128dc7)
color 0a
:loop
echo.
echo ============================================================
echo   STREAMING LATEST BOT LOG FROM HETZNER (87.99.151.70)
echo   (Reattaches to newest log file every 60s)
echo ============================================================
echo.
ssh -i "C:\Users\VASU\.ssh\hetzner_sniper" -o StrictHostKeyChecking=no -o ServerAliveInterval=15 root@87.99.151.70 "while true; do L=$(ls -t /root/piggy/logs/pgg2_v30_*.log /root/piggy/logs/pgg2_direct_live_*.log /root/piggy/logs/pgg2_direct_drylive_*.log /root/piggy/logs/phase20_survivor_*.log 2>/dev/null | head -1); echo --- FOLLOWING $L ---; timeout 60 tail -F -n 50 $L; sleep 1; done"
echo.
echo SSH disconnected -- retrying in 3 seconds (Ctrl+C to abort)
timeout /t 3 /nobreak >nul
goto loop
