"""V47 Phase 1 - Size-normalized edge evaluator.

Compute per-size economics for a candidate pump_bc entry:
  - buy_tokens_raw and post-buy curve state (CPMM math reused from
    pgg2_v42h_local_curve_quote)
  - projected_sell_out_sol after applying pending external buys/sells
    (reuses V46 pending-flow projector primitives)
  - stress_sell_out_sol = worst of the V46 four stress models for this
    specific size
  - protocol/creator fees on both buy and sell legs
  - signature tx fee (2 * DEFAULT_TX_FEE_SOL) and priority fee
  - ATA rent (caller-supplied; default 0 if ATA already exists)
  - all_in_pnl, stress_all_in_pnl
  - bps metrics: edge_bps, self_impact_bps, fee_drag_bps
  - min-out guard encodability (u64 fit; > 0)
  - SIZE-NORMALIZED required profit:
        required_profit_sol = max(2 * tx_fee_sol + priority_fee_buffer
                                  + 0.000005,
                                  size_sol * 0.0010)
  - meets_required_profit / meets_zero_loss_stress

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import os
import re as _re
import sys
from typing import Any, Dict, List, Optional, Tuple


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
            f"V47-EDGE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47_size_normalized_edge")


# Defer heavy imports to call-time (per V46 pattern).
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


# Re-export DEFAULT_TX_FEE_SOL at module-load time so callers can use it
# as a default arg without forcing them to import the helpers themselves.
try:
    sys.path.insert(0, "/root/piggy")
    from pgg2_v42h_local_curve_quote import (
        DEFAULT_TX_FEE_SOL as _DEFAULT_TX_FEE_SOL,
    )
    DEFAULT_TX_FEE_SOL = _DEFAULT_TX_FEE_SOL
except Exception:
    DEFAULT_TX_FEE_SOL = 0.0000287


U64_MAX = (1 << 64) - 1
SELF_IMPACT_CAP_BPS = 250.0  # 2.5% absolute curve impact cap
MIN_GUARD_SLIPPAGE = 0.99  # min_out = best * 0.99 (1% slippage allowance)


def _apply_external_buy_to_curve(
    curve_state, sol_in, V42HCurveState, LAMPORTS_PER_SOL,
    local_buy_quote_tokens_raw,
):
    """Replicate V46 projector's _apply_external_buy_to_curve.

    Returns (new_state, tokens_out). PURE arithmetic.
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
    curve_state, tokens_in, V42HCurveState,
    local_sell_quote_sol,
):
    gross_sol_net, fees = local_sell_quote_sol(curve_state, int(tokens_in))
    new_vtok = int(curve_state.virtual_token_reserves) + int(tokens_in)
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


def _apply_all_pending_to_curve(
    state, pending_buys, pending_sells,
    V42HCurveState, LAMPORTS_PER_SOL,
    local_buy_quote_tokens_raw, local_sell_quote_sol,
):
    """Apply pending buys (ts-sorted) then pending sells (ts-sorted)."""
    cur = state
    if pending_buys:
        for ts, sol_in, _sig, _sl in sorted(pending_buys, key=lambda x: x[0]):
            if sol_in <= 0:
                continue
            cur, _ = _apply_external_buy_to_curve(
                cur, float(sol_in), V42HCurveState, LAMPORTS_PER_SOL,
                local_buy_quote_tokens_raw,
            )
    if pending_sells:
        for ts, tok_in, _sig, _sl in sorted(pending_sells, key=lambda x: x[0]):
            if tok_in <= 0:
                continue
            cur = _apply_external_sell_to_curve(
                cur, int(tok_in), V42HCurveState, local_sell_quote_sol,
            )
    return cur


