"""V40 Route Matrix.

For a candidate mint, quote every available (buy_route, sell_route) pair and
emit a structured PGG2-V40-ROUTE-MATRIX log per pair.

All quotes are read-only on-chain CPMM math (uses the existing
`quote_pump_buy_tokens`, `quote_pump_sell_sol`, `quote_pumpswap_buy`,
`quote_pumpswap_sell` helpers on `DirectPumpQuoteBroker`). NO tx is signed
or sent here. Simulation is performed by the v40 atomic-arb module after
the route matrix is computed.

Route identifiers:
- "pump_bc"    : Pump.fun bonding curve (pre-migration)
- "pumpswap"   : PumpSwap AMM pool (post-migration; index=0 canonical pool)
- "pumpswap_1" : PumpSwap AMM pool index=1 (rare; used only if two pools exist)

Hard constraints:
- No send_* / sendTransaction call anywhere
- Pure read functions: rpc(getAccountInfo / getTokenAccountBalance) and CPMM math
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

from solders.pubkey import Pubkey

from birth_first_sniper import log, short_addr
from pgg2_direct_pump import (
    DirectPumpQuoteBroker,
    PUMP_AMM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    WSOL_MINT,
    PumpBondingCurve,
    PumpGlobal,
    PumpSwapGlobal,
    PumpSwapPool,
    as_pubkey,
    pda,
    u16,
)
from pgg2_live_raptor import LAMPORTS_PER_SOL


DEFAULT_TX_FEE_SOL = 0.0000287  # base sig fee + typical priority fee envelope (SOL)


@dataclass
class RouteAvailability:
    """Per-mint route inventory."""
    mint: str
    has_pump_bc: bool = False
    pump_bc_complete: bool = False
    pumpswap_pools: list[PumpSwapPool] = field(default_factory=list)
    curve: Optional[PumpBondingCurve] = None
    pump_global: Optional[PumpGlobal] = None
    pumpswap_global: Optional[PumpSwapGlobal] = None
    notes: list[str] = field(default_factory=list)

    def routes(self) -> list[str]:
        out: list[str] = []
        # pump_bc only usable if curve exists AND not migrated/complete.
        if self.has_pump_bc and not self.pump_bc_complete:
            out.append("pump_bc")
        for idx, _pool in enumerate(self.pumpswap_pools):
            out.append("pumpswap" if idx == 0 else f"pumpswap_{idx}")
        return out

    def pool_for(self, route: str) -> Optional[PumpSwapPool]:
        if route == "pumpswap":
            return self.pumpswap_pools[0] if self.pumpswap_pools else None
        if route.startswith("pumpswap_"):
            try:
                idx = int(route.split("_", 1)[1])
            except Exception:
                return None
            return self.pumpswap_pools[idx] if 0 <= idx < len(self.pumpswap_pools) else None
        return None


@dataclass
class RoutePairQuote:
    mint: str
    buy_route: str
    sell_route: str
    buy_amount_sol: float
    buy_quote_tokens_raw: int
    buy_quote_tokens_ui: float
    buy_quote_latency_ms: int
    sell_quote_out_lamports: int
    sell_quote_out_sol: float
    sell_quote_latency_ms: int
    fees_bps_buy: int
    fees_bps_sell: int
    tx_fee_estimate_sol: float
    estimated_all_in_pnl_sol: float
    sim_needed: bool
    account_completeness: bool
    blocker: str = ""

    def to_log_fields(self) -> str:
        return (
            f"mint={short_addr(self.mint)} "
            f"buy_route={self.buy_route} sell_route={self.sell_route} "
            f"buy_in={self.buy_amount_sol:.6f} "
            f"buy_tokens_raw={self.buy_quote_tokens_raw} "
            f"buy_tokens_ui={self.buy_quote_tokens_ui:.6f} "
            f"buy_quote_ms={self.buy_quote_latency_ms} "
            f"sell_out_sol={self.sell_quote_out_sol:.6f} "
            f"sell_quote_ms={self.sell_quote_latency_ms} "
            f"fee_bps_buy={self.fees_bps_buy} fee_bps_sell={self.fees_bps_sell} "
            f"tx_fee_sol={self.tx_fee_estimate_sol:.6f} "
            f"all_in_pnl_sol={self.estimated_all_in_pnl_sol:+.6f} "
            f"complete={1 if self.account_completeness else 0} "
            f"blocker={self.blocker or 'none'}"
        )


def _pumpswap_total_bps(g: PumpSwapGlobal) -> int:
    return int(g.lp_fee_bps + g.protocol_fee_bps + g.coin_creator_fee_bps)


def _pump_total_bps(g: PumpGlobal) -> int:
    return int(g.fee_bps + g.creator_fee_bps)


def _find_extra_pumpswap_pools(broker: DirectPumpQuoteBroker, mint: Pubkey, max_index: int = 4) -> list[PumpSwapPool]:
    """Probe canonical PDA seeds for pool indices 0..max_index-1. The base
    `pumpswap_pool` helper resolves the canonical (creator, mint, WSOL) pool
    at index 0 via several creator candidates and falls back to
    getProgramAccounts. This helper enumerates additional indices using the
    same creator candidates so we can detect rare multi-pool mints."""
    found: list[PumpSwapPool] = []
    candidates: list[Pubkey] = [pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint)), PUMP_PROGRAM_ID]
    try:
        curve = broker.bonding_curve(mint)
        candidates.append(curve.creator)
    except Exception:
        pass
    seen: set[str] = set()
    for idx in range(max_index):
        for creator in dict.fromkeys(candidates):
            pool_key = pda(PUMP_AMM_PROGRAM_ID, b"pool", u16(idx), bytes(creator), bytes(mint), bytes(WSOL_MINT))
            if str(pool_key) in seen:
                continue
            seen.add(str(pool_key))
            info = broker.account_info(pool_key)
            if not info:
                continue
            try:
                pool = broker.parse_pool(pool_key, broker.account_data(info))
            except Exception:
                continue
            if pool.base_mint == mint and pool.quote_mint == WSOL_MINT:
                if not any(p.key == pool.key for p in found):
                    found.append(pool)
    # Also include getProgramAccounts fallback (already in pumpswap_pool) to
    # catch non-canonical creator pools we may have missed.
    if not found:
        try:
            primary = broker.pumpswap_pool(mint)
            found.append(primary)
        except Exception:
            pass
    return found


def probe_route_availability(broker: DirectPumpQuoteBroker, mint_str: str) -> RouteAvailability:
    """Resolve curve + pool inventory for `mint_str`. Never raises; failures
    are recorded as `notes` and the relevant flags are left False."""
    mint = as_pubkey(mint_str)
    av = RouteAvailability(mint=mint_str)
    try:
        curve = broker.bonding_curve(mint)
        av.curve = curve
        av.has_pump_bc = True
        av.pump_bc_complete = bool(curve.complete)
    except Exception as exc:
        av.notes.append(f"bonding_curve_missing:{type(exc).__name__}")
    try:
        av.pump_global = broker.pump_global()
    except Exception as exc:
        av.notes.append(f"pump_global_missing:{type(exc).__name__}")
    try:
        av.pumpswap_global = broker.pumpswap_global()
    except Exception as exc:
        av.notes.append(f"pumpswap_global_missing:{type(exc).__name__}")
    try:
        pools = _find_extra_pumpswap_pools(broker, mint)
        av.pumpswap_pools = pools
    except Exception as exc:
        av.notes.append(f"pumpswap_pool_probe_failed:{type(exc).__name__}")
    return av


def _quote_buy(
    broker: DirectPumpQuoteBroker,
    av: RouteAvailability,
    route: str,
    spend_lamports: int,
) -> tuple[int, int, int, int]:
    """Return (tokens_out_raw, fee_bps_total, latency_ms, fees_lamports)."""
    t0 = time.monotonic()
    if route == "pump_bc":
        if not av.curve or not av.pump_global:
            return (0, 0, 0, 0)
        tokens, fees = broker.quote_pump_buy_tokens(spend_lamports, av.curve, av.pump_global)
        return (int(tokens), _pump_total_bps(av.pump_global), int((time.monotonic() - t0) * 1000), int(fees))
    pool = av.pool_for(route)
    if pool is None or av.pumpswap_global is None:
        return (0, 0, 0, 0)
    tokens, fees = broker.quote_pumpswap_buy(spend_lamports, pool, av.pumpswap_global)
    return (int(tokens), _pumpswap_total_bps(av.pumpswap_global), int((time.monotonic() - t0) * 1000), int(fees))


def _quote_sell(
    broker: DirectPumpQuoteBroker,
    av: RouteAvailability,
    route: str,
    tokens_raw: int,
) -> tuple[int, int, int, int]:
    """Return (sol_out_lamports, fee_bps_total, latency_ms, fees_lamports)."""
    t0 = time.monotonic()
    if tokens_raw <= 0:
        return (0, 0, 0, 0)
    if route == "pump_bc":
        if not av.curve or not av.pump_global:
            return (0, 0, 0, 0)
        gross, fees = broker.quote_pump_sell_sol(tokens_raw, av.curve, av.pump_global)
        return (int(gross), _pump_total_bps(av.pump_global), int((time.monotonic() - t0) * 1000), int(fees))
    pool = av.pool_for(route)
    if pool is None or av.pumpswap_global is None:
        return (0, 0, 0, 0)
    gross, fees = broker.quote_pumpswap_sell(tokens_raw, pool, av.pumpswap_global)
    return (int(gross), _pumpswap_total_bps(av.pumpswap_global), int((time.monotonic() - t0) * 1000), int(fees))


def build_route_matrix(
    broker: DirectPumpQuoteBroker,
    mint_str: str,
    buy_amount_sol: float,
    tx_fee_estimate_sol: float = DEFAULT_TX_FEE_SOL,
    *,
    include_same_route: bool = False,
) -> tuple[RouteAvailability, list[RoutePairQuote]]:
    """Quote every (buy_route, sell_route) pair for `mint`.

    `include_same_route=False` matches the v40 design: same-route round-trip
    is mathematically negative at 0.015 SOL (2x100bps fee). Set True to also
    emit pump_bc→pump_bc / pumpswap→pumpswap pairs for sanity-check reports.

    Emits one `PGG2-V40-ROUTE-MATRIX` log line per pair."""
    av = probe_route_availability(broker, mint_str)
    spend_lamports = max(1, int(buy_amount_sol * LAMPORTS_PER_SOL))
    routes = av.routes()
    pairs: list[RoutePairQuote] = []
    if not routes:
        log(
            f"PGG2-V40-ROUTE-MATRIX-EMPTY mint={short_addr(mint_str)} "
            f"reason=no_routes notes={','.join(av.notes) or 'none'}"
        )
        return av, pairs
    for buy_route in routes:
        tokens_out_raw, fee_bps_buy, buy_ms, _buy_fees_lp = _quote_buy(broker, av, buy_route, spend_lamports)
        if tokens_out_raw <= 0:
            continue
        for sell_route in routes:
            if not include_same_route and buy_route == sell_route:
                continue
            sol_out_lp, fee_bps_sell, sell_ms, _sell_fees_lp = _quote_sell(broker, av, sell_route, tokens_out_raw)
            buy_tokens_ui = broker.raw_to_ui(as_pubkey(mint_str), tokens_out_raw)
            sell_sol = sol_out_lp / LAMPORTS_PER_SOL
            all_in_pnl = sell_sol - (buy_amount_sol + 2 * tx_fee_estimate_sol)
            account_complete = bool(
                (buy_route == "pump_bc" and av.curve and av.pump_global)
                or (buy_route.startswith("pumpswap") and av.pool_for(buy_route) and av.pumpswap_global)
            ) and bool(
                (sell_route == "pump_bc" and av.curve and av.pump_global)
                or (sell_route.startswith("pumpswap") and av.pool_for(sell_route) and av.pumpswap_global)
            )
            blocker = ""
            if sol_out_lp <= 0:
                blocker = "sell_quote_zero"
            elif tokens_out_raw <= 0:
                blocker = "buy_quote_zero"
            pair = RoutePairQuote(
                mint=mint_str,
                buy_route=buy_route,
                sell_route=sell_route,
                buy_amount_sol=buy_amount_sol,
                buy_quote_tokens_raw=tokens_out_raw,
                buy_quote_tokens_ui=buy_tokens_ui,
                buy_quote_latency_ms=buy_ms,
                sell_quote_out_lamports=sol_out_lp,
                sell_quote_out_sol=sell_sol,
                sell_quote_latency_ms=sell_ms,
                fees_bps_buy=fee_bps_buy,
                fees_bps_sell=fee_bps_sell,
                tx_fee_estimate_sol=tx_fee_estimate_sol,
                estimated_all_in_pnl_sol=all_in_pnl,
                sim_needed=True,
                account_completeness=account_complete,
                blocker=blocker,
            )
            pairs.append(pair)
            log(f"PGG2-V40-ROUTE-MATRIX {pair.to_log_fields()}")
    return av, pairs


__all__ = [
    "RouteAvailability",
    "RoutePairQuote",
    "DEFAULT_TX_FEE_SOL",
    "build_route_matrix",
    "probe_route_availability",
]
