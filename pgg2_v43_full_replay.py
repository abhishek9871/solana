#!/usr/bin/env python3
"""
pgg2_v43_full_replay.py — Phase 6 full historical replay.

For each historical log with V42F coverage, walk in time order through the
1s buckets of V43_REGIME_DATASET.jsonl. At each bucket:
   1. Evaluate the promoted regime gate.
   2. If hot, evaluate the promoted per-mint entry rules over V42F intersnap
      rows whose decision_ts ∈ that bucket. For each row that satisfies a
      promoted rule, "simulate" an entry. Track outcome via the V42F-
      precomputed `label_first_bank_or_scratch_pnl`. We DO NOT re-enter the
      same (log, mint) twice.
   3. If cold, no entries.

Per-log report contains:
   - regime-hot uptime %
   - entries, wins (≥SCRATCH_THRESHOLD), losses (<-SCRATCH_THRESHOLD), scratches
   - cumulative simulated PnL
   - whether the V39B winner window produced 10W/0L
   - whether losing log windows correctly idled

Output: /root/piggy/V43_FULL_REPLAY_REPORT.md
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple

V42F_DATASET = "/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl"
V43_DATASET = "/root/piggy/V43_REGIME_DATASET.jsonl"
PROMO_GATE = "/root/piggy/V43_PROMOTED_GATE.json"
PROMO_ENTRY = "/root/piggy/V43_PROMOTED_ENTRY_RULES.json"
REPORT = "/root/piggy/V43_FULL_REPLAY_REPORT.md"

SCRATCH = 0.00005
BANK = 0.00060


def load_promo():
    g = {}
    e = {}
    if os.path.exists(PROMO_GATE):
        with open(PROMO_GATE) as fh:
            g = json.load(fh)
    if os.path.exists(PROMO_ENTRY):
        with open(PROMO_ENTRY) as fh:
            e = json.load(fh)
    return g, e


def gate_pred_from_promo(promo):
    name = promo.get("promoted_gate_name") or ""
    top = promo.get("promoted_gate_top") or {}
    params = top.get("params") or {}
    if name == "v43_quote_continuation_hot":
        return lambda r: (
            (r.get("distinct_mints_with_quote_n_to_n_plus_1_improvement_30s", 0) or 0) >= params["T"]
            and (r.get("median_quote_improvement_across_mints_30s", 0.0) or 0.0) >= params["M"]
        )
    if name == "v43_curve_breadth_hot":
        return lambda r: (
            (r.get("distinct_mints_with_positive_curve_delta_30s", 0) or 0) >= params["T"]
            and (r.get("total_pump_bc_quoteable_mints_30s", 0) or 0) >= params["N"]
        )
    if name == "v43_tape_buy_pressure_hot":
        return lambda r: (
            (r.get("buy_count_30s", 0) or 0) >= params["B"]
            and (r.get("buy_sol_total_30s", 0.0) or 0.0) >= params["S"]
            and (r.get("buy_sell_ratio_30s", 0.0) or 0.0) >= params["R"]
        )
    if name == "v43_execution_quality_hot":
        Q, P, L = params["Q"], params["P"], params["L"]
        def f(r):
            lat = r.get("quote_latency_p50_30s", -1.0)
            lat_ok = True if (lat is None or lat < 0) else lat <= L
            return (
                (r.get("sim_needed_0_rate_60s", 0.0) or 0.0) >= Q
                and (r.get("pair_source_current_sig_rate_60s", 0.0) or 0.0) >= P
                and lat_ok
            )
        return f
    if name == "v43_combined_hot":
        f1 = params["f1"]; f2 = params["f2"]
        p1 = lambda r: (
            (r.get("distinct_mints_with_quote_n_to_n_plus_1_improvement_30s", 0) or 0) >= f1["T"]
            and (r.get("median_quote_improvement_across_mints_30s", 0.0) or 0.0) >= f1["M"]
        )
        p2 = lambda r: (
            (r.get("distinct_mints_with_positive_curve_delta_30s", 0) or 0) >= f2["T"]
            and (r.get("total_pump_bc_quoteable_mints_30s", 0) or 0) >= f2["N"]
        )
        return lambda r: p1(r) and p2(r)
    # fallback: always-hot if no gate selected
    return lambda r: True


def make_entry_rule_predicates(promo_entry: Dict[str, Any]) -> List[Tuple[str, Callable[[Dict[str, Any]], bool]]]:
    """Build predicates over a V42F row's `features` dict from each promoted rule."""
    out = []
    rules = promo_entry.get("promoted_entry_rules", [])
    for rule in rules:
        fam = rule.get("family", "")
        p = rule.get("params")
        # Params is stored as a list (since tuple was JSON-serialised)
        if not isinstance(p, (list, tuple)):
            continue
        if fam == "v43_hot_quote_gradient_predictor" and len(p) >= 2:
            qg, qd = p[0], p[1]
            out.append((fam, lambda f, qg=qg, qd=qd: (
                (f.get("f_quote_gradient", 0.0) or 0.0) >= qg
                and (f.get("f_quote_delta_N_minus_1", 0.0) or 0.0) >= qd
            )))
        elif fam == "v43_hot_curve_delta_quote_follow" and len(p) >= 2:
            cd, cg = p[0], p[1]
            out.append((fam, lambda f, cd=cd, cg=cg: (
                (f.get("f_curve_delta_N_minus_1", 0.0) or 0.0) >= cd
                and (f.get("f_curve_gradient", 0.0) or 0.0) >= cg
            )))
        elif fam == "v43_hot_recovered_quote_acceleration" and len(p) >= 2:
            qd, rr = p[0], p[1]
            out.append((fam, lambda f, qd=qd, rr=rr: (
                ((not rr) or bool(f.get("f_recovered_quote") or 0))
                and (f.get("f_quote_delta_N_minus_1", 0.0) or 0.0) >= qd
                and (f.get("f_quote_delta_N_minus_2", 0.0) or 0.0) >= 0
            )))
        elif fam == "v43_hot_pending_flow_predictor" and len(p) >= 2:
            pb, sp = p[0], p[1]
            out.append((fam, lambda f, pb=pb, sp=sp: (
                (f.get("f_buy1000_sol", 0.0) or 0.0) >= pb
                and (f.get("f_since_prev_buy_sol", 0.0) or 0.0) >= sp
            )))
        elif fam == "v43_hot_high_momentum_confirmed" and len(p) >= 3:
            cg, qg, b5 = p[0], p[1], p[2]
            out.append((fam, lambda f, cg=cg, qg=qg, b5=b5: (
                (f.get("f_curve_gradient", 0.0) or 0.0) >= cg
                and (f.get("f_quote_gradient", 0.0) or 0.0) >= qg
                and (f.get("f_buy500_sol", 0.0) or 0.0) >= b5
            )))
    return out


