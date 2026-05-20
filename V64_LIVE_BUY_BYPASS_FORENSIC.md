# V64 — Live Buy Bypass Forensic (Phase 1)

Generated: 2026-05-19. On-chain + log forensic of the 4rzH..X3Fb live buy
that was NOT supposed to happen.

```
ROOT_CAUSE = live_buy_sent_without_required_gate_passport
```

The candidate had a `single_buyer_shadow_only` block at first evaluation, a
`blockers=v47h_ratio` V67 block at every subsequent evaluation, and a
`clean_close_gate` entry block on decision_id v48-1 — yet decision_id v48-2
fired live ~54ms later. There is no unified passport that carries blocker
state across snapshot refreshes.

## Mint

| Field | Value |
|---|---|
| Mint | `4rzHYPWUjccxqbE41sHbBbb5YSLvPLRuLQDWjTruX3Fb` (Token-2022) |
| Buy sig | `AHW3DjRB1hDA..` |
| Buy size | 0.005 SOL |
| Buy time (UTC) | 2026-05-19 16:48:42 |
| Source log | `logs/V62B_STAGEA_RUN1.log.preserved` |

## Lined-up gate timeline (gathered from log)

All events at `2026-05-19 16:48:42` (within ~55ms).

| Order | Gate | Result | Detail |
|---|---|---|---|
| 1 | **V47C-MULTI-BUYER-GATE** (first) | **pass=0 blocker=single_buyer_shadow_only** | ub250=1, pbs=4.0, tbshare=1.000 |
| 2 | V47C-MULTI-BUYER-GATE (second eval ~30ms later) | pass=1 | ub250=2, pbs=5.6, tbshare=0.714 |
| 3 | **V47E-TWO-BUYER-GUARD** (first) | **mode=block reason=ub2_tbs_gt_060_block** | ub=2, tbs=0.714 |
| 4 | V47E-REPLACEMENT-SCAN | fired | reason=v47e_two_buyer_block (candidate kept on watchlist) |
| 5 | V47C-MULTI-BUYER-GATE (third) | pass=1 | ub250=3, tbshare=0.606 |
| 6 | V47E-TWO-BUYER-GUARD (second) | mode=delegate_v47d | ub=3 |
| 7 | V47F-SIZE-EDGE-FLOOR | pass=1 | exp_pnl=+0.001695 ≥ floor 0.000600 |
| 8 | V47H-RUG-VETO | veto_pass=1 | ratio=+0.339 |
| 9 | V47I-MEDIUM-RUG-VETO | veto_pass=1 | — |
| 10 | V68-WHALE-FOLLOW-CHECK | pass=0 | signer not in active pool |
| 11 | V56D-FLOW-LANE-PASS | pass | exp_pnl=+0.001695 |
| 12 | **V67-FLOW-CONFIRM-CHECK** (first) | **pass=0 blockers=v47h_ratio** | v47h_ratio=0.3391 > 0.3390 |
| 13 | **V48-CANDIDATE-DECISION v48-1** | **gate_pass=false** | v67_flow_confirm_gate_pass=0; bot proceeds anyway via lane-OR |
| 14 | **V48-DRYLIVE-ENTRY-BLOCK v48-1** | **blocker=clean_close_gate** | detail=ub_le4_top_share_gt_0.550 |
| 15 | V47C-MULTI-BUYER-GATE (fourth) | pass=1 | ub250=4, tbshare=0.455 |
| 16 | V47E-TWO-BUYER-GUARD (third) | mode=delegate_v47d | — |
| 17 | V47F, V47H, V47I | pass | — |
| 18 | V68-WHALE-FOLLOW-CHECK | pass=0 | — |
| 19 | V56D-FLOW-LANE-PASS | pass | — |
| 20 | **V67-FLOW-CONFIRM-CHECK** (second) | **pass=0 blockers=v47h_ratio** | v47h_ratio=0.4757 > 0.3390 |
| 21 | **V48-CANDIDATE-DECISION v48-2** | **gate_pass=true** | despite v67_flow_confirm_gate_pass=0, v56b=0, v57=0, v58=0; only v56d_flow_gate_pass=1; signal_lane=v56d_flow_scratch |
| 22 | V59-TRUE-EDGE-PASS | pass | tier=bank, true_edge=+0.001548 |
| 23 | V48-LIVE-OPEN-REVALIDATE | pass=1 | snapshot age 320ms |
| 24 | V48-LIVE-ENTRY-OPEN-ATTEMPT | — | sent the live buy |
| 25 | PGG2-DIRECT-QUOTE BUY | — | quote built |
| 26 | **PGG2-V48-LIVE-BUY-SEND** | sent | size=0.005, sig=AHW3DjRB.. |

