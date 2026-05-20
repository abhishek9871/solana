#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy

echo PRE_BOT_STATE
ps -eo pid,comm,args | awk '/python/ && (/pgg2_v/ || /PGG2.py/) && !/awk/ {print}' || true
echo PRE_TMUX_STATE
tmux ls 2>/dev/null || true

test -f pgg2_v48_drylive_harness.py.tick1_candidate
test -f start_v55_stagea.sh.tick1_candidate

python3 -m py_compile pgg2_v48_drylive_harness.py.tick1_candidate
bash -n start_v55_stagea.sh.tick1_candidate

TS="$(date +%Y%m%d_%H%M%S)"
cp -p pgg2_v48_drylive_harness.py "pgg2_v48_drylive_harness.py.pre_v56d_tick1_${TS}.bak"
cp -p start_v55_stagea.sh "start_v55_stagea.sh.pre_v56d_tick1_${TS}.bak"

mv pgg2_v48_drylive_harness.py.tick1_candidate pgg2_v48_drylive_harness.py
mv start_v55_stagea.sh.tick1_candidate start_v55_stagea.sh
chmod +x start_v55_stagea.sh

python3 -m py_compile pgg2_v48_drylive_harness.py
bash -n start_v55_stagea.sh

echo "DEPLOY_OK TS=${TS}"
ls -l \
  pgg2_v48_drylive_harness.py \
  "pgg2_v48_drylive_harness.py.pre_v56d_tick1_${TS}.bak" \
  start_v55_stagea.sh \
  "start_v55_stagea.sh.pre_v56d_tick1_${TS}.bak"
