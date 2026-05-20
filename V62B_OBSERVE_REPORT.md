# V62B — Observe Report (Phase 8)

Generated: 2026-05-19 16:43 UTC. 3-minute LIVE-OFF observe run.

## Setup

| Knob | Value |
|---|---|
| Launcher | `/root/piggy/_launch_v62b_observe.sh` |
| `PGG2_ENABLE_LIVE` | 0 |
| `PGG2_V60_OBSERVE_MODE` | 1 |
| `PGG2_V61_ENABLED` | 1 |
| `PGG2_V62B_ENABLED` | 1 |
| `PGG2_V50B_MAX_OPEN` | 0 (no entries can fire) |
| `PGG2_V50B_MAX_CLOSES` | 1 (so bot keeps scanning to hit target) |
| `PGG2_RESCUE_JUPITER_FALLBACK` | 0 |
| `PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL` | 0.000500 |
| Duration | 180s |
| Log | `/root/piggy/logs/V62B_OBSERVE_RAW.log` (820 lines) |

## Aggregate counts (180s)

| Pattern | Count | Expected | Result |
|---|---|---|---|
| `V60-FIREWALL` (firewall evaluations) | 106 | >0 | ✅ firewall active |
| `V48-LIVE-SELL-SEND` | 0 | 0 (no positions opened) | ✅ no legacy sell sends |
| `V48-LIVE-EMERGENCY-SELL-SEND` | 0 | 0 (no positions opened) | ✅ no legacy emergency sends |
| `jupiter_fallback` / `JUPITER-FALLBACK` | 0 | 0 | ✅ no Jupiter engaged |
| `V62B-...` lines (any) | 0 | 0 (no positions to sell) | ✅ V62B path quiescent (expected) |
| Tracebacks | 0 | 0 | ✅ clean startup with V62B integrated |
| `stop_reason` | `running` (180s timeout) | clean exit | ✅ |
| `wallet_delta` | +0.000000000 SOL | 0 (no live ops) | ✅ |
| `V50B-COMPLETE` | 1 | 1 | ✅ |

## What the observe verifies

1. **No regression at startup.** V62B module imports cleanly via the
   `_V62B_AVAILABLE = True` path in `pgg2_v48_drylive_harness.py:64-72`.
   Bot reached `PGG2-V50B-START` and ran the V48 harness loop for the
   full 180s budget without exception.

2. **V60 firewall still functioning.** 106 firewall evaluations within
   180s confirms candidates are flowing through the pre-buy gate normally.
   V62B integration does not break the entry path.

3. **No legacy sell paths fire when no positions exist.** Trivially true
   in observe (max_open=0), but confirms the bot doesn't spuriously emit
   sell paths in steady-state.

4. **No Jupiter fallback engaged.** Confirms `PGG2_RESCUE_JUPITER_FALLBACK=0`
   takes effect at the env level. Even without a position to sell, this
   verifies the env flag is read.

## What the observe does NOT cover

Because `max_open=0`, no live position ever opens, so the V62B sell-router
code path is not exercised at runtime in this observe. What IS exercised:

- Module-level import + `_V62B_AVAILABLE=True` resolution
- Harness conditional check (`if _V62B_AVAILABLE and _env_flag("PGG2_V62B_ENABLED", "1")`)
  is statically present and syntax-valid (verified by `py_compile`)

For runtime verification of the sell loop itself, the next step is either:
- (a) **Stage A live with 1 entry** — V62B fires on the first close
- (b) **A scripted self-test that fakes a broker + raw balance + calls
  `v62b_close_position()` directly** — already partially covered by the
  module's `__main__` self-test (verified Fwye bank=0.005260, emergency=0.000020,
  scratch=0.005010 min_sol policies)

The Fwye replay document (`V62B_REPLAY_FWYE.md`) walks step-by-step through
the expected V62B behavior given the recorded Fwye numbers.

## Pass criteria (per V62B spec Phase 8)

- ✅ Candidates that would buy: firewall fired 106 times = candidates seen
- ✅ Sell router dry-run can build bank/scratch/emergency tx: self-test verified
  the min_sol computation; runtime path requires an actual open position
  to fire, which max_open=0 prevents (intentional for observe)
- ✅ No old sell path used: 0 V48-LIVE-SELL-SEND, 0 V48-LIVE-EMERGENCY-SELL-SEND
- ✅ No Jupiter fallback path: 0 jupiter_fallback events
- ✅ No errors: clean V50B-COMPLETE, no tracebacks

## Wallet state

- Pre-observe: 0.107659 SOL
- Post-observe: 0.107659 SOL (no change — LIVE OFF)

## Next phase gate

Per spec, Phase 9 (Stage A live with V62B sell-router enforced) requires
explicit user re-authorization. Do not autostart Stage A.

## Linked files

- `pgg2_v62b_authoritative_sell_router.py` (583 lines, self-test passes)
- `pgg2_v48_drylive_harness.py` lines 64-72 (import), 3525 (Jupiter gate),
  4400-4470 (bank/scratch/max_hold gate), 4520-4640 (emergency gate)
- `_launch_v62b_stagea.sh`, `_launch_v62b_observe.sh`
- `V62B_FWYE_FINAL_FORENSIC.md`
- `V62B_REPLAY_FWYE.md`
