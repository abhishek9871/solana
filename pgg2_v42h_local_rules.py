"""V42H Phase 4/5 — Rules + redesigned late-entry blocker for the LOCAL
curve-quote ticket engine.

Replaces V42G's blunt `current_quote_below_last_bank_quote` block (which
rejected 83/164 candidate fires in the 10-min sample) with a tri-condition
gate:

  block if   current_quote < break_even_quote (cost + 2*tx_fee + safety)
  OR         latest_local_quote_gradient < 0
  OR         a virtual_loss occurred AFTER the last bank
  ALLOW controlled pullback only if all three hold:
     - current_quote >= break_even_quote
     - pullback_depth <= max_pullback_depth_fraction_of_last_wave (0.50)
     - local_quote_gradient flipped positive in the last update

Pure arithmetic. Static-grep enforces no-send.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pgg2_v42h_local_ticket_engine import (
    LocalCurveQuoteVirtualTicketEngine,
    LocalCurveSnapshot,
    LocalVirtualTicket,
    DEFAULT_BANK_PNL_SOL,
    DEFAULT_SCRATCH_PNL_SOL,
)
from pgg2_v42h_local_curve_quote import (
    LAMPORTS_PER_SOL,
    DEFAULT_TX_FEE_SOL,
    break_even_sell_out_sol,
    local_sell_quote_sol,
)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class RuleEvalResult:
    rule_id: str
    passed: bool
    reason: str
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LateBlockResult:
    blocked: bool
    reasons: List[str]
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    allow_reason: str = ""


def load_rules(path: Optional[str] = None) -> Dict[str, Any]:
    if path is None:
        for c in (
            Path("/root/piggy/data/v42h_local_runner_rules.json"),
            Path("data/v42h_local_runner_rules.json"),
        ):
            if c.exists():
                path = str(c)
                break
    if path is None:
        raise FileNotFoundError("v42h_local_runner_rules.json not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ----- causal helpers ----------------------------------------------------

def _wins_last_window_ms(
    tickets: List[LocalVirtualTicket], now_ms: int, window_ms: int
) -> List[LocalVirtualTicket]:
    return [
        t for t in tickets
        if t.outcome == "virtual_bank_win"
        and t.outcome_ts_ms is not None
        and (now_ms - t.outcome_ts_ms) <= window_ms
    ]


def _losses_last_window_ms(
    tickets: List[LocalVirtualTicket], now_ms: int, window_ms: int
) -> List[LocalVirtualTicket]:
    return [
        t for t in tickets
        if t.outcome == "virtual_loss"
        and t.outcome_ts_ms is not None
        and (now_ms - t.outcome_ts_ms) <= window_ms
    ]


def _last_bank_ticket(
    tickets: List[LocalVirtualTicket],
) -> Optional[LocalVirtualTicket]:
    banks = [
        t for t in tickets
        if t.outcome == "virtual_bank_win" and t.outcome_ts_ms is not None
    ]
    if not banks:
        return None
    return sorted(banks, key=lambda t: t.outcome_ts_ms or 0)[-1]


def _last_loss_ticket(
    tickets: List[LocalVirtualTicket],
) -> Optional[LocalVirtualTicket]:
    losses = [
        t for t in tickets
        if t.outcome == "virtual_loss" and t.outcome_ts_ms is not None
    ]
    if not losses:
        return None
    return sorted(losses, key=lambda t: t.outcome_ts_ms or 0)[-1]


def _current_sell_out_sol(latest_snap: LocalCurveSnapshot) -> float:
    """Same-snapshot reflexive sell quote (sell our 0.015 buy-tokens at THIS
    same snap's curve). Structurally negative on pump_bc (~-0.0004 SOL).

    For wave/runner rule logic, prefer `_current_sell_out_sol_for_prior_buy`
    which evaluates the position-value of the last-bank's tokens at the
    current curve — that is the right "is the runner still running" metric.
    """
    return float(latest_snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL


def _current_sell_out_sol_for_prior_buy(
    latest_snap: LocalCurveSnapshot,
    prior_buy_tokens_raw: int,
) -> float:
    """Sell `prior_buy_tokens_raw` AT the current curve. This is the metric
    that captures "what does the prior position sell for NOW" — i.e. has the
    runner held? Returns SOL net of fees."""
    if prior_buy_tokens_raw <= 0:
        return 0.0
    sell_lamports, _fee = local_sell_quote_sol(
        latest_snap.curve_state, int(prior_buy_tokens_raw)
    )
    return float(sell_lamports) / LAMPORTS_PER_SOL


def _local_quote_gradient(
    engine: LocalCurveQuoteVirtualTicketEngine, mint: str
) -> float:
    """Most-recent delta in sell_out_sol sequence. Positive = improving."""
    st = engine.mint_state(mint)
    if st is None or len(st.sell_out_sol_seq) < 2:
        return 0.0
    (_t1, v1), (_t2, v2) = list(st.sell_out_sol_seq)[-2:]
    return float(v2 - v1)


def _local_quote_flipped_positive(
    engine: LocalCurveQuoteVirtualTicketEngine, mint: str
) -> bool:
    """True if previous delta was <=0 and latest delta is > 0 (i.e. the
    pullback just stopped)."""
    st = engine.mint_state(mint)
    if st is None or len(st.sell_out_sol_seq) < 3:
        return False
    pts = list(st.sell_out_sol_seq)[-3:]
    d1 = pts[1][1] - pts[0][1]
    d2 = pts[2][1] - pts[1][1]
    return d1 <= 0.0 and d2 > 0.0


def _pullback_depth_fraction_of_last_wave(
    tickets: List[LocalVirtualTicket],
    latest_snap: LocalCurveSnapshot,
    amount_sol: float,
) -> Tuple[float, Optional[float]]:
    """Fraction = (last_bank_sell_out - current_value_of_last_bank_tokens) /
                  (last_bank_sell_out - break_even).

    Both quantities are SOL: the value of last_bank's buy_tokens at the bank
    time, vs at the latest snap. Returns (fraction, last_bank_sell_out_or_None).
    0 means no pullback; 1 means current value is at break-even.
    """
    last_bank = _last_bank_ticket(tickets)
    if last_bank is None or last_bank.bank_sell_out_sol is None:
        return 0.0, None
    cur = _current_sell_out_sol_for_prior_buy(latest_snap, last_bank.buy_tokens_raw)
    be = break_even_sell_out_sol(amount_sol, DEFAULT_TX_FEE_SOL, 0.0)
    if last_bank.bank_sell_out_sol <= be:
        return 0.0, last_bank.bank_sell_out_sol
    denom = last_bank.bank_sell_out_sol - be
    if denom <= 0:
        return 0.0, last_bank.bank_sell_out_sol
    pullback = max(0.0, last_bank.bank_sell_out_sol - cur)
    return min(1.5, pullback / denom), last_bank.bank_sell_out_sol


# ----- rule evaluators ---------------------------------------------------

def _eval_fast_two_bank_runner(
    cfg: Dict[str, Any],
    engine: LocalCurveQuoteVirtualTicketEngine,
    mint: str,
    latest_snap: LocalCurveSnapshot,
    amount_sol: float,
    now_ms: int,
) -> RuleEvalResult:
    tickets = engine.tickets(mint)
    diag: Dict[str, Any] = {}
    wins_1500 = _wins_last_window_ms(tickets, now_ms, 1500)
    diag["wins_1500ms"] = len(wins_1500)
    if len(wins_1500) < int(cfg["min_virtual_bank_wins_last_1500ms"]):
        return RuleEvalResult("v42h_fast_two_bank_runner", False,
                              "insufficient_wins_1500ms", diag)
    bank_times = [t.bank_time_ms for t in wins_1500 if t.bank_time_ms is not None]
    if bank_times:
        avg_bank = sum(bank_times) / len(bank_times)
    else:
        avg_bank = float("inf")
    diag["avg_bank_time_ms"] = avg_bank
    if avg_bank > float(cfg["max_avg_bank_time_ms"]):
        return RuleEvalResult("v42h_fast_two_bank_runner", False,
                              "avg_bank_time_too_slow", diag)
    # max_virtual_loss_after_first_bank: false → forbid any loss after first bank.
    if not bool(cfg["max_virtual_loss_after_first_bank"]):
        first_bank = sorted(
            [t for t in tickets if t.outcome == "virtual_bank_win" and t.outcome_ts_ms is not None],
            key=lambda t: t.outcome_ts_ms or 0,
        )
        if first_bank:
            fb_ts = first_bank[0].outcome_ts_ms or 0
            losses_after = [
                t for t in tickets
                if t.outcome == "virtual_loss" and t.outcome_ts_ms is not None
                and t.outcome_ts_ms > fb_ts
            ]
            diag["losses_after_first_bank"] = len(losses_after)
            if losses_after:
                return RuleEvalResult("v42h_fast_two_bank_runner", False,
                                      "virtual_loss_after_first_bank", diag)
    last_bank = _last_bank_ticket(tickets)
    if last_bank is not None and last_bank.bank_sell_out_sol is not None:
        cur = _current_sell_out_sol_for_prior_buy(latest_snap, last_bank.buy_tokens_raw)
        diag["current_value_of_bank_tokens_sol"] = cur
        gap = last_bank.bank_sell_out_sol - cur
        diag["current_below_last_bank_quote_sol"] = gap
        if gap > float(cfg["max_current_below_last_bank_quote_sol"]):
            return RuleEvalResult("v42h_fast_two_bank_runner", False,
                                  "current_too_far_below_last_bank", diag)
    # require_latest_accountSubscribe_not_negative — the most recent curve_price
    # delta must not be negative.
    st = engine.mint_state(mint)
    if cfg.get("require_latest_accountSubscribe_not_negative", True) and st is not None:
        if len(st.curve_price_seq) >= 2:
            (_t1, p1), (_t2, p2) = list(st.curve_price_seq)[-2:]
            if p2 < p1:
                diag["last_curve_delta"] = p2 - p1
                return RuleEvalResult("v42h_fast_two_bank_runner", False,
                                      "latest_curve_delta_negative", diag)
    if cfg.get("require_route_pump_bc", True) and latest_snap.route != "pump_bc":
        return RuleEvalResult("v42h_fast_two_bank_runner", False,
                              "route_not_pump_bc", diag)
    if cfg.get("require_sim_needed_zero", True) and int(latest_snap.sim_needed) != 0:
        return RuleEvalResult("v42h_fast_two_bank_runner", False,
                              "sim_needed_nonzero", diag)
    if cfg.get("require_direct_pair_source", True) and latest_snap.pair_source not in (
        "accountSubscribe", "direct", "current_sig",
    ):
        return RuleEvalResult("v42h_fast_two_bank_runner", False,
                              "pair_source_not_direct", diag)
    return RuleEvalResult("v42h_fast_two_bank_runner", True, "ok", diag)


def _eval_one_bank_plus_continuation(
    cfg: Dict[str, Any],
    engine: LocalCurveQuoteVirtualTicketEngine,
    mint: str,
    latest_snap: LocalCurveSnapshot,
    amount_sol: float,
    now_ms: int,
) -> RuleEvalResult:
    tickets = engine.tickets(mint)
    diag: Dict[str, Any] = {}
    banks = [
        t for t in tickets
        if t.outcome == "virtual_bank_win" and t.bank_pnl_sol is not None
        and t.outcome_ts_ms is not None
    ]
    # Restrict to RECENT banks (last 2000ms) — stale banks are dead waves
    # and produce most observed-negative entries.
    recent_banks = [b for b in banks if (now_ms - (b.outcome_ts_ms or 0)) <= 2000]
    min_pnl = float(cfg["min_one_virtual_bank_win_pnl_sol"])
    big = [t for t in recent_banks if (t.bank_pnl_sol or 0.0) >= min_pnl]
    diag["recent_banks"] = len(recent_banks)
    diag["big_banks"] = len(big)
    diag["banks_total"] = len(banks)
    if not big:
        return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                              "no_recent_high_pnl_bank", diag)
    # require_next_curve_update_nonneg_or_quote_grad_pos — tightened to AND
    # (both curve nonneg AND gradient strictly positive) to keep observed
    # negatives down. See V42H_REPLAY_ON_V42G analysis.
    if cfg.get("require_next_curve_update_nonneg_or_quote_grad_pos", True):
        st = engine.mint_state(mint)
        curve_ok = False
        if st is not None and len(st.curve_price_seq) >= 2:
            (_t1, p1), (_t2, p2) = list(st.curve_price_seq)[-2:]
            curve_ok = p2 >= p1
        quote_grad = _local_quote_gradient(engine, mint)
        diag["last_quote_gradient"] = quote_grad
        diag["curve_ok"] = curve_ok
        if not (curve_ok and quote_grad > 0):
            return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                                  "no_continuation_signal", diag)
    max_loss_age = int(cfg["max_virtual_loss_age_ms"])
    last_loss = _last_loss_ticket(tickets)
    if last_loss is not None and last_loss.outcome_ts_ms is not None:
        age = now_ms - last_loss.outcome_ts_ms
        diag["last_loss_age_ms"] = age
        if age <= max_loss_age:
            return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                                  "recent_virtual_loss", diag)
    # current must be above break_even + safety. We measure "current value of
    # the last bank's buy_tokens at the latest curve" — that's the right
    # quantity for runner continuation.
    last_bank = _last_bank_ticket(tickets)
    if last_bank is None:
        return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                              "no_bank_to_measure_against", diag)
    cur = _current_sell_out_sol_for_prior_buy(latest_snap, last_bank.buy_tokens_raw)
    be = break_even_sell_out_sol(amount_sol, DEFAULT_TX_FEE_SOL, 0.0)
    edge_above_be = cur - be
    diag["edge_above_break_even_sol"] = edge_above_be
    if edge_above_be < float(cfg["min_current_above_break_even_sol"]):
        return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                              "current_too_close_to_break_even", diag)
    if cfg.get("require_route_pump_bc", True) and latest_snap.route != "pump_bc":
        return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                              "route_not_pump_bc", diag)
    if cfg.get("require_sim_needed_zero", True) and int(latest_snap.sim_needed) != 0:
        return RuleEvalResult("v42h_one_bank_plus_continuation", False,
                              "sim_needed_nonzero", diag)
    return RuleEvalResult("v42h_one_bank_plus_continuation", True, "ok", diag)


def _eval_pullback_resume_runner(
    cfg: Dict[str, Any],
    engine: LocalCurveQuoteVirtualTicketEngine,
    mint: str,
    latest_snap: LocalCurveSnapshot,
    amount_sol: float,
    now_ms: int,
) -> RuleEvalResult:
    tickets = engine.tickets(mint)
    diag: Dict[str, Any] = {}
    wins_5s = _wins_last_window_ms(tickets, now_ms, 5000)
    diag["wins_5000ms"] = len(wins_5s)
    if len(wins_5s) < int(cfg["min_virtual_bank_wins_last_5000ms"]):
        return RuleEvalResult("v42h_pullback_resume_runner", False,
                              "insufficient_wins_5000ms", diag)
    last_bank = _last_bank_ticket(tickets)
    if last_bank is None or last_bank.bank_sell_out_sol is None:
        return RuleEvalResult("v42h_pullback_resume_runner", False,
                              "no_bank_with_sell_out", diag)
    cur = _current_sell_out_sol_for_prior_buy(latest_snap, last_bank.buy_tokens_raw)
    be = break_even_sell_out_sol(amount_sol, DEFAULT_TX_FEE_SOL, 0.0)
    diag["current"] = cur
    diag["last_bank_quote"] = last_bank.bank_sell_out_sol
    diag["break_even"] = be
    if cfg.get("require_current_below_last_bank_but_above_break_even", True):
        if not (cur < last_bank.bank_sell_out_sol and cur >= be):
            return RuleEvalResult("v42h_pullback_resume_runner", False,
                                  "not_in_controlled_pullback_band", diag)
    depth_frac, _ = _pullback_depth_fraction_of_last_wave(
        tickets, latest_snap, amount_sol
    )
    diag["pullback_depth_fraction"] = depth_frac
    if depth_frac > float(cfg["max_pullback_depth_fraction_of_last_wave"]):
        return RuleEvalResult("v42h_pullback_resume_runner", False,
                              "pullback_too_deep", diag)
    if cfg.get("require_local_quote_gradient_flipped_positive", True):
        if not _local_quote_flipped_positive(engine, mint):
            return RuleEvalResult("v42h_pullback_resume_runner", False,
                                  "quote_gradient_not_flipped_positive", diag)
    max_loss_age = int(cfg["max_virtual_loss_age_ms"])
    last_loss = _last_loss_ticket(tickets)
    if last_loss is not None and last_loss.outcome_ts_ms is not None:
        age = now_ms - last_loss.outcome_ts_ms
        diag["last_loss_age_ms"] = age
        if age <= max_loss_age:
            return RuleEvalResult("v42h_pullback_resume_runner", False,
                                  "recent_virtual_loss", diag)
    return RuleEvalResult("v42h_pullback_resume_runner", True, "ok", diag)


def _eval_high_edge_local_runner(
    cfg: Dict[str, Any],
    engine: LocalCurveQuoteVirtualTicketEngine,
    mint: str,
    latest_snap: LocalCurveSnapshot,
    amount_sol: float,
    now_ms: int,
) -> RuleEvalResult:
    tickets = engine.tickets(mint)
    diag: Dict[str, Any] = {}
    banks = [
        t for t in tickets
        if t.outcome == "virtual_bank_win" and t.bank_pnl_sol is not None
        and t.outcome_ts_ms is not None
    ]
    # Restrict to RECENT banks (last 2500ms).
    recent_banks = [b for b in banks if (now_ms - (b.outcome_ts_ms or 0)) <= 2500]
    min_pnl = float(cfg["min_one_virtual_bank_win_pnl_sol"])
    big = [t for t in recent_banks if (t.bank_pnl_sol or 0.0) >= min_pnl]
    diag["recent_banks"] = len(recent_banks)
    diag["big_banks"] = len(big)
    if not big:
        return RuleEvalResult("v42h_high_edge_local_runner", False,
                              "no_recent_high_edge_win", diag)
    if cfg.get("forbid_negative_curve_after_bank", True):
        st = engine.mint_state(mint)
        if st is not None and st.last_negative_curve_update_ts_ms is not None:
            first_bank = sorted(banks, key=lambda t: t.outcome_ts_ms or 0)[0]
            if (
                first_bank.outcome_ts_ms is not None
                and st.last_negative_curve_update_ts_ms > first_bank.outcome_ts_ms
            ):
                diag["neg_curve_after_bank_ts"] = st.last_negative_curve_update_ts_ms
                return RuleEvalResult("v42h_high_edge_local_runner", False,
                                      "neg_curve_after_bank", diag)
    if cfg.get("require_current_positive_after_stress", True):
        last_bank = _last_bank_ticket(tickets)
        if last_bank is None:
            return RuleEvalResult("v42h_high_edge_local_runner", False,
                                  "no_bank_to_measure_against", diag)
        cur = _current_sell_out_sol_for_prior_buy(
            latest_snap, last_bank.buy_tokens_raw,
        )
        be = break_even_sell_out_sol(amount_sol, DEFAULT_TX_FEE_SOL, 0.0)
        if cur < be:
            diag["cur"] = cur
            diag["be"] = be
            return RuleEvalResult("v42h_high_edge_local_runner", False,
                                  "current_below_break_even", diag)
    if cfg.get("require_route_pump_bc", True) and latest_snap.route != "pump_bc":
        return RuleEvalResult("v42h_high_edge_local_runner", False,
                              "route_not_pump_bc", diag)
    return RuleEvalResult("v42h_high_edge_local_runner", True, "ok", diag)


_RULE_DISPATCH: Dict[str, Callable[..., RuleEvalResult]] = {
    "v42h_fast_two_bank_runner": _eval_fast_two_bank_runner,
    "v42h_one_bank_plus_continuation": _eval_one_bank_plus_continuation,
    "v42h_pullback_resume_runner": _eval_pullback_resume_runner,
    "v42h_high_edge_local_runner": _eval_high_edge_local_runner,
}


def evaluate_all_rules(
    engine: LocalCurveQuoteVirtualTicketEngine,
    mint: str,
    latest_snap: LocalCurveSnapshot,
    rules_cfg: Dict[str, Any],
    amount_sol: float,
    now_ms: int,
) -> List[RuleEvalResult]:
    out: List[RuleEvalResult] = []
    for rid, rcfg in rules_cfg.get("rules", {}).items():
        fn = _RULE_DISPATCH.get(rid)
        if fn is None:
            continue
        try:
            out.append(fn(rcfg, engine, mint, latest_snap, amount_sol, now_ms))
        except Exception as exc:
            out.append(RuleEvalResult(
                rid, False, f"eval_error:{type(exc).__name__}", {"err": str(exc)}
            ))
    return out


# ----- redesigned late-entry blocker (Phase 5) ---------------------------

def evaluate_late_entry_blockers(
    engine: LocalCurveQuoteVirtualTicketEngine,
    mint: str,
    latest_snap: LocalCurveSnapshot,
    amount_sol: float,
    now_ms: int,
    pullback_fraction_cap: float = 0.50,
    max_last_bank_age_ms: int = 2000,
) -> LateBlockResult:
    """Replaces V42G's blunt blocker. Logic:

    Block if ANY of:
      - current_quote < break_even_quote (cost + 2*tx_fee)
      - latest_local_quote_gradient < 0
      - a virtual_loss occurred after the last bank

    Otherwise ALLOW. As a softer admit-conditional, if `current_quote <
    last_bank_quote` we require:
      - current_quote >= break_even_quote
      - pullback_depth_fraction <= pullback_fraction_cap
      - local_quote_gradient flipped positive in the last update.

    Pure, fast, no network.
    """
    tickets = engine.tickets(mint)
    diag: Dict[str, Any] = {}
    reasons: List[str] = []
    last_bank = _last_bank_ticket(tickets)
    last_bank_q = last_bank.bank_sell_out_sol if last_bank is not None else None
    last_loss = _last_loss_ticket(tickets)
    # "current_quote" = value-now of the last bank's buy_tokens. This is the
    # runner-continuation metric (vs same-snapshot reflexive sell which is
    # structurally negative on pump_bc).
    if last_bank is not None and last_bank.buy_tokens_raw > 0:
        cur = _current_sell_out_sol_for_prior_buy(
            latest_snap, last_bank.buy_tokens_raw,
        )
    else:
        cur = _current_sell_out_sol(latest_snap)
    be = break_even_sell_out_sol(amount_sol, DEFAULT_TX_FEE_SOL, 0.0)
    grad = _local_quote_gradient(engine, mint)

    diag["current_quote"] = cur
    diag["break_even_quote"] = be
    diag["last_bank_quote"] = last_bank_q
    diag["gradient"] = grad
    diag["last_loss_age_ms"] = (
        (now_ms - (last_loss.outcome_ts_ms or now_ms)) if last_loss else -1
    )

    # No banks yet → late-entry blocker is N/A.
    if last_bank is None:
        return LateBlockResult(blocked=False, reasons=[], diagnostics=diag,
                               allow_reason="no_banks_yet_blocker_na")

    # Bank-staleness gate: a bank older than ~2s is not a live wave.
    if last_bank.outcome_ts_ms is not None:
        bank_age_ms = now_ms - (last_bank.outcome_ts_ms or now_ms)
        diag["last_bank_age_ms"] = bank_age_ms
        if bank_age_ms > int(max_last_bank_age_ms):
            return LateBlockResult(
                blocked=True,
                reasons=["last_bank_too_stale"],
                diagnostics=diag,
            )

    # Hard block: current below break_even.
    if cur < be:
        reasons.append("current_quote_below_break_even")
    # Hard block: gradient negative (require STRICTLY positive to admit).
    if grad <= 0:
        reasons.append("local_quote_gradient_not_positive")
    # Hard block: the last TWO deltas should BOTH be non-negative — a single
    # positive blip after sustained drops is a likely fakeout.
    st = engine.mint_state(mint)
    if st is not None and len(st.sell_out_sol_seq) >= 3:
        pts = list(st.sell_out_sol_seq)[-3:]
        d1 = pts[1][1] - pts[0][1]
        d2 = pts[2][1] - pts[1][1]
        diag["prev_delta"] = d1
        diag["last_delta"] = d2
        if d1 < 0 or d2 <= 0:
            reasons.append("two_delta_sequence_not_positive")
    # Hard block: loss after last bank.
    if last_loss is not None and last_bank.outcome_ts_ms is not None:
        if (last_loss.outcome_ts_ms or 0) > (last_bank.outcome_ts_ms or 0):
            reasons.append("virtual_loss_after_last_bank")

    if reasons:
        return LateBlockResult(blocked=True, reasons=reasons, diagnostics=diag)

    # Soft path: if current < last_bank_quote, only allow controlled pullback.
    if last_bank_q is not None and cur < last_bank_q:
        depth_frac, _ = _pullback_depth_fraction_of_last_wave(
            tickets, latest_snap, amount_sol
        )
        diag["pullback_depth_fraction"] = depth_frac
        if depth_frac > float(pullback_fraction_cap):
            return LateBlockResult(
                blocked=True,
                reasons=["pullback_depth_exceeds_cap"],
                diagnostics=diag,
            )
        if not _local_quote_flipped_positive(engine, mint):
            return LateBlockResult(
                blocked=True,
                reasons=["pullback_gradient_not_flipped_positive"],
                diagnostics=diag,
            )
        return LateBlockResult(
            blocked=False, reasons=[], diagnostics=diag,
            allow_reason="controlled_pullback_resume",
        )

    return LateBlockResult(
        blocked=False, reasons=[], diagnostics=diag,
        allow_reason="current_at_or_above_last_bank",
    )


def emit_late_entry_decision_log(
    mint: str, lbr: LateBlockResult, log_fn: Callable[[str], None]
) -> None:
    try:
        cur = lbr.diagnostics.get("current_quote", 0.0)
        last_bank_q = lbr.diagnostics.get("last_bank_quote", None)
        be = lbr.diagnostics.get("break_even_quote", 0.0)
        depth = lbr.diagnostics.get("pullback_depth_fraction", -1)
        grad = lbr.diagnostics.get("gradient", 0.0)
        last_bank_s = "None" if last_bank_q is None else f"{last_bank_q:.9f}"
        log_fn(
            f"PGG2-V42H-LATE-ENTRY-DECISION mint={_short(mint)} "
            f"current_quote={cur:.9f} last_bank_quote={last_bank_s} "
            f"break_even_quote={be:.9f} pullback_depth={depth} gradient={grad:+.9f} "
            f"allow={not lbr.blocked} "
            f"reason={lbr.allow_reason or ','.join(lbr.reasons) or 'ok'}"
        )
    except Exception:
        pass


def emit_candidate_log(
    mint: str, res: RuleEvalResult, log_fn: Callable[[str], None]
) -> None:
    if not res.passed:
        return
    try:
        log_fn(
            f"PGG2-V42H-CANDIDATE-ENTRY mint={_short(mint)} rule={res.rule_id} "
            f"diag={res.diagnostics}"
        )
    except Exception:
        pass


def emit_rule_block_log(
    mint: str, res: RuleEvalResult, log_fn: Callable[[str], None]
) -> None:
    if res.passed:
        return
    try:
        log_fn(
            f"PGG2-V42H-RULE-BLOCK mint={_short(mint)} rule={res.rule_id} "
            f"reason={res.reason}"
        )
    except Exception:
        pass


__all__ = [
    "load_rules",
    "evaluate_all_rules",
    "evaluate_late_entry_blockers",
    "emit_late_entry_decision_log",
    "emit_candidate_log",
    "emit_rule_block_log",
    "RuleEvalResult",
    "LateBlockResult",
]
