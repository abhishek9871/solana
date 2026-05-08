#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

# Phase 20 2026-05-08 — SURVIVOR TRADER.
# Pivot away from fresh-mint sniping entirely.
# Trade ONLY surviving mints (livestream active, age 60s-10min, recent buying).
# Tight exits: +10% profit bank, -5% stoploss, 60s timebox.
# Survivor bias filters out 91% rug rate naturally.
# Expected: 3-8 trades/hour, 60-70% win rate, slow positive accumulation.

export PGG2_RUN_PREFIX="${PGG2_RUN_PREFIX:-phase20_survivor}"

# DRY-LIVE
export PGG2_EXECUTION_MODE=quote
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=1
export PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK
export PGG2_DIRECT_LIVE_CONFIRM=I_ACCEPT_DIRECT_PUMP_RISK
export PGG2_QUOTE_SHADOW_POSITIONS=1
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_SIMULATE_BEFORE_SEND=0
export PGG2_LIVE_SKIP_PREFLIGHT=1
export SMART_WALLET_WS_ENABLED=0

# RugCheck (still gate)
export RUGCHECK_ENABLED=1
export RUGCHECK_REJECT_SCORE=4
export RUGCHECK_TIMEOUT_SEC=0.45
export RUGCHECK_CACHE_TTL_SEC=300
export PGG2_RUGCHECK_GATE_ENABLED=1

# Engagement poller — REQUIRED for survivor trader
export ENGAGEMENT_POLL_ENABLED=1
export ENGAGEMENT_POLL_SEC=4.0
export ENGAGEMENT_POLL_LIMIT=50

# ============================================================================
# PHASE 21 — RE-ENABLE OG FRESH-MINT LANES (winner DNA from earlier phases)
# ============================================================================
# These were disabled in Phase 20 to bet on engagement-survivor. That bet
# produced 0/5 wins. Re-enabling because they're shred-driven (sub-second),
# fire on real on-chain signals, and have profitable history in this codebase.
export PGG2_PRICED_SNAP_ENABLED=1
export PGG2_PRICED_BREAKOUT_ENABLED=1
export PGG2_CURVE_LAG_REVEAL_ENABLED=1
export PGG2_RAW_MOMENTUM_ENABLED=1
export PGG2_BIRTH_FANOUT_ENABLED=1

# ============================================================================
# PHASE 21 — DISABLE engagement_driven (proven loser this session)
# ============================================================================
export PGG2_ENGAGEMENT_DRIVEN_ENABLED=0
export PGG2_ENGAGEMENT_POLL_STRIKE_ENABLED=0
export PGG2_ENGAGEMENT_MANAGE_LOOP_ENABLED=0

# ============================================================================
# PHASE 21 — NEW BOUNCE_BUY LANE (option B, brand new, structurally inverse)
# ============================================================================
# Fires when tape shows >=30% dump from local peak within 60s. Catches the
# bounce after panic flush. Target +5%, stop -7%, 90s timebox.
export PGG2_BOUNCE_BUY_ENABLED=1
export PGG2_BOUNCE_BUY_LANE_SOL=0.040
export PGG2_BOUNCE_BUY_MIN_DROP_PCT=0.30            # 30% dump min (Phase 21: 25% had too many shallow flushes)
export PGG2_BOUNCE_BUY_MAX_DROP_PCT=0.60            # 60% dump cap (above is rug)
export PGG2_BOUNCE_BUY_MAX_AGE_SINCE_PEAK_SEC=45.0  # tighter window (was 60s)
export PGG2_BOUNCE_BUY_ONLY_PRE_MIGRATION=1
export PGG2_BOUNCE_BUY_MIN_AGE_MS=60000
export PGG2_BOUNCE_BUY_MAX_AGE_MS=1800000
# Phase 22: Phase 21 dissection — bounce_buy losses had buyers=2, buy700<2, top>0.5
export PGG2_BOUNCE_BUY_MIN_RECENT_BUYERS=4          # was 2 (too few)
export PGG2_BOUNCE_BUY_MIN_BUY700_SOL=3.0           # NEW: real flow required
export PGG2_BOUNCE_BUY_MAX_TOP700=0.40              # NEW: no whale concentration
export PGG2_BOUNCE_BUY_MAX_TOP1500=0.40             # NEW: no whale concentration
export PGG2_BOUNCE_BUY_MAX_SELL_RATIO=0.30          # was 1.50 (tightened — sells must be defeated)
# Bounce exit math — profit_bank moved ABOVE slippage band (was banking inside band = loss)
export PGG2_BOUNCE_PROFIT_BANK_MULT=1.12            # was 1.05 — clears slippage + small win
export PGG2_BOUNCE_STOPLOSS_MULT=0.93
export PGG2_BOUNCE_TIMEBOX_SEC=90.0

