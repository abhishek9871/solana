"""V42B — Curve Projector with feed-fusion + curve-delta-lead inputs.

Builds on pgg2_v42_curve_projector.project_post_flow_quote(). Adds:

  - lead_confidence:    composite of (curve_delta_is_leading, positive_delta_streak,
                        source_agreement_count, normalised quote_out_delta_1000ms)
  - projected_max_loss: worst-case PnL across { current, +500ms_stress } - tx_fee
                        floor (i.e. how negative could this go if our projection
                        is wrong AND the +500ms feed-delay scenario materialises)
  - lead_meta:          curve-delta + fusion snapshot fields useful for the
                        rules engine

Emits `PGG2-V42B-FLOW-PROJECTION`.
"""
from __future__ import annotations

import os
from typing import Any, Optional, Sequence


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def compute_lead_confidence(
    curve_delta_snap: dict[str, Any],
    fusion_snap: dict[str, Any],
) -> float:
    """Composite confidence in [0,1].

    Inputs:
      curve_delta_snap["curve_delta_is_leading"]
      curve_delta_snap["positive_delta_streak"]
      curve_delta_snap["sell_quote_gradient"]
      curve_delta_snap["curve_price_delta_1000ms"]
      curve_delta_snap["is_late"]
      fusion_snap["source_agreement_count"]
      fusion_snap["pending_buy_count_1000ms"]
      fusion_snap["source_late"]
    """
    score = 0.0
    if curve_delta_snap.get("curve_delta_is_leading", False):
        score += 0.30
    streak = int(curve_delta_snap.get("positive_delta_streak", 0))
    score += min(0.20, streak * 0.07)
    grad = float(curve_delta_snap.get("sell_quote_gradient", 0.0))
    if grad > 0:
        # Normalise: gradient of +0.0001 SOL on a sample is "strong"
        score += min(0.20, max(0.0, grad / 0.0001) * 0.10)
    d_p_1s = float(curve_delta_snap.get("curve_price_delta_1000ms", 0.0))
    if d_p_1s > 0:
        score += 0.10
    agreement = int(fusion_snap.get("source_agreement_count", 0))
    score += min(0.15, agreement * 0.075)
    if curve_delta_snap.get("is_late", False):
        score -= 0.20
    if fusion_snap.get("source_late", False) and not curve_delta_snap.get("curve_delta_is_leading", False):
        score -= 0.15
    return max(0.0, min(1.0, score))


def project_v42b(
    broker: Any,
    mint_pubkey: str,
    amount_sol: float,
    fusion_snap: dict[str, Any],
    curve_delta_snap: dict[str, Any],
    pending_events: Optional[Sequence[Any]] = None,
    logger: Optional[Any] = None,
    tx_fee_sol: Optional[float] = None,
    pnl_floor_sol: float = 0.00020,
    stress_pnl_floor_sol: float = 0.00020,
    stress_latency_ms: int = 500,
) -> dict[str, Any]:
    """Returns a dict with all V42 projection keys PLUS:

      lead_confidence            float
      projected_max_loss         float (most-negative PnL across baseline+stress)
      lead_meta                  dict
      projected_pnl_baseline     same as projected_all_in_pnl
      projected_pnl_stress       float
    """
    from pgg2_v42_curve_projector import project_post_flow_quote  # type: ignore

    # Baseline projection (no stress).
    base = project_post_flow_quote(
        broker=broker,
        mint_pubkey=mint_pubkey,
        amount_sol=amount_sol,
        oracle_snapshot=fusion_snap,
        pending_events=pending_events,
        logger=None,
        tx_fee_sol=tx_fee_sol,
        pnl_floor_sol=pnl_floor_sol,
        stress_latency_ms=0,
    )
    # Stress projection (+stress_latency_ms feed-delay).
    stress = project_post_flow_quote(
        broker=broker,
        mint_pubkey=mint_pubkey,
        amount_sol=amount_sol,
        oracle_snapshot=fusion_snap,
        pending_events=pending_events,
        logger=None,
        tx_fee_sol=tx_fee_sol,
        pnl_floor_sol=stress_pnl_floor_sol,
        stress_latency_ms=stress_latency_ms,
    )
    base_pnl = float(base.get("projected_all_in_pnl", -999.0))
    stress_pnl = float(stress.get("projected_all_in_pnl", -999.0))
    lead_conf = compute_lead_confidence(curve_delta_snap, fusion_snap)
    projected_max_loss = min(base_pnl, stress_pnl)

    out = dict(base)  # keep baseline fields verbatim
    out["projection_ok_baseline"] = base.get("projection_ok", False)
    out["projection_ok_stress"] = stress.get("projection_ok", False)
    out["projected_pnl_baseline"] = base_pnl
    out["projected_pnl_stress"] = stress_pnl
    out["projected_max_loss"] = float(projected_max_loss)
    out["projected_safety_margin_stress"] = float(stress_pnl - stress_pnl_floor_sol)
    out["lead_confidence"] = float(lead_conf)
    out["stress_latency_ms"] = int(stress_latency_ms)
    out["lead_meta"] = {
        "curve_delta_is_leading": bool(curve_delta_snap.get("curve_delta_is_leading", False)),
        "positive_delta_streak": int(curve_delta_snap.get("positive_delta_streak", 0)),
        "sell_quote_gradient": float(curve_delta_snap.get("sell_quote_gradient", 0.0)),
        "curve_price_delta_1000ms": float(curve_delta_snap.get("curve_price_delta_1000ms", 0.0)),
        "quote_out_delta_1000ms": float(curve_delta_snap.get("quote_out_delta_1000ms", 0.0)),
        "source_agreement_count": int(fusion_snap.get("source_agreement_count", 0)),
        "feed_source_primary": fusion_snap.get("primary_source", ""),
        "source_late": bool(fusion_snap.get("source_late", False)),
        "curve_is_late": bool(curve_delta_snap.get("is_late", False)),
        "curve_age_ms": int(curve_delta_snap.get("curve_age_ms", 0)),
        "source_latency_p50_ms": int(curve_delta_snap.get("source_latency_p50_ms", 0)),
        "source_latency_p95_ms": int(curve_delta_snap.get("source_latency_p95_ms", 0)),
    }

    if logger is not None:
        try:
            logger(
                f"PGG2-V42B-FLOW-PROJECTION mint={_short(mint_pubkey)} "
                f"amount_sol={float(amount_sol):.6f} "
                f"projected_pnl_baseline={base_pnl:+.6f} "
                f"projected_pnl_stress={stress_pnl:+.6f} "
                f"projected_max_loss={projected_max_loss:+.6f} "
                f"lead_confidence={lead_conf:.3f} "
                f"streak={out['lead_meta']['positive_delta_streak']} "
                f"leading={int(out['lead_meta']['curve_delta_is_leading'])} "
                f"agreement={out['lead_meta']['source_agreement_count']} "
                f"sell_grad={out['lead_meta']['sell_quote_gradient']:+.12f} "
                f"d_p_1s={out['lead_meta']['curve_price_delta_1000ms']:+.12f}"
            )
        except Exception:
            pass

    return out


__all__ = ["project_v42b", "compute_lead_confidence"]
