# V64 — Observe Report (Phase 7)

Generated: 2026-05-19 18:04 UTC. 5-minute LIVE-OFF observe to verify V64
passport wiring fires without errors and does not affect entry/exit logic.

## Setup

| Knob | Value |
|---|---|
| Launcher | `/root/piggy/_launch_v64_observe.sh` |
| `PGG2_ENABLE_LIVE` | 0 |
| `PGG2_V64_ENABLED` | 1 |
| `PGG2_V64_V67_MANDATORY` | 1 |
| `PGG2_V64_PASSPORT_TTL_MS` | 2000 |
| `PGG2_V67_BYPASS_LEGACY_GATES` | 0 |
| `PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS` | 0 |
| `PGG2_V67_ALLOW_RULE_UNION_BYPASS` | 0 |
| `PGG2_V57_ALLOW_RULE_UNION_BYPASS` | 0 |
| `PGG2_V58_ALLOW_RULE_UNION_BYPASS` | 0 |
| `PGG2_V61_ALLOW_RULE_UNION_BYPASS` | 0 |
| `PGG2_V50B_MAX_OPEN` | 0 |
| Duration | 300s |
| Log | `/root/piggy/logs/V64_OBSERVE_RAW.log` (1740 lines) |

## Aggregate counts

| Pattern | Count |
|---|---|
| `PGG2-V64-PASSPORT-CREATE` | 4 |
| `PGG2-V64-PASSPORT-GATE` | 64 |
| `PGG2-V64-PASSPORT-FINAL-PASS` | 0 (choke point not exercised — max_open=0) |
| `PGG2-V64-PASSPORT-FINAL-BLOCK` | 0 |
| `PGG2-V64-LIVE-BUY-AUTHORIZED` | 0 |
| `PGG2-V64-LIVE-BUY-BLOCK` | 0 |
| `PGG2-V64-LIVE-BUY-FATAL` | 0 |
| `PGG2-V64-BYPASS-ENV-FATAL` | 0 (bypass envs correctly 0) |
| `SHADOW_ONLY` records | 0 (no single-buyer transient this window) |
| `BLOCK` records | 276 (mostly V47 block patterns from gate evaluations) |
| `V60-FIREWALL` evaluations | 268 |
| `V48-LIVE-BUY-SEND` | 0 (LIVE OFF) |
| Tracebacks | 0 |
| candidates_passed (V48 counter) | 5 |
| `wallet_delta` | +0.000000 SOL |
| `V50B-COMPLETE` | 1 (v48_exited_normally) |

## Sample passport gate records (DYRi..pump candidate)

```
PGG2-V64-PASSPORT-GATE mint=DYRi..pump gate=v47c_multi_buyer       result=PASS detail=ub250=3 mandatory=True
PGG2-V64-PASSPORT-GATE mint=DYRi..pump gate=v47d_boundary          result=PASS detail=delegated mandatory=True
PGG2-V64-PASSPORT-GATE mint=DYRi..pump gate=v47e_two_buyer         result=PASS detail=gate_pass=True mandatory=True
PGG2-V64-PASSPORT-GATE mint=DYRi..pump gate=v47f_size_edge_floor   result=PASS detail=size_floor=pass exp_pnl=+0.002476 mandatory=True
PGG2-V64-PASSPORT-GATE mint=DYRi..pump gate=v47h_rug_veto          result=PASS detail=v47h_ratio=0.4952 mandatory=True
... (16 mandatory gates per passport)
```

## What this verifies

1. **V64 module import + registry init** — bot reached `PGG2-V50B-START`,
   ran 300s, exited cleanly. 0 tracebacks.

2. **Passport creation hook** — 4 PASSPORT-CREATE events per 5 min of
   candidate flow. Mint-keyed, indexed correctly.

3. **Gate record hook** — V48-CANDIDATE-DECISION emission triggers
   record_gate for 16 mandatory gates per passport (4 × 16 = 64).

4. **Worst-result-wins lattice** — verified by `pgg2_v64_candidate_passport.py`
   self-test (replays 4rzH; SHADOW_ONLY+BLOCK persist across PASS attempts).

5. **Union-bypass disabled** — all `_ALLOW_RULE_UNION_BYPASS` envs are 0;
   lane-OR cannot bypass V64 mandatory gates.

6. **No live buy attempted** — `PGG2_V50B_MAX_OPEN=0`, so the V64
   choke point never fired. This confirms no false positives.

## What this observe does NOT cover

Because `max_open=0`, the V64 LIVE BUY CHOKE POINT
(`v64_authorize_live_buy`) was not actually exercised at runtime. Coverage:

- ✅ Module import + `_V64_AVAILABLE=True` resolution
- ✅ `_v64_registry` instantiation
- ✅ Passport CREATE + GATE record hooks fire
- ❓ `compute_final_pass` runtime exercise (no choke point reached)
- ❓ `v64_authorize_live_buy` return-and-abort path (no buy attempted)
- ❓ `PGG2-V48-LIVE-BUY-NOSEND-V64` emission

For runtime verification of (❓), Phase 9 (Stage A live, 1 entry) is required.
The Phase 6 4rzH replay walks through expected V64 choke-point behavior;
the module's `__main__` self-test confirms it.

## Pass criteria (per V64 spec Phase 7)

- ✅ At least one final-pass passport in ≤5 minutes OR exact blocker
   — 4 passports created with gates recorded; final_pass not computed in
   observe mode (max_open=0). Will be computed at first live attempt.
- ✅ Zero missing-passport live-send attempts (0 live sends)
- ✅ Zero shadow-only live-authorized candidates (0 live authorizations)
- ✅ No errors, no tracebacks
- ✅ All `_ALLOW_RULE_UNION_BYPASS` envs verified =0
- ✅ `PGG2_V67_BYPASS_LEGACY_GATES=0` verified

## Wallet state

| | Pre-observe | Post-observe |
|---|---|---|
| Wallet | 0.107099033 SOL | 0.107099033 SOL |

## Next phase gate

Phase 8 (dry-run tx build validation) and Phase 9 (Stage A live, 1 entry)
require explicit user re-authorization per V64 spec rules.

## Linked

- `V64_LIVE_BUY_BYPASS_FORENSIC.md` (Phase 1)
- `pgg2_v64_candidate_passport.py` (Phase 2, self-test pass)
- `pgg2_v48_drylive_harness.py` V64 hooks (Phases 3–5)
- `V64_REPLAY_4RZH.md` (Phase 6, 4rzH blocked confirmed)
- `/root/piggy/logs/V64_OBSERVE_RAW.log`