# ============================================================================
# PHASE 22 — UNIVERSAL LOSS DNA BLOCK (pre-strike features filter)
# ============================================================================
# Phase 21 dissection (22 losses vs 12 wins): top1500>0.50 in 8 losses 0 wins,
# move250<0.92 = already dumping, sell_ratio>0.30 = toxic flow.
export PGG2_LOSS_DNA_BLOCK_ENABLED=1
export PGG2_LOSS_DNA_BLOCK_LOG=0                    # set to 1 for diagnostics
export PGG2_BLOCK_MAX_TOP1500=0.50
export PGG2_BLOCK_MIN_MOVE250=0.92
export PGG2_BLOCK_MAX_SELL_RATIO_1500=0.50
export PGG2_BLOCK_TOP700_SINGLE_BUYER=0.95

# ============================================================================
# PHASE 23 — DISABLE QUOTE_LOSS_CLAMP (DRY-LIVE ARTIFACT, not real loss)
# ============================================================================
# Phase 22 dissection: 5 of 8 losses were quote_loss_clamp firing within 2-9s
# of entry on positions with PRISTINE features (move=1.2-2.2, buy700=20-80,
# uniq700=8-26, top700=0.09-0.24). The clamp fires because the broker
# simulates "if I sold right now, what would I get?" — but on pump.fun bonding
# curves, our own buy increases vsol_lamports, so the immediate sell-back
# quote is mechanically -3-5% even with NO real price movement. This artifact
# closes good positions before they have time to move. Disable it; let
# moonshot_rider, scale_out, min_hold_panic, hard_break_grace handle exits.
export PGG2_LIVE_QUOTE_LOSS_CLAMP_ENABLED=0
# Also widen grace periods on remaining safeties so they don't fire too soon
export PGG2_HARD_BREAK_GRACE_SEC=12.0
export PGG2_MIN_HOLD_SEC=15.0
export PGG2_LIVE_PRICED_BREAKOUT_RUNNER_LOSS_GRACE_SEC=15.0

# Old engagement criteria kept for env-completeness (lane disabled above)
export PGG2_ENGAGEMENT_DRIVEN_ENABLED_OLD=0
# Looser engagement criteria — the strict 15/5 produced 0 trades in 3.5 min
export PGG2_ENGAGEMENT_MIN_VIEWERS=10
export PGG2_ENGAGEMENT_MIN_REPLIES=3
export PGG2_ENGAGEMENT_MIN_BUY1500=1.0
export PGG2_ENGAGEMENT_MIN_UNIQ1500=3
export PGG2_ENGAGEMENT_MAX_SELL_RATIO=0.20
# Require mint to be 30s-30min old (loosen — survivor zone is 30s-30min)
export PGG2_ENGAGEMENT_MIN_AGE_MS=30000
export PGG2_ENGAGEMENT_MAX_AGE_MS=1800000
# Recent buying (loose)
export PGG2_ENGAGEMENT_MAX_LAST_BUY_AGE_MS=3000
# Phase 20F: bigger stake — small wins matter, overhead becomes proportionally smaller
export PGG2_ENGAGEMENT_LANE_SOL=0.060

# ============================================================================
# PHASE 20F — TAKE-PROFIT-AND-RUN + NO-RED VOLUNTARY CLOSE
# ============================================================================
# Stack: partial-bank ladder + trailing peak lock + no-red voluntary close.
# Most positions tag a small peak then drift; we now bank that peak instead
# of letting it bleed. Voluntary close ONLY at green; red positions hold
# until they recover, hit catastrophic floor, or hit absolute timebox.
# Phase 20G: tier thresholds RAISED above the slippage band (1.8% per leg = 3.6% drag).
# Banking at peaks below +10% locked in losses on noise ticks. Now only fire on real pumps.
export PGG2_ENGAGEMENT_TIER1_PEAK=1.10          # +10% → bank 50% (covers slippage + small profit)
export PGG2_ENGAGEMENT_TIER1_FRAC=0.50
export PGG2_ENGAGEMENT_TIER2_PEAK=1.25          # +25% → bank 50% of remaining
export PGG2_ENGAGEMENT_TIER2_FRAC=0.50
export PGG2_ENGAGEMENT_TIER3_PEAK=1.50          # +50% → bank 50% of remaining
export PGG2_ENGAGEMENT_TIER3_FRAC=0.50
export PGG2_ENGAGEMENT_TRAIL_ARM_PEAK=1.10      # arm trail at +10% peak (no noise-tick triggers)
export PGG2_ENGAGEMENT_TRAIL_DROP=0.90          # close on 10% drop from peak (wide — ride real waves)
export PGG2_ENGAGEMENT_ABSOLUTE_TIMEBOX_SEC=600.0  # 10 min absolute max hold (more patience)
export PGG2_ENGAGEMENT_CATASTROPHIC_MULT=0.50   # only force-close below -50% (true rug)
# Old knobs kept for compat but disabled by new structure
export PGG2_ENGAGEMENT_PROFIT_BANK_MULT=99.0    # disabled — ladder + trail handle profit
export PGG2_ENGAGEMENT_STOPLOSS_MULT=0.0001     # disabled — catastrophic_mult is the floor
export PGG2_ENGAGEMENT_TIMEBOX_SEC=99999.0      # disabled — absolute_timebox is the cap
export PGG2_ENGAGEMENT_FAST_CUTOFF_SEC=0.0      # disabled — was wrong direction
export PGG2_ENGAGEMENT_FAST_CUTOFF_PEAK=0.0

