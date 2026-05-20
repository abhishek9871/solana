"""V42J Phase 2 - Immediate local reprice at bank event.

Computes the six required reprice fields entirely from local CPMM math
at the bank-event moment. No broker RTT. No simulation calls. Pure
arithmetic on the V42HCurveState the bank-event captured.

The doc explicitly says: "do not require same-state roundtrip to be
positive. Same-state is structurally negative. The entry is allowed only
if the bank-event model predicts the next continuation tick remains
positive under stress."

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
import time
from typing import Any, Callable, Dict, Optional


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
            f"V42J-REPRICE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v42j_reprice")


from pgg2_v42h_local_curve_quote import (
    V42HCurveState,
    local_buy_quote_tokens_raw,
    local_sell_quote_sol,
    LAMPORTS_PER_SOL,
    DEFAULT_TX_FEE_SOL,
)
from pgg2_v42j_bank_event import V42JBankEvent


# Default magnitudes for projected (+) and stress (-) continuation ticks.
# These operate on the post-our-buy virtual_sol_reserves.
# Calibrated against typical accountSubscribe inter-snap deltas observed
# on pump_bc bonding curves (~0.1-0.5% magnitudes). The spec quoted
# "e.g., +0.5%" verbally but a 0.5% stress is the upper boundary of what
# we'd see between consecutive snaps; a 0.1% stress reflects the typical
# adverse tick magnitude (clamp model).
PROJ_TICK_FRAC = 0.005   # +0.5% (matches spec verbal example)
STRESS_TICK_FRAC = -0.005  # -0.5% (matches spec verbal example; meaningful adverse)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _simulate_buy_post_state(
    cs: V42HCurveState, amount_sol: float
) -> "tuple[V42HCurveState, int]":
    """Apply our 0.015 SOL buy to the curve. Returns (post_state, tokens_out).

    Uses the SAME integer-math sequence as local_buy_quote_tokens_raw so
    we stay byte-for-byte consistent with the production buy quote.

    Curve update rule (constant-product CPMM with fees external):
        new_vsol = vsol + net_for_curve            # what hit the curve
        new_vtok = vtok - tokens_out               # what we removed
        new_rtok = rtok - tokens_out               # real-token cap drops too

    The fee portion does NOT go onto the curve (it's collected separately
    by the protocol). So we add net_for_curve, not the full lamports.
    """
    spend_lamports = int(round(float(amount_sol) * LAMPORTS_PER_SOL))
    total_fee_bps = max(0, int(cs.fee_bps) + int(cs.creator_fee_bps))
    net_sol = spend_lamports * 10_000 // (10_000 + total_fee_bps)
    fees = (
        (net_sol * int(cs.fee_bps) + 9_999) // 10_000
        + (net_sol * int(cs.creator_fee_bps) + 9_999) // 10_000
    )
    if net_sol + fees > spend_lamports:
        net_sol -= net_sol + fees - spend_lamports
    net_for_curve = max(0, net_sol - 1)
    denom = max(int(cs.virtual_sol_reserves) + net_for_curve, 1)
    tokens = net_for_curve * int(cs.virtual_token_reserves) // denom
    tokens = max(0, min(int(tokens), int(cs.real_token_reserves)))
    new_vsol = int(cs.virtual_sol_reserves) + int(net_for_curve)
    new_vtok = int(cs.virtual_token_reserves) - int(tokens)
    new_rtok = max(0, int(cs.real_token_reserves) - int(tokens))
    post = V42HCurveState(
        virtual_sol_reserves=int(new_vsol),
        virtual_token_reserves=int(new_vtok),
        real_token_reserves=int(new_rtok),
        fee_bps=int(cs.fee_bps),
        creator_fee_bps=int(cs.creator_fee_bps),
    )
    return post, int(tokens)


def _apply_tick(cs: V42HCurveState, vsol_frac: float) -> V42HCurveState:
    """Apply a relative tick to virtual_sol_reserves. Keeps vtok and rtok
    constant - the next market action that arrives is a buy (price rises)
    or a sell (price falls). For our forward-looking projection we shift
    vsol by the fractional amount and leave vtok unchanged (which models
    "another trader pushes the SOL side up/down without consuming tokens
    yet"). This is the cleanest, most conservative tick model.
    """
    delta = int(round(float(cs.virtual_sol_reserves) * float(vsol_frac)))
    new_vsol = max(1, int(cs.virtual_sol_reserves) + delta)
    return V42HCurveState(
        virtual_sol_reserves=new_vsol,
        virtual_token_reserves=int(cs.virtual_token_reserves),
        real_token_reserves=int(cs.real_token_reserves),
        fee_bps=int(cs.fee_bps),
        creator_fee_bps=int(cs.creator_fee_bps),
    )


def _sell_out_sol(cs: V42HCurveState, tokens: int) -> float:
    sell_lamports, _fee = local_sell_quote_sol(cs, int(tokens))
    return float(sell_lamports) / float(LAMPORTS_PER_SOL)


def reprice_at_bank_event(
    event: V42JBankEvent,
    local_quote_engine: Any = None,
    current_snap: Any = None,
    amount_sol: float = 0.015,
    tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
    proj_tick_frac: float = PROJ_TICK_FRAC,
    stress_tick_frac: float = STRESS_TICK_FRAC,
    now_ts_ms: Optional[int] = None,
    logger: Optional[Callable[[str], None]] = None,
    virtual_ticket_engine: Any = None,
) -> Dict[str, Any]:
    """Compute the six reprice fields at the bank event.

    Args:
        event: V42JBankEvent emitted in this same call frame.
        local_quote_engine: ignored (we use local CPMM math directly); kept
            for ABI compatibility with the spec signature.
        current_snap: the LocalCurveSnapshot from on_curve_update; preferred
            source for curve_state. If None, we reconstruct from event.
        amount_sol: trade size (0.015).
        tx_fee_sol: round-trip tx fee per leg (used twice).
        proj_tick_frac: projection upward tick on vsol (+0.5% default).
        stress_tick_frac: stress downward tick on vsol (-0.5% default).
        now_ts_ms: when reprice happens. If None -> _now_ms.
    """
    if now_ts_ms is None:
        now_ts_ms = _now_ms()

    # Reconstruct curve state from current_snap (preferred) or event.
    if current_snap is not None and hasattr(current_snap, "curve_state"):
        cs = current_snap.curve_state
    else:
        ccs = event.current_curve_state
        cs = V42HCurveState(
            virtual_sol_reserves=int(ccs.get("virtual_sol_reserves", 0)),
            virtual_token_reserves=int(ccs.get("virtual_token_reserves", 0)),
            real_token_reserves=int(ccs.get("real_token_reserves", 0)),
            fee_bps=int(ccs.get("fee_bps", 100)),
            creator_fee_bps=int(ccs.get("creator_fee_bps", 0)),
        )

    # bank_event_current_buy_tokens_raw = fresh 0.015 SOL buy at current state
    cur_buy_tokens, _bf = local_buy_quote_tokens_raw(cs, amount_sol)

    # Same-state immediate sell of those tokens (structurally negative).
    same_state_sell_sol = _sell_out_sol(cs, cur_buy_tokens) if cur_buy_tokens > 0 else 0.0
    same_state_pnl = same_state_sell_sol - float(amount_sol) - 2.0 * float(tx_fee_sol)

    # Apply our buy to get post-state for self-impact-aware projection.
    post_state, post_buy_tokens = _simulate_buy_post_state(cs, amount_sol)
    # post_buy_tokens should equal cur_buy_tokens by construction (same math).

    # PROJECTION MODEL - bank-derived continuation.
    # The triggering ticket bought at vsol_open and sees bank_pnl now at
    # vsol_now. The observed curve lift between open->now (let it be
    # observed_lift_frac = vsol_now/vsol_open - 1) is the empirical
    # tick magnitude that ALREADY delivered +bank_pnl on a 0.015-SOL trade
    # entering at vsol_open. The continuation hypothesis is: ONE MORE tick
    # of comparable magnitude lifts vsol further by observed_lift_frac,
    # delivering analogous PnL to a buyer at vsol_now (i.e., us).
    #
    # If we cannot read the triggering ticket's open state (no engine ref),
    # we fall back to a bank-pnl-implied lift: bank_pnl / amount_sol.
    # In both cases we clamp the projected lift to a safe ceiling.
    observed_lift_frac = 0.0
    if virtual_ticket_engine is not None:
        try:
            tk = virtual_ticket_engine.get_ticket(event.triggering_ticket_id)
            if tk is not None and tk.buy_curve_state is not None:
                vopen = int(tk.buy_curve_state.virtual_sol_reserves)
                vnow = int(cs.virtual_sol_reserves)
                if vopen > 0:
                    observed_lift_frac = max(0.0, (vnow - vopen) / float(vopen))
        except Exception:
            observed_lift_frac = 0.0
    if observed_lift_frac <= 0.0:
        bank_pnl_val = float(getattr(event, "bank_pnl", 0.0) or 0.0)
        observed_lift_frac = max(0.0, bank_pnl_val / float(amount_sol))
    # Clamp.
    observed_lift_frac = min(observed_lift_frac, 0.20)
    # Project one continuation tick of equal magnitude on top of post_state.
    proj_frac_effective = max(float(proj_tick_frac), observed_lift_frac)
    proj_state = _apply_tick(post_state, proj_frac_effective)
    proj_sell_sol = _sell_out_sol(proj_state, post_buy_tokens)
    proj_pnl = proj_sell_sol - float(amount_sol) - 2.0 * float(tx_fee_sol)

    # Stress: -stress_tick on vsol AFTER our buy hits the curve.
    stress_state = _apply_tick(post_state, stress_tick_frac)
    stress_sell_sol = _sell_out_sol(stress_state, post_buy_tokens)
    stress_pnl = stress_sell_sol - float(amount_sol) - 2.0 * float(tx_fee_sol)

    freshness_ms = int(now_ts_ms) - int(event.event_ts_ms)

    # Spec also requires source_late and route, sim_needed.
    out: Dict[str, Any] = {
        "bank_event_current_buy_tokens_raw": int(cur_buy_tokens),
        "bank_event_self_impact_post_buy_tokens_raw": int(post_buy_tokens),
        "bank_event_projected_pnl": float(proj_pnl),
        "bank_event_stress_pnl": float(stress_pnl),
        "bank_event_same_state_pnl": float(same_state_pnl),
        "bank_event_freshness_ms": int(freshness_ms),
        "route": "pump_bc",
        "sim_needed": 0,
        "source_late": False,
        # Diagnostics:
        "current_sell_sol_at_now": float(same_state_sell_sol),
        "proj_sell_sol": float(proj_sell_sol),
        "stress_sell_sol": float(stress_sell_sol),
        "post_state_vsol": int(post_state.virtual_sol_reserves),
        "post_state_vtok": int(post_state.virtual_token_reserves),
    }

    log = logger
    if log is not None:
        try:
            log(
                f"PGG2-V42J-BANK-EVENT-REPRICE mint={_short(event.mint)} "
                f"cur_buy_tok={cur_buy_tokens} post_buy_tok={post_buy_tokens} "
                f"proj_pnl={proj_pnl:+.9f} stress_pnl={stress_pnl:+.9f} "
                f"same_pnl={same_state_pnl:+.9f} freshness={freshness_ms}"
            )
        except Exception:
            pass

    return out


__all__ = [
    "reprice_at_bank_event",
    "PROJ_TICK_FRAC",
    "STRESS_TICK_FRAC",
]
