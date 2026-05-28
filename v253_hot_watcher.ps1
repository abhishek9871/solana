param(
    [int]$MaxCycles = 120,
    [int]$TargetPositive = 10,
    [int]$MinRawEdgeLamports = 10000,
    [int]$ScanLimit = 80,
    [int]$SleepSeconds = 8,
    [string]$SizesSol = "0.00005,0.00006,0.00007,0.00008,0.00009,0.00010,0.00011,0.00012,0.00013,0.00014,0.00015,0.00016,0.00018,0.00020,0.00025,0.00030,0.00040,0.00050,0.00075,0.001,0.0015,0.002,0.003,0.005,0.0075,0.01,0.015,0.02,0.03,0.04,0.05",
    [string]$Workspace = "C:\Users\VASU\Desktop\tradingMahadevjiwin"
)

$ErrorActionPreference = "Continue"

$Key = Join-Path $env:USERPROFILE ".ssh\hetzner_sniper"
$Remote = "root@87.99.151.70"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Log = Join-Path $Workspace ("v253_hot_watcher_" + $Stamp + ".log")
$LocalCandidates = Join-Path $Workspace "_v223_v246_broad.jsonl"
$RemoteCandidates = "/root/piggy/data/v223_v246_broad.jsonl"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $line | Tee-Object -FilePath $Log -Append | Out-Host
}

function Invoke-Logged {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Log "=== $Label START ==="
    $output = @()
    try {
        $output = & $Command 2>&1
        foreach ($line in $output) {
            $line | Tee-Object -FilePath $Log -Append | Out-Host
        }
        $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
        Write-Log "=== $Label END exit=$code ==="
        return [pscustomobject]@{ Code = $code; Output = $output }
    } catch {
        Write-Log "=== $Label EXCEPTION $($_.Exception.Message) ==="
        return [pscustomobject]@{ Code = 999; Output = @($_.Exception.Message) }
    }
}

function Invoke-Remote {
    param([string]$Script)
    ssh -i $Key -o LogLevel=ERROR $Remote $Script
}

function Get-LocalCandidateStats {
    if (-not (Test-Path -LiteralPath $LocalCandidates)) {
        return [pscustomobject]@{ Rows = 0; Mints = 0; BestEdge = -1; BestMint = "-" }
    }
    $rows = 0
    $bestEdge = -1
    $bestMint = "-"
    $mints = @{}
    Get-Content -LiteralPath $LocalCandidates -ReadCount 1000 | ForEach-Object {
        foreach ($line in $_) {
            if (-not $line.Trim()) { continue }
            try {
                $obj = $line | ConvertFrom-Json
                $rows += 1
                $mint = [string]$obj.mint
                if ($mint) { $mints[$mint] = $true }
                $edge = [int64]$obj.edge_lamports
                if ($edge -gt $bestEdge) {
                    $bestEdge = $edge
                    $bestMint = if ($mint.Length -gt 8) { $mint.Substring(0, 8) } else { $mint }
                }
            } catch {
                continue
            }
        }
    }
    return [pscustomobject]@{
        Rows = $rows
        Mints = $mints.Count
        BestEdge = $bestEdge
        BestMint = $bestMint
    }
}

function Assert-No-Other-Runners {
    $patterns = @(
        "*v246_overnight_runner.ps1*",
        "*v223_gpa_multipool_eval.py*",
        "*v245_fast_single_tx_oracle.py*",
        "*v252_sender_atomic_test.py*",
        "*v253_hot_watcher.ps1*"
    )
    $others = Get-CimInstance Win32_Process | Where-Object {
        $cmd = $_.CommandLine
        $cmd -and $_.ProcessId -ne $PID -and ($patterns | Where-Object { $cmd -like $_ })
    }
    if ($others) {
        $others | Select-Object ProcessId, Name, CommandLine | Format-Table -Wrap | Out-String | ForEach-Object {
            Write-Log $_
        }
        throw "local_duplicate_runner_detected"
    }
    $remoteOut = Invoke-Remote "ps aux | grep -E 'python.*(pgg2|v22|v24|v245|v246|v252|v253|raptor)' | grep -v grep || echo NO_BOT_PROCS"
    foreach ($line in $remoteOut) { Write-Log $line }
    if (($remoteOut -join "`n") -notmatch "NO_BOT_PROCS") {
        throw "remote_duplicate_runner_detected"
    }
}

