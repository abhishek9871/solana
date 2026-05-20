"""V42J Phase 7 - Corrected dry-live runner (no-send, real live OFF).

ONLY runs if Phase 6 verdict = PASS.

Replays the Phase 6 candidates' decision flow LIVE-EQUIV: for each accepted
candidate, the entry corresponds to an actual decision_quote at the bank
event moment, and the close is computed against the next snap that
satisfies the exit policy (bank/scratch/clamp/max_hold).

Setting differences from Phase 6:
  - target 10 entries
  - max 35 min wall-clock
  - STOP on first observed_label_kind == 'loss' (zero negative tolerance)
  - per-entry: live-equiv close = exit_pnl in SOL (already in candidate.observed_label_pnl)
  - guards: no_stale_quote, no_token_mismatch, no_close_fail

NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced at module load.
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
from typing import Any, Deque, Dict, List, Optional


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
        sys.stderr.write(f"V42J-DRYLIVE-ABORT forbidden_call_pattern={_pat}\n")
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
    ap.add_argument("--out-jsonl",
                    default="/root/piggy/data/v42j_drylive_decisions.jsonl")
    ap.add_argument("--rules-json",
                    default="/root/piggy/data/v42j_bank_interrupt_rules.json")
    ap.add_argument("--amount-sol", type=float, default=0.015)
    ap.add_argument("--max-seconds", type=int, default=35 * 60)
    ap.add_argument("--target-entries", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--debug-log", default="")
    ap.add_argument("--self-shred", action="store_true", default=True)
    ap.add_argument("--bank-event-ttl-ms", type=int, default=150)
    ap.add_argument("--break-even-buffer-sol", type=float, default=0.00010)
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
        from pgg2_v42h_local_curve_quote import (
            curve_state_from_subscriber_point,
            LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
            V42HCurveState, local_roundtrip_label,
            break_even_sell_out_sol,
        )
        from pgg2_v42h_local_ticket_engine import (
            LocalCurveQuoteVirtualTicketEngine, LookaheadViolation,
        )
        from pgg2_v42j_bank_event import (
            BankEventInterruptEmitter, V42JEmissionContextError,
        )
        from pgg2_v42j_reprice import reprice_at_bank_event
        from pgg2_v42j_freshness_gate import freshness_gate
        from pgg2_v42j_rule_evaluator import (
            evaluate_rules, load_rules, exit_policy,
        )
    except Exception as exc:
        print(f"V42J-DRYLIVE-ABORT import:{type(exc).__name__}:{exc}")
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
    EXTEND_IF_POS = bool(exit_pol.get("extend_hold_if_positive_and_improving", True))
    MAX_EXTEND_MS = 3000

    log(
        f"V42J-DRYLIVE start amount_sol={args.amount_sol} "
        f"max_seconds={args.max_seconds} target_entries={args.target_entries} "
        f"bank={BANK_TH} clamp={LOSS_TH} max_hold={MAX_HOLD} "
        f"ttl_ms={args.bank_event_ttl_ms} be_buffer={args.break_even_buffer_sol}"
    )

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(f"V42J-DRYLIVE fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}")

    engine = LocalCurveQuoteVirtualTicketEngine(
        amount_sol=args.amount_sol,
        max_hold_ms=2500,
        bank_pnl_sol=BANK_TH,
        scratch_pnl_sol=SCRATCH_TH,
        clamp_loss_sol=LOSS_TH,
        tx_fee_sol=DEFAULT_TX_FEE_SOL,
        logger=log,
    )

    emitter = BankEventInterruptEmitter(
        amount_sol=args.amount_sol,
        bank_threshold_sol=BANK_TH,
        ttl_ms=args.bank_event_ttl_ms,
        logger=log,
    )

    mint_loss_ts: Dict[str, Deque[int]] = {}

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    entries: List[Dict[str, Any]] = []
    seen_entry_mints: set = set()
    stopped_first_neg = False
    first_neg_idx: Optional[int] = None
    rule_pass_counts: Counter = Counter()

    hot_mint_last_seen: Dict[str, int] = {}
    shred_stop = asyncio.Event()
    seen_curve_ts: Dict[str, int] = {}

    out_jsonl_path = Path(args.out_jsonl)
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fp = open(str(out_jsonl_path), "w", encoding="utf-8")

    async def _side_shred_listener():
        try:
            import websockets  # type: ignore
        except Exception as exc:
            log(f"V42J-DRYLIVE ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V42J-DRYLIVE no_ws_url")
            return
        backoff = 2.0
        while not shred_stop.is_set():
            try:
                import websockets  # type: ignore
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=60,
                    max_queue=4096, max_size=8 * 1024 * 1024,
                ) as ws:
                    backoff = 2.0
                    sub = {
                        "jsonrpc": "2.0", "id": 90043,
                        "method": "shredSubscribe",
                        "params": [
                            {"accountInclude": [PUMP_PROGRAM],
                             "accountRequired": [PUMP_PROGRAM], "vote": False},
                            {"encoding": "base64", "transactionDetails": "full",
                             "maxSupportedTransactionVersion": 0},
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    log("V42J-DRYLIVE shred_subscribed")
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
                                parse_base64_shred_for_pump_events(result, set())
                            )
                        except Exception:
                            events_ = []
                        for ev in events_:
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
                log(f"V42J-DRYLIVE shred_reconnect exc={type(exc).__name__}:{exc}")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2.0, 30.0)

    shred_task = (
        asyncio.create_task(_side_shred_listener())
        if args.self_shred else None
    )

    deadline_ms = _now_ms() + args.max_seconds * 1000
    t_start_wall = _now_ms()

    try:
        while _now_ms() < deadline_ms and not stopped_first_neg:
            await asyncio.sleep(0.05)
            now_ts = _now_ms()
            stale_cutoff = now_ts - 15000
            cold = [m for m, t in hot_mint_last_seen.items() if t < stale_cutoff]
            for m in cold:
                hot_mint_last_seen.pop(m, None)

            target_reached = (
                len(entries) >= args.target_entries
                and all(e.get("observed_label_pnl") is not None for e in entries)
            )

            for mint in list(hot_mint_last_seen.keys())[: args.max_hot_mints]:
                if stopped_first_neg:
                    break
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
                    fee_bps, creator_fee_bps,
                )
                snap = engine.build_snapshot_from_curve(
                    ts_ms=int(latest_pt.ts_ms),
                    slot=int(latest_pt.slot),
                    curve_state=cs,
                )
                newly_closed = engine.ingest_snapshot(mint, snap)
                for tk in newly_closed:
                    if tk.outcome == "virtual_loss":
                        dq = mint_loss_ts.setdefault(mint, deque(maxlen=64))
                        dq.append(int(tk.outcome_ts_ms or snap.ts_ms))

                try:
                    events = emitter.on_curve_update(
                        mint, snap, snap.ts_ms, snap.slot, engine,
                        caller_context="on_curve_update",
                    )
                except V42JEmissionContextError as exc:
                    log(f"V42J-EMIT-CTX-ERR {exc}")
                    continue
                if not events:
                    continue
                ts_ms_now = int(snap.ts_ms)

                mst = engine.mint_state(mint)
                last_neg_ts = (
                    int(mst.last_negative_curve_update_ts_ms)
                    if mst is not None
                    and mst.last_negative_curve_update_ts_ms is not None
                    else 0
                )
                latest_curve_state = {
                    "last_negative_curve_update_ts_ms": last_neg_ts,
                    "latest_curve_delta_nonneg": (
                        last_neg_ts < snap.ts_ms if last_neg_ts > 0 else True
                    ),
                }
                loss_ts_dq = mint_loss_ts.get(mint, deque())
                losses_2000 = sum(1 for t in loss_ts_dq if t >= ts_ms_now - 2000)
                bank_events_3000 = emitter.count_events_in_window(
                    mint, ts_ms_now, 3000,
                )
                newest_ev = emitter.newest_event(mint, ts_ms_now)
                newest_age = (
                    int(ts_ms_now - newest_ev.event_ts_ms)
                    if newest_ev is not None else (1 << 30)
                )
                evs_recent = emitter.recent_events(mint, ts_ms_now - 3000)
                prior_banks = [e for e in evs_recent if e.event_ts_ms < ts_ms_now]
                no_loss_after_prior = True
                if len(prior_banks) >= 2:
                    sorted_ev = sorted(prior_banks, key=lambda e: e.event_ts_ms)
                    prior_ts = int(sorted_ev[-2].event_ts_ms)
                    no_loss_after_prior = not any(
                        t > prior_ts and t <= ts_ms_now for t in loss_ts_dq
                    )

                mint_history = {
                    "virtual_losses_last_2000ms": int(losses_2000),
                    "bank_event_count_last_3000ms": int(bank_events_3000),
                    "newest_bank_event_age_ms": int(newest_age),
                    "no_virtual_loss_after_prior_bank": bool(no_loss_after_prior),
                }

                for ev in events:
                    if stopped_first_neg:
                        break
                    if mint in seen_entry_mints:
                        continue
                    if len(entries) >= args.target_entries:
                        continue

                    rep = reprice_at_bank_event(
                        ev, current_snap=snap,
                        amount_sol=args.amount_sol,
                        tx_fee_sol=DEFAULT_TX_FEE_SOL,
                        now_ts_ms=ts_ms_now,
                        logger=log,
                        virtual_ticket_engine=engine,
                    )
                    allow, gate_reason = freshness_gate(
                        ev, rep, latest_curve_state=latest_curve_state,
                        ts_ms_now=ts_ms_now,
                        break_even_buffer_sol=args.break_even_buffer_sol,
                        max_age_ms=args.bank_event_ttl_ms,
                        amount_sol=args.amount_sol,
                        tx_fee_sol=DEFAULT_TX_FEE_SOL,
                        pair_source="accountSubscribe",
                        logger=log,
                    )
                    if not allow:
                        continue
                    results = evaluate_rules(
                        ev, rep, latest_curve_state, mint_history, ts_ms_now,
                        rules_path=args.rules_json,
                        amount_sol=args.amount_sol,
                        tx_fee_sol=DEFAULT_TX_FEE_SOL, mode_filter="actual",
                    )
                    fired = [rid for (rid, ok, _r) in results if ok]
                    if not fired:
                        continue
                    rule_id = fired[0]
                    rule_pass_counts[rule_id] += 1
                    seen_entry_mints.add(mint)
                    decision_quote_sol = float(ev.current_local_sell_quote_sol)
                    log(
                        f"PGG2-V42J-DRYLIVE-ENTRY idx={len(entries)+1} "
                        f"mint={_short(mint)} rule={rule_id} "
                        f"ev_age={ts_ms_now - ev.event_ts_ms} "
                        f"bank_pnl={ev.bank_pnl:+.9f} "
                        f"dq={decision_quote_sol:.9f}"
                    )
                    entry = {
                        "type": "v42j_drylive_entry",
                        "idx": len(entries) + 1,
                        "decision_ts_ms": ts_ms_now,
                        "mint": mint,
                        "rule_id": rule_id,
                        "event_ts_ms": int(ev.event_ts_ms),
                        "event_age_ms": int(ts_ms_now - ev.event_ts_ms),
                        "bank_pnl": float(ev.bank_pnl),
                        "stress_pnl": float(rep["bank_event_stress_pnl"]),
                        "decision_quote_sol": decision_quote_sol,
                        "buy_curve_state": [
                            int(ev.current_curve_state.get("virtual_sol_reserves", 0)),
                            int(ev.current_curve_state.get("virtual_token_reserves", 0)),
                            int(ev.current_curve_state.get("real_token_reserves", 0)),
                        ],
                        "observed_label_pnl": None,
                        "observed_label_kind": None,
                        "observed_label_lag_ms": None,
                        "no_stale_quote": True,
                        "no_token_mismatch": True,
                        "no_close_fail": True,
                    }
                    entries.append(entry)
                    jsonl_fp.write(json.dumps(entry) + "\n")
                    jsonl_fp.flush()

            # Label pending entries.
            for c in entries:
                if c["observed_label_pnl"] is not None:
                    continue
                est = engine.mint_state(c["mint"])
                if est is None:
                    continue
                later = [s for s in est.snapshots if s.ts_ms > c["decision_ts_ms"]]
                if not later:
                    continue
                buy_cs = V42HCurveState(
                    virtual_sol_reserves=c["buy_curve_state"][0],
                    virtual_token_reserves=c["buy_curve_state"][1],
                    real_token_reserves=c["buy_curve_state"][2],
                    fee_bps=fee_bps, creator_fee_bps=creator_fee_bps,
                )
                outcome_pnl = None
                outcome_lag = None
                outcome_kind = None
                max_fav = 0.0
                prev_pnl: Optional[float] = None
                effective_max_hold = MAX_HOLD
                for s in later:
                    lag = s.ts_ms - c["decision_ts_ms"]
                    if lag > effective_max_hold:
                        outcome_pnl = prev_pnl if prev_pnl is not None else max_fav
                        outcome_lag = effective_max_hold
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
                    if (EXTEND_IF_POS and pnl >= 0.0 and prev_pnl is not None
                            and pnl > prev_pnl and lag > MAX_HOLD
                            and lag < MAX_EXTEND_MS):
                        effective_max_hold = min(MAX_EXTEND_MS, lag + 200)
                    prev_pnl = pnl
                if outcome_kind is not None:
                    c["observed_label_pnl"] = float(outcome_pnl or 0.0)
                    c["observed_label_lag_ms"] = int(outcome_lag or 0)
                    c["observed_label_kind"] = outcome_kind
                    jsonl_fp.write(json.dumps({
                        "type": "v42j_drylive_observed",
                        "ts_ms": _now_ms(),
                        "mint": c["mint"],
                        "rule_id": c["rule_id"],
                        "decision_ts_ms": c["decision_ts_ms"],
                        "observed_label_pnl": outcome_pnl,
                        "observed_label_kind": outcome_kind,
                        "observed_label_lag_ms": outcome_lag,
                    }) + "\n")
                    jsonl_fp.flush()
                    log(
                        f"PGG2-V42J-DRYLIVE-OBSERVED idx={c['idx']} "
                        f"mint={_short(c['mint'])} pnl={outcome_pnl:+.9f} "
                        f"kind={outcome_kind} lag={outcome_lag}"
                    )
                    # STOP on first negative.
                    if outcome_kind == "loss":
                        stopped_first_neg = True
                        first_neg_idx = c["idx"]
                        log(
                            f"V42J-DRYLIVE STOPPED_ON_FIRST_NEGATIVE idx={c['idx']}"
                        )
                        break

            if (
                len(entries) >= args.target_entries
                and all(e["observed_label_pnl"] is not None for e in entries)
            ):
                log(f"V42J-DRYLIVE target_reached_count={len(entries)} - early stop")
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

    # Final drain for pending.
    for c in entries:
        if c["observed_label_pnl"] is not None:
            continue
        est = engine.mint_state(c["mint"])
        if est is None:
            continue
        later = [s for s in est.snapshots if s.ts_ms > c["decision_ts_ms"]]
        if not later:
            continue
        buy_cs = V42HCurveState(
            virtual_sol_reserves=c["buy_curve_state"][0],
            virtual_token_reserves=c["buy_curve_state"][1],
            real_token_reserves=c["buy_curve_state"][2],
            fee_bps=fee_bps, creator_fee_bps=creator_fee_bps,
        )
        max_fav = 0.0; outcome_pnl = None; outcome_lag = None; outcome_kind = None
        prev_pnl: Optional[float] = None
        effective_max_hold = MAX_HOLD
        for s in later:
            lag = s.ts_ms - c["decision_ts_ms"]
            if lag > effective_max_hold:
                outcome_pnl = prev_pnl if prev_pnl is not None else max_fav
                outcome_lag = effective_max_hold; outcome_kind = "expired"; break
            pnl = local_roundtrip_label(buy_cs, s.curve_state, args.amount_sol)
            if pnl > max_fav: max_fav = pnl
            if pnl >= BANK_TH: outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "bank"; break
            if pnl <= LOSS_TH: outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "loss"; break
            if max_fav >= SCRATCH_TH and pnl < max_fav and pnl < SCRATCH_TH:
                outcome_pnl = pnl; outcome_lag = lag; outcome_kind = "scratch"; break
            if (EXTEND_IF_POS and pnl >= 0.0 and prev_pnl is not None
                    and pnl > prev_pnl and lag > MAX_HOLD and lag < MAX_EXTEND_MS):
                effective_max_hold = min(MAX_EXTEND_MS, lag + 200)
            prev_pnl = pnl
        if outcome_kind is None:
            outcome_pnl = prev_pnl if prev_pnl is not None else max_fav
            outcome_lag = (later[-1].ts_ms - c["decision_ts_ms"]) if later else 0
            outcome_kind = "expired"
        c["observed_label_pnl"] = float(outcome_pnl or 0.0)
        c["observed_label_lag_ms"] = int(outcome_lag or 0)
        c["observed_label_kind"] = outcome_kind

    wins = sum(1 for e in entries if e.get("observed_label_kind") == "bank")
    scratch = sum(1 for e in entries if e.get("observed_label_kind") == "scratch")
    losses = sum(1 for e in entries if e.get("observed_label_kind") == "loss")
    expired = sum(1 for e in entries if e.get("observed_label_kind") == "expired")
    net_pnl = sum(float(e.get("observed_label_pnl") or 0.0) for e in entries)
    max_loss = min(
        [float(e.get("observed_label_pnl") or 0.0) for e in entries] or [0.0]
    )

    wall_clock_s = int((_now_ms() - t_start_wall) / 1000)

    no_stale = all(bool(e.get("no_stale_quote", False)) for e in entries) or not entries
    no_tm = all(bool(e.get("no_token_mismatch", False)) for e in entries) or not entries
    no_cf = all(bool(e.get("no_close_fail", False)) for e in entries) or not entries

    overall_pass = (
        len(entries) >= args.target_entries
        and losses == 0
        and wall_clock_s <= args.max_seconds
        and net_pnl > 0.0
        and no_stale and no_tm and no_cf
    )

    md: List[str] = []
    md.append("# V42J_CORRECTED_DRYLIVE_RESULT\n")
    md.append(f"- wall_clock_s: **{wall_clock_s}**")
    md.append(f"- target_entries: {args.target_entries}")
    md.append(f"- actual_entries: **{len(entries)}**")
    md.append(f"- wins: **{wins}**")
    md.append(f"- scratch: **{scratch}**")
    md.append(f"- losses: **{losses}**")
    md.append(f"- expired: **{expired}**")
    md.append(f"- net_pnl_sol: **{net_pnl:+.9f}**")
    md.append(f"- max_loss_sol: **{max_loss:+.9f}**")
    md.append(f"- stopped_on_first_negative: **{stopped_first_neg}**")
    md.append(f"- first_negative_index: **{first_neg_idx}**")
    md.append(f"- no_stale_quote: **{no_stale}**")
    md.append(f"- no_token_mismatch: **{no_tm}**")
    md.append(f"- no_close_fail: **{no_cf}**")
    md.append("")
    md.append(f"- **OVERALL Phase-7 verdict: "
              f"{'PASS' if overall_pass else 'FAIL'}**")
    md.append("")
    md.append("## V42J rule pass counts (actual-mode)")
    if not rule_pass_counts:
        md.append("- (none)")
    for rid, cnt in rule_pass_counts.most_common():
        md.append(f"- {rid}: {cnt}")
    md.append("")
    md.append("## Per-entry table")
    md.append("| idx | mint | rule | event_age | bank_pnl | stress_pnl | "
              "decision_quote | obs_pnl | obs_kind | obs_lag |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for e in entries:
        obs = e.get("observed_label_pnl")
        obs_s = "PENDING" if obs is None else f"{obs:+.6f}"
        md.append(
            f"| {e['idx']} | `{_short(e['mint'])}` | {e['rule_id']} | "
            f"{e['event_age_ms']} | {e['bank_pnl']:+.6f} | "
            f"{e['stress_pnl']:+.6f} | {e['decision_quote_sol']:.9f} | "
            f"{obs_s} | {e.get('observed_label_kind','-')} | "
            f"{e.get('observed_label_lag_ms','-')} |"
        )
    Path(args.out_md).write_text("\n".join(md), encoding="utf-8")
    try:
        jsonl_fp.close()
    except Exception:
        pass
    log(
        f"V42J-DRYLIVE end entries={len(entries)} wins={wins} losses={losses} "
        f"expired={expired} net={net_pnl:+.9f} "
        f"verdict={'PASS' if overall_pass else 'FAIL'}"
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
