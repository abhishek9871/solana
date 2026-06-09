#!/usr/bin/env python3
"""Small latency-first Goal5 scout.

This runner keeps the decision path intentionally narrow:

  PublicNode Yellowstone feed -> two-buy continuation shape -> guarded buy
  -> fast rent-aware sell/close -> wallet/token verification.

It does not launch V287/V288 and does not use the large send-authority file.
"""
from __future__ import annotations

import argparse
import os
import queue
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from pgg2_goal5_standalone import (
    ATA_RENT_LAMPORTS,
    LAMPORTS_PER_SOL,
    _decode_pump,
    _default_rpc_url,
    _load_env,
    _log,
    _make_broker,
    _proto_imports,
    _remember_feed_accounts,
    _request_iter,
    _short,
    _token_account_lamports,
    _token_accounts_summary,
    _token_balance_raw_or_zero,
    _wait_token_balance,
    _wallet_lamports,
)


@dataclass
class ScoutCandidate:
    mint: str
    start_ms: int
    current_lamports: int
    first_follow_lamports: int
    follow_lamports: int
    follow_buys: int
    start_sig: str
    start_payer: str
    feed_rec: dict[str, Any]
    follow_sigs: list[str] = field(default_factory=list)
    follow_payers: list[str] = field(default_factory=list)
    latest_follow_ms: int = 0
    train_span_ms: int = 0


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _quote_ref_030(quote_tokens: float, size_sol: float) -> float:
    if size_sol <= 0:
        return 0.0
    return float(quote_tokens) * (0.030 / float(size_sol))


def _follow_current_ratio(current_sol: float, follow_sol: float) -> float:
    if current_sol <= 0:
        return 0.0
    return float(follow_sol) / float(current_sol)


def _buy_slippage_pct_for_reason(args: argparse.Namespace, reason: str) -> float:
    if str(reason).startswith("small005_c3_"):
        return float(getattr(args, "c3_buy_slippage_pct", args.buy_slippage_pct))
    return float(args.buy_slippage_pct)


def _pre_shape_allowed(
    args: argparse.Namespace,
    current_sol: float,
    first_follow_sol: float,
    follow_sol: float,
    follow_buys: int,
    start_age_ms: int,
    latest_follow_age_ms: int | None = None,
    train_span_ms: int | None = None,
) -> tuple[bool, str]:
    train_follow_age_ms = start_age_ms if latest_follow_age_ms is None else int(latest_follow_age_ms)
    train_span = 0 if train_span_ms is None else int(train_span_ms)
    buy_train_fresh_shape = (
        args.clean_buy_train_continuation_enabled
        and float(args.size_sol) <= 0.006
        and float(args.buy_train_min_current_sol) <= current_sol <= float(args.buy_train_max_current_sol)
        and follow_buys >= int(args.buy_train_min_follow_buys)
        and follow_sol >= float(args.buy_train_min_follow_sol)
        and train_follow_age_ms <= int(args.buy_train_max_follow_age_ms)
        and train_span <= int(args.buy_train_max_train_span_ms)
    )
    if start_age_ms > int(args.max_prequote_start_age_ms):
        if buy_train_fresh_shape:
            return True, "pre_clean_buy_train_continuation"
        return False, "pre_stale_start"
    if (
        args.proven_strong_enabled
        and 2.00 <= current_sol <= 2.18
        and follow_buys == 1
        and 2.00 <= first_follow_sol <= 2.80
        and 2.00 <= follow_sol <= 2.80
    ):
        return True, "pre_proven_strong_first_follow"
    if (
        args.micro_c0_highquote_enabled
        and 0.28 <= current_sol <= 0.65
        and follow_buys == 1
        and 0.45 <= first_follow_sol <= 0.58
        and 0.45 <= follow_sol <= 0.58
    ):
        return True, "pre_micro_c0_f05_q900_follow"
    if (
        args.cur1_q600_clean_follow_enabled
        and 0.90 <= current_sol <= 1.30
        and follow_buys == 1
        and 0.30 <= first_follow_sol <= 0.55
        and 0.30 <= follow_sol <= 0.55
        and _follow_current_ratio(current_sol, min(first_follow_sol, follow_sol))
        >= float(getattr(args, "cur1_q600_min_follow_current_ratio", 0.38))
        and train_span <= int(getattr(args, "cur1_q600_max_train_span_ms", 25))
    ):
        return True, "pre_cur1_q600_clean_follow"
    if (
        args.small_size005_cur1_q900_follow_enabled
        and 0.95 <= current_sol <= 1.05
        and 1 <= follow_buys <= 2
        and 0.49 <= first_follow_sol <= 0.53
        and 0.49 <= follow_sol <= 0.56
    ):
        return True, "pre_small005_cur1_f05_q900_follow"
    if (
        args.small_size005_c0_f22_multi_q900_enabled
        and 0.80 <= current_sol <= 0.90
        and 3 <= follow_buys <= 4
        and 2.18 <= first_follow_sol <= 2.25
        and 2.35 <= follow_sol <= 2.55
    ):
        return True, "pre_small005_c0_f22_multi_q900"
    if (
        args.small_size005_c3_f13_q720_enabled
        and 3.20 <= current_sol <= 3.40
        and follow_buys == 1
        and 1.20 <= first_follow_sol <= 1.35
        and 1.20 <= follow_sol <= 1.35
    ):
        return True, "pre_small005_c3_f13_q720"
    if (
        args.small_size005_c3_strong_fast_follow_enabled
        and float(args.size_sol) <= 0.006
        and 3.00 <= current_sol <= 3.40
        and 1 <= follow_buys <= 3
        and 1.45 <= first_follow_sol <= 2.20
        and 1.45 <= follow_sol <= 3.60
        and train_span <= 25
    ):
        return True, "pre_small005_c3_strong_fast_follow"
    if buy_train_fresh_shape:
        return True, "pre_clean_buy_train_continuation"
    if (
        args.scratch_midquote_enabled
        and 0.65 <= current_sol <= 1.95
        and follow_buys == 1
        and 0.38 <= first_follow_sol <= 1.25
        and 0.38 <= follow_sol <= 1.25
    ):
        return True, "pre_scratch_midquote_one_follow"
    if (
        args.early_cur1_q800_enabled
        and 1.15 <= current_sol <= 1.35
        and follow_buys == 0
        and first_follow_sol == 0.0
        and follow_sol == 0.0
    ):
        return True, "pre_early_cur1_q800_current"
    return False, "pre_shape"