## Hard outputs (Phase 1 deliverables per spec)

```
mint = 4rzHYPWUjccxqbE41sHbBbb5YSLvPLRuLQDWjTruX3Fb
buy_timestamp = 2026-05-19 16:48:42
buy_sig = AHW3DjRB1hDA..

v47c_pass_existed = true   (at ub250>=2 evaluations)
v47c_block_existed = true  (at ub250=1, "single_buyer_shadow_only")
single_buyer_shadow_only_existed = true

later_gate_overrode_shadow_only = true
  mechanism = candidate re-evaluated with fresh snapshot as new buyers arrived;
              transient SHADOW_ONLY blocker NOT preserved across snapshot refresh.

unified_decision_id = false
  decision_id changed: v48-1 (blocked) -> v48-2 (passed) ~54ms apart.
  no carry-over of blocker state between decision_ids.

function_that_sent_buy = pgg2_v48_drylive_harness.py:~3488 send_signed()
  reached via: V48-LIVE-ENTRY-OPEN-ATTEMPT -> _build_live_sell_quote_fast()
                                          -> PGG2-DIRECT-QUOTE BUY
                                          -> PGG2-V48-LIVE-BUY-SEND

why_it_did_not_stop_on_v47c_block:
  - V47C ran multiple times as flow events arrived
  - First eval said SHADOW_ONLY (ub=1); not persisted to a passport
  - At eval 2, ub=2 -> pass=1; V47E then blocked on top-buyer-share
  - At eval 3, ub=3 -> V47E delegated through
  - V47C+V47E+V47F+V47H+V47I all passed; only V67 blocked on v47h_ratio
  - V48-CANDIDATE-DECISION uses LANE-OR: if any of
    {v56b, v56d, v67, v57, v58, v61_fanout, v68} passes, gate_pass=true
  - V56D was the passing lane; signal_lane=v56d_flow_scratch
  - decision_id v48-1 was ENTRY-blocked by clean_close_gate; the SAME mint
    was re-decisioned as v48-2 about 50ms later with a fresher snapshot,
    and the clean_close_gate did not fire because top_share had dropped
    from 0.606 to 0.455 (ub=4 instead of ub=3).
  - No passport persists the v48-1 entry block to v48-2.

ROOT_CAUSE = live_buy_sent_without_required_gate_passport
```

## Bypass mechanisms identified

The 4rzH..X3Fb buy fired because **at least 3 distinct gate-evasion patterns**
all combined:

### Bypass #1 — V47C SHADOW_ONLY not persisted

V47C-MULTI-BUYER-GATE re-evaluates per snapshot. The first evaluation said
`pass=0 blocker=single_buyer_shadow_only`. Subsequent evaluations with more
buyers passed. **No mechanism prevents the bot from acting on the LATER
evaluation.** V64 must record the EARLIEST V47C result for a mint and treat
that as authoritative.

### Bypass #2 — V67 BLOCK overridden by V56D PASS via lane-OR

V67-FLOW-CONFIRM-CHECK said `pass=0 blockers=v47h_ratio` at BOTH evaluations
(v48-1 and v48-2). But V48-CANDIDATE-DECISION uses lane-OR logic:

