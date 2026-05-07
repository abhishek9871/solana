import itertools
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


RUNID = sys.argv[1] if len(sys.argv) > 1 else "pgg2_direct_live_20260506_214938"
BASE = Path("/root/piggy")
DEC = BASE / "data" / f"{RUNID}_decisions.jsonl"
RAW = BASE / "data" / f"{RUNID}_raw.jsonl"
LOG = BASE / "logs" / f"{RUNID}.log"


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


def sh(m):
    return f"{m[:4]}..{m[-4:]}" if m else "?"


by = defaultdict(dict)
for line in open(DEC):
    if not line.strip():
        continue
    x = json.loads(line)
    m = x.get("mint")
    if not m:
        continue
    if x.get("kind") in {"wave_arm", "strike_plan", "open", "close"}:
        by[m][x.get("kind")] = x

trades = {}
short_to_full = {}
for m, d in by.items():
    if "open" not in d or "close" not in d:
        continue
    op, cl = d["open"], d["close"]
    of = op.get("features") or {}
    sf = (d.get("strike_plan") or {}).get("features") or {}
    ep = f(of.get("price"))
    row = {
        "mint": m,
        "short": sh(m),
        "lane": op.get("lane") or (d.get("strike_plan") or {}).get("lane") or "?",
        "reason": cl.get("reason"),
        "pnl": f(cl.get("pnl_sol")),
        "win": f(cl.get("pnl_sol")) > 0,
        "open_ts": op.get("ts_ms", 0),
        "close_ts": cl.get("ts_ms", 0),
        "entry_price": ep,
        "hold_s": (cl.get("ts_ms", 0) - op.get("ts_ms", 0)) / 1000.0,
        "raw_post_max": 0.0,
        "raw_pre_max": ep,
        "raw_pre_min": ep,
        "raw_0_1s_buy": 0.0,
        "raw_0_1s_sell": 0.0,
        "raw_1_3s_buy": 0.0,
        "raw_1_3s_sell": 0.0,
        "raw_to_close_buy": 0.0,
        "raw_to_close_sell": 0.0,
        "raw_buys_to_close": 0,
        "raw_sells_to_close": 0,
        "raw_unique_buyers_to_close": set(),
        "raw_unique_sellers_to_close": set(),
        "raw_first_700_net": 0.0,
        "raw_first_1500_net": 0.0,
    }
    for k in [
        "age_ms", "buy700", "buy1500", "uniq700", "uniq1500", "top_share700", "top_share1500",
        "move700", "move1500", "sell700", "sell1500", "last_sell_age_ms", "score", "cluster_score",
        "vsol_sol", "first_buy_sol", "buyer_hhi700", "slot_buy_sol", "slot_buyers", "slot_top_share",
    ]:
        row[k] = f(of.get(k))
    for k in ["priced_snap_entry_move", "priced_snap_age_sec", "priced_snap_sell_ratio1500"]:
        row[k] = f(sf.get(k, of.get(k)))
    trades[m] = row
    short_to_full[row["short"]] = m

rx_buy = re.compile(r"PGG2-LIVE-BUY\s+(\S+).*cost=([0-9.]+).*wallet_delta=([-+0-9.]+)")
rx_sell = re.compile(r"PGG2-LIVE-SELL\s+(\S+).*proceeds=([0-9.]+).*mult=([0-9.]+).*pnl=([-+0-9.]+)")
rx_qbuy = re.compile(r"PGG2-DIRECT-QUOTE BUY\s+(\S+).* route=\S+ in=([0-9.]+) out=.* fee_bps=([0-9.]+) fee=([0-9.]+)")
for line in open(LOG, errors="replace"):
    m = rx_buy.search(line)
    if m and m.group(1) in short_to_full:
        t = trades[short_to_full[m.group(1)]]
        t["buy_cost"] = f(m.group(2))
    m = rx_sell.search(line)
    if m and m.group(1) in short_to_full:
        t = trades[short_to_full[m.group(1)]]
        t["sell_proceeds"] = f(m.group(2))
        t["close_mult_log"] = f(m.group(3), 1.0)
    m = rx_qbuy.search(line)
    if m and m.group(1) in short_to_full:
        t = trades[short_to_full[m.group(1)]]
        t["trade_input"] = f(m.group(2))

