# V62B — Fwye Final Forensic (Phase 1)

Generated: 2026-05-19 ~15:58 UTC. Authoritative cause + design intent for V62B.

## Hard output

```
FWYE_ROOT_CAUSE = sell_router_ownership_failure
ENTRY_VALID = true
EXIT_FAILED = true
```

## What V60 + V61 did right

| Signal | Value | Verdict |
|---|---|---|
| V60-FIREWALL-PASS | true_edge = +0.000678 SOL | ✅ above raised micro threshold +0.000500 |
| V61-CONTINUATION-PASS | score = 1.000 (all rules satisfied) | ✅ curve+flow stable at decision time |
| V60 size_cap | size = 0.0050 = cap | ✅ no oversize |
| V60 v59_universal | lane = v56d_flow_scratch \| v67_flow_confirm | ✅ legit V48 lane |
| Post-buy expected_sol_out | 0.006131 SOL on 0.005 buy | ✅ **+22% above buy size — profitable trade waiting** |

Entry was valid. There was a profitable exit available. Selling at the expected output would have closed the position at break-even or better after fees + ATA rent recovery.

## What the exit handling did wrong

### Failure 1: Bank sell never confirmed (broker capability gap)

- 15:38:37 `V48-LIVE-SELL-SEND attempt=1 reason=bank` sig `5bYwij…`, expected 0.005956 SOL
- 30 V48-LIVE-SELL-PENDING poll iterations — every one crashed with `AttributeError: DirectPumpQuoteBroker has no signature_status`
- 15:38:39 `V48-LIVE-SELL-FAILED-SAFE pending_timeout` after 2041ms
- **The first sell may have landed on-chain silently.** The bot couldn't verify because the polling method doesn't exist on the broker. So the bot gave up without knowing.

### Failure 2: Emergency sell rejected on-chain (min_sol too tight)

- 15:38:39 `V48-LIVE-EMERGENCY-SELL-SEND` sig `67gCuF…`, expected 0.006131, min_sol_out = **0.003000** (50% loss floor)
- 15:38:40 RPC returned `InstructionError[2, Custom(6023)]` — `TooLittleSolReceived` (Pump.fun sell slippage guard)
- **The min_sol guard (0.003) was higher than the actual on-chain sell return.** By send-land time, curve had moved enough that the actual sell out fell below 0.003. The program rejected.
- Note: `rescue_all_stuck.py` succeeds with `min_sol=0.000020` (effectively accept-any-price). The emergency path was using the wrong policy.

### Failure 3: Jupiter fallback engaged too eagerly

- 15:38:40 `action=jupiter_fallback expected_tokens_raw=115145374915`
- Jupiter balance query returned only **955_207_895 tokens** (0.8% of buy size) — the bank sell DID land silently, consuming most tokens, OR balance state was stale
- Jupiter rescue tx submitted, never confirmed in 61s
- 15:41:11 `V50B-COMPLETE stop_reason=negative_close wallet_delta=-0.001218119`
- **Jupiter fallback for pump_bc is wrong policy.** It introduces a different DEX path, different account model, different timing. It should not be the default-after-1-failure recovery.

## What V62B fixes (this is the design intent)

1. **V62B owns the entire sell loop for pump_bc.** No V48 sell path runs for a live pump_bc position. No Jupiter fallback. Bank, scratch, max_hold, emergency — all routed through V62B.

2. **V62B has direct RPC signature polling.** Doesn't rely on `broker.signature_status`. Uses `getSignatureStatuses` JSON-RPC directly. Solves Failure 1's "polling crashes silently" pattern.

3. **V62B has separate min_sol policies per exit kind:**
   - bank/scratch: `max(cost_basis + fees + small_profit, expected_sol_out × 0.85)` — protects positivity but loose enough to land
   - emergency: `0.000020` (rescue-equivalent, accept-any-price-but-not-zero) — solves Failure 2's "min too tight under decay"

4. **V62B has resend ladder.** If first send doesn't confirm in 300ms: rebuild with fresh blockhash + re-queried raw balance + requoted min_sol → resend. Up to 3 attempts within 1500ms. Solves the abandon-at-2s pattern.

5. **V62B blocks Jupiter fallback for pump_bc.** Jupiter only runs if V62B router has set `router_failed_final=true` AND emergency-clear via V62B has also failed. Solves Failure 3's "Jupiter took over too soon."

## Hypothetical Fwye replay through V62B

| Event | V62B path |
|---|---|
| 15:38:37 V48 bank-sell decision | V62B owns it. Builds bank quote, computes min_sol = `max(0.005 + 0.00006 + 0.0002, 0.005956 × 0.85)` = `max(0.005260, 0.005062)` = **0.005260**. |
| Send via SWQOS | V62B-SELL-SEND sig X1. |
| Poll via direct RPC at 100ms intervals | If processed within 300ms → confirmed. If not at 300ms: rebuild with fresh blockhash, requery balance, resend X2. Repeat up to 3 attempts. |
| Likely outcome | First sig X1 lands within 1-2 slots (1-2 × 400ms ≈ 400-800ms). With min_sol=0.005260, the actual return needs to be ≥ 0.005260. If curve held to that level (which it did at quote-time — expected was 0.005956 minus 1% fee margin), sell confirms. Close = break-even to slight positive after ATA rent. |
| If curve dropped below 0.005260 mid-send | V62B retry 2 rebuilds with FRESH quote, FRESH min_sol. New quote might be 0.004800. New min_sol = `max(0.005260, 0.004800 × 0.85)` = `0.005260` (still positive-protected). Sell would reject again on slippage. After retry 3 fails, V62B router enters emergency mode. |
| Emergency mode | min_sol = 0.000020. Rebuild with raw balance. Send. This is the rescue path that ALREADY WORKS. Sell lands. Close at whatever the curve gives. If curve gave 0.005, close = +$0 break-even. If curve gave 0.003, close = -$0.40 (much smaller than current -$0.22 actually, wait, current is already -$0.22). The point is: tokens are NOT stuck, no Jupiter, deterministic close path. |

**Conclusion: V62B would have either:**
- Closed Fwye non-negative if curve held (most likely given the expected_sol_out was +22% over buy size)
- Closed Fwye at controlled emergency price if curve dumped (no stuck tokens, no Jupiter slippage)

Either way: no fallback to Jupiter, no "tokens vanished" state, no 61-second confirmation timeout.

## Linked memory

- [[v62_fwye_sell_failure_forensic_2026_05_19]] — detailed forensic with code line numbers
- [[v61_continuation_oracle_session_2026_05_19]] — V61 session summary
- [[pumpswap_integration_may2026]] — Apr-28 Pump-v2 account layout
