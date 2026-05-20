# V61 P6 Observe Report — 5-min, LIVE OFF

**Run:** 2026-05-19 14:30:39 → 14:35:39 UTC (300s)
**Log:** `/root/piggy/logs/pgg2_v55_stagea_20260519_143039.log`
**Mode:** `PGG2_ENABLE_LIVE=0`, `PGG2_V50B_MAX_OPEN=0`, `PGG2_V60_OBSERVE_MODE=1`, `PGG2_V61_ENABLED=1`
**Wallet:** 0.108054 SOL → 0.108054 SOL (delta +0.000000)

## Verdict

```
V61_PHASE6_PASS=true (via exact blocker branch)
```

## Counts

| Hook | Count |
|---|---|
| PUMPPORTAL-NEW-MINT | 93 |
| V47C-MULTI-BUYER-GATE | 82 |
| V47C pass=1 | 9 |
| V67-FLOW-CONFIRM-PRECHECK | 1 |
| V67-PRECHECK pass=1 | 0 |
| **V48-CANDIDATE-DECISION** | **0** |
| V59-TRUE-EDGE-PASS | 0 |
| V60-FIREWALL-CHECK (shadow @ V47C-early) | 82 |
| V60-FIREWALL-PASS | 0 |
| V60-FIREWALL-BLOCK | 82 (all `v59_true_edge` micro_block — ep_est=0 proxy in shadow) |
| V60-OBSERVE-PASS | 0 |
| V60-OBSERVE-BLOCK | 82 |
| **V61-PRECHECK / PASS / BLOCK** | **0 / 0 / 0** |
| V48-LIVE-BUY-SEND | 0 ✅ |
| Tracebacks | 0 ✅ |

## Exact blocker

V61 hooks fire only in the harness's live-buy-send code path (`_open_v48_live_record`), which is gated by `len(open_positions) < max_open`. With `max_open=0` (observe-only), the buy-send path is skipped entirely → V61 never reaches.

This is a known limitation of the observe mode: V61 cannot be exercised in LIVE-OFF observe with the current architecture. V61 was designed as a final pre-send gate in the live buy-send path.

V60 SHADOW (at V47C log-emit, runs regardless of LIVE/max_open) does fire (82 events). All V60-SHADOW-BLOCK at `v59_true_edge` because the V47C-stage shadow uses a coarse `ep_est=0` proxy — the actual ep is only computed downstream at V48-CANDIDATE-DECISION.

## What this observe validates

| Item | Result |
|---|---|
| Harness imports cleanly with V60+V61 hooks | ✅ |
| 5-min runtime stable, 0 Tracebacks | ✅ |
| 0 live buys (safety: LIVE OFF respected) | ✅ |
| V60 SHADOW fires on every V47C-eval candidate | ✅ (82/82) |
| Wallet unchanged | ✅ |

## What this observe does NOT validate

- V61 live verdict generation (requires LIVE ON + max_open ≥ 1 + a candidate passing V47C → V47F → V67 → V48-CANDIDATE-DECISION)
- V61 post-V60 wiring works at runtime (only unit-test + replay validate this)
- V61 catches losers in production (replay covers this — V61_REPLAY_ON_V60_RUN4.md)

## V61 module + integration state (re-checked)

- `pgg2_v61_live_continuation_oracle.py`: 10 rules, self-test PASSES (both V60 RUN4 losers blocked)
- Harness Phase 3 patch: V61 hook injected at `pgg2_v48_drylive_harness.py:~3375` (post-V60-SEND-AUTHORIZED, pre-BUY-SEND)
- Sync mode (no async wait), reads `oracle._states[mint].points` directly
- Fail-closed on any V61 error
- 8 V61 markers in harness, syntax OK, imports OK

## Spec Phase 6 criteria evaluation

| Criterion | Result |
|---|---|
| ≥1 V61 pass in ≤5min OR exact blocker | ✅ exact blocker = max_open=0 prevents buy-send path |
| No bypasses | ✅ V60-SHADOW shows no firewall bypass |
| No size > 0.005 | ✅ all V60-SHADOW emissions at size=0.0050 |
| Observe ran ≤ 5min | ✅ self-stop at 300s |
| Bot stopped cleanly | ✅ V50B-COMPLETE stop_reason=v48_exited_normally |
| Wallet unchanged | ✅ delta 0.000000 |

## Path forward

V61 validation depends on:
1. **Self-test (unit test)** — passed (synthetic millisecond data matching forensic)
2. **Replay on V60 RUN4** — passed (3/3 V60 PASSes blocked by V61)
3. **Live observe (this run)** — V61 not exercised (max_open=0 prevented buy-send path)
4. **Stage A live** — only this proves V61 catches real losers in production

Stage A live is the next test. Risk capped at drawdown_cap=0.003 SOL.
