# V63 — Replay on V62B Stage A (Phase 5)

Generated: 2026-05-19. Replays the V62B Stage A RUN1 (`4rzH..X3Fb`) through
the V63 final-pnl accounting framework to show what the trade's pass/fail
verdict would be under the corrected formula:

    final_wallet_delta = wallet_after_close_account - wallet_before_buy

## Input data (from Phase 1 forensic)

| Field | Value |
|---|---|
| Mint | `4rzHYPWUjccxqbE41sHbBbb5YSLvPLRuLQDWjTruX3Fb` (Token-2022) |
| Buy sig | `AHW3DjRB1hDA..` |
| Confirmed sell sig | `4Lfp3jVRCQfG..` (V62B attempt 2, late confirm) |
| Failed retry sigs | `2uF7gn..` (attempt 1, Custom 3012), `3gR1MA..` (attempt 3, Custom 3012) |
| Wallet before buy | 107_659_494 lamports |
| Wallet after sell tx | 107_149_033 lamports |
| Wallet after both failed retries | 107_099_033 lamports |
| Wallet after V63 close-account | 107_099_033 lamports (no-op: ATA already closed in sell tx) |
| ATA created in buy | `47mqqtNykNJjEgng1rTX1AP6gmH4gvwoyjWMaDWqkCZZ` |
| ATA closed in sell tx | Yes (CloseAccount instruction in sell tx) |

## V63 accounting walkthrough

### V63 hooks fire in order

1. **PGG2-V63-WALLET-BEFORE-BUY** (recorded right before V48-LIVE-BUY-SEND)
   ```
   PGG2-V63-WALLET-BEFORE-BUY mint=4rzH..X3Fb
     wallet_before_lamports=107659494
   ```

2. **V62B sell loop** runs (3 attempts, attempt-2 late-confirms).

3. **PGG2-V63-POST-SELL-CLEAN-CLOSE-START** (after V62B-SELL-CONFIRMED)
   ```
   PGG2-V63-POST-SELL-CLEAN-CLOSE-START mint=4rzH..X3Fb
     sell_sig=4Lfp3jVRCQfG..  actual_pnl_pre_rent=-0.000510461
   ```

4. **V63 module queries the ATA** via getAccountInfo. Result: account doesn't
   exist (closed atomically in sell tx). V63 returns `already_closed=True`,
   no standalone CloseAccount sent.

5. **PGG2-V63-POST-SELL-CLEAN-CLOSE-DONE** (no rent recovered by V63 because
   it was already inside the sell tx)
   ```
   PGG2-V63-POST-SELL-CLEAN-CLOSE-DONE mint=4rzH..X3Fb
     status=already_closed_in_sell_tx
     rent_recovered_sol=+0.000000000
     actual_pnl_post_rent=-0.000510461
     close_sig=None
   ```

6. **PGG2-V48-LIVE-SMOKE-END** (existing, now includes v63 fields)
   ```
   PGG2-V48-LIVE-SMOKE-END mint=4rzH..X3Fb buy_sig=AHW3.. sell_sig=4Lfp..
     close_reason=emergency_timeout
     predicted_all_in=-0.000729896 actual_all_in_pnl=-0.000510461
     token_residual_raw=0 non_neg=0 neg=1 router=v62b
     v63_status=already_closed_in_sell_tx
     v63_rent_recovered_sol=+0.000000000
   ```

7. **PGG2-V63-FINAL-PNL** (the authoritative pass/fail line)
   ```
   PGG2-V63-FINAL-PNL mint=4rzH..X3Fb
     wallet_before=107659494
     wallet_after=107099033
     final_wallet_delta_sol=-0.000560461
     broker_delta_buy_sol=-0.007104080
     broker_delta_sell_sol=+0.006593619
     broker_sum_sol=-0.000510461
     rent_recovered_sol=+0.000000000
     unattributed_sol=-0.000050000
     v63_status=already_closed_in_sell_tx
     pass=false
   ```

