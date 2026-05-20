#!/usr/bin/env python3
"""
pgg2_v43_regime_forensic.py — Phase 3 forensic comparator.

Compare the regime-feature vector of the "should-have-been-hot" 60s window
ENDING at 13:35:30 UTC on 2026-05-12 (just before the first V39B winner
entry at 13:35:42) against the same time-resolution windows in:
    - V39B losing live-mirror runs ~16:00..17:30 UTC same day
    - V42D capture window
    - V42E 600s capture
    - all other historical idle/losing windows in V43_REGIME_DATASET.jsonl

For each regime feature, compute:
    winner_value
    loser_mean, loser_std
    separation_score = (winner_value - loser_mean) / (loser_std + epsilon)

Output: /root/piggy/V43_10W_REGIME_SIGNATURE.md
"""
from __future__ import annotations

import datetime
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from typing import Any, Dict, List

DATASET = "/root/piggy/V43_REGIME_DATASET.jsonl"
REPORT = "/root/piggy/V43_10W_REGIME_SIGNATURE.md"

# Winner anchor: V39B drylive winner started 13:35:42 UTC on 2026-05-12.
# We pick the bucket whose decision_ts is at or just before 13:35:30 UTC
# (i.e. the 60s window ending 12s before the first entry).
WINNER_LOG = "pgg2_v39b_quote_rescue_drylive_20260512_133527.log"
WINNER_ANCHOR_TS_MS = int(
    datetime.datetime(2026, 5, 12, 13, 35, 30, tzinfo=datetime.timezone.utc).timestamp()
    * 1000
)

LOSER_LOG_PREFIXES = (
    "pgg2_v39b_quote_rescue_live_mirror_",
    "pgg2_v42d_",
    "pgg2_v42e_",
    "pgg2_v42b_capture_",
    "pgg2_v42_capture_",
    "pgg2_v30_",
)

FEATURE_KEYS = [
    "total_pump_bc_quoteable_mints_5s",
    "total_pump_bc_quoteable_mints_10s",
    "total_pump_bc_quoteable_mints_30s",
    "total_pump_bc_quoteable_mints_60s",
    "total_pump_bc_quoteable_mints_120s",
    "total_created_mints_5s",
    "total_created_mints_10s",
    "total_created_mints_30s",
    "total_created_mints_60s",
    "total_created_mints_120s",
    "total_shred_events_5s",
    "total_shred_events_10s",
    "total_shred_events_30s",
    "total_shred_events_60s",
    "buy_count_5s",
    "buy_count_10s",
    "buy_count_30s",
    "buy_count_60s",
    "sell_count_5s",
    "sell_count_10s",
    "sell_count_30s",
    "sell_count_60s",
    "buy_sol_total_5s",
    "buy_sol_total_10s",
    "buy_sol_total_30s",
    "buy_sol_total_60s",
    "sell_sol_total_5s",
    "sell_sol_total_10s",
    "sell_sol_total_30s",
    "sell_sol_total_60s",
    "buy_sell_ratio_5s",
    "buy_sell_ratio_30s",
    "buy_sell_ratio_60s",
    "distinct_mints_with_positive_quote_gradient_30s",
    "distinct_mints_with_sell_quote_improvement_gt_0p0002_30s",
    "distinct_mints_with_positive_curve_delta_30s",
    "distinct_mints_with_quote_n_to_n_plus_1_improvement_30s",
    "median_quote_improvement_across_mints_30s",
    "p90_quote_improvement_across_mints_30s",
    "distinct_mints_that_would_have_banked_under_label_60s",
    "distinct_mints_that_would_have_lost_under_label_60s",
    "cohort_hit_rate_60s",
    "quote_latency_p50_30s",
    "quote_latency_p90_30s",
    "pair_source_current_sig_rate_60s",
    "sim_needed_0_rate_60s",
    "feed_health_active_decisions_30s",
]


