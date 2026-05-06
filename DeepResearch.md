# Next-generation pump.fun sniper architecture

The current bot is choppy because **it computes truth from a stream that lies (raw shreds), and arms its trigger from a stream that tells the truth too late (BondingCurve at processed commitment)**. Every losing trade in the recent paper session is a structural consequence of that gap, not a parameter-tuning problem. The fix is not better filters; it is a re-architecture into a **two-track state machine** in which the shred tape only opens an option, the BondingCurve account confirms it, and a **scout-then-scale sizing curve** ensures that no full-size position is ever exposed before the market has mechanically proven expansion. Combined with a **warmed sell path** signed against a fresh blockhash on a 20s refresh, an **absorption-and-impact-decay distribution detector**, and an **append-only event log feeding a triple-barrier-labeled LightGBM head**, this turns high-frequency detection into rare high-conviction fills with structurally bounded loss per trade. The rest of this document is implementable from top to bottom.

---

## 1. Blunt diagnosis of why the current bot is choppy

**The bot is buying the manufactured first impulse and exiting after the insiders have exited.** Pine Analytics, working over a 30-day pump.fun sample of ~15,000 launches, established three numbers that explain your loss profile exactly:

- **>50%** of pump.fun tokens are sniped **in the same block as creation** by deployer-funded wallets.
- **>55%** of those snipers are **fully out within 60 seconds**, **>85% within 5 minutes**, and **>90% in just 1–2 swap events**.
- **87%** of those deployer-funded snipes are profitable for the snipers — meaning the SOL profit is paid by *the next layer of buyers*, which is exactly where a market-wide shred sniper sits.

You are the exit liquidity. Specifically:

1. **Your "continuation strike board" is correct in spirit but late.** A shred-arms / curve-confirms gate buys you out of the literal first slot, but a one-confirmation BondingCurve update at `processed` commitment lands roughly a slot (~400 ms) after the bundle. By that point the bundle's first dump tx is already in the next slot's mempool. You are buying *into* the dump, then exiting at the bottom of the bundle wallet's distribution.
2. **You have no distinction between "real markup" and "bundle pop with a bot tail."** Both produce a curve continuation tick. The 7-loss/2-win mix is consistent with the empirical base rate when a sniper enters every observable continuation indiscriminately.
3. **Your sizing is binary.** Open-source bots without exception, including the most respected references (chainstacklabs, 1fge, FLOCK4H Dexter, keidev-sol), use *flat sizing*. There is no published OSS bot with scout-then-scale, and the absence is precisely why their published results look like yours.
4. **Your exits are price-based, not microstructure-based.** A 1.4–1.6x runner that fades back to your stop is the canonical signature of a TP/SL ladder fired against a price stream when the *flow* had already inverted. Insiders telegraph their exit in OFI and impact-decay 1–3 seconds before price prints the top.
5. **You depend on a chain that lies twice.** Raw instruction price is fake (priced from `max_sol_cost`, not realized fill). One-shot BondingCurve at `processed` is real but asynchronous. The fix is not picking one; it is correlating both with a small synthetic order book.

---

## 2. Proposed winning architecture

```
┌────────────────────────────┐    ┌────────────────────────────┐
│  WS-A (multiplexed)        │    │  WS-B (multiplexed)        │
│  shredSubscribe (pump.fun  │    │  programSubscribe          │
│  program filter)           │    │   (BondingCurve, processed)│
└────────────┬───────────────┘    └────────────┬───────────────┘
             │ pre-confirm intent              │ truth state
             ▼                                 ▼
        ┌────────────────────────────────────────────┐
        │  EVENT NORMALIZER                          │
        │  decoders: Create, TradeEvent (disc        │
        │  189,219,127,…), buy/sell ix, BondingCurve │
        │  account snapshots (offsets 0x08…0x51)     │
        └────────────────┬───────────────────────────┘
                         ▼
        ┌────────────────────────────────────────────┐
        │  PER-MINT FEATURE STATE                    │
        │  ring buffer last 30 s of events           │
        │  causal rolling features at 50 ms tick     │
        │  windows: 200 ms, 500 ms, 1 s, 2 s, 5 s    │
        └────────────────┬───────────────────────────┘
                         ▼
        ┌────────────────────────────────────────────┐
        │  STAGE-A HARD GATE  (rules, ~20 µs)        │
        │  reject bundles, dev outflow,              │
        │  insider-funded buyers, repeat-rugger      │
        └────────────────┬───────────────────────────┘
                         ▼
        ┌────────────────────────────────────────────┐
        │  STAGE-B CURVE-ACCEL CLASSIFIER (LightGBM, │
        │  Treelite-compiled, ~50 µs)                │
        │  output p(2x within 60 s)                  │
        └────────────────┬───────────────────────────┘
                         ▼
        ┌────────────────────────────────────────────┐
        │  STRATEGY FSM (per mint)                   │
        │  ARMED → SCOUT_LIVE → SCALE → RUNNER →     │
        │  PARTIAL → EXIT, with KILL transitions     │
        └────────────────┬───────────────────────────┘
                         ▼
        ┌────────────────────────────────────────────┐
        │  EXECUTION                                 │
        │  warmed sell tx (re-sign every 20 s)       │
        │  Raptor /quote-and-swap → /send-transaction│
        │  fallback: Jupiter → Jito /api/v1/transactions│
        └────────────┬───────────────────────────────┘
                     ▼
        ┌────────────────────────────────────────────┐
        │  EVENT LOG + CANDIDATE LOG (Parquet)       │
        │  forward labels (triple-barrier) computed  │
        │  offline; nightly LightGBM retrain         │
        └────────────────────────────────────────────┘
```

