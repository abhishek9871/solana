@echo off
title PGG2 Experimentalji Tail - Hetzner
cd /d C:\Users\VASU\Desktop\tradingMahadevjiwin
echo Connecting to Hetzner PGG2 tail...
echo.
ssh -t -i C:\Users\VASU\.ssh\hetzner_sniper -o StrictHostKeyChecking=no root@87.99.151.70 "cd /root/piggy; RUNID=$(cat current_pgg2_runid.txt); LOG=/root/piggy/logs/${RUNID}.log; echo RUN_ID=${RUNID}; echo LOG=${LOG}; echo; tail -n 80 -F ${LOG}"
echo.
echo Tail ended. Press any key to close.
pause >nul
