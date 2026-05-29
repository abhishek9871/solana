#!/usr/bin/env python3
"""V287 selected-band live smoke.

One-entry live smoke for the V286 calibrated continuation band. This is not a
general bot: it uses the PublicNode Yellowstone feed, waits for the exact
selected-band setup, sends one small guarded pump buy through Helius Sender,
then exits on a proportional profit target or bounded timeout.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from collections import Counter, defaultdict, deque
from typing import Any, Iterator

import grpc  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.signature import Signature  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore

ROOT = "/root/piggy"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PROTO_DIR = os.path.join(ROOT, "yellowstone_proto")
if PROTO_DIR not in sys.path:
    sys.path.insert(0, PROTO_DIR)

import geyser_pb2  # type: ignore  # noqa: E402
import geyser_pb2_grpc  # type: ignore  # noqa: E402

from birth_first_sniper import BotConfig  # type: ignore  # noqa: E402
from pgg2_direct_pump import (  # type: ignore  # noqa: E402
    DirectPumpQuoteBroker,
    KNOWN_PUMP_SOCIAL_FEE_PDAS,
    PUMP_FEE_PROGRAM_ID,
    PUMP_PROGRAM_ID,
    PumpBuybackPair,
    TOKEN_PROGRAM_ID as DIRECT_TOKEN_PROGRAM_ID,
    as_pubkey,
    get_associated_token_address,
    pda,
)
from pgg2_v74_sender_adapter import install_into_broker, make_sender  # type: ignore  # noqa: E402
from pgg2_v75_sender_tx_builder import make_tip_builder  # type: ignore  # noqa: E402
from pgg2_v285_grpc_buy_train_continuation_no_send import (  # type: ignore  # noqa: E402
    DISC_BUY,
    DISC_BUY_EXACT_SOL_IN,
    DISC_SELL,
    LAMPORTS_PER_SOL,
    PUMP_PROGRAM,
    _request_iter,
    _short,
)

ATA_RENT_LAMPORTS = 2_039_280
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WALLET = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _pubkey(raw: bytes) -> str:
    try:
        return str(Pubkey.from_bytes(raw))
    except Exception:
        return ""


def _rate_float(quote: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        rate = quote.get("rate") or {}
        return float(rate.get(key, default) or default)
    except Exception:
        return default


def _txn_account_keys(txn_b64: str) -> set[str]:
    tx = VersionedTransaction.from_bytes(base64.b64decode(str(txn_b64)))
    return {str(k) for k in tx.message.account_keys}


def _validate_buy_creator_vault(broker: Any, mint: str, txn_b64: str) -> bool:
    """Fail closed before send if the buy transaction was built with a stale creator vault."""
    try:
        curve = broker.bonding_curve(as_pubkey(mint))
        return _validate_buy_creator_vault_from_creator(mint, txn_b64, curve.creator)
    except Exception as exc:
        _log(
            "PGG2-V287-CREATOR-VAULT-CHECK-FAIL "
            f"mint={_short(mint)} full_mint={mint} err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _validate_buy_creator_vault_from_creator(mint: str, txn_b64: str, creator: Any) -> bool:
    """Validate creator-vault membership without a second curve RPC read."""
    try:
        creator_pk = as_pubkey(str(creator))
        expected = str(pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(creator_pk)))
        keys = _txn_account_keys(txn_b64)
        ok = expected in keys
        _log(
            "PGG2-V287-CREATOR-VAULT-CHECK "
            f"mint={_short(mint)} full_mint={mint} creator={creator_pk} "
            f"expected={expected} present={int(ok)} account_keys={len(keys)} source=cached_curve"
        )
        return ok
    except Exception as exc:
        _log(
            "PGG2-V287-CREATOR-VAULT-CHECK-FAIL "
            f"mint={_short(mint)} full_mint={mint} source=cached_curve "
            f"err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _load_env() -> None:
    path = os.path.join(ROOT, ".env")
    try:
        with open(path, "r", encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip("\"'"))
    except FileNotFoundError:
        pass


def _default_rpc_url() -> str:
    if os.environ.get("V287_RPC_URL"):
        return str(os.environ["V287_RPC_URL"])
    if os.environ.get("RPCFAST_API_KEY"):
        return f"https://solana-rpc.rpcfast.com/?api_key={os.environ['RPCFAST_API_KEY']}"
    if os.environ.get("SOLANATRACKER_RPC_HTTP"):
        return str(os.environ["SOLANATRACKER_RPC_HTTP"])
    helius_key = os.environ.get("HELIUS_API_KEY", "")
    if helius_key:
        return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
    return "https://api.mainnet-beta.solana.com"


def _rpc_label(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc or "unknown"
    except Exception:
        return "unknown"


def _read_rpc_urls(primary: str) -> list[str]:
    urls: list[str] = []

    def add(url: str) -> None:
        if url and url not in urls:
            urls.append(url)

    add(primary)
    add(os.environ.get("V287_READ_RPC_URL", ""))
    if os.environ.get("RPCFAST_API_KEY"):
        add(f"https://solana-rpc.rpcfast.com/?api_key={os.environ['RPCFAST_API_KEY']}")
    add(os.environ.get("SOLANATRACKER_RPC_HTTP", ""))
    if os.environ.get("HELIUS_API_KEY"):
        add(f"https://mainnet.helius-rpc.com/?api-key={os.environ['HELIUS_API_KEY']}")
    add("https://api.mainnet-beta.solana.com")
    return urls


def _rpc_post_once(url: str, method: str, params: list[Any], timeout: float = 20.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("result")


def _rpc_post(url: str, method: str, params: list[Any], timeout: float = 20.0) -> Any:
    last_exc: Exception | None = None
    for candidate in _read_rpc_urls(url):
        try:
            return _rpc_post_once(candidate, method, params, timeout)
        except Exception as exc:
            last_exc = exc
            _log(
                "PGG2-V287-READ-RPC-FAILOVER "
                f"method={method} endpoint={_rpc_label(candidate)} "
                f"err={type(exc).__name__}:{str(exc)[:120]}"
            )
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("no_read_rpc_available")


def _wallet_lamports(rpc_url: str, commitment: str = "confirmed") -> int:
    return int(_rpc_post(rpc_url, "getBalance", [WALLET, {"commitment": commitment}])["value"])


def _mint_token_account_lamports(rpc_url: str, mint: str, commitment: str = "processed") -> int:
    try:
        res = _rpc_post(
            rpc_url,
            "getTokenAccountsByOwner",
            [WALLET, {"mint": mint}, {"encoding": "jsonParsed", "commitment": commitment}],
            timeout=4.0,
        )
    except Exception:
        return 0
    best = 0
    for item in res.get("value", []):
        try:
            best = max(best, int(item.get("account", {}).get("lamports") or 0))
        except Exception:
            continue
    return best


def _token_accounts(rpc_url: str) -> tuple[int, int]:
    nonzero = 0
    rent_locked = 0
    rpc_failed: Exception | None = None
    for program_id in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        try:
            res = _rpc_post(
                rpc_url,
                "getTokenAccountsByOwner",
                [WALLET, {"programId": program_id}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            )
        except Exception as exc:
            rpc_failed = exc
            continue
        for item in res.get("value", []):
            acc = item.get("account", {})
            info = acc.get("data", {}).get("parsed", {}).get("info", {})
            amount = str(info.get("tokenAmount", {}).get("amount", "0"))
            lamports = int(acc.get("lamports") or 0)
            if amount != "0":
                nonzero += 1
            elif lamports > 0:
                rent_locked += 1
    if nonzero == 0 and rent_locked == 0 and rpc_failed is not None:
        try:
            out = subprocess.check_output(
                [
                    "/root/.local/share/solana/install/active_release/bin/spl-token",
                    "accounts",
                    "--url",
                    "https://api.mainnet-beta.solana.com",
                    "--owner",
                    WALLET,
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception as exc:
            _log(
                "PGG2-V287-TOKEN-ACCOUNTS-DEGRADED "
                f"rpc_err={type(rpc_failed).__name__}:{str(rpc_failed)[:120]} "
                f"cli_err={type(exc).__name__}:{str(exc)[:120]} clean_assumed=1"
            )
            return 0, 0
        nonzero_cli = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in ("Token", "-----------------------------------------------------"):
                try:
                    if float(parts[-1]) != 0.0:
                        nonzero_cli += 1
                except Exception:
                    continue
        _log(f"PGG2-V287-TOKEN-ACCOUNTS-CLI-FALLBACK nonzero={nonzero_cli}")
        return nonzero_cli, 0
    return nonzero, rent_locked


def _configure_live_env(args: argparse.Namespace) -> None:
    rpc_url = str(args.rpc_url)
    os.environ["PGG2_EXECUTION_MODE"] = "live"
    os.environ["PGG2_LIVE_CONFIRM"] = "I_ACCEPT_REAL_SOL_RISK"
    os.environ["PGG2_DIRECT_LIVE_CONFIRM"] = "I_ACCEPT_DIRECT_PUMP_RISK"
    os.environ["PGG2_WALLET_KEYPAIR"] = "/root/piggy/live_wallet.key"
    os.environ["PGG2_LIVE_RPC_URL"] = rpc_url
    os.environ["PGG2_V74_SENDER_URL"] = "https://sender.helius-rpc.com/fast?swqos_only=true"
    os.environ["PGG2_V74_SENDER_PING_URL"] = "https://sender.helius-rpc.com/ping"
    os.environ["PGG2_V75_TIP_LAMPORTS"] = str(int(args.sender_tip_lamports))
    os.environ["PGG2_LIVE_SKIP_PREFLIGHT"] = "1"
    os.environ["PGG2_LIVE_MAX_RETRIES"] = "0"
    os.environ["PGG2_LIVE_SIMULATE_BEFORE_SEND"] = "0"
    os.environ["PGG2_LIVE_CONFIRM_TIMEOUT_SEC"] = "18"
    os.environ["PGG2_LIVE_HTTP_TIMEOUT_SEC"] = "6"
    os.environ["PGG2_LIVE_HTTP_RETRIES"] = "1"
    os.environ["PGG2_QUOTE_SIMULATE"] = "0"
    os.environ["PGG2_DIRECT_SELECT_BUYBACK_BY_SIM"] = "0"
    os.environ["PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL"] = "1"
    os.environ["PGG2_DIRECT_CLOSE_TOKEN_ATA_ONLY_ON_FULL_BALANCE"] = "1"
    os.environ["PGG2_DIRECT_COMPUTE_UNIT_LIMIT"] = "260000"
    os.environ["PGG2_DIRECT_PRIORITY_FEE_SOL"] = f"{float(args.priority_fee_sol):.9f}"
    os.environ["PGG2_DIRECT_BLOCKHASH_COMMITMENT"] = "processed"
    os.environ.setdefault("PGG2_DIRECT_BLOCKHASH_CACHE_MS", "30000")
    os.environ.setdefault("PGG2_DIRECT_BLOCKHASH_CACHE_LOG", "1")
    os.environ.setdefault("V287_SELL_BEFORE_BUY_CONFIRMED", "1")
    os.environ.setdefault("V287_EARLY_TOKEN_COMMITMENT", "processed")
    os.environ.setdefault("V287_EARLY_TOKEN_WAIT_SEC", "1.25")
    os.environ.setdefault("V287_EARLY_TOKEN_POLL_MS", "25")
    os.environ.setdefault("V287_SELL_BALANCE_COMMITMENT", "processed")
    os.environ.setdefault("V287_SELL_FLOOR_COMMITMENT", "processed")
    os.environ.setdefault("V287_EARLY_SELL_POLL_MS", "25")
    os.environ.setdefault("V287_EXTENDED_SCRATCH_MAX_MS", "900")
    os.environ.setdefault("V287_REQUIRE_POST_PLAN_REARM", "1")
    os.environ.setdefault("V287_POST_PLAN_REARM_TTL_MS", "1100")
    os.environ.setdefault("V287_BACKGROUND_BLOCKHASH_WARM_MS", "20000")
    os.environ.setdefault("V287_BACKGROUND_GLOBAL_WARM_MS", "4000")
    os.environ.setdefault("V287_MIN_FINAL_REFRESH_ABS_DRIFT_PCT", "0.05")
    os.environ.setdefault("V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS", "0")
    os.environ.setdefault("V287_FRESH_IMPULSE_ZERO_PREV_MIN_REARM_SOL", "1.50")
    os.environ.setdefault("V287_FRESH_IMPULSE_PREV_CARRY_MIN_SOL", "2.00")
    os.environ.setdefault("V287_ALLOW_PREPLAN_REARM_CREDIT", "1")
    os.environ.setdefault("V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS", "850")
    os.environ.setdefault("V287_VERIFIED_CONTINUATION_MIN_REARM_DELAY_MS", "75")
    os.environ.setdefault("V287_VERIFIED_CONTINUATION_MAX_REARM_DELAY_MS", "350")
    os.environ.setdefault("V287_VERIFIED_STRONG_FRESH_REARM_MIN_SOL", "3.80")
    os.environ.setdefault("V287_VERIFIED_PRIOR_CARRY_PREV_MIN_SOL", "2.00")
    os.environ.setdefault("V287_VERIFIED_PRIOR_CARRY_REARM_MIN_SOL", "0.70")
    os.environ.setdefault("V287_VERIFIED_PRIOR_CARRY_REARM_MAX_SOL", "1.20")
    os.environ.setdefault("V287_VERIFIED_MID_CARRY_PREV_MIN_SOL", "0.90")
    os.environ.setdefault("V287_VERIFIED_MID_CARRY_PREV_MAX_SOL", "2.00")
    os.environ.setdefault("V287_VERIFIED_MID_CARRY_REARM_MIN_SOL", "2.00")
    os.environ.setdefault("V287_VERIFIED_MID_CARRY_REARM_MAX_SOL", "3.50")
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_MIN_BUYS", "4")
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_MIN_SOL", "4.00")
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_MAX_AGE_MS", "350")
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_PREV_MAX_SOL", "0.10")
    os.environ.setdefault("V287_KEEP_UNVERIFIED_FRESH_WATCH", "1")
    os.environ.setdefault("V287_KEEP_UNVERIFIED_FRESH_WATCH_MAX_MS", "350")
    os.environ["PGG2_LIVE_MIN_TRADE_SOL"] = f"{float(args.size_sol):.9f}"
    os.environ["PGG2_LIVE_MAX_TRADE_SOL"] = f"{float(args.size_sol):.9f}"
    os.environ["PGG2_LIVE_MIN_WALLET_RESERVE_SOL"] = f"{float(args.min_reserve_sol):.9f}"


def _build_broker(args: argparse.Namespace) -> tuple[Any, Any]:
    _configure_live_env(args)
    broker = DirectPumpQuoteBroker(BotConfig())
    if str(broker.public_key) != WALLET:
        raise RuntimeError(f"wallet_mismatch broker={broker.public_key} expected={WALLET}")
    make_tip_builder(log_fn=_log).install_into_broker(broker)
    sender = make_sender(log_fn=_log)
    install_into_broker(broker, sender, log_fn=_log)
    sender.validate_endpoint()
    sender.ping()
    return broker, sender


def _decode_pump(update: geyser_pb2.SubscribeUpdate) -> dict[str, Any] | None:
    """Decode Pump buy/sell and carry buyback/social accounts from Geyser.

    This avoids waiting for getTransaction/confirmed RPC just to discover the
    May-2026 Pump remaining accounts. The feed transaction already has them.
    """
    if not update.HasField("transaction"):
        return None
    info = update.transaction.transaction
    tx = info.transaction
    if not tx.signatures:
        return None
    sig = str(Signature.from_bytes(tx.signatures[0]))
    keys = [_pubkey(bytes(k)) for k in tx.message.account_keys]
    try:
        meta = info.meta
        keys.extend(_pubkey(bytes(k)) for k in getattr(meta, "loaded_writable_addresses", []))
        keys.extend(_pubkey(bytes(k)) for k in getattr(meta, "loaded_readonly_addresses", []))
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
                if int(account_idx) < len(keys)
                and keys[int(account_idx)] == str(PUMP_FEE_PROGRAM_ID)
            ]
            if fee_positions:
                extras = [
                    keys[int(account_idx)]
                    for account_idx in accounts[fee_positions[-1] + 1 :]
                    if int(account_idx) < len(keys)
                ]
                social_positions = [
                    idx for idx, key in enumerate(extras) if key in KNOWN_PUMP_SOCIAL_FEE_PDAS
                ]
                if social_positions:
                    social_idx = social_positions[0]
                    social_fee_pda = str(extras[social_idx])
                    recipients = [
                        str(key)
                        for key in extras[:social_idx]
                        if key and key not in KNOWN_PUMP_SOCIAL_FEE_PDAS
                    ]
                    if recipients:
                        buyback_recipient = recipients[0]
        except Exception:
            buyback_recipient = ""
            social_fee_pda = ""

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
            return common
        sol_lamports = first if disc == DISC_BUY_EXACT_SOL_IN else second
        common.update({"kind": "buy", "token_amount_raw": 0, "sol_lamports": int(sol_lamports)})
        return common
    return None


def _remember_pair_from_feed_rec(broker: Any, rec: dict[str, Any]) -> bool:
    mint = str(rec.get("mint") or "")
    _remember_static_accounts_from_feed_rec(broker, rec)
    recipient = str(rec.get("buyback_recipient") or "")
    social = str(rec.get("social_fee_pda") or "")
    if not mint or not recipient or not social:
        return False
    try:
        broker.remember_pump_buyback_pair(
            as_pubkey(mint),
            PumpBuybackPair(as_pubkey(recipient), as_pubkey(social), "geyser_tx"),
        )
        _log(
            "PGG2-V287-PAIR-PREWARM-GEYSER "
            f"mint={_short(mint)} recipient={_short(recipient)} social={_short(social)}"
        )
        return True
    except Exception as exc:
        _log(
            "PGG2-V287-PAIR-PREWARM-GEYSER-FAIL "
            f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:120]}"
        )
        return False


def _remember_static_accounts_from_feed_rec(broker: Any, rec: dict[str, Any]) -> bool:
    """Carry Pump account metas from the raw Geyser tx into the precompiled plan.

    The hot send path cannot afford an extra creator-vault discovery pass. The
    transaction feed already gives us Pump's fee recipient and creator-vault
    metas; storing them lets prepare_pump_buy_static_plan compile the final
    account list while the candidate is still waiting for rearm.
    """
    mint = str(rec.get("mint") or "")
    if not mint:
        return False
    stored = False
    creator_vault = str(rec.get("creator_vault") or "")
    fee_recipient = str(rec.get("fee_recipient") or "")
    token_program = str(rec.get("token_program") or "")
    try:
        if token_program:
            as_pubkey(token_program)
            # mint_owner() only needs the account owner. The Geyser Pump ix
            # already carries the token program at account index 8, so cache it
            # and avoid a per-candidate getAccountInfo RPC in the hot path.
            getattr(broker, "_account_cache", {})[mint] = (
                time.time(),
                {"owner": token_program, "data": ["", "base64"]},
            )
            stored = True
        if creator_vault:
            as_pubkey(creator_vault)
            getattr(broker, "_pump_creator_vault_override", {})[mint] = creator_vault
            stored = True
        if fee_recipient:
            as_pubkey(fee_recipient)
            getattr(broker, "_pump_fee_recipient_override", {})[mint] = fee_recipient
            stored = True
        if stored:
            _log(
                "PGG2-V287-STATIC-ACCOUNT-PREWARM "
                f"mint={_short(mint)} creator_vault={_short(creator_vault)} "
                f"fee_recipient={_short(fee_recipient)} "
                f"token_program={_short(token_program)}"
            )
    except Exception as exc:
        _log(
            "PGG2-V287-STATIC-ACCOUNT-PREWARM-FAIL "
            f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:120]}"
        )
        return False
    return stored


def _prepare_static_buy_plan_from_feed_rec(
    broker: Any, rec: dict[str, Any], amount_sol: float
) -> bool:
    mint = str(rec.get("mint") or "")
    if not mint:
        return False
    try:
        _remember_pair_from_feed_rec(broker, rec)
        _remember_static_accounts_from_feed_rec(broker, rec)
        if not hasattr(broker, "prepare_pump_buy_static_plan"):
            return False
        broker.prepare_pump_buy_static_plan(mint, float(amount_sol), creator="")
        _log(
            "PGG2-V287-FAST-STATIC-PLAN-READY "
            f"mint={_short(mint)} size_sol={float(amount_sol):.6f}"
        )
        return True
    except Exception as exc:
        _log(
            "PGG2-V287-FAST-STATIC-PLAN-FAIL "
            f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _static_plan_future_done_ok(plan_fut: Any) -> tuple[bool, bool, str]:
    if plan_fut is None:
        return False, False, "missing"
    try:
        if not plan_fut.done():
            return False, False, "pending"
        return True, bool(plan_fut.result(timeout=0)), ""
    except Exception as exc:
        return True, False, f"{type(exc).__name__}:{str(exc)[:120]}"


def _candidate_live_ttl_ms(cand: dict[str, Any]) -> int:
    base = int(os.environ.get("V287_CANDIDATE_TTL_MS", "350"))
    if cand.get("post_plan_rearm_required"):
        return max(base, int(os.environ.get("V287_POST_PLAN_REARM_TTL_MS", "1100")))
    return base


def _prebuy_postbuy_sell_projection_from_curve(
    broker: Any,
    mint: str,
    curve: Any,
    args: argparse.Namespace,
    *,
    log_tag: str = "PGG2-V287-PREBUY-POSTBUY-SELL-CHECK",
) -> tuple[bool, float, int]:
    """Evaluate entry birth profitability from one already-fetched curve."""
    try:
        mint_pk = as_pubkey(mint)
        global_cfg = broker.pump_global()
        spend_lamports = max(1, int(float(args.size_sol) * LAMPORTS_PER_SOL))
        expected_tokens_raw, _buy_curve_fee = broker.quote_pump_buy_tokens(
            spend_lamports, curve, global_cfg
        )
        expected_tokens_ui = float(broker.raw_to_ui(mint_pk, expected_tokens_raw))
        post_curve = broker.simulate_post_buy_pump_curve(curve, expected_tokens_raw)
        projected_sell_out, projected_sell_curve_fee = broker.quote_pump_sell_sol(
            expected_tokens_raw, post_curve, global_cfg
        )
        buy_tx_fee_est = int(os.environ.get("V287_PREBUY_BUY_TX_FEE_EST_LAMPORTS", "50000"))
        sell_fee_est = int(args.sell_fee_est_lamports)
        projection_buffer = int(os.environ.get("V287_PREBUY_PROJECTION_BUFFER_LAMPORTS", "250000"))
        min_projected_delta = int(
            os.environ.get(
                "V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS",
                os.environ.get("V287_PREBUY_MIN_SELF_ROUNDTRIP_DELTA_LAMPORTS", "-1000000"),
            )
        )
        projected_delta = (
            int(projected_sell_out)
            - int(spend_lamports)
            - int(buy_tx_fee_est)
            - int(sell_fee_est)
            - int(projection_buffer)
        )
        ok = projected_delta >= min_projected_delta
        _log(
            f"{log_tag} "
            f"mint={_short(mint)} full_mint={mint} expected_tokens={expected_tokens_ui:.6f} "
            f"expected_tokens_raw={expected_tokens_raw} projected_sell_out={int(projected_sell_out)} "
            f"projected_sell_curve_fee={int(projected_sell_curve_fee)} spend_lamports={spend_lamports} "
            f"buy_tx_fee_est={buy_tx_fee_est} sell_fee_est={sell_fee_est} "
            f"projection_buffer={projection_buffer} projected_delta={projected_delta:+} "
            f"min_projected_delta={min_projected_delta} pass={int(ok)} source=fresh_curve_once"
        )
        return bool(ok), expected_tokens_ui, int(expected_tokens_raw)
    except Exception as exc:
        _log(
            "PGG2-V287-PREBUY-POSTBUY-SELL-CHECK-FAIL "
            f"mint={_short(mint)} full_mint={mint} source=fresh_curve_once "
            f"err={type(exc).__name__}:{str(exc)[:180]}"
        )
        return False, 0.0, 0


def _prebuy_continuation_credit_projection_from_curve(
    broker: Any,
    mint: str,
    curve: Any,
    args: argparse.Namespace,
    continuation_lamports: int,
) -> tuple[bool, int]:
    """Project our sell after a bounded, conservative continuation credit.

    Instant self-roundtrip is structurally negative on Pump because fees are
    paid both ways. This lane is a continuation scalper, so the final authority
    must model a small amount of follow-through, but only from observed rearm
    flow and only before any live buy is sent.
    """
    try:
        global_cfg = broker.pump_global()
        spend_lamports = max(1, int(float(args.size_sol) * LAMPORTS_PER_SOL))
        our_tokens_raw, _our_buy_fee = broker.quote_pump_buy_tokens(
            spend_lamports, curve, global_cfg
        )
        post_our_curve = broker.simulate_post_buy_pump_curve(curve, our_tokens_raw)
        cont_tokens_raw = 0
        post_cont_curve = post_our_curve
        if continuation_lamports > 0:
            cont_tokens_raw, _cont_fee = broker.quote_pump_buy_tokens(
                int(continuation_lamports), post_our_curve, global_cfg
            )
            post_cont_curve = broker.simulate_post_buy_pump_curve(
                post_our_curve, cont_tokens_raw
            )
        projected_sell_out, projected_sell_curve_fee = broker.quote_pump_sell_sol(
            our_tokens_raw, post_cont_curve, global_cfg
        )
        buy_tx_fee_est = int(os.environ.get("V287_PREBUY_BUY_TX_FEE_EST_LAMPORTS", "50000"))
        sell_fee_est = int(args.sell_fee_est_lamports)
        projection_buffer = int(os.environ.get("V287_CONTINUATION_PROJECTION_BUFFER_LAMPORTS", "350000"))
        min_projected_delta = int(
            os.environ.get(
                "V287_CONTINUATION_MIN_PROJECTED_DELTA_LAMPORTS",
                str(max(0, int(args.min_profit_lamports))),
            )
        )
        projected_delta = (
            int(projected_sell_out)
            - int(spend_lamports)
            - int(buy_tx_fee_est)
            - int(sell_fee_est)
            - int(projection_buffer)
        )
        ok = projected_delta >= min_projected_delta
        _log(
            "PGG2-V287-CONTINUATION-CREDIT-CHECK "
            f"mint={_short(mint)} full_mint={mint} "
            f"our_tokens_raw={our_tokens_raw} continuation_lamports={int(continuation_lamports)} "
            f"continuation_tokens_raw={cont_tokens_raw} projected_sell_out={int(projected_sell_out)} "
            f"projected_sell_curve_fee={int(projected_sell_curve_fee)} spend_lamports={spend_lamports} "
            f"buy_tx_fee_est={buy_tx_fee_est} sell_fee_est={sell_fee_est} "
            f"projection_buffer={projection_buffer} projected_delta={projected_delta:+} "
            f"min_projected_delta={min_projected_delta} pass={int(ok)}"
        )
        return bool(ok), int(projected_delta)
    except Exception as exc:
        _log(
            "PGG2-V287-CONTINUATION-CREDIT-CHECK-FAIL "
            f"mint={_short(mint)} full_mint={mint} err={type(exc).__name__}:{str(exc)[:180]}"
        )
        return False, -10**18


def _prebuy_postbuy_sell_projection_pass(
    broker: Any,
    mint: str,
    buy_quote: dict[str, Any],
    wallet_before_buy_lamports: int,
    args: argparse.Namespace,
) -> bool:
    """Block buys whose immediate post-buy sell model is not already positive.

    The fresh-impulse lane can have enough token output but still be below the
    rent-aware sell floor after our own buy impact and fees. That must fail
    before opening; the sell worker is for closing good entries, not rescuing
    entries that are negative at birth.
    """
    try:
        mint_pk = as_pubkey(mint)
        expected_tokens_ui = _rate_float(buy_quote, "amountOut")
        expected_tokens_raw = int(broker.ui_to_raw(mint_pk, expected_tokens_ui))
        curve = broker.bonding_curve(mint_pk)
        global_cfg = broker.pump_global()
        post_curve = broker.simulate_post_buy_pump_curve(curve, expected_tokens_raw)
        projected_sell_out, projected_sell_curve_fee = broker.quote_pump_sell_sol(
            expected_tokens_raw, post_curve, global_cfg
        )
        spend_lamports = max(1, int(float(args.size_sol) * LAMPORTS_PER_SOL))
        buy_tx_fee_est = int(os.environ.get("V287_PREBUY_BUY_TX_FEE_EST_LAMPORTS", "50000"))
        sell_fee_est = int(args.sell_fee_est_lamports)
        projection_buffer = int(os.environ.get("V287_PREBUY_PROJECTION_BUFFER_LAMPORTS", "250000"))
        min_projected_delta = int(
            os.environ.get(
                "V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS",
                os.environ.get("V287_PREBUY_MIN_SELF_ROUNDTRIP_DELTA_LAMPORTS", "-1000000"),
            )
        )
        projected_delta = (
            int(projected_sell_out)
            - int(spend_lamports)
            - int(buy_tx_fee_est)
            - int(sell_fee_est)
            - int(projection_buffer)
        )
        ok = projected_delta >= min_projected_delta
        _log(
            "PGG2-V287-PREBUY-POSTBUY-SELL-CHECK "
            f"mint={_short(mint)} full_mint={mint} expected_tokens={expected_tokens_ui:.6f} "
            f"expected_tokens_raw={expected_tokens_raw} projected_sell_out={int(projected_sell_out)} "
            f"projected_sell_curve_fee={int(projected_sell_curve_fee)} spend_lamports={spend_lamports} "
            f"buy_tx_fee_est={buy_tx_fee_est} sell_fee_est={sell_fee_est} "
            f"projection_buffer={projection_buffer} projected_delta={projected_delta:+} "
            f"min_projected_delta={min_projected_delta} pass={int(ok)}"
        )
        return bool(ok)
    except Exception as exc:
        _log(
            "PGG2-V287-PREBUY-POSTBUY-SELL-CHECK-FAIL "
            f"mint={_short(mint)} full_mint={mint} err={type(exc).__name__}:{str(exc)[:180]}"
        )
        return False


SHADOW_WINDOWS_MS = (350, 1000, 3000)


def _shadow_start(
    shadows: dict[str, dict[str, Any]],
    counters: Counter[str],
    *,
    reason: str,
    rec: dict[str, Any],
    now_ms: int,
    ttl_ms: int,
    max_open: int,
    current_buy_sol: float,
    prev_buys: int,
    prev_buy_sol: float,
    prev_sells: int,
    top_share: float,
    top_lane: str = "-",
    rearm_min_sol: float = 0.0,
) -> None:
    """Track near-decision outcomes without spending.

    The live logs had enough evidence to count blockers, but not enough to
    label whether a block protected us or missed a continuation. This tracker
    keeps a short post-block window from the same Geyser feed.
    """
    if ttl_ms <= 0:
        return
    if len(shadows) >= max(1, int(max_open)):
        counters["shadow_drop_capacity"] += 1
        return
    mint = str(rec.get("mint") or "")
    sig = str(rec.get("sig") or "")
    if not mint or not sig:
        return
    key = f"{mint}:{sig}:{reason}"
    if key in shadows:
        return
    sh: dict[str, Any] = {
        "key": key,
        "mint": mint,
        "start_sig": sig,
        "start_ms": int(now_ms),
        "end_ms": int(now_ms) + int(ttl_ms),
        "reason": reason,
        "current_buy_sol": float(current_buy_sol),
        "prev_buys": int(prev_buys),
        "prev_buy_sol": float(prev_buy_sol),
        "prev_sells": int(prev_sells),
        "top_share": float(top_share),
        "top_lane": str(top_lane or "-"),
        "rearm_min_sol": float(rearm_min_sol or 0.0),
        "first_sell_ms": None,
    }
    for window_ms in SHADOW_WINDOWS_MS:
        sh[f"buy_count_{window_ms}"] = 0
        sh[f"buy_lamports_{window_ms}"] = 0
        sh[f"sell_count_{window_ms}"] = 0
    shadows[key] = sh
    counters["shadow_start"] += 1
    _log(
        "PGG2-V287-SHADOW-MISS-START "
        f"mint={_short(mint)} full_mint={mint} blocker={reason} "
        f"current_buy_sol={float(current_buy_sol):.6f} prev_buys_1s={int(prev_buys)} "
        f"prev_buy_sol_1s={float(prev_buy_sol):.6f} prev_sells_1s={int(prev_sells)} "
        f"top_share_1s={float(top_share):.4f} top_lane={top_lane} "
        f"rearm_min_sol={float(rearm_min_sol or 0.0):.6f} ttl_ms={int(ttl_ms)}"
    )


def _shadow_update(
    shadows: dict[str, dict[str, Any]],
    counters: Counter[str],
    rec: dict[str, Any],
    now_ms: int,
) -> None:
    mint = str(rec.get("mint") or "")
    if not mint:
        return
    touched = False
    for sh in list(shadows.values()):
        if str(sh.get("mint")) != mint:
            continue
        if str(rec.get("sig") or "") == str(sh.get("start_sig") or ""):
            continue
        dt_ms = int(now_ms) - int(sh.get("start_ms") or now_ms)
        if dt_ms < 0 or dt_ms > int(sh.get("end_ms") or now_ms) - int(sh.get("start_ms") or now_ms):
            continue
        kind = str(rec.get("kind") or "")
        if kind == "sell" and sh.get("first_sell_ms") is None:
            sh["first_sell_ms"] = dt_ms
        for window_ms in SHADOW_WINDOWS_MS:
            if dt_ms > window_ms:
                continue
            if kind == "buy":
                sh[f"buy_count_{window_ms}"] = int(sh.get(f"buy_count_{window_ms}") or 0) + 1
                sh[f"buy_lamports_{window_ms}"] = int(sh.get(f"buy_lamports_{window_ms}") or 0) + int(
                    rec.get("sol_lamports") or 0
                )
                touched = True
            elif kind == "sell":
                sh[f"sell_count_{window_ms}"] = int(sh.get(f"sell_count_{window_ms}") or 0) + 1
                touched = True
    if touched:
        counters["shadow_update"] += 1


def _shadow_emit_end(
    shadows: dict[str, dict[str, Any]],
    counters: Counter[str],
    key: str,
    now_ms: int,
    status: str,
) -> None:
    sh = shadows.pop(key, None)
    if not sh:
        return
    age_ms = max(0, int(now_ms) - int(sh.get("start_ms") or now_ms))
    full_ms = SHADOW_WINDOWS_MS[-1]
    full_buy_sol = int(sh.get(f"buy_lamports_{full_ms}") or 0) / LAMPORTS_PER_SOL
    rearm070 = int(full_buy_sol >= 0.70)
    rearm180 = int(full_buy_sol >= 1.80)
    rearm200 = int(full_buy_sol >= 2.00)
    clean350 = int(int(sh.get("sell_count_350") or 0) == 0)
    clean1000 = int(int(sh.get("sell_count_1000") or 0) == 0)
    first_sell = sh.get("first_sell_ms")
    counters["shadow_end"] += 1
    _log(
        "PGG2-V287-SHADOW-MISS-END "
        f"mint={_short(str(sh.get('mint') or ''))} full_mint={sh.get('mint')} "
        f"blocker={sh.get('reason')} status={status} age_ms={age_ms} "
        f"current_buy_sol={float(sh.get('current_buy_sol') or 0.0):.6f} "
        f"prev_buys_1s={int(sh.get('prev_buys') or 0)} "
        f"prev_buy_sol_1s={float(sh.get('prev_buy_sol') or 0.0):.6f} "
        f"prev_sells_1s={int(sh.get('prev_sells') or 0)} "
        f"top_share_1s={float(sh.get('top_share') or 0.0):.4f} "
        f"top_lane={sh.get('top_lane')} rearm_min_sol={float(sh.get('rearm_min_sol') or 0.0):.6f} "
        f"future_buy_sol_350ms={int(sh.get('buy_lamports_350') or 0)/LAMPORTS_PER_SOL:.6f} "
        f"future_buys_350ms={int(sh.get('buy_count_350') or 0)} "
        f"future_sells_350ms={int(sh.get('sell_count_350') or 0)} "
        f"future_buy_sol_1000ms={int(sh.get('buy_lamports_1000') or 0)/LAMPORTS_PER_SOL:.6f} "
        f"future_buys_1000ms={int(sh.get('buy_count_1000') or 0)} "
        f"future_sells_1000ms={int(sh.get('sell_count_1000') or 0)} "
        f"future_buy_sol_3000ms={full_buy_sol:.6f} "
        f"future_buys_3000ms={int(sh.get('buy_count_3000') or 0)} "
        f"future_sells_3000ms={int(sh.get('sell_count_3000') or 0)} "
        f"first_sell_ms={first_sell if first_sell is not None else -1} "
        f"clean350={clean350} clean1000={clean1000} "
        f"would_rearm070={rearm070} would_rearm180={rearm180} would_rearm200={rearm200}"
    )


def _shadow_flush_expired(
    shadows: dict[str, dict[str, Any]],
    counters: Counter[str],
    now_ms: int,
    status: str = "expired",
) -> None:
    for key, sh in list(shadows.items()):
        if int(now_ms) >= int(sh.get("end_ms") or now_ms):
            _shadow_emit_end(shadows, counters, key, now_ms, status)


def _prewarm_pair_from_sigs(broker: Any, mint: str, sigs: list[str], attempts: int = 3) -> bool:
    """Populate the Pump buyback/social pair cache before build_buy.

    The selected-band feed gives us the external Pump signatures. The direct
    live broker refuses to build a guarded buy for fresh mints unless this
    pair has been observed from a real Pump transaction.
    """
    seen: set[str] = set()
    for sig in sigs:
        sig = str(sig or "")
        if not sig or sig in seen:
            continue
        seen.add(sig)
        for attempt in range(max(1, int(attempts))):
            try:
                ok = bool(broker.prewarm_pump_buyback_pair_from_sig(mint, sig))
            except Exception as exc:
                ok = False
                _log(
                    "PGG2-V287-PAIR-PREWARM-EXC "
                    f"mint={_short(mint)} sig={sig[:16]} attempt={attempt + 1} "
                    f"err={type(exc).__name__}:{str(exc)[:120]}"
                )
            if ok:
                _log(
                    "PGG2-V287-PAIR-PREWARM-OK "
                    f"mint={_short(mint)} sig={sig[:16]} attempt={attempt + 1}"
                )
                return True
            time.sleep(0.045)
    _log(
        "PGG2-V287-PAIR-PREWARM-BLOCK "
        f"mint={_short(mint)} sigs={len(seen)} reason=no_confirmed_pair"
    )
    return False


def _token_balance_raw_for_commitment(broker: Any, mint: str, commitment: str) -> int:
    if commitment == "confirmed":
        return int(broker.token_balance_raw(as_pubkey(mint)))
    mint_pk = as_pubkey(mint)
    try:
        token_program = broker.mint_owner(mint_pk)
    except Exception:
        token_program = DIRECT_TOKEN_PROGRAM_ID
    ata = get_associated_token_address(as_pubkey(broker.public_key), mint_pk, token_program)
    out = broker.rpc("getTokenAccountBalance", [str(ata), {"commitment": commitment}])
    return int(((out or {}).get("value") or {}).get("amount") or 0)


def _wait_token_balance_raw(
    broker: Any,
    mint: str,
    timeout_sec: float,
    *,
    commitment: str = "confirmed",
    poll_ms: int = 80,
) -> int:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            bal = int(_token_balance_raw_for_commitment(broker, mint, commitment))
            if bal > 0:
                return bal
        except Exception:
            pass
        time.sleep(max(0.01, float(poll_ms) / 1000.0))
    return 0


def _sell_all_for_target(
    broker: Any,
    mint: str,
    wallet_before_buy_lamports: int,
    buy_sig: str,
    args: argparse.Namespace,
) -> tuple[str, int, int]:
    entry_deadline = time.time() + float(args.max_hold_ms) / 1000.0
    target_lamports = int(args.min_profit_lamports)
    scratch_profit_lamports = int(args.scratch_profit_lamports)
    sell_fee_est = int(args.sell_fee_est_lamports)
    last_expected = 0
    last_min = 0
    last_quote: dict[str, Any] | None = None
    last_quote_min_out = 0
    last_scratch_min = 0
    failed_exit_fee_spend = 0
    floor_wallet_now: int | None = None
    floor_recoverable_rent: int | None = None
    balance_commitment = os.environ.get("V287_SELL_BALANCE_COMMITMENT", "processed")
    floor_commitment = os.environ.get("V287_SELL_FLOOR_COMMITMENT", "processed")
    sell_poll_sec = max(
        0.01,
        float(os.environ.get("V287_EARLY_SELL_POLL_MS", str(args.sell_poll_ms))) / 1000.0,
    )
    _log(
        "PGG2-V287-EARLY-SELL-WORKER-START "
        f"mint={_short(mint)} buy_sig={buy_sig[:24]} "
        f"balance_commitment={balance_commitment} floor_commitment={floor_commitment} "
        f"poll_ms={int(sell_poll_sec * 1000)}"
    )

    def _floor_inputs() -> tuple[int, int]:
        nonlocal floor_wallet_now, floor_recoverable_rent
        if floor_wallet_now is None:
            floor_wallet_now = int(_wallet_lamports(str(args.rpc_url), floor_commitment))
        if floor_recoverable_rent is None:
            floor_recoverable_rent = int(_mint_token_account_lamports(str(args.rpc_url), mint, "processed"))
            if floor_recoverable_rent <= 0:
                floor_recoverable_rent = ATA_RENT_LAMPORTS
        return floor_wallet_now, floor_recoverable_rent

    def _rent_aware_floor(desired_profit_lamports: int) -> tuple[int, int, int]:
        wallet_now, recoverable_rent = _floor_inputs()
        floor = int(
            wallet_before_buy_lamports
            + desired_profit_lamports
            - wallet_now
            - recoverable_rent
            + sell_fee_est
            + failed_exit_fee_spend
        )
        return max(100, floor), wallet_now, recoverable_rent

    def _send_protected_sell(quote: dict[str, Any], min_out_lamports: int, guard: str, expected_out: int) -> str:
        if expected_out < min_out_lamports:
            raise RuntimeError(
                f"sell_not_executable:guard={guard}:expected={expected_out}:min={min_out_lamports}"
            )
        protected = broker.retarget_sell_min_sol(quote, mint, min_out_lamports / LAMPORTS_PER_SOL)
        signed_b64, sig_preview = broker.sign_transaction(str(protected["txn"]))
        _log(
            "PGG2-V287-PROTECTED-SELL-SEND "
            f"mint={_short(mint)} guard={guard} sig_preview={sig_preview[:24]} "
            f"min_out_lamports={min_out_lamports} expected_out={expected_out}"
        )
        sig = broker.send_signed(signed_b64)
        ok = broker.wait_confirmed(sig)
        if not ok:
            raise RuntimeError(f"protected_sell_confirm_failed:{guard}:{sig}")
        _log(f"PGG2-V287-PROTECTED-SELL-CONFIRMED mint={_short(mint)} guard={guard} sig={sig}")
        return sig

    while time.time() < entry_deadline:
        try:
            token_raw = int(_token_balance_raw_for_commitment(broker, mint, balance_commitment))
        except Exception as exc:
            _log(
                "PGG2-V287-SELL-BALANCE-ERROR "
                f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:180]}"
            )
            time.sleep(sell_poll_sec)
            continue
        if token_raw <= 0:
            time.sleep(sell_poll_sec)
            continue
        token_ui = broker.raw_to_ui(as_pubkey(mint), token_raw)
        try:
            quote = broker.build_sell(mint, token_ui, float(args.sell_slippage_pct))
        except Exception as exc:
            _log(
                "PGG2-V287-SELL-QUOTE-ERROR "
                f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:180]} "
                f"last_expected={last_expected} last_quote_min={last_quote_min_out}"
            )
            if last_quote is not None and time.time() >= entry_deadline:
                if os.environ.get("V287_DISABLE_NEGATIVE_MAXHOLD_SELL", "1") != "0":
                    if last_scratch_min > 0 and last_expected >= last_scratch_min:
                        sig = _send_protected_sell(
                            last_quote,
                            last_scratch_min,
                            "quote_error_scratch_guard",
                            last_expected,
                        )
                        return sig, last_expected, last_scratch_min
                    raise RuntimeError(
                        "quote_error_scratch_not_executable_no_low_guard:"
                        f"expected={last_expected}:scratch={last_scratch_min}"
                    )
                fallback_min = max(100, last_quote_min_out)
                sig = _send_protected_sell(last_quote, fallback_min, "quote_error_low_guard", last_expected)
                return sig, last_expected, fallback_min
            time.sleep(sell_poll_sec)
            continue
        expected_out = int(float((quote.get("rate") or {}).get("amountOut") or 0.0) * LAMPORTS_PER_SOL)
        quote_min_out = int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL)
        last_quote = quote
        last_quote_min_out = quote_min_out
        min_out_needed, wallet_now, recoverable_rent = _rent_aware_floor(target_lamports)
        scratch_min_needed, _, _ = _rent_aware_floor(scratch_profit_lamports)
        last_scratch_min = scratch_min_needed
        last_expected = expected_out
        last_min = min_out_needed
        projected_delta = (
            wallet_now
            + expected_out
            + recoverable_rent
            - sell_fee_est
            - failed_exit_fee_spend
            - wallet_before_buy_lamports
        )
        _log(
            "PGG2-V287-SELL-CHECK "
            f"mint={_short(mint)} token_raw={token_raw} expected_out={expected_out} "
            f"min_needed={min_out_needed} projected_delta={projected_delta:+} "
            f"target={target_lamports} wallet_now={wallet_now} "
            f"recoverable_rent={recoverable_rent} failed_exit_fees={failed_exit_fee_spend} "
            f"buy_sig={buy_sig[:24]}"
        )
        if expected_out >= min_out_needed:
            try:
                sig = _send_protected_sell(quote, min_out_needed, "target", expected_out)
                return sig, expected_out, min_out_needed
            except Exception as exc:
                failed_exit_fee_spend += sell_fee_est
                _log(
                    "PGG2-V287-PROTECTED-SELL-FAILED "
                    f"mint={_short(mint)} guard=target exc={type(exc).__name__}:{exc} "
                    f"failed_exit_fees={failed_exit_fee_spend}"
                )
        elif expected_out >= scratch_min_needed:
            try:
                _log(
                    "PGG2-V287-IMMEDIATE-SCRATCH-SELL "
                    f"mint={_short(mint)} expected_out={expected_out} "
                    f"scratch_min={scratch_min_needed} projected_delta={projected_delta:+} "
                    f"target_min={min_out_needed}"
                )
                sig = _send_protected_sell(quote, scratch_min_needed, "scratch_immediate", expected_out)
                return sig, expected_out, scratch_min_needed
            except Exception as exc:
                failed_exit_fee_spend += sell_fee_est
                _log(
                    "PGG2-V287-PROTECTED-SELL-FAILED "
                    f"mint={_short(mint)} guard=scratch_immediate exc={type(exc).__name__}:{exc} "
                    f"failed_exit_fees={failed_exit_fee_spend}"
                )
        time.sleep(sell_poll_sec)

    # Bounded safety exit. It is still protected by an explicit rent-aware
    # min_out, so a bad close fails instead of realizing a negative wallet delta.
    token_raw = int(_token_balance_raw_for_commitment(broker, mint, balance_commitment))
    if token_raw <= 0:
        return "", last_expected, last_min
    token_ui = broker.raw_to_ui(as_pubkey(mint), token_raw)
    try:
        quote = broker.build_sell(mint, token_ui, float(args.emergency_sell_slippage_pct))
    except Exception as exc:
        if last_quote is None:
            raise
        _log(
            "PGG2-V287-MAXHOLD-QUOTE-FALLBACK "
            f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:180]} "
            f"last_expected={last_expected} last_quote_min={last_quote_min_out}"
        )
        quote = last_quote
    expected_out = int(float((quote.get("rate") or {}).get("amountOut") or 0.0) * LAMPORTS_PER_SOL)
    quote_min_out = int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL)
    target_min, wallet_now, recoverable_rent = _rent_aware_floor(target_lamports)
    scratch_min, _, _ = _rent_aware_floor(scratch_profit_lamports)
    last_scratch_min = scratch_min
    if expected_out >= target_min:
        maxhold_guard = "maxhold_target"
        maxhold_min = target_min
    elif expected_out >= scratch_min:
        maxhold_guard = "maxhold_scratch"
        maxhold_min = scratch_min
    else:
        if os.environ.get("V287_DISABLE_NEGATIVE_MAXHOLD_SELL", "1") != "0":
            extended_deadline = time.time() + (
                float(os.environ.get("V287_EXTENDED_SCRATCH_MAX_MS", "5000")) / 1000.0
            )
            _log(
                "PGG2-V287-MAXHOLD-SCRATCH-MISS "
                f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                f"target_min={target_min} quote_min_out={quote_min_out} action=extend_no_low_guard"
            )
            while time.time() < extended_deadline:
                time.sleep(sell_poll_sec)
                token_raw = int(_token_balance_raw_for_commitment(broker, mint, balance_commitment))
                if token_raw <= 0:
                    return "", expected_out, scratch_min
                token_ui = broker.raw_to_ui(as_pubkey(mint), token_raw)
                quote = broker.build_sell(mint, token_ui, float(args.sell_slippage_pct))
                expected_out = int(float((quote.get("rate") or {}).get("amountOut") or 0.0) * LAMPORTS_PER_SOL)
                scratch_min, wallet_now, recoverable_rent = _rent_aware_floor(scratch_profit_lamports)
                projected_delta = (
                    wallet_now
                    + expected_out
                    + recoverable_rent
                    - sell_fee_est
                    - failed_exit_fee_spend
                    - wallet_before_buy_lamports
                )
                _log(
                    "PGG2-V287-EXTENDED-SCRATCH-CHECK "
                    f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                    f"projected_delta={projected_delta:+}"
                )
                if expected_out >= scratch_min:
                    sig = _send_protected_sell(quote, scratch_min, "extended_scratch", expected_out)
                    return sig, expected_out, scratch_min
            if os.environ.get("V287_EMERGENCY_CLOSE_ON_NO_SCRATCH", "1") == "1":
                emergency_min = max(100, int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL))
                _log(
                    "PGG2-V287-NO-SCRATCH-EMERGENCY-CLOSE "
                    f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                    f"selected_min={emergency_min} reason=maxhold_scratch_not_executable"
                )
                sig = _send_protected_sell(quote, emergency_min, "no_scratch_emergency_low_guard", expected_out)
                return sig, expected_out, emergency_min
            raise RuntimeError(
                f"maxhold_scratch_not_executable_no_low_guard:expected={expected_out}:scratch={scratch_min}"
            )
        maxhold_guard = "maxhold_low_guard"
        maxhold_min = max(100, quote_min_out)
    projected_delta = (
        wallet_now
        + expected_out
        + recoverable_rent
        - sell_fee_est
        - failed_exit_fee_spend
        - wallet_before_buy_lamports
    )
    _log(
        "PGG2-V287-MAXHOLD-PROTECTED-SELL "
        f"mint={_short(mint)} guard={maxhold_guard} token_raw={token_raw} "
        f"expected_out={expected_out} target_min={target_min} scratch_min={scratch_min} "
        f"selected_min={maxhold_min} projected_delta={projected_delta:+} "
        f"recoverable_rent={recoverable_rent} quote_min_out={quote_min_out}"
    )
    try:
        sig = _send_protected_sell(quote, maxhold_min, maxhold_guard, expected_out)
        return sig, expected_out, maxhold_min
    except Exception as exc:
        failed_exit_fee_spend += sell_fee_est
        _log(
            "PGG2-V287-PROTECTED-SELL-FAILED "
            f"mint={_short(mint)} guard={maxhold_guard} exc={type(exc).__name__}:{exc} "
            f"failed_exit_fees={failed_exit_fee_spend}"
        )
        try:
            token_raw_after = int(_token_balance_raw_for_commitment(broker, mint, balance_commitment))
        except Exception as bal_exc:
            if "could not find account" in str(bal_exc).lower() or "invalid param" in str(bal_exc).lower():
                token_raw_after = 0
            else:
                raise
        if token_raw_after <= 0:
            return sig if "sig" in locals() else "", expected_out, maxhold_min
        if maxhold_guard == "maxhold_target":
            token_ui = broker.raw_to_ui(as_pubkey(mint), token_raw_after)
            quote = broker.build_sell(mint, token_ui, float(args.emergency_sell_slippage_pct))
            expected_out = int(float((quote.get("rate") or {}).get("amountOut") or 0.0) * LAMPORTS_PER_SOL)
            quote_min_out = int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL)
            scratch_min, wallet_now, recoverable_rent = _rent_aware_floor(scratch_profit_lamports)
            last_scratch_min = scratch_min
            scratch_retry_min = scratch_min if expected_out >= scratch_min else max(100, quote_min_out)
            scratch_retry_guard = (
                "maxhold_scratch_after_target_fail"
                if expected_out >= scratch_min
                else "maxhold_low_guard_after_target_fail"
            )
            projected_delta = (
                wallet_now
                + expected_out
                + recoverable_rent
                - sell_fee_est
                - failed_exit_fee_spend
                - wallet_before_buy_lamports
            )
            _log(
                "PGG2-V287-MAXHOLD-PROTECTED-SELL "
                f"mint={_short(mint)} guard={scratch_retry_guard} "
                f"token_raw={token_raw_after} expected_out={expected_out} "
                f"scratch_min={scratch_min} selected_min={scratch_retry_min} "
                f"projected_delta={projected_delta:+} recoverable_rent={recoverable_rent} "
                f"quote_min_out={quote_min_out}"
            )
            if (
                os.environ.get("V287_DISABLE_NEGATIVE_MAXHOLD_SELL", "1") != "0"
                and expected_out < scratch_min
            ):
                _log(
                    "PGG2-V287-TARGET-FAIL-SCRATCH-MISS "
                    f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                    f"quote_min_out={quote_min_out} action=no_low_guard"
                )
                if os.environ.get("V287_EMERGENCY_CLOSE_ON_NO_SCRATCH", "1") == "1":
                    emergency_min = max(100, quote_min_out)
                    _log(
                        "PGG2-V287-NO-SCRATCH-EMERGENCY-CLOSE "
                        f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                        f"selected_min={emergency_min} reason=target_fail_scratch_not_executable"
                    )
                    sig = _send_protected_sell(
                        quote,
                        emergency_min,
                        "target_fail_no_scratch_emergency_low_guard",
                        expected_out,
                    )
                    return sig, expected_out, emergency_min
                raise RuntimeError(
                    "target_fail_scratch_not_executable_no_low_guard:"
                    f"expected={expected_out}:scratch={scratch_min}"
                )
            try:
                sig = _send_protected_sell(
                    quote,
                    scratch_retry_min,
                    scratch_retry_guard,
                    expected_out,
                )
                return sig, expected_out, scratch_retry_min
            except Exception as scratch_exc:
                failed_exit_fee_spend += sell_fee_est
                _log(
                    "PGG2-V287-PROTECTED-SELL-FAILED "
                    f"mint={_short(mint)} guard={scratch_retry_guard} "
                    f"exc={type(scratch_exc).__name__}:{scratch_exc} "
                    f"failed_exit_fees={failed_exit_fee_spend}"
                )
                try:
                    remaining_raw = int(_token_balance_raw_for_commitment(broker, mint, balance_commitment))
                except Exception as bal_exc:
                    if "could not find account" in str(bal_exc).lower() or "invalid param" in str(bal_exc).lower():
                        remaining_raw = 0
                    else:
                        raise
                if remaining_raw <= 0:
                    return sig if "sig" in locals() else "", expected_out, scratch_retry_min
        raise RuntimeError(f"maxhold_protected_sell_failed_token_still_open:{mint}")


def _maybe_sell_before_buy_confirm(
    broker: Any,
    mint: str,
    wallet_before_buy_lamports: int,
    buy_sig: str,
    args: argparse.Namespace,
) -> tuple[str, int, int] | None:
    if os.environ.get("V287_SELL_BEFORE_BUY_CONFIRMED", "1") == "0":
        return None
    commitment = os.environ.get("V287_EARLY_TOKEN_COMMITMENT", "processed")
    wait_sec = float(os.environ.get("V287_EARLY_TOKEN_WAIT_SEC", "1.25"))
    poll_ms = int(os.environ.get("V287_EARLY_TOKEN_POLL_MS", "25"))
    token_raw = _wait_token_balance_raw(
        broker,
        mint,
        wait_sec,
        commitment=commitment,
        poll_ms=poll_ms,
    )
    _log(
        "PGG2-V287-EARLY-TOKEN-BALANCE "
        f"mint={_short(mint)} buy_sig={buy_sig[:24]} token_raw={token_raw} "
        f"commitment={commitment} wait_ms={int(wait_sec * 1000)} poll_ms={poll_ms}"
    )
    if token_raw <= 0:
        return None
    return _sell_all_for_target(broker, mint, wallet_before_buy_lamports, buy_sig, args)


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--endpoint", default=os.environ.get("PUBLICNODE_YELLOWSTONE_ENDPOINT", "solana-yellowstone-grpc.publicnode.com:443"))
    ap.add_argument("--token-env", default="PUBLICNODE_X_TOKEN")
    ap.add_argument("--metadata-key", default=os.environ.get("PUBLICNODE_GRPC_METADATA_KEY", "x-token"))
    ap.add_argument("--ping-seconds", type=int, default=15)
    ap.add_argument("--rpc-url", default=_default_rpc_url())
    ap.add_argument("--size-sol", type=float, default=float(os.environ.get("V287_SIZE_SOL", "0.030")))
    ap.add_argument("--current-min-sol", type=float, default=float(os.environ.get("V287_CURRENT_MIN_SOL", "2.80")))
    ap.add_argument("--current-max-sol", type=float, default=float(os.environ.get("V287_CURRENT_MAX_SOL", "3.25")))
    ap.add_argument("--prev-sol-min-1s", type=float, default=float(os.environ.get("V287_PREV_SOL_MIN_1S", "7.50")))
    ap.add_argument("--prev-sol-max-1s", type=float, default=float(os.environ.get("V287_PREV_SOL_MAX_1S", "9.60")))
    ap.add_argument("--rearm-min-sol", type=float, default=float(os.environ.get("V287_REARM_MIN_SOL", "0.70")))
    ap.add_argument("--rearm-max-sol", type=float, default=float(os.environ.get("V287_REARM_MAX_SOL", "1.65")))
    ap.add_argument("--top-share-min", type=float, default=float(os.environ.get("V287_TOP_SHARE_MIN", "0.55")))
    ap.add_argument("--top-share-normal-min", type=float, default=float(os.environ.get("V287_TOP_SHARE_NORMAL_MIN", "0.55")))
    ap.add_argument("--top-share-max", type=float, default=float(os.environ.get("V287_TOP_SHARE_MAX", "0.80")))
    ap.add_argument("--low-top-rearm-min-sol", type=float, default=float(os.environ.get("V287_LOW_TOP_REARM_MIN_SOL", "1.00")))
    ap.add_argument("--low-top-rearm-max-sol", type=float, default=float(os.environ.get("V287_LOW_TOP_REARM_MAX_SOL", "1.65")))
    ap.add_argument("--fresh-impulse-current-min-sol", type=float, default=float(os.environ.get("V287_FRESH_IMPULSE_CURRENT_MIN_SOL", "2.80")))
    ap.add_argument("--fresh-impulse-current-max-sol", type=float, default=float(os.environ.get("V287_FRESH_IMPULSE_CURRENT_MAX_SOL", "3.25")))
    ap.add_argument("--fresh-impulse-prev-buy-max-sol", type=float, default=float(os.environ.get("V287_FRESH_IMPULSE_PREV_BUY_MAX_SOL", "3.00")))
    ap.add_argument("--fresh-impulse-rearm-min-sol", type=float, default=float(os.environ.get("V287_FRESH_IMPULSE_REARM_MIN_SOL", "3.00")))
    ap.add_argument("--fresh-impulse-rearm-max-sol", type=float, default=float(os.environ.get("V287_FRESH_IMPULSE_REARM_MAX_SOL", "4.50")))
    ap.add_argument(
        "--fresh-impulse-max-rearm-buys",
        type=int,
        default=int(os.environ.get("V287_FRESH_IMPULSE_MAX_REARM_BUYS", "3")),
    )
    ap.add_argument(
        "--high-current-rearm-min-buys",
        type=int,
        default=int(os.environ.get("V287_HIGH_CURRENT_REARM_MIN_BUYS", "2")),
    )
    ap.add_argument(
        "--enable-single-prior-buy-lane",
        action="store_true",
        default=os.environ.get("V287_ENABLE_SINGLE_PRIOR_BUY_LANE", "1") == "1",
    )
    ap.add_argument(
        "--single-prior-current-min-sol",
        type=float,
        default=float(os.environ.get("V287_SINGLE_PRIOR_CURRENT_MIN_SOL", "2.00")),
    )
    ap.add_argument(
        "--single-prior-current-max-sol",
        type=float,
        default=float(os.environ.get("V287_SINGLE_PRIOR_CURRENT_MAX_SOL", "2.50")),
    )
    ap.add_argument(
        "--single-prior-prev-buy-sol-min",
        type=float,
        default=float(os.environ.get("V287_SINGLE_PRIOR_PREV_BUY_SOL_MIN", "5.00")),
    )
    ap.add_argument(
        "--single-prior-prev-buy-sol-max",
        type=float,
        default=float(os.environ.get("V287_SINGLE_PRIOR_PREV_BUY_SOL_MAX", "10.00")),
    )
    ap.add_argument(
        "--single-prior-rearm-min-sol",
        type=float,
        default=float(os.environ.get("V287_SINGLE_PRIOR_REARM_MIN_SOL", "4.00")),
    )
    ap.add_argument(
        "--single-prior-rearm-max-sol",
        type=float,
        default=float(os.environ.get("V287_SINGLE_PRIOR_REARM_MAX_SOL", "6.50")),
    )
    ap.add_argument(
        "--enable-two-prior-buy-lane",
        action="store_true",
        default=os.environ.get("V287_ENABLE_TWO_PRIOR_BUY_LANE", "0") == "1",
    )
    ap.add_argument(
        "--two-prior-current-min-sol",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_CURRENT_MIN_SOL", "2.00")),
    )
    ap.add_argument(
        "--two-prior-current-max-sol",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_CURRENT_MAX_SOL", "3.25")),
    )
    ap.add_argument(
        "--two-prior-prev-buy-sol-min",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_PREV_BUY_SOL_MIN", "5.00")),
    )
    ap.add_argument(
        "--two-prior-prev-buy-sol-max",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_PREV_BUY_SOL_MAX", "10.00")),
    )
    ap.add_argument(
        "--two-prior-top-share-min",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_TOP_SHARE_MIN", "0.55")),
    )
    ap.add_argument(
        "--two-prior-top-share-max",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_TOP_SHARE_MAX", "0.80")),
    )
    ap.add_argument(
        "--two-prior-rearm-min-sol",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_REARM_MIN_SOL", "1.00")),
    )
    ap.add_argument(
        "--two-prior-rearm-max-sol",
        type=float,
        default=float(os.environ.get("V287_TWO_PRIOR_REARM_MAX_SOL", "4.50")),
    )
    ap.add_argument(
        "--enable-oversize-train-lane",
        action="store_true",
        default=(
            os.environ.get("V287_ENABLE_OVERSIZE_TRAIN_LANE", "0") == "1"
            and os.environ.get("V287_ALLOW_OVERSIZE_TRAIN_LIVE_RISK", "0") == "1"
        ),
    )
    ap.add_argument("--oversize-train-min-buys", type=int, default=int(os.environ.get("V287_OVERSIZE_TRAIN_MIN_BUYS", "3")))
    ap.add_argument("--oversize-train-min-sol", type=float, default=float(os.environ.get("V287_OVERSIZE_TRAIN_MIN_SOL", "5.00")))
    ap.add_argument("--oversize-train-max-age-ms", type=int, default=int(os.environ.get("V287_OVERSIZE_TRAIN_MAX_AGE_MS", "350")))
    ap.add_argument(
        "--enable-low-top-lane",
        action="store_true",
        default=os.environ.get("V287_ENABLE_LOW_TOP_LANE", "0") == "1",
    )
    ap.add_argument(
        "--enable-fresh-impulse-lane",
        action="store_true",
        default=os.environ.get("V287_ENABLE_FRESH_IMPULSE_LANE", "0") == "1",
    )
    ap.add_argument("--min-profit-lamports", type=int, default=int(os.environ.get("V287_MIN_PROFIT_LAMPORTS", "1000000")))
    ap.add_argument("--min-reserve-sol", type=float, default=float(os.environ.get("V287_MIN_RESERVE_SOL", "0.040")))
    ap.add_argument("--sender-tip-lamports", type=int, default=int(os.environ.get("PGG2_V75_TIP_LAMPORTS", "5000")))
    ap.add_argument("--priority-fee-sol", type=float, default=float(os.environ.get("V287_PRIORITY_FEE_SOL", "0.000005")))
    ap.add_argument("--buy-slippage-pct", type=float, default=float(os.environ.get("V287_BUY_SLIPPAGE_PCT", "2.50")))
    ap.add_argument("--sell-slippage-pct", type=float, default=float(os.environ.get("V287_SELL_SLIPPAGE_PCT", "4.00")))
    ap.add_argument("--emergency-sell-slippage-pct", type=float, default=float(os.environ.get("V287_EMERGENCY_SELL_SLIPPAGE_PCT", "15.0")))
    ap.add_argument("--sell-fee-est-lamports", type=int, default=int(os.environ.get("V287_SELL_FEE_EST_LAMPORTS", "30000")))
    ap.add_argument("--scratch-profit-lamports", type=int, default=int(os.environ.get("V287_SCRATCH_PROFIT_LAMPORTS", "0")))
    ap.add_argument("--max-hold-ms", type=int, default=int(os.environ.get("V287_MAX_HOLD_MS", "900")))
    ap.add_argument("--sell-poll-ms", type=int, default=int(os.environ.get("V287_SELL_POLL_MS", "90")))
    ap.add_argument("--min-buy-quote-tokens", type=float, default=float(os.environ.get("V287_MIN_BUY_QUOTE_TOKENS", "500000")))
    ap.add_argument("--shadow-miss-ms", type=int, default=int(os.environ.get("V287_SHADOW_MISS_MS", "3000")))
    ap.add_argument("--shadow-miss-max", type=int, default=int(os.environ.get("V287_SHADOW_MISS_MAX", "128")))
    ap.add_argument(
        "--skip-startup-token-check",
        action="store_true",
        default=os.environ.get("V287_SKIP_STARTUP_TOKEN_CHECK", "0") == "1",
    )
    args = ap.parse_args()

    token = os.environ.get(str(args.token_env), "")
    if not token:
        _log("PGG2-V287-FATAL missing_publicnode_token")
        return 2
    if int(args.sender_tip_lamports) != 5000:
        _log(f"PGG2-V287-FATAL sender_tip_not_exact_5000 tip={args.sender_tip_lamports}")
        return 2
    if float(args.size_sol) <= 0 or float(args.size_sol) > 0.035:
        _log(f"PGG2-V287-FATAL size_outside_smoke_limit size={args.size_sol}")
        return 2
    if (
        (
            bool(args.enable_oversize_train_lane)
            or os.environ.get("V287_ENABLE_OVERSIZE_TRAIN_LANE", "0") == "1"
        )
        and os.environ.get("V287_ALLOW_OVERSIZE_TRAIN_LIVE_RISK", "0") != "1"
    ):
        _log(
            "PGG2-V287-FATAL oversize_train_requires_risk_override "
            "reason=BSAq_live_loss_replay"
        )
        return 2
    if (
        os.environ.get("V287_ENABLE_CONTINUATION_CREDIT", "0") != "0"
        and os.environ.get("V287_ALLOW_CONTINUATION_CREDIT_LIVE_RISK", "0") != "1"
    ):
        _log(
            "PGG2-V287-FATAL continuation_credit_requires_risk_override "
            "reason=5HYj_BSAq_live_loss_replay"
        )
        return 2
    if os.environ.get("V287_DISABLE_NEGATIVE_MAXHOLD_SELL", "1") == "0":
        _log(
            "PGG2-V287-FATAL negative_maxhold_sell_enabled "
            "reason=live_losses_require_scratch_or_emergency_guard"
        )
        return 2
    if os.environ.get("V287_EMERGENCY_CLOSE_ON_NO_SCRATCH", "1") != "1":
        _log(
            "PGG2-V287-FATAL emergency_close_disabled "
            "reason=no_low_guard_run_left_open_token"
        )
        return 2
    if os.environ.get("V287_ALLOW_SNAPSHOT_COMPILE_FALLBACK_SEND", "0") == "1":
        _log(
            "PGG2-V287-FATAL snapshot_compile_fallback_send_enabled "
            "reason=stale_snapshot_caused_pump_6042_fee_burn"
        )
        return 2
    prebuy_min_self_roundtrip_delta = int(
        os.environ.get(
            "V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS",
            os.environ.get("V287_PREBUY_MIN_SELF_ROUNDTRIP_DELTA_LAMPORTS", "-1000000"),
        )
    )
    if prebuy_min_self_roundtrip_delta < -1_250_000:
        _log(
            "PGG2-V287-FATAL prebuy_self_roundtrip_floor_too_loose "
            f"min_delta={prebuy_min_self_roundtrip_delta} floor=-1250000"
        )
        return 2

    before = _wallet_lamports(str(args.rpc_url))
    if args.skip_startup_token_check:
        nonzero, rent_locked = 0, 0
        _log("PGG2-V287-STARTUP-TOKEN-CHECK-SKIP reason=external_preflight_required")
    else:
        nonzero, rent_locked = _token_accounts(str(args.rpc_url))
    needed = int((float(args.size_sol) + float(args.min_reserve_sol)) * LAMPORTS_PER_SOL) + ATA_RENT_LAMPORTS + 80_000
    _log(
        "PGG2-V287-STATE "
        f"wallet_sol={before/LAMPORTS_PER_SOL:.9f} nonzero_tokens={nonzero} "
        f"rent_locked_empty={rent_locked} needed_lamports={needed}"
    )
    if nonzero or rent_locked:
        _log("PGG2-V287-FATAL wallet_not_clean")
        return 2
    if before < needed:
        _log(f"PGG2-V287-FATAL insufficient_wallet_lamports have={before} need={needed}")
        return 2

    broker, sender = _build_broker(args)
    try:
        warm_start = _now_ms()
        broker.pump_global()
        broker.latest_blockhash(force=True)
        _log(f"PGG2-V287-HOT-PATH-WARM-STARTUP ms={_now_ms()-warm_start}")
    except Exception as exc:
        _log(
            "PGG2-V287-HOT-PATH-WARM-STARTUP-FAIL "
            f"err={type(exc).__name__}:{str(exc)[:160]}"
        )
    _log(
        "PGG2-V287-SELECTED-BAND-CONFIG "
        f"lane=c5_j3_winner_fingerprint current=[{float(args.current_min_sol):.2f},{float(args.current_max_sol):.2f}] "
        f"prev_sol_1s=[{float(args.prev_sol_min_1s):.2f},{float(args.prev_sol_max_1s):.2f}] "
        f"top_share=[{float(args.top_share_min):.2f},{float(args.top_share_max):.2f}] "
        f"normal_top>={float(args.top_share_normal_min):.2f} "
        f"rearm_buy_sol=[{float(args.rearm_min_sol):.2f},{float(args.rearm_max_sol):.2f}] "
        f"low_top_enabled={int(bool(args.enable_low_top_lane))} "
        f"low_top_rearm_buy_sol=[{float(args.low_top_rearm_min_sol):.2f},{float(args.low_top_rearm_max_sol):.2f}] "
        f"fresh_impulse_enabled={int(bool(args.enable_fresh_impulse_lane))} "
        f"fresh_current=[{float(args.fresh_impulse_current_min_sol):.2f},{float(args.fresh_impulse_current_max_sol):.2f}] "
        f"fresh_prev_buy_max={float(args.fresh_impulse_prev_buy_max_sol):.2f} "
        f"fresh_rearm_buy_sol=[{float(args.fresh_impulse_rearm_min_sol):.2f},{float(args.fresh_impulse_rearm_max_sol):.2f}] "
        f"fresh_max_rearm_buys={int(args.fresh_impulse_max_rearm_buys)} "
        f"high_current_rearm_min_buys={int(args.high_current_rearm_min_buys)} "
        f"single_prior_enabled={int(bool(args.enable_single_prior_buy_lane))} "
        f"single_prior_current=[{float(args.single_prior_current_min_sol):.2f},{float(args.single_prior_current_max_sol):.2f}] "
        f"single_prior_prev_buy_sol=[{float(args.single_prior_prev_buy_sol_min):.2f},{float(args.single_prior_prev_buy_sol_max):.2f}] "
        f"single_prior_rearm_sol=[{float(args.single_prior_rearm_min_sol):.2f},{float(args.single_prior_rearm_max_sol):.2f}] "
        f"two_prior_enabled={int(bool(args.enable_two_prior_buy_lane))} "
        f"two_prior_current=[{float(args.two_prior_current_min_sol):.2f},{float(args.two_prior_current_max_sol):.2f}] "
        f"two_prior_prev_buy_sol=[{float(args.two_prior_prev_buy_sol_min):.2f},{float(args.two_prior_prev_buy_sol_max):.2f}] "
        f"two_prior_top_share=[{float(args.two_prior_top_share_min):.2f},{float(args.two_prior_top_share_max):.2f}] "
        f"two_prior_rearm_sol=[{float(args.two_prior_rearm_min_sol):.2f},{float(args.two_prior_rearm_max_sol):.2f}] "
        f"oversize_train_enabled={int(bool(args.enable_oversize_train_lane))} "
        f"oversize_train_risk_override={int(os.environ.get('V287_ALLOW_OVERSIZE_TRAIN_LIVE_RISK', '0') == '1')} "
        f"oversize_train_min_buys={int(args.oversize_train_min_buys)} "
        f"oversize_train_min_sol={float(args.oversize_train_min_sol):.2f} "
        f"oversize_train_max_age_ms={int(args.oversize_train_max_age_ms)} "
        f"continuation_credit_enabled={int(os.environ.get('V287_ENABLE_CONTINUATION_CREDIT', '0') != '0')} "
        f"negative_maxhold_sell_disabled={int(os.environ.get('V287_DISABLE_NEGATIVE_MAXHOLD_SELL', '1') != '0')} "
        f"emergency_close_on_no_scratch={int(os.environ.get('V287_EMERGENCY_CLOSE_ON_NO_SCRATCH', '1') == '1')} "
        f"size_sol={args.size_sol:.6f} target_lamports={args.min_profit_lamports} "
        f"prebuy_min_self_roundtrip_delta={prebuy_min_self_roundtrip_delta} "
        f"min_buy_quote_tokens={float(args.min_buy_quote_tokens):.6f} "
        f"shadow_miss_ms={int(args.shadow_miss_ms)}"
    )

    stub = geyser_pb2_grpc.GeyserStub(grpc.secure_channel(str(args.endpoint), grpc.ssl_channel_credentials()))
    metadata = [(str(args.metadata_key), token)]
    hist: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=256))
    active: dict[str, dict[str, Any]] = {}
    shadows: dict[str, dict[str, Any]] = {}
    seen_sigs: set[str] = set()
    counters: Counter[str] = Counter()
    start_time = time.time()
    prewarm_pool = ThreadPoolExecutor(max_workers=4)
    last_blockhash_warm_ms = 0
    blockhash_warm_future: Any = None
    last_global_warm_ms = 0
    global_warm_future: Any = None

    try:
        for update in stub.Subscribe(_request_iter(args), metadata=metadata):
            now = _now_ms()
            blockhash_warm_ms = int(os.environ.get("V287_BACKGROUND_BLOCKHASH_WARM_MS", "20000"))
            if (
                blockhash_warm_ms > 0
                and now - last_blockhash_warm_ms >= blockhash_warm_ms
                and (blockhash_warm_future is None or blockhash_warm_future.done())
            ):
                last_blockhash_warm_ms = now
                blockhash_warm_future = prewarm_pool.submit(broker.latest_blockhash, True)
                counters["background_blockhash_warm"] += 1
            global_warm_ms = int(os.environ.get("V287_BACKGROUND_GLOBAL_WARM_MS", "4000"))
            if (
                global_warm_ms > 0
                and now - last_global_warm_ms >= global_warm_ms
                and (global_warm_future is None or global_warm_future.done())
            ):
                last_global_warm_ms = now
                global_warm_future = prewarm_pool.submit(broker.pump_global)
                counters["background_global_warm"] += 1
            if time.time() - start_time > int(args.seconds):
                counters["timeout"] += 1
                break
            counters["grpc_updates"] += 1
            rec = _decode_pump(update)
            if not rec:
                counters["grpc_non_pump_update"] += 1
                continue
            sig = str(rec["sig"])
            if sig in seen_sigs:
                counters["duplicate"] += 1
                continue
            seen_sigs.add(sig)
            counters[f"event_{rec['kind']}"] += 1
            mint = str(rec["mint"])
            _shadow_flush_expired(shadows, counters, now)
            _shadow_update(shadows, counters, rec, now)

            # Candidate watches are intentionally short-lived. Flush all stale
            # watches on every event so one dead mint cannot suppress fresh
            # impulse opportunities until that same mint happens to trade again.
            for active_mint, active_cand in list(active.items()):
                if now - int(active_cand["start_ms"]) <= _candidate_live_ttl_ms(active_cand):
                    continue
                counters["candidate_expired"] += 1
                active_rearm_min = int(
                    active_cand.get("rearm_min_lamports")
                    or int(float(args.rearm_min_sol) * LAMPORTS_PER_SOL)
                )
                active_rearm_max = int(active_cand.get("rearm_max_lamports") or 0)
                _log(
                    "PGG2-V287-CANDIDATE-EXPIRE "
                    f"mint={_short(active_mint)} pre_entry_buy_sol="
                    f"{active_cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                    f"rearm_min_sol={active_rearm_min/LAMPORTS_PER_SOL:.6f} "
                    f"rearm_max_sol={active_rearm_max/LAMPORTS_PER_SOL:.6f} "
                    f"ttl_ms={_candidate_live_ttl_ms(active_cand)} "
                    "reason=global_stale_flush"
                )
                active.pop(active_mint, None)

            # Manage one active pre-entry candidate.
            cand = active.get(mint)
            if cand:
                if now - int(cand["start_ms"]) > _candidate_live_ttl_ms(cand):
                    counters["candidate_expired"] += 1
                    cand_rearm_min_lamports = int(
                        cand.get("rearm_min_lamports") or int(float(args.rearm_min_sol) * LAMPORTS_PER_SOL)
                    )
                    _log(
                        "PGG2-V287-CANDIDATE-EXPIRE "
                        f"mint={_short(mint)} pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_min_sol={cand_rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"ttl_ms={_candidate_live_ttl_ms(cand)}"
                    )
                    active.pop(mint, None)
                elif rec["kind"] == "sell":
                    counters["candidate_abort_sell"] += 1
                    _log(f"PGG2-V287-CANDIDATE-ABORT mint={_short(mint)} reason=pre_entry_sell")
                    active.pop(mint, None)
                elif rec["kind"] == "buy":
                    cand["pre_entry_buys"] += 1
                    cand["pre_entry_buy_lamports"] += int(rec["sol_lamports"])
                    cand_rearm_min_lamports = int(cand.get("rearm_min_lamports") or int(float(args.rearm_min_sol) * LAMPORTS_PER_SOL))
                    cand_rearm_max_lamports = int(cand.get("rearm_max_lamports") or 0)
                    if cand_rearm_max_lamports > 0 and cand["pre_entry_buy_lamports"] > cand_rearm_max_lamports:
                        if str(cand.get("top_lane", "")) in {
                            "single_prior_buy_continuation",
                            "two_prior_buy_continuation",
                        }:
                            counters["lane_rearm_max_block"] += 1
                            _log(
                                "PGG2-V287-LANE-REARM-MAX-BLOCK "
                                f"mint={_short(mint)} top_lane={cand.get('top_lane', 'unknown')} "
                                f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"delay_ms={now-int(cand['start_ms'])}"
                            )
                            active.pop(mint, None)
                            continue
                        train_ok = (
                            bool(args.enable_oversize_train_lane)
                            and now - int(cand["start_ms"]) <= int(args.oversize_train_max_age_ms)
                            and int(cand["pre_entry_buys"]) >= int(args.oversize_train_min_buys)
                            and int(cand["pre_entry_buy_lamports"])
                            >= int(float(args.oversize_train_min_sol) * LAMPORTS_PER_SOL)
                        )
                        if train_ok:
                            counters["oversize_train_pass"] += 1
                            cand["top_lane"] = "oversize_train"
                            _log(
                                "PGG2-V287-OVERSIZE-TRAIN-PASS "
                                f"mint={_short(mint)} pre_entry_buys={int(cand['pre_entry_buys'])} "
                                f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                f"rearm_min_sol={cand_rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"delay_ms={now-int(cand['start_ms'])}"
                            )
                        else:
                            hot_train_min_buys = int(
                                os.environ.get("V287_VERIFIED_HOT_TRAIN_MIN_BUYS", "4")
                            )
                            hot_train_min_sol = float(
                                os.environ.get("V287_VERIFIED_HOT_TRAIN_MIN_SOL", "4.00")
                            )
                            hot_train_max_age_ms = int(
                                os.environ.get("V287_VERIFIED_HOT_TRAIN_MAX_AGE_MS", "350")
                            )
                            hot_train_prev_max_sol = float(
                                os.environ.get("V287_VERIFIED_HOT_TRAIN_PREV_MAX_SOL", "0.10")
                            )
                            hot_train_ok = (
                                os.environ.get("V287_ENABLE_VERIFIED_HOT_TRAIN", "1") != "0"
                                and str(cand.get("top_lane", "")) == "fresh_impulse"
                                and float(cand.get("prev_buy_sol") or 0.0)
                                <= hot_train_prev_max_sol
                                and now - int(cand["start_ms"]) <= hot_train_max_age_ms
                                and int(cand["pre_entry_buys"]) >= hot_train_min_buys
                                and cand["pre_entry_buy_lamports"]
                                >= int(hot_train_min_sol * LAMPORTS_PER_SOL)
                            )
                            if hot_train_ok:
                                counters["verified_hot_train_pass"] += 1
                                cand["verified_hot_train"] = 1
                                _log(
                                    "PGG2-V287-VERIFIED-HOT-TRAIN-PASS "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                    f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                    f"prev_buy_sol={float(cand.get('prev_buy_sol') or 0.0):.6f} "
                                    f"min_buys={hot_train_min_buys} min_sol={hot_train_min_sol:.6f} "
                                    f"delay_ms={now-int(cand['start_ms'])} source=live_shadow_miss_separator"
                                )
                            else:
                                counters["oversize_train_wait"] += 1
                                _log(
                                    "PGG2-V287-OVERSIZE-TRAIN-WAIT "
                                    f"mint={_short(mint)} pre_entry_buys={int(cand['pre_entry_buys'])} "
                                    f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                    f"need_buys={int(args.oversize_train_min_buys)} "
                                    f"need_sol={float(args.oversize_train_min_sol):.6f} "
                                    f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                                    f"delay_ms={now-int(cand['start_ms'])}"
                                )
                                continue
                    hot_train_flag = bool(cand.get("verified_hot_train"))
                    if (
                        str(cand.get("top_lane", "")) == "fresh_impulse"
                        and int(cand.get("pre_entry_buys") or 0)
                        > int(args.fresh_impulse_max_rearm_buys)
                        and not hot_train_flag
                    ):
                        counters["fresh_impulse_rearm_buys_block"] += 1
                        _log(
                            "PGG2-V287-FRESH-IMPULSE-REARM-BUYS-BLOCK "
                            f"mint={_short(mint)} pre_entry_buys={int(cand['pre_entry_buys'])} "
                            f"max_rearm_buys={int(args.fresh_impulse_max_rearm_buys)} "
                            f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                            f"delay_ms={now-int(cand['start_ms'])}"
                        )
                        active.pop(mint, None)
                        continue
                    if (
                        str(cand.get("top_lane", "")) == "fresh_impulse"
                        and int(cand.get("pre_entry_buys") or 0)
                        > int(args.fresh_impulse_max_rearm_buys)
                        and hot_train_flag
                    ):
                        _log(
                            "PGG2-V287-FRESH-IMPULSE-REARM-BUYS-ALLOW "
                            f"mint={_short(mint)} full_mint={mint} "
                            f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                            f"max_rearm_buys={int(args.fresh_impulse_max_rearm_buys)} "
                            "reason=verified_hot_train"
                        )
                    if cand["pre_entry_buys"] >= 1 and cand["pre_entry_buy_lamports"] >= cand_rearm_min_lamports:
                        cand.setdefault(
                            "first_rearm_pass_delay_ms",
                            max(0, now - int(cand["start_ms"])),
                        )
                        counters["rearm_pass"] += 1
                        _log(
                            "PGG2-V287-REARM-PASS "
                            f"mint={_short(mint)} pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                            f"rearm_min_sol={cand_rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                            f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                            f"top_lane={cand.get('top_lane', 'unknown')} "
                            f"delay_ms={now-int(cand['start_ms'])}"
                        )
                        require_post_plan_rearm = (
                            os.environ.get("V287_REQUIRE_POST_PLAN_REARM", "1") != "0"
                            and os.environ.get("V287_FAST_STATIC_FINAL_BUY", "1") != "0"
                        )
                        if require_post_plan_rearm:
                            plan_done, plan_ok_now, plan_err = _static_plan_future_done_ok(
                                cand.get("static_plan_future")
                            )
                            if not plan_ok_now:
                                if not plan_done:
                                    cand["post_plan_rearm_required"] = 1
                                    cand.setdefault("post_plan_rearm_wait_start_ms", now)
                                    cand["post_plan_rearm_base_lamports"] = int(
                                        cand.get("pre_entry_buy_lamports") or 0
                                    )
                                    cand["post_plan_rearm_base_buys"] = int(
                                        cand.get("pre_entry_buys") or 0
                                    )
                                    cand["post_plan_rearm_wait_last_ms"] = now
                                    counters["post_plan_rearm_wait"] += 1
                                    _log(
                                        "PGG2-V287-POST-PLAN-REARM-WAIT "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"top_lane={cand.get('top_lane', 'unknown')} "
                                        f"plan_ready=0 plan_state={plan_err} "
                                        f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                        f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                        "post_plan_buy_sol=0.000000 "
                                        f"delay_ms={now-int(cand['start_ms'])} "
                                        f"ttl_ms={_candidate_live_ttl_ms(cand)} "
                                        "reason=require_fresh_buy_after_static_plan_ready"
                                    )
                                    hist[mint].append(rec)
                                    continue
                                counters["post_plan_rearm_plan_fail"] += 1
                                _log(
                                    "PGG2-V287-POST-PLAN-REARM-BLOCK "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"plan_ready=0 plan_state={plan_err or 'done_false'} "
                                    "reason=static_plan_failed"
                                )
                                active.pop(mint, None)
                                continue
                            if cand.get("post_plan_rearm_required"):
                                post_plan_base_lamports = int(
                                    cand.get("post_plan_rearm_base_lamports") or 0
                                )
                                post_plan_base_buys = int(
                                    cand.get("post_plan_rearm_base_buys") or 0
                                )
                                post_plan_lamports = max(
                                    0,
                                    int(cand.get("pre_entry_buy_lamports") or 0)
                                    - post_plan_base_lamports,
                                )
                                post_plan_buys = max(
                                    0,
                                    int(cand.get("pre_entry_buys") or 0)
                                    - post_plan_base_buys,
                                )
                                post_plan_min_lamports = int(
                                    float(
                                        os.environ.get(
                                            "V287_POST_PLAN_REARM_MIN_SOL",
                                            f"{cand_rearm_min_lamports / LAMPORTS_PER_SOL:.9f}",
                                        )
                                    )
                                    * LAMPORTS_PER_SOL
                                )
                                if post_plan_buys < 1 or post_plan_lamports < post_plan_min_lamports:
                                    credit_wait_ms = now - int(
                                        cand.get("post_plan_rearm_wait_start_ms") or now
                                    )
                                    allow_preplan_credit = (
                                        os.environ.get("V287_ALLOW_PREPLAN_REARM_CREDIT", "1") != "0"
                                        and str(cand.get("top_lane", "")) == "fresh_impulse"
                                        and int(cand.get("pre_entry_buys") or 0) >= 1
                                        and int(cand.get("pre_entry_buy_lamports") or 0)
                                        >= cand_rearm_min_lamports
                                        and credit_wait_ms
                                        <= int(os.environ.get("V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS", "850"))
                                    )
                                    if allow_preplan_credit:
                                        counters["post_plan_preplan_credit_pass"] += 1
                                        _log(
                                            "PGG2-V287-POST-PLAN-REARM-CREDIT-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"top_lane={cand.get('top_lane', 'unknown')} "
                                            "plan_ready=1 plan_state=ready "
                                            f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                            f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                            f"post_plan_buys={post_plan_buys} "
                                            f"post_plan_buy_sol={post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                            f"post_plan_min_sol={post_plan_min_lamports/LAMPORTS_PER_SOL:.6f} "
                                            f"credit_wait_ms={credit_wait_ms} "
                                            f"credit_max_wait_ms={int(os.environ.get('V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS', '850'))} "
                                            f"adaptive_rearm_reason={cand.get('adaptive_rearm_reason', '-')}"
                                        )
                                        cand["post_plan_rearm_required"] = 0
                                    else:
                                        counters["post_plan_rearm_delta_wait"] += 1
                                        _log(
                                            "PGG2-V287-POST-PLAN-REARM-WAIT "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"top_lane={cand.get('top_lane', 'unknown')} "
                                            "plan_ready=1 plan_state=ready "
                                            f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                            f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                            f"post_plan_buys={post_plan_buys} "
                                            f"post_plan_buy_sol={post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                            f"post_plan_min_sol={post_plan_min_lamports/LAMPORTS_PER_SOL:.6f} "
                                            f"delay_ms={now-int(cand['start_ms'])} "
                                            f"ttl_ms={_candidate_live_ttl_ms(cand)} "
                                            "reason=post_plan_delta_below_rearm_min"
                                        )
                                        hist[mint].append(rec)
                                        continue
                                counters["post_plan_rearm_pass"] += 1
                                _log(
                                    "PGG2-V287-POST-PLAN-REARM-PASS "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"top_lane={cand.get('top_lane', 'unknown')} "
                                    f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                    f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                    f"post_plan_buys={post_plan_buys} "
                                    f"post_plan_buy_sol={post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                    f"post_plan_min_sol={post_plan_min_lamports/LAMPORTS_PER_SOL:.6f} "
                                    f"wait_ms={now-int(cand.get('post_plan_rearm_wait_start_ms') or now)} "
                                    f"delay_ms={now-int(cand['start_ms'])}"
                                )
                                cand["post_plan_rearm_required"] = 0
                        pair_ok = bool(cand.get("candidate_pair_ok"))
                        pair_ok = _remember_pair_from_feed_rec(broker, rec) or pair_ok
                        fut = cand.get("prewarm_future")
                        if not pair_ok and fut is not None:
                            try:
                                pair_ok = bool(fut.result(timeout=0.20))
                            except FuturesTimeout:
                                counters["pair_prewarm_wait_timeout"] += 1
                                _log(f"PGG2-V287-PAIR-PREWARM-WAIT-TIMEOUT mint={_short(mint)}")
                            except Exception as exc:
                                counters["pair_prewarm_future_exc"] += 1
                                _log(
                                    "PGG2-V287-PAIR-PREWARM-FUTURE-EXC "
                                    f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:120]}"
                                )
                        if not pair_ok:
                            pair_ok = _prewarm_pair_from_sigs(
                                broker,
                                mint,
                                [str(rec.get("sig") or ""), str(cand.get("candidate_sig") or "")],
                                attempts=2,
                            )
                        if not pair_ok:
                            counters["pair_prewarm_block"] += 1
                            active.pop(mint, None)
                            continue
                        try:
                            if os.environ.get("V287_FAST_STATIC_FINAL_BUY", "1") != "0":
                                plan_ready = False
                                plan_fut = cand.get("static_plan_future")
                                if plan_fut is not None:
                                    try:
                                        plan_ready = bool(
                                            plan_fut.result(
                                                timeout=float(os.environ.get("V287_FAST_PLAN_WAIT_SEC", "0.25"))
                                            )
                                        )
                                    except FuturesTimeout:
                                        counters["fast_static_plan_wait_timeout"] += 1
                                        _log(f"PGG2-V287-FAST-STATIC-PLAN-WAIT-TIMEOUT mint={_short(mint)}")
                                    except Exception as exc:
                                        counters["fast_static_plan_future_exc"] += 1
                                        _log(
                                            "PGG2-V287-FAST-STATIC-PLAN-FUTURE-EXC "
                                            f"mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:120]}"
                                        )
                                if not plan_ready:
                                    counters["fast_static_plan_miss"] += 1
                                    _log(
                                        "PGG2-V287-FAST-STATIC-PLAN-MISS "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        "reason=plan_not_ready fallback=snapshot_compile"
                                    )

                                fast_start_ms = _now_ms()
                                curve = broker.bonding_curve(as_pubkey(mint))
                                curve_ts_ms = _now_ms()
                                curve_ms = curve_ts_ms - fast_start_ms
                                continuation_model_ok = False
                                continuation_reason = ""
                                ok_proj, quote_tokens, _expected_raw = _prebuy_postbuy_sell_projection_from_curve(
                                    broker,
                                    mint,
                                    curve,
                                    args,
                                    log_tag="PGG2-V287-FAST-FINAL-PREBUY-CHECK",
                                )
                                if not ok_proj:
                                    continuation_ok = False
                                    observed_rearm_lamports = int(
                                        cand.get("pre_entry_buy_lamports") or 0
                                    )
                                    observed_rearm_sol = observed_rearm_lamports / LAMPORTS_PER_SOL
                                    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
                                    current_buy_sol = float(cand.get("current_buy_sol") or 0.0)
                                    prev_buy_sol = float(cand.get("prev_buy_sol") or 0.0)
                                    top_lane = str(cand.get("top_lane") or "")
                                    age_ms = _now_ms() - int(cand.get("start_ms") or _now_ms())
                                    first_rearm_delay_ms = int(
                                        cand.get("first_rearm_pass_delay_ms") or age_ms
                                    )
                                    min_verified_delay_ms = int(
                                        os.environ.get(
                                            "V287_VERIFIED_CONTINUATION_MIN_REARM_DELAY_MS",
                                            "75",
                                        )
                                    )
                                    max_verified_delay_ms = int(
                                        os.environ.get(
                                            "V287_VERIFIED_CONTINUATION_MAX_REARM_DELAY_MS",
                                            "350",
                                        )
                                    )
                                    paced_rearm = (
                                        min_verified_delay_ms
                                        <= first_rearm_delay_ms
                                        <= max_verified_delay_ms
                                    )
                                    verified_fresh_base = (
                                        top_lane == "fresh_impulse"
                                        and 2.80 <= current_buy_sol <= 3.25
                                        and 1 <= pre_entry_buys <= int(args.fresh_impulse_max_rearm_buys)
                                        and age_ms <= 350
                                        and paced_rearm
                                    )
                                    verified_low_rearm_continuation = (
                                        os.environ.get(
                                            "V287_ENABLE_VERIFIED_LOW_REARM_CONTINUATION",
                                            "1",
                                        )
                                        != "0"
                                        and verified_fresh_base
                                        and 0.70 <= observed_rearm_sol <= 1.65
                                    )
                                    verified_strong_fresh_rearm = (
                                        verified_fresh_base
                                        and prev_buy_sol <= 1e-12
                                        and observed_rearm_sol
                                        >= float(
                                            os.environ.get(
                                                "V287_VERIFIED_STRONG_FRESH_REARM_MIN_SOL",
                                                "3.80",
                                            )
                                        )
                                        and observed_rearm_sol
                                        <= float(args.fresh_impulse_rearm_max_sol)
                                    )
                                    verified_prior_carry_rearm = (
                                        verified_fresh_base
                                        and prev_buy_sol
                                        >= float(
                                            os.environ.get(
                                                "V287_VERIFIED_PRIOR_CARRY_PREV_MIN_SOL",
                                                "2.00",
                                            )
                                        )
                                        and observed_rearm_sol
                                        >= float(
                                            os.environ.get(
                                                "V287_VERIFIED_PRIOR_CARRY_REARM_MIN_SOL",
                                                "0.70",
                                            )
                                        )
                                        and observed_rearm_sol
                                        <= float(
                                            os.environ.get(
                                                "V287_VERIFIED_PRIOR_CARRY_REARM_MAX_SOL",
                                                "1.20",
                                            )
                                        )
                                    )
                                    verified_mid_carry_rearm = (
                                        verified_fresh_base
                                        and prev_buy_sol
                                        >= float(
                                            os.environ.get(
                                                "V287_VERIFIED_MID_CARRY_PREV_MIN_SOL",
                                                "1.00",
                                            )
                                        )
                                        and prev_buy_sol
                                        < float(
                                            os.environ.get(
                                                "V287_VERIFIED_MID_CARRY_PREV_MAX_SOL",
                                                "2.00",
                                            )
                                        )
                                        and observed_rearm_sol
                                        >= float(
                                            os.environ.get(
                                                "V287_VERIFIED_MID_CARRY_REARM_MIN_SOL",
                                                "2.00",
                                            )
                                        )
                                        and observed_rearm_sol
                                        <= float(
                                            os.environ.get(
                                                "V287_VERIFIED_MID_CARRY_REARM_MAX_SOL",
                                                "3.20",
                                            )
                                        )
                                    )
                                    verified_hot_train = (
                                        os.environ.get("V287_ENABLE_VERIFIED_HOT_TRAIN", "1")
                                        != "0"
                                        and top_lane == "fresh_impulse"
                                        and 2.80 <= current_buy_sol <= 3.25
                                        and prev_buy_sol
                                        <= float(
                                            os.environ.get(
                                                "V287_VERIFIED_HOT_TRAIN_PREV_MAX_SOL",
                                                "0.10",
                                            )
                                        )
                                        and pre_entry_buys
                                        >= int(
                                            os.environ.get(
                                                "V287_VERIFIED_HOT_TRAIN_MIN_BUYS",
                                                "4",
                                            )
                                        )
                                        and observed_rearm_sol
                                        >= float(
                                            os.environ.get(
                                                "V287_VERIFIED_HOT_TRAIN_MIN_SOL",
                                                "4.00",
                                            )
                                        )
                                        and age_ms
                                        <= int(
                                            os.environ.get(
                                                "V287_VERIFIED_HOT_TRAIN_MAX_AGE_MS",
                                                "350",
                                            )
                                        )
                                    )
                                    verified_high_current_rearm = (
                                        os.environ.get(
                                            "V287_ENABLE_HIGH_CURRENT_REARM_LANE",
                                            "1",
                                        )
                                        != "0"
                                        and verified_fresh_base
                                        and pre_entry_buys >= int(args.high_current_rearm_min_buys)
                                        and 1.65 < observed_rearm_sol <= 4.50
                                        and (
                                            verified_strong_fresh_rearm
                                            or verified_mid_carry_rearm
                                        )
                                    )
                                    if verified_high_current_rearm:
                                        continuation_ok = True
                                        continuation_model_ok = True
                                        continuation_reason = "high_current_rearm"
                                        counters["high_current_rearm_lane_pass"] += 1
                                        _log(
                                            "PGG2-V287-HIGH-CURRENT-REARM-LANE-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"current_buy_sol={current_buy_sol:.6f} "
                                            f"prev_buy_sol={prev_buy_sol:.6f} "
                                            f"pre_entry_buys={pre_entry_buys} "
                                            f"min_pre_entry_buys={int(args.high_current_rearm_min_buys)} "
                                            f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                            f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                            f"age_ms={age_ms} source=live_replay_separator"
                                        )
                                    if (
                                        not continuation_ok
                                        and (
                                            verified_strong_fresh_rearm
                                            or verified_prior_carry_rearm
                                            or verified_mid_carry_rearm
                                            or verified_hot_train
                                        )
                                    ):
                                        continuation_ok = True
                                        continuation_model_ok = True
                                        if verified_strong_fresh_rearm:
                                            continuation_reason = "strong_fresh_rearm"
                                        elif verified_prior_carry_rearm:
                                            continuation_reason = "prior_carry_rearm"
                                        elif verified_hot_train:
                                            continuation_reason = "hot_train"
                                        else:
                                            continuation_reason = "mid_carry_rearm"
                                        counters["verified_flow_continuation_pass"] += 1
                                        _log(
                                            "PGG2-V287-VERIFIED-FLOW-CONTINUATION-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"current_buy_sol={current_buy_sol:.6f} "
                                            f"prev_buy_sol={prev_buy_sol:.6f} "
                                            f"pre_entry_buys={pre_entry_buys} "
                                            f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                            f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                            f"age_ms={age_ms}"
                                        )
                                    if os.environ.get("V287_ENABLE_CONTINUATION_CREDIT", "0") != "0":
                                        credit_fraction = float(
                                            os.environ.get("V287_CONTINUATION_CREDIT_FRACTION", "0.50")
                                        )
                                        credit_cap_sol = float(
                                            os.environ.get("V287_CONTINUATION_CREDIT_MAX_SOL", "2.00")
                                        )
                                        observed_rearm_lamports = int(
                                            cand.get("pre_entry_buy_lamports") or 0
                                        )
                                        continuation_lamports = min(
                                            int(credit_cap_sol * LAMPORTS_PER_SOL),
                                            max(0, int(observed_rearm_lamports * credit_fraction)),
                                        )
                                        continuation_ok, continuation_delta = (
                                            _prebuy_continuation_credit_projection_from_curve(
                                                broker,
                                                mint,
                                                curve,
                                                args,
                                                continuation_lamports,
                                            )
                                            )
                                        if continuation_ok:
                                            continuation_model_ok = True
                                            continuation_reason = "modeled_credit"
                                            counters["continuation_credit_pass"] += 1
                                            _log(
                                                "PGG2-V287-CONTINUATION-CREDIT-PASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"observed_rearm_lamports={observed_rearm_lamports} "
                                                f"credit_lamports={continuation_lamports} "
                                                f"projected_delta={continuation_delta:+}"
                                            )
                                    elif verified_low_rearm_continuation and not continuation_ok:
                                        credit_fraction = float(
                                            os.environ.get(
                                                "V287_VERIFIED_CONTINUATION_CREDIT_FRACTION",
                                                "0.50",
                                            )
                                        )
                                        continuation_lamports = max(
                                            0,
                                            int(observed_rearm_lamports * credit_fraction),
                                        )
                                        continuation_ok, continuation_delta = (
                                            _prebuy_continuation_credit_projection_from_curve(
                                                broker,
                                                mint,
                                                curve,
                                                args,
                                                continuation_lamports,
                                            )
                                        )
                                        if continuation_ok:
                                            continuation_model_ok = True
                                            continuation_reason = "verified_low_rearm_credit"
                                            counters["verified_low_rearm_continuation_pass"] += 1
                                            _log(
                                                "PGG2-V287-VERIFIED-LOW-REARM-CONTINUATION-PASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"current_buy_sol={current_buy_sol:.6f} "
                                                f"pre_entry_buys={pre_entry_buys} "
                                                f"observed_rearm_lamports={observed_rearm_lamports} "
                                                f"credit_lamports={continuation_lamports} "
                                                f"projected_delta={continuation_delta:+} "
                                                f"age_ms={age_ms}"
                                            )
                                    if not continuation_ok:
                                        keep_unverified_fresh_watch = (
                                            os.environ.get(
                                                "V287_KEEP_UNVERIFIED_FRESH_WATCH",
                                                "1",
                                            )
                                            != "0"
                                            and top_lane == "fresh_impulse"
                                            and age_ms
                                            < int(
                                                os.environ.get(
                                                    "V287_KEEP_UNVERIFIED_FRESH_WATCH_MAX_MS",
                                                    "350",
                                                )
                                            )
                                            and (
                                                pre_entry_buys
                                                < int(
                                                    os.environ.get(
                                                        "V287_VERIFIED_HOT_TRAIN_MIN_BUYS",
                                                        "4",
                                                    )
                                                )
                                                or observed_rearm_sol
                                                < float(
                                                    os.environ.get(
                                                        "V287_VERIFIED_HOT_TRAIN_MIN_SOL",
                                                        "4.00",
                                                    )
                                                )
                                            )
                                        )
                                        if keep_unverified_fresh_watch:
                                            counters["unverified_fresh_watch_keep"] += 1
                                            _log(
                                                "PGG2-V287-UNVERIFIED-FRESH-WATCH-KEEP "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"current_buy_sol={current_buy_sol:.6f} "
                                                f"prev_buy_sol={prev_buy_sol:.6f} "
                                                f"pre_entry_buys={pre_entry_buys} "
                                                f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                                f"age_ms={age_ms} "
                                                "reason=wait_for_verified_hot_train_or_carry"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                        if (
                                            os.environ.get("V287_ENABLE_CONTINUATION_CREDIT", "0") == "0"
                                            and not verified_low_rearm_continuation
                                            and not verified_high_current_rearm
                                            and not verified_strong_fresh_rearm
                                            and not verified_prior_carry_rearm
                                            and not verified_mid_carry_rearm
                                            and not verified_hot_train
                                        ):
                                            _log(
                                                "PGG2-V287-CONTINUATION-CREDIT-DISABLED "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                "reason=live_false_positive_replay"
                                            )
                                        counters["prebuy_postbuy_sell_block"] += 1
                                        _log(
                                            "PGG2-V287-PREBUY-POSTBUY-SELL-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} source=fast_final_curve"
                                        )
                                        active.pop(mint, None)
                                        continue
                                min_quote_tokens = float(args.min_buy_quote_tokens)
                                _log(
                                    "PGG2-V287-BUY-QUOTE-VIABILITY "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                    f"pass={int(quote_tokens >= min_quote_tokens)} source=fast_final_curve"
                                )
                                if min_quote_tokens > 0 and quote_tokens < min_quote_tokens:
                                    counters["buy_quote_token_block"] += 1
                                    _log(
                                        "PGG2-V287-BUY-QUOTE-TOKEN-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                        "source=fast_final_curve"
                                    )
                                    active.pop(mint, None)
                                    continue

                                min_tokens_ui = quote_tokens * max(
                                    0.0, 1.0 - (float(args.buy_slippage_pct) / 100.0)
                                )
                                if (
                                    not plan_ready
                                    and plan_fut is not None
                                    and hasattr(
                                        broker,
                                        "build_fast_signed_buy_with_min_tokens_from_curve_snapshot",
                                    )
                                ):
                                    try:
                                        plan_ready = bool(
                                            plan_fut.result(
                                                timeout=float(
                                                    os.environ.get(
                                                        "V287_FAST_PLAN_RECHECK_WAIT_SEC",
                                                        "0.40",
                                                    )
                                                )
                                            )
                                        )
                                        if plan_ready:
                                            counters["fast_static_plan_recheck_ready"] += 1
                                            _log(
                                                "PGG2-V287-FAST-STATIC-PLAN-RECHECK-READY "
                                                f"mint={_short(mint)} full_mint={mint}"
                                            )
                                    except FuturesTimeout:
                                        counters["fast_static_plan_recheck_timeout"] += 1
                                        _log(
                                            "PGG2-V287-FAST-STATIC-PLAN-RECHECK-TIMEOUT "
                                            f"mint={_short(mint)} full_mint={mint}"
                                        )
                                    except Exception as exc:
                                        counters["fast_static_plan_recheck_exc"] += 1
                                        _log(
                                            "PGG2-V287-FAST-STATIC-PLAN-RECHECK-EXC "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"err={type(exc).__name__}:{str(exc)[:120]}"
                                        )
                                if plan_ready and os.environ.get(
                                    "V287_REFRESH_CURVE_AFTER_PLAN_RECHECK",
                                    "1",
                                ) != "0":
                                    pre_refresh_quote_tokens = float(quote_tokens)
                                    fast_start_ms = _now_ms()
                                    curve = broker.bonding_curve(as_pubkey(mint))
                                    curve_ts_ms = _now_ms()
                                    curve_ms = curve_ts_ms - fast_start_ms
                                    ok_proj, quote_tokens, _expected_raw = _prebuy_postbuy_sell_projection_from_curve(
                                        broker,
                                        mint,
                                        curve,
                                        args,
                                        log_tag="PGG2-V287-FAST-FINAL-PREBUY-REFRESH-CHECK",
                                    )
                                    if not ok_proj:
                                        if continuation_model_ok and quote_tokens > 0:
                                            counters["prebuy_refresh_projection_continuation_pass"] += 1
                                            _log(
                                                "PGG2-V287-PREBUY-REFRESH-CONTINUATION-PASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason or 'verified_flow'} "
                                                "self_roundtrip_negative=1"
                                            )
                                        else:
                                            counters["prebuy_refresh_projection_block"] += 1
                                            _log(
                                                "PGG2-V287-PREBUY-REFRESH-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} reason=projection"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    _log(
                                        "PGG2-V287-BUY-QUOTE-VIABILITY-REFRESH "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                        f"pass={int(quote_tokens >= min_quote_tokens)} source=fast_final_curve_refresh"
                                    )
                                    if min_quote_tokens > 0 and quote_tokens < min_quote_tokens:
                                        counters["buy_quote_token_refresh_block"] += 1
                                        _log(
                                            "PGG2-V287-BUY-QUOTE-TOKEN-REFRESH-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                            "source=fast_final_curve_refresh"
                                        )
                                        active.pop(mint, None)
                                        continue
                                    min_tokens_ui = quote_tokens * max(
                                        0.0,
                                        1.0 - (float(args.buy_slippage_pct) / 100.0),
                                    )
                                    final_refresh_drift_pct = (
                                        (
                                            (float(quote_tokens) - pre_refresh_quote_tokens)
                                            / pre_refresh_quote_tokens
                                        )
                                        * 100.0
                                        if pre_refresh_quote_tokens > 0
                                        else 0.0
                                    )
                                    min_abs_refresh_drift_pct = float(
                                        os.environ.get(
                                            "V287_MIN_FINAL_REFRESH_ABS_DRIFT_PCT",
                                            "0.05",
                                        )
                                    )
                                    _log(
                                        "PGG2-V287-FINAL-REFRESH-DRIFT-CHECK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"first_tokens={pre_refresh_quote_tokens:.6f} "
                                        f"refresh_tokens={float(quote_tokens):.6f} "
                                        f"drift_pct={final_refresh_drift_pct:+.3f} "
                                        f"min_abs_drift_pct={min_abs_refresh_drift_pct:.3f}"
                                    )
                                    if (
                                        min_abs_refresh_drift_pct > 0
                                        and abs(final_refresh_drift_pct)
                                        < min_abs_refresh_drift_pct
                                    ):
                                        counters["final_refresh_no_movement_block"] += 1
                                        _log(
                                            "PGG2-V287-FINAL-REFRESH-DRIFT-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"drift_pct={final_refresh_drift_pct:+.3f} "
                                            "reason=no_curve_movement_before_sender"
                                        )
                                        active.pop(mint, None)
                                        continue
                                if (
                                    not plan_ready
                                    and os.environ.get(
                                        "V287_ALLOW_SNAPSHOT_COMPILE_FALLBACK_SEND",
                                        "0",
                                    )
                                    != "1"
                                ):
                                    counters["snapshot_compile_fallback_block"] += 1
                                    _log(
                                        "PGG2-V287-SNAPSHOT-COMPILE-FALLBACK-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"snapshot_age_ms={max(0, _now_ms() - int(curve_ts_ms))} "
                                        "reason=prevent_pump_6042_fee_burn"
                                    )
                                    active.pop(mint, None)
                                    continue
                                build_start_ms = _now_ms()
                                if plan_ready and hasattr(
                                    broker, "build_fast_signed_buy_with_min_tokens_from_curve_snapshot"
                                ):
                                    buy_quote = broker.build_fast_signed_buy_with_min_tokens_from_curve_snapshot(
                                        mint,
                                        float(args.size_sol),
                                        min_tokens_ui,
                                        virtual_token_reserves=int(curve.virtual_token_reserves),
                                        virtual_sol_reserves=int(curve.virtual_sol_reserves),
                                        real_token_reserves=int(curve.real_token_reserves),
                                        real_sol_reserves=int(curve.real_sol_reserves),
                                        token_total_supply=int(curve.token_total_supply),
                                        complete=bool(curve.complete),
                                        creator="",
                                        is_mayhem=bool(getattr(curve, "is_mayhem", False)),
                                        cashback_enabled=bool(getattr(curve, "cashback_enabled", False)),
                                        snapshot_ts_ms=curve_ts_ms,
                                    )
                                else:
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
                                        snapshot_ts_ms=curve_ts_ms,
                                    )
                                build_ms = _now_ms() - build_start_ms
                                buy_quote_source = str(buy_quote.get("quote_source") or "")
                                buy_snapshot_ts_ms = int(buy_quote.get("snapshot_ts_ms") or curve_ts_ms or 0)
                                buy_snapshot_age_ms = (
                                    max(0, _now_ms() - buy_snapshot_ts_ms)
                                    if buy_snapshot_ts_ms > 0
                                    else 999999
                                )
                                max_buy_snapshot_age_ms = int(
                                    os.environ.get("V287_MAX_BUY_SNAPSHOT_AGE_MS", "120")
                                )
                                max_fast_buy_build_ms = int(
                                    os.environ.get("V287_MAX_FAST_BUY_BUILD_MS", "80")
                                )
                                if buy_quote_source != "v165_fast_precompiled_snapshot":
                                    counters["non_precompiled_buy_source_block"] += 1
                                    _log(
                                        "PGG2-V287-NON-PRECOMPILED-BUY-SOURCE-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"source={buy_quote_source or '-'} "
                                        "reason=prevent_stale_snapshot_fee_burn"
                                    )
                                    active.pop(mint, None)
                                    continue
                                if buy_snapshot_age_ms > max_buy_snapshot_age_ms:
                                    counters["buy_snapshot_age_block"] += 1
                                    _log(
                                        "PGG2-V287-BUY-SNAPSHOT-AGE-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"snapshot_age_ms={buy_snapshot_age_ms} "
                                        f"max_age_ms={max_buy_snapshot_age_ms}"
                                    )
                                    active.pop(mint, None)
                                    continue
                                if build_ms > max_fast_buy_build_ms:
                                    counters["fast_buy_build_time_block"] += 1
                                    _log(
                                        "PGG2-V287-FAST-BUY-BUILD-TIME-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"build_ms={build_ms} max_build_ms={max_fast_buy_build_ms}"
                                    )
                                    active.pop(mint, None)
                                    continue
                                if buy_quote.get("route") != "pump_bc":
                                    raise RuntimeError(f"route_not_pump_bc_fast:{buy_quote.get('route')}")
                                if not _validate_buy_creator_vault_from_creator(
                                    mint, str(buy_quote["txn"]), curve.creator
                                ):
                                    counters["creator_vault_block"] += 1
                                    _log(
                                        "PGG2-V287-CREATOR-VAULT-MISMATCH-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} source=fast_final_curve"
                                    )
                                    active.pop(mint, None)
                                    continue
                                signed_b64 = str(buy_quote.get("signed_txn") or buy_quote.get("txn") or "")
                                sig_preview = str(buy_quote.get("sig_preview") or "")
                                if not signed_b64:
                                    signed_b64, sig_preview = broker.sign_transaction(str(buy_quote["txn"]))
                                wallet_before_buy = _wallet_lamports(str(args.rpc_url))
                                _log(
                                    "PGG2-V287-BUY-SEND "
                                    f"mint={_short(mint)} size_sol={args.size_sol:.6f} "
                                    f"sig_preview={sig_preview[:24]} slippage={args.buy_slippage_pct:.2f} "
                                    f"source=fast_static_final curve_ms={curve_ms} build_ms={build_ms} "
                                    f"snapshot_age_ms={buy_snapshot_age_ms} quote_source={buy_quote_source}"
                                )
                                buy_sig = broker.send_signed(signed_b64)
                                early_sell = _maybe_sell_before_buy_confirm(
                                    broker, mint, wallet_before_buy, buy_sig, args
                                )
                                ok = broker.wait_confirmed(buy_sig)
                                if not ok and early_sell is None:
                                    counters["buy_failed_safe"] += 1
                                    _log(f"PGG2-V287-BUY-FAILED-SAFE mint={_short(mint)} sig={buy_sig}")
                                    active.pop(mint, None)
                                    continue
                                if ok:
                                    counters["buy_confirmed"] += 1
                                    _log(f"PGG2-V287-BUY-CONFIRMED mint={_short(mint)} sig={buy_sig}")
                                elif early_sell is not None:
                                    _log(
                                        "PGG2-V287-BUY-CONFIRM-LATE-AFTER-EARLY-SELL "
                                        f"mint={_short(mint)} sig={buy_sig}"
                                    )
                                if early_sell is not None:
                                    sell_sig, expected_out, min_needed = early_sell
                                else:
                                    token_raw = _wait_token_balance_raw(
                                        broker, mint, 2.5, commitment="confirmed"
                                    )
                                    _log(f"PGG2-V287-TOKEN-BALANCE mint={_short(mint)} token_raw={token_raw}")
                                    if token_raw <= 0:
                                        raise RuntimeError("buy_confirmed_but_no_token_balance")
                                    sell_sig, expected_out, min_needed = _sell_all_for_target(
                                        broker, mint, wallet_before_buy, buy_sig, args
                                    )
                                time.sleep(1.0)
                                final_wallet = _wallet_lamports(str(args.rpc_url))
                                nonzero2, rent2 = _token_accounts(str(args.rpc_url))
                                delta = final_wallet - wallet_before_buy
                                _log(
                                    "PGG2-V287-SMOKE-END "
                                    f"mint={_short(mint)} wallet_before={wallet_before_buy} "
                                    f"wallet_after={final_wallet} delta_lamports={delta:+} "
                                    f"sell_sig={sell_sig or '-'} expected_out={expected_out} min_needed={min_needed} "
                                    f"nonzero_tokens={nonzero2} rent_locked_empty={rent2}"
                                )
                                return 0 if delta >= 0 and nonzero2 == 0 else 1

                            wallet_before_buy = _wallet_lamports(str(args.rpc_url))
                            buy_quote = broker.build_buy(mint, float(args.size_sol), float(args.buy_slippage_pct))
                            if buy_quote.get("route") != "pump_bc":
                                raise RuntimeError(f"route_not_pump_bc:{buy_quote.get('route')}")
                            quote_tokens = _rate_float(buy_quote, "amountOut")
                            min_quote_tokens = float(args.min_buy_quote_tokens)
                            _log(
                                "PGG2-V287-BUY-QUOTE-VIABILITY "
                                f"mint={_short(mint)} full_mint={mint} "
                                f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                f"pass={int(quote_tokens >= min_quote_tokens)}"
                            )
                            if min_quote_tokens > 0 and quote_tokens < min_quote_tokens:
                                counters["buy_quote_token_block"] += 1
                                _log(
                                    "PGG2-V287-BUY-QUOTE-TOKEN-BLOCK "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f}"
                                )
                                active.pop(mint, None)
                                continue
                            if not _validate_buy_creator_vault(broker, mint, str(buy_quote["txn"])):
                                counters["creator_vault_retry"] += 1
                                _log(
                                    "PGG2-V287-CREATOR-VAULT-REBUILD "
                                    f"mint={_short(mint)} full_mint={mint} reason=first_build_mismatch"
                                )
                                time.sleep(0.035)
                                buy_quote = broker.build_buy(mint, float(args.size_sol), float(args.buy_slippage_pct))
                                if buy_quote.get("route") != "pump_bc":
                                    raise RuntimeError(f"route_not_pump_bc_after_rebuild:{buy_quote.get('route')}")
                                quote_tokens = _rate_float(buy_quote, "amountOut")
                                _log(
                                    "PGG2-V287-BUY-QUOTE-VIABILITY-REBUILD "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                    f"pass={int(quote_tokens >= min_quote_tokens)}"
                                )
                                if min_quote_tokens > 0 and quote_tokens < min_quote_tokens:
                                    counters["buy_quote_token_block_rebuild"] += 1
                                    _log(
                                        "PGG2-V287-BUY-QUOTE-TOKEN-BLOCK-REBUILD "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f}"
                                    )
                                    active.pop(mint, None)
                                    continue
                                if not _validate_buy_creator_vault(broker, mint, str(buy_quote["txn"])):
                                    counters["creator_vault_block"] += 1
                                    _log(
                                        "PGG2-V287-CREATOR-VAULT-MISMATCH-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} retry=1"
                                    )
                                    active.pop(mint, None)
                                    continue
                            if not _prebuy_postbuy_sell_projection_pass(
                                broker, mint, buy_quote, wallet_before_buy, args
                            ):
                                counters["prebuy_postbuy_sell_block"] += 1
                                _log(
                                    "PGG2-V287-PREBUY-POSTBUY-SELL-BLOCK "
                                    f"mint={_short(mint)} full_mint={mint}"
                                )
                                active.pop(mint, None)
                                continue
                            if os.environ.get("V287_SEND_BOUNDARY_REQUOTE", "1") != "0":
                                try:
                                    first_quote_tokens = float(quote_tokens)
                                    boundary_quote = broker.build_buy(
                                        mint, float(args.size_sol), float(args.buy_slippage_pct)
                                    )
                                    if boundary_quote.get("route") != "pump_bc":
                                        raise RuntimeError(
                                            f"boundary_route_not_pump_bc:{boundary_quote.get('route')}"
                                        )
                                    boundary_tokens = _rate_float(boundary_quote, "amountOut")
                                    drift_pct = (
                                        ((boundary_tokens - first_quote_tokens) / first_quote_tokens) * 100.0
                                        if first_quote_tokens > 0
                                        else 0.0
                                    )
                                    _log(
                                        "PGG2-V287-SEND-BOUNDARY-BUY-REQUOTE "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"first_tokens={first_quote_tokens:.6f} "
                                        f"boundary_tokens={boundary_tokens:.6f} "
                                        f"drift_pct={drift_pct:+.3f}"
                                    )
                                    max_neg_drift = float(
                                        os.environ.get("V287_MAX_BOUNDARY_NEG_DRIFT_PCT", "5.0")
                                    )
                                    if max_neg_drift > 0 and drift_pct < -max_neg_drift:
                                        counters["send_boundary_token_drift_block"] += 1
                                        _log(
                                            "PGG2-V287-SEND-BOUNDARY-TOKEN-DRIFT-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"drift_pct={drift_pct:+.3f} "
                                            f"max_neg_drift_pct={max_neg_drift:.3f}"
                                        )
                                        active.pop(mint, None)
                                        continue
                                    if (
                                        not _validate_buy_creator_vault(
                                            broker, mint, str(boundary_quote["txn"])
                                        )
                                    ):
                                        counters["boundary_creator_vault_retry"] += 1
                                        _log(
                                            "PGG2-V287-SEND-BOUNDARY-CREATOR-VAULT-REBUILD "
                                            f"mint={_short(mint)} full_mint={mint}"
                                        )
                                        time.sleep(0.025)
                                        boundary_quote = broker.build_buy(
                                            mint, float(args.size_sol), float(args.buy_slippage_pct)
                                        )
                                        if boundary_quote.get("route") != "pump_bc":
                                            raise RuntimeError(
                                                "boundary_route_not_pump_bc_after_rebuild:"
                                                f"{boundary_quote.get('route')}"
                                            )
                                        boundary_tokens = _rate_float(boundary_quote, "amountOut")
                                        if (
                                            not _validate_buy_creator_vault(
                                                broker, mint, str(boundary_quote["txn"])
                                            )
                                        ):
                                            counters["boundary_creator_vault_block"] += 1
                                            _log(
                                                "PGG2-V287-SEND-BOUNDARY-CREATOR-VAULT-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint}"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    if not _prebuy_postbuy_sell_projection_pass(
                                        broker, mint, boundary_quote, wallet_before_buy, args
                                    ):
                                        counters["send_boundary_projection_block"] += 1
                                        _log(
                                            "PGG2-V287-SEND-BOUNDARY-PROJECTION-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint}"
                                        )
                                        active.pop(mint, None)
                                        continue
                                    buy_quote = boundary_quote
                                    quote_tokens = boundary_tokens
                                except Exception as boundary_exc:
                                    counters["send_boundary_requote_error"] += 1
                                    _log(
                                        "PGG2-V287-SEND-BOUNDARY-REQUOTE-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"err={type(boundary_exc).__name__}:{str(boundary_exc)[:160]}"
                                    )
                                    active.pop(mint, None)
                                    continue
                            signed_b64, sig_preview = broker.sign_transaction(str(buy_quote["txn"]))
                            _log(
                                "PGG2-V287-BUY-SEND "
                                f"mint={_short(mint)} size_sol={args.size_sol:.6f} "
                                f"sig_preview={sig_preview[:24]} slippage={args.buy_slippage_pct:.2f}"
                            )
                            buy_sig = broker.send_signed(signed_b64)
                            early_sell = _maybe_sell_before_buy_confirm(
                                broker, mint, wallet_before_buy, buy_sig, args
                            )
                            ok = broker.wait_confirmed(buy_sig)
                            if not ok and early_sell is None:
                                counters["buy_failed_safe"] += 1
                                _log(f"PGG2-V287-BUY-FAILED-SAFE mint={_short(mint)} sig={buy_sig}")
                                active.pop(mint, None)
                                continue
                            if ok:
                                counters["buy_confirmed"] += 1
                                _log(f"PGG2-V287-BUY-CONFIRMED mint={_short(mint)} sig={buy_sig}")
                            elif early_sell is not None:
                                _log(
                                    "PGG2-V287-BUY-CONFIRM-LATE-AFTER-EARLY-SELL "
                                    f"mint={_short(mint)} sig={buy_sig}"
                                )
                            if early_sell is not None:
                                sell_sig, expected_out, min_needed = early_sell
                            else:
                                token_raw = _wait_token_balance_raw(
                                    broker, mint, 2.5, commitment="confirmed"
                                )
                                _log(f"PGG2-V287-TOKEN-BALANCE mint={_short(mint)} token_raw={token_raw}")
                                if token_raw <= 0:
                                    raise RuntimeError("buy_confirmed_but_no_token_balance")
                                sell_sig, expected_out, min_needed = _sell_all_for_target(
                                    broker, mint, wallet_before_buy, buy_sig, args
                                )
                            time.sleep(1.0)
                            final_wallet = _wallet_lamports(str(args.rpc_url))
                            nonzero2, rent2 = _token_accounts(str(args.rpc_url))
                            delta = final_wallet - wallet_before_buy
                            _log(
                                "PGG2-V287-SMOKE-END "
                                f"mint={_short(mint)} wallet_before={wallet_before_buy} "
                                f"wallet_after={final_wallet} delta_lamports={delta:+} "
                                f"sell_sig={sell_sig or '-'} expected_out={expected_out} min_needed={min_needed} "
                                f"nonzero_tokens={nonzero2} rent_locked_empty={rent2}"
                            )
                            return 0 if delta >= 0 and nonzero2 == 0 else 1
                        except Exception as exc:
                            counters["live_exception"] += 1
                            _log(f"PGG2-V287-LIVE-EXCEPTION mint={_short(mint)} err={type(exc).__name__}:{str(exc)[:240]}")
                            return 1

            hist[mint].append(rec)
            if rec["kind"] != "buy":
                continue
            current_buy_sol = int(rec["sol_lamports"]) / LAMPORTS_PER_SOL
            recent = [x for x in hist[mint] if now - int(x["recv_ms"]) <= 1000]
            prev_buys = [x for x in recent if x["kind"] == "buy" and x["sig"] != rec["sig"]]
            prev_sells = [x for x in recent if x["kind"] == "sell"]
            prev_buy_sol = sum(int(x["sol_lamports"]) for x in prev_buys) / LAMPORTS_PER_SOL
            by_sig: dict[str, int] = {}
            for x in prev_buys:
                by_sig[str(x.get("sig"))] = by_sig.get(str(x.get("sig")), 0) + int(x["sol_lamports"])
            top_share = max(by_sig.values()) / max(1, sum(by_sig.values())) if by_sig else 1.0
            current_ok_main = (
                float(args.current_min_sol) <= current_buy_sol <= float(args.current_max_sol)
            )
            current_ok_single_prior = (
                bool(args.enable_single_prior_buy_lane)
                and len(prev_buys) == 1
                and float(args.single_prior_current_min_sol)
                <= current_buy_sol
                <= float(args.single_prior_current_max_sol)
            )
            current_ok_two_prior = (
                bool(args.enable_two_prior_buy_lane)
                and len(prev_buys) == 2
                and float(args.two_prior_current_min_sol)
                <= current_buy_sol
                <= float(args.two_prior_current_max_sol)
            )
            if not (current_ok_main or current_ok_single_prior or current_ok_two_prior):
                counters["block_current_band"] += 1
                continue
            if len(prev_buys) < 3:
                single_prior_ok = (
                    bool(args.enable_single_prior_buy_lane)
                    and not active
                    and len(prev_buys) == 1
                    and len(prev_sells) == 0
                    and float(args.single_prior_current_min_sol)
                    <= current_buy_sol
                    <= float(args.single_prior_current_max_sol)
                    and float(args.single_prior_prev_buy_sol_min)
                    <= prev_buy_sol
                    <= float(args.single_prior_prev_buy_sol_max)
                    and top_share >= 0.80
                )
                if single_prior_ok:
                    top_lane = "single_prior_buy_continuation"
                    rearm_min_lamports = int(float(args.single_prior_rearm_min_sol) * LAMPORTS_PER_SOL)
                    rearm_max_lamports = int(float(args.single_prior_rearm_max_sol) * LAMPORTS_PER_SOL)
                    candidate_pair_ok = _remember_pair_from_feed_rec(broker, rec)
                    active[mint] = {
                        "mint": mint,
                        "start_ms": now,
                        "candidate_sig": sig,
                        "candidate_pair_ok": candidate_pair_ok,
                        "current_buy_sol": current_buy_sol,
                        "prev_buy_sol": prev_buy_sol,
                        "top_share": top_share,
                        "top_lane": top_lane,
                        "rearm_min_lamports": rearm_min_lamports,
                        "rearm_max_lamports": rearm_max_lamports,
                        "pre_entry_buys": 0,
                        "pre_entry_buy_lamports": 0,
                    }
                    active[mint]["prewarm_future"] = (
                        None
                        if candidate_pair_ok
                        else prewarm_pool.submit(_prewarm_pair_from_sigs, broker, mint, [sig])
                    )
                    active[mint]["static_plan_future"] = prewarm_pool.submit(
                        _prepare_static_buy_plan_from_feed_rec,
                        broker,
                        dict(rec),
                        float(args.size_sol),
                    )
                    counters["single_prior_buy_candidate_start"] += 1
                    counters["candidate_start"] += 1
                    _shadow_start(
                        shadows,
                        counters,
                        reason="single_prior_buy_candidate_watch",
                        rec=rec,
                        now_ms=now,
                        ttl_ms=int(args.shadow_miss_ms),
                        max_open=int(args.shadow_miss_max),
                        current_buy_sol=current_buy_sol,
                        prev_buys=len(prev_buys),
                        prev_buy_sol=prev_buy_sol,
                        prev_sells=len(prev_sells),
                        top_share=top_share,
                        top_lane=top_lane,
                        rearm_min_sol=rearm_min_lamports / LAMPORTS_PER_SOL,
                    )
                    _log(
                        "PGG2-V287-SINGLE-PRIOR-BUY-CANDIDATE "
                        f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                        f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                        "source=deep_shadow_clean_cluster"
                    )
                    continue
                two_prior_ok = (
                    bool(args.enable_two_prior_buy_lane)
                    and not active
                    and len(prev_buys) == 2
                    and len(prev_sells) == 0
                    and float(args.two_prior_current_min_sol)
                    <= current_buy_sol
                    <= float(args.two_prior_current_max_sol)
                    and float(args.two_prior_prev_buy_sol_min)
                    <= prev_buy_sol
                    <= float(args.two_prior_prev_buy_sol_max)
                    and float(args.two_prior_top_share_min)
                    <= top_share
                    <= float(args.two_prior_top_share_max)
                )
                if two_prior_ok:
                    top_lane = "two_prior_buy_continuation"
                    rearm_min_lamports = int(float(args.two_prior_rearm_min_sol) * LAMPORTS_PER_SOL)
                    rearm_max_lamports = int(float(args.two_prior_rearm_max_sol) * LAMPORTS_PER_SOL)
                    candidate_pair_ok = _remember_pair_from_feed_rec(broker, rec)
                    active[mint] = {
                        "mint": mint,
                        "start_ms": now,
                        "candidate_sig": sig,
                        "candidate_pair_ok": candidate_pair_ok,
                        "current_buy_sol": current_buy_sol,
                        "prev_buy_sol": prev_buy_sol,
                        "top_share": top_share,
                        "top_lane": top_lane,
                        "rearm_min_lamports": rearm_min_lamports,
                        "rearm_max_lamports": rearm_max_lamports,
                        "pre_entry_buys": 0,
                        "pre_entry_buy_lamports": 0,
                    }
                    active[mint]["prewarm_future"] = (
                        None
                        if candidate_pair_ok
                        else prewarm_pool.submit(_prewarm_pair_from_sigs, broker, mint, [sig])
                    )
                    active[mint]["static_plan_future"] = prewarm_pool.submit(
                        _prepare_static_buy_plan_from_feed_rec,
                        broker,
                        dict(rec),
                        float(args.size_sol),
                    )
                    counters["two_prior_buy_candidate_start"] += 1
                    counters["candidate_start"] += 1
                    _shadow_start(
                        shadows,
                        counters,
                        reason="two_prior_buy_candidate_watch",
                        rec=rec,
                        now_ms=now,
                        ttl_ms=int(args.shadow_miss_ms),
                        max_open=int(args.shadow_miss_max),
                        current_buy_sol=current_buy_sol,
                        prev_buys=len(prev_buys),
                        prev_buy_sol=prev_buy_sol,
                        prev_sells=len(prev_sells),
                        top_share=top_share,
                        top_lane=top_lane,
                        rearm_min_sol=rearm_min_lamports / LAMPORTS_PER_SOL,
                    )
                    _log(
                        "PGG2-V287-TWO-PRIOR-BUY-CANDIDATE "
                        f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                        f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                        "source=deep_shadow_clean_cluster"
                    )
                    continue
                fresh_impulse_ok = (
                    bool(args.enable_fresh_impulse_lane)
                    and not active
                    and len(prev_sells) == 0
                    and current_ok_main
                    and current_buy_sol >= float(args.fresh_impulse_current_min_sol)
                    and current_buy_sol <= float(args.fresh_impulse_current_max_sol)
                    and prev_buy_sol <= float(args.fresh_impulse_prev_buy_max_sol)
                )
                if fresh_impulse_ok:
                    top_lane = "fresh_impulse"
                    configured_rearm_min_sol = float(args.fresh_impulse_rearm_min_sol)
                    adaptive_rearm_min_sol = configured_rearm_min_sol
                    adaptive_rearm_reason = "configured"
                    prev_carry_min_sol = float(
                        os.environ.get("V287_FRESH_IMPULSE_PREV_CARRY_MIN_SOL", "2.00")
                    )
                    zero_prev_min_sol = float(
                        os.environ.get("V287_FRESH_IMPULSE_ZERO_PREV_MIN_REARM_SOL", "1.50")
                    )
                    if prev_buy_sol + 1e-12 < prev_carry_min_sol:
                        adaptive_rearm_min_sol = max(configured_rearm_min_sol, zero_prev_min_sol)
                        adaptive_rearm_reason = "zero_or_weak_prev_live_loss_floor"
                    else:
                        adaptive_rearm_reason = "prior_carry_live_win_floor"
                    rearm_min_lamports = int(adaptive_rearm_min_sol * LAMPORTS_PER_SOL)
                    rearm_max_lamports = int(float(args.fresh_impulse_rearm_max_sol) * LAMPORTS_PER_SOL)
                    candidate_pair_ok = _remember_pair_from_feed_rec(broker, rec)
                    active[mint] = {
                        "mint": mint,
                        "start_ms": now,
                        "candidate_sig": sig,
                        "candidate_pair_ok": candidate_pair_ok,
                        "current_buy_sol": current_buy_sol,
                        "prev_buy_sol": prev_buy_sol,
                        "prev_buys": len(prev_buys),
                        "top_share": top_share,
                        "top_lane": top_lane,
                        "adaptive_rearm_reason": adaptive_rearm_reason,
                        "rearm_min_lamports": rearm_min_lamports,
                        "rearm_max_lamports": rearm_max_lamports,
                        "pre_entry_buys": 0,
                        "pre_entry_buy_lamports": 0,
                    }
                    active[mint]["prewarm_future"] = (
                        None
                        if candidate_pair_ok
                        else prewarm_pool.submit(_prewarm_pair_from_sigs, broker, mint, [sig])
                    )
                    active[mint]["static_plan_future"] = prewarm_pool.submit(
                        _prepare_static_buy_plan_from_feed_rec,
                        broker,
                        dict(rec),
                        float(args.size_sol),
                    )
                    counters["fresh_impulse_candidate_start"] += 1
                    counters["candidate_start"] += 1
                    _shadow_start(
                        shadows,
                        counters,
                        reason="fresh_impulse_candidate_watch",
                        rec=rec,
                        now_ms=now,
                        ttl_ms=int(args.shadow_miss_ms),
                        max_open=int(args.shadow_miss_max),
                        current_buy_sol=current_buy_sol,
                        prev_buys=len(prev_buys),
                        prev_buy_sol=prev_buy_sol,
                        prev_sells=len(prev_sells),
                        top_share=top_share,
                        top_lane=top_lane,
                        rearm_min_sol=rearm_min_lamports / LAMPORTS_PER_SOL,
                    )
                    _log(
                        "PGG2-V287-FRESH-IMPULSE-CANDIDATE "
                        f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} "
                        f"configured_rearm_min_sol={configured_rearm_min_sol:.6f} "
                        f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"adaptive_rearm_reason={adaptive_rearm_reason}"
                    )
                    continue
                counters["block_prev_buys"] += 1
                _shadow_start(
                    shadows,
                    counters,
                    reason="prev_buys",
                    rec=rec,
                    now_ms=now,
                    ttl_ms=int(args.shadow_miss_ms),
                    max_open=int(args.shadow_miss_max),
                    current_buy_sol=current_buy_sol,
                    prev_buys=len(prev_buys),
                    prev_buy_sol=prev_buy_sol,
                    prev_sells=len(prev_sells),
                    top_share=top_share,
                )
                continue
            if not (float(args.prev_sol_min_1s) <= prev_buy_sol <= float(args.prev_sol_max_1s)):
                counters["block_prev_sol_band"] += 1
                _log(
                    "PGG2-V287-PREV-SOL-BAND-BLOCK "
                    f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                    f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                    f"top_share_1s={top_share:.4f} "
                    f"prev_min={float(args.prev_sol_min_1s):.4f} prev_max={float(args.prev_sol_max_1s):.4f}"
                )
                _shadow_start(
                    shadows,
                    counters,
                    reason="prev_sol_band",
                    rec=rec,
                    now_ms=now,
                    ttl_ms=int(args.shadow_miss_ms),
                    max_open=int(args.shadow_miss_max),
                    current_buy_sol=current_buy_sol,
                    prev_buys=len(prev_buys),
                    prev_buy_sol=prev_buy_sol,
                    prev_sells=len(prev_sells),
                    top_share=top_share,
                )
                continue
            if prev_sells:
                counters["block_prev_sells"] += 1
                _shadow_start(
                    shadows,
                    counters,
                    reason="prev_sells",
                    rec=rec,
                    now_ms=now,
                    ttl_ms=int(args.shadow_miss_ms),
                    max_open=int(args.shadow_miss_max),
                    current_buy_sol=current_buy_sol,
                    prev_buys=len(prev_buys),
                    prev_buy_sol=prev_buy_sol,
                    prev_sells=len(prev_sells),
                    top_share=top_share,
                )
                continue
            if not (float(args.top_share_min) <= top_share <= float(args.top_share_max)):
                counters["block_top_band"] += 1
                _log(
                    "PGG2-V287-TOP-BAND-BLOCK "
                    f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                    f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                    f"top_share_1s={top_share:.4f} "
                    f"top_min={float(args.top_share_min):.4f} top_max={float(args.top_share_max):.4f}"
                )
                _shadow_start(
                    shadows,
                    counters,
                    reason="top_band",
                    rec=rec,
                    now_ms=now,
                    ttl_ms=int(args.shadow_miss_ms),
                    max_open=int(args.shadow_miss_max),
                    current_buy_sol=current_buy_sol,
                    prev_buys=len(prev_buys),
                    prev_buy_sol=prev_buy_sol,
                    prev_sells=len(prev_sells),
                    top_share=top_share,
                )
                continue
            if top_share < float(args.top_share_normal_min):
                if not bool(args.enable_low_top_lane):
                    counters["block_top_band"] += 1
                    _log(
                        "PGG2-V287-TOP-BAND-BLOCK "
                        f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"top_share_1s={top_share:.4f} top_min={float(args.top_share_normal_min):.4f} "
                        "reason=low_top_lane_disabled"
                    )
                    _shadow_start(
                        shadows,
                        counters,
                        reason="low_top_disabled",
                        rec=rec,
                        now_ms=now,
                        ttl_ms=int(args.shadow_miss_ms),
                        max_open=int(args.shadow_miss_max),
                        current_buy_sol=current_buy_sol,
                        prev_buys=len(prev_buys),
                        prev_buy_sol=prev_buy_sol,
                        prev_sells=len(prev_sells),
                        top_share=top_share,
                    )
                    continue
                top_lane = "low_top_strong_rearm"
                rearm_min_lamports = int(float(args.low_top_rearm_min_sol) * LAMPORTS_PER_SOL)
                rearm_max_lamports = int(float(args.low_top_rearm_max_sol) * LAMPORTS_PER_SOL)
            else:
                top_lane = "normal_top"
                rearm_min_lamports = int(float(args.rearm_min_sol) * LAMPORTS_PER_SOL)
                rearm_max_lamports = int(float(args.rearm_max_sol) * LAMPORTS_PER_SOL)
            if active:
                counters["block_active_exists"] += 1
                _shadow_start(
                    shadows,
                    counters,
                    reason="active_exists",
                    rec=rec,
                    now_ms=now,
                    ttl_ms=int(args.shadow_miss_ms),
                    max_open=int(args.shadow_miss_max),
                    current_buy_sol=current_buy_sol,
                    prev_buys=len(prev_buys),
                    prev_buy_sol=prev_buy_sol,
                    prev_sells=len(prev_sells),
                    top_share=top_share,
                    top_lane=top_lane,
                    rearm_min_sol=rearm_min_lamports / LAMPORTS_PER_SOL,
                )
                continue
            candidate_pair_ok = _remember_pair_from_feed_rec(broker, rec)
            active[mint] = {
                "mint": mint,
                "start_ms": now,
                "candidate_sig": sig,
                "candidate_pair_ok": candidate_pair_ok,
                "current_buy_sol": current_buy_sol,
                "prev_buy_sol": prev_buy_sol,
                "top_share": top_share,
                "top_lane": top_lane,
                "rearm_min_lamports": rearm_min_lamports,
                "rearm_max_lamports": rearm_max_lamports,
                "pre_entry_buys": 0,
                "pre_entry_buy_lamports": 0,
            }
            active[mint]["prewarm_future"] = (
                None
                if candidate_pair_ok
                else prewarm_pool.submit(_prewarm_pair_from_sigs, broker, mint, [sig])
            )
            active[mint]["static_plan_future"] = prewarm_pool.submit(
                _prepare_static_buy_plan_from_feed_rec,
                broker,
                dict(rec),
                float(args.size_sol),
            )
            counters["candidate_start"] += 1
            _shadow_start(
                shadows,
                counters,
                reason="candidate_watch",
                rec=rec,
                now_ms=now,
                ttl_ms=int(args.shadow_miss_ms),
                max_open=int(args.shadow_miss_max),
                current_buy_sol=current_buy_sol,
                prev_buys=len(prev_buys),
                prev_buy_sol=prev_buy_sol,
                prev_sells=len(prev_sells),
                top_share=top_share,
                top_lane=top_lane,
                rearm_min_sol=rearm_min_lamports / LAMPORTS_PER_SOL,
            )
            _log(
                "PGG2-V287-CANDIDATE "
                f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                f"top_share_1s={top_share:.4f} top_lane={top_lane} "
                f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f}"
            )
    except grpc.RpcError as exc:
        _log(f"PGG2-V287-GRPC-ERROR code={exc.code()} details={str(exc.details())[:200]}")
        counters["grpc_error"] += 1
    finally:
        _shadow_flush_expired(shadows, counters, _now_ms(), status="final")
        for key in list(shadows.keys()):
            _shadow_emit_end(shadows, counters, key, _now_ms(), "final")
        try:
            prewarm_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            prewarm_pool.shutdown(wait=False)
        _log("PGG2-V287-FINAL " + " ".join(f"{k}={v}" for k, v in counters.most_common(80)))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
