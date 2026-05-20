# RUN_LIVE — V65 Stage A live runbook

Canonical workflow to clone the repo → push to Hetzner → run live.

This runbook is for the **V65 control-plane stack** (V47C..V47I + V59 + V60 +
V61 entry gates, V62B sell router, V63 final PnL, V64 CandidatePassport).
Last validated 2026-05-20.

## 1. Prerequisites

- Hetzner Ashburn VPS: `87.99.151.70` (cpx11)
- SSH key: `~/.ssh/hetzner_sniper`
- Solana wallet keypair already on Hetzner (loaded by broker at startup)
- Helius RPC API key (used in launchers via `HELIUS_RPC_URL` env)
- Python venv on Hetzner at `/root/piggy/venv`
- Working directory on Hetzner: `/root/piggy/`

## 2. File inventory — what runs the bot

| File | Role |
|---|---|
| `pgg2_v48_drylive_harness.py` | Main harness — gates + buy/sell loop + V64 choke point |
| `pgg2_v50b_stagea_live.py` | Runner — wraps the harness with Stage A budgets |
| `pgg2_direct_pump.py` | Broker — Pump.fun buy/sell tx builders + RPC + SWQOS |
| `pgg2_v59_true_edge.py` | Slippage-calibrated true-edge model |
| `pgg2_v60_live_send_firewall.py` | Universal live-send firewall (size cap, route, snapshot freshness) |
| `pgg2_v61_live_continuation_oracle.py` | Post-V60 continuation oracle (250ms hold, fresh curve, peak detect) |
| `pgg2_v62b_authoritative_sell_router.py` | Pump_bc sell router — bank/scratch/max_hold/emergency + V65 duplicate-safe pre-retry |
| `pgg2_v63_post_sell_clean_close.py` | Mandatory CloseAccount safety net |
| `pgg2_v63_final_pnl.py` | Wallet-snapshot final PnL accounting |
| `pgg2_v64_candidate_passport.py` | Mint-keyed gate passport — no buy without final_pass |
| `pgg2_v47c_multi_buyer_gate.py` ...`pgg2_v47i_*.py` | V47 entry-gate stack |
| `pgg2_rpc_pool.py` | Multi-RPC pool (ST + Helius + Helius beta) |
| `jupiter_rescue.py` | Legacy Jupiter rescue (blocked for pump_bc in V62B mode) |
| `rescue_all_stuck.py` | Standalone tool — rescues nonzero token balances |
| `active_snipers.txt` | Whale-follow active-pool list (V68 lane) |
| `start_v55_stagea.sh` | Main env+config launcher (sourced by all V62B/V64/V65 stage launchers) |
| `_launch_v62b_stagea.sh` | **V65 Stage A live (1 entry, 0.005 SOL max, drawdown cap 0.003 SOL)** |
| `_launch_v62b_observe.sh` | V65 observe LIVE OFF |
| `_launch_v64_observe.sh` | V64 observe LIVE OFF |

## 3. Push repo → Hetzner

After pulling from main, push everything to `/root/piggy/`:

```bash
# From repo root (C:\Users\VASU\Desktop\tradingMahadevjiwin or equivalent)

# Bot Python modules + broker
scp -i ~/.ssh/hetzner_sniper pgg2_*.py root@87.99.151.70:/root/piggy/

# Launchers + helpers
scp -i ~/.ssh/hetzner_sniper *.sh root@87.99.151.70:/root/piggy/

# Whale-follow active pool (optional)
scp -i ~/.ssh/hetzner_sniper active_snipers.txt root@87.99.151.70:/root/piggy/

# Helper scripts
scp -i ~/.ssh/hetzner_sniper jupiter_rescue.py rescue_all_stuck.py root@87.99.151.70:/root/piggy/

# Verify syntax
ssh -i ~/.ssh/hetzner_sniper root@87.99.151.70 \
  '/root/piggy/venv/bin/python -m py_compile /root/piggy/pgg2_v48_drylive_harness.py \
   && /root/piggy/venv/bin/python -c "import pgg2_v48_drylive_harness" \
   && echo SYNTAX_OK'
```

