param(
    [int]$TargetWins = 10,
    [int]$MaxCycles = 30,
    [int]$MaxMinutes = 60,
    [int]$ScanTimeoutSec = 115,
    [int]$SendTimeoutSec = 65,
    [int]$SendLimit = 35,
    [int]$MaxPerMint = 4,
    [string]$Remote = "root@87.99.151.70",
    [string]$SshKey = "$env:USERPROFILE\.ssh\hetzner_sniper",
    [string]$WslDistro = "Ubuntu-24.04",
    [string]$WslProject = "~/pgg2-local",
    [string]$RemoteProject = "/root/piggy",
    [string]$CandidateFile = "_v223_v246_broad.jsonl"
)

$ErrorActionPreference = "Stop"

$sizes = "0.00005,0.00006,0.00007,0.00008,0.00009,0.00010,0.00011,0.00012,0.00013,0.00014,0.00015,0.00016,0.00018,0.00020,0.00025,0.00030,0.00040,0.00050,0.00075,0.001,0.0015,0.002,0.003,0.005,0.0075,0.01,0.015,0.02,0.03,0.04,0.05"
$runId = Get-Date -Format "yyyyMMdd_HHmmss"
$logPath = Join-Path $PSScriptRoot "v256_rpcfast_atomic_loop_$runId.log"
$localCandidate = Join-Path $PSScriptRoot $CandidateFile
$startedAt = Get-Date

function Write-RunLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $line | Tee-Object -FilePath $logPath -Append
}

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command,
        [switch]$AllowFailure
    )
    Write-RunLog "PGG2-V256-$Label-START"
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Command 2>&1
        $code = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } else { 0 }
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
    $out | ForEach-Object { Write-RunLog "$_" }
    Write-RunLog "PGG2-V256-$Label-END exit=$code"
    if (-not $AllowFailure -and $code -ne 0) {
        throw "PGG2-V256-$Label failed with exit=$code"
    }
    return [pscustomobject]@{ ExitCode = $code; Output = ($out -join "`n") }
}

function Invoke-Remote {
    param([string]$RemoteCommand, [switch]$AllowFailure)
    Invoke-Checked -Label "SSH" -AllowFailure:$AllowFailure -Command {
        $args = @("-i", $SshKey, "-o", "LogLevel=ERROR", $Remote, $RemoteCommand)
        & ssh.exe @args
    }
}

function Get-WalletState {
    $cmd = "cd $RemoteProject && /root/piggy/venv/bin/python -u v246_wallet_check.py"
    $res = Invoke-Remote -RemoteCommand $cmd
    $text = $res.Output
    if ($text -notmatch "balance_lamports=(\d+).*token_accounts=(\d+).*nonzero_tokens=(\d+)") {
        throw "Could not parse wallet state"
    }
    return [pscustomobject]@{
        Lamports = [int64]$Matches[1]
        TokenAccounts = [int]$Matches[2]
        NonzeroTokens = [int]$Matches[3]
        Raw = $text
    }
}

function Assert-RemoteClean {
    $cmd = @"
cd $RemoteProject
if ps aux | grep -E 'python.*(pgg2|v22|v24|v245|v246|v252|v253|v254|v255|v256|raptor)' | grep -v grep >/tmp/v256_bot_procs.txt; then
  cat /tmp/v256_bot_procs.txt
  echo PGG2-V256-REMOTE-BOT-PROCS=1
  exit 12
else
  echo PGG2-V256-REMOTE-BOT-PROCS=0
fi
/root/piggy/venv/bin/python -u v246_wallet_check.py
"@
    $res = Invoke-Remote -RemoteCommand $cmd
    if ($res.Output -notmatch "PGG2-V256-REMOTE-BOT-PROCS=0") {
        throw "Remote bot process detected"
    }
    if ($res.Output -notmatch "token_accounts=0 nonzero_tokens=0") {
        throw "Remote token residual detected"
    }
}

function Invoke-FreshScan {
    if (Test-Path $localCandidate) {
        Remove-Item -LiteralPath $localCandidate -Force
    }
    $scan = @"
set -e
cd $WslProject
source ~/pgg2-local-venv/bin/activate
rm -f data/v223_v246_broad.jsonl data/v256_scan_last.log
timeout ${ScanTimeoutSec}s python -u v223_gpa_multipool_eval.py --max-mints 500 --sizes-sol '$sizes' --fee-buffer-lamports 0 --projection-buffer-lamports 0 --min-edge-lamports 1 --min-quote-reserve-lamports 0 --out-jsonl /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl > data/v256_scan_last.log 2>&1 || true
tail -8 data/v256_scan_last.log || true
if [ -s /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl ]; then
  cp /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl /mnt/c/Users/VASU/Desktop/tradingMahadevjiwin/$CandidateFile
  printf 'PGG2-V256-SCAN-CANDIDATES '
  wc -l /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl
else
  echo 'PGG2-V256-SCAN-NO-CANDIDATES'
fi
"@
    $res = Invoke-Checked -Label "SCAN" -AllowFailure -Command {
        & wsl -d $WslDistro -- bash -lc $scan
    }
    if (-not (Test-Path $localCandidate)) {
        return [pscustomobject]@{ Candidates = 0; Output = $res.Output }
    }
    $count = 0
    if ($res.Output -match "PGG2-V256-SCAN-CANDIDATES\s+(\d+)") {
        $count = [int]$Matches[1]
    }
    return [pscustomobject]@{ Candidates = $count; Output = $res.Output }
}