# ============================================================================
# PHASE 20D — POLL-DRIVEN STRIKE LOOP (FRESH BIAS + higher frequency)
# ============================================================================
export PGG2_ENGAGEMENT_POLL_STRIKE_ENABLED=1
export PGG2_ENGAGEMENT_POLL_STRIKE_SEC=2.5             # 2x more frequent
export PGG2_ENGAGEMENT_POLL_STRIKE_MAX_PER_ITER=4      # 2x more strikes per cycle
export PGG2_ENGAGEMENT_POLL_STRIKE_WARMUP_SEC=8.0
# Phase 20D: only fire on PRE-MIGRATION mints (complete=0). The 3-trade
# signal showed mature post-migration tokens drift; fresh ones can pop.
export PGG2_ENGAGEMENT_POLL_FRESH_ONLY=1
export PGG2_ENGAGEMENT_POLL_FRESH_MAX_AGE_MS=3600000   # rank bonus for <1h
# Wider age window — engaged tokens range from seconds to hours
export PGG2_ENGAGEMENT_POLL_MIN_AGE_MS=30000           # 30s minimum (catch fresh)
export PGG2_ENGAGEMENT_POLL_MAX_AGE_MS=86400000        # 24h cap
export PGG2_ENGAGEMENT_POLL_MIN_VIEWERS=4              # very loose
export PGG2_ENGAGEMENT_POLL_MIN_REPLIES=2              # loose
export PGG2_ENGAGEMENT_POLL_DIAG_EVERY=4
# Re-eligibility: mint can re-fire 5 min after last attempt
export PGG2_ENGAGEMENT_POLL_SEEN_COOLDOWN_MS=300000
export PGG2_ENGAGEMENT_MANAGE_LOOP_ENABLED=1
export PGG2_ENGAGEMENT_MANAGE_SEC=2.0                  # 2x more frequent manage

# Source OG/attack configs (mostly disabled lanes above, but inherit base setup)
source <(sed '/^exec \.\/start_pgg2_attack_paper.sh/,$d' ./start_pgg2_direct_live_candidate.sh)
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump
source <(sed '/^exec \.\/venv\/bin\/python -u PGG2.py/,$d' ./start_pgg2_attack_paper.sh)
export PGG2_EXECUTION_MODE=quote
export PGG2_DRY_LIVE_MODE=1
export PIGGY_PAPER_TRADING=0
export PGG2_LIVE_BROKER=direct_pump

# Phase 21 re-assert (sourcing OG configs may have reset)
export PGG2_PRICED_SNAP_ENABLED=1
export PGG2_PRICED_BREAKOUT_ENABLED=1
export PGG2_CURVE_LAG_REVEAL_ENABLED=1
export PGG2_RAW_MOMENTUM_ENABLED=1
export PGG2_BIRTH_FANOUT_ENABLED=1
export PGG2_ENGAGEMENT_DRIVEN_ENABLED=0
export PGG2_ENGAGEMENT_POLL_STRIKE_ENABLED=0
export PGG2_ENGAGEMENT_MANAGE_LOOP_ENABLED=0
export PGG2_BOUNCE_BUY_ENABLED=1

# Phase 21: re-enable OG bot's exit logic (moonshot rider + scale-out)
# These are battle-tested for the OG fresh-mint lanes. Bounce_buy and the old
# engagement_driven blocks have their own exit branches in manage_position.
export PGG2_MOONSHOT_RIDE_ENABLED=1
export PGG2_SCALE_OUT_ENABLED=1
export PGG2_LATCH_SCALE_OUT_ENABLED=1
export PGG2_STALL_EXIT_ENABLED=1
export PGG2_MIN_HOLD_ENABLED=1
export PGG2_PEAK_LOCK_ENABLED=1

# Phase 20G: bigger stake to make small wins meaningful — also lift broker cap
export PIGGY_SCOUT_SOL=0.060
export PIGGY_MAX_POSITION_SOL=0.060
export PIGGY_PROBE_SOL=0.060
export PIGGY_MAX_OPEN=5
export PGG2_LIVE_MAX_TRADE_SOL=0.060
export PGG2_LIVE_MIN_TRADE_SOL=0.030

