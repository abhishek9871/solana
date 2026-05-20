"""Find the entry-time fingerprint of trades that ended in quote_profit_bank
(the 93% WR exit bucket). If their entry features cluster tightly, that
fingerprint becomes the entry filter for near-guaranteed wins.

Pull every PGG2-LIVE-BUY and the surrounding context (5 lines before/after)
from the OG logs. Then for each trade, attach all available features.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter, defaultdict


SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/hetzner_sniper"),
       "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10",
       "root@87.99.151.70"]


BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-BUY (?P<mint>\S+) lane=(?P<lane>\S+) cost=(?P<cost>[\d.]+) "
    r"wallet_delta=(?P<wd>[-+\d.]+) sig=(?P<sig>\S+) score=(?P<score>[\d.]+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-SELL (?P<mint>\S+) reason=(?P<reason>\S+) sig=(?P<sig>\S+) "
    r"proceeds=(?P<proc>[-+\d.]+) wallet_delta=(?P<wd>[-+\d.]+) mult=(?P<mult>[\d.]+) "
    r"pnl=(?P<pnl>[-+\d.]+) session=(?P<sess>[-+\d.]+)"
)
# Plan log has rich features: lane name, move, age, b1500, uniq1500, top, sellr, vsol
PLAN_RE = re.compile(
    r"(?:PIGGY-PLAN|priced_snap|preprice_reveal|curve_lag_reveal).*"
    r"move=(?P<move>[\d.]+)x.*age=(?P<age>[\d.]+)s.*"
    r"b1500=(?P<b1500>[\d.]+)/(?P<u1500>\d+).*top=(?P<top>[\d.]+).*"
    r"sellr=(?P<sellr>[-+\d.]+).*vsol=(?P<vsol>[\d.]+)"
)
PIGGY_PLAN_RE = re.compile(r"PIGGY-PLAN (?P<mint>\S+).*?reason=(?P<reason>[^\s].*?)(?=\s+[a-z_]+=|$)")


def fetch_file(remote_path: str) -> str:
    try:
        return subprocess.check_output(SSH + [f"cat {remote_path}"], text=True, errors="replace")
    except Exception:
        return ""


def parse_log(text: str, fname: str) -> list:
    """Pair buy+sell, attach any nearby plan-line features."""
    open_buys: dict[str, dict] = {}
    trades = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = BUY_RE.search(line)
        if m:
            d = m.groupdict()
            # Look back 30 lines for the most recent plan-line that mentions this mint
            move = age = b1500 = u1500 = top = sellr = vsol = None
            for j in range(max(0, i - 50), i):
                prev = lines[j]
                if d["mint"][:4] not in prev:
                    continue
                pm = PLAN_RE.search(prev)
                if pm:
                    move = float(pm.group("move"))
                    age = float(pm.group("age"))
                    b1500 = float(pm.group("b1500"))
                    u1500 = int(pm.group("u1500"))
                    top = float(pm.group("top"))
                    sellr = float(pm.group("sellr"))
                    vsol = float(pm.group("vsol"))
            try:
                hour = int(d["ts"].split(" ")[1].split(":")[0])
            except Exception:
                hour = -1
            open_buys[d["mint"]] = {
                "ts": d["ts"], "hour": hour, "lane": d["lane"], "cost": float(d["cost"]),
                "score": float(d["score"]), "move": move, "age": age,
                "b1500": b1500, "u1500": u1500, "top": top, "sellr": sellr, "vsol": vsol,
                "fname": fname,
            }
            continue
        m = SELL_RE.search(line)
        if m:
            d = m.groupdict()
            buy = open_buys.pop(d["mint"], None)
            if not buy:
                continue
            trades.append({**buy, "reason": d["reason"],
                           "mult": float(d["mult"]), "pnl": float(d["pnl"])})
    return trades


def main():
    files = [x.strip() for x in subprocess.check_output(
        SSH + ["ls /root/piggy/logs/pgg2_direct_live*.log"], text=True).splitlines() if x.strip()]
    print(f"[+] scanning {len(files)} log files...", flush=True)
    all_trades = []
    for f in files:
        all_trades.extend(parse_log(fetch_file(f), os.path.basename(f)))

    priced = [t for t in all_trades if t["lane"] == "priced_snap"]
    print(f"[+] {len(priced)} priced_snap trades parsed", flush=True)

    bank = [t for t in priced if t["reason"] == "quote_profit_bank"]
    other = [t for t in priced if t["reason"] != "quote_profit_bank"]
    print(f"[+] {len(bank)} quote_profit_bank trades, {len(other)} others", flush=True)
    print()

    # Compute feature stats for bank vs others
    def stat(name, key, only_winner=False):
        bv = [t[key] for t in bank if t.get(key) is not None]
        if only_winner:
            ov = [t[key] for t in other if t.get(key) is not None and t["pnl"] > 0]
            olabel = "other_WIN"
        else:
            ov = [t[key] for t in other if t.get(key) is not None]
            olabel = "other"
        if not bv or not ov:
            return
        bv.sort(); ov.sort()
        def s(v):
            return f"[{v[0]:.3f} .. {v[len(v)//2]:.3f} .. {v[-1]:.3f}]"
        print(f"  {name:<10} BANK n={len(bv):>3} {s(bv)}   {olabel} n={len(ov):>3} {s(ov)}")

    print("=== Entry features: quote_profit_bank vs ALL OTHER trades ===")
    for k, label in [("score", "score"), ("cost", "cost"), ("move", "move"),
                     ("age", "age_sec"), ("b1500", "buy1500"), ("u1500", "uniq1500"),
                     ("top", "top1500"), ("sellr", "sell_ratio"), ("vsol", "vsol"),
                     ("hour", "hour")]:
        stat(label, k)

    print()
    print("=== HOUR distribution of profit_bank wins ===")
    bank_hours = Counter(t["hour"] for t in bank)
    other_hours = Counter(t["hour"] for t in other)
    print(f"{'hour':<6} {'bank':>5} {'other':>6} {'bank%':>7}")
    for h in sorted(set(list(bank_hours.keys()) + list(other_hours.keys()))):
        b = bank_hours.get(h, 0)
        o = other_hours.get(h, 0)
        tot = b + o
        pct = b / tot * 100 if tot else 0
        if b > 0 or o > 3:
            print(f"{h:<6d} {b:>5d} {o:>6d} {pct:>6.1f}%")

    # Combined filter search: pairwise feature thresholds
    print()
    print("=== TIGHT SUBSETS — find combo where WR > 85% ===")
    def wr_check(subset):
        n = len(subset); w = sum(1 for t in subset if t["pnl"] > 0)
        return n, w, (w / n if n else 0), sum(t["pnl"] for t in subset)
    candidates = []
    # try every score x b1500 x hour-window combo
    for score_min in [270, 280, 290, 300, 310]:
        for b1500_min in [6, 8, 10, 12]:
            for u1500_min in [4, 6, 8]:
                for hour_window in [None, set(range(16, 21)), set(range(13, 18)),
                                    set(range(20, 24))]:
                    subset = [t for t in priced
                              if t["score"] >= score_min
                              and (t.get("b1500") is None or t["b1500"] >= b1500_min)
                              and (t.get("u1500") is None or t["u1500"] >= u1500_min)
                              and (hour_window is None or t["hour"] in hour_window)]
                    n, w, wr, net = wr_check(subset)
                    if n >= 10 and wr >= 0.50:
                        candidates.append({
                            "score>=": score_min, "b1500>=": b1500_min,
                            "u1500>=": u1500_min, "hours": hour_window,
                            "n": n, "w": w, "wr": wr, "net": net
                        })
    candidates.sort(key=lambda c: (-c["wr"], -c["net"]))
    print(f"{'score>=':<8} {'b1500>=':<8} {'u1500>=':<8} {'hours':<14} {'n':>4} {'W':>3} {'WR%':>5} {'net':>11}")
    print("-" * 75)
    for c in candidates[:15]:
        hours_str = "ALL" if c["hours"] is None else f"{min(c['hours'])}-{max(c['hours'])}"
        print(f"{c['score>=']:<8d} {c['b1500>=']:<8d} {c['u1500>=']:<8d} {hours_str:<14} "
              f"{c['n']:>4d} {c['w']:>3d} {c['wr']*100:>4.0f} {c['net']:>+11.6f}")


if __name__ == "__main__":
    main()
