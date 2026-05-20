"""V42H-SAFE Phase 5 — Strict-subset no-send capture.

Same pipeline as V42H Phase-7 capture (`_v42h_no_send_capture.py`) but:
  - Loads `data/v42hsafe_rules.json` (NOT `v42h_local_runner_rules.json`)
  - Uses V42H-SAFE late-entry blocker (`pgg2_v42hsafe_late_entry`)
  - Uses V42H-SAFE entry gate (`pgg2_v42hsafe_entry_gate`)
  - SHADOW-mode rule passes are LOGGED but NEVER become candidate entries.
  - One per-candidate JSONL line written to `data/v42hsafe_no_send_decisions.jsonl`

Pipeline:
   shred (side-listener)  --> hot_mint set
                                       |
                                       v
   accountSubscribe oracle (V42C)  --> per-mint curve update stream
                                       |
                                       v
   LocalCurveQuoteVirtualTicketEngine (V42H)
       - opens 1 virtual ticket per accountSubscribe update
       - quote math computed LOCALLY from (vsol, vtok, fee_bps)
       - causal future-PnL observation as the chain extends
                                       |
                                       v
   evaluate_all_rules (V42H rules module)
      |   filter to actual-mode rules only for candidate consideration
      |   shadow rules logged via PGG2-V42HSAFE-SHADOW-PASS
      v
   pgg2_v42hsafe_late_entry.late_entry_decision
      |   strict gate from Phase 4
      v
   pgg2_v42hsafe_entry_gate.evaluate_entry_gate
      |   survival filter from Phase 3
      v
   PGG2-V42HSAFE-CANDIDATE-ENTRY (logged, NO send)

Stop at --target-pass (default 10) actual-mode candidates or --max-seconds
(default 600). Output `/root/piggy/V42HSAFE_NO_SEND_REPORT.md` and a JSONL
of every actual-mode candidate to
`/root/piggy/data/v42hsafe_no_send_decisions.jsonl`.

CAUSALITY: each candidate's observed outcome is computed from the FIRST
subsequent curve update on its mint (never used as a feature). The exit
policy is then applied to that observation chain within max_hold_ms (2500).

NO transactions. Static-grep enforces no-send at module load.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re as _re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        print(f"V42HSAFE-NO-SEND-ABORT forbidden_call_pattern={_pat}")
        sys.exit(2)


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _now_ms() -> int:
    return int(time.time() * 1000)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-jsonl",
                    default="/root/piggy/data/v42hsafe_no_send_decisions.jsonl")
    ap.add_argument("--rules-json",
                    default="/root/piggy/data/v42hsafe_rules.json")
    ap.add_argument("--amount-sol", type=float, default=0.015)
    ap.add_argument("--max-seconds", type=int, default=600)
    ap.add_argument("--target-pass", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--debug-log", default="")
    ap.add_argument("--self-shred", action="store_true", default=True)
    return ap.parse_args()


async def amain() -> int:
    sys.path.insert(0, "/root/piggy")
    args = parse_args()
    try:
        from pgg2_v42_curve_account_subscriber import CurveAccountSubscriberOracle  # type: ignore
        from pgg2_direct_pump import DirectPumpQuoteBroker  # type: ignore
        from birth_first_sniper import BotConfig, parse_base64_shred_for_pump_events  # type: ignore
        from pgg2_v42h_local_curve_quote import (
            curve_state_from_subscriber_point,
            LAMPORTS_PER_SOL,
            DEFAULT_TX_FEE_SOL,
            V42HCurveState,
            local_roundtrip_label,
            local_sell_quote_sol,
            break_even_sell_out_sol,
        )
        from pgg2_v42h_local_ticket_engine import (
            LocalCurveQuoteVirtualTicketEngine, LookaheadViolation,
        )
        from pgg2_v42h_local_rules import (
            load_rules as load_v42h_rules,
            evaluate_all_rules, emit_rule_block_log,
        )
        # NEW V42H-SAFE imports.
        from pgg2_v42hsafe_late_entry import (
            late_entry_decision as v42hsafe_late_entry,
            format_log_line as v42hsafe_late_log,
        )
        from pgg2_v42hsafe_entry_gate import (
            evaluate_entry_gate as v42hsafe_gate,
            format_log_line as v42hsafe_gate_log,
        )
    except Exception as exc:
        print(f"V42HSAFE-NO-SEND-ABORT import:{type(exc).__name__}:{exc}")
        return 2

    log_fp = None
    if args.debug_log:
        log_fp = open(args.debug_log, "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        if log_fp is not None:
            log_fp.write(line + "\n")
            log_fp.flush()

    # Load V42H-SAFE rules. We feed the V42H rule-eval the SAME schema so
    # evaluate_all_rules can keep working. We additionally maintain a
    # rule_mode map for actual/shadow filtering.
    rules_cfg = load_v42h_rules(args.rules_json)
    rule_mode_map: Dict[str, str] = {
        rid: str(cfg.get("mode", "actual"))
        for rid, cfg in rules_cfg.get("rules", {}).items()
    }
    # Log the disabled rules header.
    for rid in rules_cfg.get("rule_disabled", []):
        log(f"PGG2-V42H-RULE-DISABLED rule={rid} reason=observed_negative_rate")
    log(f"V42HSAFE-NO-SEND rule_modes={rule_mode_map}")
    BANK_TH = float(rules_cfg.get("exit_policy", {}).get("bank_pnl_sol", 0.00060))
    LOSS_TH = float(rules_cfg.get("exit_policy", {}).get("clamp_loss_sol", -0.00050))
    SCRATCH_TH = float(rules_cfg.get("exit_policy", {}).get("scratch_pnl_sol", 0.00005))
    MAX_HOLD = int(rules_cfg.get("exit_policy", {}).get("max_hold_ms", 2500))

    log(
        f"V42HSAFE-NO-SEND start amount_sol={args.amount_sol} "
        f"max_seconds={args.max_seconds} target_pass={args.target_pass} "
        f"rules_path={args.rules_json}"
    )

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(f"V42HSAFE-NO-SEND fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}")

    engine = LocalCurveQuoteVirtualTicketEngine(
        amount_sol=args.amount_sol,
        max_hold_ms=MAX_HOLD,
        bank_pnl_sol=BANK_TH,
        scratch_pnl_sol=SCRATCH_TH,
        clamp_loss_sol=LOSS_TH,
        tx_fee_sol=DEFAULT_TX_FEE_SOL,
        logger=log,
    )

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    candidates: List[Dict[str, Any]] = []
    rule_pass_counts_actual: Counter = Counter()
    rule_pass_counts_shadow: Counter = Counter()
    rule_block_counts: Counter = Counter()
    late_block_counts: Counter = Counter()
    late_allow_counts: Counter = Counter()
    gate_block_counts: Counter = Counter()
    gate_allow_counts: Counter = Counter()
    seen_pass_mints: set = set()

    hot_mint_last_seen: Dict[str, int] = {}
    shred_stop = asyncio.Event()
    seen_curve_ts: Dict[str, int] = {}

    out_jsonl_path = Path(args.out_jsonl)
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fp = open(str(out_jsonl_path), "w", encoding="utf-8")

    def _ticket_history_from_engine(mint: str) -> List[Dict[str, Any]]:
        return [
            {
                "ticket_id": tk.ticket_id,
                "outcome": tk.outcome,
                "outcome_ts_ms": tk.outcome_ts_ms,
                "bank_pnl_sol": tk.bank_pnl_sol,
                "bank_time_ms": tk.bank_time_ms,
                "bank_sell_out_sol": tk.bank_sell_out_sol,
                "buy_snapshot_ts_ms": tk.buy_snapshot_ts_ms,
                "buy_tokens_raw": tk.buy_tokens_raw,
            }
            for tk in engine.tickets(mint)
        ]

    def _current_sell_for_prior_buy(latest_snap, buy_tokens_raw: int) -> float:
        if buy_tokens_raw <= 0:
            return 0.0
        sell_lamports, _fee = local_sell_quote_sol(
            latest_snap.curve_state, int(buy_tokens_raw)
        )
        return float(sell_lamports) / LAMPORTS_PER_SOL

    def _last_bank_ts(history: List[Dict[str, Any]]) -> Optional[int]:
        bs = [int(t["outcome_ts_ms"]) for t in history
              if t.get("outcome") == "virtual_bank_win" and t.get("outcome_ts_ms") is not None]
        return max(bs) if bs else None

    def _last_loss_ts(history: List[Dict[str, Any]]) -> Optional[int]:
        ls = [int(t["outcome_ts_ms"]) for t in history
              if t.get("outcome") == "virtual_loss" and t.get("outcome_ts_ms") is not None]
        return max(ls) if ls else None

    def _last_bank_buy_tokens(history: List[Dict[str, Any]]) -> int:
        bs = [t for t in history
              if t.get("outcome") == "virtual_bank_win" and t.get("outcome_ts_ms") is not None]
        if not bs:
            return 0
        bs.sort(key=lambda x: int(x["outcome_ts_ms"]))
        return int(bs[-1].get("buy_tokens_raw") or 0)

    async def _side_shred_listener():
        try:
            import websockets  # type: ignore
        except Exception as exc:
            log(f"V42HSAFE-NO-SEND ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V42HSAFE-NO-SEND no_ws_url")
            return
        backoff = 2.0
        while not shred_stop.is_set():
            try:
                import websockets  # type: ignore  (re-import per loop for cleanliness)
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=60,
                    max_queue=4096, max_size=8 * 1024 * 1024,
                ) as ws:
                    backoff = 2.0
                    sub = {
                        "jsonrpc": "2.0", "id": 90041,
                        "method": "shredSubscribe",
                        "params": [
                            {"accountInclude": [PUMP_PROGRAM],
                             "accountRequired": [PUMP_PROGRAM], "vote": False},
                            {"encoding": "base64", "transactionDetails": "full",
                             "maxSupportedTransactionVersion": 0},
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    log("V42HSAFE-NO-SEND shred_subscribed")
                    async for raw in ws:
                        if shred_stop.is_set():
                            break
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        method = str(data.get("method") or "").lower()
                        if "shred" not in method:
                            continue
                        result = ((data.get("params") or {}).get("result") or {})
                        try:
                            events = list(parse_base64_shred_for_pump_events(result, set()))
                        except Exception:
                            events = []
                        for ev in events:
                            m = getattr(ev, "mint", "") or ""
                            if not m:
                                continue
                            ts_ms = int(getattr(ev, "ts_ms", _now_ms()) or _now_ms())
                            hot_mint_last_seen[m] = ts_ms
                            oracle.mark_feed_event(m, ts_ms)
                            if len(hot_mint_last_seen) <= args.max_hot_mints:
                                oracle.request_subscription(m)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(f"V42HSAFE-NO-SEND shred_reconnect exc={type(exc).__name__}:{exc}")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2.0, 30.0)

    shred_task = asyncio.create_task(_side_shred_listener()) if args.self_shred else None

    deadline_ms = _now_ms() + args.max_seconds * 1000
    t_start_wall = _now_ms()

    try:
        while _now_ms() < deadline_ms:
            await asyncio.sleep(0.05)
            now_ts = _now_ms()
            stale_cutoff = now_ts - 15000
            cold = [m for m, t in hot_mint_last_seen.items() if t < stale_cutoff]
            for m in cold:
                hot_mint_last_seen.pop(m, None)

            target_reached = len(candidates) >= args.target_pass
            for mint in list(hot_mint_last_seen.keys())[: args.max_hot_mints]:
                st = oracle._states.get(mint)
                if st is None or not st.points:
                    continue
                latest_pt = None
                for p in reversed(st.points):
                    if not p.error:
                        latest_pt = p
                        break
                if latest_pt is None:
                    continue
                last_ingest_ts = seen_curve_ts.get(mint, 0)
                if latest_pt.ts_ms <= last_ingest_ts:
                    continue
                seen_curve_ts[mint] = latest_pt.ts_ms

                cs = curve_state_from_subscriber_point(
                    int(latest_pt.virtual_sol_reserves),
                    int(latest_pt.virtual_token_reserves),
                    int(latest_pt.real_token_reserves),
                    fee_bps,
                    creator_fee_bps,
                )
                snap = engine.build_snapshot_from_curve(
                    ts_ms=int(latest_pt.ts_ms),
                    slot=int(latest_pt.slot),
                    curve_state=cs,
                )
                engine.ingest_snapshot(mint, snap)
                e_st = engine.mint_state(mint)
                if e_st is None or not e_st.snapshots:
                    continue
                if mint in seen_pass_mints:
                    continue
                latest_snap = e_st.snapshots[-1]
                results = evaluate_all_rules(
                    engine, mint, latest_snap, rules_cfg, args.amount_sol, snap.ts_ms,
                )
                fired = [r for r in results if r.passed]
                for r in results:
                    if r.passed:
                        mode = rule_mode_map.get(r.rule_id, "actual")
                        if mode == "actual":
                            rule_pass_counts_actual[r.rule_id] += 1
                        else:
                            rule_pass_counts_shadow[r.rule_id] += 1
                            log(
                                f"PGG2-V42HSAFE-SHADOW-PASS mint={mint[:4]}..{mint[-4:]} "
                                f"rule={r.rule_id} diag={r.diagnostics}"
                            )
                    else:
                        rule_block_counts[r.reason] += 1
                actual_fired = [r for r in fired if rule_mode_map.get(r.rule_id, "actual") == "actual"]
                if not actual_fired:
                    continue
                if target_reached:
                    continue

                first_rule = actual_fired[0]

                # Build features for late-entry + gate evaluation.
                history = _ticket_history_from_engine(mint)
                last_bank_ts = _last_bank_ts(history)
                last_loss_ts = _last_loss_ts(history)
                last_bank_tokens = _last_bank_buy_tokens(history)

                # current_local_quote: sell-out of last bank's tokens at NEW
                # curve (runner-continuation metric, matches V42H rule logic).
                cq = _current_sell_for_prior_buy(latest_snap, last_bank_tokens) \
                    if last_bank_tokens > 0 else \
                    (float(latest_snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL)
                be = break_even_sell_out_sol(args.amount_sol, DEFAULT_TX_FEE_SOL, 0.0)

                # latest_quote_gradient: most recent delta in the engine's
                # sell_out_sol_seq (last 2 samples).
                grad = 0.0
                if len(e_st.sell_out_sol_seq) >= 2:
                    (_t1, v1), (_t2, v2) = list(e_st.sell_out_sol_seq)[-2:]
                    grad = float(v2 - v1)

                # latest_account_sub_delta: most recent delta in curve_price_seq.
                cdelta = 0.0
                if len(e_st.curve_price_seq) >= 2:
                    (_t1, p1), (_t2, p2) = list(e_st.curve_price_seq)[-2:]
                    cdelta = float(p2 - p1)
                last_curve_update_kind = "negative" if cdelta < 0 else (
                    "positive" if cdelta > 0 else "flat"
                )

                # V42H-SAFE LATE-ENTRY (Phase 4)
                lbr = v42hsafe_late_entry(
                    mint=mint,
                    ticket_history=history,
                    current_quote_sol=cq,
                    break_even_quote=be,
                    latest_quote_gradient=grad,
                    last_virtual_bank_ts_ms=last_bank_ts,
                    last_virtual_loss_ts_ms=last_loss_ts,
                    ts_ms_now=snap.ts_ms,
                )
                log(v42hsafe_late_log(lbr))
                if not lbr["allowed"]:
                    late_block_counts[lbr["reason"]] += 1
                    continue
                late_allow_counts[lbr["reason"]] += 1

                # V42H-SAFE ENTRY GATE (Phase 3)
                gd = v42hsafe_gate(
                    mint=mint,
                    rule_id=first_rule.rule_id,
                    ticket_history=history,
                    current_quote_sol=cq,
                    break_even_quote=be,
                    latest_quote_gradient=grad,
                    latest_account_sub_delta=cdelta,
                    last_curve_update_kind=last_curve_update_kind,
                    ts_ms_now=snap.ts_ms,
                    decision_ts_ms=snap.ts_ms,
                    route=str(getattr(latest_snap, "route", "pump_bc")),
                    sim_needed=int(getattr(latest_snap, "sim_needed", 0)),
                    pair_source=str(getattr(latest_snap, "pair_source", "accountSubscribe")),
                    rules_path=args.rules_json,
                )
                log(v42hsafe_gate_log(gd))
                if not gd["gate_pass"]:
                    gate_block_counts[gd["blocker"] or "unknown"] += 1
                    continue
                gate_allow_counts["v42hsafe_strict_pass"] += 1

                # Emit candidate.
                seen_pass_mints.add(mint)
                log(
                    f"PGG2-V42HSAFE-CANDIDATE-ENTRY mint={mint[:4]}..{mint[-4:]} "
                    f"rule={first_rule.rule_id}"
                )

                # Last-bank look-up for the per-candidate row.
                banks = [t for t in engine.tickets(mint) if t.outcome == "virtual_bank_win"]
                last_bank = sorted(banks, key=lambda t: t.outcome_ts_ms or 0)[-1] if banks else None
                cand = {
                    "mint": mint,
                    "rule_id": first_rule.rule_id,
                    "rule_mode": "actual",
                    "ts_ms": snap.ts_ms,
                    "decision_quote_sol_reflexive": float(latest_snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL,
                    "current_local_quote": float(cq),
                    "break_even_quote": float(be),
                    "latest_quote_gradient": float(grad),
                    "latest_account_sub_delta": float(cdelta),
                    "last_curve_update_kind": last_curve_update_kind,
                    "buy_tokens_raw": int(latest_snap.buy_tokens_raw),
                    "buy_curve_state": (
                        latest_snap.curve_state.virtual_sol_reserves,
                        latest_snap.curve_state.virtual_token_reserves,
                    ),
                    "last_bank_pnl_sol": (last_bank.bank_pnl_sol if last_bank else None),
                    "last_bank_age_ms": (
                        (snap.ts_ms - (last_bank.outcome_ts_ms or snap.ts_ms))
                        if last_bank else None
                    ),
                    "consecutive_virtual_wins": int(gd["fields"]["consecutive_virtual_wins"]),
                    "virtual_losses_last_3000ms": int(gd["fields"]["virtual_losses_last_3000ms"]),
                    "last_virtual_loss_age_ms": gd["fields"]["last_virtual_loss_age_ms"],
                    "time_since_last_virtual_bank_ms": gd["fields"]["time_since_last_virtual_bank_ms"],
                    "time_since_first_virtual_bank_ms": gd["fields"]["time_since_first_virtual_bank_ms"],
                    "late_entry_reason": lbr["reason"],
                    "gate_blocker": None,
                    "observed_outcome_pnl_sol": None,
                    "observed_outcome_lag_ms": None,
                    "observed_outcome_kind": None,
                }
                candidates.append(cand)
                jsonl_fp.write(json.dumps({
                    "type": "v42hsafe_candidate",
                    "ts_ms": snap.ts_ms,
                    **{k: v for k, v in cand.items() if k != "buy_curve_state"},
                    "buy_curve_state": list(cand["buy_curve_state"]),
                }) + "\n")
                jsonl_fp.flush()
                log(
                    f"PGG2-V42HSAFE-CANDIDATE-DECISION mint={mint[:4]}..{mint[-4:]} "
                    f"rule={first_rule.rule_id} count={len(candidates)}/{args.target_pass}"
                )

            # Update observed outcomes (exit-policy simulation).
            for c in candidates:
                if c["observed_outcome_pnl_sol"] is not None:
                    continue
                est = engine.mint_state(c["mint"])
                if est is None:
                    continue
                later = [s for s in est.snapshots if s.ts_ms > c["ts_ms"]]
                if not later:
                    continue
                buy_cs = V42HCurveState(
                    virtual_sol_reserves=c["buy_curve_state"][0],
                    virtual_token_reserves=c["buy_curve_state"][1],
                    real_token_reserves=c["buy_curve_state"][1],
                    fee_bps=fee_bps,
                    creator_fee_bps=creator_fee_bps,
                )
                outcome_pnl = None
                outcome_lag = None
                outcome_kind = None
                max_fav = 0.0
                done = False
                for s in later:
                    lag = s.ts_ms - c["ts_ms"]
                    if lag > MAX_HOLD:
                        if outcome_pnl is None:
                            outcome_pnl = max_fav
                            outcome_lag = MAX_HOLD
                            outcome_kind = "expired"
                            done = True
                        break
                    pnl = local_roundtrip_label(buy_cs, s.curve_state, args.amount_sol)
                    if pnl > max_fav:
                        max_fav = pnl
                    if pnl >= BANK_TH:
                        outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "bank"; done = True; break
                    if pnl <= LOSS_TH:
                        outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "loss"; done = True; break
                    if max_fav >= SCRATCH_TH and pnl < max_fav and pnl < SCRATCH_TH:
                        outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "scratch"; done = True; break
                if done:
                    c["observed_outcome_pnl_sol"] = float(outcome_pnl)
                    c["observed_outcome_lag_ms"] = int(outcome_lag or 0)
                    c["observed_outcome_kind"] = outcome_kind
                    jsonl_fp.write(json.dumps({
                        "type": "v42hsafe_observed",
                        "ts_ms": _now_ms(),
                        "mint": c["mint"],
                        "rule_id": c["rule_id"],
                        "decision_ts_ms": c["ts_ms"],
                        "observed_outcome_pnl_sol": outcome_pnl,
                        "observed_outcome_lag_ms": outcome_lag,
                        "observed_outcome_kind": outcome_kind,
                    }) + "\n")
                    jsonl_fp.flush()
                    log(
                        f"PGG2-V42HSAFE-CANDIDATE-OBSERVED mint={c['mint'][:4]}..{c['mint'][-4:]} "
                        f"pnl={outcome_pnl:+.9f} kind={outcome_kind} lag_ms={outcome_lag}"
                    )

            if (
                len(candidates) >= args.target_pass
                and all(c["observed_outcome_pnl_sol"] is not None for c in candidates)
            ):
                log(
                    f"V42HSAFE-NO-SEND target_reached_and_all_observed_count={len(candidates)} — early stop"
                )
                break

    finally:
        shred_stop.set()
        if shred_task is not None:
            shred_task.cancel()
            try:
                await shred_task
            except Exception:
                pass
        try:
            await oracle.stop()
        except Exception:
            pass

    # Final drain.
    for c in candidates:
        if c["observed_outcome_pnl_sol"] is not None:
            continue
        est = engine.mint_state(c["mint"])
        if est is None:
            continue
        later = [s for s in est.snapshots if s.ts_ms > c["ts_ms"]]
        if not later:
            continue
        buy_cs = V42HCurveState(
            virtual_sol_reserves=c["buy_curve_state"][0],
            virtual_token_reserves=c["buy_curve_state"][1],
            real_token_reserves=c["buy_curve_state"][1],
            fee_bps=fee_bps,
            creator_fee_bps=creator_fee_bps,
        )
        max_fav = 0.0
        outcome_pnl = None
        outcome_lag = None
        outcome_kind = None
        for s in later:
            lag = s.ts_ms - c["ts_ms"]
            if lag > MAX_HOLD:
                if outcome_pnl is None:
                    outcome_pnl = max_fav
                    outcome_lag = MAX_HOLD
                    outcome_kind = "expired"
                break
            pnl = local_roundtrip_label(buy_cs, s.curve_state, args.amount_sol)
            if pnl > max_fav:
                max_fav = pnl
            if pnl >= BANK_TH:
                outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "bank"; break
            if pnl <= LOSS_TH:
                outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "loss"; break
            if max_fav >= SCRATCH_TH and pnl < max_fav and pnl < SCRATCH_TH:
                outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "scratch"; break
        if outcome_pnl is None:
            outcome_pnl = max_fav
            outcome_lag = (later[-1].ts_ms - c["ts_ms"]) if later else 0
            outcome_kind = "expired"
        c["observed_outcome_pnl_sol"] = float(outcome_pnl)
        c["observed_outcome_lag_ms"] = int(outcome_lag or 0)
        c["observed_outcome_kind"] = outcome_kind

    # Aggregate observations.
    bank_obs = 0
    scratch_obs = 0
    neg_obs = 0
    neutral_obs = 0
    pending_obs = 0
    for c in candidates:
        v = c["observed_outcome_pnl_sol"]
        if v is None:
            pending_obs += 1
            continue
        if v >= BANK_TH:
            bank_obs += 1
        elif v >= SCRATCH_TH:
            scratch_obs += 1
        elif v <= LOSS_TH:
            neg_obs += 1
        else:
            neutral_obs += 1

    e_stats = engine.stats
    runtime_s = int((_now_ms() - t_start_wall) / 1000)

    one_bank_actual_entries = sum(
        1 for c in candidates if c["rule_id"] == "v42h_one_bank_plus_continuation"
    )

    verdict_n = len(candidates) >= args.target_pass
    verdict_no_neg = neg_obs == 0
    verdict_no_lookahead = e_stats.get("lookahead_blocks", 0) == 0
    verdict_no_one_bank = one_bank_actual_entries == 0
    verdict_pass = verdict_n and verdict_no_neg and verdict_no_lookahead and verdict_no_one_bank

    md: List[str] = []
    md.append("# V42HSAFE_NO_SEND_REPORT\n")
    md.append(f"- amount_sol: {args.amount_sol}")
    md.append(f"- runtime_s: {runtime_s}")
    md.append(f"- rules: `{args.rules_json}`")
    md.append(f"- target_candidates: {args.target_pass}")
    md.append(f"- fee_bps (protocol): {fee_bps}")
    md.append(f"- creator_fee_bps: {creator_fee_bps}")
    md.append("")
    md.append("## Engine sanity")
    md.append(f"- snapshots_ingested: **{e_stats['snapshots_ingested']}**")
    md.append(f"- virtual_tickets_opened: **{e_stats['tickets_opened']}**")
    md.append(f"- virtual_banks: **{e_stats['tickets_banked']}**")
    md.append(f"- virtual_scratch: **{e_stats['tickets_scratched']}**")
    md.append(f"- virtual_losses: **{e_stats['tickets_lost']}**")
    md.append(f"- virtual_expired: **{e_stats['tickets_expired']}**")
    md.append(f"- lookahead_blocks (must be 0 for PASS): **{e_stats.get('lookahead_blocks', 0)}**")
    md.append("")
    md.append("## Rule pass counts (actual-mode only)")
    for rid, cnt in rule_pass_counts_actual.most_common():
        md.append(f"- {rid}: {cnt}")
    if not rule_pass_counts_actual:
        md.append("- (none)")
    md.append("")
    md.append("## Shadow-mode rule emit counts (visibility only — not entered)")
    for rid, cnt in rule_pass_counts_shadow.most_common():
        md.append(f"- {rid}: {cnt}")
    if not rule_pass_counts_shadow:
        md.append("- (none)")
    md.append("")
    md.append("## Survival-gate ALLOWED")
    for reason, cnt in gate_allow_counts.most_common():
        md.append(f"- {reason}: {cnt}")
    if not gate_allow_counts:
        md.append("- (none)")
    md.append("")
    md.append("## Survival-gate BLOCKED (per blocker)")
    for reason, cnt in gate_block_counts.most_common():
        md.append(f"- {reason}: {cnt}")
    if not gate_block_counts:
        md.append("- (none)")
    md.append("")
    md.append("## Late-entry ALLOWED")
    for reason, cnt in late_allow_counts.most_common():
        md.append(f"- {reason}: {cnt}")
    if not late_allow_counts:
        md.append("- (none)")
    md.append("")
    md.append("## Late-entry BLOCKED")
    for reason, cnt in late_block_counts.most_common():
        md.append(f"- {reason}: {cnt}")
    if not late_block_counts:
        md.append("- (none)")
    md.append("")
    md.append("## Top rule-block reasons")
    for reason, cnt in rule_block_counts.most_common(15):
        md.append(f"- {reason}: {cnt}")
    md.append("")
    md.append("## V42H-SAFE candidate entries")
    md.append(f"- **candidates_count: {len(candidates)}**")
    md.append(f"- one_bank_actual_entries: {one_bank_actual_entries}  (must be 0)")
    md.append("")
    md.append("## Causal observed outcomes (exit-policy applied within max_hold_ms)")
    md.append(f"- observed_bank (>= +0.00060 SOL): **{bank_obs}**")
    md.append(f"- observed_scratch: **{scratch_obs}**")
    md.append(f"- observed_clamp_loss (<= -0.00050 SOL): **{neg_obs}**")
    md.append(f"- observed_neutral: **{neutral_obs}**")
    md.append(f"- pending (no future snap): **{pending_obs}**")
    md.append("")
    md.append("## Verdict")
    md.append(f"- meets_target_count (>= {args.target_pass}): **{verdict_n}**")
    md.append(f"- zero_observed_negative_outcomes: **{verdict_no_neg}**")
    md.append(f"- zero_lookahead_violations: **{verdict_no_lookahead}**")
    md.append(f"- no_one_bank_actual_entries: **{verdict_no_one_bank}**")
    md.append(f"- **OVERALL Phase-5 verdict: {'PASS' if verdict_pass else 'FAIL'}**")
    md.append("")
    md.append("## Per-candidate detail")
    md.append("| # | mint | rule | last_bank_pnl | last_bank_age_ms | decision_quote_sol_reflexive | current_local_quote | obs_pnl | obs_kind | obs_lag_ms |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(candidates):
        obs = c["observed_outcome_pnl_sol"]
        obs_s = "PENDING" if obs is None else f"{obs:+.9f}"
        lbpnl = c["last_bank_pnl_sol"]
        lbpnl_s = "-" if lbpnl is None else f"{lbpnl:+.6f}"
        md.append(
            f"| {i+1} | `{c['mint'][:4]}..{c['mint'][-4:]}` | {c['rule_id']} | "
            f"{lbpnl_s} | {c['last_bank_age_ms']} | "
            f"{c['decision_quote_sol_reflexive']:.9f} | "
            f"{c['current_local_quote']:.9f} | "
            f"{obs_s} | {c['observed_outcome_kind']} | "
            f"{c['observed_outcome_lag_ms'] if c['observed_outcome_lag_ms'] is not None else '-'} |"
        )
    md.append("")
    md.append("## Stage-A readiness gate")
    md.append(f"- Phase-6 (corrected dry-live) gate: **{'GO' if verdict_pass else 'NO-GO'}**")

    Path(args.out_md).write_text("\n".join(md), encoding="utf-8")
    try:
        jsonl_fp.close()
    except Exception:
        pass
    log(
        f"V42HSAFE-NO-SEND end candidates={len(candidates)} bank={bank_obs} "
        f"scratch={scratch_obs} neg={neg_obs} neutral={neutral_obs} "
        f"pending={pending_obs} verdict={'PASS' if verdict_pass else 'FAIL'}"
    )
    if log_fp is not None:
        log_fp.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
