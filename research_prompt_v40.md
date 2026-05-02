# Deep Research Prompt: Pump.fun Sniper — Why V39 Predator Loses Despite 59% Win Rate

## Context

You wrote `solana_sniper_v39_predator.py` which I deployed in paper mode for live observation. Goal: turn $8 ($18 / 0.21 SOL) → $100. We need a code-level (no external services, no paid infra) mechanism that **delivers absolute wins, big wins, and profit from dumps** at this capital tier.

V39 is the most sophisticated version yet — asymmetric sizing (micro/scout/core), dump-rebound mechanism, late breakout scalp, scale-in for winners, dynamic trailing floor, runner moonbag. Despite all this, **session is net negative.**

I need you to do a much deeper research round and design V40 that actually wins.

## V39 Live Results (~17 min paper run)

- **29 trades** | W=17 / L=12 = **59% win rate**
- **Net PnL: -0.0085 SOL = -$0.71** (LOSS despite winning more than losing)
- Trade types observed:
  - Micro-probes (0.006 SOL weak signals): 18 trades — most close at exact break-even
  - Scout dump-rebounds: 6 trades — mixed, 2 wins, 4 small losses
  - Co-buy entries: 3 trades — all break-even or small loss
  - Core-sized momentum (0.025 SOL): 1 trade (BT5XXivP) — **-$0.44 with scale-in compounding**

## Why Win Rate Is a Lie Here

The 59% "win rate" is misleading. Breakdown of actual outcomes:

- **~14 trades** closed at `peak=1.00x recv=cost pnl=+0.0000` (no movement, paper mode rounds to break-even, counted as wins because pnl ≥ 0)
- **3 actual wins** in the +$0.03 to +$0.09 range (peaks 1.12-1.26x)
- **12 actual losses** in the -$0.02 to -$0.44 range
- **1 catastrophic loss** of -$0.44 (BT5XXivP — core size + scale-in compounding RIGHT before reversal)

In live mode, those 14 "break-even wins" become **small losses due to slippage + fees**. So real-world V39 performance would be even worse than -$0.71.

**Aggregate math:**
- Average actual win: ~+$0.05
- Average actual loss: ~-$0.07 (with one -$0.44 outlier)
- Need to win ~60%+ AND average win > average loss to be profitable
- Currently winning ~60% but average win < average loss → guaranteed bleed

## Specific Failure Modes Observed

### 1. Dump-rebound enters AFTER the bounce played out
- 8nfzmVYx: +30% price recovery during rebound watch → entered → small win +$0.07
- FSXWdYTe: +24% price recovery during watch → entered → token immediately rolled over, peak=1.00x, exited -29%, **-$0.15**
- 9GfGKhzG: rebound confirmed (5B/5S → 6B/2S) → entered → -34% in seconds, **-$0.17**
- Pattern: by the time we *confirm* the bounce (2 up-ticks), the bounce is exhausted

### 2. TP triggers on peak crossing but sells at current price
- 8SXywvtn: peak=1.19x triggered TP RUNG 2 sell-100%, but actual sell happened at ~0.85x (price had retraced) → -$0.07
- 7FsaGJPD: peak=1.26x sold 100% at near-peak → +$0.09 win (this worked)
- Inconsistent. Trail floor + TP rung both react to peak, not to current price decay

### 3. Trail floor too loose for fast gaps
- BT5XXivP: peak 1.13x, floor=1.03x, but mult observed at 0.76x in next cycle → blew through floor → -$0.44
- The dynamic floor assumes gradual decay. Solana memes gap.

### 4. Scale-in compounds loss exactly at the wrong time
- BT5XXivP: scaled in at peak=1.13x, mult=1.13x. Within 4 seconds: gap to 0.76x → -24% loss applied to **larger** position
- Scale-in fires on "proof of life" but gives no protection against the dump that often follows the pump
- Net effect: scale-in turns a -$0.32 loss into -$0.44

### 5. Core size doesn't have an edge advantage
- Score-based sizing assumes high-score setups have higher win probability
- BT5XXivP had score=8 (high) → still rugged
- High score may correlate with "looks like a real pump" but devs are sophisticated about creating that look

### 6. Most "no momentum" exits are paper-mode artifacts
- Bot exits at exactly `mult=1.00x` after 20-40s timeout because curve hasn't moved
- In paper mode this rounds to recv=cost, pnl=$0.00
- In live mode this would be -1 to -3% due to bid/ask spread + fees

## What Would Actually Win

**The user demands:**
1. **Absolute wins** — every trade closes positive (mathematically aspirational; we know the floor is high)
2. **Big wins** — when we win, win meaningfully (avg $0.30+ not $0.05)
3. **Profit from dumps** — the user is convinced this is possible and we haven't found the mechanism
4. **Code-level only** — no paid Geyser, no Jito, no Twitter scraping, no external services
5. **At $8 capital** — must work without scale

