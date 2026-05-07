from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def source_for_row(row: dict[str, Any]) -> str:
    kind = str(row.get("instruction_kind") or "")
    if kind.startswith("pumpswap_"):
        return "pumpswap"
    if kind.startswith("launchlab_"):
        return "launchlab"
    if kind.startswith("dbc_"):
        return "dbc"
    return "pump"


def row_price(row: dict[str, Any]) -> float:
    cp = float(row.get("curve_price") or 0.0)
    if cp > 0:
        return cp
    sol = float(row.get("sol") or 0.0)
    tok = float(row.get("token_amount") or 0.0)
    if sol > 0 and tok > 0:
        return sol / tok
    return 0.0


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def fmt_sol(v: float) -> str:
    return f"{v:+.6f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fast experimentalji dry-live/source report")
    ap.add_argument("--run-id", default="", help="Run id under /root/piggy/data")
    ap.add_argument("--data-dir", default="/root/piggy/data")
    ap.add_argument("--raw", default="")
    ap.add_argument("--decisions", default="")
    ap.add_argument("--observations", default="")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    raw_path = Path(args.raw) if args.raw else data_dir / f"{args.run_id}_raw.jsonl"
    dec_path = Path(args.decisions) if args.decisions else data_dir / f"{args.run_id}_decisions.jsonl"
    obs_path = Path(args.observations) if args.observations else data_dir / f"{args.run_id}_source_observations.jsonl"

    raw = load_jsonl(raw_path)
    dec = load_jsonl(dec_path)
    obs = load_jsonl(obs_path)

    source_counts = Counter()
    source_mints: dict[str, set[str]] = defaultdict(set)
    source_buy_sol = Counter()
    mint_prices: dict[str, list[tuple[int, float]]] = defaultdict(list)
    first_ts = None
    last_ts = None
    for row in raw:
        ts = int(row.get("ts_ms") or 0)
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        src = source_for_row(row)
        source_counts[src] += 1
        mint = str(row.get("mint") or "")
        if mint:
            source_mints[src].add(mint)
        if row.get("side") == "buy":
            source_buy_sol[src] += float(row.get("sol") or 0.0)
        p = row_price(row)
        if mint and p > 0:
            mint_prices[mint].append((ts, p))

    opens = [r for r in dec if r.get("kind") == "open"]
    closes = [r for r in dec if r.get("kind") == "close"]
    wins = [r for r in closes if float(r.get("pnl_sol") or 0.0) > 0]
    losses = [r for r in closes if float(r.get("pnl_sol") or 0.0) < 0]
    net = sum(float(r.get("pnl_sol") or 0.0) for r in closes)

    lane_pnl = Counter()
    lane_counts = Counter()
    mint_lane = {}
    for r in opens:
        mint_lane[str(r.get("mint") or "")] = str(r.get("lane") or "?")
    for r in closes:
        lane = mint_lane.get(str(r.get("mint") or ""), str(r.get("lane") or "?"))
        lane_counts[lane] += 1
        lane_pnl[lane] += float(r.get("pnl_sol") or 0.0)

    print("=== EXPERIMENTALJI FAST REPORT ===")
    if first_ts and last_ts:
        runtime = max(0, (last_ts - first_ts) / 1000.0)
        print(f"runtime_raw: {runtime/60:.1f}m")
    print(f"raw_file: {raw_path}")
    print(f"decisions_file: {dec_path}")
    print()
    print("SOURCE TAPE")
    for src in sorted(source_counts):
        print(
            f"  {src:10s} events={source_counts[src]:6d} "
            f"mints={len(source_mints[src]):5d} buy_sol={source_buy_sol[src]:9.3f}"
        )
    if obs:
        obs_counts = Counter(str(r.get("source") or "?") for r in obs)
        print("OBSERVATION-ONLY")
        for src, n in sorted(obs_counts.items()):
            print(f"  {src:10s} observations={n}")
    print()
    print("TRADES")
    print(f"  opens={len(opens)} closes={len(closes)} W/L={len(wins)}/{len(losses)} net={fmt_sol(net)} SOL")
    print(f"  gross_wins={fmt_sol(sum(float(r.get('pnl_sol') or 0.0) for r in wins))} SOL")
    print(f"  gross_losses={fmt_sol(sum(float(r.get('pnl_sol') or 0.0) for r in losses))} SOL")
    print()
    print("LANES")
    for lane, n in lane_counts.most_common():
        print(f"  {lane:28s} n={n:3d} pnl={fmt_sol(lane_pnl[lane])} SOL")

    print()
    print("RECENT LOSSES")
    for r in losses[-8:]:
        mint = str(r.get("mint") or "")
        pts = mint_prices.get(mint, [])
        close_ts = int(r.get("ts_ms") or 0)
        close_price = 0.0
        max_after = 0.0
        if pts:
            before = [p for ts, p in pts if ts <= close_ts]
            after = [p for ts, p in pts if ts >= close_ts and ts <= close_ts + 120_000]
            close_price = before[-1] if before else pts[0][1]
            max_after = max(after) if after else 0.0
        post_mult = (max_after / close_price) if close_price > 0 and max_after > 0 else 0.0
        print(
            f"  {mint[:8]:8s} reason={str(r.get('reason') or ''):28s} "
            f"pnl={fmt_sol(float(r.get('pnl_sol') or 0.0))} post2m={post_mult:.2f}x"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
