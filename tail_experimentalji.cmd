@echo off
title Experimentalji Dry-Live Tail - Hetzner
ssh -i C:\Users\VASU\.ssh\hetzner_sniper -o StrictHostKeyChecking=no root@87.99.151.70 "cd /root/piggy; RUNID=$(cat /root/piggy/current_pgg2_runid.txt); echo RUN_ID=$RUNID; echo LOG=/root/piggy/logs/${RUNID}.log; tail -f /root/piggy/logs/${RUNID}.log"
