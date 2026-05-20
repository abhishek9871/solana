"""Mine all OG live-runner trades to find the discriminator that separates
winners from losers. Inputs: every pgg2_direct_live_*.log on Hetzner.

Per trade extract:
  - mint (4-char prefix)
  - score at entry (from PGG2-LIVE-BUY)
  - cost (size in SOL)
  - exit reason (from PGG2-LIVE-SELL)
  - mult (exit/entry multiplier)
  - pnl (SOL gain/loss)

Then find: what score threshold maximizes (wins kept) - (losses kept)?
And: do exit reasons cluster by win/loss?

Run on Hetzner via SSH.
"""
from __future__ import annotations

import glob
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
    """Parse log content into list of paired buy/sell trades."""
    open_buys: dict[str, dict] = {}
    trades: list[dict] = []
    for line in text.splitlines():
        m = BUY_RE.search(line)
        if m:
            d = m.groupdict()
            open_buys[d["mint"]] = {
                "ts_buy": d["ts"], "lane": d["lane"],
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
                "fname": fname,
                "mint": d["mint"],
                "ts_buy": buy["ts_buy"],
                "ts_sell": d["ts"],
                "lane": buy["lane"],
                "cost": buy["cost"],
                "score": buy["score"],
                "reason": d["reason"],
                "proceeds": float(d["proc"]),
                "mult": float(d["mult"]),
                "pnl": float(d["pnl"]),
            })
    return trades


def fetch_remote_logs(host_path: str = "root@87.99.151.70:/root/piggy/logs") -> list:
    """SSH cat each remote log and parse it."""
    ssh = ["ssh", "-i", os.path.expanduser("~/.ssh/hetzner_sniper"),
           "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10",
           host_path.split(":")[0]]
    listing = subprocess.check_output(ssh + ["ls /root/piggy/logs/pgg2_direct_live*.log"], text=True)
    files = [f.strip() for f in listing.splitlines() if f.strip()]
    print(f"[+] {len(files)} OG live logs to scan", flush=True)
    all_trades = []
    for f in files:
        try:
            text = subprocess.check_output(ssh + [f"grep -E 'PGG2-LIVE-(BUY |SELL )' {f}"], text=True)
        except subprocess.CalledProcessError:
            continue
        trades = parse_log_text(text, os.path.basename(f))
        if trades:
            all_trades.extend(trades)
    return all_trades


def main() -> int:
    trades = fetch_remote_logs()
    if not trades:
        print("no trades parsed", flush=True)
        return 1
    print(f"[+] parsed {len(trades)} closed trades", flush=True)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    print(f"[+] {len(wins)} WINS / {len(losses)} LOSSES", flush=True)
    print(f"[+] baseline net: {sum(t['pnl'] for t in trades):+.6f} SOL", flush=True)
    print(flush=True)

    # === score distribution ===
    def stats(name, values):
        if not values:
            return
        s = sorted(values)
        n = len(s)
        print(f"  {name:<10} n={n} min={s[0]:.1f} p25={s[n//4]:.1f} med={s[n//2]:.1f} "
              f"p75={s[3*n//4]:.1f} max={s[-1]:.1f}", flush=True)

    print("SCORE distribution:")
    stats("WIN", [t["score"] for t in wins])
    stats("LOSS", [t["score"] for t in losses])
    print(flush=True)

    # === score threshold sweep ===
    print("SCORE-THRESHOLD SWEEP (admit if score >= threshold):")
    print(f"{'thresh':>8} {'wins_kept':>10} {'losses_kept':>12} {'net':>12} {'delta':>10}", flush=True)
    print("-" * 60, flush=True)
    baseline_net = sum(t["pnl"] for t in trades)
    for th in [0, 100, 150, 200, 220, 240, 250, 260, 270, 280, 290, 300, 320, 350, 400]:
        admitted = [t for t in trades if t["score"] >= th]
        if not admitted:
            continue
        w = sum(1 for t in admitted if t["pnl"] > 0)
        l = sum(1 for t in admitted if t["pnl"] <= 0)
        net = sum(t["pnl"] for t in admitted)
        delta = net - baseline_net
        mark = " <-- improves" if delta > 0 else ""
        print(f"{th:>8d} {w:>10d} {l:>12d} {net:>+12.6f} {delta:>+10.6f}{mark}", flush=True)
    print(flush=True)

    # === exit reasons cluster by W/L? ===
    print("EXIT REASON breakdown:")
    reasons = defaultdict(lambda: {"w": 0, "l": 0, "net": 0.0})
    for t in trades:
        bucket = "w" if t["pnl"] > 0 else "l"
        reasons[t["reason"]][bucket] += 1
        reasons[t["reason"]]["net"] += t["pnl"]
    print(f"{'reason':<40} {'wins':>5} {'losses':>7} {'net':>12}", flush=True)
    print("-" * 70, flush=True)
    for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]["net"]):
        print(f"{r:<40} {c['w']:>5} {c['l']:>7} {c['net']:>+12.6f}", flush=True)
    print(flush=True)

    # === cost (trade-size) breakdown ===
    print("BY-SIZE bucket:")
    buckets = [(0.015, "tiny<=0.015"), (0.020, "0.015-0.02"), (0.030, "0.02-0.03"),
               (0.050, "0.03-0.05"), (1.0, "0.05+")]
    prev = 0.0
    for cap, name in buckets:
        bk = [t for t in trades if prev < t["cost"] <= cap]
        prev = cap
        if not bk:
            continue
        w = sum(1 for t in bk if t["pnl"] > 0)
        net = sum(t["pnl"] for t in bk)
        print(f"  {name:<15} n={len(bk):>3} wins={w:>3} ({w/len(bk)*100:.0f}%) "
              f"net={net:+.6f}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
