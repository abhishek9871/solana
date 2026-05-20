"""V47-P12 Regime Gate — live DO-NOT-TRADE filter.

Computes the V43 hot-regime signal in real-time from raw shred-derived
event streams. Predicate (per user spec for V47-P12):

    is_hot := (quoteable_mints_60s <= 10) AND (buy_sell_ratio_60s >= 2.5)

Rationale: V43's hot-regime signature (memorized as the DON'T-TRADE filter
that produced 0 entries during V39B losing-mirror windows) is LOW flow +
HIGH buy/sell ratio. The persisted V43_PROMOTED_GATE.json encodes a
different family (tape_buy_pressure with B>=5, S>=4.0, R>=2.0); per user
instruction we use the LOW-flow / HIGH-ratio signature specified for
V47-P12.

API:
    class V47P12RegimeGate:
        def ingest_curve_update(self, mint, ts_ms): ...
        def ingest_pump_buy(self, mint, sol_in, ts_ms): ...
        def ingest_pump_sell(self, mint, tokens_in, ts_ms): ...
        def is_hot_regime(self, ts_ms_now) -> tuple[bool, dict]

PURE STATE. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple


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
            f"V47P12-REGIME-GATE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError(
            "forbidden_call_pattern_in_pgg2_v47p12_regime_gate"
        )


# V47-P12 hot-regime thresholds (low flow + high buy/sell ratio).
# Per user spec; values match V43 hot-regime signature memorized from prior work.
QMINTS_60S_MAX = 10
BS_RATIO_60S_MIN = 2.5
BS_RATIO_CAP = 10.0  # avoid divide-by-zero spike on sells == 0


@dataclass
class _CurveEvent:
    ts_ms: int


@dataclass
class _BuyEvent:
    ts_ms: int
    sol_in: float


@dataclass
class _SellEvent:
    ts_ms: int
    tokens_in: int


@dataclass
class _GateState:
    # per-mint rolling curve-update events for "quoteable mints" count
    curve_events_by_mint: Dict[str, Deque[_CurveEvent]] = field(default_factory=dict)
    # global rolling buy/sell totals for buy/sell ratio
    buys: Deque[_BuyEvent] = field(default_factory=lambda: deque(maxlen=4096))
    sells: Deque[_SellEvent] = field(default_factory=lambda: deque(maxlen=4096))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


class V47P12RegimeGate:
    """Live V43-style hot-regime gate. Pure state, no transactions.

    Hot regime conditions (per V47-P12 spec):
      - quoteable_mints_60s <= 10
      - buy_sell_ratio_60s  >= 2.5

    Outside the hot regime -> DO NOT TRADE.
    """

    WINDOW_MS = 60_000
    RETENTION_MS = 90_000  # keep a bit more than the window for stability

    def __init__(self, logger=None, emit_sample_denom: int = 200) -> None:
        self._st = _GateState()
        self._logger = logger
        self._emit_sample_denom = max(1, int(emit_sample_denom))
        self._evals = 0

    def _log(self, msg: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(msg)
        except Exception:
            pass

    # --------- ingest paths ---------
    def ingest_curve_update(self, mint: str, ts_ms: Optional[int] = None) -> None:
        if not mint:
            return
        t = int(ts_ms if ts_ms is not None else _now_ms())
        dq = self._st.curve_events_by_mint.get(mint)
        if dq is None:
            dq = deque(maxlen=256)
            self._st.curve_events_by_mint[mint] = dq
        dq.append(_CurveEvent(ts_ms=t))

    def ingest_pump_buy(
        self, mint: str, sol_in: float, ts_ms: Optional[int] = None
    ) -> None:
        t = int(ts_ms if ts_ms is not None else _now_ms())
        try:
            sol = float(sol_in)
        except Exception:
            sol = 0.0
        self._st.buys.append(_BuyEvent(ts_ms=t, sol_in=sol))
        # curve will be marked separately on accountSubscribe; raw buy alone
        # does not mark "quoteable" — only an actual curve update from the
        # subscriber does.

    def ingest_pump_sell(
        self, mint: str, tokens_in: int, ts_ms: Optional[int] = None
    ) -> None:
        t = int(ts_ms if ts_ms is not None else _now_ms())
        try:
            toks = int(tokens_in)
        except Exception:
            toks = 0
        self._st.sells.append(_SellEvent(ts_ms=t, tokens_in=toks))

    # --------- core evaluation ---------
    def _prune(self, ts_ms_now: int) -> None:
        cutoff = ts_ms_now - int(self.RETENTION_MS)
        # prune buys
        while self._st.buys and int(self._st.buys[0].ts_ms) < cutoff:
            self._st.buys.popleft()
        while self._st.sells and int(self._st.sells[0].ts_ms) < cutoff:
            self._st.sells.popleft()
        # prune curve events; drop empty mint buckets
        dead = []
        for m, dq in self._st.curve_events_by_mint.items():
            while dq and int(dq[0].ts_ms) < cutoff:
                dq.popleft()
            if not dq:
                dead.append(m)
        for m in dead:
            self._st.curve_events_by_mint.pop(m, None)

    def quoteable_mints_60s(self, ts_ms_now: int) -> int:
        self._prune(ts_ms_now)
        window_start = ts_ms_now - int(self.WINDOW_MS)
        count = 0
        for _, dq in self._st.curve_events_by_mint.items():
            # mint counts as quoteable if it had ANY curve update in [start, now]
            for ev in reversed(dq):
                if int(ev.ts_ms) > ts_ms_now:
                    continue
                if int(ev.ts_ms) >= window_start:
                    count += 1
                    break
                else:
                    break
        return count

    def buy_sell_ratio_60s(self, ts_ms_now: int) -> float:
        self._prune(ts_ms_now)
        window_start = ts_ms_now - int(self.WINDOW_MS)
        buys_n = 0
        sells_n = 0
        for ev in self._st.buys:
            if int(ev.ts_ms) > ts_ms_now:
                continue
            if int(ev.ts_ms) >= window_start:
                buys_n += 1
        for ev in self._st.sells:
            if int(ev.ts_ms) > ts_ms_now:
                continue
            if int(ev.ts_ms) >= window_start:
                sells_n += 1
        if sells_n <= 0:
            return float(BS_RATIO_CAP) if buys_n > 0 else 0.0
        ratio = float(buys_n) / float(sells_n)
        if ratio > BS_RATIO_CAP:
            ratio = float(BS_RATIO_CAP)
        return ratio

    def is_hot_regime(self, ts_ms_now: int) -> Tuple[bool, Dict[str, Any]]:
        self._evals += 1
        qmints = self.quoteable_mints_60s(ts_ms_now)
        bs = self.buy_sell_ratio_60s(ts_ms_now)
        reason = None
        low_flow_ok = qmints <= QMINTS_60S_MAX
        ratio_ok = bs >= BS_RATIO_60S_MIN
        is_hot = bool(low_flow_ok and ratio_ok)
        if not is_hot:
            if not low_flow_ok and not ratio_ok:
                reason = (
                    f"qmints_60s={qmints}>10 AND bs_ratio_60s={bs:.2f}<2.5"
                )
            elif not low_flow_ok:
                reason = f"qmints_60s={qmints}>10"
            else:
                reason = f"bs_ratio_60s={bs:.2f}<2.5"
        info = {
            "quoteable_mints_60s": int(qmints),
            "buy_sell_ratio_60s": float(bs),
            "reason_if_blocked": reason,
            "thresholds": {
                "qmints_60s_max": int(QMINTS_60S_MAX),
                "bs_ratio_60s_min": float(BS_RATIO_60S_MIN),
            },
        }
        # Sampled debug log
        if (self._evals % self._emit_sample_denom) == 0:
            self._log(
                f"PGG2-V47P12-REGIME-SAMPLE ts={ts_ms_now} "
                f"qmints={qmints} bs_ratio={bs:.3f} hot={is_hot} "
                f"reason={reason}"
            )
        return is_hot, info

    # --------- introspection ---------
    def stats(self) -> Dict[str, Any]:
        return {
            "evals_total": int(self._evals),
            "mints_with_curve_events": int(
                len(self._st.curve_events_by_mint)
            ),
            "buys_buffered": int(len(self._st.buys)),
            "sells_buffered": int(len(self._st.sells)),
        }
