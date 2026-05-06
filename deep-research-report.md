# Exact Strategy Architecture for a Solana pump.fun Moonshot Sniper Bot

## Blunt diagnosis

Your bot is still choppy because it has fixed **price truth** but not **market-quality truth**. Raw shreds are fast intent, and BondingCurve virtual reserves are the correct pre-graduation pricing surface, but the current continuation board still asks a weak question: *did price continue?* The stronger question is: *did the launch reach this price through the kind of path that usually keeps expanding?* Research on Pump.fun’s launch microstructure shows that the amount of SOL on the curve by itself is not enough; the strongest uplift comes from **how fast** the same curve state is reached, **who** is participating, and whether the token is already showing dump-like behavior. Tokens that reach the same curve state in **fewer trades** have materially higher odds of graduating, while bot-heavy participation and dump behavior are much worse signs. citeturn34view0turn32view0turn35view0

The market you are trading is structurally adversarial. Pump.fun became a dominant memecoin venue on Solana, but the success base rate is still terrible: one academic study found fewer than 2% of tokens graduated in the sampled period, and a second September 2025 study describes a graduation rate well below 1%. The same September 2025 paper also finds dumps are not a side case: among tokens with enough swaps to analyze, over 92% exhibited at least one dump event. That matters because the same paper shows there is a **mechanical incentive to sell before graduation**: the virtual-liquidity phase absorbs sells better than the post-migration AMM, so insiders and coordinated early wallets are often rewarded for unloading before the handoff. In plain terms, late confirmation is often just you volunteering to be the exit bid. citeturn30view1turn33view1turn35view0

So the current bot’s failure mode is specific, not vague. It is buying too much size at the first moment when curve truth looks acceptable, but before the market has proven **absorption**, **breadth**, and **lack of distribution**. That leaves it in the worst zone: too late for the pure speed edge, too early to know whether the markup is real. citeturn34view0turn32view1turn35view0

## Proposed winning architecture

The next architecture should be three layers, not one:

**Layer one is speed detection.** Keep market-wide pre-confirmation shreds for earliest intent detection. That is still your best source for “something is being manufactured right now.” citeturn9view0

**Layer two is quality gating.** Keep BondingCurve reserve updates as the sole pre-graduation price truth. Add a second, mint-scoped actor-flow layer from the existing Solana Tracker Datastream connection: `bundlers`, `insider`, `dev_holding`, `top10`, and `sniper` rooms joined only for armed candidates. Those rooms are specifically designed to stream holder-percentage changes per token over one multiplexed WebSocket connection, which fits your two-connection constraint if the first connection carries both `shredSubscribe` and `programSubscribe`. Solana’s WebSocket PubSub is explicitly built on a persistent connection with multiple subscriptions, and Solana Tracker documents both `shredSubscribe` and `programSubscribe` on the RPC WebSocket side and room-based join/leave semantics on the Datastream side. citeturn36view0turn8view0turn9view0turn24view0turn25view0turn25view1turn26view0turn22search0

**Layer three is risk geometry.** Stop trying to make the first decision be both “is the launch real?” and “how much size should I own?” Those are different decisions. The first decision should be a **tiny scout**; the second should be a **scale** that is only unlocked by real curve continuation plus absorption. That preserves frequency while changing the PnL geometry. You do not solve this by trading less. You solve it by **sampling more states with tiny size** and **concentrating size only where the market has already paid for the right to own more**. That is exactly where your current bot is weak. citeturn34view0turn35view0

The practical two-WebSocket topology is:

- **WebSocket A: Solana Tracker RPC**  
  Run `shredSubscribe` and `programSubscribe` together. Use shreds for intent and signer topology. Use BondingCurve program updates for reserve-truth pricing and reserve-velocity calculations. citeturn36view0turn8view0turn9view0

