import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from analyze_pgg2_quote_run import (
    BUY_RE,
    SELL_RE,
    HOLD_RE,
    fnum,
    historical_live_edge_reject,
    load_decisions,
    short_of,
)


def load_log_rows(path: Path):
    buys = []
    sells = []
    holds = defaultdict(list)
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if m := BUY_RE.search(line):
            d = m.groupdict()
            for k in ("cost", "tokens", "fill", "score"):
                d[k] = fnum(d[k])
            buys.append(d)
        if m := SELL_RE.search(line):
            d = m.groupdict()
            for k in ("quote_out", "overhead", "proceeds", "pnl", "session"):
                d[k] = fnum(d[k])
            sells.append(d)
        if m := HOLD_RE.search(line):
            d = m.groupdict()
            for k in ("quote_out", "cost", "need"):
                d[k] = fnum(d[k])
            holds[d["short"]].append(d)
    return buys, sells, holds


def current_gate_pass(lane, f):
    return historical_live_edge_reject(lane, f) is None


def proposed_gate_pass(lane, f):
    # Current gate plus the missing live-only refinements found from the 2026-05-06 run.
    if not current_gate_pass(lane, f):
        return False

    score = fnum(f.get("score"))
    buy700 = fnum(f.get("buy700"))
    buy1500 = fnum(f.get("buy1500"))
    uniq700 = int(fnum(f.get("uniq700")))
    uniq1500 = int(fnum(f.get("uniq1500")))
    top700 = fnum(f.get("top_share700"), 1.0)
    top1500 = fnum(f.get("top_share1500"), 1.0)
    vsol = fnum(f.get("vsol_sol"))
    move700 = fnum(f.get("move700"))
    wave_base = fnum(f.get("wave_base_move"))
    slot_buy = fnum(f.get("slot_buy_sol"))
    slot_buyers = int(fnum(f.get("slot_buyers")))
    slot_top = fnum(f.get("slot_top_share"), 1.0)

    if lane == "birth_fanout":
        return buy1500 >= 14.0 and (score >= 230.0 or buy1500 >= 15.5)

    if lane == "breadth_ignition":
        return (
            score >= 140.0
            and buy1500 >= 3.0
            and uniq1500 >= 8
            and move700 >= 1.05
            and vsol >= 40.0
            and slot_buyers >= 5
            and slot_top <= 0.35
        )

    if lane == "curve_lag_reveal":
        # The fresh loser passed only through the bootstrap branch despite a red 700ms
        # curve and weak top-share. Existing strong top-share winners remain allowed.
        if top700 < 0.3458 and move700 < 0.98:
            return False
        if score < 100 and buy1500 >= 17.5 and top700 < 0.50 and vsol > 35.0:
            return False
        return True

    return True


def collect_rows(root: Path):
    rows = []
    for log in sorted(root.glob("pgg2_quote*.log")):
        stem = log.stem
        dec = root / f"{stem}_decisions.jsonl"
        if not dec.exists():
            continue
        by_short, plans, opens, closes, skipped, kinds = load_decisions(dec)
        buys, sells, holds = load_log_rows(log)
        for s in sells:
            mint = by_short.get(s["short"])
            buy = next((b for b in buys if b["short"] == s["short"]), {})
            lane = buy.get("lane") or (plans[mint][0].get("lane") if mint in plans and plans[mint] else "?")
            f = {}
            if mint and opens.get(mint):
                f = opens[mint][0].get("features") or {}
            elif mint and plans.get(mint):
                f = plans[mint][0].get("features") or {}
            rows.append(
                {
                    "run": stem,
                    "ts": s["ts"],
                    "short": s["short"],
                    "mint": mint,
                    "lane": lane,
                    "reason": s["reason"],
                    "pnl": s["pnl"],
                    "cost": buy.get("cost", 0.0),
                    "quote_out": s["quote_out"],
                    "hold_max": max([h["quote_out"] for h in holds.get(s["short"], [])] or [0.0]),
                    "f": f,
                }
            )
    return rows


