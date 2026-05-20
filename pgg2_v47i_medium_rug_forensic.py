"""V47I — 500-1000ms medium-rug forensic.

V47H Phase 6 left 2 losses in the 500-1000ms window:
  - CNk6t2GAECsGbQtEXmgB1qQu8Hu3grxAmfCa4fsYrJkQ at 514ms (clamp_loss -0.001472)
  - DxPaAa15THHBKYm1ZsMtMLs5twEUmKgMrBpGAB2wpump at 991ms (expired_loss -0.000157)

Both rugged AFTER our buy. The V47G watchdog correctly fired emergency exits;
V47I's question is whether the rug precursors are detectable PRE-ENTRY in the
500-1000ms window using only data persisted in v47h_no_send_decisions.jsonl.

This module reads V47H + V47F + V47E JSONLs, joins entry+close, extracts the
features that exist on each record, and produces a comparison table.

Limitations honestly documented:
  - V47F and V47E entries do NOT persist 500ms / 1000ms buyer windows or
    sell-side breadth or curve-history. Only V47H persists those. So most
    inter-cohort comparisons are sparse.
  - V47H records persist:
      pending_buy_count_{50,100,250,500,1000}ms
      pending_buy_sol_{50,100,250,500,1000}ms
      unique_buyers_{50,100,250,500,1000}ms
      pending_sell_count_250ms / pending_sell_sol_250ms / unique_sellers_250ms
      v47h_sell_count_250ms / v47h_unique_sellers_250ms
      v47h_curve_deltas_500ms_len / v47h_curve_deltas_1000ms_len  (LENGTHS only)
    The actual curve delta values are NOT persisted. Curve velocity /
    deceleration cannot be recovered.
  - Quote history values are NOT persisted. Quote weakening cannot be
    recovered.

Outputs a Markdown report to V47I_500_1000MS_RUG_FORENSIC.md.

PURE READ-ONLY. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import json
import os
import re as _re
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


_FORBIDDEN_CALL_PATTERNS = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN_CALL_PATTERNS:
    if _re.search(_pat, _src):
        sys.stderr.write(
            f"V47I-MEDIUM-RUG-FORENSIC-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47i_forensic")


V47H_JSONL = "/root/piggy/data/v47h_no_send_decisions.jsonl"
V47F_JSONL = "/root/piggy/data/v47f_drylive_decisions.jsonl"
V47E_JSONL = "/root/piggy/data/v47e_drylive_decisions.jsonl"
REPORT_PATH = "/root/piggy/V47I_500_1000MS_RUG_FORENSIC.md"


def _short(m: str) -> str:
    if not m or len(m) < 6:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if isinstance(r, dict):
                    out.append(r)
            except Exception:
                pass
    return out


def _v47h_join(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join v47h_candidate with v47h_observed/v47h_observed_watchdog by
    (mint, decision_ts_ms)."""
    cands = [r for r in recs if r.get("type") == "v47h_candidate"]
    obs_map: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for r in recs:
        if r.get("type") in ("v47h_observed", "v47h_observed_watchdog"):
            k = (r.get("mint", ""), int(r.get("decision_ts_ms") or 0))
            obs_map[k] = r
    joined = []
    for c in cands:
        k = (c.get("mint", ""), int(c.get("decision_ts_ms") or 0))
        m = dict(c)
        o = obs_map.get(k)
        if o is not None:
            for kk in (
                "observed_label_pnl",
                "observed_label_kind",
                "observed_label_lag_ms",
                "v47g_watchdog_action",
                "v47g_watchdog_reason",
                "v47g_midhold_abort_action",
                "v47g_midhold_abort_reason",
            ):
                if kk in o and m.get(kk) is None:
                    m[kk] = o[kk]
        joined.append(m)
    return joined


