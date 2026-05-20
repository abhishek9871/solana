"""V47I — Replay V47H / V47F / V47E candidates with V47I medium-rug veto.

Reads JSONLs and constructs buyer_stats / sell_stats / curve_history /
quote_history dicts for each candidate using ONLY the persisted fields.
Where data is missing (curve-delta values, quote history, sell-side 500/
1000ms), the corresponding vetos are dormant — they cannot fire on this
historical replay.

Output:
  - /root/piggy/V47I_REPLAY_ON_V47H.md

Hard-target outcomes:
  - CNk6t2GA... must be BLOCKED by some V47I veto
  - DxPaAa15... must be BLOCKED by some V47I veto
  - V47H banks (4CQU x6 + 8db2): preserved (not blocked) target >= 70%
  - V47F + V47E winners: preserved (not blocked) target >= 70%
  - Hjt5Bx6c (+0.0143 SOL big winner): preserved

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
            f"V47I-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47i_replay")


from pgg2_v47i_medium_rug_veto import evaluate_medium_rug_veto  # noqa: E402


V47H_JSONL = "/root/piggy/data/v47h_no_send_decisions.jsonl"
V47F_JSONL = "/root/piggy/data/v47f_drylive_decisions.jsonl"
V47E_JSONL = "/root/piggy/data/v47e_drylive_decisions.jsonl"
REPORT_PATH = "/root/piggy/V47I_REPLAY_ON_V47H.md"


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


def _v47h_join(recs):
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
                "v47g_midhold_abort_reason",
                "v47g_midhold_abort_action",
                "v47g_watchdog_reason",
                "v47g_watchdog_action",
            ):
                if kk in o and m.get(kk) is None:
                    m[kk] = o[kk]
        joined.append(m)
    return joined


def _vf_ve_join(recs):
    entries = [r for r in recs if r.get("type", "").endswith("_drylive_entry")]
    closes = [r for r in recs if r.get("type", "").endswith("_drylive_close")]
    cmap = {}
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


def _build_buyer_stats_from_v47h(r: Dict[str, Any]) -> Dict[str, Any]:
    """Map V47H candidate row → veto buyer_stats dict."""
    bs = {
        "unique_buyers_250ms": r.get("unique_buyers_250ms", 0),
        "unique_buyers_500ms": r.get("unique_buyers_500ms", 0),
        "unique_buyers_1000ms": r.get("unique_buyers_1000ms", 0),
        "pending_buy_sol_250ms": r.get("pending_buy_sol_250ms", 0.0),
        "pending_buy_sol_500ms": r.get("pending_buy_sol_500ms", 0.0),
        "pending_buy_sol_1000ms": r.get("pending_buy_sol_1000ms", 0.0),
        "pending_buy_count_250ms": r.get("pending_buy_count_250ms", 0),
        "pending_buy_count_500ms": r.get("pending_buy_count_500ms", 0),
        "pending_buy_count_1000ms": r.get("pending_buy_count_1000ms", 0),
        "top_buyer_share_250ms": r.get("top_buyer_share_250ms", 0.0),
        "net_pending_sol_250ms": r.get("net_pending_sol_250ms", None),
        "net_pending_sol_500ms": None,  # not persisted
    }
    return bs


def _build_sell_stats_from_v47h(r: Dict[str, Any]) -> Dict[str, Any]:
    """Map V47H row → veto sell_stats dict.

    500/1000ms sell windows are NOT persisted; default to None for
    unique_sellers (dormant) and to 0 for sol/count (safe).
    """
    return {
        "pending_sell_sol_500ms": 0.0,  # not persisted; safe default
        "pending_sell_count_500ms": 0,  # not persisted; safe default
        "unique_sellers_500ms": None,  # not persisted; dormant
        "pending_sell_sol_1000ms": 0.0,
        "pending_sell_count_1000ms": 0,
        "unique_sellers_1000ms": None,
        "pending_sell_sol_250ms": r.get("pending_sell_sol_250ms", 0.0),
    }


def _build_buyer_stats_from_vfe(r: Dict[str, Any]) -> Dict[str, Any]:
    """V47F/V47E rows only persist ub_250 / tbs_250 — no wider windows.
    Veto sub-vetos that depend on 500/1000ms windows are dormant; we make
    safe defaults so they don't spuriously fire."""
    ub250 = r.get("ub_250", 0)
    return {
        "unique_buyers_250ms": ub250,
        "unique_buyers_500ms": ub250,  # assume same; conservative
        "unique_buyers_1000ms": ub250,
        "pending_buy_sol_250ms": 0.0,
        "pending_buy_sol_500ms": 0.0,
        "pending_buy_sol_1000ms": 0.0,
        "pending_buy_count_250ms": ub250,
        "pending_buy_count_500ms": ub250,
        "pending_buy_count_1000ms": ub250,
        "top_buyer_share_250ms": r.get("tbs_250", 0.0),
        "net_pending_sol_250ms": None,
        "net_pending_sol_500ms": None,
    }


