"""V42J Phase 1 - Bank-event interrupt emitter.

V42H/V42HSAFE/V42I all entered AFTER a virtual bank had completed, by which
time the runner had already retraced (8/10 SAFE losses, 4/10 V42I losses).
V42J's hypothesis: emit a V42JBankEvent SYNCHRONOUSLY inside the same
accountSubscribe-update call frame that processes the snap which causes a
virtual ticket to cross the +0.00060 SOL bank threshold. Any entry decision
is then gated by event_age_ms <= 150 so we never act on a stale bank.

HARD RULE
=========
This event is emitted ONLY inside the accountSubscribe update processing
path; the BankEventInterruptEmitter is callable only from on_curve_update;
emission outside this path is a programming error and the function will
raise V42JEmissionContextError.

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ----- static-grep self-check ----------------------------------------
_FORBIDDEN_CALL_PATTERNS = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _src = _self.read()
for _pat in _FORBIDDEN_CALL_PATTERNS:
    if _re.search(_pat, _src):
        sys.stderr.write(
            f"V42J-BANK-EVENT-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v42j_bank_event")


BANK_THRESHOLD_SOL = 0.00060
FRESHNESS_TTL_MS_DEFAULT = 150


class V42JEmissionContextError(RuntimeError):
    """Raised if BankEventInterruptEmitter is invoked outside on_curve_update."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass(frozen=True)
class V42JBankEvent:
    """A bank-threshold crossing event emitted SYNCHRONOUSLY inside the
    accountSubscribe processing path. All 13 fields are causally bound to
    the snap that triggered the crossing."""
    mint: str
    event_ts_ms: int                       # = snap.ts_ms of triggering update
    curve_update_slot: int
    triggering_ticket_id: str
    triggering_ticket_buy_ts_ms: int       # ticket's open_snap ts
    triggering_ticket_tokens_raw: int      # tokens the ticket "bought"
    bank_pnl: float                        # ticket's realized PnL at this snap
    bank_threshold: float                  # always BANK_THRESHOLD_SOL
    current_curve_state: Dict[str, Any]    # snapshot of vsol/vtok/rtok/fee
    current_local_buy_quote_tokens_raw: int   # 0.015 SOL buy at THIS snap
    current_local_sell_quote_sol: float        # immediate sell-back of those tokens
    source: str                             # "accountSubscribe"
    event_fresh_until_ms: int               # event_ts_ms + ttl_ms

    def freshness_ms(self, now_ms: int) -> int:
        return int(now_ms) - int(self.event_ts_ms)

    def is_fresh(self, now_ms: int, ttl_ms: int = FRESHNESS_TTL_MS_DEFAULT) -> bool:
        return self.freshness_ms(now_ms) <= int(ttl_ms)


