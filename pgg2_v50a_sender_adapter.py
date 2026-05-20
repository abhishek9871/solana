"""V50A Helius Sender adapter — SWQOS-only paid execution path.

This module is EXEMPT from the global send_* call-prohibition because
it IS the live tx sender. All other V50A files must remain call-clean.

Public API:
  - build_v50a_tx(unsigned_tx_b64, keypair, swqos_tip_sol,
                  priority_fee_microlamports, compute_unit_limit,
                  tip_account_pubkey) -> bytes
        Decode an unsigned base64 V0 tx, splice in the
        ComputeBudget(setComputeUnitLimit + setComputeUnitPrice) and the
        SystemProgram.transfer tip instruction, re-sign with the wallet
        keypair, and return raw signed tx bytes.

  - async send_via_helius_sender_swqos(signed_tx_bytes, helius_api_key)
        POST to https://sender.helius-rpc.com/fast?swqos_only=true with the
        exact Helius Sender JSON-RPC envelope and measure send latency.

  - async confirm_signature(signature, helius_api_key, max_wait_ms=30000)
        Poll getSignatureStatuses on Helius mainnet RPC every ~200ms
        until processed/confirmed/finalized or timeout/err.

All logs follow the PGG2-V50A-SENDER-* and PGG2-V50A-LANDING-LATENCY
emit patterns.
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, CompiledInstruction, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

# CONSTANT — absolute hard cap per V50A run.
PGG2_V50A_MAX_TIP_SOL = 0.000005

HELIUS_SENDER_FAST_SWQOS_URL = "https://sender.helius-rpc.com/fast?swqos_only=true"

# Standard SPL transfer system program id constant.
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# Standard Compute Budget program id.
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string(
    "ComputeBudget111111111111111111111111111111"
)


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def _decompile_compiled(
    compiled: CompiledInstruction,
    src_msg,  # MessageV0
    account_keys: List[Pubkey],
    *,
    lookup_writable: List[Pubkey],
    lookup_readonly: List[Pubkey],
) -> Instruction:
    """Reconstruct a high-level Instruction from a CompiledInstruction,
    using the original MessageV0 header to recover is_signer / is_writable
    flags for each AccountMeta. Without this, `try_compile` would mark every
    re-used account as read-only-non-signer, which breaks instructions that
    require writable references (ATA program, Pump program, etc.) and
    surfaces as InstructionError::PrivilegeEscalation during simulation.

    For V0 messages, address-lookup-table entries appear at indices >=
    len(static_account_keys); the order is [static_keys, alt_writable,
    alt_readonly] per the Solana spec. We resolve them via the supplied
    `lookup_writable` / `lookup_readonly` lists, marking ALT-writable as
    writable and ALT-readonly as readonly. ALT entries are never signers.
    """
    program_id_index = int(compiled.program_id_index)
    static_n = len(account_keys)
    lw_n = len(lookup_writable)

    def resolve(idx: int) -> Tuple[Pubkey, bool, bool]:
        """Returns (pubkey, is_signer, is_writable) for an index in the
        instruction's account list. Indices < static_n point at static
        account_keys; indices in [static_n, static_n+lw_n) point at ALT
        writable; >= static_n+lw_n point at ALT readonly."""
        if idx < static_n:
            pk = account_keys[idx]
            return pk, bool(src_msg.is_signer(idx)), bool(src_msg.is_maybe_writable(idx))
        elif idx < static_n + lw_n:
            return lookup_writable[idx - static_n], False, True
        else:
            return lookup_readonly[idx - static_n - lw_n], False, False

    # Program id from the static account-key region (program ids never go
    # through ALT in V0 messages).
    pid = account_keys[program_id_index]

    accounts_list: List[AccountMeta] = []
    for idx in compiled.accounts:
        pk, is_signer, is_writable = resolve(int(idx))
        accounts_list.append(AccountMeta(pk, is_signer, is_writable))
    return Instruction(pid, bytes(compiled.data), accounts_list)


def build_v50a_tx(
    unsigned_tx_b64: str,
    keypair: Keypair,
    swqos_tip_sol: float,
    priority_fee_microlamports: int,
    compute_unit_limit: int,
    tip_account_pubkey: str,
) -> bytes:
    """Decode the V48-built unsigned base64 tx, prepend ComputeBudget
    instructions (CU limit + price), append a SystemProgram.transfer of
    `swqos_tip_sol` SOL to `tip_account_pubkey`, sign with `keypair`, and
    return raw signed tx bytes ready for Helius Sender.

    Assertion: tip_sol <= PGG2_V50A_MAX_TIP_SOL.
    """
    if swqos_tip_sol > PGG2_V50A_MAX_TIP_SOL + 1e-12:
        raise RuntimeError(
            f"V50A tip exceeds hard cap: tip_sol={swqos_tip_sol:.9f} "
            f"max={PGG2_V50A_MAX_TIP_SOL:.9f}"
        )
    if swqos_tip_sol <= 0:
        raise RuntimeError(f"V50A tip must be > 0 (got {swqos_tip_sol})")

    tip_lamports = int(round(swqos_tip_sol * 1_000_000_000))
    tip_pk = Pubkey.from_string(tip_account_pubkey)
    payer_pk = keypair.pubkey()

    # 1. Decode the unsigned VersionedTransaction (V0 message).
    raw = base64.b64decode(unsigned_tx_b64)
    src_tx = VersionedTransaction.from_bytes(raw)
    src_msg = src_tx.message

    # 2. Decompile the existing instructions back to high-level Instructions,
    # preserving each AccountMeta's is_signer / is_writable from the original
    # MessageV0 header. Then strip any existing ComputeBudget ixs.
    src_account_keys = list(src_msg.account_keys)
    src_alts = list(getattr(src_msg, "address_table_lookups", []) or [])

    # For ALT entries, we don't have the actual pubkeys without an RPC
    # lookup against the table account. Pump.fun bonding-curve buys built
    # by DirectPumpQuoteBroker.compile_tx pass an empty ALT list, so this
    # path is exercised cleanly. If callers ever pass an ALT-using tx,
    # we'd need to fetch the table contents; raise loudly so we don't
    # silently produce a broken tx.
    if src_alts:
        raise RuntimeError(
            "V50A: source tx uses address_table_lookups; cannot splice "
            "without ALT account fetch. ALT lookups not supported in V50A."
        )

    src_instructions: List[Instruction] = []
    for cix in src_msg.instructions:
        ix = _decompile_compiled(
            cix,
            src_msg,
            src_account_keys,
            lookup_writable=[],
            lookup_readonly=[],
        )
        if str(ix.program_id) == str(COMPUTE_BUDGET_PROGRAM_ID):
            # Drop existing compute-budget ixs; we re-prepend ours below.
            continue
        src_instructions.append(ix)

    # 3. Build our compute-budget and tip-transfer ixs.
    cu_limit_ix = set_compute_unit_limit(int(compute_unit_limit))
    cu_price_ix = set_compute_unit_price(int(priority_fee_microlamports))
    tip_ix = transfer(
        TransferParams(
            from_pubkey=payer_pk,
            to_pubkey=tip_pk,
            lamports=tip_lamports,
        )
    )

    # 4. Re-compile the message with the new instruction order:
    #    [cu_limit, cu_price, *src_ixs, tip_transfer].
    instructions = [cu_limit_ix, cu_price_ix, *src_instructions, tip_ix]
    recent_blockhash: Hash = src_msg.recent_blockhash

    new_msg = MessageV0.try_compile(
        payer_pk,
        instructions,
        [],  # we explicitly rejected ALT input above
        recent_blockhash,
    )

    # 5. Sign.
    new_tx = VersionedTransaction(new_msg, [keypair])
    return bytes(new_tx)


async def send_via_helius_sender_swqos(
    signed_tx_bytes: bytes,
    helius_api_key: str,
    *,
    timeout_s: float = 8.0,
) -> Dict[str, Any]:
    """POST to Helius Sender SWQOS-only endpoint. Returns:
      {"signature": str, "send_latency_ms": float, "raw_response": dict}
    or on failure:
      {"error": str, "send_latency_ms": float, "raw_response": dict}
    """
    tx_b64 = base64.b64encode(signed_tx_bytes).decode("ascii")
    body = {
        "jsonrpc": "2.0",
        "id": "v50a",
        "method": "sendTransaction",
        "params": [
            tx_b64,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 0,
            },
        ],
    }
    # api_key is appended as a query param OR via Authorization. Per Helius
    # Sender docs the endpoint accepts unauthenticated for sendTransaction
    # (rate-limited globally) — we still pass it as a query arg for tracking.
    url = HELIUS_SENDER_FAST_SWQOS_URL
    if helius_api_key:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}api-key={helius_api_key}"

    t0 = time.time()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as s:
            async with s.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as r:
                raw_text = await r.text()
                latency_ms = (time.time() - t0) * 1000.0
                try:
                    data: Dict[str, Any] = await _safe_json(raw_text)
                except Exception:
                    data = {"_raw": raw_text}
                if r.status != 200:
                    return {
                        "error": f"http_{r.status}",
                        "send_latency_ms": latency_ms,
                        "raw_response": data,
                    }
                if "error" in data:
                    return {
                        "error": str(data.get("error")),
                        "send_latency_ms": latency_ms,
                        "raw_response": data,
                    }
                sig = data.get("result")
                if not sig or not isinstance(sig, str):
                    return {
                        "error": "no_signature_in_response",
                        "send_latency_ms": latency_ms,
                        "raw_response": data,
                    }
                return {
                    "signature": sig,
                    "send_latency_ms": latency_ms,
                    "raw_response": data,
                }
    except Exception as exc:
        latency_ms = (time.time() - t0) * 1000.0
        return {
            "error": f"exc:{type(exc).__name__}:{exc}",
            "send_latency_ms": latency_ms,
            "raw_response": {},
        }


async def _safe_json(raw_text: str) -> Dict[str, Any]:
    import json as _json
    return _json.loads(raw_text)


async def confirm_signature(
    signature: str,
    helius_api_key: str,
    *,
    max_wait_ms: int = 30_000,
    poll_interval_ms: int = 200,
) -> Dict[str, Any]:
    """Poll getSignatureStatuses on Helius mainnet RPC.

    Returns:
      {"status": "processed"|"confirmed"|"finalized"|"failed"|"timeout",
       "slot": int, "processed_at_ms": float, "confirmed_at_ms": float,
       "err": dict or None, "polls": int}

    `processed_at_ms` is set as soon as we observe ANY non-null status (the
    tx made it on-chain). `confirmed_at_ms` is set when confirmationStatus
    crosses "confirmed" (or "finalized") for the first time.
    """
    url = f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
    t0 = time.time()
    processed_at_ms = 0.0
    confirmed_at_ms = 0.0
    last_slot = 0
    last_err = None
    polls = 0
    deadline = t0 + (max_wait_ms / 1000.0)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=5)
    ) as s:
        while time.time() < deadline:
            polls += 1
            body = {
                "jsonrpc": "2.0",
                "id": polls,
                "method": "getSignatureStatuses",
                "params": [
                    [signature],
                    {"searchTransactionHistory": False},
                ],
            }
            try:
                async with s.post(
                    url,
                    json=body,
                    headers={"Content-Type": "application/json"},
                ) as r:
                    if r.status != 200:
                        await asyncio.sleep(poll_interval_ms / 1000.0)
                        continue
                    data = await r.json()
                    statuses = (
                        ((data or {}).get("result") or {}).get("value") or []
                    )
                    status_row = statuses[0] if statuses else None
                    if status_row:
                        last_slot = int(status_row.get("slot") or 0)
                        err = status_row.get("err")
                        cs = str(
                            status_row.get("confirmationStatus") or "processed"
                        )
                        if processed_at_ms == 0.0:
                            processed_at_ms = (time.time() - t0) * 1000.0
                        if err:
                            last_err = err
                            return {
                                "status": "failed",
                                "slot": last_slot,
                                "processed_at_ms": processed_at_ms,
                                "confirmed_at_ms": 0.0,
                                "err": err,
                                "polls": polls,
                            }
                        if cs in {"confirmed", "finalized"}:
                            confirmed_at_ms = (time.time() - t0) * 1000.0
                            return {
                                "status": cs,
                                "slot": last_slot,
                                "processed_at_ms": processed_at_ms,
                                "confirmed_at_ms": confirmed_at_ms,
                                "err": None,
                                "polls": polls,
                            }
                        # else: processed but not confirmed yet — keep polling.
            except Exception:
                pass
            await asyncio.sleep(poll_interval_ms / 1000.0)

    return {
        "status": "timeout",
        "slot": last_slot,
        "processed_at_ms": processed_at_ms,
        "confirmed_at_ms": confirmed_at_ms,
        "err": last_err,
        "polls": polls,
    }


# ---------------- Convenience helper for callers ----------------

async def build_sign_send_confirm(
    *,
    unsigned_tx_b64: str,
    keypair: Keypair,
    swqos_tip_sol: float,
    priority_fee_microlamports: int,
    compute_unit_limit: int,
    tip_account_pubkey: str,
    helius_api_key: str,
    confirm_max_wait_ms: int = 30_000,
    mint_short: str = "",
    leg: str = "buy",
) -> Dict[str, Any]:
    """High-level wrapper for one-shot build->send->confirm with full
    instrumentation. Used by the V50A stage runner.

    Returns dict with: signature, send_latency_ms, total_landing_ms,
    processed_at_ms, confirmed_at_ms, status, err, tx_size_bytes.
    """
    signed_bytes = build_v50a_tx(
        unsigned_tx_b64=unsigned_tx_b64,
        keypair=keypair,
        swqos_tip_sol=swqos_tip_sol,
        priority_fee_microlamports=priority_fee_microlamports,
        compute_unit_limit=compute_unit_limit,
        tip_account_pubkey=tip_account_pubkey,
    )
    tx_size = len(signed_bytes)
    _log(
        f"PGG2-V50A-SENDER-SEND mint={mint_short} leg={leg} "
        f"tip_sol={swqos_tip_sol:.9f} priority_micro={priority_fee_microlamports} "
        f"cu_limit={compute_unit_limit} tx_size_bytes={tx_size}"
    )
    send_t0 = time.time()
    send_result = await send_via_helius_sender_swqos(
        signed_bytes, helius_api_key
    )
    sig = send_result.get("signature")
    err = send_result.get("error")
    send_latency_ms = float(send_result.get("send_latency_ms", 0.0))
    _log(
        f"PGG2-V50A-SENDER-RESULT mint={mint_short} leg={leg} "
        f"sig={sig or 'NONE'} send_latency_ms={send_latency_ms:.1f} "
        f"ok={bool(sig)} err={err!r}"
    )
    if not sig:
        return {
            "signature": None,
            "send_latency_ms": send_latency_ms,
            "total_landing_ms": 0.0,
            "processed_at_ms": 0.0,
            "confirmed_at_ms": 0.0,
            "status": "send_failed",
            "err": err,
            "tx_size_bytes": tx_size,
            "raw_response": send_result.get("raw_response"),
        }
    confirm = await confirm_signature(
        sig,
        helius_api_key,
        max_wait_ms=confirm_max_wait_ms,
    )
    total_landing_ms = (time.time() - send_t0) * 1000.0
    _log(
        f"PGG2-V50A-LANDING-LATENCY mint={mint_short} leg={leg} "
        f"sig={sig} processed_at_ms={confirm.get('processed_at_ms', 0.0):.1f} "
        f"confirmed_at_ms={confirm.get('confirmed_at_ms', 0.0):.1f} "
        f"total_landing_ms={total_landing_ms:.1f} status={confirm.get('status')}"
    )
    return {
        "signature": sig,
        "send_latency_ms": send_latency_ms,
        "total_landing_ms": total_landing_ms,
        "processed_at_ms": float(confirm.get("processed_at_ms", 0.0)),
        "confirmed_at_ms": float(confirm.get("confirmed_at_ms", 0.0)),
        "status": str(confirm.get("status")),
        "err": confirm.get("err"),
        "tx_size_bytes": tx_size,
        "raw_response": send_result.get("raw_response"),
    }
