import json
import sys
from collections import defaultdict

BASE = "/root/piggy"
runid = sys.argv[1]
raw_path = f"{BASE}/data/{runid}_raw.jsonl"
state_path = f"{BASE}/data/{runid}_state.json"
run_start_ms = 0
try:
    run_start_ms = int(float(json.load(open(state_path))["session"]["started_at"]) * 1000)
except Exception:
    pass

events = defaultdict(list)
for line in open(raw_path):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    if x.get("kind") != "trade":
        continue
    if run_start_ms and int(x.get("ts_ms") or 0) < run_start_ms:
        continue
    m = x.get("mint")
    if m:
        events[m].append(x)


def stats(rows, start, end):
    buy = sell = 0.0
    buyers = defaultdict(float)
    buys = sells = 0
    for x in rows:
        ts = int(x.get("ts_ms") or 0)
        if ts < start or ts > end:
            continue
        sol = float(x.get("sol") or 0.0)
        if x.get("side") == "buy":
            buy += sol
            buys += 1
            buyers[x.get("user") or x.get("signer") or "?"] += sol
        elif x.get("side") == "sell":
            sell += sol
            sells += 1
    top = max(buyers.values()) / buy if buy > 0 and buyers else 1.0
    return buy, sell, len(buyers), top, buys, sells


def first_cross(rows, threshold):
    first_price = 0.0
    for x in rows:
        p = float(x.get("curve_price") or 0.0)
        if p > 0:
            first_price = p
            break
    if first_price <= 0:
        return None
    for x in rows:
        p = float(x.get("curve_price") or 0.0)
        ts = int(x.get("ts_ms") or 0)
        if p > 0 and p / first_price >= threshold and x.get("side") == "buy":
            return x, first_price
    return None


for threshold in (1.25, 1.35, 1.50, 1.75):
    cands = []
    for m, rows in events.items():
        rows.sort(key=lambda x: int(x.get("ts_ms") or 0))
        found = first_cross(rows, threshold)
        if not found:
            continue
        x, first_price = found
        ts = int(x.get("ts_ms") or 0)
        ep = float(x.get("curve_price") or 0.0)
        b700, s700, u700, top700, nb700, ns700 = stats(rows, ts - 700, ts)
        b1500, s1500, u1500, top1500, nb1500, ns1500 = stats(rows, ts - 1500, ts)
        sellr1500 = s1500 / max(b1500, 0.001)
        post = [
            (int(y.get("ts_ms") or 0), float(y.get("curve_price") or 0.0))
            for y in rows
            if int(y.get("ts_ms") or 0) >= ts and int(y.get("ts_ms") or 0) <= ts + 60000 and float(y.get("curve_price") or 0.0) > 0
        ]
        if not post:
            continue
        mx_ts, mx = max(post, key=lambda z: z[1])
        mn_ts, mn = min(post, key=lambda z: z[1])
        cands.append(
            (
                mx / ep,
                m,
                mn / ep,
                (mx_ts - ts) / 1000,
                (mn_ts - ts) / 1000,
                ep / first_price,
                b700,
                u700,
                top700,
                b1500,
                u1500,
                top1500,
                sellr1500,
                float(x.get("vsol_sol") or 0.0),
            )
        )
    winners = [c for c in cands if c[0] >= 1.35 and c[2] >= 0.88]
    print(f"threshold={threshold:.2f} candidates={len(cands)} clean_winish={len(winners)}")
    for c in sorted(cands, reverse=True)[:20]:
        mx, m, mn, tmax, tmin, emove, b700, u700, top700, b1500, u1500, top1500, sellr, vsol = c
        print(
            f"  {m[:8]} post={mx:4.2f}x min={mn:4.2f}x tmax={tmax:5.1f}s tmin={tmin:5.1f}s "
            f"entry={emove:4.2f} vsol={vsol:5.1f} b700={b700:5.2f}/{u700} top={top700:.2f} "
            f"b1500={b1500:5.2f}/{u1500} top15={top1500:.2f} sellr={sellr:.2f}"
        )