- **WebSocket B: Solana Tracker Datastream**  
  On candidate arm, dynamically join `bundlers:{mint}`, `insider:{mint}`, `dev_holding:{mint}`, `top10:{mint}`, and `sniper:{mint}`. Optionally join `latest` globally to seed metadata and the initial risk snapshot, because new-token messages already include risk fields such as snipers, insiders, top10, and dev share. If the token survives toward migration or you hold through it, join an aggregated or primary price room for the post-graduation handoff. Solana Tracker’s aggregated price room is explicitly built to reduce single-pool manipulation and automatically rotates main pools after migration. citeturn22search2turn22search0turn24view0turn25view0turn26view0turn4search3turn22search3

- **Execution path: Raptor over HTTP first**  
  Use `quote-and-swap` and `send-transaction` in the hot execution path, with direct-route restrictions and aggressive fee tuning. Do **not** spend your second WebSocket on Raptor `/stream/swap` yet; under your cap, the second socket is more valuable as actor-flow truth than as warmed quotes. Build exits immediately after fill using HTTP, and only graduate to `/stream/swap` later if tiny-live proves exit build latency is the actual limiter. Raptor documents `quote-and-swap` as a single-request quote+transaction builder, `send-transaction` as Yellowstone Jet TPU transmission with automatic resending, and `/stream/swap` as ready-to-sign transaction streaming that re-sends every 10 slots without updates. citeturn15view0turn15view1turn17view0

One more architecture fix is non-optional: harden your parser and executor for Pump.fun `create_v2`, Token2022-owned bonding curves, and Mayhem-mode fee-recipient differences. Pump’s public docs explicitly call out that `create_v2` coins use Token2022 for the bonding curve token accounts and may require a different fee recipient in buy/sell account positions when `is_mayhem_mode` is true. If you do not normalize this now, you will get silent parse drift and sporadic execution failures on a growing subset of launches. citeturn37search0turn18view0

## Exact strategy state machine

This should be an **event-driven state machine**, not a single “buy or skip” branch. The purpose is to separate discovery, tiny information purchase, proof of expansion, de-risking, and runner management.

**Discover.** A mint enters the machine when the first Pump.fun buy sequence appears in shreds and the bonding curve exists in cache. Initialize ring buffers for shreds, reserve updates, wallet participation, and actor-flow deltas. Join the Datastream risk rooms for that mint immediately if the first shred burst is above discovery noise. citeturn9view0turn24view0turn25view0turn22search0

**Arm.** The token moves from Discover to Arm only if the first burst is an **A-grade impulse**: very high reserve-intent pace, enough width, and no hard red flags from immediate actor concentration. Arming does **not** permit full entry. It just tells the bot this mint is worth spending latency and room subscriptions on. The arm should expire quickly. If fresh pressure does not continue almost immediately, cancel the strike and leave the rooms. This matches the research result that successful launches are fast and momentum-driven, and that slow fragmented accumulation is materially worse. citeturn33view0turn34view0

**Scout.** On an armed token, send a **tiny scout** only if the burst is still live on the latest curve snapshot. Scout size should be **15% to 20% of intended max position**, never more. The moment the scout is sent, the bot must also prepare its scratch exit path. If the signal invalidates before transmission, drop the order and do not send. After transmission, assume you cannot cancel; that is why the scout must stay tiny. Raptor can build the transaction in one request and return `lastValidBlockHeight`, which is enough for a fast sign-and-send loop. citeturn15view0turn21view3

**Confirm.** A scout by itself proves nothing. The token only moves to confirmation if, after the scout, the curve makes a fresh higher high **and** the first material sell is absorbed. “Material sell” should mean a sell that either causes a meaningful local log-return shock, consumes a non-trivial fraction of the last one-second gross buys, or retraces a meaningful fraction of the impulse leg. If that sell is not absorbed quickly, scratch immediately. If the token cannot print a post-scout higher high inside a tight time budget, scratch immediately. This is the missing gate in the current bot. citeturn35view0turn33view1

