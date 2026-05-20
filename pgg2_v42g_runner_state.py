"""V42G Phase 2 — RunnerState per mint.

Computes a runner_confidence_score from observed virtual-ticket outcomes plus
current curve / quote gradients. NO live tx. NO network.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pgg2_v42g_virtual_ticket_engine import (
    VirtualTicket,
    VirtualTicketEngine,
    LAMPORTS_PER_SOL,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class RunnerStateSnapshot:
    """Snapshot of a mint's runner state at a moment in time."""
    mint: str
    computed_at_ms: int
    virtual_wins_last_2s: int
    virtual_wins_last_5s: int
    virtual_losses_last_5s: int
    consecutive_virtual_wins: int
    average_bank_time_ms: Optional[float]
    average_virtual_win_pnl: Optional[float]
    max_virtual_adverse_before_bank: float
    current_quote_gradient: float
    current_accountSubscribe_curve_gradient: float
    last_negative_curve_update_age_ms: int
    last_virtual_loss_age_ms: int
    last_virtual_bank_ts_ms: Optional[int]
    last_virtual_bank_pnl_sol: Optional[float]
    runner_confidence_score: float
    waves_count: int
    extra: Dict[str, Any] = field(default_factory=dict)


def _gradient(seq: List[float]) -> float:
    if len(seq) < 2:
        return 0.0
    return float(seq[-1] - seq[-2])


def compute_runner_state(
    engine: VirtualTicketEngine,
    mint: str,
    now_ms: Optional[int] = None,
) -> Optional[RunnerStateSnapshot]:
    st = engine.mint_state(mint)
    if st is None:
        return None
    now = now_ms if now_ms is not None else _now_ms()

    tickets: List[VirtualTicket] = list(st.tickets)
    closed_banks = [t for t in tickets if t.outcome == "virtual_bank_win"]
    closed_losses = [t for t in tickets if t.outcome == "virtual_loss"]

    last_2s_window = 2500
    last_5s_window = 5000
    wins_2s = [
        t for t in closed_banks
        if t.outcome_ts is not None and (now - t.outcome_ts) <= last_2s_window
    ]
    wins_5s = [
        t for t in closed_banks
        if t.outcome_ts is not None and (now - t.outcome_ts) <= last_5s_window
    ]
    losses_5s = [
        t for t in closed_losses
        if t.outcome_ts is not None and (now - t.outcome_ts) <= last_5s_window
    ]

    # consecutive virtual wins counted backwards through the chronological
    # ticket sequence.
    seq_by_outcome_ts = sorted(
        [t for t in tickets if t.outcome_ts is not None],
        key=lambda t: t.outcome_ts or 0,
    )
    cons_wins = 0
    for t in reversed(seq_by_outcome_ts):
        if t.outcome == "virtual_bank_win":
            cons_wins += 1
        else:
            break

    last5_banks = closed_banks[-5:]
    if last5_banks:
        bank_times = [t.bank_time_ms for t in last5_banks if t.bank_time_ms is not None]
        avg_bank_time_ms = (sum(bank_times) / len(bank_times)) if bank_times else None
        bank_pnls = [t.bank_pnl_sol for t in last5_banks if t.bank_pnl_sol is not None]
        avg_win_pnl = (sum(bank_pnls) / len(bank_pnls)) if bank_pnls else None
    else:
        avg_bank_time_ms = None
        avg_win_pnl = None

    max_adv = 0.0
    for t in closed_banks:
        if t.max_adverse_before_bank < max_adv:
            max_adv = t.max_adverse_before_bank

    quote_gradient = _gradient(list(st.last_quote_out_sol_sequence))
    curve_gradient = _gradient(list(st.last_curve_price_sequence))

    if st.last_negative_curve_update_ts is not None:
        last_neg_age = int(now - st.last_negative_curve_update_ts)
    else:
        last_neg_age = -1

    last_loss_ts = None
    if closed_losses:
        last_loss_ts = closed_losses[-1].outcome_ts
    if last_loss_ts is not None:
        last_loss_age = int(now - last_loss_ts)
    else:
        last_loss_age = -1

    last_bank = closed_banks[-1] if closed_banks else None

    # waves_count = number of distinct virtual_bank_wins observed (each is a wave).
    waves = len(closed_banks)

    # runner_confidence_score (weighted sum per V42G).
    # Weights chosen to land in [0, 100]-ish range:
    #   + consecutive_wins * 20
    #   + wins_5s * 10
    #   - losses_5s * 15
    #   + (quote_gradient > 0) * 10
    #   + (curve_gradient > 0) * 10
    #   - (last_neg_age_ms >= 0 and last_neg_age_ms < 1500) * 25
    #   + (last_bank.bank_pnl_sol > 0.001) * 5
    #   - clamp(0, max_adverse_before_bank/0.001) * 5
    score = 0.0
    score += cons_wins * 20.0
    score += len(wins_5s) * 10.0
    score -= len(losses_5s) * 15.0
    score += 10.0 if quote_gradient > 0 else 0.0
    score += 10.0 if curve_gradient > 0 else 0.0
    if last_neg_age >= 0 and last_neg_age < 1500:
        score -= 25.0
    if last_bank is not None and last_bank.bank_pnl_sol is not None and last_bank.bank_pnl_sol > 0.001:
        score += 5.0
    score -= min(20.0, abs(max_adv) / 0.001 * 5.0)

    snap = RunnerStateSnapshot(
        mint=mint,
        computed_at_ms=now,
        virtual_wins_last_2s=len(wins_2s),
        virtual_wins_last_5s=len(wins_5s),
        virtual_losses_last_5s=len(losses_5s),
        consecutive_virtual_wins=cons_wins,
        average_bank_time_ms=avg_bank_time_ms,
        average_virtual_win_pnl=avg_win_pnl,
        max_virtual_adverse_before_bank=float(max_adv),
        current_quote_gradient=float(quote_gradient),
        current_accountSubscribe_curve_gradient=float(curve_gradient),
        last_negative_curve_update_age_ms=last_neg_age,
        last_virtual_loss_age_ms=last_loss_age,
        last_virtual_bank_ts_ms=last_bank.outcome_ts if last_bank else None,
        last_virtual_bank_pnl_sol=last_bank.bank_pnl_sol if last_bank else None,
        runner_confidence_score=float(score),
        waves_count=int(waves),
    )
    return snap


