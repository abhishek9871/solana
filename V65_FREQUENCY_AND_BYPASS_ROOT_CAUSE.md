# V65 — Consolidated Frequency + Bypass Root-Cause (Phase 1)

Generated: 2026-05-20 06:15 UTC. Final synthesis of why the bot has both
(a) a frequency collapse and (b) a control-plane bypass, and what to
change to restore safe live entries at the target rate.

```
V65_ROOT_CAUSE_SUPPLY_NOT_MARKET=true
V65_ROOT_CAUSE_OVERGATING=true
V65_ROOT_CAUSE_MISSING_PASSPORT=true
```

## A. Supply exists

The bot is NOT operating in a dead market. The audit + replay evidence:

| Source | Finding |
|---|---|
| V58 22-min audit | 386 events with `ep ≥ +0.00025` |
| Post-dedup window | ~10–20 unique Tier-B mints per 5–10 min |
| V59 live-equivalent validation | **4 true-edge passes in 5 min, 0 false positives** at the original `+0.000050` micro floor |
| V60 projection after lane-floor fix | **~1.2 V60-pass candidates per minute** |
| V64 Stage A RUN1 (30 min, 2026-05-19 19:07-19:37) | 555 candidates_seen, 16 V48 decisions, 6 passports created — **the funnel is alive** |

**Conclusion**: the "no entry" state at the end of V64 Stage A RUN1 was NOT
because no candidates existed. They did. They were filtered.

## B. Frequency collapsed because of gate-ordering and threshold escalation

Three independent over-gates accumulated upstream of the V59/V60/V61 final
authority. Each one looked reasonable in isolation; together they choked
the bot.

### B1. V67/V56D expected_pnl floor stuck at 0.001500

Historic configuration:
```
PGG2_V67_MIN_EXPECTED_PNL=0.001500
PGG2_V48_V56D_MIN_EXPECTED_PNL=0.001500
```

V58 audit measured blocked-candidate `ep` distribution at `+0.000329` to
`+0.000786` (median around `+0.000550`). V60's slippage-calibrated final
gate required only `ep ≈ +0.000880` at 0.005 SOL to net `true_edge ≥ 0`.

So a candidate with `ep = +0.0010` was killed by V67/V56D's `0.0015` floor
even though V60 would have admitted it. The upstream gate was tighter than
the final gate — wrong direction. **Status: already corrected to
`0.000900` in `_launch_v62b_stagea.sh`.**

### B2. clean_close_entry_floor at 0.001500 — authoritative

Historic configuration:
```
PGG2_V48_CLEAN_CLOSE_ENTRY_FLOOR_SOL=0.001500
```

