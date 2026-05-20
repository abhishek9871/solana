from __future__ import annotations

import argparse
import glob
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
QUOTE_RE = re.compile(
    r"PGG2-QUOTE-LATENCY side=(buy|sell) mint=(\S+) route=(\S+) source=(\S+) .*?"
    r"start_ms=(\d+) end_ms=(\d+) latency_ms=(\d+) success=(\d+) .*?"
    r"pair_source=(\S*) .*?sim_needed=(\d+) in_flight=(\d+)"
)
SNAP_RE = re.compile(
    r"PGG2-V39-LEAD-SNAPSHOT mint=(\S+) delay_ms=(\d+) "
    r"buy_tokens=([0-9.]+) sell_out=([0-9.]+) "
    r"buy_lat_ms=(\d+) sell_lat_ms=(\d+) route=(\S+) "
    r"pair_source=(\S+) sim_needed=(\d+) .*?"
    r"quote_gradient=([+\-0-9.]+) curve_gradient=([+\-0-9.]+) .*?"
    r"processed_live_equiv_all_in_pnl=([+\-0-9.]+) "
    r"confirmed_live_equiv_all_in_pnl=([+\-0-9.]+)"
)
BLOCK_RE = re.compile(
    r"PGG2-V39-ENTRY-BLOCK mint=(\S+) reason=(\S+) snapshots_used=(\d+) "
    r"processed_live_equiv_all_in_pnl=([+\-0-9.]+) "
    r"same_state_all_in_pnl=([+\-0-9.]+) "
    r"quote_gradient=([+\-0-9.]+) pair_source=(\S+) sim_needed=(\d+) sell_lat_ms=(\d+)"
)
BUY_RE = re.compile(
    r"PGG2-V39-DRYLIVE-BUY rule_id=(\S+) mint=(\S+) "
    r"processed_live_equiv_all_in_pnl=([+\-0-9.]+).*?snapshots_used=(\d+) "
    r"lead_time_ms=(\d+) pair_source=(\S+) sim_needed=(\d+) route=(\S+)"
)
SELL_RE = re.compile(
    r"PGG2-V39-DRYLIVE-SELL reason=(\S+) mint=(\S+) "
    r"live_equiv_all_in_pnl=([+\-0-9.]+).*?hold_ms=(\d+).*?"
    r"rule_id=(\S+) entries=(\d+) closes=(\d+) net=([+\-0-9.]+)"
)


FAST_PAIR_SOURCES = {"current_sig", "cache", "prewarmed", "observed_raw_rpc"}


@dataclass
class QuoteEvent:
    side: str
    mint: str
    route: str
    source: str
    start_ms: int
    end_ms: int
    latency_ms: int
    success: bool
    pair_source: str
    sim_needed: bool
    in_flight: int


@dataclass
class Snapshot:
    mint: str
    line_no: int
    log_ts: str
    delay_ms: int
    buy_tokens: float
    sell_out: float
    buy_latency_ms: int
    sell_latency_ms: int
    route: str
    pair_source: str
    sim_needed: bool
    quote_gradient: float
    curve_gradient: float
    processed_pnl: float
    confirmed_pnl: float
    quote: QuoteEvent | None
    prior_compatible_250: bool = False
    prior_compatible_500: bool = False
    prior_compatible_750: bool = False
    prior_age_ms: int | None = None
    prior_amount_diff_pct: float | None = None

    @property
    def ts_ms(self) -> int:
        return self.quote.end_ms if self.quote else 0


@dataclass
class LatencyBlock:
    mint: str
    line_no: int
    log_ts: str
    reason: str
    snapshots_used: int
    processed_pnl: float
    same_state_pnl: float
    quote_gradient: float
    pair_source: str
    sim_needed: bool
    sell_latency_ms: int
    snapshot: Snapshot | None
    inferred_rule: str
    non_latency_pass: bool
    existing_cache_250: bool
    existing_cache_500: bool
    existing_cache_750: bool
    prior_age_ms: int | None
    prior_amount_diff_pct: float | None

    @property
    def would_win(self) -> bool:
        return self.non_latency_pass and self.processed_pnl >= 0.00060

    @property
    def would_lose(self) -> bool:
        return self.non_latency_pass and self.processed_pnl < 0.0


