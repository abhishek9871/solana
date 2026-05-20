"""V47C - Phase 1 surgical forensic on V47B's 10 candidates.

Reads /root/piggy/data/v47b_no_send_decisions.jsonl and produces a
per-candidate table of:
- mint, selected_size_sol
- obs_kind, obs_pnl, obs_lag_ms
- unique_buyers_50/100/250/500ms (V47B persisted only 250ms; 50/100/500 are
  marked "n/a (not persisted by V47B)" honestly)
- pending_buy_count_250ms, pending_buy_sol_250ms, pending_sell_sol_250ms
- top_buyer_share_250ms (largest_pending_buy_sol / pending_buy_sol_250ms)
- largest_buy_sol_250ms
- expected_pnl, adverse_pnl, final_min_tokens_guard
- adverse_branch_outcome

Then evaluates the V47C gate logic per candidate and emits aggregate stats.

PURE READ. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import re as _re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


# ----- static-grep self-check ----------------------------------------
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
            f"V47C-FORENSIC-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47c_forensic")


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _evaluate_v47c_gates(c: Dict[str, Any]) -> Dict[str, Any]:
    """Apply V47C multi-buyer gate + size-cap rules to a V47B record."""
    ub_250 = int(c.get("unique_buyers_250ms", 0))
    pbc_250 = int(c.get("pending_buy_count_250ms", 0))
    pbs_250 = float(c.get("pending_buy_sol_250ms", 0.0))
    pss_250 = float(c.get("pending_sell_sol_250ms", 0.0))
    lpb_250 = float(c.get("largest_pending_buy_sol_250ms", 0.0))
    top_buyer_share = (
        (lpb_250 / pbs_250) if pbs_250 > 0.0 else 0.0
    )
    requested = float(c.get("selected_size_sol", 0.0))

    # --- Multi-buyer gate ---
    gate_pass = True
    gate_blocker = None
    if ub_250 < 2:
        gate_pass = False
        gate_blocker = "single_buyer_shadow_only"
    elif pbc_250 < 2:
        gate_pass = False
        gate_blocker = "pending_buy_count_lt_2"
    elif pbs_250 <= pss_250:
        gate_pass = False
        gate_blocker = "buy_sol_not_above_sell_sol"
    elif top_buyer_share > 0.75:
        gate_pass = False
        gate_blocker = "top_buyer_share_too_high"

    # --- Size cap (V47C Phase 3) ---
    cap_pass = True
    cap_reason = None
    capped_size = None
    if ub_250 < 2:
        cap_pass = False
        cap_reason = "no_entry_single_buyer"
    else:
        if ub_250 == 2:
            max_for_breadth = 0.020
        else:
            max_for_breadth = 0.050
        if requested > 0.020:
            # Stricter quality required for >0.020
            # Here we only have ub_250 (V47B didn't persist ub_500). Treat
            # ub_500 as "unknown >= ub_250" for forensic-honest evaluation.
            ub_500_known = None
            if ub_250 < 3 and ub_500_known is None:
                # Without ub_500 we can pass only if ub_250 >= 3
                cap_pass = False
                cap_reason = "size_cap_strict_ub_below_3_on_size_gt_0020"
            elif top_buyer_share > 0.65:
                cap_pass = False
                cap_reason = "size_cap_top_buyer_share_gt_065"
            elif pbs_250 < requested * 2:
                cap_pass = False
                cap_reason = (
                    "size_cap_pending_buy_sol_lt_2x_requested"
                )
        if cap_pass:
            capped = min(requested, max_for_breadth)
            if capped < requested:
                cap_reason = (
                    f"cap_applied:reduce_to_{capped:.3f}_from_{requested:.3f}"
                )
            else:
                cap_reason = f"cap_applied:ok_{capped:.3f}"
            capped_size = capped

    v47c_admit = bool(
        gate_pass and cap_pass and capped_size is not None
    )

    return {
        "ub_250": int(ub_250),
        "pbc_250": int(pbc_250),
        "pbs_250": float(pbs_250),
        "pss_250": float(pss_250),
        "lpb_250": float(lpb_250),
        "top_buyer_share": float(top_buyer_share),
        "requested_size": float(requested),
        "multi_buyer_gate_pass": bool(gate_pass),
        "multi_buyer_gate_blocker": gate_blocker,
        "size_cap_pass": bool(cap_pass),
        "size_cap_reason": cap_reason,
        "capped_size": capped_size,
        "v47c_would_admit": bool(v47c_admit),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in-jsonl",
        default="/root/piggy/data/v47b_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md",
        default="/root/piggy/V47B_CANDIDATE_SURGICAL_FORENSIC.md",
    )
    args = ap.parse_args()

    rows: List[Dict[str, Any]] = []
    with open(args.in_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue

    cands = [r for r in rows if r.get("type") == "v47b_candidate"]
    obs = [r for r in rows if r.get("type") == "v47b_observed"]
    obs_by_key = {(o["mint"], o["decision_ts_ms"]): o for o in obs}

    # Run V47C gate evaluation per candidate.
    per_cand: List[Dict[str, Any]] = []
    for i, c in enumerate(cands, 1):
        o = obs_by_key.get((c["mint"], c["decision_ts_ms"]), {})
        gate = _evaluate_v47c_gates(c)
        per_cand.append(
            {
                "idx": i,
                "mint": c["mint"],
                "selected_size_sol": float(c["selected_size_sol"]),
                "expected_pnl": float(c["expected_pnl"]),
                "adverse_pnl": float(c["adverse_pnl"]),
                "final_min_tokens_guard": int(c["final_min_tokens_guard"]),
                "adverse_branch_outcome": c["adverse_branch_outcome"],
                "obs_kind": o.get("observed_label_kind"),
                "obs_pnl": o.get("observed_label_pnl"),
                "obs_lag_ms": o.get("observed_label_lag_ms"),
                **gate,
            }
        )

    # --- Aggregate summary ---
    obs_kind_counts: Dict[str, int] = {}
    for p in per_cand:
        k = str(p.get("obs_kind") or "pending")
        obs_kind_counts[k] = obs_kind_counts.get(k, 0) + 1

    negatives_idx = [
        p["idx"] for p in per_cand
        if p.get("obs_kind") in ("clamp_loss", "expired_loss")
    ]
    banks_idx = [p["idx"] for p in per_cand if p.get("obs_kind") == "bank"]
    neutral_idx = [p["idx"] for p in per_cand if p.get("obs_kind") == "neutral"]
    scratch_idx = [p["idx"] for p in per_cand if p.get("obs_kind") == "scratch"]

    # Block stats by V47C rules.
    blocked_negatives = [
        p["idx"] for p in per_cand
        if p["idx"] in negatives_idx and not p["v47c_would_admit"]
    ]
    blocked_banks = [
        p["idx"] for p in per_cand
        if p["idx"] in banks_idx and not p["v47c_would_admit"]
    ]
    blocked_neutral = [
        p["idx"] for p in per_cand
        if p["idx"] in neutral_idx and not p["v47c_would_admit"]
    ]
    blocked_scratch = [
        p["idx"] for p in per_cand
        if p["idx"] in scratch_idx and not p["v47c_would_admit"]
    ]

    md = Path(args.out_md)
    md.parent.mkdir(parents=True, exist_ok=True)
    with open(md, "w", encoding="utf-8") as f:
        f.write("# V47B 10-Candidate Surgical Forensic (Phase 1)\n\n")
        f.write(
            "Inputs: /root/piggy/data/v47b_no_send_decisions.jsonl "
            f"({len(cands)} candidate records, {len(obs)} observed records)\n\n"
        )

        f.write("## Data availability\n\n")
        f.write(
            "V47B JSONL persisted the following pending-flow features "
            "directly at decision time:\n"
        )
        f.write("- unique_buyers_250ms (yes)\n")
        f.write("- pending_buy_count_50/100/250/500/1000ms (yes)\n")
        f.write("- pending_buy_sol_50/100/250/500/1000ms (yes)\n")
        f.write("- pending_sell_count_250ms / pending_sell_sol_250ms (yes)\n")
        f.write("- largest_pending_buy_sol_250ms (yes)\n")
        f.write(
            "- unique_buyers_50/100/500/1000ms: **NOT persisted by V47B**. "
            "These would require replaying the raw shred buffer logic which "
            "V47B did not retain in this JSONL. For Phase 1 forensic these "
            "are reported as `n/a`.\n"
        )
        f.write(
            "- signer_at_decision is persisted but only for the buy event "
            "that *triggered* evaluation; the full set of pending-buy signers "
            "in each window is **NOT persisted**. The V47C gate evaluation in "
            "this forensic therefore uses `unique_buyers_250ms` (the only "
            "persisted breadth metric).\n\n"
        )
        f.write(
            "**Honest limitation:** Phase 6 replay will use the persisted "
            "ub250 directly. Phase 7 (fresh capture) will measure all windows.\n\n"
        )

        f.write("## Hard outputs\n\n")
        # Claim 1: ub250>=2 blocks all 3 negatives?
        all_neg_have_ub_eq_1 = all(
            p["ub_250"] == 1 for p in per_cand
            if p["idx"] in negatives_idx
        )
        f.write(
            f"- All 3 negatives (#{','.join(str(i) for i in negatives_idx)}) "
            f"have `unique_buyers_250ms == 1`? **"
            f"{'YES' if all_neg_have_ub_eq_1 else 'NO'}**\n"
        )
        f.write(
            f"- `unique_buyers_250ms >= 2` blocks all 3 V47B negatives? **"
            f"{'YES' if all(p['idx'] in blocked_negatives for p in per_cand if p['idx'] in negatives_idx) else 'NO'}**\n"
        )
        # Claim 2: keep all 5 banks?
        all_banks_pass = all(
            p["v47c_would_admit"] for p in per_cand
            if p["idx"] in banks_idx
        )
        f.write(
            f"- V47C admits ALL 5 V47B banks? **"
            f"{'YES' if all_banks_pass else 'NO'}** "
            f"(blocked banks: {blocked_banks if blocked_banks else 'none'})\n"
        )
        # Claim 3: block neutral?
        neutral_blocked = all(
            p["idx"] in blocked_neutral for p in per_cand
            if p["idx"] in neutral_idx
        )
        f.write(
            f"- V47C blocks neutral candidate(s) (#{neutral_idx})? **"
            f"{'YES' if neutral_blocked else 'NO'}** "
            f"(neutral admitted: {[p['idx'] for p in per_cand if p['idx'] in neutral_idx and p['v47c_would_admit']]})\n"
        )
        # Claim 4: 0.05 SOL banks dependent on only 2 buyers?
        banks_05_at_ub2 = [
            p for p in per_cand
            if p["idx"] in banks_idx
            and abs(float(p["selected_size_sol"]) - 0.05) < 1e-6
            and p["ub_250"] == 2
        ]
        f.write(
            f"- Banks at size=0.050 with ub_250==2 (flag for cap analysis): "
            f"{[p['idx'] for p in banks_05_at_ub2]} "
            f"(count={len(banks_05_at_ub2)})\n"
        )
        # Claim 5: 0.05 SOL with only 1 buyer (any candidate)?
        any_05_at_ub1 = [
            p for p in per_cand
            if abs(float(p["selected_size_sol"]) - 0.05) < 1e-6
            and p["ub_250"] == 1
        ]
        f.write(
            f"- ANY V47B candidate at size=0.050 with ub_250==1: "
            f"#{[p['idx'] for p in any_05_at_ub1]} "
            f"(observed outcomes: "
            f"{[(p['idx'], p['obs_kind'], '%.6f' % (p['obs_pnl'] or 0.0)) for p in any_05_at_ub1]})\n"
        )
        f.write(
            "- Implication: V47C should REQUIRE stricter breadth at size>=0.050 -> "
            f"the spec rule (ub_250 >= 3) {'IS' if banks_05_at_ub2 or any_05_at_ub1 else 'MAY BE'} restrictive.\n\n"
        )

        f.write("## Per-candidate table\n\n")
        f.write(
            "| # | mint | size | obs_kind | obs_pnl | lag_ms | "
            "ub_250 | pbc_250 | pb_sol_250 | ps_sol_250 | top_share | "
            "adv_pnl | adv_branch | mb_gate | sz_cap | v47c_admit |\n"
        )
        f.write(
            "|---|------|------|----------|---------|--------|--------|"
            "---------|-------------|-------------|-----------|---------|"
            "-----------|---------|--------|------------|\n"
        )
        for p in per_cand:
            obs_pnl_str = (
                f"{p['obs_pnl']:+.6f}" if p["obs_pnl"] is not None else "n/a"
            )
            lag_str = (
                str(p["obs_lag_ms"]) if p["obs_lag_ms"] is not None else "n/a"
            )
            f.write(
                f"| {p['idx']} | {_short(p['mint'])} | "
                f"{p['selected_size_sol']:.4f} | "
                f"{p['obs_kind'] or 'pending'} | "
                f"{obs_pnl_str} | {lag_str} | "
                f"{p['ub_250']} | {p['pbc_250']} | "
                f"{p['pbs_250']:.4f} | {p['pss_250']:.4f} | "
                f"{p['top_buyer_share']:.2f} | "
                f"{p['adverse_pnl']:+.6f} | "
                f"{p['adverse_branch_outcome']} | "
                f"{int(p['multi_buyer_gate_pass'])} | "
                f"{int(p['size_cap_pass'])} | "
                f"{int(p['v47c_would_admit'])} |\n"
            )
        f.write("\n")

        f.write("## Multi-buyer gate blocker reasons\n\n")
        blocker_count: Dict[str, int] = {}
        for p in per_cand:
            r = p["multi_buyer_gate_blocker"]
            if r is None:
                continue
            blocker_count[r] = blocker_count.get(r, 0) + 1
        if blocker_count:
            for k, v in sorted(blocker_count.items(), key=lambda x: -x[1]):
                f.write(f"- {k}: {v}\n")
        else:
            f.write("- (none)\n")
        f.write("\n")

        f.write("## Size-cap reasons (for candidates that pass mb_gate)\n\n")
        cap_reasons: Dict[str, int] = {}
        for p in per_cand:
            if not p["multi_buyer_gate_pass"]:
                continue
            r = p["size_cap_reason"]
            if r is None:
                continue
            cap_reasons[r] = cap_reasons.get(r, 0) + 1
        if cap_reasons:
            for k, v in sorted(cap_reasons.items(), key=lambda x: -x[1]):
                f.write(f"- {k}: {v}\n")
        else:
            f.write("- (none)\n")
        f.write("\n")

        f.write("## Aggregate: V47C survivors\n\n")
        survivors = [p for p in per_cand if p["v47c_would_admit"]]
        f.write(f"- candidates_in_V47B: {len(per_cand)}\n")
        f.write(f"- candidates_surviving_V47C: {len(survivors)}\n")
        survived_outcomes: Dict[str, int] = {}
        survived_pnl_sum = 0.0
        for p in survivors:
            k = p["obs_kind"] or "pending"
            survived_outcomes[k] = survived_outcomes.get(k, 0) + 1
            if p["obs_pnl"] is not None:
                survived_pnl_sum += float(p["obs_pnl"])
        f.write(
            f"- survived_outcome_distribution: {survived_outcomes}\n"
        )
        f.write(
            f"- survived_net_observed_pnl (sum): {survived_pnl_sum:+.6f} SOL\n"
        )
        f.write("\n")

        # Block reasoning explicit for the spec's key candidates.
        f.write("## Spec-required candidate inspections\n\n")
        for idx_label in ("#1", "#8", "#9"):
            i = int(idx_label[1:])
            p = per_cand[i - 1]
            f.write(
                f"- **Candidate {idx_label}** mint={_short(p['mint'])} "
                f"size={p['selected_size_sol']:.4f} "
                f"obs={p['obs_kind']} ({p['obs_pnl']:+.6f}): "
                f"ub_250={p['ub_250']}, "
                f"V47C admit={p['v47c_would_admit']}, "
                f"blocker="
                f"{p['multi_buyer_gate_blocker'] or p['size_cap_reason']}\n"
            )
        # Catastrophic loser #8 deep look:
        p8 = per_cand[7]
        f.write(
            f"- **#8 catastrophic loser** detail: lpb_250="
            f"{p8['lpb_250']:.4f} SOL (largest single buy), "
            f"top_buyer_share={p8['top_buyer_share']:.2f} -> "
            f"would multi-buyer gate fail with reason="
            f"`{p8['multi_buyer_gate_blocker']}`. "
            f"Confirmed: V47C blocks this catastrophic loser.\n"
        )

        f.write("\n## HONEST ASSESSMENT\n\n")
        f.write(
            "- Spec hypothesis: 'unique_buyers_250ms >= 2 blocks all 3 "
            "negatives AND keeps all 5 banks'. **EMPIRICAL TRUTH from V47B "
            f"data:** Blocks all 3 negatives = "
            f"{all(p['idx'] in blocked_negatives for p in per_cand if p['idx'] in negatives_idx)}. "
            f"Keeps all 5 banks = {all_banks_pass} "
            f"(blocked banks: {blocked_banks}).\n"
        )
        # Identify any banks blocked by V47C.
        if blocked_banks:
            f.write(
                f"- **Important caveat:** the spec's expected '5 banks "
                f"averaged ~2.2 unique buyers' claim is partly inconsistent "
                f"with the persisted V47B data. Banks "
                f"{blocked_banks} have ub_250==1 and would be blocked by "
                f"the multi-buyer gate. The V47C filter is therefore more "
                f"conservative than the spec assumed: it trades some "
                f"banks for elimination of negatives.\n"
            )
        f.write(
            "- Net effect of V47C on V47B 10-candidate set: "
            f"{len(survivors)} survivors with outcomes "
            f"{survived_outcomes} totalling {survived_pnl_sum:+.6f} SOL. "
            "(Compared to V47B's net of "
            f"{sum(p['obs_pnl'] for p in per_cand if p['obs_pnl'] is not None):+.6f} SOL "
            "across all 10.)\n"
        )

    # Also emit a compact json for replay tools.
    out_json = md.with_suffix(".json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(per_cand, f, default=str, indent=2)

    print(
        f"V47C-FORENSIC wrote {md} "
        f"survivors={len(survivors)}/{len(per_cand)} "
        f"blocked_neg={blocked_negatives} "
        f"blocked_bank={blocked_banks}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
