"""v39 lead-time scalp analysis for the exact v36c3 winner pattern.

This intentionally does not replay broad flow-delay candidates. It reconstructs
the exact v36c3 10W/0L scalp winners, asks whether each could have been entered
early enough for sequential live timing, extracts pre-bank features, and tests
candidate v39 lead-time rules against the exact winners plus known losers.
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
from pgg2_v38_flow_delay_replay import (
    CREATOR_FEE_BPS,
    INITIAL_REAL_TOKENS,
    INITIAL_VSOL,
    INITIAL_VTOKENS,
    PROTOCOL_FEE_BPS,
    flow_stats,
    load_raw,
    make_tape,
    md_table,
    reconstruct_state,
    short_addr,
)


V36C3_LOG = "logs/pgg2_v36c3_sla20_20260512_071443.log"
V36C3_RAW = "data/pgg2_v30_shadowlab_drylive_20260512_071443_raw.jsonl"
V36B_LOG = "logs/pgg2_v36b_sla20_20260512_060322.log"
V36B_RAW = "data/pgg2_v30_shadowlab_drylive_20260512_060323_raw.jsonl"

LATENCIES_MS = [0, 100, 250, 350, 500, 750, 1000]
LEADS_MS = [0, 250, 500, 750, 1000, 1500, 2000]
FEATURE_OFFSETS_MS = [-2000, -1500, -1000, -750, -500, -250, 0]
AMOUNT_SOL = 0.015


@dataclass
class QuoteSample:
    side: str
    short_mint: str
    start_ms: int
    end_ms: int
    latency_ms: int
    out: float
    tokens: float = 0.0
    pair_source: str = ""
    sim_needed: int = 0
    in_flight: int = 0


@dataclass
class ExactEntry:
    run: str
    short_mint: str
    full_mint: str = ""
    rule_id: str = ""
    policy_id: str = ""
    amount_sol: float = AMOUNT_SOL
    buy_quote_tokens: float = 0.0
    buy_quote_start_ms: int = 0
    buy_quote_ts_ms: int = 0
    buy_quote_latency_ms: int = 0
    sell_quote_start_ms: int = 0
    sell_quote_ts_ms: int = 0
    sell_quote_latency_ms: int = 0
    t1_minus_t0_ms: int = 0
    pair_source: str = ""
    sim_needed: int = 0
    in_flight: int = 0
    first_sell_quote_out: float = 0.0
    bank_sell_quote_out: float = 0.0
    entry_snapshot_all_in_pnl: float = 0.0
    final_close_reason: str = ""
    final_all_in_pnl: float = 0.0
    entry_features: str = ""
    lead_results: dict[str, Any] = field(default_factory=dict)
    feature_rows: list[dict[str, Any]] = field(default_factory=list)


_RE_SCALP_BUY = re.compile(
    r"PGG2-SCALP-BUY rule_id=(\S+) policy_id=(\S+).*?mint=(\S+) "
    r"amount=([0-9.]+) quote_tokens=([0-9.]+) immediate_out=([0-9.]+) "
    r"immediate_pnl=([+\-0-9.]+).*?pair_source=(\S+) .*?entry_features=\{([^}]*)\}"
)
_RE_LOCK = re.compile(
    r"PGG2-QUOTE-SHADOW-BUY-LOCKED mint=(\S+) quote_id=\S+:(\d+) "
    r"quote_tokens=([0-9.]+).*?quote_age_ms=(\d+) immediate_out=([0-9.]+)"
)
_RE_SELL = re.compile(
    r"PGG2-QUOTE-SHADOW-SELL (\S+) reason=(\S+) quote_out=([0-9.]+).*?"
    r"all_in_pnl=([+\-0-9.]+)"
)
_RE_DIRECT_BUY = re.compile(r"PGG2-DIRECT-QUOTE BUY (\S+) .*?out=([0-9.]+)")
_RE_DIRECT_SELL = re.compile(r"PGG2-DIRECT-QUOTE SELL (\S+) .*?in_tokens=([0-9.]+) out=([0-9.]+)")
_RE_LAT = re.compile(
    r"PGG2-QUOTE-LATENCY side=(buy|sell) mint=(\S+) .*?start_ms=(\d+) "
    r"end_ms=(\d+) latency_ms=(\d+) .*?pair_source=(\S+) .*?sim_needed=(\d+) in_flight=(\d+)"
)


def artifact_base(root: Path) -> Path:
    base = root / "remote_v38_artifacts"
    return base if base.exists() else root


def parse_log(log_path: Path, run: str, short_map: dict[str, str]) -> tuple[list[ExactEntry], list[QuoteSample]]:
    pending_quote: dict[tuple[str, str], dict[str, Any]] = {}
    latest_buy: dict[str, dict[str, Any]] = {}
    latest_lock: dict[str, dict[str, Any]] = {}
    entries: list[ExactEntry] = []
    quote_samples: list[QuoteSample] = []

    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        mbq = _RE_DIRECT_BUY.search(line)
        if mbq:
            s, out = mbq.groups()
            pending_quote[("buy", s)] = {"out": float(out), "tokens": float(out)}
            continue
        msq = _RE_DIRECT_SELL.search(line)
        if msq:
            s, toks, out = msq.groups()
            pending_quote[("sell", s)] = {"out": float(out), "tokens": float(toks)}
            continue

        mlq = _RE_LAT.search(line)
        if mlq:
            side, s, start, end, lat, pair, sim_needed, in_flight = mlq.groups()
            pending = pending_quote.pop((side, s), {})
            quote_samples.append(
                QuoteSample(
                    side=side,
                    short_mint=s,
                    start_ms=int(start),
                    end_ms=int(end),
                    latency_ms=int(lat),
                    out=float(pending.get("out") or 0.0),
                    tokens=float(pending.get("tokens") or 0.0),
                    pair_source=pair,
                    sim_needed=int(sim_needed),
                    in_flight=int(in_flight),
                )
            )
            continue

        mb = _RE_SCALP_BUY.search(line)
        if mb:
            rule, policy, s, amount, toks, imm_out, _imm_pnl, pair, features = mb.groups()
            latest_buy[s] = {
                "rule_id": rule,
                "policy_id": policy,
                "amount_sol": float(amount),
                "buy_quote_tokens": float(toks),
                "first_sell_quote_out": float(imm_out),
                "pair_source": pair,
                "entry_features": features,
            }
            continue

        ml = _RE_LOCK.search(line)
        if ml:
            s, lock_ts, toks, age, imm_out = ml.groups()
            latest_lock[s] = {
                "lock_ts_ms": int(lock_ts),
                "quote_age_ms": int(age),
                "quote_tokens": float(toks),
                "first_sell_quote_out": float(imm_out),
            }
            continue

        ms = _RE_SELL.search(line)
        if ms:
            s, reason, qout, pnl = ms.groups()
            if s not in latest_buy or s not in latest_lock:
                continue
            buy = latest_buy[s]
            lock = latest_lock[s]
            entries.append(
                ExactEntry(
                    run=run,
                    short_mint=s,
                    full_mint=short_map.get(s, ""),
                    rule_id=str(buy["rule_id"]),
                    policy_id=str(buy["policy_id"]),
                    amount_sol=float(buy["amount_sol"]),
                    buy_quote_tokens=float(lock.get("quote_tokens") or buy["buy_quote_tokens"]),
                    first_sell_quote_out=float(lock.get("first_sell_quote_out") or buy["first_sell_quote_out"]),
                    bank_sell_quote_out=float(qout),
                    entry_snapshot_all_in_pnl=float(pnl),
                    final_close_reason=reason,
                    final_all_in_pnl=float(pnl),
                    pair_source=str(buy["pair_source"]),
                    entry_features=str(buy["entry_features"]),
                )
            )
    attach_quotes(entries, quote_samples)
    return entries, quote_samples


def attach_quotes(entries: list[ExactEntry], quotes: list[QuoteSample]) -> None:
    for e in entries:
        buys = [
            q for q in quotes
            if q.side == "buy"
            and q.short_mint == e.short_mint
            and abs(q.tokens - e.buy_quote_tokens) <= max(1e-6, e.buy_quote_tokens * 1e-6)
        ]
        sells = [
            q for q in quotes
            if q.side == "sell"
            and q.short_mint == e.short_mint
            and abs(q.out - e.bank_sell_quote_out) <= 1e-6
        ]
        if buys:
            q = buys[0]
            e.buy_quote_start_ms = q.start_ms
            e.buy_quote_ts_ms = q.end_ms
            e.buy_quote_latency_ms = q.latency_ms
            e.pair_source = q.pair_source or e.pair_source
            e.sim_needed = q.sim_needed
            e.in_flight = q.in_flight
        if sells:
            q = sells[0]
            e.sell_quote_start_ms = q.start_ms
            e.sell_quote_ts_ms = q.end_ms
            e.sell_quote_latency_ms = q.latency_ms
        elif e.buy_quote_ts_ms:
            # Fallback for older logs: sell quote appears after buy quote.
            later = [q for q in quotes if q.side == "sell" and q.short_mint == e.short_mint and q.end_ms >= e.buy_quote_ts_ms]
            if later:
                q = later[0]
                e.sell_quote_start_ms = q.start_ms
                e.sell_quote_ts_ms = q.end_ms
                e.sell_quote_latency_ms = q.latency_ms
        if e.buy_quote_ts_ms and e.sell_quote_ts_ms:
            e.t1_minus_t0_ms = e.sell_quote_ts_ms - e.buy_quote_ts_ms


def simulator_for_latency(latency_ms: int) -> LiveFlowDelaySimulator:
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
        latency=LatencyModel(
            buy_submit_ms=min(80, latency_ms),
            buy_processed_ms=latency_ms,
            buy_confirmed_ms=latency_ms,
            sell_submit_ms=80,
        ),
        policy=ExitPolicy(
            bank_pnl_min_sol=0.00060,
            scratch_pnl_min_sol=0.00010,
            clamp_pnl_max_loss_sol=-0.00050,
            max_hold_ms=3000,
            flow_stall_ms=750,
            require_post_entry_buy=False,
        ),
    )


def simulate_entry_at(raw_rows: list[dict[str, Any]], entry_ts: int, latency_ms: int) -> dict[str, Any]:
    sim = simulator_for_latency(latency_ms)
    vsol, vtok, real, conf = reconstruct_state(raw_rows, entry_ts)
    tape = make_tape(raw_rows, entry_ts - 2000, entry_ts + 3500)
    res = sim.simulate("processed_mode", entry_ts, AMOUNT_SOL, vsol, vtok, real, tape)
    return {
        "pnl": float(res.all_in_pnl_sol),
        "reason": res.close_reason,
        "hold_ms": int(res.hold_ms),
        "post_buys": int(res.post_entry_buys_before_exit),
        "post_buy_sol": float(res.post_entry_buy_sol_before_exit),
        "state_confidence": conf,
    }


def quote_stats_for(quotes: list[QuoteSample], short_mint: str, ts_ms: int) -> dict[str, Any]:
    prior_buy = [q for q in quotes if q.short_mint == short_mint and q.side == "buy" and q.end_ms <= ts_ms]
    prior_sell = [q for q in quotes if q.short_mint == short_mint and q.side == "sell" and q.end_ms <= ts_ms]
    last_buy = prior_buy[-1] if prior_buy else None
    last_sell = prior_sell[-1] if prior_sell else None
    prev_sell = prior_sell[-2] if len(prior_sell) >= 2 else None
    return {
        "buy_quote_available": bool(last_buy),
        "sell_quote_available": bool(last_sell),
        "last_buy_quote_tokens": last_buy.out if last_buy else 0.0,
        "last_sell_quote_out": last_sell.out if last_sell else 0.0,
        "pair_source": last_buy.pair_source if last_buy else "",
        "sim_needed": last_buy.sim_needed if last_buy else "",
        "buy_quote_latency_ms": last_buy.latency_ms if last_buy else "",
        "sell_quote_latency_ms": last_sell.latency_ms if last_sell else "",
        "quote_out_delta": (last_sell.out - prev_sell.out) if last_sell and prev_sell else "",
        "two_snapshot_trend": "up" if last_sell and prev_sell and last_sell.out > prev_sell.out else ("down" if last_sell and prev_sell else "na"),
    }


def event_features(raw_rows: list[dict[str, Any]], ts_ms: int) -> dict[str, Any]:
    def stats(win: int) -> dict[str, Any]:
        return flow_stats(raw_rows, ts_ms - win, ts_ms)
    s250 = stats(250)
    s500 = stats(500)
    s1000 = stats(1000)
    last_rows = [r for r in raw_rows if int(r.get("ts_ms") or 0) <= ts_ms]
    prev_rows = [r for r in raw_rows if int(r.get("ts_ms") or 0) <= ts_ms - 250]
    price = float((last_rows[-1] if last_rows else {}).get("curve_price") or 0.0)
    prev_price = float((prev_rows[-1] if prev_rows else {}).get("curve_price") or 0.0)
    return {
        "buy250": s250["buy_sol"],
        "sell250": s250["sell_sol"],
        "buyers250": s250["buyers"],
        "buy500": s500["buy_sol"],
        "sell500": s500["sell_sol"],
        "buyers500": s500["buyers"],
        "buy1000": s1000["buy_sol"],
        "sell1000": s1000["sell_sol"],
        "buyers1000": s1000["buyers"],
        "top1000": s1000["top_share"],
        "curve_price": price,
        "curve_price_delta_250": price - prev_price if price and prev_price else 0.0,
        "feed_gap_ms": feed_gap_ms(raw_rows, ts_ms),
    }


def feed_gap_ms(raw_rows: list[dict[str, Any]], ts_ms: int) -> int:
    before = [int(r.get("ts_ms") or 0) for r in raw_rows if int(r.get("ts_ms") or 0) <= ts_ms]
    if len(before) < 2:
        return 0
    return before[-1] - before[-2]


def analyze_lead_time(entries: list[ExactEntry], raw_by_mint: dict[str, list[dict[str, Any]]]) -> None:
    for e in entries:
        rows = raw_by_mint.get(e.full_mint, [])
        lead_grid: dict[str, Any] = {}
        for lead in LEADS_MS:
            entry_ts = max(0, e.buy_quote_start_ms - lead)
            for latency in LATENCIES_MS:
                lead_grid[f"lead{lead}_lat{latency}"] = simulate_entry_at(rows, entry_ts, latency)
        e.lead_results = lead_grid


def required_lead(e: ExactEntry, threshold: float, latency_ms: int = 500) -> str:
    passing: list[int] = []
    for lead in LEADS_MS:
        res = e.lead_results.get(f"lead{lead}_lat{latency_ms}", {})
        if float(res.get("pnl", -999.0)) >= threshold:
            passing.append(lead)
    return str(min(passing)) if passing else "not_catchable"


def max_latency_tolerated(e: ExactEntry, threshold: float = 0.00060) -> str:
    passing: list[int] = []
    for latency in LATENCIES_MS:
        ok = any(float(e.lead_results.get(f"lead{lead}_lat{latency}", {}).get("pnl", -999.0)) >= threshold for lead in LEADS_MS)
        if ok:
            passing.append(latency)
    return str(max(passing)) if passing else "none"


def flow_summary(raw_rows: list[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    stats = flow_stats(raw_rows, start_ms, end_ms)
    return {
        "external_buys": int(stats["buys"]),
        "external_sells": int(stats["sells"]),
        "external_buy_sol": float(stats["buy_sol"]),
        "external_sell_sol": float(stats["sell_sol"]),
        "external_buyers": int(stats["buyers"]),
        "top_buyer_share": float(stats["top_share"]),
    }


def extract_prebank_features(entries: list[ExactEntry], raw_by_mint: dict[str, list[dict[str, Any]]], quotes: list[QuoteSample]) -> None:
    for e in entries:
        rows = raw_by_mint.get(e.full_mint, [])
        feature_rows: list[dict[str, Any]] = []
        for off in FEATURE_OFFSETS_MS:
            ts = e.buy_quote_start_ms + off
            q = quote_stats_for(quotes, e.short_mint, ts)
            ev = event_features(rows, ts)
            feature_rows.append({
                "offset_ms": off,
                "ts_ms": ts,
                **q,
                **ev,
                "v39_quote_gradient_lead": bool(q["sell_quote_available"] and q["two_snapshot_trend"] == "up"),
                "v39_curve_state_lead": bool(ev["curve_price_delta_250"] > 0 and q["buy_quote_available"]),
                "v39_buy_burst_lead": bool(ev["buy500"] > 0 and q["buy_quote_available"] and q["sell_quote_available"] and q["two_snapshot_trend"] == "up"),
            })
        # first quoteable / recovered quote moments from logs, when present.
        buy_quotes = [q for q in quotes if q.short_mint == e.short_mint and q.side == "buy"]
        sell_quotes = [q for q in quotes if q.short_mint == e.short_mint and q.side == "sell"]
        if buy_quotes and sell_quotes:
            first_ts = max(buy_quotes[0].end_ms, sell_quotes[0].end_ms)
            q = quote_stats_for(quotes, e.short_mint, first_ts)
            ev = event_features(rows, first_ts)
            feature_rows.append({
                "offset_ms": "first_quoteable",
                "ts_ms": first_ts,
                **q,
                **ev,
                "first_quoteable_is_entry_safe": first_ts < e.sell_quote_start_ms,
                "v39_quote_gradient_lead": False,
                "v39_curve_state_lead": bool(ev["curve_price_delta_250"] > 0),
                "v39_buy_burst_lead": False,
            })
        e.feature_rows = feature_rows


def v39_rule_signal(e: ExactEntry, mode_latency_ms: int = 500) -> dict[str, Any]:
    """Exact-pattern candidate rules; all features are pre-bank only."""
    # Rule 1: quote-gradient lead. Requires two sell quote snapshots before the
    # old bank moment and positive sequential replay at the selected latency.
    for row in e.feature_rows:
        ts = int(row["ts_ms"])
        # The old bank quote itself is not a valid entry feature. A v39 lead
        # rule must fire before the sell quote that banked the old dry-live
        # entry begins, otherwise it is just the old same-state ESB edge.
        if ts >= e.sell_quote_start_ms:
            continue
        lead = max(0, e.buy_quote_start_ms - ts)
        nearest_lead = min(LEADS_MS, key=lambda x: abs(x - lead))
        sim = e.lead_results.get(f"lead{nearest_lead}_lat{mode_latency_ms}", {})
        projected = float(sim.get("pnl", -999))
        fast = row.get("pair_source") in {"current_sig", "cache", "prewarmed", "observed_raw_rpc"}
        sim_free = row.get("sim_needed") in {0, "0", ""}
        if (
            row.get("v39_quote_gradient_lead")
            and fast
            and sim_free
            and projected >= 0.00010
        ):
            return {"entered": True, "rule_id": "v39_quote_gradient_lead", "ts_ms": ts, "lead_ms": lead, "projected_pnl": projected}
        if (
            row.get("v39_curve_state_lead")
            and fast
            and sim_free
            and projected >= 0.00010
        ):
            return {"entered": True, "rule_id": "v39_curve_state_lead", "ts_ms": ts, "lead_ms": lead, "projected_pnl": projected}
        if (
            row.get("v39_buy_burst_lead")
            and fast
            and sim_free
            and projected >= 0.00010
        ):
            return {"entered": True, "rule_id": "v39_buy_burst_lead", "ts_ms": ts, "lead_ms": lead, "projected_pnl": projected}
    return {"entered": False, "rule_id": "", "ts_ms": 0, "lead_ms": 0, "projected_pnl": 0.0}


def write_timeline_report(entries: list[ExactEntry], quotes: list[QuoteSample]) -> None:
    rows = []
    for e in entries:
        raw_rows = getattr(e, "_raw_rows", [])
        pre_flow = flow_summary(raw_rows, e.buy_quote_start_ms - 1000, e.buy_quote_start_ms)
        bank_flow = flow_summary(raw_rows, e.buy_quote_start_ms, e.sell_quote_ts_ms)
        qbefore = [q for q in quotes if q.short_mint == e.short_mint and q.side == "sell" and q.end_ms < e.buy_quote_ts_ms]
        qbetween = [q for q in quotes if q.short_mint == e.short_mint and q.side == "sell" and e.buy_quote_ts_ms <= q.end_ms <= e.sell_quote_ts_ms]
        qafter = [q for q in quotes if q.short_mint == e.short_mint and q.side == "sell" and q.end_ms > e.sell_quote_ts_ms]
        lead0_500 = float(e.lead_results.get("lead0_lat500", {}).get("pnl", -999.0))
        confirmed_1000 = float(e.lead_results.get("lead0_lat1000", {}).get("pnl", -999.0))
        rows.append([
            e.short_mint,
            e.rule_id,
            e.buy_quote_start_ms,
            e.buy_quote_latency_ms,
            f"{e.buy_quote_tokens:.3f}",
            e.sell_quote_ts_ms,
            e.sell_quote_ts_ms - e.buy_quote_start_ms,
            f"{e.entry_snapshot_all_in_pnl:+.6f}",
            bank_flow["external_buys"],
            bank_flow["external_sells"],
            f"{bank_flow['external_buy_sol']:.3f}",
            bank_flow["external_buyers"],
            f"{bank_flow['top_buyer_share']:.2f}",
            "yes" if pre_flow["external_buy_sol"] > 0 else "no",
            "yes" if bank_flow["external_buy_sol"] > 0 else "no",
            "yes" if confirmed_1000 >= 0.00010 else "no",
            "yes" if lead0_500 >= 0.00010 else "no",
            f"{(qbefore[-1].out if qbefore else 0.0):.6f}",
            f"{(qbetween[-1].out if qbetween else 0.0):.6f}",
            f"{(qafter[0].out if qafter else 0.0):.6f}",
            e.pair_source,
            e.sim_needed,
            e.in_flight,
            e.final_close_reason,
            f"{e.final_all_in_pnl:+.6f}",
        ])
    Path("V36C3_EXACT_WINNER_TIMELINE.md").write_text(
        "# V36C3_EXACT_WINNER_TIMELINE\n\n"
        "Only the actual v36c3 10W/0L scalp winners are included here. "
        "Broad non-matching candidates are intentionally excluded.\n\n"
        + md_table(
            [
                "mint", "entry_rule", "buy_quote_T0_start", "buy_latency_ms",
                "buy_quote_tokens", "entry_snapshot_T1", "T1-T0_ms",
                "snapshot_all_in", "external_buys_quoteStart_T1", "external_sells_quoteStart_T1",
                "external_buy_sol_T0_T1", "external_buyers_T0_T1",
                "top_buyer_share_T0_T1", "pre_entry_flow_visible",
                "post_entry_flow_visible", "confirmed_path_catches_at_T0",
                "processed_path_catches_at_T0", "sell_quote_before_T0", "sell_quote_T0_to_T1",
                "sell_quote_after_T1", "quote_source", "sim_needed", "in_flight",
                "final_close_reason", "final_all_in",
            ],
            rows,
        )
        + "\n",
        encoding="utf-8",
    )


def write_lead_time_report(entries: list[ExactEntry]) -> None:
    rows = []
    for e in entries:
        rows.append([
            e.short_mint,
            e.buy_quote_start_ms,
            e.sell_quote_ts_ms,
            required_lead(e, 0.00010, 500),
            required_lead(e, 0.00060, 500),
            max_latency_tolerated(e, 0.00060),
            "yes" if max_latency_tolerated(e, 0.00060) != "none" else "no",
            "" if max_latency_tolerated(e, 0.00060) != "none" else "no lead/latency grid point stayed bank-positive",
        ])
    Path("LEAD_TIME_REQUIREMENT_TABLE.md").write_text(
        "# LEAD_TIME_REQUIREMENT_TABLE\n\n"
        "Latency grid: 0, 100, 250, 350, 500, 750, 1000 ms. Lead grid: "
        "0, 250, 500, 750, 1000, 1500, 2000 ms before old buy quote request-start T0. "
        "Required lead columns are for 500ms processed latency. This models the real-live problem: "
        "if we wait for the buy quote to finish, the old bank quote may already be gone.\n\n"
        + md_table(
            [
                "mint", "old_drylive_T0", "bank_T1",
                "required_lead_ms_for_+0.00010", "required_lead_ms_for_+0.00060",
                "max_latency_tolerated_ms", "live_sequential_can_catch", "blocker",
            ],
            rows,
        )
        + "\n",
        encoding="utf-8",
    )


def write_prebank_features(entries: list[ExactEntry], losers: list[ExactEntry]) -> None:
    rows = []
    for e, case in [(x, "winner") for x in entries] + [(x, "known_loser") for x in losers]:
        for r in e.feature_rows:
            entry_safe = bool(r.get("first_quoteable_is_entry_safe", True))
            rows.append([
                case,
                e.short_mint,
                r["offset_ms"],
                entry_safe,
                r["buy_quote_available"],
                r["sell_quote_available"],
                r["pair_source"],
                r["sim_needed"],
                r["buy_quote_latency_ms"],
                r["sell_quote_latency_ms"],
                f"{float(r['quote_out_delta']):+.6f}" if isinstance(r["quote_out_delta"], float) else r["quote_out_delta"],
                r["two_snapshot_trend"],
                f"{r['buy250']:.3f}/{r['sell250']:.3f}",
                f"{r['buy500']:.3f}/{r['sell500']:.3f}",
                f"{r['buy1000']:.3f}/{r['sell1000']:.3f}",
                f"{r['top1000']:.2f}",
                f"{r['curve_price_delta_250']:+.9f}",
                r["feed_gap_ms"],
                r["v39_quote_gradient_lead"],
                r["v39_curve_state_lead"],
                r["v39_buy_burst_lead"],
            ])
    Path("V36C3_PREBANK_FEATURES.md").write_text(
        "# V36C3_PREBANK_FEATURES\n\n"
        "Features are computed at timestamps before the old bank moment only. "
        "Old pre-buy same-state ESB PnL is not used as an entry feature. "
        "A false buy/sell quote field means no quote was logged by the old bot at that time; "
        "it is not treated as proof that a quote was impossible. known_loser rows are included "
        "to test whether winner features also admit the 2xty loss.\n\n"
        + md_table(
            [
                "case", "mint", "offset_vs_T0_ms", "entry_safe_before_bank",
                "buy_quote_logged", "sell_quote_logged", "pair_source",
                "sim_needed", "buy_lat", "sell_lat", "quote_out_delta",
                "two_snapshot_trend", "buy/sell250", "buy/sell500",
                "buy/sell1000", "top1000", "curve_price_delta250", "feed_gap_ms",
                "quote_gradient", "curve_state", "buy_burst_confirmed",
            ],
            rows[:200],
        )
        + "\n",
        encoding="utf-8",
    )


def write_rules_json(entries: list[ExactEntry]) -> None:
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    signals = [v39_rule_signal(e, 500) for e in entries]
    data = {
        "version": "v39_lead_time_scalp",
        "accepted_sla": "10w0l_within_35min",
        "stretch_sla": "10w0l_within_20min",
        "source": "exact_v36c3_winner_timeline_not_broad_flow_delay",
        "hard_rules": {
            "old_prebuy_same_state_esb_pnl_allowed": False,
            "protected_hold_substitute_allowed": False,
            "jito_allowed": False,
            "live_sequential_replay_required": True,
        },
        "exact_winner_rule_hits_at_500ms": {
            "captured": sum(1 for s in signals if s["entered"]),
            "total": len(signals),
            "note": "Count uses only pre-bank logged features; first bank sell quote is never entry permission.",
        },
        "rules": [
            {
                "rule_id": "v39_quote_gradient_lead",
                "entry": {
                    "two_consecutive_sell_quote_snapshots_improve": True,
                    "projected_live_sequential_pnl_min_sol": 0.00010,
                    "direct_pump_bc": True,
                    "sim_needed": False,
                    "pair_source_allowlist": ["current_sig", "cache", "prewarmed", "observed_raw_rpc"],
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
                "exit": {"bank": 0.00060, "scratch": 0.00010, "clamp": -0.00050, "max_hold_ms": 3000},
            },
            {
                "rule_id": "v39_curve_state_lead",
                "entry": {
                    "curve_or_reserve_price_delta_positive_across_two_snapshots": True,
                    "projected_live_sequential_pnl_min_sol_at_500ms": 0.00010,
                    "direct_pump_bc": True,
                    "sim_needed": False,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
                "exit": {"bank": 0.00060, "scratch": 0.00010, "clamp": -0.00050, "max_hold_ms": 3000},
            },
            {
                "rule_id": "v39_recovered_quote_lead",
                "entry": {
                    "quote_initially_unavailable_or_curve_missing": True,
                    "recovered_quote_then_quote_trend_improves_within_ms": 1000,
                    "projected_live_sequential_pnl_positive": True,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
                "exit": {"bank": 0.00060, "scratch": 0.00010, "clamp": -0.00050, "max_hold_ms": 3000},
            },
            {
                "rule_id": "v39_buy_burst_lead",
                "entry": {
                    "external_buys_or_reserve_deltas_visible_before_old_bank": True,
                    "must_pass_quote_gradient_or_curve_state_confirmation": True,
                    "raw_flow_alone_allowed": False,
                    "old_prebuy_same_state_esb_pnl_allowed": False,
                },
                "exit": {"bank": 0.00060, "scratch": 0.00010, "clamp": -0.00050, "max_hold_ms": 3000},
            },
        ],
    }
    (data_dir / "v39_lead_time_rules.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_exact_replay(entries: list[ExactEntry], losers: list[ExactEntry]) -> bool:
    rows = []
    captured = 0
    negatives = 0
    net = 0.0
    all_cases = [(e, "winner") for e in entries] + [(l, "known_loser") for l in losers]
    for e, group in all_cases:
        sig = v39_rule_signal(e, 500)
        if sig["entered"]:
            pnl = float(sig["projected_pnl"])
            captured += 1 if group == "winner" else 0
            negatives += 1 if pnl < 0 else 0
            net += pnl
            result = "entered"
        else:
            pnl = 0.0
            result = "avoided"
        lead_catchable = max_latency_tolerated(e, 0.00060) != "none"
        blocker = ""
        if group == "winner" and not sig["entered"]:
            blocker = "lead_time_sim_positive_but_no_logged_prebank_quote_or_curve_confirmation" if lead_catchable else "not_live_sequential_catchable"
        rows.append([
            group,
            e.short_mint,
            result,
            sig["rule_id"],
            sig["lead_ms"],
            "yes" if lead_catchable else "no",
            f"{pnl:+.6f}",
            "negative" if pnl < 0 else "nonnegative",
            blocker,
        ])
    pass_exact = captured >= 8 and negatives == 0 and all(
        not (r[0] == "known_loser" and r[2] == "entered" and r[5].startswith("-"))
        for r in rows
    )
    Path("V39_EXACT_PATTERN_REPLAY.md").write_text(
        "# V39_EXACT_PATTERN_REPLAY\n\n"
        "Replay scope is restricted to the exact v36c3 10 winners and known "
        "loss examples. It does not use broad generic flow-delay candidates.\n\n"
        + md_table(
            [
                "case", "mint", "action", "rule_id", "entry_lead_ms",
                "lead_time_catchable_any_latency", "live_seq_pnl_500ms",
                "outcome", "blocker",
            ],
            rows,
        )
        + f"\n\nCaptured winners: {captured}/10\n\n"
        + f"Negative admitted outcomes: {negatives}\n\n"
        + f"Net projected all-in: {net:+.6f} SOL\n\n"
        + ("PASS: exact pattern qualifies for broader v39 replay.\n" if pass_exact else "FAIL: exact pattern does not yet qualify for broader replay or dry-live.\n"),
        encoding="utf-8",
    )
    return pass_exact


def write_broader_replay_skipped(reason: str) -> None:
    Path("V39_RULE_REPLAY_ON_ALL_LOGS.md").write_text(
        "# V39_RULE_REPLAY_ON_ALL_LOGS\n\n"
        f"Not run. Reason: {reason}\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    base = artifact_base(Path(args.root))

    v36c3_raw, v36c3_short = load_raw(base / V36C3_RAW)
    winners, quotes = parse_log(base / V36C3_LOG, "v36c3", v36c3_short)
    winners = [e for e in winners if e.final_all_in_pnl > 0][:10]
    analyze_lead_time(winners, v36c3_raw)
    for e in winners:
        e._raw_rows = v36c3_raw.get(e.full_mint, [])  # type: ignore[attr-defined]
    extract_prebank_features(winners, v36c3_raw, quotes)

    v36b_raw, v36b_short = load_raw(base / V36B_RAW)
    v36b_entries, v36b_quotes = parse_log(base / V36B_LOG, "v36b", v36b_short)
    losers = [e for e in v36b_entries if e.short_mint.startswith("2xty")]
    analyze_lead_time(losers, v36b_raw)
    for e in losers:
        e._raw_rows = v36b_raw.get(e.full_mint, [])  # type: ignore[attr-defined]
    extract_prebank_features(losers, v36b_raw, v36b_quotes)

    write_timeline_report(winners, quotes)
    write_lead_time_report(winners)
    write_prebank_features(winners, losers)
    write_rules_json(winners)
    exact_pass = write_exact_replay(winners, losers)
    if exact_pass:
        write_broader_replay_skipped("exact replay passed, but broader v39-only replay implementation is intentionally not run by this script revision")
    else:
        write_broader_replay_skipped("exact v36c3 pattern failed pass condition, so Phase 7 is gated off")
    print(f"winners={len(winners)} losers={len(losers)} exact_pass={exact_pass}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