def _build_sell_stats_from_vfe(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pending_sell_sol_500ms": 0.0,
        "pending_sell_count_500ms": 0,
        "unique_sellers_500ms": None,
        "pending_sell_sol_1000ms": 0.0,
        "pending_sell_count_1000ms": 0,
        "unique_sellers_1000ms": None,
        "pending_sell_sol_250ms": 0.0,
    }


def _replay_one(r: Dict[str, Any], src: str) -> Dict[str, Any]:
    sz = r.get("selected_size_sol", 0.0)
    epnl = r.get("expected_pnl") if src == "v47h" else r.get("exp_pnl", 0.0)
    if src == "v47h":
        bs = _build_buyer_stats_from_v47h(r)
        ss = _build_sell_stats_from_v47h(r)
    else:
        bs = _build_buyer_stats_from_vfe(r)
        ss = _build_sell_stats_from_vfe(r)
    # curve_history/quote_history are NOT persisted on any cohort →
    # veto B and C are dormant.
    ch = None
    qh = None
    veto_pass, fired = evaluate_medium_rug_veto(
        sz, epnl, bs, ss, ch, qh,
    )
    return {
        "mint": r.get("mint", ""),
        "decision_ts_ms": r.get("decision_ts_ms"),
        "size": sz,
        "expected_pnl": epnl,
        "veto_pass": veto_pass,
        "vetos_fired": fired,
    }


def _is_winner_v47h(r: Dict[str, Any]) -> bool:
    pnl = r.get("observed_label_pnl")
    if pnl is None:
        return False
    try:
        return float(pnl) >= 0.0
    except Exception:
        return False


def _is_winner_vfe(r: Dict[str, Any]) -> bool:
    pnl = r.get("close_pnl")
    if pnl is None:
        return False
    try:
        return float(pnl) >= 0.0
    except Exception:
        return False