**Scale one.** Only after post-scout higher high **and** absorbed sell should you add size. The first add should take the position to about **50% of max size**, not 100%. Adding here is what you are currently doing too early. The important twist is that the add is not unlocked by “continuation happened”; it is unlocked by “continuation survived a real test.” citeturn34view0turn35view0

**De-risk.** On the first genuine pop after Scale One, sell enough to reduce the position back near scout-size economics. In practice that means cutting **35% to 60%** of the current inventory so the remaining risk is near flat after fees. This is the core of the near-zero-loss structure: your trade is not allowed to sit at half or full size waiting to see if it becomes a runner. The market must first finance that right. This is design, but it directly addresses the paper’s finding that simple buy-and-hold on curve state alone is not a good edge and that dynamic strategies can do better by changing exposure across states. citeturn33view0turn34view0

**Scale two.** Full size is only allowed after the trade has already started paying and the token proves a second expansion leg: another absorbed sell, another higher high, and still no actor-flow distribution. Then, and only then, add the remaining size up to max. If you reach this state often enough, you have solved the real problem. If you never reach it, your signal quality is still bad. citeturn35view0turn34view0

**Runner.** After the first partial, the remaining position is treated as a financed runner. It stays alive only while expansion remains positive and distribution remains absent. The runner dies on the first structural distribution trigger, not on hope. citeturn35view0turn33view1

**Cooldown.** After scratch or distribution exit, put the mint into a short cooldown. Re-entry is allowed only if the token rebuilds a new base, shows new-wallet participation rather than the same cluster recycling, and passes the full Arm test again. Do not machine-gun the same bait mint. The point is not lower frequency overall; it is lower frequency of paying twice for the same fake manufacture. citeturn35view0turn32view1

## Exact hot-path signals to compute

The edge comes from computing the **path** to the current curve state, not just the state itself. The empirical research is clear on this: curve position alone is too weak; speed of capital accumulation, participant mix, and dump behavior add the real signal. Your hot path should therefore compute three classes of features, all normalized to rolling percentiles by token age and curve stage. citeturn34view0turn32view0turn35view0

**From pre-confirmation shreds, compute intent and topology.**

- `gross_buy_sol_{100ms,250ms,500ms,1s}`
- `gross_sell_sol_{100ms,250ms,500ms,1s}`
- `net_sol_{100ms,250ms,500ms,1s}`
- `buy_count` and `sell_count`
- `unique_buyers`, `new_buyer_ratio`, `repeat_buyer_ratio`
- `top_wallet_buy_share`
- `buyer_hhi` or equivalent concentration score
- `median_buy_size`, `p90_buy_size`, `max_buy_size`
- `mid_size_buy_share` so the bot can separate “one whale lift” from “broad medium-size ladder”
- `inter_buy_interval_mean` and `inter_buy_interval_cv`
- `buy_then_sell_flip_rate_2s`
- `wallet_prior_sum` from your historical wallet-role database
- `fingerprint_prior_sum`, where fingerprint is built from instruction layout, CU price, account order, tip pattern, and other repeatable bot-template markers

This is where you repurpose the old 3,235-wallet system. Do **not** use those wallets as copy-trade triggers anymore. Use them as role priors. The September 2025 paper finds top-trader presence has only a modest uplift and is double-edged; several “top” wallets show extreme sell-only asymmetry, which is consistent with acting as profit-taking or aggregation endpoints rather than long-side conviction. In other words, wallet history is useful, but mostly as “what role is here?” not “follow this wallet blindly.” citeturn32view1turn31view2

**From programSubscribe reserve updates, compute curve truth.**

- `curve_price`
- `virtual_sol_reserves`, `virtual_token_reserves`
- `reserve_velocity = d(vSol)/dt`
- `price_velocity = d(log price)/dt`
- `price_acceleration = d²(log price)/dt²`
- `trades_to_reach_current_vSol`
- `net_sol_per_trade`
- `price_change_per_net_sol`
- `pullback_depth_from_local_high`
- `reclaim_time_after_pullback`
- `material_sell_absorption_rate`
- `time_since_last_marginal_high`
- `failed_continuation_count`
- `compression_ratio = realized_price_range / gross_buy_sol`
- `gross_to_net_ratio = gross_buy_sol / max(net_buy_sol, epsilon)`

