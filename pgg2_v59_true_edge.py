"""V59 true-edge calculator.

Extends V58 net_wallet_edge with empirical slippage budget:

    true_edge = ep
              - fees (buy_base + sell_base)
              - tips (buy + sell SWQOS)
              - priority (buy + sell)
              - rent_penalty   (0 if CloseAccount succeeds, ATA rent otherwise)
              - slippage_budget (from V59 slippage model, size-tiered)
              - safety_buffer   (default 0.000100 SOL)

V59 promotion thresholds:
  Tier B (micro): true_edge >= +0.00005 SOL
  Tier A (bank):  true_edge >= +0.000400 SOL

Hard rule: no live buy unless true_edge passes the requested tier.

Log:
  PGG2-V59-TRUE-EDGE mint=.. size=.. ep=.. fees=.. tips=.. priority=..
      rent_penalty=.. slippage_budget=.. safety_buffer=.. true_edge=..
      pass_micro=.. pass_bank=.. blocker=..
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from pgg2_v59_slippage_model import get_model as _get_slip_model


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
class TrueEdgeInputs:
    mint: str
    size_sol: float
    expected_pnl_sol: float
    # Fees (per leg; total = 2× single-leg values)
    base_fee_per_leg_sol: float
    priority_per_leg_sol: float
    tip_per_leg_sol: float
    # ATA rent recovery
    ata_rent_sol: float
    close_account_will_succeed: bool


@dataclass
class TrueEdgeResult:
    mint: str
    size_sol: float
    ep_sol: float
    total_fees_sol: float
    total_priority_sol: float
    total_tips_sol: float
    rent_penalty_sol: float
    slippage_budget_sol: float
    slippage_tier: str
    safety_buffer_sol: float
    true_edge_sol: float
    pass_micro: bool
    pass_bank: bool
    blocker: Optional[str]


class V59TrueEdge:
    def __init__(self) -> None:
        # Defaults pulled from same envs as V58 calculator
        self.base_fee = _envf("PGG2_DRY_LIVE_BASE_TX_FEE_SOL", 0.000005)
        self.priority_per_leg = _envf("PGG2_DRY_LIVE_PRIORITY_FEE_SOL", 0.000005)
        self.tip_per_leg = _envf("PGG2_V58_SWQOS_TIP_SOL", 0.000005)
        self.ata_rent = _envf("PGG2_DRY_LIVE_ATA_RENT_SOL", 0.002039280)
        self.close_default = _envb("PGG2_DRY_LIVE_RECOVER_ATA_RENT", True)
        # V59 spec safety buffer + tier thresholds
        self.safety_buffer = _envf("PGG2_V59_SAFETY_BUFFER_SOL", 0.000100)
        self.micro_threshold = _envf("PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL", 0.00005)
        self.bank_threshold = _envf("PGG2_V59_BANK_TRUE_EDGE_MIN_SOL", 0.000400)
        self.slip = _get_slip_model()

    def compute(self, inputs: TrueEdgeInputs) -> TrueEdgeResult:
        fees = inputs.base_fee_per_leg_sol * 2
        priority = inputs.priority_per_leg_sol * 2
        tips = inputs.tip_per_leg_sol * 2
        rent_penalty = 0.0 if inputs.close_account_will_succeed else inputs.ata_rent_sol
        slip_budget = self.slip.get_budget(inputs.size_sol)
        slip_sol = slip_budget.budget_sol
        true_edge = (
            inputs.expected_pnl_sol
            - fees - priority - tips
            - rent_penalty
            - slip_sol
            - self.safety_buffer
        )
        # Decide blocker (used by V59 router for tier choice)
        blocker: Optional[str] = None
        pass_micro = true_edge >= self.micro_threshold
        pass_bank = true_edge >= self.bank_threshold
        if not pass_micro:
            blocker = f"true_edge_below_micro({true_edge:+.6f}<{self.micro_threshold:+.6f})"
        return TrueEdgeResult(
            mint=inputs.mint, size_sol=inputs.size_sol,
            ep_sol=inputs.expected_pnl_sol,
            total_fees_sol=fees, total_priority_sol=priority,
            total_tips_sol=tips, rent_penalty_sol=rent_penalty,
            slippage_budget_sol=slip_sol, slippage_tier=slip_budget.tier,
            safety_buffer_sol=self.safety_buffer,
            true_edge_sol=true_edge,
            pass_micro=pass_micro, pass_bank=pass_bank,
            blocker=blocker,
        )

    def compute_default(
        self,
        mint: str,
        expected_pnl_sol: float,
        size_sol: float = 0.005,
        assume_close_succeeds: Optional[bool] = None,
    ) -> TrueEdgeResult:
        inputs = TrueEdgeInputs(
            mint=mint, size_sol=size_sol,
            expected_pnl_sol=expected_pnl_sol,
            base_fee_per_leg_sol=self.base_fee,
            priority_per_leg_sol=self.priority_per_leg,
            tip_per_leg_sol=self.tip_per_leg,
            ata_rent_sol=self.ata_rent,
            close_account_will_succeed=(
                self.close_default if assume_close_succeeds is None
                else assume_close_succeeds
            ),
        )
        return self.compute(inputs)

    def format_log_line(self, r: TrueEdgeResult) -> str:
        short = r.mint[:4] + ".." + r.mint[-4:] if len(r.mint) > 10 else r.mint
        return (
            f"PGG2-V59-TRUE-EDGE {short} size={r.size_sol:.4f} "
            f"ep={r.ep_sol:+.6f} fees={r.total_fees_sol:.6f} "
            f"priority={r.total_priority_sol:.6f} tips={r.total_tips_sol:.6f} "
            f"rent_penalty={r.rent_penalty_sol:.6f} "
            f"slippage={r.slippage_budget_sol:.6f}({r.slippage_tier}) "
            f"safety={r.safety_buffer_sol:.6f} "
            f"true_edge={r.true_edge_sol:+.6f} "
            f"pass_micro={int(r.pass_micro)} pass_bank={int(r.pass_bank)} "
            f"blocker={r.blocker or '-'}"
        )


_SINGLETON: Optional[V59TrueEdge] = None


def get_calculator() -> V59TrueEdge:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V59TrueEdge()
    return _SINGLETON


def compute_and_log(
    mint: str, expected_pnl_sol: float, size_sol: float = 0.005,
    assume_close_succeeds: Optional[bool] = None,
    log_fn: Callable[[str], None] = print,
) -> TrueEdgeResult:
    calc = get_calculator()
    r = calc.compute_default(mint, expected_pnl_sol, size_sol, assume_close_succeeds)
    log_fn(calc.format_log_line(r))
    return r


if __name__ == "__main__":
    calc = get_calculator()
    print(f"=== V59 True-Edge Calculator ===")
    print(f"micro_threshold = {calc.micro_threshold:+.6f}")
    print(f"bank_threshold  = {calc.bank_threshold:+.6f}")
    print(f"safety_buffer   = {calc.safety_buffer:+.6f}")
    print()
    # Replay the 3 V58 Stage A losses
    print("--- V58 losses replayed through V59 (would they have been blocked?) ---")
    for mint, sz, ep in [
        ("CVz4..pump", 0.015, 0.000275),
        ("7jNJ..pump", 0.005, 0.000533),
        ("EsVt..pump", 0.005, 0.000789),
    ]:
        r = compute_and_log(mint, ep, sz, True)
        print(f"  -> pass_micro={r.pass_micro} pass_bank={r.pass_bank}")
    print()
    # New thresholds: what ep is required for pass_micro at each size
    print("--- Minimum ep to pass V59 micro mode (true_edge >= +0.00005) ---")
    for sz in (0.005, 0.010, 0.015):
        # Find min ep
        for ep_test in [0.0005, 0.0008, 0.001, 0.0015, 0.002]:
            r = calc.compute_default("TEST" + str(sz) + "x" * 36, ep_test, sz, True)
            if r.pass_micro:
                print(f"  size={sz:.3f}: ep >= {ep_test:+.4f} passes (true_edge={r.true_edge_sol:+.6f})")
                break
