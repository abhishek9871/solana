import glob
import json
import os
from collections import defaultdict


def f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def load(path):
    by = defaultdict(dict)
    for line in open(path):
        if not line.strip():
            continue
        try:
            x = json.loads(line)
        except Exception:
            continue
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
        row = {"pnl": f(d["close"].get("pnl_sol")), "lane": d["open"].get("lane") or (d.get("strike_plan") or {}).get("lane")}
        for k in ["age_ms","move1500","move700","score","top_share1500","slot_top_share","slot_buyers","buy1500","uniq1500","first_buy_sol","priced_snap_entry_move"]:
            row[k] = f(of.get(k, sf.get(k)))
        for k in ["priced_snap_entry_move", "priced_snap_age_sec"]:
            row[k] = f(sf.get(k, of.get(k)))
        rows.append(row)
    return rows


rules = [
    ("R1_late_flat", lambda r: r["age_ms"] > 2200 and r["move1500"] < 1.05),
    ("R2_top_mismatch", lambda r: r["top_share1500"] > 0.23 and 0 < r["slot_top_share"] < 0.26),
    ("R3_dead_flat_score", lambda r: r["move1500"] < 1.01 and r["score"] > 93),
    ("R4_uniq1500_lt6", lambda r: r["uniq1500"] < 6),
    ("R5_lowbuy_lowscore", lambda r: r["buy1500"] < 10.5 and r["score"] < 150),
]

combos = [
    ("R1", ["R1_late_flat"]),
    ("R1_OR_R2", ["R1_late_flat", "R2_top_mismatch"]),
    ("R1_OR_R2_OR_R3", ["R1_late_flat", "R2_top_mismatch", "R3_dead_flat_score"]),
    ("R4", ["R4_uniq1500_lt6"]),
    ("R4_OR_R5", ["R4_uniq1500_lt6", "R5_lowbuy_lowscore"]),
    ("ALL_FIVE", [r[0] for r in rules]),
]
rule_map = dict(rules)

paths = sorted(glob.glob("/root/piggy/data/pgg2*_decisions.jsonl"))
print(f"files={len(paths)}")
totals = {name: {"base":0.0,"new":0.0,"n":0,"skip":0,"sw":0,"sl":0,"skipw":0.0,"skipl":0.0} for name,_ in combos}
for path in paths:
    rows = load(path)
    if len(rows) < 1:
        continue
    run = os.path.basename(path).replace("_decisions.jsonl","")
    base = sum(r["pnl"] for r in rows)
    w = sum(r["pnl"] > 0 for r in rows)
    l = len(rows) - w
    if len(rows) < 5 and "214938" not in run:
        # Print only meaningful small runs if they have an effect.
        pass
    print(f"\n{run} n={len(rows)} W/L={w}/{l} base={base:+.6f}")
    for cname, rnames in combos:
        skip = [r for r in rows if any(rule_map[n](r) for n in rnames)]
        new = sum(r["pnl"] for r in rows if r not in skip)
        sw = sum(r["pnl"] > 0 for r in skip)
        sl = len(skip) - sw
        skipw = sum(r["pnl"] for r in skip if r["pnl"] > 0)
        skipl = sum(r["pnl"] for r in skip if r["pnl"] <= 0)
        totals[cname]["base"] += base
        totals[cname]["new"] += new
        totals[cname]["n"] += len(rows)
        totals[cname]["skip"] += len(skip)
        totals[cname]["sw"] += sw
        totals[cname]["sl"] += sl
        totals[cname]["skipw"] += skipw
        totals[cname]["skipl"] += skipl
        print(f"  {cname:16s} new={new:+.6f} delta={new-base:+.6f} skip={len(skip):2d} skipW={sw:2d}({skipw:+.6f}) skipL={sl:2d}({skipl:+.6f})")

print("\nTOTALS")
for cname, t in totals.items():
    print(f"{cname:16s} base={t['base']:+.6f} new={t['new']:+.6f} delta={t['new']-t['base']:+.6f} n={t['n']} skip={t['skip']} skipW={t['sw']}({t['skipw']:+.6f}) skipL={t['sl']}({t['skipl']:+.6f})")

