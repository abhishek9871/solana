import collections
import json
import math
import os
import statistics
import sys
from pathlib import Path


def load_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def q(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def short(mint: str) -> str:
    return (mint or "?")[:8]


def robust_max_mult(raw_rows, mint, entry_ts, entry_price, window_ms=120_000):
    if entry_price <= 0:
        return 0.0, 0
    vals = []
    max_ts = 0
    for x in raw_rows:
        if x.get("mint") != mint:
            continue
        ts = int(x.get("ts_ms") or 0)
        if ts < entry_ts or ts > entry_ts + window_ms:
            continue
        price = q(x.get("curve_price"))
        if price <= 0:
            continue
        mult = price / entry_price
        if 0.01 <= mult <= 50.0:
            vals.append(mult)
            if mult == max(vals):
                max_ts = ts
    if not vals:
        return 0.0, 0
    vals.sort()
    # Use the 99th percentile instead of a single max to avoid one bad cache spike.
    idx = min(len(vals) - 1, max(0, int(len(vals) * 0.99)))
    return vals[idx], max_ts


def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else ""
    base = Path(os.environ.get("PIGGY_DATA_DIR", "/root/piggy/data"))
    if not run_id:
        run_id = Path("/root/piggy/current_pgg2_runid.txt").read_text().strip()

    state_path = base / f"{run_id}_state.json"
    dec_path = base / f"{run_id}_decisions.jsonl"
    raw_path = base / f"{run_id}_raw.jsonl"

    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text()).get("session", {})
    decisions = load_jsonl(dec_path)
    raw = load_jsonl(raw_path)

    by_mint = collections.defaultdict(list)
    for row in decisions:
        mint = row.get("mint")
        if mint:
            by_mint[mint].append(row)

    trades = []
    for mint, rows in by_mint.items():
        opens = [r for r in rows if r.get("kind") == "open"]
        closes = [r for r in rows if r.get("kind") == "close"]
        if not opens or not closes:
            continue
        op = opens[0]
        cl = closes[-1]
        of = op.get("features") or {}
        cf = cl.get("features") or {}
        entry_ts = int(op.get("ts_ms") or 0)
        close_ts = int(cl.get("ts_ms") or 0)
        entry_price = q(of.get("price") or of.get("curve_price"))
        post_mult, post_ts = robust_max_mult(raw, mint, entry_ts, entry_price)
        trades.append(
            {
                "mint": mint,
                "lane": op.get("lane") or of.get("lane") or "?",
                "reason": cl.get("reason"),
                "pnl": q(cl.get("pnl_sol")),
                "age_s": max(0.0, (close_ts - entry_ts) / 1000.0),
                "score": q(of.get("score")),
                "buy700": q(of.get("buy700")),
                "buy1500": q(of.get("buy1500")),
                "uniq700": q(of.get("uniq700")),
                "top700": q(of.get("top_share700")),
                "move700": q(of.get("move700")),
                "base": q(of.get("base_move") or of.get("wave_base_move")),
                "entry_price": entry_price,
                "close_peak": q(cf.get("peak_mult")),
                "close_mult": q(cf.get("mult") or cf.get("live_mult")),
                "post2m_p99": post_mult,
                "post2m_age_s": max(0.0, (post_ts - entry_ts) / 1000.0) if post_ts else 0.0,
            }
        )

    print("RUN", run_id)
    if state:
        print(
            "STATE closes={closes} W/L={wins}/{losses} net={net:+.6f} best={best:.3f} reconn={reconn}".format(
                closes=state.get("closes", 0),
                wins=state.get("wins", 0),
                losses=state.get("losses", 0),
                net=q(state.get("realized_pnl_sol")),
                best=q(state.get("best_mult"), 1.0),
                reconn=state.get("reconnects", 0),
            )
        )

    print("TRADES", len(trades), "net", f"{sum(t['pnl'] for t in trades):+.6f}")
    print()
    print("LANES")
    for lane in sorted(set(t["lane"] for t in trades)):
        grp = [t for t in trades if t["lane"] == lane]
        print(
            f"  {lane:24s} n={len(grp):2d} W/L={sum(t['pnl']>0 for t in grp)}/{sum(t['pnl']<0 for t in grp)} "
            f"net={sum(t['pnl'] for t in grp):+.6f} avg_age={statistics.mean(t['age_s'] for t in grp):.2f}s"
        )

    print()
    print("TRADE DETAIL")
    for t in trades:
        print(
            f"  {short(t['mint'])} {t['lane']:20s} {t['reason']:30s} "
            f"pnl={t['pnl']:+.6f} age={t['age_s']:5.2f}s score={t['score']:6.1f} "
            f"b700={t['buy700']:5.2f} u700={t['uniq700']:3.0f} top={t['top700']:.2f} "
            f"m700={t['move700']:.3f} peak={t['close_peak']:.2f} post2m={t['post2m_p99']:.2f}x"
        )

    losses = [t for t in trades if t["pnl"] < 0]
    print()
    print("LOSS DIAGNOSIS")
    for t in losses:
        tag = []
        if t["lane"] == "curve_lag_reveal":
            tag.append("full-size curve_lag loss")
        if t["age_s"] < 1.0:
            tag.append("instant rug/quote drop")
        if t["post2m_p99"] >= 1.5:
            tag.append("later pump after stop")
        elif t["post2m_p99"] < 1.35:
            tag.append("did not recover enough")
        print(f"  {short(t['mint'])}: {', '.join(tag) or 'normal loss'}")


if __name__ == "__main__":
    main()
