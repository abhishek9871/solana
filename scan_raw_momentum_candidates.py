import json
import sys
from collections import defaultdict, deque

BASE = "/root/piggy"
runid = sys.argv[1]
raw_path = f"{BASE}/data/{runid}_raw.jsonl"
state_path = f"{BASE}/data/{runid}_state.json"
run_start_ms = 0
try:
    run_start_ms = int(float(json.load(open(state_path))["session"]["started_at"]) * 1000)
except Exception:
    pass

events_by_mint = defaultdict(list)
rows = []
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
    ts = int(x.get("ts_ms") or 0)
    if run_start_ms and ts < run_start_ms:
        continue
    p = float(x.get("curve_price") or 0.0)
    if not m or ts <= 0:
        continue
    rows.append(x)
    events_by_mint[m].append(x)


def stats(mint, start, end):
    buy = sell = 0.0
    buyers = defaultdict(float)
    for x in events_by_mint[mint]:
        ts = int(x.get("ts_ms") or 0)
        if ts < start or ts > end:
            continue
        sol = float(x.get("sol") or 0.0)
        if x.get("side") == "buy":
            buy += sol
            buyers[x.get("user") or x.get("signer") or "?"] += sol
        elif x.get("side") == "sell":
            sell += sol
    top = max(buyers.values()) / buy if buy > 0 and buyers else 1.0
    return buy, sell, len(buyers), top


first_price = {}
arms = {}
candidates = []
seen = set()

for x in rows:
    m = x["mint"]
    ts = int(x.get("ts_ms") or 0)
    p = float(x.get("curve_price") or 0.0)
    if p <= 0:
        continue
    first_price.setdefault(m, p)
    if m in seen:
        continue
    if x.get("side") != "buy":
        continue
    fp = first_price[m]
    entry_move = p / fp if fp > 0 else 0
    vsol = float(x.get("vsol_sol") or 0.0)
    buy10, sell10, uniq10, top10 = stats(m, ts - 10000, ts)
    sellr10 = sell10 / max(buy10, 0.001)
    arm = arms.get(m)
    if not arm:
        if (
            buy10 >= 8.0
            and uniq10 >= 8
            and 0.25 <= top10 <= 0.35
            and sellr10 <= 0.15
            and 1.15 <= entry_move <= 1.70
            and vsol >= 35.0
        ):
            arms[m] = (ts, p, buy10, sell10, uniq10, top10, sellr10, entry_move, vsol)
        continue
    arm_ts, arm_price, abuy, asell, auniq, atop, asellr, aentry_move, avsol = arm
    age = ts - arm_ts
    if age > 6500:
        seen.add(m)
        continue
    buy700, sell700, uniq700, top700 = stats(m, ts - 700, ts)
    buy1500, sell1500, uniq1500, top1500 = stats(m, ts - 1500, ts)
    confirm = p / arm_price if arm_price > 0 else 0
    if (
        confirm >= 1.10
        and (sell700 / max(buy700, 0.001)) <= 0.10
        and uniq700 >= 3
        and buy1500 >= 3.5
    ):
        candidates.append((m, ts, p, age, confirm, abuy, auniq, atop, asellr, aentry_move, avsol, buy700, uniq700, top700))
        seen.add(m)

print(f"raw_momentum_candidates={len(candidates)}")
for c in candidates[:50]:
    m, ts, ep, age, confirm, abuy, auniq, atop, asellr, entry_move, vsol, b700, u700, top700 = c
    post = [
        (int(x.get("ts_ms") or 0), float(x.get("curve_price") or 0.0))
        for x in events_by_mint[m]
        if int(x.get("ts_ms") or 0) >= ts and int(x.get("ts_ms") or 0) <= ts + 60000 and float(x.get("curve_price") or 0.0) > 0
    ]
    mx_ts, mx = max(post, key=lambda z: z[1]) if post else (ts, ep)
    mn_ts, mn = min(post, key=lambda z: z[1]) if post else (ts, ep)
    print(
        f"{m[:8]} post_max={mx/ep:5.2f}x post_min={mn/ep:5.2f}x "
        f"tmax={(mx_ts-ts)/1000:5.2f}s tmin={(mn_ts-ts)/1000:5.2f}s "
        f"age={age}ms confirm={confirm:.2f} arm={abuy:.1f}/{auniq} top={atop:.2f} "
        f"sellr={asellr:.2f} entry={entry_move:.2f} vsol={vsol:.1f} live700={b700:.1f}/{u700} top={top700:.2f}"
    )
