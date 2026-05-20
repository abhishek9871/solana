# V58 — V57 Threshold Audit (Phase 1)

Generated: 2026-05-19 09:44 UTC. Parsed from preserved V57 observe + Stage A logs (~22 min total telemetry).

## Hard output

**Candidate supply DOES exist in micro-positive bands.** Tonight's market is NOT "dead" — it just doesn't produce ep ≥ 0.0015 candidates. Lowering to ep ≥ 0.00025 captures 386 events in 22 min.

```
MICRO_POSITIVE_SUPPLY_EXISTS=true
```

## Source data

- `V58_PHASE0_preserved_v57_observe.log` (5 min, 746 NEARMISS-SEEN events)
- `V58_PHASE0_preserved_v57_stagea.log` (~17.5 min, 2007 NEARMISS-SEEN events)
- Total events scanned: **2,753 across 55,735 log lines**
- Unique mints (rough dedup): **285**, average **9.7 events per mint** (V48 re-evaluates each mint many times)

## v48_tracked_ep distribution (n=2753)

| Stat | Value |
|---|---|
| max | **+0.001477** |
| p90 | +0.000394 |
| p75 | -0.000072 |
| p50 (median) | -0.000129 |
| p25 | -0.000150 |
| p10 | -0.000155 |
| min | -0.000544 |
| mean | -0.000014 |
| **positive %** | **18.5%** (509 / 2753) |

**Reading**: The market produces a fat-tailed distribution centered slightly below zero. Top decile is genuinely positive (above +0.000394). The max (+0.001477) is **0.0023 below the V67 0.0015 threshold** — explains why V57 saw 0 promotions at the strict threshold.

## Tier admit counts (raw events, multi-emission per mint inflates)

| Tier threshold | Events | % | Per minute | Per 5-min |
|---|---|---|---|---|
| ep ≥ 0.0015 (V67 hard) | 0 | 0.0% | 0.0 | 0 |
| ep ≥ 0.0010 (V55 floor) | 30 | 1.1% | 1.4 | ~7 |
| ep ≥ 0.0006 | 161 | 5.8% | 7.3 | ~37 |
| ep ≥ 0.0004 | 268 | 9.7% | 12.2 | ~61 |
| **ep ≥ 0.00025 (V58 Tier B)** | **386** | **14.0%** | **17.5** | **~88** |
| ep ≥ 0.00010 | 465 | 16.9% | 21.1 | ~106 |
| ep ≥ 0 (any positive) | 509 | 18.5% | 23.1 | ~116 |

## Frequency math at Tier B (after dedup + filters)

- 17.5 raw events / min × (1 unique per ~10 events) = **~1.75 unique mints/min**
- Across 5 min = **~9 unique Tier-B-eligible mints**
- Across 10 min = **~18 unique Tier-B-eligible mints**

After V47C single-buyer filter (rejected 565/668 = 84% of V57 rejects tonight) and concentration filter, the survivable population is roughly 15-20% → **~1-3 truly entry-able mints per 5-10 min**.

That aligns exactly with the V58 goal of 1-2 winning entries per 5-10 min.

## API call estimate for V53 risk veto (post-promotion only)

Per V58 spec: V53 only called AFTER promotion. With ~1-3 promotions per 5-10 min:
- Tier A + Tier B promotions: ~3-6 candidates per 10 min
- Risk API calls: 3-6 per 10 min = **well under 10 per 5-min budget**

API budget is comfortable.

## Critical: net-wallet-edge math at micro-band

For a 0.005 SOL trade at ep=+0.00025:
- Buy base fee: 0.000005 SOL
- Sell base fee: 0.000005 SOL
- Priority fee × 2: ~0.000010 SOL
- SWQOS tip × 2: 0.000010 SOL
- **Sum non-rent fees**: ~0.000030 SOL
- ep=+0.00025 → after fees: **+0.00022 SOL** — micro-positive

**BUT**: if ATA is NOT closed after sell:
- ATA rent locked: 0.002039 SOL
- Net wallet delta = +0.00022 - 0.002039 = **-0.00182 SOL = LOSS**

If ATA IS closed (rent recovered):
- Net wallet delta = +0.00022 SOL = **WIN**

**Phase 3 (close-account rent recovery) is mandatory for micro-win viability.**

## Recommendation per V58 spec

- ✅ Phase 1 audit completed
- ✅ Micro-positive supply EXISTS (386 events ≥ +0.00025 in 22 min)
- ✅ Tier B threshold (+0.00025) catches real flow
- ✅ Net wallet edge feasibility hinges on ATA close-account (Phase 3)
- Proceed to Phase 2 (net wallet edge model) → Phase 3 (ATA close) → Phase 4 (two-tier router) → Phase 6 observe → Phase 7 Stage A
