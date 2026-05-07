import itertools
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


RUNID = sys.argv[1] if len(sys.argv) > 1 else "pgg2_direct_live_20260506_214938"
DEC = Path("/root/piggy/data") / f"{RUNID}_decisions.jsonl"


def fnum(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v):
            return default
        return v
    except Exception:
        return default


def short(m):
    return f"{m[:4]}..{m[-4:]}" if m else "?"


by_mint = defaultdict(dict)
for line in open(DEC):
    if not line.strip():
        continue
    x = json.loads(line)
    m = x.get("mint")
    if not m:
        continue
    k = x.get("kind")
    if k in {"strike_plan", "open", "close"}:
        by_mint[m][k] = x

trades = []
for m, d in by_mint.items():
    op = d.get("open")
    cl = d.get("close")
    if not op or not cl:
        continue
    f = op.get("features") or {}
    strike = d.get("strike_plan") or {}
    sf = strike.get("features") or {}
    pnl = fnum(cl.get("pnl_sol"), 0.0)
    row = {
        "mint": m,
        "short": short(m),
        "lane": op.get("lane") or strike.get("lane"),
        "reason": cl.get("reason"),
        "pnl": pnl,
        "win": pnl > 0,
        "hold_s": (cl.get("ts_ms", 0) - op.get("ts_ms", 0)) / 1000.0,
    }
    for key in [
        "age_ms",
        "buy700",
        "buy1500",
        "uniq700",
        "uniq1500",
        "top_share700",
        "top_share1500",
        "move700",
        "move1500",
        "sell700",
        "sell1500",
        "last_sell_age_ms",
        "score",
        "cluster_score",
        "vsol_sol",
        "first_buy_sol",
        "buyer_hhi700",
        "slot_buy_sol",
        "slot_buyers",
        "slot_top_share",
    ]:
        row[key] = fnum(f.get(key))
    for key in ["priced_snap_entry_move", "priced_snap_age_sec", "priced_snap_sell_ratio1500"]:
        row[key] = fnum(sf.get(key, f.get(key)))
    trades.append(row)

base = sum(t["pnl"] for t in trades)
base_w = sum(t["pnl"] for t in trades if t["pnl"] > 0)
base_l = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
base_w_count = sum(1 for t in trades if t["pnl"] > 0)
base_l_count = len(trades) - base_w_count

print(f"BASE trades={len(trades)} W/L={base_w_count}/{base_l_count} net={base:+.6f} grossW={base_w:+.6f} grossL={base_l:+.6f}")
print()


def eval_skip(name, pred):
    kept = [t for t in trades if not pred(t)]
    skipped = [t for t in trades if pred(t)]
    pnl = sum(t["pnl"] for t in kept)
    kept_w = sum(t["pnl"] for t in kept if t["pnl"] > 0)
    kept_l = sum(t["pnl"] for t in kept if t["pnl"] <= 0)
    sk_w = sum(t["pnl"] for t in skipped if t["pnl"] > 0)
    sk_l = sum(t["pnl"] for t in skipped if t["pnl"] <= 0)
    return {
        "name": name,
        "kept_n": len(kept),
        "skip_n": len(skipped),
        "net": pnl,
        "delta": pnl - base,
        "kept_gw": kept_w,
        "kept_gl": kept_l,
        "skipped_gw": sk_w,
        "skipped_gl": sk_l,
        "skipped_w_n": sum(1 for t in skipped if t["pnl"] > 0),
        "skipped_l_n": sum(1 for t in skipped if t["pnl"] <= 0),
        "kept_w_n": sum(1 for t in kept if t["pnl"] > 0),
        "kept_l_n": sum(1 for t in kept if t["pnl"] <= 0),
    }


def val(t, k):
    v = t.get(k)
    return v if v is not None else 0.0


candidates = []

features = [
    "age_ms",
    "buy700",
    "buy1500",
    "uniq700",
    "uniq1500",
    "top_share700",
    "top_share1500",
    "move700",
    "move1500",
    "last_sell_age_ms",
    "score",
    "cluster_score",
    "vsol_sol",
    "first_buy_sol",
    "buyer_hhi700",
    "slot_buy_sol",
    "slot_buyers",
    "slot_top_share",
    "priced_snap_entry_move",
    "priced_snap_age_sec",
]

