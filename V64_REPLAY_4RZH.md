# V64 — Replay 4rzH through V64 (Phase 6)

Generated: 2026-05-19. Replays the 4rzHmpaGUgXizN2QwpYAonpzBHEyrTudPWuWogcrX3Fb
candidate through the V64 CandidatePassport. The replay uses the recorded gate
emissions from `V62B_STAGEA_RUN1.log.preserved` and confirms V64 would have
blocked the live buy.

## Replay sequence

Same sequence as the original (timestamps relative):

| T+ms | Event | V64 passport action |
|---|---|---|
| 0 | V47C ub250=1 → SHADOW_ONLY single_buyer_shadow_only | `passport.record_gate("v47c_multi_buyer", SHADOW_ONLY, "ub250=1")` |
| 30 | V47C ub250=2 → PASS | record_gate (no-op, worst-result wins: SHADOW_ONLY persists) |
| 35 | V47E ub=2 tbs=0.714 → BLOCK ub2_tbs_gt_060 | `passport.record_gate("v47e_two_buyer", BLOCK)` |
| 45 | V47C ub250=3 → PASS | record_gate (no-op) |
| 47 | V47E ub=3 → delegate_through (PASS) | record_gate (no-op, v47e already BLOCK) |
| 48 | V47F PASS, V47H PASS, V47I PASS | record_gate PASS |
| 50 | V67-FLOW-CONFIRM blockers=v47h_ratio → BLOCK | `passport.record_gate("v67_flow_confirm", BLOCK)` |
| 52 | V48-CANDIDATE-DECISION v48-1 gate_pass=false | passport already populated |
| 53 | V48-DRYLIVE-ENTRY-BLOCK clean_close_gate | `passport.record_gate("v48_entry_block", BLOCK, "clean_close_gate")` |
| 102 | V47C ub250=4 → PASS | record_gate (no-op) |
| 104 | V67-FLOW-CONFIRM blockers=v47h_ratio (still) → BLOCK | record_gate (no-op, already BLOCK) |
| 106 | V48-CANDIDATE-DECISION v48-2 gate_pass=true (lane-OR via V56D) | passport.record_decision_id_refresh("v48-2"); but BLOCK results persist |
| 110 | V59-TRUE-EDGE-PASS bank tier | record_gate PASS |
| 115 | V48-LIVE-ENTRY-OPEN-ATTEMPT decision_id=v48-2 | — |
| 120 | **Reach V48-LIVE-BUY-SEND choke point** | **`v64_authorize_live_buy(passport=...)` runs** |

## V64 choke point evaluation

```python
passport.compute_final_pass()
# Mandatory gates required PASS:
#   v47c_multi_buyer    -> SHADOW_ONLY  ← FAIL
#   v47e_two_buyer      -> BLOCK        ← FAIL
#   v67_flow_confirm    -> BLOCK        ← FAIL (mandatory in V64)
#   v48_entry_block     -> BLOCK        ← FAIL
# Other gates: PASS
# Result: final_pass = False

v64_authorize_live_buy(...)
# Returns:
#   authorized = False
#   reason = "passport_failed"
#   blockers = [
#     "v47e_two_buyer:ub2_tbs_gt_060_block",
#     "v67_flow_confirm:v47h_ratio",
#     "v48_entry_block:clean_close_gate:ub_le4_top_share_gt_0.550",
#   ]
#   shadow_only = ["v47c_multi_buyer:single_buyer_shadow_only"]
```

## Expected emitted log lines (vs original RUN1)

| RUN1 (V62B/V63 era) | V64 replay |
|---|---|
| `PGG2-V48-LIVE-BUY-SEND mint=4rzH..X3Fb size=0.005000 sig_preview=AHW3..` | **`PGG2-V48-LIVE-BUY-NOSEND-V64 mint=4rzH..X3Fb reason=passport_failed blockers=v47e_two_buyer:ub2_tbs_gt_060_block,v67_flow_confirm:v47h_ratio,v48_entry_block:clean_close_gate:ub_le4_top_share_gt_0.550`** |
| `PGG2-V48-LIVE-BUY-CONFIRMED ... actual_tokens=111571..` | (no buy occurred) |
| `PGG2-V62B-V48-EMERGENCY-BLOCKED ... routing_to=v62b` | (no buy → no sell needed) |
| `PGG2-V48-LIVE-SMOKE-END close_reason=emergency_timeout actual_all_in_pnl=-0.000510` | (no close) |
| Wallet: -0.000560 SOL | Wallet: **0** SOL (no trade) |

## Hard outputs (per V64 spec Phase 6)

```
4rzH_creates_passport = true
v47c_gate_writes_shadow_only = true   (passport.gate_results["v47c_multi_buyer"].result == SHADOW_ONLY)
v47e_gate_writes_block = true         (passport.gate_results["v47e_two_buyer"].result == BLOCK)
v67_gate_writes_block = true          (passport.gate_results["v67_flow_confirm"].result == BLOCK)
v48_entry_block_recorded = true       (passport.gate_results["v48_entry_block"].result == BLOCK)
final_pass = false
live_buy_blocked = true
no_send = true

4RZH_BLOCKED_BY_V64 = true
```

(verified by `pgg2_v64_candidate_passport.py` `__main__` self-test which replays
exactly this sequence with the same gate inputs and confirms `Authorized:
False reason: passport_failed`.)

## Why V64 catches what RUN1 missed

| Bypass pattern | V64 defense |
|---|---|
| V47C SHADOW_ONLY at ub=1 forgotten when ub later >=2 | Worst-result lattice: `record_gate` refuses to downgrade BLOCK/SHADOW_ONLY to PASS |
| V67 BLOCK overridden by V56D PASS via lane-OR | V64 makes each mandatory gate independently required; lane-OR is irrelevant |
| clean_close_gate BLOCK on v48-1 forgotten on v48-2 refresh | Passport keyed by MINT, not decision_id; v48-1 BLOCK persists when v48-2 arrives |

## Pass criteria (per V64 spec Phase 6)

- ✅ 4rzH creates passport (record_gate populates it)
- ✅ V47C gate writes SHADOW_ONLY (single_buyer_shadow_only)
- ✅ final_pass = false (multiple blockers + shadow_only)
- ✅ live buy would be blocked (`PGG2-V48-LIVE-BUY-NOSEND-V64`)
- ✅ no send

**`4RZH_BLOCKED_BY_V64=true`** ✅

## Linked

- `V64_LIVE_BUY_BYPASS_FORENSIC.md` (Phase 1)
- `pgg2_v64_candidate_passport.py` (Phase 2; self-test executes exactly this replay)
- `pgg2_v48_drylive_harness.py` (Phases 3–5; V64 hooks at decision, entry-block, and live-buy-send)
- `_launch_v62b_stagea.sh` (Phase 5; V64 env config + union-bypass=0 enforced)
