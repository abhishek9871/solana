import glob
import json
import os
import re
from collections import defaultdict

BASE = "/root/piggy"

buy_re = re.compile(
    r"PGG2-QUOTE-SHADOW-BUY (?P<short>\S+) lane=(?P<lane>\S+) "
    r"cost=(?P<cost>[-0-9.]+).*?entry_roundtrip_loss=(?P<rtl>[-0-9.]+)"
)
try_re = re.compile(
    r"PGG2-LIVE-ROUNDTRIP-TRY (?P<short>\S+) lane=(?P<lane>\S+) "
    r"loss=(?P<loss>[-0-9.]+) max=(?P<max>[-0-9.]+) amount=(?P<amount>[-0-9.]+)"
)
loss_check_re = re.compile(
    r"PGG2-LIVE-QUOTE-LOSS-CHECK (?P<short>\S+) lane=(?P<lane>\S+) "
    r"quote_out=(?P<quote>[-0-9.]+) pnl=(?P<pnl>[+-]?[0-9.]+) max_loss=(?P<max>[-0-9.]+)"
)
sell_re = re.compile(
    r"PGG2-QUOTE-SHADOW-SELL (?P<short>\S+) reason=(?P<reason>\S+) "
    r".*?mult=(?P<mult>[-0-9.]+) pnl=(?P<pnl>[+-]?[0-9.]+)"
)


def short(mint: str) -> str:
    return (mint or "")[:4]


def norm_short(log_short: str) -> str:
    return (log_short or "").split("..", 1)[0]


def load_decisions(runid: str):
    path = f"{BASE}/data/{runid}_decisions.jsonl"
    rows = defaultdict(dict)
    if not os.path.exists(path):
        return rows
    for line in open(path):
        if not line.strip():
            continue
        x = json.loads(line)
        m = x.get("mint")
        if not m:
            continue
        k = x.get("kind")
        if k == "open":
            rows[m]["open"] = x
        elif k == "close":
            rows[m]["close"] = x
        elif k == "strike_plan":
            rows[m]["strike"] = x
    return rows


def main():
    all_rows = []
    for log_path in sorted(glob.glob(f"{BASE}/logs/experimentalji_direct_drylive_*.log")):
        runid = os.path.basename(log_path)[:-4]
        decisions = load_decisions(runid)
        by_short = defaultdict(lambda: {"tries": [], "checks": [], "buy": None, "sell": None})
        for line in open(log_path, errors="ignore"):
            for regex, key in (
                (try_re, "tries"),
                (loss_check_re, "checks"),
            ):
                m = regex.search(line)
                if m:
                    d = m.groupdict()
                    d["ts"] = line[:21]
                    d["short"] = norm_short(d["short"])
                    by_short[d["short"]][key].append(d)
            m = buy_re.search(line)
            if m:
                d = m.groupdict()
                d["ts"] = line[:21]
                d["short"] = norm_short(d["short"])
                by_short[d["short"]]["buy"] = d
            m = sell_re.search(line)
            if m:
                d = m.groupdict()
                d["ts"] = line[:21]
                d["short"] = norm_short(d["short"])
                by_short[d["short"]]["sell"] = d

        for mint, row in decisions.items():
            close = row.get("close")
            op = row.get("open")
            strike = row.get("strike") or {}
            if not close or not op:
                continue
            lane = op.get("lane")
            if lane != "curve_lag_reveal":
                continue
            s = short(mint)
            logs = by_short.get(s, {})
            fop = (strike.get("features") or op.get("features") or {})
            fcl = close.get("features") or {}
            first_check = (logs.get("checks") or [{}])[0]
            last_check = (logs.get("checks") or [{}])[-1]
            all_rows.append(
                {
                    "run": runid.replace("experimentalji_direct_drylive_", ""),
                    "mint": s,
                    "pnl": float(close.get("pnl_sol") or 0),
                    "reason": close.get("reason"),
                    "age": (close.get("ts_ms", 0) - op.get("ts_ms", 0)) / 1000,
                    "cost": float((logs.get("buy") or {}).get("cost") or 0),
                    "entry_rtl": float((logs.get("buy") or {}).get("rtl") or 0),
                    "first_check": float(first_check.get("pnl") or 0),
                    "last_check": float(last_check.get("pnl") or 0),
                    "checks": len(logs.get("checks") or []),
                    "buy1500": float(fop.get("buy1500") or 0),
                    "uniq1500": int(fop.get("uniq1500") or 0),
                    "top1500": float(fop.get("top_share1500") or 0),
                    "live700": float(fop.get("curve_lag_live_buy700") or fop.get("buy700") or 0),
                    "uniq700": int(fop.get("curve_lag_live_unique700") or fop.get("uniq700") or 0),
                    "move700": float(fop.get("move700") or 0),
                    "entry_move": float(fop.get("entry_move_from_first") or 0),
                    "close_mult": float((logs.get("sell") or {}).get("mult") or 0),
                    "curve_move_close": float(fcl.get("move700") or fcl.get("wave_base_move") or 0),
                }
            )

    print("CURVE_LAG_DIRECT_DRYLIVE_TRADES")
    print(
        "run              mint pnl       reason                 age  cost  entryRT firstQ  lastQ checks b1500/u/top live700/u move700 entryMove closeMult curveClose"
    )
    for r in all_rows:
        print(
            f"{r['run'][-13:]:13s} {r['mint']:4s} {r['pnl']:+.6f} {r['reason'][:22]:22s} "
            f"{r['age']:4.2f} {r['cost']:.3f} {r['entry_rtl']:+.6f} {r['first_check']:+.6f} {r['last_check']:+.6f} "
            f"{r['checks']:2d} {r['buy1500']:.2f}/{r['uniq1500']}/{r['top1500']:.2f} "
            f"{r['live700']:.2f}/{r['uniq700']} {r['move700']:.3f} {r['entry_move']:.3f} {r['close_mult']:.3f} {r['curve_move_close']:.3f}"
        )
    wins = [r for r in all_rows if r["pnl"] > 0]
    losses = [r for r in all_rows if r["pnl"] <= 0]
    print()
    print(f"summary n={len(all_rows)} wins={len(wins)} losses={len(losses)} net={sum(r['pnl'] for r in all_rows):+.6f}")
    for name, group in (("wins", wins), ("losses", losses)):
        if not group:
            continue
        print(f"{name}:")
        for key in ("entry_rtl", "first_check", "buy1500", "uniq1500", "live700", "uniq700", "move700", "entry_move"):
            vals = sorted(float(r[key]) for r in group)
            print(f"  {key:12s} min={vals[0]:+.6f} med={vals[len(vals)//2]:+.6f} max={vals[-1]:+.6f}")


if __name__ == "__main__":
    main()
