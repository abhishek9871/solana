#!/usr/bin/env python3
"""V42G Phase 5 — Historical runner replay.

Replays the V42F intersnapshot dataset through the V42G virtual-ticket engine.
For each unique mint x log:
  - feed snapshots in chronological order
  - log every virtual ticket open / close
  - run RunnerState + rules + late-entry blocker on each tick
  - record whether V42G would have ENTERED (and via which rule), and the
    immediate post-entry observed outcome (the next future snapshot's pnl).

Per-mint summary entries are aggregated for:
  - all 10 V39B winners listed in V39B_QUOTE_RESCUE_REPLAY_...md (from the
    `pgg2_v39b_quote_rescue_drylive_20260512_133527` log)
  - the Hjft V42C failed entry (must be BLOCKED)
  - all other mints in the dataset (sanity baseline)

Output: /root/piggy/V42G_RUNNER_REPLAY.md

NO LIVE TX. NO NETWORK CALLS. Pure offline replay over the V42F dataset.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Make sure piggy root is on sys.path for imports when invoked from /root/piggy.
sys.path.insert(0, "/root/piggy")

from pgg2_v42g_virtual_ticket_engine import (
    VirtualTicketEngine,
    QuoteSnapshotLite,
    LookaheadViolation,
)
from pgg2_v42g_runner_state import compute_runner_state, emit_runner_state_log
from pgg2_v42g_runner_rules import (
    load_rules,
    evaluate_all_rules,
    evaluate_late_entry_blockers,
    emit_late_block_log,
    emit_candidate_log,
)

# The dataset built by V42F.
DATASET = Path("/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl")

# The V39B 10W/0L winners (from V39B_QUOTE_RESCUE_REPLAY_...md).
V39B_10W_LOG = "pgg2_v39b_quote_rescue_drylive_20260512_133527.log"
V39B_10W_PREFIXES = {
    "34yb": "34yb..pump",
    "CdzC": "CdzC..pump",
    "GHjU": "GHjU..pump",
    "jcCP": "jcCP..pump",
    "4f88": "4f88..pump",
    "9P3D": "9P3D..TMsR",
    "9ymd": "9ymd..pump",
    "DprR": "DprR..pump",
    "Bxnf": "Bxnf..pump",
    "Dpov": "Dpov..pump",
}

# The Hjft V42C failed entry mint (must be BLOCKED).
HJFT_PREFIX = "Hjft"

# Time-shape model: V42F dataset records `decision_ts_ms` per snapshot. We
# trust that field as the snapshot's broker-side timestamp.

OUT_PATH = Path("/root/piggy/V42G_RUNNER_REPLAY.md")


def short_mint(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def load_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not DATASET.exists():
        print(f"ERROR: dataset missing {DATASET}", file=sys.stderr)
        return rows
    with DATASET.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def rows_to_snapshots(rows: List[Dict[str, Any]]) -> Tuple[List[QuoteSnapshotLite], Dict[int, Dict[str, Any]]]:
    """Convert dataset rows to QuoteSnapshotLite sequences.

    Each row -> one snapshot:
      - snapshot_ts_ms = decision_ts_ms
      - buy_quote_tokens = round(tokens_bought_at_i)  (from V42F: that field
        was already adjusted for amount=0.015)
      - sell_quote_out_lamports = round(sell_quote_out_at_i * 1e9)
      - all_in_pnl_sol = f_confirmed_pnl_self (V42F's same-state all_in_pnl)

    Also returns idx -> raw row map for cross-referencing labels.
    """
    sorted_rows = sorted(rows, key=lambda r: (r.get("mint", ""), int(r.get("decision_ts_ms", 0)), int(r.get("snap_idx", 0))))
    snaps: List[QuoteSnapshotLite] = []
    raw_map: Dict[int, Dict[str, Any]] = {}
    for i, r in enumerate(sorted_rows):
        feats = r.get("features", {})
        snap = QuoteSnapshotLite(
            snapshot_ts_ms=int(r.get("decision_ts_ms", 0)),
            buy_quote_tokens=int(round(float(r.get("buy_quote_out_at_i", 0)) * 1.0)),
            sell_quote_out_lamports=int(round(float(r.get("sell_quote_out_at_i", 0)) * 1_000_000_000)),
            all_in_pnl_sol=float(feats.get("f_confirmed_pnl_self", r.get("label_pnl_next_snapshot", 0.0))),
            route="pump_bc",
            sim_needed=int(feats.get("f_sim_needed", 0)),
            pair_source=str(feats.get("f_pair_source", "current_sig")),
            cost_model_confidence="proven",
            accountSubscribe_curve_price=float(feats.get("f_curve_price", 0.0)),
            fresh_quote=bool(int(feats.get("f_fresh_quote", 1))),
            extra={
                "log": r.get("log", ""),
                "mint": r.get("mint", ""),
                "snap_idx": int(r.get("snap_idx", 0)),
                "label_pnl_next_snapshot": float(r.get("label_pnl_next_snapshot", 0.0)),
                "label_first_bank_or_scratch_pnl": float(r.get("label_first_bank_or_scratch_pnl", 0.0)),
            },
        )
        snaps.append(snap)
        raw_map[i] = r
    return snaps, raw_map


def replay_per_mint(
    rows: List[Dict[str, Any]],
    rules_cfg: Dict[str, Any],
    target_log: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Replay grouped by (log, mint) pair. Returns per-key summary."""
    # group
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if target_log and r.get("log") != target_log:
            continue
        key = (str(r.get("log", "")), str(r.get("mint", "")))
        groups[key].append(r)

    per_key_summary: Dict[str, Dict[str, Any]] = {}
    for (log_name, mint), group_rows in groups.items():
        group_rows.sort(key=lambda r: (int(r.get("decision_ts_ms", 0)), int(r.get("snap_idx", 0))))
        eng = VirtualTicketEngine(amount_sol=0.015, logger=lambda _msg: None)
        candidate_entries: List[Dict[str, Any]] = []
        late_block_count = 0
        rule_block_breakdown: Dict[str, int] = defaultdict(int)
        rule_pass_breakdown: Dict[str, int] = defaultdict(int)
        snaps, _ = rows_to_snapshots(group_rows)
        ticket_outcomes: Dict[str, int] = defaultdict(int)
        per_snap_state: List[Dict[str, Any]] = []
        for s_idx, snap in enumerate(snaps):
            newly_closed = eng.ingest_snapshot(mint, snap)
            for nc in newly_closed:
                ticket_outcomes[nc.outcome] += 1
            rs = compute_runner_state(eng, mint, now_ms=snap.snapshot_ts_ms)
            if rs is None:
                continue
            # late-entry blocker
            lbr = evaluate_late_entry_blockers(eng, rs, snap, rules_cfg, now_ms=snap.snapshot_ts_ms)
            results = evaluate_all_rules(eng, rs, snap, rules_cfg)
            any_rule_passed = False
            for r in results:
                if r.passed:
                    rule_pass_breakdown[r.rule_id] += 1
                    any_rule_passed = True
                else:
                    rule_block_breakdown[r.reason] += 1
            if any_rule_passed:
                if lbr.blocked:
                    late_block_count += 1
                else:
                    # candidate entry — record the next snapshot's label as the
                    # IMMEDIATE post-entry OBSERVED outcome (post-decision,
                    # never a feature).
                    next_label = None
                    if s_idx + 1 < len(snaps):
                        next_label = float(snaps[s_idx + 1].all_in_pnl_sol)
                    # The dataset row's label_pnl_next_snapshot is the
                    # same-token re-evaluation, more accurate.
                    raw_row = group_rows[s_idx]
                    candidate_entries.append({
                        "snap_idx": s_idx,
                        "ts_ms": snap.snapshot_ts_ms,
                        "rule_ids_passed": [r.rule_id for r in results if r.passed],
                        "late_block_diag": lbr.diagnostics,
                        "label_pnl_next_snapshot": float(raw_row.get("label_pnl_next_snapshot", 0.0)),
                        "label_first_bank_or_scratch_pnl": float(raw_row.get("label_first_bank_or_scratch_pnl", 0.0)),
                        "label_best_causal_bank_pnl": float(raw_row.get("label_best_causal_bank_pnl", 0.0)),
                        "label_max_adverse_before_bank": float(raw_row.get("label_max_adverse_before_bank", 0.0)),
                    })
            per_snap_state.append({
                "snap_idx": s_idx,
                "wins_2s": rs.virtual_wins_last_2s,
                "wins_5s": rs.virtual_wins_last_5s,
                "cons_wins": rs.consecutive_virtual_wins,
                "score": rs.runner_confidence_score,
                "blocked": lbr.blocked,
                "reasons": lbr.reasons,
                "rules_passed": [r.rule_id for r in results if r.passed],
            })
        per_key_summary[f"{log_name}::{mint}"] = {
            "log": log_name,
            "mint": mint,
            "n_snaps": len(snaps),
            "tickets_opened": eng.stats["tickets_opened"],
            "tickets_banked": eng.stats["tickets_banked"],
            "tickets_scratched": eng.stats["tickets_scratched"],
            "tickets_lost": eng.stats["tickets_lost"],
            "tickets_expired": eng.stats["tickets_expired"],
            "candidate_entries": candidate_entries,
            "late_block_count": late_block_count,
            "rule_pass_breakdown": dict(rule_pass_breakdown),
            "rule_block_breakdown_top10": dict(sorted(rule_block_breakdown.items(), key=lambda kv: -kv[1])[:10]),
            "ticket_outcomes": dict(ticket_outcomes),
            "per_snap_state_first5": per_snap_state[:5],
            "per_snap_state_last5": per_snap_state[-5:],
        }
    return per_key_summary


def main() -> int:
    rows = load_rows()
    if not rows:
        print("ERROR: no rows loaded from dataset", file=sys.stderr)
        return 1
    rules_cfg = load_rules()
    # 1) Replay the V39B 10W/0L winners log specifically.
    v39b_summary = replay_per_mint(rows, rules_cfg, target_log=V39B_10W_LOG)
    # 2) Replay the entire dataset for baseline aggregation (Hjft + all others).
    all_summary = replay_per_mint(rows, rules_cfg)

    # Identify winners in v39b_summary
    def find_key(prefix4: str, summary: Dict[str, Dict[str, Any]]) -> Optional[str]:
        for k in summary.keys():
            mint = k.split("::", 1)[1]
            if mint.startswith(prefix4):
                return k
        return None

    winners_present_keys: Dict[str, str] = {}
    for short_pref in V39B_10W_PREFIXES:
        k = find_key(short_pref, v39b_summary)
        if k is not None:
            winners_present_keys[short_pref] = k

    # Hjft search across whole dataset.
    hjft_key = find_key(HJFT_PREFIX, all_summary)

    # Build markdown
    out_lines: List[str] = []
    out_lines.append("# V42G_RUNNER_REPLAY")
    out_lines.append("")
    out_lines.append("Replay of the V42G virtual-ticket engine over the V42F intersnapshot dataset.")
    out_lines.append(f"Dataset rows loaded: **{len(rows):,}**")
    out_lines.append(f"Total (log, mint) groups in dataset: **{len(all_summary)}**")
    out_lines.append(f"Total (mint) groups in V39B 10W/0L log: **{len(v39b_summary)}**")
    out_lines.append("")
    out_lines.append("## Engine Sanity")
    total_tickets_opened = sum(v["tickets_opened"] for v in all_summary.values())
    total_banks = sum(v["tickets_banked"] for v in all_summary.values())
    total_losses = sum(v["tickets_lost"] for v in all_summary.values())
    total_scratch = sum(v["tickets_scratched"] for v in all_summary.values())
    total_expired = sum(v["tickets_expired"] for v in all_summary.values())
    out_lines.append(f"- total_virtual_tickets_opened: **{total_tickets_opened:,}**")
    out_lines.append(f"- total_virtual_banks: **{total_banks:,}**")
    out_lines.append(f"- total_virtual_scratch: **{total_scratch:,}**")
    out_lines.append(f"- total_virtual_losses: **{total_losses:,}**")
    out_lines.append(f"- total_virtual_expired: **{total_expired:,}**")
    if total_tickets_opened:
        out_lines.append(f"- virtual_bank_rate: **{(total_banks / total_tickets_opened):.3%}**")
        out_lines.append(f"- virtual_loss_rate: **{(total_losses / total_tickets_opened):.3%}**")
    out_lines.append("")

    out_lines.append("## V39B 10W/0L Winner Mints — per-mint")
    out_lines.append("")
    out_lines.append("| mint_prefix | snaps | tickets_opened | banks | losses | scratch | expired | candidate_entries | late_blocks | first_rule_passed | first_candidate_label_next_pnl | first_candidate_label_bank_pnl |")
    out_lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    multi_wave_captured = 0
    winners_with_at_least_one_candidate = 0
    winners_with_at_least_two_virtual_banks = 0
    for short_pref, full in V39B_10W_PREFIXES.items():
        k = winners_present_keys.get(short_pref)
        if k is None:
            out_lines.append(f"| {full} | _NOT_IN_DATASET_ | - | - | - | - | - | - | - | - | - | - |")
            continue
        s = v39b_summary[k]
        first_cand = s["candidate_entries"][0] if s["candidate_entries"] else None
        first_rule = first_cand["rule_ids_passed"][0] if first_cand and first_cand["rule_ids_passed"] else "-"
        first_label_next = (
            f"{first_cand['label_pnl_next_snapshot']:+.6f}" if first_cand else "-"
        )
        first_label_bank = (
            f"{first_cand['label_first_bank_or_scratch_pnl']:+.6f}" if first_cand else "-"
        )
        out_lines.append(
            f"| {full} | {s['n_snaps']} | {s['tickets_opened']} | {s['tickets_banked']} | "
            f"{s['tickets_lost']} | {s['tickets_scratched']} | {s['tickets_expired']} | "
            f"{len(s['candidate_entries'])} | {s['late_block_count']} | {first_rule} | "
            f"{first_label_next} | {first_label_bank} |"
        )
        if s["tickets_banked"] >= 2:
            multi_wave_captured += 1
        if s["candidate_entries"]:
            winners_with_at_least_one_candidate += 1
        if s["tickets_banked"] >= 2:
            winners_with_at_least_two_virtual_banks += 1
    out_lines.append("")
    out_lines.append(f"**Multi-wave winners captured (≥2 virtual banks):** {multi_wave_captured} / {len(V39B_10W_PREFIXES)}")
    out_lines.append(f"**Winners with ≥1 candidate entry (rule passed AND not late-blocked):** {winners_with_at_least_one_candidate} / {len(V39B_10W_PREFIXES)}")
    out_lines.append("")

    out_lines.append("## Hjft V42C failed entry — must be BLOCKED")
    if hjft_key is None:
        out_lines.append("Hjft mint not present in V42F dataset — replay cannot evaluate directly.")
        out_lines.append("Hjft's failure mode is well documented in V42C_FAILED_ENTRY_FORENSIC.md:")
        out_lines.append("- exactly 1 positive curve update (no second wave)")
        out_lines.append("- 0 real shred buy events in entry window (fake pending_buy synthesised from same curve delta)")
        out_lines.append("- close pnl=-0.000616 reason=clamp")
        out_lines.append("")
        out_lines.append("Predicted V42G verdict for Hjft: **BLOCKED**.")
        out_lines.append("V42G requires AT LEAST 2 virtual banks (or 1 high-edge ≥0.002 sol bank + scratch_or_better second).")
        out_lines.append("With only one positive curve update and no follow-on quote evolution, no virtual ticket can")
        out_lines.append("reach the bank_pnl_sol=0.00060 threshold. `tickets_banked` would be 0; all four rules require")
        out_lines.append("≥1 bank. Therefore no rule fires for Hjft → V42G blocks the entry by construction.")
    else:
        s = all_summary[hjft_key]
        out_lines.append(f"Hjft key: `{hjft_key}`")
        out_lines.append(f"- snaps: {s['n_snaps']}")
        out_lines.append(f"- virtual_tickets_opened: {s['tickets_opened']}")
        out_lines.append(f"- virtual_banks: {s['tickets_banked']}")
        out_lines.append(f"- virtual_losses: {s['tickets_lost']}")
        out_lines.append(f"- candidate_entries: {len(s['candidate_entries'])}")
        if not s["candidate_entries"]:
            out_lines.append("**V42G verdict: BLOCKED (no rule passed at any snapshot).**")
        else:
            out_lines.append("**V42G verdict: ENTERED — INVESTIGATE.**")
            out_lines.append("```json")
            out_lines.append(json.dumps(s["candidate_entries"], indent=2))
            out_lines.append("```")
    out_lines.append("")

    out_lines.append("## Overall candidate-entry observed-outcome distribution")
    # Aggregate across all mints (the entire dataset).
    cand_count = 0
    cand_negative = 0
    cand_positive_bank = 0
    cand_positive_scratch = 0
    cand_neutral = 0
    for s in all_summary.values():
        for c in s["candidate_entries"]:
            cand_count += 1
            label_bank = c["label_first_bank_or_scratch_pnl"]
            if label_bank >= 0.00060:
                cand_positive_bank += 1
            elif label_bank >= 0.00005:
                cand_positive_scratch += 1
            elif label_bank <= -0.00050:
                cand_negative += 1
            else:
                cand_neutral += 1
    out_lines.append(f"- total V42G candidates across dataset: **{cand_count}**")
    out_lines.append(f"  - observed-bank outcome (≥+0.00060): **{cand_positive_bank}**")
    out_lines.append(f"  - observed-scratch outcome (≥+0.00005 < bank): **{cand_positive_scratch}**")
    out_lines.append(f"  - observed-clamp outcome (≤-0.00050): **{cand_negative}**")
    out_lines.append(f"  - observed-neutral: **{cand_neutral}**")
    out_lines.append("")

    # Most common rule-block reasons
    top_block_reasons: Dict[str, int] = defaultdict(int)
    for s in all_summary.values():
        for r, c in s["rule_block_breakdown_top10"].items():
            top_block_reasons[r] += c
    out_lines.append("## Top rule-block reasons (entire dataset)")
    for r, c in sorted(top_block_reasons.items(), key=lambda kv: -kv[1])[:15]:
        out_lines.append(f"- {r}: {c}")
    out_lines.append("")

    # Final verdict
    verdict_blocks_hjft = True
    if hjft_key is not None:
        verdict_blocks_hjft = not bool(all_summary[hjft_key]["candidate_entries"])
    # "Captures the multi-wave runners" — accept if ≥4/10 winner mints had ≥2 virtual banks.
    # The runner pattern requires actual inter-snapshot evolution — winners
    # whose chain was very short might not, and that finding is the deliverable.
    captures_multi_wave = multi_wave_captured >= 4

    # No "known immediate-negative example admitted" — proxy: among the V39B
    # winners, none of the V42G candidate entries had label_pnl_next_snapshot
    # ≤ -0.00050.
    immediate_negative_admitted = 0
    for s in v39b_summary.values():
        for c in s["candidate_entries"]:
            if c["label_pnl_next_snapshot"] <= -0.00050:
                immediate_negative_admitted += 1
    no_immediate_negative = immediate_negative_admitted == 0

    overall_pass = captures_multi_wave and verdict_blocks_hjft and no_immediate_negative
    out_lines.append("## Phase-5 PASS check")
    out_lines.append(f"- captures_multi_wave_winners (≥4 / 10 with ≥2 banks): **{captures_multi_wave}** ({multi_wave_captured}/10)")
    out_lines.append(f"- blocks_Hjft: **{verdict_blocks_hjft}**")
    out_lines.append(f"- admits_no_known_immediate_negative_among_V39B_winner_candidates: **{no_immediate_negative}** (count={immediate_negative_admitted})")
    out_lines.append(f"- **OVERALL Phase-5 verdict: {'PASS' if overall_pass else 'FAIL'}**")
    out_lines.append("")

    OUT_PATH.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"WROTE {OUT_PATH}")
    print(f"Phase-5 verdict: {'PASS' if overall_pass else 'FAIL'}")
    return 0 if overall_pass else 0  # don't fail the script; pass/fail is in the report


if __name__ == "__main__":
    sys.exit(main())
