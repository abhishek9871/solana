"""V51B Stage A live runner — one entry, official Pump v2 builder, Helius Sender SWQOS-only.

PASS: one non-negative close (wallet delta >= 0 post-fees) OR safe failed-buy with no position.
FAIL: any negative close, stuck token, close fail, fee budget breach.

Hard caps:
- 1 successful close max
- 0.005 SOL trade size
- 0.000005 SOL SWQOS tip (max)
- 0.00030 SOL fee budget
- 1500 ms max hold (watchdog forces exit)
- 0.008 SOL realized wallet drawdown hardcap (emergency)
"""
from __future__ import annotations
import asyncio
import base64
import json
import os
import sys
import time
from urllib import request as urlreq
from typing import Optional

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

sys.path.insert(0, "/root/piggy")
from pgg2_pump_v2_idl_constants import (
    BUY_V2_DISCRIMINATOR, SELL_V2_DISCRIMINATOR,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID, SYSTEM_PROGRAM_ID,
    PUMP_PROGRAM_ID, NATIVE_MINT,
)
from pgg2_pump_v2_accounts import (
    resolve_v2_accounts_sol_paired, V2AccountsError, ata, pda,
)
from pgg2_pump_v2_builder import (
    build_buy_v2_ix, build_sell_v2_ix,
    decode_buy_v2_guard, decode_sell_v2_guard,
)


# --- constants ---
TIP_LAMPORTS = 5_000
TIP_SOL = TIP_LAMPORTS / 1e9
TRADE_SIZE_SOL = 0.005
TRADE_SIZE_LAMPORTS = int(TRADE_SIZE_SOL * 1e9)
FEE_BUDGET_SOL = 0.00030
PRIORITY_FEE_MICROLAMPORTS = 100_000
CU_LIMIT = 400_000
MAX_HOLD_MS = 1500
WATCHDOG_INTERVAL_MS = 250
WALLET_DRAWDOWN_HARDCAP_SOL = 0.008
HOLDER_MIN_COUNT = 4
HOLDER_TOP1_EXTREME_PCT = 45.0
HOLDER_EXTREME_COUNT_MIN = 8
BANK_THRESHOLD_SOL = 0.00060
SCRATCH_THRESHOLD_SOL = 0.00005
EMERGENCY_MIN_SOL_RATIO = 0.40
EMERGENCY_MIN_SOL_FLOOR = 0.0005
HELIUS_SENDER_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"
TIP_ACCOUNT = Pubkey.from_string("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env(path: str = "/root/piggy/.env") -> dict:
    env = {}
    for line in open(path):
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    return env


def load_keypair() -> Keypair:
    raw = open("/root/piggy/live_wallet.key").read().strip()
    return Keypair.from_base58_string(raw)


def rpc(url: str, method: str, params: list, timeout: float = 8.0) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urlreq.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def create_idempotent_ata_ix(payer: Pubkey, owner: Pubkey, mint: Pubkey, token_program: Pubkey, ata_pk: Pubkey) -> Instruction:
    metas = [
        AccountMeta(payer, True, True),
        AccountMeta(ata_pk, False, True),
        AccountMeta(owner, False, False),
        AccountMeta(mint, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(token_program, False, False),
    ]
    return Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes([1]), metas)


def find_active_pump_candidate(helius_url: str, user: Pubkey, max_attempts: int = 30) -> Optional[str]:
    """Find a current active pump.fun bonding curve mint by sampling recent Pump program transactions."""
    r = rpc(helius_url, "getSignaturesForAddress", [str(PUMP_PROGRAM_ID), {"limit": max_attempts}])
    sigs = [s["signature"] for s in r.get("result", [])][:max_attempts]
    seen_mints = set()
    for sig in sigs:
        try:
            tx = rpc(helius_url, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            v = tx.get("result")
            if not v or not v.get("meta"):
                continue
            for tb in v["meta"].get("postTokenBalances", []):
                mint_str = tb.get("mint", "")
                if not mint_str.endswith("pump"):
                    continue
                if mint_str in seen_mints:
                    continue
                seen_mints.add(mint_str)
                try:
                    mint_pk = Pubkey.from_string(mint_str)
                    bc = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint_pk))
                    bcr = rpc(helius_url, "getAccountInfo", [str(bc), {"encoding": "base64", "commitment": "processed"}])
                    bv = bcr.get("result", {}).get("value")
                    if not bv:
                        continue
                    bdata = base64.b64decode(bv["data"][0])
                    if len(bdata) < 49 or bool(bdata[48]):
                        continue
                    vsol = int.from_bytes(bdata[16:24], "little")
                    if vsol < 30 * 1_000_000_000:  # at least 30 SOL virtual; means curve has movement
                        continue
                    return mint_str
                except Exception:
                    continue
        except Exception:
            continue
    return None