The single most important curve-derived feature should be **low trades-to-vSol**. The paper’s strongest result is that tokens reaching a given vSol in fewer trades have much higher success odds than tokens that grind to the same vSol through many small swaps. That means your bot should treat “same price, fewer steps” as materially better than “same price, lots of churn.” citeturn34view0

**From Datastream actor rooms, compute distribution truth.**

- `sniper_total_pct` and `sniper_delta`
- `insider_total_pct` and `insider_delta`
- `bundler_total_pct` and `bundler_delta`
- `dev_pct` and `dev_delta`
- `top10_total_pct` and `top10_delta`

The hard-red-flag interpretation should be simple:

- **`dev_delta < 0` before your full-size unlock**: no-add or immediate exit.
- **`insider_delta < 0` during your confirmation or early hold**: immediate exit bias.
- **`bundler_delta < 0` after the first push**: distribution until proven otherwise.
- **`top10_delta` rising while unique buyers stall**: internal concentration, not healthy breadth.
- **`top10_delta` falling at the same time failed highs accumulate**: distribution, not organic rotation.

These feeds exist precisely to expose changing holder concentration without hitting REST. They are the cheapest available way to make your current continuation logic adversary-aware. citeturn22search0turn24view0turn25view0turn26view0

Normalize every feature by **rolling empirical percentiles** computed against a trailing cohort of launches at the same age bucket and comparable curve stage. Do not hard-code raw SOL thresholds. The architecture is exact if the thresholds are percentile gates; that is regime-adaptive and directly implementable.

## Exact entry, scale, and exit rules at a structural level

A **real markup phase** looks like this:

- the token reaches its next curve state in **few trades**, not many;
- reserve velocity is high and stays high after the first burst;
- unique buyers continue arriving across consecutive short windows;
- no single wallet or cluster becomes the whole move;
- the first real sell is absorbed quickly;
- the token prints a fresh high after that absorption;
- actor-flow percentages do not show dev, insider, or bundler unloading into the move. citeturn34view0turn35view0turn24view0turn25view0turn22search0

A **fake impulse** looks like this:

- lots of buys, but too many trades for too little curve progress;
- one-wallet or one-cluster lift with weak breadth;
- the first real sell breaks momentum and is not reclaimed;
- gross buy flow rises while price range compresses;
- profitable or repeated wallets appear without broader swarm follow-through;
- dev, insider, bundler, or top-holder percentages deteriorate during the push. citeturn34view0turn32view1turn35view0turn33view1

On the buy-timing question, the answer is **yes**: buy earlier, but only with a tiny scout. “No position until confirmation” is losing the one thing your infrastructure is best at, which is queue priority on the very earliest valid states. But “full entry after confirmation” is also wrong, because confirmation is often the exact zone where pre-graduation exit incentives become strongest. The correct structure is:

- **Scout on the first A-grade impulse.**
- **Do not full-size.**
- **Require absorbed sell + post-absorption higher high for the first real add.**
- **Take an early partial so the runner is financed.**
- **Only then allow full-size.** citeturn35view0turn34view0

Mechanically, the near-zero-loss structure should be:

- **Scout = 0.15 to 0.20 U**, where `U` is intended max position.
- **Scale One = +0.30 to +0.35 U** only after absorbed sell + higher high.
- **First partial = sell 35% to 60% of held size** on the first expansion pop so the remaining risk is near flat after fees.
- **Scale Two = remaining size up to 1.0 U** only after a second absorbed continuation.
- **Never average down.**
- **Never scale before the token has survived one real sell.**
- **Never allow the first decision to be “I now own full size.”**

