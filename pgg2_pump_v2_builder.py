"""V51B Pump v2 instruction builder + decoder."""
from __future__ import annotations
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from pgg2_pump_v2_idl_constants import (
    BUY_V2_DISCRIMINATOR, SELL_V2_DISCRIMINATOR,
    BUY_V2_ACCOUNT_FLAGS, SELL_V2_ACCOUNT_FLAGS,
    PUMP_PROGRAM_ID,
)


class V2BuilderError(Exception):
    pass


def _build_metas(account_flags: list, accounts: dict) -> list[AccountMeta]:
    metas = []
    for name, writable, signer in account_flags:
        if name not in accounts:
            raise V2BuilderError(f"missing_account name={name}")
        pk = accounts[name]
        if not isinstance(pk, Pubkey):
            raise V2BuilderError(f"non_pubkey name={name} type={type(pk).__name__}")
        metas.append(AccountMeta(pk, signer, writable))
    return metas


def build_buy_v2_ix(accounts: dict, amount_base_raw: int, max_sol_cost_lamports: int) -> Instruction:
    if amount_base_raw <= 0 or amount_base_raw >= 2**64:
        raise V2BuilderError(f"amount_out_of_range {amount_base_raw}")
    if max_sol_cost_lamports <= 0 or max_sol_cost_lamports >= 2**64:
        raise V2BuilderError(f"max_sol_cost_out_of_range {max_sol_cost_lamports}")
    data = (
        BUY_V2_DISCRIMINATOR
        + amount_base_raw.to_bytes(8, "little")
        + max_sol_cost_lamports.to_bytes(8, "little")
    )
    metas = _build_metas(BUY_V2_ACCOUNT_FLAGS, accounts)
    return Instruction(PUMP_PROGRAM_ID, data, metas)


def build_sell_v2_ix(accounts: dict, amount_base_raw: int, min_sol_output_lamports: int) -> Instruction:
    if amount_base_raw <= 0 or amount_base_raw >= 2**64:
        raise V2BuilderError(f"amount_out_of_range {amount_base_raw}")
    if min_sol_output_lamports < 0 or min_sol_output_lamports >= 2**64:
        raise V2BuilderError(f"min_sol_output_out_of_range {min_sol_output_lamports}")
    data = (
        SELL_V2_DISCRIMINATOR
        + amount_base_raw.to_bytes(8, "little")
        + min_sol_output_lamports.to_bytes(8, "little")
    )
    metas = _build_metas(SELL_V2_ACCOUNT_FLAGS, accounts)
    return Instruction(PUMP_PROGRAM_ID, data, metas)


def decode_buy_v2_guard(ix_data: bytes) -> dict:
    if len(ix_data) < 24:
        raise V2BuilderError(f"buy_v2_data_too_short {len(ix_data)}")
    if ix_data[:8] != BUY_V2_DISCRIMINATOR:
        raise V2BuilderError("buy_v2_disc_mismatch")
    return {
        "amount": int.from_bytes(ix_data[8:16], "little"),
        "max_sol_cost": int.from_bytes(ix_data[16:24], "little"),
    }


def decode_sell_v2_guard(ix_data: bytes) -> dict:
    if len(ix_data) < 24:
        raise V2BuilderError(f"sell_v2_data_too_short {len(ix_data)}")
    if ix_data[:8] != SELL_V2_DISCRIMINATOR:
        raise V2BuilderError("sell_v2_disc_mismatch")
    return {
        "amount": int.from_bytes(ix_data[8:16], "little"),
        "min_sol_output": int.from_bytes(ix_data[16:24], "little"),
    }