for line in open(RAW):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    t = trades.get(x.get("mint"))
    if not t:
        continue
    ts = x.get("ts_ms", 0)
    if ts < t["open_ts"] or ts > t["close_ts"] + 60000:
        continue
    sol = abs(f(x.get("sol")))
    side = x.get("side") or x.get("instruction_kind")
    cp = f(x.get("curve_price"))
    rel = ts - t["open_ts"]
    if t["entry_price"] > 0 and cp > 0:
        if ts <= t["close_ts"]:
            t["raw_pre_max"] = max(t["raw_pre_max"], cp)
            t["raw_pre_min"] = min(t["raw_pre_min"], cp)
        elif cp / t["entry_price"] > t["raw_post_max"]:
            t["raw_post_max"] = cp / t["entry_price"]
    if ts <= t["close_ts"]:
        if side == "buy":
            t["raw_to_close_buy"] += sol
            t["raw_buys_to_close"] += 1
            if x.get("user"):
                t["raw_unique_buyers_to_close"].add(x.get("user"))
            if rel <= 1000:
                t["raw_0_1s_buy"] += sol
            elif rel <= 3000:
                t["raw_1_3s_buy"] += sol
            if rel <= 700:
                t["raw_first_700_net"] += sol
            if rel <= 1500:
                t["raw_first_1500_net"] += sol
        elif side == "sell":
            t["raw_to_close_sell"] += sol
            t["raw_sells_to_close"] += 1
            if x.get("user"):
                t["raw_unique_sellers_to_close"].add(x.get("user"))
            if rel <= 1000:
                t["raw_0_1s_sell"] += sol
            elif rel <= 3000:
                t["raw_1_3s_sell"] += sol
            if rel <= 700:
                t["raw_first_700_net"] -= sol
            if rel <= 1500:
                t["raw_first_1500_net"] -= sol

rows = list(trades.values())
for t in rows:
    t["raw_to_close_net"] = t["raw_to_close_buy"] - t["raw_to_close_sell"]
    t["raw_0_1s_net"] = t["raw_0_1s_buy"] - t["raw_0_1s_sell"]
    t["raw_1_3s_net"] = t["raw_1_3s_buy"] - t["raw_1_3s_sell"]
    t["raw_unique_buyers_n"] = len(t["raw_unique_buyers_to_close"])
    t["raw_unique_sellers_n"] = len(t["raw_unique_sellers_to_close"])
    t["pre_runup_mult"] = t["raw_pre_max"] / t["entry_price"] if t["entry_price"] else 1.0
    t["pre_drawdown_mult"] = t["raw_pre_min"] / t["entry_price"] if t["entry_price"] else 1.0
    t["recover_15"] = (t["pnl"] <= 0 and t["raw_post_max"] >= 1.5)
    t["dead_loss"] = (t["pnl"] <= 0 and t["raw_post_max"] < 1.2)
    # Sets are not useful downstream.
    del t["raw_unique_buyers_to_close"]
    del t["raw_unique_sellers_to_close"]

base = sum(t["pnl"] for t in rows)
base_loss = sum(t["pnl"] for t in rows if t["pnl"] <= 0)
base_win = sum(t["pnl"] for t in rows if t["pnl"] > 0)
print(f"BASE n={len(rows)} W/L={sum(t['pnl']>0 for t in rows)}/{sum(t['pnl']<=0 for t in rows)} net={base:+.6f} grossW={base_win:+.6f} grossL={base_loss:+.6f}")
print()

print("BY_TRADE_INPUT_SIZE")
for size in sorted(set(round(t.get("trade_input", 0), 6) for t in rows)):
    g = [t for t in rows if round(t.get("trade_input", 0), 6) == size]
    print(f"input={size:.6f} n={len(g):2d} W/L={sum(t['pnl']>0 for t in g):2d}/{sum(t['pnl']<=0 for t in g):2d} net={sum(t['pnl'] for t in g):+.6f} avg={sum(t['pnl'] for t in g)/len(g):+.6f}")
print()

groups = {
    "WIN": [t for t in rows if t["pnl"] > 0],
    "DEAD_LOSS_post<1.2": [t for t in rows if t["dead_loss"]],
    "RECOVER_LOSS_post>=1.5": [t for t in rows if t["recover_15"]],
    "OTHER_LOSS": [t for t in rows if t["pnl"] <= 0 and not t["dead_loss"] and not t["recover_15"]],
}
features = [
    "trade_input", "buy700", "buy1500", "uniq700", "uniq1500", "top_share700", "top_share1500", "move700", "move1500",
    "score", "cluster_score", "slot_buy_sol", "slot_buyers", "raw_0_1s_net", "raw_1_3s_net", "raw_to_close_net",
    "raw_buys_to_close", "raw_sells_to_close", "raw_unique_buyers_n", "raw_unique_sellers_n", "pre_runup_mult",
    "pre_drawdown_mult", "close_mult_log", "hold_s",
]

