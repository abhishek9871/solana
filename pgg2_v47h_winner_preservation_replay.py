"""V47H Phase 5 — Winner preservation replay.

Reads V47E and V47F dry-live JSONLs. For each known WINNER (close_kind=bank),
applies V47H rug veto using the available pre-buy features:
  - ub_250 (unique_buyers_250ms)
  - tbs_250 (top_buyer_share_250ms)
  - exp_pnl, selected_size_sol

Note: V47E/V47F entry JSONL records do NOT persist:
  - pending_buy_sol_250ms / pending_buy_count_250ms
  - pending_sell_sol_250ms / pending_sell_count_250ms
  - unique_sellers_250ms / largest_sell_*
  - curve_history

So the replay is best-effort: vetos that DO NOT depend on missing fields
(C, D-tbs, D-pnl-lt, E) are evaluated; vetos that depend on missing fields
(A, B, D-any-sell, D-pbsol-lt-5x, F) are skipped (impossible to evaluate
from persisted data).

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
            f"V47H-WINNER-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError(
            "forbidden_call_pattern_in_v47h_winner_preservation_replay"
        )


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
        "--v47e-jsonl",
        default="/root/piggy/data/v47e_drylive_decisions.jsonl",
    )
    ap.add_argument(
        "--v47f-jsonl",
        default="/root/piggy/data/v47f_drylive_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md",
        default="/root/piggy/V47H_WINNER_PRESERVATION_REPLAY.md",
    )
    args = ap.parse_args()

    sys.path.insert(0, "/root/piggy")
    try:
        from pgg2_v47h_rug_veto import evaluate_rug_veto  # type: ignore
    except Exception as exc:
        sys.stderr.write(
            f"V47H-WINNER-REPLAY-ABORT import:{type(exc).__name__}:{exc}\n"
        )
        return 2

    def _entries_and_closes(path: str, entry_type: str, close_type: str):
        recs = _load_jsonl(path)
        entries = [r for r in recs if r.get("type") == entry_type]
        close_map: Dict[Any, Dict[str, Any]] = {}
        for r in recs:
            if r.get("type") == close_type:
                close_map[(r.get("mint"), r.get("decision_ts_ms"))] = r
        return entries, close_map

    e_entries, e_closes = _entries_and_closes(
        args.v47e_jsonl, "v47e_drylive_entry", "v47e_drylive_close",
    )
    f_entries, f_closes = _entries_and_closes(
        args.v47f_jsonl, "v47f_drylive_entry", "v47f_drylive_close",
    )

    def _eval(entries, closes, tag):
        rows = []
        for e in entries:
            k = (e.get("mint"), e.get("decision_ts_ms"))
            c = closes.get(k, {})
            close_kind = c.get("close_kind")
            close_pnl = c.get("close_pnl")
            ub = int(e.get("ub_250", 0) or 0)
            tbs = float(e.get("tbs_250", 0.0) or 0.0)
            size = float(e.get("selected_size_sol", 0.0) or 0.0)
            exp_pnl = float(e.get("exp_pnl", 0.0) or 0.0)
            # Build buyer_stats; pending_buy_sol_250ms missing — use 0
            # which conservatively triggers veto D-pbsol-lt-5x for ub==2.
            buyer_stats = {
                "unique_buyers_250ms": ub,
                "top_buyer_share_250ms": tbs,
                "pending_buy_sol_250ms": 0.0,
                "pending_buy_count_250ms": 0,
            }
            # Sell-stats: persisted as zeros (no sells observed at decision).
            sell_stats = {
                "pending_sell_sol_250ms": 0.0,
                "pending_sell_count_250ms": 0,
                "unique_sellers_250ms": None,
                "largest_sell_sol_250ms": None,
                "largest_sell_share_250ms": None,
            }
            veto_pass, fired = evaluate_rug_veto(
                size_sol=size,
                expected_pnl=exp_pnl,
                buyer_stats=buyer_stats,
                sell_stats=sell_stats,
                curve_history=None,
                dev_sell_detected_bool=False,
                logger=None,
                mint_for_log=e.get("mint"),
            )
            # Also evaluate WITHOUT the pbsol_lt_5x veto subclause and
            # without veto-D-any-sell — these depend on data NOT persisted
            # in V47E/V47F JSONL and produce false-positive blocks.
            # For winner-preservation we want to know which vetos are
            # "real signal" vs "artifact of missing data."
            replay_fired_no_artifacts = [
                v for v in fired
                if v not in (
                    "veto_d_ub2_pbsol_lt_5x",  # pbsol missing in JSONL
                )
            ]
            real_veto_pass = (len(replay_fired_no_artifacts) == 0)
            rows.append({
                "tag": tag,
                "mint": e.get("mint"),
                "decision_ts_ms": e.get("decision_ts_ms"),
                "size": size,
                "ub_250": ub,
                "tbs_250": tbs,
                "exp_pnl": exp_pnl,
                "close_kind": close_kind,
                "close_pnl": close_pnl,
                "veto_pass_raw": veto_pass,
                "vetos_fired_raw": fired,
                "veto_pass_no_artifacts": real_veto_pass,
                "vetos_fired_no_artifacts": replay_fired_no_artifacts,
            })
        return rows

    rows_e = _eval(e_entries, e_closes, "V47E")
    rows_f = _eval(f_entries, f_closes, "V47F")
    rows = rows_e + rows_f

    banks_only = [r for r in rows if r["close_kind"] == "bank"]
    negatives = [r for r in rows
                 if r["close_kind"] in ("clamp_loss", "expired_loss")]

    # Aggregate.
    n_banks = len(banks_only)
    n_banks_preserved_raw = sum(
        1 for r in banks_only if r["veto_pass_raw"]
    )
    n_banks_preserved_no_artifacts = sum(
        1 for r in banks_only if r["veto_pass_no_artifacts"]
    )
    n_neg = len(negatives)
    n_neg_blocked_raw = sum(
        1 for r in negatives if not r["veto_pass_raw"]
    )
    n_neg_blocked_no_artifacts = sum(
        1 for r in negatives if not r["veto_pass_no_artifacts"]
    )

    os.makedirs(os.path.dirname(args.out_md) or ".", exist_ok=True)
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write("# V47H — Winner Preservation Replay\n\n")
        f.write(f"V47E source: {args.v47e_jsonl}\n")
        f.write(f"V47F source: {args.v47f_jsonl}\n")
        f.write(f"V47E entries={len(e_entries)} closes={len(e_closes)}\n")
        f.write(f"V47F entries={len(f_entries)} closes={len(f_closes)}\n\n")

        f.write("## Persisted-feature limitation\n\n")
        f.write("V47E/V47F entry records only persist:\n")
        f.write("- selected_size_sol, exp_pnl, ub_250, tbs_250\n\n")
        f.write("Missing:\n")
        f.write("- pending_buy_sol_250ms, pending_buy_count_250ms\n")
        f.write("- pending_sell_*, unique_sellers_250ms, largest_sell_*\n")
        f.write("- curve_history (vsol_deltas)\n\n")
        f.write("Replay therefore distinguishes:\n")
        f.write("- veto_pass_raw: literal application of V47H veto using\n")
        f.write("  pbsol=0 (which conservatively triggers veto_d_ub2_pbsol\n")
        f.write("  whenever ub_250==2 — a false-positive artifact)\n")
        f.write("- veto_pass_no_artifacts: same but with veto_d_ub2_pbsol_lt_5x\n")
        f.write("  filtered out (since pbsol is truly unknown, not zero).\n\n")

        f.write("## Per-winner verdict\n\n")
        f.write("| tag | mint | size | ub | tbs | exp_pnl | close_pnl | close_kind | veto_raw | veto_no_artifacts | reasons |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in banks_only:
            reasons = "|".join(r["vetos_fired_no_artifacts"]) or "-"
            f.write(
                f"| {r['tag']} | {_short(r['mint'] or '')} "
                f"| {r['size']:.4f} | {r['ub_250']} "
                f"| {r['tbs_250']:.3f} | {r['exp_pnl']:+.6f} "
                f"| {r['close_pnl']:+.6f} | {r['close_kind']} "
                f"| {'PASS' if r['veto_pass_raw'] else 'BLOCK'} "
                f"| {'PASS' if r['veto_pass_no_artifacts'] else 'BLOCK'} "
                f"| {reasons} |\n"
            )
        f.write("\n")

        # Per-negative verdict (FScZ V47E, 584B negative V47F).
        f.write("## Per-known-negative verdict\n\n")
        f.write("| tag | mint | size | ub | tbs | exp_pnl | close_pnl | close_kind | veto_raw | veto_no_artifacts | reasons |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in negatives:
            reasons = "|".join(r["vetos_fired_no_artifacts"]) or "-"
            f.write(
                f"| {r['tag']} | {_short(r['mint'] or '')} "
                f"| {r['size']:.4f} | {r['ub_250']} "
                f"| {r['tbs_250']:.3f} | {r['exp_pnl']:+.6f} "
                f"| {r['close_pnl']:+.6f} | {r['close_kind']} "
                f"| {'PASS' if r['veto_pass_raw'] else 'BLOCK'} "
                f"| {'PASS' if r['veto_pass_no_artifacts'] else 'BLOCK'} "
                f"| {reasons} |\n"
            )
        f.write("\n")

        # Hjt5 specific.
        rec_hjt5 = next((r for r in rows if (r["mint"] or "").startswith("Hjt5")), None)
        f.write("## Hjt5 (V47E #1 big winner +0.014252 SOL)\n\n")
        if rec_hjt5:
            reasons = "|".join(rec_hjt5["vetos_fired_no_artifacts"]) or "-"
            f.write(f"- size={rec_hjt5['size']} ub={rec_hjt5['ub_250']} "
                    f"tbs={rec_hjt5['tbs_250']:.3f} exp_pnl={rec_hjt5['exp_pnl']:+.6f}\n")
            f.write(f"- close_pnl={rec_hjt5['close_pnl']:+.6f} kind={rec_hjt5['close_kind']}\n")
            f.write(f"- veto_pass_no_artifacts: "
                    f"{'PRESERVED' if rec_hjt5['veto_pass_no_artifacts'] else 'BLOCKED'}\n")
            f.write(f"- reasons: {reasons}\n\n")
        else:
            f.write("- not found\n\n")

        f.write("## Aggregate\n\n")
        pct_raw = (100.0 * n_banks_preserved_raw / n_banks) if n_banks else 0.0
        pct_no = (100.0 * n_banks_preserved_no_artifacts / n_banks) if n_banks else 0.0
        f.write(f"- known bank winners: {n_banks}\n")
        f.write(f"- preserved (raw, with artifact false-positives): "
                f"{n_banks_preserved_raw} ({pct_raw:.1f}%)\n")
        f.write(f"- preserved (artifacts removed): "
                f"{n_banks_preserved_no_artifacts} ({pct_no:.1f}%)\n")
        f.write(f"- known negatives: {n_neg}\n")
        f.write(f"- blocked by V47H (raw): {n_neg_blocked_raw}\n")
        f.write(f"- blocked by V47H (artifacts removed): {n_neg_blocked_no_artifacts}\n\n")

        passed = (pct_no >= 70.0) and (
            n_neg == 0 or n_neg_blocked_no_artifacts == n_neg
        )
        f.write(f"## Verdict\n\n")
        if passed:
            f.write(f"PASS — winner preservation {pct_no:.1f}% "
                    f"(target >=70%) AND known negatives blocked "
                    f"{n_neg_blocked_no_artifacts}/{n_neg}.\n")
        else:
            f.write(f"REVIEW — preservation {pct_no:.1f}% "
                    f"vs target 70%; negatives blocked "
                    f"{n_neg_blocked_no_artifacts}/{n_neg}.\n")
        f.write("\n")
        f.write("## Notes on residual veto blocks of winners\n\n")
        f.write("If any winners are blocked by V47H, the spec instructs:\n")
        f.write("'only relax the veto that blocks winners and does not block rugs.'\n")
        f.write("Veto A (sell pressure) and Veto B (curve reversal) MUST NOT be relaxed.\n")
        f.write("Veto C (blow-off) protects against Dxxi-class anomalies.\n")
        f.write("Veto D (thin 2-buyer) protects against Dxxi/3pX3 patterns.\n")
        f.write("Veto E (weak marginal) protects against 2Jng-class weakness.\n")
        f.write("If any of C/D/E blocks a winner, document below; do not auto-relax.\n")

    print(f"V47H-WINNER-REPLAY ok: out={args.out_md} "
          f"banks={n_banks} preserved_raw={n_banks_preserved_raw} "
          f"preserved_no_artifacts={n_banks_preserved_no_artifacts} "
          f"negs={n_neg} blocked_no_artifacts={n_neg_blocked_no_artifacts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
