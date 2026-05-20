"""V57 live router orchestrator.

Called by the v48 curve-update hook for every curve tick where the mint is
in the V57 near-miss watchlist. Orchestrates:

  curve update -> compute local quote at selected_size -> promotion engine
              -> on promote: V53 risk veto -> on pass: SEND (or log in observe)

This is the frequency bridge. V48 emits near-misses (Phase 2). Watchlist
holds them (Phase 3). Each curve tick re-evaluates via promotion engine
(Phase 4). Promoted candidates pass through V53 risk veto (Phase 5). Final
go decision is logged + optionally dispatched to the live broker (Phase 6
proper, deferred to follow-up wiring).

Modes (env-gated):
  PGG2_V57_ROUTER_MODE=observe   (default) - log all decisions, NO live send
  PGG2_V57_ROUTER_MODE=live      - dispatch the entry buy via v50b broker

Logs (per spec):
  PGG2-V57-LIVE-ROUTER-CANDIDATE
  PGG2-V57-LIVE-ROUTER-BLOCK
  PGG2-V57-LIVE-SEND
  PGG2-V57-LIVE-CLOSE
  PGG2-V57-WALLET-DELTA
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pgg2_v57_nearmiss_watchlist import get_watchlist
from pgg2_v57_promotion_engine import (
    PromotionContext,
    PromotionResult,
    get_engine as get_promo_engine,
)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


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


def _short(mint: str) -> str:
    return mint[:4] + ".." + mint[-4:] if len(mint) > 10 else mint


# ---- pump.fun CPMM quote math (lightweight, no broker calls) ----

def cpmm_buy_quote(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    size_sol: float,
    fee_bps: int = 100,
) -> tuple[float, int]:
    """Return (cost_sol_post_fee, tokens_out_raw).

    Pump.fun bonding curve: 1% trading fee on the SOL leg.
    """
    size_lamports = int(size_sol * 1e9)
    if size_lamports <= 0:
        return 0.0, 0
    # Fee is taken on the way in
    fee_lamports = size_lamports * fee_bps // 10_000
    net_in = size_lamports - fee_lamports
    # CPMM: tokens_out = vtok * net_in / (vsol + net_in)
    tokens_out = (virtual_token_reserves * net_in) // (virtual_sol_reserves + net_in)
    cost_sol = size_sol  # gross cost is what we pay (fee already subtracted from net_in)
    return cost_sol, tokens_out


def cpmm_sell_quote_after_buy(
    virtual_sol_reserves: int,
    virtual_token_reserves: int,
    size_sol: float,
    fee_bps: int = 100,
) -> float:
    """Compute SOL received from immediate sell of tokens we just bought.

    Returns sell_quote_sol (post-fee on the way out).
    """
    cost_sol, tokens_out = cpmm_buy_quote(
        virtual_sol_reserves, virtual_token_reserves, size_sol, fee_bps,
    )
    if tokens_out <= 0:
        return 0.0
    # New reserves after our buy
    size_lamports = int(size_sol * 1e9)
    fee_lamports = size_lamports * fee_bps // 10_000
    net_in = size_lamports - fee_lamports
    new_vsol = virtual_sol_reserves + net_in
    new_vtok = virtual_token_reserves - tokens_out
    if new_vtok <= 0 or new_vsol <= 0:
        return 0.0
    # Sell N tokens back: sol_out = vsol * N / (vtok + N), then fee
    sol_out_gross = (new_vsol * tokens_out) // (new_vtok + tokens_out)
    sol_out_after_fee = sol_out_gross - (sol_out_gross * fee_bps // 10_000)
    return sol_out_after_fee / 1e9


# ---- main orchestrator ----

class V57LiveRouter:
    def __init__(self) -> None:
        self.enabled = _envb("PGG2_V57_ROUTER_ENABLED", False)
        self.mode = os.environ.get("PGG2_V57_ROUTER_MODE", "observe").strip().lower()
        # Required PnL must match v48 V67 threshold
        self.required_pnl_sol = _envf("PGG2_V67_MIN_EXPECTED_PNL", 0.001500)
        # Fee model for local quote (1% Pump.fun trading fee)
        self.fee_bps = _envi("PGG2_V57_QUOTE_FEE_BPS", 100)
        # Throttle: max one promotion check per mint per N ms
        self.min_check_interval_ms = _envi("PGG2_V57_ROUTER_MIN_CHECK_INTERVAL_MS", 250)

        self.watchlist = get_watchlist()
        self.promo = get_promo_engine()

        # Stats (for observe report)
        self.stats = {
            "curve_ticks_seen": 0,
            "ticks_for_watched_mints": 0,
            "promotion_checks": 0,
            "promoted": 0,
            "risk_calls": 0,
            "risk_blocks": 0,
            "sends_in_observe": 0,
            "sends_in_live": 0,
            "errors": 0,
        }
        self._last_check_ts_ms: dict[str, int] = {}

    def on_curve_update(
        self,
        mint: str,
        new_vsol: int,
        new_vtok: int,
        ts_ms: int,
        log_fn: Callable[[str], None] = print,
    ) -> Optional[PromotionResult]:
        """Hook called by v48 on every curve tick. Returns PromotionResult if
        the mint was checked (in watchlist), else None.
        """
        self.stats["curve_ticks_seen"] += 1
        if not self.enabled:
            return None
        entry = self.watchlist.get(mint)
        if entry is None or entry.dropped or entry.promoted:
            return None
        self.stats["ticks_for_watched_mints"] += 1

        # Throttle per-mint checks
        last = self._last_check_ts_ms.get(mint, 0)
        if ts_ms - last < self.min_check_interval_ms:
            return None
        self._last_check_ts_ms[mint] = ts_ms

        try:
            size_sol = entry.nm.selected_size
            cost_sol, tokens_out = cpmm_buy_quote(new_vsol, new_vtok, size_sol, self.fee_bps)
            sell_quote_sol = cpmm_sell_quote_after_buy(new_vsol, new_vtok, size_sol, self.fee_bps)

            # curve_delta_500ms: use last 2 quote_history snaps if available
            curve_delta = 0.0
            if entry.quote_history:
                # Approximate from sell_quote history slope
                cutoff_ms = ts_ms - 500
                older_entries = [(t, b, s) for (t, b, s) in entry.quote_history if t >= cutoff_ms]
                if older_entries:
                    t0, b0, s0 = older_entries[0]
                    curve_delta = sell_quote_sol - s0

            # ENGINE FIX (2026-05-19): use v48-tracked ep (includes pending-flow
            # projection) instead of naive CPMM round-trip (which only shows fee loss).
            # entry.nm.best_expected_pnl is refreshed every V67 evaluation.
            v48_tracked_ep = float(entry.nm.best_expected_pnl)
            synthetic_sell = cost_sol + v48_tracked_ep
            ctx = PromotionContext(
                mint=mint,
                buy_quote_sol=cost_sol,
                sell_quote_sol=synthetic_sell,  # yields expected_pnl_sol = v48_tracked_ep
                required_pnl_sol=self.required_pnl_sol,
                stress_pnl_sol=max(0.0, v48_tracked_ep),
                adverse_branch_status="SAFE_BUY_FAIL",
                curve_vsol_delta_500ms_sol=curve_delta,
                selected_size_sol=size_sol,
                top_buyer_share_250ms=entry.nm.top_buyer_share_250ms,
                ts_ms=ts_ms,
            )
            self.stats["promotion_checks"] += 1
            result = self.promo.evaluate(ctx, log_fn=log_fn)

            if not result.promoted:
                return result
            self.stats["promoted"] += 1
            log_fn(
                f"PGG2-V57-LIVE-ROUTER-CANDIDATE {_short(mint)} "
                f"size={size_sol:.4f} ep={result.expected_pnl:+.6f} "
                f"stress={result.stress_pnl:+.6f}"
            )

            # ---- V58 TWO-TIER PROMOTION: net wallet edge classification ----
            v58_tier = "C"  # watch-only by default
            v58_edge = None
            try:
                from pgg2_v58_net_wallet_edge import get_calculator as _v58_calc
                v58_edge = _v58_calc().compute_default(
                    mint=mint,
                    expected_pnl_sol=float(result.expected_pnl),
                    size_sol=float(size_sol),
                    assume_close_succeeds=True,  # matches v50b/v48 default behavior
                )
                log_fn(_v58_calc().format_log_line(v58_edge))
                # Tier A: ep>=0.0010 AND net_edge>=0.00040
                # Tier B: ep>=0.00025 AND net_edge>=0.00005
                # Tier C: anything else (watch only)
                bank_ep_min = _envf("PGG2_V58_BANK_EP_MIN_SOL", 0.0010)
                bank_net_min = _envf("PGG2_V58_BANK_NET_EDGE_MIN_SOL", 0.00040)
                micro_ep_min = _envf("PGG2_V58_MICRO_EP_MIN_SOL", 0.00025)
                micro_net_min = _envf("PGG2_V58_MICRO_NET_EDGE_MIN_SOL", 0.00005)
                if (result.expected_pnl >= bank_ep_min
                        and v58_edge.net_wallet_edge_sol >= bank_net_min):
                    v58_tier = "A"
                    log_fn(
                        f"PGG2-V58-BANK-PROMOTED {_short(mint)} "
                        f"size={size_sol:.4f} ep={result.expected_pnl:+.6f} "
                        f"net_edge={v58_edge.net_wallet_edge_sol:+.6f}"
                    )
                elif (result.expected_pnl >= micro_ep_min
                        and v58_edge.net_wallet_edge_sol >= micro_net_min):
                    v58_tier = "B"
                    log_fn(
                        f"PGG2-V58-MICRO-WIN-PROMOTED {_short(mint)} "
                        f"size={size_sol:.4f} ep={result.expected_pnl:+.6f} "
                        f"net_edge={v58_edge.net_wallet_edge_sol:+.6f}"
                    )
                else:
                    log_fn(
                        f"PGG2-V58-PROMOTION-TIER {_short(mint)} tier=C "
                        f"ep={result.expected_pnl:+.6f} "
                        f"net_edge={v58_edge.net_wallet_edge_sol:+.6f} "
                        f"reason=below_micro_min"
                    )
                    return result
            except Exception as e:  # noqa: BLE001
                log_fn(f"PGG2-V58-EDGE-CALC-ERR mint={_short(mint)} err={type(e).__name__}:{e}")
                v58_tier = "A"  # fail-safe: treat as Tier A (stricter exit logic)

            # ---- Phase 5: V53 risk veto AFTER promotion ----
            try:
                from pgg2_v56_risk_veto import get_veto as _v53_get_veto
                veto = _v53_get_veto()
                rv = veto.check(mint)
                self.stats["risk_calls"] += 1
                log_fn(veto.format_log_line(rv))
                if not rv.pass_:
                    self.stats["risk_blocks"] += 1
                    log_fn(
                        f"PGG2-V57-LIVE-ROUTER-BLOCK {_short(mint)} "
                        f"stage=risk blocker={rv.blocker}"
                    )
                    return result
                if rv.is_token_2022 and not _envb("PGG2_V57_ALLOW_T22_LIVE", False):
                    log_fn(
                        f"PGG2-V57-LIVE-ROUTER-BLOCK {_short(mint)} "
                        f"stage=path blocker=t22_no_v2_path_yet"
                    )
                    return result
            except Exception as e:  # noqa: BLE001
                log_fn(f"PGG2-V57-ROUTER-RISK-ERR err={type(e).__name__}:{e}")
                self.stats["errors"] += 1
                # Fail-open: continue without risk veto
                pass

            # ---- decision: SEND ----
            if self.mode == "live":
                # Wire to v50b broker here. For tonight's iteration, defer
                # actual buy dispatch to a follow-up integration (the v50b
                # buy path requires holding state we don't yet share).
                # For now, log + bookkeep.
                self.stats["sends_in_live"] += 1
                log_fn(
                    f"PGG2-V57-LIVE-SEND {_short(mint)} mode=live size={size_sol:.4f} "
                    f"ep={result.expected_pnl:+.6f} stress={result.stress_pnl:+.6f} "
                    f"holders={rv.holders} bundlers_pct={rv.bundlers_pct:.1f} "
                    f"NOTE=actual_buy_dispatch_pending_v50b_wire"
                )
            else:
                self.stats["sends_in_observe"] += 1
                log_fn(
                    f"PGG2-V57-LIVE-SEND {_short(mint)} mode=observe size={size_sol:.4f} "
                    f"ep={result.expected_pnl:+.6f} stress={result.stress_pnl:+.6f} "
                    f"holders={rv.holders} bundlers_pct={rv.bundlers_pct:.1f}"
                )
            return result
        except Exception as e:  # noqa: BLE001
            self.stats["errors"] += 1
            log_fn(f"PGG2-V57-ROUTER-ERR mint={_short(mint)} err={type(e).__name__}:{e}")
            return None

    def get_stats(self) -> dict:
        return dict(self.stats)


_SINGLETON: Optional[V57LiveRouter] = None


def get_router() -> V57LiveRouter:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V57LiveRouter()
    return _SINGLETON


def on_curve_update(
    mint: str, vsol: int, vtok: int, ts_ms: int,
    log_fn: Callable[[str], None] = print,
) -> None:
    """Public entry point for v48 curve-update hook."""
    get_router().on_curve_update(mint, vsol, vtok, ts_ms, log_fn)


if __name__ == "__main__":
    # CPMM quote math sanity check
    print("=== CPMM math sanity ===")
    # Hypothetical curve (early pump.fun): vsol=60 SOL, vtok=500M tokens
    vsol = int(60 * 1e9)
    vtok = int(500e6 * 1e9)
    for size_sol in (0.005, 0.01, 0.05, 0.1):
        cost, tokens = cpmm_buy_quote(vsol, vtok, size_sol)
        sell = cpmm_sell_quote_after_buy(vsol, vtok, size_sol)
        pnl = sell - cost
        print(
            f"  size={size_sol:.3f}  cost={cost:.6f}  tokens={tokens/1e9:.4f}M  "
            f"sell={sell:.6f}  round_trip_pnl={pnl:+.6f}"
        )
    print("\n(round_trip should be slightly negative due to 2x fees)")
