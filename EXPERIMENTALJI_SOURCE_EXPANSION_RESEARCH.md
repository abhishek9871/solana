# Experimentalji Source Expansion Research

Date: 2026-05-07
Scope: expand mint coverage without touching the PGG2 production candidate.

## Current Baseline

`experimentalji.py` is a byte-for-byte copy of the current `PGG2.py` production candidate at the time of this research. The running bot should not be changed while this work is explored.

The current hot path is pump.fun only:

- `birth_first_sniper.py` listens to Solana Tracker `shredSubscribe` with `accountRequired=[pump.fun program]`.
- `programSubscribe` keeps a pump.fun BondingCurve cache.
- `experimentalji.py` receives normalized `PumpEvent` objects and applies the same entry/exit lanes.
- `pgg2_direct_pump.py` already has direct builders for pump.fun bonding curve and PumpSwap execution.

## Research Sources

Primary sources checked:

- Pump program docs: https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md
- PumpSwap docs: https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_SWAP_README.md
- Raydium LaunchLab docs: https://docs.raydium.io/raydium/build/ts-sdk-demo/launchlab
- Raydium LaunchLab SDK source: https://github.com/raydium-io/raydium-sdk-V2/tree/master/src/raydium/launchpad
- Meteora DBC docs: https://docs.meteora.ag/overview/products/dbc/what-is-dbc
- Meteora DBC curve config: https://docs.meteora.ag/overview/products/dbc/curve-configuration
- Helius transactionSubscribe docs: https://www.helius.dev/docs/enhanced-websockets/transaction-subscribe
- Solana programSubscribe docs: https://solana.com/docs/rpc/websocket/programsubscribe
- Solana logsSubscribe docs: https://solana.com/docs/rpc/websocket/logssubscribe
- Birdeye new pair stream: https://docs.birdeye.so/docs/subscribe_new_pair
- DexScreener API reference: https://docs.dexscreener.com/api/reference
- Solana Tracker shredSubscribe docs: https://docs.solanatracker.io/solana-rpc/websockets/shredsubscribe
- Solana Tracker Raptor overview: https://docs.solanatracker.io/raptor/overview

## Main Finding

The right expansion is not another parameter tweak. It is a source-router architecture:

1. Add more mint sources.
2. Decode each source with its own venue adapter.
3. Normalize every venue into the same internal event shape.
4. Reuse the existing PGG2 feature engine only after price, side, fee, and route truth are normalized.

Blindly applying pump.fun thresholds to every new venue would create false positives because each venue has different curve math, fees, migration behavior, account layout, and latency profile.

## Adapter Contract

Each new venue should implement this minimum contract:

```text
VenueAdapter
  name
  program_ids()
  parse_transaction(tx) -> NormalizedTradeEvent[]
  parse_account_update(account) -> VenueCurvePoint | None
  price_for_event(event, curve_cache) -> float | None
  quote_buy(mint, sol_in) -> Quote
  quote_sell(position) -> Quote
  cost_model() -> VenueCosts
```

Normalized events must include:

- `venue`
- `mint`
- `side`
- `sol`
- `token_amount`
- `curve_price`
- `slot`
- `ts_ms`
- `signer`
- `pool_or_curve`
- `complete_or_migrated`

Only after this normalization should `experimentalji.py` run `priced_snap`, `birth_fanout`, `spark3`, `reclaim`, or other lanes.

## Priority 1: PumpSwap Graduation And Post-Graduation Pools

Why first:

- Pump docs confirm pump.fun migrates completed curves to PumpSwap.
- PumpSwap docs confirm program id `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`.
- We already have direct Pump/PumpSwap execution code.
- This is the lowest-risk expansion because it stays within the pump ecosystem.

Technical work:

- Subscribe/parse PumpSwap AMM transactions, not just bonding curve transactions.
- Track newly migrated pools from pump `migrate` and PumpSwap pool creation.
- Build a PumpSwap pool cache from pool base/quote vault balances.
- Normalize PumpSwap swaps into the same event stream.
- Keep PumpSwap entries behind `EXPERIMENTALJI_ENABLE_PUMPSWAP_SOURCE=1`.

Validation:

- Ingest-only for 30 to 60 minutes.
- Measure post-pool-create 30s, 60s, 180s max multiplier.
- Compare against current PGG2 missed-moonshot list.
- Enable quote-only dry-live before any send path.

Risk:

- PumpSwap is constant product AMM, not bonding curve. Existing pump curve move thresholds cannot be trusted until normalized against pool reserves.

## Priority 2: Raydium LaunchLab / LetsBONK

Why second:

- Raydium docs identify LaunchLab as bonding-curve infrastructure and list mainnet program id `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`.
- Local legacy code already references this program as BONK/LaunchLab.
- Raydium SDK exposes Launchpad pool layouts with fields like `virtualA`, `virtualB`, `realA`, `realB`, `totalFundRaisingB`, and `tradeFeeRate`.

Technical work:

- Build a LaunchLab adapter using the Raydium SDK layout, not guesswork.
- Decode LaunchLab buy/sell transactions from shreds or transactionSubscribe.
- Maintain pool state cache from LaunchpadPool accounts.
- Normalize virtual reserve price into `curve_price`.
- Use quote-only first. Direct execution is separate and must not rely on pump.fun instruction builders.

Validation:

- Ingest-only capture of LaunchLab mints.
- Calculate same post-birth multipliers used for pump.fun.
- Only enable entries if LaunchLab winners separate from losers in live data.

Risk:

- Migration target can be Raydium AMM/CPMM. Execution and pricing must switch by migration type.
- The PGG2 pump lanes may need venue-specific thresholds because LaunchLab curve parameters differ.

## Priority 3: Meteora DBC / Bags-Style DBC Launches

Why third:

- Meteora DBC docs describe a permissionless launch protocol with virtual pools that graduate to DAMM v1 or DAMM v2.
- DBC mainnet program id is `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`.
- DBC supports configurable multi-segment curves, dynamic fees, creator fees, Token-2022, and migration thresholds.

Technical work:

- Use Meteora DBC SDK/IDL for account decoding.
- Parse `VirtualPool` and `PoolConfig` state.
- Implement DBC price from current curve segment, not a pump-style reserve shortcut.
- Normalize DBC trades into the event stream.
- Quote-only first; direct execution must be DBC-specific or routed through a no-extra-fee path if available.

Validation:

- Ingest-only DBC mints.
- Calculate post-creation max multipliers.
- Separate by curve config because one DBC curve can behave very differently from another.

Risk:

- DBC is more configurable than pump.fun. A single entry model across all DBC configs will overfit or break.
- Token-2022 transfer fees and dynamic fees must be included in dry-live accounting.

## Priority 4: Helius transactionSubscribe As Supplemental Feed

Why useful:

- Helius supports `transactionSubscribe` with `accountInclude`, `accountRequired`, and processed commitment.
- It allows broad filters up to large account lists.

Use:

- Redundant discovery and parser validation, not the only hot path.
- Compare arrival timestamps against Solana Tracker shreds.
- Catch venues not available in the current Solana Tracker filter setup.

Risk:

- It is still provider-dependent. It must be measured against the existing shred feed before it is trusted for entries.

## Priority 5: Birdeye And DexScreener Radar

Why useful:

- Birdeye has a real-time new-pair WebSocket.
- DexScreener has pair/token endpoints and rate limits documented.

Use:

- Radar/backfill/missed-moonshot detection.
- Building the "what did we miss?" dataset.
- Not for sub-second entry decisions.

Risk:

- These APIs are indexed and can lag the actual chain. They must not sit on the critical entry path.

## Implementation Plan

1. Keep `PGG2.py` untouched.
2. Work only in `experimentalji.py` plus new adapter modules.
3. Add `source_router.py` with feature flags for each source.
4. Start with PumpSwap source ingestion because execution support already exists.
5. Add LaunchLab adapter in ingest-only mode.
6. Add DBC adapter in ingest-only mode.
7. Run multi-source capture and build fast analytics:
   - mints by source per 10 minutes
   - entries the existing lanes would take
   - post-entry max multiplier
   - loser drawdown
   - fee-adjusted dry-live PnL
8. Only promote a source to quote/dry-live when it proves edge in live captured data.
9. Only promote a source to live when quote/dry-live matches the direct execution model.

## Fast Validation Requirement

Replay of full tapes is too slow. For source expansion we should use indexed, event-level validation:

- Stream raw events into newline JSON.
- Convert once into compact per-mint feature timelines.
- Store per-mint arrays in a small SQLite or parquet cache.
- Run threshold/entry simulations against the cache, not the raw full tape.

Target runtime:

- 30 to 60 minute capture ingest: live time.
- Post-capture analytics: under 10 seconds for common sweeps.
- Config sweep: under 60 seconds.

## Go / No-Go Rule

A source is not live-ready unless:

- It increases executable entries per hour.
- It remains positive after venue fees, platform fees, slippage, rent/ATA costs, and failed-send overhead.
- It does not depend on an indexed API for entry timing.
- It has quote/dry-live logs that match the direct live cost model.
- It has a kill switch and position recovery behavior for that venue.