def summarize(rows, name, pred=lambda r: True):
    kept = [r for r in rows if pred(r)]
    wins = [r for r in kept if r["pnl"] > 0]
    losses = [r for r in kept if r["pnl"] <= 0]
    print(
        f"{name:34s} n={len(kept):3d} W/L={len(wins):2d}/{len(losses):2d} "
        f"net={sum(r['pnl'] for r in kept):+.6f} grossW={sum(r['pnl'] for r in wins):+.6f} grossL={sum(r['pnl'] for r in losses):+.6f}"
    )
    return kept


def main():
    root = Path("quote_history")
    rows = collect_rows(root)
    print(f"rows={len(rows)} runs={len(set(r['run'] for r in rows))}")
    summarize(rows, "BASE")
    summarize(rows, "CURRENT LIVE EDGE", lambda r: current_gate_pass(r["lane"], r["f"]))
    summarize(rows, "PROPOSED LIVE EDGE V2", lambda r: proposed_gate_pass(r["lane"], r["f"]))
    print()

    print("=== BY LANE BASE ===")
    for lane in sorted(set(r["lane"] for r in rows)):
        summarize(rows, lane, lambda r, lane=lane: r["lane"] == lane)
    print()

    print("=== BY LANE CURRENT GATE ===")
    for lane in sorted(set(r["lane"] for r in rows)):
        summarize(rows, lane, lambda r, lane=lane: r["lane"] == lane and current_gate_pass(r["lane"], r["f"]))
    print()

    print("=== BY LANE PROPOSED V2 ===")
    for lane in sorted(set(r["lane"] for r in rows)):
        summarize(rows, lane, lambda r, lane=lane: r["lane"] == lane and proposed_gate_pass(r["lane"], r["f"]))
    print()

    print("=== BY RUN: CURRENT -> PROPOSED ===")
    for run in sorted(set(r["run"] for r in rows)):
        base = [r for r in rows if r["run"] == run]
        cur = [r for r in base if current_gate_pass(r["lane"], r["f"])]
        pro = [r for r in base if proposed_gate_pass(r["lane"], r["f"])]
        print(
            f"{run:42s} base {len(base):2d} {sum(r['pnl'] for r in base):+.6f} | "
            f"cur {len(cur):2d} {sum(r['pnl'] for r in cur):+.6f} | "
            f"v2 {len(pro):2d} {sum(r['pnl'] for r in pro):+.6f}"
        )
    print()

    print("=== CURRENT GATE LOSSES THAT V2 REJECTS ===")
    for r in rows:
        if current_gate_pass(r["lane"], r["f"]) and not proposed_gate_pass(r["lane"], r["f"]) and r["pnl"] <= 0:
            f = r["f"]
            print(
                f"{r['run']} {r['ts']} {r['short']:10s} {r['lane']:22s} pnl={r['pnl']:+.6f} "
                f"reason={r['reason']} score={fnum(f.get('score')):.1f} b1500={fnum(f.get('buy1500')):.3f} "
                f"u1500={int(fnum(f.get('uniq1500')))} top700={fnum(f.get('top_share700'),0):.3f} "
                f"top1500={fnum(f.get('top_share1500'),0):.3f} vsol={fnum(f.get('vsol_sol')):.2f} "
                f"move700={fnum(f.get('move700')):.3f} slot_buyers={int(fnum(f.get('slot_buyers')))} slot_top={fnum(f.get('slot_top_share'),0):.3f}"
            )
    print()

    print("=== CURRENT GATE WINS THAT V2 REJECTS ===")
    for r in rows:
        if current_gate_pass(r["lane"], r["f"]) and not proposed_gate_pass(r["lane"], r["f"]) and r["pnl"] > 0:
            f = r["f"]
            print(
                f"{r['run']} {r['ts']} {r['short']:10s} {r['lane']:22s} pnl={r['pnl']:+.6f} "
                f"reason={r['reason']} score={fnum(f.get('score')):.1f} b1500={fnum(f.get('buy1500')):.3f} "
                f"u1500={int(fnum(f.get('uniq1500')))} top700={fnum(f.get('top_share700'),0):.3f} "
                f"top1500={fnum(f.get('top_share1500'),0):.3f} vsol={fnum(f.get('vsol_sol')):.2f} "
                f"move700={fnum(f.get('move700')):.3f} slot_buyers={int(fnum(f.get('slot_buyers')))} slot_top={fnum(f.get('slot_top_share'),0):.3f}"
            )


if __name__ == "__main__":
    main()
