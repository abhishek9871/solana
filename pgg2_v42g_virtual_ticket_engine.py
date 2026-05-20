"""V42G — Virtual-Ticket Runner Confirmation Engine.

Strategic correction over V42E (single-snapshot round-trip) and V42F (per-mint
causal prediction):

    V42E proved that single-snapshot pnl on a static curve is mathematically
    negative for pump_bc (2% fee + curve impact = -0.0004 SOL min loss).
    V42F proved per-mint *prediction* of the next snapshot from snap-N features
    alone is not statistically separable.

V42G observes virtual ticket OUTCOMES across snapshot pairs and aggregates the
pattern. A `VirtualTicket` is opened at snapshot N-1 with the buy quote taken
from snapshot N-1's `buy_quote_tokens`. The ticket's outcomes are evaluated at
snapshots N, N+1, N+2, ... (each new snapshot = one new observation point).

CAUSALITY GUARANTEE
    A ticket whose ID is `t` and whose buy_snapshot_ts is `ts_buy` cannot be
    "consumed" (used for a real entry decision) until at least one observation
    snapshot AFTER `ts_buy` has been ingested. Calling `wait_until_observed` on
    a ticket whose `first_observation_ts` is None raises `LookaheadViolation`.

This module ONLY computes virtual ticket bookkeeping. NO transactions, NO
signing, NO network. Pure observation arithmetic on quote snapshots.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple


LAMPORTS_PER_SOL = 1_000_000_000

# Exit-policy thresholds (matched to v42g_runner_rules.json).
BANK_PNL_SOL = 0.00060
SCRATCH_PNL_SOL = 0.00005
CLAMP_LOSS_SOL = -0.00050
MAX_HOLD_MS = 2500

# Rolling chain depth per mint.
DEFAULT_CHAIN_DEPTH = 8

# Default tx fee (used in same_snapshot_pnl reconstruction). The future PnL
# values stored on the ticket are LIVE-EQUIV PnL values already net of all
# trading fees (computed externally and passed in via QuoteSnapshot.all_in_pnl)
# so they need no extra adjustment.
DEFAULT_TX_FEE_SOL = 0.000010


class LookaheadViolation(RuntimeError):
    """Raised when an entry path tries to consume a virtual ticket whose
    outcome has not yet been observed at the entry's decision_ts."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _new_ticket_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class QuoteSnapshotLite:
    """A snapshot row used by the virtual-ticket engine.

    Keep this struct small and protocol-agnostic so it works whether the
    feeder is V42E QuoteConfirmationWatchlist or the bot's lead-snapshot
    capture.
    """
    snapshot_ts_ms: int            # broker-side decision ts
    buy_quote_tokens: int          # tokens we would buy with amount_sol
    sell_quote_out_lamports: int   # round-trip sell of buy_quote_tokens at this curve
    all_in_pnl_sol: float          # live-equiv same-snapshot pnl, already net of fees
    route: str = "pump_bc"
    sim_needed: int = 0
    pair_source: str = "current_sig"
    cost_model_confidence: str = "proven"
    accountSubscribe_curve_price: float = 0.0
    fresh_quote: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VirtualTicket:
    """One observed virtual ticket.

    The ticket is OPENED at snapshot N with `buy_snapshot_ts = snap_N.ts`. Its
    future PnL chain is filled in as snapshots N+1, N+2, ... arrive. The
    ticket's `outcome` and `outcome_ts` flip the FIRST time the exit policy
    decides bank / scratch / loss; the ticket is `closed`-but-not-removed and
    its outcome is then queryable.

    `first_observation_ts` is set to the timestamp of the FIRST future
    snapshot observation. Until it is set, `wait_until_observed` raises
    `LookaheadViolation`.
    """
    ticket_id: str
    mint: str
    buy_snapshot_ts: int           # snapshot N's ts
    open_emit_ts: int              # ts when ticket was created (= snapshot N's ts)
    amount_sol: float
    buy_tokens_raw: int            # tokens we would have bought at snapshot N
    buy_cost_sol: float            # amount_sol (the canonical cost)
    same_snapshot_pnl: float       # snap N's own all_in_pnl (V42E impossible-positive metric)
    future_pnls: List[Tuple[int, int, float]] = field(default_factory=list)
    # tuple: (future_snap_idx, future_snap_ts, future_pnl)
    first_observation_ts: Optional[int] = None
    outcome: str = "open"           # open | virtual_bank_win | virtual_scratch | virtual_loss | expired
    outcome_ts: Optional[int] = None
    outcome_reason: str = ""
    bank_time_ms: Optional[int] = None
    bank_pnl_sol: Optional[float] = None
    max_adverse_before_bank: float = 0.0
    max_favorable: float = 0.0
    last_observation_curve_price: float = 0.0
    closed: bool = False

    def has_observation(self) -> bool:
        return self.first_observation_ts is not None

    def age_ms(self, now_ms: Optional[int] = None) -> int:
        n = now_ms if now_ms is not None else _now_ms()
        return int(max(0, n - self.buy_snapshot_ts))


