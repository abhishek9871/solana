#!/usr/bin/env python3
"""V108 external raw pump buy transaction decoder.

Pure decode/verify. No wallet access and no transaction send path.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from solders.transaction import VersionedTransaction  # type: ignore


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
MEMO_PROGRAM_V2 = "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"
DISC_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_BUY_EXACT_SOL_IN = bytes([56, 252, 116, 8, 158, 223, 205, 95])
DISC_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
SETUP_PROGRAMS = {
    COMPUTE_BUDGET_PROGRAM,
    SYSTEM_PROGRAM,
    ASSOCIATED_TOKEN_PROGRAM,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    MEMO_PROGRAM,
    MEMO_PROGRAM_V2,
}


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if s and len(s) > 10 else (s or "?")


@dataclass
class ExternalPumpBuy:
    raw_tx_b64: str
    signature: str
    fee_payer: str
    mint: str
    bonding_curve: str
    user: str
    instruction_kind: str
    sol_lamports: int
    token_amount_raw: int
    slot: int = 0
    source: str = "unknown"


def _key(keys: list[Any], idx: int) -> str:
    try:
        return str(keys[int(idx)])
    except Exception:
        return ""


def decode_external_pump_buy(raw_tx_b64: str, *, expected_sig: str = "", source: str = "unknown", slot: int = 0) -> ExternalPumpBuy:
    raw = base64.b64decode(raw_tx_b64)
    tx = VersionedTransaction.from_bytes(raw)
    keys = list(tx.message.account_keys)
    signature = str(tx.signatures[0]) if tx.signatures else expected_sig
    if expected_sig and signature and expected_sig != signature:
        raise ValueError(f"signature_mismatch expected={expected_sig} actual={signature}")
    fee_payer = str(keys[0]) if keys else ""
    found: Optional[ExternalPumpBuy] = None
    seen_non_setup_before_pump: list[str] = []
    for ix in tx.message.instructions:
        program = _key(keys, int(ix.program_id_index))
        if program != PUMP_PROGRAM:
            if program and program not in SETUP_PROGRAMS:
                seen_non_setup_before_pump.append(program)
            continue
        if seen_non_setup_before_pump:
            raise ValueError(
                "external_tx_has_pre_pump_program:"
                + ",".join(seen_non_setup_before_pump[:3])
            )
        data = bytes(ix.data)
        if len(data) < 24:
            continue
        disc = data[:8]
        accounts = list(ix.accounts)
        if disc not in {DISC_BUY, DISC_BUY_EXACT_SOL_IN, DISC_SELL}:
            continue
        if disc == DISC_SELL:
            raise ValueError("external_tx_is_sell_not_buy")
        mint = _key(keys, accounts[2] if len(accounts) > 2 else -1)
        curve = _key(keys, accounts[3] if len(accounts) > 3 else -1)
        user = _key(keys, accounts[6] if len(accounts) > 6 else 0) or fee_payer
        first = int.from_bytes(data[8:16], "little")
        second = int.from_bytes(data[16:24], "little")
        if disc == DISC_BUY_EXACT_SOL_IN:
            # Current exact-sol-in builder encodes SOL first, min tokens second.
            sol_lamports = first
            token_amount = second
            kind = "buy_exact_sol_in"
        else:
            token_amount = first
            sol_lamports = second
            kind = "buy"
        if not mint or not curve or sol_lamports <= 0:
            continue
        found = ExternalPumpBuy(
            raw_tx_b64=raw_tx_b64,
            signature=signature,
            fee_payer=fee_payer,
            mint=mint,
            bonding_curve=curve,
            user=user,
            instruction_kind=kind,
            sol_lamports=sol_lamports,
            token_amount_raw=token_amount,
            slot=int(slot or 0),
            source=source,
        )
        break
    if found is None:
        raise ValueError("no_pump_buy_instruction")
    # Reject mixed suspicious pump sell in same external tx.
    for ix in tx.message.instructions:
        program = _key(keys, int(ix.program_id_index))
        if program == PUMP_PROGRAM and bytes(ix.data).startswith(DISC_SELL):
            raise ValueError("external_tx_contains_pump_sell")
    return found


def decode_record(rec: dict[str, Any]) -> ExternalPumpBuy:
    return decode_external_pump_buy(
        str(rec.get("raw_tx_b64") or ""),
        expected_sig=str(rec.get("signature") or ""),
        source=str(rec.get("source") or "jsonl"),
        slot=int(rec.get("slot") or 0),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default="/root/piggy/data/v108_raw_tx_capture_audit.jsonl")
    args = ap.parse_args()
    for line in Path(args.jsonl).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if not rec.get("raw_tx_b64"):
            continue
        try:
            decoded = decode_record(rec)
        except Exception as exc:
            _log(f"PGG2-V108-EXTERNAL-TX-DECODE-BLOCK err={type(exc).__name__}:{exc}")
            return 1
        _log(
            f"PGG2-V108-EXTERNAL-TX-DECODE-PASS mint={_short(decoded.mint)} "
            f"sig={decoded.signature[:16]} curve={_short(decoded.bonding_curve)} "
            f"sol_lamports={decoded.sol_lamports} kind={decoded.instruction_kind}"
        )
        print(json.dumps(asdict(decoded), sort_keys=True))
        return 0
    _log("PGG2-V108-EXTERNAL-TX-DECODE-BLOCK reason=no_raw_tx_b64_record")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