function Upload-Candidates {
    Invoke-Checked -Label "UPLOAD" -Command {
        $args = @("-i", $SshKey, "-q", $localCandidate, "${Remote}:$RemoteProject/data/v223_v246_broad.jsonl")
        & scp.exe @args
    } | Out-Null
}

function Invoke-OneAtomicSend {
    $cmd = @"
cd $RemoteProject
timeout ${SendTimeoutSec}s /root/piggy/venv/bin/python -u v255_jito_inline_atomic.py --limit $SendLimit --max-per-mint $MaxPerMint --lut-json '' --tip-ladder-lamports 0 --good-enough-tip-lamports 0 --quote-cushions 1,2,4,8,10,16,24,32 --min-profit-lamports 1 --min-positive-delta-lamports 1 --transport rpcfast_rpc --live --confirm-live I_ACCEPT_V255_JITO_INLINE_ATOMIC_RISK
rc=`$?
echo PGG2-V256-V255-EXIT=`$rc
/root/piggy/venv/bin/python -u v246_wallet_check.py
exit 0
"@
    $res = Invoke-Remote -RemoteCommand $cmd -AllowFailure
    $text = $res.Output
    if ($text -match "PGG2-V255-FINAL-WALLET pre=(\d+) post=(\d+) delta=([+-]?\d+)") {
        return [pscustomobject]@{
            Sent = $true
            Pre = [int64]$Matches[1]
            Post = [int64]$Matches[2]
            Delta = [int64]$Matches[3]
            Output = $text
        }
    }
    return [pscustomobject]@{ Sent = $false; Pre = 0; Post = 0; Delta = 0; Output = $text }
}

Write-RunLog "PGG2-V256-LOOP-START target_wins=$TargetWins max_cycles=$MaxCycles max_minutes=$MaxMinutes"
Assert-RemoteClean
$baseline = Get-WalletState
$wins = 0
$attempts = 0
$noSendCycles = 0

while ($wins -lt $TargetWins -and $attempts -lt $MaxCycles) {
    $elapsed = ((Get-Date) - $startedAt).TotalMinutes
    if ($elapsed -ge $MaxMinutes) {
        Write-RunLog ("PGG2-V256-STOP reason=max_minutes elapsed_min={0:N2}" -f $elapsed)
        break
    }

    $attempts += 1
    Write-RunLog "PGG2-V256-CYCLE-START cycle=$attempts wins=$wins"
    Assert-RemoteClean

    $scanResult = Invoke-FreshScan
    if ($scanResult.Candidates -le 0) {
        $noSendCycles += 1
        Write-RunLog "PGG2-V256-CYCLE-NO-SCAN-CANDIDATES cycle=$attempts no_send_cycles=$noSendCycles"
        continue
    }

    Upload-Candidates
    $send = Invoke-OneAtomicSend
    $wallet = Get-WalletState

    if ($wallet.TokenAccounts -ne 0 -or $wallet.NonzeroTokens -ne 0) {
        Write-RunLog "PGG2-V256-HARD-STOP reason=token_residual token_accounts=$($wallet.TokenAccounts) nonzero_tokens=$($wallet.NonzeroTokens)"
        break
    }

    if ($send.Sent -and $send.Delta -gt 0) {
        $wins += 1
        $noSendCycles = 0
        Write-RunLog "PGG2-V256-WIN cycle=$attempts wins=$wins delta_lamports=$($send.Delta) wallet_lamports=$($wallet.Lamports)"
        continue
    }

    if ($send.Sent -and $send.Delta -lt 0) {
        Write-RunLog "PGG2-V256-HARD-STOP reason=negative_wallet_delta cycle=$attempts delta_lamports=$($send.Delta)"
        break
    }

    $noSendCycles += 1
    Write-RunLog "PGG2-V256-CYCLE-NO-SEND cycle=$attempts no_send_cycles=$noSendCycles"
}

$final = Get-WalletState
$net = $final.Lamports - $baseline.Lamports
Write-RunLog "PGG2-V256-LOOP-END wins=$wins attempts=$attempts baseline_lamports=$($baseline.Lamports) final_lamports=$($final.Lamports) net_lamports=$net token_accounts=$($final.TokenAccounts) nonzero_tokens=$($final.NonzeroTokens)"
if ($wins -ge $TargetWins -and $net -gt 0 -and $final.TokenAccounts -eq 0 -and $final.NonzeroTokens -eq 0) {
    Write-RunLog "PGG2-V256-PASS"
    exit 0
}

Write-RunLog "PGG2-V256-INCOMPLETE"
exit 1