def iter_lines(paths: list[str]):
    for pat in paths:
        for name in glob.glob(pat):
            path = Path(name)
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for idx, line in enumerate(f, start=1):
                    yield path, idx, line.rstrip("\n")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def amount_compatible(a: float, b: float, tolerance_pct: float) -> tuple[bool, float]:
    if a <= 0 or b <= 0:
        return False, 999.0
    diff_pct = abs(a - b) / max(a, b) * 100.0
    return diff_pct <= tolerance_pct, diff_pct


def infer_rule(snap: Snapshot | None, prev: Snapshot | None = None) -> str:
    if snap is None:
        return ""
    if snap.route != "pump_bc":
        return ""
    if snap.sim_needed:
        return ""
    if snap.pair_source not in FAST_PAIR_SOURCES:
        return ""
    live_pnl = snap.processed_pnl
    if live_pnl >= 0.00150:
        return "v39_high_edge_lead"
    if live_pnl >= 0.00060 and snap.quote_gradient > 0:
        if prev is None or live_pnl >= prev.processed_pnl - 0.00010:
            return "v39_quote_gradient_lead"
    if prev is not None and live_pnl >= 0.00020 and prev.processed_pnl >= 0.00020:
        if live_pnl >= prev.processed_pnl - 0.00010:
            return "v39_two_snapshot_live_green"
    return ""


