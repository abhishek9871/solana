# Goal5 Frozen Live Smoke Manual

This file is the checkpoint for the current working Goal5 live lane. Read this before touching code or running live.

## Frozen Files

Commit and deploy these files together:

- `pgg2_goal5_speed_scout.py` - the live smoke runner.
- `pgg2_goal5_standalone.py` - feed, wallet, broker, quote, sell, and verification helpers.
- `pgg2_direct_pump.py` - direct pump buy/sell execution helper.
- `pgg2_live_raptor.py` - RPC/keypair/send broker used by the direct pump helper.
- `birth_first_sniper.py` - shared config/env/log/pubkey helpers used by the broker stack.
- `pgg2_v74_sender_adapter.py` - optional Helius Sender adapter if the env enables it.
- `pgg2_v75_sender_tx_builder.py` - optional Sender tip builder if the env enables Sender.
- `v246_wallet_check.py` - wallet/token verification.
- `yellowstone_proto/` - generated PublicNode Yellowstone gRPC protobuf files.

Do not use the old V287/V288 engine for this lane. Do not add broad gates or mint-specific patches before running the frozen smoke.

## What Worked

The current frozen config moved away from the oversized V287 authority file and uses a small latency-first Goal5 runner. The main working behavior is:

- PublicNode Yellowstone feed observes pump.fun flow.
- The runner only sends on narrow Goal5 lane shapes.
- Current best repeated lane: `small005_c3_f13_q720`.
- The latest strong win came from `cur1_q600_clean_follow`.
- After `goal5_c3hightotal_live_smoke30_20260609_142931.log`, `cur1_q600_clean_follow` now requires `follow/current >= 0.38` so weak-follow upper-cur1 rows like `3c6T..pump` do not fire.
- After `goal5_c3hightotal_live_smoke60_20260609_151801.log`, `cur1_q600_clean_follow` also requires `train_span_ms <= 25` so delayed-follow cur1 rows like `2Hur..pump` do not fire. This does not touch `small005_c3_f13_q720`.
- The same 60-minute log showed the frequency miss was C3-adjacent, not cur1: `small005_c3_f13_q720` had zero exact rows, while 18 fast C3 strong-follow rows across 12 mints had `current=3.00..3.40`, `first_follow=1.45..2.20`, `follow_buys=1..3`, and `train_span_ms<=25`. These now use the separate `small005_c3_strong_fast_follow` lane and must pass the existing C3 postquote tape before sending.
- After `goal5_c3strongfast_live_smoke10_20260609_161800.log`, C3 rows around `761k` quote-ref were shown to be overblocked by the old `760k` cap, so C3 auth now allows up to `765k`. The same run also proved the C3 fast path can hit Pump `6042` buy slippage after tape; C3 buys now use `--c3-buy-slippage-pct 16.0` while other lanes still use `--buy-slippage-pct 8.0`.
- After `goal5_batch1_smoke2_20260609_165100.log`, `c3_tape_mid_clean_ok` was proven unsafe: `9PrU..pump` had only `2.574 SOL` hidden continuation with `2` large buys and immediately opened with negative sell headroom, closing at `-1,087,532` lamports. C3 mid-clean tape is now disabled by default; C3 sends require the stronger `c3_tape_ok` or `c3_tape_high_total_low_dust_ok` paths.
- Size is `0.005 SOL`.
- The run stops after one completed close by default.
- Sell requires positive headroom and closes the token account.
- `pgg2_direct_pump.py` refreshes the pump curve before snapshot buy and blocks stale min-token buys when the fresh quote cannot satisfy the min.

Recent live evidence on this frozen command:

- `goal5_c3hightotal_live_smoke5_20260609_125620.log`: clean win, `+386021` lamports, no token residual.
- `goal5_repeat_c3hightotal_live_smoke5_20260609_130241.log`: clean win, `+389064` lamports, no token residual.
- `goal5_repeat2_c3hightotal_live_smoke5_20260609_130609.log`: clean no-trade timeout, no spend.
- `goal5_c3hightotal_live_smoke20_20260609_131523.log`: clean win, `+387544` lamports, no token residual.
- `goal5_c3hightotal_live_smoke20_20260609_133248.log`: clean no-trade timeout, no spend.
- `goal5_c3hightotal_live_smoke60_20260609_135558.log`: clean win, `+2246967` lamports, no token residual.
- `goal5_c3hightotal_live_smoke30_20260609_142931.log`: clean close but loss, `-132843` lamports, no token residual; root cause was weak-follow `cur1_q600_clean_follow` with follow/current about `0.24`.
- `goal5_c3hightotal_live_smoke60_20260609_151801.log`: clean close but loss, `-153459` lamports, no token residual; root cause was delayed-follow `cur1_q600_clean_follow` with `train_span_ms=220`.

Current frozen record after the cur1 weak-follow and delayed-follow patches: 4 clean wins, 2 clean no-trade timeouts, 2 clean closed losses, 0 failed-buy fee burns, 0 stuck tokens.

## Exact Live Command

Run from `/root/piggy` on Hetzner. This is a max-duration smoke; it stops early after one close.

