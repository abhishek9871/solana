"""
pgg2 shadow lab report (v30 — causal replay)

Reads the executable shadow lab JSONL and a bot log to emit a decision-quality
report.

Key change vs prior versions: lane ranking uses CAUSAL deterministic exit
policies replayed against each candidate's quote timeline. `best_executable_pnl`
is reported separately as "look-ahead only — not tradable".

Sections:
  0. quote coverage (gate before strategy)
  1. actual trades by lane (from bot log)
  2. ghost trades by lane_candidate (lookahead-only summary, labelled)
  3. no-quote causes by lane_candidate
  4. canary validation (if any)
  5. CAUSAL policy replay: lane × policy
  6. CAUSAL virtual rule matrix
  7. top causal winners / top causal losers
  8. recommendation (causal only, never avg_best)

Usage:
    py pgg2_shadow_lab_report.py [--lab PATH] [--log PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


DEFAULT_LAB = Path("data/pgg2_executable_shadow_lab.jsonl")


# --------------------------- log regexes ---------------------------

_RE_LIVE_BUY = re.compile(
    r"PGG2-LIVE-BUY (?P<mint>\S+) lane=(?P<lane>\S+) cost=(?P<cost>[\d.+-]+) "
    r"tokens=(?P<tokens>[\d.eE+-]+) fill=(?P<fill>[\d.eE+-]+) "
    r"quote_tokens=(?P<qt>[\d.eE+-]+)"
)
_RE_DIRECT_BUY = re.compile(
    r"PGG2-DIRECT-QUOTE BUY (?P<mint>\S+) route=\S+ in=(?P<in_sol>[\d.+-]+) "
    r"out=(?P<out_tok>[\d.eE+-]+)"
)
_RE_DIRECT_SELL = re.compile(
    r"PGG2-DIRECT-QUOTE SELL (?P<mint>\S+) route=\S+ in_tokens=(?P<in_tok>[\d.eE+-]+) "
    r"out=(?P<out_sol>[\d.+-]+)"
)
_RE_LIVE_BANK = re.compile(r"PGG2-LIVE-QUOTE-ANY-PROFIT-BANK (?P<mint>\S+).*pnl=(?P<pnl>[+\-\d.]+)")
_RE_LIVE_PROFIT = re.compile(r"PGG2-LIVE-QUOTE-PROFIT-CHECK (?P<mint>\S+).*pnl=(?P<pnl>[+\-\d.]+)")
_RE_LOSS_CHECK = re.compile(r"PGG2-LIVE-QUOTE-LOSS-CHECK (?P<mint>\S+).*pnl=(?P<pnl>[+\-\d.]+)")
_RE_LOSS_EVAL = re.compile(r"PGG2-LIVE-QUOTE-LOSS-EVAL (?P<mint>\S+) lane=(?P<lane>\S+)")
_RE_PROFIT_EVAL = re.compile(r"PGG2-LIVE-QUOTE-PROFIT-EVAL (?P<mint>\S+) lane=(?P<lane>\S+)")
_RE_LOSS_CLAMP = re.compile(r"quote_loss_clamp")
_RE_LIVE_SELL = re.compile(
    r"PGG2-LIVE-SELL (?P<mint>\S+) reason=(?P<reason>\S+) .*pnl=(?P<pnl>[+\-\d.]+)"
)
_RE_QUOTE_SHADOW_SELL = re.compile(
    r"PGG2-QUOTE-SHADOW-SELL (?P<mint>\S+) reason=(?P<reason>\S+).*pnl=(?P<pnl>[+\-\d.]+)"
)
_RE_NO_QUOTE_TOKENS = re.compile(r"PGG2-LIVE-BUY-NO-QUOTE-TOKENS")
_RE_CANARY_BUY = re.compile(
    r"PGG2-SHADOW-CANARY-BUY (?P<mint>\S+) amount=(?P<amt>[\d.+-]+) "
    r"quote_tokens=(?P<qt>[\d.eE+-]+) immediate_pnl=(?P<pnl>[+\-\d.]+)"
)
_RE_CANARY_OPEN = re.compile(
    r"PGG2-SHADOW-CANARY-OPEN (?P<mint>\S+) cost=(?P<cost>[\d.+-]+) tokens=(?P<tok>[\d.eE+-]+)"
)
_RE_V2_PROBE = re.compile(r"PGG2-DIRECT-V2-PROBE (?P<status>\S+) mint=(?P<mint>\S+)")
_RE_QUOTE_LATENCY = re.compile(
    r"PGG2-QUOTE-LATENCY side=(?P<side>\S+) mint=\S+ route=(?P<route>\S+) "
    r"source=(?P<source>\S+) lane=\S* start_ms=\d+ end_ms=\d+ "
    r"latency_ms=(?P<lat>\d+) success=(?P<succ>\d) error_class=(?P<err>\S*) "
    r"pair_source=\S* pair_prewarm=\d sim_needed=\d in_flight=(?P<inflight>\d+)"
)
_RE_RISK_TICK = re.compile(r"PGG2-RISK-(?:QUOTE-REQ|QUOTE-RESULT|CLOSE-REQUEST|CLOSE-ACK|QUOTE-STALE|WORKER-START)")


# --------------------------- causal policies ---------------------------

# Each policy: bank_pnl_sol, clamp_pnl_sol, timebox_ms, min_immediate_pnl_sol (None = no filter)
POLICIES: dict[str, dict] = {
    "A_fast_bank_tight_clamp": {
        "bank_pnl_sol": 0.00035,
        "clamp_pnl_sol": -0.00075,
        "timebox_ms": 2000,
        "min_immediate_pnl_sol": None,
    },
    "B_medium_bank_tight_clamp": {
        "bank_pnl_sol": 0.00060,
        "clamp_pnl_sol": -0.00090,
        "timebox_ms": 5000,
        "min_immediate_pnl_sol": None,
    },
    "C_moonshot_hold_protected_clamp": {
        "bank_pnl_sol": 0.00500,  # high bank threshold = ride to moonshot
        "clamp_pnl_sol": -0.00100,
        "timebox_ms": 10000,
        "min_immediate_pnl_sol": None,
        "bank_signal_only": True,
    },
    "D_immediate_edge_only": {
        "bank_pnl_sol": 0.00035,
        "clamp_pnl_sol": -0.00075,
        "timebox_ms": 2000,
        "min_immediate_pnl_sol": -0.00120,
    },
}


def build_timeline(record: dict) -> list[tuple[int, float]]:
    """Return [(t_ms, pnl), ...] using route-aware all_in_pnl when present.
    Falls back to legacy `immediate_pnl` / `pnl` only if a record predates the
    v33 schema. Records with `pnl_model_version=v33_route_aware` are used as-is.
    """
    timeline: list[tuple[int, float]] = []
    # prefer canonical all-in field
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
    return timeline


def replay_policy(record: dict, policy: dict) -> dict:
    """Deterministic causal exit replay.

    Returns dict with keys: entered (bool), exit_reason, exit_pnl,
    time_in_trade_ms, worst_adverse, bank_signal_only_pnl (if applicable).
    """
    timeline = build_timeline(record)
    if not timeline:
        return {"entered": False, "exit_reason": "no_timeline"}
    min_imm = policy.get("min_immediate_pnl_sol")
    entry_pnl = timeline[0][1]
    if min_imm is not None and entry_pnl < min_imm:
        return {"entered": False, "exit_reason": "below_entry_filter"}
    bank = policy["bank_pnl_sol"]
    clamp = policy["clamp_pnl_sol"]
    timebox = policy["timebox_ms"]
    bank_signal_only = policy.get("bank_signal_only", False)
    worst = entry_pnl
    first_bank_signal_pnl = None
    # Decision happens AT each timeline point (we sample at t_ms).
    # First check t=0 immediate. If entry conditions met at t=0, we have
    # already "entered". The exit decisions occur at subsequent timeline pts.
    for idx, (t, pnl) in enumerate(timeline):
        if t == 0:
            continue
        worst = min(worst, pnl)
        if pnl >= bank:
            if bank_signal_only and first_bank_signal_pnl is None:
                first_bank_signal_pnl = pnl
                # do not exit; ride
                continue
            return {
                "entered": True,
                "exit_reason": "bank",
                "exit_pnl": pnl,
                "time_in_trade_ms": t,
                "worst_adverse": worst,
                "bank_signal_pnl": first_bank_signal_pnl,
            }
        if pnl <= clamp:
            return {
                "entered": True,
                "exit_reason": "clamp",
                "exit_pnl": pnl,
                "time_in_trade_ms": t,
                "worst_adverse": worst,
                "bank_signal_pnl": first_bank_signal_pnl,
            }
        if t >= timebox:
            return {
                "entered": True,
                "exit_reason": "timebox",
                "exit_pnl": pnl,
                "time_in_trade_ms": t,
                "worst_adverse": worst,
                "bank_signal_pnl": first_bank_signal_pnl,
            }
    # Timeline ran out before any exit condition. Use last available point.
    last_t, last_pnl = timeline[-1]
    return {
        "entered": True,
        "exit_reason": "data_end",
        "exit_pnl": last_pnl,
        "time_in_trade_ms": last_t,
        "worst_adverse": worst,
        "bank_signal_pnl": first_bank_signal_pnl,
    }


# --------------------------- virtual rules ---------------------------

def _f(record: dict, key: str, default: float = 0.0) -> float:
    v = record.get(key)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _i(record: dict, key: str, default: int = 0) -> int:
    v = record.get(key)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


# v30 — strategy safety blacklist. Rules in this set are barred from being
# promoted to dry-live actual-entry, regardless of what the matrix shows on
# a later sample. Rules can only leave the blacklist after a deliberate code
# change AND independent causal validation under a different rule definition.
BLACKLIST: dict[str, str] = {
    "raw_momentum_current": "v33 audit: 107 records old_avg=-0.0021 → new_avg=+0.0002 SOL (improved under route-aware, but still net-negative cumulative under realistic-latency replay; requires independent fresh holdout to unblock)",
    "raw_momentum_quote_edge_only": "raw_momentum variant; not independently qualified under v33 route-aware + latency-realistic replay",
    "raw_momentum_recovered_quote_only": "raw_momentum variant; not independently qualified under v33 route-aware + latency-realistic replay",
}


def rule_trigger(record: dict, rule_id: str) -> bool:
    """Boolean trigger conditions per rule.

    All conditions use features known at the candidate's decision time.
    None refer to future_sells or best_executable_pnl.
    """
    lane = record.get("lane_candidate", "")
    exec_eligible = bool(record.get("execution_eligible"))
    imm = _f(record, "immediate_pnl", -1.0)
    recovered = bool(record.get("quote_recovered"))
    first_q_ms = _i(record, "first_quoteable_ms", -1)
    slot_buyers = _i(record, "slot_buyers")
    slot_buy_sol = _f(record, "slot_buy_sol")
    slot_top_share = _f(record, "slot_top_share")
    event_sol = _f(record, "event_sol")
    age_ms = _i(record, "age_ms")
    last_sell_age_ms = _i(record, "last_sell_age_ms", 999999)
    last_buy_age_ms = _i(record, "last_buy_age_ms", 999999)
    s5_buys = _i(record, "s5_buys")
    s5_buy_sol = _f(record, "s5_buy_sol")
    s5_sell_sol = _f(record, "s5_sell_sol")
    s30_unique_buyers = _i(record, "s30_unique_buyers")
    s60_unique_buyers = _i(record, "s60_unique_buyers")
    entry_impact = _f(record, "entry_quote_impact")

    # ---- Legacy raw_momentum & curve_lag & priced_snap kept for matrix visibility ----
    if rule_id == "raw_momentum_current":
        return lane == "raw_momentum_shadow" and exec_eligible
    if rule_id == "raw_momentum_quote_edge_only":
        return lane == "raw_momentum_shadow" and exec_eligible and imm >= -0.00100
    if rule_id == "raw_momentum_recovered_quote_only":
        return lane == "raw_momentum_shadow" and exec_eligible and recovered
    if rule_id == "curve_lag_reveal_current":
        return lane == "curve_lag_reveal_shadow" and exec_eligible
    if rule_id == "curve_lag_reveal_recovered_quote":
        return lane == "curve_lag_reveal_shadow" and exec_eligible and recovered
    if rule_id == "priced_snap_current":
        return lane == "priced_snap_like" and exec_eligible and bool(record.get("actual_entry_allowed"))
    if rule_id == "priced_snap_loose_shadow_only":
        return lane == "priced_snap_like" and exec_eligible
    if rule_id == "priced_snap_quote_edge_shadow_only":
        return lane == "priced_snap_like" and exec_eligible and imm >= -0.00120

    # ---- v30 explicit rule IDs (replacing generic_observation) ----
    if rule_id == "quote_edge_small_loss":
        return exec_eligible and -0.00120 <= imm <= -0.00020
    if rule_id == "quote_edge_near_flat":
        return exec_eligible and imm >= -0.00020
    if rule_id == "early_quote_recovery":
        return (
            exec_eligible
            and recovered
            and 0 <= first_q_ms <= 250
            and record.get("initial_direct_quote_error_class") == "curve_missing"
            and imm >= -0.00120
        )
    if rule_id == "late_quote_recovery":
        return (
            exec_eligible
            and recovered
            and 250 < first_q_ms <= 1000
            and record.get("initial_direct_quote_error_class") == "curve_missing"
        )
    if rule_id == "sell_absorption_recovery":
        # Sells exist and are recent, yet immediate quote is not crushed.
        # Approximation using entry-time-known features only.
        return (
            exec_eligible
            and last_sell_age_ms < 5000
            and s5_sell_sol > 0.05
            and imm >= -0.00120
        )
    if rule_id == "low_concentration_breadth":
        return (
            exec_eligible
            and slot_buyers >= 4
            and slot_top_share <= 0.40
            and slot_buy_sol >= 3.0
        )
    if rule_id == "micro_bounce_candidate":
        # Post-drop recovery: classifier already labels these `bounce_buy` or
        # `rug_bounce_like`. Tighten with age and recent buyer support.
        return (
            exec_eligible
            and lane in {"bounce_buy", "rug_bounce_like"}
            and s5_buys >= 1
            and age_ms >= 3000
        )
    if rule_id == "curve_lag_candidate":
        # Curve/account lag: recovered curve_missing OR a curve_lag-like classification.
        return exec_eligible and (
            (recovered and 0 <= first_q_ms <= 1000)
            or lane == "curve_lag_reveal_shadow"
        )
    return False


ALL_RULES = [
    # legacy (matrix visibility only)
    "raw_momentum_current",
    "raw_momentum_quote_edge_only",
    "raw_momentum_recovered_quote_only",
    "curve_lag_reveal_current",
    "curve_lag_reveal_recovered_quote",
    "priced_snap_current",
    "priced_snap_loose_shadow_only",
    "priced_snap_quote_edge_shadow_only",
    # v30 explicit rule IDs
    "quote_edge_small_loss",
    "quote_edge_near_flat",
    "early_quote_recovery",
    "late_quote_recovery",
    "sell_absorption_recovery",
    "low_concentration_breadth",
    "micro_bounce_candidate",
    "curve_lag_candidate",
]


def is_rule_blacklisted(rule_id: str) -> bool:
    return rule_id in BLACKLIST


# --------------------------- loaders ---------------------------


def load_lab(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def fmt_sol(x: float) -> str:
    return f"{x:+.6f}"


# --------------------------- sections ---------------------------


PREREG_PATH = Path("data/v33_preregistered_rules.json")


def _load_preregistered() -> Optional[dict]:
    try:
        with PREREG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _matches_primary_rule(record: dict, prereg: dict) -> bool:
    """Check if a single shadow lab record satisfies the pre-registered
    `v33_quote_edge_150_C` rule's entry definition. Uses ONLY fields the
    bot would have at decision time.
    """
    if not prereg:
        return False
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
    # sim_needed check — only direct fast-quote rows qualify
    if pair_src.startswith("sim_selected:"):
        return False
    aip = float(record.get("all_in_immediate_pnl") or -1.0)
    min_aip = float(p.get("all_in_immediate_pnl_min_sol", 0.0015))
    if aip < min_aip:
        return False
    if not record.get("execution_eligible"):
        return False
    return True


def _replay_v33_policy_C(record: dict, prereg: dict) -> dict:
    """Replay the pre-registered v33 exit policy on a record's timeline."""
    if not prereg:
        return {"entered": False}
    exit_p = prereg["primary"]["exit"]
    bank = float(exit_p["bank_all_in_pnl_min_sol"])
    scratch_min = float(exit_p["scratch_exit_min_all_in_pnl_sol"])
    clamp = float(exit_p["clamp_all_in_pnl_max_sol"])
    timebox_ms = int(exit_p["timebox_ms"])
    abs_max = int(exit_p["absolute_max_hold_ms"])
    timeline = build_timeline(record)
    if not timeline:
        return {"entered": False}
    entry_pnl = timeline[0][1]
    worst = entry_pnl
    prev_pnl = entry_pnl
    for t, pnl in timeline:
        if t == 0:
            continue
        worst = min(worst, pnl)
        if pnl >= bank:
            return {"entered": True, "exit_reason": "bank", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if pnl <= clamp:
            return {"entered": True, "exit_reason": "clamp", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        # scratch-exit on deterioration when still >= scratch_min
        if pnl >= scratch_min and pnl < prev_pnl - 0.00020 and pnl < bank:
            return {"entered": True, "exit_reason": "scratch", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if t >= abs_max:
            return {"entered": True, "exit_reason": "absolute_max_hold", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        if t >= timebox_ms and pnl < scratch_min:
            return {"entered": True, "exit_reason": "timebox", "exit_pnl": pnl, "time_in_trade_ms": t, "worst_adverse": worst}
        prev_pnl = pnl
    last_t, last_pnl = timeline[-1]
    return {"entered": True, "exit_reason": "data_end", "exit_pnl": last_pnl, "time_in_trade_ms": last_t, "worst_adverse": worst}


def section_holdout(records: list[dict]) -> str:
    """Evaluate the pre-registered rule on the current shadow lab as if it
    were the fresh holdout. Loads `data/v33_preregistered_rules.json`.
    """
    lines = ["## 7c. v33 PRE-REGISTERED RULE HOLDOUT"]
    prereg = _load_preregistered()
    if not prereg:
        lines.append("(no pre-registered rules file found at data/v33_preregistered_rules.json)")
        return "\n".join(lines) + "\n"
    lines.append(f"primary rule: {prereg['primary']['rule_id']}")
    lines.append(f"policy:       {prereg['primary']['policy_id']}")
    gates = prereg["qualification_gates_for_one_entry_pilot"]
    matches = [r for r in records if _matches_primary_rule(r, prereg)]
    if not matches:
        lines.append("(no records match the pre-registered entry definition in this lab)")
        return "\n".join(lines) + "\n"
    outcomes = [_replay_v33_policy_C(r, prereg) for r in matches]
    outcomes = [o for o in outcomes if o.get("entered")]
    n = len(outcomes)
    if n == 0:
        lines.append(f"{len(matches)} matching candidates but 0 entered after policy replay")
        return "\n".join(lines) + "\n"
    wins = [o for o in outcomes if o.get("exit_pnl", 0.0) > 0]
    losses = [o for o in outcomes if o.get("exit_pnl", 0.0) <= 0]
    net = sum(o["exit_pnl"] for o in outcomes)
    max_loss = min((o["exit_pnl"] for o in outcomes), default=0.0)
    hit = 100.0 * len(wins) / n
    gross_win = sum(o["exit_pnl"] for o in wins)
    gross_loss = abs(sum(o["exit_pnl"] for o in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    top_winner = max((o["exit_pnl"] for o in wins), default=0.0)
    concentration = (top_winner / gross_win) if gross_win > 0 else 0.0
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    lines.append(
        f"holdout candidates matched: {len(matches)}    entered after policy replay: {n}    "
        f"wins: {len(wins)}    losses: {len(losses)}"
    )
    lines.append(
        f"net all_in pnl: {net:+.6f} SOL    avg: {(net/n):+.6f}    "
        f"max single loss: {max_loss:+.6f}"
    )
    lines.append(f"hit rate: {hit:.1f}%    profit factor: {pf_s}    top winner concentration: {concentration:.2f}")
    # gates check
    lines.append("")
    lines.append("qualification gate check:")
    def gate(ok: bool, label: str) -> str:
        return ("[PASS] " if ok else "[FAIL] ") + label
    lines.append(gate(n >= gates["fresh_holdout_qualifying_n_min"], f"fresh holdout n >= {gates['fresh_holdout_qualifying_n_min']} (got {n})"))
    lines.append(gate(net > 0, f"net > 0 (got {net:+.6f})"))
    lines.append(gate(hit >= gates["fresh_holdout_hit_rate_min_pct"], f"hit >= {gates['fresh_holdout_hit_rate_min_pct']}% (got {hit:.1f}%)"))
    lines.append(gate(pf >= gates["fresh_holdout_profit_factor_min"], f"PF >= {gates['fresh_holdout_profit_factor_min']} (got {pf_s})"))
    lines.append(gate(max_loss >= gates["fresh_holdout_max_single_all_in_loss_max_sol"], f"max loss within {gates['fresh_holdout_max_single_all_in_loss_max_sol']} (got {max_loss:+.6f})"))
    lines.append(gate(concentration <= gates["fresh_holdout_top_winner_concentration_max"], f"top winner concentration <= {gates['fresh_holdout_top_winner_concentration_max']:.2f} (got {concentration:.2f})"))
    lines.append(gate(len(losses) <= gates["fresh_holdout_max_losers"], f"losers <= {gates['fresh_holdout_max_losers']} (got {len(losses)})"))
    return "\n".join(lines) + "\n"


def section_costmodel_proof(records: list[dict]) -> str:
    """v33 — TRANSACTION COST MODEL PROOF.

    Verified via direct code inspection of pgg2_direct_pump.py:
      - line 1060-1061: SELL closes user token ATA when
        PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL=1 (default True).
      - line 289-296: compute_budget_ixs sets compute_unit_limit (default
        220_000) and compute_unit_price derived from
        PGG2_DIRECT_PRIORITY_FEE_SOL (default 0.000005 SOL).
      - line 1287-1288: PumpSwap SELL closes base token ATA + line 1285
        closes WSOL quote ATA.
      - quote_pump_buy_tokens (line ~813): output tokens are computed AFTER
        deducting protocol+creator fee from input SOL.
      - quote_pump_sell_sol (line ~823): output SOL = gross - protocol_fee -
        creator_fee (i.e., fees ARE already inside quote_out).
    """
    lines = ["## 0d. TRANSACTION COST MODEL PROOF (verified against builder)"]
    cm = [
        {
            "route": "pump_bc",
            "buy_creates_ata": True,
            "sell_closes_token_ata": True,
            "sell_closes_wsol": False,
            "ata_rent_recovered_immediately": True,
            "cleanup_required": False,
            "cleanup_cost_est": 0.000000,
            "buy_signatures": 1,
            "sell_signatures": 1,
            "buy_base_fee_est": 0.000005,
            "sell_base_fee_est": 0.000005,
            "priority_fee_est": 0.000005,
            "compute_unit_limit": 220000,
            "compute_unit_price_microlamports": 22727,
            "quote_includes_protocol_fee": True,
            "quote_includes_creator_fee": True,
            "unrecoverable_roundtrip_cost": 0.000020,
            "confidence": "proven",
            "evidence": "pgg2_direct_pump.py:1060-1061 close_token_account on sell; quote_pump_buy/sell fees subtracted on-curve",
        },
        {
            "route": "pumpswap",
            "buy_creates_ata": True,
            "sell_closes_token_ata": True,
            "sell_closes_wsol": True,
            "ata_rent_recovered_immediately": True,
            "cleanup_required": False,
            "cleanup_cost_est": 0.000000,
            "buy_signatures": 1,
            "sell_signatures": 1,
            "buy_base_fee_est": 0.000005,
            "sell_base_fee_est": 0.000005,
            "priority_fee_est": 0.000005,
            "compute_unit_limit": 220000,
            "compute_unit_price_microlamports": 22727,
            "quote_includes_protocol_fee": True,
            "quote_includes_creator_fee": True,
            "unrecoverable_roundtrip_cost": 0.000020,
            "confidence": "proven",
            "evidence": "pgg2_direct_pump.py:1287-1288 close_token_account base; line 1285 close WSOL on sell",
        },
        {
            "route": "raptor",
            "buy_creates_ata": "unknown",
            "sell_closes_token_ata": "unknown",
            "ata_rent_recovered_immediately": "inferred",
            "cleanup_required": "unknown",
            "buy_signatures": 1,
            "sell_signatures": 1,
            "buy_base_fee_est": 0.000005,
            "sell_base_fee_est": 0.000005,
            "priority_fee_est": "broker-side",
            "quote_includes_protocol_fee": True,
            "quote_includes_creator_fee": True,
            "unrecoverable_roundtrip_cost": 0.000020,
            "confidence": "inferred",
            "evidence": "broker-built tx, SolanaTracker swap endpoint; sell-side accounting opaque",
        },
    ]
    lines.append(
        "ATA rent (0.002039 SOL) is recovered atomically on SELL only when "
        "PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL=1 (default True). If disabled, "
        "rent counts as locked capital and must be added to unrecoverable cost."
    )
    for row in cm:
        lines.append("")
        lines.append(f"route={row['route']} confidence={row['confidence']}")
        for k, v in row.items():
            if k in ("route", "confidence"):
                continue
            lines.append(f"  {k}: {v}")
    # also dump runtime evidence (counts of close_token_account & priority-fee log lines)
    return "\n".join(lines) + "\n"


def _all_in_pnl_pump_bc(cost_sol: float, quote_out: float, tx_fee: float = 0.000010, ata_recoverable: bool = True) -> dict:
    """Mirror of broker.quote_all_in_pnl for pump_bc route. Used in the
    report so we can recompute historical canaries under the new model.
    """
    gross = quote_out - cost_sol
    extra = 2 * tx_fee
    return {
        "gross_quote_pnl": gross,
        "extra_overhead_not_in_quote": extra,
        "all_in_pnl": gross - extra,
        "pnl_basis": "pump_bc_route_aware_v32",
    }


def section_costmodel_audit(records: list[dict], health: dict) -> str:
    """v32 — recompute historical shadow records under the new CostModel
    and show the delta vs. the legacy fixed-overhead PnL.
    """
    lines = ["## 0c. COST MODEL AUDIT (legacy fixed-overhead vs route-aware)"]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    legacy_overhead = 0.00235
    eligible = [r for r in records if r.get("execution_eligible")]
    if not eligible:
        lines.append("(no execution-eligible records)")
        return "\n".join(lines) + "\n"
    lines.append(
        f"legacy fixed overhead = {legacy_overhead:.6f} SOL  "
        f"new model (pump_bc) = 2 * tx_fee = 0.000020 SOL (ATA rent recoverable)"
    )
    by_lane: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for r in eligible:
        cost = float(r.get("scout_sol") or 0.015)
        out = float(r.get("immediate_reverse_out") or 0.0)
        if out <= 0:
            continue
        old_pnl = out - legacy_overhead - cost
        new = _all_in_pnl_pump_bc(cost, out)
        delta = new["all_in_pnl"] - old_pnl
        by_lane[r.get("lane_candidate", "?")].append((old_pnl, new["all_in_pnl"], delta))
    lines.append(
        f"{'lane':28s} {'n':>4s} {'old_avg':>11s} {'new_avg':>11s} {'avg_delta':>11s} "
        f"{'new_pos':>7s} {'new_neg':>7s}"
    )
    for lane, rows in sorted(by_lane.items()):
        n = len(rows)
        old_avg = sum(r[0] for r in rows) / n if n else 0.0
        new_avg = sum(r[1] for r in rows) / n if n else 0.0
        delta_avg = sum(r[2] for r in rows) / n if n else 0.0
        new_pos = sum(1 for r in rows if r[1] > 0)
        new_neg = sum(1 for r in rows if r[1] < 0)
        lines.append(
            f"{lane:28s} {n:>4d} {old_avg:+11.6f} {new_avg:+11.6f} {delta_avg:+11.6f} "
            f"{new_pos:>7d} {new_neg:>7d}"
        )
    # Fpts canary recompute (if present)
    fpts = [r for r in records if str(r.get("mint", "")).startswith("Fpts") and r.get("lane_candidate") == "shadow_lab_canary"]
    if fpts:
        r = fpts[0]
        cost = float(r.get("scout_sol") or 0.015)
        out = float(r.get("immediate_reverse_out") or 0.0)
        old_pnl = out - legacy_overhead - cost
        new = _all_in_pnl_pump_bc(cost, out)
        lines.append("")
        lines.append("Fpts canary recompute (representative case from 2026-05-11 v32 run):")
        lines.append(f"  cost={cost:.6f} quote_out={out:.6f}")
        lines.append(f"  legacy old_pnl = out - 0.00235 - cost = {old_pnl:+.6f}")
        lines.append(f"  new all_in    = {new['all_in_pnl']:+.6f}")
        lines.append(f"  difference    = {new['all_in_pnl'] - old_pnl:+.6f} (old overhead overstated by ~0.00233)")
        verdict = "would NOT pass new floor (>=0)" if new["all_in_pnl"] < 0 else "would PASS new floor"
        lines.append(f"  verdict: {verdict}")
    return "\n".join(lines) + "\n"


def section_latency_adjusted_floor(records: list[dict], health: dict) -> str:
    """v32 — per-rule numeric latency-adjusted entry floor."""
    lines = ["## 6c. LATENCY-ADJUSTED ENTRY FLOOR (numeric per rule)"]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    sell_latencies = [
        float(s.get("latency_ms") or 0)
        for s in (health.get("quote_latency_samples") or [])
        if s.get("side") == "sell"
    ]
    p90_sell_lat_ms = 0
    if sell_latencies:
        sorted_lat = sorted(sell_latencies)
        p90_sell_lat_ms = sorted_lat[int(len(sorted_lat) * 0.90)]
    eligible = [r for r in records if r.get("execution_eligible")]
    if not eligible:
        lines.append("(no execution-eligible records)")
        return "\n".join(lines) + "\n"
    target_buffer = 0.00020
    lines.append(
        f"target_buffer = +{target_buffer:.5f} SOL    p90 sell latency = {p90_sell_lat_ms:.0f}ms"
    )
    lines.append(
        f"{'rule_id':36s} {'n':>4s} {'p90_adverse':>12s} {'old_thresh':>11s} "
        f"{'new_floor':>11s} {'pass_n':>6s}"
    )
    old_threshold = -0.00150
    for rule_id in ALL_RULES:
        if is_rule_blacklisted(rule_id):
            continue
        cohort = [r for r in eligible if rule_trigger(r, rule_id)]
        if not cohort:
            continue
        deteriorations: list[float] = []
        for r in cohort:
            imm = float(r.get("immediate_pnl") or 0.0)
            future = r.get("future_sells") or []
            within = [f for f in future if isinstance(f, dict) and int(f.get("t_ms") or 0) <= max(1000, p90_sell_lat_ms * 2)]
            if not within:
                continue
            worst_after_entry = min(float(f.get("pnl") or 0.0) for f in within)
            deteriorations.append(imm - worst_after_entry)
        if not deteriorations:
            continue
        sd = sorted(deteriorations)
        p90_adverse = sd[int(len(sd) * 0.90) if int(len(sd) * 0.90) < len(sd) else len(sd) - 1]
        new_floor = target_buffer + max(0.0, p90_adverse)
        pass_n = sum(1 for r in cohort if float(r.get("immediate_pnl") or -1.0) >= new_floor)
        lines.append(
            f"{rule_id:36s} {len(cohort):>4d} {p90_adverse:+12.6f} {old_threshold:+11.6f} "
            f"{new_floor:+11.6f} {pass_n:>6d}"
        )
    return "\n".join(lines) + "\n"


def section_quote_latency(health: dict) -> str:
    lines = ["## 0a. QUOTE LATENCY (gate before any actual entry)"]
    if health.get("missing") or health.get("error"):
        lines.append("(no log supplied — quote latency comes from log file)")
        return "\n".join(lines) + "\n"
    samples = health.get("quote_latency_samples") or []
    if not samples:
        lines.append("(no PGG2-QUOTE-LATENCY samples in log)")
        return "\n".join(lines) + "\n"

    def pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sv = sorted(values)
        idx = int(len(sv) * p / 100.0)
        if idx >= len(sv):
            idx = len(sv) - 1
        return sv[idx]

    by_side: dict[str, list[float]] = defaultdict(list)
    by_side_source: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_route: dict[str, list[float]] = defaultdict(list)
    in_flight_max = 0
    over_buckets = {"gt_150ms": 0, "gt_300ms": 0, "gt_500ms": 0, "gt_1000ms": 0}
    for s in samples:
        lat = float(s.get("latency_ms") or 0)
        by_side[s.get("side", "?")].append(lat)
        by_side_source[(s.get("side", "?"), s.get("source", "?"))].append(lat)
        by_route[s.get("route", "?")].append(lat)
        if lat > 150:
            over_buckets["gt_150ms"] += 1
        if lat > 300:
            over_buckets["gt_300ms"] += 1
        if lat > 500:
            over_buckets["gt_500ms"] += 1
        if lat > 1000:
            over_buckets["gt_1000ms"] += 1
        try:
            in_flight_max = max(in_flight_max, int(s.get("in_flight") or 0))
        except Exception:
            pass

    lines.append(
        f"total samples: {len(samples)}    max in_flight per mint observed: {in_flight_max}"
    )
    lines.append(f"{'side':6s} {'n':>4s} {'p50':>6s} {'p75':>6s} {'p90':>6s} {'p95':>6s} {'max':>6s}")
    for side, lats in sorted(by_side.items()):
        lines.append(
            f"{side:6s} {len(lats):>4d} {pct(lats, 50):>6.0f} {pct(lats, 75):>6.0f} "
            f"{pct(lats, 90):>6.0f} {pct(lats, 95):>6.0f} {max(lats):>6.0f}"
        )
    lines.append("")
    lines.append("Latency by (side, pair source):")
    lines.append(f"  {'side':6s} {'source':28s} {'n':>4s} {'p50':>6s} {'p95':>6s}")
    for (side, src), lats in sorted(by_side_source.items()):
        lines.append(
            f"  {side:6s} {src:28s} {len(lats):>4d} {pct(lats, 50):>6.0f} {pct(lats, 95):>6.0f}"
        )
    lines.append("")
    lines.append("Latency by route:")
    for route, lats in sorted(by_route.items()):
        lines.append(
            f"  {route:24s} n={len(lats)} p50={pct(lats, 50):.0f} p95={pct(lats, 95):.0f}"
        )
    lines.append("")
    total = len(samples)
    lines.append(
        f"latency over thresholds: >150ms={over_buckets['gt_150ms']}/{total}  "
        f">300ms={over_buckets['gt_300ms']}/{total}  "
        f">500ms={over_buckets['gt_500ms']}/{total}  "
        f">1000ms={over_buckets['gt_1000ms']}/{total}"
    )
    return "\n".join(lines) + "\n"


def section_realistic_latency_replay(records: list[dict], health: dict) -> str:
    """v31 — replay causal policies with the constraint that quotes are
    only available at intervals equal to the measured median sell-quote
    latency (or 500ms fallback).
    """
    lines = ["## 5c. REALISTIC LATENCY CAUSAL REPLAY"]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    sell_samples = [
        float(s.get("latency_ms") or 0)
        for s in (health.get("quote_latency_samples") or [])
        if s.get("side") == "sell"
    ]
    if not sell_samples:
        lines.append("(no sell-side latency samples — supply --log)")
        return "\n".join(lines) + "\n"
    sorted_lat = sorted(sell_samples)
    median_lat_ms = sorted_lat[len(sorted_lat) // 2]
    p90_lat_ms = sorted_lat[int(len(sorted_lat) * 0.90)]
    lines.append(
        f"using sell-side median latency = {median_lat_ms:.0f}ms, p90 = {p90_lat_ms:.0f}ms"
    )
    # Build a coarsened timeline per record: only points at multiples of median_lat_ms
    # remain visible. Replay each policy with that filtered timeline.
    bucket_ms = max(50.0, median_lat_ms)
    eligible = [r for r in records if r.get("execution_eligible")]
    if not eligible:
        lines.append("(no execution-eligible records)")
        return "\n".join(lines) + "\n"
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        by_lane[r.get("lane_candidate", "?")].append(r)
    lines.append(
        f"{'lane':24s} {'policy':30s} {'n':>4s} {'ent':>4s} {'net':>11s} {'maxLoss':>11s} {'hit%':>5s} {'PF':>5s}"
    )
    flagged: list[tuple] = []
    for lane, recs in sorted(by_lane.items()):
        for pid, policy in POLICIES.items():
            outcomes = []
            for r in recs:
                tl = build_timeline(r)
                # filter timeline: keep only points at multiples of bucket_ms,
                # forcing the policy to wait `bucket_ms` between samples.
                if not tl:
                    continue
                kept: list[tuple[int, float]] = [tl[0]]
                next_threshold = bucket_ms
                for t, pnl in tl[1:]:
                    if t >= next_threshold:
                        kept.append((t, pnl))
                        next_threshold = t + bucket_ms
                # synth record with filtered timeline
                synth = dict(r)
                synth["immediate_pnl"] = kept[0][1] if kept else r.get("immediate_pnl")
                synth["future_sells"] = [
                    {"t_ms": t, "pnl": p} for (t, p) in kept[1:]
                ]
                o = replay_policy(synth, policy)
                if o.get("entered"):
                    outcomes.append(o)
            s = _summarize_outcomes(outcomes)
            pf_s = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
            lines.append(
                f"{lane:24s} {pid:30s} {len(recs):>4d} {s['ent']:>4d} {s['net']:+11.6f} "
                f"{s['max_loss']:+11.6f} {s['hit']:>5.1f} {pf_s:>5s}"
            )
            # ideal-positive but latency-negative?
            ideal_outcomes = [replay_policy(r, policy) for r in recs]
            ideal_outcomes = [o for o in ideal_outcomes if o.get("entered")]
            ideal = _summarize_outcomes(ideal_outcomes)
            if ideal["net"] > 0 and s["net"] <= 0 and s["ent"] >= 3:
                flagged.append((lane, pid, ideal["net"], s["net"]))
    if flagged:
        lines.append("")
        lines.append("Rules that are IDEAL-POSITIVE but LATENCY-NEGATIVE (must be blacklisted):")
        for lane, pid, ideal_net, real_net in flagged:
            lines.append(
                f"  - {lane}/{pid}: ideal_net={ideal_net:+.6f} realistic_net={real_net:+.6f}"
            )
    return "\n".join(lines) + "\n"


def section_quote_coverage(records: list[dict], health: dict) -> str:
    lines = ["## 0. QUOTE COVERAGE"]
    if not records:
        lines.append("(no shadow-lab records)")
        return "\n".join(lines) + "\n"
    total = len(records)
    direct_buy_ok = sum(1 for r in records if r.get("direct_quote_success"))
    direct_sell_ok = sum(1 for r in records if r.get("direct_sell_quote_success"))
    direct_sell_among_buy_ok = sum(
        1 for r in records if r.get("direct_quote_success") and r.get("direct_sell_quote_success")
    )
    fallback_ok = sum(1 for r in records if r.get("economic_quote_success"))
    exec_eligible = sum(1 for r in records if r.get("execution_eligible"))
    pair_sources = Counter()
    prewarm_attempted = 0
    prewarm_success = 0
    curve_missing = 0
    curve_missing_recovered = 0
    first_quoteable_ms = []
    for r in records:
        ps = r.get("pair_source")
        if ps:
            pair_sources[ps] += 1
        if r.get("pair_prewarm_attempted"):
            prewarm_attempted += 1
            if r.get("pair_prewarm_success"):
                prewarm_success += 1
        if r.get("initial_direct_quote_error_class") == "curve_missing":
            curve_missing += 1
            if r.get("quote_recovered"):
                curve_missing_recovered += 1
                if r.get("first_quoteable_ms") is not None:
                    first_quoteable_ms.append(int(r["first_quoteable_ms"]))
    error_classes = Counter()
    for r in records:
        ec = r.get("direct_quote_error_class") or r.get("reverse_quote_error_class")
        if ec:
            error_classes[ec] += 1
    avg_entry_ms = 0.0
    nq = [r.get("entry_quote_ms") for r in records if r.get("entry_quote_ms")]
    if nq:
        avg_entry_ms = sum(float(x) for x in nq) / len(nq)
    lines.append(f"total candidates              : {total}")
    lines.append(f"direct BUY  quote success     : {direct_buy_ok}/{total} = {100.0*direct_buy_ok/max(total,1):5.1f}%")
    lines.append(f"direct SELL quote success     : {direct_sell_ok}/{total} = {100.0*direct_sell_ok/max(total,1):5.1f}%")
    if direct_buy_ok:
        lines.append(
            f"direct SELL after BUY ok      : {direct_sell_among_buy_ok}/{direct_buy_ok} = {100.0*direct_sell_among_buy_ok/max(direct_buy_ok,1):5.1f}%"
        )
    lines.append(f"economic fallback success     : {fallback_ok}/{total} = {100.0*fallback_ok/max(total,1):5.1f}%")
    lines.append(f"execution-eligible candidates : {exec_eligible}/{total} = {100.0*exec_eligible/max(total,1):5.1f}%")
    lines.append(f"avg entry quote latency       : {avg_entry_ms:.1f} ms")
    if prewarm_attempted:
        lines.append(
            f"pair prewarm from current_sig : {prewarm_success}/{prewarm_attempted} = {100.0*prewarm_success/max(prewarm_attempted,1):5.1f}%"
        )
    lines.append(f"curve_missing initial errors  : {curve_missing}")
    if curve_missing:
        rate = 100.0 * curve_missing_recovered / curve_missing
        med = median(first_quoteable_ms) if first_quoteable_ms else None
        lines.append(
            f"curve_missing recovered       : {curve_missing_recovered}/{curve_missing} = {rate:5.1f}%"
            + (f"  median_first_quoteable_ms={med}" if med is not None else "")
        )
    if pair_sources:
        lines.append("pair source breakdown:")
        for src, cnt in pair_sources.most_common():
            lines.append(f"  {src:>28s}  {cnt}")
    if error_classes:
        lines.append("top direct-quote error classes:")
        for cls, cnt in error_classes.most_common(5):
            lines.append(f"  {cls:>28s}  {cnt}")
    # v2 probe
    v2_attempted = sum(1 for r in records if r.get("v2_probe_attempted"))
    v2_build_ok = sum(1 for r in records if r.get("v2_probe_build_ok"))
    v2_sim_ok = sum(1 for r in records if r.get("v2_probe_sim_ok"))
    if v2_attempted:
        lines.append("Pump v2 probe:")
        lines.append(f"  attempted={v2_attempted} build_ok={v2_build_ok} sim_ok={v2_sim_ok}")
        v2_errs = Counter()
        for r in records:
            if r.get("v2_probe_error"):
                v2_errs[str(r.get("v2_probe_error"))[:60]] += 1
        for err, cnt in v2_errs.most_common(3):
            lines.append(f"    err={err} cnt={cnt}")
    return "\n".join(lines) + "\n"


def section_ghost_summary(records: list[dict]) -> str:
    if not records:
        return "## 2. Ghost trades by lane_candidate (LOOK-AHEAD ONLY — not tradable)\n(no shadow-lab records)\n"
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_lane[r.get("lane_candidate", "?")].append(r)
    lines = [
        "## 2. Ghost trades by lane_candidate (LOOK-AHEAD ONLY — not tradable)",
        "    avg_best uses future_sells max; use ONLY for diagnostics. See section 5/6 for promotion-quality numbers.",
    ]
    lines.append(
        f"{'lane_candidate':28s} {'cnt':>4s} {'win':>4s} {'loss':>4s} {'mshot':>5s} "
        f"{'fake':>4s} {'nq':>4s} {'allow':>5s} {'imm_sum':>12s} {'BEST_la':>12s} {'avg_BEST_la':>12s}"
    )
    for lane, recs in sorted(by_lane.items()):
        cnt = len(recs)
        win = sum(1 for r in recs if r.get("label") == "executable_win")
        loss = sum(1 for r in recs if r.get("label") == "executable_loss")
        mshot = sum(1 for r in recs if r.get("label") == "missed_moonshot")
        fake = sum(1 for r in recs if r.get("label") == "fake_pump")
        nq = sum(1 for r in recs if r.get("label") == "blocked_by_no_quote")
        allow = sum(1 for r in recs if r.get("actual_entry_allowed"))
        imm = sum(_f(r, "immediate_pnl") for r in recs)
        best = sum(_f(r, "best_executable_pnl") for r in recs)
        avg_best = best / cnt if cnt else 0.0
        lines.append(
            f"{lane:28s} {cnt:>4d} {win:>4d} {loss:>4d} {mshot:>5d} {fake:>4d} "
            f"{nq:>4d} {allow:>5d} {imm:+12.6f} {best:+12.6f} {avg_best:+12.6f}"
        )
    return "\n".join(lines) + "\n"


def section_no_quote_causes(records: list[dict]) -> str:
    lines = ["## 3. No-quote causes by lane_candidate"]
    blocked = [r for r in records if r.get("label") == "blocked_by_no_quote"]
    if not blocked:
        lines.append("(no blocked_by_no_quote records)")
        return "\n".join(lines) + "\n"
    breakdown: dict[tuple, int] = defaultdict(int)
    error_samples: dict[str, str] = {}
    for r in blocked:
        lane = r.get("lane_candidate", "?")
        side = r.get("no_quote_side", "?")
        reason = r.get("no_quote_reason") or r.get("entry_quote_error") or r.get("reverse_quote_error") or "unknown"
        if ":" in reason and reason not in ("amountOut_le_zero",):
            reason_short = reason.split(":", 1)[0].strip()
            error_samples.setdefault(reason_short, reason[:120])
            reason = reason_short
        breakdown[(lane, side, reason)] += 1
    lines.append(f"{'lane':28s} {'side':5s} {'reason':28s} {'cnt':>4s}")
    for (lane, side, reason), cnt in sorted(breakdown.items(), key=lambda kv: -kv[1]):
        lines.append(f"{lane:28s} {side:5s} {reason:28s} {cnt:>4d}")
    if error_samples:
        lines.append("Error samples:")
        for k, v in error_samples.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) + "\n"


def section_actual_trades(health: dict) -> str:
    lines = ["## 1. Actual trades by lane (from bot log)"]
    if health.get("missing"):
        lines.append(f"(log file not found: {health.get('path')})")
        return "\n".join(lines) + "\n"
    if health.get("error"):
        lines.append(f"(error reading log: {health.get('error')})")
        return "\n".join(lines) + "\n"
    lanes = health.get("lanes") or {}
    if not lanes:
        lines.append("(no PGG2-LIVE-SELL lines parsed)")
        return "\n".join(lines) + "\n"
    lines.append(f"{'lane':24s} {'cnt':>4s} {'W':>4s} {'L':>4s} {'net':>12s} {'maxLoss':>12s}")
    for lane, d in sorted(lanes.items()):
        lines.append(
            f"{lane:24s} {d['cnt']:>4d} {d['w']:>4d} {d['l']:>4d} {d['net']:+12.6f} {d['max_loss']:+12.6f}"
        )
    return "\n".join(lines) + "\n"


def section_canary(records: list[dict], health: dict) -> str:
    lines = ["## 4. Canary validation (P0 machinery)"]
    canary_records = [r for r in records if r.get("lane_candidate") == "shadow_lab_canary"]
    canary_buy = health.get("canary_buy") or []
    canary_open = health.get("canary_open") or []
    if not canary_buy and not canary_records:
        lines.append("(no canary fired — canary disabled by default in coverage runs; set PGG2_SHADOW_LAB_CANARY_ACTUAL_ENTRY=1 to opt in)")
        return "\n".join(lines) + "\n"

    def ok(b: bool, msg: str) -> str:
        return ("[PASS] " if b else "[FAIL] ") + msg

    lines.append(ok(bool(canary_buy), f"PGG2-SHADOW-CANARY-BUY observed ({len(canary_buy)} entries)"))
    lines.append(ok(bool(canary_open), f"PGG2-SHADOW-CANARY-OPEN observed ({len(canary_open)} entries)"))
    pb = health.get("profit_eval_canary", 0)
    lc = health.get("loss_eval_canary", 0)
    lines.append(ok(pb > 0, f"quote_profit_bank evaluated (eval_trace={pb})"))
    lines.append(ok(lc > 0, f"quote_loss_clamp evaluated (eval_trace={lc})"))
    return "\n".join(lines) + "\n"


def section_causal_replay(records: list[dict]) -> str:
    lines = [
        "## 5. CAUSAL policy replay (lane × policy) — promotion-quality numbers",
        "    Decision uses ONLY information available at decision time. Wins/losses count entered candidates.",
    ]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    eligible = [r for r in records if r.get("execution_eligible")]
    if not eligible:
        lines.append("(no execution-eligible records)")
        return "\n".join(lines) + "\n"
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in eligible:
        by_lane[r.get("lane_candidate", "?")].append(r)
    hdr = (
        f"{'lane':24s} {'policy':30s} {'n':>4s} {'ent':>4s} {'W':>4s} {'L':>4s} "
        f"{'net':>11s} {'avg':>11s} {'maxLoss':>11s} {'hit%':>5s} {'PF':>5s} {'medT':>5s}"
    )
    lines.append(hdr)
    for lane, recs in sorted(by_lane.items()):
        for pid, policy in POLICIES.items():
            outcomes = []
            for r in recs:
                o = replay_policy(r, policy)
                if o.get("entered"):
                    outcomes.append(o)
            n = len(recs)
            ent = len(outcomes)
            wins = [o for o in outcomes if o.get("exit_pnl", 0) > 0]
            losses = [o for o in outcomes if o.get("exit_pnl", 0) <= 0]
            net = sum(o.get("exit_pnl", 0.0) for o in outcomes)
            avg = (net / ent) if ent else 0.0
            max_loss = min((o.get("exit_pnl", 0.0) for o in outcomes), default=0.0)
            hit_rate = (100.0 * len(wins) / max(ent, 1)) if ent else 0.0
            gross_win = sum(o.get("exit_pnl", 0.0) for o in wins)
            gross_loss = abs(sum(o.get("exit_pnl", 0.0) for o in losses))
            pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
            med_t = median([o.get("time_in_trade_ms", 0) for o in outcomes]) if outcomes else 0
            lines.append(
                f"{lane:24s} {pid:30s} {n:>4d} {ent:>4d} {len(wins):>4d} {len(losses):>4d} "
                f"{net:+11.6f} {avg:+11.6f} {max_loss:+11.6f} {hit_rate:>5.1f} "
                f"{('inf' if pf == float('inf') else f'{pf:.2f}'):>5s} {int(med_t):>5d}"
            )
    return "\n".join(lines) + "\n"


def section_rule_matrix(records: list[dict]) -> str:
    lines = [
        "## 6. CAUSAL virtual rule matrix — per-rule × per-policy",
        "    A rule may be considered for promotion only if: n>=30 entered, causal net>0, max loss <= budget, hit_rate good OR PF>>1.",
    ]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    hdr = (
        f"{'rule_id':36s} {'policy':30s} {'n':>4s} {'ent':>4s} {'W':>4s} {'L':>4s} "
        f"{'net':>11s} {'avg':>11s} {'maxLoss':>11s} {'hit%':>5s} {'PF':>5s} {'qualifies':>9s}"
    )
    lines.append(hdr)
    promotable: list[tuple] = []
    for rule_id in ALL_RULES:
        cohort = [r for r in records if rule_trigger(r, rule_id)]
        n = len(cohort)
        for pid, policy in POLICIES.items():
            outcomes = []
            for r in cohort:
                o = replay_policy(r, policy)
                if o.get("entered"):
                    outcomes.append(o)
            ent = len(outcomes)
            wins = [o for o in outcomes if o.get("exit_pnl", 0) > 0]
            losses = [o for o in outcomes if o.get("exit_pnl", 0) <= 0]
            net = sum(o.get("exit_pnl", 0.0) for o in outcomes)
            avg = (net / ent) if ent else 0.0
            max_loss = min((o.get("exit_pnl", 0.0) for o in outcomes), default=0.0)
            hit_rate = (100.0 * len(wins) / max(ent, 1)) if ent else 0.0
            gross_win = sum(o.get("exit_pnl", 0.0) for o in wins)
            gross_loss = abs(sum(o.get("exit_pnl", 0.0) for o in losses))
            pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
            qualifies = (
                ent >= 30
                and net > 0
                and max_loss > -0.00075  # v33 max loss budget per trade (was -0.00300)
                and (hit_rate >= 50.0 or pf >= 1.50)
            )
            if qualifies:
                promotable.append((rule_id, pid, ent, net, avg, hit_rate, pf))
            lines.append(
                f"{rule_id:36s} {pid:30s} {n:>4d} {ent:>4d} {len(wins):>4d} {len(losses):>4d} "
                f"{net:+11.6f} {avg:+11.6f} {max_loss:+11.6f} {hit_rate:>5.1f} "
                f"{('inf' if pf == float('inf') else f'{pf:.2f}'):>5s} {('YES' if qualifies else '-'):>9s}"
            )
    if promotable:
        lines.append("")
        lines.append("Qualifying rule × policy combos (n>=30 entered, causal net>0, max loss <= -0.003 SOL, hit%>=50 OR PF>=1.5):")
        for rid, pid, ent, net, avg, hr, pf in promotable:
            pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
            lines.append(f"  - {rid}/{pid}: n={ent} net={net:+.6f} avg={avg:+.6f} hit={hr:.1f}% PF={pf_s}")
    return "\n".join(lines) + "\n"


def _summarize_outcomes(outcomes: list[dict]) -> dict:
    if not outcomes:
        return {"ent": 0, "net": 0.0, "wins": 0, "losses": 0, "max_loss": 0.0, "hit": 0.0, "pf": 0.0, "concentration": 0.0}
    wins = [o for o in outcomes if o.get("exit_pnl", 0.0) > 0]
    losses = [o for o in outcomes if o.get("exit_pnl", 0.0) <= 0]
    net = sum(o.get("exit_pnl", 0.0) for o in outcomes)
    max_loss = min((o.get("exit_pnl", 0.0) for o in outcomes), default=0.0)
    hit = 100.0 * len(wins) / max(len(outcomes), 1)
    gross_win = sum(o.get("exit_pnl", 0.0) for o in wins)
    gross_loss = abs(sum(o.get("exit_pnl", 0.0) for o in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    top_winner = max((o.get("exit_pnl", 0.0) for o in wins), default=0.0)
    concentration = (top_winner / gross_win) if gross_win > 0 else 0.0
    return {
        "ent": len(outcomes),
        "net": net,
        "wins": len(wins),
        "losses": len(losses),
        "max_loss": max_loss,
        "hit": hit,
        "pf": pf,
        "concentration": concentration,
    }


# v30 — grid-search rule templates. Each template is a tuple
# (rule_id, predicate_fn). We generate ~50-100 rules combining a small base
# template with quote-edge / quote-recovery / breadth / concentration thresholds.
def _generate_mined_rules() -> list[tuple[str, callable]]:
    rules: list[tuple[str, callable]] = []

    def add(rule_id: str, fn):
        rules.append((rule_id, fn))

    # quote-edge variants (immediate pnl thresholds)
    for imm_lo in (-0.00150, -0.00120, -0.00100, -0.00075, -0.00050, -0.00020, 0.00000):
        threshold = imm_lo
        add(
            f"mined_quote_edge_pnl_ge_{int(round(-imm_lo * 1e5))}",
            lambda r, t=threshold: bool(r.get("execution_eligible")) and _f(r, "immediate_pnl", -1.0) >= t,
        )
    # recovery variants
    for fmax in (100, 250, 500, 1000, 2000):
        add(
            f"mined_recovery_under_{fmax}ms",
            lambda r, fm=fmax: bool(r.get("execution_eligible"))
            and bool(r.get("quote_recovered"))
            and 0 <= _i(r, "first_quoteable_ms", -1) <= fm,
        )
    # breadth / concentration variants
    for buyers_min in (3, 4, 5, 6, 8):
        for top_max in (0.30, 0.40, 0.50):
            add(
                f"mined_breadth_b{buyers_min}_top{int(top_max*100)}",
                lambda r, b=buyers_min, t=top_max: bool(r.get("execution_eligible"))
                and _i(r, "slot_buyers") >= b
                and _f(r, "slot_top_share") <= t,
            )
    # combined: recovery + quote-edge
    for fmax in (250, 500, 1000):
        for imm_lo in (-0.00120, -0.00075, -0.00050):
            add(
                f"mined_recovery_under_{fmax}_pnl_ge_{int(round(-imm_lo * 1e5))}",
                lambda r, fm=fmax, t=imm_lo: bool(r.get("execution_eligible"))
                and bool(r.get("quote_recovered"))
                and 0 <= _i(r, "first_quoteable_ms", -1) <= fm
                and _f(r, "immediate_pnl", -1.0) >= t,
            )
    # bounce candidates: post-drop with recent buyer support
    for sells_age_max in (3000, 5000):
        for s5_buys_min in (1, 2, 3):
            add(
                f"mined_bounce_s{sells_age_max}_b{s5_buys_min}",
                lambda r, sa=sells_age_max, sb=s5_buys_min: bool(r.get("execution_eligible"))
                and _i(r, "last_sell_age_ms", 999999) <= sa
                and _i(r, "s5_buys") >= sb
                and _f(r, "immediate_pnl", -1.0) >= -0.00120,
            )
    return rules


def section_rule_miner(records: list[dict]) -> str:
    lines = [
        "## 6b. CAUSAL RULE MINER — discovery / validation split (60/40 time-ordered)",
        "    Overfit protection: rule must be net-positive in BOTH discovery and validation.",
        "    Promotion: total entered >= 30, validation entered >= 10, max single loss > -0.003 SOL,",
        "    profit factor >= 1.5 OR hit rate >= 60%, top-winner concentration <= 70%.",
    ]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    ordered = sorted(records, key=lambda r: int(r.get("ts_ms") or 0))
    if len(ordered) < 5:
        lines.append(f"(only {len(ordered)} records — too few for split)")
        return "\n".join(lines) + "\n"
    split_idx = int(len(ordered) * 0.6)
    discovery = ordered[:split_idx]
    validation = ordered[split_idx:]
    lines.append(f"discovery records : {len(discovery)}    validation records : {len(validation)}")

    mined_rules = _generate_mined_rules()
    qualifying: list[dict] = []
    candidates_inspected: list[dict] = []
    for rule_id, fn in mined_rules:
        if is_rule_blacklisted(rule_id):
            continue
        for pid, policy in POLICIES.items():
            disc_outcomes = [replay_policy(r, policy) for r in discovery if fn(r)]
            disc_outcomes = [o for o in disc_outcomes if o.get("entered")]
            val_outcomes = [replay_policy(r, policy) for r in validation if fn(r)]
            val_outcomes = [o for o in val_outcomes if o.get("entered")]
            disc = _summarize_outcomes(disc_outcomes)
            val = _summarize_outcomes(val_outcomes)
            total_ent = disc["ent"] + val["ent"]
            if total_ent < 5:
                continue
            candidate = {
                "rule_id": rule_id,
                "policy": pid,
                "disc": disc,
                "val": val,
                "total_ent": total_ent,
                "total_net": disc["net"] + val["net"],
            }
            candidates_inspected.append(candidate)
            promo = (
                total_ent >= 30
                and val["ent"] >= 10
                and disc["net"] > 0
                and val["net"] > 0
                and val["max_loss"] > -0.00075
                and disc["max_loss"] > -0.00075
                and (val["pf"] >= 1.5 or val["hit"] >= 60.0)
                and val["concentration"] <= 0.70
                and disc["concentration"] <= 0.70
            )
            if promo:
                qualifying.append(candidate)

    lines.append(f"rule×policy combos inspected: {len(candidates_inspected)}")
    if candidates_inspected:
        lines.append("")
        lines.append("Top 20 by VALIDATION causal net pnl:")
        candidates_inspected.sort(key=lambda c: c["val"]["net"], reverse=True)
        hdr = f"  {'rule_id':40s} {'policy':30s} {'d_n':>4s} {'d_net':>10s} {'v_n':>4s} {'v_net':>10s} {'v_hit':>6s} {'v_pf':>6s} {'qual':>5s}"
        lines.append(hdr)
        for c in candidates_inspected[:20]:
            pf_s = "inf" if c["val"]["pf"] == float("inf") else f"{c['val']['pf']:.2f}"
            qual = "YES" if (c in qualifying) else "-"
            lines.append(
                f"  {c['rule_id']:40s} {c['policy']:30s} "
                f"{c['disc']['ent']:>4d} {c['disc']['net']:+10.6f} "
                f"{c['val']['ent']:>4d} {c['val']['net']:+10.6f} "
                f"{c['val']['hit']:>5.1f}% {pf_s:>6s} {qual:>5s}"
            )
        lines.append("")
        lines.append("Top 20 by VALIDATION profit factor (val n >= 3):")
        ranked_pf = [c for c in candidates_inspected if c["val"]["ent"] >= 3]
        ranked_pf.sort(key=lambda c: (c["val"]["pf"] if c["val"]["pf"] != float("inf") else 1e9), reverse=True)
        for c in ranked_pf[:20]:
            pf_s = "inf" if c["val"]["pf"] == float("inf") else f"{c['val']['pf']:.2f}"
            qual = "YES" if (c in qualifying) else "-"
            lines.append(
                f"  {c['rule_id']:40s} {c['policy']:30s} "
                f"{c['val']['ent']:>4d} v_net={c['val']['net']:+.6f} v_hit={c['val']['hit']:.1f}% pf={pf_s} {qual}"
            )
    # rejected: passed discovery, failed validation
    passed_disc = [c for c in candidates_inspected if c["disc"]["net"] > 0 and c["disc"]["ent"] >= 3]
    failed_val = [c for c in passed_disc if c["val"]["net"] <= 0]
    if failed_val:
        lines.append("")
        lines.append(f"Rules that passed discovery but failed validation: {len(failed_val)} (top 5)")
        for c in failed_val[:5]:
            lines.append(
                f"  - {c['rule_id']:40s} {c['policy']:30s} d_net={c['disc']['net']:+.6f} v_net={c['val']['net']:+.6f}"
            )
    # passed both but under sample threshold
    passed_both_thin = [
        c
        for c in candidates_inspected
        if c["disc"]["net"] > 0 and c["val"]["net"] > 0 and c["total_ent"] < 30
    ]
    if passed_both_thin:
        lines.append("")
        lines.append(f"Rules positive in both windows but under n>=30: {len(passed_both_thin)} (top 5)")
        for c in passed_both_thin[:5]:
            pf_s = "inf" if c["val"]["pf"] == float("inf") else f"{c['val']['pf']:.2f}"
            lines.append(
                f"  - {c['rule_id']:40s} {c['policy']:30s} total_n={c['total_ent']} v_n={c['val']['ent']} v_hit={c['val']['hit']:.1f}% pf={pf_s}"
            )
    if qualifying:
        lines.append("")
        lines.append(f"QUALIFYING rules (all gates passed): {len(qualifying)}")
        for c in qualifying:
            pf_s = "inf" if c["val"]["pf"] == float("inf") else f"{c['val']['pf']:.2f}"
            lines.append(
                f"  ** {c['rule_id']}/{c['policy']}: total_n={c['total_ent']} v_n={c['val']['ent']} "
                f"v_net={c['val']['net']:+.6f} v_hit={c['val']['hit']:.1f}% pf={pf_s} **"
            )
    return "\n".join(lines) + "\n"


def section_blacklist(records: list[dict]) -> str:
    lines = ["## 5b. STRATEGY BLACKLIST (rules barred from promotion)"]
    if not BLACKLIST:
        lines.append("(empty)")
        return "\n".join(lines) + "\n"
    for rule_id, reason in BLACKLIST.items():
        cohort = [r for r in records if rule_trigger(r, rule_id)]
        lines.append(f"  - {rule_id}")
        lines.append(f"    reason: {reason}")
        lines.append(f"    current records matching: {len(cohort)}")
    return "\n".join(lines) + "\n"


def section_top_causal(records: list[dict], limit: int = 10) -> str:
    lines = ["## 7. Top causal winners / losers (using policy A — fast bank/tight clamp)"]
    if not records:
        lines.append("(no records)")
        return "\n".join(lines) + "\n"
    eligible = [r for r in records if r.get("execution_eligible")]
    if not eligible:
        lines.append("(no execution-eligible records)")
        return "\n".join(lines) + "\n"
    policy_A = POLICIES["A_fast_bank_tight_clamp"]
    rows: list[tuple[dict, dict]] = []
    for r in eligible:
        o = replay_policy(r, policy_A)
        if o.get("entered"):
            rows.append((r, o))
    rows_winners = sorted(rows, key=lambda x: x[1].get("exit_pnl", 0.0), reverse=True)[:limit]
    rows_losers = sorted(rows, key=lambda x: x[1].get("exit_pnl", 0.0))[:limit]
    lines.append("Winners:")
    for r, o in rows_winners:
        lines.append(
            f"  + {str(r.get('mint',''))[:10]} lane={r.get('lane_candidate')} "
            f"exit={o.get('exit_pnl', 0.0):+.6f}@{o.get('time_in_trade_ms')}ms "
            f"reason={o.get('exit_reason')} imm={r.get('immediate_pnl', 0.0):+.6f}"
        )
    lines.append("Losers:")
    for r, o in rows_losers:
        lines.append(
            f"  - {str(r.get('mint',''))[:10]} lane={r.get('lane_candidate')} "
            f"exit={o.get('exit_pnl', 0.0):+.6f}@{o.get('time_in_trade_ms')}ms "
            f"reason={o.get('exit_reason')} imm={r.get('immediate_pnl', 0.0):+.6f}"
        )
    return "\n".join(lines) + "\n"


def section_recommendation(records: list[dict]) -> str:
    lines = ["## 8. Recommendation (CAUSAL only, miner-driven)"]
    if not records:
        lines.append("Insufficient data. Run coverage test.")
        return "\n".join(lines) + "\n"
    # Find qualifying rule via the MINER. The miner uses discovery/validation
    # split + concentration + max-loss budget + PF/hit gates.
    ordered = sorted(records, key=lambda r: int(r.get("ts_ms") or 0))
    split_idx = int(len(ordered) * 0.6)
    discovery = ordered[:split_idx]
    validation = ordered[split_idx:]
    qualifying: list[dict] = []
    # explicit + mined rules
    explicit_rule_fns: list[tuple[str, callable]] = [(rid, (lambda r, rid=rid: rule_trigger(r, rid))) for rid in ALL_RULES]
    mined_rule_fns = _generate_mined_rules()
    for rule_id, fn in explicit_rule_fns + mined_rule_fns:
        if is_rule_blacklisted(rule_id):
            continue
        for pid, policy in POLICIES.items():
            disc_outcomes = [o for r in discovery if fn(r) for o in [replay_policy(r, policy)] if o.get("entered")]
            val_outcomes = [o for r in validation if fn(r) for o in [replay_policy(r, policy)] if o.get("entered")]
            disc = _summarize_outcomes(disc_outcomes)
            val = _summarize_outcomes(val_outcomes)
            total_ent = disc["ent"] + val["ent"]
            if total_ent < 30:
                continue
            if val["ent"] < 10:
                continue
            if disc["net"] <= 0 or val["net"] <= 0:
                continue
            if val["max_loss"] <= -0.00075 or disc["max_loss"] <= -0.00075:
                continue
            if not (val["pf"] >= 1.5 or val["hit"] >= 60.0):
                continue
            if val["concentration"] > 0.70 or disc["concentration"] > 0.70:
                continue
            qualifying.append({"rule_id": rule_id, "policy": pid, "disc": disc, "val": val, "total_ent": total_ent})
    if qualifying:
        # promote the qualifying rule with highest validation net
        qualifying.sort(key=lambda c: c["val"]["net"], reverse=True)
        best = qualifying[0]
        pf_s = "inf" if best["val"]["pf"] == float("inf") else f"{best['val']['pf']:.2f}"
        lines.append(
            f"Promote `{best['rule_id']}` with policy `{best['policy']}` to **DRY-LIVE actual-entry** only:"
        )
        lines.append(
            f"  total_n={best['total_ent']}, validation_n={best['val']['ent']}, "
            f"validation_net={best['val']['net']:+.6f} SOL, hit={best['val']['hit']:.1f}%, PF={pf_s}, "
            f"max_loss={best['val']['max_loss']:+.6f}, concentration={best['val']['concentration']:.2f}"
        )
        lines.append(
            "  DO NOT enable real live. Add an env switch + actual-entry path for this rule only, "
            "and re-validate the next session with the same miner."
        )
        return "\n".join(lines) + "\n"

    # No rule qualifies — name the exact missing condition and which sampler needs density.
    direct_buy_ok = sum(1 for r in records if r.get("direct_quote_success"))
    coverage_pct = 100.0 * direct_buy_ok / max(len(records), 1)
    if coverage_pct < 50.0:
        lines.append(
            f"No rule qualifies. Quote coverage at {coverage_pct:.1f}% is below 50%. "
            "Fix quote coverage before strategy interpretation."
        )
        return "\n".join(lines) + "\n"

    # Diagnose: which rules came closest, what's the blocking condition?
    near_misses: list[tuple] = []
    explicit_rule_fns = [(rid, (lambda r, rid=rid: rule_trigger(r, rid))) for rid in ALL_RULES if not is_rule_blacklisted(rid)]
    mined_rule_fns = [r for r in _generate_mined_rules() if not is_rule_blacklisted(r[0])]
    for rule_id, fn in explicit_rule_fns + mined_rule_fns:
        for pid, policy in POLICIES.items():
            disc_o = [o for r in discovery if fn(r) for o in [replay_policy(r, policy)] if o.get("entered")]
            val_o = [o for r in validation if fn(r) for o in [replay_policy(r, policy)] if o.get("entered")]
            disc = _summarize_outcomes(disc_o)
            val = _summarize_outcomes(val_o)
            total = disc["ent"] + val["ent"]
            if total < 5:
                continue
            if disc["net"] > 0 and val["net"] > 0:
                # near miss — which gate failed?
                reasons = []
                if total < 30:
                    reasons.append(f"total_n={total} (<30)")
                if val["ent"] < 10:
                    reasons.append(f"val_n={val['ent']} (<10)")
                if val["max_loss"] <= -0.00075:
                    reasons.append(f"val_max_loss={val['max_loss']:+.6f} (<=-0.003)")
                if not (val["pf"] >= 1.5 or val["hit"] >= 60.0):
                    pf_s = "inf" if val["pf"] == float("inf") else f"{val['pf']:.2f}"
                    reasons.append(f"val_pf={pf_s} hit={val['hit']:.1f}% (need PF>=1.5 OR hit>=60%)")
                if val["concentration"] > 0.70:
                    reasons.append(f"val_concentration={val['concentration']:.2f} (>0.70)")
                near_misses.append((rule_id, pid, total, val["ent"], val["net"], reasons))

    if not near_misses:
        lines.append(
            "No rule qualifies. No rule is even net-positive in both discovery and validation. "
            "Causally toxic regime or rule definitions need revision. "
            "Required: more density on `early_quote_recovery`, `late_quote_recovery`, `curve_lag_candidate`, "
            "`micro_bounce_candidate`, `low_concentration_breadth`, `quote_edge_near_flat`. "
            "Tune the targeted sampler (PGG2_SHADOW_OBSERVE_*) to admit more candidates from these families."
        )
        return "\n".join(lines) + "\n"

    # Sort by total_n then validation net
    near_misses.sort(key=lambda x: (x[2], x[4]), reverse=True)
    lines.append("No rule qualifies for promotion. Closest near-misses (positive in both windows):")
    for rule_id, pid, total, val_n, val_net, reasons in near_misses[:10]:
        lines.append(f"  - {rule_id}/{pid}: total_n={total} val_n={val_n} val_net={val_net:+.6f}")
        for reason in reasons:
            lines.append(f"      blocked by: {reason}")
    # what to do
    sampler_needs = set()
    for rule_id, _pid, total, _val_n, _val_net, _r in near_misses[:10]:
        if "quote_edge" in rule_id:
            sampler_needs.add("quote_edge family (raise sampling for buys with imm_pnl >= -0.0012)")
        if "recovery" in rule_id or "curve_lag" in rule_id:
            sampler_needs.add("curve_lag / recovery family (loosen MIN_EVENT_SOL, lower curve-retry cooldown)")
        if "bounce" in rule_id:
            sampler_needs.add("bounce family (loosen rug_bounce drop-percent threshold)")
        if "breadth" in rule_id:
            sampler_needs.add("low_concentration_breadth (admit small slot_buy_sol when slot_top is low)")
    if sampler_needs:
        lines.append("")
        lines.append("Targeted sampler density needed:")
        for s in sorted(sampler_needs):
            lines.append(f"  - {s}")
    return "\n".join(lines) + "\n"


# --------------------------- log parser ---------------------------


def parse_log(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "missing": True}
    health: dict = {
        "buys": 0,
        "buys_with_quote_tokens": 0,
        "buys_with_zero_quote": 0,
        "no_quote_tokens_fallbacks": 0,
        "direct_buys": 0,
        "direct_sells": [],
        "any_profit_bank_fires": 0,
        "profit_check_fires": 0,
        "profit_eval_canary": 0,
        "loss_eval_canary": 0,
        "loss_check_fires": 0,
        "loss_clamp_fires": 0,
        "canary_buy": [],
        "canary_open": [],
        "v2_probe_attempted": 0,
        "quote_latency_samples": [],
        "risk_worker_started": False,
        "risk_quote_reqs": 0,
        "risk_quote_results": 0,
        "risk_quote_stale": 0,
        "risk_close_requests": 0,
        "risk_close_acks": 0,
        "lanes": defaultdict(lambda: {"cnt": 0, "w": 0, "l": 0, "net": 0.0, "max_loss": 0.0}),
    }
    mint_lane: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _RE_LIVE_BUY.search(line)
                if m:
                    health["buys"] += 1
                    qt = 0.0
                    try:
                        qt = float(m.group("qt"))
                    except Exception:
                        pass
                    if qt > 0:
                        health["buys_with_quote_tokens"] += 1
                    else:
                        health["buys_with_zero_quote"] += 1
                    mint_lane[m.group("mint")] = m.group("lane")
                if _RE_NO_QUOTE_TOKENS.search(line):
                    health["no_quote_tokens_fallbacks"] += 1
                m = _RE_DIRECT_BUY.search(line)
                if m:
                    health["direct_buys"] += 1
                m = _RE_DIRECT_SELL.search(line)
                if m:
                    health["direct_sells"].append({"mint": m.group("mint"), "out_sol": m.group("out_sol")})
                if _RE_LIVE_BANK.search(line):
                    health["any_profit_bank_fires"] += 1
                if _RE_LIVE_PROFIT.search(line):
                    health["profit_check_fires"] += 1
                m = _RE_PROFIT_EVAL.search(line)
                if m and m.group("lane") == "shadow_lab_canary":
                    health["profit_eval_canary"] += 1
                if _RE_LOSS_CHECK.search(line):
                    health["loss_check_fires"] += 1
                m = _RE_LOSS_EVAL.search(line)
                if m and m.group("lane") == "shadow_lab_canary":
                    health["loss_eval_canary"] += 1
                if _RE_LOSS_CLAMP.search(line):
                    health["loss_clamp_fires"] += 1
                if _RE_V2_PROBE.search(line):
                    health["v2_probe_attempted"] += 1
                m = _RE_QUOTE_LATENCY.search(line)
                if m:
                    health["quote_latency_samples"].append({
                        "side": m.group("side"),
                        "route": m.group("route"),
                        "source": m.group("source"),
                        "latency_ms": int(m.group("lat")),
                        "success": m.group("succ") == "1",
                        "in_flight": int(m.group("inflight")),
                    })
                if "PGG2-RISK-WORKER-START" in line:
                    health["risk_worker_started"] = True
                if "PGG2-RISK-QUOTE-REQ" in line:
                    health["risk_quote_reqs"] += 1
                if "PGG2-RISK-QUOTE-RESULT" in line:
                    health["risk_quote_results"] += 1
                if "PGG2-RISK-QUOTE-STALE" in line:
                    health["risk_quote_stale"] += 1
                if "PGG2-RISK-CLOSE-REQUEST" in line:
                    health["risk_close_requests"] += 1
                if "PGG2-RISK-CLOSE-ACK" in line:
                    health["risk_close_acks"] += 1
                m = _RE_CANARY_BUY.search(line)
                if m:
                    health["canary_buy"].append({"mint": m.group("mint"), "qt": m.group("qt"), "pnl": m.group("pnl")})
                m = _RE_CANARY_OPEN.search(line)
                if m:
                    health["canary_open"].append({"mint": m.group("mint"), "cost": m.group("cost")})
                m = _RE_LIVE_SELL.search(line)
                if m:
                    mint = m.group("mint")
                    lane = mint_lane.get(mint, "?")
                    try:
                        pnl = float(m.group("pnl"))
                    except Exception:
                        continue
                    L = health["lanes"][lane]
                    L["cnt"] += 1
                    L["net"] += pnl
                    if pnl >= 0:
                        L["w"] += 1
                    else:
                        L["l"] += 1
                        L["max_loss"] = min(L["max_loss"], pnl)
    except Exception as exc:
        return {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    health["lanes"] = dict(health["lanes"])
    health["path"] = str(path)
    return health


# --------------------------- driver ---------------------------


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lab", default=str(DEFAULT_LAB))
    p.add_argument("--log", default=None)
    args = p.parse_args(argv)

    records = load_lab(Path(args.lab))
    health = parse_log(Path(args.log)) if args.log else {"missing": True}

    print(f"# PGG2 shadow lab report (v33 cost-model-proven) — lab={args.lab} records={len(records)}\n")
    print(section_costmodel_proof(records))
    print(section_costmodel_audit(records, health))
    print(section_quote_latency(health))
    print(section_latency_adjusted_floor(records, health))
    print(section_quote_coverage(records, health))
    print(section_actual_trades(health))
    print(section_ghost_summary(records))
    print(section_no_quote_causes(records))
    print(section_canary(records, health))
    print(section_causal_replay(records))
    print(section_blacklist(records))
    print(section_rule_matrix(records))
    print(section_rule_miner(records))
    print(section_realistic_latency_replay(records, health))
    print(section_top_causal(records))
    print(section_holdout(records))
    print(section_recommendation(records))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