def med(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[len(vals)//2]

print("GROUP_MEDIANS")
for feat in features:
    parts = []
    for name, g in groups.items():
        parts.append(f"{name}={med([t.get(feat) for t in g])}")
    print(f"{feat:22s} " + " ".join(parts))
print()

def eval_condition(name, pred, action="skip", factor=0.5):
    selected = [t for t in rows if pred(t)]
    if not selected:
        return None
    if action == "skip":
        new = sum(t["pnl"] for t in rows if not pred(t))
    else:
        new = sum((t["pnl"] * factor if pred(t) else t["pnl"]) for t in rows)
    skipped_w = sum(1 for t in selected if t["pnl"] > 0)
    skipped_l = sum(1 for t in selected if t["pnl"] <= 0)
    sel_win_pnl = sum(t["pnl"] for t in selected if t["pnl"] > 0)
    sel_loss_pnl = sum(t["pnl"] for t in selected if t["pnl"] <= 0)
    return {
        "name": name, "selected": len(selected), "new": new, "delta": new - base,
        "w": skipped_w, "l": skipped_l, "sel_win_pnl": sel_win_pnl, "sel_loss_pnl": sel_loss_pnl,
        "kept_entries": len(rows) if action == "size" else len(rows) - len(selected),
    }

conds = []
for feat in features:
    vals = sorted(set(t.get(feat, 0) for t in rows))
    if len(vals) < 6:
        continue
    for p in [0.10,0.15,0.20,0.25,0.33,0.50,0.67,0.75,0.80,0.85,0.90]:
        th = vals[int((len(vals)-1)*p)]
        conds.append((f"{feat} < {th:.6g}", lambda t, feat=feat, th=th: t.get(feat, 0) < th))
        conds.append((f"{feat} > {th:.6g}", lambda t, feat=feat, th=th: t.get(feat, 0) > th))

all_conds = list(conds)
for (n1,p1),(n2,p2) in itertools.combinations(conds, 2):
    all_conds.append((f"({n1}) AND ({n2})", lambda t, p1=p1, p2=p2: p1(t) and p2(t)))

print("BEST_SKIP_KEEPING_90PCT_WIN_PNL_AND_80PCT_ENTRIES")
skip_results = []
for name, pred in all_conds:
    r = eval_condition(name, pred, "skip")
    if not r:
        continue
    kept_win = base_win - r["sel_win_pnl"]
    kept_entry_frac = r["kept_entries"] / len(rows)
    kept_win_frac = kept_win / base_win if base_win else 1
    if r["delta"] > 0 and kept_entry_frac >= 0.80 and kept_win_frac >= 0.90:
        r["kept_win_frac"] = kept_win_frac
        r["kept_entry_frac"] = kept_entry_frac
        skip_results.append(r)
for r in sorted(skip_results, key=lambda x: x["delta"], reverse=True)[:30]:
    print(f"delta={r['delta']:+.6f} new={r['new']:+.6f} keepEntry={r['kept_entry_frac']:.0%} keepWinPnl={r['kept_win_frac']:.0%} sel={r['selected']:2d} W/L={r['w']:2d}/{r['l']:2d} selW={r['sel_win_pnl']:+.6f} selL={r['sel_loss_pnl']:+.6f} {r['name']}")
print()

print("BEST_HALF_SIZE_WEAK_BUCKETS_KEEPING_ALL_ENTRIES")
size_results = []
for name, pred in all_conds:
    r = eval_condition(name, pred, "size", factor=0.5)
    if not r:
        continue
    # Full entry frequency. Require the selected bucket to be net negative; otherwise size-down is fake.
    if r["sel_win_pnl"] + r["sel_loss_pnl"] < 0 and r["delta"] > 0:
        size_results.append(r)
for r in sorted(size_results, key=lambda x: x["delta"], reverse=True)[:30]:
    print(f"delta={r['delta']:+.6f} new={r['new']:+.6f} sizeHalf sel={r['selected']:2d} W/L={r['w']:2d}/{r['l']:2d} selNet={r['sel_win_pnl']+r['sel_loss_pnl']:+.6f} selW={r['sel_win_pnl']:+.6f} selL={r['sel_loss_pnl']:+.6f} {r['name']}")
print()

print("BEST_DEAD_LOSS_SIGNATURES_NO_WINNER_CUT")
dead_results = []
for name, pred in all_conds:
    sel = [t for t in rows if pred(t)]
    if not sel:
        continue
    w = [t for t in sel if t["pnl"] > 0]
    dead = [t for t in sel if t["dead_loss"]]
    rec = [t for t in sel if t["recover_15"]]
    if not w and dead:
        dead_results.append((sum(t["pnl"] for t in dead), len(sel), len(dead), len(rec), name))
for pnl, n, dead_n, rec_n, name in sorted(dead_results, key=lambda x: x[0])[:25]:
    print(f"deadLossSum={pnl:+.6f} selected={n:2d} dead={dead_n:2d} recover={rec_n:2d} {name}")