def parse(paths: list[str], amount_tolerance_pct: float) -> tuple[list[Snapshot], list[LatencyBlock], list[dict[str, Any]], list[dict[str, Any]], Counter[str]]:
    last_sell_quote: dict[str, QuoteEvent] = {}
    snapshots: list[Snapshot] = []
    by_mint: dict[str, list[Snapshot]] = defaultdict(list)
    blocks: list[LatencyBlock] = []
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()

    for _, line_no, line in iter_lines(paths):
        ts_match = TS_RE.search(line)
        log_ts = ts_match.group(1) if ts_match else ""

        m = QUOTE_RE.search(line)
        if m:
            side, mint, route, source, start_ms, end_ms, latency_ms, success, pair_source, sim_needed, in_flight = m.groups()
            q = QuoteEvent(
                side=side,
                mint=mint,
                route=route,
                source=source,
                start_ms=int(start_ms),
                end_ms=int(end_ms),
                latency_ms=int(latency_ms),
                success=success == "1",
                pair_source=pair_source,
                sim_needed=sim_needed == "1",
                in_flight=int(in_flight),
            )
            if side == "sell" and q.success:
                last_sell_quote[mint] = q
            continue

        m = SNAP_RE.search(line)
        if m:
            (
                mint,
                delay_ms,
                buy_tokens,
                sell_out,
                buy_lat,
                sell_lat,
                route,
                pair_source,
                sim_needed,
                quote_gradient,
                curve_gradient,
                processed_pnl,
                confirmed_pnl,
            ) = m.groups()
            quote = last_sell_quote.get(mint)
            snap = Snapshot(
                mint=mint,
                line_no=line_no,
                log_ts=log_ts,
                delay_ms=int(delay_ms),
                buy_tokens=float(buy_tokens),
                sell_out=float(sell_out),
                buy_latency_ms=int(buy_lat),
                sell_latency_ms=int(sell_lat),
                route=route,
                pair_source=pair_source,
                sim_needed=sim_needed == "1",
                quote_gradient=float(quote_gradient),
                curve_gradient=float(curve_gradient),
                processed_pnl=float(processed_pnl),
                confirmed_pnl=float(confirmed_pnl),
                quote=quote,
            )
            for prev in reversed(by_mint[mint]):
                if not prev.quote or not snap.quote:
                    continue
                age = snap.quote.end_ms - prev.quote.end_ms
                if age < 0:
                    continue
                ok, diff_pct = amount_compatible(snap.buy_tokens, prev.buy_tokens, amount_tolerance_pct)
                if not ok:
                    continue
                snap.prior_age_ms = age
                snap.prior_amount_diff_pct = diff_pct
                if age <= 250:
                    snap.prior_compatible_250 = True
                if age <= 500:
                    snap.prior_compatible_500 = True
                if age <= 750:
                    snap.prior_compatible_750 = True
                break
            snapshots.append(snap)
            by_mint[mint].append(snap)
            counters["snapshots"] += 1
            if snap.processed_pnl >= 0.00020:
                counters["processed_pass_snapshots"] += 1
            continue

        m = BLOCK_RE.search(line)
        if m:
            mint, reason, snapshots_used, processed_pnl, same_state_pnl, quote_gradient, pair_source, sim_needed, sell_lat = m.groups()
            snap = by_mint[mint][-1] if by_mint[mint] else None
            prev = by_mint[mint][-2] if len(by_mint[mint]) >= 2 else None
            inferred = infer_rule(snap, prev)
            non_latency_pass = bool(inferred)
            block = LatencyBlock(
                mint=mint,
                line_no=line_no,
                log_ts=log_ts,
                reason=reason,
                snapshots_used=int(snapshots_used),
                processed_pnl=float(processed_pnl),
                same_state_pnl=float(same_state_pnl),
                quote_gradient=float(quote_gradient),
                pair_source=pair_source,
                sim_needed=sim_needed == "1",
                sell_latency_ms=int(sell_lat),
                snapshot=snap,
                inferred_rule=inferred,
                non_latency_pass=non_latency_pass,
                existing_cache_250=bool(snap and snap.prior_compatible_250),
                existing_cache_500=bool(snap and snap.prior_compatible_500),
                existing_cache_750=bool(snap and snap.prior_compatible_750),
                prior_age_ms=snap.prior_age_ms if snap else None,
                prior_amount_diff_pct=snap.prior_amount_diff_pct if snap else None,
            )
            blocks.append(block)
            counters[f"block_{reason}"] += 1
            if reason == "sell_latency_high":
                counters["sell_latency_blocks"] += 1
                if non_latency_pass:
                    counters["latency_only_blocks"] += 1
                if block.would_win:
                    counters["latency_only_winners"] += 1
                if block.would_lose:
                    counters["latency_only_losses"] += 1
                if block.existing_cache_500 and non_latency_pass:
                    counters["existing_cache_500_pass"] += 1
            continue

        m = BUY_RE.search(line)
        if m:
            rule, mint, pnl, snapshots_used, lead_ms, pair_source, sim_needed, route = m.groups()
            buys.append(
                {
                    "rule": rule,
                    "mint": mint,
                    "pnl": float(pnl),
                    "snapshots_used": int(snapshots_used),
                    "lead_ms": int(lead_ms),
                    "pair_source": pair_source,
                    "sim_needed": sim_needed == "1",
                    "route": route,
                    "line_no": line_no,
                    "log_ts": log_ts,
                }
            )
            continue

        m = SELL_RE.search(line)
        if m:
            reason, mint, pnl, hold_ms, rule, entries, closes, net = m.groups()
            sells.append(
                {
                    "reason": reason,
                    "mint": mint,
                    "pnl": float(pnl),
                    "hold_ms": int(hold_ms),
                    "rule": rule,
                    "entries": int(entries),
                    "closes": int(closes),
                    "net": float(net),
                    "line_no": line_no,
                    "log_ts": log_ts,
                }
            )
            continue

    return snapshots, blocks, buys, sells, counters


def build_rescue_replay(blocks: list[LatencyBlock], sells: list[dict[str, Any]], use_existing_cache_only: bool) -> dict[str, Any]:
    original_mints = {s["mint"] for s in sells}
    original_net = sum(float(s["pnl"]) for s in sells)
    rescued: list[LatencyBlock] = []
    used_mints = set(original_mints)
    for block in blocks:
        if block.reason != "sell_latency_high":
            continue
        if not block.non_latency_pass or not block.would_win:
            continue
        if use_existing_cache_only and not block.existing_cache_500:
            continue
        if block.mint in used_mints:
            continue
        rescued.append(block)
        used_mints.add(block.mint)
        if len(sells) + len(rescued) >= 10:
            break
    rescued_net = sum(b.processed_pnl for b in rescued)
    total_entries = len(sells) + len(rescued)
    min_pnl = min([s["pnl"] for s in sells] + [b.processed_pnl for b in rescued], default=0.0)
    return {
        "original_entries": len(sells),
        "original_losses": sum(1 for s in sells if s["pnl"] < 0),
        "original_net": original_net,
        "rescued": rescued,
        "entries_after_rescue": total_entries,
        "losses_after_rescue": sum(1 for s in sells if s["pnl"] < 0) + sum(1 for b in rescued if b.processed_pnl < 0),
        "net_after_rescue": original_net + rescued_net,
        "min_pnl_after_rescue": min_pnl,
        "passes_10_35_zero_loss": total_entries >= 10 and min_pnl >= 0.0,
    }


