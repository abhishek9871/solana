"""V47H Phase 4 — Replay V47G no-send candidates through the V47H rug veto.

For each `v47g_candidate` record in /root/piggy/data/v47g_no_send_decisions.jsonl
this tool reconstructs the (buyer_stats, sell_stats, curve_history)
inputs that V47H_RUG_VETO would have seen and records whether the veto
would have admitted or blocked the candidate.

Note: V47G JSONL does not persist curve_history (only decision_curve_state
single point), so veto B is dormant in replay. Veto A subclauses that
depend on V47H-specific keys (unique_sellers_250ms, largest_sell_*) are
also dormant — only veto A_sell_pressure_35pct and A_sell_count_geq_buy
can fire from V47G data.

PURE READ. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import os
import re as _re
import sys
from typing import Any, Dict, List, Optional


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
            f"V47H-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47h_replay_on_v47g")


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
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


def _short(mint: str) -> str:
    if not mint or len(mint) < 6:
        return mint or "?"
    return mint[:6]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--v47g-jsonl",
        default="/root/piggy/data/v47g_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md", default="/root/piggy/V47H_REPLAY_ON_V47G.md",
    )
    args = ap.parse_args()

    sys.path.insert(0, "/root/piggy")
    try:
        from pgg2_v47h_rug_veto import evaluate_rug_veto  # type: ignore
    except Exception as exc:
        sys.stderr.write(
            f"V47H-REPLAY-ABORT import_rug_veto:{type(exc).__name__}:{exc}\n"
        )
        return 2

    recs = _load_jsonl(args.v47g_jsonl)
    cands = [r for r in recs if r.get("type") == "v47g_candidate"]
    obs_map: Dict[Any, Dict[str, Any]] = {}
    for r in recs:
        t = r.get("type", "")
        if t in ("v47g_observed", "v47g_observed_watchdog"):
            obs_map[(r.get("mint"), r.get("decision_ts_ms"))] = r

    results: List[Dict[str, Any]] = []
    for c in cands:
        buyer_stats = {
            "unique_buyers_250ms": int(c.get("unique_buyers_250ms", 0)),
            "top_buyer_share_250ms": float(c.get("top_buyer_share_250ms", 0.0)),
            "pending_buy_sol_250ms": float(c.get("pending_buy_sol_250ms", 0.0)),
            "pending_buy_count_250ms": int(c.get("pending_buy_count_250ms", 0)),
        }
        sell_stats = {
            "pending_sell_sol_250ms": float(c.get("pending_sell_sol_250ms", 0.0)),
            "pending_sell_count_250ms": int(c.get("pending_sell_count_250ms", 0)),
            # Replay limitation: not persisted in V47G JSONL — pass None
            # so veto A subclauses for breadth/largest-share remain dormant.
            "unique_sellers_250ms": None,
            "largest_sell_sol_250ms": None,
            "largest_sell_share_250ms": None,
        }
        # Curve history not persisted — pass None (veto B dormant in replay).
        veto_pass, fired = evaluate_rug_veto(
            size_sol=float(c.get("selected_size_sol", 0.0)),
            expected_pnl=float(c.get("expected_pnl", 0.0)),
            buyer_stats=buyer_stats,
            sell_stats=sell_stats,
            curve_history=None,
            dev_sell_detected_bool=False,
            logger=None,
            mint_for_log=c.get("mint"),
        )
        obs = obs_map.get((c.get("mint"), c.get("decision_ts_ms")), {})
        results.append({
            "mint": c.get("mint"),
            "decision_ts_ms": c.get("decision_ts_ms"),
            "size": float(c.get("selected_size_sol", 0.0)),
            "exp_pnl": float(c.get("expected_pnl", 0.0)),
            "ratio": (
                float(c.get("expected_pnl", 0.0))
                / float(c.get("selected_size_sol", 1.0))
                if float(c.get("selected_size_sol", 0.0)) > 0 else 0.0
            ),
            "ub_250": int(c.get("unique_buyers_250ms", 0)),
            "tbs_250": float(c.get("top_buyer_share_250ms", 0.0)),
            "pbsol_250": float(c.get("pending_buy_sol_250ms", 0.0)),
            "veto_pass": veto_pass,
            "vetos_fired": fired,
            "_obs_kind": obs.get("observed_label_kind"),
            "_obs_pnl": obs.get("observed_label_pnl"),
            "_obs_lag_ms": obs.get("observed_label_lag_ms"),
        })

    # Compose report.
    os.makedirs(os.path.dirname(args.out_md) or ".", exist_ok=True)
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write("# V47H — Replay on V47G no-send candidates\n\n")
        f.write(f"Source: {args.v47g_jsonl}\n")
        f.write(f"Candidates replayed: {len(results)}\n\n")

        f.write("## Per-candidate verdict\n\n")
        f.write("| mint | size | exp_pnl | ratio | ub | tbs | pbsol | obs_kind | obs_pnl | obs_lag_ms | veto_pass | reasons |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            reasons = "|".join(r["vetos_fired"]) if r["vetos_fired"] else "-"
            f.write(
                f"| {_short(r['mint'] or '')} "
                f"| {r['size']:.4f} "
                f"| {r['exp_pnl']:+.6f} "
                f"| {r['ratio']:+.3f} "
                f"| {r['ub_250']} "
                f"| {r['tbs_250']:.3f} "
                f"| {r['pbsol_250']:.3f} "
                f"| {r['_obs_kind']} "
                f"| {r['_obs_pnl']} "
                f"| {r['_obs_lag_ms']} "
                f"| {'PASS' if r['veto_pass'] else 'BLOCK'} "
                f"| {reasons} |\n"
            )
        f.write("\n")

        # Specific checks.
        rec_2jng = next((r for r in results
                         if (r["mint"] or "").startswith("2Jng")), None)
        rec_dxxi = next((r for r in results
                         if (r["mint"] or "").startswith("Dxxi")), None)
        rec_3px3 = next((r for r in results
                         if (r["mint"] or "").startswith("3pX3")), None)

        f.write("## Specific checks\n\n")
        if rec_2jng:
            reasons = "|".join(rec_2jng["vetos_fired"]) or "-"
            f.write(f"### 2Jng (known sub-500ms rug @ 395ms)\n")
            f.write(f"- size={rec_2jng['size']}, exp_pnl={rec_2jng['exp_pnl']:+.6f}, "
                    f"ratio={rec_2jng['ratio']:+.3f}, ub={rec_2jng['ub_250']}, "
                    f"tbs={rec_2jng['tbs_250']:.3f}\n")
            f.write(f"- veto_pass={'PASS' if rec_2jng['veto_pass'] else 'BLOCK'}\n")
            f.write(f"- reasons: {reasons}\n\n")
        if rec_dxxi:
            reasons = "|".join(rec_dxxi["vetos_fired"]) or "-"
            f.write(f"### Dxxi (known sub-500ms rug @ 414ms)\n")
            f.write(f"- size={rec_dxxi['size']}, exp_pnl={rec_dxxi['exp_pnl']:+.6f}, "
                    f"ratio={rec_dxxi['ratio']:+.3f}, ub={rec_dxxi['ub_250']}, "
                    f"tbs={rec_dxxi['tbs_250']:.3f}\n")
            f.write(f"- veto_pass={'PASS' if rec_dxxi['veto_pass'] else 'BLOCK'}\n")
            f.write(f"- reasons: {reasons}\n\n")
        if rec_3px3:
            reasons = "|".join(rec_3px3["vetos_fired"]) or "-"
            f.write(f"### 3pX3 (pending — no observed outcome)\n")
            f.write(f"- size={rec_3px3['size']}, exp_pnl={rec_3px3['exp_pnl']:+.6f}, "
                    f"ratio={rec_3px3['ratio']:+.3f}, ub={rec_3px3['ub_250']}, "
                    f"tbs={rec_3px3['tbs_250']:.3f}\n")
            f.write(f"- veto_pass={'PASS' if rec_3px3['veto_pass'] else 'BLOCK'}\n")
            f.write(f"- reasons: {reasons}\n\n")

        # Scratches preserved?
        scratches = [r for r in results if r["_obs_kind"] == "scratch"]
        preserved = [r for r in scratches if r["veto_pass"]]
        f.write(f"### V47G scratches preserved\n")
        f.write(f"- total V47G scratches: {len(scratches)}\n")
        f.write(f"- preserved by V47H: {len(preserved)}\n")
        if scratches:
            f.write("- detail:\n")
            for r in scratches:
                v = "PASS" if r["veto_pass"] else "BLOCK"
                reasons = "|".join(r["vetos_fired"]) or "-"
                f.write(
                    f"  - {_short(r['mint'] or '')} ratio={r['ratio']:+.3f} "
                    f"ub={r['ub_250']} → {v} reasons={reasons}\n"
                )
        f.write("\n")

        # Summary verdict.
        total_rugs = [
            r for r in results
            if r["_obs_kind"] in ("clamp_loss", "expired_loss")
            and r["_obs_lag_ms"] is not None
            and int(r["_obs_lag_ms"]) < 500
        ]
        blocked_rugs = [r for r in total_rugs if not r["veto_pass"]]
        f.write("## Aggregate\n\n")
        f.write(f"- known sub-500ms rugs: {len(total_rugs)}\n")
        f.write(f"- rugs blocked by V47H veto: {len(blocked_rugs)} "
                f"({len(blocked_rugs)}/{len(total_rugs)})\n")
        f.write(f"- scratches preserved by V47H: {len(preserved)}/{len(scratches)}\n")

        all_neg_blocked = (len(blocked_rugs) == len(total_rugs))
        f.write(f"\n## Verdict\n\n")
        if all_neg_blocked and len(preserved) == len(scratches):
            f.write("PASS — all sub-500ms rugs blocked, all V47G scratches preserved.\n")
        elif all_neg_blocked:
            f.write("PARTIAL — all rugs blocked but some scratches also blocked.\n")
        else:
            f.write("FAIL — some sub-500ms rugs would still admit.\n")

        f.write("\n## Replay limitations\n\n")
        f.write("- V47G JSONL does NOT persist:\n")
        f.write("  - unique_sellers_250ms (veto A subclause: dormant)\n")
        f.write("  - largest_sell_sol/share_250ms (veto A subclause: dormant)\n")
        f.write("  - curve_history vsol_deltas (veto B fully dormant)\n")
        f.write("  - dev/creator wallet (veto F always dormant)\n")
        f.write("- These vetos are wired live in the V47H no-send capture and add\n")
        f.write("  additional protection that this replay can't demonstrate.\n")

    print(f"V47H-REPLAY ok: out={args.out_md} cands={len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
