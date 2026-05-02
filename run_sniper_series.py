"""Run repeated sniper dry sessions and track consecutive target wins."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FINAL_RE = re.compile(r"SNIPER FINAL .* pnl=\$([+-]?\d+(?:\.\d+)?)")
CLOSE_RE = re.compile(r"SNIPER CLOSE\s+([A-Z0-9]+)\s+\S+.* pnl=\$?([+-]?\d+(?:\.\d+)?)")


def run_once(args: argparse.Namespace, attempt: int, skipped_symbols: set[str]) -> tuple[Decimal | None, set[str]]:
    cmd = [
        sys.executable,
        str(ROOT / "sniper_paper_bot.py"),
        "--seconds",
        str(args.seconds),
        "--target",
        str(args.target),
        "--loss",
        str(args.loss),
        "--max-trades",
        str(args.max_trades),
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
        "--trail-arm-pct",
        str(args.trail_arm_pct),
        "--trail-giveback-pct",
        str(args.trail_giveback_pct),
        "--slow-start-sec",
        str(args.slow_start_sec),
        "--slow-start-fee-multiple",
        str(args.slow_start_fee_multiple),
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
        "--side",
        args.side,
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
        "--symbol-sides",
        args.symbol_sides,
    ]
    if args.turbo:
        cmd.append("--turbo")
    if args.fade:
        cmd.append("--fade")
    if args.symbols:
        cmd.extend(["--symbols", args.symbols])
    combined_skip = set(skipped_symbols)
    if args.skip_symbols:
        combined_skip.update(s.strip().upper() for s in args.skip_symbols.split(",") if s.strip())
    if combined_skip:
        cmd.extend(["--skip-symbols", ",".join(sorted(combined_skip))])

    print(f"\n=== SERIES ATTEMPT {attempt} ===", flush=True)
    if combined_skip:
        print(f"adaptive skip: {','.join(sorted(combined_skip))}", flush=True)
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
    new_bad_symbols: set[str] = set()
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        m = FINAL_RE.search(line)
        if m:
            pnl = Decimal(m.group(1))
        close_match = CLOSE_RE.search(line)
        if close_match:
            symbol = close_match.group(1)
            trade_pnl = Decimal(close_match.group(2))
            if trade_pnl <= Decimal("-1.00"):
                new_bad_symbols.add(symbol)
    proc.wait()
    if proc.returncode != 0:
        print(f"attempt {attempt} exited with code {proc.returncode}", flush=True)
    return pnl, new_bad_symbols


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--need-consecutive", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=240)
    parser.add_argument("--target", type=Decimal, default=Decimal("10"))
    parser.add_argument("--loss", type=Decimal, default=Decimal("-8"))
    parser.add_argument("--max-trades", type=int, default=8)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--leverage", type=int, default=70)
    parser.add_argument("--margin-fraction", type=Decimal, default=Decimal("0.90"))
    parser.add_argument("--tp-pct", type=Decimal, default=Decimal("0.008"))
    parser.add_argument("--sl-pct", type=Decimal, default=Decimal("0.0012"))
    parser.add_argument("--trail-arm-pct", type=Decimal, default=Decimal("0.008"))
    parser.add_argument("--trail-giveback-pct", type=Decimal, default=Decimal("0.003"))
    parser.add_argument("--slow-start-sec", type=float, default=8.0)
    parser.add_argument("--slow-start-fee-multiple", type=Decimal, default=Decimal("1.10"))
    parser.add_argument("--min-flow-notional", type=Decimal, default=Decimal("50000"))
    parser.add_argument("--min-v1-pct", type=float, default=0.10)
    parser.add_argument("--min-v3-pct", type=float, default=0.19)
    parser.add_argument("--min-f1", type=float, default=0.58)
    parser.add_argument("--min-f3", type=float, default=0.45)
    parser.add_argument("--required-streak", type=int, default=3)
    parser.add_argument("--required-age-sec", type=float, default=0.25)
    parser.add_argument("--min-book-imbalance", type=float, default=0.05)
    parser.add_argument("--side", choices=("BOTH", "LONG", "SHORT"), default="BOTH")
    parser.add_argument("--confirm-delay-sec", type=float, default=0.0)
    parser.add_argument("--confirm-min-move-pct", type=float, default=0.0)
    parser.add_argument("--max-stop-risk-usdt", type=Decimal, default=Decimal("0"))
    parser.add_argument("--snap-profit-usdt", type=Decimal, default=Decimal("0"))
    parser.add_argument("--net-trail-arm-usdt", type=Decimal, default=Decimal("0"))
    parser.add_argument("--net-trail-giveback-usdt", type=Decimal, default=Decimal("0"))
    parser.add_argument("--symbol-sides", default="")
    parser.add_argument("--turbo", action="store_true", default=True)
    parser.add_argument("--no-turbo", dest="turbo", action="store_false")
    parser.add_argument("--fade", action="store_true")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--skip-symbols", default="")
    args = parser.parse_args()

    consecutive = 0
    wins = 0
    losses = 0
    total = Decimal("0")
    bad_symbols: set[str] = set()
    for attempt in range(1, args.max_attempts + 1):
        pnl, new_bad_symbols = run_once(args, attempt, bad_symbols)
        if new_bad_symbols:
            bad_symbols.update(new_bad_symbols)
            print(f"ATTEMPT {attempt}: adding adaptive skips {','.join(sorted(new_bad_symbols))}", flush=True)
        if pnl is None:
            consecutive = 0
            losses += 1
            print(f"ATTEMPT {attempt}: no parsed result, consecutive reset", flush=True)
            continue
        total += pnl
        if pnl >= args.target:
            wins += 1
            consecutive += 1
            print(f"ATTEMPT {attempt}: WIN pnl={pnl:+.4f}, consecutive={consecutive}", flush=True)
        else:
            losses += 1
            consecutive = 0
            print(f"ATTEMPT {attempt}: MISS pnl={pnl:+.4f}, consecutive reset", flush=True)
        if consecutive >= args.need_consecutive:
            print(
                f"SERIES SUCCESS: {consecutive} consecutive target wins, "
                f"wins={wins} losses={losses} total_pnl={total:+.4f}",
                flush=True,
            )
            return 0
    print(
        f"SERIES STOP: only {consecutive} consecutive target wins after {args.max_attempts} attempts, "
        f"wins={wins} losses={losses} total_pnl={total:+.4f} adaptive_skips={','.join(sorted(bad_symbols))}",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
