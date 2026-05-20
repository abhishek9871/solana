"""V53 Stage A live runner — V51B v2/SWQOS + V53 risk gate.

Flow:
1. Find a current active pump.fun bonding-curve mint (via Pump program tx scan)
2. Holder breadth gate (count >= 4)
3. V53 risk gate (SolanaTracker risk intelligence)
4. Resolve all 27/26 v2 accounts; build buy_v2 + create_idempotent ATA
5. SWQOS-only send via Helius Sender
6. Watchdog poll bonding curve every 250ms; max_hold 1500ms
7. Sell via sell_v2 + SWQOS
8. Stop after 1 close (pass or fail)
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
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

sys.path.insert(0, "/root/piggy")
from pgg2_pump_v2_idl_constants import (
    BUY_V2_DISCRIMINATOR, SELL_V2_DISCRIMINATOR,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID, SYSTEM_PROGRAM_ID,
    PUMP_PROGRAM_ID, NATIVE_MINT,
)
from pgg2_pump_v2_accounts import resolve_v2_accounts_sol_paired, V2AccountsError, ata, pda
from pgg2_pump_v2_builder import build_buy_v2_ix, build_sell_v2_ix, decode_buy_v2_guard, decode_sell_v2_guard
from pgg2_v53_risk_intelligence import V53RiskChecker, evaluate_risk_veto, DEFAULT_RULES


TIP_LAMPORTS = 5_000
TIP_SOL = TIP_LAMPORTS / 1e9
TRADE_SIZE_SOL = 0.005
TRADE_SIZE_LAMPORTS = int(TRADE_SIZE_SOL * 1e9)
FEE_BUDGET_SOL = 0.00030
PRIORITY_FEE_MICROLAMPORTS = 100_000
CU_LIMIT = 400_000
MAX_HOLD_MS = 5000  # extended — flat curves in 1500ms guaranteed fee loss
WATCHDOG_INTERVAL_MS = 250
WALLET_DRAWDOWN_HARDCAP_SOL = 0.008
HOLDER_MIN_COUNT = 20  # raised — dead/dormant coins block
BANK_THRESHOLD_SOL = 0.00060
SCRATCH_THRESHOLD_SOL = 0.00005
EMERGENCY_MIN_SOL_RATIO = 0.40
EMERGENCY_MIN_SOL_FLOOR = 0.0005
HELIUS_SENDER_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"
TIP_ACCOUNT = Pubkey.from_string("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE")
OBSERVE_MODE = os.environ.get("PGG2_V53_OBSERVE_ONLY") == "1"
MAX_RUN_S = int(os.environ.get("PGG2_V53_MAX_SECONDS", "1200"))


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


def create_idempotent_ata_ix(payer, owner, mint, token_program, ata_pk):
    metas = [
        AccountMeta(payer, True, True),
        AccountMeta(ata_pk, False, True),
        AccountMeta(owner, False, False),
        AccountMeta(mint, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(token_program, False, False),
    ]
    return Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes([1]), metas)


def find_candidates(helius_url: str, seen: set, max_attempts: int = 30) -> list[str]:
    """Return list of fresh candidate mints from recent Pump program tx scan."""
    try:
        r = rpc(helius_url, "getSignaturesForAddress", [str(PUMP_PROGRAM_ID), {"limit": max_attempts}])
    except Exception:
        return []
    sigs = [s["signature"] for s in r.get("result", [])][:max_attempts]
    fresh = []
    for sig in sigs:
        try:
            tx = rpc(helius_url, "getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            v = tx.get("result")
            if not v or not v.get("meta"):
                continue
            for tb in v["meta"].get("postTokenBalances", []):
                m = tb.get("mint", "")
                if m and m.endswith("pump") and m not in seen:
                    fresh.append(m)
                    seen.add(m)
        except Exception:
            continue
    return fresh


def fetch_bc_state(helius_url: str, bc_pk: Pubkey) -> tuple[int, int, bool]:
    """Returns (vtok, vsol, complete)."""
    try:
        r = rpc(helius_url, "getAccountInfo", [str(bc_pk), {"encoding": "base64", "commitment": "processed"}])
        v = r.get("result", {}).get("value")
        if not v:
            return (0, 0, True)
        data = base64.b64decode(v["data"][0])
        if len(data) < 49:
            return (0, 0, True)
        vtok = int.from_bytes(data[8:16], "little")
        vsol = int.from_bytes(data[16:24], "little")
        complete = bool(data[48])
        return (vtok, vsol, complete)
    except Exception:
        return (0, 0, True)


def get_token_account_balance_raw(helius_url: str, ata_pk: Pubkey) -> int:
    try:
        r = rpc(helius_url, "getTokenAccountBalance", [str(ata_pk), {"commitment": "processed"}])
        amt = r.get("result", {}).get("value", {}).get("amount")
        return int(amt) if amt else 0
    except Exception:
        return 0


def get_wallet_sol(helius_url: str, user: Pubkey) -> float:
    try:
        r = rpc(helius_url, "getBalance", [str(user), {"commitment": "processed"}])
        return r.get("result", {}).get("value", 0) / 1e9
    except Exception:
        return 0.0


def bc_sell_quote_sol(vsol: int, vtok: int, tokens_in: int) -> float:
    if vtok + tokens_in <= 0:
        return 0.0
    sol_out_raw = (vsol * tokens_in) // (vtok + tokens_in)
    return (sol_out_raw / 1e9) * (1 - 0.0105)


async def helius_swqos_send(signed_bytes: bytes) -> dict:
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


def confirm_sig(helius_url: str, sig: str, max_wait_ms: int = 30_000) -> dict:
    t0 = time.time()
    while (time.time() - t0) * 1000 < max_wait_ms:
        try:
            r = rpc(helius_url, "getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            v = r.get("result", {}).get("value", [None])[0]
            if v and v.get("confirmationStatus"):
                return {"status": v["confirmationStatus"], "err": v.get("err"), "slot": v.get("slot"), "wait_ms": (time.time() - t0) * 1000}
        except Exception:
            pass
        time.sleep(0.2)
    return {"status": "timeout", "err": None, "slot": None, "wait_ms": (time.time() - t0) * 1000}


async def execute_one_entry(helius_url: str, kp: Keypair, mint_str: str, accs: dict, vtok: int, vsol: int, user: Pubkey) -> dict:
    bc_pk = accs["bonding_curve"]
    net_for_curve = int(TRADE_SIZE_LAMPORTS * (1 - 0.0105))
    expected_tokens = net_for_curve * vtok // (vsol + net_for_curve)
    amount_buy = int(expected_tokens * 0.90)
    max_sol_cost = int(TRADE_SIZE_LAMPORTS * 1.15)
    log(f"PGG2-V53-BUYV2-PLAN mint={mint_str[:14]}.. expected_tokens={expected_tokens} amount_buy={amount_buy} max_sol_cost={max_sol_cost}")

    buy_ix = build_buy_v2_ix(accs, amount_buy, max_sol_cost)
    dec = decode_buy_v2_guard(bytes(buy_ix.data))
    assert dec["amount"] == amount_buy and dec["max_sol_cost"] == max_sol_cost
    log(f"PGG2-V53-BUYV2-DECODE-PASS {dec}")

    create_ata = create_idempotent_ata_ix(user, user, accs["base_mint"], accs["base_token_program"], accs["associated_base_user"])
    tip_ix = transfer(TransferParams(from_pubkey=user, to_pubkey=TIP_ACCOUNT, lamports=TIP_LAMPORTS))
    cu_lim = set_compute_unit_limit(CU_LIMIT)
    cu_pr = set_compute_unit_price(PRIORITY_FEE_MICROLAMPORTS)

    bh_resp = rpc(helius_url, "getLatestBlockhash", [{"commitment": "processed"}])
    bh = Hash.from_string(bh_resp["result"]["value"]["blockhash"])
    msg = MessageV0.try_compile(user, [cu_lim, cu_pr, create_ata, buy_ix, tip_ix], [], bh)
    tx = VersionedTransaction(msg, [kp])
    log(f"PGG2-V53-BUYV2-BUILD tx_len={len(bytes(tx))}")

    send = await helius_swqos_send(bytes(tx))
    if not send.get("ok"):
        log(f"PGG2-V53-SWQOS-RESULT leg=buy ok=false err={send.get('error')}")
        return {"ok": False, "stage": "buy_send_failed", "err": send.get("error")}
    buy_sig = send["sig"]
    log(f"PGG2-V53-SWQOS-SEND leg=buy sig={buy_sig} send_lat_ms={send['lat_ms']:.1f}")

    bc = confirm_sig(helius_url, buy_sig, 30_000)
    log(f"PGG2-V53-BUYV2-CONFIRM sig={buy_sig} status={bc['status']} err={bc['err']} wait_ms={bc['wait_ms']:.0f}")
    if bc.get("err") or bc["status"] == "timeout":
        return {"ok": False, "stage": "buy_reverted_or_timeout", "err": bc.get("err"), "buy_sig": buy_sig}

    user_base_ata = accs["associated_base_user"]
    actual_tokens = 0
    t0 = time.time()
    while time.time() - t0 < 5.0:
        actual_tokens = get_token_account_balance_raw(helius_url, user_base_ata)
        if actual_tokens > 0:
            break
        time.sleep(0.15)
    log(f"PGG2-V53-RAW-BALANCE tokens_raw={actual_tokens}")
    if actual_tokens == 0:
        return {"ok": False, "stage": "no_token_after_buy", "buy_sig": buy_sig}

    buy_processed_ts_ms = time.time() * 1000
    log(f"PGG2-V53-WATCHDOG-START tokens={actual_tokens}")
    peak_pnl = -999.0
    prev_pnl = None
    neg_grad = 0
    exit_action = None
    exit_reason = None
    while True:
        age = time.time() * 1000 - buy_processed_ts_ms
        vt2, vs2, _ = fetch_bc_state(helius_url, bc_pk)
        sell_sol = bc_sell_quote_sol(vs2, vt2, actual_tokens) if vt2 and vs2 else 0.0
        pnl = sell_sol - TRADE_SIZE_SOL - 0.000060
        peak_pnl = max(peak_pnl, pnl)
        if prev_pnl is not None and pnl < prev_pnl:
            neg_grad += 1
        else:
            neg_grad = 0
        prev_pnl = pnl
        log(f"PGG2-V53-WATCHDOG-QUOTE age_ms={age:.0f} sell_sol={sell_sol:.6f} pnl={pnl:.6f}")
        if pnl >= BANK_THRESHOLD_SOL:
            exit_action, exit_reason = "bank", "ge_bank"
            break
        if pnl >= SCRATCH_THRESHOLD_SOL and (sell_sol < (peak_pnl + TRADE_SIZE_SOL + 0.000060) - 0.0005 or neg_grad >= 2):
            exit_action, exit_reason = "scratch", "deteriorating"
            break
        if age >= MAX_HOLD_MS:
            exit_action = "max_hold_exit"
            exit_reason = "pnl_pos" if pnl >= 0 else "pnl_neg"
            break
        await asyncio.sleep(WATCHDOG_INTERVAL_MS / 1000.0)

    log(f"PGG2-V53-WATCHDOG-EXIT action={exit_action} reason={exit_reason} pnl={prev_pnl:.6f}")
    current_q = bc_sell_quote_sol(vs2, vt2, actual_tokens) if vt2 and vs2 else 0.0
    if exit_action == "bank":
        min_sol = max(int((current_q * 0.85) * 1e9), int(BANK_THRESHOLD_SOL * 0.5 * 1e9))
    elif exit_action == "scratch":
        min_sol = max(int((current_q * 0.75) * 1e9), int(EMERGENCY_MIN_SOL_FLOOR * 1e9))
    else:
        min_sol = max(int((current_q * EMERGENCY_MIN_SOL_RATIO) * 1e9), int(EMERGENCY_MIN_SOL_FLOOR * 1e9))
    log(f"PGG2-V53-SELLV2-MIN current_quote={current_q:.6f} min_sol_lamports={min_sol}")

    sell_ix = build_sell_v2_ix(accs, actual_tokens, min_sol)
    sdec = decode_sell_v2_guard(bytes(sell_ix.data))
    assert sdec["amount"] == actual_tokens and sdec["min_sol_output"] == min_sol
    log(f"PGG2-V53-SELLV2-DECODE-PASS {sdec}")

    bh_resp = rpc(helius_url, "getLatestBlockhash", [{"commitment": "processed"}])
    bh = Hash.from_string(bh_resp["result"]["value"]["blockhash"])
    msg2 = MessageV0.try_compile(user, [cu_lim, cu_pr, sell_ix, tip_ix], [], bh)
    tx2 = VersionedTransaction(msg2, [kp])
    ss = await helius_swqos_send(bytes(tx2))
    if not ss.get("ok"):
        log(f"PGG2-V53-SELLV2-SEND-FAIL err={ss.get('error')}")
        return {"ok": False, "stage": "sell_send_failed", "buy_sig": buy_sig, "err": ss.get("error")}
    sell_sig = ss["sig"]
    log(f"PGG2-V53-SWQOS-SEND leg=sell sig={sell_sig} send_lat_ms={ss['lat_ms']:.1f}")

    sc = confirm_sig(helius_url, sell_sig, 30_000)
    log(f"PGG2-V53-SELLV2-CONFIRM status={sc['status']} err={sc['err']} wait_ms={sc['wait_ms']:.0f}")

    if sc.get("err") or sc["status"] == "timeout":
        log("PGG2-V53-SELLV2-RETRY low_min_sol")
        retry_min = max(int(EMERGENCY_MIN_SOL_FLOOR * 1e9), 100_000)
        retry_amt = get_token_account_balance_raw(helius_url, user_base_ata) or actual_tokens
        sell_ix2 = build_sell_v2_ix(accs, retry_amt, retry_min)
        bh_resp = rpc(helius_url, "getLatestBlockhash", [{"commitment": "processed"}])
        bh = Hash.from_string(bh_resp["result"]["value"]["blockhash"])
        msg3 = MessageV0.try_compile(user, [cu_lim, cu_pr, sell_ix2, tip_ix], [], bh)
        tx3 = VersionedTransaction(msg3, [kp])
        rt = await helius_swqos_send(bytes(tx3))
        if rt.get("ok"):
            confirm_sig(helius_url, rt["sig"], 30_000)

    time.sleep(2.0)
    final_token = get_token_account_balance_raw(helius_url, user_base_ata)
    return {"ok": True, "buy_sig": buy_sig, "sell_sig": sell_sig, "final_token_raw": final_token, "exit_action": exit_action}


async def main() -> int:
    env = load_env()
    helius_key = env["HELIUS_API_KEY"]
    st_data_key = env.get("SOLANATRACKER_DATA_API_KEY", "").strip()
    if not st_data_key:
        log("PGG2-V53-CONFIG-ERR no SOLANATRACKER_DATA_API_KEY")
        return 2

    helius_url = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
    kp = load_keypair()
    user = kp.pubkey()
    risk = V53RiskChecker(st_data_key)

    wallet_before = get_wallet_sol(helius_url, user)
    log(f"PGG2-V53-START user={user} wallet_before={wallet_before:.9f} observe_only={OBSERVE_MODE}")

    deadline = time.time() + MAX_RUN_S
    seen_mints: set = set()
    candidates_seen = 0
    blocked_by: dict[str, int] = {}
    passed = 0

    while time.time() < deadline:
        fresh = find_candidates(helius_url, seen_mints, max_attempts=30)
        if not fresh:
            time.sleep(2)
            continue

        for mint_str in fresh:
            if time.time() >= deadline:
                break
            candidates_seen += 1
            mint = Pubkey.from_string(mint_str)

            # Fast bonding curve sanity
            bc = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
            vtok, vsol, complete = fetch_bc_state(helius_url, bc)
            if complete or vsol < 30 * 1_000_000_000:
                blocked_by["bc_complete_or_thin"] = blocked_by.get("bc_complete_or_thin", 0) + 1
                continue

            # Resolve accounts
            try:
                accs = resolve_v2_accounts_sol_paired(helius_url, mint, user)
            except V2AccountsError as e:
                log(f"PGG2-V53-V2-ACCOUNTS-BLOCK mint={mint_str[:14]}.. err={e}")
                blocked_by["v2_accounts_block"] = blocked_by.get("v2_accounts_block", 0) + 1
                continue
            except Exception as e:
                msg = str(e)
                log(f"PGG2-V53-V2-ACCOUNTS-RPC-ERR mint={mint_str[:14]}.. err={msg[:80]}")
                blocked_by["rpc_error"] = blocked_by.get("rpc_error", 0) + 1
                if "429" in msg:
                    time.sleep(2.0)
                continue

            # V53 risk gate
            features = risk.fetch(mint_str)
            if not features.get("ok"):
                log(f"PGG2-V53-RISK-UNAVAILABLE mint={mint_str[:14]}.. err={features.get('error')}")
                blocked_by[f"risk_{features.get('error','unknown')}"] = blocked_by.get(f"risk_{features.get('error','unknown')}", 0) + 1
                # rate-limited → wait briefly and continue scan; don't burn the candidate
                if features.get("error") == "rate_limited":
                    time.sleep(1.1)
                continue
            ok_risk, blockers = evaluate_risk_veto(features)
            log_summary = (f"score={features.get('score')} bndP={features.get('bundlers_pct')} "
                           f"devP={features.get('dev_pct')} sniP={features.get('snipers_pct')} "
                           f"top10={features.get('top10_pct')} holders={features.get('holders')} dangers={features.get('danger_names')}")
            log(f"PGG2-V53-RISK-GATE mint={mint_str[:14]}.. pass={ok_risk} blockers={blockers} {log_summary}")
            if not ok_risk:
                blocked_by[blockers[0]] = blocked_by.get(blockers[0], 0) + 1
                continue

            # Holder count gate (also covered by risk gate but enforced explicitly)
            if (features.get("holders") or 0) < HOLDER_MIN_COUNT:
                blocked_by["holder_count_lt_4"] = blocked_by.get("holder_count_lt_4", 0) + 1
                continue

            passed += 1
            log(f"PGG2-V53-CANDIDATE-PASSED mint={mint_str[:14]}.. passed={passed}")

            if OBSERVE_MODE:
                continue  # observe-only; never send

            # Execute live
            res = await execute_one_entry(helius_url, kp, mint_str, accs, vtok, vsol, user)
            time.sleep(2)
            wallet_after = get_wallet_sol(helius_url, user)
            delta = wallet_after - wallet_before
            log(f"PGG2-V53-WALLET wallet_after={wallet_after:.9f} delta={delta:+.9f}")
            if res.get("ok"):
                if res.get("final_token_raw", 0) > 0:
                    log(f"PGG2-V53-RESULT verdict=FAIL_TOKEN_RESIDUAL token_raw={res['final_token_raw']} delta={delta:+.9f}")
                    return 1
                if delta >= 0:
                    log(f"PGG2-V53-RESULT verdict=PASS_NON_NEGATIVE_CLOSE delta={delta:+.9f}")
                    return 0
                log(f"PGG2-V53-RESULT verdict=FAIL_NEGATIVE_CLOSE delta={delta:+.9f}")
                return 1
            # Buy reverted or send failed = SAFE_FAIL
            log(f"PGG2-V53-RESULT verdict=SAFE_FAIL stage={res.get('stage')} err={res.get('err')} delta={delta:+.9f}")
            return 0

        if time.time() < deadline and not fresh:
            time.sleep(2)

    log(f"PGG2-V53-DEADLINE-REACHED candidates_seen={candidates_seen} passed={passed} blocked_by={dict(sorted(blocked_by.items(), key=lambda x: -x[1])[:10])}")
    log("PGG2-V53-RESULT verdict=SAFE_FAIL_NO_PASS_IN_DEADLINE")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
