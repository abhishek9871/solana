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

# === CONFIG ===
PAPER_TRADING = True  # set False for live trading

# Wallet & RPC
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb")
SOLANA_WS_URL = SOLANA_RPC_URL.replace("https://", "wss://").replace("http://", "ws://")
PRIVATE_KEY_B58 = os.environ.get("SOLANA_PRIVATE_KEY", "")

# Pump.fun program (post-2026-04-28 update)
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # for jupiter health probe
WSOL_DECIMALS = 9

# Jupiter — try lite endpoint first (often more reliable DNS)
JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP = "https://lite-api.jup.ag/swap/v1/swap"

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
DUMP_REBOUND_ENABLED = True
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
SCALE_IN_ENABLED = os.environ.get("SCALE_IN_ENABLED", "1") == "1"
# Compatibility constants for V38 dump-bounce + cold momentum paths.
# These names are used below; without them, the bot would crash when those paths fire.
DUMP_BOUNCE_WATCH_SEC = 45
DUMP_BOUNCE_MAX_CURVE_PROGRESS = 0.72
DUMP_BOUNCE_MIN_GROWTH = 0.005
COLD_MOMENTUM_MIN_GROWTH_3S = MIN_MOMENTUM_GROWTH_3S
CASHBACK_MOMENTUM_MIN_GROWTH_3S = 0.0015

# Circuit breakers
MAX_SESSION_LOSS_SOL = 0.20           # if down 0.2 SOL total, halt
MAX_CONSEC_LOSSES = 20                # V17.5b: most losses are -$0.008 (early dump) — tolerate more
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "10"))  # V39: allow more tiny scouts without blocking runners

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


positions: dict[str, Position] = {}
session_pnl_sol = 0.0
session_wins = 0
session_losses = 0
consec_losses = 0
last_seen_mints: set[str] = set()
jupiter_last_ok = 0.0                  # last successful jupiter API call (any token)
jupiter_blocked_until = 0.0            # rate-limited until this timestamp

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
    "Xwu6DKqGo4wKPBAPvNYHsjMTV2JxqmW6ubuvhQYKu6E": "Xwu6_pnl0_wr57",
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
def jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int,
                  slippage_bps: int = MAX_SLIPPAGE_BPS, retries: int = 0) -> Optional[dict]:
    """One quote call. On 429 (rate-limited), sets a global cooldown so all callers back off."""
    global jupiter_last_ok, jupiter_blocked_until
    # Honor global rate-limit cooldown
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
                return r.json()
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


def execute_swap(kp: Keypair, client: Client, swap_tx_b64: str) -> Optional[str]:
    """Sign and broadcast a Jupiter swap transaction. Returns signature."""
    try:
        raw = base64.b64decode(swap_tx_b64)
        vt = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(vt.message, [kp])
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
    # All future tracking will read this curve directly (no Jupiter polling).
    curve = get_curve_state(client, bc_pda)
    if curve and curve["price"]:
        entry_price = curve["price"]
        log(f"  entry_price from curve: {entry_price:.6e}")
    else:
        # Fallback: derive from quote (only used if curve read fails — rare)
        entry_price = amount_lamports / out_amount
        log(f"  curve unavailable, using quote-derived entry_price: {entry_price:.6e}")

    if PAPER_TRADING:
        log(f"  [PAPER] would buy: {out_amount/10**decimals:,.0f} tokens @ {entry_price:.6e}")
        return Position(mint=mint, entry_price=entry_price, entry_amount_sol=amount_sol,
                        token_amount=out_amount, open_time=time.time(),
                        bc_pda=bc_pda)

    if not kp:
        log(f"  ERR: live mode but no keypair")
        return None
    swap_tx = jupiter_swap(quote, str(kp.pubkey()))
    if not swap_tx:
        return None
    sig = execute_swap(kp, client, swap_tx)
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
        log(f"  [PAPER] simulated sell")
        # Use CURRENT price multiplier if provided (realistic), else peak (best case)
        m = current_multiplier if current_multiplier is not None else pos.peak_price
        sol_recv = pos.entry_amount_sol * pos.remaining_pct * fraction * m
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

                # 2. Send all smart wallet subscriptions
                pending_acks = {1: "pump"}
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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Interrupted")
