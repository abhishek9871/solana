# V287 Live Smoke Runbook

This runbook freezes the live path that produced the latest two clean V287
one-entry wins. It is for agents that need to restore and run the same live
smoke without rediscovering the lane or changing strategy.

## Scope

Use this path only for the V287 selected fast-lane live smoke:

- Runner: `pgg2_v287_selected_band_live_smoke.py`
- Launcher: `_launch_v287_oneentry_smoke.sh`
- Default trade size: `V287_SIZE_SOL=0.030`
- Sender tip: `PGG2_V75_TIP_LAMPORTS=5000`
- Transport: Helius Sender/SWQOS through the existing broker adapter
- Feed: PublicNode Yellowstone gRPC from the existing `.env`
- Stop condition: one buy/sell close or timeout

Do not use this runbook for Kamino, V102/V107 directional runners, V108/V109
atomic bundle work, PumpPortal, or unrelated old score-gate bots.

## Files Frozen In This Commit

Core live smoke files:

- `pgg2_v287_selected_band_live_smoke.py`
- `_launch_v287_oneentry_smoke.sh`
- `v246_wallet_check.py`

Direct dependencies imported by the runner:

- `birth_first_sniper.py`
- `pgg2_direct_pump.py`
- `pgg2_live_raptor.py`
- `pgg2_v74_sender_adapter.py`
- `pgg2_v75_sender_tx_builder.py`
- `pgg2_v285_grpc_buy_train_continuation_no_send.py`

Import-chain dependencies required for a clean import:

- `pgg2_v129_sof_stagea_live_bundle.py`
- `pgg2_v129_sof_no_send_bundle_validation.py`
- `pgg2_v108_bundle_builder.py`
- `pgg2_v108_bundle_profit_model.py`
- `pgg2_v108_external_tx_decoder.py`
- `pgg2_v108_jito_bundle_sender.py`
- `pgg2_v109_no_send_live_bundle_validation.py`

Yellowstone protobuf files:

- `yellowstone_proto/geyser_pb2.py`
- `yellowstone_proto/geyser_pb2_grpc.py`
- `yellowstone_proto/solana_storage_pb2.py`
- `yellowstone_proto/solana_storage_pb2_grpc.py`

Do not commit or print `.env`, `live_wallet.key`, API keys, or private key
material. Those stay on Hetzner.

## Winning Lane Details

The active V287 path is a selected-band fast-lane smoke. The important enabled
live reasons are:

- `selected_seed_prior_carry_rearm`
- `selected_seed_prior_single_strong_rearm`
- `selected_single_prior_strong_rearm`
- narrow `selected_fresh_single_mid_rearm`

Important protections preserved in the launcher and runner:

- Broad fresh actual remains disabled:
  `V287_SELECTED_FRESH_ACTUAL_ENABLED=0`
- Narrow fresh single-mid actual is enabled:
  `V287_SELECTED_FRESH_SINGLE_MID_ACTUAL_ENABLED=1`
- Fresh single-mid uses:
  `V287_SELECTED_SINGLE_MID_MIN_QUOTE_TOKENS=680000`
  and `V287_SELECTED_FRESH_SINGLE_MID_MIN_TOKEN_HEADROOM_PCT=5.00`
- Seed-prior max quote tokens:
  `V287_SELECTED_SEED_PRIOR_MAX_QUOTE_TOKENS=760000`
- Single-prior max quote tokens:
  `V287_SELECTED_SINGLE_PRIOR_MAX_QUOTE_TOKENS=650000`
- Selected continuation min-token guard mode is `floor`, not quote-relative
  slippage, for the repaired continuation lanes.
- Final refresh drift and headroom checks must pass before buy send.
- Sell uses protected min-SOL guard; no `min=0` sell is acceptable.

Latest live evidence before this freeze:

- `v287_fresh_single_mid_smoke_20260531_213615.log`
  closed `vKPf..pump` with `delta_lamports=+18409468`, zero token residual.
- `v287_10min_same_smoke_20260601_053551.log`
  closed `J5XP..pump` with `delta_lamports=+5861071`, zero token residual.

## Restore On Hetzner

On Hetzner:

```bash
cd /root/piggy
git fetch origin
git checkout main
git pull --ff-only origin main
```

The expected runtime files that are not in git must already exist on Hetzner:

- `/root/piggy/.env`
- `/root/piggy/live_wallet.key`
- `/root/piggy/venv/`

Before any live run, verify clean state:

```bash
cd /root/piggy
ps aux | grep -E "pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|tmux.*v287" | grep -v grep || echo no_bot_running
/root/piggy/venv/bin/python v246_wallet_check.py
```

Only proceed if there is no bot process and `token_accounts=0 nonzero_tokens=0`.

## Run One-Entry Live Smoke

For a 10-minute bounded smoke using the same path:

```bash
cd /root/piggy
RUNID="v287_manual_smoke_$(date +%Y%m%d_%H%M%S)" \
LOG="logs/${RUNID}.log" \
V287_SMOKE_SECONDS=600 \
./_launch_v287_oneentry_smoke.sh
```

The launcher stops after one close or timeout. It is normal for it to stop
before 10 minutes if one trade closes.

For a shorter 3-minute smoke, set:

```bash
V287_SMOKE_SECONDS=180
```

## Success Criteria

Do not call a run successful based only on a buy send, sell send, or sell
confirmation. A successful smoke requires all three:

1. `PGG2-V287-SMOKE-END` shows `delta_lamports` positive or at least
   non-negative.
2. `v246_wallet_check.py` reports `token_accounts=0 nonzero_tokens=0`.
3. No V287 bot or tmux process remains running.

Post-run verification:

```bash
cd /root/piggy
/root/piggy/venv/bin/python v246_wallet_check.py
ps aux | grep -E "pgg2_v287_selected_band_live_smoke.py|python.*pgg2_v287|tmux.*v287" | grep -v grep || echo no_bot_running
grep -E "PGG2-V287-BUY-SEND|PGG2-V287-PROTECTED-SELL-CONFIRMED|PGG2-V287-SMOKE-END|PGG2-V287-FINAL" logs/<RUNID>.log | tail -40
```

## If No Trade Fires

Do not loosen broad gates immediately. First inspect:

```bash
grep -E "REARM-PASS|selected_fresh_single_mid_rearm|selected_seed_prior|BUY-QUOTE-HEADROOM|MIN-TOKEN-GUARD|TOKEN-CAP|PREBUY|SMOKE-END|FINAL" logs/<RUNID>.log | tail -120
```

Common safe blockers:

- quote token cap block
- prebuy postbuy sell block
- headroom block
- no selected rearm before timeout

If no buy was sent, wallet should be unchanged except no material spend. Confirm
with `v246_wallet_check.py`.

## If A Buy Sends

Watch for:

- `PGG2-V287-BUY-SEND`
- `PGG2-V287-EARLY-TOKEN-BALANCE`
- `PGG2-V287-PROTECTED-SELL-SEND`
- `PGG2-V287-PROTECTED-SELL-CONFIRMED`
- `PGG2-V287-SMOKE-END`

If the run exits with nonzero tokens, do not start another bot. Rescue/close the
token first, then re-run `v246_wallet_check.py`.
