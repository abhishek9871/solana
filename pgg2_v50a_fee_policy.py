"""V50A edge-capped fee policy.

Given expected per-trade SOL edge and per-tx fee estimates, decides whether
a candidate clears the minimum edge-after-fees floor. Also exposes a small
FeeBudget book-keeper for the V50A Stage A runner.

Forbidden-call clean: no sendTransaction / send_signed paths.
"""
from __future__ import annotations

import os
import re as _re_self
import sys
import time
from typing import Any, Dict, Tuple

# Static-grep self check — forbidden send patterns must NOT appear here.
_FORBIDDEN = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _src):
        sys.stderr.write(f"V50A-FEE-POLICY-ABORT forbidden_call_pattern={_pat}\n")
        sys.exit(2)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def estimate_total_fee_sol(
    priority_fee_microlamports: int,
    compute_unit_limit: int,
    swqos_tip_sol: float,
    base_signature_fee_sol: float = 0.000005,
) -> float:
    """Return the total per-tx SOL fee for ONE buy or sell.

    Fee components:
      - base signature fee  (default 5000 lamports = 0.000005 SOL)
      - priority fee        = (priority_fee_microlamports * cu_limit) / 1e15
                              i.e. micro-lamports per CU * CU = lamports * 1e-6
                              divided by lamports_per_sol (1e9) -> /1e15
      - SWQOS tip transfer  (paid even on failed sends per Helius docs)
    """
    priority_lamports = float(priority_fee_microlamports) * float(compute_unit_limit) / 1_000_000.0
    priority_sol = priority_lamports / 1_000_000_000.0
    total = float(base_signature_fee_sol) + priority_sol + float(swqos_tip_sol)
    return float(total)


def evaluate_fee_policy(
    expected_edge_sol: float,
    buy_total_fee_sol: float,
    sell_total_fee_sol: float,
    min_edge_after_fees_sol: float = 0.00030,
) -> Tuple[bool, Dict[str, Any]]:
    """Decide whether the expected edge survives the round-trip fees.

    expected_edge_sol = the strategy's pre-fee expected SOL profit per
    trade (this is `exp_pnl` from the V47I/V48 decision record OR an
    explicit edge supplied by the caller).
    """
    total_fees = float(buy_total_fee_sol) + float(sell_total_fee_sol)
    edge_after = float(expected_edge_sol) - total_fees
    allow = edge_after >= float(min_edge_after_fees_sol)
    return allow, {
        "expected_edge_sol": float(expected_edge_sol),
        "buy_fee_sol": float(buy_total_fee_sol),
        "sell_fee_sol": float(sell_total_fee_sol),
        "total_fees_sol": float(total_fees),
        "edge_after_fees_sol": float(edge_after),
        "min_required_sol": float(min_edge_after_fees_sol),
        "allow": bool(allow),
        "reason": "edge_below_floor" if not allow else "ok",
    }


class FeeBudget:
    """Lightweight running budget tracker for the V50A Stage A run.

    Tracks total SOL spent on tx fees (priority + tip + base) and the
    number of failed sends.  `should_stop` is the only way the runner
    asks "have we hit a stop condition?".
    """

    def __init__(self, budget_sol: float = 0.00025, max_failed_sends: int = 2) -> None:
        self.budget_sol = float(budget_sol)
        self.max_failed_sends = int(max_failed_sends)
        self.spent_sol = 0.0
        self.failed_sends = 0
        self.successful_sends = 0
        self.created_ts = time.time()

    def can_afford(self, projected_fee_sol: float) -> bool:
        return (self.spent_sol + float(projected_fee_sol)) <= self.budget_sol

    def consume(self, fee_sol: float) -> None:
        self.spent_sol = float(self.spent_sol) + max(0.0, float(fee_sol))

    def record_failed_send(self, fee_sol: float = 0.0) -> None:
        self.failed_sends += 1
        if fee_sol > 0:
            self.consume(fee_sol)

    def record_successful_send(self, fee_sol: float = 0.0) -> None:
        self.successful_sends += 1
        if fee_sol > 0:
            self.consume(fee_sol)

    def should_stop(self) -> Tuple[bool, str]:
        if self.spent_sol >= self.budget_sol:
            return True, f"fee_budget_consumed spent={self.spent_sol:.9f} budget={self.budget_sol:.9f}"
        if self.failed_sends >= self.max_failed_sends:
            return True, f"max_failed_sends_reached failed={self.failed_sends} max={self.max_failed_sends}"
        return False, ""

    def remaining_sol(self) -> float:
        return max(0.0, self.budget_sol - self.spent_sol)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "budget_sol": self.budget_sol,
            "max_failed_sends": self.max_failed_sends,
            "spent_sol": round(self.spent_sol, 12),
            "remaining_sol": round(self.remaining_sol(), 12),
            "failed_sends": self.failed_sends,
            "successful_sends": self.successful_sends,
            "uptime_s": round(time.time() - self.created_ts, 3),
        }


def log_fee_policy_decision(
    *,
    mint_short: str,
    expected_edge_sol: float,
    buy_fee_sol: float,
    sell_fee_sol: float,
    edge_after_sol: float,
    allow: bool,
    reason: str,
) -> None:
    _log(
        f"PGG2-V50A-FEE-POLICY mint={mint_short} "
        f"exp_edge={expected_edge_sol:+.9f} "
        f"buy_fee={buy_fee_sol:.9f} sell_fee={sell_fee_sol:.9f} "
        f"edge_after={edge_after_sol:+.9f} "
        f"allow={str(bool(allow)).lower()} reason={reason}"
    )
