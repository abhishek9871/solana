"""V46 Phase 1 - Shred Coverage Audit.

Goal: empirically measure whether raw Pump shred BUY events (over the
existing shredSubscribe websocket) lead the accountSubscribe curve account
updates by >=100ms.

Methodology:
  1. Subscribe to shredSubscribe (program=PUMP_PROGRAM) - global capture of
     all Pump BUY/SELL instructions via parse_base64_shred_for_pump_events.
  2. Subscribe to accountSubscribe for the bonding-curve PDA of each mint
     that appears in shred events (mint is auto-discovered).
  3. Record every shred BUY event with ts_received and slot.
  4. Record every accountSubscribe curve update where
     virtual_sol_reserves delta > 0 (positive money flow), with ts_received
     and slot.
  5. For each positive curve update, find the LATEST shred buy event(s) on
     the same mint with ts_shred <= ts_curve and slot in {curve_slot,
     curve_slot-1, curve_slot+1}. Compute lead = ts_curve - ts_shred.
  6. After capture window, summarize statistics and emit
     V46_SHRED_COVERAGE_AUDIT.md.

PURE OBSERVATION. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re as _re
import statistics
import sys
import time
from collections import deque, Counter
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
            f"V46-AUDIT-ABORT forbidden_call_pattern={_pat}\n"
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
    ap.add_argument("--out-md", default="/root/piggy/V46_SHRED_COVERAGE_AUDIT.md")
    ap.add_argument(
        "--out-jsonl",
        default="/root/piggy/data/v46_shred_coverage_events.jsonl",
    )
    ap.add_argument("--max-seconds", type=int, default=480,
                    help="Capture duration in seconds (5-10 min)")
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--lead-pass-ms", type=int, default=100,
                    help="Lead threshold for PASS")
    ap.add_argument("--pass-fraction", type=float, default=0.30,
                    help="Min fraction of positive curve updates with lead>=lead-pass-ms")
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
    except Exception as exc:
        print(f"V46-AUDIT-ABORT import:{type(exc).__name__}:{exc}")
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

    log(
        f"V46-AUDIT start max_seconds={args.max_seconds} "
        f"lead_pass_ms={args.lead_pass_ms} pass_fraction={args.pass_fraction} "
        f"max_hot_mints={args.max_hot_mints}"
    )
    if os.environ.get("PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE", "0") != "1":
        log("V46-AUDIT WARNING: PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE != 1")

    out_jsonl_path = Path(args.out_jsonl)
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fp = open(str(out_jsonl_path), "w", encoding="utf-8")

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    # Buffers (mint-keyed)
    shred_buys: Dict[str, Deque[dict]] = {}  # mint -> deque of {ts_ms, slot, sol_in, signer, sig}
    shred_sells: Dict[str, Deque[dict]] = {}
    curve_updates: Dict[str, Deque[dict]] = {}  # mint -> deque of {ts_ms, slot, vsol, vtok, delta_vsol, prev_vsol}
    last_vsol_per_mint: Dict[str, int] = {}
    hot_mint_last_seen: Dict[str, int] = {}
    seen_curve_ts: Dict[str, int] = {}

    # Stats
    total_shred_buys = 0
    total_shred_sells = 0
    total_curve_updates = 0
    total_positive_curve_updates = 0
    leads_observed: List[Tuple[str, int, float]] = []  # (mint, slot, lead_ms)
    positive_updates_with_lead: int = 0
    positive_updates_with_lead_ge_100: int = 0
    positive_updates_no_prior_buy: int = 0
    sample_no_buy_mints: List[Tuple[str, int]] = []

    shred_stop = asyncio.Event()

    async def _shred_listener():
        nonlocal total_shred_buys, total_shred_sells
        try:
            import websockets  # type: ignore
        except Exception as exc:
            log(f"V46-AUDIT ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V46-AUDIT no_ws_url")
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
                        "jsonrpc": "2.0", "id": 90046,
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
                    log("V46-AUDIT shred_subscribed")
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
                            sol_in = sol_lamports / 1_000_000_000.0
                            signer = str(getattr(ev, "signer", "") or "")
                            sig = str(getattr(ev, "sig", "") or "")
                            is_buy = bool(getattr(ev, "is_buy", False))
                            entry = {
                                "ts_ms": ts_ms,
                                "slot": slot,
                                "sol_in": sol_in,
                                "signer": signer,
                                "sig": sig,
                            }
                            if is_buy:
                                shred_buys.setdefault(
                                    m, deque(maxlen=512)
                                ).append(entry)
                                total_shred_buys += 1
                            else:
                                shred_sells.setdefault(
                                    m, deque(maxlen=512)
                                ).append(entry)
                                total_shred_sells += 1
                            hot_mint_last_seen[m] = ts_ms
                            if len(hot_mint_last_seen) <= args.max_hot_mints:
                                oracle.request_subscription(m)
                            oracle.mark_feed_event(m, ts_ms)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(f"V46-AUDIT shred_reconnect "
                    f"exc={type(exc).__name__}:{exc}")
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2.0, 30.0)

    shred_task = asyncio.create_task(_shred_listener())

    deadline_ms = _now_ms() + args.max_seconds * 1000
    t_start_wall = _now_ms()
    next_progress_ms = t_start_wall + 30_000

    try:
        while _now_ms() < deadline_ms:
            await asyncio.sleep(0.05)
            now_ts = _now_ms()
            stale_cutoff = now_ts - 60_000
            cold = [m for m, t in hot_mint_last_seen.items()
                    if t < stale_cutoff]
            for m in cold:
                hot_mint_last_seen.pop(m, None)

            # Poll oracle states for new curve points
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

                total_curve_updates += 1
                prev_vsol = last_vsol_per_mint.get(
                    mint, latest_pt.virtual_sol_reserves
                )
                delta_vsol = (
                    int(latest_pt.virtual_sol_reserves) - int(prev_vsol)
                )
                last_vsol_per_mint[mint] = int(latest_pt.virtual_sol_reserves)
                cu_entry = {
                    "ts_ms": int(latest_pt.ts_ms),
                    "slot": int(latest_pt.slot),
                    "vsol": int(latest_pt.virtual_sol_reserves),
                    "vtok": int(latest_pt.virtual_token_reserves),
                    "prev_vsol": int(prev_vsol),
                    "delta_vsol": int(delta_vsol),
                }
                curve_updates.setdefault(
                    mint, deque(maxlen=512)
                ).append(cu_entry)

                if delta_vsol <= 0:
                    continue
                total_positive_curve_updates += 1

                # Find latest shred buy(s) for this mint with ts<=cu_ts and
                # slot in {cu_slot, cu_slot-1, cu_slot+1}
                buys_for_mint = shred_buys.get(mint) or deque()
                candidate_buys = [
                    b for b in buys_for_mint
                    if b["ts_ms"] <= cu_entry["ts_ms"]
                    and (b["slot"] == cu_entry["slot"]
                         or b["slot"] == cu_entry["slot"] - 1
                         or b["slot"] == cu_entry["slot"] + 1)
                ]
                if not candidate_buys:
                    positive_updates_no_prior_buy += 1
                    if len(sample_no_buy_mints) < 20:
                        sample_no_buy_mints.append(
                            (mint, cu_entry["slot"])
                        )
                    record = {
                        "kind": "positive_curve_update_no_prior_buy",
                        "mint": mint, "ts_curve_ms": cu_entry["ts_ms"],
                        "slot": cu_entry["slot"],
                        "delta_vsol": cu_entry["delta_vsol"],
                        "prev_vsol": cu_entry["prev_vsol"],
                    }
                    jsonl_fp.write(json.dumps(record) + "\n")
                    jsonl_fp.flush()
                    continue

                # Use the latest buy <= ts_curve as the lead anchor.
                candidate_buys.sort(key=lambda b: b["ts_ms"])
                anchor = candidate_buys[-1]
                lead_ms = float(cu_entry["ts_ms"] - anchor["ts_ms"])
                leads_observed.append(
                    (mint, cu_entry["slot"], lead_ms)
                )
                positive_updates_with_lead += 1
                if lead_ms >= args.lead_pass_ms:
                    positive_updates_with_lead_ge_100 += 1

                record = {
                    "kind": "positive_curve_update_with_lead",
                    "mint": mint,
                    "ts_curve_ms": cu_entry["ts_ms"],
                    "ts_shred_ms": anchor["ts_ms"],
                    "lead_ms": lead_ms,
                    "slot": cu_entry["slot"],
                    "shred_slot": anchor["slot"],
                    "delta_vsol": cu_entry["delta_vsol"],
                    "prev_vsol": cu_entry["prev_vsol"],
                    "shred_sol_in": anchor["sol_in"],
                    "signer": anchor["signer"],
                    "n_candidate_buys_in_slot_window": len(candidate_buys),
                }
                jsonl_fp.write(json.dumps(record) + "\n")
                jsonl_fp.flush()

            # Progress emit every 30s
            if now_ts >= next_progress_ms:
                log(
                    f"V46-AUDIT progress elapsed_s={(now_ts - t_start_wall)/1000.0:.0f} "
                    f"buys={total_shred_buys} sells={total_shred_sells} "
                    f"updates={total_curve_updates} pos={total_positive_curve_updates} "
                    f"with_lead={positive_updates_with_lead} "
                    f"with_lead_ge_{args.lead_pass_ms}={positive_updates_with_lead_ge_100} "
                    f"no_buy={positive_updates_no_prior_buy} "
                    f"hot_mints={len(hot_mint_last_seen)}"
                )
                next_progress_ms = now_ts + 30_000
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
    lead_values = [l for _m, _s, l in leads_observed]
    lead_med = statistics.median(lead_values) if lead_values else 0.0
    lead_p25 = (
        statistics.quantiles(lead_values, n=4)[0]
        if len(lead_values) >= 4 else (
            min(lead_values) if lead_values else 0.0
        )
    )
    lead_p75 = (
        statistics.quantiles(lead_values, n=4)[2]
        if len(lead_values) >= 4 else (
            max(lead_values) if lead_values else 0.0
        )
    )
    lead_p95 = (
        statistics.quantiles(lead_values, n=20)[18]
        if len(lead_values) >= 20 else (
            max(lead_values) if lead_values else 0.0
        )
    )
    lead_max = max(lead_values) if lead_values else 0.0
    lead_min = min(lead_values) if lead_values else 0.0

    frac_with_lead_ge_100 = (
        positive_updates_with_lead_ge_100 / total_positive_curve_updates
        if total_positive_curve_updates > 0 else 0.0
    )
    verdict_pass = (
        total_positive_curve_updates > 0
        and frac_with_lead_ge_100 >= args.pass_fraction
    )

    # Determine feed shape via PUMP_PROGRAM-only subscription (global).
    feed_shape = "global_pump_program_shredSubscribe"

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V46 — Shred Coverage Audit\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- wall_clock_s: {elapsed_s:.1f}\n")
        f.write(f"- shred_feed_shape: {feed_shape}\n")
        f.write(f"- shred_feed_source: shredSubscribe over SOLANATRACKER_RPC_WS\n")
        f.write(f"- shred_filter: accountInclude=[PUMP_PROGRAM] (global, not mint-filtered)\n")
        f.write(f"- curve_source: accountSubscribe via CurveAccountSubscriberOracle\n")
        f.write(f"- lead_pass_ms: {args.lead_pass_ms}\n")
        f.write(f"- pass_fraction: {args.pass_fraction}\n\n")
        f.write("## Counters\n\n")
        f.write(f"- total_shred_buys: {total_shred_buys}\n")
        f.write(f"- total_shred_sells: {total_shred_sells}\n")
        f.write(f"- total_curve_updates: {total_curve_updates}\n")
        f.write(f"- positive_curve_updates: {total_positive_curve_updates}\n")
        f.write(f"- positive_updates_with_shred_lead_observed: {positive_updates_with_lead}\n")
        f.write(f"- positive_updates_with_shred_lead_ge_{args.lead_pass_ms}ms: {positive_updates_with_lead_ge_100}\n")
        f.write(f"- positive_updates_no_prior_buy_in_slot_window: {positive_updates_no_prior_buy}\n")
        f.write(f"- unique_mints_observed: {len(hot_mint_last_seen)}\n\n")
        f.write("## Fraction analysis\n\n")
        if total_positive_curve_updates > 0:
            f.write(
                f"- frac_positive_with_any_prior_buy: "
                f"{positive_updates_with_lead/total_positive_curve_updates:.4f}\n"
            )
            f.write(
                f"- frac_positive_with_lead_ge_{args.lead_pass_ms}ms: "
                f"{frac_with_lead_ge_100:.4f}\n"
            )
        else:
            f.write("- (no positive curve updates observed)\n")
        f.write("\n## Lead-time distribution (ms; positive=raw led curve)\n\n")
        if lead_values:
            f.write(f"- n_observations: {len(lead_values)}\n")
            f.write(f"- min: {lead_min:.1f}\n")
            f.write(f"- p25: {lead_p25:.1f}\n")
            f.write(f"- median: {lead_med:.1f}\n")
            f.write(f"- p75: {lead_p75:.1f}\n")
            f.write(f"- p95: {lead_p95:.1f}\n")
            f.write(f"- max: {lead_max:.1f}\n")
            mean_lead = sum(lead_values) / len(lead_values)
            f.write(f"- mean: {mean_lead:.1f}\n")
        else:
            f.write("- (no leads observed)\n")
        f.write("\n## Sample mints where curve moved positive but no prior shred buy\n\n")
        if sample_no_buy_mints:
            for m, slot in sample_no_buy_mints[:20]:
                f.write(f"- {_short(m)} slot={slot}\n")
        else:
            f.write("- (none)\n")
        f.write("\n## Verdict\n\n")
        f.write(f"- pass_criterion: positive_curve_updates>0 AND frac_with_lead_ge_{args.lead_pass_ms}ms >= {args.pass_fraction}\n")
        f.write(f"- frac_with_lead_ge_{args.lead_pass_ms}ms: {frac_with_lead_ge_100:.4f}\n")
        f.write(f"- VERDICT: {'PASS' if verdict_pass else 'FAIL'}\n")
        if not verdict_pass and total_positive_curve_updates > 0:
            f.write(f"- blocker: median lead {lead_med:.1f}ms; "
                    f"only {positive_updates_with_lead_ge_100}/"
                    f"{total_positive_curve_updates} positive updates "
                    f"had a prior buy with lead >= {args.lead_pass_ms}ms\n")
        if total_positive_curve_updates == 0:
            f.write("- blocker: no positive curve updates observed in capture window\n")
        f.write("\n")
    log(f"V46-AUDIT wrote {md_path}")
    log(
        f"V46-AUDIT done elapsed_s={elapsed_s:.1f} "
        f"buys={total_shred_buys} sells={total_shred_sells} "
        f"pos_updates={total_positive_curve_updates} "
        f"frac_lead_ge_{args.lead_pass_ms}={frac_with_lead_ge_100:.4f} "
        f"verdict={'PASS' if verdict_pass else 'FAIL'}"
    )
    return 0 if verdict_pass else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
