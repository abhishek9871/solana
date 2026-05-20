# V63 — V62B Rent Forensic (Phase 1)

Generated: 2026-05-19. On-chain analysis of V62B Stage A RUN1 closure
to determine whether the 4rzH..X3Fb position's ATA rent was recovered
or still locked.

## Subject trade

| Field | Value |
|---|---|
| Mint | `4rzHYPWUjccxqbE41sHbBbb5YSLvPLRuLQDWjTruX3Fb` (Token-2022) |
| ATA | `47mqqtNykNJjEgng1rTX1AP6gmH4gvwoyjWMaDWqkCZZ` |
| Buy sig | `AHW3DjRB1hDATuUSctuqyTbqGDYSctBkysi6BURWSXwQWjnNbJ1Ebo8iFvWQaP61oCxLeHLUftC2WrJYUiTGEV5` |
| Confirmed sell sig | `4Lfp3jVRCQfGBusVUQybEqaxsPNAUsJhSWDApbnrF1sLTraTtHxk99ho29CsbpVx8iPjQ6XBTfR2t8PrX69wDZP5` |
| Token program | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` (Token-2022) |
| Wallet | `Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7` |

## Lamport-level reconciliation

### BUY tx (`AHW3DjRB..`)

| Account | pre_lamports | post_lamports | delta |
|---|---|---|---|
| Wallet (idx 0) | 107,659,494 | 100,555,414 | **-7,104,080** |
| ATA `47mqqt..` (idx 4) | 0 | 2,074,080 | **+2,074,080** (rent prepay) |
| Tx fee | — | — | -25,000 |
| Curve PDA + tip + program fees | — | — | -5,005,000 (approx) |

CloseAccount instruction in buy tx: **None** (correct — buy creates the ATA).

### SELL tx (`4Lfp3j..`)

| Account | pre_lamports | post_lamports | delta |
|---|---|---|---|
| Wallet (idx 0) | 100,555,414 | 107,149,033 | **+6,593,619** |
| ATA `47mqqt..` (idx 5) | 2,074,080 | **0** | **-2,074,080** (rent returned) |
| Tx fee | — | — | -25,000 |

CloseAccount instruction in sell tx: **FOUND** (outer instruction).
```
{"program": "spl-token", "programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
 "parsed": {"info": {"account": "47mqqtNykNJjEgng1rTX1AP6gmH4gvwoyjWMaDWqkCZZ",
                     "destination": "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7",
                     "owner":       "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"},
            "type": "closeAccount"}}
```

The destination of the closed account's rent was the wallet itself. **Rent of 2,074,080 lamports was recovered atomically in the sell tx**, not left locked.

### Two failed V62B retry sends consumed fees

V62B's resend ladder fired 3 attempts. Attempt 2 (`4Lfp3j..`) was the late-confirmed winner. Attempts 1 (`2uF7gn..`) and 3 (`3gR1MA..`) reached the leader AFTER the ATA was already closed and rejected with on-chain `Custom 3012` (`AccountAlreadyClosed` / `InvalidAccountState`). Each still paid the base tx fee.

| Sig | Status | Fee burned |
|---|---|---|
| 2uF7gn.. (attempt 1) | `Err: InstructionError[2, Custom 3012]` finalized | 25,000 lamports |
| 4Lfp3j.. (attempt 2) | Success | 25,000 lamports (in SELL row above) |
| 3gR1MA.. (attempt 3) | `Err: InstructionError[2, Custom 3012]` finalized | 25,000 lamports |

Two extra failed-retry fees = **50,000 lamports** loss.

This explains the discrepancy:
- Wallet post-confirmed-sell: 107,149,033
- Wallet now: 107,099,033
- Diff: -50,000 lamports = 2 × failed retry fee ✓

## Hard outputs (Phase 1 deliverables)

```
TOKEN_RESIDUAL_ZERO=true
ATA_RENT_STILL_LOCKED=false     ← rent was recovered atomically in the sell tx
ATA_CLOSED_IN_SELL_TX=true       ← broker's sell builder includes CloseAccount
V62B_RESULT_BEFORE_RENT_CLOSE = N/A (cannot separate — rent close was in the same tx)
V62B_RESULT_AFTER_RENT_CLOSE  = buy_delta + sell_delta + failed_retry_fees
                              = -7,104,080 + 6,593,619 - 50,000
                              = -560,461 lamports
                              = **-0.000560 SOL ≈ -$0.10**
