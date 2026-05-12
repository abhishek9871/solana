# RELEASE_V36C3_VALIDATION

Parsed dry-live SLA log: `/root/piggy/logs/pgg2_v36c3_sla20_20260512_071443.log`

## Run window

| Field | Value |
|---|---|
| Run start | `2026-05-12 07:14:43 UTC` |
| Last PIGGY-STATUS | `2026-05-12 07:45:37 UTC` |
| Wallclock span | ~30 min 54 s |
| Strict 20-min window | 07:14:43 → 07:34:43 (held 8 of the 10 entries) |
| Run end reason | manual stop after 10th entry banked |

## Headline result

```
W/L = 10/0
realized_all_in = +0.037778 SOL
legacy_realized = +0.014478 SOL
pnl_model_version = v33_route_aware
risk_worker_entry_snapshot_bank fires = 10/10
mode = QUOTE  (real live OFF)
```

## All 10 closes

| # | timestamp (UTC) | mint | close reason | all_in_pnl |
|---|---|---|---|---:|
| 1 | 2026-05-12 07:14:50 | 3qts..pump | `risk_worker_entry_snapshot_bank` | +0.003697 |
| 2 | 2026-05-12 07:20:01 | JLEj..pump | `risk_worker_entry_snapshot_bank` | +0.007169 |
| 3 | 2026-05-12 07:20:17 | HWec..pump | `risk_worker_entry_snapshot_bank` | +0.001275 |
| 4 | 2026-05-12 07:21:44 | Dtbb..pump | `risk_worker_entry_snapshot_bank` | +0.000944 |
| 5 | 2026-05-12 07:25:29 | 2794..pump | `risk_worker_entry_snapshot_bank` | +0.003821 |
| 6 | 2026-05-12 07:31:01 | 9v8N..pump | `risk_worker_entry_snapshot_bank` | +0.001855 |
| 7 | 2026-05-12 07:32:17 | 6CMj..pump | `risk_worker_entry_snapshot_bank` | +0.007600 |
| 8 | 2026-05-12 07:33:19 | HM19..35e9 | `risk_worker_entry_snapshot_bank` | +0.002160 |
| 9 | 2026-05-12 07:40:20 | FDTg..pump | `risk_worker_entry_snapshot_bank` | +0.008385 |
| 10 | 2026-05-12 07:45:13 | 4uwU..pump | `risk_worker_entry_snapshot_bank` | +0.000872 |

**Sum of all_in PnLs:** +0.037778 SOL.

## Safety counters

| Counter | Value |
|---|---:|
| `PGG2-DRYLIVE-PILOT-BUY` | 0 (no primary fires this window) |
| `PGG2-SCALP-BUY` | 10 |
| `PGG2-ENTRY-SNAPSHOT-BANK` | 10 |
| Scout-size invariant blocks | 3 (raw_momentum candidates whose buy quote was built at a different scout_sol) |
| `PGG2-RISK-CLOSE-FAIL` | **0** |
| Tracebacks | **0** |
| Token mismatch (`PGG2-POSITION-TOKEN-MISMATCH-FATAL`) | **0** |
| `PGG2-PILOT-PREENTRY-BLOCK blocker=stale_quote` | 16 (correctly blocked stale-quote candidates from entering) |
| QUOTE-LATENCY events with `in_flight ≥ 2` for any mint | **0** |
| Negative `all_in_pnl` SELL events | **0** |
| Real-live confirm env (`PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK`) | **0** |

## Launcher and env

- Launcher: `pgg2_v33_verify_pilot.sh` (calls `start_pgg2_v30_shadowlab_drylive.sh` → `start_pgg2_attack_paper.sh`)
- Key env (verified live on the running bot via `/proc/<pid>/environ`):
  - `PGG2_EXECUTION_MODE=quote`
  - `PGG2_ACTUAL_ENTRY_MASTER_ENABLED=1`
  - `PGG2_DRYLIVE_PILOT_ENABLED=1`
  - `PGG2_DRYLIVE_PILOT_MAX_ENTRIES=3`
  - `PGG2_DRYLIVE_PILOT_SOL=0.015`
  - `PGG2_PREENTRY_MIN_ALL_IN_PNL_SOL=0.00150`
  - `PGG2_SCALP_ENABLED=1`
  - `PGG2_SCALP_MAX_ENTRIES=10`
  - `PGG2_SCALP_SOL=0.015`
  - `PGG2_SCALP_MIN_ALL_IN_PNL_SOL=0.00060`
  - `PGG2_SCALP_MAX_QUOTE_AGE_MS=750`
  - `PGG2_SCALP_BANK_MIN_PNL_SOL=0.00020`
  - `PGG2_SCALP_CLAMP_MAX_LOSS_SOL=0.00030`
  - `PGG2_SCALP_TIMEBOX_MS=3000`
  - `PGG2_SCALP_ABS_MAX_HOLD_MS=3000`
  - `PGG2_SCALP_SESSION_LOSS_CAP_SOL=0.0015`
  - `PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=1` (default; not set explicitly in launcher)
  - `PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0` (default; blocks ESB in live mode)
  - `PIGGY_MAX_OPEN_POSITIONS=5`
  - `PGG2_LIVE_CONFIRM` **unset** (real live disabled)

## Statement

Real-live remained OFF for the entire v36c-3 run. Every entry was a quote-mode dry-live position. Every close went through `broker.close()` in quote-only branch (`self.quote_only and self.quote_shadow_positions`), reusing the broker's `_recent_sell_quotes` cache (populated by `build_sell` per the v34-P1 in_flight dedup fix). No tx was signed or sent.

**Summary line:** `10W / 0L  +0.037778 SOL realized_all_in  risk_worker_entry_snapshot_bank 10/10  mode=QUOTE`.