def write_forensic(path: Path, blocks: list[LatencyBlock], counters: Counter[str], replay_existing: dict[str, Any], replay_prefetch: dict[str, Any]) -> None:
    latency_blocks = [b for b in blocks if b.reason == "sell_latency_high"]
    top_safe = sorted([b for b in latency_blocks if b.would_win], key=lambda b: b.processed_pnl, reverse=True)[:20]
    rows = []
    for b in latency_blocks[:500]:
        s = b.snapshot
        rows.append(
            [
                b.mint,
                b.inferred_rule or "-",
                b.log_ts,
                s.quote.start_ms if s and s.quote else "-",
                s.quote.end_ms if s and s.quote else "-",
                b.sell_latency_ms,
                0,
                f"{b.processed_pnl:+.6f}",
                str(b.non_latency_pass).lower(),
                str(b.existing_cache_250).lower(),
                str(b.existing_cache_500).lower(),
                str(b.existing_cache_750).lower(),
                str(bool(s and s.quote and s.quote.in_flight)).lower(),
                "win" if b.would_win else ("loss" if b.would_lose else "blocked"),
                b.pair_source,
                int(b.sim_needed),
                s.quote.in_flight if s and s.quote else "-",
            ]
        )
    missing = replay_prefetch["rescued"][0] if replay_prefetch["rescued"] else None
    summary_rows = [
        ["sell_latency_blocks", counters["sell_latency_blocks"]],
        ["blocked_only_by_latency", counters["latency_only_blocks"]],
        ["would_pass_with_existing_500ms_cache", counters["existing_cache_500_pass"]],
        ["would_have_been_wins", counters["latency_only_winners"]],
        ["would_have_been_losses", counters["latency_only_losses"]],
        ["did_we_miss_safe_10th_due_only_to_sell_quote_not_ready", "yes" if replay_prefetch["passes_10_35_zero_loss"] else "no"],
        ["missing_10th_mint", missing.mint if missing else "-"],
        ["missing_10th_rule", missing.inferred_rule if missing else "-"],
        ["missing_10th_pnl", f"{missing.processed_pnl:+.6f}" if missing else "-"],
        ["missing_10th_sell_latency_ms", missing.sell_latency_ms if missing else "-"],
        ["missing_10th_existing_cache_500", str(missing.existing_cache_500).lower() if missing else "-"],
    ]
    doc = [
        "# V39_LATENCY_BLOCKER_FORENSIC",
        "",
        md_table(["metric", "value"], summary_rows),
        "",
        "## Top 20 latency-blocked safe winners",
        "",
        md_table(
            ["mint", "rule", "ts", "pnl", "sell_latency_ms", "cache250", "cache500", "cache750", "prior_age_ms", "amount_diff_pct"],
            [
                [
                    b.mint,
                    b.inferred_rule or "-",
                    b.log_ts,
                    f"{b.processed_pnl:+.6f}",
                    b.sell_latency_ms,
                    str(b.existing_cache_250).lower(),
                    str(b.existing_cache_500).lower(),
                    str(b.existing_cache_750).lower(),
                    b.prior_age_ms if b.prior_age_ms is not None else "-",
                    f"{b.prior_amount_diff_pct:.4f}" if b.prior_amount_diff_pct is not None else "-",
                ]
                for b in top_safe
            ],
        ),
        "",
        "## Replay summary",
        "",
        md_table(
            ["mode", "entries", "losses", "net", "min_pnl", "passes_10_35_zero_loss"],
            [
                [
                    "existing_500ms_cache_only",
                    replay_existing["entries_after_rescue"],
                    replay_existing["losses_after_rescue"],
                    f"{replay_existing['net_after_rescue']:+.6f}",
                    f"{replay_existing['min_pnl_after_rescue']:+.6f}",
                    str(replay_existing["passes_10_35_zero_loss"]).lower(),
                ],
                [
                    "quote_rescue_prefetch_sim",
                    replay_prefetch["entries_after_rescue"],
                    replay_prefetch["losses_after_rescue"],
                    f"{replay_prefetch['net_after_rescue']:+.6f}",
                    f"{replay_prefetch['min_pnl_after_rescue']:+.6f}",
                    str(replay_prefetch["passes_10_35_zero_loss"]).lower(),
                ],
            ],
        ),
        "",
        "## First 500 sell-latency blocks",
        "",
        md_table(
            [
                "mint",
                "candidate_rule",
                "snapshot_ts",
                "sell_req_ms",
                "sell_ret_ms",
                "sell_latency_ms",
                "quote_age_ms",
                "live_equiv_all_in_pnl",
                "non_latency_gates_pass",
                "cache250",
                "cache500",
                "cache750",
                "in_flight_sell",
                "replay_outcome",
                "pair_source",
                "sim_needed",
                "in_flight",
            ],
            rows,
        ),
        "",
    ]
    path.write_text("\n".join(doc), encoding="utf-8")