for feat in features:
    vals = sorted({val(t, feat) for t in trades})
    if len(vals) < 4:
        continue
    pivots = sorted({vals[int((len(vals) - 1) * p)] for p in [0.1, 0.15, 0.2, 0.25, 0.33, 0.4, 0.5, 0.6, 0.67, 0.75, 0.8, 0.85, 0.9]})
    for th in pivots:
        candidates.append(eval_skip(f"skip {feat} < {th:.6g}", lambda t, feat=feat, th=th: val(t, feat) < th))
        candidates.append(eval_skip(f"skip {feat} > {th:.6g}", lambda t, feat=feat, th=th: val(t, feat) > th))

# Small hand-picked two-condition bad signatures.
conds = []
for feat in features:
    vals = sorted({val(t, feat) for t in trades})
    if len(vals) < 4:
        continue
    low = vals[int((len(vals) - 1) * 0.25)]
    high = vals[int((len(vals) - 1) * 0.75)]
    conds.append((f"{feat} < {low:.6g}", lambda t, feat=feat, low=low: val(t, feat) < low))
    conds.append((f"{feat} > {high:.6g}", lambda t, feat=feat, high=high: val(t, feat) > high))

for (n1, p1), (n2, p2) in itertools.combinations(conds, 2):
    candidates.append(eval_skip(f"skip ({n1}) AND ({n2})", lambda t, p1=p1, p2=p2: p1(t) and p2(t)))

good = []
for c in candidates:
    if c["skip_n"] == 0:
        continue
    # Preserve the behavior the user likes: most entries and almost all gross wins.
    kept_entry_frac = c["kept_n"] / len(trades)
    kept_win_frac = c["kept_gw"] / base_w if base_w else 1.0
    loss_cut_frac = (base_l - c["kept_gl"]) / abs(base_l) if base_l else 0.0
    c["kept_entry_frac"] = kept_entry_frac
    c["kept_win_frac"] = kept_win_frac
    c["loss_cut_frac"] = loss_cut_frac
    if kept_entry_frac >= 0.80 and kept_win_frac >= 0.90 and c["delta"] > 0:
        good.append(c)

print("BEST_SAFE_ENTRY_FILTERS preserve >=80% entries and >=90% gross wins")
for c in sorted(good, key=lambda x: (x["delta"], x["loss_cut_frac"]), reverse=True)[:25]:
    print(
        f"{c['name']:<70s} kept={c['kept_n']:2d} W/L={c['kept_w_n']:2d}/{c['kept_l_n']:2d} "
        f"net={c['net']:+.6f} delta={c['delta']:+.6f} "
        f"skipW={c['skipped_w_n']:2d}({c['skipped_gw']:+.6f}) skipL={c['skipped_l_n']:2d}({c['skipped_gl']:+.6f}) "
        f"entry={c['kept_entry_frac']:.0%} winPnL={c['kept_win_frac']:.0%} lossCut={c['loss_cut_frac']:.0%}"
    )
print()

print("NO-WINNER-CUT LOSS SKIPS")
no_win = [c for c in candidates if c["skipped_w_n"] == 0 and c["skipped_l_n"] > 0]
for c in sorted(no_win, key=lambda x: (x["skipped_l_n"], -x["skipped_gl"]), reverse=True)[:25]:
    print(
        f"{c['name']:<70s} skipped_losses={c['skipped_l_n']:2d} saved={-c['skipped_gl']:+.6f} kept={c['kept_n']:2d} net={c['net']:+.6f}"
    )
print()

print("TIME_SPLIT_CHECK top candidates on first half vs second half")
trades_sorted = sorted(trades, key=lambda t: t["mint"])  # deterministic fallback if no timestamps
# Re-read with open timestamps for a real split.
with_ts = []
for m, d in by_mint.items():
    if d.get("open") and d.get("close"):
        row = next(t for t in trades if t["mint"] == m)
        row = dict(row)
        row["open_ts"] = d["open"].get("ts_ms", 0)
        with_ts.append(row)
trades_sorted = sorted(with_ts, key=lambda t: t["open_ts"])
mid_ts = trades_sorted[len(trades_sorted)//2]["open_ts"]

def eval_on(subset, pred):
    b = sum(t["pnl"] for t in subset)
    kept = [t for t in subset if not pred(t)]
    return len(subset), sum(t["pnl"] for t in kept), sum(t["pnl"] for t in kept)-b, sum(1 for t in kept if t["pnl"]>0), sum(1 for t in kept if t["pnl"]<=0)

for c in sorted(good, key=lambda x: x["delta"], reverse=True)[:10]:
    name = c["name"]
    # Reconstruct only simple conditions printed by this script for time split not needed here.
    print(f"{name} overall_delta={c['delta']:+.6f} skippedW={c['skipped_w_n']} skippedL={c['skipped_l_n']}")

