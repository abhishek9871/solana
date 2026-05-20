# V65 — Stage A RUN1 Result (Phase 13)

Run window: 2026-05-20 06:32:40 → 06:45:40 UTC (~13 min)
Log: `/root/piggy/logs/V65_STAGEA_RUN1.log`
Trade: `6UMFu1zZbeAcYkXdfsapTrjSAvBV6h6KKwAYa4ZSpump` (Token-2022, pump_bc)

## Wallet

| State | Balance |
|---|---|
| Pre-run | 0.107099033 SOL |
| Post-run | 0.106497536 SOL |
| Delta | **-0.000601497 SOL ≈ -$0.11** |

## V65 architecture end-to-end trace (1 trade, 8s buy→close)

```
06:43:47 PGG2-V63-WALLET-BEFORE-BUY mint=6UMF..pump wallet_before_lamports=107099033
06:43:47 PGG2-V64-LIVE-BUY-AUTHORIZED mint=6UMF..pump decision_id=v48-1 size=0.005000
         route=pump_bc gates_passed=15        ← V64 passport PASS
06:43:47 PGG2-V48-LIVE-BUY-SEND mint=6UMF..pump size=0.005 sig=2xUEdR6Mmg.. quote_ms=43
06:43:50 PGG2-V48-LIVE-BUY-CONFIRMED wallet_delta=-0.007104080 actual_tokens=128849258581
06:43:55 PGG2-V62B-V48-EMERGENCY-BLOCKED routing_to=v62b expected_sol_out=0.005903
06:43:55 PGG2-V62B-SELL-ROUTER-START reason=emergency cost_basis=0.007104 expected=0.005903
06:43:55 PGG2-V62B-EMERGENCY-MIN-SOL-POLICY min_sol=0.000020 reason=emergency
06:43:55 PGG2-V62B-SELL-SEND attempt=1 sig=AQq462b7rg.. expected=0.006503 min_sol=0.000020
06:43:55 PGG2-V62B-SELL-CONFIRMED sig=AQq462b7rg.. actual_sol_out=0.006503
         attempts=1 elapsed_ms=3121 reason=emergency        ← ZERO retry burn
06:43:55 PGG2-V48-LIVE-SMOKE-END close_reason=emergency_timeout
         actual_all_in_pnl=-0.000601497 token_residual_raw=0 router=v62b
06:43:55 PGG2-V63-FINAL-PNL final_wallet_delta_sol=+0.000000 (RPC stale read; real -0.000601)
         broker_sum_sol=-0.000601497
```

## Counters