def _lane_allowed(
    args: argparse.Namespace,
    current_sol: float,
    first_follow_sol: float,
    follow_sol: float,
    follow_buys: int,
    quote_tokens: float,
    start_age_ms: int,
    projected_headroom_lamports: int | None = None,
    latest_follow_age_ms: int | None = None,
    train_span_ms: int | None = None,
) -> tuple[bool, str]:
    max_send_age_ms = int(getattr(args, "max_send_start_age_ms", args.max_start_age_ms))
    projected_headroom = -10**18 if projected_headroom_lamports is None else int(projected_headroom_lamports)
    train_follow_age_ms = start_age_ms if latest_follow_age_ms is None else int(latest_follow_age_ms)
    train_span = 0 if train_span_ms is None else int(train_span_ms)
    buy_train_fresh_shape = (
        args.clean_buy_train_continuation_enabled
        and float(args.size_sol) <= 0.006
        and float(args.buy_train_min_current_sol) <= current_sol <= float(args.buy_train_max_current_sol)
        and follow_buys >= int(args.buy_train_min_follow_buys)
        and follow_sol >= float(args.buy_train_min_follow_sol)
        and float(args.buy_train_min_quote_tokens_ref) <= quote_tokens <= float(args.buy_train_max_quote_tokens_ref)
        and train_follow_age_ms <= int(args.buy_train_max_follow_age_ms)
        and train_span <= int(args.buy_train_max_train_span_ms)
    )
    if start_age_ms > max_send_age_ms:
        if buy_train_fresh_shape:
            return True, "clean_buy_train_continuation"
        return False, "stale_start"
    if (
        args.proven_strong_enabled
        and 2.00 <= current_sol <= 2.18
        and follow_buys == 1
        and 2.00 <= first_follow_sol <= 2.80
        and 2.00 <= follow_sol <= 2.80
        and 615_000 <= quote_tokens <= 700_000
    ):
        return True, "proven_strong_first_follow"
    if (
        args.micro_c0_highquote_enabled
        and 0.28 <= current_sol <= 0.65
        and follow_buys == 1
        and 0.45 <= first_follow_sol <= 0.58
        and 0.45 <= follow_sol <= 0.58
        and 900_000 <= quote_tokens <= 1_020_000
    ):
        return True, "micro_c0_f05_q900_follow"
    if (
        args.cur1_q600_clean_follow_enabled
        and 0.90 <= current_sol <= 1.30
        and follow_buys == 1
        and 0.30 <= first_follow_sol <= 0.55
        and 0.30 <= follow_sol <= 0.55
        and _follow_current_ratio(current_sol, min(first_follow_sol, follow_sol))
        >= float(getattr(args, "cur1_q600_min_follow_current_ratio", 0.38))
        and train_span <= int(getattr(args, "cur1_q600_max_train_span_ms", 25))
        and 560_000 <= quote_tokens <= 650_000
    ):
        return True, "cur1_q600_clean_follow"
    if (
        args.small_size005_cur1_q900_follow_enabled
        and float(args.size_sol) <= 0.006
        and 0.95 <= current_sol <= 1.05
        and 1 <= follow_buys <= 2
        and 0.49 <= first_follow_sol <= 0.53
        and 0.49 <= follow_sol <= 0.56
        and 850_000 <= quote_tokens <= 970_000
    ):
        return True, "small005_cur1_f05_q900_follow"
    if (
        args.small_size005_c0_f22_multi_q900_enabled
        and float(args.size_sol) <= 0.006
        and 0.80 <= current_sol <= 0.90
        and 3 <= follow_buys <= 4
        and 2.18 <= first_follow_sol <= 2.25
        and 2.35 <= follow_sol <= 2.55
        and 880_000 <= quote_tokens <= 930_000
    ):
        return True, "small005_c0_f22_multi_q900"
    if (
        args.small_size005_c3_f13_q720_enabled
        and float(args.size_sol) <= 0.006
        and 3.20 <= current_sol <= 3.40
        and follow_buys == 1
        and 1.20 <= first_follow_sol <= 1.35
        and 1.20 <= follow_sol <= 1.35
        and 660_000 <= quote_tokens <= 765_000
    ):
        return True, "small005_c3_f13_q720"
    if (
        args.small_size005_c3_strong_fast_follow_enabled
        and float(args.size_sol) <= 0.006
        and 3.00 <= current_sol <= 3.40
        and 1 <= follow_buys <= 3
        and 1.45 <= first_follow_sol <= 2.20
        and 1.45 <= follow_sol <= 3.60
        and train_span <= 25
        and 520_000 <= quote_tokens <= 765_000
    ):
        return True, "small005_c3_strong_fast_follow"
    if buy_train_fresh_shape:
        return True, "clean_buy_train_continuation"
    if (
        args.scratch_midquote_enabled
        and 0.65 <= current_sol <= 1.95
        and follow_buys == 1
        and 0.38 <= first_follow_sol <= 1.25
        and 0.38 <= follow_sol <= 1.25
        and float(args.scratch_midquote_min_quote_tokens_ref)
        <= quote_tokens
        <= float(args.scratch_midquote_max_quote_tokens_ref)
        and projected_headroom >= int(args.scratch_midquote_min_projected_headroom_lamports)
    ):
        return True, "scratch_positive_midquote_one_follow"
    if (
        args.early_cur1_q800_enabled
        and 1.15 <= current_sol <= 1.35
        and follow_buys == 0
        and first_follow_sol == 0.0
        and follow_sol == 0.0
        and 830_000 <= quote_tokens <= 890_000
    ):
        return True, "early_cur1_current_q800"
    return False, "shape"


def _c3_postquote_tape_check(
    args: argparse.Namespace,
    cand: ScoutCandidate,
    reason: str,
    mint_hist: list[dict[str, Any]],
) -> tuple[bool, str]:
    if not str(reason).startswith("small005_c3_") or not bool(args.c3_postquote_tape_check_enabled):
        return True, "not_c3"

    known_sigs = {str(cand.start_sig), *(str(sig) for sig in cand.follow_sigs)}
    after_start = [
        x
        for x in mint_hist
        if int(x.get("recv_ms") or 0) > int(cand.start_ms)
        and _now_ms() - int(x.get("recv_ms") or 0) <= int(args.c3_postquote_tape_window_ms)
    ]
    sells = [x for x in after_start if x.get("kind") == "sell"]
    hidden_buys = [
        x
        for x in after_start
        if x.get("kind") == "buy" and str(x.get("sig") or "") not in known_sigs
    ]
    hidden_sol = sum(int(x.get("sol_lamports") or 0) for x in hidden_buys) / LAMPORTS_PER_SOL
    dust_max = float(args.c3_dust_buy_max_sol) * LAMPORTS_PER_SOL
    large_min = float(args.c3_large_buy_min_sol) * LAMPORTS_PER_SOL
    hidden_dust = sum(1 for x in hidden_buys if int(x.get("sol_lamports") or 0) < dust_max)
    hidden_large = sum(1 for x in hidden_buys if int(x.get("sol_lamports") or 0) >= large_min)
    current_sol = int(cand.current_lamports) / LAMPORTS_PER_SOL
    first_follow_sol = int(cand.first_follow_lamports) / LAMPORTS_PER_SOL
    c3_mid_current_min = 3.00 if reason == "small005_c3_strong_fast_follow" else 3.20
    pass_check = (
        not sells
        and hidden_sol >= float(args.c3_min_hidden_postfollow_sol)
        and hidden_large >= int(args.c3_min_hidden_large_buys)
        and hidden_dust <= int(args.c3_max_hidden_dust_buys)
    )
    mid_clean_pass = (
        not pass_check
        and not sells
        and c3_mid_current_min <= current_sol <= 3.40
        and hidden_sol >= 2.00
        and hidden_large >= 2
        and hidden_dust == 0
        and bool(getattr(args, "c3_mid_clean_tape_enabled", False))
    )
    high_total_low_dust_pass = (
        not pass_check
        and not mid_clean_pass
        and not sells
        and c3_mid_current_min <= current_sol <= 3.40
        and hidden_sol >= 3.00
        and hidden_large >= 2
        and hidden_dust <= 1
    )
    pass_check = pass_check or mid_clean_pass or high_total_low_dust_pass
    if pass_check and high_total_low_dust_pass:
        reason_out = "c3_tape_high_total_low_dust_ok"
    elif pass_check and mid_clean_pass:
        reason_out = "c3_tape_mid_clean_ok"
    elif pass_check:
        reason_out = "c3_tape_ok"
    else:
        reason_out = "c3_tape_block"
    low_anchor_block = (
        pass_check
        and reason == "small005_c3_strong_fast_follow"
        and bool(args.c3_strong_fast_low_anchor_block_enabled)
        and current_sol <= float(args.c3_strong_fast_low_anchor_max_current_sol)
        and first_follow_sol <= float(args.c3_strong_fast_low_anchor_max_first_follow_sol)
        and hidden_sol < float(args.c3_strong_fast_low_anchor_min_hidden_sol)
    )
    if low_anchor_block:
        pass_check = False
        reason_out = "c3_tape_low_anchor_block"
    _log(
        "PGG2-GOAL5-C3-POSTQUOTE-TAPE "
        f"mint={_short(cand.mint)} full_mint={cand.mint} pass={int(pass_check)} reason={reason_out} "
        f"start_sig={cand.start_sig} follow_sigs={','.join(cand.follow_sigs)} "
        f"start_payer={cand.start_payer} follow_payers={','.join(cand.follow_payers)} "
        f"events_after_start={len(after_start)} hidden_buys={len(hidden_buys)} "
        f"current_sol={current_sol:.6f} first_follow_sol={first_follow_sol:.6f} "
        f"hidden_sol={hidden_sol:.6f} hidden_large={hidden_large} hidden_dust={hidden_dust} "
        f"sells_after_start={len(sells)} min_hidden_sol={float(args.c3_min_hidden_postfollow_sol):.6f} "
        f"min_hidden_large={int(args.c3_min_hidden_large_buys)} max_hidden_dust={int(args.c3_max_hidden_dust_buys)}"
    )
    return pass_check, reason_out


