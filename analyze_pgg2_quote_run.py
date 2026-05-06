import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-QUOTE-SHADOW-BUY (?P<short>\S+) "
    r"lane=(?P<lane>\S+) cost=(?P<cost>[-0-9.]+) quote_tokens=(?P<tokens>[-0-9.]+) "
    r"fill=(?P<fill>[-+0-9.eE]+) score=(?P<score>[-0-9.]+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-QUOTE-SHADOW-SELL (?P<short>\S+) "
    r"reason=(?P<reason>\S+) quote_out=(?P<quote_out>[-0-9.]+) overhead=(?P<overhead>[-0-9.]+) "
    r"proceeds=(?P<proceeds>[-0-9.]+) pnl=(?P<pnl>[-+0-9.]+) session=(?P<session>[-+0-9.]+)"
)
HOLD_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-SELL-HOLD (?P<short>\S+) "
    r"reason=(?P<reason>\S+) quote_out=(?P<quote_out>[-0-9.]+) cost=(?P<cost>[-0-9.]+) need=(?P<need>[-0-9.]+)"
)
SKIP_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-EDGE-SKIP lane=(?P<lane>\S+) reason=(?P<reason>.+)$"
)


def fnum(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def short_of(mint: str) -> str:
    return f"{mint[:4]}..pump"


def load_decisions(path: Path):
    by_short = {}
    plans = defaultdict(list)
    opens = defaultdict(list)
    closes = defaultdict(list)
    skipped = []
    kinds = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        kinds[row.get("kind")] += 1
        mint = row.get("mint")
        if mint:
            by_short[short_of(mint)] = mint
        kind = row.get("kind")
        if kind == "strike_plan":
            plans[mint].append(row)
        elif kind == "open":
            opens[mint].append(row)
        elif kind == "close":
            closes[mint].append(row)
        elif kind == "strike_skipped":
            skipped.append(row)
    return by_short, plans, opens, closes, skipped, kinds


def load_log(path: Path):
    buys = []
    sells = []
    holds = defaultdict(list)
    edge_skips = []
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
        if m := SKIP_RE.search(line):
            edge_skips.append(m.groupdict())
    return buys, sells, holds, edge_skips


def pick_entry_features(mint, plans, opens):
    if opens.get(mint):
        return opens[mint][0].get("features") or {}
    if plans.get(mint):
        return plans[mint][0].get("features") or {}
    return {}


def historical_live_edge_reject(lane, features):
    # Mirrors the current pgg2_live_raptor.py live-edge filter to explain why a row passed.
    buy700 = fnum(features.get("buy700"))
    buy1500 = fnum(features.get("buy1500"))
    uniq700 = int(fnum(features.get("uniq700")))
    uniq1500 = int(fnum(features.get("uniq1500")))
    top700 = fnum(features.get("top_share700"), 1.0)
    top1500 = fnum(features.get("top_share1500"), 1.0)
    score = fnum(features.get("score"))
    vsol = fnum(features.get("vsol_sol"))

    if lane == "birth_fanout":
        if uniq1500 < 14:
            return f"birth_uniq1500_low:{uniq1500}"
        if top1500 > 0.30:
            return f"birth_top1500_high:{top1500:.2f}"
        if not (score >= 230 or buy1500 >= 15.5):
            return f"birth_conviction_low:score={score:.1f},buy1500={buy1500:.2f}"
        return None
    if lane == "curve_lag_reveal":
        if top700 >= 0.50:
            return None
        if top700 >= 0.3458 and vsol <= 35.0:
            return None
        if score < 100 and buy1500 >= 17.5 and vsol <= 42.0:
            return None
        return f"curve_quote_edge_low:top700={top700:.2f},vsol={vsol:.2f},buy1500={buy1500:.2f}"
    if lane == "reclaim_wave":
        if uniq700 < 7:
            return f"reclaim_uniq700_low:{uniq700}"
        if top700 > 0.32:
            return f"reclaim_top700_high:{top700:.2f}"
        return None
    if lane == "second_wave_after_cluster":
        if uniq1500 < 8:
            return f"second_uniq1500_low:{uniq1500}"
        if top700 > 0.40:
            return f"second_top700_high:{top700:.2f}"
        return None
    if lane in {"early_ignition", "late_ignition"}:
        if score < 190:
            return f"ignition_score_low:{score:.1f}"
        if buy1500 < 6.0:
            return f"ignition_buy1500_low:{buy1500:.2f}"
        return None
    return None


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else "pgg2_quote_005_20260506_014600"
    dec_path = Path(f"{prefix}_decisions.jsonl")
    log_path = Path(f"{prefix}.log")
    state_path = Path(f"{prefix}_state.json")
    by_short, plans, opens, closes, skipped, kinds = load_decisions(dec_path)
    buys, sells, holds, edge_skips = load_log(log_path)

    state = json.loads(state_path.read_text(encoding="utf-8"))["session"] if state_path.exists() else {}
    print("=== STATE ===")
    for k in ("creates", "trades", "strike_plans", "scouts", "closes", "wins", "losses", "kills", "best_mult", "realized_pnl_sol", "reconnects"):
        print(f"{k:18s} {state.get(k)}")
    print()
    print("=== LOG COUNTS ===")
    print(f"quote buys:        {len(buys)}")
    print(f"quote sells:       {len(sells)}")
    print(f"sell holds:        {sum(len(v) for v in holds.values())}")
    print(f"edge skips:        {len(edge_skips)}")
    print(f"decision kinds:    {dict(kinds)}")
    print("edge skip reasons:")
    for reason, n in Counter(s["reason"] for s in edge_skips).most_common():
        print(f"  {n:2d} {reason}")
    print()

    print("=== ACTUAL QUOTE FILLS ===")
    total = 0.0
    rows = []
    for s in sells:
        mint = by_short.get(s["short"])
        buy = next((b for b in buys if b["short"] == s["short"]), {})
        lane = buy.get("lane") or (plans[mint][0].get("lane") if mint in plans and plans[mint] else "?")
        feats = pick_entry_features(mint, plans, opens)
        reject = historical_live_edge_reject(lane, feats)
        hold_max = max([h["quote_out"] for h in holds.get(s["short"], [])] or [0.0])
        total += s["pnl"]
        rows.append((s, buy, lane, feats, reject, hold_max))
    rows.sort(key=lambda r: r[0]["ts"])
    for i, (s, buy, lane, f, reject, hold_max) in enumerate(rows, 1):
        print(
            f"{i:02d} {s['ts']} {s['short']:10s} {lane:24s} {s['reason']:26s} "
            f"pnl={s['pnl']:+.6f} cost={buy.get('cost', 0):.3f} qout={s['quote_out']:.6f} "
            f"hold_max={hold_max:.6f} pass={'YES' if reject is None else 'NO'}"
        )
        print(
            "    "
            f"score={fnum(f.get('score')):.1f} cluster={fnum(f.get('cluster_score')):.1f} "
            f"buy700={fnum(f.get('buy700')):.3f} uniq700={int(fnum(f.get('uniq700')))} top700={fnum(f.get('top_share700'), 0):.3f} "
            f"buy1500={fnum(f.get('buy1500')):.3f} uniq1500={int(fnum(f.get('uniq1500')))} top1500={fnum(f.get('top_share1500'), 0):.3f} "
            f"vsol={fnum(f.get('vsol_sol')):.2f} move700={fnum(f.get('move700')):.3f} wave_base={fnum(f.get('wave_base_move')):.3f} "
            f"slot_buy={fnum(f.get('slot_buy_sol')):.3f} slot_buyers={int(fnum(f.get('slot_buyers')))} slot_top={fnum(f.get('slot_top_share'), 0):.3f}"
        )
    print(f"\nnet quote pnl: {total:+.6f} SOL")
    print()

    print("=== NON-FILLED STRIKE/EDGE NOTES ===")
    skipped_by_reason = Counter(row.get("reason") for row in skipped)
    for reason, n in skipped_by_reason.most_common(12):
        print(f"  decision skipped {n:2d} {reason}")
    print()

    # Show planned mints that never bought, to understand missed/blocked opportunities.
    sold_short = {s["short"] for s in sells}
    buy_short = {b["short"] for b in buys}
    all_plan_mints = [m for m, ps in plans.items() if ps]
    never_bought = [m for m in all_plan_mints if short_of(m) not in buy_short]
    print(f"planned mints: {len(all_plan_mints)}  bought: {len(buy_short)}  never bought: {len(never_bought)}")
    for m in never_bought[:20]:
        p = plans[m][0]
        f = p.get("features") or {}
        print(
            f"  {short_of(m):10s} lane={p.get('lane'):24s} plan_score={fnum(p.get('score')):.1f} "
            f"edge_reject={historical_live_edge_reject(p.get('lane'), f)} "
            f"feat score={fnum(f.get('score')):.1f} b1500={fnum(f.get('buy1500')):.2f} u1500={int(fnum(f.get('uniq1500')))} "
            f"top700={fnum(f.get('top_share700'), 0):.2f} top1500={fnum(f.get('top_share1500'), 0):.2f}"
        )


if __name__ == "__main__":
    main()
