"""v37 — Pump bonding-curve self-impact simulator.

Computes the live-equivalent all-in PnL for a hypothetical atomic
buy + sell against a pump.fun bonding-curve. Matches the integer math
in pgg2_direct_pump.py exactly:
  - quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
  - quote_pump_sell_sol(token_amount, curve, global_cfg)

The key correction over dry-live's pre-buy reverse quote: the sell is
evaluated against the POST-BUY curve state, not the pre-buy state. This
is the self-impact dry-live ESB was hiding.

Cost components included by default:
  - buy protocol + creator fee (already in Pump's quote math)
  - sell protocol + creator fee (already in Pump's quote math)
  - base tx fee (5_000 lamports, configurable)
  - priority fee from compute-budget price * limit
  - ATA-create rent (un-recoverable when the atomic tx omits the close
    instruction; see ESB_LIVE_EQUIVALENCE_TRUTH_TEST.md)

No I/O. Pure-math simulator. Safe to call thousands of times per second.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

LAMPORTS_PER_SOL = 1_000_000_000


def _ceil_div(a: int, b: int) -> int:
    if b <= 0:
        return 0
    return (a + b - 1) // b


@dataclass
class SelfImpactCostModel:
    """Per-call cost configuration. Defaults match the v36c-3 atomic ESB
    cost surface."""

    base_tx_fee_lamports: int = 5_000
    compute_unit_limit: int = 600_000
    compute_unit_price_micro_lamports: int = 22_700
    ata_rent_lamports: int = 2_039_280  # 0.002039280 SOL — standard SPL ATA rent
    ata_recoverable: bool = False  # True only if the live path closes the ATA atomically

    def priority_fee_lamports(self) -> int:
        # Solana priority-fee formula: micro_lamports * units / 1_000_000
        return int(self.compute_unit_price_micro_lamports * self.compute_unit_limit // 1_000_000)

    def total_overhead_lamports(self) -> int:
        ata_cost = 0 if self.ata_recoverable else int(self.ata_rent_lamports)
        return int(self.base_tx_fee_lamports) + self.priority_fee_lamports() + ata_cost


class PumpBCSelfImpactSimulator:
    """Pure-math simulator. Construct once per bot, call simulate_roundtrip
    per candidate.
    """

    def __init__(
        self,
        protocol_fee_bps: int,
        creator_fee_bps: int,
        cost_model: Optional[SelfImpactCostModel] = None,
    ) -> None:
        self.protocol_fee_bps = int(protocol_fee_bps)
        self.creator_fee_bps = int(creator_fee_bps)
        self.cost_model = cost_model or SelfImpactCostModel()

    # ------------------------------------------------------------------
    # Buy
    # ------------------------------------------------------------------
    def simulate_buy(
        self,
        amount_sol: float,
        virtual_sol_reserves: int,
        virtual_token_reserves: int,
        real_token_reserves: int,
    ) -> dict[str, Any]:
        """Mirror of `DirectPumpQuoteBroker.quote_pump_buy_tokens`.

        Returns post-buy state + tokens_out + fee in lamports.
        """
        spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
        protocol_fee = _ceil_div(spend_lamports * self.protocol_fee_bps, 10_000)
        creator_fee = _ceil_div(spend_lamports * self.creator_fee_bps, 10_000)
        buy_fee_lamports = protocol_fee + creator_fee
        net_for_curve = spend_lamports - buy_fee_lamports
        if net_for_curve <= 0:
            return {
                "tokens_out": 0,
                "buy_fee_lamports": buy_fee_lamports,
                "post_vsols": virtual_sol_reserves,
                "post_vtokens": virtual_token_reserves,
                "post_real_tokens": real_token_reserves,
                "spend_lamports": spend_lamports,
                "net_for_curve_lamports": 0,
            }
        tokens_out = (
            net_for_curve
            * virtual_token_reserves
            // max(virtual_sol_reserves + net_for_curve, 1)
        )
        tokens_out = max(0, min(int(tokens_out), int(real_token_reserves)))
        post_vsols = int(virtual_sol_reserves) + net_for_curve
        post_vtokens = int(virtual_token_reserves) - tokens_out
        post_real_tokens = int(real_token_reserves) - tokens_out
        return {
            "tokens_out": int(tokens_out),
            "buy_fee_lamports": int(buy_fee_lamports),
            "post_vsols": int(post_vsols),
            "post_vtokens": int(post_vtokens),
            "post_real_tokens": int(post_real_tokens),
            "spend_lamports": int(spend_lamports),
            "net_for_curve_lamports": int(net_for_curve),
        }

    # ------------------------------------------------------------------
    # Sell against an arbitrary curve state (post-buy or any-state)
    # ------------------------------------------------------------------
    def simulate_sell(
        self,
        tokens_in: int,
        virtual_sol_reserves: int,
        virtual_token_reserves: int,
    ) -> dict[str, Any]:
        """Mirror of `DirectPumpQuoteBroker.quote_pump_sell_sol`."""
        if int(tokens_in) <= 0:
            return {
                "sol_out_lamports": 0,
                "gross_sol_lamports": 0,
                "sell_fee_lamports": 0,
            }
        gross_sol = (
            int(tokens_in)
            * int(virtual_sol_reserves)
            // max(int(virtual_token_reserves) + int(tokens_in), 1)
        )
        protocol_fee = _ceil_div(gross_sol * self.protocol_fee_bps, 10_000)
        creator_fee = _ceil_div(gross_sol * self.creator_fee_bps, 10_000)
        total_fees = protocol_fee + creator_fee
        net_sol = max(0, gross_sol - total_fees)
        return {
            "sol_out_lamports": int(net_sol),
            "gross_sol_lamports": int(gross_sol),
            "sell_fee_lamports": int(total_fees),
        }

    # ------------------------------------------------------------------
    # Roundtrip — the live-equivalent all_in
    # ------------------------------------------------------------------
    def simulate_roundtrip(
        self,
        amount_sol: float,
        virtual_sol_reserves: int,
        virtual_token_reserves: int,
        real_token_reserves: int,
    ) -> dict[str, Any]:
        """Atomic buy then sell against the post-buy state. Returns the
        live-equivalent all-in PnL in SOL (net wallet delta)."""
        buy = self.simulate_buy(
            amount_sol,
            virtual_sol_reserves,
            virtual_token_reserves,
            real_token_reserves,
        )
        # Sell EXACTLY the buy's output tokens.
        sell = self.simulate_sell(
            buy["tokens_out"], buy["post_vsols"], buy["post_vtokens"]
        )
        overhead_lamports = self.cost_model.total_overhead_lamports()
        cost_lamports = int(amount_sol * LAMPORTS_PER_SOL)
        # Net wallet delta in lamports = sell proceeds − buy cost − overhead.
        # buy fee is already inside the buy's `net_for_curve` accounting
        # (Pump takes its cut from the input SOL); we DO NOT subtract it
        # again. sell fee is already inside sell's `sol_out_lamports`.
        net_lamports = (
            int(sell["sol_out_lamports"]) - cost_lamports - int(overhead_lamports)
        )
        # For diagnostic comparability with the old dry-live model, also
        # compute what dry-live's quote_all_in_pnl WOULD have said: same
        # pre-buy state sell against `tokens_out` units, no overhead-tax.
        prebuy_same_state_sell = self.simulate_sell(
            buy["tokens_out"], virtual_sol_reserves, virtual_token_reserves
        )
        prebuy_lamports = (
            int(prebuy_same_state_sell["sol_out_lamports"]) - cost_lamports
        )
        # Self-impact cost: the difference between same-state sell and
        # post-buy sell, before overhead.
        self_impact_lamports = (
            int(prebuy_same_state_sell["sol_out_lamports"]) - int(sell["sol_out_lamports"])
        )
        return {
            "amount_sol": float(amount_sol),
            "tokens_out": int(buy["tokens_out"]),
            "buy_fee_lamports": int(buy["buy_fee_lamports"]),
            "sell_out_lamports": int(sell["sol_out_lamports"]),
            "sell_fee_lamports": int(sell["sell_fee_lamports"]),
            "overhead_lamports": int(overhead_lamports),
            "self_impact_lamports": int(self_impact_lamports),
            "live_equiv_all_in_pnl_sol": net_lamports / LAMPORTS_PER_SOL,
            "prebuy_same_state_all_in_pnl_sol": prebuy_lamports / LAMPORTS_PER_SOL,
            "self_impact_cost_sol": self_impact_lamports / LAMPORTS_PER_SOL,
            "overhead_cost_sol": int(overhead_lamports) / LAMPORTS_PER_SOL,
            "post_vsols": int(buy["post_vsols"]),
            "post_vtokens": int(buy["post_vtokens"]),
            "post_real_tokens": int(buy["post_real_tokens"]),
        }

    # ------------------------------------------------------------------
    # Convenience: build from a Pump bonding curve + global config
    # objects (as exposed by DirectPumpQuoteBroker).
    # ------------------------------------------------------------------
    @classmethod
    def from_broker(cls, broker: Any, cost_model: Optional[SelfImpactCostModel] = None) -> "PumpBCSelfImpactSimulator":
        global_cfg = broker.pump_global()
        return cls(
            protocol_fee_bps=int(global_cfg.fee_bps),
            creator_fee_bps=int(global_cfg.creator_fee_bps),
            cost_model=cost_model,
        )

    def simulate_roundtrip_from_curve(
        self,
        amount_sol: float,
        curve: Any,
    ) -> dict[str, Any]:
        """Convenience wrapper that pulls reserves from a Pump curve."""
        return self.simulate_roundtrip(
            amount_sol=amount_sol,
            virtual_sol_reserves=int(curve.virtual_sol_reserves),
            virtual_token_reserves=int(curve.virtual_token_reserves),
            real_token_reserves=int(curve.real_token_reserves),
        )
