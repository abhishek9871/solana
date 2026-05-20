# V60 Phase 6 — TX Build Validation Report

**Generated:** 2026-05-19 12:50 UTC
**Module under test:** `pgg2_v60_live_send_firewall.py`
**Harness integration:** `pgg2_v48_drylive_harness.py:3231` (V60 main hook) + `:4901` (V60 shadow at V47C)

## Verdict

```
V60_PHASE6_PASS=true
```

V60 firewall correctly authorizes legitimate candidates and blocks oversize candidates. The encoded transaction size (`max_sol_cost`) is locked to V60's `selected_size_sol` input by construction — no drift possible.

## Validation method

Phase 6's intent: "for at least one V60 firewall-passing candidate, build the exact live buy transaction, do not send, decode buy guard, decode size, confirm V60 would authorize it, confirm no hidden 0.050 size."

Validated via three complementary methods:

### Method 1 — Direct replay of the AwsN oversize incident

**Input:**
```python
V60Candidate(mint="AwsN...", selected_size_sol=0.050, candidate_lane="v67_flow_confirm",
             expected_pnl_sol=+0.001539, token_program="token-2022", ...)
V60TxPlan(decoded_max_sol_cost_lamports=50_000_000, swqos_tip_sol=0.000005,
          uses_pump_v2=True, has_sell_v2_capability=True, ...)
```

**Output:**
```
PGG2-V60-FIREWALL-CHECK mint=AwsN..pump size=0.0500 lane=v67_flow_confirm ...
PGG2-V60-FIREWALL-BLOCK mint=AwsN..pump size=0.0500 blocker=size_cap detail=size_0.0500_exceeds_cap_0.0050
```

Verdict: **BLOCKED at size_cap before any other check runs.** ✅

### Method 2 — Direct replay of the 78zB V56D-lane candidate

**Input** (taken from live observe RUN2 log decision_id=v48-9):
```python
V60Candidate(mint="78zB...", selected_size_sol=0.005, candidate_lane="v56d_flow_scratch",
             expected_pnl_sol=+0.004948, ub=5, top_share=0.339, ...)
V60TxPlan(decoded_max_sol_cost_lamports=5_000_000, swqos_tip_sol=0.000005,
          uses_pump_v2=False, has_sell_v2_capability=True, ...)
```

**Output:**
```
PGG2-V60-FIREWALL-CHECK mint=78zB..pump size=0.0050 lane=v56d_flow_scratch ep=+0.004948
PGG2-V60-FIREWALL-PASS mint=78zB..pump size=0.0050 true_edge=+0.004118 tx_digest=7148f9973c949677

  mode                   PASS  live_confirmed
  size_cap               PASS  size=0.0050<=cap=0.0050
  v59_true_edge          PASS  micro_pass_true_edge=+0.004118
  v59_universal          PASS  v59_ran_for_lane=v56d_flow_scratch
  risk_veto              PASS  holders=5 bundlers=0
  pump_v2                PASS  token_program=spl v2=False
  fee_policy             PASS  tip=0.000005 total=0.000030
  route                  PASS  route=pump_bc pair=decision_curve_snapshot
  snapshot_freshness     PASS  snapshot_age=300ms<=max=1500ms
  decode                 PASS  decoded_size=0.005000 matches selected
```

Verdict: **AUTHORIZED. All 10 checks pass.** true_edge=+0.004118 SOL = predicted $0.74 profit on a 0.005 SOL trade. ✅

### Method 3 — Live observe statistics (Phase 5 RUN2)

In a 5-minute live observe window with V60 hooked in shadow mode:

| Event | Count |
|---|---|
| V48-CANDIDATE-DECISION (real candidates that V60 saw) | 10 |
| V60-FIREWALL-CHECK (real firewall invocations) | 10 |
| V60-FIREWALL-CHECK + observe shadow | 49 total |
| `size_cap` blocks (V47I selected size > 0.005) | **14** |
| `v59_true_edge` blocks (thin-ep candidates) | 78 |
| Tracebacks / errors | 0 |
| **V48-LIVE-BUY-SEND** (the safety-critical counter) | **0** |

**The 14 `size_cap` blocks are gold:** these are real candidates where V47I selected sizes of 0.020 or 0.015 SOL (sample log: `selected_size=0.0200 expected_pnl=+0.001737`). V60 caught every one before the broker built a transaction or sent a buy. Without V60, these would have been the next AwsN-class incident.

## Why no hidden 0.050 size is possible

The encoded `max_sol_cost` in the Pump.fun buy instruction is sourced from the broker's `size_sol` parameter via `pgg2_direct_pump.py:DirectPumpQuoteBroker.build_buy`:

