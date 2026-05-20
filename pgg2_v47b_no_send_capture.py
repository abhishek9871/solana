"""V47B - No-Send capture with guarded-branch size sweep.

Forks pgg2_v47p13_no_send_capture.py and replaces the V46 project-and-gate
flow with V47B guarded-branch sim + adverse-fail guard:

  For each raw Pump BUY shred event on a tracked mint, for each size in the
  sweep [0.005, 0.010, 0.015, 0.020, 0.030, 0.050, 0.075, 0.100]:
    1. simulate_branches -> exp/par/adv outcomes
    2. compute_guard_for_adverse_fail_or_profit -> final_min_tokens or
       guard_too_tight
    3. resim with guard -> branch outcomes including SAFE_BUY_FAIL
    4. If pass (all branches WIN or SAFE_BUY_FAIL) AND
       expected_pnl >= required_profit_for_size -> selectable

  Among selectable sizes: pick SMALLEST with expected_pnl >= required_profit.

Causal: every feature ts <= decision_ts_ms. Future curve snapshots = label only.

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
            f"V47B-NO-SEND-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Adaptive size sweep. 0.100 is no-send only; dry-live picks max 0.075.
SIZE_SWEEP_SOL = (0.005, 0.010, 0.015, 0.020, 0.030, 0.050, 0.075, 0.100)
# Cap dry-live size at 0.075 SOL (capture is no-send, so 0.1 allowed here).
DRYLIVE_MAX_SIZE_SOL = 0.075


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _required_profit_for_size(size_sol: float, tx_fee_sol: float) -> float:
    """Min expected PnL we require for a size to be selectable.

    Heuristic: max(2*tx_fee + priority_buffer + 0.000005, size * 0.0010).
    priority_buffer = 0.0000287 (one extra unit) for safety.
    """
    sig = 2.0 * float(tx_fee_sol)
    priority_buf = 0.0000287
    floor_a = sig + priority_buf + 0.000005
    floor_b = float(size_sol) * 0.0010
    return float(max(floor_a, floor_b))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", required=True)
    ap.add_argument(
        "--out-jsonl",
        default="/root/piggy/data/v47b_no_send_decisions.jsonl",
    )
    ap.add_argument("--max-seconds", type=int, default=600)
    ap.add_argument("--target-pass", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument("--debug-log", default="")
    ap.add_argument("--strategy-min-tokens-frac", type=float, default=0.95)
    ap.add_argument("--max-guard-fraction", type=float, default=0.995)
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
        from pgg2_v47b_guarded_branch_sim import (  # type: ignore
            simulate_branches,
            BRANCH_WIN,
            BRANCH_SAFE_BUY_FAIL,
            BRANCH_UNSAFE_OPEN,
            BRANCH_UNKNOWN,
        )
        from pgg2_v47b_adverse_fail_guard import (  # type: ignore
            compute_guard_for_adverse_fail_or_profit,
        )
    except Exception as exc:
        print(f"V47B-NO-SEND-ABORT import:{type(exc).__name__}:{exc}")
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

    BANK_TH = 0.00060
    SCRATCH_TH = 0.00005
    LOSS_TH = -0.00050
    MAX_HOLD = 1500
    MAX_EXTEND_MS = 3000
    EXTEND_IF_POS = True

    log(
        f"V47B-NO-SEND start sizes={SIZE_SWEEP_SOL} "
        f"max_seconds={args.max_seconds} target_pass={args.target_pass} "
        f"strategy_min_frac={args.strategy_min_tokens_frac} "
        f"max_guard_frac={args.max_guard_fraction} "
        f"bank={BANK_TH} scratch={SCRATCH_TH} clamp={LOSS_TH} "
        f"max_hold={MAX_HOLD}"
    )
    if os.environ.get("PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE", "0") != "1":
        log("V47B-NO-SEND WARNING: PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE != 1")
    if os.environ.get("V47B_NO_SEND", "0") != "1":
        log("V47B-NO-SEND WARNING: V47B_NO_SEND env not 1")

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(
        f"V47B-NO-SEND fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}"
    )

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    buffer_ = V46PendingFlowBuffer(logger=log, emit_sample_denom=200)

    candidates: List[Dict[str, Any]] = []
    seen_pass_mints: set = set()
    lookahead_block_count = 0

    raw_buys_seen = 0
    raw_sells_seen = 0
    curve_updates_seen = 0
    sim_evals_total = 0
    snapshots_total = 0

    # Per-size counters across all evaluations.
    size_eval_counts: Dict[float, int] = {s: 0 for s in SIZE_SWEEP_SOL}
    size_guarded_pass_counts: Dict[float, int] = {s: 0 for s in SIZE_SWEEP_SOL}
    size_guard_too_tight_counts: Dict[float, int] = {
        s: 0 for s in SIZE_SWEEP_SOL
    }
    size_unsafe_open_counts: Dict[float, int] = {s: 0 for s in SIZE_SWEEP_SOL}
    size_selectable_counts: Dict[float, int] = {s: 0 for s in SIZE_SWEEP_SOL}
    size_required_profit_fail_counts: Dict[float, int] = {
        s: 0 for s in SIZE_SWEEP_SOL
    }

    # Per-branch outcome distribution (across selected-size branch trials).
    exp_outcome_counts: Counter = Counter()
    par_outcome_counts: Counter = Counter()
    adv_outcome_counts: Counter = Counter()

    blocker_counts: Counter = Counter()
    selected_size_dist: Counter = Counter()

    hot_mint_last_seen: Dict[str, int] = {}
    seen_curve_ts: Dict[str, int] = {}
    last_vsol_per_mint: Dict[str, int] = {}

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
            log(f"V47B-NO-SEND ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V47B-NO-SEND no_ws_url")
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
                    log("V47B-NO-SEND shred_subscribed")
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
                log(f"V47B-NO-SEND shred_reconnect "
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

    def _evaluate_size_for_mint(
        cs, size_sol: float, pending_buys, pending_sells,
        tx_fee_sol: float,
    ) -> Dict[str, Any]:
        """Run V47B for a single size. Returns dict with selectable/blocker."""
        # Pass 1: no guard (to get expected/adverse token counts).
        r0 = simulate_branches(
            cs, size_sol, pending_buys, pending_sells,
            exec_delay_ms=250,
        )
        exp_t = int(r0["expected_tokens"])
        adv_t = int(r0["adverse_tokens"])

        # Track unsafe-open (without guard) for diagnostic.
        if r0["adverse_branch_outcome"] == BRANCH_UNSAFE_OPEN \
                or r0["expected_branch_outcome"] == BRANCH_UNSAFE_OPEN \
                or r0["partial_branch_outcome"] == BRANCH_UNSAFE_OPEN:
            # Will be reported regardless of guard (post-guard SAFE_FAIL
            # for adverse, but a true UNSAFE here means even continuation
            # is bad).
            pass

        if exp_t <= 0:
            return {
                "selectable": False,
                "blocker": "expected_tokens_zero",
                "size_sol": float(size_sol),
                "r0": r0, "r1": None, "guard": None,
                "required_profit": 0.0,
            }

        strategy_min = int(exp_t * float(args.strategy_min_tokens_frac))
        g = compute_guard_for_adverse_fail_or_profit(
            expected_tokens=exp_t,
            adverse_tokens=adv_t,
            min_tokens_for_nonnegative_exit=0,  # rely on expected_pnl check
            strategy_min_tokens=strategy_min,
            max_guard_fraction=float(args.max_guard_fraction),
        )

        if not g["pass"]:
            return {
                "selectable": False,
                "blocker": "guard_too_tight",
                "size_sol": float(size_sol),
                "r0": r0, "r1": None, "guard": g,
                "required_profit": 0.0,
            }

        # Pass 2: with guard.
        r1 = simulate_branches(
            cs, size_sol, pending_buys, pending_sells,
            exec_delay_ms=250,
            guard_min_tokens=int(g["final_min_tokens"]),
        )

        required_profit = _required_profit_for_size(size_sol, tx_fee_sol)

        if not r1["pass"]:
            return {
                "selectable": False,
                "blocker": f"branch_check:{r1['blocker']}",
                "size_sol": float(size_sol),
                "r0": r0, "r1": r1, "guard": g,
                "required_profit": float(required_profit),
            }

        if float(r1["expected_pnl"]) < float(required_profit):
            return {
                "selectable": False,
                "blocker": "expected_pnl_below_required",
                "size_sol": float(size_sol),
                "r0": r0, "r1": r1, "guard": g,
                "required_profit": float(required_profit),
            }

        return {
            "selectable": True,
            "blocker": None,
            "size_sol": float(size_sol),
            "r0": r0, "r1": r1, "guard": g,
            "required_profit": float(required_profit),
        }

    def _maybe_evaluate(
        mint: str, ts_ms_now: int, slot: int, sol_in: float, signer: str,
    ):
        nonlocal sim_evals_total, lookahead_block_count
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

        # Optional: only run sim when there's at least some pending flow
        # to make the adverse branch meaningful. Without pending flow, all
        # branches are identical -> guard_too_tight.
        if not pending_buys and not pending_sells:
            return

        sim_evals_total += 1

        # Sweep sizes and find smallest selectable that meets required_profit.
        size_results: Dict[float, Dict[str, Any]] = {}
        for size in SIZE_SWEEP_SOL:
            size_eval_counts[size] += 1
            res = _evaluate_size_for_mint(
                cs, size, pending_buys, pending_sells,
                float(DEFAULT_TX_FEE_SOL),
            )
            size_results[size] = res
            # Diagnostic counters.
            r0 = res["r0"]
            r1 = res["r1"]
            if r0 is not None:
                if r1 is None:
                    # Pass-1 outcomes only, but track unsafe_open flag.
                    for outc in (
                        r0["expected_branch_outcome"],
                        r0["partial_branch_outcome"],
                        r0["adverse_branch_outcome"],
                    ):
                        if outc == BRANCH_UNSAFE_OPEN:
                            size_unsafe_open_counts[size] += 1
                            break
                else:
                    if r1["pass"]:
                        size_guarded_pass_counts[size] += 1
            if res["blocker"] == "guard_too_tight":
                size_guard_too_tight_counts[size] += 1
            if res["blocker"] == "expected_pnl_below_required":
                size_required_profit_fail_counts[size] += 1
            if res["selectable"]:
                size_selectable_counts[size] += 1
            if res["blocker"]:
                blocker_counts[res["blocker"]] += 1
            # Branch outcome distribution from pass-2 (guarded) or pass-1.
            r_for_dist = r1 if r1 is not None else r0
            if r_for_dist is not None:
                exp_outcome_counts[r_for_dist["expected_branch_outcome"]] += 1
                par_outcome_counts[r_for_dist["partial_branch_outcome"]] += 1
                adv_outcome_counts[r_for_dist["adverse_branch_outcome"]] += 1

        # Pick smallest selectable size with expected_pnl >= required_profit.
        selectable_sizes = [
            s for s, r in size_results.items() if r["selectable"]
        ]
        if not selectable_sizes:
            return

        # Apply dry-live max cap during selection (no-send capture has no cap,
        # but we want to be honest about which sizes a downstream dry-live
        # COULD select; we still report selectables). For the *chosen* size,
        # spec says "smallest selectable", and downstream dry-live runs on
        # whatever we record. Capture allows 0.100, but dry-live must cap.
        selected_size = min(selectable_sizes)
        res = size_results[selected_size]
        r1 = res["r1"]
        g = res["guard"]

        # Build candidate record.
        tok_we_got = int(r1["expected_tokens"])
        sell_lams_expected = int(r1["expected_sell_lams"])
        decision_quote_sol = float(sell_lams_expected) / float(LAMPORTS_PER_SOL)

        rec = {
            "type": "v47b_candidate",
            "decision_ts_ms": int(ts_ms_now),
            "mint": mint,
            "slot_at_decision": int(slot),
            "sol_in_at_decision": float(sol_in),
            "signer_at_decision": signer,
            "selected_size_sol": float(selected_size),
            "required_profit_sol": float(res["required_profit"]),
            "expected_pnl": float(r1["expected_pnl"]),
            "partial_pnl": float(r1["partial_pnl"]),
            "adverse_pnl": float(r1["adverse_pnl"]),
            "expected_tokens": int(r1["expected_tokens"]),
            "partial_tokens": int(r1["partial_tokens"]),
            "adverse_tokens": int(r1["adverse_tokens"]),
            "final_min_tokens_guard": int(g["final_min_tokens"]),
            "guard_fraction": float(g["guard_fraction"]),
            "expected_branch_outcome": r1["expected_branch_outcome"],
            "partial_branch_outcome": r1["partial_branch_outcome"],
            "adverse_branch_outcome": r1["adverse_branch_outcome"],
            # Per-size selectable list for full transparency.
            "all_size_results": {
                f"{s:.3f}": {
                    "selectable": bool(size_results[s]["selectable"]),
                    "blocker": size_results[s]["blocker"],
                    "expected_pnl": float(
                        size_results[s]["r1"]["expected_pnl"]
                        if size_results[s]["r1"] is not None
                        else size_results[s]["r0"]["expected_pnl"]
                    ),
                    "adverse_pnl": float(
                        size_results[s]["r1"]["adverse_pnl"]
                        if size_results[s]["r1"] is not None
                        else size_results[s]["r0"]["adverse_pnl"]
                    ),
                }
                for s in SIZE_SWEEP_SOL
            },
            # Pending-flow features (causally OK; from buffer at ts_ms_now).
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
            "source_lead_ms": float(source_lead_ms),
            "decision_quote_sol": float(decision_quote_sol),
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
        selected_size_dist[f"{selected_size:.3f}"] += 1
        seen_pass_mints.add(mint)
        pending_candidates[(mint, int(ts_ms_now))] = rec
        jsonl_fp.write(json.dumps(rec) + "\n")
        jsonl_fp.flush()
        log(
            f"V47B-CANDIDATE mint={_short(mint)} "
            f"size={selected_size:.4f} "
            f"exp_pnl={r1['expected_pnl']:+.6f} "
            f"par_pnl={r1['partial_pnl']:+.6f} "
            f"adv_pnl={r1['adverse_pnl']:+.6f} "
            f"guard={g['final_min_tokens']} "
            f"lead={rec['source_lead_ms']:+.0f}ms "
            f"adv_branch={r1['adverse_branch_outcome']} "
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
                        tok_at_dec = int(rec.get("expected_tokens", 0))
                        size_sol = float(rec.get("selected_size_sol", 0.0))
                        if tok_at_dec <= 0:
                            sell_lams_now = 0
                        else:
                            sell_lams_now, _ = local_sell_quote_sol(
                                cs_now, int(tok_at_dec)
                            )
                        sell_sol_now = float(sell_lams_now) / float(LAMPORTS_PER_SOL)
                        pnl_now = (
                            sell_sol_now
                            - float(size_sol)
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
                                json.dumps({"type": "v47b_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "decision_ts_ms",
                                        "selected_size_sol",
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
                                f"V47B-OBSERVED bank pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)} "
                                f"size={size_sol:.4f}"
                            )
                            continue
                        if pnl_now <= LOSS_TH:
                            rec["observed_label_pnl"] = float(pnl_now)
                            rec["observed_label_kind"] = "clamp_loss"
                            rec["observed_label_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47b_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "decision_ts_ms",
                                        "selected_size_sol",
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
                                f"V47B-OBSERVED clamp_loss pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)} "
                                f"size={size_sol:.4f}"
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
                                json.dumps({"type": "v47b_observed", **{
                                    k: rec[k] for k in (
                                        "mint", "decision_ts_ms",
                                        "selected_size_sol",
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
                                f"V47B-OBSERVED {label_kind} pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )

            if now_ts >= next_progress_ms:
                log(
                    f"V47B-NO-SEND progress elapsed_s="
                    f"{(now_ts - t_start_wall)/1000.0:.0f} "
                    f"buys={raw_buys_seen} sells={raw_sells_seen} "
                    f"curve_updates={curve_updates_seen} "
                    f"sim_evals={sim_evals_total} "
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
                    json.dumps({"type": "v47b_observed", **{
                        k: rec.get(k) for k in (
                            "mint", "decision_ts_ms",
                            "selected_size_sol",
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
        # NOTE: do not close log_fp here; log() still uses it below for
        # the wrote/done lines. It is closed at function exit via GC.

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

    all_candidates_adverse_safe = (
        all(
            r.get("adverse_branch_outcome") in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL)
            for r in candidates
        ) if candidates else False
    )
    all_candidates_in_size_range = (
        all(
            float(r.get("selected_size_sol", 0.0)) >= 0.005
            and float(r.get("selected_size_sol", 0.0)) <= 0.100
            for r in candidates
        ) if candidates else False
    )
    all_candidates_guard_feasible = (
        all(
            int(r.get("final_min_tokens_guard", 0)) > 0
            for r in candidates
        ) if candidates else False
    )

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V47B - Guarded-Branch Size No-Send Report\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- runtime_s: {elapsed_s:.1f}\n")
        f.write(f"- sizes_tested: {SIZE_SWEEP_SOL}\n")
        f.write(f"- amount_sol_min: {min(SIZE_SWEEP_SOL)}\n")
        f.write(f"- amount_sol_max: {max(SIZE_SWEEP_SOL)}\n")
        f.write(f"- strategy_min_tokens_frac: {args.strategy_min_tokens_frac}\n")
        f.write(f"- max_guard_fraction: {args.max_guard_fraction}\n")
        f.write(f"- target_pass: {args.target_pass}\n\n")
        f.write("## Engine sanity\n\n")
        f.write(f"- raw_pump_buys_seen: {raw_buys_seen}\n")
        f.write(f"- raw_pump_sells_seen: {raw_sells_seen}\n")
        f.write(f"- curve_updates: {curve_updates_seen}\n")
        f.write(f"- snapshots: {snapshots_total}\n")
        f.write(f"- sim_evaluations: {sim_evals_total}\n")
        f.write(f"- lookahead_blocks: {lookahead_block_count}\n")
        f.write(f"- hot_mints_terminal: {len(hot_mint_last_seen)}\n\n")
        f.write("## Size sweep results\n\n")
        f.write(
            "| size_sol | evaluated | guarded_pass | guard_too_tight | "
            "required_profit_fail | unsafe_open | selectable |\n"
        )
        f.write(
            "|----------|-----------|--------------|-----------------|"
            "----------------------|-------------|------------|\n"
        )
        for s in SIZE_SWEEP_SOL:
            f.write(
                f"| {s:.3f} | {size_eval_counts[s]} | "
                f"{size_guarded_pass_counts[s]} | "
                f"{size_guard_too_tight_counts[s]} | "
                f"{size_required_profit_fail_counts[s]} | "
                f"{size_unsafe_open_counts[s]} | "
                f"{size_selectable_counts[s]} |\n"
            )
        f.write("\n## Branch outcome distribution (all evaluations)\n\n")
        f.write("expected_branch:\n")
        for k in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL, BRANCH_UNSAFE_OPEN, BRANCH_UNKNOWN):
            f.write(f"- {k}: {exp_outcome_counts.get(k, 0)}\n")
        f.write("\npartial_branch:\n")
        for k in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL, BRANCH_UNSAFE_OPEN, BRANCH_UNKNOWN):
            f.write(f"- {k}: {par_outcome_counts.get(k, 0)}\n")
        f.write("\nadverse_branch:\n")
        for k in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL, BRANCH_UNSAFE_OPEN, BRANCH_UNKNOWN):
            f.write(f"- {k}: {adv_outcome_counts.get(k, 0)}\n")
        f.write("\n## Top blockers\n\n")
        for reason, cnt in blocker_counts.most_common(15):
            f.write(f"- {reason}: {cnt}\n")
        if not blocker_counts:
            f.write("- (none)\n")
        f.write("\n## V47B candidate entries\n\n")
        f.write(f"- candidates_count: {len(candidates)}\n")
        f.write(f"- unique_mints: {len(seen_pass_mints)}\n")
        f.write(f"- selected_size distribution:\n")
        for s, c in sorted(selected_size_dist.items()):
            f.write(f"  - {s}: {c}\n")
        if not selected_size_dist:
            f.write("  - (none)\n")
        f.write("\n## Causal observed outcomes\n\n")
        f.write(f"- bank: {bank_count}\n")
        f.write(f"- scratch: {scratch_count}\n")
        f.write(f"- clamp_loss: {clamp_count}\n")
        f.write(f"- expired_loss: {expired_loss_count}\n")
        f.write(f"- neutral: {neutral_count}\n")
        f.write(f"- pending: {pending_count}\n\n")
        f.write("## Verdict\n\n")
        meets_target = bool(len(candidates) >= int(args.target_pass))
        zero_neg = bool(clamp_count == 0 and expired_loss_count == 0)
        zero_la = bool(lookahead_block_count == 0)
        overall = bool(
            meets_target and zero_neg and zero_la
            and all_candidates_adverse_safe
            and all_candidates_guard_feasible
            and all_candidates_in_size_range
        )
        f.write(f"- meets_target_count(>={args.target_pass}): {meets_target} "
                f"({len(candidates)}/{args.target_pass})\n")
        f.write(f"- zero_observed_negative_outcomes: {zero_neg}\n")
        f.write(f"- zero_lookahead_violations: {zero_la}\n")
        f.write(f"- all_candidates_adverse_safe: {all_candidates_adverse_safe}\n")
        f.write(f"- all_candidates_guard_feasible: {all_candidates_guard_feasible}\n")
        f.write(f"- all_candidates_in_size_range: {all_candidates_in_size_range}\n")
        f.write(f"- OVERALL_VERDICT: {'PASS' if overall else 'FAIL'}\n\n")
        f.write("## Per-candidate detail\n\n")
        if candidates:
            f.write(
                "| # | mint | size | exp_pnl | par_pnl | adv_pnl | "
                "exp_tok | adv_tok | guard | obs_kind | obs_pnl | obs_lag |\n"
            )
            f.write(
                "|---|------|------|---------|---------|---------|"
                "---------|---------|-------|----------|---------|---------|\n"
            )
            for i, r in enumerate(candidates, 1):
                f.write(
                    f"| {i} | {_short(r.get('mint',''))} | "
                    f"{float(r.get('selected_size_sol',0.0)):.4f} | "
                    f"{float(r.get('expected_pnl',0.0)):+.6f} | "
                    f"{float(r.get('partial_pnl',0.0)):+.6f} | "
                    f"{float(r.get('adverse_pnl',0.0)):+.6f} | "
                    f"{int(r.get('expected_tokens',0))} | "
                    f"{int(r.get('adverse_tokens',0))} | "
                    f"{int(r.get('final_min_tokens_guard',0))} | "
                    f"{r.get('observed_label_kind','pending') or 'pending'} | "
                    f"{('%+.6f' % float(r.get('observed_label_pnl') or 0.0)) if r.get('observed_label_pnl') is not None else 'n/a'} | "
                    f"{r.get('observed_label_lag_ms','') if r.get('observed_label_lag_ms') is not None else ''} |\n"
                )
        else:
            f.write("- (none)\n")
        f.write("\n## HONEST ASSESSMENT\n\n")
        if len(candidates) == 0:
            if sim_evals_total == 0:
                f.write(
                    "- V47B yielded 0 candidates AND 0 sim evals (no shred "
                    "flow with pending pre-curve buys). Likely cause: low "
                    "raw shred throughput during window, OR no mints had "
                    "pending buys in the 250ms window not yet reflected in "
                    "curve updates.\n"
                )
            else:
                top_blockers = dict(blocker_counts.most_common(5))
                f.write(
                    f"- V47B yielded 0 candidates from {sim_evals_total} "
                    f"sim evals. Top blockers: {top_blockers}.\n"
                )
                # Identify dominant blocker.
                if blocker_counts.most_common(1):
                    dom = blocker_counts.most_common(1)[0][0]
                    if dom == "guard_too_tight":
                        f.write(
                            "- Dominant blocker: guard_too_tight. The "
                            "adverse_tokens are too close to expected_tokens "
                            "(small pending flow impact), so the guard "
                            "would either fail expected or allow adverse.\n"
                        )
                    elif dom == "expected_pnl_below_required":
                        f.write(
                            "- Dominant blocker: expected_pnl_below_required. "
                            "The continuation projection didn't clear the "
                            "fee+priority+scratch floor.\n"
                        )
                    elif "unsafe_open" in dom:
                        f.write(
                            "- Dominant blocker: branch is UNSAFE_OPEN even "
                            "with guard. Adverse path produces a real loss "
                            "exceeding clamp threshold; the simulator marked "
                            "it as a fail correctly.\n"
                        )
        elif clamp_count + expired_loss_count > 0:
            f.write(
                f"- V47B produced {len(candidates)} candidate(s) but "
                f"{clamp_count + expired_loss_count} resulted in losses.\n"
            )
        elif meets_target and zero_neg:
            f.write(
                f"- V47B PASSED: {len(candidates)} candidates, 0 negative "
                f"outcomes, all adverse branches in WIN/SAFE_FAIL, all "
                "guards feasible, all sizes in range. Eligible for dry-live.\n"
            )
        else:
            f.write(
                f"- V47B produced {len(candidates)} candidate(s) without "
                f"negatives but did not reach target ({args.target_pass}).\n"
            )

    log(f"V47B-NO-SEND wrote {md_path}")
    log(
        f"V47B-NO-SEND done elapsed_s={elapsed_s:.1f} "
        f"candidates={len(candidates)} "
        f"banks={bank_count} losses={clamp_count + expired_loss_count} "
        f"pending={pending_count}"
    )
    overall_pass = bool(
        len(candidates) >= args.target_pass
        and clamp_count == 0
        and expired_loss_count == 0
        and lookahead_block_count == 0
        and all_candidates_adverse_safe
        and all_candidates_guard_feasible
        and all_candidates_in_size_range
    )
    return 0 if overall_pass else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
