"""V42H Phase 3 — AccountSubscribe-driven virtual ticket engine.

Strategic correction over V42G's ticket engine:

    V42G opened virtual tickets only when V42E QuoteConfirmationWatchlist
    drove a broker-side quote snapshot. That broker call took 150-500ms
    (network RTT + processing). By the time the next snapshot arrived,
    the runner had already retraced — so V42G saw 38 multi-wave runners,
    fired 164 rules, but late_entry_blocker rejected 100% of them
    because `current_quote_below_last_bank_quote`.

V42H opens a virtual ticket on EVERY accountSubscribe curve update,
computing buy/sell quotes LOCALLY from (vsol, vtok). No broker RTT.
Cadence ≈ slot cadence ~400ms but unlimited per-mint (no broker throttle).

CAUSALITY (unchanged from V42G):
    A ticket opened at update N (curve_state_N) is observed only at updates
    N+1, N+2, ... The future-PnL is computed via local_roundtrip_label
    (buy at N's curve_state, sell at N+k's curve_state). Future updates are
    pure observations — never used as features.

Pure arithmetic. NO transactions. NO network. Static-grep enforces this.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from pgg2_v42h_local_curve_quote import (
    V42HCurveState,
    local_buy_quote_tokens_raw,
    local_sell_quote_sol,
    local_roundtrip_label,
    local_live_equiv_pnl,
    LAMPORTS_PER_SOL,
    DEFAULT_TX_FEE_SOL,
    maybe_emit_quote_log,
)

# Reference token amount for the "price gradient" sample (so deltas reflect
# curve direction, not our 0.015 SOL reflexive roundtrip which is curve-scale
# invariant). 1B raw tokens = ~1000 UI tokens (pump 6 decimals).
SAMPLE_TOKENS_RAW_FOR_GRADIENT = 1_000_000_000


# Defaults aligned with data/v42h_local_runner_rules.json exit_policy.
DEFAULT_BANK_PNL_SOL = 0.00060
DEFAULT_SCRATCH_PNL_SOL = 0.00005
DEFAULT_CLAMP_LOSS_SOL = -0.00050
DEFAULT_MAX_HOLD_MS = 2500
DEFAULT_CHAIN_DEPTH = 12


class LookaheadViolation(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _new_ticket_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class LocalCurveSnapshot:
    """A single accountSubscribe-derived snapshot.

    All fields are derived locally — no broker RTT — from the curve account
    bytes. Quote math uses (vsol, vtok, fee_bps, creator_fee_bps).
    """
    ts_ms: int
    slot: int
    curve_state: V42HCurveState
    buy_tokens_raw: int                 # what 0.015 SOL would buy at this state
    sell_quote_out_lamports: int        # what the just-bought tokens sell for (round trip)
    live_equiv_pnl_sol: float           # single-snapshot PnL (diagnostic)
    curve_price: float                  # vsol/vtok
    route: str = "pump_bc"
    sim_needed: int = 0
    pair_source: str = "accountSubscribe"
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LocalVirtualTicket:
    ticket_id: str
    mint: str
    buy_snapshot_ts_ms: int
    open_emit_ts_ms: int
    amount_sol: float
    buy_curve_state: V42HCurveState
    buy_tokens_raw: int
    same_snapshot_pnl_sol: float
    future_pnls: List[Tuple[int, int, float]] = field(default_factory=list)
    # (chain_idx, future_ts_ms, future_pnl_sol)
    first_observation_ts_ms: Optional[int] = None
    outcome: str = "open"       # open|virtual_bank_win|virtual_scratch|virtual_loss|expired
    outcome_ts_ms: Optional[int] = None
    outcome_reason: str = ""
    bank_time_ms: Optional[int] = None
    bank_pnl_sol: Optional[float] = None
    bank_sell_out_sol: Optional[float] = None
    max_adverse_before_bank: float = 0.0
    max_favorable: float = 0.0
    closed: bool = False


@dataclass
class _LocalMintState:
    mint: str
    snapshots: Deque[LocalCurveSnapshot]
    tickets: List[LocalVirtualTicket] = field(default_factory=list)
    by_id: Dict[str, LocalVirtualTicket] = field(default_factory=dict)
    # Rolling deltas for rule eval. Each is a deque of (ts_ms, value).
    sell_out_sol_seq: Deque[Tuple[int, float]] = field(default_factory=lambda: deque(maxlen=16))
    curve_price_seq: Deque[Tuple[int, float]] = field(default_factory=lambda: deque(maxlen=16))
    last_negative_curve_update_ts_ms: Optional[int] = None
    last_negative_quote_update_ts_ms: Optional[int] = None


class LocalCurveQuoteVirtualTicketEngine:
    """One ticket per accountSubscribe curve update, LOCAL quote math, strict causality."""

    def __init__(
        self,
        amount_sol: float = 0.015,
        chain_depth: int = DEFAULT_CHAIN_DEPTH,
        max_hold_ms: int = DEFAULT_MAX_HOLD_MS,
        bank_pnl_sol: float = DEFAULT_BANK_PNL_SOL,
        scratch_pnl_sol: float = DEFAULT_SCRATCH_PNL_SOL,
        clamp_loss_sol: float = DEFAULT_CLAMP_LOSS_SOL,
        tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.amount_sol = float(amount_sol)
        self.chain_depth = int(chain_depth)
        self.max_hold_ms = int(max_hold_ms)
        self.bank_pnl_sol = float(bank_pnl_sol)
        self.scratch_pnl_sol = float(scratch_pnl_sol)
        self.clamp_loss_sol = float(clamp_loss_sol)
        self.tx_fee_sol = float(tx_fee_sol)
        self._log = logger or (lambda _m: None)
        self._mints: Dict[str, _LocalMintState] = {}
        self.stats: Dict[str, int] = {
            "snapshots_ingested": 0,
            "tickets_opened": 0,
            "tickets_banked": 0,
            "tickets_scratched": 0,
            "tickets_lost": 0,
            "tickets_expired": 0,
            "tickets_still_open": 0,
            "lookahead_blocks": 0,
        }

    def mint_state(self, mint: str) -> Optional[_LocalMintState]:
        return self._mints.get(mint)

    def tickets(self, mint: str) -> List[LocalVirtualTicket]:
        st = self._mints.get(mint)
        return list(st.tickets) if st is not None else []

    def open_tickets(self, mint: str) -> List[LocalVirtualTicket]:
        return [t for t in self.tickets(mint) if not t.closed]

    def get_ticket(self, ticket_id: str) -> Optional[LocalVirtualTicket]:
        for st in self._mints.values():
            t = st.by_id.get(ticket_id)
            if t is not None:
                return t
        return None

    def wait_until_observed(
        self, ticket_id: str, now_ms: Optional[int] = None
    ) -> LocalVirtualTicket:
        t = self.get_ticket(ticket_id)
        if t is None:
            self.stats["lookahead_blocks"] += 1
            raise LookaheadViolation(f"ticket_not_found:{ticket_id}")
        n = now_ms if now_ms is not None else _now_ms()
        if t.first_observation_ts_ms is None:
            self.stats["lookahead_blocks"] += 1
            raise LookaheadViolation(
                f"ticket={t.ticket_id} mint={_short(t.mint)} not_yet_observed "
                f"buy_ts={t.buy_snapshot_ts_ms} now={n}"
            )
        if t.first_observation_ts_ms > n:
            self.stats["lookahead_blocks"] += 1
            raise LookaheadViolation(
                f"ticket={t.ticket_id} obs_ts={t.first_observation_ts_ms} > now={n}"
            )
        return t

    def build_snapshot_from_curve(
        self,
        ts_ms: int,
        slot: int,
        curve_state: V42HCurveState,
    ) -> LocalCurveSnapshot:
        """Construct one snapshot from a curve_state using LOCAL math only."""
        buy_tokens, _buy_fee = local_buy_quote_tokens_raw(curve_state, self.amount_sol)
        if buy_tokens > 0:
            sell_lamports, _sell_fee = local_sell_quote_sol(curve_state, buy_tokens)
        else:
            sell_lamports = 0
        live_equiv = local_live_equiv_pnl(curve_state, self.amount_sol, self.tx_fee_sol)
        cp = (
            float(curve_state.virtual_sol_reserves)
            / max(1.0, float(curve_state.virtual_token_reserves))
        )
        return LocalCurveSnapshot(
            ts_ms=int(ts_ms),
            slot=int(slot),
            curve_state=curve_state,
            buy_tokens_raw=int(buy_tokens),
            sell_quote_out_lamports=int(sell_lamports),
            live_equiv_pnl_sol=float(live_equiv),
            curve_price=float(cp),
        )

    def ingest_snapshot(
        self,
        mint: str,
        snap: LocalCurveSnapshot,
    ) -> List[LocalVirtualTicket]:
        """Update open tickets with this new observation + open one new ticket
        using the previous snapshot's curve_state. Strict causality."""
        if not mint or snap is None:
            return []
        st = self._mints.get(mint)
        if st is None:
            st = _LocalMintState(
                mint=mint,
                snapshots=deque(maxlen=max(2, self.chain_depth)),
            )
            self._mints[mint] = st
        prev_snap = st.snapshots[-1] if st.snapshots else None
        st.snapshots.append(snap)
        self.stats["snapshots_ingested"] += 1

        # Update rolling sequences (for rules: gradient detection). Use a
        # FIXED reference token amount so curve direction is visible in the
        # delta (our reflexive 0.015-SOL round-trip is curve-scale invariant
        # and would mask the signal).
        ref_sell_lamports, _ref_fee = local_sell_quote_sol(
            snap.curve_state, SAMPLE_TOKENS_RAW_FOR_GRADIENT
        )
        sample_sell_sol = float(ref_sell_lamports) / LAMPORTS_PER_SOL
        st.sell_out_sol_seq.append((snap.ts_ms, sample_sell_sol))
        if snap.curve_price > 0:
            st.curve_price_seq.append((snap.ts_ms, snap.curve_price))

        # Track negative curve / sell-quote updates.
        if prev_snap is not None:
            if snap.curve_price > 0 and prev_snap.curve_price > 0 and snap.curve_price < prev_snap.curve_price:
                st.last_negative_curve_update_ts_ms = snap.ts_ms
            if len(st.sell_out_sol_seq) >= 2:
                (_t_prev, prev_sample), (_t_cur, cur_sample) = list(st.sell_out_sol_seq)[-2:]
                if cur_sample < prev_sample:
                    st.last_negative_quote_update_ts_ms = snap.ts_ms

        newly_closed: List[LocalVirtualTicket] = []

        # 1) Update open tickets with future observation @ this snap.
        for tk in st.tickets:
            if tk.closed:
                continue
            if snap.ts_ms <= tk.buy_snapshot_ts_ms:
                continue
            future_pnl = local_roundtrip_label(
                tk.buy_curve_state, snap.curve_state, tk.amount_sol, self.tx_fee_sol
            )
            tk.future_pnls.append(
                (len(tk.future_pnls) + 1, snap.ts_ms, float(future_pnl))
            )
            if tk.first_observation_ts_ms is None:
                tk.first_observation_ts_ms = snap.ts_ms
            if future_pnl > tk.max_favorable:
                tk.max_favorable = float(future_pnl)
            if future_pnl < tk.max_adverse_before_bank and tk.bank_time_ms is None:
                tk.max_adverse_before_bank = float(future_pnl)
            self._maybe_close_ticket(tk, snap)
            if tk.closed and tk.outcome != "open" and tk not in newly_closed:
                newly_closed.append(tk)

        # 2) Expire any open ticket past max_hold_ms.
        now = snap.ts_ms
        for tk in st.tickets:
            if tk.closed:
                continue
            if (now - tk.buy_snapshot_ts_ms) > self.max_hold_ms:
                tk.outcome = "expired"
                tk.outcome_ts_ms = now
                tk.outcome_reason = "max_hold_exceeded"
                tk.closed = True
                self.stats["tickets_expired"] += 1
                self._emit_expire(tk)
                if tk not in newly_closed:
                    newly_closed.append(tk)

        # 3) Open a NEW ticket using the PREVIOUS snapshot's curve_state.
        if prev_snap is not None:
            new_ticket = LocalVirtualTicket(
                ticket_id=_new_ticket_id(),
                mint=mint,
                buy_snapshot_ts_ms=int(prev_snap.ts_ms),
                open_emit_ts_ms=int(snap.ts_ms),
                amount_sol=self.amount_sol,
                buy_curve_state=prev_snap.curve_state,
                buy_tokens_raw=int(prev_snap.buy_tokens_raw),
                same_snapshot_pnl_sol=float(prev_snap.live_equiv_pnl_sol),
            )
            st.tickets.append(new_ticket)
            st.by_id[new_ticket.ticket_id] = new_ticket
            self.stats["tickets_opened"] += 1
            self._emit_open(new_ticket)
            # Immediate first observation = current snap (strictly after prev).
            future_pnl = local_roundtrip_label(
                new_ticket.buy_curve_state, snap.curve_state,
                new_ticket.amount_sol, self.tx_fee_sol,
            )
            new_ticket.future_pnls.append((1, snap.ts_ms, float(future_pnl)))
            new_ticket.first_observation_ts_ms = snap.ts_ms
            if future_pnl > new_ticket.max_favorable:
                new_ticket.max_favorable = float(future_pnl)
            if future_pnl < new_ticket.max_adverse_before_bank:
                new_ticket.max_adverse_before_bank = float(future_pnl)
            self._maybe_close_ticket(new_ticket, snap)
            if new_ticket.closed and new_ticket.outcome != "open":
                newly_closed.append(new_ticket)

        # Sample-emit a local quote line.
        maybe_emit_quote_log(
            self._log,
            _short(mint),
            int(snap.buy_tokens_raw),
            float(snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL,
            float(snap.live_equiv_pnl_sol),
        )

        self.stats["tickets_still_open"] = sum(
            1 for s2 in self._mints.values() for t in s2.tickets if not t.closed
        )
        return newly_closed

    def _maybe_close_ticket(
        self, tk: LocalVirtualTicket, snap: LocalCurveSnapshot
    ) -> None:
        if tk.closed:
            return
        if not tk.future_pnls:
            return
        latest_pnl = tk.future_pnls[-1][2]
        if latest_pnl >= self.bank_pnl_sol and tk.bank_time_ms is None:
            tk.outcome = "virtual_bank_win"
            tk.outcome_ts_ms = snap.ts_ms
            tk.outcome_reason = "bank_threshold_crossed"
            tk.bank_time_ms = int(snap.ts_ms - tk.buy_snapshot_ts_ms)
            tk.bank_pnl_sol = float(latest_pnl)
            tk.bank_sell_out_sol = (
                float(snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL
                if snap.sell_quote_out_lamports > 0
                else None
            )
            tk.closed = True
            self.stats["tickets_banked"] += 1
            self._emit_bank(tk)
            return
        if latest_pnl <= self.clamp_loss_sol:
            tk.outcome = "virtual_loss"
            tk.outcome_ts_ms = snap.ts_ms
            tk.outcome_reason = "clamp_loss"
            tk.closed = True
            self.stats["tickets_lost"] += 1
            self._emit_loss(tk)
            return
        if (
            len(tk.future_pnls) >= 2
            and tk.max_favorable >= self.scratch_pnl_sol
            and latest_pnl < tk.max_favorable
            and latest_pnl < self.bank_pnl_sol
            and latest_pnl < self.scratch_pnl_sol
        ):
            tk.outcome = "virtual_scratch"
            tk.outcome_ts_ms = snap.ts_ms
            tk.outcome_reason = "favorable_high_then_decline"
            tk.closed = True
            self.stats["tickets_scratched"] += 1
            self._emit_scratch(tk)

    # ----- emit helpers ----------------------------------------------

    def _emit_open(self, tk: LocalVirtualTicket) -> None:
        try:
            self._log(
                f"PGG2-V42H-LOCAL-TICKET-OPEN mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} buy_ts={tk.buy_snapshot_ts_ms} "
                f"buy_tokens_raw={tk.buy_tokens_raw} amount_sol={tk.amount_sol:.6f} "
                f"same_snap_pnl={tk.same_snapshot_pnl_sol:+.9f} route=pump_bc"
            )
        except Exception:
            pass

    def _emit_bank(self, tk: LocalVirtualTicket) -> None:
        try:
            self._log(
                f"PGG2-V42H-LOCAL-TICKET-BANK mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} bank_time_ms={tk.bank_time_ms} "
                f"bank_pnl_sol={tk.bank_pnl_sol:+.9f} "
                f"bank_sell_out_sol={tk.bank_sell_out_sol or 0.0:.9f} "
                f"max_adverse={tk.max_adverse_before_bank:+.9f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass

    def _emit_scratch(self, tk: LocalVirtualTicket) -> None:
        try:
            latest_pnl = tk.future_pnls[-1][2] if tk.future_pnls else 0.0
            self._log(
                f"PGG2-V42H-LOCAL-TICKET-SCRATCH mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} reason={tk.outcome_reason} "
                f"max_favorable={tk.max_favorable:+.9f} latest_pnl={latest_pnl:+.9f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass

    def _emit_loss(self, tk: LocalVirtualTicket) -> None:
        try:
            latest_pnl = tk.future_pnls[-1][2] if tk.future_pnls else 0.0
            self._log(
                f"PGG2-V42H-LOCAL-TICKET-LOSS mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} reason={tk.outcome_reason} "
                f"latest_pnl={latest_pnl:+.9f} obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass

    def _emit_expire(self, tk: LocalVirtualTicket) -> None:
        try:
            self._log(
                f"PGG2-V42H-LOCAL-TICKET-EXPIRE mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} max_favorable={tk.max_favorable:+.9f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass


__all__ = [
    "LocalCurveQuoteVirtualTicketEngine",
    "LocalCurveSnapshot",
    "LocalVirtualTicket",
    "LookaheadViolation",
    "DEFAULT_BANK_PNL_SOL",
    "DEFAULT_SCRATCH_PNL_SOL",
    "DEFAULT_CLAMP_LOSS_SOL",
    "DEFAULT_MAX_HOLD_MS",
]
