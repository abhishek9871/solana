# V61 — V60 RUN4 Loss Forensic

Generated: 2026-05-19 ~14:10 UTC. Parses preserved V60 Stage A RUN4 log (`/root/piggy/logs/V61_PRESERVED_V60_RUN4_1779199570.log`).

## Verdict

```
LOSSES_HAVE_PRE_SEND_WARNINGS = false
LOSSES_HAVE_POST_V60_NEGATIVE_CONTINUATION = true
V61_ORACLE_WOULD_BLOCK_BOTH_LOSERS = true (via Rules 2/3/4 in post-V60 confirmation window)
```

**Headline:** Neither loser showed pre-V60 warning signs. Both showed immediate post-V60 curve collapse. V61 must run AFTER V60-PASS as a post-confirmation continuation check, not as a parallel pre-V60 signal.

## Summary table

| mint | V60 true_edge | outcome | wallet_delta | curve_dump_pct@2s | first_neg_curve_age_ms (post V60 pass) | V61 blocker (which rule) |
|---|---|---|---|---|---|---|
| 3Tcp..pump | +0.000987 | EMERGENCY-SELL | -0.0019 SOL | -13.25% | ~2000 ms | **Rule 3 + Rule 4** (post-V60 negative curve delta + collapsing quote) |
| 4QAn..pump | +0.001537 | BUY-FAILED-SAFE | -0.0000025 SOL | n/a | n/a (preempted) | preempted by on-chain min-token-guard reject |
| 66Qi..pump | +0.000786 | EMERGENCY-SELL | -0.0019 SOL | -9.99% | ~100 ms | **Rule 2 + Rule 3 + Rule 4** (immediate post-V60 negative curve + quote collapse) |

## 3Tcp..pump — LOSER

### A. V48 candidate decision @ 13:42:15

```
signal_lane=v56d_flow_scratch  ub250=3  top_share=0.435  v47h_ratio=0.3634
pbs1000=7.590  psc1000=0  pss1000=0.000  source_lead_ms=250  gate_pass=true
```

### B. Curve trajectory window

| Timestamp | vSol (B) | vTok (T) | price | delta from prev |
|---|---|---|---|---|
| 13:42:13 | 39.40 | 817.01 | 0.000048224915 | baseline |
| 13:42:13 | 40.76 | 789.80 | 0.000051603872 | +6.99% (recovery) |
| **13:42:15 V60 PASS** | 40.76 | 789.80 | 0.000051603872 | 0.00% (stalled) |
| 13:42:17 (BUY+2s) | 40.76 | 789.71 | 0.000051616378 | +0.02% |
| 13:42:17 (BUY+2s) | 40.66 | 791.62 | 0.000051366881 | **-0.48% FIRST NEGATIVE** |
| 13:42:17 (BUY+2s) | 37.88 | 849.75 | 0.000044579679 | **-13.25% DUMP** |

### C. Pending flow

```
pre-V60:  ub250=3 pbsol=7.590 pssol=0.000 → pure buy
post-V60: no sell flow visible (pss stays 0)
```

### D. Quote trajectory

| ts | dir | in | out | min |
|---|---|---|---|---|
| 13:42:15 | BUY  | 0.005 | 95920 | 91124 |
| 13:42:15 | SELL | 91124 | 0.004655 | 0.003631 |
| 13:42:17 | SELL | 94894 | **0.004849** | 0.003782 |
| 13:42:17 | SELL | 94894 | 0.004825 | 0.003764 |
| 13:42:17 | SELL | 94894 | **0.004188 collapse** | 0.003266 |
| 13:42:19 | EMERGENCY-SELL | 94894 | 0.004189 | 0.003 |

### E. Pre-send signal analysis

| Question | Answer |
|---|---|
| Curve decelerating BEFORE V60? | No — flat from 13:42:13 to 13:42:15 (2 points only, insufficient for 2nd derivative but no decline visible) |
| Sell pressure BEFORE V60? | No — pss1000=0, psc1000=0 |
| Quote flattening BEFORE V60? | No — snapshot-based, only 1 pre-V60 quote pair (age 346ms) |
| First negative signal AFTER V60, BEFORE buy_confirmed? | YES at 13:42:17 (-0.48% then -13.25%) |
| Time V60 PASS → first negative curve delta | **~2000ms** |

