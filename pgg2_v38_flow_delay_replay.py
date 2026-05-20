"""Replay v38 live-equivalent flow-delay scalp on existing PGG2 logs.

Outputs:
  - TIME_DELAY_EDGE_FORENSIC.md
  - LIVE_FLOW_DELAY_SIM.md
  - V38_REPLAY_ON_EXISTING_LOGS.md

This intentionally does not use the old entry-snapshot/same-state ESB PnL as
an entry feature. It replays a sequential model: our buy first, external tape
after entry, then sell after processed/confirmed latency.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from pgg2_live_flow_delay_sim import (
    ExitPolicy,
    LatencyModel,
    LiveFlowDelaySimulator,
    TapeEvent,
)
from pgg2_pump_selfimpact_sim import LAMPORTS_PER_SOL, SelfImpactCostModel


INITIAL_VSOL = 30 * LAMPORTS_PER_SOL
INITIAL_VTOKENS = 1_073_000_000_000_000
INITIAL_REAL_TOKENS = 793_100_000_000_000
PROTOCOL_FEE_BPS = 100
CREATOR_FEE_BPS = 0
MODES = ("processed_mode", "confirmed_mode", "optimistic_ordered_mode")
LIVE_SAFE_MODES = ("processed_mode", "confirmed_mode")


def short_addr(s: str) -> str:
    return f"{s[:4]}..{s[-4:]}" if len(s) > 10 else s


@dataclass
class RunSpec:
    name: str
    log: Path
    raw: Path


@dataclass
class Entry:
    run: str
    short_mint: str
    full_mint: str = ""
    buy_quote_ts_ms: int = 0
    sell_quote_ts_ms: int = 0
    delay_ms: int = 0
    quote_tokens: float = 0.0
    first_sell_quote_out: float = 0.0
    bank_sell_quote_out: float = 0.0
    drylive_all_in_pnl: float = 0.0
    pair_source: str = ""
    entry_features: str = ""
    pre_stats: dict[str, Any] = field(default_factory=dict)
    post_stats: dict[str, Any] = field(default_factory=dict)
    sim: dict[str, Any] = field(default_factory=dict)


def load_raw(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    by_mint: dict[str, list[dict[str, Any]]] = {}
    short_map: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("kind") != "trade":
                continue
            mint = str(row.get("mint") or "")
            if not mint:
                continue
            by_mint.setdefault(mint, []).append(row)
            short_map.setdefault(short_addr(mint), mint)
    for rows in by_mint.values():
        rows.sort(key=lambda r: int(r.get("ts_ms") or 0))
    return by_mint, short_map


_RE_BUY = re.compile(
    r"PGG2-(?:SCALP|DRYLIVE-PILOT)-BUY .*?mint=(\S+).*?"
    r"quote_tokens=([0-9.]+).*?immediate_out=([0-9.]+).*?"
    r"immediate_pnl=([+\-0-9.]+).*?pair_source=(\S+).*?"
    r"entry_features=\{([^}]*)\}"
)
_RE_LOCK = re.compile(
    r"PGG2-QUOTE-SHADOW-BUY-LOCKED mint=(\S+) quote_id=\S+:(\d+) "
    r"quote_tokens=([0-9.]+).*?quote_age_ms=(\d+) immediate_out=([0-9.]+)"
)
_RE_SELL = re.compile(
    r"PGG2-QUOTE-SHADOW-SELL (\S+) reason=(\S+) quote_out=([0-9.]+).*?"
    r"all_in_pnl=([+\-0-9.]+)"
)


def parse_entries(run: RunSpec, short_map: dict[str, str]) -> list[Entry]:
    latest_buy: dict[str, dict[str, Any]] = {}
    latest_lock: dict[str, dict[str, Any]] = {}
    entries: list[Entry] = []
    for line in run.log.read_text(encoding="utf-8", errors="ignore").splitlines():
        mb = _RE_BUY.search(line)
        if mb:
            s, toks, out, imm, pair, features = mb.groups()
            latest_buy[s] = {
                "quote_tokens": float(toks),
                "first_sell_quote_out": float(out),
                "immediate_pnl": float(imm),
                "pair_source": pair,
                "entry_features": features,
            }
        ml = _RE_LOCK.search(line)
        if ml:
            s, t0, toks, age, out = ml.groups()
            latest_lock[s] = {
                "buy_quote_ts_ms": int(t0),
                "delay_ms": int(age),
                "quote_tokens": float(toks),
                "first_sell_quote_out": float(out),
            }
        ms = _RE_SELL.search(line)
        if ms:
            s, reason, qout, pnl = ms.groups()
            if s not in latest_lock:
                continue
            lock = latest_lock[s]
            buy = latest_buy.get(s, {})
            t0 = int(lock["buy_quote_ts_ms"])
            delay = int(lock["delay_ms"])
            entries.append(
                Entry(
                    run=run.name,
                    short_mint=s,
                    full_mint=short_map.get(s, ""),
                    buy_quote_ts_ms=t0,
                    sell_quote_ts_ms=t0 + delay,
                    delay_ms=delay,
                    quote_tokens=float(lock.get("quote_tokens") or buy.get("quote_tokens") or 0.0),
                    first_sell_quote_out=float(lock.get("first_sell_quote_out") or buy.get("first_sell_quote_out") or 0.0),
                    bank_sell_quote_out=float(qout),
                    drylive_all_in_pnl=float(pnl),
                    pair_source=str(buy.get("pair_source") or ""),
                    entry_features=str(buy.get("entry_features") or ""),
                )
            )
    return [e for e in entries if e.drylive_all_in_pnl > 0]


def flow_stats(rows: list[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    window = [r for r in rows if start_ms <= int(r.get("ts_ms") or 0) <= end_ms]
    buys = [r for r in window if r.get("side") == "buy"]
    sells = [r for r in window if r.get("side") == "sell"]
    by_user: dict[str, float] = {}
    for r in buys:
        user = str(r.get("user") or r.get("signer") or "")
        by_user[user] = by_user.get(user, 0.0) + float(r.get("sol") or 0.0)
    buy_sol = sum(float(r.get("sol") or 0.0) for r in buys)
    sell_sol = sum(float(r.get("sol") or 0.0) for r in sells)
    return {
        "buys": len(buys),
        "sells": len(sells),
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "buyers": len(by_user),
        "top_share": (max(by_user.values()) / buy_sol) if by_user and buy_sol > 0 else 0.0,
    }


def make_tape(rows: list[dict[str, Any]], start_ms: int, end_ms: int) -> list[TapeEvent]:
    out: list[TapeEvent] = []
    for r in rows:
        ts = int(r.get("ts_ms") or 0)
        if start_ms <= ts <= end_ms:
            out.append(
                TapeEvent(
                    ts_ms=ts,
                    is_buy=(r.get("side") == "buy"),
                    sol_lamports=int(float(r.get("sol") or 0.0) * LAMPORTS_PER_SOL),
                    token_amount=int(float(r.get("token_amount") or 0.0)),
                    user=str(r.get("user") or r.get("signer") or ""),
                )
            )
    return out


def reconstruct_state(rows: list[dict[str, Any]], entry_ts_ms: int) -> tuple[int, int, int, str]:
    """Approximate curve state from raw tape before entry.

    For fresh pump mints this is usually close. If the run did not observe the
    mint from birth, this is marked approximate in the report.
    """
    sim = LiveFlowDelaySimulator(PROTOCOL_FEE_BPS, CREATOR_FEE_BPS)
    vsol = INITIAL_VSOL
    vtok = INITIAL_VTOKENS
    real = INITIAL_REAL_TOKENS
    seen_before = 0
    for r in rows:
        ts = int(r.get("ts_ms") or 0)
        if ts >= entry_ts_ms:
            break
        sol_lamports = int(float(r.get("sol") or 0.0) * LAMPORTS_PER_SOL)
        if r.get("side") == "buy":
            vsol, vtok, real = sim._apply_external_buy(vsol, vtok, real, sol_lamports)
        elif r.get("side") == "sell":
            vsol, vtok, real = sim._apply_external_sell(vsol, vtok, real, sol_lamports)
        seen_before += 1
    confidence = "raw_from_birth" if seen_before > 0 else "approx_initial_state"
    return vsol, vtok, real, confidence


def simulator() -> LiveFlowDelaySimulator:
    cost = SelfImpactCostModel(
        base_tx_fee_lamports=10_000,
        compute_unit_limit=440_000,
        compute_unit_price_micro_lamports=22_727,
        ata_recoverable=True,
    )
    return LiveFlowDelaySimulator(
        protocol_fee_bps=PROTOCOL_FEE_BPS,
        creator_fee_bps=CREATOR_FEE_BPS,
        cost_model=cost,
        latency=LatencyModel(),
        policy=ExitPolicy(
            bank_pnl_min_sol=0.00060,
            scratch_pnl_min_sol=0.00005,
            clamp_pnl_max_loss_sol=-0.00050,
            max_hold_ms=3000,
            flow_stall_ms=750,
            require_post_entry_buy=True,
        ),
    )


def enrich_entries(entries: list[Entry], raw_by_mint: dict[str, list[dict[str, Any]]]) -> None:
    sim = simulator()
    for e in entries:
        rows = raw_by_mint.get(e.full_mint, [])
        e.pre_stats = flow_stats(rows, e.buy_quote_ts_ms - 750, e.buy_quote_ts_ms - 1)
        e.post_stats = flow_stats(rows, e.buy_quote_ts_ms, e.sell_quote_ts_ms)
        vsol, vtok, real, conf = reconstruct_state(rows, e.buy_quote_ts_ms)
        tape = make_tape(rows, e.buy_quote_ts_ms - 750, e.buy_quote_ts_ms + 3000)
        for mode in ("processed_mode", "confirmed_mode", "optimistic_ordered_mode"):
            res = sim.simulate(mode, e.buy_quote_ts_ms, 0.015, vsol, vtok, real, tape)
            e.sim[mode] = {
                "pnl": res.all_in_pnl_sol,
                "reason": res.close_reason,
                "hold_ms": res.hold_ms,
                "post_buys": res.post_entry_buys_before_exit,
                "post_buy_sol": res.post_entry_buy_sol_before_exit,
                "confidence": conf,
            }


def candidate_replay(raw_by_mint: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    sim = simulator()
    candidates: list[dict[str, Any]] = []
    for mint, rows in raw_by_mint.items():
        last_candidate = 0
        for r in rows:
            ts = int(r.get("ts_ms") or 0)
            if r.get("side") != "buy" or ts - last_candidate < 1000:
                continue
            pre = flow_stats(rows, ts - 750, ts - 1)
            if pre["buy_sol"] < 0.50 or pre["buyers"] < 1 or pre["top_share"] > 1.0:
                continue
            last_candidate = ts
            vsol, vtok, real, conf = reconstruct_state(rows, ts)
            tape = make_tape(rows, ts - 750, ts + 3000)
            item: dict[str, Any] = {
                "mint": mint,
                "short": short_addr(mint),
                "ts_ms": ts,
                "pre_buy_sol_750": pre["buy_sol"],
                "pre_buyers_750": pre["buyers"],
                "pre_top_share_750": pre["top_share"],
                "state_confidence": conf,
            }
            for mode in MODES:
                res = sim.simulate(mode, ts, 0.015, vsol, vtok, real, tape)
                item[mode] = {
                    "pnl": res.all_in_pnl_sol,
                    "reason": res.close_reason,
                    "hold_ms": res.hold_ms,
                    "post_buys": res.post_entry_buys_before_exit,
                    "post_buy_sol": res.post_entry_buy_sol_before_exit,
                }
            candidates.append(item)
    candidates.sort(key=lambda x: int(x["ts_ms"]))
    return candidates


def filter_candidates(
    cands: list[dict[str, Any]],
    *,
    min_buy_sol_750: float,
    min_buyers_750: int,
    max_top_share_750: float,
) -> list[dict[str, Any]]:
    return [
        c for c in cands
        if float(c.get("pre_buy_sol_750") or 0.0) >= min_buy_sol_750
        and int(c.get("pre_buyers_750") or 0) >= min_buyers_750
        and float(c.get("pre_top_share_750") or 0.0) <= max_top_share_750
    ]


def mode_summary(cands: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    vals = [float(c[mode]["pnl"]) for c in cands]
    if not vals:
        return {"n": 0}
    wins = [v for v in vals if v >= 0.00060]
    nonneg = [v for v in vals if v >= 0.0]
    losses = [v for v in vals if v < 0.0]
    gross_win = sum(v for v in vals if v > 0)
    gross_loss = abs(sum(v for v in vals if v < 0))
    best20 = 0
    best20_nonneg = 0
    best20_wins = 0
    zero10 = False
    for i, c in enumerate(cands):
        start = int(c["ts_ms"])
        win = [x for x in cands if start <= int(x["ts_ms"]) <= start + 20 * 60_000]
        nn = [x for x in win if float(x[mode]["pnl"]) >= 0.0]
        ww = [x for x in win if float(x[mode]["pnl"]) >= 0.00060]
        best20 = max(best20, len(win))
        best20_nonneg = max(best20_nonneg, len(nn))
        best20_wins = max(best20_wins, len(ww))
        consec = win[:10]
        if len(consec) >= 10 and all(float(x[mode]["pnl"]) >= 0.00060 for x in consec):
            zero10 = True
    return {
        "n": len(vals),
        "wins_ge_00060": len(wins),
        "nonnegative": len(nonneg),
        "losses": len(losses),
        "net": sum(vals),
        "avg": sum(vals) / len(vals),
        "best": max(vals),
        "worst": min(vals),
        "pf": (gross_win / gross_loss) if gross_loss > 0 else math.inf,
        "median_hold": median([int(c[mode]["hold_ms"]) for c in cands]),
        "best20_candidates": best20,
        "best20_nonnegative": best20_nonneg,
        "best20_wins_ge_00060": best20_wins,
        "zero10_in_20m": zero10,
    }


def mine_zero_loss_rules(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find strict pre-entry-flow rules that satisfy zero-loss replay.

    This is intentionally simple and auditable: only pre-entry flow fields are
    mined. No dry-live same-state ESB PnL, no future best, no post-entry flow is
    used to decide entry.
    """
    rows: list[dict[str, Any]] = []
    buy_sol_grid = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 60.0, 75.0, 100.0]
    buyers_grid = [1, 2, 3, 5]
    top_grid = [1.0, 0.90, 0.75, 0.60, 0.45]
    for min_buy_sol in buy_sol_grid:
        for min_buyers in buyers_grid:
            for max_top in top_grid:
                filt = filter_candidates(
                    cands,
                    min_buy_sol_750=min_buy_sol,
                    min_buyers_750=min_buyers,
                    max_top_share_750=max_top,
                )
                if len(filt) < 10:
                    continue
                for mode in LIVE_SAFE_MODES:
                    s = mode_summary(filt, mode)
                    if (
                        int(s.get("losses", 0)) == 0
                        and int(s.get("best20_wins_ge_00060", 0)) >= 10
                        and float(s.get("net", 0.0)) > 0
                    ):
                        rows.append({
                            "mode": mode,
                            "rule_id": (
                                f"v38_preflow_scalp_buy{min_buy_sol:g}"
                                f"_buyers{min_buyers}_top{max_top:g}"
                            ),
                            "min_buy_sol_750": min_buy_sol,
                            "min_buyers_750": min_buyers,
                            "max_top_share_750": max_top,
                            **s,
                        })
    rows.sort(
        key=lambda r: (
            -int(r.get("best20_nonnegative", 0)),
            -float(r.get("net", 0.0)),
            float(r.get("min_buy_sol_750", 0.0)),
        )
    )
    return rows