The `unattributed_sol = -0.000050000` is the smoking gun: 2 V62B failed-retry
sigs × 25_000 lamports tx fee each = 50_000 lamports = 0.000050 SOL.

## Hard outputs

```
TOKEN_RESIDUAL_ZERO=true
ATA_RENT_STILL_LOCKED=false
V62B_RESULT_BEFORE_RENT_CLOSE=N/A  (rent was inside sell tx atomically)
V62B_RESULT_AFTER_RENT_CLOSE = broker_sum_sol + rent_recovered_sol
                             = -0.000510461 + 0.000000000
                             = -0.000510461 SOL
V63_FINAL_WALLET_DELTA_SOL = -0.000560461 SOL  ← canonical pass/fail metric
V63_PASS = false                  ← Stage A would FAIL under V63 accounting
```

## Comparison: broker delta vs V63 final wallet delta

| Metric | Value | Includes |
|---|---|---|
| broker_delta_buy + broker_delta_sell | -0.000510461 SOL | Only the 2 confirmed txs' wallet deltas |
| V63 final_wallet_delta | -0.000560461 SOL | All wallet effects: trade + ATA atomic close + 2 retry fees |
| Δ (unattributed) | -0.000050000 SOL | 2× failed-retry tx fees burned by V62B sends that hit Custom 3012 |

## Did V62B Stage A "pass" in any accounting?

**No.** Under both accountings:
- Broker delta: -0.000510 SOL → negative → FAIL
- V63 final: -0.000560 SOL → negative → FAIL

The earlier RUN1 result note ("ATA rent ~$0.36 still recoverable") was wrong
in two ways:
1. The rent WAS recovered atomically in the sell tx (Phase 1 forensic).
2. Even adding the rent back doesn't matter — it's already in `broker_delta_sell`.

So the trade was a real -$0.10 loss caused by:
- Bad entry (curve dumped within 1.5s, V47/V48 max_position fired emergency in 2s)
- V62B retry-ladder fee burn (-0.000050 SOL = 50k lamports extra)

Both issues are out of scope for V63 (which only owns rent recovery + final
pnl accounting). The retry-ladder fee burn could be optimized in a future
V62B revision by reducing `MAX_ATTEMPTS` from 3 to 2, since the
`FINAL_WAIT_MS=700` already provides a buffer after the second send.

## Pass criteria (per V63 spec Phase 5)

- ✅ V63 replay produces no stuck token (residual=0 as before)
- ✅ Final PnL is computed after rent recovery (already_closed → no rent added,
  but the formula is correct)
- ✅ Result reported: final PnL is NEGATIVE → trade does NOT change classification
  ("V62B Stage A architecture closer than previously reported" — this is FALSE
  for RUN1; the trade was indeed a loss)

## Implication for V63 Stage A (Phase 7)

V63 accounting must be enabled BEFORE the next live run. If the next trade
produces a sell tx that does NOT include CloseAccount (e.g. due to broker
edge case), V63 will send a standalone CloseAccount and recover the rent;
the recovered_lamports will be added to `actual_pnl` and `final_wallet_delta`
will reflect it.

For RUN1's specific trade, V63 would not have changed the outcome because
the broker's sell builder already closed the ATA atomically. V63's value is
defensive — guaranteeing future regressions don't leave rent trapped.

## Linked

- `V63_V62B_RENT_FORENSIC.md` — Phase 1 forensic (lamport reconciliation)
- `pgg2_v63_post_sell_clean_close.py` — Phase 2 module
- `pgg2_v63_final_pnl.py` — Phase 4 module
- `pgg2_direct_pump.py` (broker.build_close_account) — Phase 2 broker patch
- `pgg2_v48_drylive_harness.py` (V63 hooks at buy + finalize + emergency) — Phase 3 + 4 wiring
- `[[v62b_stagea_run1_2026_05_19]]` — RUN1 memo (will be amended)