function Assert-Wallet-Clean {
    $walletOut = Invoke-Remote "cd /root/piggy && /root/piggy/venv/bin/python -u v246_wallet_check.py"
    foreach ($line in $walletOut) { Write-Log $line }
    $text = $walletOut -join "`n"
    if ($text -match "nonzero_tokens=([1-9][0-9]*)") {
        throw "remote_nonzero_token_account_detected"
    }
    return $text
}

Write-Log "PGG2-V253-HOT-WATCHER-START log=$Log"
Write-Log "PGG2-V253-MODE v245_pumpswap_atomic_single_tx sender_swqos=1 exact_wallet_delta_required=1 fail_closed=1"
Write-Log "PGG2-V253-CONFIG max_cycles=$MaxCycles target_positive=$TargetPositive min_raw_edge=$MinRawEdgeLamports scan_limit=$ScanLimit sleep_seconds=$SleepSeconds"
Write-Log "PGG2-V253-SIZES sizes_sol=$SizesSol"

$positive = 0
$zero = 0
$noExact = 0
$safeSkips = 0
$negative = 0

try {
    Assert-No-Other-Runners
    Assert-Wallet-Clean | Out-Null
} catch {
    Write-Log "PGG2-V253-PREFLIGHT-HARD-STOP reason=$($_.Exception.Message)"
    exit 10
}

