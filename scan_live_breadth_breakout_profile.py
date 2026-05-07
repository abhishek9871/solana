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
    for x in rows:
        ts = int(x.get("ts_ms") or 0)
        if ts < start or ts > end:
            continue
        sol = float(x.get("sol") or 0.0)
        user = x.get("user") or x.get("signer") or "?"
        if x.get("side") == "buy":
            buy += sol
            buyers[user] += sol
        elif x.get("side") == "sell":
            sell += sol
    top = max(buyers.values()) / buy if buy > 0 and buyers else 1.0
    return buy, sell, len(buyers), top


rows_out = []
for m, rows in events.items():
    rows.sort(key=lambda x: int(x.get("ts_ms") or 0))
    first_price = next((float(x.get("curve_price") or 0.0) for x in rows if float(x.get("curve_price") or 0.0) > 0), 0.0)
    if first_price <= 0:
        continue
    for x in rows:
        if x.get("side") != "buy":
            continue
        ts = int(x.get("ts_ms") or 0)
        price = float(x.get("curve_price") or 0.0)
        if price <= 0:
            continue
        entry = price / first_price
        b700, s700, u700, top700 = stats(rows, ts - 700, ts)
        b1500, s1500, u1500, top1500 = stats(rows, ts - 1500, ts)
        sellr = s1500 / max(b1500, 0.001)
        vsol = float(x.get("vsol_sol") or 0.0)
        if not (
            1.45 <= entry <= 1.70
            and vsol >= 45.0
            and b700 >= 6.5
            and u700 >= 6
            and top700 <= 0.30
            and b1500 >= 8.0
            and u1500 >= 7
            and top1500 <= 0.25
            and sellr <= 0.03
        ):
            continue
        post = [
            (int(y.get("ts_ms") or 0), float(y.get("curve_price") or 0.0))
            for y in rows
            if int(y.get("ts_ms") or 0) >= ts and int(y.get("ts_ms") or 0) <= ts + 60000 and float(y.get("curve_price") or 0.0) > 0
        ]
        if not post:
            continue
        mx_ts, mx = max(post, key=lambda z: z[1])
        mn_ts, mn = min(post, key=lambda z: z[1])
        rows_out.append((mx / price, m, mn / price, (mx_ts - ts) / 1000, (mn_ts - ts) / 1000, entry, vsol, b700, u700, top700, b1500, u1500, top1500, sellr))
        break

rows_out.sort(reverse=True)
print(f"live_breadth_breakout_profile={len(rows_out)}")
for r in rows_out:
    mx, m, mn, tmax, tmin, entry, vsol, b700, u700, top700, b1500, u1500, top1500, sellr = r
    print(
        f"{m[:8]} post={mx:.2f}x min={mn:.2f}x tmax={tmax:.1f}s tmin={tmin:.1f}s "
        f"entry={entry:.2f} vsol={vsol:.1f} b700={b700:.2f}/{u700} top={top700:.2f} "
        f"b1500={b1500:.2f}/{u1500} top15={top1500:.2f} sellr={sellr:.2f}"
    )
