from __future__ import annotations

import base64
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from birth_first_sniper import (
    DISC_BUY,
    DISC_BUY_EXACT_SOL_IN,
    DISC_SELL,
    PUMP_PROGRAM,
    SOL_MINT,
    PumpEvent,
    env_bool,
    env_float,
    env_str,
    get_account_key,
    get_key,
    now_ms,
    now_ns,
)


PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
WSOL_MINT = SOL_MINT

DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN = bytes([198, 46, 21, 82, 180, 217, 232, 112])
DISC_PUMP_AMM_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def source_programs() -> list[str]:
    programs = [PUMP_PROGRAM]
    if env_bool("EXPERIMENTALJI_ENABLE_PUMPSWAP_SOURCE", True):
        programs.append(PUMP_AMM_PROGRAM)
    if env_bool("EXPERIMENTALJI_ENABLE_LAUNCHLAB_SOURCE", True):
        programs.append(LAUNCHLAB_PROGRAM)
    if env_bool("EXPERIMENTALJI_ENABLE_DBC_SOURCE", True):
        programs.append(METEORA_DBC_PROGRAM)
    return list(dict.fromkeys(programs))


def event_source(event: PumpEvent) -> str:
    kind = event.instruction_kind or ""
    if kind.startswith("pumpswap_"):
        return "pumpswap"
    if kind.startswith("launchlab_"):
        return "launchlab"
    if kind.startswith("dbc_"):
        return "dbc"
    return "pump"


def trade_enabled_for_source(source: str) -> bool:
    if source == "pump":
        return True
    if source == "pumpswap":
        return env_bool("EXPERIMENTALJI_TRADE_PUMPSWAP_SOURCE", True)
    if source == "launchlab":
        return env_bool("EXPERIMENTALJI_TRADE_LAUNCHLAB_SOURCE", False)
    if source == "dbc":
        return env_bool("EXPERIMENTALJI_TRADE_DBC_SOURCE", False)
    return False


@dataclass
class ExperimentalParseResult:
    events: list[PumpEvent] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    hits: Counter[str] = field(default_factory=Counter)


def _decode_shred(shred_result: dict[str, Any]) -> tuple[list[Pubkey], Any, str, int, str] | None:
    tx_outer = (shred_result.get("transaction") or {}).get("transaction")
    if not (isinstance(tx_outer, list) and tx_outer):
        return None
    raw = base64.b64decode(tx_outer[0])
    vt = VersionedTransaction.from_bytes(raw)
    keys = list(vt.message.account_keys)
    if not keys:
        return None
    signer = str(keys[0])
    slot = int(shred_result.get("slot") or 0)
    sig = str(shred_result.get("signature") or "")
    return keys, vt.message, signer, slot, sig


def _price_hint(sol_lamports: int, token_amount: int) -> float:
    if sol_lamports <= 0 or token_amount <= 0:
        return 0.0
    return sol_lamports / max(token_amount, 1)


def parse_experimental_shred(shred_result: dict[str, Any], tracked_wallets: set[str]) -> ExperimentalParseResult:
    """Parse non-baseline sources from a Solana Tracker base64 shred.

    This deliberately excludes baseline pump.fun bonding-curve instructions;
    the existing parser remains the source of truth for those. The extra
    sources are normalized into PumpEvent so experimentalji can reuse the same
    tape, feature, quote, and dry-live accounting path.
    """
    out = ExperimentalParseResult()
    try:
        decoded = _decode_shred(shred_result)
        if decoded is None:
            return out
        keys, msg, signer, slot, sig = decoded
        ts_ms = now_ms()
        recv_ns = now_ns()
        max_trade_lamports = int(env_float("EXPERIMENTALJI_MAX_DECODED_TRADE_SOL", 500.0) * 1_000_000_000)
        for ix in msg.instructions:
            try:
                program = get_key(keys, int(ix.program_id_index))
            except Exception:
                continue
            if program == PUMP_PROGRAM:
                continue
            data = bytes(ix.data)
            accounts = list(ix.accounts)
            if not data or len(data) < 8:
                continue

            if program == PUMP_AMM_PROGRAM:
                out.hits["pumpswap_ix"] += 1
                event = _parse_pumpswap_ix(
                    keys,
                    accounts,
                    data,
                    signer,
                    sig,
                    slot,
                    ts_ms,
                    recv_ns,
                    tracked_wallets,
                    max_trade_lamports,
                )
                if event:
                    out.events.append(event)
                continue

            if program == LAUNCHLAB_PROGRAM:
                out.hits["launchlab_ix"] += 1
                event = _parse_launchlab_ix(
                    keys,
                    accounts,
                    data,
                    signer,
                    sig,
                    slot,
                    ts_ms,
                    recv_ns,
                    tracked_wallets,
                    max_trade_lamports,
                )
                if event:
                    out.events.append(event)
                continue

            if program == METEORA_DBC_PROGRAM:
                out.hits["dbc_ix"] += 1
                out.observations.append(
                    {
                        "source": "dbc",
                        "kind": "dbc_instruction",
                        "ts_ms": ts_ms,
                        "sig": sig,
                        "slot": slot,
                        "signer": signer,
                        "disc": list(data[:8]),
                        "accounts": [get_account_key(keys, accounts, i) for i in range(min(8, len(accounts)))],
                    }
                )
    except Exception as exc:
        out.hits[f"parse_error:{type(exc).__name__}"] += 1
    return out


