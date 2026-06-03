# WIN287 Live Smoke Manual

This folder freezes the exact V287 live-smoke bundle pulled from Hetzner `/root/piggy` after the clean `Be7m..pump` win on June 3, 2026.

The goal of this bundle is reproducibility: a future agent can copy this folder to Hetzner, run the same one-entry V287 smoke, and verify the same safety conditions without guessing which files matter.

## What This Is

WIN287 is the selected seed-prior carry live-smoke lane:

- Main runner: `pgg2_v287_selected_band_live_smoke.py`
- Launcher: `_launch_v287_oneentry_smoke.sh`
- Replay/audit helper: `v287_authority_replay.py`
- Geyser protobufs: `yellowstone_proto/`
- Sender/SWQOS helpers and local dependency closure are included in this folder.

This is not the old V102/V107 directional path, not Kamino, and not a raw-feed-only bundle path. It is the patched V287 fast lane that produced clean protected sells with no residual tokens.

## Files Included

Root files:

- `_launch_v287_oneentry_smoke.sh`
- `pgg2_v287_selected_band_live_smoke.py`
- `v287_authority_replay.py`
- `birth_first_sniper.py`
- `pgg2_direct_pump.py`
- `pgg2_live_raptor.py`
- `pgg2_v74_sender_adapter.py`
- `pgg2_v75_sender_tx_builder.py`
- `pgg2_v285_grpc_buy_train_continuation_no_send.py`
- `pgg2_v129_sof_stagea_live_bundle.py`
- `pgg2_v108_bundle_builder.py`
- `pgg2_v108_bundle_profit_model.py`
- `pgg2_v108_external_tx_decoder.py`
- `pgg2_v108_jito_bundle_sender.py`
- `pgg2_v109_no_send_live_bundle_validation.py`
- `pgg2_v129_sof_no_send_bundle_validation.py`

Generated protobuf files:

- `yellowstone_proto/geyser_pb2.py`
- `yellowstone_proto/geyser_pb2_grpc.py`
- `yellowstone_proto/solana_storage_pb2.py`
- `yellowstone_proto/solana_storage_pb2_grpc.py`
- `yellowstone_proto/geyser.proto`
- `yellowstone_proto/solana-storage.proto`

Integrity hashes are in `WIN287_FILE_MANIFEST.sha256`.

## What Is Not Included

Do not put these in git or in this folder:

- `/root/piggy/.env`
- `/root/piggy/live_wallet.key`
- private API keys
- wallet seed/key material
- live logs unless explicitly needed for an investigation

The launcher expects `.env`, `live_wallet.key`, and the existing `/root/piggy/venv` to already exist on Hetzner.

## Recent Evidence

Clean smoke #1:

- Run: `v287_oneentry_smoke_20260603_142018`
- Mint: `CiN8..pump`
- Result: `wallet_before=215899523 wallet_after=217657693`
- Delta: `+1758170` lamports
- Residuals: `nonzero_tokens=0 rent_locked_empty=0`

Clean smoke #2:

- Run: `v287_oneentry_smoke_20260603_143058`
- Mint: `Be7m..pump`
- Authority reason: `consumed_postplan_zero_drift_authorized`
- Buy: `PGG2-V287-BUY-SEND`
- Sell: `PGG2-V287-PROTECTED-SELL-CONFIRMED`
- Result: `wallet_before=217657693 wallet_after=220030128`
- Delta: `+2372435` lamports
- Residuals: `nonzero_tokens=0 rent_locked_empty=0`
- Failed-safe buys: none
- Process after run: no V287 process

Frequency is market-dependent. In the evidence above, one win happened in under 3 minutes and another after about 24 minutes. Do not force sends to satisfy a timer; the frozen lane is useful because it waits and only sends when its authority passes.

## One-Command Windows Run

From the local repo root:

```powershell
cd .\win287
.\win287_smoke_30m.bat
```

This deploys the frozen files to Hetzner, validates syntax, and starts one 30-minute one-entry smoke.

## Manual Deploy

From the local repo root:

```powershell
cd .\win287
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy_win287_to_hetzner.ps1
```

This copies only the frozen code/protobuf files. It does not copy `.env` or wallet material.

## Manual Run

After deploy:

```powershell
cd .\win287
powershell -NoProfile -ExecutionPolicy Bypass -File .\run_win287_smoke_30m.ps1 -Seconds 1800
```

The run script uses the non-tmux `nohup` pattern because earlier tmux use could trigger launcher preflight conflicts. It writes the active run id to `/root/piggy/v287_current_runid.txt`.

