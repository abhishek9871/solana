"""V56 live momentum gate.

Called AFTER v48 candidate decides, BEFORE V53 risk veto.

Hard rule: risk-clean is not enough. Momentum must be real.

Pass conditions (any one):
  - sell_quote_improving (projected sell quote > 500ms ago)
  - curve_delta_positive (accountSubscribe vsol delta net positive in last 500ms)
  - pending_buy_accelerating (pending buy flow density increasing)
  - expected_pnl_strong (expected_pnl >= strong threshold AND fee hurdle clear)

Block conditions (any one, evaluated first):
  - pnl_below_fee_hurdle (expected_pnl < fee hurdle)
  - sell_quote_flat (no improvement for >= flat_max_ms)
  - all_signals_neutral (no positive momentum signal AND not strong-pnl)

Log: PGG2-V56-LIVE-MOMENTUM-GATE mint=.. exp_pnl=.. fee_hurdle=..
     sell_q_now=.. sell_q_500ms=.. curve_delta_500ms=..
     pending_buy_500ms=.. pending_buy_1000ms=.. pass=.. blocker=..
     reason_pass=..

The gate is conservative: any single positive signal passes. The intent is
to filter "risk-clean but dead" candidates, not to require all signals positive.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class MomentumSnapshot:
    """All the local-state inputs the gate needs.

    The integration layer fills these from v48 harness data at the
    moment after the candidate is emitted.
    """
    mint: str
    expected_pnl_sol: float

    # Sell-quote projection (V47I local CPMM quote engine result)
    sell_quote_now_sol: float
    sell_quote_500ms_ago_sol: float

    # Curve delta (accountSubscribe BondingCurve PDA vsol changes)
    curve_vsol_delta_500ms_sol: float  # net delta over last 500ms

    # Pending-buy flow (V47C signer-aware buffer / V42C oracle)
    pending_buy_count_500ms: int
    pending_buy_count_1000ms: int
    pending_buy_sol_500ms: float
    pending_buy_sol_1000ms: float

    # Trigger reference (when candidate first emitted)
    trigger_ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    # Optional V47H ratio (rug-veto upstream may have set this)
    v47h_ratio: Optional[float] = None


@dataclass
class MomentumResult:
    mint: str
    pass_: bool
    blocker: Optional[str]
    reason_pass: Optional[str]
    signals: dict


class V56MomentumGate:
    def __init__(self) -> None:
        # Fee hurdle: expected_pnl must exceed this to enter at all
        self.fee_hurdle_sol = _envf("PGG2_V56_MOMENTUM_FEE_HURDLE_SOL", 0.0006)

        # Strong-PnL shortcut: pnl >= this passes regardless of other signals
        self.strong_pnl_sol = _envf("PGG2_V56_MOMENTUM_STRONG_PNL_SOL", 0.005)

        # Sell quote: require improvement of at least this much
        self.sell_quote_min_improvement_sol = _envf(
            "PGG2_V56_MOMENTUM_SELL_Q_MIN_IMPROVEMENT_SOL", 0.0
        )

        # Curve delta: positive net required (in SOL)
        self.curve_delta_min_sol = _envf("PGG2_V56_MOMENTUM_CURVE_DELTA_MIN_SOL", 0.0)

        # Pending buy acceleration: count_1000ms / count_500ms > this ratio
        # (count in second half-second > count in first half-second)
        self.pending_buy_accel_ratio = _envf(
            "PGG2_V56_MOMENTUM_PENDING_BUY_ACCEL_RATIO", 1.0
        )

        # Strict mode: require BOTH a momentum signal AND pnl above fee hurdle
        # (default: ANY momentum signal passes once fee hurdle is met)
        self.strict_mode = _envb("PGG2_V56_MOMENTUM_STRICT_MODE", False)

        # Master enable
        self.enabled = _envb("PGG2_V56_MOMENTUM_GATE_ENABLED", True)

    def check(self, snap: MomentumSnapshot) -> MomentumResult:
        signals = {
            "sell_quote_improving": (
                snap.sell_quote_now_sol - snap.sell_quote_500ms_ago_sol
                >= self.sell_quote_min_improvement_sol
            ),
            "curve_delta_positive": (
                snap.curve_vsol_delta_500ms_sol > self.curve_delta_min_sol
            ),
            "pending_buy_accelerating": (
                snap.pending_buy_count_500ms > 0
                and (snap.pending_buy_count_1000ms - snap.pending_buy_count_500ms)
                / max(snap.pending_buy_count_500ms, 1)
                > (self.pending_buy_accel_ratio - 1.0)
            ),
            "expected_pnl_strong": (
                snap.expected_pnl_sol >= self.strong_pnl_sol
            ),
        }

        # Disabled = pass everything (still emit log line)
        if not self.enabled:
            return MomentumResult(
                mint=snap.mint, pass_=True, blocker=None,
                reason_pass="gate_disabled", signals=signals,
            )

        # Block: PnL below fee hurdle (hardest stop)
        if snap.expected_pnl_sol < self.fee_hurdle_sol:
            return MomentumResult(
                mint=snap.mint, pass_=False,
                blocker=f"pnl_below_fee_hurdle({snap.expected_pnl_sol:.6f}<{self.fee_hurdle_sol:.6f})",
                reason_pass=None, signals=signals,
            )

        # Block: sell quote flat or negative
        sell_delta = snap.sell_quote_now_sol - snap.sell_quote_500ms_ago_sol
        if sell_delta < 0:
            return MomentumResult(
                mint=snap.mint, pass_=False,
                blocker=f"sell_quote_falling({sell_delta:+.6f})",
                reason_pass=None, signals=signals,
            )

        # Pass: any momentum signal positive
        positive = [k for k, v in signals.items() if v]
        if positive:
            return MomentumResult(
                mint=snap.mint, pass_=True, blocker=None,
                reason_pass="+".join(positive), signals=signals,
            )

        # Strict mode: no positive signal = block
        # Default (non-strict): allow if pnl > fee_hurdle AND sell_delta >= 0
        if self.strict_mode:
            return MomentumResult(
                mint=snap.mint, pass_=False,
                blocker="all_signals_neutral",
                reason_pass=None, signals=signals,
            )
        return MomentumResult(
            mint=snap.mint, pass_=True, blocker=None,
            reason_pass="pnl_above_hurdle_neutral_signals", signals=signals,
        )

    def format_log_line(self, snap: MomentumSnapshot, r: MomentumResult) -> str:
        short = snap.mint[:4] + ".." + snap.mint[-4:] if len(snap.mint) > 10 else snap.mint
        return (
            f"PGG2-V56-LIVE-MOMENTUM-GATE {short} "
            f"exp_pnl={snap.expected_pnl_sol:+.6f} "
            f"fee_hurdle={self.fee_hurdle_sol:.6f} "
            f"sell_q_now={snap.sell_quote_now_sol:.6f} "
            f"sell_q_500ms={snap.sell_quote_500ms_ago_sol:.6f} "
            f"curve_delta_500ms={snap.curve_vsol_delta_500ms_sol:+.6f} "
            f"pending_buy_500ms={snap.pending_buy_count_500ms} "
            f"pending_buy_1000ms={snap.pending_buy_count_1000ms} "
            f"pass={int(r.pass_)} "
            f"blocker={r.blocker or '-'} "
            f"reason_pass={r.reason_pass or '-'}"
        )


_SINGLETON: Optional[V56MomentumGate] = None


def get_gate() -> V56MomentumGate:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V56MomentumGate()
    return _SINGLETON


def check_and_log(snap: MomentumSnapshot, log_fn=print) -> MomentumResult:
    gate = get_gate()
    r = gate.check(snap)
    log_fn(gate.format_log_line(snap, r))
    return r


if __name__ == "__main__":
    # CLI smoke test with synthetic data
    test = MomentumSnapshot(
        mint="TESTpump" + "1" * 36,
        expected_pnl_sol=0.0015,
        sell_quote_now_sol=0.00510,
        sell_quote_500ms_ago_sol=0.00500,
        curve_vsol_delta_500ms_sol=0.250,
        pending_buy_count_500ms=2,
        pending_buy_count_1000ms=5,
        pending_buy_sol_500ms=0.5,
        pending_buy_sol_1000ms=1.2,
    )
    r = check_and_log(test)
    print(f"\nResult: pass={r.pass_} reason={r.reason_pass} blocker={r.blocker}")

    print("\n--- block case: pnl below hurdle ---")
    test2 = MomentumSnapshot(
        mint="TESTpump" + "2" * 36,
        expected_pnl_sol=0.0001,  # below default 0.0006 hurdle
        sell_quote_now_sol=0.005,
        sell_quote_500ms_ago_sol=0.005,
        curve_vsol_delta_500ms_sol=0.0,
        pending_buy_count_500ms=0,
        pending_buy_count_1000ms=0,
        pending_buy_sol_500ms=0,
        pending_buy_sol_1000ms=0,
    )
    r2 = check_and_log(test2)
    print(f"\nResult: pass={r2.pass_} blocker={r2.blocker}")

    print("\n--- block case: sell quote falling ---")
    test3 = MomentumSnapshot(
        mint="TESTpump" + "3" * 36,
        expected_pnl_sol=0.002,
        sell_quote_now_sol=0.0048,
        sell_quote_500ms_ago_sol=0.0050,  # fell
        curve_vsol_delta_500ms_sol=0.0,
        pending_buy_count_500ms=0,
        pending_buy_count_1000ms=0,
        pending_buy_sol_500ms=0,
        pending_buy_sol_1000ms=0,
    )
    r3 = check_and_log(test3)
    print(f"\nResult: pass={r3.pass_} blocker={r3.blocker}")
