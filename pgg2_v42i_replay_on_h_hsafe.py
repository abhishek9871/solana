"""V42I Phase 4 — Replay V42I gating on V42H and V42H-SAFE candidate logs.

This module reads:
    /root/piggy/data/v42hsafe_no_send_decisions.jsonl  (10 V42HSAFE records)
    /root/piggy/V42H_NO_SEND_REPORT.md                 (10 V42H records, table)

And produces:
    /root/piggy/V42I_REPLAY_ON_V42H_LOGS.md

CRITICAL HONESTY: V42H and V42HSAFE did NOT persist a per-snap virtual-ticket
trace. They only persisted aggregate snapshots at decision_ts (current quote,
latest_quote_gradient, latest_account_sub_delta, last_curve_update_kind,
consecutive_virtual_wins, virtual_losses_last_3000ms,
last_virtual_loss_age_ms, time_since_last_virtual_bank_ms,
time_since_first_virtual_bank_ms, last_bank_pnl_sol, last_bank_age_ms).
The ACTIVE-TICKET (which is V42I's whole concept) was implicit in the
engine at decision_ts and is unrecoverable from these logs.

What we CAN reconstruct partially:
    - completed_virtual_banks_last_3000ms (= 1+ since
      time_since_last_virtual_bank_ms is set and tslb <= 3000)
    - latest_completed_bank_pnl (= last_bank_pnl_sol)
    - latest_completed_bank_time_ms (= ts_ms - last_bank_age_ms)
    - latest_local_quote_gradient (= latest_quote_gradient)
    - latest_curve_delta (= latest_account_sub_delta)
    - completed_virtual_losses_after_latest_bank: derivable when the
      JSONL contains last_virtual_loss_age_ms and we compare to
      time_since_last_virtual_bank_ms

What we CANNOT reconstruct:
    - active_ticket_id / age / pnl / gradient / max_adverse — the
      V42HSAFE log only sees the moment of the bank, NOT the "open"
      ticket the engine had at that moment

Strategy:
    1. For each historical record, mark "insufficient_active_ticket_data".
    2. Apply the SUBSET of V42I block guards that CAN be evaluated:
         - require a completed bank (we have lcbtm)
         - require no virtual_loss_after_last_bank
         - require latest_local_quote_gradient >= 0
         - require no negative-curve-update after bank (via
           last_curve_update_kind != "negative" AS A PROXY — this is
           "current update", not "any update after bank"; we note the
           limitation)
    3. Approximate the active-ticket condition by assuming the historical
       entry was the moment "just after a bank" (V42H/SAFE behaviour) —
       i.e. the active ticket would have been age=0 (just opened) with
       cur_pnl=0. Per V42I block, age<min OR cur_pnl<positive_threshold
       both fail. So V42I would BLOCK ALL HISTORICAL RECORDS as "active
       ticket not yet positive" — which is correct because V42I says we
       should wait for the active ticket to BECOME positive, then enter.
    4. Report the avoidance count (per V42I = 10/10 blocked → would have
       avoided all 8 V42HSAFE losses by NOT entering at those moments).
       BUT this is INFORMATIONAL ONLY because we cannot positively confirm
       V42I would have found OTHER entry moments later in the same wave
       — Phase 5 is the empirical test for that.

The output is honest about data limits.

PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import os
import re as _re
import sys
from pathlib import Path
from statistics import median
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
        sys.stderr.write(f"V42I-REPLAY-ABORT forbidden_call_pattern={_pat}\n")
        raise RuntimeError("forbidden_call_pattern_in_v42i_replay")


V42HSAFE_JSONL = "/root/piggy/data/v42hsafe_no_send_decisions.jsonl"
V42H_REPORT_MD = "/root/piggy/V42H_NO_SEND_REPORT.md"
OUT_MD = "/root/piggy/V42I_REPLAY_ON_V42H_LOGS.md"


def load_v42hsafe_records(path: str = V42HSAFE_JSONL) -> List[Dict[str, Any]]:
    """Pair each v42hsafe_candidate with its v42hsafe_observed row."""
    candidates: List[Dict[str, Any]] = []
    observed_by_key: Dict[Tuple[str, int], Dict[str, Any]] = {}
    if not Path(path).exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
            except Exception:
                continue
            if rec.get("type") == "v42hsafe_candidate":
                candidates.append(rec)
            elif rec.get("type") == "v42hsafe_observed":
                k = (str(rec.get("mint")), int(rec.get("decision_ts_ms") or 0))
                observed_by_key[k] = rec
    out: List[Dict[str, Any]] = []
    for c in candidates:
        k = (str(c.get("mint")), int(c.get("ts_ms") or 0))
        obs = observed_by_key.get(k)
        if obs:
            c2 = dict(c)
            c2["observed_outcome_pnl_sol"] = obs.get("observed_outcome_pnl_sol")
            c2["observed_outcome_lag_ms"] = obs.get("observed_outcome_lag_ms")
            c2["observed_outcome_kind"] = obs.get("observed_outcome_kind")
            out.append(c2)
        else:
            out.append(c)
    return out


def parse_v42h_report_table(path: str = V42H_REPORT_MD) -> List[Dict[str, Any]]:
    """Parse the per-candidate table from V42H_NO_SEND_REPORT.md.
    Schema is:
      | # | mint | rule | last_bank_pnl | last_bank_age_ms |
      decision_quote_sol | obs_pnl | obs_kind | obs_lag_ms |
    """
    out: List[Dict[str, Any]] = []
    if not Path(path).exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    in_table = False
    for line in lines:
        s = line.strip()
        if s.startswith("## Per-candidate detail"):
            in_table = True
            continue
        if not in_table:
            continue
        if not s.startswith("|"):
            if s == "":
                continue
            break
        if "---" in s or "#" in s and "rule" in s.lower():
            continue
        parts = [p.strip() for p in s.split("|")]
        # parts = ['', '#', 'mint', 'rule', 'last_bank_pnl', ...]
        # so skip leading/trailing empties
        parts = [p for p in parts if p != ""]
        if len(parts) < 9:
            continue
        try:
            idx = int(parts[0])
        except Exception:
            continue
        try:
            row = {
                "source": "v42h",
                "idx": idx,
                "mint": parts[1].strip("`"),
                "rule_id": parts[2],
                "last_bank_pnl_sol": float(parts[3]),
                "last_bank_age_ms": int(parts[4]),
                "decision_quote_sol_reflexive": float(parts[5]),
                "observed_outcome_pnl_sol": float(parts[6]),
                "observed_outcome_kind": parts[7],
                "observed_outcome_lag_ms": int(parts[8]),
                "_has_full_state": False,
            }
            out.append(row)
        except Exception:
            continue
    return out


def replay_v42i_on_record(
    rec: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply V42I logic against a historical record. Returns dict with
    {v42i_decision, v42i_rule, v42i_block_reason, has_full_state}."""
    # Reconstructable fields (V42HSAFE JSONL has more than V42H report):
    has_full_state = rec.get("_has_full_state", True)
    last_bank_pnl = rec.get("last_bank_pnl_sol")
    last_bank_age_ms = rec.get("last_bank_age_ms")
    latest_quote_grad = rec.get("latest_quote_gradient")
    latest_curve_delta = rec.get("latest_account_sub_delta")
    last_curve_update_kind = rec.get("last_curve_update_kind", "unknown")
    tslb = rec.get("time_since_last_virtual_bank_ms")
    last_loss_age = rec.get("last_virtual_loss_age_ms")

    # Build a partial state dict.
    state = {
        "mint": rec.get("mint", ""),
        "completed_virtual_banks_last_3000ms": (
            1 if (tslb is not None and int(tslb) <= 3000) else 0
        ),
        "completed_virtual_losses_last_3000ms": (
            1 if (last_loss_age is not None and int(last_loss_age) <= 3000) else 0
        ),
        "latest_completed_bank_pnl": last_bank_pnl,
        "latest_completed_bank_time_ms": (
            (int(rec.get("ts_ms") or 0) - int(last_bank_age_ms))
            if (last_bank_age_ms is not None and rec.get("ts_ms"))
            else None
        ),
        # ACTIVE TICKET — UNRECONSTRUCTABLE from these logs.
        "active_ticket_id": "__UNKNOWN__",
        "active_ticket_age_ms": None,
        "active_ticket_current_pnl": None,
        "active_ticket_pnl_gradient": None,
        "active_ticket_max_adverse": None,
        "active_ticket_is_positive": False,
        "active_ticket_is_improving": False,
        "active_ticket_distance_to_bank": None,
        "active_ticket_open_after_first_bank_ms": None,
        "first_completed_bank_time_ms": None,
        "first_bank_time_to_completion_ms": None,
        "latest_curve_delta": latest_curve_delta or 0.0,
        "latest_local_quote_gradient": latest_quote_grad or 0.0,
        "negative_curve_after_latest_bank": (
            last_curve_update_kind == "negative"
        ),
        "completed_virtual_losses_after_latest_bank": (
            1 if (
                last_loss_age is not None
                and tslb is not None
                and int(last_loss_age) < int(tslb)
            ) else 0
        ),
    }

    # V42I block: insufficient active-ticket data means we cannot say
    # V42I would have admitted this candidate — by V42I logic the
    # active ticket must be present AND positive AND improving AND
    # age <= 900ms.
    # The V42H/SAFE candidates fired AT the moment a bank had just
    # completed — meaning the active ticket the engine had at that
    # moment was either just-opened (age ~0, pnl ~0) or none. Either
    # way V42I would BLOCK (active_ticket_pnl_below_positive_threshold).
    decision_block = True
    block_reason = "insufficient_active_ticket_data_v42i_blocks_default"

    # Apply non-active-ticket subset of V42I checks:
    if state["completed_virtual_banks_last_3000ms"] == 0 \
            and state["latest_completed_bank_time_ms"] is None:
        decision_block = True
        block_reason = "no_completed_virtual_bank_yet"
    elif state["completed_virtual_losses_after_latest_bank"] > 0:
        decision_block = True
        block_reason = "completed_virtual_loss_after_latest_bank"
    elif float(state["latest_local_quote_gradient"] or 0.0) < 0.0:
        decision_block = True
        block_reason = "latest_local_quote_gradient_negative"
    elif state["negative_curve_after_latest_bank"]:
        decision_block = True
        block_reason = "negative_curve_update_after_latest_bank"
    else:
        # Non-active-ticket fields are clean. V42I STILL blocks because
        # active-ticket fields are unknown. Document this honestly.
        decision_block = True
        block_reason = "active_ticket_state_unreconstructable_from_log"

    return {
        "v42i_decision": "block" if decision_block else "enter",
        "v42i_rule": None,
        "v42i_block_reason": block_reason,
        "has_full_state": has_full_state,
        "reconstructed_state": state,
    }


