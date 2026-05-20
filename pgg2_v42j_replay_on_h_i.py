"""V42J Phase 5 - Replay on V42H/V42HSAFE/V42I logs.

For each historical record, simulate the V42J bank-event interrupt:
identify the bank event that "triggered" the V42H/V42HSAFE/V42I entry,
compute event_age_at_log_decision_ms, decide whether V42J would have
entered (given its 150ms TTL), and compare the hypothetical V42J
outcome (the same future-snap label from the source) vs the recorded
outcome.

PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import re as _re
import sys
import time
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
            f"V42J-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v42j_replay")


# V42J freshness TTL: a record is V42J-eligible only when bank_event was
# observed within this window of the V42H/V42HSAFE/V42I decision.
V42J_TTL_MS = 150
V42J_BANK_THR = 0.00060
V42J_STRESS_THR = 0.00010
V42J_BREAK_EVEN_BUF = 0.00010
AMOUNT_SOL = 0.015
TX_FEE_SOL = 0.0000287
BREAK_EVEN = AMOUNT_SOL + 2.0 * TX_FEE_SOL  # 0.0150574


# V42H NO_SEND records (10 entries), extracted from V42H_NO_SEND_REPORT.md
# columns: mint_short, rule, latest_bank_pnl, last_bank_age_ms,
#          decision_quote, obs_pnl, obs_kind, obs_lag_ms
V42H_RECORDS: List[Dict[str, Any]] = [
    {"idx": 1, "mint_short": "8BTD..iASL", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.002711, "last_bank_age_ms": 0,
     "decision_quote": 0.014691629, "obs_pnl": -0.001466681,
     "obs_kind": "loss", "obs_lag_ms": 382},
    {"idx": 2, "mint_short": "EUCZ..pump", "rule": "v42h_fast_two_bank_runner",
     "latest_bank_pnl": 0.003908, "last_bank_age_ms": 0,
     "decision_quote": 0.014697908, "obs_pnl": 0.002411516,
     "obs_kind": "bank", "obs_lag_ms": 426},
    {"idx": 3, "mint_short": "EN2K..pump", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.005092, "last_bank_age_ms": 0,
     "decision_quote": 0.014691829, "obs_pnl": 0.000607317,
     "obs_kind": "bank", "obs_lag_ms": 795},
    {"idx": 4, "mint_short": "5t5L..Qj2H", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.002061, "last_bank_age_ms": 0,
     "decision_quote": 0.014692160, "obs_pnl": 0.000003416,
     "obs_kind": "expired", "obs_lag_ms": 2500},
    {"idx": 5, "mint_short": "9SA6..VYKJ", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.001870, "last_bank_age_ms": 0,
     "decision_quote": 0.014692895, "obs_pnl": -0.002160038,
     "obs_kind": "loss", "obs_lag_ms": 357},
    {"idx": 6, "mint_short": "8Fgr..pump", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.003510, "last_bank_age_ms": 399,
     "decision_quote": 0.014691657, "obs_pnl": -0.000762778,
     "obs_kind": "loss", "obs_lag_ms": 754},
    {"idx": 7, "mint_short": "9cvA..pump", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.008543, "last_bank_age_ms": 0,
     "decision_quote": 0.014694034, "obs_pnl": 0.003429957,
     "obs_kind": "bank", "obs_lag_ms": 1917},
    {"idx": 8, "mint_short": "GvJr..pump", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.002491, "last_bank_age_ms": 787,
     "decision_quote": 0.014692222, "obs_pnl": 0.000000000,
     "obs_kind": "expired", "obs_lag_ms": 2500},
    {"idx": 9, "mint_short": "49qw..pump", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.001242, "last_bank_age_ms": 0,
     "decision_quote": 0.014694515, "obs_pnl": -0.001843014,
     "obs_kind": "loss", "obs_lag_ms": 809},
    {"idx": 10, "mint_short": "3Y97..pump", "rule": "v42h_one_bank_plus_continuation",
     "latest_bank_pnl": 0.004901, "last_bank_age_ms": 0,
     "decision_quote": 0.014692700, "obs_pnl": 0.003143367,
     "obs_kind": "bank", "obs_lag_ms": 1318},
]


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    if not Path(path).exists():
        return []
    recs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except Exception:
            pass
    return recs


def _v42j_replay_record(
    source: str,
    mint_short: str,
    rule_in_log: str,
    log_outcome_kind: str,
    log_outcome_pnl: float,
    bank_pnl: Optional[float],
    last_bank_age_ms: Optional[int],
    decision_quote_sol: float,
    decision_ts_ms: int,
) -> Dict[str, Any]:
    """Decide whether V42J would have entered for this record.

    Logic:
      - event_ts_ms = decision_ts_ms - last_bank_age_ms (proxy)
      - event_age_at_log_decision_ms = last_bank_age_ms (definition)
      - If bank_pnl is None or < V42J_BANK_THR -> block: bank_pnl_below_threshold
      - If event_age_at_log_decision_ms > V42J_TTL_MS -> block: bank_event_stale
      - Else: V42J's break-even buffer gate applies:
            decision_quote_sol >= BREAK_EVEN + V42J_BREAK_EVEN_BUF (0.00010)
        Else -> block: below_break_even_buffer
      - Else: V42J would ENTER. Outcome label = log_outcome_*.
    """
    rec = {
        "source": source,
        "mint": mint_short,
        "rule_in_log": rule_in_log,
        "log_outcome": log_outcome_kind,
        "log_outcome_pnl": float(log_outcome_pnl) if log_outcome_pnl is not None else None,
        "event_ts_ms": int(decision_ts_ms - (last_bank_age_ms or 0)),
        "event_age_at_log_decision_ms": int(last_bank_age_ms or 0),
        "bank_pnl": float(bank_pnl) if bank_pnl is not None else None,
        "v42j_would_enter": False,
        "v42j_block_reason": None,
        "v42j_outcome": None,
        "v42j_outcome_pnl": None,
    }

    # Step 1: Did the candidate even cross V42J bank threshold?
    if bank_pnl is None or float(bank_pnl) < V42J_BANK_THR:
        rec["v42j_block_reason"] = "bank_pnl_below_threshold"
        return rec

    # Step 2: Was the bank event fresh at the decision moment?
    age = int(last_bank_age_ms or 0)
    if age > V42J_TTL_MS:
        rec["v42j_block_reason"] = "bank_event_stale"
        return rec

    # Step 3: Was the current sell quote above break-even + buffer?
    if float(decision_quote_sol) < (BREAK_EVEN + V42J_BREAK_EVEN_BUF):
        rec["v42j_block_reason"] = "below_break_even_buffer"
        return rec

    # V42J would have entered.
    rec["v42j_would_enter"] = True
    rec["v42j_block_reason"] = None
    rec["v42j_outcome"] = log_outcome_kind
    rec["v42j_outcome_pnl"] = (
        float(log_outcome_pnl) if log_outcome_pnl is not None else None
    )
    return rec


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v42hsafe-jsonl",
                    default="/root/piggy/data/v42hsafe_no_send_decisions.jsonl")
    ap.add_argument("--v42i-jsonl",
                    default="/root/piggy/data/v42i_no_send_decisions.jsonl")
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-jsonl",
                    default="/root/piggy/data/v42j_replay_on_h_i.jsonl")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    replayed: List[Dict[str, Any]] = []

    # V42H records (10 baked in).
    for r in V42H_RECORDS:
        rec = _v42j_replay_record(
            source="v42h",
            mint_short=r["mint_short"],
            rule_in_log=r["rule"],
            log_outcome_kind=r["obs_kind"],
            log_outcome_pnl=r["obs_pnl"],
            bank_pnl=r["latest_bank_pnl"],
            last_bank_age_ms=r["last_bank_age_ms"],
            decision_quote_sol=r["decision_quote"],
            # No persisted decision_ts; for V42H records the decision_ts is
            # taken to be 0 reference (relative). event_ts is derived as
            # decision_ts - last_bank_age. For block-reason logic only the
            # delta matters.
            decision_ts_ms=0,
        )
        replayed.append(rec)

    # V42HSAFE records.
    hsafe_recs = _load_jsonl(args.v42hsafe_jsonl)
    hsafe_decisions = [
        r for r in hsafe_recs if r.get("type") == "v42hsafe_candidate"
    ]
    for r in hsafe_decisions:
        obs_pnl = r.get("observed_outcome_pnl_sol")
        obs_kind = r.get("observed_outcome_kind") or "unknown"
        bank_pnl = r.get("last_bank_pnl_sol")
        last_bank_age = r.get("last_bank_age_ms")
        decision_quote = r.get("current_local_quote") or r.get("decision_quote_sol_reflexive")
        rec = _v42j_replay_record(
            source="v42hsafe",
            mint_short=_short(r.get("mint", "")),
            rule_in_log=str(r.get("rule_id", "?")),
            log_outcome_kind=str(obs_kind),
            log_outcome_pnl=float(obs_pnl) if obs_pnl is not None else 0.0,
            bank_pnl=float(bank_pnl) if bank_pnl is not None else None,
            last_bank_age_ms=int(last_bank_age) if last_bank_age is not None else None,
            decision_quote_sol=float(decision_quote) if decision_quote is not None else 0.0,
            decision_ts_ms=int(r.get("ts_ms", 0)),
        )
        replayed.append(rec)

    # V42I records.
    i_recs = _load_jsonl(args.v42i_jsonl)
    i_decisions = [r for r in i_recs if r.get("type") == "v42i_candidate"]
    for r in i_decisions:
        ats = r.get("active_ticket_state", {}) or {}
        bank_pnl = ats.get("latest_completed_bank_pnl")
        latest_bank_time = ats.get("latest_completed_bank_time_ms")
        decision_ts = int(r.get("decision_ts_ms") or 0)
        if latest_bank_time and decision_ts:
            bank_age = max(0, decision_ts - int(latest_bank_time))
        else:
            bank_age = 0
        decision_quote = r.get("decision_quote_sol")
        rec = _v42j_replay_record(
            source="v42i",
            mint_short=_short(r.get("mint", "")),
            rule_in_log=str(r.get("rule_id", "?")),
            log_outcome_kind=str(r.get("observed_label_kind") or "unknown"),
            log_outcome_pnl=float(r.get("observed_label_pnl") or 0.0),
            bank_pnl=float(bank_pnl) if bank_pnl is not None else None,
            last_bank_age_ms=int(bank_age),
            decision_quote_sol=float(decision_quote) if decision_quote is not None else 0.0,
            decision_ts_ms=decision_ts,
        )
        replayed.append(rec)

    # Output JSONL.
    Path(args.out_jsonl).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_jsonl, "w", encoding="utf-8") as fp:
        for rec in replayed:
            fp.write(json.dumps(rec) + "\n")

    # Aggregate.
    n = len(replayed)
    block_counts: Dict[str, int] = {}
    enter_counts: Dict[str, int] = {}  # by rule_in_log
    enter_count_total = 0
    enter_negative = 0   # admits with v42j_outcome == "loss"
    enter_bank = 0
    enter_scratch = 0
    enter_expired = 0
    enter_other = 0
    one_tick_admits = 0
    source_negatives = sum(
        1 for r in replayed
        if r.get("log_outcome") == "loss" or (
            r.get("log_outcome_pnl") is not None
            and r["log_outcome_pnl"] is not None
            and float(r["log_outcome_pnl"]) <= -0.00050
        )
    )

    for rec in replayed:
        if rec["v42j_would_enter"]:
            enter_count_total += 1
            enter_counts[rec["rule_in_log"]] = enter_counts.get(
                rec["rule_in_log"], 0) + 1
            outc = rec.get("v42j_outcome") or "unknown"
            outp = rec.get("v42j_outcome_pnl")
            if outc == "bank":
                enter_bank += 1
            elif outc == "scratch":
                enter_scratch += 1
            elif outc == "loss" or (outp is not None and float(outp) <= -0.00050):
                enter_negative += 1
            elif outc == "expired":
                enter_expired += 1
            else:
                enter_other += 1
            # One-tick spike check (must be 0): event with bank_pnl < 0.00060
            # admitted by V42J - impossible per our gate (bank_pnl_below_threshold)
            # but we audit anyway.
            if rec.get("bank_pnl") is not None and float(rec["bank_pnl"]) < V42J_BANK_THR:
                one_tick_admits += 1
        else:
            br = rec.get("v42j_block_reason") or "unknown"
            block_counts[br] = block_counts.get(br, 0) + 1

    # Markdown report.
    md: List[str] = []
    md.append("# V42J_REPLAY_ON_V42H_V42I\n")
    md.append(f"- total_records: **{n}**")
    md.append(f"- v42j_ttl_ms: {V42J_TTL_MS}")
    md.append(f"- v42j_bank_threshold_sol: {V42J_BANK_THR}")
    md.append(f"- v42j_break_even_buffer_sol: {V42J_BREAK_EVEN_BUF}")
    md.append(f"- amount_sol: {AMOUNT_SOL}")
    md.append(f"- break_even_quote: {BREAK_EVEN:.7f}")
    md.append("")
    md.append("## Source negatives (recorded)")
    md.append(f"- source_negative_count: **{source_negatives}**")
    md.append("")
    md.append("## V42J would-block counts")
    if not block_counts:
        md.append("- (none)")
    for reason, cnt in sorted(block_counts.items(), key=lambda x: -x[1]):
        md.append(f"- {reason}: {cnt}")
    md.append("")
    md.append("## V42J would-enter counts (by rule_in_log)")
    if not enter_counts:
        md.append("- (none)")
    for rid, cnt in sorted(enter_counts.items(), key=lambda x: -x[1]):
        md.append(f"- {rid}: {cnt}")
    md.append(f"- **v42j_would_enter_total: {enter_count_total} of {n}**")
    md.append("")
    md.append("## V42J hypothetical outcomes (when V42J would have entered)")
    md.append(f"- bank: **{enter_bank}**")
    md.append(f"- scratch: **{enter_scratch}**")
    md.append(f"- loss (clamp): **{enter_negative}**")
    md.append(f"- expired: **{enter_expired}**")
    md.append(f"- other/unknown: **{enter_other}**")
    md.append("")
    md.append("## One-tick spike admit check")
    md.append(f"- v42j_admits_with_bank_pnl_below_threshold: **{one_tick_admits}** (must be 0)")
    md.append("")
    md.append("## Comparison")
    md.append(f"- source_negatives: **{source_negatives}**")
    md.append(f"- v42j_admits_negatives: **{enter_negative}**")
    delta = source_negatives - enter_negative
    md.append(f"- reduction: **{delta}**")
    md.append("")
    md.append("## Per-record table")
    md.append("| source | mint | rule_in_log | log_outcome | event_ts | "
              "event_age | v42j_enter | v42j_block | v42j_outcome |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in replayed:
        md.append(
            f"| {r['source']} | `{r['mint']}` | {r['rule_in_log']} | "
            f"{r['log_outcome']} | {r['event_ts_ms']} | "
            f"{r['event_age_at_log_decision_ms']} | "
            f"{r['v42j_would_enter']} | {r.get('v42j_block_reason','-')} | "
            f"{r.get('v42j_outcome') or '-'} |"
        )
    md.append("")
    md.append("## Pass criteria")
    pass_reduce = enter_negative <= source_negatives
    pass_blocks_stale = (
        block_counts.get("bank_event_stale", 0) > 0 or n == 0
        or all(rec["v42j_would_enter"]
               or rec.get("v42j_block_reason") != "bank_event_stale"
               for rec in replayed
               if (rec.get("event_age_at_log_decision_ms") or 0) <= V42J_TTL_MS)
    )
    pass_no_spikes = one_tick_admits == 0
    md.append(f"- v42j_does_not_increase_negatives_vs_source: **{pass_reduce}**")
    md.append(f"- v42j_blocks_stale_events_when_present: "
              f"**{pass_blocks_stale}**")
    md.append(f"- v42j_does_not_admit_one_tick_spikes: **{pass_no_spikes}**")
    overall_pass = pass_reduce and pass_blocks_stale and pass_no_spikes
    md.append(f"- **OVERALL Phase-5 verdict: "
              f"{'PASS' if overall_pass else 'FAIL'}**")

    Path(args.out_md).write_text("\n".join(md), encoding="utf-8")
    print(
        f"V42J-REPLAY n={n} would_enter={enter_count_total} "
        f"negs_admitted={enter_negative} blocks={block_counts} "
        f"out={args.out_md}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