def _vf_ve_join(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join {v47f,v47e}_drylive_entry with corresponding close record."""
    entries = [r for r in recs if r.get("type", "").endswith("_drylive_entry")]
    closes = [r for r in recs if r.get("type", "").endswith("_drylive_close")]
    cmap: Dict[Tuple[str, int, float], Dict[str, Any]] = {}
    for r in closes:
        k = (
            r.get("mint", ""),
            int(r.get("decision_ts_ms") or 0),
            float(r.get("selected_size_sol") or 0.0),
        )
        cmap[k] = r
    joined = []
    for e in entries:
        k = (
            e.get("mint", ""),
            int(e.get("decision_ts_ms") or 0),
            float(e.get("selected_size_sol") or 0.0),
        )
        m = dict(e)
        c = cmap.get(k)
        if c is not None:
            for kk in ("close_pnl", "close_kind", "close_lag_ms"):
                if c.get(kk) is not None:
                    m[kk] = c[kk]
        joined.append(m)
    return joined


def _classify_v47h(r: Dict[str, Any]) -> str:
    pnl = r.get("observed_label_pnl")
    if pnl is None:
        return "unknown"
    try:
        pnl_f = float(pnl)
    except Exception:
        return "unknown"
    if pnl_f >= 0.0:
        return "bank_or_nonneg"
    return "loss"


def _classify_close(r: Dict[str, Any]) -> str:
    pnl = r.get("close_pnl")
    ck = r.get("close_kind")
    if pnl is None:
        return "unknown"
    try:
        pnl_f = float(pnl)
    except Exception:
        return "unknown"
    if pnl_f >= 0.0:
        return "bank_or_nonneg"
    return "loss"


def _format_table_v47h(rows: List[Dict[str, Any]]) -> str:
    lines = []
    header = (
        "| mint | epnl | ub250 | tbs250 | pbsol250 | pbsol500 | pbsol1000 | "
        "ub500 | ub1000 | psc250 | us250 | curve500_n | curve1000_n | "
        "obs_pnl | obs_kind | lag_ms |"
    )
    lines.append(header)
    lines.append(
        "|------|------|-------|--------|----------|----------|-----------|"
        "-------|--------|--------|-------|-----------|-------------|"
        "---------|----------|--------|"
    )
    for r in rows:
        lines.append(
            f"| {_short(r.get('mint',''))} | "
            f"{float(r.get('expected_pnl') or 0):.5f} | "
            f"{int(r.get('unique_buyers_250ms') or 0)} | "
            f"{float(r.get('top_buyer_share_250ms') or 0):.3f} | "
            f"{float(r.get('pending_buy_sol_250ms') or 0):.3f} | "
            f"{float(r.get('pending_buy_sol_500ms') or 0):.3f} | "
            f"{float(r.get('pending_buy_sol_1000ms') or 0):.3f} | "
            f"{int(r.get('unique_buyers_500ms') or 0)} | "
            f"{int(r.get('unique_buyers_1000ms') or 0)} | "
            f"{int(r.get('pending_sell_count_250ms') or 0)} | "
            f"{int(r.get('unique_sellers_250ms') or 0)} | "
            f"{int(r.get('v47h_curve_deltas_500ms_len') or 0)} | "
            f"{int(r.get('v47h_curve_deltas_1000ms_len') or 0)} | "
            f"{float(r.get('observed_label_pnl') or 0):+.5f} | "
            f"{r.get('observed_label_kind') or '-'} | "
            f"{int(r.get('observed_label_lag_ms') or 0)} |"
        )
    return "\n".join(lines)


def _format_table_vfe(label: str, rows: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append(f"### {label}\n")
    header = (
        "| mint | sz | ub250 | tbs250 | exp_pnl | adv_pnl | close_kind | "
        "close_pnl | close_lag_ms |"
    )
    lines.append(header)
    lines.append(
        "|------|----|-------|--------|---------|---------|------------|"
        "-----------|--------------|"
    )
    for r in rows:
        lines.append(
            f"| {_short(r.get('mint',''))} | "
            f"{float(r.get('selected_size_sol') or 0):.3f} | "
            f"{int(r.get('ub_250') or 0)} | "
            f"{float(r.get('tbs_250') or 0):.3f} | "
            f"{float(r.get('exp_pnl') or 0):+.5f} | "
            f"{float(r.get('adv_pnl') or 0):+.5f} | "
            f"{r.get('close_kind') or '-'} | "
            f"{float(r.get('close_pnl') or 0):+.5f} | "
            f"{int(r.get('close_lag_ms') or 0)} |"
        )
    return "\n".join(lines)


def _compute_stats(rows: List[Dict[str, Any]], key: str) -> Dict[str, float]:
    vals = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except Exception:
            pass
    if not vals:
        return {"n": 0, "min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0}
    s = sorted(vals)
    n = len(s)
    return {
        "n": n,
        "min": s[0],
        "max": s[-1],
        "mean": sum(s) / n,
        "median": s[n // 2],
    }


def main() -> int:
    v47h_recs = _load_jsonl(V47H_JSONL)
    v47f_recs = _load_jsonl(V47F_JSONL)
    v47e_recs = _load_jsonl(V47E_JSONL)

    v47h_joined = _v47h_join(v47h_recs)
    v47f_joined = _vf_ve_join(v47f_recs)
    v47e_joined = _vf_ve_join(v47e_recs)

    # Split V47H rows into the 2 target rugs vs banks vs others.
    rugs_500_1000 = [
        r for r in v47h_joined
        if _classify_v47h(r) == "loss"
        and r.get("observed_label_lag_ms") is not None
        and 500 <= int(r.get("observed_label_lag_ms") or 0) <= 1000
    ]
    banks = [
        r for r in v47h_joined
        if _classify_v47h(r) == "bank_or_nonneg"
    ]
    other_losses = [
        r for r in v47h_joined
        if _classify_v47h(r) == "loss"
        and r.get("observed_label_lag_ms") is not None
        and not (500 <= int(r.get("observed_label_lag_ms") or 0) <= 1000)
    ]

    # ===== Compute features per cohort =====
    feature_keys = [
        "expected_pnl",
        "unique_buyers_250ms",
        "unique_buyers_500ms",
        "unique_buyers_1000ms",
        "top_buyer_share_250ms",
        "pending_buy_sol_250ms",
        "pending_buy_sol_500ms",
        "pending_buy_sol_1000ms",
        "pending_buy_count_250ms",
        "pending_buy_count_500ms",
        "pending_buy_count_1000ms",
        "pending_sell_count_250ms",
        "unique_sellers_250ms",
        "v47h_curve_deltas_500ms_len",
        "v47h_curve_deltas_1000ms_len",
    ]

    stats_rugs = {k: _compute_stats(rugs_500_1000, k) for k in feature_keys}
    stats_banks = {k: _compute_stats(banks, k) for k in feature_keys}

    # ===== Inter-cohort delta analysis =====
    # For each feature, compute (rug_median - bank_median).
    delta_analysis = []
    for k in feature_keys:
        sr = stats_rugs[k]
        sb = stats_banks[k]
        delta = sr["median"] - sb["median"]
        delta_analysis.append((k, sr["median"], sb["median"], delta))

    # ===== Distinguishing-feature search =====
    # Find features where the 2 rugs both lie OUTSIDE the bank min..max range,
    # or where the rugs both lie at one extreme of the bank distribution.
    distinguishing = []
    for k in feature_keys:
        rug_vals = [float(r.get(k) or 0) for r in rugs_500_1000]
        bank_vals = [float(r.get(k) or 0) for r in banks]
        if not rug_vals or not bank_vals:
            continue
        bank_min = min(bank_vals)
        bank_max = max(bank_vals)
        bank_median = sorted(bank_vals)[len(bank_vals) // 2]
        all_rugs_below = all(rv < bank_min for rv in rug_vals)
        all_rugs_above = all(rv > bank_max for rv in rug_vals)
        all_rugs_below_median = all(rv < bank_median for rv in rug_vals)
        all_rugs_above_median = all(rv > bank_median for rv in rug_vals)
        verdict = "no_separation"
        if all_rugs_below:
            verdict = "rugs_strictly_below_bank_range"
        elif all_rugs_above:
            verdict = "rugs_strictly_above_bank_range"
        elif all_rugs_below_median:
            verdict = "rugs_below_bank_median"
        elif all_rugs_above_median:
            verdict = "rugs_above_bank_median"
        distinguishing.append((k, rug_vals, bank_min, bank_max, bank_median, verdict))

    # ===== Build report =====
    lines = []
    lines.append("# V47I 500-1000ms Medium-Rug Forensic")
    lines.append("")
    lines.append(f"generated_ts_utc: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    lines.append("")
    lines.append("## TL;DR (top of report)")
    lines.append("")
    lines.append(
        f"- V47H joined records: {len(v47h_joined)}  "
        f"(rugs in 500-1000ms window: {len(rugs_500_1000)}, "
        f"banks_or_nonneg: {len(banks)}, "
        f"other_losses: {len(other_losses)})"
    )
    lines.append(f"- V47F joined entries: {len(v47f_joined)}")
    lines.append(f"- V47E joined entries: {len(v47e_joined)}")
    lines.append("")
    lines.append("### Features that uniquely BLOCK both CNk6 and DxPa (out-of-range vs banks)")
    lines.append("")
    has_strict_separation = False
    for (k, rug_vals, bmin, bmax, bmed, v) in distinguishing:
        if v in ("rugs_strictly_below_bank_range", "rugs_strictly_above_bank_range"):
            has_strict_separation = True
            lines.append(
                f"- **{k}** = {rug_vals} (banks range: [{bmin:.4f}..{bmax:.4f}], "
                f"verdict: `{v}`)"
            )
    if not has_strict_separation:
        lines.append(
            "- **NONE.** No single persisted feature in V47H JSONL puts both "
            "CNk6 (514ms) and DxPa (991ms) strictly outside the bank range."
        )
    lines.append("")
    lines.append("### Features where both rugs are on the same side of bank median")
    lines.append("")
    for (k, rug_vals, bmin, bmax, bmed, v) in distinguishing:
        if v in ("rugs_below_bank_median", "rugs_above_bank_median"):
            lines.append(
                f"- {k} = {rug_vals} (bank median: {bmed:.4f}, "
                f"bank range: [{bmin:.4f}..{bmax:.4f}], verdict: `{v}`)"
            )
    lines.append("")
    lines.append("### Honest assessment of pre-entry detectability")
    lines.append("")
    if has_strict_separation:
        lines.append(
            "At least one persisted feature strictly separates the 2 medium-rugs "
            "from the 8 bank winners. V47I can attempt a veto using that feature."
        )
    else:
        lines.append(
            "**No persisted feature strictly separates both medium-window rugs "
            "from all bank winners.** Both CNk6 and DxPa lie *within* the bank "
            "distribution on every persisted dimension. The persisted feature "
            "set in `v47h_no_send_decisions.jsonl` does NOT contain a signal "
            "that, at decision time, would distinguish a 500-1000ms rug from a "
            "healthy candidate. V47I's only remaining options are weaker "
            "median-side heuristics (which may sacrifice winners) OR features "
            "that are not yet persisted (curve-delta values, quote history, "
            "sell-side 500/1000ms windows) and would require capture extension."
        )
    lines.append("")
    lines.append("## Features NOT persisted in V47H JSONL (cannot be recovered)")
    lines.append("")
    lines.append("- Curve delta VALUES over last 500ms / 1000ms (only LENGTHS persisted)")
    lines.append("- Curve velocity / deceleration trajectory")
    lines.append("- Quote history (local sell quote at -100ms, -200ms, ...)")
    lines.append("- Quote gradient (Δquote per update)")
    lines.append("- Sell-side per-window stats at 500ms and 1000ms")
    lines.append("- Net pending sol per window (250ms only)")
    lines.append("- Buy-cluster speed (250ms only via `buy_cluster_speed_250ms`)")
    lines.append("")
    lines.append(
        "**These features must be captured in V47I's no-send to be available "
        "for the medium-rug veto. Phase 2 wrappers extend the buffer to "
        "expose them.**"
    )
    lines.append("")
    lines.append("## Per-cohort feature stats")
    lines.append("")
    lines.append("### 500-1000ms rugs (n=" + str(len(rugs_500_1000)) + ")")
    lines.append("")
    lines.append("| feature | n | min | max | mean | median |")
    lines.append("|---------|---|-----|-----|------|--------|")
    for k in feature_keys:
        s = stats_rugs[k]
        lines.append(
            f"| {k} | {s['n']} | {s['min']:.5f} | {s['max']:.5f} | "
            f"{s['mean']:.5f} | {s['median']:.5f} |"
        )
    lines.append("")
    lines.append("### Banks / non-neg (n=" + str(len(banks)) + ")")
    lines.append("")
    lines.append("| feature | n | min | max | mean | median |")
    lines.append("|---------|---|-----|-----|------|--------|")
    for k in feature_keys:
        s = stats_banks[k]
        lines.append(
            f"| {k} | {s['n']} | {s['min']:.5f} | {s['max']:.5f} | "
            f"{s['mean']:.5f} | {s['median']:.5f} |"
        )
    lines.append("")
    lines.append("## Per-feature rug-vs-bank delta")
    lines.append("")
    lines.append("| feature | rug_median | bank_median | delta(rug-bank) |")
    lines.append("|---------|------------|-------------|-----------------|")
    for (k, rmed, bmed, d) in delta_analysis:
        lines.append(f"| {k} | {rmed:.5f} | {bmed:.5f} | {d:+.5f} |")
    lines.append("")
    lines.append("## Full V47H row table (joined)")
    lines.append("")
    lines.append(_format_table_v47h(v47h_joined))
    lines.append("")
    lines.append("## V47F dry-live joined entries (close_pnl shown)")
    lines.append("")
    lines.append(_format_table_vfe("V47F", v47f_joined))
    lines.append("")
    lines.append("## V47E dry-live joined entries (close_pnl shown)")
    lines.append("")
    lines.append(_format_table_vfe("V47E", v47e_joined))
    lines.append("")
    lines.append("## V47G watchdog reasons fired on CNk6 / DxPa")
    lines.append("")
    for r in rugs_500_1000:
        lines.append(
            f"- **{_short(r.get('mint',''))}** lag={r.get('observed_label_lag_ms')}ms "
            f"watchdog_reason=`{r.get('v47g_midhold_abort_reason') or '-'}` "
            f"watchdog_action=`{r.get('v47g_midhold_abort_action') or '-'}`"
        )
    lines.append("")
    lines.append("## Recommendation for V47I medium-rug veto")
    lines.append("")
    if not has_strict_separation:
        lines.append(
            "**The structural truth is that with persisted JSONL features alone, "
            "the 2 medium-window rugs are indistinguishable from healthy "
            "candidates.** V47I should therefore:"
        )
        lines.append("")
        lines.append(
            "1. Build wrappers in V47I no-send to capture the MISSING features "
            "(sell-side 500/1000ms, curve-delta trajectory, quote history) — "
            "these are causal and require no paid feeds."
        )
        lines.append(
            "2. Apply the 5 sub-vetos (A medium-window sell pressure, B curve "
            "deceleration, C quote weakening, D thin-edge-with-sell, E "
            "fast-reversal) on the FRESH features captured in V47I no-send."
        )
        lines.append(
            "3. Accept that Phase 3 replay against V47H JSONL will be limited to "
            "feature-A and a coarse approximation of E (the only sub-vetos for "
            "which inputs are persisted)."
        )
        lines.append(
            "4. Phase 4 fresh no-send is the empirical test where all 5 vetos "
            "operate on full feature inputs."
        )
        lines.append(
            "5. If even fresh no-send cannot distinguish 500-1000ms rugs from "
            "winners, the truth is that 500-1000ms rugs are indistinguishable "
            "pre-entry on free feeds, and the only remaining defence is V47G "
            "watchdog (which is what already fires)."
        )
    else:
        lines.append("Strict separation exists — V47I can use it directly.")
    lines.append("")

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[V47I-FORENSIC] wrote {REPORT_PATH} (rugs={len(rugs_500_1000)}, banks={len(banks)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
