#!/usr/bin/env python3
"""V252 exact-positive PumpSwap single-tx Sender smoke.

Builds the same V245/V224 atomic multipool transaction, simulates exact payer
wallet delta, and posts to Helius Sender SWQOS only if the exact simulated
wallet delta is positive.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from solders.transaction import VersionedTransaction

from pgg2_v109_no_send_live_bundle_validation import _load_env, _make_broker
from pgg2_v224_pumpswap_multipool_builder import build_explicit_multipool_tx


READ_RPC = "https://public.rpc.solanavibestation.com"
SENDER_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"
WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
HELIUS_TIP_ACCOUNT = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"


def rpc(url: str, method: str, params: list, timeout: float = 20.0):
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


def main() -> int:
    _load_env()
    os.environ["HELIUS_RPC_URL"] = READ_RPC
    os.environ["SOLANA_RPC_URL"] = READ_RPC
    os.environ["V224_BUY_MODE"] = "exact_base_out"
    os.environ["V224_COMPUTE_BUDGET_MODE"] = "none"
    os.environ["V224_EXACT_BASE_REMAINING"] = "1"
    os.environ["V224_ADDRESS_LOOKUP_TABLE_JSON"] = "/root/piggy/data/v244_static_lut.json"
    os.environ["PGG2_DIRECT_TRACK_VOLUME"] = "0"
    os.environ["V224_CLOSE_USER_VOLUME"] = "1"
    os.environ["PGG2_JITO_TIP_ACCOUNT"] = HELIUS_TIP_ACCOUNT

    pre = int(rpc(READ_RPC, "getBalance", [WALLET, {"commitment": "processed"}])["value"])
    print(f"PGG2-V252-SENDER-PREFLIGHT-WALLET pre_lamports={pre}", flush=True)

    broker = _make_broker()
    broker.rpc_url = READ_RPC
    broker.refresh_blockhash_cache()

    rows = [
        json.loads(line)
        for line in open("/root/piggy/data/v223_v246_broad.jsonl", encoding="utf-8")
        if line.strip()
    ]
    rows.sort(key=lambda row: int(row.get("edge_lamports", 0)), reverse=True)

    scan_limit = max(1, int(os.environ.get("V252_SCAN_LIMIT", "80") or "80"))
    print(f"PGG2-V252-SENDER-SCAN-LIMIT rows={scan_limit}", flush=True)

    min_projected_edge = max(
        1, int(os.environ.get("V252_MIN_PROJECTED_EDGE_LAMPORTS", "4991") or "4991")
    )
    min_raw_edge = max(1, int(os.environ.get("V252_MIN_RAW_EDGE_LAMPORTS", "10000") or "10000"))
    print(
        f"PGG2-V252-SENDER-PROJECTED-MIN edge_lamports={min_projected_edge}",
        flush=True,
    )
    print(f"PGG2-V252-SENDER-RAW-MIN edge_lamports={min_raw_edge}", flush=True)

    best = None
    for idx, cand in enumerate(rows[:scan_limit], 1):
        raw_edge = int(cand.get("edge_lamports", 0))
        if raw_edge < min_raw_edge:
            print(
                "PGG2-V252-SENDER-RAW-BLOCK "
                f"idx={idx} mint={str(cand.get('mint', ''))[:4]} "
                f"edge={raw_edge} min_raw={min_raw_edge}",
                flush=True,
            )
            continue
        try:
            meta = build_explicit_multipool_tx(
                broker=broker,
                mint=str(cand["mint"]),
                buy_pool_key=str(cand["buy_pool"]),
                sell_pool_key=str(cand["sell_pool"]),
                size_lamports=int(cand["size_lamports"]),
                min_profit_lamports=1,
                fee_buffer_lamports=0,
                projection_buffer_lamports=0,
                tip_lamports=5000,
            )
        except Exception as exc:
            print(
                "PGG2-V252-SENDER-BUILD-BLOCK "
                f"idx={idx} mint={str(cand.get('mint', ''))[:4]} "
                f"edge={cand.get('edge_lamports')} "
                f"err={type(exc).__name__}:{str(exc)[:120]}",
                flush=True,
            )
            continue

        projected_edge = int(meta["projected_edge_lamports"])
        if projected_edge < min_projected_edge:
            print(
                "PGG2-V252-SENDER-PROJECTED-BLOCK "
                f"idx={idx} mint={str(cand.get('mint', ''))[:4]} "
                f"edge={cand.get('edge_lamports')} projected={projected_edge} "
                f"min_projected={min_projected_edge}",
                flush=True,
            )
            continue

        tx_b64 = str(meta["tx_b64"])
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
        sim = None
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
                delta = int(acct.get("lamports") or 0) - pre
                sim = {
                    "err": val.get("err"),
                    "delta": delta,
                    "rpc": url,
                    "units": val.get("unitsConsumed"),
                }
                break
            except Exception as exc:
                last = f"{type(exc).__name__}:{str(exc)[:120]}"
        if not sim:
            print(
                f"PGG2-V252-SENDER-SIM-BLOCK idx={idx} "
                f"mint={str(cand.get('mint', ''))[:4]} err={last}",
                flush=True,
            )
            continue
        print(
            "PGG2-V252-SENDER-EXACT-SIM "
            f"idx={idx} mint={str(cand['mint'])[:4]} edge={cand['edge_lamports']} "
            f"projected={meta['projected_edge_lamports']} raw_len={meta['tx_raw_len']} "
            f"delta={sim['delta']} err={sim['err']} rpc={sim['rpc']}",
            flush=True,
        )
        if sim["err"] is None and int(sim["delta"]) >= 1 and int(meta["tx_raw_len"]) <= 1232:
            best = (cand, meta, tx_b64, sim)
            break

    if not best:
        print("PGG2-V252-SENDER-NO-EXACT-POSITIVE", flush=True)
        return 2

    cand, meta, tx_b64, sim = best
    sig_preview = str(VersionedTransaction.from_bytes(base64.b64decode(tx_b64)).signatures[0])
    print(
        f"PGG2-V252-SENDER-EXACT-POSITIVE mint={str(cand['mint'])[:4]} "
        f"delta={sim['delta']} sig={sig_preview} tip=5000 endpoint=sender_swqos",
        flush=True,
    )
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
        print(f"PGG2-V252-SENDER-SEND-HTTP-ERR code={exc.code} body={body}", flush=True)
        raise
    print(f"PGG2-V252-SENDER-SEND-RAW {out}", flush=True)
    if out.get("error"):
        raise RuntimeError(out["error"])
    sig = str(out.get("result") or "")
    if not sig:
        raise RuntimeError("sender_returned_no_signature")
    print(f"PGG2-V252-SENDER-SEND sig={sig}", flush=True)

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
            print(f"PGG2-V252-SENDER-STATUS poll={poll} status={status}", flush=True)
            if status and (
                status.get("confirmationStatus") in ("processed", "confirmed", "finalized")
                or status.get("err") is not None
            ):
                break
        except Exception as exc:
            print(
                f"PGG2-V252-SENDER-STATUS-ERR poll={poll} "
                f"err={type(exc).__name__}:{str(exc)[:120]}",
                flush=True,
            )
    post = int(rpc(READ_RPC, "getBalance", [WALLET, {"commitment": "processed"}])["value"])
    print(f"PGG2-V252-SENDER-FINAL-WALLET pre={pre} post={post} delta={post - pre}", flush=True)
    if post < pre:
        print("PGG2-V252-SENDER-HARD-FAIL reason=negative_wallet_delta", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