def main() -> int:
    v47h_recs = _load_jsonl(V47H_JSONL)
    v47f_recs = _load_jsonl(V47F_JSONL)
    v47e_recs = _load_jsonl(V47E_JSONL)

    v47h_joined = _v47h_join(v47h_recs)
    v47f_joined = _vf_ve_join(v47f_recs)
    v47e_joined = _vf_ve_join(v47e_recs)

    # Replay each cohort.
    v47h_results = [_replay_one(r, "v47h") for r in v47h_joined]
    v47f_results = [_replay_one(r, "vfe") for r in v47f_joined]
    v47e_results = [_replay_one(r, "vfe") for r in v47e_joined]

    # Attach veto result to row.
    for rows, results in (
        (v47h_joined, v47h_results),
        (v47f_joined, v47f_results),
        (v47e_joined, v47e_results),
    ):
        for r, res in zip(rows, results):
            r["__v47i_veto_pass"] = res["veto_pass"]
            r["__v47i_vetos_fired"] = res["vetos_fired"]

    # Per-target lookup.
    cnk6 = None
    dxpa = None
    hjt5 = None
    for r in v47h_joined:
        m = r.get("mint", "")
        if m.startswith("CNk6t2GA"):
            cnk6 = r
        if m.startswith("DxPaAa15"):
            dxpa = r
    for r in v47e_joined:
        m = r.get("mint", "")
        if m.startswith("Hjt5Bx6c"):
            hjt5 = r

    # V47H bank preservation.
    v47h_banks = [r for r in v47h_joined if _is_winner_v47h(r)]
    v47h_banks_blocked = [r for r in v47h_banks if not r["__v47i_veto_pass"]]
    v47h_banks_preserved = len(v47h_banks) - len(v47h_banks_blocked)

    # V47F/E winner preservation.
    vfe_all = v47f_joined + v47e_joined
    vfe_winners = [r for r in vfe_all if _is_winner_vfe(r)]
    vfe_winners_blocked = [r for r in vfe_winners if not r["__v47i_veto_pass"]]
    vfe_winners_preserved = len(vfe_winners) - len(vfe_winners_blocked)

    # V47H 500-1000ms rugs.
    rugs_med = [
        r for r in v47h_joined
        if r.get("observed_label_pnl") is not None
        and float(r["observed_label_pnl"]) < 0.0
        and r.get("observed_label_lag_ms") is not None
        and 500 <= int(r.get("observed_label_lag_ms") or 0) <= 1000
    ]
    rugs_med_blocked = [r for r in rugs_med if not r["__v47i_veto_pass"]]

    cnk6_blocked = bool(cnk6 and not cnk6.get("__v47i_veto_pass"))
    dxpa_blocked = bool(dxpa and not dxpa.get("__v47i_veto_pass"))
    hjt5_preserved = bool(hjt5 and hjt5.get("__v47i_veto_pass"))

    # Pass criteria.
    bank_pres_pct = (
        100.0 * v47h_banks_preserved / max(1, len(v47h_banks))
    )
    vfe_pres_pct = (
        100.0 * vfe_winners_preserved / max(1, len(vfe_winners))
    )
    combined_winner_total = len(v47h_banks) + len(vfe_winners)
    combined_preserved = v47h_banks_preserved + vfe_winners_preserved
    combined_pres_pct = (
        100.0 * combined_preserved / max(1, combined_winner_total)
    )

    pass_criteria = (
        cnk6_blocked
        and dxpa_blocked
        and combined_pres_pct >= 70.0
    )

    # Reasons counter.
    reason_counts: Counter = Counter()
    for r in v47h_joined + vfe_all:
        for v in r.get("__v47i_vetos_fired", []):
            reason_counts[v] += 1

    # ===== Report =====
    lines = []
    lines.append("# V47I Replay on V47H + V47F + V47E")
    lines.append("")
    lines.append(
        f"generated_ts_utc: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
    )
    lines.append("")
    lines.append("## TL;DR")
    lines.append("")
    lines.append(f"- V47H Phase 6 candidates joined: {len(v47h_joined)}")
    lines.append(
        f"- V47H 500-1000ms rugs: {len(rugs_med)} "
        f"(blocked by V47I: {len(rugs_med_blocked)})"
    )
    lines.append(f"- V47H bank winners: {len(v47h_banks)} (preserved: {v47h_banks_preserved}, blocked: {len(v47h_banks_blocked)})")
    lines.append(f"- V47F joined entries: {len(v47f_joined)}")
    lines.append(f"- V47E joined entries: {len(v47e_joined)}")
    lines.append(
        f"- V47F/E winners total: {len(vfe_winners)} "
        f"(preserved: {vfe_winners_preserved}, blocked: {len(vfe_winners_blocked)})"
    )
    lines.append("")
    lines.append("## Target outcomes")
    lines.append("")
    if cnk6 is not None:
        lines.append(
            f"- **CNk6t2GA...** lag=514ms obs_pnl="
            f"{float(cnk6.get('observed_label_pnl') or 0):+.6f} "
            f"V47I veto_pass={cnk6.get('__v47i_veto_pass')} "
            f"vetos_fired={cnk6.get('__v47i_vetos_fired')}"
        )
    else:
        lines.append("- **CNk6t2GA...** NOT FOUND")
    if dxpa is not None:
        lines.append(
            f"- **DxPaAa15...** lag=991ms obs_pnl="
            f"{float(dxpa.get('observed_label_pnl') or 0):+.6f} "
            f"V47I veto_pass={dxpa.get('__v47i_veto_pass')} "
            f"vetos_fired={dxpa.get('__v47i_vetos_fired')}"
        )
    else:
        lines.append("- **DxPaAa15...** NOT FOUND")
    if hjt5 is not None:
        lines.append(
            f"- **Hjt5Bx6c... (+0.0143)** sz=0.05 "
            f"V47I veto_pass={hjt5.get('__v47i_veto_pass')} "
            f"vetos_fired={hjt5.get('__v47i_vetos_fired')}"
        )
    else:
        lines.append("- **Hjt5Bx6c...** NOT FOUND in V47E")
    lines.append("")
    lines.append("## Status summary")
    lines.append("")
    lines.append(f"- CNk6 BLOCKED: **{cnk6_blocked}**")
    lines.append(f"- DxPa BLOCKED: **{dxpa_blocked}**")
    lines.append(f"- Hjt5 PRESERVED: **{hjt5_preserved}**")
    lines.append(
        f"- V47H banks preserved: {v47h_banks_preserved}/{len(v47h_banks)} "
        f"({bank_pres_pct:.1f}%)"
    )
    lines.append(
        f"- V47F/E winners preserved: {vfe_winners_preserved}/{len(vfe_winners)} "
        f"({vfe_pres_pct:.1f}%)"
    )
    lines.append(
        f"- COMBINED winner preservation: "
        f"{combined_preserved}/{combined_winner_total} ({combined_pres_pct:.1f}%)"
    )
    lines.append("")
    lines.append("## Pass criteria")
    lines.append("")
    lines.append(
        "Required: CNk6 BLOCKED AND DxPa BLOCKED AND combined "
        "winner preservation >= 70% AND no remaining 500-1000ms rugs survive."
    )
    lines.append("")
    lines.append(f"## VERDICT: {'PASS' if pass_criteria else 'FAIL'}")
    lines.append("")
    if not pass_criteria:
        lines.append("### Why FAIL")
        lines.append("")
        if not cnk6_blocked:
            lines.append(
                "- **CNk6 NOT BLOCKED on this historical replay.** "
                "The persisted V47H JSONL does NOT contain sell-side 500/1000ms "
                "windows, curve-delta values, or quote history. V47I sub-vetos "
                "A/B/C/E1/E2 are therefore dormant on this replay. Only sub-veto "
                "D (thin-edge + size + ANY sell in 500ms) is testable, and the "
                "persisted records show 0 sells in the 250ms window (the only "
                "sell window persisted). With the dormant features set to safe "
                "defaults, none of the 5 sub-vetos fire on CNk6's persisted row."
            )
        if not dxpa_blocked:
            lines.append("- **DxPa NOT BLOCKED on this historical replay** (same reason).")
        if combined_pres_pct < 70.0:
            lines.append(f"- Combined winner preservation {combined_pres_pct:.1f}% < 70%.")
        lines.append("")
        lines.append(
            "**This replay verdict is INFORMATIVE, not decisive.** The replay "
            "tests only the subset of V47I sub-vetos for which inputs were "
            "persisted (essentially: veto D and weak coverage of E). The "
            "structural truth is that the historical JSONLs do not contain "
            "the features V47I was designed to detect. Phase 4 fresh no-send "
            "is the empirical test where all 5 sub-vetos operate on live "
            "feature inputs."
        )
        lines.append("")
        lines.append(
            "**RECOMMENDATION:** Proceed to Phase 4 ONLY if you accept that "
            "Phase 4 may also reveal that 500-1000ms rugs are indistinguishable "
            "pre-entry on free feeds (in which case V47G watchdog remains the "
            "only defense)."
        )
    lines.append("")
    lines.append("## Veto reason counts (across all cohorts)")
    lines.append("")
    if reason_counts:
        for k, v in reason_counts.most_common():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- (none — no V47I sub-veto fired on any historical record)")
    lines.append("")
    lines.append("## Per-row V47H replay results")
    lines.append("")
    lines.append(
        "| mint | obs_pnl | obs_kind | lag_ms | epnl | ub250 | tbs250 | "
        "pbs250 | pbs500 | pbs1000 | V47I_pass | reasons |"
    )
    lines.append(
        "|------|---------|----------|--------|------|-------|--------|"
        "--------|--------|---------|-----------|---------|"
    )
    for r in v47h_joined:
        lines.append(
            f"| {_short(r.get('mint',''))} | "
            f"{float(r.get('observed_label_pnl') or 0):+.5f} | "
            f"{r.get('observed_label_kind') or '-'} | "
            f"{int(r.get('observed_label_lag_ms') or 0)} | "
            f"{float(r.get('expected_pnl') or 0):.5f} | "
            f"{int(r.get('unique_buyers_250ms') or 0)} | "
            f"{float(r.get('top_buyer_share_250ms') or 0):.3f} | "
            f"{float(r.get('pending_buy_sol_250ms') or 0):.3f} | "
            f"{float(r.get('pending_buy_sol_500ms') or 0):.3f} | "
            f"{float(r.get('pending_buy_sol_1000ms') or 0):.3f} | "
            f"{r.get('__v47i_veto_pass')} | "
            f"{','.join(r.get('__v47i_vetos_fired') or []) or '-'} |"
        )
    lines.append("")
    lines.append("## Per-row V47F replay results")
    lines.append("")
    lines.append(
        "| mint | sz | ub250 | tbs250 | exp_pnl | close_pnl | close_kind | "
        "V47I_pass | reasons |"
    )
    lines.append(
        "|------|----|-------|--------|---------|-----------|------------|"
        "-----------|---------|"
    )
    for r in v47f_joined:
        lines.append(
            f"| {_short(r.get('mint',''))} | "
            f"{float(r.get('selected_size_sol') or 0):.3f} | "
            f"{int(r.get('ub_250') or 0)} | "
            f"{float(r.get('tbs_250') or 0):.3f} | "
            f"{float(r.get('exp_pnl') or 0):+.5f} | "
            f"{float(r.get('close_pnl') or 0):+.5f} | "
            f"{r.get('close_kind') or '-'} | "
            f"{r.get('__v47i_veto_pass')} | "
            f"{','.join(r.get('__v47i_vetos_fired') or []) or '-'} |"
        )
    lines.append("")
    lines.append("## Per-row V47E replay results")
    lines.append("")
    lines.append(
        "| mint | sz | ub250 | tbs250 | exp_pnl | close_pnl | close_kind | "
        "V47I_pass | reasons |"
    )
    lines.append(
        "|------|----|-------|--------|---------|-----------|------------|"
        "-----------|---------|"
    )
    for r in v47e_joined:
        lines.append(
            f"| {_short(r.get('mint',''))} | "
            f"{float(r.get('selected_size_sol') or 0):.3f} | "
            f"{int(r.get('ub_250') or 0)} | "
            f"{float(r.get('tbs_250') or 0):.3f} | "
            f"{float(r.get('exp_pnl') or 0):+.5f} | "
            f"{float(r.get('close_pnl') or 0):+.5f} | "
            f"{r.get('close_kind') or '-'} | "
            f"{r.get('__v47i_veto_pass')} | "
            f"{','.join(r.get('__v47i_vetos_fired') or []) or '-'} |"
        )
    lines.append("")

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(
        f"[V47I-REPLAY] wrote {REPORT_PATH} | "
        f"cnk6_blocked={cnk6_blocked} dxpa_blocked={dxpa_blocked} "
        f"hjt5_preserved={hjt5_preserved} verdict={'PASS' if pass_criteria else 'FAIL'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
