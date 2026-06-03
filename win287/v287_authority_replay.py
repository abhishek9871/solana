#!/usr/bin/env python3
"""Replay narrow V287 seed-prior send-authority shapes.

This is an import-only check. It does not connect to Solana and does not send.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "pgg2_v287_selected_band_live_smoke.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("v287_runner_under_test", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {RUNNER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def set_env() -> None:
    values = {
        "V287_SELECTED_SEED_PRIOR_GENERIC_POSITIVE_WATCH_SEND_ENABLED": "0",
        "V287_SELECTED_SEED_PRIOR_GENERIC_WEAK_WATCH_SEND_ENABLED": "0",
        "V287_SELECTED_SEED_PRIOR_ALLOW_DRIFT_ONLY_NEGATIVE_ROUNDTRIP": "1",
        "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_NO_POSTPLAN_PCT": "8.00",
        "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_CURRENT_SOL": "2.50",
        "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_PRE_ENTRY_SOL": "3.50",
        "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_PRE_ENTRY_BUYS": "2",
        "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_QUOTE_TOKENS": "825000",
        "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MIN_TOP_SHARE": "0.999",
        "V287_SELECTED_SEED_PRIOR_REQUIRED_POSTPLAN_MIN_SOL": "0.50",
        "V287_SELECTED_SEED_PRIOR_REQUIRE_POSTPLAN_ABOVE_SOL": "3.00",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_POSTPLAN_SEND_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_MIN_DRIFT_PCT": "2.00",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_MIN_QUOTE_TOKENS": "680000",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_QUOTE_TOKENS": "760000",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_REARM_LAG_MS": "650",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_PRE_ENTRY_SOL": "4.00",
        "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_POSTPLAN_SOL": "1.30",
        "V287_SELECTED_SEED_PRIOR_SPEED_AUTHORITY_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_SPEED_MAX_REARM_LAG_MS": "350",
        "V287_SELECTED_SEED_PRIOR_SPEED_MAX_QUOTE_TOKENS": "900000",
        "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_ZERODRIFT_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_SOL": "0.70",
        "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_BUYS": "1",
        "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_POSTPLAN_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_MIN_POSTPLAN_SOL": "0.70",
        "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_MIN_POSTPLAN_BUYS": "1",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_ZERODRIFT_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_CURRENT_SOL": "1.95",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_CURRENT_SOL": "2.10",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_EXACT_BUYS": "1",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_SOL": "2.00",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_SOL": "2.50",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_FIRST_DELAY_MS": "80",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_LAST_DELAY_MS": "90",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_LAG_MS": "650",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_QUOTE_TOKENS": "500000",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_QUOTE_TOKENS": "620000",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_DRIFT_PCT": "0.05",
        "V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_TOP_SHARE": "0.999",
        "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_POSTPLAN_BRIDGE_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MIN_POSTPLAN_SOL": "1.50",
        "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_POSTPLAN_SOL": "3.50",
        "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MIN_POSTPLAN_BUYS": "1",
        "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_PRE_ENTRY_SOL": "6.50",
        "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_LAG_MS": "650",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_SEND_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_SOL": "1.30",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_BUYS": "1",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_QUOTE_TOKENS": "540000",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_QUOTE_TOKENS": "760000",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_REARM_LAG_MS": "650",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_PRE_ENTRY_SOL": "11.00",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_DRIFT_PCT": "-0.050",
        "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_TOP_SHARE": "0.999",
        "V287_SELECTED_SEED_PRIOR_ZERO_WATCH_MAX_QUOTE_TOKENS": "620000",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_QUOTE_TOKENS": "925000",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_POSTPLAN_SOL": "1.20",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_POSTPLAN_SOL": "2.00",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_PRE_ENTRY_SOL": "3.00",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_PRE_ENTRY_SOL": "3.70",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_PRE_ENTRY_BUYS": "2",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_PRE_ENTRY_BUYS": "2",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_CURRENT_SOL": "2.30",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_REARM_LAG_MS": "650",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOWTHROUGH_OVERRIDE_ENABLED": "1",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_SOL": "2.00",
        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_BUYS": "2",
    }
    os.environ.update(values)


def base_cand(v, **overrides):
    now = v._now_ms()
    cand = {
        "selected_reason": "selected_seed_prior_carry_rearm",
        "top_lane": "seed_prior_carry_continuation",
        "current_buy_sol": 2.5,
        "top_share": 1.0,
        "pre_entry_buys": 2,
        "pre_entry_buy_lamports": int(2.08 * v.LAMPORTS_PER_SOL),
        "post_plan_rearm_passed": 1,
        "post_plan_followthrough_buys": 1,
        "post_plan_followthrough_lamports": int(0.86 * v.LAMPORTS_PER_SOL),
        "first_rearm_pass_delay_ms": 32,
        "last_rearm_pass_delay_ms": 33,
        "last_rearm_lag_ms": 291,
        "last_rearm_pass_ts_ms": now - 576,
        "prev_sells": 0,
    }
    cand.update(overrides)
    return cand


def run_case(v, name, cand, quote_tokens, drift_pct, expected_ok):
    ok, reason = v._v287_seed_prior_final_send_authority(
        cand,
        "selected_seed_prior_carry_rearm",
        quote_tokens,
        drift_pct,
        True,
    )
    evals = cand.get("v287_seed_prior_strong_drift_eval", {})
    preauth = cand.get("v287_seed_prior_zero_drift_preauth_eval", {})
    watch = cand.get("v287_seed_prior_watch_followthrough_eval", {})
    verdict = "PASS" if ok == expected_ok else "FAIL"
    print(
        f"{verdict} {name}: ok={ok} reason={reason} "
        f"strong_pass={evals.get('pass')} drift={drift_pct:+.3f} "
        f"quote={quote_tokens:.3f} "
        f"preauth={preauth.get('flag', '-')}:{preauth.get('pass', '-')} "
        f"watch={watch.get('reason', '-')}"
    )
    return ok == expected_ok


def run_high_cap_watch_case(v, name, cand, quote_tokens, expected_ok):
    ok, reason = v._v287_seed_prior_high_cap_watch_ok(
        cand,
        "selected_seed_prior_carry_rearm",
        quote_tokens,
        760000.0,
    )
    verdict = "PASS" if ok == expected_ok else "FAIL"
    print(
        f"{verdict} {name}: high_cap_watch_ok={ok} reason={reason} "
        f"quote={quote_tokens:.3f} pre_entry={v._v287_cand_pre_entry_sol(cand):.3f} "
        f"postplan={v._v287_cand_post_plan_sol(cand):.3f}"
    )
    return ok == expected_ok


def run_cap_override_case(v, name, cand, quote_tokens, expected_ok):
    ok = v._v287_seed_prior_clean_cap_override_ok(
        cand,
        "selected_seed_prior_carry_rearm",
        quote_tokens,
        760000.0,
    )
    verdict = "PASS" if ok == expected_ok else "FAIL"
    print(
        f"{verdict} {name}: cap_override_ok={ok} "
        f"high_cap_override={cand.get('seed_prior_high_cap_followthrough_override_ok')} "
        f"quote={quote_tokens:.3f}"
    )
    return ok == expected_ok


def run_single_strong_bridge_case(v, name, cand, expected_ok):
    ok, reason = v._v287_seed_prior_single_strong_postplan_bridge_ok(
        cand,
        "selected_seed_prior_single_strong_rearm",
    )
    verdict = "PASS" if ok == expected_ok else "FAIL"
    print(
        f"{verdict} {name}: single_strong_bridge_ok={ok} reason={reason} "
        f"pre_entry={v._v287_cand_pre_entry_sol(cand):.3f} "
        f"postplan={v._v287_cand_post_plan_sol(cand):.3f} "
        f"postplan_buys={int(cand.get('post_plan_followthrough_buys') or 0)}"
    )
    return ok == expected_ok


class FakeCurve:
    def __init__(self, creator):
        self.creator = creator


class FakeBroker:
    def __init__(self, v, mint, creator, override):
        self._pump_creator_vault_override = {str(mint): str(override)}
        self._pump_fee_recipient_override = {}
        self._account_cache = {}
        self._creator = str(creator)

    def bonding_curve(self, _mint_pk):
        return FakeCurve(self._creator)

    def last_pair_info(self, _mint):
        return {}


def _fake_pump_accounts(v, mint, creator_vault):
    accounts = [str(v.Pubkey.new_unique()) for _ in range(18)]
    accounts[2] = str(v.as_pubkey(mint))
    accounts[3] = str(
        v.pda(v.PUMP_PROGRAM_ID, b"bonding-curve", bytes(v.as_pubkey(mint)))
    )
    accounts[9] = str(v.as_pubkey(creator_vault))
    accounts[15] = str(v.PUMP_FEE_PROGRAM_ID)
    return accounts


def run_creator_vault_account_case(v, name, use_expected_account, expected_ok):
    mint = str(v.Pubkey.new_unique())
    creator = str(v.Pubkey.new_unique())
    expected_vault = str(
        v.pda(v.PUMP_PROGRAM_ID, b"creator-vault", bytes(v.as_pubkey(creator)))
    )
    override_vault = str(v.Pubkey.new_unique())
    broker = FakeBroker(v, mint, creator, override_vault)
    account9 = expected_vault if use_expected_account else override_vault
    accounts = _fake_pump_accounts(v, mint, account9)
    old_decoder = v._pump_buy_exact_sol_in_ix_accounts
    v._pump_buy_exact_sol_in_ix_accounts = lambda _txn_b64: list(accounts)
    try:
        ok = v._validate_pump_buy_account_indexes(
            broker,
            mint,
            "synthetic",
            "creator_vault_replay",
            creator,
        )
    finally:
        v._pump_buy_exact_sol_in_ix_accounts = old_decoder
    verdict = "PASS" if ok == expected_ok else "FAIL"
    print(
        f"{verdict} {name}: account_index_ok={ok} "
        f"account9={'expected' if use_expected_account else 'feed_override'} "
        f"expected={expected_ok}"
    )
    return ok == expected_ok


def run_live_creator_vault_final_case(v, name, use_expected_account, expected_ok):
    mint = str(v.Pubkey.new_unique())
    creator = str(v.Pubkey.new_unique())
    expected_vault = str(
        v.pda(v.PUMP_PROGRAM_ID, b"creator-vault", bytes(v.as_pubkey(creator)))
    )
    override_vault = str(v.Pubkey.new_unique())
    broker = FakeBroker(v, mint, creator, override_vault)
    account9 = expected_vault if use_expected_account else override_vault
    accounts = _fake_pump_accounts(v, mint, account9)
    cand = {
        "latest_static_account_fp": {"creator_vault": override_vault},
        "latest_static_account_rec": {"kind": "buy", "sig": "synthetic_sig"},
    }
    old_decoder = v._pump_buy_exact_sol_in_ix_accounts
    old_live = v._live_creator_vault_from_rpc
    old_fp = v._validate_pump_buy_candidate_fingerprint
    v._pump_buy_exact_sol_in_ix_accounts = lambda _txn_b64: list(accounts)
    v._live_creator_vault_from_rpc = (
        lambda _broker, _mint, _rpc_url: (creator, expected_vault, 0, 1, "")
    )
    v._validate_pump_buy_candidate_fingerprint = lambda **_kwargs: True
    try:
        ok = v._validate_pump_buy_live_creator_vault(
            broker=broker,
            mint=mint,
            txn_b64="synthetic",
            rpc_url="http://synthetic",
            source="creator_vault_replay_final",
            cand=cand,
        )
    finally:
        v._pump_buy_exact_sol_in_ix_accounts = old_decoder
        v._live_creator_vault_from_rpc = old_live
        v._validate_pump_buy_candidate_fingerprint = old_fp
    verdict = "PASS" if ok == expected_ok else "FAIL"
    print(
        f"{verdict} {name}: live_final_ok={ok} "
        f"account9={'expected' if use_expected_account else 'feed_override'} "
        f"expected={expected_ok}"
    )
    return ok == expected_ok


def main() -> int:
    set_env()
    v = load_runner()
    all_ok = True

    all_ok &= run_case(
        v,
        "7ZjC_strong_overchoke",
        base_cand(v),
        801528.153433,
        10.559,
        True,
    )
    all_ok &= run_case(
        v,
        "3Jax_strong_positive_drift_kept",
        base_cand(
            v,
            current_buy_sol=2.0,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(1.131289 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=0,
            post_plan_followthrough_buys=0,
            post_plan_followthrough_lamports=0,
            first_rearm_pass_delay_ms=144,
            last_rearm_pass_delay_ms=144,
            last_rearm_lag_ms=556,
        ),
        751515.534380,
        9.040,
        True,
    )
    all_ok &= run_case(
        v,
        "14gm_strong_positive_drift_kept",
        base_cand(
            v,
            current_buy_sol=2.0,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(2.100000 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(0.990000 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=60,
            last_rearm_pass_delay_ms=202,
            last_rearm_lag_ms=644,
        ),
        595142.128783,
        11.361,
        True,
    )
    all_ok &= run_case(
        v,
        "53mi_weak_strong_drift_loss_blocks",
        base_cand(
            v,
            current_buy_sol=2.5,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(2.600000 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.200000 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=26,
            last_rearm_pass_delay_ms=36,
            last_rearm_lag_ms=555,
        ),
        775813.406900,
        6.137,
        False,
    )
    all_ok &= run_case(
        v,
        "3kpW_speed_postplan_preauth",
        base_cand(
            v,
            first_rearm_pass_delay_ms=22,
            last_rearm_pass_delay_ms=23,
            last_rearm_lag_ms=291,
            seed_prior_speed_postplan_zero_drift_send_ok=1,
        ),
        801069.941482,
        0.0,
        True,
    )
    all_ok &= run_case(
        v,
        "8oHg_speed_positive_postplan",
        base_cand(
            v,
            current_buy_sol=2.5,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(2.08 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(0.86 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=20,
            last_rearm_pass_delay_ms=21,
            last_rearm_lag_ms=280,
        ),
        698249.035820,
        0.051,
        True,
    )
    all_ok &= run_case(
        v,
        "8oHg_speed_positive_prior_sell_blocks",
        base_cand(
            v,
            current_buy_sol=2.5,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(2.08 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(0.86 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=20,
            last_rearm_pass_delay_ms=21,
            last_rearm_lag_ms=280,
            prev_sells=1,
        ),
        698249.035820,
        0.051,
        False,
    )
    all_ok &= run_case(
        v,
        "5oDo_consumed_postplan_zero_drift",
        base_cand(
            v,
            current_buy_sol=2.75,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(2.53 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.43 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=34,
            last_rearm_pass_delay_ms=35,
            last_rearm_lag_ms=553,
        ),
        733076.387431,
        0.0,
        True,
    )
    all_ok &= run_case(
        v,
        "6Z2L_consumed_postplan_sol_low_blocks",
        base_cand(
            v,
            current_buy_sol=2.0,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(2.58 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.20 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=21,
            last_rearm_pass_delay_ms=22,
            last_rearm_lag_ms=560,
        ),
        559256.226927,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "Gm4H_consumed_postplan_quote_high_blocks",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(1.65 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(0.825 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=42,
            last_rearm_pass_delay_ms=44,
            last_rearm_lag_ms=562,
        ),
        802158.837102,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "7d88_one_strong_postplan_zero_drift_blocks",
        base_cand(
            v,
            current_buy_sol=2.0,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(3.00 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.50 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=33,
            last_rearm_pass_delay_ms=35,
            last_rearm_lag_ms=563,
        ),
        782134.333277,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "7Bfo_fast_single_rearm_zero_drift",
        base_cand(
            v,
            current_buy_sol=2.0,
            pre_entry_buys=1,
            pre_entry_buy_lamports=int(2.2 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=0,
            post_plan_followthrough_buys=0,
            post_plan_followthrough_lamports=0,
            first_rearm_pass_delay_ms=23,
            last_rearm_pass_delay_ms=23,
            last_rearm_lag_ms=564,
        ),
        548068.459800,
        0.0,
        True,
    )
    all_ok &= run_case(
        v,
        "F5wg_fast_single_rearm_blocks_small_late_high_quote",
        base_cand(
            v,
            current_buy_sol=2.0,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(1.08125 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=0,
            post_plan_followthrough_buys=0,
            post_plan_followthrough_lamports=0,
            first_rearm_pass_delay_ms=87,
            last_rearm_pass_delay_ms=87,
            last_rearm_lag_ms=558,
        ),
        752062.423756,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "6bSm_fast_single_rearm_blocks_weak_rearm",
        base_cand(
            v,
            current_buy_sol=2.35,
            pre_entry_buys=1,
            pre_entry_buy_lamports=int(0.759375 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=0,
            post_plan_followthrough_buys=0,
            post_plan_followthrough_lamports=0,
            first_rearm_pass_delay_ms=335,
            last_rearm_pass_delay_ms=335,
            last_rearm_lag_ms=335,
        ),
        600000.0,
        0.0,
        False,
    )
    all_ok &= run_single_strong_bridge_case(
        v,
        "Ek5T_first_single_strong_still_waits",
        base_cand(
            v,
            current_buy_sol=2.5,
            pre_entry_buys=1,
            pre_entry_buy_lamports=int(3.0 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=0,
            post_plan_followthrough_buys=0,
            post_plan_followthrough_lamports=0,
            first_rearm_pass_delay_ms=49,
            last_rearm_pass_delay_ms=49,
            last_rearm_lag_ms=49,
        ),
        False,
    )
    all_ok &= run_single_strong_bridge_case(
        v,
        "Ek5T_postplan_single_strong_bridges",
        base_cand(
            v,
            current_buy_sol=2.5,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(5.0 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(2.0 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=49,
            last_rearm_pass_delay_ms=628,
            last_rearm_lag_ms=278,
        ),
        True,
    )
    all_ok &= run_single_strong_bridge_case(
        v,
        "single_strong_bridge_prior_sell_blocks",
        base_cand(
            v,
            current_buy_sol=2.5,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(5.0 * v.LAMPORTS_PER_SOL),
            post_plan_rearm_passed=1,
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(2.0 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=49,
            last_rearm_pass_delay_ms=628,
            last_rearm_lag_ms=278,
            prev_sells=1,
        ),
        False,
    )
    all_ok &= run_case(
        v,
        "rH3p_stale_zero_drift_no_flag",
        base_cand(
            v,
            first_rearm_pass_delay_ms=19,
            last_rearm_pass_delay_ms=20,
            last_rearm_lag_ms=558,
        ),
        801528.153456,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "E76c_watch_high_quote_still_blocks",
        base_cand(
            v,
            current_buy_sol=2.64,
            pre_entry_buys=5,
            pre_entry_buy_lamports=int(3.057 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=0,
            post_plan_followthrough_lamports=0,
            first_rearm_pass_delay_ms=26,
            last_rearm_pass_delay_ms=1458,
            last_rearm_lag_ms=282,
            seed_prior_watch_followthrough_send_ok=1,
            seed_prior_watch_followthrough_lamports=int(1.85 * v.LAMPORTS_PER_SOL),
            seed_prior_watch_followthrough_buys=2,
            seed_prior_watch_followthrough_ts_ms=v._now_ms() - 271,
        ),
        757040.688706,
        0.0,
        False,
    )
    all_ok &= run_high_cap_watch_case(
        v,
        "FLbM_high_cap_watch_keep",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(3.289 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.617 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=24,
            last_rearm_pass_delay_ms=25,
            last_rearm_lag_ms=572,
        ),
        904388.900374,
        True,
    )
    all_ok &= run_high_cap_watch_case(
        v,
        "high_cap_watch_prev_sell_blocks",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(3.289 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.617 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=24,
            last_rearm_pass_delay_ms=25,
            last_rearm_lag_ms=572,
            prev_sells=1,
        ),
        904388.900374,
        False,
    )
    all_ok &= run_high_cap_watch_case(
        v,
        "high_cap_watch_postplan_low_blocks",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(3.289 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(0.86 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=24,
            last_rearm_pass_delay_ms=25,
            last_rearm_lag_ms=572,
        ),
        904388.900374,
        False,
    )
    all_ok &= run_high_cap_watch_case(
        v,
        "high_cap_watch_quote_too_high_blocks",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(3.289 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.617 * v.LAMPORTS_PER_SOL),
            first_rearm_pass_delay_ms=24,
            last_rearm_pass_delay_ms=25,
            last_rearm_lag_ms=572,
        ),
        950000.0,
        False,
    )
    all_ok &= run_cap_override_case(
        v,
        "high_cap_no_followthrough_still_blocks",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=2,
            pre_entry_buy_lamports=int(3.289 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.617 * v.LAMPORTS_PER_SOL),
            seed_prior_high_cap_watch=1,
        ),
        904388.900374,
        False,
    )
    all_ok &= run_cap_override_case(
        v,
        "high_cap_followthrough_override",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=4,
            pre_entry_buy_lamports=int(5.614 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_buys=1,
            post_plan_followthrough_lamports=int(1.617 * v.LAMPORTS_PER_SOL),
            seed_prior_high_cap_watch=1,
            seed_prior_watch_followthrough_send_ok=1,
            seed_prior_watch_followthrough_lamports=int(2.325 * v.LAMPORTS_PER_SOL),
            seed_prior_watch_followthrough_buys=2,
            seed_prior_watch_followthrough_ts_ms=v._now_ms() - 120,
        ),
        904388.900374,
        True,
    )
    all_ok &= run_case(
        v,
        "speed_preauth_with_prev_sell_blocks",
        base_cand(
            v,
            seed_prior_speed_postplan_zero_drift_send_ok=1,
            prev_sells=1,
        ),
        801069.941482,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "HygP_generic_watch_loss",
        base_cand(
            v,
            current_buy_sol=2.2,
            pre_entry_buys=5,
            pre_entry_buy_lamports=int(5.07 * v.LAMPORTS_PER_SOL),
            post_plan_followthrough_lamports=int(0.792 * v.LAMPORTS_PER_SOL),
            seed_prior_positive_refresh_watch=1,
            seed_prior_watch_followthrough_send_ok=1,
            seed_prior_watch_followthrough_lamports=int(2.32 * v.LAMPORTS_PER_SOL),
            seed_prior_watch_followthrough_buys=2,
            seed_prior_watch_followthrough_ts_ms=v._now_ms() - 276,
        ),
        743334.300695,
        2.086,
        False,
    )
    all_ok &= run_case(
        v,
        "zero_drift_watch_shape",
        base_cand(v),
        651556.763585,
        0.0,
        False,
    )
    all_ok &= run_case(
        v,
        "negative_drift_shape",
        base_cand(v),
        520722.537448,
        -14.133,
        False,
    )
    all_ok &= run_creator_vault_account_case(
        v,
        "creator_vault_feed_override_blocks_when_curve_known",
        False,
        False,
    )
    all_ok &= run_creator_vault_account_case(
        v,
        "creator_vault_curve_account_passes_when_curve_known",
        True,
        True,
    )
    all_ok &= run_live_creator_vault_final_case(
        v,
        "live_creator_vault_final_blocks_feed_override",
        False,
        False,
    )
    all_ok &= run_live_creator_vault_final_case(
        v,
        "live_creator_vault_final_passes_curve_vault",
        True,
        True,
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