**Three structural decisions distinguish this from the current design:**

- **One physical socket per channel; multiplex everything else.** Solana Tracker's 2-WS Free cap is *physical connections*, not subscriptions. PubSub is fully multiplexed by `subscription id`. Open WS-A for `shredSubscribe`, WS-B for `programSubscribe` plus N `accountSubscribe`s of held mints — uses both connections with arbitrary subscription depth.
- **Two truth sources, one decision surface.** The shred tape arms; the BondingCurve account snapshot at `processed` confirms; the synthesized "synthetic curve" (instantaneous price reconstructed from the most recent shred-derived buy/sell using `(virtual_sol_reserves + Δsol_net)² / k` between BondingCurve account updates) is the actual decision input. This collapses the shred-vs-account latency gap to ~1 slot worst case.
- **Sizing is the strategy.** A scout fill of 0.005–0.02 SOL is a paid information probe; the realized fill price, slippage, and one-tick post-fill curve velocity are signals you literally cannot get any other way. The full position is only assembled after the market has mechanically printed continuation against your scout.

---

## 3. Exact strategy state machine

```
States:
  IDLE
  ARMED               # shred tape detected qualifying impulse
  SCOUT_LIVE          # scout buy submitted, awaiting confirmation
  SCOUT_FILLED        # scout fill confirmed on BondingCurve account
  SCALE_LIVE          # full-size add submitted
  SCALE_FILLED        # at planned size
  PARTIAL_DONE        # first take-profit tranche realized
  RUNNER              # remainder held with trailing rule
  EXITING             # exit submitted
  CLOSED
  KILLED              # forced flat by distribution detector or rug

Transitions (guards in [ ], actions in { }):

IDLE → ARMED
  [ Stage-A gates pass on shred tape:
        bundle_at_deploy=false  OR  bundle_supply_captured_pct < 0.15
        AND creator_blacklist_hit=false
        AND creator_repeat_rugger=false
        AND creator_outflow_5s_flag=false
        AND f_<50ms_2s ≤ 0.35
        AND USR_2s ≥ 0.5
        AND dev_buy_sol ∈ [0.5, 4.5] ]
  { open accountSubscribe(BondingCurve PDA) on WS-B }

ARMED → IDLE
  [ 1.5 s elapsed AND no continuation print on real curve ]
  { close accountSubscribe }

ARMED → SCOUT_LIVE
  [ Stage-B p(2x) ≥ τ_scout (default 0.35)
    AND v_1s > 0 on real curve (BondingCurve.virtual_sol_reserves rising)
    AND nOFI_2s = OFI_2s / Y(t) ≥ 0.02
    AND a_1s ≥ 0 ]
  { build buy ix for SCOUT_SIZE = clamp(0.005 SOL, 0.02 SOL, 0.04% of curve Y);
    submit via Raptor /quote-and-swap → /send-transaction
    with priorityFee="veryHigh", slippageBps=2500, dexes=Pump.fun;
    in parallel build warmed sell tx skeleton (no signing yet) }

SCOUT_LIVE → IDLE     [ tx failed / expired ]   { log failure }
SCOUT_LIVE → KILLED   [ creator_outflow_flag fires before fill ] { abandon }

SCOUT_LIVE → SCOUT_FILLED
  [ BondingCurve account update arrives showing tokens credited to bot ATA
    AND realized entry price reconstructed ]
  { record entry_price, entry_tokens, entry_slot;
    sign warmed sell tx for full scout position against fresh blockhash;
    start 20 s blockhash refresh timer for warmed sell }

SCOUT_FILLED → KILLED
  [ ANY of:
       AR_sell_2s flips to > 1.0 (sell residual positive)
       creator_outflow_5s_flag = 1
       topN_flip_5s ≥ 0.4
       IPD < 0.5  AND  B_5s > B_avg_30s
       price_drawdown_from_entry > 8%   (scratch exit for slippage-haircut)
  ]
  { fire warmed sell }

SCOUT_FILLED → SCALE_LIVE
  [ realized post-scout 1 s velocity v_1s > 0
    AND post-scout absorption AR_sell_2s ≤ 0.3 (sells absorbed)
    AND realized fill price ≤ quoted price * (1 + 0.005)  (no first-tick slip)
    AND Stage-B p(2x | post-scout features) ≥ τ_scale (default 0.55) ]
  { build buy ix for SCALE_SIZE = TARGET_SIZE − SCOUT_SIZE,
    where TARGET_SIZE = min(quarter-Kelly(p,r_win,r_loss), per-mint cap, 0.5% of Y);
    submit via Raptor with priorityFee="turbo" }

SCALE_LIVE → SCALE_FILLED   [ BondingCurve update credits scale tokens ]
SCALE_LIVE → KILLED         [ same kill rules as SCOUT_FILLED ]

SCALE_FILLED → PARTIAL_DONE
  [ price ≥ entry_blended * 1.6
    OR (price ≥ entry_blended * 1.3 AND a_1s < 0 for ≥ 500 ms) ]
  { fire warmed sell for 50% of position;
    immediately request fresh /swap-instructions for remainder size,
    re-sign new warmed sell }

SCALE_FILLED → KILLED   [ same kill rules; price < entry_blended * 0.85 ]

PARTIAL_DONE → RUNNER
  [ remainder still open AND a_1s ≥ 0 ]
  { activate trailing rule: peak = max(peak, price);
    exit when price ≤ peak * (1 − 0.18) }

RUNNER → EXITING
  [ price ≤ peak * (1 − 0.18)
    OR a_500ms < 0 AND ã_1s < 0 for ≥ 700 ms while in_profit
    OR migration event observed
    OR 60 s elapsed since SCOUT_FILLED  (hard time stop) ]
  { fire warmed sell for remainder }

EXITING → CLOSED   [ sell tx confirmed, log realized PnL ]
KILLED  → CLOSED   [ sell tx confirmed ]
```

