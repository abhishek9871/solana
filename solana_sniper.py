"""
SOLANA MEMECOIN SNIPER v38.1 — ATTACK/REBOUND ENGINE

V17 PHILOSOPHY: $8 -> $100 needs ONE big win, not many small ones. So:
  - SMART WALLET CO-BUY as primary entry trigger: subscribes to 10 verified
    Kolscan top-tier wallets via Helius logsSubscribe. Fires entry signal when
    >=2 distinct smart wallets buy same token within 30 seconds.
  - EARLY CURVE ONLY: only act on co-buys when bonding curve <10% (leaves runway).
  - LET WINNERS RUN: TP1 at 2.0x (was 1.08x), TP2 at 5x, TP3 at 10x.
    Above 10x: trail SL at 50% of peak (catches 50-100x moonshots).
  - WIDER SL: -50% (was -25%) gives trades room to breathe.
  - PEAK CHECK: hard exit if peak < 1.5x at 5 min (caps deadweight).
  - SMALLER POSITIONS: 0.008 SOL/trade (was 0.012) — more attempts at moonshots.

V16 changes (vs v15) — solve both frequency AND win rate:
  - CURVE VELOCITY GATE: take a curve snapshot at eval time, then again 30s later. Require
    curve_progress to increase by >=2% in 30s. This is the strongest "momentum is alive"
    signal — a token frozen at 30% for 5 min is dying; one going 25%->32% in 30s is real.
  - EARLY DUMP EXIT: if peak hasn't exceeded 1.05x after 3 min in position, exit
    immediately (caps damage on tokens that never pump).
  - LOOSER ENTRY: 10-50% curve (was 20-45%), 60s wait (was 90s), 3 unique buyers/60s
    (was 5), 60% buy ratio (was 70%). Catches more candidates but velocity gate filters quality.
  - FASTER TP: 1.08x sells 50% (was 1.20x), 1.30x sells 30%, 1.80x sells 30%, then trail
    above. Locks micro-pumps before they dump.
  - 15-MIN HARD TIMEOUT (was 30min) — exit dead positions faster, recycle capital.

V15 changes (vs v14) — research-backed win rate uplift (target 50-65%):
  - AGGRESSIVE TP: 1.20x sells 50% (locks principal+10%), 1.50x sells 25%, 2.0x sells 25%,
    above 2.0x switches to pure trailing SL (-35% from peak) so moonshots can run.
  - 30-MIN HARD TIME STOP: tokens that don't move in 30min mostly bleed (median graduation
    time is 4.4 min per arxiv data; 30min is generous).
  - NARROWER ENTRY: 20-45% bonding curve progress (was 2-50%); buyer ratio gate
    (>=70% buys in last transactions); unique-buyer velocity gate (>=5 in last 60s).
  - DEV WALLET REPUTATION: SQLite cache of creator's prior tokens. Hard reject if
    >=3 prior dead-in-1-hour rugs. New creators allowed (70%+ of graduators).
  - Per arxiv research (655K tokens): graduation rate 0.63%, dump rate 92%; we filter
    aggressively to enter only positive-EV setups.

V14 changes (vs v13.1) — KILLS Jupiter rate-limit problem at the architectural level:
  - Position management now reads bonding curve DIRECTLY from chain via Helius RPC
    (free, unlimited on free tier). Zero Jupiter calls per poll.
  - Entry price comes from on-chain virtual_sol/virtual_token (marginal curve price)
  - Jupiter is only called: (1) once at buy time, (2) once at each sell, (3) only when
    a token graduates off pump.fun (the curve no longer prices it).
  - Polling back to 2s (Helius easily handles 3 positions x 30 polls/min = 1.5 RPC/sec)
  - Auto-detects graduation: when curve "complete" flag flips, switches to Jupiter pricing
  - Eliminates all rate-limit-induced false closes

V13.1 changes (vs v13):
  - 5s polling — keeps us under lite-api.jup.ag's free-tier rate limit (now obsolete)
  - Global rate-limit cooldown on 429 — all callers back off 60s when throttled
  - Outage-mode sleep extended (8s -> 15s) so the rate limit can recover
  - Health-probe cache extended (15s -> 30s) — fewer health checks during normal operation

V13 changes vs v12:
  - Realized-SOL tracking on every sell -> proper session PnL accounting (TP rungs were missing!)
  - Jupiter health probe distinguishes API outages from real token death (no more false TOKEN DEAD)
  - HARD TIMEOUT now actually attempts a final sell (was just deleting from memory)
  - TOKEN DEAD path attempts aggressive live-mode sell with max slippage
  - Quote retry on transient errors (timeout, 429 rate-limit)
  - Session reporter logs running pnl every 60s
  - More resilient WebSocket (longer ping_interval/timeout)

Monitors pump.fun for new token launches with strong momentum, applies multi-layer
safety checks, executes buys via Jupiter aggregator, and sells with a take-profit
ladder. Built for the post-2026-04-28 pump.fun program structure.

ARCHITECTURE:
  1. DETECTION: monitors Solana logs for pump.fun program activity (buy txs)
     Identifies tokens with >5 unique buyers and rising bonding curve in last 60s
  2. SAFETY GATES: mint authority renounced + freeze authority null + top10 holders
     concentration < 30% + age < 30min (catches early winners, avoids established rugs)
  3. EXECUTION: Jupiter v6 swap API for buy + sell (reliable, MEV-protected via Jito)
  4. TP LADDER: 1.25x -> sell 25%, 1.5x -> sell 50%, 2x -> sell rest
  5. STOP-LOSS: -40% from entry
  6. PAPER MODE: default ON, set PAPER_TRADING=False for live trading

SETUP:
  1. Create Phantom or Solflare wallet
  2. Export private key (Phantom: Settings -> Security -> Export Private Key)
  3. Set environment variable:
       set SOLANA_PRIVATE_KEY=<your_base58_private_key>
       set SOLANA_RPC_URL=https://api.mainnet-beta.solana.com  (or Helius/QuickNode)
  4. Fund wallet with SOL on Solana mainnet
  5. py solana_sniper.py

SAFETY:
  - PAPER_TRADING flag must be explicitly set to False for live trading
  - All transaction signing requires private key in memory (never logged)
  - Each snipe has a hard MAX_SOL cap
  - Total session loss circuit breaker
  - Sell simulation before buy (rejects honeypots)
"""

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import base58
import requests
import websockets
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed, Processed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.signature import Signature

# V41.12: simple .env loader so SOLANATRACKER_API_KEY etc. work without external deps.
def _load_dotenv() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

# === CONFIG ===
PAPER_TRADING = True  # set False for live trading

# Wallet & RPC
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb")
SOLANA_WS_URL = SOLANA_RPC_URL.replace("https://", "wss://").replace("http://", "ws://")
PRIVATE_KEY_B58 = os.environ.get("SOLANA_PRIVATE_KEY", "")

# V41.14: Solana Tracker RPC with shredSubscribe (50-150ms latency vs Helius ~500-1500ms)
ST_RPC_KEY = os.environ.get("SOLANATRACKER_RPC_KEY", "")
ST_RPC_HTTP = os.environ.get("SOLANATRACKER_RPC_HTTP", "")
ST_RPC_WS = os.environ.get("SOLANATRACKER_RPC_WS", "")
ST_RPC_ENABLED = bool(ST_RPC_KEY and ST_RPC_WS)

# Pump.fun program (post-2026-04-28 update)
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
# V41.7: bonk.fun (Raydium LaunchLab) program. Bonk graduates to Raydium V4 AMM
# or Raydium CPMM via 'migrate_to_amm' / 'migrate_to_cpswap' instructions.
# Source: https://docs.bitquery.io/docs/blockchain/Solana/letsbonk-api/
BONK_PROGRAM = Pubkey.from_string("LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")
BONK_ENABLED = os.environ.get("BONK_ENABLED", "1") == "1"

# V41.12: Solana Tracker pre-filters bundled launches via their multi-heuristic engine.
# Free tier: 10k req/month, 1 req/sec. Provides riskScore + bundlers/snipers/insiders/dev/top10.
# Source: https://docs.solanatracker.io/data-api
SOLANATRACKER_API_KEY = os.environ.get("SOLANATRACKER_API_KEY", "")
SOLANATRACKER_ENABLED = bool(SOLANATRACKER_API_KEY) and os.environ.get("SOLANATRACKER_ENABLED", "1") == "1"
SOLANATRACKER_BASE = "https://data.solanatracker.io"
# V41.13: 2-min poll per user request. Burns ~21,600 calls/month — exhausts 10k plan
# by ~day 14. User explicitly accepted this trade-off for faster candidate discovery.
SOLANATRACKER_POLL_SEC = int(os.environ.get("SOLANATRACKER_POLL_SEC", "120"))
# Filter thresholds — tokens passing ALL of these enter our pipeline.
# V41.13f: tightened filters to match HfpkGDz1 quality (the only ST entry that won).
# HfpkGDz1: score=3, 0% bundlers, 0% dev, 0.3% top10, curve=34.9%.
# Recent losers (FCCbnhmW, AoUKsvRu, 7uXMPK4K) had score 4-5, bundlers 0.4-4.4%, top10 0.5-9.8%.
# Pulling thresholds down to filter only HfpkGDz1-tier candidates.
ST_MAX_RISK_SCORE = int(os.environ.get("ST_MAX_RISK_SCORE", "3"))         # was 5 — only top-quality
ST_MAX_BUNDLER_PCT = float(os.environ.get("ST_MAX_BUNDLER_PCT", "2.0"))   # was 15 — clean only
ST_MAX_DEV_PCT = float(os.environ.get("ST_MAX_DEV_PCT", "2.0"))           # was 8 — no whale dev
ST_MAX_TOP10_PCT = float(os.environ.get("ST_MAX_TOP10_PCT", "10.0"))      # was 40 — distributed
ST_MIN_CURVE_PCT = float(os.environ.get("ST_MIN_CURVE_PCT", "15.0"))      # V41.13f: HfpkGDz1 was 34.9% — going 15+ to capture mid-curve like it
ST_MAX_CURVE_PCT = float(os.environ.get("ST_MAX_CURVE_PCT", "80.0"))      # entry too late = no runway
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # for jupiter health probe
WSOL_DECIMALS = 9

# Jupiter — try lite endpoint first (often more reliable DNS)
JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP = "https://lite-api.jup.ag/swap/v1/swap"

# V41.14: Raptor (Solana Tracker DEX aggregator). Free hosted endpoint, no auth needed.
# Routes across 20+ DEXes including Pump.fun, Raydium, Meteora, Orca, etc.
# Includes Yellowstone Jet TPU for live transaction sending (faster than Jupiter).
RAPTOR_BASE = os.environ.get("RAPTOR_BASE", "https://raptor-beta.solanatracker.io")
RAPTOR_ENABLED = os.environ.get("RAPTOR_ENABLED", "1") == "1"

# Trading params
# V38.2: configurable sizing. Normal entries get the full shot; weak/late/dump
# rebound entries use a smaller scalp size so we can take more opportunities
# without letting one bad bounce wreck the paper run.
SNIPE_AMOUNT_SOL = float(os.environ.get("SNIPE_AMOUNT_SOL", "0.025"))
SCALP_AMOUNT_SOL = float(os.environ.get("SCALP_AMOUNT_SOL", str(min(SNIPE_AMOUNT_SOL, 0.012))))
MAX_SLIPPAGE_BPS = 2000
PRIORITY_FEE_LAMPORTS = 500_000
# V38.1: earlier locks + moonbag. Fractions are of REMAINING position.
# Normal mode banks early but keeps a runner; scalp/rebound mode turns small
# bounces into green exits instead of waiting for oversized pumps.
TP_LADDER = [
    (1.08, 0.25),                     # +8% -> sell 25% of remaining
    (1.25, 0.30),                     # +25% -> sell 30% of remaining
    (2.00, 0.35),                     # 2x -> sell 35% of remaining
    (5.00, 0.50),                     # 5x -> sell 50% of remaining, trail moonbag
]
TP_RUNNER_MODE = True                 # keep final remainder alive for moonshots
SCALP_TP_LADDER = [
    (1.03, 0.65),                     # dump/late scalp: +3% lock 65% fast
    (1.08, 1.00),                     # +8% sell all remaining
]
SCALP_SL_PCT = -0.03                  # -3% (tighter than -5%) — minimize dump damage
SCALP_TIMEOUT_SEC = 180               # 3 min hard timeout
SL_PCT = -0.08                        # V20.3: -8% (was -15%) — minimize flash dump damage
FLASH_EXIT_THRESHOLD = 0.99           # V20.2: -1% on first poll = exit immediately (was 0.97)
COBUY_VERIFY_DELAY_SEC = 2            # V38.2: co-buy verification must be fast; slow gates miss pumps
COBUY_MIN_GROWTH_AFTER_DELAY = 0.01   # V20.2: require +1% curve growth in verify window
PREENTRY_BUYSELL_LOOKBACK = 12        # V20.2: check last N pump.fun txs for buy/sell ratio
POSITION_TIMEOUT_MIN = 12             # V38.2: recycle dead capital faster
RUNNER_TIMEOUT_MIN = 60               # but keep proven runners alive longer

# Safety thresholds
MAX_TOP10_CONCENTRATION = 0.995
MAX_TOKEN_AGE_MIN = 30
MIN_LIQUIDITY_SOL = 5
# V17.2: FAST + FREQUENT — wide entry, exit strategy does the work
MIN_CURVE_PROGRESS = 0.01             # V38.2: earlier ignition shots, guarded by momentum/rebound checks
MAX_CURVE_PROGRESS = 0.58             # V38.2: allow late breakout scalps up to 58% in cold path
MIN_UNIQUE_BUYERS_60S = 2             # 2 buyers (was 3) — looser
MIN_BUY_RATIO_RECENT = 0.50           # 50% (was 60%) — looser
MIN_CURVE_GROWTH_30S = 0.0            # V23: DISABLED - velocity gate adds 15s latency we can't afford
EVAL_WAIT_SEC = 3                     # V38.2: faster evaluation; follow-up gates decide quality
EARLY_DUMP_PEAK_THRESHOLD = 1.02      # V31: 1.02x in 30s — if no movement, bail near-flat
EARLY_DUMP_TIMEOUT_SEC = 30           # V31: 30s (was 2min) — exit no-pump tokens fast at break-even

# V38: more trades without blind trades. Hard rejects only for structural danger;
# weak/dump/no-momentum setups become SCALP entries when rebound confirms.
STARTING_CURVE_TOKENS = 793_100_000_000_000
COBUY_HARD_NO_RUNWAY = 0.90           # >90% curve = almost no runway, do not chase
LATE_SCALP_CURVE_START = 0.50         # 50-90% can be scalp-only if breakout confirms
# V41.17b: dump-rebound DISABLED. Contrarian (buy-the-dump → hope for bounce) plays
# fight the dominant momentum structure of pump.fun memecoins, where dumps mostly keep
# dumping. Also pollutes the V41.17 copy_fast PnL signal — when we evaluate copy_fast's
# session win rate, dump-rebound trades dilute the data. Cleanest path: one strategy
# at a time. Both wait_for_dump_rebound() implementations already early-exit on this
# flag (lines 826 and 3179), so disabling here cleanly bypasses every call site.
DUMP_REBOUND_ENABLED = os.environ.get("DUMP_REBOUND_ENABLED", "0") == "1"
DUMP_REBOUND_WAIT_SEC = 45            # V38.2: watch dumps longer for exhaustion bounce instead of instant skip
DUMP_REBOUND_MIN_BOUNCE = 0.005       # +0.5% price uptick per tick = rebound confirmation
MIN_MOMENTUM_GROWTH_3S = 0.003        # was 1.5%; too strict. +0.3% catches ignition
FORCE_PAPER_CURVE_ENTRY = True        # paper mode may simulate pre-Jupiter pump.fun buys

# V39: asymmetric sizing. The old bot chose between SKIP and full 0.025 SOL.
# That is the wrong game. Weak/noisy/dump-rebound signals become tiny scouts;
# strong signals get core size; proven winners can scale in. This increases
# trade participation without donating full size to every dead mint.
MICRO_SCOUT_AMOUNT_SOL = float(os.environ.get("MICRO_SCOUT_AMOUNT_SOL", "0.003"))
SCOUT_AMOUNT_SOL = float(os.environ.get("SCOUT_AMOUNT_SOL", "0.006"))
CORE_AMOUNT_SOL = float(os.environ.get("CORE_AMOUNT_SOL", str(SNIPE_AMOUNT_SOL)))
SCALE_IN_AMOUNT_SOL = float(os.environ.get("SCALE_IN_AMOUNT_SOL", "0.010"))
MAX_POSITION_AMOUNT_SOL = float(os.environ.get("MAX_POSITION_AMOUNT_SOL", "0.035"))
FULL_SIZE_SCORE = int(os.environ.get("FULL_SIZE_SCORE", "6"))
PAPER_SCOUT_EVERY_VALID_MINT = os.environ.get("PAPER_SCOUT_EVERY_VALID_MINT", "1") == "1"
SCALE_IN_ENABLED = os.environ.get("SCALE_IN_ENABLED", "0") == "1"  # V41.2: disabled — compounds losses on bad reads
# Compatibility constants for V38 dump-bounce + cold momentum paths.
# These names are used below; without them, the bot would crash when those paths fire.
DUMP_BOUNCE_WATCH_SEC = 45
DUMP_BOUNCE_MAX_CURVE_PROGRESS = 0.72
DUMP_BOUNCE_MIN_GROWTH = 0.005
COLD_MOMENTUM_MIN_GROWTH_3S = MIN_MOMENTUM_GROWTH_3S
CASHBACK_MOMENTUM_MIN_GROWTH_3S = 0.0015

# Circuit breakers — V41.5/8 tuned for $17.64 bankroll (0.21 SOL).
# V41.8: positions are 0.05 SOL = $4.20 each. SL = -7% = -$0.30/trade.
# Halt at -$2.50 = -0.03 SOL (~14% bankroll drawdown) = ~8 SL hits in a row.
MAX_SESSION_LOSS_SOL = float(os.environ.get("MAX_SESSION_LOSS_SOL", "0.10"))   # V41.13n: 0.03→0.10 — small positions need more sample, halt at -$8.40 instead of -$2.50
MAX_CONSEC_LOSSES = int(os.environ.get("MAX_CONSEC_LOSSES", "5"))               # halt at 5 straight
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "10"))
# V41.5: Daily trade cap. Past 25 trades the bot is overtrading a regime that hasn't worked.
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "25"))
# V41.5: Pause after losing streak — variance pressure compounds, give the regime time to shift.
LOSS_STREAK_PAUSE_THRESHOLD = int(os.environ.get("LOSS_STREAK_PAUSE_THRESHOLD", "3"))
LOSS_STREAK_PAUSE_SEC = int(os.environ.get("LOSS_STREAK_PAUSE_SEC", "90"))

# === STATE ===
@dataclass
class Position:
    mint: str
    entry_price: float
    entry_amount_sol: float
    token_amount: float
    open_time: float
    peak_price: float = 1.0            # multiplier peak; start at 1x for sane no-pump accounting
    rung_hit: int = 0
    remaining_pct: float = 1.0
    realized_sol: float = 0.0          # cumulative SOL received from sells
    last_price: float = 0.0            # last successfully observed price
    bc_pda: Optional[Pubkey] = None    # cached bonding curve PDA
    graduated: bool = False            # true once token migrates off pump.fun curve
    late_scalp: bool = False           # V21/V38: scalp mode (late-stage or dump-rebound)
    strategy: str = "momentum"          # momentum | cobuy | late_breakout | dump_rebound | weak_scalp | micro_probe
    entry_progress: float = 0.0        # bonding-curve progress at entry
    quality_score: int = 0             # diagnostic only; not used for guarantees
    entry_size_sol: float = 0.0        # actual size chosen by V39 sizing logic
    adds_done: int = 0                 # scale-ins already executed
    launchpad: str = "pump"            # V41.7: "pump" or "bonk" — affects fee math + TP threshold
    # V41.17 Fix #9: copy_fast entries stamp the original shred time for the 8s no-pump time-stop
    signal_time_ms: int = 0


positions: dict[str, Position] = {}
session_pnl_sol = 0.0
session_wins = 0
session_losses = 0
consec_losses = 0
# V41.5: trade-counting and pause state
daily_trade_count = 0                  # count of entries opened this session
daily_count_reset_ts = time.time()     # when the daily counter last reset
streak_pause_until = 0.0               # epoch ts; if now < this, no new entries allowed
last_seen_mints: set[str] = set()
jupiter_last_ok = 0.0                  # last successful jupiter API call (any token)
jupiter_blocked_until = 0.0            # rate-limited until this timestamp


# V41.5: trades whose net pnl is below this threshold count as losses, not wins.
# Paper-mode break-evens that round to 0 are NOT wins — in live mode they'd be small fee+slippage losses.
MIN_REAL_WIN_SOL = float(os.environ.get("MIN_REAL_WIN_SOL", "0.0001"))


def _entry_circuit_breakers_open() -> tuple[bool, str]:
    """V41.5: centralised entry gate. Returns (blocked, reason).
    Checks session loss, consec losses, daily trade cap, streak pause.
    """
    global daily_trade_count, daily_count_reset_ts
    now = time.time()
    # Daily window resets every 24h. If the last reset was over 24h ago, zero the counter.
    if now - daily_count_reset_ts > 86400:
        daily_trade_count = 0
        daily_count_reset_ts = now
    if session_pnl_sol <= -MAX_SESSION_LOSS_SOL:
        return True, f"session loss limit hit ({session_pnl_sol:+.4f} SOL)"
    if consec_losses >= MAX_CONSEC_LOSSES:
        return True, f"consec_loss limit hit ({consec_losses})"
    if daily_trade_count >= MAX_TRADES_PER_DAY:
        return True, f"daily trade cap hit ({daily_trade_count}/{MAX_TRADES_PER_DAY})"
    if now < streak_pause_until:
        return True, f"streak pause active ({int(streak_pause_until - now)}s remaining)"
    return False, ""


def _record_trade_close(pnl: float) -> None:
    """V41.5: centralised close accounting.
    - Honest W/L classification (paper break-even is NOT a win)
    - Triggers streak pause when consec_losses >= LOSS_STREAK_PAUSE_THRESHOLD
    """
    global session_pnl_sol, session_wins, session_losses, consec_losses, streak_pause_until
    session_pnl_sol += pnl
    if pnl > MIN_REAL_WIN_SOL:
        session_wins += 1
        consec_losses = 0
    else:
        session_losses += 1
        consec_losses += 1
        if consec_losses >= LOSS_STREAK_PAUSE_THRESHOLD and streak_pause_until < time.time():
            streak_pause_until = time.time() + LOSS_STREAK_PAUSE_SEC
            log(f"  STREAK PAUSE: {consec_losses} consec losses — pausing new entries for {LOSS_STREAK_PAUSE_SEC}s")


def _record_entry_opened() -> None:
    """V41.5: increment daily trade counter when a position is opened."""
    global daily_trade_count
    daily_trade_count += 1

# V26: 38 ALL-VALIDATED smart wallets restored — every one has proven PnL > 0,
# win rate >= 50%, hold >= 30s. Volume matters; the 50% curve cap (V24) handles
# the late-stage exit-liquidity risk regardless of wallet quality.
SMART_WALLETS = {
    # === Tier 1: heavyweights (PnL > 5 SOL) ===
    "DHbx6cSyVBSx2qZfNqYf1FG9UHQzfAkRoCsC9DnRCmja": "DHbx_pnl14_wr89",
    "4tH7jjPRZmXgFJvUTLK13YkRg1kjWQSUWJa1bqRVjJsE": "4tH7_pnl13_wr64",
    "5EPN2bhCSGc4WAcHx3x79KmT8vNVrASkKvmhzCkSempu": "5EPN_pnl8_wr88",
    "F5Hrs3fTxA6cPsdYa1r2zazymsetbFpXpzEuQWXPNusu": "F5Hr_pnl8_wr71",
    "DzpVESbfz8FLBRt7yjFNE1fZnbpSyxaBWTd8Bhz8qeaW": "DzpV_pnl5_wr57",
    "Ack1QVrWLNTT1BqoZXye4FoTxHWTnx3HCBwyn3Mc5kV3": "Ack1_pnl5_wr80",
    # === Tier 2: solid (PnL 1-5 SOL) ===
    "HNKvqBj7majHp8gbQgSwpxdEk1vPWrcdghs5EMYk2ZEw": "HNKv_pnl5_wr75",
    "7r14pGkk9x45i7z5L2smaeCpACQVenNXEnV9FL7cov2j": "7r14_pnl5_wr71",
    "DZurvQ7Lwv8xz983y8Xx7TfJntMmF41yscR7SoDU4aNt": "DZur_pnl4_wr57",
    "DD2qxJP24AEemDevYYLw4y7z6yPWCtG3oC8cGJ5x4pif": "DD2q_pnl3_wr56",
    "CsDL8yHruRnxRGdnwwTrsrsX7DWsuEPFBk2VP6BXK6AY": "CsDL_pnl3_wr60",
    "8gCJYyKWnKGoY6Di3iF1iUErfXHpQyiaGLB1TyRYbhcF": "8gCJ_pnl3_wr64",
    "3Ksc9EE8wR8ZzkkuKPywNAkeeCh5DhKByBxQPGzgQ3o5": "3Ksc_pnl2_wr67",
    "4mLmXQgkEThAWcKpPrXPSUG6QvoCexpBERy7DMXeNAh6": "4mLm_pnl2_wr100",
    "GKDiL2NHBzk82rQFHpGdiN6y7tu7WGGFTHGdgDyqzLCk": "GKDi_pnl2_wr73",
    "8NRSnFtTDoFa31EvGEEb11n1rYBEzpUH9aNes6GH6Lab": "8NRS_pnl2_wr58",
    "6ccknqfnao4ASbGsGio921PREtMGYFHXve4qjsvn1oHc": "6cck_pnl2_wr60",
    "Csg3fe7CRD65XNknyQN22gpc1gQeukGgckARf3LoSviu": "Csg3_pnl1_wr75",
    "4jiyvWrE6qk1wS5pSxnbvS6hjzBH6ZE25ZX6si1RC4PW": "4jiy_pnl1_wr55",
    "5fjZatNnqmWixUtycekodpsaNHCHVe48oBzaLbV4kXRL": "5fjZ_pnl1_wr55",
    "EsWPskUe3o5t8Z1cLZgHCnQanUUakKApsX7JJYhr1FyW": "EsWP_pnl1_wr67",
    "6EbsHdF6UTckvhspsx32Zt6LB4R8S74AoiL1k71EC85E": "6Ebs_pnl1_wr100",
    "Aywk7AN98iTnwaK9ZBafYqGL14xM65zJF68sVC8UkAiY": "Aywk_pnl1_wr75",
    "8yG9XobFX9VpNVDYzAwoR8MrmCE5bC8ThoaKLJ4Bev7j": "8yG9_pnl1_wr100",
    # === Tier 3: small but consistent (PnL < 1 SOL) ===
    "BesRBxseW4jZdUt4w7Nvici3mqH4jsfC7vu62B2WGzgd": "BesR_pnl0_wr58",
    # DEMOTED: 0/3 cobuy entries triggered by Xwu6 won this session — pnl=0/wr=57 was baseline noise.
    # "Xwu6DKqGo4wKPBAPvNYHsjMTV2JxqmW6ubuvhQYKu6E": "Xwu6_pnl0_wr57",
    "9Q4TpsUMco8hU6xY3LAseURi3mmjMKVWDwuvEqvZuEQk": "9Q4T_pnl0_wr71",
    "vMpqXnizwETfNePsJd82hdS9qAK2MU5d92GevFpE4r3":  "vMpq_pnl0_wr62",
    "BHbpFZdLwiWZfRY8b8sFo51bZpkrNDPXCvL8ZRquTpbf": "BHbp_pnl0_wr60",
    "BCdJLWTX26JTcWxThPPxohvaqup3rwmSSDDGTBpya6Le": "BCdJ_pnl0_wr78",
    "EQYRwojm39HCPGeSnZL1EzLYEPK5SGa5A7c6UKXPLxiQ": "EQYR_pnl0_wr86",
    "EuyDTS27zwGZiRai8LqRYBiALU9bsqws4ZuvrgvyMzuF": "EuyD_pnl0_wr80",
    "CoHYfUDxSk32F9fZqsdJTaFf8yGU5B4i5GwrfCCm3rHn": "CoHY_pnl0_wr58",
    "6t6E2wJnKSwgeJM5vWoPhq6g1uD5rMvuF4dnituFgEjp": "6t6E_pnl0_wr55",
    "Gvu4wQZuZcDC8yonafDsKqaJCWLD2e45FUVCTPQ2kXU9": "Gvu4_pnl0_wr55",
    "7x73hzxXYU4bJeesYzm4FJNKNc1jSH2urR7mBRdYM3bD": "7x73_pnl0_wr55",
    "4oqC5j6s3bHrZVG6PrsAFSHuKaqaQJpP1qeqCU9M9Q1b": "4oqC_pnl0_wr67",
    "Aop91KkNRco3eV99pyXCDxk23hfAkAD8PsUsTEumS4Uf": "Aop9_pnl0_wr100",
}
# token_mint -> [(timestamp, wallet_str), ...] - co-buy detection
smart_wallet_buys: dict[str, list[tuple[float, str]]] = {}
COBUY_WINDOW_SEC = 60
COBUY_THRESHOLD = 1                   # V20: all 38 wallets validated profitable — single signal trustable
# Track tokens we've already fired co-buy entry for (don't double-snipe)
cobuy_fired: set[str] = set()
# Tokens currently being watched for a dump -> reversal flip. This turns
# some old "skip because dumping" cases into a timed bounce attempt instead
# of giving up instantly.
dump_bounce_active: set[str] = set()


def log(msg: str):
    safe = str(msg).encode("ascii", "replace").decode("ascii")
    print(f"[{time.strftime('%H:%M:%S')}] {safe}", flush=True)


def load_keypair() -> Optional[Keypair]:
    if not PRIVATE_KEY_B58:
        if PAPER_TRADING:
            log("PAPER MODE: no private key needed")
            return None
        log("ERROR: SOLANA_PRIVATE_KEY env var not set")
        return None
    try:
        secret = base58.b58decode(PRIVATE_KEY_B58)
        kp = Keypair.from_bytes(secret)
        log(f"Wallet: {kp.pubkey()}")
        return kp
    except Exception as e:
        log(f"ERROR loading keypair: {e}")
        return None


