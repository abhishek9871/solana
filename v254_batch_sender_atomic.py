#!/usr/bin/env python3
"""V254 exact-positive batched PumpSwap single-transaction runner.

This is a frequency patch for V252/V253: several PumpSwap pool-dislocation
routes can be just below the fixed one-transaction cost by themselves. V254
tries to pack two independent buy->sell atomic routes into one transaction so
they share one base fee. Live sends are opt-in only.

It still fails closed:
- no send unless exact simulateTransaction payer wallet delta is positive;
- every leg has buy and sell guards;
- transaction must fit under 1232 bytes;
- final wallet/token state remains the authority.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import struct
import time
import urllib.error
import urllib.request
import argparse
from typing import Any

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

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
from pgg2_v109_no_send_live_bundle_validation import _ensure_tip_account, _load_env, _make_broker
from pgg2_v224_pumpswap_multipool_builder import (  # type: ignore
    DISC_PUMP_AMM_CLOSE_USER_VOLUME,
    _compile_v224_tx,
    exact_base_buy_quote_lamports_v224,
    max_exact_base_for_quote_cap_v224,
    quote_pumpswap_buy_exact_quote_in_v224,
    quote_pumpswap_sell_v224,
)
from pgg2_v225_pumpswap_multipool_bundle_builder import (  # type: ignore
    DISC_PUMP_AMM_BUY_EXACT_BASE_OUT,
    pumpswap_current_remaining_metas,
)


READ_RPC = "https://public.rpc.solanavibestation.com"
SENDER_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"
WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
HELIUS_TIP_ACCOUNT = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
MAX_TX_RAW_LEN = 1232
LIVE_CONFIRMATION = "I_ACCEPT_V254_BATCH_ATOMIC_RISK"


def rpc(url: str, method: str, params: list[Any], timeout: float = 20.0) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) & 0xFFFF,
        "method": method,
        "params": params,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if out.get("error"):
        raise RuntimeError(out["error"])
    return out.get("result")


def _setup_env(*, tip_account: str = HELIUS_TIP_ACCOUNT) -> None:
    _load_env()
    os.environ["HELIUS_RPC_URL"] = READ_RPC
    os.environ["SOLANA_RPC_URL"] = READ_RPC
    os.environ["V224_BUY_MODE"] = "exact_base_out"
    os.environ["V224_COMPUTE_BUDGET_MODE"] = "none"
    os.environ["V224_EXACT_BASE_REMAINING"] = "1"
    os.environ["V224_ADDRESS_LOOKUP_TABLE_JSON"] = "/root/piggy/data/v244_static_lut.json"
    os.environ["PGG2_DIRECT_TRACK_VOLUME"] = "0"
    os.environ["V224_CLOSE_USER_VOLUME"] = "1"
    os.environ["PGG2_JITO_TIP_ACCOUNT"] = tip_account


def _fee_accounts(broker: Any, global_cfg: Any, pool: Any) -> tuple[Pubkey, Pubkey, Pubkey, Pubkey]:
    fee_recipient = broker.pumpswap_fee_recipient(global_cfg, pool)
    fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, TOKEN_PROGRAM_ID)
    creator_vault_authority = Pubkey.find_program_address(
        [b"creator_vault", bytes(pool.coin_creator)], PUMP_AMM_PROGRAM_ID
    )[0]
    creator_vault_ata = get_associated_token_address(
        creator_vault_authority, WSOL_MINT, TOKEN_PROGRAM_ID
    )
    return fee_recipient, fee_recipient_ata, creator_vault_ata, creator_vault_authority


def build_batch_tx(
    *,
    broker: Any,
    cands: list[dict[str, Any]],
    min_profit_lamports_per_leg: int,
    tip_lamports: int,
) -> dict[str, Any]:
    user = as_pubkey(broker.public_key)
    quote_token_program = TOKEN_PROGRAM_ID
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)
    global_cfg = broker.pumpswap_global()
    exact_base_remaining = os.environ.get("V224_EXACT_BASE_REMAINING", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    track_volume = os.environ.get("PGG2_DIRECT_TRACK_VOLUME", "1") != "0"
    user_volume = Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(user)], PUMP_AMM_PROGRAM_ID
    )[0]

    ixs: list[Instruction] = [
        create_idempotent_associated_token_account(user, user, WSOL_MINT, quote_token_program),
    ]
    legs: list[dict[str, Any]] = []
    seen_mints: set[str] = set()
    total_quote_in = 0
    total_expected_sell_out = 0

    for cand in cands:
        mint = str(cand["mint"])
        if mint in seen_mints:
            raise RuntimeError("duplicate_mint_in_batch")
        seen_mints.add(mint)
        mint_pk = as_pubkey(mint)
        buy_pool = broker.parse_pool(
            as_pubkey(str(cand["buy_pool"])),
            broker.account_data(broker.account_info(str(cand["buy_pool"]))),
        )
        sell_pool = broker.parse_pool(
            as_pubkey(str(cand["sell_pool"])),
            broker.account_data(broker.account_info(str(cand["sell_pool"]))),
        )
        if str(buy_pool.base_mint) != mint or str(sell_pool.base_mint) != mint:
            raise RuntimeError("pool_mint_mismatch")
        if str(buy_pool.quote_mint) != str(WSOL_MINT) or str(sell_pool.quote_mint) != str(WSOL_MINT):
            raise RuntimeError("pool_quote_not_wsol")

        size_lamports = int(cand["size_lamports"])
        expected_tokens, required_quote_in = max_exact_base_for_quote_cap_v224(
            broker, size_lamports, buy_pool, mint_pk
        )
        if expected_tokens <= 0 or required_quote_in <= 0:
            raise RuntimeError("exact_base_buy_zero")
        quote_cushion = int(os.environ.get("V224_EXACT_BASE_QUOTE_CUSHION_LAMPORTS", "10") or 10)
        quote_in_lamports = int(required_quote_in) + max(0, quote_cushion)
        expected_sell_out, _sell_fee = quote_pumpswap_sell_v224(
            broker, int(expected_tokens), sell_pool, mint_pk
        )
        min_quote_out = int(quote_in_lamports) + int(min_profit_lamports_per_leg)
        if int(expected_sell_out) < int(min_quote_out):
            raise RuntimeError(
                f"leg_not_executable mint={mint[:4]} expected_sell_out={expected_sell_out} "
                f"min_quote_out={min_quote_out}"
            )

        base_token_program = broker.mint_owner(mint_pk)
        user_base_ata = get_associated_token_address(user, mint_pk, base_token_program)
        (
            buy_fee_recipient,
            buy_fee_recipient_ata,
            buy_creator_vault_ata,
            buy_creator_vault_authority,
        ) = _fee_accounts(broker, global_cfg, buy_pool)
        (
            sell_fee_recipient,
            sell_fee_recipient_ata,
            sell_creator_vault_ata,
            sell_creator_vault_authority,
        ) = _fee_accounts(broker, global_cfg, sell_pool)

        buy_data = (
            DISC_PUMP_AMM_BUY_EXACT_BASE_OUT
            + u64(int(expected_tokens))
            + u64(int(quote_in_lamports))
            + (b"\x01" if track_volume else b"\x00")
        )
        sell_data = DISC_PUMP_AMM_SELL + u64(int(expected_tokens)) + u64(int(min_quote_out))

        ixs.extend(
            [
                create_idempotent_associated_token_account(user, user, mint_pk, base_token_program),
                transfer(
                    TransferParams(
                        from_pubkey=user,
                        to_pubkey=user_quote_ata,
                        lamports=int(quote_in_lamports),
                    )
                ),
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
                            pumpswap_current_remaining_metas(
                                broker, mint_pk, buy_pool, quote_token_program
                            )
                            if exact_base_remaining
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
                            pumpswap_current_remaining_metas(
                                broker, mint_pk, sell_pool, quote_token_program
                            )
                            if exact_base_remaining
                            else []
                        ),
                    ],
                ),
                close_token_account(base_token_program, user_base_ata, user, user),
            ]
        )
        total_quote_in += int(quote_in_lamports)
        total_expected_sell_out += int(expected_sell_out)
        legs.append(
            {
                "mint": mint,
                "buy_pool": str(cand["buy_pool"]),
                "sell_pool": str(cand["sell_pool"]),
                "size_lamports": int(size_lamports),
                "quote_in_lamports": int(quote_in_lamports),
                "expected_tokens_raw": int(expected_tokens),
                "expected_sell_out_lamports": int(expected_sell_out),
                "min_quote_out_lamports": int(min_quote_out),
                "raw_edge_lamports": int(cand.get("edge_lamports", 0)),
            }
        )

    ixs.append(close_token_account(quote_token_program, user_quote_ata, user, user))
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
    if int(tip_lamports) > 0:
        ixs.append(
            transfer(
                TransferParams(
                    from_pubkey=user,
                    to_pubkey=as_pubkey(_ensure_tip_account()),
                    lamports=int(tip_lamports),
                )
            )
        )

    tx_b64 = _compile_v224_tx(broker, ixs)
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    buy_ixs = 0
    sell_ixs = 0
    for ix in tx.message.instructions:
        program = tx.message.account_keys[ix.program_id_index]
        data = bytes(ix.data)
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_BUY_EXACT_BASE_OUT):
            buy_ixs += 1
        if program == PUMP_AMM_PROGRAM_ID and data.startswith(DISC_PUMP_AMM_SELL):
            sell_ixs += 1
            encoded_sell_min = struct.unpack("<Q", data[16:24])[0]
            if encoded_sell_min <= 0:
                raise RuntimeError("sell_min_zero")
    if buy_ixs != len(legs) or sell_ixs != len(legs):
        raise RuntimeError(f"buy_sell_count_mismatch buys={buy_ixs} sells={sell_ixs} legs={len(legs)}")
    projected_edge = int(total_expected_sell_out) - int(total_quote_in) - int(tip_lamports)
    return {
        "kind": "v254_batch",
        "legs": legs,
        "leg_count": len(legs),
        "projected_edge_lamports": int(projected_edge),
        "total_quote_in_lamports": int(total_quote_in),
        "total_expected_sell_out_lamports": int(total_expected_sell_out),
        "tip_lamports": int(tip_lamports),
        "tx_b64": tx_b64,
        "tx_raw_len": len(base64.b64decode(tx_b64)),
        "tx_b64_len": len(tx_b64),
    }


def simulate_wallet_delta(tx_b64: str, pre: int) -> dict[str, Any]:
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    payer = str(tx.message.account_keys[0])
    sim_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateTransaction",
        "params": [
            tx_b64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
                "accounts": {"encoding": "base64", "addresses": [payer]},
            },
        ],
    }
    last = ""
    for url in (READ_RPC, "https://api.mainnet-beta.solana.com"):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(sim_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            out = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            if out.get("error"):
                last = str(out["error"])[:160]
                continue
            val = (out.get("result") or {}).get("value") or {}
            acct = ((val.get("accounts") or [{}])[0] or {})
            return {
                "err": val.get("err"),
                "delta": int(acct.get("lamports") or 0) - int(pre),
                "rpc": url,
                "units": val.get("unitsConsumed"),
            }
        except Exception as exc:
            last = f"{type(exc).__name__}:{str(exc)[:120]}"
    return {"err": f"sim_unavailable:{last}", "delta": -10**18, "rpc": "-", "units": None}


def candidate_batches(rows: list[dict[str, Any]], max_rows: int) -> list[list[dict[str, Any]]]:
    filtered: list[dict[str, Any]] = []
    seen_route: set[tuple[str, str, str, int]] = set()
    for row in rows[:max_rows]:
        key = (
            str(row.get("mint")),
            str(row.get("buy_pool")),
            str(row.get("sell_pool")),
            int(row.get("size_lamports", 0)),
        )
        if key in seen_route:
            continue
        seen_route.add(key)
        filtered.append(row)
    out: list[list[dict[str, Any]]] = []
    for a, b in itertools.combinations(filtered, 2):
        if str(a.get("mint")) == str(b.get("mint")):
            continue
        out.append([a, b])
    out.sort(key=lambda pair: sum(int(x.get("edge_lamports", 0)) for x in pair), reverse=True)
    return out


def _rpcfast_url() -> str:
    _load_env()
    explicit = os.environ.get("RPCFAST_RPC_URL", "").strip()
    if explicit:
        return explicit
    key = os.environ.get("RPCFAST_API_KEY", "").strip()
    if not key:
        raise RuntimeError("missing_rpcfast_api_key")
    return "https://solana-rpc.rpcfast.com/?api_key=" + key


def _send_rpcfast(tx_b64: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) & 0xFFFF,
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 0,
                "preflightCommitment": "processed",
            },
        ],
    }
    req = urllib.request.Request(
        _rpcfast_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    out = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
    print(f"PGG2-V254-BATCH-RPCFAST-SEND-RAW {out}", flush=True)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return str(out.get("result") or "")


def _send_sender(tx_b64: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) & 0xFFFF,
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {"encoding": "base64", "skipPreflight": True, "maxRetries": 0},
        ],
    }
    req = urllib.request.Request(
        SENDER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        print(f"PGG2-V254-BATCH-SEND-HTTP-ERR code={exc.code} body={body}", flush=True)
        raise
    print(f"PGG2-V254-BATCH-SENDER-SEND-RAW {out}", flush=True)
    if out.get("error"):
        raise RuntimeError(out["error"])
    return str(out.get("result") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates-jsonl", default="/root/piggy/data/v223_v246_broad.jsonl")
    ap.add_argument("--scan-limit", type=int, default=int(os.environ.get("V254_BATCH_SCAN_LIMIT", "80") or "80"))
    ap.add_argument("--combo-limit", type=int, default=int(os.environ.get("V254_BATCH_COMBO_LIMIT", "60") or "60"))
    ap.add_argument("--min-projected-edge-lamports", type=int, default=int(os.environ.get("V254_MIN_PROJECTED_EDGE_LAMPORTS", "1") or "1"))
    ap.add_argument("--min-positive-delta-lamports", type=int, default=int(os.environ.get("V254_MIN_POSITIVE_DELTA_LAMPORTS", "1") or "1"))
    ap.add_argument("--tip-lamports", type=int, default=int(os.environ.get("V254_TIP_LAMPORTS", "0") or "0"))
    ap.add_argument("--transport", choices=["rpcfast_rpc", "helius_sender_swqos"], default=os.environ.get("V254_TRANSPORT", "rpcfast_rpc"))
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--confirm-live", default="")
    args = ap.parse_args()

    _setup_env()
    pre = int(rpc(READ_RPC, "getBalance", [WALLET, {"commitment": "processed"}])["value"])
    print(f"PGG2-V254-BATCH-PREFLIGHT-WALLET pre_lamports={pre}", flush=True)

    broker = _make_broker()
    broker.rpc_url = READ_RPC
    broker.refresh_blockhash_cache()

    rows = [
        json.loads(line)
        for line in open(args.candidates_jsonl, encoding="utf-8")
        if line.strip()
    ]
    rows.sort(key=lambda row: int(row.get("edge_lamports", 0)), reverse=True)
    scan_limit = max(2, int(args.scan_limit))
    combo_limit = max(1, int(args.combo_limit))
    min_projected = int(args.min_projected_edge_lamports)
    print(
        f"PGG2-V254-BATCH-CONFIG scan_limit={scan_limit} combo_limit={combo_limit} "
        f"min_projected={min_projected} tip_lamports={args.tip_lamports} "
        f"transport={args.transport} live={int(args.live)}",
        flush=True,
    )

    best = None
    for idx, batch in enumerate(candidate_batches(rows, scan_limit)[:combo_limit], 1):
        raw_sum = sum(int(x.get("edge_lamports", 0)) for x in batch)
        mints = ",".join(str(x.get("mint", ""))[:4] for x in batch)
        try:
            meta = build_batch_tx(
                broker=broker,
                cands=batch,
                min_profit_lamports_per_leg=1,
                tip_lamports=int(args.tip_lamports),
            )
        except Exception as exc:
            print(
                f"PGG2-V254-BATCH-BUILD-BLOCK idx={idx} mints={mints} raw_sum={raw_sum} "
                f"err={type(exc).__name__}:{str(exc)[:140]}",
                flush=True,
            )
            continue
        if int(meta["tx_raw_len"]) > MAX_TX_RAW_LEN:
            print(
                f"PGG2-V254-BATCH-SIZE-BLOCK idx={idx} mints={mints} raw_sum={raw_sum} "
                f"raw_len={meta['tx_raw_len']} max={MAX_TX_RAW_LEN}",
                flush=True,
            )
            continue
        if int(meta["projected_edge_lamports"]) < min_projected:
            print(
                f"PGG2-V254-BATCH-PROJECTED-BLOCK idx={idx} mints={mints} raw_sum={raw_sum} "
                f"projected={meta['projected_edge_lamports']} min={min_projected}",
                flush=True,
            )
            continue
        sim = simulate_wallet_delta(str(meta["tx_b64"]), pre)
        print(
            f"PGG2-V254-BATCH-EXACT-SIM idx={idx} mints={mints} raw_sum={raw_sum} "
            f"projected={meta['projected_edge_lamports']} raw_len={meta['tx_raw_len']} "
            f"delta={sim['delta']} err={sim['err']} rpc={sim['rpc']}",
            flush=True,
        )
        if sim["err"] is None and int(sim["delta"]) >= int(args.min_positive_delta_lamports):
            best = (meta, sim)
            break
    if not best:
        print("PGG2-V254-BATCH-NO-EXACT-POSITIVE", flush=True)
        return 2

    meta, sim = best
    tx_b64 = str(meta["tx_b64"])
    sig_preview = str(VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).signatures[0])
    print(
        f"PGG2-V254-BATCH-EXACT-POSITIVE legs={meta['leg_count']} "
        f"delta={sim['delta']} projected={meta['projected_edge_lamports']} "
        f"sig={sig_preview} transport={args.transport}",
        flush=True,
    )
    if not args.live:
        print("PGG2-V254-BATCH-DRYRUN-NO-SEND", flush=True)
        return 0
    if args.confirm_live != LIVE_CONFIRMATION:
        raise RuntimeError("missing_live_confirmation")
    if args.transport == "rpcfast_rpc":
        sig = _send_rpcfast(tx_b64)
    else:
        sig = _send_sender(tx_b64)
    print(f"PGG2-V254-BATCH-SEND sig={sig}", flush=True)

    status = None
    for poll in range(1, 21):
        time.sleep(1)
        try:
            st = rpc(
                READ_RPC,
                "getSignatureStatuses",
                [[sig], {"searchTransactionHistory": False}],
                timeout=10,
            )
            status = (st.get("value") or [None])[0]
            print(f"PGG2-V254-BATCH-STATUS poll={poll} status={status}", flush=True)
            if status and (
                status.get("confirmationStatus") in ("processed", "confirmed", "finalized")
                or status.get("err") is not None
            ):
                break
        except Exception as exc:
            print(
                f"PGG2-V254-BATCH-STATUS-ERR poll={poll} "
                f"err={type(exc).__name__}:{str(exc)[:120]}",
                flush=True,
            )
    post = int(rpc(READ_RPC, "getBalance", [WALLET, {"commitment": "processed"}])["value"])
    print(f"PGG2-V254-BATCH-FINAL-WALLET pre={pre} post={post} delta={post - pre}", flush=True)
    if post < pre:
        print("PGG2-V254-BATCH-HARD-FAIL reason=negative_wallet_delta", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
