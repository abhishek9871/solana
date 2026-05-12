# RELEASE NOTES — v36c-3 (Entry-Snapshot-Bank dry-live winner)

Tag: `v36c3-drylive-10w0l`
Branch: `main`

## What v36c-3 is

A frozen restore point for the PGG2 sniper bot that produced **10 wins / 0 losses, +0.037778 SOL realized_all_in** in a dry-live SLA pilot. Every close was a `risk_worker_entry_snapshot_bank`. No close-fail, no traceback, no token mismatch, no concurrent broker `in_flight ≥ 2` for any pilot mint. Real-live remained OFF throughout. Validation details: `RELEASE_V36C3_VALIDATION.md`. File checksums: `RELEASE_V36C3_CHECKSUMS.txt`.

This is a **dry-live release**. Real-live execution requires an additional live-equivalence implementation before any unattended size; the smoke procedure is one-entry only and documented below.

## Why v33 route-aware cost model replaced the old fixed overhead

The pre-v33 pipeline subtracted a fixed `quote_roundtrip_overhead_sol = 0.00235 SOL` from every close, treating ATA rent as a permanently sunk cost. In reality the pump.fun direct path emits `close_token_account` on sell, which **recovers** the rent. The old formula made round-trip-zero positions look like guaranteed losses and pushed the bot away from valid edges. The v33 route-aware accounting:

- splits cost into `gross_quote_pnl` (already in the quote) and `extra_overhead_not_in_quote` (tx fees only, ~0.000020 SOL for `pump_bc`);
- tags every record with `cost_model_route` + `cost_model_confidence`;
- emits both `all_in_pnl` and `legacy_pnl` so the transition is auditable.

All SLA gates (holdout, ablation, oracle) operate on `all_in_pnl`.

## What Entry Snapshot Bank means

When the pre-entry buy quote + immediate reverse sell quote yield an `all_in_immediate_pnl` already above the bank threshold (`+0.00020` for scalp, `+0.00060` for primary), the bot **closes the position immediately using the same locked sell quote** that was already cached on the broker side (per the v34-P1 in-flight dedup fix). No new network sell quote is issued post-open.

Mechanism (broker side):
- `DirectPumpQuoteBroker.build_sell` populates `broker._recent_sell_quotes[mint]` on every successful sell quote.
- `RaptorLiveBroker.close()` for a risk-owned mint reads from this cache first via `get_recent_sell_quote(max_age_ms=1500)`, bypassing the parallel network call.
- The scheduled close fires through the existing `RiskWorker._schedule_close` path so the close-skip idempotency + features-dict invariants remain enforced.

This eliminates the race window that caused the v36b 2xty loss (`-0.002098 SOL` while waiting ~700 ms for the next risk-worker quote) and the v36c-1 5D5f loss (`-0.005442 SOL` from the same race on the primary path).

## Why scalp rules are no-hold bank/scratch

The dry-live data shows that for the scalp band (entry `all_in_immediate_pnl ∈ [+0.00060, +0.00150)`), the only causally safe exit is to bank the entry quote and exit immediately. Holding the position waits on the risk worker's next quote (typical 300 ms cache + 700-900 ms network = ~1 s), during which fast mints crash through any clamp tighter than the QuoteManager refresh cycle. **Scalp policy is therefore: entry snapshot bank or do not enter.** The risk worker's bank/clamp/timebox/abs-max thresholds still apply as a backstop if for some reason the entry snapshot bank does not fire.

## What the scout-size invariant blocks

Some lanes (e.g. `rug_bounce_buy`) construct `plan.scout_sol` at a non-default size (0.010 SOL). The shadow lab builds its buy + reverse-sell quote pair at `plan.scout_sol`, so the resulting `quote_tokens` and `immediate_reverse_out` correspond to that size. If scalp then opens a position at `PGG2_SCALP_SOL=0.015` while inheriting those quote_tokens, the position's `cost` is 0.015 but the tokens-held are sized for 0.010 — selling them recovers only ~0.010 SOL, producing a structural −0.004 SOL loss regardless of price action.

**Fix:** `_try_scalp_entry` now hard-blocks any record where `record["scout_sol"] != PGG2_SCALP_SOL` with `PGG2-SCALP-PREENTRY-BLOCK blocker=scout_size_mismatch`. Caught and prevented 3 entries in the v36c-3 run.

## What QuoteManager does

Centralised quote service for runtime sell quotes used by the risk worker + close path. Key features:
- Single in-flight per `(mint, side, amount, pair_source)` key.
- Cache TTL `PGG2_QUOTE_MGR_CACHE_TTL_MS=300`.
- Explicit `quote_status ∈ {fresh_network_quote, cache_hit, rate_limited_no_quote, error}` so the risk worker cannot silently treat a missing quote as fresh.
- Active-position quote exclusivity: when a position is `mark_risk_owned`, shadow-lab future_sells + delayed-scanner skip with `PGG2-QUOTE-MGR-RISK-OWNED-BLOCK` so no parallel network sells race the risk worker.

## What RiskWorker does

