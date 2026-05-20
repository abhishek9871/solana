"""V40 Atomic Route Arbitrage builder + simulator.

Builds a SINGLE on-chain transaction that:
  1) compute budget ixs
  2) create_idempotent ATAs (base mint + WSOL when pumpswap leg is present)
  3) buy ix on `buy_route` with `min_tokens_out = sell_tokens_raw` exactly
  4) sell ix on `sell_route` with `min_sol_lamports = floor(min_sol_out * 1e9)` exactly
  5) close ATA(s) best-effort (WSOL always; base mint only if env opt-in)

If the buy leg under-fills (delivers fewer tokens than `sell_tokens_raw`),
the buy reverts on-chain and the whole tx fails. If the sell leg can't
return the encoded `min_sol_lamports`, the sell reverts and the tx fails.
Either way no position opens and the wallet is unchanged (minus base sig fee).

Hard invariants enforced + checked after build:
- Buy data encodes `min_tokens_out == sell_tokens_raw` exactly
- Sell data encodes `min_sol_lamports == floor(min_sol_out * 1e9)` exactly
- After build, the serialized tx is decoded and the encoded fields are
  re-read from the buy/sell ix data. Any mismatch raises.

ABSOLUTELY NO send_signed / sendTransaction / send_signed_rpc allowed in this
module. Only `simulate_signed_atomic` (RPC simulateTransaction) is used.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any, Optional

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from birth_first_sniper import env_bool, env_int, log, short_addr
from pgg2_direct_pump import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    DirectPumpQuoteBroker,
    DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN,
    DISC_PUMP_AMM_SELL,
    DISC_PUMP_BUY_EXACT_SOL_IN,
    DISC_PUMP_SELL,
    PUMP_AMM_PROGRAM_ID,
    PUMP_FEE_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    WSOL_MINT,
    PumpBondingCurve,
    PumpSwapPool,
    as_pubkey,
    close_token_account,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    pda,
    sync_native,
    u64,
)
from pgg2_live_raptor import LAMPORTS_PER_SOL, b58encode
from pgg2_v40_route_matrix import RouteAvailability, probe_route_availability


LEGACY_TX_MAX_BYTES = 1232


@dataclass
class AtomicArbBuild:
    mint: str
    buy_route: str
    sell_route: str
    buy_amount_sol: float
    spend_lamports: int
    sell_tokens_raw: int
    min_sol_out_sol: float
    encoded_min_tokens_raw: int
    encoded_min_sol_lamports: int
    unsigned_b64: str
    signed_b64: str
    signed_b58: str
    tx_size_bytes: int
    compute_unit_limit: int
    compute_unit_price_micro_lamports: int
    ix_count: int
    ix_summary: dict[str, Any]


def _compute_budget_ixs(limit: int, price_microlamports: int) -> list[Instruction]:
    return [set_compute_unit_limit(int(limit)), set_compute_unit_price(int(price_microlamports))]


def _pump_bc_buy_ix(
    broker: DirectPumpQuoteBroker,
    mint: Pubkey,
    curve: PumpBondingCurve,
    spend_lamports: int,
    min_tokens_out: int,
) -> tuple[Instruction, dict[str, Any]]:
    """Mirror of build_atomic_buy_sell_close's buy leg, decoupled."""
    global_cfg = broker.pump_global()
    user = as_pubkey(broker.public_key)
    token_program = broker.mint_owner(mint)
    user_ata = get_associated_token_address(user, mint, token_program)
    associated_curve = get_associated_token_address(curve.key, mint, token_program)
    creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
    fee_recipient = broker.pump_fee_recipient(global_cfg, curve)
    user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
    track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
    data = DISC_PUMP_BUY_EXACT_SOL_IN + u64(spend_lamports) + u64(min_tokens_out) + track_volume
    metas = [
        AccountMeta(broker.pump_global_key, False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(curve.key, False, True),
        AccountMeta(associated_curve, False, True),
        AccountMeta(user_ata, False, True),
        AccountMeta(user, True, True),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(token_program, False, False),
        AccountMeta(creator_vault, False, True),
        AccountMeta(broker.pump_event_authority, False, False),
        AccountMeta(PUMP_PROGRAM_ID, False, False),
        AccountMeta(broker.pump_global_volume_accumulator, False, False),
        AccountMeta(user_volume, False, True),
        AccountMeta(broker.pump_fee_config, False, False),
        AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
        *broker.pump_buy_remaining_metas(mint),
    ]
    return Instruction(PUMP_PROGRAM_ID, data, metas), {
        "user_ata": user_ata,
        "token_program": token_program,
        "user": user,
    }


def _pump_bc_sell_ix(
    broker: DirectPumpQuoteBroker,
    mint: Pubkey,
    curve: PumpBondingCurve,
    token_amount: int,
    min_sol_lamports: int,
) -> Instruction:
    global_cfg = broker.pump_global()
    user = as_pubkey(broker.public_key)
    token_program = broker.mint_owner(mint)
    user_ata = get_associated_token_address(user, mint, token_program)
    associated_curve = get_associated_token_address(curve.key, mint, token_program)
    creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
    fee_recipient = broker.pump_fee_recipient(global_cfg, curve)
    data = DISC_PUMP_SELL + u64(token_amount) + u64(min_sol_lamports)
    metas = [
        AccountMeta(broker.pump_global_key, False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(curve.key, False, True),
        AccountMeta(associated_curve, False, True),
        AccountMeta(user_ata, False, True),
        AccountMeta(user, True, True),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(creator_vault, False, True),
        AccountMeta(token_program, False, False),
        AccountMeta(broker.pump_event_authority, False, False),
        AccountMeta(PUMP_PROGRAM_ID, False, False),
        AccountMeta(broker.pump_fee_config, False, False),
        AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
        *broker.pump_sell_remaining_metas(mint, curve, user),
    ]
    return Instruction(PUMP_PROGRAM_ID, data, metas)


def _pumpswap_buy_ix(
    broker: DirectPumpQuoteBroker,
    mint: Pubkey,
    pool: PumpSwapPool,
    spend_lamports: int,
    min_base_out: int,
) -> tuple[Instruction, dict[str, Any]]:
    global_cfg = broker.pumpswap_global()
    user = as_pubkey(broker.public_key)
    base_token_program = broker.mint_owner(mint)
    quote_token_program = TOKEN_PROGRAM_ID
    user_base_ata = get_associated_token_address(user, mint, base_token_program)
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)
    fee_recipient = broker.pumpswap_fee_recipient(global_cfg, pool)
    fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, quote_token_program)
    creator_vault_authority = pda(PUMP_AMM_PROGRAM_ID, b"creator_vault", bytes(pool.coin_creator))
    creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, quote_token_program)
    user_volume = pda(PUMP_AMM_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
    track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
    data = DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN + u64(spend_lamports) + u64(min_base_out) + track_volume
    metas = broker.pumpswap_common_metas(
        pool,
        user,
        mint,
        user_base_ata,
        user_quote_ata,
        fee_recipient,
        fee_recipient_ata,
        base_token_program,
        quote_token_program,
        creator_vault_ata,
        creator_vault_authority,
        user_volume,
        include_volume=True,
    )
    return Instruction(PUMP_AMM_PROGRAM_ID, data, metas), {
        "user_base_ata": user_base_ata,
        "user_quote_ata": user_quote_ata,
        "base_token_program": base_token_program,
        "quote_token_program": quote_token_program,
        "user": user,
    }


def _pumpswap_sell_ix(
    broker: DirectPumpQuoteBroker,
    mint: Pubkey,
    pool: PumpSwapPool,
    base_amount: int,
    min_quote_out: int,
) -> Instruction:
    global_cfg = broker.pumpswap_global()
    user = as_pubkey(broker.public_key)
    base_token_program = broker.mint_owner(mint)
    quote_token_program = TOKEN_PROGRAM_ID
    user_base_ata = get_associated_token_address(user, mint, base_token_program)
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)
    fee_recipient = broker.pumpswap_fee_recipient(global_cfg, pool)
    fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, quote_token_program)
    creator_vault_authority = pda(PUMP_AMM_PROGRAM_ID, b"creator_vault", bytes(pool.coin_creator))
    creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, quote_token_program)
    data = DISC_PUMP_AMM_SELL + u64(base_amount) + u64(min_quote_out)
    metas = broker.pumpswap_common_metas(
        pool,
        user,
        mint,
        user_base_ata,
        user_quote_ata,
        fee_recipient,
        fee_recipient_ata,
        base_token_program,
        quote_token_program,
        creator_vault_ata,
        creator_vault_authority,
        None,
        include_volume=False,
    )
    return Instruction(PUMP_AMM_PROGRAM_ID, data, metas)


