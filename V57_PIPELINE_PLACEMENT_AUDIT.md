# V57 Pipeline Placement Audit — 2026-05-19 08:56 UTC

## Hard output

```
V56_HOOK_TOO_LATE=true
```

## Why V56 never fires (positional bug)

V56 hook lives at `pgg2_v50b_stagea_live.py:692`, inside `_v50b_retarget_buy()`.
That function is the monkey-patched `retarget_buy_min_tokens` — V48 calls it
only AFTER it has already decided to buy and is preparing the buy tx.

V48's "decide to buy" path runs through V67 final gate. V67 blocks the buy
BEFORE `retarget_buy_min_tokens` is ever called. Therefore V56 hook never sees
the candidate.

```
V48 candidate emit
   |
   v
V47B/C/D/E/F/G/H/I gate stack  -> passes
   |
   v
V67 final gate (expected_pnl check)
   |
   X  block on expected_pnl_below_required  <-- 99.99% blocked here
   |
   v  (only if V67 passes)
V48 calls broker.retarget_buy_min_tokens
   |
   v
v50b _v50b_retarget_buy monkey-patch     <-- V56 HOOK HERE (too late)
   |
   v
SWQOS send
```

## Pipeline counts from V56 runs tonight

### Run 1: pgg2_v55_stagea_20260519_070228.log (30 min, 07:02 → 07:32)
- V67 early-blocks: **5,437**
- V47C gate evaluations: **2,854**
- V67 curve updates: **5,099**
- V48 LIVE-BUY emissions: **0**
- V56 hook firings: **0**
- Buys executed: **0**

### Run 2: pgg2_v55_stagea_20260519_083405.log (9.5 min, 08:34 → 08:43)
- V67 early-blocks: **1,443**
- V47C gate evaluations: **1,335**
- V67 curve updates: **2,869**
- V48 LIVE-BUY emissions: **0**
- V56 hook firings: **0**

### Combined: ~40 min runtime
- V67 early-blocks: **6,880** (~172/min, ~3/sec)
- V47C evaluations: **4,189**
- V67 curve updates: **7,968**
- **Zero V48 buy emissions, zero V56 hook firings, zero buys**

## Top blocker — `expected_pnl_below_required:7`

Every V67 early-block log line on tonight's data has this shape:

```
PGG2-V67-EARLY-BLOCK mint=<MINT>..pump reason=no_selectable_size
    best_size=0.0050 best_expected_pnl=-0.000128
    blockers=expected_pnl_below_required:7
```

`:7` = all 7 size tiers (0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.075 SOL) fail
the `expected_pnl >= required_pnl` check.

`best_expected_pnl` values seen tonight are in the range **-0.000089 to -0.001463
SOL**, with the smallest-magnitude misses around **-0.000089 to -0.000150 SOL**
(i.e. just below the `PGG2_V67_MIN_EXPECTED_PNL=0.001500` threshold).

## Conclusion

V47/V48 candidate supply is HEALTHY — 172 candidates/min reaching V67. The
problem is positional, not supply.

- **V47/V48/V47C/V67 stack: ✅ working as designed**
- **Candidate supply: ✅ abundant (~10,000/hour)**
- **V67 gate: ✅ correctly rejecting negative-EV setups**
- **V56 hook position: ❌ AFTER V67, so it never sees the near-misses**

## V57 fix

Hook into V67's early-block point (where `expected_pnl_below_required` fires)
and emit a `V67NearMiss` event. Add near-misses to a watchlist. On each local
curve update, re-evaluate. If `expected_pnl` flips positive AND stress non-
negative AND curve gradient positive — PROMOTE the candidate, run V53 risk
veto, then send via SWQOS.

This is what bridges supply to entries without loosening V67 thresholds.

## V67 early-block log point — to be located in v48 harness

Source file: `/root/piggy/pgg2_v48_drylive_harness.py` (422 KB)

The log line `PGG2-V67-EARLY-BLOCK mint=... reason=no_selectable_size
best_size=... best_expected_pnl=... blockers=expected_pnl_below_required:7`
is emitted from V48 when the V67 final gate fails to find any selectable size.
Phase 2 will grep for this exact log emission to find the insertion point for
the `V67NearMiss` event export.