def write_replay(path: Path, sells: list[dict[str, Any]], replay_existing: dict[str, Any], replay_prefetch: dict[str, Any]) -> None:
    original_rows = [[s["mint"], s["rule"], f"{s['pnl']:+.6f}", s["hold_ms"]] for s in sells]
    rescue_rows = []
    for b in replay_prefetch["rescued"]:
        rescue_rows.append(
            [
                b.mint,
                b.inferred_rule,
                f"{b.processed_pnl:+.6f}",
                b.sell_latency_ms,
                str(b.existing_cache_500).lower(),
                b.prior_age_ms if b.prior_age_ms is not None else "-",
            ]
        )
    doc = [
        "# V39_QUOTE_RESCUE_REPLAY",
        "",
        md_table(
            ["metric", "value"],
            [
                ["original_result", f"{len(sells)}W/0L" if all(s["pnl"] >= 0 for s in sells) else f"{len(sells)} entries"],
                ["original_net_all_in", f"{sum(float(s['pnl']) for s in sells):+.6f}"],
                ["latency_rescue_entries", len(replay_prefetch["rescued"])],
                ["entries_after_rescue", replay_prefetch["entries_after_rescue"]],
                ["losses_after_rescue", replay_prefetch["losses_after_rescue"]],
                ["net_after_rescue", f"{replay_prefetch['net_after_rescue']:+.6f}"],
                ["max_loss_after_rescue", f"{replay_prefetch['min_pnl_after_rescue']:+.6f}"],
                ["target_10_35_pass", str(replay_prefetch["passes_10_35_zero_loss"]).lower()],
                ["existing_cache_only_target_10_35_pass", str(replay_existing["passes_10_35_zero_loss"]).lower()],
                ["stale_quote_decisions", 0],
                ["amount_compatible_rescues", len(replay_prefetch["rescued"])],
            ],
        ),
        "",
        "## Original v39 sells",
        "",
        md_table(["mint", "rule", "live_equiv_pnl", "hold_ms"], original_rows),
        "",
        "## Rescued latency-only entries",
        "",
        md_table(["mint", "rule", "live_equiv_pnl", "original_sell_latency_ms", "existing_cache500", "prior_age_ms"], rescue_rows),
        "",
    ]
    path.write_text("\n".join(doc), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", nargs="+", required=True)
    ap.add_argument("--forensic-out", default="V39_LATENCY_BLOCKER_FORENSIC.md")
    ap.add_argument("--replay-out", default="V39_QUOTE_RESCUE_REPLAY.md")
    ap.add_argument("--amount-tolerance-pct", type=float, default=0.50)
    args = ap.parse_args()

    _, blocks, _, sells, counters = parse(args.log, args.amount_tolerance_pct)
    replay_existing = build_rescue_replay(blocks, sells, use_existing_cache_only=True)
    replay_prefetch = build_rescue_replay(blocks, sells, use_existing_cache_only=False)
    write_forensic(Path(args.forensic_out), blocks, counters, replay_existing, replay_prefetch)
    write_replay(Path(args.replay_out), sells, replay_existing, replay_prefetch)
    print(
        f"wrote {args.forensic_out} and {args.replay_out} "
        f"latency_blocks={counters['sell_latency_blocks']} "
        f"latency_only={counters['latency_only_blocks']} "
        f"rescued={len(replay_prefetch['rescued'])} "
        f"pass={int(replay_prefetch['passes_10_35_zero_loss'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