def _stress_sell_out_for_size(
    base_state, pending_buys, pending_sells, size_sol,
    V42HCurveState, LAMPORTS_PER_SOL,
    local_buy_quote_tokens_raw, local_sell_quote_sol,
):
    """Run V46's 4 stress models for a SPECIFIC size; return worst sell_out_sol.

    Models:
      (a) only 50% of pending buys land after us
      (b) the largest pending buy was already priced in
      (c) a pending sell equal in size to the largest pending buy appears
      (d) our buy lands LAST after all pending flow (250ms exec delay)

    Returns: (worst_sell_out_sol, dict_of_per_model_sell_outs).
    """
    # Compute tokens_we_get under each scenario then sell at the resulting
    # state. The "sell_out_sol" is the projected gross-after-fees we receive.
    sell_outs = {}

    # (a) 50% pending buys after us
    pending_a = [(t, s * 0.5, sig, sl) for (t, s, sig, sl) in pending_buys]
    state_a_after_us, tok_a = _apply_external_buy_to_curve(
        base_state, float(size_sol), V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw,
    )
    state_a = _apply_all_pending_to_curve(
        state_a_after_us, pending_a, pending_sells,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    sell_lams_a, _ = local_sell_quote_sol(state_a, int(tok_a))
    sell_outs["a_50pct_drop"] = float(sell_lams_a) / float(LAMPORTS_PER_SOL)

    # (b) drop largest pending buy
    if pending_buys:
        sorted_b = sorted(pending_buys, key=lambda x: -float(x[1]))
        without_largest = sorted_b[1:]
    else:
        without_largest = []
    state_b_after_us, tok_b = _apply_external_buy_to_curve(
        base_state, float(size_sol), V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw,
    )
    state_b = _apply_all_pending_to_curve(
        state_b_after_us, without_largest, pending_sells,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    sell_lams_b, _ = local_sell_quote_sol(state_b, int(tok_b))
    sell_outs["b_largest_priced_in"] = float(sell_lams_b) / float(LAMPORTS_PER_SOL)

    # (c) a pending sell equal in size to largest buy appears
    sells_c = list(pending_sells or [])
    if pending_buys:
        largest_sol = float(max(b[1] for b in pending_buys))
        tokens_for_largest, _ = local_buy_quote_tokens_raw(
            base_state, float(largest_sol)
        )
        if tokens_for_largest > 0:
            sells_c.append((0, int(tokens_for_largest), "_v47_stress_c", 0))
    state_c_after_us, tok_c = _apply_external_buy_to_curve(
        base_state, float(size_sol), V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw,
    )
    state_c = _apply_all_pending_to_curve(
        state_c_after_us, pending_buys, sells_c,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    sell_lams_c, _ = local_sell_quote_sol(state_c, int(tok_c))
    sell_outs["c_sell_appears"] = float(sell_lams_c) / float(LAMPORTS_PER_SOL)

    # (d) we land LAST after all pending flow
    state_after_pending = _apply_all_pending_to_curve(
        base_state, pending_buys, pending_sells,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    tok_d, _ = local_buy_quote_tokens_raw(state_after_pending, float(size_sol))
    if tok_d > 0:
        state_after_us_d, _ = _apply_external_buy_to_curve(
            state_after_pending, float(size_sol),
            V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
        )
        sell_lams_d, _ = local_sell_quote_sol(state_after_us_d, int(tok_d))
        sell_outs["d_we_land_last"] = float(sell_lams_d) / float(LAMPORTS_PER_SOL)
    else:
        sell_outs["d_we_land_last"] = 0.0

    worst = min(sell_outs.values())
    return float(worst), sell_outs


def _projected_sell_out_for_size(
    base_state, pending_buys, pending_sells, size_sol,
    V42HCurveState, LAMPORTS_PER_SOL,
    local_buy_quote_tokens_raw, local_sell_quote_sol,
):
    """Baseline: our buy lands first; pending buys/sells land AFTER us.
    Sell our tokens at the final state. Returns projected_sell_out_sol.
    """
    state_after_us, tokens_we_got = _apply_external_buy_to_curve(
        base_state, float(size_sol), V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw,
    )
    if tokens_we_got <= 0:
        return 0.0, 0, state_after_us
    final_state = _apply_all_pending_to_curve(
        state_after_us, pending_buys, pending_sells,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )
    sell_lams, _ = local_sell_quote_sol(final_state, int(tokens_we_got))
    return (
        float(sell_lams) / float(LAMPORTS_PER_SOL),
        int(tokens_we_got),
        state_after_us,
    )


def evaluate_size(
    size_sol: float,
    latest_curve_state,
    pending_buys: List[Tuple[int, float, str, int]],
    pending_sells: List[Tuple[int, int, str, int]],
    our_priority_fee_lamports: int = 0,
    tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
    priority_fee_buffer: float = 0.000010,
    ata_rent_sol: float = 0.0,
) -> Dict[str, Any]:
    """V47 size-normalized economics for a single candidate size.

    Args:
      size_sol: candidate buy size in SOL (>0).
      latest_curve_state: V42HCurveState - PUMP_BC reserves + fee_bps.
      pending_buys: pending external BUYS (ts_ms, sol_in, signer, slot).
      pending_sells: pending external SELLS (ts_ms, tokens_in, signer, slot).
      our_priority_fee_lamports: planned priority fee for our buy+sell (sum
        across both tx; lamports).
      tx_fee_sol: SOL per signature; multiplied by 2 for buy+sell pair.
      priority_fee_buffer: explicit headroom over fees in required-profit
        (default 0.000010 SOL).
      ata_rent_sol: 0 if our ATA already exists; ~0.00203928 on first buy
        per (mint, wallet). Caller controls.

    Returns: dict with all economics fields (see module docstring + spec).
    Pure arithmetic; no I/O.
    """
    (
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL_X,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    ) = _import_quote_helpers()

    size_sol_f = float(size_sol)
    if size_sol_f <= 0:
        return {
            "size_sol": float(size_sol_f),
            "error": "size_must_be_positive",
            "buy_tokens_raw": 0,
            "post_buy_virtual_sol_reserves": int(latest_curve_state.virtual_sol_reserves),
            "post_buy_virtual_token_reserves": int(latest_curve_state.virtual_token_reserves),
            "projected_sell_out_sol": 0.0,
            "stress_sell_out_sol": 0.0,
            "all_in_pnl": -1e9,
            "stress_all_in_pnl": -1e9,
            "edge_bps": -1e9,
            "self_impact_bps": 0.0,
            "fee_drag_bps": 1e9,
            "min_token_buy_guard": 0,
            "min_sol_sell_guard": 0,
            "guards_encodable": False,
            "required_profit_sol": 0.0,
            "meets_required_profit": False,
            "meets_zero_loss_stress": False,
        }

    # 1. CPMM buy at base state
    buy_tokens_raw, buy_fee_lamports = local_buy_quote_tokens_raw(
        latest_curve_state, size_sol_f
    )

    # 2. Post-buy state (our buy only; no pending applied yet — that comes
    # in projected/stress)
    if buy_tokens_raw > 0:
        post_buy_state, _ = _apply_external_buy_to_curve(
            latest_curve_state, size_sol_f, V42HCurveState, LAMPORTS_PER_SOL,
            local_buy_quote_tokens_raw,
        )
    else:
        post_buy_state = latest_curve_state

    post_buy_vsol = int(post_buy_state.virtual_sol_reserves)
    post_buy_vtok = int(post_buy_state.virtual_token_reserves)

    # 3. Projected sell-out
    projected_sell_out_sol, tokens_we_got, _ = _projected_sell_out_for_size(
        latest_curve_state, pending_buys, pending_sells, size_sol_f,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )

    # 4. Stress sell-out (worst-of-4)
    stress_sell_out_sol, stress_breakdown = _stress_sell_out_for_size(
        latest_curve_state, pending_buys, pending_sells, size_sol_f,
        V42HCurveState, LAMPORTS_PER_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )

    # 5. Fees. Pump.fun bonding curve: BOTH buy and sell legs charge
    # protocol fee_bps + creator_fee_bps on the SOL leg. The buy leg fee is
    # deducted from spend_sol; the sell leg fee is deducted from gross_sol.
    fee_bps = int(latest_curve_state.fee_bps)
    creator_bps = int(latest_curve_state.creator_fee_bps)
    protocol_fee_buy_sol = float(size_sol_f * fee_bps / 10000.0)
    creator_fee_buy_sol = float(size_sol_f * creator_bps / 10000.0)
    # Sell fees apply to the GROSS sol coming out of the curve (before fees).
    # projected_sell_out_sol is net-of-fees from local_sell_quote_sol; the
    # gross is approximately projected_sell_out / (1 - total_fee_bps/10000).
    total_sell_fee_bps = float(fee_bps + creator_bps)
    gross_sell_factor = 1.0 - (total_sell_fee_bps / 10000.0)
    if gross_sell_factor > 0:
        projected_sell_gross_sol = projected_sell_out_sol / gross_sell_factor
        stress_sell_gross_sol = stress_sell_out_sol / gross_sell_factor
    else:
        projected_sell_gross_sol = projected_sell_out_sol
        stress_sell_gross_sol = stress_sell_out_sol
    protocol_fee_sell_sol = float(
        projected_sell_gross_sol * fee_bps / 10000.0
    )
    creator_fee_sell_sol = float(
        projected_sell_gross_sol * creator_bps / 10000.0
    )

    # 6. Signature fee (2 tx: buy + sell) and priority fee
    signature_tx_fee_sol = float(2.0 * float(tx_fee_sol))
    priority_fee_sol = float(int(our_priority_fee_lamports)) / float(LAMPORTS_PER_SOL)

    # 7. PnL (note: local_sell_quote_sol already returns net-of-sell-fees;
    # we DO NOT double-subtract sell-fees in all_in_pnl)
    total_signature_and_priority = (
        signature_tx_fee_sol + priority_fee_sol + float(ata_rent_sol)
    )
    all_in_pnl = (
        float(projected_sell_out_sol)
        - size_sol_f
        - total_signature_and_priority
    )
    stress_all_in_pnl = (
        float(stress_sell_out_sol)
        - size_sol_f
        - total_signature_and_priority
    )

    # 8. Bps metrics
    edge_bps = (all_in_pnl / size_sol_f) * 10000.0 if size_sol_f > 0 else -1e9
    # Self-impact: signed % change in virtual_sol (positive when we push price up)
    base_vsol = int(latest_curve_state.virtual_sol_reserves)
    if base_vsol > 0:
        self_impact_bps = (
            (post_buy_vsol / float(base_vsol)) - 1.0
        ) * 10000.0
    else:
        self_impact_bps = 0.0
    # Fee drag (all fees normalized vs trade size)
    total_fees_sol = (
        protocol_fee_buy_sol + creator_fee_buy_sol
        + protocol_fee_sell_sol + creator_fee_sell_sol
        + signature_tx_fee_sol + priority_fee_sol + float(ata_rent_sol)
    )
    fee_drag_bps = (total_fees_sol / size_sol_f) * 10000.0 if size_sol_f > 0 else 0.0

    # 9. Min-out guards
    # min_token_buy_guard = floor(buy_tokens_raw * 0.99) (caller will encode in u64)
    min_token_buy_guard = int(buy_tokens_raw * MIN_GUARD_SLIPPAGE)
    # min_sol_sell_guard = floor(stress_sell_out_sol_lamports * 0.99)
    min_sol_sell_lamports = int(
        round(stress_sell_out_sol * LAMPORTS_PER_SOL * MIN_GUARD_SLIPPAGE)
    )
    if min_sol_sell_lamports < 0:
        min_sol_sell_lamports = 0
    guards_encodable = bool(
        min_token_buy_guard > 0
        and 0 < min_sol_sell_lamports <= U64_MAX
        and min_token_buy_guard <= U64_MAX
    )

    # 10. Size-normalized required profit
    required_profit_sol = max(
        2.0 * float(tx_fee_sol)
        + float(priority_fee_buffer)
        + 0.000005,
        size_sol_f * 0.0010,
    )
    meets_required_profit = bool(all_in_pnl >= required_profit_sol)
    meets_zero_loss_stress = bool(stress_all_in_pnl >= 0.0)

    return {
        "size_sol": float(size_sol_f),
        "buy_tokens_raw": int(buy_tokens_raw),
        "post_buy_virtual_sol_reserves": int(post_buy_vsol),
        "post_buy_virtual_token_reserves": int(post_buy_vtok),
        "projected_sell_out_sol": float(projected_sell_out_sol),
        "stress_sell_out_sol": float(stress_sell_out_sol),
        "stress_breakdown": stress_breakdown,
        "protocol_fee_buy_sol": float(protocol_fee_buy_sol),
        "creator_fee_buy_sol": float(creator_fee_buy_sol),
        "protocol_fee_sell_sol": float(protocol_fee_sell_sol),
        "creator_fee_sell_sol": float(creator_fee_sell_sol),
        "signature_tx_fee_sol": float(signature_tx_fee_sol),
        "priority_fee_sol": float(priority_fee_sol),
        "ata_rent_sol": float(ata_rent_sol),
        "total_fees_sol": float(total_fees_sol),
        "all_in_pnl": float(all_in_pnl),
        "stress_all_in_pnl": float(stress_all_in_pnl),
        "edge_bps": float(edge_bps),
        "self_impact_bps": float(self_impact_bps),
        "fee_drag_bps": float(fee_drag_bps),
        "min_token_buy_guard": int(min_token_buy_guard),
        "min_sol_sell_guard": int(min_sol_sell_lamports),
        "guards_encodable": bool(guards_encodable),
        "required_profit_sol": float(required_profit_sol),
        "meets_required_profit": bool(meets_required_profit),
        "meets_zero_loss_stress": bool(meets_zero_loss_stress),
    }


__all__ = [
    "evaluate_size",
    "DEFAULT_TX_FEE_SOL",
    "SELF_IMPACT_CAP_BPS",
    "U64_MAX",
]
