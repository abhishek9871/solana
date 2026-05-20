"""V47C - Phase 6 replay of V47B candidates with V47C filters.

For each record in /root/piggy/data/v47b_no_send_decisions.jsonl, apply:
  1. Multi-buyer gate (Phase 2)
  2. Size cap (Phase 3)

Determine if V47C would admit or block. For admitted candidates, also
record the V47B observed outcome (for fair counterfactual analysis).

NOTE on data limitation: V47B persisted unique_buyers_250ms but NOT
unique_buyers_50/100/500/1000. The replay uses persisted ub_250 as the
only breadth metric. Phase 7 is the empirical test where all windows are
measured fresh.

Outputs:
  - /root/piggy/V47C_REPLAY_ON_V47B.md

PURE READ. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import re as _re
import sys
from pathlib import Path
from typing import Any, Dict, List


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
            f"V47C-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47c_replay")


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _import_v47c_gates():
    sys.path.insert(0, "/root/piggy")
    from pgg2_v47c_multi_buyer_gate import evaluate_multi_buyer_gate
    from pgg2_v47c_size_cap import apply_size_cap
    return evaluate_multi_buyer_gate, apply_size_cap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in-jsonl",
        default="/root/piggy/data/v47b_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md",
        default="/root/piggy/V47C_REPLAY_ON_V47B.md",
    )
    args = ap.parse_args()

    evaluate_multi_buyer_gate, apply_size_cap = _import_v47c_gates()

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

    per_cand: List[Dict[str, Any]] = []
    for i, c in enumerate(cands, 1):
        ub_250 = int(c.get("unique_buyers_250ms", 0) or 0)
        pbc_250 = int(c.get("pending_buy_count_250ms", 0) or 0)
        pbs_250 = float(c.get("pending_buy_sol_250ms", 0.0) or 0.0)
        pss_250 = float(c.get("pending_sell_sol_250ms", 0.0) or 0.0)
        lpb_250 = float(c.get("largest_pending_buy_sol_250ms", 0.0) or 0.0)
        tbs_250 = (lpb_250 / pbs_250) if pbs_250 > 0 else 0.0
        requested = float(c.get("selected_size_sol", 0.0))

        buyer_stats = {
            "unique_buyers_250ms": ub_250,
            "unique_buyers_500ms": ub_250,  # V47B did not persist 500ms; conservative
            "pending_buy_count_250ms": pbc_250,
            "pending_buy_sol_250ms": pbs_250,
            "pending_sell_sol_250ms": pss_250,
            "top_buyer_share_250ms": tbs_250,
        }

        gate_pass, gate_blocker = evaluate_multi_buyer_gate(buyer_stats)
        if gate_pass:
            capped, cap_reason = apply_size_cap(
                requested, buyer_stats, pbs_250,
            )
        else:
            capped, cap_reason = None, f"gate_blocked:{gate_blocker}"

        admit = bool(gate_pass and capped is not None)
        o = obs_by_key.get((c["mint"], c["decision_ts_ms"]), {})

        per_cand.append(
            {
                "idx": i,
                "mint": c["mint"],
                "selected_size_sol": float(requested),
                "ub_250": ub_250,
                "pbc_250": pbc_250,
                "pbs_250": pbs_250,
                "pss_250": pss_250,
                "lpb_250": lpb_250,
                "tbs_250": tbs_250,
                "multi_buyer_gate_pass": bool(gate_pass),
                "multi_buyer_gate_blocker": gate_blocker,
                "size_cap_applied": bool(
                    capped is not None and capped < requested
                ),
                "capped_size": capped,
                "size_cap_reason": cap_reason,
                "v47c_admit": bool(admit),
                "obs_kind": o.get("observed_label_kind"),
                "obs_pnl": o.get("observed_label_pnl"),
                "obs_lag_ms": o.get("observed_label_lag_ms"),
                "expected_pnl": float(c.get("expected_pnl", 0.0)),
                "adverse_pnl": float(c.get("adverse_pnl", 0.0)),
            }
        )

    # Aggregate counters.
    blocked_by_gate = [p for p in per_cand if not p["multi_buyer_gate_pass"]]
    blocked_by_cap = [
        p for p in per_cand
        if p["multi_buyer_gate_pass"] and p["capped_size"] is None
    ]
    survivors = [p for p in per_cand if p["v47c_admit"]]

    neg_kinds = ("clamp_loss", "expired_loss")
    v47b_negatives = [p for p in per_cand if p["obs_kind"] in neg_kinds]
    v47b_banks = [p for p in per_cand if p["obs_kind"] == "bank"]
    v47b_neutral = [p for p in per_cand if p["obs_kind"] == "neutral"]
    v47b_scratch = [p for p in per_cand if p["obs_kind"] == "scratch"]

    # Survivors broken down by V47B observed outcome.
    surv_neg = [p for p in survivors if p["obs_kind"] in neg_kinds]
    surv_bank = [p for p in survivors if p["obs_kind"] == "bank"]
    surv_neutral = [p for p in survivors if p["obs_kind"] == "neutral"]
    surv_scratch = [p for p in survivors if p["obs_kind"] == "scratch"]

    blocked_neg = [p for p in per_cand if p["obs_kind"] in neg_kinds and not p["v47c_admit"]]
    blocked_bank = [p for p in per_cand if p["obs_kind"] == "bank" and not p["v47c_admit"]]

    surv_net = sum(
        float(p["obs_pnl"]) for p in survivors
        if p["obs_pnl"] is not None
    )
    v47b_net = sum(
        float(p["obs_pnl"]) for p in per_cand
        if p["obs_pnl"] is not None
    )

    # Phase 6 pass condition.
    all_neg_blocked = (len(surv_neg) == 0)
    banks_preserved = (len(surv_bank) >= 4)  # >=4 of 5 V47B banks
    no_remaining_negative = (len(surv_neg) == 0)
    phase6_pass = bool(
        all_neg_blocked and banks_preserved and no_remaining_negative
    )

    md = Path(args.out_md)
    md.parent.mkdir(parents=True, exist_ok=True)
    with open(md, "w", encoding="utf-8") as f:
        f.write("# V47C Replay on V47B 10 Candidates (Phase 6)\n\n")
        f.write(f"- input_jsonl: {args.in_jsonl}\n")
        f.write(f"- v47b_candidates_loaded: {len(cands)}\n")
        f.write(f"- v47b_observed_loaded: {len(obs)}\n\n")

        f.write("## Limitation\n\n")
        f.write(
            "V47B persisted only `unique_buyers_250ms`. V47C size-cap rules "
            "reference `unique_buyers_500ms` for stricter checks above "
            "0.020 SOL; in this replay we use `unique_buyers_500ms = "
            "unique_buyers_250ms` as a conservative under-estimate (which "
            "is sound: if ub_500 is unknown but at least ub_250, then the "
            "decision is conservative, never overly permissive). Phase 7 "
            "measures all windows fresh.\n\n"
        )

        f.write("## Original V47B distribution\n\n")
        f.write(f"- candidates: {len(per_cand)}\n")
        f.write(f"- banks: {len(v47b_banks)}\n")
        f.write(f"- negatives (clamp/expired): {len(v47b_negatives)}\n")
        f.write(f"- scratch: {len(v47b_scratch)}\n")
        f.write(f"- neutral: {len(v47b_neutral)}\n")
        f.write(f"- V47B net observed PnL: {v47b_net:+.6f} SOL\n\n")

        f.write("## After V47C filter\n\n")
        f.write(f"- survivors (admitted): {len(survivors)}\n")
        f.write(
            f"- blocked_by_multi_buyer_gate: {len(blocked_by_gate)} "
            f"(idx: {[p['idx'] for p in blocked_by_gate]})\n"
        )
        f.write(
            f"- blocked_by_size_cap_after_gate_pass: {len(blocked_by_cap)} "
            f"(idx: {[p['idx'] for p in blocked_by_cap]})\n"
        )
        f.write(
            f"- surviving banks: {len(surv_bank)} (idx: {[p['idx'] for p in surv_bank]})\n"
        )
        f.write(
            f"- surviving negatives: {len(surv_neg)} (idx: {[p['idx'] for p in surv_neg]})\n"
        )
        f.write(
            f"- surviving neutral: {len(surv_neutral)} (idx: {[p['idx'] for p in surv_neutral]})\n"
        )
        f.write(
            f"- surviving scratch: {len(surv_scratch)} (idx: {[p['idx'] for p in surv_scratch]})\n"
        )
        f.write(f"- V47C survivors net observed PnL: {surv_net:+.6f} SOL\n\n")

        f.write("## Block reasons summary\n\n")
        blocker_counts: Dict[str, int] = {}
        for p in per_cand:
            if not p["v47c_admit"]:
                r = p["multi_buyer_gate_blocker"] or p["size_cap_reason"] or "unknown"
                blocker_counts[r] = blocker_counts.get(r, 0) + 1
        if blocker_counts:
            for k, v in sorted(blocker_counts.items(), key=lambda x: -x[1]):
                f.write(f"- {k}: {v}\n")
        else:
            f.write("- (none — all admitted)\n")
        f.write("\n")

        f.write("## Spec-required candidate inspections\n\n")
        # #8 catastrophic loser.
        p8 = per_cand[7]
        f.write(
            f"- **#8 (4YRQ, size=0.0500, V47B obs={p8['obs_kind']} "
            f"{p8['obs_pnl']:+.6f}):** ub_250={p8['ub_250']}, "
            f"tbs={p8['tbs_250']:.2f}. "
            f"multi_buyer_gate_pass={p8['multi_buyer_gate_pass']} "
            f"(blocker: {p8['multi_buyer_gate_blocker']}). "
            f"-> V47C **{'ADMIT' if p8['v47c_admit'] else 'BLOCK'}**. "
            f"(BLOCKED ENTIRELY by multi-buyer gate, not just size-capped.)\n"
        )
        for idx_label in ("#1", "#9"):
            i = int(idx_label[1:])
            p = per_cand[i - 1]
            f.write(
                f"- **{idx_label} ({_short(p['mint'])}, size="
                f"{p['selected_size_sol']:.4f}, V47B obs={p['obs_kind']}):** "
                f"V47C **{'ADMIT' if p['v47c_admit'] else 'BLOCK'}** "
                f"(blocker: {p['multi_buyer_gate_blocker'] or p['size_cap_reason']}).\n"
            )
        f.write("\n")

        f.write("## Per-candidate replay table\n\n")
        f.write(
            "| # | mint | size | ub_250 | tbs | mb_gate | sz_cap | "
            "capped | v47c_admit | obs_kind | obs_pnl |\n"
        )
        f.write(
            "|---|------|------|--------|-----|---------|--------|"
            "--------|------------|----------|---------|\n"
        )
        for p in per_cand:
            capped_str = (
                f"{p['capped_size']:.4f}" if p['capped_size'] is not None
                else "BLOCK"
            )
            obs_pnl = (
                f"{p['obs_pnl']:+.6f}" if p['obs_pnl'] is not None else "n/a"
            )
            f.write(
                f"| {p['idx']} | {_short(p['mint'])} | "
                f"{p['selected_size_sol']:.4f} | "
                f"{p['ub_250']} | "
                f"{p['tbs_250']:.2f} | "
                f"{int(p['multi_buyer_gate_pass'])} | "
                f"{int(p['capped_size'] is not None and p['multi_buyer_gate_pass'])} | "
                f"{capped_str} | "
                f"{int(p['v47c_admit'])} | "
                f"{p['obs_kind']} | "
                f"{obs_pnl} |\n"
            )
        f.write("\n")

        f.write("## Pass condition\n\n")
        f.write(
            f"- all V47B negatives blocked: **{all_neg_blocked}** "
            f"({len(surv_neg)}/{len(v47b_negatives)} V47B negatives "
            "survived V47C)\n"
        )
        f.write(
            f"- >=4 of 5 V47B banks preserved: **{banks_preserved}** "
            f"({len(surv_bank)}/{len(v47b_banks)} banks preserved)\n"
        )
        f.write(
            f"- no remaining negative among survivors: **{no_remaining_negative}**\n"
        )
        f.write(f"- VERDICT: **{'PASS' if phase6_pass else 'FAIL'}**\n\n")

        f.write("## HONEST ASSESSMENT\n\n")
        if phase6_pass:
            f.write(
                f"- V47C filter eliminates all V47B negatives while "
                f"preserving {len(surv_bank)}/{len(v47b_banks)} banks. "
                f"Surviving net PnL: {surv_net:+.6f} SOL.\n"
            )
        else:
            reasons = []
            if not all_neg_blocked:
                reasons.append(
                    f"survived negatives: {[p['idx'] for p in surv_neg]}"
                )
            if not banks_preserved:
                reasons.append(
                    f"only {len(surv_bank)}/{len(v47b_banks)} banks preserved"
                )
            f.write(
                f"- V47C did NOT pass Phase 6 on V47B data. "
                f"Reasons: {'; '.join(reasons)}. "
                f"This is informative; Phase 7 will provide the empirical "
                f"test on a fresh window.\n"
            )
        # Critical: trade-off explanation.
        f.write(
            f"- Trade-off: V47C is more conservative. It removes "
            f"{len(v47b_negatives) - len(surv_neg)}/{len(v47b_negatives)} "
            f"negatives and "
            f"{len(v47b_banks) - len(surv_bank)}/{len(v47b_banks)} banks. "
            f"Net PnL on V47B inputs: V47B={v47b_net:+.6f} -> V47C={surv_net:+.6f} "
            f"(delta {surv_net - v47b_net:+.6f}).\n"
        )

    print(
        f"V47C-REPLAY wrote {md} "
        f"survivors={len(survivors)}/{len(per_cand)} "
        f"phase6_pass={phase6_pass}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