def _clean_buy_train_tape_check(
    args: argparse.Namespace,
    cand: ScoutCandidate,
    reason: str,
    mint_hist: list[dict[str, Any]],
) -> tuple[bool, str]:
    if reason != "clean_buy_train_continuation" or not bool(args.clean_buy_train_tape_check_enabled):
        return True, "not_buy_train"

    start_ms = int(cand.start_ms)
    max_age_ms = int(args.buy_train_tape_window_ms)
    train_events = [
        x
        for x in mint_hist
        if start_ms <= int(x.get("recv_ms") or 0)
        and int(x.get("recv_ms") or 0) - start_ms <= max_age_ms
    ]
    buys = [x for x in train_events if x.get("kind") == "buy"]
    sells = [x for x in train_events if x.get("kind") == "sell"]
    buy_sol = sum(int(x.get("sol_lamports") or 0) for x in buys) / LAMPORTS_PER_SOL
    follow_buy_sol = sum(int(x.get("sol_lamports") or 0) for x in buys if str(x.get("sig") or "") != str(cand.start_sig)) / LAMPORTS_PER_SOL
    dust_max = float(args.buy_train_dust_buy_max_sol) * LAMPORTS_PER_SOL
    large_min = float(args.buy_train_large_buy_min_sol) * LAMPORTS_PER_SOL
    dust_buys = sum(1 for x in buys if int(x.get("sol_lamports") or 0) < dust_max)
    large_buys = sum(1 for x in buys if int(x.get("sol_lamports") or 0) >= large_min)
    payers = [str(x.get("fee_payer") or "") for x in buys if str(x.get("fee_payer") or "")]
    unique_payers = len(set(payers))
    top_payer_count = max((payers.count(p) for p in set(payers)), default=0)
    pass_check = (
        not sells
        and len(buys) >= int(args.buy_train_min_total_buys)
        and buy_sol >= float(args.buy_train_min_total_sol)
        and follow_buy_sol >= float(args.buy_train_min_follow_sol)
        and large_buys >= int(args.buy_train_min_large_buys)
        and unique_payers >= int(args.buy_train_min_unique_payers)
        and dust_buys <= int(args.buy_train_max_dust_buys)
        and top_payer_count <= int(args.buy_train_max_top_payer_count)
    )
    reason_out = "buy_train_tape_ok" if pass_check else "buy_train_tape_block"
    _log(
        "PGG2-GOAL5-BUY-TRAIN-TAPE "
        f"mint={_short(cand.mint)} full_mint={cand.mint} pass={int(pass_check)} reason={reason_out} "
        f"start_sig={cand.start_sig} follow_sigs={','.join(cand.follow_sigs)} "
        f"events={len(train_events)} buys={len(buys)} sells={len(sells)} "
        f"buy_sol={buy_sol:.6f} follow_buy_sol={follow_buy_sol:.6f} "
        f"large_buys={large_buys} dust_buys={dust_buys} unique_payers={unique_payers} "
        f"top_payer_count={top_payer_count} min_total_buys={int(args.buy_train_min_total_buys)} "
        f"min_total_sol={float(args.buy_train_min_total_sol):.6f} "
        f"min_follow_sol={float(args.buy_train_min_follow_sol):.6f}"
    )
    return pass_check, reason_out