@dataclass
class _MintState:
    mint: str
    snapshots: Deque[QuoteSnapshotLite]
    tickets: List[VirtualTicket] = field(default_factory=list)
    # ticket lookup by id, retained for O(1) wait_until_observed.
    by_id: Dict[str, VirtualTicket] = field(default_factory=dict)
    # rolling deltas used by RunnerState
    last_quote_out_sol_sequence: Deque[float] = field(default_factory=lambda: deque(maxlen=8))
    last_curve_price_sequence: Deque[float] = field(default_factory=lambda: deque(maxlen=8))
    last_negative_curve_update_ts: Optional[int] = None


class VirtualTicketEngine:
    """Per-mint virtual ticket bookkeeping with strict causality.

    Usage:
        eng = VirtualTicketEngine(amount_sol=0.015, logger=print)
        eng.ingest_snapshot(mint, snap_struct)
        # Every NEW snapshot N+1 will:
        #  - update prior ticket(s) opened at snap_N (and earlier) with their
        #    next future_pnl_k observation.
        #  - open a NEW VirtualTicket whose buy_snapshot_ts = snap_N.ts (the
        #    *previous* snapshot's ts, i.e. the one we "could have bought at"
        #    just before snapshot N+1 was observed).
        #
        # Tickets are never opened on the very first snapshot of a mint
        # because there is no previous snapshot to use as the buy time.

    All log emits use the PGG2-V42G-* prefix.
    """

    def __init__(
        self,
        amount_sol: float = 0.015,
        chain_depth: int = DEFAULT_CHAIN_DEPTH,
        max_hold_ms: int = MAX_HOLD_MS,
        bank_pnl_sol: float = BANK_PNL_SOL,
        scratch_pnl_sol: float = SCRATCH_PNL_SOL,
        clamp_loss_sol: float = CLAMP_LOSS_SOL,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.amount_sol = float(amount_sol)
        self.chain_depth = int(chain_depth)
        self.max_hold_ms = int(max_hold_ms)
        self.bank_pnl_sol = float(bank_pnl_sol)
        self.scratch_pnl_sol = float(scratch_pnl_sol)
        self.clamp_loss_sol = float(clamp_loss_sol)
        self._log = logger or (lambda _msg: None)
        self._mints: Dict[str, _MintState] = {}
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

    # ----- public surface -----------------------------------------------

    def mint_state(self, mint: str) -> Optional[_MintState]:
        return self._mints.get(mint)

    def tickets(self, mint: str) -> List[VirtualTicket]:
        st = self._mints.get(mint)
        return list(st.tickets) if st is not None else []

    def open_tickets(self, mint: str) -> List[VirtualTicket]:
        return [t for t in self.tickets(mint) if not t.closed]

    def closed_tickets(self, mint: str) -> List[VirtualTicket]:
        return [t for t in self.tickets(mint) if t.closed]

    def get_ticket(self, ticket_id: str) -> Optional[VirtualTicket]:
        for st in self._mints.values():
            t = st.by_id.get(ticket_id)
            if t is not None:
                return t
        return None

    def wait_until_observed(self, ticket_id: str, now_ms: Optional[int] = None) -> VirtualTicket:
        """Causality gate. Returns the ticket only if AT LEAST ONE future
        observation snapshot has been ingested AFTER the ticket's
        buy_snapshot_ts. Otherwise raises LookaheadViolation.

        This is the API every entry path must call before consuming a ticket's
        outcome.
        """
        t = self.get_ticket(ticket_id)
        if t is None:
            self.stats["lookahead_blocks"] += 1
            raise LookaheadViolation(f"ticket_not_found:{ticket_id}")
        n = now_ms if now_ms is not None else _now_ms()
        if t.first_observation_ts is None:
            self.stats["lookahead_blocks"] += 1
            raise LookaheadViolation(
                f"ticket={t.ticket_id} mint={_short(t.mint)} not_yet_observed "
                f"buy_ts={t.buy_snapshot_ts} now={n}"
            )
        if t.first_observation_ts > n:
            # Defensive — caller's now_ms is earlier than the observation we
            # already have. Treat as not-yet-observed AT the requested ts.
            self.stats["lookahead_blocks"] += 1
            raise LookaheadViolation(
                f"ticket={t.ticket_id} obs_ts={t.first_observation_ts} > now={n}"
            )
        return t

    def ingest_snapshot(self, mint: str, snap: QuoteSnapshotLite) -> List[VirtualTicket]:
        """Ingest one new quote snapshot for `mint`.

        - Update all open tickets on this mint with a new future observation
          (and possibly close them per exit policy).
        - Open a new VirtualTicket using the PREVIOUS snapshot's quote (so the
          buy_snapshot_ts is strictly in the past relative to this new snap).

        Returns the list of newly closed tickets (so callers can react).
        """
        if not mint or snap is None:
            return []
        st = self._mints.get(mint)
        if st is None:
            st = _MintState(
                mint=mint,
                snapshots=deque(maxlen=max(2, self.chain_depth)),
            )
            self._mints[mint] = st
        st.snapshots.append(snap)
        self.stats["snapshots_ingested"] += 1

        sell_out_sol = float(snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL
        st.last_quote_out_sol_sequence.append(sell_out_sol)
        if snap.accountSubscribe_curve_price > 0:
            st.last_curve_price_sequence.append(snap.accountSubscribe_curve_price)

        # Track negative curve updates (the "no neg curve after last win" rule
        # in late_entry_blockers depends on this).
        if len(st.last_curve_price_sequence) >= 2:
            prev = st.last_curve_price_sequence[-2]
            cur = st.last_curve_price_sequence[-1]
            if prev > 0 and cur > 0 and cur < prev:
                st.last_negative_curve_update_ts = snap.snapshot_ts_ms

        newly_closed: List[VirtualTicket] = []

        # 1) Update existing open tickets with future observation @ this snap.
        for tk in st.tickets:
            if tk.closed:
                continue
            if snap.snapshot_ts_ms <= tk.buy_snapshot_ts:
                # Strictly future-only.
                continue
            # Compute future_pnl_k: sell tokens-bought-at-buy snapshot AT THIS
            # new snapshot. We don't have the broker here; we approximate
            # sell-out by ratio of sell_quote_out at this snap (which is
            # sell of *its own* tokens) but the caller-provided
            # `extra["sell_at_buy_tokens_pnl"]` is preferred when available.
            future_pnl = tk.same_snapshot_pnl
            if "future_pnl_override" in snap.extra:
                future_pnl = float(snap.extra["future_pnl_override"])
            else:
                # Closed-form approximation:
                # Assume the curve's price at this snap is encoded by
                # sell_quote_out_lamports/buy_quote_tokens. Treat the position
                # value as `buy_tokens_raw * (sell_out_lamports / buy_quote_tokens)`.
                try:
                    if snap.buy_quote_tokens > 0:
                        unit_value_lamports = (
                            float(snap.sell_quote_out_lamports)
                            / float(snap.buy_quote_tokens)
                        )
                        approx_sell_out_lamports = (
                            unit_value_lamports * float(tk.buy_tokens_raw)
                        )
                        approx_sell_out_sol = approx_sell_out_lamports / LAMPORTS_PER_SOL
                        future_pnl = (
                            approx_sell_out_sol - tk.buy_cost_sol - (2.0 * DEFAULT_TX_FEE_SOL)
                        )
                except Exception:
                    pass
            tk.future_pnls.append(
                (len(tk.future_pnls) + 1, snap.snapshot_ts_ms, float(future_pnl))
            )
            if tk.first_observation_ts is None:
                tk.first_observation_ts = snap.snapshot_ts_ms
            tk.last_observation_curve_price = float(snap.accountSubscribe_curve_price)
            if future_pnl > tk.max_favorable:
                tk.max_favorable = float(future_pnl)
            if future_pnl < tk.max_adverse_before_bank and tk.bank_time_ms is None:
                tk.max_adverse_before_bank = float(future_pnl)
            self._maybe_close_ticket(tk, snap, st)
            if tk.closed and tk.outcome != "open":
                if tk not in newly_closed:
                    newly_closed.append(tk)

        # 2) Expire any open ticket past max_hold_ms.
        now = snap.snapshot_ts_ms
        for tk in st.tickets:
            if tk.closed:
                continue
            if (now - tk.buy_snapshot_ts) > self.max_hold_ms:
                tk.outcome = "expired"
                tk.outcome_ts = now
                tk.outcome_reason = "max_hold_exceeded"
                tk.closed = True
                self.stats["tickets_expired"] += 1
                self._emit_expire(tk)
                if tk not in newly_closed:
                    newly_closed.append(tk)

        # 3) Open a NEW ticket using the PREVIOUS snapshot's buy quote.
        if len(st.snapshots) >= 2:
            prev = st.snapshots[-2]
            new_ticket = VirtualTicket(
                ticket_id=_new_ticket_id(),
                mint=mint,
                buy_snapshot_ts=int(prev.snapshot_ts_ms),
                open_emit_ts=int(snap.snapshot_ts_ms),
                amount_sol=self.amount_sol,
                buy_tokens_raw=int(prev.buy_quote_tokens),
                buy_cost_sol=self.amount_sol,
                same_snapshot_pnl=float(prev.all_in_pnl_sol),
            )
            st.tickets.append(new_ticket)
            st.by_id[new_ticket.ticket_id] = new_ticket
            self.stats["tickets_opened"] += 1
            self._emit_open(new_ticket, prev)

            # Immediately apply the CURRENT snap as the first future
            # observation for this newly opened ticket (causal — the current
            # snap was observed AFTER prev.snapshot_ts_ms).
            try:
                unit_value_lamports = (
                    float(snap.sell_quote_out_lamports) / float(snap.buy_quote_tokens)
                    if snap.buy_quote_tokens > 0
                    else 0.0
                )
                approx_sell_out_lamports = unit_value_lamports * float(new_ticket.buy_tokens_raw)
                approx_sell_out_sol = approx_sell_out_lamports / LAMPORTS_PER_SOL
                future_pnl = (
                    approx_sell_out_sol - new_ticket.buy_cost_sol - (2.0 * DEFAULT_TX_FEE_SOL)
                )
            except Exception:
                future_pnl = new_ticket.same_snapshot_pnl
            new_ticket.future_pnls.append((1, snap.snapshot_ts_ms, float(future_pnl)))
            new_ticket.first_observation_ts = snap.snapshot_ts_ms
            new_ticket.last_observation_curve_price = float(snap.accountSubscribe_curve_price)
            if future_pnl > new_ticket.max_favorable:
                new_ticket.max_favorable = float(future_pnl)
            if future_pnl < new_ticket.max_adverse_before_bank:
                new_ticket.max_adverse_before_bank = float(future_pnl)
            self._maybe_close_ticket(new_ticket, snap, st)
            if new_ticket.closed and new_ticket.outcome != "open":
                newly_closed.append(new_ticket)

        # Update still_open counter for diagnostics.
        self.stats["tickets_still_open"] = sum(
            1 for st2 in self._mints.values() for t in st2.tickets if not t.closed
        )
        return newly_closed

    # ----- closing logic ------------------------------------------------

    def _maybe_close_ticket(
        self, tk: VirtualTicket, snap: QuoteSnapshotLite, st: _MintState
    ) -> None:
        if tk.closed:
            return
        if not tk.future_pnls:
            return
        latest_pnl = tk.future_pnls[-1][2]
        # Bank: any future PnL crosses bank threshold.
        if latest_pnl >= self.bank_pnl_sol and tk.bank_time_ms is None:
            tk.outcome = "virtual_bank_win"
            tk.outcome_ts = snap.snapshot_ts_ms
            tk.outcome_reason = "bank_threshold_crossed"
            tk.bank_time_ms = int(snap.snapshot_ts_ms - tk.buy_snapshot_ts)
            tk.bank_pnl_sol = float(latest_pnl)
            tk.closed = True
            self.stats["tickets_banked"] += 1
            self._emit_bank(tk)
            return
        # Loss: any future PnL drops to clamp.
        if latest_pnl <= self.clamp_loss_sol:
            tk.outcome = "virtual_loss"
            tk.outcome_ts = snap.snapshot_ts_ms
            tk.outcome_reason = "clamp_loss"
            tk.closed = True
            self.stats["tickets_lost"] += 1
            self._emit_loss(tk)
            return
        # Scratch: if we previously saw favorable >= scratch but now declining
        # and we have at least 2 observations, and the last observation is
        # strictly lower than the prior favorable.
        if (
            len(tk.future_pnls) >= 2
            and tk.max_favorable >= self.scratch_pnl_sol
            and latest_pnl < tk.max_favorable
            and latest_pnl < self.bank_pnl_sol
        ):
            # Only call it a scratch close if we've actually slipped back
            # below scratch threshold from a non-bank high.
            if latest_pnl < self.scratch_pnl_sol:
                tk.outcome = "virtual_scratch"
                tk.outcome_ts = snap.snapshot_ts_ms
                tk.outcome_reason = "favorable_high_then_decline"
                tk.closed = True
                self.stats["tickets_scratched"] += 1
                self._emit_scratch(tk)
                return
        # Otherwise: keep open.

    # ----- emit helpers -------------------------------------------------

    def _emit_open(self, tk: VirtualTicket, prev_snap: QuoteSnapshotLite) -> None:
        try:
            self._log(
                f"PGG2-V42G-VIRTUAL-TICKET-OPEN mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} buy_snapshot_ts={tk.buy_snapshot_ts} "
                f"buy_tokens_raw={tk.buy_tokens_raw} buy_cost_sol={tk.buy_cost_sol:.6f} "
                f"same_snapshot_pnl={tk.same_snapshot_pnl:+.6f} "
                f"route={prev_snap.route} sim_needed={prev_snap.sim_needed} "
                f"pair_source={prev_snap.pair_source} "
                f"cmc={prev_snap.cost_model_confidence}"
            )
        except Exception:
            pass

    def _emit_bank(self, tk: VirtualTicket) -> None:
        try:
            self._log(
                f"PGG2-V42G-VIRTUAL-TICKET-BANK mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} bank_time_ms={tk.bank_time_ms} "
                f"bank_pnl_sol={tk.bank_pnl_sol:+.6f} "
                f"max_adverse_before_bank={tk.max_adverse_before_bank:+.6f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass

    def _emit_scratch(self, tk: VirtualTicket) -> None:
        try:
            latest_pnl = tk.future_pnls[-1][2] if tk.future_pnls else 0.0
            self._log(
                f"PGG2-V42G-VIRTUAL-TICKET-SCRATCH mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} reason={tk.outcome_reason} "
                f"max_favorable={tk.max_favorable:+.6f} "
                f"latest_pnl={latest_pnl:+.6f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass

    def _emit_loss(self, tk: VirtualTicket) -> None:
        try:
            latest_pnl = tk.future_pnls[-1][2] if tk.future_pnls else 0.0
            self._log(
                f"PGG2-V42G-VIRTUAL-TICKET-LOSS mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} reason={tk.outcome_reason} "
                f"latest_pnl={latest_pnl:+.6f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass

    def _emit_expire(self, tk: VirtualTicket) -> None:
        try:
            self._log(
                f"PGG2-V42G-VIRTUAL-TICKET-EXPIRE mint={_short(tk.mint)} "
                f"ticket_id={tk.ticket_id} reason={tk.outcome_reason} "
                f"max_favorable={tk.max_favorable:+.6f} "
                f"obs_chain_len={len(tk.future_pnls)}"
            )
        except Exception:
            pass


__all__ = [
    "VirtualTicketEngine",
    "VirtualTicket",
    "QuoteSnapshotLite",
    "LookaheadViolation",
    "BANK_PNL_SOL",
    "SCRATCH_PNL_SOL",
    "CLAMP_LOSS_SOL",
    "MAX_HOLD_MS",
]
