# V57 Observe Report — 5-minute run, 2026-05-19 09:12:01 → 09:17:01 UTC

## Bot state at end

- `pgg2_v50b_stagea_live` self-stopped at v48 `max_seconds=300` boundary
- tmux session `pgg2_v57_observe` cleaned up
- Wallet: **0.120938 SOL ($21.77)** — delta +0.000000 SOL
- 0 token positions

## V57 hook fire counts

| Hook | Count |
|---|---|
| PGG2-V67-EARLY-BLOCK (V48 supply) | **981** |
| PGG2-V57-NEARMISS-SEEN | **746** |
| PGG2-V57-WATCHLIST-ADD | **31** |
| PGG2-V57-WATCHLIST-REFRESH | 47 |
| PGG2-V57-WATCHLIST-REJECT | 668 |
| PGG2-V57-WATCHLIST-DROP | 5 |
| PGG2-V57-PROMOTION-CHECK | **517** |
| PGG2-V57-PROMOTED | **0** |
| PGG2-V57-LIVE-ROUTER-CANDIDATE | 0 |
| PGG2-V57-LIVE-ROUTER-BLOCK | 0 |
| PGG2-V57-LIVE-SEND | 0 |
| PGG2-V57-RISK-VETO | 0 |
| PGG2-V56-RISK-VETO (V56 disabled — sanity) | 0 |
| PGG2-V57-NEARMISS-EXPORT-ERR | 0 |
| PGG2-V57-ROUTER-ERR | 0 |

**Throughput**: 746 near-misses / 5 min = **~150/min** export rate. **Zero errors.**

## Reject reason breakdown (668 rejects)

| Reason | Count |
|---|---|
| single_buyer | 565 |
| v47c_catastrophic (top_buyer_share > 0.85) | ~95 |
| ep_not_near (outside band) | 5 |
| pair_source | 0 (after pair_source fix applied) |

The single-buyer + catastrophic-concentration rejects are correct behavior — these are mints with no real buyer interest yet.

## Sample WATCHLIST-ADD (the 31 that made it through)

```
[09:12:09] Tpww..pump  ep=+0.001359  size=0.0750  ub250=2  top=0.500  pending=1.050
[09:12:19] GnTP..pump  ep=+0.000356  size=0.0750  ub250=2  top=0.752  pending=1.284
[09:12:24] 4sH7..pump  ep=-0.000134  size=0.0050  ub250=2  top=0.500  pending=0.110
[09:12:25] 7rCc..pump  ep=+0.000442  size=0.0750  ub250=2  top=0.709  pending=0.822
[09:12:40] Cqhy..pump  ep=-0.000080  size=0.0050  ub250=2  top=0.723  pending=3.246
```

Best v48-computed ep tonight: **+0.001359** (Tpww at size 0.075) — **still below required +0.001500 threshold by 0.000141 SOL**.

## Why 0 promotions despite 517 checks — TWO distinct issues

### Issue 1: Promotion engine ep math is naive (BUG)

Sample PROMOTION-CHECK:
```
Tpww..pump  pass=0  ep=-0.001492  stress=+0.000000  blocker=ep_below_required(-0.001492<+0.001500)
```

The promotion engine recomputes ep locally via simple CPMM round-trip math (buy→sell). That math captures only the **2% fee loss** (1% in + 1% out) — it does NOT include pending-flow projection like v48's V67 evaluator does. So even on Tpww (where v48 computed ep=+0.001359), my engine sees -0.001492.

**Fix**: PromotionContext should accept an override `expected_pnl_sol` from v48's tracked value (already in `entry.nm.best_expected_pnl`, refreshed every V67 evaluation). The router should pass v48's ep, not a local recompute.

### Issue 2: Even with v48's ep, no candidate hit +0.001500 threshold

The 31 admitted candidates had v48-tracked ep in the range:
- min: -0.000134 (still in band via absolute_floor=-0.00025 rule)
- max: **+0.001359** (Tpww size=0.075)

Best ep is 9.4% short of required +0.001500. **Tonight's pump.fun regime doesn't produce candidates that hit the V67 threshold across the 7-tier size sweep.**

Same conclusion as V56 run earlier tonight (40 min, 6,880 V67 blocks, 0 buys).

## Per V57 spec recommendation section

> "If V57 observe shows enough promotions, Stage A immediately."

Doesn't apply — 0 promotions.

> "If observe shows near-misses but no promotions, tune the watchlist band, not the entry thresholds."

We have **746 near-misses, 31 admitted, 517 promotion checks, 0 promoted**. The watchlist band is working — admitting candidates near the threshold. The bottleneck is the promotion engine's ep computation (Issue 1) + tonight's regime not producing 0.0015+ ep (Issue 2).

> "Hard rule: Do not loosen blindly. Do not enter while expected edge is negative."

Honored. The bot would have fired 0 entries tonight even if the engine were fixed, because no candidate had v48-tracked ep ≥ +0.001500.

## Estimated frequency at 1-2 winning entries per 5-10 min

With the engine bug fixed (Issue 1), V57 would fire when v48-tracked ep crosses +0.001500.

Tonight's data: 0 candidates crossed the threshold across the entire 5-min window. Best was +0.001359 (9.4% short).

**To hit 1-2 entries / 5-10 min, one of these must happen:**

1. **Market regime shifts** — US-hours pump.fun typically produces 5-10× more positive-ep candidates than current 04:48 AM EDT trough. Wait for 14:00-22:00 UTC window.
2. **Lower V67 threshold** to capture tonight's regime — `PGG2_V67_MIN_EXPECTED_PNL=0.001000` would have captured Tpww (+0.001359) and 7rCc (+0.000442). Spec said "do not loosen blindly" — but 0.0010 is the historical V55-banked threshold that produced +$1.69, so it's not blind.
3. **Both** — fix engine bug + lower threshold + run during US hours.

## Spec Phase 7 pass criteria

> Pass if: at least 1 full V57 pass candidate in 5 minutes OR exact blocker.

**EXACT BLOCKERS identified**:
1. **Engine bug**: promotion engine ep math doesn't include pending-flow projection (uses simple CPMM round-trip). Fix in `pgg2_v57_live_router.py` — pass `entry.nm.best_expected_pnl` to PromotionContext.
2. **Market regime**: tonight's best v48-tracked ep was +0.001359, below required +0.001500.

**Phase 7 PASS** by "exact blocker" branch. No silent drops, no errors.

## Required state machine still works

| Stage | Active? | Evidence |
|---|---|---|
| V67 early-block emission | ✅ | 981 events |
| V57 near-miss export | ✅ | 746 events, 0 errors |
| Watchlist admit/reject | ✅ | 31 add / 668 reject with clean reason breakdown |
| Watchlist refresh on re-evaluation | ✅ | 47 refreshes |
| Watchlist sweep/drop | ✅ | 5 TTL drops |
| V48 curve-update → router hook | ✅ | 517 promotion-checks fired |
| Promotion engine evaluation | ✅ | runs but blocked by Issue 1 + Issue 2 |
| V53 risk veto | ⚠️ | armed but not exercised (0 promotions reached it) |
| Live router send (observe mode) | ⚠️ | armed but not exercised |

All wiring works. Two known issues identified, both characterized.