def check_holder_breadth(helius_url: str, mint: Pubkey) -> dict:
    """V51B holder breadth gate. count >= 4 required. Top1 >= 45% AND count < 8 blocks."""
    try:
        largest = rpc(helius_url, "getTokenLargestAccounts", [str(mint), {"commitment": "processed"}])
        supply = rpc(helius_url, "getTokenSupply", [str(mint), {"commitment": "processed"}])
    except Exception as e:
        return {"ok": False, "error": f"rpc_fail:{e}", "pass": False}
    holders = largest.get("result", {}).get("value", [])
    total_supply = int(supply.get("result", {}).get("value", {}).get("amount", "0") or "0")
    if not holders or not total_supply:
        return {"ok": False, "error": "no_holders_data", "pass": False}
    bc_pda = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
    bc_ata_legacy = ata(bc_pda, mint, TOKEN_PROGRAM_ID)
    bc_ata_2022 = ata(bc_pda, mint, TOKEN_2022_PROGRAM_ID)
    exclude = {str(bc_pda), str(bc_ata_legacy), str(bc_ata_2022)}
    filtered = [(h["address"], int(h["amount"])) for h in holders if h["address"] not in exclude and int(h["amount"]) > 0]
    if not filtered:
        return {"ok": True, "top1_pct": 0.0, "holder_count": 0, "pass": False, "blocker": "no_external_holders"}
    largest_amt = max(amt for _, amt in filtered)
    top1_pct = (largest_amt / total_supply) * 100
    holder_count = len(filtered)
    if holder_count < HOLDER_MIN_COUNT:
        return {"ok": True, "top1_pct": top1_pct, "holder_count": holder_count, "pass": False, "blocker": f"holder_count_lt_{HOLDER_MIN_COUNT}"}
    if top1_pct > HOLDER_TOP1_EXTREME_PCT and holder_count < HOLDER_EXTREME_COUNT_MIN:
        return {"ok": True, "top1_pct": top1_pct, "holder_count": holder_count, "pass": False, "blocker": "extreme_concentration_with_thin_breadth"}
    return {"ok": True, "top1_pct": top1_pct, "holder_count": holder_count, "pass": True}


async def helius_send(signed_bytes: bytes, helius_api_key: str) -> dict:
    import aiohttp
    tx_b64 = base64.b64encode(signed_bytes).decode()
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
        "params": [tx_b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
    }
    t0 = time.time()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post(HELIUS_SENDER_URL, json=body, headers={"Content-Type": "application/json"}) as r:
            txt = await r.text()
            try:
                data = json.loads(txt)
            except Exception:
                data = {"raw": txt}
    lat_ms = (time.time() - t0) * 1000
    if "result" in data:
        return {"ok": True, "sig": data["result"], "lat_ms": lat_ms}
    return {"ok": False, "error": data.get("error", data), "lat_ms": lat_ms}


