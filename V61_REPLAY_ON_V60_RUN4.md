# V61 Replay on V60 RUN4 Log

**Log:** /root/piggy/logs/V61_PRESERVED_V60_RUN4_1779199570.log
**Replay timestamp:** 2026-05-19T14:29:15.028064Z

## Hard output
```
V60_LOSERS_3TCP_BLOCKED_BY_V61 = 1/1
V60_LOSERS_66QI_BLOCKED_BY_V61 = 1/1
TOTAL_V60_PASSES = 3
V61_BLOCKS = 3
V61_PASSES = 0
```

## Per-event verdict

| ts | mint | size | true_edge | curve_pts | V61 | blocker | rule |
|---|---|---|---|---|---|---|---|
| 2026-05-19 13:42:15 | 3Tcp..pump | 0.005 | +0.000987 | 7 | BLOCK | quote_slope_non_positive | r4_quote_slope_positive |
| 2026-05-19 13:45:23 | 4QAn..pump | 0.005 | +0.001537 | 7 | BLOCK | quote_slope_non_positive | r4_quote_slope_positive |
| 2026-05-19 13:45:48 | 66Qi..pump | 0.005 | +0.000786 | 10 | BLOCK | quote_slope_non_positive | r4_quote_slope_positive |

## Per-event detail

### 3Tcp..pump @ 2026-05-19 13:42:15

- size: 0.005
- V60 true_edge: +0.000987
- curve points in buffer: 7
- V61 verdict: BLOCK
- blocker: quote_slope_non_positive
- rule: r4_quote_slope_positive
- detail: non_positive_quote_slope=+0.0000000000

### 4QAn..pump @ 2026-05-19 13:45:23

- size: 0.005
- V60 true_edge: +0.001537
- curve points in buffer: 7
- V61 verdict: BLOCK
- blocker: quote_slope_non_positive
- rule: r4_quote_slope_positive
- detail: non_positive_quote_slope=+0.0000000000

### 66Qi..pump @ 2026-05-19 13:45:48

- size: 0.005
- V60 true_edge: +0.000786
- curve points in buffer: 10
- V61 verdict: BLOCK
- blocker: quote_slope_non_positive
- rule: r4_quote_slope_positive
- detail: non_positive_quote_slope=+0.0000000000

## Dominant blocker

| blocker | count |
|---|---|
| quote_slope_non_positive | 3 |
