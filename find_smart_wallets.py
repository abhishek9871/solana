"""
Discover smart wallets by analyzing recent pump.fun BUYERS and their hit rate on pumped tokens.
"""
import os
import time
from collections import defaultdict
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature

RPC = "https://mainnet.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
PUMP = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

c = Client(RPC, commitment=Confirmed)


def derive_bc_pda(mint_pk):
    bc_pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP)
    return bc_pda


def get_curve_progress(mint_pk):
    try:
        bc_pda = derive_bc_pda(mint_pk)
        info = c.get_account_info(bc_pda, commitment=Confirmed)
        if not info.value: return None
        data = bytes(info.value.data)
        if len(data) < 49: return None
        real_token = int.from_bytes(data[24:32], "little")
        complete = data[48] != 0
        if complete: return 1.0
        return 1.0 - (real_token / 793_100_000_000_000)
    except: return None


def main():
    print("=" * 60)
    print("SMART WALLET DISCOVERY (via buyer hit-rate on pumps)")
    print("=" * 60)

    # Step 1: Fetch many recent pump.fun signatures
    print("\nStep 1: Fetch 500 recent pump.fun signatures...")
    sigs = c.get_signatures_for_address(PUMP, limit=500, commitment=Confirmed)
    sig_list = sigs.value
    print(f"  Got {len(sig_list)} signatures")

    # Step 2: Parse each — collect (buyer, target_mint) pairs from BUY transactions
    print("\nStep 2: Parse each tx to find buyer + target mint...")
    buyer_mints = defaultdict(set)  # buyer -> set of mints they bought
    parsed = 0
    bought_mints_seen = set()
    for i, s in enumerate(sig_list):
        if i % 50 == 0:
            print(f"  parsing {i}/{len(sig_list)}, buyer/mint pairs found: {sum(len(v) for v in buyer_mints.values())}")
        try:
            tx = c.get_transaction(Signature.from_string(str(s.signature)),
                                      max_supported_transaction_version=0,
                                      commitment=Confirmed)
            if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                continue
            meta = tx.value.transaction.meta
            logs = meta.log_messages or []
            if not any("Instruction: Buy" in l for l in logs):
                continue
            keys = tx.value.transaction.transaction.message.account_keys
            if not keys or len(keys) < 2:
                continue
            buyer = str(keys[0])
            # Find the mint - in post token balances, the buyer should now own a non-SOL token
            post_bals = meta.post_token_balances or []
            target_mint = None
            for bal in post_bals:
                mint_str = str(bal.mint)
                if mint_str == "So11111111111111111111111111111111111111112":
                    continue
                if str(bal.owner) == buyer:
                    if bal.ui_token_amount and float(bal.ui_token_amount.ui_amount or 0) > 0:
                        target_mint = mint_str
                        break
            if target_mint and target_mint.lower().endswith("pump"):
                buyer_mints[buyer].add(target_mint)
                bought_mints_seen.add(target_mint)
                parsed += 1
        except Exception:
            continue
    print(f"  Parsed {parsed} buy transactions across {len(buyer_mints)} buyers, {len(bought_mints_seen)} mints")

    # Step 3: Check each mint's progress
    print(f"\nStep 3: Check progress on {len(bought_mints_seen)} mints...")
    mint_progress = {}
    for i, mint in enumerate(bought_mints_seen):
        if i % 20 == 0:
            print(f"  curve check {i}/{len(bought_mints_seen)}")
        try:
            p = get_curve_progress(Pubkey.from_string(mint))
            if p is not None:
                mint_progress[mint] = p
        except: continue

    # Step 4: Score each buyer = sum of (progress weight) of mints they bought
    # Weight: progress >= 0.5 = strong, progress >= 0.3 = moderate, < 0.3 = weak
    print("\nStep 4: Rank buyers by their hit rate on pumped tokens...")
    buyer_scores = []
    for buyer, mints in buyer_mints.items():
        if len(mints) < 2:
            continue  # need 2+ trades to be statistically meaningful
        score = 0
        wins_50 = 0  # mints reaching 50%+
        wins_30 = 0
        for m in mints:
            p = mint_progress.get(m, 0)
            if p >= 0.50: score += 5; wins_50 += 1
            elif p >= 0.30: score += 2; wins_30 += 1
            elif p >= 0.10: score += 1
        if score >= 3:
            buyer_scores.append((buyer, score, len(mints), wins_50, wins_30))

    buyer_scores.sort(key=lambda x: -x[1])
    print(f"\nFound {len(buyer_scores)} buyers with score >= 3")
    print(f"\n{'BUYER':<46} {'SCORE':>6} {'TX':>4} {'>=50%':>5} {'>=30%':>5}")
    print("-" * 70)
    for buyer, score, tx_ct, w50, w30 in buyer_scores[:25]:
        print(f"{buyer} {score:>6} {tx_ct:>4} {w50:>5} {w30:>5}")

    # Output as Python dict for the bot
    print("\n=== Python dict to paste into solana_sniper.py ===")
    print("SMART_WALLETS = {")
    for buyer, score, tx_ct, w50, w30 in buyer_scores[:20]:
        print(f'    "{buyer}": "score_{score}_tx_{tx_ct}",')
    print("}")


if __name__ == "__main__":
    main()
