#!/usr/bin/env python3
"""V271 capacity audit for the frozen V223/V255/V256 PumpSwap atomic lane.

This is no-spend. It answers one question before any exact sim or live send:
does the current PumpSwap multipool lane contain enough raw edge to justify a
dollar-denominated target, after de-duplicating route candidates?
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


LAMPORTS_PER_SOL = 1_000_000_000


def load_best_routes(path: pathlib.Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        key = (str(row.get("mint") or ""), str(row.get("buy_pool") or ""), str(row.get("sell_pool") or ""))
        if not all(key):
            continue
        if key not in best or int(row.get("edge_lamports", 0)) > int(best[key].get("edge_lamports", 0)):
            best[key] = row
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-jsonl", required=True)
    ap.add_argument("--sol-usd", type=float, default=80.0)
    ap.add_argument("--min-profit-usd", type=float, default=0.50)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--out-json", default="")
    args = ap.parse_args()

    path = pathlib.Path(args.candidate_jsonl)
    target_lamports = int((float(args.min_profit_usd) / float(args.sol_usd)) * LAMPORTS_PER_SOL + 0.999999)
    routes = load_best_routes(path)
    rows = sorted(routes.values(), key=lambda r: int(r.get("edge_lamports", 0)), reverse=True)
    positive = [r for r in rows if int(r.get("edge_lamports", 0)) > 0]
    max_edge = int(positive[0].get("edge_lamports", 0)) if positive else 0
    top_n_sum = sum(int(r.get("edge_lamports", 0)) for r in positive[: max(1, int(args.top_n))])
    total_positive = sum(int(r.get("edge_lamports", 0)) for r in positive)
    exact_target_routes = sum(1 for r in positive if int(r.get("edge_lamports", 0)) >= target_lamports)
    multiplier_needed = (target_lamports / max(max_edge, 1)) if max_edge else float("inf")
    report = {
        "candidate_jsonl": str(path),
        "target_lamports": target_lamports,
        "target_usd": float(args.min_profit_usd),
        "sol_usd": float(args.sol_usd),
        "unique_routes": len(routes),
        "positive_routes": len(positive),
        "max_edge_lamports": max_edge,
        "top_n": int(args.top_n),
        "top_n_sum_lamports": top_n_sum,
        "total_positive_raw_edge_lamports": total_positive,
        "routes_ge_target": exact_target_routes,
        "single_route_multiplier_needed": multiplier_needed,
        "top_routes": [
            {
                "mint": r.get("mint"),
                "buy_pool": r.get("buy_pool"),
                "sell_pool": r.get("sell_pool"),
                "size_lamports": int(r.get("size_lamports", 0)),
                "edge_lamports": int(r.get("edge_lamports", 0)),
                "why": r.get("why"),
            }
            for r in positive[: max(1, int(args.top_n))]
        ],
    }
    line = (
        "PGG2-V271-CAPACITY "
        f"target_lamports={target_lamports} unique_routes={len(routes)} "
        f"positive_routes={len(positive)} max_edge={max_edge} "
        f"top{int(args.top_n)}_sum={top_n_sum} total_positive={total_positive} "
        f"routes_ge_target={exact_target_routes} "
        f"single_route_multiplier_needed={multiplier_needed:.2f}"
    )
    print(line, flush=True)
    print(json.dumps(report, sort_keys=True), flush=True)
    if args.out_json:
        pathlib.Path(args.out_json).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if exact_target_routes > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
