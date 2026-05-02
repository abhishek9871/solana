"""
MASSIVE alpha discovery — scan deep, validate broadly, keep top 300.

Pipeline:
  1. Scan 5000 recent pump.fun signatures (10 batches of 500)
  2. Extract all unique buyers
  3. Filter to those with >= 2 buys (likely active, not noise)
  4. Validate each with last 30 of their txs (PnL, win rate, hold time)
  5. Keep top 300 ranked by total PnL where:
     - PnL > 0
     - Win rate >= 50%
     - Avg hold >= 30s (excludes MEV bots)
     - Trades >= 3

Output: Python dict ready to paste into solana_sniper.py SMART_WALLETS
"""
import os
import time
from collections import defaultdict
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey
from solders.signature import Signature

c = Client("https://mainnet.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb",
            commitment=Confirmed)
PUMP = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
SOL_MINT = "So11111111111111111111111111111111111111112"


def scan_buyers(target_sigs=5000):
    """Scan recent pump.fun signatures, build buyer -> count dict."""
    print(f"Scanning {target_sigs} recent pump.fun signatures...")
    all_sigs = []
    last_sig = None
    while len(all_sigs) < target_sigs:
        try:
            if last_sig:
                sigs = c.get_signatures_for_address(PUMP, limit=500,
                                                      before=Signature.from_string(last_sig))
            else:
                sigs = c.get_signatures_for_address(PUMP, limit=500)
            if not sigs.value:
                break
            all_sigs.extend(sigs.value)
            last_sig = str(sigs.value[-1].signature)
            print(f"  {len(all_sigs)}/{target_sigs} fetched", flush=True)
        except Exception as e:
            print(f"  scan err: {e}")
            break
    print(f"\nParsing {len(all_sigs)} txs to find buyers...")
    buyer_count = defaultdict(int)
    for i, s in enumerate(all_sigs):
        if i % 200 == 0:
            print(f"  parsing {i}/{len(all_sigs)}, unique buyers: {len(buyer_count)}", flush=True)
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
    return buyer_count


def validate_wallet_fast(wallet, max_tx=20):
    """Lightweight validation — last 20 txs, find buy/sell pairs, compute PnL."""
    try:
        sigs = c.get_signatures_for_address(Pubkey.from_string(wallet), limit=max_tx,
                                              commitment=Confirmed)
        actions = defaultdict(list)
        for s in sigs.value[:max_tx]:
            try:
                tx = c.get_transaction(Signature.from_string(str(s.signature)),
                                          max_supported_transaction_version=0,
                                          commitment=Confirmed)
                if not tx.value or not tx.value.transaction or not tx.value.transaction.meta:
                    continue
                meta = tx.value.transaction.meta
                logs = meta.log_messages or []
                is_buy = any("Instruction: Buy" in l for l in logs)
                is_sell = any("Instruction: Sell" in l for l in logs)
                if not (is_buy or is_sell): continue
                bt = getattr(s, "block_time", None) or 0
                # find target mint
                pre_bals = {f"{b.account_index}:{str(b.mint)}":
                              float(b.ui_token_amount.ui_amount or 0) for b in (meta.pre_token_balances or [])}
                post_bals = {f"{b.account_index}:{str(b.mint)}":
                               float(b.ui_token_amount.ui_amount or 0) for b in (meta.post_token_balances or [])}
                target_mint = None
                token_delta = 0
                for k, post_amt in post_bals.items():
                    idx, mint = k.split(":", 1)
                    if mint == SOL_MINT or not mint.lower().endswith("pump"): continue
                    pre_amt = pre_bals.get(k, 0)
                    delta = post_amt - pre_amt
                    if abs(delta) > 0:
                        target_mint = mint
                        token_delta = delta
                        break
                if target_mint is None:
                    for k, pre_amt in pre_bals.items():
                        idx, mint = k.split(":", 1)
                        if mint == SOL_MINT or not mint.lower().endswith("pump"): continue
                        post_amt = post_bals.get(k, 0)
                        delta = post_amt - pre_amt
                        if delta < 0:
                            target_mint = mint
                            token_delta = delta
                            break
                if not target_mint: continue
                pre_sol = (meta.pre_balances[0] if meta.pre_balances else 0) / 1e9
                post_sol = (meta.post_balances[0] if meta.post_balances else 0) / 1e9
                sol_delta = post_sol - pre_sol
                actions[target_mint].append({
                    "ts": bt, "is_buy": is_buy, "is_sell": is_sell,
                    "token_delta": token_delta, "sol_delta": sol_delta,
                })
            except: continue

        total_pnl = 0
        hold_times = []
        wins = 0; losses = 0
        for mint, acts in actions.items():
            acts.sort(key=lambda a: a["ts"] or 0)
            buy = None
            for a in acts:
                if a["is_buy"]:
                    buy = a
                elif a["is_sell"] and buy:
                    cost = -buy["sol_delta"]
                    recv = a["sol_delta"]
                    pnl = recv - cost
                    total_pnl += pnl
                    hold_t = (a["ts"] or 0) - (buy["ts"] or 0)
                    hold_times.append(hold_t)
                    if pnl > 0: wins += 1
                    else: losses += 1
                    buy = None
        if not hold_times:
            return None
        return {
            "wins": wins, "losses": losses,
            "total_pnl": total_pnl,
            "avg_hold": sum(hold_times) / len(hold_times),
            "trades": len(hold_times),
        }
    except Exception:
        return None