In V47G/V47H sessions, this floor blocked all four V48 candidates in a
single run BEFORE V60/V61 could evaluate them. The clean_close gate was
designed as TELEMETRY ("would this candidate survive a hypothetical clean
close?") but was wired as ENTRY-AUTHORITATIVE.

**Status: already set to `0.000000` in launcher; needs to remain so AND
be reclassified as telemetry-only in code.**

### B3. V59 micro true-edge floor reactively raised to +0.000500

Historic timeline:
- V59 origin: `+0.000050` micro min — produced 4 passes / 5 min, 0 FPs
- After 8ojt thin-edge loser (V61 session, 2026-05-19): raised to `+0.000500`
- V64 Stage A RUN1: 6 mints reached passport; V59 blocked them all
  with `true_edge=+0.000226` to `+0.000275` — values that the original
  `+0.000050` floor would have admitted

Direct evidence from V64 Stage A RUN1:
```
PGG2-V59-TRUE-EDGE QdwT..pump size=0.0050 ep=+0.001056 ... true_edge=+0.000226 pass_micro=0
PGG2-V59-TRUE-EDGE QdwT..pump size=0.0050 ep=+0.001105 ... true_edge=+0.000275 pass_micro=0
```

**Status: `_launch_v62b_stagea.sh` currently has `PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL=0.000500` — MUST REVERT to `+0.000050` per V65 spec.**

## C. Safety failures were bypass + missing-passport failures

### C1. AwsN — V67 legacy size-restore bypass

V60 audit (2026-05-19): four live-send paths existed before V60 came
online. The size-restore branch after V47F downsize converted a config
0.005 SOL into a 0.050 SOL live buy. **Status: V60 firewall closes this;
`PGG2_V67_BYPASS_LEGACY_GATES=0` mandatory.**

### C2. 4rzH — live buy after V47C SHADOW_ONLY

Direct log evidence from V62B Stage A RUN1 (preserved):
```
[2026-05-19 16:48:42] PGG2-V47C-MULTI-BUYER-GATE mint=4rzH..X3Fb ub250=1 ... pass=0 blocker=single_buyer_shadow_only
[2026-05-19 16:48:42] PGG2-V47C-MULTI-BUYER-GATE mint=4rzH..X3Fb ub250=2 ... pass=1
[2026-05-19 16:48:42] PGG2-V47C-MULTI-BUYER-GATE mint=4rzH..X3Fb ub250=3 ... pass=1
[2026-05-19 16:48:42] PGG2-V47C-MULTI-BUYER-GATE mint=4rzH..X3Fb ub250=4 ... pass=1
[2026-05-19 16:48:42] PGG2-V48-LIVE-BUY-SEND mint=4rzH..X3Fb size=0.005000 sig_preview=AHW3..
```

The SHADOW_ONLY at ub=1 was forgotten when ub later climbed. V67 BLOCK
was OR-overridden by V56D PASS via lane-OR. clean_close BLOCK on v48-1
was forgotten on v48-2 snapshot refresh.

**Root cause**: `live_buy_sent_without_required_gate_passport`.
**Status**: V64 CandidatePassport already implemented:
- Mint-keyed (not decision_id) — survives snapshot refresh
- Worst-result-wins lattice — SHADOW_ONLY cannot be downgraded
- Union-bypass envs all 0 — lane-OR cannot override BLOCK
- `PGG2_V67_BYPASS_LEGACY_GATES=1` is fatal

## D. Sell path lessons (V62B + V63)

### D1. V62B authoritative is correct

Fwye (2026-05-19): valid V60+V61 entry (`expected_sol_out=0.006131` on
0.005 SOL buy = +22% edge), but lost because:
- `broker.signature_status()` AttributeError on every poll
- Legacy `retarget_sell_min_sol` Custom(6023) reject on Token-2022
- Jupiter fallback engaged after 1 emergency attempt with stale balance

V62B fixed: direct RPC polling, 3-retry resend ladder, emergency
`min_sol=0.000020`, Jupiter blocked for pump_bc. **Keep as-is.**

### D2. V63 — final wallet delta accounting

V62B Stage A RUN1 (4rzH): wallet delta `-0.000560 SOL` vs
broker_delta_sum `-0.000510 SOL`. The 50 000 lamport `unattributed` =
**2 × V62B retry tx fees** burned because attempts 1 and 3 reached the
leader AFTER attempt 2's late confirm closed the ATA → Custom 3012.

**Lesson**: V62B duplicate-safe pre-retry state check is the missing
refinement. Before issuing retry 2 or 3:
1. Re-query `getTokenAccountsByOwner` for this mint
2. If account doesn't exist OR raw_balance == 0:
   - STOP, mark `resolved_by_state=true`
   - Do NOT send retry
3. Else: rebuild + resend

V63 final PnL accounting (wallet_after - wallet_before) is canonical
pass/fail metric. **Keep V63 as authority; don't redo CloseAccount if
sell tx atomic-closed already.**

## E. The fix is control-plane consolidation, NOT a new strategy

Per evidence, the changes needed are:

| Change | Status |
|---|---|
| Restore V59 micro floor to `+0.000050` | **PENDING** (launcher has 0.000500) |
| Keep V59 bank floor `+0.000400` | done |
| Keep V67/V56D prefilter at `~0.000900` | done |
| clean_close as telemetry only | done (`=0.000000`) |
| V64 passport mandatory at every live-buy | done (harness:3677) |
| Union-bypass envs all 0 | done (5 lanes set in launcher) |
| `PGG2_V67_BYPASS_LEGACY_GATES=0` | done |
| V62B authoritative for pump_bc exits | done |
| V62B duplicate-safe pre-retry state check | **PENDING** |
| V63 final wallet delta = pass/fail authority | done |
| No Jupiter for pump_bc | done (3 guards) |
| Token-2022 / Pump v2 | done (broker handles) |
| Max size 0.005 SOL live | done (env + V60 firewall) |
| SWQOS tip 0.000005 SOL | done |

## F. Hard outputs (per spec)

```
V65_ROOT_CAUSE_SUPPLY_NOT_MARKET = true
  Evidence: 386 events @ ep >= +0.00025 (V58), 4/5min V59 passes, ~1.2/min V60 projection,
            555 candidates_seen in 30-min V64 Stage A RUN1.

V65_ROOT_CAUSE_OVERGATING = true
  Evidence: V67/V56D floor 0.001500 (B1), clean_close 0.001500 (B2), V59 micro 0.000500 (B3).
  Final state of V64 Stage A RUN1: 6 passports created, all blocked at V59 with true_edge
  in +0.000226 to +0.000275 range (would have passed +0.000050 micro floor).

V65_ROOT_CAUSE_MISSING_PASSPORT = true
  Evidence: 4rzH live-bought with PGG2-V47C-MULTI-BUYER-GATE pass=0 blocker=single_buyer_shadow_only
  recorded in same minute as buy. V64 forensic identified 3 bypass mechanisms; V64 passport
  closes all 3 architecturally.
```

## G. Required actions for V65 (only 2 left)

1. **Revert `PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL` from `0.000500` → `0.000050`** in
   `_launch_v62b_stagea.sh` and `_launch_v62b_observe.sh` (and any other live launcher).

2. **Add V62B duplicate-safe pre-retry state check** in
   `pgg2_v62b_authoritative_sell_router.py`: before each retry attempt
   (i.e., before attempts 2 and 3), call broker.token_balance_raw or
   getAccountInfo to verify the ATA still exists and balance > 0. If
   either condition fails, emit `PGG2-V62B-SELL-RESOLVED-BY-STATE` and
   stop the ladder.

Everything else from V60/V61/V62B/V63/V64 stays as-is.

## Linked

- `V64_LIVE_BUY_BYPASS_FORENSIC.md` (3 bypass patterns)
- `V64_REPLAY_4RZH.md` (4RZH_BLOCKED_BY_V64=true)
- `V63_V62B_RENT_FORENSIC.md` (retry-fee-burn forensic)
- `V62B_FWYE_FINAL_FORENSIC.md` (sell-router ownership)
- `pgg2_v59_true_edge.py` (slippage-calibrated model)
- `pgg2_v60_live_send_firewall.py`
- `pgg2_v61_live_continuation_oracle.py`
- `pgg2_v62b_authoritative_sell_router.py`
- `pgg2_v63_post_sell_clean_close.py`
- `pgg2_v63_final_pnl.py`
- `pgg2_v64_candidate_passport.py`
- `/root/piggy/logs/V64_STAGEA_RUN1.log.preserved.v65` (1.03 MB)