def build_v38_rules(mined: list[dict[str, Any]]) -> dict[str, Any]:
    primary = mined[0] if mined else None
    if primary:
        primary = {
            k: ("inf" if isinstance(v, float) and math.isinf(v) else v)
            for k, v in primary.items()
        }
    preflow_thresholds = {
        "pre_entry_buy_sol_750_min": float(primary["min_buy_sol_750"]) if primary else 75.0,
        "pre_entry_buyer_count_750_min": int(primary["min_buyers_750"]) if primary else 1,
        "pre_entry_top_buyer_share_750_max": float(primary["max_top_share_750"]) if primary else 1.0,
        "expected_all_in_processed_min_sol": 0.00060,
        "bank_all_in_min_sol": 0.00060,
        "scratch_all_in_min_sol": 0.00005,
        "clamp_all_in_max_loss_sol": -0.00050,
        "max_hold_ms": 3000,
    }
    return {
        "version": "v38_flow_delay_scalp",
        "created_by": "pgg2_v38_flow_delay_replay.py",
        "strategy_decision": {
            "rejected_atomic_instant_esb": True,
            "rejected_protected_hold_substitute": True,
            "rejected_jito_first": True,
            "selected_strategy": "v38_flow_delay_scalp",
            "reason": "drylive_edge_is_external_flow_between_quotes",
        },
        "primary_replay_rule": primary,
        "rules": [
            {
                "rule_id": "v38_preflow_scalp",
                "actual_entry": "shadow_only_until_fresh_corrected_drylive_passes",
                "entry": {
                    "route": "pump_bc",
                    "sim_needed": False,
                    "cost_model_confidence": "proven",
                    "pair_source": ["current_sig", "cache", "prewarmed", "observed_raw_rpc"],
                    "pre_entry_external_flow": preflow_thresholds,
                    "sell_pressure_not_accelerating": True,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
                "exit": {
                    "bank_all_in_min_sol": 0.00060,
                    "scratch_all_in_min_sol": 0.00005,
                    "clamp_all_in_max_loss_sol": -0.00050,
                    "max_hold_ms": 3000,
                },
            },
            {
                "rule_id": "v38_postflow_confirm_scalp",
                "actual_entry": "dry_live_only_until_processed_mode_proves_live_safe",
                "entry": {
                    "quote_edge_required": True,
                    "post_entry_external_flow_confirmation_required": True,
                    "abort_if_confirmation_missing": True,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
            },
            {
                "rule_id": "v38_delayed_green_flow",
                "actual_entry": "shadow_only",
                "entry": {
                    "fresh_delayed_snapshots_min": 2,
                    "both_live_flow_delay_all_in_positive": True,
                    "external_buys_continuing": True,
                    "stale_quote_allowed": False,
                    "sim_needed": False,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
            },
            {
                "rule_id": "v38_recovered_flow_green",
                "actual_entry": "shadow_only",
                "entry": {
                    "source": "curve_missing_recovered_quote",
                    "recovered_quote_required": True,
                    "external_buy_flow_required": True,
                    "live_flow_delay_all_in_positive": True,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
            },
        ],
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def write_reports(runs: list[RunSpec], all_entries: list[Entry], all_candidates: list[dict[str, Any]]) -> None:
    mined = mine_zero_loss_rules(all_candidates)
    Path("v38_flow_delay_rules.json").write_text(
        json.dumps(build_v38_rules(mined), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    forensic_rows = []
    for e in all_entries:
        processed_pnl = float(e.sim.get("processed_mode", {}).get("pnl", 0.0))
        confirmed_pnl = float(e.sim.get("confirmed_mode", {}).get("pnl", 0.0))
        pre_buy_sol = float(e.pre_stats.get("buy_sol", 0.0))
        post_buy_sol = float(e.post_stats.get("buy_sol", 0.0))
        forensic_rows.append([
            e.run,
            e.short_mint,
            e.buy_quote_ts_ms,
            e.sell_quote_ts_ms,
            e.delay_ms,
            f"{e.quote_tokens:.3f}",
            f"{e.first_sell_quote_out:.6f}",
            f"{e.bank_sell_quote_out:.6f}",
            f"{e.drylive_all_in_pnl:+.6f}",
            f"{pre_buy_sol:.3f}/{e.pre_stats.get('buyers', 0)}",
            f"{post_buy_sol:.3f}/{e.post_stats.get('buyers', 0)}",
            f"{e.post_stats.get('sell_sol', 0):.3f}/{e.post_stats.get('sells', 0)}",
            f"{e.post_stats.get('top_share', 0.0):.2f}",
            "yes" if pre_buy_sol > 0 else "no",
            "yes" if post_buy_sol > 0 else "no",
            "yes" if pre_buy_sol == 0 and post_buy_sol > 0 else "no",
            "yes" if processed_pnl >= 0.00060 else "no",
            "yes" if confirmed_pnl >= 0.00060 else "no",
        ])
    Path("TIME_DELAY_EDGE_FORENSIC.md").write_text(
        "# TIME_DELAY_EDGE_FORENSIC\n\n"
        "Dry-live winners are decomposed into pre-entry observable flow and "
        "post-entry confirmation flow. Post-entry flow is only counted between "
        "the locked buy quote timestamp and the bank sell quote timestamp.\n\n"
        + md_table(
            [
                "run", "mint", "buy_quote_ts", "sell_quote_ts", "delay_ms",
                "buy_quote_tokens", "first_sell_quote_out", "bank_quote_out",
                "drylive_all_in", "pre750 buySOL/buyers", "post buySOL/buyers",
                "post sellSOL/sells", "post_top_share", "pre_flow_visible",
                "post_flow_visible", "lookahead_only_flow", "processed_caught",
                "confirmed_caught",
            ],
            forensic_rows[:80],
        )
        + "\n\nNote: if post_flow_visible is `no`, that dry-live win cannot be "
        "explained by observable external buys between the buy quote and bank "
        "quote in the raw tape; it must be treated as not proven live-flow-delay.\n",
        encoding="utf-8",
    )

    sim_rows = []
    for mode in MODES:
        s = mode_summary(all_candidates, mode)
        sim_rows.append([
            mode,
            s.get("n", 0),
            s.get("wins_ge_00060", 0),
            s.get("nonnegative", 0),
            s.get("losses", 0),
            f"{s.get('net', 0):+.6f}",
            f"{s.get('worst', 0):+.6f}",
            f"{s.get('pf', 0):.2f}" if math.isfinite(float(s.get("pf", 0))) else "inf",
            int(s.get("median_hold", 0) or 0),
            s.get("best20_wins_ge_00060", 0),
            s.get("zero10_in_20m", False),
        ])
    Path("LIVE_FLOW_DELAY_SIM.md").write_text(
        "# LIVE_FLOW_DELAY_SIM\n\n"
        "Candidate rule: pre-entry external buy flow in the prior 750ms "
        "(`buy_sol >= 0.50`, buyers >= 1), then sequential live simulation. "
        "The curve is advanced with our buy first and then observed future tape. "
        "`optimistic_ordered_mode` is research-only and not live-safe.\n\n"
        + md_table(
            [
                "mode", "N", "wins>=0.00060", "nonnegative", "losses", "net",
                "worst", "PF", "median_hold_ms", "best20_wins", "10/20 wins",
            ],
            sim_rows,
        )
        + "\n\n## Strict Preflow Miner\n\n"
        + mined_rules_table(mined)
        + "\n",
        encoding="utf-8",
    )

    remain_rows = []
    for e in all_entries:
        remain_rows.append([
            e.run,
            e.short_mint,
            f"{e.drylive_all_in_pnl:+.6f}",
            f"{e.sim.get('processed_mode', {}).get('pnl', 0):+.6f}",
            e.sim.get("processed_mode", {}).get("reason", ""),
            f"{e.sim.get('confirmed_mode', {}).get('pnl', 0):+.6f}",
            e.sim.get("confirmed_mode", {}).get("reason", ""),
            e.sim.get("processed_mode", {}).get("post_buys", 0),
            f"{e.sim.get('processed_mode', {}).get('post_buy_sol', 0):.3f}",
        ])
    Path("V38_REPLAY_ON_EXISTING_LOGS.md").write_text(
        "# V38_REPLAY_ON_EXISTING_LOGS\n\n"
        "This is the corrected v38 replay. It rejects atomic instant ESB, "
        "protected-hold substitution, and Jito-first assumptions. It uses only "
        "processed/confirmed sequential modes for live-safety decisions.\n\n"
        "## Existing Dry-Live Winners Under Live-Flow-Delay\n\n"
        + md_table(
            [
                "run", "mint", "drylive_all_in", "processed_pnl", "processed_reason",
                "confirmed_pnl", "confirmed_reason", "post_buys", "post_buy_sol",
            ],
            remain_rows[:120],
        )
        + "\n\n## Aggregate Replay\n\n"
        + md_table(
            [
                "mode", "N", "wins>=0.00060", "nonnegative", "losses", "net",
                "worst", "PF", "median_hold_ms", "best20_wins", "10/20 wins",
            ],
            sim_rows,
        )
        + "\n\n## Strict Preflow Miner\n\n"
        + mined_rules_table(mined)
        + "\n\n## Decision\n\n"
        + decision_text(all_candidates, mined)
        + "\n",
        encoding="utf-8",
    )


def mined_rules_table(mined: list[dict[str, Any]], limit: int = 12) -> str:
    if not mined:
        return "No processed/confirmed pre-entry-flow rule hit >=10 bank wins in a 20-minute window with zero losses.\n"
    rows = []
    for r in mined[:limit]:
        rows.append([
            r["mode"],
            r["rule_id"],
            r["n"],
            r["best20_wins_ge_00060"],
            r["losses"],
            f"{r['net']:+.6f}",
            f"{r['worst']:+.6f}",
            f"{r['pf']:.2f}" if math.isfinite(float(r["pf"])) else "inf",
            f"{r['min_buy_sol_750']:.1f}",
            r["min_buyers_750"],
            f"{r['max_top_share_750']:.2f}",
        ])
    return md_table(
        [
            "mode", "rule_id", "N", "best20_wins", "losses", "net",
            "worst", "PF", "min_buy_sol_750", "min_buyers_750", "max_top_share_750",
        ],
        rows,
    ) + "\n\nThese mined rules use only pre-entry observable flow. They are not a live approval; they define the v38 shadow/corrected-dry-live candidate to validate next.\n"


def decision_text(cands: list[dict[str, Any]], mined: list[dict[str, Any]]) -> str:
    lines = []
    for mode in LIVE_SAFE_MODES:
        s = mode_summary(cands, mode)
        if s.get("zero10_in_20m") and int(s.get("losses", 0)) == 0:
            lines.append(f"- {mode}: passes 10/20 zero-loss on existing logs.")
        else:
            lines.append(
                f"- {mode}: does not prove live-safe 10/20 zero-loss. "
                f"best20_wins={s.get('best20_wins_ge_00060', 0)}, "
                f"losses={s.get('losses', 0)}, worst={s.get('worst', 0):+.6f}."
            )
    if mined:
        primary = mined[0]
        lines.append(
            "- strict preflow candidate: existing-log replay finds a zero-loss "
            f"{primary['mode']} rule ({primary['rule_id']}) with "
            f"best20_wins={primary['best20_wins_ge_00060']} and "
            f"threshold pre_buy_sol_750>={primary['min_buy_sol_750']:.1f}. "
            "This is the v38 candidate for corrected dry-live; it must be fresh-validated before live."
        )
    else:
        lines.append(
            "- strict preflow candidate: none found. Existing logs do not support "
            "v38 flow-delay 10/20 zero-loss without a new live-equivalent idea."
        )
    lines.append("- optimistic_ordered_mode is research-only and cannot justify live trading.")
    return "\n".join(lines)


def default_runs(root: Path) -> list[RunSpec]:
    artifact_base = root / "remote_v38_artifacts"
    base = artifact_base if artifact_base.exists() else root
    pairs = [
        ("v33_max3", "logs/pgg2_v33_max3_pilot_20260512_031517.log", "data/pgg2_v30_shadowlab_drylive_20260512_031517_raw.jsonl"),
        ("v35_sla20", "logs/pgg2_v35_sla20_20260512_051615.log", "data/pgg2_v30_shadowlab_drylive_20260512_051615_raw.jsonl"),
        ("v36b_sla20", "logs/pgg2_v36b_sla20_20260512_060322.log", "data/pgg2_v30_shadowlab_drylive_20260512_060323_raw.jsonl"),
        ("v36c_sla20", "logs/pgg2_v36c_sla20_20260512_065335.log", "data/pgg2_v30_shadowlab_drylive_20260512_065335_raw.jsonl"),
        ("v36c2_sla20", "logs/pgg2_v36c2_sla20_20260512_070821.log", "data/pgg2_v30_shadowlab_drylive_20260512_070821_raw.jsonl"),
        ("v36c3_sla20", "logs/pgg2_v36c3_sla20_20260512_071443.log", "data/pgg2_v30_shadowlab_drylive_20260512_071443_raw.jsonl"),
        ("v38_100302", "logs/pgg2_v38_drylive_20260512_100302.log", "data/pgg2_v38_drylive_20260512_100302_raw.jsonl"),
    ]
    return [RunSpec(n, base / l, base / r) for n, l, r in pairs if (base / l).exists() and (base / r).exists()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="repo root containing remote_v38_artifacts")
    args = ap.parse_args()
    root = Path(args.root)
    runs = default_runs(root)
    all_entries: list[Entry] = []
    all_candidates: list[dict[str, Any]] = []
    for run in runs:
        raw_by_mint, short_map = load_raw(run.raw)
        entries = parse_entries(run, short_map)
        enrich_entries(entries, raw_by_mint)
        all_entries.extend(entries)
        all_candidates.extend(candidate_replay(raw_by_mint))
    write_reports(runs, all_entries, all_candidates)
    print(f"runs={len(runs)} entries={len(all_entries)} candidates={len(all_candidates)}")
    for mode in ("processed_mode", "confirmed_mode", "optimistic_ordered_mode"):
        print(mode, mode_summary(all_candidates, mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
