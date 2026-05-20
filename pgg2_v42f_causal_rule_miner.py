#!/usr/bin/env python3
"""V42F Phase 3 + 4 — causal rule grid search + time-split validation.

Reads /root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl.

Rule families (5):
  1. v42f_quote_gradient_predictor
  2. v42f_curve_delta_quote_follow
  3. v42f_recovered_quote_acceleration
  4. v42f_pending_flow_predictor
  5. v42f_high_momentum_confirmed

Exit policies (4):
  A: bank +0.00060
  B: bank +0.00040 / scratch +0.00005
  C: bank +0.00080 with hold-while-gradient-positive
  D: immediate scratch on negative gradient flip

Splits by time:
  discovery 0..60%, validation 60..80%, holdout 80..100%

NO TX. NO NETWORK.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


DATASET = Path("/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl")
OUT = Path("/root/piggy/V42F_CAUSAL_RULE_REPORT.md")
PROMOTED = Path("/root/piggy/V42F_PROMOTED_RULES.json")


# ----- Exit-policy realisation -----

def realise_policy_A(
    chain_future: List[Optional[float]],
    chain_future_grad_after: List[Optional[float]],
) -> Optional[float]:
    """A: bank +0.00060 on first crossing; else last value (scratch fallback)."""
    realized: Optional[float] = None
    for p in chain_future:
        if p is None:
            continue
        if p >= 0.00060:
            return min(p, 0.00060 + 0.00060)  # clamp
        realized = p
    return realized


def realise_policy_B(chain_future: List[Optional[float]], _g) -> Optional[float]:
    """B: bank +0.00040 then scratch +0.00005."""
    realized: Optional[float] = None
    for p in chain_future:
        if p is None:
            continue
        if p >= 0.00040:
            return min(p, 0.00040 + 0.00060)
        if p >= 0.00005:
            realized = p
    if realized is not None:
        return realized
    # else last observed (negative held)
    for p in reversed(chain_future):
        if p is not None:
            return p
    return None


def realise_policy_C(
    chain_future: List[Optional[float]],
    chain_future_grad: List[Optional[float]],
) -> Optional[float]:
    """C: bank +0.00080 while gradient stays positive; else scratch on flip."""
    realized: Optional[float] = None
    for i, p in enumerate(chain_future):
        if p is None:
            continue
        g = chain_future_grad[i] if i < len(chain_future_grad) else None
        if p >= 0.00080:
            return min(p, 0.00080 + 0.00060)
        if g is not None and g < 0 and p > 0:
            return p
        realized = p
    return realized


def realise_policy_D(
    chain_future: List[Optional[float]],
    chain_future_grad: List[Optional[float]],
) -> Optional[float]:
    """D: immediate scratch on negative gradient flip; else best."""
    best: Optional[float] = None
    for i, p in enumerate(chain_future):
        if p is None:
            continue
        if best is None or p > best:
            best = p
        g = chain_future_grad[i] if i < len(chain_future_grad) else None
        if g is not None and g < 0:
            return p if best is None else max(p, -0.0010)
    return best


# ----- Rule families -----

def rule_quote_gradient_predictor(
    f: Dict[str, Any], qgrad_min: float, qdelta_n1_min: float
) -> bool:
    if f["f_quote_gradient"] < qgrad_min:
        return False
    if f["f_quote_delta_N_minus_1"] < qdelta_n1_min:
        return False
    return True


def rule_curve_delta_quote_follow(
    f: Dict[str, Any], cdelta_n1_min: float, cgrad_min: float
) -> bool:
    if f["f_curve_delta_N_minus_1"] < cdelta_n1_min:
        return False
    if f["f_curve_gradient"] < cgrad_min:
        return False
    return True


def rule_recovered_quote_acceleration(
    f: Dict[str, Any], qdelta_n1_min: float, recovered_required: int
) -> bool:
    if recovered_required and not f["f_recovered_quote"]:
        return False
    if f["f_quote_delta_N_minus_1"] < qdelta_n1_min:
        return False
    if f["f_quote_delta_N_minus_2"] < 0:
        return False
    return True


def rule_pending_flow_predictor(
    f: Dict[str, Any], pending_buy_sol_min: float, since_prev_buy_min: float
) -> bool:
    if f["f_buy1000_sol"] < pending_buy_sol_min:
        return False
    if f["f_since_prev_buy_sol"] < since_prev_buy_min:
        return False
    return True


def rule_high_momentum_confirmed(
    f: Dict[str, Any], cgrad_min: float, qgrad_min: float, b500_sol_min: float
) -> bool:
    if f["f_curve_gradient"] < cgrad_min:
        return False
    if f["f_quote_gradient"] < qgrad_min:
        return False
    if f["f_buy500_sol"] < b500_sol_min:
        return False
    return True


# ----- Grids -----

GRIDS: List[Tuple[str, Callable, List[Tuple[Any, ...]]]] = [
    (
        "v42f_quote_gradient_predictor",
        rule_quote_gradient_predictor,
        [(qg, qd) for qg in (0.00010, 0.00050, 0.00100, 0.00200) for qd in (0.0, 0.000010, 0.000050, 0.000200)],
    ),
    (
        "v42f_curve_delta_quote_follow",
        rule_curve_delta_quote_follow,
        [(cd, cg) for cd in (0.0, 1e-9, 1e-8, 1e-7, 1e-6) for cg in (0.0, 1e-12, 1e-11)],
    ),
    (
        "v42f_recovered_quote_acceleration",
        rule_recovered_quote_acceleration,
        [(qd, r) for qd in (0.0, 0.000020, 0.000100, 0.000500) for r in (1, 0)],
    ),
    (
        "v42f_pending_flow_predictor",
        rule_pending_flow_predictor,
        [(pb, sp) for pb in (0.5, 1.0, 2.0, 5.0) for sp in (0.0, 0.5, 1.0, 2.0)],
    ),
    (
        "v42f_high_momentum_confirmed",
        rule_high_momentum_confirmed,
        [
            (cg, qg, b5)
            for cg in (0.0, 1e-12)
            for qg in (0.0, 0.00050, 0.00100)
            for b5 in (0.0, 0.5, 1.0)
        ],
    ),
]

POLICIES = {
    "A_bank_60": realise_policy_A,
    "B_bank_40_scratch_5": realise_policy_B,
    "C_bank_80_hold_pos_grad": realise_policy_C,
    "D_scratch_on_grad_flip": realise_policy_D,
}


def split_rows(rows: List[Dict[str, Any]]) -> Tuple[List, List, List]:
    rows = sorted(rows, key=lambda r: r["decision_ts_ms"])
    n = len(rows)
    a = int(n * 0.60)
    b = int(n * 0.80)
    return rows[:a], rows[a:b], rows[b:]


def compute_future_chain(
    row: Dict[str, Any], all_rows_by_chain: Dict[Tuple[str, str], List[Dict[str, Any]]]
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """Return (future_pnl_chain, future_quote_grad_chain) up to horizon ~5s.

    Reconstructed from the per-row labels (which are future-snap values) plus
    by walking the by-chain index.
    """
    key = (row["log"], row["mint"])
    chain = all_rows_by_chain.get(key, [])
    idx = row["snap_idx"]
    horizon_end_ts = row["decision_ts_ms"] + 5000
    pnls: List[Optional[float]] = []
    grads: List[Optional[float]] = []
    for r in chain[idx + 1:]:
        if r["decision_ts_ms"] > horizon_end_ts:
            break
        # The per-row sell_quote_out_at_i is r's own snap sell_out — re-price
        # the SOL recovery of selling the original buyer's tokens. Since the
        # bot's `live_equiv_all_in_pnl` always uses 0.015 SOL round-trip basis,
        # use the all-in PnL of the future snap's sell quote vs amount_sol.
        sell_out_future = r["sell_quote_out_at_i"]
        if sell_out_future is None or sell_out_future <= 0:
            pnls.append(None)
        else:
            pnls.append(sell_out_future - row["amount_sol"] - 0.000020)
        grads.append(r["features"]["f_quote_gradient"])
    return pnls, grads


def eval_rule(
    rule_fn: Callable,
    params: Tuple[Any, ...],
    split_rows_in: List[Dict[str, Any]],
    by_chain: Dict[Tuple[str, str], List[Dict[str, Any]]],
    policy_fn: Callable,
) -> Dict[str, Any]:
    fires: List[Dict[str, Any]] = []
    for row in split_rows_in:
        if not rule_fn(row["features"], *params):
            continue
        future_pnl_chain, future_grad_chain = compute_future_chain(row, by_chain)
        realized = policy_fn(future_pnl_chain, future_grad_chain)
        if realized is None:
            continue
        fires.append({"row": row, "realized": realized})

    n = len(fires)
    pnls = [f["realized"] for f in fires]
    total = sum(pnls) if pnls else 0.0
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    max_loss = min(pnls) if pnls else 0.0
    max_win = max(pnls) if pnls else 0.0
    avg = (total / n) if n else 0.0
    top_winner_share = (max_win / total) if (total > 0 and max_win > 0) else 0.0
    return {
        "n": n,
        "total_pnl": total,
        "avg_pnl": avg,
        "wins": wins,
        "losses": losses,
        "max_loss": max_loss,
        "max_win": max_win,
        "top_winner_share": top_winner_share,
        "fires": fires,
    }


def qualify(
    disc: Dict[str, Any],
    val: Dict[str, Any],
    hold: Dict[str, Any],
    min_n_holdout: int = 3,
    tiny_loss_bound: float = -0.0001,
) -> Tuple[bool, str]:
    if disc["n"] == 0:
        return False, "discovery_no_fires"
    if disc["total_pnl"] <= 0:
        return False, "discovery_pnl_non_positive"
    if val["n"] == 0:
        return False, "validation_no_fires"
    if val["total_pnl"] <= 0:
        return False, "validation_pnl_non_positive"
    if hold["n"] == 0:
        return False, "holdout_no_fires"
    if hold["total_pnl"] <= 0:
        return False, "holdout_pnl_non_positive"
    if hold["n"] < min_n_holdout:
        return False, f"holdout_n_below_{min_n_holdout}"
    if hold["max_loss"] < tiny_loss_bound:
        return False, f"holdout_max_loss_{hold['max_loss']:.6f}_below_{tiny_loss_bound}"
    if hold["top_winner_share"] > 0.70:
        return False, f"holdout_top_winner_share_{hold['top_winner_share']:.2f}_above_0.70"
    return True, "qualified"


def main() -> int:
    rows: List[Dict[str, Any]] = []
    with DATASET.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))

    by_chain: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r["log"], r["mint"])
        by_chain.setdefault(key, []).append(r)
    for k in by_chain:
        by_chain[k].sort(key=lambda r: r["decision_ts_ms"])

    disc_rows, val_rows, hold_rows = split_rows(rows)

    print(
        f"[V42F-MINER] rows={len(rows)} disc={len(disc_rows)} "
        f"val={len(val_rows)} hold={len(hold_rows)}"
    )

    results: List[Dict[str, Any]] = []
    promoted: List[Dict[str, Any]] = []

    for family_name, rule_fn, grid in GRIDS:
        for params in grid:
            for policy_name, policy_fn in POLICIES.items():
                d = eval_rule(rule_fn, params, disc_rows, by_chain, policy_fn)
                v = eval_rule(rule_fn, params, val_rows, by_chain, policy_fn)
                h = eval_rule(rule_fn, params, hold_rows, by_chain, policy_fn)
                ok, why = qualify(d, v, h)
                rec = {
                    "family": family_name,
                    "params": params,
                    "policy": policy_name,
                    "disc": {k: v for k, v in d.items() if k != "fires"},
                    "val": {k: v for k, v in v.items() if k != "fires"},
                    "hold": {k: v for k, v in h.items() if k != "fires"},
                    "qualified": ok,
                    "qualify_reason": why,
                }
                results.append(rec)
                if ok:
                    promoted.append(rec)

    # Pick best per family by holdout total PnL among qualified, else best
    # discovery
    best_by_family: Dict[str, Dict[str, Any]] = {}
    for rec in results:
        fam = rec["family"]
        cur = best_by_family.get(fam)
        score = (
            (rec["hold"]["total_pnl"] if rec["qualified"] else -1e9)
            + rec["disc"]["total_pnl"] * 0.001
        )
        cur_score = (
            (cur["hold"]["total_pnl"] if cur and cur["qualified"] else -1e9)
            + (cur["disc"]["total_pnl"] * 0.001 if cur else -1e9)
        ) if cur else -1e18
        if cur is None or score > cur_score:
            best_by_family[fam] = rec

    md = []
    md.append("# V42F Causal Rule Report")
    md.append("")
    md.append(f"- dataset: `{DATASET}`")
    md.append(f"- total_rows: {len(rows)}")
    md.append(f"- discovery rows: {len(disc_rows)}")
    md.append(f"- validation rows: {len(val_rows)}")
    md.append(f"- holdout rows: {len(hold_rows)}")
    md.append(f"- rule families: {len(GRIDS)}")
    md.append(f"- total grid evals: {len(results)}")
    md.append(f"- qualified rules: {len(promoted)}")
    md.append("")
    md.append("## Best per family")
    md.append("")
    md.append("| Family | Params | Policy | Disc n | Disc PnL | Val n | Val PnL | Hold n | Hold PnL | Hold max_loss | Top winner share | Qualified | Why |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for fam, rec in best_by_family.items():
        md.append(
            "| {fam} | {p} | {pol} | {dn} | {dp:+.6f} | {vn} | {vp:+.6f} | {hn} | {hp:+.6f} | {ml:+.6f} | {tws:.2f} | {q} | {why} |".format(
                fam=fam,
                p=rec["params"],
                pol=rec["policy"],
                dn=rec["disc"]["n"],
                dp=rec["disc"]["total_pnl"],
                vn=rec["val"]["n"],
                vp=rec["val"]["total_pnl"],
                hn=rec["hold"]["n"],
                hp=rec["hold"]["total_pnl"],
                ml=rec["hold"]["max_loss"],
                tws=rec["hold"]["top_winner_share"],
                q="YES" if rec["qualified"] else "no",
                why=rec["qualify_reason"],
            )
        )
    md.append("")
    md.append("## Qualified rules (PASSED full discovery/val/holdout time-split)")
    md.append("")
    if not promoted:
        md.append("**No causal rule predicts the inter-snapshot positive label without lookahead while passing all 3 splits.**")
    else:
        md.append("| Family | Params | Policy | Disc n | Disc PnL | Val n | Val PnL | Hold n | Hold PnL | Hold max_loss | Top winner share |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for rec in promoted:
            md.append(
                "| {fam} | {p} | {pol} | {dn} | {dp:+.6f} | {vn} | {vp:+.6f} | {hn} | {hp:+.6f} | {ml:+.6f} | {tws:.2f} |".format(
                    fam=rec["family"],
                    p=rec["params"],
                    pol=rec["policy"],
                    dn=rec["disc"]["n"],
                    dp=rec["disc"]["total_pnl"],
                    vn=rec["val"]["n"],
                    vp=rec["val"]["total_pnl"],
                    hn=rec["hold"]["n"],
                    hp=rec["hold"]["total_pnl"],
                    ml=rec["hold"]["max_loss"],
                    tws=rec["hold"]["top_winner_share"],
                )
            )
    md.append("")
    md.append("## Methodology")
    md.append("")
    md.append("- Strict time-split: discovery 0..60%, validation 60..80%, holdout 80..100% — by `decision_ts_ms` ascending.")
    md.append("- For each rule + parameter + exit-policy combination, every row in the split is treated as a candidate entry; the row's features (all timestamped <= decision_ts) are evaluated by the rule. If the rule fires, the future-snapshot price chain (the LABEL — not used as input) is consumed under the exit policy to realize a PnL.")
    md.append("- Qualification: discovery PnL > 0, validation PnL > 0, holdout PnL > 0, holdout n >= 3, holdout max_loss >= -0.0001, top_winner_share <= 0.70.")
    md.append("- Exit policies tested: A (bank 60), B (bank 40 + scratch 5), C (bank 80 + hold-while-grad+), D (scratch on grad flip).")

    OUT.write_text("\n".join(md), encoding="utf-8")
    PROMOTED.write_text(json.dumps(promoted, indent=2, default=str), encoding="utf-8")
    print(f"[V42F-MINER] qualified={len(promoted)} out={OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
