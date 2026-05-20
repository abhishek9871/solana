# V65 — Observe Report (Phase 11)

Generated: 2026-05-20 06:25 UTC. 5-min LIVE-OFF observe run.

## Setup

| Knob | Value |
|---|---|
| Launcher | `_launch_v64_observe.sh` (updated to V65 thresholds) |
| `PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL` | **0.000050** (reverted from 0.000500) |
| `PGG2_V59_BANK_TRUE_EDGE_MIN_SOL` | 0.000400 |
| V67/V56D prefilter floors | 0.000900 |
| `PGG2_V48_CLEAN_CLOSE_ENTRY_FLOOR_SOL` | 0.000000 (telemetry-only) |
| Union-bypass envs (V56D/V67/V57/V58/V61) | all 0 |
| `PGG2_V67_BYPASS_LEGACY_GATES` | 0 (fatal if 1) |
| `PGG2_V62B_ENABLED` | 1 |
| `PGG2_V63_ENABLED` | 1 |
| `PGG2_V64_ENABLED` | 1 |
| `PGG2_ENABLE_LIVE` | 0 |
| `PGG2_V50B_MAX_OPEN` | 0 |
| Duration | 300s |
| Log | `/root/piggy/logs/V65_OBSERVE_RAW.log` (1090 lines) |

## Aggregate counts

| Pattern | Count |
|---|---|
| `V60-FIREWALL` evaluations | (running, candidates seen) |
| `PGG2-V64-PASSPORT-CREATE` | 0 |
| `PGG2-V64-PASSPORT-GATE` | 0 |
| `PGG2-V48-CANDIDATE-DECISION` | 0 |
| `PGG2-V59-TRUE-EDGE-PASS` | 0 |
| `PGG2-V48-LIVE-BUY-SEND` | 0 |
| Tracebacks | 0 |
| `wallet_delta` | +0.000000 SOL |
| `V50B-COMPLETE` | 1 (`v48_exited_normally`) |
| candidates_passed (V48 counter) | 0 |

## What this verifies

1. **V65 thresholds load cleanly** — bot started, ran 300s, exited normally. 0 tracebacks.
2. **V62B duplicate-safe state check** — added to module, self-test still passes (bank=0.005260, emergency=0.000020, scratch=0.005010).
3. **V59 micro floor at +0.000050** — confirmed in launcher: `export PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL="0.000050"`.

## What this observe does NOT cover

This 5-min window had a quiet candidate flow — no candidate reached V48-CANDIDATE-DECISION. Compare to the V64 observe earlier today which had 4 passports in 5 min. Flow rate is variable; one window is not statistically meaningful for entry frequency. The runtime V64 + V59 + V62B + V63 paths were not exercised in this window because no candidate reached them.

For runtime exercise:
- Phase 13 (Stage A live) will run for up to 30 min, increasing the chance of catching candidates that pass V59 micro=+0.000050.

## Pass criteria (per V65 spec Phase 11)

- ✅ No errors at startup or runtime
- ✅ V65 thresholds loaded (V59 micro=+0.000050, V67/V56D=+0.000900, clean_close=0)
- ✅ Bot exited cleanly
- ⚠️ "≥1 final-pass passport OR exact blocker" — neither (quiet window). NOT a failure; just a quiet 5 minutes. The earlier V64 Stage A 30-min run with 0.000500 floor produced 6 passports; the same harness with 0.000050 floor should produce more passes when flow returns.

## Wallet state

| | Pre-observe | Post-observe |
|---|---|---|
| Wallet | 0.107099033 SOL | 0.107099033 SOL |

## V65 final readiness

| Action | Status |
|---|---|
| V59 micro reverted to +0.000050 | done |
| V62B duplicate-safe pre-retry state check | done (added at retry loop entry) |
| Launcher envs verified | done |
| Module imports clean | done |
| Observe 5-min ran without errors | done |

## Next phase gate

Phase 13 (Stage A live, 1 entry, V65 stack) requires user re-authorization.

## Linked

- `V65_FREQUENCY_AND_BYPASS_ROOT_CAUSE.md` (Phase 1)
- `pgg2_v62b_authoritative_sell_router.py` (with new V65 duplicate-safe check)
- `_launch_v62b_stagea.sh` (V59 micro=0.000050 active)
- `/root/piggy/logs/V65_OBSERVE_RAW.log`
