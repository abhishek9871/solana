import json
import sys
from collections import defaultdict

BASE = "/root/piggy"
runid = sys.argv[1]
raw_path = f"{BASE}/data/{runid}_raw.jsonl"
dec_path = f"{BASE}/data/{runid}_decisions.jsonl"
state_path = f"{BASE}/data/{runid}_state.json"
run_start_ms = 0
try:
    run_start_ms = int(float(json.load(open(state_path))["session"]["started_at"]) * 1000)
except Exception:
    pass

opened = set()
closed = set()
for line in open(dec_path):
    if not line.strip():
        continue
    x = json.loads(line)
    if x.get("kind") == "open":
        opened.add(x.get("mint"))
    elif x.get("kind") == "close":
        closed.add(x.get("mint"))

first = {}
maxp = {}
maxt = {}
last = {}
counts = defaultdict(int)
for line in open(raw_path):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    m = x.get("mint")
    if not m:
        continue
    counts[x.get("kind", "?")] += 1
    p = float(x.get("curve_price") or x.get("price") or 0.0)
    ts = int(x.get("ts_ms") or x.get("ts") or 0)
    if run_start_ms and ts < run_start_ms:
        continue
    if p <= 0 or ts <= 0:
        continue
    if m not in first:
        first[m] = (ts, p)
    last[m] = (ts, p)
    if p > maxp.get(m, 0.0):
        maxp[m] = p
        maxt[m] = ts

rows = []
for m, (fts, fp) in first.items():
    mx = maxp.get(m, 0.0)
    if fp <= 0 or mx <= 0:
        continue
    mult = mx / fp
    if mult >= 1.5:
        rows.append((mult, m, fts, maxt.get(m, fts), fp, mx, m in opened))
rows.sort(reverse=True)

print(f"raw_kind_counts={dict(counts)} opened={len(opened)} closed={len(closed)}")
print(f"mints_with_1.5x+={len(rows)} 2x+={sum(1 for r in rows if r[0] >= 2)} 3x+={sum(1 for r in rows if r[0] >= 3)}")
print(f"missed_2x+={sum(1 for r in rows if r[0] >= 2 and not r[6])} captured_2x+={sum(1 for r in rows if r[0] >= 2 and r[6])}")
print("top missed/captured moves by raw first->max curve price:")
for mult, m, fts, mts, fp, mx, was_opened in rows[:25]:
    label = "CAPTURED" if was_opened else "MISSED"
    dt = (mts - fts) / 1000
    print(f"{label:8s} {m[:8]} mult={mult:6.2f}x dt={dt:6.2f}s first={fp:.10g} max={mx:.10g}")
