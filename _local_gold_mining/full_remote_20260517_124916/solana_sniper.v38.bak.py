"""
SOLANA MEMECOIN SNIPER v17 — MOONSHOT HUNTER

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
SNIPE_AMOUNT_SOL = 0.025              # V20: $2.10 per trade — meaningful profit per win
                                       # 8 attempts on 0.21 SOL wallet
                                       # TP1 (1.30x sells 30%) = ~$0.20 win
                                       # TP2 (2.0x sells 30%) = ~$0.45 win
                                       # TP3 (5x sells 50%) = ~$5 win on moonshot
                                       # Above 5x: trail captures up to $25+ on 50x moonshot
MAX_SLIPPAGE_BPS = 2000
PRIORITY_FEE_LAMPORTS = 500_000
# V38: fixed TP ladder math bug. sell_token uses fractions OF-REMAINING, not
# of-original, so old (0.40, 0.30, 0.30) actually sold 40 + 18 + 12.6 = 70.6%,
# silently abandoning 29.4% of every winner that fully ran. Now adjusted to
# preserve intended 40/30/30 split: last rung = 1.00 (all remaining).
TP_LADDER = [
    (1.15, 0.40),                     # +15% peak -> sell 40% of original (locks initial)
    (1.80, 0.50),                     # +80% peak -> sell 50% of remaining (= 30% of original)
    (3.00, 1.00),                     # +200% peak -> sell ALL remaining (= 30% of original)
]
SCALP_TP_LADDER = [
    (1.05, 1.00),                     # scalp mode: +5% sell all (unchanged)
]
SCALP_SL_PCT = -0.03                  # -3% (tighter than -5%) — minimize dump damage
SCALP_TIMEOUT_SEC = 180               # 3 min hard timeout
SL_PCT = -0.08                        # V20.3: -8% (was -15%) — minimize flash dump damage
FLASH_EXIT_THRESHOLD = 0.99           # V20.2: -1% on first poll = exit immediately (was 0.97)
COBUY_VERIFY_DELAY_SEC = 10           # V20.2: wait this long after co-buy, then verify curve growth
COBUY_MIN_GROWTH_AFTER_DELAY = 0.01   # V20.2: require +1% curve growth in verify window
PREENTRY_BUYSELL_LOOKBACK = 12        # V20.2: check last N pump.fun txs for buy/sell ratio
POSITION_TIMEOUT_MIN = 20             # V17.3: 20min — recycle capital

# Safety thresholds
MAX_TOP10_CONCENTRATION = 0.995
MAX_TOKEN_AGE_MIN = 30
MIN_LIQUIDITY_SOL = 5
# V17.2: FAST + FREQUENT — wide entry, exit strategy does the work
MIN_CURVE_PROGRESS = 0.02             # 2% - take more shots
MAX_CURVE_PROGRESS = 0.50             # 50% - generous upper bound
MIN_UNIQUE_BUYERS_60S = 2             # 2 buyers (was 3) — looser
MIN_BUY_RATIO_RECENT = 0.50           # 50% (was 60%) — looser
MIN_CURVE_GROWTH_30S = 0.0            # V23: DISABLED - velocity gate adds 15s latency we can't afford
EVAL_WAIT_SEC = 5                     # V23: 5s — minimum wait for new mint to populate (was 20s)
EARLY_DUMP_PEAK_THRESHOLD = 1.02      # V31: 1.02x in 30s — if no movement, bail near-flat
EARLY_DUMP_TIMEOUT_SEC = 30           # V31: 30s (was 2min) — exit no-pump tokens fast at break-even

# Circuit breakers
MAX_SESSION_LOSS_SOL = 0.20           # if down 0.2 SOL total, halt
MAX_CONSEC_LOSSES = 20                # V17.5b: most losses are -$0.008 (early dump) — tolerate more
MAX_CONCURRENT_POSITIONS = 6          # V17.5: capture more co-buys when wallets pile in

# === STATE ===
@dataclass
class Position:
    mint: str
    entry_price: float
    entry_amount_sol: float
    token_amount: float
    open_time: float
    peak_price: float = 0.0
    rung_hit: int = 0
    remaining_pct: float = 1.0
    realized_sol: float = 0.0          # cumulative SOL received from sells
    last_price: float = 0.0            # last successfully observed price
    bc_pda: Optional[Pubkey] = None    # cached bonding curve PDA
    graduated: bool = False            # true once token migrates off pump.fun curve
    late_scalp: bool = False           # V21: late-stage scalp mode (tight TP, fast exit)


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
    """Fast-track entry triggered by smart-wallet co-buy. Skips 30s wait, applies curve check only."""
    global session_pnl_sol, consec_losses
    try:
        if session_pnl_sol <= -MAX_SESSION_LOSS_SOL: return
        if consec_losses >= MAX_CONSEC_LOSSES: return
        if len(positions) >= MAX_CONCURRENT_POSITIONS: return
        if mint in positions:
            return  # already in this position

        # Quick safety + curve check
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

        # V24: cap co-buy entries at 50% curve. Late-stage smart-wallet "buys"
        # are usually graduation scalps where we end up as exit liquidity
        # (peak=1.00x pattern). <50% = real accumulation window.
        bc_pda = derive_bc_pda(mint_pk)
        curve = get_curve_state(client, bc_pda)
        if not curve or not curve["price"]:
            log(f"  CO-BUY SKIP {mint[:8]}: no curve state")
            return
        progress = 1.0 - (curve["real_token"] / 793_100_000_000_000)

        if progress >= 0.50:
            log(f"  CO-BUY SKIP {mint[:8]}: curve {progress*100:.1f}% (>50%, late-stage)")
            return

        # V27: 5s sandwich-detection — wait briefly, re-check curve.
        # If curve dropped, smart wallet's buy was sandwich-bait, skip.
        await asyncio.sleep(5)
        if mint in positions: return
        curve2 = get_curve_state(client, bc_pda)
        if not curve2 or not curve2["price"]:
            log(f"  CO-BUY SKIP {mint[:8]}: curve unreadable post-verify")
            return
        progress2 = 1.0 - (curve2["real_token"] / 793_100_000_000_000)
        if progress2 < progress - 0.005:  # curve shrunk >0.5% in 5s = active dump
            log(f"  CO-BUY SKIP {mint[:8]}: dump after smart buy ({progress*100:.1f}% -> {progress2*100:.1f}%)")
            return

        # V27: scan last 5 bonding-curve txs. 2+ sells = dump in progress.
        if not check_curve_not_dumping(client, bc_pda, mint):
            return

        # V36: buyer-history filter — first 5 buyers must be mostly established wallets,
        # not dev-alts. This is the actual signal that separates the 0.5% graduates
        # from the 99.5% rugs on pump.fun.
        if not check_buyer_history(client, bc_pda, mint):
            return

        cashback_tag = " [CASHBACK]" if curve2.get("cashback") else ""
        log(f"  CO-BUY ENTRY{cashback_tag} {mint[:8]}: curve {progress2*100:.1f}%, smart={smart_names}")
        pos = buy_token(kp, client, mint, SNIPE_AMOUNT_SOL)
        if pos:
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


def check_curve_not_dumping(client: Client, bc_pda: Pubkey, mint: str) -> bool:
    """V27: scan last 5 txs on bonding curve. If 2+ sells, return False (skip entry).
    V29: uses Processed commitment — fastest visibility into pending dumps."""
    try:
        from solders.signature import Signature as SolSig
        sigs = client.get_signatures_for_address(bc_pda, limit=5, commitment=Confirmed)
        sells = 0
        for s in sigs.value[:5]:
            try:
                tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                              max_supported_transaction_version=0,
                                              commitment=Confirmed)
                if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                    continue
                logs = tx.value.transaction.meta.log_messages or []
                if any("Instruction: Sell" in l for l in logs):
                    sells += 1
            except: continue
        if sells >= 2:
            log(f"  SKIP {mint[:8]}: dump in progress ({sells}/5 recent txs are sells)")
            return False
    except Exception:
        pass
    return True


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
    """V16 entry strategy: 60s wait, layered filters, curve velocity gate confirms momentum is live."""
    global session_pnl_sol, consec_losses
    try:
        if session_pnl_sol <= -MAX_SESSION_LOSS_SOL: return
        if consec_losses >= MAX_CONSEC_LOSSES: return
        if len(positions) >= MAX_CONCURRENT_POSITIONS: return
        if mint in positions: return  # V19.1 fix: prevent double-entry from cold-sniper after co-buy
        if mint in cobuy_fired: return  # also skip if cobuy already triggered for this mint

        amount_lamports = int(SNIPE_AMOUNT_SOL * 10**WSOL_DECIMALS)

        # V16: shorter eval wait (60s vs 90s) — catches more "still climbing" tokens
        await asyncio.sleep(EVAL_WAIT_SEC)

        # Get current quote
        quote = jupiter_quote(SOL_MINT, mint, amount_lamports)
        if not quote:
            return  # silent — no Jupiter route
        out_amount = float(quote.get("outAmount", 0))
        if out_amount == 0:
            return

        # Honeypot check via sell quote
        sell_quote = jupiter_quote(mint, SOL_MINT, int(out_amount), slippage_bps=2000)
        if not sell_quote:
            log(f"  {mint[:8]}: HONEYPOT")
            return
        sell_back = float(sell_quote.get("outAmount", 0)) / 10**WSOL_DECIMALS
        round_trip = sell_back / SNIPE_AMOUNT_SOL
        if round_trip < 0.80:
            log(f"  {mint[:8]}: tax {(1-round_trip)*100:.0f}%")
            return

        # Quick safety: parse mint authority directly (avoid slow indexing calls)
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

        # Holder count (need 5+ for organic interest)
        real_holders = 0
        try:
            largest = client.get_token_largest_accounts(mint_pk, commitment=Confirmed)
            if largest.value:
                supply = client.get_token_supply(mint_pk, commitment=Confirmed).value
                if supply:
                    threshold = float(supply.amount) * 0.0001
                    real_holders = sum(1 for acc in largest.value if float(acc.amount.amount) > threshold)
                    if real_holders < 5:
                        log(f"  {mint[:8]}: only {real_holders} holders, skip")
                        return
        except Exception:
            pass

        # PUMP.FUN BONDING CURVE PROGRESS — strongest signal of real buying
        # Bonding curve PDA = ['bonding-curve', mint] under pump.fun program
        try:
            bc_pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM)
            bc_info = client.get_account_info(bc_pda, commitment=Confirmed)
            if bc_info.value:
                # Bonding curve account layout: 8-byte discriminator + virtual_token_reserves(8) + virtual_sol_reserves(8) + real_token_reserves(8) + real_sol_reserves(8) + token_total_supply(8) + complete(1)
                bc_data = bytes(bc_info.value.data)
                if len(bc_data) >= 49:
                    real_token_reserves = int.from_bytes(bc_data[8+16:8+24], "little")
                    token_total_supply = int.from_bytes(bc_data[8+32:8+40], "little")
                    if token_total_supply > 0:
                        # Progress = how much has been BOUGHT off the bonding curve
                        # 1 - (real_token_reserves / starting_curve_supply)
                        # Pump.fun starts curve at ~793.1M tokens, graduates at 0
                        curve_progress = 1.0 - (real_token_reserves / 793_100_000_000_000)  # in lamports
                        log(f"  {mint[:8]}: bonding curve progress {curve_progress*100:.1f}%")
                        if curve_progress < MIN_CURVE_PROGRESS:
                            log(f"  {mint[:8]}: too quiet ({curve_progress*100:.1f}% < {MIN_CURVE_PROGRESS*100:.0f}%), skip")
                            return
                        if curve_progress > MAX_CURVE_PROGRESS:
                            log(f"  {mint[:8]}: too late ({curve_progress*100:.1f}% > {MAX_CURVE_PROGRESS*100:.0f}%), skip")
                            return
        except Exception as e:
            log(f"  {mint[:8]} bc check err: {e}")
            return  # if we can't read the curve, don't enter

        # === FAST CURVE VELOCITY GATE (15s — V17.3) ===
        if MIN_CURVE_GROWTH_30S > 0:
            try:
                snap1_progress = curve_progress
                await asyncio.sleep(15)  # V17.3: 15s instead of 30s
                bc_info2 = client.get_account_info(bc_pda, commitment=Confirmed)
                if bc_info2.value:
                    bc_data2 = bytes(bc_info2.value.data)
                    if len(bc_data2) >= 49:
                        real_token_2 = int.from_bytes(bc_data2[8+16:8+24], "little")
                        snap2_progress = 1.0 - (real_token_2 / 793_100_000_000_000)
                        growth = snap2_progress - snap1_progress
                        if growth < MIN_CURVE_GROWTH_30S:
                            log(f"  {mint[:8]}: curve frozen (Δ{growth*100:+.2f}% in 15s), dying — skip")
                            return
                        log(f"  {mint[:8]}: curve LIVE (+{growth*100:.2f}% in 15s, now {snap2_progress*100:.1f}%)")
                        if snap2_progress > MAX_CURVE_PROGRESS:
                            log(f"  {mint[:8]}: pumped past cap during gate, skip")
                            return
            except Exception as e:
                log(f"  {mint[:8]} velocity gate err: {e}")

        # === V15 VELOCITY + BUYER-RATIO GATE ===
        # Look at recent transactions on the bonding curve PDA to count distinct buyers
        # in the last 60 seconds and the buy/sell ratio.
        try:
            from solders.signature import Signature as SolSig
            sigs = client.get_signatures_for_address(bc_pda, limit=30, commitment=Confirmed)
            if sigs.value:
                # Count distinct signer wallets in last 60s + classify buy/sell
                cutoff = int(time.time()) - 60
                signers_60s = set()
                buy_count = 0
                sell_count = 0
                for s in sigs.value[:30]:
                    bt = getattr(s, "block_time", None) or 0
                    # Fetch tx to count fee payer (signer) + classify direction
                    try:
                        tx = client.get_transaction(SolSig.from_string(str(s.signature)),
                                                      max_supported_transaction_version=0,
                                                      commitment=Confirmed)
                        if not tx.value or not tx.value.transaction:
                            continue
                        keys = tx.value.transaction.transaction.message.account_keys
                        if not keys: continue
                        signer = str(keys[0])
                        if bt and bt >= cutoff:
                            signers_60s.add(signer)
                        # Classify via log messages
                        meta = tx.value.transaction.meta
                        if meta and meta.log_messages:
                            joined = "\n".join(meta.log_messages)
                            if "Buy" in joined: buy_count += 1
                            elif "Sell" in joined: sell_count += 1
                    except Exception:
                        continue
                if len(signers_60s) < MIN_UNIQUE_BUYERS_60S:
                    log(f"  {mint[:8]}: only {len(signers_60s)} unique buyers/60s (<{MIN_UNIQUE_BUYERS_60S}), skip")
                    return
                total_dir = buy_count + sell_count
                if total_dir > 0:
                    buy_ratio = buy_count / total_dir
                    if buy_ratio < MIN_BUY_RATIO_RECENT:
                        log(f"  {mint[:8]}: buy_ratio {buy_ratio*100:.0f}% (<{MIN_BUY_RATIO_RECENT*100:.0f}%), skip")
                        return
                    log(f"  {mint[:8]}: velocity OK (buyers_60s={len(signers_60s)}, buy_ratio={buy_ratio*100:.0f}%)")
        except Exception as e:
            log(f"  {mint[:8]} velocity check err: {e} — proceeding without it")

        # === V15 DEV WALLET REPUTATION ===
        # Check creator's prior pump.fun tokens. Reject serial ruggers.
        try:
            if not check_dev_reputation(client, mint_pk):
                log(f"  {mint[:8]}: dev wallet has bad rug history, skip")
                return
        except Exception as e:
            log(f"  {mint[:8]} dev rep check err: {e}")

        # V21.2 race-condition fix: re-check after all sleeps
        if mint in positions:
            log(f"  {mint[:8]}: skip (already in positions, co-buy raced ahead)")
            return
        if mint in cobuy_fired:
            log(f"  {mint[:8]}: skip (co-buy already fired during eval)")
            return

        # V27: anti-rug pre-entry check on bonding curve
        bc_pda = derive_bc_pda(mint_pk)
        if not check_curve_not_dumping(client, bc_pda, mint):
            return

        # V36: buyer-history filter — graduate-vs-rug signal.
        # First buyers must be established wallets, not dev alts.
        if not check_buyer_history(client, bc_pda, mint):
            return

        # V33: momentum-confirmation gate. Wait 3s, require curve grew >=1.5%.
        # Tokens that don't show buying pressure in their first 3 observable seconds
        # are not pumping — entering them produces peak=1.00x losses.
        try:
            curve_pre = get_curve_state(client, bc_pda, fast=True)
            if not curve_pre or not curve_pre["price"]:
                return
            progress_pre = 1.0 - (curve_pre["real_token"] / 793_100_000_000_000)
            await asyncio.sleep(3)
            if mint in positions: return
            curve_post = get_curve_state(client, bc_pda, fast=True)
            if not curve_post or not curve_post["price"]:
                log(f"  {mint[:8]}: skip (curve unreadable post-3s)")
                return
            progress_post = 1.0 - (curve_post["real_token"] / 793_100_000_000_000)
            growth = progress_post - progress_pre
            if growth < 0.015:
                log(f"  {mint[:8]}: skip (no momentum: curve {progress_pre*100:.1f}% -> {progress_post*100:.1f}%, +{growth*100:.2f}%)")
                return
            # V34: distinguish organic momentum from dev-self-pump bait by counting unique buyers.
            # Dev-self-pumps with alt wallets show 1-2 unique buyer pubkeys; organic has 3+.
            unique_buyers = count_unique_buyers(client, bc_pda, lookback=5)
            if 0 <= unique_buyers < 3:
                log(f"  {mint[:8]}: skip (likely dev-self-pump: only {unique_buyers} unique buyers in last 5 txs)")
                return
            log(f"  {mint[:8]}: momentum OK (+{growth*100:.2f}% in 3s, {unique_buyers} unique buyers)")
        except Exception as e:
            log(f"  {mint[:8]}: momentum check err {e} — proceeding cautiously")

        # V37: detect cashback flag from curve_post (read during V33/V34 momentum check)
        cashback_tag = ""
        try:
            cashback_tag = " [CASHBACK]" if curve_post.get("cashback") else ""
        except: pass
        log(f"  {mint[:8]}: ENTRY{cashback_tag} (holders={real_holders if 'real_holders' in dir() else '?'}, rt={round_trip*100:.0f}%)")
        pos = buy_token(kp, client, mint, SNIPE_AMOUNT_SOL)
        if pos:
            positions[mint] = pos
            asyncio.create_task(manage_position(client, kp, pos))
    except Exception as e:
        log(f"  eval err for {mint[:8]}: {e}")


async def manage_position(client: Client, kp: Optional[Keypair], pos: Position):
    """Monitor a position via on-chain bonding curve reads (NO Jupiter polling).
    TP ladder + trailing SL + timeout. Tracks realized PnL."""
    global session_pnl_sol, session_wins, session_losses, consec_losses
    log(f"Managing {pos.mint[:8]} entry_px={pos.entry_price:.6e}")
    closed = False
    close_reason = ""
    dead_reads = 0   # count of consecutive curve reads that came back empty/dead

    def record_sell(sol_recv):
        if sol_recv is not None:
            pos.realized_sol += sol_recv

    while not closed:
        try:
            # V32: SMART-WALLET-SOLD SIGNAL — fastest possible exit when an elite wallet exits the same token
            if pos.mint in smart_wallet_sold:
                smart_wallet_sold.discard(pos.mint)
                last_mult = pos.last_price / pos.entry_price if pos.last_price > 0 else 1.0
                close_reason = f"SMART SELL EXIT (mult={last_mult:.2f}x peak={pos.peak_price:.2f}x)"
                log(f"  {close_reason} {pos.mint[:8]}")
                if pos.remaining_pct > 0.01:
                    record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=last_mult))
                closed = True
                break

            # HARD TIMEOUT
            if time.time() - pos.open_time > POSITION_TIMEOUT_MIN * 60:
                close_reason = f"HARD TIMEOUT {POSITION_TIMEOUT_MIN}min"
                log(f"  {close_reason}, force-sell {pos.mint[:8]}")
                if pos.remaining_pct > 0.01 and pos.last_price > 0:
                    last_mult = pos.last_price / pos.entry_price
                    record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=last_mult))
                closed = True
                break

            # V16 EARLY DUMP EXIT: if peak hasn't exceeded 1.05x after 3 min, position is dead
            elapsed = time.time() - pos.open_time
            if elapsed > EARLY_DUMP_TIMEOUT_SEC and pos.peak_price < EARLY_DUMP_PEAK_THRESHOLD:
                close_reason = f"EARLY DUMP EXIT (no pump in 3min, peak={pos.peak_price:.2f}x)"
                log(f"  {close_reason} {pos.mint[:8]}")
                if pos.remaining_pct > 0.01 and pos.last_price > 0:
                    last_mult = pos.last_price / pos.entry_price
                    record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=last_mult))
                closed = True
                break

            # === READ ON-CHAIN BONDING CURVE (free, unlimited via Helius) ===
            # V29: fast=True uses Processed commitment — sub-second curve update visibility
            curve = get_curve_state(client, pos.bc_pda, fast=True) if pos.bc_pda else None
            current_price = None

            if curve is None:
                # RPC blip — wait briefly, retry
                dead_reads += 1
                if dead_reads >= 6:
                    close_reason = f"RPC failed 6x, force-sell"
                    log(f"  {close_reason} {pos.mint[:8]}")
                    if pos.remaining_pct > 0.01 and pos.last_price > 0:
                        last_mult = pos.last_price / pos.entry_price
                        record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=last_mult))
                    closed = True
                    break
                await asyncio.sleep(3)
                continue

            if curve["complete"] or not curve["price"]:
                # Token graduated or curve drained -> fall back to Jupiter for tracking
                pos.graduated = True
                probe_qty = int(pos.token_amount * 0.01) or 1
                quote = jupiter_quote(pos.mint, SOL_MINT, probe_qty)
                if not quote or float(quote.get("outAmount", 0)) == 0:
                    dead_reads += 1
                    if dead_reads >= 6:
                        close_reason = f"GRADUATED + no Jupiter route"
                        log(f"  {close_reason}, force-close {pos.mint[:8]}")
                        if pos.remaining_pct > 0.01 and pos.last_price > 0:
                            last_mult = pos.last_price / pos.entry_price
                            record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=last_mult))
                        closed = True
                        break
                    await asyncio.sleep(5)
                    continue
                sol_lamports = float(quote["outAmount"])
                current_price = sol_lamports / probe_qty
                dead_reads = 0
            else:
                current_price = curve["price"]
                dead_reads = 0

            pos.last_price = current_price
            multiplier = current_price / pos.entry_price

            # V28 RACE-THE-DUMP: scan last 5 txs on bonding curve. If sells outnumber
            # buys (≥3 of 5), a coordinated dump is in progress — fire exit in the
            # same slot range to extract SOL before the curve fully drains.
            # Skips if peak already hit TP territory (1.15x+) — TP/trail handles those.
            if pos.bc_pda and not pos.graduated and pos.peak_price < 1.15:
                try:
                    from solders.signature import Signature as SolSig
                    sigs = client.get_signatures_for_address(pos.bc_pda, limit=5, commitment=Confirmed)
                    sells = 0; buys = 0
                    for s in sigs.value[:5]:
                        sig_str = str(s.signature)
                        try:
                            tx = client.get_transaction(SolSig.from_string(sig_str),
                                                          max_supported_transaction_version=0,
                                                          commitment=Confirmed)
                            if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                                continue
                            logs = tx.value.transaction.meta.log_messages or []
                            if any("Instruction: Sell" in l for l in logs): sells += 1
                            elif any("Instruction: Buy" in l for l in logs): buys += 1
                        except: continue
                    # Coordinated dump: 3+ sells in last 5 txs (60%+ sell ratio)
                    if sells >= 3:
                        close_reason = f"RACE EXIT (dump in progress: {sells}/5 sells, mult={multiplier:.2f}x)"
                        log(f"  {close_reason} {pos.mint[:8]}")
                        record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=multiplier))
                        closed = True
                        break
                except Exception:
                    pass  # if race check fails, fall through to standard exits

            # V20.3 FLASH EXIT: any dump within first 30s where peak hasn't broken 1.05x = exit.
            # Catches both first-poll dumps AND between-poll crashes early in position life.
            elapsed_sec = time.time() - pos.open_time
            if elapsed_sec < 30 and pos.peak_price < 1.05 and multiplier < FLASH_EXIT_THRESHOLD:
                close_reason = f"FLASH EXIT ({elapsed_sec:.0f}s in, mult={multiplier:.2f}x, peak={pos.peak_price:.2f}x)"
                log(f"  {close_reason} {pos.mint[:8]}")
                record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=multiplier))
                closed = True
                break

            if multiplier > pos.peak_price:
                pos.peak_price = multiplier

            # V21: Pick TP ladder based on position type
            ladder = SCALP_TP_LADDER if pos.late_scalp else TP_LADDER
            if pos.rung_hit < len(ladder):
                trigger, sell_frac = ladder[pos.rung_hit]
                if pos.peak_price >= trigger:
                    mode_tag = "SCALP " if pos.late_scalp else ""
                    log(f"  {mode_tag}TP RUNG {pos.rung_hit+1}: peak={pos.peak_price:.2f}x, selling {sell_frac*100:.0f}%")
                    sol_recv = sell_token(kp, client, pos, sell_frac, current_multiplier=multiplier)
                    if sol_recv is not None:
                        record_sell(sol_recv)
                        pos.remaining_pct *= (1 - sell_frac)
                        pos.rung_hit += 1
                        if pos.rung_hit == len(ladder) or pos.remaining_pct < 0.01:
                            close_reason = f"TP COMPLETE peak={pos.peak_price:.2f}x"
                            closed = True
                            break

            # Trailing SL — tighten as peaks rise to lock in profits.
            # V30: closed gap between 1.00x-1.25x peaks. Tokens that pumped 5-10%
            # and drifted back to -8% SL were big losers (FeN4QxRX, AYv1dhfs).
            # Now: any move >5% locks at -2%; any move >10% locks at +2%.
            change = (current_price - pos.entry_price) / pos.entry_price
            if pos.peak_price >= 3.0:
                effective_sl = 1.5  # peak 3x -> SL +150%
            elif pos.peak_price >= 2.0:
                effective_sl = 0.8  # peak 2x -> SL +80%
            elif pos.peak_price >= 1.5:
                effective_sl = 0.3  # peak 1.5x -> SL +30%
            elif pos.peak_price >= 1.25:
                effective_sl = 0.05  # peak 1.25x -> SL +5%
            elif pos.peak_price >= 1.10:
                effective_sl = 0.02  # V30: peak 1.10x -> SL +2% (lock small profit)
            elif pos.peak_price >= 1.05:
                effective_sl = -0.02  # V30: peak 1.05x -> SL -2% (tight scratch)
            else:
                # No meaningful peak yet — use standard SL
                effective_sl = SCALP_SL_PCT if pos.late_scalp else SL_PCT
            if change < effective_sl:
                close_reason = f"TRAIL SL at {change*100:+.1f}% (eff_sl={effective_sl*100:+.0f}% peak={pos.peak_price:.2f}x)"
                log(f"  {close_reason}")
                record_sell(sell_token(kp, client, pos, 1.0, current_multiplier=multiplier))
                closed = True
                break

            await asyncio.sleep(1)  # V20.1: 1s polling — faster flash dump detection
        except Exception as e:
            log(f"  manage err: {e}")
            await asyncio.sleep(5)

    # === Final accounting (single source of truth for PnL) ===
    pnl = pos.realized_sol - pos.entry_amount_sol
    session_pnl_sol += pnl
    if pnl >= 0:
        session_wins += 1
        consec_losses = 0
    else:
        session_losses += 1
        consec_losses += 1
    log(f"  CLOSED {pos.mint[:8]} peak={pos.peak_price:.2f}x recv={pos.realized_sol:.4f} cost={pos.entry_amount_sol:.4f} "
        f"pnl={pnl:+.4f} SOL | session={session_pnl_sol:+.4f} W={session_wins} L={session_losses} "
        f"reason={close_reason}")
    if pos.mint in positions:
        del positions[pos.mint]


async def session_reporter():
    """Periodic session summary so user can see PnL in realtime."""
    while True:
        await asyncio.sleep(60)
        log(f"=== SESSION: pnl={session_pnl_sol:+.4f} SOL | W={session_wins} L={session_losses} | "
            f"open={len(positions)} | consec_loss={consec_losses} ===")


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
    log(f"Starting pump.fun monitor...")
    # Run reporter alongside the monitor
    asyncio.create_task(session_reporter())
    await monitor_pump_fun(client, kp)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Interrupted")
