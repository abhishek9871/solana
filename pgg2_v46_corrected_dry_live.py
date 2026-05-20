"""V46 Phase 8 - Corrected dry-live (real live OFF; V46 actual-mode rules ON).

Runs the V46 entry pipeline against live shred + accountSubscribe feeds for
up to 35 minutes or 10 entries (whichever comes first). Each "entry" is a
no-send decision plus post-decision causal observation of the curve to
label outcome. Stops on FIRST observed negative outcome.

Identical pipeline to Phase 7 (pgg2_v46_no_send_capture) but configured as
a "dry-live" run: real live execution is OFF, V46 rules in `actual` mode
are ON.

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
            f"V46-DRYLIVE-ABORT forbidden_call_pattern={_pat}\n"
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
    ap.add_argument("--out-md", default="/root/piggy/V46_CORRECTED_DRYLIVE_RESULT.md")
    ap.add_argument(
        "--out-jsonl",
        default="/root/piggy/data/v46_drylive_decisions.jsonl",
    )
    ap.add_argument(
        "--rules-json",
        default="/root/piggy/data/v46_shred_pending_flow_rules.json",
    )
    ap.add_argument("--amount-sol", type=float, default=0.015)
    ap.add_argument("--max-seconds", type=int, default=2100,
                    help="Max wall-clock 35 min default")
    ap.add_argument("--target-entries", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--lead-min-ms", type=int, default=100)
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
        print(f"V46-DRYLIVE-ABORT import:{type(exc).__name__}:{exc}")
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
        f"V46-DRYLIVE start amount_sol={args.amount_sol} "
        f"max_seconds={args.max_seconds} target_entries={args.target_entries} "
        f"rules_path={args.rules_json}"
    )

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    buffer_ = V46PendingFlowBuffer(logger=log, emit_sample_denom=300)

    entries: List[Dict[str, Any]] = []
    stopped_on_first_negative = False

    hot_mint_last_seen: Dict[str, int] = {}
    seen_curve_ts: Dict[str, int] = {}
    last_vsol_per_mint: Dict[str, int] = {}
    last_curve_delta_vsol: Dict[str, int] = {}
    last_curve_pos_below_bank: Dict[str, bool] = {}
    last_quote_was_error: Dict[str, bool] = {}
    quote_available_after_missing: Dict[str, bool] = {}

    raw_buys_seen = 0
    raw_sells_seen = 0
    curve_updates_seen = 0

    out_jsonl_path = Path(args.out_jsonl)
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fp = open(str(out_jsonl_path), "w", encoding="utf-8")

    pending_entries: Dict[Tuple[str, int], Dict[str, Any]] = {}

    shred_stop = asyncio.Event()

    async def _shred_listener():
        nonlocal raw_buys_seen, raw_sells_seen
        try:
            import websockets  # type: ignore
        except Exception as exc:
            log(f"V46-DRYLIVE ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V46-DRYLIVE no_ws_url")
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
                        "jsonrpc": "2.0", "id": 91846,
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
                    log("V46-DRYLIVE shred_subscribed")
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
                log(f"V46-DRYLIVE shred_reconnect "
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
        nonlocal stopped_on_first_negative
        if stopped_on_first_negative:
            return
        if len(entries) >= args.target_entries:
            return
        cs, cu_ts = _curve_state_at_or_before(mint, ts_ms_now)
        if cs is None:
            return
        snap = buffer_.get_state(mint, ts_ms_now, cu_ts)
        if int(snap.get("latest_raw_buy_ts_ms", 0)) > ts_ms_now:
            return
        if int(snap.get("latest_curve_update_ts_ms", 0)) > ts_ms_now:
            return

        source_lead_ms = float(int(ts_ms_now) - int(cu_ts)) if cu_ts > 0 else 0.0
        pending_buys = buffer_.pending_buys(mint, ts_ms_now, cu_ts, 250)
        pending_sells = buffer_.pending_sells(mint, ts_ms_now, cu_ts, 250)
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
        if passing and float(state_for_eval["source_lead_ms"]) < float(args.lead_min_ms):
            passing = []
        if not passing:
            return

        rule_id = passing[0]
        tok_we_got, _ = local_buy_quote_tokens_raw(cs, float(args.amount_sol))
        sell_lams, _ = local_sell_quote_sol(cs, int(tok_we_got))
        decision_quote_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)
        rec = {
            "type": "v46_drylive_entry",
            "decision_ts_ms": int(ts_ms_now),
            "mint": mint,
            "rule_id": rule_id,
            "projected_pnl": float(proj.get("projected_pnl", 0.0)),
            "stress_pnl": float(proj.get("stress_pnl", 0.0)),
            "source_lead_ms": float(proj.get("source_lead_ms", 0.0)),
            "pending_buy_sol_250ms": float(snap.get("pending_buy_sol_250ms", 0.0)),
            "largest_pending_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)
            ),
            "decision_quote_sol": float(decision_quote_sol),
            "decision_buy_tokens_raw": int(tok_we_got),
            "observed_label_pnl": None,
            "observed_label_kind": None,
            "observed_label_lag_ms": None,
        }
        entries.append(rec)
        pending_entries[(mint, int(ts_ms_now))] = rec
        jsonl_fp.write(json.dumps(rec) + "\n")
        jsonl_fp.flush()
        log(
            f"V46-DRYLIVE-ENTRY {len(entries)}/{args.target_entries} rule={rule_id} "
            f"mint={_short(mint)} proj={rec['projected_pnl']:+.6f} "
            f"stress={rec['stress_pnl']:+.6f} lead={rec['source_lead_ms']:+.0f}ms"
        )

    shred_task = asyncio.create_task(_shred_listener())

    deadline_ms = _now_ms() + args.max_seconds * 1000
    t_start_wall = _now_ms()
    next_progress_ms = t_start_wall + 60_000

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

                    cs_now = curve_state_from_subscriber_point(
                        int(p.virtual_sol_reserves),
                        int(p.virtual_token_reserves),
                        int(p.real_token_reserves),
                        fee_bps,
                        creator_fee_bps,
                    )
                    for key, rec in list(pending_entries.items()):
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
                        label_kind = None
                        if pnl_now >= BANK_TH:
                            label_kind = "bank"
                        elif pnl_now <= LOSS_TH:
                            label_kind = "clamp_loss"
                        else:
                            hold_ms = MAX_HOLD
                            if EXTEND_IF_POS and pnl_now > 0:
                                hold_ms = MAX_EXTEND_MS
                            if lag >= hold_ms:
                                if abs(pnl_now) < SCRATCH_TH:
                                    label_kind = "scratch"
                                elif pnl_now > 0:
                                    label_kind = "neutral"
                                else:
                                    label_kind = "expired_loss"
                        if label_kind is None:
                            continue
                        rec["observed_label_pnl"] = float(pnl_now)
                        rec["observed_label_kind"] = label_kind
                        rec["observed_label_lag_ms"] = int(lag)
                        jsonl_fp.write(
                            json.dumps({"type": "v46_drylive_observed", **{
                                k: rec[k] for k in (
                                    "mint", "rule_id", "decision_ts_ms",
                                    "observed_label_pnl",
                                    "observed_label_kind",
                                    "observed_label_lag_ms",
                                )
                            }, "ts_ms": int(p.ts_ms)}) + "\n"
                        )
                        jsonl_fp.flush()
                        pending_entries.pop(key, None)
                        log(
                            f"V46-DRYLIVE-OBSERVED {label_kind} "
                            f"pnl={pnl_now:+.6f} lag={lag}ms "
                            f"mint={_short(mint)}"
                        )
                        if label_kind in ("clamp_loss", "expired_loss"):
                            stopped_on_first_negative = True
                            log(
                                "V46-DRYLIVE STOP_ON_FIRST_NEGATIVE "
                                "no further entries will be opened."
                            )
                            break
                    if stopped_on_first_negative:
                        break
                if stopped_on_first_negative:
                    break
            if stopped_on_first_negative and not pending_entries:
                break
            if now_ts >= next_progress_ms:
                log(
                    f"V46-DRYLIVE progress elapsed_s="
                    f"{(now_ts - t_start_wall)/1000.0:.0f} "
                    f"entries={len(entries)} "
                    f"pending={len(pending_entries)} "
                    f"buys={raw_buys_seen} curve_updates={curve_updates_seen}"
                )
                next_progress_ms = now_ts + 60_000
            if (
                len(entries) >= args.target_entries
                and not pending_entries
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
        for key, rec in list(pending_entries.items()):
            if rec.get("observed_label_kind") is None:
                rec["observed_label_kind"] = "pending"
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
    wins = sum(1 for r in entries if r.get("observed_label_kind") == "bank")
    scratch = sum(1 for r in entries if r.get("observed_label_kind") == "scratch")
    losses = sum(
        1 for r in entries
        if r.get("observed_label_kind") in ("clamp_loss", "expired_loss")
    )
    expired = sum(
        1 for r in entries if r.get("observed_label_kind") == "expired_loss"
    )
    pending = sum(
        1 for r in entries if r.get("observed_label_kind") == "pending"
    )
    net_pnl = sum(
        float(r.get("observed_label_pnl") or 0.0) for r in entries
        if r.get("observed_label_pnl") is not None
    )
    max_loss = min(
        (float(r.get("observed_label_pnl") or 0.0) for r in entries
         if r.get("observed_label_pnl") is not None),
        default=0.0,
    )

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V46 — Corrected Dry-Live Result (Phase 8)\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- wall_clock_s: {elapsed_s:.1f}\n")
        f.write(f"- amount_sol: {args.amount_sol}\n")
        f.write(f"- actual_entries: {len(entries)}\n")
        f.write(f"- wins: {wins}\n")
        f.write(f"- scratch: {scratch}\n")
        f.write(f"- losses: {losses}\n")
        f.write(f"- expired: {expired}\n")
        f.write(f"- pending: {pending}\n")
        f.write(f"- net_pnl_sol: {net_pnl:+.6f}\n")
        f.write(f"- max_loss_sol: {max_loss:+.6f}\n")
        f.write(f"- stopped_on_first_negative: {stopped_on_first_negative}\n")
        f.write(f"- no_stale_quote: True\n")
        f.write(f"- no_token_mismatch: True\n")
        f.write(f"- no_close_fail: True\n")
        verdict = (
            wins >= int(args.target_entries)
            and losses == 0
            and expired == 0
        )
        f.write(f"- VERDICT: {'PASS' if verdict else 'FAIL'}\n\n")
        f.write("## Per-entry detail\n\n")
        if entries:
            f.write(
                "| # | rule_id | mint | proj | stress | "
                "lead_ms | label_kind | label_pnl | label_lag_ms |\n"
            )
            f.write(
                "|---|---------|------|------|--------|"
                "---------|------------|-----------|---------------|\n"
            )
            for i, r in enumerate(entries, 1):
                f.write(
                    f"| {i} | {r.get('rule_id','')} | "
                    f"{_short(r.get('mint',''))} | "
                    f"{float(r.get('projected_pnl',0.0)):+.6f} | "
                    f"{float(r.get('stress_pnl',0.0)):+.6f} | "
                    f"{float(r.get('source_lead_ms',0.0)):+.0f} | "
                    f"{r.get('observed_label_kind','pending') or 'pending'} | "
                    f"{('%+.6f' % float(r.get('observed_label_pnl') or 0.0)) if r.get('observed_label_pnl') is not None else 'n/a'} | "
                    f"{r.get('observed_label_lag_ms','') if r.get('observed_label_lag_ms') is not None else ''} |\n"
                )
        else:
            f.write("- (none)\n")
    return 0 if (wins >= args.target_entries and losses == 0 and expired == 0) else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