## Monitor

```powershell
cd .\win287
powershell -NoProfile -ExecutionPolicy Bypass -File .\monitor_win287.ps1
```

Watch for:

- `PGG2-V287-SEED-PRIOR-FINAL-SEND-AUTHORITY-PASS`
- `PGG2-V287-BUY-SEND`
- `PGG2-V287-PROTECTED-SELL-SEND`
- `PGG2-V287-PROTECTED-SELL-CONFIRMED`
- `PGG2-V287-SMOKE-END`

Bad events:

- `PGG2-V287-BUY-FAILED-SAFE`
- negative wallet delta in `PGG2-V287-SMOKE-END`
- `nonzero_tokens > 0`
- `rent_locked_empty > 0`
- V287 process still running after `SMOKE-END`

## Exact Remote Start Command

Use this only if you are operating directly over SSH:

```bash
cd /root/piggy
RUNID=v287_oneentry_smoke_$(date +%Y%m%d_%H%M%S)
echo "$RUNID" > v287_current_runid.txt
WRAP=logs/${RUNID}.wrapper.log
nohup env V287_SMOKE_SECONDS=1800 RUNID="$RUNID" ./_launch_v287_oneentry_smoke.sh > "$WRAP" 2>&1 < /dev/null &
pid=$!
echo "$pid" > v287_current_pid.txt
echo "STARTED_RUNID=$RUNID PID=$pid"
```

## Required Preflight

Before starting any smoke:

```bash
cd /root/piggy
ps aux | grep -E "pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|tmux.*v287" | grep -v grep || echo no_v287_process
bash -n _launch_v287_oneentry_smoke.sh
/root/piggy/venv/bin/python -m py_compile pgg2_v287_selected_band_live_smoke.py v287_authority_replay.py
```

If a V287 process is already running, do not start another one.

## Post-Run Verification

After a run closes or times out:

```bash
cd /root/piggy
RUNID=$(cat v287_current_runid.txt)
ps aux | grep -E "pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|_launch_v287" | grep -v grep || echo no_v287_process
grep "PGG2-V287-SMOKE-END" logs/${RUNID}.log | tail -5
grep "PGG2-V287-BUY-FAILED-SAFE" logs/${RUNID}.log | tail -5 || true
```

The minimum success bar is:

- `wallet_after > wallet_before`
- no `BUY-FAILED-SAFE`
- `nonzero_tokens=0`
- `rent_locked_empty=0`
- no V287 process remains

## Token Residual Scan

Use public RPC for a final lightweight token-account check:

```bash
cd /root/piggy
python3 - <<'PY'
import json, urllib.request
wallet='Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7'
rpc='https://api.mainnet-beta.solana.com'
programs={
  'Tokenkeg':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
  'Token-2022':'TokenzQdBNbLqP5VEhdkAS6EPFHLy6e93Vzyb6Jk7'
}
def call(method, params):
    data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode()
    req=urllib.request.Request(rpc, data=data, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)
print('wallet_balance_lamports', call('getBalance',[wallet])['result']['value'])
for name, program in programs.items():
    res=call('getTokenAccountsByOwner',[wallet, {'programId': program}, {'encoding':'jsonParsed'}])
    vals=res.get('result',{}).get('value',[])
    nonzero=[]; empty=[]
    for v in vals:
        info=v.get('account',{}).get('data',{}).get('parsed',{}).get('info',{})
        amt=int(info.get('tokenAmount',{}).get('amount','0'))
        lam=int(v.get('account',{}).get('lamports',0))
        if amt:
            nonzero.append((v.get('pubkey'), amt, lam, info.get('mint')))
        elif lam:
            empty.append((v.get('pubkey'), lam, info.get('mint')))
    print(name, 'accounts', len(vals), 'nonzero', len(nonzero), 'rent_locked_empty', len(empty))
PY
```

## Operating Rules For Future Agents

1. Start from this folder before touching V287 again.
2. Do not pivot to Kamino, V102/V107 directional, V108/V109 raw-feed-only, or Jupiter routes when the user asks for the frozen V287 smoke.
3. Do not modify code before a smoke unless there is a concrete, current failure.
4. Never run multiple V287 sessions at the same time.
5. Never copy or print `.env`.
6. Never copy or print `live_wallet.key`.
7. Treat success as wallet delta plus clean token scan plus clean process state, not just a sell log.
8. If the run times out with no trade, report the counts and do analysis; do not blindly loosen gates during the run.
