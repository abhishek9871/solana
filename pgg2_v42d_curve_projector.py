"""V42D — Persistent-flow Curve Projector with anti-mean-reversion stress.

Wraps the V42C projector to STOP DOUBLE-COUNTING the last observed positive
curve delta as future flow. V42C synthesised a `PendingFlowAtom(buy,
sol=vsol_delta_1000ms)` and applied it ON TOP of the current curve — but the
vsol growth represented by that delta is ALREADY reflected in the current
curve state. Net: ~3× over-application.

V42D fixes this by:
  1. Computing `current_state_pnl` from the CURRENT curve only (no synthesis,
     empty pending_events).
  2. Computing `pending_flow_pnl` only from REAL shred buy atoms whose
     `ts_ms > curve_snap['last_poll_ts_ms']` (i.e. arrived AFTER the curve
     snapshot was last refreshed and therefore not yet baked in).
  3. Computing `persistent_trend_pnl` only when `positive_delta_streak >= 2`
     OR `positive_updates_within_1000ms >= 2` — a multi-tick uptrend gets to
     credit a single additional follow-up buy equal to the AVERAGE recent
     window delta (not the full 1000ms sum).
  4. Applying a `one_tick_spike_penalty` when only 1 positive update is
     observed: estimate mean revert at 0.5× the last delta and subtract from
     PnL.
  5. Computing `mean_reversion_stress_pnl` by projecting the next curve
     update as a 0.5× adverse mirror SELL of the last positive delta.
  6. The composite `live_equiv_projected_pnl` =
       current_state_pnl + pending_flow_pnl + persistent_trend_pnl
       − one_tick_spike_penalty.

A single positive curve update may add the mint to the confirmation
watchlist (Phase 4) but DOES NOT add to the projected PnL on its own.

Emits `PGG2-V42D-PROJECTION` with all six fields.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional, Sequence


LAMPORTS_PER_SOL = 1_000_000_000


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _filter_pending_atoms_post_curve(
    pending_events: Sequence[Any],
    curve_last_poll_ts_ms: int,
) -> list[Any]:
    """Return only atoms whose `ts_ms > curve_last_poll_ts_ms`. These are
    the only atoms that represent flow not yet reflected in the current
    curve state. Earlier atoms ARE already baked in (or were never seen on
    the curve and are pure noise — either way, don't apply them on top).
    """
    if not pending_events:
        return []
    out: list[Any] = []
    cutoff = int(curve_last_poll_ts_ms)
    for a in pending_events:
        try:
            ts = int(getattr(a, "ts_ms", 0) or 0)
        except Exception:
            ts = 0
        # Only count buys; sells are not "future buy flow".
        side = str(getattr(a, "side", "") or "").lower()
        if side != "buy":
            continue
        if ts > cutoff:
            out.append(a)
    return out


def _persistent_trend_atoms(
    curve_snap: dict[str, Any],
) -> list[Any]:
    """If we've seen >=2 non-negative deltas within 1000ms, project ONE
    follow-up buy equal to the AVERAGE per-update vsol growth (not the
    cumulative sum, which would re-apply the already-baked growth).
    """
    pos_updates = int(curve_snap.get("positive_updates_within_1000ms", 0))
    streak = int(curve_snap.get("positive_delta_streak", 0))
    if pos_updates < 2 and streak < 2:
        return []
    vsol_d_1000 = int(curve_snap.get("vsol_delta_1000ms", 0))
    if vsol_d_1000 <= 0:
        return []
    # Average size per positive update.
    denom = max(1, pos_updates)
    avg_sol = (float(vsol_d_1000) / LAMPORTS_PER_SOL) / float(denom)
    # Be conservative: cap at 0.005 SOL of credit.
    avg_sol = min(0.005, avg_sol)
    if avg_sol < 0.0001:
        return []
    try:
        from pgg2_v42_curve_projector import PendingFlowAtom  # type: ignore
    except Exception:
        return []
    return [PendingFlowAtom(ts_ms=_now_ms() - 100, side="buy", sol=avg_sol)]


def _mean_reversion_adverse_atoms(
    curve_snap: dict[str, Any],
) -> list[Any]:
    """Build a 0.5× adverse mirror SELL of the last positive curve delta.
    This is the "what if the next tick mean-reverts" scenario.
    """
    vsol_d_1000 = int(curve_snap.get("vsol_delta_1000ms", 0))
    if vsol_d_1000 <= 0:
        # No recent positive delta — adverse case is "0" by default.
        return []
    mirror_sol = (float(vsol_d_1000) / LAMPORTS_PER_SOL) * 0.5
    if mirror_sol <= 0.0:
        return []
    try:
        from pgg2_v42_curve_projector import PendingFlowAtom  # type: ignore
    except Exception:
        return []
    return [PendingFlowAtom(ts_ms=_now_ms() - 50, side="sell", sol=mirror_sol)]


def _project_with_atoms(
    broker: Any,
    mint_pubkey: str,
    amount_sol: float,
    fusion_snap: dict[str, Any],
    curve_delta_snap: dict[str, Any],
    atoms: list[Any],
    tx_fee_sol: Optional[float],
) -> dict[str, Any]:
    """Run project_v42b in V42C baseline-injection mode with the given atoms."""
    from pgg2_v42b_curve_projector import project_v42b  # type: ignore
    from pgg2_direct_pump import PumpBondingCurve, as_pubkey, pda, PUMP_PROGRAM_ID  # type: ignore

    vsol = int(curve_delta_snap.get("virtual_sol_reserves", 0))
    vtok = int(curve_delta_snap.get("virtual_token_reserves", 0))
    rsol = int(curve_delta_snap.get("real_sol_reserves", 0))
    rtok = int(curve_delta_snap.get("real_token_reserves", 0))
    if vsol <= 0 or vtok <= 0:
        return {
            "projection_ok": False,
            "projection_ok_baseline": False,
            "projected_all_in_pnl": -999.0,
            "projected_pnl_stress": -999.0,
            "projected_tokens_raw": 0,
            "projected_buy_fee_sol": 0.0,
        }
    mint_pk = as_pubkey(mint_pubkey)
    curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint_pk))
    override_curve = PumpBondingCurve(
        key=curve_key,
        virtual_token_reserves=vtok,
        virtual_sol_reserves=vsol,
        real_token_reserves=rtok,
        real_sol_reserves=rsol,
        token_total_supply=0,
        complete=False,
        creator=curve_key,
        is_mayhem=False,
        cashback_enabled=False,
    )
    orig_bc = broker.bonding_curve
    def _bc_override(_mint_pk: Any, _orig=orig_bc, _ov=override_curve, _expected=mint_pk):
        try:
            if str(_mint_pk) == str(_expected):
                return _ov
        except Exception:
            pass
        return _orig(_mint_pk)
    broker.bonding_curve = _bc_override  # type: ignore[assignment]
    try:
        out = project_v42b(
            broker=broker,
            mint_pubkey=mint_pubkey,
            amount_sol=amount_sol,
            fusion_snap=fusion_snap,
            curve_delta_snap=curve_delta_snap,
            pending_events=atoms,
            logger=None,
            tx_fee_sol=tx_fee_sol,
            pnl_floor_sol=0.00020,
            stress_pnl_floor_sol=0.00020,
            stress_latency_ms=0,
        )
    finally:
        broker.bonding_curve = orig_bc  # type: ignore[assignment]
    return out


def project_v42d(
    broker: Any,
    mint_pubkey: str,
    amount_sol: float,
    curve_delta_snap: dict[str, Any],
    fusion_snap: Optional[dict[str, Any]] = None,
    pending_events: Optional[Sequence[Any]] = None,
    logger: Optional[Any] = None,
    tx_fee_sol: Optional[float] = None,
) -> dict[str, Any]:
    """Compute the V42D 6-component projection.

    Output dict keys:
      projection_ok                  bool
      projection_ok_baseline         bool (compat)
      current_state_pnl              float
      pending_flow_pnl               float (delta vs current_state from real post-snap atoms)
      persistent_trend_pnl           float (delta vs current_state from streak avg follow-up)
      one_tick_spike_penalty         float (positive number to subtract)
      mean_reversion_stress_pnl      float (absolute PnL with adverse atom applied)
      live_equiv_projected_pnl       float
      projected_all_in_pnl           float (alias = live_equiv_projected_pnl) for rule-engine compat
      projected_pnl_stress           float (alias = mean_reversion_stress_pnl)
      projected_max_loss             float (min of live_equiv and stress)
      projected_tokens_raw           int   (from current_state projection)
      projected_buy_fee_sol          float
      v42d_anti_onetick_active       bool
      v42d_persistent_trend_active   bool
      v42d_pending_flow_atoms_kept   int
      v42d_pending_flow_atoms_seen   int
      lead_confidence                float (from baseline projection)
      lead_meta                      dict
      v42c_baseline_source           "accountSubscribe"
    """
    fusion_snap = fusion_snap or {}
    short = _short(mint_pubkey)
    last_poll_ts = int(curve_delta_snap.get("last_poll_ts_ms", 0))

    # 1) current_state_pnl: no atoms, pure current curve.
    base = _project_with_atoms(
        broker=broker,
        mint_pubkey=mint_pubkey,
        amount_sol=amount_sol,
        fusion_snap=fusion_snap,
        curve_delta_snap=curve_delta_snap,
        atoms=[],
        tx_fee_sol=tx_fee_sol,
    )
    if not base.get("projection_ok_baseline", base.get("projection_ok", False)):
        out = {
            "projection_ok": False,
            "projection_ok_baseline": False,
            "projection_error": base.get("projection_error", "baseline_unproj"),
            "current_state_pnl": -999.0,
            "pending_flow_pnl": 0.0,
            "persistent_trend_pnl": 0.0,
            "one_tick_spike_penalty": 0.0,
            "mean_reversion_stress_pnl": -999.0,
            "live_equiv_projected_pnl": -999.0,
            "projected_all_in_pnl": -999.0,
            "projected_pnl_stress": -999.0,
            "projected_max_loss": -999.0,
            "projected_tokens_raw": 0,
            "projected_buy_fee_sol": 0.0,
            "v42d_anti_onetick_active": False,
            "v42d_persistent_trend_active": False,
            "v42d_pending_flow_atoms_kept": 0,
            "v42d_pending_flow_atoms_seen": int(len(pending_events or [])),
            "lead_confidence": 0.0,
            "lead_meta": {},
            "v42c_baseline_source": "accountSubscribe",
        }
        if logger is not None:
            try:
                logger(
                    f"PGG2-V42D-PROJECTION mint={short} status=baseline_unproj "
                    f"err={base.get('projection_error','')}"
                )
            except Exception:
                pass
        return out

    current_state_pnl = float(base.get("projected_all_in_pnl", -999.0))
    tokens_raw = int(base.get("projected_tokens_raw", 0))
    buy_fee_sol = float(base.get("projected_buy_fee_sol", 0.0))
    lead_conf = float(base.get("lead_confidence", 0.0))
    lead_meta = dict(base.get("lead_meta", {}))

    # 2) pending_flow_pnl: REAL shred atoms arrived AFTER curve snapshot.
    post_snap_atoms = _filter_pending_atoms_post_curve(
        pending_events or [], last_poll_ts
    )
    pending_flow_pnl = 0.0
    if post_snap_atoms:
        try:
            pf = _project_with_atoms(
                broker=broker,
                mint_pubkey=mint_pubkey,
                amount_sol=amount_sol,
                fusion_snap=fusion_snap,
                curve_delta_snap=curve_delta_snap,
                atoms=post_snap_atoms,
                tx_fee_sol=tx_fee_sol,
            )
            if pf.get("projection_ok_baseline", pf.get("projection_ok", False)):
                pending_flow_pnl = float(pf.get("projected_all_in_pnl", current_state_pnl)) - current_state_pnl
        except Exception:
            pending_flow_pnl = 0.0
    # Hard rule: only credit POSITIVE delta from pending flow. Negative pending
    # (sells) reduce via the explicit stress path, not via this path.
    pending_flow_pnl = max(0.0, pending_flow_pnl)

    # 3) persistent_trend_pnl: only when streak >= 2 OR positive_updates_within_1000ms >= 2.
    persistent_atoms = _persistent_trend_atoms(curve_delta_snap)
    persistent_active = bool(persistent_atoms)
    persistent_trend_pnl = 0.0
    if persistent_atoms:
        try:
            pt = _project_with_atoms(
                broker=broker,
                mint_pubkey=mint_pubkey,
                amount_sol=amount_sol,
                fusion_snap=fusion_snap,
                curve_delta_snap=curve_delta_snap,
                atoms=persistent_atoms,
                tx_fee_sol=tx_fee_sol,
            )
            if pt.get("projection_ok_baseline", pt.get("projection_ok", False)):
                persistent_trend_pnl = float(pt.get("projected_all_in_pnl", current_state_pnl)) - current_state_pnl
        except Exception:
            persistent_trend_pnl = 0.0
    persistent_trend_pnl = max(0.0, persistent_trend_pnl)

    # 4) one_tick_spike_penalty: estimated mean-revert when only 1 positive update.
    pos_updates = int(curve_delta_snap.get("positive_updates_within_1000ms", 0))
    streak = int(curve_delta_snap.get("positive_delta_streak", 0))
    anti_onetick = bool(int(os.environ.get("PGG2_V42D_ANTI_ONETICK_SPIKE", "1")) and pos_updates < 2 and streak < 2)
    one_tick_spike_penalty = 0.0
    if anti_onetick:
        # Estimate adverse: 0.5× the last positive vsol delta as a sell.
        rev_atoms = _mean_reversion_adverse_atoms(curve_delta_snap)
        if rev_atoms:
            try:
                rv = _project_with_atoms(
                    broker=broker,
                    mint_pubkey=mint_pubkey,
                    amount_sol=amount_sol,
                    fusion_snap=fusion_snap,
                    curve_delta_snap=curve_delta_snap,
                    atoms=rev_atoms,
                    tx_fee_sol=tx_fee_sol,
                )
                if rv.get("projection_ok_baseline", rv.get("projection_ok", False)):
                    # Penalty is the shortfall vs current_state_pnl.
                    rv_pnl = float(rv.get("projected_all_in_pnl", current_state_pnl))
                    one_tick_spike_penalty = max(0.0, current_state_pnl - rv_pnl)
            except Exception:
                one_tick_spike_penalty = 0.0

    # 5) mean_reversion_stress_pnl: absolute PnL with 0.5× adverse mirror applied.
    # Always run, regardless of anti_onetick toggle.
    rev_atoms_for_stress = _mean_reversion_adverse_atoms(curve_delta_snap)
    if rev_atoms_for_stress:
        try:
            ms = _project_with_atoms(
                broker=broker,
                mint_pubkey=mint_pubkey,
                amount_sol=amount_sol,
                fusion_snap=fusion_snap,
                curve_delta_snap=curve_delta_snap,
                atoms=rev_atoms_for_stress,
                tx_fee_sol=tx_fee_sol,
            )
            if ms.get("projection_ok_baseline", ms.get("projection_ok", False)):
                mean_reversion_stress_pnl = float(ms.get("projected_all_in_pnl", current_state_pnl))
            else:
                mean_reversion_stress_pnl = current_state_pnl
        except Exception:
            mean_reversion_stress_pnl = current_state_pnl
    else:
        # No recent positive delta -> stress == current.
        mean_reversion_stress_pnl = current_state_pnl

    # 6) live_equiv_projected_pnl
    live_equiv_projected_pnl = (
        current_state_pnl
        + pending_flow_pnl
        + persistent_trend_pnl
        - one_tick_spike_penalty
    )
    projected_max_loss = min(live_equiv_projected_pnl, mean_reversion_stress_pnl)

    out = {
        "projection_ok": True,
        "projection_ok_baseline": True,
        "projection_error": "",
        "current_state_pnl": float(current_state_pnl),
        "pending_flow_pnl": float(pending_flow_pnl),
        "persistent_trend_pnl": float(persistent_trend_pnl),
        "one_tick_spike_penalty": float(one_tick_spike_penalty),
        "mean_reversion_stress_pnl": float(mean_reversion_stress_pnl),
        "live_equiv_projected_pnl": float(live_equiv_projected_pnl),
        # rule-engine compat aliases
        "projected_all_in_pnl": float(live_equiv_projected_pnl),
        "projected_pnl_baseline": float(current_state_pnl),
        "projected_pnl_stress": float(mean_reversion_stress_pnl),
        "projected_max_loss": float(projected_max_loss),
        "projected_tokens_raw": int(tokens_raw),
        "projected_buy_fee_sol": float(buy_fee_sol),
        "v42d_anti_onetick_active": bool(anti_onetick),
        "v42d_persistent_trend_active": bool(persistent_active),
        "v42d_pending_flow_atoms_kept": int(len(post_snap_atoms)),
        "v42d_pending_flow_atoms_seen": int(len(pending_events or [])),
        "lead_confidence": float(lead_conf),
        "lead_meta": lead_meta,
        "v42c_baseline_source": "accountSubscribe",
    }
    if logger is not None:
        try:
            logger(
                f"PGG2-V42D-PROJECTION mint={short} "
                f"current_state_pnl={current_state_pnl:+.6f} "
                f"pending_flow_pnl={pending_flow_pnl:+.6f} "
                f"persistent_trend_pnl={persistent_trend_pnl:+.6f} "
                f"one_tick_spike_penalty={one_tick_spike_penalty:+.6f} "
                f"mean_reversion_stress_pnl={mean_reversion_stress_pnl:+.6f} "
                f"live_equiv_projected_pnl={live_equiv_projected_pnl:+.6f} "
                f"pos1s={pos_updates} streak={streak} "
                f"anti_onetick={int(anti_onetick)} "
                f"persistent={int(persistent_active)} "
                f"atoms_post_snap={len(post_snap_atoms)}/{len(pending_events or [])}"
            )
        except Exception:
            pass
    return out


__all__ = ["project_v42d"]