# === SAFETY CHECKS ===
def check_mint_safety(client: Client, mint: str) -> tuple[bool, str]:
    """Returns (safe, reason). Parses account data directly (avoids index lag from
    get_token_supply / get_token_largest_accounts which require Helius indexing)."""
    try:
        mint_pubkey = Pubkey.from_string(mint)
        info = client.get_account_info(mint_pubkey, commitment=Confirmed)
        if not info.value:
            return False, "mint account not found"
        # SPL Token Mint account layout:
        # bytes 0-4: mint_authority option (4 = ?? tag, but Solana uses 4 bytes for option Some/None)
        # bytes 4-36: mint_authority pubkey (only valid if option == 1)
        # bytes 36-44: supply (u64 LE)
        # byte 44: decimals
        # byte 45: is_initialized
        # bytes 46-50: freeze_authority option
        # bytes 50-82: freeze_authority pubkey
        data = bytes(info.value.data)
        if len(data) < 82:
            return False, f"invalid mint data len={len(data)}"
        owner = str(info.value.owner)
        # SPL Token Program: TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA
        # Token-2022:        TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb
        spl_token = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        token_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
        if owner not in (spl_token, token_2022):
            return False, f"not a token mint (owner={owner[:16]})"
        mint_auth_option = int.from_bytes(data[0:4], "little")
        if mint_auth_option == 1:
            return False, "mint authority NOT renounced (rug risk)"
        freeze_auth_option = int.from_bytes(data[46:50], "little")
        if freeze_auth_option == 1:
            return False, "freeze authority NOT null (rug risk)"
        is_init = data[45]
        if is_init != 1:
            return False, "mint not initialized"
        # Optional: try top holders concentration (may fail if not indexed; warn but allow)
        try:
            largest = client.get_token_largest_accounts(mint_pubkey, commitment=Confirmed)
            supply_resp = client.get_token_supply(mint_pubkey, commitment=Confirmed)
            if largest.value and supply_resp.value:
                total_supply = float(supply_resp.value.amount)
                if total_supply > 0:
                    top10 = sum(float(acc.amount.amount) for acc in largest.value[:10])
                    concentration = top10 / total_supply
                    if concentration > MAX_TOP10_CONCENTRATION:
                        return False, f"top10 hold {concentration*100:.0f}%"
                    return True, f"safe (top10={concentration*100:.0f}%)"
        except Exception:
            pass
        return True, "safe (basic checks passed, top10 unknown)"
    except Exception as e:
        return False, f"safety err: {e}"


# === ON-CHAIN PRICE (NO JUPITER NEEDED) ===
def get_curve_state(client: Client, bc_pda: Pubkey, fast: bool = False) -> Optional[dict]:
    """Read pump.fun bonding curve account directly from chain. No external API.
    Returns {price, virtual_sol, virtual_token, real_token, complete, cashback} or None.
    Pricing: marginal price = virtual_sol_reserves / virtual_token_reserves
    (lamports per smallest token unit — same scale as our entry_price).
    fast=True uses Processed commitment for sub-second visibility (V29).
    V37: byte 82 = cashback_enabled flag (post-Feb 2026 program upgrade)."""
    try:
        from solana.rpc.commitment import Processed
        info = client.get_account_info(bc_pda, commitment=(Processed if fast else Confirmed))
        if not info.value:
            return None
        data = bytes(info.value.data)
        if len(data) < 49:
            return None
        virtual_token = int.from_bytes(data[8:16], "little")
        virtual_sol = int.from_bytes(data[16:24], "little")
        real_token = int.from_bytes(data[24:32], "little")
        complete = data[48] != 0
        # V37: cashback flag at byte 82 (post-cashback-upgrade tokens only).
        # Cashback Coins return 100% of the 0.3% trading fees to traders proportional to volume —
        # creates positive expectancy boost on every trade, even losing ones.
        cashback = bool(data[82] != 0) if len(data) > 82 else False
        if virtual_token == 0:
            return {"price": None, "virtual_sol": virtual_sol, "virtual_token": 0,
                    "real_token": real_token, "complete": complete, "cashback": cashback}
        return {
            "price": virtual_sol / virtual_token,
            "virtual_sol": virtual_sol,
            "virtual_token": virtual_token,
            "real_token": real_token,
            "complete": complete,
            "cashback": cashback,
        }
    except Exception:
        return None


def derive_bc_pda(mint_pk: Pubkey) -> Pubkey:
    bc_pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM)
    return bc_pda


# V41.7: bundle-detection thresholds. Per Trench.bot guide: 2 same-slot wallets is noise
# (BullX/copy-trader artifacts), 3+ is a bundle, 5+ is a hard rug-bait signal.
# V41.8: widened slot window from +1 to +5 to catch stagger-bundles that intentionally
# space buys to evade detection. Also added early-sell-pressure check.
# Source: https://docs.trench.bot/bundle-tools/bundle-scanner-guide
BUNDLE_DETECTION_ENABLED = os.environ.get("BUNDLE_DETECTION_ENABLED", "1") == "1"
BUNDLE_SAME_SLOT_THRESHOLD = int(os.environ.get("BUNDLE_SAME_SLOT_THRESHOLD", "3"))
BUNDLE_SLOT_WINDOW = int(os.environ.get("BUNDLE_SLOT_WINDOW", "5"))   # V41.8: was +1
BUNDLE_LOOKBACK_SIGS = int(os.environ.get("BUNDLE_LOOKBACK_SIGS", "30"))   # V41.8: was 25
BUNDLE_FUNDING_LOOKBACK_SIGS = int(os.environ.get("BUNDLE_FUNDING_LOOKBACK_SIGS", "5"))
BUNDLE_FUNDING_THRESHOLD = int(os.environ.get("BUNDLE_FUNDING_THRESHOLD", "3"))
# V41.8: early-sell-pressure check — if dev is already dumping in first N sigs, skip.
EARLY_SELL_PRESSURE_LOOKBACK = int(os.environ.get("EARLY_SELL_PRESSURE_LOOKBACK", "10"))


def is_bundled_launch(client: Client, mint_pk: Pubkey, bc_pda: Pubkey) -> tuple[bool, str]:
    """V41.7: free-tier bundle detection.

    Returns (is_bundled, reason_string).

    Phase 1 — same-slot cluster: if N+ distinct buyer wallets fired in the SAME slot
    as the curve's earliest activity, it's an atomic bundle (Jito-style multi-wallet
    snipe at create_token time).

    Phase 2 — funding source correlation (only triggered when same-slot count is 2-3,
    just below the hard threshold): trace each suspicious buyer's most recent SOL
    inflow; if 3+ buyers share a parent funder within the lookback window, BUNDLE.

    Both phases use only free Helius RPC (getSignaturesForAddress + getTransaction
    at confirmed commitment). Cost: 1 RPC for Phase 1, +3-5 RPCs if Phase 2 triggers.
    """
    if not BUNDLE_DETECTION_ENABLED:
        return False, ""
    try:
        # Phase 1: pull recent sigs on the bc_pda
        sigs_resp = client.get_signatures_for_address(
            bc_pda, limit=BUNDLE_LOOKBACK_SIGS, commitment=Confirmed
        )
        sigs_value = getattr(sigs_resp, "value", None) or []
        if len(sigs_value) < BUNDLE_SAME_SLOT_THRESHOLD:
            # Too few txs — can't confirm or rule out bundle yet.
            return False, ""

        # Sort by slot ascending (oldest first); the very first slot contains create + bundled buys
        sigs_sorted = sorted(sigs_value, key=lambda s: getattr(s, "slot", 0))
        if not sigs_sorted:
            return False, ""
        earliest_slot = getattr(sigs_sorted[0], "slot", 0)
        # V41.8: widened window — catch stagger bundles spaced over up to BUNDLE_SLOT_WINDOW slots.
        same_slot_sigs = [s for s in sigs_sorted if getattr(s, "slot", 0) <= earliest_slot + BUNDLE_SLOT_WINDOW]
        if len(same_slot_sigs) < BUNDLE_SAME_SLOT_THRESHOLD:
            return False, ""

        # Resolve fee_payer per same-slot tx — that's the buyer wallet for buy txs.
        # On a bundle, each pre-buy is signed by a different alt wallet, so distinct
        # fee_payers ARE the bundled wallet count.
        buyer_set: set[str] = set()
        for s in same_slot_sigs:
            sig_str = str(getattr(s, "signature", ""))
            if not sig_str:
                continue
            try:
                tx_resp = client.get_transaction(
                    Signature.from_string(sig_str),
                    max_supported_transaction_version=0,
                    commitment=Confirmed,
                )
                tx_val = getattr(tx_resp, "value", None)
                if not tx_val or not tx_val.transaction:
                    continue
                keys = tx_val.transaction.transaction.message.account_keys
                if keys:
                    # The first account in account_keys is the fee_payer (buyer wallet).
                    fee_payer = str(keys[0])
                    buyer_set.add(fee_payer)
            except Exception:
                continue

        # V41.10/11: when 3+ same-slot buyers detected, distinguish DEV BUNDLE from COPY-TRADER STORM.
        # Dev bundle: alts share parent funder OR have very few recent txs (fresh wallets) → SKIP
        # Copy-trader storm: each buyer has independent funder + many recent diverse txs → ALLOW
        # V41.11: added fresh-wallet count signal — dev alts typically have <5 recent txs total,
        # while copy-trader bots have many sigs across many mints.
        if len(buyer_set) >= BUNDLE_SAME_SLOT_THRESHOLD:
            funder_counts: dict[str, int] = {}
            fresh_wallet_count = 0
            for buyer in buyer_set:
                try:
                    buyer_pk = Pubkey.from_string(buyer)
                    fsigs = client.get_signatures_for_address(
                        buyer_pk, limit=10, commitment=Confirmed
                    )
                    fv = getattr(fsigs, "value", None) or []
                    # V41.11: a wallet with very few sigs (fresh, only-this-mint) is a dev alt.
                    if len(fv) <= 4:
                        fresh_wallet_count += 1
                    for fs in fv[:BUNDLE_FUNDING_LOOKBACK_SIGS]:
                        fsig = str(getattr(fs, "signature", ""))
                        if not fsig:
                            continue
                        try:
                            ftx = client.get_transaction(
                                Signature.from_string(fsig),
                                max_supported_transaction_version=0,
                                commitment=Confirmed,
                            )
                            fval = getattr(ftx, "value", None)
                            if not fval or not fval.transaction or not fval.transaction.meta:
                                continue
                            fkeys = fval.transaction.transaction.message.account_keys
                            if not fkeys or len(fkeys) < 2:
                                continue
                            meta = fval.transaction.meta
                            pre_balances = list(meta.pre_balances)
                            post_balances = list(meta.post_balances)
                            keys_str = [str(k) for k in fkeys]
                            if buyer not in keys_str:
                                continue
                            idx = keys_str.index(buyer)
                            if idx >= len(pre_balances) or idx >= len(post_balances):
                                continue
                            delta = post_balances[idx] - pre_balances[idx]
                            if delta > 1_000_000:  # got >0.001 SOL inbound
                                funder = keys_str[0]  # tx fee_payer = funder for inbound transfer
                                if funder != buyer:
                                    funder_counts[funder] = funder_counts.get(funder, 0) + 1
                                    break  # one funding event per buyer is enough
                        except Exception:
                            continue
                except Exception:
                    continue

            # If a single parent funded BUNDLE_FUNDING_THRESHOLD+ of these buyers → real bundle
            for funder, count in funder_counts.items():
                if count >= BUNDLE_FUNDING_THRESHOLD:
                    return True, (f"BUNDLE: {len(buyer_set)} same-slot wallets, {count} share funder "
                                  f"{funder[:8]}.. (slot {earliest_slot})")
            # V41.11: fresh-wallet check — if 3+ buyers are fresh (<=4 total sigs), they're dev alts
            # even if no shared funder is detectable in the 5-sig lookback.
            if fresh_wallet_count >= BUNDLE_FUNDING_THRESHOLD:
                return True, (f"BUNDLE: {len(buyer_set)} same-slot wallets, {fresh_wallet_count} fresh wallets "
                              f"(<=4 sigs each = dev alts, slot {earliest_slot})")
            # No shared funder + not fresh-wallet pattern → likely copy-trader storm. ALLOW.
            log(f"  bundle-check {str(mint_pk)[:8]}: {len(buyer_set)} same-slot wallets, "
                f"funders={len(funder_counts)} fresh={fresh_wallet_count} — passes as copy-trader/organic")

        # V41.8: early-sell-pressure check — if dev is already dumping in the first
        # EARLY_SELL_PRESSURE_LOOKBACK txs, the launch is being scrubbed. Skip.
        # Look at first N sigs (chronologically), check tx instructions for Sell.
        early_sigs = sigs_sorted[:EARLY_SELL_PRESSURE_LOOKBACK]
        early_sell_count = 0
        for s in early_sigs:
            sig_str = str(getattr(s, "signature", ""))
            if not sig_str:
                continue
            try:
                tx_resp = client.get_transaction(
                    Signature.from_string(sig_str),
                    max_supported_transaction_version=0,
                    commitment=Confirmed,
                )
                tx_val = getattr(tx_resp, "value", None)
                if not tx_val or not tx_val.transaction or not tx_val.transaction.meta:
                    continue
                log_msgs = list(getattr(tx_val.transaction.meta, "log_messages", None) or [])
                if any("Program log: Instruction: Sell" in lm for lm in log_msgs):
                    early_sell_count += 1
            except Exception:
                continue
        # V41.11: loosened from 2 to 4 sells. Per research, organic launches naturally have
        # 1-2 profit-takers in first 10 txs. Only aggressive dumping (4+ sells) signals dev exit.
        if early_sell_count >= 4:
            return True, f"EARLY DUMP: {early_sell_count} sells in first {EARLY_SELL_PRESSURE_LOOKBACK} txs — aggressive dev exit"

        return False, ""
    except Exception as e:
        # On any failure, fail OPEN (don't block legit launches due to RPC hiccup)
        return False, f"check err: {e}"


def curve_progress_from_state(curve: Optional[dict]) -> Optional[float]:
    """Return pump.fun curve progress as 0..1, or None if unreadable."""
    if not curve or curve.get("real_token") is None:
        return None
    try:
        return 1.0 - (float(curve["real_token"]) / STARTING_CURVE_TOKENS)
    except Exception:
        return None


def read_curve_flow(client: Client, bc_pda: Pubkey, limit: int = 12, seconds: int = 45) -> dict:
    """Read recent pump.fun curve flow from on-chain logs.

    This is intentionally free/RPC-only: no paid dashboards, no external alpha feeds.
    Returns buys/sells and unique buyer/seller counts. On error it returns zeros and
    sets err=True so callers can avoid treating RPC failure as a bearish signal.
    """
    flow = {
        "buys": 0, "sells": 0,
        "buyers": set(), "sellers": set(),
        "unique_buyers": 0, "unique_sellers": 0,
        "txs": 0, "err": False,
    }
    try:
        from solders.signature import Signature as SolSig
        sigs = client.get_signatures_for_address(bc_pda, limit=limit, commitment=Confirmed)
        if not sigs.value:
            return flow
        now_i = int(time.time())
        for s in sigs.value[:limit]:
            try:
                bt = getattr(s, "block_time", None) or 0
                if seconds and bt and bt < now_i - seconds:
                    continue
                tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                              max_supported_transaction_version=0,
                                              commitment=Confirmed)
                if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                    continue
                logs = tx.value.transaction.meta.log_messages or []
                keys = tx.value.transaction.transaction.message.account_keys
                signer = str(keys[0]) if keys else "?"
                joined = "\n".join(logs)
                if "Instruction: Buy" in joined:
                    flow["buys"] += 1
                    flow["buyers"].add(signer)
                    flow["txs"] += 1
                elif "Instruction: Sell" in joined:
                    flow["sells"] += 1
                    flow["sellers"].add(signer)
                    flow["txs"] += 1
            except Exception:
                continue
        flow["unique_buyers"] = len(flow["buyers"])
        flow["unique_sellers"] = len(flow["sellers"])
        return flow
    except Exception:
        flow["err"] = True
        return flow


def flow_is_dumping(flow: dict) -> bool:
    """True when recent sells are strong enough that a normal momentum entry is bad."""
    if flow.get("err"):
        return False
    buys = int(flow.get("buys", 0))
    sells = int(flow.get("sells", 0))
    # Sell-majority or a burst of 3+ sells means momentum entry is wrong.
    return sells >= 3 or (sells >= 2 and sells > buys)


def flow_is_recovering(flow: dict) -> bool:
    """True when buyers have absorbed the dump enough for a rebound scalp."""
    if flow.get("err"):
        return False
    buys = int(flow.get("buys", 0))
    sells = int(flow.get("sells", 0))
    unique_buyers = int(flow.get("unique_buyers", 0))
    return buys >= max(2, sells) and unique_buyers >= 2


async def wait_for_dump_rebound(client: Client, bc_pda: Pubkey, mint: str) -> tuple[bool, bool, dict, Optional[dict]]:
    """Do not automatically skip dumps. Convert them into rebound-scalp chances.

    Spot bots cannot profit from a dump by shorting. The code-level edge available
    to a free user is to wait for sell pressure exhaustion, then buy the first
    confirmed curve-price recovery with scalp exits. Returns:
      (allow_entry, is_rebound_scalp, final_flow, final_curve)
    """
    curve0 = get_curve_state(client, bc_pda, fast=True)
    flow0 = read_curve_flow(client, bc_pda, limit=10, seconds=45)
    if not curve0 or not curve0.get("price"):
        return False, False, flow0, curve0

    if not flow_is_dumping(flow0):
        return True, False, flow0, curve0

    if not DUMP_REBOUND_ENABLED:
        log(f"  SKIP {mint[:8]}: dump in progress ({flow0['sells']}/{flow0['txs']} recent sells)")
        return False, False, flow0, curve0

    log(f"  {mint[:8]}: DUMP WATCH ({flow0['buys']} buys/{flow0['sells']} sells) — waiting for rebound instead of blind skip")
    prev_price = float(curve0["price"])
    up_ticks = 0
    final_curve = curve0
    final_flow = flow0
    deadline = time.time() + DUMP_REBOUND_WAIT_SEC

    while time.time() < deadline:
        await asyncio.sleep(1)
        curve = get_curve_state(client, bc_pda, fast=True)
        if not curve or not curve.get("price"):
            continue
        price = float(curve["price"])
        if price >= prev_price * (1.0 + DUMP_REBOUND_MIN_BOUNCE):
            up_ticks += 1
        else:
            up_ticks = 0
        prev_price = price
        final_curve = curve

        if up_ticks >= 2:
            final_flow = read_curve_flow(client, bc_pda, limit=10, seconds=30)
            if flow_is_recovering(final_flow):
                prog = curve_progress_from_state(final_curve)
                ptxt = f"{prog*100:.1f}%" if prog is not None else "?"
                log(f"  {mint[:8]}: DUMP-REBOUND ENTRY OK (curve={ptxt}, flow={final_flow['buys']}B/{final_flow['sells']}S)")
                return True, True, final_flow, final_curve

    log(f"  SKIP {mint[:8]}: dump did not rebound in {DUMP_REBOUND_WAIT_SEC}s")
    return False, False, final_flow, final_curve


async def wait_for_late_breakout(client: Client, bc_pda: Pubkey, mint: str, progress: float) -> tuple[bool, Optional[dict], dict]:
    """For 50-90% curve smart-wallet signals: only scalp if price is still expanding.

    This keeps us from being exit liquidity at 80%+ while not auto-skipping every
    late signal. It requires a tiny real-time curve expansion and buyer-dominant flow.
    """
    if progress >= COBUY_HARD_NO_RUNWAY:
        return False, None, {}
    c1 = get_curve_state(client, bc_pda, fast=True)
    if not c1 or not c1.get("price"):
        return False, c1, {}
    p1 = float(c1["price"])
    pr1 = curve_progress_from_state(c1) or progress
    await asyncio.sleep(2)
    c2 = get_curve_state(client, bc_pda, fast=True)
    if not c2 or not c2.get("price"):
        return False, c2, {}
    p2 = float(c2["price"])
    pr2 = curve_progress_from_state(c2) or pr1
    flow = read_curve_flow(client, bc_pda, limit=8, seconds=25)
    price_growth = (p2 / p1) - 1.0 if p1 else 0.0
    curve_growth = pr2 - pr1
    if (price_growth >= 0.012 or curve_growth >= 0.006) and flow.get("buys", 0) >= max(2, flow.get("sells", 0)):
        log(f"  {mint[:8]}: LATE BREAKOUT SCALP OK ({progress*100:.1f}% curve, price +{price_growth*100:.2f}%, flow={flow['buys']}B/{flow['sells']}S)")
        return True, c2, flow
    log(f"  SKIP {mint[:8]}: late signal but no breakout ({progress*100:.1f}% curve, price +{price_growth*100:.2f}%, flow={flow.get('buys',0)}B/{flow.get('sells',0)}S)")
    return False, c2, flow


def safe_record_sell(pos: Position, sol_recv: Optional[float]) -> bool:
    """Record a sell only if execution/paper simulation succeeded."""
    if sol_recv is None:
        return False
    pos.realized_sol += sol_recv
    return True


def choose_entry_amount(strategy: str, late_scalp: bool, quality_score: int) -> float:
    """V39 sizing engine. Full size is reserved for genuinely strong setups.
    Risky/noisy/dump-rebound/late signals still participate, but only as scouts.
    In paper mode, even weak valid mints can be micro-probed for data."""
    risky = late_scalp or strategy in {"late_breakout", "dump_rebound", "weak_scalp", "micro_probe", "very_late_micro"}
    if not risky and quality_score >= FULL_SIZE_SCORE:
        return min(CORE_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL)
    if quality_score >= 2 or risky:
        return min(SCOUT_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL)
    if PAPER_TRADING and PAPER_SCOUT_EVERY_VALID_MINT:
        return min(MICRO_SCOUT_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL)
    return 0.0


def merge_position_add(base: Position, add: Position):
    """Merge a winner-only scale-in into the existing position using token-weighted
    average entry. This never averages down; it only adds after proof of life."""
    total_tokens = base.token_amount + add.token_amount
    if total_tokens <= 0:
        return
    base.entry_price = ((base.entry_price * base.token_amount) +
                        (add.entry_price * add.token_amount)) / total_tokens
    base.token_amount = total_tokens
    base.entry_amount_sol += add.entry_amount_sol
    base.entry_size_sol = base.entry_amount_sol
    base.bc_pda = base.bc_pda or add.bc_pda
    if base.last_price > 0:
        base.peak_price = max(base.peak_price, base.last_price / base.entry_price)


# === V17: SMART WALLET CO-BUY HANDLER ===
async def handle_smart_wallet_buy(client: Client, kp: Optional[Keypair], wallet: str, sig: str):
    """When a smart wallet does a pump.fun buy, parse the target mint and check co-buy threshold."""
    try:
        # Fetch the tx to identify target mint (the non-SOL token bought)
        tx = client.get_transaction(Signature.from_string(sig),
                                      max_supported_transaction_version=0,
                                      commitment=Confirmed)
        if not tx.value or not tx.value.transaction:
            return
        meta = tx.value.transaction.meta
        if not meta:
            return

        # Find target mint via post token balances
        target_mint = None
        post_balances = meta.post_token_balances or []
        for bal in post_balances:
            mint_str = str(bal.mint)
            if mint_str == SOL_MINT:
                continue
            owner = str(bal.owner) if bal.owner else None
            # The buyer (smart wallet) should now own this token
            if owner == wallet:
                # Token amount > 0 confirms they have a balance after the tx
                if bal.ui_token_amount and float(bal.ui_token_amount.ui_amount or 0) > 0:
                    target_mint = mint_str
                    break

        if not target_mint:
            return

        # Filter: only pump.fun-style mints (vanity-suffix)
        if not (target_mint.lower().endswith("pump") or target_mint.lower().endswith("bonk")):
            return

        log(f"  SMART BUY: {SMART_WALLETS.get(wallet, wallet[:8])} bought {target_mint[:8]}")

        # Track co-buy
        now = time.time()
        prior = smart_wallet_buys.get(target_mint, [])
        # Prune old entries
        prior = [(ts, w) for (ts, w) in prior if now - ts < COBUY_WINDOW_SEC]
        if not any(w == wallet for (ts, w) in prior):
            prior.append((now, wallet))
        smart_wallet_buys[target_mint] = prior

        distinct_buyers = {w for (ts, w) in prior}
        if len(distinct_buyers) >= COBUY_THRESHOLD and target_mint not in cobuy_fired:
            cobuy_fired.add(target_mint)
            names = [SMART_WALLETS.get(w, w[:8]) for w in distinct_buyers]
            log(f"  *** CO-BUY SIGNAL *** {target_mint[:8]} hit by {names}")
            # Fast-track entry — skip the 30s wait
            asyncio.create_task(cobuy_snipe(client, kp, target_mint, names))
    except Exception as e:
        log(f"  smart buy parse err for {wallet[:8]}: {e}")


# V32: smart-wallet sell signals — early-exit triggers for positions we hold
smart_wallet_sold: set[str] = set()

# V41: graduated mints we've already snipped (don't double-snipe same migration)
graduated_seen: set[str] = set()

# V41.1: PumpSwap graduation sniper — tightened after live data showed
# graduation FOMO is small/non-existent. Most grads peak at 1.10-1.20x or just timeout.
# Faster Jupiter retry + tighter TP + faster timeout to extract any small wins
# the rare cold-graduation pump produces, while limiting bleed on dead grads.
GRAD_AMOUNT_SOL = float(os.environ.get("GRAD_AMOUNT_SOL", "0.05"))     # V41.13o: restored to 0.05 SOL ($4.20). $2 wins at +50% TP are the goal. Trailing stop locks partial wins on tokens that don't reach +50%.
GRAD_INITIAL_DELAY_SEC = int(os.environ.get("GRAD_INITIAL_DELAY_SEC", "3"))   # was 8 — try fast
GRAD_JUPITER_RETRY_MAX = int(os.environ.get("GRAD_JUPITER_RETRY_MAX", "12"))  # was 6 — wait up to 60s
# V41.13o: TP restored to +50% sell 100% — target $2.10 win on 0.05 SOL position.
# Trailing stop in manage_graduation_position locks partial wins for tokens that pump
# but don't reach +50% (e.g. peak 1.30x → trail at peak × 0.92 = 1.196x = +$0.82).
GRAD_TP_LADDER = [
    (1.50, 1.00),    # +50% sell 100% — $2.10 target
]
# V41.13o: trailing stop activation/lock. When peak > TRAILING_ACTIVATION, SL becomes
# peak * TRAILING_DISTANCE instead of entry * (1+SL_PCT). Locks partial wins.
GRAD_TRAILING_ACTIVATION = float(os.environ.get("GRAD_TRAILING_ACTIVATION", "1.04"))   # V41.13p: 1.08→1.04 — most grad pumps cap at 1.05-1.10
GRAD_TRAILING_DISTANCE = float(os.environ.get("GRAD_TRAILING_DISTANCE", "0.96"))       # V41.13p: 0.92→0.96 — tighter trail captures more
GRAD_SL_PCT = float(os.environ.get("GRAD_SL_PCT", "-0.07"))             # -7% — gap-cap (loss caps at $0.30/trade)
GRAD_TIMEOUT_SEC = int(os.environ.get("GRAD_TIMEOUT_SEC", "90"))        # 90s — graduation FOMO is short
# V41.12c: ST clean mid-curve tokens grow over minutes/hours, not seconds. Extended timeout.
GRAD_TIMEOUT_ST_SEC = int(os.environ.get("GRAD_TIMEOUT_ST_SEC", "1800")) # 30 min hold for ST clean mints


