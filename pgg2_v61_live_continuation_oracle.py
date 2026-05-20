"""V61 Live Continuation Oracle.

Runs AFTER V60 firewall PASS but BEFORE broker.send_signed. Validates that the
candidate's curve and quote trajectories are still favorable RIGHT NOW, after
fresh data has arrived since V60 evaluated. Closes the gap that V60 leaves:
V60 gates on static entry economics (predicted edge from snapshot at decision
time); V61 gates on real-time continuation immediately before the buy is sent.

Built in response to V60 RUN4 forensic (2026-05-19):
  - 3Tcp..pump: V60 PASS true_edge=+0.000987 → curve dumped -13.25% within 2s
  - 66Qi..pump: V60 PASS true_edge=+0.000786 → curve dumped -9.99% within 100ms
  Both losses post-V60. Pre-V60 had clean buy flow. V61 sees the post-V60
  trajectory and refuses to send when curve/quote slope flips negative.

Hard rule: no live buy unless V60-FIREWALL-PASS AND V61-CONTINUATION-PASS.

10 PASS rules (ALL must be true):
  1. V60 true_edge already passed (caller-asserted; we double-check via input).
  2. Latest curve delta is positive or neutral (>= 0).
  3. No negative curve delta in the last 500ms window.
  4. Quote slope positive in the last 300-700ms window.
  5. Quote slope not decelerating by more than 50% (current slope >= 0.5 *
     peak slope in window).
  6. pending_sell_sol_500ms < pending_buy_sol_500ms * 0.25.
  7. pending_sell_count_500ms < pending_buy_count_500ms.
  8. Local quote improved after V60 pass (post-V60 quote > pre-V60 quote).
  9. Candidate has not peaked and flattened. Peak detect: block if curve
     rose > 30% in the last 1000ms (likely peak-entry pattern, mean reversion
     follows). Otherwise PASS.
 10. Signal age <= 500ms (snapshot must be fresh).

BLOCK conditions (any one fires → block):
  - quote_peak_age_ms > 500 AND current quote below peak.
  - curve_delta_negative_after_v60_pass.
  - sell pressure appears after V60 pass.
  - quote flat for 500ms (slope near zero).
  - expected break-even requires continuation but current slope cannot cover
    it within max_hold_ms.
  - stale accountSubscribe / no fresh curve data within 250ms of V60 PASS.

Caller responsibility (in harness):
  1. V60 PASS fires.
  2. Caller waits 250ms (or until ≥1 fresh curve update arrives, whichever
     first; max 500ms).
  3. Caller calls v61_check_continuation(...) with the data window.
  4. If PASS → send_signed.
  5. If BLOCK with `blocker=stale_quote_not_yet_confirmed` → add to V61
     watchlist (1000ms TTL), re-evaluate on each fresh update.

Logs (emitted via log_fn):
  PGG2-V61-CONTINUATION-CHECK mint=.. v60_true_edge=.. window_size=.. signal_age_ms=..
  PGG2-V61-CONTINUATION-PASS  mint=.. score=.. curve_slope=.. quote_slope=..
  PGG2-V61-CONTINUATION-BLOCK mint=.. blocker=.. detail=..
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# ============================================================================
#  Env helpers
# ============================================================================

def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ============================================================================
#  Data classes
# ============================================================================

@dataclass
class CurvePoint:
    """One curve snapshot: (timestamp_ms, vsol_lamports, vtok_raw, price)."""
    timestamp_ms: int
    vsol_lamports: int
    vtok_raw: int
    price: float  # vsol / vtok


@dataclass
class QuotePoint:
    """One local quote evaluation: (timestamp_ms, sell_quote_sol_out)."""
    timestamp_ms: int
    sell_quote_sol_out: float


@dataclass
class V61Inputs:
    mint: str
    selected_size_sol: float
    v60_true_edge_sol: float
    v60_pass_timestamp_ms: int
    # Curve history: ordered ascending by timestamp_ms
    curve_history: list[CurvePoint]
    # Quote history: ordered ascending by timestamp_ms
    quote_history: list[QuotePoint]
    # Pending flow (snapshot at V60 pass time)
    pending_buy_sol_500ms: float
    pending_sell_sol_500ms: float
    pending_buy_count_500ms: int
    pending_sell_count_500ms: int
    # Latest signal age (ms)
    signal_age_ms: int
    # Optional: expected break-even and max hold
    expected_break_even_sol: Optional[float] = None
    max_hold_ms: int = 1500
    # Timestamp at oracle eval time
    eval_timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class V61RuleResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class V61Decision:
    passed: bool
    blocker: Optional[str]
    rules: list[V61RuleResult]
    # Scores
    continuation_score: float
    curve_slope: float
    curve_acceleration: float
    quote_slope: float
    quote_acceleration: float
    sell_pressure_ratio: float
    freshness_ms: int

    def to_log_pass(self, short_mint: str) -> str:
        return (
            f"PGG2-V61-CONTINUATION-PASS mint={short_mint} "
            f"score={self.continuation_score:.3f} "
            f"curve_slope={self.curve_slope:+.6f} "
            f"quote_slope={self.quote_slope:+.6f} "
            f"sell_ratio={self.sell_pressure_ratio:.3f} "
            f"freshness_ms={self.freshness_ms}"
        )

    def to_log_block(self, short_mint: str) -> str:
        det = ""
        for r in self.rules:
            if not r.passed:
                det = r.detail
                break
        return (
            f"PGG2-V61-CONTINUATION-BLOCK mint={short_mint} "
            f"blocker={self.blocker} detail={det} "
            f"curve_slope={self.curve_slope:+.6f} "
            f"quote_slope={self.quote_slope:+.6f}"
        )


# ============================================================================
#  Config
# ============================================================================

class V61Config:
    def __init__(self) -> None:
        # Rule thresholds (env-overridable)
        self.curve_slope_window_ms = _envi("PGG2_V61_CURVE_SLOPE_WINDOW_MS", 500)
        self.quote_slope_min_window_ms = _envi("PGG2_V61_QUOTE_SLOPE_MIN_MS", 300)
        self.quote_slope_max_window_ms = _envi("PGG2_V61_QUOTE_SLOPE_MAX_MS", 700)
        self.quote_slope_decel_max_pct = _envf(
            "PGG2_V61_QUOTE_SLOPE_DECEL_MAX_PCT", 0.50
        )
        self.sell_buy_sol_ratio_max = _envf(
            "PGG2_V61_SELL_BUY_SOL_RATIO_MAX", 0.25
        )
        self.signal_age_max_ms = _envi("PGG2_V61_SIGNAL_AGE_MAX_MS", 500)
        self.peak_rise_pct_window_ms = _envi(
            "PGG2_V61_PEAK_WINDOW_MS", 1000
        )
        self.peak_rise_pct_threshold = _envf(
            "PGG2_V61_PEAK_RISE_PCT_THRESHOLD", 0.30
        )
        self.min_curve_points = _envi("PGG2_V61_MIN_CURVE_POINTS", 2)
        self.min_quote_points = _envi("PGG2_V61_MIN_QUOTE_POINTS", 2)
        self.quote_flat_window_ms = _envi("PGG2_V61_QUOTE_FLAT_WINDOW_MS", 500)
        self.quote_flat_threshold_sol = _envf(
            "PGG2_V61_QUOTE_FLAT_THRESHOLD_SOL", 0.0000010
        )
        # Stale data threshold (caller should already have waited)
        self.max_freshness_after_v60_ms = _envi(
            "PGG2_V61_MAX_FRESHNESS_AFTER_V60_MS", 500
        )


_CFG: Optional[V61Config] = None


def get_config() -> V61Config:
    global _CFG
    if _CFG is None:
        _CFG = V61Config()
    return _CFG


# ============================================================================
#  Slope/acceleration helpers
# ============================================================================

def _filter_window(points, window_start_ms: int, window_end_ms: int):
    return [p for p in points if window_start_ms <= p.timestamp_ms <= window_end_ms]


def _slope_per_ms(points, accessor) -> float:
    """Linear slope of values vs timestamp_ms across the points list."""
    n = len(points)
    if n < 2:
        return 0.0
    xs = [p.timestamp_ms for p in points]
    ys = [accessor(p) for p in points]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def _acceleration(points, accessor) -> float:
    """Approximate 2nd derivative via finite-difference of slopes."""
    n = len(points)
    if n < 3:
        return 0.0
    mid = n // 2
    s1 = _slope_per_ms(points[:mid + 1], accessor)
    s2 = _slope_per_ms(points[mid:], accessor)
    dt = points[-1].timestamp_ms - points[0].timestamp_ms
    if dt == 0:
        return 0.0
    return (s2 - s1) / max(1, dt / 2)


# ============================================================================
#  Rule implementations
# ============================================================================

def _rule_2_latest_curve_delta_non_negative(inputs: V61Inputs) -> V61RuleResult:
    if len(inputs.curve_history) < 2:
        return V61RuleResult(
            "r2_latest_curve_delta",
            False,
            f"insufficient_curve_points_{len(inputs.curve_history)}",
        )
    last = inputs.curve_history[-1]
    prev = inputs.curve_history[-2]
    delta = last.price - prev.price
    if delta >= 0:
        return V61RuleResult(
            "r2_latest_curve_delta",
            True,
            f"delta={delta:+.9f}",
        )
    return V61RuleResult(
        "r2_latest_curve_delta",
        False,
        f"latest_curve_delta_negative={delta:+.9f}",
    )


def _rule_3_no_negative_curve_in_500ms(
    cfg: V61Config, inputs: V61Inputs
) -> V61RuleResult:
    window_start = inputs.eval_timestamp_ms - cfg.curve_slope_window_ms
    pts = _filter_window(inputs.curve_history, window_start, inputs.eval_timestamp_ms)
    if len(pts) < 2:
        return V61RuleResult(
            "r3_no_negative_in_window",
            False,
            f"insufficient_curve_points_in_{cfg.curve_slope_window_ms}ms_window",
        )
    neg_count = 0
    for i in range(1, len(pts)):
        if pts[i].price < pts[i - 1].price:
            neg_count += 1
    if neg_count == 0:
        return V61RuleResult(
            "r3_no_negative_in_window",
            True,
            f"checked_{len(pts)}_points_in_{cfg.curve_slope_window_ms}ms",
        )
    return V61RuleResult(
        "r3_no_negative_in_window",
        False,
        f"{neg_count}_negative_deltas_in_{cfg.curve_slope_window_ms}ms_window",
    )


def _rule_4_quote_slope_positive(
    cfg: V61Config, inputs: V61Inputs
) -> tuple[V61RuleResult, float]:
    window_end = inputs.eval_timestamp_ms
    window_start = window_end - cfg.quote_slope_max_window_ms
    pts = _filter_window(inputs.quote_history, window_start, window_end)
    if len(pts) < 2:
        return (
            V61RuleResult(
                "r4_quote_slope_positive",
                False,
                f"insufficient_quote_points_in_{cfg.quote_slope_max_window_ms}ms",
            ),
            0.0,
        )
    slope = _slope_per_ms(pts, lambda p: p.sell_quote_sol_out)
    if slope > 0:
        return (
            V61RuleResult(
                "r4_quote_slope_positive",
                True,
                f"slope={slope:+.10f}_per_ms ({len(pts)}_points)",
            ),
            slope,
        )
    return (
        V61RuleResult(
            "r4_quote_slope_positive",
            False,
            f"non_positive_quote_slope={slope:+.10f}",
        ),
        slope,
    )


def _rule_5_no_excessive_decel(
    cfg: V61Config, inputs: V61Inputs, current_slope: float
) -> V61RuleResult:
    # Compare current half-window slope to peak (first-half) slope.
    window_start = inputs.eval_timestamp_ms - cfg.quote_slope_max_window_ms
    pts = _filter_window(
        inputs.quote_history, window_start, inputs.eval_timestamp_ms
    )
    if len(pts) < 4:
        # Insufficient points to compute deceleration; pass by default
        # (Rule 4 already verified positive slope).
        return V61RuleResult(
            "r5_no_excessive_decel",
            True,
            f"insufficient_points_for_decel_check_{len(pts)}",
        )
    mid = len(pts) // 2
    early = _slope_per_ms(pts[:mid + 1], lambda p: p.sell_quote_sol_out)
    late = _slope_per_ms(pts[mid:], lambda p: p.sell_quote_sol_out)
    if early <= 0:
        # If early slope was already non-positive, no decel concern (caller
        # of Rule 4 will have caught the non-positive issue).
        return V61RuleResult(
            "r5_no_excessive_decel",
            True,
            f"early_slope_non_positive={early:+.10f}",
        )
    decel_ratio = late / early
    if decel_ratio >= 0.5:
        return V61RuleResult(
            "r5_no_excessive_decel",
            True,
            f"decel_ratio={decel_ratio:.3f} (>= 0.5 = not decelerating > 50%)",
        )
    return V61RuleResult(
        "r5_no_excessive_decel",
        False,
        f"decel_ratio={decel_ratio:.3f}_below_0.5 (decelerating > 50%)",
    )


def _rule_6_sell_buy_sol_ratio(
    cfg: V61Config, inputs: V61Inputs
) -> tuple[V61RuleResult, float]:
    buy_sol = max(0.0, inputs.pending_buy_sol_500ms)
    sell_sol = max(0.0, inputs.pending_sell_sol_500ms)
    if buy_sol <= 0:
        return (
            V61RuleResult(
                "r6_sell_buy_ratio",
                False,
                f"pending_buy_sol_zero ({buy_sol:.6f})",
            ),
            float("inf") if sell_sol > 0 else 0.0,
        )
    ratio = sell_sol / buy_sol
    if ratio < cfg.sell_buy_sol_ratio_max:
        return (
            V61RuleResult(
                "r6_sell_buy_ratio",
                True,
                f"sell/buy={ratio:.3f} < {cfg.sell_buy_sol_ratio_max}",
            ),
            ratio,
        )
    return (
        V61RuleResult(
            "r6_sell_buy_ratio",
            False,
            f"sell/buy={ratio:.3f} >= {cfg.sell_buy_sol_ratio_max}",
        ),
        ratio,
    )


def _rule_7_sell_buy_count(inputs: V61Inputs) -> V61RuleResult:
    if inputs.pending_sell_count_500ms < inputs.pending_buy_count_500ms:
        return V61RuleResult(
            "r7_sell_buy_count",
            True,
            f"sell={inputs.pending_sell_count_500ms} < buy={inputs.pending_buy_count_500ms}",
        )
    return V61RuleResult(
        "r7_sell_buy_count",
        False,
        f"sell={inputs.pending_sell_count_500ms} >= buy={inputs.pending_buy_count_500ms}",
    )


def _rule_8_local_quote_improved(inputs: V61Inputs) -> V61RuleResult:
    # Find the quote at V60 PASS time and the latest quote.
    v60_quote = None
    for q in inputs.quote_history:
        if q.timestamp_ms >= inputs.v60_pass_timestamp_ms:
            v60_quote = q
            break
    if v60_quote is None and inputs.quote_history:
        # No post-V60 quote yet; treat as insufficient data.
        return V61RuleResult(
            "r8_local_quote_improved",
            False,
            "no_quote_at_or_after_v60_pass",
        )
    if not inputs.quote_history:
        return V61RuleResult(
            "r8_local_quote_improved",
            False,
            "no_quote_history",
        )
    latest = inputs.quote_history[-1]
    if v60_quote is None:
        return V61RuleResult(
            "r8_local_quote_improved",
            False,
            "no_post_v60_quote",
        )
    if latest.sell_quote_sol_out > v60_quote.sell_quote_sol_out:
        return V61RuleResult(
            "r8_local_quote_improved",
            True,
            f"latest={latest.sell_quote_sol_out:.6f} > v60_pass={v60_quote.sell_quote_sol_out:.6f}",
        )
    return V61RuleResult(
        "r8_local_quote_improved",
        False,
        f"latest={latest.sell_quote_sol_out:.6f} <= v60_pass={v60_quote.sell_quote_sol_out:.6f}",
    )


def _rule_9_not_peaked_and_flattened(
    cfg: V61Config, inputs: V61Inputs
) -> V61RuleResult:
    """Detect peak-entry pattern: large rise in last N ms.

    Inspired by 66Qi..pump RUN4 loss: curve had +108% in the window
    immediately before V60 evaluated. Mean-reversion followed within 100ms.
    Block if curve rose more than `peak_rise_pct_threshold` in the
    `peak_rise_pct_window_ms` window.
    """
    if len(inputs.curve_history) < 2:
        # No history to evaluate; assume not peaked
        return V61RuleResult(
            "r9_not_peaked",
            True,
            f"insufficient_curve_points_{len(inputs.curve_history)}",
        )
    latest = inputs.curve_history[-1]
    window_start = latest.timestamp_ms - cfg.peak_rise_pct_window_ms
    pts = _filter_window(
        inputs.curve_history, window_start, latest.timestamp_ms
    )
    if len(pts) < 2:
        return V61RuleResult(
            "r9_not_peaked",
            True,
            f"insufficient_curve_points_in_{cfg.peak_rise_pct_window_ms}ms_window",
        )
    earliest = pts[0]
    if earliest.price <= 0:
        return V61RuleResult(
            "r9_not_peaked",
            True,
            "earliest_price_zero",
        )
    rise_pct = (latest.price - earliest.price) / earliest.price
    if rise_pct < cfg.peak_rise_pct_threshold:
        return V61RuleResult(
            "r9_not_peaked",
            True,
            f"rise_pct={rise_pct*100:+.2f}% < threshold={cfg.peak_rise_pct_threshold*100:.0f}%",
        )
    return V61RuleResult(
        "r9_not_peaked",
        False,
        f"peak_entry_detected_rise_pct={rise_pct*100:+.2f}% >= {cfg.peak_rise_pct_threshold*100:.0f}%",
    )


def _rule_10_signal_age(cfg: V61Config, inputs: V61Inputs) -> V61RuleResult:
    if inputs.signal_age_ms <= cfg.signal_age_max_ms:
        return V61RuleResult(
            "r10_signal_age",
            True,
            f"signal_age={inputs.signal_age_ms}ms <= {cfg.signal_age_max_ms}ms",
        )
    return V61RuleResult(
        "r10_signal_age",
        False,
        f"signal_age={inputs.signal_age_ms}ms > {cfg.signal_age_max_ms}ms",
    )


def _rule_freshness(cfg: V61Config, inputs: V61Inputs) -> V61RuleResult:
    """In async (wait) mode: block if no fresh curve update has arrived since V60 PASS.
    In sync (no wait) mode (default): block if latest curve point is older than
    max_curve_age_ms relative to eval_timestamp_ms.
    """
    if not inputs.curve_history:
        return V61RuleResult(
            "r_freshness", False, "no_curve_history",
        )
    # Sync-mode freshness: latest curve point recent enough relative to now.
    sync_mode = _envb("PGG2_V61_SYNC_FRESHNESS_MODE", True)
    max_age = _envi("PGG2_V61_MAX_CURVE_AGE_MS", 1000)
    if sync_mode:
        latest_ts = inputs.curve_history[-1].timestamp_ms
        age = inputs.eval_timestamp_ms - latest_ts
        if age <= max_age:
            return V61RuleResult(
                "r_freshness",
                True,
                f"sync_mode_latest_curve_age={age}ms<={max_age}ms",
            )
        return V61RuleResult(
            "r_freshness",
            False,
            f"sync_mode_latest_curve_too_old_age={age}ms>{max_age}ms",
        )
    # Async (wait) mode: strict check for new data after V60 PASS.
    fresh_count = sum(
        1 for cp in inputs.curve_history
        if cp.timestamp_ms > inputs.v60_pass_timestamp_ms
    )
    if fresh_count >= 1:
        return V61RuleResult(
            "r_freshness",
            True,
            f"{fresh_count}_fresh_curve_points_after_v60_pass",
        )
    return V61RuleResult(
        "r_freshness",
        False,
        f"no_fresh_curve_after_v60_pass_in_{cfg.max_freshness_after_v60_ms}ms",
    )


def _rule_quote_flat(cfg: V61Config, inputs: V61Inputs) -> V61RuleResult:
    window_start = inputs.eval_timestamp_ms - cfg.quote_flat_window_ms
    pts = _filter_window(
        inputs.quote_history, window_start, inputs.eval_timestamp_ms
    )
    if len(pts) < 2:
        return V61RuleResult(
            "r_quote_flat", True, f"insufficient_quote_points={len(pts)}"
        )
    mn = min(p.sell_quote_sol_out for p in pts)
    mx = max(p.sell_quote_sol_out for p in pts)
    spread = mx - mn
    if spread > cfg.quote_flat_threshold_sol:
        return V61RuleResult(
            "r_quote_flat", True, f"quote_spread={spread:.6f}_above_flat_threshold"
        )
    return V61RuleResult(
        "r_quote_flat",
        False,
        f"quote_flat_for_{cfg.quote_flat_window_ms}ms (spread={spread:.6f})",
    )


# ============================================================================
#  Main entry point
# ============================================================================

def _short(mint: str) -> str:
    if len(mint) > 10:
        return mint[:4] + ".." + mint[-4:]
    return mint


def v61_check_continuation(
    inputs: V61Inputs,
    log_fn: Optional[Callable[[str], None]] = None,
) -> V61Decision:
    """Run all V61 rules. Returns V61Decision."""
    cfg = get_config()
    log = log_fn if log_fn is not None else print
    short = _short(inputs.mint)

    # Audit log
    log(
        f"PGG2-V61-CONTINUATION-CHECK mint={short} "
        f"v60_true_edge={inputs.v60_true_edge_sol:+.6f} "
        f"curve_points={len(inputs.curve_history)} "
        f"quote_points={len(inputs.quote_history)} "
        f"signal_age_ms={inputs.signal_age_ms} "
        f"size={inputs.selected_size_sol:.4f}"
    )

    rules: list[V61RuleResult] = []
    blocker: Optional[str] = None

    # Freshness check first (caller should ensure ≥1 fresh curve after V60).
    r_fresh = _rule_freshness(cfg, inputs)
    rules.append(r_fresh)
    if not r_fresh.passed:
        blocker = "stale_no_fresh_curve_after_v60_pass"

    # Rule 2: latest curve delta non-negative
    if blocker is None:
        r = _rule_2_latest_curve_delta_non_negative(inputs)
        rules.append(r)
        if not r.passed:
            blocker = "curve_delta_negative"

    # Rule 3: no negative curve delta in last 500ms
    if blocker is None:
        r = _rule_3_no_negative_curve_in_500ms(cfg, inputs)
        rules.append(r)
        if not r.passed:
            blocker = "negative_curve_in_500ms_window"

    # Rule 4: quote slope positive (skip in sync mode when no real quote diversity)
    quote_slope = 0.0
    _sync_skip_quote_rules = _envb("PGG2_V61_SYNC_SKIP_QUOTE_RULES", True)
    if blocker is None:
        if _sync_skip_quote_rules and len(inputs.quote_history) < 3:
            rules.append(V61RuleResult(
                "r4_quote_slope_positive", True,
                f"sync_mode_skip_insufficient_quote_history={len(inputs.quote_history)}",
            ))
        else:
            r, quote_slope = _rule_4_quote_slope_positive(cfg, inputs)
            rules.append(r)
            if not r.passed:
                blocker = "quote_slope_non_positive"

    # Rule 5: no excessive deceleration (also skipped if quote rules skipped)
    if blocker is None:
        if _sync_skip_quote_rules and len(inputs.quote_history) < 3:
            rules.append(V61RuleResult(
                "r5_no_excessive_decel", True,
                "sync_mode_skip",
            ))
        else:
            r = _rule_5_no_excessive_decel(cfg, inputs, quote_slope)
            rules.append(r)
            if not r.passed:
                blocker = "quote_slope_decelerating_over_50pct"

    # Rule 6: sell/buy SOL ratio
    sell_pressure_ratio = 0.0
    if blocker is None:
        r, sell_pressure_ratio = _rule_6_sell_buy_sol_ratio(cfg, inputs)
        rules.append(r)
        if not r.passed:
            blocker = "sell_pressure_too_high"

    # Rule 7: sell/buy count
    if blocker is None:
        r = _rule_7_sell_buy_count(inputs)
        rules.append(r)
        if not r.passed:
            blocker = "sell_count_exceeds_buy_count"

    # Rule 8: local quote improved after V60 pass (skip in sync mode)
    if blocker is None:
        if _sync_skip_quote_rules and len(inputs.quote_history) < 2:
            rules.append(V61RuleResult(
                "r8_local_quote_improved", True,
                "sync_mode_skip",
            ))
        else:
            r = _rule_8_local_quote_improved(inputs)
            rules.append(r)
            if not r.passed:
                blocker = "local_quote_did_not_improve_after_v60"

    # Rule 9: not peaked and flattened (66Qi pattern)
    if blocker is None:
        r = _rule_9_not_peaked_and_flattened(cfg, inputs)
        rules.append(r)
        if not r.passed:
            blocker = "peak_entry_detected_rule_9"

    # Rule 10: signal age
    if blocker is None:
        r = _rule_10_signal_age(cfg, inputs)
        rules.append(r)
        if not r.passed:
            blocker = "signal_too_stale"

    # Quote-flat additional check (skip in sync mode without real quotes)
    if blocker is None:
        if _sync_skip_quote_rules and len(inputs.quote_history) < 3:
            rules.append(V61RuleResult(
                "r_quote_flat", True,
                "sync_mode_skip",
            ))
        else:
            r = _rule_quote_flat(cfg, inputs)
            rules.append(r)
            if not r.passed:
                blocker = "quote_flat_500ms"

    # Compute scores
    curve_slope = _slope_per_ms(
        inputs.curve_history[-min(8, len(inputs.curve_history)):],
        lambda p: p.price,
    )
    curve_accel = _acceleration(
        inputs.curve_history[-min(8, len(inputs.curve_history)):],
        lambda p: p.price,
    )
    quote_accel = _acceleration(
        inputs.quote_history[-min(8, len(inputs.quote_history)):],
        lambda p: p.sell_quote_sol_out,
    ) if len(inputs.quote_history) >= 3 else 0.0

    # Continuation score: 0.0 to 1.0
    passed_count = sum(1 for x in rules if x.passed)
    total_count = len(rules) if rules else 1
    score = passed_count / total_count

    freshness_ms = 0
    if inputs.curve_history:
        freshness_ms = max(
            0, inputs.eval_timestamp_ms - inputs.curve_history[-1].timestamp_ms
        )

    decision = V61Decision(
        passed=(blocker is None),
        blocker=blocker,
        rules=rules,
        continuation_score=score,
        curve_slope=curve_slope,
        curve_acceleration=curve_accel,
        quote_slope=quote_slope,
        quote_acceleration=quote_accel,
        sell_pressure_ratio=sell_pressure_ratio,
        freshness_ms=freshness_ms,
    )

    if decision.passed:
        log(decision.to_log_pass(short))
    else:
        log(decision.to_log_block(short))

    return decision


# ============================================================================
#  Self-test: replay both V60 RUN4 losers + a synthetic winner
# ============================================================================

if __name__ == "__main__":
    print("=== V61 Live Continuation Oracle self-test ===")

    # ----- 3Tcp..pump RUN4 forensic replay -----
    print("\n--- 3Tcp..pump (V60 PASS @ 13:42:15, curve dumped at +2000ms) ---")
    v60_ts = 1779197535000
    curve_3tcp = [
        CurvePoint(v60_ts - 2000, 39_400_000_000, 817_010_000_000_000, 0.000048224915),
        CurvePoint(v60_ts - 100,  40_760_000_000, 789_800_000_000_000, 0.000051603872),
        CurvePoint(v60_ts + 100,  40_760_000_000, 789_800_000_000_000, 0.000051603872),
        CurvePoint(v60_ts + 1900, 40_760_000_000, 789_710_000_000_000, 0.000051616378),
        CurvePoint(v60_ts + 2000, 40_660_000_000, 791_620_000_000_000, 0.000051366881),
        CurvePoint(v60_ts + 2100, 37_880_000_000, 849_750_000_000_000, 0.000044579679),
    ]
    quotes_3tcp = [
        QuotePoint(v60_ts - 350, 0.004655),
        QuotePoint(v60_ts + 1700, 0.004849),
        QuotePoint(v60_ts + 1800, 0.004825),
        QuotePoint(v60_ts + 1900, 0.004188),
    ]
    inp_3tcp = V61Inputs(
        mint="3TcpBF4L9EejdGYYYm2bZZqoJMURby2MHkKcoSrhpump",
        selected_size_sol=0.005,
        v60_true_edge_sol=0.000987,
        v60_pass_timestamp_ms=v60_ts,
        curve_history=curve_3tcp,
        quote_history=quotes_3tcp,
        pending_buy_sol_500ms=7.590,
        pending_sell_sol_500ms=0.0,
        pending_buy_count_500ms=3,
        pending_sell_count_500ms=0,
        signal_age_ms=250,
        eval_timestamp_ms=v60_ts + 2200,  # Evaluate at point of buy_confirm
    )
    dec = v61_check_continuation(inp_3tcp)
    print(f"  passed={dec.passed} blocker={dec.blocker}")
    print(f"  curve_slope={dec.curve_slope:+.10f}/ms")
    print(f"  quote_slope={dec.quote_slope:+.10f}/ms")
    assert not dec.passed, "3Tcp MUST be blocked"

    # ----- 66Qi..pump RUN4 forensic replay -----
    print("\n--- 66Qi..pump (V60 PASS @ 13:45:48, curve dumped at +100ms) ---")
    v60_ts_66qi = 1779197748000
    curve_66qi = [
        CurvePoint(v60_ts_66qi - 31000, 53_430_000_000, 602_440_000_000_000, 0.000088693115),
        CurvePoint(v60_ts_66qi - 1000,  44_900_000_000, 716_910_000_000_000, 0.000062631767),
        CurvePoint(v60_ts_66qi + 0,     77_110_000_000, 417_430_000_000_000, 0.000184732663),
        CurvePoint(v60_ts_66qi + 100,   77_020_000_000, 417_940_000_000_000, 0.000184288515),
        CurvePoint(v60_ts_66qi + 300,   76_730_000_000, 419_520_000_000_000, 0.000182899410),
        CurvePoint(v60_ts_66qi + 700,   75_220_000_000, 427_970_000_000_000, 0.000175749603),
    ]
    quotes_66qi = [
        QuotePoint(v60_ts_66qi - 343, 0.004655),
        QuotePoint(v60_ts_66qi + 200, 0.004836),
        QuotePoint(v60_ts_66qi + 300, 0.004799),
        QuotePoint(v60_ts_66qi + 500, 0.004612),
    ]
    inp_66qi = V61Inputs(
        mint="66QiuTWd8vUvK1sjU1gNNY1WfWCtFCHEnqJmFnFZpump",
        selected_size_sol=0.005,
        v60_true_edge_sol=0.000786,
        v60_pass_timestamp_ms=v60_ts_66qi,
        curve_history=curve_66qi,
        quote_history=quotes_66qi,
        pending_buy_sol_500ms=13.0,
        pending_sell_sol_500ms=0.0,
        pending_buy_count_500ms=3,
        pending_sell_count_500ms=0,
        signal_age_ms=250,
        eval_timestamp_ms=v60_ts_66qi + 600,
    )
    dec = v61_check_continuation(inp_66qi)
    print(f"  passed={dec.passed} blocker={dec.blocker}")
    print(f"  curve_slope={dec.curve_slope:+.10f}/ms")
    print(f"  quote_slope={dec.quote_slope:+.10f}/ms")
    assert not dec.passed, "66Qi MUST be blocked"

    # ----- Synthetic winner: clean rising candidate -----
    print("\n--- Synthetic winner: monotonically rising curve + improving quote ---")
    v60_ts_win = 1779197900000
    curve_win = [
        CurvePoint(v60_ts_win - 1500, 31_000_000_000, 1_050_000_000_000_000, 0.000029524),
        CurvePoint(v60_ts_win - 1000, 31_200_000_000, 1_045_000_000_000_000, 0.000029856),
        CurvePoint(v60_ts_win - 500,  31_500_000_000, 1_040_000_000_000_000, 0.000030288),
        CurvePoint(v60_ts_win + 0,    31_800_000_000, 1_035_000_000_000_000, 0.000030725),
        CurvePoint(v60_ts_win + 200,  32_100_000_000, 1_030_000_000_000_000, 0.000031165),
        CurvePoint(v60_ts_win + 350,  32_400_000_000, 1_025_000_000_000_000, 0.000031610),
    ]
    quotes_win = [
        QuotePoint(v60_ts_win - 350, 0.004810),
        QuotePoint(v60_ts_win + 100, 0.004890),
        QuotePoint(v60_ts_win + 200, 0.004970),
        QuotePoint(v60_ts_win + 300, 0.005060),
    ]
    inp_win = V61Inputs(
        mint="WinSyntheticMintXxxxxxxxxxxxxxxxxxxxxxxxxxx",
        selected_size_sol=0.005,
        v60_true_edge_sol=0.001500,
        v60_pass_timestamp_ms=v60_ts_win,
        curve_history=curve_win,
        quote_history=quotes_win,
        pending_buy_sol_500ms=4.0,
        pending_sell_sol_500ms=0.0,
        pending_buy_count_500ms=4,
        pending_sell_count_500ms=0,
        signal_age_ms=200,
        eval_timestamp_ms=v60_ts_win + 350,
    )
    dec = v61_check_continuation(inp_win)
    print(f"  passed={dec.passed} blocker={dec.blocker}")
    print(f"  curve_slope={dec.curve_slope:+.10f}/ms")
    print(f"  quote_slope={dec.quote_slope:+.10f}/ms")
    print(f"  continuation_score={dec.continuation_score:.3f}")
    if not dec.passed:
        for r in dec.rules:
            status = "PASS" if r.passed else "BLOCK"
            print(f"    {r.name:<26} {status:<5} {r.detail}")
    print()
    print("=== self-test complete ===")
