"""V51B Pump v2 account resolver.

Resolves the 27/26 mandatory accounts for buy_v2/sell_v2 from a mint and user.
Reads the on-chain bonding curve to extract creator for the creator-vault PDA.
"""
from __future__ import annotations
import base64
import json
from urllib import request as urlreq
from solders.pubkey import Pubkey
from pgg2_pump_v2_idl_constants import (
    PUMP_PROGRAM_ID, PUMP_FEE_PROGRAM_ID, SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID,
    NATIVE_MINT, NORMAL_FEE_RECIPIENTS_PK, BUYBACK_FEE_RECIPIENTS_PK,
)


def pda(program: Pubkey, *seeds: bytes) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), program)[0]


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey) -> Pubkey:
    return Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )[0]


class V2AccountsError(Exception):
    pass


def _rpc(url: str, method: str, params: list, timeout: float = 5.0) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urlreq.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_mint_owner(helius_url: str, mint: Pubkey) -> Pubkey:
    r = _rpc(helius_url, "getAccountInfo", [str(mint), {"encoding": "base64", "commitment": "processed"}])
    v = r.get("result", {}).get("value")
    if not v:
        raise V2AccountsError(f"mint_account_not_found mint={mint}")
    return Pubkey.from_string(v["owner"])


def fetch_bonding_curve_creator(helius_url: str, bonding_curve_pk: Pubkey) -> Pubkey:
    r = _rpc(helius_url, "getAccountInfo", [str(bonding_curve_pk), {"encoding": "base64", "commitment": "processed"}])
    v = r.get("result", {}).get("value")
    if not v:
        raise V2AccountsError(f"bonding_curve_not_found bc={bonding_curve_pk}")
    data = base64.b64decode(v["data"][0])
    if len(data) < 81:
        raise V2AccountsError(f"bonding_curve_data_too_short len={len(data)}")
    return Pubkey.from_bytes(data[49:81])


def resolve_v2_accounts_sol_paired(
    helius_url: str,
    mint: Pubkey,
    user: Pubkey,
    fee_recipient_pk: Pubkey | None = None,
    buyback_fee_recipient_pk: Pubkey | None = None,
) -> dict:
    """Returns dict with all v2 accounts. For SOL-paired coins only."""
    fee_recipient = fee_recipient_pk or NORMAL_FEE_RECIPIENTS_PK[0]
    buyback = buyback_fee_recipient_pk or BUYBACK_FEE_RECIPIENTS_PK[0]

    base_mint = mint
    quote_mint = NATIVE_MINT
    quote_token_program = TOKEN_PROGRAM_ID

    base_token_program = fetch_mint_owner(helius_url, base_mint)
    if base_token_program not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        raise V2AccountsError(f"unsupported_base_token_program owner={base_token_program}")

    bonding_curve = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(base_mint))
    creator = fetch_bonding_curve_creator(helius_url, bonding_curve)

    global_pda = pda(PUMP_PROGRAM_ID, b"global")
    event_authority = pda(PUMP_PROGRAM_ID, b"__event_authority")
    creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(creator))
    global_volume = pda(PUMP_PROGRAM_ID, b"global_volume_accumulator")
    user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
    sharing_config = pda(PUMP_FEE_PROGRAM_ID, b"sharing-config", bytes(base_mint))
    fee_config = pda(PUMP_FEE_PROGRAM_ID, b"fee_config", bytes(PUMP_PROGRAM_ID))

    aqf_recipient = ata(fee_recipient, quote_mint, quote_token_program)
    aqf_buyback = ata(buyback, quote_mint, quote_token_program)
    abbc = ata(bonding_curve, base_mint, base_token_program)
    aqbc = ata(bonding_curve, quote_mint, quote_token_program)
    abu = ata(user, base_mint, base_token_program)
    aqu = ata(user, quote_mint, quote_token_program)
    acv = ata(creator_vault, quote_mint, quote_token_program)
    auva = ata(user_volume, quote_mint, quote_token_program)

    return {
        "global": global_pda,
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "base_token_program": base_token_program,
        "quote_token_program": quote_token_program,
        "associated_token_program": ASSOCIATED_TOKEN_PROGRAM_ID,
        "fee_recipient": fee_recipient,
        "associated_quote_fee_recipient": aqf_recipient,
        "buyback_fee_recipient": buyback,
        "associated_quote_buyback_fee_recipient": aqf_buyback,
        "bonding_curve": bonding_curve,
        "associated_base_bonding_curve": abbc,
        "associated_quote_bonding_curve": aqbc,
        "user": user,
        "associated_base_user": abu,
        "associated_quote_user": aqu,
        "creator_vault": creator_vault,
        "associated_creator_vault": acv,
        "sharing_config": sharing_config,
        "global_volume_accumulator": global_volume,
        "user_volume_accumulator": user_volume,
        "associated_user_volume_accumulator": auva,
        "fee_config": fee_config,
        "fee_program": PUMP_FEE_PROGRAM_ID,
        "system_program": SYSTEM_PROGRAM_ID,
        "event_authority": event_authority,
        "program": PUMP_PROGRAM_ID,
        "_creator": creator,
    }
