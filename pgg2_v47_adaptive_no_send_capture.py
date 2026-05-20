"""V47 Phase 3 - Adaptive-size no-send capture.

For each raw Pump BUY shred event on a tracked mint:
  1. Build pending_buys / pending_sells lists at decision_ts.
  2. Run V47 size selector across [0.001, 0.002, 0.003, 0.005, 0.0075,
     0.010, 0.015] (and optionally 0.020 in no-send mode only).
  3. If selected_size_sol is not None -> ENTRY CANDIDATE.
  4. Snapshot decision-time features.
  5. Observe future curve updates up to max_hold (with extension on
     positive+improving series).
  6. Label outcome per size-normalized exit policy.

Causality: every feature ts <= decision_ts_ms. NO LOOKAHEAD.

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
            f"V47-NO-SEND-ABORT forbidden_call_pattern={_pat}\n"
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
        default="/root/piggy/data/v47_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--rules-json",
        default="/root/piggy/data/v47_adaptive_size_rules.json",
    )
    ap.add_argument("--max-seconds", type=int, default=600)
    ap.add_argument("--target-pass", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--include-20m", action="store_true",
                    help="Include 0.020 SOL in size sweep (no-send only)")
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
        from pgg2_v47_size_selector import (
            select_size_for_candidate,
            DEFAULT_SIZES_SOL,
        )
    except Exception as exc:
        print(f"V47-NO-SEND-ABORT import:{type(exc).__name__}:{exc}")
        return 2

    # Load rules JSON (for sizes + exit policy + headers).
    rules_cfg: Dict[str, Any] = {}
    try:
        with open(args.rules_json, "r", encoding="utf-8") as f:
            rules_cfg = json.load(f)
    except Exception as exc:
        print(f"V47-NO-SEND-ABORT rules_load:{type(exc).__name__}:{exc}")
        return 2

    sizes_to_try = list(rules_cfg.get("sizes_sol", DEFAULT_SIZES_SOL))
    if args.include_20m and 0.020 not in sizes_to_try:
        sizes_to_try = sorted(set(list(sizes_to_try) + [0.020]))

    exit_pol = dict(rules_cfg.get("exit_policy") or {})
    SCRATCH_TH = float(exit_pol.get("scratch_threshold_sol", 0.00001))
    # clamp_threshold_sol_relative: -0.5 means a loss exceeding 50% of
    # the trade size is the clamp. Numerically we apply: clamp_th =
    # rel * size_sol (per-trade). But for V47 we use a hard zero floor:
    # observed_pnl < 0 -> live loss (since stress >= 0 demanded).
    CLAMP_REL = float(exit_pol.get("clamp_threshold_sol_relative", -0.5))
    MAX_HOLD = int(exit_pol.get("max_hold_ms", 1500))
    EXTEND_IF_POS = bool(
        exit_pol.get("extend_hold_if_positive_and_improving", True)
    )
    MAX_EXTEND_MS = int(exit_pol.get("max_extend_ms", 3000))

    log_fp = None
    if args.debug_log:
        log_fp = open(args.debug_log, "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        if log_fp is not None:
            log_fp.write(line + "\n")
            log_fp.flush()

    log(
        f"V47-NO-SEND start sizes={sizes_to_try} "
        f"max_seconds={args.max_seconds} target_pass={args.target_pass} "
        f"rules_path={args.rules_json} max_hold_ms={MAX_HOLD}"
    )
    if os.environ.get("PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE", "0") != "1":
        log("V47-NO-SEND WARNING: PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE != 1")

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(
        f"V47-NO-SEND fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}"
    )

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    buffer_ = V46PendingFlowBuffer(logger=log, emit_sample_denom=400)

    candidates: List[Dict[str, Any]] = []
    seen_pass_mints: set = set()
    lookahead_block_count = 0

    raw_buys_seen = 0
    raw_sells_seen = 0
    curve_updates_seen = 0
    snapshots_total = 0

    # Per-size blocker counters and accepted counts (for the spec's
    # per-size blocker table).
    per_size_counters: Dict[float, Counter] = {
        float(s): Counter() for s in sizes_to_try
    }
    per_size_tested: Dict[float, int] = {float(s): 0 for s in sizes_to_try}
    per_size_accepted: Dict[float, int] = {float(s): 0 for s in sizes_to_try}
    # Selected size distribution among candidates
    selected_size_counter: Counter = Counter()
    candidates_evaluated = 0
    candidates_with_selected_size = 0
    candidates_no_size_works = 0

    hot_mint_last_seen: Dict[str, int] = {}
    seen_curve_ts: Dict[str, int] = {}

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
            log(f"V47-NO-SEND ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V47-NO-SEND no_ws_url")
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
                        "jsonrpc": "2.0", "id": 91047,
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
                    log("V47-NO-SEND shred_subscribed")
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
                log(f"V47-NO-SEND shred_reconnect "
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
        nonlocal candidates_evaluated, candidates_with_selected_size
        nonlocal candidates_no_size_works, lookahead_block_count
        if len(candidates) >= args.target_pass:
            return
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

        # V47 entry filter: require raw buy visible AND not yet reflected,
        # mirroring V46's structural causality requirement (the size lift
        # is the V47 change; this guard stays).
        raw_buy_visible_before_curve_update = bool(
            not snap.get("reflected_in_curve", False)
        )
        if not raw_buy_visible_before_curve_update:
            return

        candidates_evaluated += 1

        # Run V47 size sweep + selector
        sel = select_size_for_candidate(
            latest_curve_state=cs,
            pending_buys=pending_buys,
            pending_sells=pending_sells,
            sizes_to_try=sizes_to_try,
            our_priority_fee_lamports=0,
            ata_rent_sol=0.0,
            logger=log,
            mint_for_log=mint,
        )
        size_eval_table = sel.get("size_eval_table") or []
        # Update per-size counters
        for row in size_eval_table:
            sz = float(row.get("size_sol", 0.0))
            if sz not in per_size_tested:
                per_size_tested[sz] = 0
                per_size_accepted[sz] = 0
                per_size_counters[sz] = Counter()
            per_size_tested[sz] += 1
            rr = row.get("reject_reason")
            if rr is None:
                per_size_accepted[sz] += 1
            else:
                per_size_counters[sz][rr] += 1

        selected_size = sel.get("selected_size_sol")
        if selected_size is None:
            candidates_no_size_works += 1
            # Emit a lightweight no-pick record (useful for analysis but
            # not a candidate)
            try:
                jsonl_fp.write(json.dumps({
                    "type": "v47_no_pick",
                    "decision_ts_ms": int(ts_ms_now),
                    "mint": mint,
                    "reason": sel.get("reason", ""),
                    "size_eval_table": [
                        {"size": r["size_sol"], "reject": r["reject_reason"],
                         "stress": r["stress_pnl"],
                         "all_in": r["all_in_pnl"],
                         "impact_bps": r["impact_bps"]}
                        for r in size_eval_table
                    ],
                }) + "\n")
                jsonl_fp.flush()
            except Exception:
                pass
            return

        candidates_with_selected_size += 1
        selected_size_counter[float(selected_size)] += 1
        seen_pass_mints.add(mint)
        sel_ev = sel.get("selected_evaluation") or {}

        # Per-trade required profit (size-normalized) for outcome label.
        req_profit = float(sel_ev.get("required_profit_sol", 0.0))
        clamp_th_sol = float(CLAMP_REL) * float(selected_size)  # negative
        # Pin clamp at <0 strictly so any negative live close counts as loss.
        # Use the maximum of CLAMP_REL*size and 0 to ensure -inf clamp on
        # zero-loss-required.
        clamp_th_sol = max(clamp_th_sol, -float(selected_size))

        rec = {
            "type": "v47_candidate",
            "decision_ts_ms": int(ts_ms_now),
            "mint": mint,
            "selected_size_sol": float(selected_size),
            "expected_pnl": float(sel.get("expected_pnl", 0.0)),
            "stress_pnl": float(sel.get("stress_pnl", 0.0)),
            "required_profit_sol": float(req_profit),
            "selection_reason": sel.get("reason", ""),
            "slot_at_decision": int(slot),
            "sol_in_at_decision": float(sol_in),
            "signer_at_decision": signer,
            "source_lead_ms": float(source_lead_ms),
            "raw_buy_lead_ms_latest": float(
                snap.get("raw_buy_lead_ms_latest", 0.0)
            ),
            "pending_buy_count_250ms": int(
                snap.get("pending_buy_count_250ms", 0)
            ),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)
            ),
            "pending_sell_count_250ms": int(
                snap.get("pending_sell_count_250ms", 0)
            ),
            "pending_sell_sol_250ms": float(
                snap.get("pending_sell_sol_250ms", 0.0)
            ),
            "largest_pending_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)
            ),
            "unique_buyers_250ms": int(
                snap.get("unique_buyers_250ms", 0)
            ),
            "net_pending_sol_250ms": float(
                snap.get("net_pending_sol_250ms", 0.0)
            ),
            "reflected_in_curve": bool(snap.get("reflected_in_curve", False)),
            "raw_buy_visible_before_curve_update": bool(
                raw_buy_visible_before_curve_update
            ),
            "projected_sell_out_sol": float(sel_ev.get("projected_sell_out_sol", 0.0)),
            "stress_sell_out_sol": float(sel_ev.get("stress_sell_out_sol", 0.0)),
            "edge_bps": float(sel_ev.get("edge_bps", 0.0)),
            "self_impact_bps": float(sel_ev.get("self_impact_bps", 0.0)),
            "fee_drag_bps": float(sel_ev.get("fee_drag_bps", 0.0)),
            "guards_encodable": bool(sel_ev.get("guards_encodable", False)),
            "min_token_buy_guard": int(sel_ev.get("min_token_buy_guard", 0)),
            "min_sol_sell_guard": int(sel_ev.get("min_sol_sell_guard", 0)),
            "buy_tokens_raw": int(sel_ev.get("buy_tokens_raw", 0)),
            "decision_curve_state": [
                int(cs.virtual_sol_reserves),
                int(cs.virtual_token_reserves),
                int(cs.real_token_reserves),
            ],
            "decision_curve_update_ts_ms": int(cu_ts),
            # Compressed eval table (one row per size)
            "size_eval_table": [
                {
                    "size": float(r["size_sol"]),
                    "stress": float(r["stress_pnl"]),
                    "all_in": float(r["all_in_pnl"]),
                    "impact_bps": float(r["impact_bps"]),
                    "reject": r["reject_reason"],
                    "meets_req": bool(r["meets_required_profit"]),
                    "meets_zero": bool(r["meets_zero_loss_stress"]),
                }
                for r in size_eval_table
            ],
            # Outcome (filled in by labeler)
            "observed_pnl": None,
            "observed_kind": None,
            "observed_lag_ms": None,
            "future_snaps_used_count": 0,
            "clamp_threshold_sol": float(clamp_th_sol),
        }
        candidates.append(rec)
        pending_candidates[(mint, int(ts_ms_now))] = rec
        jsonl_fp.write(json.dumps(rec) + "\n")
        jsonl_fp.flush()
        log(
            f"V47-CANDIDATE mint={_short(mint)} "
            f"size={rec['selected_size_sol']:.4f} "
            f"exp={rec['expected_pnl']:+.6f} "
            f"stress={rec['stress_pnl']:+.6f} "
            f"req={rec['required_profit_sol']:.6f} "
            f"impact_bps={rec['self_impact_bps']:+.1f} "
            f"lead={rec['source_lead_ms']:+.0f} "
            f"pb250={rec['pending_buy_count_250ms']} "
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
                        continue
                    buffer_.mark_curve_update(mint, int(p.ts_ms))
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
                        tok_at_dec = int(rec.get("buy_tokens_raw", 0))
                        if tok_at_dec <= 0:
                            sell_lams_now = 0
                        else:
                            sell_lams_now, _ = local_sell_quote_sol(
                                cs_now, int(tok_at_dec)
                            )
                        sell_sol_now = float(sell_lams_now) / float(LAMPORTS_PER_SOL)
                        size_sol_used = float(rec.get("selected_size_sol", 0.0))
                        pnl_now = (
                            sell_sol_now
                            - size_sol_used
                            - 2.0 * float(DEFAULT_TX_FEE_SOL)
                        )
                        lag = int(p.ts_ms) - int(dts)
                        rec["future_snaps_used_count"] = (
                            int(rec.get("future_snaps_used_count", 0)) + 1
                        )
                        req_profit_rec = float(rec.get("required_profit_sol", 0.0))
                        clamp_th = float(rec.get("clamp_threshold_sol", -size_sol_used))
                        # Bank when pnl >= required_profit (size-normalized)
                        if pnl_now >= req_profit_rec:
                            rec["observed_pnl"] = float(pnl_now)
                            rec["observed_kind"] = "bank"
                            rec["observed_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "decision_ts_ms",
                                        "selected_size_sol",
                                        "expected_pnl", "stress_pnl",
                                        "observed_pnl",
                                        "observed_kind",
                                        "observed_lag_ms",
                                        "future_snaps_used_count",
                                    )
                                }, "ts_ms": int(p.ts_ms)}) + "\n"
                            )
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            log(
                                f"V47-OBSERVED bank pnl={pnl_now:+.6f} "
                                f"size={size_sol_used:.4f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )
                            continue
                        # Clamp loss when pnl <= clamp (or strict negative
                        # if clamp_th >= 0)
                        if pnl_now <= clamp_th or pnl_now < 0:
                            rec["observed_pnl"] = float(pnl_now)
                            rec["observed_kind"] = "clamp_loss"
                            rec["observed_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "decision_ts_ms",
                                        "selected_size_sol",
                                        "expected_pnl", "stress_pnl",
                                        "observed_pnl",
                                        "observed_kind",
                                        "observed_lag_ms",
                                        "future_snaps_used_count",
                                    )
                                }, "ts_ms": int(p.ts_ms)}) + "\n"
                            )
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            log(
                                f"V47-OBSERVED clamp_loss pnl={pnl_now:+.6f} "
                                f"size={size_sol_used:.4f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )
                            continue
                        hold_ms = MAX_HOLD
                        if EXTEND_IF_POS and pnl_now > 0:
                            hold_ms = MAX_EXTEND_MS
                        if lag >= hold_ms:
                            if pnl_now > 0 and pnl_now < req_profit_rec:
                                if pnl_now < SCRATCH_TH:
                                    label_kind = "scratch"
                                else:
                                    label_kind = "neutral"
                            elif pnl_now == 0:
                                label_kind = "scratch"
                            elif pnl_now > 0:
                                label_kind = "neutral"
                            else:
                                label_kind = "expired_loss"
                            rec["observed_pnl"] = float(pnl_now)
                            rec["observed_kind"] = label_kind
                            rec["observed_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "decision_ts_ms",
                                        "selected_size_sol",
                                        "expected_pnl", "stress_pnl",
                                        "observed_pnl",
                                        "observed_kind",
                                        "observed_lag_ms",
                                        "future_snaps_used_count",
                                    )
                                }, "ts_ms": int(p.ts_ms)}) + "\n"
                            )
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            log(
                                f"V47-OBSERVED {label_kind} pnl={pnl_now:+.6f} "
                                f"size={size_sol_used:.4f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )

            if now_ts >= next_progress_ms:
                log(
                    f"V47-NO-SEND progress elapsed_s="
                    f"{(now_ts - t_start_wall)/1000.0:.0f} "
                    f"buys={raw_buys_seen} sells={raw_sells_seen} "
                    f"curve_updates={curve_updates_seen} "
                    f"evaluated={candidates_evaluated} "
                    f"selected={candidates_with_selected_size} "
                    f"no_size={candidates_no_size_works} "
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
            if rec.get("observed_kind") is None:
                rec["observed_kind"] = "pending"
                rec["observed_pnl"] = None
                rec["observed_lag_ms"] = None
                jsonl_fp.write(
                    json.dumps({"type": "v47_observed", **{
                        k: rec.get(k) for k in (
                            "mint", "decision_ts_ms",
                            "selected_size_sol",
                            "expected_pnl", "stress_pnl",
                            "observed_pnl",
                            "observed_kind",
                            "observed_lag_ms",
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
        k = rec.get("observed_kind") or "pending"
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

    # Causality: every selected candidate must have stress_all_in_pnl >= 0
    all_stress_nonneg = all(
        float(r.get("stress_pnl", -1.0)) >= 0.0 for r in candidates
    )

    amount_min = min(sizes_to_try) if sizes_to_try else 0.0
    amount_max = max(sizes_to_try) if sizes_to_try else 0.0
    total_candidates = max(1, len(candidates))

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V47 — Adaptive Micro-Size No-Send Report (Phase 3)\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- amount_sol_min: {amount_min}\n")
        f.write(f"- amount_sol_max: {amount_max}\n")
        f.write(
            f"- sizes_tested: {', '.join(f'{s:.4f}' for s in sizes_to_try)}\n"
        )
        f.write(f"- runtime_s: {elapsed_s:.1f}\n")
        f.write(f"- rules_path: {args.rules_json}\n")
        f.write(f"- max_hold_ms: {MAX_HOLD}\n")
        f.write(f"- extend_hold_ms: {MAX_EXTEND_MS}\n\n")
        f.write("## Engine sanity\n\n")
        f.write(f"- raw_pump_buys_seen: {raw_buys_seen}\n")
        f.write(f"- raw_pump_sells_seen: {raw_sells_seen}\n")
        f.write(f"- curve_updates: {curve_updates_seen}\n")
        f.write(f"- snapshots: {snapshots_total}\n")
        f.write(f"- lookahead_blocks: {lookahead_block_count}\n")
        f.write(f"- hot_mints_terminal: {len(hot_mint_last_seen)}\n\n")
        f.write("## Size selector summary\n\n")
        f.write(f"- candidates_evaluated: {candidates_evaluated}\n")
        f.write(f"- candidates_with_selected_size: {candidates_with_selected_size}\n")
        f.write(f"- candidates_no_size_works: {candidates_no_size_works}\n\n")
        f.write("## Per-size blocker table\n\n")
        f.write(
            "| size_sol | tested | stress_negative | self_impact_too_high | "
            "guards_not_encodable | below_required_profit | accepted |\n"
        )
        f.write(
            "|----------|--------|-----------------|----------------------|"
            "----------------------|-----------------------|----------|\n"
        )
        for sz in sorted(per_size_tested.keys()):
            ctr = per_size_counters.get(sz, Counter())
            f.write(
                f"| {sz:.4f} | {per_size_tested[sz]} | "
                f"{ctr.get('stress_negative', 0)} | "
                f"{ctr.get('self_impact_too_high', 0)} | "
                f"{ctr.get('guards_not_encodable', 0)} | "
                f"{ctr.get('below_required_profit', 0)} | "
                f"{per_size_accepted.get(sz, 0)} |\n"
            )
        # Top reasons across sizes (aggregate)
        global_reasons: Counter = Counter()
        for ctr in per_size_counters.values():
            global_reasons.update(ctr)
        f.write("\n## Top reasons across sizes (aggregate)\n\n")
        for reason, cnt in global_reasons.most_common(10):
            f.write(f"- {reason}: {cnt}\n")
        if not global_reasons:
            f.write("- (none)\n")

        f.write("\n## Size-admission delta (smaller-sizes admit more?)\n\n")
        f.write(
            "Whether 0.001 SOL admits candidates that 0.015 SOL rejects:\n"
        )
        small = float(min(sizes_to_try)) if sizes_to_try else 0.0
        big = float(max(sizes_to_try)) if sizes_to_try else 0.0
        f.write(
            f"- accepted_at_size_{small:.4f}: {per_size_accepted.get(small, 0)}\n"
        )
        f.write(
            f"- accepted_at_size_{big:.4f}: {per_size_accepted.get(big, 0)}\n"
        )

        f.write("\n## V47 candidate entries\n\n")
        f.write(f"- candidates_count: {len(candidates)}\n")
        f.write(f"- unique_mints: {len(seen_pass_mints)}\n\n")
        f.write("## Causal observed outcomes\n\n")
        f.write(f"- bank: {bank_count}\n")
        f.write(f"- scratch: {scratch_count}\n")
        f.write(f"- clamp_loss: {clamp_count}\n")
        f.write(f"- expired_loss: {expired_loss_count}\n")
        f.write(f"- neutral: {neutral_count}\n")
        f.write(f"- pending: {pending_count}\n\n")
        f.write("## Selected size distribution\n\n")
        if selected_size_counter:
            for sz in sorted(selected_size_counter.keys()):
                cnt = selected_size_counter[sz]
                pct = 100.0 * cnt / max(1, sum(selected_size_counter.values()))
                f.write(f"- {sz:.4f} SOL: {cnt} ({pct:.1f}%)\n")
        else:
            f.write("- (no candidates selected)\n")
        f.write("\n## Verdict\n\n")
        meets_target = len(candidates) >= int(args.target_pass)
        zero_neg = (clamp_count == 0 and expired_loss_count == 0)
        zero_la = (lookahead_block_count == 0)
        overall = bool(
            meets_target and zero_neg and zero_la and all_stress_nonneg
        )
        f.write(f"- meets_target_count: {meets_target} ({len(candidates)}/{args.target_pass})\n")
        f.write(f"- zero_observed_negative_outcomes: {zero_neg}\n")
        f.write(f"- zero_lookahead_violations: {zero_la}\n")
        f.write(f"- all_selected_stress_nonneg: {all_stress_nonneg}\n")
        f.write(f"- OVERALL_VERDICT: {'PASS' if overall else 'FAIL'}\n\n")
        f.write("## Per-candidate detail\n\n")
        if candidates:
            f.write(
                "| # | mint | size | exp_pnl | stress_pnl | req | "
                "impact_bps | lead_ms | observed_kind | observed_pnl | "
                "observed_lag_ms |\n"
            )
            f.write(
                "|---|------|------|---------|------------|------|"
                "------------|---------|----------------|---------------|"
                "------------------|\n"
            )
            for i, r in enumerate(candidates, 1):
                f.write(
                    f"| {i} | {_short(r.get('mint',''))} | "
                    f"{float(r.get('selected_size_sol',0.0)):.4f} | "
                    f"{float(r.get('expected_pnl',0.0)):+.6f} | "
                    f"{float(r.get('stress_pnl',0.0)):+.6f} | "
                    f"{float(r.get('required_profit_sol',0.0)):.6f} | "
                    f"{float(r.get('self_impact_bps',0.0)):+.1f} | "
                    f"{float(r.get('source_lead_ms',0.0)):+.0f} | "
                    f"{r.get('observed_kind','pending') or 'pending'} | "
                    f"{('%+.6f' % float(r.get('observed_pnl') or 0.0)) if r.get('observed_pnl') is not None else 'n/a'} | "
                    f"{r.get('observed_lag_ms','') if r.get('observed_lag_ms') is not None else ''} |\n"
                )
        else:
            f.write("- (none)\n")
    log(f"V47-NO-SEND wrote {md_path}")
    log(
        f"V47-NO-SEND done elapsed_s={elapsed_s:.1f} "
        f"candidates={len(candidates)} "
        f"banks={bank_count} losses={clamp_count + expired_loss_count} "
        f"pending={pending_count}"
    )
    return 0 if (
        len(candidates) >= args.target_pass
        and clamp_count == 0
        and expired_loss_count == 0
        and lookahead_block_count == 0
        and all_stress_nonneg
    ) else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
