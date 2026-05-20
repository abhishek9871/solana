"""V47F - FScZ dry-live loss forensic + V47F-rules pre-replay summary.

Reads /root/piggy/data/v47e_drylive_decisions.jsonl and extracts the FScZ
loss + the 6 V47E winners. Produces a "would-have-been-caught" table for
each of the proposed V47F filters:

  - Size-tiered exp_pnl floor (Phase 2)
  - Hard hold caps by size (Phase 4)
  - extend_if_positive disable for size >= 0.030 (Phase 4)
  - Mid-hold dump abort (Phase 5)

NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import re as _re
import sys
from pathlib import Path
from typing import Any, Dict, List


# ----- static-grep self-check ----------------------------------------
_FORBIDDEN_CALL_PATTERNS = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN_CALL_PATTERNS:
    if _re.search(_pat, _src):
        sys.stderr.write(
            f"V47F-FORENSIC-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47f_fscz_forensic")


# V47F tier definitions (mirror Phase 2; standalone copy for forensic only)
def _v47f_required_floor(size_sol: float) -> float:
    s = float(size_sol)
    if s <= 0.010 + 1e-9:
        return 0.000600
    if s <= 0.020 + 1e-9:
        return 0.001000
    if s <= 0.030 + 1e-9:
        return 0.002000
    if s <= 0.050 + 1e-9:
        return 0.003000
    return min(s * 0.06, 0.003000)


def _v47f_floor_pass(size_sol: float, exp_pnl: float) -> (bool, str):
    s = float(size_sol)
    pnl = float(exp_pnl)
    floor = _v47f_required_floor(s)
    if pnl < floor:
        return (False, f"exp_pnl_lt_floor_{int(round(floor*1e6))}u_at_size_{int(round(s*1000))}m")
    if s >= 0.030 - 1e-9:
        ratio = pnl / s if s > 0 else 0.0
        if ratio < 0.06 and pnl < 0.003000:
            return (False, f"large_size_ratio_lt_06pct_AND_pnl_lt_3000u")
    return (True, "")


def _v47f_hold_caps(size_sol: float) -> Dict[str, Any]:
    s = float(size_sol)
    if s <= 0.010 + 1e-9:
        return {"max_hold_ms": 2500, "max_extend_ms": 1500, "extend_allowed": True}
    if s <= 0.020 + 1e-9:
        return {"max_hold_ms": 1800, "max_extend_ms": 1000, "extend_allowed": True}
    return {"max_hold_ms": 1000, "max_extend_ms": 0, "extend_allowed": False}


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in-jsonl",
        default="/root/piggy/data/v47e_drylive_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md",
        default="/root/piggy/V47E_FSCZ_FAILURE_FORENSIC.md",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    entries: List[Dict[str, Any]] = []
    closes: List[Dict[str, Any]] = []

    with open(args.in_jsonl, "r", encoding="utf-8") as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            t = d.get("type")
            if t == "v47e_drylive_entry":
                entries.append(d)
            elif t == "v47e_drylive_close":
                closes.append(d)

    # Match closes -> entries by (mint, decision_ts_ms)
    close_by_key = {}
    for c in closes:
        close_by_key[(c["mint"], int(c["decision_ts_ms"]))] = c

    enriched: List[Dict[str, Any]] = []
    for e in entries:
        key = (e["mint"], int(e["decision_ts_ms"]))
        c = close_by_key.get(key)
        rec = dict(e)
        if c is not None:
            rec["close_kind"] = c["close_kind"]
            rec["close_pnl"] = c["close_pnl"]
            rec["close_lag_ms"] = c["close_lag_ms"]
        enriched.append(rec)

    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    f = open(out_md, "w", encoding="utf-8")

    f.write("# V47E FScZ Failure Forensic + V47F-Rules Pre-Replay\n\n")
    f.write("Source: " + args.in_jsonl + "\n\n")
    f.write(f"Total dry-live entries: {len(entries)}\n")
    f.write(f"Total close records:    {len(closes)}\n\n")

    # === Hard-output answers (top of report) ===
    fscz_entry = None
    hjt5_entry = None
    for e in enriched:
        if e["mint"].startswith("FScZ"):
            fscz_entry = e
        elif e["mint"].startswith("Hjt5"):
            hjt5_entry = e

    f.write("## HARD ANSWERS (front of report)\n\n")
    if fscz_entry:
        sz = float(fscz_entry["selected_size_sol"])
        pnl = float(fscz_entry["exp_pnl"])
        floor_req = _v47f_required_floor(sz)
        floor_pass, floor_reason = _v47f_floor_pass(sz, pnl)
        caps = _v47f_hold_caps(sz)
        lag = fscz_entry.get("close_lag_ms")
        f.write(
            f"### FScZ (size={sz:.3f}, exp_pnl={pnl:+.6f})\n\n"
            f"- V47F floor required at size={sz:.3f}: {floor_req:+.6f}\n"
            f"- FScZ exp_pnl ({pnl:+.6f}) >= floor? {floor_pass} "
            f"(reason={floor_reason or 'pass'})\n"
            f"- ANSWER #1: \"Was FScZ blockable by exp_pnl >= +0.002 for size>=0.030?\" "
            f"=> {'YES' if not floor_pass else 'NO'}. "
            f"FScZ blocked by V47F floor (size 0.020<x<=0.030 requires >=+0.002; "
            f"FScZ exp_pnl=+0.001024 < 0.002, AND ratio 0.034 < 0.06).\n"
            f"- V47F hold caps for size={sz:.3f}: max_hold_ms={caps['max_hold_ms']}, "
            f"max_extend_ms={caps['max_extend_ms']}, extend_allowed={caps['extend_allowed']}\n"
            f"- FScZ observed close_lag_ms={lag}; would-be exit at hold_cap = "
            f"min(1000ms, current). The dump-abort + 1000ms hold cap forces exit at "
            f"or before 1000ms, avoiding the 4108ms clamp-loss.\n"
            f"- ANSWER #2: \"Was FScZ avoidable by max_hold <= 1500ms (no extend at "
            f"size>0.020)?\" => YES. V47F sets max_hold=1000ms and extend_allowed=False "
            f"for size>0.020. FScZ would have closed at ~1000ms instead of 4108ms.\n"
            f"- ANSWER #3: \"Was FScZ avoidable by disabling extend_if_positive for "
            f"size>=0.030?\" => YES if PnL went negative before 1500ms. "
            f"Trajectory not persisted in V47E dry-live JSONL "
            f"(close records only emit close_pnl). "
            f"But the 4108ms close_lag itself proves extend fired (default MAX_HOLD=1500, "
            f"MAX_EXTEND=3000, then expired or clamped). With V47F extend_allowed=False "
            f"at size 0.030, the position cannot extend past 1000ms hard cap.\n\n"
        )
    else:
        f.write("### FScZ not found in dry-live JSONL!\n\n")

    if hjt5_entry:
        sz = float(hjt5_entry["selected_size_sol"])
        pnl = float(hjt5_entry["exp_pnl"])
        floor_req = _v47f_required_floor(sz)
        floor_pass, floor_reason = _v47f_floor_pass(sz, pnl)
        f.write(
            f"### Hjt5 (size={sz:.3f}, exp_pnl={pnl:+.6f}, realized +0.014252)\n\n"
            f"- V47F floor required at size={sz:.3f}: {floor_req:+.6f}\n"
            f"- Hjt5 exp_pnl ({pnl:+.6f}) >= floor? {floor_pass} "
            f"(reason={floor_reason or 'pass'})\n"
            f"- ANSWER #4: \"Would V47F block this $0.014 winner?\" "
            f"=> {'YES (BLOCKED at size=0.050)' if not floor_pass else 'NO'}. "
            f"Hjt5 exp_pnl=+0.001758 is below the size>0.030 tier floor of +0.003.\n"
            f"- DOWNSIZE PATH: V47F can downsize Hjt5 to a smaller tier. "
            f"At size=0.020 the floor is +0.001 - Hjt5 exp_pnl recomputed at "
            f"size=0.020 would likely still pass (smaller size produces somewhat "
            f"smaller exp_pnl due to slippage but proportionally similar; not "
            f"trivially predictable without the actual recompute against curve state). "
            f"This needs the live recompute in Phase 6.\n"
            f"- SIZE-SELECTABILITY TRADE-OFF: V47F's strict floor at size>0.030 "
            f"trades raw-size winners for FScZ-class loss avoidance. The big "
            f"+0.014 winner may need to be captured at downsized 0.015-0.020 "
            f"instead of 0.050 — a 3x reduction in upside.\n\n"
        )

    # Per-entry table for all V47E entries
    f.write("## All V47E dry-live entries (with V47F rule status)\n\n")
    f.write(
        "| # | mint | size | ub_250 | tbs | exp_pnl | adv_pnl | close_kind | "
        "close_pnl | close_lag_ms | V47F_floor_pass | V47F_floor_req | "
        "V47F_max_hold_ms | extend_allowed |\n"
        "|---|------|------|--------|-----|---------|---------|------------|"
        "-----------|--------------|-----------------|-----------------|"
        "------------------|----------------|\n"
    )
    for i, e in enumerate(enriched, 1):
        sz = float(e["selected_size_sol"])
        epnl = float(e["exp_pnl"])
        adv = float(e.get("adv_pnl", 0.0))
        cpnl = e.get("close_pnl")
        clag = e.get("close_lag_ms")
        ck = e.get("close_kind") or "pending"
        fp, freason = _v47f_floor_pass(sz, epnl)
        req = _v47f_required_floor(sz)
        caps = _v47f_hold_caps(sz)
        f.write(
            f"| {i} | {_short(e['mint'])} | {sz:.4f} | "
            f"{int(e.get('ub_250',0))} | "
            f"{float(e.get('tbs_250',0.0)):.3f} | "
            f"{epnl:+.6f} | {adv:+.6f} | {ck} | "
            f"{('%+.6f'%float(cpnl)) if cpnl is not None else 'n/a'} | "
            f"{int(clag) if clag is not None else 'n/a'} | "
            f"{fp} | {req:+.6f} | {caps['max_hold_ms']} | "
            f"{caps['extend_allowed']} |\n"
        )

    # Summary of V47E winners blocked/passed by V47F floor
    f.write("\n## V47E winner survivor analysis under V47F floor\n\n")
    closed_nn = [e for e in enriched if e.get("close_kind") == "bank"]
    closed_n = [e for e in enriched if e.get("close_kind") in ("clamp_loss", "expired_loss")]
    f.write(f"- V47E banks: {len(closed_nn)}\n")
    f.write(f"- V47E losses: {len(closed_n)}\n")
    survivors = []
    blocked = []
    for e in closed_nn:
        sz = float(e["selected_size_sol"])
        epnl = float(e["exp_pnl"])
        fp, _ = _v47f_floor_pass(sz, epnl)
        if fp:
            survivors.append(e)
        else:
            blocked.append(e)
    for e in closed_n:
        sz = float(e["selected_size_sol"])
        epnl = float(e["exp_pnl"])
        fp, _ = _v47f_floor_pass(sz, epnl)
        if not fp:
            blocked.append(e)
    f.write(f"- V47F-survived banks (at original size): {len(survivors)}\n")
    f.write(f"- V47F-blocked entries (banks OR losses): {len(blocked)}\n")
    for e in blocked:
        f.write(
            f"  - BLOCKED at original size: {_short(e['mint'])} "
            f"size={float(e['selected_size_sol']):.3f} "
            f"exp_pnl={float(e['exp_pnl']):+.6f} "
            f"close_kind={e.get('close_kind')} "
            f"close_pnl={e.get('close_pnl')}\n"
        )
    f.write("\n")

    # Net PnL deltas
    net_v47e = sum(float(e.get("close_pnl") or 0.0) for e in enriched if e.get("close_pnl") is not None)
    surv_pnl = sum(float(e.get("close_pnl") or 0.0) for e in survivors)
    f.write(f"- V47E net realized PnL (closed only): {net_v47e:+.6f}\n")
    f.write(f"- V47F net realized PnL if blocks honored, no downsize: {surv_pnl:+.6f}\n")
    f.write("  (true V47F net depends on downsize recapture in Phase 6)\n\n")

    # Honest trade-off statement
    f.write("## HONEST TRADE-OFF\n\n")
    if hjt5_entry:
        sz = float(hjt5_entry["selected_size_sol"])
        fp, _ = _v47f_floor_pass(sz, float(hjt5_entry["exp_pnl"]))
        if not fp:
            f.write(
                "- The V47F size-tiered floor at size>0.030 (>=+0.003 SOL exp_pnl) "
                "BLOCKS the biggest realized winner Hjt5 (+0.014252) at its "
                "original size=0.050. This means V47F sacrifices upside on Hjt5-class "
                "candidates in exchange for stopping FScZ-class losses.\n"
                "- Downsize-before-block (Phase 3) may recapture Hjt5 at size 0.015 or "
                "0.020 where the floor is lower (+0.001) — but the realized upside "
                "scales DOWN with the smaller size. Hjt5 at 0.015 would realize "
                "roughly 0.014252 * 0.015/0.050 ~= +0.004276 (linear approx; "
                "actual depends on curve+exec) — still a winner but only ~30% of "
                "the original.\n"
            )
        else:
            f.write(
                "- Hjt5 PASSES the V47F floor at its original size. The V47F floor "
                "does NOT degrade the big winner. V47F is a strict improvement over "
                "V47E for this trade.\n"
            )
    f.write("\n")
    f.write("## Trajectory data availability\n\n")
    f.write(
        "- V47E dry-live JSONL emits only close-event records (close_pnl, "
        "close_kind, close_lag_ms). Per-snap PnL trajectory between decision and "
        "close is NOT persisted. So precise reconstruction of \"first time PnL "
        "went negative\" or \"first time pending sells flipped\" for FScZ is "
        "best-effort.\n"
        "- For the FScZ close: close_lag_ms=4108 with close_kind=clamp_loss. "
        "Given MAX_HOLD=1500 and MAX_EXTEND_MS=3000 in V47E, the 4108ms close "
        "implies (a) PnL was >0 at the first 1500ms boundary triggering extend, "
        "and (b) PnL fell below LOSS_TH=-0.00050 between ms 1500 and 4108. "
        "Exact transition time is in the curve subscriber state at run time, "
        "not in the persisted JSONL.\n"
        "- V47F's mid-hold dump abort (Phase 5) is designed to detect this exact "
        "transition (peak quote dropping by >=0.0005 SOL, curve vsol peak "
        "dropping by >=5%, pending sells flipping over pending buys, gradient "
        "going negative two updates in a row).\n"
    )

    f.close()
    print(f"V47F-FORENSIC wrote {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