def _trade(
    args: argparse.Namespace,
    broker: Any,
    cand: ScoutCandidate,
    counters: Counter[str],
    hist: dict[str, deque[dict[str, Any]]],
    hist_lock: threading.Lock,
) -> bool:
    from pgg2_direct_pump import as_pubkey  # type: ignore

    mint = cand.mint
    current_sol = cand.current_lamports / LAMPORTS_PER_SOL
    first_follow_sol = cand.first_follow_lamports / LAMPORTS_PER_SOL
    follow_sol = cand.follow_lamports / LAMPORTS_PER_SOL
    now_ms = _now_ms()
    start_age_ms = now_ms - cand.start_ms
    latest_follow_ms = cand.latest_follow_ms or cand.start_ms
    latest_follow_age_ms = now_ms - latest_follow_ms
    _remember_feed_accounts(broker, cand.feed_rec)

    pre_allowed, pre_reason = _pre_shape_allowed(
        args,
        current_sol,
        first_follow_sol,
        follow_sol,
        cand.follow_buys,
        start_age_ms,
        latest_follow_age_ms,
        cand.train_span_ms,
    )
    if not pre_allowed:
        counters[f"preauth_block_{pre_reason}"] += 1
        _log(
            "PGG2-GOAL5-SCOUT-PREAUTH "
            f"mint={_short(mint)} full_mint={mint} pass=0 reason={pre_reason} "
            f"current_sol={current_sol:.6f} first_follow_sol={first_follow_sol:.6f} "
            f"follow_sol={follow_sol:.6f} follow_buys={cand.follow_buys} "
            f"start_age_ms={start_age_ms} latest_follow_age_ms={latest_follow_age_ms} "
            f"train_span_ms={cand.train_span_ms}"
        )
        return False

    quote_start = _now_ms()
    try:
        curve = broker.bonding_curve(as_pubkey(mint))
        global_cfg = broker.pump_global()
        spend_lamports = int(float(args.size_sol) * LAMPORTS_PER_SOL)
        tokens_raw, buy_fee = broker.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
        quote_tokens = float(broker.raw_to_ui(as_pubkey(mint), int(tokens_raw)))
        quote_tokens_ref = _quote_ref_030(quote_tokens, float(args.size_sol))
    except Exception as exc:
        counters["auth_block_quote_error"] += 1
        _log(
            "PGG2-GOAL5-SCOUT-QUOTE-BLOCK "
            f"mint={_short(mint)} full_mint={mint} reason=quote_error "
            f"error={str(exc).replace(' ', '_')[:160]} "
            f"pre_reason={pre_reason} start_age_ms={_now_ms() - cand.start_ms} "
            f"prequote_start_age_ms={start_age_ms} latest_follow_age_ms={latest_follow_age_ms} "
            f"train_span_ms={cand.train_span_ms}"
        )
        return False
    quote_latency_ms = _now_ms() - quote_start
    projected_sell_lamports = 0
    projected_sell_fee = 0
    projected_cost_lamports = (
        spend_lamports
        + int(args.buy_tx_fee_est_lamports)
        + int(args.sell_fee_est_lamports)
        + int(args.target_lamports)
    )
    projected_headroom = 0
    post_buy_curve = broker.simulate_post_buy_pump_curve(curve, int(tokens_raw))
    projected_sell_lamports, projected_sell_fee = broker.quote_pump_sell_sol(
        int(tokens_raw),
        post_buy_curve,
        global_cfg,
    )
    projected_headroom = int(projected_sell_lamports) - int(projected_cost_lamports)
    send_now_ms = _now_ms()
    send_start_age_ms = send_now_ms - cand.start_ms
    send_latest_follow_age_ms = send_now_ms - latest_follow_ms
    allowed, reason = _lane_allowed(
        args,
        current_sol,
        first_follow_sol,
        follow_sol,
        cand.follow_buys,
        quote_tokens_ref,
        send_start_age_ms,
        projected_headroom,
        send_latest_follow_age_ms,
        cand.train_span_ms,
    )
    _log(
        "PGG2-GOAL5-SCOUT-AUTH "
        f"mint={_short(mint)} full_mint={mint} pass={int(allowed)} reason={reason} "
        f"current_sol={current_sol:.6f} first_follow_sol={first_follow_sol:.6f} "
        f"follow_sol={follow_sol:.6f} follow_buys={cand.follow_buys} "
        f"quote_tokens={quote_tokens:.6f} quote_tokens_ref_030={quote_tokens_ref:.6f} buy_fee={buy_fee} "
        f"projected_sell={projected_sell_lamports} projected_sell_fee={projected_sell_fee} "
        f"projected_cost={projected_cost_lamports} projected_headroom={projected_headroom} "
        f"pre_reason={pre_reason} start_age_ms={send_start_age_ms} "
        f"prequote_start_age_ms={start_age_ms} latest_follow_age_ms={send_latest_follow_age_ms} "
        f"train_span_ms={cand.train_span_ms} quote_latency_ms={quote_latency_ms}"
    )
    if not allowed:
        counters[f"auth_block_{reason}"] += 1
        return False
    with hist_lock:
        mint_hist = list(hist.get(mint, ()))
    tape_ok, tape_reason = _c3_postquote_tape_check(args, cand, reason, mint_hist)
    if not tape_ok:
        counters[f"auth_block_{tape_reason}"] += 1
        return False
    train_ok, train_reason = _clean_buy_train_tape_check(args, cand, reason, mint_hist)
    if not train_ok:
        counters[f"auth_block_{train_reason}"] += 1
        return False
    send_curve = curve
    send_tokens_raw = int(tokens_raw)
    send_quote_tokens = float(quote_tokens)
    send_buy_fee = int(buy_fee)
    if bool(args.final_projection_check_enabled):
        projection_start = _now_ms()
        try:
            final_curve = broker.bonding_curve(as_pubkey(mint))
            final_tokens_raw, final_buy_fee = broker.quote_pump_buy_tokens(spend_lamports, final_curve, global_cfg)
            final_quote_tokens = float(broker.raw_to_ui(as_pubkey(mint), int(final_tokens_raw)))
            final_post_buy_curve = broker.simulate_post_buy_pump_curve(final_curve, int(final_tokens_raw))
            final_sell_lamports, final_sell_fee = broker.quote_pump_sell_sol(
                int(final_tokens_raw),
                final_post_buy_curve,
                global_cfg,
            )
        except Exception as exc:
            counters["auth_block_final_projection_error"] += 1
            _log(
                "PGG2-GOAL5-SCOUT-FINAL-PROJECTION-BLOCK "
                f"mint={_short(mint)} full_mint={mint} reason=projection_error "
                f"error={str(exc).replace(' ', '_')[:160]}"
            )
            return False
        final_cost = (
            spend_lamports
            + int(args.buy_tx_fee_est_lamports)
            + int(args.sell_fee_est_lamports)
            + int(args.target_lamports)
        )
        final_headroom = int(final_sell_lamports) - int(final_cost)
        min_final_headroom = int(args.final_projection_min_headroom_lamports)
        _log(
            "PGG2-GOAL5-SCOUT-FINAL-PROJECTION "
            f"mint={_short(mint)} full_mint={mint} pass={int(final_headroom >= min_final_headroom)} "
            f"reason={reason} quote_tokens={final_quote_tokens:.6f} buy_fee={int(final_buy_fee)} "
            f"projected_sell={int(final_sell_lamports)} projected_sell_fee={int(final_sell_fee)} "
            f"projected_cost={int(final_cost)} projected_headroom={int(final_headroom)} "
            f"min_headroom={min_final_headroom} latency_ms={_now_ms() - projection_start}"
        )
        if final_headroom < min_final_headroom:
            counters["auth_block_final_projection_negative"] += 1
            return False
        send_curve = final_curve
        send_tokens_raw = int(final_tokens_raw)
        send_quote_tokens = float(final_quote_tokens)
        send_buy_fee = int(final_buy_fee)
    if not args.live:
        counters["dry_ready"] += 1
        _log(f"PGG2-GOAL5-SCOUT-DRY-READY mint={_short(mint)} reason={reason} size_sol={args.size_sol:.6f}")
        return True

    wallet_before = _wallet_lamports(broker, "processed")
    buy_slippage_pct = _buy_slippage_pct_for_reason(args, reason)
    min_tokens_ui = send_quote_tokens * max(0.0, 1.0 - buy_slippage_pct / 100.0)
    buy_quote = broker.build_buy_with_min_tokens_from_curve_snapshot(
        mint,
        float(args.size_sol),
        min_tokens_ui,
        virtual_token_reserves=int(send_curve.virtual_token_reserves),
        virtual_sol_reserves=int(send_curve.virtual_sol_reserves),
        real_token_reserves=int(send_curve.real_token_reserves),
        real_sol_reserves=int(send_curve.real_sol_reserves),
        token_total_supply=int(send_curve.token_total_supply),
        complete=bool(send_curve.complete),
        creator=str(send_curve.creator),
        is_mayhem=bool(getattr(send_curve, "is_mayhem", False)),
        cashback_enabled=bool(getattr(send_curve, "cashback_enabled", False)),
        snapshot_ts_ms=_now_ms(),
    )
    signed_b64 = str(buy_quote.get("txn") or "")
    if not signed_b64:
        raise RuntimeError("goal5_scout_buy_tx_missing")
    _log(
        "PGG2-GOAL5-SCOUT-BUY-SEND "
        f"mint={_short(mint)} reason={reason} wallet_before={wallet_before} "
        f"size_sol={args.size_sol:.6f} quote_tokens={send_quote_tokens:.6f} "
        f"buy_fee={send_buy_fee} buy_slippage_pct={buy_slippage_pct:.3f} min_tokens={min_tokens_ui:.6f}"
    )
    buy_sent_ms = _now_ms()
    buy_sig = broker.send_signed(signed_b64)
    token_raw = _wait_token_balance(broker, mint, float(args.token_wait_sec), int(args.token_poll_ms))
    _log(
        "PGG2-GOAL5-SCOUT-EARLY-TOKEN "
        f"mint={_short(mint)} token_raw={token_raw} buy_sig={buy_sig} wait_ms={_now_ms() - buy_sent_ms}"
    )
    if token_raw <= 0:
        token_raw = _wait_token_balance(broker, mint, float(args.token_late_wait_sec), int(args.token_poll_ms))
    if token_raw <= 0:
        counters["buy_failed_safe"] += 1
        _log(f"PGG2-GOAL5-SCOUT-BUY-FAILED-SAFE mint={_short(mint)} sig={buy_sig}")
        return False

    sell_sig = ""
    expected_out = 0
    min_needed = 0
    sell_attempts = 0
    buy_train_exit = reason == "clean_buy_train_continuation"
    profit_headroom_lamports = (
        int(args.buy_train_sell_min_headroom_lamports)
        if buy_train_exit
        else int(args.sell_min_headroom_lamports)
    )
    opened_at = time.time()
    base_deadline = opened_at + max(0.4, float(args.max_hold_ms) / 1000.0)
    hard_deadline = (
        opened_at + max(float(args.max_hold_ms), float(args.buy_train_max_hold_ms)) / 1000.0
        if buy_train_exit
        else base_deadline
    )
    last_headroom: int | None = None
    best_headroom = -10**18
    last_improve_at = opened_at
    while True:
        token_after_poll = _token_balance_raw_or_zero(broker, mint, "processed")
        if token_after_poll > 0:
            token_raw = token_after_poll
        elif sell_sig:
            break
        wallet_now = _wallet_lamports(broker, "processed")
        rent_lamports = _token_account_lamports(broker, mint, "processed") or ATA_RENT_LAMPORTS
        min_needed = max(
            int(args.sell_floor_lamports),
            wallet_before + int(args.target_lamports) + int(args.sell_fee_est_lamports) - wallet_now - rent_lamports,
        )
        token_ui = broker.raw_to_ui(as_pubkey(mint), int(token_raw))
        quote = broker.build_sell(mint, token_ui, float(args.sell_slippage_pct))
        expected_out = int(float((quote.get("rate") or {}).get("amountOut") or 0.0) * LAMPORTS_PER_SOL)
        headroom = expected_out - min_needed
        now = time.time()
        with hist_lock:
            post_buy_events = [
                x
                for x in list(hist.get(mint, ()))
                if int(x.get("recv_ms") or 0) >= buy_sent_ms
            ]
        post_buy_sells = sum(1 for x in post_buy_events if x.get("kind") == "sell")
        if last_headroom is None or headroom >= last_headroom + int(args.buy_train_improve_step_lamports):
            last_improve_at = now
        best_headroom = max(best_headroom, headroom)
        last_headroom = headroom
        _log(
            "PGG2-GOAL5-SCOUT-SELL-CHECK "
            f"mint={_short(mint)} expected_out={expected_out} min_needed={min_needed} "
            f"headroom={headroom} token_raw={token_raw} "
            f"policy={'buy_train' if buy_train_exit else 'default'} "
            f"profit_headroom={profit_headroom_lamports} post_buy_sells={post_buy_sells} "
            f"best_headroom={best_headroom}"
        )
        should_profit_sell = headroom >= profit_headroom_lamports
        if buy_train_exit:
            improving_recently = (now - last_improve_at) * 1000 <= int(args.buy_train_improve_grace_ms)
            extend_ok = (
                post_buy_sells <= 0
                and headroom >= int(args.buy_train_extend_min_headroom_lamports)
                and improving_recently
                and now < hard_deadline
            )
            should_rescue = (
                (post_buy_sells > 0 and bool(args.buy_train_exit_on_postbuy_sell))
                or headroom <= int(args.loss_rescue_headroom_lamports)
                or (now >= base_deadline and not extend_ok)
                or now >= hard_deadline
            )
        else:
            should_rescue = now >= base_deadline or headroom <= int(args.loss_rescue_headroom_lamports)
        if not should_profit_sell and not should_rescue:
            time.sleep(max(0.025, float(args.sell_poll_ms) / 1000.0))
            continue

        if should_profit_sell:
            mode = "profit"
            sell_min_lamports = min_needed
        else:
            mode = "rescue"
            sell_min_lamports = max(
                int(args.sell_floor_lamports),
                int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL),
            )
        protected = broker.retarget_sell_min_sol(quote, mint, sell_min_lamports / LAMPORTS_PER_SOL)
        sell_sig = broker.send_signed(str(protected["txn"]))
        sell_attempts += 1
        sell_ok = broker.wait_confirmed(sell_sig)
        time.sleep(0.30)
        token_after = _token_balance_raw_or_zero(broker, mint, "processed")
        _log(
            "PGG2-GOAL5-SCOUT-SELL-RESULT "
            f"mint={_short(mint)} mode={mode} attempt={sell_attempts} sig={sell_sig} "
            f"ok={int(bool(sell_ok))} token_raw_after={token_after}"
        )
        if sell_ok and token_after <= 0:
            break
        if token_after > 0:
            token_raw = token_after
        if sell_attempts >= int(args.sell_max_attempts):
            raise RuntimeError("goal5_scout_sell_failed_open_token")

    time.sleep(0.8)
    final_wallet = _wallet_lamports(broker, "processed")
    final_token_raw = _token_balance_raw_or_zero(broker, mint, "processed")
    nonzero, rent_locked = _token_accounts_summary(broker)
    delta = final_wallet - wallet_before
    _log(
        "PGG2-GOAL5-SCOUT-SMOKE-END "
        f"mint={_short(mint)} reason={reason} wallet_before={wallet_before} wallet_after={final_wallet} "
        f"delta_lamports={delta:+} buy_sig={buy_sig} sell_sig={sell_sig} "
        f"expected_out={expected_out} min_needed={min_needed} token_raw_after={final_token_raw} "
        f"nonzero_tokens={nonzero} rent_locked_empty={rent_locked}"
    )
    if final_token_raw > 0:
        raise RuntimeError("goal5_scout_token_residual")
    counters["closed"] += 1
    counters["win" if delta > 0 else "loss"] += 1
    return delta > 0


