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


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _quote_ref_030(quote_tokens: float, size_sol: float) -> float:
    if size_sol <= 0:
        return 0.0
    return float(quote_tokens) * (0.030 / float(size_sol))


def _pre_shape_allowed(
    args: argparse.Namespace,
    current_sol: float,
    first_follow_sol: float,
    follow_sol: float,
    follow_buys: int,
    start_age_ms: int,
) -> tuple[bool, str]:
    if start_age_ms > int(args.max_prequote_start_age_ms):
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
) -> tuple[bool, str]:
    max_send_age_ms = int(getattr(args, "max_send_start_age_ms", args.max_start_age_ms))
    if start_age_ms > max_send_age_ms:
        return False, "stale_start"
    projected_headroom = -10**18 if projected_headroom_lamports is None else int(projected_headroom_lamports)
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
        and 700_000 <= quote_tokens <= 760_000
    ):
        return True, "small005_c3_f13_q720"
    if (
        args.scratch_midquote_enabled
        and 0.65 <= current_sol <= 1.95
        and follow_buys == 1
        and 0.38 <= first_follow_sol <= 1.25
        and 0.38 <= follow_sol <= 1.25
        and 680_000 <= quote_tokens <= 805_000
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
    if reason != "small005_c3_f13_q720" or not bool(args.c3_postquote_tape_check_enabled):
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
    pass_check = (
        not sells
        and hidden_sol >= float(args.c3_min_hidden_postfollow_sol)
        and hidden_large >= int(args.c3_min_hidden_large_buys)
        and hidden_dust <= int(args.c3_max_hidden_dust_buys)
    )
    reason_out = "c3_tape_ok" if pass_check else "c3_tape_block"
    _log(
        "PGG2-GOAL5-C3-POSTQUOTE-TAPE "
        f"mint={_short(cand.mint)} full_mint={cand.mint} pass={int(pass_check)} reason={reason_out} "
        f"start_sig={cand.start_sig} follow_sigs={','.join(cand.follow_sigs)} "
        f"start_payer={cand.start_payer} follow_payers={','.join(cand.follow_payers)} "
        f"events_after_start={len(after_start)} hidden_buys={len(hidden_buys)} "
        f"hidden_sol={hidden_sol:.6f} hidden_large={hidden_large} hidden_dust={hidden_dust} "
        f"sells_after_start={len(sells)} min_hidden_sol={float(args.c3_min_hidden_postfollow_sol):.6f} "
        f"min_hidden_large={int(args.c3_min_hidden_large_buys)} max_hidden_dust={int(args.c3_max_hidden_dust_buys)}"
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
    start_age_ms = _now_ms() - cand.start_ms
    _remember_feed_accounts(broker, cand.feed_rec)

    pre_allowed, pre_reason = _pre_shape_allowed(
        args,
        current_sol,
        first_follow_sol,
        follow_sol,
        cand.follow_buys,
        start_age_ms,
    )
    if not pre_allowed:
        counters[f"preauth_block_{pre_reason}"] += 1
        _log(
            "PGG2-GOAL5-SCOUT-PREAUTH "
            f"mint={_short(mint)} full_mint={mint} pass=0 reason={pre_reason} "
            f"current_sol={current_sol:.6f} first_follow_sol={first_follow_sol:.6f} "
            f"follow_sol={follow_sol:.6f} follow_buys={cand.follow_buys} "
            f"start_age_ms={start_age_ms}"
        )
        return False

    quote_start = _now_ms()
    curve = broker.bonding_curve(as_pubkey(mint))
    global_cfg = broker.pump_global()
    spend_lamports = int(float(args.size_sol) * LAMPORTS_PER_SOL)
    tokens_raw, buy_fee = broker.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
    quote_tokens = float(broker.raw_to_ui(as_pubkey(mint), int(tokens_raw)))
    quote_tokens_ref = _quote_ref_030(quote_tokens, float(args.size_sol))
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
    if bool(args.scratch_midquote_enabled):
        post_buy_curve = broker.simulate_post_buy_pump_curve(curve, int(tokens_raw))
        projected_sell_lamports, projected_sell_fee = broker.quote_pump_sell_sol(
            int(tokens_raw),
            post_buy_curve,
            global_cfg,
        )
        projected_headroom = int(projected_sell_lamports) - int(projected_cost_lamports)
    send_start_age_ms = _now_ms() - cand.start_ms
    allowed, reason = _lane_allowed(
        args,
        current_sol,
        first_follow_sol,
        follow_sol,
        cand.follow_buys,
        quote_tokens_ref,
        send_start_age_ms,
        projected_headroom,
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
        f"prequote_start_age_ms={start_age_ms} quote_latency_ms={quote_latency_ms}"
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
    if not args.live:
        counters["dry_ready"] += 1
        _log(f"PGG2-GOAL5-SCOUT-DRY-READY mint={_short(mint)} reason={reason} size_sol={args.size_sol:.6f}")
        return True

    wallet_before = _wallet_lamports(broker, "processed")
    min_tokens_ui = quote_tokens * max(0.0, 1.0 - float(args.buy_slippage_pct) / 100.0)
    buy_quote = broker.build_buy_with_min_tokens_from_curve_snapshot(
        mint,
        float(args.size_sol),
        min_tokens_ui,
        virtual_token_reserves=int(curve.virtual_token_reserves),
        virtual_sol_reserves=int(curve.virtual_sol_reserves),
        real_token_reserves=int(curve.real_token_reserves),
        real_sol_reserves=int(curve.real_sol_reserves),
        token_total_supply=int(curve.token_total_supply),
        complete=bool(curve.complete),
        creator=str(curve.creator),
        is_mayhem=bool(getattr(curve, "is_mayhem", False)),
        cashback_enabled=bool(getattr(curve, "cashback_enabled", False)),
        snapshot_ts_ms=_now_ms(),
    )
    signed_b64 = str(buy_quote.get("txn") or "")
    if not signed_b64:
        raise RuntimeError("goal5_scout_buy_tx_missing")
    _log(
        "PGG2-GOAL5-SCOUT-BUY-SEND "
        f"mint={_short(mint)} reason={reason} wallet_before={wallet_before} "
        f"size_sol={args.size_sol:.6f} quote_tokens={quote_tokens:.6f} min_tokens={min_tokens_ui:.6f}"
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
    deadline = time.time() + max(0.4, float(args.max_hold_ms) / 1000.0)
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
        _log(
            "PGG2-GOAL5-SCOUT-SELL-CHECK "
            f"mint={_short(mint)} expected_out={expected_out} min_needed={min_needed} "
            f"headroom={headroom} token_raw={token_raw}"
        )
        should_profit_sell = headroom >= int(args.sell_min_headroom_lamports)
        should_rescue = time.time() >= deadline or headroom <= int(args.loss_rescue_headroom_lamports)
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
        scratch_midquote_enabled=False,
        scratch_midquote_min_projected_headroom_lamports=250_000,
        early_cur1_q800_enabled=False,
        size_sol=0.005,
        max_start_age_ms=250,
        max_prequote_start_age_ms=250,
        max_send_start_age_ms=250,
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
        ("small005_c3_f13_low_quote_block", False, 3.300, 1.320, 1.320, 1, 650_000, 90),
        ("small005_c3_f13_b2_block", False, 3.300, 1.320, 2.090, 2, 735_495, 90),
        ("scratch_midquote_disabled", False, 0.700, 0.400, 0.400, 1, 694_463, 40, 655_995),
        ("early_cur1_q800_disabled", False, 1.250, 0.000, 0.000, 0, 856_000, 40),
        ("early_cur1_q900_mixed_block", False, 1.000, 0.000, 0.000, 0, 994_780, 40),
        ("early_cur1_q600_actual_loss_family_block", False, 1.190, 0.000, 0.000, 0, 669_000, 40),
        ("stale_block", False, 0.415, 0.406, 0.406, 1, 856_000, 400),
    ]
    ok = True
    for case in cases:
        name, expected, cur, first, follow, buys, quote, age, *rest = case
        projected = rest[0] if rest else 0
        got, reason = _lane_allowed(ns, cur, first, follow, buys, quote, age, projected)
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
        ok = ok and got is expected
    tape_ns = argparse.Namespace(
        c3_postquote_tape_check_enabled=True,
        c3_postquote_tape_window_ms=650,
        c3_min_hidden_postfollow_sol=3.0,
        c3_min_hidden_large_buys=3,
        c3_large_buy_min_sol=0.30,
        c3_dust_buy_max_sol=0.10,
        c3_max_hidden_dust_buys=2,
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
    for name, expected, hist_rows in [
        ("c3_tape_9yY4_style_pass", True, win_hist),
        ("c3_tape_AJq6_style_block", False, loss_hist),
    ]:
        got, reason = _c3_postquote_tape_check(tape_ns, c3_cand, "small005_c3_f13_q720", hist_rows)
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
        f"small005_cur1_q900={int(args.small_size005_cur1_q900_follow_enabled)} "
        f"small005_c0_f22_multi={int(args.small_size005_c0_f22_multi_q900_enabled)} "
        f"small005_c3_f13={int(args.small_size005_c3_f13_q720_enabled)} "
        f"scratch_midquote={int(args.scratch_midquote_enabled)} "
        f"scratch_midquote_min_headroom={args.scratch_midquote_min_projected_headroom_lamports} "
        f"early_cur1_q800={int(args.early_cur1_q800_enabled)} "
        f"fresh_start_no_prior_buy_ms={args.fresh_start_no_prior_buy_ms} "
        f"max_prequote_start_age_ms={args.max_prequote_start_age_ms} "
        f"max_send_start_age_ms={args.max_send_start_age_ms} "
        f"sell_min_headroom={args.sell_min_headroom_lamports} max_hold_ms={args.max_hold_ms} "
        f"c3_tape_check={int(args.c3_postquote_tape_check_enabled)}"
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
    ap.add_argument("--small-size005-cur1-q900-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--small-size005-c0-f22-multi-q900-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--small-size005-c3-f13-q720-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--scratch-midquote-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--scratch-midquote-min-projected-headroom-lamports", type=int, default=250_000)
    ap.add_argument("--early-cur1-q800-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--buy-slippage-pct", type=float, default=8.0)
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
    ap.add_argument("--c3-large-buy-min-sol", type=float, default=0.30)
    ap.add_argument("--c3-dust-buy-max-sol", type=float, default=0.10)
    ap.add_argument("--c3-max-hidden-dust-buys", type=int, default=2)
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