That is the practical closest thing to a zero-or-near-zero-loss structure. The loss does not disappear because fills cannot be canceled once transmitted, but the amount of capital exposed to unproven expansion stays tiny, and most bad trades die while still at scout size.

For distribution detection before collapse, implement a hard **distribution board** in parallel with the continuation board. It should trigger on any of these:

- `material_sell_absorption = false`
- `failed_continuation_count >= 2`
- `compression_ratio` in the worst decile while gross buys remain high
- `time_since_last_marginal_high` exceeds the early-regime budget
- `new_buyer_ratio` decays while sell-side wallets increase
- `dev_delta < 0`, `insider_delta < 0`, or `bundler_delta < 0`
- `top_buyer_flip` where a wallet that was among the largest buyers since strike starts reducing or selling
- a robust return-shock detector on curve returns, using a median/MAD or Shewhart-style violation to flag one-wallet and clustered dump events

The research already gives you the economic rationale for taking these signals seriously: dumps cluster once enough SOL has accumulated, concentrated selling is easier to identify than coordinated pumping, and selling before graduation is mechanically more profitable than after it. Treat a one-wallet dump at high curve state as an auto-exit, not a warning. citeturn33view1turn35view0

## Execution-speed plan

For entries, use **Raptor `quote-and-swap`**, not separate quote and build calls. It returns the quote, base64 transaction, and `lastValidBlockHeight` in one request, which is the shortest practical path under your infrastructure. Use **`send-transaction`** for transmission, because Raptor explicitly routes through Yellowstone Jet TPU with automatic resending and confirmation tracking. That is already a strong landing stack without adding new infrastructure. citeturn15view0turn15view1turn17view0

For route constraints, force the router to behave like a sniper, not like a generic aggregator:

- `onlyDirectRoutes = true`
- `maxHops = 1`
- `dexes` or `pools` allowlist restricted to the relevant Pump.fun / PumpSwap venue while the token is still on launchpad liquidity
- after migration, switch to the post-graduation primary or aggregated price regime and, if needed, a pool allowlist for the most liquid route

Raptor explicitly supports direct-route constraints, DEX filtering, pool filtering, explicit compute-unit overrides, fee caps, and tip fields. That is exactly what you need here. Multi-hop is a latency tax and a failure surface on fresh memecoins. citeturn17view0turn15view0

For fees and compute, follow Solana’s production guidance: competitive priority fee, explicit compute limit, fresh blockhash, and self-managed retry logic if you are handling raw sends yourself. In your case, you can let Raptor manage the send path, but you should still explicitly choose a **high urgency band for scratches and distribution exits**. Entry scouts can be `High` or `VeryHigh`. Scratch exits should be at least as aggressive and usually more aggressive than the scout. Early profit-taking can be a touch less aggressive if the route is stable. Solana’s docs are explicit that fresh blockhash, competitive priority fee, and proper compute-unit sizing are the key landing levers on mainnet. citeturn21view3turn17view0

For slippage, use asymmetric policy:

- **Scout entries:** dynamic slippage with a cap that is wide enough to preserve speed on tiny size.
- **Scale adds:** dynamic slippage with a tighter cap than scouts, because now you have proof and larger size.
- **Scratch exits and distribution exits:** dynamic slippage with the widest cap of all, because certainty matters more than cosmetic exit price.
- **Voluntary profit partials:** tighter cap than scratch exits.

The biggest execution mistake in your current evolution is waiting for the exit signal before beginning exit construction. Do not do that. The moment a scout fills, build an **exact-balance scratch sell**. Rebuild it on every scale change. Rebuild again on migration if you remain in the trade. That way the exit trigger only has to decide whether to transmit, not to start thinking. Raptor’s `quote-and-swap` and `build swap instructions` endpoints are both available for this, but `quote-and-swap` is the better starting point because it minimizes round trips and complexity. Use `swap-instructions` only if you decide you need to compose a custom transaction shell later. citeturn15view0turn14view0