# Strip overfit blocks (won't matter for engagement lane but tidy)
export PGG2_PRICED_SNAP_BLOCK_TOP700=1.01
export PGG2_LIVE_BLOCK_HHI700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE700_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_MOVE1500_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_PER_BUYER_ABOVE=99999
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_BELOW=-1
export PGG2_PRICED_SNAP_BLOCK_AVG_BUY_7_TIGHT=-1
export PGG2_PRICED_SNAP_BLOCK_SCORE_BELOW=0

# Disable other safety mechanisms — irrelevant for engagement_driven (handled by tight exits)
export PGG2_ANTIBOT_FILTER_ENABLED=0
export PGG2_ANTI_MARTINGALE_ENABLED=0
export PIGGY_MOON_FAIL_SEC=999.0

# ============================================================================
# PHASE 24 — RE-ASSERT EXIT SAFETY DISABLES (after source blocks ran)
# ============================================================================
# Phase 23 dissection: REAL price_mult was 1.000 on every trade — positions
# closed in 2-5s based on SIMULATED sell-back quote, not actual price action.
# Root cause: source statements (lines ~177-186) re-enabled the safeties.
# Re-assert disables HERE so they survive sourcing.
export PGG2_LIVE_QUOTE_LOSS_CLAMP_ENABLED=0
# KEEP profit_bank enabled — that's how the real winner (+42% move, 15.9s hold) closed
export PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_ENABLED=1
# But require a meaningful gain before banking (avoid profit_bank firing on simulated noise)
export PGG2_LIVE_QUOTE_PROFIT_BANK_MIN_PNL_SOL=0.00250    # need +0.0025 SOL = ~+$0.50 net
export PGG2_LIVE_QUOTE_ANY_PROFIT_BANK_MIN_PNL_SOL=0.00250
export PGG2_HARD_BREAK_GRACE_ENABLED=1              # ENABLE grace (was getting disabled by line above)
export PGG2_HARD_BREAK_GRACE_SEC=15.0               # 15s grace before hard_break_grace can fire
export PGG2_MIN_HOLD_SEC=15.0                       # 15s min hold floor
export PGG2_LIVE_PRICED_BREAKOUT_RUNNER_LOSS_GRACE_SEC=20.0
# Stall exit was firing on buy_stall=True at 1.4s — too eager
export PGG2_STALL_EXIT_ENABLED=0
# Buy-stall lane-cuts happen in OG fresh-mint exit branches; widen them
export PIGGY_FULL_NO_POP_SEC=15.0
export PIGGY_FULL_NO_POP_MIN_BUYS=2
export PGG2_LIVE_BIRTH_FANOUT_NO_FOLLOW_SEC=10.0    # birth_fanout was killing at 2.8s on layered_no_follow

# ============================================================================
# PHASE 25 — UNIVERSAL REAL-PRICE EXIT GOVERNOR
# ============================================================================
# Replaces OG kill_* and broker simulated-quote exits with deterministic
# real-price-only exit logic. Bounds every loss at ~-$1.40, rides real
# winners with 6% peak trail. Ignores buy-flow death noise that was
# closing positions at -$2-6 during 2-30s holds.
export PGG2_PHASE25_UNIVERSAL_EXIT_ENABLED=1
export PGG2_PHASE25_STOP_MULT=0.95              # -5% real price stop
export PGG2_PHASE25_CATASTROPHIC_MULT=0.60      # -40% rug guard
export PGG2_PHASE25_TRAIL_ARM_PEAK=1.08         # arm trail once peak >= +8% real
export PGG2_PHASE25_TRAIL_DROP=0.94             # close on 6% drop from peak
export PGG2_PHASE25_MIN_HOLD_SEC=4.0            # 4s settle window before exits
export PGG2_PHASE25_TIMEBOX_SEC=60.0            # 60s timebox
export PGG2_PHASE25_TIMEBOX_MIN_MULT=1.02       # accept small loss at 60s if no pop

mkdir -p /root/piggy/logs /root/piggy/data
RUNID="${PGG2_RUN_PREFIX}_$(date -u +%Y%m%d_%H%M%S)"
echo "$RUNID" > /root/piggy/current_pgg2_runid.txt
export PIGGY_STATE_FILE="/root/piggy/data/${RUNID}_state.json"
export PIGGY_RAW_EVENTS_FILE="/root/piggy/data/${RUNID}_raw.jsonl"
export PIGGY_DECISIONS_FILE="/root/piggy/data/${RUNID}_decisions.jsonl"

echo "PHASE20-DRYLIVE RUN_ID=$RUNID mode=SURVIVOR_TRADER engagement_only tight_exits=10/-5"

exec ./venv/bin/python -u PGG2.py 2>&1 | tee "/root/piggy/logs/${RUNID}.log"
