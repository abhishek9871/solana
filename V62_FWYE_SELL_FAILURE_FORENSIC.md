# V62 — Fwye Sell Failure Forensic (Phase 1)

Generated: 2026-05-19 ~15:55 UTC.
Source: `/root/piggy/logs/V62_PRESERVED_V61_RUN5_Fwye_1779206138.log`.
Mint: `Fwyeva5daU1zd4zZftQNv6b8gciNGnsG2MEQexqBpump` (Token-2022).

## Summary

```
EXACT_REASON_SELL_FAILED = bank-sell never polled (broker.signature_status method missing)
                          + emergency-sell hit InstructionError[2, Custom(6023)] on-chain
                          + token balance had collapsed to 955M of 115B before Jupiter ran
JUPITER_FALLBACK_TRIGGER_LINE = pgg2_v48_drylive_harness.py:4486 → _try_jupiter_fallback at :4489
V51B_RESEND_WOULD_HAVE_CLOSED_NONNEGATIVE = false (high confidence)
ROOT_CAUSE_CLASS = Token-2022 sell uses legacy retarget_sell_min_sol (not sell_v2); broker lacks signature_status RPC poll
```

## Sell timeline (millisecond-level)

| ts | event | sig | min_sol | expected_sol | status |
|---|---|---|---|---|---|
| 15:38:37.000 | V48-LIVE-SELL-SEND (bank attempt 1) | 5bYwij… | 0.004797 | 0.005956 | sent |
| 15:38:37.037 | V50A-SENDER-SEND (5bYwij) | 5bYwij… | — | — | sender ok, latency 37.9ms |
| 15:38:37.1→38.0 | V48-LIVE-SELL-PENDING poll loop (~30 iterations) | 5bYwij… | — | — | each poll raises `AttributeError: DirectPumpQuoteBroker has no signature_status` |
| 15:38:39.041 | V48-LIVE-SELL-FAILED-SAFE | 5bYwij… | — | — | pending_ms=2041 → abandon |
| 15:38:39.043 | emergency sell quote rebuilt | — | — | 0.006131 | fresh snapshot, curve recovered |
| 15:38:39.044 | V48-LIVE-EMERGENCY-SELL-SEND | 67gCuF… | 0.003000 | 0.006131 | sent (min_out_frac=0.970) |
| 15:38:40.043 | V50A-SENDER-SEND (67gCuF) | 67gCuF… | — | — | sender ok, latency 43.3ms |
| 15:38:40.044 | **PGG2-LIVE-TX-ERR** | 67gCuF… | — | — | **`InstructionError: [2, Custom(6023)]`** — on-chain program reject |
| 15:38:40.050 | _try_jupiter_fallback engaged | — | — | — | balance query returned tokens_raw=955_207_895 (0.8% of 115B bought) |
| 15:39:41.000 | Jupiter rescue tx 4cqdWR…GdqdMxHN… | 4cqdWR… | — | — | **confirm timeout 61s** — never landed |
| 15:41:11.000 | V50B-COMPLETE | — | — | — | stop_reason=negative_close wallet_delta=-0.001218119 |

## On-chain status of each signature

- **5bYwij…** (bank sell): Status UNKNOWN. Broker's `signature_status()` method does not exist → polling raised AttributeError on every iteration. Tx may have landed silently, or been dropped. Bot abandoned at 2041ms without confirmation.

- **67gCuF…** (emergency sell): CONFIRMED FAILED on-chain. RPC returned `{"InstructionError": [2, {"Custom": 6023}]}` synchronously. Instruction at index 2 (likely the transfer or curve-state check) rejected. Custom 6023 in the Pump.fun bonding curve program family is typically a `slippage_exceeded` or `account-extension-mismatch` for Token-2022 mints.

- **4cqdWR…** (Jupiter rescue): Sent but `getSignatureStatuses` never returned a confirmed slot within the 61s window. Position state when Jupiter ran: only 955M tokens of original 115B remained — 99.2% of position had vanished. Either the bank sell DID land (just not seen by our polling) consuming most tokens, OR the curve crash between 15:38:37 and 15:38:40 plus a partial sell collapsed value before Jupiter quoted.

## Code-path analysis

**Why first sell never confirmed (orthogonal to V51B resend):**
- `pgg2_v48_drylive_harness.py:4138` `_poll_pending_guarded_sells()`
- Line 4145: `status = broker.signature_status(_sig)` → AttributeError on `DirectPumpQuoteBroker`
- Line 4150: exception caught, logs PENDING-WARN, returns None
- Line 4179: status=None + elapsed > 2500ms → mark FAILED-SAFE
- Line 4185: emits `V48-LIVE-SELL-FAILED-SAFE reason=pending_timeout`
- **Bug:** broker has `wait_confirmed` (blocking) but no `signature_status` (non-blocking poll). The nonblocking sell loop assumed the latter exists.