def load_v43() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    with open(V43_DATASET) as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            out[d.get("log", "")].append(d)
    for log in out:
        out[log].sort(key=lambda r: r.get("decision_ts_ms", 0))
    return out


def load_v42f_by_log() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    with open(V42F_DATASET) as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            out[d.get("log", "")].append(d)
    for log in out:
        out[log].sort(key=lambda r: r.get("decision_ts_ms", 0))
    return out


def main() -> int:
    promo_gate, promo_entry = load_promo()
    gate_pred = gate_pred_from_promo(promo_gate)
    entry_preds = make_entry_rule_predicates(promo_entry)
    print(f"[v43-replay] gate={promo_gate.get('promoted_gate_name')} entry rules={len(entry_preds)}")

    v43_idx = load_v43()
    v42f_idx = load_v42f_by_log()
    print(f"[v43-replay] v43 logs={len(v43_idx)} v42f logs={len(v42f_idx)}")

    # Per log statistics
    summary = []
    overall = defaultdict(int)
    overall_pnl = 0.0
    for log, buckets in sorted(v43_idx.items()):
        hot_buckets = [b for b in buckets if gate_pred(b)]
        hot_pct = 100.0 * len(hot_buckets) / max(1, len(buckets))
        hot_ts_set = set(b["decision_ts_ms"] for b in hot_buckets)
        v42f_rows = v42f_idx.get(log, [])

        # walk rows in time order; only consider if in a hot bucket
        already_entered = set()
        entries = 0
        wins = 0
        losses = 0
        scratches = 0
        pnl = 0.0
        first_neg_ts = None
        wins_before_first_loss = 0
        early_wins = 0
        for r in v42f_rows:
            ts = r.get("decision_ts_ms", 0)
            bucket_ts = (ts // 1000) * 1000
            if bucket_ts not in hot_ts_set:
                continue
            # Only one entry per mint per log
            mint = r.get("mint", "")
            if (log, mint) in already_entered:
                continue
            # Rule firing
            f = r.get("features", {})
            fired_rule = None
            for fname, pr in entry_preds:
                if pr(f):
                    fired_rule = fname
                    break
            if fired_rule is None:
                continue
            already_entered.add((log, mint))
            entries += 1
            lbl = r.get("label_first_bank_or_scratch_pnl", 0.0) or 0.0
            try:
                import math
                if math.isnan(float(lbl)):
                    lbl = 0.0
            except Exception:
                lbl = 0.0
            pnl += lbl
            if lbl >= SCRATCH:
                wins += 1
                if first_neg_ts is None:
                    wins_before_first_loss += 1
            elif lbl < -SCRATCH:
                losses += 1
                if first_neg_ts is None:
                    first_neg_ts = ts
            else:
                scratches += 1
                if first_neg_ts is None:
                    wins_before_first_loss += 0
        is_v39b_winner = log == "pgg2_v39b_quote_rescue_drylive_20260512_133527.log"
        is_v39b_mirror = log.startswith("pgg2_v39b_quote_rescue_live_mirror_")

        summary.append({
            "log": log,
            "buckets": len(buckets),
            "hot_buckets": len(hot_buckets),
            "hot_pct": hot_pct,
            "v42f_rows": len(v42f_rows),
            "entries": entries,
            "wins": wins,
            "losses": losses,
            "scratches": scratches,
            "pnl": pnl,
            "wins_before_first_loss": wins_before_first_loss,
            "first_neg_ts": first_neg_ts,
            "is_v39b_winner": is_v39b_winner,
            "is_v39b_mirror": is_v39b_mirror,
        })
        overall["buckets"] += len(buckets)
        overall["hot_buckets"] += len(hot_buckets)
        overall["entries"] += entries
        overall["wins"] += wins
        overall["losses"] += losses
        overall["scratches"] += scratches
        overall_pnl += pnl

    # V39B winner window: did it produce 10W/0L?
    winner = next((s for s in summary if s["is_v39b_winner"]), None)
    mirrors = [s for s in summary if s["is_v39b_mirror"]]

    with open(REPORT, "w", encoding="utf-8") as out:
        out.write("# V43 Full Historical Replay Report\n\n")
        out.write(f"- regime gate: `{promo_gate.get('promoted_gate_name')}` "
                  f"params=`{(promo_gate.get('promoted_gate_top') or {}).get('params')}`\n")
        out.write(f"- entry rules: {len(entry_preds)} ({[f for f,_ in entry_preds]})\n")
        out.write(f"- logs replayed: {len(summary)}\n\n")

        out.write("## Overall\n\n")
        out.write(f"- total buckets: {overall['buckets']}\n")
        out.write(f"- hot buckets: {overall['hot_buckets']} ({100*overall['hot_buckets']/max(1,overall['buckets']):.1f}%)\n")
        out.write(f"- total simulated entries: {overall['entries']}\n")
        out.write(f"- W/L/scratch: {overall['wins']}/{overall['losses']}/{overall['scratches']}\n")
        out.write(f"- summed PnL: {overall_pnl:+.5f} SOL\n\n")

        out.write("## V39B drylive winner reproduction\n\n")
        if winner:
            out.write(f"- log: `{winner['log']}`\n")
            out.write(f"- hot uptime: {winner['hot_pct']:.1f}%\n")
            out.write(f"- entries: {winner['entries']} (wins={winner['wins']} losses={winner['losses']} scratches={winner['scratches']})\n")
            out.write(f"- wins before first loss: **{winner['wins_before_first_loss']}**\n")
            out.write(f"- summed PnL: {winner['pnl']:+.5f} SOL\n")
            if winner["wins_before_first_loss"] >= 10 and winner["losses"] == 0:
                out.write("\n**V39B 10W/0L sequence REPRODUCED in replay.**\n\n")
            elif winner["wins_before_first_loss"] >= 5:
                out.write(f"\n**Partial reproduction: {winner['wins_before_first_loss']} wins before first loss.**\n\n")
            else:
                out.write("\n**10W/0L sequence NOT reproduced.** The promoted regime + entry rules did not "
                          "produce a 10-win streak even with hindsight.\n\n")
        else:
            out.write("(winner log not in V43 dataset)\n\n")

        out.write("## V39B losing live-mirror runs\n\n")
        if mirrors:
            tot_e = sum(s["entries"] for s in mirrors)
            tot_h = sum(s["hot_buckets"] for s in mirrors)
            tot_b = sum(s["buckets"] for s in mirrors)
            out.write(f"- mirror logs: {len(mirrors)}\n")
            out.write(f"- mirror buckets: {tot_b}, hot buckets: {tot_h} ({100*tot_h/max(1,tot_b):.1f}%)\n")
            out.write(f"- entries during mirrors: {tot_e}\n")
            if tot_e == 0:
                out.write("\n**Losing windows correctly produced ZERO entries.**\n\n")
            elif tot_e < 10:
                out.write(f"\nLosing windows produced {tot_e} entries (suppression partial).\n\n")
            else:
                out.write(f"\nLosing windows produced {tot_e} entries — the gate did NOT suppress losing-window flow.\n\n")
        else:
            out.write("(no mirror logs found)\n\n")

        out.write("## Per-log breakdown\n\n")
        out.write("| Log | Buckets | Hot % | V42F rows | Entries | W/L/S | PnL |\n")
        out.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for s in sorted(summary, key=lambda x: -x["entries"])[:40]:
            out.write(f"| `{s['log']}` | {s['buckets']} | {s['hot_pct']:.1f}% | {s['v42f_rows']} | "
                      f"{s['entries']} | {s['wins']}/{s['losses']}/{s['scratches']} | {s['pnl']:+.5f} |\n")

        out.write("\n## Verdict\n\n")
        ok_winner = winner and winner["wins_before_first_loss"] >= 5 and winner["losses"] == 0
        ok_losers = (not mirrors) or sum(s["entries"] for s in mirrors) < 5
        if ok_winner and ok_losers:
            out.write("**REPLAY OK** — V39B winner window produced ≥5 wins before any loss AND losing-mirror "
                      "windows produced <5 entries total. Stage-A go-live is plausible if Phase 7 sees the regime hot live.\n")
        else:
            out.write("**REPLAY DID NOT MEET TIGHTEST CRITERIA.** "
                      f"winner={'OK' if ok_winner else 'fail'} losers={'OK' if ok_losers else 'fail'}. "
                      "Stage-A is NOT validated by historical replay.\n")

    print(f"[v43-replay] wrote {REPORT} overall entries={overall['entries']} W/L={overall['wins']}/{overall['losses']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
