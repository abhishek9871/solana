"""V42H — LOCAL CURVE-QUOTE RUNNER ENGINE.

Phase 1: compute pump_bc CPMM buy/sell quotes LOCALLY from accountSubscribe
curve state (virtual_sol_reserves, virtual_token_reserves) without round-tripping
to broker.bonding_curve()/broker.build_buy(). This eliminates the broker
quote cadence (150-500ms) bottleneck that V42G hit: now every accountSubscribe
update becomes a snapshot we can act on.

Quote math is byte-for-byte identical to
`DirectPumpQuoteBroker.quote_pump_buy_tokens / quote_pump_sell_sol`
in pgg2_direct_pump.py lines 880-895. Fee model: protocol fee_bps +
creator_fee_bps (read once from pump_global() and cached).

Causality: this module performs PURE arithmetic on curve state — no network,
no transactions. The accountSubscribe watcher is the data source; this is
just the math.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple


LAMPORTS_PER_SOL = 1_000_000_000
# Default tx fee (live-mirror env value):
# PGG2_V39_LIVE_ROUTE_AWARE_TX_FEE_SOL=0.0000287
DEFAULT_TX_FEE_SOL = 0.0000287

# Sample 1/100 emits to avoid log flood.
_EMIT_SAMPLE_DENOM = max(1, int(os.environ.get("PGG2_V42H_QUOTE_EMIT_SAMPLE_DENOM", "100")))
_emit_counter = 0


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // max(1, b)


@dataclass(frozen=True)
class V42HCurveState:
    """Minimal curve state. We don't need creator/pubkey — only the reserves
    and the global fee bps. real_token_reserves is used as a quote cap."""
    virtual_sol_reserves: int
    virtual_token_reserves: int
    real_token_reserves: int
    fee_bps: int
    creator_fee_bps: int


def curve_state_from_pump_curve(curve_obj, global_cfg) -> V42HCurveState:
    """Build a V42HCurveState from a DirectPumpQuoteBroker.bonding_curve()
    return + pump_global() return. Defensive against partial objects."""
    return V42HCurveState(
        virtual_sol_reserves=int(getattr(curve_obj, "virtual_sol_reserves", 0)),
        virtual_token_reserves=int(getattr(curve_obj, "virtual_token_reserves", 0)),
        real_token_reserves=int(getattr(curve_obj, "real_token_reserves", 0)),
        fee_bps=int(getattr(global_cfg, "fee_bps", 100)),
        creator_fee_bps=int(getattr(global_cfg, "creator_fee_bps", 0)),
    )


def curve_state_from_subscriber_point(
    vsol: int, vtok: int, rtok: int, fee_bps: int, creator_fee_bps: int
) -> V42HCurveState:
    """Build a V42HCurveState directly from raw V42C subscriber fields."""
    return V42HCurveState(
        virtual_sol_reserves=int(vsol),
        virtual_token_reserves=int(vtok),
        real_token_reserves=int(rtok),
        fee_bps=int(fee_bps),
        creator_fee_bps=int(creator_fee_bps),
    )


def local_buy_quote_tokens_raw(
    curve_state: V42HCurveState, amount_sol: float
) -> Tuple[int, int]:
    """LOCAL replica of DirectPumpQuoteBroker.quote_pump_buy_tokens.

    Returns (tokens_raw, fee_lamports). Identical integer-math sequence to
    pgg2_direct_pump.py lines 880-888.
    """
    spend_lamports = int(round(float(amount_sol) * LAMPORTS_PER_SOL))
    total_fee_bps = max(0, int(curve_state.fee_bps) + int(curve_state.creator_fee_bps))
    net_sol = spend_lamports * 10_000 // (10_000 + total_fee_bps)
    fees = (
        _ceil_div(net_sol * int(curve_state.fee_bps), 10_000)
        + _ceil_div(net_sol * int(curve_state.creator_fee_bps), 10_000)
    )
    if net_sol + fees > spend_lamports:
        net_sol -= net_sol + fees - spend_lamports
    net_for_curve = max(0, net_sol - 1)
    denom = max(int(curve_state.virtual_sol_reserves) + net_for_curve, 1)
    tokens = net_for_curve * int(curve_state.virtual_token_reserves) // denom
    tokens = max(0, min(int(tokens), int(curve_state.real_token_reserves)))
    return tokens, max(0, fees)


def local_sell_quote_sol(
    curve_state: V42HCurveState, tokens_raw: int
) -> Tuple[int, int]:
    """LOCAL replica of DirectPumpQuoteBroker.quote_pump_sell_sol.

    Returns (gross_sol_lamports_net_of_fees, fee_lamports). Identical to
    pgg2_direct_pump.py lines 890-895 (returns the NET gross, after fees).
    """
    if int(tokens_raw) <= 0:
        return 0, 0
    denom = max(int(curve_state.virtual_token_reserves) + int(tokens_raw), 1)
    gross_sol = int(tokens_raw) * int(curve_state.virtual_sol_reserves) // denom
    protocol_fee = _ceil_div(gross_sol * int(curve_state.fee_bps), 10_000)
    creator_fee = _ceil_div(gross_sol * int(curve_state.creator_fee_bps), 10_000)
    fees = protocol_fee + creator_fee
    return max(0, gross_sol - fees), max(0, fees)


def local_roundtrip_label(
    curve_state_buy: V42HCurveState,
    curve_state_sell: V42HCurveState,
    amount_sol: float,
    tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
) -> float:
    """Inter-snapshot PnL: buy tokens at curve_state_buy, sell at curve_state_sell.

    This is the CAUSAL future-PnL: the buy snapshot was observed strictly before
    the sell snapshot. Returns PnL in SOL net of all fees.
    """
    tokens_raw, _buy_fee = local_buy_quote_tokens_raw(curve_state_buy, amount_sol)
    if tokens_raw <= 0:
        return -float(amount_sol) - 2.0 * float(tx_fee_sol)
    sell_lamports, _sell_fee = local_sell_quote_sol(curve_state_sell, tokens_raw)
    sell_sol = float(sell_lamports) / LAMPORTS_PER_SOL
    return sell_sol - float(amount_sol) - 2.0 * float(tx_fee_sol)


def local_live_equiv_pnl(
    curve_state: V42HCurveState,
    amount_sol: float,
    tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
) -> float:
    """Single-snapshot live-equiv PnL: how much would we net if we bought and
    sold at the same curve state? Returns SOL net of fees.

    V42E proved this is structurally negative on pump_bc — protocol fees and
    curve impact subtract ~0.0004 SOL on a 0.015 SOL trade. This metric is
    DIAGNOSTIC ONLY; do not use it as a positive-edge gate. Use
    `local_roundtrip_label` between strictly-ordered snapshots.
    """
    tokens_raw, _buy_fee = local_buy_quote_tokens_raw(curve_state, amount_sol)
    if tokens_raw <= 0:
        return -float(amount_sol) - 2.0 * float(tx_fee_sol)
    sell_lamports, _sell_fee = local_sell_quote_sol(curve_state, tokens_raw)
    sell_sol = float(sell_lamports) / LAMPORTS_PER_SOL
    return sell_sol - float(amount_sol) - 2.0 * float(tx_fee_sol)


def break_even_sell_out_sol(
    amount_sol: float, tx_fee_sol: float = DEFAULT_TX_FEE_SOL, safety_buffer_sol: float = 0.0
) -> float:
    """Sell-out amount at which round-trip pnl = -safety_buffer_sol.

    For "break even ignoring drift", safety_buffer_sol = 0. To require positive
    edge above cost+fees, pass safety_buffer_sol = the required edge.
    """
    return float(amount_sol) + 2.0 * float(tx_fee_sol) - float(safety_buffer_sol)


def maybe_emit_quote_log(
    log_fn, mint_short: str, buy_tokens: int, sell_sol: float, pnl_sol: float
) -> None:
    """Sample-emit one local-quote log line per 100 calls (configurable). The
    log line is fire-and-forget — failures are swallowed."""
    global _emit_counter
    _emit_counter += 1
    if _emit_counter % _EMIT_SAMPLE_DENOM != 0:
        return
    try:
        log_fn(
            f"PGG2-V42H-LOCAL-QUOTE mint={mint_short} buy_tokens={buy_tokens} "
            f"sell_sol={sell_sol:.9f} pnl={pnl_sol:+.9f}"
        )
    except Exception:
        pass


__all__ = [
    "LAMPORTS_PER_SOL",
    "DEFAULT_TX_FEE_SOL",
    "V42HCurveState",
    "curve_state_from_pump_curve",
    "curve_state_from_subscriber_point",
    "local_buy_quote_tokens_raw",
    "local_sell_quote_sol",
    "local_roundtrip_label",
    "local_live_equiv_pnl",
    "break_even_sell_out_sol",
    "maybe_emit_quote_log",
]
