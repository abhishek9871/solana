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
    m = x.get("mint")
    if run_start_ms and int(x.get("ts_ms") or 0) < run_start_ms:
        continue
    if not m:
        continue
    events[m].append(x)


def stats(rows, start, end):
    buy = sell = tracked_buy = 0.0
    buyers = defaultdict(float)
    tracked_buyers = set()
    for x in rows:
        ts = int(x.get("ts_ms") or 0)
        if ts < start or ts > end:
            continue
        sol = float(x.get("sol") or 0.0)
        user = x.get("user") or x.get("signer") or "?"
        if x.get("side") == "buy":
            buy += sol
            buyers[user] += sol
            if x.get("tracked"):
                tracked_buy += sol
                tracked_buyers.add(user)
        elif x.get("side") == "sell":
            sell += sol
    top = max(buyers.values()) / buy if buy > 0 and buyers else 1.0
    return buy, sell, len(buyers), top, tracked_buy, len(tracked_buyers)


candidates = []
for m, rows in events.items():
    rows.sort(key=lambda x: int(x.get("ts_ms") or 0))
    t0 = int(rows[0].get("ts_ms") or 0)
    first_price = None
    for x in rows:
        p = float(x.get("curve_price") or 0.0)
        ts = int(x.get("ts_ms") or 0)
        if p > 0:
            first_price = (ts, p, x)
            break
    if not first_price:
        continue
    fts, fp, fx = first_price
    if fts - t0 > 2500:
        continue
    buy, sell, uniq, top, tbuy, tuniq = stats(rows, t0, fts)
    sellr = sell / max(buy, 0.001)
    if not (
        tbuy >= 1.0
        and tuniq >= 1
        and buy >= 5.0
        and uniq >= 4
        and top <= 0.60
        and sellr <= 0.03
        and float(fx.get("vsol_sol") or 0.0) >= 30.0
    ):
        continue
    post = [
        (int(x.get("ts_ms") or 0), float(x.get("curve_price") or 0.0))
        for x in rows
        if int(x.get("ts_ms") or 0) >= fts and int(x.get("ts_ms") or 0) <= fts + 120000 and float(x.get("curve_price") or 0.0) > 0
    ]
    mx_ts, mx = max(post, key=lambda z: z[1]) if post else (fts, fp)
    mn_ts, mn = min(post, key=lambda z: z[1]) if post else (fts, fp)
    candidates.append((mx / fp, m, fts, fp, buy, uniq, top, tbuy, tuniq, sellr, float(fx.get("vsol_sol") or 0.0), mn / fp, (mx_ts - fts) / 1000, (mn_ts - fts) / 1000))

candidates.sort(reverse=True)
print(f"tracked_launch_candidates={len(candidates)}")
for r in candidates[:40]:
    mult, m, fts, fp, buy, uniq, top, tbuy, tuniq, sellr, vsol, minmult, tmax, tmin = r
    print(
        f"{m[:8]} max={mult:5.2f}x min={minmult:5.2f}x tmax={tmax:6.2f}s tmin={tmin:6.2f}s "
        f"launch={buy:.2f}/{uniq} top={top:.2f} tracked={tbuy:.2f}/{tuniq} sellr={sellr:.2f} vsol={vsol:.1f}"
    )
