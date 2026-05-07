import itertools
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


RUNID = sys.argv[1] if len(sys.argv) > 1 else "pgg2_direct_live_20260506_214938"
DEC = Path("/root/piggy/data") / f"{RUNID}_decisions.jsonl"


def f(x, d=0.0):
    try:
        if x is None:
            return d
        v = float(x)
        if math.isnan(v):
            return d
        return v
    except Exception:
        return d


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

rows = []
for m, d in by.items():
    if "open" not in d or "close" not in d:
        continue
    of = d["open"].get("features") or {}
    sf = (d.get("strike_plan") or {}).get("features") or {}
    row = {
        "mint": m,
        "lane": d["open"].get("lane") or (d.get("strike_plan") or {}).get("lane") or "?",
        "reason": d["close"].get("reason"),
        "pnl": f(d["close"].get("pnl_sol")),
    }
    for k in [
        "age_ms", "buy700", "buy1500", "uniq700", "uniq1500", "top_share700", "top_share1500",
        "move700", "move1500", "sell700", "sell1500", "last_sell_age_ms", "score", "cluster_score",
        "vsol_sol", "first_buy_sol", "buyer_hhi700", "slot_buy_sol", "slot_buyers", "slot_top_share",
    ]:
        row[k] = f(of.get(k))
    for k in ["priced_snap_entry_move", "priced_snap_age_sec", "priced_snap_sell_ratio1500"]:
        row[k] = f(sf.get(k, of.get(k)))
    rows.append(row)

base = sum(r["pnl"] for r in rows)
gross_win = sum(r["pnl"] for r in rows if r["pnl"] > 0)
gross_loss = sum(r["pnl"] for r in rows if r["pnl"] <= 0)
print(f"BASE n={len(rows)} W/L={sum(r['pnl']>0 for r in rows)}/{sum(r['pnl']<=0 for r in rows)} net={base:+.6f} grossW={gross_win:+.6f} grossL={gross_loss:+.6f}")

features = [
    "age_ms", "buy700", "buy1500", "uniq700", "uniq1500", "top_share700", "top_share1500",
    "move700", "move1500", "last_sell_age_ms", "score", "cluster_score", "vsol_sol",
    "first_buy_sol", "buyer_hhi700", "slot_buy_sol", "slot_buyers", "slot_top_share",
    "priced_snap_entry_move", "priced_snap_age_sec", "priced_snap_sell_ratio1500",
]

conds = []
for feat in features:
    vals = sorted(set(r.get(feat, 0.0) for r in rows))
    if len(vals) < 6:
        continue
    for p in [0.10,0.15,0.20,0.25,0.33,0.40,0.50,0.60,0.67,0.75,0.80,0.85,0.90]:
        th = vals[int((len(vals)-1)*p)]
        conds.append((f"{feat} < {th:.6g}", frozenset(i for i, r in enumerate(rows) if r.get(feat,0.0) < th)))
        conds.append((f"{feat} > {th:.6g}", frozenset(i for i, r in enumerate(rows) if r.get(feat,0.0) > th)))

# Add pair intersections as atomic rules, since many useful signatures are two-feature.
atomic = list(conds)
for (n1,s1),(n2,s2) in itertools.combinations(conds, 2):
    inter = s1 & s2
    if 2 <= len(inter) <= 20:
        atomic.append((f"({n1}) AND ({n2})", frozenset(inter)))

# Keep only atoms that are individually not obviously bad.
filtered = []
for name, sidx in atomic:
    if not sidx:
        continue
    win_pnl_cut = sum(rows[i]["pnl"] for i in sidx if rows[i]["pnl"] > 0)
    loss_cut = sum(rows[i]["pnl"] for i in sidx if rows[i]["pnl"] <= 0)
    if loss_cut < 0 and (win_pnl_cut <= 0.01 or -loss_cut > win_pnl_cut * 2):
        filtered.append((name, sidx))

# Deduplicate same selected set.
dedup = {}
for name, sidx in filtered:
    if sidx not in dedup or len(name) < len(dedup[sidx]):
        dedup[sidx] = name
atoms = [(name, sidx) for sidx, name in dedup.items()]

def score_set(skip):
    kept = [r for i, r in enumerate(rows) if i not in skip]
    skipped = [r for i, r in enumerate(rows) if i in skip]
    new = sum(r["pnl"] for r in kept)
    skip_win = sum(r["pnl"] for r in skipped if r["pnl"] > 0)
    skip_loss = sum(r["pnl"] for r in skipped if r["pnl"] <= 0)
    return {
        "new": new,
        "delta": new - base,
        "skip_n": len(skipped),
        "skip_w_n": sum(r["pnl"] > 0 for r in skipped),
        "skip_l_n": sum(r["pnl"] <= 0 for r in skipped),
        "skip_win": skip_win,
        "skip_loss": skip_loss,
        "keep_entry_frac": len(kept) / len(rows),
        "keep_win_frac": (gross_win - skip_win) / gross_win if gross_win else 1,
    }

# Keep the combinatorics bounded: rank individual atoms, then combine only the useful frontier.
ranked_atoms = []
for name, sidx in atoms:
    sc = score_set(sidx)
    # Useful atoms save meaningful loss and do not burn much winner PnL.
    if sc["skip_loss"] < 0 and sc["skip_win"] <= 0.015 and sc["delta"] > -0.005:
        ranked_atoms.append((sc["delta"] - max(0.0, sc["skip_win"]) * 2 + (-sc["skip_loss"]) * 0.1, name, sidx))
ranked_atoms = sorted(ranked_atoms, reverse=True)[:80]
atoms = [(name, sidx) for _, name, sidx in ranked_atoms]

results = []
for k in [1, 2, 3]:
    for combo in itertools.combinations(range(len(atoms)), k):
        skip = frozenset().union(*(atoms[i][1] for i in combo))
        sc = score_set(skip)
        if sc["delta"] <= 0:
            continue
        if sc["keep_entry_frac"] < 0.80:
            continue
        if sc["keep_win_frac"] < 0.95:
            continue
        names = [atoms[i][0] for i in combo]
        results.append((sc["delta"], sc, names))

print("\nBEST_COMBO_ENTRY_ONLY keep >=80% entries, >=95% win pnl")
for delta, sc, names in sorted(results, key=lambda x: x[0], reverse=True)[:40]:
    print(
        f"delta={delta:+.6f} new={sc['new']:+.6f} skip={sc['skip_n']:2d} "
        f"skipW={sc['skip_w_n']:2d}({sc['skip_win']:+.6f}) skipL={sc['skip_l_n']:2d}({sc['skip_loss']:+.6f}) "
        f"keepEntry={sc['keep_entry_frac']:.0%} keepWinPnl={sc['keep_win_frac']:.0%}"
    )
    for n in names:
        print(f"  - {n}")
