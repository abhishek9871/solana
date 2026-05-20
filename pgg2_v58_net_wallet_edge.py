"""V58 net wallet edge calculator.

Computes the TRUE wallet-delta-after-everything for a candidate trade:

    net_wallet_edge = expected_pnl
                    - buy_base_fee   - sell_base_fee
                    - buy_priority   - sell_priority
                    - buy_tip        - sell_tip
                    - unclosed_rent_penalty

Where `unclosed_rent_penalty` is ATA rent if the post-sell CloseAccount fails,
0 if the close succeeds (rent reclaimed).

Hard rule: no V58 micro-win entry unless net_wallet_edge > 0.

Log:
  PGG2-V58-NET-WALLET-EDGE mint=.. expected_pnl=.. fees=.. tips=.. priority=..
      rent_recovered=.. net_wallet_edge=.. pass=..

Solana token-account rent recovery requires:
  1. token balance is zero
  2. CloseAccount instruction sent for the correct token program (Tokenkeg or Token2022)
  3. confirmed on-chain
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional


# Solana constants (rent-exempt minimum for token account = 165 bytes for SPL,
# 170+ for Token-2022 with extensions; we use a single conservative figure
# that matches v50b's DRY_LIVE_ATA_RENT_SOL default).
ATA_RENT_SOL_DEFAULT = 0.002039280  # matches PGG2_DRY_LIVE_ATA_RENT_SOL


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class EdgeInputs:
    """Inputs needed to compute the net wallet edge for a candidate trade."""
    mint: str
    expected_pnl_sol: float
    size_sol: float
    # Fees + tips (all per-leg in SOL)
    buy_base_fee_sol: float
    sell_base_fee_sol: float
    buy_priority_sol: float
    sell_priority_sol: float
    buy_tip_sol: float
    sell_tip_sol: float
    # ATA rent recovery — if assume_close_account_will_succeed, rent_penalty=0
    ata_rent_sol: float = ATA_RENT_SOL_DEFAULT
    assume_close_account_will_succeed: bool = True


@dataclass
class EdgeResult:
    mint: str
    expected_pnl_sol: float
    total_fees_sol: float
    total_tips_sol: float
    total_priority_sol: float
    rent_penalty_sol: float
    net_wallet_edge_sol: float
    pass_micro: bool         # net edge >= micro threshold
    pass_bank: bool          # net edge >= bank threshold

    @property
    def gross_cost_sol(self) -> float:
        return self.total_fees_sol + self.total_tips_sol + self.total_priority_sol + self.rent_penalty_sol


class V58NetWalletEdgeCalculator:
    """Singleton calculator for V58 net wallet edge."""

    def __init__(self) -> None:
        # Pull defaults from env so the same source-of-truth as v50b is used.
        self.default_buy_base_fee = _envf("PGG2_DRY_LIVE_BASE_TX_FEE_SOL", 0.000005)
        self.default_sell_base_fee = _envf("PGG2_DRY_LIVE_BASE_TX_FEE_SOL", 0.000005)
        self.default_buy_priority = _envf("PGG2_DRY_LIVE_PRIORITY_FEE_SOL", 0.000005)
        self.default_sell_priority = _envf("PGG2_DRY_LIVE_PRIORITY_FEE_SOL", 0.000005)
        # SWQOS tip is fixed per spec at 0.000005 SOL per leg
        self.default_buy_tip = _envf("PGG2_V58_SWQOS_TIP_SOL", 0.000005)
        self.default_sell_tip = _envf("PGG2_V58_SWQOS_TIP_SOL", 0.000005)
        self.default_ata_rent = _envf("PGG2_DRY_LIVE_ATA_RENT_SOL", ATA_RENT_SOL_DEFAULT)
        self.default_recover_ata_rent = _envb("PGG2_DRY_LIVE_RECOVER_ATA_RENT", True)

        # Thresholds per V58 spec
        self.micro_threshold = _envf("PGG2_V58_MICRO_NET_EDGE_MIN_SOL", 0.00005)
        self.bank_threshold = _envf("PGG2_V58_BANK_NET_EDGE_MIN_SOL", 0.00040)

    def compute(self, inputs: EdgeInputs) -> EdgeResult:
        fees = inputs.buy_base_fee_sol + inputs.sell_base_fee_sol
        priority = inputs.buy_priority_sol + inputs.sell_priority_sol
        tips = inputs.buy_tip_sol + inputs.sell_tip_sol
        rent_penalty = 0.0 if inputs.assume_close_account_will_succeed else inputs.ata_rent_sol
        net = inputs.expected_pnl_sol - fees - priority - tips - rent_penalty
        return EdgeResult(
            mint=inputs.mint,
            expected_pnl_sol=inputs.expected_pnl_sol,
            total_fees_sol=fees,
            total_tips_sol=tips,
            total_priority_sol=priority,
            rent_penalty_sol=rent_penalty,
            net_wallet_edge_sol=net,
            pass_micro=net >= self.micro_threshold,
            pass_bank=net >= self.bank_threshold,
        )

    def compute_default(
        self,
        mint: str,
        expected_pnl_sol: float,
        size_sol: float = 0.005,
        assume_close_succeeds: bool = True,
    ) -> EdgeResult:
        inputs = EdgeInputs(
            mint=mint,
            expected_pnl_sol=expected_pnl_sol,
            size_sol=size_sol,
            buy_base_fee_sol=self.default_buy_base_fee,
            sell_base_fee_sol=self.default_sell_base_fee,
            buy_priority_sol=self.default_buy_priority,
            sell_priority_sol=self.default_sell_priority,
            buy_tip_sol=self.default_buy_tip,
            sell_tip_sol=self.default_sell_tip,
            ata_rent_sol=self.default_ata_rent,
            assume_close_account_will_succeed=(
                self.default_recover_ata_rent and assume_close_succeeds
            ),
        )
        return self.compute(inputs)

    def format_log_line(self, r: EdgeResult) -> str:
        short = r.mint[:4] + ".." + r.mint[-4:] if len(r.mint) > 10 else r.mint
        return (
            f"PGG2-V58-NET-WALLET-EDGE {short} "
            f"expected_pnl={r.expected_pnl_sol:+.6f} "
            f"fees={r.total_fees_sol:.6f} "
            f"priority={r.total_priority_sol:.6f} "
            f"tips={r.total_tips_sol:.6f} "
            f"rent_recovered={int(r.rent_penalty_sol == 0)} "
            f"rent_penalty={r.rent_penalty_sol:.6f} "
            f"net_wallet_edge={r.net_wallet_edge_sol:+.6f} "
            f"pass_micro={int(r.pass_micro)} "
            f"pass_bank={int(r.pass_bank)}"
        )


_SINGLETON: Optional[V58NetWalletEdgeCalculator] = None


def get_calculator() -> V58NetWalletEdgeCalculator:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V58NetWalletEdgeCalculator()
    return _SINGLETON


def compute_and_log(
    mint: str,
    expected_pnl_sol: float,
    size_sol: float = 0.005,
    assume_close_succeeds: bool = True,
    log_fn: Callable[[str], None] = print,
) -> EdgeResult:
    calc = get_calculator()
    r = calc.compute_default(mint, expected_pnl_sol, size_sol, assume_close_succeeds)
    log_fn(calc.format_log_line(r))
    return r


if __name__ == "__main__":
    # Sanity scenarios for the V58 audit thresholds
    calc = get_calculator()
    print(f"=== V58 Net Wallet Edge Calculator ===")
    print(f"micro_threshold = {calc.micro_threshold:+.6f} SOL")
    print(f"bank_threshold  = {calc.bank_threshold:+.6f} SOL")
    print(f"ata_rent_default = {calc.default_ata_rent:.6f} SOL")
    print()

    # Scenario 1: ep=+0.00025 with rent recovered (V58 Tier B sweet spot)
    print("--- Scenario 1: ep=+0.00025, ATA closed (rent recovered) ---")
    r1 = compute_and_log("TESTpump" + "1" * 36, 0.00025, assume_close_succeeds=True)
    print(f"  pass_micro={r1.pass_micro}  pass_bank={r1.pass_bank}")
    print()

    # Scenario 2: same ep but ATA close FAILS (rent locked)
    print("--- Scenario 2: ep=+0.00025, ATA NOT closed (rent locked) ---")
    r2 = compute_and_log("TESTpump" + "2" * 36, 0.00025, assume_close_succeeds=False)
    print(f"  pass_micro={r2.pass_micro}  pass_bank={r2.pass_bank}")
    print(f"  >>> RENT LOSS is {abs(r2.net_wallet_edge_sol):.6f} SOL = ${abs(r2.net_wallet_edge_sol)*180:.2f}")
    print()

    # Scenario 3: ep=+0.0010 V55 floor, rent recovered (Tier A)
    print("--- Scenario 3: ep=+0.0010 (V55 floor), ATA closed ---")
    r3 = compute_and_log("TESTpump" + "3" * 36, 0.0010, assume_close_succeeds=True)
    print(f"  pass_micro={r3.pass_micro}  pass_bank={r3.pass_bank}")
    print()

    # Scenario 4: ep=0 break-even, rent recovered
    print("--- Scenario 4: ep=0.0 (break-even), ATA closed ---")
    r4 = compute_and_log("TESTpump" + "4" * 36, 0.0, assume_close_succeeds=True)
    print(f"  pass_micro={r4.pass_micro}  pass_bank={r4.pass_bank}")
    print()

    # Scenario 5: ep that just clears micro threshold with close
    # Need net_wallet_edge >= 0.00005, total non-rent fee = ~0.00003
    # So expected_pnl needs to be >= 0.00008 with close
    print("--- Scenario 5: ep=+0.00010 (the actual minimum for Tier B) ---")
    r5 = compute_and_log("TESTpump" + "5" * 36, 0.00010, assume_close_succeeds=True)
    print(f"  pass_micro={r5.pass_micro}  pass_bank={r5.pass_bank}")
