#!/usr/bin/env python3
"""V272 no-spend batched PumpSwap atomic loop builder.

Same frozen lane, different packaging: several same-mint PumpSwap buy/sell
loops in one signed transaction with shared ATA setup/close. The transaction is
only useful if exact simulation proves the combined no-rent trade delta clears
the requested floor.

No send path exists in this script.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from pgg2_v109_no_send_live_bundle_validation import _ensure_tip_account, _load_env, _make_broker
from pgg2_direct_pump import (
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
from pgg2_v224_pumpswap_multipool_builder import (
    DISC_PUMP_AMM_BUY_EXACT_BASE_OUT,
    DISC_PUMP_AMM_CLOSE_USER_VOLUME,
    _compile_v224_tx,
    max_exact_base_for_quote_cap_v224,
    pumpswap_current_remaining_metas,
    quote_pumpswap_sell_v224,
)
from v245_fast_single_tx_oracle import _sim_bundle_rpcs, _standard_sim_rpcs, simulate_one


BASE_TX_FEE_LAMPORTS = 5_000


def log(msg: str) -> None:
    import time

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_rows(path: Path, limit: int, max_per_pair: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    rows.sort(key=lambda r: int(r.get("edge_lamports", 0)), reverse=True)
    picked: list[dict[str, Any]] = []
    counts: dict[tuple[str, str, str], int] = {}
    for row in rows:
        key = (str(row.get("mint") or ""), str(row.get("buy_pool") or ""), str(row.get("sell_pool") or ""))
        if not all(key):
            continue
        if int(row.get("edge_lamports", 0)) <= 0:
            continue
        if counts.get(key, 0) >= max(1, int(max_per_pair)):
            continue
        picked.append(row)
        counts[key] = counts.get(key, 0) + 1
        if len(picked) >= int(limit):
            break
    return picked


def select_same_mint_batch(rows: list[dict[str, Any]], max_legs: int) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by.setdefault(str(row.get("mint") or ""), []).append(row)
    best: list[dict[str, Any]] = []
    best_sum = -1
    for mint_rows in by.values():
        unique_routes: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in mint_rows:
            key = (str(row.get("buy_pool") or ""), str(row.get("sell_pool") or ""))
            if key in seen:
                continue
            seen.add(key)
            unique_routes.append(row)
            if len(unique_routes) >= int(max_legs):
                break
        total = sum(int(r.get("edge_lamports", 0)) for r in unique_routes)
        if total > best_sum:
            best = unique_routes
            best_sum = total
    return best


def fee_accounts(broker: Any, global_cfg: Any, pool: Any) -> tuple[Pubkey, Pubkey, Pubkey, Pubkey]:
    fee_recipient = broker.pumpswap_fee_recipient(global_cfg, pool)
    fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, TOKEN_PROGRAM_ID)
    creator_vault_authority = Pubkey.find_program_address(
        [b"creator_vault", bytes(pool.coin_creator)], PUMP_AMM_PROGRAM_ID
    )[0]
    creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, TOKEN_PROGRAM_ID)
    return fee_recipient, fee_recipient_ata, creator_vault_authority, creator_vault_ata


def build_batch_tx(
    *,
    broker: Any,
    legs: list[dict[str, Any]],
    leg_min_profit_lamports: int,
    tip_lamports: int,
) -> dict[str, Any]:
    if not legs:
        raise RuntimeError("no_legs")
    mint = str(legs[0]["mint"])
    if any(str(row.get("mint")) != mint for row in legs):
        raise RuntimeError("batch_mint_mismatch")
    mint_pk = as_pubkey(mint)
    user = as_pubkey(broker.public_key)
    global_cfg = broker.pumpswap_global()
    base_token_program = broker.mint_owner(mint_pk)
    quote_token_program = TOKEN_PROGRAM_ID
    user_base_ata = get_associated_token_address(user, mint_pk, base_token_program)
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)
    track_volume = os.environ.get("PGG2_DIRECT_TRACK_VOLUME", "0") not in {"0", "false", "False"}
    exact_base_remaining = os.environ.get("V224_EXACT_BASE_REMAINING", "1").strip().lower() not in {"0", "false", "no"}
    user_volume = Pubkey.find_program_address([b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM_ID)[0]

    leg_meta: list[dict[str, Any]] = []
    total_quote_in = 0
    projected_edge = 0
    loop_ixs: list[Instruction] = []
    for idx, row in enumerate(legs, 1):
        buy_pool = broker.parse_pool(as_pubkey(str(row["buy_pool"])), broker.account_data(broker.account_info(str(row["buy_pool"]))))
        sell_pool = broker.parse_pool(as_pubkey(str(row["sell_pool"])), broker.account_data(broker.account_info(str(row["sell_pool"]))))
        if str(buy_pool.base_mint) != mint or str(sell_pool.base_mint) != mint:
            raise RuntimeError(f"pool_mint_mismatch_leg_{idx}")
        if str(buy_pool.quote_mint) != str(WSOL_MINT) or str(sell_pool.quote_mint) != str(WSOL_MINT):
            raise RuntimeError(f"pool_quote_not_wsol_leg_{idx}")
        expected_tokens, required_quote_in = max_exact_base_for_quote_cap_v224(
            broker, int(row["size_lamports"]), buy_pool, mint_pk
        )
        if expected_tokens <= 0 or required_quote_in <= 0:
            raise RuntimeError(f"exact_base_buy_zero_leg_{idx}")
        quote_in_lamports = int(required_quote_in) + int(os.environ.get("V224_EXACT_BASE_QUOTE_CUSHION_LAMPORTS", "10") or 10)
        expected_sell_out, _sell_fee = quote_pumpswap_sell_v224(broker, int(expected_tokens), sell_pool, mint_pk)
        min_quote_out = int(quote_in_lamports) + int(leg_min_profit_lamports)
        if expected_sell_out < min_quote_out:
            raise RuntimeError(
                f"leg_not_executable_{idx} expected_sell_out={expected_sell_out} min_quote_out={min_quote_out}"
            )
        buy_fee_recipient, buy_fee_recipient_ata, buy_creator_vault_authority, buy_creator_vault_ata = fee_accounts(broker, global_cfg, buy_pool)
        sell_fee_recipient, sell_fee_recipient_ata, sell_creator_vault_authority, sell_creator_vault_ata = fee_accounts(broker, global_cfg, sell_pool)
        buy_data = (
            DISC_PUMP_AMM_BUY_EXACT_BASE_OUT
            + u64(int(expected_tokens))
            + u64(int(quote_in_lamports))
            + (b"\x01" if track_volume else b"\x00")
        )
        sell_data = DISC_PUMP_AMM_SELL + u64(int(expected_tokens)) + u64(int(min_quote_out))
        loop_ixs.append(
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
                    *(pumpswap_current_remaining_metas(broker, mint_pk, buy_pool, quote_token_program) if exact_base_remaining else []),
                ],
            )
        )
        loop_ixs.append(
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
                    *(pumpswap_current_remaining_metas(broker, mint_pk, sell_pool, quote_token_program) if exact_base_remaining else []),
                ],
            )
        )
        total_quote_in += int(quote_in_lamports)
        projected_edge += int(expected_sell_out) - int(quote_in_lamports)
        leg_meta.append(
            {
                "idx": idx,
                "buy_pool": str(row["buy_pool"]),
                "sell_pool": str(row["sell_pool"]),
                "size_lamports": int(row["size_lamports"]),
                "quote_in_lamports": int(quote_in_lamports),
                "expected_tokens_raw": int(expected_tokens),
                "expected_sell_out_lamports": int(expected_sell_out),
                "projected_edge_lamports": int(expected_sell_out) - int(quote_in_lamports),
            }
        )

    compute_budget_mode = os.environ.get("V224_COMPUTE_BUDGET_MODE", "none").strip().lower()
    compute_ixs = broker.compute_budget_ixs()
    if compute_budget_mode == "none":
        compute_ixs = []
    elif compute_budget_mode == "limit":
        compute_ixs = compute_ixs[:1]
    ixs = [
        *compute_ixs,
        create_idempotent_associated_token_account(user, user, mint_pk, base_token_program),
        create_idempotent_associated_token_account(user, user, WSOL_MINT, quote_token_program),
        transfer(TransferParams(from_pubkey=user, to_pubkey=user_quote_ata, lamports=int(total_quote_in))),
        sync_native(quote_token_program, user_quote_ata),
        *loop_ixs,
        close_token_account(quote_token_program, user_quote_ata, user, user),
        close_token_account(base_token_program, user_base_ata, user, user),
        Instruction(
            PUMP_AMM_PROGRAM_ID,
            DISC_PUMP_AMM_CLOSE_USER_VOLUME,
            [
                AccountMeta(user, True, True),
                AccountMeta(user_volume, False, True),
                AccountMeta(broker.pump_amm_event_authority, False, False),
                AccountMeta(PUMP_AMM_PROGRAM_ID, False, False),
            ],
        ),
    ]
    tip_account = _ensure_tip_account()
    if int(tip_lamports) > 0:
        ixs.append(transfer(TransferParams(from_pubkey=user, to_pubkey=as_pubkey(tip_account), lamports=int(tip_lamports))))
        projected_edge -= int(tip_lamports)
    tx_b64 = _compile_v224_tx(broker, ixs)
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    return {
        "mint": mint,
        "legs": leg_meta,
        "leg_count": len(legs),
        "total_quote_in_lamports": int(total_quote_in),
        "projected_edge_lamports": int(projected_edge),
        "tip_lamports": int(tip_lamports),
        "tx_b64": tx_b64,
        "tx_raw_len": len(base64.b64decode(tx_b64)),
        "tx_signature": str(tx.signatures[0]) if tx.signatures else "",
    }


def main() -> int:
    _load_env()
    os.environ.setdefault("V224_BUY_MODE", "exact_base_out")
    os.environ.setdefault("V224_EXACT_BASE_REMAINING", "1")
    os.environ.setdefault("V224_CLOSE_USER_VOLUME", "1")
    os.environ.setdefault("V224_COMPUTE_BUDGET_MODE", "none")
    os.environ.setdefault("V224_ADDRESS_LOOKUP_TABLE_JSON", "/root/piggy/data/v244_static_lut.json")
    os.environ.setdefault("PGG2_DIRECT_TRACK_VOLUME", "0")
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-jsonl", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-per-pair", type=int, default=1)
    ap.add_argument("--max-legs", type=int, default=6)
    ap.add_argument("--leg-min-profit-lamports", type=int, default=0)
    ap.add_argument("--tip-lamports", type=int, default=0)
    ap.add_argument("--min-trade-delta-lamports", type=int, default=1)
    ap.add_argument("--out-json", default="")
    args = ap.parse_args()
    rows = load_rows(Path(args.candidates_jsonl), int(args.limit), int(args.max_per_pair))
    broker = _make_broker()
    broker.refresh_blockhash_cache()
    sim_rpcs = _sim_bundle_rpcs()
    standard_rpcs = _standard_sim_rpcs()
    best: dict[str, Any] | None = None
    best_err = ""
    for legs_count in range(min(int(args.max_legs), len(rows)), 0, -1):
        legs = select_same_mint_batch(rows, legs_count)
        try:
            meta = build_batch_tx(
                broker=broker,
                legs=legs,
                leg_min_profit_lamports=int(args.leg_min_profit_lamports),
                tip_lamports=int(args.tip_lamports),
            )
            tx_b64 = str(meta["tx_b64"])
            raw_len = int(meta["tx_raw_len"])
            if raw_len > 1232:
                log(f"PGG2-V272-BATCH-BLOCK legs={legs_count} reason=tx_too_large raw_len={raw_len}")
                best_err = f"tx_too_large:{raw_len}"
                continue
            sim = simulate_one(tx_b64, sim_rpcs, standard_rpcs)
            delta = int(sim.get("wallet_delta_lamports") or 0)
            trade_delta_no_rent = int(meta["projected_edge_lamports"]) - BASE_TX_FEE_LAMPORTS
            log(
                f"PGG2-V272-BATCH-SIM legs={legs_count} raw_len={raw_len} "
                f"projected={meta['projected_edge_lamports']} delta={delta} "
                f"trade_delta_no_rent={trade_delta_no_rent} ok={int(bool(sim.get('ok')))} "
                f"errs={sim.get('tx_errs')}"
            )
            rec = {k: v for k, v in meta.items() if k != "tx_b64"}
            rec.update({"wallet_delta_lamports": delta, "trade_delta_no_rent_lamports": trade_delta_no_rent, "sim": sim})
            if sim.get("ok") and trade_delta_no_rent >= int(args.min_trade_delta_lamports):
                best = rec
                break
        except Exception as exc:
            best_err = f"{type(exc).__name__}:{str(exc)[:220]}"
            log(f"PGG2-V272-BATCH-BLOCK legs={legs_count} err={best_err}")
    if best:
        log(
            f"PGG2-V272-BATCH-PASS legs={best['leg_count']} "
            f"trade_delta_no_rent={best['trade_delta_no_rent_lamports']} "
            f"projected={best['projected_edge_lamports']}"
        )
        if args.out_json:
            Path(args.out_json).write_text(json.dumps(best, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(best, sort_keys=True), flush=True)
        return 0
    log(f"PGG2-V272-NO-BATCH-PASS last={best_err}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
