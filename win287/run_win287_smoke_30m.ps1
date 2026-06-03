param(
  [string]$HostName = "87.99.151.70",
  [string]$UserName = "root",
  [string]$KeyPath = "$env:USERPROFILE\.ssh\hetzner_sniper",
  [int]$Seconds = 1800
)

$ErrorActionPreference = "Stop"
$Target = "$UserName@$HostName"

ssh -i $KeyPath $Target "cd /root/piggy && if ps aux | grep -E 'pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|tmux.*v287' | grep -v grep >/dev/null; then echo 'V287-RUN-FATAL existing V287 process'; ps aux | grep -E 'pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|tmux.*v287' | grep -v grep; exit 2; fi; RUNID=v287_oneentry_smoke_`$(date +%Y%m%d_%H%M%S); echo `$RUNID > v287_current_runid.txt; WRAP=logs/`$RUNID.wrapper.log; nohup env V287_SMOKE_SECONDS=$Seconds RUNID=`$RUNID ./_launch_v287_oneentry_smoke.sh > `$WRAP 2>&1 < /dev/null & pid=`$!; echo `$pid > v287_current_pid.txt; echo STARTED_RUNID=`$RUNID PID=`$pid; sleep 2; ps -p `$pid -o pid,cmd --no-headers || true; ls -l `$WRAP logs/`$RUNID.log 2>/dev/null || true"

Write-Host "Use .\monitor_win287.ps1 to watch the run."
