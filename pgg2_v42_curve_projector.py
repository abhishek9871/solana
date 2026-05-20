"""V42 — Curve Projector.

For a candidate pump.fun mint, project the bonding-curve state AFTER our
hypothetical buy AND AFTER the observed pending external flow has landed.
Use the projected curve to compute the projected sell quote for our holdings,
and return route-aware all-in PnL.

Inputs:
  - broker: a DirectPumpQuoteBroker (or subclass) providing:
      .bonding_curve(mint_pubkey)        -> PumpBondingCurve
      .pump_global()                     -> PumpGlobal
      .quote_pump_buy_tokens(spend_lp, curve, global_cfg)  -> (tokens_raw, fees)
      .quote_pump_sell_sol(token_amt, curve, global_cfg)   -> (sol_lamports, fees)
      .simulate_post_buy_pump_curve(curve, tokens_received) -> PumpBondingCurve
  - oracle_snapshot: PendingFlowOracle.snapshot(mint, now_ms)
  - mint_pubkey: str
  - amount_sol: float (size of our hypothetical buy)
  - pending_events: optional list of (ts_ms, side, sol) for time-ordered
    application; if absent we infer from oracle_snapshot windows.

Hard rule: NEVER substitute the current sell quote. Projection MUST use
post-our-buy + post-pending-flow curve state.

Output dict fields:
  projected_tokens_raw           - our buy fill on current curve (raw int)
  projected_post_flow_sell_out   - SOL we'd get selling all those tokens
                                    on the post-(our-buy + pending-flow) curve
  projected_all_in_pnl           - route-aware PnL (pump_bc route)
  projected_required_flow_sol    - external buy flow baked into projection
  projected_safety_margin        - projected_all_in_pnl - +0.00020 floor
  source_latency_ms              - feed latency from oracle
  projected_post_flow_curve_price - vSOL/vTOK on the projected curve
  projected_buy_fee_sol          - buy fee component
  projected_sell_fee_sol         - sell fee component
  projection_ok                  - boolean
  projection_error               - error if any

Emit `PGG2-V42-FLOW-PROJECTION` log with all fields.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence


LAMPORTS_PER_SOL = 1_000_000_000


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _short(mint: str) -> str:
    if not mint:
        return "?"
    return mint[:4] + ".." + mint[-4:] if len(mint) > 10 else mint


@dataclass
class PendingFlowAtom:
    ts_ms: int
    side: str   # "buy" / "sell"
    sol: float


def apply_pending_flow_to_curve(
    broker: Any,
    curve: Any,
    global_cfg: Any,
    pending_events: Iterable[PendingFlowAtom],
) -> tuple[Any, float, float]:
    """Walk pending events in ts_ms order, shifting reserves with the CPMM
    invariant for each one. Returns (new_curve, total_external_buy_sol,
    total_external_sell_sol).

    For buys we use quote_pump_buy_tokens to compute the tokens delta and
    then simulate_post_buy_pump_curve. For sells we approximate the sell as
    receiving tokens onto the curve and reducing vSOL accordingly using the
    same k=vSOL*vTOK invariant.
    """
    events = sorted(
        (e for e in pending_events if e and e.sol > 0),
        key=lambda e: int(e.ts_ms),
    )
    cur = curve
    buy_total = 0.0
    sell_total = 0.0
    for ev in events:
        try:
            if ev.side == "buy":
                spend_lamports = max(1, int(ev.sol * LAMPORTS_PER_SOL))
                tokens, _fee = broker.quote_pump_buy_tokens(spend_lamports, cur, global_cfg)
                if tokens > 0:
                    cur = broker.simulate_post_buy_pump_curve(cur, int(tokens))
                buy_total += float(ev.sol)
            else:
                # External sell shifts curve the other way. We model it as
                # adding gross_sol of vSOL back is wrong — sells REDUCE vSOL
                # and INCREASE vTOK. Reverse of buy: solve for tokens given
                # gross sol-out, then increase vTOK, decrease vSOL by net.
                # Use the inverse of quote_pump_sell_sol: token_amount such
                # that gross sol matches ev.sol*LAMPORTS_PER_SOL. Use a
                # bisection because the curve math is monotonic.
                target_lp = max(1, int(ev.sol * LAMPORTS_PER_SOL))
                # bound: at most all real token reserves
                lo, hi = 1, max(2, int(cur.virtual_token_reserves))
                best = 1
                for _ in range(40):
                    mid = (lo + hi) // 2
                    if mid <= 0:
                        break
                    sol_out, _ = broker.quote_pump_sell_sol(mid, cur, global_cfg)
                    if sol_out >= target_lp:
                        best = mid
                        hi = mid - 1
                    else:
                        lo = mid + 1
                    if lo > hi:
                        break
                tokens_added = int(best)
                # Apply the sell — opposite of buy: vTOK increases, vSOL drops.
                from pgg2_direct_pump import PumpBondingCurve  # late import
                k = int(cur.virtual_sol_reserves) * int(cur.virtual_token_reserves)
                vtok_new = int(cur.virtual_token_reserves) + tokens_added
                vsol_new = max(1, k // max(1, vtok_new))
                cur = PumpBondingCurve(
                    key=cur.key,
                    virtual_token_reserves=vtok_new,
                    virtual_sol_reserves=vsol_new,
                    real_token_reserves=int(cur.real_token_reserves) + tokens_added,
                    real_sol_reserves=int(getattr(cur, "real_sol_reserves", 0)),
                    token_total_supply=int(cur.token_total_supply),
                    complete=bool(cur.complete),
                    creator=cur.creator,
                    is_mayhem=bool(getattr(cur, "is_mayhem", False)),
                    cashback_enabled=bool(getattr(cur, "cashback_enabled", False)),
                )
                sell_total += float(ev.sol)
        except Exception:
            continue
    return cur, buy_total, sell_total


def project_post_flow_quote(
    broker: Any,
    mint_pubkey: str,
    amount_sol: float,
    oracle_snapshot: dict[str, Any],
    pending_events: Optional[Sequence[PendingFlowAtom]] = None,
    logger: Optional[Any] = None,
    tx_fee_sol: Optional[float] = None,
    pnl_floor_sol: float = 0.00020,
    stress_latency_ms: int = 0,
) -> dict[str, Any]:
    """Compute the V42 projection. See module docstring for fields.

    If `pending_events` is None, we synthesize one event-block per window
    from the oracle snapshot, treating each window's net buy-sol as a single
    buy. The richer version (caller passes actual per-tx atoms) is used in
    Phase 4/5 where we have raw.jsonl available.
    """
    try:
        from pgg2_direct_pump import as_pubkey
    except Exception as exc:
        return {
            "projection_ok": False,
            "projection_error": f"import:{type(exc).__name__}:{exc}",
            "projected_tokens_raw": 0,
            "projected_post_flow_sell_out": 0.0,
            "projected_all_in_pnl": -999.0,
            "projected_required_flow_sol": 0.0,
            "projected_safety_margin": -999.0,
            "source_latency_ms": int(oracle_snapshot.get("feed_latency_ms", 0)),
        }
    short = _short(mint_pubkey)
    feed_latency_ms = int(oracle_snapshot.get("feed_latency_ms", 0)) + max(0, int(stress_latency_ms))
    try:
        mint_pk = as_pubkey(mint_pubkey)
        curve_now = broker.bonding_curve(mint_pk)
        global_cfg = broker.pump_global()
    except Exception as exc:
        return {
            "projection_ok": False,
            "projection_error": f"curve_fetch:{type(exc).__name__}:{exc}",
            "projected_tokens_raw": 0,
            "projected_post_flow_sell_out": 0.0,
            "projected_all_in_pnl": -999.0,
            "projected_required_flow_sol": 0.0,
            "projected_safety_margin": -999.0,
            "source_latency_ms": feed_latency_ms,
        }

    # 1. Our hypothetical buy on the CURRENT curve.
    spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
    try:
        our_tokens, buy_fee_lp = broker.quote_pump_buy_tokens(spend_lamports, curve_now, global_cfg)
    except Exception as exc:
        return {
            "projection_ok": False,
            "projection_error": f"our_buy_quote:{type(exc).__name__}:{exc}",
            "projected_tokens_raw": 0,
            "projected_post_flow_sell_out": 0.0,
            "projected_all_in_pnl": -999.0,
            "projected_required_flow_sol": 0.0,
            "projected_safety_margin": -999.0,
            "source_latency_ms": feed_latency_ms,
        }
    curve_after_us = broker.simulate_post_buy_pump_curve(curve_now, int(our_tokens))

    # 2. Apply pending external flow.
    if pending_events is None:
        # Synthesise from oracle: one buy at the trailing-1000ms total, one
        # sell at the trailing-1000ms total. Stress-latency case shrinks the
        # baked-in flow proportionally to how much the feed would have aged.
        buy_pool = float(oracle_snapshot.get("pending_buy_sol_1000ms", 0.0))
        sell_pool = float(oracle_snapshot.get("pending_sell_sol_1000ms", 0.0))
        if stress_latency_ms > 0:
            # As the feed becomes older relative to entry decision, the bot
            # had LESS opportunity to predict future flow; we model that by
            # scaling the baked-in expected flow down.
            shrink = max(0.0, 1.0 - (stress_latency_ms / 1000.0))
            buy_pool *= shrink
            sell_pool *= shrink
        atoms: list[PendingFlowAtom] = []
        if buy_pool > 0:
            atoms.append(PendingFlowAtom(ts_ms=0, side="buy", sol=buy_pool))
        if sell_pool > 0:
            atoms.append(PendingFlowAtom(ts_ms=1, side="sell", sol=sell_pool))
    else:
        atoms = list(pending_events)
        if stress_latency_ms > 0:
            # Stress: drop the most-recent stress-window of pending events
            # because, in a real world with +500ms feed delay, those atoms
            # wouldn't have been visible at decision time. We take the
            # latest observed ts_ms as the wall-clock anchor.
            if atoms:
                latest_ts = max(int(a.ts_ms) for a in atoms)
                cutoff = latest_ts - int(stress_latency_ms)
                atoms = [a for a in atoms if int(a.ts_ms) <= cutoff]
    curve_post, external_buy_total, external_sell_total = apply_pending_flow_to_curve(
        broker, curve_after_us, global_cfg, atoms
    )

    # 3. Project the sell quote of OUR tokens on the post-flow curve.
    try:
        sell_out_lp, sell_fee_lp = broker.quote_pump_sell_sol(int(our_tokens), curve_post, global_cfg)
    except Exception as exc:
        return {
            "projection_ok": False,
            "projection_error": f"projected_sell:{type(exc).__name__}:{exc}",
            "projected_tokens_raw": int(our_tokens),
            "projected_post_flow_sell_out": 0.0,
            "projected_all_in_pnl": -999.0,
            "projected_required_flow_sol": external_buy_total,
            "projected_safety_margin": -999.0,
            "source_latency_ms": feed_latency_ms,
        }
    sell_out_sol = sell_out_lp / LAMPORTS_PER_SOL
    buy_fee_sol = buy_fee_lp / LAMPORTS_PER_SOL
    sell_fee_sol = sell_fee_lp / LAMPORTS_PER_SOL

    # 4. Route-aware all-in PnL.
    if tx_fee_sol is None:
        tx_fee_sol = _env_float(
            "PGG2_V39_LIVE_ROUTE_AWARE_TX_FEE_SOL",
            _env_float("PGG2_ROUTE_AWARE_TX_FEE_SOL", 0.000010),
        )
    # Use broker.quote_all_in_pnl if available so we get identical accounting
    # to the running bot; fall back to manual math.
    try:
        econ = broker.quote_all_in_pnl(
            route="pump_bc",
            cost_sol=float(amount_sol),
            quote_out=float(sell_out_sol),
            quote_metadata={"buy_fee_sol": buy_fee_sol, "sell_fee_sol": sell_fee_sol},
            execution_context={
                "tx_fee_sol": float(tx_fee_sol),
                "ata_recoverable": True,
            },
        )
        all_in_pnl = float(econ["all_in_pnl"])
    except Exception:
        all_in_pnl = float(sell_out_sol) - float(amount_sol) - 2 * float(tx_fee_sol)

    projected_post_flow_price = (
        curve_post.virtual_sol_reserves / max(1, curve_post.virtual_token_reserves)
    )

    result = {
        "projection_ok": True,
        "projection_error": "",
        "projected_tokens_raw": int(our_tokens),
        "projected_post_flow_sell_out": float(sell_out_sol),
        "projected_all_in_pnl": float(all_in_pnl),
        "projected_required_flow_sol": float(external_buy_total),
        "projected_external_sell_sol": float(external_sell_total),
        "projected_safety_margin": float(all_in_pnl - float(pnl_floor_sol)),
        "source_latency_ms": int(feed_latency_ms),
        "projected_post_flow_curve_price": float(projected_post_flow_price),
        "projected_buy_fee_sol": float(buy_fee_sol),
        "projected_sell_fee_sol": float(sell_fee_sol),
        "amount_sol": float(amount_sol),
        "tx_fee_sol": float(tx_fee_sol),
        "stress_latency_ms": int(stress_latency_ms),
    }

    if logger is not None:
        try:
            logger(
                f"PGG2-V42-FLOW-PROJECTION mint={short} "
                f"amount_sol={amount_sol:.6f} our_tokens={int(our_tokens)} "
                f"projected_sell_out={sell_out_sol:.6f} "
                f"projected_all_in_pnl={all_in_pnl:+.6f} "
                f"projected_required_flow_sol={external_buy_total:.3f} "
                f"projected_safety_margin={result['projected_safety_margin']:+.6f} "
                f"source_latency_ms={feed_latency_ms} "
                f"stress_latency_ms={stress_latency_ms} "
                f"projected_post_flow_price={projected_post_flow_price:.12f}"
            )
        except Exception:
            pass

    return result


__all__ = [
    "PendingFlowAtom",
    "apply_pending_flow_to_curve",
    "project_post_flow_quote",
]
