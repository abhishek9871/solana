import glob
import json
import os
from collections import defaultdict


def load_trades(runid):
    dec = f"/root/piggy/data/{runid}_decisions.jsonl"
    if not os.path.exists(dec):
        return []
    by_mint = defaultdict(dict)
    for line in open(dec):
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
            by_mint[m][x.get("kind")] = x
    trades = []
    for m, d in by_mint.items():
        if "open" not in d or "close" not in d:
            continue
        op = d["open"]
        cl = d["close"]
        f = op.get("features") or {}
        try:
            pnl = float(cl.get("pnl_sol") or 0)
        except Exception:
            pnl = 0.0
        trades.append(
            {
                "mint": m,
                "lane": op.get("lane") or (d.get("strike_plan") or {}).get("lane"),
                "reason": cl.get("reason"),
                "pnl": pnl,
                "uniq1500": f.get("uniq1500") or 0,
                "buy1500": f.get("buy1500") or 0,
                "score": f.get("score") or 0,
                "last_sell_age_ms": f.get("last_sell_age_ms") or 0,
                "slot_buyers": f.get("slot_buyers") or 0,
                "cluster_score": f.get("cluster_score") or 0,
                "first_buy_sol": f.get("first_buy_sol") or 0,
            }
        )
    return trades


candidates = [
    ("uniq1500_lt_6", lambda t: t["uniq1500"] < 6),
    ("buy1500_lt_10_score_lt_150", lambda t: t["buy1500"] < 10.4422 and t["score"] < 150.17),
    ("last_sell_old_slot_buyers_lt_6", lambda t: t["last_sell_age_ms"] > 2551 and t["slot_buyers"] < 6),
    ("cluster_low_first_buy_high", lambda t: t["cluster_score"] < 189.606 and t["first_buy_sol"] > 4.4),
]

runids = []
for path in glob.glob("/root/piggy/data/pgg2_direct_live_*_decisions.jsonl"):
    runids.append(os.path.basename(path).replace("_decisions.jsonl", ""))
runids = sorted(set(runids))

print(f"runs={len(runids)}")
for runid in runids:
    trades = load_trades(runid)
    if not trades:
        continue
    base = sum(t["pnl"] for t in trades)
    w = sum(1 for t in trades if t["pnl"] > 0)
    l = len(trades) - w
    print(f"\n{runid} trades={len(trades)} W/L={w}/{l} base={base:+.6f}")
    for name, pred in candidates:
        kept = [t for t in trades if not pred(t)]
        skipped = [t for t in trades if pred(t)]
        new = sum(t["pnl"] for t in kept)
        sw = sum(1 for t in skipped if t["pnl"] > 0)
        sl = len(skipped) - sw
        sgw = sum(t["pnl"] for t in skipped if t["pnl"] > 0)
        sgl = sum(t["pnl"] for t in skipped if t["pnl"] <= 0)
        print(f"  {name:30s} new={new:+.6f} delta={new-base:+.6f} skipped={len(skipped):2d} skipW={sw:2d}({sgw:+.6f}) skipL={sl:2d}({sgl:+.6f})")