ROOT_CAUSE = retry_ladder_fee_burn + curve_dump (NOT missing CloseAccount)
```

## Audit of the wallet's other ATAs (zombie rent inventory)

| Token program | Total ATAs | Zero-balance with locked rent |
|---|---|---|
| SPL Token (Tokenkeg…) | 0 | 0 |
| Token-2022 (Tokenz…) | 21 | **21** (each 2,074,080 lamports) |

Total locked rent: **21 × 2,074,080 = 43,555,680 lamports ≈ 0.0436 SOL ≈ $7.84**.

These zombie ATAs are from prior runs (pre-V62B / V55 / V54 / V51) where the sell path did NOT include CloseAccount. None are from the V62B Stage A RUN1 trade — the V62B trade's ATA was closed atomically.

Sample zombies (all Token-2022, all 2,074,080 lamports each):
- `BF6AoYuAQJDokskuu5ddL5ELWXyCJHnavwLYFq2spump`
- `97GmwBFaWse3vw9PUFDK593yjdfvCDULDF18WFdbpump`
- `7yQSQKvHVNrdniX4uSEXP4yB1R5rtvpXvB6ZPFmBpump`
- `Hy2fhe1ek1N4Rw3No1Mt6rYxciwMTpTMhLJXP1Zbpump`
- (+ 17 more — all end in `pump`)

## Revised conclusion about V62B Stage A RUN1

The RUN1 result memo's claim that "ATA rent ~$0.36 still recoverable" was **incorrect**. The rent WAS recovered atomically in the sell tx. The wallet delta -0.000560 SOL is the **final P&L after rent recovery**, not a pre-rent intermediate state.

Actual root cause of the -$0.10 loss:
1. Curve dumped within 1.5s — buy edge of 0.005 SOL bought tokens worth only ~0.004545 SOL of return (-0.000455 SOL on the trade itself)
2. Two V62B retry attempts consumed 50,000 lamports of wasted fees (-0.000050 SOL)
3. Net: -0.000510 (V62B accounting) + the retry fees = -0.000560 SOL wallet delta

V62B's sell-router design is fine. The V62B router is **already feeding the broker's `build_sell`** which **already includes CloseAccount** for Token-2022 full-balance sells. The buy-broker's `build_sell` correctly attaches the CloseAccount to the sell tx itself.

## What V63 still needs to fix (for THIS trade and for general safety)

Even though the 4rzH ATA was closed, V63 is still necessary because:

1. **21 zombie ATAs from prior trades** (≈$7.84 in locked rent) prove that historically the sell path did NOT always include CloseAccount. A safety net at the V62B layer protects against any future code regression.

2. **V62B retry-ladder fee waste** — when an earlier attempt confirms late, subsequent attempts that already reached the leader will hit `Custom 3012`. V63 should at least:
   - After `PGG2-V62B-SELL-CONFIRMED`, cancel any in-flight retries from the same router invocation (impossible — already on wire) OR
   - Wait the full 700ms `FINAL_WAIT_MS` BEFORE sending retry 3 — this would have caught attempt 2's confirm at T+1500ms and never sent attempt 3 (and possibly retry 2 was the late-confirmer so this is a chicken-and-egg). Realistic mitigation: reduce `MAX_ATTEMPTS` from 3 to 2 since `FINAL_WAIT_MS=700` already gives a buffer after the first 2 sends.

3. **Defense in depth on CloseAccount** — V63 module verifies after sell confirms that the ATA is either:
   - Closed (recovered by sell tx, no-op) ✓
   - Still open with zero balance (V63 issues CloseAccount) ✓
   - Still open with non-zero balance (residual; treat as stuck) ✓

4. **Final PnL accounting** — bot must compute `final_wallet_delta = wallet_after_close_account - wallet_before_buy` and use that for Stage A pass/fail, not `buy_delta + sell_delta` from broker reports.

## Suggested action on the 21 zombie ATAs

Out of scope for V63 spec, but worth surfacing: a batch `CloseAccount` rescue script would recover ≈0.0436 SOL ($7.84) of trapped rent from prior bots' incomplete cleanups. Existing `rescue_all_stuck.py` should handle this if invoked.

## Linked memory

- [[v62b_stagea_run1_2026_05_19]] — RUN1 trade detail (will be corrected with this forensic)
- [[v62-fwye-sell-failure-forensic-2026-05-19]] — V62B genesis forensic
- [[pumpswap_integration_may2026]] — Apr-28 Pump-v2 layout
