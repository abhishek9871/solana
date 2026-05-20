#!/bin/bash
set -e
LOGNAME="v55_live_$(date +%H%M%S).log"
echo "log=$LOGNAME"
tmux new-session -d -s bot "cd /root/piggy && \
  PGG2_LIVE_CONFIRM_TIMEOUT_SEC=30 \
  PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL=0.040 \
  PGG2_V67_SKIP_TOKEN_PREFLIGHT=1 \
  PGG2_V48_SESSION_CLOSED_BLOCK=1 \
  PGG2_V48_PUMP_SUFFIX_ONLY=1 \
  PGG2_V48_DEPLOYER_BLOCK_ON_MISSING_SCORE=1 \
  PGG2_V48_DEPLOYER_REQUIRE_PUMPPORTAL=1 \
  bash /root/piggy/start_v55_stagea.sh 2>&1 | tee /root/piggy/$LOGNAME"
echo "$LOGNAME" > /tmp/current_log.txt
sleep 1
tmux ls