class BankEventInterruptEmitter:
    """Bank-event detector that emits a V42JBankEvent in the same call frame
    as the accountSubscribe update which triggered the crossing.

    Walks all currently-tracked virtual tickets for a given mint. For any
    ticket whose CURRENT PnL transitions from < +0.00060 to >= +0.00060
    between previous-snap and this snap, emit one event.

    USE FROM on_curve_update(...) ONLY.
    """

    def __init__(
        self,
        amount_sol: float = 0.015,
        bank_threshold_sol: float = BANK_THRESHOLD_SOL,
        ttl_ms: int = FRESHNESS_TTL_MS_DEFAULT,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.amount_sol = float(amount_sol)
        self.bank_threshold_sol = float(bank_threshold_sol)
        self.ttl_ms = int(ttl_ms)
        self._log = logger or (lambda _m: None)
        # caller_marker is required: must be one of the documented contexts.
        self._allowed_contexts = {"on_curve_update"}
        # Track which (mint, ticket_id) we already emitted for - once per ticket.
        self._emitted_by_ticket: Dict[str, set] = {}
        # Per-mint rolling record of last N events for repeat-rule eval.
        self._recent_events: Dict[str, List[V42JBankEvent]] = {}
        self.stats: Dict[str, int] = {
            "events_emitted": 0,
            "ticks_processed": 0,
            "ticket_walks": 0,
            "context_errors": 0,
        }

    def on_curve_update(
        self,
        mint: str,
        snap: Any,
        ts_ms: int,
        slot: int,
        virtual_ticket_engine: Any,
        caller_context: str = "on_curve_update",
    ) -> List[V42JBankEvent]:
        """Synchronously emit V42JBankEvent[] for tickets that crossed the
        bank threshold on this snap. MUST be called inside the
        accountSubscribe update processing path."""
        if caller_context not in self._allowed_contexts:
            self.stats["context_errors"] += 1
            raise V42JEmissionContextError(
                f"caller_context={caller_context!r} not in {self._allowed_contexts}"
            )
        if not mint or snap is None:
            return []
        self.stats["ticks_processed"] += 1

        events: List[V42JBankEvent] = []

        # Get all tickets for this mint from the engine (engine owns them).
        try:
            tickets = list(virtual_ticket_engine.tickets(mint))
        except Exception:
            return []

        # Compute current 0.015-SOL buy + immediate sell at this snap state.
        try:
            from pgg2_v42h_local_curve_quote import (
                local_buy_quote_tokens_raw,
                local_sell_quote_sol,
                LAMPORTS_PER_SOL,
            )
        except Exception:
            return []

        cur_state = snap.curve_state
        cur_buy_tokens, _bf = local_buy_quote_tokens_raw(cur_state, self.amount_sol)
        if cur_buy_tokens > 0:
            cur_sell_lamports, _sf = local_sell_quote_sol(cur_state, cur_buy_tokens)
        else:
            cur_sell_lamports = 0
        cur_sell_sol = float(cur_sell_lamports) / float(LAMPORTS_PER_SOL)

        emitted_for_mint = self._emitted_by_ticket.setdefault(mint, set())

        for tk in tickets:
            self.stats["ticket_walks"] += 1
            if tk.ticket_id in emitted_for_mint:
                continue
            if not tk.future_pnls:
                continue
            # The LATEST future_pnl entry should be the observation at THIS snap.
            # We only emit when (prev_pnl < threshold AND latest_pnl >= threshold).
            latest_idx, latest_ts, latest_pnl = tk.future_pnls[-1]
            if latest_ts != int(snap.ts_ms):
                # This ticket's latest observation wasn't this snap - skip.
                continue
            if float(latest_pnl) < self.bank_threshold_sol:
                continue
            # Find previous PnL on this ticket (if any).
            if len(tk.future_pnls) >= 2:
                _pidx, _pts, prev_pnl = tk.future_pnls[-2]
                if float(prev_pnl) >= self.bank_threshold_sol:
                    # Already crossed earlier - don't emit again.
                    emitted_for_mint.add(tk.ticket_id)
                    continue
            # else: this is the FIRST observation and already at/over threshold;
            #       still a crossing (from "not held" to held + over).

            # SYNCHRONOUS emission.
            event_ts = int(snap.ts_ms)
            cur_state_dict = {
                "virtual_sol_reserves": int(cur_state.virtual_sol_reserves),
                "virtual_token_reserves": int(cur_state.virtual_token_reserves),
                "real_token_reserves": int(cur_state.real_token_reserves),
                "fee_bps": int(cur_state.fee_bps),
                "creator_fee_bps": int(cur_state.creator_fee_bps),
                "complete": bool(getattr(cur_state, "complete", False)),
            }
            ev = V42JBankEvent(
                mint=mint,
                event_ts_ms=event_ts,
                curve_update_slot=int(slot),
                triggering_ticket_id=str(tk.ticket_id),
                triggering_ticket_buy_ts_ms=int(tk.buy_snapshot_ts_ms),
                triggering_ticket_tokens_raw=int(tk.buy_tokens_raw),
                bank_pnl=float(latest_pnl),
                bank_threshold=float(self.bank_threshold_sol),
                current_curve_state=cur_state_dict,
                current_local_buy_quote_tokens_raw=int(cur_buy_tokens),
                current_local_sell_quote_sol=float(cur_sell_sol),
                source="accountSubscribe",
                event_fresh_until_ms=event_ts + int(self.ttl_ms),
            )
            events.append(ev)
            emitted_for_mint.add(tk.ticket_id)
            self.stats["events_emitted"] += 1
            # Maintain a recent-events log per mint (bounded).
            rec = self._recent_events.setdefault(mint, [])
            rec.append(ev)
            if len(rec) > 64:
                del rec[: len(rec) - 64]
            try:
                self._log(
                    f"PGG2-V42J-BANK-EVENT mint={_short(mint)} "
                    f"ts={ev.event_ts_ms} slot={ev.curve_update_slot} "
                    f"ttid={ev.triggering_ticket_id} "
                    f"ttbuy_ts={ev.triggering_ticket_buy_ts_ms} "
                    f"tokens={ev.triggering_ticket_tokens_raw} "
                    f"pnl={ev.bank_pnl:+.9f} thr={ev.bank_threshold:+.9f} "
                    f"fresh_until={ev.event_fresh_until_ms}"
                )
            except Exception:
                pass
        return events

    def recent_events(self, mint: str, since_ts_ms: int) -> List[V42JBankEvent]:
        """Return events for mint with event_ts_ms >= since_ts_ms (causal-safe)."""
        return [
            e for e in self._recent_events.get(mint, [])
            if int(e.event_ts_ms) >= int(since_ts_ms)
        ]

    def count_events_in_window(
        self, mint: str, ts_ms_now: int, window_ms: int
    ) -> int:
        cutoff = int(ts_ms_now) - int(window_ms)
        return sum(
            1 for e in self._recent_events.get(mint, [])
            if int(e.event_ts_ms) >= cutoff and int(e.event_ts_ms) <= int(ts_ms_now)
        )

    def newest_event(self, mint: str, ts_ms_now: int) -> Optional[V42JBankEvent]:
        evs = [
            e for e in self._recent_events.get(mint, [])
            if int(e.event_ts_ms) <= int(ts_ms_now)
        ]
        if not evs:
            return None
        return max(evs, key=lambda e: int(e.event_ts_ms))


__all__ = [
    "BANK_THRESHOLD_SOL",
    "FRESHNESS_TTL_MS_DEFAULT",
    "V42JEmissionContextError",
    "V42JBankEvent",
    "BankEventInterruptEmitter",
]
