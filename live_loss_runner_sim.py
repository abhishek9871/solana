import json
import re
import sys
from collections import defaultdict
from pathlib import Path


RUNID = sys.argv[1] if len(sys.argv) > 1 else "pgg2_direct_live_20260506_214938"
BASE = Path("/root/piggy")
DEC = BASE / "data" / f"{RUNID}_decisions.jsonl"
RAW = BASE / "data" / f"{RUNID}_raw.jsonl"
LOG = BASE / "logs" / f"{RUNID}.log"


def f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def s(m):
    return f"{m[:4]}..{m[-4:]}" if m else "?"


by = defaultdict(dict)
for line in open(DEC):
    if not line.strip():
        continue
    x = json.loads(line)
    m = x.get("mint")
    if not m:
        continue
    if x.get("kind") in {"open", "close"}:
        by[m][x.get("kind")] = x

trades = {}
short_to_full = {}
for m, d in by.items():
    if "open" not in d or "close" not in d:
        continue
    ep = f((d["open"].get("features") or {}).get("price"))
    if ep <= 0:
        continue
    key = s(m)
    short_to_full[key] = m
    trades[m] = {
        "mint": m,
        "short": key,
        "pnl": f(d["close"].get("pnl_sol")),
        "reason": d["close"].get("reason"),
        "entry_ts": d["open"].get("ts_ms", 0),
        "close_ts": d["close"].get("ts_ms", 0),
        "entry_price": ep,
        "post_max_mult": 0.0,
    }

rx_sell = re.compile(r"PGG2-LIVE-SELL\s+(\S+).*reason=(\S+).*proceeds=([0-9.]+).*mult=([0-9.]+).*pnl=([-+0-9.]+)")
for line in open(LOG, errors="replace"):
    m = rx_sell.search(line)
    if not m:
        continue
    full = short_to_full.get(m.group(1))
    if full and full in trades:
        trades[full]["proceeds"] = f(m.group(3))
        trades[full]["close_mult"] = f(m.group(4), 1.0)

for line in open(RAW):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    m = x.get("mint")
    t = trades.get(m)
    if not t:
        continue
    ts = x.get("ts_ms", 0)
    if ts <= t["close_ts"] or ts > t["close_ts"] + 60000:
        continue
    cp = f(x.get("curve_price"))
    if cp <= 0:
        continue
    mult = cp / t["entry_price"]
    if mult > t["post_max_mult"]:
        t["post_max_mult"] = mult

base = sum(t["pnl"] for t in trades.values())
losses = [t for t in trades.values() if t["pnl"] <= 0 and "proceeds" in t and t.get("close_mult", 0) > 0]
print(f"base={base:+.6f} losses_with_log={len(losses)}")

for frac in [0.05, 0.10, 0.15, 0.20, 0.25]:
    for target in [1.3, 1.5, 2.0, 3.0]:
        delta = 0.0
        hit = 0
        miss = 0
        extra_loss = 0.0
        extra_win = 0.0
        for t in losses:
            proceeds = t["proceeds"]
            close_mult = max(t["close_mult"], 0.000001)
            if t["post_max_mult"] >= target:
                d = frac * proceeds * (target / close_mult - 1.0)
                hit += 1
                extra_win += d
            else:
                # Worst-case: retained runner becomes worthless.
                d = -frac * proceeds
                miss += 1
                extra_loss += d
            delta += d
        print(
            f"frac={frac:.2f} target={target:.1f} hit={hit:2d} miss={miss:2d} "
            f"delta={delta:+.6f} new={base+delta:+.6f} extra_win={extra_win:+.6f} extra_loss={extra_loss:+.6f}"
        )
    print()

print("best_loss_runner_candidates")
for t in sorted(losses, key=lambda x: x["post_max_mult"], reverse=True)[:20]:
    print(
        f"{t['short']:12s} reason={t['reason']:34s} pnl={t['pnl']:+.6f} close_mult={t.get('close_mult'):.3f} "
        f"proceeds={t.get('proceeds'):.6f} post_max={t['post_max_mult']:.2f}"
    )

