param(
  [string]$HostName = "87.99.151.70",
  [string]$UserName = "root",
  [string]$KeyPath = "$env:USERPROFILE\.ssh\hetzner_sniper"
)

$ErrorActionPreference = "Stop"
$Target = "$UserName@$HostName"

ssh -i $KeyPath $Target @'
cd /root/piggy || exit 1
RUNID=$(cat v287_current_runid.txt 2>/dev/null || true)
echo RUNID=$RUNID
date -u
ps aux | grep -E "pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|_launch_v287" | grep -v grep || echo no_v287_process
echo COUNTS
grep -E "PGG2-V287-(BUY-SEND|SMOKE-END|BUY-FAILED-SAFE|PROTECTED-SELL-SEND|PROTECTED-SELL-CONFIRMED|SEED-PRIOR-FINAL-SEND-AUTHORITY-PASS|FINAL-BUY-QUOTE-TOKEN-CAP-CHECK|SEED-PRIOR-TOKEN-CAP-OVERRIDE|CANDIDATE-ABORT-SELL|CANDIDATE-EXPIRE|REARM-PASS|SEED-PRIOR-CARRY-CANDIDATE|SEED-PRIOR-FINAL-SEND-AUTHORITY-CHECK|SEED-PRIOR-FINAL-SEND-AUTHORITY-BLOCK)" logs/${RUNID}.log 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i ~ /^PGG2-V287-/) c[$i]++} END{for(k in c) print k,c[k]}' | sort
echo EVENTS
grep -E "PGG2-V287-(BUY-SEND|PROTECTED-SELL|SMOKE-END|BUY-FAILED-SAFE|SEED-PRIOR-FINAL-SEND-AUTHORITY-(PASS|CHECK|BLOCK))" logs/${RUNID}.log 2>/dev/null | tail -80
'@
