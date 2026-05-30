# V287 One-Entry Smoke Runbook

This is the frozen live-smoke path for the V287 selected-band runner. It is meant for the instruction: "pull the code and run one entry smoke."

## Frozen Files

- `pgg2_v287_selected_band_live_smoke.py`
- `_launch_v287_oneentry_smoke.sh`
- `pgg2_v74_sender_adapter.py`
- `pgg2_v75_sender_tx_builder.py`
- `pgg2_v285_grpc_buy_train_continuation_no_send.py`
- `pgg2_v129_sof_stagea_live_bundle.py`
- `pgg2_v129_sof_no_send_bundle_validation.py`
- `yellowstone_proto/geyser_pb2.py`
- `yellowstone_proto/geyser_pb2_grpc.py`
- `yellowstone_proto/solana_storage_pb2.py`
- `yellowstone_proto/solana_storage_pb2_grpc.py`

The runner hash frozen from the proven Hetzner copy is:

```text
610fd5ab69482c1adc70ebda0d0632583d0a3e8dec165aabc19511bdf49f880a  pgg2_v287_selected_band_live_smoke.py
```

## What This Runner Does

V287 listens to the PublicNode Yellowstone feed, waits for the selected-band setup, sends one guarded pump.fun buy through Helius Sender SWQOS, then exits with a protected sell. It is a one-entry smoke runner, not a general always-on bot.

Do not switch this smoke to old score gates, old ESB, PumpPortal execution, Jupiter fallback, protected-hold, or unrelated runner files.

## Required Hetzner State

Expected directory:

```bash
cd /root/piggy
```

Required local-only files that are not committed:

- `/root/piggy/.env`
- `/root/piggy/live_wallet.key`
- `/root/piggy/venv`

Required `.env` variables are loaded by the launcher without printing them. At minimum the environment must provide the live RPC/feed credentials already used by this runner, including `HELIUS_API_KEY` and `PUBLICNODE_X_TOKEN`.

## Restore And Run

Use this exact sequence:

```bash
cd /root/piggy
git fetch origin
git checkout main
git pull --ff-only origin main
/root/piggy/venv/bin/python -m py_compile pgg2_v287_selected_band_live_smoke.py
bash -n _launch_v287_oneentry_smoke.sh
./_launch_v287_oneentry_smoke.sh
```

The launcher writes a log under `logs/v287_oneentry_smoke_YYYYmmdd_HHMMSS.log`.

To change only the timeout for a smoke, set:

```bash
V287_SMOKE_SECONDS=600 ./_launch_v287_oneentry_smoke.sh
```

Do not edit the launcher just to change the smoke duration.

## Preflight Checks

Before running, verify no duplicate bot is active:

```bash
ps aux | grep -E "pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|tmux.*v287" | grep -v grep || true
```

Check wallet and tokens:

```bash
solana balance Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7
spl-token accounts --owner Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7
```

If a nonzero token account exists, do not start another smoke until it is reconciled.

## Success Criteria

For one-entry smoke success, the log should show:

- one live buy sent and confirmed;
- one protected sell confirmed;
- `SMOKE-END` wallet delta positive;
- `nonzero_tokens=0`;
- no traceback;
- no stuck token;
- the process exits after the close.

## Stop Criteria

Stop and inspect before another run if any of these occur:

- buy fails and fee burn is nontrivial;
- sell fails or leaves residual tokens;
- wallet delta is negative;
- token residual is nonzero;
- traceback appears;
- duplicate process warning appears;
- PublicNode or Sender auth fails.

## Proven Evidence Before Freeze

Two recent one-entry live smokes closed cleanly:

```text
logs/v287_instantdense_3m_20260530_094742.log
mint=522f..pump
wallet_before=0.285708669 SOL
wallet_after=0.300815150 SOL
delta=+0.015106481 SOL
buy_sent=1 buy_confirmed=1 protected_sell_confirmed=1 failed_buys=0 token_residual=0

logs/v287_repeat_oneentry_3m_20260530_095632.log
mint=3Dok..pump
wallet_before=0.300815150 SOL
wallet_after=0.302643528 SOL
delta=+0.001828378 SOL
buy_sent=1 buy_confirmed=1 protected_sell_confirmed=1 failed_buys=0 token_residual=0
```

Combined result from those two smokes:

```text
2/2 clean closes
total_delta=+0.016934859 SOL
final_wallet=0.302643528 SOL
token_accounts_empty=true
```

## Operator Notes

- Keep `PGG2_V75_TIP_LAMPORTS=5000`; the runner rejects other tip values.
- The launcher sources `.env` but does not echo secrets.
- The launcher blocks if another V287 bot or tmux process is already running.
- The committed code is the restorable baseline. For experiments, branch first.