def load_dataset() -> List[Dict[str, Any]]:
    out = []
    with open(DATASET, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def main() -> int:
    if not os.path.exists(DATASET):
        print(f"[v43-forensic] missing {DATASET}", file=sys.stderr)
        return 1
    recs = load_dataset()
    print(f"[v43-forensic] {len(recs)} records loaded")

    # Identify winner bucket
    winner_recs = [
        r for r in recs
        if r.get("log") == WINNER_LOG
        and abs(r.get("decision_ts_ms", 0) - WINNER_ANCHOR_TS_MS) < 1500
    ]
    if not winner_recs:
        # fall back to the closest bucket ≤ WINNER_ANCHOR_TS_MS within the same log
        cand = [
            r for r in recs
            if r.get("log") == WINNER_LOG
            and r.get("decision_ts_ms", 0) <= WINNER_ANCHOR_TS_MS
        ]
        cand.sort(key=lambda r: -r.get("decision_ts_ms", 0))
        winner_recs = cand[:1]

    if not winner_recs:
        print(f"[v43-forensic] FATAL: no winner-anchor bucket found for {WINNER_LOG}", file=sys.stderr)
        return 1
    winner = winner_recs[0]
    print(f"[v43-forensic] winner bucket ts={winner['decision_ts_ms']} log={winner['log']}")

    # Build loser pool: buckets from losing/idle logs.
    # Exclude the V39B winner log itself.
    losers: List[Dict[str, Any]] = []
    for r in recs:
        log = r.get("log", "")
        if log == WINNER_LOG:
            continue
        if any(log.startswith(p) for p in LOSER_LOG_PREFIXES):
            losers.append(r)
    print(f"[v43-forensic] loser pool size = {len(losers)}")

    # Per-feature winner/loser stats
    rows = []
    for k in FEATURE_KEYS:
        try:
            wv = float(winner.get(k, 0) or 0)
        except Exception:
            wv = 0.0
        lvals = []
        for r in losers:
            try:
                lvals.append(float(r.get(k, 0) or 0))
            except Exception:
                pass
        if not lvals:
            continue
        lmean = statistics.fmean(lvals)
        lsd = statistics.pstdev(lvals) if len(lvals) > 1 else 1.0
        sep = (wv - lmean) / (lsd + 1e-9)
        lmax = max(lvals)
        # Also percentile of winner value within loser distribution
        below = sum(1 for x in lvals if x < wv)
        pct = below / len(lvals)
        rows.append({
            "feature": k,
            "winner": wv,
            "loser_mean": lmean,
            "loser_std": lsd,
            "loser_max": lmax,
            "separation_score": sep,
            "winner_percentile_within_losers": pct,
        })

    rows.sort(key=lambda r: abs(r["separation_score"]), reverse=True)

    # Anchored separations to specific loser families
    families = {
        "v39b_live_mirror": [r for r in losers if r["log"].startswith("pgg2_v39b_quote_rescue_live_mirror_")],
        "v42_capture": [r for r in losers if r["log"].startswith(("pgg2_v42_capture_", "pgg2_v42b_capture_"))],
        "v30_shadowlab": [r for r in losers if r["log"].startswith("pgg2_v30_")],
    }

    with open(REPORT, "w", encoding="utf-8") as out:
        out.write("# V43 10W/0L Regime-Signature Forensic\n\n")
        out.write(f"- winner reference: `{WINNER_LOG}`\n")
        out.write(f"- winner anchor (UTC ms): {WINNER_ANCHOR_TS_MS} (60s window ending 12s before first entry)\n")
        out.write(f"- winner bucket selected: ts_ms={winner.get('decision_ts_ms')} ({datetime.datetime.utcfromtimestamp(winner['decision_ts_ms']/1000)} UTC)\n")
        out.write(f"- loser pool size: {len(losers)} buckets\n\n")

        out.write("## Top 25 features by absolute separation score\n\n")
        out.write("| Feature | Winner | Loser mean | Loser std | Loser max | Sep score | Winner pct |\n")
        out.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for r in rows[:25]:
            out.write(
                f"| `{r['feature']}` | {r['winner']:.4f} | {r['loser_mean']:.4f} | {r['loser_std']:.4f} | {r['loser_max']:.4f} | {r['separation_score']:+.3f} | {r['winner_percentile_within_losers']*100:.1f}% |\n"
            )

        out.write("\n## Family-stratified comparisons (winner vs each loser family)\n\n")
        for fname, fbuckets in families.items():
            out.write(f"\n### Family `{fname}` ({len(fbuckets)} buckets)\n\n")
            if not fbuckets:
                out.write("_(empty)_\n")
                continue
            fam_rows = []
            for k in FEATURE_KEYS:
                try:
                    wv = float(winner.get(k, 0) or 0)
                except Exception:
                    wv = 0.0
                fvals = []
                for r in fbuckets:
                    try:
                        fvals.append(float(r.get(k, 0) or 0))
                    except Exception:
                        pass
                if not fvals:
                    continue
                fmean = statistics.fmean(fvals)
                fsd = statistics.pstdev(fvals) if len(fvals) > 1 else 1.0
                sep = (wv - fmean) / (fsd + 1e-9)
                below = sum(1 for x in fvals if x < wv)
                fam_rows.append((abs(sep), k, wv, fmean, fsd, sep, below/len(fvals)))
            fam_rows.sort(reverse=True)
            out.write("| Feature | Winner | Family mean | Family std | Sep score | Winner pct |\n")
            out.write("|---|---:|---:|---:|---:|---:|\n")
            for _, k, wv, fmean, fsd, sep, pct in fam_rows[:15]:
                out.write(f"| `{k}` | {wv:.4f} | {fmean:.4f} | {fsd:.4f} | {sep:+.3f} | {pct*100:.1f}% |\n")

        # Conclusion
        top = rows[:5] if rows else []
        # Count how many top-5 features had a winner percentile > 90%
        strong = [r for r in top if r["winner_percentile_within_losers"] > 0.9]
        very_strong = [r for r in rows[:25] if r["winner_percentile_within_losers"] > 0.95]
        out.write("\n## Conclusion\n\n")
        if strong:
            out.write(f"The winning V39B window was **categorically different** from losing/idle windows:\n")
            for r in strong:
                out.write(
                    f"- `{r['feature']}`: winner={r['winner']:.4f} vs loser-mean={r['loser_mean']:.4f} (winner is at percentile **{r['winner_percentile_within_losers']*100:.1f}%** within loser distribution; separation score {r['separation_score']:+.2f}).\n"
                )
            out.write(f"\n**Verdict: REGIME SIGNATURE DETECTABLE CAUSALLY.** {len(very_strong)} features exceed the 95th-percentile threshold, "
                      "indicating a causal regime gate is feasible.\n")
        elif rows:
            out.write("No feature had winner-value > 90th percentile within loser distribution.\n")
            out.write("**Verdict: REGIME SIGNATURE WEAK.** The winner window does not look "
                      "qualitatively different from losing windows by these regime features alone.\n")
            for r in top:
                out.write(
                    f"- `{r['feature']}`: winner={r['winner']:.4f} loser-mean={r['loser_mean']:.4f} sep={r['separation_score']:+.2f} pct={r['winner_percentile_within_losers']*100:.1f}%\n"
                )

    print(f"[v43-forensic] wrote {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
