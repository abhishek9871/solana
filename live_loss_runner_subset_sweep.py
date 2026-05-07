import itertools
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


def short(m):
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
    of = d["open"].get("features") or {}
    ep = f(of.get("price"))
    if ep <= 0:
        continue
    key = short(m)
    short_to_full[key] = m
    row = {
        "mint": m,
        "short": key,
        "pnl": f(d["close"].get("pnl_sol")),
        "reason": d["close"].get("reason"),
        "entry_ts": d["open"].get("ts_ms", 0),
        "close_ts": d["close"].get("ts_ms", 0),
        "entry_price": ep,
        "post_max_mult": 0.0,
    }
    for k in ["buy700","buy1500","uniq700","uniq1500","top_share700","top_share1500","score","cluster_score","move700","move1500","last_sell_age_ms","first_buy_sol","slot_buyers","slot_buy_sol","buyer_hhi700"]:
        row[k] = f(of.get(k))
    trades[m] = row

rx_sell = re.compile(r"PGG2-LIVE-SELL\s+(\S+).*proceeds=([0-9.]+).*mult=([0-9.]+).*pnl=([-+0-9.]+)")
for line in open(LOG, errors="replace"):
    m = rx_sell.search(line)
    if not m:
        continue
    full = short_to_full.get(m.group(1))
    if full and full in trades:
        trades[full]["proceeds"] = f(m.group(2))
        trades[full]["close_mult"] = f(m.group(3), 1.0)

for line in open(RAW):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    t = trades.get(x.get("mint"))
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

losses = [t for t in trades.values() if t["pnl"] <= 0 and "proceeds" in t and t.get("close_mult", 0) > 0]
base = sum(t["pnl"] for t in trades.values())

def runner_delta(selected, frac=0.05, target=1.5):
    delta = 0.0
    hit = miss = 0
    for t in selected:
        if t["post_max_mult"] >= target:
            d = frac * t["proceeds"] * (target / max(t["close_mult"], 0.000001) - 1.0)
            hit += 1
        else:
            d = -frac * t["proceeds"]
            miss += 1
        delta += d
    return delta, hit, miss

features = ["buy700","buy1500","uniq700","uniq1500","top_share700","top_share1500","score","cluster_score","move700","move1500","first_buy_sol","slot_buyers","slot_buy_sol","buyer_hhi700"]
conds = []
for k in features:
    vals = sorted(set(t[k] for t in losses))
    if len(vals) < 4:
        continue
    for p in [0.25, 0.33, 0.50, 0.67, 0.75]:
        th = vals[int((len(vals)-1)*p)]
        conds.append((f"{k} < {th:.6g}", lambda t, k=k, th=th: t[k] < th))
        conds.append((f"{k} > {th:.6g}", lambda t, k=k, th=th: t[k] > th))

results = []
for name, pred in conds:
    selected = [t for t in losses if pred(t)]
    if not selected:
        continue
    for frac in [0.03, 0.05, 0.08, 0.10]:
        for target in [1.3, 1.5, 2.0]:
            delta, hit, miss = runner_delta(selected, frac, target)
            results.append((delta, name, frac, target, len(selected), hit, miss))

for (n1,p1),(n2,p2) in itertools.combinations(conds,2):
    selected = [t for t in losses if p1(t) and p2(t)]
    if len(selected) < 2:
        continue
    for frac in [0.03, 0.05, 0.08, 0.10]:
        for target in [1.3, 1.5, 2.0]:
            delta, hit, miss = runner_delta(selected, frac, target)
            results.append((delta, f"({n1}) AND ({n2})", frac, target, len(selected), hit, miss))

print(f"base={base:+.6f} losses={len(losses)}")
print("BEST_POSITIVE_LOSS_RUNNER_SUBSETS worst-case miss assumes runner goes to zero")
for delta, name, frac, target, n, hit, miss in sorted(results, reverse=True)[:40]:
    if delta <= 0:
        break
    print(f"delta={delta:+.6f} new={base+delta:+.6f} frac={frac:.2f} target={target:.1f} selected={n:2d} hit={hit:2d} miss={miss:2d} cond={name}")

