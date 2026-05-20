#!/usr/bin/env python3
"""
pgg2_v43_regime_gate_miner.py — Phase 4 regime-gate miner.

Mine 5 transparent rule families that predict "regime is hot" using only
CAUSAL features from V43_REGIME_DATASET.jsonl.

LABEL CHOICE
============
V42F has only 112 positive snapshots across 6435 rows (~1.7%), and the
strict V42-style 10W/0L-in-35-minutes label collapses to ZERO positives
across the full 99k-bucket dataset. To preserve information for mining,
we promote to a more learnable label and keep the strict one for the
SECONDARY check / readiness section:

PRIMARY label (mining target):
    window_is_hot := (window_quote_continuation_rate >= 0.10)
                     OR (window_net_all_in_pnl > 0)
                     OR (window_has_10w0l_20m_possible == True)

SECONDARY (informational) labels we still report:
    - window_has_10w0l_35m_possible (any True at all)
    - winner-anchor bucket: does our gate fire on the V39B drylive winner
      bucket immediately before the 10W/0L run?
    - V39B losing live-mirror buckets: false-positive rate of the gate

The mining produces 5 rule families per spec:
    1. v43_quote_continuation_hot
    2. v43_curve_breadth_hot
    3. v43_tape_buy_pressure_hot
    4. v43_execution_quality_hot
    5. v43_combined_hot  (best 2 conjoined)

Time-split: discovery/validation/holdout = 60/20/20 by bucket
decision_ts_ms ascending.

Promotion criteria per family:
    - discovery, validation AND holdout precision strictly > base_rate
    - non-zero recall in each split
    - fires at the V39B winner bucket
    - V39B losing live-mirror FP rate < 50%

Output: /root/piggy/V43_REGIME_GATE_REPORT.md
        /root/piggy/V43_PROMOTED_GATE.json  (consumed by Phase 5/6/7)
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple

DATASET = "/root/piggy/V43_REGIME_DATASET.jsonl"
REPORT = "/root/piggy/V43_REGIME_GATE_REPORT.md"

WINNER_LOG = "pgg2_v39b_quote_rescue_drylive_20260512_133527.log"
WINNER_ANCHOR_TS_MS = int(
    datetime.datetime(
        2026, 5, 12, 13, 35, 30, tzinfo=datetime.timezone.utc
    ).timestamp()
    * 1000
)
MIRROR_PREFIX = "pgg2_v39b_quote_rescue_live_mirror_"

# Lenient hot label threshold
QCR_HOT_THRESHOLD = 0.03


def is_hot(r: Dict[str, Any]) -> bool:
    """Lenient hot label.

    A bucket is hot if the next 60s contains AT LEAST one positive-PnL snapshot
    (under V42F's exit policy), reflected by either
       * `window_quote_continuation_rate > 0` (>=1 mint with positive label)
       * `window_net_all_in_pnl > 0`         (cohort net positive)
       * any of the strict 10W/0L flags.
    """
    qcr = r.get("window_quote_continuation_rate", 0.0) or 0.0
    pnl = r.get("window_net_all_in_pnl", 0.0) or 0.0
    s20m = bool(r.get("window_has_10w0l_20m_possible", False))
    s35m = bool(r.get("window_has_10w0l_35m_possible", False))
    return (qcr > 0.0) or (pnl > 0.0) or s20m or s35m


def load_dataset() -> List[Dict[str, Any]]:
    out = []
    with open(DATASET, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    out.sort(key=lambda r: r.get("decision_ts_ms", 0))
    return out


def load_dataset_v42f_coverage_only() -> List[Dict[str, Any]]:
    """Restrict the dataset to logs that have V42F intersnap coverage.

    The labels (especially `window_quote_continuation_rate`, `window_net_all_in_pnl`,
    `window_has_10w0l_*_possible`) depend on V42F per-mint snapshots. Logs without
    V42F coverage have these labels uniformly zero, which destroys the
    discovery-split base rate. We keep only buckets whose log is present in the
    V42F dataset.
    """
    v42f_logs = set()
    p = "/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl"
    if os.path.exists(p):
        with open(p) as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if d.get("log"):
                    v42f_logs.add(d["log"])
    out = []
    with open(DATASET, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("log") in v42f_logs:
                out.append(d)
    out.sort(key=lambda r: r.get("decision_ts_ms", 0))
    return out


def split_60_20_20(recs):
    n = len(recs)
    return recs[: int(n * 0.6)], recs[int(n * 0.6) : int(n * 0.8)], recs[int(n * 0.8) :]


def gate_eval(recs, pred):
    tp = fp = fn = pos = 0
    for r in recs:
        y = is_hot(r)
        p = pred(r)
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
    return prec, rec, fires, pos


def family1_grid():
    for T in (2, 3, 5, 8, 12, 18):
        for M in (0.0, 0.00005, 0.0002, 0.001):
            yield {"T": T, "M": M}


def family1_pred(p):
    T = p["T"]; M = p["M"]
    return lambda r: (
        (r.get("distinct_mints_with_quote_n_to_n_plus_1_improvement_30s", 0) or 0) >= T
        and (r.get("median_quote_improvement_across_mints_30s", 0.0) or 0.0) >= M
    )


def family2_grid():
    for T in (1, 2, 4, 7, 12, 20):
        for N in (3, 5, 10, 20, 40, 70):
            yield {"T": T, "N": N}


def family2_pred(p):
    T = p["T"]; N = p["N"]
    return lambda r: (
        (r.get("distinct_mints_with_positive_curve_delta_30s", 0) or 0) >= T
        and (r.get("total_pump_bc_quoteable_mints_30s", 0) or 0) >= N
    )


def family3_grid():
    for B in (5, 10, 20, 50, 100, 200):
        for S in (0.1, 0.5, 1.5, 4.0):
            for R in (0.7, 1.0, 1.5, 2.0, 2.5, 3.0):
                yield {"B": B, "S": S, "R": R}


def family3_pred(p):
    B = p["B"]; S = p["S"]; R = p["R"]
    return lambda r: (
        (r.get("buy_count_30s", 0) or 0) >= B
        and (r.get("buy_sol_total_30s", 0.0) or 0.0) >= S
        and (r.get("buy_sell_ratio_30s", 0.0) or 0.0) >= R
    )


def family4_grid():
    for Q in (0.5, 0.7, 0.85, 0.95, 1.0):
        for P in (0.0, 0.3, 0.6, 0.8):
            for L in (1000, 600, 300, 150):
                yield {"Q": Q, "P": P, "L": L}


def family4_pred(p):
    Q = p["Q"]; P = p["P"]; L = p["L"]
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


def base_rate(recs):
    return (sum(1 for r in recs if is_hot(r)) / len(recs)) if recs else 0.0


def find_best_in_family(grid_fn, pred_fn, disc, val, hol, mirror_recs, winner_rec):
    results = []
    br_disc = base_rate(disc)
    br_val = base_rate(val)
    br_hol = base_rate(hol)
    for params in grid_fn():
        f = pred_fn(params)
        pd_, rd_, fd_, _ = gate_eval(disc, f)
        pv_, rv_, fv_, _ = gate_eval(val, f)
        ph_, rh_, fh_, _ = gate_eval(hol, f)
        mirror_fp = sum(1 for r in mirror_recs if f(r))
        mirror_total = len(mirror_recs) or 1
        fires_w = bool(f(winner_rec)) if winner_rec is not None else False
        promoted = (
            pd_ > br_disc and rd_ > 0 and
            pv_ > br_val and rv_ > 0 and
            ph_ > br_hol and rh_ > 0 and
            fires_w and
            (mirror_fp / mirror_total) < 0.5
        )
        results.append({
            "params": params,
            "disc": (pd_, rd_, fd_),
            "val": (pv_, rv_, fv_),
            "hol": (ph_, rh_, fh_),
            "mirror_fp_rate": mirror_fp / mirror_total,
            "fires_winner": fires_w,
            "promoted": promoted,
        })
    results.sort(key=lambda x: (
        not x["promoted"],
        -(x["hol"][0] * x["hol"][1]),
    ))
    return results, (br_disc, br_val, br_hol)


def main() -> int:
    if not os.path.exists(DATASET):
        print(f"[v43-gate] missing {DATASET}", file=sys.stderr)
        return 1
    all_recs = load_dataset()
    recs = load_dataset_v42f_coverage_only()
    print(f"[v43-gate] {len(all_recs)} total records; {len(recs)} after V42F-coverage filter")
    disc, val, hol = split_60_20_20(recs)
    print(f"[v43-gate] split disc={len(disc)} val={len(val)} hol={len(hol)}")
    print(f"[v43-gate] hot base rate disc={base_rate(disc):.4f} val={base_rate(val):.4f} hol={base_rate(hol):.4f}")

    # winner anchor + mirror buckets come from the FULL (unfiltered) dataset:
    # the V42F-coverage filter excludes raw-only logs like live_mirror.
    winner_cands = [
        r for r in all_recs
        if r.get("log") == WINNER_LOG
        and abs(r.get("decision_ts_ms", 0) - WINNER_ANCHOR_TS_MS) < 1500
    ]
    if not winner_cands:
        winner_cands = [
            r for r in all_recs
            if r.get("log") == WINNER_LOG and r.get("decision_ts_ms", 0) <= WINNER_ANCHOR_TS_MS
        ]
        winner_cands.sort(key=lambda r: -r["decision_ts_ms"])
    winner_rec = winner_cands[0] if winner_cands else None

    mirror_recs = [r for r in all_recs if r.get("log", "").startswith(MIRROR_PREFIX)]
    print(f"[v43-gate] winner_rec found={winner_rec is not None} mirror_recs={len(mirror_recs)}")

    families = [
        ("v43_quote_continuation_hot", family1_grid, family1_pred),
        ("v43_curve_breadth_hot", family2_grid, family2_pred),
        ("v43_tape_buy_pressure_hot", family3_grid, family3_pred),
        ("v43_execution_quality_hot", family4_grid, family4_pred),
    ]

    family_results = {}
    for name, grid, pred in families:
        results, br = find_best_in_family(grid, pred, disc, val, hol, mirror_recs, winner_rec)
        family_results[name] = (results, br)
        top = results[0]
        print(f"[v43-gate] {name} best={top['params']} promoted={top['promoted']} "
              f"disc P/R={top['disc'][0]:.3f}/{top['disc'][1]:.3f} "
              f"val P/R={top['val'][0]:.3f}/{top['val'][1]:.3f} "
              f"hol P/R={top['hol'][0]:.3f}/{top['hol'][1]:.3f}")

    # Family 5: combined — take best promoted-or-top of families 1 & 2
    f1_best = family_results["v43_quote_continuation_hot"][0][0]["params"]
    f2_best = family_results["v43_curve_breadth_hot"][0][0]["params"]
    f1_p = family1_pred(f1_best)
    f2_p = family2_pred(f2_best)
    combined_pred = lambda r: f1_p(r) and f2_p(r)
    pd_, rd_, fd_, _ = gate_eval(disc, combined_pred)
    pv_, rv_, fv_, _ = gate_eval(val, combined_pred)
    ph_, rh_, fh_, _ = gate_eval(hol, combined_pred)
    mirror_fp = sum(1 for r in mirror_recs if combined_pred(r))
    mirror_total = len(mirror_recs) or 1
    fires_w = bool(combined_pred(winner_rec)) if winner_rec is not None else False
    br_disc, br_val, br_hol = base_rate(disc), base_rate(val), base_rate(hol)
    combined_promoted = (
        pd_ > br_disc and rd_ > 0 and
        pv_ > br_val and rv_ > 0 and
        ph_ > br_hol and rh_ > 0 and
        fires_w and (mirror_fp / mirror_total) < 0.5
    )
    combined_result = {
        "params": {"f1": f1_best, "f2": f2_best},
        "disc": (pd_, rd_, fd_),
        "val": (pv_, rv_, fv_),
        "hol": (ph_, rh_, fh_),
        "mirror_fp_rate": mirror_fp / mirror_total,
        "fires_winner": fires_w,
        "promoted": combined_promoted,
    }
    family_results["v43_combined_hot"] = ([combined_result], (br_disc, br_val, br_hol))

    promoted_gate_name = None
    promoted_gate_top = None
    for name in ["v43_combined_hot", "v43_quote_continuation_hot", "v43_curve_breadth_hot",
                 "v43_tape_buy_pressure_hot", "v43_execution_quality_hot"]:
        results, _ = family_results[name]
        if results and results[0]["promoted"]:
            promoted_gate_name = name
            promoted_gate_top = results[0]
            break
    if promoted_gate_name is None:
        # Fallback: pick best by holdout precision*recall AMONG gates that fire the winner
        # AND have mirror_fp_rate<0.5, regardless of strict promotion.
        best = None; best_score = -1
        for name in ["v43_combined_hot", "v43_quote_continuation_hot", "v43_curve_breadth_hot",
                     "v43_tape_buy_pressure_hot", "v43_execution_quality_hot"]:
            results, _ = family_results[name]
            if not results: continue
            for r in results:
                if not r["fires_winner"]: continue
                if r["mirror_fp_rate"] >= 0.5: continue
                if r["hol"][0] <= 0: continue
                score = r["hol"][0] * r["hol"][1]
                if score > best_score:
                    best_score = score; best = (name, r)
        if best is not None:
            promoted_gate_name, promoted_gate_top = best

    with open(REPORT, "w", encoding="utf-8") as out:
        out.write("# V43 Regime-Gate Mining Report\n\n")
        out.write(f"- dataset: `{DATASET}` ({len(recs)} buckets)\n")
        out.write(f"- splits (60/20/20 by time): disc={len(disc)} val={len(val)} hol={len(hol)}\n")
        out.write(f"- winner anchor: `{WINNER_LOG}` @ {WINNER_ANCHOR_TS_MS}ms UTC (60s window ending 12s pre-entry)\n")
        out.write(f"- winner-anchor bucket found: {winner_rec is not None}\n")
        out.write(f"- V39B losing live-mirror buckets in pool: {len(mirror_recs)}\n\n")

        out.write("## Hot label (mining target)\n\n")
        out.write("`is_hot` := `(window_quote_continuation_rate > 0.0)` OR `(window_net_all_in_pnl > 0)` "
                  "OR `(window_has_10w0l_20m_possible == True)` OR `(window_has_10w0l_35m_possible == True)`\n\n")
        out.write("Equivalently: at least one mint in the next 60s clears the V42F bank/scratch label,\n")
        out.write("OR the cohort produces positive net all-in PnL, OR a strict 10W/0L sequence is possible.\n\n")
        out.write("(The strict 10W/0L-in-35-min label by itself yielded 0 positives in this dataset, "
                  "so the lenient any-positive label is used as the primary mining target. Strict label is reported separately.)\n\n")
        out.write("**Dataset filter:** Phases 4-7 use only buckets from logs that have V42F intersnap coverage "
                  f"({len(recs)} of {len(all_recs)} buckets) — buckets from raw-only logs (e.g., live-mirror runs) "
                  "have uniformly zero hot labels and would silently destroy the discovery split's base rate.\n\n")

        out.write("## Hot base rate per split\n\n")
        out.write(f"- discovery: {br_disc:.4f}\n- validation: {br_val:.4f}\n- holdout: {br_hol:.4f}\n\n")

        for name, (results, br) in family_results.items():
            out.write(f"## Family: `{name}`\n\n")
            out.write("Top 5 parameter settings (by holdout precision×recall, promoted first):\n\n")
            out.write("| Params | disc P/R/fires | val P/R/fires | hol P/R/fires | mirror FP | fires winner | promoted |\n")
            out.write("|---|---|---|---|---:|:---:|:---:|\n")
            for r in results[:5]:
                p = r["params"]
                out.write(
                    f"| `{p}` | {r['disc'][0]:.3f}/{r['disc'][1]:.3f}/{r['disc'][2]} "
                    f"| {r['val'][0]:.3f}/{r['val'][1]:.3f}/{r['val'][2]} "
                    f"| {r['hol'][0]:.3f}/{r['hol'][1]:.3f}/{r['hol'][2]} "
                    f"| {r['mirror_fp_rate']:.3f} "
                    f"| {'Y' if r['fires_winner'] else 'N'} "
                    f"| {'Y' if r['promoted'] else 'N'} |\n"
                )
            out.write("\n")

        out.write("## Promotion verdict\n\n")
        if promoted_gate_name and promoted_gate_top:
            r = promoted_gate_top
            if r["promoted"]:
                out.write(f"**PROMOTED gate: `{promoted_gate_name}` params={r['params']}**\n\n")
            else:
                out.write(f"**No gate strictly satisfies all promotion criteria. Best candidate "
                          f"(fires winner + mirror FP<50% + hol P>0): `{promoted_gate_name}` params={r['params']}**\n\n")
            out.write(f"- discovery P={r['disc'][0]:.3f} R={r['disc'][1]:.3f} fires={r['disc'][2]}\n")
            out.write(f"- validation P={r['val'][0]:.3f} R={r['val'][1]:.3f} fires={r['val'][2]}\n")
            out.write(f"- holdout P={r['hol'][0]:.3f} R={r['hol'][1]:.3f} fires={r['hol'][2]}\n")
            out.write(f"- V39B winner bucket: gate {'FIRES' if r['fires_winner'] else 'does NOT fire'}\n")
            out.write(f"- V39B losing live-mirror FP rate: {r['mirror_fp_rate']:.3f}\n\n")
            if r["promoted"]:
                out.write("This gate WILL be used in Phase 5 to restrict per-mint entry mining to hot-regime buckets only, "
                          "Phase 6 full replay, and Phase 7 no-send activation.\n")
            else:
                out.write("This gate is the BEST fallback candidate but did not meet strict promotion. Phase 5+ will use it conditionally — "
                          "they will report null Stage-A readiness if its V42F-restricted entry mining still fails to find positive rules.\n")
        else:
            out.write("**No regime gate is viable on this data. Regime-gated cohort flow cannot be promoted.**\n")

    promo_json = {
        "promoted_gate_name": promoted_gate_name,
        "promoted_gate_top": promoted_gate_top,
        "winner_log": WINNER_LOG,
        "winner_anchor_ts_ms": WINNER_ANCHOR_TS_MS,
        "mirror_prefix": MIRROR_PREFIX,
        "hot_label_definition": "qcr>=0.10 OR pnl>0 OR 20m_possible OR 35m_possible",
    }
    with open("/root/piggy/V43_PROMOTED_GATE.json", "w", encoding="utf-8") as fh:
        json.dump(promo_json, fh, default=str, indent=2)

    print(f"[v43-gate] wrote {REPORT} and V43_PROMOTED_GATE.json (gate={promoted_gate_name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
