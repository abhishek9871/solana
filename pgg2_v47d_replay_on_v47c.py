"""V47D - Replay on V47C candidates.

Reads the V47C no-send decisions JSONL and re-applies the V47D pipeline:
  - boundary guard at original size
  - downsizer (if original blocked)
  - estimate outcome at final size

Output: V47D_REPLAY_ON_V47C.md
  - per-candidate verdict (blocked / downsized / admitted_as_is)
  - hard outputs: 47eK loss, 4 banks preserved, scratches/neutrals
  - aggregate: V47D survivors with size + outcome
  - PASS/FAIL: any remaining negative?

NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import os
import re as _re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
            f"V47D-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "-"
    return mint[:4] + ".." + mint[-4:]


def _load_decisions(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _pair(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cands = [r for r in records if r.get("type") == "v47c_candidate"]
    obs = [r for r in records if r.get("type") == "v47c_observed"]
    omap: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for o in obs:
        omap[(str(o.get("mint", "")), int(o.get("decision_ts_ms", 0)))] = o
    for c in cands:
        key = (str(c.get("mint", "")), int(c.get("decision_ts_ms", 0)))
        o = omap.get(key)
        if o is not None:
            c["observed_label_pnl"] = o.get("observed_label_pnl")
            c["observed_label_kind"] = o.get("observed_label_kind")
            c["observed_label_lag_ms"] = o.get("observed_label_lag_ms")
    return cands


def _exp_pnl_at(c: Dict[str, Any], sz: float) -> Optional[float]:
    sr = c.get("all_size_results") or {}
    key = f"{sz:.3f}"
    if key in sr:
        return float(sr[key].get("expected_pnl", 0.0))
    return None


def _selectable_at(c: Dict[str, Any], sz: float) -> Tuple[bool, Optional[str]]:
    sr = c.get("all_size_results") or {}
    key = f"{sz:.3f}"
    if key not in sr:
        return (False, "size_not_evaluated")
    sel = bool(sr[key].get("selectable", False))
    return (sel, sr[key].get("blocker") if not sel else None)


def _estimate_outcome_at_size(
    obs_pnl: Optional[float],
    original_size: float,
    final_size: float,
    obs_kind: Optional[str],
) -> Tuple[Optional[float], Optional[str]]:
    """Estimate observed PnL at downsized final size.

    Heuristic: PnL scales linearly with size for small sizes (curve impact
    is sub-linear for tiny sizes vs total reserve). Linear scaling is a
    CONSERVATIVE estimate for our purposes:
      - If observed was a loss, smaller size -> smaller loss (we may
        cross the LOSS_TH = -0.00050 threshold and become non-negative)
      - If observed was a bank, smaller size -> smaller gain (may cross
        BANK_TH = 0.00060 threshold and become neutral/scratch)
    """
    if obs_pnl is None or original_size <= 0:
        return (None, None)
    if abs(final_size - original_size) < 1e-9:
        return (float(obs_pnl), obs_kind)
    ratio = final_size / original_size
    est = float(obs_pnl) * ratio
    if est >= 0.00060:
        return (est, "bank_est")
    if est <= -0.00050:
        return (est, "clamp_loss_est")
    if abs(est) < 0.00005:
        return (est, "scratch_est")
    if est > 0:
        return (est, "neutral_est")
    return (est, "expired_loss_est")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--jsonl", default="/root/piggy/data/v47c_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md", default="/root/piggy/V47D_REPLAY_ON_V47C.md",
    )
    args = ap.parse_args()

    sys.path.insert(0, "/root/piggy")
    from pgg2_v47d_boundary_guard import (  # type: ignore
        evaluate_boundary_guard,
    )
    from pgg2_v47d_downsizer import (  # type: ignore
        downsize_candidate,
    )

    records = _load_decisions(args.jsonl)
    cands = _pair(records)

    rows: List[Dict[str, Any]] = []
    for c in cands:
        buyer_stats = {
            "unique_buyers_250ms": int(c.get("unique_buyers_250ms", 0) or 0),
            "unique_buyers_500ms": int(c.get("unique_buyers_500ms", 0) or 0),
            "pending_buy_count_250ms": int(
                c.get("pending_buy_count_250ms", 0) or 0
            ),
            "pending_buy_sol_250ms": float(
                c.get("pending_buy_sol_250ms", 0.0) or 0.0
            ),
            "pending_sell_sol_250ms": float(
                c.get("pending_sell_sol_250ms", 0.0) or 0.0
            ),
            "top_buyer_share_250ms": float(
                c.get("top_buyer_share_250ms", 0.0) or 0.0
            ),
            "largest_buy_sol_250ms": float(
                c.get("largest_pending_buy_sol_250ms", 0.0) or 0.0
            ),
        }
        size = float(c.get("selected_size_sol", 0.0) or 0.0)
        exp_pnl = float(c.get("expected_pnl", 0.0) or 0.0)
        adv_branch = str(c.get("adverse_branch_outcome", "") or "")
        passes, blocker = evaluate_boundary_guard(
            size_sol=size,
            buyer_stats=buyer_stats,
            expected_pnl=exp_pnl,
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=adv_branch,
        )

        def _epfn(sz: float) -> float:
            v = _exp_pnl_at(c, sz)
            return float(v if v is not None else 0.0)

        def _bfn(sz: float) -> Tuple[bool, Optional[str]]:
            return _selectable_at(c, sz)

        final_size, action, reason = downsize_candidate(
            initial_selected_size=size,
            buyer_stats=buyer_stats,
            expected_pnl_fn=_epfn,
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=adv_branch,
            branch_check_fn=_bfn,
        )

        obs_pnl = c.get("observed_label_pnl")
        obs_kind = c.get("observed_label_kind")
        obs_lag = c.get("observed_label_lag_ms")

        if passes:
            verdict = "admitted_as_is"
            v_size = size
        elif final_size is not None:
            verdict = (
                "downsized"
                if abs(final_size - size) > 1e-9
                else "admitted_as_is"
            )
            v_size = final_size
        else:
            verdict = "blocked"
            v_size = None

        est_pnl, est_outcome = (None, None)
        if v_size is not None and obs_pnl is not None:
            est_pnl, est_outcome = _estimate_outcome_at_size(
                obs_pnl, size, v_size, obs_kind,
            )

        rows.append({
            "mint": str(c.get("mint", "")),
            "decision_ts_ms": int(c.get("decision_ts_ms", 0)),
            "original_size": size,
            "ub_250": buyer_stats["unique_buyers_250ms"],
            "ub_500": buyer_stats["unique_buyers_500ms"],
            "tbs_250": buyer_stats["top_buyer_share_250ms"],
            "pbs_250": buyer_stats["pending_buy_sol_250ms"],
            "exp_pnl_orig": exp_pnl,
            "adv_branch": adv_branch,
            "obs_kind": obs_kind,
            "obs_pnl": obs_pnl,
            "obs_lag_ms": obs_lag,
            "v47d_verdict": verdict,
            "v47d_blocker": blocker,
            "v47d_final_size": v_size,
            "v47d_downsize_action": action,
            "v47d_downsize_reason": reason,
            "est_pnl_at_final": est_pnl,
            "est_outcome_at_final": est_outcome,
        })

    # Counts
    verdict_counts: Counter = Counter(r["v47d_verdict"] for r in rows)
    forty_seven_ek = [r for r in rows if r["mint"].startswith("47eK")]
    forty_seven_ek_blocked_at_020 = (
        len(forty_seven_ek) > 0
        and forty_seven_ek[0]["v47d_blocker"] == "ub_2_size_geq_020_blocked"
    )
    forty_seven_ek_final = (
        forty_seven_ek[0]["v47d_final_size"] if forty_seven_ek else None
    )
    forty_seven_ek_action = (
        forty_seven_ek[0]["v47d_downsize_action"] if forty_seven_ek else "-"
    )
    forty_seven_ek_est_pnl = (
        forty_seven_ek[0]["est_pnl_at_final"] if forty_seven_ek else None
    )
    forty_seven_ek_est_outcome = (
        forty_seven_ek[0]["est_outcome_at_final"] if forty_seven_ek else None
    )

    banks = [r for r in rows if r["obs_kind"] == "bank"]
    banks_admitted = [
        r for r in banks
        if r["v47d_verdict"] in ("admitted_as_is", "downsized")
    ]
    all_banks_admitted = (len(banks) == len(banks_admitted))

    scratches_or_neutrals = [
        r for r in rows if r["obs_kind"] in ("scratch", "neutral")
    ]

    # Negative outcomes after V47D
    remaining_neg = []
    for r in rows:
        if r["v47d_verdict"] == "blocked":
            continue
        eo = r["est_outcome_at_final"]
        if eo in ("clamp_loss_est", "expired_loss_est"):
            remaining_neg.append(r)

    overall_pass = (len(remaining_neg) == 0) and forty_seven_ek_blocked_at_020

    md = Path(args.out_md)
    md.parent.mkdir(parents=True, exist_ok=True)
    with open(md, "w", encoding="utf-8") as f:
        f.write("# V47D Replay on V47C Candidates\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- input_jsonl: {args.jsonl}\n")
        f.write(f"- v47c_candidates: {len(rows)}\n\n")

        f.write("## Original V47C distribution\n\n")
        orig_kinds: Counter = Counter()
        for r in rows:
            orig_kinds[r["obs_kind"] or "pending"] += 1
        for k, v in orig_kinds.most_common():
            f.write(f"- {k}: {v}\n")
        f.write("\n")

        f.write("## V47D verdict per candidate\n\n")
        f.write(
            "| # | mint | orig_size | ub_250 | tbs | exp_pnl | obs_kind | "
            "obs_pnl | obs_lag | v47d_verdict | blocker | final_size | "
            "est_outcome | est_pnl |\n"
            "|---|------|-----------|--------|-----|---------|----------|"
            "---------|---------|--------------|---------|------------|"
            "-------------|---------|\n"
        )
        for i, r in enumerate(rows, 1):
            f.write(
                f"| {i} | {_short(r['mint'])} | "
                f"{r['original_size']:.4f} | "
                f"{r['ub_250']} | {r['tbs_250']:.3f} | "
                f"{r['exp_pnl_orig']:+.6f} | "
                f"{r['obs_kind'] or '-'} | "
                f"{('%+.6f' % r['obs_pnl']) if r['obs_pnl'] is not None else 'n/a'} | "
                f"{r['obs_lag_ms'] if r['obs_lag_ms'] is not None else '-'} | "
                f"{r['v47d_verdict']} | "
                f"{r['v47d_blocker'] or '-'} | "
                f"{('%.4f' % r['v47d_final_size']) if r['v47d_final_size'] is not None else '-'} | "
                f"{r['est_outcome_at_final'] or '-'} | "
                f"{('%+.6f' % r['est_pnl_at_final']) if r['est_pnl_at_final'] is not None else '-'} |\n"
            )
        f.write("\n")

        f.write("## Hard outputs\n\n")
        f.write(
            f"- 47eK loss BLOCKED at 0.020 SOL by V47D rule A: "
            f"{'YES' if forty_seven_ek_blocked_at_020 else 'NO'}\n"
        )
        if forty_seven_ek_final is not None:
            f.write(
                f"- 47eK downsized to {forty_seven_ek_final:.4f} SOL "
                f"(action={forty_seven_ek_action}). "
                f"Est outcome at smaller size: "
                f"{forty_seven_ek_est_outcome} "
                f"(est_pnl="
                f"{forty_seven_ek_est_pnl:+.6f}"
                f")\n"
            )
        else:
            f.write(
                f"- 47eK could NOT be downsized safely under V47D "
                f"(full block - safer outcome).\n"
            )
        f.write(
            f"- All 4 V47C banks admitted (as-is or downsized): "
            f"{'YES' if all_banks_admitted else 'NO'} "
            f"({len(banks_admitted)}/{len(banks)})\n"
        )
        f.write(
            f"- Scratches/neutrals ({len(scratches_or_neutrals)} total): "
        )
        s_admitted = [
            r for r in scratches_or_neutrals
            if r["v47d_verdict"] in ("admitted_as_is", "downsized")
        ]
        s_blocked = [
            r for r in scratches_or_neutrals
            if r["v47d_verdict"] == "blocked"
        ]
        f.write(
            f"admitted={len(s_admitted)}, blocked={len(s_blocked)} "
            "(blocks are acceptable when size violates V47D)\n"
        )

        f.write("\n## V47D verdict distribution\n\n")
        for k in ("admitted_as_is", "downsized", "blocked"):
            f.write(f"- {k}: {verdict_counts.get(k, 0)}\n")

        f.write("\n## Aggregate V47D survivors with size + outcome\n\n")
        survivors = [r for r in rows if r["v47d_verdict"] != "blocked"]
        f.write(f"- total_survivors: {len(survivors)}\n")
        size_dist: Counter = Counter()
        outcome_dist: Counter = Counter()
        for r in survivors:
            if r["v47d_final_size"] is not None:
                size_dist[f"{r['v47d_final_size']:.4f}"] += 1
            outcome_dist[r["est_outcome_at_final"] or "unknown"] += 1
        f.write("- final_size_distribution:\n")
        for k in sorted(size_dist.keys()):
            f.write(f"  - {k}: {size_dist[k]}\n")
        f.write("- est_outcome_distribution:\n")
        for k, v in outcome_dist.most_common():
            f.write(f"  - {k}: {v}\n")

        f.write("\n## Verdict\n\n")
        f.write(
            f"- remaining_negative_estimates: {len(remaining_neg)}\n"
        )
        for rn in remaining_neg:
            f.write(
                f"  - {_short(rn['mint'])} final={rn['v47d_final_size']:.4f} "
                f"est={rn['est_outcome_at_final']} "
                f"(est_pnl={rn['est_pnl_at_final']:+.6f})\n"
            )
        f.write(
            f"- 47eK_specific_block_pass: "
            f"{'YES' if forty_seven_ek_blocked_at_020 else 'NO'}\n"
        )
        f.write(
            f"- OVERALL_VERDICT: {'PASS' if overall_pass else 'FAIL'} "
            "(no remaining negative AND 47eK blocked by rule A)\n"
        )

    print(f"V47D-REPLAY wrote {md}")
    print(
        f"V47D-REPLAY admitted_as_is="
        f"{verdict_counts.get('admitted_as_is', 0)} "
        f"downsized={verdict_counts.get('downsized', 0)} "
        f"blocked={verdict_counts.get('blocked', 0)} "
        f"remaining_neg={len(remaining_neg)} "
        f"47eK_blocked_ruleA={int(forty_seven_ek_blocked_at_020)} "
        f"pass={int(overall_pass)}"
    )
    return 0 if overall_pass else 3


if __name__ == "__main__":
    sys.exit(main())
