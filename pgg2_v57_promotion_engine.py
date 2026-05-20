"""V57 local promotion engine.

For every watched mint, on every local CPMM curve update from the v48 harness:
  - recompute local buy/sell quote at selected size
  - recompute expected_pnl
  - recompute stress_pnl (adverse branch)
  - check momentum since watchlist add
  - decide PROMOTE / HOLD / DROP

Promotion requires ALL of:
  - expected_pnl >= required_pnl
  - stress_pnl >= 0
  - quote gradient positive or non-negative (buy quote not worsening)
  - curve delta positive in last 500ms
  - no negative curve update after watchlist add (tracked by watchlist)
  - not dead-flat: local sell quote must IMPROVE after watchlist add
  - selected size must still pass V47F/V47D/V47E caps (caller responsibility)
  - adverse branch SAFE_BUY_FAIL or non-negative

Logs:
  PGG2-V57-PROMOTION-CHECK  (every evaluation)
  PGG2-V57-PROMOTED         (when all conditions pass)
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pgg2_v57_nearmiss_watchlist import (
    V57NearMissWatchlist, WatchlistEntry, get_watchlist,
)


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
class PromotionContext:
    """Live state the v48 harness provides on each curve update for a watched mint."""
    mint: str
    # Local CPMM quote results at the candidate's selected size
    buy_quote_sol: float            # SOL cost to buy `selected_size_sol` worth of tokens
    sell_quote_sol: float           # SOL received if we sell those tokens immediately
    # Required PnL (env-driven, must match v48 V67 threshold)
    required_pnl_sol: float
    # Stress branch from V47B
    stress_pnl_sol: float
    adverse_branch_status: str   # "SAFE_BUY_FAIL" | "BUY_OK_SELL_FAIL" | "BUY_OK_SELL_OK" | ...
    # Curve delta over last 500ms (signed, SOL units)
    curve_vsol_delta_500ms_sol: float
    # Selected size (must match V47F/V47D/V47E caps already)
    selected_size_sol: float
    # V47C top-share at evaluation time (drop if it shoots up)
    top_buyer_share_250ms: float = 0.0
    # Optional: time of context (default=now)
    ts_ms: int = 0

    def __post_init__(self) -> None:
        if self.ts_ms == 0:
            self.ts_ms = int(time.time() * 1000)

    @property
    def expected_pnl_sol(self) -> float:
        """Current expected PnL at the snapshotted size."""
        return self.sell_quote_sol - self.buy_quote_sol


@dataclass
class PromotionResult:
    mint: str
    promoted: bool
    blocker: Optional[str]
    expected_pnl: float
    stress_pnl: float
    quote_gradient: float
    quote_improvement_since_add: float
    curve_delta_500ms: float
    reasons_passed: list[str]


class V57PromotionEngine:
    def __init__(self) -> None:
        self.enabled = _envb("PGG2_V57_PROMOTION_ENABLED", True)
        # Spec defaults
        self.min_quote_gradient = _envf("PGG2_V57_PROMOTION_MIN_QUOTE_GRADIENT_SOL", 0.0)
        self.min_curve_delta_500ms = _envf(
            "PGG2_V57_PROMOTION_MIN_CURVE_DELTA_500MS_SOL", 0.0
        )
        # Sell-quote must improve by at least this much since add
        self.min_sell_improvement_since_add = _envf(
            "PGG2_V57_PROMOTION_MIN_SELL_IMPROVEMENT_SOL", 0.0
        )
        # Top-buyer-share blow-up cutoff (if concentration spikes post-add, drop)
        self.max_top_buyer_share = _envf(
            "PGG2_V57_PROMOTION_MAX_TOP_BUYER_SHARE", 0.65
        )
        # Adverse-branch allowed states
        allowed_default = "SAFE_BUY_FAIL,BUY_OK_SELL_OK"
        self.allowed_adverse = set(
            os.environ.get("PGG2_V57_PROMOTION_ALLOWED_ADVERSE", allowed_default).split(",")
        )
        self.watchlist = get_watchlist()

    def _short(self, mint: str) -> str:
        return mint[:4] + ".." + mint[-4:] if len(mint) > 10 else mint

    def _quote_gradient(self, entry: WatchlistEntry, ctx: PromotionContext) -> float:
        """Slope of (sell - buy) over last few quote updates (positive = improving)."""
        if not entry.quote_history:
            return 0.0
        if len(entry.quote_history) < 2:
            ts, b0, s0 = entry.quote_history[0]
            cur_net = ctx.sell_quote_sol - ctx.buy_quote_sol
            return cur_net - (s0 - b0)
        # Most recent vs earliest in history
        ts0, b0, s0 = entry.quote_history[0]
        ts1, b1, s1 = entry.quote_history[-1]
        if ts1 == ts0:
            return 0.0
        net1 = s1 - b1
        net0 = s0 - b0
        return net1 - net0

    def evaluate(
        self,
        ctx: PromotionContext,
        log_fn: Callable[[str], None] = print,
    ) -> PromotionResult:
        sh = self._short(ctx.mint)

        entry = self.watchlist.get(ctx.mint)
        if entry is None or entry.dropped or entry.promoted:
            reason = "no_entry" if entry is None else ("already_promoted" if entry.promoted else "already_dropped")
            r = PromotionResult(
                mint=ctx.mint, promoted=False, blocker=reason,
                expected_pnl=ctx.expected_pnl_sol, stress_pnl=ctx.stress_pnl_sol,
                quote_gradient=0.0, quote_improvement_since_add=0.0,
                curve_delta_500ms=ctx.curve_vsol_delta_500ms_sol, reasons_passed=[],
            )
            return r

        # Track the quote in the watchlist (also drops on negative curve)
        is_neg_curve = ctx.curve_vsol_delta_500ms_sol < 0.0
        self.watchlist.on_curve_update(
            ctx.mint, ctx.buy_quote_sol, ctx.sell_quote_sol, is_neg_curve, log_fn
        )

        # Refresh entry after possible drop
        entry = self.watchlist.get(ctx.mint)
        if entry is None or entry.dropped:
            r = PromotionResult(
                mint=ctx.mint, promoted=False, blocker="dropped_during_curve_update",
                expected_pnl=ctx.expected_pnl_sol, stress_pnl=ctx.stress_pnl_sol,
                quote_gradient=0.0, quote_improvement_since_add=0.0,
                curve_delta_500ms=ctx.curve_vsol_delta_500ms_sol, reasons_passed=[],
            )
            log_fn(
                f"PGG2-V57-PROMOTION-CHECK {sh} pass=0 blocker=dropped_during_curve_update"
            )
            return r

        ep = ctx.expected_pnl_sol
        grad = self._quote_gradient(entry, ctx)
        # Sell quote improvement since add (first entry vs current)
        if entry.quote_history:
            ts0, b0, s0 = entry.quote_history[0]
            sell_imp = ctx.sell_quote_sol - s0
        else:
            sell_imp = 0.0

        reasons_passed = []
        blocker: Optional[str] = None

        # 1) PnL >= required
        if ep < ctx.required_pnl_sol:
            blocker = f"ep_below_required({ep:+.6f}<{ctx.required_pnl_sol:+.6f})"
        else:
            reasons_passed.append("ep_ge_required")

        # 2) Stress PnL >= 0
        if blocker is None and ctx.stress_pnl_sol < 0:
            blocker = f"stress_negative({ctx.stress_pnl_sol:+.6f})"
        elif blocker is None:
            reasons_passed.append("stress_ge_0")

        # 3) Adverse branch allowed
        if blocker is None and ctx.adverse_branch_status not in self.allowed_adverse:
            blocker = f"adverse_branch({ctx.adverse_branch_status})"
        elif blocker is None:
            reasons_passed.append(f"adverse_ok({ctx.adverse_branch_status})")

        # 4) Quote gradient non-negative
        if blocker is None and grad < self.min_quote_gradient:
            blocker = f"quote_gradient_neg({grad:+.6f})"
        elif blocker is None:
            reasons_passed.append("gradient_ok")

        # 5) Curve delta positive in last 500ms
        if blocker is None and ctx.curve_vsol_delta_500ms_sol < self.min_curve_delta_500ms:
            blocker = f"curve_delta_neg({ctx.curve_vsol_delta_500ms_sol:+.6f})"
        elif blocker is None:
            reasons_passed.append("curve_delta_pos")

        # 6) Sell quote must IMPROVE since add (not dead-flat)
        if blocker is None and sell_imp < self.min_sell_improvement_since_add:
            blocker = f"sell_quote_flat_or_worse({sell_imp:+.6f})"
        elif blocker is None:
            reasons_passed.append("sell_improved")

        # 7) Top-buyer share not blown up
        if (
            blocker is None
            and ctx.top_buyer_share_250ms > self.max_top_buyer_share
        ):
            blocker = f"top_share_blowup({ctx.top_buyer_share_250ms:.3f}>{self.max_top_buyer_share})"
        elif blocker is None:
            reasons_passed.append("concentration_ok")

        promoted = blocker is None
        log_fn(
            f"PGG2-V57-PROMOTION-CHECK {sh} pass={int(promoted)} "
            f"ep={ep:+.6f} stress={ctx.stress_pnl_sol:+.6f} "
            f"grad={grad:+.6f} sell_imp={sell_imp:+.6f} "
            f"curve_delta={ctx.curve_vsol_delta_500ms_sol:+.6f} "
            f"adverse={ctx.adverse_branch_status} "
            f"top={ctx.top_buyer_share_250ms:.3f} "
            f"blocker={blocker or '-'}"
        )

        if promoted:
            self.watchlist.mark_promoted(ctx.mint)
            log_fn(
                f"PGG2-V57-PROMOTED {sh} ep={ep:+.6f} stress={ctx.stress_pnl_sol:+.6f} "
                f"size={ctx.selected_size_sol:.4f} adverse={ctx.adverse_branch_status} "
                f"reasons={'+'.join(reasons_passed)}"
            )

        return PromotionResult(
            mint=ctx.mint, promoted=promoted, blocker=blocker,
            expected_pnl=ep, stress_pnl=ctx.stress_pnl_sol,
            quote_gradient=grad, quote_improvement_since_add=sell_imp,
            curve_delta_500ms=ctx.curve_vsol_delta_500ms_sol,
            reasons_passed=reasons_passed,
        )


_SINGLETON: Optional[V57PromotionEngine] = None


def get_engine() -> V57PromotionEngine:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V57PromotionEngine()
    return _SINGLETON


if __name__ == "__main__":
    # Smoke test: admit a near-miss, then simulate two curve updates,
    # the second of which makes it pass.
    import time as _t
    from pgg2_v57_nearmiss_watchlist import V67NearMiss, get_watchlist

    wl = get_watchlist()
    engine = get_engine()

    nm = V67NearMiss(
        mint="TESTpump" + "Z" * 36, ts_ms=int(_t.time() * 1000),
        best_expected_pnl=-0.00010, required_pnl=0.00150, selected_size=0.005,
        route="pump_bc", sim_needed=0, pair_source="current_sig",
        unique_buyers_250ms=3, top_buyer_share_250ms=0.20,
        pending_buy_sol_1000ms=0.5, blocker_reason="no_selectable_size",
    )
    entry = wl.admit(nm)
    print(f"\n--> watchlist admit: ok={entry is not None}")

    print("\n=== Tick 1: still slightly negative, no improvement yet ===")
    ctx1 = PromotionContext(
        mint=nm.mint, buy_quote_sol=0.00504, sell_quote_sol=0.00500,
        required_pnl_sol=0.00150, stress_pnl_sol=0.0001,
        adverse_branch_status="SAFE_BUY_FAIL",
        curve_vsol_delta_500ms_sol=0.0,
        selected_size_sol=0.005, top_buyer_share_250ms=0.20,
    )
    r1 = engine.evaluate(ctx1)
    print(f"\n--> tick1: promoted={r1.promoted} blocker={r1.blocker}")

    print("\n=== Tick 2: PnL flips positive, curve delta positive, sell improves ===")
    ctx2 = PromotionContext(
        mint=nm.mint, buy_quote_sol=0.00500, sell_quote_sol=0.00665,
        required_pnl_sol=0.00150, stress_pnl_sol=0.0010,
        adverse_branch_status="SAFE_BUY_FAIL",
        curve_vsol_delta_500ms_sol=0.30,
        selected_size_sol=0.005, top_buyer_share_250ms=0.20,
    )
    r2 = engine.evaluate(ctx2)
    print(f"\n--> tick2: promoted={r2.promoted} blocker={r2.blocker} reasons={r2.reasons_passed}")
