"""V47H — Pre-entry rug veto.

The 2 sub-500ms rugs that V47G's watchdog could not prevent (the rug fires
in the same slot as our buy lands, so no post-buy mechanism can catch it
before the loss) require a pre-entry filter. This module is the only new
DECISION layer V47H adds on top of V47G; the existing V47B/C/D/E/F/G entry
filters remain unchanged.

The veto is causal: it operates only on pre-buy buyer/sell/curve stats
that exist at the decision time. It is the LAST filter in the entry chain,
applied AFTER the V47G size-tiered floor and downsizer, immediately before
position open.

Veto categories:

A. Sell-pressure          — pending sells / breadth / largest-seller-share
B. Curve reversal         — negative deltas in last 500-1000ms
C. Blow-off               — exp_pnl/size ratio out of bounds
D. Thin two-buyer         — narrow buyer set + heavy share + zero pbsol margin
E. Weak marginal          — size 0.005 AND tiny edge AND narrow breadth
F. Dev/creator sell       — dormant (requires creator-wallet tracking not
                            available in free feeds)

API:
    evaluate_rug_veto(size_sol, expected_pnl, buyer_stats, sell_stats,
                      curve_history, dev_sell_detected_bool, logger=None,
                      mint_for_log=None)
        -> (veto_pass: bool, vetos_fired: list[str])

veto_pass=True  ⇒ NO veto fired; candidate proceeds to entry.
veto_pass=False ⇒ at least one veto fired; candidate is shadowed-only.

PURE FUNCTION. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple


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
            f"V47H-RUG-VETO-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47h_rug_veto")


# Veto identifiers (stable strings used in JSONL telemetry).
VETO_A_SELL_PRESSURE_35PCT = "veto_a_sell_pressure_35pct"
VETO_A_SELL_COUNT_GEQ_BUY = "veto_a_sell_count_geq_buy_count"
VETO_A_SELLER_BREADTH = "veto_a_seller_breadth_geq_buyer"
VETO_A_LARGEST_SELL_SHARE = "veto_a_largest_sell_share_40pct"

VETO_B_CURVE_REVERSAL_500MS = "veto_b_curve_reversal_500ms"
VETO_B_TWO_SIGN_FLIPS_1000MS = "veto_b_two_sign_flips_1000ms"
VETO_B_VELOCITY_DECEL_50PCT = "veto_b_velocity_decel_50pct"

VETO_C_BLOWOFF_RATIO_2 = "veto_c_blowoff_ratio_geq_2"
VETO_C_BLOWOFF_RATIO_075 = "veto_c_blowoff_ratio_geq_075_without_quality"

VETO_D_UB2_TBS = "veto_d_ub2_tbs_gt_050"
VETO_D_UB2_ANY_SELL = "veto_d_ub2_any_sell"
VETO_D_UB2_PNL_LT = "veto_d_ub2_pnl_lt_0001"
VETO_D_UB2_PBSOL_LT_5X = "veto_d_ub2_pbsol_lt_5x"

VETO_E_WEAK_MARGINAL = "veto_e_weak_marginal"

VETO_F_DEV_CREATOR_SELL = "veto_f_dev_creator_sell"


def _short(mint: str) -> str:
    if not mint or len(mint) < 6:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _i(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return int(default)


def evaluate_rug_veto(
    size_sol: float,
    expected_pnl: float,
    buyer_stats: Dict[str, Any],
    sell_stats: Dict[str, Any],
    curve_history: Optional[Dict[str, Any]],
    dev_sell_detected_bool: bool = False,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """Causal pre-entry rug veto.

    Parameters
    ----------
    size_sol : float
        Final selected position size in SOL.
    expected_pnl : float
        Expected pnl from V47B branch sim at the SELECTED size.
    buyer_stats : dict
        Buyer-side aggregates. Required keys (defaults to safe zeros if
        missing):
          - unique_buyers_250ms
          - top_buyer_share_250ms
          - pending_buy_sol_250ms
          - pending_buy_count_250ms
    sell_stats : dict
        Sell-side aggregates. Required keys (defaults to safe zeros):
          - pending_sell_sol_250ms
          - pending_sell_count_250ms
          - unique_sellers_250ms        (new in V47H sell-aware buffer)
          - largest_sell_sol_250ms       (new in V47H sell-aware buffer)
          - largest_sell_share_250ms     (computed; new in V47H buffer)
        For backwards-compat callers without the V47H buffer extension,
        unique_sellers/largest_sell_* can be omitted; those subclauses
        are then equivalent to "data unavailable, do not block."
    curve_history : dict or None
        Optional curve-history snapshot. Expected keys (all optional):
          - vsol_deltas_last_500ms       (list[float], oldest→newest)
          - vsol_deltas_last_1000ms      (list[float], oldest→newest)
          - peak_pos_delta_idx_500ms     (int; -1 if none)
        If None or missing keys → veto B subclauses do not fire (dormant).
    dev_sell_detected_bool : bool
        True if a sell from the creator/dev wallet has been detected in
        the last 1000ms for this mint. Default False (dormant; requires
        creator-wallet tracking unavailable in current free feeds).
    logger : optional callable
    mint_for_log : optional mint string for telemetry

    Returns
    -------
    (veto_pass, vetos_fired)
        veto_pass=True  → no veto fired; admit to entry.
        veto_pass=False → ≥1 veto fired; record reasons and shadow-only.
    """
    sz = _f(size_sol)
    exp_pnl = _f(expected_pnl)
    bs = buyer_stats or {}
    ss = sell_stats or {}
    ch = curve_history or {}

    ub_250 = _i(bs.get("unique_buyers_250ms", 0))
    tbs_250 = _f(bs.get("top_buyer_share_250ms", 0.0))
    pbsol_250 = _f(bs.get("pending_buy_sol_250ms", 0.0))
    pbcount_250 = _i(bs.get("pending_buy_count_250ms", 0))

    pss_sol_250 = _f(ss.get("pending_sell_sol_250ms", 0.0))
    pss_count_250 = _i(ss.get("pending_sell_count_250ms", 0))
    us_250_raw = ss.get("unique_sellers_250ms", None)
    us_available = us_250_raw is not None
    us_250 = _i(us_250_raw, 0)
    largest_sell_sol_raw = ss.get("largest_sell_sol_250ms", None)
    largest_sell_sol_available = largest_sell_sol_raw is not None
    largest_sell_sol_250 = _f(largest_sell_sol_raw, 0.0)
    largest_sell_share_raw = ss.get("largest_sell_share_250ms", None)
    if largest_sell_share_raw is None and pss_sol_250 > 0:
        largest_sell_share_250 = (
            largest_sell_sol_250 / pss_sol_250
            if largest_sell_sol_available else 0.0
        )
    else:
        largest_sell_share_250 = _f(largest_sell_share_raw, 0.0)

    fired: List[str] = []

    # --- VETO A: sell pressure -------------------------------------------
    if pss_sol_250 >= pbsol_250 * 0.35 and pbsol_250 > 0:
        fired.append(VETO_A_SELL_PRESSURE_35PCT)
    if pss_count_250 >= pbcount_250 and pbcount_250 > 0:
        fired.append(VETO_A_SELL_COUNT_GEQ_BUY)
    if us_available and us_250 >= ub_250 and ub_250 > 0:
        fired.append(VETO_A_SELLER_BREADTH)
    if largest_sell_sol_available and largest_sell_share_250 >= 0.40:
        fired.append(VETO_A_LARGEST_SELL_SHARE)

    # --- VETO B: curve reversal ------------------------------------------
    # 500ms reversal: any neg delta AFTER strongest pos delta within 500ms.
    deltas_500 = ch.get("vsol_deltas_last_500ms", None)
    if deltas_500 and len(deltas_500) >= 2:
        ds = list(deltas_500)
        peak_idx = -1
        peak_val = -1e18
        for i, d in enumerate(ds):
            try:
                dv = float(d)
            except Exception:
                continue
            if dv > peak_val:
                peak_val = dv
                peak_idx = i
        if peak_idx >= 0 and peak_val > 0:
            saw_neg_after = False
            for j in range(peak_idx + 1, len(ds)):
                try:
                    if float(ds[j]) < 0:
                        saw_neg_after = True
                        break
                except Exception:
                    pass
            if saw_neg_after:
                fired.append(VETO_B_CURVE_REVERSAL_500MS)

    # 1000ms two-sign-flip check.
    deltas_1000 = ch.get("vsol_deltas_last_1000ms", None)
    if deltas_1000 and len(deltas_1000) >= 3:
        ds = list(deltas_1000)
        flips = 0
        prev_sign = 0
        for d in ds:
            try:
                dv = float(d)
            except Exception:
                continue
            cur_sign = 1 if dv > 0 else (-1 if dv < 0 else 0)
            if cur_sign != 0 and prev_sign != 0 and cur_sign != prev_sign:
                flips += 1
            if cur_sign != 0:
                prev_sign = cur_sign
        if flips >= 2:
            fired.append(VETO_B_TWO_SIGN_FLIPS_1000MS)

    # Velocity decel: compare peak velocity (max abs delta) to most-recent
    # window velocity. If recent < peak * 0.5 → decel.
    if deltas_1000 and len(deltas_1000) >= 4:
        ds = list(deltas_1000)
        # Two halves: front half = first len/2, back half = remainder.
        half = len(ds) // 2
        front = ds[:half]
        back = ds[half:]
        try:
            peak_v = max(abs(float(x)) for x in front) if front else 0.0
            recent_v = sum(abs(float(x)) for x in back) / max(1, len(back))
            if peak_v > 0 and recent_v < peak_v * 0.5:
                fired.append(VETO_B_VELOCITY_DECEL_50PCT)
        except Exception:
            pass

    # --- VETO C: blow-off ------------------------------------------------
    ratio = (exp_pnl / sz) if sz > 0 else 0.0
    if ratio >= 2.0:
        fired.append(VETO_C_BLOWOFF_RATIO_2)
    elif ratio >= 0.75:
        # Require ALL of: ub>=4, tbs<=0.40, no pending sells, pbsol >= 8x
        quality = (
            ub_250 >= 4
            and tbs_250 <= 0.40
            and pss_sol_250 <= 1e-12
            and pss_count_250 == 0
            and pbsol_250 >= (sz * 8.0)
        )
        if not quality:
            fired.append(VETO_C_BLOWOFF_RATIO_075)

    # --- VETO D: thin two-buyer continuation -----------------------------
    if ub_250 == 2:
        if tbs_250 > 0.50:
            fired.append(VETO_D_UB2_TBS)
        if pss_count_250 > 0 or pss_sol_250 > 0:
            fired.append(VETO_D_UB2_ANY_SELL)
        if exp_pnl < 0.001000:
            fired.append(VETO_D_UB2_PNL_LT)
        if pbsol_250 < (sz * 5.0):
            fired.append(VETO_D_UB2_PBSOL_LT_5X)

    # --- VETO E: weak marginal -------------------------------------------
    if sz <= 0.005 + 1e-12 and exp_pnl < 0.000900 and ub_250 < 5:
        fired.append(VETO_E_WEAK_MARGINAL)

    # --- VETO F: dev/creator sell (dormant unless data available) --------
    if bool(dev_sell_detected_bool):
        fired.append(VETO_F_DEV_CREATOR_SELL)

    veto_pass = len(fired) == 0

    # Telemetry log line.
    if logger is not None:
        try:
            s2b_ratio = (
                (pss_sol_250 / pbsol_250) if pbsol_250 > 0 else 0.0
            )
            reversal_short = "yes" if VETO_B_CURVE_REVERSAL_500MS in fired else "no"
            two_flip_short = "yes" if VETO_B_TWO_SIGN_FLIPS_1000MS in fired else "no"
            logger(
                f"PGG2-V47H-RUG-VETO mint={_short(mint_for_log or '')} "
                f"size={sz:.4f} exp_pnl={exp_pnl:+.6f} "
                f"ratio={ratio:+.3f} "
                f"ub={ub_250} tbs={tbs_250:.3f} "
                f"pbs={pbsol_250:.4f} pss={pss_sol_250:.4f} "
                f"s2b={s2b_ratio:.3f} "
                f"us={us_250 if us_available else -1} "
                f"reversal={reversal_short} two_flip={two_flip_short} "
                f"dev={int(bool(dev_sell_detected_bool))} "
                f"veto_pass={int(veto_pass)} "
                f"reasons={'|'.join(fired) if fired else '-'}"
            )
        except Exception:
            pass

    return (veto_pass, fired)


__all__ = [
    "evaluate_rug_veto",
    "VETO_A_SELL_PRESSURE_35PCT",
    "VETO_A_SELL_COUNT_GEQ_BUY",
    "VETO_A_SELLER_BREADTH",
    "VETO_A_LARGEST_SELL_SHARE",
    "VETO_B_CURVE_REVERSAL_500MS",
    "VETO_B_TWO_SIGN_FLIPS_1000MS",
    "VETO_B_VELOCITY_DECEL_50PCT",
    "VETO_C_BLOWOFF_RATIO_2",
    "VETO_C_BLOWOFF_RATIO_075",
    "VETO_D_UB2_TBS",
    "VETO_D_UB2_ANY_SELL",
    "VETO_D_UB2_PNL_LT",
    "VETO_D_UB2_PBSOL_LT_5X",
    "VETO_E_WEAK_MARGINAL",
    "VETO_F_DEV_CREATOR_SELL",
]
