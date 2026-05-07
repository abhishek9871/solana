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
from solders.instruction import AccountMeta, Instruction
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
        self._mint_decimals: dict[str, int] = {}
        log(
            f"PGG2-DIRECT: mode={self.mode.upper()} "
            "route=pump_fun_bonding_curve+pumpswap "
            f"send={0 if self.quote_only else 1} third_party_swap=0"
        )

    def build_swap(self, from_mint: str, to_mint: str, amount: Any, slippage: float) -> dict[str, Any]:
        if from_mint == SOL_MINT and to_mint != SOL_MINT:
            return self.build_buy(to_mint, float(amount), slippage)
        if to_mint == SOL_MINT and from_mint != SOL_MINT:
            return self.build_sell(from_mint, amount, slippage)
        raise RuntimeError(f"direct broker only supports SOL<->token swaps: {from_mint}->{to_mint}")

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

    def latest_blockhash(self) -> Hash:
        commitment = env_str("PGG2_DIRECT_BLOCKHASH_COMMITMENT", "processed")
        out = self.rpc("getLatestBlockhash", [{"commitment": commitment}])
        return Hash.from_string(str(((out or {}).get("value") or {}).get("blockhash")))

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
        pair = self.forced_pump_buyback_pair() or self.cached_pump_buyback_pair(mint) or self.observed_pump_buyback_pair(mint)
        if not pair and env_bool("PGG2_DIRECT_REQUIRE_OBSERVED_BUYBACK_PAIR", self.mode == "live"):
            raise RuntimeError(f"no confirmed pump buyback/social pair observed: {short_addr(str(mint))}")
        if pair:
            social_fee_pda = AccountMeta(pair.social_fee_pda, False, True)
        else:
            buyback = self.pump_buyback_remaining_metas(mint)
            social_fee_pda = buyback[-1] if buyback else None
        metas = [AccountMeta(self.pump_bonding_curve_v2(mint), False, False)]
        if social_fee_pda:
            metas.append(social_fee_pda)
        return metas

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
        if env_bool("PGG2_DIRECT_SELECT_BUYBACK_BY_SIM", False) and self.quote_simulate:
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
        return {
            "txn": txn,
            "route": "pump_bc",
            "rate": {
                "amountOut": out_ui,
                "minAmountOut": min_ui,
                "priceImpact": spend_lamports / max(curve.virtual_sol_reserves, 1),
                "fee": fee_sol,
                "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
            },
        }

    def build_sell(self, mint_str: str, amount: Any, slippage: float) -> dict[str, Any]:
        mint = as_pubkey(mint_str)
        curve = self.bonding_curve(mint)
        if curve.complete:
            return self.build_pumpswap_sell(mint, amount, slippage)
        global_cfg = self.pump_global()
        token_amount = self.ui_to_raw(mint, amount)
        expected_lamports, fee_lamports = self.quote_pump_sell_sol(token_amount, curve, global_cfg)
        min_sol_output = int(expected_lamports * max(0.0, 1.0 - slippage / 100.0))
        if env_bool("PGG2_DIRECT_EXIT_ANY_EXECUTABLE_PRICE", False):
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
        if env_bool("PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL", True):
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
        return {
            "txn": txn,
            "route": "pump_bc",
            "rate": {
                "amountOut": out_sol,
                "minAmountOut": min_sol,
                "priceImpact": token_amount / max(curve.virtual_token_reserves, 1),
                "fee": fee_sol,
                "feeBps": global_cfg.fee_bps + global_cfg.creator_fee_bps,
            },
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
