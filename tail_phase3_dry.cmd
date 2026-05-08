@echo off
title *** PHASE 20C SURVIVOR (poll-driven strikes + 24h age) - phase20c_dry ***
ssh -i C:\Users\VASU\.ssh\hetzner_sniper -o StrictHostKeyChecking=no root@87.99.151.70 "RUNID=$(cat /root/piggy/current_pgg2_runid.txt 2>/dev/null) && tail -F /root/piggy/logs/${RUNID}.log /root/piggy/data/${RUNID}_decisions.jsonl"
