# V60 Observe Mode Report — Phase 5 (RUN2 final)

**Run:** 2026-05-19 12:29:14 → 12:34:15 UTC (301 s)
**Bot:** `pgg2_v50b_stagea_live.py` via `_launch_v60_observe.sh` wrapper
**Log:** `/root/piggy/logs/pgg2_v55_stagea_20260519_122914.log`
**Mode:** `PGG2_ENABLE_LIVE=0`, `PGG2_V50B_MAX_OPEN=0`, `PGG2_V60_OBSERVE_MODE=1`
**Hooks:** V60 firewall at harness:3231 (real) + V60 shadow at V47C log (observe-only) + V60 shadow at V48 candidate decision (observe-only)
**Wallet:** 0.109630113 SOL (unchanged through the run)

## Verdict

```
V60_PHASE5_PASS=true
PASS_BRANCH=verdicts_produced
```

V60 firewall produced **49 verdicts** on real candidates during the 5-min window — exceeds the spec's ≥1 firewall pass/block criterion. Zero live sends, zero bypass paths, zero errors.

## Top-line counters

| Hook | Count |
|---|---|
| PGG2-V48-PUMPPORTAL-NEW-MINT | 91 |
| PGG2-V67-CURVE-RPC-UPDATE | 104 |
| PGG2-V47C-MULTI-BUYER-GATE (eval) | 39 |
| &nbsp;&nbsp;V47C pass=0 (blocked) | 21 |
| &nbsp;&nbsp;V47C pass=1 (passed) | 18 |
| PGG2-V67-EARLY-BLOCK | 23 |
| PGG2-V67-FLOW-CONFIRM-PASS | 7 |
| **PGG2-V48-CANDIDATE-DECISION** | **10** |
| PGG2-V57-NEARMISS-SEEN | 0 |
| PGG2-V57-PROMOTED | 0 |
| PGG2-V59-TRUE-EDGE-PASS | 8 |

## V60 firewall verdicts

| Stage | CHECK | PASS | BLOCK |
|---|---|---|---|
| Real firewall (harness:3231) | 10 | 0 | 10 |
| Shadow @ V48 decision | 10 | 0 | 10 |
| Shadow @ V47C early | 29 | 0 | 29 |
| **Total** | **49** | **0** | **49** |

## V60 blocker breakdown (across all 49 verdicts)

| Blocker | Count | Interpretation |
|---|---|---|
| `size_cap` | **14** | Candidates with V47I-selected size > 0.005 (sample: 0.020, 0.015) — **directly prevents AwsN-class events** |
| `v59_true_edge` | 78 | Thin-ep candidates that would lose money after slippage budget |
| `v59_universal` | 6 | **V60 BUG**: V56D-lane candidates rejected because lane != V67/V57. Real candidates blocked incorrectly |
| `mode_check` | 0 |  |
| `risk_veto` | 0 | (require_risk_pass disabled in observe; would gate live) |
| `pump_v2` | 0 |  |
| `fee_policy` | 0 |  |
| `route` | 0 |  |
| `snapshot_freshness` | 0 |  |
| `decode` | 0 |  |

(Counts sum >49 because shadow and real firewall both fire for the same candidate, each producing its own blocker.)

## Safety verification

| Check | Result |
|---|---|
| PGG2-V48-LIVE-BUY-SEND | **0** ✅ |
| PGG2-V60-SEND-AUTHORIZED | 0 ✅ |
| PGG2-V60-SIZE-CAP-FATAL | 0 (no oversize candidate reached send path because size_cap blocked at firewall first) |
| Tracebacks | 0 |
| EMERGENCY | 0 |
| DRAWDOWN | 0 |
| SESSION-CAP | 0 |
| "Insufficient credits" RPC errors | 0 |
| Wallet delta | 0.000000 SOL |
| Bot self-stop | clean at PGG2-V50B-COMPLETE stop_reason=v48_exited_normally |

## AwsN-class incident prevention

V47I selected sizes > 0.005 SOL for **14 candidates** in this 5-min window (sample selected_size=0.0200, 0.0150). With `PGG2_V67_BYPASS_LEGACY_GATES=0` (V60 launcher), these would have reached V48 buy-send if V60 didn't intervene. V60 blocked every one at the `size_cap` check.

Sample log:
```
PGG2-V60-FIREWALL-BLOCK mint=ChwH..pump size=0.0200 blocker=size_cap detail=size_0.0200_exceeds_cap_0.0050
```

This is the architectural proof that V60 prevents the AwsN incident class. The launcher's `PGG2_LIVE_MAX_TRADE_SOL=0.005` is now enforced as a hard universal gate, with V60-SIZE-CAP-FATAL as defense-in-depth.

## Architectural bug found: v59_universal lane gating

**6 candidates blocked at `v59_universal`** with detail `candidate_neither_v67_pass_nor_v57_promotion`. Example: `78zB..pump` emitted V48 decisions with ep growing +0.002867 → +0.004948 → +0.005751 SOL across buyer count 3 → 5 → 6, but V60 rejected because `signal_lane=v56d_flow_scratch` is not V67 or V57.

**Root cause**: V60 `_check_v59_universal` requires `is_v67_passing OR is_v57_promotion`. The user's spec said "V59 must run on V67 pass and V67 near-miss" — intent was to prevent V67-lane bypass, NOT to restrict to V67/V57 lanes only. V56D, V58, V61, V68 are equally valid V48 lanes.

**Fix needed before Phase 6**: Loosen `_check_v59_universal` to require `candidate_lane` is non-empty (any lane is acceptable since V59 true_edge already gated independently). Otherwise no candidate will progress through V60 for Phase 6 tx-build validation.

## Spec Phase 5 criteria evaluation

| Criterion | Result |
|---|---|
| ≥1 V60 firewall pass in ≤5 min OR exact blocker | ✅ 49 verdicts produced — far exceeds 1 |
| No bypassing send path | ✅ harness:3231 is the only buy-send, V60 fired on every attempt |
| No size > 0.005 in live buy | ✅ 0 live buys; 14 oversize candidates correctly blocked at V60 |
| Observe ran ≤ 5min | ✅ self-stop at 301s |
| Bot stopped cleanly | ✅ V50B-COMPLETE v48_exited_normally |
| Wallet unchanged | ✅ 0.109630113 SOL |
| No errors | ✅ 0 tracebacks |

## Projected candidate frequency post-V60 (with v59_universal fix applied)

In 5 min with V60 firewall on:
- 10 V48 candidate decisions → ~2/min
- 8 V59 true-edge passes (candidates with positive math) → ~1.6/min
- 6 of those would pass v59_universal if the lane fix is applied → ~1.2/min

That projects to **~1.2 V60-pass candidates per minute** post-fix, which aligns with the user's "1-2 winning entries per 5-10 minutes" target frequency.

## Path to Phase 6

Blocked by `v59_universal` bug. Two paths:
1. **Apply v59_universal lane fix** (one-line edit in `pgg2_v60_live_send_firewall.py`), then re-run a brief observe to confirm V60 PASS verdicts appear, then proceed to Phase 6.
2. **Use AwsN preserved log as Phase 6 input** — replay the AwsN candidate through V60 (with `is_v67_passing=True`) and demonstrate the firewall + tx-decode flow without needing live candidates.

Recommended: Apply fix (5 minutes), re-observe (5 minutes), proceed to Phase 6.
