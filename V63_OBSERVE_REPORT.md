# V63 — Observe Report (Phase 6)

Generated: 2026-05-19 17:33 UTC. 3-minute LIVE-OFF observe to verify V63
wiring loads cleanly and does not affect entry logic.

## Setup

| Knob | Value |
|---|---|
| Launcher | `/root/piggy/_launch_v62b_observe.sh` |
| `PGG2_ENABLE_LIVE` | 0 |
| `PGG2_V60_OBSERVE_MODE` | 1 |
| `PGG2_V61_ENABLED` | 1 |
| `PGG2_V62B_ENABLED` | 1 |
| `PGG2_V63_ENABLED` | (defaults to 1) |
| `PGG2_V50B_MAX_OPEN` | 0 (no entries can fire) |
| `PGG2_V50B_MAX_CLOSES` | 1 (so bot keeps scanning) |
| `PGG2_RESCUE_JUPITER_FALLBACK` | 0 |
| Duration | 180s |
| Log | `/root/piggy/logs/V63_OBSERVE_RAW.log` (936 lines) |

## Aggregate counts

| Pattern | Count | Expected | Result |
|---|---|---|---|
| `V60-FIREWALL` (firewall evaluations) | 102 | >0 | ✅ firewall active |
| `V48-LIVE-SELL-SEND` (legacy) | 0 | 0 | ✅ |
| `V48-LIVE-EMERGENCY-SELL-SEND` (legacy) | 0 | 0 | ✅ |
| `jupiter_fallback` | 0 | 0 | ✅ |
| `PGG2-V62B-` (any) | 0 | 0 (no closes) | ✅ |
| `PGG2-V63-` (any) | 0 | 0 (no buy events) | ✅ |
| Tracebacks | 0 | 0 | ✅ no V63 import errors at startup |
| `stop_reason` | `running` (180s timeout) | clean exit | ✅ |
| `wallet_delta` | +0.000000 SOL | 0 (LIVE OFF) | ✅ |
| `V50B-COMPLETE` | 1 | 1 | ✅ |
| candidates_passed | 3 | >0 | ✅ candidates flowing through gates |

## What this verifies

1. **No regression at startup.** V63 module imports cleanly via
   `_V63_AVAILABLE = True` path. Harness reaches `PGG2-V50B-START` and
   runs full 180s budget without exception.

2. **V60 firewall still functioning.** 102 firewall evaluations in 180s.
   V63 integration doesn't break the entry path.

3. **V63 hooks dormant when no entries fire.** Because `max_open=0`, no buy
   sent → no `PGG2-V63-WALLET-BEFORE-BUY` snapshot → no
   `PGG2-V63-POST-SELL-CLEAN-CLOSE-*` → no `PGG2-V63-FINAL-PNL`. This is the
   correct behavior. V63 only emits logs when there's a real trade to close.

4. **Final wallet delta = 0 SOL** confirms no rogue sends fired.

## What this observe does NOT cover

Because `max_open=0`, V63's runtime path is not exercised. Coverage is:

- ✅ Module import + `_V63_AVAILABLE=True` resolution
- ✅ Harness conditional checks (`if _V63_AVAILABLE and _env_flag("PGG2_V63_ENABLED", "1")`)
  syntactically valid
- ❓ V63 close-account RPC interaction with broker
- ❓ V63 `compute_final` end-to-end with real wallet snapshots
- ❓ `PGG2-V63-FINAL-PNL` log emission

For runtime verification of (❓), Phase 7 (Stage A live, 1 entry) is required.
The Phase 5 replay document walks through expected V63 behavior given the
RUN1 recorded data.

## Pass criteria (per V63 spec Phase 6)

- ✅ No errors at startup or runtime
- ✅ V63 module imports
- ✅ V62B sell router (which V63 wraps) is in place
- ✅ No old V48 sell paths fired
- ✅ No Jupiter fallback fired

All Phase 6 criteria PASS. Ready for Phase 7 (Stage A live) — but only with
explicit user re-authorization per the strict V63 spec rules.

## Wallet state

| | Pre-observe | Post-observe |
|---|---|---|
| Wallet | 0.107099033 SOL | 0.107099033 SOL |
| Token ATAs (residual rent) | 21 zombies × ~0.00207 SOL = ~0.0436 SOL | unchanged (Phase 6 does not rescue) |

The 21 zombie ATAs from prior pre-V62B runs are still trapped (~$7.84). V63
will prevent FUTURE zombies but does not retroactively close historical
ones — that requires running `rescue_all_stuck.py` with a CloseAccount loop
(out of scope for V63 spec).

## Linked

- `V63_V62B_RENT_FORENSIC.md` (Phase 1)
- `pgg2_v63_post_sell_clean_close.py` (Phase 2)
- `pgg2_v63_final_pnl.py` (Phase 4)
- `V63_REPLAY_ON_V62B_STAGEA.md` (Phase 5)
- `pgg2_direct_pump.py:build_close_account` (Phase 2 broker patch)
- `pgg2_v48_drylive_harness.py` V63 hooks (Phases 3 + 4)