**Key invariants enforced by the FSM:**

1. The bot **never holds full size** before SCALE_FILLED. Worst-case exposure during scouting is ≤ 0.02 SOL.
2. The warmed sell is signed *before* SCALE_LIVE submits. There is no sequence in which the bot is full-size with no exit ready.
3. KILL transitions exist out of every post-fill state. There is no path that requires waiting for a price-based stop; flow-based and counterparty-based kills fire first.
4. Every transition has a logged event. The state log itself is a training feature for the exit policy.

---

## 4. Hot-path signals to compute

All signals are causal (window closed-left). Recompute on every event arrival; cap update budget at 20 µs per mint via incremental ring-buffer accumulators. `Y(t)` = `virtual_sol_reserves(t)` in SOL.

| Signal | Formula | Window | Threshold (markup) | Threshold (kill) |
|---|---|---|---|---|
| `price_t` | `vSol/vTok` (lamports/base unit) | spot | rising | falling |
| `OFI_W` | `Σ s_i·q_i` SOL, s∈{+1,−1} | 1 s, 2 s, 5 s | OFI_2s > max(0.3, 0.01·Y) | OFI_2s < 0 sustained |
| `nOFI_W` | `OFI_W / Y(t)` | 1 s, 2 s | ≥ 0.02 | < 0 |
| `z_OFI_2s` | EWMA z-score, τ=30 s | 2 s | > 2.5 | < −2.5 |
| `v_W` | `ln(price_t/price_{t-W}) / W` | 200 ms, 1 s, 2 s | v_200ms>0 ∧ v_1s>0 ∧ v_2s>0 | any negative |
| `a_W` | `(v_W(t) − v_W(t-W))/W` | 1 s | ≥ 0 | < 0 for ≥ 500 ms |
| `r̂_net` | `2·ln(1 + ΔY_net / Y(t-W))` (mechanical impact) | 2 s | — | — |
| `r̂_sell` | `2·ln(1 − Q_sell_W/Y(t-W))` | 2 s | — | — |
| `AR_sell` | `r_real / r̂_sell` (both negative under sells) | 2 s | ≤ 0.3 (absorption) | > 1.0 (sell residual flips positive) |
| `ε_abs` | `r_real − r̂_net` | 1 s | > 0 with negative r̂_net | < 0 with positive r̂_net |
| `IPD` (impact decay) | `mean(Δp_per_SOL last 5 s) / mean(Δp_per_SOL prior 30 s, same size bucket)` | 5 s vs 30 s | ≥ 1 | < 0.5 with B_5s > B_avg |
| `SBR` | `S_W / (B_W + ε)` | 2 s, 5 s | < 0.5 | > 0.6 while r_5s ≥ 0 |
| `TSH` | `t − argmax_{u≤t} P(u)` while volume elevated | rolling | < 4 s | > 8 s with elevated volume |
| `RC` (range compression) | `range(P, last 5s)/range(P, prior 15s)` after vol spike | 5/15 s | > 0.6 | < 0.4 after vol spike |
| `topN_flip` | top-10 buyer net-position decreasing fraction | 5 s | < 0.2 | ≥ 0.4 |
| `USR_W` | `unique_signers / trades` | 2 s, 10 s | ≥ 0.5 (rising) | < 0.4 (falling) |
| `f_<50ms_W` | fraction inter-arrivals < 50 ms | 2 s, 5 s | ≤ 0.25 | > 0.35 |
| `frac_same_slot_W` | fraction consecutive buys in same slot | 2 s | ≤ 0.20 | > 0.30 with N≥4 |
| `Gini_size_5s` | Gini of buy SOL sizes | 5 s | ≥ 0.45 | < 0.30 |
| `H_size_5s` | Shannon entropy normalized | 5 s | ≥ 0.7 | < 0.4 |
| `creator_outflow_flag` | binary: creator-or-top-1 net-sold ≥ 1 SOL last 5 s | 5 s | 0 | 1 (hard kill) |
| `dev_buy_sol` | first creator swap SOL | t=0 | ∈ [0.5, 4.5] | < 0.5 or > 4.5 |
| `bundle_supply_captured_pct` | bundle wallets' tokens / total | t=0 | < 15% | ≥ 15% |
| `frac_funded_from_creator_5buys` | first-5 buyers funded by creator within 72 h | t=0 | 0 | ≥ 1 |

