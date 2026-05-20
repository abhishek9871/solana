#!/usr/bin/env bash
set -euo pipefail
cd /root/piggy

echo "=== CLEAN STOP ==="
for s in v67_stagea_live v55_stagea_live v58_live v61_live v50b_stagea_live v39b_live_mirror v39b_live_smoke; do
  tmux kill-session -t "$s" 2>/dev/null || true
done
python3 - <<'PY'
import os, signal, subprocess, time
needles=('pgg2_v50b_stagea_live.py','pgg2_v48_drylive_harness.py','PGG2.py')
me=os.getpid()
for _ in range(2):
    out=subprocess.check_output(['ps','-eo','pid,comm,args'], text=True)
    pids=[]
    for line in out.splitlines()[1:]:
        parts=line.strip().split(None,2)
        if len(parts)<3: continue
        pid=int(parts[0]); comm=parts[1]; args=parts[2]
        if pid==me or not comm.startswith('python'): continue
        if any(n in args for n in needles):
            pids.append(pid)
    if not pids: break
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
    time.sleep(1)
PY

echo "=== ACTIVE VALIDATION ==="
python3 -m py_compile /root/piggy/pgg2_v48_drylive_harness.py /root/piggy/pgg2_v50b_stagea_live.py
bash -n /root/piggy/start_v55_stagea.sh
if grep -RInE 'V42C|CurveAccountSubscriber|pgg2_v42_curve|accountSubscribe' /root/piggy/pgg2_v48_drylive_harness.py /root/piggy/start_v55_stagea.sh /root/piggy/pgg2_v50b_stagea_live.py; then
  echo "ACTIVE_REF_VERIFY=FAIL"
  exit 10
fi

echo "=== WALLET CLEAN CHECK ==="
python3 - <<'PY'
import json, urllib.request
wallet='Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7'
url='https://api.mainnet-beta.solana.com'
def rpc(method, params):
    data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode()
    req=urllib.request.Request(url, data=data, headers={'Content-Type':'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=12))
bal=rpc('getBalance',[wallet]).get('result',{}).get('value',0)/1e9
tok=rpc('getTokenAccountsByOwner',[wallet, {'programId':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'}, {'encoding':'jsonParsed'}]).get('result',{}).get('value',[])
open_accts=[]
for a in tok:
    amt=((a.get('account') or {}).get('data') or {}).get('parsed',{}).get('info',{}).get('tokenAmount',{})
    if int(amt.get('amount','0') or 0) != 0:
        open_accts.append(a.get('pubkey'))
print(f'WALLET_SOL={bal:.9f}')
print(f'TOKEN_ACCOUNT_TOTAL={len(tok)}')
print(f'OPEN_TOKEN_COUNT={len(open_accts)}')
if open_accts:
    print('OPEN_TOKEN_ACCOUNTS=' + ','.join(open_accts[:10]))
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
python3 - <<'PY'
import subprocess
out=subprocess.check_output(['ps','-eo','pid,comm,args'], text=True)
for line in out.splitlines():
    if 'pgg2_v50b_stagea_live.py' in line or 'pgg2_v48_drylive_harness.py' in line:
        if 'python' in line:
            print(line)
PY
echo "=== TAIL ==="
tail -n 80 "$LOG"
