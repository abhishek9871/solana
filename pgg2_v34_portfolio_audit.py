"""v34 — portfolio holdout + frequency / missed-winner audit.

Reads:
  - data/v33_preregistered_portfolio.json  (frozen rule set)
  - data/pgg2_executable_shadow_lab.jsonl  (shadow lab records)
  - optionally a runtime log path for log-derived counts

Emits to stdout:
  - per-rule funnel: total observed, qualifying, gates evaluated (PASS/FAIL)
  - frequency KPI: actual entries/hour (from log), shadow-qualified entries/hour
  - missed winners: candidates with all_in >= +0.00150 NOT entered as pilot,
    with blocker reason + which sibling rule would have caught them.

Writes (when --write):
  - data/v34_portfolio_audit.json  (machine-readable)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_LAB = "/root/piggy/data/pgg2_executable_shadow_lab.jsonl"
DEFAULT_PORT = "/root/piggy/data/v33_preregistered_portfolio.json"


def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _file_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _is_basic_eligible(r: dict[str, Any], cons: dict[str, Any]) -> bool:
    """Common eligibility checks applied to every rule before per-rule matching."""
    ps = str(r.get("pair_source", ""))
    if ps.startswith("sim_selected:"):
        return False
    if r.get("cost_model_confidence") != "proven":
        return False
    if not r.get("execution_eligible"):
        return False
    lane = r.get("lane_candidate", "")
    if lane in set(cons.get("blacklisted_lane_candidates", [])):
        return False
    return True


def _pair_source_ok(r: dict[str, Any], allowed: list[str]) -> bool:
    ps = str(r.get("pair_source", ""))
    if not allowed:
        return True
    for a in allowed:
        if ps == a or ps.endswith(":" + a):
            return True
    return False


def _delayed_snapshots(r: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (r.get("delayed_snapshots") or []) if isinstance(s, dict)]


def _matches_primary(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    aip = float(r.get("all_in_immediate_pnl") or -10.0)
    return aip >= float(e.get("all_in_immediate_pnl_min_sol", 0.00150))


def _matches_120_two_snapshot(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    ds = _delayed_snapshots(r)
    if not ds:
        return False
    window_ms = int(e.get("snapshot_window_ms", 1000))
    s1_min = float(e["snapshot1_all_in_pnl_min_sol"])
    s2_min = float(e["snapshot2_all_in_pnl_min_sol"])
    delta = float(e["snapshot2_min_delta_vs_snapshot1_sol"])
    # Sorted snapshots
    snaps = sorted(ds, key=lambda x: int(x.get("delayed_entry_ms", 0)))
    aip = float(r.get("all_in_immediate_pnl") or -10.0)
    # First snapshot can be t=0 (all_in_immediate_pnl) or a delayed snapshot.
    # We accept either as snapshot1, then look for snapshot2 within window_ms.
    candidates_s1: list[tuple[int, float]] = [(0, aip)]
    for s in snaps:
        v = s.get("all_in_immediate_pnl_at_delay")
        if v is not None:
            candidates_s1.append((int(s["delayed_entry_ms"]), float(v)))
    for t1, v1 in candidates_s1:
        if v1 < s1_min:
            continue
        for s in snaps:
            t2 = int(s.get("delayed_entry_ms", 0))
            v2 = s.get("all_in_immediate_pnl_at_delay")
            if v2 is None:
                continue
            if t2 <= t1:
                continue
            if t2 - t1 > window_ms:
                continue
            if float(v2) < s2_min:
                continue
            if float(v2) - v1 < delta:
                continue
            return True
    return False


def _matches_150_fast_bank_a(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    return _matches_primary(r, rule)  # entry criterion is the same; policy differs


def _matches_delayed_allin_green(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    ds = _delayed_snapshots(r)
    min_n = int(e.get("min_delayed_entries", 2))
    sep_ms = int(e.get("min_snapshot_separation_ms", 250))
    thresh = float(e.get("min_snapshot_all_in_pnl_sol", 0.00060))
    valid = sorted(
        [s for s in ds if s.get("all_in_immediate_pnl_at_delay") is not None and not s.get("error")],
        key=lambda x: int(x.get("delayed_entry_ms", 0)),
    )
    if len(valid) < min_n:
        return False
    chosen: list[dict[str, Any]] = []
    last_t = -10_000
    for s in valid:
        if float(s["all_in_immediate_pnl_at_delay"]) >= thresh and int(s["delayed_entry_ms"]) - last_t >= sep_ms:
            chosen.append(s)
            last_t = int(s["delayed_entry_ms"])
            if len(chosen) >= min_n:
                return True
    return False


def _matches_pullback_absorption(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    # Heuristic: prior sell pressure proxy = r.get("s1500", {}).get("sell_sol") > 0
    s1500 = r.get("s1500") or {}
    sell1500 = float(s1500.get("sell_sol") or 0.0)
    if sell1500 <= 0.0:
        return False
    ds = _delayed_snapshots(r)
    best = None
    for s in ds:
        v = s.get("all_in_immediate_pnl_at_delay")
        if v is None:
            continue
        if best is None or float(v) > best:
            best = float(v)
    if best is None or best < float(e["min_recovered_all_in_pnl_sol"]):
        return False
    if float(r.get("slot_top_share") or 1.0) > float(e["max_slot_top_share"]):
        return False
    if int(r.get("slot_buyers") or 0) < int(e["min_slot_unique_buyers"]):
        return False
    return True


def _matches_high_edge_fast_exit(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    aip = float(r.get("all_in_immediate_pnl") or -10.0)
    return aip >= float(e["all_in_immediate_pnl_min_sol"])


def _matches_recovered_quote(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    if not r.get("quote_recovered"):
        return False
    first_q_ms = r.get("first_quoteable_ms")
    if first_q_ms is None:
        return False
    max_rec = int(e.get("max_recovery_ms", 1000))
    if not (0 <= int(first_q_ms) <= max_rec):
        return False
    aip = float(r.get("all_in_immediate_pnl") or -10.0)
    return aip >= float(e["recovered_all_in_pnl_min_sol"])


MATCHERS: dict[str, Any] = {
    "v33_quote_edge_150_C": _matches_primary,
    "v33_quote_edge_120_two_snapshot_C": _matches_120_two_snapshot,
    "v33_quote_edge_150_fast_bank_A": _matches_150_fast_bank_a,
    "v33_delayed_allin_green_confirmed": _matches_delayed_allin_green,
    "v33_pullback_absorption_green": _matches_pullback_absorption,
    "v33_high_edge_fast_exit": _matches_high_edge_fast_exit,
    "v33_recovered_quote_green": _matches_recovered_quote,
}


def _replay_exit(r: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    bank = float(policy["bank_all_in_pnl_min_sol"])
    scratch_min = float(policy["scratch_exit_min_all_in_pnl_sol"])
    clamp = float(policy["clamp_all_in_pnl_max_sol"])
    timebox_ms = int(policy["timebox_ms"])
    abs_max = int(policy["absolute_max_hold_ms"])
    timeline: list[tuple[int, float]] = []
    imm = r.get("all_in_immediate_pnl")
    if imm is None:
        imm = r.get("immediate_pnl")
    if imm is not None:
        timeline.append((0, float(imm)))
    for fs in (r.get("future_sells") or []):
        if not isinstance(fs, dict) or "t_ms" not in fs:
            continue
        v = fs.get("all_in_pnl")
        if v is None:
            v = fs.get("pnl")
        if v is None:
            continue
        timeline.append((int(fs["t_ms"]), float(v)))
    timeline.sort(key=lambda x: x[0])
    if not timeline:
        return {"entered": False}
    worst = timeline[0][1]
    prev_pnl = timeline[0][1]
    for t, pnl in timeline:
        if t == 0:
            continue
        worst = min(worst, pnl)
        if pnl >= bank:
            return {"entered": True, "exit_reason": "bank", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if pnl <= clamp:
            return {"entered": True, "exit_reason": "clamp", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if pnl >= scratch_min and pnl < prev_pnl - 0.00020 and pnl < bank:
            return {"entered": True, "exit_reason": "scratch", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if t >= abs_max:
            return {"entered": True, "exit_reason": "absolute_max_hold", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if t >= timebox_ms and pnl < scratch_min:
            return {"entered": True, "exit_reason": "timebox", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        prev_pnl = pnl
    last_t, last_pnl = timeline[-1]
    return {"entered": True, "exit_reason": "data_end", "exit_pnl": last_pnl, "time_in_trade_ms": last_t, "worst_adverse": worst}


def _gate_eval(rule: dict[str, Any], rep: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    n = rep["unique_mints"]
    wins = rep["wins"]
    losses = rep["losses"]
    net = rep["net_all_in_pnl"]
    hit = (wins / max(n, 1)) * 100.0
    max_loss = rep["max_single_all_in_loss"]
    top = rep["top_winner_share"]
    pf_num = sum(p for p in rep["pnls"] if p >= 0)
    pf_den = abs(sum(p for p in rep["pnls"] if p < 0))
    pf = (pf_num / pf_den) if pf_den > 0 else float("inf")
    gate_results = {
        "n_ge_10": n >= int(gates["fresh_holdout_qualifying_n_min"]),
        "net_gt_0": net > float(gates["fresh_holdout_net_all_in_pnl_min_sol"]),
        "hit_ge_min": hit >= float(gates["fresh_holdout_hit_rate_min_pct"]),
        "pf_ge_min": (pf == float("inf")) or pf >= float(gates["fresh_holdout_profit_factor_min"]),
        "max_loss_within_budget": max_loss >= float(gates["fresh_holdout_max_single_all_in_loss_max_sol"]),
        "losers_le_max": losses <= int(gates["fresh_holdout_max_losers"]),
        "top_concentration_le_max": top <= float(gates["fresh_holdout_top_winner_concentration_max"]),
    }
    gate_results["all_gates_passed"] = all(gate_results.values())
    rep["gate_results"] = gate_results
    rep["hit_rate_pct"] = hit
    rep["profit_factor"] = pf if pf != float("inf") else None
    return rep


def _audit_rule(rule: dict[str, Any], records: list[dict[str, Any]], gates: dict[str, Any], cons: dict[str, Any]) -> dict[str, Any]:
    rule_id = rule["rule_id"]
    matcher = MATCHERS.get(rule_id)
    rep: dict[str, Any] = {
        "rule_id": rule_id,
        "status": rule.get("status", "shadow_only"),
        "matched_records": 0,
        "qualifying_records": 0,
        "qualifying_unique_mints": [],
        "wins": 0,
        "losses": 0,
        "pnls": [],
        "net_all_in_pnl": 0.0,
        "max_single_all_in_loss": 0.0,
        "max_single_all_in_win": 0.0,
        "top_winner_share": 0.0,
        "unique_mints": 0,
        "exit_reasons": {},
    }
    if matcher is None:
        rep["error"] = "no matcher implemented"
        return rep
    seen_mints: set[str] = set()
    for r in records:
        if not _is_basic_eligible(r, cons):
            continue
        rep["matched_records"] += 1
        try:
            ok = matcher(r, rule)
        except Exception:
            ok = False
        if not ok:
            continue
        mint = str(r.get("mint", ""))
        if mint in seen_mints:
            continue
        seen_mints.add(mint)
        rep["qualifying_records"] += 1
        exit_rep = _replay_exit(r, rule["exit"])
        if not exit_rep.get("entered"):
            continue
        pnl = float(exit_rep["exit_pnl"])
        rep["pnls"].append(pnl)
        rep["net_all_in_pnl"] += pnl
        if pnl >= 0:
            rep["wins"] += 1
            rep["max_single_all_in_win"] = max(rep["max_single_all_in_win"], pnl)
        else:
            rep["losses"] += 1
            rep["max_single_all_in_loss"] = min(rep["max_single_all_in_loss"], pnl)
        rep["exit_reasons"][exit_rep["exit_reason"]] = rep["exit_reasons"].get(exit_rep["exit_reason"], 0) + 1
        rep["qualifying_unique_mints"].append(mint)
    rep["unique_mints"] = len(rep["qualifying_unique_mints"])
    if rep["pnls"]:
        max_win = max(rep["pnls"])
        rep["top_winner_share"] = (max(0.0, max_win) / max(abs(rep["net_all_in_pnl"]), 1e-12)) if rep["net_all_in_pnl"] > 0 else 0.0
    return _gate_eval(rule, rep, gates)


_RE_PILOT_BUY = re.compile(r"PGG2-DRYLIVE-PILOT-BUY rule_id=\S+ policy_id=\S+ pnl_model_version=\S+ mint=(\S+)")
_RE_SHADOW_LAB_REC = re.compile(r"SHADOW-LAB-REC (\S+) lane=(\S+) label=\S+ pnl_model=\S+ all_in_immediate_pnl=([+\-0-9.]+)")


def _runtime_counts(log_path: Optional[str]) -> dict[str, Any]:
    if not log_path:
        return {}
    p = Path(log_path)
    if not p.exists():
        return {}
    buy = 0
    shadow_recs = 0
    high_edge_shadow = 0
    pilot_buy_mints: list[str] = []
    log_mtime = int(p.stat().st_mtime)
    log_ctime = int(p.stat().st_ctime)
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "PGG2-DRYLIVE-PILOT-BUY" in line:
                m = _RE_PILOT_BUY.search(line)
                if m:
                    pilot_buy_mints.append(m.group(1))
                buy += 1
            if "SHADOW-LAB-REC" in line:
                m = _RE_SHADOW_LAB_REC.search(line)
                if m:
                    shadow_recs += 1
                    if float(m.group(3)) >= 0.00150:
                        high_edge_shadow += 1
    return {
        "log_path": str(p),
        "log_mtime_ts": log_mtime,
        "log_ctime_ts": log_ctime,
        "actual_pilot_buys": buy,
        "shadow_lab_recs": shadow_recs,
        "shadow_high_edge_records": high_edge_shadow,
        "actual_pilot_mints": pilot_buy_mints,
    }


def _missed_winners(records: list[dict[str, Any]], cons: dict[str, Any], top_n: int = 20, actual_mints: Optional[list[str]] = None, primary_rule: Optional[dict[str, Any]] = None, siblings: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    actual = set(actual_mints or [])
    # short_prefix lookup so we can match PILOT-BUY's short form against the JSONL's long form
    short_to_match = {m.split("..")[0]: m for m in actual}
    items: list[dict[str, Any]] = []
    for r in records:
        if not _is_basic_eligible(r, cons):
            continue
        aip = float(r.get("all_in_immediate_pnl") or -10.0)
        if aip < 0.00150:
            continue
        mint = str(r.get("mint", ""))
        if mint in actual or any(mint.startswith(p) for p in short_to_match):
            continue
        rec = {
            "mint": mint,
            "all_in_immediate_pnl": aip,
            "all_in_best_pnl_lookahead": r.get("all_in_best_pnl_lookahead"),
            "lane_candidate": r.get("lane_candidate"),
            "pair_source": r.get("pair_source"),
            "slot_top_share": r.get("slot_top_share"),
            "slot_buyers": r.get("slot_buyers"),
            "would_pass_primary": False,
            "siblings_that_match": [],
        }
        # would primary have caught it? (post-blacklist + slot gates apply at runtime, not in matcher)
        if primary_rule is not None and _matches_primary(r, primary_rule):
            rec["would_pass_primary"] = True
        # which siblings match?
        for s in (siblings or []):
            m = MATCHERS.get(s["rule_id"])
            if m and m(r, s):
                rec["siblings_that_match"].append(s["rule_id"])
        items.append(rec)
    items.sort(key=lambda x: float(x["all_in_immediate_pnl"]), reverse=True)
    return items[:top_n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=DEFAULT_LAB)
    ap.add_argument("--portfolio", default=DEFAULT_PORT)
    ap.add_argument("--log", default=None, help="optional bot log for actual-entry counts")
    ap.add_argument("--write", action="store_true", help="write data/v34_portfolio_audit.json")
    ap.add_argument("--last-n", type=int, default=0, help="limit lab records to last N")
    args = ap.parse_args()

    port = _load(args.portfolio)
    portfolio_hash = _file_hash(args.portfolio)
    cons = port.get("global_constraints", {})
    gates = port["qualification_gates_per_sibling_for_one_entry_pilot"]

    records: list[dict[str, Any]] = []
    with Path(args.lab).open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    if args.last_n > 0:
        records = records[-args.last_n:]

    rt = _runtime_counts(args.log)
    elapsed_sec = (time.time() - rt["log_ctime_ts"]) if rt.get("log_ctime_ts") else 0
    elapsed_hr = max(0.01, elapsed_sec / 3600.0)
    actual_rate = (rt.get("actual_pilot_buys", 0) / elapsed_hr) if elapsed_hr > 0.01 else 0.0

    all_rules = [port["primary"], *port["siblings"]]
    rule_reports = [_audit_rule(rule, records, gates, cons) for rule in all_rules]

    siblings = port["siblings"]
    primary_rule = port["primary"]
    miss = _missed_winners(records, cons, top_n=20, actual_mints=rt.get("actual_pilot_mints"), primary_rule=primary_rule, siblings=siblings)

    out = {
        "portfolio_hash": portfolio_hash,
        "lab_path": args.lab,
        "lab_records_scanned": len(records),
        "runtime_log": rt,
        "frequency_kpi": {
            "actual_pilot_entries_per_hour": round(actual_rate, 3),
            "elapsed_hours": round(elapsed_hr, 3),
            "shadow_lab_recs": rt.get("shadow_lab_recs"),
            "shadow_high_edge_records": rt.get("shadow_high_edge_records"),
        },
        "rule_reports": rule_reports,
        "missed_winners_top_20": miss,
    }
    if args.write:
        Path("/root/piggy/data/v34_portfolio_audit.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