## The Research Questions for V40

### Question 1: What separates the rare 1.5x-3x runners from the 1.0x-1.10x fakes?

V39 enters too many setups that look real but don't run. We need a pre-entry signal that's predictive of magnitude, not just direction. Specifically:
- Is there a bonding curve liquidity profile that distinguishes real pumps from dev pumps?
- Is there a holder-pattern (e.g. distribution of token amounts across early holders) that predicts continuation?
- Is there a creator wallet behavioral signal (buying pattern, fee structure) that hints at "this dev will let it run"?
- Is there a Solana program-level signal (account state, instruction sequence) that distinguishes "honest launch + organic buyers" from "atomic-bundle dump farm"?

### Question 2: Is there ANY spot mechanism to profit from a dump in flight?

We've established that pre-confirmation visibility doesn't exist on Solana, no synthetic shorts exist for pre-graduation tokens, no perp markets list them. But:
- Can we exploit the bonding curve PDA's deterministic price formula to construct a paired-trade where one side profits from the dump?
- Are there pump.fun program-level features (e.g., **referral fees, creator fee splits, cashback claim mechanics**) that effectively let a trader profit from someone else's loss?
- Is there a way to atomically buy + sell in the same tx using flash loans (Kamino, Solend) that captures the dev's pump at the peak before the dump completes?

### Question 3: Why is the dump-rebound mechanism failing to generate net profit?

Per V39 logs: rebound confirmation works (we detect the bounce), but post-entry the tokens often roll over immediately. Either:
- Our rebound criteria are too lax (any +0.5% × 2 ticks)
- The rebound itself is the dev/insider scalp that we're following too late
- We need a stronger continuation signal (e.g., 3+ unique buyers in the post-rebound window, accelerating buy ratio, bonding curve growth >X% in <Y seconds)

### Question 4: How does scale-in actually work for moonshots, given the gap risk?

V39's scale-in adds at peak=1.10x. But the trade either:
- Continues to moon (scale-in pays off — never observed yet)
- Reverses (scale-in compounds the loss — observed once, -$0.44)

What's the criterion for distinguishing "this is going to 5x" from "this is about to dump 30% in 2 seconds"?

### Question 5: Why are we burning half our equity on probes that close at zero?

PAPER_SCOUT_EVERY_VALID_MINT generates 14+ break-even trades that in live mode become small losses (fees + slippage). This is dead activity — high noise, no signal extracted. Either:
- Probes need to size much smaller (<0.001 SOL each)
- Probes need a way to convert into core position when something happens
- Probes should be eliminated entirely if they don't generate edge

## What We Have That Works

1. **V36 buyer-history filter** — distinguishes established wallets from dev alts (3/3 vs 5/5 established)
2. **V37 cashback flag detection** — byte 82 of bonding curve PDA tells us if traders earn fee rebates
3. **V32 smart-wallet sell signal** — exits when a watched wallet dumps the same token
4. **Race-the-dump exit** — catches active dumps in progress
5. **V30 dynamic trail floor** — peak-aware SL that breathes for runners

## Constraints (Code-Level Only)

- Free Helius RPC (logsSubscribe, getAccountInfo, getSignaturesForAddress, getTransaction at processed/confirmed commitment)
- Python execution
- Paper mode for testing, live mode requires Jupiter route OR direct pump.fun program instructions
- No paid Geyser, no Jito mempool, no Twitter scraping, no Telegram alpha groups, no insider channels
- No external alpha sources — purely on-chain data
- Capital: $8 (0.21 SOL)

## Deliverable Request

Design V40 that:
1. **Eliminates break-even noise trades** — every position taken has clear positive expectancy or isn't taken
2. **Captures runners more reliably** — TP triggers on actual exit price, not peak crossing; trail floor handles 50% gaps in one cycle
3. **Sizes scale-in only when momentum is verified to continue** (not just "peak hit 1.10x once")
4. **Provides the actual on-chain mechanism for profit-from-dumps** that we've been unable to find
5. **Maintains volume for Big Wins** — don't go back to V25 elite-only silence; find the entry filter that catches real pumps without donating to rugs

Specifically address:
- The **rebound timing problem** (we enter after the bounce is over)
- The **TP/peak mismatch** (peak triggers, current price exits)
- The **scale-in gap risk** (compounding into reversal)
- The **break-even probe waste** (14 noise trades produce no signal)
- The **core-size catastrophic loss** problem (one -$0.44 wipes 4-8 small wins)

Output: a complete `solana_sniper_v40.py` file with explanatory commentary on each mechanism, plus a written explanation of the research findings that justify each design choice.

The user expects this to win. Not 59% break-even win rate — actual money. If a mechanism can't deliver that at $8 capital with code-level free-tier infrastructure, say so explicitly. We've already eliminated paid infrastructure paths. Don't go in circles.
