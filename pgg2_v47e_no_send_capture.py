"""V47E - No-Send capture with V47E two-buyer guard + V47D boundary guard +
downsize + replacement router + clean-close evaluator.

Forks pgg2_v47d_no_send_capture.py and adds:
  - V47E two-buyer guard (Phase 2): applied BEFORE the V47D boundary guard
    for ub_250 <= 2 cases; delegates to V47D for ub_250 >= 3
  - V47E clean-close evaluator (Phase 3): called at end-of-run to determine
    pass status: closed_nonneg >= target AND closed_neg == 0 AND pending == 0
    AND open_positions == 0 (open_positions is structurally 0 in no-send)

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
            f"V47E-NO-SEND-ABORT forbidden_call_pattern={_pat}\n"
        )
        sys.exit(2)


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Adaptive size sweep. 0.100 is no-send only; dry-live picks max 0.075.
SIZE_SWEEP_SOL = (0.005, 0.010, 0.015, 0.020, 0.030, 0.050, 0.075, 0.100)
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
        default="/root/piggy/data/v47e_no_send_decisions.jsonl",
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
        print(f"V47E-NO-SEND-ABORT import:{type(exc).__name__}:{exc}")
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
        f"V47E-NO-SEND start sizes={SIZE_SWEEP_SOL} "
        f"max_seconds={args.max_seconds} target_pass={args.target_pass} "
        f"strategy_min_frac={args.strategy_min_tokens_frac} "
        f"max_guard_frac={args.max_guard_fraction} "
        f"bank={BANK_TH} scratch={SCRATCH_TH} clamp={LOSS_TH} "
        f"max_hold={MAX_HOLD}"
    )
    for env_name in (
        "PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE",
        "V47E_NO_SEND",
        "PGG2_V47E_TWO_BUYER_CONCENTRATION_GUARD",
        "PGG2_V47E_REQUIRE_CLEAN_CLOSE_PASS",
    ):
        if os.environ.get(env_name, "0") != "1":
            log(f"V47E-NO-SEND WARNING: {env_name} env not 1")

    cfg = BotConfig()
    broker = DirectPumpQuoteBroker(cfg)
    pg = broker.pump_global()
    fee_bps = int(pg.fee_bps)
    creator_fee_bps = int(pg.creator_fee_bps)
    log(
        f"V47E-NO-SEND fee_bps={fee_bps} creator_fee_bps={creator_fee_bps}"
    )

    oracle = CurveAccountSubscriberOracle(broker=broker, logger=log)
    await oracle.start()

    _v46buf = V46PendingFlowBuffer(logger=log, emit_sample_denom=400)
    buffer_ = V47CSignerAwareBuffer(
        _v46buf, logger=log, emit_sample_denom=400,
    )

    # Counters.
    mb_gate_total = 0
    mb_gate_pass_total = 0
    mb_gate_blocker_counts: Counter = Counter()
    size_cap_block_counts: Counter = Counter()
    size_cap_applied_count = 0

    # V47D-specific counters.
    boundary_guard_total = 0
    boundary_guard_pass_total = 0
    boundary_guard_blocker_counts: Counter = Counter()
    downsize_attempt_count = 0
    downsize_success_count = 0
    downsize_fail_count = 0
    replacement_scan_count = 0
    replacement_selected_count = 0

    # V47E-specific counters.
    two_buyer_total = 0
    two_buyer_actual = 0
    two_buyer_shadow = 0
    two_buyer_block = 0
    two_buyer_delegate = 0
    two_buyer_reason_counts: Counter = Counter()
    shadow_candidates: List[Dict[str, Any]] = []

    candidates: List[Dict[str, Any]] = []
    seen_pass_mints: set = set()
    lookahead_block_count = 0

    raw_buys_seen = 0
    raw_sells_seen = 0
    curve_updates_seen = 0
    sim_evals_total = 0
    snapshots_total = 0

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
    exp_outcome_counts: Counter = Counter()
    par_outcome_counts: Counter = Counter()
    adv_outcome_counts: Counter = Counter()
    blocker_counts: Counter = Counter()
    selected_size_dist: Counter = Counter()

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
            log(f"V47E-NO-SEND ws_import_err={exc}")
            return
        url = os.environ.get("SOLANATRACKER_RPC_WS", "")
        if not url:
            log("V47E-NO-SEND no_ws_url")
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
                        "jsonrpc": "2.0", "id": 92247,
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
                    log("V47E-NO-SEND shred_subscribed")
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
                log(f"V47E-NO-SEND shred_reconnect "
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
        r0 = simulate_branches(
            cs, size_sol, pending_buys, pending_sells,
            exec_delay_ms=250,
        )
        exp_t = int(r0["expected_tokens"])
        adv_t = int(r0["adverse_tokens"])

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
            min_tokens_for_nonnegative_exit=0,
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
        nonlocal mb_gate_total, mb_gate_pass_total
        nonlocal size_cap_applied_count
        nonlocal boundary_guard_total, boundary_guard_pass_total
        nonlocal downsize_attempt_count, downsize_success_count
        nonlocal downsize_fail_count
        nonlocal replacement_scan_count, replacement_selected_count
        nonlocal two_buyer_total, two_buyer_actual, two_buyer_shadow
        nonlocal two_buyer_block, two_buyer_delegate

        if len(candidates) >= args.target_pass:
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

        # --- V47C Phase 2: Multi-buyer gate -----------------------------
        mb_gate_total += 1
        buyer_stats_for_gate = {
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "pending_buy_count_250ms": int(
                snap.get("pending_buy_count_250ms", 0)
            ),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)
            ),
            "pending_sell_sol_250ms": float(
                snap.get("pending_sell_sol_250ms", 0.0)
            ),
            "top_buyer_share_250ms": float(
                snap.get("top_buyer_share_250ms", 0.0)
            ),
        }
        mb_pass, mb_blocker = evaluate_multi_buyer_gate(
            buyer_stats_for_gate, logger=log, mint_for_log=mint,
        )
        if not mb_pass:
            mb_gate_blocker_counts[mb_blocker or "unknown"] += 1
            return
        mb_gate_pass_total += 1
        sim_evals_total += 1

        size_results: Dict[float, Dict[str, Any]] = {}
        for size in SIZE_SWEEP_SOL:
            size_eval_counts[size] += 1
            res = _evaluate_size_for_mint(
                cs, size, pending_buys, pending_sells,
                float(DEFAULT_TX_FEE_SOL),
            )
            size_results[size] = res
            r0 = res["r0"]
            r1 = res["r1"]
            if r0 is not None:
                if r1 is None:
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
            r_for_dist = r1 if r1 is not None else r0
            if r_for_dist is not None:
                exp_outcome_counts[r_for_dist["expected_branch_outcome"]] += 1
                par_outcome_counts[r_for_dist["partial_branch_outcome"]] += 1
                adv_outcome_counts[r_for_dist["adverse_branch_outcome"]] += 1

        selectable_sizes = [
            s for s, r in size_results.items() if r["selectable"]
        ]
        if not selectable_sizes:
            return

        # --- V47C Phase 3: Size cap ------------------------------------
        buyer_stats_for_cap = {
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "unique_buyers_500ms": int(snap.get("unique_buyers_500ms", 0)),
            "top_buyer_share_250ms": float(
                snap.get("top_buyer_share_250ms", 0.0)
            ),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)
            ),
        }
        size_cap_pass: Dict[float, Tuple[Optional[float], str]] = {}
        for s in selectable_sizes:
            capped, cap_reason = apply_size_cap(
                float(s), buyer_stats_for_cap,
                float(snap.get("pending_buy_sol_250ms", 0.0)),
                logger=log, mint_for_log=mint,
            )
            size_cap_pass[s] = (capped, cap_reason)
            if capped is None:
                size_cap_block_counts[cap_reason or "unknown"] += 1
        admissible_sizes = [
            s for s in selectable_sizes
            if size_cap_pass[s][0] is not None
            and abs(size_cap_pass[s][0] - s) < 1e-9
        ]
        if not admissible_sizes:
            return

        selected_size = min(admissible_sizes)
        res = size_results[selected_size]
        r1 = res["r1"]
        g = res["guard"]
        cap_capped, cap_reason = size_cap_pass[selected_size]
        if cap_capped is not None and abs(cap_capped - float(selected_size)) > 1e-9:
            size_cap_applied_count += 1

        # --- V47D Phase 2: Boundary guard ------------------------------
        # Build full V47D buyer_stats (includes largest_buy_sol_250ms).
        buyer_stats_for_v47d = {
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "unique_buyers_500ms": int(snap.get("unique_buyers_500ms", 0)),
            "pending_buy_count_250ms": int(
                snap.get("pending_buy_count_250ms", 0)
            ),
            "pending_buy_sol_250ms": float(
                snap.get("pending_buy_sol_250ms", 0.0)
            ),
            "pending_sell_sol_250ms": float(
                snap.get("pending_sell_sol_250ms", 0.0)
            ),
            "top_buyer_share_250ms": float(
                snap.get("top_buyer_share_250ms", 0.0)
            ),
            "largest_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)
            ),
        }
        adv_branch = str(r1.get("adverse_branch_outcome", "") or "")
        exp_pnl_orig = float(r1["expected_pnl"])

        # --- V47E Phase 2: Two-buyer guard (applied BEFORE V47D for ub<=2)
        two_buyer_total += 1
        tb_mode, tb_reason = evaluate_two_buyer_guard(
            size_sol=float(selected_size),
            buyer_stats=buyer_stats_for_v47d,
            expected_pnl=exp_pnl_orig,
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=adv_branch,
            logger=log,
            mint_for_log=mint,
        )
        two_buyer_reason_counts[(tb_mode, tb_reason)] += 1
        if tb_mode == MODE_BLOCK:
            two_buyer_block += 1
            replacement_scan_count += 1
            log(
                f"PGG2-V47E-REPLACEMENT-SCAN ts={ts_ms_now} "
                f"reason=v47e_two_buyer_block mint={_short(mint)} "
                f"orig_size={float(selected_size):.4f} "
                f"tb_reason={tb_reason}"
            )
            return
        if tb_mode == MODE_SHADOW:
            two_buyer_shadow += 1
            # Record as shadow only — do NOT count toward target_pass.
            shadow_candidates.append({
                "mint": mint, "ts_ms": ts_ms_now,
                "size_sol": float(selected_size),
                "ub": int(snap.get("unique_buyers_250ms", 0)),
                "tbs": float(snap.get("top_buyer_share_250ms", 0.0)),
                "reason": tb_reason,
            })
            return
        if tb_mode == MODE_ACTUAL:
            two_buyer_actual += 1
        elif tb_mode == MODE_DELEGATE_V47D:
            two_buyer_delegate += 1

        boundary_guard_total += 1
        bg_pass, bg_blocker = evaluate_boundary_guard(
            size_sol=float(selected_size),
            buyer_stats=buyer_stats_for_v47d,
            expected_pnl=exp_pnl_orig,
            no_negative_curve_update_250ms=True,
            adverse_branch_outcome=adv_branch,
            logger=log,
            mint_for_log=mint,
        )

        original_selected_size = float(selected_size)
        downsized_bool = False
        downsize_action = "original"

        if bg_pass:
            boundary_guard_pass_total += 1
            # Accept at original size.
            final_size = float(selected_size)
            final_res = res
            final_r1 = r1
            final_g = g
        else:
            boundary_guard_blocker_counts[bg_blocker or "unknown"] += 1

            # --- V47D Phase 3: Downsize-before-block --------------------
            def _exp_pnl_fn(sz: float) -> float:
                # Recompute expected_pnl at this size using size_results.
                r = size_results.get(float(sz))
                if r is None:
                    return 0.0
                r1_at = r.get("r1")
                if r1_at is not None:
                    return float(r1_at.get("expected_pnl", 0.0))
                r0_at = r.get("r0")
                return float(r0_at.get("expected_pnl", 0.0)) if r0_at else 0.0

            def _branch_fn(sz: float) -> Tuple[bool, Optional[str]]:
                r = size_results.get(float(sz))
                if r is None:
                    return (False, "size_not_swept")
                return (
                    bool(r.get("selectable", False)),
                    r.get("blocker") if not r.get("selectable") else None,
                )

            downsize_attempt_count += 1
            d_size, d_action, d_reason = downsize_candidate(
                initial_selected_size=original_selected_size,
                buyer_stats=buyer_stats_for_v47d,
                expected_pnl_fn=_exp_pnl_fn,
                no_negative_curve_update_250ms=True,
                adverse_branch_outcome=adv_branch,
                branch_check_fn=_branch_fn,
                multi_buyer_pass=True,
                logger=log,
                mint_for_log=mint,
            )

            if d_size is None:
                downsize_fail_count += 1
                # --- V47D Phase 4: Replacement router (logged here) ----
                replacement_scan_count += 1
                log(
                    f"PGG2-V47E-REPLACEMENT-SCAN ts={ts_ms_now} "
                    f"reason=guard_blocked_no_downsize "
                    f"mint={_short(mint)} "
                    f"orig_size={original_selected_size:.4f} "
                    f"bg_blocker={bg_blocker or '-'} "
                    f"d_reason={d_reason}"
                )
                return
            else:
                downsize_success_count += 1
                downsized_bool = (
                    abs(d_size - original_selected_size) > 1e-9
                )
                downsize_action = d_action
                final_size = float(d_size)
                final_res = size_results[final_size]
                final_r1 = final_res["r1"]
                final_g = final_res["guard"]
                if downsized_bool:
                    log(
                        f"PGG2-V47E-REPLACEMENT-SELECTED mint={_short(mint)} "
                        f"size={final_size:.4f} slot={slot} "
                        f"orig_size={original_selected_size:.4f} "
                        f"action=downsized"
                    )

        # Build candidate record.
        tok_we_got = int(final_r1["expected_tokens"])
        sell_lams_expected = int(final_r1["expected_sell_lams"])
        decision_quote_sol = float(sell_lams_expected) / float(LAMPORTS_PER_SOL)

        rec = {
            "type": "v47e_candidate",
            "decision_ts_ms": int(ts_ms_now),
            "mint": mint,
            "slot_at_decision": int(slot),
            "sol_in_at_decision": float(sol_in),
            "signer_at_decision": signer,
            "selected_size_sol": float(final_size),
            "original_size_sol": float(original_selected_size),
            "downsized_bool": bool(downsized_bool),
            "downsize_action": str(downsize_action),
            "multi_buyer_gate_pass": True,
            "multi_buyer_gate_blocker": None,
            "size_cap_applied": bool(
                cap_capped is not None
                and abs(cap_capped - float(original_selected_size)) > 1e-9
            ),
            "size_cap_reason": cap_reason,
            "boundary_guard_pass_at_original": bool(bg_pass),
            "boundary_guard_blocker_at_original": bg_blocker,
            "required_profit_sol": float(final_res["required_profit"]),
            "expected_pnl": float(final_r1["expected_pnl"]),
            "partial_pnl": float(final_r1["partial_pnl"]),
            "adverse_pnl": float(final_r1["adverse_pnl"]),
            "expected_tokens": int(final_r1["expected_tokens"]),
            "partial_tokens": int(final_r1["partial_tokens"]),
            "adverse_tokens": int(final_r1["adverse_tokens"]),
            "final_min_tokens_guard": int(final_g["final_min_tokens"]),
            "guard_fraction": float(final_g["guard_fraction"]),
            "expected_branch_outcome": final_r1["expected_branch_outcome"],
            "partial_branch_outcome": final_r1["partial_branch_outcome"],
            "adverse_branch_outcome": final_r1["adverse_branch_outcome"],
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
            "unique_buyers_50ms": int(snap.get("unique_buyers_50ms", 0)),
            "unique_buyers_100ms": int(snap.get("unique_buyers_100ms", 0)),
            "unique_buyers_250ms": int(snap.get("unique_buyers_250ms", 0)),
            "unique_buyers_500ms": int(snap.get("unique_buyers_500ms", 0)),
            "unique_buyers_1000ms": int(snap.get("unique_buyers_1000ms", 0)),
            "top_buyer_share_250ms": float(snap.get("top_buyer_share_250ms", 0.0)),
            "top_buyer_signer_250ms": str(snap.get("top_buyer_signer_250ms", "")),
            "top_buyer_sol_250ms": float(snap.get("top_buyer_sol_250ms", 0.0)),
            "largest_pending_buy_sol_250ms": float(
                snap.get("largest_pending_buy_sol_250ms", 0.0)
            ),
            "buy_cluster_speed_250ms": float(
                snap.get("buy_cluster_speed_250ms", 0.0)
            ),
            "raw_buy_lead_ms_latest": float(
                snap.get("raw_buy_lead_ms_latest", 0.0)
            ),
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
        if downsized_bool:
            replacement_selected_count += 1

        candidates.append(rec)
        selected_size_dist[f"{final_size:.3f}"] += 1
        seen_pass_mints.add(mint)
        pending_candidates[(mint, int(ts_ms_now))] = rec
        jsonl_fp.write(json.dumps(rec) + "\n")
        jsonl_fp.flush()
        log(
            f"V47E-CANDIDATE mint={_short(mint)} "
            f"size={final_size:.4f} "
            f"orig_size={original_selected_size:.4f} "
            f"downsized={int(downsized_bool)} "
            f"ub250={rec.get('unique_buyers_250ms', 0)} "
            f"tbs={rec.get('top_buyer_share_250ms', 0.0):.3f} "
            f"exp_pnl={final_r1['expected_pnl']:+.6f} "
            f"par_pnl={final_r1['partial_pnl']:+.6f} "
            f"adv_pnl={final_r1['adverse_pnl']:+.6f} "
            f"guard={final_g['final_min_tokens']} "
            f"lead={rec['source_lead_ms']:+.0f}ms "
            f"adv_branch={final_r1['adverse_branch_outcome']} "
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
                                json.dumps({"type": "v47e_observed", **{
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
                                f"V47E-OBSERVED bank pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)} "
                                f"size={size_sol:.4f}"
                            )
                            continue
                        if pnl_now <= LOSS_TH:
                            rec["observed_label_pnl"] = float(pnl_now)
                            rec["observed_label_kind"] = "clamp_loss"
                            rec["observed_label_lag_ms"] = int(lag)
                            jsonl_fp.write(
                                json.dumps({"type": "v47e_observed", **{
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
                                f"V47E-OBSERVED clamp_loss pnl={pnl_now:+.6f} "
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
                                json.dumps({"type": "v47e_observed", **{
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
                                f"V47E-OBSERVED {label_kind} pnl={pnl_now:+.6f} "
                                f"lag={lag}ms mint={_short(mint)}"
                            )

            if now_ts >= next_progress_ms:
                log(
                    f"V47E-NO-SEND progress elapsed_s="
                    f"{(now_ts - t_start_wall)/1000.0:.0f} "
                    f"buys={raw_buys_seen} sells={raw_sells_seen} "
                    f"curve_updates={curve_updates_seen} "
                    f"sim_evals={sim_evals_total} "
                    f"candidates={len(candidates)} "
                    f"boundary_pass={boundary_guard_pass_total} "
                    f"downsize_success={downsize_success_count} "
                    f"replacements_scanned={replacement_scan_count} "
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
        # Replacement-none log if we exited the loop without filling target.
        if len(candidates) < args.target_pass:
            log(
                f"PGG2-V47E-REPLACEMENT-NONE "
                f"elapsed={(_now_ms() - t_start_wall)/1000.0:.0f}s "
                f"reason=timeout_or_end target={args.target_pass} "
                f"accepted={len(candidates)}"
            )
        for key, rec in list(pending_candidates.items()):
            if rec.get("observed_label_kind") is None:
                rec["observed_label_kind"] = "pending"
                rec["observed_label_pnl"] = None
                rec["observed_label_lag_ms"] = None
                jsonl_fp.write(
                    json.dumps({"type": "v47e_observed", **{
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

    # Specific V47D sanity checks
    no_ub2_size_geq_020 = all(
        not (
            int(r.get("unique_buyers_250ms", 0)) == 2
            and float(r.get("selected_size_sol", 0.0)) >= 0.020 - 1e-9
        )
        for r in candidates
    ) if candidates else True
    no_size_geq_020_tbs_gt_055 = all(
        not (
            float(r.get("selected_size_sol", 0.0)) >= 0.020 - 1e-9
            and float(r.get("top_buyer_share_250ms", 0.0)) > 0.55
        )
        for r in candidates
    ) if candidates else True
    all_ub_250_geq_2 = (
        all(int(r.get("unique_buyers_250ms", 0)) >= 2 for r in candidates)
        if candidates else False
    )

    md_path = Path(args.out_md)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# V47E - Two-Buyer-Guarded No-Send Report\n\n")
        f.write(f"- run_ts_local: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- runtime_s: {elapsed_s:.1f}\n")
        f.write(f"- sizes_tested: {SIZE_SWEEP_SOL}\n")
        f.write(f"- amount_sol_min: {min(SIZE_SWEEP_SOL)}\n")
        f.write(f"- amount_sol_max: {max(SIZE_SWEEP_SOL)}\n")
        f.write(f"- strategy_min_tokens_frac: {args.strategy_min_tokens_frac}\n")
        f.write(f"- max_guard_fraction: {args.max_guard_fraction}\n")
        f.write(f"- target_pass: {args.target_pass}\n")
        f.write("- rules_path: (in-code; see pgg2_v47e_two_buyer_guard.py "
                "+ pgg2_v47d_boundary_guard.py + pgg2_v47e_clean_close.py)\n\n")
        f.write("## Engine sanity\n\n")
        f.write(f"- raw_pump_buys_seen: {raw_buys_seen}\n")
        f.write(f"- raw_pump_sells_seen: {raw_sells_seen}\n")
        f.write(f"- curve_updates: {curve_updates_seen}\n")
        f.write(f"- snapshots: {snapshots_total}\n")
        f.write(f"- sim_evaluations: {sim_evals_total}\n")
        f.write(f"- lookahead_blocks: {lookahead_block_count}\n")
        f.write(f"- hot_mints_terminal: {len(hot_mint_last_seen)}\n\n")
        f.write("## Multi-buyer gate stats (V47C)\n\n")
        f.write(
            f"- evaluations: {mb_gate_total}\n"
            f"- passed: {mb_gate_pass_total}\n"
        )
        f.write("- block_reasons:\n")
        if mb_gate_blocker_counts:
            for k, v in mb_gate_blocker_counts.most_common(10):
                f.write(f"  - {k}: {v}\n")
        else:
            f.write("  - (none)\n")

        f.write("\n## V47E two-buyer guard stats\n\n")
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
        f.write(f"- shadow_candidates_recorded: {len(shadow_candidates)}\n")

        f.write("\n## V47D boundary guard stats (delegated from V47E for ub>=3)\n\n")
        f.write(f"- evaluations: {boundary_guard_total}\n")
        f.write(f"- passed: {boundary_guard_pass_total}\n")
        f.write("- blocker_reasons (rule-tagged):\n")
        if boundary_guard_blocker_counts:
            for k, v in boundary_guard_blocker_counts.most_common(20):
                f.write(f"  - {k}: {v}\n")
        else:
            f.write("  - (none)\n")

        f.write("\n## Downsize stats\n\n")
        f.write(f"- downsize_attempts: {downsize_attempt_count}\n")
        f.write(f"- downsize_success: {downsize_success_count}\n")
        f.write(f"- downsize_fail: {downsize_fail_count}\n")

        f.write("\n## Replacement router stats\n\n")
        f.write(f"- replacement_scans: {replacement_scan_count}\n")
        f.write(f"- replacement_selected: {replacement_selected_count}\n")

        f.write("\n## Size sweep results\n\n")
        f.write(
            "| size_sol | evaluated | guarded_pass | guard_too_tight | "
            "required_profit_fail | unsafe_open | selectable |\n"
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

        f.write("\n## V47E candidate entries\n\n")
        f.write(f"- candidates_count: {len(candidates)}\n")
        f.write(f"- unique_mints: {len(seen_pass_mints)}\n")
        f.write("- selected (final) size distribution:\n")
        for s, c in sorted(selected_size_dist.items()):
            f.write(f"  - {s}: {c}\n")
        if not selected_size_dist:
            f.write("  - (none)\n")
        ub_dist: Counter = Counter()
        for r in candidates:
            ub_dist[int(r.get("unique_buyers_250ms", 0))] += 1
        f.write("- unique_buyers_250ms distribution (selected):\n")
        if ub_dist:
            for k in sorted(ub_dist.keys()):
                f.write(f"  - {k}: {ub_dist[k]}\n")
        else:
            f.write("  - (none)\n")

        # Specific checks
        ub2_size_ge_020 = [
            r for r in candidates
            if int(r.get("unique_buyers_250ms", 0)) == 2
            and float(r.get("selected_size_sol", 0.0)) >= 0.020 - 1e-9
        ]
        size_ge_020_high_tbs = [
            r for r in candidates
            if float(r.get("selected_size_sol", 0.0)) >= 0.020 - 1e-9
            and float(r.get("top_buyer_share_250ms", 0.0)) > 0.55
        ]
        f.write("\n## Sanity assertions\n\n")
        f.write(
            f"- accepted size>=0.020 AND ub_250==2 count "
            f"(must be 0): {len(ub2_size_ge_020)}\n"
        )
        f.write(
            f"- accepted size>=0.020 AND tbs>0.55 count "
            f"(must be 0): {len(size_ge_020_high_tbs)}\n"
        )

        f.write("\n## Causal observed outcomes\n\n")
        f.write(f"- bank: {bank_count}\n")
        f.write(f"- scratch: {scratch_count}\n")
        f.write(f"- clamp_loss: {clamp_count}\n")
        f.write(f"- expired_loss: {expired_loss_count}\n")
        f.write(f"- neutral: {neutral_count}\n")
        f.write(f"- pending: {pending_count}\n\n")

        f.write("## Per-candidate detail\n\n")
        if candidates:
            f.write(
                "| # | mint | size | orig_size | downsized | ub_250 | tbs | "
                "exp_pnl | adv_pnl | adv_branch | obs_kind | obs_pnl | "
                "obs_lag |\n"
                "|---|------|------|-----------|-----------|--------|-----|"
                "---------|---------|------------|----------|---------|"
                "---------|\n"
            )
            for i, r in enumerate(candidates, 1):
                f.write(
                    f"| {i} | {_short(r.get('mint',''))} | "
                    f"{float(r.get('selected_size_sol',0.0)):.4f} | "
                    f"{float(r.get('original_size_sol',0.0)):.4f} | "
                    f"{int(bool(r.get('downsized_bool', False)))} | "
                    f"{int(r.get('unique_buyers_250ms',0))} | "
                    f"{float(r.get('top_buyer_share_250ms',0.0)):.3f} | "
                    f"{float(r.get('expected_pnl',0.0)):+.6f} | "
                    f"{float(r.get('adverse_pnl',0.0)):+.6f} | "
                    f"{r.get('adverse_branch_outcome','-')} | "
                    f"{r.get('observed_label_kind','pending') or 'pending'} | "
                    f"{('%+.6f' % float(r.get('observed_label_pnl') or 0.0)) if r.get('observed_label_pnl') is not None else 'n/a'} | "
                    f"{r.get('observed_label_lag_ms','') if r.get('observed_label_lag_ms') is not None else ''} |\n"
                )
        else:
            f.write("- (none)\n")

        # V47E specific sanity checks
        no_ub_le_1_actual = all(
            int(r.get("unique_buyers_250ms", 0)) >= 2 for r in candidates
        ) if candidates else True
        no_ub2_tbs_gt_055_actual = all(
            not (
                int(r.get("unique_buyers_250ms", 0)) == 2
                and float(r.get("top_buyer_share_250ms", 0.0)) > 0.55
            )
            for r in candidates
        ) if candidates else True
        # In no-send pending_count == count of candidates whose observation
        # did not conclude within MAX_HOLD / MAX_EXTEND_MS.
        cc = evaluate_clean_close(
            entries=len(candidates),
            closed_nonneg=bank_count + scratch_count + neutral_count,
            closed_neg=clamp_count + expired_loss_count,
            pending=pending_count,
            open_positions=0,
            target_closed_nonneg=int(args.target_pass),
            logger=log,
        )

        # Verdict
        meets_target = bool(len(candidates) >= int(args.target_pass))
        zero_neg = bool(clamp_count == 0 and expired_loss_count == 0)
        zero_la = bool(lookahead_block_count == 0)
        clean_close_pass = bool(cc["pass"])
        overall = bool(
            clean_close_pass and zero_la
            and all_candidates_adverse_safe
            and all_candidates_guard_feasible
            and all_candidates_in_size_range
            and all_ub_250_geq_2
            and no_ub2_size_geq_020
            and no_size_geq_020_tbs_gt_055
            and no_ub_le_1_actual
            and no_ub2_tbs_gt_055_actual
        )
        f.write("\n## V47E clean-close status\n\n")
        f.write(f"- entries: {cc['entries']}\n")
        f.write(f"- closed_nonneg: {cc['closed_nonneg']}\n")
        f.write(f"- closed_neg: {cc['closed_neg']}\n")
        f.write(f"- pending: {cc['pending']}\n")
        f.write(f"- open_positions: {cc['open_positions']}  (no-send: always 0)\n")
        f.write(f"- pass: {cc['pass']}\n")
        f.write(f"- fail_reason: {cc['fail_reason']}\n\n")

        f.write("\n## Verdict\n\n")
        f.write(
            f"- meets_target_count(>={args.target_pass}): {meets_target} "
            f"({len(candidates)}/{args.target_pass})\n"
        )
        f.write(f"- clean_close_pass: {clean_close_pass}\n")
        f.write(f"- zero_observed_negative_outcomes: {zero_neg}\n")
        f.write(f"- zero_lookahead_violations: {zero_la}\n")
        f.write(f"- all_candidates_ub250_geq_2: {all_ub_250_geq_2}\n")
        f.write(f"- no_ub_le_1_actual: {no_ub_le_1_actual}\n")
        f.write(f"- no_ub2_tbs_gt_055_actual: {no_ub2_tbs_gt_055_actual}\n")
        f.write(f"- no_ub2_size_geq_020: {no_ub2_size_geq_020}\n")
        f.write(f"- no_size_geq_020_tbs_gt_055: {no_size_geq_020_tbs_gt_055}\n")
        f.write(f"- all_candidates_adverse_safe: {all_candidates_adverse_safe}\n")
        f.write(f"- all_candidates_guard_feasible: {all_candidates_guard_feasible}\n")
        f.write(f"- all_candidates_in_size_range: {all_candidates_in_size_range}\n")
        f.write(f"- OVERALL_VERDICT: {'PASS' if overall else 'FAIL'}\n\n")

        f.write("## HONEST ASSESSMENT\n\n")
        if len(candidates) == 0:
            f.write(
                f"- V47E yielded 0 candidates in {elapsed_s:.0f}s. "
                f"two_buyer_total={two_buyer_total}, "
                f"two_buyer_block={two_buyer_block}, "
                f"boundary_guard_total={boundary_guard_total}, "
                f"replacement_scans={replacement_scan_count}. "
                f"Top mb-gate blockers: "
                f"{dict(mb_gate_blocker_counts.most_common(5))}. "
                f"Top two-buyer reasons: "
                f"{dict(two_buyer_reason_counts.most_common(5))}.\n"
            )
        elif clamp_count + expired_loss_count > 0:
            f.write(
                f"- V47E produced {len(candidates)} candidate(s) but "
                f"{clamp_count + expired_loss_count} resulted in observed "
                f"negative outcomes. Two-buyer + boundary guards did not "
                f"catch the failure pattern.\n"
            )
        elif clean_close_pass:
            f.write(
                f"- V47E PASSED clean-close: {len(candidates)} candidates, "
                f"closed_nonneg={cc['closed_nonneg']}, closed_neg=0, "
                f"pending=0, open=0. Two-buyer guard + boundary guard + "
                f"replacement router operational.\n"
            )
        elif pending_count > 0:
            f.write(
                f"- V47E produced {len(candidates)} candidate(s) with "
                f"{pending_count} pending observations at end-of-run. "
                f"Clean-close pass requires pending=0. Increase max_seconds "
                f"or reduce target_pass to satisfy clean-close.\n"
            )
        else:
            f.write(
                f"- V47E produced {len(candidates)} candidate(s) without "
                f"negatives but did not reach target ({args.target_pass}). "
                f"Replacement_scans={replacement_scan_count}; "
                f"two_buyer_block={two_buyer_block}.\n"
            )

    log(f"V47E-NO-SEND wrote {md_path}")
    log(
        f"V47E-NO-SEND done elapsed_s={elapsed_s:.1f} "
        f"candidates={len(candidates)} "
        f"banks={bank_count} losses={clamp_count + expired_loss_count} "
        f"pending={pending_count} "
        f"boundary_pass={boundary_guard_pass_total}/{boundary_guard_total} "
        f"downsize_ok={downsize_success_count}/{downsize_attempt_count} "
        f"replacement_scans={replacement_scan_count}"
    )
    overall_pass = bool(
        cc["pass"]
        and lookahead_block_count == 0
        and all_candidates_adverse_safe
        and all_candidates_guard_feasible
        and all_candidates_in_size_range
        and all(int(r.get("unique_buyers_250ms", 0)) >= 2 for r in candidates)
        and no_ub2_size_geq_020
        and no_size_geq_020_tbs_gt_055
        and no_ub_le_1_actual
        and no_ub2_tbs_gt_055_actual
    )
    return 0 if overall_pass else 3


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
