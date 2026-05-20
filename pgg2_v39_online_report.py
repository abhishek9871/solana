from __future__ import annotations

import argparse
import glob
import re
from collections import Counter
from pathlib import Path


RE_SNAPSHOT = re.compile(
    r"PGG2-V39-LEAD-SNAPSHOT mint=(\S+).*?delay_ms=(\d+).*?"
    r"pair_source=(\S+) sim_needed=(\d+).*?"
    r"processed_live_equiv_all_in_pnl=([+\-0-9.]+)"
)
RE_BLOCK = re.compile(
    r"PGG2-V39-ENTRY-BLOCK mint=(\S+) reason=(\S+).*?"
    r"processed_live_equiv_all_in_pnl=([+\-0-9.]+).*?"
    r"same_state_all_in_pnl=([+\-0-9.]+).*?"
    r"pair_source=(\S+) sim_needed=(\d+) sell_lat_ms=(\d+)"
)
RE_BUY = re.compile(
    r"PGG2-V39-DRYLIVE-BUY rule_id=(\S+) mint=(\S+) "
    r"processed_live_equiv_all_in_pnl=([+\-0-9.]+)"
)
RE_SELL = re.compile(
    r"PGG2-V39-DRYLIVE-SELL reason=(\S+) mint=(\S+) "
    r"live_equiv_all_in_pnl=([+\-0-9.]+).*?hold_ms=(\d+)"
)


def iter_lines(paths: list[str]):
    for pat in paths:
        for name in glob.glob(pat):
            path = Path(name)
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    yield line.rstrip("\n")


def md_table(headers: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", nargs="+", required=True)
    ap.add_argument("--out", default="V39_ONLINE_BLOCKER_REPORT.md")
    args = ap.parse_args()

    snapshots = 0
    quoteable_mints: set[str] = set()
    two_snapshot_mints: Counter[str] = Counter()
    block_reasons: Counter[str] = Counter()
    buys: list[tuple[str, str, float]] = []
    sells: list[tuple[str, str, float, int]] = []
    sim_needed_blocks = 0
    latency_blocks = 0
    loss_floor_blocks = 0
    fake_esb_would_pass = 0
    live_seq_pass = 0
    improving_blocks = 0
    stale_lines = 0
    close_fail = 0
    token_mismatch = 0
    jito = 0
    real_live = 0

    for line in iter_lines(args.log):
        if "jito" in line.lower():
            jito += 1
        if "mode=LIVE" in line or "PGG2-LIVE-BUY" in line:
            real_live += 1
        if "STALE" in line or "stale_quote" in line:
            stale_lines += 1
        if "CLOSE-FAIL" in line:
            close_fail += 1
        if "TOKEN-MISMATCH-FATAL" in line:
            token_mismatch += 1
        m = RE_SNAPSHOT.search(line)
        if m:
            mint, delay, pair, sim, pnl = m.groups()
            snapshots += 1
            quoteable_mints.add(mint)
            two_snapshot_mints[mint] += 1
            if float(pnl) >= 0.00020:
                live_seq_pass += 1
            continue
        m = RE_BLOCK.search(line)
        if m:
            mint, reason, live_pnl, same_state, pair, sim, sell_lat = m.groups()
            block_reasons[reason] += 1
            if sim == "1":
                sim_needed_blocks += 1
            if int(sell_lat) > 750:
                latency_blocks += 1
            if float(live_pnl) < 0.00020:
                loss_floor_blocks += 1
            if float(same_state) >= 0.00060 and float(live_pnl) < 0.00020:
                fake_esb_would_pass += 1
            if reason == "live_seq_floor_or_gradient":
                improving_blocks += 1
            continue
        m = RE_BUY.search(line)
        if m:
            rule, mint, pnl = m.groups()
            buys.append((rule, mint, float(pnl)))
            continue
        m = RE_SELL.search(line)
        if m:
            reason, mint, pnl, hold = m.groups()
            sells.append((reason, mint, float(pnl), int(hold)))
            continue

    negative = [s for s in sells if s[2] < 0]
    net = sum(s[2] for s in sells)
    report = [
        "# V39_ONLINE_BLOCKER_REPORT",
        "",
        md_table(
            ["metric", "value"],
            [
                ["online_snapshots", snapshots],
                ["quoteable_mints", len(quoteable_mints)],
                ["mints_with_two_snapshots", sum(1 for _, c in two_snapshot_mints.items() if c >= 2)],
                ["v39_buys", len(buys)],
                ["v39_sells", len(sells)],
                ["negative_live_equiv_sells", len(negative)],
                ["net_live_equiv_pnl", f"{net:+.6f}"],
                ["processed_live_seq_pass_snapshots", live_seq_pass],
                ["blocked_by_sim_needed", sim_needed_blocks],
                ["blocked_by_quote_latency", latency_blocks],
                ["blocked_by_loss_floor", loss_floor_blocks],
                ["old_fake_esb_would_pass_but_live_seq_failed", fake_esb_would_pass],
                ["stale_or_stale_block_lines", stale_lines],
                ["close_fail_lines", close_fail],
                ["token_mismatch_lines", token_mismatch],
                ["jito_mentions", jito],
                ["real_live_lines", real_live],
            ],
        ),
        "",
        "## Block Reasons",
        "",
        md_table(["reason", "count"], [[k, v] for k, v in block_reasons.most_common()]),
        "",
        "## V39 Sells",
        "",
        md_table(
            ["reason", "mint", "live_equiv_pnl", "hold_ms"],
            [[r, m, f"{p:+.6f}", h] for r, m, p, h in sells],
        ),
        "",
    ]
    Path(args.out).write_text("\n".join(report), encoding="utf-8")
    print(f"wrote {args.out} snapshots={snapshots} buys={len(buys)} sells={len(sells)} negatives={len(negative)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
