"""Deep mining of OG live trades. Extract every entry-time feature available
from the log, find the tightest subset with maximum WR.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict


# Buy line has: mint lane cost wallet_delta sig score
BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-BUY (?P<mint>\S+) lane=(?P<lane>\S+) cost=(?P<cost>[\d.]+) "
    r"wallet_delta=(?P<wd>[-+\d.]+) sig=(?P<sig>\S+) score=(?P<score>[\d.]+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-SELL (?P<mint>\S+) reason=(?P<reason>\S+) sig=(?P<sig>\S+) "
    r"proceeds=(?P<proc>[-+\d.]+) wallet_delta=(?P<wd>[-+\d.]+) mult=(?P<mult>[\d.]+) "
    r"pnl=(?P<pnl>[-+\d.]+) session=(?P<sess>[-+\d.]+)"
)
# Reason string in the BUY context has entry_move/age/buy1500/uniq1500/top/sell_ratio
# but the BUY log itself doesn't carry these. They appear in DIRECT/STRIKE PLAN logs near each buy.
PLAN_RE = re.compile(
    r"plan=priced_snap\s+(?P<mint>\S+)\s+(?P<rest>.*)"
)


def parse_log_text(text: str, fname: str) -> list:
    """Pair BUY+SELL within the log, return trade records."""
    open_buys: dict[str, dict] = {}
    trades: list[dict] = []
    # Build a hour-of-day index
    for line in text.splitlines():
        m = BUY_RE.search(line)
        if m:
            d = m.groupdict()
            # Parse hour-of-day from "2026-05-07 16:03:25"
            ts = d["ts"]
            try:
                hour = int(ts.split(" ")[1].split(":")[0])
            except Exception:
                hour = -1
            open_buys[d["mint"]] = {
                "ts": ts, "hour": hour, "lane": d["lane"],
                "cost": float(d["cost"]), "score": float(d["score"]),
                "fname": fname,
            }
            continue
        m = SELL_RE.search(line)
        if m:
            d = m.groupdict()
            buy = open_buys.pop(d["mint"], None)
            if not buy:
                continue
            trades.append({
                **buy,
                "reason": d["reason"], "mult": float(d["mult"]),
                "pnl": float(d["pnl"]),
            })
    return trades


def fetch_all():
    ssh = ["ssh", "-i", os.path.expanduser("~/.ssh/hetzner_sniper"),
           "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10",
           "root@87.99.151.70"]
    files = subprocess.check_output(ssh + ["ls /root/piggy/logs/pgg2_direct_live*.log"], text=True).splitlines()
    out = []
    for f in [x.strip() for x in files if x.strip()]:
        try:
            text = subprocess.check_output(ssh + [f"grep -E 'PGG2-LIVE-(BUY |SELL )' {f}"], text=True)
        except subprocess.CalledProcessError:
            continue
        out.extend(parse_log_text(text, os.path.basename(f)))
    return out


def main():
    trades = [t for t in fetch_all() if t["lane"] == "priced_snap"]
    print(f"[+] priced_snap trades: {len(trades)}", flush=True)
    print()

    # Score buckets — find highest WR within tight score windows
    print("=== SCORE band x COST band x HOUR ===")
    print(f"{'cost_band':<10} {'score_band':<14} {'hours':<10} {'n':>4} {'W':>3} {'WR%':>5} {'net':>11} {'avg_W':>9} {'avg_L':>9}")
    print("-" * 90)
    cost_bands = [(0.014, 0.018, "0.01-0.02"),
                  (0.018, 0.025, "0.02-0.025"),
                  (0.025, 0.035, "0.025-0.035"),
                  (0.035, 0.055, "0.035-0.055"),
                  (0.055, 1.0, "0.055+")]
    score_bands = [(0, 250, "<250"), (250, 270, "250-270"),
                   (270, 290, "270-290"), (290, 310, "290-310"),
                   (310, 999, ">=310")]
    hour_groups = [(set(range(0, 24)), "ALL"),
                   ({0, 1, 2, 3, 4, 5}, "EARLY 0-5"),
                   ({6, 7, 8, 9, 10, 11}, "MORN 6-11"),
                   ({12, 13, 14, 15, 16, 17}, "AFT 12-17"),
                   ({18, 19, 20, 21, 22, 23}, "EVE 18-23")]
    best_subsets = []
    for cl, ch, cn in cost_bands:
        for sl, sh, sn in score_bands:
            for hours, hn in hour_groups:
                bk = [t for t in trades
                      if cl <= t["cost"] < ch
                      and sl <= t["score"] < sh
                      and t["hour"] in hours]
                if len(bk) < 5:
                    continue
                w = sum(1 for t in bk if t["pnl"] > 0)
                net = sum(t["pnl"] for t in bk)
                wr = w / len(bk)
                wins = [t["pnl"] for t in bk if t["pnl"] > 0]
                losses = [t["pnl"] for t in bk if t["pnl"] <= 0]
                avg_w = sum(wins)/len(wins) if wins else 0
                avg_l = sum(losses)/len(losses) if losses else 0
                best_subsets.append((cn, sn, hn, len(bk), w, wr, net, avg_w, avg_l))
                if wr >= 0.50 and len(bk) >= 10:
                    print(f"{cn:<10} {sn:<14} {hn:<10} {len(bk):>4} {w:>3} "
                          f"{wr*100:>4.0f} {net:>+11.6f} {avg_w:>+9.6f} {avg_l:>+9.6f}")

    print()
    print("=== TOP 15 SUBSETS BY WIN RATE (n>=10) ===")
    print(f"{'cost':<10} {'score':<14} {'hours':<10} {'n':>4} {'W':>3} {'WR%':>5} {'net':>11}")
    print("-" * 75)
    best_subsets.sort(key=lambda x: (-x[5], -x[6]))
    for row in best_subsets[:20]:
        cn, sn, hn, n, w, wr, net, avg_w, avg_l = row
        if n >= 10:
            print(f"{cn:<10} {sn:<14} {hn:<10} {n:>4} {w:>3} {wr*100:>4.0f} {net:>+11.6f}")


if __name__ == "__main__":
    main()