def main():
    start = time.time()
    print("=" * 70)
    print("MASSIVE ALPHA DISCOVERY — target top 300 validated wallets")
    print("=" * 70)
    print()

    # Step 1: scan
    buyer_count = scan_buyers(target_sigs=5000)
    print(f"\nFound {len(buyer_count)} unique buyers in scan.")

    # Step 2: filter to 2+ buys
    candidates = [(w, ct) for w, ct in buyer_count.items() if ct >= 2]
    candidates.sort(key=lambda x: -x[1])
    print(f"Candidates with >=2 buys: {len(candidates)}")
    print()

    # Step 3: validate each (cap at 500 candidates max for time budget)
    to_validate = candidates[:500]
    print(f"Validating top {len(to_validate)} most-active candidates...")
    print()
    print(f"{'WALLET':<46} {'PnL':>9} {'TRD':>4} {'WIN%':>5} {'HOLD':>6}")
    print("-" * 78)

    validated = []
    for i, (wallet, _) in enumerate(to_validate):
        if i % 25 == 0 and i > 0:
            print(f"  ... validated {i}/{len(to_validate)}, found {len(validated)} smart so far ({(time.time()-start):.0f}s)", flush=True)
        r = validate_wallet_fast(wallet)
        if not r:
            continue
        wr = r["wins"] / r["trades"] * 100 if r["trades"] else 0
        if r["total_pnl"] > 0 and wr >= 50 and r["avg_hold"] >= 30 and r["trades"] >= 3:
            validated.append((wallet, r["total_pnl"], r["trades"], wr, r["avg_hold"]))

    validated.sort(key=lambda x: -x[1])
    print(f"\n{'='*70}")
    print(f"VALIDATED {len(validated)} smart wallets in {(time.time()-start)/60:.1f} min")
    print(f"{'='*70}\n")

    # Output top 300
    keep = validated[:300]
    for w, pnl, trd, wr, hold in keep[:30]:  # only print top 30
        print(f"{w} {pnl:>+8.4f}  {trd:>3}  {wr:>4.0f}%  {hold:>5.0f}s")

    print()
    print("=" * 70)
    print(f"OUTPUT: top {len(keep)} smart wallets for solana_sniper.py")
    print("=" * 70)
    # Write to file (so we can paste cleanly)
    with open("validated_alphas_300.txt", "w") as f:
        f.write("SMART_WALLETS = {\n")
        for w, pnl, trd, wr, hold in keep:
            label = f"pnl_{pnl:+.2f}_wr_{wr:.0f}_h_{hold:.0f}"
            f.write(f'    "{w}": "{label}",\n')
        f.write("}\n")
    print(f"Written to validated_alphas_300.txt ({len(keep)} wallets)")


if __name__ == "__main__":
    main()