## 4. Pre-flight check

```bash
ssh -i ~/.ssh/hetzner_sniper root@87.99.151.70 << 'EOF'
# Stop any running bot
pkill -9 -f pgg2_v50b 2>/dev/null
tmux kill-server 2>/dev/null

# Verify no positions / nonzero balances
python3 << 'PY'
import json, urllib.request
WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
RPC = "https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
def rpc(m, p):
    req = urllib.request.Request(RPC, data=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(), headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())
print("wallet=%.9f SOL" % (rpc("getBalance",[WALLET])["result"]["value"]/1e9))
nonzero = 0
for prog in ("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
    r = rpc("getTokenAccountsByOwner", [WALLET, {"programId": prog}, {"encoding":"jsonParsed"}])
    for x in r["result"]["value"]:
        if x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"] != "0":
            nonzero += 1
print("nonzero_positions=" + str(nonzero))
PY
EOF
```

Expect `nonzero_positions=0` before launching. If non-zero, run `rescue_all_stuck.py` first.

## 5. Launch live (Stage A: 1 entry, 30 min budget)

```bash
ssh -i ~/.ssh/hetzner_sniper -o ServerAliveInterval=10 root@87.99.151.70 \
  "tmux kill-session -t v65_stagea 2>/dev/null; \
   tmux new-session -d -s v65_stagea \
   'cd /root/piggy && bash _launch_v62b_stagea.sh > logs/V65_STAGEA_RUN.log 2>&1' \
   && echo LAUNCHED && tmux ls"
```

## 6. Watch progress

```bash
# Tail live events (filter for buy/sell/V64/V62B/V63)
ssh -i ~/.ssh/hetzner_sniper root@87.99.151.70 \
  'tail -F /root/piggy/logs/V65_STAGEA_RUN.log' | \
  grep -E "PGG2-V48-LIVE-BUY-SEND|PGG2-V64-LIVE-BUY-AUTHORIZED|PGG2-V62B-SELL-CONFIRMED|PGG2-V63-FINAL-PNL|PGG2-V48-LIVE-SMOKE-END|PGG2-V50B-COMPLETE|wallet_delta=|Traceback"
```

Key log signatures:
- `PGG2-V64-LIVE-BUY-AUTHORIZED gates_passed=15` — passport passed, all gates green
- `PGG2-V48-LIVE-BUY-SEND` — live buy sent (only after V64 AUTHORIZED)
- `PGG2-V62B-SELL-CONFIRMED reason=emergency attempts=1` — clean sell, no retry burn
- `PGG2-V62B-SELL-RESOLVED-BY-STATE` — duplicate-safe state check triggered (rare; prior sig confirmed late)
- `PGG2-V48-LIVE-SMOKE-END router=v62b token_residual_raw=0` — close, no stuck tokens
- `PGG2-V63-FINAL-PNL` — final wallet-delta accounting
- `PGG2-V48-LIVE-BUY-NOSEND-V64 reason=passport_failed` — V64 blocked (this is correct behavior)

## 7. Stop bot

```bash
ssh -i ~/.ssh/hetzner_sniper root@87.99.151.70 \
  'pkill -9 -f pgg2_v50b; tmux kill-server 2>/dev/null; echo STOPPED'
```

## 8. Stage A budget + safety knobs (already set in `_launch_v62b_stagea.sh`)

