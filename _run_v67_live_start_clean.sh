#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy
set -a
. /root/piggy/.env
set +a

echo "=== CLEAN STOP ==="
for s in v67_stagea_live v55_stagea_live v58_live v61_live v50b_stagea_live v39b_live_mirror v39b_live_smoke; do
  tmux kill-session -t "$s" 2>/dev/null || true
done
/root/piggy/venv/bin/python - <<'PY'
import os, signal, subprocess, time
needles=('pgg2_v50b_stagea_live.py','pgg2_v48_drylive_harness.py','PGG2.py')
for sig in (signal.SIGTERM, signal.SIGKILL):
    out=subprocess.check_output(['ps','-eo','pid,comm,args'], text=True)
    pids=[]
    for line in out.splitlines()[1:]:
        parts=line.strip().split(None,2)
        if len(parts)<3: continue
        pid=int(parts[0]); comm=parts[1]; args=parts[2]
        if not comm.startswith('python'): continue
        if any(n in args for n in needles): pids.append(pid)
    if not pids: break
    for pid in pids:
        try: os.kill(pid, sig)
        except ProcessLookupError: pass
    time.sleep(1)
PY

echo "=== VALIDATE ==="
python3 -m py_compile /root/piggy/pgg2_v48_drylive_harness.py /root/piggy/pgg2_v50b_stagea_live.py
bash -n /root/piggy/start_v55_stagea.sh

echo "=== OPEN TOKEN CHECK ==="
/root/piggy/venv/bin/python - <<'PY'
from solders.pubkey import Pubkey
from birth_first_sniper import BotConfig
from pgg2_direct_pump import DirectPumpQuoteBroker
b=DirectPumpQuoteBroker(BotConfig())
open_count=0
for mint in ['7syJyNSyjcFMEWne7or4qSWLXvfD1JvamAgPoEC6pump','31LCn7AEDPkTwteYJHS7Bj5wedSqSAcfd2rh8EC1pump']:
    pk=Pubkey.from_string(mint)
    try:
        raw=b.token_balance_raw(pk)
    except Exception:
        raw=0
    print('CHECK_RAW', mint, raw)
    if raw: open_count += 1
print('WALLET_SOL', b.balance_sol())
print('OPEN_TOKEN_COUNT', open_count)
if open_count:
    raise SystemExit(3)
PY

RUNID=pgg2_v67_only_live_$(date +%Y%m%d_%H%M%S)
echo "$RUNID" > current_pgg2_v67_stagea_runid.txt
LOG=/root/piggy/logs/${RUNID}.log
export RUNID
export PGG2_V50B_MAX_CLOSES=10
export PGG2_V50B_MAX_OPEN=1
export PGG2_V50B_MAX_SECONDS=2100
export PGG2_V67_ONLY_LANE=1
export PGG2_V67_FLOW_CONFIRM_LANE_ENABLED=1
export PGG2_V67_BYPASS_LEGACY_GATES=1
export PGG2_V67_ALLOW_RULE_UNION_BYPASS=1
export PGG2_V67_MAX_BUY_SOL_1000=25.000
export PGG2_V67_MAX_V47H_RATIO=0.339
export PGG2_JITO_ENABLED=0
export PGG2_PROTECTED_HOLD_ENABLED=0

echo "=== START ==="
tmux new -ds v67_stagea_live "cd /root/piggy && timeout 2100 ./start_v55_stagea.sh 2>&1 | tee -a '$LOG'"
sleep 3
echo RUNID=$RUNID
echo LOG=$LOG
tmux ls | grep v67_stagea_live || true
/root/piggy/venv/bin/python - <<'PY'
import subprocess
out=subprocess.check_output(['ps','-eo','pid,comm,args'], text=True)
for line in out.splitlines():
    if 'pgg2_v50b_stagea_live.py' in line or 'pgg2_v48_drylive_harness.py' in line:
        if 'python' in line:
            print(line)
PY
echo "=== TAIL ==="
tail -n 60 "$LOG"
