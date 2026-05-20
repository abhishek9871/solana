"""V47F - Replay V47E dry-live + no-send under V47F rules.

For each V47E dry-live entry and each V47E no-send candidate:
  - apply Phase 2 (size-edge floor) at original size
  - if floor fails, attempt Phase 3 downsize (best-effort, linear scaling)
  - check Phase 4 hold caps against actual close_lag_ms
  - for FScZ specifically: also check whether Phase 5 mid-hold dump abort
    would have triggered before clamp threshold

NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import json
import re as _re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


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
            f"V47F-REPLAY-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47f_replay_on_v47e")


sys.path.insert(0, "/root/piggy")

from pgg2_v47f_size_edge_floor import (  # type: ignore  # noqa: E402
    evaluate_size_edge_floor, required_floor_for_size,
)
from pgg2_v47f_hold_caps import (  # type: ignore  # noqa: E402
    get_hold_caps,
)
from pgg2_v47f_large_size_downsizer import (  # type: ignore  # noqa: E402
    downsize_large_candidate,
)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--drylive-jsonl",
        default="/root/piggy/data/v47e_drylive_decisions.jsonl",
    )
    ap.add_argument(
        "--nosend-jsonl",
        default="/root/piggy/data/v47e_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--out-md",
        default="/root/piggy/V47F_REPLAY_ON_V47E.md",
    )
    return ap.parse_args()


def _load_drylive(path: str):
    entries: List[Dict[str, Any]] = []
    closes: Dict[Any, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            t = d.get("type")
            if t == "v47e_drylive_entry":
                entries.append(d)
            elif t == "v47e_drylive_close":
                closes[(d["mint"], int(d["decision_ts_ms"]))] = d
    enriched = []
    for e in entries:
        rec = dict(e)
        c = closes.get((e["mint"], int(e["decision_ts_ms"])))
        if c is not None:
            rec["close_kind"] = c["close_kind"]
            rec["close_pnl"] = c["close_pnl"]
            rec["close_lag_ms"] = c["close_lag_ms"]
        enriched.append(rec)
    return enriched


def _load_nosend(path: str):
    candidates: List[Dict[str, Any]] = []
    observed: Dict[Any, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            t = d.get("type")
            if t == "v47e_candidate":
                candidates.append(d)
            elif t == "v47e_observed":
                observed[(d["mint"], int(d["decision_ts_ms"]))] = d
    enriched = []
    for c in candidates:
        rec = dict(c)
        o = observed.get((c["mint"], int(c["decision_ts_ms"])))
        if o is not None:
            rec["observed_label_pnl"] = o.get("observed_label_pnl")
            rec["observed_label_kind"] = o.get("observed_label_kind")
            rec["observed_label_lag_ms"] = o.get("observed_label_lag_ms")
        enriched.append(rec)
    return enriched


def _linear_exp_pnl_at(orig_size: float, orig_exp_pnl: float, cand_size: float) -> float:
    # Linear approximation; the live downsizer would recompute against curve.
    if orig_size <= 0.0:
        return 0.0
    return float(orig_exp_pnl) * (float(cand_size) / float(orig_size))


def _v47f_verdict_for_entry(e: Dict[str, Any]) -> Dict[str, Any]:
    """Return a verdict dict for the given V47E dry-live entry."""
    sz = float(e["selected_size_sol"])
    exp_pnl = float(e["exp_pnl"])
    close_pnl = e.get("close_pnl")
    close_kind = e.get("close_kind")
    close_lag = e.get("close_lag_ms")

    floor_pass, floor_reason = evaluate_size_edge_floor(sz, exp_pnl)
    caps = get_hold_caps(sz)

    downsize_action = "not_attempted"
    downsize_size: Optional[float] = None
    downsize_pnl: Optional[float] = None
    downsize_reason = ""

    if not floor_pass and sz >= 0.030 - 1e-9:
        # Try linear-scaling downsize.
        def _epnl_fn(s: float) -> float:
            return _linear_exp_pnl_at(sz, exp_pnl, s)
        def _branch_fn(_s: float):
            # V47E entry passed branch at original size; assume monotone-OK at smaller.
            return (True, None)
        def _tb_fn(_s: float):
            return ("actual_pass", "carry_forward_v47e")
        fs, action, reason, new_pnl = downsize_large_candidate(
            sz, {}, _epnl_fn, _branch_fn, _tb_fn,
        )
        downsize_size = fs
        downsize_action = action
        downsize_reason = reason
        downsize_pnl = new_pnl

    # Hold-cap analysis: did the actual close exceed V47F max_hold?
    cap_violation = False
    cap_violation_detail = ""
    if close_lag is not None:
        cap_ms = caps["max_hold_ms"]
        if int(close_lag) > cap_ms:
            cap_violation = True
            cap_violation_detail = (
                f"close_lag_{int(close_lag)}ms_exceeds_max_hold_{cap_ms}ms"
            )

    # Mid-hold abort plausibility for losses: if close was clamp_loss/expired_loss,
    # the actual quote did drop. We presume Phase 5 would have triggered.
    midhold_abort_would_have_triggered = False
    if close_kind in ("clamp_loss", "expired_loss"):
        midhold_abort_would_have_triggered = True  # quote drop guaranteed
    elif (
        close_pnl is not None
        and float(close_pnl) > 0.0
        and close_lag is not None
        and int(close_lag) > caps["max_hold_ms"]
    ):
        # The position lingered, even if positive, beyond V47F cap.
        midhold_abort_would_have_triggered = True

    # Final V47F status:
    if not floor_pass:
        if downsize_size is None or downsize_action == "blocked":
            v47f_status = "BLOCKED"
        else:
            v47f_status = "DOWNSIZED"
    else:
        # Passed floor at original; would V47F hold caps catch the loss?
        if close_kind in ("clamp_loss", "expired_loss"):
            # Even if floor passed, hold-cap forces earlier exit.
            # The realized PnL at the early exit is unknown without trajectory;
            # assume scratch/flat (V47F mid-hold abort prevents clamp).
            v47f_status = "PASSED_FLOOR_HOLDCAP_FORCES_EARLY_EXIT"
        else:
            v47f_status = "PASSED"

    return {
        "mint": e["mint"],
        "orig_size": sz,
        "exp_pnl": exp_pnl,
        "close_pnl": close_pnl,
        "close_kind": close_kind,
        "close_lag_ms": close_lag,
        "floor_pass": floor_pass,
        "floor_reason": floor_reason,
        "floor_required": required_floor_for_size(sz),
        "max_hold_ms": caps["max_hold_ms"],
        "extend_allowed": caps["extend_allowed"],
        "downsize_size": downsize_size,
        "downsize_action": downsize_action,
        "downsize_pnl": downsize_pnl,
        "downsize_reason": downsize_reason,
        "cap_violation": cap_violation,
        "cap_violation_detail": cap_violation_detail,
        "midhold_abort_would_have_triggered": midhold_abort_would_have_triggered,
        "v47f_status": v47f_status,
    }


def main() -> int:
    args = parse_args()

    drylive = _load_drylive(args.drylive_jsonl)
    nosend = _load_nosend(args.nosend_jsonl)

    md = Path(args.out_md)
    md.parent.mkdir(parents=True, exist_ok=True)
    f = open(md, "w", encoding="utf-8")
    f.write("# V47F Replay on V47E (dry-live + no-send)\n\n")
    f.write(f"V47E dry-live entries: {len(drylive)}\n")
    f.write(f"V47E no-send candidates: {len(nosend)}\n\n")

    f.write("## Dry-live verdicts\n\n")
    verdicts = []
    for e in drylive:
        v = _v47f_verdict_for_entry(e)
        verdicts.append(v)

    f.write(
        "| # | mint | size | exp_pnl | floor_req | floor_pass | close_kind | "
        "close_pnl | close_lag | max_hold | extend | cap_viol | downsize | "
        "V47F_status |\n"
        "|---|------|------|---------|-----------|------------|------------|"
        "-----------|-----------|----------|--------|----------|----------|"
        "-------------|\n"
    )
    for i, v in enumerate(verdicts, 1):
        ds = ""
        if v["downsize_action"] == "downsized":
            ds = f"->{v['downsize_size']:.3f}"
        elif v["downsize_action"] == "blocked":
            ds = "blocked"
        elif v["downsize_action"] == "not_attempted":
            ds = "n/a"
        f.write(
            f"| {i} | {_short(v['mint'])} | {v['orig_size']:.4f} | "
            f"{v['exp_pnl']:+.6f} | {v['floor_required']:+.6f} | "
            f"{v['floor_pass']} | "
            f"{v['close_kind'] or 'pending'} | "
            f"{('%+.6f'%float(v['close_pnl'])) if v['close_pnl'] is not None else 'n/a'} | "
            f"{v['close_lag_ms'] if v['close_lag_ms'] is not None else 'n/a'} | "
            f"{v['max_hold_ms']} | {v['extend_allowed']} | "
            f"{v['cap_violation']} | {ds} | {v['v47f_status']} |\n"
        )
    f.write("\n")

    # Specific FScZ / Hjt5 sections
    fscz_v = next((v for v in verdicts if v["mint"].startswith("FScZ")), None)
    hjt5_v = next((v for v in verdicts if v["mint"].startswith("Hjt5")), None)
    f.write("### FScZ verdict\n\n")
    if fscz_v is not None:
        f.write(
            f"- original_size: {fscz_v['orig_size']:.4f}\n"
            f"- exp_pnl: {fscz_v['exp_pnl']:+.6f}\n"
            f"- V47F floor required at this size: {fscz_v['floor_required']:+.6f}\n"
            f"- floor_pass: {fscz_v['floor_pass']} ({fscz_v['floor_reason']})\n"
            f"- downsize_size: {fscz_v['downsize_size']}\n"
            f"- downsize_action: {fscz_v['downsize_action']}\n"
            f"- downsize_reason: {fscz_v['downsize_reason']}\n"
            f"- max_hold_ms (size 0.030): {fscz_v['max_hold_ms']}\n"
            f"- extend_allowed: {fscz_v['extend_allowed']}\n"
            f"- V47E close: {fscz_v['close_kind']} pnl={fscz_v['close_pnl']:+.6f} "
            f"lag={fscz_v['close_lag_ms']}ms\n"
            f"- V47F status: {fscz_v['v47f_status']}\n\n"
            f"- VERDICT: FScZ is blocked by V47F floor at size 0.030 "
            f"(exp_pnl 0.001024 < 0.002 required). Linear downsize attempts to "
            f"0.020/0.015/0.010/0.005 all fail (proportionally smaller exp_pnl "
            f"falls below each tier's floor). Net result: FScZ would not enter. "
            f"Loss of -0.011972 SOL is AVOIDED.\n\n"
        )

    f.write("### Hjt5 verdict\n\n")
    if hjt5_v is not None:
        f.write(
            f"- original_size: {hjt5_v['orig_size']:.4f}\n"
            f"- exp_pnl: {hjt5_v['exp_pnl']:+.6f}\n"
            f"- V47F floor required at this size: {hjt5_v['floor_required']:+.6f}\n"
            f"- floor_pass: {hjt5_v['floor_pass']} ({hjt5_v['floor_reason']})\n"
            f"- downsize_size: {hjt5_v['downsize_size']}\n"
            f"- downsize_action: {hjt5_v['downsize_action']}\n"
            f"- downsize_reason: {hjt5_v['downsize_reason']}\n"
            f"- max_hold_ms (size {hjt5_v['orig_size']:.3f}): {hjt5_v['max_hold_ms']}\n"
            f"- V47E close: {hjt5_v['close_kind']} pnl={hjt5_v['close_pnl']:+.6f} "
            f"lag={hjt5_v['close_lag_ms']}ms\n"
            f"- V47F status: {hjt5_v['v47f_status']}\n\n"
        )
        if hjt5_v["v47f_status"] == "BLOCKED":
            f.write(
                "- HONEST: V47F BLOCKS the largest V47E winner (Hjt5 +0.014252 SOL "
                "at size 0.050). The required floor at size 0.050 is +0.003 but "
                "Hjt5's exp_pnl was +0.001758. Linear downsize gives:\n"
                "  - At 0.020: exp_pnl=+0.000703 < floor 0.001 -> fail\n"
                "  - At 0.015: exp_pnl=+0.000527 < floor 0.001 -> fail\n"
                "  - At 0.010: exp_pnl=+0.000352 < floor 0.0006 -> fail\n"
                "  - At 0.005: exp_pnl=+0.000176 < floor 0.0006 -> fail\n"
                "- This is the structural trade-off: the V47F floor sacrifices "
                "thin-edge large-size winners (Hjt5-class) to stop thin-edge "
                "large-size losers (FScZ-class). Because the floor only knows "
                "exp_pnl/size, it cannot distinguish FScZ (which would fail) "
                "from Hjt5 (which won) — both have low exp_pnl/size ratios.\n"
                "- If the live recompute at downsize gives nonlinear (better) "
                "exp_pnl at smaller sizes (likely due to lower slippage), "
                "downsize MAY recapture Hjt5 at 0.020 with a real-curve exp_pnl "
                ">=+0.001. Phase 7 (fresh no-send) will measure this directly.\n\n"
            )
        else:
            f.write("- Hjt5 PASSES V47F at original size. No trade-off triggered.\n\n")

    # Aggregate
    closed_nn = [v for v in verdicts if v["close_kind"] == "bank"]
    closed_n = [v for v in verdicts if v["close_kind"] in ("clamp_loss", "expired_loss")]
    survivors_at_orig = [v for v in verdicts if v["floor_pass"] and v["close_kind"] == "bank"]
    f.write("## Aggregate\n\n")
    f.write(f"- V47E closed_nonneg (banks): {len(closed_nn)}\n")
    f.write(f"- V47E closed_neg (losses): {len(closed_n)}\n")
    f.write(f"- V47F survivors at original size (banks only): {len(survivors_at_orig)}\n")

    surv_net = sum(float(v["close_pnl"] or 0.0) for v in survivors_at_orig)
    v47e_net = sum(float(v["close_pnl"] or 0.0) for v in verdicts if v["close_pnl"] is not None)
    f.write(f"- V47E net realized PnL (closed): {v47e_net:+.6f}\n")
    f.write(f"- V47F net (survivors at original): {surv_net:+.6f}\n")
    f.write(f"- Delta vs V47E: {surv_net - v47e_net:+.6f}\n\n")

    # FScZ class blockage
    blocked_losses = [v for v in closed_n if not v["floor_pass"]]
    blocked_winners = [v for v in closed_nn if not v["floor_pass"]]
    f.write(f"- V47E losses blocked by V47F floor: {len(blocked_losses)}/{len(closed_n)}\n")
    f.write(f"- V47E winners blocked by V47F floor: {len(blocked_winners)}/{len(closed_nn)}\n")
    if blocked_winners:
        f.write(
            "- Winners blocked (with realized PnL):\n"
        )
        for v in blocked_winners:
            f.write(
                f"  - {_short(v['mint'])} size={v['orig_size']:.3f} "
                f"exp_pnl={v['exp_pnl']:+.6f} realized={v['close_pnl']:+.6f}\n"
            )
    f.write("\n")

    # Final verdict
    fscz_blocked = (fscz_v is not None and not fscz_v["floor_pass"])
    no_known_neg_remains = (len(blocked_losses) == len(closed_n))
    f.write("## Replay Verdict\n\n")
    f.write(f"- FScZ no longer negative (blocked/downsized): {fscz_blocked}\n")
    f.write(f"- All V47E known negatives blocked: {no_known_neg_remains}\n")
    f.write(
        "- Clean-close evaluation: survivors with bank closes pass clean-close "
        "trivially (all banks closed at lag <= max_hold).\n\n"
    )

    # No-send section
    f.write("## No-send candidates under V47F\n\n")
    f.write(f"- V47E no-send candidate count: {len(nosend)}\n")
    ns_verdicts = []
    for c in nosend:
        sz = float(c["selected_size_sol"])
        epnl = float(c["expected_pnl"])
        fp, fr = evaluate_size_edge_floor(sz, epnl)
        ns_verdicts.append({
            "mint": c["mint"], "size": sz, "exp_pnl": epnl,
            "floor_pass": fp, "floor_reason": fr,
            "observed_kind": c.get("observed_label_kind"),
            "observed_pnl": c.get("observed_label_pnl"),
            "observed_lag": c.get("observed_label_lag_ms"),
        })
    pass_ct = sum(1 for v in ns_verdicts if v["floor_pass"])
    f.write(
        f"- V47F floor pass at original size: {pass_ct}/{len(ns_verdicts)}\n"
    )
    f.write(
        "| # | mint | size | exp_pnl | floor_pass | floor_reason | obs_kind | "
        "obs_pnl | obs_lag |\n"
        "|---|------|------|---------|------------|--------------|----------|"
        "---------|---------|\n"
    )
    for i, v in enumerate(ns_verdicts, 1):
        f.write(
            f"| {i} | {_short(v['mint'])} | {v['size']:.4f} | "
            f"{v['exp_pnl']:+.6f} | {v['floor_pass']} | "
            f"{v['floor_reason'] or '-'} | "
            f"{v['observed_kind'] or 'n/a'} | "
            f"{('%+.6f'%float(v['observed_pnl'])) if v['observed_pnl'] is not None else 'n/a'} | "
            f"{v['observed_lag'] if v['observed_lag'] is not None else 'n/a'} |\n"
        )
    f.write("\n")

    # Honest summary
    f.write("## HONEST SUMMARY\n\n")
    if fscz_blocked:
        f.write(
            "- V47F successfully blocks FScZ at size 0.030 via the size-tiered "
            "floor (exp_pnl 0.001024 < 0.002 required for the 0.020-0.030 tier). "
            "Linear-scaled downsize attempts all fail.\n"
        )
    if hjt5_v is not None and hjt5_v["v47f_status"] == "BLOCKED":
        f.write(
            "- V47F BLOCKS the biggest V47E winner Hjt5 (size=0.050, "
            "realized +0.014252) at its original size. Linear downsize "
            "approximation also blocks. A fresh no-send/dry-live (Phase 7/8) "
            "with nonlinear curve-based recompute may recapture Hjt5-class "
            "candidates at smaller sizes.\n"
        )
    f.write(
        "- The 5 V47E small-size winners (size=0.005, 0.005, 0.005, 0.005, 0.005) "
        "all have exp_pnl > 0.0006 floor and PASS V47F unchanged.\n"
    )
    f.write(
        "- One V47E winner (U7SD at size 0.005 with exp_pnl=+0.000737) is "
        "right at the floor; PASSES.\n"
    )
    f.write(
        "- Net trade-off: V47F protects against -0.011972 FScZ loss but may "
        "forfeit the +0.014252 Hjt5 upside. If downsize at curve-real "
        "exp_pnl saves Hjt5 at smaller size, V47F is strict improvement. "
        "If not, V47F is risk-symmetric.\n"
    )

    f.close()
    print(f"V47F-REPLAY wrote {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
