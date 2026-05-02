"""
Quick wallet pool expansion — grab ALL recent pump.fun buyers (last 1500 sigs).
Output Python dict to paste into solana_sniper.py.
"""
import os
from collections import defaultdict
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature

c = Client("https://mainnet.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb",
            commitment=Confirmed)
PUMP = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")


def main():
    print("Fetching 1500 recent pump.fun signatures (3 batches of 500)...")
    all_sigs = []
    last_sig = None
    for batch in range(3):
        opts = {"limit": 500}
        if last_sig:
            sigs = c.get_signatures_for_address(PUMP, limit=500, before=Signature.from_string(last_sig))
        else:
            sigs = c.get_signatures_for_address(PUMP, limit=500)
        if not sigs.value:
            break
        all_sigs.extend(sigs.value)
        last_sig = str(sigs.value[-1].signature)
        print(f"  batch {batch+1}: {len(sigs.value)} sigs, total {len(all_sigs)}")
    print()
    print(f"Parsing {len(all_sigs)} transactions for buyers...")

    buyer_count = defaultdict(int)
    for i, s in enumerate(all_sigs):
        if i % 100 == 0:
            print(f"  {i}/{len(all_sigs)} parsed, unique buyers so far: {len(buyer_count)}")
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
            if keys:
                buyer_count[str(keys[0])] += 1
        except: continue

    # Wallets with 2+ buys = active enough to track
    active_wallets = {w: ct for w, ct in buyer_count.items() if ct >= 2}
    print()
    print(f"=== Found {len(buyer_count)} unique buyers, {len(active_wallets)} with 2+ buys ===")
    print()

    # Output sorted by frequency
    ranked = sorted(active_wallets.items(), key=lambda x: -x[1])
    print("SMART_WALLETS = {")
    for wallet, ct in ranked[:200]:
        print(f'    "{wallet}": "active_{ct}",')
    print("}")
    print()
    print(f"Total wallets in dict: {min(200, len(ranked))}")


if __name__ == "__main__":
    main()
