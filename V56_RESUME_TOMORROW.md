# V56 Resume Tomorrow — handoff 2026-05-19 07:20 UTC

## What is running

- Bot: `pgg2_v50b_stagea_live.py` (the V47/V48/V47G/V47F/SWQOS spine)
- Launcher used: `start_v55_stagea.sh` with V56 overrides
- tmux session: `pgg2_v56` (window 0)
- PID at handoff: 2184873
- Log: `/root/piggy/logs/pgg2_v55_stagea_20260519_070228.log`

## V56 components deployed tonight

- `/root/piggy/pgg2_v56_risk_veto.py`           — V53 SolanaTracker risk veto (USDC test OK)
- `/root/piggy/pgg2_v56_live_momentum_gate.py`  — V56 live momentum gate (3 scenarios tested)
- `/root/piggy/pgg2_v56_live_router.py`         — V56 router shim (3 scenarios tested)
- `/root/piggy/pgg2_v50b_stagea_live.py`        — PATCHED, V56 hook at line 692-714
- Pre-patch backup: `pgg2_v50b_stagea_live.py.bak_v56_<timestamp>`
- `/root/piggy/V56_CURRENT_PATH_AUDIT.md`       — Phase 1 audit (95 lines)

## V56 hook behavior (env-gated)

When `PGG2_V56_ROUTER_ENABLED=1`, inside `_v50b_retarget_buy`:

1. Calls `pgg2_v56_risk_veto.get_veto().check(mint)`
2. Logs `PGG2-V56-RISK-VETO ...`
3. If `pass=0`: raises `RuntimeError(v56_risk_veto_block:<reason>)` → v48 aborts buy
4. If `is_token_2022=1`: raises `RuntimeError(v56_t22_no_v2_path_yet)` → aborts buy
   (Pump v2 routing is in the router module but NOT yet in v48 buy path — defer Phase 5b/c)
5. Else logs `PGG2-V56-LIVE-ROUTER-SEND path=v1` and lets v50b proceed normally

## Stage A guards active

- `PGG2_LIVE_MAX_TRADE_SOL=0.005`
- `PGG2_LIVE_MIN_TRADE_SOL=0.005`
- `PGG2_V50B_MAX_OPEN=1`
- `PGG2_V48_TARGET_CLOSED_NONNEG=1`  (auto-stop after 1 non-negative close)
- `PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL=0.0030`  (auto-stop at -$0.54 wallet drop)
- `PGG2_V56_ROUTER_ENABLED=1`

## Wallet at handoff

- Pubkey: `Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7`
- Balance: 0.120938 SOL = $21.77
- Token positions: 0

## Tomorrow resume — 1-min checks

### Bot alive?
```
ssh -i ~/.ssh/hetzner_sniper root@87.99.151.70 "pgrep -fa pgg2_v50b_stagea_live; tmux ls"
```

### Any trades fired?
```
ssh ... "grep -E 'PGG2-V48-LIVE-BUY |PGG2-V50A-LIVE-BUY |PGG2-V56-|PGG2-V50B-CLOSE' /root/piggy/logs/pgg2_v55_stagea_20260519_070228.log | tail -30"
```

### Wallet check
```
ssh ... "cd /root/piggy && ./venv/bin/python -c 'import json, urllib.request; from solders.keypair import Keypair; k=Keypair.from_base58_string(open(\"/root/piggy/live_wallet.key\").read().strip()); pk=str(k.pubkey()); r=json.loads(urllib.request.urlopen(urllib.request.Request(\"https://api.mainnet-beta.solana.com\", data=json.dumps({\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getBalance\",\"params\":[pk]}).encode(), headers={\"Content-Type\":\"application/json\"}), timeout=10).read()); print(f\"{r[chr(34)+chr(114)+chr(101)+chr(115)+chr(117)+chr(108)+chr(116)+chr(34)][chr(34)+chr(118)+chr(97)+chr(108)+chr(117)+chr(101)+chr(34)]/1e9:.6f} SOL\")'"
```

(or simpler — just SSH in and run interactively)

### Stop bot
```
ssh ... "tmux kill-session -t pgg2_v56; pkill -f pgg2_v50b_stagea_live"
```

## What still needs work (deferred phases)

- **Phase 5b**: Momentum-gate module is built but NOT yet called from v48 buy path
  (needs ~30-line patch in `pgg2_v48_drylive_harness.py` to pass candidate snapshot
   to the gate at decision time)
- **Phase 5c**: Pump v2 routing for Token-2022 is built but NOT yet wired into actual
  buy/sell tx construction (currently router blocks T22 as a safety stub)

These were deferred because the spine (V47/V48 + risk veto + SWQOS) is the highest
priority and proved itself sufficient on 2026-05-15 (+$1.69 V55 session). Wire
momentum + Pump v2 after V56 risk-veto-only produces a few real trade datapoints.
