#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s\]]+)")


def kv(line: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in KV_RE.findall(line)}


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    idx = min(len(vals) - 1, max(0, int(round((p / 100.0) * (len(vals) - 1)))))
    return vals[idx]


def stats(vals: list[float]) -> dict[str, Any]:
    return {
        "n": len(vals),
        "p50": round(pct(vals, 50), 1),
        "p90": round(pct(vals, 90), 1),
        "p95": round(pct(vals, 95), 1),
        "max": round(max(vals), 1) if vals else 0.0,
    }


def parse_log(path: Path) -> dict[str, Any]:
    quote_lat: dict[tuple[str, str], list[float]] = defaultdict(list)
    quote_lat_by_pair: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    quote_success = Counter()
    quote_errors = Counter()
    entry_blocks = Counter()
    prefetch_entry_blocks = Counter()
    counts = Counter()
    max_inflight = 0
    live_buys = []
    live_sells = []
    v39_candidate_checks = []
    v39_valid_checks = []
    v39_snap_positive_latency_blocks = []
    direct_429 = 0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.search(r"(?:HTTPError|status|code|http)\s*[=:]?\s*429\b|\b429 Too Many Requests\b", line, re.I):
            direct_429 += 1
        if "PGG2-QUOTE-LATENCY" in line:
            d = kv(line)
            side = d.get("side", "?")
            pair = d.get("pair_source") or d.get("source") or "unknown"
            route = d.get("route", "unknown")
            lat = fnum(d.get("latency_ms"))
            if d.get("success") == "1":
                quote_lat[(side, route)].append(lat)
                quote_lat_by_pair[(side, route, pair)].append(lat)
                quote_success[(side, route, pair)] += 1
            else:
                quote_errors[(side, d.get("error_class", "unknown"))] += 1
            max_inflight = max(max_inflight, int(fnum(d.get("in_flight"), 0)))
        if "PGG2-V39-ENTRY-BLOCK" in line:
            d = kv(line)
            reason = d.get("reason", "unknown")
            entry_blocks[reason] += 1
            pnl = fnum(d.get("processed_live_equiv_all_in_pnl"))
            sell_lat = fnum(d.get("sell_lat_ms"))
            if reason == "sell_latency_high" and pnl >= 0.00020:
                v39_snap_positive_latency_blocks.append(
                    {
                        "mint": d.get("mint", ""),
                        "pnl": pnl,
                        "sell_lat_ms": sell_lat,
                        "rule_possible": "pnl>=floor_before_latency",
                    }
                )
        if "PGG2-V39-PREFETCH-ENTRY-BLOCK" in line:
            d = kv(line)
            prefetch_entry_blocks[d.get("reason", "unknown")] += 1
        if "PGG2-V39-PREFETCH-ENTRY-CHECK" in line:
            d = kv(line)
            v39_candidate_checks.append(d)
            if d.get("rule_id", "-") != "-":
                v39_valid_checks.append(d)
        if "PGG2-QUOTE-MGR-REQ" in line:
            counts["quote_mgr_req"] += 1
        if "PGG2-QUOTE-MGR-NETWORK-RESULT" in line:
            counts["quote_mgr_network_result"] += 1
        if "PGG2-QUOTE-MGR-CACHE-HIT" in line:
            counts["quote_mgr_cache_hit"] += 1
        if "PGG2-QUOTE-MGR-RATE-LIMITED" in line:
            counts["quote_mgr_rate_limited"] += 1
        if "PGG2-QUOTE-MGR-ERROR" in line:
            counts["quote_mgr_error"] += 1
        if "PGG2-V39-SELL-PREFETCH-REQ" in line:
            counts["prefetch_req"] += 1
        if "PGG2-V39-SELL-PREFETCH-RESULT" in line:
            counts["prefetch_result"] += 1
        if "PGG2-V39-WATCHLIST-ADD" in line:
            counts["watchlist_add"] += 1
        if "PGG2-V39-WATCHLIST-DROP" in line:
            counts["watchlist_drop"] += 1
        if "PGG2-V39-ENTRY-ROUTER" in line:
            counts["v39_entry_router"] += 1
        if "PGG2-V39-LIVE-BUY-SEND" in line:
            counts["v39_live_buy_send"] += 1
        if "PGG2-LIVE-BUY" in line:
            live_buys.append(line)
        if "PGG2-LIVE-SELL" in line:
            live_sells.append(line)
        if "Traceback" in line:
            counts["traceback"] += 1
        if "PGG2-POSITION-TOKEN-MISMATCH-FATAL" in line:
            counts["token_mismatch"] += 1
        if "PGG2-RISK-CLOSE-FAIL" in line:
            counts["close_fail"] += 1

    return {
        "path": str(path),
        "quote_latency": {f"{k[0]}/{k[1]}": stats(v) for k, v in sorted(quote_lat.items())},
        "quote_latency_by_pair": {f"{k[0]}/{k[1]}/{k[2]}": stats(v) for k, v in sorted(quote_lat_by_pair.items())},
        "quote_success": dict(quote_success),
        "quote_errors": dict(quote_errors),
        "entry_blocks": dict(entry_blocks),
        "prefetch_entry_blocks": dict(prefetch_entry_blocks),
        "counts": dict(counts),
        "max_inflight": max_inflight,
        "direct_429_mentions": direct_429,
        "live_buys": live_buys,
        "live_sells": live_sells,
        "v39_prefetch_entry_checks": len(v39_candidate_checks),
        "v39_prefetch_valid_rule_checks": len(v39_valid_checks),
        "v39_valid_rule_examples": v39_valid_checks[:10],
        "positive_latency_blocks": v39_snap_positive_latency_blocks[:20],
        "positive_latency_block_count": len(v39_snap_positive_latency_blocks),
    }