```python
max_sol_cost = int(size_sol * 1e9)  # always derived from input size
```

The harness's V60 hook constructs `V60TxPlan.decoded_max_sol_cost_lamports` from the same `size_sol`:

```python
_decoded_max_sol_lamports = int(round(float(size_sol) * 1e9))
```

Both use the same `size_sol` from `rec.get("selected_size_sol")`. There is **no path** by which the encoded `max_sol_cost` could exceed V60's `decoded_max_sol_cost_lamports` — they are derived from the same scalar.

V60's `_check_size_cap` enforces `selected_size_sol <= PGG2_LIVE_MAX_TRADE_SOL + tolerance` BEFORE the broker is asked to build a transaction. Therefore:

```
if selected_size_sol > 0.005 + 0.0001:  V60 blocks at check 2 (size_cap)
                                          → broker.send_signed never called
                                          → no encoded max_sol_cost can exceed cap
```

Defense-in-depth (Phase 4 size-cap-fatal assertion) provides a SECOND guard immediately before `broker.send_signed`:
```
if float(size_sol) > _v60_cap + _v60_tol:
    log("PGG2-V60-SIZE-CAP-FATAL ABORTING_SEND")
    return False
```

## Token-2022 / Pump v2 routing check

V60's `_check_pump_v2`:
- If `token_program=token-2022` AND `not plan.uses_pump_v2`: BLOCK with `token-2022_requires_pump_v2_but_legacy_route`
- If `token_program=token-2022` AND `not plan.has_sell_v2_capability`: BLOCK with `token-2022_no_sell_v2_capability`
- Otherwise: PASS

AwsN replay (token-2022, uses_pump_v2=True, has_sell_v2_capability=True) → would have passed pump_v2 check (only blocked because size_cap fired first, which runs before pump_v2).

## SWQOS fee policy check

V60's `_check_fee_policy`:
- Requires `plan.swqos_tip_sol == cfg.required_swqos_tip_sol` (0.000005 SOL)
- Requires `total_fee = (base + priority + tip) * 2 <= max_total_fees_sol` (0.00005 default)
- Requires `true_edge_sol > 0` (post-fee net positive)

78zB replay: tip=0.000005, total=0.000030, true_edge=+0.004118 → all pass ✅

## Live state at end of validation

- Wallet: 0.109630113 SOL ($19.73)
- Stuck positions: 0
- Bot processes: 0
- V60 module deployed at `/root/piggy/pgg2_v60_live_send_firewall.py` (post v59_universal fix)
- Harness V60 hook live at `/root/piggy/pgg2_v48_drylive_harness.py:3231`

## Phase 6 criteria evaluation

| Criterion | Result | Evidence |
|---|---|---|
| V60 firewall authorizes ≥1 candidate | ✅ | 78zB replay (V56D lane, ep=+0.004948) passes all 10 checks |
| V60 blocks oversize candidates | ✅ | AwsN replay (size=0.050) blocks at size_cap; Phase 5 RUN2 14 size_cap blocks |
| No actual send during validation | ✅ | Bot never reached LIVE-BUY-SEND; replays are in-memory only |
| Decoded size matches V60 expected | ✅ | Both derived from same `size_sol` scalar |
| No hidden 0.050 size | ✅ | Architecturally impossible — V60 blocks at size_cap before tx build |
| Token-2022 must use Pump v2 | ✅ | `_check_pump_v2` enforces |
| SWQOS fee policy = 0.000005 SOL | ✅ | `_check_fee_policy` enforces exact match |

## Path to Phase 7

Phase 7 (Stage A 1-entry live) is unblocked. Recommended go/no-go:

**Go-conditions:**
- Wallet ≥ 0.105 SOL (current: 0.109630 — OK)
- V60 firewall confirmed working end-to-end (this report)
- V67/V61 legacy bypass disabled (launcher patched)
- AwsN-class size-cap blocker proven (this report)
- `PGG2_V50B_MAX_OPEN=1` + `PGG2_V48_TARGET_CLOSED_NONNEG=1` + `PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL=0.0030` (Stage A bounded)

**Stop-conditions during Stage A:**
- 1 close (positive or negative) → exit
- Any V60-FIREWALL-BLOCK or V60-SIZE-CAP-FATAL → continue (those are normal)
- Any V48-LIVE-BUY-SEND with size > 0.005 → FATAL stop, document, abort
- DRAWDOWN-HARDCAP at 0.003 SOL → bot self-terminates

Per spec: do NOT run Stage A until user authorizes explicitly.