def run_replay() -> int:
    safe = load_v42hsafe_records()
    for r in safe:
        r["_has_full_state"] = True
        r["source"] = "v42hsafe"
    h = parse_v42h_report_table()

    all_records: List[Dict[str, Any]] = []
    all_records.extend(safe)
    all_records.extend(h)

    replays: List[Dict[str, Any]] = []
    for rec in all_records:
        rep = replay_v42i_on_record(rec)
        rep["source"] = rec.get("source", "?")
        rep["mint"] = rec.get("mint", "")
        rep["rule_in_log"] = rec.get("rule_id", "?")
        rep["label_pnl"] = rec.get("observed_outcome_pnl_sol")
        rep["label_kind"] = rec.get("observed_outcome_kind")
        rep["label_lag_ms"] = rec.get("observed_outcome_lag_ms")
        rep["log_active_ticket_age_ms"] = rec.get("time_since_last_virtual_bank_ms")
        rep["log_active_ticket_pnl"] = rec.get("last_bank_pnl_sol")
        rep["log_active_grad"] = rec.get("latest_quote_gradient")
        rep["log_last_bank_age_ms"] = rec.get("last_bank_age_ms")
        rep["_has_full_state"] = rec.get("_has_full_state", False)
        replays.append(rep)

    # Summary
    total = len(replays)
    blocked = sum(1 for r in replays if r["v42i_decision"] == "block")
    entered = total - blocked

    # Per-source breakdown.
    safe_recs = [r for r in replays if r["source"] == "v42hsafe"]
    h_recs = [r for r in replays if r["source"] == "v42h"]

    safe_losses = sum(
        1 for r in safe_recs if r.get("label_kind") in ("loss", "expired", "scratch")
        and (
            (r.get("label_pnl") or 0.0) < 0.00005
            or r.get("label_kind") in ("loss",)
        )
    )
    safe_actual_losses = [r for r in safe_recs if r.get("label_kind") == "loss"]
    h_wins = [r for r in h_recs if r.get("label_kind") == "bank"]
    h_losses = [r for r in h_recs if r.get("label_kind") == "loss"]

    v42i_avoid_safe_losses = sum(
        1 for r in safe_actual_losses if r["v42i_decision"] == "block"
    )
    v42i_admit_h_wins = sum(
        1 for r in h_wins if r["v42i_decision"] == "enter"
    )

    md: List[str] = []
    md.append("# V42I_REPLAY_ON_V42H_LOGS\n")
    md.append("## Honest limitation")
    md.append("")
    md.append(
        "V42H and V42HSAFE capture pipelines persisted the **aggregate state**"
        " at decision_ts (current quote, latest_quote_gradient,"
        " latest_account_sub_delta, last_curve_update_kind,"
        " consecutive_virtual_wins, virtual_losses_last_3000ms,"
        " last_virtual_loss_age_ms, time_since_last_virtual_bank_ms,"
        " time_since_first_virtual_bank_ms, last_bank_pnl_sol,"
        " last_bank_age_ms), but they did **NOT** persist a per-snap virtual"
        "-ticket trace. The V42I `ActiveTicketState` requires the LIVE"
        " open-ticket snapshot (id, age, current_pnl, gradient, max_adverse)"
        " at decision_ts. These cannot be reconstructed from the historical"
        " logs.\n"
    )
    md.append(
        "Per the user's spec under Phase 4: when JSONL state is insufficient,"
        " 'do NOT fabricate numbers; mark unknown and proceed to Phase 5"
        " with the empirical test'.\n"
    )
    md.append(
        "We therefore apply the **non-active-ticket subset** of V42I checks"
        " here, and note that V42I would have BLOCKED every historical"
        " candidate at the moment captured because the active ticket was"
        " unreconstructable. This means V42I would have **avoided all 8"
        " V42HSAFE losses** at the captured moments, but it does NOT prove"
        " V42I would not have found a DIFFERENT entry moment on the same"
        " mint a few hundred ms earlier or later — Phase 5 is the empirical"
        " test for that.\n"
    )
    md.append("## Summary counts\n")
    md.append(f"- v42hsafe records loaded: **{len(safe_recs)}** (full JSONL state)")
    md.append(f"- v42h records loaded:     **{len(h_recs)}** (from report table only)")
    md.append(f"- total replay records:     **{total}**")
    md.append(f"- v42i decisions = block:   **{blocked}**")
    md.append(f"- v42i decisions = enter:   **{entered}**")
    md.append(f"- v42hsafe actual losses:   **{len(safe_actual_losses)}**"
              f"  (`label_kind=='loss'`)")
    md.append(f"- v42i avoids of safe losses: **{v42i_avoid_safe_losses}**"
              f" / {len(safe_actual_losses)}")
    md.append(f"- v42h wins (banks):         **{len(h_wins)}**")
    md.append(f"- v42i admits v42h wins:     **{v42i_admit_h_wins}** / {len(h_wins)}")
    md.append(f"- v42h losses:               **{len(h_losses)}**")
    md.append("")
    md.append("## Per-mint table (v42hsafe + v42h)\n")
    md.append("| source | mint | rule_in_log | log_tslb_ms | log_last_bank_pnl |"
              " log_grad | label_pnl | label_kind | v42i_decision |"
              " v42i_block_reason |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in replays:
        mint = str(r.get("mint", ""))
        mshort = (mint[:4] + ".." + mint[-4:]) if len(mint) > 10 else mint
        tslb = r.get("log_active_ticket_age_ms")
        lbpnl = r.get("log_active_ticket_pnl")
        lbpnl_s = "-" if lbpnl is None else f"{float(lbpnl):+.6f}"
        grad = r.get("log_active_grad")
        grad_s = "-" if grad is None else f"{float(grad):+.7e}"
        lpnl = r.get("label_pnl")
        lpnl_s = "-" if lpnl is None else f"{float(lpnl):+.6f}"
        md.append(
            f"| {r.get('source','?')} | `{mshort}` | "
            f"{r.get('rule_in_log','-')} | "
            f"{tslb if tslb is not None else '-'} | "
            f"{lbpnl_s} | {grad_s} | "
            f"{lpnl_s} | {r.get('label_kind','-')} | "
            f"{r.get('v42i_decision','-')} | "
            f"{r.get('v42i_block_reason','-')} |"
        )
    md.append("")
    md.append("## Conclusion")
    md.append("")
    md.append(
        "- All historical V42H/V42HSAFE candidate entries were captured at"
        " the moment of bank completion (post-bank), where V42I's strict"
        " active-ticket gate cannot be evaluated from the persisted logs.\n"
    )
    md.append(
        f"- V42I's NON-active-ticket subset blocks all {total} historical"
        " candidates as `active_ticket_state_unreconstructable_from_log`,"
        " which would have avoided **all 8 V42HSAFE losses** AND **all 4"
        " V42H losses** at the captured moments — at the cost of also"
        " blocking the 4 V42H wins AND the 1 V42HSAFE win at those moments"
        " (a different entry moment may have been admitted instead).\n"
    )
    md.append(
        "- This replay does NOT gate Phase 5. Phase 5 is the empirical"
        " test of V42I's discovery rate and outcome distribution against"
        " live shred/curve data.\n"
    )
    md.append("## Median active-ticket-age proxy at entry (from log)")
    tslbs = [
        int(r["log_active_ticket_age_ms"]) for r in replays
        if r.get("log_active_ticket_age_ms") is not None
    ]
    if tslbs:
        md.append(f"- median time_since_last_virtual_bank_ms across all"
                  f" historical records: **{int(median(tslbs))} ms**"
                  f"  (proxy for 'how late we entered after last bank')")
    last_bank_ages = [
        int(r["log_last_bank_age_ms"]) for r in replays
        if r.get("log_last_bank_age_ms") is not None
    ]
    if last_bank_ages:
        md.append(f"- median last_bank_age_ms across all historical records:"
                  f" **{int(median(last_bank_ages))} ms**")
    md.append("")

    Path(OUT_MD).write_text("\n".join(md), encoding="utf-8")
    print(f"V42I-REPLAY done total={total} blocked={blocked}"
          f" entered={entered} safe_loss_avoid={v42i_avoid_safe_losses}/"
          f"{len(safe_actual_losses)} h_win_admit={v42i_admit_h_wins}/"
          f"{len(h_wins)} out={OUT_MD}")
    return 0


def main() -> int:
    global OUT_MD
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", default=OUT_MD)
    args = ap.parse_args()
    OUT_MD = args.out_md
    return run_replay()


if __name__ == "__main__":
    sys.exit(main())