### F. V61 verdict

**Rule 3 + Rule 4** would have blocked. If V61 checks curve continuity for 500ms after V60 PASS:
- At 13:42:17 (2s post V60-PASS, just before send_signed completes), curve delta is -0.48% → Rule 3 fires
- Quote slope is collapsing: 0.004849 → 0.004188 in ~500ms → Rule 4 fires

## 4QAn..pump — PREEMPTED

```
V60 PASS 13:45:23  true_edge=+0.001537
BUY-FAILED-SAFE 13:45:24  reason=not_processed_or_min_token_guard
fee_spent=0.000025 SOL
```

V61 oracle never reached. The on-chain min-token-guard rejected the buy at land time because the curve had moved between V60 PASS and slot landing. This is a different failure mode (chain-side guard reject, not bot-side error).

## 66Qi..pump — LOSER

### A. V48 candidate decision @ 13:45:48

```
signal_lane=v56d_flow_scratch|v67_flow_confirm  ub250=3  top_share=0.385
v47h_ratio=0.3232  pbs1000=13.000  psc1000=0  pss1000=0.000  source_lead_ms=250
gate_pass=true
```

### B. Curve trajectory window

| Timestamp | vSol (B) | vTok (T) | price | delta from prev |
|---|---|---|---|---|
| 13:45:47 | 44.90 | 716.91 | 0.000062631767 | baseline |
| **13:45:48 V60 PASS** | 77.11 | 417.43 | 0.000184732663 | **+108.50% explosive spike** |
| 13:45:49 (BUY+1s) | 77.02 | 417.94 | 0.000184288515 | **-0.24% FIRST NEGATIVE** |
| 13:45:49 (BUY+1s) | 76.73 | 419.52 | 0.000182899410 | -0.76% |
| 13:45:49 (BUY+1.2s) | 75.22 | 427.97 | 0.000175749603 | -3.91% |
| 13:45:50 (BUY+2s) | 74.43 | 432.51 | 0.000172077712 | -1.89% |
| 13:45:50 (BUY+2s) | 73.61 | 437.31 | 0.000168319257 | -2.19% |
| 13:45:51 (BUY+3s) | 73.16 | 439.98 | 0.000166283125 | -1.21% |

Total post-V60 dump: **-9.99%** in ~3 seconds.

### C. Pending flow

```
pre-V60: ub250=1→3, pbsol=0.800→13.000, pssol=0.000 → pure buy + explosive ramp
post-V60: no sell flow logged (pss stays 0)
```

### D. Quote trajectory

| ts | dir | in | out | min |
|---|---|---|---|---|
| 13:45:48 | BUY  | 0.005 | 26796 | 25456 |
| 13:45:48 | SELL | 25456 | 0.004655 | 0.003631 |
| 13:45:49 | SELL | 26506 | **0.004836** (peak +0.181 bps) | 0.003772 |
| 13:45:49 | SELL | 26506 | 0.004799 | 0.003743 |
| 13:45:49 | SELL | 26506 | 0.004612 | 0.003597 |
| 13:45:50 | SELL | 26506 | 0.004515 | 0.003522 |
| 13:45:50 | SELL | 26506 | 0.004417 | 0.003445 |
| 13:45:50 | SELL | 26506 | 0.004328 | 0.003376 |
| 13:45:51 | EMERGENCY-SELL | — | **0.004363** | 0.003 |

Quote collapse: 0.004836 → 0.004363 = **-9.77%** in ~1.5s.

### E. Pre-send signal analysis

| Question | Answer |
|---|---|
| Curve decelerating BEFORE V60? | No — explosive +108.5% spike right at V60 pass, no deceleration visible (this is the peak entry pattern: bot entered AT the top) |
| Sell pressure BEFORE V60? | No — pss1000=0, psc1000=0 |
| Quote flattening BEFORE V60? | No — snapshot-based, only 1 pre-V60 quote pair (age 343ms) |
| First negative signal AFTER V60? | YES at 13:45:49 (-0.24% within 100ms) |
| Time V60 PASS → first negative curve delta | **~100ms** — almost immediate |