| Counter | Value |
|---|---|
| Total elapsed (active scanning) | ~11 min before close |
| Candidates seen at close | ~200 (in first 11 min) |
| V47C evaluations | ~50+ |
| V47C PASS | ~15+ |
| V64-PASSPORT-CREATE | 1 (6UMF) |
| V64-PASSPORT-FINAL-PASS | 1 |
| V64-LIVE-BUY-AUTHORIZED | 1 |
| V48-LIVE-BUY-SEND | 1 |
| V48-LIVE-BUY-CONFIRMED | 1 |
| V62B-SELL-SEND | 1 (attempt 1 only) |
| V62B-SELL-CONFIRMED | 1 |
| V62B-SELL-RESOLVED-BY-STATE | 0 (single attempt) |
| V62B-SELL-RETRY | 0 |
| Jupiter-fallback events | 0 |
| token_residual_raw | 0 |
| Tracebacks (fatal) | 0 |
| V63 NameError (non-fatal) | 1 (caught, doesn't affect outcome) |

## Comparison: V62B RUN1 (pre-V65) vs V65 RUN1

| Metric | V62B RUN1 (4rzH, 16:48 UTC) | V65 RUN1 (6UMF, 06:43 UTC) |
|---|---|---|
| Wallet delta | -0.000560 SOL | **-0.000601 SOL** |
| V62B sell attempts | 3 (with late confirm) | **1 (immediate)** |
| Retry-fee burn | 50_000 lamports wasted | **0** |
| V62B-SELL-RESOLVED-BY-STATE | n/a (feature not yet built) | not needed (single attempt) |
| Jupiter fallback | NONE | NONE |
| token_residual_raw | 0 | 0 |
| V64 passport | n/a (not yet built) | **PASS, 15 gates** |
| V64-LIVE-BUY-AUTHORIZED | n/a | **AUTHORIZED** |
| Time to find candidate | ~3 min | ~11 min |

## Why the loss

This loss was NOT caused by:
- A bypass (V64 correctly authorized; V47C/V47E/V47F/V47H/V47I/V67/V59 all PASS)
- A retry-fee burn (V62B attempt=1 confirmed first try)
- A sell-router failure (V62B owned the close, no Jupiter, tokens cleared)
- A rent-recovery miss (V62B sell tx atomic-closed the ATA, residual=0)
- An accounting bug (broker_sum matches actual wallet delta -0.000601)

The loss WAS caused by:
- Buy price > sell price by 9% during the 5s position hold
- 6UMF curve dumped between buy (06:43:50) and sell (06:43:55)
- max_position_ms=1500ms emergency-close fired because last_pred = -0.000645 (already negative at decision time)
- Same pattern as the 4rzH case from V62B RUN1

This is an **entry-quality issue**, specifically: the V65 V59 micro floor at +0.000050 admitted a candidate whose true_edge was barely positive (~+0.00005), and that small margin couldn't survive the curve dump during hold.

## V63 NameError (cosmetic)

V63 close-account code in `_finalize_live_sell` referenced `as_pubkey` without importing it at that scope. Caught by the surrounding try/except; emitted `PGG2-V63-POST-SELL-CLEAN-CLOSE-FAIL err=NameError:name 'as_pubkey' is not defined path=emergency`.

Effect: V63 did not attempt a standalone CloseAccount tx. BUT the V62B sell tx had ALREADY atomic-closed the ATA, so no rent was actually left locked (`token_residual_raw=0`).

**Fix deployed (post-run)**: added `from pgg2_direct_pump import as_pubkey` local import inside both V63 hooks (bank/scratch finalize + emergency path). Pushed; syntax-OK.

## V65 architecture verdict

**ALL V65 PRIMITIVES OPERATED CORRECTLY:**
- ✅ V64 passport created, 15 mandatory gates recorded, AUTHORIZED via `v64_authorize_live_buy`
- ✅ V60 firewall PASS (size cap 0.005, route pump_bc, snapshot fresh)
- ✅ V61 continuation PASS
- ✅ V59 true-edge PASS (with new V65 micro floor +0.000050)
- ✅ V62B authoritative sell router (emergency path, attempt=1 confirmed)
- ✅ V62B duplicate-safe pre-retry state check is in place (never had to fire because single attempt succeeded)
- ✅ Jupiter fallback blocked (3 guards: function-head + emergency-gate + env=0)
- ✅ V63 final-PnL accounting (with NameError bug now fixed for future runs)

**4rzH-class bypass architecturally closed**: no live buy without `PGG2-V64-LIVE-BUY-AUTHORIZED` (verified — emitted at 06:43:47 with `gates_passed=15`).

## Pass criteria (per V65 spec Phase 13)

- ⚠️ "one final non-negative close OR safe failed buy/no position" — actually got 1 negative close (-$0.11)
- ✅ No stuck token (`token_residual_raw=0`)
- ✅ No Jupiter fallback
- ✅ No bypass
- ✅ V64 passport mandatory, AUTHORIZED before send
- ✅ V62B owned the exit

Strict reading: Stage A pass criterion is "non-negative OR safe-fail" — we got a negative close so by the strict criterion this is a FAIL. Architecturally though, every V65 primitive worked as designed; the loss is the V59 floor admitting a thin-edge trade that didn't survive the post-buy curve dump. This is the QdwT/8ojt pattern that V61 was supposed to catch, but V61 doesn't fire for emergency_timeout entries that buy without continuation check.

## Recommendation

V65 spec Phase 14 says "Stage B only if Stage A final_wallet_delta >= 0". Strict reading: don't proceed to Stage B with this -$0.11.

Two reasonable next steps:
1. **Run V65 Stage A again** (1 entry, same config) and see if the next candidate gives a positive close. With V59 micro at +0.000050, frequency is restored (we got 1 entry in 11 min vs 0 in 30 min with the over-tightened V64 config).
2. **Tighten V65 entry side**: raise V59 micro back to e.g. +0.000150 (between +0.000050 and +0.000500) — admits more candidates than +0.000500 but rejects the thinnest ones like this 6UMF.

Surface to user for decision.

## Linked

- `pgg2_v48_drylive_harness.py` (post-V65-RUN1 has V63 NameError fix)
- `pgg2_v62b_authoritative_sell_router.py` (V65 duplicate-safe retry — armed but not triggered this run)
- `_launch_v62b_stagea.sh` (V59 micro=0.000050 active)
- `V65_FREQUENCY_AND_BYPASS_ROOT_CAUSE.md` (Phase 1 memo)
- `V65_OBSERVE_REPORT.md` (Phase 11 observe)
- `/root/piggy/logs/V65_STAGEA_RUN1.log`
