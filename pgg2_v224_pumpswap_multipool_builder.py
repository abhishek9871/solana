#!/usr/bin/env python3
"""V224 no-send explicit PumpSwap multi-pool atomic builder.

Builds one signed transaction:
  compute budget
  create base ATA
  create WSOL ATA
  fund/sync WSOL
  PumpSwap buy on explicit pool A
  PumpSwap sell on explicit pool B
  close WSOL ATA
  close base ATA
  Jito tip transfer

No send. This is a construction/guard validation tool.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
from pathlib import Path
from typing import Any

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from pgg2_v109_no_send_live_bundle_validation import _ensure_tip_account, _load_env, _make_broker
from pgg2_direct_pump import (  # type: ignore
    DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN,
    DISC_PUMP_AMM_SELL,
    PUMP_AMM_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
    as_pubkey,
    close_token_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    sync_native,
    u64,
)
from pgg2_v225_pumpswap_multipool_bundle_builder import (  # type: ignore
    DISC_PUMP_AMM_BUY_EXACT_BASE_OUT,
    pumpswap_current_remaining_metas,
)


LAMPORTS_PER_SOL = 1_000_000_000
DISC_PUMP_AMM_CLOSE_USER_VOLUME = bytes([249, 69, 164, 218, 150, 103, 84, 138])


def short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if s and len(s) > 10 else (s or "?")


def log(line: str) -> None:
    import time

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def ceil_div(a: int, b: int) -> int:
    return (int(a) + int(b) - 1) // int(b)


def _spl_mint_supply(raw: bytes) -> int:
    return int.from_bytes(raw[36:44], "little") if len(raw) >= 44 else 0


def _fees_from_config_data(data: bytes) -> tuple[tuple[int, int, int], list[tuple[int, tuple[int, int, int]]]]:
    """Parse current Pump fee-config account enough for PumpSwap quote math.

    Anchor layout:
      8 discriminator, u8 bump, pubkey admin, Fees flat_fees,
      Vec<FeeTier> fee_tiers, Vec<FeeTier> stable_fee_tiers.
    FeeTier is u128 market_cap_lamports_threshold + Fees.
    """
    if len(data) < 69:
        raise RuntimeError(f"fee_config_too_short len={len(data)}")
    off = 8
    off += 1  # bump
    off += 32  # admin

    def take_u64() -> int:
        nonlocal off
        v = int.from_bytes(data[off:off + 8], "little")
        off += 8
        return v

    def take_u128() -> int:
        nonlocal off
        v = int.from_bytes(data[off:off + 16], "little")
        off += 16
        return v

    flat = (take_u64(), take_u64(), take_u64())
    n = int.from_bytes(data[off:off + 4], "little")
    off += 4
    tiers: list[tuple[int, tuple[int, int, int]]] = []
    for _ in range(max(0, min(n, 128))):
        if off + 40 > len(data):
            break
        threshold = take_u128()
        fees = (take_u64(), take_u64(), take_u64())
        tiers.append((threshold, fees))
    return flat, tiers


def pumpswap_effective_fee_bps(
    broker: Any,
    pool: Any,
    base_mint: Pubkey,
    base_reserve: int,
    quote_reserve: int,
) -> tuple[int, int, int]:
    """Match the current PumpSwap SDK fee-tier selector for quote math."""
    try:
        fee_data = broker.account_data(broker.account_info(broker.pump_amm_fee_config, ttl_sec=5.0))
        flat, tiers = _fees_from_config_data(fee_data)
    except Exception:
        # Fail conservatively to the old global values if fee-config cannot be
        # read. Exact simulation still remains the final authority.
        global_cfg = broker.pumpswap_global()
        return (
            int(global_cfg.lp_fee_bps),
            int(global_cfg.protocol_fee_bps),
            int(global_cfg.coin_creator_fee_bps),
        )

    pump_pool_authority = Pubkey.find_program_address(
        [b"pool-authority", bytes(base_mint)], PUMP_PROGRAM_ID
    )[0]
    if str(pool.creator) != str(pump_pool_authority):
        return flat
    supply = _spl_mint_supply(broker.account_data(broker.account_info(base_mint, ttl_sec=5.0)))
    market_cap = int(quote_reserve) * int(supply) // max(int(base_reserve), 1)
    if not tiers:
        return flat
    selected = tiers[0][1]
    if market_cap < int(tiers[0][0]):
        return selected
    for threshold, fees in reversed(tiers):
        if market_cap >= int(threshold):
            selected = fees
            break
    return selected


def _total_fee_bps_for_pool(
    broker: Any,
    pool: Any,
    base_mint: Pubkey,
    base_reserve: int,
    quote_reserve: int,
) -> tuple[int, int, int, int]:
    lp_bps, protocol_bps, creator_bps = pumpswap_effective_fee_bps(
        broker, pool, base_mint, int(base_reserve), int(quote_reserve)
    )
    creator_applied = 0 if str(pool.coin_creator) == str(Pubkey.default()) else int(creator_bps)
    total = int(lp_bps) + int(protocol_bps) + int(creator_applied)
    return int(total), int(lp_bps), int(protocol_bps), int(creator_applied)


def exact_base_buy_quote_lamports_v224(
    broker: Any,
    base_amount_out: int,
    pool: Any,
    base_mint: Pubkey,
) -> int:
    base_reserve = int(broker.token_account_balance_raw(pool.pool_base_token_account))
    quote_reserve = int(broker.token_account_balance_raw(pool.pool_quote_token_account))
    if int(base_amount_out) <= 0 or int(base_amount_out) >= base_reserve:
        return 0
    internal_quote = ceil_div(int(base_amount_out) * quote_reserve, base_reserve - int(base_amount_out))
    _total_bps, lp_bps, protocol_bps, creator_bps = _total_fee_bps_for_pool(
        broker, pool, base_mint, base_reserve, quote_reserve
    )
    return (
        int(internal_quote)
        + ceil_div(internal_quote * lp_bps, 10_000)
        + ceil_div(internal_quote * protocol_bps, 10_000)
        + ceil_div(internal_quote * creator_bps, 10_000)
    )


def max_exact_base_for_quote_cap_v224(
    broker: Any,
    quote_cap: int,
    pool: Any,
    base_mint: Pubkey,
) -> tuple[int, int]:
    approx_tokens, _fee, _net = quote_pumpswap_buy_exact_quote_in_v224(
        broker, int(quote_cap), pool, base_mint
    )
    lo = 0
    hi = max(0, int(approx_tokens))
    best_tokens = 0
    best_cost = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        cost = exact_base_buy_quote_lamports_v224(broker, mid, pool, base_mint)
        if cost and cost <= int(quote_cap):
            best_tokens = mid
            best_cost = cost
            lo = mid + 1
        else:
            hi = mid - 1
    return best_tokens, best_cost


def quote_pumpswap_buy_exact_quote_in_v224(
    broker: Any,
    spend_lamports: int,
    pool: Any,
    base_mint: Pubkey,
) -> tuple[int, int, int]:
    """Use the on-chain PumpSwap BuyExactQuoteIn rounding.

    The shared broker quote is one net-quote lamport optimistic for this
    instruction shape, which turns into a 6040 min-base failure. Keep the fix
    local to V224 so protected production quote code stays untouched.
    """
    base_reserve = broker.token_account_balance_raw(pool.pool_base_token_account)
    quote_reserve = broker.token_account_balance_raw(pool.pool_quote_token_account)
    total_fee_bps, lp_bps, protocol_bps, creator_bps = _total_fee_bps_for_pool(
        broker, pool, base_mint, int(base_reserve), int(quote_reserve)
    )
    effective_quote = int(spend_lamports) * 10_000 // max(10_000 + total_fee_bps, 1)
    fees = (
        ceil_div(effective_quote * lp_bps, 10_000)
        + ceil_div(effective_quote * protocol_bps, 10_000)
        + ceil_div(effective_quote * creator_bps, 10_000)
    )
    total_with_fees = int(effective_quote) + int(fees)
    if total_with_fees > int(spend_lamports):
        effective_quote = max(0, int(effective_quote) - (total_with_fees - int(spend_lamports)))
        fees = (
            ceil_div(effective_quote * lp_bps, 10_000)
            + ceil_div(effective_quote * protocol_bps, 10_000)
            + ceil_div(effective_quote * creator_bps, 10_000)
        )
    input_amount = max(0, int(effective_quote) - 1)
    base_out = int(input_amount) * int(base_reserve) // max(int(quote_reserve) + int(input_amount), 1)
    base_buffer = int(os.environ.get("V224_QUOTE_IN_NET_LAMPORT_BUFFER", "0") or 0)
    return max(0, int(base_out) - max(0, base_buffer)), max(0, int(fees)), int(effective_quote)


def quote_pumpswap_sell_v224(
    broker: Any,
    base_amount: int,
    pool: Any,
    base_mint: Pubkey,
) -> tuple[int, int]:
    base_reserve = int(broker.token_account_balance_raw(pool.pool_base_token_account))
    quote_reserve = int(broker.token_account_balance_raw(pool.pool_quote_token_account))
    gross_quote = int(base_amount) * quote_reserve // max(base_reserve + int(base_amount), 1)
    _total_bps, lp_bps, protocol_bps, creator_bps = _total_fee_bps_for_pool(
        broker, pool, base_mint, base_reserve, quote_reserve
    )
    fees = (
        ceil_div(gross_quote * lp_bps, 10_000)
        + ceil_div(gross_quote * protocol_bps, 10_000)
        + ceil_div(gross_quote * creator_bps, 10_000)
    )
    return max(0, int(gross_quote) - int(fees)), max(0, int(fees))


def _compile_v224_tx(broker: Any, ixs: list[Instruction]) -> str:
    """Compile with an optional static address lookup table."""
    lut_path = os.environ.get("V224_ADDRESS_LOOKUP_TABLE_JSON", "").strip()
    lut_key = os.environ.get("V224_ADDRESS_LOOKUP_TABLE", "").strip()
    if lut_path and not lut_key:
        try:
            lut_key = str(json.loads(Path(lut_path).read_text(encoding="utf-8")).get("lookup_table") or "")
        except Exception:
            lut_key = ""
    if not lut_key:
        return broker.compile_tx(ixs)
    info = broker.account_info(lut_key)
    raw = broker.account_data(info)
    if len(raw) < 56 or (len(raw) - 56) % 32 != 0:
        raise RuntimeError(f"v224_lut_bad_account_data_len={len(raw)}")
    lut_addresses = [Pubkey.from_bytes(raw[i : i + 32]) for i in range(56, len(raw), 32)]
    lut = AddressLookupTableAccount(as_pubkey(lut_key), lut_addresses)
    payer = as_pubkey(broker.public_key)
    msg = MessageV0.try_compile(payer, ixs, [lut], broker.latest_blockhash())
    tx = VersionedTransaction(msg, [broker.keypair])
    return base64.b64encode(bytes(tx)).decode("ascii")


def build_explicit_multipool_tx(
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
) -> dict[str, Any]:
    mint_pk = as_pubkey(mint)
    buy_pool = broker.parse_pool(as_pubkey(buy_pool_key), broker.account_data(broker.account_info(buy_pool_key)))
    sell_pool = broker.parse_pool(as_pubkey(sell_pool_key), broker.account_data(broker.account_info(sell_pool_key)))
    if str(buy_pool.base_mint) != mint or str(sell_pool.base_mint) != mint:
        raise RuntimeError("pool_mint_mismatch")
    if str(buy_pool.quote_mint) != str(WSOL_MINT) or str(sell_pool.quote_mint) != str(WSOL_MINT):
        raise RuntimeError("pool_quote_not_wsol")

    global_cfg = broker.pumpswap_global()
    buy_mode = os.environ.get("V224_BUY_MODE", "exact_quote_in").strip().lower()
    if buy_mode == "exact_base_out":
        expected_tokens, required_quote_in = max_exact_base_for_quote_cap_v224(
            broker, int(size_lamports), buy_pool, mint_pk
        )
        if expected_tokens <= 0 or required_quote_in <= 0:
            raise RuntimeError("exact_base_buy_zero")
        exact_base_quote_cushion = int(os.environ.get("V224_EXACT_BASE_QUOTE_CUSHION_LAMPORTS", "10") or 10)
        quote_in_lamports = int(required_quote_in) + max(0, exact_base_quote_cushion)
        buy_fee = max(0, int(size_lamports) - int(required_quote_in))
    else:
        expected_tokens, buy_fee, _net_quote = quote_pumpswap_buy_exact_quote_in_v224(
            broker, int(size_lamports), buy_pool, mint_pk
        )
        quote_in_lamports = int(size_lamports)
    expected_sell_out, sell_fee = quote_pumpswap_sell_v224(broker, int(expected_tokens), sell_pool, mint_pk)
    min_quote_out = (
        int(quote_in_lamports)
        + int(fee_buffer_lamports)
        + int(projection_buffer_lamports)
        + int(min_profit_lamports)
        + int(tip_lamports)
    )
    if expected_sell_out < min_quote_out:
        raise RuntimeError(
            f"not_executable expected_sell_out={expected_sell_out} min_quote_out={min_quote_out}"
        )

    user = as_pubkey(broker.public_key)
    base_token_program = broker.mint_owner(mint_pk)
    quote_token_program = TOKEN_PROGRAM_ID
    user_base_ata = get_associated_token_address(user, mint_pk, base_token_program)
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)

    def fee_accounts(pool: Any) -> tuple[Pubkey, Pubkey, Pubkey]:
        fee_recipient = broker.pumpswap_fee_recipient(global_cfg, pool)
        fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, quote_token_program)
        creator_vault_authority = Pubkey.find_program_address(
            [b"creator_vault", bytes(pool.coin_creator)], PUMP_AMM_PROGRAM_ID
        )[0]
        creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, quote_token_program)
        return fee_recipient, fee_recipient_ata, creator_vault_ata

    buy_fee_recipient, buy_fee_recipient_ata, buy_creator_vault_ata = fee_accounts(buy_pool)
    sell_fee_recipient, sell_fee_recipient_ata, sell_creator_vault_ata = fee_accounts(sell_pool)
    buy_creator_vault_authority = Pubkey.find_program_address(
        [b"creator_vault", bytes(buy_pool.coin_creator)], PUMP_AMM_PROGRAM_ID
    )[0]
    sell_creator_vault_authority = Pubkey.find_program_address(
        [b"creator_vault", bytes(sell_pool.coin_creator)], PUMP_AMM_PROGRAM_ID
    )[0]
    track_volume = os.environ.get("PGG2_DIRECT_TRACK_VOLUME", "1") != "0"
    # PumpSwap's current buy account layout still expects the volume metas.
    # The boolean in the instruction data controls whether tracking is applied.
    # For V245 atomic scalps we pass the accounts but set the flag false so the
    # program does not initialize a rent-locking user volume PDA.
    user_volume = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM_ID
    )[0]
    if buy_mode == "exact_base_out":
        buy_data = (
            DISC_PUMP_AMM_BUY_EXACT_BASE_OUT
            + u64(int(expected_tokens))
            + u64(int(quote_in_lamports))
            + (b"\x01" if track_volume else b"\x00")
        )
    else:
        buy_data = (
            DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN
            + u64(int(quote_in_lamports))
            + u64(int(expected_tokens))
            + (b"\x01" if track_volume else b"\x00")
        )
    sell_data = DISC_PUMP_AMM_SELL + u64(int(expected_tokens)) + u64(int(min_quote_out))
    compute_budget_mode = os.environ.get("V224_COMPUTE_BUDGET_MODE", "full").strip().lower()
    exact_base_remaining = os.environ.get("V224_EXACT_BASE_REMAINING", "1").strip().lower() not in {"0", "false", "no"}
    quote_in_remaining = os.environ.get("V224_QUOTE_IN_REMAINING", "1").strip().lower() not in {"0", "false", "no"}
    close_user_volume = os.environ.get("V224_CLOSE_USER_VOLUME", "1").strip().lower() not in {"0", "false", "no"}
    compute_ixs = broker.compute_budget_ixs()
    if compute_budget_mode == "none":
        compute_ixs = []
    elif compute_budget_mode == "limit":
        compute_ixs = compute_ixs[:1]

    ixs = [
        *compute_ixs,
        create_idempotent_associated_token_account(user, user, mint_pk, base_token_program),
        create_idempotent_associated_token_account(user, user, WSOL_MINT, quote_token_program),
        transfer(TransferParams(from_pubkey=user, to_pubkey=user_quote_ata, lamports=int(quote_in_lamports))),
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
                *(
                    pumpswap_current_remaining_metas(broker, mint_pk, buy_pool, quote_token_program)
                    if (
                        (buy_mode == "exact_base_out" and exact_base_remaining)
                        or (buy_mode != "exact_base_out" and quote_in_remaining)
                    )
                    else []
                ),
            ],
        ),
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
                *(
                    pumpswap_current_remaining_metas(broker, mint_pk, sell_pool, quote_token_program)
                    if (
                        (buy_mode == "exact_base_out" and exact_base_remaining)
                        or (buy_mode != "exact_base_out" and quote_in_remaining)
                    )
                    else []
                ),
            ],
        ),
        close_token_account(quote_token_program, user_quote_ata, user, user),
        close_token_account(base_token_program, user_base_ata, user, user),
    ]

    if close_user_volume:
        ixs.append(
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

    tip_account = _ensure_tip_account()
    if int(tip_lamports) > 0:
        ixs.append(
            transfer(
                TransferParams(
                    from_pubkey=user,
                    to_pubkey=as_pubkey(tip_account),
                    lamports=int(tip_lamports),
                )
            )
        )

    tx_b64 = _compile_v224_tx(broker, ixs)
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    buy_ix_found = False
    sell_ix_found = False
    encoded_buy_min = -1
    encoded_sell_min = -1
    for ix in tx.message.instructions:
        program = tx.message.account_keys[ix.program_id_index]
        data = bytes(ix.data)
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN):
            buy_ix_found = True
            encoded_buy_min = struct.unpack("<Q", data[16:24])[0]
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_BUY_EXACT_BASE_OUT):
            buy_ix_found = True
            encoded_buy_min = struct.unpack("<Q", data[8:16])[0]
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_SELL):
            sell_ix_found = True
            encoded_sell_min = struct.unpack("<Q", data[16:24])[0]
    if not buy_ix_found or not sell_ix_found:
        raise RuntimeError("missing_buy_or_sell_ix")
    if encoded_buy_min != int(expected_tokens):
        raise RuntimeError(f"buy_min_mismatch encoded={encoded_buy_min} expected={expected_tokens}")
    if encoded_sell_min != int(min_quote_out):
        raise RuntimeError(f"sell_min_mismatch encoded={encoded_sell_min} expected={min_quote_out}")

    projected_edge = (
        int(expected_sell_out)
        - int(quote_in_lamports)
        - int(fee_buffer_lamports)
        - int(projection_buffer_lamports)
        - int(tip_lamports)
    )
    return {
        "mint": mint,
        "buy_pool": buy_pool_key,
        "sell_pool": sell_pool_key,
        "buy_mode": buy_mode,
        "size_lamports": int(size_lamports),
        "quote_in_lamports": int(quote_in_lamports),
        "required_quote_in_lamports": int(required_quote_in) if buy_mode == "exact_base_out" else int(size_lamports),
        "expected_tokens_raw": int(expected_tokens),
        "expected_sell_out_lamports": int(expected_sell_out),
        "min_quote_out_lamports": int(min_quote_out),
        "projected_edge_lamports": int(projected_edge),
        "buy_fee_lamports": int(buy_fee),
        "sell_fee_lamports": int(sell_fee),
        "tip_lamports": int(tip_lamports),
        "track_volume": bool(track_volume),
        "close_user_volume": bool(close_user_volume),
        "compute_budget_mode": compute_budget_mode,
        "exact_base_remaining": bool(exact_base_remaining),
        "quote_in_remaining": bool(quote_in_remaining),
        "tx_b64": tx_b64,
        "tx_b64_len": len(tx_b64),
        "tx_raw_len": len(base64.b64decode(tx_b64)),
        "address_lookup_table": os.environ.get("V224_ADDRESS_LOOKUP_TABLE", "").strip(),
        "tx_signature": str(tx.signatures[0]) if tx.signatures else "",
        "buy_ix_found": buy_ix_found,
        "sell_ix_found": sell_ix_found,
        "encoded_buy_min_tokens_raw": int(encoded_buy_min),
        "encoded_sell_min_lamports": int(encoded_sell_min),
    }


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-json", required=True)
    ap.add_argument("--fee-buffer-lamports", type=int, default=int(os.environ.get("V224_FEE_BUFFER_LAMPORTS", "15000")))
    ap.add_argument("--projection-buffer-lamports", type=int, default=int(os.environ.get("V224_PROJECTION_BUFFER_LAMPORTS", "5000")))
    ap.add_argument("--min-profit-lamports", type=int, default=int(os.environ.get("V224_MIN_PROFIT_LAMPORTS", "30000")))
    ap.add_argument("--tip-lamports", type=int, default=int(os.environ.get("V224_TIP_LAMPORTS", "1000")))
    ap.add_argument("--out-tx-b64", default="")
    args = ap.parse_args()
    cand = json.loads(args.candidate_json)
    broker = _make_broker()
    broker.refresh_blockhash_cache()
    result = build_explicit_multipool_tx(
        broker=broker,
        mint=str(cand["mint"]),
        buy_pool_key=str(cand["buy_pool"]),
        sell_pool_key=str(cand["sell_pool"]),
        size_lamports=int(cand["size_lamports"]),
        min_profit_lamports=int(args.min_profit_lamports),
        fee_buffer_lamports=int(args.fee_buffer_lamports),
        projection_buffer_lamports=int(args.projection_buffer_lamports),
        tip_lamports=int(args.tip_lamports),
    )
    log(
        f"PGG2-V224-MULTIPOOL-BUILD-PASS mint={short(result['mint'])} "
        f"buy_pool={short(result['buy_pool'])} sell_pool={short(result['sell_pool'])} "
        f"size={result['size_lamports']/LAMPORTS_PER_SOL:.4f} "
        f"expected_sell_out={result['expected_sell_out_lamports']} "
        f"min_quote_out={result['min_quote_out_lamports']} "
        f"projected_edge_lamports={result['projected_edge_lamports']:+} "
        f"sig={short(result['tx_signature'])}"
    )
    tx_b64 = str(result.pop("tx_b64"))
    if args.out_tx_b64:
        Path(args.out_tx_b64).write_text(tx_b64, encoding="ascii")
        result["tx_b64_path"] = args.out_tx_b64
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
