"""V47I — 500-1000ms medium-rug veto.

The V47H rug-veto blocks sub-500ms rugs (proven on 2 Jng + Dxxi banks). V47I
adds a pre-entry filter targeting the 500-1000ms window specifically, where
V47H Phase 6 produced 2 losses (CNk6 at 514ms, DxPa at 991ms).

V47I is the LAST filter in the chain, applied AFTER V47H rug-veto. It is
causal: it operates only on pre-buy buyer/sell/curve/quote stats that exist
at decision time.

Phase 1 forensic finding: **no single persisted V47H feature strictly
separates both medium rugs from bank winners**. The 2 rugs lie within the
bank distribution on every persisted dimension, though both lie below the
bank median on buy-side strength (buyers, pbsol, count, exp_pnl) and on the
high side of top_buyer_share. Sell-side 500/1000ms windows, curve-delta
values, and quote history are NOT persisted in V47H JSONL but become
available when V47I no-send capture extends the wrappers.

5 sub-vetos (any fires → block):

A. Medium-window sell pressure (500ms):
   - pending_sell_sol_500ms >= pending_buy_sol_500ms * 0.25
   - sell_count_500ms >= buy_count_500ms * 0.75
   - unique_sellers_500ms >= unique_buyers_500ms (and >0)

B. Curve deceleration (causal trajectory):
   - Velocity dropped by >60% from peak window to recent window
   - Two consecutive lower POSITIVE curve deltas before entry
   - Negative curve delta within last 750ms

C. Quote weakening (local quote history):
   - Current local quote below recent peak by >= 300 microSOL (0.0003 SOL)
   - Quote gradient negative in 2 of last 3 updates
   - Quote improvement flattening AND exp_pnl is only marginal (<0.0015)

D. Medium-rug thin edge: block if exp_pnl < +0.001200 AND size <= 0.010
   AND ANY sell pressure exists in 500ms window.

E. Suspicious fast-reversal setup:
   - Strong projected edge (exp_pnl >= +0.002) but net_pending_sol_500ms is
     FALLING vs net_pending_sol_250ms.
   - Buy pressure exists only in 250ms but DISAPPEARS in 500ms window.

API:
    evaluate_medium_rug_veto(size_sol, expected_pnl, buyer_stats,
                             sell_stats, curve_history, quote_history,
                             logger=None, mint_for_log=None)
        -> (veto_pass: bool, vetos_fired: list[str])

veto_pass=True  → no veto fired; admit to entry (downstream of V47H pass).
veto_pass=False → ≥1 veto fired; shadow-only.

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
            f"V47I-MEDIUM-RUG-VETO-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47i_medium_rug_veto")


# ---- veto IDs (stable strings used in JSONL telemetry) ---------------
VETO_A_SELL_PRESSURE_500MS_25PCT = "veto_a_sell_pressure_500ms_25pct"
VETO_A_SELL_COUNT_500MS_75PCT_BUY = "veto_a_sell_count_500ms_75pct_buy"
VETO_A_SELLER_BREADTH_500MS_GEQ_BUYER = "veto_a_seller_breadth_500ms_geq_buyer"

VETO_B_VELOCITY_DECEL_60PCT = "veto_b_velocity_decel_60pct"
VETO_B_TWO_LOWER_POSITIVE_DELTAS = "veto_b_two_lower_positive_deltas"
VETO_B_NEGATIVE_DELTA_WITHIN_750MS = "veto_b_negative_delta_within_750ms"

VETO_C_QUOTE_DROP_300U_FROM_PEAK = "veto_c_quote_drop_300u_from_peak"
VETO_C_QUOTE_GRADIENT_NEGATIVE_2OF3 = "veto_c_quote_gradient_negative_2of3"
VETO_C_MARGINAL_PNL_FLAT_QUOTE = "veto_c_marginal_pnl_flat_quote"

VETO_D_THIN_EDGE_WITH_500MS_SELL = "veto_d_thin_edge_with_500ms_sell"

VETO_E_STRONG_PNL_WITH_FALLING_NET_FLOW = (
    "veto_e_strong_pnl_with_falling_net_flow"
)
VETO_E_BUY_PRESSURE_ONLY_250MS = "veto_e_buy_pressure_only_250ms"


# Tunable thresholds (kept inline so a single edit suffices).
_QUOTE_PEAK_DROP_SOL = 0.000300
_THIN_EDGE_EXP_PNL = 0.001200
_THIN_EDGE_MAX_SIZE = 0.010 + 1e-12
_STRONG_PNL_THRESHOLD = 0.002000
_MARGINAL_PNL_THRESHOLD = 0.001500
_VELOCITY_DECEL_FRACTION = 0.40  # recent < peak * 0.40 → 60% decel


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


def _safe_list(x: Any) -> List[float]:
    """Convert iterable to list of floats (best-effort)."""
    if x is None:
        return []
    out = []
    try:
        for v in x:
            try:
                out.append(float(v))
            except Exception:
                continue
    except Exception:
        return []
    return out


def evaluate_medium_rug_veto(
    size_sol: float,
    expected_pnl: float,
    buyer_stats: Dict[str, Any],
    sell_stats: Dict[str, Any],
    curve_history: Optional[Dict[str, Any]],
    quote_history: Optional[Dict[str, Any]],
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """Causal pre-entry medium-rug veto.

    Parameters
    ----------
    size_sol : float
    expected_pnl : float
    buyer_stats : dict
        Required keys (defaults to safe zeros):
          - unique_buyers_250ms / unique_buyers_500ms / unique_buyers_1000ms
          - pending_buy_sol_250ms / pending_buy_sol_500ms /
            pending_buy_sol_1000ms
          - pending_buy_count_250ms / pending_buy_count_500ms /
            pending_buy_count_1000ms
          - top_buyer_share_250ms (optional, used in C marginal check)
          - net_pending_sol_250ms / net_pending_sol_500ms (sol delta)
    sell_stats : dict
        Required keys (defaults to safe zeros):
          - pending_sell_count_500ms / pending_sell_sol_500ms
          - unique_sellers_500ms / unique_sellers_1000ms
          - pending_sell_count_1000ms / pending_sell_sol_1000ms
    curve_history : dict or None
        Required keys (optional):
          - vsol_deltas_last_500ms (list[float])
          - vsol_deltas_last_1000ms (list[float])
          - vsol_delta_ts_last_1000ms (list[int])  ages from oldest→newest
        If None or empty → veto B subclauses dormant.
    quote_history : dict or None
        Required keys (optional):
          - local_quote_last (float, current local sell quote in SOL)
          - local_quote_peak_500ms (float)
          - local_quote_history_5 (list[float], oldest→newest)
          - local_quote_gradient_history_3 (list[float], oldest→newest)
        If None or empty → veto C subclauses dormant.
    logger : optional callable
    mint_for_log : optional mint

    Returns
    -------
    (veto_pass, vetos_fired)
    """
    sz = _f(size_sol)
    exp_pnl = _f(expected_pnl)
    bs = buyer_stats or {}
    ss = sell_stats or {}
    ch = curve_history or {}
    qh = quote_history or {}

    # Buyer-side
    pbs_250 = _f(bs.get("pending_buy_sol_250ms", 0.0))
    pbs_500 = _f(bs.get("pending_buy_sol_500ms", 0.0))
    pbs_1000 = _f(bs.get("pending_buy_sol_1000ms", 0.0))
    pbc_250 = _i(bs.get("pending_buy_count_250ms", 0))
    pbc_500 = _i(bs.get("pending_buy_count_500ms", 0))
    pbc_1000 = _i(bs.get("pending_buy_count_1000ms", 0))
    ub_250 = _i(bs.get("unique_buyers_250ms", 0))
    ub_500 = _i(bs.get("unique_buyers_500ms", 0))
    ub_1000 = _i(bs.get("unique_buyers_1000ms", 0))
    tbs_250 = _f(bs.get("top_buyer_share_250ms", 0.0))
    net_250_raw = bs.get("net_pending_sol_250ms", None)
    net_250 = _f(net_250_raw, 0.0) if net_250_raw is not None else None
    net_500_raw = bs.get("net_pending_sol_500ms", None)
    net_500 = _f(net_500_raw, 0.0) if net_500_raw is not None else None

    # Sell-side
    pss_sol_500 = _f(ss.get("pending_sell_sol_500ms", 0.0))
    pss_count_500 = _i(ss.get("pending_sell_count_500ms", 0))
    us_500_raw = ss.get("unique_sellers_500ms", None)
    us_500_available = us_500_raw is not None
    us_500 = _i(us_500_raw, 0)
    pss_sol_1000 = _f(ss.get("pending_sell_sol_1000ms", 0.0))
    pss_count_1000 = _i(ss.get("pending_sell_count_1000ms", 0))
    us_1000_raw = ss.get("unique_sellers_1000ms", None)

    # Curve-history
    deltas_500 = _safe_list(ch.get("vsol_deltas_last_500ms"))
    deltas_1000 = _safe_list(ch.get("vsol_deltas_last_1000ms"))
    delta_ages_1000 = _safe_list(ch.get("vsol_delta_ts_last_1000ms"))

    # Quote-history
    q_now = qh.get("local_quote_last")
    q_peak_500 = qh.get("local_quote_peak_500ms")
    q_hist_5 = _safe_list(qh.get("local_quote_history_5"))
    q_grad_3 = _safe_list(qh.get("local_quote_gradient_history_3"))

    fired: List[str] = []

    # ===== VETO A: medium-window sell pressure (500ms) ============
    # A1: pending_sell_sol_500ms >= pending_buy_sol_500ms * 0.25
    if pbs_500 > 0 and pss_sol_500 >= pbs_500 * 0.25:
        fired.append(VETO_A_SELL_PRESSURE_500MS_25PCT)
    # A2: sell_count_500ms >= buy_count_500ms * 0.75
    if pbc_500 > 0 and pss_count_500 >= pbc_500 * 0.75:
        fired.append(VETO_A_SELL_COUNT_500MS_75PCT_BUY)
    # A3: unique_sellers_500ms >= unique_buyers_500ms (and >0)
    if us_500_available and us_500 >= ub_500 and us_500 > 0 and ub_500 > 0:
        fired.append(VETO_A_SELLER_BREADTH_500MS_GEQ_BUYER)

    # ===== VETO B: curve deceleration =============================
    # B1: velocity dropped by >60% (peak vs recent avg).
    if deltas_1000 and len(deltas_1000) >= 4:
        half = len(deltas_1000) // 2
        front = deltas_1000[:half]
        back = deltas_1000[half:]
        try:
            peak_v = max(abs(x) for x in front) if front else 0.0
            recent_v = (sum(abs(x) for x in back) / max(1, len(back)))
            if peak_v > 0 and recent_v < peak_v * _VELOCITY_DECEL_FRACTION:
                fired.append(VETO_B_VELOCITY_DECEL_60PCT)
        except Exception:
            pass

    # B2: two consecutive lower positive deltas before entry.
    # i.e., last 3 deltas were all positive but each lower than the previous.
    if deltas_1000 and len(deltas_1000) >= 3:
        last3 = deltas_1000[-3:]
        if all(d > 0 for d in last3) and last3[0] > last3[1] > last3[2]:
            fired.append(VETO_B_TWO_LOWER_POSITIVE_DELTAS)

    # B3: negative curve delta within last 750ms.
    # Use deltas_1000 with ages; if no ages, fall back to deltas_500 (any neg
    # in 500ms is necessarily within 750ms).
    saw_neg_750 = False
    if deltas_1000 and delta_ages_1000 and len(deltas_1000) == len(delta_ages_1000):
        for d, age in zip(deltas_1000, delta_ages_1000):
            try:
                if float(age) <= 750.0 and float(d) < 0.0:
                    saw_neg_750 = True
                    break
            except Exception:
                pass
    if not saw_neg_750 and deltas_500:
        for d in deltas_500:
            if d < 0:
                saw_neg_750 = True
                break
    if saw_neg_750:
        fired.append(VETO_B_NEGATIVE_DELTA_WITHIN_750MS)

    # ===== VETO C: quote weakening ================================
    # C1: current local quote below recent peak by >= 300 microSOL.
    if (
        q_now is not None
        and q_peak_500 is not None
    ):
        try:
            q_now_f = float(q_now)
            q_peak_f = float(q_peak_500)
            if q_peak_f - q_now_f >= _QUOTE_PEAK_DROP_SOL:
                fired.append(VETO_C_QUOTE_DROP_300U_FROM_PEAK)
        except Exception:
            pass

    # C2: quote gradient negative in 2 of last 3 updates.
    if q_grad_3 and len(q_grad_3) >= 3:
        last3 = q_grad_3[-3:]
        neg_count = sum(1 for g in last3 if g < 0)
        if neg_count >= 2:
            fired.append(VETO_C_QUOTE_GRADIENT_NEGATIVE_2OF3)

    # C3: quote improvement flattening AND exp_pnl is only marginal.
    # Flattening = last 3 gradients all small magnitude (<5 microSOL) and not
    # all positive.
    if q_grad_3 and len(q_grad_3) >= 3 and exp_pnl < _MARGINAL_PNL_THRESHOLD:
        last3 = q_grad_3[-3:]
        small_mag = all(abs(g) < 5e-6 for g in last3)
        not_all_pos = not all(g > 0 for g in last3)
        if small_mag and not_all_pos:
            fired.append(VETO_C_MARGINAL_PNL_FLAT_QUOTE)

    # ===== VETO D: thin edge with 500ms sell ======================
    # Block if exp_pnl < +0.001200 AND size <= 0.010 AND any sell pressure
    # exists in 500ms window.
    sell_any_500 = (
        pss_sol_500 > 0
        or pss_count_500 > 0
        or (us_500_available and us_500 > 0)
    )
    if exp_pnl < _THIN_EDGE_EXP_PNL and sz <= _THIN_EDGE_MAX_SIZE and sell_any_500:
        fired.append(VETO_D_THIN_EDGE_WITH_500MS_SELL)

    # ===== VETO E: fast-reversal setup ============================
    # E1: strong projected edge AND net_pending_sol falling (500ms < 250ms).
    if (
        exp_pnl >= _STRONG_PNL_THRESHOLD
        and net_250 is not None
        and net_500 is not None
        and net_500 < net_250  # falling means net dropped between 250 and 500 windows
    ):
        fired.append(VETO_E_STRONG_PNL_WITH_FALLING_NET_FLOW)

    # E2: buy pressure exists only in 250ms but DISAPPEARS in 500ms.
    # Operational: pbsol_500 == pbsol_250 (no growth between 250-500) AND
    # additionally we want to detect a buy cluster that DOES NOT extend
    # backwards into the 500 window. The mathematically meaningful pattern is:
    # pbc_250 > 0 AND pbc_500 == pbc_250 (no older buys 250-500ms ago)
    # OR pbc_500 < pbc_250 (impossible; 500ms window is superset).
    # Real "disappear" pattern: pbsol_500 - pbsol_250 == 0 AND
    # pbsol_1000 - pbsol_500 > 0 → buys are clustered in 250-500 ago, not now.
    # We flag the latter to detect "rally was 500ms ago, now stalled".
    older_500_to_1000 = pbs_1000 - pbs_500
    newer_0_to_250 = pbs_250
    middle_250_to_500 = pbs_500 - pbs_250
    if (
        pbs_500 > 0
        and middle_250_to_500 <= 0.0001  # essentially no buys 250-500ms ago
        and older_500_to_1000 > 0.0001  # but buys did happen 500-1000ms ago
        and newer_0_to_250 > 0  # current 250ms also has buys
    ):
        # Pattern: buy cluster 500-1000ms ago, gap 250-500ms ago, then new
        # 0-250ms cluster. This is the "fake bounce" pattern.
        fired.append(VETO_E_BUY_PRESSURE_ONLY_250MS)

    veto_pass = len(fired) == 0

    # ----- log -------
    if logger is not None:
        try:
            curve_decel = "yes" if VETO_B_VELOCITY_DECEL_60PCT in fired else "no"
            quote_decay = "yes" if VETO_C_QUOTE_DROP_300U_FROM_PEAK in fired else "no"
            logger(
                f"PGG2-V47I-MEDIUM-RUG-VETO mint={_short(mint_for_log or '')} "
                f"size={sz:.4f} exp_pnl={exp_pnl:+.6f} "
                f"pbs_250={pbs_250:.3f} pbs_500={pbs_500:.3f} "
                f"pss_250={_f(ss.get('pending_sell_sol_250ms', 0.0)):.4f} "
                f"pss_500={pss_sol_500:.4f} "
                f"ub_250={ub_250} ub_500={ub_500} "
                f"us_500={us_500 if us_500_available else -1} "
                f"tbs_250={tbs_250:.3f} "
                f"curve_decel={curve_decel} quote_decay={quote_decay} "
                f"deltas500_n={len(deltas_500)} qhist_n={len(q_hist_5)} "
                f"veto_pass={int(veto_pass)} "
                f"reasons={'|'.join(fired) if fired else '-'}"
            )
        except Exception:
            pass

    return (veto_pass, fired)


__all__ = [
    "evaluate_medium_rug_veto",
    "VETO_A_SELL_PRESSURE_500MS_25PCT",
    "VETO_A_SELL_COUNT_500MS_75PCT_BUY",
    "VETO_A_SELLER_BREADTH_500MS_GEQ_BUYER",
    "VETO_B_VELOCITY_DECEL_60PCT",
    "VETO_B_TWO_LOWER_POSITIVE_DELTAS",
    "VETO_B_NEGATIVE_DELTA_WITHIN_750MS",
    "VETO_C_QUOTE_DROP_300U_FROM_PEAK",
    "VETO_C_QUOTE_GRADIENT_NEGATIVE_2OF3",
    "VETO_C_MARGINAL_PNL_FLAT_QUOTE",
    "VETO_D_THIN_EDGE_WITH_500MS_SELL",
    "VETO_E_STRONG_PNL_WITH_FALLING_NET_FLOW",
    "VETO_E_BUY_PRESSURE_ONLY_250MS",
]