def _self_test() -> int:
    ns = argparse.Namespace(
        proven_strong_enabled=True,
        micro_c0_highquote_enabled=True,
        cur1_q600_clean_follow_enabled=True,
        small_size005_cur1_q900_follow_enabled=True,
        small_size005_c0_f22_multi_q900_enabled=True,
        small_size005_c3_f13_q720_enabled=True,
        small_size005_c3_strong_fast_follow_enabled=True,
        clean_buy_train_continuation_enabled=True,
        buy_train_min_current_sol=0.25,
        buy_train_max_current_sol=1.00,
        buy_train_min_follow_buys=3,
        buy_train_min_follow_sol=1.10,
        buy_train_min_quote_tokens_ref=250_000.0,
        buy_train_max_quote_tokens_ref=950_000.0,
        buy_train_max_start_age_ms=220,
        buy_train_max_follow_age_ms=220,
        buy_train_max_train_span_ms=650,
        scratch_midquote_enabled=False,
        scratch_midquote_min_quote_tokens_ref=680_000.0,
        scratch_midquote_max_quote_tokens_ref=805_000.0,
        scratch_midquote_min_projected_headroom_lamports=250_000,
        early_cur1_q800_enabled=False,
        cur1_q600_min_follow_current_ratio=0.38,
        c3_buy_slippage_pct=16.0,
        buy_slippage_pct=8.0,
        size_sol=0.005,
        max_start_age_ms=250,
        max_prequote_start_age_ms=250,
        max_send_start_age_ms=420,
        cur1_q600_max_train_span_ms=25,
    )
    cases = [
        ("Ftuc_win_shape", True, 2.072, 2.394, 2.394, 1, 618_769, 80),
        ("Bi6N_win_shape", True, 2.114, 2.071, 2.071, 1, 628_605, 90),
        ("ArVc_loss_block_quote", False, 2.116, 2.311, 2.311, 1, 609_804, 90),
        ("B41Z_loss_block_follow_high", False, 0.600, 1.200, 1.200, 1, 968_220, 90),
        ("c0_f05_q900_scout", True, 0.500, 0.500, 0.500, 1, 976_000, 90),
        ("c0_f065_q833_block", False, 0.500, 0.651, 0.651, 1, 833_920, 90),
        ("c0_f05_q600_block", False, 0.500, 0.520, 0.520, 1, 606_236, 90),
        ("cur1_q600_clean_follow", True, 1.000, 0.402, 0.402, 1, 607_525, 90),
        ("cur1_q662_known_bad_block", False, 1.064, 0.524, 0.524, 1, 669_828, 90),
        ("small005_cur1_f05_q900_b1", True, 1.000, 0.500, 0.500, 1, 879_736, 90),
        ("small005_cur1_f05_q900_b2", True, 1.000, 0.500, 0.502, 2, 942_431, 90),
        ("small005_cur1_f05_low_quote_block", False, 1.000, 0.500, 0.500, 1, 735_495, 90),
        ("small005_cur1_f10_block", False, 1.000, 1.000, 1.000, 1, 853_958, 90),
        ("small005_c0_f22_b3", True, 0.850, 2.200, 2.427, 3, 911_483, 90),
        ("small005_c0_f22_b4", True, 0.850, 2.200, 2.477, 4, 895_619, 90),
        ("small005_c0_f22_b1_block", False, 0.850, 2.200, 2.200, 1, 936_893, 90),
        ("small005_c3_f13_q720", True, 3.300, 1.320, 1.320, 1, 735_495, 90),
        ("small005_c3_f12_q670_shadow_pass", True, 3.300, 1.200, 1.200, 1, 670_173, 90),
        ("small005_c3_f13_q761_cap_pass", True, 3.300, 1.320, 1.320, 1, 761_500, 90),
        ("small005_c3_current306_live_loss_block", False, 3.060, 1.244, 1.244, 1, 695_536, 90),
        ("small005_c3_f13_low_quote_block", False, 3.300, 1.320, 1.320, 1, 650_000, 90),
        ("small005_c3_f13_b2_block", False, 3.300, 1.320, 2.090, 2, 735_495, 90),
        ("small005_c3_strong_fast_f209_b1", True, 3.300, 2.090, 2.090, 1, 620_000, 10, 0, 0, 0),
        ("small005_c3_strong_fast_q761_cap_pass", True, 3.300, 1.650, 1.650, 1, 761_800, 10, 0, 0, 0),
        ("small005_c3_strong_fast_f153_b3", True, 3.000, 1.530, 3.390, 3, 620_000, 10, 0, 0, 10),
        ("small005_c3_strong_fast_slow_span_block", False, 3.000, 1.530, 3.390, 3, 620_000, 80, 0, 0, 60),
        ("small005_c3_strong_fast_high_quote_block", False, 3.300, 2.090, 2.090, 1, 820_000, 10, 0, 0, 0),
        ("clean_buy_train_68gu_shape", True, 0.885938, 0.885938, 2.275875, 3, 520_000, 132),
        ("clean_buy_train_b2_block", False, 0.885938, 0.885938, 1.771875, 2, 520_000, 76),
        ("clean_buy_train_quote_low_block", False, 0.885938, 0.885938, 2.275875, 3, 200_000, 132),
        ("clean_buy_train_current_high_block", False, 3.579950, 8.000000, 9.952000, 3, 520_000, 187),
        ("scratch_midquote_disabled", False, 0.700, 0.400, 0.400, 1, 694_463, 40, 655_995),
        ("early_cur1_q800_disabled", False, 1.250, 0.000, 0.000, 0, 856_000, 40),
        ("early_cur1_q900_mixed_block", False, 1.000, 0.000, 0.000, 0, 994_780, 40),
        ("early_cur1_q600_actual_loss_family_block", False, 1.190, 0.000, 0.000, 0, 669_000, 40),
        ("stale_block", False, 0.415, 0.406, 0.406, 1, 856_000, 400),
        ("cur1_8uyR_win_shape", True, 1.000, 0.500, 0.500, 1, 603_426, 415, 0, 415, 0),
        ("cur1_3c6T_weak_follow_loss_block", False, 1.2926, 0.314927, 0.314927, 1, 642_132, 164, 0, 157, 7),
        ("cur1_2Hur_delayed_follow_loss_block", False, 1.000, 0.471173, 0.471173, 1, 605_782, 341, 0, 121, 220),
    ]
    ok = True
    c3_slip = _buy_slippage_pct_for_reason(ns, "small005_c3_strong_fast_follow")
    non_c3_slip = _buy_slippage_pct_for_reason(ns, "cur1_q600_clean_follow")
    print(f"c3_buy_slippage expected=16.0 got={c3_slip:.1f}")
    print(f"non_c3_buy_slippage expected=8.0 got={non_c3_slip:.1f}")
    ok = ok and abs(c3_slip - 16.0) < 1e-9 and abs(non_c3_slip - 8.0) < 1e-9
    for case in cases:
        name, expected, cur, first, follow, buys, quote, age, *rest = case
        projected = rest[0] if rest else 0
        latest_follow_age = rest[1] if len(rest) > 1 else None
        train_span = rest[2] if len(rest) > 2 else None
        got, reason = _lane_allowed(ns, cur, first, follow, buys, quote, age, projected, latest_follow_age, train_span)
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
        ok = ok and got is expected
    got, reason = _pre_shape_allowed(
        ns,
        0.398,
        0.344,
        2.6372,
        14,
        448,
        latest_follow_age_ms=24,
        train_span_ms=448,
    )
    print(f"clean_buy_train_fresh_follow_preauth expected=1 got={int(got)} reason={reason}")
    ok = ok and got is True
    got, reason = _lane_allowed(
        ns,
        0.398,
        0.344,
        2.6372,
        14,
        520_000,
        560,
        latest_follow_age_ms=118,
        train_span_ms=560,
    )
    print(f"clean_buy_train_fresh_follow_auth expected=1 got={int(got)} reason={reason}")
    ok = ok and got is True
    ns.scratch_midquote_enabled = True
    ns.scratch_midquote_min_quote_tokens_ref = 500_000.0
    ns.scratch_midquote_max_quote_tokens_ref = 820_000.0
    got, reason = _lane_allowed(ns, 0.700, 0.400, 0.400, 1, 694_463, 40, 655_995)
    print(f"scratch_midquote_005_positive expected=1 got={int(got)} reason={reason}")
    ok = ok and got is True
    got, reason = _lane_allowed(ns, 0.700, 0.400, 0.400, 1, 694_463, 40, 240_000)
    print(f"scratch_midquote_low_headroom_block expected=0 got={int(got)} reason={reason}")
    ok = ok and got is False
    ns.scratch_midquote_enabled = False
    got, reason = _pre_shape_allowed(
        ns,
        0.398,
        0.344,
        2.6372,
        14,
        820,
        latest_follow_age_ms=24,
        train_span_ms=820,
    )
    print(f"clean_buy_train_span_block expected=0 got={int(got)} reason={reason}")
    ok = ok and got is False
    tape_ns = argparse.Namespace(
        c3_postquote_tape_check_enabled=True,
        c3_postquote_tape_window_ms=650,
        c3_min_hidden_postfollow_sol=3.0,
        c3_min_hidden_large_buys=3,
        c3_mid_clean_tape_enabled=False,
        c3_strong_fast_low_anchor_block_enabled=True,
        c3_strong_fast_low_anchor_max_current_sol=3.05,
        c3_strong_fast_low_anchor_max_first_follow_sol=1.60,
        c3_strong_fast_low_anchor_min_hidden_sol=3.50,
        c3_large_buy_min_sol=0.30,
        c3_dust_buy_max_sol=0.10,
        c3_max_hidden_dust_buys=2,
        clean_buy_train_tape_check_enabled=True,
        buy_train_tape_window_ms=650,
        buy_train_min_total_buys=4,
        buy_train_min_total_sol=2.40,
        buy_train_min_follow_sol=1.10,
        buy_train_min_large_buys=4,
        buy_train_large_buy_min_sol=0.25,
        buy_train_min_unique_payers=4,
        buy_train_dust_buy_max_sol=0.10,
        buy_train_max_dust_buys=1,
        buy_train_max_top_payer_count=4,
    )
    base_ms = _now_ms() - 100
    c3_cand = ScoutCandidate(
        mint="UnitTestPump",
        start_ms=base_ms,
        current_lamports=int(3.3 * LAMPORTS_PER_SOL),
        first_follow_lamports=int(1.32 * LAMPORTS_PER_SOL),
        follow_lamports=int(1.32 * LAMPORTS_PER_SOL),
        follow_buys=1,
        start_sig="start",
        start_payer="payer_start",
        feed_rec={},
        follow_sigs=["follow"],
        follow_payers=["payer_follow"],
    )

    def buy(sig: str, sol: float, offset_ms: int) -> dict[str, Any]:
        return {"kind": "buy", "sig": sig, "sol_lamports": int(sol * LAMPORTS_PER_SOL), "recv_ms": base_ms + offset_ms}

    win_hist = [
        buy("start", 3.3, 0),
        buy("follow", 1.32, 10),
        buy("h1", 0.99, 20),
        buy("h2", 0.33, 30),
        buy("h3", 0.002, 40),
        buy("h4", 1.25, 50),
        buy("h5", 0.5735, 60),
        buy("h6", 0.0847, 70),
    ]
    loss_hist = [
        buy("start", 3.3, 0),
        buy("follow", 1.32, 10),
        buy("h1", 1.21, 20),
        buy("h2", 0.88, 30),
        buy("d1", 0.06, 40),
        buy("d2", 0.06, 50),
        buy("d3", 0.012, 60),
        buy("d4", 0.06, 70),
        buy("h3", 0.189731, 80),
    ]
    mid_clean_hist = [
        buy("start", 3.3, 0),
        buy("follow", 1.32, 10),
        buy("h1", 1.21, 25),
        buy("h2", 0.88, 40),
    ]
    high_total_low_dust_hist = [
        buy("start", 3.3, 0),
        buy("follow", 1.32, 10),
        buy("h1", 1.65, 25),
        buy("h2", 1.98, 45),
        buy("d1", 0.002, 80),
    ]
    for name, expected, hist_rows in [
        ("c3_tape_9yY4_style_pass", True, win_hist),
        ("c3_tape_AJq6_style_block", False, loss_hist),
        ("c3_tape_mid_clean_9PrU_style_block", False, mid_clean_hist),
        ("c3_tape_83WE_style_pass", True, high_total_low_dust_hist),
    ]:
        got, reason = _c3_postquote_tape_check(tape_ns, c3_cand, "small005_c3_f13_q720", hist_rows)
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
        ok = ok and got is expected
    strong_c3_cand = ScoutCandidate(
        mint="StrongFastC3Pump",
        start_ms=base_ms,
        current_lamports=int(3.0 * LAMPORTS_PER_SOL),
        first_follow_lamports=int(1.53 * LAMPORTS_PER_SOL),
        follow_lamports=int(1.53 * LAMPORTS_PER_SOL),
        follow_buys=1,
        start_sig="start",
        start_payer="payer_start",
        feed_rec={},
        follow_sigs=["follow"],
        follow_payers=["payer_follow"],
    )
    strong_c3_hist = [
        buy("start", 3.0, 0),
        buy("follow", 1.53, 10),
        buy("h1", 1.21, 25),
        buy("h2", 0.88, 40),
    ]
    got, reason = _c3_postquote_tape_check(
        tape_ns,
        strong_c3_cand,
        "small005_c3_strong_fast_follow",
        strong_c3_hist,
    )
    print(f"c3_tape_strong_fast_mid_clean_block expected=0 got={int(got)} reason={reason}")
    ok = ok and got is False
    strong_c3_low_anchor_loss_cand = ScoutCandidate(
        mint="StrongFastLowAnchorLossPump",
        start_ms=base_ms,
        current_lamports=int(3.0 * LAMPORTS_PER_SOL),
        first_follow_lamports=int(1.5 * LAMPORTS_PER_SOL),
        follow_lamports=int(1.5 * LAMPORTS_PER_SOL),
        follow_buys=1,
        start_sig="start",
        start_payer="payer_start",
        feed_rec={},
        follow_sigs=["follow"],
        follow_payers=["payer_follow"],
    )
    strong_c3_low_anchor_loss_hist = [
        buy("start", 3.0, 0),
        buy("follow", 1.5, 10),
        buy("h1", 1.50, 25),
        buy("h2", 1.50, 40),
        buy("h3", 0.377242, 60),
    ]
    got, reason = _c3_postquote_tape_check(
        tape_ns,
        strong_c3_low_anchor_loss_cand,
        "small005_c3_strong_fast_follow",
        strong_c3_low_anchor_loss_hist,
    )
    print(f"c3_tape_strong_fast_low_anchor_loss_block expected=0 got={int(got)} reason={reason}")
    ok = ok and got is False
    strong_c3_high_follow_win_cand = ScoutCandidate(
        mint="StrongFastHighFollowWinPump",
        start_ms=base_ms,
        current_lamports=int(3.116955 * LAMPORTS_PER_SOL),
        first_follow_lamports=int(2.133045 * LAMPORTS_PER_SOL),
        follow_lamports=int(2.133045 * LAMPORTS_PER_SOL),
        follow_buys=1,
        start_sig="start",
        start_payer="payer_start",
        feed_rec={},
        follow_sigs=["follow"],
        follow_payers=["payer_follow"],
    )
    strong_c3_high_follow_win_hist = [
        buy("start", 3.116955, 0),
        buy("follow", 2.133045, 10),
        buy("h1", 1.65, 25),
        buy("h2", 1.515428, 45),
    ]
    got, reason = _c3_postquote_tape_check(
        tape_ns,
        strong_c3_high_follow_win_cand,
        "small005_c3_strong_fast_follow",
        strong_c3_high_follow_win_hist,
    )
    print(f"c3_tape_strong_fast_high_follow_win_pass expected=1 got={int(got)} reason={reason}")
    ok = ok and got is True
    c3_low_current_cand = ScoutCandidate(
        mint="LowCurrentC3Pump",
        start_ms=base_ms,
        current_lamports=int(3.06 * LAMPORTS_PER_SOL),
        first_follow_lamports=int(1.2444 * LAMPORTS_PER_SOL),
        follow_lamports=int(1.2444 * LAMPORTS_PER_SOL),
        follow_buys=1,
        start_sig="start",
        start_payer="payer_start",
        feed_rec={},
        follow_sigs=["follow"],
        follow_payers=["payer_follow"],
    )
    low_current_hist = [
        buy("start", 3.06, 0),
        buy("follow", 1.2444, 10),
        buy("h1", 1.2240, 40),
    ]
    got, reason = _c3_postquote_tape_check(
        tape_ns,
        c3_low_current_cand,
        "small005_c3_f13_q720",
        low_current_hist,
    )
    print(f"c3_tape_low_current_live_loss_block expected=0 got={int(got)} reason={reason}")
    ok = ok and got is False
    train_cand = ScoutCandidate(
        mint="TrainUnitPump",
        start_ms=base_ms,
        current_lamports=int(0.885938 * LAMPORTS_PER_SOL),
        first_follow_lamports=int(0.885938 * LAMPORTS_PER_SOL),
        follow_lamports=int(2.275875 * LAMPORTS_PER_SOL),
        follow_buys=3,
        start_sig="s0",
        start_payer="p0",
        feed_rec={},
        follow_sigs=["s1", "s2", "s3"],
        follow_payers=["p1", "p2", "p3"],
    )
    train_pass_hist = [
        {"kind": "buy", "sig": "s0", "sol_lamports": int(0.885938 * LAMPORTS_PER_SOL), "recv_ms": base_ms, "fee_payer": "p0"},
        {"kind": "buy", "sig": "s1", "sol_lamports": int(0.885938 * LAMPORTS_PER_SOL), "recv_ms": base_ms + 20, "fee_payer": "p1"},
        {"kind": "buy", "sig": "s2", "sol_lamports": int(0.504000 * LAMPORTS_PER_SOL), "recv_ms": base_ms + 45, "fee_payer": "p2"},
        {"kind": "buy", "sig": "s3", "sol_lamports": int(0.379688 * LAMPORTS_PER_SOL), "recv_ms": base_ms + 75, "fee_payer": "p3"},
    ]
    train_sell_hist = train_pass_hist + [
        {"kind": "sell", "sig": "sell0", "sol_lamports": 0, "recv_ms": base_ms + 90, "fee_payer": "p4"}
    ]
    train_dust_hist = train_pass_hist[:2] + [
        {"kind": "buy", "sig": "d0", "sol_lamports": int(0.05 * LAMPORTS_PER_SOL), "recv_ms": base_ms + 45, "fee_payer": "p2"},
        {"kind": "buy", "sig": "d1", "sol_lamports": int(0.05 * LAMPORTS_PER_SOL), "recv_ms": base_ms + 75, "fee_payer": "p3"},
    ]
    for name, expected, hist_rows in [
        ("buy_train_tape_68gu_style_pass", True, train_pass_hist),
        ("buy_train_tape_sell_block", False, train_sell_hist),
        ("buy_train_tape_dust_block", False, train_dust_hist),
    ]:
        got, reason = _clean_buy_train_tape_check(tape_ns, train_cand, "clean_buy_train_continuation", hist_rows)
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
        ok = ok and got is expected
    print("GOAL5_SPEED_SCOUT_SELF_TEST_OK" if ok else "GOAL5_SPEED_SCOUT_SELF_TEST_FAIL")
    return 0 if ok else 1