| Knob | Value | Why |
|---|---|---|
| `PGG2_LIVE_MAX_TRADE_SOL` | 0.005 | Position size cap; V60 firewall enforces |
| `PGG2_V50B_MAX_OPEN` | 1 | Single position at a time |
| `PGG2_V50B_MAX_CLOSES` | 1 | Stage A is 1 entry |
| `PGG2_V48_TARGET_CLOSED_NONNEG` | 1 | Pass = 1 non-negative close |
| `PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL` | 0.0030 | Hard stop on cumulative loss |
| `PGG2_V50B_MAX_SECONDS` | 1800 | 30-min budget |
| `PGG2_V50B_STAGEA_FEE_BUDGET_SOL` | 0.00030 | Fee burn cap |
| `PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL` | **0.000150** | True-edge floor for "micro" tier (V65 RUN2 setting; RUN1 used 0.000050) |
| `PGG2_V59_BANK_TRUE_EDGE_MIN_SOL` | 0.000400 | True-edge floor for "bank" tier |
| `PGG2_V62B_ENABLED` | 1 | V62B authoritative sell router on |
| `PGG2_V63_ENABLED` | 1 | V63 close-account safety net + final PnL |
| `PGG2_V64_ENABLED` | 1 | V64 passport mandatory for every live buy |
| `PGG2_RESCUE_JUPITER_FALLBACK` | 0 | Jupiter blocked for pump_bc |
| `PGG2_V67_BYPASS_LEGACY_GATES` | 0 | Bypass envs fatal in V64 mode |
| `PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS` | 0 | Lane-OR disabled |
| `PGG2_V67_ALLOW_RULE_UNION_BYPASS` | 0 | Lane-OR disabled |
| `PGG2_V57_ALLOW_RULE_UNION_BYPASS` | 0 | Lane-OR disabled |
| `PGG2_V58_ALLOW_RULE_UNION_BYPASS` | 0 | Lane-OR disabled |
| `PGG2_V61_ALLOW_RULE_UNION_BYPASS` | 0 | Lane-OR disabled |
| `PGG2_V48_CLEAN_CLOSE_ENTRY_FLOOR_SOL` | 0.000000 | clean_close demoted to telemetry |

## 9. Architecture summary (why it's safe)

- **V64 CandidatePassport** keyed by mint (not decision_id) — mandatory before every live buy
- **Worst-result-wins** lattice — V47C SHADOW_ONLY cannot be downgraded by later PASS
- **Union-bypass disabled** — lane-OR cannot override BLOCK
- **V59 slippage-calibrated** — true_edge = ep - fees - tips - priority - slippage - safety
- **V60 universal firewall** — size cap, route, snapshot age, encoded tx decode
- **V61 continuation oracle** — post-V60 250ms hold + fresh curve + peak detect
- **V62B authoritative sell** — owns pump_bc bank/scratch/max_hold/emergency; no Jupiter
- **V62B duplicate-safe pre-retry** — polls prior sigs before retry to avoid Custom 3012 fee burn
- **V63 final PnL** — wallet_after - wallet_before is the canonical pass/fail metric
- **SWQOS-only** sending — Helius Sender at 0.000005 SOL tip

## 10. Tuning notes

V65 has two validated micro-floor values for `PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL`:

| Floor | Behavior |
|---|---|
| `0.000050` | Permissive — V65 RUN1 produced 1 entry in 11 min (entry-quality losers possible) |
| `0.000150` | Balanced — V65 RUN2 had 0 entries in 30 min (safer; tighter on flow) |
| `0.000500` | Too tight — V64 RUN1 had 0 entries in 30 min (over-tightened, kills frequency) |

Default in `_launch_v62b_stagea.sh` is `0.000150`. Adjust based on flow conditions.

## 11. Wallet

- Address: `Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7`
- Last known balance: 0.106497536 SOL (2026-05-20 07:23 UTC)

## 12. Forensic / reports archive

All control-plane forensics in repo root:
- `V62_FWYE_SELL_FAILURE_FORENSIC.md` — sell-router ownership failure
- `V62B_FWYE_FINAL_FORENSIC.md`, `V62B_REPLAY_FWYE.md` — V62B design
- `V62B_OBSERVE_REPORT.md`, `V62B_STAGEA_RUN1_RESULT.md` — V62B live validation
- `V63_V62B_RENT_FORENSIC.md`, `V63_REPLAY_ON_V62B_STAGEA.md`, `V63_OBSERVE_REPORT.md` — V63 design
- `V64_LIVE_BUY_BYPASS_FORENSIC.md`, `V64_REPLAY_4RZH.md`, `V64_OBSERVE_REPORT.md` — V64 design (4rzH bypass)
- `V65_FREQUENCY_AND_BYPASS_ROOT_CAUSE.md`, `V65_OBSERVE_REPORT.md`, `V65_STAGEA_RUN1_RESULT.md` — V65 consolidation
