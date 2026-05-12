"""v36c — loss forensic for the v36b SLA log.

For each actual entry (PILOT-BUY or SCALP-BUY), pull:
    - mint, ts, selected_rule, lane bypass, entry features
    - matching PGG2-PNL-BREAKDOWN (route, all_in, cost, gross)
    - matching SHADOW-LAB-REC (lane_label, label, all_in_lookahead)
    - matching PGG2-QUOTE-SHADOW-SELL (close all_in, reason, hold_ms)
    - quote latencies for the mint near entry
Then compare the loser vs winners along multiple feature axes and report
what discriminates them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

_RE_PILOT_BUY = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-DRYLIVE-PILOT-BUY rule_id=(?P<rule>\S+) policy_id=(?P<pol>\S+) pnl_model_version=\S+ "
    r"mint=(?P<mint>\S+) amount=(?P<amount>[\d.+-]+) quote_tokens=(?P<qt>[\d.+-]+) "
    r"immediate_out=(?P<imm>[\d.+-]+) immediate_pnl=(?P<ipnl>[\d.+-]+) buy_impact=(?P<imp>[\d.+-]+) "
    r"pair_source=(?P<pair>\S+) first_quoteable_ms=(?P<fqms>\S+) "
    r"entry_features=\{age_ms:(?P<age>[\d-]+),slot_buyers:(?P<sb>\d+),slot_buy_sol:(?P<sbs>[\d.]+),slot_top:(?P<st>[\d.]+)\}"
)
_RE_SCALP_BUY = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-SCALP-BUY rule_id=(?P<rule>\S+) policy_id=(?P<pol>\S+) pnl_model_version=\S+ "
    r"mint=(?P<mint>\S+) amount=(?P<amount>[\d.+-]+) quote_tokens=(?P<qt>[\d.+-]+) "
    r"immediate_out=(?P<imm>[\d.+-]+) immediate_pnl=(?P<ipnl>[\d.+-]+) buy_impact=(?P<imp>[\d.+-]+) "
    r"pair_source=(?P<pair>\S+) first_quoteable_ms=(?P<fqms>\S+) "
    r"entry_features=\{age_ms:(?P<age>[\d-]+),slot_buyers:(?P<sb>\d+),slot_top:(?P<st>[\d.]+)\}"
)
_RE_SELL = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-QUOTE-SHADOW-SELL (?P<mint>\S+) reason=(?P<reason>\S+) "
    r"quote_out=(?P<qo>[\d.+-]+) gross_quote_pnl=(?P<gqp>[\d.+-]+) extra_tx_cost=(?P<etc>[\d.+-]+) "
    r"all_in_pnl=(?P<all_in>[\d.+-]+) legacy_pnl=(?P<lpnl>[\d.+-]+)"
)
_RE_LAB_REC = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] SHADOW-LAB-REC (?P<mint>\S+) lane=(?P<lane>\S+) label=(?P<label>\S+) "
    r"pnl_model=\S+ all_in_immediate_pnl=(?P<aip>[\d.+-]+) all_in_best_pnl_lookahead=(?P<ala>[\S]+) "
    r"legacy_immediate_pnl=(?P<lip>[\d.+-]+)"
)
_RE_BYPASS = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-LANE-BLACKLIST-BYPASS mint=(?P<mint>\S+) "
    r"old_lane=(?P<old_lane>\S+) independent_rule=(?P<rule>\S+)"
)
_RE_QUOTE_LATENCY = re.compile(
    r"\[(?P<ts>[\d\-\: ]+)\] PGG2-QUOTE-LATENCY side=(?P<side>\S+) mint=(?P<mint>\S+) .*? "
    r"latency_ms=(?P<lat>\d+) success=(?P<succ>\d).*?pair_source=(?P<pair>\S+).*?in_flight=(?P<inflight>\d+)"
)


def _short_match(short_a: str, full_b: str) -> bool:
    """Match 'mint=ABCD..pump' style short prefix to a full mint anywhere."""
    if short_a == full_b:
        return True
    prefix = short_a.split("..")[0]
    return full_b.startswith(prefix)


def parse_log(path: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    bypass_by_mint: dict[str, dict[str, Any]] = {}
    sells_by_mint: dict[str, list[dict[str, Any]]] = {}
    labs_by_mint: dict[str, list[dict[str, Any]]] = {}
    lats_by_mint: dict[str, list[dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "PGG2-LANE-BLACKLIST-BYPASS" in line:
                m = _RE_BYPASS.search(line)
                if m:
                    bypass_by_mint[m.group("mint")] = m.groupdict()
                continue
            if "PGG2-DRYLIVE-PILOT-BUY" in line:
                m = _RE_PILOT_BUY.search(line)
                if m:
                    d = m.groupdict()
                    d["entry_kind"] = "pilot"
                    d["slot_buy_sol"] = float(d["sbs"])
                    entries.append(d)
                continue
            if "PGG2-SCALP-BUY" in line:
                m = _RE_SCALP_BUY.search(line)
                if m:
                    d = m.groupdict()
                    d["entry_kind"] = "scalp"
                    d["slot_buy_sol"] = None  # not in scalp buy line
                    entries.append(d)
                continue
            if "PGG2-QUOTE-SHADOW-SELL" in line:
                m = _RE_SELL.search(line)
                if m:
                    sells_by_mint.setdefault(m.group("mint"), []).append(m.groupdict())
                continue
            if "SHADOW-LAB-REC" in line:
                m = _RE_LAB_REC.search(line)
                if m:
                    labs_by_mint.setdefault(m.group("mint"), []).append(m.groupdict())
                continue
            if "PGG2-QUOTE-LATENCY" in line:
                m = _RE_QUOTE_LATENCY.search(line)
                if m:
                    lats_by_mint.setdefault(m.group("mint"), []).append(m.groupdict())
                continue
    # join sells/labs/lats/bypass to entries
    for e in entries:
        m = e["mint"]
        e["bypass"] = bypass_by_mint.get(m)
        # closest sell after entry
        sells = [s for s in sells_by_mint.get(m, []) if s["ts"] >= e["ts"]]
        e["sell"] = sells[0] if sells else None
        # lab rec for same mint
        labs = labs_by_mint.get(m, [])
        e["lab"] = labs[-1] if labs else None
        # quote latencies near entry (buy + sell for this mint, average + p95)
        lats = lats_by_mint.get(m, [])
        buy_lats = [int(x["lat"]) for x in lats if x["side"] == "buy"]
        sell_lats = [int(x["lat"]) for x in lats if x["side"] == "sell"]
        e["buy_lat_avg"] = (sum(buy_lats) / len(buy_lats)) if buy_lats else None
        e["sell_lat_avg"] = (sum(sell_lats) / len(sell_lats)) if sell_lats else None
        e["buy_lat_max"] = max(buy_lats) if buy_lats else None
        e["sell_lat_max"] = max(sell_lats) if sell_lats else None
        # cast numerics
        e["amount_f"] = float(e["amount"])
        e["qt_f"] = float(e["qt"])
        e["imm_f"] = float(e["imm"])
        e["ipnl_f"] = float(e["ipnl"])
        e["imp_f"] = float(e["imp"])
        e["age_i"] = int(e["age"])
        e["sb_i"] = int(e["sb"])
        e["st_f"] = float(e["st"])
        e["fqms_i"] = int(e["fqms"]) if e["fqms"].lstrip("-").isdigit() else None
    return {
        "entries": entries,
        "bypass_count": len(bypass_by_mint),
        "sell_count": sum(len(v) for v in sells_by_mint.values()),
    }


def quarantine_check(e: dict[str, Any]) -> dict[str, Any]:
    sb = e["sb_i"]
    st = e["st_f"]
    rule = e["entry_kind"]
    lane = (e.get("lab") or {}).get("lane", "")
    reasons = []
    # 1. slot_buyers <= 1
    if sb <= 1:
        reasons.append("slot_buyers_le_1")
    # 2. slot_top >= 0.90
    if st >= 0.90:
        reasons.append("slot_top_ge_0.90")
    # 3. lane=raw_momentum AND slot_top>=0.80
    if lane == "raw_momentum_shadow" and st >= 0.80:
        reasons.append("raw_momentum_and_slot_top_ge_0.80")
    # 4. lane=raw_momentum AND slot_buyers<2
    if lane == "raw_momentum_shadow" and sb < 2:
        reasons.append("raw_momentum_and_slot_buyers_lt_2")
    # 5. scalp AND slot_top>=0.90
    if rule == "scalp" and st >= 0.90:
        reasons.append("scalp_and_slot_top_ge_0.90")
    # 6. scalp AND slot_buyers<=1
    if rule == "scalp" and sb <= 1:
        reasons.append("scalp_and_slot_buyers_le_1")
    quarantined = len(reasons) > 0
    return {"quarantined": quarantined, "reasons": reasons}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    args = ap.parse_args()
    result = parse_log(Path(args.log))
    entries = result["entries"]
    print("V36B_ENTRY_FORENSIC_TABLE")
    print(f"total entries: {len(entries)}")
    print(f"bypass events: {result['bypass_count']}")
    print()
    header = (
        f"{'#':<3} {'mint':<14} {'kind':<6} {'lane':<24} {'bypass':<6} {'all_in':>9} "
        f"{'close':>10} {'reason':<22} {'sb':>3} {'top':>5} {'buy_lat':>7} {'sell_lat':>8} {'quarantined':<11}"
    )
    print(header)
    print("-" * len(header))
    losers = []
    winners = []
    for i, e in enumerate(entries, start=1):
        mint = e["mint"][:14]
        kind = e["entry_kind"]
        lane = (e.get("lab") or {}).get("lane", "?")[:24]
        bypass = "yes" if e.get("bypass") else "-"
        all_in_close = float(e["sell"]["all_in"]) if e.get("sell") else 0.0
        reason = e["sell"]["reason"][:22] if e.get("sell") else "?"
        all_in_open_est = float(e["imm"]) - 0.000020 - float(e["amount"])  # approx route-aware
        sb = e["sb_i"]
        st = e["st_f"]
        bl = e["buy_lat_max"] if e["buy_lat_max"] is not None else "-"
        sl = e["sell_lat_max"] if e["sell_lat_max"] is not None else "-"
        q = quarantine_check(e)
        qmark = "QUARANTINE" if q["quarantined"] else "ok"
        print(f"{i:<3} {mint:<14} {kind:<6} {lane:<24} {bypass:<6} {all_in_open_est:>+9.6f} {all_in_close:>+10.6f} {reason:<22} {sb:>3d} {st:>5.3f} {str(bl):>7s} {str(sl):>8s} {qmark:<11}")
        e["_q"] = q
        if all_in_close is not None and all_in_close < 0:
            losers.append(e)
        elif all_in_close is not None:
            winners.append(e)
    print()
    print("DISCRIMINATOR ANALYSIS")
    print(f"losers : {len(losers)}")
    print(f"winners: {len(winners)}")
    print()
    for label, group in (("LOSERS", losers), ("WINNERS", winners)):
        if not group:
            continue
        sbs = [g["sb_i"] for g in group]
        sts = [g["st_f"] for g in group]
        print(f"{label}:")
        print(f"  slot_buyers: min={min(sbs)} max={max(sbs)} mean={sum(sbs)/len(sbs):.2f}")
        print(f"  slot_top:    min={min(sts):.3f} max={max(sts):.3f} mean={sum(sts)/len(sts):.3f}")
        # quarantine hit rates
        qh = sum(1 for g in group if g["_q"]["quarantined"])
        print(f"  quarantined under proposed rules: {qh}/{len(group)}")
        for g in group:
            mint = g["mint"][:12]
            sb = g["sb_i"]; st = g["st_f"]
            all_in_close = float(g["sell"]["all_in"]) if g.get("sell") else 0
            q = g["_q"]
            reasons = ",".join(q["reasons"]) if q["reasons"] else "-"
            print(f"    {mint} sb={sb} top={st:.3f} close={all_in_close:+.6f} q={'YES' if q['quarantined'] else 'no '} reasons={reasons}")
    print()
    # Ablation: which guard would kill the loser without killing winners?
    print("ABLATION (apply each rule alone, count loser blocks vs winner blocks)")
    ablations = [
        ("slot_top>=0.90 AND slot_buyers<=1", lambda e: e["st_f"] >= 0.90 and e["sb_i"] <= 1),
        ("slot_top>=0.90", lambda e: e["st_f"] >= 0.90),
        ("slot_buyers<=1", lambda e: e["sb_i"] <= 1),
        ("slot_top>=0.80 AND slot_buyers<=1", lambda e: e["st_f"] >= 0.80 and e["sb_i"] <= 1),
        ("scalp AND slot_top>=0.90", lambda e: e["entry_kind"] == "scalp" and e["st_f"] >= 0.90),
        ("raw_momentum AND slot_buyers<=1", lambda e: (e.get("lab") or {}).get("lane") == "raw_momentum_shadow" and e["sb_i"] <= 1),
        ("raw_momentum AND (slot_top>=0.90 OR slot_buyers<=1)", lambda e: (e.get("lab") or {}).get("lane") == "raw_momentum_shadow" and (e["st_f"] >= 0.90 or e["sb_i"] <= 1)),
    ]
    for desc, fn in ablations:
        l_block = sum(1 for g in losers if fn(g))
        w_block = sum(1 for g in winners if fn(g))
        winner_blocks_detail = ", ".join(g["mint"][:8] for g in winners if fn(g))
        verdict = "GOOD" if (l_block == len(losers) and w_block == 0) else f"check (loses {l_block}/{len(losers)} losers, blocks {w_block}/{len(winners)} winners)"
        print(f"  {desc:60s}  losers_blocked={l_block}/{len(losers)}  winners_blocked={w_block}/{len(winners)}  {verdict}{(' [' + winner_blocks_detail + ']') if winner_blocks_detail else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
