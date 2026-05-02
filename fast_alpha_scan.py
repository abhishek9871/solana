"""
Fast scan — get all active pump.fun buyers, no validation.
Output top 300 by buy count.
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
    print("Fetching 5000 pump.fun sigs (10 batches of 500)...", flush=True)
    all_sigs = []
    last_sig = None
    for batch in range(10):
        if last_sig:
            sigs = c.get_signatures_for_address(PUMP, limit=500,
                                                  before=Signature.from_string(last_sig))
        else:
            sigs = c.get_signatures_for_address(PUMP, limit=500)
        if not sigs.value: break
        all_sigs.extend(sigs.value)
        last_sig = str(sigs.value[-1].signature)
        print(f"  batch {batch+1}/10: total {len(all_sigs)}", flush=True)

    print(f"\nParsing {len(all_sigs)} txs...", flush=True)
    buyer_count = defaultdict(int)
    for i, s in enumerate(all_sigs):
        if i % 250 == 0:
            print(f"  {i}/{len(all_sigs)}, buyers: {len(buyer_count)}", flush=True)
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

    # Filter to wallets with 3+ buys (active enough)
    active = [(w, ct) for w, ct in buyer_count.items() if ct >= 3]
    active.sort(key=lambda x: -x[1])
    print(f"\nFound {len(active)} wallets with 3+ buys")
    print(f"Top 300 most active being saved...")

    keep = active[:300]
    with open("alphas_300_fast.txt", "w") as f:
        f.write("SMART_WALLETS = {\n")
        for i, (w, ct) in enumerate(keep):
            f.write(f'    "{w}": "buys_{ct}",\n')
        f.write("}\n")
    print(f"Wrote {len(keep)} wallets to alphas_300_fast.txt")

    # Also print top 20 to verify
    print()
    for w, ct in keep[:20]:
        print(f"  {w}: {ct} buys")


if __name__ == "__main__":
    main()
