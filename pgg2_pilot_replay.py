"""v33 — offline pilot-log replay summary.

Parses a single dry-live pilot log and emits a structured summary of
the pilot lifecycle so we can confirm — before re-running — that the
pre-fix log had the architecture issues (close-fail, quote overlap,
rule alias) AND the economic close was correct.

Usage:
  python pgg2_pilot_replay.py --log <path> [--mint <mint>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

_RE_BUY = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-DRYLIVE-PILOT-BUY rule_id=(?P<rule_id>\S+).*?"
    r"mint=(?P<mint>\S+).*?amount=(?P<amount>[\d.+-]+).*?quote_tokens=(?P<qt>[\d.+-]+).*?"
    r"immediate_out=(?P<imm_out>[\d.+-]+).*?immediate_pnl=(?P<imm_pnl>[\d.+-]+).*?"
    r"pair_source=(?P<pair_source>\S+)"
)
_RE_LOCK = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-QUOTE-SHADOW-BUY-LOCKED mint=(?P<mint>\S+) "
    r"quote_id=(?P<qid>\S+) quote_tokens=(?P<qt>[\d.+-]+) fill=\S+ "
    r"quote_age_ms=(?P<qage>\d+) immediate_out=(?P<imm_out>[\d.+-]+) "
    r"cost=(?P<cost>[\d.+-]+) lane=(?P<lane>\S+)"
)
_RE_OPEN = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-DRYLIVE-PILOT-OPEN mint=(?P<mint>\S+) "
    r"cost=(?P<cost>[\d.+-]+) tokens=(?P<tokens>[\d.+-]+)"
)
_RE_RISK_EVAL = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-RISK-QUOTE-EVAL mint=(?P<mint>\S+) "
    r"quote_status=(?P<status>\S+) .*?quote_out=(?P<out>[\d.+-]+) "
    r"net_pnl=(?P<net_pnl>[\d.+-]+) trigger=(?P<trigger>\S+) "
    r"in_flight_for_key=(?P<inflight>\d+)"
)
_RE_RISK_CLOSE_REQ = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-RISK-CLOSE-REQUEST mint=(?P<mint>\S+) "
    r"reason=(?P<reason>\S+)"
)
_RE_RISK_CLOSE_ACK = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-RISK-CLOSE-ACK mint=(?P<mint>\S+) "
    r"reason=(?P<reason>\S+)"
)
_RE_RISK_CLOSE_FAIL = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-RISK-CLOSE-FAIL mint=(?P<mint>\S+) "
    r"(?P<exc>[^:]+): (?P<detail>.+)$"
)
_RE_RISK_CLOSE_SKIP = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-RISK-CLOSE-SKIP mint=(?P<mint>\S+) "
    r"reason=(?P<reason>\S+)"
)
_RE_SELL = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-QUOTE-SHADOW-SELL (?P<mint>\S+) "
    r"reason=(?P<reason>\S+) quote_out=(?P<out>[\d.+-]+) "
    r"gross_quote_pnl=(?P<gross>[\d.+-]+) extra_tx_cost=(?P<extra>[\d.+-]+) "
    r"all_in_pnl=(?P<all_in>[\d.+-]+) legacy_pnl=(?P<legacy>[\d.+-]+) "
    r"pnl_model_version=(?P<model>\S+) cost_model_route=(?P<route>\S+) "
    r"cost_model_confidence=(?P<conf>\S+)"
)
_RE_LATENCY_INFLIGHT = re.compile(
    r"PGG2-QUOTE-LATENCY side=(?P<side>\S+) mint=\S+ .*?in_flight=(?P<inflight>\d+)"
)
_RE_BUY_FULL = re.compile(r"PGG2-DRYLIVE-PILOT-BUY .*? mint=(?P<mint>[A-Za-z0-9.]+)")


def _full_mint_for_prefix(lines: list[str], prefix: str) -> Optional[str]:
    """Find full mint pubkey from any line that prints it (the short form
    "AbCd..pump" is the prefix; we need the long form for the BUY record's
    immediate_pnl line)."""
    target = prefix.split("..")[0]
    for line in lines:
        for tok in re.findall(r"\b([A-Za-z0-9]{6,44}(?:pump|bonk|tofu)?)\b", line):
            if tok.startswith(target) and len(tok) > len(target):
                return tok
    return prefix


def replay(log_path: Path, target_mint_prefix: Optional[str] = None) -> dict[str, Any]:
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    pilots: list[dict[str, Any]] = []
    # Index BUY events
    for i, line in enumerate(lines):
        m = _RE_BUY.search(line)
        if not m:
            continue
        mint_short = m.group("mint")
        if target_mint_prefix and not mint_short.startswith(target_mint_prefix):
            continue
        pilots.append({
            "buy_index": i,
            "ts": m.group("ts"),
            "rule_id": m.group("rule_id"),
            "mint": mint_short,
            "amount": float(m.group("amount")),
            "quote_tokens_decision": float(m.group("qt")),
            "immediate_out_decision": float(m.group("imm_out")),
            "immediate_pnl_decision": float(m.group("imm_pnl")),
            "pair_source": m.group("pair_source"),
        })
    if not pilots:
        return {"error": "no_PILOT_BUY_event_found", "log_path": str(log_path)}
    for p in pilots:
        mint_short = p["mint"]
        # Scan window from BUY to BUY+~120s for lifecycle
        window = lines[p["buy_index"]: p["buy_index"] + 6000]
        # Find LOCK
        lock = None
        for line in window:
            mm = _RE_LOCK.search(line)
            if mm and mm.group("mint") == mint_short:
                lock = {
                    "ts": mm.group("ts"),
                    "quote_id": mm.group("qid"),
                    "quote_tokens_locked": float(mm.group("qt")),
                    "quote_age_ms": int(mm.group("qage")),
                    "immediate_out_locked": float(mm.group("imm_out")),
                    "cost": float(mm.group("cost")),
                    "lane": mm.group("lane"),
                }
                break
        p["lock"] = lock
        # Find OPEN
        open_ev = None
        for line in window:
            mm = _RE_OPEN.search(line)
            if mm and mm.group("mint") == mint_short:
                open_ev = {
                    "ts": mm.group("ts"),
                    "cost": float(mm.group("cost")),
                    "tokens_position": float(mm.group("tokens")),
                }
                break
        p["open"] = open_ev
        # Token match check
        if lock and open_ev:
            tokens_match = (
                abs(p["quote_tokens_decision"] - lock["quote_tokens_locked"]) <= 1e-6
                and abs(lock["quote_tokens_locked"] - open_ev["tokens_position"]) <= 1e-6
            )
        else:
            tokens_match = False
        p["tokens_match"] = tokens_match
        # Risk eval timeline
        risk_evals: list[dict[str, Any]] = []
        for line in window:
            mm = _RE_RISK_EVAL.search(line)
            if mm and mm.group("mint") == mint_short:
                risk_evals.append({
                    "ts": mm.group("ts"),
                    "status": mm.group("status"),
                    "quote_out": float(mm.group("out")),
                    "net_pnl": float(mm.group("net_pnl")),
                    "trigger": mm.group("trigger"),
                    "in_flight_for_key": int(mm.group("inflight")),
                })
        p["risk_eval_count"] = len(risk_evals)
        p["risk_eval_first_3"] = risk_evals[:3]
        # Close events
        req = next((m for m in (_RE_RISK_CLOSE_REQ.search(l) for l in window) if m and m.group("mint") == mint_short), None)
        ack = next((m for m in (_RE_RISK_CLOSE_ACK.search(l) for l in window) if m and m.group("mint") == mint_short), None)
        fails = [m for m in (_RE_RISK_CLOSE_FAIL.search(l) for l in window) if m and m.group("mint") == mint_short]
        skips = [m for m in (_RE_RISK_CLOSE_SKIP.search(l) for l in window) if m and m.group("mint") == mint_short]
        sells = [m for m in (_RE_SELL.search(l) for l in window) if m and m.group("mint") == mint_short]
        p["close_request_reason"] = req.group("reason") if req else None
        p["close_ack_reason"] = ack.group("reason") if ack else None
        p["close_fail_count"] = len(fails)
        p["close_fail_details"] = [f"{m.group('exc').strip()}: {m.group('detail').strip()}" for m in fails]
        p["close_skip_reasons"] = [m.group("reason") for m in skips]
        if sells:
            sell = sells[0]
            p["sell"] = {
                "ts": sell.group("ts"),
                "reason": sell.group("reason"),
                "quote_out": float(sell.group("out")),
                "gross_quote_pnl": float(sell.group("gross")),
                "extra_tx_cost": float(sell.group("extra")),
                "all_in_pnl": float(sell.group("all_in")),
                "legacy_pnl": float(sell.group("legacy")),
                "pnl_model_version": sell.group("model"),
                "cost_model_route": sell.group("route"),
                "cost_model_confidence": sell.group("conf"),
            }
        else:
            p["sell"] = None
        # Quote overlap detection
        overlap_max = 0
        overlap_lines = 0
        for line in window:
            if mint_short.split("..")[0] not in line:
                continue
            ml = _RE_LATENCY_INFLIGHT.search(line)
            if ml:
                inflight = int(ml.group("inflight"))
                if inflight > 1:
                    overlap_lines += 1
                    overlap_max = max(overlap_max, inflight)
        p["quote_overlap_max_inflight"] = overlap_max
        p["quote_overlap_event_count"] = overlap_lines
        # Rule/policy alias detection
        rule_id_set = set()
        for line in window:
            if mint_short.split("..")[0] not in line:
                continue
            for tok in re.findall(r"rule_id=(\S+)", line):
                rule_id_set.add(tok)
        p["rule_ids_seen"] = sorted(rule_id_set)
        p["rule_alias_present"] = any(
            r in {"drylive_quote_edge_150_protected", "mined_quote_edge_pnl_ge_150"}
            for r in rule_id_set
        )
    return {
        "log_path": str(log_path),
        "pilots_count": len(pilots),
        "pilots": pilots,
    }


def gate_summary(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return {"economic_pass": "N/A", "close_fail": "N/A", "quote_overlap": "N/A", "naming": "N/A"}
    p = (result.get("pilots") or [{}])[0]
    economic = "PASS" if (p.get("sell") and float(p["sell"]["all_in_pnl"]) >= 0) else "FAIL"
    close_fail = "FAIL" if p.get("close_fail_count", 0) > 0 else "PASS"
    overlap = "FAIL" if p.get("quote_overlap_max_inflight", 0) > 1 else "PASS"
    rule_alias = "FAIL" if p.get("rule_alias_present") else "PASS"
    tokens_match = "PASS" if p.get("tokens_match") else "FAIL"
    risk_managed = "PASS" if (p.get("close_request_reason") and "risk_worker" in p["close_request_reason"]) else "FAIL"
    return {
        "economic_pass": economic,
        "close_fail_zero": close_fail,
        "quote_overlap_le_1": overlap,
        "naming_canonical": rule_alias,
        "tokens_match": tokens_match,
        "risk_managed_close": risk_managed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--mint", default=None, help="optional mint prefix filter")
    ap.add_argument("--out", default=None, help="optional JSON output path")
    args = ap.parse_args()
    result = replay(Path(args.log), args.mint)
    result["gates"] = gate_summary(result)
    txt = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
