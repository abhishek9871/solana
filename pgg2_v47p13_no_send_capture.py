"""V47-P13 - No-send capture at 0.1 SOL, REGIME GATE DROPPED.

Forks pgg2_v47p12_no_send_capture.py with two changes ONLY:
  1. amount_sol = 0.1 (was 0.05)
  2. Regime gate REMOVED entirely - every shred event proceeds straight to
     V46 rule evaluation. No V47P12RegimeGate import, no is_hot_regime() call.

All other wiring is identical:
  - shredSubscribe over SOLANATRACKER_RPC_WS (parse_base64_shred_for_pump_events)
  - PumpFun accountSubscribe oracle (pgg2_v42_curve_account_subscriber)
  - V46PendingFlowBuffer
  - V42H local CPMM quote (pgg2_v42h_local_curve_quote)
  - V46 pending-flow projector
  - V46 rules JSON

NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re as _re
import sys
import time
from collections import Counter, deque
from pathlib import Path
from statistics import median
from typing import Any, Deque, Dict, List, Optional, Tuple


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
            f"V47P13-NO-SEND-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", required=True)
    ap.add_argument(
        "--out-jsonl",
        default="/root/piggy/data/v47p13_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--rules-json",
        default="/root/piggy/data/v46_shred_pending_flow_rules.json",
    )
    ap.add_argument("--amount-sol", type=float, default=0.1)
    ap.add_argument("--max-seconds", type=int, default=600)
    ap.add_argument("--target-pass", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--lead-min-ms", type=int, default=100,
                    help="Minimum source_lead_ms accepted as a candidate")
    ap.add_argument("--debug-log", default="")
    return ap.parse_args()


async def amain() -> int:
    sys.path.insert(0, "/root/piggy")
    args = parse_args()

    try:
        from pgg2_v42_curve_account_subscriber import (  # type: ignore
            CurveAccountSubscriberOracle,
        )
        from pgg2_direct_pump import DirectPumpQuoteBroker  # type: ignore
        from birth_first_sniper import (  # type: ignore
            BotConfig, parse_base64_shred_for_pump_events,
        )
        from pgg2_v42h_local_curve_quote import (  # type: ignore
            curve_state_from_subscriber_point,
            LAMPORTS_PER_SOL,
            DEFAULT_TX_FEE_SOL,
            local_buy_quote_tokens_raw,
            local_sell_quote_sol,
        )
        from pgg2_v46_pending_flow_buffer import V46PendingFlowBuffer
        from pgg2_v46_pending_flow_projector import project_with_pending_flow
        from pgg2_v46_rule_evaluator import (
            load_rules, evaluate_rules, exit_policy,
        )
    except Exception as exc:
        print(f"V47P13-NO-SEND-ABORT import:{type(exc).__name__}:{exc}")
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

    rules_cfg = load_rules(args.rules_json)
    exit_pol = exit_policy(args.rules_json)
    BANK_TH = float(exit_pol.get("bank_threshold_sol", 0.00060))
    SCRATCH_TH = float(exit_pol.get("scratch_threshold_sol", 0.00005))
    LOSS_TH = float(exit_pol.get("clamp_threshold_sol", -0.00050))
    MAX_HOLD = int(exit_pol.get("max_hold_ms", 1500))
    EXTEND_IF_POS = bool(
        exit_pol.get("extend_hold_if_positive_and_improving", True)
    )
    MAX_EXTEND_MS = 3000

    log(
        f"V47P13-NO-SEND start amount_sol={args.amount_sol} "
        f"max_seconds={args.max_seconds} target_pass={args.target_pass} "
        f"rules_path={args.rules_json} bank={BANK_TH} scratch={SCRATCH_TH} "
        f"clamp={LOSS_TH} max_hold={MAX_HOLD} lead_min={args.lead_min_ms} "
        f"regime_gate=DROPPED"
    )
    if os.environ.get("PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE", "0") != "1":
        log("V47P13-NO-SEND WARNING: PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE != 1")
    if os.environ.get("V47P13_NO_SEND", "0") != "1":
        log("V47P13-NO-SEND WARNING: V47P13_NO_SEND env not 1")

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(
        f"V47P13-NO-SEND fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}"
    )

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    buffer_ = V46PendingFlowBuffer(logger=log, emit_sample_denom=200)

    candidates: List[Dict[str, Any]] = []
    rule_pass_counts: Counter = Counter()
    block_counts: Counter = Counter()
    seen_pass_mints: set = set()
    lookahead_block_count = 0

    raw_buys_seen = 0
    raw_sells_seen = 0
    curve_updates_seen = 0
    rule_evals_total = 0
    rule_evals_passed = 0
    snapshots_total = 0
    lead_samples_at_decision_ms: List[float] = []

    hot_mint_last_seen: Dict[str, int] = {}
    seen_curve_ts: Dict[str, int] = {}
    last_vsol_per_mint: Dict[str, int] = {}
    last_curve_delta_vsol: Dict[str, int] = {}
    last_curve_pos_below_bank: Dict[str, bool] = {}
    last_quote_was_error: Dict[str, bool] = {}
    quote_available_after_missing: Dict[str, bool] = {}

    out_jsonl_path = Path(args.out_jsonl)
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fp = open(str(out_jsonl_path), "w", encoding="utf-8")

    pending_candidates: Dict[Tuple[str, int], Dict[str, Any]] = {}

    shred_stop = asyncio.Event()

    async def _shred_listener():
        nonlocal raw_buys_seen, raw_sells_seen
        try:
            import websockets  # type: ignore
        except Exception as exc:
            log(f"V47P13-NO-SEND ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V47P13-NO-SEND no_ws_url")
            return
        backoff = 2.0
        while not shred_stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=60,
                    max_queue=4096, max_size=8 * 1024 * 1024,
                ) as ws:
                    backoff = 2.0
                    sub = {
                        "jsonrpc": "2.0", "id": 91247,
                        "method": "shredSubscribe",
                        "params": [
                            {"accountInclude": [PUMP_PROGRAM],
                             "accountRequired": [PUMP_PROGRAM],
                             "vote": False},
                            {"encoding": "base64",
                             "transactionDetails": "full",
                             "maxSupportedTransactionVersion": 0},
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    log("V47P13-NO-SEND shred_subscribed")
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
                        result = (
                            (data.get("params") or {}).get("result") or {}
                        )
                        try:
                            events_ = list(
                                parse_base64_shred_for_pump_events(
                                    result, set()
                                )
                            )
                        except Exception:
                            events_ = []
                        ts_ms = _now_ms()
                        for ev in events_:
                            m = getattr(ev, "mint", "") or ""
                            if not m or getattr(ev, "kind", "") != "trade":
                                continue
                            slot = int(getattr(ev, "slot", 0) or 0)
                            sol_lamports = int(
                                getattr(ev, "sol_lamports", 0) or 0
                            )
                            tokens = int(
                                getattr(ev, "token_amount", 0) or 0
                            )
                            sol_in = sol_lamports / 1_000_000_000.0
                            signer = str(getattr(ev, "signer", "") or "")
                            is_buy = bool(getattr(ev, "is_buy", False))
                            if is_buy:
                                buffer_.ingest_pump_buy(
                                    m, sol_in, signer, slot, ts_ms,
                                )
                                raw_buys_seen += 1
                                _maybe_evaluate(
                                    m, ts_ms, slot, sol_in, signer,
                                )
                            else:
                                buffer_.ingest_pump_sell(
                                    m, tokens, signer, slot, ts_ms, 0.0,
                                )
                                raw_sells_seen += 1
                            hot_mint_last_seen[m] = ts_ms
                            if len(hot_mint_last_seen) <= args.max_hot_mints:
                                oracle.request_subscription(m)
                            oracle.mark_feed_event(m, ts_ms)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(f"V47P13-NO-SEND shred_reconnect "
                    f"exc={type(exc).__name__}:{exc}")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2.0, 30.0)

    def _curve_state_at_or_before(mint: str, ts_ms_now: int):
        st = oracle._states.get(mint)
        if st is None or not st.points:
            return None, 0
        cs_pt = None
        for p in reversed(st.points):
            if p.error:
                continue
            if int(p.ts_ms) <= int(ts_ms_now):
                cs_pt = p
                break
        if cs_pt is None:
            return None, 0
        cs = curve_state_from_subscriber_point(
            int(cs_pt.virtual_sol_reserves),
            int(cs_pt.virtual_token_reserves),
            int(cs_pt.real_token_reserves),
            fee_bps,
            creator_fee_bps,
        )
        return cs, int(cs_pt.ts_ms)

    def _maybe_evaluate(
        mint: str, ts_ms_now: int, slot: int, sol_in: float, signer: str,
    ):
        nonlocal rule_evals_total, rule_evals_passed, lookahead_block_count
        if len(candidates) >= args.target_pass:
            return

        # V47-P13: NO REGIME GATE. Every shred event proceeds directly to
        # V46 rule evaluation against the latest curve snapshot.

        cs, cu_ts = _curve_state_at_or_before(mint, ts_ms_now)
        if cs is None:
            return

        snap = buffer_.get_state(mint, ts_ms_now, cu_ts)
        if int(snap.get("latest_raw_buy_ts_ms", 0)) > ts_ms_now:
            lookahead_block_count += 1
            return
        if int(snap.get("latest_curve_update_ts_ms", 0)) > ts_ms_now:
            lookahead_block_count += 1
            return

        source_lead_ms = float(int(ts_ms_now) - int(cu_ts)) if cu_ts > 0 else 0.0

        pending_buys = buffer_.pending_buys(mint, ts_ms_now, cu_ts, 250)
        pending_sells = buffer_.pending_sells(mint, ts_ms_now, cu_ts, 250)

        # KEY DIFFERENCE vs V46/V47P12: our_buy_sol = 0.1 (was 0.015 / 0.05).
        proj = project_with_pending_flow(
            latest_curve_state=cs,
            our_buy_sol=float(args.amount_sol),
            pending_buys=pending_buys,
            pending_sells=pending_sells,
            unique_buyers=int(snap.get("unique_buyers_250ms", 0)),
            cluster_speed=float(snap.get("buy_cluster_speed_250ms", 0.0)),
            source_lead_ms=source_lead_ms,
            mint_for_log=mint,
            bank_target_sol=BANK_TH,
        )

        latest_curve_delta_positive_but_below_bank = bool(
            last_curve_pos_below_bank.get(mint, False)
        )
        qa_after_missing = bool(quote_available_after_missing.get(mint, False))
        raw_pending_buy_flow_continues = (
            int(snap.get("pending_buy_count_250ms", 0)) >= 2
        )

        state_for_eval = {
            "raw_buy_visible_before_curve_update": bool(
                not snap.get("reflected_in_curve", False)
            ),
            "reflected_in_curve": bool(
                snap.get("reflected_in_curve", False)
            ),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)
            ),
            "pending_sell_sol_250ms": float(
                snap.get("pending_sell_sol_250ms", 0.0)
            ),
            "largest_pending_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)
            ),
            "pending_buy_count_250ms": int(
                snap.get("pending_buy_count_250ms", 0)
            ),
            "projected_pnl": float(proj.get("projected_pnl", 0.0)),
            "stress_pnl": float(proj.get("stress_pnl", 0.0)),
            "source_lead_ms": float(proj.get("source_lead_ms", 0.0)),
            "route": "pump_bc",
            "sim_needed": 0,
            "latest_curve_delta_positive_but_below_bank":
                latest_curve_delta_positive_but_below_bank,
            "quote_available_after_missing": qa_after_missing,
            "raw_pending_buy_flow_continues": raw_pending_buy_flow_continues,
        }

        passing, blocks = evaluate_rules(rules_cfg, state_for_eval)
        rule_evals_total += 1
        if blocks:
            for b in blocks:
                block_counts[b.split(":")[1].split("=")[0] if ":" in b else b] += 1

        if passing and float(state_for_eval["source_lead_ms"]) < float(args.lead_min_ms):
            block_counts["source_lead_too_low_after_pass"] += 1
            passing = []

        if not passing:
            return

        rule_evals_passed += 1
        rule_id = passing[0]
        rule_pass_counts[rule_id] += 1
        seen_pass_mints.add(mint)
        lead_samples_at_decision_ms.append(
            float(state_for_eval["source_lead_ms"])
        )

        tok_we_got, _ = local_buy_quote_tokens_raw(cs, float(args.amount_sol))
        sell_lams, _ = local_sell_quote_sol(cs, int(tok_we_got))
        decision_quote_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)

        rec = {
            "type": "v47p13_candidate",
            "decision_ts_ms": int(ts_ms_now),
            "mint": mint,
            "rule_id": rule_id,
            "slot_at_decision": int(slot),
            "sol_in_at_decision": float(sol_in),
            "signer_at_decision": signer,
            "our_buy_sol": float(args.amount_sol),
            # pending-flow buffer fields
            "pending_buy_count_50ms": int(snap.get("pending_buy_count_50ms", 0)),
            "pending_buy_sol_50ms": float(snap.get("pending_buy_sol_50ms", 0.0)),
            "pending_buy_count_100ms": int(snap.get("pending_buy_count_100ms", 0)),
            "pending_buy_sol_100ms": float(snap.get("pending_buy_sol_100ms", 0.0)),
            "pending_buy_count_250ms": int(snap.get("pending_buy_count_250ms", 0)),
            "pending_buy_sol_250ms": float(snap.get("pending_buy_sol_250ms", 0.0)),
            "pending_buy_count_500ms": int(snap.get("pending_buy_count_500ms", 0)),
            "pending_buy_sol_500ms": float(snap.get("pending_buy_sol_500ms", 0.0)),
            "pending_buy_count_1000ms": int(snap.get("pending_buy_count_1000ms", 0)),
            "pending_buy_sol_1000ms": float(snap.get("pending_buy_sol_1000ms", 0.0)),
            "pending_sell_count_250ms": int(snap.get("pending_sell_count_250ms", 0)),
            "pending_sell_sol_250ms": float(snap.get("pending_sell_sol_250ms", 0.0)),
            "net_pending_sol_250ms": float(snap.get("net_pending_sol_250ms", 0.0)),
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "largest_pending_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)
            ),
            "buy_cluster_speed_250ms": float(snap.get("buy_cluster_speed_250ms", 0.0)),
            "raw_buy_lead_ms_latest": float(snap.get("raw_buy_lead_ms_latest", 0.0)),
            "reflected_in_curve": bool(snap.get("reflected_in_curve", False)),
            # projector at 0.1 SOL
            "projected_pnl": float(proj.get("projected_pnl", 0.0)),
            "stress_pnl": float(proj.get("stress_pnl", 0.0)),
            "pending_flow_sol_used": float(proj.get("pending_flow_sol_used", 0.0)),
            "projected_sell_out": float(proj.get("projected_sell_out", 0.0)),
            "required_flow_sol": float(proj.get("required_flow_sol", 0.0)),
            "source_lead_ms": float(proj.get("source_lead_ms", 0.0)),
            "confidence": float(proj.get("confidence", 0.0)),
            "stress_pnl_50pct_drop": float(proj.get("stress_pnl_50pct_drop", 0.0)),
            "stress_pnl_largest_priced_in": float(
                proj.get("stress_pnl_largest_priced_in", 0.0)
            ),
            "stress_pnl_sell_appears": float(
                proj.get("stress_pnl_sell_appears", 0.0)
            ),
            "stress_pnl_we_land_last": float(
                proj.get("stress_pnl_we_land_last", 0.0)
            ),
            "raw_buy_visible_before_curve_update": bool(
                state_for_eval["raw_buy_visible_before_curve_update"]
            ),
            "decision_quote_sol": float(decision_quote_sol),
            "decision_buy_tokens_raw": int(tok_we_got),
            "decision_curve_state": [
                int(cs.virtual_sol_reserves),
                int(cs.virtual_token_reserves),
                int(cs.real_token_reserves),
            ],
            "decision_curve_update_ts_ms": int(cu_ts),
            "observed_label_pnl": None,
            "observed_label_kind": None,
            "observed_label_lag_ms": None,
            "future_snaps_used_count": 0,
        }
        candidates.append(rec)
        pending_candidates[(mint, int(ts_ms_now))] = rec
        jsonl_fp.write(json.dumps(rec) + "\n")
        jsonl_fp.flush()
        log(
            f"V47P13-CANDIDATE rule={rule_id} mint={_short(mint)} "
            f"size=0.1 "
            f"proj={rec['projected_pnl']:+.6f} "
            f"stress={rec['stress_pnl']:+.6f} "
            f"lead={rec['source_lead_ms']:+.0f}ms "
            f"pb250={rec['pending_buy_count_250ms']} "
            f"pb_sol250={rec['pending_buy_sol_250ms']:.4f} "
            f"target_progress={len(candidates)}/{args.target_pass}"
        )

    shred_task = asyncio.create_task(_shred_listener())

    deadline_ms = _now_ms() + args.max_seconds * 1000
    t_start_wall = _now_ms()
    next_progress_ms = t_start_wall + 30_000

    try:
        while _now_ms() < deadline_ms:
            await asyncio.sleep(0.05)
            now_ts = _now_ms()
            stale_cutoff = now_ts - 30_000
            cold = [m for m, t in hot_mint_last_seen.items()
                    if t < stale_cutoff]
            for m in cold:
                hot_mint_last_seen.pop(m, None)

            for mint in list(hot_mint_last_seen.keys())[: args.max_hot_mints]:
                st = oracle._states.get(mint)
                if st is None or not st.points:
                    continue
                last_ingest_ts = seen_curve_ts.get(mint, 0)
                new_points = []
                for p in st.points:
                    if int(p.ts_ms) > int(last_ingest_ts):
                        new_points.append(p)
                if not new_points:
                    continue
                new_points.sort(key=lambda x: x.ts_ms)
                for p in new_points:
                    seen_curve_ts[mint] = int(p.ts_ms)
                    if p.error:
                        last_quote_was_error[mint] = True
                        continue
                    buffer_.mark_curve_update(mint, int(p.ts_ms))
                    if last_quote_was_error.get(mint, False):
                        quote_available_after_missing[mint] = True
                    else:
                        quote_available_after_missing[mint] = False
                    last_quote_was_error[mint] = False
                    prev_vsol = last_vsol_per_mint.get(
                        mint, int(p.virtual_sol_reserves)
                    )
                    delta_vsol = int(p.virtual_sol_reserves) - int(prev_vsol)
                    last_vsol_per_mint[mint] = int(p.virtual_sol_reserves)
                    last_curve_delta_vsol[mint] = int(delta_vsol)
                    last_curve_pos_below_bank[mint] = bool(
                        delta_vsol > 0 and delta_vsol < int(0.05 * 1_000_000_000)
                    )
                    curve_updates_seen += 1
                    snapshots_total += 1

                    cs_now = curve_state_from_subscriber_point(
                        int(p.virtual_sol_reserves),
                        int(p.virtual_token_reserves),
                        int(p.real_token_reserves),
                        fee_bps,
                        creator_fee_bps,
                    )
                    for key, rec in list(pending_candidates.items()):
                        m, dts = key
                        if m != mint:
                            continue
                        if int(p.ts_ms) <= int(dts):
                            continue
                        tok_at_dec = int(rec.get("decision_buy_tokens_raw", 0))
                        if tok_at_dec <= 0:
                            sell_lams_now = 0
                        else:
                            sell_lams_now, _ = local_sell_quote_sol(
                                cs_now, int(tok_at_dec)
                            )
                        sell_sol_now = float(sell_lams_now) / float(LAMPORTS_PER_SOL)
                        pnl_now = (
                            sell_sol_now
                            - float(args.amount_sol)
                            - 2.0 * float(DEFAULT_TX_FEE_SOL)
                        )
                        lag = int(p.ts_ms) - int(dts)
                        rec["future_snaps_used_count"] = (
                            int(rec.get("future_snaps_used_count", 0)) + 1
                        )
                        if pnl_now >= BANK_TH:
                            rec["observed_label_pnl"] = float(pnl_now)
                            rec["observed_label_kind"] = "bank"
                            rec["observed_label_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47p13_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "rule_id", "decision_ts_ms",
                                        "observed_label_pnl",
                                        "observed_label_kind",
                                        "observed_label_lag_ms",
                                        "future_snaps_used_count",
                                    )
                                }, "ts_ms": int(p.ts_ms)}) + "\n"
                            )
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            log(
                                f"V47P13-OBSERVED bank pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )
                            continue
                        if pnl_now <= LOSS_TH:
                            rec["observed_label_pnl"] = float(pnl_now)
                            rec["observed_label_kind"] = "clamp_loss"
                            rec["observed_label_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47p13_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "rule_id", "decision_ts_ms",
                                        "observed_label_pnl",
                                        "observed_label_kind",
                                        "observed_label_lag_ms",
                                        "future_snaps_used_count",
                                    )
                                }, "ts_ms": int(p.ts_ms)}) + "\n"
                            )
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            log(
                                f"V47P13-OBSERVED clamp_loss pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )
                            continue
                        hold_ms = MAX_HOLD
                        if EXTEND_IF_POS and pnl_now > 0:
                            hold_ms = MAX_EXTEND_MS
                        if lag >= hold_ms:
                            label_kind = (
                                "scratch" if abs(pnl_now) < SCRATCH_TH
                                else ("neutral" if pnl_now > 0 else "expired_loss")
                            )
                            rec["observed_label_pnl"] = float(pnl_now)
                            rec["observed_label_kind"] = label_kind
                            rec["observed_label_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47p13_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "rule_id", "decision_ts_ms",
                                        "observed_label_pnl",
                                        "observed_label_kind",
                                        "observed_label_lag_ms",
                                        "future_snaps_used_count",
                                    )
                                }, "ts_ms": int(p.ts_ms)}) + "\n"
                            )
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            log(
                                f"V47P13-OBSERVED {label_kind} pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )

            if now_ts >= next_progress_ms:
                log(
                    f"V47P13-NO-SEND progress elapsed_s="
                    f"{(now_ts - t_start_wall)/1000.0:.0f} "
                    f"buys={raw_buys_seen} sells={raw_sells_seen} "
                    f"curve_updates={curve_updates_seen} "
                    f"rule_evals={rule_evals_total} "
                    f"passes={rule_evals_passed} "
                    f"candidates={len(candidates)} "
                    f"pending_label={len(pending_candidates)} "
                    f"hot_mints={len(hot_mint_last_seen)}"
                )
                next_progress_ms = now_ts + 30_000

            if (
                len(candidates) >= args.target_pass
                and not pending_candidates
            ):
                break
    finally:
        shred_stop.set()
        try:
            shred_task.cancel()
            try:
                await shred_task
            except Exception:
                pass
        except Exception:
            pass
        for key, rec in list(pending_candidates.items()):
            if rec.get("observed_label_kind") is None:
                rec["observed_label_kind"] = "pending"
                rec["observed_label_pnl"] = None
                rec["observed_label_lag_ms"] = None
                jsonl_fp.write(
                    json.dumps({"type": "v47p13_observed", **{
                        k: rec.get(k) for k in (
                            "mint", "rule_id", "decision_ts_ms",
                            "observed_label_pnl",
                            "observed_label_kind",
                            "observed_label_lag_ms",
                            "future_snaps_used_count",
                        )
                    }, "ts_ms": _now_ms()}) + "\n"
                )
                jsonl_fp.flush()
        try:
            await oracle.stop()
        except Exception:
            pass
        try:
            jsonl_fp.close()
        except Exception:
            pass
        if log_fp is not None:
            try:
                log_fp.close()
            except Exception:
                pass

    elapsed_s = (_now_ms() - t_start_wall) / 1000.0

    bank_count = scratch_count = clamp_count = neutral_count = pending_count = 0
    expired_loss_count = 0
    for rec in candidates:
        k = rec.get("observed_label_kind") or "pending"
        if k == "bank":
            bank_count += 1
        elif k == "scratch":
            scratch_count += 1
        elif k == "clamp_loss":
            clamp_count += 1
        elif k == "neutral":
            neutral_count += 1
        elif k == "expired_loss":
            expired_loss_count += 1
        else:
            pending_count += 1

    leads = sorted(lead_samples_at_decision_ms)
    import statistics as _stats

    def _pctile(values, q):
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        n = len(values)
        k = q * (n - 1)
        f = int(k)
        c = min(f + 1, n - 1)
        return values[f] + (values[c] - values[f]) * (k - f)

    lead_median = _stats.median(leads) if leads else 0.0
    lead_p25 = _pctile(leads, 0.25) if leads else 0.0
    lead_p75 = _pctile(leads, 0.75) if leads else 0.0
    lead_p95 = _pctile(leads, 0.95) if leads else 0.0
    lead_min = min(leads) if leads else 0.0
    lead_max = max(leads) if leads else 0.0

    # Fee math at 0.1 SOL (1% protocol + 0.05% creator + 2 sig fees).
    pf_buy = float(args.amount_sol) * 0.01
    cf_buy = float(args.amount_sol) * 0.0005
    sig_fee_total = 2.0 * float(0.0000287)
    rt_fee_drag = 2.0 * float(args.amount_sol) * (0.01 + 0.0005) + sig_fee_total
    bps_required = BANK_TH / float(args.amount_sol) * 10000.0 if args.amount_sol > 0 else 0.0

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V47-P13 - No-Send Report (Regime Gate DROPPED)\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- amount_sol: {args.amount_sol}  "
                f"(EXPLICIT user-authorized for V47-P13)\n")
        f.write(f"- runtime_s: {elapsed_s:.1f}\n")
        f.write(f"- rules_path: {args.rules_json}\n")
        f.write(f"- bank_threshold_sol: {BANK_TH}\n")
        f.write(f"- scratch_threshold_sol: {SCRATCH_TH}\n")
        f.write(f"- clamp_threshold_sol: {LOSS_TH}\n")
        f.write(f"- lead_min_ms: {args.lead_min_ms}\n")
        f.write(f"- regime_gate: DROPPED (every shred event proceeds straight "
                f"to V46 rule evaluation)\n\n")
        f.write("## Engine sanity\n\n")
        f.write(f"- raw_pump_buys_seen: {raw_buys_seen}\n")
        f.write(f"- raw_pump_sells_seen: {raw_sells_seen}\n")
        f.write(f"- curve_updates_seen: {curve_updates_seen}\n")
        f.write(f"- snapshots: {snapshots_total}\n")
        f.write(f"- rule_evaluations: {rule_evals_total}\n")
        f.write(f"- rule_passes: {rule_evals_passed}\n")
        f.write(f"- lookahead_blocks: {lookahead_block_count}\n")
        f.write(f"- hot_mints_terminal: {len(hot_mint_last_seen)}\n\n")
        f.write("## Fee math at 0.1 SOL\n\n")
        f.write(f"- protocol_fee_buy_sol: {pf_buy:.6f}\n")
        f.write(f"- creator_fee_buy_sol:  {cf_buy:.6f}\n")
        f.write(f"- 2x signature_fees_sol: {sig_fee_total:.6f}\n")
        f.write(
            f"- approx_round_trip_fee_drag_sol: {rt_fee_drag:.6f}\n"
        )
        f.write(
            f"- required_edge_bps_for_bank_threshold: "
            f"{bps_required:.2f} bps\n"
        )
        f.write(
            "  - At 0.015 SOL: "
            f"{BANK_TH/0.015*10000.0:.2f} bps required. "
            "At 0.05 SOL: "
            f"{BANK_TH/0.05*10000.0:.2f} bps required. "
            "At 0.1 SOL the bank floor as a percentage of size is "
            f"{0.1/0.015:.2f}x easier than 0.015 SOL.\n"
        )
        f.write("\n## Raw shred lead-time at decision (ms)\n\n")
        if leads:
            f.write(f"- n: {len(leads)}\n")
            f.write(f"- min: {lead_min:.1f}\n")
            f.write(f"- p25: {lead_p25:.1f}\n")
            f.write(f"- median: {lead_median:.1f}\n")
            f.write(f"- p75: {lead_p75:.1f}\n")
            f.write(f"- p95: {lead_p95:.1f}\n")
            f.write(f"- max: {lead_max:.1f}\n")
        else:
            f.write("- (no candidates)\n")
        f.write("\n## Rule pass counts (at 0.1 SOL)\n\n")
        for r in sorted(rule_pass_counts.keys()):
            f.write(f"- {r}: {rule_pass_counts[r]}\n")
        if not rule_pass_counts:
            f.write("- (none)\n")
        f.write("\n## Top rule-block reasons\n\n")
        for reason, cnt in block_counts.most_common(15):
            f.write(f"- {reason}: {cnt}\n")
        if not block_counts:
            f.write("- (none)\n")
        f.write("\n## V47-P13 candidates\n\n")
        f.write(f"- candidates_count: {len(candidates)}\n")
        f.write(f"- unique_mints: {len(seen_pass_mints)}\n\n")
        f.write("## Causal observed outcomes (at 0.1 SOL)\n\n")
        f.write(f"- bank: {bank_count}\n")
        f.write(f"- scratch: {scratch_count}\n")
        f.write(f"- clamp_loss: {clamp_count}\n")
        f.write(f"- expired_loss: {expired_loss_count}\n")
        f.write(f"- neutral: {neutral_count}\n")
        f.write(f"- pending: {pending_count}\n\n")
        f.write("## Verdict\n\n")
        meets_target = len(candidates) >= int(args.target_pass)
        zero_neg = (clamp_count == 0 and expired_loss_count == 0)
        zero_la = (lookahead_block_count == 0)
        all_leads_ok = (
            all(
                float(r.get("source_lead_ms", 0.0)) >= float(args.lead_min_ms)
                for r in candidates
            )
            if candidates else False
        )
        all_proj_ok = (
            all(
                float(r.get("projected_pnl", 0.0)) >= BANK_TH
                for r in candidates
            )
            if candidates else False
        )
        overall = bool(
            meets_target and zero_neg and zero_la
            and all_leads_ok and all_proj_ok
        )
        f.write(f"- meets_target_count: {meets_target} ({len(candidates)}/{args.target_pass})\n")
        f.write(f"- zero_observed_negative_outcomes: {zero_neg}\n")
        f.write(f"- zero_lookahead_violations: {zero_la}\n")
        f.write(f"- all_candidates_lead_ms_ge_{args.lead_min_ms}: {all_leads_ok}\n")
        f.write(f"- projected_pnl_clears_bank_threshold_for_all: {all_proj_ok}\n")
        f.write(f"- OVERALL_VERDICT: {'PASS' if overall else 'FAIL'}\n\n")
        f.write("## Per-candidate detail\n\n")
        if candidates:
            f.write(
                "| # | rule_id | mint | size | proj_pnl | stress_pnl | "
                "lead_ms | dec_quote | obs_kind | obs_pnl | obs_lag_ms |\n"
            )
            f.write(
                "|---|---------|------|------|----------|------------|"
                "---------|-----------|----------|---------|------------|\n"
            )
            for i, r in enumerate(candidates, 1):
                f.write(
                    f"| {i} | {r.get('rule_id','')} | "
                    f"{_short(r.get('mint',''))} | "
                    f"{float(r.get('our_buy_sol',0.0)):.3f} | "
                    f"{float(r.get('projected_pnl',0.0)):+.6f} | "
                    f"{float(r.get('stress_pnl',0.0)):+.6f} | "
                    f"{float(r.get('source_lead_ms',0.0)):+.0f} | "
                    f"{float(r.get('decision_quote_sol',0.0)):.6f} | "
                    f"{r.get('observed_label_kind','pending') or 'pending'} | "
                    f"{('%+.6f' % float(r.get('observed_label_pnl') or 0.0)) if r.get('observed_label_pnl') is not None else 'n/a'} | "
                    f"{r.get('observed_label_lag_ms','') if r.get('observed_label_lag_ms') is not None else ''} |\n"
                )
        else:
            f.write("- (none)\n")
        f.write("\n## HONEST ASSESSMENT\n\n")
        if len(candidates) == 0:
            if rule_evals_total == 0:
                f.write(
                    "- V47-P13 yielded 0 candidates AND 0 rule evals (no "
                    "shred flow / no curve oracle data). EXACT BLOCKER: "
                    "feed/pipeline. Recommendation: confirm shred "
                    "subscription and oracle subscription, then retry.\n"
                )
            else:
                f.write(
                    f"- V47-P13 yielded 0 candidates from "
                    f"{rule_evals_total} rule evaluations. The regime "
                    "gate was DROPPED, so the blocker is purely in the "
                    f"V46 rules. Top rule-blocks: "
                    f"{dict(block_counts.most_common(5))}.\n"
                    "- Recommendation: at 0.1 SOL the projected PnL "
                    "still did not clear the V46 rule predicates within "
                    "the observation window. Required edge as bps is "
                    f"{bps_required:.2f}, which is the lowest of any "
                    "size tested. If even this is not firing, the issue "
                    "is rule strictness (not size, not regime). Do NOT "
                    "proceed to dry-live.\n"
                )
        elif clamp_count + expired_loss_count > 0:
            f.write(
                f"- V47-P13 produced {len(candidates)} candidate(s) but "
                f"{clamp_count + expired_loss_count} resulted in losses "
                f"({clamp_count} clamp, {expired_loss_count} expired). "
                "Per-rule loss breakdown follows.\n"
            )
            rule_outcome: Dict[str, Dict[str, int]] = {}
            for r in candidates:
                rid = r.get("rule_id", "?")
                kind = r.get("observed_label_kind", "pending") or "pending"
                d = rule_outcome.setdefault(
                    rid, {"bank": 0, "scratch": 0, "neutral": 0,
                          "clamp_loss": 0, "expired_loss": 0, "pending": 0}
                )
                d[kind] = d.get(kind, 0) + 1
            for rid, d in sorted(rule_outcome.items()):
                f.write(
                    f"  - {rid}: bank={d.get('bank',0)} "
                    f"scratch={d.get('scratch',0)} "
                    f"neutral={d.get('neutral',0)} "
                    f"clamp_loss={d.get('clamp_loss',0)} "
                    f"expired_loss={d.get('expired_loss',0)} "
                    f"pending={d.get('pending',0)}\n"
                )
            f.write(
                "- Recommendation: do NOT proceed to dry-live with the "
                "rule(s) showing losses. Per-rule causal review needed.\n"
            )
        elif meets_target and zero_neg:
            f.write(
                f"- V47-P13 PASSED: {len(candidates)} candidates, 0 "
                f"negative outcomes, all leads >= {args.lead_min_ms}ms, "
                "all projected_pnl >= bank floor, regime gate DROPPED.\n"
                "- Recommendation: at 0.1 SOL with no regime gate, this "
                "is a strong signal. Eligible to proceed to dry-live for "
                "execution validation, but ONLY upon user direction. "
                "Wallet impact ~40 percent per trade requires explicit "
                "confirmation.\n"
            )
        else:
            f.write(
                f"- V47-P13 produced {len(candidates)} candidate(s) "
                "without negatives but did not reach the target count "
                f"({args.target_pass}). Recommendation: run a longer "
                "capture or document the current rarity; do NOT proceed "
                "to dry-live until target count is met.\n"
            )

    log(f"V47P13-NO-SEND wrote {md_path}")
    log(
        f"V47P13-NO-SEND done elapsed_s={elapsed_s:.1f} "
        f"candidates={len(candidates)} "
        f"banks={bank_count} losses={clamp_count + expired_loss_count} "
        f"pending={pending_count}"
    )
    return 0 if (
        len(candidates) >= args.target_pass
        and clamp_count == 0
        and expired_loss_count == 0
        and lookahead_block_count == 0
    ) else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
