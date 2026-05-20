#!/bin/bash
set -euo pipefail
cd /root/piggy
RUNID="v56b_v56d_guarded_live_$(date +%Y%m%d_%H%M%S)"
echo "$RUNID" > current_pgg2_v58_live_runid.txt
mkdir -p logs
exec timeout 1000 ./start_v55_stagea.sh 2>&1 | tee -a "logs/${RUNID}.log"
