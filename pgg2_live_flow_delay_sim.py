"""v38 live-equivalent flow-delay scalp simulator.

This module models the only live-equivalent version of the dry-live ESB/scalp
edge: buy first, then let external tape move the curve, then sell after a
modeled processed/confirmed delay. It does not model atomic instant buy->sell
as a strategy and it does not assume Jito ordering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from pgg2_pump_selfimpact_sim import (
    LAMPORTS_PER_SOL,
    PumpBCSelfImpactSimulator,
    SelfImpactCostModel,
    _ceil_div,
)


@dataclass(frozen=True)
class TapeEvent:
    ts_ms: int
    is_buy: bool
    sol_lamports: int
    token_amount: int = 0
    user: str = ""


@dataclass(frozen=True)
class LatencyModel:
    buy_submit_ms: int = 80
    buy_processed_ms: int = 220
    buy_confirmed_ms: int = 750
    sell_submit_ms: int = 80

    def buy_ready_ts(self, t0_ms: int, mode: str) -> int:
        if mode == "processed_mode":
            return t0_ms + self.buy_processed_ms
        if mode == "confirmed_mode":
            return t0_ms + self.buy_confirmed_ms
        if mode == "optimistic_ordered_mode":
            return t0_ms + self.buy_submit_ms
        raise ValueError(f"unknown mode: {mode}")


@dataclass(frozen=True)
class ExitPolicy:
    bank_pnl_min_sol: float = 0.00060
    scratch_pnl_min_sol: float = 0.00005
    clamp_pnl_max_loss_sol: float = -0.00050
    max_hold_ms: int = 3000
    flow_stall_ms: int = 750
    require_post_entry_buy: bool = True


@dataclass(frozen=True)
class FlowDelaySimResult:
    mode: str
    entered: bool
    close_reason: str
    all_in_pnl_sol: float
    hold_ms: int
    sell_at_ts_ms: int
    tokens_in: int
    external_buys: int
    external_sells: int
    external_buy_sol: float
    external_sell_sol: float
    pre_entry_buys_750ms: int
    pre_entry_buy_sol_750ms: float
    post_entry_buys_before_exit: int
    post_entry_buy_sol_before_exit: float
    notes: dict[str, Any]


class LiveFlowDelaySimulator:
    """Pure math simulator for sequential live flow-delay exits."""

    def __init__(
        self,
        protocol_fee_bps: int = 100,
        creator_fee_bps: int = 0,
        cost_model: Optional[SelfImpactCostModel] = None,
        latency: Optional[LatencyModel] = None,
        policy: Optional[ExitPolicy] = None,
    ) -> None:
        self.bc = PumpBCSelfImpactSimulator(protocol_fee_bps, creator_fee_bps, cost_model)
        self.latency = latency or LatencyModel()
        self.policy = policy or ExitPolicy()

    def _apply_external_buy(
        self, vsols: int, vtokens: int, real_tokens: int, sol_lamports: int
    ) -> tuple[int, int, int]:
        if sol_lamports <= 0:
            return vsols, vtokens, real_tokens
        protocol_fee = _ceil_div(sol_lamports * self.bc.protocol_fee_bps, 10_000)
        creator_fee = _ceil_div(sol_lamports * self.bc.creator_fee_bps, 10_000)
        net = sol_lamports - protocol_fee - creator_fee
        if net <= 0:
            return vsols, vtokens, real_tokens
        tokens_out = net * vtokens // max(vsols + net, 1)
        tokens_out = max(0, min(int(tokens_out), int(real_tokens)))
        return vsols + net, vtokens - tokens_out, real_tokens - tokens_out

    def _apply_external_sell(
        self, vsols: int, vtokens: int, real_tokens: int, sol_lamports_received: int
    ) -> tuple[int, int, int]:
        if sol_lamports_received <= 0:
            return vsols, vtokens, real_tokens
        denom = max(10_000 - self.bc.protocol_fee_bps - self.bc.creator_fee_bps, 1)
        gross_sol = _ceil_div(sol_lamports_received * 10_000, denom)
        gross_sol = max(0, min(gross_sol, vsols - 1))
        if gross_sol <= 0:
            return vsols, vtokens, real_tokens
        tokens_in = gross_sol * vtokens // max(vsols - gross_sol, 1)
        return vsols - gross_sol, vtokens + tokens_in, real_tokens + tokens_in

    def simulate(
        self,
        mode: str,
        entry_ts_ms: int,
        amount_sol: float,
        virtual_sol_reserves: int,
        virtual_token_reserves: int,
        real_token_reserves: int,
        tape: Iterable[TapeEvent],
    ) -> FlowDelaySimResult:
        events = sorted(tape, key=lambda e: e.ts_ms)
        pre750 = [e for e in events if entry_ts_ms - 750 <= e.ts_ms < entry_ts_ms and e.is_buy]
        pre_entry_buy_sol_750 = sum(e.sol_lamports for e in pre750) / LAMPORTS_PER_SOL

        buy = self.bc.simulate_buy(
            amount_sol, virtual_sol_reserves, virtual_token_reserves, real_token_reserves
        )
        my_tokens = int(buy["tokens_out"])
        if my_tokens <= 0:
            return FlowDelaySimResult(
                mode=mode,
                entered=False,
                close_reason="precheck_no_tokens",
                all_in_pnl_sol=0.0,
                hold_ms=0,
                sell_at_ts_ms=0,
                tokens_in=0,
                external_buys=0,
                external_sells=0,
                external_buy_sol=0.0,
                external_sell_sol=0.0,
                pre_entry_buys_750ms=len(pre750),
                pre_entry_buy_sol_750ms=pre_entry_buy_sol_750,
                post_entry_buys_before_exit=0,
                post_entry_buy_sol_before_exit=0.0,
                notes={},
            )

        cur_vsols = int(buy["post_vsols"])
        cur_vtokens = int(buy["post_vtokens"])
        cur_real = int(buy["post_real_tokens"])
        spend = int(amount_sol * LAMPORTS_PER_SOL)
        overhead = self.bc.cost_model.total_overhead_lamports()
        ready_ts = self.latency.buy_ready_ts(entry_ts_ms, mode)
        max_exit_ts = entry_ts_ms + self.policy.max_hold_ms
        last_external_buy_ts: Optional[int] = None

        external_buys = 0
        external_sells = 0
        external_buy_sol = 0.0
        external_sell_sol = 0.0
        post_buys = 0
        post_buy_sol = 0.0
        close_reason = ""
        sell_at = max_exit_ts

        def pnl_now() -> float:
            sell = self.bc.simulate_sell(my_tokens, cur_vsols, cur_vtokens)
            return (int(sell["sol_out_lamports"]) - spend - overhead) / LAMPORTS_PER_SOL

        def decide(ts: int) -> str:
            if ts < ready_ts:
                return ""
            net = pnl_now()
            if net <= self.policy.clamp_pnl_max_loss_sol:
                return "clamp"
            if self.policy.require_post_entry_buy and post_buys <= 0:
                if ts >= max_exit_ts and net >= self.policy.scratch_pnl_min_sol:
                    return "scratch_no_post_buy"
                return ""
            if net >= self.policy.bank_pnl_min_sol:
                return "bank"
            if last_external_buy_ts is not None and ts - last_external_buy_ts >= self.policy.flow_stall_ms:
                if net >= self.policy.scratch_pnl_min_sol:
                    return "scratch_flow_stall"
                if net <= self.policy.clamp_pnl_max_loss_sol:
                    return "clamp_flow_stall"
            if ts >= max_exit_ts:
                return "max_hold"
            return ""

        for ev in events:
            if ev.ts_ms < entry_ts_ms:
                continue
            if ev.ts_ms > max_exit_ts:
                break
            reason = decide(ev.ts_ms)
            if reason:
                close_reason = reason
                sell_at = ev.ts_ms
                break
            if ev.is_buy:
                cur_vsols, cur_vtokens, cur_real = self._apply_external_buy(
                    cur_vsols, cur_vtokens, cur_real, ev.sol_lamports
                )
                external_buys += 1
                post_buys += 1
                sol = ev.sol_lamports / LAMPORTS_PER_SOL
                external_buy_sol += sol
                post_buy_sol += sol
                last_external_buy_ts = ev.ts_ms
            else:
                cur_vsols, cur_vtokens, cur_real = self._apply_external_sell(
                    cur_vsols, cur_vtokens, cur_real, ev.sol_lamports
                )
                external_sells += 1
                external_sell_sol += ev.sol_lamports / LAMPORTS_PER_SOL

        if not close_reason:
            close_reason = decide(max_exit_ts) or "max_hold"
            sell_at = max(ready_ts, max_exit_ts)

        final_pnl = pnl_now()
        return FlowDelaySimResult(
            mode=mode,
            entered=True,
            close_reason=close_reason,
            all_in_pnl_sol=final_pnl,
            hold_ms=max(0, sell_at - entry_ts_ms),
            sell_at_ts_ms=sell_at,
            tokens_in=my_tokens,
            external_buys=external_buys,
            external_sells=external_sells,
            external_buy_sol=external_buy_sol,
            external_sell_sol=external_sell_sol,
            pre_entry_buys_750ms=len(pre750),
            pre_entry_buy_sol_750ms=pre_entry_buy_sol_750,
            post_entry_buys_before_exit=post_buys,
            post_entry_buy_sol_before_exit=post_buy_sol,
            notes={
                "ready_ts_ms": ready_ts,
                "max_exit_ts_ms": max_exit_ts,
                "overhead_lamports": overhead,
            },
        )