for ($cycle = 1; $cycle -le $MaxCycles; $cycle++) {
    Write-Log "PGG2-V253-CYCLE-START cycle=$cycle positive=$positive target=$TargetPositive"
    try {
        Assert-Wallet-Clean | Out-Null
    } catch {
        Write-Log "PGG2-V253-HARD-STOP reason=$($_.Exception.Message)"
        exit 11
    }

    if (Test-Path -LiteralPath $LocalCandidates) {
        Remove-Item -LiteralPath $LocalCandidates -Force -ErrorAction SilentlyContinue
    }

    $scanScript = @'
set -e
cd ~/pgg2-local
source ~/pgg2-local-venv/bin/activate
rm -f data/v223_v246_broad.jsonl data/v253_scan_last.log
timeout 170s python -u v223_gpa_multipool_eval.py \
  --max-mints 1000 \
  --sizes-sol '__SIZES_SOL__' \
  --fee-buffer-lamports 0 \
  --projection-buffer-lamports 0 \
  --min-edge-lamports 1 \
  --min-quote-reserve-lamports 0 \
  --out-jsonl /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl \
  > data/v253_scan_last.log 2>&1 || true
tail -5 data/v253_scan_last.log || true
if [ -s /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl ]; then
  cp /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl /mnt/c/Users/VASU/Desktop/tradingMahadevjiwin/_v223_v246_broad.jsonl
  printf 'PGG2-V253-SCAN-CANDIDATES '
  wc -l /home/vasurajput1996/pgg2-local/data/v223_v246_broad.jsonl
else
  echo 'PGG2-V253-SCAN-NO-CANDIDATES'
fi
'@
    $scanScript = $scanScript.Replace("__SIZES_SOL__", $SizesSol)

    $scan = Invoke-Logged "cycle-$cycle-scan" {
        wsl -d Ubuntu-24.04 -- bash -lc $scanScript
    }
    $stats = Get-LocalCandidateStats
    Write-Log "PGG2-V253-SCAN-STATS cycle=$cycle rows=$($stats.Rows) mints=$($stats.Mints) best_edge=$($stats.BestEdge) best_mint=$($stats.BestMint)"
    if ($stats.Rows -le 0) {
        $safeSkips += 1
        Write-Log "PGG2-V253-CYCLE-SKIP cycle=$cycle reason=no_candidates"
        Start-Sleep -Seconds $SleepSeconds
        continue
    }
    if ($stats.BestEdge -lt $MinRawEdgeLamports) {
        $safeSkips += 1
        Write-Log "PGG2-V253-CYCLE-SKIP cycle=$cycle reason=best_raw_edge_below_min best_edge=$($stats.BestEdge) min_raw=$MinRawEdgeLamports"
        Start-Sleep -Seconds $SleepSeconds
        continue
    }

    $upload = Invoke-Logged "cycle-$cycle-upload" {
        scp -i $Key -q $LocalCandidates "${Remote}:${RemoteCandidates}"
    }
    if ($upload.Code -ne 0) {
        $safeSkips += 1
        Write-Log "PGG2-V253-CYCLE-SKIP cycle=$cycle reason=upload_failed code=$($upload.Code)"
        Start-Sleep -Seconds $SleepSeconds
        continue
    }

    $remoteRun = "cd /root/piggy && V252_SCAN_LIMIT=$ScanLimit V252_MIN_RAW_EDGE_LAMPORTS=$MinRawEdgeLamports timeout 115s /root/piggy/venv/bin/python -u v252_sender_atomic_test.py; rc=`$?; /root/piggy/venv/bin/python -u v246_wallet_check.py; exit `$rc"
    $run = Invoke-Logged "cycle-$cycle-v252-exact-positive-send" {
        Invoke-Remote $remoteRun
    }
    $runText = $run.Output -join "`n"
    if ($runText -match "PGG2-V252-SENDER-FINAL-WALLET .*delta=(-?[0-9]+)") {
        $delta = [int64]$Matches[1]
        if ($delta -gt 0) {
            $positive += 1
            Write-Log "PGG2-V253-CYCLE-POSITIVE cycle=$cycle delta=$delta positives=$positive"
        } elseif ($delta -eq 0) {
            $zero += 1
            Write-Log "PGG2-V253-CYCLE-ZERO cycle=$cycle delta=0 zero_count=$zero"
        } else {
            $negative += 1
            Write-Log "PGG2-V253-HARD-STOP cycle=$cycle reason=negative_wallet_delta delta=$delta"
            exit 12
        }
    } elseif ($runText -match "PGG2-V252-SENDER-NO-EXACT-POSITIVE") {
        $noExact += 1
        Write-Log "PGG2-V253-CYCLE-NO-EXACT-POSITIVE cycle=$cycle no_exact=$noExact"
    } else {
        $safeSkips += 1
        Write-Log "PGG2-V253-CYCLE-NO-FINAL-DELTA cycle=$cycle code=$($run.Code)"
    }

    if ($runText -match "nonzero_tokens=([1-9][0-9]*)") {
        Write-Log "PGG2-V253-HARD-STOP cycle=$cycle reason=nonzero_tokens_after_run"
        exit 13
    }
    if ($runText -match "PGG2-V252-SENDER-HARD-FAIL") {
        Write-Log "PGG2-V253-HARD-STOP cycle=$cycle reason=v252_hard_fail"
        exit 14
    }
    if ($positive -ge $TargetPositive) {
        Write-Log "PGG2-V253-TARGET-REACHED positives=$positive target=$TargetPositive"
        break
    }
    Start-Sleep -Seconds $SleepSeconds
}

$finalWallet = Assert-Wallet-Clean
Write-Log "PGG2-V253-FINAL positive=$positive zero=$zero no_exact=$noExact safe_skips=$safeSkips negative=$negative"
Write-Log "PGG2-V253-FINAL-WALLET $finalWallet"
Write-Log "PGG2-V253-HOT-WATCHER-END log=$Log"
exit 0
