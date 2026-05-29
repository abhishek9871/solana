from __future__ import annotations

import base64
import json
import math
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, CompiledInstruction, Instruction
from solders.message import MessageV0
from solders.null_signer import NullSigner
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from birth_first_sniper import SOL_MINT, env_bool, env_float, env_int, env_str, log, short_addr
from pgg2_live_raptor import LAMPORTS_PER_SOL, RaptorLiveBroker, b58encode


PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMP_AMM_PROGRAM_ID = Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
PUMP_FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
WSOL_MINT = Pubkey.from_string(SOL_MINT)

DISC_PUMP_BUY_EXACT_SOL_IN = bytes([56, 252, 116, 8, 158, 223, 205, 95])
DISC_PUMP_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN = bytes([198, 46, 21, 82, 180, 217, 232, 112])
DISC_PUMP_AMM_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])

DEFAULT_PUMP_FEE_RECIPIENTS = [
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
    "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
    "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
    "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",
    "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
    "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
]
DEFAULT_PUMPSWAP_FEE_RECIPIENTS = [
    "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
    "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
    "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
    "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
    "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
    "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
    "JCRGumoE9Qi5BBgULTgdgTLjSgkCMSbF62ZZfGs84JeU",
]

PUMP_SOCIAL_FEE_ACCOUNT_DISC = bytes([153, 166, 71, 144, 179, 189, 137, 251])
DEFAULT_PUMP_BUYBACK_FEE_RECIPIENT = "4Yz1hC4oeNNGgpsvURZHpomCiWSjLSHm6PF7DbRR4TwB"
DEFAULT_PUMP_BUYBACK_FEE_RECIPIENTS = [
    "4Yz1hC4oeNNGgpsvURZHpomCiWSjLSHm6PF7DbRR4TwB",
    "8xz8BsVxbKYJFuu7a5iojQLtCCoCtUa63wcXzVfiyyPo",
    "HXo88sWyiicozA1fyLUKr3gVmk7RF5sY2sWrXth3cUCj",
    "E8YQC4pynREnxP13nfeobZ8bZMhDsvEKonj6WpUQiSaG",
    "9vUZuJBp4Afv8J1qRDb4EASyhdxDg4M7rKUP691onx3d",
    "UPTGBeAbvRmNxXYMAQojsGxhbouzTc3VLPvSyjy1Yi7",
]
DEFAULT_PUMP_SOCIAL_FEE_PDAS = [
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
]
KNOWN_PUMP_SOCIAL_FEE_PDAS = set(DEFAULT_PUMP_SOCIAL_FEE_PDAS)
DEFAULT_PUMP_BUYBACK_FEE_PAIRS = [
    ("4Yz1hC4oeNNGgpsvURZHpomCiWSjLSHm6PF7DbRR4TwB", "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    ("4Yz1hC4oeNNGgpsvURZHpomCiWSjLSHm6PF7DbRR4TwB", "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL"),
    ("8xz8BsVxbKYJFuu7a5iojQLtCCoCtUa63wcXzVfiyyPo", "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"),
    ("HXo88sWyiicozA1fyLUKr3gVmk7RF5sY2sWrXth3cUCj", "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"),
    ("E8YQC4pynREnxP13nfeobZ8bZMhDsvEKonj6WpUQiSaG", "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
    ("9vUZuJBp4Afv8J1qRDb4EASyhdxDg4M7rKUP691onx3d", "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL"),
]


def ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b


def as_pubkey(value: str | Pubkey) -> Pubkey:
    return value if isinstance(value, Pubkey) else Pubkey.from_string(value)


def pda(program: Pubkey, *seeds: bytes) -> Pubkey:
    return Pubkey.find_program_address(list(seeds), program)[0]


def get_associated_token_address(owner: Pubkey, mint: Pubkey, token_program_id: Pubkey = TOKEN_PROGRAM_ID) -> Pubkey:
    return pda(ASSOCIATED_TOKEN_PROGRAM_ID, bytes(owner), bytes(token_program_id), bytes(mint))


def create_idempotent_associated_token_account(
    payer: Pubkey,
    owner: Pubkey,
    mint: Pubkey,
    token_program_id: Pubkey = TOKEN_PROGRAM_ID,
) -> Instruction:
    ata = get_associated_token_address(owner, mint, token_program_id)
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM_ID,
        b"\x01",
        [
            AccountMeta(payer, True, True),
            AccountMeta(ata, False, True),
            AccountMeta(owner, False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program_id, False, False),
        ],
    )


def sync_native(token_program_id: Pubkey, account: Pubkey) -> Instruction:
    return Instruction(token_program_id, b"\x11", [AccountMeta(account, False, True)])


def close_token_account(token_program_id: Pubkey, account: Pubkey, destination: Pubkey, owner: Pubkey) -> Instruction:
    return Instruction(
        token_program_id,
        b"\x09",
        [
            AccountMeta(account, False, True),
            AccountMeta(destination, False, True),
            AccountMeta(owner, True, False),
        ],
    )


def u64(value: int) -> bytes:
    return struct.pack("<Q", max(0, int(value)))


def u16(value: int) -> bytes:
    return struct.pack("<H", max(0, int(value)))


@dataclass(frozen=True)
class PumpGlobal:
    fee_recipient: Pubkey
    fee_recipients: list[Pubkey]
    reserved_fee_recipients: list[Pubkey]
    fee_bps: int
    creator_fee_bps: int


@dataclass(frozen=True)
class PumpBondingCurve:
    key: Pubkey
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool
    creator: Pubkey
    is_mayhem: bool
    cashback_enabled: bool


@dataclass(frozen=True)
class PumpSwapGlobal:
    lp_fee_bps: int
    protocol_fee_bps: int
    coin_creator_fee_bps: int
    protocol_fee_recipients: list[Pubkey]
    reserved_fee_recipients: list[Pubkey]


@dataclass(frozen=True)
class PumpSwapPool:
    key: Pubkey
    index: int
    creator: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    pool_base_token_account: Pubkey
    pool_quote_token_account: Pubkey
    coin_creator: Pubkey
    is_mayhem: bool


@dataclass(frozen=True)
class PumpBuybackPair:
    recipient: Pubkey
    social_fee_pda: Pubkey
    source: str


@dataclass(frozen=True)
class PumpBuyStaticPlan:
    mint: Pubkey
    spend_lamports: int
    token_program: Pubkey
    curve_key: Pubkey
    user: Pubkey
    user_ata: Pubkey
    associated_curve: Pubkey
    creator: Pubkey
    creator_vault: Pubkey
    user_volume: Pubkey
    fee_recipient: Pubkey
    base_metas: tuple[AccountMeta, ...]
    remaining_metas: tuple[AccountMeta, ...]
    ata_ix: Instruction
    header: Any
    account_keys: tuple[Pubkey, ...]
    address_table_lookups: tuple[Any, ...]
    compiled_instructions: tuple[CompiledInstruction, ...]
    pump_ix_index: int
    recent_blockhash: Hash
    pair_source: str
    pair_recipient: str
    pair_social: str
    created_ts_ms: int


class DirectPumpQuoteBroker(RaptorLiveBroker):
    """Direct Pump/PumpSwap broker.

    Quote mode builds and simulates local on-chain instructions without sending.
    Live mode is hard-gated and uses the same local builder, then relies on the
    inherited simulate-before-send/send path. No third-party swap transaction
    builder is used in either mode.
    """

    def __init__(self, config: Any):
        super().__init__(config)
        if self.mode == "live":
            confirm = env_str("PGG2_DIRECT_LIVE_CONFIRM")
            if confirm != "I_ACCEPT_DIRECT_PUMP_RISK":
                raise RuntimeError("direct Pump live mode blocked: set PGG2_DIRECT_LIVE_CONFIRM=I_ACCEPT_DIRECT_PUMP_RISK")
        else:
            self.quote_only = True
        self.quote_simulate = env_bool("PGG2_QUOTE_SIMULATE", True)
        self.quote_shadow_positions = env_bool("PGG2_QUOTE_SHADOW_POSITIONS", True)
        self.pump_global_key = pda(PUMP_PROGRAM_ID, b"global")
        self.pump_amm_global_config = pda(PUMP_AMM_PROGRAM_ID, b"global_config")
        self.pump_event_authority = pda(PUMP_PROGRAM_ID, b"__event_authority")
        self.pump_amm_event_authority = pda(PUMP_AMM_PROGRAM_ID, b"__event_authority")
        self.pump_global_volume_accumulator = pda(PUMP_PROGRAM_ID, b"global_volume_accumulator")
        self.pump_amm_global_volume_accumulator = pda(PUMP_AMM_PROGRAM_ID, b"global_volume_accumulator")
        self.pump_fee_config = pda(PUMP_FEE_PROGRAM_ID, b"fee_config", bytes(PUMP_PROGRAM_ID))
        self.pump_amm_fee_config = pda(PUMP_FEE_PROGRAM_ID, b"fee_config", bytes(PUMP_AMM_PROGRAM_ID))
        self._account_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._pump_social_fee_pdas: tuple[float, list[Pubkey]] = (0.0, [])
        self._pump_buyback_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._pump_buyback_selected: dict[str, PumpBuybackPair] = {}
        self._pump_buyback_observed_miss: dict[str, float] = {}
        # v30 — pair source tracking for the executable shadow lab.
        self._last_pair_source: dict[str, str] = {}
        self._last_pair_recipient: dict[str, str] = {}
        self._last_pair_social: dict[str, str] = {}
        self._pump_creator_vault_override: dict[str, str] = {}
        self._pump_fee_recipient_override: dict[str, str] = {}
        self._mint_decimals: dict[str, int] = {}
        self._latest_blockhash_cache: tuple[float, Optional[Hash]] = (0.0, None)
        self._pump_buy_static_plans: dict[str, PumpBuyStaticPlan] = {}
        # V47B in-flight quote tracker (restored 2026-05-19 V58 broker recovery)
        self._inflight_quotes: dict[str, int] = {}
        log(
            f"PGG2-DIRECT: mode={self.mode.upper()} "
            "route=pump_fun_bonding_curve+pumpswap "
            f"send={0 if self.quote_only else 1} third_party_swap=0"
        )

    def build_swap(self, from_mint: str, to_mint: str, amount: Any, slippage: float) -> dict[str, Any]:
        side = "buy" if str(from_mint) == SOL_MINT else "sell"
        mint = str(to_mint) if side == "buy" else str(from_mint)
        priority = self.current_swap_priority() if hasattr(self, "current_swap_priority") else "background"
        if env_bool("PGG2_GLOBAL_SWAP_BUDGET_ENABLED", False):
            entry_critical = priority in {"entry", "risk"}
            interval_ms = max(
                0,
                env_int(
                    "PGG2_GLOBAL_SWAP_ENTRY_MIN_INTERVAL_MS" if entry_critical else "PGG2_GLOBAL_SWAP_MIN_INTERVAL_MS",
                    0 if entry_critical else 450,
                ),
            )
            max_wait_ms = max(
                0,
                env_int(
                    "PGG2_GLOBAL_SWAP_ENTRY_MAX_WAIT_MS" if entry_critical else "PGG2_GLOBAL_SWAP_MAX_WAIT_MS",
                    250 if entry_critical else 5000,
                ),
            )
            wait_ms = 0
            with self._swap_budget_lock:
                now_ms = int(time.time() * 1000)
                next_allowed_ms = self._swap_entry_next_allowed_ms if entry_critical else self._swap_next_allowed_ms
                target_ms = max(next_allowed_ms, self._swap_backoff_until_ms)
                raw_wait_ms = max(0, target_ms - now_ms)
                if (
                    not entry_critical
                    and max_wait_ms > 0
                    and raw_wait_ms > max_wait_ms
                    and env_bool("PGG2_GLOBAL_SWAP_DROP_BACKGROUND_ON_WAIT", True)
                ):
                    log(
                        f"PGG2-GLOBAL-SWAP-BUDGET-DROP side={side} mint={short_addr(mint)} "
                        f"priority={priority} wait_ms={raw_wait_ms} max_wait_ms={max_wait_ms} "
                        f"reason=background_wait_exceeded"
                    )
                    raise RuntimeError("global_swap_budget_background_wait_exceeded")
                wait_ms = raw_wait_ms
                if max_wait_ms > 0:
                    wait_ms = min(wait_ms, max_wait_ms)
                if entry_critical:
                    self._swap_entry_next_allowed_ms = max(now_ms + wait_ms, now_ms) + interval_ms
                else:
                    self._swap_next_allowed_ms = max(now_ms + wait_ms, now_ms) + interval_ms
            if wait_ms > 0:
                log(
                    f"PGG2-GLOBAL-SWAP-BUDGET-WAIT side={side} mint={short_addr(mint)} "
                    f"priority={priority} wait_ms={wait_ms} interval_ms={interval_ms}"
                )
                time.sleep(wait_ms / 1000.0)
        try:
            if from_mint == SOL_MINT and to_mint != SOL_MINT:
                return self.build_buy(to_mint, float(amount), slippage)
            if to_mint == SOL_MINT and from_mint != SOL_MINT:
                return self.build_sell(from_mint, amount, slippage)
            raise RuntimeError(f"direct broker only supports SOL<->token swaps: {from_mint}->{to_mint}")
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if env_bool("PGG2_GLOBAL_SWAP_BUDGET_ENABLED", False) and (
                "429" in msg or "Too Many Requests" in msg
            ):
                backoff_ms = env_int("PGG2_GLOBAL_SWAP_429_BACKOFF_MS", 2500)
                with self._swap_budget_lock:
                    self._swap_backoff_until_ms = max(
                        self._swap_backoff_until_ms,
                        int(time.time() * 1000) + backoff_ms,
                    )
                log(
                    f"PGG2-GLOBAL-SWAP-429-BACKOFF side={side} mint={short_addr(mint)} "
                    f"backoff_ms={backoff_ms} error={msg}"
                )
            raise

    def account_info(self, pubkey: Pubkey, ttl_sec: float = 0.35) -> Optional[dict[str, Any]]:
        key = str(pubkey)
        cached = self._account_cache.get(key)
        if cached and time.time() - cached[0] <= ttl_sec:
            return cached[1]
        commitment = env_str("PGG2_DIRECT_ACCOUNT_COMMITMENT", "processed")
        out = self.rpc(
            "getAccountInfo",
            [key, {"encoding": "base64", "commitment": commitment}],
        )
        value = (out or {}).get("value")
        if value:
            self._account_cache[key] = (time.time(), value)
        return value

    @staticmethod
    def account_data(info: dict[str, Any]) -> bytes:
        data = info.get("data") or []
        if isinstance(data, list) and data:
            return base64.b64decode(data[0])
        if isinstance(data, str):
            return base64.b64decode(data)
        return b""

    def latest_blockhash(self, force: bool = False) -> Hash:
        commitment = env_str("PGG2_DIRECT_BLOCKHASH_COMMITMENT", "processed")
        ttl_ms = max(0, env_int("PGG2_DIRECT_BLOCKHASH_CACHE_MS", 0))
        now = time.time()
        cached_ts, cached_hash = self._latest_blockhash_cache
        if (
            not force
            and ttl_ms > 0
            and cached_hash is not None
            and (now - cached_ts) * 1000.0 <= ttl_ms
        ):
            return cached_hash
        start_ms = int(time.time() * 1000)
        out = self.rpc("getLatestBlockhash", [{"commitment": commitment}])
        blockhash = Hash.from_string(str(((out or {}).get("value") or {}).get("blockhash")))
        self._latest_blockhash_cache = (time.time(), blockhash)
        if env_bool("PGG2_DIRECT_BLOCKHASH_CACHE_LOG", False):
            log(
                f"PGG2-DIRECT-BLOCKHASH-CACHE refresh=1 "
                f"commitment={commitment} latency_ms={int(time.time() * 1000) - start_ms} "
                f"ttl_ms={ttl_ms}"
            )
        return blockhash

    def refresh_blockhash_cache(self) -> Hash:
        return self.latest_blockhash(force=True)

    def compile_tx(self, instructions: list[Instruction]) -> str:
        payer = as_pubkey(self.public_key)
        msg = MessageV0.try_compile(payer, instructions, [], self.latest_blockhash())
        signer = self.keypair if self.keypair else NullSigner(payer)
        tx = VersionedTransaction(msg, [signer])
        return base64.b64encode(bytes(tx)).decode("ascii")

    def compute_budget_ixs(self) -> list[Instruction]:
        units = env_int("PGG2_DIRECT_COMPUTE_UNIT_LIMIT", 220_000)
        priority_lamports = int(env_float("PGG2_DIRECT_PRIORITY_FEE_SOL", 0.000005) * LAMPORTS_PER_SOL)
        micro_lamports = env_int(
            "PGG2_DIRECT_COMPUTE_UNIT_PRICE_MICROLAMPORTS",
            int(priority_lamports * 1_000_000 / max(units, 1)),
        )
        return [set_compute_unit_limit(units), set_compute_unit_price(max(0, micro_lamports))]

    def mint_owner(self, mint: Pubkey) -> Pubkey:
        info = self.account_info(mint, ttl_sec=30.0)
        if not info:
            raise RuntimeError(f"mint account missing: {mint}")
        return Pubkey.from_string(str(info.get("owner")))

    def mint_decimals(self, mint: Pubkey) -> int:
        key = str(mint)
        if key in self._mint_decimals:
            return self._mint_decimals[key]
        data = self.account_data(self.account_info(mint, ttl_sec=30.0) or {})
        decimals = int(data[44]) if len(data) > 44 else env_int("PGG2_DIRECT_DEFAULT_TOKEN_DECIMALS", 6)
        self._mint_decimals[key] = decimals
        return decimals

    def raw_to_ui(self, mint: Pubkey, amount: int) -> float:
        return amount / float(10 ** self.mint_decimals(mint))

    def ui_to_raw(self, mint: Pubkey, amount: Any) -> int:
        if isinstance(amount, str):
            if amount == "auto":
                return self.token_balance_raw(mint)
            if amount.startswith("raw:"):
                return max(0, int(amount.split(":", 1)[1]))
            amount = float(amount)
        return max(0, int(float(amount) * (10 ** self.mint_decimals(mint))))

    def token_balance_raw(self, mint: Pubkey) -> int:
        token_program = self.mint_owner(mint)
        ata = get_associated_token_address(as_pubkey(self.public_key), mint, token_program)
        out = self.rpc("getTokenAccountBalance", [str(ata), {"commitment": "confirmed"}])
        return int(((out or {}).get("value") or {}).get("amount") or 0)

    def pump_global(self) -> PumpGlobal:
        info = self.account_info(self.pump_global_key, ttl_sec=5.0)
        if not info:
            raise RuntimeError("pump global account missing")
        data = self.account_data(info)
        fee_recipients = [Pubkey.from_string(x) for x in DEFAULT_PUMP_FEE_RECIPIENTS]
        reserved_fee_recipients: list[Pubkey] = []
        fee_recipient = fee_recipients[0]
        fee_bps = 100
        creator_fee_bps = 0
        if len(data) >= 386:
            fee_recipient = Pubkey.from_bytes(data[41:73])
            fee_bps = int.from_bytes(data[105:113], "little")
            creator_fee_bps = int.from_bytes(data[154:162], "little")
            fee_recipients = [Pubkey.from_bytes(data[162 + i * 32:194 + i * 32]) for i in range(7)]
            reserved_start = 162 + 7 * 32 + 32 + 32 + 1 + 32 + 32 + 1
            if len(data) >= reserved_start + 7 * 32:
                reserved_fee_recipients = [
                    Pubkey.from_bytes(data[reserved_start + i * 32:reserved_start + (i + 1) * 32])
                    for i in range(7)
                ]
        if fee_recipient not in fee_recipients:
            fee_recipients = [fee_recipient, *fee_recipients]
        return PumpGlobal(fee_recipient, fee_recipients, reserved_fee_recipients, fee_bps, creator_fee_bps)

    def bonding_curve(self, mint: Pubkey) -> PumpBondingCurve:
        curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
        # The bonding curve is the fast-moving price source. In live mode a
        # confirmed/cached read can be stale enough to trip Pump 6042 slippage
        # immediately after send, so default to a fresh processed account read.
        ttl_sec = env_float("PGG2_DIRECT_CURVE_ACCOUNT_TTL_SEC", 0.0)
        info = self.account_info(curve_key, ttl_sec=ttl_sec)
        if not info:
            raise RuntimeError(f"bonding curve missing: {short_addr(str(mint))}")
        data = self.account_data(info)
        if len(data) < 49:
            raise RuntimeError(f"bonding curve data too short: {short_addr(str(mint))} len={len(data)}")
        creator = Pubkey.default()
        if len(data) >= 81:
            creator = Pubkey.from_bytes(data[49:81])
        return PumpBondingCurve(
            key=curve_key,
            virtual_token_reserves=int.from_bytes(data[8:16], "little"),
            virtual_sol_reserves=int.from_bytes(data[16:24], "little"),
            real_token_reserves=int.from_bytes(data[24:32], "little"),
            real_sol_reserves=int.from_bytes(data[32:40], "little"),
            token_total_supply=int.from_bytes(data[40:48], "little"),
            complete=bool(data[48]),
            creator=creator,
            is_mayhem=bool(data[81]) if len(data) > 81 else False,
            cashback_enabled=bool(data[82]) if len(data) > 82 else False,
        )

    @staticmethod
    def pump_bonding_curve_v2(mint: Pubkey) -> Pubkey:
        return pda(PUMP_PROGRAM_ID, b"bonding-curve-v2", bytes(mint))

    def pump_fee_recipient(self, global_cfg: PumpGlobal, curve: PumpBondingCurve) -> Pubkey:
        pool = global_cfg.reserved_fee_recipients if curve.is_mayhem and global_cfg.reserved_fee_recipients else global_cfg.fee_recipients
        idx = int(time.time_ns()) % max(len(pool), 1)
        return pool[idx]

    def pump_social_fee_pdas(self) -> list[Pubkey]:
        forced = env_str("PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA", "").strip()
        if forced:
            return [as_pubkey(forced)]
        ttl_sec = env_float("PGG2_DIRECT_PUMP_SOCIAL_FEE_CACHE_SEC", 60.0)
        ts, cached = self._pump_social_fee_pdas
        if cached and time.time() - ts <= ttl_sec:
            return cached
        found: list[Pubkey] = []
        if env_bool("PGG2_DIRECT_DISCOVER_SOCIAL_FEE_PDAS", False):
            try:
                rows = self.rpc(
                    "getProgramAccounts",
                    [
                        str(PUMP_FEE_PROGRAM_ID),
                        {
                            "encoding": "base64",
                            "filters": [
                                {"dataSize": 208},
                                {"memcmp": {"offset": 0, "bytes": b58encode(PUMP_SOCIAL_FEE_ACCOUNT_DISC)}},
                            ],
                        },
                    ],
                )
                found = sorted(Pubkey.from_string(str(row["pubkey"])) for row in rows or [])
            except Exception as exc:
                log(f"PGG2-DIRECT-WARN social_fee_discovery_failed {type(exc).__name__}: {exc}")
        if not found:
            found = [Pubkey.from_string(x) for x in DEFAULT_PUMP_SOCIAL_FEE_PDAS]
        self._pump_social_fee_pdas = (time.time(), found)
        return found

    def pump_buyback_cache_file(self) -> Path:
        default_path = "/root/piggy/data/pgg2_pump_remaining_cache.json"
        return Path(env_str("PGG2_DIRECT_PUMP_REMAINING_CACHE", default_path))

    def pump_buyback_cache(self) -> dict[str, Any]:
        ttl_sec = env_float("PGG2_DIRECT_PUMP_REMAINING_CACHE_SEC", 2.0)
        ts, cached = self._pump_buyback_cache
        if cached and time.time() - ts <= ttl_sec:
            return cached
        path = self.pump_buyback_cache_file()
        data: dict[str, Any] = {}
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
        except Exception as exc:
            log(f"PGG2-DIRECT-WARN buyback_cache_read_failed {path} {type(exc).__name__}: {exc}")
        self._pump_buyback_cache = (time.time(), data)
        return data

    def cached_pump_buyback_pair(self, mint: Pubkey) -> Optional[PumpBuybackPair]:
        selected = self._pump_buyback_selected.get(str(mint))
        if selected:
            return selected
        rows = self.pump_buyback_cache()
        row = rows.get(str(mint))
        if not isinstance(row, dict):
            return None
        recipient = row.get("buyback_fee_recipient") or row.get("recipient")
        social = row.get("social_fee_pda") or row.get("social")
        if not recipient or not social:
            return None
        try:
            return PumpBuybackPair(as_pubkey(str(recipient)), as_pubkey(str(social)), "cache")
        except Exception:
            return None

    def remember_pump_buyback_pair(self, mint: Pubkey, pair: PumpBuybackPair) -> None:
        self._pump_buyback_selected[str(mint)] = pair
        if not env_bool("PGG2_DIRECT_PERSIST_SIM_SELECTED_BUYBACK", True):
            return
        path = self.pump_buyback_cache_file()
        try:
            rows = self.pump_buyback_cache()
            rows[str(mint)] = {
                "buyback_fee_recipient": str(pair.recipient),
                "social_fee_pda": str(pair.social_fee_pda),
                "source": pair.source,
                "ts": time.time(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(rows, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            tmp.replace(path)
            self._pump_buyback_cache = (time.time(), rows)
        except Exception as exc:
            log(f"PGG2-DIRECT-WARN buyback_cache_write_failed {path} {type(exc).__name__}: {exc}")

    def observed_pump_buyback_pair(self, mint: Pubkey) -> Optional[PumpBuybackPair]:
        if not env_bool("PGG2_DIRECT_OBSERVED_PAIR_FROM_RAW", True):
            return None
        key = str(mint)
        miss_ttl = env_float("PGG2_DIRECT_OBSERVED_PAIR_MISS_TTL_SEC", 3.0)
        miss_ts = self._pump_buyback_observed_miss.get(key, 0.0)
        if miss_ts and time.time() - miss_ts <= miss_ttl:
            return None
        raw_path = Path(env_str("PGG2_DIRECT_OBSERVED_PAIR_RAW_FILE", str(self.config.raw_events_file)))
        if not raw_path.is_file():
            self._pump_buyback_observed_miss[key] = time.time()
            return None
        max_sigs = env_int("PGG2_DIRECT_OBSERVED_PAIR_MAX_SIGS", 2)
        tail_bytes = env_int("PGG2_DIRECT_OBSERVED_PAIR_TAIL_BYTES", 4 * 1024 * 1024)
        sigs: list[str] = []
        seen: set[str] = set()
        try:
            with raw_path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
                blob = fh.read().decode("utf-8", "replace")
        except Exception as exc:
            log(f"PGG2-DIRECT-WARN observed_pair_raw_read_failed {short_addr(key)} {type(exc).__name__}: {exc}")
            self._pump_buyback_observed_miss[key] = time.time()
            return None
        for line in reversed(blob.splitlines()):
            if len(sigs) >= max_sigs:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("mint") or "") != key:
                continue
            sig = str(row.get("sig") or "")
            if not sig or sig in seen:
                continue
            if row.get("side") == "buy" and row.get("instruction_kind") in {"buy", "buy_exact_sol_in"}:
                sigs.append(sig)
                seen.add(sig)
        for sig in sigs:
            pair = self.pump_buyback_pair_from_signature(sig)
            if pair:
                observed = PumpBuybackPair(pair[0], pair[1], "observed_raw_rpc")
                self.remember_pump_buyback_pair(mint, observed)
                log(
                    f"PGG2-DIRECT-OBSERVED-PAIR {short_addr(key)} "
                    f"{short_addr(str(observed.recipient))}/{short_addr(str(observed.social_fee_pda))}"
                )
                return observed
        self._pump_buyback_observed_miss[key] = time.time()
        return None

    def probe_pump_v2_buy(self, mint: Any, amount_sol: float) -> dict[str, Any]:
        """v30 — Pump v2 instruction probe.

        Pump's public docs announce three new bonding-curve instructions:
            buy_v2, sell_v2, buy_exact_quote_in_v2
        which are documented to use mandatory accounts in the same order for
        all coins (no optional remaining accounts).

        This probe is intentionally a SAFE STUB. We do not have an
        authoritative IDL or canonical discriminator + account list for the v2
        instructions in this repository. Guessing the layout would risk
        corrupting live transactions, so we refuse to construct a v2 tx and
        record exactly what is missing.

        To enable a real build we need, from the official Pump source:
          1. Instruction discriminator bytes for buy_v2 / sell_v2 /
             buy_exact_quote_in_v2 (8-byte Anchor discriminators)
          2. Exact account list and order for each instruction
          3. Data layout (lamports / token amounts encoding)
          4. Whether the fee_config PDA + buyback recipient / social fee PDA
             accounts move into the mandatory list or are dropped
        """
        if not env_bool("PGG2_DIRECT_PUMP_V2_PROBE", False):
            return {"v2_probe_attempted": False}
        # mandatory hard-gate: never attempt v2 in real live mode
        if self.mode == "live":
            return {
                "v2_probe_attempted": True,
                "v2_probe_build_ok": False,
                "v2_probe_sim_ok": False,
                "v2_probe_error": "v2_probe_refused_live_mode",
            }
        missing_idl = (
            "v2_idl_unavailable: need authoritative discriminator + account list for "
            "buy_v2 / sell_v2 / buy_exact_quote_in_v2 before construction is safe"
        )
        log(
            f"PGG2-DIRECT-V2-PROBE blocked mint={short_addr(str(mint))} reason=v2_idl_unavailable"
        )
        return {
            "v2_probe_attempted": True,
            "v2_probe_build_ok": False,
            "v2_probe_sim_ok": False,
            "v2_probe_error": missing_idl,
        }

    def prewarm_pump_buyback_pair_from_sig(self, mint: Any, sig: str) -> bool:
        """v30 — discover and persist the Pump buyback/social pair from a
        candidate event signature. Lets the shadow lab and live broker quote
        fresh mints whose pair is not yet in the raw-event tape.

        Returns True if a pair was discovered (newly or already cached).
        Safe in any mode: only does a read-only RPC and updates cache; does
        not change strict-required behavior at build time.
        """
        if not sig:
            return False
        try:
            mint_pk = as_pubkey(str(mint))
        except Exception:
            return False
        # already cached or in-memory? skip the RPC
        if self.cached_pump_buyback_pair(mint_pk):
            return True
        pair = self.pump_buyback_pair_from_signature(sig)
        if not pair:
            return False
        observed = PumpBuybackPair(pair[0], pair[1], "current_sig")
        self.remember_pump_buyback_pair(mint_pk, observed)
        log(
            f"PGG2-DIRECT-PAIR-PREWARM mint={short_addr(str(mint))} sig={short_addr(sig)} "
            f"source=current_sig recipient={short_addr(str(observed.recipient))} "
            f"social={short_addr(str(observed.social_fee_pda))}"
        )
        return True

    def pump_buyback_pair_from_signature(self, sig: str) -> Optional[tuple[Pubkey, Pubkey]]:
        try:
            tx = self.rpc(
                "getTransaction",
                [
                    sig,
                    {
                        "encoding": "json",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
        except Exception as exc:
            log(f"PGG2-DIRECT-WARN observed_pair_fetch_failed {short_addr(sig)} {type(exc).__name__}: {exc}")
            return None
        if not tx:
            return None
        msg = ((tx.get("transaction") or {}).get("message") or {})
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        keys = list(msg.get("accountKeys") or [])
        loaded = meta.get("loadedAddresses") or {}
        keys.extend(loaded.get("writable") or [])
        keys.extend(loaded.get("readonly") or [])
        pump_program = str(PUMP_PROGRAM_ID)
        fee_program = str(PUMP_FEE_PROGRAM_ID)
        for ix in msg.get("instructions") or []:
            pid_idx = ix.get("programIdIndex")
            if not isinstance(pid_idx, int) or pid_idx >= len(keys) or keys[pid_idx] != pump_program:
                continue
            accounts = list(ix.get("accounts") or [])
            fee_positions = [
                pos for pos, account_idx in enumerate(accounts)
                if isinstance(account_idx, int) and account_idx < len(keys) and keys[account_idx] == fee_program
            ]
            if not fee_positions:
                continue
            extras = [
                keys[account_idx]
                for account_idx in accounts[fee_positions[-1] + 1:]
                if isinstance(account_idx, int) and account_idx < len(keys)
            ]
            # Only accept an observed pair when the social-fee PDA is one of
            # Pump's known fee PDAs. Some transactions append unrelated accounts
            # such as jito111... after the fee program; using those as the
            # social-fee PDA causes live Pump 6057 failures.
            social_positions = [
                idx for idx, key in enumerate(extras)
                if str(key) in KNOWN_PUMP_SOCIAL_FEE_PDAS
            ]
            if not social_positions:
                continue
            social_idx = social_positions[0]
            recipient_candidates = [
                as_pubkey(str(key))
                for key in extras[:social_idx]
                if str(key) not in KNOWN_PUMP_SOCIAL_FEE_PDAS
            ]
            if recipient_candidates:
                return recipient_candidates[0], as_pubkey(str(extras[social_idx]))
        return None

    def forced_pump_buyback_pair(self) -> Optional[PumpBuybackPair]:
        forced_recipient = env_str("PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT", "").strip()
        forced_social = env_str("PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA", "").strip()
        if not (forced_recipient and forced_social):
            return None
        return PumpBuybackPair(as_pubkey(forced_recipient), as_pubkey(forced_social), "env")

    def pump_buyback_remaining_metas(self, mint: Optional[Pubkey] = None) -> list[AccountMeta]:
        if not env_bool("PGG2_DIRECT_INCLUDE_BUYBACK_REMAINING", True):
            return []
        forced = self.forced_pump_buyback_pair()
        if forced:
            return self.pump_buyback_pair_metas(forced.recipient, forced.social_fee_pda)
        if mint:
            cached = self.cached_pump_buyback_pair(mint)
            if cached:
                return self.pump_buyback_pair_metas(cached.recipient, cached.social_fee_pda)
            observed = self.observed_pump_buyback_pair(mint)
            if observed:
                return self.pump_buyback_pair_metas(observed.recipient, observed.social_fee_pda)
        # The public Pump IDL still lists 16 buy_exact_sol_in accounts, but the
        # deployed May 2026 program rejects simulations without these remaining
        # buyback/social-fee accounts. This pair is taken from current mainnet
        # Pump transactions, then the social-fee PDA list is discovered by RPC.
        buyback_recipient = as_pubkey(
            env_str("PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT", DEFAULT_PUMP_BUYBACK_FEE_RECIPIENT)
        )
        social_fee_pdas = self.pump_social_fee_pdas()
        social_fee_pda = social_fee_pdas[int(time.time_ns()) % max(len(social_fee_pdas), 1)]
        return [
            AccountMeta(buyback_recipient, False, False),
            AccountMeta(social_fee_pda, False, True),
        ]

    def pump_buy_remaining_metas(self, mint: Pubkey) -> list[AccountMeta]:
        # v30 — record which source we end up using, for the shadow lab.
        mint_key = str(mint)
        forced = self.forced_pump_buyback_pair()
        cached = self.cached_pump_buyback_pair(mint) if not forced else None
        observed = self.observed_pump_buyback_pair(mint) if not (forced or cached) else None
        pair = forced or cached or observed
        if not pair and env_bool("PGG2_DIRECT_REQUIRE_OBSERVED_BUYBACK_PAIR", self.mode == "live"):
            self._last_pair_source[mint_key] = "none"
            raise RuntimeError(f"no confirmed pump buyback/social pair observed: {short_addr(str(mint))}")
        if pair:
            if forced:
                source = "forced"
            elif cached:
                # cache stores source ("current_sig", "sim_selected", "observed_raw_rpc", "env")
                source = cached.source or "cache"
            else:
                source = observed.source or "observed_raw"
            self._last_pair_source[mint_key] = source
            self._last_pair_recipient[mint_key] = str(pair.recipient)
            self._last_pair_social[mint_key] = str(pair.social_fee_pda)
            social_fee_pda = AccountMeta(pair.social_fee_pda, False, True)
        else:
            buyback = self.pump_buyback_remaining_metas(mint)
            social_fee_pda = buyback[-1] if buyback else None
            self._last_pair_source[mint_key] = "default"
            if social_fee_pda:
                self._last_pair_social[mint_key] = str(social_fee_pda.pubkey)
        metas = [AccountMeta(self.pump_bonding_curve_v2(mint), False, False)]
        if social_fee_pda:
            metas.append(social_fee_pda)
        return metas

    def last_pair_info(self, mint: Any) -> dict[str, str]:
        """Return the most recent pair source/recipient/social PDA used for
        this mint by the buy builder. Empty dict if not seen.
        """
        key = str(mint)
        out: dict[str, str] = {}
        src = self._last_pair_source.get(key)
        if src is not None:
            out["pair_source"] = src
        rec = self._last_pair_recipient.get(key)
        if rec:
            out["pair_recipient"] = rec
        soc = self._last_pair_social.get(key)
        if soc:
            out["pair_social_fee_pda"] = soc
        return out

    def _pump_buy_static_plan_key(self, mint: Pubkey, spend_lamports: int) -> str:
        return f"{str(mint)}:{int(spend_lamports)}"

    def prepare_pump_buy_static_plan(
        self,
        mint_any: Any,
        amount_sol: Optional[float] = None,
        *,
        creator: str = "",
    ) -> PumpBuyStaticPlan:
        """Precompile the static Pump buy transaction shape for a mint.

        V165 keeps the hot callback out of MessageV0.try_compile and V75 tip
        injection. The READY/prewarm path compiles a placeholder buy with the
        same account list and SWQOS tip; the live send path only patches the
        Pump buy instruction data with the fresh min-token guard and signs.
        """
        t0 = time.time()
        mint = as_pubkey(mint_any)
        if amount_sol is None:
            amount_sol = env_float(
                "V116C_TRADE_SIZE_SOL",
                env_float("V102_SCOUT_SIZE_SOL", env_float("PGG2_LIVE_MIN_TRADE_SOL", 0.0015)),
            )
        spend_lamports = max(1, int(float(amount_sol) * LAMPORTS_PER_SOL))
        key = self._pump_buy_static_plan_key(mint, spend_lamports)
        cached = self._pump_buy_static_plans.get(key)
        if cached:
            return cached

        t_accounts0 = time.time()
        token_program = self.mint_owner(mint)
        user = as_pubkey(self.public_key)
        curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
        associated_curve = get_associated_token_address(curve_key, mint, token_program)
        user_ata = get_associated_token_address(user, mint, token_program)
        creator_pk = as_pubkey(creator) if creator else Pubkey.default()
        creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(creator_pk))
        user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
        global_cfg = self.pump_global()
        placeholder_curve = PumpBondingCurve(
            key=curve_key,
            virtual_token_reserves=1,
            virtual_sol_reserves=1,
            real_token_reserves=1,
            real_sol_reserves=0,
            token_total_supply=0,
            complete=False,
            creator=creator_pk,
            is_mayhem=False,
            cashback_enabled=False,
        )
        fee_recipient = self.pump_fee_recipient(global_cfg, placeholder_curve)
        creator_vault_override = self._pump_creator_vault_override.get(str(mint), "")
        if creator_vault_override:
            creator_vault = as_pubkey(creator_vault_override)
        fee_recipient_override = self._pump_fee_recipient_override.get(str(mint), "")
        if fee_recipient_override:
            fee_recipient = as_pubkey(fee_recipient_override)
        remaining_metas = tuple(self.pump_buy_remaining_metas(mint))
        pair_source = self._last_pair_source.get(str(mint), "unknown")
        pair_recipient = self._last_pair_recipient.get(str(mint), "")
        pair_social = self._last_pair_social.get(str(mint), "")
        base_metas = (
            AccountMeta(self.pump_global_key, False, False),
            AccountMeta(fee_recipient, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(curve_key, False, True),
            AccountMeta(associated_curve, False, True),
            AccountMeta(user_ata, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
            AccountMeta(creator_vault, False, True),
            AccountMeta(self.pump_event_authority, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
            AccountMeta(self.pump_global_volume_accumulator, False, False),
            AccountMeta(user_volume, False, True),
            AccountMeta(self.pump_fee_config, False, False),
            AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
        )
        ata_ix = create_idempotent_associated_token_account(user, user, mint, token_program)
        account_ms = int((time.time() - t_accounts0) * 1000)

        t_compile0 = time.time()
        track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
        placeholder_data = DISC_PUMP_BUY_EXACT_SOL_IN + u64(spend_lamports) + u64(0) + track_volume
        placeholder_b64 = self.compile_tx([
            *self.compute_budget_ixs(),
            ata_ix,
            Instruction(PUMP_PROGRAM_ID, placeholder_data, [*base_metas, *remaining_metas]),
        ])
        tx = VersionedTransaction.from_bytes(base64.b64decode(placeholder_b64))
        msg = tx.message
        pump_ix_index = -1
        for idx, cix in enumerate(msg.instructions):
            try:
                program_key = msg.account_keys[int(cix.program_id_index)]
            except Exception:
                continue
            if program_key == PUMP_PROGRAM_ID and bytes(cix.data).startswith(DISC_PUMP_BUY_EXACT_SOL_IN):
                pump_ix_index = idx
                break
        if pump_ix_index < 0:
            raise RuntimeError("v165_precompiled_pump_ix_not_found")
        compile_ms = int((time.time() - t_compile0) * 1000)

        plan = PumpBuyStaticPlan(
            mint=mint,
            spend_lamports=spend_lamports,
            token_program=token_program,
            curve_key=curve_key,
            user=user,
            user_ata=user_ata,
            associated_curve=associated_curve,
            creator=creator_pk,
            creator_vault=creator_vault,
            user_volume=user_volume,
            fee_recipient=fee_recipient,
            base_metas=base_metas,
            remaining_metas=remaining_metas,
            ata_ix=ata_ix,
            header=msg.header,
            account_keys=tuple(msg.account_keys),
            address_table_lookups=tuple(msg.address_table_lookups),
            compiled_instructions=tuple(msg.instructions),
            pump_ix_index=pump_ix_index,
            recent_blockhash=msg.recent_blockhash,
            pair_source=pair_source,
            pair_recipient=pair_recipient,
            pair_social=pair_social,
            created_ts_ms=int(time.time() * 1000),
        )
        self._pump_buy_static_plans[key] = plan
        log(
            f"PGG2-V165-PUMP-BUY-STATIC-PLAN-READY mint={short_addr(str(mint))} "
            f"amount_sol={float(amount_sol):.6f} pair_source={pair_source} "
            f"account_ms={account_ms} compile_ms={compile_ms} "
            f"total_ms={int((time.time() - t0) * 1000)} pump_ix_index={pump_ix_index}"
        )
        return plan

    def build_fast_signed_buy_with_min_tokens_from_curve_snapshot(
        self,
        mint_str: str,
        amount_sol: float,
        min_tokens_ui: float,
        *,
        virtual_token_reserves: int,
        virtual_sol_reserves: int,
        real_token_reserves: int,
        real_sol_reserves: int,
        token_total_supply: int = 0,
        complete: bool = False,
        creator: str = "",
        is_mayhem: bool = False,
        cashback_enabled: bool = False,
        snapshot_ts_ms: int = 0,
    ) -> dict[str, Any]:
        if env_bool("PGG2_V165_REQUIRE_PRECOMPILED_BUY_PLAN", True):
            mint = as_pubkey(mint_str)
            spend_lamports = max(1, int(float(amount_sol) * LAMPORTS_PER_SOL))
            key = self._pump_buy_static_plan_key(mint, spend_lamports)
            plan = self._pump_buy_static_plans.get(key)
            if plan is None:
                raise RuntimeError("v165_precompiled_buy_plan_missing")
        else:
            plan = self.prepare_pump_buy_static_plan(mint_str, amount_sol, creator=creator)

        _qstart_ms = int(time.time() * 1000)
        t0 = time.time()
        mint = plan.mint
        creator_pk = as_pubkey(creator) if creator else plan.creator
        creator_vault_override = self._pump_creator_vault_override.get(str(mint), "")
        has_creator_vault_override = bool(
            creator_vault_override and str(plan.creator_vault) == creator_vault_override
        )
        if env_bool("PGG2_V165_REJECT_DEFAULT_CREATOR_PLAN", True):
            default_creator = Pubkey.default()
            if (plan.creator == default_creator or creator_pk == default_creator) and not has_creator_vault_override:
                raise RuntimeError("v165_precompiled_creator_missing")
            if creator and plan.creator != creator_pk:
                raise RuntimeError(
                    f"v165_precompiled_creator_mismatch plan={short_addr(str(plan.creator))} "
                    f"creator={short_addr(str(creator_pk))}"
                )
        curve = PumpBondingCurve(
            key=plan.curve_key,
            virtual_token_reserves=int(virtual_token_reserves),
            virtual_sol_reserves=int(virtual_sol_reserves),
            real_token_reserves=int(real_token_reserves),
            real_sol_reserves=int(real_sol_reserves),
            token_total_supply=int(token_total_supply),
            complete=bool(complete),
            creator=creator_pk,
            is_mayhem=bool(is_mayhem),
            cashback_enabled=bool(cashback_enabled),
        )
        if curve.complete:
            raise RuntimeError("v165_fast_snapshot_only_supports_pump_bc")
        global_cfg = self.pump_global()
        token_out, fee_lamports = self.quote_pump_buy_tokens(plan.spend_lamports, curve, global_cfg)
        if env_bool("PGG2_V165_FAST_ASSUME_PUMP_DECIMALS", True):
            scale = 10 ** env_int("V102_PUMP_TOKEN_DECIMALS", 6)
            min_tokens_out = max(0, int(float(min_tokens_ui) * scale))
            out_ui = token_out / float(scale)
            min_ui = min_tokens_out / float(scale)
        else:
            min_tokens_out = max(0, self.ui_to_raw(mint, min_tokens_ui))
            out_ui = self.raw_to_ui(mint, token_out)
            min_ui = self.raw_to_ui(mint, min_tokens_out)

        patch_data = (
            DISC_PUMP_BUY_EXACT_SOL_IN
            + u64(plan.spend_lamports)
            + u64(min_tokens_out)
            + (b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00")
        )
        old_ix = plan.compiled_instructions[plan.pump_ix_index]
        insts = list(plan.compiled_instructions)
        insts[plan.pump_ix_index] = CompiledInstruction(
            old_ix.program_id_index,
            patch_data,
            old_ix.accounts,
        )
        t_msg0 = time.time()
        plan_age_ms_before_build = max(0, int(time.time() * 1000) - int(plan.created_ts_ms or 0))
        plan_blockhash_max_age_ms = max(
            0,
            env_int("PGG2_V165_FAST_PLAN_BLOCKHASH_MAX_AGE_MS", 30000),
        )
        if plan.recent_blockhash and plan_age_ms_before_build <= plan_blockhash_max_age_ms:
            recent_blockhash = plan.recent_blockhash
            blockhash_source = "static_plan"
        else:
            recent_blockhash = self.latest_blockhash()
            blockhash_source = "cache_or_rpc"
        msg = MessageV0(
            plan.header,
            list(plan.account_keys),
            recent_blockhash,
            insts,
            list(plan.address_table_lookups),
        )
        signer = self.keypair if self.keypair else NullSigner(plan.user)
        tx = VersionedTransaction(msg, [signer])
        raw = bytes(tx)
        verify_ok = True
        verify_detail = "unavailable"
        try:
            verify_results = tx.verify_with_results()
            verify_ok = all(bool(v) for v in verify_results)
            verify_detail = ",".join("1" if bool(v) else "0" for v in verify_results)
        except Exception as exc:
            verify_ok = False
            verify_detail = f"{type(exc).__name__}:{str(exc)[:80]}"
        encoded_spend_lamports = -1
        encoded_min_tokens_raw = -1
        encoded_pump_ix_count = 0
        tip_ix_count = 0
        tip_lamports = 0
        tip_to = ""
        try:
            verify_tx = VersionedTransaction.from_bytes(raw)
            for ix in verify_tx.message.instructions:
                program_id = verify_tx.message.account_keys[ix.program_id_index]
                data = bytes(ix.data)
                if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_BUY_EXACT_SOL_IN):
                    encoded_pump_ix_count += 1
                    if len(data) >= len(DISC_PUMP_BUY_EXACT_SOL_IN) + 16:
                        encoded_spend_lamports = struct.unpack(
                            "<Q",
                            data[
                                len(DISC_PUMP_BUY_EXACT_SOL_IN):
                                len(DISC_PUMP_BUY_EXACT_SOL_IN) + 8
                            ],
                        )[0]
                        encoded_min_tokens_raw = struct.unpack(
                            "<Q",
                            data[
                                len(DISC_PUMP_BUY_EXACT_SOL_IN) + 8:
                                len(DISC_PUMP_BUY_EXACT_SOL_IN) + 16
                            ],
                        )[0]
                    break
            for ix in verify_tx.message.instructions:
                program_id = verify_tx.message.account_keys[ix.program_id_index]
                data = bytes(ix.data)
                if (
                    program_id == SYSTEM_PROGRAM_ID
                    and len(data) >= 12
                    and int.from_bytes(data[:4], "little") == 2
                    and len(ix.accounts) >= 2
                ):
                    lamports = int.from_bytes(data[4:12], "little")
                    to_pk = verify_tx.message.account_keys[int(ix.accounts[1])]
                    if lamports >= env_int("PGG2_V75_TIP_LAMPORTS", 5000):
                        tip_ix_count += 1
                        tip_lamports = lamports
                        tip_to = str(to_pk)
        except Exception as exc:
            verify_ok = False
            verify_detail = f"{verify_detail}|decode:{type(exc).__name__}:{str(exc)[:80]}"
        guard_match = (
            encoded_pump_ix_count == 1
            and encoded_spend_lamports == int(plan.spend_lamports)
            and encoded_min_tokens_raw == int(min_tokens_out)
        )
        tip_ok = tip_ix_count >= 1 and tip_lamports >= env_int("PGG2_V75_TIP_LAMPORTS", 5000)
        if env_bool("PGG2_V165_REQUIRE_FAST_TX_VERIFY", True) and (
            not verify_ok
            or not guard_match
            or (
                env_bool("PGG2_V165_REQUIRE_FAST_TIP_VERIFY", True)
                and not tip_ok
            )
        ):
            log(
                f"PGG2-V165-FAST-TX-VERIFY mint={short_addr(mint_str)} "
                f"verify_ok={int(verify_ok)} guard_match={int(guard_match)} "
                f"tip_ok={int(tip_ok)} tip_ix_count={tip_ix_count} "
                f"tip_lamports={tip_lamports} tip_to={tip_to or '-'} "
                f"verify_detail={verify_detail!r} pump_ix_count={encoded_pump_ix_count} "
                f"encoded_spend_lamports={encoded_spend_lamports} "
                f"expected_spend_lamports={int(plan.spend_lamports)} "
                f"encoded_min_tokens_raw={encoded_min_tokens_raw} "
                f"expected_min_tokens_raw={int(min_tokens_out)} tx_bytes={len(raw)}"
            )
            raise RuntimeError("v165_fast_tx_verify_failed")
        signed_b64 = base64.b64encode(raw).decode("ascii")
        sign_compile_ms = int((time.time() - t_msg0) * 1000)
        _qend_ms = int(time.time() * 1000)
        self._last_pair_source[str(mint)] = plan.pair_source
        if plan.pair_recipient:
            self._last_pair_recipient[str(mint)] = plan.pair_recipient
        if plan.pair_social:
            self._last_pair_social[str(mint)] = plan.pair_social
        fee_sol = fee_lamports / LAMPORTS_PER_SOL
        log(
            f"PGG2-V165-FAST-SNAPSHOT-BUY-BUILD mint={short_addr(mint_str)} "
            f"plan_age_ms={max(0, _qend_ms - int(plan.created_ts_ms or 0))} "
            f"patch_sign_ms={sign_compile_ms} total_ms={int((time.time() - t0) * 1000)} "
            f"out={out_ui:.6f} min={min_ui:.6f} pair_source={plan.pair_source} "
            f"blockhash_source={blockhash_source} "
            f"snapshot_age_ms={max(0, _qend_ms - int(snapshot_ts_ms or 0)) if snapshot_ts_ms else -1}"
        )
        log(
            f"PGG2-V165-FAST-TX-VERIFY mint={short_addr(mint_str)} "
            f"verify_ok={int(verify_ok)} guard_match={int(guard_match)} "
            f"tip_ok={int(tip_ok)} tip_ix_count={tip_ix_count} "
            f"tip_lamports={tip_lamports} tip_to={tip_to or '-'} "
            f"verify_detail={verify_detail!r} pump_ix_count={encoded_pump_ix_count} "
            f"encoded_spend_lamports={encoded_spend_lamports} "
            f"encoded_min_tokens_raw={encoded_min_tokens_raw} tx_bytes={len(raw)}"
        )
        try:
            self.record_quote_latency(
                side="buy",
                mint=mint_str,
                route="pump_bc",
                source="v165_fast_precompiled_snapshot",
                start_ms=_qstart_ms,
                end_ms=_qend_ms,
                success=True,
                pair_source=plan.pair_source,
                was_simulation_needed=False,
            )
        except Exception:
            pass
        return {
            "txn": signed_b64,
            "signed_txn": signed_b64,
            "sig_preview": str(tx.signatures[0]) if tx.signatures else b58encode(raw),
            "route": "pump_bc",
            "quote_network_latency_ms": max(0, _qend_ms - _qstart_ms),
            "quote_returned_ts_ms": _qend_ms,
            "quote_source": "v165_fast_precompiled_snapshot",
            "snapshot_ts_ms": int(snapshot_ts_ms or 0),
            "v165_fast_signed": True,
            "rate": {
                "amountOut": out_ui,
                "minAmountOut": min_ui,
                "priceImpact": plan.spend_lamports / max(curve.virtual_sol_reserves, 1),
                "fee": fee_sol,
                "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
            },
        }

    def pump_sell_remaining_metas(self, mint: Pubkey, curve: PumpBondingCurve, user: Pubkey) -> list[AccountMeta]:
        buyback = self.pump_buyback_remaining_metas(mint)
        social_fee_pda = buyback[-1] if buyback else None
        metas: list[AccountMeta] = []
        user_volume_mode = env_str("PGG2_DIRECT_SELL_USER_VOLUME_MODE", "auto").strip().lower()
        include_user_volume = user_volume_mode == "always" or (
            user_volume_mode == "auto" and curve.cashback_enabled
        )
        if include_user_volume:
            metas.append(AccountMeta(pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user)), False, True))
        metas.append(AccountMeta(self.pump_bonding_curve_v2(mint), False, False))
        if social_fee_pda:
            metas.append(social_fee_pda)
        return metas

    @staticmethod
    def pump_buyback_pair_metas(recipient: Pubkey, social_fee_pda: Pubkey) -> list[AccountMeta]:
        return [
            AccountMeta(recipient, False, False),
            AccountMeta(social_fee_pda, False, True),
        ]

    def pump_buyback_candidate_pairs(self, mint: Optional[Pubkey] = None) -> list[PumpBuybackPair]:
        forced = self.forced_pump_buyback_pair()
        if forced:
            return [forced]
        pairs: list[PumpBuybackPair] = []
        cached = self.cached_pump_buyback_pair(mint) if mint else None
        if cached:
            pairs.append(cached)
        observed = self.observed_pump_buyback_pair(mint) if mint and not cached else None
        if observed and not any(pair.recipient == observed.recipient and pair.social_fee_pda == observed.social_fee_pda for pair in pairs):
            pairs.append(observed)
        pairs.extend(
            PumpBuybackPair(as_pubkey(recipient), as_pubkey(social), "default")
            for recipient, social in DEFAULT_PUMP_BUYBACK_FEE_PAIRS
        )
        recipients = [as_pubkey(x) for x in DEFAULT_PUMP_BUYBACK_FEE_RECIPIENTS]
        socials = self.pump_social_fee_pdas()
        for recipient in recipients:
            for social in socials:
                if not any(pair.recipient == recipient and pair.social_fee_pda == social for pair in pairs):
                    pairs.append(PumpBuybackPair(recipient, social, "grid"))
        # v30 — cap the sim-select candidate list so shadow-lab quote latency
        # stays manageable. Cache hit / prewarm / observed pairs are kept at
        # the head, so the cap rarely affects accuracy.
        cap = env_int("PGG2_DIRECT_SIM_SELECT_MAX_CANDIDATES", 0)
        if cap > 0:
            return pairs[:cap]
        return pairs

    def quote_pump_buy_tokens(self, spend_lamports: int, curve: PumpBondingCurve, global_cfg: PumpGlobal) -> tuple[int, int]:
        total_fee_bps = max(0, global_cfg.fee_bps + global_cfg.creator_fee_bps)
        net_sol = spend_lamports * 10_000 // (10_000 + total_fee_bps)
        fees = ceil_div(net_sol * global_cfg.fee_bps, 10_000) + ceil_div(net_sol * global_cfg.creator_fee_bps, 10_000)
        if net_sol + fees > spend_lamports:
            net_sol -= net_sol + fees - spend_lamports
        net_for_curve = max(0, net_sol - 1)
        tokens = net_for_curve * curve.virtual_token_reserves // max(curve.virtual_sol_reserves + net_for_curve, 1)
        return max(0, min(tokens, curve.real_token_reserves)), max(0, fees)

    def quote_pump_sell_sol(self, token_amount: int, curve: PumpBondingCurve, global_cfg: PumpGlobal) -> tuple[int, int]:
        gross_sol = token_amount * curve.virtual_sol_reserves // max(curve.virtual_token_reserves + token_amount, 1)
        protocol_fee = ceil_div(gross_sol * global_cfg.fee_bps, 10_000)
        creator_fee = ceil_div(gross_sol * global_cfg.creator_fee_bps, 10_000)
        fees = protocol_fee + creator_fee
        return max(0, gross_sol - fees), max(0, fees)

    def build_buy(self, mint_str: str, amount_sol: float, slippage: float) -> dict[str, Any]:
        # v31 — quote latency telemetry. Increment in-flight + capture start.
        self._inflight_quotes[mint_str] = self._inflight_quotes.get(mint_str, 0) + 1
        _qstart_ms = int(time.time() * 1000)
        _qsim_needed = bool(env_bool("PGG2_DIRECT_SELECT_BUYBACK_BY_SIM", False) and self.quote_simulate)
        try:
            return self._build_buy_impl(mint_str, amount_sol, slippage, _qstart_ms, _qsim_needed)
        except Exception as exc:
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="buy",
                    mint=mint_str,
                    route="pump_bc",
                    source="error",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=False,
                    error_class=type(exc).__name__,
                    was_simulation_needed=_qsim_needed,
                )
            except Exception:
                pass
            raise
        finally:
            self._inflight_quotes[mint_str] = max(0, self._inflight_quotes.get(mint_str, 1) - 1)

    def simulate_post_buy_pump_curve(self, curve: PumpBondingCurve, tokens_received: int) -> PumpBondingCurve:
        """Shift the bonding-curve state to reflect a buy that yielded `tokens_received`.

        Pump uses a CPMM with virtual reserves and k = vSOL × vTOK invariant:
          - new vTOK = vTOK - tokens_received
          - new vSOL = k / new vTOK
        Real reserves and other fields are preserved for downstream callers.
        """
        if tokens_received <= 0:
            return curve
        k = int(curve.virtual_sol_reserves) * int(curve.virtual_token_reserves)
        vtok_new = max(1, int(curve.virtual_token_reserves) - int(tokens_received))
        vsol_new = max(1, k // vtok_new)
        return PumpBondingCurve(
            key=curve.key,
            virtual_token_reserves=vtok_new,
            virtual_sol_reserves=vsol_new,
            real_token_reserves=max(0, int(curve.real_token_reserves) - int(tokens_received)),
            real_sol_reserves=int(curve.real_sol_reserves),  # not used in CPMM math here
            token_total_supply=int(curve.token_total_supply),
            complete=bool(curve.complete),
            creator=curve.creator,
            is_mayhem=bool(getattr(curve, "is_mayhem", False)),
            cashback_enabled=bool(getattr(curve, "cashback_enabled", False)),
        )

    def compute_min_buy_tokens_for_principal_recovery(
        self,
        mint_str: str,
        amount_sol: float,
        target_recovery_sol: float,
        sell_fee_buffer_sol: float = 0.0,
    ) -> dict[str, Any]:
        """PR2-Phase 2: smallest T_min where, if our buy lands with exactly T_min
        tokens, selling some recovery tranche of T_min on the post-our-buy curve
        yields target_recovery_sol + sell_fee_buffer_sol.

        Binary search over T_min. For each trial:
          1. Simulate post-our-buy curve state (assumes our buy got exactly T_min).
          2. On that post-buy curve, find smallest recovery_tokens (subset of T_min)
             whose sell yields target.
          3. If found AND recovery_fraction <= PGG2_V39_RECOVERY_MAX_FRACTION, feasible.

        If even T_expected (best-case fill) is infeasible, returns
        feasible=false and final_buy_min_tokens_raw = T_expected (so the caller
        can decide to block the entry).
        """
        mint = as_pubkey(mint_str)
        curve = self.bonding_curve(mint)
        global_cfg = self.pump_global()
        spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
        expected_tokens_raw, _expected_fee = self.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
        target_lamports = max(1, int((target_recovery_sol + sell_fee_buffer_sol) * LAMPORTS_PER_SOL))
        max_fraction = env_float("PGG2_V39_RECOVERY_MAX_FRACTION", 0.95)

        def feasible_at(t_min: int) -> tuple[bool, int, int]:
            """Return (feasible, recovery_tokens, expected_recovery_lamports) for a trial T_min."""
            if t_min <= 0 or t_min > expected_tokens_raw:
                return (False, 0, 0)
            post_buy_curve = self.simulate_post_buy_pump_curve(curve, t_min)
            # On post-buy curve, find smallest recovery_tokens whose sell yields target
            max_sell_lp, _ = self.quote_pump_sell_sol(t_min, post_buy_curve, global_cfg)
            if max_sell_lp < target_lamports:
                return (False, t_min, max_sell_lp)
            lo, hi, best = 1, t_min, t_min
            for _ in range(40):
                if lo >= hi:
                    break
                mid = (lo + hi) // 2
                try:
                    sol_out, _ = self.quote_pump_sell_sol(mid, post_buy_curve, global_cfg)
                except Exception:
                    lo = mid + 1
                    continue
                if sol_out >= target_lamports:
                    best = mid
                    hi = mid
                else:
                    lo = mid + 1
            sol_out, _ = self.quote_pump_sell_sol(best, post_buy_curve, global_cfg)
            rec_fraction = best / max(t_min, 1)
            return (rec_fraction <= max_fraction, best, sol_out)

        # Outer binary search over T_min, from a low floor up to expected_tokens_raw
        lo = max(1, expected_tokens_raw // 2)
        hi = expected_tokens_raw
        best_t = expected_tokens_raw
        best_rec = expected_tokens_raw
        best_rec_lp = 0
        any_feasible = False

        # First check the worst case feasible-at-expected: if even T_expected is infeasible, block
        feas_at_expected, rec_at_expected, rec_lp_at_expected = feasible_at(expected_tokens_raw)
        if not feas_at_expected:
            log(
                f"PGG2-V39-BUY-RECOVERY-MIN-TOKEN-GUARD mint={short_addr(mint_str)} "
                f"expected_tokens_raw={expected_tokens_raw} existing_min_tokens_raw=- "
                f"min_recovery_buy_tokens_raw={expected_tokens_raw} "
                f"final_buy_min_tokens_raw={expected_tokens_raw} "
                f"recovery_fraction={rec_at_expected/max(expected_tokens_raw,1):.4f} "
                f"feasible=false gate_pass=false reason=infeasible_at_expected_tokens"
            )
            return {
                "expected_tokens_raw": expected_tokens_raw,
                "min_recovery_buy_tokens_raw": expected_tokens_raw,
                "recovery_tokens_raw_at_min": rec_at_expected,
                "expected_recovery_lamports": rec_lp_at_expected,
                "feasible": False,
                "max_fraction": max_fraction,
            }

        any_feasible = True
        best_t = expected_tokens_raw
        max_iter = max(16, env_int("PGG2_V39_RECOVERY_MIN_TOKENS_BSEARCH_MAX_ITER", 50))
        for _ in range(max_iter):
            if lo >= hi:
                break
            mid = (lo + hi) // 2
            feas, rec_tokens, rec_lp = feasible_at(mid)
            if feas:
                best_t = mid
                best_rec = rec_tokens
                best_rec_lp = rec_lp
                hi = mid
            else:
                lo = mid + 1

        # Re-check at best_t for the report
        _, best_rec, best_rec_lp = feasible_at(best_t)
        rec_fraction = best_rec / max(best_t, 1)
        log(
            f"PGG2-V39-BUY-RECOVERY-MIN-TOKEN-GUARD mint={short_addr(mint_str)} "
            f"expected_tokens_raw={expected_tokens_raw} existing_min_tokens_raw=- "
            f"min_recovery_buy_tokens_raw={best_t} final_buy_min_tokens_raw={best_t} "
            f"recovery_tokens_at_min={best_rec} recovery_fraction={rec_fraction:.4f} "
            f"expected_recovery_lamports={best_rec_lp} feasible=true gate_pass=true"
        )
        return {
            "expected_tokens_raw": expected_tokens_raw,
            "min_recovery_buy_tokens_raw": best_t,
            "recovery_tokens_raw_at_min": best_rec,
            "expected_recovery_lamports": best_rec_lp,
            "feasible": True,
            "max_fraction": max_fraction,
        }

    def quote_principal_recovery_tranche(
        self,
        mint_str: str,
        actual_tokens_raw: int,
        target_recovery_sol: float,
        sell_fee_buffer_sol: float = 0.0,
    ) -> dict[str, Any]:
        """Binary-search the smallest token amount whose Pump bonding-curve sell
        yield meets target_recovery_sol + sell_fee_buffer_sol.

        This is the core primitive for PR-Phase 2: instead of full-position sells,
        we sell *exactly enough* tokens to recover principal + fees + buffer, and
        keep the rest as a free residual.

        Returns:
            recovery_tokens_raw, recovery_tokens_ui, recovery_min_sol_lamports,
            expected_recovery_sol, residual_tokens_raw, principal_recovery_possible,
            recovery_fraction (recovery_tokens_raw / actual_tokens_raw)
        """
        mint = as_pubkey(mint_str)
        curve = self.bonding_curve(mint)
        global_cfg = self.pump_global()
        target_lamports = max(1, int((target_recovery_sol + sell_fee_buffer_sol) * LAMPORTS_PER_SOL))

        # Feasibility check: even selling 100% must clear target
        max_sol_lamports, _ = self.quote_pump_sell_sol(actual_tokens_raw, curve, global_cfg)
        if max_sol_lamports < target_lamports:
            log(
                f"PGG2-V39-PRINCIPAL-RECOVERY-QUOTE mint={short_addr(mint_str)} "
                f"actual_tokens_raw={actual_tokens_raw} target_recovery_sol={target_recovery_sol:.9f} "
                f"sell_fee_buffer_sol={sell_fee_buffer_sol:.9f} target_lamports={target_lamports} "
                f"max_sol_lamports={max_sol_lamports} recovery_tokens_raw={actual_tokens_raw} "
                f"recovery_fraction=1.0000 residual_tokens_raw=0 principal_recovery_possible=false"
            )
            return {
                "recovery_tokens_raw": actual_tokens_raw,
                "recovery_tokens_ui": self.raw_to_ui(mint, actual_tokens_raw),
                "expected_recovery_sol": max_sol_lamports / LAMPORTS_PER_SOL,
                "recovery_min_sol_lamports": target_lamports,
                "residual_tokens_raw": 0,
                "recovery_fraction": 1.0,
                "principal_recovery_possible": False,
            }

        # Binary search for the smallest token amount that clears target
        lo, hi = 1, actual_tokens_raw
        best = actual_tokens_raw
        max_iter = max(16, env_int("PGG2_V39_RECOVERY_BSEARCH_MAX_ITER", 60))
        for _ in range(max_iter):
            if lo >= hi:
                break
            mid = (lo + hi) // 2
            try:
                sol_out, _ = self.quote_pump_sell_sol(mid, curve, global_cfg)
            except Exception:
                lo = mid + 1
                continue
            if sol_out >= target_lamports:
                best = mid
                hi = mid
            else:
                lo = mid + 1

        sol_out, _ = self.quote_pump_sell_sol(best, curve, global_cfg)
        recovery_fraction = best / max(actual_tokens_raw, 1)
        log(
            f"PGG2-V39-PRINCIPAL-RECOVERY-QUOTE mint={short_addr(mint_str)} "
            f"actual_tokens_raw={actual_tokens_raw} target_recovery_sol={target_recovery_sol:.9f} "
            f"sell_fee_buffer_sol={sell_fee_buffer_sol:.9f} target_lamports={target_lamports} "
            f"recovery_tokens_raw={best} recovery_fraction={recovery_fraction:.4f} "
            f"expected_recovery_lamports={sol_out} residual_tokens_raw={actual_tokens_raw - best} "
            f"principal_recovery_possible=true"
        )
        return {
            "recovery_tokens_raw": best,
            "recovery_tokens_ui": self.raw_to_ui(mint, best),
            "expected_recovery_sol": sol_out / LAMPORTS_PER_SOL,
            "recovery_min_sol_lamports": target_lamports,
            "residual_tokens_raw": actual_tokens_raw - best,
            "recovery_fraction": recovery_fraction,
            "principal_recovery_possible": True,
        }

    def estimate_recovery_feasibility_pre_entry(
        self,
        mint_str: str,
        min_tokens_raw: int,
        target_recovery_sol: float,
    ) -> dict[str, Any]:
        """PR-Phase 5 pre-entry feasibility gate.

        Estimate whether principal recovery is plausible *before* sending the buy.
        Uses the buy guard's min_tokens_raw (the worst-case fill) as the basis for
        the recovery binary search. If even the worst-case fill can recover
        target_recovery_sol with recovery_fraction <= max_fraction, gate passes.
        """
        max_fraction = env_float("PGG2_V39_RECOVERY_MAX_FRACTION", 0.95)
        quote = self.quote_principal_recovery_tranche(
            mint_str=mint_str,
            actual_tokens_raw=int(min_tokens_raw),
            target_recovery_sol=float(target_recovery_sol),
        )
        gate_pass = bool(quote["principal_recovery_possible"]) and quote["recovery_fraction"] <= max_fraction
        log(
            f"PGG2-V39-RECOVERY-FEASIBILITY-GATE mint={short_addr(mint_str)} "
            f"expected_tokens_raw=- min_tokens_raw={int(min_tokens_raw)} "
            f"estimated_recovery_tokens_raw={quote['recovery_tokens_raw']} "
            f"recovery_fraction={quote['recovery_fraction']:.4f} "
            f"expected_recovery_sol={quote['expected_recovery_sol']:.9f} "
            f"target_recovery_sol={target_recovery_sol:.9f} max_fraction={max_fraction:.4f} "
            f"gate_pass={'true' if gate_pass else 'false'}"
        )
        return {**quote, "gate_pass": gate_pass, "max_fraction": max_fraction}

    def decode_pump_buy_guard_from_tx_b64(self, tx_b64: str) -> dict[str, Any]:
        """Decode bytes 8..16 (spend_lamports) and 16..24 (min_tokens_raw) from
        a signed/built Pump buy tx. Returns {kind, encoded_spend_lamports,
        encoded_min_tokens_raw} or {kind: 'none'}.

        Used by raptor's pre-send PGG2-V39-LIVE-BUY-GUARD-DECODE-PASS/FAIL check.
        """
        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(str(tx_b64)))
            for ix in tx.message.instructions:
                program_id = tx.message.account_keys[ix.program_id_index]
                data = bytes(ix.data)
                if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_BUY_EXACT_SOL_IN):
                    if len(data) >= len(DISC_PUMP_BUY_EXACT_SOL_IN) + 16:
                        prefix = len(DISC_PUMP_BUY_EXACT_SOL_IN)
                        return {
                            "kind": "buy",
                            "encoded_spend_lamports": struct.unpack("<Q", data[prefix:prefix + 8])[0],
                            "encoded_min_tokens_raw": struct.unpack("<Q", data[prefix + 8:prefix + 16])[0],
                        }
        except Exception:
            pass
        return {"kind": "none"}

    def decode_pump_sell_guard_from_tx_b64(self, tx_b64: str) -> dict[str, Any]:
        """Decode bytes 8..16 (token_amount_raw) and 16..24 (min_sol_lamports)
        from a Pump sell tx. Returns {kind, encoded_token_amount_raw,
        encoded_min_sol_lamports} or {kind: 'none'}.
        """
        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(str(tx_b64)))
            for ix in tx.message.instructions:
                program_id = tx.message.account_keys[ix.program_id_index]
                data = bytes(ix.data)
                if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_SELL):
                    if len(data) >= len(DISC_PUMP_SELL) + 16:
                        prefix = len(DISC_PUMP_SELL)
                        return {
                            "kind": "sell",
                            "encoded_token_amount_raw": struct.unpack("<Q", data[prefix:prefix + 8])[0],
                            "encoded_min_sol_lamports": struct.unpack("<Q", data[prefix + 8:prefix + 16])[0],
                        }
        except Exception:
            pass
        return {"kind": "none"}

    def retarget_buy_min_tokens(self, quote: dict[str, Any], mint_str: str, min_tokens_ui: float) -> dict[str, Any]:
        """Retarget an existing pump_bc buy transaction to an explicit min-token floor.

        Fail-closed: after mutation + resign, decode the encoded min_tokens_raw
        from the rebuilt tx bytes and verify it matches. Emit
        PGG2-BUY-MIN-TOKEN-GUARD-ENCODED on every call (pass or fail) and raise
        on mismatch so the caller cannot send a tx whose on-chain guard differs
        from what Python intended.
        """
        mint = as_pubkey(mint_str)
        min_tokens_raw = self.ui_to_raw(mint, min_tokens_ui)
        tx = VersionedTransaction.from_bytes(base64.b64decode(str(quote["txn"])))
        msg = tx.message
        out_instructions = []
        changed = False
        expected_tokens_raw = 0
        spend_lamports_encoded = 0
        for ix in msg.instructions:
            program_id = msg.account_keys[ix.program_id_index]
            data = bytes(ix.data)
            if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_BUY_EXACT_SOL_IN):
                prefix_len = len(DISC_PUMP_BUY_EXACT_SOL_IN) + 8
                # capture pre-mutation values for guard-encoded log
                if len(data) >= prefix_len:
                    spend_lamports_encoded = struct.unpack("<Q", data[len(DISC_PUMP_BUY_EXACT_SOL_IN):prefix_len])[0]
                if len(data) >= prefix_len + 8:
                    expected_tokens_raw = struct.unpack("<Q", data[prefix_len:prefix_len + 8])[0]
                data = data[:prefix_len] + u64(min_tokens_raw) + data[prefix_len + 8 :]
                changed = True
            out_instructions.append(CompiledInstruction(ix.program_id_index, data, ix.accounts))
        if not changed:
            log(
                f"PGG2-BUY-MIN-TOKEN-GUARD-ENCODED mint={short_addr(mint_str)} "
                f"expected_tokens_raw=0 min_tokens_raw={min_tokens_raw} encoded_min_tokens_raw=-1 "
                f"adaptive_slippage_pct=- guard_match=false reason=pump_buy_instruction_not_found"
            )
            raise RuntimeError("pump_buy_instruction_not_found_for_min_token_retarget")
        new_msg = MessageV0(msg.header, msg.account_keys, msg.recent_blockhash, out_instructions, msg.address_table_lookups)
        signer = self.keypair if self.keypair else NullSigner(as_pubkey(self.public_key))
        new_tx = VersionedTransaction(new_msg, [signer])
        new_tx_bytes = bytes(new_tx)
        # post-mutation decode-verify: parse the rebuilt tx and read bytes 16..24
        # of the pump buy ix data to confirm what's actually encoded
        verify_tx = VersionedTransaction.from_bytes(new_tx_bytes)
        encoded_min_tokens_raw = -1
        for ix in verify_tx.message.instructions:
            program_id = verify_tx.message.account_keys[ix.program_id_index]
            data = bytes(ix.data)
            if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_BUY_EXACT_SOL_IN):
                if len(data) >= len(DISC_PUMP_BUY_EXACT_SOL_IN) + 16:
                    encoded_min_tokens_raw = struct.unpack(
                        "<Q", data[len(DISC_PUMP_BUY_EXACT_SOL_IN) + 8: len(DISC_PUMP_BUY_EXACT_SOL_IN) + 16]
                    )[0]
                break
        guard_match = (encoded_min_tokens_raw == min_tokens_raw)
        adaptive_slip_pct = -1.0
        if expected_tokens_raw > 0:
            adaptive_slip_pct = 100.0 * (1.0 - (min_tokens_raw / max(expected_tokens_raw, 1)))
        log(
            f"PGG2-BUY-MIN-TOKEN-GUARD-ENCODED mint={short_addr(mint_str)} "
            f"expected_tokens_raw={expected_tokens_raw} min_tokens_raw={min_tokens_raw} "
            f"encoded_min_tokens_raw={encoded_min_tokens_raw} "
            f"adaptive_slippage_pct={adaptive_slip_pct:.4f} guard_match={'true' if guard_match else 'false'}"
        )
        if not guard_match:
            raise RuntimeError(
                f"pump_buy_min_token_guard_encoding_mismatch: "
                f"intended={min_tokens_raw} encoded={encoded_min_tokens_raw}"
            )
        out = dict(quote)
        rate = dict(out.get("rate") or {})
        rate["minAmountOut"] = self.raw_to_ui(mint, min_tokens_raw)
        out["rate"] = rate
        out["txn"] = base64.b64encode(new_tx_bytes).decode("ascii")
        log(
            f"PGG2-DIRECT-BUY-MIN-RETARGET mint={short_addr(mint_str)} "
            f"min_tokens={rate['minAmountOut']:.6f} source=existing_quote"
        )
        return out

    def build_buy_with_min_tokens(self, mint_str: str, amount_sol: float, min_tokens_ui: float) -> dict[str, Any]:
        """Build a pump_bc exact-SOL-in buy with an explicit token floor."""
        self._inflight_quotes[mint_str] = self._inflight_quotes.get(mint_str, 0) + 1
        _qstart_ms = int(time.time() * 1000)
        _qsim_needed = bool(env_bool("PGG2_DIRECT_SELECT_BUYBACK_BY_SIM", False) and self.quote_simulate)
        try:
            mint = as_pubkey(mint_str)
            curve = self.bonding_curve(mint)
            if curve.complete:
                raise RuntimeError("explicit_min_tokens_only_supports_pump_bc")
            global_cfg = self.pump_global()
            spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
            token_out, fee_lamports = self.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
            min_tokens_out = max(0, self.ui_to_raw(mint, min_tokens_ui))
            token_program = self.mint_owner(mint)
            user = as_pubkey(self.public_key)
            user_ata = get_associated_token_address(user, mint, token_program)
            associated_curve = get_associated_token_address(curve.key, mint, token_program)
            creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
            fee_recipient = self.pump_fee_recipient(global_cfg, curve)
            user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
            track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
            data = DISC_PUMP_BUY_EXACT_SOL_IN + u64(spend_lamports) + u64(min_tokens_out) + track_volume
            base_metas = [
                AccountMeta(self.pump_global_key, False, False),
                AccountMeta(fee_recipient, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(curve.key, False, True),
                AccountMeta(associated_curve, False, True),
                AccountMeta(user_ata, False, True),
                AccountMeta(user, True, True),
                AccountMeta(SYSTEM_PROGRAM_ID, False, False),
                AccountMeta(token_program, False, False),
                AccountMeta(creator_vault, False, True),
                AccountMeta(self.pump_event_authority, False, False),
                AccountMeta(PUMP_PROGRAM_ID, False, False),
                AccountMeta(self.pump_global_volume_accumulator, False, False),
                AccountMeta(user_volume, False, True),
                AccountMeta(self.pump_fee_config, False, False),
                AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
            ]
            ata_ix = create_idempotent_associated_token_account(user, user, mint, token_program)

            def make_tx(extra_metas: list[AccountMeta]) -> str:
                return self.compile_tx(
                    [
                        *self.compute_budget_ixs(),
                        ata_ix,
                        Instruction(PUMP_PROGRAM_ID, data, [*base_metas, *extra_metas]),
                    ]
                )

            selected_pair = ""
            txn = make_tx(self.pump_buy_remaining_metas(mint))
            existing_pair_src = self._last_pair_source.get(str(mint), "none")
            skip_sim_if_cached = env_bool("PGG2_DIRECT_SKIP_SIM_IF_CACHED", True)
            has_fast_pair = existing_pair_src in {"forced", "cache", "current_sig", "observed_raw_rpc"}
            if skip_sim_if_cached and has_fast_pair:
                _qsim_needed = False
            if env_bool("PGG2_DIRECT_SELECT_BUYBACK_BY_SIM", False) and self.quote_simulate and not (skip_sim_if_cached and has_fast_pair):
                selected = False
                for pair in self.pump_buyback_candidate_pairs(mint):
                    candidate_txn = make_tx(
                        [
                            AccountMeta(self.pump_bonding_curve_v2(mint), False, False),
                            AccountMeta(pair.social_fee_pda, False, True),
                        ]
                    )
                    try:
                        signed_b64, _ = self.sign_transaction(candidate_txn)
                        if self.simulate_signed(signed_b64):
                            txn = candidate_txn
                            selected_pair = (
                                f" buyback={short_addr(str(pair.recipient))}/"
                                f"{short_addr(str(pair.social_fee_pda))} source={pair.source}"
                            )
                            self.remember_pump_buyback_pair(mint, pair)
                            self._last_pair_source[str(mint)] = f"sim_selected:{pair.source}"
                            self._last_pair_recipient[str(mint)] = str(pair.recipient)
                            self._last_pair_social[str(mint)] = str(pair.social_fee_pda)
                            selected = True
                            break
                    except Exception as exc:
                        log(
                            "PGG2-DIRECT-WARN buyback_pair_sim_error "
                            f"{short_addr(str(pair.recipient))}/{short_addr(str(pair.social_fee_pda))} "
                            f"source={pair.source} "
                            f"{type(exc).__name__}: {exc}"
                        )
                if not selected:
                    msg = "PGG2-DIRECT-WARN no_buyback_pair_simulated_ok"
                    if env_bool("PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK", self.mode == "live"):
                        raise RuntimeError(msg)
                    log(f"{msg} returning_default_pair")
            out_ui = self.raw_to_ui(mint, token_out)
            min_ui = self.raw_to_ui(mint, min_tokens_out)
            fee_sol = fee_lamports / LAMPORTS_PER_SOL
            log(
                f"PGG2-DIRECT-QUOTE BUY {short_addr(mint_str)} route=pump_bc "
                f"in={amount_sol:.6f} out={out_ui:.6f} min={min_ui:.6f} "
                f"fee_bps={global_cfg.fee_bps + global_cfg.creator_fee_bps} fee={fee_sol:.6f} "
                f"explicit_min_tokens=1{selected_pair}"
            )
            _qend_ms = int(time.time() * 1000)
            pair_source = self._last_pair_source.get(str(mint), "unknown")
            try:
                self.record_quote_latency(
                    side="buy",
                    mint=mint_str,
                    route="pump_bc",
                    source=pair_source,
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=True,
                    pair_source=pair_source,
                    was_simulation_needed=_qsim_needed,
                )
            except Exception:
                pass
            return {
                "txn": txn,
                "route": "pump_bc",
                "quote_network_latency_ms": max(0, _qend_ms - _qstart_ms),
                "quote_returned_ts_ms": _qend_ms,
                "rate": {
                    "amountOut": out_ui,
                    "minAmountOut": min_ui,
                    "priceImpact": spend_lamports / max(curve.virtual_sol_reserves, 1),
                    "fee": fee_sol,
                    "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
                },
            }
        except Exception as exc:
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="buy",
                    mint=mint_str,
                    route="pump_bc",
                    source="error",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=False,
                    error_class=type(exc).__name__,
                    was_simulation_needed=_qsim_needed,
                )
            except Exception:
                pass
            raise
        finally:
            self._inflight_quotes[mint_str] = max(0, self._inflight_quotes.get(mint_str, 1) - 1)

    def build_buy_with_min_tokens_from_curve_snapshot(
        self,
        mint_str: str,
        amount_sol: float,
        min_tokens_ui: float,
        *,
        virtual_token_reserves: int,
        virtual_sol_reserves: int,
        real_token_reserves: int,
        real_sol_reserves: int,
        token_total_supply: int = 0,
        complete: bool = False,
        creator: str = "",
        is_mayhem: bool = False,
        cashback_enabled: bool = False,
        snapshot_ts_ms: int = 0,
    ) -> dict[str, Any]:
        """Build a pump_bc buy from the already-observed curve snapshot.

        This avoids the live adapter doing a fresh bonding-curve RPC read after
        V48 has already decided from accountSubscribe data. The transaction is
        still protected by the explicit min-token guard; if the chain has moved
        by send/sim time, Pump fails it with slippage rather than opening a bad
        position.
        """
        self._inflight_quotes[mint_str] = self._inflight_quotes.get(mint_str, 0) + 1
        _qstart_ms = int(time.time() * 1000)
        _qsim_needed = False
        try:
            mint = as_pubkey(mint_str)
            curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
            creator_pk = as_pubkey(creator) if creator else Pubkey.default()
            curve = PumpBondingCurve(
                key=curve_key,
                virtual_token_reserves=int(virtual_token_reserves),
                virtual_sol_reserves=int(virtual_sol_reserves),
                real_token_reserves=int(real_token_reserves),
                real_sol_reserves=int(real_sol_reserves),
                token_total_supply=int(token_total_supply),
                complete=bool(complete),
                creator=creator_pk,
                is_mayhem=bool(is_mayhem),
                cashback_enabled=bool(cashback_enabled),
            )
            if curve.complete:
                raise RuntimeError("snapshot_explicit_min_tokens_only_supports_pump_bc")
            global_cfg = self.pump_global()
            spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
            token_out, fee_lamports = self.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
            min_tokens_out = max(0, self.ui_to_raw(mint, min_tokens_ui))
            token_program = self.mint_owner(mint)
            user = as_pubkey(self.public_key)
            user_ata = get_associated_token_address(user, mint, token_program)
            associated_curve = get_associated_token_address(curve.key, mint, token_program)
            creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
            fee_recipient = self.pump_fee_recipient(global_cfg, curve)
            user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
            track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
            data = DISC_PUMP_BUY_EXACT_SOL_IN + u64(spend_lamports) + u64(min_tokens_out) + track_volume
            base_metas = [
                AccountMeta(self.pump_global_key, False, False),
                AccountMeta(fee_recipient, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(curve.key, False, True),
                AccountMeta(associated_curve, False, True),
                AccountMeta(user_ata, False, True),
                AccountMeta(user, True, True),
                AccountMeta(SYSTEM_PROGRAM_ID, False, False),
                AccountMeta(token_program, False, False),
                AccountMeta(creator_vault, False, True),
                AccountMeta(self.pump_event_authority, False, False),
                AccountMeta(PUMP_PROGRAM_ID, False, False),
                AccountMeta(self.pump_global_volume_accumulator, False, False),
                AccountMeta(user_volume, False, True),
                AccountMeta(self.pump_fee_config, False, False),
                AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
            ]
            ata_ix = create_idempotent_associated_token_account(user, user, mint, token_program)
            txn = self.compile_tx([
                *self.compute_budget_ixs(),
                ata_ix,
                Instruction(PUMP_PROGRAM_ID, data, [*base_metas, *self.pump_buy_remaining_metas(mint)]),
            ])
            out_ui = self.raw_to_ui(mint, token_out)
            min_ui = self.raw_to_ui(mint, min_tokens_out)
            fee_sol = fee_lamports / LAMPORTS_PER_SOL
            self._last_pair_source[str(mint)] = "decision_curve_snapshot"
            log(
                f"PGG2-DIRECT-QUOTE BUY {short_addr(mint_str)} route=pump_bc "
                f"in={amount_sol:.6f} out={out_ui:.6f} min={min_ui:.6f} "
                f"fee_bps={global_cfg.fee_bps + global_cfg.creator_fee_bps} fee={fee_sol:.6f} "
                f"explicit_min_tokens=1 source=decision_curve_snapshot "
                f"snapshot_age_ms={max(0, int(time.time() * 1000) - int(snapshot_ts_ms or 0)) if snapshot_ts_ms else -1}"
            )
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="buy",
                    mint=mint_str,
                    route="pump_bc",
                    source="decision_curve_snapshot",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=True,
                    pair_source="decision_curve_snapshot",
                    was_simulation_needed=_qsim_needed,
                )
            except Exception:
                pass
            return {
                "txn": txn,
                "route": "pump_bc",
                "quote_network_latency_ms": max(0, _qend_ms - _qstart_ms),
                "quote_returned_ts_ms": _qend_ms,
                "quote_source": "decision_curve_snapshot",
                "snapshot_ts_ms": int(snapshot_ts_ms or 0),
                "rate": {
                    "amountOut": out_ui,
                    "minAmountOut": min_ui,
                    "priceImpact": spend_lamports / max(curve.virtual_sol_reserves, 1),
                    "fee": fee_sol,
                    "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
                },
            }
        except Exception as exc:
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="buy",
                    mint=mint_str,
                    route="pump_bc",
                    source="decision_curve_snapshot_error",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=False,
                    error_class=type(exc).__name__,
                    was_simulation_needed=_qsim_needed,
                )
            except Exception:
                pass
            raise
        finally:
            self._inflight_quotes[mint_str] = max(0, self._inflight_quotes.get(mint_str, 1) - 1)

    def _build_buy_impl(self, mint_str: str, amount_sol: float, slippage: float, _qstart_ms: int, _qsim_needed: bool) -> dict[str, Any]:
        mint = as_pubkey(mint_str)
        curve = self.bonding_curve(mint)
        if curve.complete:
            return self.build_pumpswap_buy(mint, amount_sol, slippage)
        global_cfg = self.pump_global()
        spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
        token_out, fee_lamports = self.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
        min_tokens_out = int(token_out * max(0.0, 1.0 - slippage / 100.0))
        token_program = self.mint_owner(mint)
        user = as_pubkey(self.public_key)
        user_ata = get_associated_token_address(user, mint, token_program)
        associated_curve = get_associated_token_address(curve.key, mint, token_program)
        creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
        fee_recipient = self.pump_fee_recipient(global_cfg, curve)
        user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
        track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
        data = DISC_PUMP_BUY_EXACT_SOL_IN + u64(spend_lamports) + u64(min_tokens_out) + track_volume
        base_metas = [
            AccountMeta(self.pump_global_key, False, False),
            AccountMeta(fee_recipient, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(curve.key, False, True),
            AccountMeta(associated_curve, False, True),
            AccountMeta(user_ata, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
            AccountMeta(creator_vault, False, True),
            AccountMeta(self.pump_event_authority, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
            AccountMeta(self.pump_global_volume_accumulator, False, False),
            AccountMeta(user_volume, False, True),
            AccountMeta(self.pump_fee_config, False, False),
            AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
        ]
        ata_ix = create_idempotent_associated_token_account(user, user, mint, token_program)

        def make_tx(extra_metas: list[AccountMeta]) -> str:
            return self.compile_tx(
                [
                    *self.compute_budget_ixs(),
                    ata_ix,
                    Instruction(PUMP_PROGRAM_ID, data, [*base_metas, *extra_metas]),
                ]
            )

        selected_pair = ""
        txn = make_tx(self.pump_buy_remaining_metas(mint))
        # v32 — fast path: if pair was already discovered (forced/cache/observed
        # /current_sig), skip the sim-select grid search. Sim-select costs
        # 600-1700ms per pair candidate which makes actual-entry impossible to
        # risk-manage. Default ON; can be disabled if a deeper sim sweep is
        # explicitly needed for research.
        _existing_pair_src = self._last_pair_source.get(str(mint), "none")
        _skip_sim_if_cached = env_bool("PGG2_DIRECT_SKIP_SIM_IF_CACHED", True)
        _has_fast_pair = _existing_pair_src in {
            "forced",
            "cache",
            "current_sig",
            "observed_raw_rpc",
        }
        if _skip_sim_if_cached and _has_fast_pair:
            _qsim_needed = False  # we are NOT going to sim-select
        if env_bool("PGG2_DIRECT_SELECT_BUYBACK_BY_SIM", False) and self.quote_simulate and not (_skip_sim_if_cached and _has_fast_pair):
            selected = False
            for pair in self.pump_buyback_candidate_pairs(mint):
                candidate_txn = make_tx(
                    [
                        AccountMeta(self.pump_bonding_curve_v2(mint), False, False),
                        AccountMeta(pair.social_fee_pda, False, True),
                    ]
                )
                try:
                    signed_b64, _ = self.sign_transaction(candidate_txn)
                    if self.simulate_signed(signed_b64):
                        txn = candidate_txn
                        selected_pair = (
                            f" buyback={short_addr(str(pair.recipient))}/"
                            f"{short_addr(str(pair.social_fee_pda))} source={pair.source}"
                        )
                        self.remember_pump_buyback_pair(mint, pair)
                        # v30 — overwrite last-pair tracking with the sim-selected pair
                        self._last_pair_source[str(mint)] = f"sim_selected:{pair.source}"
                        self._last_pair_recipient[str(mint)] = str(pair.recipient)
                        self._last_pair_social[str(mint)] = str(pair.social_fee_pda)
                        log(
                            f"PGG2-DIRECT-PAIR-SIM-SELECTED mint={short_addr(str(mint))} "
                            f"source={pair.source} recipient={short_addr(str(pair.recipient))} "
                            f"social={short_addr(str(pair.social_fee_pda))}"
                        )
                        selected = True
                        break
                except Exception as exc:
                    log(
                        "PGG2-DIRECT-WARN buyback_pair_sim_error "
                        f"{short_addr(str(pair.recipient))}/{short_addr(str(pair.social_fee_pda))} "
                        f"source={pair.source} "
                        f"{type(exc).__name__}: {exc}"
                    )
            if not selected:
                msg = "PGG2-DIRECT-WARN no_buyback_pair_simulated_ok"
                if env_bool("PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK", self.mode == "live"):
                    raise RuntimeError(msg)
                log(f"{msg} returning_default_pair")
        out_ui = self.raw_to_ui(mint, token_out)
        min_ui = self.raw_to_ui(mint, min_tokens_out)
        fee_sol = fee_lamports / LAMPORTS_PER_SOL
        log(
            f"PGG2-DIRECT-QUOTE BUY {short_addr(mint_str)} route=pump_bc "
            f"in={amount_sol:.6f} out={out_ui:.6f} min={min_ui:.6f} "
            f"fee_bps={global_cfg.fee_bps + global_cfg.creator_fee_bps} fee={fee_sol:.6f}{selected_pair}"
        )
        # v31 — record latency telemetry for the successful buy quote
        _qend_ms = int(time.time() * 1000)
        _pair_source = self._last_pair_source.get(str(mint), "unknown")
        try:
            self.record_quote_latency(
                side="buy",
                mint=mint_str,
                route="pump_bc",
                source=_pair_source,
                start_ms=_qstart_ms,
                end_ms=_qend_ms,
                success=True,
                pair_source=_pair_source,
                was_simulation_needed=_qsim_needed,
            )
        except Exception:
            pass
        return {
            "txn": txn,
            "route": "pump_bc",
            "quote_network_latency_ms": max(0, _qend_ms - _qstart_ms),
            "quote_returned_ts_ms": _qend_ms,
            "rate": {
                "amountOut": out_ui,
                "minAmountOut": min_ui,
                "priceImpact": spend_lamports / max(curve.virtual_sol_reserves, 1),
                "fee": fee_sol,
                "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
            },
        }

    def build_sell(self, mint_str: str, amount: Any, slippage: float) -> dict[str, Any]:
        # v31 — quote latency telemetry for sell.
        self._inflight_quotes[mint_str] = self._inflight_quotes.get(mint_str, 0) + 1
        _qstart_ms = int(time.time() * 1000)
        try:
            result = self._build_sell_impl(mint_str, amount, slippage, _qstart_ms)
            # v34 — populate recent-sell-quote cache so the close() path for
            # risk-owned mints can reuse this quote and skip a parallel
            # network call (broker in_flight=1 invariant).
            try:
                self.record_sell_quote(mint_str, result, float(self.rate_amount_out(result)))
            except Exception:
                pass
            return result
        except Exception as exc:
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="sell",
                    mint=mint_str,
                    route="pump_bc",
                    source="error",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=False,
                    error_class=type(exc).__name__,
                )
            except Exception:
                pass
            raise
        finally:
            self._inflight_quotes[mint_str] = max(0, self._inflight_quotes.get(mint_str, 1) - 1)

    def build_sell_from_curve_snapshot(
        self,
        mint_str: str,
        amount: Any,
        slippage: float,
        *,
        virtual_token_reserves: int,
        virtual_sol_reserves: int,
        real_token_reserves: int,
        real_sol_reserves: int,
        token_total_supply: int = 0,
        complete: bool = False,
        creator: str = "",
        is_mayhem: bool = False,
        cashback_enabled: bool = False,
        snapshot_ts_ms: int = 0,
        include_close_token_ata: bool = False,
    ) -> dict[str, Any]:
        """Build a pump_bc sell from an already-observed accountSubscribe curve.

        V48 live cannot afford a fresh bonding_curve()/token-balance quote round
        trip after the buy lands. This mirrors the buy snapshot builder: quote
        and build from the latest local curve point, then protect execution with
        the caller's min-SOL guard.
        """
        self._inflight_quotes[mint_str] = self._inflight_quotes.get(mint_str, 0) + 1
        _qstart_ms = int(time.time() * 1000)
        try:
            mint = as_pubkey(mint_str)
            curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
            creator_pk = as_pubkey(creator) if creator else Pubkey.default()
            curve = PumpBondingCurve(
                key=curve_key,
                virtual_token_reserves=int(virtual_token_reserves),
                virtual_sol_reserves=int(virtual_sol_reserves),
                real_token_reserves=int(real_token_reserves),
                real_sol_reserves=int(real_sol_reserves),
                token_total_supply=int(token_total_supply),
                complete=bool(complete),
                creator=creator_pk,
                is_mayhem=bool(is_mayhem),
                cashback_enabled=bool(cashback_enabled),
            )
            if curve.complete:
                raise RuntimeError("snapshot_sell_only_supports_pump_bc")
            global_cfg = self.pump_global()
            token_amount = self.ui_to_raw(mint, amount)
            expected_lamports, fee_lamports = self.quote_pump_sell_sol(token_amount, curve, global_cfg)
            min_sol_output = int(expected_lamports * max(0.0, 1.0 - float(slippage) / 100.0))
            token_program = self.mint_owner(mint)
            user = as_pubkey(self.public_key)
            user_ata = get_associated_token_address(user, mint, token_program)
            associated_curve = get_associated_token_address(curve.key, mint, token_program)
            creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
            fee_recipient = self.pump_fee_recipient(global_cfg, curve)
            data = DISC_PUMP_SELL + u64(token_amount) + u64(min_sol_output)
            metas = [
                AccountMeta(self.pump_global_key, False, False),
                AccountMeta(fee_recipient, False, True),
                AccountMeta(mint, False, False),
                AccountMeta(curve.key, False, True),
                AccountMeta(associated_curve, False, True),
                AccountMeta(user_ata, False, True),
                AccountMeta(user, True, True),
                AccountMeta(SYSTEM_PROGRAM_ID, False, False),
                AccountMeta(creator_vault, False, True),
                AccountMeta(token_program, False, False),
                AccountMeta(self.pump_event_authority, False, False),
                AccountMeta(PUMP_PROGRAM_ID, False, False),
                AccountMeta(self.pump_fee_config, False, False),
                AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
                *self.pump_sell_remaining_metas(mint, curve, user),
            ]
            ixs = [*self.compute_budget_ixs(), Instruction(PUMP_PROGRAM_ID, data, metas)]
            if include_close_token_ata:
                ixs.append(close_token_account(token_program, user_ata, user, user))
            txn = self.compile_tx(ixs)
            out_sol = expected_lamports / LAMPORTS_PER_SOL
            min_sol = min_sol_output / LAMPORTS_PER_SOL
            fee_sol = fee_lamports / LAMPORTS_PER_SOL
            log(
                f"PGG2-DIRECT-QUOTE SELL {short_addr(mint_str)} route=pump_bc "
                f"in_tokens={self.raw_to_ui(mint, token_amount):.6f} out={out_sol:.6f} "
                f"min={min_sol:.6f} fee_bps={global_cfg.fee_bps + global_cfg.creator_fee_bps} "
                f"fee={fee_sol:.6f} source=curve_snapshot "
                f"snapshot_age_ms={max(0, int(time.time() * 1000) - int(snapshot_ts_ms or 0)) if snapshot_ts_ms else -1}"
            )
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="sell",
                    mint=mint_str,
                    route="pump_bc",
                    source="curve_snapshot",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=True,
                    pair_source=self._last_pair_source.get(str(mint), "unknown"),
                )
            except Exception:
                pass
            result = {
                "txn": txn,
                "route": "pump_bc",
                "quote_network_latency_ms": max(0, _qend_ms - _qstart_ms),
                "quote_returned_ts_ms": _qend_ms,
                "quote_source": "curve_snapshot",
                "snapshot_ts_ms": int(snapshot_ts_ms or 0),
                "rate": {
                    "amountOut": out_sol,
                    "minAmountOut": min_sol,
                    "priceImpact": token_amount / max(curve.virtual_token_reserves, 1),
                    "fee": fee_sol,
                    "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
                },
            }
            try:
                self.record_sell_quote(mint_str, result, float(self.rate_amount_out(result)))
            except Exception:
                pass
            return result
        except Exception as exc:
            _qend_ms = int(time.time() * 1000)
            try:
                self.record_quote_latency(
                    side="sell",
                    mint=mint_str,
                    route="pump_bc",
                    source="curve_snapshot_error",
                    start_ms=_qstart_ms,
                    end_ms=_qend_ms,
                    success=False,
                    error_class=type(exc).__name__,
                )
            except Exception:
                pass
            raise
        finally:
            self._inflight_quotes[mint_str] = max(0, self._inflight_quotes.get(mint_str, 1) - 1)

    def retarget_sell_min_sol(self, quote: dict[str, Any], mint_str: str, min_sol_out: float) -> dict[str, Any]:
        """Retarget an existing pump_bc sell transaction to an explicit SOL floor.

        Fail-closed:
          - refuse min_sol_lamports == 0 (or 1, the legacy EXIT_ANY fallback)
          - after mutation, decode the rebuilt tx and verify encoded_min_sol_lamports
            matches what was intended
          - emit PGG2-SELL-MIN-SOL-GUARD-ENCODED with structured fields
          - raise on mismatch so caller cannot send an unprotected sell

        Callers that want the legacy "exit at any price" behaviour for non-v39
        emergencies must use the default builder path; v39 live emergency
        closes must still go through this function with an explicit floor.
        """
        min_lamports = max(0, int(float(min_sol_out) * LAMPORTS_PER_SOL))
        # block v39 unsafe-zero / one-lamport encodings
        floor_lamports = max(1, env_int("PGG2_V39_SELL_MIN_SOL_FLOOR_LAMPORTS", 100))
        if min_lamports < floor_lamports:
            log(
                f"PGG2-SELL-MIN-SOL-GUARD-ENCODED mint={short_addr(mint_str)} "
                f"sell_tokens_raw=0 expected_sol_out=- min_sol_lamports={min_lamports} "
                f"encoded_min_sol_lamports=-1 guard_match=false reason=below_floor "
                f"floor_lamports={floor_lamports}"
            )
            raise RuntimeError(
                f"pump_sell_min_sol_below_floor: min_lamports={min_lamports} floor={floor_lamports}"
            )
        tx = VersionedTransaction.from_bytes(base64.b64decode(str(quote["txn"])))
        msg = tx.message
        out_instructions = []
        changed = False
        sell_tokens_raw = 0
        for ix in msg.instructions:
            program_id = msg.account_keys[ix.program_id_index]
            data = bytes(ix.data)
            if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_SELL):
                prefix_len = len(DISC_PUMP_SELL) + 8
                if len(data) >= prefix_len:
                    sell_tokens_raw = struct.unpack(
                        "<Q", data[len(DISC_PUMP_SELL):prefix_len]
                    )[0]
                data = data[:prefix_len] + u64(min_lamports) + data[prefix_len + 8 :]
                changed = True
            out_instructions.append(CompiledInstruction(ix.program_id_index, data, ix.accounts))
        if not changed:
            log(
                f"PGG2-SELL-MIN-SOL-GUARD-ENCODED mint={short_addr(mint_str)} "
                f"sell_tokens_raw=0 expected_sol_out=- min_sol_lamports={min_lamports} "
                f"encoded_min_sol_lamports=-1 guard_match=false reason=pump_sell_instruction_not_found"
            )
            raise RuntimeError("pump_sell_instruction_not_found_for_min_sol_retarget")
        new_msg = MessageV0(msg.header, msg.account_keys, msg.recent_blockhash, out_instructions, msg.address_table_lookups)
        signer = self.keypair if self.keypair else NullSigner(as_pubkey(self.public_key))
        new_tx = VersionedTransaction(new_msg, [signer])
        new_tx_bytes = bytes(new_tx)
        verify_tx = VersionedTransaction.from_bytes(new_tx_bytes)
        encoded_min_sol_lamports = -1
        for ix in verify_tx.message.instructions:
            program_id = verify_tx.message.account_keys[ix.program_id_index]
            data = bytes(ix.data)
            if program_id == PUMP_PROGRAM_ID and data.startswith(DISC_PUMP_SELL):
                if len(data) >= len(DISC_PUMP_SELL) + 16:
                    encoded_min_sol_lamports = struct.unpack(
                        "<Q", data[len(DISC_PUMP_SELL) + 8: len(DISC_PUMP_SELL) + 16]
                    )[0]
                break
        expected_sol_out = float((quote.get("rate") or {}).get("amountOut") or 0.0)
        guard_match = (encoded_min_sol_lamports == min_lamports)
        log(
            f"PGG2-SELL-MIN-SOL-GUARD-ENCODED mint={short_addr(mint_str)} "
            f"sell_tokens_raw={sell_tokens_raw} expected_sol_out={expected_sol_out:.9f} "
            f"min_sol_lamports={min_lamports} encoded_min_sol_lamports={encoded_min_sol_lamports} "
            f"guard_match={'true' if guard_match else 'false'}"
        )
        if not guard_match:
            raise RuntimeError(
                f"pump_sell_min_sol_guard_encoding_mismatch: "
                f"intended={min_lamports} encoded={encoded_min_sol_lamports}"
            )
        out = dict(quote)
        rate = dict(out.get("rate") or {})
        rate["minAmountOut"] = min_lamports / LAMPORTS_PER_SOL
        out["rate"] = rate
        out["txn"] = base64.b64encode(new_tx_bytes).decode("ascii")
        log(
            f"PGG2-DIRECT-SELL-MIN-RETARGET mint={short_addr(mint_str)} "
            f"min_sol={rate['minAmountOut']:.9f} source=existing_quote"
        )
        return out

    def _build_sell_impl(self, mint_str: str, amount: Any, slippage: float, _qstart_ms: int) -> dict[str, Any]:
        mint = as_pubkey(mint_str)
        curve = self.bonding_curve(mint)
        if curve.complete:
            return self.build_pumpswap_sell(mint, amount, slippage)
        global_cfg = self.pump_global()
        token_amount = self.ui_to_raw(mint, amount)
        expected_lamports, fee_lamports = self.quote_pump_sell_sol(token_amount, curve, global_cfg)
        min_sol_output = int(expected_lamports * max(0.0, 1.0 - slippage / 100.0))
        if env_bool("PGG2_DIRECT_EXIT_ANY_EXECUTABLE_PRICE", False):
            if env_bool("PGG2_V39_LIVE_FORBID_EXIT_ANY", False):
                # PR-Phase 1: block the legacy "exit at any price" override when
                # v39-live forbids it. Keep the slippage-based min_sol_output instead.
                log(
                    f"PGG2-V39-FALLBACK-PATH-BLOCK side=sell reason=exit_any_executable_price "
                    f"mint={short_addr(mint_str)} retained_min_sol_lamports={min_sol_output}"
                )
            else:
                min_sol_output = max(0, env_int("PGG2_DIRECT_EXIT_MIN_LAMPORTS", 1))
        token_program = self.mint_owner(mint)
        user = as_pubkey(self.public_key)
        user_ata = get_associated_token_address(user, mint, token_program)
        associated_curve = get_associated_token_address(curve.key, mint, token_program)
        creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
        fee_recipient = self.pump_fee_recipient(global_cfg, curve)
        data = DISC_PUMP_SELL + u64(token_amount) + u64(min_sol_output)
        metas = [
            AccountMeta(self.pump_global_key, False, False),
            AccountMeta(fee_recipient, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(curve.key, False, True),
            AccountMeta(associated_curve, False, True),
            AccountMeta(user_ata, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(creator_vault, False, True),
            AccountMeta(token_program, False, False),
            AccountMeta(self.pump_event_authority, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
            AccountMeta(self.pump_fee_config, False, False),
            AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
            *self.pump_sell_remaining_metas(mint, curve, user),
        ]
        ixs = [*self.compute_budget_ixs(), Instruction(PUMP_PROGRAM_ID, data, metas)]
        include_close = env_bool("PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL", True) and not bool(
            getattr(self, "_force_no_close_token_ata", False)
        )
        if include_close and self.mode == "live" and env_bool("PGG2_DIRECT_CLOSE_TOKEN_ATA_ONLY_ON_FULL_BALANCE", True):
            try:
                live_balance_raw = int(self.token_balance_raw(mint))
            except Exception:
                live_balance_raw = -1
            if live_balance_raw <= 0 or token_amount < live_balance_raw:
                include_close = False
                log(
                    f"PGG2-DIRECT-CLOSE-ATA-SKIP mint={short_addr(mint_str)} "
                    f"reason=partial_or_unknown_balance sell_tokens_raw={token_amount} "
                    f"wallet_balance_raw={live_balance_raw}"
                )
        if include_close:
            ixs.append(close_token_account(token_program, user_ata, user, user))
        txn = self.compile_tx(ixs)
        out_sol = expected_lamports / LAMPORTS_PER_SOL
        min_sol = min_sol_output / LAMPORTS_PER_SOL
        fee_sol = fee_lamports / LAMPORTS_PER_SOL
        log(
            f"PGG2-DIRECT-QUOTE SELL {short_addr(mint_str)} route=pump_bc "
            f"in_tokens={self.raw_to_ui(mint, token_amount):.6f} out={out_sol:.6f} "
            f"min={min_sol:.6f} fee_bps={global_cfg.fee_bps + global_cfg.creator_fee_bps} fee={fee_sol:.6f}"
        )
        # v31 — record sell quote latency
        _qend_ms = int(time.time() * 1000)
        _pair_source = self._last_pair_source.get(str(mint), "unknown")
        try:
            self.record_quote_latency(
                side="sell",
                mint=mint_str,
                route="pump_bc",
                source=_pair_source,
                start_ms=_qstart_ms,
                end_ms=_qend_ms,
                success=True,
                pair_source=_pair_source,
            )
        except Exception:
            pass
        return {
            "txn": txn,
            "route": "pump_bc",
            "quote_network_latency_ms": max(0, _qend_ms - _qstart_ms),
            "quote_returned_ts_ms": _qend_ms,
            "rate": {
                "amountOut": out_sol,
                "minAmountOut": min_sol,
                "priceImpact": token_amount / max(curve.virtual_token_reserves, 1),
                "fee": fee_sol,
                "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
            },
        }

    # ------------------------------------------------------------------
    # V63 standalone CloseAccount builder — safety net for ATAs that
    # remained open after a full-balance sell. Used by
    # pgg2_v63_post_sell_clean_close after V62B-SELL-CONFIRMED to recover
    # any rent that the broker's atomic sell+close path failed to recover.
    # ------------------------------------------------------------------
    def build_close_account(
        self,
        mint: str,
        ata: str,
        owner: str,
        token_program: str,
    ) -> dict[str, Any]:
        user = as_pubkey(self.public_key)
        if str(user) != owner:
            raise RuntimeError(
                f"build_close_account_owner_mismatch broker={user} requested={owner}"
            )
        ata_pk = as_pubkey(ata)
        tp_pk = as_pubkey(token_program)
        close_ix = close_token_account(tp_pk, ata_pk, user, user)
        ixs = [*self.compute_budget_ixs(), close_ix]
        txn = self.compile_tx(ixs)
        return {"txn": txn, "route": "v63_close_account"}

    # ------------------------------------------------------------------
    # v36c-3 ATOMIC ESB — one Solana v0 transaction containing
    #   compute_budget(x2)  +  create_idempotent_ATA  +  pump_buy
    #   +  pump_sell  +  close_token_account
    # All-or-nothing atomicity preserves the entry-snapshot edge in live
    # mode without requiring a Jito tip. Caller: AtomicSingleTxESBExecutor.
    # ------------------------------------------------------------------
    def build_atomic_buy_sell_close(
        self,
        mint_str: str,
        amount_sol: float,
        sell_slippage_pct: float,
        compute_unit_limit: int = 600_000,
        compute_unit_price_micro_lamports: int = 22_700,
        expected_quote_tokens: Optional[float] = None,
    ) -> dict[str, Any]:
        # Route-aware refusal: this builder only supports pump_bc. The
        # caller has already gated on route, but defend in depth.
        mint = as_pubkey(mint_str)
        curve = self.bonding_curve(mint)
        if curve.complete:
            raise RuntimeError(
                "build_atomic_buy_sell_close: curve complete; pump_bc required, "
                "pumpswap atomic not supported in this builder"
            )
        global_cfg = self.pump_global()
        # ----- BUY instruction (mirrors _build_buy_impl exactly) -----
        spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
        token_out, _buy_fee_lamports = self.quote_pump_buy_tokens(
            spend_lamports, curve, global_cfg
        )
        # Allow zero buy-side slippage in atomic mode; the sell side's
        # min_sol_output enforces price floor on the exit leg.
        min_tokens_out = int(token_out)
        token_program = self.mint_owner(mint)
        user = as_pubkey(self.public_key)
        user_ata = get_associated_token_address(user, mint, token_program)
        associated_curve = get_associated_token_address(curve.key, mint, token_program)
        creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
        fee_recipient = self.pump_fee_recipient(global_cfg, curve)
        user_volume = pda(PUMP_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
        track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
        buy_data = (
            DISC_PUMP_BUY_EXACT_SOL_IN + u64(spend_lamports) + u64(min_tokens_out) + track_volume
        )
        buy_metas = [
            AccountMeta(self.pump_global_key, False, False),
            AccountMeta(fee_recipient, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(curve.key, False, True),
            AccountMeta(associated_curve, False, True),
            AccountMeta(user_ata, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_program, False, False),
            AccountMeta(creator_vault, False, True),
            AccountMeta(self.pump_event_authority, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
            AccountMeta(self.pump_global_volume_accumulator, False, False),
            AccountMeta(user_volume, False, True),
            AccountMeta(self.pump_fee_config, False, False),
            AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
            *self.pump_buy_remaining_metas(mint),
        ]
        buy_ix = Instruction(PUMP_PROGRAM_ID, buy_data, buy_metas)
        # ----- SELL instruction (sells the EXACT token_out from buy) -----
        # min_sol_output is the slippage floor against the post-buy curve
        # state. We compute it against the CURRENT (pre-buy) curve as a
        # conservative lower bound — the post-buy state will have slightly
        # less SOL in the curve (so sell output will be slightly lower),
        # which is fine since min_sol_output is the FLOOR, not the
        # expected.
        sell_expected_lamports, _sell_fee_lamports = self.quote_pump_sell_sol(
            token_out, curve, global_cfg
        )
        sell_min_sol_output = int(
            sell_expected_lamports * max(0.0, 1.0 - float(sell_slippage_pct) / 100.0)
        )
        if env_bool("PGG2_DIRECT_EXIT_ANY_EXECUTABLE_PRICE", False):
            sell_min_sol_output = max(
                0, env_int("PGG2_DIRECT_EXIT_MIN_LAMPORTS", 1)
            )
        sell_data = DISC_PUMP_SELL + u64(token_out) + u64(sell_min_sol_output)
        sell_metas = [
            AccountMeta(self.pump_global_key, False, False),
            AccountMeta(fee_recipient, False, True),
            AccountMeta(mint, False, False),
            AccountMeta(curve.key, False, True),
            AccountMeta(associated_curve, False, True),
            AccountMeta(user_ata, False, True),
            AccountMeta(user, True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(creator_vault, False, True),
            AccountMeta(token_program, False, False),
            AccountMeta(self.pump_event_authority, False, False),
            AccountMeta(PUMP_PROGRAM_ID, False, False),
            AccountMeta(self.pump_fee_config, False, False),
            AccountMeta(PUMP_FEE_PROGRAM_ID, False, False),
            *self.pump_sell_remaining_metas(mint, curve, user),
        ]
        sell_ix = Instruction(PUMP_PROGRAM_ID, sell_data, sell_metas)
        # ----- ATA create_idempotent + (optional) close_account -----
        ata_ix = create_idempotent_associated_token_account(user, user, mint, token_program)
        # v36c-3: close_token_account in the atomic tx fails with SPL Token
        # Custom:11 (NonNativeHasBalance) whenever Pump delivers MORE tokens
        # than `min_tokens_out` (buy_exact_sol_in is a floor, not exact).
        # Sell consumes exactly `token_out`, leaving residual dust. Close
        # rejects non-empty accounts. Default to NO close inside the atomic
        # tx; the cost is one ATA rent (~0.002 SOL) staying locked per
        # trade. v33 cost model already accounts for ata_recoverable in
        # the legacy/dry-live path; for atomic ESB the recovery is
        # deferred to a separate cleanup tx (out of scope of one-entry
        # smoke). Set PGG2_ATOMIC_ESB_INCLUDE_CLOSE=1 to force-include it
        # if a future Pump instruction variant supports exact-output buys.
        include_close = env_bool("PGG2_ATOMIC_ESB_INCLUDE_CLOSE", False)
        close_ix = (
            close_token_account(token_program, user_ata, user, user)
            if include_close
            else None
        )
        # ----- Custom compute budget (atomic needs more CU than per-leg) -----
        from solders.compute_budget import (  # type: ignore
            set_compute_unit_limit,
            set_compute_unit_price,
        )
        cb_limit_ix = set_compute_unit_limit(int(compute_unit_limit))
        cb_price_ix = set_compute_unit_price(int(compute_unit_price_micro_lamports))
        # ----- Assemble the atomic instruction list -----
        # Order matters:
        #   1. compute budget (limit, price) — first so they apply to all
        #   2. ATA create_idempotent — must run before buy stores tokens
        #   3. pump buy
        #   4. pump sell (sells the locked token_out from buy)
        #   5. close token account — claim rent back since all tokens sold
        instructions = [cb_limit_ix, cb_price_ix, ata_ix, buy_ix, sell_ix]
        if close_ix is not None:
            instructions.append(close_ix)
        # Token invariant (REAL one): sell.token_amount must equal
        # buy.token_out within the SAME transaction. Both use `token_out`
        # from the same atomic-build quote, so this is satisfied by
        # construction. The caller's `expected_quote_tokens` (if passed)
        # came from an EARLIER dry-live quote against a different
        # (typically pre-buy) curve state — comparing it strictly to the
        # atomic build's now-quote is incorrect because the curve moves
        # between quotes. We record the delta as a feature for the
        # truth-test report instead of blocking.
        buy_tokens_ui = self.raw_to_ui(mint, token_out)
        caller_token_delta = None
        if expected_quote_tokens is not None:
            caller_token_delta = float(expected_quote_tokens) - float(buy_tokens_ui)
        # Compile into a v0 transaction (compile_tx already returns a
        # MessageV0-based VersionedTransaction). Then sign with the live
        # keypair so the caller can simulate + send directly.
        unsigned_b64 = self.compile_tx(instructions)
        signed_b64, signed_b58 = self.sign_transaction(unsigned_b64)
        # tx_size: decode + len. compile_tx returns base64 of the full
        # serialized signed (well, NullSigner-stubbed) tx — but we then
        # re-sign which produces the actual signed bytes.
        signed_bytes_len = len(base64.b64decode(signed_b64))
        ix_summary = {
            "compute_budget_limit": int(compute_unit_limit),
            "compute_budget_price": int(compute_unit_price_micro_lamports),
            "ata_create_idempotent": True,
            "pump_buy": True,
            "pump_sell": True,
            "close_token_account": bool(close_ix is not None),
        }
        log(
            f"PGG2-ATOMIC-ESB-BUILD-PASS mint={short_addr(mint_str)} "
            f"variant=A buy_sell_close size={signed_bytes_len}B "
            f"cu_limit={compute_unit_limit} cu_price={compute_unit_price_micro_lamports} "
            f"buy_tokens={buy_tokens_ui:.6f} sell_tokens={buy_tokens_ui:.6f} "
            f"min_sol_out={sell_min_sol_output/LAMPORTS_PER_SOL:.6f}"
        )
        return {
            "signed_b64": signed_b64,
            "signed_b58": signed_b58,
            "route": "pump_bc",
            "expected_buy_tokens": float(buy_tokens_ui),
            "sell_input_tokens": float(buy_tokens_ui),
            "caller_token_delta": caller_token_delta,
            "sell_min_sol_output_sol": sell_min_sol_output / LAMPORTS_PER_SOL,
            "compute_unit_limit": int(compute_unit_limit),
            "compute_unit_price_micro_lamports": int(compute_unit_price_micro_lamports),
            "tx_size_bytes": int(signed_bytes_len),
            "cost_model_route": "pump_bc",
            "ix_summary": ix_summary,
            "variant": "A",
        }

    def pumpswap_global(self) -> PumpSwapGlobal:
        info = self.account_info(self.pump_amm_global_config, ttl_sec=5.0)
        if not info:
            raise RuntimeError("pumpswap global config missing")
        data = self.account_data(info)
        recipients = [Pubkey.from_string(x) for x in DEFAULT_PUMPSWAP_FEE_RECIPIENTS]
        reserved: list[Pubkey] = []
        lp_fee_bps = 20
        protocol_fee_bps = 5
        coin_creator_fee_bps = 5
        if len(data) >= 321:
            lp_fee_bps = int.from_bytes(data[40:48], "little")
            protocol_fee_bps = int.from_bytes(data[48:56], "little")
            recipients = [Pubkey.from_bytes(data[57 + i * 32:57 + (i + 1) * 32]) for i in range(8)]
            coin_creator_fee_bps = int.from_bytes(data[313:321], "little")
            reserved_start = 321 + 32 + 32 + 32 + 1
            if len(data) >= reserved_start + 7 * 32:
                reserved = [Pubkey.from_bytes(data[reserved_start + i * 32:reserved_start + (i + 1) * 32]) for i in range(7)]
        return PumpSwapGlobal(lp_fee_bps, protocol_fee_bps, coin_creator_fee_bps, recipients, reserved)

    def parse_pool(self, key: Pubkey, data: bytes) -> PumpSwapPool:
        if len(data) < 243:
            raise RuntimeError(f"pumpswap pool data too short: {key} len={len(data)}")
        return PumpSwapPool(
            key=key,
            index=int.from_bytes(data[9:11], "little"),
            creator=Pubkey.from_bytes(data[11:43]),
            base_mint=Pubkey.from_bytes(data[43:75]),
            quote_mint=Pubkey.from_bytes(data[75:107]),
            pool_base_token_account=Pubkey.from_bytes(data[139:171]),
            pool_quote_token_account=Pubkey.from_bytes(data[171:203]),
            coin_creator=Pubkey.from_bytes(data[211:243]) if len(data) >= 243 else Pubkey.default(),
            is_mayhem=bool(data[243]) if len(data) > 243 else False,
        )

    def pumpswap_pool(self, mint: Pubkey) -> PumpSwapPool:
        curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint))
        candidates = [curve_key, PUMP_PROGRAM_ID]
        try:
            curve = self.bonding_curve(mint)
            candidates.append(curve.creator)
        except Exception:
            pass
        for creator in dict.fromkeys(candidates):
            pool_key = pda(PUMP_AMM_PROGRAM_ID, b"pool", u16(0), bytes(creator), bytes(mint), bytes(WSOL_MINT))
            info = self.account_info(pool_key)
            if not info:
                continue
            pool = self.parse_pool(pool_key, self.account_data(info))
            if pool.base_mint == mint and pool.quote_mint == WSOL_MINT:
                return pool
        out = self.rpc(
            "getProgramAccounts",
            [
                str(PUMP_AMM_PROGRAM_ID),
                {
                    "encoding": "base64",
                    "filters": [
                        {"memcmp": {"offset": 43, "bytes": str(mint)}},
                        {"memcmp": {"offset": 75, "bytes": str(WSOL_MINT)}},
                    ],
                },
            ],
        )
        for row in out or []:
            key = Pubkey.from_string(row["pubkey"])
            pool = self.parse_pool(key, self.account_data((row.get("account") or {})))
            if pool.base_mint == mint and pool.quote_mint == WSOL_MINT:
                return pool
        raise RuntimeError(f"pumpswap pool not found: {short_addr(str(mint))}")

    def token_account_balance_raw(self, token_account: Pubkey) -> int:
        data = self.account_data(self.account_info(token_account) or {})
        return int.from_bytes(data[64:72], "little") if len(data) >= 72 else 0

    def quote_pumpswap_buy(self, spend_lamports: int, pool: PumpSwapPool, global_cfg: PumpSwapGlobal) -> tuple[int, int]:
        total_fee_bps = global_cfg.lp_fee_bps + global_cfg.protocol_fee_bps + global_cfg.coin_creator_fee_bps
        net_quote = spend_lamports * 10_000 // max(10_000 + total_fee_bps, 1)
        fees = spend_lamports - net_quote
        base_reserve = self.token_account_balance_raw(pool.pool_base_token_account)
        quote_reserve = self.token_account_balance_raw(pool.pool_quote_token_account)
        base_out = net_quote * base_reserve // max(quote_reserve + net_quote, 1)
        return max(0, base_out), max(0, fees)

    def quote_pumpswap_sell(self, base_amount: int, pool: PumpSwapPool, global_cfg: PumpSwapGlobal) -> tuple[int, int]:
        total_fee_bps = global_cfg.lp_fee_bps + global_cfg.protocol_fee_bps + global_cfg.coin_creator_fee_bps
        base_reserve = self.token_account_balance_raw(pool.pool_base_token_account)
        quote_reserve = self.token_account_balance_raw(pool.pool_quote_token_account)
        gross_quote = base_amount * quote_reserve // max(base_reserve + base_amount, 1)
        fees = ceil_div(gross_quote * total_fee_bps, 10_000)
        return max(0, gross_quote - fees), max(0, fees)

    def pumpswap_fee_recipient(self, global_cfg: PumpSwapGlobal, pool: PumpSwapPool) -> Pubkey:
        recipients = global_cfg.reserved_fee_recipients if pool.is_mayhem and global_cfg.reserved_fee_recipients else global_cfg.protocol_fee_recipients
        return recipients[int(time.time_ns()) % max(len(recipients), 1)]

    def build_pumpswap_buy(self, mint: Pubkey, amount_sol: float, slippage: float) -> dict[str, Any]:
        pool = self.pumpswap_pool(mint)
        global_cfg = self.pumpswap_global()
        spend_lamports = max(1, int(amount_sol * LAMPORTS_PER_SOL))
        base_out, fee_lamports = self.quote_pumpswap_buy(spend_lamports, pool, global_cfg)
        min_base_out = int(base_out * max(0.0, 1.0 - slippage / 100.0))
        user = as_pubkey(self.public_key)
        base_token_program = self.mint_owner(mint)
        quote_token_program = TOKEN_PROGRAM_ID
        user_base_ata = get_associated_token_address(user, mint, base_token_program)
        user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)
        fee_recipient = self.pumpswap_fee_recipient(global_cfg, pool)
        fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, quote_token_program)
        creator_vault_authority = pda(PUMP_AMM_PROGRAM_ID, b"creator_vault", bytes(pool.coin_creator))
        creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, quote_token_program)
        user_volume = pda(PUMP_AMM_PROGRAM_ID, b"user_volume_accumulator", bytes(user))
        track_volume = b"\x01" if env_bool("PGG2_DIRECT_TRACK_VOLUME", True) else b"\x00"
        data = DISC_PUMP_AMM_BUY_EXACT_QUOTE_IN + u64(spend_lamports) + u64(min_base_out) + track_volume
        metas = self.pumpswap_common_metas(
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
        ixs = [
            *self.compute_budget_ixs(),
            create_idempotent_associated_token_account(user, user, mint, base_token_program),
            create_idempotent_associated_token_account(user, user, WSOL_MINT, quote_token_program),
            transfer(TransferParams(from_pubkey=user, to_pubkey=user_quote_ata, lamports=spend_lamports)),
            sync_native(quote_token_program, user_quote_ata),
            Instruction(PUMP_AMM_PROGRAM_ID, data, metas),
            close_token_account(quote_token_program, user_quote_ata, user, user),
        ]
        txn = self.compile_tx(ixs)
        out_ui = self.raw_to_ui(mint, base_out)
        min_ui = self.raw_to_ui(mint, min_base_out)
        fee_sol = fee_lamports / LAMPORTS_PER_SOL
        total_bps = global_cfg.lp_fee_bps + global_cfg.protocol_fee_bps + global_cfg.coin_creator_fee_bps
        log(
            f"PGG2-DIRECT-QUOTE BUY {short_addr(str(mint))} route=pumpswap "
            f"in={amount_sol:.6f} out={out_ui:.6f} min={min_ui:.6f} fee_bps={total_bps} fee={fee_sol:.6f}"
        )
        return {"txn": txn, "route": "pumpswap", "rate": {"amountOut": out_ui, "minAmountOut": min_ui, "priceImpact": spend_lamports / max(self.token_account_balance_raw(pool.pool_quote_token_account), 1), "fee": fee_sol, "feeBps": total_bps}}

    def build_pumpswap_sell(self, mint: Pubkey, amount: Any, slippage: float) -> dict[str, Any]:
        pool = self.pumpswap_pool(mint)
        global_cfg = self.pumpswap_global()
        base_amount = self.ui_to_raw(mint, amount)
        quote_out, fee_lamports = self.quote_pumpswap_sell(base_amount, pool, global_cfg)
        min_quote_out = int(quote_out * max(0.0, 1.0 - slippage / 100.0))
        if env_bool("PGG2_DIRECT_EXIT_ANY_EXECUTABLE_PRICE", False):
            min_quote_out = max(0, env_int("PGG2_DIRECT_EXIT_MIN_LAMPORTS", 1))
        user = as_pubkey(self.public_key)
        base_token_program = self.mint_owner(mint)
        quote_token_program = TOKEN_PROGRAM_ID
        user_base_ata = get_associated_token_address(user, mint, base_token_program)
        user_quote_ata = get_associated_token_address(user, WSOL_MINT, quote_token_program)
        fee_recipient = self.pumpswap_fee_recipient(global_cfg, pool)
        fee_recipient_ata = get_associated_token_address(fee_recipient, WSOL_MINT, quote_token_program)
        creator_vault_authority = pda(PUMP_AMM_PROGRAM_ID, b"creator_vault", bytes(pool.coin_creator))
        creator_vault_ata = get_associated_token_address(creator_vault_authority, WSOL_MINT, quote_token_program)
        data = DISC_PUMP_AMM_SELL + u64(base_amount) + u64(min_quote_out)
        metas = self.pumpswap_common_metas(
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
        ixs = [
            *self.compute_budget_ixs(),
            create_idempotent_associated_token_account(user, user, WSOL_MINT, quote_token_program),
            Instruction(PUMP_AMM_PROGRAM_ID, data, metas),
            close_token_account(quote_token_program, user_quote_ata, user, user),
        ]
        if env_bool("PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL", True):
            ixs.append(close_token_account(base_token_program, user_base_ata, user, user))
        txn = self.compile_tx(ixs)
        out_sol = quote_out / LAMPORTS_PER_SOL
        min_sol = min_quote_out / LAMPORTS_PER_SOL
        fee_sol = fee_lamports / LAMPORTS_PER_SOL
        total_bps = global_cfg.lp_fee_bps + global_cfg.protocol_fee_bps + global_cfg.coin_creator_fee_bps
        log(
            f"PGG2-DIRECT-QUOTE SELL {short_addr(str(mint))} route=pumpswap "
            f"in_tokens={self.raw_to_ui(mint, base_amount):.6f} out={out_sol:.6f} "
            f"min={min_sol:.6f} fee_bps={total_bps} fee={fee_sol:.6f}"
        )
        return {"txn": txn, "route": "pumpswap", "rate": {"amountOut": out_sol, "minAmountOut": min_sol, "priceImpact": base_amount / max(self.token_account_balance_raw(pool.pool_base_token_account), 1), "fee": fee_sol, "feeBps": total_bps}}

    def pumpswap_common_metas(
        self,
        pool: PumpSwapPool,
        user: Pubkey,
        base_mint: Pubkey,
        user_base_ata: Pubkey,
        user_quote_ata: Pubkey,
        fee_recipient: Pubkey,
        fee_recipient_ata: Pubkey,
        base_token_program: Pubkey,
        quote_token_program: Pubkey,
        creator_vault_ata: Pubkey,
        creator_vault_authority: Pubkey,
        user_volume: Optional[Pubkey],
        *,
        include_volume: bool,
    ) -> list[AccountMeta]:
        metas = [
            AccountMeta(pool.key, False, True),
            AccountMeta(user, True, True),
            AccountMeta(self.pump_amm_global_config, False, False),
            AccountMeta(base_mint, False, False),
            AccountMeta(WSOL_MINT, False, False),
            AccountMeta(user_base_ata, False, True),
            AccountMeta(user_quote_ata, False, True),
            AccountMeta(pool.pool_base_token_account, False, True),
            AccountMeta(pool.pool_quote_token_account, False, True),
            AccountMeta(fee_recipient, False, False),
            AccountMeta(fee_recipient_ata, False, True),
            AccountMeta(base_token_program, False, False),
            AccountMeta(quote_token_program, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
            AccountMeta(self.pump_amm_event_authority, False, False),
            AccountMeta(PUMP_AMM_PROGRAM_ID, False, False),
            AccountMeta(creator_vault_ata, False, True),
            AccountMeta(creator_vault_authority, False, False),
        ]
        if include_volume and user_volume:
            metas.extend(
                [
                    AccountMeta(self.pump_amm_global_volume_accumulator, False, False),
                    AccountMeta(user_volume, False, True),
                ]
            )
        metas.extend([AccountMeta(self.pump_amm_fee_config, False, False), AccountMeta(PUMP_FEE_PROGRAM_ID, False, False)])
        return metas