```
v48-2: v56b_gate_pass=0 v56d_flow_gate_pass=1 v67_flow_confirm_gate_pass=0
       v57_impulse_gate_pass=0 v58_flow_gate_pass=0
       signal_lane=v56d_flow_scratch
       gate_pass=true   ← TRUE despite 4 of 5 lanes saying FALSE
```

The lane-OR logic implements the "union bypass" pattern. Each lane has its
own `_UNION_BYPASS=1` flag set in the launcher (V67/V56D/V57/V58/V61).

V64 must make EVERY MANDATORY gate ALL-PASS; lane-OR must be disabled for
live (telemetry-only OK).

### Bypass #3 — clean_close_gate re-evaluated with later snapshot

V48-DRYLIVE-ENTRY-BLOCK on v48-1 said `blocker=clean_close_gate
detail=ub_le4_top_share_gt_0.550`. The v48-2 decision_id ~54ms later was NOT
blocked by clean_close_gate because top_share had dropped from 0.606 to 0.455.

**The same mint, refreshed snapshot, different decision_id, blocker
disappeared.** V64 must lock the blocker for at least a cooldown window so
the same mint cannot bypass it via a 50ms-later refresh.

## Concrete code paths that allowed it

| File / line | What it does | V64 fix |
|---|---|---|
| `pgg2_v48_drylive_harness.py:~3478` (V60 SEND-AUTHORIZED → buy) | Sends buy on gate_pass=true | Require passport.final_pass=true and verify decision_id+mint+size match |
| `PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS=1` (env) | Lane-OR allows V67/V61 BLOCK to be bypassed by V56D PASS | Force=0 in V64 live mode |
| `PGG2_V67_ALLOW_RULE_UNION_BYPASS=1` (env) | Same pattern | Force=0 |
| `PGG2_V67_BYPASS_LEGACY_GATES=0` (currently OK) | But the flag's mere existence is risky | Fatal if =1 in V64 |
| V47C re-evaluation on snapshot refresh | Earliest-blocker not persisted | V64 passport accumulates worst-case across all evaluations |
| Decision_id reset on snapshot refresh | v48-1 → v48-2 forgets v48-1 blockers | V64 passport bound to MINT, not decision_id |

## Cross-check — what was the actual live entry candidate's V64 passport status?

If V64 had been running:

| Gate | V64 result on 4rzH..X3Fb |
|---|---|
| V47C (worst seen) | `SHADOW_ONLY` (single_buyer at eval 1) |
| V47D | TELEMETRY (delegated through V47E) |
| V47E | `BLOCK` once (ub=2 tbs=0.714); then delegated through |
| V47F | PASS |
| V47H | PASS |
| V47I | PASS |
| V59 true-edge | PASS |
| V60 firewall | PASS |
| V61 continuation | (not invoked, V60-flow-watch disabled) |
| V67 flow-confirm | `BLOCK` (blockers=v47h_ratio at every eval) |
| V53 risk veto | not consulted |
| V56D flow lane | PASS |
| V68 whale | `BLOCK` (signer not in active pool) |
| **V64 final_pass** | **false** (V47C SHADOW_ONLY + V67 BLOCK + V68 BLOCK + V47E one-time BLOCK) |

The 4rzH..X3Fb passport would have **clearly failed final_pass under V64**.
The bypass that allowed it was the union-bypass / lane-OR semantics of V48-
CANDIDATE-DECISION combined with snapshot-refresh decision_id re-creation.

## Pass condition (per V64 spec Phase 1)

✅ Forensic explicitly shows the bypass mechanism (three independent patterns enumerated above).
✅ `ROOT_CAUSE = live_buy_sent_without_required_gate_passport` stated.

## Linked memory

- `[[v62b_stagea_run1_2026_05_19]]` — RUN1 memo (V64 forensic clarifies it was NOT entry-quality, it was gate-bypass)
- `[[v62-fwye-sell-failure-forensic-2026-05-19]]` — Fwye forensic (sell-router was the V62 issue)
- `V63_V62B_RENT_FORENSIC.md` — rent forensic (rent was clean; not the issue)