### F. V61 verdict

**Rule 2 + Rule 3 + Rule 4** would have blocked. V61 post-V60 window of 100-500ms would catch this:
- At T+100ms after V60 PASS, curve delta is already -0.24% → Rule 2 (latest delta non-positive) fires
- At T+200ms, -0.76% → Rule 3 fires (any negative in last 500ms)
- Quote slope already inverting → Rule 4 fires

The 66Qi pattern is the **"entered at the peak"** pattern: 66Qi had a +108% spike just as V60 evaluated. V60's V59 true_edge math is correct on the snapshot, but the snapshot captured the very peak; one slot later, mean-reversion started.

## Hard outputs

**Q: Which pre-send signal would have blocked the losers?**

A: **None pre-V60. Both losses are POST-V60 phenomena.** The blocker must be a **post-V60 continuation check** that delays the actual send_signed by 100-500ms and re-validates curve + quote slope in that window. V61 Rules 2/3/4 (latest curve delta, no negative in 500ms, quote slope positive in 300-700ms) would block both losers if evaluated in a post-V60 confirmation window.

**Q: Did the losses have curve deceleration before buy?**

A: **No.** 3Tcp had a flat curve pre-V60 (stable, no decline). 66Qi had explosive +108% acceleration right at V60 pass — the opposite of deceleration. Both had insufficient pre-V60 curve points for 2nd-derivative analysis (only 1-2 points).

**Q: Did they have sell pressure before buy?**

A: **No.** Both had `pss1000=0.000` and `psc1000=0` pre-V60. Pure buy pressure: 3Tcp pbs1000=7.590, 66Qi pbs1000=13.000.

**Q: Did they have flattening quote gradient before buy?**

A: **No measurable signal.** Both had snapshot-based quote pairs at V60 evaluation time (age ~340-346ms). Only one pre-V60 quote pair was available per mint — insufficient for slope analysis.

**Q: Did they have NO positive continuation after V60 pass?**

A: **YES, definitively.** Both losers entered emergency-sell within 2-3 seconds of buy-confirm with 9-13% drawdown:
- **3Tcp:** Curve stalled at peak through V60+2s, then crashed -13.25%. Quote peaked at 0.004849 (+4% slip post-buy), then collapsed to 0.004188 (emergency floor) within 500ms.
- **66Qi:** Curve immediately negative within 100ms of V60. Quote peaked at 0.004836 (+4% slip), then sustained decline to 0.004363 over 1.5s.

Neither mint showed any sustained price recovery or quote improvement after V60 PASS.

## Implications for V61 design

1. **The continuation oracle must run AFTER V60 PASS, in a 100-500ms confirmation window** — not as a parallel pre-V60 check.

2. **Snapshot-quote-only data is insufficient.** V61 must consume fresh curve updates that arrive in the post-V60 window. Recommended: wait for at least 1 fresh curve update (≤300ms) after V60 PASS before send_signed.

3. **The 66Qi "entered at peak" pattern is the dominant loss mode.** Big +108% spike right at V60 eval → V60's math says +true_edge → mean-reversion immediately. V61 rule 9 ("candidate has not peaked and flattened") needs to detect this: if the curve rose >X% in the last N ms, suspect a peak and require continuation confirmation.

4. **Watchlist promotion is essential** (Phase 4). If V60 passes but V61 wants confirmation, candidate sits in 1000ms watchlist. Re-evaluate on every curve update. If curve continues up → promote. If curve flattens or reverses → drop. Without this, V61 will reject too aggressively in fast markets.

5. **Cannot rely on pre-V60 pending-flow signals.** Both losers had clean pre-V60 pending flow (zero sells, strong buys). The sell pressure appeared on-chain AFTER V60 saw them. The V42C pending-flow oracle is not predictive of post-V60 curve direction.

## Linked memory

- [[v60_stage_a_first_session_2026_05_19]] — V60 RUN4 session summary
- [[v59_bypass_bug_2026_05_19]] — AwsN incident that motivated V60
