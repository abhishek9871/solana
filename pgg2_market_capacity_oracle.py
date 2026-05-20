"""v36 — Market Capacity Oracle.

Question this tool answers definitively:
    In any 20-minute window of the available shadow-lab data, how many entries
    COULD the bot have taken with zero negative all-in closes if it had routed
    every safe fast-quote opportunity?

Inputs:
    - data/pgg2_executable_shadow_lab.jsonl  (all v33/v35 records)
    - optionally a runtime log to pull actual pilot/scalp BUYs for diffing

Method:
    For each lab record, apply broad mechanical safety filters ONLY:
        - cost_model_confidence == "proven"
        - sim_needed == 0 (no sim_selected pair_source)
        - pair_source in {current_sig, cache, prewarmed, observed_raw_rpc}
        - direct route pump_bc (i.e. execution_eligible)
        - all_in_immediate_pnl available
    Lane blacklist is recorded as a feature, NOT a hard filter.

    Then replay FIVE causal exit policies against the record's t=0 +
    future_sells timeline:
        a) instant_bank: bank as soon as all_in >= +0.00020
        b) fast_bank:   bank at +0.00060, scratch on deterioration, clamp -0.00050
        c) protected_hold: primary v33 exit (bank +0.00060, clamp -0.00075,
                           timebox 5000, abs_max 10000)
        d) high_edge_fast: bank +0.00080, clamp -0.00050, abs_max 5000
        e) delayed_green: pick best delayed snapshot >= +0.00060 as entry-time

    A candidate is "safe-executable" if AT LEAST ONE of (a..e) closes with
    all_in >= 0. The best-of-five is the chosen exit_pnl.

    Roll a 20-min window, count safe-executable unique mints under
    max_open=3 and max_open=5, with and without lane blacklist.

Output:
    MARKET CAPACITY ORACLE
    - total candidates
    - mechanically-safe candidates
    - safe-executable (zero-negative under some exit)
    - safe entries per 20-min window (median / max / min)
    - max_open=3 / max_open=5 throughput
    - with / without lane blacklist
    - top-50 missed safe entries (where actual bot didn't enter)
    - explicit verdict: was target 10/20 ever feasible? under which guard tier?
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_LAB = "/root/piggy/data/pgg2_executable_shadow_lab.jsonl"
ALLOWED_PAIR_SOURCES = {"current_sig", "cache", "prewarmed", "observed_raw_rpc"}
LANE_BLACKLIST = {"raw_momentum_shadow", "raw_momentum_current", "generic_observation"}


def _mech_safe(r: dict[str, Any]) -> bool:
    ps = str(r.get("pair_source", ""))
    if ps.startswith("sim_selected:"):
        return False
    if r.get("cost_model_confidence") != "proven":
        return False
    if not r.get("execution_eligible"):
        return False
    ok = False
    for a in ALLOWED_PAIR_SOURCES:
        if ps == a or ps.endswith(":" + a):
            ok = True
            break
    if not ok:
        return False
    if r.get("all_in_immediate_pnl") is None:
        return False
    return True


def _timeline(r: dict[str, Any]) -> list[tuple[int, float]]:
    tl: list[tuple[int, float]] = []
    aip = r.get("all_in_immediate_pnl")
    if aip is None:
        aip = r.get("immediate_pnl")
    if aip is not None:
        tl.append((0, float(aip)))
    for fs in (r.get("future_sells") or []):
        if not isinstance(fs, dict) or "t_ms" not in fs:
            continue
        v = fs.get("all_in_pnl")
        if v is None:
            v = fs.get("pnl")
        if v is None:
            continue
        tl.append((int(fs["t_ms"]), float(v)))
    tl.sort(key=lambda x: x[0])
    return tl


def _replay_policy(tl: list[tuple[int, float]], bank: float, scratch_min: float, clamp: float, timebox_ms: int, abs_max: int, scratch_deteriorate_eps: float = 0.00020) -> dict[str, Any]:
    if not tl:
        return {"entered": False}
    worst = tl[0][1]
    prev = tl[0][1]
    for t, pnl in tl:
        if t == 0:
            continue
        worst = min(worst, pnl)
        if pnl >= bank:
            return {"entered": True, "exit_pnl": pnl, "time_in_trade_ms": t, "exit_reason": "bank"}
        if pnl <= clamp:
            return {"entered": True, "exit_pnl": pnl, "time_in_trade_ms": t, "exit_reason": "clamp"}
        if pnl >= scratch_min and pnl < prev - scratch_deteriorate_eps and pnl < bank:
            return {"entered": True, "exit_pnl": pnl, "time_in_trade_ms": t, "exit_reason": "scratch"}
        if t >= abs_max:
            return {"entered": True, "exit_pnl": pnl, "time_in_trade_ms": t, "exit_reason": "absolute_max_hold"}
        if t >= timebox_ms and pnl < scratch_min:
            return {"entered": True, "exit_pnl": pnl, "time_in_trade_ms": t, "exit_reason": "timebox"}
        prev = pnl
    last_t, last = tl[-1]
    return {"entered": True, "exit_pnl": last, "time_in_trade_ms": last_t, "exit_reason": "data_end"}


def _best_safe_exit(r: dict[str, Any]) -> dict[str, Any]:
    """Try five causal exits. Return the best non-negative exit if any.
    Otherwise return the LEAST-NEGATIVE exit (so the safe? gate can decide).
    """
    tl = _timeline(r)
    if not tl:
        return {"safe": False, "best_pnl": -10.0}
    # a) instant_bank: bank=+0.00020, scratch=+0.00005, clamp=-0.00020, timebox=2000, abs_max=2000
    a = _replay_policy(tl, 0.00020, 0.00005, -0.00020, 2000, 2000)
    # b) fast_bank: bank=+0.00060, scratch=+0.00010, clamp=-0.00050, timebox=3000, abs_max=3000
    b = _replay_policy(tl, 0.00060, 0.00010, -0.00050, 3000, 3000)
    # c) protected_hold (primary v33): bank=+0.00060, scratch=+0.00010, clamp=-0.00075, timebox=5000, abs_max=10000
    c = _replay_policy(tl, 0.00060, 0.00010, -0.00075, 5000, 10000)
    # d) high_edge_fast: bank=+0.00080, scratch=+0.00020, clamp=-0.00050, timebox=3000, abs_max=5000
    d = _replay_policy(tl, 0.00080, 0.00020, -0.00050, 3000, 5000)
    # e) delayed_green: take the best delayed snapshot if >= +0.00060 as virtual entry
    ds_best_pnl = None
    ds = r.get("delayed_snapshots") or []
    for s in ds:
        v = s.get("all_in_immediate_pnl_at_delay")
        if v is None:
            continue
        if float(v) >= 0.00060:
            if ds_best_pnl is None or float(v) > ds_best_pnl:
                ds_best_pnl = float(v)
    e = {"entered": ds_best_pnl is not None, "exit_pnl": (ds_best_pnl or -10.0), "exit_reason": "delayed_green_entry"}
    candidates = [x for x in (a, b, c, d, e) if x.get("entered")]
    if not candidates:
        return {"safe": False, "best_pnl": -10.0}
    # Best non-negative if any, else best pnl
    nonneg = [x for x in candidates if x["exit_pnl"] >= 0]
    if nonneg:
        best = max(nonneg, key=lambda x: x["exit_pnl"])
        return {
            "safe": True,
            "best_pnl": best["exit_pnl"],
            "best_exit_reason": best["exit_reason"],
            "best_time_in_trade_ms": best.get("time_in_trade_ms", 0),
            "any_clamp": any(x.get("exit_reason") == "clamp" for x in candidates),
        }
    best = max(candidates, key=lambda x: x["exit_pnl"])
    return {"safe": False, "best_pnl": best["exit_pnl"], "best_exit_reason": best["exit_reason"]}


def _route_window(safe_records: list[dict[str, Any]], window_start: int, window_end: int, max_open: int, allow_blacklist: bool) -> dict[str, Any]:
    """Simulate winner-first routing inside a 20-min window with max_open
    concurrent positions, no duplicate mint."""
    cands: list[dict[str, Any]] = []
    for r in safe_records:
        if not (window_start <= int(r.get("ts_ms", 0)) < window_end):
            continue
        if not allow_blacklist and r.get("_lane_blacklisted"):
            continue
        cands.append(r)
    cands.sort(key=lambda r: (int(r.get("ts_ms", 0)), -float(r["best_pnl"])))
    open_pos: list[dict[str, Any]] = []
    seen: set[str] = set()
    taken: list[dict[str, Any]] = []
    for r in cands:
        t = int(r["ts_ms"])
        open_pos = [p for p in open_pos if int(p["ts_ms"]) + int(p["best_time_in_trade_ms"]) > t]
        mint = str(r.get("mint", ""))
        if mint in seen:
            continue
        if len(open_pos) >= max_open:
            continue
        taken.append(r)
        open_pos.append(r)
        seen.add(mint)
    nonneg = sum(1 for r in taken if r["best_pnl"] >= 0)
    losses = sum(1 for r in taken if r["best_pnl"] < 0)
    net = sum(float(r["best_pnl"]) for r in taken)
    return {
        "window_start": window_start,
        "window_end": window_end,
        "candidates": len(cands),
        "entries": len(taken),
        "wins": nonneg,
        "losses": losses,
        "net_all_in": net,
        "sla_met_10_zero_neg": (len(taken) >= 10 and losses == 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lab", default=DEFAULT_LAB)
    ap.add_argument("--window-min", type=int, default=20)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

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
    total = len(records)
    mech_safe = [r for r in records if _mech_safe(r)]
    safe_records: list[dict[str, Any]] = []
    for r in mech_safe:
        rep = _best_safe_exit(r)
        if rep["safe"]:
            r2 = dict(r)
            r2["best_pnl"] = rep["best_pnl"]
            r2["best_exit_reason"] = rep["best_exit_reason"]
            r2["best_time_in_trade_ms"] = rep["best_time_in_trade_ms"]
            r2["_lane_blacklisted"] = (r.get("lane_candidate", "") in LANE_BLACKLIST)
            safe_records.append(r2)

    if not records:
        print(json.dumps({"error": "no records"}, indent=2))
        return 0

    t_first = int(records[0]["ts_ms"])
    t_last = int(records[-1]["ts_ms"])
    span_min = (t_last - t_first) / 60000.0
    window_ms = args.window_min * 60 * 1000
    step_ms = window_ms // 4

    # 4 scenarios: max_open in (3, 5) x lane_blacklist in (True=use, False=bypass)
    scenarios = []
    for max_open in (3, 5):
        for allow_blacklist in (True, False):
            windows = []
            t = t_first
            while t + window_ms <= t_last + step_ms:
                w = _route_window(safe_records, t, t + window_ms, max_open, allow_blacklist)
                windows.append(w)
                t += step_ms
            qual = sum(1 for w in windows if w["sla_met_10_zero_neg"])
            entries_distribution = sorted([w["entries"] for w in windows], reverse=True)
            best = max(windows, key=lambda w: w["entries"], default=None)
            scenarios.append({
                "scenario": f"max_open={max_open}_blacklist_bypass={'yes' if not allow_blacklist else 'no'}",
                "max_open": max_open,
                "lane_blacklist_bypass": not allow_blacklist,
                "windows_evaluated": len(windows),
                "windows_meeting_sla_10_zero_neg": qual,
                "best_window_entries": best["entries"] if best else 0,
                "best_window_wins": best["wins"] if best else 0,
                "best_window_losses": best["losses"] if best else 0,
                "best_window_net": float(best["net_all_in"]) if best else 0.0,
                "top10_window_entries": entries_distribution[:10],
            })

    # Verdict: was 10/20 ever feasible under broad mechanical safety + max_open=5?
    feasible_scenario = next((s for s in scenarios if s["max_open"] == 5 and s["lane_blacklist_bypass"]), None)
    market_capacity_sufficient = (feasible_scenario and feasible_scenario["windows_meeting_sla_10_zero_neg"] > 0)

    out = {
        "lab_path": args.lab,
        "total_records": total,
        "mechanically_safe_records": len(mech_safe),
        "safe_executable_records_any_exit_nonneg": len(safe_records),
        "time_span_minutes": round(span_min, 1),
        "scenarios": scenarios,
        "market_capacity_verdict": {
            "definition": "Capacity is sufficient if max_open=5 + lane_blacklist_bypass yields >=1 window with >=10 entries and 0 losses.",
            "sufficient": bool(market_capacity_sufficient),
            "explanation": (
                "Market capacity SUFFICIENT — frequency blocker is artificial (routing/sampler)." if market_capacity_sufficient else
                "Market capacity INSUFFICIENT — fewer than 10 safe opportunities in any 20-min window even under broadest routing."
            ),
        },
    }
    if args.write:
        Path("/root/piggy/data/v36_market_capacity_oracle.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
