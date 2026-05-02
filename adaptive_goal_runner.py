"""Adaptive dry-run goal runner for the sniper bot.

This script never places live orders. It runs a short live-data probe, learns
which symbol+direction pairs are working right now under the same capped-risk
rules as the bot, then runs the paper sniper only on those pairs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FINAL_RE = re.compile(r"SNIPER FINAL .* pnl=\$([+-]?\d+(?:\.\d+)?)")


@dataclass
class PairStats:
    symbol: str
    side: str
    count: int = 0
    net: Decimal = Decimal("0")
    best: Decimal = Decimal("-999999")
    worst: Decimal = Decimal("999999")
    stops: int = 0
    positives: int = 0
    profit_exits: int = 0

    def add(self, item: dict[str, str]) -> None:
        net = Decimal(str(item.get("net", "0")))
        best = Decimal(str(item.get("best_net", "0")))
        worst = Decimal(str(item.get("worst_net", "0")))
        reason = str(item.get("reason", ""))
        self.count += 1
        self.net += net
        self.best = max(self.best, best)
        self.worst = min(self.worst, worst)
        self.stops += 1 if reason == "stop" else 0
        self.positives += 1 if net > 0 else 0
        self.profit_exits += 1 if reason in {"net-profit", "target", "net-trail"} else 0

    @property
    def avg(self) -> Decimal:
        return self.net / Decimal(self.count) if self.count else Decimal("0")


def run_stream(cmd: list[str], label: str) -> tuple[int, Decimal | None]:
    print(f"\n=== {label} ===", flush=True)
    print(" ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    pnl: Decimal | None = None
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        m = FINAL_RE.search(line)
        if m:
            pnl = Decimal(m.group(1))
    proc.wait()
    return proc.returncode, pnl


def parse_probe(path: Path) -> tuple[list[PairStats], set[str]]:
    pairs: dict[tuple[str, str], PairStats] = {}
    bad_symbols: set[str] = set()
    if not path.exists():
        return [], bad_symbols
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        symbol = str(item["symbol"])
        side = str(item["side"])
        net = Decimal(str(item.get("net", "0")))
        reason = str(item.get("reason", ""))
        key = (symbol, side)
        pairs.setdefault(key, PairStats(symbol=symbol, side=side)).add(item)
        if reason == "stop" or net <= Decimal("-1.50"):
            bad_symbols.add(symbol)
    ranked = sorted(pairs.values(), key=lambda s: (s.net, s.best, s.positives), reverse=True)
    return ranked, bad_symbols


def choose_pairs(
    ranked: list[PairStats],
    min_pair_net: Decimal,
    min_best_net: Decimal,
    max_pairs: int,
) -> list[PairStats]:
    selected: list[PairStats] = []
    by_symbol: defaultdict[str, list[PairStats]] = defaultdict(list)
    for stats in ranked:
        by_symbol[stats.symbol].append(stats)

    for symbol, rows in by_symbol.items():
        rows.sort(key=lambda s: (s.net, s.best, s.positives), reverse=True)
        best = rows[0]
        other_net = sum((r.net for r in rows[1:]), Decimal("0"))
        if best.net >= min_pair_net and best.best >= min_best_net and best.net + other_net > Decimal("-1.50"):
            selected.append(best)
    selected.sort(key=lambda s: (s.net, s.best, s.positives), reverse=True)
    return selected[:max_pairs]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--probe-seconds", type=int, default=180)
    ap.add_argument("--trade-seconds", type=int, default=300)
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--target", type=Decimal, default=Decimal("10"))
    ap.add_argument("--loss", type=Decimal, default=Decimal("-8"))
    ap.add_argument("--max-trades", type=int, default=8)
    ap.add_argument("--max-pairs", type=int, default=6)
    ap.add_argument("--leverage", type=int, default=175)
    ap.add_argument("--margin-fraction", type=Decimal, default=Decimal("0.95"))
    ap.add_argument("--tp-pct", type=Decimal, default=Decimal("0.0035"))
    ap.add_argument("--sl-pct", type=Decimal, default=Decimal("0.0018"))
    ap.add_argument("--max-stop-risk-usdt", type=Decimal, default=Decimal("2.75"))
    ap.add_argument("--snap-profit-usdt", type=Decimal, default=Decimal("2.50"))
    ap.add_argument("--net-trail-arm-usdt", type=Decimal, default=Decimal("1.50"))
    ap.add_argument("--net-trail-giveback-usdt", type=Decimal, default=Decimal("0.85"))
    ap.add_argument("--min-flow-notional", type=Decimal, default=Decimal("5000"))
    ap.add_argument("--min-v1-pct", type=float, default=0.045)
    ap.add_argument("--min-v3-pct", type=float, default=0.08)
    ap.add_argument("--min-f1", type=float, default=0.35)
    ap.add_argument("--min-f3", type=float, default=0.20)
    ap.add_argument("--required-streak", type=int, default=2)
    ap.add_argument("--required-age-sec", type=float, default=0.05)
    ap.add_argument("--min-book-imbalance", type=float, default=-0.10)
    ap.add_argument("--confirm-delay-sec", type=float, default=0.45)
    ap.add_argument("--confirm-min-move-pct", type=float, default=0.03)
    ap.add_argument("--min-pair-net", type=Decimal, default=Decimal("0.50"))
    ap.add_argument("--min-best-net", type=Decimal, default=Decimal("1.50"))
    ap.add_argument("--skip-symbols", default="")
    args = ap.parse_args()

    base_skip = {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    adaptive_skip: set[str] = set()
    best_pnl: Decimal | None = None

    for cycle in range(1, args.cycles + 1):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        probe_path = ROOT / "logs" / f"adaptive_probe_{stamp}_c{cycle}.jsonl"
        skip = ",".join(sorted(base_skip | adaptive_skip))
        probe_cmd = [
            sys.executable,
            str(ROOT / "signal_probe.py"),
            "--seconds",
            str(args.probe_seconds),
            "--count",
            str(args.count),
            "--leverage",
            str(args.leverage),
            "--margin-fraction",
            str(args.margin_fraction),
            "--tp-pct",
            str(args.tp_pct),
            "--sl-pct",
            str(args.sl_pct),
            "--horizon-sec",
            "60",
            "--min-flow-notional",
            str(args.min_flow_notional),
            "--min-v1-pct",
            str(args.min_v1_pct),
            "--min-v3-pct",
            str(args.min_v3_pct),
            "--min-f1",
            str(args.min_f1),
            "--min-f3",
            str(args.min_f3),
            "--required-streak",
            str(args.required_streak),
            "--required-age-sec",
            str(args.required_age_sec),
            "--min-book-imbalance",
            str(args.min_book_imbalance),
            "--max-stop-risk-usdt",
            str(args.max_stop_risk_usdt),
            "--snap-profit-usdt",
            str(args.snap_profit_usdt),
            "--net-trail-arm-usdt",
            str(args.net_trail_arm_usdt),
            "--net-trail-giveback-usdt",
            str(args.net_trail_giveback_usdt),
            "--skip-symbols",
            skip,
            "--out",
            str(probe_path),
        ]
        run_stream(probe_cmd, f"ADAPTIVE PROBE CYCLE {cycle}")
        ranked, bad_symbols = parse_probe(probe_path)
        adaptive_skip.update(bad_symbols)
        print("\nProbe ranking:", flush=True)
        for row in ranked[:12]:
            print(
                f"  {row.symbol}:{row.side} count={row.count} net={row.net:+.4f} "
                f"avg={row.avg:+.4f} best={row.best:+.4f} stops={row.stops}",
                flush=True,
            )
        selected = choose_pairs(ranked, args.min_pair_net, args.min_best_net, args.max_pairs)
        if not selected:
            print("No positive symbol-direction pair survived this probe; skipping trade phase.", flush=True)
            continue
        symbols = ",".join(row.symbol for row in selected)
        symbol_sides = ",".join(f"{row.symbol}:{row.side}" for row in selected)
        print(f"Selected pairs: {symbol_sides}", flush=True)

        trade_cmd = [
            sys.executable,
            str(ROOT / "sniper_paper_bot.py"),
            "--seconds",
            str(args.trade_seconds),
            "--symbols",
            symbols,
            "--symbol-sides",
            symbol_sides,
            "--target",
            str(args.target),
            "--loss",
            str(args.loss),
            "--max-trades",
            str(args.max_trades),
            "--max-hold-sec",
            "90",
            "--leverage",
            str(args.leverage),
            "--margin-fraction",
            str(args.margin_fraction),
            "--tp-pct",
            str(args.tp_pct),
            "--sl-pct",
            str(args.sl_pct),
            "--trail-arm-pct",
            "0.0030",
            "--trail-giveback-pct",
            "0.0012",
            "--slow-start-sec",
            "45",
            "--slow-start-fee-multiple",
            "0.10",
            "--min-flow-notional",
            str(args.min_flow_notional),
            "--min-v1-pct",
            str(args.min_v1_pct),
            "--min-v3-pct",
            str(args.min_v3_pct),
            "--min-f1",
            str(args.min_f1),
            "--min-f3",
            str(args.min_f3),
            "--required-streak",
            str(args.required_streak),
            "--required-age-sec",
            str(args.required_age_sec),
            "--min-book-imbalance",
            str(args.min_book_imbalance),
            "--confirm-delay-sec",
            str(args.confirm_delay_sec),
            "--confirm-min-move-pct",
            str(args.confirm_min_move_pct),
            "--max-stop-risk-usdt",
            str(args.max_stop_risk_usdt),
            "--snap-profit-usdt",
            str(args.snap_profit_usdt),
            "--net-trail-arm-usdt",
            str(args.net_trail_arm_usdt),
            "--net-trail-giveback-usdt",
            str(args.net_trail_giveback_usdt),
            "--turbo",
        ]
        _, pnl = run_stream(trade_cmd, f"ADAPTIVE TRADE CYCLE {cycle}")
        if pnl is not None:
            best_pnl = pnl if best_pnl is None else max(best_pnl, pnl)
            if pnl >= args.target:
                print(f"ADAPTIVE SUCCESS cycle={cycle} pnl={pnl:+.4f}", flush=True)
                return 0
        adaptive_skip.update(row.symbol for row in selected if pnl is not None and pnl < Decimal("0"))

    print(f"ADAPTIVE STOP best_pnl={best_pnl if best_pnl is not None else 'none'}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