A dedicated thread, owner of quote-based exits for risk-managed lanes. Reads sell quotes via QuoteManager. Computes `all_in_pnl` per-tick using the broker's `quote_all_in_pnl`. Fires close on any of: bank, scratch (deteriorating positive), clamp, timebox, absolute_max_hold. Bank/clamp/timebox/abs-max are passed per-position via a `policy` dict at `add_position()` time, so primary and scalp rules use their own thresholds without process-global env vars. Idempotent: a second close request for the same mint emits `PGG2-RISK-CLOSE-SKIP reason=already_scheduled` or `position_gone_or_already_closed`.

## Rules live / dry-live eligibility

| Rule | dry-live | real-live | notes |
|---|---|---|---|
| `v33_quote_edge_150_C` (primary, protected hold) | ✅ allowed | ❌ blocked pending live-equivalence | proven holdout; Entry Snapshot Bank wired |
| `v33_instant_green_scalp` (no-hold bank/scratch) | ✅ allowed | ❌ blocked pending live-equivalence | Entry Snapshot Bank is *structural* to the rule |
| `v33_quote_edge_120_two_snapshot_C` | shadow only | ❌ blocked | replay only |
| `v33_quote_edge_150_fast_bank_A` | shadow only | ❌ blocked | replay only |
| `v33_delayed_green_confirmed` | shadow only | ❌ blocked | replay only |
| `v33_high_edge_fast_exit` | shadow only | ❌ blocked | replay only |
| `v33_recovered_quote_green` | shadow only | ❌ blocked | replay only |
| `v33_pullback_absorption_green` | shadow only | ❌ blocked | replay only |

## What `PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE` means

Default = `0`. While 0 and the broker is in `mode=live`, every Entry Snapshot Bank attempt logs `PGG2-LIVE-EQUIVALENCE-BLOCK reason=entry_snapshot_bank_not_live_eligible` and does NOT proceed. The flag must be set to `1` only when one of the following is true:
1. The bot ships a live atomic buy+sell bundle (Jito / sequential atomic) that reproduces the locked sell-quote close behaviour, OR
2. A separate live smoke proves the live buy confirmation + sell submission preserves the entry snapshot edge.

Setting `PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=1` without one of the two is unsafe and is the responsibility of the operator.

## Why the dry-live result was accepted

- 10 entries, 10 closes, **0 negative all_in closes**.
- Entry Snapshot Bank fired 10/10 — the exact mechanism the dry-live verifies.
- `RISK-CLOSE-FAIL=0`, `Tracebacks=0`, `POSITION-TOKEN-MISMATCH-FATAL=0`, `in_flight ≥ 2 on any pilot mint = 0`.
- Scout-size invariant blocked 3 raw-momentum-bypass candidates with non-matching scout_sol — exactly the class that caused the v36c-1 3UMG loss.
- 1 v36c-3 candidate (2xty profile, slot_top=1.0, slot_buyers=1) entered and banked at +0.000872 instead of clamping at −0.002 — the structural fix worked.

The frequency SLA target (10/20 min) was met at 8 entries in strict window + 10 entries in ~31 min. Frequency is treated as a separate workstream from safety; this release locks the safety semantics.

## What remains different in real live

- The dry-live `risk_worker_entry_snapshot_bank` reads the broker's `_recent_sell_quotes` cache populated by the most recent quote — no on-chain action.
- Real live would require an actual buy tx submission + confirmation, then a sell tx submission + confirmation. Between the two, the price can drift. The dry-live's "instant" bank assumes zero drift, which is true only for unsent quote snapshots.
- Wallet delta is therefore not the same as `quote_all_in_pnl` in real live; it must be reconciled tx-by-tx.

## Exact live smoke procedure

1. Verify `git log -1 --oneline` shows the v36c-3 release commit.
2. Verify `git tag --contains HEAD` lists `v36c3-drylive-10w0l`.
3. Recompute `sha256sum` of all release files on the Hetzner remote and diff against `RELEASE_V36C3_CHECKSUMS.txt`. Stop if any line differs.
4. Verify wallet keypair file present at `PGG2_WALLET_KEYPAIR` and reports the expected public key.
5. Verify wallet balance ≥ `0.020 SOL` (single smoke trade size + buffer).
6. Set `PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK` only in the smoke launcher session.
7. Set the live-smoke launcher (`start_v36c3_live_smoke.sh`) with: `PGG2_EXECUTION_MODE=live`, `PGG2_DRYLIVE_PILOT_MAX_ENTRIES=1`, `PGG2_SCALP_MAX_ENTRIES=0`, `PIGGY_MAX_OPEN_POSITIONS=1`, `PGG2_LIVE_MAX_TRADE_SOL=0.015`, `PGG2_LIVE_SESSION_LOSS_CAP_SOL=0.005`, `PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0` (default keeps ESB OFF in live until proven).
8. Run one entry. Stop immediately after first close, regardless of outcome.
9. Diff actual wallet delta vs `quote_all_in_pnl`. Difference must be ≤ `0.0005 SOL` to consider next step.
10. Do NOT run multi-entry live, do NOT run unattended, do NOT escalate size without explicit user sign-off and a 3-entry smoke gate.