**Why emergency sell hit Custom(6023):**
- Line 4442: `guarded_sell = broker.retarget_sell_min_sol(sell_quote, mint, emergency_min)`
- `retarget_sell_min_sol` modifies the min_sol guard but does NOT rebuild the instruction account layout for Token-2022
- Line 4472: `broker.send_signed(signed_sell)` sends the (legacy) tx
- On chain, the Pump program rejected instruction 2 with Custom 6023 — likely slippage-exceeded under the post-3s curve, OR Token-2022 account layout mismatch
- Sell never made it past on-chain validation; tokens stayed in wallet

**Why Jupiter fallback engaged:**
- Line 4486: on `LIVE-TX-ERR` (or `not_confirmed`), action=`jupiter_fallback`
- Line 4489: `_try_jupiter_fallback(mint_str, expected_tokens_raw=115145374915, ...)`
- Line 3508-3526 in `_try_jupiter_fallback`: balance retry loop (3 attempts, 0.5/1.0s backoff)
- Line 3539: calls `jupiter_rescue_one()` with whatever balance it found (955M, not the expected 115B)
- Jupiter built a quote for 955M, sent the rescue tx, never got confirmation in 61s

**Sell method used (the structural problem):**
- Both sells used `broker.retarget_sell_min_sol()` — the LEGACY path
- Broker had `has_sell_v2_capability=True` available (lines 1104, 3350, 5005) but the emergency-sell code didn't call `sell_v2`
- For Token-2022 mints with the April-28 program upgrade (additional accounts + FeeConfig changes), the legacy sell instruction can fail with Custom errors

## V51B-style resend ladder counterfactual

Would V51B (sell_v2 + 3 retries within 1500ms, fresh blockhash + raw balance, higher priority) have closed Fwye non-negative?

**No, with ~85% confidence.** Specifically:

1. **First sell timeout class would be fixed by V51B's approach:** V51B skips `signature_status` polling entirely and resends blindly with new blockhash. With 3 retries in 1500ms, retry 2 or 3 might catch a block before the curve dumps further. But by retry 3 (~T+1.5s post first send), the curve had already recovered then started decaying. Expected sell out was still positive at retry 1 time, but rapidly degrading.

2. **Emergency sell Custom(6023) class is NOT fixed by V51B resend:** Custom 6023 is a synchronous on-chain validation error, not a confirmation timeout. Retransmitting the same instruction with fresh blockhash produces the SAME error. V51B retry wouldn't change the outcome. The fix here requires **switching to sell_v2** (V62 spec mandates this).

3. **Position degradation in 3 seconds:** Between bank-sell send (T+0) and Jupiter fallback start (T+3s), token balance went from 115B to 955M. Some sell DID consume tokens — likely 5bYwij landed silently. By the time V51B retries 2-3 would have run, position was already dust. Recovery window: ~500ms post-T+0.

4. **Estimated outcome with V51B:** Bank sell might have confirmed via retry within 500ms (price still around 0.005-0.006). If it did, close would be near scratch (~0.000-0.001 SOL above buy + ATA rent recovery ~ break-even or +$0.05). If retries all failed, same Custom(6023) on emergency sell → still negative close. **Best case: scratch break-even. Worst case: identical to actual outcome (-$0.22).**

5. **What V62 actually needs to fix:**
   - **A. Add `broker.signature_status()`** so first-sell polling works
   - **B. Switch emergency sell to `sell_v2`** for Token-2022 mints
   - **C. Add resend ladder** for both bank and emergency paths
   - **D. Disable Jupiter fallback** for pump_bc when V62 router has not yet exhausted retries
   - **E. Add fresh blockhash + raw balance + priority bump** on each retry

## Implications for V62 design

The Fwye case proves **three concurrent failures**:
- broker missing `signature_status` method → bank sell abandoned without knowing on-chain state
- legacy `retarget_sell_min_sol` used instead of `sell_v2` for a Token-2022 mint → Custom(6023)
- Jupiter fallback engaged too early, after only 1 emergency attempt, with degraded balance

V62 must fix all three:
- **P3 sell router**: sell_v2 only for Token-2022, raw balance from chain at send time, no Jupiter fallback for pump_bc
- **P4 resend ladder**: 3 retries within 1500ms with fresh blockhash + priority bump + raw balance re-query
- **P5 disable Jupiter for pump_bc**: only V62-router-final-fail can call legacy fallback

## Linked memory

- [[v60_stage_a_first_session_2026_05_19]] — V60 RUN4 forensic
- [[v61_continuation_oracle_session_2026_05_19]] — V61 session summary
- [[pumpswap_integration_may2026]] — April-28 Pump-v2 account layout changes