async def graduation_snipe(client: Client, kp: Optional[Keypair], mint: str,
                           launchpad: str = "pump", signer: Optional[str] = None):
    """V41: PumpSwap graduation sniper. V41.7: launchpad-aware (pump | bonk).

    When a pump.fun token graduates to PumpSwap, the bonding curve completes and
    a new PumpSwap pool is created. The first 30-300 seconds typically see FOMO
    buys from DexScreener trending + retail catching up. This is structurally
    different from pre-graduation sniping:

    - Token has already survived the 99.5% rug filter (it graduated)
    - PumpSwap LP tokens are burned at migration → liquidity is permanent
    - Predictable event trigger (curve.complete = true → migrate instruction)
    - Less competition than pre-grad (most bots focus on fresh launches)

    Strategy:
      1. Wait GRAD_INITIAL_DELAY_SEC for Jupiter to index the new pool
      2. Get Jupiter quote (it will route through PumpSwap automatically)
      3. Retry up to GRAD_JUPITER_RETRY_MAX if no route yet
      4. Buy GRAD_AMOUNT_SOL
      5. Tight TP ladder + tight SL + short timeout

    V41.17: copy_fast branch parallelizes the probe quote with a smart-wallet
    exit check (Fix #3) so we abort if the trader has already dumped, AND
    consults the warm /stream/swap pool (Fix #2) for ~10-30ms entries vs 300-500ms.
    """
    global session_pnl_sol
    try:
        blocked, reason = _entry_circuit_breakers_open()
        if blocked:
            log(f"  GRAD HALT {mint[:8]}: {reason}")
            return
        # V41.14: ST/copy_fast/grad_imminent/momentum entries have NO concurrent cap —
        # quality is pre-verified, position size intentional, paper-mode bankroll unlimited.
        # Was silently dropping copy_fast entries when 5 grad-imminent slots were open.
        UNCAPPED = ("st_pump", "copy_fast", "grad_imminent", "momentum")
        if launchpad not in UNCAPPED and len(positions) >= MAX_CONCURRENT_POSITIONS:
            log(f"  GRAD HALT {mint[:8]}: max concurrent ({MAX_CONCURRENT_POSITIONS}) reached, launchpad={launchpad}")
            return
        if mint in positions:
            log(f"  GRAD SKIP {mint[:8]}: already in positions (dedup)")
            return

        # V41.14: copy_fast — when shredSubscribe fires for a top trader's buy, we
        # have ~200ms latency advantage. Burning it on observation wait kills the edge.
        # Skip observation entirely and buy on the same block the trader bought.
        if launchpad == "copy_fast":
            signal_time_ms = int(time.time() * 1000)
            probe_sol_lamports = int(0.001 * 1e9)
            # V41.17 Fix #3: race the probe quote with a smart-wallet exit check.
            # Both calls are ~150-300ms; running them in parallel adds zero hot-path
            # latency. If the smart wallet has already sold this mint, we'd be
            # buying their exit liquidity — abort.
            quote_task = asyncio.create_task(asyncio.to_thread(
                jupiter_quote, SOL_MINT, mint, probe_sol_lamports
            ))
            if EXIT_CHECK_ENABLED and signer:
                exit_task = asyncio.create_task(_smart_wallet_still_holding(signer, mint))
            else:
                exit_task = None
            baseline_quote = await quote_task
            if exit_task is not None:
                still_holding = await exit_task
                if not still_holding:
                    _copy_trade_stats["exit_blocked"] += 1
                    log(f"  GRAD ABORT {mint[:8]} (copy_fast): smart wallet {signer[:8]} already sold")
                    return
            if not baseline_quote or float(baseline_quote.get("outAmount", 0)) == 0:
                log(f"  GRAD SKIP {mint[:8]}: copy_fast no Raptor/Jupiter route — skipping")
                return
            # V41.17 Fix #2: warm pool fast-path. If pre-built tx is fresh, ship it.
            # CRITICAL: the warm path submits the tx itself (no second swap via buy_token).
            # Bookkeeping quote runs AFTER send so its latency doesn't delay entry.
            pos = None
            warm = _consume_warm_swap_tx(mint) if (not PAPER_TRADING and kp) else None
            if warm:
                tx_b64, _lvbh = warm
                log(f"  GRAD WARM HIT {mint[:8]} (copy_fast): pre-built tx, shipping immediately")
                # Skip simulation on warm path — Raptor already validated the tx; latency wins
                sig_out = execute_swap(kp, client, tx_b64, simulate_first=False)
                if sig_out:
                    # Post-send bookkeeping — quote latency now irrelevant for entry timing
                    bookkeep = jupiter_quote(SOL_MINT, mint, int(GRAD_AMOUNT_SOL * 10**WSOL_DECIMALS))
                    out_amt = float(bookkeep.get("outAmount", 0)) if bookkeep else 0.0
                    if out_amt > 0:
                        entry_price = int(GRAD_AMOUNT_SOL * 10**WSOL_DECIMALS) / out_amt
                        pos = Position(
                            mint=mint, entry_price=entry_price,
                            entry_amount_sol=GRAD_AMOUNT_SOL, token_amount=out_amt,
                            open_time=time.time(),
                            bc_pda=derive_bc_pda(Pubkey.from_string(mint)),
                        )
                        _copy_trade_stats["warm_hit"] += 1
                        log(f"  GRAD WARM ENTERED {mint[:8]} @ {entry_price:.6e} (sig={sig_out[:16]})")
                    else:
                        log(f"  GRAD WARM bookkeeping quote failed {mint[:8]} — pos lost from accounting")
                else:
                    log(f"  GRAD WARM tx send failed {mint[:8]} — falling back to standard buy")
                    _copy_trade_stats["warm_miss"] += 1
            # Fallback to standard buy_token (does its own quote+swap+send)
            if pos is None:
                log(f"  GRAD ENTRY {mint[:8]} (copy_fast): trader signal, entering immediately, buying {GRAD_AMOUNT_SOL} SOL")
                pos = buy_token(kp, client, mint, GRAD_AMOUNT_SOL)
                if not pos:
                    log(f"  GRAD BUY FAILED {mint[:8]}")
                    return
            pos.strategy = "graduation"
            pos.late_scalp = True
            pos.entry_progress = 1.0
            pos.entry_size_sol = GRAD_AMOUNT_SOL
            pos.quality_score = 8
            pos.launchpad = launchpad
            # V41.17 Fix #9: stamp signal time so manage_graduation_position can apply
            # the 8s no-pump time-stop without affecting non-copy-fast flows.
            pos.signal_time_ms = signal_time_ms
            positions[mint] = pos
            _record_entry_opened()
            asyncio.create_task(manage_graduation_position(client, kp, pos))
            return

        log(f"  GRAD WAIT {mint[:8]}: holding {GRAD_INITIAL_DELAY_SEC}s for Jupiter to index PumpSwap pool")
        await asyncio.sleep(GRAD_INITIAL_DELAY_SEC)

        # Try to get a baseline quote — retry with backoff if Jupiter hasn't indexed yet
        probe_sol_lamports = int(0.001 * 1e9)  # tiny probe quote, just for price discovery
        baseline_quote = None
        for attempt in range(GRAD_JUPITER_RETRY_MAX):
            baseline_quote = jupiter_quote(SOL_MINT, mint, probe_sol_lamports)
            if baseline_quote and float(baseline_quote.get("outAmount", 0)) > 0:
                break
            log(f"  GRAD {mint[:8]}: Jupiter no route yet (attempt {attempt+1}/{GRAD_JUPITER_RETRY_MAX}), waiting 5s")
            await asyncio.sleep(5)
        if not baseline_quote or float(baseline_quote.get("outAmount", 0)) == 0:
            log(f"  GRAD SKIP {mint[:8]}: Jupiter never indexed pool after {GRAD_JUPITER_RETRY_MAX} attempts")
            return

        baseline_tokens_per_001 = float(baseline_quote["outAmount"])

        # V41.3 MOMENTUM-CONFIRMED ENTRY: observe price action for 5s before entering.
        # Empirical V41 data showed graduations split into 3 outcomes in the first 10s:
        #   - Price up >=5%  -> usually keeps running to TP (rare but real wins)
        #   - Price flat     -> 90s timeout, small drag loss
        #   - Price down     -> immediate -15% to -50% gap dump
        # Waiting 5s and only entering on confirmed +5% momentum filters out ~80% of losers.
        log(f"  GRAD OBSERVE {mint[:8]}: baseline set, waiting 5s for momentum confirmation")
        await asyncio.sleep(5)
        confirm_quote = jupiter_quote(SOL_MINT, mint, probe_sol_lamports)
        if not confirm_quote or float(confirm_quote.get("outAmount", 0)) == 0:
            log(f"  GRAD SKIP {mint[:8]}: Jupiter route disappeared during observe window")
            return
        confirm_tokens_per_001 = float(confirm_quote["outAmount"])
        # Price went UP if we get fewer tokens for the same SOL.
        price_change = (baseline_tokens_per_001 / confirm_tokens_per_001) - 1.0 if confirm_tokens_per_001 else 0.0
        # V41.4: dual-threshold momentum gate. Empirical V41.3 data:
        #   +25% momentum -> 2.73x win
        #   +77% momentum -> -30% loss
        #   +97% momentum -> -50% loss
        # Extreme spikes (>50% in 5s) are single-whale pumps that get dumped on us.
        # Real continuations are gradual — 5-50% range is the sweet spot.
        # V41.5 graduation tightening: +40-50% sweet spot for graduation pumps.
        # V41.12: ST CLEAN MINTs are mid-curve tokens already vetted by Solana Tracker's
        # multi-heuristic risk engine. We trust their filter — only require the price
        # isn't actively dropping (-3% floor). Skip extreme spikes since they're bait.
        if launchpad == "momentum":
            # V41.13j: momentum sniper already verified 5m/15m uptrend via ST events.
            # No additional gate needed. Only reject if price collapsed in the 5s observe window.
            if price_change < -0.10:
                log(f"  GRAD SKIP {mint[:8]}: collapsed during observe ({price_change*100:+.1f}%)")
                return
            log(f"  GRAD MOMENTUM OK {mint[:8]} (momentum): {price_change*100:+.1f}% — riding hot token")
        elif launchpad == "grad_imminent":
            # V41.13h: grad-imminent tokens (curve 95-99%) are flat by nature — buying
            # fills the curve without raising price. We don't need momentum, the catalyst
            # is the imminent graduation event. Only reject if price is actively dumping.
            if price_change < -0.05:
                log(f"  GRAD SKIP {mint[:8]}: dumping pre-grad ({price_change*100:+.1f}% in 5s)")
                return
            log(f"  GRAD MOMENTUM OK {mint[:8]} (grad_imminent): {price_change*100:+.1f}% — entering pre-grad")
        elif launchpad == "st_pump":
            # V41.13f: ST entries now require MONOTONIC RISING price over 15s (3 samples).
            # Previous "anywhere from -3% allowed" let too many flat/declining tokens enter.
            # Real winners (HfpkGDz1) showed sustained uptrend BEFORE we entered. We need
            # to confirm the uptrend exists, not just that it isn't dumping.
            log(f"  GRAD ST-CONFIRM {mint[:8]}: T+5s={price_change*100:+.1f}%, watching 10 more")
            await asyncio.sleep(10)
            tx_quote = jupiter_quote(SOL_MINT, mint, probe_sol_lamports)
            if not tx_quote or float(tx_quote.get("outAmount", 0)) == 0:
                log(f"  GRAD SKIP {mint[:8]}: Jupiter route lost during ST confirmation")
                return
            tx_tokens_per_001 = float(tx_quote["outAmount"])
            price_change_15 = (baseline_tokens_per_001 / tx_tokens_per_001) - 1.0 if tx_tokens_per_001 else 0.0
            # Require: rising at T+5s (>+1%), still rising at T+15s, total +3% to +50%
            if price_change < 0.01:
                log(f"  GRAD SKIP {mint[:8]}: T+5s flat/down ({price_change*100:+.1f}%) — no uptrend confirmed")
                return
            if price_change_15 < price_change:
                log(f"  GRAD SKIP {mint[:8]}: T+15s reversed ({price_change_15*100:+.1f}% < T+5s {price_change*100:+.1f}%) — momentum stalled")
                return
            if price_change_15 < 0.03:
                log(f"  GRAD SKIP {mint[:8]}: T+15s only {price_change_15*100:+.1f}% — below +3% confirmation floor")
                return
            if price_change_15 > 0.50:
                log(f"  GRAD SKIP {mint[:8]}: T+15s spike +{price_change_15*100:.1f}% — pump-and-dump bait")
                return
            log(f"  GRAD MOMENTUM OK {mint[:8]} (st_pump): T+5s +{price_change*100:.1f}%, T+15s +{price_change_15*100:.1f}% — confirmed uptrend")
        else:
            min_mom, max_mom = 0.40, 0.50
            if price_change < min_mom:
                log(f"  GRAD SKIP {mint[:8]}: price dropping after 5s ({price_change*100:+.1f}%) — below {min_mom*100:.0f}% floor (40-50% graduation sweet spot)")
                return
            if price_change > max_mom:
                log(f"  GRAD SKIP {mint[:8]}: extreme spike +{price_change*100:.1f}% in 5s — above {max_mom*100:.0f}% (40-50% graduation sweet spot)")
                return
            log(f"  GRAD MOMENTUM OK {mint[:8]} ({launchpad}): +{price_change*100:.1f}% in 5s (band: 40-50% graduation sweet spot)")

        log(f"  GRAD ENTRY {mint[:8]} ({launchpad}): confirmed pump, buying {GRAD_AMOUNT_SOL} SOL")
        pos = buy_token(kp, client, mint, GRAD_AMOUNT_SOL)
        if not pos:
            log(f"  GRAD BUY FAILED {mint[:8]}")
            return
        # Mark this as a graduation snipe — uses GRAD ladder, GRAD timeout
        pos.strategy = "graduation"
        pos.late_scalp = True   # use tight scalp-style exits
        pos.entry_progress = 1.0   # already graduated
        pos.entry_size_sol = GRAD_AMOUNT_SOL
        pos.quality_score = 8     # graduated tokens are inherently higher quality
        pos.launchpad = launchpad
        positions[mint] = pos
        _record_entry_opened()
        asyncio.create_task(manage_graduation_position(client, kp, pos))
    except Exception as e:
        log(f"  graduation_snipe err for {mint[:8]}: {e}")


async def manage_graduation_position(client: Client, kp: Optional[Keypair], pos: Position):
    """V41: lightweight position manager for graduation snipes.

    Different from manage_position (which expects pump.fun bonding curve reads).
    Graduated tokens trade on PumpSwap, so we use Jupiter quotes for price.
    """
    global session_pnl_sol, session_wins, session_losses, consec_losses
    log(f"Managing GRAD {pos.mint[:8]} entry={pos.entry_amount_sol:.4f} SOL")
    closed = False
    close_reason = ""
    open_time = pos.open_time
    rung_hit = 0

    def try_grad_sell(reason: str, fraction: float, multiplier: float) -> bool:
        nonlocal close_reason, closed
        if pos.remaining_pct <= 0.01:
            close_reason = reason
            closed = True
            return True
        sol_recv = sell_token(kp, client, pos, fraction, current_multiplier=multiplier)
        if sol_recv is None:
            log(f"  GRAD SELL FAILED ({reason}) {pos.mint[:8]} — keeping position")
            return False
        pos.realized_sol += sol_recv
        pos.remaining_pct *= (1 - fraction)
        close_reason = reason
        if fraction >= 0.999 or pos.remaining_pct <= 0.01:
            closed = True
        return True

    while not closed:
        try:
            elapsed = time.time() - open_time
            # V41.13-14: ST, grad-imminent, momentum, copy_fast entries have NO timeout.
            if pos.launchpad in ("st_pump", "grad_imminent", "momentum", "copy_fast"):
                timeout_for_pos = float("inf")
            else:
                timeout_for_pos = GRAD_TIMEOUT_SEC
            if elapsed > timeout_for_pos:
                # Get current price for accurate close
                probe_qty = max(int(pos.token_amount * 0.01), 1)
                quote = jupiter_quote(pos.mint, SOL_MINT, probe_qty)
                if quote and float(quote.get("outAmount", 0)) > 0:
                    cur_price = float(quote["outAmount"]) / probe_qty
                    mult = cur_price / pos.entry_price if pos.entry_price else 1.0
                else:
                    mult = pos.last_price / pos.entry_price if pos.last_price > 0 else 1.0
                if try_grad_sell(f"GRAD TIMEOUT {timeout_for_pos}s mult={mult:.2f}x", 1.0, mult):
                    break

            # Get current price via Jupiter probe (1% of position)
            probe_qty = max(int(pos.token_amount * 0.01), 1)
            quote = jupiter_quote(pos.mint, SOL_MINT, probe_qty)
            if not quote or float(quote.get("outAmount", 0)) == 0:
                # Jupiter route lost — wait and retry
                await asyncio.sleep(3)
                continue
            sol_per_unit = float(quote["outAmount"]) / probe_qty
            pos.last_price = sol_per_unit
            multiplier = sol_per_unit / pos.entry_price if pos.entry_price else 1.0
            if multiplier > pos.peak_price:
                pos.peak_price = multiplier

            # V41.17 Fix #9: 8s no-pump time-stop for copy_fast entries.
            # Empirical: 4 winners hit 1.04x activation in 3-5s; 3 losers sat dead at
            # 0.97-1.02x for 15+s before tanking. If peak hasn't reached activation by
            # TIME_STOP_NO_PUMP_SEC, the pump has died — exit at current price (cap loss
            # at -1% to break-even rather than trailing's eventual -4%). Only fires BEFORE
            # trailing activates; once activated, trailing logic owns the exit.
            if (TIME_STOP_ENABLED and pos.launchpad == "copy_fast"
                    and pos.signal_time_ms > 0
                    and pos.peak_price < GRAD_TRAILING_ACTIVATION):
                age_s = (time.time() * 1000 - pos.signal_time_ms) / 1000
                if age_s > TIME_STOP_NO_PUMP_SEC:
                    if try_grad_sell(
                        f"GRAD 8s NO-PUMP exit (age={age_s:.1f}s peak={pos.peak_price:.3f}x mult={multiplier:.3f}x)",
                        1.0, multiplier,
                    ):
                        break

            # V41.13o + V41.14d: TRAILING STOP with slippage protection.
            # Empirical: full-position sell into $5-15k pool slips 2-5%. If trail floor is
            # 1.01x but slippage takes us back to 0.98x, "trail win" becomes real loss.
            # Require: trail_floor must be > 1 + GRAD_TRAILING_MIN_LOCK (clear slippage).
            change = (sol_per_unit - pos.entry_price) / pos.entry_price
            if pos.peak_price >= GRAD_TRAILING_ACTIVATION:
                trail_floor = pos.peak_price * GRAD_TRAILING_DISTANCE
                # Only honor trail exit if lock >= 4% (covers round-trip slippage)
                min_lock = 1.04
                if trail_floor < min_lock:
                    # Use min_lock as effective trail floor (don't exit below slippage threshold)
                    trail_floor = min_lock
                if multiplier <= trail_floor:
                    win_pct = (trail_floor - 1.0) * 100
                    if try_grad_sell(f"GRAD TRAIL exit at {trail_floor:.2f}x (peak={pos.peak_price:.2f}x, locked {win_pct:+.1f}%)", 1.0, trail_floor):
                        break
            elif change <= GRAD_SL_PCT:
                if try_grad_sell(f"GRAD SL hit {change*100:.1f}% mult={multiplier:.2f}x", 1.0, multiplier):
                    break

            # TP ladder — uses CURRENT price not peak (V40 fix preserved).
            # V41.7/9: bonk launchpad uses 1% trade fee vs pump.fun 0.3%, so TP threshold
            # gets a +2% offset to net the same realised profit after fees.
            fee_offset = 0.02 if pos.launchpad in ("bonk", "bonk_pregrad") else 0.0
            if rung_hit < len(GRAD_TP_LADDER):
                trigger, sell_frac = GRAD_TP_LADDER[rung_hit]
                effective_trigger = trigger + fee_offset
                if multiplier >= effective_trigger:
                    log(f"  GRAD TP RUNG {rung_hit+1} ({pos.launchpad}) mult={multiplier:.2f}x trigger={effective_trigger:.2f}x: selling {sell_frac*100:.0f}% of remaining")
                    if try_grad_sell(f"GRAD TP RUNG {rung_hit+1} mult={multiplier:.2f}x", sell_frac, multiplier):
                        rung_hit += 1
                        if pos.remaining_pct <= 0.01:
                            break

            await asyncio.sleep(2)
        except Exception as e:
            log(f"  manage_graduation_position err: {e}")
            await asyncio.sleep(5)

    pnl = pos.realized_sol - pos.entry_amount_sol
    _record_trade_close(pnl)
    log(f"  CLOSED GRAD {pos.mint[:8]} peak={pos.peak_price:.2f}x recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} "
        f"pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} W={session_wins} L={session_losses} reason={close_reason}")
    if pos.mint in positions:
        del positions[pos.mint]


async def handle_smart_wallet_sell(client: Client, kp: Optional[Keypair], wallet: str, sig: str):
    """V32: when a smart wallet SELLS a pump.fun token we hold, signal immediate exit.
    The smart wallet's sell is the strongest leading indicator that the pump is over —
    insiders know first, retail dumps after."""
    try:
        tx = client.get_transaction(Signature.from_string(sig),
                                      max_supported_transaction_version=0,
                                      commitment=Confirmed)
        if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
            return
        meta = tx.value.transaction.meta
        # Find token sold via PRE token balances (smart wallet had it before, less or zero after)
        pre_balances = meta.pre_token_balances or []
        post_balances = meta.post_token_balances or []
        post_owned = {(str(b.mint), str(b.owner)): float(b.ui_token_amount.ui_amount or 0)
                      for b in post_balances if b.owner}
        for bal in pre_balances:
            mint_str = str(bal.mint)
            if mint_str == SOL_MINT:
                continue
            if str(bal.owner) != wallet:
                continue
            pre_amt = float(bal.ui_token_amount.ui_amount or 0) if bal.ui_token_amount else 0
            post_amt = post_owned.get((mint_str, wallet), 0)
            if pre_amt > 0 and post_amt < pre_amt:
                # This is a sale of mint_str by wallet
                if mint_str in positions:
                    log(f"  *** SMART SELL *** {SMART_WALLETS.get(wallet, wallet[:8])} sold {mint_str[:8]} (we hold) — exit signal")
                    smart_wallet_sold.add(mint_str)
                else:
                    log(f"  smart sell: {SMART_WALLETS.get(wallet, wallet[:8])} sold {mint_str[:8]} (no position)")
                return
    except Exception as e:
        log(f"  smart sell parse err for {wallet[:8]}: {e}")


async def cobuy_snipe(client: Client, kp: Optional[Keypair], mint: str, smart_names: list):
    """V38 smart-wallet entry.

    Earlier versions either skipped too much or chased late exit-liquidity.
    This version uses three modes:
      1) <50% curve: normal co-buy momentum.
      2) 50-90% curve: late-breakout SCALP only if price/flow confirms.
      3) active dump: wait for dump-exhaustion rebound, then SCALP.
    """
    global session_pnl_sol, consec_losses
    try:
        if session_pnl_sol <= -MAX_SESSION_LOSS_SOL: return
        if consec_losses >= MAX_CONSEC_LOSSES: return
        if len(positions) >= MAX_CONCURRENT_POSITIONS: return
        if mint in positions:
            return

        # Quick structural safety. These are the only true hard rejections.
        try:
            mint_pk = Pubkey.from_string(mint)
            info = client.get_account_info(mint_pk, commitment=Confirmed)
            if not info.value: return
            data = bytes(info.value.data)
            if len(data) < 82: return
            owner = str(info.value.owner)
            if owner not in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                             "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
                return
            mint_auth = int.from_bytes(data[0:4], "little")
            freeze_auth = int.from_bytes(data[46:50], "little")
            if mint_auth == 1 or freeze_auth == 1:
                log(f"  CO-BUY SKIP {mint[:8]}: authority not renounced")
                return
        except Exception:
            return

        bc_pda = derive_bc_pda(mint_pk)
        curve = get_curve_state(client, bc_pda, fast=True)
        if not curve or not curve.get("price"):
            log(f"  CO-BUY SKIP {mint[:8]}: no curve state")
            return
        progress = curve_progress_from_state(curve)
        if progress is None:
            log(f"  CO-BUY SKIP {mint[:8]}: progress unreadable")
            return

        late_scalp = False
        strategy = "cobuy"
        quality_score = 3  # a validated wallet bought it; this is our base edge

        # 90%+ has almost no curve runway. Live mode should not donate exit liquidity;
        # paper mode can micro-probe to collect evidence without pretending it is safe.
        if progress >= COBUY_HARD_NO_RUNWAY:
            if not (PAPER_TRADING and PAPER_SCOUT_EVERY_VALID_MINT):
                log(f"  CO-BUY SKIP {mint[:8]}: curve {progress*100:.1f}% (>90%, no runway)")
                return
            log(f"  CO-BUY MICRO {mint[:8]}: curve {progress*100:.1f}% (>90%, paper probe only)")
            late_scalp = True
            strategy = "very_late_micro"
            quality_score -= 3

        # 50-90% is no longer a blind skip. It can be taken only as a breakout scalp.
        if progress >= LATE_SCALP_CURVE_START and strategy != "very_late_micro":
            ok, curve2, flow = await wait_for_late_breakout(client, bc_pda, mint, progress)
            if not ok:
                if not (PAPER_TRADING and PAPER_SCOUT_EVERY_VALID_MINT):
                    return
                log(f"  {mint[:8]}: late breakout failed — paper micro-probe instead of full skip")
                late_scalp = True
                strategy = "micro_probe"
                quality_score -= 2
            else:
                late_scalp = True
                strategy = "late_breakout"
                quality_score += 1
                if curve2 and curve2.get("price"):
                    curve = curve2
                    progress = curve_progress_from_state(curve2) or progress
        else:
            # For normal early/mid co-buys, dumps become rebound-scalps instead of auto-skips.
            ok, is_rebound, flow, curve2 = await wait_for_dump_rebound(client, bc_pda, mint)
            if not ok:
                if not (PAPER_TRADING and PAPER_SCOUT_EVERY_VALID_MINT):
                    return
                log(f"  {mint[:8]}: dump did not rebound — paper micro-probe only")
                late_scalp = True
                strategy = "micro_probe"
                quality_score -= 2
            if curve2 and curve2.get("price"):
                curve = curve2
                progress = curve_progress_from_state(curve2) or progress
            if is_rebound:
                late_scalp = True
                strategy = "dump_rebound"
                quality_score += 1

        # Buyer-history is useful, but not a hard skip for smart-wallet entries.
        # If weak, downgrade to scalp mode rather than throw away every opportunity.
        buyer_ok = check_buyer_history(client, bc_pda, mint)
        if buyer_ok:
            quality_score += 2
        else:
            log(f"  {mint[:8]}: buyer history weak; taking only scalp-mode if entry fires")
            late_scalp = True
            if strategy == "cobuy":
                strategy = "weak_scalp"

        cashback_tag = " [CASHBACK]" if curve.get("cashback") else ""
        amount_sol = choose_entry_amount(strategy, late_scalp, quality_score)
        if amount_sol <= 0:
            log(f"  CO-BUY NO ENTRY {mint[:8]}: strategy={strategy}, score={quality_score}")
            return
        log(f"  CO-BUY ENTRY{cashback_tag} {mint[:8]}: curve {progress*100:.1f}%, strategy={strategy}, score={quality_score}, amount={amount_sol:.4f} SOL, smart={smart_names}")
        pos = buy_token(kp, client, mint, amount_sol)
        if pos:
            pos.late_scalp = late_scalp
            pos.strategy = strategy
            pos.entry_progress = progress
            pos.quality_score = quality_score
            pos.entry_size_sol = amount_sol
            positions[mint] = pos
            asyncio.create_task(manage_position(client, kp, pos))
    except Exception as e:
        log(f"  cobuy_snipe err for {mint[:8]}: {e}")

# V36: cache for buyer-history lookups so we don't burn RPC on repeats
_buyer_history_cache: dict[str, tuple[bool, float]] = {}  # wallet -> (established, ts)
_BUYER_HISTORY_TTL = 300  # 5 min


def is_established_wallet(client: Client, buyer: str) -> bool:
    """V36: an "established" wallet has prior on-chain activity (>=5 sigs).
    Fresh dev-alt wallets have 0-2 sigs. Distinguishes real buyers from rug-pullers' alts."""
    cached = _buyer_history_cache.get(buyer)
    if cached and time.time() - cached[1] < _BUYER_HISTORY_TTL:
        return cached[0]
    try:
        from solana.rpc.commitment import Confirmed
        sigs = client.get_signatures_for_address(Pubkey.from_string(buyer), limit=5, commitment=Confirmed)
        established = bool(sigs.value) and len(sigs.value) >= 5
        _buyer_history_cache[buyer] = (established, time.time())
        return established
    except Exception:
        return False


def check_buyer_history(client: Client, bc_pda: Pubkey, mint: str) -> bool:
    """V36: pump.fun graduation-rate research finding (April 2026):
    Of ~30k tokens launched daily, <0.5% graduate. Tokens that graduate share
    one signal — early buyers are ESTABLISHED wallets (real users), not freshly
    generated dev alts. Tokens with fresh-wallet first buyers are 99%+ rugs.

    Check first 5 unique buyers. Require >=3 established (5+ prior sigs)."""
    try:
        from solders.signature import Signature as SolSig
        # NOTE: getSignaturesForAddress and getTransaction require Confirmed commitment minimum.
        sigs = client.get_signatures_for_address(bc_pda, limit=12, commitment=Confirmed)
        unique_buyers: list[str] = []
        seen = set()
        for s in sigs.value[:12]:
            try:
                tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                              max_supported_transaction_version=0,
                                              commitment=Confirmed)
                if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                    continue
                logs = tx.value.transaction.meta.log_messages or []
                if not any("Instruction: Buy" in l for l in logs):
                    continue
                keys = tx.value.transaction.transaction.message.account_keys
                if not keys:
                    continue
                buyer = str(keys[0])
                if buyer in seen:
                    continue
                seen.add(buyer)
                unique_buyers.append(buyer)
                if len(unique_buyers) >= 5:
                    break
            except Exception:
                continue
        if len(unique_buyers) < 3:
            log(f"  {mint[:8]}: SKIP (only {len(unique_buyers)} unique buyers — too few to assess)")
            return False
        established = sum(1 for b in unique_buyers if is_established_wallet(client, b))
        if established < 3:
            log(f"  {mint[:8]}: SKIP (only {established}/{len(unique_buyers)} buyers established — likely dev alts)")
            return False
        log(f"  {mint[:8]}: buyer history OK ({established}/{len(unique_buyers)} established)")
        return True
    except Exception as e:
        log(f"  {mint[:8]}: buyer history err {e} — proceeding")
        return True  # fail-open


def count_unique_buyers(client: Client, bc_pda: Pubkey, lookback: int = 5) -> int:
    """V34: count distinct buyer wallets in last N txs on bonding curve.
    Dev-self-pumps have 1-2 unique buyers (dev + alts). Organic momentum has many.
    Returns count of unique buyer pubkeys, or -1 on error."""
    try:
        from solders.signature import Signature as SolSig
        sigs = client.get_signatures_for_address(bc_pda, limit=lookback, commitment=Confirmed)
        buyers = set()
        for s in sigs.value[:lookback]:
            try:
                tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                              max_supported_transaction_version=0,
                                              commitment=Confirmed)
                if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                    continue
                logs = tx.value.transaction.meta.log_messages or []
                if not any("Instruction: Buy" in l for l in logs):
                    continue
                # Buyer is the fee payer / first signer
                keys = tx.value.transaction.transaction.message.account_keys
                if keys:
                    buyers.add(str(keys[0]))
            except: continue
        return len(buyers)
    except Exception:
        return -1


# === DEV WALLET REPUTATION (V15) ===
# In-memory cache, keyed by creator pubkey string. Lazy-populated on first lookup.
# Each entry: {"prior_tokens": int, "rugs_under_1h": int, "checked_at": ts}
_dev_rep_cache: dict[str, dict] = {}
DEV_REP_TTL_SEC = 3600   # refresh after 1 hour


def get_token_creator(client: Client, mint_pk: Pubkey) -> Optional[str]:
    """Find the wallet that created the mint (signer of the create transaction)."""
    try:
        from solders.signature import Signature as SolSig
        sigs = client.get_signatures_for_address(mint_pk, limit=10, commitment=Confirmed)
        if not sigs.value:
            return None
        # Oldest signatures last in the response — iterate to find a tx that calls
        # pump.fun's Create instruction.
        for sig_info in reversed(sigs.value):
            try:
                tx = client.get_transaction(SolSig.from_string(str(sig_info.signature)),
                                              max_supported_transaction_version=0,
                                              commitment=Confirmed)
                if not tx.value or not tx.value.transaction:
                    continue
                meta = tx.value.transaction.meta
                if meta and meta.log_messages:
                    if any("Instruction: Create" in l for l in meta.log_messages):
                        keys = tx.value.transaction.transaction.message.account_keys
                        if keys:
                            return str(keys[0])
            except Exception:
                continue
        return None
    except Exception:
        return None