On `/stream/swap`: it is technically excellent for active held-mint exit warmth because it streams ready-to-sign transactions and re-sends the latest transaction after 10 slots without updates. But under your current two-WebSocket cap, it is not the first thing to deploy. The second socket is more valuable as **actor-flow and post-graduation price truth** than as streaming swap quotes. If tiny-live later proves that your true bottleneck is exit build latency rather than signal quality, then promote `/stream/swap` for currently held mints only and demote less valuable Datastream rooms. citeturn17view0

I would not add native entity["company","Jito Labs","solana mev infra"] bundle infrastructure right now. Jito’s docs make clear that bundles provide MEV protection, ordering control, and revert behavior, and Solana’s docs explicitly recommend considering bundles in competitive environments. But your binding constraint today is still **selection and sizing geometry**, not transmission primitive. Raptor plus Yellowstone Jet TPU is enough for the next implementation. If you later add bundles, use them first on **high-value exits and scale orders**, not as a bandage for bad entries. citeturn21view0turn21view1turn21view2turn21view3

## Logging and training plan

You need two logs, not one: a **raw event log** for deterministic replay, and a **feature/event snapshot log** for model training.

The raw event log should persist every hot-path event with nanosecond or microsecond local receive time: shred arrival, decoded instruction, signer, side, SOL amount, token amount, slot, reserve update, Datastream actor update, quote request, quote response, send time, ack time, fill detection, and exit trigger. Do not just store final bars. Store the sequence. The academic Pump.fun studies that extracted signal from these launches did so from transaction-level data and time-resolved curve states, not from coarse candles. Your own future alpha needs the same granularity. citeturn31view1turn30view1

At **signal time** log:

- mint, creator, token-program mode, `create_v2`/Mayhem flags if known
- first seen slot and local receive time
- creator prior score
- wallet-role priors already present
- initial risk snapshot: `sniper_pct`, `insider_pct`, `dev_pct`, `top10_pct`, `bundler_pct`
- initial curve reserves and derived price
- first-burst shred features and percentile ranks

At **strike time** log:

- all current shred/curve/actor features
- `ImpulseScore`, `ExpansionScore`, `DistributionRisk`
- arm reason codes
- room-subscription start times
- expected scout amount bands
- route config chosen for buy

At **entry** log:

- quote price, curve price, and their gap
- signed tx creation time
- send time, ack time, fill time
- priority fee mode, CU price, CU limit, tips
- route, pool, DEX allowlist, direct-route flag
- expected output vs actual output
- entry slippage vs live curve
- exact reason code for the scout

At **scale-in** log:

- whether Scale One or Scale Two
- the exact absorbed-sell event that unlocked it
- new actor-flow deltas since scout
- post-add scratch path build time
- blended average cost

At **partial** log:

- trigger type: structural pop, de-risk requirement, migration handoff
- realized PnL on sold slice
- remaining inventory
- remaining inventory cost basis
- whether the trade is now “financed” by rule

At **exit** log:

- trigger class: scratch timeout, break of strike low, unabsorbed sell, one-wallet dump, dev unload, insider unload, bundler unload, failed continuation, compression, runner trail, migration fail
- trigger timestamp
- time from trigger to wire
- time from trigger to fill
- quote-at-trigger, curve-at-trigger, actual fill
- giveback from local peak
- final MFE and MAE

Store **future outcomes** from every candidate and every event state, exactly at:

- **1 second**
- **2 seconds**
- **5 seconds**
- **10 seconds**

For each horizon store:

- forward return from current curve price
- forward max favorable excursion
- forward max adverse excursion
- forward net reserve delta
- whether a new marginal high printed
- whether a material sell was absorbed
- whether a dump flag occurred
- actor-flow deltas over the horizon

