#!/usr/bin/env python3
"""
pgg2_v43_hot_regime_entry_miner.py — Phase 5.

Restrict V42F-style per-mint causal rule mining to ONLY the buckets where the
promoted regime gate (from V43_PROMOTED_GATE.json) is HOT.

For each V42F intersnap row, look up the V43 regime bucket whose
decision_ts_ms equals (row.decision_ts_ms // 1000)*1000 (1s bucket cadence)
and check whether the promoted gate fires on that bucket. If yes, the row
is "in-regime" and kept; else it's discarded.

Then re-run V42F's 5 rule families (quote_gradient, curve_delta_quote_follow,
recovered_quote_acceleration, pending_flow_predictor, high_momentum_confirmed)
with the same 60/20/20 time split. Apply V42F's exit-policy A
(bank ≥ 0.00060 / scratch ≥ 0.00005) as the realised label.

Promotion criteria (mirror V42F):
    - discovery, validation, holdout all show positive precision lift over
      base rate AND non-zero recall.

Output: /root/piggy/V43_HOT_REGIME_ENTRY_RULE_REPORT.md
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, List, Tuple

V42F_DATASET = "/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl"
V43_DATASET = "/root/piggy/V43_REGIME_DATASET.jsonl"
PROMO = "/root/piggy/V43_PROMOTED_GATE.json"
REPORT = "/root/piggy/V43_HOT_REGIME_ENTRY_RULE_REPORT.md"

BANK_THRESHOLD = 0.00060
SCRATCH_THRESHOLD = 0.00005


def load_promo() -> Dict[str, Any]:
    with open(PROMO, "r", encoding="utf-8") as fh:
        return json.load(fh)


def gate_predicate_from_promo(promo: Dict[str, Any]) -> Callable[[Dict[str, Any]], bool]:
    name = promo.get("promoted_gate_name") or ""
    top = promo.get("promoted_gate_top") or {}
    params = top.get("params") or {}
    if name == "v43_quote_continuation_hot":
        T = params["T"]; M = params["M"]
        return lambda r: (
            (r.get("distinct_mints_with_quote_n_to_n_plus_1_improvement_30s", 0) or 0) >= T
            and (r.get("median_quote_improvement_across_mints_30s", 0.0) or 0.0) >= M
        )
    if name == "v43_curve_breadth_hot":
        T = params["T"]; N = params["N"]
        return lambda r: (
            (r.get("distinct_mints_with_positive_curve_delta_30s", 0) or 0) >= T
            and (r.get("total_pump_bc_quoteable_mints_30s", 0) or 0) >= N
        )
    if name == "v43_tape_buy_pressure_hot":
        B = params["B"]; S = params["S"]; R = params["R"]
        return lambda r: (
            (r.get("buy_count_30s", 0) or 0) >= B
            and (r.get("buy_sol_total_30s", 0.0) or 0.0) >= S
            and (r.get("buy_sell_ratio_30s", 0.0) or 0.0) >= R
        )
    if name == "v43_execution_quality_hot":
        Q = params["Q"]; P = params["P"]; L = params["L"]
        def f(r):
            lat = r.get("quote_latency_p50_30s", -1.0)
            if lat is None or lat < 0:
                lat_ok = True
            else:
                lat_ok = lat <= L
            return (
                (r.get("sim_needed_0_rate_60s", 0.0) or 0.0) >= Q
                and (r.get("pair_source_current_sig_rate_60s", 0.0) or 0.0) >= P
                and lat_ok
            )
        return f
    if name == "v43_combined_hot":
        f1 = params["f1"]; f2 = params["f2"]
        f1_p = lambda r: (
            (r.get("distinct_mints_with_quote_n_to_n_plus_1_improvement_30s", 0) or 0) >= f1["T"]
            and (r.get("median_quote_improvement_across_mints_30s", 0.0) or 0.0) >= f1["M"]
        )
        f2_p = lambda r: (
            (r.get("distinct_mints_with_positive_curve_delta_30s", 0) or 0) >= f2["T"]
            and (r.get("total_pump_bc_quoteable_mints_30s", 0) or 0) >= f2["N"]
        )
        return lambda r: f1_p(r) and f2_p(r)
    return lambda r: True  # null gate


def load_regime_index(pred: Callable[[Dict[str, Any]], bool]) -> Dict[Tuple[str, int], bool]:
    """Map (log, bucket_ts_ms) -> hot/cold."""
    out: Dict[Tuple[str, int], bool] = {}
    with open(V43_DATASET, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            log = d.get("log", "")
            ts = d.get("decision_ts_ms", 0)
            out[(log, ts)] = pred(d)
    return out


def load_v42f_rows() -> List[Dict[str, Any]]:
    out = []
    with open(V42F_DATASET, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    out.sort(key=lambda r: r.get("decision_ts_ms", 0))
    return out


def realised_label(row: Dict[str, Any]) -> float:
    """V42F policy-A: bank_or_scratch."""
    if "label_first_bank_or_scratch_pnl" in row:
        return float(row["label_first_bank_or_scratch_pnl"])
    return 0.0


# Rule families (mirroring V42F)
def rule_quote_gradient_predictor(f, qg_min, qd_min):
    return (
        (f.get("f_quote_gradient", 0.0) or 0.0) >= qg_min
        and (f.get("f_quote_delta_N_minus_1", 0.0) or 0.0) >= qd_min
    )


def rule_curve_delta_quote_follow(f, cd_min, cg_min):
    return (
        (f.get("f_curve_delta_N_minus_1", 0.0) or 0.0) >= cd_min
        and (f.get("f_curve_gradient", 0.0) or 0.0) >= cg_min
    )


def rule_recovered_quote_acceleration(f, qd_min, rec_req):
    if rec_req and not (f.get("f_recovered_quote") or 0):
        return False
    return (
        (f.get("f_quote_delta_N_minus_1", 0.0) or 0.0) >= qd_min
        and (f.get("f_quote_delta_N_minus_2", 0.0) or 0.0) >= 0
    )


def rule_pending_flow_predictor(f, pb_min, sp_min):
    return (
        (f.get("f_buy1000_sol", 0.0) or 0.0) >= pb_min
        and (f.get("f_since_prev_buy_sol", 0.0) or 0.0) >= sp_min
    )


def rule_high_momentum_confirmed(f, cg_min, qg_min, b5_min):
    return (
        (f.get("f_curve_gradient", 0.0) or 0.0) >= cg_min
        and (f.get("f_quote_gradient", 0.0) or 0.0) >= qg_min
        and (f.get("f_buy500_sol", 0.0) or 0.0) >= b5_min
    )


GRIDS = [
    ("v43_hot_quote_gradient_predictor", rule_quote_gradient_predictor,
        [(qg, qd) for qg in (0.0, 0.00010, 0.00050, 0.00100) for qd in (0.0, 0.000010, 0.000050, 0.000200)]),
    ("v43_hot_curve_delta_quote_follow", rule_curve_delta_quote_follow,
        [(cd, cg) for cd in (0.0, 1e-9, 1e-8, 1e-7) for cg in (0.0, 1e-12, 1e-11)]),
    ("v43_hot_recovered_quote_acceleration", rule_recovered_quote_acceleration,
        [(qd, r) for qd in (0.0, 0.000020, 0.000100) for r in (0, 1)]),
    ("v43_hot_pending_flow_predictor", rule_pending_flow_predictor,
        [(pb, sp) for pb in (0.0, 0.5, 1.0, 2.0) for sp in (0.0, 0.5, 1.0)]),
    ("v43_hot_high_momentum_confirmed", rule_high_momentum_confirmed,
        [(cg, qg, b5) for cg in (0.0, 1e-12) for qg in (0.0, 0.00050, 0.00100) for b5 in (0.0, 0.5, 1.0)]),
]


def eval_rule(recs: List[Dict[str, Any]], pred) -> Tuple[float, float, int, int, float]:
    tp = fp = fn = pos = 0
    realised_sum = 0.0
    for r in recs:
        f = r.get("features", {})
        y_realised = realised_label(r)
        y = y_realised >= SCRATCH_THRESHOLD
        p = pred(f)
        if p:
            realised_sum += y_realised
        if p and y:
            tp += 1
        elif p and not y:
            fp += 1
        elif (not p) and y:
            fn += 1
        if y:
            pos += 1
    fires = tp + fp
    prec = tp / fires if fires else 0.0
    rec = tp / pos if pos else 0.0
    return prec, rec, fires, pos, realised_sum


def base_rate(recs):
    if not recs:
        return 0.0
    p = sum(1 for r in recs if realised_label(r) >= SCRATCH_THRESHOLD)
    return p / len(recs)


def main() -> int:
    if not os.path.exists(PROMO):
        print("[v43-hot] no promoted gate", file=sys.stderr)
        return 1
    promo = load_promo()
    pred = gate_predicate_from_promo(promo)
    print(f"[v43-hot] gate: {promo.get('promoted_gate_name')}")

    regime = load_regime_index(pred)
    v42f = load_v42f_rows()
    print(f"[v43-hot] regime buckets indexed={len(regime)} v42f rows={len(v42f)}")

    # Filter v42f rows to those whose bucket is hot
    hot_rows = []
    for r in v42f:
        ts = r.get("decision_ts_ms", 0)
        bucket = (ts // 1000) * 1000
        log = r.get("log", "")
        if regime.get((log, bucket), False):
            hot_rows.append(r)
    cold_rows = [r for r in v42f if r not in hot_rows]
    print(f"[v43-hot] hot rows={len(hot_rows)} cold rows={len(v42f) - len(hot_rows)}")

    # Time split
    hot_rows.sort(key=lambda r: r.get("decision_ts_ms", 0))
    n = len(hot_rows)
    disc = hot_rows[: int(n * 0.6)]
    val = hot_rows[int(n * 0.6) : int(n * 0.8)]
    hol = hot_rows[int(n * 0.8) :]
    print(f"[v43-hot] split disc={len(disc)} val={len(val)} hol={len(hol)}")

    br_d, br_v, br_h = base_rate(disc), base_rate(val), base_rate(hol)
    print(f"[v43-hot] base rate disc={br_d:.4f} val={br_v:.4f} hol={br_h:.4f}")

    family_results: Dict[str, List[Dict[str, Any]]] = {}
    for fam_name, fn, grid in GRIDS:
        rows = []
        for params in grid:
            pred_rule = (lambda p: lambda f: fn(f, *p))(params)
            pd_, rd_, fd_, _, rs_d = eval_rule(disc, pred_rule)
            pv_, rv_, fv_, _, rs_v = eval_rule(val, pred_rule)
            ph_, rh_, fh_, _, rs_h = eval_rule(hol, pred_rule)
            promoted = (
                pd_ > br_d and rd_ > 0 and
                pv_ > br_v and rv_ > 0 and
                ph_ > br_h and rh_ > 0
            )
            rows.append({
                "params": params,
                "disc": (pd_, rd_, fd_, rs_d),
                "val": (pv_, rv_, fv_, rs_v),
                "hol": (ph_, rh_, fh_, rs_h),
                "promoted": promoted,
            })
        rows.sort(key=lambda x: (not x["promoted"], -x["hol"][3]))
        family_results[fam_name] = rows

    promoted_rules = []
    for fname, rows in family_results.items():
        for r in rows:
            if r["promoted"]:
                promoted_rules.append((fname, r))
                break  # one per family

    with open(REPORT, "w", encoding="utf-8") as out:
        out.write("# V43 Hot-Regime Entry-Rule Report (Phase 5)\n\n")
        out.write(f"- promoted regime gate: `{promo.get('promoted_gate_name')}` "
                  f"params=`{(promo.get('promoted_gate_top') or {}).get('params')}`\n")
        out.write(f"- V42F intersnap rows: {len(v42f)}\n")
        out.write(f"- in-regime rows (hot bucket): **{len(hot_rows)}** "
                  f"({100*len(hot_rows)/max(1,len(v42f)):.1f}%)\n")
        out.write(f"- split disc/val/hol: {len(disc)} / {len(val)} / {len(hol)}\n")
        out.write(f"- base rate of any-positive realised label (PnL ≥ {SCRATCH_THRESHOLD}): "
                  f"disc={br_d:.4f} val={br_v:.4f} hol={br_h:.4f}\n\n")

        for fname, rows in family_results.items():
            out.write(f"## Family: `{fname}`\n\n")
            out.write("Top 5 parameter settings (promoted first, then by holdout realised PnL sum):\n\n")
            out.write("| Params | disc P/R/fires/sumPnL | val P/R/fires/sumPnL | hol P/R/fires/sumPnL | promoted |\n")
            out.write("|---|---|---|---|:---:|\n")
            for r in rows[:5]:
                out.write(
                    f"| `{r['params']}` "
                    f"| {r['disc'][0]:.3f}/{r['disc'][1]:.3f}/{r['disc'][2]}/{r['disc'][3]:.5f} "
                    f"| {r['val'][0]:.3f}/{r['val'][1]:.3f}/{r['val'][2]}/{r['val'][3]:.5f} "
                    f"| {r['hol'][0]:.3f}/{r['hol'][1]:.3f}/{r['hol'][2]}/{r['hol'][3]:.5f} "
                    f"| {'Y' if r['promoted'] else 'N'} |\n"
                )
            out.write("\n")

        out.write("## Promotion verdict\n\n")
        if promoted_rules:
            out.write(f"**{len(promoted_rules)} families promoted at least one rule satisfying lift+recall in all three splits:**\n\n")
            for fname, r in promoted_rules:
                out.write(f"- `{fname}` params={r['params']} — disc P={r['disc'][0]:.3f} R={r['disc'][1]:.3f} "
                          f"/ val P={r['val'][0]:.3f} R={r['val'][1]:.3f} / hol P={r['hol'][0]:.3f} R={r['hol'][1]:.3f}\n")
        else:
            out.write("**NO per-mint entry rule promoted under the hot-regime restriction.**\n\n")
            out.write("Even after restricting to hot-regime buckets, the per-mint features in V42F do not "
                      "produce a rule with consistent precision lift + non-zero recall across all three splits.\n")

    promo_out = {
        "regime_gate": promo.get("promoted_gate_name"),
        "promoted_entry_rules": [
            {"family": fname, **r} for fname, r in promoted_rules
        ],
        "v42f_rows_total": len(v42f),
        "v42f_rows_in_regime": len(hot_rows),
    }
    with open("/root/piggy/V43_PROMOTED_ENTRY_RULES.json", "w", encoding="utf-8") as fh:
        json.dump(promo_out, fh, default=str, indent=2)

    print(f"[v43-hot] wrote {REPORT} promoted_rules={len(promoted_rules)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
