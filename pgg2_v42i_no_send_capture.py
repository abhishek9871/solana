"""V42I Phase 5 — Fresh active-ticket no-send capture.

Wires up the V42H account-subscriber + local CPMM quote engine + virtual
ticket engine, the V42I ActiveTicketStateTracker, the V42I rule
evaluator, and the V42I entry-block. For every accountSubscribe curve
update on a tracked mint, V42I evaluates the active-ticket state and
emits an ENTRY CANDIDATE only when:
    1. a V42I rule passes (`pgg2_v42i_rule_evaluator.evaluate_rules`)
    2. `pgg2_v42i_entry_block.should_block_entry` returns block=False
The candidate is then observed forward via the V42I exit policy (bank
+0.00060, scratch +0.00005 with deterioration, clamp -0.00050, max
hold 1500ms with extension if positive+improving) within max_hold_ms.

Settings from env / args:
    --target-pass         (default 10)
    --max-seconds         (default 600 = 10 min)
    --amount-sol          (default 0.015)
    --rules-json          (default /root/piggy/data/v42i_active_ticket_rules.json)
    --debug-log
    PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE=1   (already in env)

Output:
    /root/piggy/data/v42i_no_send_decisions.jsonl   (per-candidate + observed)
    /root/piggy/V42I_NO_SEND_REPORT.md              (report)

Causality: ActiveTicketState is computed from engine ticket history
filtered to outcome_ts <= ts_ms_now. Observed outcome reads ONLY future
snapshots strictly after decision_ts. lookahead_blocks must remain 0.

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
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional


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
        sys.stderr.write(f"V42I-NO-SEND-ABORT forbidden_call_pattern={_pat}\n")
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
        default="/root/piggy/data/v42i_no_send_decisions.jsonl",
    )
    ap.add_argument(
        "--rules-json",
        default="/root/piggy/data/v42i_active_ticket_rules.json",
    )
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
        from pgg2_v42_curve_account_subscriber import (  # type: ignore
            CurveAccountSubscriberOracle,
        )
        from pgg2_direct_pump import DirectPumpQuoteBroker  # type: ignore
        from birth_first_sniper import (  # type: ignore
            BotConfig, parse_base64_shred_for_pump_events,
        )
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
        from pgg2_v42i_active_ticket_state import (
            ActiveTicketStateTracker,
        )
        from pgg2_v42i_rule_evaluator import (
            evaluate_rules, load_rules,
        )
        from pgg2_v42i_entry_block import (
            should_block_entry, format_log_line as fmt_block_log,
        )
    except Exception as exc:
        print(f"V42I-NO-SEND-ABORT import:{type(exc).__name__}:{exc}")
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
    exit_pol = rules_cfg.get("exit_policy", {})
    BANK_TH = float(exit_pol.get("bank_threshold_sol", 0.00060))
    SCRATCH_TH = float(exit_pol.get("scratch_threshold_sol", 0.00005))
    LOSS_TH = float(exit_pol.get("clamp_threshold_sol", -0.00050))
    MAX_HOLD = int(exit_pol.get("max_hold_ms", 1500))
    EXTEND_IF_POS = bool(
        exit_pol.get("extend_hold_if_positive_and_improving", True)
    )
    # Hard cap on extension to prevent runaway holds:
    MAX_EXTEND_MS = 3000

    log(
        f"V42I-NO-SEND start amount_sol={args.amount_sol} "
        f"max_seconds={args.max_seconds} target_pass={args.target_pass} "
        f"rules_path={args.rules_json} bank={BANK_TH} scratch={SCRATCH_TH} "
        f"clamp={LOSS_TH} max_hold={MAX_HOLD} extend_pos={EXTEND_IF_POS}"
    )
    if os.environ.get("PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE", "0") != "1":
        log("V42I-NO-SEND WARNING: PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE != 1")

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(f"V42I-NO-SEND fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}")

    # Engine sized to V42I exit policy.
    engine = LocalCurveQuoteVirtualTicketEngine(
        amount_sol=args.amount_sol,
        max_hold_ms=2500,  # engine internal hold (independent of V42I exit)
        bank_pnl_sol=BANK_TH,
        scratch_pnl_sol=SCRATCH_TH,
        clamp_loss_sol=LOSS_TH,
        tx_fee_sol=DEFAULT_TX_FEE_SOL,
        logger=log,
    )

    tracker = ActiveTicketStateTracker(engine=engine, logger=log)

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    candidates: List[Dict[str, Any]] = []
    rule_pass_counts: Counter = Counter()
    block_counts: Counter = Counter()
    seen_pass_mints: set = set()
    state_updates_total = 0
    last_state_emit_per_mint: Dict[str, int] = {}

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
            log(f"V42I-NO-SEND ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V42I-NO-SEND no_ws_url")
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
                        "jsonrpc": "2.0", "id": 90042,
                        "method": "shredSubscribe",
                        "params": [
                            {"accountInclude": [PUMP_PROGRAM],
                             "accountRequired": [PUMP_PROGRAM], "vote": False},
                            {"encoding": "base64",
                             "transactionDetails": "full",
                             "maxSupportedTransactionVersion": 0},
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    log("V42I-NO-SEND shred_subscribed")
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
                            events = list(
                                parse_base64_shred_for_pump_events(
                                    result, set()
                                )
                            )
                        except Exception:
                            events = []
                        for ev in events:
                            m = getattr(ev, "mint", "") or ""
                            if not m:
                                continue
                            ts_ms = int(
                                getattr(ev, "ts_ms", _now_ms()) or _now_ms()
                            )
                            hot_mint_last_seen[m] = ts_ms
                            oracle.mark_feed_event(m, ts_ms)
                            if len(hot_mint_last_seen) <= args.max_hot_mints:
                                oracle.request_subscription(m)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(f"V42I-NO-SEND shred_reconnect "
                    f"exc={type(exc).__name__}:{exc}")
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
        while _now_ms() < deadline_ms:
            await asyncio.sleep(0.05)
            now_ts = _now_ms()
            stale_cutoff = now_ts - 15000
            cold = [m for m, t in hot_mint_last_seen.items()
                    if t < stale_cutoff]
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

                # Update active-ticket state.
                active_state = tracker.ingest_curve_update(
                    mint, snap, snap.ts_ms,
                )
                state_updates_total += 1

                if mint in seen_pass_mints:
                    continue
                if target_reached:
                    continue

                # Apply V42I rules.
                rule_results = evaluate_rules(
                    active_state,
                    rules_path=args.rules_json,
                    route="pump_bc",
                    sim_needed=0,
                )
                fired_rules = [
                    rid for (rid, ok, _r) in rule_results if ok
                ]
                if not fired_rules:
                    # Track top block reasons via the no-pass results.
                    for rid, ok, reason in rule_results:
                        if not ok:
                            block_counts[f"rule:{rid}:{reason}"] += 1
                    continue
                rule_id = fired_rules[0]
                rule_pass_counts[rule_id] += 1

                # Apply V42I entry-block.
                block, breason = should_block_entry(active_state, snap.ts_ms)
                log(fmt_block_log(mint, rule_id, block, breason))
                if block:
                    block_counts[f"entry_block:{breason}"] += 1
                    continue

                # ENTRY CANDIDATE.
                seen_pass_mints.add(mint)
                log(
                    f"PGG2-V42I-CANDIDATE-ENTRY mint={_short(mint)} "
                    f"rule={rule_id} ata={active_state['active_ticket_age_ms']} "
                    f"atpnl={active_state['active_ticket_current_pnl']:+.9f}"
                )

                decision_quote_sol = (
                    float(snap.sell_quote_out_lamports) / LAMPORTS_PER_SOL
                )
                # break-even quote = open-snap_sell_sol + 2*tx_fee
                # active ticket's break-even is amount + 2*tx_fee (in SOL).
                be_quote = break_even_sell_out_sol(
                    args.amount_sol, DEFAULT_TX_FEE_SOL, 0.0
                )

                cand = {
                    "type": "v42i_candidate",
                    "decision_ts_ms": snap.ts_ms,
                    "mint": mint,
                    "rule_id": rule_id,
                    "active_ticket_state": {
                        k: active_state.get(k) for k in [
                            "completed_virtual_banks_last_3000ms",
                            "completed_virtual_losses_last_3000ms",
                            "latest_completed_bank_pnl",
                            "latest_completed_bank_time_ms",
                            "active_ticket_id",
                            "active_ticket_age_ms",
                            "active_ticket_current_pnl",
                            "active_ticket_pnl_gradient",
                            "active_ticket_max_adverse",
                            "active_ticket_is_positive",
                            "active_ticket_is_improving",
                            "active_ticket_distance_to_bank",
                            "latest_curve_delta",
                            "latest_local_quote_gradient",
                        ]
                    },
                    "decision_quote_sol": decision_quote_sol,
                    "break_even_quote": be_quote,
                    "buy_curve_state": [
                        snap.curve_state.virtual_sol_reserves,
                        snap.curve_state.virtual_token_reserves,
                        snap.curve_state.real_token_reserves,
                    ],
                    "observed_label_pnl": None,
                    "observed_label_kind": None,
                    "observed_label_lag_ms": None,
                    "future_snaps_used_count": 0,
                }
                candidates.append(cand)
                jsonl_fp.write(json.dumps({
                    "type": "v42i_candidate",
                    **{k: v for k, v in cand.items() if k != "type"},
                }) + "\n")
                jsonl_fp.flush()

            # Update observed outcomes for candidates whose label is pending.
            for c in candidates:
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
                    fee_bps=fee_bps,
                    creator_fee_bps=creator_fee_bps,
                )
                outcome_pnl = None
                outcome_lag = None
                outcome_kind = None
                max_fav = 0.0
                prev_pnl: Optional[float] = None
                snaps_used = 0
                effective_max_hold = MAX_HOLD
                for s in later:
                    snaps_used += 1
                    lag = s.ts_ms - c["decision_ts_ms"]
                    if lag > effective_max_hold:
                        outcome_pnl = (
                            prev_pnl
                            if prev_pnl is not None else max_fav
                        )
                        outcome_lag = effective_max_hold
                        outcome_kind = "expired"
                        break
                    pnl = local_roundtrip_label(
                        buy_cs, s.curve_state, args.amount_sol,
                    )
                    if pnl > max_fav:
                        max_fav = pnl
                    # Bank closes.
                    if pnl >= BANK_TH:
                        outcome_pnl = pnl
                        outcome_lag = lag
                        outcome_kind = "bank"
                        break
                    # Clamp loss closes.
                    if pnl <= LOSS_TH:
                        outcome_pnl = pnl
                        outcome_lag = lag
                        outcome_kind = "loss"
                        break
                    # Scratch closes (favorable high then decline below
                    # scratch threshold).
                    if (
                        max_fav >= SCRATCH_TH
                        and pnl < max_fav
                        and pnl < SCRATCH_TH
                    ):
                        outcome_pnl = pnl
                        outcome_lag = lag
                        outcome_kind = "scratch"
                        break
                    # Extend-hold logic: if positive AND improving, extend
                    # up to MAX_EXTEND_MS.
                    if (
                        EXTEND_IF_POS
                        and pnl >= 0.0
                        and prev_pnl is not None
                        and pnl > prev_pnl
                        and lag > MAX_HOLD
                        and lag < MAX_EXTEND_MS
                    ):
                        effective_max_hold = min(MAX_EXTEND_MS, lag + 200)
                    prev_pnl = pnl

                if outcome_kind is not None:
                    c["observed_label_pnl"] = float(outcome_pnl or 0.0)
                    c["observed_label_lag_ms"] = int(outcome_lag or 0)
                    c["observed_label_kind"] = outcome_kind
                    c["future_snaps_used_count"] = snaps_used
                    jsonl_fp.write(json.dumps({
                        "type": "v42i_observed",
                        "ts_ms": _now_ms(),
                        "mint": c["mint"],
                        "rule_id": c["rule_id"],
                        "decision_ts_ms": c["decision_ts_ms"],
                        "observed_label_pnl": outcome_pnl,
                        "observed_label_kind": outcome_kind,
                        "observed_label_lag_ms": outcome_lag,
                        "future_snaps_used_count": snaps_used,
                    }) + "\n")
                    jsonl_fp.flush()
                    log(
                        f"PGG2-V42I-OBSERVED mint={_short(c['mint'])} "
                        f"pnl={outcome_pnl:+.9f} kind={outcome_kind} "
                        f"lag_ms={outcome_lag} snaps={snaps_used}"
                    )

            if (
                len(candidates) >= args.target_pass
                and all(c["observed_label_pnl"] is not None for c in candidates)
            ):
                log(
                    f"V42I-NO-SEND target_reached_and_all_observed_count="
                    f"{len(candidates)} — early stop"
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

    # Final drain: any candidate without label, label with whatever future
    # snaps exist (may be "expired" with max_fav).
    for c in candidates:
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
            fee_bps=fee_bps,
            creator_fee_bps=creator_fee_bps,
        )
        max_fav = 0.0
        outcome_pnl = None
        outcome_lag = None
        outcome_kind = None
        prev_pnl: Optional[float] = None
        snaps_used = 0
        effective_max_hold = MAX_HOLD
        for s in later:
            snaps_used += 1
            lag = s.ts_ms - c["decision_ts_ms"]
            if lag > effective_max_hold:
                outcome_pnl = prev_pnl if prev_pnl is not None else max_fav
                outcome_lag = effective_max_hold
                outcome_kind = "expired"
                break
            pnl = local_roundtrip_label(
                buy_cs, s.curve_state, args.amount_sol,
            )
            if pnl > max_fav:
                max_fav = pnl
            if pnl >= BANK_TH:
                outcome_pnl = pnl
                outcome_lag = lag
                outcome_kind = "bank"
                break
            if pnl <= LOSS_TH:
                outcome_pnl = pnl
                outcome_lag = lag
                outcome_kind = "loss"
                break
            if max_fav >= SCRATCH_TH and pnl < max_fav and pnl < SCRATCH_TH:
                outcome_pnl = pnl
                outcome_lag = lag
                outcome_kind = "scratch"
                break
            if (
                EXTEND_IF_POS
                and pnl >= 0.0
                and prev_pnl is not None
                and pnl > prev_pnl
                and lag > MAX_HOLD
                and lag < MAX_EXTEND_MS
            ):
                effective_max_hold = min(MAX_EXTEND_MS, lag + 200)
            prev_pnl = pnl
        if outcome_kind is None:
            outcome_pnl = (
                prev_pnl if prev_pnl is not None else max_fav
            )
            outcome_lag = (
                later[-1].ts_ms - c["decision_ts_ms"]
            ) if later else 0
            outcome_kind = "expired"
        c["observed_label_pnl"] = float(outcome_pnl or 0.0)
        c["observed_label_lag_ms"] = int(outcome_lag or 0)
        c["observed_label_kind"] = outcome_kind
        c["future_snaps_used_count"] = snaps_used

    # Aggregate observations.
    bank_obs = 0
    scratch_obs = 0
    neg_obs = 0
    neutral_obs = 0
    pending_obs = 0
    for c in candidates:
        v = c["observed_label_pnl"]
        if v is None:
            pending_obs += 1
            continue
        kind = c.get("observed_label_kind")
        if kind == "bank":
            bank_obs += 1
        elif kind == "scratch":
            scratch_obs += 1
        elif kind == "loss":
            neg_obs += 1
        elif v >= BANK_TH:
            bank_obs += 1
        elif v <= LOSS_TH:
            neg_obs += 1
        elif v >= SCRATCH_TH:
            scratch_obs += 1
        else:
            neutral_obs += 1

    e_stats = engine.stats
    runtime_s = int((_now_ms() - t_start_wall) / 1000)

    # Median active-ticket age at entry.
    ages = [
        int(c["active_ticket_state"].get("active_ticket_age_ms") or 0)
        for c in candidates
        if c["active_ticket_state"].get("active_ticket_age_ms") is not None
    ]
    median_age = int(median(ages)) if ages else 0

    verdict_n = len(candidates) >= args.target_pass
    verdict_no_neg = neg_obs == 0
    verdict_no_lookahead = e_stats.get("lookahead_blocks", 0) == 0
    verdict_pass = verdict_n and verdict_no_neg and verdict_no_lookahead

    md: List[str] = []
    md.append("# V42I_NO_SEND_REPORT\n")
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
    md.append(f"- lookahead_blocks (must be 0 for PASS): "
              f"**{e_stats.get('lookahead_blocks', 0)}**")
    md.append(f"- v42i_state_updates_total: **{state_updates_total}**")
    md.append("")
    md.append("## V42I rule pass counts (actual-mode)")
    for rid, cnt in rule_pass_counts.most_common():
        md.append(f"- {rid}: {cnt}")
    if not rule_pass_counts:
        md.append("- (none)")
    md.append("")
    md.append("## V42I ENTRY-BLOCK counts (per blocker reason)")
    entry_block_total = 0
    block_subset = [
        (k.split(":", 1)[1], v) for k, v in block_counts.items()
        if k.startswith("entry_block:")
    ]
    for reason, cnt in sorted(block_subset, key=lambda x: -x[1])[:20]:
        md.append(f"- {reason}: {cnt}")
        entry_block_total += cnt
    if not block_subset:
        md.append("- (none)")
    md.append("")
    md.append("## Top rule-block reasons (sampled)")
    rule_block_subset = [
        (k.split(":", 1)[1], v) for k, v in block_counts.items()
        if k.startswith("rule:")
    ]
    for reason, cnt in sorted(rule_block_subset, key=lambda x: -x[1])[:15]:
        md.append(f"- {reason}: {cnt}")
    if not rule_block_subset:
        md.append("- (none)")
    md.append("")
    one_bank_actual_entries = sum(
        1 for c in candidates if c["rule_id"] == "v42h_one_bank_plus_continuation"
    )
    md.append("## V42I candidate entries")
    md.append(f"- **candidates_count: {len(candidates)}**")
    md.append(f"- one_bank_actual_entries (legacy V42H rule, must be 0):"
              f" {one_bank_actual_entries}")
    md.append("")
    md.append("## Causal observed outcomes (V42I exit policy)")
    md.append(f"- observed_bank (>= +{BANK_TH} SOL): **{bank_obs}**")
    md.append(f"- observed_scratch: **{scratch_obs}**")
    md.append(f"- observed_clamp_loss (<= {LOSS_TH} SOL): **{neg_obs}**")
    md.append(f"- observed_neutral: **{neutral_obs}**")
    md.append(f"- pending (no future snap): **{pending_obs}**")
    md.append("")
    md.append(f"- median_active_ticket_age_ms_at_entry: **{median_age}**")
    md.append("")
    md.append("## Verdict")
    md.append(f"- meets_target_count (>= {args.target_pass}): **{verdict_n}**")
    md.append(f"- zero_observed_negative_outcomes: **{verdict_no_neg}**")
    md.append(f"- zero_lookahead_violations: **{verdict_no_lookahead}**")
    md.append(f"- **OVERALL Phase-5 verdict: "
              f"{'PASS' if verdict_pass else 'FAIL'}**")
    md.append("")
    md.append("## Per-candidate detail")
    md.append("| # | mint | rule | latest_bank_pnl | active_ticket_age_ms |"
              " active_ticket_pnl | active_ticket_grad | decision_quote |"
              " obs_pnl | obs_kind | obs_lag_ms |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(candidates):
        ats = c["active_ticket_state"]
        lbpnl = ats.get("latest_completed_bank_pnl")
        lbpnl_s = "-" if lbpnl is None else f"{float(lbpnl):+.6f}"
        ata = ats.get("active_ticket_age_ms")
        apnl = ats.get("active_ticket_current_pnl")
        apnl_s = "-" if apnl is None else f"{float(apnl):+.6f}"
        agr = ats.get("active_ticket_pnl_gradient")
        agr_s = "-" if agr is None else f"{float(agr):+.6f}"
        obs = c["observed_label_pnl"]
        obs_s = "PENDING" if obs is None else f"{obs:+.6f}"
        mshort = _short(c["mint"])
        md.append(
            f"| {i+1} | `{mshort}` | {c['rule_id']} | {lbpnl_s} |"
            f" {ata if ata is not None else '-'} | {apnl_s} | {agr_s} |"
            f" {c['decision_quote_sol']:.9f} | {obs_s} |"
            f" {c.get('observed_label_kind','-')} |"
            f" {c.get('observed_label_lag_ms','-')} |"
        )
    md.append("")
    md.append("## Stage-A readiness gate")
    md.append(f"- Phase-6 (corrected dry-live) gate: "
              f"**{'GO' if verdict_pass else 'NO-GO'}**")

    Path(args.out_md).write_text("\n".join(md), encoding="utf-8")
    try:
        jsonl_fp.close()
    except Exception:
        pass
    log(
        f"V42I-NO-SEND end candidates={len(candidates)} bank={bank_obs} "
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