Also store longer runner labels outside the hot path: `30s`, `60s`, and `300s` max return, plus binary labels for `1.5x`, `2x`, `3x`, `5x`, and `10x` achieved before a structural dump. That is how you learn which early states become actual moonshots, not just 8-second pops. The Pump.fun literature explicitly suggests moving toward hazard-rate style continuous success updates as a natural extension; your logging should make that possible, but you do not need a complex model on day one. Start with monotonic gradient-boosted trees or a simple survival model with monotone constraints. citeturn35view0

## Validation, anti-patterns, and the one change to ship next

Define **1R** as the realized loss of a full-size position if it is scratched immediately after the structure invalidates. Then validate the new architecture in two stages.

For the **first 50 paper trades**, run a true A/B shadow against the current continuation bot on the same opportunities. The pass criteria should be:

- **Net expectancy over 50 trades must be positive.**
- **Scale-trade expectancy must be positive and materially better than the current bot.**
- **False-scale rate must be below 35%.**  
  “False scale” means the trade reached Scale One but never reached the first de-risk partial before structural scratch.
- **Median scout-only loss must be at or below 0.20R.**
- **The 90th percentile scout-only loss must be at or below 0.35R.**
- **Missed-runner rate must be at or below 25%** for tokens that go at least +60% from Arm within 10 seconds.
- **Distribution-exit effectiveness must exceed 60%**.  
  That means more than 60% of exits triggered by the distribution board should have a lower price 2 seconds later than the bot’s exit price.
- **Scale rate should sit between roughly 25% and 60% of scouts.**  
  Below that, the bot is still too timid to exploit its speed edge. Above that, it is still over-scaling bait.

For **tiny-live**, do it in two phases, not one.

**Phase A: 10 live trades at 0.10x normal scout size, scout-only.**  
Pass only if:

- no expired or stuck transactions,
- entry-send success exceeds 95%,
- scratch exits are already prebuilt on at least 90% of trades,
- median trigger-to-wire time on scratch exits stays under your internal one-update budget,
- realized entry and exit slippage each stay below 25% of the median first-partial edge seen in paper.

**Phase B: next 10 live trades at 0.25x normal scout size, enable Scale One only.**  
Pass only if:

- false-scale rate stays within 10 percentage points of paper,
- no manual intervention is needed,
- distribution exits still beat the 2-second-later price in at least 60% of flagged exits,
- prebuilt sell refreshes after each size change work every time.

Only after both phases pass should you enable Scale Two and normal scout size.

The **things not to do** are straightforward:

- Do not use raw tape price for PnL, stops, or continuation truth.
- Do not buy full size on first impulse.
- Do not treat buy count as bullish if curve expansion is weak.
- Do not trust one-whale lift without width.
- Do not scale before the first material sell is absorbed.
- Do not treat profitable-wallet presence as sufficient alpha.
- Do not treat creator history as a hard whitelist trigger.
- Do not let dev, insider, or bundler reductions be “warning only.”
- Do not wait for an exit trigger before building the sell transaction.
- Do not average down failed scouts.
- Do not keep post-graduation pricing on BondingCurve truth after migration.
- Do not re-enter the same mint without a full reset in participation and structure. citeturn18view0turn35view0turn32view1turn37search0turn4search3

The **single best next implementation** is this:

**Ship an absorption-gated scout-then-scale architecture immediately.**

That means:

- scout **15% to 20%** of max size on the first **A-grade** impulse,
- build the scratch sell immediately,
- add only after **the first material sell is absorbed and the curve prints a post-absorption higher high**,
- de-risk aggressively on the first real pop,
- allow full size only after the trade has already proved expansion and paid for the right to own more.

That one change is highest impact because it directly attacks your real failure mode: **too many full-economic entries into fake continuations**. It preserves frequency, works with your current infrastructure, creates the cleanest future labels, and does not require expensive new infra. Prebuilt exits matter, actor-flow classification matters, model training matters, but this is the change that most immediately shifts the payoff curve in your favor. citeturn34view0turn35view0turn17view0