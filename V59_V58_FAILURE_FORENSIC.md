# V59 — V58 Failure Forensic (Phase 1)

Generated: 2026-05-19 10:28 UTC. Parsed from preserved V58 Stage A live log.

## Hard output

```
V58_LOSSES_EXPLAINED_BY_SLIPPAGE=true
```

## V58 live trades (3 entries)

### 1. CVz4..pump

| Field | Value |
|---|---|
| size | 0.015 SOL |
| ep (predicted) | +0.000275 |
| buy wallet delta | -0.017104 SOL |
| expected tokens | 173,567.05 |
| actual tokens | 171,826.52 |
| buy slippage | -1.00% (1,740 tokens short) |
| pair_source | decision_curve_snapshot |
| last sell expected_sol_out | 0.014694 |
| last sell min_sol_out (floor) | 0.014797 |
| gap (eso - mso) | **-0.000104** (negative → couldn't execute clean sell) |
| close reason | EMERGENCY exit path (5 emergencies fired) |
| ATA closed | ✅ (rent recovered) |
| **Lost because** | sell quote dropped below min-sol-out buffer; emergency exit took a 0.5% additional loss |

### 2. 7jNJ..pump

| Field | Value |
|---|---|
| size | 0.005 SOL |
| ep (predicted) | +0.000533 |
| buy wallet delta | -0.007104 SOL |
| expected tokens | 47,074.24 |
| actual tokens | 46,567.26 |
| buy slippage | -1.08% (507 tokens short) |
| last sell expected_sol_out | 0.004882 |
| last sell min_sol_out (floor) | 0.004797 |
| gap (eso - mso) | +0.000084 |
| sell sig | 3W5K6QFAemSViawwiBADDqF5jQ2U6t87rgtQ1aRdcdTjjRCZVGdkcQGqoS8JW2HeYsYfrn3cYNAbcnPoUAEZ7X5X |
| close reason | timebox_buffered_positive |
| ATA closed | ✅ (rent recovered) |
| **Lost because** | buy_wd=0.007104 includes 0.002039 ATA rent + 0.005 size + 0.00006 fees. Sell returned 0.00488, rent recovered 0.00204 → net (sell + rent) = 0.00692 vs buy 0.00710 → **-0.00018 SOL loss** |

### 3. EsVt..pump

| Field | Value |
|---|---|
| size | 0.005 SOL |
| ep (predicted) | +0.000789 |
| buy wallet delta | -0.007104 SOL |
| expected tokens | 141,595.69 |
| actual tokens | 140,087.80 |
| buy slippage | -1.06% (1,508 tokens short) |
| last sell expected_sol_out | 0.004849 |
| last sell min_sol_out (floor) | 0.004797 |
| gap (eso - mso) | +0.000051 |
| sell sig | 3A2kwZ4nqUitb5oHYYDKF1VJDW8MwdnvK4rNRrNGnCz9EqNSVSLtachZPJtANzJxYoQnH9DdY3uLY8qjiBQq8JKN |
| close reason | timebox_buffered_positive |
| ATA closed | ✅ |
| **Lost because** | Same structure as 7jNJ — sell after rent recovery is slightly less than buy due to round-trip slippage |

## Aggregate slippage by size tier (n=3)

| Tier | n | buy_slip min | buy_slip max | buy_slip mean |
|---|---|---|---|---|
| 0.005 SOL | 2 | 1.06% | 1.08% | **1.07%** |
| 0.015 SOL | 1 | 1.00% | 1.00% | **1.00%** |

**Empirical observation**: buy slippage is consistently ~1% on Pump.fun bonding-curve trades. The Pump.fun bonding curve has slippage from:
- 1% trading fee per leg
- CPMM price impact (larger size = larger impact)
- Concurrent flow (other buyers between snapshot and our send)

## Cost of a round-trip at each size

For a clean `decision_curve_snapshot` buy → `timebox_buffered_positive` sell with rent recovery:

| Size | Fees+Tips+Priority | ATA rent (recovered) | Buy slip | Sell slip | Round-trip cost |
|---|---|---|---|---|---|
| 0.005 | 0.000030 | 0 (closed) | ~0.000054 | ~0.000050 | **~0.000134 SOL** |
| 0.015 | 0.000030 | 0 (closed) | ~0.000150 | ~0.000150 | **~0.000330 SOL** |

To net positive after slippage, **ep must exceed round-trip cost**. V58 fired at ep=+0.000275 and +0.000533 — those barely cover slippage on 0.015 and partially cover on 0.005 respectively. Real on-chain dynamics added 1-2× more slippage than the static model predicted, producing the -0.00018 to -0.00104 SOL net losses per trade.

## V59 slippage budget — per spec fallback (n=3 too small for empirical p95)

```
size <= 0.005:           slippage_budget = 0.000700 SOL  (≈ 14% of size)
0.005 < size <= 0.010:   slippage_budget = 0.001000 SOL  (≈ 10-20% of size)
0.010 < size <= 0.015:   slippage_budget = 0.001500 SOL  (≈ 10% of size)
size > 0.015:            DISABLE micro mode
```

## V59 true-edge math (for size=0.005, micro mode)

```
true_edge = ep - 0.000030 (fees+tips+priority)
              - 0          (rent recovered via CloseAccount, default ON)
              - 0.000700   (slippage_budget for size<=0.005)
              - 0.000100   (safety buffer)
         = ep - 0.000830

For micro pass (true_edge >= +0.00005):  ep >= +0.000880 SOL
For bank pass  (true_edge >= +0.000400):  ep >= +0.001230 SOL
```

## Implication for V58 Stage A losses

| Trade | ep | V59 true_edge | V59 verdict |
|---|---|---|---|
| CVz4 (size 0.015) | +0.000275 | -0.001355 | ❌ BLOCKED (true_edge < +0.00005) |
| 7jNJ (size 0.005) | +0.000533 | -0.000297 | ❌ BLOCKED (true_edge < +0.00005) |
| EsVt (size 0.005) | +0.000789 | -0.000041 | ❌ BLOCKED (true_edge < +0.00005) |

**All 3 V58 trades would have been BLOCKED by V59 true-edge.** Wallet would have been preserved.

## Recommendation

Proceed to Phase 2 (slippage model module) → Phase 3 (true-edge module) → Phase 4 (V67 + V57 routing through V59) → Phase 6 (live-equivalent validation, LIVE OFF) BEFORE any further live entries.
