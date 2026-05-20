"""V42I Phase 1 — Active-ticket state tracker.

V42H/V42HSAFE entered AFTER virtual banks completed: by then the runner
had already cooled (8/10 SAFE losses confirm this). V42I's middle-ground
hypothesis is to enter DURING the second virtual ticket's open run — after
one bank has proved the mint can move, but BEFORE waiting for the next
bank completion event (which is where the cooled top happens).

This module is a NON-MODIFYING wrapper around
`pgg2_v42h_local_ticket_engine.LocalCurveQuoteVirtualTicketEngine`. It
reads the engine's per-mint state and synthesises an "active ticket"
view at each accountSubscribe update.

DEFINITION of the active ticket:
    The most-recently-opened ticket on this mint that has NOT yet closed
    (outcome=="open"). The V42H engine spawns one ticket per snapshot —
    so at any time there are typically several open tickets (most recent
    is the freshest "active wave"). We pick the latest open ticket
    (highest open_emit_ts_ms / buy_snapshot_ts_ms) and compute its
    forward state from its own future_pnls trace.

Per-mint state fields (computed at each curve update):
    completed_virtual_banks_last_3000ms
    completed_virtual_losses_last_3000ms
    latest_completed_bank_pnl
    latest_completed_bank_time_ms     (completion ts ms-since-epoch)
    active_ticket_id
    active_ticket_age_ms              (now - buy_snapshot_ts_ms of active)
    active_ticket_current_pnl         (latest future_pnl entry, SOL)
    active_ticket_pnl_gradient        (last2 of future_pnls SOL delta)
    active_ticket_max_adverse         (<=0; most-negative observed)
    active_ticket_is_positive         (current_pnl >= +0.00005)
    active_ticket_is_improving        (gradient > 0)
    active_ticket_distance_to_bank    (0.00060 - current_pnl, clipped >=0)
    latest_curve_delta
    latest_local_quote_gradient

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple


# ------------- static-grep self-check (no-send fail-closed) ----------
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
        sys.stderr.write(f"V42I-ACTIVE-TICKET-STATE-ABORT forbidden_call_pattern={_pat}\n")
        raise RuntimeError("forbidden_call_pattern_in_v42i_active_ticket_state")


# ------------- thresholds (mirror engine + V42I rules) ----------------
BANK_THRESHOLD_SOL = 0.00060
POSITIVE_THRESHOLD_SOL = 0.00005
WINDOW_3000_MS = 3000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


@dataclass
class _PerMintTrace:
    """Per-mint trace of active-ticket snapshots, for diagnostics."""
    mint: str
    snaps: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=64)
    )


class ActiveTicketStateTracker:
    """Wrapper around LocalCurveQuoteVirtualTicketEngine. Does NOT modify it.

    Usage:
        tracker = ActiveTicketStateTracker(engine=engine, logger=log)
        # after engine.ingest_snapshot(mint, snap):
        tracker.ingest_curve_update(mint, snap, snap.ts_ms)
        state = tracker.get_state(mint, snap.ts_ms)
    """

    def __init__(
        self,
        engine: Any,
        logger: Optional[Any] = None,
    ) -> None:
        self._engine = engine
        self._log = logger or (lambda _m: None)
        self._traces: Dict[str, _PerMintTrace] = {}

    # ----- public API ----------------------------------------------------

    def ingest_curve_update(
        self,
        mint: str,
        snap: Any,
        ts_ms: int,
    ) -> Dict[str, Any]:
        """Read engine state for this mint, snapshot the active-ticket view.

        Returns the state dict. Also records a per-mint trace entry for
        get_active_ticket_history. The engine itself is NOT modified."""
        if not mint:
            return self._empty_state(mint, int(ts_ms))
        state = self._compute_state(mint, int(ts_ms))
        tr = self._traces.get(mint)
        if tr is None:
            tr = _PerMintTrace(mint=mint)
            self._traces[mint] = tr
        tr.snaps.append({
            "ts_ms": int(ts_ms),
            "active_ticket_id": state["active_ticket_id"],
            "active_ticket_age_ms": state["active_ticket_age_ms"],
            "active_ticket_current_pnl": state["active_ticket_current_pnl"],
            "active_ticket_pnl_gradient": state["active_ticket_pnl_gradient"],
            "active_ticket_max_adverse": state["active_ticket_max_adverse"],
            "completed_virtual_banks_last_3000ms":
                state["completed_virtual_banks_last_3000ms"],
            "latest_curve_delta": state["latest_curve_delta"],
            "latest_local_quote_gradient": state["latest_local_quote_gradient"],
        })
        # Emit log line (caller-suppressible).
        self._emit_log(state)
        return state

    def get_state(self, mint: str, ts_ms_now: int) -> Dict[str, Any]:
        return self._compute_state(mint, int(ts_ms_now))

    def get_active_ticket_history(self, mint: str) -> List[Dict[str, Any]]:
        tr = self._traces.get(mint)
        return list(tr.snaps) if tr is not None else []

    # ----- core compute --------------------------------------------------

    def _compute_state(self, mint: str, ts_ms_now: int) -> Dict[str, Any]:
        """Causally-safe state at ts_ms_now (no future tickets/snaps used)."""
        out = self._empty_state(mint, ts_ms_now)

        mst = None
        try:
            mst = self._engine.mint_state(mint)
        except Exception:
            mst = None
        if mst is None:
            return out

        tickets = list(getattr(mst, "tickets", []) or [])

        # Banks/losses in last 3000ms (causally up to ts_ms_now)
        banks_3k: List[Any] = []
        losses_3k: List[Any] = []
        completed_banks: List[Any] = []  # all banks with outcome_ts <= ts_now
        for t in tickets:
            outc = getattr(t, "outcome", "open")
            outc_ts = getattr(t, "outcome_ts_ms", None)
            if outc_ts is None:
                continue
            if int(outc_ts) > int(ts_ms_now):
                continue
            if outc == "virtual_bank_win":
                completed_banks.append(t)
                if (int(ts_ms_now) - int(outc_ts)) <= WINDOW_3000_MS:
                    banks_3k.append(t)
            elif outc == "virtual_loss":
                if (int(ts_ms_now) - int(outc_ts)) <= WINDOW_3000_MS:
                    losses_3k.append(t)

        out["completed_virtual_banks_last_3000ms"] = len(banks_3k)
        out["completed_virtual_losses_last_3000ms"] = len(losses_3k)

        latest_bank = None
        if completed_banks:
            completed_banks.sort(
                key=lambda t: int(getattr(t, "outcome_ts_ms", 0) or 0)
            )
            latest_bank = completed_banks[-1]
            out["latest_completed_bank_pnl"] = float(
                getattr(latest_bank, "bank_pnl_sol", 0.0) or 0.0
            )
            out["latest_completed_bank_time_ms"] = int(
                getattr(latest_bank, "outcome_ts_ms", 0) or 0
            )

        # First completed bank time + first bank's time-to-completion
        # (open_ts -> bank_ts) — for v42i_fast_second_wave rule.
        if completed_banks:
            first_bank = completed_banks[0]
            out["first_completed_bank_time_ms"] = int(
                getattr(first_bank, "outcome_ts_ms", 0) or 0
            )
            buy_ts = int(getattr(first_bank, "buy_snapshot_ts_ms", 0) or 0)
            bank_ts = int(getattr(first_bank, "outcome_ts_ms", 0) or 0)
            if buy_ts > 0 and bank_ts >= buy_ts:
                out["first_bank_time_to_completion_ms"] = bank_ts - buy_ts
            else:
                out["first_bank_time_to_completion_ms"] = None

        # Active ticket: latest open, causally observed up to ts_ms_now.
        active = self._pick_active_ticket(tickets, ts_ms_now)
        if active is not None:
            buy_ts = int(getattr(active, "buy_snapshot_ts_ms", 0) or 0)
            out["active_ticket_id"] = str(
                getattr(active, "ticket_id", "")
            )
            age_ms = max(0, int(ts_ms_now) - buy_ts)
            out["active_ticket_age_ms"] = age_ms

            # future_pnls = list of (chain_idx, future_ts_ms, future_pnl_sol)
            fpnls = list(getattr(active, "future_pnls", []) or [])
            # Causal filter: only future_pnls with ts <= ts_ms_now
            fpnls_causal = [
                (idx, fts, fpn) for (idx, fts, fpn) in fpnls
                if int(fts) <= int(ts_ms_now)
            ]
            if fpnls_causal:
                cur_pnl = float(fpnls_causal[-1][2])
                out["active_ticket_current_pnl"] = cur_pnl
                if len(fpnls_causal) >= 2:
                    prev_pnl = float(fpnls_causal[-2][2])
                    out["active_ticket_pnl_gradient"] = cur_pnl - prev_pnl
                else:
                    out["active_ticket_pnl_gradient"] = 0.0
                # max_adverse = min of pnl-trace (clipped at 0, must be <=0)
                m = min(float(fpn) for (_i, _t, fpn) in fpnls_causal)
                out["active_ticket_max_adverse"] = m if m < 0 else 0.0
                out["active_ticket_is_positive"] = (
                    cur_pnl >= POSITIVE_THRESHOLD_SOL
                )
                out["active_ticket_is_improving"] = (
                    out["active_ticket_pnl_gradient"] > 0.0
                )
                out["active_ticket_distance_to_bank"] = max(
                    0.0, BANK_THRESHOLD_SOL - cur_pnl
                )
            else:
                # Active ticket has no observation yet -> "not yet visible".
                # We DO NOT set positive/improving=True in this state.
                out["active_ticket_current_pnl"] = 0.0
                out["active_ticket_pnl_gradient"] = 0.0
                out["active_ticket_max_adverse"] = 0.0
                out["active_ticket_is_positive"] = False
                out["active_ticket_is_improving"] = False
                out["active_ticket_distance_to_bank"] = BANK_THRESHOLD_SOL

            # active-ticket open time relative to first completed bank
            if (
                out["first_completed_bank_time_ms"] is not None
                and buy_ts > 0
            ):
                # buy_ts is when the ticket "buy" snapshot was taken; that's
                # essentially when this ticket opened.
                gap = buy_ts - int(out["first_completed_bank_time_ms"])
                out["active_ticket_open_after_first_bank_ms"] = gap

            # break-even sell-out for THIS active ticket (open-snap basis):
            # amount_sol + 2*tx_fee_sol — but we work in PnL space here.
            # The break-even check is done in entry_block (compares
            # current_quote_sol vs break-even). We do not duplicate it here.

        # Rolling curve / quote deltas from engine state.
        sq = getattr(mst, "sell_out_sol_seq", None)
        if sq is not None and len(sq) >= 2:
            pts = list(sq)
            (_t1, v1), (_t2, v2) = pts[-2], pts[-1]
            out["latest_local_quote_gradient"] = float(v2) - float(v1)
        cp = getattr(mst, "curve_price_seq", None)
        if cp is not None and len(cp) >= 2:
            pts = list(cp)
            (_t1, p1), (_t2, p2) = pts[-2], pts[-1]
            out["latest_curve_delta"] = float(p2) - float(p1)

        # negative-curve-after-bank flag (used by entry block).
        last_neg_curve_ts = getattr(
            mst, "last_negative_curve_update_ts_ms", None
        )
        if (
            last_neg_curve_ts is not None
            and out["latest_completed_bank_time_ms"] is not None
            and int(last_neg_curve_ts) > int(out["latest_completed_bank_time_ms"])
            and int(last_neg_curve_ts) <= int(ts_ms_now)
        ):
            out["negative_curve_after_latest_bank"] = True
        else:
            out["negative_curve_after_latest_bank"] = False

        # virtual_losses after latest bank (count). Causal up to ts_now.
        if out["latest_completed_bank_time_ms"] is not None:
            cnt = 0
            for t in tickets:
                if getattr(t, "outcome", "") != "virtual_loss":
                    continue
                outc_ts = getattr(t, "outcome_ts_ms", None)
                if outc_ts is None or int(outc_ts) > int(ts_ms_now):
                    continue
                if int(outc_ts) > int(out["latest_completed_bank_time_ms"]):
                    cnt += 1
            out["completed_virtual_losses_after_latest_bank"] = cnt
        else:
            out["completed_virtual_losses_after_latest_bank"] = 0

        return out

    def _pick_active_ticket(
        self,
        tickets: List[Any],
        ts_ms_now: int,
    ) -> Optional[Any]:
        """Latest open ticket whose buy_snapshot_ts_ms <= ts_ms_now."""
        open_tickets: List[Tuple[int, Any]] = []
        for t in tickets:
            if getattr(t, "closed", False):
                continue
            if getattr(t, "outcome", "open") != "open":
                continue
            buy_ts = int(getattr(t, "buy_snapshot_ts_ms", 0) or 0)
            if buy_ts > int(ts_ms_now):
                continue
            # require at least open_emit_ts <= ts_now (we already have buy_ts
            # <= ts_now; that's the strict causal condition).
            open_tickets.append((buy_ts, t))
        if not open_tickets:
            return None
        open_tickets.sort(key=lambda x: x[0])
        return open_tickets[-1][1]

    def _empty_state(self, mint: str, ts_ms_now: int) -> Dict[str, Any]:
        return {
            "mint": str(mint or ""),
            "ts_ms_now": int(ts_ms_now),
            "completed_virtual_banks_last_3000ms": 0,
            "completed_virtual_losses_last_3000ms": 0,
            "latest_completed_bank_pnl": None,
            "latest_completed_bank_time_ms": None,
            "first_completed_bank_time_ms": None,
            "first_bank_time_to_completion_ms": None,
            "active_ticket_id": None,
            "active_ticket_age_ms": None,
            "active_ticket_current_pnl": None,
            "active_ticket_pnl_gradient": None,
            "active_ticket_max_adverse": None,
            "active_ticket_is_positive": False,
            "active_ticket_is_improving": False,
            "active_ticket_distance_to_bank": None,
            "active_ticket_open_after_first_bank_ms": None,
            "latest_curve_delta": 0.0,
            "latest_local_quote_gradient": 0.0,
            "negative_curve_after_latest_bank": False,
            "completed_virtual_losses_after_latest_bank": 0,
        }

    # ----- emit ----------------------------------------------------------

    def _emit_log(self, st: Dict[str, Any]) -> None:
        try:
            atid = st["active_ticket_id"]
            atid_short = atid[:8] if atid else "None"
            self._log(
                f"PGG2-V42I-ACTIVE-TICKET-STATE mint={_short(st['mint'])} "
                f"cb3k={st['completed_virtual_banks_last_3000ms']} "
                f"cl3k={st['completed_virtual_losses_last_3000ms']} "
                f"lcbpnl="
                f"{(st['latest_completed_bank_pnl'] or 0.0):+.6f} "
                f"lcbtm={st['latest_completed_bank_time_ms'] or 0} "
                f"atid={atid_short} "
                f"ata={st['active_ticket_age_ms']} "
                f"atpnl="
                f"{(st['active_ticket_current_pnl'] or 0.0):+.6f} "
                f"atpgr="
                f"{(st['active_ticket_pnl_gradient'] or 0.0):+.6f} "
                f"atadv="
                f"{(st['active_ticket_max_adverse'] or 0.0):+.6f} "
                f"atpos={bool(st['active_ticket_is_positive'])} "
                f"atimpr={bool(st['active_ticket_is_improving'])} "
                f"atdist="
                f"{(st['active_ticket_distance_to_bank'] or 0.0):+.6f} "
                f"cdelta={st['latest_curve_delta']:+.9f} "
                f"lqgr={st['latest_local_quote_gradient']:+.9f}"
            )
        except Exception:
            pass


__all__ = [
    "ActiveTicketStateTracker",
    "BANK_THRESHOLD_SOL",
    "POSITIVE_THRESHOLD_SOL",
]
