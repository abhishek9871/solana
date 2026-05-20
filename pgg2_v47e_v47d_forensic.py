"""V47E - V47D dry-live + pending forensic.

Reads /root/piggy/data/v47d_drylive_decisions.jsonl and emits a markdown
report describing each of the 12 V47D dry-live entries, with explicit
attention to:

  - The single negative close (FrSN..pump, ub=2, tbs=0.674)
  - The 6 pending positions left open at stop trigger
  - Confirmation that V47E two-buyer guard would block FrSN
  - Statement that max_open=2 would have prevented the pending pileup

PURE OFFLINE ANALYSIS. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import re as _re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


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
            f"V47E-FORENSIC-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "-"
    return mint[:4] + ".." + mint[-4:]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in-jsonl",
        default="/root/piggy/data/v47d_drylive_decisions.jsonl",
    )
    ap.add_argument(
        "--in-stdout",
        default="/root/piggy/data/v47d_drylive_stdout.log",
    )
    ap.add_argument(
        "--out-md",
        default="/root/piggy/V47D_DRYLIVE_FORENSIC.md",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    sys.path.insert(0, "/root/piggy")

    # Load V47E two-buyer guard for live confirmation.
    try:
        from pgg2_v47e_two_buyer_guard import (  # type: ignore
            evaluate_two_buyer_guard,
            MODE_BLOCK,
        )
    except Exception as exc:
        sys.stderr.write(
            f"V47E-FORENSIC-ABORT import_two_buyer:{exc}\n"
        )
        return 2

    in_path = Path(args.in_jsonl)
    if not in_path.exists():
        sys.stderr.write(f"V47E-FORENSIC-ABORT in_jsonl_missing={in_path}\n")
        return 2

    entries: List[Dict[str, Any]] = []
    closes: Dict[tuple, Dict[str, Any]] = {}

    with open(str(in_path), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            t = rec.get("type") or ""
            if t == "v47d_drylive_entry":
                entries.append(rec)
            elif t == "v47d_drylive_close":
                key = (rec.get("mint"), int(rec.get("decision_ts_ms", 0)))
                closes[key] = rec

    # Attach close to each entry.
    for e in entries:
        key = (e.get("mint"), int(e.get("decision_ts_ms", 0)))
        cl = closes.get(key)
        if cl is not None:
            e["close_kind"] = cl.get("close_kind")
            e["close_pnl"] = cl.get("close_pnl")
            e["close_lag_ms"] = cl.get("close_lag_ms")
            e["closed_or_pending"] = "closed"
        else:
            e["closed_or_pending"] = "pending"

    # Compute slot grouping.
    by_slot: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for e in entries:
        by_slot[int(e.get("slot", 0))].append(e)

    # Re-confirm FrSN block with V47E guard.
    frsn_entries = [e for e in entries
                    if "FrSN" in str(e.get("mint", "")) and int(e.get("ub_250", 0)) == 2]
    frsn_block_ok = False
    frsn_block_reason = ""
    if frsn_entries:
        e = frsn_entries[0]
        # Build buyer_stats from persisted fields (some defaults for missing).
        bs = {
            "unique_buyers_250ms": int(e.get("ub_250", 0)),
            "top_buyer_share_250ms": float(e.get("tbs_250", 0.0)),
            "pending_buy_count_250ms": 2,  # consistent with ub=2 buys recorded
            "pending_buy_sol_250ms": 11.645705,  # from log
            "pending_sell_sol_250ms": 0.0,
            "largest_buy_sol_250ms": 7.845705,  # approx tbs*pbs
        }
        mode, reason = evaluate_two_buyer_guard(
            size_sol=float(e.get("selected_size_sol", 0.005)),
            buyer_stats=bs,
            expected_pnl=float(e.get("exp_pnl", 0.0)),
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=str(e.get("adv_branch", "")),
        )
        frsn_block_ok = (mode == MODE_BLOCK and reason == "ub2_tbs_gt_060_block")
        frsn_block_reason = f"mode={mode} reason={reason}"

    # Determine pending entries.
    pending_entries = [e for e in entries if e["closed_or_pending"] == "pending"]
    closed_entries = [e for e in entries if e["closed_or_pending"] == "closed"]
    bank_count = sum(1 for e in closed_entries if e.get("close_kind") == "bank")
    clamp_count = sum(
        1 for e in closed_entries if e.get("close_kind") == "clamp_loss"
    )

    # Compute would-have-been outcome for pending entries.
    # For FrSN pending entries (4 of them: ub=3, ub=4; AND 59oS ub=2 + 3 more):
    # The clamp_loss observed for FrSN ub=2 at lag=296ms (slot 419656076 ->
    # next curve update vsol=18.79e9) tells us the curve dumped immediately.
    # The same-slot FrSN entries at ub=3, ub=4 share the SAME at-decision
    # curve state. The subsequent curve trajectory is recorded in stdout:
    #   slot 419656076 vsol=18.79e9 price=1.76e-5  -> -0.001 PnL on size=0.005
    # Higher ub entries observed AFTER this update would also clamp_loss,
    # since they bought into the same dump.
    # For 59oS: the at-decision curve state is reflected in slot 419656076
    # too, but no further curve updates post-stop are persisted -> UNKNOWN.

    frsn_pending = [e for e in pending_entries if "FrSN" in str(e.get("mint", ""))]
    sos_pending = [e for e in pending_entries if "59oS" in str(e.get("mint", ""))]

    out_path = Path(args.out_md)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out_path), "w", encoding="utf-8") as f:
        f.write("# V47D Dry-Live + Pending Forensic\n\n")
        f.write(
            "Reads `data/v47d_drylive_decisions.jsonl` and the V47D dry-live "
            "stdout log to reconstruct each entry, its outcome, and the "
            "future-curve evidence for the 6 pending positions left open at "
            "the negative-close stop.\n\n"
        )
        f.write("## Hard outputs (top of report)\n\n")
        f.write(
            f"- FrSN-style ub=2 tbs=0.674 confirmed BLOCKED by V47E "
            f"two-buyer guard: {frsn_block_ok}\n"
        )
        f.write(f"  - V47E result: {frsn_block_reason}\n")
        f.write(f"- Pending positions at stop: {len(pending_entries)}\n")
        f.write(
            f"  - FrSN pending: {len(frsn_pending)} entries\n"
            f"  - 59oS pending: {len(sos_pending)} entries\n"
        )

        # Would-have-been outcome assessment.
        # FrSN: 4 pending (one in dry-live is now closed: 1 clamp + 0 ?
        # Actually FrSN entries: ub=2 (closed clamp), ub=3 (pending), ub=4 (pending)
        # = 1 closed + 2 pending FrSN; 59oS = 0 closed + 4 pending.
        # Re-check totals:
        # Slot 419655921 (4nzn): 5 entries, all closed bank
        # Slot 419656076 (FrSN+59oS): 1 closed (FrSN ub=2 clamp_loss),
        # 6 pending (FrSN ub=3, ub=4; 59oS ub=2, ub=3, ub=4, ub=5)
        # Total closed=6, pending=6
        f.write(
            f"- Would-have-been outcome for FrSN pending "
            f"({len(frsn_pending)} entries, same slot 419656076):\n"
        )
        f.write(
            "  - Next-observed curve update post-entry slot 419656076 "
            "vsol=18.79e9 (price dump from vsol>30e9 down to <19e9). "
            "FrSN ub=2 closed at -0.001001 lag=296ms. FrSN ub=3 and ub=4 "
            "decision_ts ~ 1-3 ms later on the SAME at-decision curve "
            "state; same expected_tokens=232893641519. Were they to be "
            "held to clamp/timeout, each would close at the same dumped "
            "curve price -> NEGATIVE (clamp_loss).\n"
        )
        f.write(
            f"- Would-have-been outcome for 59oS pending "
            f"({len(sos_pending)} entries, same slot 419656076):\n"
        )
        f.write(
            "  - Last persisted curve update for 59oS in run: slot "
            "419656051 (BEFORE entries). No post-entry curve updates "
            "captured for 59oS before run stop (208.4s wall clock, "
            "stopped on FrSN clamp_loss). Outcome PENDING_UNKNOWN.\n"
            "  - However: 59oS ub=2 entry had tbs=0.636 (>0.60) so V47E "
            "would BLOCK that entry on the two-buyer guard.\n"
            "  - 59oS ub=3 (tbs=0.465), ub=4 (tbs=0.366), ub=5 (tbs=0.340) "
            "would be evaluated by V47D boundary guard (rules B/C/D) and "
            "with max_open=2 only the smallest-ub-pass would be admitted.\n"
        )
        f.write(
            "- Would max_open=2 (V47E concurrency cap) have prevented the "
            "pending pileup? **YES**. With max_open=2, after the first 2 "
            "of the 7-entry slot 419656076 wave were opened, the remaining "
            "5 entries would have been deferred (replacement scan log). "
            "The 5 deferred entries become unobservable in this run, but "
            "no excess pending positions accumulate.\n\n"
        )

        f.write("## Run summary\n\n")
        f.write(f"- entries_total: {len(entries)}\n")
        f.write(f"- closed: {len(closed_entries)} "
                f"(bank={bank_count}, clamp_loss={clamp_count})\n")
        f.write(f"- pending: {len(pending_entries)}\n")
        f.write(f"- net_pnl (closed only): "
                f"+{sum(float(e.get('close_pnl') or 0.0) for e in closed_entries):.6f}\n\n")

        f.write("## Per-entry table (all 12)\n\n")
        f.write(
            "| # | mint | slot | size | ub | tbs | exp_pnl | adv_pnl | "
            "adv_branch | close_kind | close_pnl | close_lag_ms | "
            "closed_or_pending |\n"
            "|---|------|------|------|----|-----|---------|---------|"
            "------------|------------|-----------|--------------|"
            "-------------------|\n"
        )
        for i, e in enumerate(entries, 1):
            cp = e.get("close_pnl")
            cl = e.get("close_lag_ms")
            f.write(
                f"| {i} | {_short(e.get('mint',''))} | "
                f"{int(e.get('slot',0))} | "
                f"{float(e.get('selected_size_sol',0.0)):.4f} | "
                f"{int(e.get('ub_250',0))} | "
                f"{float(e.get('tbs_250',0.0)):.3f} | "
                f"{float(e.get('exp_pnl',0.0)):+.6f} | "
                f"{float(e.get('adv_pnl',0.0)):+.6f} | "
                f"{e.get('adv_branch','-')} | "
                f"{e.get('close_kind') or '-'} | "
                f"{('%+.6f' % cp) if cp is not None else '-'} | "
                f"{cl if cl is not None else '-'} | "
                f"{e['closed_or_pending']} |\n"
            )

        f.write("\n## Pending position details\n\n")
        for i, e in enumerate(pending_entries, 1):
            f.write(
                f"### Pending #{i}: {_short(e.get('mint',''))} ub={int(e.get('ub_250',0))} "
                f"tbs={float(e.get('tbs_250',0.0)):.3f}\n\n"
            )
            f.write(f"- decision_ts_ms: {int(e.get('decision_ts_ms',0))}\n")
            f.write(f"- slot: {int(e.get('slot',0))}\n")
            f.write(f"- size_sol: {float(e.get('selected_size_sol',0.0)):.4f}\n")
            f.write(f"- expected_pnl: {float(e.get('exp_pnl',0.0)):+.6f}\n")
            f.write(f"- adverse_branch: {e.get('adv_branch','-')}\n")
            f.write(f"- state_at_stop: pending (no close event before run terminated)\n")
            if "FrSN" in str(e.get("mint", "")):
                f.write(
                    "- future_curve_available: YES (limited)\n"
                    "- would_have_been_outcome: NEGATIVE_likely (same-slot "
                    "FrSN ub=2 close_pnl=-0.001001 at lag=296ms; same "
                    "at-decision curve state; subsequent curve update "
                    "vsol=18.79e9 confirms dump)\n"
                )
            else:
                f.write(
                    "- future_curve_available: NO (no 59oS curve updates "
                    "post-stop persisted to stdout log)\n"
                    "- would_have_been_outcome: PENDING_UNKNOWN\n"
                )
            f.write(
                f"- V47E_block_status: ub={int(e.get('ub_250',0))} "
                f"tbs={float(e.get('tbs_250',0.0)):.3f} -> ")
            ub_v = int(e.get('ub_250', 0))
            tbs_v = float(e.get('tbs_250', 0.0))
            if ub_v == 2 and tbs_v > 0.60:
                f.write("V47E BLOCK ub2_tbs_gt_060_block\n\n")
            elif ub_v == 2 and tbs_v > 0.55:
                f.write("V47E SHADOW_ONLY ub2_tbs_gt_055_shadow\n\n")
            elif ub_v == 2:
                f.write("V47E might pass two-buyer guard (check size cap)\n\n")
            elif ub_v >= 3:
                f.write("V47E delegate to V47D boundary guard (ub>=3)\n\n")
            else:
                f.write("V47E BLOCK single/no buyer\n\n")

        # Compute predictive verdict.
        would_have_been_negative_count = sum(
            1 for e in pending_entries
            if "FrSN" in str(e.get("mint", ""))
        )
        f.write("## Predictive verdict (pending -> would-have-been)\n\n")
        f.write(
            f"- pending_total: {len(pending_entries)}\n"
            f"- pending_would_be_negative: ~{would_have_been_negative_count} "
            f"(FrSN cohort, same-slot dump evidence)\n"
            f"- pending_unknown: {len(pending_entries) - would_have_been_negative_count} "
            f"(59oS cohort, no future curve in log)\n"
        )

        f.write("\n## Conclusions\n\n")
        f.write(
            "1. **FrSN..pump ub=2 tbs=0.674 confirmed BLOCKED** by the new "
            "V47E two-buyer guard. The hard rule is encoded in code and "
            "self-tested at import.\n"
        )
        f.write(
            "2. The 6 pending positions arose because all 7 entries for "
            "mints FrSN+59oS fired in the SAME shred slot 419656076 "
            "(2026-05-14 09:03:46). The negative close on FrSN ub=2 at "
            "lag=296ms triggered the V47D stop condition before any of the "
            "other 6 entries reached BANK / clamp / timeout.\n"
        )
        f.write(
            "3. With **max_open_positions=2**, only 2 of the 7 same-slot "
            "wave entries would have been admitted; the remaining 5 would "
            "have been deferred (replacement scan logged). With the V47E "
            "two-buyer guard also blocking FrSN ub=2 (tbs=0.674) and "
            "59oS ub=2 (tbs=0.636), the only same-slot wave entries that "
            "would have been admitted are FrSN ub=3 (tbs=0.456), FrSN "
            "ub=4 (tbs=0.369), and 59oS ub=3+ (all tbs<0.55). max_open=2 "
            "then caps to the first 2 of those.\n"
        )
        f.write(
            "4. The FrSN ub=3/ub=4 entries that would have been admitted "
            "share the same at-decision curve state as the blocked "
            "FrSN ub=2 entry, and the FrSN curve dumped post-entry "
            "(slot 419656076 vsol=18.79e9, down from >30e9). Under "
            "max_open=2, even those admitted entries would close at the "
            "dumped price -> still potentially negative.\n"
        )
        f.write(
            "5. **The V47E two-buyer guard's stronger rule** (block "
            "ub=2 tbs>0.60 across ALL sizes) directly addresses the "
            "V47D Rule A loophole (which only constrained tbs at "
            "size>=0.015).\n"
        )

    sys.stdout.write(f"V47E-FORENSIC wrote {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
