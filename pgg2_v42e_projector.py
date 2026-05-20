"""V42E — Curve-trigger + Quote-Confirmation Projector.

Wraps V42D projector but drops the 2-account-update gate. The
first positive curve update merely ADDS to the V42E quote-confirmation
watchlist; entry is gated on the QUOTE-CONFIRMATION CHAIN snapshots taken
by `pgg2_v42e_quote_confirmation.QuoteConfirmationWatchlist`.

Output fields (in addition to V42D's projector fields):
  curve_trigger_ts             int  (= watchlist add ts)
  quote_confirm_ts             int  (= last snapshot ts, 0 if no snapshot)
  quote_confirm_pnl            float (= last snapshot all_in_pnl)
  quote_gradient_pnl           float (= last snapshot gradient)
  quote_stress_pnl             float (= last snapshot stress_pnl)
  quote_snapshot_count         int
  quote_snapshots_within_1000ms int
  negative_curve_after_trigger bool
  curve_was_unavailable_at_trigger bool
  recovered_quote_seen         bool
  entry_allowed                bool  (final gate result placeholder for caller
                                       to populate via rule dispatch)

Emits `PGG2-V42E-PROJECTION`.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional, Sequence


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _now_ms() -> int:
    return int(time.time() * 1000)


def project_v42e(
    broker: Any,
    mint_pubkey: str,
    amount_sol: float,
    curve_delta_snap: dict[str, Any],
    watchlist_entry: Optional[Any] = None,
    fusion_snap: Optional[dict[str, Any]] = None,
    pending_events: Optional[Sequence[Any]] = None,
    logger: Optional[Any] = None,
    tx_fee_sol: Optional[float] = None,
) -> dict[str, Any]:
    """Compose V42D projection with V42E quote-confirmation telemetry.

    Hard rule: a single positive curve tick (which triggered the watchlist
    add) does NOT pass entry by itself. The caller must enforce the V42E
    rule gates over the watchlist_entry snapshots. This projector simply
    surfaces both projections side by side.
    """
    short = _short(mint_pubkey)
    try:
        from pgg2_v42d_curve_projector import project_v42d  # type: ignore
    except Exception as exc:
        return {
            "projection_ok": False,
            "projection_error": f"v42d_import:{type(exc).__name__}",
            "entry_allowed": False,
        }

    base = project_v42d(
        broker=broker,
        mint_pubkey=mint_pubkey,
        amount_sol=amount_sol,
        curve_delta_snap=curve_delta_snap,
        fusion_snap=fusion_snap,
        pending_events=pending_events,
        logger=None,  # don't double-log; we emit our own line below
        tx_fee_sol=tx_fee_sol,
    )

    # V42E quote-confirmation overlay.
    curve_trigger_ts = 0
    quote_confirm_ts = 0
    quote_confirm_pnl = 0.0
    quote_gradient_pnl = 0.0
    quote_stress_pnl = 0.0
    quote_snapshot_count = 0
    quote_snapshots_within_1000ms = 0
    negative_curve_after_trigger = False
    curve_was_unavailable_at_trigger = False
    recovered_quote_seen = False

    if watchlist_entry is not None:
        try:
            curve_trigger_ts = int(getattr(watchlist_entry, "trigger_ts_ms", 0) or 0)
            snaps = list(getattr(watchlist_entry, "snapshots", []) or [])
            quote_snapshot_count = len(snaps)
            if snaps:
                last = snaps[-1]
                quote_confirm_ts = int(getattr(last, "taken_ts_ms", 0) or 0)
                quote_confirm_pnl = float(getattr(last, "all_in_pnl", 0.0) or 0.0)
                quote_gradient_pnl = float(getattr(last, "gradient", 0.0) or 0.0)
                quote_stress_pnl = float(getattr(last, "stress_pnl", 0.0) or 0.0)
                quote_snapshots_within_1000ms = sum(
                    1 for s in snaps if int(getattr(s, "age_ms", 0) or 0) <= 1000
                )
            negative_curve_after_trigger = bool(
                getattr(watchlist_entry, "saw_negative_curve_after_trigger", False)
            )
            curve_was_unavailable_at_trigger = bool(
                getattr(watchlist_entry, "curve_was_unavailable_at_trigger", False)
            )
            recovered_quote_seen = bool(
                getattr(watchlist_entry, "recovered_quote_seen", False)
            )
        except Exception:
            pass

    out = dict(base)
    out.update(
        {
            "curve_trigger_ts": int(curve_trigger_ts),
            "quote_confirm_ts": int(quote_confirm_ts),
            "quote_confirm_pnl": float(quote_confirm_pnl),
            "quote_gradient_pnl": float(quote_gradient_pnl),
            "quote_stress_pnl": float(quote_stress_pnl),
            "quote_snapshot_count": int(quote_snapshot_count),
            "quote_snapshots_within_1000ms": int(quote_snapshots_within_1000ms),
            "negative_curve_after_trigger": bool(negative_curve_after_trigger),
            "curve_was_unavailable_at_trigger": bool(curve_was_unavailable_at_trigger),
            "recovered_quote_seen": bool(recovered_quote_seen),
            # entry_allowed is a placeholder: caller computes via rule dispatch.
            "entry_allowed": False,
            "v42e_baseline_source": "accountSubscribe+quote_chain",
        }
    )
    if logger is not None:
        try:
            logger(
                f"PGG2-V42E-PROJECTION mint={short} "
                f"current_state_pnl={base.get('current_state_pnl', 0.0):+.6f} "
                f"live_equiv_v42d={base.get('live_equiv_projected_pnl', 0.0):+.6f} "
                f"stress_v42d={base.get('mean_reversion_stress_pnl', 0.0):+.6f} "
                f"curve_trigger_ts={curve_trigger_ts} "
                f"quote_confirm_ts={quote_confirm_ts} "
                f"quote_confirm_pnl={quote_confirm_pnl:+.6f} "
                f"quote_gradient_pnl={quote_gradient_pnl:+.6f} "
                f"quote_stress_pnl={quote_stress_pnl:+.6f} "
                f"snapshots={quote_snapshot_count} "
                f"snap_in_1s={quote_snapshots_within_1000ms} "
                f"neg_curve_after_trigger={int(negative_curve_after_trigger)} "
                f"curve_unavail_at_trigger={int(curve_was_unavailable_at_trigger)} "
                f"recovered={int(recovered_quote_seen)} "
                f"entry_allowed=0"
            )
        except Exception:
            pass
    return out


def evaluate_rule(
    rule_id: str,
    rule: dict[str, Any],
    proj: dict[str, Any],
    watchlist_entry: Any,
    curve_snap: dict[str, Any],
) -> tuple[bool, str]:
    """V42E rule dispatcher. Returns (ok, fail_reason)."""
    snaps = list(getattr(watchlist_entry, "snapshots", []) or [])
    if rule_id == "v42e_curve_trigger_quote_confirmed":
        if rule.get("require_first_positive_curve_update", True):
            if int(curve_snap.get("positive_updates_within_1000ms", 0)) < 1 \
                    and int(curve_snap.get("positive_delta_streak", 0)) < 1:
                return False, "no_first_positive_curve_update"
        snaps_in_1s = [s for s in snaps if int(getattr(s, "age_ms", 0) or 0) <= 1000
                       and getattr(s, "error", "") == ""]
        if len(snaps_in_1s) < int(rule.get("min_quote_snapshots_within_1000ms", 2)):
            return False, "below_min_quote_snapshots_1000ms"
        s1, s2 = snaps_in_1s[0], snaps_in_1s[1]
        if float(getattr(s1, "all_in_pnl", -999.0)) < float(rule.get("snapshot_1_min_all_in_pnl", 0.00020)):
            return False, "snapshot_1_pnl_below_min"
        if float(getattr(s2, "all_in_pnl", -999.0)) < float(rule.get("snapshot_2_min_all_in_pnl", 0.00060)):
            return False, "snapshot_2_pnl_below_min"
        max_decline = float(rule.get("max_snapshot_2_below_snapshot_1", 0.00010))
        if float(getattr(s1, "all_in_pnl", 0.0)) - float(getattr(s2, "all_in_pnl", 0.0)) > max_decline:
            return False, "snapshot_2_dropped_too_far_below_snapshot_1"
        # Use the LATER (snapshot_2) freshness numbers.
        if int(getattr(s2, "quote_age_ms", 9999) or 0) > int(rule.get("max_quote_age_ms", 600)):
            return False, "quote_age_too_old"
        if int(getattr(s2, "quote_sell_latency_ms", 9999) or 0) > int(rule.get("max_sell_quote_latency_ms", 750)):
            return False, "sell_quote_latency_too_high"
        if rule.get("require_route_pump_bc", True) and str(getattr(s2, "route", "")) != "pump_bc":
            return False, "route_not_pump_bc"
        if rule.get("require_sim_needed_zero", True) and int(getattr(s2, "sim_needed", 1)) != 0:
            return False, "sim_needed_nonzero"
        if rule.get("require_source_late_false", True) and bool(getattr(s2, "is_late", True)):
            return False, "source_is_late"
        if rule.get("forbid_negative_curve_after_watchlist_start", True) and bool(
            getattr(watchlist_entry, "saw_negative_curve_after_trigger", False)
        ):
            return False, "negative_curve_after_watchlist_start"
        return True, ""

    if rule_id == "v42e_quote_gradient_confirmed":
        # Require >= N consecutive snapshots with strictly increasing all_in_pnl.
        min_consec = int(rule.get("min_consecutive_increasing_sell_quote_snapshots", 2))
        ok_snaps = [s for s in snaps if getattr(s, "error", "") == ""]
        if len(ok_snaps) < min_consec:
            return False, "below_min_quote_snapshots"
        # Find the longest tail with strictly increasing all_in_pnl.
        consec = 1
        best = 1
        for i in range(1, len(ok_snaps)):
            if float(ok_snaps[i].all_in_pnl) > float(ok_snaps[i - 1].all_in_pnl):
                consec += 1
                best = max(best, consec)
            else:
                consec = 1
        if best < min_consec:
            return False, "no_consecutive_increasing_quotes"
        last = ok_snaps[-1]
        if float(getattr(last, "all_in_pnl", -999.0)) < float(rule.get("min_live_equiv_all_in_pnl", 0.00060)):
            return False, "live_equiv_all_in_pnl_below_min"
        if float(getattr(last, "stress_pnl", -999.0)) < float(rule.get("min_stress_pnl", 0.00020)):
            return False, "stress_pnl_below_min"
        if rule.get("require_positive_curve_update_within_1500ms", True):
            # require any positive curve update in the last 1500ms window.
            dprice_1500 = float(curve_snap.get("curve_price_delta_1000ms", 0.0))
            if dprice_1500 <= 0:
                return False, "no_positive_curve_update_within_1500ms"
        if rule.get("require_route_pump_bc", True) and str(getattr(last, "route", "")) != "pump_bc":
            return False, "route_not_pump_bc"
        if rule.get("require_sim_needed_zero", True) and int(getattr(last, "sim_needed", 1)) != 0:
            return False, "sim_needed_nonzero"
        return True, ""

    if rule_id == "v42e_high_edge_single_confirm":
        if rule.get("require_first_positive_curve_update", True):
            if int(curve_snap.get("positive_updates_within_1000ms", 0)) < 1 \
                    and int(curve_snap.get("positive_delta_streak", 0)) < 1:
                return False, "no_first_positive_curve_update"
        ok_snaps = [s for s in snaps if getattr(s, "error", "") == ""]
        if not ok_snaps:
            return False, "no_snapshot"
        max_pnl = max(float(getattr(s, "all_in_pnl", -999.0)) for s in ok_snaps)
        if max_pnl < float(rule.get("min_single_snapshot_all_in_pnl", 0.00250)):
            return False, "max_snapshot_pnl_below_high_min"
        # find the snapshot with max_pnl
        for s in ok_snaps:
            if abs(float(s.all_in_pnl) - max_pnl) < 1e-12:
                top = s
                break
        else:
            top = ok_snaps[-1]
        if float(getattr(top, "stress_pnl", -999.0)) < float(rule.get("min_stress_pnl", 0.00100)):
            return False, "stress_pnl_below_high_min"
        if rule.get("forbid_negative_curve_after_trigger", True) and bool(
            getattr(watchlist_entry, "saw_negative_curve_after_trigger", False)
        ):
            return False, "negative_curve_after_trigger"
        if rule.get("require_route_pump_bc", True) and str(getattr(top, "route", "")) != "pump_bc":
            return False, "route_not_pump_bc"
        if rule.get("require_sim_needed_zero", True) and int(getattr(top, "sim_needed", 1)) != 0:
            return False, "sim_needed_nonzero"
        return True, ""

    if rule_id == "v42e_recovered_quote_confirmed":
        if rule.get("require_quote_was_curve_missing_or_unavailable", True) and not bool(
            getattr(watchlist_entry, "curve_was_unavailable_at_trigger", False)
        ):
            return False, "curve_was_available_at_trigger"
        if rule.get("require_quote_recovered", True) and not bool(
            getattr(watchlist_entry, "recovered_quote_seen", False)
        ):
            return False, "quote_not_recovered"
        ok_snaps = [s for s in snaps if getattr(s, "error", "") == ""]
        if not ok_snaps:
            return False, "no_snapshot"
        last = ok_snaps[-1]
        if float(getattr(last, "all_in_pnl", -999.0)) < float(rule.get("min_recovered_live_equiv_all_in_pnl", 0.00060)):
            return False, "recovered_live_equiv_pnl_below_min"
        if rule.get("min_second_snapshot_nonneg", True) and len(ok_snaps) >= 2:
            if float(getattr(ok_snaps[1], "all_in_pnl", -999.0)) < 0.0:
                return False, "second_snapshot_negative"
        if rule.get("require_route_pump_bc", True) and str(getattr(last, "route", "")) != "pump_bc":
            return False, "route_not_pump_bc"
        if rule.get("require_sim_needed_zero", True) and int(getattr(last, "sim_needed", 1)) != 0:
            return False, "sim_needed_nonzero"
        return True, ""

    return False, "unknown_rule_id"


__all__ = ["project_v42e", "evaluate_rule"]