def check_dev_reputation(client: Client, mint_pk: Pubkey) -> bool:
    """Returns True if creator wallet looks acceptable, False if known serial rugger.
    Conservative: new creators (no history) are ACCEPTED — per research, 70%+ of
    graduators are first-time creators."""
    creator = get_token_creator(client, mint_pk)
    if not creator:
        return True  # can't determine creator -> accept (don't penalize indeterminacy)

    cached = _dev_rep_cache.get(creator)
    if cached and time.time() - cached["checked_at"] < DEV_REP_TTL_SEC:
        prior = cached["prior_tokens"]
        rugs = cached["rugs_under_1h"]
        # Reject if creator has 3+ tokens that all died fast
        if prior >= 3 and rugs == prior:
            return False
        return True

    # Fresh lookup: get last ~50 transactions for the creator wallet, find pump.fun
    # Create instructions, then check each prior token's bonding curve survival.
    try:
        from solders.signature import Signature as SolSig
        sigs = client.get_signatures_for_address(Pubkey.from_string(creator),
                                                  limit=50, commitment=Confirmed)
        prior_tokens: list[Pubkey] = []
        if sigs.value:
            for s in sigs.value:
                try:
                    tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                                  max_supported_transaction_version=0,
                                                  commitment=Confirmed)
                    if not tx.value or not tx.value.transaction:
                        continue
                    meta = tx.value.transaction.meta
                    if not meta or not meta.log_messages:
                        continue
                    if not any("Instruction: Create" in l for l in meta.log_messages):
                        continue
                    keys = tx.value.transaction.transaction.message.account_keys
                    if len(keys) > 1:
                        candidate = keys[1]
                        if candidate != mint_pk and candidate not in prior_tokens:
                            prior_tokens.append(candidate)
                except Exception:
                    continue

        # For each prior token, check current bonding curve state — if "complete" or
        # very low real_token_reserves with low real_sol_reserves, it likely lived.
        # If real_token_reserves still ~starting and real_sol_reserves tiny, it died.
        rugs = 0
        for ptok in prior_tokens[:8]:  # cap lookups; recent 8 prior tokens are sufficient signal
            try:
                pbc = derive_bc_pda(ptok)
                pcurve = get_curve_state(client, pbc)
                if not pcurve:
                    continue
                # Heuristic: "rugged in <1h" ~ low real_sol AND not graduated AND high real_token_reserves
                # I.e., almost no buyers picked it up.
                if (not pcurve["complete"]
                    and pcurve["virtual_sol"] < 30 * 10**9   # < 30 SOL pumped in
                    and pcurve["real_token"] > 700_000_000_000_000):  # ~ near starting supply
                    rugs += 1
            except Exception:
                continue

        _dev_rep_cache[creator] = {
            "prior_tokens": len(prior_tokens),
            "rugs_under_1h": rugs,
            "checked_at": time.time(),
        }
        if len(prior_tokens) >= 3 and rugs == len(prior_tokens):
            return False
        return True
    except Exception:
        return True   # on error, default to accept


# === JUPITER SWAP ===
def _raptor_quote_raw(input_mint: str, output_mint: str, amount_lamports: int,
                      slippage_bps: int = MAX_SLIPPAGE_BPS) -> Optional[dict]:
    """V41.14: Raptor /quote — Solana Tracker's DEX aggregator. Faster than Jupiter via
    Yellowstone Jet TPU on send side. Returns Jupiter-compatible dict (outAmount mapped
    from amountOut so existing code keeps working)."""
    if not RAPTOR_ENABLED:
        return None
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(slippage_bps),
    }
    try:
        r = requests.get(f"{RAPTOR_BASE}/quote", params=params, timeout=5)
        if r.status_code != 200:
            return None
        d = r.json()
        if not d or not d.get("amountOut"):
            return None
        # Normalize to Jupiter-shape so existing quote-consumers keep working unchanged
        d["outAmount"] = d.get("amountOut")
        d["otherAmountThreshold"] = d.get("minAmountOut")
        d["_source"] = "raptor"
        return d
    except (requests.Timeout, requests.ConnectionError):
        return None
    except Exception as e:
        log(f"raptor quote err: {e}")
        return None


def jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int,
                  slippage_bps: int = MAX_SLIPPAGE_BPS, retries: int = 0) -> Optional[dict]:
    """V41.14: tries Raptor first (faster, free), falls back to Jupiter on failure.
    Function name kept as jupiter_quote for compatibility with all call sites."""
    global jupiter_last_ok, jupiter_blocked_until
    # Try Raptor first — sub-second response, no rate limits on hosted endpoint
    if RAPTOR_ENABLED:
        rq = _raptor_quote_raw(input_mint, output_mint, amount_lamports, slippage_bps)
        if rq is not None:
            jupiter_last_ok = time.time()  # Raptor success counts as healthy
            return rq
    # Fallback to Jupiter
    if time.time() < jupiter_blocked_until:
        return None
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
    }
    for attempt in range(retries + 1):
        try:
            r = requests.get(JUPITER_QUOTE, params=params, timeout=6)
            if r.status_code == 200:
                jupiter_last_ok = time.time()
                d = r.json()
                if d:
                    d["_source"] = "jupiter"
                return d
            if r.status_code == 429:
                # Set global cooldown - back off ALL Jupiter calls for 60s
                jupiter_blocked_until = time.time() + 60
                log(f"jupiter rate-limited, backing off 60s")
                return None
            # Other 4xx = real error (no route, invalid mint) — don't retry
            if 400 <= r.status_code < 500:
                return None
        except (requests.Timeout, requests.ConnectionError):
            if attempt < retries:
                time.sleep(0.8)
                continue
        except Exception as e:
            log(f"jupiter quote err: {e}")
            return None
    return None


def jupiter_healthy() -> bool:
    """Probe SOL->USDC quote (always-routable). Returns True if Jupiter API responsive.
    Cached: skips probe if a successful quote happened in last 30s.
    Honors global rate-limit cooldown."""
    global jupiter_last_ok, jupiter_blocked_until
    if time.time() < jupiter_blocked_until:
        return False  # rate-limited, definitely not healthy
    if time.time() - jupiter_last_ok < 30:
        return True
    try:
        r = requests.get(JUPITER_QUOTE, params={
            "inputMint": SOL_MINT,
            "outputMint": USDC_MINT,
            "amount": "10000000",  # 0.01 SOL
            "slippageBps": "50",
            "swapMode": "ExactIn",
        }, timeout=4)
        if r.status_code == 200 and r.json().get("outAmount"):
            jupiter_last_ok = time.time()
            return True
        if r.status_code == 429:
            jupiter_blocked_until = time.time() + 60
        return False
    except Exception:
        return False


def jupiter_swap(quote: dict, user_pubkey: str) -> Optional[str]:
    """Returns base64 serialized swap transaction."""
    try:
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": PRIORITY_FEE_LAMPORTS,
            "dynamicComputeUnitLimit": True,
        }
        r = requests.post(JUPITER_SWAP, json=body, timeout=10)
        if r.status_code != 200:
            log(f"jupiter swap http {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("swapTransaction")
    except Exception as e:
        log(f"jupiter swap err: {e}")
        return None


def execute_swap(kp: Keypair, client: Client, swap_tx_b64: str, simulate_first: bool = False) -> Optional[str]:
    """Sign and broadcast a Jupiter/Raptor swap transaction. Returns signature.

    V41.17 Fix #8: when simulate_first=True, calls simulateTransaction before
    sending. If simulation reports an error, abort — saves gas on tx that would
    fail due to mid-flight slippage races. Adds ~30-50ms latency; only worth on
    entries above SIMULATE_NOTIONAL_USD_THRESHOLD."""
    try:
        raw = base64.b64decode(swap_tx_b64)
        vt = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(vt.message, [kp])
        if simulate_first:
            try:
                sim = client.simulate_transaction(signed)
                if sim.value and getattr(sim.value, "err", None):
                    log(f"  swap SIM REJECTED: {sim.value.err} — aborting send (saves gas)")
                    return None
            except Exception as e:
                # Simulation infra error — log and proceed (don't block on infra)
                log(f"  swap sim err (proceeding anyway): {type(e).__name__}: {str(e)[:120]}")
        serialized = bytes(signed)
        result = client.send_raw_transaction(serialized)
        sig = str(result.value)
        log(f"  tx sent: {sig[:16]}...")
        # Confirm
        for _ in range(30):
            status = client.get_signature_statuses([Signature.from_string(sig)])
            if status.value and status.value[0]:
                conf = status.value[0]
                if conf.err:
                    log(f"  tx FAILED: {conf.err}")
                    return None
                if conf.confirmation_status in ("confirmed", "finalized"):
                    log(f"  tx confirmed")
                    return sig
            time.sleep(1)
        log(f"  tx timeout (still pending)")
        return sig
    except Exception as e:
        log(f"  swap exec err: {e}")
        return None


def buy_token(kp: Optional[Keypair], client: Client, mint: str, amount_sol: float) -> Optional[Position]:
    log(f"BUY {mint}: {amount_sol} SOL")
    amount_lamports = int(amount_sol * 10**WSOL_DECIMALS)
    mint_pk = Pubkey.from_string(mint)
    bc_pda = derive_bc_pda(mint_pk)

    # Query Jupiter for the buy fill
    quote = jupiter_quote(SOL_MINT, mint, amount_lamports)
    out_amount = float(quote.get("outAmount", 0)) if quote else 0

    # V21.3: PAPER MODE — if Jupiter has no route (token pre-graduation), simulate using
    # bonding curve price directly. Live mode would need pump.fun direct program calls.
    if (not quote or out_amount == 0):
        if not PAPER_TRADING:
            log(f"  no quote — token not on Jupiter (live mode requires pump.fun direct, skip)")
            return None
        curve_for_sim = get_curve_state(client, bc_pda)
        if not curve_for_sim or not curve_for_sim["price"]:
            log(f"  no quote AND no curve — skip")
            return None
        # In paper mode, simulate buy at marginal curve price (slight slippage)
        sim_price = curve_for_sim["price"]
        out_amount = amount_lamports / sim_price * 0.97  # 3% slippage assumption
        log(f"  [PAPER] no Jupiter route, using curve price (sim out_amount={out_amount:.0f})")
        quote = None  # explicitly mark as no Jupiter
    try:
        info = client.get_account_info(mint_pk, commitment=Confirmed)
        decimals = int(bytes(info.value.data)[44]) if info.value else 6
    except Exception:
        decimals = 6

    # Use the on-chain bonding curve marginal price as our reference entry_price.
    # V41.13b BUG FIX: for post-graduation tokens, the bonding curve is FROZEN at
    # the graduation state. Using its price gives a stale reference far from current
    # PumpSwap/Raydium price — multiplier is always <1.0x → TP never fires.
    # If complete=True (graduated), use the actual quote-derived buy price instead.
    curve = get_curve_state(client, bc_pda)
    if curve and curve["price"] and not curve.get("complete"):
        entry_price = curve["price"]
        log(f"  entry_price from curve: {entry_price:.6e}")
    else:
        # Post-graduation OR curve read failed — use the quote we just got.
        entry_price = amount_lamports / out_amount
        reason = "post-grad" if (curve and curve.get("complete")) else "curve unavailable"
        log(f"  entry_price from quote ({reason}): {entry_price:.6e}")

    if PAPER_TRADING:
        log(f"  [PAPER] would buy: {out_amount/10**decimals:,.0f} tokens @ {entry_price:.6e}")
        return Position(mint=mint, entry_price=entry_price, entry_amount_sol=amount_sol,
                        token_amount=out_amount, open_time=time.time(),
                        bc_pda=bc_pda)

    if not kp:
        log(f"  ERR: live mode but no keypair")
        return None
    # V41.17 Fix #10: prefer Raptor /swap when the quote came from Raptor —
    # auto-tracks pump.fun program upgrades (Apr-2026 17→18 account upgrade
    # silently broke raw-ix bots). Fall back to Jupiter if Raptor swap-build fails.
    swap_tx = None
    if quote and quote.get("_source") == "raptor":
        swap_tx = raptor_swap_build(quote, str(kp.pubkey()))
    if not swap_tx:
        swap_tx = jupiter_swap(quote, str(kp.pubkey()))
    if not swap_tx:
        return None
    # V41.17 Fix #8: pre-flight simulation for entries above SIMULATE_NOTIONAL_USD_THRESHOLD.
    # SOL ~$170 → 0.03 SOL ≈ $5. Skips simulation on tiny scout entries.
    notional_threshold_sol = SIMULATE_NOTIONAL_USD_THRESHOLD / 170.0
    should_sim = amount_sol >= notional_threshold_sol
    sig = execute_swap(kp, client, swap_tx, simulate_first=should_sim)
    if not sig:
        return None
    pos = Position(mint=mint, entry_price=entry_price,
                   entry_amount_sol=amount_sol, token_amount=out_amount,
                   open_time=time.time(), bc_pda=bc_pda)
    log(f"  ENTERED @ {entry_price:.6e}, qty={out_amount/10**decimals:,.0f}")
    return pos


def sell_token(kp: Optional[Keypair], client: Client, pos: Position, fraction: float, current_multiplier: float = None) -> Optional[float]:
    """Sell `fraction` (0-1) of position. Returns SOL received.
    current_multiplier: actual price multiplier at time of sell (paper mode uses this)."""
    sell_qty = pos.token_amount * pos.remaining_pct * fraction
    log(f"SELL {pos.mint}: {fraction*100:.0f}% of remaining ({sell_qty:.0f} tokens)")
    if PAPER_TRADING:
        # V41.13d FIX: query Jupiter for the ACTUAL full-size sell quote so paper mode
        # reflects real slippage. The 1%-probe quote used for trigger detection can be
        # wildly inflated for low-liq pools (saw 2394x on $5.6k pool — pure quote artifact).
        # The full-size quote applies real price impact.
        sell_qty_int = max(int(sell_qty), 1)
        quote = jupiter_quote(pos.mint, SOL_MINT, sell_qty_int)
        if quote and float(quote.get("outAmount", 0)) > 0:
            # outAmount is in lamports (SOL_MINT decimals = 9)
            sol_recv = float(quote["outAmount"]) / 10**WSOL_DECIMALS
            actual_mult = sol_recv / (pos.entry_amount_sol * pos.remaining_pct * fraction) if pos.entry_amount_sol > 0 else 1.0
            log(f"  [PAPER] simulated sell via full-quote: {sol_recv:.4f} SOL (actual mult={actual_mult:.2f}x)")
            return sol_recv
        # Fallback: multiplier-based when no quote available (rare)
        m = current_multiplier if current_multiplier is not None else pos.peak_price
        sol_recv = pos.entry_amount_sol * pos.remaining_pct * fraction * m
        log(f"  [PAPER] simulated sell (fallback multiplier {m:.2f}x): {sol_recv:.4f} SOL")
        return sol_recv

    sell_amount = int(sell_qty)
    quote = jupiter_quote(pos.mint, SOL_MINT, sell_amount)
    if not quote:
        log(f"  no sell quote — possible honeypot")
        return None
    sol_out_lamports = float(quote.get("outAmount", 0))
    if sol_out_lamports == 0:
        log(f"  zero SOL out — honeypot?")
        return None
    swap_tx = jupiter_swap(quote, str(kp.pubkey()))
    if not swap_tx:
        return None
    sig = execute_swap(kp, client, swap_tx)
    if not sig:
        return None
    sol_recv = sol_out_lamports / 10**WSOL_DECIMALS
    log(f"  RECEIVED {sol_recv:.4f} SOL")
    return sol_recv


# === DETECTION ===
async def monitor_pump_fun(client: Client, kp: Optional[Keypair]):
    """Subscribe to pump.fun program logs for new token mints."""
    log(f"Connecting to {SOLANA_WS_URL}")
    # Map of subscription id -> meaning (so we can route messages)
    sub_id_map: dict[int, str] = {}
    while True:
        try:
            async with websockets.connect(SOLANA_WS_URL, ping_interval=10, ping_timeout=20,
                                            close_timeout=10) as ws:
                # 1. Send pump.fun subscription
                req = {
                    "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                    "params": [
                        {"mentions": [str(PUMP_PROGRAM)]},
                        {"commitment": "processed"},
                    ],
                }
                await ws.send(json.dumps(req))

                pending_acks = {1: "pump"}

                # V41.7: bonk.fun (Raydium LaunchLab) subscription — parallel stream for
                # bonk migrations. Bonk migrates to Raydium V4/CPMM via migrate_to_amm /
                # migrate_to_cpswap instructions.
                if BONK_ENABLED:
                    bonk_req = {
                        "jsonrpc": "2.0", "id": 2, "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(BONK_PROGRAM)]},
                            {"commitment": "processed"},
                        ],
                    }
                    await ws.send(json.dumps(bonk_req))
                    pending_acks[2] = "bonk"

                # 2. Send all smart wallet subscriptions
                for i, (wallet, name) in enumerate(SMART_WALLETS.items()):
                    rid = 100 + i
                    sw_req = {
                        "jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                        "params": [
                            {"mentions": [wallet]},
                            {"commitment": "processed"},
                        ],
                    }
                    await ws.send(json.dumps(sw_req))
                    pending_acks[rid] = f"smart:{wallet}"
                log(f"Sent {len(pending_acks)} subscription requests; awaiting acks...")

                # Drain ACKs and route updates correctly
                ack_count = 0

                async for msg in ws:
                    try:
                        data = json.loads(msg)

                        # Handle ACK responses (pending subscriptions)
                        if "id" in data and "result" in data and "method" not in data:
                            rid = data["id"]
                            if rid in pending_acks:
                                sub_id_map[data["result"]] = pending_acks[rid]
                                ack_count += 1
                                if ack_count == len(pending_acks):
                                    log(f"All {ack_count} subscriptions confirmed (incl. {len(SMART_WALLETS)} smart wallets)")
                            continue

                        params = data.get("params", {})
                        sub_id = params.get("subscription")
                        sub_type = sub_id_map.get(sub_id, "")
                        result = params.get("result", {})
                        value = result.get("value", {})
                        sig = value.get("signature")
                        logs = value.get("logs", []) or []

                        # === V17: SMART WALLET BUY DETECTION ===
                        # === V32: ALSO detect smart-wallet SELLS — early exit signal for held positions ===
                        if sub_type.startswith("smart:"):
                            wallet = sub_type[6:]
                            is_buy = any("Program log: Instruction: Buy" in l for l in logs)
                            is_sell = any("Program log: Instruction: Sell" in l for l in logs)
                            mentions_pump = any(str(PUMP_PROGRAM) in l for l in logs) or \
                                            any("pump" in l.lower() for l in logs)
                            if mentions_pump and sig:
                                if is_buy:
                                    asyncio.create_task(handle_smart_wallet_buy(client, kp, wallet, sig))
                                elif is_sell:
                                    asyncio.create_task(handle_smart_wallet_sell(client, kp, wallet, sig))
                            continue

                        # === V41.7/9: BONK STREAM HANDLERS ===
                        # bonk.fun (Raydium LaunchLab) emits:
                        #   - 'initialize_v2' on new mint creation (V41.9: pre-grad sniping)
                        #   - 'migrate_to_amm' / 'migrate_to_cpswap' on graduation
                        # Both are routable via Jupiter (LaunchLab → Raydium AMM/CPMM).
                        # Source: chainstacklabs/letsbonk-fun-bot, bitquery LetsBonk docs.
                        if sub_type == "bonk":
                            is_bonk_init = any(
                                "Program log: Instruction: initialize_v2" in l
                                for l in logs
                            )
                            is_bonk_migrate = any(
                                ("Program log: Instruction: migrate_to_amm" in l) or
                                ("Program log: Instruction: migrate_to_cpswap" in l)
                                for l in logs
                            )
                            if (is_bonk_init or is_bonk_migrate) and sig:
                                try:
                                    tx = client.get_transaction(Signature.from_string(sig),
                                                                  max_supported_transaction_version=0,
                                                                  commitment=Confirmed)
                                    if tx.value and tx.value.transaction:
                                        keys = tx.value.transaction.transaction.message.account_keys
                                        bonk_mint = None
                                        for k in keys:
                                            ks = str(k)
                                            ks_lc = ks.lower()
                                            if ks_lc.endswith("bonk"):
                                                bonk_mint = ks
                                                break
                                        if bonk_mint and bonk_mint not in graduated_seen:
                                            graduated_seen.add(bonk_mint)
                                            if len(graduated_seen) > 500:
                                                graduated_seen.clear()
                                            if is_bonk_init:
                                                log(f"*** BONK NEW MINT *** {bonk_mint} (initialize_v2, sig={sig[:16]})")
                                                asyncio.create_task(graduation_snipe(client, kp, bonk_mint, launchpad="bonk_pregrad"))
                                            else:
                                                log(f"*** BONK GRADUATION DETECTED *** {bonk_mint} migrated to Raydium (sig={sig[:16]})")
                                                asyncio.create_task(graduation_snipe(client, kp, bonk_mint, launchpad="bonk"))
                                except Exception as e:
                                    log(f"  bonk parse err: {e}")
                            continue

                        # === V41: GRADUATION DETECTION (PumpSwap migration) ===
                        # When a token's bonding curve completes (100% / $69k MC), pump.fun
                        # emits "Instruction: Migrate" in the same program log stream. This
                        # triggers the post-graduation sniper — token has already survived
                        # the 99.5% rug filter, PumpSwap pool just opened.
                        is_migrate = any("Program log: Instruction: Migrate" in l for l in logs)
                        if is_migrate and sig:
                            try:
                                tx = client.get_transaction(Signature.from_string(sig),
                                                              max_supported_transaction_version=0,
                                                              commitment=Confirmed)
                                if tx.value and tx.value.transaction:
                                    keys = tx.value.transaction.transaction.message.account_keys
                                    # In migrate tx, the mint being graduated is one of the accounts
                                    # Iterate to find pump-suffix mint
                                    grad_mint = None
                                    for k in keys:
                                        ks = str(k)
                                        ks_lc = ks.lower()
                                        if ks_lc.endswith("pump") or ks_lc.endswith("bonk"):
                                            grad_mint = ks
                                            break
                                    if grad_mint and grad_mint not in graduated_seen:
                                        graduated_seen.add(grad_mint)
                                        if len(graduated_seen) > 500:
                                            graduated_seen.clear()
                                        log(f"*** GRADUATION DETECTED *** {grad_mint} migrated to PumpSwap (sig={sig[:16]})")
                                        asyncio.create_task(graduation_snipe(client, kp, grad_mint))
                            except Exception as e:
                                log(f"  migrate parse err: {e}")
                            continue

                        # Pump.fun stream — original logic
                        # Look for create instruction (new mint)
                        is_create = any("Program log: Instruction: Create" in l for l in logs)
                        if not is_create:
                            continue
                        # Find mint pubkey in logs (some bots emit it; otherwise need tx parse)
                        mint = None
                        for line in logs:
                            if "Mint:" in line:
                                parts = line.split("Mint:")
                                if len(parts) > 1:
                                    candidate = parts[1].strip().split()[0]
                                    if len(candidate) >= 32:
                                        mint = candidate
                                        break
                        if not mint:
                            # Fall back to fetching the transaction
                            try:
                                tx = client.get_transaction(Signature.from_string(sig),
                                                              max_supported_transaction_version=0,
                                                              commitment=Confirmed)
                                if tx.value and tx.value.transaction:
                                    keys = tx.value.transaction.transaction.message.account_keys
                                    # Pump.fun create has mint at index 0 typically
                                    if len(keys) > 1:
                                        mint = str(keys[1])
                            except Exception:
                                pass
                        if not mint or mint in last_seen_mints:
                            continue
                        last_seen_mints.add(mint)
                        if len(last_seen_mints) > 1000:
                            last_seen_mints.clear()
                        # Filter: pump.fun token mints typically have "pump" or "bonk" suffix
                        # (vanity addresses generated by devs). Skip everything else
                        # to avoid wasting safety-check API calls on dev/curve addresses.
                        mint_lc = mint.lower()
                        if not (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                            continue
                        log(f"NEW MINT (vanity): {mint}  sig={sig[:16] if sig else '?'}")
                        # Spawn evaluator
                        asyncio.create_task(evaluate_and_snipe(client, kp, mint))
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        log(f"msg parse err: {e}")
        except Exception as e:
            log(f"WS disconnected: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


async def evaluate_and_snipe(client: Client, kp: Optional[Keypair], mint: str):
    """V38 cold-mint entry: aggressive, but not blind.

    The old cold path silently returned when Jupiter had no route, which meant the
    paper-mode bonding-curve simulator rarely got used. V38 fixes that and turns
    weak/noisy setups into scalp-mode candidates instead of blanket skips.
    """
    global session_pnl_sol, consec_losses
    try:
        if session_pnl_sol <= -MAX_SESSION_LOSS_SOL: return
        if consec_losses >= MAX_CONSEC_LOSSES: return
        if len(positions) >= MAX_CONCURRENT_POSITIONS: return
        if mint in positions: return
        if mint in cobuy_fired: return

        amount_lamports = int(SNIPE_AMOUNT_SOL * 10**WSOL_DECIMALS)
        await asyncio.sleep(EVAL_WAIT_SEC)

        # Structural safety first. Do not bypass these; authority rugs are not alpha.
        try:
            mint_pk = Pubkey.from_string(mint)
            info = client.get_account_info(mint_pk, commitment=Confirmed)
            if not info.value:
                return
            data = bytes(info.value.data)
            if len(data) < 82: return
            owner = str(info.value.owner)
            if owner not in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                             "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
                return
            mint_auth = int.from_bytes(data[0:4], "little")
            freeze_auth = int.from_bytes(data[46:50], "little")
            if mint_auth == 1 or freeze_auth == 1:
                log(f"  {mint[:8]}: authority NOT renounced")
                return
        except Exception:
            return

        bc_pda = derive_bc_pda(mint_pk)
        curve = get_curve_state(client, bc_pda, fast=True)
        if not curve or not curve.get("price"):
            log(f"  {mint[:8]}: no bonding curve state")
            return
        curve_progress = curve_progress_from_state(curve)
        if curve_progress is None:
            return
        log(f"  {mint[:8]}: bonding curve progress {curve_progress*100:.1f}%")

        late_scalp = False
        strategy = "momentum"
        quality_score = 0

        # Too early is not an instant skip; give the curve a few seconds to ignite.
        if curve_progress < MIN_CURVE_PROGRESS:
            c0 = curve
            await asyncio.sleep(3)
            c1 = get_curve_state(client, bc_pda, fast=True)
            if c1 and c1.get("price"):
                p0 = curve_progress_from_state(c0) or curve_progress
                p1 = curve_progress_from_state(c1) or p0
                if p1 > p0:
                    curve = c1
                    curve_progress = p1
                    quality_score += 1
                    log(f"  {mint[:8]}: early ignition ({p0*100:.2f}% -> {p1*100:.2f}%)")
                else:
                    log(f"  {mint[:8]}: too quiet, no ignition yet")
                    return

        # Late cold mints are not blanket skips; they can be taken as breakout scalps.
        if curve_progress >= COBUY_HARD_NO_RUNWAY:
            log(f"  {mint[:8]}: too late ({curve_progress*100:.1f}% > 90%), no runway")
            return
        if curve_progress > MAX_CURVE_PROGRESS:
            ok, curve2, flow_late = await wait_for_late_breakout(client, bc_pda, mint, curve_progress)
            if not ok:
                return
            late_scalp = True
            strategy = "late_breakout"
            quality_score += 1
            if curve2 and curve2.get("price"):
                curve = curve2
                curve_progress = curve_progress_from_state(curve2) or curve_progress

        # Jupiter checks: in paper mode, lack of Jupiter route should not block
        # pre-graduation curve simulation. In live mode, we still cannot execute
        # pre-Jupiter pump.fun buys with this file alone.
        round_trip = 0.94
        quote = jupiter_quote(SOL_MINT, mint, amount_lamports)
        if quote and float(quote.get("outAmount", 0)) > 0:
            out_amount = float(quote.get("outAmount", 0))
            sell_quote = jupiter_quote(mint, SOL_MINT, int(out_amount), slippage_bps=2000)
            if sell_quote and float(sell_quote.get("outAmount", 0)) > 0:
                sell_back = float(sell_quote.get("outAmount", 0)) / 10**WSOL_DECIMALS
                round_trip = sell_back / SNIPE_AMOUNT_SOL
                if round_trip >= 0.80:
                    quality_score += 1
                else:
                    log(f"  {mint[:8]}: bad round-trip/tax {(1-round_trip)*100:.0f}%, scalp-only")
                    late_scalp = True
                    strategy = "weak_scalp"
            else:
                log(f"  {mint[:8]}: no sell quote; scalp-only in paper, skip in live")
                if not PAPER_TRADING:
                    return
                late_scalp = True
                strategy = "weak_scalp"
        else:
            if not PAPER_TRADING:
                log(f"  {mint[:8]}: no Jupiter route; live mode cannot buy this pre-graduation token")
                return
            if FORCE_PAPER_CURVE_ENTRY:
                log(f"  {mint[:8]}: [PAPER] no Jupiter route — using on-chain curve simulation")
                quality_score += 1
            else:
                return

        # Holder count is now a quality signal, not a hard skip. Very early winners
        # often have few indexed holders before the pump becomes obvious.
        real_holders = 0
        try:
            largest = client.get_token_largest_accounts(mint_pk, commitment=Confirmed)
            if largest.value:
                supply = client.get_token_supply(mint_pk, commitment=Confirmed).value
                if supply:
                    threshold = float(supply.amount) * 0.0001
                    real_holders = sum(1 for acc in largest.value if float(acc.amount.amount) > threshold)
                    if real_holders >= 5:
                        quality_score += 1
                    else:
                        log(f"  {mint[:8]}: only {real_holders} holders indexed — scalp bias, not hard skip")
                        late_scalp = True
        except Exception:
            pass

        # Free on-chain flow: use it for scoring and dump-rebound conversion.
        flow = read_curve_flow(client, bc_pda, limit=30, seconds=60)
        if not flow.get("err"):
            total_dir = flow["buys"] + flow["sells"]
            buy_ratio = (flow["buys"] / total_dir) if total_dir else 0.0
            if flow["unique_buyers"] >= MIN_UNIQUE_BUYERS_60S:
                quality_score += 1
            else:
                late_scalp = True
                log(f"  {mint[:8]}: weak unique buyers ({flow['unique_buyers']}); scalp bias")
            if total_dir and buy_ratio >= MIN_BUY_RATIO_RECENT:
                quality_score += 1
            elif total_dir:
                log(f"  {mint[:8]}: sell-heavy flow {flow['buys']}B/{flow['sells']}S — looking for rebound")

        # Serial-rug devs are a hard no. That is not alpha; that is donating.
        try:
            if not check_dev_reputation(client, mint_pk):
                log(f"  {mint[:8]}: dev wallet has bad rug history, skip")
                return
        except Exception as e:
            log(f"  {mint[:8]} dev rep check err: {e}")

        # Race-condition fix before final checks.
        if mint in positions:
            return
        if mint in cobuy_fired:
            return

        # If active dumping exists, do not skip immediately: convert to rebound scalp.
        ok, is_rebound, flow2, curve2 = await wait_for_dump_rebound(client, bc_pda, mint)
        if not ok:
            if not (PAPER_TRADING and PAPER_SCOUT_EVERY_VALID_MINT):
                return
            log(f"  {mint[:8]}: dump did not rebound — paper micro-probe instead of skip")
            late_scalp = True
            strategy = "micro_probe"
            quality_score -= 2
        if curve2 and curve2.get("price"):
            curve = curve2
            curve_progress = curve_progress_from_state(curve2) or curve_progress
        if is_rebound:
            late_scalp = True
            strategy = "dump_rebound"
            quality_score += 1

        # Buyer history is a strong quality signal, but V38 no longer makes it a
        # universal skip. Weak buyers force scalp mode.
        buyer_ok = check_buyer_history(client, bc_pda, mint)
        if buyer_ok:
            quality_score += 2
        else:
            late_scalp = True
            if strategy == "momentum":
                strategy = "weak_scalp"

        # Momentum confirmation: lower threshold. If weak but buyers are present,
        # enter scalp instead of skipping everything.
        try:
            curve_pre = get_curve_state(client, bc_pda, fast=True)
            if not curve_pre or not curve_pre.get("price"):
                return
            progress_pre = curve_progress_from_state(curve_pre) or curve_progress
            price_pre = float(curve_pre["price"])
            await asyncio.sleep(3)
            if mint in positions: return
            curve_post = get_curve_state(client, bc_pda, fast=True)
            if not curve_post or not curve_post.get("price"):
                log(f"  {mint[:8]}: skip (curve unreadable post-3s)")
                return
            progress_post = curve_progress_from_state(curve_post) or progress_pre
            price_post = float(curve_post["price"])
            growth = progress_post - progress_pre
            price_growth = (price_post / price_pre) - 1.0 if price_pre else 0.0
            unique_buyers = count_unique_buyers(client, bc_pda, lookback=5)
            if growth >= MIN_MOMENTUM_GROWTH_3S or price_growth >= 0.006:
                quality_score += 2
                log(f"  {mint[:8]}: momentum OK (+{growth*100:.2f}% curve, +{price_growth*100:.2f}% price, {unique_buyers} unique buyers)")
            else:
                flow3 = read_curve_flow(client, bc_pda, limit=8, seconds=20)
                if flow3.get("buys", 0) >= 2 and flow3.get("buys", 0) >= flow3.get("sells", 0):
                    late_scalp = True
                    if strategy == "momentum":
                        strategy = "weak_scalp"
                    log(f"  {mint[:8]}: weak momentum but buyers absorbing ({flow3['buys']}B/{flow3['sells']}S) — scalp entry")
                else:
                    if PAPER_TRADING and PAPER_SCOUT_EVERY_VALID_MINT:
                        late_scalp = True
                        strategy = "micro_probe"
                        quality_score -= 2
                        log(f"  {mint[:8]}: no momentum/no absorption — paper micro-probe only")
                    else:
                        log(f"  {mint[:8]}: no momentum and no buyer absorption — skip dead mint")
                        return
            if 0 <= unique_buyers < 2:
                late_scalp = True
                if strategy == "momentum":
                    strategy = "weak_scalp"
                log(f"  {mint[:8]}: low unique buyers ({unique_buyers}); scalp-only")
            curve = curve_post
            curve_progress = progress_post
        except Exception as e:
            log(f"  {mint[:8]}: momentum check err {e} — proceeding cautiously")
            late_scalp = True
            if strategy == "momentum":
                strategy = "weak_scalp"

        cashback_tag = " [CASHBACK]" if curve.get("cashback") else ""
        amount_sol = choose_entry_amount(strategy, late_scalp, quality_score)
        if amount_sol <= 0:
            log(f"  {mint[:8]}: NO ENTRY strategy={strategy} score={quality_score}")
            return
        log(f"  {mint[:8]}: ENTRY{cashback_tag} strategy={strategy} scalp={late_scalp} score={quality_score} amount={amount_sol:.4f} SOL holders={real_holders} rt={round_trip*100:.0f}%")
        pos = buy_token(kp, client, mint, amount_sol)
        if pos:
            pos.late_scalp = late_scalp
            pos.strategy = strategy
            pos.entry_progress = curve_progress
            pos.quality_score = quality_score
            pos.entry_size_sol = amount_sol
            positions[mint] = pos
            asyncio.create_task(manage_position(client, kp, pos))
    except Exception as e:
        log(f"  eval err for {mint[:8]}: {e}")


async def manage_position(client: Client, kp: Optional[Keypair], pos: Position):
    """V38 position manager.

    Fixes two mechanical leaks:
      1) sell failures no longer delete positions from memory;
      2) TP ladder no longer kills the moonbag at the final rung when TP_RUNNER_MODE=True.
    """
    global session_pnl_sol, session_wins, session_losses, consec_losses
    log(f"Managing {pos.mint[:8]} entry_px={pos.entry_price:.6e} strategy={pos.strategy} scalp={pos.late_scalp} score={pos.quality_score}")
    closed = False
    close_reason = ""
    dead_reads = 0
    sell_failures = 0

    def current_mult_from_last(default: float = 1.0) -> float:
        return (pos.last_price / pos.entry_price) if pos.last_price > 0 else default

    def try_sell_fraction(reason: str, fraction: float, multiplier: float) -> bool:
        nonlocal close_reason, sell_failures, closed
        if pos.remaining_pct <= 0.01:
            close_reason = reason
            closed = True
            return True
        sol_recv = sell_token(kp, client, pos, fraction, current_multiplier=multiplier)
        if not safe_record_sell(pos, sol_recv):
            sell_failures += 1
            log(f"  SELL FAILED but position kept open ({reason}); failures={sell_failures}")
            return False
        pos.remaining_pct *= (1 - fraction)
        sell_failures = 0
        close_reason = reason
        if fraction >= 0.999 or pos.remaining_pct <= 0.01:
            closed = True
        return True

    def runner_floor_multiplier() -> float:
        """Dynamic floor. Allows moonshots to breathe while locking achieved edge."""
        peak = max(pos.peak_price, 1.0)
        if pos.late_scalp:
            if peak >= 1.08:
                return 1.03
            if peak >= 1.04:
                return 1.00
            if peak >= 1.03:
                return 0.995
            return 1.0 + SCALP_SL_PCT
        if peak >= 10.0:
            return peak * 0.50
        if peak >= 5.0:
            return max(3.0, peak * 0.55)
        if peak >= 3.0:
            return max(2.0, peak * 0.60)
        if peak >= 2.0:
            return max(1.35, peak * 0.65)
        if peak >= 1.50:
            return 1.20
        if peak >= 1.25:
            return 1.05
        if peak >= 1.10:
            return 1.03
        if peak >= 1.08:
            return 1.00
        if peak >= 1.05:
            return 0.985
        return 1.0 + SL_PCT

    while not closed:
        try:
            # === READ CURRENT PRICE FIRST ===
            curve = get_curve_state(client, pos.bc_pda, fast=True) if pos.bc_pda else None
            current_price = None

            if curve is None:
                dead_reads += 1
                if dead_reads >= 6:
                    mult = current_mult_from_last(1.0)
                    if try_sell_fraction(f"RPC failed 6x, force-sell", 1.0, mult):
                        break
                await asyncio.sleep(2)
                continue

            if curve.get("complete") or not curve.get("price"):
                pos.graduated = True
                probe_qty = int(pos.token_amount * max(pos.remaining_pct, 0.01) * 0.01) or 1
                quote = jupiter_quote(pos.mint, SOL_MINT, probe_qty)
                if not quote or float(quote.get("outAmount", 0)) == 0:
                    dead_reads += 1
                    if dead_reads >= 6:
                        mult = current_mult_from_last(1.0)
                        if try_sell_fraction("GRADUATED + no Jupiter route", 1.0, mult):
                            break
                    await asyncio.sleep(3)
                    continue
                current_price = float(quote["outAmount"]) / probe_qty
                dead_reads = 0
            else:
                current_price = float(curve["price"])
                dead_reads = 0

            pos.last_price = current_price
            multiplier = current_price / pos.entry_price if pos.entry_price else 1.0
            if multiplier > pos.peak_price:
                pos.peak_price = multiplier

            # V39 winner-only scale-in. Never average down; only add after proof of life
            # and before TP rungs. This is how we get bigger wins without making every
            # random mint a full-size entry.
            if (SCALE_IN_ENABLED and not pos.late_scalp and pos.quality_score >= FULL_SIZE_SCORE and
                pos.rung_hit == 0 and pos.adds_done < 2 and
                pos.entry_amount_sol < MAX_POSITION_AMOUNT_SOL and
                ((pos.adds_done == 0 and pos.peak_price >= 1.10 and multiplier >= 1.04) or
                 (pos.adds_done == 1 and pos.peak_price >= 1.35 and multiplier >= 1.18))):
                add_amt = min(SCALE_IN_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL - pos.entry_amount_sol)
                if add_amt >= MICRO_SCOUT_AMOUNT_SOL:
                    log(f"  SCALE-IN {pos.mint[:8]} add={add_amt:.4f} peak={pos.peak_price:.2f}x mult={multiplier:.2f}x")
                    add_pos = buy_token(kp, client, pos.mint, add_amt)
                    if add_pos:
                        merge_position_add(pos, add_pos)
                        pos.adds_done += 1
                        multiplier = current_price / pos.entry_price if pos.entry_price else multiplier
                        log(f"  SCALE-IN DONE {pos.mint[:8]} total_cost={pos.entry_amount_sol:.4f} avg_entry={pos.entry_price:.6e}")

            elapsed = time.time() - pos.open_time

            # V32: smart-wallet sold the same token we hold -> exit. If sell fails,
            # keep managing instead of deleting the position.
            if pos.mint in smart_wallet_sold:
                smart_wallet_sold.discard(pos.mint)
                reason = f"SMART SELL EXIT (mult={multiplier:.2f}x peak={pos.peak_price:.2f}x)"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier):
                    break

            # Hard timeout recycles capital, but do NOT kill a proven runner at
            # the same timeout as a dead coin. Scalp/no-edge exits fast; runners
            # get time to become the rare outsized win.
            if pos.late_scalp:
                timeout_sec = SCALP_TIMEOUT_SEC
                timeout_label = f"{SCALP_TIMEOUT_SEC}s scalp"
            elif pos.rung_hit > 0 or pos.peak_price >= 1.50:
                timeout_sec = RUNNER_TIMEOUT_MIN * 60
                timeout_label = f"{RUNNER_TIMEOUT_MIN}min runner"
            else:
                timeout_sec = POSITION_TIMEOUT_MIN * 60
                timeout_label = f"{POSITION_TIMEOUT_MIN}min"
            if elapsed > timeout_sec:
                reason = f"HARD TIMEOUT {timeout_label}"
                log(f"  {reason}, force-sell {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier):
                    break

            # No-pump bailout. For rebound/weak scalps, be even stricter.
            early_timeout = 20 if pos.late_scalp else EARLY_DUMP_TIMEOUT_SEC
            early_peak = 1.015 if pos.late_scalp else EARLY_DUMP_PEAK_THRESHOLD
            if elapsed > early_timeout and pos.peak_price < early_peak:
                reason = f"NO-MOMENTUM EXIT ({elapsed:.0f}s, peak={pos.peak_price:.2f}x, mult={multiplier:.2f}x)"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier):
                    break

            # Race-the-dump: for low-peak positions, exit fast. For runners, exit if
            # sells become extreme AND price is already giving back hard.
            if pos.bc_pda and not pos.graduated:
                flow = read_curve_flow(client, pos.bc_pda, limit=6, seconds=20)
                sells = flow.get("sells", 0)
                buys = flow.get("buys", 0)
                if pos.peak_price < 1.15 and sells >= 3:
                    reason = f"RACE EXIT ({sells}/6 sells, mult={multiplier:.2f}x)"
                    log(f"  {reason} {pos.mint[:8]}")
                    if try_sell_fraction(reason, 1.0, multiplier):
                        break
                if pos.peak_price >= 1.50 and sells >= 4 and multiplier < pos.peak_price * 0.72:
                    reason = f"RUNNER DUMP EXIT ({buys}B/{sells}S, mult={multiplier:.2f}x peak={pos.peak_price:.2f}x)"
                    log(f"  {reason} {pos.mint[:8]}")
                    if try_sell_fraction(reason, 1.0, multiplier):
                        break

            # Flash exit for immediate bad entries. This is what keeps forced volume
            # from turning into catastrophic losses.
            if elapsed < 30 and pos.peak_price < 1.05 and multiplier < FLASH_EXIT_THRESHOLD:
                reason = f"FLASH EXIT ({elapsed:.0f}s, mult={multiplier:.2f}x, peak={pos.peak_price:.2f}x)"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier):
                    break

            # TP ladder. In runner mode, final rung does NOT close if moonbag remains.
            ladder = SCALP_TP_LADDER if pos.late_scalp else TP_LADDER
            if pos.rung_hit < len(ladder):
                trigger, sell_frac = ladder[pos.rung_hit]
                if pos.peak_price >= trigger:
                    mode_tag = "SCALP " if pos.late_scalp else ""
                    reason = f"{mode_tag}TP RUNG {pos.rung_hit+1} peak={pos.peak_price:.2f}x"
                    log(f"  {reason}: selling {sell_frac*100:.0f}% of remaining")
                    before_remaining = pos.remaining_pct
                    if try_sell_fraction(reason, sell_frac, multiplier):
                        pos.rung_hit += 1
                        if pos.remaining_pct <= 0.01:
                            close_reason = f"TP COMPLETE peak={pos.peak_price:.2f}x"
                            closed = True
                            break
                        if pos.rung_hit == len(ladder) and not TP_RUNNER_MODE:
                            # Legacy behavior: sell rest after final rung.
                            if try_sell_fraction(f"TP COMPLETE final exit peak={pos.peak_price:.2f}x", 1.0, multiplier):
                                break
                        elif pos.rung_hit == len(ladder) and TP_RUNNER_MODE:
                            log(f"  RUNNER MODE: keeping {pos.remaining_pct*100:.1f}% moonbag alive after ladder")
                    else:
                        # sell failed; do not advance the rung
                        pos.remaining_pct = before_remaining

            # Dynamic trailing floor. Unlike the old fixed ladder, this preserves
            # 10x+ upside while preventing a 3x from round-tripping to red.
            floor = runner_floor_multiplier()
            if multiplier < floor:
                reason = f"TRAIL FLOOR EXIT mult={multiplier:.2f}x floor={floor:.2f}x peak={pos.peak_price:.2f}x"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier):
                    break

            await asyncio.sleep(1)
        except Exception as e:
            log(f"  manage err: {e}")
            await asyncio.sleep(3)

    # === Final accounting ===
    pnl = pos.realized_sol - pos.entry_amount_sol
    session_pnl_sol += pnl
    if pnl >= 0:
        session_wins += 1
        consec_losses = 0
    else:
        session_losses += 1
        consec_losses += 1
    log(f"  CLOSED {pos.mint[:8]} strategy={pos.strategy} peak={pos.peak_price:.2f}x recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} "
        f"pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} W={session_wins} L={session_losses} "
        f"reason={close_reason}")
    if pos.mint in positions:
        del positions[pos.mint]