**EWMA recursions (no allocation, no leakage):**
```
α = 1 − exp(−Δt / τ)             # τ = 30 s for OFI z-score
μ_t = (1−α)·μ_{t−1} + α·X_t       # update AFTER reading current value
σ²_t = (1−α)·σ²_{t−1} + α·(X_t − μ_{t−1})²
z_t  = (X_t − μ_{t−1}) / sqrt(σ²_{t−1} + ε)   # use μ_{t−1}, σ_{t−1} only
```

**Composite Stage-A reject rule (single eval, ~20 µs):**
```
reject if any of:
  f_<50ms_2s > 0.35 AND median_inter_buy_2s < 80 ms
  frac_funded_from_creator_5buys ≥ 1
  bundle_supply_captured_pct ≥ 0.15
  creator_outflow_flag = 1
  Gini_size_5s < 0.30 AND H_size_5s/lnB < 0.40
  dev_buy_sol < 0.5 OR dev_buy_sol > 4.5
  topN_flip_5s ≥ 0.40
  AR_sell_2s > 1.0
  IPD < 0.5 AND B_5s > 1.5 · B_avg_30s
```

**Stage-B classifier:** LightGBM, ~200 trees, depth 6, ~50 features (everything in §7's catalog at signal time). Compile via Treelite or `lleaves` to a shared object loaded by the Rust trader; expected inference 5–50 µs CPU, no GPU/FPGA needed.

---

## 5. Entry, scale, and exit rules at structural level

**Sizing curve (the heart of the strategy):**

| Phase | Size | Trigger | Why |
|---|---|---|---|
| ARMED | 0 SOL | Stage-A pass + p(2x) ≥ 0.35 | Free option; subscribe to curve account, prepare scout |
| SCOUT | 0.005–0.02 SOL (≤ 0.04% of Y) | Real curve v_1s > 0 + nOFI_2s ≥ 0.02 | Paid information probe; insufficient to attract toxic counterparty |
| SCALE | TARGET − SCOUT | Scout fill confirms + AR_sell_2s ≤ 0.3 + p(2x) ≥ 0.55 | Mechanical proof of continuation past first-impulse window |
| PARTIAL | sell 50% | price ≥ 1.6×blended OR (≥1.3× and a_1s<0 ≥500 ms) | Recover scout+slippage cost; convert risk into runner |
| RUNNER | remainder | trailing 18% from peak; hard time-stop 60 s post-SCOUT_FILLED | Capture the 1-in-N tail; bounded by hard time stop |

**TARGET_SIZE** uses fractional Kelly with conservative damping, capped at hard limits:
```
f* = (p · |r_win| − (1−p) · |r_loss|) / (|r_win| · |r_loss|)
TARGET_SIZE = clamp( 0.25 · f* · bankroll,
                     SCOUT_SIZE,
                     min(per_mint_cap, 0.005 · Y(t)) )   # ≤0.5% of curve SOL
```

The 0.5%-of-Y cap is mandatory: at 30 SOL initial Y, 0.5% = 0.15 SOL, which moves price ~1% mechanically. Above that you self-impact and become your own bait for the next bundle.

**Exit rules ranked by precedence (highest first, evaluated every event):**

1. **Hard rug kill.** `creator_outflow_flag` OR `topN_flip ≥ 0.5` OR `AR_sell_2s > 1.0` OR `IPD < 0.5` while in position. Fire warmed sell immediately, do not check price.
2. **Scratch exit during scout.** While in SCOUT_FILLED, if drawdown from entry > 8%, fire warmed sell. Asymmetric: scout losses are bounded by size.
3. **Partial trigger.** As above; converts scout-recovery + half target into realized SOL.
4. **Trailing peak rule.** Active in RUNNER state. `exit when price ≤ peak·(1−0.18)`.
5. **Microstructure exit.** While in profit ≥ 30%, if `a_500ms < 0 AND ã_1s < 0 for ≥ 700 ms`, exit. Captures the canonical "blowoff stalling" pattern before the trailing peak rule would trigger.
6. **Hard time stop.** 60 s since SCOUT_FILLED. Empirically, 85% of insider-driven rallies are over by this point.
7. **Migration trigger.** If `BondingCurve.complete = true` event observed, exit on the PumpSwap pool side of the migration (graduation often pumps then dumps; do not assume liquidity continuity).

---

## 6. Execution-speed plan

**Buy path:**

Use Raptor `POST /quote-and-swap` (single round-trip) → `POST /send-transaction` (Yellowstone Jet TPU, direct-to-leader). Force `dexes=Pump.fun,Pumpswap` to skip aggregation. Set `priorityFee="veryHigh"` for SCOUT, `"turbo"` for SCALE; `slippageBps=2500` early in curve, drop to 1500 above 50% bonding progress; `txVersion="v0"`. Tip via `tipAccount`/`tipLamports` is a single-tx tip transfer, not a Jito bundle. For the 1-in-N races where bundle semantics matter, send the same built tx in parallel to Jito `/api/v1/transactions` — first-confirmed wins, second is a no-op due to balance/idempotency. Public Raptor Beta is free; no extra infra needed.

**Warmed sell path (the most important latency optimization):**

The constraint is that a signed Solana tx is valid only while its `recentBlockhash` remains in the validator's BlockhashQueue — 150 slots, **~60 s worst case**. There is no placeholder-blockhash trick; the blockhash is signed over.

Pattern:
1. On SCOUT_FILLED, call `POST /swap-instructions` (not `/swap`) with the held position size. Returns `setupInstructions[]`, `swapInstruction`, `cleanupInstruction`, `addressLookupTableAddresses[]`.
2. Build the v0 message client-side. Fetch fresh blockhash from a hot RPC. Sign. Hold signed bytes in memory.
3. **Refresh timer at 20 s** (well inside the 60 s cliff): re-fetch blockhash, rebuild message from cached ix list, re-sign. ~5–20 ms cost. No `/quote` call needed if `quoteResponse` is cached and slippage tolerance still covers current pool state.
4. Re-quote (via `/stream` WS, or polled `/quote`) every 2 s; if amountOut drifts > 30%, rebuild ix list (one extra HTTP).
5. On trigger: `POST /send-transaction` with the cached signed bytes. Race a parallel send to Jito `/api/v1/transactions`. Whichever lands first wins.

**Durable nonce alternative (use only for runner state, not scout):** create a per-bot SystemProgram nonce account (~0.0014 SOL rent), prepend `nonceAdvance` ix as the first instruction in your sell tx, sign once, hold indefinitely. The catch: every `nonceAdvance` consumes the current nonce; if you fire two warmed exits without advancing, the second invalidates. Pattern is correct for *one* held position at a time per nonce account. Maintain a small pool of nonce accounts equal to max concurrent positions.

**Compute budget:** 200,000 CU is enough for a single-hop pump.fun swap with ATA creation. Don't over-budget — wasted CU is wasted priority dollars per CU. Set `computeUnitLimit=200_000`, `computeUnitPriceMicroLamports` derived from Raptor's recommended `priorityFee.levels.veryHigh`, capped at `maxPriorityFee=1_500_000` µ-lamports.

**What to prepare BEFORE entry:**
- Per-mint BondingCurve PDA + Associated BondingCurve ATA derivations.
- Bot's ATA for the mint (idempotent create ix bundled with first buy).
- Pre-fetched FeeProgram CPI accounts (`fee_config`, `global_volume_accumulator`, `user_volume_accumulator`).
- Cached blockhash refreshed in a background thread (`getLatestBlockhash` every 2 s).
- Connection pools to Raptor `/send-transaction` and Jito `/api/v1/transactions`, pre-warmed (TLS handshake + HTTP/2 stream open).

**What to prepare AFTER scout fill:**
- The warmed sell ix list (requires post-fill token balance to be known).
- The signed warmed sell tx + the 20 s refresh loop.

---

## 7. Logging / training plan

**Two append-only Parquet datasets, partitioned by `dt=YYYY-MM-DD/hr=HH/`, ZSTD-compressed.**

### `events.parquet` — raw immutable event log

```
mint, slot, tx_index_in_block, instr_index, inner_instr_index,
event_type {CREATE, BUY, SELL, MIGRATE, COMPLETE, BC_ACCOUNT_UPDATE},
signer, sol_in, sol_out, tokens_in, tokens_out,
v_sol_post, v_tok_post, real_sol_post, real_tok_post,
fee_paid, priority_fee, jito_tip,
bundle_id (nullable), bundle_pos (nullable, 0..4),
shred_recv_ns, account_recv_ns, source {SHRED, ACCOUNT}
```
Sort within file by `(slot, tx_index, instr_index)`.

### `candidates.parquet` — one row per launch with point-in-time feature snapshots and forward labels

```
mint, creator, create_slot,
signal_slot, signal_wall_ns,           # when bot first armed
strike_slot, strike_wall_ns,           # scout buy fill slot
entry_price, entry_sol_spent, entry_tokens_received,
scale_in_slots[], scale_in_prices[], scale_in_sol[],
partial_slots[], partial_prices[], partial_sol[],
exit_slot, exit_price, exit_sol_received,
exit_reason {TP, SL, TIMEOUT, MIGRATION, RUG, KILL_FLOW, KILL_INSIDER, MANUAL},
state_transitions: list<{from_state, to_state, slot, wall_ns, trigger}>,

features_at_signal: struct<all signals from §4 PLUS creator-history features:
  creator_prior_launches, creator_prior_2x_rate, creator_prior_graduation_rate,
  creator_repeat_rugger, creator_age_slots, creator_balance_sol,
  bundle_at_deploy, bundle_size, bundle_buy_sol_total,
  bundle_supply_captured_pct, bundle_unique_funders, jito_tip_sol,
  social_metadata_present, name_token_dup_24h>

features_at_strike, features_at_scale, features_at_partial, features_at_exit:
  same shape as features_at_signal

outcomes: struct<
  price_t_plus_1s, _2s, _5s, _10s, _30s, _60s,
  max_price_0_to_{1,2,5,10,30,60}s,
  min_price_0_to_60s,
  max_fwd_logret_{1,2,5,10,30,60}s,
  hit_2x_within_{10,30,60}s,
  hit_3x_within_60s, hit_5x_within_60s, hit_10x_within_60s,
  time_to_2x_seconds,                  # NaN if not hit
  triple_barrier_label {1=tp, 0_sl, 0_timeout},
  realized_exit_logret,                # actual bot pnl in ln units
  sharpe_like_60s, mfe_mae_ratio>

label_end_slot, schema_version
```

### Label generation (offline, from `events.parquet` only — no real fills required)

**Triple-barrier per Lopez de Prado, parameterized for seconds-scale:**
- Upper: +100% (2x) on `price = vSol/vTok` reconstructed at every event.
- Lower: −30%.
- Vertical: `signal_slot + ceil(60s / 0.4s)` slots.
- Path-aware: scan all sub-second price points; require `low_time < high_time` correctness.

**Auxiliary regression labels** for multi-task / sizing models: `max_fwd_logret_{H}s` and `time_to_2x_seconds` for survival analysis.

### Training methodology

- **PurgedKFold with embargo = 60 s** (label horizon) plus 1-minute buffer. Never random-shuffle.
- **Class weighting** via `scale_pos_weight = N_neg/N_pos`. **Do not use SMOTE** on this feature space — synthetic interpolations between two pump candidates produce non-physical feature combinations. Threshold calibration > resampling.
- **Focal loss** custom objective when easy negatives dominate gradient.
- **Isotonic recalibration** of probabilities on out-of-fold predictions.
- **Threshold τ chosen** by `argmax_τ [mean_OOS_logret(τ) − 1.0·std_OOS_logret(τ)]` subject to minimum candidate frequency.
- **Meta-labeling layer**: M1 = the Stage-A rule emitter; M2 = LightGBM probability head — `M2_prob ≥ τ` is the actual trade gate. M1's recall is high, M2 trades recall for precision.

### Bootstrapping schedule

| Phase | Days | Action | Output |
|---|---|---|---|
| 0 | 0–7 | Passive monitor: log every create + every shred + every BC update; generate triple-barrier labels offline | ~50 k–300 k labeled candidates |
| 1 | 7–14 | Paper-mode FSM with simulated fills using realistic slippage + 2-slot detection lag | `realized_exit_logret` polluted-but-useful labels |
| 2 | 14 | Train LightGBM v1 on Phase-0 chain-derived labels only. Calibrate τ on held-out days | First model |
| 3 | 14–28 | Tiny live (0.005–0.02 SOL scout, 0.05–0.10 SOL target) at top 0.1% probabilities | Few hundred *real* fills, live-haircut multiplier |
| 4 | 28+ | PU-learning expansion + active learning on uncertainty band (p ∈ 0.3–0.7); daily retrain on 30-day rolling window | Self-improving model |

---

## 8. 50-trade paper validation criteria

The current paper session was 9 trades with 22% win rate and slightly negative PnL. To declare the new architecture worth running tiny-live, run **exactly 50 entries** under the new FSM in paper mode, on live shred + live curve data, with realistic slippage simulation.

| Metric | Pass | Marginal | Fail |
|---|---|---|---|
| Stage-A reject rate (of all ARMED) | 70–95% | 50–70% | <50% (gates too loose) or >95% (too tight) |
| Scout fill rate (of SCOUT_LIVE) | ≥85% | 70–85% | <70% (latency or slippage problem) |
| Scale conversion rate (SCOUT_FILLED → SCALE_LIVE) | 25–55% | 15–25% | <15% (scout signal too weak) or >70% (scout-confirm gate too loose) |
| Avg realized loss on KILLED trades | ≤ 8% of scout | 8–15% | > 15% (warmed sell path slow) |
| Avg realized PnL per scratched trade (post-scout, pre-scale) | ≥ −0.6% of scout SOL | −0.6 to −1.5% | < −1.5% |
| Win rate (≥+30% on blended entry) on SCALE_FILLED trades | ≥ 50% | 35–50% | < 35% |
| Median win size (SCALE_FILLED winners) | ≥ +60% blended | +30–60% | < +30% |
| 90th-percentile winner | ≥ +200% | +100–200% | < +100% |
| Net PnL across 50 entries | ≥ +25% on aggregate scout+scale capital | 0 to +25% | < 0 |
| Max consecutive losses | ≤ 8 | 8–15 | > 15 |
| Distribution-detector hit rate before peak (counted on losers that did peak) | ≥ 60% of losers fired a kill BEFORE peak | 40–60% | < 40% |
| `IPD < 0.5` correctly leading exit on losers | ≥ 50% of losers showed it before exit | 30–50% | < 30% |

**Hard gate to advance:** all metrics in "Pass" or "Marginal", at least 8 of 12 in "Pass", and net PnL strictly positive. Any "Fail" on Stage-A reject rate, kill latency, or net PnL → do not advance.

---

## 9. Tiny-live validation criteria

After 50-paper-trade pass, run **exactly 100 live entries** at minimum size: SCOUT 0.005 SOL, TARGET 0.05 SOL. Aggregate capital at risk per trade ≤ 0.05 SOL; total live exposure ≤ 5 SOL.

| Metric | Pass | Marginal | Fail |
|---|---|---|---|
| Live-vs-paper haircut on win rate | ≤ 10 pp drop | 10–20 pp | > 20 pp |
| Median fill latency, signal_slot → strike_slot | ≤ 2 slots (~800 ms) | 2–4 slots | > 4 slots |
| Scout fill rate live | ≥ 75% | 65–75% | < 65% |
| Sell tx land latency, kill_trigger → exit_confirm | ≤ 1 slot (~400 ms) typical | 1–2 slots | > 2 slots |
| Warmed-sell expiry events (blockhash stale) | 0 | 1–2 | ≥ 3 |
| Realized slippage vs Raptor quote | ≤ 1.5% median | 1.5–4% | > 4% |
| Net PnL on 100 entries | ≥ +0.3 SOL absolute | 0 to +0.3 | < 0 |
| Max single-trade loss | ≤ 0.012 SOL (≤ scout + slippage) | 0.012–0.03 | > 0.03 (warmed sell failed) |
| Drawdown from peak equity | ≤ 25% of risked capital | 25–40% | > 40% |
| Time spent in RUNNER state on winners | ≥ 30% of winners reached RUNNER | 15–30% | < 15% (partials too aggressive) |
| Killed trades that would have been winners (false-positive kill) | ≤ 20% | 20–35% | > 35% |

**Hard gate to scale up:** net PnL strictly positive, max single-trade loss within budget, sell latency ≤ 1 slot median, no warmed-sell expiry events. After pass, scale TARGET to 0.25–0.5 SOL while keeping SCOUT at 0.01–0.02 SOL — the asymmetry is the structural protection.

---

## 10. Things NOT to do

1. **Do not price from raw shred instruction fields.** `max_sol_cost` is a slippage cap, not a fill price. The realized fill comes only from BondingCurve account `(virtual_sol_reserves, virtual_token_reserves)` post-tx, or from synthesized post-trade reserves using `vSol' = k/(vTok ± Δt)`. The previous "fake 10x peaks, fake 0x exits" episode is the canonical symptom.
2. **Do not enter full size on the first impulse.** Empirically, >50% of pump.fun launches are sniped same-block by deployer-funded wallets, >55% of those snipers exit within 60 s. Full-size first-impulse entry IS exit liquidity for them.
3. **Do not use buy count alone as a signal.** A bundle is by definition many buys with no curve expansion (pump price moves only on net SOL into the AMM). Always require `nOFI_2s ≥ 0.02` AND `v_1s > 0` AND `a_1s ≥ 0`.
4. **Do not rely on whale lift without absorption.** A single 5 SOL buy that prints +30% then immediately retraces is a manufactured spike. Require `AR_sell_2s ≤ 0.3` for ≥ 1 s before scaling — proves real bid demand absorbing sells.
5. **Do not enter on late-wave continuation without distribution filter.** If `IPD < 0.5` AND `B_5s > B_avg_30s`, buys are growing but per-SOL impact is shrinking — someone is selling into bots. This is the most expensive trade you can take.
6. **Do not enter after confirmation if confirmation already consumed the edge.** If `signal_slot + 2 < current_slot` (more than two slots stale), abort. The bundle's first dump tx is usually within slot+1.
7. **Do not call `getLatestBlockhash` in the buy path.** Background-cache it every 2 s. A blockhash lookup is 30–100 ms and you do not have it.
8. **Do not depend on REST data APIs in the hot path.** Solana Tracker Data API is credit-limited; Raptor `/quote` is fine pre-flight but not for in-position re-evaluation. Use `/stream` WS for live quote refresh; use the in-process synthetic curve for everything else.
9. **Do not use `/swap` instead of `/swap-instructions` for warmed exits.** `/swap` returns a built tx with a baked blockhash; you cannot refresh without another HTTP round trip. `/swap-instructions` returns ix you can rebuild locally.
10. **Do not treat the 2-WS Free cap as a 2-subscription cap.** It is 2 *physical* connections. Multiplex many `programSubscribe`/`accountSubscribe`/`shredSubscribe` calls on each socket.
11. **Do not batch creator-history lookups in the hot path.** Maintain a RocksDB-backed creator dossier in a background thread; cache lookups at signal time.
12. **Do not hold any position past the 60 s post-SCOUT_FILLED hard time stop without a deliberate "RUNNER promotion" decision.** 85% of insider-driven moves are over by then. Continuing past requires a fresh active decision, not a default.
13. **Do not ladder TP at fixed price multiples (50% @ +25%, 25% @ +50% style).** This is the open-source default (bloodbee, TreeCityWes, justindev361) and it is wrong for this regime — it sells into the absorption-failure that the FSM should be reading as a kill, AND it leaves runner upside on the table by fixed-targeting. The microstructure exit (a_500ms < 0 ∧ ã_1s < 0 ≥ 700 ms) replaces it.
14. **Do not pay for new infra before the 50-paper / 100-tiny-live gates pass.** The Raptor public beta + Solana Tracker Free 2-WS multiplexed is sufficient through tiny-live. Upgrade only when sustained live PnL justifies the credit cost.
15. **Do not retrain on the most recent 1 hour of data.** Labels for those candidates are still maturing. Embargo 1 hour minimum, 60 s minimum strict.
16. **Do not include `realized_exit_logret` in the primary classification head's training set.** It is polluted by your own exit policy and will teach the model to avoid trades that would have been profitable under a better policy. Train the entry head on chain-derived triple-barrier labels only; train the exit head separately on intra-trade state logs.
17. **Do not skip the migration handler.** When `BondingCurve.complete = true`, liquidity moves to a PumpSwap pool with different fee structure (0.20% LP + 0.05% protocol + creator fee) and different price-impact characteristics. Treat migration as a forced exit unless explicitly designed for post-graduation.
18. **Do not buy when `bundle_supply_captured_pct ≥ 15%`.** Insiders own enough supply that any rally is just their distribution window. The Pine Analytics 87%-profitable-snipers number is dominated by exactly this case.

---

## Conclusion

The bot does not have a parameter problem; it has a **structural exposure problem**: full-size first-impulse fills price-stopped on a tape that lies. The redesign moves the bot from a one-shot strategy that competes with bundlers on speed (a race against actors co-located in Frankfurt with 0.1 ms RTT to leaders, which a residential or regional VPS will lose forever) into a **two-phase information-buying strategy** that competes on signal quality. The scout is an explicit purchase of post-fill signal, sized so that its worst case is bounded and its information yield is maximal. The warmed sell turns the existing Raptor `/swap-instructions` endpoint into a sub-100ms exit that does not require new infra. The Stage-A rule layer rejects the manufactured-impulse class that comprises the dominant share of pump.fun launches in 2026, and the Stage-B LightGBM head — trainable from chain data alone with no live trading required — converts the residual into ranked probabilities. **The single highest-impact change is the scout-then-scale architecture**, because it is the only change that mechanically ensures losses are bounded by an amount the operator chooses, regardless of what the model gets wrong; everything else is an optimization on top of that invariant.