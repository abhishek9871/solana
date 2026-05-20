"""V42G Phase 3/4 — Runner rule evaluator and late-entry blocker.

Reads `data/v42g_runner_rules.json` (or path given) and evaluates each rule
against a `RunnerStateSnapshot` + last `QuoteSnapshotLite` + the engine's
ticket history.

CAUSALITY: rule evaluation depends ONLY on already-observed tickets (i.e. on
RunnerStateSnapshot fields). A ticket whose `first_observation_ts` is None
cannot be in the rolling counters because RunnerStateSnapshot itself only
counts tickets with outcomes already observed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pgg2_v42g_virtual_ticket_engine import VirtualTicket, VirtualTicketEngine, QuoteSnapshotLite
from pgg2_v42g_runner_state import RunnerStateSnapshot


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class RuleEvalResult:
    rule_id: str
    passed: bool
    reason: str
    diagnostics: Dict[str, Any]


@dataclass
class LateBlockResult:
    blocked: bool
    reasons: List[str]
    diagnostics: Dict[str, Any]


def load_rules(path: Optional[str] = None) -> Dict[str, Any]:
    if path is None:
        candidates = [
            Path("/root/piggy/data/v42g_runner_rules.json"),
            Path("data/v42g_runner_rules.json"),
            Path(__file__).resolve().parent / "data_v42g_runner_rules.json",
        ]
        for c in candidates:
            if c.exists():
                path = str(c)
                break
    if path is None:
        raise FileNotFoundError("v42g_runner_rules.json not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ----- helpers ------------------------------------------------------------

def _wins_last_window(tickets: List[VirtualTicket], now_ms: int, window_ms: int) -> List[VirtualTicket]:
    return [
        t for t in tickets
        if t.outcome == "virtual_bank_win"
        and t.outcome_ts is not None
        and (now_ms - t.outcome_ts) <= window_ms
    ]


def _last_bank_ticket(tickets: List[VirtualTicket]) -> Optional[VirtualTicket]:
    banks = [t for t in tickets if t.outcome == "virtual_bank_win" and t.outcome_ts is not None]
    banks.sort(key=lambda t: t.outcome_ts or 0)
    return banks[-1] if banks else None


def _last_ticket(tickets: List[VirtualTicket]) -> Optional[VirtualTicket]:
    closed = [t for t in tickets if t.outcome_ts is not None]
    closed.sort(key=lambda t: t.outcome_ts or 0)
    return closed[-1] if closed else None


def _latest_observed_curve_negative(rs: RunnerStateSnapshot) -> bool:
    return rs.current_accountSubscribe_curve_gradient < 0


def _saw_negative_curve_after_first_win(
    engine: VirtualTicketEngine, mint: str, first_win: Optional[VirtualTicket]
) -> bool:
    if first_win is None:
        return False
    st = engine.mint_state(mint)
    if st is None:
        return False
    if st.last_negative_curve_update_ts is None:
        return False
    return st.last_negative_curve_update_ts > first_win.outcome_ts


# ----- rule evaluators ----------------------------------------------------

def _eval_two_ticket_runner(
    rule_cfg: Dict[str, Any],
    rs: RunnerStateSnapshot,
    last_snap: QuoteSnapshotLite,
    tickets: List[VirtualTicket],
    engine: VirtualTicketEngine,
) -> RuleEvalResult:
    diag: Dict[str, Any] = {}
    diag["wins_2500ms"] = rs.virtual_wins_last_2s
    diag["consecutive_wins"] = rs.consecutive_virtual_wins
    diag["last_loss_age_ms"] = rs.last_virtual_loss_age_ms
    diag["avg_bank_ms"] = rs.average_bank_time_ms
    diag["quote_gradient"] = rs.current_quote_gradient
    diag["curve_gradient"] = rs.current_accountSubscribe_curve_gradient
    diag["last_neg_curve_age_ms"] = rs.last_negative_curve_update_age_ms

    if rs.virtual_wins_last_2s < int(rule_cfg["min_virtual_bank_wins_last_2500ms"]):
        return RuleEvalResult("v42g_two_ticket_runner", False, "insufficient_wins_2500ms", diag)
    if rs.consecutive_virtual_wins < int(rule_cfg["min_consecutive_virtual_wins"]):
        return RuleEvalResult("v42g_two_ticket_runner", False, "insufficient_consecutive_wins", diag)
    max_loss_age = int(rule_cfg["max_virtual_loss_age_ms"])
    if max_loss_age == -1:
        if rs.last_virtual_loss_age_ms >= 0 and rs.last_virtual_loss_age_ms <= 5000:
            return RuleEvalResult("v42g_two_ticket_runner", False, "recent_virtual_loss", diag)
    else:
        if rs.last_virtual_loss_age_ms >= 0 and rs.last_virtual_loss_age_ms < max_loss_age:
            return RuleEvalResult("v42g_two_ticket_runner", False, "recent_virtual_loss", diag)
    if rs.average_bank_time_ms is None or rs.average_bank_time_ms > float(rule_cfg["max_avg_bank_time_ms"]):
        return RuleEvalResult("v42g_two_ticket_runner", False, "avg_bank_time_too_slow", diag)
    if rule_cfg["require_quote_gradient_nonneg"] and rs.current_quote_gradient < 0:
        return RuleEvalResult("v42g_two_ticket_runner", False, "quote_gradient_negative", diag)
    if rule_cfg["require_curve_gradient_nonneg_or_no_neg_after_last_win"]:
        last_win = _last_bank_ticket(tickets)
        neg_after = _saw_negative_curve_after_first_win(engine, last_snap.extra.get("mint", rs.mint), last_win)
        if rs.current_accountSubscribe_curve_gradient < 0 and neg_after:
            return RuleEvalResult("v42g_two_ticket_runner", False, "curve_negative_after_win", diag)
    if rule_cfg["require_route_pump_bc"] and last_snap.route != "pump_bc":
        return RuleEvalResult("v42g_two_ticket_runner", False, "route_not_pump_bc", diag)
    if rule_cfg["require_sim_needed_zero"] and int(last_snap.sim_needed) != 0:
        return RuleEvalResult("v42g_two_ticket_runner", False, "sim_needed_nonzero", diag)
    if rule_cfg["require_fresh_quote"] and not bool(last_snap.fresh_quote):
        return RuleEvalResult("v42g_two_ticket_runner", False, "stale_quote", diag)
    if rule_cfg["require_buy_min_token_guard_encodable"] and last_snap.buy_quote_tokens <= 0:
        return RuleEvalResult("v42g_two_ticket_runner", False, "buy_guard_not_encodable", diag)
    if rule_cfg["require_sell_min_sol_guard_encodable"] and last_snap.sell_quote_out_lamports <= 0:
        return RuleEvalResult("v42g_two_ticket_runner", False, "sell_guard_not_encodable", diag)
    return RuleEvalResult("v42g_two_ticket_runner", True, "ok", diag)


def _eval_fast_repeat_runner(
    rule_cfg: Dict[str, Any],
    rs: RunnerStateSnapshot,
    last_snap: QuoteSnapshotLite,
    tickets: List[VirtualTicket],
    engine: VirtualTicketEngine,
) -> RuleEvalResult:
    diag: Dict[str, Any] = {}
    diag["wins_5s"] = rs.virtual_wins_last_5s

    if rs.virtual_wins_last_5s < int(rule_cfg["min_virtual_bank_wins_last_5000ms"]):
        return RuleEvalResult("v42g_fast_repeat_runner", False, "insufficient_wins_5s", diag)
    fast_wins = [
        t for t in tickets
        if t.outcome == "virtual_bank_win"
        and t.bank_time_ms is not None and t.bank_time_ms <= 750
    ]
    diag["fast_wins"] = len(fast_wins)
    if len(fast_wins) < int(rule_cfg["min_wins_with_bank_time_le_750ms"]):
        return RuleEvalResult("v42g_fast_repeat_runner", False, "insufficient_fast_wins", diag)
    if not bool(rule_cfg["max_virtual_loss_after_first_win"]) and rs.last_virtual_loss_age_ms >= 0:
        # Default config: "max_virtual_loss_after_first_win": false means
        # "forbid any virtual loss happening after the first win".
        last_win = _last_bank_ticket(tickets)
        if last_win is not None:
            # find any loss whose outcome_ts > first_win.outcome_ts
            first_win = sorted(
                [t for t in tickets if t.outcome == "virtual_bank_win" and t.outcome_ts is not None],
                key=lambda t: t.outcome_ts or 0,
            )[0]
            loss_after = [
                t for t in tickets
                if t.outcome == "virtual_loss"
                and t.outcome_ts is not None
                and t.outcome_ts > (first_win.outcome_ts or 0)
            ]
            diag["losses_after_first_win"] = len(loss_after)
            if loss_after:
                return RuleEvalResult("v42g_fast_repeat_runner", False, "loss_after_first_win", diag)
    # max_current_below_last_bank_pnl: current pnl must not drop more than X
    # below the last bank pnl.
    last_bank = _last_bank_ticket(tickets)
    if last_bank is not None and last_bank.bank_pnl_sol is not None:
        # Use the last open ticket's most-recent observed pnl (causal — already
        # observed in a prior snapshot ingest cycle).
        opens = [t for t in tickets if t.future_pnls]
        if opens:
            cur_pnl = opens[-1].future_pnls[-1][2]
            diag["cur_pnl"] = cur_pnl
            diag["last_bank_pnl"] = last_bank.bank_pnl_sol
            if (last_bank.bank_pnl_sol - cur_pnl) > float(rule_cfg["max_current_below_last_bank_pnl"]):
                return RuleEvalResult("v42g_fast_repeat_runner", False, "current_too_far_below_last_bank", diag)
    return RuleEvalResult("v42g_fast_repeat_runner", True, "ok", diag)


def _eval_high_edge_runner(
    rule_cfg: Dict[str, Any],
    rs: RunnerStateSnapshot,
    last_snap: QuoteSnapshotLite,
    tickets: List[VirtualTicket],
    engine: VirtualTicketEngine,
) -> RuleEvalResult:
    diag: Dict[str, Any] = {}
    min_one_pnl = float(rule_cfg["min_one_virtual_win_pnl_sol"])
    banks = [t for t in tickets if t.outcome == "virtual_bank_win" and t.bank_pnl_sol is not None]
    diag["banks"] = len(banks)
    big_banks = [t for t in banks if t.bank_pnl_sol >= min_one_pnl]
    diag["big_banks"] = len(big_banks)
    if not big_banks:
        return RuleEvalResult("v42g_high_edge_runner", False, "no_high_edge_win", diag)
    # min_second_ticket_outcome: scratch_or_better
    # interpret as: among all closed tickets, at least 2 with outcome
    # virtual_bank_win OR virtual_scratch.
    ok_seconds = [
        t for t in tickets
        if t.outcome in ("virtual_bank_win", "virtual_scratch")
    ]
    diag["scratch_or_better"] = len(ok_seconds)
    if len(ok_seconds) < 2:
        return RuleEvalResult("v42g_high_edge_runner", False, "no_second_ticket_scratch_or_better", diag)
    if rule_cfg["require_curve_or_quote_gradient_nonneg"]:
        if rs.current_quote_gradient < 0 and rs.current_accountSubscribe_curve_gradient < 0:
            return RuleEvalResult("v42g_high_edge_runner", False, "both_gradients_negative", diag)
    if rule_cfg["forbid_negative_curve_update_after_first_win"]:
        first_win = sorted(banks, key=lambda t: t.outcome_ts or 0)[0]
        st = engine.mint_state(rs.mint)
        if (
            st is not None
            and st.last_negative_curve_update_ts is not None
            and first_win.outcome_ts is not None
            and st.last_negative_curve_update_ts > first_win.outcome_ts
        ):
            return RuleEvalResult("v42g_high_edge_runner", False, "neg_curve_after_first_win", diag)
    return RuleEvalResult("v42g_high_edge_runner", True, "ok", diag)


def _eval_curve_plus_virtual_runner(
    rule_cfg: Dict[str, Any],
    rs: RunnerStateSnapshot,
    last_snap: QuoteSnapshotLite,
    tickets: List[VirtualTicket],
    engine: VirtualTicketEngine,
) -> RuleEvalResult:
    diag: Dict[str, Any] = {}
    st = engine.mint_state(rs.mint)
    if st is None:
        return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "no_state", diag)
    seq = list(st.last_curve_price_sequence)
    diag["curve_seq_len"] = len(seq)
    if rule_cfg["require_positive_curve_delta_sequence"]:
        if len(seq) < 3:
            return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "curve_seq_too_short", diag)
        deltas = [seq[i] - seq[i - 1] for i in range(1, len(seq))]
        diag["last_two_deltas"] = deltas[-2:]
        if not (deltas[-1] >= 0 and deltas[-2] >= 0):
            return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "curve_delta_not_positive_sequence", diag)
    banks = [t for t in tickets if t.outcome == "virtual_bank_win"]
    diag["banks"] = len(banks)
    if len(banks) < int(rule_cfg["min_virtual_bank_wins"]):
        return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "insufficient_banks", diag)
    if rule_cfg["require_second_ticket_currently_positive_not_yet_banked"]:
        opens = [t for t in tickets if not t.closed and t.future_pnls]
        if not opens:
            return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "no_open_ticket", diag)
        cur_pnl = opens[-1].future_pnls[-1][2]
        diag["open_ticket_pnl"] = cur_pnl
        if cur_pnl <= 0:
            return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "open_ticket_not_positive", diag)
    if rule_cfg["require_quote_gradient_positive"] and rs.current_quote_gradient <= 0:
        return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "quote_gradient_not_positive", diag)
    if rule_cfg["require_fresh_buy_quote"] and not bool(last_snap.fresh_quote):
        return RuleEvalResult("v42g_curve_plus_virtual_runner", False, "stale_buy_quote", diag)
    return RuleEvalResult("v42g_curve_plus_virtual_runner", True, "ok", diag)


_RULE_DISPATCH: Dict[str, Callable[..., RuleEvalResult]] = {
    "v42g_two_ticket_runner": _eval_two_ticket_runner,
    "v42g_fast_repeat_runner": _eval_fast_repeat_runner,
    "v42g_high_edge_runner": _eval_high_edge_runner,
    "v42g_curve_plus_virtual_runner": _eval_curve_plus_virtual_runner,
}


def evaluate_all_rules(
    engine: VirtualTicketEngine,
    rs: RunnerStateSnapshot,
    last_snap: QuoteSnapshotLite,
    rules_cfg: Dict[str, Any],
) -> List[RuleEvalResult]:
    out: List[RuleEvalResult] = []
    tickets = engine.tickets(rs.mint)
    last_snap.extra.setdefault("mint", rs.mint)
    for rid, rcfg in rules_cfg.get("rules", {}).items():
        fn = _RULE_DISPATCH.get(rid)
        if fn is None:
            continue
        try:
            out.append(fn(rcfg, rs, last_snap, tickets, engine))
        except Exception as exc:
            out.append(RuleEvalResult(rid, False, f"eval_error:{type(exc).__name__}", {"err": str(exc)}))
    return out


# ----- Phase 4: late-entry blocker --------------------------------------

def evaluate_late_entry_blockers(
    engine: VirtualTicketEngine,
    rs: RunnerStateSnapshot,
    last_snap: QuoteSnapshotLite,
    rules_cfg: Dict[str, Any],
    now_ms: int,
) -> LateBlockResult:
    cfg = rules_cfg.get("late_entry_blockers", {})
    diag: Dict[str, Any] = {}
    reasons: List[str] = []
    tickets = engine.tickets(rs.mint)
    first_win = None
    banks = [t for t in tickets if t.outcome == "virtual_bank_win" and t.outcome_ts is not None]
    if banks:
        first_win = sorted(banks, key=lambda t: t.outcome_ts or 0)[0]

    if first_win is None:
        # No first virtual win yet — let any rule decide; late-blocker is N/A.
        return LateBlockResult(blocked=False, reasons=[], diagnostics={"first_win": None})

    age_since_first = now_ms - (first_win.outcome_ts or now_ms)
    diag["age_since_first_win_ms"] = age_since_first
    max_age = int(cfg.get("max_age_since_first_virtual_win_ms", 5000))
    if age_since_first > max_age:
        reasons.append("too_late_after_first_win")

    if cfg.get("block_if_current_quote_below_last_virtual_bank_quote", True):
        last_bank = _last_bank_ticket(tickets)
        if last_bank is not None and last_snap.sell_quote_out_lamports > 0:
            # Recover last bank's expected sell_out from last_bank's bank_pnl_sol.
            # bank_pnl ≈ sell_out - amount_sol - 2*tx_fee  ⇒  sell_out ≈ bank_pnl + amount_sol + 2*tx_fee
            last_bank_sell_out_sol = (
                (last_bank.bank_pnl_sol or 0.0)
                + last_bank.amount_sol
                + 0.000020
            )
            cur_sell_out_sol = float(last_snap.sell_quote_out_lamports) / 1_000_000_000
            diag["cur_sell_out_sol"] = cur_sell_out_sol
            diag["last_bank_sell_out_sol"] = last_bank_sell_out_sol
            if cur_sell_out_sol < last_bank_sell_out_sol:
                reasons.append("current_quote_below_last_bank_quote")

    if cfg.get("block_if_latest_accountSubscribe_curve_update_negative", True):
        if rs.current_accountSubscribe_curve_gradient < 0:
            reasons.append("latest_curve_update_negative")

    if cfg.get("block_if_latest_virtual_ticket_scratch_or_loss", True):
        last_closed = _last_ticket(tickets)
        if last_closed is not None and last_closed.outcome in ("virtual_scratch", "virtual_loss"):
            reasons.append("latest_virtual_ticket_scratch_or_loss")

    if cfg.get("block_if_current_quote_gradient_negative", True):
        if rs.current_quote_gradient < 0:
            reasons.append("current_quote_gradient_negative")

    max_waves = int(cfg.get("max_waves_unless_still_improving", 3))
    diag["waves_count"] = rs.waves_count
    if rs.waves_count > max_waves:
        # "unless_still_improving" — improving = last bank pnl >= prior bank pnl
        improving = False
        if len(banks) >= 2:
            sb = sorted(banks, key=lambda t: t.outcome_ts or 0)
            if (sb[-1].bank_pnl_sol or 0) > (sb[-2].bank_pnl_sol or 0):
                improving = True
        if not improving:
            reasons.append("too_many_waves_not_improving")

    blocked = len(reasons) > 0
    return LateBlockResult(blocked=blocked, reasons=reasons, diagnostics=diag)


def emit_late_block_log(mint: str, lbr: LateBlockResult, log_fn: Callable[[str], None]) -> None:
    if not lbr.blocked:
        return
    try:
        log_fn(
            f"PGG2-V42G-LATE-RUNNER-BLOCK mint={_short(mint)} "
            f"reasons={','.join(lbr.reasons)} "
            f"diag={lbr.diagnostics}"
        )
    except Exception:
        pass


def emit_candidate_log(mint: str, res: RuleEvalResult, log_fn: Callable[[str], None]) -> None:
    if not res.passed:
        return
    try:
        log_fn(
            f"PGG2-V42G-CANDIDATE-ENTRY mint={_short(mint)} rule={res.rule_id} "
            f"diag={res.diagnostics}"
        )
    except Exception:
        pass


def emit_rule_block_log(mint: str, res: RuleEvalResult, log_fn: Callable[[str], None]) -> None:
    if res.passed:
        return
    try:
        log_fn(
            f"PGG2-V42G-RULE-BLOCK mint={_short(mint)} rule={res.rule_id} "
            f"reason={res.reason} diag={res.diagnostics}"
        )
    except Exception:
        pass


__all__ = [
    "load_rules",
    "evaluate_all_rules",
    "evaluate_late_entry_blockers",
    "emit_late_block_log",
    "emit_candidate_log",
    "emit_rule_block_log",
    "RuleEvalResult",
    "LateBlockResult",
]