def _decode_verify_buy_min(ix_data: bytes, intended_min_tokens: int, buy_route: str) -> int:
    """Read the encoded min_tokens_out u64 out of the serialized buy ix and
    raise if it doesn't match `intended_min_tokens`. Returns the encoded
    value on success."""
    if buy_route == "pump_bc":
        disc = DISC_PUMP_BUY_EXACT_SOL_IN
    else:
        disc = DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN
    if not ix_data.startswith(disc):
        raise RuntimeError(f"buy ix data missing discriminator route={buy_route}")
    encoded = struct.unpack("<Q", ix_data[len(disc) + 8 : len(disc) + 16])[0]
    if encoded != int(intended_min_tokens):
        raise RuntimeError(
            f"buy min_tokens mismatch route={buy_route} "
            f"encoded={encoded} intended={int(intended_min_tokens)}"
        )
    return int(encoded)


def _decode_verify_sell_min(ix_data: bytes, intended_min_sol_lp: int, sell_route: str) -> int:
    """Read the encoded min_sol_lamports u64 out of the serialized sell ix.
    Raise on mismatch."""
    disc = DISC_PUMP_SELL if sell_route == "pump_bc" else DISC_PUMP_AMM_SELL
    if not ix_data.startswith(disc):
        raise RuntimeError(f"sell ix data missing discriminator route={sell_route}")
    encoded = struct.unpack("<Q", ix_data[len(disc) + 8 : len(disc) + 16])[0]
    if encoded != int(intended_min_sol_lp):
        raise RuntimeError(
            f"sell min_sol mismatch route={sell_route} "
            f"encoded={encoded} intended={int(intended_min_sol_lp)}"
        )
    return int(encoded)