def _parse_pumpswap_ix(
    keys: list[Pubkey],
    accounts: list[int],
    data: bytes,
    signer: str,
    sig: str,
    slot: int,
    ts_ms: int,
    recv_ns: int,
    tracked_wallets: set[str],
    max_trade_lamports: int,
) -> Optional[PumpEvent]:
    disc = data[:8]
    if len(data) < 24:
        return None
    pool = get_account_key(keys, accounts, 0)
    user = get_account_key(keys, accounts, 1) or signer
    base_mint = get_account_key(keys, accounts, 3)
    quote_mint = get_account_key(keys, accounts, 4)
    if quote_mint and quote_mint != WSOL_MINT:
        return None
    if not base_mint or not pool:
        return None
    if disc == DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN:
        sol_lamports = int.from_bytes(data[8:16], "little")
        min_base_out = int.from_bytes(data[16:24], "little")
        if sol_lamports <= 0 or sol_lamports > max_trade_lamports:
            return None
        return PumpEvent(
            ts_ms=ts_ms,
            recv_ns=recv_ns,
            sig=sig,
            slot=slot,
            signer=signer,
            kind="trade",
            mint=base_mint,
            bonding_curve=pool,
            is_buy=True,
            sol_lamports=sol_lamports,
            token_amount=min_base_out,
            user=user,
            tracked=(signer in tracked_wallets) or (user in tracked_wallets),
            instruction_kind="pumpswap_buy_exact_quote_in",
            price_hint=_price_hint(sol_lamports, min_base_out),
        )
    if disc == DISC_PUMP_AMM_SELL:
        base_amount = int.from_bytes(data[8:16], "little")
        min_quote_out = int.from_bytes(data[16:24], "little")
        if min_quote_out <= 0 or min_quote_out > max_trade_lamports or base_amount <= 0:
            return None
        return PumpEvent(
            ts_ms=ts_ms,
            recv_ns=recv_ns,
            sig=sig,
            slot=slot,
            signer=signer,
            kind="trade",
            mint=base_mint,
            bonding_curve=pool,
            is_buy=False,
            sol_lamports=min_quote_out,
            token_amount=base_amount,
            user=user,
            tracked=(signer in tracked_wallets) or (user in tracked_wallets),
            instruction_kind="pumpswap_sell",
            price_hint=_price_hint(min_quote_out, base_amount),
        )
    return None


def _parse_launchlab_ix(
    keys: list[Pubkey],
    accounts: list[int],
    data: bytes,
    signer: str,
    sig: str,
    slot: int,
    ts_ms: int,
    recv_ns: int,
    tracked_wallets: set[str],
    max_trade_lamports: int,
) -> Optional[PumpEvent]:
    # LaunchLab/LetsBONK direct trades observed in the legacy bot used the same
    # buy/sell discriminators and first two u64 amount fields as pump.fun. Treat
    # this as normalized evidence first; entries stay disabled by default until
    # a LaunchLab-specific executor is proven.
    disc = data[:8]
    if len(data) < 24 or disc not in {DISC_BUY, DISC_BUY_EXACT_SOL_IN, DISC_SELL}:
        return None
    mint = get_account_key(keys, accounts, 2)
    pool = get_account_key(keys, accounts, 3)
    user = get_account_key(keys, accounts, 6) or signer
    if not mint:
        return None
    is_buy = disc in {DISC_BUY, DISC_BUY_EXACT_SOL_IN}
    first_u64 = int.from_bytes(data[8:16], "little")
    second_u64 = int.from_bytes(data[16:24], "little")
    if disc == DISC_BUY_EXACT_SOL_IN:
        if first_u64 > max_trade_lamports and 0 < second_u64 <= max_trade_lamports:
            token_amount = first_u64
            sol_lamports = second_u64
        else:
            sol_lamports = first_u64
            token_amount = second_u64
        instruction_kind = "launchlab_buy_exact_sol_in"
    else:
        token_amount = first_u64
        sol_lamports = second_u64
        instruction_kind = "launchlab_buy" if is_buy else "launchlab_sell"
    if sol_lamports <= 0 or token_amount <= 0 or sol_lamports > max_trade_lamports:
        return None
    return PumpEvent(
        ts_ms=ts_ms,
        recv_ns=recv_ns,
        sig=sig,
        slot=slot,
        signer=signer,
        kind="trade",
        mint=mint,
        bonding_curve=pool,
        is_buy=is_buy,
        sol_lamports=sol_lamports,
        token_amount=token_amount,
        user=user,
        tracked=(signer in tracked_wallets) or (user in tracked_wallets),
        instruction_kind=instruction_kind,
        price_hint=_price_hint(sol_lamports, token_amount),
    )


def append_observations(path: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True, default=str) + "\n")
    except Exception:
        return