async def session_reporter():
    """Periodic session summary so user can see PnL in realtime."""
    while True:
        await asyncio.sleep(60)
        log(f"=== SESSION: pnl={session_pnl_sol:+.4f} SOL | W={session_wins} L={session_losses} | "
            f"open={len(positions)} | dump_watch={len(dump_bounce_active)} | consec_loss={consec_losses} ===")
        # V41.15c: copy-trade pipeline diagnostics — see where shreds are dropped
        s = _copy_trade_stats
        if s["shreds"] > 0:
            log(f"=== COPY-PIPELINE: shreds={s['shreds']} sig_dedup={s['sig_dedup']} excpt={s['exception']} "
                f"no_meta={s['no_meta']} wrong_signer={s['wrong_signer']} no_buy={s['no_buy']} "
                f"non_memecoin={s['non_memecoin']} dedup={s['dedup']} rug_blocked={s['rug_blocked']} fired={s['fired']} ===")


# === MAIN ===
async def main():
    if PAPER_TRADING:
        log("=" * 60)
        log("PAPER TRADING MODE — no real transactions")
        log("Set PAPER_TRADING=False in code for live trading")
        log("=" * 60)
    else:
        log("=" * 60)
        log("LIVE TRADING MODE — REAL MONEY AT RISK")
        log("=" * 60)
    kp = load_keypair()
    if not PAPER_TRADING and not kp:
        log("FATAL: live mode requires SOLANA_PRIVATE_KEY env var")
        return
    client = Client(SOLANA_RPC_URL, commitment=Confirmed)
    # Verify RPC
    try:
        slot = client.get_slot().value
        log(f"RPC connected, slot {slot}")
    except Exception as e:
        log(f"FATAL: RPC connection failed: {e}")
        return
    # Verify Jupiter API
    if jupiter_healthy():
        log(f"Jupiter API healthy")
    else:
        log(f"WARNING: Jupiter API not responding to health probe")
    if kp:
        try:
            balance = client.get_balance(kp.pubkey()).value / 10**9
            log(f"Balance: {balance:.4f} SOL")
            if not PAPER_TRADING and balance < 0.1:
                log(f"WARNING: low balance, snipes need at least 0.1 SOL")
        except Exception:
            pass

    log(f"Snipe size: {SNIPE_AMOUNT_SOL} SOL/trade")
    log(f"Max concurrent: {MAX_CONCURRENT_POSITIONS}")
    log(f"Session loss limit: {MAX_SESSION_LOSS_SOL} SOL")
    log(f"Mode: V38.1 attack/rebound | paper_curve={FORCE_PAPER_CURVE_ENTRY} | runner={TP_RUNNER_MODE}")
    log(f"Starting pump.fun monitor...")
    # Run reporter alongside the monitor
    asyncio.create_task(session_reporter())
    await monitor_pump_fun(client, kp)


# ==============================================================================
# V40 REAL-EDGE OVERRIDES — impulse tape + event-driven exits
# ==============================================================================
# Why this block exists:
#   V39 generated lots of paper "wins" that were actually no-movement exits.
#   Those become losses live after fees/spread. V40 removes weak micro-probes,
#   requires real SOL impulse before entry, and manages positions from a processed
#   accountSubscribe stream so TP/trail decisions use CURRENT price, not stale peak.
#
# Core design:
#   1. No paper micro-probes. If the chain does not show conviction, do not enter.
#   2. Runner predictor = SOL accumulation velocity through meaningful buys.
#      Count/unique buyers matter, but buy SOL per tx and buy-vs-sell pressure matter more.
#   3. Dump "profit" in spot = only after sell exhaustion; pure in-flight shorts do not
#      exist for pre-grad pump.fun. Rebound entries must be close to the local low, not
#      after a +25% bounce has already played out.
#   4. Scale-in only after an actual TP has realized SOL and fresh post-TP impulse remains.
#   5. Paper mode now subtracts realistic round-trip drag so break-even noise is counted
#      as a loss, not a fake win.

import contextlib

# --- V40 constants tuned for tiny capital. Use env vars if you want to stress-test. ---
PAPER_SCOUT_EVERY_VALID_MINT = False              # kill V39 break-even noise
FORCE_PAPER_CURVE_ENTRY = True
# V41.2: KILL CORE size for momentum strategy. Empirical proof high-score
# CORE-size momentum trades are catastrophic (-$1.48 across 4 trades). Score
# measures past tape, not future inventory release. Devs craft setups that
# look high-quality, then dump on the bigger position. Force all entries
# to SCOUT max so one rug doesn't wipe many small wins.
SNIPE_AMOUNT_SOL = float(os.environ.get("V41_CORE_AMOUNT_SOL", "0.05"))     # V41.8: 0.006→0.05 ($4.20)
CORE_AMOUNT_SOL = SNIPE_AMOUNT_SOL
SCOUT_AMOUNT_SOL = float(os.environ.get("V40_SCOUT_AMOUNT_SOL", "0.05"))      # V41.8: scout matches core
MICRO_SCOUT_AMOUNT_SOL = float(os.environ.get("V40_MICRO_SCOUT_AMOUNT_SOL", "0.005"))   # V41.8: was 0.001
SCALE_IN_AMOUNT_SOL = float(os.environ.get("V40_SCALE_IN_AMOUNT_SOL", "0.05"))
MAX_POSITION_AMOUNT_SOL = float(os.environ.get("V40_MAX_POSITION_AMOUNT_SOL", "0.10"))
FULL_SIZE_SCORE = int(os.environ.get("V40_FULL_SIZE_SCORE", "8"))
MAX_CONCURRENT_POSITIONS = int(os.environ.get("V40_MAX_CONCURRENT_POSITIONS", "3"))   # V41.8: 6→3 (positions are 8x larger now)
POSITION_TIMEOUT_MIN = int(os.environ.get("V40_POSITION_TIMEOUT_MIN", "8"))
RUNNER_TIMEOUT_MIN = int(os.environ.get("V40_RUNNER_TIMEOUT_MIN", "90"))
SCALP_TIMEOUT_SEC = int(os.environ.get("V40_SCALP_TIMEOUT_SEC", "75"))
SL_PCT = float(os.environ.get("V40_BASE_SL", "-0.06"))
SCALP_SL_PCT = float(os.environ.get("V40_SCALP_SL", "-0.025"))
FLASH_EXIT_THRESHOLD = float(os.environ.get("V40_FLASH_EXIT", "0.985"))
EARLY_DUMP_TIMEOUT_SEC = int(os.environ.get("V40_NO_MOMENTUM_SEC", "24"))
EARLY_DUMP_PEAK_THRESHOLD = float(os.environ.get("V40_NO_MOMENTUM_PEAK", "1.035"))
COBUY_HARD_NO_RUNWAY = float(os.environ.get("V40_HARD_NO_RUNWAY", "0.78"))
LATE_SCALP_CURVE_START = float(os.environ.get("V40_LATE_SCALP_START", "0.52"))
MAX_CURVE_PROGRESS = float(os.environ.get("V40_MAX_COLD_CURVE", "0.62"))
MIN_CURVE_PROGRESS = float(os.environ.get("V40_MIN_CURVE", "0.015"))

# V41.8: TP raised to +50% sell 100% to target $2 wins on 0.05 SOL position.
# Math: 0.05 SOL × +50% × $84/SOL = $2.10 per TP hit.
# Trade-off: misses smaller +18-30% pumps but captures only the meaningful runners.
# Scalp ladder kept at +4.5% for weak setups (small wins better than time-out 0).
TP_RUNNER_MODE = False
TP_LADDER = [
    (1.50, 1.00),   # sell 100% at +50% — bigger swing target
]
SCALP_TP_LADDER = [(1.045, 1.00)]

# V40 impulse thresholds.
IMPULSE_WINDOW_SEC = int(os.environ.get("V40_IMPULSE_WINDOW_SEC", "38"))
IMPULSE_LOOKBACK_TX = int(os.environ.get("V40_IMPULSE_LOOKBACK_TX", "18"))
MIN_IMPULSE_SCORE = int(os.environ.get("V40_MIN_IMPULSE_SCORE", "6"))
MIN_CORE_IMPULSE_SCORE = int(os.environ.get("V40_MIN_CORE_IMPULSE_SCORE", "8"))
MIN_BUY_SOL = float(os.environ.get("V40_MIN_BUY_SOL", "0.075"))
MIN_AVG_BUY_SOL = float(os.environ.get("V40_MIN_AVG_BUY_SOL", "0.012"))
MIN_BUY_PRESSURE = float(os.environ.get("V40_MIN_BUY_PRESSURE", "1.85"))
MIN_UNIQUE_IMPULSE_BUYERS = int(os.environ.get("V40_MIN_UNIQUE_IMPULSE_BUYERS", "3"))
MIN_PRICE_GROWTH_4S = float(os.environ.get("V40_MIN_PRICE_GROWTH_4S", "0.010"))
MIN_CURVE_GROWTH_4S = float(os.environ.get("V40_MIN_CURVE_GROWTH_4S", "0.0045"))
DUMP_REBOUND_WAIT_SEC = int(os.environ.get("V40_DUMP_REBOUND_WAIT_SEC", "24"))
DUMP_REBOUND_MAX_RECOVERY = float(os.environ.get("V40_DUMP_REBOUND_MAX_RECOVERY", "0.12"))
DUMP_REBOUND_MIN_RECOVERY = float(os.environ.get("V40_DUMP_REBOUND_MIN_RECOVERY", "0.018"))
PAPER_ROUND_TRIP_DRAG_BPS = int(os.environ.get("V40_PAPER_DRAG_BPS", "250"))
PAPER_FIXED_FEE_SOL = float(os.environ.get("V40_PAPER_FIXED_FEE_SOL", "0.00001"))

# Optional processed account stream: this is the free-tier mechanism that reduces
# 1-second polling gaps. It does not see pending txs; it reacts to curve account
# updates as soon as the processed websocket emits them.
USE_POSITION_ACCOUNT_STREAM = os.environ.get("V40_USE_ACCOUNT_STREAM", "1") == "1"
_curve_stream_cache: dict[str, tuple[dict, float]] = {}
_curve_stream_tasks: dict[str, asyncio.Task] = {}


def _decode_curve_bytes(data: bytes) -> Optional[dict]:
    """Decode Pump BondingCurve bytes. Layout is Anchor discriminator + u64 fields.
    This duplicates get_curve_state without an RPC call so accountSubscribe events can
    feed position management directly.
    """
    try:
        if len(data) < 49:
            return None
        virtual_token = int.from_bytes(data[8:16], "little")
        virtual_sol = int.from_bytes(data[16:24], "little")
        real_token = int.from_bytes(data[24:32], "little")
        real_sol = int.from_bytes(data[32:40], "little") if len(data) >= 40 else 0
        token_total_supply = int.from_bytes(data[40:48], "little") if len(data) >= 48 else 0
        complete = data[48] != 0
        cashback = bool(data[82] != 0) if len(data) > 82 else False
        price = (virtual_sol / virtual_token) if virtual_token else None
        return {
            "price": price,
            "virtual_sol": virtual_sol,
            "virtual_token": virtual_token,
            "real_token": real_token,
            "real_sol": real_sol,
            "token_total_supply": token_total_supply,
            "complete": complete,
            "cashback": cashback,
        }
    except Exception:
        return None


async def stream_curve_account(pos: Position):
    """Processed accountSubscribe watcher for a held bonding curve.

    The old manager polled once per second and often observed: peak 1.13x -> next
    sample 0.76x. This watcher receives account updates on every buy/sell, so the
    manager can react on the first observed curve mutation instead of waiting for
    the next polling tick. It is still not a mempool; it is the fastest free RPC
    stream available in this file.
    """
    if not USE_POSITION_ACCOUNT_STREAM or not pos.bc_pda:
        return
    mint = pos.mint
    while mint in positions:
        try:
            async with websockets.connect(SOLANA_WS_URL, ping_interval=10, ping_timeout=20, close_timeout=10) as ws:
                req = {
                    "jsonrpc": "2.0", "id": 901, "method": "accountSubscribe",
                    "params": [str(pos.bc_pda), {"encoding": "base64", "commitment": "processed"}],
                }
                await ws.send(json.dumps(req))
                log(f"  V40 stream on {mint[:8]} curve {str(pos.bc_pda)[:8]}")
                while mint in positions:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    params = msg.get("params") or {}
                    result = params.get("result") or {}
                    value = result.get("value") or {}
                    data_field = value.get("data")
                    if isinstance(data_field, list) and data_field:
                        b64 = data_field[0]
                    elif isinstance(data_field, str):
                        b64 = data_field
                    else:
                        continue
                    try:
                        curve = _decode_curve_bytes(base64.b64decode(b64))
                    except Exception:
                        curve = None
                    if curve and curve.get("price"):
                        _curve_stream_cache[mint] = (curve, time.time())
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"  V40 stream err {mint[:8]}: {e}; reconnecting")
            await asyncio.sleep(1)


def get_position_curve(client: Client, pos: Position) -> Optional[dict]:
    """Prefer fresh processed websocket state; fallback to RPC."""
    cached = _curve_stream_cache.get(pos.mint)
    if cached and time.time() - cached[1] < 2.5:
        return cached[0]
    return get_curve_state(client, pos.bc_pda, fast=True) if pos.bc_pda else None


def read_curve_tape(client: Client, bc_pda: Pubkey, limit: int = IMPULSE_LOOKBACK_TX,
                    seconds: int = IMPULSE_WINDOW_SEC) -> dict:
    """Recent curve flow with SOL size, not just tx count.

    V39 saw many "buyers" but most were tiny/noise. Research-backed predictor is
    fast SOL accumulation through meaningful buys. We approximate buy/sell SOL from
    fee-payer balance deltas. This is noisy but good enough for gating: large real
    buys create larger SOL deltas; bot churn creates many tiny deltas.
    """
    tape = {
        "buys": 0, "sells": 0, "txs": 0, "err": False,
        "buyers": set(), "sellers": set(), "unique_buyers": 0, "unique_sellers": 0,
        "buy_sol": 0.0, "sell_sol": 0.0, "max_buy_sol": 0.0,
        "avg_buy_sol": 0.0, "buy_pressure": 0.0, "whale_share": 0.0,
        "established_buyers": 0,
    }
    try:
        from solders.signature import Signature as SolSig
        sigs = client.get_signatures_for_address(bc_pda, limit=limit, commitment=Confirmed)
        if not sigs.value:
            return tape
        now_i = int(time.time())
        for s in sigs.value[:limit]:
            try:
                bt = getattr(s, "block_time", None) or 0
                if seconds and bt and bt < now_i - seconds:
                    continue
                tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                            max_supported_transaction_version=0,
                                            commitment=Confirmed)
                if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                    continue
                meta = tx.value.transaction.meta
                logs = meta.log_messages or []
                joined = "\n".join(logs)
                keys = tx.value.transaction.transaction.message.account_keys
                signer = str(keys[0]) if keys else "?"
                pre0 = int(meta.pre_balances[0]) if meta.pre_balances else 0
                post0 = int(meta.post_balances[0]) if meta.post_balances else 0
                delta_sol = (post0 - pre0) / 1e9
                if "Instruction: Buy" in joined:
                    spent = max(-delta_sol, 0.0)
                    tape["buys"] += 1
                    tape["txs"] += 1
                    tape["buyers"].add(signer)
                    tape["buy_sol"] += spent
                    tape["max_buy_sol"] = max(tape["max_buy_sol"], spent)
                elif "Instruction: Sell" in joined:
                    got = max(delta_sol, 0.0)
                    tape["sells"] += 1
                    tape["txs"] += 1
                    tape["sellers"].add(signer)
                    tape["sell_sol"] += got
            except Exception:
                continue
        tape["unique_buyers"] = len(tape["buyers"])
        tape["unique_sellers"] = len(tape["sellers"])
        tape["avg_buy_sol"] = tape["buy_sol"] / tape["buys"] if tape["buys"] else 0.0
        tape["buy_pressure"] = tape["buy_sol"] / max(tape["sell_sol"], 0.001)
        tape["whale_share"] = tape["max_buy_sol"] / tape["buy_sol"] if tape["buy_sol"] > 0 else 0.0
        # Keep this cheap: only check up to first 5 buyers, cache handles repeats.
        est = 0
        for buyer in list(tape["buyers"])[:5]:
            if buyer != "?" and is_established_wallet(client, buyer):
                est += 1
        tape["established_buyers"] = est
        return tape
    except Exception:
        tape["err"] = True
        return tape


