"""V46 Phase 4 - Pending-Flow Curve Projector.

Apply pending external BUY/SELL events (not yet reflected in
accountSubscribe curve) on top of latest curve state, then compute our
hypothetical 0.015 SOL buy and projected sell-out PnL.

Reuses the integer-exact CPMM math from pgg2_v42h_local_curve_quote.

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import os
import re as _re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


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
            f"V46-PROJECTOR-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v46_pending_flow_projector")


# We import the integer-exact CPMM helpers at call-time so the projector
# can be imported in environments without the full bot dependency graph.
def _import_quote_helpers():
    sys.path.insert(0, "/root/piggy")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pgg2_v42h_local_curve_quote import (  # type: ignore
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    )
    return (
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    )


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _apply_external_buy_to_curve(
    curve_state, sol_in: float, V42HCurveState, LAMPORTS_PER_SOL,
    local_buy_quote_tokens_raw,
):
    """Apply an external buy of `sol_in` SOL to curve_state. Returns new state.

    Uses local_buy_quote_tokens_raw to compute tokens_out and net_for_curve
    (sol that lands in the curve after fees), then mutates reserves:
        new_vsol += net_for_curve
        new_vtok -= tokens_out
    """
    tokens_out, fees = local_buy_quote_tokens_raw(curve_state, float(sol_in))
    spend_lamports = int(round(float(sol_in) * LAMPORTS_PER_SOL))
    total_fee_bps = max(
        0,
        int(curve_state.fee_bps) + int(curve_state.creator_fee_bps),
    )
    net_sol = spend_lamports * 10_000 // (10_000 + total_fee_bps)
    if net_sol + fees > spend_lamports:
        net_sol -= net_sol + fees - spend_lamports
    net_for_curve = max(0, net_sol - 1)
    new_vsol = int(curve_state.virtual_sol_reserves) + int(net_for_curve)
    new_vtok = max(1, int(curve_state.virtual_token_reserves) - int(tokens_out))
    new_real_tok = max(0, int(curve_state.real_token_reserves) - int(tokens_out))
    return V42HCurveState(
        virtual_sol_reserves=int(new_vsol),
        virtual_token_reserves=int(new_vtok),
        real_token_reserves=int(new_real_tok),
        fee_bps=int(curve_state.fee_bps),
        creator_fee_bps=int(curve_state.creator_fee_bps),
    ), int(tokens_out)


def _apply_external_sell_to_curve(
    curve_state, tokens_in: int, V42HCurveState,
    local_sell_quote_sol,
):
    """Apply an external sell of `tokens_in` tokens. Returns new state."""
    gross_sol_net, fees = local_sell_quote_sol(curve_state, int(tokens_in))
    # The curve receives the tokens_in tokens (back into vtok) and loses
    # the gross_sol from vsol. Integer-exact path: see direct_pump.
    new_vtok = int(curve_state.virtual_token_reserves) + int(tokens_in)
    # gross_sol_net is NET of fees; the curve loses the GROSS amount.
    total_fee_bps = max(
        0,
        int(curve_state.fee_bps) + int(curve_state.creator_fee_bps),
    )
    if gross_sol_net + fees > 0:
        gross_sol = int(gross_sol_net) + int(fees)
    else:
        gross_sol = int(gross_sol_net)
    new_vsol = max(1, int(curve_state.virtual_sol_reserves) - int(gross_sol))
    new_real_tok = (
        int(curve_state.real_token_reserves) + int(tokens_in)
    )
    return V42HCurveState(
        virtual_sol_reserves=int(new_vsol),
        virtual_token_reserves=int(new_vtok),
        real_token_reserves=int(new_real_tok),
        fee_bps=int(curve_state.fee_bps),
        creator_fee_bps=int(curve_state.creator_fee_bps),
    )


def _confidence_score(unique_buyers: int, cluster_speed: float) -> float:
    """Heuristic 0..1: high when many unique buyers and dense cluster."""
    ub = max(0, int(unique_buyers))
    cs = max(0.0, float(cluster_speed))
    # ub component: 0..0.6 saturating at 4 unique buyers.
    ub_c = min(0.6, ub / 4.0 * 0.6)
    # cs component: 0..0.4 saturating at 16 buys/sec.
    cs_c = min(0.4, cs / 16.0 * 0.4)
    return float(ub_c + cs_c)


def _required_flow_sol_for_pnl(
    base_curve_state, pending_buys_after_ours: List[Tuple[int, float, str, int]],
    our_buy_sol: float, target_pnl: float,
    V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
    local_buy_quote_tokens_raw, local_sell_quote_sol,
) -> float:
    """Binary search the minimum total pending_sol_after_us such that
    projected_pnl >= target_pnl. Returns 0 if already met with zero, or
    a positive number, capped at the sum of supplied pending buys.
    """
    # Quickly check if zero-pending hits the target.
    pnl_zero = _project_pnl_under_state_change(
        base_curve_state, [], our_buy_sol,
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    if pnl_zero >= target_pnl:
        return 0.0
    # Sum of buys we have to consider for upper bound.
    total_avail = float(sum(b[1] for b in pending_buys_after_ours))
    if total_avail <= 0:
        return float("inf")
    # Use proportional scaling - dilute each pending buy by alpha.
    lo, hi = 0.0, total_avail
    for _ in range(20):
        mid = 0.5 * (lo + hi)
        alpha = mid / total_avail if total_avail > 0 else 0.0
        scaled = [(t, s * alpha, sig, sl) for (t, s, sig, sl) in pending_buys_after_ours]
        pnl = _project_pnl_under_state_change(
            base_curve_state, scaled, our_buy_sol,
            V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
            local_buy_quote_tokens_raw, local_sell_quote_sol,
        )
        if pnl >= target_pnl:
            hi = mid
        else:
            lo = mid
    return float(hi)


def _project_pnl_under_state_change(
    base_curve_state,
    pending_buys_after_ours: List[Tuple[int, float, str, int]],
    our_buy_sol: float,
    V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
    local_buy_quote_tokens_raw, local_sell_quote_sol,
    pending_sells_after_ours: Optional[List[Tuple[int, int, str, int]]] = None,
) -> float:
    """Compute pnl: buy our 0.015 SOL at base_state, apply pending events
    that land AFTER us, then sell at the resulting curve state. Returns SOL
    net of 2x tx fees.
    """
    # 1. Our buy at base state.
    tokens_we_got, _fees_we_paid = local_buy_quote_tokens_raw(
        base_curve_state, float(our_buy_sol)
    )
    if tokens_we_got <= 0:
        return -float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL)

    # 2. Advance the curve by OUR buy to compute the state after we land.
    state_after_us, _our_tokens_out = _apply_external_buy_to_curve(
        base_curve_state, float(our_buy_sol),
        V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
    )

    # 3. Apply pending buys/sells (ordered by ts).
    cur = state_after_us
    if pending_buys_after_ours:
        for ts, sol_in, _sig, _slot in sorted(pending_buys_after_ours, key=lambda x: x[0]):
            if sol_in <= 0:
                continue
            cur, _ = _apply_external_buy_to_curve(
                cur, float(sol_in),
                V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
            )
    if pending_sells_after_ours:
        for ts, tok_in, _sig, _slot in sorted(pending_sells_after_ours, key=lambda x: x[0]):
            if tok_in <= 0:
                continue
            cur = _apply_external_sell_to_curve(
                cur, int(tok_in), V42HCurveState, local_sell_quote_sol,
            )

    # 4. Sell at the final state.
    sell_lamports, _sell_fees = local_sell_quote_sol(cur, int(tokens_we_got))
    sell_sol = float(sell_lamports) / float(LAMPORTS_PER_SOL)
    return float(sell_sol) - float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL)


def project_with_pending_flow(
    latest_curve_state,
    our_buy_sol: float,
    pending_buys: List[Tuple[int, float, str, int]],
    pending_sells: List[Tuple[int, int, str, int]],
    exec_delay_ms: int = 100,
    unique_buyers: int = 0,
    cluster_speed: float = 0.0,
    source_lead_ms: float = 0.0,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
    bank_target_sol: float = 0.00060,
) -> Dict[str, Any]:
    """Compute the V46 pending-flow projection.

    Args:
      latest_curve_state: V42HCurveState (vsol, vtok, real_tok, fee_bps,
        creator_fee_bps) reflecting the most recent accountSubscribe
        update.
      our_buy_sol: our trade size (fixed 0.015 in V46).
      pending_buys: list of (ts_ms, sol_in, signer, slot) tuples with
        shred ts > latest_curve_update_ts (causal).
      pending_sells: list of (ts_ms, tokens_in, signer, slot) tuples.
      exec_delay_ms: how long until our buy lands. Default 100ms.
      unique_buyers / cluster_speed: from pending-flow buffer.
      source_lead_ms: ts_curve - ts_latest_shred at decision time.
      logger: optional log function.

    Returns dict (see module spec).
    """
    (
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    ) = _import_quote_helpers()

    # ----- baseline projection: our buy lands FIRST, all pending land after ----
    pnl_default = _project_pnl_under_state_change(
        latest_curve_state, pending_buys, float(our_buy_sol),
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
        pending_sells_after_ours=pending_sells,
    )

    # ----- stress models ----------------------------------------------------
    pending_sum_sol = float(sum(b[1] for b in pending_buys))

    # (a) only 50% of pending buys land after us
    pnl_a = _project_pnl_under_state_change(
        latest_curve_state,
        [(t, s * 0.5, sig, sl) for (t, s, sig, sl) in pending_buys],
        float(our_buy_sol),
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
        pending_sells_after_ours=pending_sells,
    )

    # (b) largest pending buy was actually already priced in
    pnl_b = pnl_default
    if pending_buys:
        sorted_b = sorted(pending_buys, key=lambda x: -float(x[1]))
        without_largest = sorted_b[1:]
        pnl_b = _project_pnl_under_state_change(
            latest_curve_state, without_largest, float(our_buy_sol),
            V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
            local_buy_quote_tokens_raw, local_sell_quote_sol,
            pending_sells_after_ours=pending_sells,
        )

    # (c) one pending sell appears equal in size to largest pending buy
    pnl_c = pnl_default
    if pending_buys:
        largest_sol = float(max(b[1] for b in pending_buys))
        # convert largest_sol to a token amount on current curve.
        tokens_for_largest, _ = local_buy_quote_tokens_raw(
            latest_curve_state, float(largest_sol)
        )
        if tokens_for_largest > 0:
            extra_sell = [(0, int(tokens_for_largest), "_v46_stress_c", 0)]
            sells_aug = list(pending_sells or []) + extra_sell
            pnl_c = _project_pnl_under_state_change(
                latest_curve_state, pending_buys, float(our_buy_sol),
                V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
                local_buy_quote_tokens_raw, local_sell_quote_sol,
                pending_sells_after_ours=sells_aug,
            )

    # (d) 250ms execution delay - our buy lands LAST after all pending flow.
    # Walk the curve by pending buys/sells first, then we buy at the final
    # state, then we sell at that same state.
    state_after_pending = latest_curve_state
    if pending_buys:
        for ts, sol_in, _sig, _sl in sorted(pending_buys, key=lambda x: x[0]):
            if sol_in <= 0:
                continue
            state_after_pending, _ = _apply_external_buy_to_curve(
                state_after_pending, float(sol_in),
                V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
            )
    if pending_sells:
        for ts, tok_in, _sig, _sl in sorted(pending_sells, key=lambda x: x[0]):
            if tok_in <= 0:
                continue
            state_after_pending = _apply_external_sell_to_curve(
                state_after_pending, int(tok_in),
                V42HCurveState, local_sell_quote_sol,
            )
    # Same-state round-trip from state_after_pending.
    tok_we_got_d, _ = local_buy_quote_tokens_raw(
        state_after_pending, float(our_buy_sol)
    )
    if tok_we_got_d > 0:
        # Apply our buy to advance state, then sell at that state.
        state_after_ours, _ = _apply_external_buy_to_curve(
            state_after_pending, float(our_buy_sol),
            V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
        )
        sell_lams_d, _ = local_sell_quote_sol(state_after_ours, int(tok_we_got_d))
        sell_sol_d = float(sell_lams_d) / float(LAMPORTS_PER_SOL)
        pnl_d = sell_sol_d - float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL)
    else:
        pnl_d = -float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL)

    stress_pnl = min(pnl_a, pnl_b, pnl_c, pnl_d)

    # Projected sell-out & required flow for bank.
    # Re-compute projected sell-out for the default scenario.
    tokens_we_got, _ = local_buy_quote_tokens_raw(
        latest_curve_state, float(our_buy_sol)
    )
    if tokens_we_got > 0:
        # Build state after our buy then pending buys/sells.
        state_after_us, _ = _apply_external_buy_to_curve(
            latest_curve_state, float(our_buy_sol),
            V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
        )
        cur = state_after_us
        for ts, sol_in, _sig, _sl in sorted(pending_buys, key=lambda x: x[0]):
            if sol_in > 0:
                cur, _ = _apply_external_buy_to_curve(
                    cur, float(sol_in),
                    V42HCurveState, LAMPORTS_PER_SOL,
                    local_buy_quote_tokens_raw,
                )
        for ts, tok_in, _sig, _sl in sorted(pending_sells or [], key=lambda x: x[0]):
            if tok_in > 0:
                cur = _apply_external_sell_to_curve(
                    cur, int(tok_in),
                    V42HCurveState, local_sell_quote_sol,
                )
        sell_lams, _ = local_sell_quote_sol(cur, int(tokens_we_got))
        projected_sell_out_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)
    else:
        projected_sell_out_sol = 0.0

    required_flow = _required_flow_sol_for_pnl(
        latest_curve_state, pending_buys, float(our_buy_sol),
        float(bank_target_sol),
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    confidence = _confidence_score(unique_buyers, cluster_speed)

    out = {
        "projected_pnl": float(pnl_default),
        "stress_pnl": float(stress_pnl),
        "pending_flow_sol_used": float(pending_sum_sol),
        "projected_sell_out": float(projected_sell_out_sol),
        "required_flow_sol": float(required_flow),
        "source_lead_ms": float(source_lead_ms),
        "confidence": float(confidence),
        "stress_pnl_50pct_drop": float(pnl_a),
        "stress_pnl_largest_priced_in": float(pnl_b),
        "stress_pnl_sell_appears": float(pnl_c),
        "stress_pnl_we_land_last": float(pnl_d),
    }

    if logger is not None:
        try:
            logger(
                f"PGG2-V46-PENDING-FLOW-PROJECTION mint={_short(mint_for_log)} "
                f"proj={out['projected_pnl']:+.6f} "
                f"stress={out['stress_pnl']:+.6f} "
                f"used={out['pending_flow_sol_used']:.4f} "
                f"sell_out={out['projected_sell_out']:.6f} "
                f"reqflow={out['required_flow_sol']:.4f} "
                f"lead={out['source_lead_ms']:+.0f} "
                f"conf={out['confidence']:.2f}"
            )
        except Exception:
            pass
    return out


__all__ = ["project_with_pending_flow"]