def run(args: argparse.Namespace) -> int:
    grpc, _Pubkey, _Signature, protos = _proto_imports()
    _geyser_pb2, geyser_pb2_grpc = protos
    token = os.environ.get(str(args.token_env), "")
    if not token:
        _log(f"PGG2-GOAL5-SCOUT-FATAL missing_token_env={args.token_env}")
        return 2
    broker = _make_broker(args)
    if args.live:
        nonzero, rent_locked = _token_accounts_summary(broker)
        wallet = _wallet_lamports(broker, "processed")
        _log(
            "PGG2-GOAL5-SCOUT-STATE "
            f"wallet_sol={wallet/LAMPORTS_PER_SOL:.9f} nonzero_tokens={nonzero} rent_locked_empty={rent_locked}"
        )
        if nonzero:
            _log("PGG2-GOAL5-SCOUT-ABORT reason=nonzero_token_accounts")
            return 2

    channel = grpc.secure_channel(str(args.endpoint), grpc.ssl_channel_credentials())
    stub = geyser_pb2_grpc.GeyserStub(channel)
    metadata = [(str(args.metadata_key), token)]
    hist: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=64))
    seen: set[str] = set()
    hist_lock = threading.Lock()
    stop_event = threading.Event()
    event_q: queue.Queue[tuple[dict[str, Any], list[dict[str, Any]]]] = queue.Queue(maxsize=int(args.feed_queue_size))
    counters: Counter[str] = Counter()
    started = time.time()
    _log(
        "PGG2-GOAL5-SCOUT-START "
        f"live={int(args.live)} seconds={args.seconds} target_closes={args.target_closes} "
        f"size_sol={args.size_sol:.6f} proven_strong={int(args.proven_strong_enabled)} "
        f"micro_c0_highquote={int(args.micro_c0_highquote_enabled)} "
        f"cur1_q600_clean={int(args.cur1_q600_clean_follow_enabled)} "
        f"cur1_q600_min_follow_current_ratio={args.cur1_q600_min_follow_current_ratio:.3f} "
        f"cur1_q600_max_train_span_ms={args.cur1_q600_max_train_span_ms} "
        f"small005_cur1_q900={int(args.small_size005_cur1_q900_follow_enabled)} "
        f"small005_c0_f22_multi={int(args.small_size005_c0_f22_multi_q900_enabled)} "
        f"small005_c3_f13={int(args.small_size005_c3_f13_q720_enabled)} "
        f"small005_c3_strong_fast={int(args.small_size005_c3_strong_fast_follow_enabled)} "
        f"clean_buy_train={int(args.clean_buy_train_continuation_enabled)} "
        f"scratch_midquote={int(args.scratch_midquote_enabled)} "
        f"scratch_midquote_quote=[{args.scratch_midquote_min_quote_tokens_ref:.0f},{args.scratch_midquote_max_quote_tokens_ref:.0f}] "
        f"scratch_midquote_min_headroom={args.scratch_midquote_min_projected_headroom_lamports} "
        f"early_cur1_q800={int(args.early_cur1_q800_enabled)} "
        f"fresh_start_no_prior_buy_ms={args.fresh_start_no_prior_buy_ms} "
        f"max_prequote_start_age_ms={args.max_prequote_start_age_ms} "
        f"max_send_start_age_ms={args.max_send_start_age_ms} "
        f"sell_min_headroom={args.sell_min_headroom_lamports} max_hold_ms={args.max_hold_ms} "
        f"buy_slippage_pct={args.buy_slippage_pct:.3f} c3_buy_slippage_pct={args.c3_buy_slippage_pct:.3f} "
        f"c3_tape_check={int(args.c3_postquote_tape_check_enabled)} "
        f"c3_mid_clean_tape={int(args.c3_mid_clean_tape_enabled)} "
        f"c3_low_anchor_block={int(args.c3_strong_fast_low_anchor_block_enabled)} "
        f"buy_train_quote_ref=[{args.buy_train_min_quote_tokens_ref:.0f},{args.buy_train_max_quote_tokens_ref:.0f}] "
        f"buy_train_max_follow_age_ms={args.buy_train_max_follow_age_ms} "
        f"buy_train_max_train_span_ms={args.buy_train_max_train_span_ms} "
        f"buy_train_tape_check={int(args.clean_buy_train_tape_check_enabled)} "
        f"buy_train_sell_headroom={args.buy_train_sell_min_headroom_lamports} "
        f"buy_train_max_hold_ms={args.buy_train_max_hold_ms} "
        f"buy_train_extend_min_headroom={args.buy_train_extend_min_headroom_lamports} "
        f"final_projection_check={int(args.final_projection_check_enabled)} "
        f"final_projection_min_headroom={args.final_projection_min_headroom_lamports}"
    )

    def feed_reader() -> None:
        try:
            for update in stub.Subscribe(_request_iter(args), metadata=metadata):
                if stop_event.is_set():
                    break
                rec = _decode_pump(update)
                if not rec:
                    continue
                sig = str(rec["sig"])
                with hist_lock:
                    if sig in seen:
                        continue
                    seen.add(sig)
                    mint = str(rec["mint"])
                    prior = list(hist[mint])
                    hist[mint].append(rec)
                try:
                    event_q.put_nowait((rec, prior))
                except queue.Full:
                    _log("PGG2-GOAL5-SCOUT-FEED-DROP reason=queue_full")
        except grpc.RpcError as exc:
            if not stop_event.is_set():
                _log(f"PGG2-GOAL5-SCOUT-GRPC-ERROR code={exc.code()} details={str(exc.details())[:220]}")
        except Exception as exc:
            if not stop_event.is_set():
                _log(f"PGG2-GOAL5-SCOUT-FEED-ERROR err={type(exc).__name__}:{str(exc)[:220]}")

    reader = threading.Thread(target=feed_reader, name="goal5-feed-reader", daemon=True)
    reader.start()
    try:
        while True:
            now = _now_ms()
            if time.time() - started > int(args.seconds):
                counters["timeout"] += 1
                break
            try:
                rec, prior = event_q.get(timeout=0.05)
            except queue.Empty:
                continue
            sig = str(rec["sig"])
            mint = str(rec["mint"])
            if rec["kind"] != "buy":
                counters["sell_event"] += 1
                continue

            candidates: list[ScoutCandidate] = []
            if bool(args.early_cur1_q800_enabled):
                current_rec_sol = int(rec["sol_lamports"]) / LAMPORTS_PER_SOL
                recent_prior = [x for x in prior if now - int(x["recv_ms"]) <= 1000]
                if (
                    1.15 <= current_rec_sol <= 1.35
                    and not any(x["kind"] == "sell" for x in recent_prior)
                ):
                    candidates.append(
                        ScoutCandidate(
                            mint=mint,
                            start_ms=now,
                            current_lamports=int(rec["sol_lamports"]),
                            first_follow_lamports=0,
                            follow_lamports=0,
                            follow_buys=0,
                            start_sig=sig,
                            start_payer=str(rec.get("fee_payer") or ""),
                            feed_rec=dict(rec),
                            latest_follow_ms=int(rec.get("recv_ms") or now),
                            train_span_ms=0,
                        )
                    )

            window_ms = int(args.max_rearm_age_ms)
            recent_buys = [x for x in prior if x["kind"] == "buy" and now - int(x["recv_ms"]) <= window_ms]
            block = "no_start"
            for start in reversed(recent_buys):
                start_ms = int(start["recv_ms"])
                if any(x["kind"] == "sell" and start_ms - int(x["recv_ms"]) <= 1000 for x in prior if int(x["recv_ms"]) < start_ms):
                    block = "prior_sell"
                    continue
                if any(
                    x["kind"] == "buy"
                    and 0 < start_ms - int(x["recv_ms"]) <= int(args.fresh_start_no_prior_buy_ms)
                    for x in prior
                    if int(x["recv_ms"]) < start_ms
                ):
                    block = "fresh_start_prior_buy"
                    continue
                after_start = [x for x in prior if int(x["recv_ms"]) > start_ms and now - int(x["recv_ms"]) <= window_ms]
                if any(x["kind"] == "sell" for x in after_start):
                    block = "sell_between"
                    continue
                follow_buys = [x for x in after_start if x["kind"] == "buy"] + [rec]
                if not follow_buys:
                    block = "no_follow"
                    continue
                follow_recv_times = [int(x.get("recv_ms") or now) for x in follow_buys]
                latest_follow_ms = max(follow_recv_times) if follow_recv_times else now
                candidates.append(
                    ScoutCandidate(
                        mint=mint,
                        start_ms=start_ms,
                        current_lamports=int(start["sol_lamports"]),
                        first_follow_lamports=int(follow_buys[0]["sol_lamports"]),
                        follow_lamports=sum(int(x["sol_lamports"]) for x in follow_buys),
                        follow_buys=len(follow_buys),
                        start_sig=str(start["sig"]),
                        start_payer=str(start.get("fee_payer") or ""),
                        feed_rec=dict(start),
                        follow_sigs=[str(x.get("sig") or "") for x in follow_buys],
                        follow_payers=[str(x.get("fee_payer") or "") for x in follow_buys],
                        latest_follow_ms=latest_follow_ms,
                        train_span_ms=max(0, latest_follow_ms - start_ms),
                    )
                )
            if not candidates:
                counters[f"block_{block}"] += 1
                continue

            for cand in candidates:
                counters["candidate"] += 1
                ok = _trade(args, broker, cand, counters, hist, hist_lock)
                if ok or counters["closed"] or counters["buy_failed_safe"]:
                    break
            if args.live and (counters["closed"] or counters["buy_failed_safe"]):
                break
            if args.target_closes > 0 and counters["closed"] >= int(args.target_closes):
                break
            if (not args.live) and args.stop_on_dry_ready and counters["dry_ready"]:
                break
    finally:
        stop_event.set()
        try:
            channel.close()
        except Exception:
            pass
        _log("PGG2-GOAL5-SCOUT-FINAL " + " ".join(f"{k}={v}" for k, v in counters.most_common(60)))
    if args.live:
        return 0 if counters["win"] > 0 and counters["loss"] == 0 else 1
    return 0 if counters["dry_ready"] > 0 else 1


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--endpoint", default=os.environ.get("PUBLICNODE_YELLOWSTONE_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"))
    ap.add_argument("--token-env", default=os.environ.get("PUBLICNODE_TOKEN_ENV", "PUBLICNODE_X_TOKEN"))
    ap.add_argument("--metadata-key", default=os.environ.get("PUBLICNODE_GRPC_METADATA_KEY", "x-token"))
    ap.add_argument("--ping-seconds", type=int, default=15)
    ap.add_argument("--rpc-url", default=_default_rpc_url())
    ap.add_argument("--size-sol", type=float, default=0.030)
    ap.add_argument("--tip-lamports", type=int, default=5000)
    ap.add_argument("--priority-fee-sol", type=float, default=0.000005)
    ap.add_argument("--max-rearm-age-ms", type=int, default=450)
    ap.add_argument("--max-start-age-ms", type=int, default=250)
    ap.add_argument("--max-prequote-start-age-ms", type=int, default=250)
    ap.add_argument("--max-send-start-age-ms", type=int, default=420)
    ap.add_argument("--fresh-start-no-prior-buy-ms", type=int, default=650)
    ap.add_argument("--proven-strong-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--micro-c0-highquote-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--cur1-q600-clean-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--cur1-q600-min-follow-current-ratio", type=float, default=0.38)
    ap.add_argument("--cur1-q600-max-train-span-ms", type=int, default=25)
    ap.add_argument("--small-size005-cur1-q900-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--small-size005-c0-f22-multi-q900-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--small-size005-c3-f13-q720-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--small-size005-c3-strong-fast-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--clean-buy-train-continuation-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--buy-train-min-current-sol", type=float, default=0.25)
    ap.add_argument("--buy-train-max-current-sol", type=float, default=1.00)
    ap.add_argument("--buy-train-min-follow-buys", type=int, default=3)
    ap.add_argument("--buy-train-min-follow-sol", type=float, default=1.10)
    ap.add_argument("--buy-train-max-start-age-ms", type=int, default=220)
    ap.add_argument("--buy-train-max-follow-age-ms", type=int, default=220)
    ap.add_argument("--buy-train-max-train-span-ms", type=int, default=650)
    ap.add_argument("--buy-train-min-quote-tokens-ref", type=float, default=250_000.0)
    ap.add_argument("--buy-train-max-quote-tokens-ref", type=float, default=950_000.0)
    ap.add_argument("--clean-buy-train-tape-check-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--buy-train-tape-window-ms", type=int, default=650)
    ap.add_argument("--buy-train-min-total-buys", type=int, default=4)
    ap.add_argument("--buy-train-min-total-sol", type=float, default=2.40)
    ap.add_argument("--buy-train-min-large-buys", type=int, default=4)
    ap.add_argument("--buy-train-large-buy-min-sol", type=float, default=0.25)
    ap.add_argument("--buy-train-min-unique-payers", type=int, default=4)
    ap.add_argument("--buy-train-dust-buy-max-sol", type=float, default=0.10)
    ap.add_argument("--buy-train-max-dust-buys", type=int, default=1)
    ap.add_argument("--buy-train-max-top-payer-count", type=int, default=4)
    ap.add_argument("--buy-train-sell-min-headroom-lamports", type=int, default=20_000)
    ap.add_argument("--buy-train-max-hold-ms", type=int, default=5200)
    ap.add_argument("--buy-train-extend-min-headroom-lamports", type=int, default=-180_000)
    ap.add_argument("--buy-train-improve-step-lamports", type=int, default=5_000)
    ap.add_argument("--buy-train-improve-grace-ms", type=int, default=900)
    ap.add_argument("--buy-train-exit-on-postbuy-sell", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--final-projection-check-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--final-projection-min-headroom-lamports", type=int, default=0)
    ap.add_argument("--scratch-midquote-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--scratch-midquote-min-quote-tokens-ref", type=float, default=680_000.0)
    ap.add_argument("--scratch-midquote-max-quote-tokens-ref", type=float, default=805_000.0)
    ap.add_argument("--scratch-midquote-min-projected-headroom-lamports", type=int, default=250_000)
    ap.add_argument("--early-cur1-q800-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--buy-slippage-pct", type=float, default=8.0)
    ap.add_argument("--c3-buy-slippage-pct", type=float, default=16.0)
    ap.add_argument("--sell-slippage-pct", type=float, default=4.0)
    ap.add_argument("--target-lamports", type=int, default=0)
    ap.add_argument("--buy-tx-fee-est-lamports", type=int, default=50_000)
    ap.add_argument("--sell-fee-est-lamports", type=int, default=30_000)
    ap.add_argument("--sell-min-headroom-lamports", type=int, default=700_000)
    ap.add_argument("--sell-floor-lamports", type=int, default=100)
    ap.add_argument("--max-hold-ms", type=int, default=6500)
    ap.add_argument("--sell-poll-ms", type=int, default=200)
    ap.add_argument("--sell-max-attempts", type=int, default=3)
    ap.add_argument("--loss-rescue-headroom-lamports", type=int, default=0)
    ap.add_argument("--token-wait-sec", type=float, default=0.60)
    ap.add_argument("--token-late-wait-sec", type=float, default=1.25)
    ap.add_argument("--token-poll-ms", type=int, default=20)
    ap.add_argument("--target-closes", type=int, default=1)
    ap.add_argument("--stop-on-dry-ready", action="store_true")
    ap.add_argument("--feed-queue-size", type=int, default=4096)
    ap.add_argument("--c3-postquote-tape-check-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--c3-postquote-tape-window-ms", type=int, default=650)
    ap.add_argument("--c3-min-hidden-postfollow-sol", type=float, default=3.0)
    ap.add_argument("--c3-min-hidden-large-buys", type=int, default=3)
    ap.add_argument("--c3-mid-clean-tape-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--c3-strong-fast-low-anchor-block-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--c3-strong-fast-low-anchor-max-current-sol", type=float, default=3.05)
    ap.add_argument("--c3-strong-fast-low-anchor-max-first-follow-sol", type=float, default=1.60)
    ap.add_argument("--c3-strong-fast-low-anchor-min-hidden-sol", type=float, default=3.50)
    ap.add_argument("--c3-large-buy-min-sol", type=float, default=0.30)
    ap.add_argument("--c3-dust-buy-max-sol", type=float, default=0.10)
    ap.add_argument("--c3-max-hidden-dust-buys", type=int, default=2)
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
