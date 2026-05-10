@echo off
title Bot Logs - DUAL-FILTER (score>=180, fill>=7e-5)
color 0a
:loop
echo.
echo ============================================================
echo   STREAMING LATEST BOT LOG FROM HETZNER (87.99.151.70)
echo   (Reattaches to newest log file every 60s)
echo ============================================================
echo.
ssh -i "C:\Users\VASU\.ssh\hetzner_sniper" -o StrictHostKeyChecking=no -o ServerAliveInterval=15 root@87.99.151.70 "while true; do L=$(ls -t /root/piggy/logs/phase20_survivor_*.log | head -1); echo --- FOLLOWING $L ---; timeout 60 tail -F -n 30 $L; sleep 1; done"
echo.
echo SSH disconnected -- retrying in 3 seconds (Ctrl+C to abort)
timeout /t 3 /nobreak >nul
goto loop
