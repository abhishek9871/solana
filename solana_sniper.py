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
import struct
import time
from collections import defaultdict, deque
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


def _env_clean(name: str, default: str = "") -> str:
    """Read env vars defensively; systemd/.env values can carry CRLF/quotes."""
    return os.environ.get(name, default).strip().strip('"').strip("'")


# === CONFIG ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_TRADING = os.environ.get("PAPER_TRADING", "1").strip().lower() not in {"0", "false", "no", "off"}
POSITIONS_STATE_FILE = os.environ.get(
    "POSITIONS_STATE_FILE",
    os.path.join(BASE_DIR, "data", "positions_state.json"),
)
ALPHA_STATE_FILE = os.environ.get(
    "ALPHA_STATE_FILE",
    os.path.join(BASE_DIR, "data", "executable_alpha.json"),
)

# Wallet & RPC
SOLANA_RPC_URL = _env_clean("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb")
SOLANA_WS_URL = SOLANA_RPC_URL.replace("https://", "wss://").replace("http://", "ws://")
PRIVATE_KEY_B58 = _env_clean("SOLANA_PRIVATE_KEY", "")

# V41.14: Solana Tracker RPC with shredSubscribe (50-150ms latency vs Helius ~500-1500ms)
ST_RPC_KEY = _env_clean("SOLANATRACKER_RPC_KEY")
ST_RPC_HTTP = _env_clean("SOLANATRACKER_RPC_HTTP")
ST_RPC_WS = _env_clean("SOLANATRACKER_RPC_WS")
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
SOLANATRACKER_API_KEY = _env_clean("SOLANATRACKER_API_KEY")
SOLANATRACKER_ENABLED = bool(SOLANATRACKER_API_KEY) and os.environ.get("SOLANATRACKER_ENABLED", "1") == "1"
SOLANATRACKER_BASE = "https://data.solanatracker.io"
# V41.13: 2-min poll per user request. Burns ~21,600 calls/month — exhausts 10k plan
# by ~day 14. User explicitly accepted this trade-off for faster candidate discovery.
SOLANATRACKER_POLL_SEC = int(os.environ.get("SOLANATRACKER_POLL_SEC", "120"))
ST_DATA_API_AUTH_BACKOFF_SEC = float(os.environ.get("ST_DATA_API_AUTH_BACKOFF_SEC", "300"))
ST_DATA_API_RATE_BACKOFF_SEC = float(os.environ.get("ST_DATA_API_RATE_BACKOFF_SEC", "45"))
ST_DATA_API_CREDIT_BACKOFF_SEC = float(os.environ.get("ST_DATA_API_CREDIT_BACKOFF_SEC", "1800"))
ST_DATA_API_BACKOFF_LOG_SEC = float(os.environ.get("ST_DATA_API_BACKOFF_LOG_SEC", "60"))
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
# V41.17f: weak_scalp DISABLED. By construction this strategy enters on WEAK signals
# (e.g., bad round-trip quote, no Jupiter route in paper, low holders) and uses scout
# size. Empirical: 5nn6F8sq lost -$0.22 on a 14s no-momentum exit, peak=1.00x — exactly
# the "follow into something that doesn't pump" failure mode V41.17 was meant to cure
# in copy_fast (Fix #11). V40's manage_position has no equivalent gate. Late_breakout
# stays (different profile, just won +$0.54). Gated in choose_entry_amount → returns 0
# → caller logs "NO ENTRY" and skips cleanly without per-site changes.
WEAK_SCALP_ENABLED = os.environ.get("WEAK_SCALP_ENABLED", "0") == "1"
# V41.17v: V40 momentum strategy DISABLED. Two losses this session
# (FD35KjEF -$0.22 NO-MOMENTUM at 24s, BEPwXuRQ -$3.69 GAP DUMP at -42%)
# show the same dead-peak pattern as weak_scalp/dump_rebound. Even with
# Fix #9 8s time-stop in V40 (V41.17j), the GAP DUMP fires faster than
# our exits can. Killing per user directive. Late_breakout stays active.
MOMENTUM_ENABLED = os.environ.get("MOMENTUM_ENABLED", "0") == "1"
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
MAX_DAILY_LOSS_SOL = float(os.environ.get("MAX_DAILY_LOSS_SOL", "0.50"))
DAILY_LOSS_EXIT_ENABLED = os.environ.get("DAILY_LOSS_EXIT_ENABLED", "1") == "1"
MIN_WALLET_BALANCE_SOL = float(os.environ.get("MIN_WALLET_BALANCE_SOL", "0.05"))
WALLET_BALANCE_CHECK_SEC = int(os.environ.get("WALLET_BALANCE_CHECK_SEC", "60"))
MAX_CONSEC_LOSSES = int(os.environ.get("MAX_CONSEC_LOSSES", "5"))               # halt at 5 straight
# Tiny paper scouts often close for -0.0000/-0.0001 SOL on fees/slippage. Count
# those in W/L, but only real losses should trip the consecutive-loss brake.
CONSEC_LOSS_COUNT_MIN_SOL = float(os.environ.get("CONSEC_LOSS_COUNT_MIN_SOL", "0.0002"))
CONSEC_LOSS_HALT_SEC = float(os.environ.get("CONSEC_LOSS_HALT_SEC", "180"))
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "10"))
# V41.19: high-frequency market tape needs a data cap, not a low-frequency throttle.
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "250"))
# Keep a streak brake, but make it compatible with sub-second tiny-scout sampling.
LOSS_STREAK_PAUSE_THRESHOLD = int(os.environ.get("LOSS_STREAK_PAUSE_THRESHOLD", "4"))
LOSS_STREAK_PAUSE_SEC = int(os.environ.get("LOSS_STREAK_PAUSE_SEC", "30"))

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
consec_loss_halt_until = 0.0
daily_pnl_sol = 0.0
daily_loss_reset_ts = time.time()
daily_loss_halt_reason = ""
_positions_closing: set[str] = set()
_entry_inflight_mints: set[str] = set()
_swarm_compound_locks: dict[str, asyncio.Lock] = {}
_recently_closed_mints: dict[str, float] = {}
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
    global daily_trade_count, daily_count_reset_ts, consec_losses, consec_loss_halt_until
    now = time.time()
    # Daily window resets every 24h. If the last reset was over 24h ago, zero the counter.
    if now - daily_count_reset_ts > 86400:
        daily_trade_count = 0
        daily_count_reset_ts = now
    if session_pnl_sol <= -MAX_SESSION_LOSS_SOL:
        return True, f"session loss limit hit ({session_pnl_sol:+.4f} SOL)"
    if _daily_loss_limit_hit():
        return True, daily_loss_halt_reason or f"daily loss limit hit ({daily_pnl_sol:+.4f} SOL)"
    if consec_losses >= MAX_CONSEC_LOSSES:
        if CONSEC_LOSS_HALT_SEC <= 0:
            return True, f"consec_loss limit hit ({consec_losses})"
        if consec_loss_halt_until <= 0:
            consec_loss_halt_until = now + CONSEC_LOSS_HALT_SEC
            log(f"  CONSEC-LOSS COOLDOWN: {consec_losses} real losses; pausing entries for "
                f"{CONSEC_LOSS_HALT_SEC:.0f}s")
        if now < consec_loss_halt_until:
            return True, f"consec_loss cooldown ({int(consec_loss_halt_until - now)}s remaining)"
        log(f"  CONSEC-LOSS COOLDOWN DONE: resuming entries after {consec_losses} real losses")
        consec_losses = 0
        consec_loss_halt_until = 0.0
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
    global consec_loss_halt_until
    global daily_pnl_sol, daily_loss_reset_ts, daily_loss_halt_reason
    now = time.time()
    if now - daily_loss_reset_ts > 86400:
        daily_pnl_sol = 0.0
        daily_loss_reset_ts = now
    session_pnl_sol += pnl
    daily_pnl_sol += pnl
    if pnl > MIN_REAL_WIN_SOL:
        session_wins += 1
        consec_losses = 0
        consec_loss_halt_until = 0.0
    else:
        session_losses += 1
        if pnl <= -CONSEC_LOSS_COUNT_MIN_SOL:
            consec_losses += 1
        if consec_losses >= LOSS_STREAK_PAUSE_THRESHOLD and streak_pause_until < now:
            streak_pause_until = time.time() + LOSS_STREAK_PAUSE_SEC
            log(f"  STREAK PAUSE: {consec_losses} real consec losses; pausing new entries for "
                f"{LOSS_STREAK_PAUSE_SEC}s")
        if consec_losses >= MAX_CONSEC_LOSSES and CONSEC_LOSS_HALT_SEC > 0 and consec_loss_halt_until <= now:
            consec_loss_halt_until = now + CONSEC_LOSS_HALT_SEC
            log(f"  CONSEC-LOSS COOLDOWN: {consec_losses} real losses; pausing entries for "
                f"{CONSEC_LOSS_HALT_SEC:.0f}s")
    if _daily_loss_limit_hit():
        daily_loss_halt_reason = f"daily loss limit hit ({daily_pnl_sol:+.4f} SOL / -{MAX_DAILY_LOSS_SOL:.4f} SOL)"


def _record_entry_opened() -> None:
    """V41.5: increment daily trade counter when a position is opened."""
    global daily_trade_count
    daily_trade_count += 1


def _position_to_state(pos: Position) -> dict:
    data = {}
    for name in Position.__dataclass_fields__:
        value = getattr(pos, name)
        data[name] = str(value) if name == "bc_pda" and value else value
    return data


def _position_from_state(mint: str, data: dict) -> Optional[Position]:
    try:
        bc_pda_raw = data.get("bc_pda")
        bc_pda = Pubkey.from_string(bc_pda_raw) if bc_pda_raw else None
        if bc_pda is None:
            bc_pda = derive_bc_pda(Pubkey.from_string(mint))
        return Position(
            mint=str(data.get("mint") or mint),
            entry_price=float(data.get("entry_price", 0.0)),
            entry_amount_sol=float(data.get("entry_amount_sol", 0.0)),
            token_amount=float(data.get("token_amount", 0.0)),
            open_time=float(data.get("open_time", time.time())),
            peak_price=float(data.get("peak_price", 1.0)),
            rung_hit=int(data.get("rung_hit", 0)),
            remaining_pct=float(data.get("remaining_pct", 1.0)),
            realized_sol=float(data.get("realized_sol", 0.0)),
            last_price=float(data.get("last_price", 0.0)),
            bc_pda=bc_pda,
            graduated=bool(data.get("graduated", False)),
            late_scalp=bool(data.get("late_scalp", False)),
            strategy=str(data.get("strategy", "momentum")),
            entry_progress=float(data.get("entry_progress", 0.0)),
            quality_score=int(data.get("quality_score", 0)),
            entry_size_sol=float(data.get("entry_size_sol", 0.0)),
            adds_done=int(data.get("adds_done", 0)),
            launchpad=str(data.get("launchpad", "pump")),
            signal_time_ms=int(data.get("signal_time_ms", 0)),
        )
    except Exception as e:
        log(f"  POSITION RESTORE SKIP {mint[:8]}: {type(e).__name__}: {e}")
        return None


def _persist_positions() -> None:
    try:
        state_dir = os.path.dirname(os.path.abspath(POSITIONS_STATE_FILE))
        os.makedirs(state_dir, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "paper_trading": PAPER_TRADING,
            "daily": {
                "pnl_sol": daily_pnl_sol,
                "reset_ts": daily_loss_reset_ts,
            },
            "positions": {
                mint: _position_to_state(pos)
                for mint, pos in positions.items()
                if pos.remaining_pct > 0.01
            },
        }
        tmp_path = POSITIONS_STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, POSITIONS_STATE_FILE)
    except Exception as e:
        log(f"  POSITION PERSIST ERR: {type(e).__name__}: {e}")


def _load_positions_state() -> int:
    global daily_pnl_sol, daily_loss_reset_ts
    if not os.path.isfile(POSITIONS_STATE_FILE):
        return 0
    try:
        with open(POSITIONS_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        daily = payload.get("daily") or {}
        reset_ts = float(daily.get("reset_ts", time.time()))
        if time.time() - reset_ts <= 86400:
            daily_loss_reset_ts = reset_ts
            daily_pnl_sol = float(daily.get("pnl_sol", 0.0))
        restored = 0
        for mint, item in (payload.get("positions") or {}).items():
            pos = _position_from_state(mint, item or {})
            if pos and pos.remaining_pct > 0.01:
                positions[pos.mint] = pos
                restored += 1
        if restored:
            log(f"  POSITION RESTORE: loaded {restored} open position(s) from {POSITIONS_STATE_FILE}")
        return restored
    except Exception as e:
        log(f"  POSITION RESTORE ERR: {type(e).__name__}: {e}")
        return 0


def _wallet_token_raw_balance(owner: str, mint: str) -> Optional[int]:
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                owner,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }
        r = requests.post(SOLANA_RPC_URL, json=payload, timeout=8)
        r.raise_for_status()
        data = r.json()
        total = 0
        for acc in ((data.get("result") or {}).get("value") or []):
            amount = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount", {}).get("amount", "0")
            total += int(amount or 0)
        return total
    except Exception as e:
        log(f"  POSITION RECONCILE ERR {mint[:8]}: {type(e).__name__}: {e}")
        return None


def _reconcile_recovered_positions(client: Client, kp: Optional[Keypair]) -> None:
    if PAPER_TRADING or not kp or not positions:
        return
    owner = str(kp.pubkey())
    changed = False
    for mint, pos in list(positions.items()):
        bal = _wallet_token_raw_balance(owner, mint)
        if bal is None:
            continue
        if bal <= 0:
            log(f"  POSITION RECONCILE DROP {mint[:8]}: wallet has zero tokens")
            positions.pop(mint, None)
            changed = True
            continue
        expected = pos.token_amount * max(pos.remaining_pct, 0.0)
        if expected <= 0 or abs(bal - expected) / max(expected, 1.0) > 0.05:
            log(f"  POSITION RECONCILE {mint[:8]}: state_qty={expected:.0f}, wallet_qty={bal}")
            pos.token_amount = float(bal)
            pos.remaining_pct = 1.0
            changed = True
    if changed:
        _persist_positions()


def _store_open_position(pos: Position) -> None:
    positions[pos.mint] = pos
    _entry_inflight_mints.discard(pos.mint)
    _positions_closing.discard(pos.mint)
    _persist_positions()


def _remove_open_position(pos: Position) -> None:
    if positions.get(pos.mint) is pos:
        positions.pop(pos.mint, None)
    elif pos.mint in positions and positions[pos.mint].open_time == pos.open_time:
        positions.pop(pos.mint, None)
    _recently_closed_mints[pos.mint] = time.time()
    if len(_recently_closed_mints) > 1000:
        cutoff = time.time() - max(RECENT_CLOSE_REENTRY_COOLDOWN_SEC, 60.0)
        for mint, ts in list(_recently_closed_mints.items()):
            if ts < cutoff:
                _recently_closed_mints.pop(mint, None)
    _positions_closing.discard(pos.mint)
    _entry_inflight_mints.discard(pos.mint)
    _swarm_compound_locks.pop(pos.mint, None)
    _persist_positions()


def _daily_loss_limit_hit() -> bool:
    if MAX_DAILY_LOSS_SOL <= 0:
        return False
    return daily_pnl_sol <= -MAX_DAILY_LOSS_SOL


def _maybe_stop_for_daily_loss() -> None:
    if not _daily_loss_limit_hit():
        return
    reason = daily_loss_halt_reason or f"daily loss limit hit ({daily_pnl_sol:+.4f} SOL)"
    log(f"FATAL: {reason}; stopping bot")
    _persist_positions()
    if DAILY_LOSS_EXIT_ENABLED:
        os._exit(0)


def _position_open_for_compound(pos: Position) -> bool:
    return (
        positions.get(pos.mint) is pos
        and pos.mint not in _positions_closing
        and pos.remaining_pct > 0.01
    )


def _mint_recently_closed(mint: str, now: Optional[float] = None) -> bool:
    if not mint:
        return False
    now = time.time() if now is None else now
    ts = _recently_closed_mints.get(mint, 0.0)
    return bool(ts and now - ts < RECENT_CLOSE_REENTRY_COOLDOWN_SEC)


def _claim_entry_mint(mint: str, source: str) -> bool:
    if not mint:
        return False
    if mint in positions or mint in _positions_closing or mint in _entry_inflight_mints:
        log(f"  ENTRY SKIP {mint[:8]} ({source}): position/in-flight dedup")
        return False
    _entry_inflight_mints.add(mint)
    return True


def _release_entry_mint(mint: str) -> None:
    _entry_inflight_mints.discard(mint)


def _get_swarm_compound_lock(mint: str) -> asyncio.Lock:
    lock = _swarm_compound_locks.get(mint)
    if lock is None:
        lock = asyncio.Lock()
        _swarm_compound_locks[mint] = lock
    return lock

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
GRAD_MANAGER_POLL_SEC = float(os.environ.get("GRAD_MANAGER_POLL_SEC", "2.0"))
GRAD_COPY_FAST_POLL_SEC = float(os.environ.get("GRAD_COPY_FAST_POLL_SEC", "0.50"))
GRAD_SWARM_POLL_SEC = float(os.environ.get("GRAD_SWARM_POLL_SEC", "0.25"))
GRAD_JUPITER_FALLBACK_SEC = float(os.environ.get("GRAD_JUPITER_FALLBACK_SEC", "2.0"))
GRAD_BC_CACHE_MAX_AGE_MS = int(os.environ.get("GRAD_BC_CACHE_MAX_AGE_MS", "1500"))
SWARM_COMPOUND_MIN_MULT = float(os.environ.get("SWARM_COMPOUND_MIN_MULT", "1.06"))
RECENT_CLOSE_REENTRY_COOLDOWN_SEC = float(os.environ.get("RECENT_CLOSE_REENTRY_COOLDOWN_SEC", "45"))
COPY_FAST_CONFIRM_ENABLED = os.environ.get("COPY_FAST_CONFIRM_ENABLED", "1") == "1"
COPY_FAST_CONFIRM_WINDOW_SEC = float(os.environ.get("COPY_FAST_CONFIRM_WINDOW_SEC", "3.0"))
COPY_FAST_CONFIRM_POLL_SEC = float(os.environ.get("COPY_FAST_CONFIRM_POLL_SEC", "0.10"))
COPY_FAST_CONFIRM_MIN_MULT = float(os.environ.get("COPY_FAST_CONFIRM_MIN_MULT", "1.12"))
COPY_FAST_CONFIRM_MAX_MULT = float(os.environ.get("COPY_FAST_CONFIRM_MAX_MULT", "1.75"))
COPY_FAST_CONFIRM_MAX_OFF_PEAK = float(os.environ.get("COPY_FAST_CONFIRM_MAX_OFF_PEAK", "0.02"))
COPY_FAST_CONFIRM_MAX_DUMP = float(os.environ.get("COPY_FAST_CONFIRM_MAX_DUMP", "-0.04"))
COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS = int(os.environ.get("COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS", "600"))
COPY_FAST_CONFIRM_MIN_SWARM = int(os.environ.get("COPY_FAST_CONFIRM_MIN_SWARM", "4"))
COPY_FAST_CONFIRM_SWARM_WINDOW_SEC = float(os.environ.get("COPY_FAST_CONFIRM_SWARM_WINDOW_SEC", "10.0"))
COPY_FAST_CONFIRM_SWARM3_CONTINUE_SEC = float(os.environ.get("COPY_FAST_CONFIRM_SWARM3_CONTINUE_SEC", "1.0"))
COPY_FAST_CONFIRMED_ENTRY_ENABLED = os.environ.get("COPY_FAST_CONFIRMED_ENTRY_ENABLED", "0") == "1"
COPY_FAST_SWARM_ENTRY_ENABLED = os.environ.get("COPY_FAST_SWARM_ENTRY_ENABLED", "0") == "1"
COPY_FAST_SOLO_ROCKET_ENABLED = os.environ.get("COPY_FAST_SOLO_ROCKET_ENABLED", "0") == "1"
COPY_FAST_SOLO_ROCKET_ALLOW_SINGLE = os.environ.get("COPY_FAST_SOLO_ROCKET_ALLOW_SINGLE", "1") == "1"
COPY_FAST_SOLO_ROCKET_MIN_MULT = float(os.environ.get("COPY_FAST_SOLO_ROCKET_MIN_MULT", "1.40"))
COPY_FAST_SOLO_ROCKET_SWARM2_MIN_MULT = float(os.environ.get("COPY_FAST_SOLO_ROCKET_SWARM2_MIN_MULT", "1.35"))
COPY_FAST_SOLO_ROCKET_AMOUNT_SOL = float(os.environ.get("COPY_FAST_SOLO_ROCKET_AMOUNT_SOL", "0.00625"))
COPY_FAST_SOLO_ROCKET_CONFIRM_DELAY_SEC = float(os.environ.get("COPY_FAST_SOLO_ROCKET_CONFIRM_DELAY_SEC", "0.12"))
COPY_FAST_SOLO_ROCKET_CONFIRM_RETAIN = float(os.environ.get("COPY_FAST_SOLO_ROCKET_CONFIRM_RETAIN", "0.97"))
COPY_FAST_SOLO_ROCKET_TP_MULT = float(os.environ.get("COPY_FAST_SOLO_ROCKET_TP_MULT", "1.055"))
COPY_FAST_SOLO_ROCKET_FAST_KILL_SEC = float(os.environ.get("COPY_FAST_SOLO_ROCKET_FAST_KILL_SEC", "2.0"))
COPY_FAST_SOLO_ROCKET_FAST_KILL_PEAK = float(os.environ.get("COPY_FAST_SOLO_ROCKET_FAST_KILL_PEAK", "1.012"))
COPY_FAST_SOLO_ROCKET_TIMEOUT_SEC = float(os.environ.get("COPY_FAST_SOLO_ROCKET_TIMEOUT_SEC", "8.0"))
ALPHA_LEARNER_ENABLED = os.environ.get("ALPHA_LEARNER_ENABLED", "1") == "1"
ALPHA_ADAPTIVE_ENTRY_ENABLED = os.environ.get("ALPHA_ADAPTIVE_ENTRY_ENABLED", "1") == "1"
ALPHA_EXPLORATION_ENABLED = os.environ.get("ALPHA_EXPLORATION_ENABLED", "0") == "1"
ALPHA_SHADOW_MARKET_TAPE = os.environ.get("ALPHA_SHADOW_MARKET_TAPE", "1") == "1"
ALPHA_MAX_PENDING_SHADOWS = int(os.environ.get("ALPHA_MAX_PENDING_SHADOWS", "120"))
ALPHA_SIGNAL_COOLDOWN_MS = int(os.environ.get("ALPHA_SIGNAL_COOLDOWN_MS", "2500"))
ALPHA_SAVE_EVERY_OUTCOMES = int(os.environ.get("ALPHA_SAVE_EVERY_OUTCOMES", "5"))
ALPHA_MIN_SAMPLES = int(os.environ.get("ALPHA_MIN_SAMPLES", "3"))
ALPHA_PROMOTE_MIN_WR = float(os.environ.get("ALPHA_PROMOTE_MIN_WR", "0.55"))
ALPHA_PROMOTE_MIN_AVG_BEST_NET = float(os.environ.get("ALPHA_PROMOTE_MIN_AVG_BEST_NET", "0.020"))
ALPHA_BLOCK_MIN_SAMPLES = int(os.environ.get("ALPHA_BLOCK_MIN_SAMPLES", "4"))
ALPHA_BLOCK_MAX_WR = float(os.environ.get("ALPHA_BLOCK_MAX_WR", "0.25"))
ALPHA_BLOCK_MAX_AVG_BEST_NET = float(os.environ.get("ALPHA_BLOCK_MAX_AVG_BEST_NET", "0.000"))
ALPHA_CONTEXT_ONLY_MIN_SAMPLES = int(os.environ.get("ALPHA_CONTEXT_ONLY_MIN_SAMPLES", "5"))
ALPHA_CONTEXT_ONLY_MIN_WR = float(os.environ.get("ALPHA_CONTEXT_ONLY_MIN_WR", "0.60"))
ALPHA_CONTEXT_ONLY_MIN_AVG_BEST_NET = float(os.environ.get("ALPHA_CONTEXT_ONLY_MIN_AVG_BEST_NET", "0.050"))
ALPHA_WALLET_ONLY_MIN_SAMPLES = int(os.environ.get("ALPHA_WALLET_ONLY_MIN_SAMPLES", "20"))
ALPHA_WALLET_ONLY_MIN_WR = float(os.environ.get("ALPHA_WALLET_ONLY_MIN_WR", "0.70"))
ALPHA_WALLET_ONLY_MIN_AVG_EXIT_NET = float(os.environ.get("ALPHA_WALLET_ONLY_MIN_AVG_EXIT_NET", "0.080"))
ALPHA_RUNNER_TP1_MULT = float(os.environ.get("ALPHA_RUNNER_TP1_MULT", "1.055"))
ALPHA_RUNNER_TP1_FRACTION = float(os.environ.get("ALPHA_RUNNER_TP1_FRACTION", "0.70"))
COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL = float(os.environ.get("COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL", "0.003125"))
COPY_FAST_ALPHA_CORE_AMOUNT_SOL = float(os.environ.get("COPY_FAST_ALPHA_CORE_AMOUNT_SOL", "0.0125"))
COPY_FAST_CONFIRMED_AMOUNT_SOL = float(os.environ.get("COPY_FAST_CONFIRMED_AMOUNT_SOL", str(COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL)))
COPY_FAST_ALPHA_MIN_ENTRY_MULT = float(os.environ.get("COPY_FAST_ALPHA_MIN_ENTRY_MULT", "1.060"))
COPY_FAST_ALPHA_EXPLORATION_MIN_MULT = float(os.environ.get("COPY_FAST_ALPHA_EXPLORATION_MIN_MULT", "1.22"))
COPY_FAST_ALPHA_EXPLORATION_MAX_MULT = float(os.environ.get("COPY_FAST_ALPHA_EXPLORATION_MAX_MULT", "1.55"))
COPY_FAST_ALPHA_MIN_AVG_EXIT_NET = float(os.environ.get("COPY_FAST_ALPHA_MIN_AVG_EXIT_NET", "0.020"))
COPY_FAST_ALPHA_CORE_MIN_SAMPLES = int(os.environ.get("COPY_FAST_ALPHA_CORE_MIN_SAMPLES", "6"))
COPY_FAST_ALPHA_CORE_MIN_WR = float(os.environ.get("COPY_FAST_ALPHA_CORE_MIN_WR", "0.70"))
COPY_FAST_ALPHA_CORE_MIN_AVG_EXIT_NET = float(os.environ.get("COPY_FAST_ALPHA_CORE_MIN_AVG_EXIT_NET", "0.040"))
COPY_FAST_ALPHA_TP_MULT = float(os.environ.get("COPY_FAST_ALPHA_TP_MULT", "1.045"))
COPY_FAST_ALPHA_FAST_KILL_SEC = float(os.environ.get("COPY_FAST_ALPHA_FAST_KILL_SEC", "2.0"))
COPY_FAST_ALPHA_FAST_KILL_PEAK = float(os.environ.get("COPY_FAST_ALPHA_FAST_KILL_PEAK", "1.012"))
COPY_FAST_ALPHA_TIMEOUT_SEC = float(os.environ.get("COPY_FAST_ALPHA_TIMEOUT_SEC", "8.0"))
MARKET_TAPE_ALPHA_ENABLED = os.environ.get("MARKET_TAPE_ALPHA_ENABLED", "1") == "1"
MARKET_TAPE_ALPHA_MAX_AGE_SEC = float(os.environ.get("MARKET_TAPE_ALPHA_MAX_AGE_SEC", "90.0"))
MARKET_TAPE_ALPHA_MAX_SELL_SOL = float(os.environ.get("MARKET_TAPE_ALPHA_MAX_SELL_SOL", "0.004"))
MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC = float(os.environ.get("MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC", "0.12"))
MARKET_TAPE_ALPHA_CONFIRM_MIN_MULT = float(os.environ.get("MARKET_TAPE_ALPHA_CONFIRM_MIN_MULT", "1.006"))
MARKET_TAPE_ALPHA_RETAIN_CONFIRM_MULT = float(os.environ.get("MARKET_TAPE_ALPHA_RETAIN_CONFIRM_MULT", "0.998"))
MARKET_TAPE_ALPHA_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_ALPHA_MIN_TRACKED", "2"))
MARKET_TAPE_ALPHA_MIN_MOVE_MULT = float(os.environ.get("MARKET_TAPE_ALPHA_MIN_MOVE_MULT", "1.040"))
MARKET_TAPE_ALPHA_MIN_AVG_EXIT_NET = float(os.environ.get("MARKET_TAPE_ALPHA_MIN_AVG_EXIT_NET", "0.000"))
MARKET_TAPE_ALPHA_STRONG_MIN_AVG_EXIT_NET = float(os.environ.get("MARKET_TAPE_ALPHA_STRONG_MIN_AVG_EXIT_NET", "0.020"))
MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_SAMPLES = int(os.environ.get("MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_SAMPLES", "5"))
MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_WR = float(os.environ.get("MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_WR", "0.70"))
MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_AVG_EXIT = float(os.environ.get("MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_AVG_EXIT", "0.050"))
MARKET_TAPE_ALPHA_CONTEXT_COOLDOWN_SEC = float(os.environ.get("MARKET_TAPE_ALPHA_CONTEXT_COOLDOWN_SEC", "2.0"))
COPY_TRADE_WS_IDLE_RECONNECT_SEC = float(os.environ.get("COPY_TRADE_WS_IDLE_RECONNECT_SEC", "45.0"))
MARKET_TAPE_ENABLED = os.environ.get("MARKET_TAPE_ENABLED", "1") == "1"
MARKET_TAPE_ALL_PUMP = os.environ.get("MARKET_TAPE_ALL_PUMP", "1") == "1"
MARKET_TAPE_AMOUNT_SOL = float(os.environ.get("MARKET_TAPE_AMOUNT_SOL", "0.0125"))
MARKET_TAPE_WINDOW_MS = int(os.environ.get("MARKET_TAPE_WINDOW_MS", "1200"))
MARKET_TAPE_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_MIN_UNIQUE", "4"))
MARKET_TAPE_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_MIN_TRACKED", "1"))
MARKET_TAPE_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_MIN_BUY_SOL", "0.08"))
MARKET_TAPE_MAX_SELL_SOL = float(os.environ.get("MARKET_TAPE_MAX_SELL_SOL", "0.01"))
MARKET_TAPE_MIN_BC_MOVE = float(os.environ.get("MARKET_TAPE_MIN_BC_MOVE", "1.005"))
MARKET_TAPE_MAX_BC_MOVE = float(os.environ.get("MARKET_TAPE_MAX_BC_MOVE", "1.35"))
MARKET_TAPE_BC_CACHE_MAX_AGE_MS = int(os.environ.get("MARKET_TAPE_BC_CACHE_MAX_AGE_MS", "700"))
MARKET_TAPE_MIN_PRICE_RATIO = float(os.environ.get("MARKET_TAPE_MIN_PRICE_RATIO", "0.82"))
MARKET_TAPE_MAX_PRICE_RATIO = float(os.environ.get("MARKET_TAPE_MAX_PRICE_RATIO", "1.12"))
MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC = float(os.environ.get("MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC", "5.0"))
MARKET_TAPE_LOW_MOVE_STRONG_BELOW = float(os.environ.get("MARKET_TAPE_LOW_MOVE_STRONG_BELOW", "1.04"))
MARKET_TAPE_LOW_MOVE_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_LOW_MOVE_MIN_UNIQUE", "8"))
MARKET_TAPE_LOW_MOVE_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_LOW_MOVE_MIN_TRACKED", "3"))
MARKET_TAPE_LOW_MOVE_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_LOW_MOVE_MIN_BUY_SOL", "5.0"))
MARKET_TAPE_MID_MOVE_STRONG_BELOW = float(os.environ.get("MARKET_TAPE_MID_MOVE_STRONG_BELOW", "1.10"))
MARKET_TAPE_MID_MOVE_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_MID_MOVE_MIN_UNIQUE", "6"))
MARKET_TAPE_MID_MOVE_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_MID_MOVE_MIN_TRACKED", "2"))
MARKET_TAPE_MID_MOVE_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_MID_MOVE_MIN_BUY_SOL", "8.0"))
MARKET_TAPE_HIGH_MOVE_STRONG_ABOVE = float(os.environ.get("MARKET_TAPE_HIGH_MOVE_STRONG_ABOVE", "1.10"))
MARKET_TAPE_HIGH_MOVE_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_HIGH_MOVE_MIN_UNIQUE", "8"))
MARKET_TAPE_HIGH_MOVE_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_HIGH_MOVE_MIN_TRACKED", "4"))
MARKET_TAPE_HIGH_MOVE_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_HIGH_MOVE_MIN_BUY_SOL", "7.5"))
MARKET_TAPE_HIGH_SCOUT_MAX_BC_MOVE = float(os.environ.get("MARKET_TAPE_HIGH_SCOUT_MAX_BC_MOVE", "1.18"))
MARKET_TAPE_HIGH_SCOUT_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_HIGH_SCOUT_MIN_UNIQUE", "6"))
MARKET_TAPE_HIGH_SCOUT_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_HIGH_SCOUT_MIN_TRACKED", "3"))
MARKET_TAPE_HIGH_SCOUT_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_HIGH_SCOUT_MIN_BUY_SOL", "2.0"))
MARKET_TAPE_CONFIRM_DELAY_SEC = float(os.environ.get("MARKET_TAPE_CONFIRM_DELAY_SEC", "0.35"))
MARKET_TAPE_CONFIRM_MIN_MULT = float(os.environ.get("MARKET_TAPE_CONFIRM_MIN_MULT", "1.003"))
MARKET_TAPE_COOLDOWN_SEC = float(os.environ.get("MARKET_TAPE_COOLDOWN_SEC", "25"))
MARKET_TAPE_MAX_ENTRIES_PER_MIN = int(os.environ.get("MARKET_TAPE_MAX_ENTRIES_PER_MIN", "18"))
MARKET_TAPE_TP_MULT = float(os.environ.get("MARKET_TAPE_TP_MULT", "1.08"))
MARKET_TAPE_FAST_KILL_SEC = float(os.environ.get("MARKET_TAPE_FAST_KILL_SEC", "3.0"))
MARKET_TAPE_FAST_KILL_PEAK = float(os.environ.get("MARKET_TAPE_FAST_KILL_PEAK", "1.025"))
MARKET_TAPE_TIMEOUT_SEC = float(os.environ.get("MARKET_TAPE_TIMEOUT_SEC", "25"))
MARKET_TAPE_EXIT_ENABLED = os.environ.get("MARKET_TAPE_EXIT_ENABLED", "1") == "1"
MARKET_TAPE_EXIT_WINDOW_MS = int(os.environ.get("MARKET_TAPE_EXIT_WINDOW_MS", "900"))
MARKET_TAPE_EXIT_DROP_MULT = float(os.environ.get("MARKET_TAPE_EXIT_DROP_MULT", "0.985"))
MARKET_TAPE_EXIT_MIN_SELL_SOL = float(os.environ.get("MARKET_TAPE_EXIT_MIN_SELL_SOL", "0.020"))
MARKET_TAPE_EXIT_SELL_BUY_RATIO = float(os.environ.get("MARKET_TAPE_EXIT_SELL_BUY_RATIO", "0.35"))
MARKET_TAPE_EXIT_SINGLE_TRACKED_SELL_MIN_AGE_SEC = float(os.environ.get(
    "MARKET_TAPE_EXIT_SINGLE_TRACKED_SELL_MIN_AGE_SEC", "0.75"
))
MARKET_TAPE_SCOUT_ENABLED = os.environ.get("MARKET_TAPE_SCOUT_ENABLED", "1") == "1"
MARKET_TAPE_SCOUT_AMOUNT_SOL = float(os.environ.get("MARKET_TAPE_SCOUT_AMOUNT_SOL", "0.00625"))
MARKET_TAPE_SCOUT_MIN_BC_MOVE = float(os.environ.get("MARKET_TAPE_SCOUT_MIN_BC_MOVE", "1.015"))
MARKET_TAPE_SCOUT_MAX_BC_MOVE = float(os.environ.get("MARKET_TAPE_SCOUT_MAX_BC_MOVE", "1.100"))
MARKET_TAPE_SCOUT_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_SCOUT_MIN_UNIQUE", "4"))
MARKET_TAPE_SCOUT_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_SCOUT_MIN_TRACKED", "2"))
MARKET_TAPE_SCOUT_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_SCOUT_MIN_BUY_SOL", "1.5"))
MARKET_TAPE_SCOUT_CONFIRM_MIN_MULT = float(os.environ.get("MARKET_TAPE_SCOUT_CONFIRM_MIN_MULT", "1.003"))
MARKET_TAPE_SCOUT_TP_MULT = float(os.environ.get("MARKET_TAPE_SCOUT_TP_MULT", "1.065"))
MARKET_TAPE_SCOUT_FAST_KILL_SEC = float(os.environ.get("MARKET_TAPE_SCOUT_FAST_KILL_SEC", "2.0"))
MARKET_TAPE_SCOUT_FAST_KILL_PEAK = float(os.environ.get("MARKET_TAPE_SCOUT_FAST_KILL_PEAK", "1.012"))
MARKET_TAPE_SCOUT_TIMEOUT_SEC = float(os.environ.get("MARKET_TAPE_SCOUT_TIMEOUT_SEC", "8.0"))
MARKET_TAPE_MAX_OBSERVED_AGE_SEC = float(os.environ.get("MARKET_TAPE_MAX_OBSERVED_AGE_SEC", "12.0"))
MARKET_TAPE_BIRTH_ENABLED = os.environ.get("MARKET_TAPE_BIRTH_ENABLED", "1") == "1"
MARKET_TAPE_BIRTH_MAX_AGE_SEC = float(os.environ.get("MARKET_TAPE_BIRTH_MAX_AGE_SEC", "6.0"))
MARKET_TAPE_BIRTH_WINDOW_MS = int(os.environ.get("MARKET_TAPE_BIRTH_WINDOW_MS", "900"))
MARKET_TAPE_BIRTH_MIN_UNIQUE = int(os.environ.get("MARKET_TAPE_BIRTH_MIN_UNIQUE", "3"))
MARKET_TAPE_BIRTH_MIN_TRACKED = int(os.environ.get("MARKET_TAPE_BIRTH_MIN_TRACKED", "2"))
MARKET_TAPE_BIRTH_MIN_BUY_SOL = float(os.environ.get("MARKET_TAPE_BIRTH_MIN_BUY_SOL", "0.75"))
MARKET_TAPE_BIRTH_MAX_SELL_SOL = float(os.environ.get("MARKET_TAPE_BIRTH_MAX_SELL_SOL", "0.002"))
MARKET_TAPE_BIRTH_MIN_BC_MOVE = float(os.environ.get("MARKET_TAPE_BIRTH_MIN_BC_MOVE", "1.000"))
MARKET_TAPE_BIRTH_MAX_BC_MOVE = float(os.environ.get("MARKET_TAPE_BIRTH_MAX_BC_MOVE", "1.080"))
MARKET_TAPE_BIRTH_CONFIRM_DELAY_SEC = float(os.environ.get("MARKET_TAPE_BIRTH_CONFIRM_DELAY_SEC", "0.12"))
MARKET_TAPE_BIRTH_CONFIRM_MIN_MULT = float(os.environ.get("MARKET_TAPE_BIRTH_CONFIRM_MIN_MULT", "1.001"))
MOONSHOT_IGNITION_ENABLED = os.environ.get("MOONSHOT_IGNITION_ENABLED", "1") == "1"
MOONSHOT_IGNITION_AMOUNT_SOL = float(os.environ.get("MOONSHOT_IGNITION_AMOUNT_SOL", "0.01875"))
MOONSHOT_IGNITION_STRONG_AMOUNT_SOL = float(os.environ.get("MOONSHOT_IGNITION_STRONG_AMOUNT_SOL", "0.03125"))
MOONSHOT_IGNITION_MAX_AMOUNT_SOL = float(os.environ.get("MOONSHOT_IGNITION_MAX_AMOUNT_SOL", "0.040"))
MOONSHOT_MAX_AGE_SEC = float(os.environ.get("MOONSHOT_MAX_AGE_SEC", "7.0"))
MOONSHOT_WINDOW_MS = int(os.environ.get("MOONSHOT_WINDOW_MS", "1800"))
MOONSHOT_MAX_CACHE_AGE_MS = int(os.environ.get("MOONSHOT_MAX_CACHE_AGE_MS", "500"))
MOONSHOT_MIN_MOVE_MULT = float(os.environ.get("MOONSHOT_MIN_MOVE_MULT", "1.120"))
MOONSHOT_STRONG_MOVE_MULT = float(os.environ.get("MOONSHOT_STRONG_MOVE_MULT", "1.250"))
MOONSHOT_MAX_CHASE_MULT = float(os.environ.get("MOONSHOT_MAX_CHASE_MULT", "1.900"))
MOONSHOT_MAX_OFF_PEAK = float(os.environ.get("MOONSHOT_MAX_OFF_PEAK", "0.055"))
MOONSHOT_MIN_UNIQUE = int(os.environ.get("MOONSHOT_MIN_UNIQUE", "5"))
MOONSHOT_MIN_TRACKED = int(os.environ.get("MOONSHOT_MIN_TRACKED", "2"))
MOONSHOT_MIN_BUY_SOL = float(os.environ.get("MOONSHOT_MIN_BUY_SOL", "2.0"))
MOONSHOT_UNTRACKED_MIN_UNIQUE = int(os.environ.get("MOONSHOT_UNTRACKED_MIN_UNIQUE", "9"))
MOONSHOT_UNTRACKED_MIN_BUY_SOL = float(os.environ.get("MOONSHOT_UNTRACKED_MIN_BUY_SOL", "6.0"))
MOONSHOT_MAX_SELL_SOL = float(os.environ.get("MOONSHOT_MAX_SELL_SOL", "0.080"))
MOONSHOT_MAX_SELL_BUY_RATIO = float(os.environ.get("MOONSHOT_MAX_SELL_BUY_RATIO", "0.10"))
MOONSHOT_MIN_SCORE = int(os.environ.get("MOONSHOT_MIN_SCORE", "8"))
MOONSHOT_STRONG_SCORE = int(os.environ.get("MOONSHOT_STRONG_SCORE", "11"))
MOONSHOT_CONFIRM_DELAY_SEC = float(os.environ.get("MOONSHOT_CONFIRM_DELAY_SEC", "0.12"))
MOONSHOT_CONFIRM_MIN_MULT = float(os.environ.get("MOONSHOT_CONFIRM_MIN_MULT", "1.006"))
MOONSHOT_MIN_PRICE_RATIO = float(os.environ.get("MOONSHOT_MIN_PRICE_RATIO", "0.82"))
MOONSHOT_MAX_PRICE_RATIO = float(os.environ.get("MOONSHOT_MAX_PRICE_RATIO", "1.55"))
MOONSHOT_CONTEXT_COOLDOWN_SEC = float(os.environ.get("MOONSHOT_CONTEXT_COOLDOWN_SEC", "2.5"))
MOONSHOT_TIMEOUT_SEC = float(os.environ.get("MOONSHOT_TIMEOUT_SEC", "18.0"))
MOONSHOT_FAST_KILL_SEC = float(os.environ.get("MOONSHOT_FAST_KILL_SEC", "1.0"))
MOONSHOT_FAST_KILL_PEAK = float(os.environ.get("MOONSHOT_FAST_KILL_PEAK", "1.045"))
MOONSHOT_DROP_EXIT_MULT = float(os.environ.get("MOONSHOT_DROP_EXIT_MULT", "0.990"))
MOONSHOT_TP1_MULT = float(os.environ.get("MOONSHOT_TP1_MULT", "1.220"))
MOONSHOT_TP1_FRACTION = float(os.environ.get("MOONSHOT_TP1_FRACTION", "0.50"))
MOONSHOT_TP2_MULT = float(os.environ.get("MOONSHOT_TP2_MULT", "1.700"))
MOONSHOT_TP2_FRACTION = float(os.environ.get("MOONSHOT_TP2_FRACTION", "0.50"))
MOONSHOT_TRAIL_ACTIVATION = float(os.environ.get("MOONSHOT_TRAIL_ACTIVATION", "1.180"))
MOONSHOT_TRAIL_DISTANCE = float(os.environ.get("MOONSHOT_TRAIL_DISTANCE", "0.880"))
VELOCITY_IGNITION_ENABLED = os.environ.get("VELOCITY_IGNITION_ENABLED", "1") == "1"
VELOCITY_WINDOW_MS = int(os.environ.get("VELOCITY_WINDOW_MS", "1200"))
VELOCITY_MAX_CACHE_AGE_MS = int(os.environ.get("VELOCITY_MAX_CACHE_AGE_MS", "450"))
VELOCITY_MIN_MOVE_MULT = float(os.environ.get("VELOCITY_MIN_MOVE_MULT", "1.080"))
VELOCITY_STRONG_MOVE_MULT = float(os.environ.get("VELOCITY_STRONG_MOVE_MULT", "1.140"))
VELOCITY_MAX_CHASE_MULT = float(os.environ.get("VELOCITY_MAX_CHASE_MULT", "1.650"))
VELOCITY_MAX_OFF_PEAK = float(os.environ.get("VELOCITY_MAX_OFF_PEAK", "0.035"))
VELOCITY_MIN_UNIQUE = int(os.environ.get("VELOCITY_MIN_UNIQUE", "7"))
VELOCITY_STRONG_UNIQUE = int(os.environ.get("VELOCITY_STRONG_UNIQUE", "10"))
VELOCITY_MIN_TRACKED = int(os.environ.get("VELOCITY_MIN_TRACKED", "2"))
VELOCITY_MIN_BUY_SOL = float(os.environ.get("VELOCITY_MIN_BUY_SOL", "2.50"))
VELOCITY_STRONG_BUY_SOL = float(os.environ.get("VELOCITY_STRONG_BUY_SOL", "6.0"))
VELOCITY_MAX_SELL_SOL = float(os.environ.get("VELOCITY_MAX_SELL_SOL", "0.015"))
VELOCITY_MAX_SELL_BUY_RATIO = float(os.environ.get("VELOCITY_MAX_SELL_BUY_RATIO", "0.04"))
VELOCITY_CONFIRM_DELAY_SEC = float(os.environ.get("VELOCITY_CONFIRM_DELAY_SEC", "0.08"))
VELOCITY_CONFIRM_MIN_MULT = float(os.environ.get("VELOCITY_CONFIRM_MIN_MULT", "1.003"))
VELOCITY_AMOUNT_SOL = float(os.environ.get("VELOCITY_AMOUNT_SOL", "0.003125"))
VELOCITY_STRONG_AMOUNT_SOL = float(os.environ.get("VELOCITY_STRONG_AMOUNT_SOL", "0.00625"))
VELOCITY_TIMEOUT_SEC = float(os.environ.get("VELOCITY_TIMEOUT_SEC", "10.0"))
VELOCITY_FAST_KILL_SEC = float(os.environ.get("VELOCITY_FAST_KILL_SEC", "0.9"))
VELOCITY_FAST_KILL_PEAK = float(os.environ.get("VELOCITY_FAST_KILL_PEAK", "1.025"))
VELOCITY_DROP_EXIT_MULT = float(os.environ.get("VELOCITY_DROP_EXIT_MULT", "0.975"))
VELOCITY_TP1_MULT = float(os.environ.get("VELOCITY_TP1_MULT", "1.120"))
VELOCITY_TP1_FRACTION = float(os.environ.get("VELOCITY_TP1_FRACTION", "0.40"))
VELOCITY_TRAIL_ACTIVATION = float(os.environ.get("VELOCITY_TRAIL_ACTIVATION", "1.100"))
VELOCITY_TRAIL_DISTANCE = float(os.environ.get("VELOCITY_TRAIL_DISTANCE", "0.920"))
VELOCITY_SELL_PRESSURE_MIN_AGE_SEC = float(os.environ.get("VELOCITY_SELL_PRESSURE_MIN_AGE_SEC", "0.75"))
COPY_FAST_IGNITION_ENABLED = os.environ.get("COPY_FAST_IGNITION_ENABLED", "1") == "1"
COPY_FAST_IGNITION_MIN_MULT = float(os.environ.get("COPY_FAST_IGNITION_MIN_MULT", "1.300"))
COPY_FAST_IGNITION_STRONG_MULT = float(os.environ.get("COPY_FAST_IGNITION_STRONG_MULT", "1.550"))
COPY_FAST_IGNITION_MIN_SWARM = int(os.environ.get("COPY_FAST_IGNITION_MIN_SWARM", "4"))
COPY_FAST_IGNITION_FAST_MULT = float(os.environ.get("COPY_FAST_IGNITION_FAST_MULT", "1.450"))
COPY_FAST_IGNITION_FAST_SWARM = int(os.environ.get("COPY_FAST_IGNITION_FAST_SWARM", "4"))
COPY_FAST_IGNITION_STRONG_SWARM = int(os.environ.get("COPY_FAST_IGNITION_STRONG_SWARM", "5"))
COPY_FAST_IGNITION_MAX_CACHE_AGE_MS = int(os.environ.get("COPY_FAST_IGNITION_MAX_CACHE_AGE_MS", "250"))
COPY_FAST_IGNITION_AMOUNT_SOL = float(os.environ.get("COPY_FAST_IGNITION_AMOUNT_SOL", "0.00625"))
COPY_FAST_IGNITION_STRONG_AMOUNT_SOL = float(os.environ.get("COPY_FAST_IGNITION_STRONG_AMOUNT_SOL", "0.009375"))
COPY_FAST_IGNITION_CONFIRM_DELAY_SEC = float(os.environ.get("COPY_FAST_IGNITION_CONFIRM_DELAY_SEC", "0.08"))
COPY_FAST_IGNITION_CONFIRM_MIN_MULT = float(os.environ.get("COPY_FAST_IGNITION_CONFIRM_MIN_MULT", "1.003"))
SWARM_SCOUT_ENABLED = os.environ.get("SWARM_SCOUT_ENABLED", "1") == "1"
SWARM_SCOUT_MIN_SIGNERS = int(os.environ.get("SWARM_SCOUT_MIN_SIGNERS", "3"))
SWARM_SCOUT_AMOUNT_SOL = float(os.environ.get("SWARM_SCOUT_AMOUNT_SOL", str(COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL)))
SWARM_SCOUT_MIN_BC_MOVE = float(os.environ.get("SWARM_SCOUT_MIN_BC_MOVE", "1.015"))
SWARM_SCOUT_MAX_BC_MOVE = float(os.environ.get("SWARM_SCOUT_MAX_BC_MOVE", "1.120"))
SWARM_SCOUT_CONFIRM_DELAY_SEC = float(os.environ.get("SWARM_SCOUT_CONFIRM_DELAY_SEC", "0.35"))
SWARM_SCOUT_CONFIRM_MIN_MULT = float(os.environ.get("SWARM_SCOUT_CONFIRM_MIN_MULT", "1.003"))
PUMP_GRADUATION_ENABLED = os.environ.get("PUMP_GRADUATION_ENABLED", "0") == "1"
# V41.12c: ST clean mid-curve tokens grow over minutes/hours, not seconds. Extended timeout.
GRAD_TIMEOUT_ST_SEC = int(os.environ.get("GRAD_TIMEOUT_ST_SEC", "1800")) # 30 min hold for ST clean mints


