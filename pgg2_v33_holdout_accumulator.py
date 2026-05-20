"""
pgg2 v33 holdout accumulator

Reads the live shadow lab JSONL + bot log, computes whether the pre-registered
`v33_quote_edge_150_C` rule has passed its holdout gates, and writes the
result to `data/v33_holdout_accumulator.json`.

Designed to be called periodically by the run-to-gate driver.

Usage:
    py pgg2_v33_holdout_accumulator.py --lab data/pgg2_executable_shadow_lab.jsonl --log <log_path>
    py pgg2_v33_holdout_accumulator.py --print          # write + print summary
    py pgg2_v33_holdout_accumulator.py --gate           # write + emit pass/fail to stdout

Exit codes:
    0 — accumulator written
    2 — accumulator written AND all qualification gates passed (ready for pilot)
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


DEFAULT_LAB = Path("data/pgg2_executable_shadow_lab.jsonl")
PREREG_PATH = Path("data/v33_preregistered_rules.json")
OUTPUT_PATH = Path("data/v33_holdout_accumulator.json")


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _load_preregistered() -> Optional[dict]:
    try:
        with PREREG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _matches_primary_rule(record: dict, prereg: dict) -> bool:
    p = prereg.get("primary", {}).get("entry", {})
    src_families = set(prereg["primary"].get("source_families", []))
    blacklisted = set(prereg["primary"].get("blacklisted_families", []))
    lane = record.get("lane_candidate", "")
    if lane in blacklisted:
        return False
    if src_families and lane not in src_families:
        return False
    if record.get("cost_model_confidence") != p.get("cost_model_confidence_required", "proven"):
        return False
    pair_src = str(record.get("pair_source", ""))
    allowed_pair = set(p.get("pair_source_required", []))
    if allowed_pair and not any(pair_src == s or pair_src.endswith(":" + s) for s in allowed_pair):
        return False
    if pair_src.startswith("sim_selected:"):
        return False
    aip = float(record.get("all_in_immediate_pnl") or -1.0)
    if aip < float(p.get("all_in_immediate_pnl_min_sol", 0.0015)):
        return False
    if not record.get("execution_eligible"):
        return False
    return True


def _replay_v33_policy_C(record: dict, prereg: dict) -> dict:
    exit_p = prereg["primary"]["exit"]
    bank = float(exit_p["bank_all_in_pnl_min_sol"])
    scratch_min = float(exit_p["scratch_exit_min_all_in_pnl_sol"])
    clamp = float(exit_p["clamp_all_in_pnl_max_sol"])
    timebox_ms = int(exit_p["timebox_ms"])
    abs_max = int(exit_p["absolute_max_hold_ms"])
    timeline = []
    imm = record.get("all_in_immediate_pnl")
    if imm is None:
        imm = record.get("immediate_pnl")
    if imm is not None:
        timeline.append((0, float(imm)))
    for fs in record.get("future_sells") or []:
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


def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    i = int(len(s) * p / 100.0)
    if i >= len(s):
        i = len(s) - 1
    return s[i]


_RE_QUOTE_LATENCY = re.compile(
    r"PGG2-QUOTE-LATENCY side=(?P<side>\S+) mint=\S+ route=\S+ source=\S+ lane=\S* "
    r"start_ms=\d+ end_ms=\d+ latency_ms=(?P<lat>\d+) success=\d error_class=\S* "
    r"pair_source=(?P<pair>\S*) pair_prewarm=\d sim_needed=\d in_flight=\d+"
)


def parse_log_latency(path: Path) -> dict:
    """Return {(side, pair_source): [latency_ms,...]}"""
    out: dict[tuple[str, str], list[float]] = {}
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _RE_QUOTE_LATENCY.search(line)
                if m:
                    key = (m.group("side"), m.group("pair") or "unknown")
                    out.setdefault(key, []).append(float(m.group("lat")))
    except Exception:
        pass
    return out


def parse_stale_decisions(path: Path) -> int:
    if not path.exists():
        return 0
    cnt = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "PGG2-QUOTE-MGR-STALE-DISCARD" in line:
                    cnt += 1
    except Exception:
        pass
    return cnt


def compute(lab_path: Path, log_path: Optional[Path]) -> dict:
    prereg = _load_preregistered()
    if not prereg:
        return {"error": "preregistered_rules_missing", "path": str(PREREG_PATH)}
    prereg_hash = _sha256_file(PREREG_PATH)
    rule_thresholds = json.dumps(prereg["primary"]["entry"], sort_keys=True) + json.dumps(prereg["primary"]["exit"], sort_keys=True)
    rule_hash = hashlib.sha256(rule_thresholds.encode()).hexdigest()

    records: list[dict] = []
    if lab_path.exists():
        try:
            with lab_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            pass

    # Filter to records with v33 schema (so we know they were written after pre-registration)
    v33_records = [r for r in records if r.get("pnl_model_version") == "v33_route_aware"]
    matches = [r for r in v33_records if _matches_primary_rule(r, prereg)]
    # Deduplicate by mint
    seen_mints: set[str] = set()
    unique_matches: list[dict] = []
    for r in matches:
        mint = str(r.get("mint", ""))
        if mint in seen_mints:
            continue
        seen_mints.add(mint)
        unique_matches.append(r)

    outcomes = [_replay_v33_policy_C(r, prereg) for r in unique_matches]
    outcomes = [o for o in outcomes if o.get("entered")]
    wins = [o for o in outcomes if o.get("exit_pnl", 0.0) > 0]
    losses = [o for o in outcomes if o.get("exit_pnl", 0.0) <= 0]
    net = sum(o.get("exit_pnl", 0.0) for o in outcomes)
    max_loss = min((o.get("exit_pnl", 0.0) for o in outcomes), default=0.0)
    hit = (100.0 * len(wins) / max(len(outcomes), 1)) if outcomes else 0.0
    gross_win = sum(o.get("exit_pnl", 0.0) for o in wins)
    gross_loss = abs(sum(o.get("exit_pnl", 0.0) for o in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    top_winner = max((o.get("exit_pnl", 0.0) for o in wins), default=0.0)
    concentration = (top_winner / gross_win) if gross_win > 0 else 0.0

    # latency stats from log
    latencies = parse_log_latency(log_path) if log_path else {}
    buy_lats: list[float] = []
    sell_lats: list[float] = []
    for (side, _src), arr in latencies.items():
        if side == "buy":
            buy_lats.extend(arr)
        elif side == "sell":
            sell_lats.extend(arr)
    buy_p90 = _percentile(buy_lats, 90)
    sell_p90 = _percentile(sell_lats, 90)
    stale_decisions = parse_stale_decisions(log_path) if log_path else 0

    # qualifier-level metadata
    qualifier_sim_needed_zero = all(
        not str(r.get("pair_source", "")).startswith("sim_selected:") for r in unique_matches
    )
    qualifier_confidence_proven = all(
        r.get("cost_model_confidence") == "proven" for r in unique_matches
    )
    fallback_only = sum(
        1 for r in unique_matches if r.get("economic_quote_source", "none") not in (None, "none", "direct")
    )

    gates = prereg["qualification_gates_for_one_entry_pilot"]
    gate_results = {
        "n_ge_10": len(outcomes) >= int(gates["fresh_holdout_qualifying_n_min"]),
        "net_gt_0": net > 0,
        "hit_ge_70": hit >= float(gates["fresh_holdout_hit_rate_min_pct"]),
        "pf_ge_2": (pf >= float(gates["fresh_holdout_profit_factor_min"])) if pf != float("inf") else True,
        "max_loss_within_budget": max_loss >= float(gates["fresh_holdout_max_single_all_in_loss_max_sol"]),
        "losers_le_1": len(losses) <= int(gates["fresh_holdout_max_losers"]),
        "concentration_le_70": concentration <= float(gates["fresh_holdout_top_winner_concentration_max"]),
        "sim_needed_zero": qualifier_sim_needed_zero,
        "cost_model_confidence_proven": qualifier_confidence_proven,
        "no_fallback_only": fallback_only == 0,
        "sell_p90_le_750": (sell_p90 is not None and sell_p90 <= float(gates["sell_p90_latency_max_ms"])),
        "buy_p90_le_900_or_locked": (buy_p90 is None) or (buy_p90 <= 900.0),
        "stale_decisions_zero": stale_decisions == 0,
    }
    all_pass = all(gate_results.values())

    accumulator = {
        "rule_id": prereg["primary"]["rule_id"],
        "preregistered_file_hash": prereg_hash,
        "rule_thresholds_hash": rule_hash,
        "holdout_records_seen": len(v33_records),
        "qualifying_unique_mints": sorted(seen_mints),
        "qualifying_count": len(outcomes),
        "wins": len(wins),
        "losses": len(losses),
        "net_all_in_pnl": net,
        "max_single_all_in_loss": max_loss,
        "hit_rate": hit,
        "profit_factor": (pf if pf != float("inf") else None),
        "top_winner_contribution_pct": concentration * 100.0,
        "buy_p90_latency_ms": buy_p90,
        "sell_p90_latency_ms": sell_p90,
        "stale_quote_count": stale_decisions,
        "sim_needed_count": sum(
            1 for r in unique_matches if str(r.get("pair_source", "")).startswith("sim_selected:")
        ),
        "fallback_count": fallback_only,
        "all_gates_passed": all_pass,
        "gate_results": gate_results,
        "last_updated_ts": int(time.time() * 1000),
    }
    return accumulator


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lab", default=str(DEFAULT_LAB))
    p.add_argument("--log", default=None)
    p.add_argument("--out", default=str(OUTPUT_PATH))
    p.add_argument("--print", action="store_true")
    p.add_argument("--gate", action="store_true")
    args = p.parse_args(argv)
    log_path = Path(args.log) if args.log else None
    accumulator = compute(Path(args.lab), log_path)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(accumulator, indent=2, default=str), encoding="utf-8")
    if args.print or args.gate:
        print(json.dumps({
            "qualifying_count": accumulator.get("qualifying_count"),
            "wins": accumulator.get("wins"),
            "losses": accumulator.get("losses"),
            "net_all_in_pnl": accumulator.get("net_all_in_pnl"),
            "hit_rate": accumulator.get("hit_rate"),
            "profit_factor": accumulator.get("profit_factor"),
            "max_loss": accumulator.get("max_single_all_in_loss"),
            "all_gates_passed": accumulator.get("all_gates_passed"),
            "gate_results": accumulator.get("gate_results"),
        }, indent=2, default=str))
    if accumulator.get("all_gates_passed"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