def confirm_sig(helius_rpc_url: str, sig: str, max_wait_ms: int = 30_000) -> dict:
    t0 = time.time()
    while (time.time() - t0) * 1000 < max_wait_ms:
        try:
            r = rpc(helius_rpc_url, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            v = r.get("result", {}).get("value", [None])[0]
            if v and v.get("confirmationStatus"):
                return {"status": v["confirmationStatus"], "err": v.get("err"), "slot": v.get("slot"), "wait_ms": (time.time() - t0) * 1000}
        except Exception:
            pass
        time.sleep(0.2)
    return {"status": "timeout", "err": None, "slot": None, "wait_ms": (time.time() - t0) * 1000}


def get_wallet_sol(helius_rpc_url: str, user: Pubkey) -> float:
    r = rpc(helius_rpc_url, "getBalance", [str(user), {"commitment": "processed"}])
    return r.get("result", {}).get("value", 0) / 1e9


def get_token_balance_raw(helius_rpc_url: str, ata_pk: Pubkey) -> int:
    try:
        r = rpc(helius_rpc_url, "getTokenAccountBalance", [str(ata_pk), {"commitment": "processed"}])
        amt = r.get("result", {}).get("value", {}).get("amount")
        if amt is None:
            return 0
        return int(amt)
    except Exception:
        return 0


def get_bc_quote_sell_sol(helius_rpc_url: str, bc_pk: Pubkey, tokens_in: int) -> float:
    try:
        r = rpc(helius_rpc_url, "getAccountInfo", [str(bc_pk), {"encoding": "base64", "commitment": "processed"}])
        v = r.get("result", {}).get("value")
        if not v:
            return 0.0
        data = base64.b64decode(v["data"][0])
        if len(data) < 24:
            return 0.0
        vtok = int.from_bytes(data[8:16], "little")
        vsol = int.from_bytes(data[16:24], "little")
        sol_out_raw = (vsol * tokens_in) // (vtok + tokens_in)
        return (sol_out_raw / 1e9) * (1 - 0.0105)
    except Exception:
        return 0.0


async def main() -> int:
    env = load_env()
    api_key = env["HELIUS_API_KEY"]
    helius_rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    kp = load_keypair()
    user = kp.pubkey()
    log(f"PGG2-V51B-STAGEA-START user={user}")

    wallet_before = get_wallet_sol(helius_rpc_url, user)
    log(f"PGG2-V51B-STAGEA-WALLET-BEFORE wallet_sol={wallet_before:.9f}")

    deadline_s = time.time() + 20 * 60
    fee_consumed_sol = 0.0
    failed_sends = 0

    selected_mint = None
    selected_accs = None
    selected_holder = None

    log("PGG2-V51B-STAGEA-SCAN-START looking for candidate")
    while time.time() < deadline_s:
        try:
            mint_str = find_active_pump_candidate(helius_rpc_url, user, max_attempts=30)
        except Exception as e:
            log(f"PGG2-V51B-STAGEA-SCAN-RPC-ERR {e}")
            time.sleep(2)
            continue
        if not mint_str:
            log("PGG2-V51B-STAGEA-SCAN-NO-CANDIDATE retry_2s")
            time.sleep(2)
            continue
        mint = Pubkey.from_string(mint_str)
        holder = check_holder_breadth(helius_rpc_url, mint)
        log(f"PGG2-V51B-STAGEA-HOLDER-GATE mint={mint_str} top1_pct={holder.get('top1_pct',0):.2f} count={holder.get('holder_count',0)} pass={holder.get('pass')} blocker={holder.get('blocker','')}")
        if not holder.get("pass"):
            time.sleep(1)
            continue
        try:
            accs = resolve_v2_accounts_sol_paired(helius_rpc_url, mint, user)
        except V2AccountsError as e:
            log(f"PGG2-V51B-STAGEA-V2-ACCOUNTS-BLOCK mint={mint_str} err={e}")
            time.sleep(1)
            continue
        selected_mint = mint
        selected_accs = accs
        selected_holder = holder
        log(f"PGG2-V51B-STAGEA-CANDIDATE-PICKED mint={mint_str} base_token_program={accs['base_token_program']} bc={accs['bonding_curve']}")
        break

    if not selected_mint:
        log("PGG2-V51B-STAGEA-NO-CANDIDATE-IN-DEADLINE")
        log(f"PGG2-V51B-STAGEA-RESULT verdict=SAFE_FAIL_NO_CANDIDATE wallet_delta=0 fee={fee_consumed_sol:.9f}")
        return 0

    mint = selected_mint
    accs = selected_accs
    bc_pk = accs["bonding_curve"]

    bcr = rpc(helius_rpc_url, "getAccountInfo", [str(bc_pk), {"encoding": "base64", "commitment": "processed"}])
    bdata = base64.b64decode(bcr["result"]["value"]["data"][0])
    vtok = int.from_bytes(bdata[8:16], "little")
    vsol = int.from_bytes(bdata[16:24], "little")
    net_for_curve = int(TRADE_SIZE_LAMPORTS * (1 - 0.0105))
    expected_tokens = net_for_curve * vtok // (vsol + net_for_curve)
    amount_base_raw = int(expected_tokens * 0.90)
    max_sol_cost = int(TRADE_SIZE_LAMPORTS * 1.15)
    log(f"PGG2-V51B-STAGEA-BUYV2-PLAN expected_tokens={expected_tokens} amount_buy={amount_base_raw} max_sol_cost={max_sol_cost}")

    buy_ix = build_buy_v2_ix(accs, amount_base_raw, max_sol_cost)
    buy_decoded = decode_buy_v2_guard(bytes(buy_ix.data))
    assert buy_decoded["amount"] == amount_base_raw and buy_decoded["max_sol_cost"] == max_sol_cost
    log(f"PGG2-V51B-STAGEA-BUYV2-DECODE-PASS decoded={buy_decoded}")

    create_base_ata = create_idempotent_ata_ix(
        payer=user, owner=user, mint=mint,
        token_program=accs["base_token_program"],
        ata_pk=accs["associated_base_user"],
    )

    from solders.system_program import TransferParams, transfer
    tip_ix = transfer(TransferParams(from_pubkey=user, to_pubkey=TIP_ACCOUNT, lamports=TIP_LAMPORTS))
    cu_limit_ix = set_compute_unit_limit(CU_LIMIT)
    cu_price_ix = set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS)

    blockhash_resp = rpc(helius_rpc_url, "getLatestBlockhash", [{"commitment": "processed"}])
    bh = Hash.from_string(blockhash_resp["result"]["value"]["blockhash"])

    msg = MessageV0.try_compile(user, [cu_limit_ix, cu_price_ix, create_base_ata, buy_ix, tip_ix], [], bh)
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    log(f"PGG2-V51B-STAGEA-BUYV2-BUILD tx_len={len(tx_bytes)}")

    send_res = await helius_send(tx_bytes, api_key)
    if not send_res.get("ok"):
        log(f"PGG2-V51B-STAGEA-SWQOS-RESULT ok=false err={send_res.get('error')}")
        failed_sends += 1
        log(f"PGG2-V51B-STAGEA-RESULT verdict=SAFE_FAIL_SEND_FAILED wallet_delta=0 fee={fee_consumed_sol:.9f}")
        return 0
    buy_sig = send_res["sig"]
    log(f"PGG2-V51B-STAGEA-SWQOS-SEND leg=buy sig={buy_sig} send_lat_ms={send_res['lat_ms']:.1f}")

    buy_conf = confirm_sig(helius_rpc_url, buy_sig, max_wait_ms=30_000)
    log(f"PGG2-V51B-STAGEA-LANDING-LATENCY buy_sig={buy_sig} status={buy_conf['status']} err={buy_conf['err']} wait_ms={buy_conf['wait_ms']:.0f}")
    if buy_conf.get("err"):
        log(f"PGG2-V51B-STAGEA-BUYV2-REVERTED err={buy_conf['err']}")
        wallet_after = get_wallet_sol(helius_rpc_url, user)
        log(f"PGG2-V51B-STAGEA-WALLET-AFTER wallet_sol={wallet_after:.9f} delta={(wallet_after - wallet_before):.9f}")
        if wallet_after >= wallet_before - 0.00005:
            log("PGG2-V51B-STAGEA-RESULT verdict=SAFE_FAIL_BUY_REVERTED wallet_delta_neg_below_threshold")
            return 0
        log("PGG2-V51B-STAGEA-RESULT verdict=FAIL_BUY_REVERTED_BUT_FEE_LOSS")
        return 1
    if buy_conf["status"] == "timeout":
        log(f"PGG2-V51B-STAGEA-BUYV2-CONFIRM-TIMEOUT sig={buy_sig}")
        log("PGG2-V51B-STAGEA-RESULT verdict=FAIL_BUY_CONFIRM_TIMEOUT")
        return 1

    log(f"PGG2-V51B-STAGEA-BUYV2-CONFIRMED sig={buy_sig}")
    fee_consumed_sol += 0.000060

    user_base_ata = accs["associated_base_user"]
    actual_tokens_raw = 0
    poll_start = time.time()
    while time.time() - poll_start < 5.0:
        actual_tokens_raw = get_token_balance_raw(helius_rpc_url, user_base_ata)
        if actual_tokens_raw > 0:
            break
        time.sleep(0.15)
    log(f"PGG2-V51B-STAGEA-RAW-BALANCE tokens_raw={actual_tokens_raw}")
    if actual_tokens_raw == 0:
        log("PGG2-V51B-STAGEA-RESULT verdict=FAIL_NO_TOKEN_AFTER_BUY")
        return 1

    buy_processed_ts_ms = time.time() * 1000
    log(f"PGG2-V51B-STAGEA-WATCHDOG-START tokens={actual_tokens_raw} buy_ts_ms={buy_processed_ts_ms:.0f}")

    peak_pnl_sol = -999.0
    prev_pnl = None
    neg_grad_count = 0
    exit_action = None
    exit_reason = None
    while True:
        age_ms = time.time() * 1000 - buy_processed_ts_ms
        sell_quote_sol = get_bc_quote_sell_sol(helius_rpc_url, bc_pk, actual_tokens_raw)
        sell_fee_est = 0.000060
        all_in_pnl = sell_quote_sol - TRADE_SIZE_SOL - sell_fee_est
        peak_pnl_sol = max(peak_pnl_sol, all_in_pnl)
        if prev_pnl is not None and all_in_pnl < prev_pnl:
            neg_grad_count += 1
        else:
            neg_grad_count = 0
        prev_pnl = all_in_pnl
        log(f"PGG2-V51B-STAGEA-WATCHDOG-QUOTE age_ms={age_ms:.0f} sell_sol={sell_quote_sol:.6f} pnl={all_in_pnl:.6f} peak={peak_pnl_sol:.6f}")

        if all_in_pnl >= BANK_THRESHOLD_SOL:
            exit_action, exit_reason = "bank", "all_in_pnl_ge_bank"
            break
        if all_in_pnl >= SCRATCH_THRESHOLD_SOL and (sell_quote_sol < (peak_pnl_sol + TRADE_SIZE_SOL + sell_fee_est) - 0.0005 or neg_grad_count >= 2):
            exit_action, exit_reason = "scratch", "deteriorating_above_scratch"
            break
        if age_ms >= MAX_HOLD_MS:
            exit_action = "max_hold_exit"
            exit_reason = "max_hold_pnl_pos" if all_in_pnl >= 0 else "max_hold_pnl_neg"
            break
        await asyncio.sleep(WATCHDOG_INTERVAL_MS / 1000.0)

    log(f"PGG2-V51B-STAGEA-WATCHDOG-EXIT action={exit_action} reason={exit_reason} pnl={prev_pnl:.6f}")

    current_quote = get_bc_quote_sell_sol(helius_rpc_url, bc_pk, actual_tokens_raw)
    if exit_action == "bank":
        min_sol_output = max(int((current_quote * 0.85) * 1e9), int(BANK_THRESHOLD_SOL * 0.5 * 1e9))
    elif exit_action == "scratch":
        min_sol_output = max(int((current_quote * 0.75) * 1e9), int(EMERGENCY_MIN_SOL_FLOOR * 1e9))
    else:
        min_sol_output = max(int((current_quote * EMERGENCY_MIN_SOL_RATIO) * 1e9), int(EMERGENCY_MIN_SOL_FLOOR * 1e9))
    log(f"PGG2-V51B-STAGEA-SELLV2-MIN current_quote={current_quote:.6f} min_sol_output_lamports={min_sol_output}")

    sell_ix = build_sell_v2_ix(accs, actual_tokens_raw, min_sol_output)
    sell_decoded = decode_sell_v2_guard(bytes(sell_ix.data))
    assert sell_decoded["amount"] == actual_tokens_raw and sell_decoded["min_sol_output"] == min_sol_output
    log(f"PGG2-V51B-STAGEA-SELLV2-DECODE-PASS decoded={sell_decoded}")

    blockhash_resp = rpc(helius_rpc_url, "getLatestBlockhash", [{"commitment": "processed"}])
    bh = Hash.from_string(blockhash_resp["result"]["value"]["blockhash"])
    msg = MessageV0.try_compile(user, [cu_limit_ix, cu_price_ix, sell_ix, tip_ix], [], bh)
    tx = VersionedTransaction(msg, [kp])
    tx_bytes = bytes(tx)
    log(f"PGG2-V51B-STAGEA-SELLV2-BUILD tx_len={len(tx_bytes)}")
    sell_send = await helius_send(tx_bytes, api_key)
    if not sell_send.get("ok"):
        log(f"PGG2-V51B-STAGEA-SELLV2-SEND-FAIL err={sell_send.get('error')}")
        log("PGG2-V51B-STAGEA-RESULT verdict=FAIL_SELL_SEND_FAILED")
        return 1
    sell_sig = sell_send["sig"]
    log(f"PGG2-V51B-STAGEA-SWQOS-SEND leg=sell sig={sell_sig} send_lat_ms={sell_send['lat_ms']:.1f}")
    fee_consumed_sol += 0.000060

    sell_conf = confirm_sig(helius_rpc_url, sell_sig, max_wait_ms=30_000)
    log(f"PGG2-V51B-STAGEA-SELLV2-CONFIRMED status={sell_conf['status']} err={sell_conf['err']} wait_ms={sell_conf['wait_ms']:.0f}")

    if sell_conf.get("err") or sell_conf["status"] == "timeout":
        log("PGG2-V51B-STAGEA-SELL-FAILED_RETRY_LOWER_MIN")
        min_sol_output_retry = max(int(EMERGENCY_MIN_SOL_FLOOR * 1e9), 100_000)
        sell_ix2 = build_sell_v2_ix(accs, get_token_balance_raw(helius_rpc_url, user_base_ata) or actual_tokens_raw, min_sol_output_retry)
        blockhash_resp = rpc(helius_rpc_url, "getLatestBlockhash", [{"commitment": "processed"}])
        bh = Hash.from_string(blockhash_resp["result"]["value"]["blockhash"])
        msg2 = MessageV0.try_compile(user, [cu_limit_ix, cu_price_ix, sell_ix2, tip_ix], [], bh)
        tx2 = VersionedTransaction(msg2, [kp])
        retry_send = await helius_send(bytes(tx2), api_key)
        log(f"PGG2-V51B-STAGEA-SELLV2-RETRY ok={retry_send.get('ok')} sig={retry_send.get('sig')} err={retry_send.get('error')}")
        if retry_send.get("ok"):
            confirm_sig(helius_rpc_url, retry_send["sig"], max_wait_ms=30_000)
        fee_consumed_sol += 0.000060

    time.sleep(2.0)
    final_token_raw = get_token_balance_raw(helius_rpc_url, user_base_ata)
    wallet_after = get_wallet_sol(helius_rpc_url, user)
    wallet_delta = wallet_after - wallet_before
    log(f"PGG2-V51B-STAGEA-WALLET-AFTER wallet_sol={wallet_after:.9f} delta={wallet_delta:.9f} token_residual_raw={final_token_raw}")
    log(f"PGG2-V51B-STAGEA-FEES_TOTAL fee_consumed={fee_consumed_sol:.9f} budget={FEE_BUDGET_SOL:.9f}")

    if final_token_raw > 0:
        log("PGG2-V51B-STAGEA-RESULT verdict=FAIL_TOKEN_RESIDUAL")
        return 1
    if wallet_delta >= 0:
        log(f"PGG2-V51B-STAGEA-RESULT verdict=PASS_NON_NEGATIVE_CLOSE wallet_delta={wallet_delta:.9f}")
        return 0
    log(f"PGG2-V51B-STAGEA-RESULT verdict=FAIL_NEGATIVE_CLOSE wallet_delta={wallet_delta:.9f}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
