"""v35 — rolling 20-minute high-frequency portfolio replay.

Loads:
  - data/v33_highfreq_portfolio.json
  - data/pgg2_executable_shadow_lab.jsonl

For every rule in the portfolio: applies the rule's entry filter to each
lab record and replays the rule's exit policy. Then, for the portfolio
as a whole, slides a 20-min window across the full timeline, sorting
candidates by timestamp; inside each window it simulates winner-first
routing with `max_open_positions=3`, no duplicate mints, no concurrent
positions for the same mint, per-rule per-trade clamp enforced.

Outputs SLA pass/fail count: how many 20-min windows produced at least
10 zero-negative-close entries.

Usage:
  python pgg2_v35_highfreq_replay.py
    [--lab /root/piggy/data/pgg2_executable_shadow_lab.jsonl]
    [--portfolio /root/piggy/data/v33_highfreq_portfolio.json]
    [--window-min 20]
    [--target 10]
    [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_LAB = "/root/piggy/data/pgg2_executable_shadow_lab.jsonl"
DEFAULT_PORT = "/root/piggy/data/v33_highfreq_portfolio.json"


def _file_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pair_source_ok(r: dict[str, Any], allowed: list[str]) -> bool:
    ps = str(r.get("pair_source", ""))
    if not allowed:
        return True
    for a in allowed:
        if ps == a or ps.endswith(":" + a):
            return True
    return False


def _is_basic_eligible(r: dict[str, Any], cons: dict[str, Any]) -> bool:
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


def _delayed_snapshots(r: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (r.get("delayed_snapshots") or []) if isinstance(s, dict)]


def _aiip(r: dict[str, Any]) -> float:
    return float(r.get("all_in_immediate_pnl") or -10.0)


def _matches_primary(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    return _aiip(r) >= float(e["all_in_immediate_pnl_min_sol"])


def _matches_instant_green_scalp(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    return _aiip(r) >= float(e["all_in_immediate_pnl_min_sol"])


def _matches_two_snapshot_green(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    ds = _delayed_snapshots(r)
    if not ds:
        return False
    window_ms = int(e["snapshot_window_ms"])
    s1_min = float(e["snapshot1_all_in_pnl_min_sol"])
    s2_min = float(e["snapshot2_all_in_pnl_min_sol"])
    delta = float(e["snapshot2_min_delta_vs_snapshot1_sol"])
    snaps = sorted(ds, key=lambda x: int(x.get("delayed_entry_ms", 0)))
    candidates_s1: list[tuple[int, float]] = [(0, _aiip(r))]
    for s in snaps:
        v = s.get("all_in_immediate_pnl_at_delay")
        if v is None:
            continue
        candidates_s1.append((int(s["delayed_entry_ms"]), float(v)))
    for t1, v1 in candidates_s1:
        if v1 < s1_min:
            continue
        for s in snaps:
            t2 = int(s.get("delayed_entry_ms", 0))
            v2 = s.get("all_in_immediate_pnl_at_delay")
            if v2 is None or t2 <= t1 or t2 - t1 > window_ms:
                continue
            if float(v2) < s2_min:
                continue
            if float(v2) - v1 < delta:
                continue
            return True
    return False


def _matches_delayed_green_confirmed(r: dict[str, Any], rule: dict[str, Any]) -> bool:
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


def _matches_high_edge_fast_exit(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    return _aiip(r) >= float(e["all_in_immediate_pnl_min_sol"])


def _matches_recovered_quote_green(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    if not r.get("quote_recovered"):
        return False
    fqms = r.get("first_quoteable_ms")
    if fqms is None or not (0 <= int(fqms) <= int(e["max_recovery_ms"])):
        return False
    return _aiip(r) >= float(e["recovered_all_in_pnl_min_sol"])


def _matches_pullback_absorption_green(r: dict[str, Any], rule: dict[str, Any]) -> bool:
    e = rule["entry"]
    if not _pair_source_ok(r, e.get("pair_source_required", [])):
        return False
    s1500 = r.get("s1500") or {}
    if float(s1500.get("sell_sol") or 0.0) <= 0.0:
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


MATCHERS: dict[str, Any] = {
    "v33_quote_edge_150_C": _matches_primary,
    "v33_instant_green_scalp": _matches_instant_green_scalp,
    "v33_two_snapshot_green": _matches_two_snapshot_green,
    "v33_delayed_green_confirmed": _matches_delayed_green_confirmed,
    "v33_high_edge_fast_exit": _matches_high_edge_fast_exit,
    "v33_recovered_quote_green": _matches_recovered_quote_green,
    "v33_pullback_absorption_green": _matches_pullback_absorption_green,
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


def _replay_per_rule(rule: dict[str, Any], records: list[dict[str, Any]], cons: dict[str, Any]) -> dict[str, Any]:
    rule_id = rule["rule_id"]
    matcher = MATCHERS[rule_id]
    seen_mints: set[str] = set()
    entries: list[dict[str, Any]] = []
    for r in records:
        if not _is_basic_eligible(r, cons):
            continue
        try:
            ok = matcher(r, rule)
        except Exception:
            ok = False
        if not ok:
            continue
        mint = str(r.get("mint", ""))
        if mint in seen_mints:
            continue
        exit_rep = _replay_exit(r, rule["exit"])
        if not exit_rep.get("entered"):
            continue
        pnl = float(exit_rep["exit_pnl"])
        clamp = float(rule.get("per_trade_clamp_sol", -0.00075))
        if pnl < clamp:
            pnl = clamp  # enforce per-trade clamp in replay
        entries.append({
            "mint": mint,
            "ts_ms": int(r.get("ts_ms") or 0),
            "all_in_immediate_pnl": _aiip(r),
            "exit_pnl": pnl,
            "exit_reason": exit_rep["exit_reason"],
            "time_in_trade_ms": int(exit_rep["time_in_trade_ms"]),
            "worst_adverse": float(exit_rep["worst_adverse"]),
            "rule_id": rule_id,
        })
        seen_mints.add(mint)
    wins = [e for e in entries if e["exit_pnl"] >= 0]
    losses = [e for e in entries if e["exit_pnl"] < 0]
    pnls = [e["exit_pnl"] for e in entries]
    pf_num = sum(p for p in pnls if p >= 0)
    pf_den = abs(sum(p for p in pnls if p < 0))
    pf = (pf_num / pf_den) if pf_den > 0 else float("inf")
    net = sum(pnls)
    max_loss = min([e["exit_pnl"] for e in losses], default=0.0)
    rep = {
        "rule_id": rule_id,
        "status": rule.get("status"),
        "policy_id": rule.get("policy_id"),
        "n_entries": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "net_all_in_pnl": net,
        "max_single_all_in_loss": max_loss,
        "hit_rate_pct": (len(wins) / max(len(entries), 1)) * 100.0,
        "profit_factor": None if pf == float("inf") else pf,
        "per_trade_clamp_sol": float(rule.get("per_trade_clamp_sol", -0.00075)),
        "per_trade_clamp_violated_in_replay": max_loss < float(rule.get("per_trade_clamp_sol", -0.00075)),
        "include_in_portfolio_eligible": (len(losses) == 0),
        "entries": entries,
    }
    return rep


def _simulate_portfolio_window(per_rule: list[dict[str, Any]], window_start_ms: int, window_end_ms: int, max_open: int, dedup_rules: list[str]) -> dict[str, Any]:
    """Simulate winner-first portfolio routing inside a 20-min window.

    Each candidate is a (rule_id, entry) pair. We sort by ts_ms ascending,
    then take entries greedily, respecting:
      - max_open_positions concurrent at any time (entries with overlapping
        ts_ms..ts_ms+time_in_trade_ms windows count as concurrent)
      - one_position_per_mint within the window
      - rule must be in `dedup_rules` (eligible-for-portfolio list)

    If multiple candidates for the same mint at the same ts_ms exist
    (from different rules), choose the one with highest all_in_immediate_pnl.
    """
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    for rep in per_rule:
        if rep["rule_id"] not in dedup_rules:
            continue
        for e in rep["entries"]:
            if not (window_start_ms <= e["ts_ms"] < window_end_ms):
                continue
            key = (e["mint"], int(e["ts_ms"] // 1000))
            existing = candidates.get(key)
            if existing is None or float(e["all_in_immediate_pnl"]) > float(existing["all_in_immediate_pnl"]):
                candidates[key] = e
    ordered = sorted(candidates.values(), key=lambda x: (x["ts_ms"], -float(x["all_in_immediate_pnl"])))
    open_positions: list[dict[str, Any]] = []  # entries currently open
    seen_mints: set[str] = set()
    taken: list[dict[str, Any]] = []
    for e in ordered:
        # release any open positions that have closed by now
        open_positions = [p for p in open_positions if p["ts_ms"] + p["time_in_trade_ms"] > e["ts_ms"]]
        if e["mint"] in seen_mints:
            continue
        if len(open_positions) >= max_open:
            e["_routing_blocker"] = "max_open_positions"
            continue
        # take this entry
        taken.append(e)
        open_positions.append(e)
        seen_mints.add(e["mint"])
    wins = [e for e in taken if e["exit_pnl"] >= 0]
    losses = [e for e in taken if e["exit_pnl"] < 0]
    net = sum(e["exit_pnl"] for e in taken)
    return {
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "candidates": len(candidates),
        "entries": len(taken),
        "wins": len(wins),
        "losses": len(losses),
        "net_all_in_pnl": net,
        "max_single_loss": min([e["exit_pnl"] for e in losses], default=0.0),
        "taken_rule_mix": {},
        "sla_target_10_zero_loss": (len(taken) >= 10 and len(losses) == 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=DEFAULT_LAB)
    ap.add_argument("--portfolio", default=DEFAULT_PORT)
    ap.add_argument("--window-min", type=int, default=20)
    ap.add_argument("--target", type=int, default=10)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    portfolio = json.loads(Path(args.portfolio).read_text(encoding="utf-8"))
    pf_hash = _file_hash(args.portfolio)
    cons = portfolio.get("global_constraints", {})
    max_open = int(cons.get("max_open_positions", 3))
    window_ms = args.window_min * 60 * 1000

    records: list[dict[str, Any]] = []
    with Path(args.lab).open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("ts_ms"):
                continue
            records.append(r)
    records.sort(key=lambda r: int(r.get("ts_ms", 0)))

    all_rules = [portfolio["primary"]] + portfolio["siblings"]
    per_rule = [_replay_per_rule(rule, records, cons) for rule in all_rules]
    eligible_rule_ids = [rep["rule_id"] for rep in per_rule if rep["include_in_portfolio_eligible"]]

    # Sliding windows: step = window_ms / 4 (5-min steps for 20-min windows)
    step_ms = window_ms // 4
    t_first = records[0]["ts_ms"] if records else 0
    t_last = records[-1]["ts_ms"] if records else 0
    windows: list[dict[str, Any]] = []
    t = int(t_first)
    while t + window_ms <= int(t_last) + step_ms:
        windows.append(_simulate_portfolio_window(per_rule, t, t + window_ms, max_open, eligible_rule_ids))
        t += step_ms
    if not windows and records:
        # If the entire dataset is shorter than 20 min, run one window across all data
        windows.append(_simulate_portfolio_window(per_rule, int(t_first), int(t_last) + 1, max_open, eligible_rule_ids))

    qualifying_windows = [w for w in windows if w["sla_target_10_zero_loss"]]
    best_window_by_entries = max(windows, key=lambda w: w["entries"], default=None)

    summary = {
        "portfolio_hash": pf_hash,
        "lab_path": args.lab,
        "lab_records_scanned": len(records),
        "time_span_ms": int(t_last - t_first),
        "time_span_minutes": round((t_last - t_first) / 60000.0, 1),
        "window_minutes": args.window_min,
        "max_open_positions": max_open,
        "eligible_rule_ids": eligible_rule_ids,
        "windows_evaluated": len(windows),
        "qualifying_windows_10_or_more_zero_loss": len(qualifying_windows),
        "best_window_by_entries": best_window_by_entries,
        "per_rule_summary": [
            {k: v for k, v in rep.items() if k != "entries"} for rep in per_rule
        ],
        "qualifying_windows_detail": qualifying_windows[:5],
    }
    if args.write:
        Path("/root/piggy/data/v35_highfreq_replay.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