async def graduation_snipe(client: Client, kp: Optional[Keypair], mint: str,
                           launchpad: str = "pump", signer: Optional[str] = None,
                           trader_price: float = 0.0):
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
    claimed_entry = False
    try:
        if launchpad == "copy_fast_swarm" and not COPY_FAST_SWARM_ENTRY_ENABLED:
            _swarm_override_entered.discard(mint)
            log(f"  GRAD SKIP {mint[:8]} (copy_fast_swarm): disabled; market_tape owns speed lane")
            return
        # V41.17za: copy_fast_swarm bypasses circuit breakers. These entries are
        # explicit overrides: capped 0.025 SOL, 30s hard timeout, dead-peak guard.
        # Streak-pause and consec_loss caps don't apply — we're knowingly riding
        # bundle pumps with bounded loss per trade.
        if launchpad != "copy_fast_swarm":
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
        if _mint_recently_closed(mint):
            if launchpad == "copy_fast_swarm":
                _swarm_override_entered.discard(mint)
            log(f"  GRAD SKIP {mint[:8]} ({launchpad}): closed within "
                f"{RECENT_CLOSE_REENTRY_COOLDOWN_SEC:.0f}s cooldown")
            return

        # V41.18: copy_fast is no longer allowed to be a blind entry. The last
        # paper run showed raw copy_fast was 0/3 and -0.0174 SOL, while confirmed
        # swarm follow-through produced the only positive lane. Treat every copy
        # signal as a candidate and require bc-cache proof before buying.
        if launchpad in ("copy_fast", "copy_fast_swarm"):
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
            # V41.17d Fix #11: slippage-vs-trader gate. Compute the price our 0.001 SOL
            # probe quote would yield, in lamports per smallest-unit token. Compare to
            # trader's actual buy price (parsed in _handle_copy_trader_tx). If our entry
            # would be > 5% above trader's price, the curve has already pumped past us —
            # we'd be entering at peak (the structural failure mode that produced the
            # peak=1.00x, -22% gap loss on 3t8wZxQJ). Skips when trader_price was
            # unparseable (multi-token tx, etc.) — fail-OPEN.
            if trader_price > 0:
                try:
                    probe_out = float(baseline_quote.get("outAmount", 0))
                    if probe_out > 0:
                        our_price = probe_sol_lamports / probe_out
                        ratio = our_price / trader_price
                        if ratio > COPY_FAST_MAX_PRICE_RATIO:
                            _copy_trade_stats["price_blocked"] += 1
                            log(f"  GRAD ABORT {mint[:8]} (copy_fast): our_px={our_price:.4e} "
                                f"trader_px={trader_price:.4e} ratio={ratio:.3f}x > "
                                f"{COPY_FAST_MAX_PRICE_RATIO:.2f}x — curve already moved past us")
                            return
                        # V41.17h: floor — token dumped >15% since trader's buy, momentum broken.
                        # CsZiG33J was 0.473x; would have caught it.
                        if ratio < COPY_FAST_MIN_PRICE_RATIO:
                            _copy_trade_stats["price_blocked"] += 1
                            log(f"  GRAD ABORT {mint[:8]} (copy_fast): our_px={our_price:.4e} "
                                f"trader_px={trader_price:.4e} ratio={ratio:.3f}x < "
                                f"{COPY_FAST_MIN_PRICE_RATIO:.2f}x — token dumped after trader, momentum broken")
                            return
                        log(f"  GRAD PRICE-OK {mint[:8]}: our/trader ratio={ratio:.3f}x")
                except Exception:
                    pass

            if not await _confirm_copy_fast_entry(
                    mint, launchpad, signal_time_ms,
                    signer=signer or "", trader_price=trader_price):
                if launchpad == "copy_fast_swarm":
                    _swarm_override_entered.discard(mint)
                return

            # V41.17z9: SWARM-OVERRIDE entries use HALF size (0.025 vs 0.05) for
            # risk management since they bypass the rug check.
            entry_override = _copy_fast_entry_overrides.pop(mint, None)
            solo_rocket = launchpad == "copy_fast" and mint in _copy_fast_solo_rocket_mints
            entry_quality = 8
            entry_reason = ""
            if entry_override:
                entry_launchpad = str(entry_override.get("launchpad") or "copy_fast_alpha")
                entry_amount = float(entry_override.get("amount") or COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL)
                entry_quality = int(entry_override.get("quality") or 8)
                entry_reason = str(entry_override.get("reason") or "")
                _copy_fast_solo_rocket_mints.discard(mint)
            else:
                entry_launchpad = "copy_fast_solo" if solo_rocket else launchpad
            if solo_rocket and not entry_override:
                entry_amount = COPY_FAST_SOLO_ROCKET_AMOUNT_SOL
                _copy_fast_solo_rocket_mints.discard(mint)
            elif not entry_override:
                if launchpad == "copy_fast_swarm":
                    entry_amount = GRAD_AMOUNT_SOL * 0.5
                elif launchpad == "copy_fast":
                    if not COPY_FAST_CONFIRMED_ENTRY_ENABLED:
                        log(f"  GRAD SKIP {mint[:8]} (copy_fast): raw confirmed entry disabled; "
                            "waiting for alpha/tape edge")
                        return
                    entry_amount = COPY_FAST_CONFIRMED_AMOUNT_SOL
                else:
                    entry_amount = GRAD_AMOUNT_SOL
            if not _claim_entry_mint(mint, launchpad):
                return
            claimed_entry = True

            # V41.17 Fix #2: warm pool fast-path. If pre-built tx is fresh, ship it.
            # CRITICAL: the warm path submits the tx itself (no second swap via buy_token).
            # Bookkeeping quote runs AFTER send so its latency doesn't delay entry.
            pos = None
            # Warm txs are built at GRAD_AMOUNT_SOL. Do not use them for capped
            # swarm overrides, or live mode can silently break the half-size cap.
            warm = (_consume_warm_swap_tx(mint)
                    if (entry_launchpad == "copy_fast"
                        and abs(entry_amount - GRAD_AMOUNT_SOL) < 1e-9
                        and not PAPER_TRADING and kp)
                    else None)
            if warm:
                tx_b64, _lvbh = warm
                log(f"  GRAD WARM HIT {mint[:8]} ({entry_launchpad}): pre-built tx, shipping immediately")
                # Skip simulation on warm path — Raptor already validated the tx; latency wins
                sig_out = execute_swap(kp, client, tx_b64, simulate_first=False)
                if sig_out:
                    # Post-send bookkeeping — quote latency now irrelevant for entry timing
                    entry_lamports = int(entry_amount * 10**WSOL_DECIMALS)
                    bookkeep = jupiter_quote(SOL_MINT, mint, entry_lamports)
                    out_amt = float(bookkeep.get("outAmount", 0)) if bookkeep else 0.0
                    if out_amt > 0:
                        entry_price = entry_lamports / out_amt
                        pos = Position(
                            mint=mint, entry_price=entry_price,
                            entry_amount_sol=entry_amount, token_amount=out_amt,
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
                reason_suffix = f" | {entry_reason}" if entry_reason else ""
                log(f"  GRAD ENTRY {mint[:8]} ({entry_launchpad}): confirmed follow-through, "
                    f"buying {entry_amount} SOL{reason_suffix}")
                pos = buy_token(kp, client, mint, entry_amount)
                if not pos:
                    log(f"  GRAD BUY FAILED {mint[:8]}")
                    _release_entry_mint(mint)
                    return
            pos.strategy = "graduation"
            pos.late_scalp = True
            pos.entry_progress = 1.0
            pos.entry_size_sol = entry_amount
            pos.quality_score = entry_quality
            pos.launchpad = entry_launchpad
            # V41.17 Fix #9: stamp signal time so manage_graduation_position can apply
            # the no-pump time-stops after we actually enter. Confirm-gated copy
            # lanes can wait 2-3s before buying; using the original signal time
            # caused immediate fast-kill exits on fresh positions.
            pos.signal_time_ms = int(time.time() * 1000)
            _store_open_position(pos)
            _record_entry_opened()
            asyncio.create_task(manage_graduation_position(client, kp, pos))
            return

        log(f"  GRAD WAIT {mint[:8]}: holding {GRAD_INITIAL_DELAY_SEC}s for Jupiter to index PumpSwap pool")
        await asyncio.sleep(GRAD_INITIAL_DELAY_SEC)

        # V41.17w: smart-buyer-only pump grad lane REMOVED (0/225 in V41.17v overnight —
        # fresh graduations have no time for buyers to develop realized PnL > 1 SOL).
        # pump/bonk/st_pump launchpads now fall through to the existing momentum-confirmed
        # entry path below (40-50% sweet spot for default, sustained uptrend for st_pump,
        # etc.). Pre-V41.17t behavior restored.

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

        # V41.17z5: launchpad-specific entry path.
        #   - "pump" / "bonk" (default graduation lane): ENTER IMMEDIATELY (no 5s
        #     observation, no momentum gate). Yesterday's OHLCV backtest showed
        #     graduation peaks happen 1-5 minutes post-grad — every second of
        #     observation costs us upside %. Dead-peak guard (V41.17z2) handles
        #     the no-movement cases at -2 to -4% capped loss.
        #   - "momentum" / "grad_imminent" / "st_pump": KEEP existing 5s observe +
        #     specialized momentum/uptrend confirmation (these lanes have other
        #     entry signals that need confirmation).
        if launchpad in ("momentum", "grad_imminent", "st_pump"):
            log(f"  GRAD OBSERVE {mint[:8]}: baseline set, waiting 5s for momentum confirmation")
            await asyncio.sleep(5)
            confirm_quote = jupiter_quote(SOL_MINT, mint, probe_sol_lamports)
            if not confirm_quote or float(confirm_quote.get("outAmount", 0)) == 0:
                log(f"  GRAD SKIP {mint[:8]}: Jupiter route disappeared during observe window")
                return
            confirm_tokens_per_001 = float(confirm_quote["outAmount"])
            price_change = (baseline_tokens_per_001 / confirm_tokens_per_001) - 1.0 if confirm_tokens_per_001 else 0.0
            if launchpad == "momentum":
                if price_change < -0.10:
                    log(f"  GRAD SKIP {mint[:8]}: collapsed during observe ({price_change*100:+.1f}%)")
                    return
                log(f"  GRAD MOMENTUM OK {mint[:8]} (momentum): {price_change*100:+.1f}% — riding hot token")
            elif launchpad == "grad_imminent":
                if price_change < -0.05:
                    log(f"  GRAD SKIP {mint[:8]}: dumping pre-grad ({price_change*100:+.1f}% in 5s)")
                    return
                log(f"  GRAD MOMENTUM OK {mint[:8]} (grad_imminent): {price_change*100:+.1f}% — entering pre-grad")
            elif launchpad == "st_pump":
                log(f"  GRAD ST-CONFIRM {mint[:8]}: T+5s={price_change*100:+.1f}%, watching 10 more")
                await asyncio.sleep(10)
                tx_quote = jupiter_quote(SOL_MINT, mint, probe_sol_lamports)
                if not tx_quote or float(tx_quote.get("outAmount", 0)) == 0:
                    log(f"  GRAD SKIP {mint[:8]}: Jupiter route lost during ST confirmation")
                    return
                tx_tokens_per_001 = float(tx_quote["outAmount"])
                price_change_15 = (baseline_tokens_per_001 / tx_tokens_per_001) - 1.0 if tx_tokens_per_001 else 0.0
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
            # V41.17z5: default graduation lane — enter on the spot, no observation.
            log(f"  GRAD INSTANT-ENTRY {mint[:8]} ({launchpad}): no observation, dead-peak guard owns downside")

        if not _claim_entry_mint(mint, launchpad):
            return
        claimed_entry = True
        log(f"  GRAD ENTRY {mint[:8]} ({launchpad}): buying {GRAD_AMOUNT_SOL} SOL")
        pos = buy_token(kp, client, mint, GRAD_AMOUNT_SOL)
        if not pos:
            log(f"  GRAD BUY FAILED {mint[:8]}")
            _release_entry_mint(mint)
            return
        # Mark this as a graduation snipe — uses GRAD ladder, GRAD timeout
        pos.strategy = "graduation"
        pos.late_scalp = True   # use tight scalp-style exits
        pos.entry_progress = 1.0   # already graduated
        pos.entry_size_sol = GRAD_AMOUNT_SOL
        pos.quality_score = 8     # graduated tokens are inherently higher quality
        pos.launchpad = launchpad
        # V41.17z4: stamp signal time so dead-peak guard works for grad lane too
        pos.signal_time_ms = int(time.time() * 1000)
        _store_open_position(pos)
        _record_entry_opened()
        asyncio.create_task(manage_graduation_position(client, kp, pos))
    except Exception as e:
        if claimed_entry:
            _release_entry_mint(mint)
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
    rung_hit = pos.rung_hit

    def try_grad_sell(reason: str, fraction: float, multiplier: float) -> bool:
        nonlocal close_reason, closed
        if positions.get(pos.mint) is not pos:
            closed = True
            return False
        if pos.mint in _positions_closing:
            return False
        if pos.remaining_pct <= 0.01:
            close_reason = reason
            closed = True
            return True
        will_close = fraction >= 0.999 or pos.remaining_pct * (1 - fraction) <= 0.01
        if will_close:
            _positions_closing.add(pos.mint)
        sol_recv = sell_token(kp, client, pos, fraction, current_multiplier=multiplier)
        if sol_recv is None:
            if will_close:
                _positions_closing.discard(pos.mint)
            log(f"  GRAD SELL FAILED ({reason}) {pos.mint[:8]} — keeping position")
            return False
        pos.realized_sol += sol_recv
        pos.remaining_pct *= (1 - fraction)
        close_reason = reason
        _persist_positions()
        if will_close:
            closed = True
        return True

    last_quote_check = 0.0

    def poll_delay() -> float:
        if pos.launchpad in ("copy_fast_solo", "copy_fast_alpha"):
            return GRAD_SWARM_POLL_SEC
        if pos.launchpad in ("market_tape", "market_tape_scout", "moonshot_ignition", "velocity_ignition"):
            return GRAD_SWARM_POLL_SEC
        if pos.launchpad == "copy_fast_swarm":
            return GRAD_SWARM_POLL_SEC
        if pos.launchpad == "copy_fast":
            return GRAD_COPY_FAST_POLL_SEC
        return GRAD_MANAGER_POLL_SEC

    async def current_price(force_quote: bool = False) -> Optional[tuple[float, str]]:
        nonlocal last_quote_check
        if pos.launchpad in ("copy_fast", "copy_fast_solo", "copy_fast_alpha", "copy_fast_swarm",
                             "market_tape", "market_tape_scout", "moonshot_ignition",
                             "velocity_ignition"):
            cached = _bc_cache_price_for_pos(pos)
            if cached:
                price, complete, age_ms = cached
                if not complete:
                    return price, f"bc_cache:{age_ms}ms"
        now = time.time()
        if (not force_quote
                and pos.launchpad in ("copy_fast", "copy_fast_solo", "copy_fast_alpha", "copy_fast_swarm",
                                      "market_tape", "market_tape_scout", "moonshot_ignition",
                                      "velocity_ignition")
                and now - last_quote_check < GRAD_JUPITER_FALLBACK_SEC):
            return None
        last_quote_check = now
        probe_qty = max(int(pos.token_amount * max(pos.remaining_pct, 0.01) * 0.01), 1)
        quote = await asyncio.to_thread(jupiter_quote, pos.mint, SOL_MINT, probe_qty)
        if not quote or float(quote.get("outAmount", 0)) == 0:
            return None
        return float(quote["outAmount"]) / probe_qty, "jupiter"

    while not closed:
        try:
            if positions.get(pos.mint) is not pos:
                return
            if pos.mint in _positions_closing:
                await asyncio.sleep(poll_delay())
                continue
            now = time.time()
            elapsed = now - open_time
            # V41.13-14: ST, grad-imminent, momentum, copy_fast entries have NO timeout.
            if pos.launchpad in ("st_pump", "grad_imminent", "momentum", "copy_fast"):
                timeout_for_pos = float("inf")
            elif pos.launchpad == "copy_fast_solo":
                timeout_for_pos = COPY_FAST_SOLO_ROCKET_TIMEOUT_SEC
            elif pos.launchpad == "copy_fast_alpha":
                timeout_for_pos = COPY_FAST_ALPHA_TIMEOUT_SEC
            elif pos.launchpad == "market_tape":
                timeout_for_pos = MARKET_TAPE_TIMEOUT_SEC
            elif pos.launchpad == "market_tape_scout":
                timeout_for_pos = MARKET_TAPE_SCOUT_TIMEOUT_SEC
            elif pos.launchpad == "moonshot_ignition":
                timeout_for_pos = MOONSHOT_TIMEOUT_SEC
            elif pos.launchpad == "velocity_ignition":
                timeout_for_pos = VELOCITY_TIMEOUT_SEC
            else:
                timeout_for_pos = GRAD_TIMEOUT_SEC

            price_info = await current_price(force_quote=elapsed > timeout_for_pos)
            if not price_info:
                await asyncio.sleep(poll_delay())
                continue
            if positions.get(pos.mint) is not pos or pos.remaining_pct <= 0.01:
                return
            sol_per_unit, price_source = price_info
            pos.last_price = sol_per_unit
            multiplier = sol_per_unit / pos.entry_price if pos.entry_price else 1.0
            if multiplier > pos.peak_price:
                pos.peak_price = multiplier

            if elapsed > timeout_for_pos:
                if try_grad_sell(f"GRAD TIMEOUT {timeout_for_pos}s mult={multiplier:.2f}x src={price_source}", 1.0, multiplier):
                    break

            # V41.17z2 DEAD-PEAK GUARD: 5s time-stop with peak<1.005x threshold.
            # Empirical pattern across 6 live trades (2W/4L):
            #   - All WINS peaked >=1.05x within 4-7 seconds (well above 1.005)
            #   - All LOSSES had peak=1.00x EXACTLY (curve never moved up post-entry)
            #     and bled to SL at -9% to -11% in 3-12 seconds.
            # A 5s/peak<1.005 exit catches every dead-peak loss without touching
            # any winner. This is much TIGHTER than the V41.17v version (8s/1.04)
            # which closed real winners; live data now confirms the proper params.
            DEAD_PEAK_TIME_SEC = 5.0
            # V41.17zc: tightened 1.005 -> 1.020. Live data (HCqzUnse peak 1.01,
            # CVZHqoMv peak 1.000, both crashed -22% to -26% past SL) showed
            # the 1.005 threshold leaked too many false-pump-then-crash entries.
            DEAD_PEAK_THRESHOLD = 1.020
            # V41.17z9: hard 30s timeout for swarm-override entries (bundle pumps
            # are typically over within 30-60s; cap exposure)
            if (pos.launchpad == "copy_fast_swarm"
                    and pos.signal_time_ms > 0
                    and (now * 1000 - pos.signal_time_ms) > 30_000):
                if try_grad_sell(
                    f"GRAD SWARM-TIMEOUT 30s exit (peak={pos.peak_price:.3f}x mult={multiplier:.3f}x)",
                    1.0, multiplier,
                ):
                    break
            if (pos.launchpad == "moonshot_ignition"
                    and pos.signal_time_ms > 0
                    and (now * 1000 - pos.signal_time_ms) > MOONSHOT_FAST_KILL_SEC * 1000
                    and pos.peak_price < MOONSHOT_FAST_KILL_PEAK):
                if try_grad_sell(
                    f"MOONSHOT FAST-KILL {MOONSHOT_FAST_KILL_SEC:.1f}s "
                    f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if (pos.launchpad == "velocity_ignition"
                    and pos.signal_time_ms > 0
                    and (now * 1000 - pos.signal_time_ms) > VELOCITY_FAST_KILL_SEC * 1000
                    and pos.peak_price < VELOCITY_FAST_KILL_PEAK):
                if try_grad_sell(
                    f"VELOCITY FAST-KILL {VELOCITY_FAST_KILL_SEC:.1f}s "
                    f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if (pos.launchpad in ("market_tape", "market_tape_scout")
                    and pos.signal_time_ms > 0
                    and (now * 1000 - pos.signal_time_ms) > (
                        MARKET_TAPE_SCOUT_FAST_KILL_SEC if pos.launchpad == "market_tape_scout"
                        else MARKET_TAPE_FAST_KILL_SEC
                    ) * 1000
                    and pos.peak_price < (
                        MARKET_TAPE_SCOUT_FAST_KILL_PEAK if pos.launchpad == "market_tape_scout"
                        else MARKET_TAPE_FAST_KILL_PEAK
                    )):
                fast_kill_sec = (
                    MARKET_TAPE_SCOUT_FAST_KILL_SEC if pos.launchpad == "market_tape_scout"
                    else MARKET_TAPE_FAST_KILL_SEC
                )
                if try_grad_sell(
                    f"{pos.launchpad.upper()} FAST-KILL {fast_kill_sec:.1f}s "
                    f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if (pos.launchpad == "copy_fast_solo"
                    and pos.signal_time_ms > 0
                    and (now * 1000 - pos.signal_time_ms) > COPY_FAST_SOLO_ROCKET_FAST_KILL_SEC * 1000
                    and pos.peak_price < COPY_FAST_SOLO_ROCKET_FAST_KILL_PEAK):
                if try_grad_sell(
                    f"COPY_FAST_SOLO FAST-KILL {COPY_FAST_SOLO_ROCKET_FAST_KILL_SEC:.1f}s "
                    f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if (pos.launchpad == "copy_fast_alpha"
                    and pos.signal_time_ms > 0
                    and (now * 1000 - pos.signal_time_ms) > COPY_FAST_ALPHA_FAST_KILL_SEC * 1000
                    and pos.peak_price < COPY_FAST_ALPHA_FAST_KILL_PEAK):
                if try_grad_sell(
                    f"COPY_FAST_ALPHA FAST-KILL {COPY_FAST_ALPHA_FAST_KILL_SEC:.1f}s "
                    f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if (pos.launchpad in ("copy_fast", "copy_fast_swarm", "pump", "bonk",
                                  "grad_imminent", "momentum", "st_pump", "market_tape",
                                  "market_tape_scout", "moonshot_ignition",
                                  "velocity_ignition", "copy_fast_solo", "copy_fast_alpha")
                    and pos.signal_time_ms > 0
                    and pos.peak_price < DEAD_PEAK_THRESHOLD):
                age_s = (now * 1000 - pos.signal_time_ms) / 1000
                if age_s > DEAD_PEAK_TIME_SEC:
                    if try_grad_sell(
                        f"GRAD DEAD-PEAK exit (age={age_s:.1f}s peak={pos.peak_price:.3f}x mult={multiplier:.3f}x)",
                        1.0, multiplier,
                    ):
                        break
                    await asyncio.sleep(poll_delay())
                    continue

            # V41.13o + V41.14d: TRAILING STOP with slippage protection.
            # Empirical: full-position sell into $5-15k pool slips 2-5%. If trail floor is
            # 1.01x but slippage takes us back to 0.98x, "trail win" becomes real loss.
            # Require: trail_floor must be > 1 + GRAD_TRAILING_MIN_LOCK (clear slippage).
            change = (sol_per_unit - pos.entry_price) / pos.entry_price
            if pos.launchpad == "moonshot_ignition":
                alpha_runner = (
                    pos.entry_amount_sol <= COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL * 1.10
                    and pos.quality_score <= 8
                )
                if alpha_runner and pos.rung_hit == 0 and multiplier >= ALPHA_RUNNER_TP1_MULT:
                    sell_frac = max(0.0, min(1.0, ALPHA_RUNNER_TP1_FRACTION))
                    log(f"  ALPHA-RUNNER TP1 mult={multiplier:.3f}x: selling {sell_frac*100:.0f}%")
                    if try_grad_sell(f"ALPHA-RUNNER TP1 {ALPHA_RUNNER_TP1_MULT:.3f}x mult={multiplier:.3f}x",
                                     sell_frac, multiplier):
                        pos.rung_hit = 1
                        rung_hit = max(rung_hit, 1)
                        _persist_positions()
                        if pos.remaining_pct <= 0.01:
                            break
                if pos.rung_hit == 0 and multiplier >= MOONSHOT_TP1_MULT:
                    sell_frac = max(0.0, min(1.0, MOONSHOT_TP1_FRACTION))
                    log(f"  MOONSHOT TP1 mult={multiplier:.3f}x: selling {sell_frac*100:.0f}%")
                    if try_grad_sell(f"MOONSHOT TP1 {MOONSHOT_TP1_MULT:.3f}x mult={multiplier:.3f}x",
                                     sell_frac, multiplier):
                        pos.rung_hit = 1
                        rung_hit = max(rung_hit, 1)
                        _persist_positions()
                        if pos.remaining_pct <= 0.01:
                            break
                if pos.rung_hit == 1 and multiplier >= MOONSHOT_TP2_MULT:
                    sell_frac = max(0.0, min(1.0, MOONSHOT_TP2_FRACTION))
                    log(f"  MOONSHOT TP2 mult={multiplier:.3f}x: selling {sell_frac*100:.0f}%")
                    if try_grad_sell(f"MOONSHOT TP2 {MOONSHOT_TP2_MULT:.3f}x mult={multiplier:.3f}x",
                                     sell_frac, multiplier):
                        pos.rung_hit = 2
                        rung_hit = max(rung_hit, 2)
                        _persist_positions()
                        if pos.remaining_pct <= 0.01:
                            break
                if pos.peak_price >= MOONSHOT_TRAIL_ACTIVATION:
                    trail_floor = max(1.08, pos.peak_price * MOONSHOT_TRAIL_DISTANCE)
                    if multiplier <= trail_floor:
                        if try_grad_sell(
                            f"MOONSHOT TRAIL exit floor={trail_floor:.3f}x "
                            f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                            1.0, multiplier,
                        ):
                            break
                elif multiplier <= MOONSHOT_DROP_EXIT_MULT:
                    if try_grad_sell(f"MOONSHOT DROP EXIT mult={multiplier:.3f}x", 1.0, multiplier):
                        break
            if pos.launchpad == "velocity_ignition":
                if pos.rung_hit == 0 and multiplier >= VELOCITY_TP1_MULT:
                    sell_frac = max(0.0, min(1.0, VELOCITY_TP1_FRACTION))
                    log(f"  VELOCITY TP1 mult={multiplier:.3f}x: selling {sell_frac*100:.0f}%")
                    if try_grad_sell(f"VELOCITY TP1 {VELOCITY_TP1_MULT:.3f}x mult={multiplier:.3f}x",
                                     sell_frac, multiplier):
                        pos.rung_hit = 1
                        rung_hit = max(rung_hit, 1)
                        _persist_positions()
                        if pos.remaining_pct <= 0.01:
                            break
                if pos.peak_price >= VELOCITY_TRAIL_ACTIVATION:
                    trail_floor = max(1.035, pos.peak_price * VELOCITY_TRAIL_DISTANCE)
                    if multiplier <= trail_floor:
                        if try_grad_sell(
                            f"VELOCITY TRAIL exit floor={trail_floor:.3f}x "
                            f"peak={pos.peak_price:.3f}x mult={multiplier:.3f}x",
                            1.0, multiplier,
                        ):
                            break
                elif multiplier <= VELOCITY_DROP_EXIT_MULT:
                    if try_grad_sell(f"VELOCITY DROP EXIT mult={multiplier:.3f}x", 1.0, multiplier):
                        break
            if pos.launchpad in ("market_tape", "market_tape_scout"):
                tape_tp_mult = (
                    MARKET_TAPE_SCOUT_TP_MULT if pos.launchpad == "market_tape_scout"
                    else MARKET_TAPE_TP_MULT
                )
                if multiplier >= tape_tp_mult and try_grad_sell(
                    f"{pos.launchpad.upper()} TP {tape_tp_mult:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if pos.launchpad == "copy_fast_solo" and multiplier >= COPY_FAST_SOLO_ROCKET_TP_MULT:
                if try_grad_sell(
                    f"COPY_FAST_SOLO TP {COPY_FAST_SOLO_ROCKET_TP_MULT:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if pos.launchpad == "copy_fast_alpha" and multiplier >= COPY_FAST_ALPHA_TP_MULT:
                if try_grad_sell(
                    f"COPY_FAST_ALPHA TP {COPY_FAST_ALPHA_TP_MULT:.3f}x mult={multiplier:.3f}x",
                    1.0, multiplier,
                ):
                    break
            if pos.launchpad not in ("moonshot_ignition", "velocity_ignition") and pos.peak_price >= GRAD_TRAILING_ACTIVATION:
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
            elif pos.launchpad not in ("moonshot_ignition", "velocity_ignition") and change <= GRAD_SL_PCT:
                if try_grad_sell(f"GRAD SL hit {change*100:.1f}% mult={multiplier:.2f}x", 1.0, multiplier):
                    break

            # TP ladder — uses CURRENT price not peak (V40 fix preserved).
            # V41.7/9: bonk launchpad uses 1% trade fee vs pump.fun 0.3%, so TP threshold
            # gets a +2% offset to net the same realised profit after fees.
            fee_offset = 0.02 if pos.launchpad in ("bonk", "bonk_pregrad") else 0.0
            if pos.launchpad not in ("moonshot_ignition", "velocity_ignition") and rung_hit < len(GRAD_TP_LADDER):
                trigger, sell_frac = GRAD_TP_LADDER[rung_hit]
                effective_trigger = trigger + fee_offset
                if multiplier >= effective_trigger:
                    log(f"  GRAD TP RUNG {rung_hit+1} ({pos.launchpad}) mult={multiplier:.2f}x trigger={effective_trigger:.2f}x: selling {sell_frac*100:.0f}% of remaining")
                    if try_grad_sell(f"GRAD TP RUNG {rung_hit+1} mult={multiplier:.2f}x", sell_frac, multiplier):
                        rung_hit += 1
                        pos.rung_hit = rung_hit
                        _persist_positions()
                        if pos.remaining_pct <= 0.01:
                            break

            await asyncio.sleep(poll_delay())
        except Exception as e:
            log(f"  manage_graduation_position err: {e}")
            await asyncio.sleep(5)

    pnl = pos.realized_sol - pos.entry_amount_sol
    if positions.get(pos.mint) is not pos:
        return
    _record_trade_close(pnl)
    log(f"  CLOSED GRAD {pos.mint[:8]} peak={pos.peak_price:.2f}x recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} "
        f"pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} W={session_wins} L={session_losses} reason={close_reason}")
    _remove_open_position(pos)
    _maybe_stop_for_daily_loss()


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
        blocked, _reason = _entry_circuit_breakers_open()
        if blocked: return
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
            _store_open_position(pos)
            _record_entry_opened()
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
                            if not PUMP_GRADUATION_ENABLED:
                                continue
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
                            if not PUMP_GRADUATION_ENABLED:
                                continue
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
        blocked, _reason = _entry_circuit_breakers_open()
        if blocked: return
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
            _store_open_position(pos)
            _record_entry_opened()
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
                        _persist_positions()
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
    _record_trade_close(pnl)
    log(f"  CLOSED {pos.mint[:8]} strategy={pos.strategy} peak={pos.peak_price:.2f}x recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} "
        f"pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} W={session_wins} L={session_losses} "
        f"reason={close_reason}")
    _remove_open_position(pos)
    _maybe_stop_for_daily_loss()


async def session_reporter():
    """Periodic session summary so user can see PnL in realtime."""
    while True:
        await asyncio.sleep(60)
        log(f"=== SESSION: pnl={session_pnl_sol:+.4f} SOL | W={session_wins} L={session_losses} | "
            f"daily={daily_pnl_sol:+.4f}/{-MAX_DAILY_LOSS_SOL:.4f} SOL | open={len(positions)} | "
            f"dump_watch={len(dump_bounce_active)} | consec_loss={consec_losses} ===")
        # V41.15c: copy-trade pipeline diagnostics — see where shreds are dropped
        s = _copy_trade_stats
        if s["shreds"] > 0:
            log(f"=== COPY-PIPELINE: shreds={s['shreds']} sig_dedup={s['sig_dedup']} excpt={s['exception']} "
                f"no_meta={s['no_meta']} wrong_signer={s['wrong_signer']} no_buy={s['no_buy']} "
                f"non_memecoin={s['non_memecoin']} dedup={s['dedup']} rug_blocked={s['rug_blocked']} fired={s['fired']} "
                f"confirm_ok={s.get('confirm_ok', 0)} confirm_blocked={s.get('confirm_blocked', 0)} "
                f"confirm_dump={s.get('confirm_dump_blocked', 0)} "
                f"mt_seen={s.get('market_tape_seen', 0)} mt_trig={s.get('market_tape_triggers', 0)} "
                f"mt_ent={s.get('market_tape_entered', 0)} mt_exit={s.get('market_tape_exits', 0)} "
                f"mt_birth={s.get('market_tape_birth_triggers', 0)} "
                f"moon_cand={s.get('moonshot_candidates', 0)} moon_trig={s.get('moonshot_triggers', 0)} "
                f"moon_blk={s.get('moonshot_blocked', 0)} "
                f"vel_cand={s.get('velocity_candidates', 0)} vel_trig={s.get('velocity_triggers', 0)} "
                f"vel_blk={s.get('velocity_blocked', 0)} "
                f"cf_ign={s.get('copy_fast_ignition', 0)} "
                f"mt_blk={s.get('market_tape_blocked', 0)} ===")
            log(f"=== MARKET-TAPE-GATES: pos={s.get('mt_pos', 0)} cd={s.get('mt_cooldown', 0)} "
                f"rate={s.get('mt_rate', 0)} uniq={s.get('mt_no_unique', 0)} "
                f"tracked={s.get('mt_no_tracked', 0)} flow={s.get('mt_flow', 0)} "
                f"no_bc={s.get('mt_no_bc', 0)} complete={s.get('mt_complete', 0)} "
                f"bc_rng={s.get('mt_bc_range', 0)} low={s.get('mt_weak_low', 0)} "
                f"mid={s.get('mt_weak_mid', 0)} high={s.get('mt_weak_high', 0)} "
                f"no_px={s.get('mt_no_price', 0)} "
                f"ratio={s.get('mt_ratio', 0)} stale={s.get('mt_stale', 0)} "
                f"close={s.get('mt_recent_close', 0)} "
                f"confirm={s.get('mt_confirm', 0)} trig={s.get('market_tape_triggers', 0)} ===")
            log(f"=== MOONSHOT-GATES: age={s.get('moon_age', 0)} flow={s.get('moon_flow', 0)} "
                f"sell={s.get('moon_sell', 0)} no_bc={s.get('moon_no_bc', 0)} "
                f"complete={s.get('moon_complete', 0)} move_lo={s.get('moon_move_low', 0)} "
                f"chase={s.get('moon_chase', 0)} off_peak={s.get('moon_off_peak', 0)} "
                f"ratio={s.get('moon_ratio', 0)} score={s.get('moon_score', 0)} "
                f"confirm={s.get('moon_confirm', 0)} ctx_cd={s.get('moon_context_cd', 0)} ===")
            log(f"=== SWARM-SCOUT: cand={s.get('swarm_scout_candidates', 0)} "
                f"trig={s.get('swarm_scout_triggers', 0)} "
                f"no_px={s.get('swarm_scout_no_price', 0)} "
                f"rng={s.get('swarm_scout_range', 0)} "
                f"rng_lo={s.get('swarm_scout_range_low', 0)} "
                f"rng_hi={s.get('swarm_scout_range_high', 0)} "
                f"ratio={s.get('swarm_scout_ratio', 0)} "
                f"confirm={s.get('swarm_scout_confirm', 0)} "
                f"busy={s.get('swarm_scout_busy', 0)} ===")
            log(f"=== ALPHA: shadow={s.get('alpha_shadow', 0)} outcomes={s.get('alpha_outcomes', 0)} "
                f"no_px={s.get('alpha_no_price', 0)} promoted={s.get('alpha_promoted', 0)} "
                f"toxic={s.get('alpha_toxic', 0)} scouts={s.get('alpha_scouts', 0)} "
                f"pending={len(_alpha_pending_keys)} ===")


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
    # V41.17f: weak_scalp killed (env: WEAK_SCALP_ENABLED=1 to revive).
    # V41.17v: V40 momentum killed (env: MOMENTUM_ENABLED=1 to revive).
    disabled_strategies = {"micro_probe", "very_late_micro"}
    if not WEAK_SCALP_ENABLED:
        disabled_strategies.add("weak_scalp")
    if not MOMENTUM_ENABLED:
        disabled_strategies.add("momentum")
    if strategy in disabled_strategies:
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
            _store_open_position(pos)
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
            _store_open_position(pos)
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
        will_close = fraction >= 0.999 or pos.remaining_pct * (1 - fraction) <= 0.01
        if will_close:
            _positions_closing.add(pos.mint)
        sol_recv = sell_token(kp, client, pos, fraction, current_multiplier=multiplier)
        if not safe_record_sell(pos, sol_recv):
            if will_close:
                _positions_closing.discard(pos.mint)
            sell_failures += 1
            log(f"  SELL FAILED but V40 keeps position open ({reason}); failures={sell_failures}")
            return False
        pos.remaining_pct *= (1 - fraction)
        sell_failures = 0
        close_reason = reason
        _persist_positions()
        if will_close:
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
                            _persist_positions()
                            multiplier = current_price / pos.entry_price if pos.entry_price else multiplier

            # V41.17j: Fix #9 ported into V40. 8s no-pump time-stop catches the dead-peak
            # pattern that bled -$0.22 each on weak_scalp (14s) and momentum (24s). Faster
            # exit caps loss at ~-$0.10 instead. Excludes late_breakout — that strategy
            # legitimately can take 10-15s to start pumping (HTNtt1C1 winner this session
            # might've been killed by an 8s gate); keep its existing 30s timeout.
            if (pos.strategy != "late_breakout"
                    and elapsed > TIME_STOP_NO_PUMP_SEC
                    and pos.peak_price < GRAD_TRAILING_ACTIVATION):
                reason = f"V40 8s NO-PUMP exit (age={elapsed:.1f}s peak={pos.peak_price:.3f}x mult={multiplier:.3f}x)"
                log(f"  {reason} {pos.mint[:8]}")
                if try_sell_fraction(reason, 1.0, multiplier): break

            # No-momentum bailout: V40 treats 1.00x exits as losses in paper, so there is
            # no reason to sit in flat tokens. (Slower fallback for late_breakout and any
            # strategies the 8s gate skipped.)
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
    _remove_open_position(pos)
    _maybe_stop_for_daily_loss()


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
COPY_TRADE_TOP_N = int(os.environ.get("COPY_TRADE_TOP_N", "200"))  # V41.17w: bumped 100→200 per ST Recipe A. 1 conn covers ~100 comfortably; pushing to 200 to expand signal volume. Watch shred volume — if dropped, dial back to 150.
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
WARM_SWAP_AMOUNT_SOL = float(os.environ.get("WARM_SWAP_AMOUNT_SOL", str(MARKET_TAPE_AMOUNT_SOL)))
WARM_PRIORITY_TTL_SEC = float(os.environ.get("WARM_PRIORITY_TTL_SEC", "45"))
WARM_POOL_REFRESH_SEC = float(os.environ.get("WARM_POOL_REFRESH_SEC", "1.0"))
# Fix #3: smart-wallet exit pre-flight (overlap with quote — no added latency)
EXIT_CHECK_TIMEOUT_SEC = float(os.environ.get("EXIT_CHECK_TIMEOUT_SEC", "0.25"))
EXIT_CHECK_ENABLED = os.environ.get("EXIT_CHECK_ENABLED", "1") == "1"
# Fix #4: curve % gate at signal time
COPY_FAST_MAX_CURVE_PCT = float(os.environ.get("COPY_FAST_MAX_CURVE_PCT", "75.0"))
# V41.17t: smart-buyer-confirmed graduation entry. /first-buyers/{mint} gives the
# first 100 buyers WITH PnL inline. Count those with realized>1 SOL on this token
# AND still holding > 0 — these are winners who are still believing. Threshold of 3
# means 3 distinct profitable holders confirms organic demand. Token-anchored signal:
# we don't need to know WHICH wallets are smart in advance — the data tells us per token.
COPY_FAST_SMART_BUYER_MIN = int(os.environ.get("COPY_FAST_SMART_BUYER_MIN", "1"))
# V41.17v: smart-buyer slippage gate. /first-buyers returns each buyer's
# cost_basis (their avg buy price). Compute the average across smart buyers,
# compare to our probe quote price. If our entry would be > 1.10x the smart
# buyers' average, the curve has already pumped past us — abort. Same
# principle as Fix #11 for copy_fast, applied to graduation entries.
SMART_BUYER_MAX_PRICE_RATIO = float(os.environ.get("SMART_BUYER_MAX_PRICE_RATIO", "1.10"))
# Fix #11: slippage-vs-trader gate. The first live copy_fast loss (3t8wZxQJ -22% in 2s,
# peak=1.00x) showed the structural pattern from memory: smart wallet's buy IS the pump,
# we follow at +1s, curve has already topped. Solution: compare our probe quote price to
# the trader's actual buy price (parsed from their tx pre/post balances). If our entry
# price > trader * 1.05, abort — curve moved against us, we'd be entering at peak.
COPY_FAST_MAX_PRICE_RATIO = float(os.environ.get("COPY_FAST_MAX_PRICE_RATIO", "1.10"))
# V41.17h: bidirectional. Second observed loss (CsZiG33J -3.6% capped by Fix #9) showed
# the inverse pattern: ratio=0.473x meant the curve had ALREADY DUMPED 53% between
# trader's buy and ours. Catching a falling knife — momentum broken, no rebound. Floor
# at 0.85 catches obvious post-trader dumps while leaving margin for normal post-buy
# settling (which moves ratio to ~0.95-1.00 on large trader buys, never to 0.85).
COPY_FAST_MIN_PRICE_RATIO = float(os.environ.get("COPY_FAST_MIN_PRICE_RATIO", "0.80"))
# Fix #5: tighter wallet allowlist filter (uses existing top-traders metrics)
COPY_TRADE_MIN_REALIZED_SOL = float(os.environ.get("COPY_TRADE_MIN_REALIZED_SOL", "1.0"))
# V41.17o: REVERTED WR 0.40 → 0.50. V41.17m bumped pool 56 → 90 but shred volume
# unexpectedly dropped 5-10x. User-flagged hypothesis: ST shredSubscribe throttles
# accountInclude > ~75 wallets. Reverting to 56 (V41.17h known-good level). If
# volume returns at 56 wallets, hypothesis confirmed and we accept 56 as the cap.
COPY_TRADE_MIN_WIN_RATE_TIGHT = float(os.environ.get("COPY_TRADE_MIN_WIN_RATE_TIGHT", "0.50"))
# V41.17p: activity filter. The leaderboard /top-traders/all is HISTORICAL — ranks
# by all-time realized PnL, ignoring whether the wallet is currently active. Direct
# /wallet/{addr}/trades check showed 8/10 of our top wallets last traded 24h-122d
# ago. Effective live pool was ~10-15 of 56. Adding inactivity ceiling: drop any
# wallet whose most recent trade was > MAX_INACTIVITY_HOURS ago. Pool drops to
# 15-25 active wallets but shred density per wallet rises ~10×.
COPY_TRADE_MAX_INACTIVITY_HOURS = int(os.environ.get("COPY_TRADE_MAX_INACTIVITY_HOURS", "24"))
# Fix #6: bundle freshness — abort if any bundle in last N seconds.
# V41.17g: 60s → 30s. The 60s threshold treated "bundle 4s ago" the same as
# "bundle 34s ago", which is too coarse. After 30s the bundle's actual buy txs
# are 6+ blocks deep, so the coordinated-entry risk window is largely past.
# Empirical session 01:26-01:45 had 7 fresh-bundle blocks: ages 4,10,16,18,34,34,(re)1.
# Two of those (the 34s ones) likely had survivable signal; recovering ~28% volume.
BUNDLE_FRESHNESS_THRESHOLD_SEC = int(os.environ.get("BUNDLE_FRESHNESS_THRESHOLD_SEC", "30"))
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
_st_data_api_backoff_until = 0.0
_st_data_api_backoff_reason = ""
_st_data_api_backoff_last_log = 0.0
# Wallets currently warm in /stream/swap (single set for all subs on the connection)
_warm_subscribed_mints: set = set()
_warm_priority_mints: dict[str, float] = {}


def _mark_warm_priority_mint(mint: str) -> None:
    if mint:
        _warm_priority_mints[mint] = time.time()


def _st_auth_headers() -> dict:
    return {"x-api-key": SOLANATRACKER_API_KEY.strip()}


def _st_data_api_blocked(path: str = "") -> bool:
    global _st_data_api_backoff_last_log
    now = time.time()
    if now >= _st_data_api_backoff_until:
        return False
    if now - _st_data_api_backoff_last_log >= ST_DATA_API_BACKOFF_LOG_SEC:
        _st_data_api_backoff_last_log = now
        suffix = f" [{path}]" if path else ""
        log(f"  ST Data API backoff{suffix}: {int(_st_data_api_backoff_until - now)}s remaining "
            f"({_st_data_api_backoff_reason})")
    return True


def _st_note_data_api_status(path: str, status_code: int, body: str = "") -> None:
    global _st_data_api_backoff_until, _st_data_api_backoff_reason, _st_data_api_backoff_last_log
    if status_code not in (401, 403, 429):
        return
    now = time.time()
    reason = f"HTTP {status_code}"
    clean_body = (body or "").replace("\n", " ").replace("\r", " ").strip()
    if clean_body:
        reason = f"{reason}: {clean_body[:120]}"
    body_lc = clean_body.lower()
    if "insufficient credit" in body_lc or "insufficient credits" in body_lc:
        wait_sec = ST_DATA_API_CREDIT_BACKOFF_SEC
    else:
        wait_sec = ST_DATA_API_RATE_BACKOFF_SEC if status_code == 429 else ST_DATA_API_AUTH_BACKOFF_SEC
    until = now + wait_sec
    if until > _st_data_api_backoff_until:
        _st_data_api_backoff_until = until
        _st_data_api_backoff_reason = reason
        _st_data_api_backoff_last_log = now
        log(f"  ST Data API backoff set for {wait_sec:.0f}s [{path}]: {reason}")


def _st_fetch(path: str):
    if _st_data_api_blocked(path):
        return None
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}{path}",
            headers=_st_auth_headers(),
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        body = (r.text or "").replace("\n", " ").replace("\r", " ").strip()
        _st_note_data_api_status(path, r.status_code, body)
        if r.status_code not in (401, 403, 429):
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
    Returns flat list of wallet entries. Costs 1 ST API call per 25 traders.
    V41.17w: overfetch by 1.5× so post-filter (WR>=50% AND realized>=1 SOL) we
    can still hit `needed` wallets. Caps at 20 pages = 500 wallets (full leaderboard).
    V41.17w fix: retry per-page on HTTP 429 (up to 2 retries with backoff). On a
    page that finally fails, CONTINUE to subsequent pages rather than abort —
    losing 25 wallets is better than losing the rest of the leaderboard."""
    if _st_data_api_blocked("/top-traders/all"):
        return None
    overfetch = max(needed, int(needed * 1.5))
    pages_needed = max(1, min(20, (overfetch + 24) // 25))
    all_wallets = []
    for page in range(1, pages_needed + 1):
        if _st_data_api_blocked(f"/top-traders/all/{page}"):
            break
        attempt = 0
        while attempt < 3:
            if _st_data_api_blocked(f"/top-traders/all/{page}"):
                break
            try:
                r = requests.get(
                    f"{SOLANATRACKER_BASE}/top-traders/all/{page}",
                    headers=_st_auth_headers(),
                    params={"expandPnl": "true"},
                    timeout=15,
                )
                if r.status_code == 429:
                    _st_note_data_api_status(f"/top-traders/all/{page}", r.status_code, r.text or "")
                    backoff = 4 * (attempt + 1)  # 4s, 8s, 12s
                    log(f"  COPY-TRADE fetch 429 page={page} attempt={attempt+1}, backing off {backoff}s")
                    time.sleep(backoff)
                    attempt += 1
                    continue
                if r.status_code != 200:
                    _st_note_data_api_status(f"/top-traders/all/{page}", r.status_code, r.text or "")
                    log(f"  COPY-TRADE fetch err page={page}: HTTP {r.status_code} (skipping page)")
                    break  # break inner retry loop, continue to next page
                data = r.json()
                wallets = data.get("wallets", []) if isinstance(data, dict) else []
                if not wallets:
                    log(f"  COPY-TRADE fetch page={page}: empty (end of leaderboard)")
                    return {"wallets": all_wallets} if all_wallets else None
                all_wallets.extend(wallets)
                break  # success, exit retry loop
            except Exception as e:
                log(f"  COPY-TRADE fetch err page={page} attempt={attempt+1}: {type(e).__name__}: {e}")
                attempt += 1
                time.sleep(3)
        else:
            log(f"  COPY-TRADE fetch gave up on page={page} after 3 attempts — continuing")
        time.sleep(3.0)  # V41.17w: 2.5→3.0 — broader margin against /tokens/multi/all collisions
    return {"wallets": all_wallets} if all_wallets else None


# ============================================================================
# V41.17x: ACTIVE-SNIPER POOL — fixes the wrong assumption that /top-traders/all
# (sorted by all-time PnL) gives us currently-active memecoin snipers. Audit
# proved 77% of that pool is INACTIVE (median 0 trades/24h). Real grad snipers
# are found by aggregating /top-traders/{mint} across recent graduations.
# ============================================================================

ACTIVE_SNIPER_POOL_FILE = "active_snipers.txt"
ACTIVE_SNIPER_REFRESH_HOURS = float(os.environ.get("ACTIVE_SNIPER_REFRESH_HOURS", "1.0"))
ACTIVE_SNIPER_MIN_GRAD_HITS = int(os.environ.get("ACTIVE_SNIPER_MIN_GRAD_HITS", "1"))
ACTIVE_SNIPER_GRAD_SAMPLE = int(os.environ.get("ACTIVE_SNIPER_GRAD_SAMPLE", "200"))
ACTIVE_SNIPER_TOP_N_PER_GRAD = int(os.environ.get("ACTIVE_SNIPER_TOP_N_PER_GRAD", "25"))
ACTIVE_SNIPER_POOL_ENABLED = os.environ.get("ACTIVE_SNIPER_POOL_ENABLED", "1") == "1"


def _load_active_sniper_pool() -> list[str]:
    """Read active_snipers.txt → list of wallet addresses (sorted by frequency desc)."""
    if not os.path.exists(ACTIVE_SNIPER_POOL_FILE):
        return []
    out = []
    try:
        for line in open(ACTIVE_SNIPER_POOL_FILE, "r", encoding="utf-8"):
            parts = line.strip().split("\t")
            if parts and len(parts[0]) >= 32:
                out.append(parts[0])
    except Exception as e:
        log(f"  active-sniper file read err: {type(e).__name__}: {e}")
    return out


def _st_build_active_sniper_pool() -> list[str]:
    """V41.17x: build a current active-sniper pool by aggregating /top-traders/{mint}
    across recent graduations. Returns wallets appearing in >= ACTIVE_SNIPER_MIN_GRAD_HITS
    graduations' top-10. Costs ~80 ST API calls per build.

    Replaces /top-traders/all (all-time PnL → 77% inactive). Audit data:
    median trades/24h was 0 in old pool, 472 in active pool.
    """
    from collections import Counter
    if _st_data_api_blocked("active-sniper build"):
        return []
    # Step 1: gather recent graduations + actively trending tokens (V41.17z7).
    # Pull from /tokens/multi/all (graduated + graduating + latest) AND
    # /tokens/trending/5m for currently-hot tokens. Broader sample = more
    # diverse wallet coverage = higher swarm correlation rate.
    mints: set[str] = set()
    for ep in ["/tokens/multi/all?limit=500", "/tokens/trending/5m"]:
        if _st_data_api_blocked(ep):
            break
        try:
            r = requests.get(f"{SOLANATRACKER_BASE}{ep}",
                             headers=_st_auth_headers(), timeout=15)
            if r.status_code != 200:
                _st_note_data_api_status(ep, r.status_code, r.text or "")
                continue
            d = r.json()
            if isinstance(d, dict):
                for cat in ("graduated", "graduating", "latest"):
                    for t in (d.get(cat) or []):
                        m = (t.get("token") or {}).get("mint")
                        if m and m.lower().endswith("pump"):
                            mints.add(m)
            elif isinstance(d, list):
                for t in d:
                    m = (t.get("token") or {}).get("mint") if isinstance(t, dict) else None
                    if m and m.lower().endswith("pump"):
                        mints.add(m)
            time.sleep(1.5)
        except Exception as e:
            log(f"  active-sniper {ep} err: {type(e).__name__}: {e}")
    grads = list(mints)[: ACTIVE_SNIPER_GRAD_SAMPLE]
    if not grads:
        log("  active-sniper build: no graduations found, returning empty")
        return []
    # Step 2: aggregate top-traders for each
    counter: Counter[str] = Counter()
    skipped = 0
    for i, mint in enumerate(grads):
        if _st_data_api_blocked(f"/top-traders/{mint[:8]}"):
            break
        try:
            r = requests.get(f"{SOLANATRACKER_BASE}/top-traders/{mint}",
                             headers=_st_auth_headers(), timeout=10)
            if r.status_code == 429:
                _st_note_data_api_status(f"/top-traders/{mint}", r.status_code, r.text or "")
                time.sleep(4); continue
            if r.status_code != 200:
                _st_note_data_api_status(f"/top-traders/{mint}", r.status_code, r.text or "")
                skipped += 1; continue
            d = r.json()
            traders = d if isinstance(d, list) else (d.get("traders") if isinstance(d, dict) else [])
            if not isinstance(traders, list): traders = []
            for t in traders[:ACTIVE_SNIPER_TOP_N_PER_GRAD]:
                w = t.get("wallet")
                if w:
                    counter[w] += 1
        except Exception:
            skipped += 1
        time.sleep(0.8)
    if not counter:
        return []
    # Step 3: filter by frequency
    candidates = [w for w, c in counter.items() if c >= ACTIVE_SNIPER_MIN_GRAD_HITS]
    candidates.sort(key=lambda w: -counter[w])
    log(f"  active-sniper build: {len(counter)} unique wallets across "
        f"{len(grads)-skipped}/{len(grads)} grads, {len(candidates)} active "
        f"(>= {ACTIVE_SNIPER_MIN_GRAD_HITS} grads)")
    # Step 4: persist for next startup
    try:
        with open(ACTIVE_SNIPER_POOL_FILE, "w", encoding="utf-8") as f:
            for w in candidates:
                f.write(f"{w}\t{counter[w]}\n")
    except Exception as e:
        log(f"  active-sniper file write err: {type(e).__name__}: {e}")
    return candidates


async def active_sniper_refresh_loop():
    """Background task — rebuild active sniper pool every ACTIVE_SNIPER_REFRESH_HOURS.
    Active grad snipers shift over time; without refresh, pool decays."""
    if not ACTIVE_SNIPER_POOL_ENABLED:
        return
    while True:
        try:
            await asyncio.sleep(ACTIVE_SNIPER_REFRESH_HOURS * 3600)
            log("ACTIVE-SNIPER refresh starting (background)")
            t0 = time.time()
            pool = await asyncio.to_thread(_st_build_active_sniper_pool)
            log(f"ACTIVE-SNIPER refresh done in {time.time()-t0:.0f}s — {len(pool)} wallets")
        except Exception as e:
            log(f"active_sniper_refresh_loop err: {type(e).__name__}: {e}")


def _first_buyers_smart_count(mint: str, min_realized: float = 1.0) -> tuple[int, int, float]:
    """V41.17t/v: count smart buyers among first 100 buyers of a token via /first-buyers/{mint}.
    A 'smart buyer' has BOTH realized > min_realized SOL on this token AND is still
    holding > 0 (winning AND believing — not the same as 'made money then dumped').

    Returns (smart_count, total_buyers, avg_cost_basis_of_smart_buyers).
    avg_cost_basis = 0.0 if no smart buyers. Caller can compare our probe quote price
    to this average to detect "curve already pumped past smart buyers".

    On error returns (0, 0, 0.0).

    Uses ST's /first-buyers endpoint which returns PnL DATA INLINE — no per-wallet
    /pnl calls needed. Single call per graduating token (~100ms-2s)."""
    if _st_data_api_blocked(f"/first-buyers/{mint[:8]}"):
        return (0, 0, 0.0)
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/first-buyers/{mint}",
            headers=_st_auth_headers(),
            timeout=5,
        )
        if r.status_code != 200:
            _st_note_data_api_status(f"/first-buyers/{mint[:8]}", r.status_code, r.text or "")
            return (0, 0, 0.0)
        data = r.json()
        if isinstance(data, list):
            buyers = data
        elif isinstance(data, dict):
            buyers = data.get("buyers") or data.get("data") or []
        else:
            buyers = []
        if not buyers:
            return (0, 0, 0.0)
        smart = 0
        cost_basis_sum = 0.0
        cost_basis_count = 0
        for b in buyers:
            realized = b.get("realized") or 0
            holding = b.get("holding") or 0
            if realized > min_realized and holding > 0:
                smart += 1
                cb = b.get("cost_basis") or 0
                if cb > 0:
                    cost_basis_sum += float(cb)
                    cost_basis_count += 1
        avg_cb = cost_basis_sum / cost_basis_count if cost_basis_count > 0 else 0.0
        return (smart, len(buyers), avg_cb)
    except Exception:
        return (0, 0, 0.0)


def _wallet_hot_signal(wallet: str) -> Optional[dict]:
    """V41.17r: pull /pnl?showHistoricPnL for a wallet. Returns recent-perf dict or None.
    Returns:
      {'new1d_pnl': USD, 'new7d_pnl': USD, 'wr_1d': %, 'wr_7d': %}
    Path within response: historic.summary.{1d|7d}.newTokens.total_pnl
    The newTokens.total_pnl is the right signal for memecoin sniping — PnL on tokens
    BOUGHT in the window (vs total which mixes in older holdings).

    V41.17s: timeout 60s → 8s. Massive-history wallets (e.g., suqh5sHtr8 with
    100k+ trades) take 30-60s per call and stack up; with 100 wallets the filter
    was taking 7+ minutes. 8s catches the fast 95% and skips the slow tail
    (those wallets get marked as 'err' and dropped — acceptable trade-off for
    reliable 2-3min boot)."""
    if _st_data_api_blocked(f"/pnl/{wallet[:8]}"):
        return None
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/pnl/{wallet}",
            headers=_st_auth_headers(),
            params={"showHistoricPnL": "true", "hideDetails": "true"},
            timeout=8,
        )
        if r.status_code != 200:
            _st_note_data_api_status(f"/pnl/{wallet[:8]}", r.status_code, r.text or "")
            return None
        d = r.json()
        hs = (d.get("historic") or {}).get("summary") or {}
        d1 = hs.get("1d") or {}
        d7 = hs.get("7d") or {}
        new1d_pnl = ((d1.get("newTokens") or {}).get("total_pnl")) or 0
        new7d_pnl = ((d7.get("newTokens") or {}).get("total_pnl")) or 0
        wr_1d = d1.get("winPercentage") or 0
        wr_7d = d7.get("winPercentage") or 0
        return {"new1d_pnl": new1d_pnl, "new7d_pnl": new7d_pnl,
                "wr_1d": wr_1d, "wr_7d": wr_7d}
    except Exception:
        return None


def _wallet_last_trade_ms(wallet: str) -> Optional[int]:
    """V41.17p: most-recent trade timestamp for a wallet via /wallet/{addr}/trades.
    Returns ms-epoch of trades[0].time, or None on error / no trades."""
    if _st_data_api_blocked(f"/wallet/{wallet[:8]}/trades"):
        return None
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/wallet/{wallet}/trades",
            headers=_st_auth_headers(),
            timeout=10,
        )
        if r.status_code != 200:
            _st_note_data_api_status(f"/wallet/{wallet[:8]}/trades", r.status_code, r.text or "")
            return None
        d = r.json()
        trades = d.get("trades", []) or []
        if not trades:
            return None
        ts = trades[0].get("time")
        return int(ts) if ts else None
    except Exception:
        return None


_copy_trader_seen_sigs: set = set()

# V41.17z7: per-mint signer history for swarm detection.
# mint -> list of (signer_str, ts_ms). Pruned to last 60s on each access.
_signer_history_per_mint: dict[str, list] = {}

# V41.17z9: track recently rug-blocked mints — if SWARM-3+ forms within 30s,
# we override the rug-block and enter with risk-capped sizing.
# mint -> ts_ms when first rug-blocked
_rug_blocked_recent: dict[str, int] = {}

# V41.17z9: track which mints we've already swarm-overridden so we don't
# re-enter the same one repeatedly.
_swarm_override_entered: set = set()
_copy_fast_solo_rocket_mints: set = set()
_swarm_scout_pending: set = set()

# V41.17zb: track mints with a pending swarm-sustain check (3s wait).
# Prevents re-scheduling the same delayed-entry coroutine for a mint.
_swarm_pending: set = set()

# V41.19: market-wide direct pump.fun tape. This is the speed lane: parse every
# direct buy/sell from ST shreds, build a sub-second per-mint tape, and enter
# clustered organic pressure before waiting for a copy-trade confirmation.
_market_tape_per_mint: dict[str, deque] = defaultdict(lambda: deque(maxlen=80))
_market_tape_first_seen_ms: dict[str, int] = {}
_market_tape_last_seen_ms: dict[str, int] = {}
_market_tape_entered_recent: dict[str, int] = {}
_market_tape_ratio_violation_until: dict[str, int] = {}
_market_tape_alpha_context_recent_ms: dict[str, int] = {}
_moonshot_context_recent_ms: dict[str, int] = {}
_market_tape_entry_times: deque = deque(maxlen=200)
_copy_fast_entry_overrides: dict[str, dict] = {}

_alpha_stats: dict[str, dict[str, dict]] = {"wallets": {}, "contexts": {}, "pairs": {}}
_alpha_pending_keys: set[str] = set()
_alpha_recent_signal_ms: dict[str, int] = {}
_alpha_outcomes_since_save = 0

_copy_trade_stats = {
    "shreds": 0, "no_meta": 0, "wrong_signer": 0, "no_buy": 0,
    "non_memecoin": 0, "dedup": 0, "fired": 0, "exception": 0,
    "sig_dedup": 0, "rug_blocked": 0,
    # V41.17: new gate counters
    "curve_blocked": 0, "exit_blocked": 0, "first_buyer_blocked": 0,
    "bundle_fresh_blocked": 0, "warm_hit": 0, "warm_miss": 0,
    # V41.17d Fix #11: price-ratio (slippage-vs-trader) blocks
    "price_blocked": 0, "trader_price_unparseable": 0,
    # V41.18 confirm-then-enter gate
    "confirm_ok": 0, "confirm_blocked": 0, "confirm_dump_blocked": 0,
    # V41.19 market-wide tape
    "market_tape_seen": 0, "market_tape_triggers": 0, "market_tape_entered": 0,
    "market_tape_blocked": 0, "market_tape_birth_triggers": 0,
    "moonshot_candidates": 0, "moonshot_triggers": 0, "moonshot_blocked": 0,
    "velocity_candidates": 0, "velocity_triggers": 0, "velocity_blocked": 0,
    "copy_fast_ignition": 0,
    # V41.20 executable-alpha learner
    "alpha_shadow": 0, "alpha_outcomes": 0, "alpha_no_price": 0,
    "alpha_promoted": 0, "alpha_toxic": 0, "alpha_scouts": 0,
}


def _mt_gate(name: str) -> None:
    _copy_trade_stats[name] = _copy_trade_stats.get(name, 0) + 1


def _alpha_empty_stat() -> dict:
    return {
        "n": 0,
        "wins": 0,
        "best_net_sum": 0.0,
        "exit_net_sum": 0.0,
        "worst_net_sum": 0.0,
        "last_best_net": 0.0,
        "last_exit_net": 0.0,
        "last_ts": 0.0,
    }


def _alpha_load_state() -> None:
    if not ALPHA_LEARNER_ENABLED or not os.path.isfile(ALPHA_STATE_FILE):
        return
    try:
        with open(ALPHA_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        stats = payload.get("stats") or {}
        for bucket in ("wallets", "contexts", "pairs"):
            if isinstance(stats.get(bucket), dict):
                _alpha_stats[bucket] = stats[bucket]
        total = sum(int(v.get("n", 0) or 0) for v in _alpha_stats.get("pairs", {}).values())
        log(f"  ALPHA RESTORE: loaded executable-edge stats ({total} pair outcomes)")
    except Exception as e:
        log(f"  ALPHA RESTORE ERR: {type(e).__name__}: {e}")


def _alpha_persist_state(force: bool = False) -> None:
    global _alpha_outcomes_since_save
    if not ALPHA_LEARNER_ENABLED:
        return
    if not force and _alpha_outcomes_since_save < ALPHA_SAVE_EVERY_OUTCOMES:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(ALPHA_STATE_FILE)), exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "stats": _alpha_stats,
        }
        tmp_path = ALPHA_STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp_path, ALPHA_STATE_FILE)
        _alpha_outcomes_since_save = 0
    except Exception as e:
        log(f"  ALPHA PERSIST ERR: {type(e).__name__}: {e}")


def _alpha_bucket(value: Optional[float], cuts: tuple[float, ...], labels: tuple[str, ...]) -> str:
    if value is None:
        return "na"
    for cut, label in zip(cuts, labels):
        if value < cut:
            return label
    return labels[-1]


def _alpha_context_key(lane: str, mint: str, signer: str,
                       trader_price: float = 0.0,
                       trigger_price: float = 0.0) -> str:
    now_ms = int(time.time() * 1000)
    first_seen = _market_tape_first_seen_ms.get(mint)
    age_s = ((now_ms - first_seen) / 1000.0) if first_seen else None
    age_b = _alpha_bucket(age_s, (3.0, 6.0, 12.0, 30.0), ("a0_3", "a3_6", "a6_12", "a12_30", "a30p"))
    swarm_n = len(_recent_swarm_events(mint, 10.0, now_ms)) if "_recent_swarm_events" in globals() else 0
    swarm_b = "sw0" if swarm_n <= 0 else ("sw1" if swarm_n == 1 else ("sw2" if swarm_n == 2 else ("sw3" if swarm_n == 3 else "sw4p")))
    move_mult = None
    bc_move = _bc_cache_move_for_mint(mint, 1800, max(MARKET_TAPE_BC_CACHE_MAX_AGE_MS, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS))
    if bc_move:
        move_mult = float(bc_move[0])
    move_b = _alpha_bucket(move_mult, (0.98, 1.02, 1.08, 1.20), ("dump", "flat", "rise", "run", "chase"))
    ratio = (trigger_price / trader_price) if trader_price and trigger_price else None
    ratio_b = _alpha_bucket(ratio, (0.82, 0.95, 1.08, 1.20), ("dumped", "cheap", "fair", "high", "chase"))
    return f"{lane}|{age_b}|{swarm_b}|{move_b}|{ratio_b}"


def _alpha_stat_view(stat: Optional[dict]) -> tuple[int, float, float, float]:
    if not stat:
        return 0, 0.0, 0.0, 0.0
    n = int(stat.get("n", 0) or 0)
    wins = int(stat.get("wins", 0) or 0)
    wr = wins / n if n else 0.0
    avg_best = float(stat.get("best_net_sum", 0.0) or 0.0) / n if n else 0.0
    avg_exit = float(stat.get("exit_net_sum", 0.0) or 0.0) / n if n else 0.0
    return n, wr, avg_best, avg_exit


def _alpha_bypass_static_guards(n: int, wr: float, avg_exit: float) -> bool:
    return (
        n >= MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_SAMPLES
        and wr >= MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_WR
        and avg_exit >= MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_AVG_EXIT
    )


def _alpha_promoted(stat: Optional[dict], min_samples: int = ALPHA_MIN_SAMPLES) -> bool:
    n, wr, avg_best, _avg_exit = _alpha_stat_view(stat)
    return n >= min_samples and wr >= ALPHA_PROMOTE_MIN_WR and avg_best >= ALPHA_PROMOTE_MIN_AVG_BEST_NET


def _alpha_context_only_promoted(stat: Optional[dict]) -> bool:
    n, wr, avg_best, _avg_exit = _alpha_stat_view(stat)
    return (
        n >= ALPHA_CONTEXT_ONLY_MIN_SAMPLES
        and wr >= ALPHA_CONTEXT_ONLY_MIN_WR
        and avg_best >= ALPHA_CONTEXT_ONLY_MIN_AVG_BEST_NET
    )


def _alpha_wallet_only_promoted(stat: Optional[dict]) -> bool:
    n, wr, _avg_best, avg_exit = _alpha_stat_view(stat)
    return (
        n >= ALPHA_WALLET_ONLY_MIN_SAMPLES
        and wr >= ALPHA_WALLET_ONLY_MIN_WR
        and avg_exit >= ALPHA_WALLET_ONLY_MIN_AVG_EXIT_NET
    )


def _alpha_toxic(stat: Optional[dict]) -> bool:
    n, wr, avg_best, _avg_exit = _alpha_stat_view(stat)
    return n >= ALPHA_BLOCK_MIN_SAMPLES and wr <= ALPHA_BLOCK_MAX_WR and avg_best <= ALPHA_BLOCK_MAX_AVG_BEST_NET


def _alpha_update_one(bucket: str, key: str, best_net: float,
                      exit_net: float, worst_net: float) -> None:
    stat = _alpha_stats.setdefault(bucket, {}).setdefault(key, _alpha_empty_stat())
    stat["n"] = int(stat.get("n", 0) or 0) + 1
    if best_net >= 0.02:
        stat["wins"] = int(stat.get("wins", 0) or 0) + 1
    stat["best_net_sum"] = float(stat.get("best_net_sum", 0.0) or 0.0) + best_net
    stat["exit_net_sum"] = float(stat.get("exit_net_sum", 0.0) or 0.0) + exit_net
    stat["worst_net_sum"] = float(stat.get("worst_net_sum", 0.0) or 0.0) + worst_net
    stat["last_best_net"] = best_net
    stat["last_exit_net"] = exit_net
    stat["last_ts"] = time.time()


def _alpha_update_stats(signer: str, context: str,
                        best_net: float, exit_net: float, worst_net: float) -> None:
    global _alpha_outcomes_since_save
    if not signer:
        signer = "unknown"
    _alpha_update_one("wallets", signer, best_net, exit_net, worst_net)
    _alpha_update_one("contexts", context, best_net, exit_net, worst_net)
    _alpha_update_one("pairs", f"{signer}|{context}", best_net, exit_net, worst_net)
    _alpha_outcomes_since_save += 1
    _copy_trade_stats["alpha_outcomes"] = _copy_trade_stats.get("alpha_outcomes", 0) + 1
    _alpha_persist_state()


async def _alpha_shadow_track(mint: str, signer: str, lane: str,
                              trader_price: float = 0.0,
                              trigger_price: float = 0.0,
                              sig: str = "") -> None:
    if not ALPHA_LEARNER_ENABLED:
        return
    key = sig or f"{lane}:{signer}:{mint}:{int(time.time() * 2)}"
    if key in _alpha_pending_keys:
        return
    if len(_alpha_pending_keys) >= ALPHA_MAX_PENDING_SHADOWS:
        return
    _alpha_pending_keys.add(key)
    _copy_trade_stats["alpha_shadow"] = _copy_trade_stats.get("alpha_shadow", 0) + 1
    try:
        start_price = trigger_price
        if start_price <= 0:
            cached = _bc_cache_price_for_mint(mint, max(MARKET_TAPE_BC_CACHE_MAX_AGE_MS, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS))
            if cached and not cached[1]:
                start_price = float(cached[0])
        if start_price <= 0:
            await asyncio.sleep(0.20)
            cached = _bc_cache_price_for_mint(mint, max(MARKET_TAPE_BC_CACHE_MAX_AGE_MS, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS))
            if cached and not cached[1]:
                start_price = float(cached[0])
        if start_price <= 0:
            _copy_trade_stats["alpha_no_price"] = _copy_trade_stats.get("alpha_no_price", 0) + 1
            return
        context = _alpha_context_key(lane, mint, signer, trader_price, start_price)
        start_ts = time.time()
        samples: list[float] = []
        for horizon in (1.0, 2.0, 5.0, 10.0):
            await asyncio.sleep(max(0.0, start_ts + horizon - time.time()))
            cached = _bc_cache_price_for_mint(mint, 1600)
            if cached and not cached[1] and cached[0] > 0:
                samples.append(float(cached[0]) / start_price)
        if not samples:
            _copy_trade_stats["alpha_no_price"] = _copy_trade_stats.get("alpha_no_price", 0) + 1
            return
        drag = max(0.0, PAPER_ROUND_TRIP_DRAG_BPS / 10000.0)
        best_net = max(samples) * (1.0 - drag) - 1.0
        exit_net = samples[-1] * (1.0 - drag) - 1.0
        worst_net = min(samples) * (1.0 - drag) - 1.0
        _alpha_update_stats(signer, context, best_net, exit_net, worst_net)
    except Exception as e:
        log(f"  ALPHA SHADOW ERR {mint[:8]}: {type(e).__name__}: {e}")
    finally:
        _alpha_pending_keys.discard(key)


def _alpha_schedule_shadow(mint: str, signer: str, lane: str,
                           trader_price: float = 0.0,
                           trigger_price: float = 0.0,
                           sig: str = "") -> None:
    if not ALPHA_LEARNER_ENABLED:
        return
    now_ms = int(time.time() * 1000)
    cooldown_key = f"{lane}:{signer}:{mint}"
    if now_ms - _alpha_recent_signal_ms.get(cooldown_key, 0) < ALPHA_SIGNAL_COOLDOWN_MS:
        return
    _alpha_recent_signal_ms[cooldown_key] = now_ms
    if len(_alpha_recent_signal_ms) > 5000:
        cutoff = now_ms - 10 * 60_000
        for k, ts in list(_alpha_recent_signal_ms.items()):
            if ts < cutoff:
                _alpha_recent_signal_ms.pop(k, None)
    asyncio.create_task(_alpha_shadow_track(
        mint, signer, lane, trader_price=trader_price,
        trigger_price=trigger_price, sig=sig,
    ))


def _alpha_entry_plan(mint: str, signer: str, lane: str,
                      trader_price: float, trigger_price: float,
                      last_mult: float, swarm_count: int,
                      off_peak_ok: bool) -> Optional[dict]:
    if not (ALPHA_LEARNER_ENABLED and ALPHA_ADAPTIVE_ENTRY_ENABLED):
        return None
    if not off_peak_ok or last_mult < COPY_FAST_ALPHA_MIN_ENTRY_MULT:
        return None
    context = _alpha_context_key(lane, mint, signer, trader_price, trigger_price)
    wallet_stat = _alpha_stats.get("wallets", {}).get(signer)
    context_stat = _alpha_stats.get("contexts", {}).get(context)
    pair_stat = _alpha_stats.get("pairs", {}).get(f"{signer}|{context}")
    if _alpha_toxic(pair_stat) or _alpha_toxic(context_stat) or _alpha_toxic(wallet_stat):
        _copy_trade_stats["alpha_toxic"] = _copy_trade_stats.get("alpha_toxic", 0) + 1
        return None

    if _alpha_promoted(pair_stat):
        n, wr, avg_best, avg_exit = _alpha_stat_view(pair_stat)
        if avg_exit < COPY_FAST_ALPHA_MIN_AVG_EXIT_NET:
            return None
        core_ok = (
            n >= COPY_FAST_ALPHA_CORE_MIN_SAMPLES
            and wr >= COPY_FAST_ALPHA_CORE_MIN_WR
            and avg_exit >= COPY_FAST_ALPHA_CORE_MIN_AVG_EXIT_NET
        )
        _copy_trade_stats["alpha_promoted"] = _copy_trade_stats.get("alpha_promoted", 0) + 1
        return {
            "launchpad": "moonshot_ignition",
            "amount": COPY_FAST_ALPHA_CORE_AMOUNT_SOL if core_ok else COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL,
            "reason": (f"{'core' if core_ok else 'scout'} pair n={n} wr={wr:.0%} "
                       f"avg_best={avg_best:+.1%} avg_exit={avg_exit:+.1%}"),
        }
    if _alpha_promoted(wallet_stat, ALPHA_MIN_SAMPLES * 2) and _alpha_promoted(context_stat):
        wn, wwr, wavg, wexit = _alpha_stat_view(wallet_stat)
        cn, cwr, cavg, cexit = _alpha_stat_view(context_stat)
        if min(wexit, cexit) < COPY_FAST_ALPHA_MIN_AVG_EXIT_NET:
            return None
        _copy_trade_stats["alpha_promoted"] = _copy_trade_stats.get("alpha_promoted", 0) + 1
        return {
            "launchpad": "moonshot_ignition",
            "amount": COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL,
            "reason": (f"wallet_context scout w={wn}/{wwr:.0%}/{wavg:+.1%}/{wexit:+.1%} "
                       f"c={cn}/{cwr:.0%}/{cavg:+.1%}/{cexit:+.1%}"),
        }
    if _alpha_wallet_only_promoted(wallet_stat):
        n, wr, avg_best, avg_exit = _alpha_stat_view(wallet_stat)
        _copy_trade_stats["alpha_promoted"] = _copy_trade_stats.get("alpha_promoted", 0) + 1
        return {
            "launchpad": "moonshot_ignition",
            "amount": COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL,
            "reason": f"wallet_scout n={n} wr={wr:.0%} avg_best={avg_best:+.1%} avg_exit={avg_exit:+.1%}",
        }

    if (ALPHA_EXPLORATION_ENABLED
            and off_peak_ok
            and swarm_count >= 2
            and COPY_FAST_ALPHA_EXPLORATION_MIN_MULT <= last_mult <= COPY_FAST_ALPHA_EXPLORATION_MAX_MULT):
        _copy_trade_stats["alpha_scouts"] = _copy_trade_stats.get("alpha_scouts", 0) + 1
        return {
            "launchpad": "moonshot_ignition",
            "amount": COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL,
            "reason": f"explore sw={swarm_count} mult={last_mult:.3f}x ctx={context}",
        }
    return None


def _alpha_market_tape_entry_plan(mint: str, signer: str,
                                  trader_price: float, trigger_price: float,
                                  unique_count: int, tracked_count: int,
                                  buy_sol: float, sell_sol: float,
                                  observed_age_ms: int) -> Optional[dict]:
    if not (ALPHA_LEARNER_ENABLED and ALPHA_ADAPTIVE_ENTRY_ENABLED and MARKET_TAPE_ALPHA_ENABLED):
        return None
    if observed_age_ms > MARKET_TAPE_ALPHA_MAX_AGE_SEC * 1000:
        return None
    if sell_sol > MARKET_TAPE_ALPHA_MAX_SELL_SOL:
        return None
    if tracked_count < MARKET_TAPE_ALPHA_MIN_TRACKED or unique_count < 3 or buy_sol < 0.50:
        return None
    move_stats = _bc_cache_window_stats_for_mint(
        mint,
        max(MARKET_TAPE_WINDOW_MS + 800, 1500),
        MARKET_TAPE_BC_CACHE_MAX_AGE_MS,
    )
    if not move_stats or move_stats["complete"]:
        return None
    move_mult = float(move_stats["move"])
    context = _alpha_context_key("market_tape", mint, signer, trader_price, trigger_price)
    wallet_stat = _alpha_stats.get("wallets", {}).get(signer)
    context_stat = _alpha_stats.get("contexts", {}).get(context)
    pair_stat = _alpha_stats.get("pairs", {}).get(f"{signer}|{context}")
    if _alpha_toxic(pair_stat) or _alpha_toxic(context_stat) or _alpha_toxic(wallet_stat):
        _copy_trade_stats["alpha_toxic"] = _copy_trade_stats.get("alpha_toxic", 0) + 1
        return None
    def _plan_from_stat(n: int, wr: float, avg_best: float, avg_exit: float,
                        reason: str) -> Optional[dict]:
        bypass_static = _alpha_bypass_static_guards(n, wr, avg_exit)
        if move_mult < MARKET_TAPE_ALPHA_MIN_MOVE_MULT and not bypass_static:
            return None
        if avg_exit < MARKET_TAPE_ALPHA_MIN_AVG_EXIT_NET:
            return None
        _copy_trade_stats["alpha_promoted"] = _copy_trade_stats.get("alpha_promoted", 0) + 1
        return {
            "amount": COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL,
            "quality": 6,
            "context": context,
            "ratio_bypass": bypass_static,
            "move_bypass": bypass_static and move_mult < MARKET_TAPE_ALPHA_MIN_MOVE_MULT,
            "min_confirm_mult": (
                MARKET_TAPE_ALPHA_RETAIN_CONFIRM_MULT
                if avg_exit >= MARKET_TAPE_ALPHA_STRONG_MIN_AVG_EXIT_NET
                else MARKET_TAPE_ALPHA_CONFIRM_MIN_MULT
            ),
            "reason": reason,
        }
    if _alpha_promoted(pair_stat):
        n, wr, avg_best, avg_exit = _alpha_stat_view(pair_stat)
        pair_plan = _plan_from_stat(
            n, wr, avg_best, avg_exit,
            f"alpha_pair_scout n={n} wr={wr:.0%} avg_best={avg_best:+.1%} "
            f"avg_exit={avg_exit:+.1%} ctx={context}",
        )
        if pair_plan:
            return pair_plan
    if _alpha_promoted(wallet_stat, ALPHA_MIN_SAMPLES * 2) and _alpha_promoted(context_stat):
        wn, wwr, wavg, wexit = _alpha_stat_view(wallet_stat)
        cn, cwr, cavg, cexit = _alpha_stat_view(context_stat)
        if min(wexit, cexit) < MARKET_TAPE_ALPHA_MIN_AVG_EXIT_NET:
            return None
        bypass_static = _alpha_bypass_static_guards(cn, cwr, cexit)
        if move_mult < MARKET_TAPE_ALPHA_MIN_MOVE_MULT and not bypass_static:
            return None
        _copy_trade_stats["alpha_promoted"] = _copy_trade_stats.get("alpha_promoted", 0) + 1
        return {
            "amount": COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL,
            "quality": 6,
            "context": context,
            "ratio_bypass": bypass_static,
            "move_bypass": bypass_static and move_mult < MARKET_TAPE_ALPHA_MIN_MOVE_MULT,
            "min_confirm_mult": (
                MARKET_TAPE_ALPHA_RETAIN_CONFIRM_MULT
                if cexit >= MARKET_TAPE_ALPHA_STRONG_MIN_AVG_EXIT_NET
                else MARKET_TAPE_ALPHA_CONFIRM_MIN_MULT
            ),
            "reason": f"alpha_wallet_context w={wn}/{wwr:.0%}/{wavg:+.1%}/{wexit:+.1%} "
                      f"c={cn}/{cwr:.0%}/{cavg:+.1%}/{cexit:+.1%} ctx={context}",
        }
    if _alpha_context_only_promoted(context_stat):
        n, wr, avg_best, avg_exit = _alpha_stat_view(context_stat)
        context_plan = _plan_from_stat(
            n, wr, avg_best, avg_exit,
            f"alpha_context n={n} wr={wr:.0%} avg_best={avg_best:+.1%} "
            f"avg_exit={avg_exit:+.1%} ctx={context}",
        )
        if context_plan:
            return context_plan
    if _alpha_wallet_only_promoted(wallet_stat):
        n, wr, avg_best, avg_exit = _alpha_stat_view(wallet_stat)
        return _plan_from_stat(
            n, wr, avg_best, avg_exit,
            f"alpha_wallet_scout n={n} wr={wr:.0%} avg_best={avg_best:+.1%} "
            f"avg_exit={avg_exit:+.1%} ctx={context}",
        )
    return None


def _moonshot_context_key(observed_age_ms: int, tracked_count: int,
                          move_mult: float, buy_sol: float) -> str:
    age_s = observed_age_ms / 1000.0
    age_b = _alpha_bucket(age_s, (1.5, 3.0, 5.0, 7.0), ("a0_15", "a15_3", "a3_5", "a5_7", "a7p"))
    tr_b = "tr0" if tracked_count <= 0 else ("tr1" if tracked_count == 1 else ("tr2" if tracked_count == 2 else "tr3p"))
    move_b = _alpha_bucket(move_mult, (1.12, 1.25, 1.50, 1.90), ("m_lt12", "m12_25", "m25_50", "m50_90", "m90p"))
    buy_b = _alpha_bucket(buy_sol, (2.0, 4.0, 7.0, 12.0), ("b_lt2", "b2_4", "b4_7", "b7_12", "b12p"))
    return f"moonshot|{age_b}|{tr_b}|{move_b}|{buy_b}"


def _velocity_ignition_plan(mint: str, unique_count: int, tracked_count: int,
                            buy_sol: float, sell_sol: float) -> Optional[dict]:
    if not VELOCITY_IGNITION_ENABLED:
        return None
    if buy_sol < VELOCITY_MIN_BUY_SOL or unique_count < VELOCITY_MIN_UNIQUE:
        return None
    sell_ratio = sell_sol / buy_sol if buy_sol > 0 else 1.0
    if sell_sol > VELOCITY_MAX_SELL_SOL or sell_ratio > VELOCITY_MAX_SELL_BUY_RATIO:
        _mt_gate("vel_sell")
        return None
    tracked_flow = tracked_count >= VELOCITY_MIN_TRACKED
    operator_flow = buy_sol >= VELOCITY_STRONG_BUY_SOL and unique_count >= VELOCITY_STRONG_UNIQUE
    if not (tracked_flow or operator_flow):
        _mt_gate("vel_flow")
        return None
    stats = _bc_cache_window_stats_for_mint(
        mint,
        VELOCITY_WINDOW_MS,
        VELOCITY_MAX_CACHE_AGE_MS,
    )
    if not stats:
        _mt_gate("vel_no_bc")
        return None
    if stats["complete"]:
        _mt_gate("vel_complete")
        return None
    move_mult = float(stats["move"])
    if move_mult < VELOCITY_MIN_MOVE_MULT:
        _mt_gate("vel_move_low")
        return None
    if move_mult > VELOCITY_MAX_CHASE_MULT:
        _mt_gate("vel_chase")
        return None
    off_peak = float(stats["off_peak"])
    if off_peak > VELOCITY_MAX_OFF_PEAK:
        _mt_gate("vel_off_peak")
        return None
    if int(stats.get("down_ticks") or 0) > int(stats.get("up_ticks") or 0) + 1:
        _mt_gate("vel_down_ticks")
        return None

    strong = (
        move_mult >= VELOCITY_STRONG_MOVE_MULT
        and (unique_count >= VELOCITY_STRONG_UNIQUE
             or tracked_count >= max(2, VELOCITY_MIN_TRACKED + 1)
             or buy_sol >= VELOCITY_STRONG_BUY_SOL)
    )
    amount = VELOCITY_STRONG_AMOUNT_SOL if strong else VELOCITY_AMOUNT_SOL
    quality = 9 if strong else 7
    return {
        "amount": amount,
        "quality": quality,
        "trigger_price": float(stats["last"]),
        "reason": (
            f"velocity unique={unique_count} tracked={tracked_count} "
            f"buy={buy_sol:.3f} sell={sell_sol:.3f}/{sell_ratio:.1%} "
            f"move={move_mult:.3f}x off_peak={off_peak:.1%} "
            f"up/down={stats['up_ticks']}/{stats['down_ticks']} "
            f"cache={stats['age_ms']}ms amount={amount:.4f} SOL"
        ),
    }


def _moonshot_ignition_plan(mint: str, signer: str, trader_price: float,
                            unique_count: int, tracked_count: int,
                            buy_sol: float, sell_sol: float,
                            observed_age_ms: int) -> Optional[dict]:
    if not MOONSHOT_IGNITION_ENABLED:
        return None
    if observed_age_ms > MOONSHOT_MAX_AGE_SEC * 1000:
        _mt_gate("moon_age")
        return None
    if buy_sol <= 0:
        return None
    sell_ratio = sell_sol / buy_sol if buy_sol > 0 else 1.0
    if sell_sol > MOONSHOT_MAX_SELL_SOL or sell_ratio > MOONSHOT_MAX_SELL_BUY_RATIO:
        _mt_gate("moon_sell")
        return None
    stats = _bc_cache_window_stats_for_mint(
        mint,
        MOONSHOT_WINDOW_MS,
        max(MOONSHOT_MAX_CACHE_AGE_MS, MARKET_TAPE_BC_CACHE_MAX_AGE_MS),
    )
    if not stats:
        _mt_gate("moon_no_bc")
        return None
    if stats["complete"]:
        _mt_gate("moon_complete")
        return None
    move_mult = float(stats["move"])
    if move_mult < MOONSHOT_MIN_MOVE_MULT:
        _mt_gate("moon_move_low")
        return None
    if move_mult > MOONSHOT_MAX_CHASE_MULT:
        _mt_gate("moon_chase")
        return None
    off_peak = float(stats["off_peak"])
    if off_peak > MOONSHOT_MAX_OFF_PEAK:
        _mt_gate("moon_off_peak")
        return None

    tracked_flow_ok = (
        unique_count >= MOONSHOT_MIN_UNIQUE
        and tracked_count >= MOONSHOT_MIN_TRACKED
        and buy_sol >= MOONSHOT_MIN_BUY_SOL
    )
    untracked_operator_ok = (
        unique_count >= MOONSHOT_UNTRACKED_MIN_UNIQUE
        and buy_sol >= MOONSHOT_UNTRACKED_MIN_BUY_SOL
        and move_mult >= MOONSHOT_STRONG_MOVE_MULT
    )
    if not (tracked_flow_ok or untracked_operator_ok):
        _mt_gate("moon_flow")
        return None

    price_ratio = None
    if trader_price > 0:
        price_ratio = float(stats["last"]) / trader_price
        if price_ratio < MOONSHOT_MIN_PRICE_RATIO or price_ratio > MOONSHOT_MAX_PRICE_RATIO:
            _mt_gate("moon_ratio")
            return None

    score = 0
    if observed_age_ms <= MOONSHOT_MAX_AGE_SEC * 1000:
        score += 1
    if move_mult >= MOONSHOT_MIN_MOVE_MULT:
        score += 2
    if move_mult >= MOONSHOT_STRONG_MOVE_MULT:
        score += 2
    if unique_count >= MOONSHOT_MIN_UNIQUE:
        score += 1
    if unique_count >= MOONSHOT_MIN_UNIQUE + 3:
        score += 1
    if tracked_count >= MOONSHOT_MIN_TRACKED:
        score += 2
    if buy_sol >= MOONSHOT_MIN_BUY_SOL:
        score += 1
    if buy_sol >= max(MOONSHOT_UNTRACKED_MIN_BUY_SOL, MOONSHOT_MIN_BUY_SOL * 2):
        score += 1
    if sell_sol <= 0.001:
        score += 1
    elif sell_ratio <= MOONSHOT_MAX_SELL_BUY_RATIO * 0.5:
        score += 1
    if stats["up_ticks"] > stats["down_ticks"]:
        score += 1
    if off_peak <= MOONSHOT_MAX_OFF_PEAK * 0.5:
        score += 1
    if untracked_operator_ok:
        score += 1
    if score < MOONSHOT_MIN_SCORE:
        _mt_gate("moon_score")
        return None

    context = _moonshot_context_key(observed_age_ms, tracked_count, move_mult, buy_sol)
    now_ms = int(time.time() * 1000)
    cooldown_ms = int(MOONSHOT_CONTEXT_COOLDOWN_SEC * 1000)
    if cooldown_ms > 0 and now_ms - _moonshot_context_recent_ms.get(context, 0) < cooldown_ms:
        _mt_gate("moon_context_cd")
        return None

    amount = MOONSHOT_IGNITION_AMOUNT_SOL
    if score >= MOONSHOT_STRONG_SCORE or (move_mult >= MOONSHOT_STRONG_MOVE_MULT and buy_sol >= MOONSHOT_UNTRACKED_MIN_BUY_SOL):
        amount = MOONSHOT_IGNITION_STRONG_AMOUNT_SOL
    amount = min(amount, MOONSHOT_IGNITION_MAX_AMOUNT_SOL)
    return {
        "amount": amount,
        "quality": min(10, score),
        "context": context,
        "trigger_price": float(stats["last"]),
        "score": score,
        "reason": (
            f"score={score} ctx={context} unique={unique_count} tracked={tracked_count} "
            f"buy={buy_sol:.3f} sell={sell_sol:.3f}/{sell_ratio:.1%} "
            f"move={move_mult:.3f}x off_peak={off_peak:.1%} "
            f"up/down={stats['up_ticks']}/{stats['down_ticks']} "
            f"age={observed_age_ms/1000:.1f}s cache={stats['age_ms']}ms"
            + (f" ratio={price_ratio:.3f}x" if price_ratio is not None else "")
        ),
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
    if _st_data_api_blocked(f"/tokens/{mint[:8]}"):
        return True, "rug-check Data API backoff - proceeding", None
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/tokens/{mint}",
            headers=_st_auth_headers(),
            timeout=1.5,
        )
        if r.status_code != 200:
            _st_note_data_api_status(f"/tokens/{mint[:8]}", r.status_code, r.text or "")
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
    if _st_data_api_blocked(f"/first-buyers/{mint[:8]}"):
        return None
    try:
        r = requests.get(
            f"{SOLANATRACKER_BASE}/first-buyers/{mint}",
            headers=_st_auth_headers(),
            timeout=2.0,
        )
        if r.status_code != 200:
            _st_note_data_api_status(f"/first-buyers/{mint[:8]}", r.status_code, r.text or "")
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
    sol_lamports = int(WARM_SWAP_AMOUNT_SOL * 10**WSOL_DECIMALS)

    while True:
        try:
            async with websockets.connect(raptor_ws_url, ping_interval=25, ping_timeout=20) as ws:
                # Refresh subscription set frequently for hot priority mints. Data API
                # trending still refreshes every 30s to avoid 429s.
                async def refresher():
                    trending_targets: set[str] = set()
                    next_trending_fetch = 0.0
                    while True:
                        try:
                            now_ts = time.time()
                            if now_ts >= next_trending_fetch:
                                data = await asyncio.to_thread(_st_fetch, "/tokens/trending/5m")
                                trending_targets = set()
                                if isinstance(data, list):
                                    for t in data[:WARM_POOL_SIZE]:
                                        mint = (t.get("token") or {}).get("mint", "")
                                        mint_lc = mint.lower()
                                        if mint and (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
                                            trending_targets.add(mint)
                                next_trending_fetch = now_ts + 30
                            for mint, ts in list(_warm_priority_mints.items()):
                                if now_ts - ts > WARM_PRIORITY_TTL_SEC:
                                    _warm_priority_mints.pop(mint, None)
                            priority = set(_warm_priority_mints.keys())
                            target_mints = set(list(priority)[:WARM_POOL_SIZE])
                            room_left = max(0, WARM_POOL_SIZE - len(target_mints))
                            if room_left:
                                target_mints |= set(list(trending_targets)[:room_left])
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
                        await asyncio.sleep(WARM_POOL_REFRESH_SEC)

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


# V41.17i: pump.fun program ID + buy ix discriminator for direct shred parsing.
# Buy ix args (Borsh): u64 amount (tokens to receive), u64 max_sol_cost (slippage cap).
# Per IDL, mint is at instruction.accounts[2].
_PUMP_PROGRAM_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_DISC_BUY_BYTES = bytes([102, 6, 61, 18, 1, 218, 235, 234])
_DISC_SELL_BYTES = bytes([51, 230, 133, 164, 1, 127, 131, 173])
_BONK_PROGRAM_STR = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"

# V41.17z: BondingCurve account discriminator (per pump.fun IDL).
# Used for programSubscribe memcmp filter to build a hot trend cache.
_BC_DISC_BYTES = bytes([0x17, 0xb7, 0xf8, 0x37, 0x60, 0xd8, 0xac, 0x60])
_BC_DISC_B58 = base58.b58encode(_BC_DISC_BYTES).decode()

# Hot cache: bc_pda (str) -> deque of (ts_ms, vSol, vTokens). Populated by
# pump_program_bc_listener via programSubscribe. Read by _compute_trend_5s.
_bc_state_cache: dict = defaultdict(lambda: deque(maxlen=20))

# V41.17z trend gate config (env-tunable). Skip copy_fast entries when the
# bonding curve was strongly dumping in the 5 seconds before the trader bought
# — that pattern produced our worst losses (e.g., 8xV18bZ3 -10.5%).
TREND_GATE_ENABLED = os.environ.get("TREND_GATE_ENABLED", "1") == "1"
TREND_GATE_5S_MIN = float(os.environ.get("TREND_GATE_5S_MIN", "-0.02"))


def _compute_trend_5s_for_mint(mint: str) -> Optional[float]:
    """Compute 5-second vSol trend on a bonding curve from the hot cache.
    Returns fractional change (e.g. +0.05 = +5%) or None if insufficient data.
    Uses fail-OPEN semantics in the gate — if no cache, no skip."""
    try:
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(_PUMP_PROGRAM_STR),
        )
        items = list(_bc_state_cache.get(str(bc_pda), ()))
    except Exception:
        return None
    if len(items) < 2:
        return None
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - 5_000
    recent = [it for it in items if it[0] >= cutoff]
    if len(recent) < 2:
        return None
    first = recent[0][1]; last = recent[-1][1]
    if first == 0:
        return None
    return (last - first) / first


def _bc_cache_price_for_mint(mint: str, max_age_ms: int) -> Optional[tuple[float, bool, int]]:
    """Return (curve_price, complete, age_ms) from the multiplexed BondingCurve cache."""
    try:
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(_PUMP_PROGRAM_STR),
        )
        items = list(_bc_state_cache.get(str(bc_pda), ()))
        if not items:
            return None
        latest = items[-1]
        ts_ms, vsol, vtoken = latest[0], latest[1], latest[2]
        complete = bool(latest[3]) if len(latest) > 3 else False
        age_ms = int(time.time() * 1000) - int(ts_ms)
        if age_ms > max_age_ms or not vtoken:
            return None
        return (float(vsol) / float(vtoken), complete, age_ms)
    except Exception:
        return None


def _bc_cache_move_for_mint(mint: str, window_ms: int,
                            max_age_ms: int) -> Optional[tuple[float, int, bool]]:
    """Return (last/first price multiplier, latest age_ms, complete) for a recent window."""
    try:
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(_PUMP_PROGRAM_STR),
        )
        items = list(_bc_state_cache.get(str(bc_pda), ()))
        if len(items) < 2:
            return None
        now_ms = int(time.time() * 1000)
        latest = items[-1]
        latest_age = now_ms - int(latest[0])
        if latest_age > max_age_ms:
            return None
        cutoff = now_ms - window_ms
        recent = [it for it in items if it[0] >= cutoff]
        if len(recent) < 2:
            return None
        first = recent[0]
        last = recent[-1]
        if not first[2] or not last[2]:
            return None
        first_px = float(first[1]) / float(first[2])
        last_px = float(last[1]) / float(last[2])
        if first_px <= 0:
            return None
        complete = bool(last[3]) if len(last) > 3 else False
        return last_px / first_px, latest_age, complete
    except Exception:
        return None


def _bc_cache_window_stats_for_mint(mint: str, window_ms: int,
                                    max_age_ms: int) -> Optional[dict]:
    """Recent curve-price shape for ignition logic.

    Returns local move, peak distance and tick direction using only the hot
    programSubscribe cache. No HTTP on the speed path.
    """
    try:
        bc_pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(_PUMP_PROGRAM_STR),
        )
        items = list(_bc_state_cache.get(str(bc_pda), ()))
        if len(items) < 2:
            return None
        now_ms = int(time.time() * 1000)
        latest = items[-1]
        latest_age = now_ms - int(latest[0])
        if latest_age > max_age_ms:
            return None
        cutoff = now_ms - window_ms
        recent = [it for it in items if it[0] >= cutoff and it[2]]
        if len(recent) < 2:
            return None
        prices = [float(it[1]) / float(it[2]) for it in recent if float(it[2]) > 0]
        if len(prices) < 2 or prices[0] <= 0:
            return None
        first_px = prices[0]
        last_px = prices[-1]
        peak_px = max(prices)
        trough_px = min(prices)
        up_ticks = sum(1 for a, b in zip(prices, prices[1:]) if b >= a * 1.002)
        down_ticks = sum(1 for a, b in zip(prices, prices[1:]) if b <= a * 0.998)
        complete = bool(recent[-1][3]) if len(recent[-1]) > 3 else False
        return {
            "first": first_px,
            "last": last_px,
            "peak": peak_px,
            "trough": trough_px,
            "move": last_px / first_px,
            "off_peak": 0.0 if peak_px <= 0 else max(0.0, 1.0 - (last_px / peak_px)),
            "drawup": 0.0 if trough_px <= 0 else last_px / trough_px,
            "age_ms": latest_age,
            "complete": complete,
            "samples": len(prices),
            "up_ticks": up_ticks,
            "down_ticks": down_ticks,
        }
    except Exception:
        return None


def _bc_cache_price_for_pos(pos: Position) -> Optional[tuple[float, bool, int]]:
    """Return (price, complete, age_ms) from the multiplexed ST BondingCurve cache."""
    try:
        if not pos.bc_pda:
            return None
        items = list(_bc_state_cache.get(str(pos.bc_pda), ()))
        if not items:
            return None
        latest = items[-1]
        ts_ms, vsol, vtoken = latest[0], latest[1], latest[2]
        complete = bool(latest[3]) if len(latest) > 3 else False
        age_ms = int(time.time() * 1000) - int(ts_ms)
        if age_ms > GRAD_BC_CACHE_MAX_AGE_MS or not vtoken:
            return None
        return (float(vsol) / float(vtoken), complete, age_ms)
    except Exception:
        return None


def _recent_swarm_events(mint: str, window_sec: float = 10.0,
                         now_ms: Optional[int] = None) -> list[tuple[str, int]]:
    now_ms = now_ms or int(time.time() * 1000)
    cutoff = now_ms - int(window_sec * 1000)
    history = _signer_history_per_mint.get(mint, [])
    return [(s, t) for s, t in history if t >= cutoff]


def _recent_swarm_signers(mint: str, window_sec: float = 10.0,
                          now_ms: Optional[int] = None) -> set[str]:
    return {s for s, _t in _recent_swarm_events(mint, window_sec, now_ms)}


async def _confirm_copy_fast_entry(mint: str, launchpad: str, signal_time_ms: int,
                                   signer: str = "", trader_price: float = 0.0) -> bool:
    """Turn raw copy signals into confirmed momentum entries.

    The last paper run showed raw `copy_fast` was the loss engine: it bought
    before the copied buy proved follow-through. This gate requires a fresh
    BondingCurve cache baseline, a +8% move, no >4% dump during the observation,
    and either SWARM-4 or SWARM-3 that keeps attracting buyers.
    """
    if not COPY_FAST_CONFIRM_ENABLED:
        return True

    start = time.time()
    initial_events = _recent_swarm_events(mint, COPY_FAST_CONFIRM_SWARM_WINDOW_SEC)
    initial_signers = {s for s, _t in initial_events}
    initial_event_count = len(initial_events)
    initial_swarm = len(initial_signers)
    baseline = _bc_cache_price_for_mint(mint, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS)
    if not baseline:
        _copy_trade_stats["confirm_blocked"] = _copy_trade_stats.get("confirm_blocked", 0) + 1
        log(f"  GRAD CONFIRM-SKIP {mint[:8]} ({launchpad}): no fresh bc-cache baseline "
            f"(<={COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS}ms required)")
        return False
    baseline_price, complete, age_ms = baseline
    if complete or baseline_price <= 0:
        _copy_trade_stats["confirm_blocked"] = _copy_trade_stats.get("confirm_blocked", 0) + 1
        log(f"  GRAD CONFIRM-SKIP {mint[:8]} ({launchpad}): curve complete/stale before entry")
        return False

    peak_price = baseline_price
    last_mult = 1.0
    last_age_ms = age_ms
    swarm_ok = initial_swarm >= COPY_FAST_CONFIRM_MIN_SWARM
    reason = "no confirming tick"
    deadline = start + COPY_FAST_CONFIRM_WINDOW_SEC

    log(f"  GRAD CONFIRM-WAIT {mint[:8]} ({launchpad}): baseline={baseline_price:.4e} "
        f"age={age_ms}ms swarm={initial_swarm}, need +{(COPY_FAST_CONFIRM_MIN_MULT-1)*100:.1f}% "
        f"and SWARM-{COPY_FAST_CONFIRM_MIN_SWARM}")

    while time.time() < deadline:
        price_info = _bc_cache_price_for_mint(mint, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS)
        if price_info:
            price, complete, last_age_ms = price_info
            if complete:
                reason = "curve completed during confirm"
                break
            peak_price = max(peak_price, price)
            last_mult = price / baseline_price if baseline_price else 1.0
            if last_mult > COPY_FAST_CONFIRM_MAX_MULT:
                _copy_trade_stats["confirm_blocked"] = _copy_trade_stats.get("confirm_blocked", 0) + 1
                log(f"  GRAD CONFIRM-CHASE-BLOCK {mint[:8]} ({launchpad}): "
                    f"mult={last_mult:.3f}x > {COPY_FAST_CONFIRM_MAX_MULT:.3f}x")
                return False
            if last_mult <= 1.0 + COPY_FAST_CONFIRM_MAX_DUMP:
                _copy_trade_stats["confirm_dump_blocked"] = _copy_trade_stats.get("confirm_dump_blocked", 0) + 1
                log(f"  GRAD CONFIRM-DUMP {mint[:8]} ({launchpad}): mult={last_mult:.3f}x <= "
                    f"{1.0 + COPY_FAST_CONFIRM_MAX_DUMP:.3f}x during confirm")
                return False

            recent_events = _recent_swarm_events(mint, COPY_FAST_CONFIRM_SWARM_WINDOW_SEC)
            recent = {s for s, _t in recent_events}
            if len(recent) >= COPY_FAST_CONFIRM_MIN_SWARM:
                swarm_ok = True
            elif initial_swarm >= 3 and time.time() - start <= COPY_FAST_CONFIRM_SWARM3_CONTINUE_SEC:
                swarm_ok = len(recent_events) > initial_event_count

            off_peak_ok = price >= peak_price * (1.0 - COPY_FAST_CONFIRM_MAX_OFF_PEAK)
            if last_mult >= COPY_FAST_CONFIRM_MIN_MULT and off_peak_ok and swarm_ok:
                ignition_swarm_ok = (
                    len(recent) >= COPY_FAST_IGNITION_MIN_SWARM
                    and last_mult >= COPY_FAST_IGNITION_MIN_MULT
                )
                ignition_fast_ok = (
                    len(recent) >= COPY_FAST_IGNITION_FAST_SWARM
                    and last_mult >= COPY_FAST_IGNITION_FAST_MULT
                )
                if (COPY_FAST_IGNITION_ENABLED
                        and launchpad == "copy_fast"
                        and (ignition_swarm_ok or ignition_fast_ok)
                        and last_age_ms <= COPY_FAST_IGNITION_MAX_CACHE_AGE_MS):
                    if COPY_FAST_IGNITION_CONFIRM_DELAY_SEC > 0:
                        await asyncio.sleep(COPY_FAST_IGNITION_CONFIRM_DELAY_SEC)
                        confirm_price_info = _bc_cache_price_for_mint(
                            mint,
                            COPY_FAST_IGNITION_MAX_CACHE_AGE_MS,
                        )
                        if not confirm_price_info or confirm_price_info[1] or confirm_price_info[0] <= 0:
                            reason = (f"copy_fast ignition no confirm price after "
                                      f"{COPY_FAST_IGNITION_CONFIRM_DELAY_SEC:.2f}s")
                            await asyncio.sleep(COPY_FAST_CONFIRM_POLL_SEC)
                            continue
                        confirm_price, _complete, last_age_ms = confirm_price_info
                        retain_mult = confirm_price / price if price > 0 else 0.0
                        if retain_mult < COPY_FAST_IGNITION_CONFIRM_MIN_MULT:
                            reason = (f"copy_fast ignition fade retain={retain_mult:.3f}x "
                                      f"need>={COPY_FAST_IGNITION_CONFIRM_MIN_MULT:.3f}x "
                                      f"after {COPY_FAST_IGNITION_CONFIRM_DELAY_SEC:.2f}s")
                            await asyncio.sleep(COPY_FAST_CONFIRM_POLL_SEC)
                            continue
                        price = confirm_price
                        peak_price = max(peak_price, price)
                        last_mult = price / baseline_price if baseline_price else last_mult
                        off_peak_ok = price >= peak_price * (1.0 - COPY_FAST_CONFIRM_MAX_OFF_PEAK)
                        if not off_peak_ok:
                            reason = "copy_fast ignition confirm fell off peak"
                            await asyncio.sleep(COPY_FAST_CONFIRM_POLL_SEC)
                            continue
                    strong = (
                        last_mult >= COPY_FAST_IGNITION_STRONG_MULT
                        and len(recent) >= COPY_FAST_IGNITION_STRONG_SWARM
                    )
                    amount = (
                        COPY_FAST_IGNITION_STRONG_AMOUNT_SOL
                        if strong else COPY_FAST_IGNITION_AMOUNT_SOL
                    )
                    _copy_fast_entry_overrides[mint] = {
                        "launchpad": "moonshot_ignition",
                        "amount": amount,
                        "quality": 10 if strong else 9,
                        "reason": (
                            f"copy_fast_ignition mult={last_mult:.3f}x "
                            f"swarm={len(recent)} age={last_age_ms}ms "
                            f"amount={amount:.4f} SOL"
                        ),
                    }
                    _copy_trade_stats["copy_fast_ignition"] = _copy_trade_stats.get("copy_fast_ignition", 0) + 1
                    _copy_trade_stats["confirm_ok"] = _copy_trade_stats.get("confirm_ok", 0) + 1
                    log(f"  GRAD CONFIRM-OK {mint[:8]} (copy_fast_ignition): "
                        f"mult={last_mult:.3f}x peak={peak_price / baseline_price:.3f}x "
                        f"swarm={len(recent)} age={last_age_ms}ms amount={amount:.4f} SOL")
                    return True
                _copy_trade_stats["confirm_ok"] = _copy_trade_stats.get("confirm_ok", 0) + 1
                log(f"  GRAD CONFIRM-OK {mint[:8]} ({launchpad}): mult={last_mult:.3f}x "
                    f"peak={peak_price / baseline_price:.3f}x swarm={len(recent)} age={last_age_ms}ms")
                return True
            single_rocket_ok = (
                COPY_FAST_SOLO_ROCKET_ALLOW_SINGLE
                and last_mult >= COPY_FAST_SOLO_ROCKET_MIN_MULT
            )
            swarm2_rocket_ok = (
                len(recent) >= 2
                and last_mult >= COPY_FAST_SOLO_ROCKET_SWARM2_MIN_MULT
            )
            solo_rocket_ok = single_rocket_ok or swarm2_rocket_ok
            alpha_plan = _alpha_entry_plan(
                mint, signer, "copy_fast", trader_price, price,
                last_mult, len(recent), off_peak_ok,
            )
            if (alpha_plan
                    and launchpad == "copy_fast"
                    and (float(alpha_plan.get("amount", 0.0)) >= COPY_FAST_SOLO_ROCKET_AMOUNT_SOL
                         or not (COPY_FAST_SOLO_ROCKET_ENABLED and solo_rocket_ok))):
                _copy_fast_entry_overrides[mint] = alpha_plan
                _copy_trade_stats["confirm_ok"] = _copy_trade_stats.get("confirm_ok", 0) + 1
                log(f"  GRAD CONFIRM-OK {mint[:8]} (copy_fast_alpha): "
                    f"{alpha_plan.get('reason', 'alpha')} mult={last_mult:.3f}x "
                    f"peak={peak_price / baseline_price:.3f}x swarm={len(recent)} age={last_age_ms}ms")
                return True
            if (COPY_FAST_SOLO_ROCKET_ENABLED
                    and launchpad == "copy_fast"
                    and solo_rocket_ok
                    and off_peak_ok):
                if COPY_FAST_SOLO_ROCKET_CONFIRM_DELAY_SEC > 0:
                    required_mult = (
                        COPY_FAST_SOLO_ROCKET_MIN_MULT if single_rocket_ok
                        else COPY_FAST_SOLO_ROCKET_SWARM2_MIN_MULT
                    ) * COPY_FAST_SOLO_ROCKET_CONFIRM_RETAIN
                    await asyncio.sleep(COPY_FAST_SOLO_ROCKET_CONFIRM_DELAY_SEC)
                    confirm_price_info = _bc_cache_price_for_mint(mint, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS)
                    if not confirm_price_info or confirm_price_info[1]:
                        reason = f"solo rocket no confirm price after {COPY_FAST_SOLO_ROCKET_CONFIRM_DELAY_SEC:.2f}s"
                        await asyncio.sleep(COPY_FAST_CONFIRM_POLL_SEC)
                        continue
                    confirm_price, _complete, last_age_ms = confirm_price_info
                    confirm_mult = confirm_price / baseline_price if baseline_price else 1.0
                    peak_price = max(peak_price, confirm_price)
                    confirm_off_peak = confirm_price >= peak_price * (1.0 - COPY_FAST_CONFIRM_MAX_OFF_PEAK)
                    if confirm_mult < required_mult or not confirm_off_peak:
                        reason = (f"solo rocket fade confirm={confirm_mult:.3f}x "
                                  f"need>={required_mult:.3f}x off_peak={confirm_off_peak} "
                                  f"age={last_age_ms}ms")
                        await asyncio.sleep(COPY_FAST_CONFIRM_POLL_SEC)
                        continue
                    last_mult = confirm_mult
                    off_peak_ok = confirm_off_peak
                _copy_fast_solo_rocket_mints.add(mint)
                _copy_trade_stats["confirm_ok"] = _copy_trade_stats.get("confirm_ok", 0) + 1
                log(f"  GRAD CONFIRM-OK {mint[:8]} (copy_fast_solo): "
                    f"solo rocket mult={last_mult:.3f}x peak={peak_price / baseline_price:.3f}x "
                    f"swarm={len(recent)} age={last_age_ms}ms")
                return True
            reason = (f"mult={last_mult:.3f}x peak={peak_price / baseline_price:.3f}x "
                      f"swarm={len(recent)} off_peak={off_peak_ok} age={last_age_ms}ms")
        await asyncio.sleep(COPY_FAST_CONFIRM_POLL_SEC)

    _copy_trade_stats["confirm_blocked"] = _copy_trade_stats.get("confirm_blocked", 0) + 1
    log(f"  GRAD CONFIRM-SKIP {mint[:8]} ({launchpad}): {reason}; "
        f"window={COPY_FAST_CONFIRM_WINDOW_SEC:.1f}s")
    return False


async def pump_program_bc_listener():
    """V41.17z: programSubscribe to pump.fun BondingCurve accounts (memcmp filter).
    Populates _bc_state_cache so copy_fast can read pre-entry trend in <1ms.

    Uses a SECOND WS connection (we have a 2-conn cap on RPC plan; copy_trader
    uses 1 conn for shredSubscribe, this uses the 2nd). If the conn is rejected
    (cap reached), we keep retrying on backoff — gate fails-OPEN meanwhile."""
    if not COPY_TRADE_ENABLED or not ST_RPC_ENABLED:
        return
    log("PUMP-BC-CACHE: starting programSubscribe for trend gate (V41.17z)")
    while True:
        try:
            sub_msg = {
                "jsonrpc": "2.0", "id": 9001,
                "method": "programSubscribe",
                "params": [
                    _PUMP_PROGRAM_STR,
                    {"encoding": "base64", "commitment": "processed",
                     "filters": [{"memcmp": {"offset": 0, "bytes": _BC_DISC_B58}}]},
                ],
            }
            async with websockets.connect(
                ST_RPC_WS,
                ping_interval=20, ping_timeout=60,
                max_queue=2048, max_size=8 * 1024 * 1024,
            ) as ws:
                await ws.send(json.dumps(sub_msg))
                ack_raw = await ws.recv()
                ack = json.loads(ack_raw)
                log(f"PUMP-BC-CACHE: subscribed (sub_id={ack.get('result')})")
                async for raw in ws:
                    d = json.loads(raw)
                    if "method" not in d:
                        continue
                    val = (d.get("params") or {}).get("result", {}).get("value", {})
                    pubkey = val.get("pubkey")
                    if not pubkey:
                        continue
                    acc = val.get("account") or {}
                    data = acc.get("data")
                    if isinstance(data, list):
                        data = data[0]
                    if not isinstance(data, str):
                        continue
                    try:
                        raw_bytes = base64.b64decode(data)
                        if len(raw_bytes) < 0x18 or raw_bytes[:8] != _BC_DISC_BYTES:
                            continue
                        vtoken = struct.unpack_from("<Q", raw_bytes, 0x08)[0]
                        vsol = struct.unpack_from("<Q", raw_bytes, 0x10)[0]
                        complete = raw_bytes[48] != 0 if len(raw_bytes) > 48 else False
                        _bc_state_cache[pubkey].append((int(time.time() * 1000), vsol, vtoken, complete))
                    except Exception:
                        continue
        except Exception as e:
            log(f"PUMP-BC-CACHE WS err, reconnect 5s: {type(e).__name__}: {e}")
            await asyncio.sleep(5)


def _parse_base64_shred_for_pump_buy(shred_result: dict, trader_set: set) -> Optional[dict]:
    """V41.17y: parse a BASE64-encoded shredSubscribe message locally with `solders`,
    extract the pump.fun (or bonk.fun) buy ix WITHOUT calling getTransaction.

    Test-confirmed: median 0.01ms, p99 0.05ms (vs getTransaction's 200-400ms).
    Yellowstone-equivalent for direct pump.fun buys; aggregator-routed buys
    (Jupiter/Raptor swap → pump.fun internal) drop to slow path.

    Returns one of:
      - dict {signer, mint, amount, max_sol_cost, trader_price, launchpad}: direct buy found
      - "WRONG_SIGNER": parsed OK but signer not in trader_set — slow path won't help
      - None: parse failed OR signer is ours but no direct buy ix (aggregator route possible)
    """
    try:
        tx_outer = (shred_result.get("transaction") or {}).get("transaction")
        # base64 shred shape: result.transaction.transaction == [b64_str, "base64"]
        if not (isinstance(tx_outer, list) and len(tx_outer) >= 1):
            return None
        b64_str = tx_outer[0]
        raw = base64.b64decode(b64_str)
        vt = VersionedTransaction.from_bytes(raw)
        msg = vt.message
        keys = list(msg.account_keys)
        if not keys:
            return None
        signer_str = str(keys[0])
        if signer_str not in trader_set:
            return "WRONG_SIGNER"  # short-circuit signal — skip slow path entirely
        for ix in msg.instructions:
            try:
                prog = str(keys[ix.program_id_index])
            except Exception:
                continue
            if prog not in (_PUMP_PROGRAM_STR, _BONK_PROGRAM_STR):
                continue
            data = bytes(ix.data)
            if len(data) < 24 or data[:8] != _DISC_BUY_BYTES:
                continue
            amount = int.from_bytes(data[8:16], "little")
            max_sol_cost = int.from_bytes(data[16:24], "little")
            if amount == 0 or max_sol_cost == 0:
                continue
            try:
                mint_idx = ix.accounts[2]
            except (IndexError, TypeError):
                continue
            if mint_idx >= len(keys):
                # mint may be in v0 address-table lookups — those need full tx
                # resolution that solders' VersionedTransaction doesn't auto-load.
                # Fall back to slow path.
                return None
            mint = str(keys[mint_idx])
            return {
                "signer": signer_str, "mint": mint,
                "amount": amount, "max_sol_cost": max_sol_cost,
                "trader_price": max_sol_cost / amount,
                "launchpad": "bonk" if prog == _BONK_PROGRAM_STR else "pump",
            }
        return None
    except Exception:
        return None


def _parse_base64_shred_for_pump_trades_any(shred_result: dict) -> list[dict]:
    """Parse direct pump.fun/bonk buy/sell instructions from a base64 shred.

    This is intentionally signer-agnostic. V41.19 uses it as a market-wide tape:
    watched wallets are a boost, but the trigger is clustered buy pressure on the
    mint itself.
    """
    trades: list[dict] = []
    try:
        tx_outer = (shred_result.get("transaction") or {}).get("transaction")
        if not (isinstance(tx_outer, list) and len(tx_outer) >= 1):
            return trades
        raw = base64.b64decode(tx_outer[0])
        vt = VersionedTransaction.from_bytes(raw)
        msg = vt.message
        keys = list(msg.account_keys)
        if not keys:
            return trades
        signer = str(keys[0])
        for ix in msg.instructions:
            try:
                prog = str(keys[ix.program_id_index])
            except Exception:
                continue
            if prog not in (_PUMP_PROGRAM_STR, _BONK_PROGRAM_STR):
                continue
            data = bytes(ix.data)
            if len(data) < 24:
                continue
            disc = data[:8]
            if disc == _DISC_BUY_BYTES:
                is_buy = True
            elif disc == _DISC_SELL_BYTES:
                is_buy = False
            else:
                continue
            amount = int.from_bytes(data[8:16], "little")
            sol_lamports = int.from_bytes(data[16:24], "little")
            if amount <= 0 or sol_lamports <= 0:
                continue
            try:
                mint_idx = ix.accounts[2]
            except (IndexError, TypeError):
                continue
            if mint_idx >= len(keys):
                continue
            mint = str(keys[mint_idx])
            trades.append({
                "signer": signer,
                "mint": mint,
                "is_buy": is_buy,
                "amount": amount,
                "sol_lamports": sol_lamports,
                "trader_price": sol_lamports / amount,
                "program": prog,
            })
    except Exception:
        return trades
    return trades


def _base64_shred_fee_payer(shred_result: dict) -> Optional[str]:
    """Return account[0] from a base64 shred, or None if it cannot be decoded."""
    try:
        tx_outer = (shred_result.get("transaction") or {}).get("transaction")
        if not (isinstance(tx_outer, list) and len(tx_outer) >= 1):
            return None
        raw = base64.b64decode(tx_outer[0])
        vt = VersionedTransaction.from_bytes(raw)
        keys = list(vt.message.account_keys)
        return str(keys[0]) if keys else None
    except Exception:
        return None


def _parse_shred_for_pump_buy(shred_result: dict, trader_set: set) -> Optional[dict]:
    """V41.17i: extract the pump.fun (or bonk.fun) buy ix directly from a jsonParsed
    shred. Returns dict with signer/mint/amount/max_sol_cost/trader_price — or None
    if no buy ix found (caller falls back to getTransaction).

    Saves the ~600-800ms getTransaction round-trip that was the dominant cost in
    the copy_fast hot path. Trade-off: trader_price is computed from max_sol_cost
    (the slippage limit they set), which is a slight overestimate of their actual
    fill price — conservative for the Fix #11 gate (errs toward letting entries
    through on the high side, blocking on the low side).

    Aggregator-routed buys (Jupiter/Raptor) have no direct pump.fun ix in the tx;
    those drop to the slow path."""
    try:
        tx_obj = (shred_result.get("transaction") or {}).get("transaction") or {}
        message = tx_obj.get("message") or {}
        account_keys = message.get("accountKeys") or []
        instructions = message.get("instructions") or []
        if not account_keys or not instructions:
            return None
        # First entry in accountKeys is the fee-payer/signer. jsonParsed gives objects
        # like {"pubkey": "...", "signer": true, "writable": true, ...}.
        first = account_keys[0]
        signer = first.get("pubkey", "") if isinstance(first, dict) else str(first)
        if signer not in trader_set:
            return None
        for ix in instructions:
            pid = ix.get("programId", "")
            if pid != _PUMP_PROGRAM_STR and pid != _BONK_PROGRAM_STR:
                continue
            data_b58 = ix.get("data", "")
            if not data_b58:
                continue
            try:
                data_bytes = base58.b58decode(data_b58)
            except Exception:
                continue
            if len(data_bytes) < 24:
                continue
            # Pump.fun and bonk.fun share the same buy discriminator
            if data_bytes[:8] != _DISC_BUY_BYTES:
                continue
            amount = int.from_bytes(data_bytes[8:16], "little")
            max_sol_cost = int.from_bytes(data_bytes[16:24], "little")
            if amount == 0 or max_sol_cost == 0:
                continue
            ix_accounts = ix.get("accounts", [])
            if len(ix_accounts) < 3:
                continue
            # accounts[] in jsonParsed shred MAY be either indices (legacy) OR pubkey
            # strings (newer servers). Handle both.
            mint_ref = ix_accounts[2]
            if isinstance(mint_ref, int):
                if mint_ref >= len(account_keys):
                    continue
                ak = account_keys[mint_ref]
                mint = ak.get("pubkey", "") if isinstance(ak, dict) else str(ak)
            else:
                mint = str(mint_ref)
            if not mint:
                continue
            trader_price = max_sol_cost / amount
            return {
                "signer": signer,
                "mint": mint,
                "amount": amount,
                "max_sol_cost": max_sol_cost,
                "trader_price": trader_price,
                "launchpad": "bonk" if pid == _BONK_PROGRAM_STR else "pump",
            }
        return None
    except Exception:
        return None


async def _delayed_swarm_override_entry(client: Client, kp: Optional[Keypair],
                                         mint: str, signer: str, trader_price: float,
                                         initial_swarm_size: int):
    """V41.17zb: wait 3s and only enter if swarm GREW.

    Decoded from C25kDotw vs HCqzUnse/CVZHqoMv: winners had swarms that kept
    growing post-trigger (C25kDotw: 3 → 4 → 5 → 6 → 8 over 10s). Losers had
    swarms that died at the trigger size (HCqzUnse: stayed at 3, CVZHqoMv:
    stayed at 4). 3-second sustain wait filters the dead swarms perfectly.
    """
    await asyncio.sleep(3.0)
    try:
        if mint in _swarm_override_entered or mint in positions:
            return
        history = _signer_history_per_mint.get(mint, [])
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - 10_000
        current_swarm = len({s for s, t in history if t >= cutoff})
        if current_swarm > initial_swarm_size:
            _swarm_override_entered.add(mint)
            _copy_trade_stats["swarm_override"] = _copy_trade_stats.get("swarm_override", 0) + 1
            log(f"  *** SWARM-SUSTAIN OVERRIDE *** {mint[:8]}: swarm grew "
                f"{initial_swarm_size}->{current_swarm} in 3s, entering")
            asyncio.create_task(graduation_snipe(
                client, kp, mint, launchpad="copy_fast_swarm",
                signer=signer, trader_price=trader_price))
        else:
            _copy_trade_stats["swarm_stalled"] = _copy_trade_stats.get("swarm_stalled", 0) + 1
            log(f"  *** SWARM-STALL SKIP *** {mint[:8]}: swarm stalled at "
                f"{current_swarm} (initial {initial_swarm_size}), avoiding likely loss")
    finally:
        _swarm_pending.discard(mint)


async def _swarm_compound_position(client: Client, kp: Optional[Keypair],
                                   mint: str, swarm_size: int, signer: str):
    """V41.17z8: when SWARM-N detected within 30s of an open position's entry,
    add to it. Half-size for SWARM-2 (0.025 SOL), full-size for SWARM-3+ (0.05).
    Uses merge_position_add — token-weighted average entry, never averages down."""
    lock = _get_swarm_compound_lock(mint)
    reserved = False
    pos = None
    async with lock:
        pos = positions.get(mint)
        if not pos or not _position_open_for_compound(pos):
            return
        if (pos.adds_done or 0) >= 2:
            return  # already compounded twice — cap
        age_s = time.time() - pos.open_time
        if age_s >= 30 or pos.peak_price < SWARM_COMPOUND_MIN_MULT:
            return
        cached = _bc_cache_price_for_pos(pos)
        if not cached:
            log(f"  SWARM-COMPOUND SKIP {mint[:8]}: no fresh bc-cache price")
            return
        current_price, complete, age_ms = cached
        if complete or not pos.entry_price:
            return
        current_mult = current_price / pos.entry_price
        if current_mult < SWARM_COMPOUND_MIN_MULT:
            log(f"  SWARM-COMPOUND SKIP {mint[:8]}: mult={current_mult:.3f}x "
                f"< {SWARM_COMPOUND_MIN_MULT:.3f}x (age={age_ms}ms)")
            return
        pos.adds_done = (pos.adds_done or 0) + 1
        reserved = True
        _persist_positions()
    add_amount = GRAD_AMOUNT_SOL * 0.5 if swarm_size == 2 else GRAD_AMOUNT_SOL
    log(f"  SWARM-COMPOUND {mint[:8]} (SWARM-{swarm_size}): reserved add #{pos.adds_done}, "
        f"adding {add_amount:.4f} SOL on {signer[:8]} only after >={SWARM_COMPOUND_MIN_MULT:.3f}x")

    async def rollback_reservation() -> None:
        nonlocal reserved
        if not reserved:
            return
        async with lock:
            current = positions.get(mint)
            if current is pos and (current.adds_done or 0) > 0:
                current.adds_done = max(0, (current.adds_done or 0) - 1)
                _persist_positions()
        reserved = False

    try:
        add_pos = await asyncio.to_thread(buy_token, kp, client, mint, add_amount)
    except Exception as e:
        log(f"  SWARM-COMPOUND BUY FAILED {mint[:8]}: {type(e).__name__}: {e}")
        await rollback_reservation()
        return
    if not add_pos:
        log(f"  SWARM-COMPOUND BUY FAILED {mint[:8]} (no pos returned)")
        await rollback_reservation()
        return
    async with lock:
        current = positions.get(mint)
        if current is not pos or not _position_open_for_compound(current):
            log(f"  SWARM-COMPOUND POST-CLOSE {mint[:8]}: add filled after close; not merging")
            current = None
        else:
            merge_position_add(current, add_pos)
            _persist_positions()
            reserved = False
            log(f"  SWARM-COMPOUND DONE {mint[:8]}: total exposure now {current.entry_amount_sol:.4f} SOL "
                f"(adds={current.adds_done})")
    if current is None:
        if not PAPER_TRADING:
            await asyncio.to_thread(sell_token, kp, client, add_pos, 1.0, 1.0)
        await rollback_reservation()


async def _swarm_scout_position(client: Client, kp: Optional[Keypair], mint: str,
                                swarm_size: int, signer: str,
                                trader_price: float) -> None:
    """Small confirm-gated entry for tracked-wallet swarms that the 1.2s
    market-tape window can miss. This keeps the old full-size swarm lane off."""
    try:
        _copy_trade_stats["swarm_scout_candidates"] = (
            _copy_trade_stats.get("swarm_scout_candidates", 0) + 1
        )
        now_ms = int(time.time() * 1000)
        if (mint in positions or mint in _positions_closing
                or _mint_recently_closed(mint, now_ms / 1000.0)
                or now_ms - _market_tape_entered_recent.get(mint, 0) < MARKET_TAPE_COOLDOWN_SEC * 1000
                or _market_tape_rate_limited(now_ms)):
            _copy_trade_stats["swarm_scout_busy"] = _copy_trade_stats.get("swarm_scout_busy", 0) + 1
            return
        if trader_price <= 0:
            _copy_trade_stats["swarm_scout_no_price"] = _copy_trade_stats.get("swarm_scout_no_price", 0) + 1
            return
        bc_move = _bc_cache_move_for_mint(
            mint,
            max(int(COPY_FAST_CONFIRM_SWARM_WINDOW_SEC * 1000), 1500),
            MARKET_TAPE_BC_CACHE_MAX_AGE_MS,
        )
        latest_price = _bc_cache_price_for_mint(mint, MARKET_TAPE_BC_CACHE_MAX_AGE_MS)
        if not bc_move or not latest_price:
            _copy_trade_stats["swarm_scout_no_price"] = _copy_trade_stats.get("swarm_scout_no_price", 0) + 1
            return
        move_mult, age_ms, complete = bc_move
        if complete or move_mult < SWARM_SCOUT_MIN_BC_MOVE or move_mult > SWARM_SCOUT_MAX_BC_MOVE:
            _copy_trade_stats["swarm_scout_range"] = _copy_trade_stats.get("swarm_scout_range", 0) + 1
            if move_mult < SWARM_SCOUT_MIN_BC_MOVE:
                _copy_trade_stats["swarm_scout_range_low"] = _copy_trade_stats.get("swarm_scout_range_low", 0) + 1
            if move_mult > SWARM_SCOUT_MAX_BC_MOVE:
                _copy_trade_stats["swarm_scout_range_high"] = _copy_trade_stats.get("swarm_scout_range_high", 0) + 1
            log(f"  SWARM-SCOUT BLOCK {mint[:8]}: bc={move_mult:.3f}x outside "
                f"{SWARM_SCOUT_MIN_BC_MOVE:.3f}-{SWARM_SCOUT_MAX_BC_MOVE:.3f}x "
                f"swarm={swarm_size} age={age_ms}ms")
            return
        price_ratio = latest_price[0] / trader_price
        if price_ratio < MARKET_TAPE_MIN_PRICE_RATIO or price_ratio > MARKET_TAPE_MAX_PRICE_RATIO:
            _market_tape_ratio_violation_until[mint] = now_ms + int(MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC * 1000)
            _copy_trade_stats["swarm_scout_ratio"] = _copy_trade_stats.get("swarm_scout_ratio", 0) + 1
            log(f"  SWARM-SCOUT BLOCK {mint[:8]}: price_ratio={price_ratio:.3f}x "
                f"outside {MARKET_TAPE_MIN_PRICE_RATIO:.2f}-{MARKET_TAPE_MAX_PRICE_RATIO:.2f}x "
                f"swarm={swarm_size} bc={move_mult:.3f}x")
            return
        trigger_price = latest_price[0]
        if SWARM_SCOUT_CONFIRM_DELAY_SEC > 0:
            await asyncio.sleep(SWARM_SCOUT_CONFIRM_DELAY_SEC)
        confirm_price = _bc_cache_price_for_mint(mint, MARKET_TAPE_BC_CACHE_MAX_AGE_MS)
        if not confirm_price or confirm_price[1]:
            _copy_trade_stats["swarm_scout_confirm"] = _copy_trade_stats.get("swarm_scout_confirm", 0) + 1
            return
        confirm_mult = confirm_price[0] / trigger_price if trigger_price > 0 else 0.0
        if confirm_mult < SWARM_SCOUT_CONFIRM_MIN_MULT:
            _copy_trade_stats["swarm_scout_confirm"] = _copy_trade_stats.get("swarm_scout_confirm", 0) + 1
            log(f"  SWARM-SCOUT BLOCK {mint[:8]}: confirm_mult={confirm_mult:.3f}x "
                f"< {SWARM_SCOUT_CONFIRM_MIN_MULT:.3f}x after {SWARM_SCOUT_CONFIRM_DELAY_SEC:.2f}s")
            return
        confirm_ratio = confirm_price[0] / trader_price
        if confirm_ratio < MARKET_TAPE_MIN_PRICE_RATIO or confirm_ratio > MARKET_TAPE_MAX_PRICE_RATIO:
            _market_tape_ratio_violation_until[mint] = int(time.time() * 1000) + int(MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC * 1000)
            _copy_trade_stats["swarm_scout_ratio"] = _copy_trade_stats.get("swarm_scout_ratio", 0) + 1
            log(f"  SWARM-SCOUT BLOCK {mint[:8]}: confirm price_ratio={confirm_ratio:.3f}x "
                f"outside {MARKET_TAPE_MIN_PRICE_RATIO:.2f}-{MARKET_TAPE_MAX_PRICE_RATIO:.2f}x")
            return
        post_ms = int(time.time() * 1000)
        if (mint in positions or mint in _positions_closing
                or _market_tape_rate_limited(post_ms)
                or post_ms - _market_tape_entered_recent.get(mint, 0) < MARKET_TAPE_COOLDOWN_SEC * 1000):
            _copy_trade_stats["swarm_scout_busy"] = _copy_trade_stats.get("swarm_scout_busy", 0) + 1
            return

        graduated_seen.add(mint)
        if len(graduated_seen) > 500:
            graduated_seen.clear()
            graduated_seen.add(mint)
        _swarm_override_entered.add(mint)
        _market_tape_entered_recent[mint] = post_ms
        _market_tape_entry_times.append(post_ms)
        _copy_trade_stats["swarm_scout_triggers"] = _copy_trade_stats.get("swarm_scout_triggers", 0) + 1
        _copy_trade_stats["market_tape_triggers"] = _copy_trade_stats.get("market_tape_triggers", 0) + 1
        reason = (f"swarm={swarm_size} signer={signer[:8]} bc={move_mult:.3f}x "
                  f"ratio={price_ratio:.3f}x confirm={confirm_mult:.3f}x age={age_ms}ms")
        log(f"  *** SWARM-SCOUT TRIGGER *** {mint[:8]}: {reason}")
        asyncio.create_task(_enter_market_tape_position(
            client, kp, mint, post_ms, reason,
            amount_sol=SWARM_SCOUT_AMOUNT_SOL,
            launchpad="market_tape_scout",
            quality_score=5,
        ))
    except Exception as e:
        _copy_trade_stats["swarm_scout_busy"] = _copy_trade_stats.get("swarm_scout_busy", 0) + 1
        log(f"  SWARM-SCOUT ERR {mint[:8]}: {type(e).__name__}: {e}")
    finally:
        _swarm_scout_pending.discard(mint)


async def _dispatch_copy_signal(client: Client, kp: Optional[Keypair], sig: str,
                                signer: str, mint: str, trader_price: float, source: str):
    """V41.17i: shared gate cascade for both fast (shred) and slow (getTransaction) paths.
    Runs dedup → memecoin filter → claim → rug check → curve gate → first-buyer gate,
    then fires graduation_snipe(launchpad="copy_fast") on success.

    V41.17z7: track per-mint signer history for swarm detection. When dedup fires
    on a mint we already entered, log it as a SWARM-N event (N distinct wallets
    bought this mint within 60s)."""
    # Record this signal in the per-mint signer history BEFORE dedup check
    now_ms = int(time.time() * 1000)
    history = _signer_history_per_mint.setdefault(mint, [])
    # Prune entries older than 60s
    cutoff = now_ms - 60_000
    history[:] = [(s, t) for s, t in history if t >= cutoff]
    history.append((signer, now_ms))
    # Cap dict size to prevent leak
    if len(_signer_history_per_mint) > 1000:
        oldest = sorted(_signer_history_per_mint.items(), key=lambda x: max((t for _, t in x[1]), default=0))
        for k, _ in oldest[:200]:
            _signer_history_per_mint.pop(k, None)

    if mint.lower().endswith(("pump", "bonk")):
        cached = _bc_cache_price_for_mint(mint, max(MARKET_TAPE_BC_CACHE_MAX_AGE_MS, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS))
        _alpha_schedule_shadow(
            mint, signer, "copy_fast",
            trader_price=trader_price,
            trigger_price=float(cached[0]) if cached and not cached[1] else 0.0,
            sig=sig,
        )

    if mint in graduated_seen:
        _copy_trade_stats["dedup"] += 1
        # SWARM detection: how many distinct wallets bought this mint within 10s?
        ten_s_ago = now_ms - 10_000
        recent_signers = {s for s, t in history if t >= ten_s_ago}
        if len(recent_signers) >= 2:
            _copy_trade_stats["swarm_detected"] = _copy_trade_stats.get("swarm_detected", 0) + 1
            log(f"  *** SWARM-{len(recent_signers)} *** {mint[:8]}: {len(recent_signers)} pool wallets bought within 10s "
                f"(last: {signer[:8]}, total signers in 60s: {len({s for s, _ in history})})")
            # V41.17z8: COMPOUND on swarm — if we have an open position on this
            # mint that's young and not crashed, add to it. Caps at 2 compounds
            # per position to prevent runaway size on extended pumps.
            existing = positions.get(mint)
            if (existing and _position_open_for_compound(existing) and (existing.adds_done or 0) < 2):
                age_s = time.time() - existing.open_time
                if age_s < 30 and existing.peak_price >= SWARM_COMPOUND_MIN_MULT:
                    asyncio.create_task(_swarm_compound_position(
                        client, kp, mint, len(recent_signers), signer))
            # V41.18: SWARM-3+ on a rug-blocked mint is only a candidate now.
            # graduation_snipe runs the bc-cache confirm gate before any buy, so
            # dead swarms and instant dumps don't get capital.
            elif (not existing
                    and len(recent_signers) >= 3
                    and mint in _rug_blocked_recent
                    and (now_ms - _rug_blocked_recent[mint]) < 30_000
                    and mint not in _swarm_override_entered):
                if COPY_FAST_SWARM_ENTRY_ENABLED:
                    _swarm_override_entered.add(mint)
                    _copy_trade_stats["swarm_override"] = _copy_trade_stats.get("swarm_override", 0) + 1
                    log(f"  *** SWARM-OVERRIDE-CANDIDATE *** {mint[:8]}: {len(recent_signers)} wallets agree, "
                        f"rug-block override candidate; waiting for bc-cache follow-through")
                    asyncio.create_task(graduation_snipe(
                        client, kp, mint, launchpad="copy_fast_swarm",
                        signer=signer, trader_price=trader_price))
                elif (SWARM_SCOUT_ENABLED
                        and len(recent_signers) >= SWARM_SCOUT_MIN_SIGNERS
                        and mint not in _swarm_scout_pending):
                    _swarm_scout_pending.add(mint)
                    log(f"  *** SWARM-SCOUT-CANDIDATE *** {mint[:8]}: {len(recent_signers)} wallets agree, "
                        f"checking bc-cache/ratio/confirm for tiny scout")
                    asyncio.create_task(_swarm_scout_position(
                        client, kp, mint, len(recent_signers), signer, trader_price))
        return
    mint_lc = mint.lower()
    if not (mint_lc.endswith("pump") or mint_lc.endswith("bonk")):
        _copy_trade_stats["non_memecoin"] += 1
        return
    graduated_seen.add(mint)
    if len(graduated_seen) > 500:
        graduated_seen.clear()
    # V41.17w: stripped curve_pct + first_buyer gates (no proven save in V41.17v overnight).
    # Rug check + Fix #11 ratio band (in graduation_snipe) are the kept gates.
    safe, reason, _snap = await asyncio.to_thread(_rug_check_with_snapshot, mint)
    if not safe:
        _copy_trade_stats["rug_blocked"] += 1
        if "fresh bundle" in (reason or ""):
            _copy_trade_stats["bundle_fresh_blocked"] += 1
        # V41.17z9: remember this mint was rug-blocked. If SWARM-3+ forms on it
        # within 30s, the swarm overrides our rug heuristic.
        _rug_blocked_recent[mint] = int(time.time() * 1000)
        # Bound dict size
        if len(_rug_blocked_recent) > 500:
            cutoff = int(time.time() * 1000) - 60_000
            for m in list(_rug_blocked_recent.keys()):
                if _rug_blocked_recent[m] < cutoff:
                    _rug_blocked_recent.pop(m, None)
        log(f"  COPY TRADE RUG-BLOCKED {signer[:8]} -> {mint[:8]}: {reason}")
        return
    # V41.17z trend gate: skip if bonding curve was DUMPING in the 5s before
    # this trader's buy. Empirical pattern from feature test: trader buys into
    # a -3.3% recent dump → token kept dumping → -7% SL. Fail-OPEN if cache miss.
    if TREND_GATE_ENABLED:
        trend_5s = _compute_trend_5s_for_mint(mint)
        if trend_5s is not None and trend_5s < TREND_GATE_5S_MIN:
            _copy_trade_stats["trend_blocked"] = _copy_trade_stats.get("trend_blocked", 0) + 1
            log(f"  COPY TRADE TREND-BLOCKED {signer[:8]} -> {mint[:8]}: "
                f"trend_5s={trend_5s*100:+.1f}% < {TREND_GATE_5S_MIN*100:+.1f}% (curve dumping)")
            return
    _copy_trade_stats["fired"] += 1
    log(f"*** COPY TRADE *** {signer[:8]} bought {mint} (sig={sig[:16]}) trader_px={trader_price:.4e} [{source}]")
    asyncio.create_task(graduation_snipe(client, kp, mint, launchpad="copy_fast",
                                        signer=signer, trader_price=trader_price))


def _market_tape_rate_limited(now_ms: int) -> bool:
    cutoff = now_ms - 60_000
    while _market_tape_entry_times and _market_tape_entry_times[0] < cutoff:
        _market_tape_entry_times.popleft()
    return len(_market_tape_entry_times) >= MARKET_TAPE_MAX_ENTRIES_PER_MIN


def _market_tape_cleanup(now_ms: int) -> None:
    stale = now_ms - int(max(MARKET_TAPE_COOLDOWN_SEC * 1000, 60_000))
    for mint, ts in list(_market_tape_entered_recent.items()):
        if ts < stale:
            _market_tape_entered_recent.pop(mint, None)
    context_stale = now_ms - int(max(MARKET_TAPE_ALPHA_CONTEXT_COOLDOWN_SEC * 1000, 10_000))
    for context, ts in list(_market_tape_alpha_context_recent_ms.items()):
        if ts < context_stale:
            _market_tape_alpha_context_recent_ms.pop(context, None)
    moon_context_stale = now_ms - int(max(MOONSHOT_CONTEXT_COOLDOWN_SEC * 1000, 10_000))
    for context, ts in list(_moonshot_context_recent_ms.items()):
        if ts < moon_context_stale:
            _moonshot_context_recent_ms.pop(context, None)
    for mint, until_ms in list(_market_tape_ratio_violation_until.items()):
        if until_ms <= now_ms:
            _market_tape_ratio_violation_until.pop(mint, None)
    stale_seen = now_ms - 10 * 60_000
    for mint, ts in list(_market_tape_last_seen_ms.items()):
        if ts < stale_seen:
            _market_tape_last_seen_ms.pop(mint, None)
            _market_tape_first_seen_ms.pop(mint, None)
            _market_tape_per_mint.pop(mint, None)


async def _close_grad_position_from_market_tape(client: Client, kp: Optional[Keypair],
                                                pos: Position, reason: str,
                                                multiplier: float) -> bool:
    """Close a hot tape/copy position from the shred tape before the poller catches up."""
    if positions.get(pos.mint) is not pos or pos.remaining_pct <= 0.01:
        return False
    if pos.mint in _positions_closing:
        return False
    _positions_closing.add(pos.mint)
    try:
        sol_recv = await asyncio.to_thread(sell_token, kp, client, pos, 1.0, multiplier)
        if sol_recv is None:
            log(f"  MARKET-TAPE EXIT FAILED {pos.mint[:8]} ({reason}) — keeping position")
            return False
        if positions.get(pos.mint) is not pos:
            return False
        pos.realized_sol += sol_recv
        pos.remaining_pct = 0.0
        _persist_positions()
        pnl = pos.realized_sol - pos.entry_amount_sol
        _record_trade_close(pnl)
        _copy_trade_stats["market_tape_exits"] = _copy_trade_stats.get("market_tape_exits", 0) + 1
        log(f"  CLOSED GRAD {pos.mint[:8]} peak={pos.peak_price:.2f}x "
            f"recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} "
            f"pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} "
            f"W={session_wins} L={session_losses} reason={reason}")
        _remove_open_position(pos)
        _maybe_stop_for_daily_loss()
        return True
    finally:
        if positions.get(pos.mint) is pos and pos.remaining_pct > 0.01:
            _positions_closing.discard(pos.mint)


async def _maybe_market_tape_exit(client: Client, kp: Optional[Keypair],
                                  mint: str, now_ms: int,
                                  event: Optional[dict] = None) -> bool:
    if not MARKET_TAPE_EXIT_ENABLED:
        return False
    pos = positions.get(mint)
    if not pos or pos.mint in _positions_closing:
        return False
    if pos.launchpad not in ("market_tape", "market_tape_scout", "moonshot_ignition",
                             "velocity_ignition", "copy_fast", "copy_fast_swarm",
                             "copy_fast_solo", "copy_fast_alpha"):
        return False

    tape = list(_market_tape_per_mint.get(mint, ()))
    if not tape:
        return False
    open_ms = int(pos.open_time * 1000)
    cutoff = max(now_ms - MARKET_TAPE_EXIT_WINDOW_MS, open_ms - 100)
    recent = [e for e in tape if e["ts"] >= cutoff]
    buys = [e for e in recent if e.get("is_buy")]
    sells = [e for e in recent if not e.get("is_buy")]
    buy_sol = sum(float(e.get("sol") or 0.0) for e in buys)
    sell_sol = sum(float(e.get("sol") or 0.0) for e in sells)

    multiplier = (pos.last_price / pos.entry_price) if pos.entry_price and pos.last_price else 1.0
    price_source = "last"
    cached = _bc_cache_price_for_pos(pos)
    if cached:
        price, complete, age_ms = cached
        if not complete and price > 0:
            pos.last_price = price
            multiplier = price / pos.entry_price if pos.entry_price else multiplier
            if multiplier > pos.peak_price:
                pos.peak_price = multiplier
            price_source = f"bc_cache:{age_ms}ms"

    if pos.launchpad in ("market_tape", "market_tape_scout"):
        tp_mult = MARKET_TAPE_SCOUT_TP_MULT if pos.launchpad == "market_tape_scout" else MARKET_TAPE_TP_MULT
        if multiplier >= tp_mult:
            return await _close_grad_position_from_market_tape(
                client, kp, pos,
                f"{pos.launchpad.upper()} TAPE-TP {tp_mult:.3f}x mult={multiplier:.3f}x src={price_source}",
                multiplier,
            )
    if pos.launchpad == "moonshot_ignition":
        if multiplier <= MOONSHOT_DROP_EXIT_MULT:
            return await _close_grad_position_from_market_tape(
                client, kp, pos,
                f"MOONSHOT DROP EXIT mult={multiplier:.3f}x peak={pos.peak_price:.3f}x src={price_source}",
                multiplier,
            )
        if pos.peak_price >= MOONSHOT_TRAIL_ACTIVATION:
            trail_floor = max(1.08, pos.peak_price * MOONSHOT_TRAIL_DISTANCE)
            if multiplier <= trail_floor:
                return await _close_grad_position_from_market_tape(
                    client, kp, pos,
                    f"MOONSHOT TAPE-TRAIL mult={multiplier:.3f}x floor={trail_floor:.3f}x "
                    f"peak={pos.peak_price:.3f}x src={price_source}",
                    multiplier,
                )
    if pos.launchpad == "velocity_ignition":
        if multiplier <= VELOCITY_DROP_EXIT_MULT:
            return await _close_grad_position_from_market_tape(
                client, kp, pos,
                f"VELOCITY DROP EXIT mult={multiplier:.3f}x peak={pos.peak_price:.3f}x src={price_source}",
                multiplier,
            )
        if pos.peak_price >= VELOCITY_TRAIL_ACTIVATION:
            trail_floor = max(1.035, pos.peak_price * VELOCITY_TRAIL_DISTANCE)
            if multiplier <= trail_floor:
                return await _close_grad_position_from_market_tape(
                    client, kp, pos,
                    f"VELOCITY TAPE-TRAIL mult={multiplier:.3f}x floor={trail_floor:.3f}x "
                    f"peak={pos.peak_price:.3f}x src={price_source}",
                    multiplier,
                )

    if multiplier <= MARKET_TAPE_EXIT_DROP_MULT:
        return await _close_grad_position_from_market_tape(
            client, kp, pos,
            f"MARKET-TAPE DROP EXIT mult={multiplier:.3f}x peak={pos.peak_price:.3f}x src={price_source}",
            multiplier,
        )

    age_since_open = max(0.0, now_ms / 1000.0 - pos.open_time)
    if (pos.launchpad in ("moonshot_ignition", "velocity_ignition")
            and age_since_open < VELOCITY_SELL_PRESSURE_MIN_AGE_SEC):
        return False
    single_tracked_sell = bool(
        event
        and not event.get("is_buy")
        and event.get("tracked")
        and age_since_open >= MARKET_TAPE_EXIT_SINGLE_TRACKED_SELL_MIN_AGE_SEC
    )
    sell_pressure = bool(sells) and (
        len(sells) >= 2
        or sell_sol >= MARKET_TAPE_EXIT_MIN_SELL_SOL
        or (buy_sol > 0 and sell_sol >= buy_sol * MARKET_TAPE_EXIT_SELL_BUY_RATIO)
        or single_tracked_sell
    )
    if sell_pressure and (multiplier < 1.030 or pos.peak_price < 1.040):
        return await _close_grad_position_from_market_tape(
            client, kp, pos,
            f"MARKET-TAPE SELL-PRESSURE EXIT {len(sells)}S/{len(buys)}B "
            f"sell={sell_sol:.3f} buy={buy_sol:.3f} mult={multiplier:.3f}x",
            multiplier,
        )
    return False


async def _enter_market_tape_position(client: Client, kp: Optional[Keypair], mint: str,
                                      signal_time_ms: int, reason: str,
                                      amount_sol: float = MARKET_TAPE_AMOUNT_SOL,
                                      launchpad: str = "market_tape",
                                      quality_score: int = 7) -> None:
    claimed_entry = False
    label = (
        "MOONSHOT-IGNITION" if launchpad == "moonshot_ignition"
        else ("VELOCITY-IGNITION" if launchpad == "velocity_ignition"
        else ("MARKET-TAPE-SCOUT" if launchpad == "market_tape_scout" else "MARKET-TAPE")
              )
    )
    try:
        blocked, why = _entry_circuit_breakers_open()
        if blocked:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_entry_halt")
            graduated_seen.discard(mint)
            log(f"  {label} HALT {mint[:8]}: {why}")
            return
        if mint in positions:
            _mt_gate("mt_pos")
            graduated_seen.discard(mint)
            return
        if not _claim_entry_mint(mint, launchpad):
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_entry_claim")
            graduated_seen.discard(mint)
            return
        claimed_entry = True
        log(f"  {label} ENTRY {mint[:8]}: {reason}, buying {amount_sol:.4f} SOL")
        pos = None
        if (not PAPER_TRADING and kp
                and abs(WARM_SWAP_AMOUNT_SOL - amount_sol) < 1e-9):
            warm = _consume_warm_swap_tx(mint)
            if warm:
                tx_b64, _lvbh = warm
                log(f"  {label} WARM HIT {mint[:8]}: pre-built tx, shipping")
                sig_out = await asyncio.to_thread(execute_swap, kp, client, tx_b64, False)
                if sig_out:
                    entry_lamports = int(amount_sol * 10**WSOL_DECIMALS)
                    bookkeep = await asyncio.to_thread(jupiter_quote, SOL_MINT, mint, entry_lamports)
                    out_amt = float(bookkeep.get("outAmount", 0)) if bookkeep else 0.0
                    if out_amt > 0:
                        pos = Position(
                            mint=mint,
                            entry_price=entry_lamports / out_amt,
                            entry_amount_sol=amount_sol,
                            token_amount=out_amt,
                            open_time=time.time(),
                            bc_pda=derive_bc_pda(Pubkey.from_string(mint)),
                        )
        if pos is None:
            pos = await asyncio.to_thread(buy_token, kp, client, mint, amount_sol)
        if not pos:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_entry_buy_fail")
            graduated_seen.discard(mint)
            _release_entry_mint(mint)
            log(f"  {label} BUY FAILED {mint[:8]}")
            return
        pos.strategy = "graduation"
        pos.late_scalp = True
        pos.entry_progress = 1.0
        pos.entry_size_sol = amount_sol
        pos.quality_score = quality_score
        pos.launchpad = launchpad
        pos.signal_time_ms = signal_time_ms
        _store_open_position(pos)
        _record_entry_opened()
        _copy_trade_stats["market_tape_entered"] = _copy_trade_stats.get("market_tape_entered", 0) + 1
        if not await _maybe_market_tape_exit(client, kp, mint, int(time.time() * 1000)):
            asyncio.create_task(manage_graduation_position(client, kp, pos))
    except Exception as e:
        if claimed_entry:
            _release_entry_mint(mint)
        _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
        _mt_gate("mt_entry_err")
        graduated_seen.discard(mint)
        log(f"  MARKET-TAPE ENTRY ERR {mint[:8]}: {type(e).__name__}: {e}")


async def _handle_market_tape_trade(client: Client, kp: Optional[Keypair], sig: str,
                                    trade: dict, trader_set: set) -> None:
    if not MARKET_TAPE_ENABLED:
        return
    mint = trade.get("mint") or ""
    mint_lc = mint.lower()
    if not mint or not mint_lc.endswith("pump"):
        return
    now_ms = int(time.time() * 1000)
    signer = trade.get("signer") or ""
    first_seen_ms = _market_tape_first_seen_ms.setdefault(mint, now_ms)
    _market_tape_last_seen_ms[mint] = now_ms
    observed_age_ms = max(0, now_ms - first_seen_ms)
    event = {
        "ts": now_ms,
        "signer": signer,
        "is_buy": bool(trade.get("is_buy")),
        "sol": float(trade.get("sol_lamports") or 0) / 1e9,
        "tracked": signer in trader_set,
        "sig": sig,
    }
    tape = _market_tape_per_mint[mint]
    tape.append(event)
    cutoff_10s = now_ms - 10_000
    while tape and tape[0]["ts"] < cutoff_10s:
        tape.popleft()
    _copy_trade_stats["market_tape_seen"] = _copy_trade_stats.get("market_tape_seen", 0) + 1

    if await _maybe_market_tape_exit(client, kp, mint, now_ms, event):
        return
    if not event["is_buy"]:
        return
    # Do not block on graduated_seen here. The copy lane claims mints before it
    # awaits slow checks; market_tape must still be allowed to fire if clustered
    # flow proves up before the copy confirm gate does.
    if mint in positions or mint in _positions_closing:
        _mt_gate("mt_pos")
        return
    _market_tape_cleanup(now_ms)
    if now_ms - _market_tape_entered_recent.get(mint, 0) < MARKET_TAPE_COOLDOWN_SEC * 1000:
        _mt_gate("mt_cooldown")
        return
    if _market_tape_rate_limited(now_ms):
        _mt_gate("mt_rate")
        return

    birth_cutoff = now_ms - MARKET_TAPE_BIRTH_WINDOW_MS
    birth_recent = [e for e in tape if e["ts"] >= birth_cutoff]
    birth_buys = [e for e in birth_recent if e["is_buy"]]
    birth_sells = [e for e in birth_recent if not e["is_buy"]]
    birth_unique = {e["signer"] for e in birth_buys if e["signer"]}
    birth_tracked = {e["signer"] for e in birth_buys if e.get("tracked")}
    pool_swarm_signers = _recent_swarm_signers(mint, COPY_FAST_CONFIRM_SWARM_WINDOW_SEC, now_ms)
    birth_tracked_count = max(len(birth_tracked), len(pool_swarm_signers))
    birth_buy_sol = sum(e["sol"] for e in birth_buys)
    birth_sell_sol = sum(e["sol"] for e in birth_sells)
    birth_lane = (
        MARKET_TAPE_BIRTH_ENABLED
        and observed_age_ms <= MARKET_TAPE_BIRTH_MAX_AGE_SEC * 1000
        and len(birth_unique) >= MARKET_TAPE_BIRTH_MIN_UNIQUE
        and birth_tracked_count >= MARKET_TAPE_BIRTH_MIN_TRACKED
        and birth_buy_sol >= MARKET_TAPE_BIRTH_MIN_BUY_SOL
        and birth_sell_sol <= MARKET_TAPE_BIRTH_MAX_SELL_SOL
    )

    cutoff = now_ms - MARKET_TAPE_WINDOW_MS
    recent = birth_recent if birth_lane else [e for e in tape if e["ts"] >= cutoff]
    buys = birth_buys if birth_lane else [e for e in recent if e["is_buy"]]
    sells = birth_sells if birth_lane else [e for e in recent if not e["is_buy"]]
    unique_buyers = birth_unique if birth_lane else {e["signer"] for e in buys if e["signer"]}
    tracked_buyers = birth_tracked if birth_lane else {e["signer"] for e in buys if e.get("tracked")}
    effective_tracked_count = max(len(tracked_buyers), len(pool_swarm_signers))
    if event["tracked"] or effective_tracked_count > 0 or len(unique_buyers) >= 2:
        _mark_warm_priority_mint(mint)
    buy_sol = birth_buy_sol if birth_lane else sum(e["sol"] for e in buys)
    sell_sol = birth_sell_sol if birth_lane else sum(e["sol"] for e in sells)
    if (ALPHA_SHADOW_MARKET_TAPE
            and (event["tracked"] or len(unique_buyers) >= 3 or effective_tracked_count >= 2)):
        _alpha_schedule_shadow(
            mint, signer, "market_tape",
            trader_price=float(trade.get("trader_price") or 0.0),
            sig=f"{sig}:mt:{len(unique_buyers)}:{effective_tracked_count}",
        )
    trader_price = float(trade.get("trader_price") or 0.0)
    if not _mint_recently_closed(mint, now_ms / 1000.0):
        velocity_plan = _velocity_ignition_plan(
            mint,
            len(unique_buyers),
            effective_tracked_count,
            buy_sol,
            sell_sol,
        )
        if velocity_plan:
            _copy_trade_stats["velocity_candidates"] = _copy_trade_stats.get("velocity_candidates", 0) + 1
            trigger_price = float(velocity_plan.get("trigger_price") or 0.0)
            if trigger_price <= 0:
                _copy_trade_stats["velocity_blocked"] = _copy_trade_stats.get("velocity_blocked", 0) + 1
                _mt_gate("vel_no_price")
                return
            if VELOCITY_CONFIRM_DELAY_SEC > 0:
                await asyncio.sleep(VELOCITY_CONFIRM_DELAY_SEC)
                confirm_price = _bc_cache_price_for_mint(
                    mint,
                    max(VELOCITY_MAX_CACHE_AGE_MS, MARKET_TAPE_BC_CACHE_MAX_AGE_MS),
                )
                if not confirm_price or confirm_price[1] or confirm_price[0] <= 0:
                    _copy_trade_stats["velocity_blocked"] = _copy_trade_stats.get("velocity_blocked", 0) + 1
                    _mt_gate("vel_confirm")
                    log(f"  VELOCITY BLOCK {mint[:8]}: no fresh confirm price after "
                        f"{VELOCITY_CONFIRM_DELAY_SEC:.2f}s")
                    return
                confirm_mult = float(confirm_price[0]) / trigger_price
                if confirm_mult < VELOCITY_CONFIRM_MIN_MULT:
                    _copy_trade_stats["velocity_blocked"] = _copy_trade_stats.get("velocity_blocked", 0) + 1
                    _mt_gate("vel_confirm")
                    log(f"  VELOCITY BLOCK {mint[:8]}: confirm_mult={confirm_mult:.3f}x "
                        f"< {VELOCITY_CONFIRM_MIN_MULT:.3f}x after "
                        f"{VELOCITY_CONFIRM_DELAY_SEC:.2f}s")
                    return
                velocity_plan["reason"] = (
                    f"{velocity_plan.get('reason', 'velocity')} confirm={confirm_mult:.3f}x/"
                    f"{VELOCITY_CONFIRM_DELAY_SEC:.2f}s"
                )
            graduated_seen.add(mint)
            if len(graduated_seen) > 500:
                graduated_seen.clear()
                graduated_seen.add(mint)
            _market_tape_entered_recent[mint] = now_ms
            _market_tape_entry_times.append(now_ms)
            _copy_trade_stats["market_tape_triggers"] = _copy_trade_stats.get("market_tape_triggers", 0) + 1
            _copy_trade_stats["velocity_triggers"] = _copy_trade_stats.get("velocity_triggers", 0) + 1
            reason = str(velocity_plan.get("reason") or "velocity ignition")
            log(f"  *** VELOCITY-IGNITION TRIGGER *** {mint[:8]}: {reason}")
            asyncio.create_task(_enter_market_tape_position(
                client, kp, mint, now_ms, reason,
                amount_sol=float(velocity_plan.get("amount") or VELOCITY_AMOUNT_SOL),
                launchpad="velocity_ignition",
                quality_score=int(velocity_plan.get("quality") or 7),
            ))
            return
    moonshot_plan = None
    if _market_tape_ratio_violation_until.get(mint, 0) <= now_ms:
        moonshot_plan = _moonshot_ignition_plan(
            mint, signer, trader_price,
            len(unique_buyers), effective_tracked_count, buy_sol, sell_sol, observed_age_ms,
        )
    if moonshot_plan:
        _copy_trade_stats["moonshot_candidates"] = _copy_trade_stats.get("moonshot_candidates", 0) + 1
        trigger_price = float(moonshot_plan.get("trigger_price") or 0.0)
        if trigger_price <= 0:
            _copy_trade_stats["moonshot_blocked"] = _copy_trade_stats.get("moonshot_blocked", 0) + 1
            _mt_gate("moon_no_price")
            return
        if MOONSHOT_CONFIRM_DELAY_SEC > 0:
            await asyncio.sleep(MOONSHOT_CONFIRM_DELAY_SEC)
            confirm_price = _bc_cache_price_for_mint(
                mint,
                max(MOONSHOT_MAX_CACHE_AGE_MS, MARKET_TAPE_BC_CACHE_MAX_AGE_MS),
            )
            if not confirm_price or confirm_price[1] or confirm_price[0] <= 0:
                _copy_trade_stats["moonshot_blocked"] = _copy_trade_stats.get("moonshot_blocked", 0) + 1
                _mt_gate("moon_confirm")
                log(f"  MOONSHOT BLOCK {mint[:8]}: no fresh confirm price after "
                    f"{MOONSHOT_CONFIRM_DELAY_SEC:.2f}s")
                return
            confirm_mult = float(confirm_price[0]) / trigger_price
            if confirm_mult < MOONSHOT_CONFIRM_MIN_MULT:
                _copy_trade_stats["moonshot_blocked"] = _copy_trade_stats.get("moonshot_blocked", 0) + 1
                _mt_gate("moon_confirm")
                log(f"  MOONSHOT BLOCK {mint[:8]}: confirm_mult={confirm_mult:.3f}x "
                    f"< {MOONSHOT_CONFIRM_MIN_MULT:.3f}x after {MOONSHOT_CONFIRM_DELAY_SEC:.2f}s")
                return
            if trader_price > 0:
                confirm_ratio = float(confirm_price[0]) / trader_price
                if confirm_ratio < MOONSHOT_MIN_PRICE_RATIO or confirm_ratio > MOONSHOT_MAX_PRICE_RATIO:
                    _copy_trade_stats["moonshot_blocked"] = _copy_trade_stats.get("moonshot_blocked", 0) + 1
                    _mt_gate("moon_ratio")
                    log(f"  MOONSHOT BLOCK {mint[:8]}: confirm_ratio={confirm_ratio:.3f}x "
                        f"outside {MOONSHOT_MIN_PRICE_RATIO:.2f}-{MOONSHOT_MAX_PRICE_RATIO:.2f}x")
                    return
        entry_ms = int(time.time() * 1000)
        context = str(moonshot_plan.get("context") or "")
        if context:
            _moonshot_context_recent_ms[context] = entry_ms
        graduated_seen.add(mint)
        if len(graduated_seen) > 500:
            graduated_seen.clear()
            graduated_seen.add(mint)
        _market_tape_entered_recent[mint] = entry_ms
        _market_tape_entry_times.append(entry_ms)
        _copy_trade_stats["market_tape_triggers"] = _copy_trade_stats.get("market_tape_triggers", 0) + 1
        _copy_trade_stats["moonshot_triggers"] = _copy_trade_stats.get("moonshot_triggers", 0) + 1
        reason = str(moonshot_plan.get("reason") or "moonshot ignition")
        log(f"  *** MOONSHOT-IGNITION TRIGGER *** {mint[:8]}: {reason}")
        asyncio.create_task(_enter_market_tape_position(
            client, kp, mint, entry_ms, reason,
            amount_sol=float(moonshot_plan.get("amount") or MOONSHOT_IGNITION_AMOUNT_SOL),
            launchpad="moonshot_ignition",
            quality_score=int(moonshot_plan.get("quality") or 9),
        ))
        return
    alpha_cached_price = _bc_cache_price_for_mint(mint, MARKET_TAPE_BC_CACHE_MAX_AGE_MS)
    alpha_price_ratio = None
    if trader_price > 0 and alpha_cached_price and not alpha_cached_price[1] and alpha_cached_price[0] > 0:
        alpha_price_ratio = float(alpha_cached_price[0]) / trader_price
    alpha_plan = _alpha_market_tape_entry_plan(
        mint, signer,
        trader_price,
        float(alpha_cached_price[0]) if alpha_cached_price and not alpha_cached_price[1] else 0.0,
        len(unique_buyers), effective_tracked_count, buy_sol, sell_sol, observed_age_ms,
    )
    if alpha_plan:
        alpha_ratio_bypass = bool(alpha_plan.get("ratio_bypass"))
        ratio_block_until = _market_tape_ratio_violation_until.get(mint, 0)
        if ratio_block_until > now_ms and not alpha_ratio_bypass:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_ratio")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: recent price_ratio violation "
                f"cooldown {(ratio_block_until - now_ms) / 1000:.1f}s")
            return
        if (alpha_price_ratio is not None
                and (alpha_price_ratio < MARKET_TAPE_MIN_PRICE_RATIO
                     or alpha_price_ratio > MARKET_TAPE_MAX_PRICE_RATIO)):
            if not alpha_ratio_bypass:
                _market_tape_ratio_violation_until[mint] = now_ms + int(MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC * 1000)
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_alpha_ratio")
                log(f"  MARKET-TAPE-ALPHA BLOCK {mint[:8]}: price_ratio={alpha_price_ratio:.3f}x "
                    f"outside {MARKET_TAPE_MIN_PRICE_RATIO:.2f}-{MARKET_TAPE_MAX_PRICE_RATIO:.2f}x")
                return
            log(f"  MARKET-TAPE-ALPHA RATIO-BYPASS {mint[:8]}: price_ratio={alpha_price_ratio:.3f}x "
                f"ctx={alpha_plan.get('context', '')}")
        if not alpha_cached_price or alpha_cached_price[1] or alpha_cached_price[0] <= 0:
            _mt_gate("mt_alpha_no_price")
            return
        trigger_price = float(alpha_cached_price[0])
        if MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC > 0:
            await asyncio.sleep(MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC)
            confirm_price = _bc_cache_price_for_mint(mint, MARKET_TAPE_BC_CACHE_MAX_AGE_MS)
            if not confirm_price or confirm_price[1] or confirm_price[0] <= 0:
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_alpha_confirm")
                log(f"  MARKET-TAPE-ALPHA BLOCK {mint[:8]}: no fresh confirm price after "
                    f"{MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC:.2f}s")
                return
            confirm_mult = float(confirm_price[0]) / trigger_price
            min_confirm_mult = float(alpha_plan.get("min_confirm_mult") or MARKET_TAPE_ALPHA_CONFIRM_MIN_MULT)
            if confirm_mult < min_confirm_mult:
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_alpha_confirm")
                log(f"  MARKET-TAPE-ALPHA BLOCK {mint[:8]}: confirm_mult={confirm_mult:.3f}x "
                    f"< {min_confirm_mult:.3f}x after "
                    f"{MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC:.2f}s")
                return
        entry_ms = int(time.time() * 1000)
        alpha_context = str(alpha_plan.get("context") or "")
        if alpha_context and MARKET_TAPE_ALPHA_CONTEXT_COOLDOWN_SEC > 0:
            cooldown_ms = int(MARKET_TAPE_ALPHA_CONTEXT_COOLDOWN_SEC * 1000)
            last_context_ms = _market_tape_alpha_context_recent_ms.get(alpha_context, 0)
            if entry_ms - last_context_ms < cooldown_ms:
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_alpha_context_cd")
                log(f"  MARKET-TAPE-ALPHA BLOCK {mint[:8]}: context cooldown "
                    f"{(cooldown_ms - (entry_ms - last_context_ms)) / 1000:.1f}s ctx={alpha_context}")
                return
            _market_tape_alpha_context_recent_ms[alpha_context] = entry_ms
        graduated_seen.add(mint)
        if len(graduated_seen) > 500:
            graduated_seen.clear()
            graduated_seen.add(mint)
        _market_tape_entered_recent[mint] = entry_ms
        _market_tape_entry_times.append(entry_ms)
        _copy_trade_stats["market_tape_triggers"] = _copy_trade_stats.get("market_tape_triggers", 0) + 1
        guard_note = ""
        if alpha_plan.get("ratio_bypass") or alpha_plan.get("move_bypass"):
            guard_note = (f" guard_bypass=ratio:{int(bool(alpha_plan.get('ratio_bypass')))}"
                          f"/move:{int(bool(alpha_plan.get('move_bypass')))}")
        reason = (f"{alpha_plan['reason']} unique={len(unique_buyers)} tracked={effective_tracked_count} "
                  f"buy={buy_sol:.3f} sell={sell_sol:.3f} seen={observed_age_ms/1000:.1f}s"
                  f"{guard_note}")
        log(f"  *** MARKET-TAPE-ALPHA TRIGGER *** {mint[:8]}: {reason}")
        asyncio.create_task(_enter_market_tape_position(
            client, kp, mint, now_ms, reason,
            amount_sol=float(alpha_plan.get("amount") or COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL),
            launchpad="moonshot_ignition",
            quality_score=max(8, int(alpha_plan.get("quality") or 6)),
        ))
        return
    ratio_block_until = _market_tape_ratio_violation_until.get(mint, 0)
    if ratio_block_until > now_ms:
        _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
        _mt_gate("mt_ratio")
        log(f"  MARKET-TAPE BLOCK {mint[:8]}: recent price_ratio violation "
            f"cooldown {(ratio_block_until - now_ms) / 1000:.1f}s")
        return
    if not birth_lane:
        if len(unique_buyers) < MARKET_TAPE_MIN_UNIQUE:
            _mt_gate("mt_no_unique")
            return
        if effective_tracked_count < MARKET_TAPE_MIN_TRACKED:
            _mt_gate("mt_no_tracked")
            return
        if buy_sol < MARKET_TAPE_MIN_BUY_SOL or sell_sol > MARKET_TAPE_MAX_SELL_SOL:
            _mt_gate("mt_flow")
            return
        if (MARKET_TAPE_MAX_OBSERVED_AGE_SEC > 0
                and observed_age_ms > MARKET_TAPE_MAX_OBSERVED_AGE_SEC * 1000):
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_stale")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: stale tape wave "
                f"seen_age={observed_age_ms/1000:.1f}s > {MARKET_TAPE_MAX_OBSERVED_AGE_SEC:.1f}s "
                f"unique={len(unique_buyers)} tracked={effective_tracked_count} buy={buy_sol:.3f}")
            return
    ratio_block_until = _market_tape_ratio_violation_until.get(mint, 0)
    if ratio_block_until > now_ms:
        _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
        _mt_gate("mt_ratio")
        log(f"  MARKET-TAPE BLOCK {mint[:8]}: recent price_ratio violation "
            f"cooldown {(ratio_block_until - now_ms) / 1000:.1f}s")
        return
    move_window_ms = max(
        (MARKET_TAPE_BIRTH_WINDOW_MS if birth_lane else MARKET_TAPE_WINDOW_MS) + 800,
        1000 if birth_lane else 1500,
    )
    bc_move = _bc_cache_move_for_mint(
        mint,
        move_window_ms,
        MARKET_TAPE_BC_CACHE_MAX_AGE_MS,
    )
    if not bc_move:
        _mt_gate("mt_no_bc")
        return
    move_mult, age_ms, complete = bc_move
    if complete:
        _mt_gate("mt_complete")
        return
    min_move = MARKET_TAPE_BIRTH_MIN_BC_MOVE if birth_lane else MARKET_TAPE_MIN_BC_MOVE
    max_move = MARKET_TAPE_BIRTH_MAX_BC_MOVE if birth_lane else MARKET_TAPE_MAX_BC_MOVE
    if move_mult < min_move or move_mult > max_move:
        if birth_lane and move_mult > MARKET_TAPE_BIRTH_MAX_BC_MOVE and move_mult <= MARKET_TAPE_MAX_BC_MOVE:
            log(f"  MARKET-TAPE-BIRTH FALLBACK {mint[:8]}: bc={move_mult:.3f}x above birth band; "
                f"evaluating normal scout wave unique={len(unique_buyers)} tracked={effective_tracked_count} "
                f"buy={buy_sol:.3f}")
            birth_lane = False
            cutoff = now_ms - MARKET_TAPE_WINDOW_MS
            recent = [e for e in tape if e["ts"] >= cutoff]
            buys = [e for e in recent if e["is_buy"]]
            sells = [e for e in recent if not e["is_buy"]]
            unique_buyers = {e["signer"] for e in buys if e["signer"]}
            tracked_buyers = {e["signer"] for e in buys if e.get("tracked")}
            effective_tracked_count = max(len(tracked_buyers), len(pool_swarm_signers))
            buy_sol = sum(e["sol"] for e in buys)
            sell_sol = sum(e["sol"] for e in sells)
            if len(unique_buyers) < MARKET_TAPE_MIN_UNIQUE:
                _mt_gate("mt_no_unique")
                return
            if effective_tracked_count < MARKET_TAPE_MIN_TRACKED:
                _mt_gate("mt_no_tracked")
                return
            if buy_sol < MARKET_TAPE_MIN_BUY_SOL or sell_sol > MARKET_TAPE_MAX_SELL_SOL:
                _mt_gate("mt_flow")
                return
            if (MARKET_TAPE_MAX_OBSERVED_AGE_SEC > 0
                    and observed_age_ms > MARKET_TAPE_MAX_OBSERVED_AGE_SEC * 1000):
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_stale")
                log(f"  MARKET-TAPE BLOCK {mint[:8]}: stale fallback wave "
                    f"seen_age={observed_age_ms/1000:.1f}s > {MARKET_TAPE_MAX_OBSERVED_AGE_SEC:.1f}s "
                    f"unique={len(unique_buyers)} tracked={effective_tracked_count} buy={buy_sol:.3f}")
                return
            bc_move = _bc_cache_move_for_mint(
                mint,
                max(MARKET_TAPE_WINDOW_MS + 800, 1500),
                MARKET_TAPE_BC_CACHE_MAX_AGE_MS,
            )
            if not bc_move:
                _mt_gate("mt_no_bc")
                return
            move_mult, age_ms, complete = bc_move
            if complete:
                _mt_gate("mt_complete")
                return
            min_move = MARKET_TAPE_MIN_BC_MOVE
            max_move = MARKET_TAPE_MAX_BC_MOVE
        if move_mult >= min_move and move_mult <= max_move:
            pass
        else:
            _mt_gate("mt_bc_range")
            if birth_lane:
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                log(f"  MARKET-TAPE-BIRTH BLOCK {mint[:8]}: bc={move_mult:.3f}x outside "
                    f"{MARKET_TAPE_BIRTH_MIN_BC_MOVE:.3f}-{MARKET_TAPE_BIRTH_MAX_BC_MOVE:.3f}x "
                    f"seen_age={observed_age_ms/1000:.1f}s unique={len(unique_buyers)} "
                    f"tracked={effective_tracked_count} buy={buy_sol:.3f}")
            return
    entry_launchpad = "market_tape"
    entry_amount_sol = MARKET_TAPE_AMOUNT_SOL
    entry_quality = 7
    confirm_min_mult = MARKET_TAPE_CONFIRM_MIN_MULT
    if birth_lane:
        entry_launchpad = "market_tape_scout"
        entry_amount_sol = MARKET_TAPE_SCOUT_AMOUNT_SOL
        entry_quality = 6
        confirm_min_mult = MARKET_TAPE_BIRTH_CONFIRM_MIN_MULT
    else:
        scout_ok = (
            MARKET_TAPE_SCOUT_ENABLED
            and MARKET_TAPE_SCOUT_MIN_BC_MOVE <= move_mult < MARKET_TAPE_SCOUT_MAX_BC_MOVE
            and len(unique_buyers) >= MARKET_TAPE_SCOUT_MIN_UNIQUE
            and effective_tracked_count >= MARKET_TAPE_SCOUT_MIN_TRACKED
            and buy_sol >= MARKET_TAPE_SCOUT_MIN_BUY_SOL
        )
        strong_low_move = (
            (len(unique_buyers) >= MARKET_TAPE_LOW_MOVE_MIN_UNIQUE
             and effective_tracked_count >= MARKET_TAPE_LOW_MOVE_MIN_TRACKED)
            or buy_sol >= MARKET_TAPE_LOW_MOVE_MIN_BUY_SOL
        )
        if move_mult < MARKET_TAPE_LOW_MOVE_STRONG_BELOW and not (scout_ok or strong_low_move):
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_weak_low")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: weak low-move setup "
                f"unique={len(unique_buyers)} tracked={effective_tracked_count} "
                f"buy={buy_sol:.3f} bc={move_mult:.3f}x")
            return
        strong_mid_move = (
            (len(unique_buyers) >= MARKET_TAPE_MID_MOVE_MIN_UNIQUE
             and effective_tracked_count >= MARKET_TAPE_MID_MOVE_MIN_TRACKED)
            or buy_sol >= MARKET_TAPE_MID_MOVE_MIN_BUY_SOL
        )
        if move_mult < MARKET_TAPE_MID_MOVE_STRONG_BELOW:
            if scout_ok:
                entry_launchpad = "market_tape_scout"
                entry_amount_sol = MARKET_TAPE_SCOUT_AMOUNT_SOL
                entry_quality = 5
                confirm_min_mult = MARKET_TAPE_SCOUT_CONFIRM_MIN_MULT
            elif strong_mid_move and effective_tracked_count < MARKET_TAPE_MID_MOVE_MIN_TRACKED:
                entry_launchpad = "market_tape_scout"
                entry_amount_sol = MARKET_TAPE_SCOUT_AMOUNT_SOL
                entry_quality = 5
                confirm_min_mult = MARKET_TAPE_SCOUT_CONFIRM_MIN_MULT
            elif not strong_mid_move:
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_weak_mid")
                log(f"  MARKET-TAPE BLOCK {mint[:8]}: weak mid-move setup "
                    f"unique={len(unique_buyers)} tracked={effective_tracked_count} "
                    f"buy={buy_sol:.3f} bc={move_mult:.3f}x")
                return
        moderate_high_scout = (
            move_mult < MARKET_TAPE_HIGH_SCOUT_MAX_BC_MOVE
            and len(unique_buyers) >= MARKET_TAPE_HIGH_SCOUT_MIN_UNIQUE
            and effective_tracked_count >= MARKET_TAPE_HIGH_SCOUT_MIN_TRACKED
            and buy_sol >= MARKET_TAPE_HIGH_SCOUT_MIN_BUY_SOL
        )
        strong_high_move = (
            (len(unique_buyers) >= MARKET_TAPE_HIGH_MOVE_MIN_UNIQUE
             and effective_tracked_count >= MARKET_TAPE_HIGH_MOVE_MIN_TRACKED)
            or buy_sol >= MARKET_TAPE_HIGH_MOVE_MIN_BUY_SOL
            or moderate_high_scout
        )
        if move_mult >= MARKET_TAPE_HIGH_MOVE_STRONG_ABOVE and not strong_high_move:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_weak_high")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: weak high-move chase "
                f"unique={len(unique_buyers)} tracked={effective_tracked_count} "
                f"buy={buy_sol:.3f} bc={move_mult:.3f}x")
            return
        if (move_mult >= MARKET_TAPE_HIGH_MOVE_STRONG_ABOVE
                and (moderate_high_scout
                     or (effective_tracked_count < MARKET_TAPE_HIGH_MOVE_MIN_TRACKED
                         and buy_sol >= MARKET_TAPE_HIGH_MOVE_MIN_BUY_SOL))):
            entry_launchpad = "market_tape_scout"
            entry_amount_sol = MARKET_TAPE_SCOUT_AMOUNT_SOL
            entry_quality = 5
            confirm_min_mult = MARKET_TAPE_SCOUT_CONFIRM_MIN_MULT
    latest_price = _bc_cache_price_for_mint(mint, MARKET_TAPE_BC_CACHE_MAX_AGE_MS)
    if not latest_price:
        _mt_gate("mt_no_price")
        return
    trader_price = float(trade.get("trader_price") or 0)
    if trader_price > 0:
        price_ratio = latest_price[0] / trader_price
        if price_ratio < MARKET_TAPE_MIN_PRICE_RATIO or price_ratio > MARKET_TAPE_MAX_PRICE_RATIO:
            _market_tape_ratio_violation_until[mint] = now_ms + int(MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC * 1000)
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_ratio")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: price_ratio={price_ratio:.3f}x "
                f"outside {MARKET_TAPE_MIN_PRICE_RATIO:.2f}-{MARKET_TAPE_MAX_PRICE_RATIO:.2f}x "
                f"bc={move_mult:.3f}x")
            return
    if _mint_recently_closed(mint, now_ms / 1000.0):
        _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
        _mt_gate("mt_recent_close")
        log(f"  MARKET-TAPE BLOCK {mint[:8]}: closed within "
            f"{RECENT_CLOSE_REENTRY_COOLDOWN_SEC:.0f}s cooldown")
        return
    confirm_delay_sec = MARKET_TAPE_BIRTH_CONFIRM_DELAY_SEC if birth_lane else MARKET_TAPE_CONFIRM_DELAY_SEC
    if confirm_delay_sec > 0:
        trigger_price = latest_price[0]
        await asyncio.sleep(confirm_delay_sec)
        confirm_price = _bc_cache_price_for_mint(mint, MARKET_TAPE_BC_CACHE_MAX_AGE_MS)
        if not confirm_price or confirm_price[1]:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_confirm")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: no fresh confirm price after "
                f"{confirm_delay_sec:.2f}s")
            return
        confirm_mult = confirm_price[0] / trigger_price if trigger_price > 0 else 0.0
        if confirm_mult < confirm_min_mult:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_confirm")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: confirm_mult={confirm_mult:.3f}x "
                f"< {confirm_min_mult:.3f}x after {confirm_delay_sec:.2f}s")
            return
        if trader_price > 0:
            confirm_ratio = confirm_price[0] / trader_price
            if confirm_ratio < MARKET_TAPE_MIN_PRICE_RATIO or confirm_ratio > MARKET_TAPE_MAX_PRICE_RATIO:
                _market_tape_ratio_violation_until[mint] = int(time.time() * 1000) + int(MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC * 1000)
                _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
                _mt_gate("mt_ratio")
                log(f"  MARKET-TAPE BLOCK {mint[:8]}: confirm price_ratio={confirm_ratio:.3f}x "
                    f"outside {MARKET_TAPE_MIN_PRICE_RATIO:.2f}-{MARKET_TAPE_MAX_PRICE_RATIO:.2f}x")
                return
        post_confirm_ms = int(time.time() * 1000)
        ratio_block_until = _market_tape_ratio_violation_until.get(mint, 0)
        if ratio_block_until > post_confirm_ms:
            _copy_trade_stats["market_tape_blocked"] = _copy_trade_stats.get("market_tape_blocked", 0) + 1
            _mt_gate("mt_ratio")
            log(f"  MARKET-TAPE BLOCK {mint[:8]}: concurrent price_ratio violation "
                f"cooldown {(ratio_block_until - post_confirm_ms) / 1000:.1f}s")
            return

    graduated_seen.add(mint)
    if len(graduated_seen) > 500:
        graduated_seen.clear()
        graduated_seen.add(mint)
    _market_tape_entered_recent[mint] = now_ms
    _market_tape_entry_times.append(now_ms)
    _copy_trade_stats["market_tape_triggers"] = _copy_trade_stats.get("market_tape_triggers", 0) + 1
    if birth_lane:
        _copy_trade_stats["market_tape_birth_triggers"] = _copy_trade_stats.get("market_tape_birth_triggers", 0) + 1
    reason = (f"unique={len(unique_buyers)} tracked={effective_tracked_count} "
              f"buy={buy_sol:.3f} sell={sell_sol:.3f} bc={move_mult:.3f}x "
              f"age={age_ms}ms seen={observed_age_ms/1000:.1f}s")
    label = "MARKET-TAPE-BIRTH" if birth_lane else (
        "MARKET-TAPE-SCOUT" if entry_launchpad == "market_tape_scout" else "MARKET-TAPE"
    )
    log(f"  *** {label} TRIGGER *** {mint[:8]}: {reason}")
    asyncio.create_task(_enter_market_tape_position(
        client, kp, mint, now_ms, reason,
        amount_sol=entry_amount_sol,
        launchpad=entry_launchpad,
        quality_score=entry_quality,
    ))


async def _handle_copy_trader_tx(client: Client, kp: Optional[Keypair], sig: str,
                                 trader_set: set, shred_result: Optional[dict] = None):
    """V41.17i: dual-path dispatch. Fast path parses the pump.fun buy ix directly
    from the shred (~5ms), saving 600-800ms over getTransaction. Falls back to
    getTransaction for aggregator-routed buys (Jupiter/Raptor) where there's no
    direct pump.fun ix in the tx, and for non-shred legacy callers (Helius
    logsSubscribe path).

    V41.15d: legacy slow path uses commitment=Confirmed (getTransaction rejects
    Processed; we hit a 100% silent-failure bug there in V41.15c)."""
    _copy_trade_stats["shreds"] += 1
    if sig in _copy_trader_seen_sigs:
        _copy_trade_stats["sig_dedup"] += 1
        return
    _copy_trader_seen_sigs.add(sig)
    if len(_copy_trader_seen_sigs) > 5000:
        _copy_trader_seen_sigs.clear()
    # FAST PATH: parse the shred directly without getTransaction.
    # V41.17y: solders-based base64 parse — median 0.01ms, p99 0.05ms.
    if shred_result is not None:
        fast = _parse_base64_shred_for_pump_buy(shred_result, trader_set)
        if fast == "WRONG_SIGNER":
            # Short-circuit: parsed OK but signer is not one of ours. Slow path
            # would just confirm wrong_signer at 200-400ms cost. Skip it.
            _copy_trade_stats["wrong_signer"] += 1
            return
        if fast is None:
            # Try jsonParsed parser as a second attempt (legacy/compat path)
            fast = _parse_shred_for_pump_buy(shred_result, trader_set)
        if isinstance(fast, dict):
            _copy_trade_stats["fast_path_hit"] = _copy_trade_stats.get("fast_path_hit", 0) + 1
            await _dispatch_copy_signal(
                client, kp, sig, fast["signer"], fast["mint"], fast["trader_price"], "shred",
            )
            return
        if MARKET_TAPE_ENABLED and MARKET_TAPE_ALL_PUMP:
            signer = _base64_shred_fee_payer(shred_result)
            if signer not in trader_set:
                # With market-wide pump.fun scope, most shreds are intentionally
                # not ours. Never let those fall through to getTransaction.
                _copy_trade_stats["wrong_signer"] += 1
                return
    # SLOW PATH: fall back to getTransaction (handles aggregator buys, Helius fallback)
    # V41.17q: wrap in asyncio.to_thread — solana-py's get_transaction is SYNC and was
    # blocking the event loop inside this coroutine. Under load (multiple concurrent
    # shred handlers) the WS reader was backlogged and the server may have been
    # dropping messages we'd otherwise receive. This is the candidate for the "shred
    # volume mysteriously low" symptom.
    try:
        tx = await asyncio.to_thread(
            client.get_transaction,
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
        # V41.17d Fix #11: also capture RAW (smallest-unit) amounts for price math.
        # ui_amount has float-rounding loss for low-decimal tokens; raw `amount` (string)
        # is the exact on-chain value. Need this to compute trader's actual buy price.
        pre_raw = {}     # mint -> int (smallest units)
        post_raw = {}    # mint -> int (smallest units)
        for b in (getattr(meta, "pre_token_balances", None) or []):
            if str(b.owner) == signer:
                m = str(b.mint)
                pre[m] = float(b.ui_token_amount.ui_amount or 0)
                try:
                    pre_raw[m] = int(b.ui_token_amount.amount or 0)
                except Exception:
                    pre_raw[m] = 0
        for b in (getattr(meta, "post_token_balances", None) or []):
            if str(b.owner) == signer:
                m = str(b.mint)
                post[m] = float(b.ui_token_amount.ui_amount or 0)
                try:
                    post_raw[m] = int(b.ui_token_amount.amount or 0)
                except Exception:
                    post_raw[m] = 0
        # V41.17d Fix #11: trader's SOL spend = pre_balance[0] - post_balance[0]
        # (signer is account[0]; balance arrays are lamports). Includes ~5-10k lamports
        # in tx fee + priority fee — negligible vs typical buy size, ignored.
        try:
            sol_spent_lamports = int((meta.pre_balances or [0])[0]) - int((meta.post_balances or [0])[0])
            if sol_spent_lamports < 0:
                sol_spent_lamports = 0
        except Exception:
            sol_spent_lamports = 0
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
                # V41.17w: stripped curve_pct + first_buyer gates (no proven save).
                # Keep only rug check; Fix #11 ratio band runs inside graduation_snipe.
                safe, reason, _snap = await asyncio.to_thread(_rug_check_with_snapshot, mint)
                if not safe:
                    _copy_trade_stats["rug_blocked"] += 1
                    if "fresh bundle" in (reason or ""):
                        _copy_trade_stats["bundle_fresh_blocked"] += 1
                    log(f"  COPY TRADE RUG-BLOCKED {signer[:8]} -> {mint[:8]}: {reason}")
                    return
                # V41.17d Fix #11: compute trader's actual buy price (lamports per
                # smallest-unit token) for the slippage-vs-trader gate. Uses raw
                # amounts (no float rounding) and the signer's full SOL delta (which
                # includes trader's fees — same as the protocol-priced quote we'll
                # compare against, so directly comparable).
                trader_raw_diff = post_raw.get(mint, 0) - pre_raw.get(mint, 0)
                trader_price = 0.0
                if sol_spent_lamports > 0 and trader_raw_diff > 0:
                    trader_price = sol_spent_lamports / trader_raw_diff
                else:
                    _copy_trade_stats["trader_price_unparseable"] += 1
                cached = _bc_cache_price_for_mint(mint, max(MARKET_TAPE_BC_CACHE_MAX_AGE_MS, COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS))
                _alpha_schedule_shadow(
                    mint, signer, "copy_fast",
                    trader_price=trader_price,
                    trigger_price=float(cached[0]) if cached and not cached[1] else 0.0,
                    sig=sig,
                )
                _copy_trade_stats["fired"] += 1
                log(f"*** COPY TRADE *** {signer[:8]} bought {mint} (sig={sig[:16]})"
                    + (f" trader_px={trader_price:.4e}" if trader_price > 0 else " trader_px=unknown"))
                # V41.14: copy_fast skips the 8s observation window. We had ~200ms
                # shred-detection latency advantage; observation was killing the edge.
                # V41.17 Fix #3: pass signer so graduation_snipe can verify smart wallet
                # hasn't already exited (parallelized with probe quote — no added latency).
                # V41.17d Fix #11: pass trader_price for slippage-vs-trader gate.
                asyncio.create_task(graduation_snipe(client, kp, mint, launchpad="copy_fast",
                                                    signer=signer, trader_price=trader_price))
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
    """V41.13g: subscribe to top traders' wallets via Helius. Mirror their buys.
    V41.17c: 5s startup grace prevents boot-time 429 collision with refresh_risk_cache
    on the Data API 1 RPS budget. Without this grace, /top-traders/all/1 races
    /tokens/multi/all and one of the two 429s, costing 60s of trader signal."""
    if not COPY_TRADE_ENABLED:
        log("COPY-TRADE: DISABLED")
        return
    # V41.17c: yield to refresh_risk_cache's first 2 fetches.
    # V41.17e: bumped 5s → 10s after observing the risk cache's first cycle takes
    # ~6s end-to-end (network latency on /tokens/multi/all + 1.2s gap + /tokens/trending/5m
    # + processing 1568 tokens). 5s grace caused the leaderboard fetch to land on top of
    # the second risk-cache fetch, triggering a fresh 429 → 60s retry.
    log("COPY-TRADE: 10s startup grace before leaderboard fetch (avoids Data API collision)")
    await asyncio.sleep(10)
    # V41.17x: ACTIVE SNIPER POOL — replace /top-traders/all (all-time PnL,
    # 77% inactive whales) with currently-active grad snipers aggregated from
    # /top-traders/{mint} across recent graduations. Audit data: median
    # trades/24h was 0 in old pool, 472 in active pool.
    last_refresh = 0.0
    top_traders: list = []
    while True:
        try:
            now = time.time()
            if now - last_refresh > COPY_TRADE_REFRESH_HOURS * 3600 or not top_traders:
                if ACTIVE_SNIPER_POOL_ENABLED:
                    # Fast path: read cached active pool from disk
                    top_traders = _load_active_sniper_pool()
                    if not top_traders:
                        log("COPY-TRADE: no cached active pool, building one (~80s)")
                        top_traders = await asyncio.to_thread(_st_build_active_sniper_pool)
                    if not top_traders:
                        log("COPY-TRADE: active pool empty, falling back to /top-traders/all")
                        # FALLBACK to legacy path
                        data = await asyncio.to_thread(_st_fetch_top_traders, COPY_TRADE_TOP_N)
                        wallets = (data or {}).get("wallets", [])
                        eligible = []
                        for w in wallets:
                            s = w.get("summary", {}) or {}
                            wr = s.get("winPercentage")
                            realized = s.get("realized") or 0
                            if wr is None: continue
                            wr_n = wr / 100.0 if wr > 1.0 else wr
                            floor_wr = max(COPY_TRADE_MIN_WIN_RATE, COPY_TRADE_MIN_WIN_RATE_TIGHT)
                            if wr_n >= floor_wr and realized >= COPY_TRADE_MIN_REALIZED_SOL:
                                eligible.append((w["wallet"], wr_n, realized))
                        eligible.sort(key=lambda x: -x[2])
                        top_traders = [t[0] for t in eligible[:COPY_TRADE_TOP_N]]
                    last_refresh = now
                    log(f"COPY-TRADE: tracking {len(top_traders)} ACTIVE GRAD SNIPERS (V41.17x — pool from /top-traders/{{mint}} aggregation, median 472 trades/24h vs 0 in old pool)")
                else:
                    data = await asyncio.to_thread(_st_fetch_top_traders, COPY_TRADE_TOP_N)
                    if not data:
                        log("COPY-TRADE: leaderboard fetch failed, retrying in 60s")
                        await asyncio.sleep(60)
                        continue
                    wallets = data.get("wallets", []) if isinstance(data, dict) else []
                    eligible = []
                    for w in wallets:
                        s = w.get("summary", {}) or {}
                        wr = s.get("winPercentage")
                        realized = s.get("realized") or 0
                        if wr is None: continue
                        wr_n = wr / 100.0 if wr > 1.0 else wr
                        floor_wr = max(COPY_TRADE_MIN_WIN_RATE, COPY_TRADE_MIN_WIN_RATE_TIGHT)
                        if wr_n >= floor_wr and realized >= COPY_TRADE_MIN_REALIZED_SOL:
                            eligible.append((w["wallet"], wr_n, realized))
                    eligible.sort(key=lambda x: -x[2])
                    top_traders = [t[0] for t in eligible[:COPY_TRADE_TOP_N]]
                    last_refresh = now
                    log(f"COPY-TRADE: tracking {len(top_traders)} top traders (V41.17h baseline)")
                if not top_traders:
                    log("COPY-TRADE: no eligible traders, retrying in 60s")
                    await asyncio.sleep(60)
                    continue
            # V41.14: shredSubscribe via Solana Tracker RPC (50-150ms latency).
            # Falls back to Helius logsSubscribe if ST RPC not configured.
            trader_set = set(top_traders)
            try:
                if ST_RPC_ENABLED:
                    # V41.17x WS hardening: at ~330 shreds/min the getTransaction
                    # backlog starves the WS reader → ST sends ping → we miss it →
                    # 1011 keepalive timeout disconnect. Raise ping_timeout so brief
                    # backlogs don't kill the connection. Also bump max_queue to
                    # absorb burst messages while we drain.
                    async with websockets.connect(
                        ST_RPC_WS,
                        ping_interval=20, ping_timeout=60,
                        max_queue=2048, max_size=8 * 1024 * 1024,
                    ) as ws:
                        # V41.17y: YELLOWSTONE-EQUIVALENT setup — base64 encoding +
                        # accountRequired=[pump_program] for server-side filter.
                        # Test confirmed: 27% of unrelated txs dropped server-side,
                        # local solders parse <1ms vs getTransaction's 200-400ms.
                        # jsonParsed encoding is server-throttled by 99% (test-confirmed
                        # 7.6/s base64 vs 0.1/s jsonParsed) — base64 is the only viable
                        # encoding for high-volume copy-trade.
                        # V41.17z: MULTIPLEX both subscriptions on the SAME WS conn
                        # to stay under ST RPC's 2-conn cap. shredSubscribe = trader
                        # buys; programSubscribe = bonding-curve cache for trend gate.
                        shred_filter = {
                            "accountRequired": [_PUMP_PROGRAM_STR],
                            "vote": False,
                        }
                        if MARKET_TAPE_ENABLED and MARKET_TAPE_ALL_PUMP:
                            shred_filter["accountInclude"] = [_PUMP_PROGRAM_STR]
                        else:
                            shred_filter["accountInclude"] = top_traders
                        shred_sub = {
                            "jsonrpc": "2.0", "id": 9000,
                            "method": "shredSubscribe",
                            "params": [
                                shred_filter,
                                {"encoding": "base64", "transactionDetails": "full",
                                 "maxSupportedTransactionVersion": 0},
                            ],
                        }
                        bc_sub = {
                            "jsonrpc": "2.0", "id": 9001,
                            "method": "programSubscribe",
                            "params": [
                                _PUMP_PROGRAM_STR,
                                {"encoding": "base64", "commitment": "processed",
                                 "filters": [{"memcmp": {"offset": 0, "bytes": _BC_DISC_B58}}]},
                            ],
                        }
                        await ws.send(json.dumps(shred_sub))
                        await ws.send(json.dumps(bc_sub))
                        scope = "ALL pump.fun direct txs" if (MARKET_TAPE_ENABLED and MARKET_TAPE_ALL_PUMP) else f"{len(top_traders)} wallets"
                        log(f"COPY-TRADE: subscribed via ST shredSubscribe for {scope} — "
                            f"V41.19 market tape + V41.17z trend cache (multiplexed on 1 WS)")
                        last_shred_msg = time.time()
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                            except asyncio.TimeoutError:
                                if time.time() - last_shred_msg > COPY_TRADE_WS_IDLE_RECONNECT_SEC:
                                    log(f"COPY-TRADE: no shred messages for "
                                        f"{COPY_TRADE_WS_IDLE_RECONNECT_SEC:.0f}s — reconnecting ST WS")
                                    break
                                continue
                            data = json.loads(raw)
                            method = data.get("method", "")
                            if not method:
                                continue
                            if "shred" in method.lower():
                                last_shred_msg = time.time()
                                res = (data.get("params", {}) or {}).get("result", {})
                                sig = res.get("signature")
                                if not sig:
                                    continue
                                if time.time() - last_refresh > COPY_TRADE_REFRESH_HOURS * 3600:
                                    log("COPY-TRADE: leaderboard refresh due — reconnecting")
                                    break
                                if MARKET_TAPE_ENABLED:
                                    for tr in _parse_base64_shred_for_pump_trades_any(res):
                                        asyncio.create_task(_handle_market_tape_trade(client, kp, sig, tr, trader_set))
                                asyncio.create_task(_handle_copy_trader_tx(client, kp, sig, trader_set, res))
                            elif "program" in method.lower():
                                if time.time() - last_shred_msg > COPY_TRADE_WS_IDLE_RECONNECT_SEC:
                                    log(f"COPY-TRADE: program stream alive but no shreds for "
                                        f"{COPY_TRADE_WS_IDLE_RECONNECT_SEC:.0f}s — reconnecting ST WS")
                                    break
                                # Update bc state cache for trend gate
                                val = (data.get("params") or {}).get("result", {}).get("value", {})
                                pubkey = val.get("pubkey")
                                if not pubkey:
                                    continue
                                acc = val.get("account") or {}
                                acc_data = acc.get("data")
                                if isinstance(acc_data, list):
                                    acc_data = acc_data[0]
                                if not isinstance(acc_data, str):
                                    continue
                                try:
                                    raw_bytes = base64.b64decode(acc_data)
                                    if len(raw_bytes) < 0x18 or raw_bytes[:8] != _BC_DISC_BYTES:
                                        continue
                                    vtoken = struct.unpack_from("<Q", raw_bytes, 0x08)[0]
                                    vsol = struct.unpack_from("<Q", raw_bytes, 0x10)[0]
                                    complete = raw_bytes[48] != 0 if len(raw_bytes) > 48 else False
                                    _bc_state_cache[pubkey].append((int(time.time() * 1000), vsol, vtoken, complete))
                                except Exception:
                                    continue
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


async def wallet_balance_monitor(client: Client, kp: Optional[Keypair]):
    if PAPER_TRADING or not kp:
        return
    while True:
        try:
            balance = await asyncio.to_thread(lambda: client.get_balance(kp.pubkey()).value / 10**9)
            if balance < MIN_WALLET_BALANCE_SOL:
                log(f"FATAL: wallet balance {balance:.4f} SOL < {MIN_WALLET_BALANCE_SOL:.4f} SOL; stopping bot")
                _persist_positions()
                os._exit(0)
        except Exception as e:
            log(f"wallet balance monitor err: {type(e).__name__}: {e}")
        await asyncio.sleep(WALLET_BALANCE_CHECK_SEC)


def _resume_recovered_position_managers(client: Client, kp: Optional[Keypair]) -> None:
    grad_launchpads = {
        "copy_fast", "copy_fast_solo", "copy_fast_alpha", "copy_fast_swarm", "market_tape",
        "market_tape_scout", "pump", "bonk", "bonk_pregrad", "grad_imminent",
        "momentum", "st_pump",
    }
    for pos in list(positions.values()):
        if pos.strategy == "graduation" or pos.launchpad in grad_launchpads:
            asyncio.create_task(manage_graduation_position(client, kp, pos))
        else:
            asyncio.create_task(manage_position(client, kp, pos))


async def main():
    if PAPER_TRADING:
        log("PAPER MODE ON — no real trades will be executed")
    else:
        log("LIVE MODE ON — real swaps enabled")
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
    restored = _load_positions_state()
    _alpha_load_state()
    _reconcile_recovered_positions(client, kp)
    if _daily_loss_limit_hit():
        log(f"FATAL: daily loss limit already hit ({daily_pnl_sol:+.4f} SOL / -{MAX_DAILY_LOSS_SOL:.4f} SOL); not starting")
        return
    log(f"Mode: V40 impulse-tape sniper | core={CORE_AMOUNT_SOL:.4f} scout={SCOUT_AMOUNT_SOL:.4f} scale={SCALE_IN_AMOUNT_SOL:.4f} paper_drag={PAPER_ROUND_TRIP_DRAG_BPS}bps")
    log(f"V40 rules: no micro-noise | TP on current price | scale-in after TP only | account_stream={USE_POSITION_ACCOUNT_STREAM}")
    log(f"Max concurrent: {MAX_CONCURRENT_POSITIONS} | Session loss limit: {MAX_SESSION_LOSS_SOL} SOL | Daily loss limit: {MAX_DAILY_LOSS_SOL} SOL")
    log(f"=== V41.5 ARCHITECTURAL TIGHTENING ===")
    log(f"  Grad gate: +40% to +50% momentum (was +5% to +50%)")
    log(f"  Grad SL: {GRAD_SL_PCT*100:.0f}% (was -12%)")
    log(f"  Grad TP: single-rung +10% sell 100% (no moonbag)")
    log(f"  V40 TP: single-rung +18% sell 100% (no moonbag)")
    log(f"  Cobuy strategy: {'ENABLED' if COBUY_ENABLED else 'DISABLED'} (Xwu6 demoted)")
    log(f"  Holders gate: V40 entries skip if <4 real holders")
    log(f"  Daily trade cap: {MAX_TRADES_PER_DAY}")
    log(f"  Streak pause: {LOSS_STREAK_PAUSE_SEC}s after {LOSS_STREAK_PAUSE_THRESHOLD} real consec losses")
    log(f"  Consec loss counting: pnl <= -{CONSEC_LOSS_COUNT_MIN_SOL:.4f} SOL; "
        f"{MAX_CONSEC_LOSSES} losses triggers {CONSEC_LOSS_HALT_SEC:.0f}s cooldown")
    log(f"  Hard halts: -{MAX_SESSION_LOSS_SOL*1e3:.1f} mSOL session; daily loss still permanent")
    log(f"  Daily loss halt: -{MAX_DAILY_LOSS_SOL:.4f} SOL/24h | live min wallet balance: {MIN_WALLET_BALANCE_SOL:.4f} SOL")
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
    log(f"=== V41.18 CONFIRM-THEN-ENTER ===")
    log(f"  Raw copy_fast is DISABLED as a blind entry; every copy signal must prove follow-through.")
    log(f"  Confirm gate: fresh bc-cache <= {COPY_FAST_CONFIRM_CACHE_MAX_AGE_MS}ms, "
        f"+{(COPY_FAST_CONFIRM_MIN_MULT-1)*100:.1f}% move, cap<={COPY_FAST_CONFIRM_MAX_MULT:.2f}x, "
        f"within {COPY_FAST_CONFIRM_MAX_OFF_PEAK*100:.1f}% of local peak, "
        f"SWARM-{COPY_FAST_CONFIRM_MIN_SWARM} or continued SWARM-3.")
    if COPY_FAST_IGNITION_ENABLED:
        log(f"  Copy-fast ignition: {COPY_FAST_IGNITION_AMOUNT_SOL:.4f}/"
            f"{COPY_FAST_IGNITION_STRONG_AMOUNT_SOL:.4f} SOL when confirm move>="
            f"{COPY_FAST_IGNITION_MIN_MULT:.2f}x with swarm>={COPY_FAST_IGNITION_MIN_SWARM}, "
            f"or >={COPY_FAST_IGNITION_FAST_MULT:.2f}x with swarm>={COPY_FAST_IGNITION_FAST_SWARM}, "
            f"cache<={COPY_FAST_IGNITION_MAX_CACHE_AGE_MS}ms and retain>="
            f"{COPY_FAST_IGNITION_CONFIRM_MIN_MULT:.3f}x/"
            f"{COPY_FAST_IGNITION_CONFIRM_DELAY_SEC:.2f}s; managed as moonshot.")
    if VELOCITY_IGNITION_ENABLED:
        log(f"  Velocity ignition: {VELOCITY_AMOUNT_SOL:.4f}/"
            f"{VELOCITY_STRONG_AMOUNT_SOL:.4f} SOL on rolling {VELOCITY_WINDOW_MS}ms tape "
            f"with {VELOCITY_MIN_UNIQUE}+ unique, tracked>={VELOCITY_MIN_TRACKED} or "
            f"buy>={VELOCITY_STRONG_BUY_SOL:.1f} SOL, move>={VELOCITY_MIN_MOVE_MULT:.3f}x, "
            f"confirm>={VELOCITY_CONFIRM_MIN_MULT:.3f}x/{VELOCITY_CONFIRM_DELAY_SEC:.2f}s, "
            f"off_peak<={VELOCITY_MAX_OFF_PEAK:.1%}; exits fast-kill "
            f"{VELOCITY_FAST_KILL_SEC:.1f}s/{VELOCITY_FAST_KILL_PEAK:.3f}x.")
    if COPY_FAST_CONFIRMED_ENTRY_ENABLED:
        log(f"  Confirmed raw copy_fast size: {COPY_FAST_CONFIRMED_AMOUNT_SOL:.4f} SOL "
            f"(alpha core remains {COPY_FAST_ALPHA_CORE_AMOUNT_SOL:.4f} SOL).")
    else:
        log("  Confirmed raw copy_fast entries: DISABLED; copy signals train alpha and can still enter via alpha/tape.")
    if COPY_FAST_SOLO_ROCKET_ENABLED:
        log(f"  Solo rocket scout: copy_fast may enter {COPY_FAST_SOLO_ROCKET_AMOUNT_SOL:.4f} SOL "
            f"at >={COPY_FAST_SOLO_ROCKET_MIN_MULT:.2f}x without swarm "
            f"({'on' if COPY_FAST_SOLO_ROCKET_ALLOW_SINGLE else 'off'}); "
            f"SWARM-2 lowers trigger to {COPY_FAST_SOLO_ROCKET_SWARM2_MIN_MULT:.2f}x; "
            f"must retain {COPY_FAST_SOLO_ROCKET_CONFIRM_RETAIN:.0%} after "
            f"{COPY_FAST_SOLO_ROCKET_CONFIRM_DELAY_SEC:.2f}s; "
            f"TP={COPY_FAST_SOLO_ROCKET_TP_MULT:.3f}x fast-kill "
            f"{COPY_FAST_SOLO_ROCKET_FAST_KILL_SEC:.1f}s/{COPY_FAST_SOLO_ROCKET_FAST_KILL_PEAK:.3f}x.")
    else:
        log("  Solo rocket scout: DISABLED; copy_fast entries must pass swarm confirm or alpha promotion.")
    if ALPHA_LEARNER_ENABLED:
        log(f"=== V41.20 EXECUTABLE-ALPHA LEARNER ===")
        log(f"  Shadows copy/tape signals for 1/2/5/10s outcomes, persists to {ALPHA_STATE_FILE}.")
        log(f"  Adaptive copy_fast_alpha: scout={COPY_FAST_ALPHA_SCOUT_AMOUNT_SOL:.4f} SOL "
            f"{'spends on exploration' if ALPHA_EXPLORATION_ENABLED else 'exploration is shadow-only'}; "
            f"core={COPY_FAST_ALPHA_CORE_AMOUNT_SOL:.4f} SOL only after "
            f"{COPY_FAST_ALPHA_CORE_MIN_SAMPLES}+ pair samples, "
            f"WR>={COPY_FAST_ALPHA_CORE_MIN_WR:.0%}, "
            f"avg_exit>={COPY_FAST_ALPHA_CORE_MIN_AVG_EXIT_NET:+.1%}; scout requires "
            f"avg_exit>={COPY_FAST_ALPHA_MIN_AVG_EXIT_NET:+.1%}; live mult must be "
            f">={COPY_FAST_ALPHA_MIN_ENTRY_MULT:.3f}x and near peak.")
        log(f"  Context-only market alpha requires {ALPHA_CONTEXT_ONLY_MIN_SAMPLES}+ samples, "
            f"WR>={ALPHA_CONTEXT_ONLY_MIN_WR:.0%}, avg_best>={ALPHA_CONTEXT_ONLY_MIN_AVG_BEST_NET:+.1%}.")
        log(f"  Wallet-only alpha scout requires {ALPHA_WALLET_ONLY_MIN_SAMPLES}+ samples, "
            f"WR>={ALPHA_WALLET_ONLY_MIN_WR:.0%}, "
            f"avg_exit>={ALPHA_WALLET_ONLY_MIN_AVG_EXIT_NET:+.1%}.")
        log(f"  Alpha exits: learned copy/tape alpha uses scout-sized moonshot runner exits, "
            f"selling {ALPHA_RUNNER_TP1_FRACTION*100:.0f}% at {ALPHA_RUNNER_TP1_MULT:.3f}x; "
            f"toxic pairs stop adapting after {ALPHA_BLOCK_MIN_SAMPLES}+ bad samples.")
        if MARKET_TAPE_ALPHA_ENABLED:
            log(f"  Market-tape alpha: context-promoted tape enters scout size before static gates "
                f"when age<={MARKET_TAPE_ALPHA_MAX_AGE_SEC:.1f}s, tracked>={MARKET_TAPE_ALPHA_MIN_TRACKED}, "
                f"sell<={MARKET_TAPE_ALPHA_MAX_SELL_SOL:.3f} SOL, move>={MARKET_TAPE_ALPHA_MIN_MOVE_MULT:.3f}x, "
                f"avg_exit>={MARKET_TAPE_ALPHA_MIN_AVG_EXIT_NET:+.1%}, "
                f"and confirm>={MARKET_TAPE_ALPHA_CONFIRM_MIN_MULT:.3f}x/"
                f"{MARKET_TAPE_ALPHA_CONFIRM_DELAY_SEC:.2f}s; strong avg-exit buckets may retain "
                f"{MARKET_TAPE_ALPHA_RETAIN_CONFIRM_MULT:.3f}x and bypass static ratio/move guards "
                f"after n>={MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_SAMPLES}, "
                f"WR>={MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_WR:.0%}, "
                f"avg_exit>={MARKET_TAPE_ALPHA_BYPASS_GUARDS_MIN_AVG_EXIT:+.1%}.")
    log(f"  Dump kill: skip if price falls below {1.0 + COPY_FAST_CONFIRM_MAX_DUMP:.3f}x during the "
        f"{COPY_FAST_CONFIRM_WINDOW_SEC:.1f}s confirm window.")
    log(f"=== V41.19 MARKET-WIDE TAPE SCALPER ===")
    log(f"  {'ENABLED' if MARKET_TAPE_ENABLED else 'DISABLED'}: parse "
        f"{'ALL pump.fun shreds' if MARKET_TAPE_ALL_PUMP else 'tracked-wallet shreds'} into sub-second per-mint buy/sell tape.")
    log(f"  Entry: {MARKET_TAPE_AMOUNT_SOL:.4f} SOL when {MARKET_TAPE_MIN_UNIQUE}+ unique buyers, "
        f"{MARKET_TAPE_MIN_TRACKED}+ active sniper, buy>={MARKET_TAPE_MIN_BUY_SOL:.3f} SOL, "
        f"sell<={MARKET_TAPE_MAX_SELL_SOL:.3f} SOL in {MARKET_TAPE_WINDOW_MS}ms.")
    log(f"  Curve gate: bc-cache move {MARKET_TAPE_MIN_BC_MOVE:.3f}x-{MARKET_TAPE_MAX_BC_MOVE:.3f}x, "
        f"cache <= {MARKET_TAPE_BC_CACHE_MAX_AGE_MS}ms. TP={MARKET_TAPE_TP_MULT:.3f}x, "
        f"fast-kill {MARKET_TAPE_FAST_KILL_SEC:.1f}s if peak<{MARKET_TAPE_FAST_KILL_PEAK:.3f}x.")
    log(f"  Dump guard: price_ratio {MARKET_TAPE_MIN_PRICE_RATIO:.2f}-{MARKET_TAPE_MAX_PRICE_RATIO:.2f}x; "
        f"bc<{MARKET_TAPE_LOW_MOVE_STRONG_BELOW:.3f}x requires "
        f"({MARKET_TAPE_LOW_MOVE_MIN_UNIQUE}+ unique and {MARKET_TAPE_LOW_MOVE_MIN_TRACKED}+ tracked) "
        f"or {MARKET_TAPE_LOW_MOVE_MIN_BUY_SOL:.1f}+ SOL buy pressure.")
    log(f"  Ratio whipsaw cooldown: {MARKET_TAPE_RATIO_VIOLATION_COOLDOWN_SEC:.1f}s after any "
        f"price_ratio breach.")
    log(f"  Mid-move guard: bc<{MARKET_TAPE_MID_MOVE_STRONG_BELOW:.3f}x requires "
        f"({MARKET_TAPE_MID_MOVE_MIN_UNIQUE}+ unique and {MARKET_TAPE_MID_MOVE_MIN_TRACKED}+ tracked) "
        f"or {MARKET_TAPE_MID_MOVE_MIN_BUY_SOL:.1f}+ SOL buy pressure.")
    log(f"  High-move guard: bc>={MARKET_TAPE_HIGH_MOVE_STRONG_ABOVE:.3f}x requires "
        f"({MARKET_TAPE_HIGH_MOVE_MIN_UNIQUE}+ unique and {MARKET_TAPE_HIGH_MOVE_MIN_TRACKED}+ tracked) "
        f"or {MARKET_TAPE_HIGH_MOVE_MIN_BUY_SOL:.1f}+ SOL buy pressure.")
    log(f"  High scout band: bc<{MARKET_TAPE_HIGH_SCOUT_MAX_BC_MOVE:.3f}x can scout with "
        f"{MARKET_TAPE_HIGH_SCOUT_MIN_UNIQUE}+ unique, {MARKET_TAPE_HIGH_SCOUT_MIN_TRACKED}+ tracked, "
        f"buy>={MARKET_TAPE_HIGH_SCOUT_MIN_BUY_SOL:.1f} SOL.")
    if MARKET_TAPE_EXIT_ENABLED:
        log(f"  Tape exits: {MARKET_TAPE_EXIT_WINDOW_MS}ms sell-pressure window, "
            f"drop<= {MARKET_TAPE_EXIT_DROP_MULT:.3f}x, sell>={MARKET_TAPE_EXIT_MIN_SELL_SOL:.3f} SOL "
            f"or sell/buy>={MARKET_TAPE_EXIT_SELL_BUY_RATIO:.2f}; single tracked sell after "
            f"{MARKET_TAPE_EXIT_SINGLE_TRACKED_SELL_MIN_AGE_SEC:.2f}s.")
    if MARKET_TAPE_SCOUT_ENABLED:
        log(f"  Scout lane: {MARKET_TAPE_SCOUT_AMOUNT_SOL:.4f} SOL at "
            f"{MARKET_TAPE_SCOUT_MIN_BC_MOVE:.3f}-{MARKET_TAPE_SCOUT_MAX_BC_MOVE:.3f}x "
            f"with {MARKET_TAPE_SCOUT_MIN_UNIQUE}+ unique, {MARKET_TAPE_SCOUT_MIN_TRACKED}+ tracked, "
            f"buy>={MARKET_TAPE_SCOUT_MIN_BUY_SOL:.1f} SOL; "
            f"TP={MARKET_TAPE_SCOUT_TP_MULT:.3f}x fast-kill "
            f"{MARKET_TAPE_SCOUT_FAST_KILL_SEC:.1f}s/{MARKET_TAPE_SCOUT_FAST_KILL_PEAK:.3f}x.")
    if MARKET_TAPE_BIRTH_ENABLED:
        log(f"  Birth scout: first {MARKET_TAPE_BIRTH_MAX_AGE_SEC:.1f}s only, "
            f"{MARKET_TAPE_BIRTH_MIN_UNIQUE}+ unique/{MARKET_TAPE_BIRTH_MIN_TRACKED}+ tracked, "
            f"buy>={MARKET_TAPE_BIRTH_MIN_BUY_SOL:.2f} SOL, sell<={MARKET_TAPE_BIRTH_MAX_SELL_SOL:.3f} SOL "
            f"in {MARKET_TAPE_BIRTH_WINDOW_MS}ms, bc={MARKET_TAPE_BIRTH_MIN_BC_MOVE:.3f}-"
            f"{MARKET_TAPE_BIRTH_MAX_BC_MOVE:.3f}x, confirm {MARKET_TAPE_BIRTH_CONFIRM_MIN_MULT:.3f}x/"
            f"{MARKET_TAPE_BIRTH_CONFIRM_DELAY_SEC:.2f}s.")
    if MOONSHOT_IGNITION_ENABLED:
        log(f"  Moonshot ignition: {MOONSHOT_IGNITION_AMOUNT_SOL:.4f}-"
            f"{MOONSHOT_IGNITION_STRONG_AMOUNT_SOL:.4f} SOL when age<={MOONSHOT_MAX_AGE_SEC:.1f}s, "
            f"move>={MOONSHOT_MIN_MOVE_MULT:.3f}x, unique>={MOONSHOT_MIN_UNIQUE}, "
            f"tracked>={MOONSHOT_MIN_TRACKED} or operator-flow buy>={MOONSHOT_UNTRACKED_MIN_BUY_SOL:.1f} SOL; "
            f"confirm {MOONSHOT_CONFIRM_MIN_MULT:.3f}x/{MOONSHOT_CONFIRM_DELAY_SEC:.2f}s.")
        log(f"  Moonshot exits: fast-kill {MOONSHOT_FAST_KILL_SEC:.1f}s/"
            f"{MOONSHOT_FAST_KILL_PEAK:.3f}x, TP1={MOONSHOT_TP1_MULT:.3f}x "
            f"({MOONSHOT_TP1_FRACTION*100:.0f}%), TP2={MOONSHOT_TP2_MULT:.3f}x "
            f"({MOONSHOT_TP2_FRACTION*100:.0f}%), trail={MOONSHOT_TRAIL_DISTANCE:.2f} "
            f"after {MOONSHOT_TRAIL_ACTIVATION:.3f}x.")
    log(f"  Stale tape guard: block non-birth market-tape entries after "
        f"{MARKET_TAPE_MAX_OBSERVED_AGE_SEC:.1f}s from first observed mint trade.")
    if SWARM_SCOUT_ENABLED:
        log(f"  Swarm scout: {SWARM_SCOUT_AMOUNT_SOL:.4f} SOL on SWARM-{SWARM_SCOUT_MIN_SIGNERS}+ "
            f"rug-block clusters if bc={SWARM_SCOUT_MIN_BC_MOVE:.3f}-{SWARM_SCOUT_MAX_BC_MOVE:.3f}x, "
            f"price_ratio sane, and {SWARM_SCOUT_CONFIRM_MIN_MULT:.3f}x continuation after "
            f"{SWARM_SCOUT_CONFIRM_DELAY_SEC:.2f}s.")
    log(f"  Micro-confirm: wait {MARKET_TAPE_CONFIRM_DELAY_SEC:.2f}s and require "
        f"{MARKET_TAPE_CONFIRM_MIN_MULT:.3f}x continuation before entry.")
    log(f"=== V41.17zc/V41.18 DEAD-PEAK + CONFIRM-GATED SWARM ===")
    log(f"  Reverted fixed 3s sustain-wait; V41.18 uses price-confirm polling instead.")
    log(f"  SWARM-3+ rug overrides are candidates only until the confirm gate passes.")
    log(f"  Dead-peak threshold tightened: 1.005 -> 1.020 (catches false-pump-then-crash).")
    log(f"=== V41.17za bypass circuit breakers for swarm-override ===")
    if COPY_FAST_SWARM_ENTRY_ENABLED:
        log(f"  copy_fast_swarm entries skip streak-pause and consec_loss halts.")
    else:
        log(f"  copy_fast_swarm entries DISABLED by default after oversized confirmed-swarm losses; "
            f"swarm still feeds market_tape.")
    log(f"=== V41.17z9/V41.18 SWARM-OVERRIDE CANDIDATE ===")
    log(f"  When SWARM-3+ forms within 30s on a rug-blocked mint, allow confirm-gated override")
    log(f"  If confirmed: 0.025 SOL position (half), 30s hard timeout, dead-peak guard active")
    log(f"=== V41.17z8 SWARM COMPOUND ===")
    log(f"  When SWARM-N (>=2 pool wallets) on a mint we have an open position on,")
    log(f"  ADD to position: 0.5x size for SWARM-2, 1x size for SWARM-3+. Cap 2 adds.")
    log(f"  Only compounds while fresh bc-cache mult >= {SWARM_COMPOUND_MIN_MULT:.3f}x; "
        f"closed mints have {RECENT_CLOSE_REENTRY_COOLDOWN_SEC:.0f}s re-entry cooldown.")
    log(f"  Token-weighted-avg entry (never averages down). 30s age window only.")
    log(f"=== V41.17z7 EXPANDED POOL + SWARM DETECTION ===")
    log(f"  Pool sample: {ACTIVE_SNIPER_GRAD_SAMPLE} mints x top {ACTIVE_SNIPER_TOP_N_PER_GRAD} traders, hits>={ACTIVE_SNIPER_MIN_GRAD_HITS}")
    log(f"  Sources: /tokens/multi/all (graduated+graduating+latest) + /tokens/trending/5m")
    log(f"  Swarm detection: log SWARM-N when N>=2 pool wallets buy same mint within 10s")
    log(f"=== V41.17z5/V41.19 GRADUATION ENTRY ===")
    if PUMP_GRADUATION_ENABLED:
        log(f"  pump/bonk grads: enter immediately after Jupiter indexing (no 5s observe)")
        log(f"  Captures more upside on fast-pumping grads; dead-peak guard handles duds")
    else:
        log(f"  pump/bonk PumpPortal graduation lane disabled after fresh -0.0056 SOL blind-grad loss.")
    log(f"=== V41.17z4 GRADUATION LANE OPENED ===")
    log(f"  Default grad branch: enter on -5% to +50% (was 40-50% in 5s)")
    log(f"  Dead-peak guard now covers: copy_fast + pump + bonk + grad_imminent + momentum + st_pump")
    log(f"  Backtest: 37.5% WR / +4.56% EV at TP+18%/SL-7% with NO momentum gate")
    log(f"=== V41.17z3 WIDER FIX#11 BAND ===")
    log(f"  Fix #11 ratio band: {COPY_FAST_MIN_PRICE_RATIO:.2f}x - {COPY_FAST_MAX_PRICE_RATIO:.2f}x (was 0.85-1.05)")
    log(f"  Dead-peak guard makes wider band safe — borderline duds capped at -3% via 5s exit")
    log(f"=== V41.17z2 DEAD-PEAK GUARD ===")
    log(f"  Exit copy_fast at 5s if peak < 1.005x (curve never moved up = dead)")
    log(f"  Empirical: 100% of losses had peak=1.00x; 100% of wins peaked >=1.05x in <7s")
    log(f"=== V41.17z TREND GATE (skip dumping curves) ===")
    log(f"  programSubscribe to pump.fun BondingCurve → hot trend cache")
    log(f"  Skip copy_fast if curve dumped >{abs(TREND_GATE_5S_MIN)*100:.0f}% in 5s before trader buy")
    log(f"  Fail-OPEN on cache miss (don't reduce frequency on no-data signals)")
    log(f"=== V41.17y YELLOWSTONE-EQUIVALENT (latency parity at $0/mo) ===")
    log(f"  shredSubscribe: accountInclude + accountRequired=[pump_program]")
    log(f"    -> server-side filter drops 27% non-pump.fun txs (test-confirmed)")
    log(f"  Local solders base64 parse: 0.01ms median (vs 250ms getTransaction)")
    log(f"    -> 25,000x faster on direct pump.fun buys (~25% of buy signal)")
    log(f"  Wrong-signer short-circuit: skip slow path on signer mismatch")
    log(f"    -> drops getTransaction calls by ~80%, conserves RPC credits")
    log(f"=== V41.17x ACTIVE-SNIPER POOL ===")
    log(f"  WRONG ASSUMPTION FIXED: /top-traders/all sorts by ALL-TIME PnL")
    log(f"    -> 77% inactive whales, median 0 trades/24h. Audit-confirmed.")
    log(f"  NEW POOL: aggregate /top-traders/{{mint}} across {ACTIVE_SNIPER_GRAD_SAMPLE} recent grads")
    log(f"    -> wallets in >= {ACTIVE_SNIPER_MIN_GRAD_HITS} grad top-10s = active snipers")
    log(f"    -> median trades/24h: 472 (audit confirmed)")
    log(f"    -> refresh every {ACTIVE_SNIPER_REFRESH_HOURS}h (snipers shift)")
    log(f"  STRIPPED (V41.17w): smart-buyer-only, curve_pct_gate, first_buyer_gate, 8s time-stop")
    log(f"  KEPT: rug check + Fix #11 ratio band ({COPY_FAST_MIN_PRICE_RATIO:.2f}-{COPY_FAST_MAX_PRICE_RATIO:.2f}x)")
    log(f"  Fix #1: pre-cached rug check (refresh {RISK_CACHE_REFRESH_SEC}s, TTL {RISK_CACHE_TTL_SEC}s)")
    log(f"  Fix #2: warm /stream/swap pool ({WARM_POOL_SIZE} mints) {'[ACTIVE]' if (WARM_POOL_ENABLED and (kp or not PAPER_TRADING)) else '[paper-skip]'}")
    log(f"  Fix #3: smart-wallet exit pre-flight {'[ACTIVE]' if EXIT_CHECK_ENABLED else '[disabled]'}")
    log(f"  Fix #6: bundle freshness gate (reject if any bundle <{BUNDLE_FRESHNESS_THRESHOLD_SEC}s old)")
    log(f"  Fix #11: bidirectional slippage gate (abort if ratio > {COPY_FAST_MAX_PRICE_RATIO:.2f} or < {COPY_FAST_MIN_PRICE_RATIO:.2f})")
    log(f"=========================================")
    asyncio.create_task(session_reporter())
    if PUMP_GRADUATION_ENABLED:
        asyncio.create_task(pumpportal_migration_listener(client, kp))
    else:
        log("PumpPortal graduation entries: DISABLED by default; V41.19 market_tape is primary speed lane")
    asyncio.create_task(solanatracker_poll_latest(client, kp))
    # V41.17 Fix #1: pre-cache risk for hot-path lookup
    asyncio.create_task(refresh_risk_cache())
    if restored:
        _resume_recovered_position_managers(client, kp)
    if not PAPER_TRADING and kp:
        asyncio.create_task(wallet_balance_monitor(client, kp))
    # V41.17x: rebuild active sniper pool every hour
    asyncio.create_task(active_sniper_refresh_loop())
    # V41.17z: BondingCurve programSubscribe is now MULTIPLEXED on the same WS as
    # shredSubscribe (inside copy_trader_listener). Standalone listener disabled
    # to stay under the ST RPC 2-conn cap.
    # asyncio.create_task(pump_program_bc_listener())
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