def _decode_ix_data_for_program(tx_b64: str, program_id: Pubkey, disc: bytes) -> Optional[bytes]:
    """Locate the first compiled instruction with `program_id` whose data
    starts with `disc` in the serialized VersionedTransaction. Returns the
    raw data bytes or None."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    keys = list(msg.account_keys)
    for cix in msg.instructions:
        pid = keys[cix.program_id_index]
        if pid != program_id:
            continue
        data = bytes(cix.data)
        if data.startswith(disc):
            return data
    return None


def build_atomic_route_arb_tx(
    broker: DirectPumpQuoteBroker,
    mint_str: str,
    buy_route: str,
    sell_route: str,
    buy_amount_sol: float,
    sell_tokens_raw: int,
    min_sol_out: float,
    availability: Optional[RouteAvailability] = None,
    *,
    compute_unit_limit: int = 700_000,
    compute_unit_price_micro_lamports: int = 22_700,
    include_base_close: Optional[bool] = None,
    allow_same_route: bool = False,
) -> AtomicArbBuild:
    """Build an atomic buy-on-A → sell-on-B transaction for `mint`.

    Args
    ----
    buy_route, sell_route :
      "pump_bc" or "pumpswap" or "pumpswap_{idx}".
    buy_amount_sol :
      SOL spend on the buy leg.
    sell_tokens_raw :
      Token raw units to sell. ALSO used as the buy leg's `min_tokens_out`
      so the tx reverts if buy under-fills.
    min_sol_out :
      Encoded floor for the sell leg's SOL output, in SOL (converted to
      lamports via `floor(* 1e9)` for the on-chain encoding).
    availability :
      Optional pre-resolved RouteAvailability. If None, will be probed.
    """
    if sell_tokens_raw <= 0:
        raise RuntimeError("sell_tokens_raw must be > 0")
    if buy_amount_sol <= 0:
        raise RuntimeError("buy_amount_sol must be > 0")
    if min_sol_out < 0:
        raise RuntimeError("min_sol_out must be >= 0")
    if buy_route == sell_route and not allow_same_route:
        raise RuntimeError(
            f"build_atomic_route_arb_tx: buy_route == sell_route ({buy_route}); "
            "same-route round-trip is mathematically negative; "
            "pass allow_same_route=True to override (control runs only)"
        )

    mint = as_pubkey(mint_str)
    av = availability or probe_route_availability(broker, mint_str)
    spend_lamports = max(1, int(buy_amount_sol * LAMPORTS_PER_SOL))
    # Hard invariants: buy min_tokens == sell input, sell min_sol == floor(min_sol*1e9).
    intended_min_tokens = int(sell_tokens_raw)
    intended_min_sol_lp = int(min_sol_out * LAMPORTS_PER_SOL)  # python int() truncates toward zero

    user = as_pubkey(broker.public_key)

    # --- compose ATAs needed ---
    base_token_program: Pubkey
    needs_pumpswap = (
        buy_route.startswith("pumpswap") or sell_route.startswith("pumpswap")
    )
    base_token_program = broker.mint_owner(mint)
    user_base_ata = get_associated_token_address(user, mint, base_token_program)
    user_quote_ata = get_associated_token_address(user, WSOL_MINT, TOKEN_PROGRAM_ID)

    # --- buy instruction ---
    if buy_route == "pump_bc":
        if not av.curve:
            raise RuntimeError("pump_bc buy route requested but bonding curve missing")
        if av.curve.complete:
            raise RuntimeError("pump_bc buy route requested but curve already migrated")
        buy_ix, _buy_extra = _pump_bc_buy_ix(broker, mint, av.curve, spend_lamports, intended_min_tokens)
    elif buy_route.startswith("pumpswap"):
        pool = av.pool_for(buy_route)
        if pool is None:
            raise RuntimeError(f"pumpswap buy route requested but no pool for {buy_route}")
        buy_ix, _buy_extra = _pumpswap_buy_ix(broker, mint, pool, spend_lamports, intended_min_tokens)
    else:
        raise RuntimeError(f"unsupported buy_route={buy_route}")

    # --- sell instruction ---
    if sell_route == "pump_bc":
        if not av.curve:
            raise RuntimeError("pump_bc sell route requested but bonding curve missing")
        if av.curve.complete:
            raise RuntimeError("pump_bc sell route requested but curve already migrated")
        sell_ix = _pump_bc_sell_ix(broker, mint, av.curve, sell_tokens_raw, intended_min_sol_lp)
    elif sell_route.startswith("pumpswap"):
        pool = av.pool_for(sell_route)
        if pool is None:
            raise RuntimeError(f"pumpswap sell route requested but no pool for {sell_route}")
        sell_ix = _pumpswap_sell_ix(broker, mint, pool, sell_tokens_raw, intended_min_sol_lp)
    else:
        raise RuntimeError(f"unsupported sell_route={sell_route}")

    # --- compose instruction list ---
    ixs: list[Instruction] = [
        set_compute_unit_limit(int(compute_unit_limit)),
        set_compute_unit_price(int(compute_unit_price_micro_lamports)),
    ]

    # Base mint ATA (always — both routes deposit/spend the user's base ATA)
    ixs.append(create_idempotent_associated_token_account(user, user, mint, base_token_program))

    # WSOL ATA + fund/sync (only for pumpswap legs; pump_bc native-SOL only)
    if needs_pumpswap:
        ixs.append(create_idempotent_associated_token_account(user, user, WSOL_MINT, TOKEN_PROGRAM_ID))
    # If buy leg is pumpswap, prefund WSOL ATA with spend_lamports + sync_native
    # (mirrors build_pumpswap_buy). pump_bc buy does NOT need WSOL because the
    # program reads SOL directly from the signer.
    if buy_route.startswith("pumpswap"):
        ixs.append(transfer(TransferParams(from_pubkey=user, to_pubkey=user_quote_ata, lamports=spend_lamports)))
        ixs.append(sync_native(TOKEN_PROGRAM_ID, user_quote_ata))

    # --- buy then sell ---
    ixs.append(buy_ix)
    ixs.append(sell_ix)

    # --- close WSOL ATA (best-effort) to recapture rent ---
    if needs_pumpswap:
        ixs.append(close_token_account(TOKEN_PROGRAM_ID, user_quote_ata, user, user))

    # Base ATA close: usually leaves residual dust (buy fills > min). Default
    # is to NOT close inside the atomic tx — same as build_atomic_buy_sell_close.
    if include_base_close is None:
        include_base_close = env_bool("PGG2_V40_INCLUDE_BASE_CLOSE", False)
    if include_base_close:
        ixs.append(close_token_account(base_token_program, user_base_ata, user, user))

    # --- compile + sign + size ---
    unsigned_b64 = broker.compile_tx(ixs)
    signed_b64, signed_b58 = broker.sign_transaction(unsigned_b64)
    signed_bytes_len = len(base64.b64decode(signed_b64))

    if signed_bytes_len > LEGACY_TX_MAX_BYTES:
        log(
            f"PGG2-V40-ATOMIC-BLOCKER reason=tx_too_large bytes={signed_bytes_len} "
            f"limit={LEGACY_TX_MAX_BYTES} mint={short_addr(mint_str)} "
            f"buy_route={buy_route} sell_route={sell_route}"
        )
        raise RuntimeError(f"tx_too_large bytes={signed_bytes_len} > {LEGACY_TX_MAX_BYTES}")

    # --- decode-verify the encoded mins ---
    buy_program_id = PUMP_PROGRAM_ID if buy_route == "pump_bc" else PUMP_AMM_PROGRAM_ID
    buy_disc = DISC_PUMP_BUY_EXACT_SOL_IN if buy_route == "pump_bc" else DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN
    buy_data = _decode_ix_data_for_program(signed_b64, buy_program_id, buy_disc)
    if buy_data is None:
        raise RuntimeError(f"could not locate buy ix in serialized tx (route={buy_route})")
    encoded_min_tokens = _decode_verify_buy_min(buy_data, intended_min_tokens, buy_route)

    sell_program_id = PUMP_PROGRAM_ID if sell_route == "pump_bc" else PUMP_AMM_PROGRAM_ID
    sell_disc = DISC_PUMP_SELL if sell_route == "pump_bc" else DISC_PUMP_AMM_SELL
    sell_data = _decode_ix_data_for_program(signed_b64, sell_program_id, sell_disc)
    if sell_data is None:
        raise RuntimeError(f"could not locate sell ix in serialized tx (route={sell_route})")
    encoded_min_sol_lp = _decode_verify_sell_min(sell_data, intended_min_sol_lp, sell_route)

    # --- structured logs ---
    log(
        f"PGG2-V40-ATOMIC-BUILD mint={short_addr(mint_str)} "
        f"buy_route={buy_route} sell_route={sell_route} "
        f"buy_amount_sol={buy_amount_sol:.6f} "
        f"sell_tokens_raw={sell_tokens_raw} "
        f"min_sol_out={min_sol_out:.6f} "
        f"ix_count={len(ixs)} tx_size={signed_bytes_len}"
    )
    log(
        f"PGG2-V40-BUY-MIN-TOKEN-ENCODED mint={short_addr(mint_str)} "
        f"buy_route={buy_route} "
        f"encoded_min_tokens_raw={encoded_min_tokens} "
        f"intended={intended_min_tokens}"
    )
    log(
        f"PGG2-V40-SELL-MIN-SOL-ENCODED mint={short_addr(mint_str)} "
        f"sell_route={sell_route} "
        f"encoded_min_sol_lamports={encoded_min_sol_lp} "
        f"intended={intended_min_sol_lp}"
    )
    log(f"PGG2-V40-ATOMIC-TX-SIZE mint={short_addr(mint_str)} bytes={signed_bytes_len}")

    return AtomicArbBuild(
        mint=mint_str,
        buy_route=buy_route,
        sell_route=sell_route,
        buy_amount_sol=buy_amount_sol,
        spend_lamports=spend_lamports,
        sell_tokens_raw=sell_tokens_raw,
        min_sol_out_sol=min_sol_out,
        encoded_min_tokens_raw=encoded_min_tokens,
        encoded_min_sol_lamports=encoded_min_sol_lp,
        unsigned_b64=unsigned_b64,
        signed_b64=signed_b64,
        signed_b58=signed_b58,
        tx_size_bytes=signed_bytes_len,
        compute_unit_limit=int(compute_unit_limit),
        compute_unit_price_micro_lamports=int(compute_unit_price_micro_lamports),
        ix_count=len(ixs),
        ix_summary={
            "compute_budget": True,
            "create_base_ata": True,
            "create_wsol_ata": bool(needs_pumpswap),
            "fund_wsol": bool(buy_route.startswith("pumpswap")),
            "sync_native": bool(buy_route.startswith("pumpswap")),
            "buy_ix": True,
            "sell_ix": True,
            "close_wsol_ata": bool(needs_pumpswap),
            "close_base_ata": bool(include_base_close),
        },
    )


@dataclass
class AtomicArbSimResult:
    success: bool
    err: Any
    blocker: str
    compute_units_consumed: int
    tx_size_bytes: int
    wallet_delta_lamports: int
    wallet_delta_sol: float
    sim_logs_tail: str


def simulate_atomic_route_arb_tx(broker: DirectPumpQuoteBroker, build: AtomicArbBuild) -> AtomicArbSimResult:
    """Simulate the atomic arb tx using simulateTransaction.

    Returns success bool, compute units, wallet delta, blocker reason.
    Never calls send_signed / sendTransaction."""
    value = broker.simulate_signed_atomic(build.signed_b64)
    err = value.get("err")
    units = int(value.get("unitsConsumed") or 0)
    logs = value.get("logs") or []
    tail = " | ".join(str(x) for x in logs[-8:])
    pre = value.get("preBalances") or []
    post = value.get("postBalances") or []
    wallet_delta_lp = int((post[0] if post else 0) - (pre[0] if pre else 0)) if pre and post else 0
    wallet_delta_sol = wallet_delta_lp / LAMPORTS_PER_SOL

    if err:
        # Classify common blockers
        blocker = "sim_error"
        tail_lower = tail.lower()
        if "slippage" in tail_lower or "6042" in tail or "minamountout" in tail_lower:
            blocker = "sell_lower_than_buy"  # buy under-fills or sell can't meet min
        elif "overflow" in tail_lower or "6023" in tail:
            blocker = "overflow"
        elif "insufficient" in tail_lower or "0x1" in tail_lower:
            blocker = "insufficient_funds"
        elif "accountnotfound" in tail_lower or "could not find account" in tail_lower:
            blocker = "account_missing"
        elif "0x1771" in tail_lower or "6001" in tail:
            blocker = "slippage_check"
        log(
            f"PGG2-V40-ATOMIC-SIM-FAIL mint={short_addr(build.mint)} "
            f"buy_route={build.buy_route} sell_route={build.sell_route} "
            f"err={err} units={units} blocker={blocker} "
            f"wallet_delta_sol={wallet_delta_sol:+.6f} "
            f"logs_tail={tail[:400]}"
        )
        return AtomicArbSimResult(
            success=False,
            err=err,
            blocker=blocker,
            compute_units_consumed=units,
            tx_size_bytes=build.tx_size_bytes,
            wallet_delta_lamports=wallet_delta_lp,
            wallet_delta_sol=wallet_delta_sol,
            sim_logs_tail=tail[:400],
        )

    log(
        f"PGG2-V40-ATOMIC-SIM-PASS mint={short_addr(build.mint)} "
        f"buy_route={build.buy_route} sell_route={build.sell_route} "
        f"units={units} tx_size={build.tx_size_bytes} "
        f"wallet_delta_sol={wallet_delta_sol:+.6f}"
    )
    return AtomicArbSimResult(
        success=True,
        err=None,
        blocker="",
        compute_units_consumed=units,
        tx_size_bytes=build.tx_size_bytes,
        wallet_delta_lamports=wallet_delta_lp,
        wallet_delta_sol=wallet_delta_sol,
        sim_logs_tail=tail[:400],
    )


__all__ = [
    "AtomicArbBuild",
    "AtomicArbSimResult",
    "LEGACY_TX_MAX_BYTES",
    "build_atomic_route_arb_tx",
    "simulate_atomic_route_arb_tx",
]
