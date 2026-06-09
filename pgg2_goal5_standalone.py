#!/usr/bin/env python3
"""Small Goal5 pump.fun live/dry runner.

This file intentionally does not launch or import the V287/V288 smoke engine.
It owns the whole decision path:

  PublicNode Yellowstone gRPC feed -> one explicit Goal5 lane -> guarded buy
  -> scratch/target sell loop -> final wallet/token check.

The live path is fail-closed by default:
  * no hidden send authority,
  * no broad V287/V288 fallback branches,
  * no buy unless the Goal5 shape and quote band both pass,
  * no sell with min_out=0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


LAMPORTS_PER_SOL = 1_000_000_000
ATA_RENT_LAMPORTS = 2_039_280
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DISC_BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
DISC_BUY_EXACT_SOL_IN = bytes([56, 252, 116, 8, 158, 223, 205, 95])
DISC_SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
KNOWN_SOCIAL_FEE_PDAS = {
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
}


def _root() -> Path:
    remote = Path("/root/piggy")
    return remote if remote.exists() else Path.cwd()


ROOT = _root()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if len(s) > 10 else s


def _branch_rescue_negative_headroom_lamports(args: argparse.Namespace, reason: str) -> int:
    if reason == "goal5_c0_q500_follow_continuation":
        return int(args.c0_q500_rescue_negative_headroom_lamports)
    if reason == "goal5_one_follow_positive_scratch":
        return int(args.one_follow_rescue_negative_headroom_lamports)
    return int(args.rescue_negative_headroom_lamports)


def _load_env() -> None:
    for path in (ROOT / ".env", Path.cwd() / ".env"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _default_rpc_url() -> str:
    if os.environ.get("GOAL5_RPC_URL"):
        return str(os.environ["GOAL5_RPC_URL"])
    if os.environ.get("V287_RPC_URL"):
        return str(os.environ["V287_RPC_URL"])
    if os.environ.get("HELIUS_API_KEY"):
        return f"https://mainnet.helius-rpc.com/?api-key={os.environ['HELIUS_API_KEY']}"
    if os.environ.get("RPCFAST_API_KEY"):
        return f"https://solana-rpc.rpcfast.com/?api_key={os.environ['RPCFAST_API_KEY']}"
    if os.environ.get("SOLANATRACKER_RPC_HTTP"):
        return str(os.environ["SOLANATRACKER_RPC_HTTP"])
    return "https://api.mainnet-beta.solana.com"


def _configure_broker_env(args: argparse.Namespace) -> None:
    os.environ["PGG2_EXECUTION_MODE"] = "live" if args.live else "quote"
    os.environ["PGG2_LIVE_CONFIRM"] = "I_ACCEPT_REAL_SOL_RISK"
    os.environ["PGG2_DIRECT_LIVE_CONFIRM"] = "I_ACCEPT_DIRECT_PUMP_RISK"
    os.environ.setdefault("PGG2_WALLET_KEYPAIR", str(ROOT / "live_wallet.key"))
    os.environ["PGG2_LIVE_RPC_URL"] = str(args.rpc_url)
    os.environ["HELIUS_RPC_URL"] = str(args.rpc_url)
    os.environ["PGG2_LIVE_SKIP_PREFLIGHT"] = "1"
    os.environ["PGG2_LIVE_MAX_RETRIES"] = "0"
    os.environ["PGG2_LIVE_SIMULATE_BEFORE_SEND"] = "0"
    os.environ["PGG2_LIVE_CONFIRM_TIMEOUT_SEC"] = "18"
    os.environ["PGG2_QUOTE_SIMULATE"] = "0"
    os.environ["PGG2_DIRECT_SELECT_BUYBACK_BY_SIM"] = "0"
    os.environ["PGG2_DIRECT_SKIP_SIM_IF_CACHED"] = "1"
    os.environ["PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL"] = "1"
    os.environ["PGG2_DIRECT_CLOSE_TOKEN_ATA_ONLY_ON_FULL_BALANCE"] = "1"
    os.environ["PGG2_DIRECT_COMPUTE_UNIT_LIMIT"] = "260000"
    os.environ["PGG2_DIRECT_PRIORITY_FEE_SOL"] = f"{float(args.priority_fee_sol):.9f}"
    os.environ["PGG2_DIRECT_BLOCKHASH_COMMITMENT"] = "processed"
    os.environ.setdefault("PGG2_DIRECT_BLOCKHASH_CACHE_MS", "30000")
    os.environ["PGG2_V74_SENDER_URL"] = "https://sender.helius-rpc.com/fast?swqos_only=true"
    os.environ["PGG2_V74_SENDER_PING_URL"] = "https://sender.helius-rpc.com/ping"
    os.environ["PGG2_V75_TIP_LAMPORTS"] = str(int(args.tip_lamports))
    os.environ["PGG2_V39_SELL_MIN_SOL_FLOOR_LAMPORTS"] = str(max(100, int(args.sell_floor_lamports)))


def _make_broker(args: argparse.Namespace) -> Any:
    from birth_first_sniper import BotConfig  # type: ignore
    from pgg2_direct_pump import DirectPumpQuoteBroker  # type: ignore

    _configure_broker_env(args)
    broker = DirectPumpQuoteBroker(BotConfig())
    if args.live:
        from pgg2_v74_sender_adapter import install_into_broker, make_sender  # type: ignore
        from pgg2_v75_sender_tx_builder import make_tip_builder  # type: ignore

        make_tip_builder(log_fn=_log).install_into_broker(broker)
        sender = make_sender(log_fn=_log)
        install_into_broker(broker, sender, log_fn=_log)
        sender.validate_endpoint()
        sender.ping()
    return broker


def _proto_imports() -> tuple[Any, Any, Any, Any]:
    import grpc  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore
    from solders.signature import Signature  # type: ignore

    proto_dir = ROOT / "yellowstone_proto"
    if str(proto_dir) not in sys.path:
        sys.path.insert(0, str(proto_dir))
    import geyser_pb2  # type: ignore
    import geyser_pb2_grpc  # type: ignore

    return grpc, Pubkey, Signature, (geyser_pb2, geyser_pb2_grpc)


def _request_iter(args: argparse.Namespace) -> Iterator[Any]:
    grpc, _Pubkey, _Signature, protos = _proto_imports()
    geyser_pb2, _geyser_pb2_grpc = protos
    req = geyser_pb2.SubscribeRequest()
    flt = req.transactions["pump"]
    flt.vote = False
    flt.failed = False
    flt.account_include.append(PUMP_PROGRAM)
    req.commitment = geyser_pb2.PROCESSED
    yield req
    ping_id = 1
    while True:
        time.sleep(max(5, int(args.ping_seconds)))
        ping = geyser_pb2.SubscribeRequest()
        ping.ping.id = ping_id
        ping_id += 1
        yield ping


def _decode_pump(update: Any) -> dict[str, Any] | None:
    _grpc, Pubkey, Signature, _protos = _proto_imports()
    if not update.HasField("transaction"):
        return None
    info = update.transaction.transaction
    tx = info.transaction
    if not tx.signatures:
        return None
    sig = str(Signature.from_bytes(tx.signatures[0]))

    def pubkey(raw: bytes) -> str:
        try:
            return str(Pubkey.from_bytes(raw))
        except Exception:
            return ""

    keys = [pubkey(bytes(k)) for k in tx.message.account_keys]
    try:
        meta = info.meta
        keys.extend(pubkey(bytes(k)) for k in getattr(meta, "loaded_writable_addresses", []))
        keys.extend(pubkey(bytes(k)) for k in getattr(meta, "loaded_readonly_addresses", []))
    except Exception:
        pass
    for ix in tx.message.instructions:
        try:
            program = keys[int(ix.program_id_index)]
        except Exception:
            continue
        if program != PUMP_PROGRAM:
            continue
        data = bytes(ix.data)
        if len(data) < 24:
            continue
        disc = data[:8]
        if disc not in {DISC_BUY, DISC_BUY_EXACT_SOL_IN, DISC_SELL}:
            continue
        accounts = list(bytes(ix.accounts))

        def acct(i: int) -> str:
            try:
                return keys[int(accounts[i])]
            except Exception:
                return ""

        buyback_recipient = ""
        social_fee_pda = ""
        try:
            fee_positions = [
                pos
                for pos, account_idx in enumerate(accounts)
                if int(account_idx) < len(keys) and keys[int(account_idx)] == "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
            ]
            if fee_positions:
                extras = [
                    keys[int(account_idx)]
                    for account_idx in accounts[fee_positions[-1] + 1 :]
                    if int(account_idx) < len(keys)
                ]
                social_positions = [idx for idx, key in enumerate(extras) if key in KNOWN_SOCIAL_FEE_PDAS]
                if social_positions:
                    social_idx = social_positions[0]
                    social_fee_pda = str(extras[social_idx])
                    recipients = [
                        str(key)
                        for key in extras[:social_idx]
                        if key and key not in KNOWN_SOCIAL_FEE_PDAS
                    ]
                    if recipients:
                        buyback_recipient = recipients[0]
        except Exception:
            pass

        first = int.from_bytes(data[8:16], "little")
        second = int.from_bytes(data[16:24], "little")
        common = {
            "slot": int(update.transaction.slot),
            "sig": sig,
            "fee_recipient": acct(1),
            "mint": acct(2),
            "curve": acct(3),
            "token_program": acct(8),
            "creator_vault": acct(9),
            "buyback_recipient": buyback_recipient,
            "social_fee_pda": social_fee_pda,
            "recv_ms": _now_ms(),
        }
        if disc == DISC_SELL:
            common.update({"kind": "sell", "token_amount_raw": first, "sol_lamports": 0})
        else:
            sol_lamports = first if disc == DISC_BUY_EXACT_SOL_IN else second
            common.update({"kind": "buy", "token_amount_raw": 0, "sol_lamports": int(sol_lamports)})
        return common
    return None


def _remember_feed_accounts(broker: Any, rec: dict[str, Any]) -> None:
    from pgg2_direct_pump import PumpBuybackPair, as_pubkey  # type: ignore

    mint = str(rec.get("mint") or "")
    if not mint:
        return
    token_program = str(rec.get("token_program") or "")
    creator_vault = str(rec.get("creator_vault") or "")
    fee_recipient = str(rec.get("fee_recipient") or "")
    try:
        if token_program:
            as_pubkey(token_program)
            getattr(broker, "_account_cache", {})[mint] = (
                time.time(),
                {"owner": token_program, "data": ["", "base64"]},
            )
        if creator_vault:
            as_pubkey(creator_vault)
            getattr(broker, "_pump_creator_vault_override", {})[mint] = creator_vault
        if fee_recipient:
            as_pubkey(fee_recipient)
            getattr(broker, "_pump_fee_recipient_override", {})[mint] = fee_recipient
        recipient = str(rec.get("buyback_recipient") or "")
        social = str(rec.get("social_fee_pda") or "")
        if recipient and social:
            broker.remember_pump_buyback_pair(
                as_pubkey(mint),
                PumpBuybackPair(as_pubkey(recipient), as_pubkey(social), "goal5_geyser"),
            )
    except Exception as exc:
        _log(f"PGG2-GOAL5-FEED-ACCOUNT-WARN mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:120]}")


@dataclass
class Candidate:
    mint: str
    start_ms: int
    current_lamports: int
    start_sig: str
    feed_rec: dict[str, Any]
    follow_lamports: int = 0
    follow_buys: int = 0
    first_follow_lamports: int = 0
    sells: int = 0
    follow_sigs: list[str] = field(default_factory=list)
    evaluated: bool = False


def _wallet_lamports(broker: Any, commitment: str = "processed") -> int:
    out = broker.rpc("getBalance", [str(broker.public_key), {"commitment": commitment}])
    return int((out or {}).get("value") or 0)


def _token_accounts_summary(broker: Any) -> tuple[int, int]:
    nonzero = 0
    rent_locked = 0
    owner = str(broker.public_key)
    for program_id in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            out = broker.rpc(
                "getTokenAccountsByOwner",
                [owner, {"programId": program_id}, {"encoding": "jsonParsed", "commitment": "processed"}],
            )
        except Exception:
            continue
        for item in (out or {}).get("value") or []:
            account = item.get("account") or {}
            info = (((account.get("data") or {}).get("parsed") or {}).get("info") or {})
            amount = str(((info.get("tokenAmount") or {}).get("amount") or "0"))
            lamports = int(account.get("lamports") or 0)
            if amount != "0":
                nonzero += 1
            elif lamports > 0:
                rent_locked += 1
    return nonzero, rent_locked


def _token_balance_raw(broker: Any, mint: str, commitment: str = "processed") -> int:
    from pgg2_direct_pump import as_pubkey, get_associated_token_address  # type: ignore

    mint_pk = as_pubkey(mint)
    token_program = broker.mint_owner(mint_pk)
    ata = get_associated_token_address(as_pubkey(str(broker.public_key)), mint_pk, token_program)
    out = broker.rpc("getTokenAccountBalance", [str(ata), {"commitment": commitment}])
    return int(((out or {}).get("value") or {}).get("amount") or 0)


def _token_balance_raw_or_zero(broker: Any, mint: str, commitment: str = "processed") -> int:
    try:
        return _token_balance_raw(broker, mint, commitment)
    except Exception as exc:
        msg = str(exc).lower()
        if (
            "could not find account" in msg
            or "account not found" in msg
            or "invalid param" in msg
            or "invalid params" in msg
        ):
            return 0
        raise


def _token_account_lamports(broker: Any, mint: str, commitment: str = "processed") -> int:
    owner = str(broker.public_key)
    try:
        out = broker.rpc(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": commitment}],
        )
    except Exception:
        return 0
    return max((int(((x.get("account") or {}).get("lamports") or 0)) for x in (out or {}).get("value") or []), default=0)


def _wait_token_balance(broker: Any, mint: str, timeout_sec: float, poll_ms: int) -> int:
    deadline = time.time() + max(0.1, float(timeout_sec))
    while time.time() < deadline:
        try:
            bal = _token_balance_raw(broker, mint, "processed")
            if bal > 0:
                return bal
        except Exception:
            pass
        time.sleep(max(0.01, poll_ms / 1000.0))
    return 0


def _shape_allowed(
    args: argparse.Namespace,
    current_sol: float,
    follow_sol: float,
    follow_buys: int,
    quote_tokens: float,
    first_follow_sol: float = 0.0,
) -> tuple[bool, str]:
    if not (args.current_min_sol <= current_sol <= args.current_max_sol):
        primary_current = False
    else:
        primary_current = True
    primary_ok = (
        getattr(args, "primary_enabled", True)
        and primary_current
        and args.follow_min_sol <= follow_sol <= args.follow_max_sol
        and args.follow_min_buys <= follow_buys <= args.follow_max_buys
        and args.quote_min_tokens <= quote_tokens <= args.quote_max_tokens
    )
    if primary_ok:
        return True, "goal5_primary_mid_quote"

    moderate_first_early_ok = (
        args.strong_first_impulse_enabled
        and args.strong_follow_enabled
        and args.strong_current_min_sol <= current_sol <= args.strong_current_max_sol
        and 0.95 <= first_follow_sol <= 1.25
        and 0.95 <= follow_sol <= 1.30
        and follow_buys == 1
        and 700_000 <= quote_tokens <= 800_000
    )
    if moderate_first_early_ok:
        return True, "goal5_moderate_first_follow_early"

    strong_first_ok = (
        args.strong_first_impulse_enabled
        and args.strong_follow_enabled
        and args.strong_current_min_sol <= current_sol <= args.strong_current_max_sol
        and args.strong_first_follow_min_sol <= first_follow_sol <= args.strong_first_follow_max_sol
        and args.strong_first_impulse_min_follow_sol <= follow_sol <= args.strong_first_impulse_max_follow_sol
        and 1 <= follow_buys <= args.strong_first_impulse_max_follow_buys
        and args.strong_first_impulse_min_quote_tokens <= quote_tokens <= args.strong_quote_max_tokens
    )
    if strong_first_ok:
        return True, "goal5_strong_first_follow_impulse"

    dynamic_score = 0
    dynamic_base_ok = (
        args.dynamic_continuation_enabled
        and args.strong_follow_enabled
        and args.strong_current_min_sol <= current_sol <= args.strong_current_max_sol
        and args.dynamic_first_min_sol <= first_follow_sol <= args.dynamic_first_max_sol
        and args.dynamic_total_min_sol <= follow_sol <= args.dynamic_total_max_sol
        and args.dynamic_follow_min_buys <= follow_buys <= args.dynamic_follow_max_buys
        and args.dynamic_quote_min_tokens <= quote_tokens <= args.dynamic_quote_max_tokens
    )
    if dynamic_base_ok:
        dynamic_score += 2 if 1.70 <= first_follow_sol <= 2.15 else 1
        dynamic_score += 2 if 3.05 <= follow_sol <= 3.95 else 1
        dynamic_score += 2 if 590_000 <= quote_tokens <= 680_000 else 1
        dynamic_score += 1 if follow_buys == 2 else 0
    if dynamic_base_ok and dynamic_score >= args.dynamic_score_min:
        return True, "goal5_dynamic_continuation_score"

    flow_score = 0
    flow_base_ok = (
        args.flow_dominant_enabled
        and args.strong_follow_enabled
        and args.strong_current_min_sol <= current_sol <= args.strong_current_max_sol
        and args.flow_first_min_sol <= first_follow_sol <= args.flow_first_max_sol
        and args.flow_total_min_sol <= follow_sol <= args.flow_total_max_sol
        and args.flow_follow_min_buys <= follow_buys <= args.flow_follow_max_buys
        and args.flow_quote_min_tokens <= quote_tokens <= args.flow_quote_max_tokens
    )
    if flow_base_ok:
        flow_score += 2 if 1.75 <= first_follow_sol <= 2.05 else 1
        flow_score += 2 if 3.75 <= follow_sol <= 4.05 else 1
        flow_score += 1 if follow_buys == 2 else 0
        flow_score += 1 if 220_000 <= quote_tokens <= 700_000 else 0
    if flow_base_ok and flow_score >= args.flow_score_min:
        return True, "goal5_flow_dominant_continuation"

    strong_ok = (
        args.strong_follow_enabled
        and args.strong_current_min_sol <= current_sol <= args.strong_current_max_sol
        and args.strong_first_follow_min_sol <= first_follow_sol <= args.strong_first_follow_max_sol
        and args.strong_follow_min_sol <= follow_sol <= args.strong_follow_max_sol
        and args.strong_follow_min_buys <= follow_buys <= args.strong_follow_max_buys
        and args.strong_quote_min_tokens <= quote_tokens <= args.strong_quote_max_tokens
    )
    if strong_ok:
        return True, "goal5_strong_fast_follow"

    heavy_lowquote_futureflow_ok = (
        getattr(args, "heavy_lowquote_futureflow_enabled", False)
        and 3.00 <= current_sol <= 4.50
        and 2.00 <= first_follow_sol <= 3.20
        and 2.00 <= follow_sol <= 3.30
        and follow_buys == 1
        and 100_000 <= quote_tokens <= 350_000
    )
    if heavy_lowquote_futureflow_ok:
        return True, "goal5_heavy_lowquote_futureflow"

    cur1_small_midquote_futureflow_ok = (
        getattr(args, "cur1_small_midquote_futureflow_enabled", False)
        and 0.90 <= current_sol <= 1.30
        and 0.30 <= first_follow_sol <= 0.80
        and 0.30 <= follow_sol <= 0.80
        and follow_buys == 1
        and 600_000 <= quote_tokens <= 700_000
    )
    if cur1_small_midquote_futureflow_ok:
        return True, "goal5_cur1_small_midquote_futureflow"

    cur1_nearcap_watch_ok = (
        getattr(args, "cur1_nearcap_watch_enabled", False)
        and 0.90 <= current_sol <= 1.30
        and 0.30 <= first_follow_sol <= 0.80
        and 0.30 <= follow_sol <= 0.80
        and follow_buys == 1
        and 700_000 < quote_tokens <= float(getattr(args, "cur1_nearcap_max_quote_tokens", 720_000.0))
    )
    if cur1_nearcap_watch_ok:
        return True, "goal5_cur1_nearcap_watch"

    c0_q500_follow_ok = (
        getattr(args, "c0_q500_follow_enabled", False)
        and 0.45 <= current_sol <= 0.55
        and 0.62 <= first_follow_sol <= 0.70
        and 0.62 <= follow_sol <= 0.70
        and follow_buys == 1
        and 550_000 <= quote_tokens <= 590_000
    )
    if c0_q500_follow_ok:
        return True, "goal5_c0_q500_follow_continuation"

    c0_q900_headroom_ok = (
        getattr(args, "c0_q900_headroom_enabled", False)
        and 0.48 <= current_sol <= 0.52
        and 0.48 <= first_follow_sol <= 0.52
        and 0.48 <= follow_sol <= 0.52
        and follow_buys == 1
        and 930_000 <= quote_tokens <= 1_000_000
    )
    if c0_q900_headroom_ok:
        return True, "goal5_c0_q900_headroom_continuation"

    c0_follow_scratch_ok = (
        getattr(args, "c0_follow_positive_scratch_enabled", False)
        and 0.45 <= current_sol <= 0.55
        and 0.48 <= first_follow_sol <= 0.80
        and 0.48 <= follow_sol <= 0.80
        and follow_buys == 1
        and 500_000 <= quote_tokens <= 1_000_000
    )
    if c0_follow_scratch_ok:
        return True, "goal5_c0_follow_positive_scratch"

    scratch_positive_follow_ok = (
        getattr(args, "scratch_positive_follow_enabled", True)
        and 0.20 <= current_sol <= 3.10
        and 0.25 <= first_follow_sol <= 4.10
        and 0.25 <= follow_sol <= 4.25
        and 1 <= follow_buys <= 2
        and 100_000 <= quote_tokens <= 1_050_000
    )
    if scratch_positive_follow_ok:
        return True, "goal5_scratch_positive_follow"

    c2_micro_q300_ok = (
        getattr(args, "c2_micro_q300_futureflow_enabled", False)
        and 2.50 <= current_sol <= 2.90
        and 0.30 <= first_follow_sol <= 0.36
        and 0.30 <= follow_sol <= 0.36
        and follow_buys == 1
        and 300_000 <= quote_tokens <= 360_000
    )
    if c2_micro_q300_ok:
        return True, "goal5_c2_micro_q300_futureflow"

    if not primary_current:
        return False, "current_band"
    if (
        args.strong_follow_enabled
        and args.strong_current_min_sol <= current_sol <= args.strong_current_max_sol
        and follow_sol >= args.strong_follow_min_sol
    ):
        min_first = min(args.strong_first_follow_min_sol, args.dynamic_first_min_sol, args.flow_first_min_sol)
        max_first = max(args.strong_first_follow_max_sol, args.dynamic_first_max_sol, args.flow_first_max_sol)
        if first_follow_sol < min_first:
            return False, "first_follow_low"
        if first_follow_sol > max_first:
            return False, "first_follow_high"
    min_quote = min(args.quote_min_tokens, args.strong_quote_min_tokens, args.dynamic_quote_min_tokens, args.flow_quote_min_tokens)
    max_quote = max(args.quote_max_tokens, args.strong_quote_max_tokens, args.dynamic_quote_max_tokens, args.flow_quote_max_tokens)
    if quote_tokens < min_quote:
        return False, "quote_low"
    if quote_tokens > max_quote:
        return False, "quote_high"
    if follow_sol < args.follow_min_sol:
        return False, "follow_sol_low"
    if follow_sol > args.follow_max_sol:
        return False, "follow_sol_high"
    if follow_buys < args.follow_min_buys:
        return False, "follow_buys_low"
    if follow_buys > args.follow_max_buys:
        return False, "follow_buys_high"
    return False, "shape_no_match"


def _self_test() -> int:
    ns = argparse.Namespace(
        current_min_sol=2.0,
        current_max_sol=2.35,
        follow_min_sol=1.50,
        follow_max_sol=2.00,
        follow_min_buys=2,
        follow_max_buys=3,
        quote_min_tokens=770000.0,
        quote_max_tokens=800000.0,
        primary_enabled=True,
        strong_follow_enabled=True,
        strong_first_impulse_enabled=True,
        strong_current_min_sol=2.00,
        strong_current_max_sol=2.18,
        strong_first_follow_min_sol=2.00,
        strong_first_follow_max_sol=2.80,
        strong_first_impulse_min_follow_sol=2.00,
        strong_first_impulse_max_follow_sol=2.80,
        strong_first_impulse_max_follow_buys=1,
        strong_first_impulse_min_quote_tokens=615000.0,
        dynamic_continuation_enabled=True,
        dynamic_first_min_sol=1.45,
        dynamic_first_max_sol=2.35,
        dynamic_total_min_sol=2.90,
        dynamic_total_max_sol=4.10,
        dynamic_follow_min_buys=2,
        dynamic_follow_max_buys=3,
        dynamic_quote_min_tokens=560000.0,
        dynamic_quote_max_tokens=700000.0,
        dynamic_score_min=5,
        flow_dominant_enabled=True,
        flow_first_min_sol=1.70,
        flow_first_max_sol=2.25,
        flow_total_min_sol=3.75,
        flow_total_max_sol=4.10,
        flow_follow_min_buys=2,
        flow_follow_max_buys=3,
        flow_quote_min_tokens=220000.0,
        flow_quote_max_tokens=700000.0,
        flow_score_min=5,
        heavy_lowquote_futureflow_enabled=False,
        cur1_small_midquote_futureflow_enabled=False,
        cur1_nearcap_watch_enabled=False,
        cur1_nearcap_max_quote_tokens=720000.0,
        c0_q500_follow_enabled=False,
        c0_q900_headroom_enabled=False,
        c0_follow_positive_scratch_enabled=False,
        one_follow_positive_scratch_enabled=False,
        scratch_positive_follow_enabled=False,
        c2_micro_q300_futureflow_enabled=False,
        strong_follow_min_sol=3.00,
        strong_follow_max_sol=4.25,
        strong_follow_min_buys=2,
        strong_follow_max_buys=4,
        strong_quote_min_tokens=560000.0,
        strong_quote_max_tokens=650000.0,
        strong_first_projection_bypass_enabled=True,
        strong_first_entry_min_projected_headroom_lamports=-700_000,
        c0_q500_projection_bypass_enabled=False,
        c0_q500_entry_min_projected_headroom_lamports=-700_000,
        one_follow_projection_bypass_enabled=True,
        one_follow_entry_min_projected_headroom_lamports=-700_000,
        entry_min_projected_headroom_lamports=700_000,
        sell_min_headroom_lamports=700_000,
    )
    cases = [
        ("9Ugi_win_shape", True, 2.20, 1.925, 2, 779844.906577, 0.750),
        ("45X2_loss_shape", False, 2.75, 1.925, 2, 758055.310003, 0.750),
        ("BJh1_loss_shape", False, 2.20, 1.650, 2, 897663.137939, 0.750),
        ("55dp_strong_follow_shape", True, 2.071137, 3.747192, 2, 621605.343078, 2.100),
        ("D4DY_low_quote_first_impulse_block", False, 2.080602, 2.069956, 1, 605808.435481, 2.069956),
        ("Ftuc_first_impulse_win_shape", True, 2.072295, 2.394274, 1, 618769.244323, 2.394274),
        ("Bi6N_first_impulse_win_shape", True, 2.113845, 2.070786, 1, 628604.750409, 2.070786),
        ("ArVc_first_impulse_loss_shape", False, 2.116277, 2.311386, 1, 609804.139913, 2.311386),
        ("CwiZ_first_impulse_failed_safe_shape", False, 2.200000, 2.156541, 1, 638625.318384, 2.156541),
        ("moderate_first_early_shape", True, 2.000000, 1.120000, 1, 736143.278917, 1.120000),
        ("Ewyd_dynamic_continuation_shape", True, 2.079476, 3.343124, 2, 619509.840262, 1.852560),
        ("21Dx_dynamic_continuation_shape", True, 2.094536, 3.886543, 2, 615708.621400, 1.930098),
        ("6kVz_weak_first_follow_loss_shape", False, 2.108841, 3.351121, 2, 617573.884646, 1.169917),
        ("5SJR_under_total_shape", False, 2.098073, 2.682064, 2, 617604.889107, 1.649795),
        ("CpFu_overheavy_first_follow_shape", False, 2.200000, 3.300000, 2, 135968.251341, 3.000000),
        ("EarU_weak_first_shape", False, 2.049496, 3.231863, 3, 602000.0, 1.218388),
        ("4QwC_quote_high_shape", False, 2.100000, 1.769349, 2, 816340.0, 0.769349),
        ("b6Ui_flow_dominant_shape", True, 2.010597, 3.941958, 3, 246101.175690, 1.779072),
        ("EXUi_flow_dominant_shape", True, 2.016175, 3.829463, 2, 237607.461391, 1.802039),
        ("EXUi_overextended_total_shape", False, 2.016175, 6.127247, 3, 236487.733036, 1.802039),
        ("flow_quote_too_low_shape", False, 2.010000, 3.900000, 2, 180000.0, 1.850000),
    ]
    ok = True
    for name, expected, current, follow, buys, quote, first_follow in cases:
        got, reason = _shape_allowed(ns, current, follow, buys, quote, first_follow)
        ok = ok and got is expected
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
    ns.heavy_lowquote_futureflow_enabled = True
    ns.cur1_small_midquote_futureflow_enabled = True
    ns.cur1_nearcap_watch_enabled = True
    ns.c0_q500_follow_enabled = True
    ns.c0_q900_headroom_enabled = True
    ns.c0_follow_positive_scratch_enabled = True
    for name, expected, current, follow, buys, quote, first_follow in [
        ("heavy_lowquote_futureflow_shape", True, 3.300000, 2.200000, 1, 220178.083941, 2.200000),
        ("heavy_lowquote_futureflow_quote_high", False, 3.300000, 2.200000, 1, 450000.0, 2.200000),
        ("heavy_lowquote_futureflow_first_low", False, 3.300000, 1.500000, 1, 220178.083941, 1.500000),
        ("cur1_small_midquote_futureflow_shape", True, 1.188000, 0.312452, 1, 649569.422610, 0.312452),
        ("cur1_small_midquote_futureflow_quote_high", False, 1.188000, 0.312452, 1, 729000.0, 0.312452),
        ("cur1_small_midquote_futureflow_current_low", False, 0.800000, 0.312452, 1, 649569.422610, 0.312452),
        ("cur1_nearcap_watch_shape", True, 1.042483, 0.452206, 1, 702706.700673, 0.452206),
        ("cur1_nearcap_watch_quote_high", False, 1.042483, 0.452206, 1, 729000.0, 0.452206),
        ("c0_q500_follow_shape", True, 0.500000, 0.651480, 1, 573341.594218, 0.651480),
        ("c0_q500_follow_quote_low_broad_scratch", True, 0.500000, 0.651480, 1, 520000.0, 0.651480),
        ("c0_q500_follow_quote_high_broad_scratch", True, 0.500000, 0.651480, 1, 620000.0, 0.651480),
        ("c0_q500_follow_current_high", False, 0.700000, 0.651480, 1, 573341.594218, 0.651480),
        ("c0_q500_follow_follow_high_broad_scratch", True, 0.500000, 0.790000, 1, 573341.594218, 0.790000),
        ("c0_q900_headroom_shape", True, 0.500000, 0.500000, 1, 976035.220806, 0.500000),
        ("c0_q900_headroom_current_low", False, 0.300000, 0.500000, 1, 976035.220806, 0.500000),
        ("c0_q900_headroom_quote_low_broad_scratch", True, 0.500000, 0.500000, 1, 890000.0, 0.500000),
        ("c0_q900_headroom_follow_low", False, 0.500000, 0.450000, 1, 976035.220806, 0.450000),
        ("c0_follow_positive_scratch_q800_shape", True, 0.500000, 0.651480, 1, 833920.126621, 0.651480),
        ("c0_follow_positive_scratch_quote_low", False, 0.500000, 0.651480, 1, 450000.0, 0.651480),
        ("c0_follow_positive_scratch_current_high", False, 0.700000, 0.651480, 1, 833920.126621, 0.651480),
        ("c0_follow_positive_scratch_follow_low", False, 0.500000, 0.420000, 1, 833920.126621, 0.420000),
    ]:
        got, reason = _shape_allowed(ns, current, follow, buys, quote, first_follow)
        ok = ok and got is expected
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
    ns.heavy_lowquote_futureflow_enabled = False
    ns.cur1_small_midquote_futureflow_enabled = False
    ns.cur1_nearcap_watch_enabled = False
    ns.c0_q500_follow_enabled = False
    ns.c0_q900_headroom_enabled = False
    ns.c0_follow_positive_scratch_enabled = False
    ns.one_follow_positive_scratch_enabled = True
    ns.scratch_positive_follow_enabled = False
    for name, expected, current, follow, buys, quote, first_follow in [
        ("one_follow_positive_scratch_cur1_q500_shape", False, 1.192250, 0.480000, 1, 510561.243866, 0.480000),
        ("one_follow_positive_scratch_low_current_q500_shape", False, 1.000000, 0.402500, 1, 512315.260702, 0.402500),
        ("one_follow_positive_scratch_low_current_q960_shape", False, 0.918000, 0.650000, 1, 960823.691439, 0.650000),
        ("one_follow_positive_scratch_fast_070_shape", False, 0.544500, 0.700000, 1, 495296.388002, 0.700000),
        ("one_follow_positive_scratch_low_current_fast1_shape", False, 0.503448, 1.012367, 1, 566320.966807, 1.012367),
        ("one_follow_positive_scratch_low_current_big_impulse_shape", False, 1.000000, 2.000000, 1, 994401.178769, 2.000000),
        ("one_follow_positive_scratch_cur1_q600_shape", False, 1.200000, 0.579495, 1, 683488.295316, 0.579495),
        ("one_follow_positive_scratch_c0_lowquote_shape", False, 0.505000, 0.600000, 1, 270511.551822, 0.600000),
        ("one_follow_positive_scratch_quote_too_low", False, 1.032750, 0.402500, 1, 169816.429945, 0.402500),
        ("one_follow_positive_scratch_follow_too_low", False, 1.100000, 0.300000, 1, 510000.0, 0.300000),
        ("one_follow_positive_scratch_current_too_high", False, 1.500000, 0.500000, 1, 510000.0, 0.500000),
    ]:
        got, reason = _shape_allowed(ns, current, follow, buys, quote, first_follow)
        ok = ok and got is expected
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
    ns.scratch_positive_follow_enabled = True
    ns.c0_q500_projection_bypass_enabled = False

    def authority_pass(current: float, follow: float, buys: int, quote: float, first_follow: float, projected_headroom: int) -> tuple[bool, str, int]:
        got, reason = _shape_allowed(ns, current, follow, buys, quote, first_follow)
        projection_bypass = (
            (
                reason == "goal5_strong_first_follow_impulse"
                and bool(getattr(ns, "strong_first_projection_bypass_enabled", True))
            )
            or (
                reason == "goal5_c0_q500_follow_continuation"
                and bool(getattr(ns, "c0_q500_projection_bypass_enabled", False))
            )
            or (
                reason == "goal5_one_follow_positive_scratch"
                and bool(getattr(ns, "one_follow_projection_bypass_enabled", True))
            )
        )
        required = int(ns.entry_min_projected_headroom_lamports)
        if reason == "goal5_strong_first_follow_impulse" and projection_bypass:
            required = int(ns.strong_first_entry_min_projected_headroom_lamports)
        elif reason == "goal5_c0_q500_follow_continuation" and projection_bypass:
            required = int(ns.c0_q500_entry_min_projected_headroom_lamports)
        elif reason == "goal5_one_follow_positive_scratch" and projection_bypass:
            required = int(ns.one_follow_entry_min_projected_headroom_lamports)
        if not projection_bypass:
            required = max(required, int(ns.sell_min_headroom_lamports), 0)
        return bool(got and projected_headroom >= required), reason, required

    for name, expected, current, follow, buys, quote, first_follow, projected_headroom in [
        ("authority_Ftuc_live_win", True, 2.072295, 2.394274, 1, 618769.244323, 2.394274, -674064),
        ("authority_Bi6N_live_win", True, 2.113845, 2.070786, 1, 628604.750409, 2.070786, -674064),
        ("authority_ArVc_live_loss_block", False, 2.116277, 2.311386, 1, 609804.139913, 2.311386, -674064),
        ("authority_Gp4F_one_follow_loss_block", False, 0.750000, 0.700000, 1, 810595.922021, 0.700000, -674064),
        ("authority_B41Z_one_follow_loss_block", False, 0.600000, 1.200000, 1, 968219.850457, 1.200000, -674064),
        ("authority_AbWe_scratch_positive_pass", True, 0.500000, 0.500000, 1, 976035.220806, 0.500000, 2085300),
        ("authority_8wK3_scratch_positive_pass", True, 1.189591, 0.480000, 1, 628604.064787, 0.480000, 787101),
    ]:
        got, reason, required = authority_pass(current, follow, buys, quote, first_follow, projected_headroom)
        ok = ok and got is expected
        print(
            f"{name} expected={int(expected)} got={int(got)} reason={reason} "
            f"projected_headroom={projected_headroom} required={required}"
        )
    ns.scratch_positive_follow_enabled = False
    ns.c2_micro_q300_futureflow_enabled = True
    for name, expected, current, follow, buys, quote, first_follow in [
        ("c2_micro_q300_futureflow_shape", True, 2.714000, 0.326000, 1, 335409.0, 0.326000),
        ("c2_micro_q300_futureflow_quote_high", False, 2.714000, 0.326000, 1, 390000.0, 0.326000),
        ("c2_micro_q300_futureflow_follow_high", False, 2.714000, 0.500000, 1, 335409.0, 0.500000),
    ]:
        got, reason = _shape_allowed(ns, current, follow, buys, quote, first_follow)
        ok = ok and got is expected
        print(f"{name} expected={int(expected)} got={int(got)} reason={reason}")
    print("GOAL5_STANDALONE_SELF_TEST_OK" if ok else "GOAL5_STANDALONE_SELF_TEST_FAIL")
    return 0 if ok else 1


def _ready_to_evaluate(args: argparse.Namespace, cand: Candidate) -> bool:
    current_sol = cand.current_lamports / LAMPORTS_PER_SOL
    follow_sol = cand.follow_lamports / LAMPORTS_PER_SOL
    first_follow_sol = cand.first_follow_lamports / LAMPORTS_PER_SOL
    primary_ready = (
        getattr(args, "primary_enabled", True)
        and cand.follow_buys >= args.follow_min_buys
        and follow_sol >= args.follow_min_sol
    )
    strong_first_ready = (
        args.strong_follow_enabled
        and args.strong_first_impulse_enabled
        and cand.follow_buys <= int(args.strong_first_impulse_max_follow_buys)
        and (
            (
                args.strong_first_follow_min_sol
                <= first_follow_sol
                <= args.strong_first_follow_max_sol
                and follow_sol >= args.strong_first_impulse_min_follow_sol
            )
            or (
                cand.follow_buys == 1
                and 0.95 <= first_follow_sol <= 1.25
                and 0.95 <= follow_sol <= 1.30
            )
        )
    )
    strong_total_ready = (
        args.strong_follow_enabled
        and cand.follow_buys >= int(args.strong_follow_min_buys)
        and follow_sol >= args.strong_follow_min_sol
    )
    dynamic_ready = (
        args.dynamic_continuation_enabled
        and cand.follow_buys >= int(args.dynamic_follow_min_buys)
        and follow_sol >= args.dynamic_total_min_sol
    )
    flow_ready = (
        args.flow_dominant_enabled
        and cand.follow_buys >= int(args.flow_follow_min_buys)
        and follow_sol >= args.flow_total_min_sol
    )
    heavy_lowquote_futureflow_ready = (
        getattr(args, "heavy_lowquote_futureflow_enabled", False)
        and 3.00 <= current_sol <= 4.50
        and cand.follow_buys == 1
        and 2.00 <= first_follow_sol <= 3.20
        and 2.00 <= follow_sol <= 3.30
    )
    cur1_small_midquote_futureflow_ready = (
        getattr(args, "cur1_small_midquote_futureflow_enabled", False)
        and 0.90 <= current_sol <= 1.30
        and cand.follow_buys == 1
        and 0.30 <= first_follow_sol <= 0.80
        and 0.30 <= follow_sol <= 0.80
    )
    c0_q500_follow_ready = (
        getattr(args, "c0_q500_follow_enabled", False)
        and 0.45 <= current_sol <= 0.55
        and cand.follow_buys == 1
        and 0.62 <= first_follow_sol <= 0.70
        and 0.62 <= follow_sol <= 0.70
    )
    c0_q900_headroom_ready = (
        getattr(args, "c0_q900_headroom_enabled", False)
        and 0.48 <= current_sol <= 0.52
        and cand.follow_buys == 1
        and 0.48 <= first_follow_sol <= 0.52
        and 0.48 <= follow_sol <= 0.52
    )
    c0_follow_positive_scratch_ready = (
        getattr(args, "c0_follow_positive_scratch_enabled", False)
        and 0.45 <= current_sol <= 0.55
        and cand.follow_buys == 1
        and 0.48 <= first_follow_sol <= 0.80
        and 0.48 <= follow_sol <= 0.80
    )
    scratch_positive_follow_ready = (
        getattr(args, "scratch_positive_follow_enabled", True)
        and 0.20 <= current_sol <= 3.10
        and 1 <= cand.follow_buys <= 2
        and 0.25 <= first_follow_sol <= 4.10
        and 0.25 <= follow_sol <= 4.25
    )
    c2_micro_q300_futureflow_ready = (
        getattr(args, "c2_micro_q300_futureflow_enabled", False)
        and 2.50 <= current_sol <= 2.90
        and cand.follow_buys == 1
        and 0.30 <= first_follow_sol <= 0.36
        and 0.30 <= follow_sol <= 0.36
    )
    return bool(
        primary_ready
        or strong_first_ready
        or strong_total_ready
        or dynamic_ready
        or flow_ready
        or heavy_lowquote_futureflow_ready
        or cur1_small_midquote_futureflow_ready
        or c0_q500_follow_ready
        or c0_q900_headroom_ready
        or c0_follow_positive_scratch_ready
        or scratch_positive_follow_ready
        or c2_micro_q300_futureflow_ready
    )


def _evaluate_candidate(args: argparse.Namespace, broker: Any, cand: Candidate, counters: Counter[str]) -> bool:
    from pgg2_direct_pump import as_pubkey  # type: ignore

    mint = cand.mint
    current_sol = cand.current_lamports / LAMPORTS_PER_SOL
    follow_sol = cand.follow_lamports / LAMPORTS_PER_SOL
    first_follow_sol = cand.first_follow_lamports / LAMPORTS_PER_SOL
    auth_start_ms = _now_ms()
    auth_start_age_ms = auth_start_ms - cand.start_ms
    if cand.follow_buys == 1 and cand.follow_sigs and cand.follow_sigs[0] == cand.start_sig:
        cand.evaluated = True
        counters["authority_block_self_impulse_no_follow"] += 1
        _log(
            "PGG2-GOAL5-AUTHORITY-CHECK "
            f"mint={_short(mint)} full_mint={mint} pass=0 reason=self_impulse_no_follow "
            f"current_sol={current_sol:.6f} follow_sol={follow_sol:.6f} follow_buys={cand.follow_buys} "
            f"first_follow_sol={first_follow_sol:.6f}"
        )
        return False
    _remember_feed_accounts(broker, cand.feed_rec)
    quote_start_ms = _now_ms()
    curve = broker.bonding_curve(as_pubkey(mint))
    global_cfg = broker.pump_global()
    spend_lamports = int(float(args.size_sol) * LAMPORTS_PER_SOL)
    tokens_raw, buy_fee = broker.quote_pump_buy_tokens(spend_lamports, curve, global_cfg)
    quote_tokens = float(broker.raw_to_ui(as_pubkey(mint), int(tokens_raw)))
    quote_latency_ms = _now_ms() - quote_start_ms
    allowed, reason = _shape_allowed(args, current_sol, follow_sol, cand.follow_buys, quote_tokens, first_follow_sol)
    _log(
        "PGG2-GOAL5-AUTHORITY-CHECK "
        f"mint={_short(mint)} full_mint={mint} pass={int(allowed)} reason={reason} "
        f"current_sol={current_sol:.6f} follow_sol={follow_sol:.6f} follow_buys={cand.follow_buys} "
        f"first_follow_sol={first_follow_sol:.6f} quote_tokens={quote_tokens:.6f} buy_fee_lamports={buy_fee} "
        f"auth_start_age_ms={auth_start_age_ms} start_age_ms={_now_ms() - cand.start_ms} "
        f"quote_latency_ms={quote_latency_ms}"
    )
    if not allowed:
        cand.evaluated = True
        counters[f"authority_block_{reason}"] += 1
        return False
    if (
        reason == "goal5_one_follow_positive_scratch"
        and quote_latency_ms > int(args.one_follow_max_quote_latency_ms)
    ):
        counters["authority_block_one_follow_quote_latency"] += 1
        _log(
            "PGG2-GOAL5-ONE-FOLLOW-LATENCY-BLOCK "
            f"mint={_short(mint)} full_mint={mint} quote_latency_ms={quote_latency_ms} "
            f"max_quote_latency_ms={int(args.one_follow_max_quote_latency_ms)} "
            f"current_sol={current_sol:.6f} follow_sol={follow_sol:.6f} quote_tokens={quote_tokens:.6f}"
        )
        return False
    cand.evaluated = True
    projection_bypass = (
        (
            reason == "goal5_strong_first_follow_impulse"
            and bool(getattr(args, "strong_first_projection_bypass_enabled", True))
        )
        or (
            reason == "goal5_c0_q500_follow_continuation"
            and bool(getattr(args, "c0_q500_projection_bypass_enabled", True))
        )
        or (
            reason == "goal5_one_follow_positive_scratch"
            and bool(getattr(args, "one_follow_projection_bypass_enabled", True))
        )
    )
    required_entry_headroom_lamports = int(args.entry_min_projected_headroom_lamports)
    if reason == "goal5_strong_first_follow_impulse" and projection_bypass:
        required_entry_headroom_lamports = int(args.strong_first_entry_min_projected_headroom_lamports)
    elif reason == "goal5_c0_q500_follow_continuation" and projection_bypass:
        required_entry_headroom_lamports = int(args.c0_q500_entry_min_projected_headroom_lamports)
    elif reason == "goal5_one_follow_positive_scratch" and projection_bypass:
        required_entry_headroom_lamports = int(args.one_follow_entry_min_projected_headroom_lamports)
    sentinel_reasons = {
        "goal5_cur1_small_midquote_futureflow",
        "goal5_cur1_nearcap_watch",
        "goal5_c0_q500_follow_continuation",
        "goal5_c0_q900_headroom_continuation",
        "goal5_c0_follow_positive_scratch",
        "goal5_c2_micro_q300_futureflow",
    }
    if reason in sentinel_reasons:
        start_age_ms = auth_start_age_ms
        if start_age_ms > int(args.cur1_max_start_age_ms):
            counters["authority_block_cur1_stale_start"] += 1
            _log(
                "PGG2-GOAL5-FINAL-SENTINEL "
                f"mint={_short(mint)} pass=0 reason=stale_start start_age_ms={start_age_ms} "
                f"authority_reason={reason} "
                f"max_start_age_ms={int(args.cur1_max_start_age_ms)} quote_tokens={quote_tokens:.6f}"
            )
            return False
        sentinel_ms = max(0, int(args.cur1_sentinel_ms))
        if sentinel_ms:
            time.sleep(sentinel_ms / 1000.0)
        sentinel_curve = broker.bonding_curve(as_pubkey(mint))
        sentinel_tokens_raw, sentinel_buy_fee = broker.quote_pump_buy_tokens(spend_lamports, sentinel_curve, global_cfg)
        sentinel_quote_tokens = float(broker.raw_to_ui(as_pubkey(mint), int(sentinel_tokens_raw)))
        quote_drop_pct = 100.0 * (quote_tokens - sentinel_quote_tokens) / max(quote_tokens, 1.0)
        sentinel_allowed, sentinel_reason = _shape_allowed(
            args,
            current_sol,
            follow_sol,
            cand.follow_buys,
            sentinel_quote_tokens,
            first_follow_sol,
        )
        final_reason = reason
        expected_sentinel_reason = reason
        if reason == "goal5_cur1_nearcap_watch":
            expected_sentinel_reason = "goal5_cur1_small_midquote_futureflow"
            final_reason = expected_sentinel_reason
        flat_scratch_path = False
        sentinel_pass = (
            sentinel_allowed
            and sentinel_reason == expected_sentinel_reason
            and quote_drop_pct >= float(args.cur1_min_final_quote_drop_pct)
        )
        if (
            not sentinel_pass
            and reason == "goal5_cur1_small_midquote_futureflow"
            and bool(getattr(args, "cur1_flat_scratch_enabled", True))
            and sentinel_allowed
            and sentinel_reason == expected_sentinel_reason
            and quote_drop_pct >= float(getattr(args, "cur1_flat_min_quote_drop_pct", 0.0))
        ):
            flat_scratch_path = True
            sentinel_pass = True
            required_entry_headroom_lamports = max(
                required_entry_headroom_lamports,
                int(getattr(args, "cur1_flat_min_headroom_lamports", 0)),
            )
        _log(
            "PGG2-GOAL5-FINAL-SENTINEL "
            f"mint={_short(mint)} full_mint={mint} pass={int(sentinel_pass)} "
            f"reason={sentinel_reason if sentinel_allowed else 'sentinel_shape_block'} "
            f"authority_reason={reason} "
            f"flat_scratch_path={int(flat_scratch_path)} "
            f"wait_ms={sentinel_ms} start_age_ms={start_age_ms} "
            f"quote_before={quote_tokens:.6f} quote_after={sentinel_quote_tokens:.6f} "
            f"quote_drop_pct={quote_drop_pct:.4f} min_drop_pct={float(args.cur1_min_final_quote_drop_pct):.4f}"
        )
        if not sentinel_pass:
            counters[f"authority_block_final_sentinel_{reason}"] += 1
            return False
        reason = final_reason
        curve = sentinel_curve
        tokens_raw = sentinel_tokens_raw
        buy_fee = sentinel_buy_fee
        quote_tokens = sentinel_quote_tokens
    post_buy_curve = broker.simulate_post_buy_pump_curve(curve, int(tokens_raw))
    projected_sell_lamports, projected_sell_fee = broker.quote_pump_sell_sol(
        int(tokens_raw),
        post_buy_curve,
        global_cfg,
    )
    projected_cost_lamports = (
        spend_lamports
        + int(args.buy_tx_fee_est_lamports)
        + int(args.sell_fee_est_lamports)
        + int(args.target_lamports)
    )
    projected_headroom = int(projected_sell_lamports) - int(projected_cost_lamports)
    if not projection_bypass:
        required_entry_headroom_lamports = max(
            required_entry_headroom_lamports,
            int(args.sell_min_headroom_lamports),
            0,
        )
    projection_pass = projected_headroom >= required_entry_headroom_lamports
    _log(
        "PGG2-GOAL5-PREBUY-SCRATCH-CHECK "
        f"mint={_short(mint)} full_mint={mint} pass={int(projection_pass)} reason={reason} "
        f"projection_bypass={int(projection_bypass)} "
        f"projected_sell={projected_sell_lamports} projected_cost={projected_cost_lamports} "
        f"projected_headroom={projected_headroom} min_headroom={required_entry_headroom_lamports} "
        f"projected_sell_fee={projected_sell_fee}"
    )
    if not projection_pass:
        counters["authority_block_prebuy_scratch_projection"] += 1
        return False
    min_tokens_ui = quote_tokens * max(0.0, 1.0 - float(args.buy_slippage_pct) / 100.0)
    if not args.live:
        counters["dry_ready"] += 1
        _log(
            "PGG2-GOAL5-DRY-READY "
            f"mint={_short(mint)} size_sol={args.size_sol:.6f} min_tokens={min_tokens_ui:.6f}"
        )
        return True

    wallet_before = _wallet_lamports(broker, "processed")
    buy_quote = broker.build_buy_with_min_tokens_from_curve_snapshot(
        mint,
        float(args.size_sol),
        min_tokens_ui,
        virtual_token_reserves=int(curve.virtual_token_reserves),
        virtual_sol_reserves=int(curve.virtual_sol_reserves),
        real_token_reserves=int(curve.real_token_reserves),
        real_sol_reserves=int(curve.real_sol_reserves),
        token_total_supply=int(curve.token_total_supply),
        complete=bool(curve.complete),
        creator=str(curve.creator),
        is_mayhem=bool(getattr(curve, "is_mayhem", False)),
        cashback_enabled=bool(getattr(curve, "cashback_enabled", False)),
        snapshot_ts_ms=_now_ms(),
    )
    signed_b64 = str(buy_quote.get("txn") or "")
    if not signed_b64:
        raise RuntimeError("goal5_buy_tx_missing")
    _log(
        "PGG2-GOAL5-BUY-SEND "
        f"mint={_short(mint)} wallet_before={wallet_before} size_sol={args.size_sol:.6f} "
        f"quote_tokens={quote_tokens:.6f} min_tokens={min_tokens_ui:.6f}"
    )
    buy_sent_ms = _now_ms()
    buy_sig = broker.send_signed(signed_b64)
    token_raw = _wait_token_balance(broker, mint, float(args.token_wait_sec), int(args.token_poll_ms))
    _log(
        "PGG2-GOAL5-EARLY-TOKEN "
        f"mint={_short(mint)} token_raw={token_raw} buy_sig={buy_sig} "
        f"wait_ms={_now_ms() - buy_sent_ms}"
    )
    buy_ok = False
    if token_raw > 0:
        _log(f"PGG2-GOAL5-BUY-TOKEN-FIRST mint={_short(mint)} action=sell_without_confirm_wait")
    else:
        _log(
            "PGG2-GOAL5-BUY-TOKEN-POLL-EXTEND "
            f"mint={_short(mint)} action=no_confirm_wait wait_sec={float(args.token_late_wait_sec):.3f}"
        )
    if token_raw <= 0:
        token_raw = _wait_token_balance(broker, mint, float(args.token_late_wait_sec), int(args.token_poll_ms))
    if token_raw <= 0:
        try:
            res = broker.rpc("getSignatureStatuses", [[buy_sig], {"searchTransactionHistory": False}])
            status = ((res or {}).get("value") or [None])[0]
            buy_ok = bool(status and not status.get("err"))
        except Exception:
            buy_ok = False
        _log(f"PGG2-GOAL5-BUY-FAILED-SAFE mint={_short(mint)} sig={buy_sig} buy_confirmed={int(buy_ok)}")
        counters["buy_failed_safe"] += 1
        return False

    sell_sig = ""
    expected_out = 0
    min_needed = 0
    sell_attempts = 0
    closed = False
    deadline = time.time() + max(0.5, float(args.max_hold_ms) / 1000.0)
    while True:
        try:
            refreshed_token_raw = _token_balance_raw_or_zero(broker, mint, "processed")
            if refreshed_token_raw > 0:
                token_raw = refreshed_token_raw
            elif sell_sig:
                closed = True
                break
        except Exception as exc:
            _log(f"PGG2-GOAL5-TOKEN-READ-WARN mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:160]}")
        wallet_now = _wallet_lamports(broker, "processed")
        rent_lamports = _token_account_lamports(broker, mint, "processed") or ATA_RENT_LAMPORTS
        min_needed = max(
            int(args.sell_floor_lamports),
            wallet_before + int(args.target_lamports) + int(args.sell_fee_est_lamports) - wallet_now - rent_lamports,
        )
        token_ui = broker.raw_to_ui(as_pubkey(mint), int(token_raw))
        quote = broker.build_sell(mint, token_ui, float(args.sell_slippage_pct))
        expected_out = int(float((quote.get("rate") or {}).get("amountOut") or 0.0) * LAMPORTS_PER_SOL)
        _log(
            "PGG2-GOAL5-SELL-CHECK "
            f"mint={_short(mint)} token_raw={token_raw} expected_out={expected_out} "
            f"min_needed={min_needed} headroom={expected_out - min_needed} "
            f"wallet_now={wallet_now} rent={rent_lamports}"
        )
        if expected_out >= min_needed:
            headroom = expected_out - min_needed
            min_headroom = int(args.sell_min_headroom_lamports)
            if headroom < min_headroom and time.time() < deadline:
                counters["sell_headroom_wait"] += 1
                _log(
                    "PGG2-GOAL5-SELL-HEADROOM-WAIT "
                    f"mint={_short(mint)} expected_out={expected_out} min_needed={min_needed} "
                    f"headroom={headroom} min_headroom={min_headroom}"
                )
                time.sleep(max(0.025, float(args.sell_poll_ms) / 1000.0))
                continue
            protected = broker.retarget_sell_min_sol(quote, mint, min_needed / LAMPORTS_PER_SOL)
            sell_sig = broker.send_signed(str(protected["txn"]))
            sell_attempts += 1
            sell_ok = broker.wait_confirmed(sell_sig)
            time.sleep(0.35)
            token_after = _token_balance_raw_or_zero(broker, mint, "processed")
            _log(
                "PGG2-GOAL5-SELL-RESULT "
                f"mint={_short(mint)} mode=scratch attempt={sell_attempts} sig={sell_sig} "
                f"ok={int(bool(sell_ok))} token_raw_after={token_after}"
            )
            if sell_ok and token_after <= 0:
                closed = True
                break
            if token_after > 0:
                token_raw = token_after
            counters["sell_failed_retry"] += 1
            if sell_attempts >= int(args.sell_max_attempts):
                _log(
                    "PGG2-GOAL5-SELL-FAILED-OPEN-TOKEN "
                    f"mint={_short(mint)} mode=scratch attempts={sell_attempts} token_raw={token_raw}"
                )
                raise RuntimeError("goal5_sell_failed_open_token")
            continue
        headroom = expected_out - min_needed
        if (
            args.rescue_negative_headroom_immediate
            and args.rescue_at_loss
            and headroom <= _branch_rescue_negative_headroom_lamports(args, reason)
        ):
            rescue_trigger = _branch_rescue_negative_headroom_lamports(args, reason)
            rescue_min = max(
                int(args.sell_floor_lamports),
                int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL),
            )
            counters["loss_rescue_negative_headroom_immediate"] += 1
            _log(
                "PGG2-GOAL5-LOSS-RESCUE-IMMEDIATE "
                f"mint={_short(mint)} expected_out={expected_out} min_needed={min_needed} "
                f"headroom={headroom} trigger={rescue_trigger} rescue_min={rescue_min}"
            )
            protected = broker.retarget_sell_min_sol(quote, mint, rescue_min / LAMPORTS_PER_SOL)
            sell_sig = broker.send_signed(str(protected["txn"]))
            sell_attempts += 1
            sell_ok = broker.wait_confirmed(sell_sig)
            time.sleep(0.35)
            token_after = _token_balance_raw_or_zero(broker, mint, "processed")
            _log(
                "PGG2-GOAL5-SELL-RESULT "
                f"mint={_short(mint)} mode=immediate_rescue attempt={sell_attempts} sig={sell_sig} "
                f"ok={int(bool(sell_ok))} token_raw_after={token_after}"
            )
            if sell_ok and token_after <= 0:
                closed = True
                break
            if token_after > 0:
                token_raw = token_after
            counters["sell_failed_retry"] += 1
            if sell_attempts >= int(args.sell_max_attempts):
                _log(
                    "PGG2-GOAL5-SELL-FAILED-OPEN-TOKEN "
                    f"mint={_short(mint)} mode=immediate_rescue attempts={sell_attempts} token_raw={token_raw}"
                )
                raise RuntimeError("goal5_sell_failed_open_token")
            continue
        if time.time() >= deadline:
            if not args.rescue_at_loss:
                _log(
                    "PGG2-GOAL5-NO-SCRATCH-HOLD "
                    f"mint={_short(mint)} expected_out={expected_out} min_needed={min_needed} action=no_loss_rescue_disabled"
                )
                counters["no_scratch_no_rescue"] += 1
                return False
            rescue_min = max(int(args.sell_floor_lamports), int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL))
            _log(
                "PGG2-GOAL5-LOSS-RESCUE "
                f"mint={_short(mint)} expected_out={expected_out} min_needed={min_needed} rescue_min={rescue_min}"
            )
            protected = broker.retarget_sell_min_sol(quote, mint, rescue_min / LAMPORTS_PER_SOL)
            sell_sig = broker.send_signed(str(protected["txn"]))
            sell_attempts += 1
            sell_ok = broker.wait_confirmed(sell_sig)
            time.sleep(0.35)
            token_after = _token_balance_raw_or_zero(broker, mint, "processed")
            _log(
                "PGG2-GOAL5-SELL-RESULT "
                f"mint={_short(mint)} mode=rescue attempt={sell_attempts} sig={sell_sig} "
                f"ok={int(bool(sell_ok))} token_raw_after={token_after}"
            )
            if sell_ok and token_after <= 0:
                closed = True
                break
            if token_after > 0:
                token_raw = token_after
            counters["sell_failed_retry"] += 1
            if sell_attempts >= int(args.sell_max_attempts):
                _log(
                    "PGG2-GOAL5-SELL-FAILED-OPEN-TOKEN "
                    f"mint={_short(mint)} mode=rescue attempts={sell_attempts} token_raw={token_raw}"
                )
                raise RuntimeError("goal5_sell_failed_open_token")
            continue
        time.sleep(max(0.025, float(args.sell_poll_ms) / 1000.0))

    time.sleep(1.0)
    final_wallet = _wallet_lamports(broker, "processed")
    final_token_raw = _token_balance_raw_or_zero(broker, mint, "processed")
    nonzero, rent_locked = _token_accounts_summary(broker)
    delta = final_wallet - wallet_before
    _log(
        "PGG2-GOAL5-SMOKE-END "
        f"mint={_short(mint)} wallet_before={wallet_before} wallet_after={final_wallet} "
        f"delta_lamports={delta:+} buy_sig={buy_sig} sell_sig={sell_sig} "
        f"expected_out={expected_out} min_needed={min_needed} closed={int(closed and final_token_raw <= 0)} "
        f"token_raw_after={final_token_raw} nonzero_tokens={nonzero} rent_locked_empty={rent_locked}"
    )
    if final_token_raw > 0:
        counters["open_token_after_sell"] += 1
        raise RuntimeError("goal5_final_token_residual")
    counters["closed"] += 1
    if delta > 0:
        counters["win"] += 1
    else:
        counters["loss"] += 1
    return delta > 0


def run(args: argparse.Namespace) -> int:
    grpc, _Pubkey, _Signature, protos = _proto_imports()
    _geyser_pb2, geyser_pb2_grpc = protos
    token = os.environ.get(str(args.token_env), "")
    if not token:
        _log(f"PGG2-GOAL5-FATAL missing_token_env={args.token_env}")
        return 2
    broker = _make_broker(args)
    try:
        broker.refresh_blockhash_cache()
    except Exception as exc:
        _log(f"PGG2-GOAL5-BLOCKHASH-WARN err={type(exc).__name__}:{exc}")
    if args.live:
        nonzero, rent_locked = _token_accounts_summary(broker)
        wallet = _wallet_lamports(broker, "processed")
        _log(f"PGG2-GOAL5-STATE wallet_sol={wallet/LAMPORTS_PER_SOL:.9f} nonzero_tokens={nonzero} rent_locked_empty={rent_locked}")
        if nonzero:
            _log("PGG2-GOAL5-ABORT reason=nonzero_token_accounts")
            return 2

    channel = grpc.secure_channel(str(args.endpoint), grpc.ssl_channel_credentials())
    stub = geyser_pb2_grpc.GeyserStub(channel)
    metadata = [(str(args.metadata_key), token)]
    hist: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=128))
    seen: set[str] = set()
    counters: Counter[str] = Counter()
    started = time.time()
    _log(
        "PGG2-GOAL5-START "
        f"live={int(args.live)} seconds={args.seconds} size_sol={args.size_sol:.6f} "
        f"primary={int(args.primary_enabled)} "
        f"current=[{args.current_min_sol:.2f},{args.current_max_sol:.2f}] "
        f"follow=[{args.follow_min_sol:.2f},{args.follow_max_sol:.2f}] "
        f"quote=[{args.quote_min_tokens:.0f},{args.quote_max_tokens:.0f}] "
        f"strong_follow={int(args.strong_follow_enabled)} "
        f"strong_current=[{args.strong_current_min_sol:.2f},{args.strong_current_max_sol:.2f}] "
        f"strong_first=[{args.strong_first_follow_min_sol:.2f},{args.strong_first_follow_max_sol:.2f}] "
        f"strong_follow_sol=[{args.strong_follow_min_sol:.2f},{args.strong_follow_max_sol:.2f}] "
        f"strong_first_quote_min={args.strong_first_impulse_min_quote_tokens:.0f} "
        f"strong_quote=[{args.strong_quote_min_tokens:.0f},{args.strong_quote_max_tokens:.0f}] "
        f"strong_first_projection_bypass={int(args.strong_first_projection_bypass_enabled)} "
        f"strong_first_entry_min_headroom={args.strong_first_entry_min_projected_headroom_lamports} "
        f"dynamic={int(args.dynamic_continuation_enabled)} "
        f"dynamic_first=[{args.dynamic_first_min_sol:.2f},{args.dynamic_first_max_sol:.2f}] "
        f"dynamic_total=[{args.dynamic_total_min_sol:.2f},{args.dynamic_total_max_sol:.2f}] "
        f"dynamic_buys=[{args.dynamic_follow_min_buys},{args.dynamic_follow_max_buys}] "
        f"dynamic_quote=[{args.dynamic_quote_min_tokens:.0f},{args.dynamic_quote_max_tokens:.0f}] "
        f"dynamic_score_min={args.dynamic_score_min} "
        f"flow_dominant={int(args.flow_dominant_enabled)} "
        f"flow_first=[{args.flow_first_min_sol:.2f},{args.flow_first_max_sol:.2f}] "
        f"flow_total=[{args.flow_total_min_sol:.2f},{args.flow_total_max_sol:.2f}] "
        f"flow_buys=[{args.flow_follow_min_buys},{args.flow_follow_max_buys}] "
        f"flow_quote=[{args.flow_quote_min_tokens:.0f},{args.flow_quote_max_tokens:.0f}] "
        f"flow_score_min={args.flow_score_min} "
        f"heavy_lowquote_futureflow={int(args.heavy_lowquote_futureflow_enabled)} "
        f"cur1_small_midquote_futureflow={int(args.cur1_small_midquote_futureflow_enabled)} "
        f"cur1_nearcap_watch={int(args.cur1_nearcap_watch_enabled)} "
        f"cur1_nearcap_max_quote_tokens={args.cur1_nearcap_max_quote_tokens:.0f} "
        f"c0_q500_follow={int(args.c0_q500_follow_enabled)} "
        f"c0_q900_headroom={int(args.c0_q900_headroom_enabled)} "
        f"c0_follow_positive_scratch={int(args.c0_follow_positive_scratch_enabled)} "
        f"one_follow_positive_scratch={int(args.one_follow_positive_scratch_enabled)} "
        f"scratch_positive_follow={int(args.scratch_positive_follow_enabled)} "
        f"c2_micro_q300_futureflow={int(args.c2_micro_q300_futureflow_enabled)} "
        f"cur1_max_start_age_ms={args.cur1_max_start_age_ms} "
        f"cur1_sentinel_ms={args.cur1_sentinel_ms} "
        f"cur1_min_final_quote_drop_pct={args.cur1_min_final_quote_drop_pct:.4f} "
        f"cur1_flat_scratch={int(args.cur1_flat_scratch_enabled)} "
        f"cur1_flat_min_quote_drop_pct={args.cur1_flat_min_quote_drop_pct:.4f} "
        f"cur1_flat_min_headroom_lamports={args.cur1_flat_min_headroom_lamports} "
        f"c0_q500_projection_bypass={int(args.c0_q500_projection_bypass_enabled)} "
        f"c0_q500_entry_min_headroom={args.c0_q500_entry_min_projected_headroom_lamports} "
        f"c0_q500_rescue_negative_headroom_lamports={args.c0_q500_rescue_negative_headroom_lamports} "
        f"one_follow_projection_bypass={int(args.one_follow_projection_bypass_enabled)} "
        f"one_follow_entry_min_headroom={args.one_follow_entry_min_projected_headroom_lamports} "
        f"one_follow_rescue_negative_headroom_lamports={args.one_follow_rescue_negative_headroom_lamports} "
        f"one_follow_max_quote_latency_ms={args.one_follow_max_quote_latency_ms} "
        f"entry_min_projected_headroom_lamports={args.entry_min_projected_headroom_lamports} "
        f"buy_tx_fee_est_lamports={args.buy_tx_fee_est_lamports} "
        f"sell_min_headroom_lamports={args.sell_min_headroom_lamports} "
        f"token_wait_sec={args.token_wait_sec:.3f} "
        f"token_late_wait_sec={args.token_late_wait_sec:.3f} "
        f"token_poll_ms={args.token_poll_ms} "
        f"rescue_negative_headroom_immediate={int(args.rescue_negative_headroom_immediate)} "
        f"rescue_negative_headroom_lamports={args.rescue_negative_headroom_lamports}"
    )
    try:
        for update in stub.Subscribe(_request_iter(args), metadata=metadata):
            now = _now_ms()
            if time.time() - started > int(args.seconds):
                counters["timeout"] += 1
                break
            rec = _decode_pump(update)
            if not rec:
                counters["non_pump"] += 1
                continue
            sig = str(rec["sig"])
            if sig in seen:
                counters["duplicate"] += 1
                continue
            seen.add(sig)
            mint = str(rec["mint"])
            _remember_feed_accounts(broker, rec)

            prior = list(hist[mint])
            hist[mint].append(rec)
            if rec["kind"] != "buy":
                continue

            window_ms = int(args.max_rearm_age_ms)
            recent = [x for x in prior if now - int(x["recv_ms"]) <= window_ms]
            start_buys = [x for x in recent if x["kind"] == "buy"]
            ready_candidates: list[Candidate] = []
            block_reason = "no_start"

            for start in reversed(start_buys):
                start_ms = int(start["recv_ms"])
                start_sol = int(start["sol_lamports"]) / LAMPORTS_PER_SOL
                heavy_lowquote_start_context = (
                    getattr(args, "heavy_lowquote_futureflow_enabled", False)
                    and 3.00 <= start_sol <= 4.50
                )
                cur1_small_midquote_start_context = (
                    getattr(args, "cur1_small_midquote_futureflow_enabled", False)
                    and 0.90 <= start_sol <= 1.30
                )
                c0_q500_follow_start_context = (
                    getattr(args, "c0_q500_follow_enabled", False)
                    and 0.45 <= start_sol <= 0.55
                )
                c0_q900_headroom_start_context = (
                    getattr(args, "c0_q900_headroom_enabled", False)
                    and 0.48 <= start_sol <= 0.52
                )
                c0_follow_positive_scratch_start_context = (
                    getattr(args, "c0_follow_positive_scratch_enabled", False)
                    and 0.45 <= start_sol <= 0.55
                )
                scratch_positive_follow_start_context = (
                    getattr(args, "scratch_positive_follow_enabled", True)
                    and 0.20 <= start_sol <= 3.10
                )
                c2_micro_q300_start_context = (
                    getattr(args, "c2_micro_q300_futureflow_enabled", False)
                    and 2.50 <= start_sol <= 2.90
                )
                if not (
                    args.current_min_sol <= start_sol <= args.current_max_sol
                    or heavy_lowquote_start_context
                    or cur1_small_midquote_start_context
                    or c0_q500_follow_start_context
                    or c0_q900_headroom_start_context
                    or c0_follow_positive_scratch_start_context
                    or scratch_positive_follow_start_context
                    or c2_micro_q300_start_context
                ):
                    block_reason = "current"
                    continue

                prior_to_start = [
                    x for x in prior
                    if int(x["recv_ms"]) < start_ms and start_ms - int(x["recv_ms"]) <= 1000
                ]
                prev_buys = [x for x in prior_to_start if x["kind"] == "buy"]
                prev_sells = [x for x in prior_to_start if x["kind"] == "sell"]
                prev_buy_sol = sum(int(x["sol_lamports"]) for x in prev_buys) / LAMPORTS_PER_SOL
                if prev_sells:
                    block_reason = "prev_sell"
                    continue
                if (
                    not heavy_lowquote_start_context
                    and not cur1_small_midquote_start_context
                    and not c0_q500_follow_start_context
                    and not c0_q900_headroom_start_context
                    and not c0_follow_positive_scratch_start_context
                    and not scratch_positive_follow_start_context
                    and not c2_micro_q300_start_context
                    and (len(prev_buys) > int(args.max_prev_buys_1s) or prev_buy_sol > float(args.max_prev_buy_sol_1s))
                ):
                    block_reason = "prev_flow"
                    continue

                after_start = [x for x in prior if int(x["recv_ms"]) > start_ms and now - int(x["recv_ms"]) <= window_ms]
                if any(x["kind"] == "sell" for x in after_start):
                    block_reason = "sell_between"
                    continue

                follow_buys = [x for x in after_start if x["kind"] == "buy"] + [rec]
                if not follow_buys:
                    block_reason = "no_follow"
                    continue
                cand = Candidate(
                    mint=mint,
                    start_ms=start_ms,
                    current_lamports=int(start["sol_lamports"]),
                    start_sig=str(start["sig"]),
                    feed_rec=dict(start),
                    follow_lamports=sum(int(x["sol_lamports"]) for x in follow_buys),
                    follow_buys=len(follow_buys),
                    first_follow_lamports=int(follow_buys[0]["sol_lamports"]),
                    follow_sigs=[str(x["sig"]) for x in follow_buys],
                )
                if _ready_to_evaluate(args, cand):
                    ready_candidates.append(cand)
                    continue
                block_reason = "not_ready"

            if not ready_candidates:
                counters[f"sliding_block_{block_reason}"] += 1
                continue

            ok = False
            for selected in ready_candidates:
                counters["sliding_candidate"] += 1
                _log(
                    "PGG2-GOAL5-SLIDING-CANDIDATE "
                    f"mint={_short(mint)} full_mint={mint} start_age_ms={now-selected.start_ms} "
                    f"current_sol={selected.current_lamports/LAMPORTS_PER_SOL:.6f} "
                    f"follow_buys={selected.follow_buys} first_follow_sol={selected.first_follow_lamports/LAMPORTS_PER_SOL:.6f} "
                    f"follow_sol={selected.follow_lamports/LAMPORTS_PER_SOL:.6f} sig={sig}"
                )
                before_closed = counters["closed"]
                before_buy_failed = counters["buy_failed_safe"]
                ok = _evaluate_candidate(args, broker, selected, counters)
                live_attempted = (
                    counters["closed"] != before_closed
                    or counters["buy_failed_safe"] != before_buy_failed
                )
                if ok or live_attempted:
                    break
            if args.live and counters["buy_failed_safe"] > 0:
                break
            if args.target_closes > 0 and counters["closed"] >= int(args.target_closes):
                break
            if (not args.live) and ok and args.stop_on_dry_ready:
                break
    except grpc.RpcError as exc:
        counters["grpc_error"] += 1
        _log(f"PGG2-GOAL5-GRPC-ERROR code={exc.code()} details={str(exc.details())[:240]}")
    finally:
        _log("PGG2-GOAL5-FINAL " + " ".join(f"{k}={v}" for k, v in counters.most_common(60)))
    if args.live:
        return 0 if counters["win"] > 0 and counters["loss"] == 0 else 1
    return 0 if counters["dry_ready"] > 0 else 1


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--endpoint", default=os.environ.get("PUBLICNODE_YELLOWSTONE_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"))
    ap.add_argument("--token-env", default=os.environ.get("PUBLICNODE_TOKEN_ENV", "PUBLICNODE_X_TOKEN"))
    ap.add_argument("--metadata-key", default=os.environ.get("PUBLICNODE_GRPC_METADATA_KEY", "x-token"))
    ap.add_argument("--ping-seconds", type=int, default=15)
    ap.add_argument("--rpc-url", default=_default_rpc_url())
    ap.add_argument("--size-sol", type=float, default=0.030)
    ap.add_argument("--tip-lamports", type=int, default=5000)
    ap.add_argument("--priority-fee-sol", type=float, default=0.000005)
    ap.add_argument("--primary-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--current-min-sol", type=float, default=2.00)
    ap.add_argument("--current-max-sol", type=float, default=2.35)
    ap.add_argument("--follow-min-sol", type=float, default=1.50)
    ap.add_argument("--follow-max-sol", type=float, default=2.00)
    ap.add_argument("--follow-min-buys", type=int, default=2)
    ap.add_argument("--follow-max-buys", type=int, default=3)
    ap.add_argument("--max-rearm-age-ms", type=int, default=950)
    ap.add_argument("--max-prev-buys-1s", type=int, default=1)
    ap.add_argument("--max-prev-buy-sol-1s", type=float, default=0.30)
    ap.add_argument("--quote-min-tokens", type=float, default=770000.0)
    ap.add_argument("--quote-max-tokens", type=float, default=800000.0)
    ap.add_argument("--strong-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--strong-first-impulse-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--strong-current-min-sol", type=float, default=2.00)
    ap.add_argument("--strong-current-max-sol", type=float, default=2.18)
    ap.add_argument("--strong-first-follow-min-sol", type=float, default=2.00)
    ap.add_argument("--strong-first-follow-max-sol", type=float, default=2.80)
    ap.add_argument("--strong-first-impulse-min-follow-sol", type=float, default=2.00)
    ap.add_argument("--strong-first-impulse-max-follow-sol", type=float, default=2.80)
    ap.add_argument("--strong-first-impulse-max-follow-buys", type=int, default=1)
    ap.add_argument("--strong-first-impulse-min-quote-tokens", type=float, default=615000.0)
    ap.add_argument("--dynamic-continuation-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--dynamic-first-min-sol", type=float, default=1.45)
    ap.add_argument("--dynamic-first-max-sol", type=float, default=2.35)
    ap.add_argument("--dynamic-total-min-sol", type=float, default=2.90)
    ap.add_argument("--dynamic-total-max-sol", type=float, default=4.10)
    ap.add_argument("--dynamic-follow-min-buys", type=int, default=2)
    ap.add_argument("--dynamic-follow-max-buys", type=int, default=3)
    ap.add_argument("--dynamic-quote-min-tokens", type=float, default=560000.0)
    ap.add_argument("--dynamic-quote-max-tokens", type=float, default=700000.0)
    ap.add_argument("--dynamic-score-min", type=int, default=5)
    ap.add_argument("--flow-dominant-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--flow-first-min-sol", type=float, default=1.70)
    ap.add_argument("--flow-first-max-sol", type=float, default=2.25)
    ap.add_argument("--flow-total-min-sol", type=float, default=3.75)
    ap.add_argument("--flow-total-max-sol", type=float, default=4.10)
    ap.add_argument("--flow-follow-min-buys", type=int, default=2)
    ap.add_argument("--flow-follow-max-buys", type=int, default=3)
    ap.add_argument("--flow-quote-min-tokens", type=float, default=220000.0)
    ap.add_argument("--flow-quote-max-tokens", type=float, default=700000.0)
    ap.add_argument("--flow-score-min", type=int, default=5)
    ap.add_argument("--heavy-lowquote-futureflow-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--cur1-small-midquote-futureflow-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--cur1-nearcap-watch-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--cur1-nearcap-max-quote-tokens", type=float, default=720000.0)
    ap.add_argument("--c0-q500-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--c0-q500-projection-bypass-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--c0-q500-entry-min-projected-headroom-lamports", type=int, default=-700_000)
    ap.add_argument("--c0-q500-rescue-negative-headroom-lamports", type=int, default=0)
    ap.add_argument("--c0-q900-headroom-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--c0-follow-positive-scratch-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--one-follow-positive-scratch-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--one-follow-projection-bypass-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--one-follow-entry-min-projected-headroom-lamports", type=int, default=-700_000)
    ap.add_argument("--one-follow-rescue-negative-headroom-lamports", type=int, default=-900_000)
    ap.add_argument("--one-follow-max-quote-latency-ms", type=int, default=180)
    ap.add_argument("--scratch-positive-follow-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--c2-micro-q300-futureflow-enabled", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--cur1-max-start-age-ms", type=int, default=250)
    ap.add_argument("--cur1-sentinel-ms", type=int, default=120)
    ap.add_argument("--cur1-min-final-quote-drop-pct", type=float, default=0.10)
    ap.add_argument("--cur1-flat-scratch-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--cur1-flat-min-quote-drop-pct", type=float, default=0.0)
    ap.add_argument("--cur1-flat-min-headroom-lamports", type=int, default=700_000)
    ap.add_argument("--strong-follow-min-sol", type=float, default=3.00)
    ap.add_argument("--strong-follow-max-sol", type=float, default=4.25)
    ap.add_argument("--strong-follow-min-buys", type=int, default=2)
    ap.add_argument("--strong-follow-max-buys", type=int, default=4)
    ap.add_argument("--strong-quote-min-tokens", type=float, default=560000.0)
    ap.add_argument("--strong-quote-max-tokens", type=float, default=650000.0)
    ap.add_argument("--strong-first-projection-bypass-enabled", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--strong-first-entry-min-projected-headroom-lamports", type=int, default=-700_000)
    ap.add_argument("--buy-slippage-pct", type=float, default=8.00)
    ap.add_argument("--sell-slippage-pct", type=float, default=4.00)
    ap.add_argument("--target-lamports", type=int, default=0)
    ap.add_argument("--buy-tx-fee-est-lamports", type=int, default=50_000)
    ap.add_argument("--sell-fee-est-lamports", type=int, default=30_000)
    ap.add_argument("--entry-min-projected-headroom-lamports", type=int, default=700_000)
    ap.add_argument("--sell-min-headroom-lamports", type=int, default=700_000)
    ap.add_argument("--sell-floor-lamports", type=int, default=100)
    ap.add_argument("--max-hold-ms", type=int, default=6500)
    ap.add_argument("--sell-poll-ms", type=int, default=200)
    ap.add_argument("--sell-max-attempts", type=int, default=3)
    ap.add_argument("--rescue-negative-headroom-immediate", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--rescue-negative-headroom-lamports", type=int, default=0)
    ap.add_argument("--token-wait-sec", type=float, default=0.60)
    ap.add_argument("--token-late-wait-sec", type=float, default=1.25)
    ap.add_argument("--token-poll-ms", type=int, default=20)
    ap.add_argument("--target-closes", type=int, default=1)
    ap.add_argument("--stop-on-dry-ready", action="store_true")
    ap.add_argument("--rescue-at-loss", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