```bash
runid=$(date +%Y%m%d_%H%M%S)
log=logs/goal5_c3hightotal_live_smoke60_${runid}.log
echo "$log" > current_goal5_smoke_log.txt
echo "$runid" > goal5_speed_scout_latest_runid.txt
setsid /root/piggy/venv/bin/python -u pgg2_goal5_speed_scout.py \
  --live \
  --seconds 3600 \
  --size-sol 0.005 \
  --target-closes 1 \
  --no-proven-strong-enabled \
  --no-micro-c0-highquote-enabled \
  --cur1-q600-clean-follow-enabled \
  --no-small-size005-cur1-q900-follow-enabled \
  --no-small-size005-c0-f22-multi-q900-enabled \
  --small-size005-c3-f13-q720-enabled \
  --small-size005-c3-strong-fast-follow-enabled \
  --no-clean-buy-train-continuation-enabled \
  --no-scratch-midquote-enabled \
  --no-early-cur1-q800-enabled \
  --no-final-projection-check-enabled \
  --buy-slippage-pct 8.0 \
  --c3-buy-slippage-pct 16.0 \
  --no-c3-mid-clean-tape-enabled \
  --sell-min-headroom-lamports 150000 \
  --loss-rescue-headroom-lamports -250000 \
  --max-hold-ms 2800 \
  --max-send-start-age-ms 420 \
  --max-prequote-start-age-ms 250 \
  > "$log" 2>&1 < /dev/null &
echo $! > goal5_speed_scout_latest_pid.txt
```

For a 20-minute smoke, change only `--seconds 3600` to `--seconds 1200`.

For a 5-minute smoke, change only `--seconds 3600` to `--seconds 300`.

## Required Pre-Run Checks

Do these before every live smoke:

```bash
cd /root/piggy
ps aux | grep -E "pgg2_goal5|goal5_speed|python.*goal5|pgg2_v287|python.*v287" | grep -v grep || echo no_bot_process
/root/piggy/venv/bin/python v246_wallet_check.py
/root/piggy/venv/bin/python -m py_compile pgg2_goal5_speed_scout.py pgg2_goal5_standalone.py pgg2_direct_pump.py
```

Expected clean state:

- No bot process.
- Wallet has SOL.
- `token_accounts=0`.
- `nonzero_tokens=0`.
- Compile exits with no output.

## Monitoring Command

```bash
cd /root/piggy
log=$(cat current_goal5_smoke_log.txt)
echo LOG=$log
printf "preauth_pass0="; grep -c "PGG2-GOAL5-SCOUT-PREAUTH.*pass=0" "$log" || true
printf "auth_pass0="; grep -c "PGG2-GOAL5-SCOUT-AUTH.*pass=0" "$log" || true
printf "auth_pass1="; grep -c "PGG2-GOAL5-SCOUT-AUTH.*pass=1" "$log" || true
printf "buy="; grep -c "PGG2-GOAL5-SCOUT-BUY" "$log" || true
printf "sell="; grep -c "PGG2-GOAL5-SCOUT-SELL" "$log" || true
printf "buy_failed="; grep -c "PGG2-GOAL5-SCOUT-BUY-FAILED" "$log" || true
grep -E "PGG2-GOAL5-(SCOUT-(AUTH|BUY|SELL|BUY-FAILED|FINAL)|C3-POSTQUOTE-TAPE)|PGG2-DIRECT-SNAPSHOT-BUY-CURVE-REFRESH" "$log" | tail -160 || true
/root/piggy/venv/bin/python v246_wallet_check.py
```

## Final Verification

After the process exits:

```bash
cd /root/piggy
log=$(cat current_goal5_smoke_log.txt)
grep -E "PGG2-GOAL5-(SCOUT-(AUTH|BUY|SELL|BUY-FAILED|FINAL)|C3-POSTQUOTE-TAPE)|PGG2-DIRECT-SNAPSHOT-BUY-CURVE-REFRESH" "$log" | tail -220 || true
ps aux | grep -E "pgg2_goal5|goal5_speed|python.*goal5" | grep -v grep || echo no_goal5_process
/root/piggy/venv/bin/python v246_wallet_check.py
```

A clean outcome is one of:

- `closed=1 win=1`, wallet increased, token accounts `0`.
- `timeout=1`, wallet unchanged, token accounts `0`.

Bad outcomes to stop and investigate:

- `PGG2-GOAL5-SCOUT-BUY-FAILED`.
- Wallet delta negative.
- `token_accounts` nonzero after final verification.
- Any live V287/V288 process running.

## Deployment From Local Checkout

If the remote is stale, copy only the frozen files:

```bash
scp -i "$USERPROFILE/.ssh/hetzner_sniper" pgg2_goal5_speed_scout.py pgg2_goal5_standalone.py pgg2_direct_pump.py pgg2_live_raptor.py birth_first_sniper.py pgg2_v74_sender_adapter.py pgg2_v75_sender_tx_builder.py v246_wallet_check.py GOAL5_LIVE_RUN_MANUAL.md root@87.99.151.70:/root/piggy/
scp -i "$USERPROFILE/.ssh/hetzner_sniper" -r yellowstone_proto root@87.99.151.70:/root/piggy/
ssh -i "$USERPROFILE/.ssh/hetzner_sniper" root@87.99.151.70 "cd /root/piggy && /root/piggy/venv/bin/python -m py_compile pgg2_goal5_speed_scout.py pgg2_goal5_standalone.py pgg2_direct_pump.py pgg2_live_raptor.py birth_first_sniper.py pgg2_v74_sender_adapter.py pgg2_v75_sender_tx_builder.py v246_wallet_check.py"
```

On Windows PowerShell, prefer this SSH key path:

```powershell
$env:USERPROFILE\.ssh\hetzner_sniper
```

## Future-Agent Rules

- Run the frozen command first before changing code.
- Do not widen the live send authority just because a smoke times out.
- Do not revive the giant V287/V288 authority files for this Goal5 lane.
- Do not commit logs, wallet files, `.env`, keypairs, or random analysis artifacts.
- If code must change, keep it in the three frozen files and rerun `py_compile` plus a short smoke.
- Preserve the safety properties: no failed-buy fee burns, no stuck token accounts, no negative close.
