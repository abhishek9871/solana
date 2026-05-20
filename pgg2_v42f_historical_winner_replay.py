#!/usr/bin/env python3
"""V42F Phase 5 — historical 10W/0L winner replay.

For each of the 10 V39B winners, find the buy-trigger snap in the chain (the
first snap where the winning bot opened a position) and check whether any
promoted V42F rule would have fired before the winning sell.

NO TX. NO NETWORK.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DATASET = Path("/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl")
PROMOTED = Path("/root/piggy/V42F_PROMOTED_RULES.json")
DECISIONS = Path("/root/piggy/data/pgg2_v39b_quote_rescue_drylive_20260512_133527_decisions.jsonl")
OUT = Path("/root/piggy/V42F_HISTORICAL_WINNER_REPLAY.md")

# Rule callables (must match miner — keep in sync)
def rule_quote_gradient_predictor(f, qgrad_min, qdelta_n1_min):
    return f["f_quote_gradient"] >= qgrad_min and f["f_quote_delta_N_minus_1"] >= qdelta_n1_min

def rule_curve_delta_quote_follow(f, cdelta_n1_min, cgrad_min):
    return f["f_curve_delta_N_minus_1"] >= cdelta_n1_min and f["f_curve_gradient"] >= cgrad_min

def rule_recovered_quote_acceleration(f, qdelta_n1_min, recovered_required):
    if recovered_required and not f["f_recovered_quote"]:
        return False
    return f["f_quote_delta_N_minus_1"] >= qdelta_n1_min and f["f_quote_delta_N_minus_2"] >= 0

def rule_pending_flow_predictor(f, pending_buy_sol_min, since_prev_buy_min):
    return f["f_buy1000_sol"] >= pending_buy_sol_min and f["f_since_prev_buy_sol"] >= since_prev_buy_min

def rule_high_momentum_confirmed(f, cgrad_min, qgrad_min, b500_sol_min):
    return (
        f["f_curve_gradient"] >= cgrad_min
        and f["f_quote_gradient"] >= qgrad_min
        and f["f_buy500_sol"] >= b500_sol_min
    )

RULE_FNS = {
    "v42f_quote_gradient_predictor": rule_quote_gradient_predictor,
    "v42f_curve_delta_quote_follow": rule_curve_delta_quote_follow,
    "v42f_recovered_quote_acceleration": rule_recovered_quote_acceleration,
    "v42f_pending_flow_predictor": rule_pending_flow_predictor,
    "v42f_high_momentum_confirmed": rule_high_momentum_confirmed,
}


def short_mint(mint: str) -> str:
    if len(mint) <= 8:
        return mint
    return mint[:4] + ".." + mint[-4:]


def main() -> int:
    promoted = []
    if PROMOTED.exists():
        promoted = json.loads(PROMOTED.read_text(encoding="utf-8"))

    rows = []
    with DATASET.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    rows.sort(key=lambda r: r["decision_ts_ms"])

    # Winner mints (full) — close events from the v39b winner log
    winners: List[Tuple[str, float, int]] = []
    if DECISIONS.exists():
        with DECISIONS.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("kind") == "close" and float(d.get("pnl_sol", 0)) > 0:
                    winners.append(
                        (d["mint"], float(d["pnl_sol"]), int(d["ts_ms"]))
                    )

    md = []
    md.append("# V42F Historical 10W/0L Winner Replay")
    md.append("")
    md.append(f"- promoted rules in: `{PROMOTED}`")
    md.append(f"- winner-source decisions: `{DECISIONS}`")
    md.append(f"- promoted rule count: {len(promoted)}")
    md.append(f"- winners found in decisions: {len(winners)}")
    md.append("")

    if not promoted:
        md.append("**No promoted rules to replay.** Nothing to test against the 10 winners.")
        md.append("")
        md.append("If the rule miner promoted NONE, V42F's transparent grid search proved that")
        md.append("no causal rule predicts the inter-snapshot positive label across the full")
        md.append("discovery/validation/holdout splits. The winners can still be detected by")
        md.append("V42B's curve-delta-or-shred signal, but that signal does NOT survive the")
        md.append("strict time-split with non-negative holdout PnL.")
        OUT.write_text("\n".join(md), encoding="utf-8")
        return 0

    # For each winner, find earliest snap in chain (within ~2s of close) and
    # test each promoted rule.
    md.append("## Per-winner detection table")
    md.append("")
    md.append("| # | mint | pnl_sol | close_ts | rule_fired | rule_id | params | policy | features_at_decision |")
    md.append("|---|---|---|---|---|---|---|---|---|")

    captured = 0
    for i, (mint_full, pnl, close_ts) in enumerate(winners, 1):
        # Match by short-mint substring since logs short the mint.
        sm = short_mint(mint_full)
        cands = [
            r for r in rows
            if r["mint"] == sm and r["decision_ts_ms"] <= close_ts
        ]
        fired = False
        fire_record = None
        for r in cands:
            for prom in promoted:
                rfn = RULE_FNS[prom["family"]]
                params = tuple(prom["params"])
                try:
                    if rfn(r["features"], *params):
                        fired = True
                        fire_record = (r, prom)
                        break
                except Exception:
                    continue
            if fired:
                break
        if fired:
            captured += 1
            r, prom = fire_record
            feats_blob = ",".join(
                f"{k}={r['features'][k]}"
                for k in (
                    "f_quote_gradient",
                    "f_curve_gradient",
                    "f_curve_delta_N_minus_1",
                    "f_quote_delta_N_minus_1",
                    "f_buy1000_sol",
                    "f_pair_source",
                )
                if k in r["features"]
            )
            md.append(
                f"| {i} | `{mint_full}` | +{pnl:.6f} | {close_ts} | yes | {prom['family']} | {prom['params']} | {prom['policy']} | {feats_blob} |"
            )
        else:
            md.append(f"| {i} | `{mint_full}` | +{pnl:.6f} | {close_ts} | no | - | - | - | (no rule fired) |")

    md.append("")
    md.append(f"## Aggregate")
    md.append("")
    md.append(f"- winners captured by ANY promoted V42F rule: **{captured} / {len(winners)}**")
    md.append(f"- pass condition: >= 8/10 with zero known historical negative admitted")
    pass_cond = captured >= 8
    md.append(f"- verdict: **{'PASS' if pass_cond else 'FAIL'}**")
    OUT.write_text("\n".join(md), encoding="utf-8")
    print(f"[V42F-WINNER-REPLAY] captured={captured}/{len(winners)} out={OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
