import json
import sys
from collections import defaultdict
from pathlib import Path


RUNID = sys.argv[1] if len(sys.argv) > 1 else "pgg2_direct_live_20260506_214938"
BASE = Path("/root/piggy/data")
DEC = BASE / f"{RUNID}_decisions.jsonl"
RAW = BASE / f"{RUNID}_raw.jsonl"


def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


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
    if x.get("kind") in {"open", "close", "strike_plan"}:
        by[m][x.get("kind")] = x

trades = {}
for m, d in by.items():
    op = d.get("open")
    cl = d.get("close")
    if not op or not cl:
        continue
    of = op.get("features") or {}
    cf = cl.get("features") or {}
    entry_price = fnum(of.get("price"))
    close_price = fnum(cf.get("price"))
    if entry_price <= 0:
        continue
    trades[m] = {
        "mint": m,
        "short": short(m),
        "lane": op.get("lane") or (d.get("strike_plan") or {}).get("lane"),
        "reason": cl.get("reason"),
        "pnl": fnum(cl.get("pnl_sol")),
        "entry_ts": op.get("ts_ms", 0),
        "close_ts": cl.get("ts_ms", 0),
        "entry_price": entry_price,
        "close_price": close_price,
        "max_before_close": entry_price,
        "max_after_close_60s": 0.0,
        "max_after_close_60s_ts": 0,
        "max_after_entry_60s": entry_price,
        "max_after_entry_60s_ts": op.get("ts_ms", 0),
        "min_after_entry_to_close": entry_price,
        "events": 0,
    }

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
    cp = fnum(x.get("curve_price"))
    if cp <= 0:
        continue
    ts = x.get("ts_ms", 0)
    if ts < t["entry_ts"]:
        continue
    if ts > t["entry_ts"] + 60000:
        continue
    t["events"] += 1
    if cp > t["max_after_entry_60s"]:
        t["max_after_entry_60s"] = cp
        t["max_after_entry_60s_ts"] = ts
    if ts <= t["close_ts"]:
        if cp > t["max_before_close"]:
            t["max_before_close"] = cp
        if cp < t["min_after_entry_to_close"]:
            t["min_after_entry_to_close"] = cp
    else:
        if cp > t["max_after_close_60s"]:
            t["max_after_close_60s"] = cp
            t["max_after_close_60s_ts"] = ts

rows = list(trades.values())
for r in rows:
    ep = r["entry_price"]
    r["max_before_mult"] = r["max_before_close"] / ep if ep else 0
    r["max_after_exit_mult"] = r["max_after_close_60s"] / ep if ep and r["max_after_close_60s"] else 0
    r["max_60_mult"] = r["max_after_entry_60s"] / ep if ep else 0
    r["min_to_close_mult"] = r["min_after_entry_to_close"] / ep if ep else 0

losses = [r for r in rows if r["pnl"] <= 0]
wins = [r for r in rows if r["pnl"] > 0]

print(f"trades_with_price={len(rows)} wins={len(wins)} losses={len(losses)}")
for group_name, group in [("LOSSES", losses), ("WINS", wins)]:
    print()
    print(group_name)
    for floor in [1.2, 1.5, 2.0, 3.0]:
        n_before = sum(1 for r in group if r["max_before_mult"] >= floor)
        n_after = sum(1 for r in group if r["max_after_exit_mult"] >= floor)
        n_60 = sum(1 for r in group if r["max_60_mult"] >= floor)
        print(f">={floor:.1f}x before_close={n_before} after_exit_60s={n_after} any_60s={n_60}")

print()
print("LOSSES_THAT_LATER_RAN")
later = [r for r in losses if r["max_after_exit_mult"] >= 1.2 or r["max_60_mult"] >= 1.5]
for r in sorted(later, key=lambda x: x["max_60_mult"], reverse=True)[:25]:
    print(
        f"{r['short']:12s} lane={r['lane']:16s} reason={r['reason']:34s} pnl={r['pnl']:+.6f} "
        f"preMax={r['max_before_mult']:.2f} postMax={r['max_after_exit_mult']:.2f} any60={r['max_60_mult']:.2f} "
        f"tPostPeak={(r['max_after_close_60s_ts']-r['close_ts'])/1000:.1f}s "
        f"minToClose={r['min_to_close_mult']:.2f} hold={(r['close_ts']-r['entry_ts'])/1000:.2f}s events={r['events']}"
    )

print()
print("WINS_LEFT_ON_TABLE")
left = [r for r in wins if r["max_after_exit_mult"] >= max(1.5, r["max_before_mult"] * 1.25)]
for r in sorted(left, key=lambda x: x["max_after_exit_mult"], reverse=True)[:25]:
    print(
        f"{r['short']:12s} lane={r['lane']:16s} reason={r['reason']:34s} pnl={r['pnl']:+.6f} "
        f"preMax={r['max_before_mult']:.2f} postMax={r['max_after_exit_mult']:.2f} any60={r['max_60_mult']:.2f} "
        f"tPostPeak={(r['max_after_close_60s_ts']-r['close_ts'])/1000:.1f}s hold={(r['close_ts']-r['entry_ts'])/1000:.2f}s"
    )

print()
print("LOSS_RECOVERY_FEATURES")
recover = [r for r in losses if r["max_after_exit_mult"] >= 1.5]
dead = [r for r in losses if r["max_after_exit_mult"] < 1.2]
full_by_mint = {}
for line in open(DEC):
    if not line.strip():
        continue
    x = json.loads(line)
    if x.get("kind") == "open":
        full_by_mint[x.get("mint")] = x.get("features") or {}
keys = ["buy700", "buy1500", "uniq700", "uniq1500", "top_share700", "top_share1500", "score", "cluster_score", "last_sell_age_ms", "move700", "move1500", "vsol_sol", "first_buy_sol", "slot_buyers"]
def med(group, key):
    vals = []
    for r in group:
        v = full_by_mint.get(r["mint"], {}).get(key)
        try:
            vals.append(float(v))
        except Exception:
            pass
    if not vals:
        return None
    vals.sort()
    return vals[len(vals)//2]
print(f"recover_losses_n={len(recover)} dead_losses_n={len(dead)}")
for key in keys:
    print(f"{key:16s} recover_med={med(recover,key)} dead_med={med(dead,key)}")
