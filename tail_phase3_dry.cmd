@echo off
title *** PHASE 7D - FULL-SIZE 0.05 SOL POSITIONS - phase7d_dry ***
ssh -i C:\Users\VASU\.ssh\hetzner_sniper -o StrictHostKeyChecking=no root@87.99.151.70 "tail -F /root/piggy/logs/pgg2_phase7d_drylive_20260508_073430.log /root/piggy/data/pgg2_phase7d_drylive_20260508_073430_decisions.jsonl"
