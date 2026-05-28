#!/usr/bin/env python3
"""V225 two-transaction PumpSwap multi-pool atomic bundle builder.

No send by default. Produces a Jito bundle:
  tx1: create ATAs, wrap SOL, explicit PumpSwap buy
  tx2: explicit PumpSwap sell, close WSOL ATA, close token ATA, tip

Jito bundle atomicity is required: if tx2 cannot execute, tx1 must not commit.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
from pathlib import Path
from typing import Any

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from pgg2_v109_no_send_live_bundle_validation import _ensure_tip_account, _load_env, _make_broker
from pgg2_v108_jito_bundle_sender import send_bundle, warm_bundle_endpoints
from pgg2_direct_pump import (  # type: ignore
    DISC_PUMP_AMM_SELL,
    PUMP_AMM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
    as_pubkey,
    close_token_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
    u64,
)

LAMPORTS_PER_SOL = 1_000_000_000
DISC_PUMP_AMM_BUY_EXACT_BASE_OUT = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_PUMP_AMM_CLOSE_USER_VOLUME = bytes([249, 69, 164, 218, 150, 103, 84, 138])


def short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if s and len(s) > 10 else (s or "?")


def log(line: str) -> None:
    import time

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _raw_len(tx_b64: str) -> int:
    return len(base64.b64decode(tx_b64))


def _verify_ixs(tx_b64: str) -> tuple[bool, bool, int, int]:
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    buy = False
    sell = False
    buy_min = -1
    sell_min = -1
    for ix in tx.message.instructions:
        program = tx.message.account_keys[ix.program_id_index]
        data = bytes(ix.data)
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_BUY_EXACT_BASE_OUT):
            buy = True
            buy_min = struct.unpack("<Q", data[8:16])[0]
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_SELL):
            sell = True
            sell_min = struct.unpack("<Q", data[16:24])[0]
    return buy, sell, buy_min, sell_min


def ceil_div(a: int, b: int) -> int:
    return (int(a) + int(b) - 1) // int(b)


def exact_base_buy_quote_lamports(broker: Any, base_amount_out: int, pool: Any, global_cfg: Any) -> int:
    total_fee_bps = int(global_cfg.lp_fee_bps + global_cfg.protocol_fee_bps + global_cfg.coin_creator_fee_bps)
    base_reserve = int(broker.token_account_balance_raw(pool.pool_base_token_account))
    quote_reserve = int(broker.token_account_balance_raw(pool.pool_quote_token_account))
    if base_amount_out <= 0 or base_amount_out >= base_reserve:
        return 0
    net_quote = ceil_div(int(base_amount_out) * quote_reserve, max(base_reserve - int(base_amount_out), 1))
    return ceil_div(net_quote * (10_000 + total_fee_bps), 10_000)


def max_exact_base_for_quote_cap(broker: Any, quote_cap: int, pool: Any, global_cfg: Any) -> tuple[int, int]:
    approx_tokens, _fee = broker.quote_pumpswap_buy(int(quote_cap), pool, global_cfg)
    lo = 0
    hi = max(0, int(approx_tokens))
    best_tokens = 0
    best_cost = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cost = exact_base_buy_quote_lamports(broker, mid, pool, global_cfg)
        if cost and cost <= int(quote_cap):
            best_tokens = mid
            best_cost = cost
            lo = mid + 1
        else:
            hi = mid - 1
    return best_tokens, best_cost


def pumpswap_current_remaining_metas(
    broker: Any,
    mint_pk: Pubkey,
    pool: Any,
    quote_token_program: Pubkey,
) -> list[AccountMeta]:
    """Current PumpSwap SDK remaining accounts for buy/sell.

    The public IDL omits these as dynamic remaining accounts, but the deployed
    program rejects exact-output buys without an authorized buyback recipient.
    """
    metas: list[AccountMeta] = []
    if str(pool.coin_creator) != str(Pubkey.default()):
        pool_v2 = Pubkey.find_program_address([b"pool-v2", bytes(mint_pk)], PUMP_AMM_PROGRAM_ID)[0]
        metas.append(AccountMeta(pool_v2, False, False))

    global_data = broker.account_data(broker.account_info(broker.pump_amm_global_config, ttl_sec=5.0))
    buyback_start = 643
    buyback_recipients: list[Pubkey] = []
    if len(global_data) >= buyback_start + 8 * 32:
        for i in range(8):
            pk = Pubkey.from_bytes(global_data[buyback_start + i * 32:buyback_start + (i + 1) * 32])
            if str(pk) != str(Pubkey.default()):
                buyback_recipients.append(pk)
    forced = os.environ.get("V225_PUMPSWAP_BUYBACK_FEE_RECIPIENT", "").strip()
    if forced:
        buyback_recipients.insert(0, as_pubkey(forced))
    if not buyback_recipients:
        raise RuntimeError("pumpswap_buyback_recipient_missing")
    buyback_recipient = buyback_recipients[0]
    buyback_ata = get_associated_token_address(buyback_recipient, WSOL_MINT, quote_token_program)
    metas.extend(
        [
            AccountMeta(buyback_recipient, False, False),
            AccountMeta(buyback_ata, False, True),
        ]
    )
    return metas


def build_two_tx_bundle(
    *,
    broker: Any,
    mint: str,
    buy_pool_key: str,
    sell_pool_key: str,
    size_lamports: int,
    min_profit_lamports: int,
    fee_buffer_lamports: int,
    projection_buffer_lamports: int,
    tip_lamports: int,
    max_quote_cushion_lamports: int,
    override_sell_min_lamports: int = -1,
) -> dict[str, Any]:
    mint_pk = as_pubkey(mint)
    buy_pool = broker.parse_pool(as_pubkey(buy_pool_key), broker.account_data(broker.account_info(buy_pool_key)))
    sell_pool = broker.parse_pool(as_pubkey(sell_pool_key), broker.account_data(broker.account_info(sell_pool_key)))
    if str(buy_pool.base_mint) != mint or str(sell_pool.base_mint) != mint:
        raise RuntimeError("pool_mint_mismatch")
    if str(buy_pool.quote_mint) != str(WSOL_MINT) or str(sell_pool.quote_mint) != str(WSOL_MINT):
        raise RuntimeError("pool_quote_not_wsol")

    global_cfg = broker.pumpswap_global()
    expected_tokens, required_quote_in = max_exact_base_for_quote_cap(broker, int(size_lamports), buy_pool, global_cfg)
    if expected_tokens <= 0 or required_quote_in <= 0:
        raise RuntimeError("exact_base_buy_zero")
    quote_cushion = max(0, int(max_quote_cushion_lamports))
    max_quote_in = int(required_quote_in) + int(quote_cushion)
    buy_fee = max(0, int(size_lamports) - int(required_quote_in))
    expected_sell_out, sell_fee = broker.quote_pumpswap_sell(int(expected_tokens), sell_pool, global_cfg)
    min_quote_out = (
        int(max_quote_in)
        + int(fee_buffer_lamports)
        + int(projection_buffer_lamports)
        + int(min_profit_lamports)
        + int(tip_lamports)
    )
    if int(override_sell_min_lamports) >= 0:
        min_quote_out = int(override_sell_min_lamports)
    if expected_sell_out < min_quote_out:
        raise RuntimeError(f"not_executable expected_sell_out={expected_sell_out} min_quote_out={min_quote_out}")

    user = as_pubkey(broker.public_key)
    base_token_program = broker.mint_owner(mint_pk)
    quote_token_program = TOKEN_PROGRAM_ID
    user_base_ata = get_associated_token_address(user, mint_pk, base_token_program)
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)

    def fee_accounts(pool: Any) -> tuple[Pubkey, Pubkey, Pubkey, Pubkey]:
        fee_recipient = broker.pumpswap_fee_recipient(global_cfg, pool)
        fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, quote_token_program)
        creator_vault_authority = Pubkey.find_program_address(
            [b"creator_vault", bytes(pool.coin_creator)], PUMP_AMM_PROGRAM_ID
        )[0]
        creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, quote_token_program)
        return fee_recipient, fee_recipient_ata, creator_vault_ata, creator_vault_authority

    buy_fee_recipient, buy_fee_recipient_ata, buy_creator_vault_ata, buy_creator_vault_authority = fee_accounts(buy_pool)
    sell_fee_recipient, sell_fee_recipient_ata, sell_creator_vault_ata, sell_creator_vault_authority = fee_accounts(sell_pool)
    user_volume = Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM_ID)[0]
    tip_account = _ensure_tip_account()
    buy_remaining_metas = pumpswap_current_remaining_metas(broker, mint_pk, buy_pool, quote_token_program)
    sell_remaining_metas = pumpswap_current_remaining_metas(broker, mint_pk, sell_pool, quote_token_program)
    track_volume = os.environ.get("V225_TRACK_VOLUME", "0").strip().lower() in {"1", "true", "yes", "on"}
    close_user_volume = os.environ.get("V225_CLOSE_USER_VOLUME", "1").strip().lower() in {"1", "true", "yes", "on"}

    buy_data = (
        DISC_PUMP_AMM_BUY_EXACT_BASE_OUT
        + u64(int(expected_tokens))
        + u64(int(max_quote_in))
        + (b"\x01" if track_volume else b"\x00")
    )
    sell_data = DISC_PUMP_AMM_SELL + u64(int(expected_tokens)) + u64(int(min_quote_out))

    tx1_ixs = [
        *broker.compute_budget_ixs(),
        create_idempotent_associated_token_account(user, user, mint_pk, base_token_program),
        create_idempotent_associated_token_account(user, user, WSOL_MINT, quote_token_program),
        transfer(TransferParams(from_pubkey=user, to_pubkey=user_quote_ata, lamports=int(max_quote_in))),
        sync_native(quote_token_program, user_quote_ata),
        Instruction(
            PUMP_AMM_PROGRAM_ID,
            buy_data,
            [
                *broker.pumpswap_common_metas(
                buy_pool,
                user,
                mint_pk,
                user_base_ata,
                user_quote_ata,
                buy_fee_recipient,
                buy_fee_recipient_ata,
                base_token_program,
                quote_token_program,
                buy_creator_vault_ata,
                buy_creator_vault_authority,
                user_volume,
                include_volume=True,
                ),
                *buy_remaining_metas,
            ],
        ),
    ]
    tx2_ixs = [
        *broker.compute_budget_ixs(),
        Instruction(
            PUMP_AMM_PROGRAM_ID,
            sell_data,
            [
                *broker.pumpswap_common_metas(
                sell_pool,
                user,
                mint_pk,
                user_base_ata,
                user_quote_ata,
                sell_fee_recipient,
                sell_fee_recipient_ata,
                base_token_program,
                quote_token_program,
                sell_creator_vault_ata,
                sell_creator_vault_authority,
                None,
                include_volume=False,
                ),
                *sell_remaining_metas,
            ],
        ),
        close_token_account(quote_token_program, user_quote_ata, user, user),
        close_token_account(base_token_program, user_base_ata, user, user),
    ]
    if close_user_volume:
        tx2_ixs.append(
            Instruction(
                PUMP_AMM_PROGRAM_ID,
                DISC_PUMP_AMM_CLOSE_USER_VOLUME,
                [
                    AccountMeta(user, True, True),
                    AccountMeta(user_volume, False, True),
                    AccountMeta(broker.pump_amm_event_authority, False, False),
                    AccountMeta(PUMP_AMM_PROGRAM_ID, False, False),
                ],
            )
        )
    if int(tip_lamports) > 0:
        tx2_ixs.append(
            transfer(
                TransferParams(
                    from_pubkey=user,
                    to_pubkey=as_pubkey(tip_account),
                    lamports=int(tip_lamports),
                )
            )
        )

    tx1 = broker.compile_tx(tx1_ixs)
    tx2 = broker.compile_tx(tx2_ixs)
    tx1_buy, tx1_sell, tx1_buy_min, _ = _verify_ixs(tx1)
    tx2_buy, tx2_sell, _, tx2_sell_min = _verify_ixs(tx2)
    if not tx1_buy or tx1_sell:
        raise RuntimeError("tx1_ix_shape_invalid")
    if tx2_buy or not tx2_sell:
        raise RuntimeError("tx2_ix_shape_invalid")
    if tx1_buy_min != int(expected_tokens):
        raise RuntimeError(f"tx1_buy_min_mismatch encoded={tx1_buy_min} expected={expected_tokens}")
    if tx2_sell_min != int(min_quote_out):
        raise RuntimeError(f"tx2_sell_min_mismatch encoded={tx2_sell_min} expected={min_quote_out}")
    if _raw_len(tx1) > 1232 or _raw_len(tx2) > 1232:
        raise RuntimeError(f"tx_size_too_large tx1={_raw_len(tx1)} tx2={_raw_len(tx2)}")

    projected_edge = (
        int(expected_sell_out)
        - int(max_quote_in)
        - int(fee_buffer_lamports)
        - int(projection_buffer_lamports)
        - int(tip_lamports)
    )
    return {
        "mint": mint,
        "buy_pool": buy_pool_key,
        "sell_pool": sell_pool_key,
        "size_lamports": int(size_lamports),
        "required_quote_in_lamports": int(required_quote_in),
        "quote_cushion_lamports": int(quote_cushion),
        "max_quote_in_lamports": int(max_quote_in),
        "expected_tokens_raw": int(expected_tokens),
        "expected_sell_out_lamports": int(expected_sell_out),
        "min_quote_out_lamports": int(min_quote_out),
        "projected_edge_lamports": int(projected_edge),
        "buy_fee_lamports": int(buy_fee),
        "sell_fee_lamports": int(sell_fee),
        "tip_lamports": int(tip_lamports),
        "override_sell_min_lamports": int(override_sell_min_lamports),
        "track_volume": bool(track_volume),
        "close_user_volume": bool(close_user_volume),
        "buy_remaining_meta_count": int(len(buy_remaining_metas)),
        "sell_remaining_meta_count": int(len(sell_remaining_metas)),
        "txs_b64": [tx1, tx2],
        "tx_raw_lens": [_raw_len(tx1), _raw_len(tx2)],
        "tx_signatures": [
            str(VersionedTransaction.from_bytes(base64.b64decode(tx1)).signatures[0]),
            str(VersionedTransaction.from_bytes(base64.b64decode(tx2)).signatures[0]),
        ],
    }


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-json", required=True)
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V225_FEE_BUFFER_LAMPORTS", "15000")))
    ap.add_argument("--projection-buffer-lamports", type=int, default=int(os.environ.get("V225_PROJECTION_BUFFER_LAMPORTS", "5000")))
    ap.add_argument("--min-profit-lamports", type=int, default=int(os.environ.get("V225_MIN_PROFIT_LAMPORTS", "30000")))
    ap.add_argument("--tip-lamports", type=int, default=int(os.environ.get("V225_TIP_LAMPORTS", "1000")))
    ap.add_argument("--max-quote-cushion-lamports", type=int, default=int(os.environ.get("V225_MAX_QUOTE_CUSHION_LAMPORTS", "10")))
    ap.add_argument("--override-sell-min-lamports", type=int, default=int(os.environ.get("V225_OVERRIDE_SELL_MIN_LAMPORTS", "-1")))
    ap.add_argument("--out-bundle-json", default="")
    ap.add_argument("--dry-run-jito", action="store_true")
    args = ap.parse_args()
    cand = json.loads(args.candidate_json)
    if args.dry_run_jito:
        warm_bundle_endpoints()
    broker = _make_broker()
    broker.refresh_blockhash_cache()
    result = build_two_tx_bundle(
        broker=broker,
        mint=str(cand["mint"]),
        buy_pool_key=str(cand["buy_pool"]),
        sell_pool_key=str(cand["sell_pool"]),
        size_lamports=int(cand["size_lamports"]),
        min_profit_lamports=int(args.min_profit_lamports),
        fee_buffer_lamports=int(args.fee_buffer_lamports),
        projection_buffer_lamports=int(args.projection_buffer_lamports),
        tip_lamports=int(args.tip_lamports),
        max_quote_cushion_lamports=int(args.max_quote_cushion_lamports),
        override_sell_min_lamports=int(args.override_sell_min_lamports),
    )
    txs = list(result.pop("txs_b64"))
    if args.out_bundle_json:
        Path(args.out_bundle_json).write_text(json.dumps({"txs_b64": txs, "meta": result}, sort_keys=True), encoding="utf-8")
        result["bundle_json_path"] = args.out_bundle_json
    if args.dry_run_jito:
        dry = send_bundle(txs, dry_run=True)
        result["dry_run_jito"] = dry
    log(
        f"PGG2-V225-MULTIPOOL-BUNDLE-BUILD-PASS mint={short(result['mint'])} "
        f"buy_pool={short(result['buy_pool'])} sell_pool={short(result['sell_pool'])} "
        f"size={result['size_lamports']/LAMPORTS_PER_SOL:.4f} "
        f"tx_raw_lens={result['tx_raw_lens']} projected_edge_lamports={result['projected_edge_lamports']:+} "
        f"sigs={short(result['tx_signatures'][0])},{short(result['tx_signatures'][1])}"
    )
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
