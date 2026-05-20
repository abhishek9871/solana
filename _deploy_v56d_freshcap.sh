#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy

echo PRE_BOT_STATE
ps -eo pid,comm,args | awk '/python/ && (/pgg2_v/ || /PGG2.py/) && !/awk/ {print}' || true
echo PRE_TMUX_STATE
tmux ls 2>/dev/null || true

test -f pgg2_v48_drylive_harness.py.freshcap_candidate
test -f start_v55_stagea.sh.freshcap_candidate
python3 -m py_compile pgg2_v48_drylive_harness.py.freshcap_candidate
bash -n start_v55_stagea.sh.freshcap_candidate

TS="$(date +%Y%m%d_%H%M%S)"
cp -p pgg2_v48_drylive_harness.py "pgg2_v48_drylive_harness.py.pre_v56d_freshcap_${TS}.bak"
cp -p start_v55_stagea.sh "start_v55_stagea.sh.pre_v56d_freshcap_${TS}.bak"

mv pgg2_v48_drylive_harness.py.freshcap_candidate pgg2_v48_drylive_harness.py
mv start_v55_stagea.sh.freshcap_candidate start_v55_stagea.sh
chmod +x start_v55_stagea.sh

python3 -m py_compile pgg2_v48_drylive_harness.py
bash -n start_v55_stagea.sh

echo "DEPLOY_OK TS=${TS}"
grep -n "V56D-LIVE-SOURCE-LEAD-BLOCK\|V56D_ACTUAL_MAX_SOURCE_LEAD\|MAX_SNAPSHOT_AGE_AT_SEND" \
  pgg2_v48_drylive_harness.py start_v55_stagea.sh