def rpc_call(url: str, method: str, params: list[Any]) -> tuple[bool, float, str]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        out = urllib.request.urlopen(req, timeout=10).read()
        ms = (time.perf_counter() - t0) * 1000
        parsed = json.loads(out)
        if "error" in parsed:
            return False, ms, str(parsed["error"])
        return True, ms, ""
    except Exception as exc:
        return False, (time.perf_counter() - t0) * 1000, repr(exc)


def raw_rpc_bench(url: str, wallet: str, n: int) -> dict[str, Any]:
    result: dict[str, Any] = {"endpoint": url.split("?")[0], "samples": n}
    calls = {
        "getSlot": ("getSlot", []),
        "getBalance": ("getBalance", [wallet]),
        "getAccountInfo_wallet": ("getAccountInfo", [wallet, {"encoding": "base64"}]),
    }
    for name, (method, params) in calls.items():
        vals: list[float] = []
        errs: list[str] = []
        for _ in range(n):
            ok, ms, err = rpc_call(url, method, params)
            vals.append(ms)
            if not ok:
                errs.append(err)
        result[name] = {**stats(vals), "errors": errs[:3], "error_count": len(errs)}
    return result


def render_md(live: dict[str, Any], dry: dict[str, Any], rpc: dict[str, Any], out: Path) -> None:
    root_causes = []
    if live["counts"].get("v39_entry_router", 0) == 0 and live.get("v39_prefetch_valid_rule_checks", 0) > 0:
        root_causes.append("v39 valid rule checks existed, but live entry router never ran; live-safe gate/plumbing blocked v39 before entry.")
    if live.get("live_buys"):
        non_v39 = [x for x in live["live_buys"] if "PGG2-V39-LIVE-BUY" not in x and "lane=v39" not in x]
        if non_v39:
            root_causes.append("live mirror allowed non-v39 queue_or_fill lanes; observed real PGG2-LIVE-BUY on non-v39 lane.")
    if live["counts"].get("quote_mgr_rate_limited", 0):
        root_causes.append("QuoteManager internal in-flight rate limits are present; this is not necessarily HTTP/RPC dashboard latency.")
    if not root_causes:
        root_causes.append("No single root cause inferred from logs; inspect raw sections below.")

    lines = [
        "# LIVE_QUOTE_LATENCY_DIFFERENTIAL_AUDIT",
        "",
        f"- live_log: `{live['path']}`",
        f"- drylive_log: `{dry['path']}`",
        "",
        "## Raw RPC/dashboard-style checks",
        "```json",
        json.dumps(rpc, indent=2, sort_keys=True),
        "```",
        "",
        "## Direct broker quote latency: live",
        "```json",
        json.dumps(live["quote_latency"], indent=2, sort_keys=True),
        "```",
        "",
        "## Direct broker quote latency: successful dry-live",
        "```json",
        json.dumps(dry["quote_latency"], indent=2, sort_keys=True),
        "```",
        "",
        "## Direct broker quote latency by pair_source: live",
        "```json",
        json.dumps(live["quote_latency_by_pair"], indent=2, sort_keys=True),
        "```",
        "",
        "## QuoteManager internals: live",
        "```json",
        json.dumps(
            {
                "counts": live["counts"],
                "entry_blocks": live["entry_blocks"],
                "prefetch_entry_blocks": live["prefetch_entry_blocks"],
                "max_inflight": live["max_inflight"],
                "direct_429_mentions": live["direct_429_mentions"],
                "v39_prefetch_entry_checks": live["v39_prefetch_entry_checks"],
                "v39_prefetch_valid_rule_checks": live["v39_prefetch_valid_rule_checks"],
                "positive_latency_block_count": live["positive_latency_block_count"],
            },
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## Live-vs-drylive path diff findings",
        "",
        f"- live v39 entry router count: `{live['counts'].get('v39_entry_router', 0)}`",
        f"- live v39 live-buy-send count: `{live['counts'].get('v39_live_buy_send', 0)}`",
        f"- live raw PGG2-LIVE-BUY count: `{len(live['live_buys'])}`",
        f"- live raw PGG2-LIVE-SELL count: `{len(live['live_sells'])}`",
        f"- dry-live entry router count: `{dry['counts'].get('v39_entry_router', 0)}`",
        "",
        "## Valid v39 prefetch checks that did not route",
        "```json",
        json.dumps(live["v39_valid_rule_examples"], indent=2, sort_keys=True),
        "```",
        "",
        "## Top positive latency-blocked examples",
        "```json",
        json.dumps(live["positive_latency_blocks"], indent=2, sort_keys=True),
        "```",
        "",
        "## Root cause",
    ]
    for item in root_causes:
        lines.append(f"- {item}")
    lines += [
        "",
        "## Required fix",
        "- Allow v39 live mirror/smoke through the v39 safety gate while keeping the same target/max-open caps.",
        "- Block all non-v39 `queue_or_fill` live entries whenever v39 live mirror/smoke mode is enabled.",
        "- Do not loosen sell-latency or PnL thresholds from this audit.",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-log", required=True)
    ap.add_argument("--drylive-log", default="logs/pgg2_v39b_quote_rescue_drylive_20260512_133527.log")
    ap.add_argument("--out", default="LIVE_QUOTE_LATENCY_DIFFERENTIAL_AUDIT.md")
    ap.add_argument("--rpc-url", default="https://api.mainnet-beta.solana.com")
    ap.add_argument("--wallet", default="Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7")
    ap.add_argument("--rpc-samples", type=int, default=5)
    args = ap.parse_args()

    live = parse_log(Path(args.live_log))
    dry = parse_log(Path(args.drylive_log))
    rpc = raw_rpc_bench(args.rpc_url, args.wallet, args.rpc_samples)
    render_md(live, dry, rpc, Path(args.out))
    print(f"AUDIT={args.out}")
    print(f"LIVE_LOG={args.live_log}")
    print(f"V39_ROUTER={live['counts'].get('v39_entry_router', 0)}")
    print(f"V39_VALID_PREFETCH_CHECKS={live.get('v39_prefetch_valid_rule_checks', 0)}")
    print(f"LIVE_BUYS={len(live['live_buys'])}")
    print(f"QUOTE_MGR_RATE_LIMITED={live['counts'].get('quote_mgr_rate_limited', 0)}")
    print(f"HTTP_429_MENTIONS={live['direct_429_mentions']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