def emit_runner_state_log(rs: RunnerStateSnapshot, log_fn: Callable[[str], None]) -> None:
    if rs is None:
        return
    try:
        log_fn(
            f"PGG2-V42G-RUNNER-STATE mint={_short(rs.mint)} "
            f"wins_2s={rs.virtual_wins_last_2s} wins_5s={rs.virtual_wins_last_5s} "
            f"losses_5s={rs.virtual_losses_last_5s} "
            f"consecutive_wins={rs.consecutive_virtual_wins} "
            f"avg_bank_ms={rs.average_bank_time_ms if rs.average_bank_time_ms is None else round(rs.average_bank_time_ms,1)} "
            f"avg_win_pnl={rs.average_virtual_win_pnl if rs.average_virtual_win_pnl is None else round(rs.average_virtual_win_pnl,6)} "
            f"max_adv_before_bank={rs.max_virtual_adverse_before_bank:+.6f} "
            f"qgrad={rs.current_quote_gradient:+.6f} "
            f"cgrad={rs.current_accountSubscribe_curve_gradient:+.6f} "
            f"last_neg_age_ms={rs.last_negative_curve_update_age_ms} "
            f"last_loss_age_ms={rs.last_virtual_loss_age_ms} "
            f"waves={rs.waves_count} "
            f"score={rs.runner_confidence_score:.2f}"
        )
    except Exception:
        pass


__all__ = ["RunnerStateSnapshot", "compute_runner_state", "emit_runner_state_log"]
