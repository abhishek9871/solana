"""V46 Phase 6 - Historical winner replay (best-effort).

For each historical V42H/V42HSAFE/V42I/V42J entry record we have a decisions
JSONL for, attempt to check whether V46 would have entered.

Reality check: V42H/V42HSAFE/V42I/V42J captures did NOT persist raw Pump
shred buy events keyed to each mint along with their wall-clock receive
timestamps. The historical JSONL files contain accountSubscribe-derived
features ONLY (curve state, virtual ticket pnl, bank events). Without
retrospective shred timestamps, the V46 lead-time check
(`source_lead_ms >= 100ms`) cannot be evaluated for those events.

What this script does:
  1. Load each available decisions JSONL.
  2. Count the records, identify which are candidates with bank/loss
     outcomes (the V42* "winners" and "losers").
  3. For each, attempt to look up a paired raw shred ts. If absent (which
     is the case for ALL historical files), record "shred_unavailable".
  4. Emit the V46_REPLAY_HISTORICAL_WINNERS.md report transparently
     stating: "shred data not retrospectively available; Phase 7 is the
     empirical test."

This is the HONEST result. Do not fabricate synthetic shred timestamps;
that would invalidate causal claims.

PURE READ. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import os
import re as _re
import sys
import time
from collections import Counter, defaultdict
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
            f"V46-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


SOURCES = [
    ("v42hsafe", "/root/piggy/data/v42hsafe_no_send_decisions.jsonl"),
    ("v42i",     "/root/piggy/data/v42i_no_send_decisions.jsonl"),
    ("v42j",     "/root/piggy/data/v42j_no_send_decisions.jsonl"),
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", default="/root/piggy/V46_REPLAY_HISTORICAL_WINNERS.md")
    ap.add_argument("--shred-archive-glob", default="/root/piggy/data/v42*_raw_shred*.jsonl")
    return ap.parse_args()


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def main() -> int:
    args = parse_args()

    per_source_stats: Dict[str, Dict[str, Any]] = {}
    historical_winners: List[Tuple[str, str, Dict[str, Any]]] = []
    historical_losses: List[Tuple[str, str, Dict[str, Any]]] = []

    for src_name, path in SOURCES:
        recs = _load_jsonl(path)
        candidates = [r for r in recs if r.get("type", "").endswith("_candidate")]
        observed = [r for r in recs if r.get("type", "").endswith("_observed")]
        banks = [r for r in observed if r.get("observed_label_kind") == "bank"]
        losses = [r for r in observed if r.get("observed_label_kind") == "loss"]
        scratches = [r for r in observed if r.get("observed_label_kind") == "scratch"]
        per_source_stats[src_name] = {
            "path": path,
            "records": len(recs),
            "candidates": len(candidates),
            "observed": len(observed),
            "banks": len(banks),
            "losses": len(losses),
            "scratches": len(scratches),
        }
        for r in banks:
            historical_winners.append((src_name, r.get("mint", ""), r))
        for r in losses:
            historical_losses.append((src_name, r.get("mint", ""), r))

    # Check for shred archive availability.
    archive_files = []
    try:
        import glob
        archive_files = glob.glob(args.shred_archive_glob)
    except Exception:
        archive_files = []
    shred_archive_available = len(archive_files) > 0

    # Build the historical winner mint set we care about (per spec):
    #   - V42H banks #2, #3, #7, #10 (not present locally - V42H ran in
    #     a different naming and we have V42HSAFE instead)
    #   - V42I banks #3, #5, #8 (V42I observed banks count)
    #   - V42J bank #2 (V42J had 1 bank observed total)
    # We surface what we actually have.

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V46 — Historical Winner Replay (Phase 6)\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("## Methodology constraint\n\n")
        f.write(
            "V42H/V42HSAFE/V42I/V42J no-send captures persisted CURVE-derived\n"
            "features only: accountSubscribe snapshots (vsol, vtok), virtual\n"
            "ticket pnl, bank-event metadata. Raw Pump shred BUY events were\n"
            "consumed live but NOT persisted to disk alongside their wall-clock\n"
            "receive timestamps and slot identities for each historical mint.\n\n"
            "Consequence: the V46 `source_lead_ms >= 100ms` precondition is\n"
            "structurally NOT evaluable on the historical entries. We cannot\n"
            "fabricate retrospective shred timestamps without invalidating the\n"
            "causality claim. Phase 7 (fresh live capture with both feeds) is\n"
            "the empirical test.\n\n"
            "Below we report what IS evaluable: the candidate/outcome ledger\n"
            "from each historical source and the per-mint mapping that V46\n"
            "would need shred archives to evaluate.\n\n"
        )
        f.write("## Source records loaded\n\n")
        f.write("| source | path | records | candidates | observed | banks | losses | scratches |\n")
        f.write("|--------|------|---------|------------|----------|-------|--------|-----------|\n")
        for s, st in per_source_stats.items():
            f.write(
                f"| {s} | {st['path']} | {st['records']} | "
                f"{st['candidates']} | {st['observed']} | "
                f"{st['banks']} | {st['losses']} | {st['scratches']} |\n"
            )
        f.write("\n")
        f.write("## V46 raw-shred visibility on historical mints\n\n")
        f.write(
            f"- shred_archive_globs_checked: {args.shred_archive_glob}\n"
            f"- shred_archive_files_found: {len(archive_files)}\n"
            f"- shred_archive_available_for_historical_replay: "
            f"{shred_archive_available}\n\n"
        )
        if not shred_archive_available:
            f.write(
                "- result: NO raw shred archive matching historical entry\n"
                "  windows is persisted on disk. Cannot retroactively evaluate\n"
                "  V46 entry on these mints. V46 entry logic relies on the\n"
                "  WALL-CLOCK timestamp at which a raw Pump BUY shred was\n"
                "  received vs. the WALL-CLOCK timestamp at which the\n"
                "  accountSubscribe curve update was received; only the latter\n"
                "  is recoverable from existing files.\n\n"
            )
        f.write("## Historical winners (banks) catalog\n\n")
        f.write(f"- total_historical_banks_observed: {len(historical_winners)}\n")
        if historical_winners:
            f.write("- per-mint listing (would-evaluate-if-shred-archive-existed):\n\n")
            f.write("| # | source | mint | observed_label_pnl | observed_label_lag_ms |\n")
            f.write("|---|--------|------|--------------------|------------------------|\n")
            for i, (src, mint, r) in enumerate(historical_winners, 1):
                pnl = r.get("observed_label_pnl")
                lag = r.get("observed_label_lag_ms")
                short = (mint[:4] + ".." + mint[-4:]) if len(mint) > 10 else mint
                f.write(
                    f"| {i} | {src} | {short} | "
                    f"{(pnl if pnl is not None else 'n/a')} | "
                    f"{(lag if lag is not None else 'n/a')} |\n"
                )
        f.write("\n## Historical losses catalog (V46 would have wanted to avoid)\n\n")
        f.write(f"- total_historical_losses_observed: {len(historical_losses)}\n")
        if historical_losses:
            f.write("| # | source | mint | observed_label_pnl | observed_label_lag_ms |\n")
            f.write("|---|--------|------|--------------------|------------------------|\n")
            for i, (src, mint, r) in enumerate(historical_losses, 1):
                pnl = r.get("observed_label_pnl")
                lag = r.get("observed_label_lag_ms")
                short = (mint[:4] + ".." + mint[-4:]) if len(mint) > 10 else mint
                f.write(
                    f"| {i} | {src} | {short} | "
                    f"{(pnl if pnl is not None else 'n/a')} | "
                    f"{(lag if lag is not None else 'n/a')} |\n"
                )
        f.write("\n## Verdict\n\n")
        f.write(
            "- replay_status: SHRED_DATA_NOT_RETROSPECTIVELY_AVAILABLE\n"
            "- V46_would_enter_count: n/a (cannot be evaluated)\n"
            "- captures_of_historical_winners: n/a\n"
            "- decision_principle: empirical test deferred to Phase 7\n"
            "- pass_criterion: this script cannot determine pass/fail; refer to\n"
            "  V46_NO_SEND_REPORT.md for the empirical answer.\n"
        )
    print(f"V46-REPLAY wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