def tape_score(tape: dict, cashback: bool = False) -> int:
    if tape.get("err"):
        return 0
    score = 0
    buy_sol = float(tape.get("buy_sol", 0.0))
    sell_sol = float(tape.get("sell_sol", 0.0))
    avg_buy = float(tape.get("avg_buy_sol", 0.0))
    buys = int(tape.get("buys", 0))
    sells = int(tape.get("sells", 0))
    unique = int(tape.get("unique_buyers", 0))
    pressure = float(tape.get("buy_pressure", 0.0))
    whale = float(tape.get("whale_share", 0.0))
    est = int(tape.get("established_buyers", 0))

    if buy_sol >= 0.20: score += 3
    elif buy_sol >= 0.12: score += 2
    elif buy_sol >= MIN_BUY_SOL: score += 1

    if avg_buy >= 0.035: score += 3
    elif avg_buy >= 0.020: score += 2
    elif avg_buy >= MIN_AVG_BUY_SOL: score += 1

    if pressure >= 4.0: score += 3
    elif pressure >= 2.5: score += 2
    elif pressure >= MIN_BUY_PRESSURE: score += 1

    if unique >= 5: score += 2
    elif unique >= MIN_UNIQUE_IMPULSE_BUYERS: score += 1

    if est >= 4: score += 2
    elif est >= 3: score += 1

    if sells == 0 and buys >= 3: score += 1
    if cashback: score += 1

    # Penalties: tiny-bot churn, sell pressure, or one-wallet pump likely to dump.
    if buys >= 8 and avg_buy < 0.010: score -= 3
    if sells >= 3 and sell_sol > buy_sol * 0.45: score -= 3
    if sell_sol > buy_sol * 0.75: score -= 4
    if whale > 0.78 and unique < 3: score -= 2
    return score


def tape_is_positive(tape: dict, min_score: int = MIN_IMPULSE_SCORE) -> bool:
    if tape.get("err"):
        return False
    return (
        tape_score(tape) >= min_score and
        float(tape.get("buy_sol", 0.0)) >= MIN_BUY_SOL and
        float(tape.get("avg_buy_sol", 0.0)) >= MIN_AVG_BUY_SOL and
        float(tape.get("buy_pressure", 0.0)) >= MIN_BUY_PRESSURE and
        int(tape.get("unique_buyers", 0)) >= MIN_UNIQUE_IMPULSE_BUYERS and
        int(tape.get("sells", 0)) <= max(2, int(tape.get("buys", 0)) // 2)
    )


async def wait_for_runner_impulse(client: Client, bc_pda: Pubkey, mint: str,
                                  base_curve: Optional[dict] = None) -> tuple[bool, Optional[dict], dict, int, dict]:
    """Confirm continuation before entry.

    This replaces V39's weak "+0.5% × 2 ticks" style confirmation. We need both:
    - curve/price expansion over a short window, and
    - meaningful SOL flow from non-fresh wallets.
    """
    c0 = base_curve or get_curve_state(client, bc_pda, fast=True)
    if not c0 or not c0.get("price"):
        return False, c0, {}, 0, {"reason": "no curve"}
    p0 = float(c0["price"])
    pr0 = curve_progress_from_state(c0) or 0.0
    await asyncio.sleep(4)
    c1 = get_curve_state(client, bc_pda, fast=True)
    if not c1 or not c1.get("price"):
        return False, c1, {}, 0, {"reason": "no curve post"}
    p1 = float(c1["price"])
    pr1 = curve_progress_from_state(c1) or pr0
    tape = read_curve_tape(client, bc_pda, limit=IMPULSE_LOOKBACK_TX, seconds=IMPULSE_WINDOW_SEC)
    score = tape_score(tape, cashback=bool(c1.get("cashback")))
    price_growth = (p1 / p0) - 1.0 if p0 else 0.0
    curve_growth = pr1 - pr0
    motion_ok = price_growth >= MIN_PRICE_GROWTH_4S or curve_growth >= MIN_CURVE_GROWTH_4S
    flow_ok = tape_is_positive(tape, min_score=MIN_IMPULSE_SCORE)
    meta = {"price_growth": price_growth, "curve_growth": curve_growth, "motion_ok": motion_ok, "flow_ok": flow_ok}
    if motion_ok and flow_ok:
        log(f"  {mint[:8]}: V40 IMPULSE OK score={score} buy={tape['buy_sol']:.3f} SOL avg={tape['avg_buy_sol']:.3f} pressure={tape['buy_pressure']:.1f} unique={tape['unique_buyers']} dP={price_growth*100:+.2f}% dC={curve_growth*100:+.2f}%")
        return True, c1, tape, score, meta
    log(f"  {mint[:8]}: no V40 impulse score={score} buy={tape.get('buy_sol',0):.3f} avg={tape.get('avg_buy_sol',0):.3f} pressure={tape.get('buy_pressure',0):.1f} unique={tape.get('unique_buyers',0)} dP={price_growth*100:+.2f}% dC={curve_growth*100:+.2f}%")
    return False, c1, tape, score, meta


def flow_is_dumping(flow: dict) -> bool:
    """V40: treat sell SOL pressure as more important than sell count."""
    if flow.get("err"):
        return False
    buys = int(flow.get("buys", 0))
    sells = int(flow.get("sells", 0))
    buy_sol = float(flow.get("buy_sol", 0.0))
    sell_sol = float(flow.get("sell_sol", 0.0))
    if sells >= 3 and sell_sol >= buy_sol * 0.35:
        return True
    return sells >= 2 and sells > buys and sell_sol >= 0.03


async def wait_for_dump_rebound(client: Client, bc_pda: Pubkey, mint: str) -> tuple[bool, bool, dict, Optional[dict]]:
    """V40 dump handling: enter near sell-exhaustion, not after the bounce is gone.

    If recovery from the local low is already >12%, the bounce is probably played
    out — V39's exact failure. The allowed entry is the first absorption impulse:
    sell pressure decays, buyers commit real SOL, and price is only 2-12% off the
    low. This is the only spot-market way to make money *after* a dump without a short.
    """
    c0 = get_curve_state(client, bc_pda, fast=True)
    tape0 = read_curve_tape(client, bc_pda, limit=12, seconds=30)
    if not c0 or not c0.get("price"):
        return False, False, tape0, c0
    if not flow_is_dumping(tape0):
        return True, False, tape0, c0
    if not DUMP_REBOUND_ENABLED:
        log(f"  SKIP {mint[:8]}: dump in progress ({tape0['sells']} sells, {tape0['sell_sol']:.3f} SOL out)")
        return False, False, tape0, c0

    log(f"  {mint[:8]}: V40 DUMP WATCH sells={tape0['sells']} sell_sol={tape0['sell_sol']:.3f}; waiting for absorption near low")
    low_price = float(c0["price"])
    best_curve = c0
    best_tape = tape0
    deadline = time.time() + DUMP_REBOUND_WAIT_SEC
    while time.time() < deadline:
        await asyncio.sleep(0.8)
        curve = get_curve_state(client, bc_pda, fast=True)
        if not curve or not curve.get("price"):
            continue
        price = float(curve["price"])
        if price < low_price:
            low_price = price
            best_curve = curve
        recovery = (price / low_price) - 1.0 if low_price else 0.0
        tape = read_curve_tape(client, bc_pda, limit=10, seconds=18)
        score = tape_score(tape, cashback=bool(curve.get("cashback")))
        absorption = (
            score >= MIN_IMPULSE_SCORE and
            tape.get("buy_sol", 0.0) >= MIN_BUY_SOL and
            tape.get("buy_pressure", 0.0) >= 2.2 and
            tape.get("unique_buyers", 0) >= MIN_UNIQUE_IMPULSE_BUYERS and
            tape.get("sells", 0) <= max(1, tape.get("buys", 0) // 2)
        )
        best_curve, best_tape = curve, tape
        if absorption and DUMP_REBOUND_MIN_RECOVERY <= recovery <= DUMP_REBOUND_MAX_RECOVERY:
            log(f"  {mint[:8]}: V40 REBOUND OK recovery={recovery*100:.1f}% score={score} buy={tape['buy_sol']:.3f} pressure={tape['buy_pressure']:.1f} {tape['buys']}B/{tape['sells']}S")
            return True, True, tape, curve
        if recovery > DUMP_REBOUND_MAX_RECOVERY and not absorption:
            log(f"  {mint[:8]}: rebound already +{recovery*100:.1f}% without absorption — do not chase")
            return False, False, tape, curve
    log(f"  SKIP {mint[:8]}: no V40 absorption rebound")
    return False, False, best_tape, best_curve


async def wait_for_late_breakout(client: Client, bc_pda: Pubkey, mint: str, progress: float) -> tuple[bool, Optional[dict], dict]:
    """V40 late entries require real flow and actual current breakout."""
    if progress >= COBUY_HARD_NO_RUNWAY:
        log(f"  SKIP {mint[:8]}: curve {progress*100:.1f}% no runway")
        return False, None, {}
    ok, curve, tape, score, meta = await wait_for_runner_impulse(client, bc_pda, mint)
    if ok and score >= MIN_CORE_IMPULSE_SCORE and tape.get("buy_pressure", 0.0) >= 2.8:
        log(f"  {mint[:8]}: V40 LATE BREAKOUT OK curve={progress*100:.1f}% score={score}")
        return True, curve, tape
    log(f"  SKIP {mint[:8]}: late signal failed V40 breakout score={score}")
    return False, curve, tape


def choose_entry_amount(strategy: str, late_scalp: bool, quality_score: int) -> float:
    """V40 sizing: no more auto-micro-probes.

    Every entry must have positive expectancy. Core size is smaller than V39 to stop
    one rug from wiping 4-8 wins. Bigger wins come from letting runners live and
    post-TP scale-ins, not from full-sizing the first candle.
    """
    if strategy in {"micro_probe", "very_late_micro"}:
        return 0.0
    if strategy in {"dump_rebound", "late_breakout", "weak_scalp"} or late_scalp:
        return SCOUT_AMOUNT_SOL if quality_score >= MIN_IMPULSE_SCORE else 0.0
    if quality_score >= MIN_CORE_IMPULSE_SCORE:
        return min(CORE_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL)
    if quality_score >= MIN_IMPULSE_SCORE:
        return min(SCOUT_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL)
    return 0.0


def sell_token(kp: Optional[Keypair], client: Client, pos: Position, fraction: float, current_multiplier: float = None) -> Optional[float]:
    """V40 sell: paper mode uses CURRENT multiplier and subtracts fee/slippage drag.

    This intentionally kills fake break-even wins. If a paper trade exits at 1.00x,
    it will be counted as a small loss, closer to live reality.
    """
    sell_qty = pos.token_amount * pos.remaining_pct * fraction
    log(f"SELL {pos.mint}: {fraction*100:.0f}% of remaining ({sell_qty:.0f} tokens)")
    if PAPER_TRADING:
        m = current_multiplier if current_multiplier is not None else (pos.last_price / pos.entry_price if pos.last_price else 1.0)
        drag = max(0.0, PAPER_ROUND_TRIP_DRAG_BPS / 10000.0)
        sol_recv = pos.entry_amount_sol * pos.remaining_pct * fraction * m * (1.0 - drag)
        sol_recv = max(0.0, sol_recv - PAPER_FIXED_FEE_SOL)
        log(f"  [PAPER V40] simulated sell mult={m:.3f} drag={drag*100:.2f}% recv={sol_recv:.5f}")
        return sol_recv

    sell_amount = int(sell_qty)
    quote = jupiter_quote(pos.mint, SOL_MINT, sell_amount, slippage_bps=MAX_SLIPPAGE_BPS, retries=1)
    if not quote:
        log(f"  no sell quote — possible honeypot/route issue")
        return None
    sol_out_lamports = float(quote.get("outAmount", 0))
    if sol_out_lamports == 0:
        log(f"  zero SOL out — honeypot?")
        return None
    if not kp:
        return None
    swap_tx = jupiter_swap(quote, str(kp.pubkey()))
    if not swap_tx:
        return None
    sig = execute_swap(kp, client, swap_tx)
    if not sig:
        return None
    sol_recv = sol_out_lamports / 10**WSOL_DECIMALS
    log(f"  RECEIVED {sol_recv:.4f} SOL")
    return sol_recv


COBUY_ENABLED = os.environ.get("V41_COBUY_ENABLED", "0") == "1"  # V41.5: disabled by default — 0/3 wins this session


async def cobuy_snipe(client: Client, kp: Optional[Keypair], mint: str, smart_names: list):
    """V40 smart-wallet path: smart buy is a lead, not enough by itself.

    V41.5: DISABLED by default. Live data showed structural late-arrival problem
    (the smart wallet's own buy IS the pump; we arrive at peak). All 3 cobuy entries
    in the prior session lost peak=1.00x. Re-enable via V41_COBUY_ENABLED=1 only
    after on-chain bundle-detection or sub-slot tx submission is added.
    """
    global session_pnl_sol, consec_losses
    try:
        if not COBUY_ENABLED:
            log(f"  V41.5 COBUY DISABLED {mint[:8]}: skipping smart-wallet copy entry")
            return
        blocked, reason = _entry_circuit_breakers_open()
        if blocked:
            log(f"  COBUY SKIP {mint[:8]}: {reason}")
            return
        if len(positions) >= MAX_CONCURRENT_POSITIONS or mint in positions:
            return
        mint_pk = Pubkey.from_string(mint)
        safe, reason = check_mint_safety(client, mint)
        if not safe:
            log(f"  CO-BUY SKIP {mint[:8]}: {reason}")
            return
        bc_pda = derive_bc_pda(mint_pk)
        curve = get_curve_state(client, bc_pda, fast=True)
        if not curve or not curve.get("price"):
            log(f"  CO-BUY SKIP {mint[:8]}: no curve state")
            return
        progress = curve_progress_from_state(curve)
        if progress is None:
            return

        strategy = "cobuy"
        late_scalp = False
        quality_score = 3

        if progress >= COBUY_HARD_NO_RUNWAY:
            log(f"  CO-BUY SKIP {mint[:8]}: curve {progress*100:.1f}% no runway")
            return
        if progress >= LATE_SCALP_CURVE_START:
            ok, curve2, tape = await wait_for_late_breakout(client, bc_pda, mint, progress)
            if not ok:
                return
            late_scalp = True
            strategy = "late_breakout"
            curve = curve2 or curve
            quality_score += tape_score(tape, cashback=bool(curve.get("cashback")))
        else:
            ok, is_rebound, tape0, curve2 = await wait_for_dump_rebound(client, bc_pda, mint)
            if not ok:
                return
            curve = curve2 or curve
            if is_rebound:
                late_scalp = True
                strategy = "dump_rebound"
            ok2, curve3, tape, score, meta = await wait_for_runner_impulse(client, bc_pda, mint, curve)
            if not ok2 and not is_rebound:
                return
            curve = curve3 or curve
            quality_score += max(score, tape_score(tape0, cashback=bool(curve.get("cashback"))))

        buyer_ok = check_buyer_history(client, bc_pda, mint)
        if buyer_ok:
            quality_score += 2
        else:
            late_scalp = True
            if strategy == "cobuy":
                strategy = "weak_scalp"
            quality_score -= 2

        amount_sol = choose_entry_amount(strategy, late_scalp, quality_score)
        if amount_sol <= 0:
            log(f"  CO-BUY NO ENTRY {mint[:8]}: strategy={strategy} score={quality_score}")
            return
        progress = curve_progress_from_state(curve) or progress
        tag = " [CASHBACK]" if curve.get("cashback") else ""
        log(f"  V40 CO-BUY ENTRY{tag} {mint[:8]} curve={progress*100:.1f}% strategy={strategy} score={quality_score} amount={amount_sol:.4f} smart={smart_names}")
        pos = buy_token(kp, client, mint, amount_sol)
        if pos:
            pos.late_scalp = late_scalp
            pos.strategy = strategy
            pos.entry_progress = progress
            pos.quality_score = quality_score
            pos.entry_size_sol = amount_sol
            positions[mint] = pos
            _record_entry_opened()
            asyncio.create_task(manage_position(client, kp, pos))
    except Exception as e:
        log(f"  V40 cobuy_snipe err for {mint[:8]}: {e}")


async def evaluate_and_snipe(client: Client, kp: Optional[Keypair], mint: str):
    """V40 cold-mint path: maintain volume by watching many mints, not by buying noise."""
    global session_pnl_sol, consec_losses
    try:
        blocked, reason = _entry_circuit_breakers_open()
        if blocked:
            log(f"  V40 EVAL HALT {mint[:8]}: {reason}")
            return
        if len(positions) >= MAX_CONCURRENT_POSITIONS or mint in positions:
            return
        await asyncio.sleep(EVAL_WAIT_SEC)
        if mint in positions or mint in cobuy_fired:
            return
        mint_pk = Pubkey.from_string(mint)
        safe, reason = check_mint_safety(client, mint)
        if not safe:
            log(f"  {mint[:8]}: safety skip {reason}")
            return
        bc_pda = derive_bc_pda(mint_pk)
        # V41.7: bundle detection — if dev pre-bought via N alts in same slot, this
        # is the exact pattern that rugged 27ybRHNq, 8QrRcgyS, DHZUcjBw, 9XFVHRMg.
        is_bundle, bundle_reason = is_bundled_launch(client, mint_pk, bc_pda)
        if is_bundle:
            log(f"  V40 BUNDLE SKIP {mint[:8]}: {bundle_reason}")
            return
        curve = get_curve_state(client, bc_pda, fast=True)
        if not curve or not curve.get("price"):
            log(f"  {mint[:8]}: no curve")
            return
        progress = curve_progress_from_state(curve)
        if progress is None:
            return
        if progress < MIN_CURVE_PROGRESS:
            # Very early is fine only if it ignites quickly; otherwise it is dead noise.
            ok, curve2, tape, score, meta = await wait_for_runner_impulse(client, bc_pda, mint, curve)
            if not ok:
                return
            curve = curve2 or curve
            progress = curve_progress_from_state(curve) or progress
        if progress >= COBUY_HARD_NO_RUNWAY:
            log(f"  {mint[:8]}: cold skip no runway {progress*100:.1f}%")
            return

        strategy = "momentum"
        late_scalp = False
        quality_score = 0

        if progress > MAX_CURVE_PROGRESS:
            ok, curve2, tape_late = await wait_for_late_breakout(client, bc_pda, mint, progress)
            if not ok:
                return
            strategy = "late_breakout"
            late_scalp = True
            curve = curve2 or curve
            quality_score += tape_score(tape_late, cashback=bool(curve.get("cashback")))
        else:
            ok, is_rebound, tape0, curve2 = await wait_for_dump_rebound(client, bc_pda, mint)
            if not ok:
                return
            curve = curve2 or curve
            if is_rebound:
                strategy = "dump_rebound"
                late_scalp = True
            ok2, curve3, tape, score, meta = await wait_for_runner_impulse(client, bc_pda, mint, curve)
            if not ok2 and not is_rebound:
                return
            curve = curve3 or curve
            quality_score += max(score, tape_score(tape0, cashback=bool(curve.get("cashback"))))

        # Jupiter route check: live cannot buy pre-grad with this file. Paper can.
        amount_lamports = int(SNIPE_AMOUNT_SOL * 10**WSOL_DECIMALS)
        round_trip = 0.94
        quote = jupiter_quote(SOL_MINT, mint, amount_lamports)
        if quote and float(quote.get("outAmount", 0)) > 0:
            out_amount = float(quote.get("outAmount", 0))
            sell_quote = jupiter_quote(mint, SOL_MINT, int(out_amount), slippage_bps=2000)
            if sell_quote and float(sell_quote.get("outAmount", 0)) > 0:
                round_trip = (float(sell_quote.get("outAmount", 0)) / 10**WSOL_DECIMALS) / SNIPE_AMOUNT_SOL
                if round_trip >= 0.84:
                    quality_score += 1
                else:
                    late_scalp = True
                    strategy = "weak_scalp"
                    quality_score -= 1
            else:
                if not PAPER_TRADING:
                    return
                late_scalp = True
                strategy = "weak_scalp"
        else:
            if not PAPER_TRADING:
                log(f"  {mint[:8]}: no Jupiter route; live mode needs direct pump.fun instructions")
                return
            quality_score += 1

        # Known serial-rug creators are hard reject.
        try:
            if not check_dev_reputation(client, mint_pk):
                log(f"  {mint[:8]}: dev wallet has bad rug history, skip")
                return
        except Exception as e:
            log(f"  {mint[:8]} dev rep check err: {e}")

        buyer_ok = check_buyer_history(client, bc_pda, mint)
        if buyer_ok:
            quality_score += 2
        else:
            late_scalp = True
            if strategy == "momentum":
                strategy = "weak_scalp"
            quality_score -= 2

        real_holders = 0
        try:
            largest = client.get_token_largest_accounts(mint_pk, commitment=Confirmed)
            if largest.value:
                supply = client.get_token_supply(mint_pk, commitment=Confirmed).value
                if supply:
                    threshold = float(supply.amount) * 0.0001
                    real_holders = sum(1 for acc in largest.value if float(acc.amount.amount) > threshold)
                    # V41.5: hard skip on too-few holders. Empirical: CwcEeBoD score=16 + CASHBACK
                    # but only 2 holders rugged. <4 holders = dev + alts pattern.
                    if real_holders < 4:
                        log(f"  V40 SKIP {mint[:8]}: too few holders ({real_holders}) — dev+alts pattern")
                        return
                    if real_holders >= 5:
                        quality_score += 1
                    elif real_holders <= 1:
                        late_scalp = True
                        quality_score -= 1
        except Exception:
            pass

        progress = curve_progress_from_state(curve) or progress
        amount_sol = choose_entry_amount(strategy, late_scalp, quality_score)
        if amount_sol <= 0:
            log(f"  {mint[:8]}: V40 NO ENTRY strategy={strategy} score={quality_score} progress={progress*100:.1f}%")
            return
        tag = " [CASHBACK]" if curve.get("cashback") else ""
        log(f"  {mint[:8]}: V40 ENTRY{tag} strategy={strategy} scalp={late_scalp} score={quality_score} amount={amount_sol:.4f} progress={progress*100:.1f}% holders={real_holders} rt={round_trip*100:.0f}%")
        pos = buy_token(kp, client, mint, amount_sol)
        if pos:
            pos.late_scalp = late_scalp
            pos.strategy = strategy
            pos.entry_progress = progress
            pos.quality_score = quality_score
            pos.entry_size_sol = amount_sol
            positions[mint] = pos
            _record_entry_opened()
            asyncio.create_task(manage_position(client, kp, pos))
    except Exception as e:
        log(f"  V40 eval err for {mint[:8]}: {e}")


async def manage_position(client: Client, kp: Optional[Keypair], pos: Position):
    """V40 manager: current-price TP, account stream, no pre-TP scale-in."""
    global session_pnl_sol, session_wins, session_losses, consec_losses
    log(f"Managing V40 {pos.mint[:8]} entry_px={pos.entry_price:.6e} strategy={pos.strategy} scalp={pos.late_scalp} score={pos.quality_score}")
    closed = False
    close_reason = ""
    sell_failures = 0
    dead_reads = 0
    last_mult = 1.0
    last_tick = time.time()

    if USE_POSITION_ACCOUNT_STREAM and pos.bc_pda and pos.mint not in _curve_stream_tasks:
        _curve_stream_tasks[pos.mint] = asyncio.create_task(stream_curve_account(pos))

    def try_sell_fraction(reason: str, fraction: float, multiplier: float) -> bool:
        nonlocal close_reason, sell_failures, closed
        if pos.remaining_pct <= 0.01:
            close_reason = reason
            closed = True
            return True
        sol_recv = sell_token(kp, client, pos, fraction, current_multiplier=multiplier)
        if not safe_record_sell(pos, sol_recv):
            sell_failures += 1
            log(f"  SELL FAILED but V40 keeps position open ({reason}); failures={sell_failures}")
            return False
        pos.remaining_pct *= (1 - fraction)
        sell_failures = 0
        close_reason = reason
        if fraction >= 0.999 or pos.remaining_pct <= 0.01:
            closed = True
        return True

    def floor_multiplier() -> float:
        peak = max(pos.peak_price, 1.0)
        if pos.late_scalp:
            if peak >= 1.10: return 1.045
            if peak >= 1.07: return 1.020
            if peak >= 1.045: return 1.000
            if peak >= 1.030: return 0.990
            return 1.0 + SCALP_SL_PCT
        if peak >= 10.0: return peak * 0.55
        if peak >= 5.0: return max(3.0, peak * 0.60)
        if peak >= 3.0: return max(2.0, peak * 0.65)
        if peak >= 2.0: return max(1.45, peak * 0.70)
        if peak >= 1.60: return 1.32
        if peak >= 1.30: return 1.12
        if peak >= 1.18: return 1.055
        if peak >= 1.10: return 1.010
        if peak >= 1.06: return 0.995
        return 1.0 + SL_PCT

    try:
        while not closed:
            curve = get_position_curve(client, pos)
            current_price = None
            if curve is None:
                dead_reads += 1
                if dead_reads >= 8:
                    if try_sell_fraction("RPC/stream failed 8x, force-sell", 1.0, last_mult):
                        break
                await asyncio.sleep(0.5)
                continue
            dead_reads = 0

            if curve.get("complete") or not curve.get("price"):
                pos.graduated = True
                probe_qty = int(pos.token_amount * max(pos.remaining_pct, 0.01) * 0.01) or 1
                quote = jupiter_quote(pos.mint, SOL_MINT, probe_qty)
                if not quote or float(quote.get("outAmount", 0)) == 0:
                    await asyncio.sleep(0.8)
                    continue
                current_price = float(quote["outAmount"]) / probe_qty
            else:
                current_price = float(curve["price"])

            pos.last_price = current_price
            multiplier = current_price / pos.entry_price if pos.entry_price else 1.0
            now = time.time()
            one_tick_drop = multiplier / last_mult - 1.0 if last_mult > 0 else 0.0
            last_mult = multiplier
            last_tick = now
            if multiplier > pos.peak_price:
                pos.peak_price = multiplier

            elapsed = now - pos.open_time

            # Smart-wallet sell: immediate all-out on first observed sell by watched wallet.
            if pos.mint in smart_wallet_sold:
                smart_wallet_sold.discard(pos.mint)
                reason = f"SMART SELL EXIT mult={multiplier:.2f}x peak={pos.peak_price:.2f}x"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier): break

            # TP uses CURRENT multiplier only. This fixes V39 peak-trigger/current-sell mismatch.
            ladder = SCALP_TP_LADDER if pos.late_scalp else TP_LADDER
            if pos.rung_hit < len(ladder):
                trigger, sell_frac = ladder[pos.rung_hit]
                if multiplier >= trigger:
                    reason = f"CURRENT TP RUNG {pos.rung_hit+1} mult={multiplier:.2f}x peak={pos.peak_price:.2f}x"
                    log(f"  {reason}: selling {sell_frac*100:.0f}% of remaining")
                    before = pos.remaining_pct
                    if try_sell_fraction(reason, sell_frac, multiplier):
                        pos.rung_hit += 1
                        if pos.remaining_pct <= 0.01:
                            close_reason = f"TP COMPLETE mult={multiplier:.2f}x"
                            closed = True
                            break
                        if pos.rung_hit == len(ladder) and TP_RUNNER_MODE:
                            log(f"  V40 RUNNER MODE: keeping {pos.remaining_pct*100:.1f}% moonbag")
                        elif pos.rung_hit == len(ladder):
                            if try_sell_fraction(f"TP final exit mult={multiplier:.2f}x", 1.0, multiplier): break
                    else:
                        pos.remaining_pct = before

            # Scale-in only after a real TP has happened and fresh impulse persists.
            if (SCALE_IN_ENABLED and not pos.late_scalp and pos.rung_hit >= 1 and pos.adds_done < 1 and
                pos.entry_amount_sol < MAX_POSITION_AMOUNT_SOL and multiplier >= 1.35 and
                multiplier >= pos.peak_price * 0.92):
                tape = read_curve_tape(client, pos.bc_pda, limit=10, seconds=14) if pos.bc_pda else {}
                score = tape_score(tape, cashback=bool(curve.get("cashback")))
                if score >= MIN_CORE_IMPULSE_SCORE and tape.get("buy_pressure", 0.0) >= 2.5 and tape.get("sells", 0) <= 1:
                    add_amt = min(SCALE_IN_AMOUNT_SOL, MAX_POSITION_AMOUNT_SOL - pos.entry_amount_sol)
                    if add_amt >= SCOUT_AMOUNT_SOL:
                        log(f"  V40 POST-TP SCALE-IN {pos.mint[:8]} add={add_amt:.4f} mult={multiplier:.2f}x score={score}")
                        add_pos = buy_token(kp, client, pos.mint, add_amt)
                        if add_pos:
                            merge_position_add(pos, add_pos)
                            pos.adds_done += 1
                            multiplier = current_price / pos.entry_price if pos.entry_price else multiplier

            # No-momentum bailout: V40 treats 1.00x exits as losses in paper, so there is
            # no reason to sit in flat tokens.
            early_timeout = 14 if pos.late_scalp else EARLY_DUMP_TIMEOUT_SEC
            early_peak = 1.018 if pos.late_scalp else EARLY_DUMP_PEAK_THRESHOLD
            if elapsed > early_timeout and pos.peak_price < early_peak:
                reason = f"NO-MOMENTUM EXIT {elapsed:.0f}s peak={pos.peak_price:.2f}x mult={multiplier:.2f}x"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier): break

            # Dump/rug gap defense. It cannot sell before the chain updates, but with
            # accountSubscribe it reacts on the first processed update.
            if pos.bc_pda and not pos.graduated:
                tape = read_curve_tape(client, pos.bc_pda, limit=6, seconds=14)
                sells = tape.get("sells", 0)
                buy_sol = tape.get("buy_sol", 0.0)
                sell_sol = tape.get("sell_sol", 0.0)
                if one_tick_drop <= -0.16 and multiplier < pos.peak_price * 0.86:
                    reason = f"GAP DUMP EXIT drop={one_tick_drop*100:.1f}% mult={multiplier:.2f}x peak={pos.peak_price:.2f}x"
                    log(f"  {reason} {pos.mint[:8]}")
                    if try_sell_fraction(reason, 1.0, multiplier): break
                if sells >= 2 and sell_sol > max(0.02, buy_sol * 0.85) and multiplier < pos.peak_price * 0.96:
                    reason = f"TAPE DUMP EXIT sell_sol={sell_sol:.3f} buy_sol={buy_sol:.3f} mult={multiplier:.2f}x"
                    log(f"  {reason} {pos.mint[:8]}")
                    if try_sell_fraction(reason, 1.0, multiplier): break

            if elapsed < 22 and pos.peak_price < 1.05 and multiplier < FLASH_EXIT_THRESHOLD:
                reason = f"FLASH EXIT {elapsed:.0f}s mult={multiplier:.2f}x peak={pos.peak_price:.2f}x"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier): break

            floor = floor_multiplier()
            if multiplier < floor:
                reason = f"TRAIL FLOOR EXIT mult={multiplier:.2f}x floor={floor:.2f}x peak={pos.peak_price:.2f}x"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier): break

            if pos.late_scalp:
                timeout_sec = SCALP_TIMEOUT_SEC
                label = f"{SCALP_TIMEOUT_SEC}s scalp"
            elif pos.rung_hit > 0 or pos.peak_price >= 1.60:
                timeout_sec = RUNNER_TIMEOUT_MIN * 60
                label = f"{RUNNER_TIMEOUT_MIN}min runner"
            else:
                timeout_sec = POSITION_TIMEOUT_MIN * 60
                label = f"{POSITION_TIMEOUT_MIN}min"
            if elapsed > timeout_sec:
                reason = f"HARD TIMEOUT {label}"
                log(f"  {reason}, force-sell {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier): break

            await asyncio.sleep(0.25 if USE_POSITION_ACCOUNT_STREAM else 0.75)
    finally:
        task = _curve_stream_tasks.pop(pos.mint, None)
        if task:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        _curve_stream_cache.pop(pos.mint, None)

    pnl = pos.realized_sol - pos.entry_amount_sol
    _record_trade_close(pnl)
    log(f"  CLOSED V40 {pos.mint[:8]} strategy={pos.strategy} peak={pos.peak_price:.2f}x recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} W={session_wins} L={session_losses} reason={close_reason}")
    positions.pop(pos.mint, None)


# V41.6: PumpPortal parallel migration detector.
# PumpPortal's free WebSocket emits migration events typically 500ms-1s faster than
# our Helius logsSubscribe. Whichever stream sees a migration first, fires the snipe;
# the graduated_seen set dedupes so the slower stream's later event is ignored.
# Source: https://pumpportal.fun/data-api/real-time/
PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
PUMPPORTAL_ENABLED = os.environ.get("PUMPPORTAL_ENABLED", "1") == "1"


async def pumpportal_migration_listener(client: Client, kp: Optional[Keypair]):
    """V41.6: parallel migration detector via PumpPortal WebSocket (free tier).
    Reconnects forever; whichever detector sees the migration first wins via graduated_seen dedupe.
    """
    if not PUMPPORTAL_ENABLED:
        log("PumpPortal listener disabled (PUMPPORTAL_ENABLED=0)")
        return
    backoff = 5
    while True:
        try:
            async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20, ping_timeout=20,
                                            close_timeout=10) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log("PumpPortal connected — subscribed to migration stream")
                backoff = 5
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    # PumpPortal emits a confirmation message first, then migration events.
                    # Migration events contain the mint address in 'mint' or 'tokenMint' field.
                    grad_mint = msg.get("mint") or msg.get("tokenMint") or msg.get("ca")
                    if not grad_mint or not isinstance(grad_mint, str) or len(grad_mint) < 32:
                        continue
                    if grad_mint in graduated_seen:
                        continue
                    graduated_seen.add(grad_mint)
                    if len(graduated_seen) > 500:
                        graduated_seen.clear()
                    sig = msg.get("signature") or msg.get("txSig") or "pumpportal"
                    log(f"*** GRADUATION DETECTED [PumpPortal] *** {grad_mint} migrated to PumpSwap (sig={str(sig)[:16]})")
                    asyncio.create_task(graduation_snipe(client, kp, grad_mint))
        except Exception as e:
            log(f"PumpPortal disconnected: {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# V41.13: rotate between 3 endpoints to broaden candidate quality.
#   - /tokens/trending?timeframe=1h — POST-grad popular tokens (Meteora/Raydium/PumpSwap)
#   - /tokens/latest — PRE-grad fresh mints (still on pump.fun bonding curve)
#   - /tokens/multi/graduated — recently-graduated tokens (POST-grad)
# Each cycle uses one endpoint, rotated, so total budget = 1 call per cycle.
# Pre-grad uses curve filter (20-80%); post-grad uses liquidity floor instead.
# V41.13e: trending and graduated produced only non-moving post-pump tokens
# (multipliers stuck at 0.98x — empirically verified via Jupiter quote).
# V41.13g: ST endpoint poll de-emphasized in favor of smart-money copy-trade.
ST_ENDPOINTS = [
    ("/tokens/latest", "latest", "pre"),
]
# V41.13g: smart-money copy-trade config.
# Verified via /top-traders/all: top 5 traders have realized PnL 1M+ SOL each.
# DfMxre4c: 21,845W / 6,951L = 75.9% WR. We mirror their buys via Helius logsSubscribe.
COPY_TRADE_ENABLED = bool(SOLANATRACKER_API_KEY) and os.environ.get("COPY_TRADE_ENABLED", "1") == "1"
COPY_TRADE_TOP_N = int(os.environ.get("COPY_TRADE_TOP_N", "100"))  # V41.15c: 60s test confirmed 100 wallets = 43 msgs/min (2x of 50). Earlier 15s test was statistically meaningless.
COPY_TRADE_REFRESH_HOURS = int(os.environ.get("COPY_TRADE_REFRESH_HOURS", "6"))
COPY_TRADE_MIN_WIN_RATE = float(os.environ.get("COPY_TRADE_MIN_WIN_RATE", "0.0"))   # WR filter dropped — even lottery-ticket traders are profitable in absolute SOL
# Post-grad tokens skip curve check; instead require real liquidity to avoid empty pools.
# Upper bound prevents entries on already-mature tokens ($1M+ liquidity won't 50% pump).
ST_MIN_LIQUIDITY_USD = float(os.environ.get("ST_MIN_LIQUIDITY_USD", "2000.0"))
ST_MAX_LIQUIDITY_USD = float(os.environ.get("ST_MAX_LIQUIDITY_USD", "500000.0"))

# === V41.17: latency + correctness fixes (mastery-doc-derived) ===
# Fix #1: pre-cached risk snapshots → 5ms hot-path lookup vs 200-400ms HTTP
RISK_CACHE_TTL_SEC = int(os.environ.get("RISK_CACHE_TTL_SEC", "30"))
RISK_CACHE_REFRESH_SEC = int(os.environ.get("RISK_CACHE_REFRESH_SEC", "15"))
# Fix #2: pre-warmed Raptor /stream/swap pool → ~10-30ms entry vs 300-500ms
WARM_POOL_SIZE = int(os.environ.get("WARM_POOL_SIZE", "50"))
WARM_SWAP_TTL_SEC = int(os.environ.get("WARM_SWAP_TTL_SEC", "15"))
WARM_POOL_ENABLED = os.environ.get("WARM_POOL_ENABLED", "1") == "1"
# Fix #3: smart-wallet exit pre-flight (overlap with quote — no added latency)
EXIT_CHECK_TIMEOUT_SEC = float(os.environ.get("EXIT_CHECK_TIMEOUT_SEC", "0.25"))
EXIT_CHECK_ENABLED = os.environ.get("EXIT_CHECK_ENABLED", "1") == "1"
# Fix #4: curve % gate at signal time
COPY_FAST_MAX_CURVE_PCT = float(os.environ.get("COPY_FAST_MAX_CURVE_PCT", "75.0"))
# Fix #5: tighter wallet allowlist filter (uses existing top-traders metrics)
COPY_TRADE_MIN_REALIZED_SOL = float(os.environ.get("COPY_TRADE_MIN_REALIZED_SOL", "5.0"))
COPY_TRADE_MIN_WIN_RATE_TIGHT = float(os.environ.get("COPY_TRADE_MIN_WIN_RATE_TIGHT", "0.50"))
# Fix #6: bundle freshness — abort if any bundle in last N seconds
BUNDLE_FRESHNESS_THRESHOLD_SEC = int(os.environ.get("BUNDLE_FRESHNESS_THRESHOLD_SEC", "60"))
# Fix #7: first-buyer holding rate gate (only for tokens >60s old)
FIRST_BUYER_MIN_HOLD_RATE = float(os.environ.get("FIRST_BUYER_MIN_HOLD_RATE", "0.30"))
FIRST_BUYER_MIN_TOKEN_AGE_SEC = int(os.environ.get("FIRST_BUYER_MIN_TOKEN_AGE_SEC", "60"))
FIRST_BUYER_CACHE_TTL_SEC = int(os.environ.get("FIRST_BUYER_CACHE_TTL_SEC", "30"))
FIRST_BUYER_GATE_ENABLED = os.environ.get("FIRST_BUYER_GATE_ENABLED", "1") == "1"
# Fix #8: simulateTransaction pre-flight for entries above $5 (live mode only)
SIMULATE_NOTIONAL_USD_THRESHOLD = float(os.environ.get("SIMULATE_NOTIONAL_USD_THRESHOLD", "5.0"))
# Fix #9: 8-second no-pump time-stop for copy_fast entries
TIME_STOP_NO_PUMP_SEC = float(os.environ.get("TIME_STOP_NO_PUMP_SEC", "8.0"))
TIME_STOP_ENABLED = os.environ.get("TIME_STOP_ENABLED", "1") == "1"

# === V41.17 STATE ===
# Risk cache: mint -> (snapshot_dict, fetched_at_epoch)
_risk_cache: dict = {}
_risk_cache_stats = {"hits": 0, "misses": 0, "stale": 0, "fills": 0, "live_fallback": 0}
# Pre-warmed swap tx cache: mint -> (swap_tx_b64, lastValidBlockHeight, fetched_at_epoch)
_warm_swap_cache: dict = {}
_warm_pool_stats = {"hits": 0, "misses": 0, "stale": 0, "subscribes": 0, "msgs_in": 0}
# First-buyer holding-rate cache: mint -> (rate, fetched_at_epoch)
_first_buyers_cache: dict = {}
# Wallets currently warm in /stream/swap (single set for all subs on the connection)
_warm_subscribed_mints: set = set()


def _st_fetch(path: str):
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}{path}",
            headers={"x-api-key": SOLANATRACKER_API_KEY},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        log(f"  ST fetch err [{path}]: HTTP {r.status_code}")
    except Exception as e:
        log(f"  ST fetch err [{path}]: {type(e).__name__}: {e}")
    return None


