"""Per-lane analysis of all OG trades. For each lane, find the score-threshold
that maximizes net while preserving winners."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import defaultdict


BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-BUY (?P<mint>\S+) lane=(?P<lane>\S+) cost=(?P<cost>[\d.]+) "
    r"wallet_delta=(?P<wd>[-+\d.]+) sig=(?P<sig>\S+) score=(?P<score>[\d.]+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-SELL (?P<mint>\S+) reason=(?P<reason>\S+) sig=(?P<sig>\S+) "
    r"proceeds=(?P<proc>[-+\d.]+) wallet_delta=(?P<wd>[-+\d.]+) mult=(?P<mult>[\d.]+) "
    r"pnl=(?P<pnl>[-+\d.]+) session=(?P<sess>[-+\d.]+)"
)


def parse_log_text(text: str, fname: str) -> list:
    open_buys: dict[str, dict] = {}
    trades: list[dict] = []
    for line in text.splitlines():
        m = BUY_RE.search(line)
        if m:
            d = m.groupdict()
            open_buys[d["mint"]] = {
                "lane": d["lane"], "cost": float(d["cost"]), "score": float(d["score"]),
            }
            continue
        m = SELL_RE.search(line)
        if m:
            d = m.groupdict()
            buy = open_buys.pop(d["mint"], None)
            if not buy:
                continue
            trades.append({
                "mint": d["mint"], "lane": buy["lane"], "cost": buy["cost"],
                "score": buy["score"], "reason": d["reason"],
                "mult": float(d["mult"]), "pnl": float(d["pnl"]),
            })
    return trades


def fetch_all_trades() -> list:
    ssh = ["ssh", "-i", os.path.expanduser("~/.ssh/hetzner_sniper"),
           "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10",
           "root@87.99.151.70"]
    listing = subprocess.check_output(ssh + ["ls /root/piggy/logs/pgg2_direct_live*.log"], text=True)
    files = [f.strip() for f in listing.splitlines() if f.strip()]
    print(f"[+] {len(files)} OG live logs", flush=True)
    all_trades = []
    for f in files:
        try:
            text = subprocess.check_output(ssh + [f"grep -E 'PGG2-LIVE-(BUY |SELL )' {f}"], text=True)
        except subprocess.CalledProcessError:
            continue
        all_trades.extend(parse_log_text(text, os.path.basename(f)))
    return all_trades


def analyze_lane(lane: str, trades: list) -> dict:
    """For a single lane, sweep score thresholds and find the best one
    that keeps as many wins as possible while improving net the most."""
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n_wins = len(wins)
    n_loss = len(losses)
    baseline_net = sum(t["pnl"] for t in trades)
    if n_wins == 0:
        return {"lane": lane, "verdict": "DROP", "reason": "no wins",
                "n": len(trades), "baseline_net": baseline_net}
    # Sweep thresholds
    scores = sorted(set(t["score"] for t in trades))
    best = {"threshold": 0, "net": baseline_net, "wins_kept": n_wins, "losses_kept": n_loss}
    for th in scores:
        admitted = [t for t in trades if t["score"] >= th]
        net = sum(t["pnl"] for t in admitted)
        w_kept = sum(1 for t in admitted if t["pnl"] > 0)
        l_kept = sum(1 for t in admitted if t["pnl"] <= 0)
        # Require: at least 80% of wins kept
        if w_kept < n_wins * 0.80:
            break  # going past this only loses more wins
        if net > best["net"]:
            best = {"threshold": th, "net": net,
                    "wins_kept": w_kept, "losses_kept": l_kept}
    return {
        "lane": lane,
        "n": len(trades),
        "wins": n_wins, "losses": n_loss,
        "baseline_net": baseline_net,
        "best_threshold": best["threshold"],
        "best_net": best["net"],
        "wins_kept": best["wins_kept"],
        "losses_kept": best["losses_kept"],
        "delta": best["net"] - baseline_net,
        "verdict": "GATE" if best["net"] > baseline_net + 0.005 else "KEEP_AS_IS",
    }


def main():
    trades = fetch_all_trades()
    print(f"[+] {len(trades)} total trades\n", flush=True)
    by_lane = defaultdict(list)
    for t in trades:
        by_lane[t["lane"]].append(t)

    print(f"{'lane':<25} {'n':>5} {'W/L':>9} {'base_net':>10} {'th':>6} "
          f"{'kept_W':>7} {'kept_L':>7} {'gated_net':>11} {'delta':>10} verdict")
    print("-" * 110)
    grand_baseline = 0.0
    grand_gated = 0.0
    for lane in sorted(by_lane.keys()):
        r = analyze_lane(lane, by_lane[lane])
        grand_baseline += r["baseline_net"]
        grand_gated += r.get("best_net", r["baseline_net"])
        if r.get("verdict") == "DROP":
            print(f"{lane:<25} {r['n']:>5} {0:>4}/{0:<3} {r['baseline_net']:>+10.6f} "
                  f"{'-':>6} {'-':>7} {'-':>7} {'-':>11} {'-':>10} DROP (no wins)")
            continue
        print(f"{lane:<25} {r['n']:>5} {r['wins']:>4}/{r['losses']:<3} "
              f"{r['baseline_net']:>+10.6f} {r['best_threshold']:>6.1f} "
              f"{r['wins_kept']:>7d} {r['losses_kept']:>7d} "
              f"{r['best_net']:>+11.6f} {r['delta']:>+10.6f} {r['verdict']}")
    print("-" * 110)
    print(f"{'TOTAL':<25} {len(trades):>5} {'':>9} {grand_baseline:>+10.6f} "
          f"{'':>6} {'':>7} {'':>7} {grand_gated:>+11.6f} {grand_gated-grand_baseline:>+10.6f}")


if __name__ == "__main__":
    main()
