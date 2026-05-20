"""V47E - Corrected dry-live with V47E two-buyer guard + clean-close +
max_open=2 concurrency cap + post-stop pending resolution.

Forks pgg2_v47d_corrected_dry_live.py and adds:
- V47E two-buyer guard (Phase 2) applied BEFORE V47D boundary guard
- V47E clean-close evaluator (Phase 3) for final pass decision
- max_open_positions=2 concurrency cap (Phase 4): defer opening a new
  entry when open_positions count >= 2
- Post-stop pending resolution: after a stop trigger, continue resolving
  ALL open positions (let them close via exit policy) for up to 5 minutes
  hard cap before exiting.

Target: 10 CLOSED non-negative AND open=0 AND pending=0.
Stop conditions:
  (a) 10 closed non-negative reached AND no open positions AND no pending
  (b) ANY closed negative -> STOP immediately (then drain open positions)
  (c) 35 min elapsed
  (d) failed-buy fee budget exceeded (0.00100 SOL)

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
            f"V47E-DRYLIVE-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Dry-live restricted max size: 0.075 SOL (no 0.100 in dry-live).
SIZE_SWEEP_SOL = (0.005, 0.010, 0.015, 0.020, 0.030, 0.050, 0.075)

DRYLIVE_MAX_SIZE_SOL = 0.075


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _required_profit_for_size(size_sol: float, tx_fee_sol: float) -> float:
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
        default="/root/piggy/data/v47e_drylive_decisions.jsonl",
    )
    ap.add_argument("--max-seconds", type=int, default=2100)  # 35 minutes
    ap.add_argument("--target-non-neg-closes", type=int, default=10)
    ap.add_argument("--max-hot-mints", type=int, default=96)
    ap.add_argument(
        "--failed-buy-fee-budget-sol", type=float, default=0.00100,
    )
    ap.add_argument("--strategy-min-tokens-frac", type=float, default=0.95)
    ap.add_argument("--max-guard-fraction", type=float, default=0.995)
    ap.add_argument("--max-open-positions", type=int, default=2)
    ap.add_argument(
        "--post-stop-drain-seconds", type=int, default=300,
    )
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
        from pgg2_v47c_signer_aware_buffer import (  # type: ignore
            V47CSignerAwareBuffer,
        )
        from pgg2_v47c_multi_buyer_gate import (  # type: ignore
            evaluate_multi_buyer_gate,
        )
        from pgg2_v47c_size_cap import apply_size_cap  # type: ignore
        from pgg2_v47d_boundary_guard import (  # type: ignore
            evaluate_boundary_guard,
        )
        from pgg2_v47d_downsizer import (  # type: ignore
            downsize_candidate,
        )
        from pgg2_v47e_two_buyer_guard import (  # type: ignore
            evaluate_two_buyer_guard,
            MODE_ACTUAL, MODE_SHADOW, MODE_BLOCK, MODE_DELEGATE_V47D,
        )
        from pgg2_v47e_clean_close import (  # type: ignore
            evaluate_clean_close,
        )
    except Exception as exc:
        print(f"V47E-DRYLIVE-ABORT import:{type(exc).__name__}:{exc}")
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
    SAFE_BUY_FAIL_COST = 2.0 * float(DEFAULT_TX_FEE_SOL)
    FEE_BUDGET = float(args.failed_buy_fee_budget_sol)

    log(
        f"V47E-DRYLIVE start sizes={SIZE_SWEEP_SOL} "
        f"max_seconds={args.max_seconds} "
        f"target_non_neg={args.target_non_neg_closes} "
        f"max_open={args.max_open_positions} "
        f"post_stop_drain_s={args.post_stop_drain_seconds} "
        f"failed_buy_budget={FEE_BUDGET:.5f} "
        f"strategy_min_frac={args.strategy_min_tokens_frac} "
        f"bank={BANK_TH} scratch={SCRATCH_TH} clamp={LOSS_TH}"
    )

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    _v46buf = V46PendingFlowBuffer(logger=log, emit_sample_denom=400)
    buffer_ = V47CSignerAwareBuffer(
        _v46buf, logger=log, emit_sample_denom=400,
    )

    # State
    candidates: List[Dict[str, Any]] = []
    raw_buys_seen = 0
    raw_sells_seen = 0
    curve_updates_seen = 0
    sim_evals_total = 0
    snapshots_total = 0
    lookahead_block_count = 0

    boundary_pass = 0
    boundary_block = 0
    downsize_ok = 0
    downsize_fail = 0
    replacement_scans = 0

    # V47E counters
    two_buyer_total = 0
    two_buyer_actual = 0
    two_buyer_shadow = 0
    two_buyer_block = 0
    two_buyer_delegate = 0
    two_buyer_reason_counts: Counter = Counter()
    max_open_skips = 0

    failed_buy_count = 0
    failed_buy_fees_total = 0.0
    non_neg_closes = 0
    negative_closes = 0
    net_pnl_sol = 0.0
    max_loss_sol = 0.0
    selected_size_dist: Counter = Counter()

    # V47E concurrency tracking
    max_open = int(args.max_open_positions)
    open_positions: Dict[Any, Dict[str, Any]] = {}  # key -> rec

    hot_mint_last_seen: Dict[str, int] = {}
    seen_curve_ts: Dict[str, int] = {}
    out_jsonl_path = Path(args.out_jsonl)
    out_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    jsonl_fp = open(str(out_jsonl_path), "w", encoding="utf-8")
    pending_candidates: Dict[Tuple[str, int], Dict[str, Any]] = {}
    shred_stop = asyncio.Event()
    stop_reason = "running"

    # ---- Helper functions (similar to capture) ------------------------
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
            fee_bps, creator_fee_bps,
        )
        return cs, int(cs_pt.ts_ms)

    def _evaluate_size_for_mint(
        cs, size_sol, pending_buys, pending_sells, tx_fee_sol,
    ) -> Dict[str, Any]:
        r0 = simulate_branches(
            cs, size_sol, pending_buys, pending_sells, exec_delay_ms=250,
        )
        exp_t = int(r0["expected_tokens"])
        adv_t = int(r0["adverse_tokens"])
        if exp_t <= 0:
            return {"selectable": False, "blocker": "expected_tokens_zero",
                    "size_sol": float(size_sol), "r0": r0, "r1": None,
                    "guard": None, "required_profit": 0.0}
        strategy_min = int(exp_t * float(args.strategy_min_tokens_frac))
        g = compute_guard_for_adverse_fail_or_profit(
            expected_tokens=exp_t, adverse_tokens=adv_t,
            min_tokens_for_nonnegative_exit=0,
            strategy_min_tokens=strategy_min,
            max_guard_fraction=float(args.max_guard_fraction),
        )
        if not g["pass"]:
            return {"selectable": False, "blocker": "guard_too_tight",
                    "size_sol": float(size_sol), "r0": r0, "r1": None,
                    "guard": g, "required_profit": 0.0}
        r1 = simulate_branches(
            cs, size_sol, pending_buys, pending_sells, exec_delay_ms=250,
            guard_min_tokens=int(g["final_min_tokens"]),
        )
        req = _required_profit_for_size(size_sol, tx_fee_sol)
        if not r1["pass"]:
            return {"selectable": False,
                    "blocker": f"branch_check:{r1['blocker']}",
                    "size_sol": float(size_sol), "r0": r0, "r1": r1,
                    "guard": g, "required_profit": float(req)}
        if float(r1["expected_pnl"]) < float(req):
            return {"selectable": False,
                    "blocker": "expected_pnl_below_required",
                    "size_sol": float(size_sol), "r0": r0, "r1": r1,
                    "guard": g, "required_profit": float(req)}
        return {"selectable": True, "blocker": None,
                "size_sol": float(size_sol), "r0": r0, "r1": r1,
                "guard": g, "required_profit": float(req)}

    def _maybe_evaluate(mint, ts_ms_now, slot, sol_in, signer):
        nonlocal sim_evals_total, lookahead_block_count
        nonlocal boundary_pass, boundary_block, downsize_ok, downsize_fail
        nonlocal replacement_scans
        nonlocal two_buyer_total, two_buyer_actual, two_buyer_shadow
        nonlocal two_buyer_block, two_buyer_delegate, max_open_skips
        if non_neg_closes >= args.target_non_neg_closes:
            return
        if failed_buy_fees_total >= FEE_BUDGET:
            return
        # V47E max_open concurrency cap (Phase 4): pre-decision check.
        if len(open_positions) >= max_open:
            max_open_skips += 1
            log(
                f"PGG2-V47E-CONCURRENCY-CAP open_now={len(open_positions)} "
                f"max_open={max_open} action=skip_entry_max_open "
                f"mint={_short(mint)}"
            )
            return
        cs, cu_ts = _curve_state_at_or_before(mint, ts_ms_now)
        if cs is None:
            return
        snap = buffer_.buyer_stats(mint, ts_ms_now, cu_ts)
        if int(snap.get("latest_raw_buy_ts_ms", 0)) > ts_ms_now:
            lookahead_block_count += 1
            return
        if int(snap.get("latest_curve_update_ts_ms", 0)) > ts_ms_now:
            lookahead_block_count += 1
            return
        source_lead_ms = float(int(ts_ms_now) - int(cu_ts)) if cu_ts > 0 else 0.0
        pending_buys = buffer_.pending_buys(mint, ts_ms_now, cu_ts, 250)
        pending_sells = buffer_.pending_sells(mint, ts_ms_now, cu_ts, 250)
        if not pending_buys and not pending_sells:
            return

        buyer_stats_for_gate = {
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "pending_buy_count_250ms": int(
                snap.get("pending_buy_count_250ms", 0)),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)),
            "pending_sell_sol_250ms": float(
                snap.get("pending_sell_sol_250ms", 0.0)),
            "top_buyer_share_250ms": float(
                snap.get("top_buyer_share_250ms", 0.0)),
        }
        mb_pass, mb_blocker = evaluate_multi_buyer_gate(
            buyer_stats_for_gate, logger=log, mint_for_log=mint,
        )
        if not mb_pass:
            return
        sim_evals_total += 1

        # Dry-live caps size at 0.075.
        size_results: Dict[float, Dict[str, Any]] = {}
        for size in SIZE_SWEEP_SOL:
            res = _evaluate_size_for_mint(
                cs, size, pending_buys, pending_sells,
                float(DEFAULT_TX_FEE_SOL),
            )
            size_results[size] = res
        selectable_sizes = [
            s for s, r in size_results.items() if r["selectable"]
        ]
        if not selectable_sizes:
            return
        # Apply V47C size cap.
        buyer_stats_for_cap = {
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "unique_buyers_500ms": int(snap.get("unique_buyers_500ms", 0)),
            "top_buyer_share_250ms": float(
                snap.get("top_buyer_share_250ms", 0.0)),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)),
        }
        size_cap_pass = {}
        for s in selectable_sizes:
            capped, _ = apply_size_cap(
                float(s), buyer_stats_for_cap,
                float(snap.get("pending_buy_sol_250ms", 0.0)),
            )
            size_cap_pass[s] = capped
        admissible = [
            s for s in selectable_sizes
            if size_cap_pass[s] is not None
            and abs(size_cap_pass[s] - s) < 1e-9
        ]
        # Dry-live further restriction: drop sizes > DRYLIVE_MAX_SIZE_SOL.
        admissible = [
            s for s in admissible if s <= DRYLIVE_MAX_SIZE_SOL + 1e-9
        ]
        if not admissible:
            return
        selected_size = min(admissible)
        res = size_results[selected_size]
        r1 = res["r1"]
        g = res["guard"]

        # V47D boundary guard.
        buyer_stats_for_v47d = {
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "unique_buyers_500ms": int(snap.get("unique_buyers_500ms", 0)),
            "pending_buy_count_250ms": int(
                snap.get("pending_buy_count_250ms", 0)),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)),
            "pending_sell_sol_250ms": float(
                snap.get("pending_sell_sol_250ms", 0.0)),
            "top_buyer_share_250ms": float(
                snap.get("top_buyer_share_250ms", 0.0)),
            "largest_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)),
        }
        adv_branch = str(r1.get("adverse_branch_outcome", "") or "")

        # V47E Phase 2: Two-buyer guard (BEFORE V47D boundary for ub<=2).
        two_buyer_total += 1
        tb_mode, tb_reason = evaluate_two_buyer_guard(
            size_sol=float(selected_size),
            buyer_stats=buyer_stats_for_v47d,
            expected_pnl=float(r1["expected_pnl"]),
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=adv_branch,
            logger=log, mint_for_log=mint,
        )
        two_buyer_reason_counts[(tb_mode, tb_reason)] += 1
        if tb_mode == MODE_BLOCK:
            two_buyer_block += 1
            replacement_scans += 1
            log(
                f"PGG2-V47E-REPLACEMENT-SCAN ts={ts_ms_now} "
                f"reason=v47e_two_buyer_block mint={_short(mint)} "
                f"size={float(selected_size):.4f} reason={tb_reason}"
            )
            return
        if tb_mode == MODE_SHADOW:
            two_buyer_shadow += 1
            # shadow_only: do not open in dry-live.
            log(
                f"PGG2-V47E-SHADOW-ONLY mint={_short(mint)} "
                f"size={float(selected_size):.4f} reason={tb_reason}"
            )
            return
        if tb_mode == MODE_ACTUAL:
            two_buyer_actual += 1
        elif tb_mode == MODE_DELEGATE_V47D:
            two_buyer_delegate += 1

        bg_pass, bg_blocker = evaluate_boundary_guard(
            size_sol=float(selected_size),
            buyer_stats=buyer_stats_for_v47d,
            expected_pnl=float(r1["expected_pnl"]),
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=adv_branch,
        )

        original_size = float(selected_size)
        downsized_bool = False
        final_size = selected_size
        final_res = res
        final_r1 = r1
        final_g = g

        if not bg_pass:
            boundary_block += 1

            def _epfn(sz):
                r = size_results.get(float(sz))
                if r is None: return 0.0
                r1_at = r.get("r1")
                if r1_at is not None:
                    return float(r1_at.get("expected_pnl", 0.0))
                return float(r.get("r0", {}).get("expected_pnl", 0.0))

            def _bfn(sz):
                r = size_results.get(float(sz))
                if r is None: return (False, "size_not_swept")
                return (
                    bool(r.get("selectable", False)),
                    r.get("blocker") if not r.get("selectable") else None,
                )

            d_size, d_action, d_reason = downsize_candidate(
                initial_selected_size=original_size,
                buyer_stats=buyer_stats_for_v47d,
                expected_pnl_fn=_epfn,
                no_negative_curve_update_250ms=True,
                adverse_branch_outcome=adv_branch,
                branch_check_fn=_bfn,
                multi_buyer_pass=True,
            )
            if d_size is None:
                downsize_fail += 1
                replacement_scans += 1
                return
            downsize_ok += 1
            downsized_bool = abs(d_size - original_size) > 1e-9
            final_size = float(d_size)
            final_res = size_results[final_size]
            final_r1 = final_res["r1"]
            final_g = final_res["guard"]
        else:
            boundary_pass += 1

        # V47E re-check: after potential downsize, the max_open cap still applies.
        if len(open_positions) >= max_open:
            max_open_skips += 1
            log(
                f"PGG2-V47E-CONCURRENCY-CAP open_now={len(open_positions)} "
                f"max_open={max_open} action=skip_entry_max_open "
                f"mint={_short(mint)} post_size_resolution=1"
            )
            return

        # Build entry record (a real "buy intent" - simulated).
        entry_idx = len(candidates) + 1
        rec = {
            "type": "v47e_drylive_entry",
            "entry_idx": int(entry_idx),
            "decision_ts_ms": int(ts_ms_now),
            "mint": mint,
            "slot": int(slot),
            "selected_size_sol": float(final_size),
            "original_size_sol": float(original_size),
            "downsized": bool(downsized_bool),
            "ub_250": int(snap.get("unique_buyers_250ms", 0)),
            "tbs_250": float(snap.get("top_buyer_share_250ms", 0.0)),
            "tb_mode": str(tb_mode),
            "tb_reason": str(tb_reason),
            "adverse_branch": str(final_r1["adverse_branch_outcome"] or ""),
            "exp_pnl": float(final_r1["expected_pnl"]),
            "adv_pnl": float(final_r1["adverse_pnl"]),
            "adv_branch": str(final_r1["adverse_branch_outcome"] or ""),
            "expected_tokens": int(final_r1["expected_tokens"]),
            "guard_min_tokens": int(final_g["final_min_tokens"]),
            "close_kind": None,
            "close_pnl": None,
            "close_lag_ms": None,
            "is_failed_buy": False,
            "opened_or_deferred": "opened",
        }
        candidates.append(rec)
        selected_size_dist[f"{final_size:.4f}"] += 1
        position_key = (mint, int(ts_ms_now))
        pending_candidates[position_key] = rec
        open_positions[position_key] = rec
        jsonl_fp.write(json.dumps(rec) + "\n")
        jsonl_fp.flush()
        log(
            f"V47E-DRYLIVE-ENTRY mint={_short(mint)} size={final_size:.4f} "
            f"orig={original_size:.4f} downsized={int(downsized_bool)} "
            f"ub={rec['ub_250']} tbs={rec['tbs_250']:.3f} "
            f"tb_mode={tb_mode} "
            f"exp_pnl={final_r1['expected_pnl']:+.6f} "
            f"open_now={len(open_positions)}/{max_open} "
            f"non_neg_closes={non_neg_closes}/{args.target_non_neg_closes}"
        )

    async def _shred_listener():
        nonlocal raw_buys_seen, raw_sells_seen
        try:
            import websockets  # type: ignore
        except Exception:
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            return
        backoff = 2.0
        while not shred_stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=60,
                    max_queue=4096, max_size=8 * 1024 * 1024,
                ) as ws:
                    backoff = 2.0
                    sub = {"jsonrpc": "2.0", "id": 92347,
                           "method": "shredSubscribe",
                           "params": [
                               {"accountInclude": [PUMP_PROGRAM],
                                "accountRequired": [PUMP_PROGRAM],
                                "vote": False},
                               {"encoding": "base64",
                                "transactionDetails": "full",
                                "maxSupportedTransactionVersion": 0},
                           ]}
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        if shred_stop.is_set(): break
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        if "shred" not in str(
                            data.get("method") or "",
                        ).lower():
                            continue
                        result = (
                            (data.get("params") or {}).get("result") or {}
                        )
                        try:
                            events_ = list(
                                parse_base64_shred_for_pump_events(
                                    result, set(),
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
                    if p.error: continue
                    buffer_.mark_curve_update(mint, int(p.ts_ms))
                    curve_updates_seen += 1
                    snapshots_total += 1
                    cs_now = curve_state_from_subscriber_point(
                        int(p.virtual_sol_reserves),
                        int(p.virtual_token_reserves),
                        int(p.real_token_reserves),
                        fee_bps, creator_fee_bps,
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
                        pnl_now = (sell_sol_now - float(size_sol)
                                   - 2.0 * float(DEFAULT_TX_FEE_SOL))
                        lag = int(p.ts_ms) - int(dts)
                        if pnl_now >= BANK_TH:
                            rec["close_kind"] = "bank"
                            rec["close_pnl"] = float(pnl_now)
                            rec["close_lag_ms"] = int(lag)
                            non_neg_closes += 1
                            net_pnl_sol += float(pnl_now)
                            jsonl_fp.write(json.dumps({
                                "type": "v47e_drylive_close",
                                **{k: rec[k] for k in (
                                    "mint", "decision_ts_ms",
                                    "selected_size_sol",
                                    "close_pnl", "close_kind",
                                    "close_lag_ms",
                                )}, "ts_ms": int(p.ts_ms),
                            }) + "\n")
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            open_positions.pop(key, None)
                            log(
                                f"V47E-DRYLIVE-CLOSE bank pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)} "
                                f"non_neg={non_neg_closes} "
                                f"open_now={len(open_positions)}"
                            )
                            continue
                        if pnl_now <= LOSS_TH:
                            rec["close_kind"] = "clamp_loss"
                            rec["close_pnl"] = float(pnl_now)
                            rec["close_lag_ms"] = int(lag)
                            negative_closes += 1
                            net_pnl_sol += float(pnl_now)
                            if pnl_now < max_loss_sol:
                                max_loss_sol = float(pnl_now)
                            jsonl_fp.write(json.dumps({
                                "type": "v47e_drylive_close",
                                **{k: rec[k] for k in (
                                    "mint", "decision_ts_ms",
                                    "selected_size_sol",
                                    "close_pnl", "close_kind",
                                    "close_lag_ms",
                                )}, "ts_ms": int(p.ts_ms),
                            }) + "\n")
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            open_positions.pop(key, None)
                            log(
                                f"V47E-DRYLIVE-CLOSE clamp_loss "
                                f"pnl={pnl_now:+.6f} lag={lag}ms "
                                f"mint={_short(mint)} "
                                f"neg={negative_closes} "
                                f"open_now={len(open_positions)}"
                            )
                            # Trigger early exit by setting deadline.
                            stop_reason = "negative_close"
                            break
                        hold = MAX_HOLD
                        if EXTEND_IF_POS and pnl_now > 0:
                            hold = MAX_EXTEND_MS
                        if lag >= hold:
                            label = (
                                "scratch" if abs(pnl_now) < SCRATCH_TH
                                else ("neutral" if pnl_now > 0
                                      else "expired_loss")
                            )
                            rec["close_kind"] = label
                            rec["close_pnl"] = float(pnl_now)
                            rec["close_lag_ms"] = int(lag)
                            if label == "expired_loss":
                                negative_closes += 1
                                net_pnl_sol += float(pnl_now)
                                if pnl_now < max_loss_sol:
                                    max_loss_sol = float(pnl_now)
                                stop_reason = "negative_close"
                            else:
                                non_neg_closes += 1
                                net_pnl_sol += float(pnl_now)
                            jsonl_fp.write(json.dumps({
                                "type": "v47e_drylive_close",
                                **{k: rec[k] for k in (
                                    "mint", "decision_ts_ms",
                                    "selected_size_sol",
                                    "close_pnl", "close_kind",
                                    "close_lag_ms",
                                )}, "ts_ms": int(p.ts_ms),
                            }) + "\n")
                            jsonl_fp.flush()
                            pending_candidates.pop(key, None)
                            open_positions.pop(key, None)
                            log(
                                f"V47E-DRYLIVE-CLOSE {label} "
                                f"pnl={pnl_now:+.6f} lag={lag}ms "
                                f"mint={_short(mint)} "
                                f"open_now={len(open_positions)}"
                            )

            stop_trigger = None
            if negative_closes > 0:
                stop_trigger = "negative_close"
            elif (
                non_neg_closes >= args.target_non_neg_closes
                and not open_positions
                and not pending_candidates
            ):
                stop_trigger = "target_reached_clean"
            elif failed_buy_fees_total >= FEE_BUDGET:
                stop_trigger = "fee_budget_exceeded"

            if stop_trigger is not None:
                stop_reason = stop_trigger
                log(
                    f"V47E-DRYLIVE stop_trigger={stop_trigger} "
                    f"open_now={len(open_positions)} "
                    f"pending_now={len(pending_candidates)} "
                    f"entering_drain_max_s={args.post_stop_drain_seconds}"
                )
                # Post-stop drain: keep resolving open positions for up to
                # post_stop_drain_seconds. Stop accepting new entries.
                drain_deadline_ms = now_ts + (
                    int(args.post_stop_drain_seconds) * 1000
                )
                # Replace per-tick exit: continue the outer while loop until
                # open_positions == 0 OR drain deadline.
                while (
                    _now_ms() < drain_deadline_ms
                    and (open_positions or pending_candidates)
                ):
                    await asyncio.sleep(0.05)
                    drain_now = _now_ms()
                    for mint_drain in list(hot_mint_last_seen.keys())[
                        : args.max_hot_mints
                    ]:
                        st_d = oracle._states.get(mint_drain)
                        if st_d is None or not st_d.points:
                            continue
                        last_t = seen_curve_ts.get(mint_drain, 0)
                        new_pts = []
                        for p in st_d.points:
                            if int(p.ts_ms) > int(last_t):
                                new_pts.append(p)
                        if not new_pts:
                            continue
                        new_pts.sort(key=lambda x: x.ts_ms)
                        for p in new_pts:
                            seen_curve_ts[mint_drain] = int(p.ts_ms)
                            if p.error: continue
                            buffer_.mark_curve_update(
                                mint_drain, int(p.ts_ms)
                            )
                            curve_updates_seen += 1
                            snapshots_total += 1
                            cs_d = curve_state_from_subscriber_point(
                                int(p.virtual_sol_reserves),
                                int(p.virtual_token_reserves),
                                int(p.real_token_reserves),
                                fee_bps, creator_fee_bps,
                            )
                            for key_d, rec_d in list(
                                pending_candidates.items()
                            ):
                                m_d, dts_d = key_d
                                if m_d != mint_drain:
                                    continue
                                if int(p.ts_ms) <= int(dts_d):
                                    continue
                                tok_d = int(rec_d.get("expected_tokens", 0))
                                size_d = float(
                                    rec_d.get("selected_size_sol", 0.0)
                                )
                                if tok_d <= 0:
                                    sell_lams_d = 0
                                else:
                                    sell_lams_d, _ = local_sell_quote_sol(
                                        cs_d, int(tok_d)
                                    )
                                sell_sol_d = (
                                    float(sell_lams_d)
                                    / float(LAMPORTS_PER_SOL)
                                )
                                pnl_d = (
                                    sell_sol_d - float(size_d)
                                    - 2.0 * float(DEFAULT_TX_FEE_SOL)
                                )
                                lag_d = int(p.ts_ms) - int(dts_d)
                                if pnl_d >= BANK_TH:
                                    rec_d["close_kind"] = "bank"
                                    rec_d["close_pnl"] = float(pnl_d)
                                    rec_d["close_lag_ms"] = int(lag_d)
                                    non_neg_closes += 1
                                    net_pnl_sol += float(pnl_d)
                                    jsonl_fp.write(json.dumps({
                                        "type": "v47e_drylive_close",
                                        **{k: rec_d[k] for k in (
                                            "mint", "decision_ts_ms",
                                            "selected_size_sol",
                                            "close_pnl", "close_kind",
                                            "close_lag_ms",
                                        )}, "ts_ms": int(p.ts_ms),
                                    }) + "\n")
                                    jsonl_fp.flush()
                                    pending_candidates.pop(key_d, None)
                                    open_positions.pop(key_d, None)
                                    log(
                                        f"V47E-DRYLIVE-DRAIN-CLOSE bank "
                                        f"pnl={pnl_d:+.6f} lag={lag_d}ms "
                                        f"mint={_short(mint_drain)} "
                                        f"non_neg={non_neg_closes} "
                                        f"open_now={len(open_positions)}"
                                    )
                                    continue
                                if pnl_d <= LOSS_TH:
                                    rec_d["close_kind"] = "clamp_loss"
                                    rec_d["close_pnl"] = float(pnl_d)
                                    rec_d["close_lag_ms"] = int(lag_d)
                                    negative_closes += 1
                                    net_pnl_sol += float(pnl_d)
                                    if pnl_d < max_loss_sol:
                                        max_loss_sol = float(pnl_d)
                                    jsonl_fp.write(json.dumps({
                                        "type": "v47e_drylive_close",
                                        **{k: rec_d[k] for k in (
                                            "mint", "decision_ts_ms",
                                            "selected_size_sol",
                                            "close_pnl", "close_kind",
                                            "close_lag_ms",
                                        )}, "ts_ms": int(p.ts_ms),
                                    }) + "\n")
                                    jsonl_fp.flush()
                                    pending_candidates.pop(key_d, None)
                                    open_positions.pop(key_d, None)
                                    log(
                                        f"V47E-DRYLIVE-DRAIN-CLOSE clamp_loss "
                                        f"pnl={pnl_d:+.6f} lag={lag_d}ms "
                                        f"mint={_short(mint_drain)} "
                                        f"open_now={len(open_positions)}"
                                    )
                                    continue
                                hold_d = MAX_HOLD
                                if EXTEND_IF_POS and pnl_d > 0:
                                    hold_d = MAX_EXTEND_MS
                                if lag_d >= hold_d:
                                    label_d = (
                                        "scratch" if abs(pnl_d) < SCRATCH_TH
                                        else ("neutral" if pnl_d > 0
                                              else "expired_loss")
                                    )
                                    rec_d["close_kind"] = label_d
                                    rec_d["close_pnl"] = float(pnl_d)
                                    rec_d["close_lag_ms"] = int(lag_d)
                                    if label_d == "expired_loss":
                                        negative_closes += 1
                                        net_pnl_sol += float(pnl_d)
                                        if pnl_d < max_loss_sol:
                                            max_loss_sol = float(pnl_d)
                                    else:
                                        non_neg_closes += 1
                                        net_pnl_sol += float(pnl_d)
                                    jsonl_fp.write(json.dumps({
                                        "type": "v47e_drylive_close",
                                        **{k: rec_d[k] for k in (
                                            "mint", "decision_ts_ms",
                                            "selected_size_sol",
                                            "close_pnl", "close_kind",
                                            "close_lag_ms",
                                        )}, "ts_ms": int(p.ts_ms),
                                    }) + "\n")
                                    jsonl_fp.flush()
                                    pending_candidates.pop(key_d, None)
                                    open_positions.pop(key_d, None)
                                    log(
                                        f"V47E-DRYLIVE-DRAIN-CLOSE {label_d} "
                                        f"pnl={pnl_d:+.6f} lag={lag_d}ms "
                                        f"mint={_short(mint_drain)} "
                                        f"open_now={len(open_positions)}"
                                    )
                    if drain_now - now_ts > 30_000:
                        log(
                            f"V47E-DRYLIVE drain_progress "
                            f"elapsed_drain_s="
                            f"{(drain_now - now_ts)/1000.0:.0f} "
                            f"open={len(open_positions)} "
                            f"pending={len(pending_candidates)} "
                            f"non_neg={non_neg_closes} "
                            f"neg={negative_closes}"
                        )
                        now_ts = drain_now
                log(
                    f"V47E-DRYLIVE drain_complete "
                    f"final_open={len(open_positions)} "
                    f"final_pending={len(pending_candidates)} "
                    f"non_neg={non_neg_closes} neg={negative_closes}"
                )
                break

            if now_ts >= next_progress_ms:
                log(
                    f"V47E-DRYLIVE progress "
                    f"elapsed_s={(now_ts - t_start_wall)/1000.0:.0f} "
                    f"entries={len(candidates)} "
                    f"non_neg_closes={non_neg_closes} "
                    f"neg_closes={negative_closes} "
                    f"open={len(open_positions)} "
                    f"pending={len(pending_candidates)} "
                    f"failed_buys={failed_buy_count} "
                    f"failed_buy_fees={failed_buy_fees_total:.5f} "
                    f"net_pnl={net_pnl_sol:+.6f}"
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

    elapsed_s = (_now_ms() - t_start_wall) / 1000.0
    budget_respected = failed_buy_fees_total <= FEE_BUDGET

    # Final open / pending counts.
    final_open = len(open_positions)
    final_pending = 0  # In drylive we collapse: anything still in
    # pending_candidates that wasn't closed counts as pending. open_positions
    # is a subset of pending_candidates.
    for key, rec in pending_candidates.items():
        if rec.get("close_kind") is None:
            final_pending += 1

    cc = evaluate_clean_close(
        entries=len(candidates),
        closed_nonneg=non_neg_closes,
        closed_neg=negative_closes,
        pending=final_pending,
        open_positions=final_open,
        target_closed_nonneg=int(args.target_non_neg_closes),
        logger=log,
    )

    overall_pass = bool(
        cc["pass"]
        and budget_respected
        and net_pnl_sol > 0.0
        and elapsed_s <= args.max_seconds + args.post_stop_drain_seconds + 1
    )

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V47E Corrected Dry-Live Result\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- wall_clock_s: {elapsed_s:.1f}\n")
        f.write(f"- stop_reason: {stop_reason}\n")
        f.write(
            f"- target_non_neg_closes: {args.target_non_neg_closes}\n"
        )
        f.write(f"- max_seconds: {args.max_seconds}\n")
        f.write(f"- max_open_positions: {max_open}\n")
        f.write(f"- post_stop_drain_seconds: {args.post_stop_drain_seconds}\n")
        f.write(f"- failed_buy_fee_budget_sol: {FEE_BUDGET:.5f}\n\n")

        f.write("## Engine sanity\n\n")
        f.write(f"- raw_pump_buys: {raw_buys_seen}\n")
        f.write(f"- raw_pump_sells: {raw_sells_seen}\n")
        f.write(f"- curve_updates: {curve_updates_seen}\n")
        f.write(f"- sim_evaluations: {sim_evals_total}\n")
        f.write(f"- lookahead_blocks: {lookahead_block_count}\n\n")

        f.write("## V47E two-buyer guard stats\n\n")
        f.write(f"- evaluations: {two_buyer_total}\n")
        f.write(f"- actual_pass: {two_buyer_actual}\n")
        f.write(f"- shadow_only: {two_buyer_shadow}\n")
        f.write(f"- block: {two_buyer_block}\n")
        f.write(f"- delegate_v47d (ub>=3): {two_buyer_delegate}\n")
        f.write("- (mode,reason) breakdown:\n")
        if two_buyer_reason_counts:
            for (m, r), c in two_buyer_reason_counts.most_common(20):
                f.write(f"  - {m} / {r}: {c}\n")
        else:
            f.write("  - (none)\n")
        f.write(f"- max_open_skips: {max_open_skips}\n\n")

        f.write("## V47D boundary guard / downsize / replacement\n\n")
        f.write(f"- boundary_guard_pass: {boundary_pass}\n")
        f.write(f"- boundary_guard_block: {boundary_block}\n")
        f.write(f"- downsize_ok: {downsize_ok}\n")
        f.write(f"- downsize_fail: {downsize_fail}\n")
        f.write(f"- replacement_scans: {replacement_scans}\n\n")

        f.write("## Outcome counts\n\n")
        f.write(f"- entries_opened: {len(candidates)}\n")
        f.write(f"- non_negative_closes: {non_neg_closes}\n")
        f.write(f"- negative_closes: {negative_closes}\n")
        f.write(f"- pending_at_stop: {final_pending}\n")
        f.write(f"- open_at_stop: {final_open}\n")
        f.write(f"- final_open_positions: {final_open}\n")
        f.write(f"- max_open_skips: {max_open_skips}\n")
        f.write(f"- failed_buys: {failed_buy_count}\n")
        f.write(f"- failed_buy_fees_total: {failed_buy_fees_total:.5f}\n")
        f.write(f"- budget_respected: {budget_respected}\n")
        f.write(f"- net_pnl_sol: {net_pnl_sol:+.6f}\n")
        f.write(f"- max_loss_sol: {max_loss_sol:+.6f}\n\n")

        f.write("## V47E clean-close status\n\n")
        f.write(f"- entries: {cc['entries']}\n")
        f.write(f"- closed_nonneg: {cc['closed_nonneg']}\n")
        f.write(f"- closed_neg: {cc['closed_neg']}\n")
        f.write(f"- pending: {cc['pending']}\n")
        f.write(f"- open_positions: {cc['open_positions']}\n")
        f.write(f"- target_closed_nonneg: {cc['target_closed_nonneg']}\n")
        f.write(f"- pass: {cc['pass']}\n")
        f.write(f"- fail_reason: {cc['fail_reason']}\n\n")

        f.write("## Selected size distribution\n\n")
        for s, c in sorted(selected_size_dist.items()):
            f.write(f"- {s}: {c}\n")
        if not selected_size_dist:
            f.write("- (none)\n")

        f.write("\n## Per-entry table\n\n")
        f.write(
            "| # | ts | mint | size | orig_size | downsized | ub | tbs | "
            "adv_outcome | opened_or_deferred | "
            "exp_pnl | close_kind | close_pnl | close_lag |\n"
            "|---|----|------|------|-----------|-----------|----|-----|"
            "------------|--------------------|"
            "---------|------------|-----------|-----------|\n"
        )
        for i, r in enumerate(candidates, 1):
            cp = r.get("close_pnl")
            f.write(
                f"| {i} | {int(r.get('decision_ts_ms', 0))} | "
                f"{_short(r.get('mint',''))} | "
                f"{float(r.get('selected_size_sol',0.0)):.4f} | "
                f"{float(r.get('original_size_sol',0.0)):.4f} | "
                f"{int(bool(r.get('downsized', False)))} | "
                f"{int(r.get('ub_250', 0))} | "
                f"{float(r.get('tbs_250', 0.0)):.3f} | "
                f"{r.get('adverse_branch','-')} | "
                f"{r.get('opened_or_deferred','opened')} | "
                f"{float(r.get('exp_pnl', 0.0)):+.6f} | "
                f"{r.get('close_kind') or 'pending'} | "
                f"{('%+.6f' % cp) if cp is not None else 'n/a'} | "
                f"{r.get('close_lag_ms') if r.get('close_lag_ms') is not None else '-'} |\n"
            )
        if not candidates:
            f.write("- (no entries)\n")

        f.write("\n## PASS Criteria\n\n")
        f.write(
            f"- closed_nonneg >= {args.target_non_neg_closes}: "
            f"{'YES' if non_neg_closes >= args.target_non_neg_closes else 'NO'} "
            f"({non_neg_closes}/{args.target_non_neg_closes})\n"
        )
        f.write(
            f"- 0 negative_closes: "
            f"{'YES' if negative_closes == 0 else 'NO'} "
            f"({negative_closes})\n"
        )
        f.write(
            f"- 0 open_positions at end: "
            f"{'YES' if final_open == 0 else 'NO'} "
            f"({final_open})\n"
        )
        f.write(
            f"- 0 pending_positions at end: "
            f"{'YES' if final_pending == 0 else 'NO'} "
            f"({final_pending})\n"
        )
        f.write(
            f"- clean_close_pass: {cc['pass']}\n"
        )
        f.write(
            f"- budget_respected (<= {FEE_BUDGET:.5f}): "
            f"{'YES' if budget_respected else 'NO'}\n"
        )
        f.write(
            f"- net_pnl > 0: "
            f"{'YES' if net_pnl_sol > 0 else 'NO'} "
            f"(net={net_pnl_sol:+.6f})\n"
        )
        f.write(
            f"\n## OVERALL_VERDICT: "
            f"{'PASS' if overall_pass else 'FAIL'}\n"
        )

    log(f"V47E-DRYLIVE wrote {md_path}")
    log(
        f"V47E-DRYLIVE done elapsed_s={elapsed_s:.1f} "
        f"non_neg={non_neg_closes} neg={negative_closes} "
        f"open={final_open} pending={final_pending} "
        f"net={net_pnl_sol:+.6f} stop_reason={stop_reason} "
        f"clean_close_pass={cc['pass']} "
        f"pass={int(overall_pass)}"
    )
    return 0 if overall_pass else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
