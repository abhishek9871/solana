"""
Validate each smart wallet's REAL pump.fun PnL.

For each wallet:
  1. Get last 30 transactions
  2. Find pump.fun BUY/SELL pairs for the same mint
  3. Calculate hold time and PnL per pair
  4. Aggregate: total PnL, avg hold time, win rate

Filter to only TRUE smart wallets:
  - Win rate >= 50%
  - Avg hold time >= 30 seconds (not MEV/sniper bots)
  - Net PnL > 0 in last 30 trades
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

WALLETS = [
    "7r14pGkk9x45i7z5L2smaeCpACQVenNXEnV9FL7cov2j",
    "WEHWDpeb45DyGafcc75c8RhSww8PYkeQBrSFyrvapvR",
    "omegoMAe1AMY5MFKQQr3JwXVy8F4eCvmBAfcpo8XAfq",
    "HNKvqBj7majHp8gbQgSwpxdEk1vPWrcdghs5EMYk2ZEw",
    "3jvM9vL8SAh1xVbXPkbd5uLiA5VaFN1VBMgb7ManU6vM",
    "9oieEBu7gprdMmWrSKQFMk48DiCa3AH2edY2eZn8ortJ",
    "CoHYfUDxSk32F9fZqsdJTaFf8yGU5B4i5GwrfCCm3rHn",
    "Gvu4wQZuZcDC8yonafDsKqaJCWLD2e45FUVCTPQ2kXU9",
    "6t6E2wJnKSwgeJM5vWoPhq6g1uD5rMvuF4dnituFgEjp",
    "7x73hzxXYU4bJeesYzm4FJNKNc1jSH2urR7mBRdYM3bD",
    "F5Hrs3fTxA6cPsdYa1r2zazymsetbFpXpzEuQWXPNusu",
    "8dtx2tr4TuJsYpri2suggFu1pg3DVjFLBBVmhtDy1MEF",
    "vMpqXnizwETfNePsJd82hdS9qAK2MU5d92GevFpE4r3",
    "BesRBxseW4jZdUt4w7Nvici3mqH4jsfC7vu62B2WGzgd",
    "CPvQ51WkJAj3rgniXRjv1JjncwCzYZZBsgqVhE4frcqh",
    "BHbpFZdLwiWZfRY8b8sFo51bZpkrNDPXCvL8ZRquTpbf",
    "DZurvQ7Lwv8xz983y8Xx7TfJntMmF41yscR7SoDU4aNt",
    "haqqiUUXB5gp7gDbSjpEkw3phAjQb25r86uXJsZQgWq",
    "DjZXpQSomAYzESMwdNNEc4Jgg52qhfAYWs43Qh4EcqFY",
    "CXdZ6m2PotNq4D7zKK3af426ohGro1xingNuf68SoKmv",
    "9cxe5fJJ5YfFnS12tixzy3Bcm4BhiSZTCGCrV1V2BWXi",
    "2VbTAgcQNt4e2HLtR5C6APGSBs5iqYSFXW7C25qh3aW4",
    "DdyQSmVYv5RDxYUzBw2c2zvxDfBzzxrrgHFjVqrZFsk7",
    "Ev4vYb4HLTdUcZZkEjC2mZYk78e2zHRxDbYpH7C3qYSb",
    "xL4gFGUx7TpxhQqsyzfRFartJ6UxwcfH9nQitnyEsH7",
    "DD2qxJP24AEemDevYYLw4y7z6yPWCtG3oC8cGJ5x4pif",
    "6Mj8JR3NkwiFnbuyD4wNYadPfvPXFQCAHBswDfrMePPD",
    "mMtkhHnTLgE6fPGKSxq3BUpCRMHx1deWUMemgZLVA1Y",
    "GpWzQoGQRY5Lcb4NTeZvb2HjCcG5y8Wy46TBejzZu18g",
    "5EPN2bhCSGc4WAcHx3x79KmT8vNVrASkKvmhzCkSempu",
    "Csg3fe7CRD65XNknyQN22gpc1gQeukGgckARf3LoSviu",
    "DFoCmLSdJhcBPMt5UV3eqb1Z4zRUeSXKGDAk9FHEsYiN",
    "9Bv8xHEPRNERSJdYkkZSBPgQGnA6atFDbZjCd42MFqEP",
    "DHbx6cSyVBSx2qZfNqYf1FG9UHQzfAkRoCsC9DnRCmja",
    "EchrsKe4sYU1KFu5oHaS45Yo2reRJzduY4emmusVZ2Fb",
    "ASjWM88oTgvmBksXXepXVvhggen8tgKiEWATKAA1u9Hk",
    "3zGr4bLJ6cWCSYQxibUSM8pVQVFWEwmtw6voT4kNCUF5",
    "ENpxborBMaWETk96MbwasDuYC3TkdPio5EkkqVwdNFa7",
    "Bpke38uhwBmew2UGhdr31X4dJD85NMFeVjULwJc2AzT2",
    "AL7CM5GaZ3koXq4eVQmzHjECvWS78j2WEoDSRtEYuqSA",
    "EQYRwojm39HCPGeSnZL1EzLYEPK5SGa5A7c6UKXPLxiQ",
    "CRUJwDkuUtyXi4JLLqK9XedKd8jHxkvHZZrbkA8CWTee",
    "GxmLUrndMiYt9eMHG4vAoeXjhkAmpfRma9ZpfgqYhgii",
    "4oqC5j6s3bHrZVG6PrsAFSHuKaqaQJpP1qeqCU9M9Q1b",
    "BnHZ99JebZEw17DXQnrnZeF3VqEBLigCNxxmv8GcobKC",
    "2ieKvTfRAd8qYGt53GtACecKjKTf4RbDpxZYpvPfjPTz",
    "Deus3xmoJ9jrG6zapzEGGvXa8JgJDBaheCQeWps2GWHx",
    "HcYgKgSiV947513GrjQ5h8knbMCsHnzwnRZHUnQEyj6d",
    "AEAvV3s6kajZyZgXw2pZSbdcgwXuUgDJ61jecczAXLMv",
    "3QqmsedKtYV9SAtohH4H4ihXwdGMEb7pCP5AHTcDRgNQ",
]


def analyze_wallet(wallet):
    """Returns dict with: win_rate, avg_hold_sec, total_sol_pnl, num_trades"""
    try:
        sigs = c.get_signatures_for_address(Pubkey.from_string(wallet), limit=50,
                                              commitment=Confirmed)
        # Map: mint -> list of (timestamp, is_buy, sol_amount, token_amount)
        actions = defaultdict(list)
        for s in sigs.value[:50]:
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
                if not (is_buy or is_sell):
                    continue
                bt = getattr(s, "block_time", None) or 0
                # Find target mint via pre/post token balances
                pre_bals = {f"{b.account_index}:{str(b.mint)}":
                              float(b.ui_token_amount.ui_amount or 0) for b in (meta.pre_token_balances or [])}
                post_bals = {f"{b.account_index}:{str(b.mint)}":
                               float(b.ui_token_amount.ui_amount or 0) for b in (meta.post_token_balances or [])}
                # SOL change: lookup pre/post for SOL accounts (use first account, fee payer)
                # We'll just track via mint balance changes
                target_mint = None
                token_delta = 0
                for k, post_amt in post_bals.items():
                    idx, mint = k.split(":", 1)
                    if mint == "So11111111111111111111111111111111111111112": continue
                    if not mint.lower().endswith("pump"): continue
                    pre_amt = pre_bals.get(k, 0)
                    delta = post_amt - pre_amt
                    if abs(delta) > 0:
                        target_mint = mint
                        token_delta = delta
                        break
                # Also include pre-only balances (full sell)
                if target_mint is None:
                    for k, pre_amt in pre_bals.items():
                        idx, mint = k.split(":", 1)
                        if mint == "So11111111111111111111111111111111111111112": continue
                        if not mint.lower().endswith("pump"): continue
                        post_amt = post_bals.get(k, 0)
                        delta = post_amt - pre_amt
                        if delta < 0:  # they sold
                            target_mint = mint
                            token_delta = delta
                            break
                if not target_mint: continue
                # SOL delta: estimate from pre/post lamports of fee payer (account 0)
                pre_sol = (meta.pre_balances[0] if meta.pre_balances else 0) / 1e9
                post_sol = (meta.post_balances[0] if meta.post_balances else 0) / 1e9
                sol_delta = post_sol - pre_sol  # negative = spent SOL (buy), positive = received SOL (sell)
                actions[target_mint].append({
                    "ts": bt,
                    "is_buy": is_buy,
                    "is_sell": is_sell,
                    "token_delta": token_delta,
                    "sol_delta": sol_delta,
                })
            except: continue

        # Pair buys and sells per mint
        total_pnl = 0
        hold_times = []
        wins = 0
        losses = 0
        for mint, acts in actions.items():
            acts.sort(key=lambda a: a["ts"] or 0)
            buy = None
            for a in acts:
                if a["is_buy"]:
                    buy = a
                elif a["is_sell"] and buy:
                    cost_sol = -buy["sol_delta"]   # SOL spent (positive number)
                    recv_sol = a["sol_delta"]      # SOL received
                    pnl = recv_sol - cost_sol
                    total_pnl += pnl
                    hold_t = (a["ts"] or 0) - (buy["ts"] or 0) if a["ts"] and buy["ts"] else 0
                    hold_times.append(hold_t)
                    if pnl > 0: wins += 1
                    else: losses += 1
                    buy = None  # reset for next pair
        return {
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "avg_hold": sum(hold_times) / len(hold_times) if hold_times else 0,
            "trades": len(hold_times),
        }
    except Exception as e:
        return {"err": str(e)}


def main():
    print(f"Validating {len(WALLETS)} wallets...")
    print()
    print(f"{'WALLET':<46} {'PnL_SOL':>9} {'TRADES':>7} {'WIN%':>5} {'HOLD_S':>7}")
    print("-" * 78)

    results = []
    for i, w in enumerate(WALLETS):
        if i % 10 == 0:
            print(f"  ... validating {i}/{len(WALLETS)}", flush=True)
        r = analyze_wallet(w)
        if "err" in r:
            continue
        if r["trades"] == 0:
            continue
        wr = r["wins"] / r["trades"] * 100 if r["trades"] else 0
        results.append((w, r["total_pnl"], r["trades"], wr, r["avg_hold"]))

    print()
    print("=" * 78)
    print("RESULTS (sorted by total PnL):")
    print("=" * 78)
    results.sort(key=lambda x: -x[1])
    for w, pnl, trades, wr, hold in results:
        marker = " *SMART*" if pnl > 0 and wr >= 50 and hold >= 30 else ""
        print(f"{w} {pnl:>+8.4f}  {trades:>6}  {wr:>4.0f}%  {hold:>6.0f}s{marker}")

    print()
    print("=" * 78)
    print("FILTERED — true smart wallets (PnL > 0, win rate >= 50%, hold >= 30s):")
    print("=" * 78)
    print("VALIDATED_SMART_WALLETS = {")
    kept = 0
    for w, pnl, trades, wr, hold in results:
        if pnl > 0 and wr >= 50 and hold >= 30 and trades >= 3:
            print(f'    "{w}": "pnl_{pnl:+.3f}_wr_{wr:.0f}_hold_{hold:.0f}s",')
            kept += 1
    print("}")
    print()
    print(f"Total validated: {kept} wallets out of {len(WALLETS)}")


if __name__ == "__main__":
    main()