def _st_extract_token_list(data):
    """Some endpoints return a bare list; some wrap in {data: [...]}. Normalize."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "tokens", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


async def solanatracker_poll_latest(client: Client, kp: Optional[Keypair]):
    """V41.13: rotates between trending/latest/top-performing endpoints. Fires entries
    on tokens passing ST risk filters AND our curve-progress window."""
    if not SOLANATRACKER_ENABLED:
        log("Solana Tracker: DISABLED (missing SOLANATRACKER_API_KEY env var)")
        return
    log(f"Solana Tracker: rotating poll every {SOLANATRACKER_POLL_SEC}s across "
        f"{[item[1] for item in ST_ENDPOINTS]} "
        f"(filter: score<={ST_MAX_RISK_SCORE} bundlers<={ST_MAX_BUNDLER_PCT}% dev<={ST_MAX_DEV_PCT}% "
        f"top10<={ST_MAX_TOP10_PCT}% pre-grad curve {ST_MIN_CURVE_PCT}-{ST_MAX_CURVE_PCT}%, "
        f"post-grad liq ${ST_MIN_LIQUIDITY_USD:,.0f}-${ST_MAX_LIQUIDITY_USD:,.0f})")
    first_poll = True
    cycle_idx = 0
    while True:
        try:
            if not first_poll:
                await asyncio.sleep(SOLANATRACKER_POLL_SEC)
            else:
                await asyncio.sleep(2)
                first_poll = False
            path, label, gate_kind = ST_ENDPOINTS[cycle_idx % len(ST_ENDPOINTS)]
            cycle_idx += 1
            raw = await asyncio.to_thread(_st_fetch, path)
            tokens = _st_extract_token_list(raw)
            if not tokens:
                log(f"Solana Tracker [{label}]: empty/failed response")
                continue
            considered = passed = 0
            for token in tokens:
                try:
                    risk = token.get("risk", {}) or {}
                    score = risk.get("score", 99)
                    bundlers = risk.get("bundlers", {}) or {}
                    bundler_pct = bundlers.get("totalPercentage")
                    bundler_pct = 0.0 if bundler_pct is None else float(bundler_pct)
                    dev = risk.get("dev", {}) or {}
                    dev_pct = dev.get("percentage")
                    dev_pct = 0.0 if dev_pct is None else float(dev_pct)
                    top10 = risk.get("top10")
                    top10 = 100.0 if top10 is None else float(top10)
                    rugged = bool(risk.get("rugged", True))
                    pools = token.get("pools") or []
                    pool0 = pools[0] if pools else {}
                    cp_raw = pool0.get("curvePercentage")
                    curve_pct = 0.0 if cp_raw is None else float(cp_raw)
                    liq_usd = ((pool0.get("liquidity") or {}).get("usd") or 0.0)
                    try:
                        liq_usd = float(liq_usd)
                    except Exception:
                        liq_usd = 0.0
                    mint = (token.get("token") or {}).get("mint", "")
                    if not mint or mint in graduated_seen:
                        continue
                    # V41.13: only memecoins (pump.fun/bonk.fun launches). Trending
                    # includes JitoSOL, USDC and other infrastructure that won't pump 50%.
                    mint_lc = mint.lower()
                    if not (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                        continue
                    considered += 1
                    # Common risk filters apply to ALL endpoints
                    if rugged:
                        continue
                    if score > ST_MAX_RISK_SCORE:
                        continue
                    if bundler_pct > ST_MAX_BUNDLER_PCT:
                        continue
                    if dev_pct > ST_MAX_DEV_PCT:
                        continue
                    if top10 > ST_MAX_TOP10_PCT:
                        continue
                    # Endpoint-specific gates
                    if gate_kind == "pre":
                        # Pre-graduation tokens MUST have mid-curve momentum
                        if curve_pct < ST_MIN_CURVE_PCT or curve_pct > ST_MAX_CURVE_PCT:
                            continue
                        gate_extra = f"curve={curve_pct:.1f}%"
                    else:
                        # Post-graduation tokens — require real liquidity but cap upper end.
                        # $1M+ tokens are established and 50% pumps are rare on our position size.
                        if liq_usd < ST_MIN_LIQUIDITY_USD or liq_usd > ST_MAX_LIQUIDITY_USD:
                            continue
                        gate_extra = f"liq=${liq_usd:,.0f}"
                    graduated_seen.add(mint)
                    if len(graduated_seen) > 500:
                        graduated_seen.clear()
                    log(f"*** ST CLEAN MINT [{label}] *** {mint} score={score} "
                        f"bundlers={bundler_pct:.1f}% dev={dev_pct:.1f}% top10={top10:.1f}% {gate_extra}")
                    passed += 1
                    asyncio.create_task(graduation_snipe(client, kp, mint, launchpad="st_pump"))
                except Exception as e:
                    log(f"  ST token parse err [{label}]: {type(e).__name__}: {e}")
            log(f"Solana Tracker [{label}]: {considered} new tokens evaluated, {passed} passed filters")
        except Exception as e:
            log(f"Solana Tracker poller err: {type(e).__name__}: {e}")
            await asyncio.sleep(30)


# V41.13g: smart-money copy-trade. Subscribe to top traders via Helius (free), copy buys.
def _st_fetch_top_traders(needed: int = 25):
    """Fetch top profitable traders from Solana Tracker, paginated. Each page=25.
    Returns flat list of wallet entries. Costs 1 ST API call per 25 traders."""
    pages_needed = max(1, (needed + 24) // 25)
    all_wallets = []
    for page in range(1, pages_needed + 1):
        try:
            r = requests.get(
                f"{SOLANATRACKER_BASE}/top-traders/all/{page}",
                headers={"x-api-key": SOLANATRACKER_API_KEY},
                params={"expandPnl": "true"},
                timeout=15,
            )
            if r.status_code != 200:
                log(f"  COPY-TRADE fetch err page={page}: HTTP {r.status_code}")
                break
            data = r.json()
            wallets = data.get("wallets", []) if isinstance(data, dict) else []
            if not wallets:
                break
            all_wallets.extend(wallets)
            time.sleep(2.5)  # V41.15: 1.1→2.5s — avoid 429 from concurrent /tokens/latest poll
        except Exception as e:
            log(f"  COPY-TRADE fetch err page={page}: {type(e).__name__}: {e}")
            break
    return {"wallets": all_wallets} if all_wallets else None


_copy_trader_seen_sigs: set = set()
_copy_trade_stats = {
    "shreds": 0, "no_meta": 0, "wrong_signer": 0, "no_buy": 0,
    "non_memecoin": 0, "dedup": 0, "fired": 0, "exception": 0,
    "sig_dedup": 0, "rug_blocked": 0,
    # V41.17: new gate counters
    "curve_blocked": 0, "exit_blocked": 0, "first_buyer_blocked": 0,
    "bundle_fresh_blocked": 0, "warm_hit": 0, "warm_miss": 0,
}


def _evaluate_risk(snap: dict) -> tuple[bool, str]:
    """V41.17: apply rejection rules to a risk snapshot. Used by both cached
    and live paths. Empirically-tuned thresholds (V41.16b research):
       Rugger CFWsZSFd: 137 bundlers, 19% top10, 0% dev → -9%
       Winner GqkStXr3: 96 bundlers, 24% top10, 0% dev → +6%
       Winner CJiDhsnv: 74 bundlers, 33% top10, 10% dev → +14%
    Score=9-10 is uniform on pump.fun pre-grad — useless filter.
    Hard reject markers: rugged flag, bundler count >=100, bundlers >20%,
    top10 >50%, dev >15%, snipers >25%, fresh bundle in last 60s, freeze/mint authority."""
    risk = snap.get("risk", {}) or {}
    if bool(risk.get("rugged", False)):
        return False, "already rugged"
    bundlers = risk.get("bundlers", {}) or {}
    bcount = bundlers.get("count", 0) or 0
    bp = bundlers.get("totalPercentage")
    bp = 0.0 if bp is None else float(bp)
    if bcount >= 100:
        return False, f"{bcount} bundled wallets"
    if bp > 20.0:
        return False, f"bundlers hold {bp:.1f}% of supply"
    top10 = risk.get("top10")
    top10 = 100.0 if top10 is None else float(top10)
    if top10 > 50.0:
        return False, f"top10 {top10:.1f}%"
    dev = risk.get("dev", {}) or {}
    dev_pct = dev.get("percentage")
    dev_pct = 0.0 if dev_pct is None else float(dev_pct)
    if dev_pct > 15.0:
        return False, f"dev {dev_pct:.1f}%"
    snipers = risk.get("snipers", {}) or {}
    snipers_pct = snipers.get("totalPercentage")
    snipers_pct = 0.0 if snipers_pct is None else float(snipers_pct)
    if snipers_pct > 25.0:
        return False, f"snipers {snipers_pct:.1f}%"
    # Fix #6: bundle freshness — any bundleTime within last BUNDLE_FRESHNESS_THRESHOLD_SEC = abort
    wallets = bundlers.get("wallets", []) or []
    if wallets:
        now_ms = int(time.time() * 1000)
        max_bt = 0
        for w in wallets:
            bt = w.get("bundleTime") or 0
            if bt and bt > max_bt:
                max_bt = bt
        if max_bt:
            age_s = (now_ms - max_bt) / 1000
            if 0 <= age_s < BUNDLE_FRESHNESS_THRESHOLD_SEC:
                return False, f"fresh bundle {int(age_s)}s ago"
    return True, ""


def _build_risk_snapshot_from_token(t: dict) -> Optional[dict]:
    """Extract a compact, cache-friendly snapshot from a TokenInfo response."""
    try:
        risk = t.get("risk", {}) or {}
        pools = t.get("pools") or []
        pool0 = pools[0] if pools else {}
        snap = {
            "risk": risk,
            "curvePercentage": pool0.get("curvePercentage"),
            "complete": bool(pool0.get("complete", False)),
            "liquidity_usd": ((pool0.get("liquidity") or {}).get("usd") or 0) or 0,
            "createdAt": pool0.get("createdAt"),  # ms epoch
            "freezeAuthority": (pool0.get("security") or {}).get("freezeAuthority"),
            "mintAuthority": (pool0.get("security") or {}).get("mintAuthority"),
        }
        return snap
    except Exception:
        return None


async def refresh_risk_cache():
    """V41.17 Fix #1: pre-fetch /tokens/multi/all + /tokens/trending/5m every
    RISK_CACHE_REFRESH_SEC seconds. Hot-path lookup becomes O(1) → ~5ms vs
    200-400ms HTTP. Empirical session bug: rug-check inline added ~250ms to
    every copy_fast signal; cache eliminates that without changing semantics."""
    if not SOLANATRACKER_ENABLED:
        log("RISK CACHE: DISABLED (no SOLANATRACKER_API_KEY)")
        return
    log(f"RISK CACHE: prefetching /tokens/multi/all + /tokens/trending/5m every {RISK_CACHE_REFRESH_SEC}s")
    while True:
        try:
            # Serialize to stay under Data API 1 RPS limit (parallel gather hit 429s
            # in 30s smoke-test). 1.2s gap is conservative; refresh cycle still <2s.
            r1 = await asyncio.to_thread(_st_fetch, "/tokens/multi/all?limit=500&minCurve=20")
            await asyncio.sleep(1.2)
            r2 = await asyncio.to_thread(_st_fetch, "/tokens/trending/5m")
            results = [r1, r2]
            now_ts = time.time()
            new_count = 0
            for res in results:
                if isinstance(res, Exception) or not res:
                    continue
                if isinstance(res, dict):
                    items = []
                    for k in ("latest", "graduating", "graduated"):
                        items.extend(res.get(k, []) or [])
                elif isinstance(res, list):
                    items = res
                else:
                    items = []
                for t in items:
                    try:
                        mint = (t.get("token") or {}).get("mint", "")
                        if not mint:
                            continue
                        snap = _build_risk_snapshot_from_token(t)
                        if snap is None:
                            continue
                        _risk_cache[mint] = (snap, now_ts)
                        _risk_cache_stats["fills"] += 1
                        new_count += 1
                    except Exception:
                        continue
            # Evict entries older than 5 min to bound memory
            cutoff = now_ts - 300
            stale_keys = [m for m, (_, ts) in _risk_cache.items() if ts < cutoff]
            for m in stale_keys:
                _risk_cache.pop(m, None)
            if new_count > 0:
                hr = _risk_cache_stats["hits"]
                ms = _risk_cache_stats["misses"]
                live = _risk_cache_stats["live_fallback"]
                log(f"  RISK CACHE refresh: +{new_count} entries, {len(_risk_cache)} total, "
                    f"hits={hr} misses={ms} live={live} evicted={len(stale_keys)}")
        except Exception as e:
            log(f"refresh_risk_cache err: {type(e).__name__}: {e}")
        await asyncio.sleep(RISK_CACHE_REFRESH_SEC)


def _rug_check_with_snapshot(mint: str) -> tuple[bool, str, Optional[dict]]:
    """V41.17 Fix #1+#4+#6: cache-first risk gate. Returns (safe, reason, snapshot).
    Snapshot enables downstream curve-% gate without an extra fetch."""
    cached = _risk_cache.get(mint)
    if cached is not None:
        snap, fetched_at = cached
        age = time.time() - fetched_at
        if age < RISK_CACHE_TTL_SEC:
            _risk_cache_stats["hits"] += 1
            safe, reason = _evaluate_risk(snap)
            return safe, reason, snap
        _risk_cache_stats["stale"] += 1
    else:
        _risk_cache_stats["misses"] += 1
    # Cache miss / stale — fall back to live HTTP (preserves V41.16b behavior)
    _risk_cache_stats["live_fallback"] += 1
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/tokens/{mint}",
            headers={"x-api-key": SOLANATRACKER_API_KEY},
            timeout=1.5,
        )
        if r.status_code != 200:
            return True, f"rug-check API {r.status_code} — proceeding", None
        d = r.json()
        snap = _build_risk_snapshot_from_token(d)
        if snap is not None:
            _risk_cache[mint] = (snap, time.time())
        safe, reason = _evaluate_risk(snap or {"risk": d.get("risk", {}) or {}})
        return safe, reason, snap
    except Exception:
        return True, "rug-check timeout — proceeding", None


def _rug_check(mint: str) -> tuple[bool, str]:
    """V41.17: thin shim preserving V41.16b interface for legacy callers."""
    safe, reason, _snap = _rug_check_with_snapshot(mint)
    return safe, reason


def _curve_pct_gate(snap: Optional[dict]) -> tuple[bool, str]:
    """V41.17 Fix #4: reject pre-grad entries above COPY_FAST_MAX_CURVE_PCT.
    Smart wallet entries above 75% curve land past the structural sweet spot —
    smart wallet's own buy IS the pump and we follow into peak. Post-grad
    tokens (complete=True) skip this gate; different price dynamics apply."""
    if not snap:
        return True, ""
    if snap.get("complete"):
        return True, ""  # post-grad — different dynamics
    cp = snap.get("curvePercentage")
    if cp is None:
        return True, ""  # no data — fail-open
    try:
        cp = float(cp)
    except Exception:
        return True, ""
    if cp > COPY_FAST_MAX_CURVE_PCT:
        return False, f"curve {cp:.1f}% > {COPY_FAST_MAX_CURVE_PCT:.0f}% (past sweet spot)"
    return True, ""


def _first_buyer_holding_rate(mint: str) -> Optional[float]:
    """V41.17 Fix #7: returns fraction of first 100 buyers still holding (0.0-1.0).
    None if data unavailable. Cached for FIRST_BUYER_CACHE_TTL_SEC."""
    cached = _first_buyers_cache.get(mint)
    if cached:
        rate, fetched_at = cached
        if time.time() - fetched_at < FIRST_BUYER_CACHE_TTL_SEC:
            return rate
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/first-buyers/{mint}",
            headers={"x-api-key": SOLANATRACKER_API_KEY},
            timeout=2.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # Endpoint returns a list directly per docs; tolerate dict shapes
        if isinstance(data, list):
            buyers = data
        elif isinstance(data, dict):
            buyers = data.get("buyers") or data.get("data") or []
        else:
            buyers = []
        if not buyers:
            return None
        n = len(buyers)
        held = sum(1 for b in buyers if (b.get("holding") or 0) > 0)
        rate = held / n if n > 0 else 0.0
        _first_buyers_cache[mint] = (rate, time.time())
        # Bound cache size
        if len(_first_buyers_cache) > 500:
            oldest = min(_first_buyers_cache, key=lambda k: _first_buyers_cache[k][1])
            _first_buyers_cache.pop(oldest, None)
        return rate
    except Exception:
        return None


def _smart_wallet_still_holding_sync(signer: str, mint: str) -> bool:
    """V41.17 Fix #3: query the smart wallet's token balance for `mint`.
    Returns True if balance > 0 (still holding) OR True on RPC error (fail-OPEN —
    don't block entries on infra issues). False ONLY if confirmed zero balance.

    Uses standard getTokenAccountsByOwner with mint filter — single call, ~80-150ms.
    Runs synchronously inside asyncio.to_thread so it can race with the probe quote."""
    if not ST_RPC_HTTP:
        return True
    try:
        body = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [signer, {"mint": mint}, {"encoding": "jsonParsed"}],
        }
        r = requests.post(ST_RPC_HTTP, json=body, timeout=EXIT_CHECK_TIMEOUT_SEC)
        if r.status_code != 200:
            return True  # fail-OPEN
        d = r.json()
        accounts = ((d.get("result") or {}).get("value")) or []
        for acct in accounts:
            data = (acct.get("account") or {}).get("data") or {}
            parsed = (data.get("parsed") or {}).get("info") or {}
            ta = parsed.get("tokenAmount") or {}
            ui = ta.get("uiAmount")
            if ui is not None and float(ui) > 0:
                return True
            amt = ta.get("amount")
            if amt is not None and int(amt) > 0:
                return True
        # All accounts checked, all zero → confirmed sold
        return False
    except (requests.Timeout, requests.ConnectionError):
        return True  # fail-OPEN
    except Exception:
        return True


async def _smart_wallet_still_holding(signer: str, mint: str) -> bool:
    """Async wrapper — keeps RPC off the event loop."""
    return await asyncio.to_thread(_smart_wallet_still_holding_sync, signer, mint)


def raptor_swap_build(quote: dict, user_pubkey: str, priority: str = "VeryHigh") -> Optional[str]:
    """V41.17 Fix #10: build swap tx via Raptor /swap. Used for entries when
    the quote came from Raptor (auto-tracks pump.fun program upgrades; the
    Apr-2026 17→18 account upgrade silently broke raw-ix bots — Raptor's path
    is upgrade-resilient)."""
    if not RAPTOR_ENABLED:
        return None
    body = {
        "userPublicKey": user_pubkey,
        "quoteResponse": quote,
        "wrapUnwrapSol": True,
        "txVersion": "v0",
        "priorityFee": priority,
        "maxPriorityFee": 100_000,
    }
    try:
        r = requests.post(f"{RAPTOR_BASE}/swap", json=body, timeout=10)
        if r.status_code != 200:
            log(f"raptor swap http {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("swapTransaction")
    except (requests.Timeout, requests.ConnectionError):
        return None
    except Exception as e:
        log(f"raptor swap err: {e}")
        return None


async def warm_raptor_swap_pool(kp: Optional[Keypair]):
    """V41.17 Fix #2: maintain a WS connection to wss://raptor-beta/stream/swap
    with WARM_POOL_SIZE pre-built tx subscriptions for top trending mints.
    On copy_fast signal: if mint in cache and tx <WARM_SWAP_TTL_SEC old, sign
    and ship in 10-30ms vs 300-500ms for /quote-and-swap.

    Live mode only: /stream/swap requires userPublicKey to build the tx.
    In paper mode this still subscribes to receive the data (used for
    diagnostics / future live readiness)."""
    if not WARM_POOL_ENABLED or not RAPTOR_ENABLED:
        log("WARM SWAP POOL: disabled (config)")
        return
    if PAPER_TRADING and not kp:
        log("WARM SWAP POOL: disabled in paper mode without keypair (would need userPublicKey)")
        return
    user_pubkey = str(kp.pubkey()) if kp else None
    if not user_pubkey:
        log("WARM SWAP POOL: no keypair, cannot build txs")
        return

    raptor_ws_url = RAPTOR_BASE.replace("https://", "wss://").replace("http://", "ws://") + "/stream/swap"
    log(f"WARM SWAP POOL: connecting to {raptor_ws_url} (target {WARM_POOL_SIZE} mints)")
    sol_lamports = int(GRAD_AMOUNT_SOL * 10**WSOL_DECIMALS)

    while True:
        try:
            async with websockets.connect(raptor_ws_url, ping_interval=25, ping_timeout=20) as ws:
                # Refresh subscription set every 30s based on top trending mints
                async def refresher():
                    while True:
                        try:
                            data = await asyncio.to_thread(_st_fetch, "/tokens/trending/5m")
                            target_mints = set()
                            if isinstance(data, list):
                                for t in data[:WARM_POOL_SIZE]:
                                    mint = (t.get("token") or {}).get("mint", "")
                                    mint_lc = mint.lower()
                                    if mint and (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                                        target_mints.add(mint)
                            # Subscribe new mints
                            for mint in target_mints - _warm_subscribed_mints:
                                msg = {
                                    "type": "subscribe",
                                    "id": f"warm_{mint[:8]}",
                                    "inputMint": SOL_MINT,
                                    "outputMint": mint,
                                    "amount": sol_lamports,
                                    "userPublicKey": user_pubkey,
                                    "slippageBps": "500",
                                    "priorityFee": "VeryHigh",
                                    "maxPriorityFee": 100_000,
                                    "txVersion": "v0",
                                    "wrapUnwrapSol": True,
                                }
                                try:
                                    await ws.send(json.dumps(msg))
                                    _warm_subscribed_mints.add(mint)
                                    _warm_pool_stats["subscribes"] += 1
                                except Exception:
                                    break
                            # Unsubscribe drops
                            for mint in list(_warm_subscribed_mints - target_mints):
                                msg = {"type": "unsubscribe", "id": f"warm_{mint[:8]}"}
                                try:
                                    await ws.send(json.dumps(msg))
                                except Exception:
                                    pass
                                _warm_subscribed_mints.discard(mint)
                                _warm_swap_cache.pop(mint, None)
                        except Exception as e:
                            log(f"warm pool refresher err: {type(e).__name__}: {e}")
                        await asyncio.sleep(30)

                async def pinger():
                    while True:
                        await asyncio.sleep(25)
                        try:
                            await ws.send(json.dumps({"type": "ping"}))
                        except Exception:
                            return

                refresher_task = asyncio.create_task(refresher())
                pinger_task = asyncio.create_task(pinger())

                try:
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            _warm_pool_stats["msgs_in"] += 1
                            # Server pushes quote + pre-built swap tx per mint update
                            sub_id = data.get("id") or ""
                            tx_b64 = data.get("swapTransaction") or (data.get("data") or {}).get("swapTransaction")
                            lvbh = data.get("lastValidBlockHeight") or (data.get("data") or {}).get("lastValidBlockHeight") or 0
                            # Recover mint from sub_id ("warm_<mint8>")
                            target_mint = None
                            if sub_id.startswith("warm_"):
                                short = sub_id[5:]
                                for m in _warm_subscribed_mints:
                                    if m.startswith(short):
                                        target_mint = m
                                        break
                            # Some servers echo outputMint in the payload — prefer that
                            payload_mint = data.get("outputMint") or (data.get("data") or {}).get("outputMint")
                            if payload_mint and isinstance(payload_mint, str):
                                target_mint = payload_mint
                            if target_mint and tx_b64:
                                _warm_swap_cache[target_mint] = (tx_b64, int(lvbh) if lvbh else 0, time.time())
                        except Exception:
                            continue
                finally:
                    refresher_task.cancel()
                    pinger_task.cancel()
        except Exception as e:
            log(f"WARM SWAP POOL ws err, reconnecting in 10s: {type(e).__name__}: {e}")
            await asyncio.sleep(10)


def _consume_warm_swap_tx(mint: str) -> Optional[tuple[str, int]]:
    """V41.17 Fix #2: pop a fresh warm tx if available. Returns (tx_b64, lvbh) or None.
    Refuses tx older than WARM_SWAP_TTL_SEC."""
    cached = _warm_swap_cache.get(mint)
    if not cached:
        _warm_pool_stats["misses"] += 1
        return None
    tx_b64, lvbh, fetched_at = cached
    if time.time() - fetched_at > WARM_SWAP_TTL_SEC:
        _warm_pool_stats["misses"] += 1
        return None
    _warm_pool_stats["hits"] += 1
    return tx_b64, lvbh


async def _handle_copy_trader_tx(client: Client, kp: Optional[Keypair], sig: str, trader_set: set):
    """Parse a transaction emitted by a top trader. If they BOUGHT a token, copy-buy it.
    V41.15d: REVERTED to Confirmed — getTransaction rejects Processed with
    'Method does not support commitment below confirmed'. The Processed change
    was silently failing 100% of the time."""
    _copy_trade_stats["shreds"] += 1
    if sig in _copy_trader_seen_sigs:
        _copy_trade_stats["sig_dedup"] += 1
        return
    _copy_trader_seen_sigs.add(sig)
    if len(_copy_trader_seen_sigs) > 5000:
        _copy_trader_seen_sigs.clear()
    try:
        tx = client.get_transaction(
            Signature.from_string(sig),
            max_supported_transaction_version=0,
            commitment=Confirmed,
        )
        if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
            _copy_trade_stats["no_meta"] += 1
            return
        meta = tx.value.transaction.meta
        keys = tx.value.transaction.transaction.message.account_keys
        keys_str = [str(k) for k in keys]
        # Identify which top trader signed this tx (signer is account[0])
        signer = keys_str[0] if keys_str else None
        if signer not in trader_set:
            _copy_trade_stats["wrong_signer"] += 1
            return
        # Compare pre/post token balances for the trader's owned token accounts
        pre = {}
        post = {}
        for b in (getattr(meta, "pre_token_balances", None) or []):
            if str(b.owner) == signer:
                pre[str(b.mint)] = float(b.ui_token_amount.ui_amount or 0)
        for b in (getattr(meta, "post_token_balances", None) or []):
            if str(b.owner) == signer:
                post[str(b.mint)] = float(b.ui_token_amount.ui_amount or 0)
        sol_mint = "So11111111111111111111111111111111111111112"
        # Find token where signer's balance INCREASED (= they bought it)
        found_buy = False
        for mint, post_amt in post.items():
            if mint == sol_mint:
                continue
            pre_amt = pre.get(mint, 0)
            if post_amt > pre_amt + 1:  # +1 to filter dust/rounding
                found_buy = True
                if mint in graduated_seen:
                    _copy_trade_stats["dedup"] += 1
                    return
                # V41.13i: only copy memecoins (pump.fun/bonk.fun). Top traders also
                # buy USDC/JitoSOL/etc which won't pump 50% on our scale.
                mint_lc = mint.lower()
                if not (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                    _copy_trade_stats["non_memecoin"] += 1
                    return
                # V41.16b: claim the mint NOW (before rug-check) to prevent
                # parallel handlers from both running rug-check on the same mint.
                # Saw earlier: 2 parallel shreds → 1 blocked, 1 passed (rug-check
                # returned different values 1s apart).
                graduated_seen.add(mint)
                if len(graduated_seen) > 500:
                    graduated_seen.clear()
                # V41.17 Fix #1+#6: cache-first rug check (~5ms hot-path vs 200-400ms).
                # Bundle freshness check (#6) is integrated inside _evaluate_risk.
                safe, reason, snap = await asyncio.to_thread(_rug_check_with_snapshot, mint)
                if not safe:
                    _copy_trade_stats["rug_blocked"] += 1
                    if "fresh bundle" in (reason or ""):
                        _copy_trade_stats["bundle_fresh_blocked"] += 1
                    log(f"  COPY TRADE RUG-BLOCKED {signer[:8]} -> {mint[:8]}: {reason}")
                    return
                # V41.17 Fix #4: curve %-gate. Reject smart-wallet entries past the
                # structural sweet spot (>75% curve = smart wallet's own buy IS the pump,
                # we follow into peak). Snapshot already in hand from rug-check.
                cv_ok, cv_reason = _curve_pct_gate(snap)
                if not cv_ok:
                    _copy_trade_stats["curve_blocked"] += 1
                    log(f"  COPY TRADE CURVE-BLOCKED {signer[:8]} -> {mint[:8]}: {cv_reason}")
                    return
                # V41.17 Fix #7: first-buyer holding rate. Only apply to tokens >60s old —
                # younger tokens don't have a meaningful first-buyer signal yet.
                if FIRST_BUYER_GATE_ENABLED and snap is not None:
                    created_at_ms = snap.get("createdAt")
                    if created_at_ms:
                        try:
                            age_s = (time.time() * 1000 - float(created_at_ms)) / 1000
                        except Exception:
                            age_s = 0
                        if age_s > FIRST_BUYER_MIN_TOKEN_AGE_SEC:
                            rate = await asyncio.to_thread(_first_buyer_holding_rate, mint)
                            if rate is not None and rate < FIRST_BUYER_MIN_HOLD_RATE:
                                _copy_trade_stats["first_buyer_blocked"] += 1
                                log(f"  COPY TRADE FIRST-BUYER-BLOCKED {signer[:8]} -> {mint[:8]}: "
                                    f"holding_rate={rate*100:.0f}% (token age {int(age_s)}s)")
                                return
                _copy_trade_stats["fired"] += 1
                log(f"*** COPY TRADE *** {signer[:8]} bought {mint} (sig={sig[:16]})")
                # V41.14: copy_fast skips the 8s observation window. We had ~200ms
                # shred-detection latency advantage; observation was killing the edge.
                # V41.17 Fix #3: pass signer so graduation_snipe can verify smart wallet
                # hasn't already exited (parallelized with probe quote — no added latency).
                asyncio.create_task(graduation_snipe(client, kp, mint, launchpad="copy_fast", signer=signer))
                return
        if not found_buy:
            _copy_trade_stats["no_buy"] += 1
    except Exception as e:
        _copy_trade_stats["exception"] += 1
        # Log only first 5 to avoid spam
        if _copy_trade_stats["exception"] <= 5:
            log(f"  copy-trade tx parse err [{_copy_trade_stats['exception']}/5]: {type(e).__name__}: {str(e)[:150]}")


async def momentum_sniper(client: Client, kp: Optional[Keypair]):
    """V41.13j: poll /tokens/multi/all and snipe tokens with REAL CURRENT MOMENTUM.
    Solana Tracker pre-computes 1m/5m/15m/1h price changes in `events`. We use that
    directly — no observation phase, no Jupiter probe gates. If 5m > +5% AND 15m > +10%
    AND 5m >= 30% of 15m (i.e. recent acceleration, not stale), enter immediately."""
    if not SOLANATRACKER_ENABLED:
        return
    log("MOMENTUM-SNIPER: polling /tokens/multi/all every 30s for currently-surging tokens")
    while True:
        try:
            await asyncio.sleep(30)
            data = await asyncio.to_thread(_st_fetch, "/tokens/multi/all")
            if not data or not isinstance(data, dict):
                continue
            considered = passed = 0
            for cat in ("latest", "graduating", "graduated"):
                for t in data.get(cat, []):
                    try:
                        risk = t.get("risk", {}) or {}
                        if bool(risk.get("rugged", True)):
                            continue
                        events = t.get("events") or {}
                        p1m = (events.get("1m") or {}).get("priceChangePercentage")
                        p5 = (events.get("5m") or {}).get("priceChangePercentage")
                        p15 = (events.get("15m") or {}).get("priceChangePercentage")
                        p1h = (events.get("1h") or {}).get("priceChangePercentage")
                        if p5 is None or p15 is None or p1h is None:
                            continue
                        # V41.13k: empirical fix — entries on 7nhN7e2y/GtkUpkPL (1m=5m=15m=1h
                        # equal) all SL'd at -16% within seconds. That equality = "just-listed
                        # token, just spiked, no sustained trend." Require sustained 1h trend.
                        if p1h < 30.0:
                            continue
                        # Trend must be sustained: 1h > 5m (i.e. trend started before last 5m)
                        if p1h < p5 * 1.2:
                            continue
                        # Don't enter at the spike-top: 1m should be SMALLER than 5m (cooling)
                        if p1m is not None and p1m > p5:
                            continue
                        # Active uptrend in last 5m
                        if p5 < 5.0:
                            continue
                        if p15 < 10.0:
                            continue
                        # Skip extreme spikes (likely pump-and-dump bait)
                        if p5 > 100.0 or p15 > 200.0 or p1h > 500.0:
                            continue
                        pools = t.get("pools") or []
                        if not pools:
                            continue
                        liq = (pools[0].get("liquidity") or {}).get("usd") or 0
                        try:
                            liq = float(liq)
                        except Exception:
                            liq = 0
                        if liq < 2000 or liq > 500000:
                            continue
                        mint = (t.get("token") or {}).get("mint", "")
                        mint_lc = mint.lower()
                        if not (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                            continue
                        if mint in graduated_seen:
                            continue
                        considered += 1
                        graduated_seen.add(mint)
                        if len(graduated_seen) > 500:
                            graduated_seen.clear()
                        log(f"*** MOMENTUM-HOT *** {mint} cat={cat} 1m+{(events.get('1m') or {}).get('priceChangePercentage') or 0:.1f}% "
                            f"5m+{p5:.1f}% 15m+{p15:.1f}% 1h+{p1h or 0:.1f}% liq=${liq:,.0f} score={risk.get('score')}")
                        passed += 1
                        # launchpad="momentum" — enters immediately, no observation gate
                        asyncio.create_task(graduation_snipe(client, kp, mint, launchpad="momentum"))
                    except Exception as e:
                        log(f"  momentum-sniper parse err: {type(e).__name__}: {e}")
            if passed > 0:
                log(f"MOMENTUM-SNIPER: {passed} tokens entered this cycle")
        except Exception as e:
            log(f"momentum_sniper err: {type(e).__name__}: {e}")
            await asyncio.sleep(15)


async def graduating_sniper(client: Client, kp: Optional[Keypair]):
    """V41.13h: target tokens at curve 95-99.5% — about to graduate within minutes.
    Graduation event = guaranteed FOMO catalyst. We enter pre-grad and sell on the spike."""
    if not SOLANATRACKER_ENABLED:
        return
    log("GRAD-IMMINENT: polling /tokens/multi/all every 60s for tokens at curve 95-99.5%")
    while True:
        try:
            await asyncio.sleep(60)
            data = await asyncio.to_thread(_st_fetch, "/tokens/multi/all")
            if not data:
                continue
            graduating = data.get("graduating", []) if isinstance(data, dict) else []
            considered = passed = 0
            for t in graduating:
                try:
                    pools = t.get("pools") or []
                    if not pools:
                        continue
                    cp_raw = pools[0].get("curvePercentage")
                    if cp_raw is None:
                        continue
                    cp = float(cp_raw)
                    if cp < 95.0 or cp >= 99.5:  # too early or too late
                        continue
                    liq_usd = ((pools[0].get("liquidity") or {}).get("usd") or 0)
                    try:
                        liq_usd = float(liq_usd)
                    except Exception:
                        liq_usd = 0
                    if liq_usd < 500:  # essentially-zero pool — skip
                        continue
                    risk = t.get("risk", {}) or {}
                    if bool(risk.get("rugged", True)):
                        continue
                    # V41.13p: pre-entry dump filter. Tokens at 95-99% curve can rug if dev
                    # is dumping. 5abdNwEj rugged -21% in 11s. Filter out tokens dropping in 1m or 5m.
                    events = t.get("events") or {}
                    p1m = (events.get("1m") or {}).get("priceChangePercentage")
                    p5m = (events.get("5m") or {}).get("priceChangePercentage")
                    if p1m is not None and p1m < -2.0:
                        continue   # already dumping in last minute
                    if p5m is not None and p5m < -5.0:
                        continue   # extended downtrend, dev likely exiting
                    mint = (t.get("token") or {}).get("mint", "")
                    if not mint or mint in graduated_seen:
                        continue
                    mint_lc = mint.lower()
                    if not (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                        continue
                    considered += 1
                    graduated_seen.add(mint)
                    if len(graduated_seen) > 500:
                        graduated_seen.clear()
                    log(f"*** GRAD-IMMINENT *** {mint} curve={cp:.2f}% liq=${liq_usd:,.0f} 1m={p1m if p1m is not None else 'NA'} 5m={p5m if p5m is not None else 'NA'}")
                    passed += 1
                    asyncio.create_task(graduation_snipe(client, kp, mint, launchpad="grad_imminent"))
                except Exception as e:
                    log(f"  grad-imminent parse err: {type(e).__name__}: {e}")
            log(f"GRAD-IMMINENT poll: {considered} candidates in 95-99.5% sweet spot")
        except Exception as e:
            log(f"graduating_sniper err: {type(e).__name__}: {e}")
            await asyncio.sleep(15)


async def copy_trader_listener(client: Client, kp: Optional[Keypair]):
    """V41.13g: subscribe to top traders' wallets via Helius. Mirror their buys."""
    if not COPY_TRADE_ENABLED:
        log("COPY-TRADE: DISABLED")
        return
    # Fetch top traders once at startup (and refresh every COPY_TRADE_REFRESH_HOURS)
    last_refresh = 0.0
    top_traders: list = []
    while True:
        try:
            now = time.time()
            if now - last_refresh > COPY_TRADE_REFRESH_HOURS * 3600 or not top_traders:
                data = await asyncio.to_thread(_st_fetch_top_traders, COPY_TRADE_TOP_N)
                if not data:
                    log("COPY-TRADE: leaderboard fetch failed, retrying in 60s")
                    await asyncio.sleep(60)
                    continue
                wallets = data.get("wallets", []) if isinstance(data, dict) else []
                # Filter & rank: require winPercentage >= threshold AND positive realized PnL
                eligible = []
                for w in wallets:
                    s = w.get("summary", {}) or {}
                    wr = s.get("winPercentage")
                    realized = s.get("realized") or 0
                    if wr is None:
                        continue
                    # API returns winPercentage as 0-1 OR 0-100 inconsistently; normalize
                    wr_n = wr / 100.0 if wr > 1.0 else wr
                    # V41.17 Fix #5: tighter wallet filter — require both consistency
                    # (winPercentage >= COPY_TRADE_MIN_WIN_RATE_TIGHT) AND meaningful PnL
                    # scale (realized >= COPY_TRADE_MIN_REALIZED_SOL). Lottery-ticket
                    # 1-SOL profit traders are not detectable enough to follow profitably.
                    floor_wr = max(COPY_TRADE_MIN_WIN_RATE, COPY_TRADE_MIN_WIN_RATE_TIGHT)
                    if wr_n >= floor_wr and realized >= COPY_TRADE_MIN_REALIZED_SOL:
                        eligible.append((w["wallet"], wr_n, realized))
                eligible.sort(key=lambda x: -x[2])  # by realized PnL desc
                top_traders = [t[0] for t in eligible[:COPY_TRADE_TOP_N]]
                last_refresh = now
                log(f"COPY-TRADE: tracking {len(top_traders)} top traders")
                for addr, wr_n, realized in eligible[:COPY_TRADE_TOP_N]:
                    log(f"  - {addr} WR={wr_n*100:.1f}% realized={realized:.0f} SOL")
                if not top_traders:
                    log("COPY-TRADE: no eligible traders, retrying in 60s")
                    await asyncio.sleep(60)
                    continue
            # V41.14: shredSubscribe via Solana Tracker RPC (50-150ms latency).
            # Falls back to Helius logsSubscribe if ST RPC not configured.
            trader_set = set(top_traders)
            try:
                if ST_RPC_ENABLED:
                    async with websockets.connect(ST_RPC_WS, ping_interval=10, ping_timeout=20) as ws:
                        # Single shredSubscribe with all wallets in accountInclude
                        sub_msg = {
                            "jsonrpc": "2.0", "id": 9000,
                            "method": "shredSubscribe",
                            "params": [
                                {"accountInclude": top_traders, "vote": False},
                                {"encoding": "base64", "transactionDetails": "full",
                                 "maxSupportedTransactionVersion": 0},
                            ],
                        }
                        await ws.send(json.dumps(sub_msg))
                        log(f"COPY-TRADE: subscribed via ST shredSubscribe for {len(top_traders)} wallets (50-150ms latency)")
                        async for raw in ws:
                            data = json.loads(raw)
                            if "method" not in data:
                                continue
                            res = (data.get("params", {}) or {}).get("result", {})
                            sig = res.get("signature")
                            if not sig:
                                continue
                            if time.time() - last_refresh > COPY_TRADE_REFRESH_HOURS * 3600:
                                log("COPY-TRADE: leaderboard refresh due — reconnecting")
                                break
                            asyncio.create_task(_handle_copy_trader_tx(client, kp, sig, trader_set))
                else:
                    # Fallback: Helius logsSubscribe (slower, ~500-1500ms)
                    async with websockets.connect(SOLANA_WS_URL, ping_interval=10, ping_timeout=20) as ws:
                        for i, addr in enumerate(top_traders):
                            sub_msg = {
                                "jsonrpc": "2.0", "id": 9000 + i,
                                "method": "logsSubscribe",
                                "params": [{"mentions": [addr]}, {"commitment": "confirmed"}],
                            }
                            await ws.send(json.dumps(sub_msg))
                        log(f"COPY-TRADE: subscribed to Helius for {len(top_traders)} wallets (fallback)")
                        async for raw in ws:
                            data = json.loads(raw)
                            if "method" not in data:
                                continue
                            val = (data.get("params", {}) or {}).get("result", {}).get("value", {})
                            sig = val.get("signature")
                            err = val.get("err")
                            if not sig or err is not None:
                                continue
                            if time.time() - last_refresh > COPY_TRADE_REFRESH_HOURS * 3600:
                                log("COPY-TRADE: leaderboard refresh due — reconnecting")
                                break
                            asyncio.create_task(_handle_copy_trader_tx(client, kp, sig, trader_set))
            except Exception as e:
                log(f"COPY-TRADE WS err, reconnecting in 5s: {type(e).__name__}: {e}")
                await asyncio.sleep(5)
        except Exception as e:
            log(f"COPY-TRADE outer err: {type(e).__name__}: {e}")
            await asyncio.sleep(15)


async def main():
    if PAPER_TRADING:
        log("PAPER MODE ON — no real trades will be executed")
    kp = load_keypair()
    if not PAPER_TRADING and not kp:
        log("FATAL: live mode requires SOLANA_PRIVATE_KEY env var")
        return
    client = Client(SOLANA_RPC_URL, commitment=Confirmed)
    try:
        slot = client.get_slot().value
        log(f"RPC connected, slot {slot}")
    except Exception as e:
        log(f"FATAL: RPC connection failed: {e}")
        return
    if jupiter_healthy():
        log("Jupiter API healthy")
    else:
        log("WARNING: Jupiter API not responding to health probe")
    if kp:
        try:
            balance = client.get_balance(kp.pubkey()).value / 10**9
            log(f"Balance: {balance:.4f} SOL")
            if not PAPER_TRADING and balance < 0.1:
                log("WARNING: low balance, tiny-cap mode enabled")
        except Exception:
            pass
    log(f"Mode: V40 impulse-tape sniper | core={CORE_AMOUNT_SOL:.4f} scout={SCOUT_AMOUNT_SOL:.4f} scale={SCALE_IN_AMOUNT_SOL:.4f} paper_drag={PAPER_ROUND_TRIP_DRAG_BPS}bps")
    log(f"V40 rules: no micro-noise | TP on current price | scale-in after TP only | account_stream={USE_POSITION_ACCOUNT_STREAM}")
    log(f"Max concurrent: {MAX_CONCURRENT_POSITIONS} | Session loss limit: {MAX_SESSION_LOSS_SOL} SOL")
    log(f"=== V41.5 ARCHITECTURAL TIGHTENING ===")
    log(f"  Grad gate: +40% to +50% momentum (was +5% to +50%)")
    log(f"  Grad SL: {GRAD_SL_PCT*100:.0f}% (was -12%)")
    log(f"  Grad TP: single-rung +10% sell 100% (no moonbag)")
    log(f"  V40 TP: single-rung +18% sell 100% (no moonbag)")
    log(f"  Cobuy strategy: {'ENABLED' if COBUY_ENABLED else 'DISABLED'} (Xwu6 demoted)")
    log(f"  Holders gate: V40 entries skip if <4 real holders")
    log(f"  Daily trade cap: {MAX_TRADES_PER_DAY}")
    log(f"  Streak pause: {LOSS_STREAK_PAUSE_SEC}s after {LOSS_STREAK_PAUSE_THRESHOLD} consec losses")
    log(f"  Hard halts: -{MAX_SESSION_LOSS_SOL*1e3:.1f} mSOL session OR {MAX_CONSEC_LOSSES} consec losses")
    log(f"  Win classifier: pnl > {MIN_REAL_WIN_SOL:.4f} SOL (paper break-even = LOSS)")
    log(f"=========================================")
    log(f"=== V41.7-13 ADDITIONS ===")
    log(f"  Bundle detection: {'ENABLED' if BUNDLE_DETECTION_ENABLED else 'DISABLED'} (>={BUNDLE_SAME_SLOT_THRESHOLD} wallets in {BUNDLE_SLOT_WINDOW}-slot window)")
    log(f"    + V41.10: shared-funder correlation, V41.11: fresh-wallet check")
    log(f"  Early-sell-pressure check: 4+ sells in first {EARLY_SELL_PRESSURE_LOOKBACK} txs = skip")
    log(f"  V41.13 SOLANA TRACKER: {'ENABLED' if SOLANATRACKER_ENABLED else 'DISABLED'}")
    if SOLANATRACKER_ENABLED:
        log(f"    + ST poll: /tokens/latest every {SOLANATRACKER_POLL_SEC}s (filter ultra-tight)")
        log(f"    + Filter: score<={ST_MAX_RISK_SCORE}, bundlers<={ST_MAX_BUNDLER_PCT}%, dev<={ST_MAX_DEV_PCT}%, top10<={ST_MAX_TOP10_PCT}%, curve {ST_MIN_CURVE_PCT}-{ST_MAX_CURVE_PCT}%")
    log(f"  V41.13g COPY-TRADE: {'ENABLED' if COPY_TRADE_ENABLED else 'DISABLED'}")
    if COPY_TRADE_ENABLED:
        log(f"    + Tracking top {COPY_TRADE_TOP_N} profitable traders")
        if ST_RPC_ENABLED:
            log(f"    + V41.14: Solana Tracker shredSubscribe (50-150ms latency)")
        else:
            log(f"    + Helius WebSocket fallback (~500-1500ms latency)")
        log(f"    + Leaderboard refresh every {COPY_TRADE_REFRESH_HOURS}h")
    log(f"  V41.14 RAPTOR: {'ENABLED' if RAPTOR_ENABLED else 'DISABLED'} ({RAPTOR_BASE})")
    if RAPTOR_ENABLED:
        log(f"    + Quotes tried via Raptor first, Jupiter as fallback")
        log(f"    + Live mode: /send-transaction via Yellowstone Jet TPU")
    log(f"  Bonk.fun stream: {'ENABLED' if BONK_ENABLED else 'DISABLED'} (program {str(BONK_PROGRAM)[:8]}...)")
    log(f"    + V41.9: detect initialize_v2 (new mint) AND migrate_to_amm/cpswap (graduation)")
    log(f"    + bonk_pregrad and bonk both use Jupiter→Raydium routing, +2% fee offset on TP")
    log(f"  V41.8 BIG-WIN MODE: pos=0.05 SOL ($4.20), V40 TP=+50%, GRAD TP=+50%, target $2.10 per TP hit")
    log(f"  Max concurrent: {MAX_CONCURRENT_POSITIONS} (was 6) | Session halt: -{MAX_SESSION_LOSS_SOL:.3f} SOL")
    log(f"  Latency stack: Helius WS (logs+accounts) + PumpPortal WS + bonk parallel stream")
    log(f"=== V41.17 LATENCY + CORRECTNESS FIXES ===")
    log(f"  Fix #1: pre-cached rug check (refresh {RISK_CACHE_REFRESH_SEC}s, TTL {RISK_CACHE_TTL_SEC}s) → ~5ms hot-path")
    log(f"  Fix #2: warm /stream/swap pool ({WARM_POOL_SIZE} mints, TTL {WARM_SWAP_TTL_SEC}s) → ~10-30ms entry"
        f" {'[ACTIVE]' if (WARM_POOL_ENABLED and (kp or not PAPER_TRADING)) else '[paper-skip]'}")
    log(f"  Fix #3: smart-wallet exit pre-flight (timeout {EXIT_CHECK_TIMEOUT_SEC*1000:.0f}ms, parallel with quote)"
        f" {'[ACTIVE]' if EXIT_CHECK_ENABLED else '[disabled]'}")
    log(f"  Fix #4: curve %-gate at signal time (max {COPY_FAST_MAX_CURVE_PCT:.0f}% — past sweet spot rejected)")
    log(f"  Fix #5: tighter wallet allowlist (WR>={COPY_TRADE_MIN_WIN_RATE_TIGHT*100:.0f}% AND realized>={COPY_TRADE_MIN_REALIZED_SOL:.0f} SOL)")
    log(f"  Fix #6: bundle freshness gate (reject if any bundle <{BUNDLE_FRESHNESS_THRESHOLD_SEC}s old)")
    log(f"  Fix #7: first-buyer holding rate gate (min {FIRST_BUYER_MIN_HOLD_RATE*100:.0f}%, age >{FIRST_BUYER_MIN_TOKEN_AGE_SEC}s)"
        f" {'[ACTIVE]' if FIRST_BUYER_GATE_ENABLED else '[disabled]'}")
    log(f"  Fix #8: simulateTransaction pre-flight for entries >${SIMULATE_NOTIONAL_USD_THRESHOLD:.0f} (live only)")
    log(f"  Fix #9: 8s no-pump time-stop on copy_fast {'[ACTIVE]' if TIME_STOP_ENABLED else '[disabled]'}")
    log(f"  Fix #10: Raptor /swap path (program-upgrade resilient — Apr 2026 buy 17→18 accts)")
    log(f"  V41.17b: dump_rebound {'ENABLED' if DUMP_REBOUND_ENABLED else 'DISABLED'} — copy_fast-only session for clean PnL signal")
    log(f"=========================================")
    asyncio.create_task(session_reporter())
    asyncio.create_task(pumpportal_migration_listener(client, kp))
    asyncio.create_task(solanatracker_poll_latest(client, kp))
    # V41.17 Fix #1: pre-cache risk for hot-path lookup
    asyncio.create_task(refresh_risk_cache())
    # V41.17 Fix #2: warm /stream/swap pool (live mode + keypair)
    if not PAPER_TRADING and kp:
        asyncio.create_task(warm_raptor_swap_pool(kp))
    # V41.15: grad_imminent DISABLED (net -$0.94 in last session vs copy_fast +$3.56).
    # Pre-graduation tokens at curve 95-99% rug too often; the catalyst doesn't reliably pump.
    # Copy-trade is the proven winning strategy.
    # asyncio.create_task(graduating_sniper(client, kp))   # net loser, disabled
    asyncio.create_task(copy_trader_listener(client, kp))
    # asyncio.create_task(momentum_sniper(client, kp))     # disabled — chases peaks
    await monitor_pump_fun(client, kp)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Interrupted")
