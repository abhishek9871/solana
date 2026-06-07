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
from types import SimpleNamespace
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


def _feed_tx_failed(info: Any) -> tuple[bool, str]:
    try:
        meta = info.meta
        if hasattr(meta, "HasField") and meta.HasField("err"):
            err_txt = str(meta.err).replace("\n", " ").strip()
            return True, err_txt or "meta_err_present"
    except Exception as exc:
        return True, f"meta_err_check_failed:{type(exc).__name__}"
    return False, ""


def _feed_account_fingerprint(rec: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in (
        "fee_recipient",
        "creator_vault",
        "token_program",
        "buyback_recipient",
        "social_fee_pda",
    ):
        raw = str(rec.get(key) or "")
        if not raw:
            continue
        try:
            out[key] = str(as_pubkey(raw))
        except Exception:
            out[key] = raw
    return out


def _v287_plan_churn_fingerprint(fp: dict[str, str]) -> dict[str, str]:
    """Return the subset of account fields that can invalidate a static plan."""
    ignored: set[str] = set()
    if os.environ.get("V287_IGNORE_SOCIAL_FEE_PDA_FOR_PLAN_CHURN", "1") != "0":
        ignored.add("social_fee_pda")
    if os.environ.get("V287_IGNORE_FEE_RECIPIENT_FOR_PLAN_CHURN", "1") != "0":
        # Pump fee-recipient rotates between successful buy instructions for the
        # same mint. Treat creator_vault/token_program as invariant; fee
        # recipient is validated as a Pump account at send time but should not
        # reset static-plan readiness for the seed-prior lane.
        ignored.add("fee_recipient")
    return {k: v for k, v in dict(fp).items() if k not in ignored}


def _v287_live_creator_vault_cache_value(broker: Any, mint: str) -> str:
    try:
        cached = getattr(broker, "_v287_live_creator_vault_cache", {}).get(str(mint))
        if not cached:
            return ""
        _creator_pk, creator_vault = cached
        if not creator_vault:
            return ""
        return str(as_pubkey(str(creator_vault)))
    except Exception:
        return ""


def _v287_apply_live_creator_vault_to_fp(
    broker: Any,
    mint: str,
    fp: dict[str, str],
    *,
    source: str,
) -> dict[str, str]:
    live_creator_vault = _v287_live_creator_vault_cache_value(broker, mint)
    if not live_creator_vault:
        return fp
    old = str(fp.get("creator_vault") or "")
    try:
        old_norm = str(as_pubkey(old)) if old else ""
    except Exception:
        old_norm = old
    if old_norm and old_norm != live_creator_vault:
        _log(
            "PGG2-V287-FEED-CREATOR-VAULT-LIVE-PREFERRED "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"feed={_short(old_norm)} live={_short(live_creator_vault)}"
        )
    fp["creator_vault"] = live_creator_vault
    return fp


def _account_fp_change_text(old: dict[str, str], new: dict[str, str]) -> str:
    parts: list[str] = []
    for key in sorted(new):
        new_val = str(new.get(key) or "")
        old_val = str(old.get(key) or "")
        if new_val and old_val != new_val:
            parts.append(f"{key}:{_short(old_val) if old_val else '-'}->{_short(new_val)}")
    return ",".join(parts) if parts else "-"


def _validate_pump_buy_account_indexes(
    broker: Any,
    mint: str,
    txn_b64: str,
    source: str,
    creator: Any = "",
) -> bool:
    """Validate the actual Pump ix account order, not only key membership.

    Anchor constraint failures burn the base fee even when the buy does not
    land. The previous guard only checked that a creator-vault key was present
    somewhere in the transaction; Custom 2006 can still happen if that key is
    at the wrong Pump account index or was learned from a failed source tx.
    """
    try:
        pump_ix_accounts = _pump_buy_exact_sol_in_ix_accounts(txn_b64)
        if not pump_ix_accounts:
            _log(
                "PGG2-V287-PUMP-ACCOUNT-INDEX-BLOCK "
                f"mint={_short(mint)} full_mint={mint} source={source} "
                "field=pump_ix expected=buy_exact_sol_in actual=missing"
            )
            return False

        def acct(i: int) -> str:
            return pump_ix_accounts[i] if i < len(pump_ix_accounts) else ""

        expected: dict[int, tuple[str, str]] = {
            2: ("mint", str(as_pubkey(mint))),
            3: ("bonding_curve", str(pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(as_pubkey(mint))))),
            15: ("fee_program", str(PUMP_FEE_PROGRAM_ID)),
        }
        ignore_fee_recipient = (
            os.environ.get("V287_IGNORE_FEE_RECIPIENT_FOR_ACCOUNT_INDEX", "1") != "0"
        )
        ignore_social_fee_pda = (
            os.environ.get("V287_IGNORE_SOCIAL_FEE_PDA_FOR_ACCOUNT_INDEX", "1") != "0"
        )
        ignored_volatile_fields: list[str] = []
        if ignore_fee_recipient:
            ignored_volatile_fields.append("fee_recipient")
        if ignore_social_fee_pda:
            ignored_volatile_fields.append("social_fee_pda")
        fee_override = str(
            getattr(broker, "_pump_fee_recipient_override", {}).get(str(mint), "")
            or ""
        )
        if fee_override and not ignore_fee_recipient:
            expected[1] = ("fee_recipient", str(as_pubkey(fee_override)))
        token_owner = ""
        try:
            cached = getattr(broker, "_account_cache", {}).get(str(mint))
            if cached:
                token_owner = str((cached[1] or {}).get("owner") or "")
        except Exception:
            token_owner = ""
        if token_owner:
            expected[8] = ("token_program", str(as_pubkey(token_owner)))
        creator_vault_override = str(
            getattr(broker, "_pump_creator_vault_override", {}).get(str(mint), "")
            or ""
        )
        creator_vault = ""
        creator_vault_source = ""
        if not creator:
            try:
                curve = broker.bonding_curve(as_pubkey(mint))
                creator = getattr(curve, "creator", "")
            except Exception:
                creator = ""
        if creator:
            try:
                creator_pk = as_pubkey(str(creator))
                creator_vault = str(
                    pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(creator_pk))
                )
                creator_vault_source = "curve_creator"
            except Exception:
                creator_vault = ""
                creator_vault_source = ""
        if not creator_vault:
            live_creator_vault = _v287_live_creator_vault_cache_value(broker, mint)
            if live_creator_vault:
                creator_vault = live_creator_vault
                creator_vault_source = "live_creator_cache"
        if not creator_vault and creator_vault_override:
            creator_vault = creator_vault_override
            creator_vault_source = "override"
        if creator_vault:
            expected[9] = ("creator_vault", str(as_pubkey(creator_vault)))
            if creator_vault_override and creator_vault_source != "override":
                try:
                    override_norm = str(as_pubkey(creator_vault_override))
                    if override_norm != str(as_pubkey(creator_vault)):
                        _log(
                            "PGG2-V287-CREATOR-VAULT-CURVE-AUTHORITY-PREFERRED "
                            f"mint={_short(mint)} full_mint={mint} source={source} "
                            f"override={_short(override_norm)} "
                            f"curve_creator_expected={_short(creator_vault)}"
                        )
                except Exception:
                    pass
        pair_info = {}
        try:
            pair_info = broker.last_pair_info(mint)
        except Exception:
            pair_info = {}
        pair_recipient = str(pair_info.get("pair_recipient") or "")
        pair_social = str(pair_info.get("pair_social_fee_pda") or "")
        if pair_recipient:
            expected[16] = ("buyback_recipient", str(as_pubkey(pair_recipient)))
        if pair_social and not ignore_social_fee_pda:
            expected[17] = ("social_fee_pda", str(as_pubkey(pair_social)))

        for index, (field, exp) in sorted(expected.items()):
            actual = acct(index)
            if actual != exp:
                _log(
                    "PGG2-V287-PUMP-ACCOUNT-INDEX-BLOCK "
                    f"mint={_short(mint)} full_mint={mint} source={source} "
                    f"field={field} index={index} expected={_short(exp)} actual={_short(actual)} "
                    f"account_count={len(pump_ix_accounts)}"
                )
                return False
        _log(
            "PGG2-V287-PUMP-ACCOUNT-INDEX-CHECK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"account_count={len(pump_ix_accounts)} pass=1 "
            f"ignored_volatile={','.join(ignored_volatile_fields) if ignored_volatile_fields else '-'}"
        )
        return True
    except Exception as exc:
        _log(
            "PGG2-V287-PUMP-ACCOUNT-INDEX-BLOCK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"field=validator err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _validate_pump_buy_candidate_fingerprint(
    *,
    broker: Any,
    mint: str,
    txn_b64: str,
    cand: dict[str, Any],
    source: str,
) -> bool:
    """Verify the precompiled buy tx still matches the candidate's latest feed.

    Fast plan refreshes can happen several times while older prewarm futures are
    still running. The account-index validator reads broker.last_pair_info(),
    but the fast builder sets that from the plan, so a stale plan can validate
    against itself. This compares the decoded tx against the candidate-local
    latest account fingerprint instead.
    """
    fp = dict(cand.get("latest_static_account_fp") or {})
    if not fp:
        fp = dict(cand.get("candidate_static_account_fp") or {})
    fp = _v287_apply_live_creator_vault_to_fp(broker, mint, fp, source=source)
    field_indexes = {
        "fee_recipient": 1,
        "token_program": 8,
        "creator_vault": 9,
        "buyback_recipient": 16,
        "social_fee_pda": 17,
    }
    if os.environ.get("V287_IGNORE_FEE_RECIPIENT_FOR_CANDIDATE_FP", "1") != "0":
        field_indexes.pop("fee_recipient", None)
    if os.environ.get("V287_IGNORE_SOCIAL_FEE_PDA_FOR_CANDIDATE_FP", "1") != "0":
        field_indexes.pop("social_fee_pda", None)
    expected: dict[int, tuple[str, str]] = {}
    for field, index in field_indexes.items():
        raw = str(fp.get(field) or "")
        if not raw:
            continue
        try:
            expected[index] = (field, str(as_pubkey(raw)))
        except Exception:
            expected[index] = (field, raw)
    if not expected:
        _log(
            "PGG2-V287-PUMP-ACCOUNT-FINGERPRINT-CHECK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            "pass=1 reason=no_candidate_fingerprint"
        )
        return True
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(str(txn_b64)))
        msg = tx.message
        keys = [str(k) for k in msg.account_keys]
        pump_ix_accounts: list[str] = []
        for cix in msg.instructions:
            try:
                program_key = keys[int(cix.program_id_index)]
            except Exception:
                continue
            data = bytes(cix.data)
            if program_key == str(PUMP_PROGRAM_ID) and data.startswith(DISC_BUY_EXACT_SOL_IN):
                pump_ix_accounts = [
                    keys[int(account_idx)]
                    for account_idx in bytes(cix.accounts)
                    if int(account_idx) < len(keys)
                ]
                break
        if not pump_ix_accounts:
            _log(
                "PGG2-V287-PUMP-ACCOUNT-FINGERPRINT-BLOCK "
                f"mint={_short(mint)} full_mint={mint} source={source} "
                "field=pump_ix expected=buy_exact_sol_in actual=missing"
            )
            return False
        for index, (field, exp) in sorted(expected.items()):
            actual = pump_ix_accounts[index] if index < len(pump_ix_accounts) else ""
            if actual != exp:
                _log(
                    "PGG2-V287-PUMP-ACCOUNT-FINGERPRINT-BLOCK "
                    f"mint={_short(mint)} full_mint={mint} source={source} "
                    f"field={field} index={index} expected={_short(exp)} "
                    f"actual={_short(actual)} account_count={len(pump_ix_accounts)}"
                )
                return False
        _log(
            "PGG2-V287-PUMP-ACCOUNT-FINGERPRINT-CHECK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"fields={','.join(field for field, _ in expected.values())} "
            f"account_count={len(pump_ix_accounts)} pass=1"
        )
        return True
    except Exception as exc:
        _log(
            "PGG2-V287-PUMP-ACCOUNT-FINGERPRINT-BLOCK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"field=validator err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _pump_buy_exact_sol_in_ix_accounts(txn_b64: str) -> list[str]:
    """Return the Pump buy_exact_sol_in account list from a serialized tx."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(str(txn_b64)))
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    for cix in msg.instructions:
        try:
            program_key = keys[int(cix.program_id_index)]
        except Exception:
            continue
        data = bytes(cix.data)
        if program_key == str(PUMP_PROGRAM_ID) and data.startswith(DISC_BUY_EXACT_SOL_IN):
            return [
                keys[int(account_idx)]
                for account_idx in bytes(cix.accounts)
                if int(account_idx) < len(keys)
            ]
    return []


def _live_creator_vault_from_rpc(
    broker: Any,
    mint: str,
    rpc_url: str,
) -> tuple[str, str, int, int, str]:
    """Fetch the authoritative curve creator and derived creator_vault.

    Returns (creator, creator_vault, fetch_ms, cache_hit, error). The cache is
    per mint because the bonding-curve creator is part of the curve account and
    should not change during the smoke.
    """
    started_ms = _now_ms()
    try:
        cache = getattr(broker, "_v287_live_creator_vault_cache", None)
        if cache is None:
            cache = {}
            setattr(broker, "_v287_live_creator_vault_cache", cache)
        cached = cache.get(str(mint))
        if cached:
            creator_pk, expected = cached
            return str(creator_pk), str(expected), 0, 1, ""

        mint_pk = as_pubkey(mint)
        curve_key = str(pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint_pk)))
        curve_data = b""
        last_err = ""
        for candidate_rpc in _read_rpc_urls(str(rpc_url)):
            try:
                # Creator-vault is part of Pump's account constraints. A
                # processed read can come from a losing fork and still pass our
                # local account-index checks, which burns the base fee with
                # Custom 2006. Use confirmed data for the final authority.
                info = _rpc_post_once(
                    candidate_rpc,
                    "getAccountInfo",
                    [curve_key, {"encoding": "base64", "commitment": "confirmed"}],
                    timeout=2.0,
                )
                value = (info or {}).get("value") or {}
                data_field = value.get("data") or []
                if isinstance(data_field, list) and data_field:
                    curve_data = base64.b64decode(data_field[0])
                elif isinstance(data_field, str):
                    curve_data = base64.b64decode(data_field)
                else:
                    curve_data = b""
                if len(curve_data) >= 81:
                    break
                last_err = (
                    f"curve_data_too_short endpoint={_rpc_label(candidate_rpc)} "
                    f"curve={_short(curve_key)} len={len(curve_data)}"
                )
                _log(
                    "PGG2-V287-LIVE-CREATOR-VAULT-RPC-MISS "
                    f"mint={_short(mint)} {last_err}"
                )
            except Exception as exc:
                last_err = (
                    f"{_rpc_label(candidate_rpc)}:{type(exc).__name__}:{str(exc)[:120]}"
                )
                _log(
                    "PGG2-V287-LIVE-CREATOR-VAULT-RPC-FAILOVER "
                    f"mint={_short(mint)} endpoint={_rpc_label(candidate_rpc)} "
                    f"err={type(exc).__name__}:{str(exc)[:120]}"
                )
                continue
        if len(curve_data) < 81:
            return "", "", _now_ms() - started_ms, 0, last_err or (
                f"curve_data_too_short curve={_short(curve_key)} len={len(curve_data)}"
            )
        creator_pk_obj = Pubkey.from_bytes(curve_data[49:81])
        if creator_pk_obj == Pubkey.default():
            return "", "", _now_ms() - started_ms, 0, "default_live_creator"
        creator_pk = str(creator_pk_obj)
        expected = str(pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(creator_pk_obj)))
        cache[str(mint)] = (creator_pk, expected)
        return creator_pk, expected, _now_ms() - started_ms, 0, ""
    except Exception as exc:
        return "", "", _now_ms() - started_ms, 0, (
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )


def _validate_pump_buy_live_creator_vault(
    *,
    broker: Any,
    mint: str,
    txn_b64: str,
    rpc_url: str,
    source: str,
    cand: dict[str, Any] | None = None,
) -> bool:
    """Validate creator_vault against the live bonding-curve creator.

    Feed-decoded account fingerprints are useful for speed, but Custom 2006 on
    Cvu7 proved they can self-validate a stale creator_vault and still burn the
    base fee. This final authority check intentionally ignores feed overrides:
    Pump constrains creator_vault to PDA("creator-vault", bonding_curve.creator).
    """
    started_ms = _now_ms()
    try:
        pump_ix_accounts = _pump_buy_exact_sol_in_ix_accounts(txn_b64)
        actual = pump_ix_accounts[9] if len(pump_ix_accounts) > 9 else ""
        if not actual:
            _log(
                "PGG2-V287-LIVE-CREATOR-VAULT-BLOCK "
                f"mint={_short(mint)} full_mint={mint} source={source} "
                f"reason=missing_tx_creator_vault account_count={len(pump_ix_accounts)}"
            )
            return False

        feed_creator_vault = ""
        if cand is not None:
            fp = dict(cand.get("latest_static_account_fp") or {})
            if not fp:
                fp = dict(cand.get("candidate_static_account_fp") or {})
            latest_rec = dict(
                cand.get("latest_static_account_rec")
                or cand.get("candidate_static_account_rec")
                or {}
            )
            feed_creator_vault = str(
                fp.get("creator_vault")
                or latest_rec.get("creator_vault")
                or ""
            )
            feed_kind = str(latest_rec.get("kind") or "")
            feed_sig = str(latest_rec.get("sig") or "")
            try:
                feed_creator_vault = (
                    str(as_pubkey(feed_creator_vault)) if feed_creator_vault else ""
                )
            except Exception:
                feed_creator_vault = ""
            default_pk = str(Pubkey.default())
            feed_matches_tx = (
                bool(feed_creator_vault)
                and feed_creator_vault != default_pk
                and feed_kind == "buy"
                and bool(feed_sig)
                and actual == feed_creator_vault
                and _validate_pump_buy_candidate_fingerprint(
                    broker=broker,
                    mint=mint,
                    txn_b64=txn_b64,
                    cand=cand,
                    source=f"{source}_successful_feed_creator_vault",
                )
            )
            _log(
                f"PGG2-V287-FEED-CREATOR-VAULT-FINAL-OBSERVE "
                f"mint={_short(mint)} full_mint={mint} "
                f"source={source} feed_kind={feed_kind or '-'} "
                f"feed_sig={_short(feed_sig) if feed_sig else '-'} "
                f"expected={_short(feed_creator_vault) if feed_creator_vault else '-'} "
                f"actual={_short(actual)} matches_tx={int(feed_matches_tx)} "
                "authority=observed_only_live_curve_required"
            )

        creator_pk, expected, fetch_ms, cache_hit, err = _live_creator_vault_from_rpc(
            broker,
            mint,
            rpc_url,
        )
        if not creator_pk or not expected:
            _log(
                "PGG2-V287-LIVE-CREATOR-VAULT-BLOCK "
                f"mint={_short(mint)} full_mint={mint} source={source} "
                f"reason={err or 'missing_live_creator_vault'} fetch_ms={fetch_ms}"
            )
            return False

        if feed_creator_vault and feed_creator_vault != expected:
            _log(
                "PGG2-V287-FEED-CREATOR-VAULT-LIVE-MISMATCH "
                f"mint={_short(mint)} full_mint={mint} source={source} "
                f"feed={_short(feed_creator_vault)} live={_short(expected)}"
            )

        ok = actual == expected
        log_name = (
            "PGG2-V287-LIVE-CREATOR-VAULT-CHECK"
            if ok
            else "PGG2-V287-LIVE-CREATOR-VAULT-BLOCK"
        )
        _log(
            f"{log_name} mint={_short(mint)} full_mint={mint} source={source} "
            f"creator={_short(creator_pk)} expected={_short(expected)} "
            f"actual={_short(actual)} pass={int(ok)} "
            f"fetch_ms={fetch_ms} cache_hit={cache_hit}"
        )
        return ok
    except Exception as exc:
        _log(
            "PGG2-V287-LIVE-CREATOR-VAULT-BLOCK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"reason=validator_error err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _validate_buy_creator_vault_from_key(
    mint: str,
    txn_b64: str,
    expected: str,
    source: str,
) -> bool:
    """Validate a known creator-vault account is present in the encoded buy tx."""
    try:
        expected_pk = str(as_pubkey(expected))
        keys = _txn_account_keys(txn_b64)
        ok = expected_pk in keys
        _log(
            "PGG2-V287-CREATOR-VAULT-CHECK "
            f"mint={_short(mint)} full_mint={mint} "
            f"expected={expected_pk} present={int(ok)} account_keys={len(keys)} "
            f"source={source}"
        )
        return ok
    except Exception as exc:
        _log(
            "PGG2-V287-CREATOR-VAULT-CHECK-FAIL "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _validate_buy_creator_vault_for_curve(
    broker: Any,
    mint: str,
    txn_b64: str,
    creator: Any,
) -> bool:
    """Validate against the curve-derived creator-vault when creator is known."""
    if creator and _validate_buy_creator_vault_from_creator(mint, txn_b64, creator):
        return True
    override = str(
        getattr(broker, "_pump_creator_vault_override", {}).get(str(mint), "")
        or ""
    )
    if override and _validate_buy_creator_vault_from_key(
        mint,
        txn_b64,
        override,
        "geyser_tx_creator_vault_override_fallback",
    ):
        return True
    return _validate_buy_creator_vault_from_creator(mint, txn_b64, creator)


def _validate_buy_creator_vault(broker: Any, mint: str, txn_b64: str) -> bool:
    """Fail closed before send if the buy transaction was built with a stale creator vault."""
    try:
        curve = broker.bonding_curve(as_pubkey(mint))
        creator = getattr(curve, "creator", "")
        if not creator:
            _log(
                "PGG2-V287-CREATOR-VAULT-CHECK-FAIL "
                f"mint={_short(mint)} full_mint={mint} "
                "err=missing_curve_creator_and_no_valid_override"
            )
            return False
        return _validate_buy_creator_vault_for_curve(broker, mint, txn_b64, creator)
    except Exception as exc:
        override = str(
            getattr(broker, "_pump_creator_vault_override", {}).get(str(mint), "")
            or ""
        )
        if override and _validate_buy_creator_vault_from_key(
            mint,
            txn_b64,
            override,
            "geyser_tx_creator_vault_override_no_curve_fallback",
        ):
            return True
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
    os.environ.setdefault("V287_CANDIDATE_TTL_MS", "1000")
    os.environ.setdefault("V287_POST_PLAN_REARM_TTL_MS", "1100")
    os.environ.setdefault("V287_BACKGROUND_BLOCKHASH_WARM_MS", "20000")
    os.environ.setdefault("V287_BACKGROUND_GLOBAL_WARM_MS", "4000")
    os.environ.setdefault("V287_MIN_FINAL_REFRESH_ABS_DRIFT_PCT", "0.05")
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_SPEED_NEGATIVE_PROJECTION_WATCH_MS", "220"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_SPEED_NEGATIVE_PROJECTION_MAX_WAITS", "1"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_SPEED_NEGATIVE_PROJECTION_MAX_AGE_MS", "1200"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_GENERIC_POSITIVE_WATCH_SEND_ENABLED", "0"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_GENERIC_WEAK_WATCH_SEND_ENABLED", "0"
    )
    try:
        _prebuy_floor = int(
            os.environ.get("V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS", "0") or 0
        )
    except Exception:
        _prebuy_floor = 0
    if _prebuy_floor < 0:
        _log(
            "PGG2-V287-PREBUY-FLOOR-CLAMP "
            f"old_min_projected_delta_lamports={_prebuy_floor} "
            "new_min_projected_delta_lamports=0 "
            "reason=selected_fingerprint_must_authorize_negative_roundtrip"
        )
        os.environ["V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS"] = "0"
    else:
        os.environ.setdefault("V287_PREBUY_MIN_PROJECTED_DELTA_LAMPORTS", "0")
    os.environ.setdefault("V287_FRESH_IMPULSE_ZERO_PREV_MIN_REARM_SOL", "1.50")
    os.environ.setdefault("V287_FRESH_IMPULSE_PREV_CARRY_MIN_SOL", "2.00")
    os.environ.setdefault("V287_ALLOW_PREPLAN_REARM_CREDIT", "1")
    os.environ.setdefault("V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS", "5")
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_ENABLED", "1"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_WAIT_MS", "900"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_CURRENT_MIN_SOL", "2.00"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_CURRENT_MAX_SOL", "2.05"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MIN_SOL", "1.05"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_SOL", "1.25"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_EXACT_BUYS", "1"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_FIRST_DELAY_MS", "80"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_POSTPLAN_SOL", "0.10"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_POSTPLAN_BUYS", "1"
    )
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MIN_TOP_SHARE", "0.999"
    )
    os.environ.setdefault("V287_ALLOW_FROZEN_INBAND_REARM_AFTER_PLAN_READY", "1")
    os.environ.setdefault("V287_FROZEN_INBAND_REARM_MAX_AGE_MS", "850")
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
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_MAX_AGE_MS", "1000")
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_PREV_MAX_SOL", "0.10")
    os.environ.setdefault("V287_VERIFIED_HOT_TRAIN_MAX_SEND_LAG_MS", "650")
    os.environ.setdefault("V287_ALLOW_NEGATIVE_SELF_ROUNDTRIP_CONTINUATION", "0")
    os.environ.setdefault("V287_ALLOW_SELECTED_NEGATIVE_ROUNDTRIP_FINGERPRINT", "1")
    os.environ.setdefault("V287_SELECTED_FRESH_LOW_MULTI_MIN_BUYS", "3")
    os.environ.setdefault("V287_SELECTED_FRESH_LOW_MULTI_MIN_DELAY_MS", "175")
    os.environ.setdefault("V287_SELECTED_FRESH_LOW_MULTI_MAX_DELAY_MS", "350")
    os.environ.setdefault("V287_SELECTED_FRESH_LOW_MULTI_MAX_SOL", "1.05")
    os.environ.setdefault("V287_SELECTED_FRESH_SINGLE_MID_MIN_SOL", "1.35")
    os.environ.setdefault("V287_SELECTED_FRESH_SINGLE_MID_MAX_SOL", "2.25")
    os.environ.setdefault("V287_SELECTED_FRESH_SINGLE_MID_MAX_DELAY_MS", "25")
    os.environ.setdefault("V287_SELECTED_FRESH_STRONG_MIN_SOL", "3.80")
    os.environ.setdefault("V287_SELECTED_FRESH_STRONG_MAX_SOL", "4.50")
    os.environ.setdefault("V287_SELECTED_FRESH_STRONG_MIN_DELAY_MS", "75")
    os.environ.setdefault("V287_SELECTED_FRESH_STRONG_MAX_DELAY_MS", "150")
    os.environ.setdefault("V287_SELECTED_FRESH_DENSE_TRAIN_MIN_BUYS", "4")
    os.environ.setdefault("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_BUYS", "5")
    os.environ.setdefault("V287_SELECTED_FRESH_DENSE_TRAIN_MIN_SOL", "2.30")
    os.environ.setdefault("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_SOL", "3.60")
    os.environ.setdefault("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_DELAY_MS", "75")
    os.environ.setdefault("V287_SELECTED_FRESH_INSTANT_DENSE_MIN_BUYS", "4")
    os.environ.setdefault("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_BUYS", "5")
    os.environ.setdefault("V287_SELECTED_FRESH_INSTANT_DENSE_MIN_SOL", "1.50")
    os.environ.setdefault("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_SOL", "4.50")
    os.environ.setdefault("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_DELAY_MS", "75")
    os.environ.setdefault("V287_NORMAL_TOP_CURRENT_MIN_SOL", "2.00")
    os.environ.setdefault("V287_NORMAL_TOP_CURRENT_MAX_SOL", "3.25")
    os.environ.setdefault("V287_EDGE_TOP_ENABLED", "1")
    os.environ.setdefault("V287_EDGE_TOP_MIN_SHARE", "0.50")
    os.environ.setdefault("V287_EDGE_TOP_REARM_MIN_SOL", "1.80")
    os.environ.setdefault("V287_EDGE_TOP_REARM_MAX_SOL", "3.20")
    os.environ.setdefault("V287_SELECTED_NORMAL_REARM_MIN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_NORMAL_REARM_MAX_SOL", "2.05")
    os.environ.setdefault("V287_SELECTED_NORMAL_MIN_DELAY_MS", "75")
    os.environ.setdefault("V287_SELECTED_NORMAL_MAX_DELAY_MS", "150")
    os.environ.setdefault("V287_SELECTED_MAX_NEG_REFRESH_DRIFT_PCT", "1.25")
    os.environ.setdefault("V287_SELECTED_BLOCK_ANY_NEG_REFRESH_DRIFT", "1")
    os.environ.setdefault("V287_SELECTED_ACCELERATION_NEG_REFRESH_DRIFT_PCT", "8.00")
    os.environ.setdefault("V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_NO_MOVEMENT_FOLLOWTHROUGH", "1")
    os.environ.setdefault("V287_BLOCK_ANY_NEGATIVE_NO_MOVEMENT", "1")
    os.environ.setdefault("V287_BLOCK_SINGLE_PRIOR_NEGATIVE_NO_MOVEMENT", "1")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_ALLOW_FLAT_REFRESH", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ALLOW_FLAT_REFRESH", "1")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_MIN_GUARD_MODE", "floor")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MIN_GUARD_MODE", "floor")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MIN_SOL", "4.50")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MAX_SOL", "10.00")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MAX_DELAY_MS", "1200")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MAX_BUYS", "6")
    os.environ.setdefault("V287_SELECTED_SINGLE_MID_MIN_QUOTE_TOKENS", "680000")
    os.environ.setdefault("V287_SELECTED_FRESH_SINGLE_MID_ACTUAL_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_FRESH_SINGLE_MID_MIN_GUARD_MODE", "floor")
    os.environ.setdefault("V287_SELECTED_FRESH_SINGLE_MID_MIN_TOKEN_HEADROOM_PCT", "5.00")
    os.environ.setdefault("V287_SELECTED_STRONG_MIN_QUOTE_TOKENS", "660000")
    os.environ.setdefault("V287_SELECTED_UPPER_MID_MULTI_MIN_QUOTE_TOKENS", "700000")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_MAX_QUOTE_TOKENS", "650000")
    os.environ.setdefault("V287_SELECTED_SINGLE_PRIOR_MIN_TOKEN_HEADROOM_PCT", "5.00")
    os.environ.setdefault("V287_ENABLE_SEED_PRIOR_CARRY_LANE", "1")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_CURRENT_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_CURRENT_MAX_SOL", "2.80")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_REARM_MIN_SOL", "0.70")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_REARM_MAX_SOL", "6.50")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_MIN_REARM_BUYS", "2")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_SINGLE_LARGE_REARM_MIN_SOL", "1.80")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_MAX_FIRST_DELAY_MS", "350")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_MAX_LAST_DELAY_MS", "350")
    os.environ.setdefault("V287_SEED_PRIOR_CARRY_TTL_MS", "1350")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_AUTHORITY_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MIN_QUOTE_TOKENS", "150000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_CURRENT_SOL", "2.65")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_PRE_ENTRY_SOL", "3.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_REARM_LAG_MS", "350")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MAX_AUTHORITY_LAG_MS", "1150")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_USE_EVENT_DELAY_LAG", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_ENABLED", "0")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_MIN_PCT", "1.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_MIN_QUOTE_TOKENS", "540000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_ZERODRIFT_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_TOKEN_HEADROOM_PCT", "4.90")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_POSTPLAN_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_POSTPLAN_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_MIN_POSTPLAN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_MIN_POSTPLAN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_ZERODRIFT_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_CURRENT_SOL", "1.95")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_CURRENT_SOL", "2.10")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_EXACT_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_SOL", "2.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_FIRST_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_LAST_DELAY_MS", "90")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_QUOTE_TOKENS", "500000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_QUOTE_TOKENS", "620000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_DRIFT_PCT", "0.05")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_ZERODRIFT_ENABLED", "0")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_CURRENT_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_CURRENT_MAX_SOL", "2.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_PRE_ENTRY_MIN_SOL", "2.40")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_PRE_ENTRY_MAX_SOL", "3.10")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MIN_SOL", "1.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_SOL", "1.60")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MIN_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_QUOTE_TOKENS", "850000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_FIRST_DELAY_MS", "60")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_LAST_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_SEND_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_SOL", "1.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_QUOTE_TOKENS", "540000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_PRE_ENTRY_SOL", "3.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_DRIFT_PCT", "-0.050")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MS", "350")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MAX_WAITS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MAX_AGE_MS", "1400")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_FOLLOW_MIN_SOL", "0.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_FOLLOW_MIN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_QUOTE_TOKENS", "795000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_QUOTE_TOKENS", "825000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_POSTPLAN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_POSTPLAN_SOL", "1.18")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_PRE_ENTRY_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_PRE_ENTRY_SOL", "2.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_EXACT_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_CURRENT_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_CURRENT_SOL", "2.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_RAW_REARM_LAG_MS", "350")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_REARM_DELAY_MS", "45")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_SEND_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_SOL", "1.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_QUOTE_TOKENS", "540000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_PRE_ENTRY_SOL", "2.15")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_PRE_ENTRY_SOL", "2.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_EXACT_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_PASS_AGE_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_NEG_DRIFT_PCT", "-20.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MIN_TOKEN_HEADROOM_PCT", "5.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PLAN_READY_WAIT_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CLEAN_MAX_QUOTE_TOKENS", "825000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CAP_MIN_PRE_ENTRY_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_CAP_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_QUOTE_TOKENS", "870000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_POSTPLAN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_POSTPLAN_SOL", "0.95")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_EXACT_POSTPLAN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_PRE_ENTRY_SOL", "1.40")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_PRE_ENTRY_SOL", "1.85")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_EXACT_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_CURRENT_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_CURRENT_SOL", "2.25")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_LAST_REARM_LAG_MS", "350")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_OVERRIDE_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_QUOTE_TOKENS", "925000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MIN_SOL", "2.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MIN_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_AGE_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_PRE_ENTRY_SOL", "6.60")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_CURRENT_SOL", "2.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_QUOTE_TOKENS", "925000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_POSTPLAN_SOL", "1.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_POSTPLAN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_PRE_ENTRY_SOL", "3.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_PRE_ENTRY_SOL", "3.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_CURRENT_SOL", "2.30")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOWTHROUGH_OVERRIDE_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MAX_AGE_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MAX_PRE_ENTRY_SOL", "7.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_DRIFT_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_DRIFT_MAX_PCT", "0.05")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_POSTPLAN_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_MIN_QUOTE_TOKENS", "700000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_MAX_POSTPLAN_AGE_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_MAX_LAST_REARM_LAG_MS", "450")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MAX_DRIFT_PCT", "2.25")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MIN_QUOTE_TOKENS", "500000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MAX_LAST_REARM_LAG_MS", "700")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_FOLLOWTHROUGH_MAX_AGE_MS", "450")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MAX_DRIFT_PCT", "0.05")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MIN_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MAX_QUOTE_TOKENS", "825000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_MIN_PRE_ENTRY_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_MAX_CURRENT_SOL", "2.80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_POSTPLAN_PRE_ENTRY_MIN_SOL", "4.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_POSTPLAN_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_POSTPLAN_MAX_QUOTE_TOKENS", "740000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MIN_SOL", "2.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MAX_SOL", "3.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MIN_QUOTE_TOKENS", "650000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MAX_QUOTE_TOKENS", "725000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_NEGATIVE_BYPASS_ENABLED", "0")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PROJECTION_BYPASS_MIN_SOL", "3.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PROJECTION_BYPASS_MIN_QUOTE_TOKENS", "620000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PROJECTION_BYPASS_MAX_QUOTE_TOKENS", "680000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_COMPRESSION_MIN_PRE_TOKENS", "700000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_COMPRESSION_MIN_REFRESH_TOKENS", "300000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_COMPRESSION_MAX_REFRESH_TOKENS", "450000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_COMPRESSION_MAX_RATIO", "0.55")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MIN_TOKENS", "470000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MIN_SOL", "1.20")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MAX_REFRESH_DROP_PCT", "0.25")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MAX_PASS_AGE_MS", "500")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MIN_TOKENS", "430000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_POSTPLAN_MIN_SOL", "2.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MIN_TOKENS", "330000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_TOKENS", "430000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MIN_PRE_ENTRY_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_PRE_ENTRY_SOL", "2.65")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MIN_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_BUYS", "3")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_REARM_DELAY_MS", "80")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_LAST_REARM_LAG_MS", "900")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MS", "950")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MIN_TOKENS", "470000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MIN_PRE_ENTRY_SOL", "4.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MIN_BUYS", "4")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MAX_REARM_DELAY_MS", "150")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MAX_LAST_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MAX_CURRENT_SOL", "2.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MIN_POSITIVE_DRIFT_PCT", "1.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_PCT", "0.30")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_PCT", "0.95")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_QUOTE_TOKENS", "600000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_QUOTE_TOKENS", "690000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_CURRENT_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_CURRENT_SOL", "2.25")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_PRE_ENTRY_SOL", "2.45")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_PRE_ENTRY_SOL", "3.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_EXACT_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_REARM_DELAY_MS", "90")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_LAST_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_DRIFT_PCT", "0.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_DRIFT_PCT", "1.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_QUOTE_TOKENS", "600000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_CURRENT_SOL", "2.25")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_PRE_ENTRY_SOL", "3.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_PRE_ENTRY_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_LAST_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_FOLLOWTHROUGH_MIN_TOP_SHARE", "0.35")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_FOLLOWTHROUGH_MAX_AGE_MS", "950")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_REQUIRE_POSTPLAN_ABOVE_SOL", "3.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_REQUIRED_POSTPLAN_MIN_SOL", "0.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POSTPLAN_ZERODRIFT_SEND_ENABLED", "0")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_REASON_RESTORE_POSTPLAN_MIN_SOL", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PLAN_REASON_RESTORE_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_PLAN_REASON_RESTORE_MAX_AGE_MS", "900")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_SOL", "0.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_SEND_ENABLED", "0")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MAX_AGE_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_TOP_SHARE", "0.999")
    os.environ.setdefault("V287_FAST_STATIC_PLAN_USE_FEED_ACCOUNTS_ONLY", "1")
    os.environ.setdefault("V287_FAST_STATIC_PLAN_LIVE_CREATOR_VAULT_PREWARM", "0")
    os.environ.setdefault("V287_TRUST_SUCCESSFUL_FEED_CREATOR_VAULT_FINAL", "0")
    os.environ.setdefault("V287_IGNORE_SOCIAL_FEE_PDA_FOR_PLAN_CHURN", "1")
    os.environ.setdefault("V287_IGNORE_FEE_RECIPIENT_FOR_PLAN_CHURN", "1")
    os.environ.setdefault("V287_IGNORE_FEE_RECIPIENT_FOR_CANDIDATE_FP", "1")
    os.environ.setdefault("V287_IGNORE_SOCIAL_FEE_PDA_FOR_CANDIDATE_FP", "1")
    os.environ.setdefault("V287_IGNORE_FEE_RECIPIENT_FOR_ACCOUNT_INDEX", "1")
    os.environ.setdefault("V287_IGNORE_SOCIAL_FEE_PDA_FOR_ACCOUNT_INDEX", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MIN_SOL", "0.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MIN_BUYS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MAX_POSTPLAN_SOL", "1.30")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MAX_AGE_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POS_REFRESH_WATCH_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POS_REFRESH_WATCH_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POS_REFRESH_WATCH_MAX_AGE_MS", "1400")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POS_REFRESH_WATCH_MAX_WAITS", "2")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_POS_REFRESH_FOLLOWTHROUGH_MAX_AGE_MS", "450")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_POSTPLAN_SEND_ENABLED", "0")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_MIN_DRIFT_PCT", "2.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_MIN_QUOTE_TOKENS", "680000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_REARM_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_PRE_ENTRY_SOL", "4.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_POSTPLAN_SOL", "1.30")
    os.environ.setdefault(
        "V287_SELECTED_SEED_PRIOR_ALLOW_DRIFT_ONLY_NEGATIVE_ROUNDTRIP",
        "1",
    )
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_BLOCK_HEAVY_CURRENT_REARM", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HEAVY_CURRENT_MIN_SOL", "2.70")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_HEAVY_REARM_MIN_SOL", "3.20")
    os.environ.setdefault("V287_SEED_PRIOR_SINGLE_STRONG_MIN_SOL", "3.00")
    os.environ.setdefault("V287_SEED_PRIOR_SINGLE_STRONG_MAX_SOL", "6.50")
    os.environ.setdefault("V287_SEED_PRIOR_SINGLE_STRONG_MAX_DELAY_MS", "75")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_MAX_QUOTE_TOKENS", "760000")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_MIN_TOKEN_HEADROOM_PCT", "5.00")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_MIN_GUARD_MODE", "floor")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_POSTPLAN_BRIDGE_ENABLED", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MIN_POSTPLAN_SOL", "1.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_POSTPLAN_SOL", "3.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MIN_POSTPLAN_BUYS", "1")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_PRE_ENTRY_SOL", "6.50")
    os.environ.setdefault("V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_LAG_MS", "650")
    os.environ.setdefault("V287_SELECTED_WEAK_DRIFT_KEEP_WATCH", "1")
    os.environ.setdefault("V287_SELECTED_WEAK_DRIFT_WATCH_MAX_AGE_MS", "900")
    os.environ.setdefault("V287_EVENT_BUY_MAX_SOL_SANITY", "20.00")
    os.environ.setdefault("V287_REFRESH_STATIC_PLAN_ON_REARM_ACCOUNTS", "1")
    os.environ.setdefault("V287_SEED_PRIOR_STATIC_PLAN_SYNC_RECOVER_ENABLED", "1")
    os.environ.setdefault("V287_SEED_PRIOR_STATIC_PLAN_CHURN_TTL_MS", "2200")
    os.environ.setdefault("V287_KEEP_UNVERIFIED_FRESH_WATCH", "1")
    os.environ.setdefault("V287_KEEP_UNVERIFIED_FRESH_WATCH_MAX_MS", "1000")
    os.environ.setdefault("V287_MAX_ACTIVE_CANDIDATES", "12")
    os.environ.setdefault("V287_SHADOW_CURRENT_BAND_REJECTS", "1")
    os.environ.setdefault("V287_ENABLE_FRESH_CLEAN_CARRY_RECLASS", "0")
    os.environ.setdefault("V287_FRESH_CLEAN_CARRY_MIN_BUYS", "3")
    os.environ.setdefault("V287_FRESH_CLEAN_CARRY_MIN_SOL", "3.50")
    os.environ.setdefault("V287_FRESH_CLEAN_CARRY_MAX_AGE_MS", "1100")
    os.environ.setdefault("V287_FRESH_CLEAN_CARRY_MAX_LAST_BUY_LAG_MS", "350")
    os.environ.setdefault("V287_SELECTED_FRESH_CLEAN_CARRY_MIN_QUOTE_TOKENS", "560000")
    os.environ.setdefault("V287_ENABLE_HIGH_CURRENT_CLEAN_TRAIN_LANE", "0")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_CURRENT_MIN_SOL", "3.30")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_CURRENT_MAX_SOL", "6.00")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_PREV_MAX_SOL", "4.50")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_TOP_MIN", "0.95")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_REARM_MIN_SOL", "3.00")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_REARM_MAX_SOL", "12.00")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_TTL_MS", "1000")
    os.environ.setdefault("V287_HIGH_CURRENT_TRAIN_ALLOW_FLAT_REFRESH", "1")
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
    tx_failed, tx_err = _feed_tx_failed(info)
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

        if tx_failed:
            _log(
                "PGG2-V287-FEED-FAILED-PUMP-TX-SKIP "
                f"mint={_short(acct(2))} sig={_short(sig)} "
                f"err={tx_err[:160] if tx_err else 'meta_err_present'}"
            )
            return None

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


def _v287_request_iter(args: argparse.Namespace) -> Iterator[geyser_pb2.SubscribeRequest]:
    """Subscribe to Pump txs plus Pump-owned curve account updates.

    The selected lane uses Geyser tx flow for speed. If the later RPC curve read
    lags that flow, a valid selected candidate can die at final prebuy. The
    account stream gives us a fresh curve snapshot without changing lane logic.
    """
    req = geyser_pb2.SubscribeRequest()
    tx = req.transactions["pump"]
    tx.vote = False
    tx.failed = False
    tx.account_include.append(PUMP_PROGRAM)
    if os.environ.get("V287_GEYSER_CURVE_ACCOUNT_CACHE", "1") != "0":
        acc = req.accounts["pump_curve_accounts"]
        acc.owner.append(PUMP_PROGRAM)
        acc.nonempty_txn_signature = True
    req.commitment = geyser_pb2.PROCESSED
    yield req
    ping_id = 1
    while True:
        time.sleep(max(5, int(args.ping_seconds)))
        ping = geyser_pb2.SubscribeRequest()
        ping.ping.id = ping_id
        ping_id += 1
        yield ping


def _v287_curve_from_account_update(
    update: geyser_pb2.SubscribeUpdate,
) -> tuple[str, Any, int, int] | None:
    if not update.HasField("account"):
        return None
    info = update.account.account
    data = bytes(info.data)
    if len(data) < 49:
        return None
    # Pump bonding-curve discriminator. Other Pump-owned accounts are not price
    # state and must not be used for buy/sell math.
    if data[:8] != bytes.fromhex("17b7f83760d8ac60"):
        return None
    curve_key = _pubkey(bytes(info.pubkey))
    if not curve_key:
        return None
    creator = Pubkey.default()
    if len(data) >= 81:
        creator = Pubkey.from_bytes(data[49:81])
    curve = SimpleNamespace(
        key=as_pubkey(curve_key),
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
    return curve_key, curve, int(update.account.slot), _now_ms()


def _v287_curve_key_for_mint(mint: str) -> str:
    return str(pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(as_pubkey(mint))))


def _v287_curve_from_geyser_cache(
    curve_cache_by_key: dict[str, dict[str, Any]],
    mint: str,
    *,
    max_age_ms: int,
) -> tuple[Any | None, int, int, int, str]:
    try:
        curve_key = _v287_curve_key_for_mint(mint)
        entry = curve_cache_by_key.get(curve_key)
        if not entry:
            return None, 0, 0, 0, "missing"
        ts_ms = int(entry.get("ts_ms") or 0)
        slot = int(entry.get("slot") or 0)
        age_ms = max(0, _now_ms() - ts_ms) if ts_ms > 0 else 999999
        if age_ms > int(max_age_ms):
            return None, ts_ms, slot, age_ms, "stale"
        curve = entry.get("curve")
        if curve is None:
            return None, ts_ms, slot, age_ms, "empty"
        return curve, ts_ms, slot, age_ms, "hit"
    except Exception as exc:
        return None, 0, 0, 0, f"{type(exc).__name__}:{str(exc)[:80]}"


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
    creator_vault_from_live = False
    fee_recipient = str(rec.get("fee_recipient") or "")
    token_program = str(rec.get("token_program") or "")
    try:
        live_creator_vault = _v287_live_creator_vault_cache_value(broker, mint)
        if live_creator_vault:
            try:
                feed_creator_vault = str(as_pubkey(creator_vault)) if creator_vault else ""
            except Exception:
                feed_creator_vault = creator_vault
            if feed_creator_vault and feed_creator_vault != live_creator_vault:
                _log(
                    "PGG2-V287-FEED-CREATOR-VAULT-LIVE-PREFERRED "
                    f"mint={_short(mint)} full_mint={mint} "
                    f"source=static_account_prewarm "
                    f"feed={_short(feed_creator_vault)} live={_short(live_creator_vault)}"
                )
            creator_vault = live_creator_vault
            creator_vault_from_live = True
        elif (
            creator_vault
            and os.environ.get("V287_FAST_STATIC_PLAN_USE_FEED_ACCOUNTS_ONLY", "0")
            == "0"
        ):
            _log(
                "PGG2-V287-FEED-CREATOR-VAULT-NOT-STORED "
                f"mint={_short(mint)} full_mint={mint} "
                f"feed={_short(creator_vault)} "
                "reason=confirmed_live_creator_vault_required"
            )
            creator_vault = ""
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
                f"token_program={_short(token_program)} "
                f"creator_vault_source={'live_confirmed' if creator_vault_from_live else 'feed'}"
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
        creator_pk = ""
        feed_creator_vault = str(rec.get("creator_vault") or "")
        try:
            feed_creator_vault = (
                str(as_pubkey(feed_creator_vault)) if feed_creator_vault else ""
            )
        except Exception:
            feed_creator_vault = ""
        creator_pk = ""
        live_creator_vault = ""
        live_prewarm_enabled = (
            os.environ.get(
                "V287_FAST_STATIC_PLAN_LIVE_CREATOR_VAULT_PREWARM", "1"
            )
            != "0"
        )
        if live_prewarm_enabled:
            creator_pk, live_creator_vault, fetch_ms, cache_hit, err = (
                _live_creator_vault_from_rpc(broker, mint, _default_rpc_url())
            )
            if creator_pk and live_creator_vault:
                if feed_creator_vault and feed_creator_vault != live_creator_vault:
                    _log(
                        "PGG2-V287-FAST-STATIC-PLAN-CREATOR-VAULT-LIVE-PREFERRED "
                        f"mint={_short(mint)} feed={_short(feed_creator_vault)} "
                        f"live={_short(live_creator_vault)} fetch_ms={fetch_ms} "
                        f"cache_hit={cache_hit}"
                    )
                getattr(broker, "_pump_creator_vault_override", {})[
                    mint
                ] = live_creator_vault
                _log(
                    "PGG2-V287-LIVE-CREATOR-VAULT-PREWARM "
                    f"mint={_short(mint)} creator={_short(creator_pk)} "
                    f"creator_vault={_short(live_creator_vault)} "
                    f"fetch_ms={fetch_ms} cache_hit={cache_hit} pass=1"
                )
            else:
                _log(
                    "PGG2-V287-LIVE-CREATOR-VAULT-PREWARM-BLOCKABLE "
                    f"mint={_short(mint)} reason={err or 'missing_live_creator_vault'} "
                    f"fetch_ms={fetch_ms} fallback=feed_creator_vault"
                )
        use_feed_only = (
            not creator_pk
            and os.environ.get("V287_FAST_STATIC_PLAN_USE_FEED_ACCOUNTS_ONLY", "0")
            != "0"
            and bool(feed_creator_vault)
            and feed_creator_vault != str(Pubkey.default())
        )
        if use_feed_only:
            _log(
                "PGG2-V287-FAST-STATIC-PLAN-FEED-ONLY "
                f"mint={_short(mint)} creator_vault={_short(feed_creator_vault)} "
                f"source=successful_geyser_pump_ix_account9 "
                "live_creator_vault_rpc=deferred_to_final_authority"
            )
        broker.prepare_pump_buy_static_plan(
            mint,
            float(amount_sol),
            creator=creator_pk if creator_pk else "",
        )
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


def _drop_static_buy_plan_for_mint_size(
    broker: Any, mint: str, amount_sol: float, reason: str
) -> bool:
    try:
        plans = getattr(broker, "_pump_buy_static_plans", {})
        spend_lamports = max(1, int(float(amount_sol) * LAMPORTS_PER_SOL))
        key = f"{str(mint)}:{int(spend_lamports)}"
        existed = key in plans
        if existed:
            plans.pop(key, None)
        _log(
            "PGG2-V287-FAST-STATIC-PLAN-DROP "
            f"mint={_short(str(mint))} size_sol={float(amount_sol):.6f} "
            f"existed={int(existed)} reason={reason}"
        )
        return bool(existed)
    except Exception as exc:
        _log(
            "PGG2-V287-FAST-STATIC-PLAN-DROP-FAIL "
            f"mint={_short(str(mint))} reason={reason} "
            f"err={type(exc).__name__}:{str(exc)[:120]}"
        )
        return False


def _refresh_static_plan_on_rearm_accounts(
    *,
    broker: Any,
    prewarm_pool: ThreadPoolExecutor,
    cand: dict[str, Any],
    rec: dict[str, Any],
    amount_sol: float,
    counters: Counter[str],
) -> None:
    if os.environ.get("V287_REFRESH_STATIC_PLAN_ON_REARM_ACCOUNTS", "1") == "0":
        return
    mint = str(rec.get("mint") or cand.get("mint") or "")
    if not mint:
        return
    new_fp = _feed_account_fingerprint(rec)
    if not new_fp:
        return
    new_fp = _v287_apply_live_creator_vault_to_fp(
        broker,
        mint,
        new_fp,
        source="rearm_account_fingerprint",
    )
    old_fp = dict(cand.get("latest_static_account_fp") or {})
    if not old_fp:
        old_fp = dict(cand.get("candidate_static_account_fp") or {})
    old_fp = _v287_apply_live_creator_vault_to_fp(
        broker,
        mint,
        old_fp,
        source="rearm_existing_fingerprint",
    )
    full_changes = _account_fp_change_text(old_fp, new_fp)
    changes = _account_fp_change_text(
        _v287_plan_churn_fingerprint(old_fp),
        _v287_plan_churn_fingerprint(new_fp),
    )
    if changes == "-":
        cand["static_plan_refresh_pending"] = 0
        cand["latest_static_account_fp"] = {
            **old_fp,
            **_v287_plan_churn_fingerprint(new_fp),
        }
        cand["latest_static_account_rec"] = dict(rec)
        if full_changes != "-":
            counters["rearm_static_plan_social_only_keep"] += 1
            _remember_static_accounts_from_feed_rec(broker, rec)
            _log(
                "PGG2-V287-REARM-STATIC-PLAN-SOCIAL-ONLY-KEEP "
                f"mint={_short(mint)} full_mint={mint} "
                f"changes={full_changes} "
                "reason=volatile_social_fee_pda_does_not_invalidate_plan"
            )
            return
        _remember_pair_from_feed_rec(broker, rec)
        _remember_static_accounts_from_feed_rec(broker, rec)
        return

    cand["latest_static_account_fp"] = {**old_fp, **new_fp}
    cand["latest_static_account_rec"] = dict(rec)
    counters["rearm_static_plan_account_change"] += 1
    cand["static_plan_refresh_pending"] = 1
    cand["static_plan_refresh_generation"] = int(
        cand.get("static_plan_refresh_generation") or 0
    ) + 1
    if str(cand.get("top_lane") or "") == "seed_prior_carry_continuation":
        cand["candidate_ttl_ms"] = max(
            int(cand.get("candidate_ttl_ms") or 0),
            int(os.environ.get("V287_SEED_PRIOR_STATIC_PLAN_CHURN_TTL_MS", "2200")),
        )
    old_fut = cand.get("static_plan_future")
    cancelled = 0
    try:
        if old_fut is not None and not old_fut.done():
            cancelled = int(bool(old_fut.cancel()))
    except Exception:
        cancelled = 0
    _drop_static_buy_plan_for_mint_size(
        broker,
        mint,
        float(amount_sol),
        "rearm_account_fingerprint_changed",
    )
    _remember_pair_from_feed_rec(broker, rec)
    _remember_static_accounts_from_feed_rec(broker, rec)
    cand["static_plan_future"] = prewarm_pool.submit(
        _prepare_static_buy_plan_from_feed_rec,
        broker,
        dict(rec),
        float(amount_sol),
    )
    cand["static_plan_refreshed_on_rearm_ts_ms"] = _now_ms()
    cand["static_plan_refreshed_on_rearm_changes"] = changes
    _log(
        "PGG2-V287-REARM-STATIC-PLAN-REFRESH "
        f"mint={_short(mint)} full_mint={mint} "
        f"changes={changes} old_future_cancelled={cancelled} "
        f"generation={int(cand.get('static_plan_refresh_generation') or 0)} "
        f"ttl_ms={_candidate_live_ttl_ms(cand)} "
        "reason=rearm_account_fingerprint_changed"
    )


def _static_plan_future_done_ok(plan_fut: Any) -> tuple[bool, bool, str]:
    if plan_fut is None:
        return False, False, "missing"
    try:
        if not plan_fut.done():
            return False, False, "pending"
        return True, bool(plan_fut.result(timeout=0)), ""
    except Exception as exc:
        return True, False, f"{type(exc).__name__}:{str(exc)[:120]}"


def _v287_try_seed_prior_static_plan_sync_recover(
    *,
    broker: Any,
    mint: str,
    cand: dict[str, Any],
    reason: str,
    amount_sol: float,
    counters: Counter[str],
    source: str,
    fallback_rec: dict[str, Any] | None = None,
) -> bool:
    """Rebuild a seed-prior static plan at the last safe boundary.

    This is not a fallback send path. It only repairs a missing/stale
    precompiled plan, then the normal account-index, fingerprint, live
    creator-vault, snapshot-age and Sender checks still have to pass.
    """
    if os.environ.get("V287_SEED_PRIOR_STATIC_PLAN_SYNC_RECOVER_ENABLED", "1") == "0":
        return False
    if not _v287_is_selected_seed_prior(cand, reason):
        return False
    latest_rec = dict(
        cand.get("latest_static_account_rec")
        or cand.get("candidate_static_account_rec")
        or fallback_rec
        or {}
    )
    if not latest_rec:
        counters["seed_prior_static_plan_sync_recover_block"] += 1
        _log(
            "PGG2-V287-SEED-PRIOR-STATIC-PLAN-RECOVER-BLOCK "
            f"mint={_short(mint)} full_mint={mint} reason={reason} "
            f"source={source} blocker=missing_latest_feed_rec"
        )
        return False
    counters["seed_prior_static_plan_sync_recover_attempt"] += 1
    _drop_static_buy_plan_for_mint_size(
        broker,
        mint,
        float(amount_sol),
        f"seed_prior_sync_recover_{source}",
    )
    ok = _prepare_static_buy_plan_from_feed_rec(
        broker,
        latest_rec,
        float(amount_sol),
    )
    cand["static_plan_future"] = None
    cand["static_plan_sync_recovered_ts_ms"] = _now_ms() if ok else 0
    if ok:
        counters["seed_prior_static_plan_sync_recover_pass"] += 1
        _log(
            "PGG2-V287-SEED-PRIOR-STATIC-PLAN-RECOVER-PASS "
            f"mint={_short(mint)} full_mint={mint} reason={reason} "
            f"source={source} latest_sig={str(latest_rec.get('sig') or '-')[:18]} "
            "next_step=normal_presend_validators"
        )
        return True
    counters["seed_prior_static_plan_sync_recover_block"] += 1
    _log(
        "PGG2-V287-SEED-PRIOR-STATIC-PLAN-RECOVER-BLOCK "
        f"mint={_short(mint)} full_mint={mint} reason={reason} "
        f"source={source} blocker=rebuild_failed"
    )
    return False


def _v287_rebuild_static_plan_with_confirmed_creator(
    *,
    broker: Any,
    mint: str,
    amount_sol: float,
    counters: Counter[str],
    source: str,
) -> bool:
    """Rebuild a Pump buy static plan from confirmed curve creator state.

    This specifically repairs creator_vault ConstraintSeeds failures. It does
    not authorize a send; the caller must rebuild the quote and re-run the
    normal account-index, fingerprint and live creator-vault checks.
    """
    counters["confirmed_creator_static_plan_rebuild_attempt"] += 1
    creator_pk, creator_vault, fetch_ms, cache_hit, err = _live_creator_vault_from_rpc(
        broker,
        mint,
        _default_rpc_url(),
    )
    if not creator_pk or not creator_vault:
        counters["confirmed_creator_static_plan_rebuild_block"] += 1
        _log(
            "PGG2-V287-CONFIRMED-CREATOR-STATIC-PLAN-REBUILD-BLOCK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"reason={err or 'missing_confirmed_creator_vault'} fetch_ms={fetch_ms}"
        )
        return False
    try:
        _drop_static_buy_plan_for_mint_size(
            broker,
            mint,
            float(amount_sol),
            f"confirmed_creator_rebuild_{source}",
        )
        getattr(broker, "_pump_creator_vault_override", {})[mint] = str(creator_vault)
        ok = bool(
            broker.prepare_pump_buy_static_plan(
                mint,
                float(amount_sol),
                creator=str(creator_pk),
            )
        )
        if not ok:
            counters["confirmed_creator_static_plan_rebuild_block"] += 1
            _log(
                "PGG2-V287-CONFIRMED-CREATOR-STATIC-PLAN-REBUILD-BLOCK "
                f"mint={_short(mint)} full_mint={mint} source={source} "
                "reason=prepare_failed"
            )
            return False
        counters["confirmed_creator_static_plan_rebuild_pass"] += 1
        _log(
            "PGG2-V287-CONFIRMED-CREATOR-STATIC-PLAN-REBUILD-PASS "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"creator={_short(creator_pk)} creator_vault={_short(creator_vault)} "
            f"fetch_ms={fetch_ms} cache_hit={cache_hit} "
            "next_step=normal_presend_validators"
        )
        return True
    except Exception as exc:
        counters["confirmed_creator_static_plan_rebuild_block"] += 1
        _log(
            "PGG2-V287-CONFIRMED-CREATOR-STATIC-PLAN-REBUILD-BLOCK "
            f"mint={_short(mint)} full_mint={mint} source={source} "
            f"err={type(exc).__name__}:{str(exc)[:160]}"
        )
        return False


def _candidate_live_ttl_ms(cand: dict[str, Any]) -> int:
    base = int(os.environ.get("V287_CANDIDATE_TTL_MS", "350"))
    base = max(base, int(cand.get("candidate_ttl_ms") or 0))
    if cand.get("post_plan_rearm_required"):
        base = max(base, int(os.environ.get("V287_POST_PLAN_REARM_TTL_MS", "1100")))
    if (
        str(cand.get("top_lane") or "") == "seed_prior_carry_continuation"
        and int(cand.get("static_plan_refresh_pending") or 0) == 1
    ):
        base = max(
            base,
            int(os.environ.get("V287_SEED_PRIOR_STATIC_PLAN_CHURN_TTL_MS", "2200")),
        )
    if cand.get("weak_drift_watch_keep"):
        base = max(
            base,
            int(os.environ.get("V287_SELECTED_WEAK_DRIFT_WATCH_MAX_AGE_MS", "900")),
        )
    if cand.get("no_movement_watch_keeps"):
        base = max(
            base,
            int(os.environ.get("V287_SELECTED_NO_MOVEMENT_WATCH_MAX_AGE_MS", "1200")),
        )
    return base


def _candidate_live_deadline_ms(cand: dict[str, Any]) -> int:
    """Return the true candidate deadline, including explicit watch extensions."""
    start_ms = int(cand.get("start_ms") or 0)
    deadline = start_ms + _candidate_live_ttl_ms(cand)
    for key in (
        "no_movement_watch_deadline_ms",
        "weak_drift_watch_deadline_ms",
    ):
        deadline = max(deadline, int(cand.get(key) or 0))
    return deadline


def _candidate_expired(cand: dict[str, Any], now_ms: int) -> bool:
    return int(now_ms) > _candidate_live_deadline_ms(cand)


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


def _v287_selected_negative_roundtrip_fingerprint(
    *,
    top_lane: str,
    current_buy_sol: float,
    prev_buy_sol: float,
    top_share: float,
    pre_entry_buys: int,
    observed_rearm_sol: float,
    first_rearm_delay_ms: int,
    last_rearm_delay_ms: int,
    last_rearm_lag_ms: int,
) -> tuple[bool, str]:
    """Allow only replay-backed V287 fast-lane negative self-roundtrip cases.

    The broad negative self-roundtrip override produced mPaf-class losses. The
    winning fast lane still needs a narrow exception because Pump self-roundtrip
    is structurally negative before external continuation arrives. These
    fingerprints are intentionally small and derived from the live win/loss
    split in the V287 logs.
    """
    if os.environ.get("V287_ALLOW_SELECTED_NEGATIVE_ROUNDTRIP_FINGERPRINT", "1") == "0":
        return False, "selected_fingerprint_disabled"
    if last_rearm_lag_ms > int(os.environ.get("V287_VERIFIED_HOT_TRAIN_MAX_SEND_LAG_MS", "650")):
        return False, "selected_rearm_lag_stale"
    generic_max_rearm_delay_ms = int(
        os.environ.get("V287_SELECTED_GENERIC_MAX_REARM_DELAY_MS", "1000")
    )
    if last_rearm_delay_ms > generic_max_rearm_delay_ms:
        return False, "selected_rearm_delay_stale"

    if top_lane == "fresh_impulse":
        if not (2.80 <= current_buy_sol <= 3.25):
            return False, "selected_fresh_current_out_of_band"
        if prev_buy_sol > 1e-12:
            return False, "selected_fresh_prev_buy_present"

        instant_dense_min_buys = int(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MIN_BUYS", "4"))
        instant_dense_max_buys = int(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_BUYS", "5"))
        instant_dense_min_sol = float(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MIN_SOL", "1.50"))
        instant_dense_max_sol = float(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_SOL", "4.50"))
        instant_dense_max_delay = int(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_DELAY_MS", "75"))
        if (
            instant_dense_min_sol <= observed_rearm_sol <= instant_dense_max_sol
            and instant_dense_min_buys <= pre_entry_buys <= instant_dense_max_buys
            and first_rearm_delay_ms <= instant_dense_max_delay
            and last_rearm_delay_ms <= instant_dense_max_delay
        ):
            return True, "selected_fresh_instant_dense_train"

        low_multi_min_buys = int(os.environ.get("V287_SELECTED_FRESH_LOW_MULTI_MIN_BUYS", "3"))
        low_multi_min_delay = int(os.environ.get("V287_SELECTED_FRESH_LOW_MULTI_MIN_DELAY_MS", "175"))
        low_multi_max_delay = int(os.environ.get("V287_SELECTED_FRESH_LOW_MULTI_MAX_DELAY_MS", "350"))
        low_multi_max_sol = float(os.environ.get("V287_SELECTED_FRESH_LOW_MULTI_MAX_SOL", "1.05"))
        if (
            0.70 <= observed_rearm_sol <= low_multi_max_sol
            and pre_entry_buys >= low_multi_min_buys
            and low_multi_min_delay <= first_rearm_delay_ms <= low_multi_max_delay
        ):
            return True, "selected_fresh_paced_low_multi_rearm"

        near_floor_min_sol = float(
            os.environ.get("V287_SELECTED_FRESH_NEAR_FLOOR_MIN_SOL", "1.20")
        )
        near_floor_max_sol = float(
            os.environ.get("V287_SELECTED_FRESH_NEAR_FLOOR_MAX_SOL", "1.50")
        )
        near_floor_min_buys = int(
            os.environ.get("V287_SELECTED_FRESH_NEAR_FLOOR_MIN_BUYS", "2")
        )
        near_floor_max_delay = int(
            os.environ.get("V287_SELECTED_FRESH_NEAR_FLOOR_MAX_DELAY_MS", "1000")
        )
        if (
            near_floor_min_sol <= observed_rearm_sol <= near_floor_max_sol
            and pre_entry_buys >= near_floor_min_buys
            and first_rearm_delay_ms <= near_floor_max_delay
            and last_rearm_delay_ms <= near_floor_max_delay
        ):
            return True, "selected_fresh_near_floor_multi_rearm"

        mid_min_sol = float(os.environ.get("V287_SELECTED_FRESH_SINGLE_MID_MIN_SOL", "1.35"))
        mid_max_sol = float(os.environ.get("V287_SELECTED_FRESH_SINGLE_MID_MAX_SOL", "2.25"))
        mid_max_delay = int(os.environ.get("V287_SELECTED_FRESH_SINGLE_MID_MAX_DELAY_MS", "25"))
        if (
            mid_min_sol <= observed_rearm_sol <= mid_max_sol
            and 1 <= pre_entry_buys <= 2
            and first_rearm_delay_ms <= mid_max_delay
        ):
            return True, "selected_fresh_single_mid_rearm"

        upper_mid_min_sol = float(
            os.environ.get("V287_SELECTED_FRESH_UPPER_MID_MULTI_MIN_SOL", "2.30")
        )
        upper_mid_max_sol = float(
            os.environ.get("V287_SELECTED_FRESH_UPPER_MID_MULTI_MAX_SOL", "3.80")
        )
        upper_mid_max_delay = int(
            os.environ.get("V287_SELECTED_FRESH_UPPER_MID_MULTI_MAX_DELAY_MS", "350")
        )
        if (
            upper_mid_min_sol <= observed_rearm_sol <= upper_mid_max_sol
            and 2 <= pre_entry_buys <= 3
            and first_rearm_delay_ms <= upper_mid_max_delay
            and last_rearm_delay_ms <= upper_mid_max_delay
        ):
            return True, "selected_fresh_upper_mid_multi_rearm"

        strong_min_sol = float(os.environ.get("V287_SELECTED_FRESH_STRONG_MIN_SOL", "3.80"))
        strong_max_sol = float(os.environ.get("V287_SELECTED_FRESH_STRONG_MAX_SOL", "4.50"))
        strong_min_delay = int(os.environ.get("V287_SELECTED_FRESH_STRONG_MIN_DELAY_MS", "75"))
        strong_max_delay = int(os.environ.get("V287_SELECTED_FRESH_STRONG_MAX_DELAY_MS", "150"))
        if (
            strong_min_sol <= observed_rearm_sol <= strong_max_sol
            and 1 <= pre_entry_buys <= 3
            and strong_min_delay <= first_rearm_delay_ms <= strong_max_delay
        ):
            return True, "selected_fresh_strong_multi_rearm"

        dense_train_min_buys = int(os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MIN_BUYS", "4"))
        dense_train_max_buys = int(os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_BUYS", "5"))
        dense_train_min_sol = float(os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MIN_SOL", "2.30"))
        dense_train_max_sol = float(os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_SOL", "3.60"))
        dense_train_max_delay = int(os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_DELAY_MS", "75"))
        if (
            dense_train_min_sol <= observed_rearm_sol <= dense_train_max_sol
            and dense_train_min_buys <= pre_entry_buys <= dense_train_max_buys
            and first_rearm_delay_ms <= dense_train_max_delay
            and last_rearm_delay_ms <= dense_train_max_delay
        ):
            return True, "selected_fresh_dense_moderate_train"

    if top_lane == "high_current_clean_train":
        if (
            os.environ.get("V287_ALLOW_HIGH_CURRENT_CLEAN_TRAIN_LIVE_RISK", "0")
            != "1"
        ):
            return False, "selected_high_current_shadow_only_after_7TSY_loss"
        high_current_min = float(os.environ.get("V287_HIGH_CURRENT_TRAIN_CURRENT_MIN_SOL", "3.30"))
        high_current_max = float(os.environ.get("V287_HIGH_CURRENT_TRAIN_CURRENT_MAX_SOL", "6.00"))
        high_rearm_min = float(os.environ.get("V287_HIGH_CURRENT_TRAIN_SELECTED_REARM_MIN_SOL", "3.00"))
        high_rearm_max = float(os.environ.get("V287_HIGH_CURRENT_TRAIN_SELECTED_REARM_MAX_SOL", "12.00"))
        high_min_buys = int(os.environ.get("V287_HIGH_CURRENT_TRAIN_SELECTED_MIN_BUYS", "2"))
        high_max_buys = int(os.environ.get("V287_HIGH_CURRENT_TRAIN_SELECTED_MAX_BUYS", "12"))
        high_max_first_delay = int(os.environ.get("V287_HIGH_CURRENT_TRAIN_SELECTED_MAX_FIRST_DELAY_MS", "350"))
        high_max_last_delay = int(os.environ.get("V287_HIGH_CURRENT_TRAIN_SELECTED_MAX_LAST_DELAY_MS", "1000"))
        if (
            high_current_min <= current_buy_sol <= high_current_max
            and high_rearm_min <= observed_rearm_sol <= high_rearm_max
            and high_min_buys <= pre_entry_buys <= high_max_buys
            and first_rearm_delay_ms <= high_max_first_delay
            and last_rearm_delay_ms <= high_max_last_delay
        ):
            return True, "selected_high_current_clean_train"

    if top_lane == "normal_top":
        normal_min_sol = float(os.environ.get("V287_SELECTED_NORMAL_REARM_MIN_SOL", "0.70"))
        normal_max_sol = float(os.environ.get("V287_SELECTED_NORMAL_REARM_MAX_SOL", "2.05"))
        normal_min_delay = int(os.environ.get("V287_SELECTED_NORMAL_MIN_DELAY_MS", "75"))
        normal_max_delay = int(os.environ.get("V287_SELECTED_NORMAL_MAX_DELAY_MS", "150"))
        if (
            normal_min_sol <= observed_rearm_sol <= normal_max_sol
            and pre_entry_buys >= 1
            and normal_min_delay <= first_rearm_delay_ms <= normal_max_delay
        ):
            return True, "selected_normal_top_rearm"

    if top_lane == "edge_top_strong_prior":
        edge_current_min = float(os.environ.get("V287_NORMAL_TOP_CURRENT_MIN_SOL", "2.00"))
        edge_current_max = float(os.environ.get("V287_NORMAL_TOP_CURRENT_MAX_SOL", "3.25"))
        edge_prev_min = float(os.environ.get("V287_EDGE_TOP_PREV_MIN_SOL", "7.50"))
        edge_prev_max = float(os.environ.get("V287_EDGE_TOP_PREV_MAX_SOL", "9.60"))
        edge_top_min = float(os.environ.get("V287_EDGE_TOP_MIN_SHARE", "0.50"))
        edge_top_max = float(os.environ.get("V287_EDGE_TOP_MAX_SHARE", "0.55"))
        edge_rearm_min = float(os.environ.get("V287_EDGE_TOP_REARM_MIN_SOL", "1.80"))
        edge_rearm_max = float(os.environ.get("V287_EDGE_TOP_REARM_MAX_SOL", "3.20"))
        edge_max_delay = int(os.environ.get("V287_EDGE_TOP_MAX_DELAY_MS", "750"))
        if (
            edge_current_min <= current_buy_sol <= edge_current_max
            and edge_prev_min <= prev_buy_sol <= edge_prev_max
            and edge_top_min <= top_share < edge_top_max
            and edge_rearm_min <= observed_rearm_sol <= edge_rearm_max
            and 1 <= pre_entry_buys <= 4
            and first_rearm_delay_ms <= edge_max_delay
            and last_rearm_delay_ms <= edge_max_delay
        ):
            return True, "selected_edge_top_strong_prior_rearm"

    if top_lane == "single_prior_buy_continuation":
        single_current_min = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_CURRENT_MIN_SOL", "2.00"))
        single_current_max = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_CURRENT_MAX_SOL", "3.25"))
        single_prev_min = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_PREV_MIN_SOL", "1.80"))
        single_prev_max = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_PREV_MAX_SOL", "5.60"))
        single_top_min = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_TOP_MIN", "0.80"))
        single_rearm_min = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_REARM_MIN_SOL", "2.00"))
        single_rearm_max = float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_REARM_MAX_SOL", "10.00"))
        single_max_delay = int(os.environ.get("V287_SELECTED_SINGLE_PRIOR_MAX_DELAY_MS", "350"))
        if (
            single_current_min <= current_buy_sol <= single_current_max
            and single_prev_min <= prev_buy_sol <= single_prev_max
            and top_share >= single_top_min
            and single_rearm_min <= observed_rearm_sol <= single_rearm_max
            and 1 <= pre_entry_buys <= 6
            and first_rearm_delay_ms <= single_max_delay
            and last_rearm_delay_ms <= single_max_delay
        ):
            return True, "selected_single_prior_strong_rearm"

    if top_lane == "seed_prior_carry_continuation":
        # This is the scanner-blind sibling of the successful single-prior lane:
        # the first buy is the "prior" leg, then a clean carry arrives before
        # we send. It stays narrower than high_current_clean_train and requires
        # either multiple clean continuation buys or one very fast, large carry.
        seed_current_min = float(os.environ.get("V287_SEED_PRIOR_CARRY_CURRENT_MIN_SOL", "2.00"))
        seed_current_max = float(os.environ.get("V287_SEED_PRIOR_CARRY_CURRENT_MAX_SOL", "2.80"))
        seed_single_strong_min = float(
            os.environ.get("V287_SEED_PRIOR_SINGLE_STRONG_MIN_SOL", "3.00")
        )
        seed_single_strong_max = float(
            os.environ.get("V287_SEED_PRIOR_SINGLE_STRONG_MAX_SOL", "6.50")
        )
        seed_single_strong_max_delay = int(
            os.environ.get("V287_SEED_PRIOR_SINGLE_STRONG_MAX_DELAY_MS", "75")
        )
        if (
            seed_current_min <= current_buy_sol <= seed_current_max
            and prev_buy_sol <= 1e-12
            and top_share >= 0.999
            and seed_single_strong_min <= observed_rearm_sol <= seed_single_strong_max
            and pre_entry_buys == 1
            and first_rearm_delay_ms <= seed_single_strong_max_delay
            and last_rearm_delay_ms <= seed_single_strong_max_delay
        ):
            return True, "selected_seed_prior_single_strong_rearm"

        seed_rearm_min = float(os.environ.get("V287_SEED_PRIOR_CARRY_REARM_MIN_SOL", "0.70"))
        seed_rearm_max = float(os.environ.get("V287_SEED_PRIOR_CARRY_REARM_MAX_SOL", "6.50"))
        seed_min_buys = int(os.environ.get("V287_SEED_PRIOR_CARRY_MIN_REARM_BUYS", "2"))
        seed_max_buys = int(os.environ.get("V287_SEED_PRIOR_CARRY_MAX_REARM_BUYS", "16"))
        seed_single_large_rearm_min = float(
            os.environ.get("V287_SEED_PRIOR_CARRY_SINGLE_LARGE_REARM_MIN_SOL", "1.80")
        )
        seed_max_first_delay = int(os.environ.get("V287_SEED_PRIOR_CARRY_MAX_FIRST_DELAY_MS", "350"))
        seed_max_last_delay = int(os.environ.get("V287_SEED_PRIOR_CARRY_MAX_LAST_DELAY_MS", "350"))
        seed_rearm_count_ok = (
            seed_min_buys <= pre_entry_buys <= seed_max_buys
            or (
                pre_entry_buys == 1
                and observed_rearm_sol >= seed_single_large_rearm_min
            )
        )
        if (
            seed_current_min <= current_buy_sol <= seed_current_max
            and prev_buy_sol <= 1e-12
            and top_share >= 0.999
            and seed_rearm_min <= observed_rearm_sol <= seed_rearm_max
            and seed_rearm_count_ok
            and first_rearm_delay_ms <= seed_max_first_delay
            and last_rearm_delay_ms <= seed_max_last_delay
        ):
            return True, "selected_seed_prior_carry_rearm"

    if top_lane == "dust_prior_clean_continuation":
        # Repair for the 2026-05-30 frequency leak: a single tiny prior buy can
        # be noise, not a real prior-leg requirement failure. This lane still
        # waits for a clean follow-through buy and then uses the same projection,
        # final refresh, min-token, and protected-sell gates as the proven path.
        dust_current_min = float(os.environ.get("V287_DUST_PRIOR_CURRENT_MIN_SOL", "2.00"))
        dust_current_max = float(os.environ.get("V287_DUST_PRIOR_CURRENT_MAX_SOL", "3.25"))
        dust_prev_max = float(os.environ.get("V287_DUST_PRIOR_PREV_MAX_SOL", "0.50"))
        dust_top_min = float(os.environ.get("V287_DUST_PRIOR_TOP_MIN", "0.95"))
        dust_rearm_min = float(os.environ.get("V287_DUST_PRIOR_REARM_MIN_SOL", "0.35"))
        dust_rearm_max = float(os.environ.get("V287_DUST_PRIOR_REARM_MAX_SOL", "2.00"))
        dust_min_delay = int(os.environ.get("V287_DUST_PRIOR_MIN_REARM_DELAY_MS", "250"))
        dust_max_delay = int(os.environ.get("V287_DUST_PRIOR_MAX_REARM_DELAY_MS", "1250"))
        dust_max_buys = int(os.environ.get("V287_DUST_PRIOR_MAX_REARM_BUYS", "3"))
        if (
            dust_current_min <= current_buy_sol <= dust_current_max
            and 0.0 <= prev_buy_sol <= dust_prev_max
            and top_share >= dust_top_min
            and dust_rearm_min <= observed_rearm_sol <= dust_rearm_max
            and 1 <= pre_entry_buys <= dust_max_buys
            and dust_min_delay <= first_rearm_delay_ms <= dust_max_delay
            and last_rearm_delay_ms <= dust_max_delay
        ):
            return True, "selected_dust_prior_clean_continuation"

    if top_lane == "two_prior_buy_continuation":
        two_current_min = float(os.environ.get("V287_SELECTED_TWO_PRIOR_CURRENT_MIN_SOL", "2.80"))
        two_current_max = float(os.environ.get("V287_SELECTED_TWO_PRIOR_CURRENT_MAX_SOL", "3.25"))
        two_prev_min = float(os.environ.get("V287_SELECTED_TWO_PRIOR_PREV_MIN_SOL", "5.00"))
        two_prev_max = float(os.environ.get("V287_SELECTED_TWO_PRIOR_PREV_MAX_SOL", "9.60"))
        two_top_min = float(os.environ.get("V287_SELECTED_TWO_PRIOR_TOP_MIN", "0.45"))
        two_top_max = float(os.environ.get("V287_SELECTED_TWO_PRIOR_TOP_MAX", "0.70"))
        two_rearm_min = float(os.environ.get("V287_SELECTED_TWO_PRIOR_REARM_MIN_SOL", "1.80"))
        two_rearm_max = float(os.environ.get("V287_SELECTED_TWO_PRIOR_REARM_MAX_SOL", "6.50"))
        two_max_delay = int(os.environ.get("V287_SELECTED_TWO_PRIOR_MAX_DELAY_MS", "350"))
        if (
            two_current_min <= current_buy_sol <= two_current_max
            and two_prev_min <= prev_buy_sol <= two_prev_max
            and two_top_min <= top_share <= two_top_max
            and two_rearm_min <= observed_rearm_sol <= two_rearm_max
            and 1 <= pre_entry_buys <= 6
            and first_rearm_delay_ms <= two_max_delay
            and last_rearm_delay_ms <= two_max_delay
        ):
            return True, "selected_two_prior_followthrough_rearm"

    return False, "selected_no_match"


def _v287_selected_negative_reason_allowed(reason: str) -> bool:
    return bool(reason.startswith("selected_")) and reason != "selected_no_match"


def _v287_selected_fresh_actual_enabled(reason: str) -> bool:
    """Keep broad fresh actual disabled while allowing replay-backed sublanes."""
    reason = str(reason or "")
    if os.environ.get("V287_SELECTED_FRESH_ACTUAL_ENABLED", "0") == "1":
        return True
    if reason == "selected_fresh_single_mid_rearm":
        return (
            os.environ.get("V287_SELECTED_FRESH_SINGLE_MID_ACTUAL_ENABLED", "1")
            != "0"
        )
    return False


def _v287_reason_min_quote_tokens(reason: str, default_min_tokens: float) -> float:
    """Raise token-output floor only for replay-backed selected exception lanes."""
    reason = str(reason or "")
    floor = float(default_min_tokens)
    if reason == "selected_fresh_single_mid_rearm":
        floor = max(
            floor,
            float(os.environ.get("V287_SELECTED_SINGLE_MID_MIN_QUOTE_TOKENS", "680000")),
        )
    elif reason == "selected_fresh_strong_multi_rearm":
        floor = max(
            floor,
            float(os.environ.get("V287_SELECTED_STRONG_MIN_QUOTE_TOKENS", "660000")),
        )
    elif reason == "selected_fresh_upper_mid_multi_rearm":
        floor = max(
            floor,
            float(
                os.environ.get(
                    "V287_SELECTED_UPPER_MID_MULTI_MIN_QUOTE_TOKENS",
                    "700000",
                )
            ),
        )
    elif reason == "selected_fresh_clean_carry_reclass":
        floor = max(
            floor,
            float(os.environ.get("V287_SELECTED_FRESH_CLEAN_CARRY_MIN_QUOTE_TOKENS", "560000")),
        )
    return floor


def _v287_reason_max_quote_tokens(reason: str) -> float:
    """Reason-specific upper token cap blocks too-early fills that replay lost."""
    reason = str(reason or "")
    if reason in {
        "selected_single_prior_strong_rearm",
        "selected_single_prior_no_movement_followthrough",
    }:
        return float(os.environ.get("V287_SELECTED_SINGLE_PRIOR_MAX_QUOTE_TOKENS", "650000"))
    if reason == "selected_seed_prior_carry_rearm":
        return float(os.environ.get("V287_SELECTED_SEED_PRIOR_MAX_QUOTE_TOKENS", "760000"))
    if reason == "selected_seed_prior_single_strong_rearm":
        return float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_MAX_QUOTE_TOKENS",
                "760000",
            )
        )
    return 0.0


def _v287_is_selected_seed_prior(cand: dict[str, Any], reason: str) -> bool:
    seed_prior_reasons = {
        "selected_seed_prior_carry_rearm",
        "selected_seed_prior_single_strong_rearm",
    }
    top_lane = str(cand.get("top_lane") or "")
    selected_top_lane = str(cand.get("selected_top_lane") or "")
    return (
        str(reason or "") in seed_prior_reasons
        and (
            top_lane == "seed_prior_carry_continuation"
            or selected_top_lane == "seed_prior_carry_continuation"
        )
    )


def _v287_cand_pre_entry_sol(cand: dict[str, Any]) -> float:
    return int(cand.get("pre_entry_buy_lamports") or 0) / LAMPORTS_PER_SOL


def _v287_cand_post_plan_sol(cand: dict[str, Any]) -> float:
    return int(cand.get("post_plan_followthrough_lamports") or 0) / LAMPORTS_PER_SOL


def _v287_selected_seed_prior_effective_rearm_lag_ms(
    cand: dict[str, Any],
    *,
    max_delay_ms: int,
    max_authority_lag_ms: int,
) -> tuple[int, int, str]:
    """Separate market rearm freshness from late final-authority bookkeeping.

    The seed-prior carry lane is a speed lane. A rearm that arrived in 30-80 ms
    should not be reclassified as stale only because static-plan/creator-vault
    checks consumed another few hundred ms before the final authority branch ran.
    We still cap the total authority lag so truly old candidates cannot pass.
    """
    cached_rearm_lag = cand.get("last_rearm_lag_ms") or cand.get(
        "last_rearm_pass_lag_ms"
    )
    if cached_rearm_lag is not None:
        raw_lag_ms = int(cached_rearm_lag)
    else:
        last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
        raw_lag_ms = (
            max(0, _now_ms() - last_rearm_ts_ms)
            if last_rearm_ts_ms > 0
            else int(cand.get("last_rearm_pass_delay_ms") or 999999)
        )
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or first_delay_ms)
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_USE_EVENT_DELAY_LAG", "1")
        != "0"
        and first_delay_ms <= int(max_delay_ms)
        and last_delay_ms <= int(max_delay_ms)
        and raw_lag_ms <= int(max_authority_lag_ms)
    ):
        return int(last_delay_ms), int(raw_lag_ms), "event_delay"
    return int(raw_lag_ms), int(raw_lag_ms), "authority_lag"


def _v287_selected_seed_prior_pending_plan_credit_ok(
    cand: dict[str, Any],
    *,
    credit_wait_ms: int,
    post_plan_buys: int,
    post_plan_lamports: int,
) -> tuple[bool, str]:
    """Credit a clean selected seed-prior rearm consumed by plan readiness.

    This is deliberately narrower than the global preplan-credit switch. It
    repairs the 6roY class: the first qualifying rearm arrived while the static
    buy plan was still pending, then later tiny post-plan deltas made the bot
    demand another 0.70 SOL fresh buy before final quote/sell safety could run.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "pending_plan_credit_disabled"
    top_lane = str(cand.get("top_lane") or cand.get("selected_top_lane") or "")
    if top_lane != "seed_prior_carry_continuation":
        return False, "pending_plan_not_seed_prior"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "pending_plan_prev_sells"
    if float(cand.get("top_share") or 0.0) < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MIN_TOP_SHARE",
            "0.999",
        )
    ):
        return False, "pending_plan_weak_top_share"

    current_sol = float(cand.get("current_buy_sol") or 0.0)
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_CURRENT_MIN_SOL",
                "2.00",
            )
        )
        <= current_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_CURRENT_MAX_SOL",
                "2.05",
            )
        )
    ):
        return False, "pending_plan_current"

    pending_lamports = int(
        cand.get("pending_plan_rearm_lamports")
        or cand.get("post_plan_rearm_base_lamports")
        or 0
    )
    pending_sol = pending_lamports / LAMPORTS_PER_SOL
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MIN_SOL",
                "1.05",
            )
        )
        <= pending_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_SOL",
                "1.25",
            )
        )
    ):
        return False, "pending_plan_rearm_sol"

    pending_buys = int(
        cand.get("pending_plan_rearm_buys")
        or cand.get("post_plan_rearm_base_buys")
        or 0
    )
    if pending_buys != int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_EXACT_BUYS",
            "1",
        )
    ):
        return False, "pending_plan_rearm_buys"

    pending_delay_ms = int(
        cand.get("pending_plan_rearm_delay_ms")
        or cand.get("first_rearm_pass_delay_ms")
        or 999999
    )
    if pending_delay_ms > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_FIRST_DELAY_MS",
            "80",
        )
    ):
        return False, "pending_plan_delay"

    if int(credit_wait_ms) > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_WAIT_MS",
            "900",
        )
    ):
        return False, "pending_plan_wait_stale"
    if int(post_plan_buys) > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_POSTPLAN_BUYS",
            "1",
        )
    ):
        return False, "pending_plan_postplan_buys"
    if (int(post_plan_lamports) / LAMPORTS_PER_SOL) > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PENDING_PLAN_CREDIT_MAX_POSTPLAN_SOL",
            "0.10",
        )
    ):
        return False, "pending_plan_postplan_sol"
    return True, "selected_seed_prior_pending_plan_first_rearm_credit"


def _v287_credit_seed_prior_postplan_followthrough(
    cand: dict[str, Any],
    reason: str,
    *,
    delta_lamports: int,
    delta_buys: int,
    ts_ms: int,
    source: str,
    mint: str = "",
    counters: Any = None,
    min_sol_env: str = "V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL",
    default_min_sol: str = "0.70",
) -> bool:
    """Credit already-proven seed-prior follow-through to final authority fields."""
    if not _v287_is_selected_seed_prior(cand, reason):
        return False
    delta_lamports = int(delta_lamports or 0)
    delta_buys = int(delta_buys or 0)
    min_lamports = int(
        float(os.environ.get(min_sol_env, default_min_sol)) * LAMPORTS_PER_SOL
    )
    if delta_buys < 1 or delta_lamports < min_lamports:
        return False
    existing_lamports = int(cand.get("post_plan_followthrough_lamports") or 0)
    existing_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    if delta_lamports < existing_lamports:
        return False
    cand["post_plan_followthrough_lamports"] = delta_lamports
    cand["post_plan_followthrough_buys"] = max(delta_buys, existing_buys)
    cand["post_plan_followthrough_ts_ms"] = int(ts_ms)
    cand["post_plan_rearm_passed"] = 1
    cand["post_plan_rearm_pass_ts_ms"] = int(ts_ms)
    cand["seed_prior_watch_followthrough_send_ok"] = 1
    cand["seed_prior_watch_followthrough_lamports"] = delta_lamports
    cand["seed_prior_watch_followthrough_buys"] = max(delta_buys, existing_buys)
    cand["seed_prior_watch_followthrough_ts_ms"] = int(ts_ms)
    if counters is not None:
        counters["seed_prior_postplan_followthrough_credit"] += 1
    _log(
        "PGG2-V287-SEED-PRIOR-POSTPLAN-FOLLOWTHROUGH-CREDIT "
        f"mint={_short(mint)} full_mint={mint} "
        f"reason={reason} source={source} "
        f"delta_buy_sol={delta_lamports/LAMPORTS_PER_SOL:.6f} "
        f"delta_buys={delta_buys} "
        f"post_plan_buy_sol={delta_lamports/LAMPORTS_PER_SOL:.6f} "
        "reason_detail=propagate_existing_followthrough_to_final_authority"
    )
    return True


def _v287_seed_prior_postplan_followthrough_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    min_sol_env: str = "V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL",
    default_min_sol: str = "0.70",
) -> tuple[bool, str]:
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "postplan_not_passed"
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    min_post_plan_sol = float(os.environ.get(min_sol_env, default_min_sol))
    if post_plan_buys < 1:
        return False, "no_postplan_buy"
    if post_plan_sol < min_post_plan_sol:
        return False, "weak_postplan_followthrough"
    return True, "postplan_followthrough"


def _v287_seed_prior_consumed_postplan_authority_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    drift_pct: float,
) -> tuple[bool, str]:
    """Allow seed-prior sends when rearm flow is already in the final curve read.

    The 30-minute preplan-floor run showed strong selected seed-prior rows
    getting blocked because the post-plan buy continuation had already moved
    the curve before the final Sender boundary read. This is not a broad
    zero-drift bypass: it requires the replay-backed seed-prior lane, multiple
    post-plan buys, a bounded token quote, fresh rearm timing, and no oversized
    self-impact shape.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_SEND_ENABLED", "1") == "0":
        return False, "consumed_postplan_disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "postplan_not_passed"
    quote_tokens = float(quote_tokens or 0.0)
    drift_pct = float(drift_pct or 0.0)
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    current_sol = float(cand.get("current_buy_sol") or 0.0)
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or first_delay_ms)
    prev_sells = int(cand.get("prev_sells") or 0)
    top_share = float(cand.get("top_share") or 0.0)
    max_delay_ms = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_REARM_DELAY_MS", "80")
    )
    max_authority_lag_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_MAX_AUTHORITY_LAG_MS",
            "1150",
        )
    )
    last_rearm_lag_ms, raw_last_rearm_lag_ms, lag_source = (
        _v287_selected_seed_prior_effective_rearm_lag_ms(
            cand,
            max_delay_ms=max_delay_ms,
            max_authority_lag_ms=max_authority_lag_ms,
        )
    )
    cand["v287_seed_prior_consumed_postplan_lag_eval"] = {
        "last_rearm_lag_ms": int(last_rearm_lag_ms),
        "raw_last_rearm_lag_ms": int(raw_last_rearm_lag_ms),
        "lag_source": str(lag_source),
        "max_authority_lag_ms": int(max_authority_lag_ms),
    }
    if post_plan_buys < int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_BUYS",
            "1",
        )
    ):
        return False, "consumed_postplan_weak_buys"
    hot_high_cap_enabled = (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_ENABLED",
            "1",
        )
        != "0"
    )
    hot_high_cap_ok = (
        hot_high_cap_enabled
        and int(prev_sells) == 0
        and post_plan_buys
        >= int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_BUYS",
                "1",
            )
        )
        and float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_POSTPLAN_SOL",
                "0.70",
            )
        )
        <= post_plan_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_POSTPLAN_SOL",
                "1.18",
            )
        )
        and float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_QUOTE_TOKENS",
                "795000",
            )
        )
        <= quote_tokens
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_QUOTE_TOKENS",
                "825000",
            )
        )
        and float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_PRE_ENTRY_SOL",
                "2.00",
            )
        )
        <= pre_entry_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_PRE_ENTRY_SOL",
                "2.70",
            )
        )
        and pre_entry_buys
        == int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_EXACT_PRE_ENTRY_BUYS",
                "2",
            )
        )
        and float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_CURRENT_SOL",
                "2.00",
            )
        )
        <= current_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_CURRENT_SOL",
                "2.20",
            )
        )
        and int(raw_last_rearm_lag_ms)
        <= int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_RAW_REARM_LAG_MS",
                "350",
            )
        )
        and first_delay_ms
        <= int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_REARM_DELAY_MS",
                "45",
            )
        )
        and last_delay_ms
        <= int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MAX_REARM_DELAY_MS",
                "45",
            )
        )
        and top_share
        >= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_HOT_HIGH_CAP_MIN_TOP_SHARE",
                "0.999",
            )
        )
    )
    cand["v287_seed_prior_consumed_hot_high_cap_eval"] = {
        "enabled": int(hot_high_cap_enabled),
        "pass": int(hot_high_cap_ok),
        "quote_tokens": float(quote_tokens),
        "post_plan_buys": int(post_plan_buys),
        "post_plan_sol": float(post_plan_sol),
        "pre_entry_buys": int(pre_entry_buys),
        "pre_entry_sol": float(pre_entry_sol),
        "current_sol": float(current_sol),
        "first_delay_ms": int(first_delay_ms),
        "last_delay_ms": int(last_delay_ms),
        "raw_last_rearm_lag_ms": int(raw_last_rearm_lag_ms),
        "prev_sells": int(prev_sells),
        "top_share": float(top_share),
    }
    if hot_high_cap_ok:
        return True, "consumed_postplan_hot_high_cap_authorized"
    if post_plan_sol < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_SOL",
            "1.30",
        )
    ):
        return False, "consumed_postplan_weak_sol"
    if quote_tokens < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_QUOTE_TOKENS",
            "540000",
        )
    ):
        return False, "consumed_postplan_low_quote"
    if quote_tokens > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_QUOTE_TOKENS",
            "760000",
        )
    ):
        return False, "consumed_postplan_high_quote"
    if last_rearm_lag_ms > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_REARM_LAG_MS",
            "650",
        )
    ):
        return False, "consumed_postplan_stale_rearm"
    if pre_entry_sol > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MAX_PRE_ENTRY_SOL",
            "11.00",
        )
    ):
        return False, "consumed_postplan_heavy_pre_entry"
    if drift_pct < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_DRIFT_PCT",
            "-0.050",
        )
    ):
        return False, "consumed_postplan_negative_drift"
    if top_share < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CONSUMED_POSTPLAN_MIN_TOP_SHARE",
            "0.999",
        )
    ):
        return False, "consumed_postplan_weak_top_share"
    return True, "consumed_postplan_flow_in_final_curve_read"


def _v287_seed_prior_hot_high_cap_bypass_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    max_quote_tokens: float,
    source: str,
    mint: str,
    counters: Any = None,
) -> bool:
    ok, authority_reason = _v287_seed_prior_consumed_postplan_authority_ok(
        cand,
        reason,
        quote_tokens=float(quote_tokens or 0.0),
        drift_pct=0.0,
    )
    if not ok or authority_reason != "consumed_postplan_hot_high_cap_authorized":
        return False
    if counters is not None:
        counters["seed_prior_hot_high_cap_bypass"] += 1
    eval_info = cand.get("v287_seed_prior_consumed_hot_high_cap_eval") or {}
    _log(
        "PGG2-V287-SEED-PRIOR-HOT-HIGH-CAP-BYPASS "
        f"mint={_short(mint)} full_mint={mint} "
        f"reason={reason} source={source} "
        f"amount_out_tokens={float(quote_tokens):.6f} "
        f"base_max_tokens={float(max_quote_tokens):.6f} "
        f"authority_reason={authority_reason} "
        f"post_plan_buys={int(eval_info.get('post_plan_buys') or 0)} "
        f"post_plan_buy_sol={float(eval_info.get('post_plan_sol') or 0.0):.6f} "
        f"pre_entry_buys={int(eval_info.get('pre_entry_buys') or 0)} "
        f"pre_entry_buy_sol={float(eval_info.get('pre_entry_sol') or 0.0):.6f} "
        f"current_buy_sol={float(eval_info.get('current_sol') or 0.0):.6f} "
        f"raw_last_rearm_lag_ms={int(eval_info.get('raw_last_rearm_lag_ms') or 0)} "
        "reason_detail=existing_hot_high_cap_authority_reached_from_cap_check"
    )
    return True


def _v287_seed_prior_credible_postplan_boundary_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    drift_pct: float,
) -> tuple[bool, str]:
    """Authorize only the bounded 5WnJ/A7uZ post-plan continuation shape.

    This is not a global negative-drift or zero-drift bypass. It requires the
    selected seed-prior carry lane, a real post-plan buy, refreshed quote still
    inside the base cap, and the same two-buy seed-prior structure seen in the
    credible last-run misses. Over-cap rows such as J9Ly/BvY9 remain blocked by
    the quote band, and weak post-plan rows such as CTf5/8Y6C remain blocked by
    the post-plan SOL threshold.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_SEND_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "credible_postplan_disabled"
    if str(reason or "") != "selected_seed_prior_carry_rearm":
        return False, "credible_postplan_reason"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "postplan_not_passed"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "prev_sells"

    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    min_post_plan_buys = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_BUYS",
            "1",
        )
    )
    min_post_plan_sol = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_SOL",
            "1.20",
        )
    )
    if post_plan_buys < min_post_plan_buys:
        return False, "credible_postplan_weak_buys"
    if post_plan_sol < min_post_plan_sol:
        return False, "credible_postplan_weak_sol"

    quote_tokens = float(quote_tokens or 0.0)
    min_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_QUOTE_TOKENS",
            "540000",
        )
    )
    max_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_QUOTE_TOKENS",
            "760000",
        )
    )
    if quote_tokens < min_quote_tokens:
        return False, "credible_postplan_low_quote"
    if quote_tokens > max_quote_tokens:
        return False, "credible_postplan_high_quote"

    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    exact_pre_entry_buys = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_EXACT_PRE_ENTRY_BUYS",
            "2",
        )
    )
    if exact_pre_entry_buys > 0 and pre_entry_buys != exact_pre_entry_buys:
        return False, "credible_postplan_pre_entry_buys"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    if pre_entry_sol < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_PRE_ENTRY_SOL",
            "2.15",
        )
    ):
        return False, "credible_postplan_pre_entry_low"
    if pre_entry_sol > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_PRE_ENTRY_SOL",
            "2.70",
        )
    ):
        return False, "credible_postplan_pre_entry_high"

    max_rearm_delay_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_REARM_DELAY_MS",
            "80",
        )
    )
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or first_delay_ms)
    if first_delay_ms > max_rearm_delay_ms or last_delay_ms > max_rearm_delay_ms:
        return False, "credible_postplan_rearm_delay"

    postplan_ts_ms = int(cand.get("post_plan_rearm_pass_ts_ms") or 0)
    max_pass_age_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MAX_PASS_AGE_MS",
            "650",
        )
    )
    if postplan_ts_ms > 0 and max(0, _now_ms() - postplan_ts_ms) > max_pass_age_ms:
        return False, "credible_postplan_stale"

    if float(cand.get("top_share") or 0.0) < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_TOP_SHARE",
            "0.999",
        )
    ):
        return False, "credible_postplan_top_share"

    drift_pct = float(drift_pct or 0.0)
    min_neg_drift_pct = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_CREDIBLE_POSTPLAN_MIN_NEG_DRIFT_PCT",
            "-20.00",
        )
    )
    if drift_pct < min_neg_drift_pct:
        return False, "credible_postplan_drift_too_negative"

    cand["v287_seed_prior_credible_postplan_eval"] = {
        "pass": 1,
        "post_plan_buys": int(post_plan_buys),
        "post_plan_sol": float(post_plan_sol),
        "min_post_plan_sol": float(min_post_plan_sol),
        "quote_tokens": float(quote_tokens),
        "min_quote_tokens": float(min_quote_tokens),
        "max_quote_tokens": float(max_quote_tokens),
        "pre_entry_buys": int(pre_entry_buys),
        "pre_entry_sol": float(pre_entry_sol),
        "first_delay_ms": int(first_delay_ms),
        "last_delay_ms": int(last_delay_ms),
        "drift_pct": float(drift_pct),
        "min_neg_drift_pct": float(min_neg_drift_pct),
    }
    if drift_pct < 0.0:
        return True, "credible_postplan_consumed_negative_refresh"
    return True, "credible_postplan_zero_or_positive_refresh"


def _v287_seed_prior_one_strong_postplan_zerodrift_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    drift_pct: float,
) -> tuple[bool, str]:
    """Authorize the narrow 9bmP/4ejc consumed-postplan shape.

    Disabled by default after the 2026-06-03 7d88 live loss. One post-plan buy
    already consumed by the final curve read is not a sufficient execution edge
    when the self-roundtrip projection is still negative.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_ZERODRIFT_ENABLED",
            "0",
        )
        == "0"
    ):
        return False, "one_strong_postplan_disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "one_strong_postplan_not_passed"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "one_strong_postplan_prev_sells"

    current_sol = float(cand.get("current_buy_sol") or 0.0)
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    quote_tokens = float(quote_tokens or 0.0)
    drift_pct = float(drift_pct or 0.0)
    top_share = float(cand.get("top_share") or 0.0)
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or first_delay_ms)
    cached_rearm_lag = (
        cand.get("last_rearm_lag_ms")
        or cand.get("last_rearm_pass_lag_ms")
    )
    if cached_rearm_lag is not None:
        last_rearm_lag_ms = int(cached_rearm_lag)
    else:
        last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
        last_rearm_lag_ms = (
            max(0, _now_ms() - last_rearm_ts_ms)
            if last_rearm_ts_ms > 0
            else int(cand.get("last_rearm_pass_delay_ms") or 999999)
        )

    if pre_entry_buys != 2:
        return False, "one_strong_postplan_pre_entry_buys"
    if post_plan_buys != 1:
        return False, "one_strong_postplan_buy_count"
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_CURRENT_MIN_SOL",
                "2.00",
            )
        )
        <= current_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_CURRENT_MAX_SOL",
                "2.20",
            )
        )
    ):
        return False, "one_strong_postplan_current"
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_PRE_ENTRY_MIN_SOL",
                "2.40",
            )
        )
        <= pre_entry_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_PRE_ENTRY_MAX_SOL",
                "3.10",
            )
        )
    ):
        return False, "one_strong_postplan_pre_entry"
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MIN_SOL",
                "1.20",
            )
        )
        <= post_plan_sol
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_SOL",
                "1.60",
            )
        )
    ):
        return False, "one_strong_postplan_sol"
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MIN_QUOTE_TOKENS",
                "760000",
            )
        )
        <= quote_tokens
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_QUOTE_TOKENS",
                "850000",
            )
        )
    ):
        return False, "one_strong_postplan_quote"
    if first_delay_ms > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_FIRST_DELAY_MS",
            "60",
        )
    ):
        return False, "one_strong_postplan_first_delay"
    if last_delay_ms > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_LAST_DELAY_MS",
            "80",
        )
    ):
        return False, "one_strong_postplan_last_delay"
    if last_rearm_lag_ms > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MAX_REARM_LAG_MS",
            "650",
        )
    ):
        return False, "one_strong_postplan_lag"
    if drift_pct < -1e-9:
        return False, "one_strong_postplan_negative_drift"
    if top_share < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_ONE_STRONG_POSTPLAN_MIN_TOP_SHARE",
            "0.999",
        )
    ):
        return False, "one_strong_postplan_top_share"
    return True, "one_strong_postplan_flow_in_final_curve_read"


def _v287_seed_prior_speed_postplan_zerodrift_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    drift_pct: float,
) -> tuple[bool, str]:
    """Allow fast seed-prior carry when post-plan flow is already consumed.

    This is the 9bmP-class repair: the speed authority already validated a
    fresh, clean seed-prior carry, and the final curve read is flat because the
    post-plan buy has already been absorbed before the Sender boundary. It
    stays narrower than the older consumed-postplan rule and does not enable
    the global zero-drift switch.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_ZERODRIFT_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "speed_postplan_disabled"
    speed_ok, speed_reason = _v287_seed_prior_speed_authority_ok(
        cand,
        reason,
        quote_tokens,
    )
    if not speed_ok:
        return False, speed_reason
    if drift_pct < -1e-9:
        return False, "speed_postplan_negative_drift"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "speed_postplan_not_passed"
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    if post_plan_buys < int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_BUYS",
            "1",
        )
    ):
        return False, "speed_postplan_weak_buys"
    if post_plan_sol < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_SOL",
            "0.70",
        )
    ):
        return False, "speed_postplan_weak_sol"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "speed_postplan_prev_sells"
    if float(cand.get("top_share") or 0.0) < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MIN_TOP_SHARE", "0.999")
    ):
        return False, "speed_postplan_top_share"
    return True, "speed_postplan_flow_in_final_curve_read"


def _v287_seed_prior_fast_single_rearm_zerodrift_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    drift_pct: float,
) -> tuple[bool, str]:
    """Allow the fast single-rearm seed-prior shape without waiting again.

    This targets the 7Bfo-class miss from the 2026-06-03 11:53 smoke: one
    large, very fast clean rearm already put the quote above the floor, but the
    no-movement watch waited for the next buy and the quote compressed.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_ZERODRIFT_ENABLED", "1") == "0":
        return False, "fast_single_rearm_disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    drift_pct = float(drift_pct or 0.0)
    if drift_pct < -1e-9 or drift_pct > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_DRIFT_PCT", "0.05")
    ):
        return False, "fast_single_rearm_drift"
    current_sol = float(cand.get("current_buy_sol") or 0.0)
    if current_sol < float(os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_CURRENT_SOL", "1.95")):
        return False, "fast_single_rearm_current_low"
    if current_sol > float(os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_CURRENT_SOL", "2.10")):
        return False, "fast_single_rearm_current_high"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "fast_single_rearm_prev_sells"
    if float(cand.get("top_share") or 0.0) < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_TOP_SHARE", "0.999")
    ):
        return False, "fast_single_rearm_top_share"
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    if pre_entry_buys != int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_EXACT_BUYS", "1")
    ):
        return False, "fast_single_rearm_buys"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    if pre_entry_sol < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_SOL", "2.00")
    ):
        return False, "fast_single_rearm_sol_low"
    if pre_entry_sol > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_SOL", "2.50")
    ):
        return False, "fast_single_rearm_sol_high"
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or first_delay_ms)
    if first_delay_ms > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_FIRST_DELAY_MS", "80")
    ):
        return False, "fast_single_rearm_first_delay"
    if last_delay_ms > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_LAST_DELAY_MS", "90")
    ):
        return False, "fast_single_rearm_last_delay"
    cached_rearm_lag = cand.get("last_rearm_lag_ms") or cand.get("last_rearm_pass_lag_ms")
    if cached_rearm_lag is not None:
        last_rearm_lag_ms = int(cached_rearm_lag)
    else:
        last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
        last_rearm_lag_ms = (
            max(0, _now_ms() - last_rearm_ts_ms)
            if last_rearm_ts_ms > 0
            else last_delay_ms
        )
    if last_rearm_lag_ms > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_LAG_MS", "650")
    ):
        return False, "fast_single_rearm_lag"
    quote_tokens = float(quote_tokens or 0.0)
    if quote_tokens < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MIN_QUOTE_TOKENS", "500000")
    ):
        return False, "fast_single_rearm_quote_low"
    if quote_tokens > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_FAST_SINGLE_REARM_MAX_QUOTE_TOKENS", "620000")
    ):
        return False, "fast_single_rearm_quote_high"
    return True, "fast_single_rearm_zero_drift_authorized"


def _v287_seed_prior_moderate_positive_drift_ok(
    cand: dict[str, Any],
    reason: str,
    *,
    quote_tokens: float,
    drift_pct: float,
) -> tuple[bool, str]:
    """Narrow authority for the clean two-buy moderate-drift miss bucket.

    This is not a global drift loosen. It targets the recently blocked
    seed-prior rows whose shadow stayed sell-clean through the first 350 ms:
    5bNE/EKUo/FmYd/9bqm. It deliberately excludes the old no-postplan high
    drift losses (EDt7/4py1), the heavy pre-entry row (ArHF), zero-drift
    carry rows (3brK/CWLG), and quote-cap rows.
    """
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_ENABLED", "1")
        == "0"
    ):
        return False, "moderate_drift_disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"

    quote_tokens = float(quote_tokens or 0.0)
    drift_pct = float(drift_pct or 0.0)
    current_sol = float(cand.get("current_buy_sol") or 0.0)
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or first_delay_ms)
    cached_rearm_lag = cand.get("last_rearm_lag_ms") or cand.get("last_rearm_pass_lag_ms")
    if cached_rearm_lag is not None:
        last_rearm_lag_ms = int(cached_rearm_lag)
    else:
        last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
        last_rearm_lag_ms = (
            max(0, _now_ms() - last_rearm_ts_ms)
            if last_rearm_ts_ms > 0
            else int(cand.get("last_rearm_pass_delay_ms") or 999999)
        )
    top_share = float(cand.get("top_share") or 0.0)
    prev_sells = int(cand.get("prev_sells") or 0)

    exact_pre_entry_buys = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_EXACT_PRE_ENTRY_BUYS",
            "2",
        )
    )
    eval_info = {
        "enabled": 1,
        "drift_pct": float(drift_pct),
        "min_drift_pct": float(
            os.environ.get("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_PCT", "0.30")
        ),
        "max_drift_pct": float(
            os.environ.get("V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_PCT", "0.95")
        ),
        "quote_tokens": float(quote_tokens),
        "min_quote_tokens": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_QUOTE_TOKENS",
                "600000",
            )
        ),
        "max_quote_tokens": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_QUOTE_TOKENS",
                "690000",
            )
        ),
        "current_sol": float(current_sol),
        "min_current_sol": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_CURRENT_SOL",
                "2.00",
            )
        ),
        "max_current_sol": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_CURRENT_SOL",
                "2.25",
            )
        ),
        "pre_entry_sol": float(pre_entry_sol),
        "min_pre_entry_sol": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_PRE_ENTRY_SOL",
                "2.45",
            )
        ),
        "max_pre_entry_sol": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_PRE_ENTRY_SOL",
                "3.50",
            )
        ),
        "pre_entry_buys": int(pre_entry_buys),
        "exact_pre_entry_buys": int(exact_pre_entry_buys),
        "post_plan_buys": int(post_plan_buys),
        "post_plan_sol": float(post_plan_sol),
        "first_delay_ms": int(first_delay_ms),
        "last_delay_ms": int(last_delay_ms),
        "max_rearm_delay_ms": int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_REARM_DELAY_MS",
                "90",
            )
        ),
        "last_rearm_lag_ms": int(last_rearm_lag_ms),
        "max_last_rearm_lag_ms": int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MAX_LAST_REARM_LAG_MS",
                "650",
            )
        ),
        "top_share": float(top_share),
        "min_top_share": float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_MODERATE_DRIFT_MIN_TOP_SHARE",
                "0.999",
            )
        ),
        "prev_sells": int(prev_sells),
    }

    def _moderate_result(ok: bool, why: str) -> tuple[bool, str]:
        eval_info["pass"] = int(ok)
        eval_info["reason"] = str(why)
        cand["v287_seed_prior_moderate_drift_eval"] = eval_info
        return ok, why

    if not (eval_info["min_drift_pct"] <= drift_pct <= eval_info["max_drift_pct"]):
        return _moderate_result(False, "moderate_drift_band")
    if not (
        eval_info["min_quote_tokens"]
        <= quote_tokens
        <= eval_info["max_quote_tokens"]
    ):
        return _moderate_result(False, "moderate_drift_quote_band")
    if not (eval_info["min_current_sol"] <= current_sol <= eval_info["max_current_sol"]):
        return _moderate_result(False, "moderate_drift_current_band")
    if not (
        eval_info["min_pre_entry_sol"]
        <= pre_entry_sol
        <= eval_info["max_pre_entry_sol"]
    ):
        return _moderate_result(False, "moderate_drift_pre_entry_band")
    if pre_entry_buys != exact_pre_entry_buys:
        return _moderate_result(False, "moderate_drift_pre_entry_buys")
    if post_plan_buys != 0 or post_plan_sol > 0.0:
        return _moderate_result(False, "moderate_drift_postplan_present")
    if first_delay_ms > eval_info["max_rearm_delay_ms"]:
        return _moderate_result(False, "moderate_drift_first_delay")
    if last_delay_ms > eval_info["max_rearm_delay_ms"]:
        return _moderate_result(False, "moderate_drift_last_delay")
    if last_rearm_lag_ms > eval_info["max_last_rearm_lag_ms"]:
        return _moderate_result(False, "moderate_drift_stale_rearm")
    if top_share < eval_info["min_top_share"]:
        return _moderate_result(False, "moderate_drift_top_share")
    if prev_sells != 0:
        return _moderate_result(False, "moderate_drift_prev_sells")
    return _moderate_result(True, "moderate_positive_clean_two_buy_seed_prior")


def _v287_seed_prior_clean_cap_override_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    max_quote_tokens: float,
) -> bool:
    """Replay-backed seed-prior cap extension.

    This catches clean carry rows that were blocked only by the base 760k token
    cap in the 2026-06-01 smoke. It deliberately requires fast seed-prior
    rearm and does not change the base cap for other selected lanes.
    """
    if not _v287_is_selected_seed_prior(cand, reason):
        return False
    if float(quote_tokens) <= float(max_quote_tokens):
        return False
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOWTHROUGH_OVERRIDE_ENABLED", "1")
        != "0"
        and int(cand.get("seed_prior_high_cap_watch") or 0) == 1
        and int(cand.get("seed_prior_watch_followthrough_send_ok") or 0) == 1
    ):
        follow_ts_ms = int(cand.get("seed_prior_watch_followthrough_ts_ms") or 0)
        follow_age_ms = (
            max(0, _now_ms() - follow_ts_ms) if follow_ts_ms > 0 else 999999
        )
        follow_lamports = int(cand.get("seed_prior_watch_followthrough_lamports") or 0)
        follow_buys = int(cand.get("seed_prior_watch_followthrough_buys") or 0)
        high_cap_max_quote = float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_QUOTE_TOKENS",
                "925000",
            )
        )
        if (
            float(quote_tokens) <= high_cap_max_quote
            and follow_age_ms
            <= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MAX_AGE_MS",
                    "650",
                )
            )
            and follow_lamports
            >= int(
                float(
                    os.environ.get(
                        "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_SOL",
                        "2.00",
                    )
                )
                * LAMPORTS_PER_SOL
            )
            and follow_buys
            >= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_BUYS",
                    "2",
                )
            )
            and int(cand.get("prev_sells") or 0) == 0
            and _v287_cand_pre_entry_sol(cand)
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MAX_PRE_ENTRY_SOL",
                    "7.00",
                )
            )
            and float(cand.get("top_share") or 0.0)
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_HIGH_CAP_FOLLOW_MIN_TOP_SHARE",
                    "0.999",
                )
            )
        ):
            cand["seed_prior_high_cap_followthrough_override_ok"] = 1
            return True
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_OVERRIDE_ENABLED", "1")
        != "0"
        and int(cand.get("post_plan_rearm_passed") or 0) == 1
    ):
        post_plan_sol = _v287_cand_post_plan_sol(cand)
        post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
        pass_ts_ms = int(cand.get("post_plan_rearm_pass_ts_ms") or 0)
        pass_age_ms = max(0, _now_ms() - pass_ts_ms) if pass_ts_ms > 0 else 999999
        postplan_cap = float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_QUOTE_TOKENS",
                "925000",
            )
        )
        if (
            float(quote_tokens) <= postplan_cap
            and post_plan_sol
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MIN_SOL",
                    "2.50",
                )
            )
            and post_plan_buys
            >= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MIN_BUYS",
                    "2",
                )
            )
            and pass_age_ms
            <= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_AGE_MS",
                    "650",
                )
            )
            and _v287_cand_pre_entry_sol(cand)
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_PRE_ENTRY_SOL",
                    "6.60",
                )
            )
            and float(cand.get("current_buy_sol") or 0.0)
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_CURRENT_SOL",
                    "2.50",
                )
            )
            and float(cand.get("top_share") or 0.0)
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MIN_TOP_SHARE",
                    "0.999",
                )
            )
        ):
            cand["seed_prior_postplan_cap_override_ok"] = 1
            return True
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_ENABLED", "1")
        != "0"
        and int(cand.get("post_plan_rearm_passed") or 0) == 1
    ):
        post_plan_sol = _v287_cand_post_plan_sol(cand)
        post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
        pre_entry_sol = _v287_cand_pre_entry_sol(cand)
        pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
        first_delay = int(cand.get("first_rearm_pass_delay_ms") or 999999)
        last_delay = int(cand.get("last_rearm_pass_delay_ms") or first_delay)
        last_rearm_lag = int(
            cand.get("last_rearm_lag_ms")
            or cand.get("last_rearm_pass_lag_ms")
            or 999999
        )
        early_clean_cap = float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_QUOTE_TOKENS",
                "870000",
            )
        )
        exact_postplan_buys = int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_EXACT_POSTPLAN_BUYS",
                "1",
            )
        )
        exact_pre_entry_buys = int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_EXACT_PRE_ENTRY_BUYS",
                "2",
            )
        )
        if (
            float(quote_tokens) <= early_clean_cap
            and (
                exact_postplan_buys <= 0
                or post_plan_buys == exact_postplan_buys
            )
            and post_plan_sol
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_POSTPLAN_SOL",
                    "0.70",
                )
            )
            and post_plan_sol
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_POSTPLAN_SOL",
                    "0.95",
                )
            )
            and (
                exact_pre_entry_buys <= 0
                or pre_entry_buys == exact_pre_entry_buys
            )
            and pre_entry_sol
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_PRE_ENTRY_SOL",
                    "1.40",
                )
            )
            and pre_entry_sol
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_PRE_ENTRY_SOL",
                    "1.85",
                )
            )
            and float(cand.get("current_buy_sol") or 0.0)
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_CURRENT_SOL",
                    "2.00",
                )
            )
            and float(cand.get("current_buy_sol") or 0.0)
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_CURRENT_SOL",
                    "2.25",
                )
            )
            and first_delay
            <= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_REARM_DELAY_MS",
                    "80",
                )
            )
            and last_delay
            <= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_REARM_DELAY_MS",
                    "80",
                )
            )
            and last_rearm_lag
            <= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_LAST_REARM_LAG_MS",
                    "350",
                )
            )
            and int(cand.get("prev_sells") or 0) == 0
            and float(cand.get("top_share") or 0.0)
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MIN_TOP_SHARE",
                    "0.999",
                )
            )
        ):
            cand["seed_prior_early_clean_cap_override_ok"] = 1
            return True
    clean_cap = float(os.environ.get("V287_SELECTED_SEED_PRIOR_CLEAN_MAX_QUOTE_TOKENS", "825000"))
    if float(quote_tokens) > clean_cap:
        return False
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    if pre_entry_sol < float(os.environ.get("V287_SELECTED_SEED_PRIOR_CAP_MIN_PRE_ENTRY_SOL", "2.00")):
        return False
    if int(cand.get("pre_entry_buys") or 0) < 2:
        return False
    cand["seed_prior_watch_cap_override_ok"] = 0
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_CAP_OVERRIDE_ENABLED", "1")
        != "0"
        and int(cand.get("seed_prior_watch_followthrough_send_ok") or 0) == 1
        and int(cand.get("post_plan_rearm_passed") or 0) == 1
    ):
        follow_ts_ms = int(cand.get("seed_prior_watch_followthrough_ts_ms") or 0)
        max_age_ms = int(
            os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MAX_AGE_MS", "650")
        )
        follow_age_ms = (
            max(0, _now_ms() - follow_ts_ms) if follow_ts_ms > 0 else 999999
        )
        follow_lamports = int(cand.get("seed_prior_watch_followthrough_lamports") or 0)
        follow_buys = int(cand.get("seed_prior_watch_followthrough_buys") or 0)
        min_follow_lamports = int(
            float(os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MIN_SOL", "0.70"))
            * LAMPORTS_PER_SOL
        )
        min_follow_buys = int(
            os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MIN_BUYS", "2")
        )
        max_postplan_sol = float(
            os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_CAP_MAX_POSTPLAN_SOL", "1.30")
        )
        post_plan_sol = _v287_cand_post_plan_sol(cand)
        if (
            follow_age_ms <= max_age_ms
            and follow_lamports >= min_follow_lamports
            and follow_buys >= min_follow_buys
            and post_plan_sol <= max_postplan_sol
        ):
            cand["seed_prior_watch_cap_override_ok"] = 1
            return True
    max_delay = int(os.environ.get("V287_SELECTED_SEED_PRIOR_CAP_MAX_REARM_DELAY_MS", "80"))
    first_delay = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay = int(cand.get("last_rearm_pass_delay_ms") or first_delay)
    if first_delay > max_delay or last_delay > max_delay:
        return False
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False
    return True


def _v287_seed_prior_high_cap_watch_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    max_quote_tokens: float,
) -> tuple[bool, str]:
    """Keep a clean high-cap seed-prior row alive for real followthrough.

    This does not authorize a send. It only prevents the refresh token cap from
    deleting an FLbM-shaped row before the next buy/sell tick proves whether the
    continuation is real. Any sell still aborts through the normal candidate
    abort path.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_ENABLED", "1") == "0":
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    quote_tokens = float(quote_tokens)
    if quote_tokens <= float(max_quote_tokens):
        return False, "cap_not_exceeded"
    if quote_tokens > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_QUOTE_TOKENS",
            "925000",
        )
    ):
        return False, "quote_too_high"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "postplan_not_passed"
    if int(cand.get("post_plan_followthrough_buys") or 0) < int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_POSTPLAN_BUYS", "1")
    ):
        return False, "postplan_buys"
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    if post_plan_sol < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_POSTPLAN_SOL", "1.20")
    ):
        return False, "postplan_sol_low"
    if post_plan_sol > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_POSTPLAN_SOL", "2.00")
    ):
        return False, "postplan_sol_high"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    if pre_entry_sol < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_PRE_ENTRY_SOL", "3.00")
    ):
        return False, "pre_entry_sol_low"
    if pre_entry_sol > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_PRE_ENTRY_SOL", "3.70")
    ):
        return False, "pre_entry_sol_high"
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    if pre_entry_buys < int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_PRE_ENTRY_BUYS", "2")
    ):
        return False, "pre_entry_buys_low"
    if pre_entry_buys > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_PRE_ENTRY_BUYS", "2")
    ):
        return False, "pre_entry_buys_high"
    if float(cand.get("current_buy_sol") or 0.0) > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_CURRENT_SOL", "2.30")
    ):
        return False, "current_buy_sol"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "prev_sells"
    if float(cand.get("top_share") or 0.0) < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MIN_TOP_SHARE", "0.999")
    ):
        return False, "top_share"
    if int(cand.get("first_rearm_pass_delay_ms") or 999999) > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_FIRST_DELAY_MS", "80")
    ):
        return False, "first_delay"
    if int(cand.get("last_rearm_pass_delay_ms") or 999999) > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_LAST_DELAY_MS", "80")
    ):
        return False, "last_delay"
    last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
    last_rearm_lag_ms = (
        max(0, _now_ms() - last_rearm_ts_ms)
        if last_rearm_ts_ms > 0
        else int(cand.get("last_rearm_lag_ms") or 999999)
    )
    if last_rearm_lag_ms > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MAX_REARM_LAG_MS", "650")
    ):
        return False, "rearm_lag"
    return True, "high_cap_clean_postplan_watch"


def _v287_seed_prior_watch_quote_floor_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
) -> tuple[bool, str]:
    """Allow late-curve seed-prior quotes only after real watch follow-through.

    This is intentionally narrower than lowering the global min-token floor.
    The 2026-06-02 tsSY miss showed a seed-prior candidate with a low quote
    (~424k tokens) but a fresh 3 SOL post-watch buy continuation before any
    sell. Without this exception, the quote floor blocks before the dedicated
    watch-followthrough send authority can run.
    """
    if (
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_ENABLED", "1")
        == "0"
    ):
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("seed_prior_watch_followthrough_send_ok") or 0) != 1:
        return False, "no_watch_followthrough"
    quote_tokens = float(quote_tokens)
    if not (
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MIN_TOKENS",
                "390000",
            )
        )
        <= quote_tokens
        <= float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_TOKENS",
                "500000",
            )
        )
    ):
        return False, "quote_band"
    follow_ts_ms = int(cand.get("seed_prior_watch_followthrough_ts_ms") or 0)
    follow_age_ms = max(0, _now_ms() - follow_ts_ms) if follow_ts_ms > 0 else 999999
    if follow_age_ms > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_AGE_MS", "450")
    ):
        return False, "follow_stale"
    follow_lamports = int(cand.get("seed_prior_watch_followthrough_lamports") or 0)
    follow_buys = int(cand.get("seed_prior_watch_followthrough_buys") or 0)
    if follow_lamports < int(
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MIN_SOL",
                "3.00",
            )
        )
        * LAMPORTS_PER_SOL
    ):
        return False, "follow_sol"
    if follow_buys < int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MIN_BUYS", "1")
    ):
        return False, "follow_buys"
    if _v287_cand_pre_entry_sol(cand) > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_PRE_ENTRY_SOL", "5.80")
    ):
        return False, "pre_entry"
    if int(cand.get("pre_entry_buys") or 0) > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_PRE_ENTRY_BUYS", "4")
    ):
        return False, "pre_entry_buys"
    if float(cand.get("current_buy_sol") or 0.0) > float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_CURRENT_SOL", "2.25")
    ):
        return False, "current"
    if int(cand.get("first_rearm_pass_delay_ms") or 999999) > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_FIRST_DELAY_MS", "80")
    ):
        return False, "first_delay"
    if int(cand.get("last_rearm_pass_delay_ms") or 999999) > int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MAX_LAST_DELAY_MS", "650")
    ):
        return False, "last_delay"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "prev_sells"
    if float(cand.get("top_share") or 0.0) < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_QUOTE_FLOOR_MIN_TOP_SHARE", "0.999")
    ):
        return False, "top_share"
    return True, "watch_followthrough_low_quote_floor"


def _v287_seed_prior_flat_send_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
) -> tuple[bool, str]:
    """Allow seed-prior zero-drift sends only for clean replay-backed shapes.

    The broad selected no-movement allowance is still blocked when the immediate
    self-roundtrip is negative. The exception here is restricted to the exact
    carry forms that had clean future flow and no early sells in the last smoke:
    a large post-plan follow-through shape or a compact 2.7-3.2 SOL carry band.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_NEGATIVE_BYPASS_ENABLED", "0") != "1":
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    quote_tokens = float(quote_tokens)
    postplan_ok = (
        pre_entry_sol >= float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_POSTPLAN_PRE_ENTRY_MIN_SOL", "4.00"))
        and post_plan_buys >= 1
        and post_plan_sol >= float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_POSTPLAN_MIN_SOL", "2.00"))
        and quote_tokens <= float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_POSTPLAN_MAX_QUOTE_TOKENS", "740000"))
    )
    if postplan_ok:
        return True, "postplan_followthrough"
    compact_ok = (
        float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MIN_SOL", "2.70"))
        <= pre_entry_sol
        <= float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MAX_SOL", "3.20"))
        and float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MIN_QUOTE_TOKENS", "650000"))
        <= quote_tokens
        <= float(os.environ.get("V287_SELECTED_SEED_PRIOR_FLAT_COMPACT_MAX_QUOTE_TOKENS", "725000"))
    )
    if compact_ok:
        return True, "compact_clean_carry"
    return False, "shape"


def _v287_seed_prior_projection_bypass_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
) -> bool:
    if os.environ.get("V287_SELECTED_SEED_PRIOR_NEGATIVE_BYPASS_ENABLED", "0") != "1":
        return False
    if not _v287_is_selected_seed_prior(cand, reason):
        return False
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    quote_tokens = float(quote_tokens)
    return (
        pre_entry_sol >= float(os.environ.get("V287_SELECTED_SEED_PRIOR_PROJECTION_BYPASS_MIN_SOL", "3.00"))
        and int(cand.get("pre_entry_buys") or 0) >= 2
        and float(os.environ.get("V287_SELECTED_SEED_PRIOR_PROJECTION_BYPASS_MIN_QUOTE_TOKENS", "620000"))
        <= quote_tokens
        <= float(os.environ.get("V287_SELECTED_SEED_PRIOR_PROJECTION_BYPASS_MAX_QUOTE_TOKENS", "680000"))
        and float(cand.get("top_share") or 0.0) >= 0.999
    )


def _v287_seed_prior_postplan_reason_restore_ok(
    cand: dict[str, Any],
) -> tuple[bool, str]:
    """Restore selected seed-prior classification lost across plan-ready wait.

    In the post-plan path the runner can correctly log
    SELECTED-PLAN-READY-WAIT-PASS, then lose that selected reason before the
    negative self-roundtrip check. This does not authorize a send by itself; it
    only lets the candidate continue to the final refresh authority.
    """
    reason = str(cand.get("selected_plan_ready_reason") or "")
    if reason not in {
        "selected_seed_prior_carry_rearm",
        "selected_seed_prior_single_strong_rearm",
    }:
        return False, "missing_selected_plan_reason"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "no_post_plan_pass"
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    if reason == "selected_seed_prior_single_strong_rearm":
        min_post_plan_sol = float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_REASON_RESTORE_POSTPLAN_MIN_SOL",
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_REARM_MIN_SOL",
                    "0.70",
                ),
            )
        )
    else:
        min_post_plan_sol = float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_REASON_RESTORE_POSTPLAN_MIN_SOL",
                "2.00",
            )
        )
    if post_plan_buys < 1 or post_plan_sol < min_post_plan_sol:
        return False, "weak_post_plan_followthrough"
    return True, "post_plan_seed_prior_reason"


def _v287_seed_prior_single_strong_postplan_bridge_ok(
    cand: dict[str, Any],
    reason: str,
) -> tuple[bool, str]:
    """Allow single-strong seed-prior only after post-plan carry exists.

    The first single-strong rearm is useful, but it must not send before the
    static plan is ready and a fresh carry appears after that plan. Once that
    post-plan carry is already present, the candidate should not re-enter the
    same wait loop and expire.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_POSTPLAN_BRIDGE_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "disabled"
    if reason != "selected_seed_prior_single_strong_rearm":
        return False, "not_single_strong"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "no_post_plan_pass"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "prior_sell"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "weak_top_share"
    first_rearm_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    if first_rearm_delay_ms > int(
        os.environ.get("V287_SEED_PRIOR_SINGLE_STRONG_MAX_DELAY_MS", "75")
    ):
        return False, "first_rearm_slow"
    last_rearm_lag_ms = int(cand.get("last_rearm_lag_ms") or 999999)
    if last_rearm_lag_ms > int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_LAG_MS",
            "650",
        )
    ):
        return False, "rearm_lag_high"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    if pre_entry_sol > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_PRE_ENTRY_SOL",
            "6.50",
        )
    ):
        return False, "pre_entry_high"
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    if post_plan_buys < int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MIN_POSTPLAN_BUYS",
            "1",
        )
    ):
        return False, "post_plan_buy_count_low"
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    if post_plan_sol < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MIN_POSTPLAN_SOL",
            "1.50",
        )
    ):
        return False, "post_plan_sol_low"
    if post_plan_sol > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_BRIDGE_MAX_POSTPLAN_SOL",
            "3.50",
        )
    ):
        return False, "post_plan_sol_high"
    return True, "post_plan_bridge"


def _v287_seed_prior_tiny_negative_drift_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    final_refresh_drift_pct: float,
    self_roundtrip_negative: bool,
    now_ms: int,
) -> tuple[bool, str]:
    """Classify Gi5f-style tiny negative drift as quote jitter, not a dump.

    This is intentionally narrower than the general selected negative
    roundtrip path. It requires real post-plan seed-prior flow and keeps the
    normal final account/fingerprint/live-vault validators in charge before
    any transaction can be sent.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_TINY_NEG_DRIFT_ENABLED", "1") == "0":
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if not bool(self_roundtrip_negative):
        return False, "self_roundtrip_nonnegative"
    drift_pct = float(final_refresh_drift_pct)
    if drift_pct >= 0.0:
        return False, "not_negative"
    max_neg_pct = abs(
        float(os.environ.get("V287_SELECTED_SEED_PRIOR_TINY_NEG_DRIFT_MAX_PCT", "0.05"))
    )
    if drift_pct < -max_neg_pct:
        return False, "drift_too_negative"
    postplan_ok, postplan_reason = _v287_seed_prior_postplan_followthrough_ok(
        cand,
        reason,
        min_sol_env="V287_SELECTED_SEED_PRIOR_TINY_NEG_POSTPLAN_MIN_SOL",
        default_min_sol="2.00",
    )
    if not postplan_ok:
        return False, postplan_reason
    quote_tokens = float(quote_tokens)
    min_quote = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_TINY_NEG_MIN_QUOTE_TOKENS", "700000")
    )
    max_quote = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_TINY_NEG_MAX_QUOTE_TOKENS", "760000")
    )
    if quote_tokens < min_quote:
        return False, "quote_below_tiny_neg_band"
    if max_quote > 0 and quote_tokens > max_quote:
        return False, "quote_above_tiny_neg_band"
    pass_ts = int(cand.get("post_plan_rearm_pass_ts_ms") or 0)
    max_postplan_age_ms = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_TINY_NEG_MAX_POSTPLAN_AGE_MS", "650")
    )
    if pass_ts <= 0 or max(0, int(now_ms) - pass_ts) > max_postplan_age_ms:
        return False, "postplan_stale"
    last_rearm_ts = int(cand.get("last_rearm_pass_ts_ms") or 0)
    max_last_rearm_lag_ms = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_TINY_NEG_MAX_LAST_REARM_LAG_MS", "450")
    )
    if last_rearm_ts <= 0 or max(0, int(now_ms) - last_rearm_ts) > max_last_rearm_lag_ms:
        return False, "rearm_stale"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "top_share"
    return True, "tiny_negative_drift_postplan_jitter"


def _v287_seed_prior_negative_refresh_watch_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    final_refresh_drift_pct: float,
    now_ms: int,
) -> tuple[bool, str]:
    """Keep moderate bullish compression alive, but require a later buy to send.

    A negative token-output refresh can mean the curve moved up from buy flow.
    It can also be a stale boundary. This helper only starts a short watch; it
    never authorizes a transaction without a new post-watch buy update.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_ENABLED", "1") == "0":
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    drift_pct = float(final_refresh_drift_pct)
    if drift_pct >= 0.0:
        return False, "not_negative"
    max_neg_pct = abs(
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MAX_DRIFT_PCT",
                "2.25",
            )
        )
    )
    if drift_pct < -max_neg_pct:
        return False, "drift_too_negative"
    quote_tokens = float(quote_tokens)
    min_quote = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MIN_QUOTE_TOKENS",
            "650000",
        )
    )
    max_quote = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MAX_QUOTE_TOKENS",
            "760000",
        )
    )
    if quote_tokens < min_quote:
        return False, "quote_below_watch_band"
    if max_quote > 0.0 and quote_tokens > max_quote:
        return False, "quote_above_watch_band"
    if int(cand.get("pre_entry_buys") or 0) < 2:
        return False, "pre_entry_buys"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "top_share"
    last_rearm_ts = int(cand.get("last_rearm_pass_ts_ms") or 0)
    max_last_rearm_lag_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MAX_LAST_REARM_LAG_MS",
            "700",
        )
    )
    if last_rearm_ts <= 0 or max(0, int(now_ms) - last_rearm_ts) > max_last_rearm_lag_ms:
        return False, "rearm_stale"
    return True, "negative_refresh_watch_band"


def _v287_seed_prior_tiny_neg_clean_cap_watch_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    final_refresh_drift_pct: float,
) -> tuple[bool, str]:
    """Keep clean-cap tiny negative refresh alive, but still require later flow.

    This is narrower than the normal negative-refresh watch. It exists for the
    760k-825k seed-prior clean-cap band, where the last run deleted a candidate
    before it could prove or disprove continuation. It never sends immediately.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    drift_pct = float(final_refresh_drift_pct)
    if drift_pct >= 0.0:
        return False, "not_negative"
    max_neg_pct = abs(
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MAX_DRIFT_PCT",
                "0.05",
            )
        )
    )
    if drift_pct < -max_neg_pct:
        return False, "drift_too_negative"
    quote_tokens = float(quote_tokens)
    min_quote = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MIN_QUOTE_TOKENS",
            "760000",
        )
    )
    max_quote = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MAX_QUOTE_TOKENS",
            "825000",
        )
    )
    if quote_tokens < min_quote:
        return False, "quote_below_clean_cap_watch_band"
    if max_quote > 0.0 and quote_tokens > max_quote:
        return False, "quote_above_clean_cap_watch_band"
    if _v287_cand_pre_entry_sol(cand) < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_MIN_PRE_ENTRY_SOL",
            "2.00",
        )
    ):
        return False, "pre_entry_sol"
    if int(cand.get("pre_entry_buys") or 0) < 2:
        return False, "pre_entry_buys"
    if float(cand.get("current_buy_sol") or 0.0) > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_MAX_CURRENT_SOL",
            "2.80",
        )
    ):
        return False, "current_buy_sol"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "top_share"
    if int(cand.get("post_plan_rearm_passed") or 0) == 1:
        return False, "postplan_should_use_postplan_path"
    return True, "tiny_negative_clean_cap_watch"


def _v287_seed_prior_negative_refresh_followthrough_send_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    final_refresh_drift_pct: float,
    now_ms: int,
) -> tuple[bool, str]:
    """Authorize only after a negative-refresh watch sees a real later buy."""
    watch_ok, watch_reason = _v287_seed_prior_negative_refresh_watch_ok(
        cand,
        reason,
        quote_tokens,
        final_refresh_drift_pct,
        now_ms,
    )
    if not watch_ok:
        (
            clean_cap_watch_ok,
            clean_cap_watch_reason,
        ) = _v287_seed_prior_tiny_neg_clean_cap_watch_ok(
            cand,
            reason,
            quote_tokens,
            final_refresh_drift_pct,
        )
        if not clean_cap_watch_ok:
            return False, watch_reason
        watch_reason = clean_cap_watch_reason
    if int(cand.get("seed_prior_negative_refresh_watch") or 0) != 1:
        return False, "no_negative_refresh_watch"
    if int(cand.get("seed_prior_watch_followthrough_send_ok") or 0) != 1:
        return False, "no_watch_followthrough"
    delta_lamports = int(cand.get("seed_prior_watch_followthrough_lamports") or 0)
    delta_buys = int(cand.get("seed_prior_watch_followthrough_buys") or 0)
    min_delta_lamports = int(
        float(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_SOL",
                "0.50",
            )
        )
        * LAMPORTS_PER_SOL
    )
    min_delta_buys = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_BUYS", "2")
    )
    if delta_lamports < min_delta_lamports or delta_buys < min_delta_buys:
        return False, "weak_watch_followthrough"
    follow_ts = int(cand.get("seed_prior_watch_followthrough_ts_ms") or 0)
    max_age_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_NEG_REFRESH_FOLLOWTHROUGH_MAX_AGE_MS",
            "450",
        )
    )
    if follow_ts <= 0 or max(0, int(now_ms) - follow_ts) > max_age_ms:
        return False, "watch_followthrough_stale"
    return True, "negative_refresh_watch_followthrough"


def _v287_seed_prior_plan_ready_reason_restore_ok(
    cand: dict[str, Any],
    now_ms: int,
) -> tuple[bool, str]:
    """Preserve a selected seed-prior decision through plan-ready waiting.

    The carry lane can mutate its visible 1s flow while the static tx plan is
    being prepared. If it already passed SELECTED-PLAN-READY-WAIT-PASS, the
    final self-roundtrip block must not reclassify it from mutated state and
    delete it before the final refresh/watch authority runs.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_PLAN_REASON_RESTORE_ENABLED", "1") == "0":
        return False, "disabled"
    reason = str(cand.get("selected_plan_ready_reason") or "")
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    ready_ts = int(cand.get("selected_plan_ready_ts_ms") or 0)
    if ready_ts <= 0:
        return False, "missing_ts"
    max_age_ms = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_PLAN_REASON_RESTORE_MAX_AGE_MS", "900")
    )
    age_ms = max(0, int(now_ms) - ready_ts)
    if age_ms > max_age_ms:
        return False, "stale"
    return True, reason


def _v287_seed_prior_watch_followthrough_state(
    cand: dict[str, Any],
    now_ms: int,
) -> tuple[str, str, int, int]:
    """Classify a seed-prior no-movement watch after a later buy update.

    Returns (state, reason, delta_lamports, delta_buys), where state is:
    - off: not a seed-prior no-movement watch
    - stale: watch deadline passed
    - keep: still waiting; latest buy was too small to prove follow-through
    - followthrough: a real post-watch buy continuation is present
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_ENABLED", "1") == "0":
        return "off", "disabled", 0, 0
    reason = str(cand.get("no_movement_watch_reason") or cand.get("selected_plan_ready_reason") or "")
    if not _v287_is_selected_seed_prior(cand, reason):
        return "off", "not_seed_prior", 0, 0
    if int(cand.get("no_movement_watch_keeps") or 0) <= 0:
        return "off", "no_watch", 0, 0
    deadline = int(cand.get("no_movement_watch_deadline_ms") or 0)
    if deadline <= 0 or int(now_ms) > deadline:
        return "stale", reason, 0, 0
    start_lamports = int(cand.get("no_movement_watch_start_pre_entry_lamports") or 0)
    start_buys = int(cand.get("no_movement_watch_start_buys") or 0)
    delta_lamports = max(0, int(cand.get("pre_entry_buy_lamports") or 0) - start_lamports)
    delta_buys = max(0, int(cand.get("pre_entry_buys") or 0) - start_buys)
    if int(cand.get("seed_prior_consumed_postplan_zero_watch") or 0) == 1:
        min_delta_lamports = int(
            float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_FOLLOW_MIN_SOL",
                    "0.50",
                )
            )
            * LAMPORTS_PER_SOL
        )
        min_delta_buys = int(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_FOLLOW_MIN_BUYS",
                "1",
            )
        )
    else:
        min_delta_lamports = int(
            float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_SOL",
                    "0.50",
                )
            )
            * LAMPORTS_PER_SOL
        )
        min_delta_buys = int(
            os.environ.get("V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_BUYS", "2")
        )
    if delta_lamports >= min_delta_lamports and delta_buys >= min_delta_buys:
        return "followthrough", reason, delta_lamports, delta_buys
    return "keep", reason, delta_lamports, delta_buys


def _v287_mark_seed_prior_consumed_zero_watch(
    cand: dict[str, Any],
    reason: str,
    now_ms: int,
    watch_ms: int,
) -> None:
    """Wait for a fresh post-boundary buy instead of sending consumed zero-drift."""
    cand["seed_prior_consumed_postplan_zero_watch"] = 1
    cand["seed_prior_consumed_postplan_zero_watch_start_ms"] = int(now_ms)
    cand["seed_prior_consumed_postplan_zero_watch_reason"] = str(reason or "")
    cand["seed_prior_consumed_postplan_send_ok"] = 0
    cand["post_plan_rearm_required"] = 1
    cand["post_plan_rearm_wait_start_ms"] = int(now_ms)
    cand["post_plan_rearm_wait_last_ms"] = int(now_ms)
    cand["post_plan_rearm_base_lamports"] = int(
        cand.get("pre_entry_buy_lamports") or 0
    )
    cand["post_plan_rearm_base_buys"] = int(cand.get("pre_entry_buys") or 0)
    cand["no_movement_watch_keeps"] = int(cand.get("no_movement_watch_keeps", 0)) + 1
    cand["no_movement_watch_deadline_ms"] = int(now_ms) + int(watch_ms)
    cand["no_movement_watch_reason"] = str(reason or "")
    cand["no_movement_watch_start_pre_entry_lamports"] = int(
        cand.get("pre_entry_buy_lamports") or 0
    )
    cand["no_movement_watch_start_buys"] = int(cand.get("pre_entry_buys") or 0)
    cand["no_movement_watch_ts_ms"] = int(now_ms)


def _v287_seed_prior_speed_authority_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
) -> tuple[bool, str]:
    """Authorize only the fast, clean seed-prior carry shape.

    This is not a broad negative-roundtrip bypass. It exists because the live
    evidence showed that waiting for heavier rearm/final self-roundtrip checks
    consumes the continuation edge in this one lane.
    """
    enabled = (
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_AUTHORITY_ENABLED", "1")
        != "0"
    )
    current_sol = float(cand.get("current_buy_sol") or 0.0)
    top_share = float(cand.get("top_share") or 0.0)
    prev_sells = int(cand.get("prev_sells") or 0)
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    first_delay_ms = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay_ms = int(cand.get("last_rearm_pass_delay_ms") or 999999)
    last_lag_ms = int(
        cand.get("last_rearm_lag_ms")
        or cand.get("last_rearm_pass_lag_ms")
        or 999999
    )
    min_quote_tokens = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MIN_QUOTE_TOKENS", "150000")
    )
    max_quote_tokens = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_QUOTE_TOKENS", "760000")
    )
    max_current_sol = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_CURRENT_SOL", "2.65")
    )
    max_pre_entry_sol = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_PRE_ENTRY_SOL", "3.00")
    )
    max_pre_entry_buys = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_PRE_ENTRY_BUYS", "2")
    )
    min_top_share = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MIN_TOP_SHARE", "0.999")
    )
    max_delay_ms = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_REARM_DELAY_MS", "80")
    )
    max_lag_ms = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_SPEED_MAX_REARM_LAG_MS", "350")
    )
    max_authority_lag_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_MAX_AUTHORITY_LAG_MS",
            "1150",
        )
    )
    effective_lag_ms, raw_lag_ms, lag_source = (
        _v287_selected_seed_prior_effective_rearm_lag_ms(
            cand,
            max_delay_ms=max_delay_ms,
            max_authority_lag_ms=max_authority_lag_ms,
        )
    )
    min_rearm_sol = float(os.environ.get("V287_SEED_PRIOR_CARRY_REARM_MIN_SOL", "0.70"))
    min_rearm_buys = int(os.environ.get("V287_SEED_PRIOR_CARRY_MIN_REARM_BUYS", "2"))
    single_large_min_sol = float(
        os.environ.get("V287_SEED_PRIOR_CARRY_SINGLE_LARGE_REARM_MIN_SOL", "1.80")
    )
    rearm_count_ok = pre_entry_buys >= min_rearm_buys or (
        pre_entry_buys == 1 and pre_entry_sol >= single_large_min_sol
    )

    def _result(ok: bool, result_reason: str) -> tuple[bool, str]:
        cand["v287_seed_prior_speed_authority_eval"] = {
            "enabled": int(enabled),
            "reason": str(reason or ""),
            "quote_tokens": float(quote_tokens),
            "min_quote_tokens": float(min_quote_tokens),
            "max_quote_tokens": float(max_quote_tokens),
            "current_sol": float(current_sol),
            "max_current_sol": float(max_current_sol),
            "top_share": float(top_share),
            "min_top_share": float(min_top_share),
            "prev_sells": int(prev_sells),
            "pre_entry_buys": int(pre_entry_buys),
            "max_pre_entry_buys": int(max_pre_entry_buys),
            "pre_entry_sol": float(pre_entry_sol),
            "max_pre_entry_sol": float(max_pre_entry_sol),
            "min_rearm_sol": float(min_rearm_sol),
            "min_rearm_buys": int(min_rearm_buys),
            "single_large_min_sol": float(single_large_min_sol),
            "first_delay_ms": int(first_delay_ms),
            "last_delay_ms": int(last_delay_ms),
            "max_delay_ms": int(max_delay_ms),
            "last_lag_ms": int(effective_lag_ms),
            "raw_last_lag_ms": int(raw_lag_ms),
            "lag_source": str(lag_source),
            "max_lag_ms": int(max_lag_ms),
            "max_authority_lag_ms": int(max_authority_lag_ms),
            "pass": int(ok),
            "result_reason": str(result_reason),
        }
        return ok, result_reason

    if not enabled:
        return _result(False, "speed_authority_disabled")
    if reason != "selected_seed_prior_carry_rearm":
        return _result(False, "not_seed_prior_carry_rearm")
    if current_sol > max_current_sol:
        return _result(False, "speed_current")
    if prev_sells != 0:
        return _result(False, "speed_prev_sells")
    if top_share < min_top_share:
        return _result(False, "speed_top_share")
    if pre_entry_sol > max_pre_entry_sol or pre_entry_buys > max_pre_entry_buys:
        return _result(False, "speed_pre_entry")
    if pre_entry_sol < min_rearm_sol or not rearm_count_ok:
        return _result(False, "speed_rearm")
    if first_delay_ms > max_delay_ms or last_delay_ms > max_delay_ms:
        return _result(False, "speed_delay")
    if effective_lag_ms > max_lag_ms:
        return _result(False, "speed_lag")
    if quote_tokens < min_quote_tokens:
        return _result(False, "speed_quote_low")
    if max_quote_tokens > 0 and quote_tokens > max_quote_tokens:
        return _result(False, "speed_quote_high")
    return _result(True, "seed_prior_speed_clean_continuation")


def _v287_seed_prior_refresh_compression_ok(
    cand: dict[str, Any],
    reason: str,
    pre_refresh_quote_tokens: float,
    refresh_quote_tokens: float,
) -> tuple[bool, str]:
    if os.environ.get("V287_SELECTED_SEED_PRIOR_NEGATIVE_BYPASS_ENABLED", "0") != "1":
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    pre_refresh_quote_tokens = float(pre_refresh_quote_tokens)
    refresh_quote_tokens = float(refresh_quote_tokens)
    if pre_refresh_quote_tokens <= 0 or refresh_quote_tokens <= 0:
        return False, "quote"
    if pre_refresh_quote_tokens < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_COMPRESSION_MIN_PRE_TOKENS", "700000")
    ):
        return False, "pre_tokens"
    if not (
        float(os.environ.get("V287_SELECTED_SEED_PRIOR_COMPRESSION_MIN_REFRESH_TOKENS", "300000"))
        <= refresh_quote_tokens
        <= float(os.environ.get("V287_SELECTED_SEED_PRIOR_COMPRESSION_MAX_REFRESH_TOKENS", "450000"))
    ):
        return False, "refresh_tokens"
    ratio = refresh_quote_tokens / pre_refresh_quote_tokens
    if ratio > float(os.environ.get("V287_SELECTED_SEED_PRIOR_COMPRESSION_MAX_RATIO", "0.55")):
        return False, "ratio"
    if _v287_cand_pre_entry_sol(cand) < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_CAP_MIN_PRE_ENTRY_SOL", "2.00")
    ):
        return False, "pre_entry"
    if int(cand.get("pre_entry_buys") or 0) < 2:
        return False, "buys"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "top_share"
    return True, "fast_curve_compression"


def _v287_seed_prior_postplan_quote_floor_ok(
    cand: dict[str, Any],
    reason: str,
    pre_refresh_quote_tokens: float,
    refresh_quote_tokens: float,
    min_quote_tokens: float,
) -> tuple[bool, str]:
    """Allow a near-floor seed-prior refresh only after real post-plan flow.

    This is deliberately narrower than lowering the global quote-token floor:
    the live miss was a selected seed-prior candidate with confirmed post-plan
    buy continuation and a refresh quote just below the static 500k base floor.
    Low-quote compression/no-postplan shapes stay blocked.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) != 1:
        return False, "postplan_not_passed"
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    if post_plan_buys < 1:
        return False, "no_postplan_buy"
    if post_plan_sol < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MIN_SOL",
            "1.20",
        )
    ):
        return False, "weak_postplan_buy"
    refresh_quote_tokens = float(refresh_quote_tokens)
    pre_refresh_quote_tokens = float(pre_refresh_quote_tokens)
    min_quote_tokens = float(min_quote_tokens)
    if refresh_quote_tokens <= 0 or pre_refresh_quote_tokens <= 0:
        return False, "quote_missing"
    if refresh_quote_tokens >= min_quote_tokens:
        return False, "not_below_floor"
    if refresh_quote_tokens < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MIN_TOKENS",
            "470000",
        )
    ):
        return False, "too_few_tokens"
    max_drop_pct = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MAX_REFRESH_DROP_PCT",
            "0.25",
        )
    )
    if refresh_quote_tokens < pre_refresh_quote_tokens * (1.0 - (max_drop_pct / 100.0)):
        return False, "refresh_quote_drop"
    if int(cand.get("pre_entry_buys") or 0) < 2:
        return False, "pre_entry_buys"
    if _v287_cand_pre_entry_sol(cand) < float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_REQUIRE_POSTPLAN_ABOVE_SOL", "3.00")
    ):
        return False, "pre_entry_sol"
    max_delay_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MAX_REARM_DELAY_MS",
            "80",
        )
    )
    first_delay = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay = int(cand.get("last_rearm_pass_delay_ms") or first_delay)
    if first_delay > max_delay_ms or last_delay > max_delay_ms:
        return False, "rearm_delay"
    pass_ts_ms = int(cand.get("post_plan_rearm_pass_ts_ms") or 0)
    max_pass_age_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_POSTPLAN_QUOTE_FLOOR_MAX_PASS_AGE_MS",
            "500",
        )
    )
    if pass_ts_ms > 0 and max(0, _now_ms() - pass_ts_ms) > max_pass_age_ms:
        return False, "postplan_stale"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "top_share"
    return True, "postplan_quote_floor"


def _v287_seed_prior_preplan_quote_floor_ok(
    cand: dict[str, Any],
    reason: str,
    pre_refresh_quote_tokens: float,
    refresh_quote_tokens: float,
    min_quote_tokens: float,
) -> tuple[bool, str]:
    """Allow BAqG-style strong pre-plan seed-prior flow near the quote floor.

    The post-plan exception misses candidates where the plan becomes ready
    after several clean buys have already moved the curve. This path is not a
    global min-token relaxation: it requires the selected seed-prior lane,
    no post-plan pass, a strong clean pre-entry train, and a near-floor quote.
    """
    if (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_ENABLED",
            "1",
        )
        == "0"
    ):
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    if int(cand.get("post_plan_rearm_passed") or 0) == 1:
        return False, "postplan_should_use_postplan_path"
    refresh_quote_tokens = float(refresh_quote_tokens)
    pre_refresh_quote_tokens = float(pre_refresh_quote_tokens)
    min_quote_tokens = float(min_quote_tokens)
    if refresh_quote_tokens <= 0 or pre_refresh_quote_tokens <= 0:
        return False, "quote_missing"
    if pre_refresh_quote_tokens < min_quote_tokens:
        return False, "first_quote_did_not_pass"
    if refresh_quote_tokens >= min_quote_tokens:
        return False, "not_below_floor"
    if refresh_quote_tokens < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MIN_TOKENS",
            "470000",
        )
    ):
        return False, "too_few_tokens"
    if _v287_cand_pre_entry_sol(cand) < float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MIN_PRE_ENTRY_SOL",
            "4.50",
        )
    ):
        return False, "pre_entry_sol"
    if int(cand.get("pre_entry_buys") or 0) < int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MIN_BUYS", "4")
    ):
        return False, "pre_entry_buys"
    max_delay_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MAX_REARM_DELAY_MS",
            "150",
        )
    )
    first_delay = int(cand.get("first_rearm_pass_delay_ms") or 999999)
    last_delay = int(cand.get("last_rearm_pass_delay_ms") or first_delay)
    if first_delay > max_delay_ms or last_delay > max_delay_ms:
        return False, "rearm_delay"
    last_rearm_ts = int(cand.get("last_rearm_pass_ts_ms") or 0)
    max_lag_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MAX_LAST_REARM_LAG_MS",
            "650",
        )
    )
    if last_rearm_ts <= 0 or max(0, _now_ms() - last_rearm_ts) > max_lag_ms:
        return False, "rearm_stale"
    if float(cand.get("current_buy_sol") or 0.0) > float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_PREPLAN_QUOTE_FLOOR_MAX_CURRENT_SOL",
            "2.50",
        )
    ):
        return False, "current_buy_sol"
    if float(cand.get("top_share") or 0.0) < 0.999:
        return False, "top_share"
    return True, "preplan_quote_floor"


def _v287_seed_prior_weak_drift_watch_ok(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    drift_pct: float,
    now_ms: int,
) -> tuple[bool, str]:
    """Keep weak positive drift alive only for fresh follow-through proof.

    This does not authorize a buy. It only allows a selected seed-prior
    candidate to remain active for a short sell-sensitive watch after the final
    refresh showed weak positive movement. A later buy continuation must still
    satisfy the final watch-followthrough authority.
    """
    if os.environ.get("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_ENABLED", "1") == "0":
        return False, "disabled"
    if not _v287_is_selected_seed_prior(cand, reason):
        return False, "not_seed_prior"
    drift_pct = float(drift_pct)
    min_drift_pct = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_DRIFT_PCT", "0.00")
    )
    max_drift_pct = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_DRIFT_PCT", "1.00")
    )
    if drift_pct < min_drift_pct or drift_pct > max_drift_pct:
        return False, "drift_band"
    quote_tokens = float(quote_tokens)
    min_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_QUOTE_TOKENS",
            "600000",
        )
    )
    max_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_QUOTE_TOKENS",
            "760000",
        )
    )
    if quote_tokens < min_quote_tokens or quote_tokens > max_quote_tokens:
        return False, "quote_band"
    current_sol = float(cand.get("current_buy_sol") or 0.0)
    max_current_sol = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_CURRENT_SOL",
            "2.25",
        )
    )
    if current_sol > max_current_sol:
        return False, "current_buy_sol"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    max_pre_entry_sol = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_PRE_ENTRY_SOL",
            "3.50",
        )
    )
    if pre_entry_sol > max_pre_entry_sol:
        return False, "pre_entry_sol"
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    min_pre_entry_buys = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_PRE_ENTRY_BUYS", "2")
    )
    max_pre_entry_buys = int(
        os.environ.get("V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_PRE_ENTRY_BUYS", "2")
    )
    if pre_entry_buys < min_pre_entry_buys or pre_entry_buys > max_pre_entry_buys:
        return False, "pre_entry_buys"
    last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
    if last_rearm_ts_ms > 0:
        last_rearm_lag_ms = max(0, int(now_ms) - last_rearm_ts_ms)
    else:
        last_rearm_lag_ms = int(
            cand.get("last_rearm_lag_ms")
            or cand.get("last_rearm_pass_lag_ms")
            or cand.get("last_rearm_pass_delay_ms")
            or 999999
        )
    max_last_rearm_lag_ms = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MAX_LAST_REARM_LAG_MS",
            "650",
        )
    )
    if last_rearm_lag_ms > max_last_rearm_lag_ms:
        return False, "last_rearm_lag"
    if int(cand.get("prev_sells") or 0) != 0:
        return False, "prev_sells"
    top_share = float(cand.get("top_share") or 0.0)
    min_top_share = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MIN_TOP_SHARE",
            "0.999",
        )
    )
    if top_share < min_top_share:
        return False, "top_share"
    cand["v287_seed_prior_weak_drift_watch_eval"] = {
        "pass": 1,
        "drift_pct": float(drift_pct),
        "min_drift_pct": float(min_drift_pct),
        "max_drift_pct": float(max_drift_pct),
        "quote_tokens": float(quote_tokens),
        "min_quote_tokens": float(min_quote_tokens),
        "max_quote_tokens": float(max_quote_tokens),
        "current_sol": float(current_sol),
        "max_current_sol": float(max_current_sol),
        "pre_entry_sol": float(pre_entry_sol),
        "max_pre_entry_sol": float(max_pre_entry_sol),
        "pre_entry_buys": int(pre_entry_buys),
        "min_pre_entry_buys": int(min_pre_entry_buys),
        "max_pre_entry_buys": int(max_pre_entry_buys),
        "last_rearm_lag_ms": int(last_rearm_lag_ms),
        "max_last_rearm_lag_ms": int(max_last_rearm_lag_ms),
        "top_share": float(top_share),
        "min_top_share": float(min_top_share),
    }
    return True, "weak_positive_drift_watch_eligible"


def _v287_seed_prior_final_send_authority(
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    final_refresh_drift_pct: float,
    self_roundtrip_negative: bool,
) -> tuple[bool, str]:
    """Last send-boundary authority for seed-prior carry entries.

    Seed-prior may look strong from candidate-start shadow flow, but the live
    loss cases showed that shadow can be consumed before our actual send. For a
    negative immediate self-roundtrip, final-refresh quote drift alone is not
    authority: the send needs actual post-plan/watch followthrough.
    """
    if not _v287_is_selected_seed_prior(cand, reason):
        return True, "not_seed_prior"
    if not bool(self_roundtrip_negative):
        return True, "self_roundtrip_nonnegative"
    quote_tokens = float(quote_tokens)
    if quote_tokens <= 0:
        return False, "missing_quote_tokens"
    speed_ok, speed_reason = _v287_seed_prior_speed_authority_ok(
        cand,
        reason,
        quote_tokens,
    )
    drift_pct = float(final_refresh_drift_pct)
    post_plan_buys = int(cand.get("post_plan_followthrough_buys") or 0)
    post_plan_sol = _v287_cand_post_plan_sol(cand)
    required_postplan_min = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_REQUIRED_POSTPLAN_MIN_SOL", "0.50")
    )
    credible_postplan_ok, credible_postplan_reason = (
        _v287_seed_prior_credible_postplan_boundary_ok(
            cand,
            reason,
            quote_tokens=quote_tokens,
            drift_pct=drift_pct,
        )
    )
    if credible_postplan_ok:
        cand["seed_prior_credible_postplan_send_ok"] = 1
        if drift_pct <= 0.0:
            cand["seed_prior_credible_postplan_zero_watch_pending"] = 1
            return (
                False,
                "credible_postplan_zero_drift_requires_fresh_post_final_followthrough",
            )
        return True, credible_postplan_reason

    def _watch_followthrough_authority_ok(authority_reason: str) -> tuple[bool, str]:
        """Authorize a send only from fresh post-watch buy continuation.

        This deliberately does not revive the old negative bypass. The lane must
        already have entered a watch state, observed a later buy continuation,
        and still be sell-clean at the final send boundary.
        """
        send_enabled = (
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_SEND_ENABLED",
                "1",
            )
            != "0"
        )
        send_flag = int(cand.get("seed_prior_watch_followthrough_send_ok") or 0)
        prev_sells = int(cand.get("prev_sells") or 0)
        top_share = float(cand.get("top_share") or 0.0)
        min_top_share_env = "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_TOP_SHARE"
        if int(cand.get("seed_prior_weak_drift_watch") or 0) == 1:
            min_top_share_env = (
                "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_FOLLOWTHROUGH_MIN_TOP_SHARE"
            )
        min_top_share = float(
            os.environ.get(
                min_top_share_env,
                "0.999",
            )
        )
        follow_ts_ms = int(cand.get("seed_prior_watch_followthrough_ts_ms") or 0)
        max_follow_age_env = "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MAX_AGE_MS"
        if int(cand.get("seed_prior_weak_drift_watch") or 0) == 1:
            max_follow_age_env = (
                "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_FOLLOWTHROUGH_MAX_AGE_MS"
            )
        max_follow_age_ms = int(
            os.environ.get(
                max_follow_age_env,
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_POS_REFRESH_FOLLOWTHROUGH_MAX_AGE_MS",
                    "650",
                ),
            )
        )
        follow_age_ms = max(0, _now_ms() - follow_ts_ms) if follow_ts_ms > 0 else 999999
        follow_delta_lamports = int(
            cand.get("seed_prior_watch_followthrough_lamports") or 0
        )
        follow_delta_buys = int(cand.get("seed_prior_watch_followthrough_buys") or 0)
        if int(cand.get("seed_prior_consumed_postplan_zero_watch") or 0) == 1:
            min_follow_delta_lamports = int(
                float(
                    os.environ.get(
                        "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_FOLLOW_MIN_SOL",
                        "0.50",
                    )
                )
                * LAMPORTS_PER_SOL
            )
            min_follow_delta_buys = int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_FOLLOW_MIN_BUYS",
                    "1",
                )
            )
            max_zero_refresh_quote_tokens = float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MAX_QUOTE_TOKENS",
                    "760000",
                )
            )
        else:
            min_follow_delta_lamports = int(
                float(
                    os.environ.get(
                        "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_SOL",
                        "0.50",
                    )
                )
                * LAMPORTS_PER_SOL
            )
            min_follow_delta_buys = int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_MIN_BUYS",
                    "2",
                )
            )
            max_zero_refresh_quote_tokens = float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_ZERO_WATCH_MAX_QUOTE_TOKENS",
                    "620000",
                )
            )
        def _watch_result(ok: bool, result_reason: str) -> tuple[bool, str]:
            cand["v287_seed_prior_watch_followthrough_eval"] = {
                "send_enabled": int(send_enabled),
                "send_flag": int(send_flag),
                "prev_sells": int(prev_sells),
                "top_share": float(top_share),
                "min_top_share": float(min_top_share),
                "follow_ts_ms": int(follow_ts_ms),
                "follow_age_ms": int(follow_age_ms),
                "max_follow_age_ms": int(max_follow_age_ms),
                "follow_delta_lamports": int(follow_delta_lamports),
                "follow_delta_buys": int(follow_delta_buys),
                "min_follow_delta_lamports": int(min_follow_delta_lamports),
                "min_follow_delta_buys": int(min_follow_delta_buys),
                "quote_tokens": float(quote_tokens),
                "max_zero_refresh_quote_tokens": float(max_zero_refresh_quote_tokens),
                "pass": int(ok),
                "reason": str(result_reason),
            }
            return ok, result_reason

        if (
            authority_reason == "positive_refresh_watch_followthrough"
            and os.environ.get(
                "V287_SELECTED_SEED_PRIOR_GENERIC_POSITIVE_WATCH_SEND_ENABLED",
                "0",
            )
            != "1"
        ):
            return _watch_result(False, "positive_watch_requires_strict_fresh_branch")
        if (
            authority_reason == "weak_positive_refresh_watch_followthrough"
            and os.environ.get(
                "V287_SELECTED_SEED_PRIOR_GENERIC_WEAK_WATCH_SEND_ENABLED",
                "0",
            )
            != "1"
        ):
            return _watch_result(False, "weak_watch_requires_strict_fresh_branch")
        if not send_enabled:
            return _watch_result(False, "watch_followthrough_send_disabled")
        if send_flag != 1:
            return _watch_result(False, "no_watch_followthrough")
        if prev_sells != 0:
            return _watch_result(False, "watch_followthrough_prev_sells")
        if top_share < min_top_share:
            return _watch_result(False, "watch_followthrough_top_share")
        if follow_ts_ms <= 0:
            return _watch_result(False, "watch_followthrough_missing_ts")
        if follow_age_ms > max_follow_age_ms:
            return _watch_result(False, "watch_followthrough_stale")
        if (
            follow_delta_lamports < min_follow_delta_lamports
            or follow_delta_buys < min_follow_delta_buys
        ):
            return _watch_result(False, "watch_followthrough_weak")
        if (
            authority_reason == "zero_refresh_watch_followthrough"
            and quote_tokens > max_zero_refresh_quote_tokens
        ):
            return _watch_result(False, "watch_followthrough_quote_too_high")
        return _watch_result(True, authority_reason)

    if drift_pct <= 0.0:
        # The pre-send path can already prove a narrow zero-drift seed-prior
        # shape. Re-check and honor those flags before the broader speed/watch
        # negative-projection wait below; otherwise a real allow is converted
        # into an expire-only watch.
        if int(cand.get("seed_prior_consumed_postplan_send_ok") or 0) == 1:
            consumed_preauth_ok, consumed_preauth_reason = (
                _v287_seed_prior_consumed_postplan_authority_ok(
                    cand,
                    reason,
                    quote_tokens=quote_tokens,
                    drift_pct=drift_pct,
                )
            )
            cand["v287_seed_prior_zero_drift_preauth_eval"] = {
                "flag": "seed_prior_consumed_postplan_send_ok",
                "pass": int(consumed_preauth_ok),
                "reason": str(consumed_preauth_reason),
                "drift_pct": float(drift_pct),
                "quote_tokens": float(quote_tokens),
                "direct_send_disabled": 1,
            }
            if consumed_preauth_ok:
                return (
                    False,
                    "consumed_postplan_zero_drift_requires_fresh_post_final_followthrough",
                )

        zero_drift_pre_authorities = (
            (
                "seed_prior_one_strong_postplan_zero_drift_send_ok",
                _v287_seed_prior_one_strong_postplan_zerodrift_ok,
                "one_strong_postplan_zero_drift_authorized",
            ),
            (
                "seed_prior_speed_postplan_zero_drift_send_ok",
                _v287_seed_prior_speed_postplan_zerodrift_ok,
                "speed_postplan_zero_drift_authorized",
            ),
        )
        for flag_name, authority_fn, authorized_reason in zero_drift_pre_authorities:
            if int(cand.get(flag_name) or 0) != 1:
                continue
            preauth_ok, preauth_reason = authority_fn(
                cand,
                reason,
                quote_tokens=quote_tokens,
                drift_pct=drift_pct,
            )
            cand["v287_seed_prior_zero_drift_preauth_eval"] = {
                "flag": str(flag_name),
                "pass": int(preauth_ok),
                "reason": str(preauth_reason),
                "drift_pct": float(drift_pct),
                "quote_tokens": float(quote_tokens),
            }
            if preauth_ok:
                return True, authorized_reason
        fast_single_ok, fast_single_reason = (
            _v287_seed_prior_fast_single_rearm_zerodrift_ok(
                cand,
                reason,
                quote_tokens=quote_tokens,
                drift_pct=drift_pct,
            )
        )
        cand["v287_seed_prior_fast_single_rearm_eval"] = {
            "pass": int(fast_single_ok),
            "reason": str(fast_single_reason),
            "drift_pct": float(drift_pct),
            "quote_tokens": float(quote_tokens),
            "current_buy_sol": float(cand.get("current_buy_sol") or 0.0),
            "pre_entry_buys": int(cand.get("pre_entry_buys") or 0),
            "pre_entry_buy_sol": float(_v287_cand_pre_entry_sol(cand)),
        }
        if fast_single_ok:
            return True, fast_single_reason
        if int(cand.get("seed_prior_consumed_postplan_zero_watch") or 0) == 1:
            consumed_watch_ok, consumed_watch_reason = (
                _watch_followthrough_authority_ok(
                    "zero_refresh_watch_followthrough"
                )
            )
            if consumed_watch_ok:
                return True, "consumed_postplan_zero_watch_followthrough"
            if int(cand.get("seed_prior_watch_followthrough_send_ok") or 0) == 1:
                return False, consumed_watch_reason
        if speed_ok:
            cand["v287_seed_prior_speed_negative_projection_gate"] = {
                "speed_reason": str(speed_reason),
                "drift_pct": float(drift_pct),
                "quote_tokens": float(quote_tokens),
                "watch_active": int(
                    cand.get("seed_prior_speed_negative_projection_watch") or 0
                ),
                "self_roundtrip_negative": int(bool(self_roundtrip_negative)),
            }
            if int(cand.get("seed_prior_speed_negative_projection_watch") or 0) == 1:
                return False, "speed_negative_projection_still_negative"
            return False, "speed_negative_zero_drift_requires_post_final_followthrough"
        watch_send_ok, watch_send_reason = _watch_followthrough_authority_ok(
            "zero_refresh_watch_followthrough"
        )
        if watch_send_ok:
            return True, watch_send_reason
        neg_watch_send_ok, neg_watch_send_reason = (
            _v287_seed_prior_negative_refresh_followthrough_send_ok(
                cand,
                reason,
                quote_tokens,
                drift_pct,
                _now_ms(),
            )
        )
        if neg_watch_send_ok:
            return True, neg_watch_send_reason
        tiny_neg_ok, tiny_neg_reason = _v287_seed_prior_tiny_negative_drift_ok(
            cand,
            reason,
            quote_tokens,
            drift_pct,
            bool(self_roundtrip_negative),
            _now_ms(),
        )
        if tiny_neg_ok:
            return True, tiny_neg_reason
        if (
            int(cand.get("post_plan_rearm_passed") or 0) == 1
            and post_plan_buys >= 1
            and post_plan_sol >= required_postplan_min
        ):
            consumed_postplan_ok, consumed_postplan_reason = (
                _v287_seed_prior_consumed_postplan_authority_ok(
                    cand,
                    reason,
                    quote_tokens=quote_tokens,
                    drift_pct=drift_pct,
                )
            )
            if consumed_postplan_ok:
                cand["seed_prior_consumed_postplan_zero_watch_pending"] = 1
                cand["seed_prior_consumed_postplan_zero_watch_blocker"] = (
                    consumed_postplan_reason
                )
                return (
                    False,
                    "consumed_postplan_zero_drift_requires_fresh_post_final_followthrough",
                )
            return False, "postplan_followthrough_consumed_before_send_boundary"
        return False, "nonpositive_final_drift_negative_roundtrip"
    min_drift_pct = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_MIN_POSITIVE_DRIFT_PCT", "1.00")
    )
    if drift_pct < min_drift_pct:
        speed_positive_postplan_ok = (
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_POSTPLAN_ENABLED",
                "1",
            )
            != "0"
            and speed_ok
            and drift_pct >= 0.0
            and quote_tokens
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_POSTPLAN_MAX_QUOTE_TOKENS",
                    "760000",
                )
            )
            and int(cand.get("post_plan_rearm_passed") or 0) == 1
            and post_plan_buys
            >= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_MIN_POSTPLAN_BUYS",
                    "1",
                )
            )
            and post_plan_sol
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_MIN_POSTPLAN_SOL",
                    "0.70",
                )
            )
        )
        cand["v287_seed_prior_speed_positive_drift_eval"] = {
            "pass": int(speed_positive_postplan_ok),
            "speed_pass": int(speed_ok),
            "speed_reason": str(speed_reason),
            "drift_pct": float(drift_pct),
            "min_drift_pct": float(min_drift_pct),
            "post_plan_passed": int(cand.get("post_plan_rearm_passed") or 0),
            "post_plan_buys": int(post_plan_buys),
            "post_plan_sol": float(post_plan_sol),
            "quote_tokens": float(quote_tokens),
        }
        if speed_positive_postplan_ok:
            return True, "speed_positive_final_drift_postplan"
        moderate_drift_ok, moderate_drift_reason = (
            _v287_seed_prior_moderate_positive_drift_ok(
                cand,
                reason,
                quote_tokens=quote_tokens,
                drift_pct=drift_pct,
            )
        )
        if moderate_drift_ok:
            return True, moderate_drift_reason
        if int(cand.get("seed_prior_weak_drift_watch") or 0) == 1:
            watch_send_ok, watch_send_reason = _watch_followthrough_authority_ok(
                "weak_positive_refresh_watch_followthrough"
            )
            if watch_send_ok:
                return True, watch_send_reason
        return False, "weak_final_drift_negative_roundtrip"
    pre_entry_sol = _v287_cand_pre_entry_sol(cand)
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    current_sol = float(cand.get("current_buy_sol") or 0.0)
    require_postplan_above = float(
        os.environ.get("V287_SELECTED_SEED_PRIOR_REQUIRE_POSTPLAN_ABOVE_SOL", "3.00")
    )
    allow_drift_only_negative = (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_ALLOW_DRIFT_ONLY_NEGATIVE_ROUNDTRIP",
            "0",
        )
        == "1"
    )
    strong_min_drift_pct = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_NO_POSTPLAN_PCT",
            "8.00",
        )
    )
    strong_max_current_sol = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_CURRENT_SOL",
            "2.50",
        )
    )
    strong_max_pre_entry_sol = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_PRE_ENTRY_SOL",
            "3.50",
        )
    )
    strong_max_pre_entry_buys = int(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_PRE_ENTRY_BUYS",
            "2",
        )
    )
    strong_max_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_QUOTE_TOKENS",
            "760000",
        )
    )
    strong_min_top_share = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MIN_TOP_SHARE",
            "0.999",
        )
    )
    top_share = float(cand.get("top_share") or 0.0)
    strong_boundary_drift_ok = (
        allow_drift_only_negative
        and drift_pct >= strong_min_drift_pct
        and current_sol <= strong_max_current_sol
        and pre_entry_sol <= strong_max_pre_entry_sol
        and pre_entry_buys <= strong_max_pre_entry_buys
        and quote_tokens <= strong_max_quote_tokens
        and top_share >= strong_min_top_share
    )
    cand["v287_seed_prior_strong_drift_eval"] = {
        "allow": int(allow_drift_only_negative),
        "pass": int(strong_boundary_drift_ok),
        "drift_pct": float(drift_pct),
        "min_drift_pct": float(strong_min_drift_pct),
        "current_sol": float(current_sol),
        "max_current_sol": float(strong_max_current_sol),
        "pre_entry_sol": float(pre_entry_sol),
        "max_pre_entry_sol": float(strong_max_pre_entry_sol),
        "pre_entry_buys": int(pre_entry_buys),
        "max_pre_entry_buys": int(strong_max_pre_entry_buys),
        "quote_tokens": float(quote_tokens),
        "max_quote_tokens": float(strong_max_quote_tokens),
        "top_share": float(top_share),
        "min_top_share": float(strong_min_top_share),
    }
    speed_pos_min_drift_pct = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_MIN_PCT",
            "1.50",
        )
    )
    speed_pos_min_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_MIN_QUOTE_TOKENS",
            "540000",
        )
    )
    speed_pos_max_quote_tokens = float(
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_MAX_QUOTE_TOKENS",
            "790000",
        )
    )
    speed_positive_no_postplan_ok = (
        os.environ.get(
            "V287_SELECTED_SEED_PRIOR_SPEED_POSITIVE_DRIFT_NO_POSTPLAN_ENABLED",
            "1",
        )
        != "0"
        and speed_ok
        and post_plan_buys == 0
        and post_plan_sol <= 1e-12
        and drift_pct >= speed_pos_min_drift_pct
        and speed_pos_min_quote_tokens <= quote_tokens <= speed_pos_max_quote_tokens
    )
    cand["v287_seed_prior_speed_positive_no_postplan_eval"] = {
        "pass": int(speed_positive_no_postplan_ok),
        "speed_pass": int(speed_ok),
        "speed_reason": str(speed_reason),
        "drift_pct": float(drift_pct),
        "min_drift_pct": float(speed_pos_min_drift_pct),
        "quote_tokens": float(quote_tokens),
        "min_quote_tokens": float(speed_pos_min_quote_tokens),
        "max_quote_tokens": float(speed_pos_max_quote_tokens),
        "post_plan_buys": int(post_plan_buys),
        "post_plan_sol": float(post_plan_sol),
    }
    if speed_positive_no_postplan_ok:
        return True, "speed_positive_final_drift_no_postplan"
    # This branch is intentionally before post-plan rejection: live logs showed
    # strong_pass=1 could otherwise be converted into a wait/block by the
    # post-plan branch ordering.
    if strong_boundary_drift_ok:
        return True, "strong_positive_final_drift_no_postplan"
    if (
        require_postplan_above > 0.0
        and pre_entry_sol >= require_postplan_above
        and (post_plan_buys < 1 or post_plan_sol < required_postplan_min)
    ):
        return False, "missing_postplan_followthrough_for_heavy_pre_entry"
    if os.environ.get("V287_SELECTED_SEED_PRIOR_BLOCK_HEAVY_CURRENT_REARM", "1") != "0":
        heavy_current_min = float(
            os.environ.get("V287_SELECTED_SEED_PRIOR_HEAVY_CURRENT_MIN_SOL", "2.70")
        )
        heavy_rearm_min = float(
            os.environ.get("V287_SELECTED_SEED_PRIOR_HEAVY_REARM_MIN_SOL", "3.20")
        )
        if current_sol >= heavy_current_min and pre_entry_sol >= heavy_rearm_min:
            return False, "heavy_current_heavy_rearm_negative_roundtrip"
    watch_send_ok, watch_send_reason = _watch_followthrough_authority_ok(
        "positive_refresh_watch_followthrough"
    )
    if watch_send_ok:
        return True, watch_send_reason
    if (
        int(cand.get("post_plan_rearm_passed") or 0) == 1
        and post_plan_buys >= 1
        and post_plan_sol >= required_postplan_min
    ):
        last_rearm_ts_ms = int(cand.get("last_rearm_pass_ts_ms") or 0)
        if last_rearm_ts_ms > 0:
            last_rearm_lag_ms = max(0, _now_ms() - last_rearm_ts_ms)
        else:
            last_rearm_lag_ms = int(
                cand.get("last_rearm_lag_ms")
                or cand.get("last_rearm_pass_lag_ms")
                or cand.get("last_rearm_pass_delay_ms")
                or 999999
            )
        fresh_positive_postplan_ok = (
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_FRESH_POS_POSTPLAN_SEND_ENABLED",
                "1",
            )
            != "0"
            and drift_pct
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_FRESH_POS_MIN_DRIFT_PCT",
                    "2.00",
                )
            )
            and quote_tokens
            >= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_FRESH_POS_MIN_QUOTE_TOKENS",
                    "680000",
                )
            )
            and quote_tokens
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_QUOTE_TOKENS",
                    "760000",
                )
            )
            and last_rearm_lag_ms
            <= int(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_REARM_LAG_MS",
                    "650",
                )
            )
            and pre_entry_sol
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_PRE_ENTRY_SOL",
                    "4.00",
                )
            )
            and post_plan_sol
            <= float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_FRESH_POS_MAX_POSTPLAN_SOL",
                    "1.30",
                )
            )
            and float(cand.get("top_share") or 0.0) >= 0.999
        )
        if fresh_positive_postplan_ok:
            return True, "fresh_positive_final_drift_with_moderate_postplan_rearm"
        consumed_postplan_ok, consumed_postplan_reason = (
            _v287_seed_prior_consumed_postplan_authority_ok(
                cand,
                reason,
                quote_tokens=quote_tokens,
                drift_pct=drift_pct,
            )
        )
        if consumed_postplan_ok:
            return True, consumed_postplan_reason
        return False, "positive_refresh_requires_post_final_followthrough"
    if allow_drift_only_negative and strong_boundary_drift_ok:
        return True, "strong_positive_final_drift_no_postplan"
    if allow_drift_only_negative:
        return False, "positive_drift_only_not_strong_enough"
    return False, "missing_postplan_followthrough_for_negative_roundtrip"


def _v287_log_seed_prior_cap_override(
    *,
    mint: str,
    cand: dict[str, Any],
    reason: str,
    quote_tokens: float,
    max_quote_tokens: float,
    source: str,
) -> None:
    _log(
        "PGG2-V287-SEED-PRIOR-TOKEN-CAP-OVERRIDE "
        f"mint={_short(mint)} full_mint={mint} "
        f"reason={reason} amount_out_tokens={float(quote_tokens):.6f} "
        f"base_max_tokens={float(max_quote_tokens):.6f} "
        f"clean_max_tokens={float(os.environ.get('V287_SELECTED_SEED_PRIOR_CLEAN_MAX_QUOTE_TOKENS', '825000')):.6f} "
        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
        f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
        f"first_rearm_delay_ms={int(cand.get('first_rearm_pass_delay_ms') or 0)} "
        f"last_rearm_delay_ms={int(cand.get('last_rearm_pass_delay_ms') or 0)} "
        f"early_clean_cap_override={int(cand.get('seed_prior_early_clean_cap_override_ok') or 0)} "
        f"early_clean_max_tokens={float(os.environ.get('V287_SELECTED_SEED_PRIOR_EARLY_CLEAN_CAP_MAX_QUOTE_TOKENS', '870000')):.6f} "
        f"watch_cap_override={int(cand.get('seed_prior_watch_cap_override_ok') or 0)} "
        f"postplan_cap_override={int(cand.get('seed_prior_postplan_cap_override_ok') or 0)} "
        f"watch_delta_sol={int(cand.get('seed_prior_watch_followthrough_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
        f"watch_delta_buys={int(cand.get('seed_prior_watch_followthrough_buys') or 0)} "
        f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
        f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
        f"postplan_clean_max_tokens={float(os.environ.get('V287_SELECTED_SEED_PRIOR_POSTPLAN_CAP_MAX_QUOTE_TOKENS', '925000')):.6f} "
        f"source={source}"
    )


def _v287_reason_min_token_headroom_pct(
    reason: str,
    cand: dict[str, Any] | None = None,
) -> float:
    """Require extra token headroom only where recent live failed-buy fees proved it."""
    reason = str(reason or "")
    if reason in {
        "selected_single_prior_strong_rearm",
        "selected_single_prior_no_movement_followthrough",
    }:
        return max(
            0.0,
            float(
                os.environ.get(
                    "V287_SELECTED_SINGLE_PRIOR_MIN_TOKEN_HEADROOM_PCT",
                    "5.00",
                )
            ),
        )
    if reason == "selected_fresh_single_mid_rearm":
        return max(
            0.0,
            float(
                os.environ.get(
                    "V287_SELECTED_FRESH_SINGLE_MID_MIN_TOKEN_HEADROOM_PCT",
                    "5.00",
                )
            ),
        )
    if reason == "selected_seed_prior_carry_rearm":
        if cand is not None and int(cand.get("seed_prior_speed_postplan_zero_drift_send_ok") or 0) == 1:
            return max(
                0.0,
                float(
                    os.environ.get(
                        "V287_SELECTED_SEED_PRIOR_SPEED_POSTPLAN_MIN_TOKEN_HEADROOM_PCT",
                        "4.90",
                    )
                ),
            )
        return max(
            0.0,
            float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_MIN_TOKEN_HEADROOM_PCT",
                    "5.00",
                )
            ),
        )
    if reason == "selected_seed_prior_single_strong_rearm":
        return max(
            0.0,
            float(
                os.environ.get(
                    "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_MIN_TOKEN_HEADROOM_PCT",
                    "5.00",
                )
            ),
        )
    return 0.0


def _v287_buy_quote_headroom_ok(
    *,
    mint: str,
    cand: dict[str, Any] | None = None,
    reason: str,
    quote_tokens: float,
    min_quote_tokens: float,
    source: str,
) -> bool:
    min_headroom_pct = _v287_reason_min_token_headroom_pct(reason, cand)
    if min_headroom_pct <= 0.0 or min_quote_tokens <= 0.0:
        return True
    required_tokens = min_quote_tokens * (1.0 + (min_headroom_pct / 100.0))
    headroom_pct = ((quote_tokens - min_quote_tokens) / min_quote_tokens) * 100.0
    ok = quote_tokens >= required_tokens
    _log(
        "PGG2-V287-BUY-QUOTE-HEADROOM-CHECK "
        f"mint={_short(mint)} full_mint={mint} "
        f"reason={reason or '-'} "
        f"amount_out_tokens={quote_tokens:.6f} "
        f"min_tokens={min_quote_tokens:.6f} "
        f"required_tokens={required_tokens:.6f} "
        f"headroom_pct={headroom_pct:+.3f} "
        f"min_headroom_pct={min_headroom_pct:.3f} "
        f"pass={int(ok)} source={source}"
    )
    return ok


def _v287_seed_prior_only_live_mode() -> bool:
    return os.environ.get("V287_LIVE_ONLY_SEED_PRIOR_CARRY", "1") != "0"


def _v287_seed_prior_only_send_allowed(
    *,
    mint: str,
    cand: dict[str, Any],
    reason: str,
    counters: Counter[str],
    source: str,
) -> bool:
    if not _v287_seed_prior_only_live_mode():
        return True
    if str(reason or "") in {
        "selected_seed_prior_carry_rearm",
        "selected_seed_prior_single_strong_rearm",
    }:
        return True
    if (
        os.environ.get("V287_SEED_PRIOR_ONLY_ALLOW_SINGLE_PRIOR_STRONG", "0") == "1"
        and str(reason or "") == "selected_single_prior_strong_rearm"
        and str(cand.get("top_lane") or "") == "single_prior_buy_continuation"
        and int(cand.get("no_movement_watch_keeps") or 0) <= 0
    ):
        counters["seed_prior_only_single_prior_strong_send_allow"] += 1
        _log(
            "PGG2-V287-SEED-PRIOR-ONLY-SINGLE-PRIOR-STRONG-SEND-ALLOW "
            f"mint={_short(mint)} full_mint={mint} "
            f"reason={reason or '-'} "
            f"top_lane={str(cand.get('top_lane') or '')} "
            f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
            f"prev_buy_sol={float(cand.get('prev_buy_sol') or 0.0):.6f} "
            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
            f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
            "source=winner_lane_allowlist"
        )
        return True
    counters["seed_prior_only_non_winner_send_block"] += 1
    _log(
        "PGG2-V287-SEED-PRIOR-ONLY-SEND-BLOCK "
        f"mint={_short(mint)} full_mint={mint} "
        f"reason={reason or '-'} "
        f"top_lane={str(cand.get('top_lane') or '')} "
        f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
        f"prev_buy_sol={float(cand.get('prev_buy_sol') or 0.0):.6f} "
        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
        f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
        "allowed_reason=selected_seed_prior_carry_rearm "
        f"source={source}"
    )
    return False


_V287_SINGLE_LANE_ALLOWED_REASON = "selected_single_prior_strong_rearm"


def _v287_single_lane_only_mode() -> bool:
    return os.environ.get("V287_SINGLE_LANE_ONLY", "0") == "1"


def _v287_single_lane_firewall_ok(
    *,
    mint: str,
    cand: dict[str, Any],
    reason: str,
    counters: Counter[str],
    source: str,
) -> bool:
    """Hard single-lane firewall at the live-buy send site.

    When V287_SINGLE_LANE_ONLY=1 the ONLY live buy permitted is the empirically
    winning lane: ``selected_single_prior_strong_rearm`` on the
    ``single_prior_buy_continuation`` shape with no active no-movement watch.
    Every other authority/reason is shadow-blocked here, regardless of which of
    the ~30 upstream authority paths set ``continuation_reason`` -- because this
    runs at the narrow waist right before ``broker.send_signed``.

    Evidence (empirical authority ledger, 2026-06-07, tools/v287_authority_ledger.py
    over all _v287_all_logs): this lane = +0.027248 SOL, 2W/1L/3burn
    (522f +0.0151, 2NFj +0.0132); all 26 other authorities net-negative
    (machine-wide realized = -0.0374 SOL / 49 sends). Default OFF preserves the
    frozen multi-authority behavior exactly.
    """
    if not _v287_single_lane_only_mode():
        return True
    reason = str(reason or "")
    top_lane = str(cand.get("top_lane") or "")
    no_move_keeps = int(cand.get("no_movement_watch_keeps") or 0)
    allowed = (
        reason == _V287_SINGLE_LANE_ALLOWED_REASON
        and top_lane == "single_prior_buy_continuation"
        and no_move_keeps <= 0
    )
    if allowed:
        counters["single_lane_firewall_allow"] += 1
        _log(
            "PGG2-V287-SINGLE-LANE-FIREWALL-ALLOW "
            f"mint={_short(mint)} full_mint={mint} reason={reason} "
            f"top_lane={top_lane} source={source}"
        )
        return True
    counters["single_lane_firewall_block"] += 1
    _log(
        "PGG2-V287-SINGLE-LANE-FIREWALL-BLOCK "
        f"mint={_short(mint)} full_mint={mint} reason={reason or '-'} "
        f"top_lane={top_lane} no_movement_watch_keeps={no_move_keeps} "
        f"allowed_reason={_V287_SINGLE_LANE_ALLOWED_REASON} source={source}"
    )
    return False


def _v287_assert_single_lane_config() -> None:
    """Defense-in-depth lock applied at startup when V287_SINGLE_LANE_ONLY=1.

    Pins the winning lane and its allowlist gate ON, then prints a banner. No
    effect unless V287_SINGLE_LANE_ONLY=1, so the frozen launcher is unchanged.
    The send-site firewall (above) is the hard guarantee; this is belt-and-braces
    so the existing ``_v287_seed_prior_only_send_allowed`` gate agrees.
    """
    if not _v287_single_lane_only_mode():
        return
    os.environ["V287_ENABLE_SINGLE_PRIOR_BUY_LANE"] = "1"
    os.environ["V287_SEED_PRIOR_ONLY_ALLOW_SINGLE_PRIOR_STRONG"] = "1"
    os.environ["V287_LIVE_ONLY_SEED_PRIOR_CARRY"] = "1"
    _log(
        "PGG2-V287-SINGLE-LANE-ONLY-ARMED "
        f"allowed_reason={_V287_SINGLE_LANE_ALLOWED_REASON} "
        "allowed_top_lane=single_prior_buy_continuation "
        "other_authorities=shadow_blocked_at_send_site"
    )


def _v287_reason_min_guard_mode(reason: str) -> str:
    reason = str(reason or "")
    if reason in {
        "selected_single_prior_strong_rearm",
        "selected_single_prior_no_movement_followthrough",
    }:
        return str(
            os.environ.get("V287_SELECTED_SINGLE_PRIOR_MIN_GUARD_MODE", "floor")
            or "floor"
        ).strip().lower()
    if reason == "selected_fresh_single_mid_rearm":
        return str(
            os.environ.get("V287_SELECTED_FRESH_SINGLE_MID_MIN_GUARD_MODE", "floor")
            or "floor"
        ).strip().lower()
    if reason == "selected_seed_prior_carry_rearm":
        return str(
            os.environ.get("V287_SELECTED_SEED_PRIOR_MIN_GUARD_MODE", "floor")
            or "floor"
        ).strip().lower()
    if reason == "selected_seed_prior_single_strong_rearm":
        return str(
            os.environ.get(
                "V287_SELECTED_SEED_PRIOR_SINGLE_STRONG_MIN_GUARD_MODE",
                "floor",
            )
            or "floor"
        ).strip().lower()
    return "slippage"


def _v287_buy_min_guard_tokens(
    *,
    mint: str,
    reason: str,
    quote_tokens: float,
    min_quote_tokens: float,
    buy_slippage_pct: float,
    source: str,
) -> float:
    slippage_tokens = quote_tokens * max(0.0, 1.0 - (buy_slippage_pct / 100.0))
    mode = _v287_reason_min_guard_mode(reason)
    if mode == "floor":
        guard_tokens = float(min_quote_tokens)
    else:
        mode = "slippage"
        guard_tokens = max(float(slippage_tokens), float(min_quote_tokens))
    _log(
        "PGG2-V287-MIN-TOKEN-GUARD-CHECK "
        f"mint={_short(mint)} full_mint={mint} "
        f"reason={reason or '-'} mode={mode} "
        f"quote_tokens={quote_tokens:.6f} "
        f"slippage_tokens={slippage_tokens:.6f} "
        f"min_quote_tokens={min_quote_tokens:.6f} "
        f"guard_tokens={guard_tokens:.6f} "
        f"source={source}"
    )
    if guard_tokens <= 0.0:
        raise RuntimeError("v287_min_token_guard_zero")
    return guard_tokens


def _v287_fresh_clean_carry_reclass_ready(cand: dict[str, Any], now_ms: int) -> tuple[bool, str]:
    """Promote a fresh/no-prior candidate only after clean continuation proves it.

    This does not re-enable the old fresh actual lane. A candidate starts as
    shadow-only, must remain sell-free, and must accumulate enough follow-through
    before it can use the selected/protected send path.
    """
    if os.environ.get("V287_ENABLE_FRESH_CLEAN_CARRY_RECLASS", "1") == "0":
        return False, "disabled"
    if str(cand.get("top_lane") or "") != "fresh_impulse":
        return False, "not_fresh"
    if float(cand.get("prev_buy_sol") or 0.0) > 1e-12:
        return False, "prior_present"
    age_ms = max(0, int(now_ms) - int(cand.get("start_ms") or now_ms))
    max_age_ms = int(os.environ.get("V287_FRESH_CLEAN_CARRY_MAX_AGE_MS", "1100"))
    if age_ms > max_age_ms:
        return False, "age"
    min_buys = int(os.environ.get("V287_FRESH_CLEAN_CARRY_MIN_BUYS", "3"))
    min_sol = float(os.environ.get("V287_FRESH_CLEAN_CARRY_MIN_SOL", "3.50"))
    pre_entry_buys = int(cand.get("pre_entry_buys") or 0)
    pre_entry_sol = int(cand.get("pre_entry_buy_lamports") or 0) / LAMPORTS_PER_SOL
    if pre_entry_buys < min_buys:
        return False, "buys"
    if pre_entry_sol < min_sol:
        return False, "sol"
    max_lag_ms = int(os.environ.get("V287_FRESH_CLEAN_CARRY_MAX_LAST_BUY_LAG_MS", "350"))
    last_lag_ms = max(0, int(now_ms) - int(cand.get("last_rearm_pass_ts_ms") or now_ms))
    if last_lag_ms > max_lag_ms:
        return False, "lag"
    return True, "clean_carry"


def _v287_fresh_actual_should_wait(cand: dict[str, Any], now_ms: int) -> bool:
    if os.environ.get("V287_ENABLE_FRESH_CLEAN_CARRY_RECLASS", "1") == "0":
        return False
    if str(cand.get("top_lane") or "") != "fresh_impulse":
        return False
    if float(cand.get("prev_buy_sol") or 0.0) > 1e-12:
        return False
    age_ms = max(0, int(now_ms) - int(cand.get("start_ms") or now_ms))
    return age_ms <= int(os.environ.get("V287_FRESH_CLEAN_CARRY_MAX_AGE_MS", "1100"))


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
                quote_min_out = int(float((quote.get("rate") or {}).get("minAmountOut") or 0.0) * LAMPORTS_PER_SOL)
                configured_floor = max(
                    100,
                    int(os.environ.get("V287_NO_SCRATCH_EMERGENCY_MIN_LAMPORTS", "20000")),
                )
                # This path is already a no-scratch emergency exit. Using the
                # quote slippage floor here caused Pump 6003 and left a
                # Token-2022 residual; use a tiny explicit nonzero floor so the
                # close can land instead of fee-burning.
                emergency_min = max(100, min(configured_floor, max(100, expected_out - 1)))
                _log(
                    "PGG2-V287-NO-SCRATCH-EMERGENCY-CLOSE "
                    f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                    f"selected_min={emergency_min} quote_min_out={quote_min_out} "
                    f"configured_floor={configured_floor} reason=maxhold_scratch_not_executable"
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
                    configured_floor = max(
                        100,
                        int(os.environ.get("V287_NO_SCRATCH_EMERGENCY_MIN_LAMPORTS", "20000")),
                    )
                    emergency_min = max(100, min(configured_floor, max(100, expected_out - 1)))
                    _log(
                        "PGG2-V287-NO-SCRATCH-EMERGENCY-CLOSE "
                        f"mint={_short(mint)} expected_out={expected_out} scratch_min={scratch_min} "
                        f"selected_min={emergency_min} quote_min_out={quote_min_out} "
                        f"configured_floor={configured_floor} reason=target_fail_scratch_not_executable"
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
    _v287_assert_single_lane_config()
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
    ap.add_argument("--max-active-candidates", type=int, default=int(os.environ.get("V287_MAX_ACTIVE_CANDIDATES", "12")))
    ap.add_argument(
        "--skip-startup-token-check",
        action="store_true",
        default=os.environ.get("V287_SKIP_STARTUP_TOKEN_CHECK", "0") == "1",
    )
    args = ap.parse_args()

    if _v287_seed_prior_only_live_mode():
        for attr in (
            "enable_fresh_impulse_lane",
            "enable_single_prior_buy_lane",
            "enable_two_prior_buy_lane",
            "enable_low_top_lane",
            "enable_oversize_train_lane",
        ):
            if hasattr(args, attr):
                if (
                    attr == "enable_single_prior_buy_lane"
                    and os.environ.get(
                        "V287_SEED_PRIOR_ONLY_ALLOW_SINGLE_PRIOR_STRONG",
                        "0",
                    )
                    == "1"
                ):
                    setattr(args, attr, True)
                    continue
                setattr(args, attr, False)
        for key in (
            "V287_EDGE_TOP_ENABLED",
            "V287_ENABLE_DUST_PRIOR_CONTINUATION_LANE",
            "V287_ENABLE_FRESH_CLEAN_CARRY_RECLASS",
            "V287_ENABLE_HIGH_CURRENT_CLEAN_TRAIN_LANE",
            "V287_ENABLE_HIGH_CURRENT_REARM_LANE",
            "V287_ENABLE_VERIFIED_HOT_TRAIN",
            "V287_ENABLE_VERIFIED_LOW_REARM_CONTINUATION",
            "V287_SELECTED_FRESH_ACTUAL_ENABLED",
            "V287_SELECTED_FRESH_SINGLE_MID_ACTUAL_ENABLED",
            "V287_SELECTED_NO_MOVEMENT_FOLLOWTHROUGH",
        ):
            os.environ[key] = "0"
        os.environ["V287_ENABLE_SEED_PRIOR_CARRY_LANE"] = "1"

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
    if prebuy_min_self_roundtrip_delta < 0:
        _log(
            "PGG2-V287-FATAL prebuy_self_roundtrip_floor_negative "
            f"min_delta={prebuy_min_self_roundtrip_delta} floor=0 "
            "reason=selected_fingerprint_authority_required"
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
        f"dust_prior_enabled={int(os.environ.get('V287_ENABLE_DUST_PRIOR_CONTINUATION_LANE', '1') != '0')} "
        f"dust_prior_prev_max={float(os.environ.get('V287_DUST_PRIOR_PREV_MAX_SOL', '0.50')):.2f} "
        f"dust_prior_rearm=[{float(os.environ.get('V287_DUST_PRIOR_REARM_MIN_SOL', '0.35')):.2f},{float(os.environ.get('V287_DUST_PRIOR_REARM_MAX_SOL', '2.00')):.2f}] "
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
    _log(
        "PGG2-V287-SEED-PRIOR-ONLY-MODE "
        f"enabled={int(_v287_seed_prior_only_live_mode())} "
        "allowed_reasons=selected_seed_prior_carry_rearm,selected_seed_prior_single_strong_rearm "
        f"fresh_enabled={int(bool(args.enable_fresh_impulse_lane))} "
        f"single_prior_enabled={int(bool(args.enable_single_prior_buy_lane))} "
        f"two_prior_enabled={int(bool(args.enable_two_prior_buy_lane))} "
        f"dust_prior_enabled={int(os.environ.get('V287_ENABLE_DUST_PRIOR_CONTINUATION_LANE', '1') != '0')} "
        f"fresh_single_mid_actual={int(os.environ.get('V287_SELECTED_FRESH_SINGLE_MID_ACTUAL_ENABLED', '0') == '1')} "
        f"strong_drift_allow={int(os.environ.get('V287_SELECTED_SEED_PRIOR_ALLOW_DRIFT_ONLY_NEGATIVE_ROUNDTRIP', '0') == '1')} "
        f"strong_drift_min_pct={float(os.environ.get('V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_NO_POSTPLAN_PCT', '8.00')):.3f} "
        f"strong_current_max={float(os.environ.get('V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_CURRENT_SOL', '2.50')):.3f} "
        f"strong_pre_entry_max={float(os.environ.get('V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_PRE_ENTRY_SOL', '3.50')):.3f} "
        f"strong_buys_max={int(os.environ.get('V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_PRE_ENTRY_BUYS', '2'))} "
        f"strong_quote_max={float(os.environ.get('V287_SELECTED_SEED_PRIOR_STRONG_DRIFT_MAX_QUOTE_TOKENS', '760000')):.3f}"
    )

    stub = geyser_pb2_grpc.GeyserStub(grpc.secure_channel(str(args.endpoint), grpc.ssl_channel_credentials()))
    metadata = [(str(args.metadata_key), token)]
    hist: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=256))
    active: dict[str, dict[str, Any]] = {}
    shadows: dict[str, dict[str, Any]] = {}
    curve_cache_by_key: dict[str, dict[str, Any]] = {}
    seen_sigs: set[str] = set()
    counters: Counter[str] = Counter()
    start_time = time.time()
    prewarm_pool = ThreadPoolExecutor(max_workers=4)
    last_blockhash_warm_ms = 0
    blockhash_warm_future: Any = None
    last_global_warm_ms = 0
    global_warm_future: Any = None

    try:
        for update in stub.Subscribe(_v287_request_iter(args), metadata=metadata):
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
            curve_update = _v287_curve_from_account_update(update)
            if curve_update is not None:
                curve_key, curve, slot, ts_ms = curve_update
                curve_cache_by_key[curve_key] = {
                    "curve": curve,
                    "slot": int(slot),
                    "ts_ms": int(ts_ms),
                }
                counters["geyser_curve_account_update"] += 1
                max_items = int(os.environ.get("V287_GEYSER_CURVE_CACHE_MAX_ITEMS", "4096"))
                if len(curve_cache_by_key) > max_items:
                    oldest = sorted(
                        curve_cache_by_key.items(),
                        key=lambda kv: int(kv[1].get("ts_ms") or 0),
                    )[:512]
                    for old_key, _old in oldest:
                        curve_cache_by_key.pop(old_key, None)
                    counters["geyser_curve_cache_prune"] += 1
                if (
                    counters["geyser_curve_account_update"] <= 5
                    or any(
                        curve_key == _v287_curve_key_for_mint(str(active_cand.get("mint") or ""))
                        for active_cand in active.values()
                        if str(active_cand.get("mint") or "")
                    )
                ):
                    _log(
                        "PGG2-V287-GEYSER-CURVE-CACHE-UPDATE "
                        f"curve={_short(curve_key)} slot={slot} "
                        f"vt={int(curve.virtual_token_reserves)} "
                        f"vs={int(curve.virtual_sol_reserves)} "
                        f"rt={int(curve.real_token_reserves)} "
                        f"rs={int(curve.real_sol_reserves)}"
                    )
                continue
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
                if not _candidate_expired(active_cand, now):
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
                if _candidate_expired(cand, now):
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
                    rec_sol_lamports = int(rec["sol_lamports"])
                    max_event_buy_lamports = int(
                        float(os.environ.get("V287_EVENT_BUY_MAX_SOL_SANITY", "20.00"))
                        * LAMPORTS_PER_SOL
                    )
                    if rec_sol_lamports <= 0 or rec_sol_lamports > max_event_buy_lamports:
                        counters["event_buy_sanity_block"] += 1
                        _log(
                            "PGG2-V287-EVENT-BUY-SANITY-BLOCK "
                            f"mint={_short(mint)} full_mint={mint} "
                            f"sol_lamports={rec_sol_lamports} "
                            f"max_lamports={max_event_buy_lamports} "
                            "source=feed_decode_guard"
                        )
                        continue
                    _refresh_static_plan_on_rearm_accounts(
                        broker=broker,
                        prewarm_pool=prewarm_pool,
                        cand=cand,
                        rec=rec,
                        amount_sol=float(args.size_sol),
                        counters=counters,
                    )
                    cand["pre_entry_buys"] += 1
                    cand["pre_entry_buy_lamports"] += rec_sol_lamports
                    cand_rearm_min_lamports = int(cand.get("rearm_min_lamports") or int(float(args.rearm_min_sol) * LAMPORTS_PER_SOL))
                    cand_rearm_max_lamports = int(cand.get("rearm_max_lamports") or 0)
                    if cand_rearm_max_lamports > 0 and cand["pre_entry_buy_lamports"] > cand_rearm_max_lamports:
                        frozen_rearm_lamports = int(
                            cand.get("last_inband_rearm_lamports") or 0
                        )
                        frozen_rearm_ts_ms = int(
                            cand.get("last_inband_rearm_ts_ms") or 0
                        )
                        frozen_rearm_age_ms = (
                            now - frozen_rearm_ts_ms if frozen_rearm_ts_ms > 0 else 10**9
                        )
                        allow_frozen_rearm = (
                            os.environ.get(
                                "V287_ALLOW_FROZEN_INBAND_REARM_AFTER_PLAN_READY",
                                "1",
                            )
                            != "0"
                            and bool(cand.get("post_plan_rearm_required"))
                            and frozen_rearm_lamports >= cand_rearm_min_lamports
                            and frozen_rearm_lamports <= cand_rearm_max_lamports
                            and frozen_rearm_age_ms
                            <= int(
                                os.environ.get(
                                    "V287_FROZEN_INBAND_REARM_MAX_AGE_MS",
                                    os.environ.get(
                                        "V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS",
                                        "5",
                                    ),
                                )
                            )
                        )
                        if allow_frozen_rearm:
                            counters["frozen_inband_rearm_after_plan_allow"] += 1
                            cand["post_plan_rearm_required"] = 0
                            cand["frozen_inband_rearm_after_plan"] = 1
                            frozen_reason = str(
                                cand.get("selected_plan_ready_reason")
                                or cand.get("no_movement_watch_reason")
                                or ""
                            )
                            frozen_base_lamports = int(
                                cand.get("post_plan_rearm_base_lamports") or 0
                            )
                            frozen_base_buys = int(
                                cand.get("post_plan_rearm_base_buys") or 0
                            )
                            frozen_delta_lamports = (
                                max(
                                    0,
                                    int(cand.get("pre_entry_buy_lamports") or 0)
                                    - frozen_base_lamports,
                                )
                                if frozen_base_lamports > 0
                                else 0
                            )
                            frozen_delta_buys = (
                                max(
                                    0,
                                    int(cand.get("pre_entry_buys") or 0)
                                    - frozen_base_buys,
                                )
                                if frozen_base_buys > 0
                                else 0
                            )
                            frozen_postplan_credit_ok = (
                                _v287_credit_seed_prior_postplan_followthrough(
                                    cand,
                                    frozen_reason,
                                    delta_lamports=frozen_delta_lamports,
                                    delta_buys=frozen_delta_buys,
                                    ts_ms=now,
                                    source="frozen_inband_rearm_after_plan",
                                    mint=mint,
                                    counters=counters,
                                )
                            )
                            _log(
                                "PGG2-V287-FROZEN-INBAND-REARM-AFTER-PLAN-ALLOW "
                                f"mint={_short(mint)} full_mint={mint} "
                                f"top_lane={cand.get('top_lane', 'unknown')} "
                                f"reason={frozen_reason or '-'} "
                                f"frozen_rearm_sol={frozen_rearm_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"current_train_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                f"post_plan_delta_sol={frozen_delta_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"post_plan_delta_buys={int(frozen_delta_buys)} "
                                f"postplan_credit={int(frozen_postplan_credit_ok)} "
                                f"rearm_min_sol={cand_rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                                f"frozen_rearm_age_ms={frozen_rearm_age_ms} "
                                f"delay_ms={now-int(cand['start_ms'])} "
                                "source=plan_latency_bridge"
                            )
                        else:
                            allow_frozen_rearm = False
                        if str(cand.get("top_lane", "")) in {
                            "single_prior_buy_continuation",
                            "two_prior_buy_continuation",
                        } and not allow_frozen_rearm:
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
                        if not allow_frozen_rearm:
                            (
                                oversize_watch_state,
                                oversize_watch_reason,
                                oversize_watch_delta_lamports,
                                oversize_watch_delta_buys,
                            ) = _v287_seed_prior_watch_followthrough_state(cand, now)
                            if oversize_watch_state == "followthrough":
                                allow_frozen_rearm = True
                                cand["seed_prior_oversize_watch_followthrough"] = 1
                                cand["seed_prior_watch_followthrough_send_ok"] = 1
                                cand[
                                    "seed_prior_watch_followthrough_lamports"
                                ] = int(oversize_watch_delta_lamports)
                                cand["seed_prior_watch_followthrough_buys"] = int(
                                    oversize_watch_delta_buys
                                )
                                cand["seed_prior_watch_followthrough_ts_ms"] = now
                                cand.setdefault(
                                    "selected_plan_ready_reason",
                                    oversize_watch_reason,
                                )
                                cand.setdefault("selected_plan_ready_ts_ms", now)
                                if cand.get("post_plan_rearm_required"):
                                    _v287_credit_seed_prior_postplan_followthrough(
                                        cand,
                                        oversize_watch_reason,
                                        delta_lamports=oversize_watch_delta_lamports,
                                        delta_buys=oversize_watch_delta_buys,
                                        ts_ms=now,
                                        source="oversize_watch_followthrough_after_plan",
                                        mint=mint,
                                        counters=counters,
                                    )
                                counters[
                                    "seed_prior_oversize_watch_followthrough_restore"
                                ] += 1
                                _log(
                                    "PGG2-V287-SEED-PRIOR-OVERSIZE-WATCH-FOLLOWTHROUGH-RESTORE "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"reason={oversize_watch_reason} "
                                    f"delta_buy_sol={oversize_watch_delta_lamports/LAMPORTS_PER_SOL:.6f} "
                                    f"delta_buys={int(oversize_watch_delta_buys)} "
                                    f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                    f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                    f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                                    "reason_detail=do_not_let_oversize_train_hide_watch_followthrough"
                                )
                        train_ok = (
                            not allow_frozen_rearm
                            and bool(args.enable_oversize_train_lane)
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
                        elif not allow_frozen_rearm:
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
                                cand["verified_hot_train_ts_ms"] = now
                                cand["verified_hot_train_delay_ms"] = now - int(cand["start_ms"])
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
                        instant_dense_min_buys = int(
                            os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MIN_BUYS", "4")
                        )
                        instant_dense_max_buys = int(
                            os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_BUYS", "5")
                        )
                        instant_dense_min_lamports = int(
                            float(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MIN_SOL", "1.50"))
                            * LAMPORTS_PER_SOL
                        )
                        instant_dense_max_lamports = int(
                            float(os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_SOL", "4.50"))
                            * LAMPORTS_PER_SOL
                        )
                        instant_dense_max_delay_ms = int(
                            os.environ.get("V287_SELECTED_FRESH_INSTANT_DENSE_MAX_DELAY_MS", "75")
                        )
                        instant_dense_ok = (
                            float(cand.get("prev_buy_sol") or 0.0) <= 1e-12
                            and instant_dense_min_buys
                            <= int(cand.get("pre_entry_buys") or 0)
                            <= instant_dense_max_buys
                            and instant_dense_min_lamports
                            <= int(cand.get("pre_entry_buy_lamports") or 0)
                            <= instant_dense_max_lamports
                            and now - int(cand["start_ms"]) <= instant_dense_max_delay_ms
                        )
                        dense_train_min_buys = int(
                            os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MIN_BUYS", "4")
                        )
                        dense_train_max_buys = int(
                            os.environ.get("V287_SELECTED_FRESH_DENSE_TRAIN_MAX_BUYS", "5")
                        )
                        dense_train_min_lamports = int(
                            float(
                                os.environ.get(
                                    "V287_SELECTED_FRESH_DENSE_TRAIN_MIN_SOL",
                                    "2.30",
                                )
                            )
                            * LAMPORTS_PER_SOL
                        )
                        dense_train_max_lamports = int(
                            float(
                                os.environ.get(
                                    "V287_SELECTED_FRESH_DENSE_TRAIN_MAX_SOL",
                                    "3.60",
                                )
                            )
                            * LAMPORTS_PER_SOL
                        )
                        dense_train_max_delay_ms = int(
                            os.environ.get(
                                "V287_SELECTED_FRESH_DENSE_TRAIN_MAX_DELAY_MS",
                                "75",
                            )
                        )
                        dense_train_ok = (
                            float(cand.get("prev_buy_sol") or 0.0) <= 1e-12
                            and dense_train_min_buys
                            <= int(cand.get("pre_entry_buys") or 0)
                            <= dense_train_max_buys
                            and dense_train_min_lamports
                            <= int(cand.get("pre_entry_buy_lamports") or 0)
                            <= dense_train_max_lamports
                            and now - int(cand["start_ms"]) <= dense_train_max_delay_ms
                        )
                        if instant_dense_ok or dense_train_ok:
                            if instant_dense_ok:
                                counters["selected_instant_dense_train_pass"] += 1
                                cand["selected_fresh_instant_dense_train"] = 1
                                log_name = "PGG2-V287-SELECTED-INSTANT-DENSE-TRAIN-PASS "
                                source = "fast_lane_overcap_repair"
                            else:
                                counters["selected_dense_moderate_train_pass"] += 1
                                cand["selected_dense_moderate_train"] = 1
                                log_name = "PGG2-V287-SELECTED-DENSE-MODERATE-TRAIN-PASS "
                                source = "fast_lane_followthrough"
                            _log(
                                log_name +
                                f"mint={_short(mint)} full_mint={mint} "
                                f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                                f"delay_ms={now-int(cand['start_ms'])} "
                                f"max_rearm_buys={int(args.fresh_impulse_max_rearm_buys)} "
                                f"source={source}"
                            )
                        else:
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
                        rearm_pass_delay_ms = max(0, now - int(cand["start_ms"]))
                        cand["last_rearm_pass_delay_ms"] = rearm_pass_delay_ms
                        cand["last_rearm_pass_ts_ms"] = now
                        cand.setdefault(
                            "first_rearm_pass_delay_ms",
                            rearm_pass_delay_ms,
                        )
                        counters["rearm_pass"] += 1
                        if (
                            cand_rearm_max_lamports <= 0
                            or cand["pre_entry_buy_lamports"] <= cand_rearm_max_lamports
                        ):
                            cand["last_inband_rearm_lamports"] = int(
                                cand["pre_entry_buy_lamports"]
                            )
                            cand["last_inband_rearm_buys"] = int(
                                cand["pre_entry_buys"]
                            )
                            cand["last_inband_rearm_ts_ms"] = now
                            cand["last_inband_rearm_delay_ms"] = rearm_pass_delay_ms
                        _log(
                            "PGG2-V287-REARM-PASS "
                            f"mint={_short(mint)} pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f} "
                            f"rearm_min_sol={cand_rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                            f"rearm_max_sol={cand_rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                            f"top_lane={cand.get('top_lane', 'unknown')} "
                            f"delay_ms={rearm_pass_delay_ms}"
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
                                    selected_wait_ok = False
                                    selected_wait_reason = "selected_no_match"
                                    if (
                                        os.environ.get(
                                            "V287_SELECTED_WAIT_FOR_PLAN_ON_REARM",
                                            "1",
                                        )
                                        != "0"
                                    ):
                                        selected_wait_ok, selected_wait_reason = (
                                            _v287_selected_negative_roundtrip_fingerprint(
                                                top_lane=str(cand.get("top_lane") or ""),
                                                current_buy_sol=float(
                                                    cand.get("current_buy_sol") or 0.0
                                                ),
                                                prev_buy_sol=float(
                                                    cand.get("prev_buy_sol") or 0.0
                                                ),
                                                top_share=float(
                                                    cand.get("top_share") or 0.0
                                                ),
                                                pre_entry_buys=int(
                                                    cand.get("pre_entry_buys") or 0
                                                ),
                                                observed_rearm_sol=(
                                                    int(
                                                        cand.get(
                                                            "pre_entry_buy_lamports"
                                                        )
                                                        or 0
                                                    )
                                                    / LAMPORTS_PER_SOL
                                                ),
                                                first_rearm_delay_ms=int(
                                                    cand.get(
                                                        "first_rearm_pass_delay_ms"
                                                    )
                                                    or rearm_pass_delay_ms
                                                ),
                                                last_rearm_delay_ms=int(
                                                    cand.get(
                                                        "last_rearm_pass_delay_ms"
                                                    )
                                                    or rearm_pass_delay_ms
                                                ),
                                                last_rearm_lag_ms=max(
                                                    0,
                                                    now
                                                    - int(
                                                        cand.get(
                                                            "last_rearm_pass_ts_ms"
                                                        )
                                                        or now
                                                    ),
                                                ),
                                            )
                                        )
                                    if selected_wait_ok:
                                        wait_start_ms = _now_ms()
                                        selected_plan_wait_ms = int(
                                            os.environ.get(
                                                "V287_SELECTED_PLAN_READY_WAIT_MS", "320"
                                            )
                                        )
                                        if (
                                            selected_wait_reason
                                            == "selected_seed_prior_carry_rearm"
                                        ):
                                            selected_plan_wait_ms = int(
                                                os.environ.get(
                                                    "V287_SELECTED_SEED_PRIOR_PLAN_READY_WAIT_MS",
                                                    str(selected_plan_wait_ms),
                                                )
                                            )
                                        wait_deadline_ms = wait_start_ms + selected_plan_wait_ms
                                        wait_plan_err = plan_err
                                        while _now_ms() < wait_deadline_ms:
                                            (
                                                wait_plan_done,
                                                wait_plan_ok,
                                                wait_plan_err,
                                            ) = _static_plan_future_done_ok(
                                                cand.get("static_plan_future")
                                            )
                                            if wait_plan_ok:
                                                plan_done = True
                                                plan_ok_now = True
                                                plan_err = ""
                                                cand["selected_plan_ready_reason"] = (
                                                    selected_wait_reason
                                                )
                                                cand["selected_plan_ready_ts_ms"] = _now_ms()
                                                counters[
                                                    "selected_plan_ready_wait_pass"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SELECTED-PLAN-READY-WAIT-PASS "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={selected_wait_reason} "
                                                    f"wait_ms={_now_ms()-wait_start_ms} "
                                                    f"wait_limit_ms={selected_plan_wait_ms} "
                                                    f"delay_ms={now-int(cand['start_ms'])} "
                                                    f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                                    f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f}"
                                                )
                                                break
                                            if wait_plan_done:
                                                plan_done = True
                                                plan_err = wait_plan_err
                                                break
                                            time.sleep(
                                                max(
                                                    0.001,
                                                    float(
                                                        os.environ.get(
                                                            "V287_SELECTED_PLAN_READY_POLL_MS",
                                                            "10",
                                                        )
                                                    )
                                                    / 1000.0,
                                                )
                                            )
                                        if not plan_ok_now:
                                            counters[
                                                "selected_plan_ready_wait_timeout"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SELECTED-PLAN-READY-WAIT-TIMEOUT "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={selected_wait_reason} "
                                                f"wait_ms={_now_ms()-wait_start_ms} "
                                                f"wait_limit_ms={selected_plan_wait_ms} "
                                                f"plan_state={wait_plan_err or plan_err or 'pending'} "
                                                f"pre_entry_buys={int(cand['pre_entry_buys'])} "
                                                f"pre_entry_buy_sol={cand['pre_entry_buy_lamports']/LAMPORTS_PER_SOL:.6f}"
                                            )
                                if not plan_ok_now and cand.get("post_plan_rearm_required"):
                                    pending_base_lamports = int(
                                        cand.get("post_plan_rearm_base_lamports") or 0
                                    )
                                    pending_base_buys = int(
                                        cand.get("post_plan_rearm_base_buys") or 0
                                    )
                                    pending_post_plan_lamports = max(
                                        0,
                                        int(cand.get("pre_entry_buy_lamports") or 0)
                                        - pending_base_lamports,
                                    )
                                    pending_post_plan_buys = max(
                                        0,
                                        int(cand.get("pre_entry_buys") or 0)
                                        - pending_base_buys,
                                    )
                                    pending_reason = str(
                                        cand.get("selected_plan_ready_reason") or ""
                                    )
                                    selected_seed_prior_pending = (
                                        _v287_is_selected_seed_prior(cand, pending_reason)
                                        and pending_post_plan_buys >= 1
                                        and pending_post_plan_lamports
                                        >= int(
                                            float(
                                                os.environ.get(
                                                    "V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL",
                                                    "0.70",
                                                )
                                            )
                                            * LAMPORTS_PER_SOL
                                        )
                                    )
                                    if selected_seed_prior_pending:
                                        wait_start_ms = _now_ms()
                                        postplan_ready_wait_ms = int(
                                            os.environ.get(
                                                "V287_SELECTED_POSTPLAN_READY_WAIT_MS",
                                                "120",
                                            )
                                        )
                                        wait_deadline_ms = (
                                            wait_start_ms + postplan_ready_wait_ms
                                        )
                                        wait_plan_err = plan_err
                                        while _now_ms() < wait_deadline_ms:
                                            (
                                                wait_plan_done,
                                                wait_plan_ok,
                                                wait_plan_err,
                                            ) = _static_plan_future_done_ok(
                                                cand.get("static_plan_future")
                                            )
                                            if wait_plan_ok:
                                                plan_done = True
                                                plan_ok_now = True
                                                plan_err = ""
                                                counters[
                                                    "selected_postplan_ready_wait_pass"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SELECTED-POSTPLAN-READY-WAIT-PASS "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={pending_reason} "
                                                    f"wait_ms={_now_ms()-wait_start_ms} "
                                                    f"wait_limit_ms={postplan_ready_wait_ms} "
                                                    f"post_plan_buys={pending_post_plan_buys} "
                                                    f"post_plan_buy_sol={pending_post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                                    "source=post_plan_followthrough_static_plan_bridge"
                                                )
                                                break
                                            if wait_plan_done:
                                                plan_done = True
                                                plan_err = wait_plan_err
                                                break
                                            time.sleep(
                                                max(
                                                    0.001,
                                                    float(
                                                        os.environ.get(
                                                            "V287_SELECTED_PLAN_READY_POLL_MS",
                                                            "10",
                                                        )
                                                    )
                                                    / 1000.0,
                                                )
                                            )
                                        if not plan_ok_now:
                                            counters[
                                                "selected_postplan_ready_wait_timeout"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SELECTED-POSTPLAN-READY-WAIT-TIMEOUT "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={pending_reason} "
                                                f"wait_ms={_now_ms()-wait_start_ms} "
                                                f"wait_limit_ms={postplan_ready_wait_ms} "
                                                f"plan_state={wait_plan_err or plan_err or 'pending'} "
                                                f"post_plan_buys={pending_post_plan_buys} "
                                                f"post_plan_buy_sol={pending_post_plan_lamports/LAMPORTS_PER_SOL:.6f}"
                                            )
                                if not plan_ok_now:
                                    if plan_done:
                                        counters["post_plan_rearm_plan_fail"] += 1
                                        _log(
                                            "PGG2-V287-POST-PLAN-REARM-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"plan_ready=0 plan_state={plan_err or 'done_false'} "
                                            "reason=static_plan_failed"
                                        )
                                        active.pop(mint, None)
                                        continue
                                    first_post_plan_wait = (
                                        int(cand.get("post_plan_rearm_required") or 0)
                                        != 1
                                    )
                                    cand["post_plan_rearm_required"] = 1
                                    cand.setdefault("post_plan_rearm_wait_start_ms", now)
                                    # Preserve the first pending-plan baseline. Replacing
                                    # it on every rearm update turns real follow-through
                                    # buys into "pre-entry" flow and leaves
                                    # post_plan_buy_sol pinned at zero.
                                    post_plan_base_lamports_for_wait = int(
                                        cand.get("pre_entry_buy_lamports") or 0
                                    )
                                    post_plan_base_buys_for_wait = int(
                                        cand.get("pre_entry_buys") or 0
                                    )
                                    post_plan_base_source = "current_pre_entry"
                                    watch_baseline_reason = str(
                                        cand.get("no_movement_watch_reason")
                                        or cand.get("selected_plan_ready_reason")
                                        or ""
                                    )
                                    if (
                                        first_post_plan_wait
                                        and _v287_is_selected_seed_prior(
                                            cand, watch_baseline_reason
                                        )
                                        and int(cand.get("no_movement_watch_keeps") or 0)
                                        > 0
                                    ):
                                        watch_base_lamports = int(
                                            cand.get(
                                                "no_movement_watch_start_pre_entry_lamports"
                                            )
                                            or 0
                                        )
                                        watch_base_buys = int(
                                            cand.get("no_movement_watch_start_buys") or 0
                                        )
                                        if (
                                            watch_base_lamports > 0
                                            and watch_base_lamports
                                            <= post_plan_base_lamports_for_wait
                                        ):
                                            post_plan_base_lamports_for_wait = (
                                                watch_base_lamports
                                            )
                                            post_plan_base_buys_for_wait = watch_base_buys
                                            post_plan_base_source = (
                                                "no_movement_watch_start"
                                            )
                                    cand.setdefault(
                                        "post_plan_rearm_base_lamports",
                                        post_plan_base_lamports_for_wait,
                                    )
                                    cand.setdefault(
                                        "post_plan_rearm_base_buys",
                                        post_plan_base_buys_for_wait,
                                    )
                                    pending_reason = str(
                                        cand.get("selected_plan_ready_reason")
                                        or cand.get("no_movement_watch_reason")
                                        or "selected_seed_prior_carry_rearm"
                                    )
                                    if (
                                        first_post_plan_wait
                                        and str(cand.get("top_lane") or "")
                                        == "seed_prior_carry_continuation"
                                    ):
                                        cand.setdefault(
                                            "pending_plan_rearm_lamports",
                                            post_plan_base_lamports_for_wait,
                                        )
                                        cand.setdefault(
                                            "pending_plan_rearm_buys",
                                            post_plan_base_buys_for_wait,
                                        )
                                        cand.setdefault(
                                            "pending_plan_rearm_delay_ms",
                                            max(0, now - int(cand.get("start_ms") or now)),
                                        )
                                        cand.setdefault(
                                            "pending_plan_rearm_reason",
                                            pending_reason,
                                        )
                                    if (
                                        first_post_plan_wait
                                        and post_plan_base_source
                                        == "no_movement_watch_start"
                                    ):
                                        counters[
                                            "seed_prior_watch_baseline_restore"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-WATCH-BASELINE-RESTORE "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={watch_baseline_reason} "
                                            f"watch_base_buys={post_plan_base_buys_for_wait} "
                                            f"watch_base_sol={post_plan_base_lamports_for_wait/LAMPORTS_PER_SOL:.6f} "
                                            f"current_pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"current_pre_entry_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                            "source=post_plan_rearm_wait"
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
                                        f"post_plan_base_buys={int(cand.get('post_plan_rearm_base_buys') or 0)} "
                                        f"post_plan_base_sol={int(cand.get('post_plan_rearm_base_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                        f"post_plan_buy_sol={max(0, int(cand.get('pre_entry_buy_lamports') or 0) - int(cand.get('post_plan_rearm_base_lamports') or 0))/LAMPORTS_PER_SOL:.6f} "
                                        f"first_wait={int(first_post_plan_wait)} "
                                        f"delay_ms={now-int(cand['start_ms'])} "
                                        f"ttl_ms={_candidate_live_ttl_ms(cand)} "
                                        "reason=require_fresh_buy_after_static_plan_ready"
                                    )
                                    hist[mint].append(rec)
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
                                post_plan_followthrough_min_lamports = int(
                                    float(
                                        os.environ.get(
                                            "V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL",
                                            "0.70",
                                        )
                                    )
                                    * LAMPORTS_PER_SOL
                                )
                                selected_postplan_min_lamports = int(
                                    float(
                                        os.environ.get(
                                            "V287_SELECTED_SEED_PRIOR_POSTPLAN_REARM_MIN_SOL",
                                            os.environ.get(
                                                "V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL",
                                                "0.70",
                                            ),
                                        )
                                    )
                                    * LAMPORTS_PER_SOL
                                )
                                if (
                                    _v287_is_selected_seed_prior(
                                        cand,
                                        str(
                                            cand.get("selected_plan_ready_reason")
                                            or cand.get("no_movement_watch_reason")
                                            or ""
                                        ),
                                    )
                                    and selected_postplan_min_lamports > 0
                                ):
                                    post_plan_min_lamports = min(
                                        post_plan_min_lamports,
                                        selected_postplan_min_lamports,
                                    )
                                if (
                                    post_plan_buys >= 1
                                    and post_plan_lamports
                                    >= post_plan_followthrough_min_lamports
                                ):
                                    cand["post_plan_followthrough_buys"] = post_plan_buys
                                    cand["post_plan_followthrough_lamports"] = (
                                        post_plan_lamports
                                    )
                                    cand["post_plan_followthrough_ts_ms"] = now
                                if post_plan_buys < 1 or post_plan_lamports < post_plan_min_lamports:
                                    credit_wait_ms = now - int(
                                        cand.get("post_plan_rearm_wait_start_ms") or now
                                    )
                                    preplan_selected_ok, preplan_selected_reason = (
                                        _v287_selected_negative_roundtrip_fingerprint(
                                            top_lane=str(cand.get("top_lane") or ""),
                                            current_buy_sol=float(
                                                cand.get("current_buy_sol") or 0.0
                                            ),
                                            prev_buy_sol=float(
                                                cand.get("prev_buy_sol") or 0.0
                                            ),
                                            top_share=float(cand.get("top_share") or 0.0),
                                            pre_entry_buys=int(
                                                cand.get("pre_entry_buys") or 0
                                            ),
                                            observed_rearm_sol=(
                                                int(
                                                    cand.get(
                                                        "pre_entry_buy_lamports"
                                                    )
                                                    or 0
                                                )
                                                / LAMPORTS_PER_SOL
                                            ),
                                            first_rearm_delay_ms=int(
                                                cand.get("first_rearm_pass_delay_ms")
                                                or max(
                                                    0,
                                                    now
                                                    - int(
                                                        cand.get("start_ms") or now
                                                    ),
                                                )
                                            ),
                                            last_rearm_delay_ms=int(
                                                cand.get("last_rearm_pass_delay_ms")
                                                or max(
                                                    0,
                                                    now
                                                    - int(
                                                        cand.get("start_ms") or now
                                                    ),
                                                )
                                            ),
                                            last_rearm_lag_ms=max(
                                                0,
                                                now
                                                - int(
                                                    cand.get(
                                                        "last_rearm_pass_ts_ms"
                                                    )
                                                    or now
                                                ),
                                            ),
                                        )
                                    )
                                    base_preplan_credit = (
                                        os.environ.get("V287_ALLOW_PREPLAN_REARM_CREDIT", "1") != "0"
                                        and int(cand.get("pre_entry_buys") or 0) >= 1
                                        and int(cand.get("pre_entry_buy_lamports") or 0)
                                        >= cand_rearm_min_lamports
                                        and credit_wait_ms
                                        <= int(os.environ.get("V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS", "5"))
                                        and (
                                            str(cand.get("top_lane", "")) == "fresh_impulse"
                                            or preplan_selected_ok
                                        )
                                    )
                                    (
                                        pending_plan_credit_ok,
                                        pending_plan_credit_reason,
                                    ) = _v287_selected_seed_prior_pending_plan_credit_ok(
                                        cand,
                                        credit_wait_ms=credit_wait_ms,
                                        post_plan_buys=post_plan_buys,
                                        post_plan_lamports=post_plan_lamports,
                                    )
                                    allow_preplan_credit = (
                                        base_preplan_credit
                                        or pending_plan_credit_ok
                                    )
                                    if allow_preplan_credit:
                                        if pending_plan_credit_ok:
                                            counters[
                                                "seed_prior_pending_plan_credit_pass"
                                            ] += 1
                                            cand[
                                                "seed_prior_pending_plan_credit_pass"
                                            ] = 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-PENDING-PLAN-CREDIT-PASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"shape={pending_plan_credit_reason} "
                                                f"pending_buys={int(cand.get('pending_plan_rearm_buys') or 0)} "
                                                f"pending_sol={int(cand.get('pending_plan_rearm_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                f"pending_delay_ms={int(cand.get('pending_plan_rearm_delay_ms') or 999999)} "
                                                f"post_plan_buys={post_plan_buys} "
                                                f"post_plan_buy_sol={post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                                f"credit_wait_ms={credit_wait_ms}"
                                            )
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
                                            f"credit_max_wait_ms={int(os.environ.get('V287_PREPLAN_REARM_CREDIT_MAX_WAIT_MS', '5'))} "
                                            f"selected_reason={preplan_selected_reason} "
                                            f"pending_plan_credit={int(pending_plan_credit_ok)} "
                                            f"pending_plan_reason={pending_plan_credit_reason} "
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
                                if (
                                    _v287_is_selected_seed_prior(
                                        cand,
                                        str(
                                            cand.get("selected_plan_ready_reason")
                                            or cand.get("no_movement_watch_reason")
                                            or ""
                                        ),
                                    )
                                    and int(
                                        cand.get("seed_prior_positive_refresh_watch") or 0
                                    )
                                    == 1
                                ):
                                    positive_watch_start_ms = int(
                                        cand.get(
                                            "seed_prior_positive_refresh_watch_start_ms"
                                        )
                                        or 0
                                    )
                                    positive_watch_age_ms = max(
                                        0,
                                        now - positive_watch_start_ms,
                                    )
                                    positive_watch_ms = int(
                                        os.environ.get(
                                            "V287_SELECTED_SEED_PRIOR_POS_REFRESH_WATCH_MS",
                                            "650",
                                        )
                                    )
                                    if (
                                        positive_watch_start_ms > 0
                                        and positive_watch_age_ms <= positive_watch_ms
                                    ):
                                        cand[
                                            "seed_prior_watch_followthrough_send_ok"
                                        ] = 1
                                        cand[
                                            "seed_prior_watch_followthrough_lamports"
                                        ] = int(post_plan_lamports)
                                        cand["seed_prior_watch_followthrough_buys"] = int(
                                            post_plan_buys
                                        )
                                        cand[
                                            "seed_prior_watch_followthrough_ts_ms"
                                        ] = now
                                        counters[
                                            "seed_prior_positive_refresh_followthrough_pass"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-POS-REFRESH-FOLLOWTHROUGH-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={cand.get('selected_plan_ready_reason') or cand.get('no_movement_watch_reason') or ''} "
                                            f"delta_buy_sol={post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                            f"delta_buys={post_plan_buys} "
                                            f"watch_age_ms={positive_watch_age_ms} "
                                            f"watch_ms={positive_watch_ms} "
                                            "reason_detail=fresh_buy_after_positive_refresh_boundary"
                                        )
                                    else:
                                        counters[
                                            "seed_prior_positive_refresh_followthrough_stale"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-POS-REFRESH-FOLLOWTHROUGH-STALE "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"delta_buy_sol={post_plan_lamports/LAMPORTS_PER_SOL:.6f} "
                                            f"delta_buys={post_plan_buys} "
                                            f"watch_age_ms={positive_watch_age_ms} "
                                            f"watch_ms={positive_watch_ms}"
                                        )
                                cand["post_plan_rearm_passed"] = 1
                                cand["post_plan_rearm_pass_ts_ms"] = now
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
                                curve_source_ts_ms = 0
                                try:
                                    curve = broker.bonding_curve(as_pubkey(mint))
                                except Exception as exc:
                                    (
                                        cached_curve,
                                        cached_ts_ms,
                                        cached_slot,
                                        cached_age_ms,
                                        cached_status,
                                    ) = _v287_curve_from_geyser_cache(
                                        curve_cache_by_key,
                                        mint,
                                        max_age_ms=int(
                                            os.environ.get(
                                                "V287_GEYSER_CURVE_CACHE_MAX_AGE_MS",
                                                "250",
                                            )
                                        ),
                                    )
                                    if cached_curve is None:
                                        counters["final_curve_missing_block"] += 1
                                        _log(
                                            "PGG2-V287-FINAL-CURVE-MISSING-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"err={type(exc).__name__}:{str(exc)[:160]} "
                                            f"cache_status={cached_status} "
                                            "reason=curve_unavailable_before_prebuy_check"
                                        )
                                        active.pop(mint, None)
                                        hist[mint].append(rec)
                                        continue
                                    curve = cached_curve
                                    counters["final_curve_geyser_cache_fallback"] += 1
                                    _log(
                                        "PGG2-V287-FINAL-CURVE-GEYSER-CACHE-FALLBACK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"cache_age_ms={cached_age_ms} slot={cached_slot} "
                                        f"cache_ts_ms={cached_ts_ms} "
                                        f"broker_err={type(exc).__name__}:{str(exc)[:120]}"
                                    )
                                    curve_source_ts_ms = int(cached_ts_ms)
                                curve_ts_ms = int(curve_source_ts_ms or _now_ms())
                                curve_ms = _now_ms() - fast_start_ms
                                continuation_model_ok = False
                                continuation_reason = ""
                                fp_now_ms = _now_ms()
                                fp_age_ms = fp_now_ms - int(
                                    cand.get("start_ms") or fp_now_ms
                                )
                                fp_first_rearm_delay_ms = int(
                                    cand.get("first_rearm_pass_delay_ms") or fp_age_ms
                                )
                                fp_last_rearm_delay_ms = int(
                                    cand.get("last_rearm_pass_delay_ms")
                                    or fp_first_rearm_delay_ms
                                )
                                fp_last_rearm_lag_ms = max(
                                    0,
                                    fp_now_ms
                                    - int(cand.get("last_rearm_pass_ts_ms") or fp_now_ms),
                                )
                                cand["last_rearm_lag_ms"] = fp_last_rearm_lag_ms
                                pre_projection_selected_ok, pre_projection_selected_reason = (
                                    _v287_selected_negative_roundtrip_fingerprint(
                                        top_lane=str(cand.get("top_lane") or ""),
                                        current_buy_sol=float(cand.get("current_buy_sol") or 0.0),
                                        prev_buy_sol=float(cand.get("prev_buy_sol") or 0.0),
                                        top_share=float(cand.get("top_share") or 0.0),
                                        pre_entry_buys=int(cand.get("pre_entry_buys") or 0),
                                        observed_rearm_sol=(
                                            int(cand.get("pre_entry_buy_lamports") or 0)
                                            / LAMPORTS_PER_SOL
                                        ),
                                        first_rearm_delay_ms=fp_first_rearm_delay_ms,
                                        last_rearm_delay_ms=fp_last_rearm_delay_ms,
                                        last_rearm_lag_ms=fp_last_rearm_lag_ms,
                                    )
                                )
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
                                    last_rearm_delay_ms = int(
                                        cand.get("last_rearm_pass_delay_ms")
                                        or first_rearm_delay_ms
                                    )
                                    last_rearm_lag_ms = max(
                                        0,
                                        _now_ms()
                                        - int(cand.get("last_rearm_pass_ts_ms") or _now_ms()),
                                    )
                                    cand["last_rearm_lag_ms"] = last_rearm_lag_ms
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
                                        and last_rearm_delay_ms <= 350
                                        and last_rearm_lag_ms
                                        <= int(
                                            os.environ.get(
                                                "V287_VERIFIED_HOT_TRAIN_MAX_SEND_LAG_MS",
                                                "650",
                                            )
                                        )
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
                                    hot_train_observed = bool(cand.get("verified_hot_train"))
                                    hot_train_observed_age_ms = int(
                                        cand.get("verified_hot_train_delay_ms") or age_ms
                                    )
                                    hot_train_send_lag_ms = max(
                                        0,
                                        _now_ms()
                                        - int(cand.get("verified_hot_train_ts_ms") or _now_ms()),
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
                                        and (
                                            last_rearm_delay_ms
                                            <= int(
                                                os.environ.get(
                                                    "V287_VERIFIED_HOT_TRAIN_MAX_AGE_MS",
                                                    "350",
                                                )
                                            )
                                            or (
                                                hot_train_observed
                                                and hot_train_observed_age_ms
                                                <= int(
                                                    os.environ.get(
                                                        "V287_VERIFIED_HOT_TRAIN_MAX_AGE_MS",
                                                        "350",
                                                    )
                                                )
                                                and hot_train_send_lag_ms
                                                <= int(
                                                    os.environ.get(
                                                        "V287_VERIFIED_HOT_TRAIN_MAX_SEND_LAG_MS",
                                                        "650",
                                                    )
                                                )
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
                                    selected_negative_ok, selected_negative_reason = (
                                        _v287_selected_negative_roundtrip_fingerprint(
                                            top_lane=top_lane,
                                            current_buy_sol=current_buy_sol,
                                            prev_buy_sol=prev_buy_sol,
                                            top_share=float(cand.get("top_share") or 0.0),
                                            pre_entry_buys=pre_entry_buys,
                                            observed_rearm_sol=observed_rearm_sol,
                                            first_rearm_delay_ms=first_rearm_delay_ms,
                                            last_rearm_delay_ms=last_rearm_delay_ms,
                                            last_rearm_lag_ms=last_rearm_lag_ms,
                                        )
                                    )
                                    if not selected_negative_ok:
                                        (
                                            restore_ok,
                                            restore_reason,
                                        ) = _v287_seed_prior_postplan_reason_restore_ok(
                                            cand
                                        )
                                        if restore_ok:
                                            selected_negative_ok = True
                                            selected_negative_reason = str(
                                                cand.get("selected_plan_ready_reason")
                                                or "selected_seed_prior_carry_rearm"
                                            )
                                            counters[
                                                "selected_seed_prior_postplan_reason_restore"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-POSTPLAN-REASON-RESTORE "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={selected_negative_reason} "
                                                f"restore_reason={restore_reason} "
                                                f"current_buy_sol={current_buy_sol:.6f} "
                                                f"pre_entry_buys={pre_entry_buys} "
                                                f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                                f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                                f"last_rearm_lag_ms={last_rearm_lag_ms} "
                                                "source=restore_plan_ready_reason_before_projection_block"
                                            )
                                    no_move_followthrough_ok = (
                                        os.environ.get(
                                            "V287_SELECTED_NO_MOVEMENT_FOLLOWTHROUGH",
                                            "1",
                                        )
                                        != "0"
                                        and not selected_negative_ok
                                        and int(cand.get("no_movement_watch_keeps") or 0) > 0
                                        and top_lane == "single_prior_buy_continuation"
                                        and 2.00 <= current_buy_sol <= 3.25
                                        and 1.80 <= prev_buy_sol <= 5.60
                                        and float(cand.get("top_share") or 0.0) >= 0.80
                                        and 1
                                        <= pre_entry_buys
                                        <= int(
                                            os.environ.get(
                                                "V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MAX_BUYS",
                                                "6",
                                            )
                                        )
                                        and float(
                                            os.environ.get(
                                                "V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MIN_SOL",
                                                "4.50",
                                            )
                                        )
                                        <= observed_rearm_sol
                                        <= float(
                                            os.environ.get(
                                                "V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MAX_SOL",
                                                "10.00",
                                            )
                                        )
                                        and first_rearm_delay_ms
                                        <= int(
                                            os.environ.get(
                                                "V287_SELECTED_SINGLE_PRIOR_MAX_DELAY_MS",
                                                "350",
                                            )
                                        )
                                        and last_rearm_delay_ms
                                        <= int(
                                            os.environ.get(
                                                "V287_SELECTED_SINGLE_PRIOR_NO_MOVE_MAX_DELAY_MS",
                                                "1200",
                                            )
                                        )
                                        and last_rearm_lag_ms
                                        <= int(
                                            os.environ.get(
                                                "V287_VERIFIED_HOT_TRAIN_MAX_SEND_LAG_MS",
                                                "650",
                                            )
                                        )
                                    )
                                    if no_move_followthrough_ok:
                                        selected_negative_ok = True
                                        selected_negative_reason = (
                                            "selected_single_prior_no_movement_followthrough"
                                        )
                                        counters[
                                            "selected_no_movement_followthrough_pass"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SELECTED-NO-MOVEMENT-FOLLOWTHROUGH-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={selected_negative_reason} "
                                            f"current_buy_sol={current_buy_sol:.6f} "
                                            f"prev_buy_sol={prev_buy_sol:.6f} "
                                            f"pre_entry_buys={pre_entry_buys} "
                                            f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                            f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                            f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                            f"last_rearm_lag_ms={last_rearm_lag_ms} "
                                            "source=repair_no_movement_frequency_leak"
                                        )
                                    if (
                                        _v287_seed_prior_only_live_mode()
                                        and selected_negative_reason
                                        == "selected_seed_prior_single_strong_rearm"
                                    ):
                                        (
                                            postplan_bridge_ok,
                                            postplan_bridge_reason,
                                        ) = _v287_seed_prior_single_strong_postplan_bridge_ok(
                                            cand,
                                            selected_negative_reason,
                                        )
                                        if postplan_bridge_ok:
                                            cand[
                                                "seed_prior_single_strong_postplan_bridge_ok"
                                            ] = 1
                                            counters[
                                                "seed_prior_single_strong_postplan_bridge_pass"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-SINGLE-STRONG-POSTPLAN-BRIDGE-PASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={selected_negative_reason} "
                                                f"current_buy_sol={current_buy_sol:.6f} "
                                                f"pre_entry_buys={pre_entry_buys} "
                                                f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                                f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                                f"last_rearm_lag_ms={last_rearm_lag_ms} "
                                                f"bridge_reason={postplan_bridge_reason} "
                                                "source=single_strong_postplan_state_bridge"
                                            )
                                        else:
                                            cand[
                                                "seed_prior_single_strong_postplan_bridge_blocker"
                                            ] = postplan_bridge_reason
                                            now_wait_for_carry_ms = _now_ms()
                                            cand["selected_plan_ready_reason"] = (
                                                selected_negative_reason
                                            )
                                            cand.setdefault(
                                                "selected_plan_ready_ts_ms",
                                                now_wait_for_carry_ms,
                                            )
                                            cand["post_plan_rearm_required"] = 1
                                            cand.setdefault(
                                                "post_plan_rearm_wait_start_ms",
                                                now_wait_for_carry_ms,
                                            )
                                            cand["post_plan_rearm_wait_last_ms"] = (
                                                now_wait_for_carry_ms
                                            )
                                            cand.setdefault(
                                                "post_plan_rearm_base_lamports",
                                                int(cand.get("pre_entry_buy_lamports") or 0),
                                            )
                                            cand.setdefault(
                                                "post_plan_rearm_base_buys",
                                                int(cand.get("pre_entry_buys") or 0),
                                            )
                                            cand[
                                                "seed_prior_single_strong_wait_for_carry"
                                            ] = 1
                                            counters["seed_prior_only_wait_for_carry"] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-ONLY-WAIT-FOR-CARRY "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={selected_negative_reason} "
                                                f"top_lane={top_lane} "
                                                f"current_buy_sol={current_buy_sol:.6f} "
                                                f"pre_entry_buys={pre_entry_buys} "
                                                f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                                f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                                f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                                f"post_plan_base_buys={int(cand.get('post_plan_rearm_base_buys') or 0)} "
                                                f"post_plan_base_sol={int(cand.get('post_plan_rearm_base_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                f"bridge_blocker={postplan_bridge_reason} "
                                                "needed_reason=fresh_postplan_carry "
                                                "reason_detail=preserve_single_strong_selected_reason_for_postplan_bridge"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                    if not continuation_ok and selected_negative_ok:
                                        continuation_ok = True
                                        continuation_model_ok = True
                                        continuation_reason = selected_negative_reason
                                        cand["selected_reason"] = selected_negative_reason
                                        cand["selected_top_lane"] = top_lane
                                        if (
                                            selected_negative_reason
                                            == "selected_seed_prior_carry_rearm"
                                            and top_lane
                                            == "seed_prior_carry_continuation"
                                        ):
                                            cand[
                                                "selected_seed_prior_carry_rearm_selected"
                                            ] = 1
                                            cand[
                                                "selected_seed_prior_carry_rearm_ts_ms"
                                            ] = _now_ms()
                                        counters["selected_negative_roundtrip_fingerprint_pass"] += 1
                                        _log(
                                            "PGG2-V287-SELECTED-NEGATIVE-ROUNDTRIP-FINGERPRINT-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={selected_negative_reason} "
                                            f"top_lane={top_lane} current_buy_sol={current_buy_sol:.6f} "
                                            f"prev_buy_sol={prev_buy_sol:.6f} "
                                            f"top_share={float(cand.get('top_share') or 0.0):.4f} "
                                            f"pre_entry_buys={pre_entry_buys} "
                                            f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                            f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                            f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                            f"last_rearm_lag_ms={last_rearm_lag_ms}"
                                        )
                                    if not continuation_ok:
                                        (
                                            plan_restore_ok,
                                            plan_restore_reason,
                                        ) = _v287_seed_prior_plan_ready_reason_restore_ok(
                                            cand,
                                            _now_ms(),
                                        )
                                        if plan_restore_ok:
                                            continuation_ok = True
                                            continuation_model_ok = True
                                            continuation_reason = plan_restore_reason
                                            counters[
                                                "selected_seed_prior_plan_ready_reason_restore"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-PLAN-READY-REASON-RESTORE "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"top_lane={top_lane} "
                                                f"current_buy_sol={current_buy_sol:.6f} "
                                                f"pre_entry_buys={pre_entry_buys} "
                                                f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                                f"selected_ready_age_ms={max(0, _now_ms()-int(cand.get('selected_plan_ready_ts_ms') or _now_ms()))} "
                                                "source=preserve_selected_decision_before_projection_block"
                                            )
                                    if not continuation_ok:
                                        (
                                            watch_state,
                                            watch_reason,
                                            watch_delta_lamports,
                                            watch_delta_buys,
                                        ) = _v287_seed_prior_watch_followthrough_state(
                                            cand,
                                            _now_ms(),
                                        )
                                        if watch_state == "followthrough":
                                            continuation_ok = True
                                            continuation_model_ok = True
                                            continuation_reason = watch_reason
                                            cand["seed_prior_watch_followthrough_send_ok"] = 1
                                            cand["seed_prior_watch_followthrough_lamports"] = int(
                                                watch_delta_lamports
                                            )
                                            cand["seed_prior_watch_followthrough_buys"] = int(
                                                watch_delta_buys
                                            )
                                            cand["seed_prior_watch_followthrough_ts_ms"] = _now_ms()
                                            counters[
                                                "selected_seed_prior_watch_followthrough_restore"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-WATCH-FOLLOWTHROUGH-RESTORE "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"delta_buy_sol={watch_delta_lamports/LAMPORTS_PER_SOL:.6f} "
                                                f"delta_buys={int(watch_delta_buys)} "
                                                f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                                "source=no_movement_watch_real_followthrough"
                                            )
                                        elif watch_state == "keep":
                                            counters[
                                                "selected_seed_prior_watch_projection_keep"
                                            ] += 1
                                            cand["no_movement_watch_deadline_ms"] = max(
                                                int(cand.get("no_movement_watch_deadline_ms") or 0),
                                                _now_ms()
                                                + int(
                                                    os.environ.get(
                                                        "V287_SELECTED_NO_MOVEMENT_WATCH_AFTER_REFRESH_MS",
                                                        "650",
                                                    )
                                                ),
                                            )
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-WATCH-PROJECTION-KEEP "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={watch_reason} "
                                                f"delta_buy_sol={watch_delta_lamports/LAMPORTS_PER_SOL:.6f} "
                                                f"delta_buys={int(watch_delta_buys)} "
                                                "reason_detail=negative_self_roundtrip_does_not_delete_active_watch"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                    if not continuation_ok:
                                        keep_unverified_fresh_watch = (
                                            os.environ.get(
                                                "V287_KEEP_UNVERIFIED_FRESH_WATCH",
                                                "1",
                                            )
                                            != "0"
                                            and top_lane == "fresh_impulse"
                                            and last_rearm_delay_ms
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
                                        if _v287_seed_prior_projection_bypass_ok(
                                            cand, continuation_reason, quote_tokens
                                        ):
                                            counters["seed_prior_projection_bypass"] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-PROJECTION-BYPASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                "source=fast_final_curve"
                                            )
                                        else:
                                            counters["prebuy_postbuy_sell_block"] += 1
                                            _log(
                                                "PGG2-V287-PREBUY-POSTBUY-SELL-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} source=fast_final_curve"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    selected_negative_reason_allowed = (
                                        _v287_selected_negative_reason_allowed(continuation_reason or "")
                                    )
                                    fresh_clean_ready, fresh_clean_reason = (
                                        _v287_fresh_clean_carry_reclass_ready(cand, _now_ms())
                                    )
                                    if (
                                        not selected_negative_reason_allowed
                                        and fresh_clean_ready
                                    ):
                                        continuation_reason = "selected_fresh_clean_carry_reclass"
                                        selected_negative_reason_allowed = True
                                        cand["fresh_clean_carry_reclass"] = 1
                                        counters["fresh_clean_carry_reclass_pass"] += 1
                                        _log(
                                            "PGG2-V287-FRESH-CLEAN-CARRY-RECLASS-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                            f"age_ms={_now_ms()-int(cand.get('start_ms') or _now_ms())} "
                                            f"reason={fresh_clean_reason} source=negative_roundtrip_authority"
                                        )
                                    if (
                                        os.environ.get(
                                            "V287_ALLOW_NEGATIVE_SELF_ROUNDTRIP_CONTINUATION",
                                            "0",
                                        )
                                        != "1"
                                        and not selected_negative_reason_allowed
                                    ):
                                        counters[
                                            "negative_self_roundtrip_continuation_block"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-NEGATIVE-SELF-ROUNDTRIP-CONTINUATION-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason or 'verified_flow'} "
                                            f"current_buy_sol={current_buy_sol:.6f} "
                                            f"prev_buy_sol={prev_buy_sol:.6f} "
                                            f"pre_entry_buys={pre_entry_buys} "
                                            f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                            f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                            f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                            f"last_rearm_lag_ms={last_rearm_lag_ms} "
                                            f"quote_tokens={quote_tokens:.6f} "
                                            "source=final_projection_negative"
                                        )
                                        active.pop(mint, None)
                                        continue
                                    if selected_negative_reason_allowed:
                                        counters["selected_negative_roundtrip_send_allow"] += 1
                                        _log(
                                            "PGG2-V287-SELECTED-NEGATIVE-ROUNDTRIP-SEND-ALLOW "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"current_buy_sol={current_buy_sol:.6f} "
                                            f"prev_buy_sol={prev_buy_sol:.6f} "
                                            f"pre_entry_buys={pre_entry_buys} "
                                            f"pre_entry_buy_sol={observed_rearm_sol:.6f} "
                                            f"first_rearm_delay_ms={first_rearm_delay_ms} "
                                            f"last_rearm_delay_ms={last_rearm_delay_ms} "
                                            f"last_rearm_lag_ms={last_rearm_lag_ms} "
                                            "source=final_projection_negative"
                                        )
                                if not continuation_reason and pre_projection_selected_ok:
                                    continuation_reason = pre_projection_selected_reason
                                    continuation_model_ok = True
                                    counters[
                                        "selected_reason_restored_for_final_refresh"
                                    ] += 1
                                    _log(
                                        "PGG2-V287-SELECTED-REASON-RESTORED-FOR-FINAL-REFRESH "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason} "
                                        f"top_lane={str(cand.get('top_lane') or '')} "
                                        f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                        f"prev_buy_sol={float(cand.get('prev_buy_sol') or 0.0):.6f} "
                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                        f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                        f"first_rearm_delay_ms={fp_first_rearm_delay_ms} "
                                        f"last_rearm_delay_ms={fp_last_rearm_delay_ms} "
                                        f"last_rearm_lag_ms={fp_last_rearm_lag_ms} "
                                        "source=positive_or_floor_passing_projection"
                                    )
                                if (
                                    str(cand.get("top_lane") or "")
                                    == "high_current_clean_train"
                                    and os.environ.get(
                                        "V287_ALLOW_HIGH_CURRENT_CLEAN_TRAIN_LIVE_RISK",
                                        "0",
                                    )
                                    != "1"
                                ):
                                    counters["high_current_train_actual_block"] += 1
                                    _log(
                                        "PGG2-V287-HIGH-CURRENT-TRAIN-ACTUAL-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason or pre_projection_selected_reason} "
                                        "source=7TSY_live_loss_replay"
                                    )
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                seed_prior_only_reason = (
                                    continuation_reason
                                    or pre_projection_selected_reason
                                    or ""
                                )
                                if not _v287_seed_prior_only_send_allowed(
                                    mint=mint,
                                    cand=cand,
                                    reason=seed_prior_only_reason,
                                    counters=counters,
                                    source="final_refresh_authority",
                                ):
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                base_min_quote_tokens = float(args.min_buy_quote_tokens)
                                if (
                                    os.environ.get(
                                        "V287_SELECTED_FRESH_ACTUAL_ENABLED",
                                        "0",
                                    )
                                    != "1"
                                    and not _v287_selected_fresh_actual_enabled(
                                        continuation_reason
                                        or pre_projection_selected_reason
                                    )
                                    and str(cand.get("top_lane") or "")
                                    == "fresh_impulse"
                                    and float(cand.get("prev_buy_sol") or 0.0)
                                    <= 1e-12
                                    and not bool(cand.get("fresh_clean_carry_reclass"))
                                ):
                                    fresh_clean_ready, fresh_clean_reason = (
                                        _v287_fresh_clean_carry_reclass_ready(cand, _now_ms())
                                    )
                                    if fresh_clean_ready:
                                        continuation_reason = "selected_fresh_clean_carry_reclass"
                                        cand["fresh_clean_carry_reclass"] = 1
                                        counters["fresh_clean_carry_reclass_pass"] += 1
                                        _log(
                                            "PGG2-V287-FRESH-CLEAN-CARRY-RECLASS-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                            f"age_ms={_now_ms()-int(cand.get('start_ms') or _now_ms())} "
                                            f"reason={fresh_clean_reason} source=fresh_shadow_authority"
                                        )
                                    elif _v287_fresh_actual_should_wait(cand, _now_ms()):
                                        counters["fresh_clean_carry_watch_keep"] += 1
                                        _log(
                                            "PGG2-V287-FRESH-CLEAN-CARRY-WATCH-KEEP "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                            f"blocker={fresh_clean_reason} source=fast_final_curve"
                                        )
                                        hist[mint].append(rec)
                                        continue
                                    else:
                                        counters[
                                            "selected_fresh_actual_shadow_only_block"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SELECTED-FRESH-ACTUAL-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                            f"reason=fresh_no_prior_shadow_only blocker={fresh_clean_reason} source=fast_final_curve"
                                        )
                                        active.pop(mint, None)
                                        hist[mint].append(rec)
                                        continue
                                min_quote_tokens = _v287_reason_min_quote_tokens(
                                    continuation_reason,
                                    base_min_quote_tokens,
                                )
                                if min_quote_tokens > base_min_quote_tokens + 1e-9:
                                    counters["selected_reason_token_floor_raise"] += 1
                                    _log(
                                        "PGG2-V287-SELECTED-REASON-TOKEN-FLOOR "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason} "
                                        f"base_min_tokens={base_min_quote_tokens:.6f} "
                                        f"reason_min_tokens={min_quote_tokens:.6f}"
                                    )
                                _log(
                                    "PGG2-V287-BUY-QUOTE-VIABILITY "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                    f"pass={int(quote_tokens >= min_quote_tokens)} source=fast_final_curve"
                                )
                                if min_quote_tokens > 0 and quote_tokens < min_quote_tokens:
                                    selected_negative_reason_allowed = (
                                        _v287_selected_negative_reason_allowed(
                                            continuation_reason or ""
                                        )
                                    )
                                    defer_low_token_to_refresh = (
                                        selected_negative_reason_allowed
                                        and plan_ready
                                        and os.environ.get(
                                            "V287_REFRESH_CURVE_AFTER_PLAN_RECHECK",
                                            "1",
                                        )
                                        != "0"
                                    )
                                    if defer_low_token_to_refresh:
                                        counters["buy_quote_token_low_refresh_defer"] += 1
                                        _log(
                                            "PGG2-V287-BUY-QUOTE-TOKEN-LOW-REFRESH-DEFER "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"amount_out_tokens={quote_tokens:.6f} "
                                            f"min_tokens={min_quote_tokens:.6f} "
                                            "source=fast_final_curve"
                                        )
                                    else:
                                        counters["buy_quote_token_block"] += 1
                                        _log(
                                            "PGG2-V287-BUY-QUOTE-TOKEN-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                            "source=fast_final_curve"
                                        )
                                        active.pop(mint, None)
                                        continue

                                max_quote_tokens = _v287_reason_max_quote_tokens(
                                    continuation_reason
                                )
                                if max_quote_tokens > 0:
                                    cap_pass = quote_tokens <= max_quote_tokens
                                    _log(
                                        "PGG2-V287-FINAL-BUY-QUOTE-TOKEN-CAP-CHECK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason} "
                                        f"amount_out_tokens={quote_tokens:.6f} "
                                        f"max_tokens={max_quote_tokens:.6f} "
                                        f"pass={int(cap_pass)} source=fast_final_curve"
                                    )
                                    if not cap_pass:
                                        (
                                            seed_prior_speed_cap_ok,
                                            seed_prior_speed_cap_reason,
                                        ) = _v287_seed_prior_speed_authority_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens,
                                        )
                                        if seed_prior_speed_cap_ok:
                                            counters["seed_prior_speed_cap_bypass"] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-SPEED-CAP-BYPASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"amount_out_tokens={quote_tokens:.6f} "
                                                f"max_tokens={max_quote_tokens:.6f} "
                                                f"authority_reason={seed_prior_speed_cap_reason} "
                                                "source=fast_final_curve"
                                            )
                                        elif _v287_seed_prior_clean_cap_override_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens,
                                            max_quote_tokens,
                                        ):
                                            counters["seed_prior_token_cap_override"] += 1
                                            _v287_log_seed_prior_cap_override(
                                                mint=mint,
                                                cand=cand,
                                                reason=continuation_reason,
                                                quote_tokens=quote_tokens,
                                                max_quote_tokens=max_quote_tokens,
                                                source="fast_final_curve",
                                            )
                                        elif _v287_seed_prior_hot_high_cap_bypass_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens=quote_tokens,
                                            max_quote_tokens=max_quote_tokens,
                                            source="fast_final_curve",
                                            mint=mint,
                                            counters=counters,
                                        ):
                                            pass
                                        else:
                                            counters["buy_quote_token_cap_block"] += 1
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
                                    curve_source_ts_ms = 0
                                    try:
                                        curve = broker.bonding_curve(as_pubkey(mint))
                                    except Exception as exc:
                                        (
                                            cached_curve,
                                            cached_ts_ms,
                                            cached_slot,
                                            cached_age_ms,
                                            cached_status,
                                        ) = _v287_curve_from_geyser_cache(
                                            curve_cache_by_key,
                                            mint,
                                            max_age_ms=int(
                                                os.environ.get(
                                                    "V287_GEYSER_CURVE_CACHE_MAX_AGE_MS",
                                                    "250",
                                                )
                                            ),
                                        )
                                        if cached_curve is None:
                                            counters["final_refresh_curve_missing_block"] += 1
                                            _log(
                                                "PGG2-V287-FINAL-REFRESH-CURVE-MISSING-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"err={type(exc).__name__}:{str(exc)[:160]} "
                                                f"cache_status={cached_status} "
                                                "reason=curve_unavailable_before_sender"
                                            )
                                            active.pop(mint, None)
                                            hist[mint].append(rec)
                                            continue
                                        curve = cached_curve
                                        counters["final_refresh_curve_geyser_cache_fallback"] += 1
                                        _log(
                                            "PGG2-V287-FINAL-REFRESH-CURVE-GEYSER-CACHE-FALLBACK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"cache_age_ms={cached_age_ms} slot={cached_slot} "
                                            f"cache_ts_ms={cached_ts_ms} "
                                            f"broker_err={type(exc).__name__}:{str(exc)[:120]}"
                                        )
                                        curve_source_ts_ms = int(cached_ts_ms)
                                    curve_ts_ms = int(curve_source_ts_ms or _now_ms())
                                    curve_ms = _now_ms() - fast_start_ms
                                    ok_proj, quote_tokens, _expected_raw = _prebuy_postbuy_sell_projection_from_curve(
                                        broker,
                                        mint,
                                        curve,
                                        args,
                                        log_tag="PGG2-V287-FAST-FINAL-PREBUY-REFRESH-CHECK",
                                    )
                                    refresh_self_roundtrip_negative = not bool(ok_proj)
                                    if not ok_proj:
                                        selected_negative_reason_allowed = (
                                            _v287_selected_negative_reason_allowed(
                                                continuation_reason or ""
                                            )
                                        )
                                        if (
                                            continuation_model_ok
                                            and quote_tokens > 0
                                            and (
                                                os.environ.get(
                                                    "V287_ALLOW_NEGATIVE_SELF_ROUNDTRIP_CONTINUATION",
                                                    "0",
                                                )
                                                == "1"
                                                or selected_negative_reason_allowed
                                            )
                                        ):
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
                                                f"mint={_short(mint)} full_mint={mint} "
                                                "reason=projection_negative_continuation_disabled"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    _log(
                                        "PGG2-V287-BUY-QUOTE-VIABILITY-REFRESH "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                        f"pass={int(quote_tokens >= min_quote_tokens)} source=fast_final_curve_refresh"
                                    )
                                    (
                                        seed_prior_speed_refresh_ok,
                                        seed_prior_speed_refresh_reason,
                                    ) = _v287_seed_prior_speed_authority_ok(
                                        cand,
                                        continuation_reason,
                                        quote_tokens,
                                    )
                                    if (
                                        seed_prior_speed_refresh_ok
                                        and min_quote_tokens > 0
                                        and quote_tokens < min_quote_tokens
                                    ):
                                        old_min_quote_tokens = float(min_quote_tokens)
                                        min_quote_tokens = min(
                                            old_min_quote_tokens,
                                            float(quote_tokens)
                                            * max(
                                                0.0,
                                                1.0
                                                - (
                                                    float(args.buy_slippage_pct)
                                                    / 100.0
                                                ),
                                            ),
                                        )
                                        counters[
                                            "seed_prior_speed_quote_floor_bypass"
                                        ] += 1
                                        cand[
                                            "seed_prior_speed_quote_floor_bypass"
                                        ] = 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-SPEED-QUOTE-FLOOR-BYPASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_speed_refresh_reason} "
                                            f"old_min_tokens={old_min_quote_tokens:.6f} "
                                            f"new_min_tokens={float(min_quote_tokens):.6f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            "source=fast_final_curve_refresh"
                                        )
                                    if min_quote_tokens > 0 and quote_tokens < min_quote_tokens:
                                        (
                                            watch_quote_floor_ok,
                                            watch_quote_floor_reason,
                                        ) = _v287_seed_prior_watch_quote_floor_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens,
                                        )
                                        if watch_quote_floor_ok:
                                            counters[
                                                "seed_prior_watch_quote_floor_allow"
                                            ] += 1
                                            old_min_quote_tokens = float(min_quote_tokens)
                                            min_quote_tokens = min(
                                                old_min_quote_tokens,
                                                float(quote_tokens)
                                                * max(
                                                    0.0,
                                                    1.0
                                                    - (
                                                        float(args.buy_slippage_pct)
                                                        / 100.0
                                                    ),
                                                ),
                                            )
                                            cand[
                                                "seed_prior_watch_quote_floor_allow"
                                            ] = 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-WATCH-QUOTE-FLOOR-ALLOW "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"refresh_tokens={float(quote_tokens):.6f} "
                                                f"old_min_tokens={old_min_quote_tokens:.6f} "
                                                f"new_min_tokens={float(min_quote_tokens):.6f} "
                                                f"watch_delta_sol={int(cand.get('seed_prior_watch_followthrough_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                f"watch_delta_buys={int(cand.get('seed_prior_watch_followthrough_buys') or 0)} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                f"first_rearm_delay_ms={int(cand.get('first_rearm_pass_delay_ms') or 999999)} "
                                                f"last_rearm_delay_ms={int(cand.get('last_rearm_pass_delay_ms') or 999999)} "
                                                f"shape={watch_quote_floor_reason} "
                                                "source=fast_final_curve_refresh"
                                            )
                                            postplan_quote_floor_ok = True
                                            postplan_quote_floor_reason = (
                                                watch_quote_floor_reason
                                            )
                                        else:
                                            postplan_quote_floor_ok = False
                                            postplan_quote_floor_reason = ""
                                        if not watch_quote_floor_ok:
                                            counters[
                                                "seed_prior_watch_quote_floor_block"
                                            ] += int(
                                                int(
                                                    cand.get(
                                                        "seed_prior_watch_followthrough_send_ok"
                                                    )
                                                    or 0
                                                )
                                                == 1
                                            )
                                        (
                                            postplan_quote_floor_ok,
                                            postplan_quote_floor_reason,
                                        ) = (
                                            (
                                                postplan_quote_floor_ok,
                                                postplan_quote_floor_reason,
                                            )
                                            if watch_quote_floor_ok
                                            else _v287_seed_prior_postplan_quote_floor_ok(
                                                cand,
                                                continuation_reason,
                                                pre_refresh_quote_tokens,
                                                quote_tokens,
                                                min_quote_tokens,
                                            )
                                        )
                                        if postplan_quote_floor_ok:
                                            if not watch_quote_floor_ok:
                                                counters[
                                                    "seed_prior_postplan_quote_floor_allow"
                                                ] += 1
                                                old_min_quote_tokens = float(min_quote_tokens)
                                                min_quote_tokens = min(
                                                    old_min_quote_tokens,
                                                    float(quote_tokens)
                                                    * max(
                                                        0.0,
                                                        1.0
                                                        - (
                                                            float(args.buy_slippage_pct)
                                                            / 100.0
                                                        ),
                                                    ),
                                                )
                                                cand[
                                                    "seed_prior_postplan_quote_floor_allow"
                                                ] = 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-POSTPLAN-QUOTE-FLOOR-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"pre_refresh_tokens={float(pre_refresh_quote_tokens):.6f} "
                                                    f"refresh_tokens={float(quote_tokens):.6f} "
                                                    f"old_min_tokens={old_min_quote_tokens:.6f} "
                                                    f"new_min_tokens={float(min_quote_tokens):.6f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"shape={postplan_quote_floor_reason} "
                                                    "source=fast_final_curve_refresh"
                                                )
                                        else:
                                            counters[
                                                "seed_prior_postplan_quote_floor_block"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-POSTPLAN-QUOTE-FLOOR-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"pre_refresh_tokens={float(pre_refresh_quote_tokens):.6f} "
                                                f"refresh_tokens={float(quote_tokens):.6f} "
                                                f"min_tokens={float(min_quote_tokens):.6f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                f"blocker={postplan_quote_floor_reason} "
                                                "source=fast_final_curve_refresh"
                                            )
                                        preplan_quote_floor_ok = False
                                        if postplan_quote_floor_ok:
                                            pass
                                        else:
                                            (
                                                preplan_quote_floor_ok,
                                                preplan_quote_floor_reason,
                                            ) = _v287_seed_prior_preplan_quote_floor_ok(
                                                cand,
                                                continuation_reason,
                                                pre_refresh_quote_tokens,
                                                quote_tokens,
                                                min_quote_tokens,
                                            )
                                            if preplan_quote_floor_ok:
                                                counters[
                                                    "seed_prior_preplan_quote_floor_allow"
                                                ] += 1
                                                old_min_quote_tokens = float(
                                                    min_quote_tokens
                                                )
                                                min_quote_tokens = min(
                                                    old_min_quote_tokens,
                                                    float(quote_tokens)
                                                    * max(
                                                        0.0,
                                                        1.0
                                                        - (
                                                            float(args.buy_slippage_pct)
                                                            / 100.0
                                                        ),
                                                    ),
                                                )
                                                cand[
                                                    "seed_prior_preplan_quote_floor_allow"
                                                ] = 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-PREPLAN-QUOTE-FLOOR-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"pre_refresh_tokens={float(pre_refresh_quote_tokens):.6f} "
                                                    f"refresh_tokens={float(quote_tokens):.6f} "
                                                    f"old_min_tokens={old_min_quote_tokens:.6f} "
                                                    f"new_min_tokens={float(min_quote_tokens):.6f} "
                                                    f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                    f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                    f"first_rearm_delay_ms={int(cand.get('first_rearm_pass_delay_ms') or 999999)} "
                                                    f"last_rearm_delay_ms={int(cand.get('last_rearm_pass_delay_ms') or 999999)} "
                                                    f"shape={preplan_quote_floor_reason} "
                                                    "source=fast_final_curve_refresh"
                                                )
                                                pass
                                            else:
                                                counters[
                                                    "seed_prior_preplan_quote_floor_block"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-PREPLAN-QUOTE-FLOOR-BLOCK "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"pre_refresh_tokens={float(pre_refresh_quote_tokens):.6f} "
                                                    f"refresh_tokens={float(quote_tokens):.6f} "
                                                    f"min_tokens={float(min_quote_tokens):.6f} "
                                                    f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                    f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                    f"blocker={preplan_quote_floor_reason} "
                                                    "source=fast_final_curve_refresh"
                                                )
                                        if postplan_quote_floor_ok or preplan_quote_floor_ok:
                                            pass
                                        elif not postplan_quote_floor_ok:
                                            (
                                                compression_ok,
                                                compression_reason,
                                            ) = _v287_seed_prior_refresh_compression_ok(
                                                cand,
                                                continuation_reason,
                                                pre_refresh_quote_tokens,
                                                quote_tokens,
                                            )
                                            if compression_ok:
                                                counters[
                                                    "seed_prior_refresh_compression_allow"
                                                ] += 1
                                                old_min_quote_tokens = float(
                                                    min_quote_tokens
                                                )
                                                min_quote_tokens = min(
                                                    float(min_quote_tokens),
                                                    float(quote_tokens)
                                                    * max(
                                                        0.0,
                                                        1.0
                                                        - (
                                                            float(args.buy_slippage_pct)
                                                            / 100.0
                                                        ),
                                                    ),
                                                )
                                                cand[
                                                    "seed_prior_refresh_compression_allow"
                                                ] = 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-REFRESH-COMPRESSION-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"pre_refresh_tokens={float(pre_refresh_quote_tokens):.6f} "
                                                    f"refresh_tokens={float(quote_tokens):.6f} "
                                                    f"compression_ratio={float(quote_tokens)/float(pre_refresh_quote_tokens):.4f} "
                                                    f"old_min_tokens={old_min_quote_tokens:.6f} "
                                                    f"new_min_tokens={float(min_quote_tokens):.6f} "
                                                    f"shape={compression_reason} "
                                                    "source=fast_final_curve_refresh"
                                                )
                                            else:
                                                low_quote_watch_enabled = (
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_ENABLED",
                                                        "1",
                                                    )
                                                    != "0"
                                                )
                                                low_quote_watch_min_tokens = float(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MIN_TOKENS",
                                                        "430000",
                                                    )
                                                )
                                                low_quote_watch_postplan_min_sol = float(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_POSTPLAN_MIN_SOL",
                                                        "2.50",
                                                    )
                                                )
                                                low_quote_watch_max_delay_ms = int(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MAX_REARM_DELAY_MS",
                                                        "80",
                                                    )
                                                )
                                                low_quote_watch_ok = (
                                                    low_quote_watch_enabled
                                                    and _v287_is_selected_seed_prior(
                                                        cand,
                                                        continuation_reason or "",
                                                    )
                                                    and float(quote_tokens)
                                                    >= low_quote_watch_min_tokens
                                                    and int(
                                                        cand.get(
                                                            "post_plan_rearm_passed"
                                                        )
                                                        or 0
                                                    )
                                                    == 1
                                                    and _v287_cand_post_plan_sol(cand)
                                                    >= low_quote_watch_postplan_min_sol
                                                    and int(
                                                        cand.get(
                                                            "first_rearm_pass_delay_ms"
                                                        )
                                                        or 999999
                                                    )
                                                    <= low_quote_watch_max_delay_ms
                                                    and int(
                                                        cand.get(
                                                            "last_rearm_pass_delay_ms"
                                                        )
                                                        or 999999
                                                    )
                                                    <= low_quote_watch_max_delay_ms
                                                    and float(cand.get("top_share") or 0.0)
                                                    >= 0.999
                                                )
                                                late_curve_watch_enabled = (
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_ENABLED",
                                                        "1",
                                                    )
                                                    != "0"
                                                )
                                                late_curve_min_tokens = float(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MIN_TOKENS",
                                                        "330000",
                                                    )
                                                )
                                                late_curve_max_tokens = float(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_TOKENS",
                                                        "430000",
                                                    )
                                                )
                                                late_curve_pre_sol = _v287_cand_pre_entry_sol(cand)
                                                late_curve_min_pre_sol = float(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MIN_PRE_ENTRY_SOL",
                                                        "2.00",
                                                    )
                                                )
                                                late_curve_max_pre_sol = float(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_PRE_ENTRY_SOL",
                                                        "2.65",
                                                    )
                                                )
                                                late_curve_min_buys = int(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MIN_BUYS",
                                                        "2",
                                                    )
                                                )
                                                late_curve_max_buys = int(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_BUYS",
                                                        "3",
                                                    )
                                                )
                                                late_curve_max_delay_ms = int(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_REARM_DELAY_MS",
                                                        "80",
                                                    )
                                                )
                                                late_curve_max_lag_ms = int(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MAX_LAST_REARM_LAG_MS",
                                                        "900",
                                                    )
                                                )
                                                late_curve_watch_ok = (
                                                    late_curve_watch_enabled
                                                    and _v287_is_selected_seed_prior(
                                                        cand,
                                                        continuation_reason or "",
                                                    )
                                                    and late_curve_min_tokens
                                                    <= float(quote_tokens)
                                                    <= late_curve_max_tokens
                                                    and late_curve_min_pre_sol
                                                    <= late_curve_pre_sol
                                                    <= late_curve_max_pre_sol
                                                    and late_curve_min_buys
                                                    <= int(cand.get("pre_entry_buys") or 0)
                                                    <= late_curve_max_buys
                                                    and int(
                                                        cand.get(
                                                            "first_rearm_pass_delay_ms"
                                                        )
                                                        or 999999
                                                    )
                                                    <= late_curve_max_delay_ms
                                                    and int(
                                                        cand.get(
                                                            "last_rearm_pass_delay_ms"
                                                        )
                                                        or 999999
                                                    )
                                                    <= late_curve_max_delay_ms
                                                    and int(
                                                        cand.get("last_rearm_lag_ms")
                                                        or cand.get(
                                                            "last_rearm_pass_lag_ms"
                                                        )
                                                        or 999999
                                                    )
                                                    <= late_curve_max_lag_ms
                                                    and int(cand.get("prev_sells") or 0) == 0
                                                    and float(cand.get("top_share") or 0.0)
                                                    >= 0.999
                                                )
                                                if low_quote_watch_ok or late_curve_watch_ok:
                                                    watch_now_ms = _now_ms()
                                                    cand["seed_prior_low_quote_watch"] = 1
                                                    if late_curve_watch_ok:
                                                        cand[
                                                            "seed_prior_late_curve_watch"
                                                        ] = 1
                                                    cand["no_movement_watch_keeps"] = int(
                                                        cand.get(
                                                            "no_movement_watch_keeps",
                                                            0,
                                                        )
                                                    ) + 1
                                                    cand[
                                                        "no_movement_watch_deadline_ms"
                                                    ] = watch_now_ms + int(
                                                        os.environ.get(
                                                            (
                                                                "V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MS"
                                                                if late_curve_watch_ok
                                                                else "V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MS"
                                                            ),
                                                            (
                                                                "950"
                                                                if late_curve_watch_ok
                                                                else "650"
                                                            ),
                                                        )
                                                    )
                                                    cand["no_movement_watch_reason"] = str(
                                                        continuation_reason or ""
                                                    )
                                                    cand[
                                                        "no_movement_watch_first_tokens"
                                                    ] = float(pre_refresh_quote_tokens)
                                                    cand[
                                                        "no_movement_watch_start_pre_entry_lamports"
                                                    ] = int(
                                                        cand.get("pre_entry_buy_lamports")
                                                        or 0
                                                    )
                                                    cand[
                                                        "no_movement_watch_start_buys"
                                                    ] = int(cand.get("pre_entry_buys") or 0)
                                                    cand["no_movement_watch_ts_ms"] = (
                                                        watch_now_ms
                                                    )
                                                    counters[
                                                        (
                                                            "seed_prior_late_curve_watch_keep"
                                                            if late_curve_watch_ok
                                                            else "seed_prior_low_quote_watch_keep"
                                                        )
                                                    ] += 1
                                                    _log(
                                                        (
                                                            "PGG2-V287-SEED-PRIOR-LATE-CURVE-WATCH-KEEP "
                                                            if late_curve_watch_ok
                                                            else "PGG2-V287-SEED-PRIOR-LOW-QUOTE-WATCH-KEEP "
                                                        )
                                                        +
                                                        f"mint={_short(mint)} full_mint={mint} "
                                                        f"reason={continuation_reason} "
                                                        f"refresh_tokens={float(quote_tokens):.6f} "
                                                        f"min_tokens={float(min_quote_tokens):.6f} "
                                                        f"low_watch_min_tokens={low_quote_watch_min_tokens:.6f} "
                                                        f"late_curve_band=[{late_curve_min_tokens:.6f},{late_curve_max_tokens:.6f}] "
                                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                        f"pre_entry_buy_sol={late_curve_pre_sol:.6f} "
                                                        f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                        f"watch_ms={int(os.environ.get(('V287_SELECTED_SEED_PRIOR_LATE_CURVE_WATCH_MS' if late_curve_watch_ok else 'V287_SELECTED_SEED_PRIOR_LOW_QUOTE_WATCH_MS'), ('950' if late_curve_watch_ok else '650')))} "
                                                        "reason_detail=low_quote_requires_fresh_post_boundary_buy"
                                                    )
                                                    hist[mint].append(rec)
                                                    continue
                                                counters[
                                                    "buy_quote_token_refresh_block"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-BUY-QUOTE-TOKEN-REFRESH-BLOCK "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"amount_out_tokens={quote_tokens:.6f} min_tokens={min_quote_tokens:.6f} "
                                                    f"compression_blocker={compression_reason} "
                                                    "source=fast_final_curve_refresh"
                                                )
                                                active.pop(mint, None)
                                                continue
                                    max_quote_tokens = _v287_reason_max_quote_tokens(
                                        continuation_reason
                                    )
                                    if max_quote_tokens > 0:
                                        cap_pass = quote_tokens <= max_quote_tokens
                                        _log(
                                            "PGG2-V287-FINAL-BUY-QUOTE-TOKEN-CAP-CHECK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"amount_out_tokens={quote_tokens:.6f} "
                                            f"max_tokens={max_quote_tokens:.6f} "
                                            f"pass={int(cap_pass)} source=fast_final_curve_refresh"
                                        )
                                        if not cap_pass:
                                            (
                                                seed_prior_speed_refresh_cap_ok,
                                                seed_prior_speed_refresh_cap_reason,
                                            ) = _v287_seed_prior_speed_authority_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens,
                                            )
                                            if seed_prior_speed_refresh_cap_ok:
                                                counters[
                                                    "seed_prior_speed_cap_refresh_bypass"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-SPEED-CAP-BYPASS "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"amount_out_tokens={quote_tokens:.6f} "
                                                    f"max_tokens={max_quote_tokens:.6f} "
                                                    f"authority_reason={seed_prior_speed_refresh_cap_reason} "
                                                    "source=fast_final_curve_refresh"
                                                )
                                            elif _v287_seed_prior_clean_cap_override_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens,
                                                max_quote_tokens,
                                            ):
                                                counters["seed_prior_token_cap_refresh_override"] += 1
                                                _v287_log_seed_prior_cap_override(
                                                    mint=mint,
                                                    cand=cand,
                                                    reason=continuation_reason,
                                                    quote_tokens=quote_tokens,
                                                    max_quote_tokens=max_quote_tokens,
                                                    source="fast_final_curve_refresh",
                                                )
                                            elif _v287_seed_prior_hot_high_cap_bypass_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens=quote_tokens,
                                                max_quote_tokens=max_quote_tokens,
                                                source="fast_final_curve_refresh",
                                                mint=mint,
                                                counters=counters,
                                            ):
                                                pass
                                            else:
                                                (
                                                    seed_prior_high_cap_watch_ok,
                                                    seed_prior_high_cap_watch_reason,
                                                ) = _v287_seed_prior_high_cap_watch_ok(
                                                    cand,
                                                    continuation_reason,
                                                    quote_tokens,
                                                    max_quote_tokens,
                                                )
                                                if seed_prior_high_cap_watch_ok:
                                                    watch_now_ms = _now_ms()
                                                    watch_ms = int(
                                                        os.environ.get(
                                                            "V287_SELECTED_SEED_PRIOR_HIGH_CAP_WATCH_MS",
                                                            "650",
                                                        )
                                                    )
                                                    counters[
                                                        "seed_prior_high_cap_watch_keep"
                                                    ] += 1
                                                    cand["seed_prior_high_cap_watch"] = 1
                                                    cand[
                                                        "seed_prior_high_cap_watch_reason"
                                                    ] = seed_prior_high_cap_watch_reason
                                                    cand[
                                                        "seed_prior_high_cap_watch_quote_tokens"
                                                    ] = float(quote_tokens)
                                                    cand["no_movement_watch_keeps"] = int(
                                                        cand.get(
                                                            "no_movement_watch_keeps",
                                                            0,
                                                        )
                                                    ) + 1
                                                    cand[
                                                        "no_movement_watch_deadline_ms"
                                                    ] = watch_now_ms + watch_ms
                                                    cand["no_movement_watch_reason"] = str(
                                                        continuation_reason or ""
                                                    )
                                                    cand[
                                                        "no_movement_watch_first_tokens"
                                                    ] = float(pre_refresh_quote_tokens)
                                                    cand[
                                                        "no_movement_watch_start_pre_entry_lamports"
                                                    ] = int(
                                                        cand.get("pre_entry_buy_lamports")
                                                        or 0
                                                    )
                                                    cand[
                                                        "no_movement_watch_start_buys"
                                                    ] = int(cand.get("pre_entry_buys") or 0)
                                                    cand["no_movement_watch_ts_ms"] = (
                                                        watch_now_ms
                                                    )
                                                    _log(
                                                        "PGG2-V287-SEED-PRIOR-HIGH-CAP-WATCH-KEEP "
                                                        f"mint={_short(mint)} full_mint={mint} "
                                                        f"reason={continuation_reason} "
                                                        f"watch_reason={seed_prior_high_cap_watch_reason} "
                                                        f"refresh_tokens={float(quote_tokens):.6f} "
                                                        f"base_max_tokens={float(max_quote_tokens):.6f} "
                                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                        f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                        f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                        f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                        f"watch_ms={watch_ms} "
                                                        "reason_detail=wait_for_post_cap_buy_followthrough"
                                                    )
                                                    hist[mint].append(rec)
                                                    continue
                                                counters["buy_quote_token_cap_refresh_block"] += 1
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
                                    selected_negative_reason_allowed = (
                                        _v287_selected_negative_reason_allowed(
                                            continuation_reason or ""
                                        )
                                    )
                                    (
                                        seed_prior_tiny_neg_ok,
                                        seed_prior_tiny_neg_reason,
                                    ) = _v287_seed_prior_tiny_negative_drift_ok(
                                        cand,
                                        continuation_reason or "",
                                        float(quote_tokens),
                                        float(final_refresh_drift_pct),
                                        bool(refresh_self_roundtrip_negative),
                                        _now_ms(),
                                    )
                                    (
                                        seed_prior_neg_refresh_watch_ok,
                                        seed_prior_neg_refresh_watch_reason,
                                    ) = _v287_seed_prior_negative_refresh_watch_ok(
                                        cand,
                                        continuation_reason or "",
                                        float(quote_tokens),
                                        float(final_refresh_drift_pct),
                                        _now_ms(),
                                    )
                                    (
                                        seed_prior_clean_cap_neg_watch_ok,
                                        seed_prior_clean_cap_neg_watch_reason,
                                    ) = _v287_seed_prior_tiny_neg_clean_cap_watch_ok(
                                        cand,
                                        continuation_reason or "",
                                        float(quote_tokens),
                                        float(final_refresh_drift_pct),
                                    )
                                    (
                                        seed_prior_neg_refresh_followthrough_ok,
                                        seed_prior_neg_refresh_followthrough_reason,
                                    ) = _v287_seed_prior_negative_refresh_followthrough_send_ok(
                                        cand,
                                        continuation_reason or "",
                                        float(quote_tokens),
                                        float(final_refresh_drift_pct),
                                        _now_ms(),
                                    )
                                    (
                                        seed_prior_credible_postplan_ok,
                                        seed_prior_credible_postplan_reason,
                                    ) = _v287_seed_prior_credible_postplan_boundary_ok(
                                        cand,
                                        continuation_reason or "",
                                        quote_tokens=float(quote_tokens),
                                        drift_pct=float(final_refresh_drift_pct),
                                    )
                                    if seed_prior_credible_postplan_ok:
                                        cand["seed_prior_credible_postplan_send_ok"] = 1
                                        counters[
                                            "seed_prior_credible_postplan_send_allow"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-CREDIBLE-POSTPLAN-SEND-ALLOW "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_credible_postplan_reason} "
                                            f"drift_pct={final_refresh_drift_pct:+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                            f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                            "reason_detail=bounded_post_plan_continuation_authorizes_final_send"
                                        )
                                    max_selected_neg_drift_pct = float(
                                        os.environ.get(
                                            "V287_SELECTED_MAX_NEG_REFRESH_DRIFT_PCT",
                                            "1.25",
                                        )
                                    )
                                    accel_selected_neg_drift_pct = float(
                                        os.environ.get(
                                            "V287_SELECTED_ACCELERATION_NEG_REFRESH_DRIFT_PCT",
                                            "8.00",
                                        )
                                    )
                                    if (
                                        selected_negative_reason_allowed
                                        and final_refresh_drift_pct
                                        < 0.0
                                        and os.environ.get(
                                            "V287_SELECTED_BLOCK_ANY_NEG_REFRESH_DRIFT",
                                            "0",
                                        )
                                        == "1"
                                        and not seed_prior_tiny_neg_ok
                                        and not seed_prior_neg_refresh_followthrough_ok
                                        and not seed_prior_speed_refresh_ok
                                        and not seed_prior_credible_postplan_ok
                                    ):
                                        if (
                                            seed_prior_neg_refresh_watch_ok
                                            or seed_prior_clean_cap_neg_watch_ok
                                        ):
                                            selected_neg_watch_reason = (
                                                seed_prior_neg_refresh_watch_reason
                                                if seed_prior_neg_refresh_watch_ok
                                                else seed_prior_clean_cap_neg_watch_reason
                                            )
                                            selected_neg_watch_source = (
                                                "standard_negative_refresh_watch"
                                                if seed_prior_neg_refresh_watch_ok
                                                else "tiny_negative_clean_cap_watch"
                                            )
                                            watch_ms = int(
                                                os.environ.get(
                                                    (
                                                        "V287_SELECTED_SEED_PRIOR_NEG_REFRESH_WATCH_MS"
                                                        if seed_prior_neg_refresh_watch_ok
                                                        else "V287_SELECTED_SEED_PRIOR_TINY_NEG_CLEAN_CAP_WATCH_MS"
                                                    ),
                                                    "650",
                                                )
                                            )
                                            counters[
                                                "seed_prior_negative_refresh_watch_keep"
                                            ] += 1
                                            cand["seed_prior_negative_refresh_watch"] = 1
                                            cand[
                                                "seed_prior_negative_refresh_watch_reason"
                                            ] = selected_neg_watch_reason
                                            cand[
                                                "seed_prior_negative_refresh_watch_drift_pct"
                                            ] = float(final_refresh_drift_pct)
                                            cand[
                                                "seed_prior_negative_refresh_watch_quote_tokens"
                                            ] = float(quote_tokens)
                                            cand["no_movement_watch_keeps"] = int(
                                                cand.get("no_movement_watch_keeps", 0)
                                            ) + 1
                                            cand["no_movement_watch_deadline_ms"] = (
                                                _now_ms() + watch_ms
                                            )
                                            cand["no_movement_watch_reason"] = str(
                                                continuation_reason or ""
                                            )
                                            cand["no_movement_watch_first_tokens"] = float(
                                                pre_refresh_quote_tokens
                                            )
                                            cand[
                                                "no_movement_watch_start_pre_entry_lamports"
                                            ] = int(cand.get("pre_entry_buy_lamports") or 0)
                                            cand["no_movement_watch_start_buys"] = int(
                                                cand.get("pre_entry_buys") or 0
                                            )
                                            cand["no_movement_watch_ts_ms"] = _now_ms()
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-NEG-REFRESH-WATCH-KEEP "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"watch_reason={selected_neg_watch_reason} "
                                                f"watch_source={selected_neg_watch_source} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                f"watch_ms={watch_ms} "
                                                "reason_detail=wait_for_next_buy_after_negative_refresh"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                        counters[
                                            "selected_refresh_negative_drift_hard_block"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SELECTED-REFRESH-NEGATIVE-DRIFT-HARD-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"drift_pct={final_refresh_drift_pct:+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            f"watch_reason={seed_prior_neg_refresh_watch_reason} "
                                            f"clean_cap_watch_reason={seed_prior_clean_cap_neg_watch_reason} "
                                            f"followthrough_reason={seed_prior_neg_refresh_followthrough_reason} "
                                            f"tiny_neg_reason={seed_prior_tiny_neg_reason} "
                                            f"cand_top_lane={str(cand.get('top_lane') or '')} "
                                            f"selected_top_lane={str(cand.get('selected_top_lane') or '')} "
                                            "source=final_refresh_before_sender "
                                            "reason_detail=prevent_pump_2006_fee_burn"
                                        )
                                        active.pop(mint, None)
                                        continue
                                    if (
                                        seed_prior_speed_refresh_ok
                                        and final_refresh_drift_pct < 0.0
                                    ):
                                        if (
                                            final_refresh_drift_pct
                                            < -abs(max_selected_neg_drift_pct)
                                            and not seed_prior_tiny_neg_ok
                                            and not seed_prior_neg_refresh_followthrough_ok
                                        ):
                                            counters[
                                                "seed_prior_speed_neg_drift_block"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-SPEED-NEG-DRIFT-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"authority_reason={seed_prior_speed_refresh_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"max_neg_drift_pct={-abs(max_selected_neg_drift_pct):+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                "source=final_refresh_before_sender "
                                                "reason_detail=speed_authority_cannot_override_adverse_final_refresh"
                                            )
                                            active.pop(mint, None)
                                            continue
                                        counters[
                                            "seed_prior_speed_neg_drift_bypass"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-SPEED-NEG-DRIFT-BYPASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_speed_refresh_reason} "
                                            f"drift_pct={final_refresh_drift_pct:+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            "source=final_refresh_before_sender"
                                        )
                                    if seed_prior_tiny_neg_ok:
                                        counters[
                                            "seed_prior_tiny_negative_drift_authority_defer"
                                        ] += 1
                                        cand["seed_prior_tiny_negative_drift_ok"] = 1
                                        cand[
                                            "seed_prior_tiny_negative_drift_reason"
                                        ] = seed_prior_tiny_neg_reason
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-TINY-NEG-DRIFT-DEFER "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_tiny_neg_reason} "
                                            f"drift_pct={final_refresh_drift_pct:+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                            f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                            "next_step=seed_prior_final_send_authority"
                                        )
                                    if (
                                        selected_negative_reason_allowed
                                        and final_refresh_drift_pct
                                        < -abs(max_selected_neg_drift_pct)
                                        and not seed_prior_neg_refresh_followthrough_ok
                                        and not seed_prior_speed_refresh_ok
                                        and not seed_prior_credible_postplan_ok
                                    ):
                                        if (
                                            os.environ.get(
                                                "V287_SELECTED_BLOCK_ANY_NEG_REFRESH_DRIFT",
                                                "0",
                                            )
                                            == "1"
                                            and not seed_prior_credible_postplan_ok
                                        ):
                                            counters[
                                                "selected_refresh_negative_drift_hard_block"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SELECTED-REFRESH-NEGATIVE-DRIFT-HARD-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"watch_reason={seed_prior_neg_refresh_watch_reason} "
                                                f"clean_cap_watch_reason={seed_prior_clean_cap_neg_watch_reason} "
                                                f"followthrough_reason={seed_prior_neg_refresh_followthrough_reason} "
                                                f"tiny_neg_reason={seed_prior_tiny_neg_reason} "
                                                f"cand_top_lane={str(cand.get('top_lane') or '')} "
                                                f"selected_top_lane={str(cand.get('selected_top_lane') or '')} "
                                                "source=final_refresh_before_sender "
                                                "reason_detail=prevent_pump_2006_fee_burn"
                                            )
                                            active.pop(mint, None)
                                            continue
                                        post_plan_followthrough_min_lamports = int(
                                            float(
                                                os.environ.get(
                                                    "V287_SELECTED_POSTPLAN_FOLLOWTHROUGH_MIN_SOL",
                                                    "0.70",
                                                )
                                            )
                                            * LAMPORTS_PER_SOL
                                        )
                                        post_plan_followthrough_ok = (
                                            int(
                                                cand.get(
                                                    "post_plan_followthrough_buys",
                                                    0,
                                                )
                                            )
                                            >= 1
                                            and int(
                                                cand.get(
                                                    "post_plan_followthrough_lamports",
                                                    0,
                                                )
                                            )
                                            >= post_plan_followthrough_min_lamports
                                        )
                                        dense_moderate_ok = (
                                            continuation_reason
                                            == "selected_fresh_dense_moderate_train"
                                        )
                                        acceleration_refresh = (
                                            final_refresh_drift_pct
                                            <= -abs(accel_selected_neg_drift_pct)
                                        )
                                        if (
                                            acceleration_refresh
                                            or post_plan_followthrough_ok
                                            or dense_moderate_ok
                                        ):
                                            counters[
                                                "selected_refresh_followthrough_pass"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SELECTED-REFRESH-FOLLOWTHROUGH-PASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"accel_threshold_pct={-abs(accel_selected_neg_drift_pct):+.3f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={int(cand.get('post_plan_followthrough_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                f"dense_moderate={int(dense_moderate_ok)} "
                                                "source=final_refresh_before_sender"
                                            )
                                        else:
                                            weak_drift_keep_watch = (
                                                os.environ.get(
                                                    "V287_SELECTED_WEAK_DRIFT_KEEP_WATCH",
                                                    "1",
                                                )
                                                != "0"
                                                and str(cand.get("top_lane") or "")
                                                == "fresh_impulse"
                                                and float(cand.get("prev_buy_sol") or 0.0)
                                                <= 1e-12
                                                and (
                                                    _now_ms()
                                                    - int(
                                                        cand.get("start_ms")
                                                        or _now_ms()
                                                    )
                                                )
                                                <= int(
                                                    os.environ.get(
                                                        "V287_SELECTED_WEAK_DRIFT_WATCH_MAX_AGE_MS",
                                                        "900",
                                                    )
                                                )
                                            )
                                            if weak_drift_keep_watch:
                                                cand["weak_drift_watch_keep"] = 1
                                                cand["weak_drift_watch_deadline_ms"] = (
                                                    _now_ms()
                                                    + int(
                                                        os.environ.get(
                                                            "V287_SELECTED_WEAK_DRIFT_WATCH_AFTER_REFRESH_MS",
                                                            "650",
                                                        )
                                                    )
                                                )
                                                counters[
                                                    "selected_weak_drift_watch_keep"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SELECTED-WEAK-DRIFT-WATCH-KEEP "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"max_neg_drift_pct={-abs(max_selected_neg_drift_pct):+.3f} "
                                                    f"watch_max_age_ms={int(os.environ.get('V287_SELECTED_WEAK_DRIFT_WATCH_MAX_AGE_MS', '900'))} "
                                                    "reason_detail=wait_for_next_followthrough_buy"
                                                )
                                                hist[mint].append(rec)
                                                continue
                                            counters[
                                                "selected_refresh_adverse_drift_block"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SELECTED-REFRESH-ADVERSE-DRIFT-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"max_neg_drift_pct={-abs(max_selected_neg_drift_pct):+.3f} "
                                                f"accel_threshold_pct={-abs(accel_selected_neg_drift_pct):+.3f} "
                                                "source=final_refresh_before_sender"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    if (
                                        min_abs_refresh_drift_pct > 0
                                        and abs(final_refresh_drift_pct)
                                        < min_abs_refresh_drift_pct
                                        and not seed_prior_tiny_neg_ok
                                    ):
                                        any_negative_no_movement = (
                                            refresh_self_roundtrip_negative
                                            and os.environ.get(
                                                "V287_BLOCK_ANY_NEGATIVE_NO_MOVEMENT",
                                                "1",
                                            )
                                            != "0"
                                        )
                                        single_prior_negative_no_movement = (
                                            refresh_self_roundtrip_negative
                                            and os.environ.get(
                                                "V287_BLOCK_SINGLE_PRIOR_NEGATIVE_NO_MOVEMENT",
                                                "1",
                                            )
                                            != "0"
                                            and str(cand.get("top_lane") or "")
                                            == "single_prior_buy_continuation"
                                            and continuation_reason
                                            in (
                                                "selected_single_prior_strong_rearm",
                                                "selected_single_prior_no_movement_followthrough",
                                            )
                                        )
                                        (
                                            seed_prior_flat_send_ok,
                                            seed_prior_flat_reason,
                                        ) = _v287_seed_prior_flat_send_ok(
                                            cand, continuation_reason, quote_tokens
                                        )
                                        if any_negative_no_movement and seed_prior_flat_send_ok:
                                            counters[
                                                "seed_prior_flat_negative_roundtrip_bypass"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-FLAT-SEND-ALLOW "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"shape={seed_prior_flat_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                "reason_detail=clean_seed_prior_replay_shape"
                                            )
                                            any_negative_no_movement = False
                                        seed_prior_watch_followthrough_send_ok = (
                                            any_negative_no_movement
                                            and os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_WATCH_FOLLOWTHROUGH_SEND_ENABLED",
                                                "1",
                                            )
                                            != "0"
                                            and int(
                                                cand.get(
                                                    "seed_prior_watch_followthrough_send_ok",
                                                    0,
                                                )
                                            )
                                            == 1
                                            and _v287_is_selected_seed_prior(
                                                cand,
                                                continuation_reason,
                                            )
                                        )
                                        if seed_prior_watch_followthrough_send_ok:
                                            counters[
                                                "seed_prior_watch_followthrough_send_allow"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-WATCH-FOLLOWTHROUGH-SEND-ALLOW "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"delta_buy_sol={int(cand.get('seed_prior_watch_followthrough_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                f"delta_buys={int(cand.get('seed_prior_watch_followthrough_buys') or 0)} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                "reason_detail=real_post_watch_buy_continuation"
                                            )
                                            any_negative_no_movement = False
                                        (
                                            seed_prior_postplan_zero_ok,
                                            seed_prior_postplan_zero_reason,
                                        ) = _v287_seed_prior_postplan_followthrough_ok(
                                            cand,
                                            continuation_reason,
                                            min_sol_env=(
                                                "V287_SELECTED_SEED_PRIOR_ZERODRIFT_POSTPLAN_MIN_SOL"
                                            ),
                                            default_min_sol="0.70",
                                        )
                                        if (
                                            any_negative_no_movement
                                            and seed_prior_postplan_zero_ok
                                            and final_refresh_drift_pct >= -1e-9
                                        ):
                                            (
                                                credible_postplan_send_ok,
                                                credible_postplan_send_reason,
                                            ) = _v287_seed_prior_credible_postplan_boundary_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens=quote_tokens,
                                                drift_pct=final_refresh_drift_pct,
                                            )
                                            (
                                                consumed_postplan_send_ok,
                                                consumed_postplan_send_reason,
                                            ) = _v287_seed_prior_consumed_postplan_authority_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens=quote_tokens,
                                                drift_pct=final_refresh_drift_pct,
                                            )
                                            (
                                                one_strong_postplan_zero_ok,
                                                one_strong_postplan_zero_reason,
                                            ) = _v287_seed_prior_one_strong_postplan_zerodrift_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens=quote_tokens,
                                                drift_pct=final_refresh_drift_pct,
                                            )
                                            (
                                                speed_postplan_zero_ok,
                                                speed_postplan_zero_reason,
                                            ) = _v287_seed_prior_speed_postplan_zerodrift_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens=quote_tokens,
                                                drift_pct=final_refresh_drift_pct,
                                            )
                                            zero_drift_send_enabled = (
                                                os.environ.get(
                                                    "V287_SELECTED_SEED_PRIOR_POSTPLAN_ZERODRIFT_SEND_ENABLED",
                                                    "0",
                                                )
                                                == "1"
                                            )
                                            if credible_postplan_send_ok:
                                                if final_refresh_drift_pct <= 1e-9:
                                                    watch_now_ms = _now_ms()
                                                    watch_ms = int(
                                                        os.environ.get(
                                                            "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MS",
                                                            "350",
                                                        )
                                                    )
                                                    _v287_mark_seed_prior_consumed_zero_watch(
                                                        cand,
                                                        continuation_reason,
                                                        watch_now_ms,
                                                        watch_ms,
                                                    )
                                                    cand[
                                                        "seed_prior_credible_postplan_zero_watch_pending"
                                                    ] = 1
                                                    counters[
                                                        "seed_prior_credible_postplan_zero_watch_keep"
                                                    ] += 1
                                                    _log(
                                                        "PGG2-V287-SEED-PRIOR-CREDIBLE-POSTPLAN-ZERO-WATCH-KEEP "
                                                        f"mint={_short(mint)} full_mint={mint} "
                                                        f"reason={continuation_reason} "
                                                        f"shape={credible_postplan_send_reason} "
                                                        f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                        f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                        f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                        f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                        f"quote_tokens={float(quote_tokens):.6f} "
                                                        f"watch_ms={watch_ms} "
                                                        "reason_detail=credible_zero_drift_requires_fresh_post_boundary_buy"
                                                    )
                                                    hist[mint].append(rec)
                                                    continue
                                                cand[
                                                    "seed_prior_credible_postplan_send_ok"
                                                ] = 1
                                                counters[
                                                    "seed_prior_credible_postplan_send_allow"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-CREDIBLE-POSTPLAN-ZERODRIFT-SEND-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"shape={credible_postplan_send_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                    f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                    f"quote_tokens={float(quote_tokens):.6f} "
                                                    "reason_detail=bounded_post_plan_flow_already_in_final_curve_read"
                                                )
                                                any_negative_no_movement = False
                                            elif consumed_postplan_send_ok:
                                                watch_now_ms = _now_ms()
                                                watch_ms = int(
                                                    os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MS",
                                                        "350",
                                                    )
                                                )
                                                _v287_mark_seed_prior_consumed_zero_watch(
                                                    cand,
                                                    continuation_reason,
                                                    watch_now_ms,
                                                    watch_ms,
                                                )
                                                counters[
                                                    "seed_prior_consumed_postplan_zero_watch_keep"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-CONSUMED-POSTPLAN-ZERO-WATCH-KEEP "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"shape={consumed_postplan_send_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                    f"last_rearm_lag_ms={int(cand.get('last_rearm_lag_ms') or cand.get('last_rearm_pass_lag_ms') or cand.get('last_rearm_pass_delay_ms') or 999999)} "
                                                    f"quote_tokens={float(quote_tokens):.6f} "
                                                    f"watch_ms={watch_ms} "
                                                    "reason_detail=consumed_zero_drift_requires_fresh_post_boundary_buy"
                                                )
                                                hist[mint].append(rec)
                                                continue
                                            elif one_strong_postplan_zero_ok:
                                                cand[
                                                    "seed_prior_one_strong_postplan_zero_drift_send_ok"
                                                ] = 1
                                                counters[
                                                    "seed_prior_one_strong_postplan_zero_drift_send_allow"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-ONE-STRONG-POSTPLAN-ZERODRIFT-SEND-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"shape={one_strong_postplan_zero_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                    f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                    f"last_rearm_lag_ms={int(cand.get('last_rearm_lag_ms') or cand.get('last_rearm_pass_lag_ms') or cand.get('last_rearm_pass_delay_ms') or 999999)} "
                                                    f"quote_tokens={float(quote_tokens):.6f} "
                                                    "reason_detail=one_strong_post_plan_flow_already_in_final_curve_read"
                                                )
                                                any_negative_no_movement = False
                                            elif speed_postplan_zero_ok:
                                                cand[
                                                    "seed_prior_speed_postplan_zero_drift_send_ok"
                                                ] = 1
                                                counters[
                                                    "seed_prior_speed_postplan_zero_drift_send_allow"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-SPEED-POSTPLAN-ZERODRIFT-SEND-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"shape={speed_postplan_zero_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                    f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                    f"quote_tokens={float(quote_tokens):.6f} "
                                                    "reason_detail=speed_authority_post_plan_flow_already_in_final_curve_read"
                                                )
                                                any_negative_no_movement = False
                                            elif zero_drift_send_enabled:
                                                cand[
                                                    "seed_prior_postplan_zero_drift_send_ok"
                                                ] = 1
                                                counters[
                                                    "seed_prior_postplan_zero_drift_send_allow"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-POSTPLAN-ZERODRIFT-SEND-ALLOW "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"shape={seed_prior_postplan_zero_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"quote_tokens={float(quote_tokens):.6f} "
                                                    "reason_detail=real_post_plan_buy_authorizes_zero_drift_negative_roundtrip"
                                                )
                                                any_negative_no_movement = False
                                            else:
                                                cand[
                                                    "seed_prior_postplan_zero_drift_send_ok"
                                                ] = 0
                                                counters[
                                                    "seed_prior_postplan_zero_drift_send_block"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-SEED-PRIOR-POSTPLAN-ZERODRIFT-SEND-BLOCK "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"shape={seed_prior_postplan_zero_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                    f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                    f"quote_tokens={float(quote_tokens):.6f} "
                                                    "reason_detail=post_plan_flow_already_consumed_before_send_boundary"
                                                )
                                        allow_no_movement_followthrough_send = (
                                            (
                                                continuation_reason
                                                == "selected_single_prior_no_movement_followthrough"
                                                or (
                                                    continuation_reason
                                                    == "selected_single_prior_strong_rearm"
                                                    and str(cand.get("top_lane") or "")
                                                    == "single_prior_buy_continuation"
                                                    and os.environ.get(
                                                        "V287_SELECTED_SINGLE_PRIOR_ALLOW_FLAT_REFRESH",
                                                        "1",
                                                    )
                                                    != "0"
                                                )
                                                or (
                                                    continuation_reason
                                                    == "selected_high_current_clean_train"
                                                    and str(cand.get("top_lane") or "")
                                                    == "high_current_clean_train"
                                                    and os.environ.get(
                                                        "V287_HIGH_CURRENT_TRAIN_ALLOW_FLAT_REFRESH",
                                                        "1",
                                                    )
                                                    != "0"
                                                )
                                                or (
                                                    continuation_reason
                                                    == "selected_seed_prior_carry_rearm"
                                                    and str(cand.get("top_lane") or "")
                                                    == "seed_prior_carry_continuation"
                                                    and os.environ.get(
                                                        "V287_SELECTED_SEED_PRIOR_ALLOW_FLAT_REFRESH",
                                                        "1",
                                                    )
                                                    != "0"
                                                )
                                            )
                                            and final_refresh_drift_pct >= -1e-9
                                            and not any_negative_no_movement
                                            and not single_prior_negative_no_movement
                                        )
                                        if any_negative_no_movement:
                                            counters[
                                                "selected_negative_no_movement_suppressed"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-NEGATIVE-NO-MOVEMENT-SUPPRESS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                "action=watch_or_block "
                                                "reason_detail=zero_drift_cannot_authorize_negative_roundtrip"
                                            )
                                        if single_prior_negative_no_movement:
                                            counters[
                                                "selected_single_prior_negative_no_movement_suppressed"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SINGLE-PRIOR-NEGATIVE-NO-MOVEMENT-SUPPRESS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                "action=watch_or_block "
                                                "reason_detail=zero_drift_cannot_authorize_negative_roundtrip"
                                            )
                                        if allow_no_movement_followthrough_send:
                                            counters[
                                                "selected_no_movement_followthrough_send_allow"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SELECTED-NO-MOVEMENT-FOLLOWTHROUGH-SEND-ALLOW "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                f"pre_entry_buy_sol={float(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                "reason_detail=observed_followthrough_after_zero_drift_watch"
                                            )
                                        else:
                                            no_movement_keep_watch = (
                                                selected_negative_reason_allowed
                                                and os.environ.get(
                                                    "V287_SELECTED_NO_MOVEMENT_KEEP_WATCH",
                                                    "1",
                                                )
                                                != "0"
                                                and (
                                                    _now_ms()
                                                    - int(
                                                        cand.get("start_ms")
                                                        or _now_ms()
                                                    )
                                                )
                                                <= int(
                                                    os.environ.get(
                                                        "V287_SELECTED_NO_MOVEMENT_WATCH_MAX_AGE_MS",
                                                        "1200",
                                                    )
                                                )
                                                and int(
                                                    cand.get(
                                                        "no_movement_watch_keeps",
                                                        0,
                                                    )
                                                )
                                                < int(
                                                    os.environ.get(
                                                        "V287_SELECTED_NO_MOVEMENT_WATCH_MAX_KEEPS",
                                                        "3",
                                                    )
                                                )
                                            )
                                            if no_movement_keep_watch:
                                                cand["no_movement_watch_keeps"] = int(
                                                    cand.get("no_movement_watch_keeps", 0)
                                                ) + 1
                                                cand["no_movement_watch_deadline_ms"] = (
                                                    _now_ms()
                                                    + int(
                                                        os.environ.get(
                                                            "V287_SELECTED_NO_MOVEMENT_WATCH_AFTER_REFRESH_MS",
                                                            "650",
                                                        )
                                                    )
                                                )
                                                cand["no_movement_watch_reason"] = str(
                                                    continuation_reason or ""
                                                )
                                                cand["no_movement_watch_first_tokens"] = float(
                                                    pre_refresh_quote_tokens
                                                )
                                                cand[
                                                    "no_movement_watch_start_pre_entry_lamports"
                                                ] = int(
                                                    cand.get("pre_entry_buy_lamports")
                                                    or 0
                                                )
                                                cand["no_movement_watch_start_buys"] = int(
                                                    cand.get("pre_entry_buys") or 0
                                                )
                                                cand["no_movement_watch_ts_ms"] = _now_ms()
                                                counters[
                                                    "selected_no_movement_watch_keep"
                                                ] += 1
                                                _log(
                                                    "PGG2-V287-FINAL-REFRESH-NO-MOVEMENT-WATCH-KEEP "
                                                    f"mint={_short(mint)} full_mint={mint} "
                                                    f"reason={continuation_reason} "
                                                    f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                    f"keep_count={int(cand.get('no_movement_watch_keeps') or 0)} "
                                                    f"watch_max_age_ms={int(os.environ.get('V287_SELECTED_NO_MOVEMENT_WATCH_MAX_AGE_MS', '1200'))} "
                                                    "reason_detail=wait_for_next_real_followthrough_buy"
                                                )
                                                hist[mint].append(rec)
                                                continue
                                            counters["final_refresh_no_movement_block"] += 1
                                            _log(
                                                "PGG2-V287-FINAL-REFRESH-DRIFT-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"drift_pct={final_refresh_drift_pct:+.3f} "
                                                "reason=no_curve_movement_before_sender"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    (
                                        seed_prior_send_ok,
                                        seed_prior_send_reason,
                                    ) = _v287_seed_prior_final_send_authority(
                                        cand=cand,
                                        reason=continuation_reason,
                                        quote_tokens=float(quote_tokens),
                                        final_refresh_drift_pct=float(
                                            final_refresh_drift_pct
                                        ),
                                        self_roundtrip_negative=bool(
                                            refresh_self_roundtrip_negative
                                        ),
                                    )
                                    if _v287_is_selected_seed_prior(
                                        cand, continuation_reason
                                    ):
                                        strong_eval = (
                                            cand.get(
                                                "v287_seed_prior_strong_drift_eval"
                                            )
                                            or {}
                                        )
                                        watch_eval = (
                                            cand.get(
                                                "v287_seed_prior_watch_followthrough_eval"
                                            )
                                            or {}
                                        )
                                        moderate_eval = (
                                            cand.get(
                                                "v287_seed_prior_moderate_drift_eval"
                                            )
                                            or {}
                                        )
                                        speed_eval = (
                                            cand.get(
                                                "v287_seed_prior_speed_authority_eval"
                                            )
                                            or {}
                                        )
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-FINAL-SEND-AUTHORITY-CHECK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"pass={int(seed_prior_send_ok)} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_send_reason} "
                                            f"self_roundtrip_negative={int(bool(refresh_self_roundtrip_negative))} "
                                            f"drift_pct={float(final_refresh_drift_pct):+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                            f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                            f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                            f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                            f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                            f"watch_flag={int(watch_eval.get('send_flag') or cand.get('seed_prior_watch_followthrough_send_ok') or 0)} "
                                            f"watch_pass={int(watch_eval.get('pass') or 0)} "
                                            f"watch_reason={str(watch_eval.get('reason') or '-')} "
                                            f"watch_age_ms={int(watch_eval.get('follow_age_ms') or 0)} "
                                            f"watch_max_age_ms={int(watch_eval.get('max_follow_age_ms') or 0)} "
                                            f"watch_delta_sol={int(watch_eval.get('follow_delta_lamports') or cand.get('seed_prior_watch_followthrough_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                            f"watch_delta_buys={int(watch_eval.get('follow_delta_buys') or cand.get('seed_prior_watch_followthrough_buys') or 0)} "
                                            f"watch_prev_sells={int(watch_eval.get('prev_sells') or cand.get('prev_sells') or 0)} "
                                            f"watch_top_share={float(watch_eval.get('top_share') or cand.get('top_share') or 0.0):.4f} "
                                            f"moderate_pass={int(moderate_eval.get('pass') or 0)} "
                                            f"moderate_reason={str(moderate_eval.get('reason') or '-')} "
                                            f"moderate_min_drift={float(moderate_eval.get('min_drift_pct') or 0.0):.3f} "
                                            f"moderate_max_drift={float(moderate_eval.get('max_drift_pct') or 0.0):.3f} "
                                            f"moderate_quote_min={float(moderate_eval.get('min_quote_tokens') or 0.0):.3f} "
                                            f"moderate_quote_max={float(moderate_eval.get('max_quote_tokens') or 0.0):.3f} "
                                            f"speed_pass={int(speed_eval.get('pass') or 0)} "
                                            f"speed_reason={str(speed_eval.get('result_reason') or '-')} "
                                            f"speed_quote_min={float(speed_eval.get('min_quote_tokens') or 0.0):.3f} "
                                            f"speed_quote_max={float(speed_eval.get('max_quote_tokens') or 0.0):.3f} "
                                            f"speed_first_delay_ms={int(speed_eval.get('first_delay_ms') or 0)} "
                                            f"speed_last_delay_ms={int(speed_eval.get('last_delay_ms') or 0)} "
                                            f"speed_lag_ms={int(speed_eval.get('last_lag_ms') or 0)} "
                                            f"strong_allow={int(strong_eval.get('allow') or 0)} "
                                            f"strong_pass={int(strong_eval.get('pass') or 0)} "
                                            f"strong_min_drift_pct={float(strong_eval.get('min_drift_pct') or 0.0):.3f} "
                                            f"strong_current_max={float(strong_eval.get('max_current_sol') or 0.0):.3f} "
                                            f"strong_pre_entry_max={float(strong_eval.get('max_pre_entry_sol') or 0.0):.3f} "
                                            f"strong_buys_max={int(strong_eval.get('max_pre_entry_buys') or 0)} "
                                            f"strong_quote_max={float(strong_eval.get('max_quote_tokens') or 0.0):.3f} "
                                            f"strong_top_min={float(strong_eval.get('min_top_share') or 0.0):.4f}"
                                        )
                                    if not seed_prior_send_ok:
                                        authority_wait_reason = str(
                                            seed_prior_send_reason or ""
                                        )
                                        authority_wait_enabled = (
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_AUTHORITY_POSTPLAN_WAIT_ENABLED",
                                                "1",
                                            )
                                            != "0"
                                        )
                                        authority_waits = int(
                                            cand.get(
                                                "seed_prior_authority_postplan_waits",
                                                0,
                                            )
                                        )
                                        authority_max_waits = int(
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_AUTHORITY_POSTPLAN_MAX_WAITS",
                                                "2",
                                            )
                                        )
                                        authority_max_age_ms = int(
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_AUTHORITY_POSTPLAN_WAIT_MAX_AGE_MS",
                                                "1400",
                                            )
                                        )
                                        authority_now_ms = _now_ms()
                                        authority_age_ms = max(
                                            0,
                                            authority_now_ms
                                            - int(cand.get("start_ms") or authority_now_ms),
                                        )
                                        speed_negative_projection_wait = (
                                            authority_wait_reason
                                            in {
                                                "speed_negative_zero_drift_requires_post_final_followthrough",
                                                "speed_negative_projection_still_negative",
                                            }
                                        )
                                        consumed_zero_watch_wait = (
                                            authority_wait_reason
                                            in {
                                                "consumed_postplan_zero_drift_requires_fresh_post_final_followthrough",
                                                "credible_postplan_zero_drift_requires_fresh_post_final_followthrough",
                                            }
                                        )
                                        consumed_zero_watch_max_waits = int(
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MAX_WAITS",
                                                "1",
                                            )
                                        )
                                        consumed_zero_watch_max_age_ms = int(
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MAX_AGE_MS",
                                                "1400",
                                            )
                                        )
                                        if (
                                            authority_wait_enabled
                                            and consumed_zero_watch_wait
                                            and os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_ENABLED",
                                                "1",
                                            )
                                            != "0"
                                            and authority_waits
                                            < consumed_zero_watch_max_waits
                                            and authority_age_ms
                                            <= consumed_zero_watch_max_age_ms
                                        ):
                                            watch_ms = int(
                                                os.environ.get(
                                                    "V287_SELECTED_SEED_PRIOR_CONSUMED_ZERO_WATCH_MS",
                                                    "350",
                                                )
                                            )
                                            _v287_mark_seed_prior_consumed_zero_watch(
                                                cand,
                                                continuation_reason,
                                                authority_now_ms,
                                                watch_ms,
                                            )
                                            cand[
                                                "seed_prior_authority_postplan_waits"
                                            ] = authority_waits + 1
                                            counters[
                                                "seed_prior_consumed_postplan_zero_watch_keep"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-CONSUMED-POSTPLAN-ZERO-WATCH-KEEP "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"authority_reason={authority_wait_reason} "
                                                f"drift_pct={float(final_refresh_drift_pct):+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                f"wait_count={authority_waits + 1} "
                                                f"max_waits={consumed_zero_watch_max_waits} "
                                                f"age_ms={authority_age_ms} "
                                                f"max_age_ms={consumed_zero_watch_max_age_ms} "
                                                f"watch_ms={watch_ms} "
                                                "reason_detail=do_not_send_consumed_zero_drift_until_fresh_buy"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                        speed_negative_projection_max_waits = int(
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_SPEED_NEGATIVE_PROJECTION_MAX_WAITS",
                                                "1",
                                            )
                                        )
                                        speed_negative_projection_max_age_ms = int(
                                            os.environ.get(
                                                "V287_SELECTED_SEED_PRIOR_SPEED_NEGATIVE_PROJECTION_MAX_AGE_MS",
                                                "1200",
                                            )
                                        )
                                        if (
                                            authority_wait_enabled
                                            and speed_negative_projection_wait
                                            and authority_waits
                                            < speed_negative_projection_max_waits
                                            and authority_age_ms
                                            <= speed_negative_projection_max_age_ms
                                        ):
                                            watch_ms = int(
                                                os.environ.get(
                                                    "V287_SELECTED_SEED_PRIOR_SPEED_NEGATIVE_PROJECTION_WATCH_MS",
                                                    "220",
                                                )
                                            )
                                            cand["seed_prior_speed_negative_projection_watch"] = 1
                                            cand[
                                                "seed_prior_speed_negative_projection_watch_reason"
                                            ] = authority_wait_reason
                                            cand["post_plan_rearm_required"] = 1
                                            cand["post_plan_rearm_wait_start_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["post_plan_rearm_wait_last_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["post_plan_rearm_base_lamports"] = int(
                                                cand.get("pre_entry_buy_lamports") or 0
                                            )
                                            cand["post_plan_rearm_base_buys"] = int(
                                                cand.get("pre_entry_buys") or 0
                                            )
                                            cand["no_movement_watch_keeps"] = int(
                                                cand.get("no_movement_watch_keeps", 0)
                                            ) + 1
                                            cand["no_movement_watch_deadline_ms"] = (
                                                authority_now_ms + watch_ms
                                            )
                                            cand["no_movement_watch_reason"] = str(
                                                continuation_reason or ""
                                            )
                                            cand[
                                                "no_movement_watch_start_pre_entry_lamports"
                                            ] = int(cand.get("pre_entry_buy_lamports") or 0)
                                            cand["no_movement_watch_start_buys"] = int(
                                                cand.get("pre_entry_buys") or 0
                                            )
                                            cand["no_movement_watch_ts_ms"] = (
                                                authority_now_ms
                                            )
                                            cand[
                                                "seed_prior_authority_postplan_waits"
                                            ] = authority_waits + 1
                                            counters[
                                                "seed_prior_speed_negative_projection_watch_keep"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-SPEED-NEGATIVE-PROJECTION-WATCH-KEEP "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"authority_reason={authority_wait_reason} "
                                                f"drift_pct={float(final_refresh_drift_pct):+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                f"post_plan_buys={int(cand.get('post_plan_followthrough_buys') or 0)} "
                                                f"post_plan_buy_sol={_v287_cand_post_plan_sol(cand):.6f} "
                                                f"wait_count={authority_waits + 1} "
                                                f"max_waits={speed_negative_projection_max_waits} "
                                                f"age_ms={authority_age_ms} "
                                                f"max_age_ms={speed_negative_projection_max_age_ms} "
                                                f"watch_ms={watch_ms} "
                                                "reason_detail=negative_final_projection_needs_fresh_post_final_buy_or_positive_projection"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                        (
                                            seed_prior_weak_drift_watch_ok,
                                            seed_prior_weak_drift_watch_reason,
                                        ) = _v287_seed_prior_weak_drift_watch_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens=float(quote_tokens),
                                            drift_pct=float(final_refresh_drift_pct),
                                            now_ms=authority_now_ms,
                                        )
                                        if (
                                            authority_wait_enabled
                                            and authority_wait_reason
                                            == "weak_final_drift_negative_roundtrip"
                                            and seed_prior_weak_drift_watch_ok
                                            and authority_waits < authority_max_waits
                                            and authority_age_ms <= authority_max_age_ms
                                        ):
                                            watch_ms = int(
                                                os.environ.get(
                                                    "V287_SELECTED_SEED_PRIOR_WEAK_DRIFT_WATCH_MS",
                                                    "650",
                                                )
                                            )
                                            cand["seed_prior_weak_drift_watch"] = 1
                                            cand["seed_prior_weak_drift_watch_start_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["seed_prior_weak_drift_watch_reason"] = (
                                                seed_prior_weak_drift_watch_reason
                                            )
                                            cand["seed_prior_positive_refresh_watch"] = 1
                                            cand[
                                                "seed_prior_positive_refresh_watch_start_ms"
                                            ] = authority_now_ms
                                            cand[
                                                "seed_prior_positive_refresh_watch_reason"
                                            ] = authority_wait_reason
                                            cand["post_plan_rearm_required"] = 1
                                            cand["post_plan_rearm_wait_start_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["post_plan_rearm_wait_last_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["post_plan_rearm_base_lamports"] = int(
                                                cand.get("pre_entry_buy_lamports") or 0
                                            )
                                            cand["post_plan_rearm_base_buys"] = int(
                                                cand.get("pre_entry_buys") or 0
                                            )
                                            cand["no_movement_watch_keeps"] = int(
                                                cand.get("no_movement_watch_keeps", 0)
                                            ) + 1
                                            cand["no_movement_watch_deadline_ms"] = (
                                                authority_now_ms + watch_ms
                                            )
                                            cand["no_movement_watch_reason"] = str(
                                                continuation_reason or ""
                                            )
                                            cand[
                                                "no_movement_watch_start_pre_entry_lamports"
                                            ] = int(cand.get("pre_entry_buy_lamports") or 0)
                                            cand["no_movement_watch_start_buys"] = int(
                                                cand.get("pre_entry_buys") or 0
                                            )
                                            cand["no_movement_watch_ts_ms"] = (
                                                authority_now_ms
                                            )
                                            cand[
                                                "seed_prior_authority_postplan_waits"
                                            ] = authority_waits + 1
                                            counters[
                                                "seed_prior_weak_drift_watch_keep"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-WEAK-DRIFT-WATCH-KEEP "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"authority_reason={authority_wait_reason} "
                                                f"watch_reason={seed_prior_weak_drift_watch_reason} "
                                                f"drift_pct={float(final_refresh_drift_pct):+.3f} "
                                                f"quote_tokens={float(quote_tokens):.6f} "
                                                f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                f"wait_count={authority_waits + 1} "
                                                f"watch_ms={watch_ms} "
                                                "reason_detail=wait_for_fresh_buy_after_weak_positive_refresh"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                        if (
                                            authority_wait_enabled
                                            and _v287_is_selected_seed_prior(
                                                cand, continuation_reason
                                            )
                                            and (
                                                authority_wait_reason.startswith(
                                                    "missing_postplan_followthrough"
                                                )
                                                or authority_wait_reason
                                                == "positive_refresh_requires_post_final_followthrough"
                                                or authority_wait_reason
                                                == "watch_followthrough_quote_too_high"
                                            )
                                            and authority_waits < authority_max_waits
                                            and authority_age_ms <= authority_max_age_ms
                                        ):
                                            cand["post_plan_rearm_required"] = 1
                                            cand["post_plan_rearm_wait_start_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["post_plan_rearm_wait_last_ms"] = (
                                                authority_now_ms
                                            )
                                            cand["post_plan_rearm_base_lamports"] = int(
                                                cand.get("pre_entry_buy_lamports") or 0
                                            )
                                            cand["post_plan_rearm_base_buys"] = int(
                                                cand.get("pre_entry_buys") or 0
                                            )
                                            cand[
                                                "seed_prior_authority_postplan_waits"
                                            ] = authority_waits + 1
                                            if (
                                                authority_wait_reason
                                                == "positive_refresh_requires_post_final_followthrough"
                                            ):
                                                cand[
                                                    "seed_prior_positive_refresh_watch"
                                                ] = 1
                                                cand[
                                                    "seed_prior_positive_refresh_watch_start_ms"
                                                ] = authority_now_ms
                                                cand[
                                                    "seed_prior_positive_refresh_watch_reason"
                                                ] = authority_wait_reason
                                                cand["no_movement_watch_keeps"] = int(
                                                    cand.get("no_movement_watch_keeps", 0)
                                                ) + 1
                                                cand["no_movement_watch_deadline_ms"] = (
                                                    authority_now_ms
                                                    + int(
                                                        os.environ.get(
                                                            "V287_SELECTED_SEED_PRIOR_POS_REFRESH_WATCH_MS",
                                                            "650",
                                                        )
                                                    )
                                                )
                                                cand["no_movement_watch_reason"] = str(
                                                    continuation_reason or ""
                                                )
                                                cand[
                                                    "no_movement_watch_start_pre_entry_lamports"
                                                ] = int(
                                                    cand.get("pre_entry_buy_lamports")
                                                    or 0
                                                )
                                                cand["no_movement_watch_start_buys"] = int(
                                                    cand.get("pre_entry_buys") or 0
                                                )
                                                cand["no_movement_watch_ts_ms"] = (
                                                    authority_now_ms
                                                )
                                            counters[
                                                "seed_prior_final_authority_postplan_wait"
                                            ] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-AUTHORITY-WAIT-FOR-POSTPLAN "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"authority_reason={authority_wait_reason} "
                                                f"wait_count={authority_waits + 1} "
                                                f"max_waits={authority_max_waits} "
                                                f"age_ms={authority_age_ms} "
                                                f"max_age_ms={authority_max_age_ms} "
                                                f"base_buys={int(cand.get('post_plan_rearm_base_buys') or 0)} "
                                                f"base_sol={int(cand.get('post_plan_rearm_base_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                                f"positive_refresh_watch={int(cand.get('seed_prior_positive_refresh_watch') or 0)} "
                                                "reason_detail=do_not_delete_selected_seed_prior_before_fresh_postplan_buy_or_lower_quote"
                                            )
                                            hist[mint].append(rec)
                                            continue
                                        counters[
                                            "seed_prior_final_send_authority_block"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-FINAL-SEND-AUTHORITY-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_send_reason} "
                                            f"drift_pct={float(final_refresh_drift_pct):+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f} "
                                            "reason_detail=final_curve_movement_not_fresh_enough_for_negative_roundtrip"
                                        )
                                        active.pop(mint, None)
                                        hist[mint].append(rec)
                                        continue
                                    if _v287_is_selected_seed_prior(
                                        cand, continuation_reason
                                    ):
                                        counters[
                                            "seed_prior_final_send_authority_pass"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-FINAL-SEND-AUTHORITY-PASS "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"authority_reason={seed_prior_send_reason} "
                                            f"drift_pct={float(final_refresh_drift_pct):+.3f} "
                                            f"quote_tokens={float(quote_tokens):.6f}"
                                        )
                                if (
                                    continuation_reason
                                    == "selected_single_prior_no_movement_followthrough"
                                    and os.environ.get(
                                        "V287_DISABLE_SINGLE_PRIOR_NO_MOVEMENT_FOLLOWTHROUGH_SEND",
                                        "1",
                                    )
                                    != "0"
                                ):
                                    counters[
                                        "selected_single_prior_no_movement_followthrough_send_block"
                                    ] += 1
                                    _log(
                                        "PGG2-V287-SINGLE-PRIOR-NO-MOVEMENT-FOLLOWTHROUGH-SEND-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason} "
                                        "reason_detail=disable_unproven_fee_burn_sublane "
                                        "evidence=GUFh_custom_2006_buy_failed_safe"
                                    )
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                if not _v287_buy_quote_headroom_ok(
                                    mint=mint,
                                    cand=cand,
                                    reason=continuation_reason,
                                    quote_tokens=float(quote_tokens),
                                    min_quote_tokens=float(min_quote_tokens),
                                    source="fast_final_send_authority",
                                ):
                                    counters["buy_quote_headroom_block"] += 1
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                min_tokens_ui = _v287_buy_min_guard_tokens(
                                    mint=mint,
                                    reason=continuation_reason,
                                    quote_tokens=float(quote_tokens),
                                    min_quote_tokens=float(min_quote_tokens),
                                    buy_slippage_pct=float(args.buy_slippage_pct),
                                    source="fast_final_send_authority",
                                )
                                if (
                                    not plan_ready
                                    and os.environ.get(
                                        "V287_ALLOW_SNAPSHOT_COMPILE_FALLBACK_SEND",
                                        "0",
                                    )
                                    != "1"
                                ):
                                    if _v287_try_seed_prior_static_plan_sync_recover(
                                        broker=broker,
                                        mint=mint,
                                        cand=cand,
                                        reason=continuation_reason or "",
                                        amount_sol=float(args.size_sol),
                                        counters=counters,
                                        source="plan_not_ready_before_send",
                                        fallback_rec=rec,
                                    ):
                                        plan_ready = True
                                        counters[
                                            "seed_prior_static_plan_sync_recover_send_ready"
                                        ] += 1
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-STATIC-PLAN-RECOVER-SEND-READY "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            "source=plan_not_ready_before_send"
                                        )
                                    else:
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
                                    def _fast_buy_quote_from_current_curve() -> dict[str, Any]:
                                        return broker.build_fast_signed_buy_with_min_tokens_from_curve_snapshot(
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

                                    try:
                                        buy_quote = _fast_buy_quote_from_current_curve()
                                    except RuntimeError as exc:
                                        stale_plan_err = str(exc)
                                        if stale_plan_err not in (
                                            "v165_precompiled_creator_missing",
                                            "v165_precompiled_creator_mismatch",
                                            "v165_precompiled_buy_plan_missing",
                                        ):
                                            raise
                                        counters["fast_static_plan_stale_rebuild"] += 1
                                        _log(
                                            "PGG2-V287-FAST-STATIC-PLAN-STALE-REBUILD "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"err={stale_plan_err} source=latest_feed_rec"
                                        )
                                        _drop_static_buy_plan_for_mint_size(
                                            broker,
                                            mint,
                                            float(args.size_sol),
                                            stale_plan_err,
                                        )
                                        if not _prepare_static_buy_plan_from_feed_rec(
                                            broker,
                                            rec,
                                            float(args.size_sol),
                                        ):
                                            counters["fast_static_plan_rebuild_block"] += 1
                                            _log(
                                                "PGG2-V287-FAST-STATIC-PLAN-REBUILD-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"err={stale_plan_err} "
                                                "reason=latest_feed_rec_could_not_rebuild"
                                            )
                                            active.pop(mint, None)
                                            continue
                                        buy_quote = _fast_buy_quote_from_current_curve()
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
                                        creator=str(getattr(curve, "creator", "")),
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
                                if not _validate_pump_buy_account_indexes(
                                    broker,
                                    mint,
                                    str(buy_quote["txn"]),
                                    "fast_final_curve",
                                    getattr(curve, "creator", ""),
                                ):
                                    counters["creator_vault_block"] += 1
                                    _log(
                                        "PGG2-V287-CREATOR-VAULT-MISMATCH-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} source=fast_final_curve"
                                    )
                                    active.pop(mint, None)
                                    continue
                                if not _v287_seed_prior_only_send_allowed(
                                    mint=mint,
                                    cand=cand,
                                    reason=continuation_reason
                                    or pre_projection_selected_reason
                                    or "",
                                    counters=counters,
                                    source="fast_static_pre_send",
                                ):
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                if not _validate_pump_buy_account_indexes(
                                    broker,
                                    mint,
                                    str(buy_quote["txn"]),
                                    "fast_static_final_presend",
                                    getattr(curve, "creator", ""),
                                ):
                                    counters["creator_vault_block"] += 1
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                fingerprint_ok = _validate_pump_buy_candidate_fingerprint(
                                    broker=broker,
                                    mint=mint,
                                    txn_b64=str(buy_quote["txn"]),
                                    cand=cand,
                                    source="fast_static_final_presend",
                                )
                                if not fingerprint_ok and _v287_try_seed_prior_static_plan_sync_recover(
                                    broker=broker,
                                    mint=mint,
                                    cand=cand,
                                    reason=continuation_reason
                                    or pre_projection_selected_reason
                                    or "",
                                    amount_sol=float(args.size_sol),
                                    counters=counters,
                                    source="fingerprint_mismatch_before_send",
                                    fallback_rec=rec,
                                ):
                                    try:
                                        rebuild_start_ms = _now_ms()
                                        buy_quote = _fast_buy_quote_from_current_curve()
                                        build_ms = _now_ms() - rebuild_start_ms
                                        buy_quote_source = str(
                                            buy_quote.get("quote_source") or ""
                                        )
                                        buy_snapshot_ts_ms = int(
                                            buy_quote.get("snapshot_ts_ms")
                                            or curve_ts_ms
                                            or 0
                                        )
                                        buy_snapshot_age_ms = (
                                            max(0, _now_ms() - buy_snapshot_ts_ms)
                                            if buy_snapshot_ts_ms > 0
                                            else 999999
                                        )
                                        fingerprint_ok = (
                                            buy_quote_source
                                            == "v165_fast_precompiled_snapshot"
                                            and buy_snapshot_age_ms
                                            <= max_buy_snapshot_age_ms
                                            and build_ms <= max_fast_buy_build_ms
                                            and buy_quote.get("route") == "pump_bc"
                                            and _validate_pump_buy_account_indexes(
                                                broker,
                                                mint,
                                                str(buy_quote["txn"]),
                                                "fast_static_fingerprint_recover",
                                                getattr(curve, "creator", ""),
                                            )
                                            and _validate_pump_buy_candidate_fingerprint(
                                                broker=broker,
                                                mint=mint,
                                                txn_b64=str(buy_quote["txn"]),
                                                cand=cand,
                                                source=(
                                                    "fast_static_fingerprint_recover"
                                                ),
                                            )
                                        )
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-FINGERPRINT-RECOVER-CHECK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"pass={int(fingerprint_ok)} "
                                            f"source={buy_quote_source or '-'} "
                                            f"snapshot_age_ms={buy_snapshot_age_ms} "
                                            f"build_ms={build_ms}"
                                        )
                                    except Exception as exc:
                                        fingerprint_ok = False
                                        _log(
                                            "PGG2-V287-SEED-PRIOR-FINGERPRINT-RECOVER-BLOCK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"err={type(exc).__name__}:{str(exc)[:120]}"
                                        )
                                if not fingerprint_ok:
                                    counters["account_fingerprint_block"] += 1
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                live_creator_vault_ok = _validate_pump_buy_live_creator_vault(
                                    broker=broker,
                                    mint=mint,
                                    txn_b64=str(buy_quote["txn"]),
                                    rpc_url=str(args.rpc_url),
                                    source="fast_static_final_presend",
                                    cand=cand,
                                )
                                if not live_creator_vault_ok:
                                    counters["live_creator_vault_block"] += 1
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                                if not _v287_single_lane_firewall_ok(
                                    mint=mint,
                                    cand=cand,
                                    reason=continuation_reason or "",
                                    counters=counters,
                                    source="fast_static_final_presend",
                                ):
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
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
                            base_min_quote_tokens = float(args.min_buy_quote_tokens)
                            if (
                                os.environ.get(
                                    "V287_SELECTED_FRESH_ACTUAL_ENABLED",
                                    "0",
                                )
                                != "1"
                                and not _v287_selected_fresh_actual_enabled(
                                    continuation_reason
                                )
                                and str(cand.get("top_lane") or "")
                                == "fresh_impulse"
                                and float(cand.get("prev_buy_sol") or 0.0)
                                <= 1e-12
                                and not bool(cand.get("fresh_clean_carry_reclass"))
                            ):
                                fresh_clean_ready, fresh_clean_reason = (
                                    _v287_fresh_clean_carry_reclass_ready(cand, _now_ms())
                                )
                                if fresh_clean_ready:
                                    continuation_reason = "selected_fresh_clean_carry_reclass"
                                    cand["fresh_clean_carry_reclass"] = 1
                                    counters["fresh_clean_carry_reclass_pass"] += 1
                                    _log(
                                        "PGG2-V287-FRESH-CLEAN-CARRY-RECLASS-PASS "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                        f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                        f"age_ms={_now_ms()-int(cand.get('start_ms') or _now_ms())} "
                                        f"reason={fresh_clean_reason} source=fallback_buy_path"
                                    )
                                elif _v287_fresh_actual_should_wait(cand, _now_ms()):
                                    counters["fresh_clean_carry_watch_keep"] += 1
                                    _log(
                                        "PGG2-V287-FRESH-CLEAN-CARRY-WATCH-KEEP "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                        f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                        f"blocker={fresh_clean_reason} source=fallback_buy_path"
                                    )
                                    hist[mint].append(rec)
                                    continue
                                else:
                                    counters[
                                        "selected_fresh_actual_shadow_only_block"
                                    ] += 1
                                    _log(
                                        "PGG2-V287-SELECTED-FRESH-ACTUAL-BLOCK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"current_buy_sol={float(cand.get('current_buy_sol') or 0.0):.6f} "
                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                        f"pre_entry_buy_sol={int(cand.get('pre_entry_buy_lamports') or 0)/LAMPORTS_PER_SOL:.6f} "
                                        f"reason=fresh_no_prior_shadow_only blocker={fresh_clean_reason} source=fallback_buy_path"
                                    )
                                    active.pop(mint, None)
                                    hist[mint].append(rec)
                                    continue
                            min_quote_tokens = _v287_reason_min_quote_tokens(
                                continuation_reason,
                                base_min_quote_tokens,
                            )
                            if min_quote_tokens > base_min_quote_tokens + 1e-9:
                                counters["selected_reason_token_floor_raise"] += 1
                                _log(
                                    "PGG2-V287-SELECTED-REASON-TOKEN-FLOOR "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"reason={continuation_reason} "
                                    f"base_min_tokens={base_min_quote_tokens:.6f} "
                                    f"reason_min_tokens={min_quote_tokens:.6f}"
                                )
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
                            max_quote_tokens = _v287_reason_max_quote_tokens(
                                continuation_reason
                            )
                            if max_quote_tokens > 0:
                                cap_pass = quote_tokens <= max_quote_tokens
                                _log(
                                    "PGG2-V287-FINAL-BUY-QUOTE-TOKEN-CAP-CHECK "
                                    f"mint={_short(mint)} full_mint={mint} "
                                    f"reason={continuation_reason} "
                                    f"amount_out_tokens={quote_tokens:.6f} "
                                    f"max_tokens={max_quote_tokens:.6f} "
                                    f"pass={int(cap_pass)} source=fallback_buy_path"
                                )
                                if not cap_pass:
                                    if _v287_seed_prior_clean_cap_override_ok(
                                        cand,
                                        continuation_reason,
                                        quote_tokens,
                                        max_quote_tokens,
                                    ):
                                        counters["seed_prior_token_cap_override"] += 1
                                        _v287_log_seed_prior_cap_override(
                                            mint=mint,
                                            cand=cand,
                                            reason=continuation_reason,
                                            quote_tokens=quote_tokens,
                                            max_quote_tokens=max_quote_tokens,
                                            source="fallback_buy_path",
                                        )
                                    elif _v287_seed_prior_hot_high_cap_bypass_ok(
                                        cand,
                                        continuation_reason,
                                        quote_tokens=quote_tokens,
                                        max_quote_tokens=max_quote_tokens,
                                        source="fallback_buy_path",
                                        mint=mint,
                                        counters=counters,
                                    ):
                                        pass
                                    else:
                                        counters["buy_quote_token_cap_block"] += 1
                                        active.pop(mint, None)
                                        continue
                            if not _validate_pump_buy_account_indexes(
                                broker,
                                mint,
                                str(buy_quote["txn"]),
                                "fallback_buy_path",
                            ):
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
                                max_quote_tokens = _v287_reason_max_quote_tokens(
                                    continuation_reason
                                )
                                if max_quote_tokens > 0:
                                    cap_pass = quote_tokens <= max_quote_tokens
                                    _log(
                                        "PGG2-V287-FINAL-BUY-QUOTE-TOKEN-CAP-CHECK "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason} "
                                        f"amount_out_tokens={quote_tokens:.6f} "
                                        f"max_tokens={max_quote_tokens:.6f} "
                                        f"pass={int(cap_pass)} source=fallback_buy_rebuild"
                                    )
                                    if not cap_pass:
                                        if _v287_seed_prior_clean_cap_override_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens,
                                            max_quote_tokens,
                                        ):
                                            counters["seed_prior_token_cap_rebuild_override"] += 1
                                            _v287_log_seed_prior_cap_override(
                                                mint=mint,
                                                cand=cand,
                                                reason=continuation_reason,
                                                quote_tokens=quote_tokens,
                                                max_quote_tokens=max_quote_tokens,
                                                source="fallback_buy_rebuild",
                                            )
                                        elif _v287_seed_prior_hot_high_cap_bypass_ok(
                                            cand,
                                            continuation_reason,
                                            quote_tokens=quote_tokens,
                                            max_quote_tokens=max_quote_tokens,
                                            source="fallback_buy_rebuild",
                                            mint=mint,
                                            counters=counters,
                                        ):
                                            pass
                                        else:
                                            counters["buy_quote_token_cap_rebuild_block"] += 1
                                            active.pop(mint, None)
                                            continue
                                if not _validate_pump_buy_account_indexes(
                                    broker,
                                    mint,
                                    str(buy_quote["txn"]),
                                    "fallback_buy_rebuild",
                                ):
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
                                if _v287_seed_prior_projection_bypass_ok(
                                    cand, continuation_reason, quote_tokens
                                ):
                                    counters["seed_prior_projection_bypass"] += 1
                                    _log(
                                        "PGG2-V287-SEED-PRIOR-PROJECTION-BYPASS "
                                        f"mint={_short(mint)} full_mint={mint} "
                                        f"reason={continuation_reason} "
                                        f"quote_tokens={float(quote_tokens):.6f} "
                                        f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                        f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                        "source=fallback_buy_path"
                                    )
                                else:
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
                                        not _validate_pump_buy_account_indexes(
                                            broker,
                                            mint,
                                            str(boundary_quote["txn"]),
                                            "send_boundary_requote",
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
                                            not _validate_pump_buy_account_indexes(
                                                broker,
                                                mint,
                                                str(boundary_quote["txn"]),
                                                "send_boundary_rebuild",
                                            )
                                        ):
                                            counters["boundary_creator_vault_block"] += 1
                                            _log(
                                                "PGG2-V287-SEND-BOUNDARY-CREATOR-VAULT-BLOCK "
                                                f"mint={_short(mint)} full_mint={mint}"
                                            )
                                            active.pop(mint, None)
                                            continue
                                    max_quote_tokens = _v287_reason_max_quote_tokens(
                                        continuation_reason
                                    )
                                    if max_quote_tokens > 0:
                                        cap_pass = boundary_tokens <= max_quote_tokens
                                        _log(
                                            "PGG2-V287-FINAL-BUY-QUOTE-TOKEN-CAP-CHECK "
                                            f"mint={_short(mint)} full_mint={mint} "
                                            f"reason={continuation_reason} "
                                            f"amount_out_tokens={boundary_tokens:.6f} "
                                            f"max_tokens={max_quote_tokens:.6f} "
                                            f"pass={int(cap_pass)} source=send_boundary_requote"
                                        )
                                        if not cap_pass:
                                            if _v287_seed_prior_clean_cap_override_ok(
                                                cand,
                                                continuation_reason,
                                                boundary_tokens,
                                                max_quote_tokens,
                                            ):
                                                counters["seed_prior_token_cap_boundary_override"] += 1
                                                _v287_log_seed_prior_cap_override(
                                                    mint=mint,
                                                    cand=cand,
                                                    reason=continuation_reason,
                                                    quote_tokens=boundary_tokens,
                                                    max_quote_tokens=max_quote_tokens,
                                                    source="send_boundary_requote",
                                                )
                                            elif _v287_seed_prior_hot_high_cap_bypass_ok(
                                                cand,
                                                continuation_reason,
                                                quote_tokens=boundary_tokens,
                                                max_quote_tokens=max_quote_tokens,
                                                source="send_boundary_requote",
                                                mint=mint,
                                                counters=counters,
                                            ):
                                                pass
                                            else:
                                                counters["buy_quote_token_cap_boundary_block"] += 1
                                                active.pop(mint, None)
                                                continue
                                    if not _prebuy_postbuy_sell_projection_pass(
                                        broker, mint, boundary_quote, wallet_before_buy, args
                                    ):
                                        if _v287_seed_prior_projection_bypass_ok(
                                            cand, continuation_reason, boundary_tokens
                                        ):
                                            counters["seed_prior_boundary_projection_bypass"] += 1
                                            _log(
                                                "PGG2-V287-SEED-PRIOR-PROJECTION-BYPASS "
                                                f"mint={_short(mint)} full_mint={mint} "
                                                f"reason={continuation_reason} "
                                                f"quote_tokens={float(boundary_tokens):.6f} "
                                                f"pre_entry_buys={int(cand.get('pre_entry_buys') or 0)} "
                                                f"pre_entry_buy_sol={_v287_cand_pre_entry_sol(cand):.6f} "
                                                "source=send_boundary_requote"
                                            )
                                        else:
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
                            if not _v287_buy_quote_headroom_ok(
                                mint=mint,
                                cand=cand,
                                reason=continuation_reason,
                                quote_tokens=float(quote_tokens),
                                min_quote_tokens=float(min_quote_tokens),
                                source="fallback_send_authority",
                            ):
                                counters["buy_quote_headroom_block"] += 1
                                active.pop(mint, None)
                                hist[mint].append(rec)
                                continue
                            if not _v287_seed_prior_only_send_allowed(
                                mint=mint,
                                cand=cand,
                                reason=continuation_reason
                                or pre_projection_selected_reason
                                or "",
                                counters=counters,
                                source="fallback_pre_send",
                            ):
                                active.pop(mint, None)
                                hist[mint].append(rec)
                                continue
                            if not _validate_pump_buy_account_indexes(
                                broker,
                                mint,
                                str(buy_quote["txn"]),
                                "fallback_final_presend",
                            ):
                                counters["creator_vault_block"] += 1
                                active.pop(mint, None)
                                hist[mint].append(rec)
                                continue
                            if not _validate_pump_buy_live_creator_vault(
                                broker=broker,
                                mint=mint,
                                txn_b64=str(buy_quote["txn"]),
                                rpc_url=str(args.rpc_url),
                                source="fallback_final_presend",
                                cand=cand,
                            ):
                                counters["live_creator_vault_block"] += 1
                                active.pop(mint, None)
                                hist[mint].append(rec)
                                continue
                            if not _v287_single_lane_firewall_ok(
                                mint=mint,
                                cand=cand,
                                reason=continuation_reason or "",
                                counters=counters,
                                source="fallback_final_presend",
                            ):
                                active.pop(mint, None)
                                hist[mint].append(rec)
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

            if rec["kind"] != "buy":
                hist[mint].append(rec)
                continue
            rec_sol_lamports = int(rec["sol_lamports"])
            max_event_buy_lamports = int(
                float(os.environ.get("V287_EVENT_BUY_MAX_SOL_SANITY", "20.00"))
                * LAMPORTS_PER_SOL
            )
            if rec_sol_lamports <= 0 or rec_sol_lamports > max_event_buy_lamports:
                counters["event_buy_sanity_block"] += 1
                _log(
                    "PGG2-V287-EVENT-BUY-SANITY-BLOCK "
                    f"mint={_short(mint)} full_mint={mint} "
                    f"sol_lamports={rec_sol_lamports} "
                    f"max_lamports={max_event_buy_lamports} "
                    "source=feed_decode_guard"
                )
                continue
            hist[mint].append(rec)
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
            current_ok_normal_top = (
                len(prev_buys) >= 3
                and float(os.environ.get("V287_NORMAL_TOP_CURRENT_MIN_SOL", "2.00"))
                <= current_buy_sol
                <= float(os.environ.get("V287_NORMAL_TOP_CURRENT_MAX_SOL", "3.25"))
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
            if not (
                current_ok_main
                or current_ok_normal_top
                or current_ok_single_prior
                or current_ok_two_prior
            ):
                seed_prior_carry_ok = (
                    os.environ.get("V287_ENABLE_SEED_PRIOR_CARRY_LANE", "1")
                    != "0"
                    and mint not in active
                    and len(active) < int(args.max_active_candidates)
                    and len(prev_buys) == 0
                    and len(prev_sells) == 0
                    and float(os.environ.get("V287_SEED_PRIOR_CARRY_CURRENT_MIN_SOL", "2.00"))
                    <= current_buy_sol
                    <= float(os.environ.get("V287_SEED_PRIOR_CARRY_CURRENT_MAX_SOL", "2.80"))
                    and top_share >= 0.999
                )
                if seed_prior_carry_ok:
                    top_lane = "seed_prior_carry_continuation"
                    rearm_min_lamports = int(
                        float(os.environ.get("V287_SEED_PRIOR_CARRY_REARM_MIN_SOL", "0.70"))
                        * LAMPORTS_PER_SOL
                    )
                    rearm_max_lamports = int(
                        float(os.environ.get("V287_SEED_PRIOR_CARRY_REARM_MAX_SOL", "6.50"))
                        * LAMPORTS_PER_SOL
                    )
                    candidate_pair_ok = _remember_pair_from_feed_rec(broker, rec)
                    active[mint] = {
                        "mint": mint,
                        "start_ms": now,
                        "candidate_sig": sig,
                        "candidate_pair_ok": candidate_pair_ok,
                        "candidate_static_account_fp": _v287_apply_live_creator_vault_to_fp(
                            broker,
                            mint,
                            _feed_account_fingerprint(rec),
                            source="candidate_start_fingerprint",
                        ),
                        "candidate_static_account_rec": dict(rec),
                        "latest_static_account_rec": dict(rec),
                        "current_buy_sol": current_buy_sol,
                        "prev_buy_sol": prev_buy_sol,
                        "prev_buys": len(prev_buys),
                        "top_share": top_share,
                        "top_lane": top_lane,
                        "rearm_min_lamports": rearm_min_lamports,
                        "rearm_max_lamports": rearm_max_lamports,
                        "candidate_ttl_ms": int(
                            os.environ.get("V287_SEED_PRIOR_CARRY_TTL_MS", "1350")
                        ),
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
                    counters["seed_prior_carry_candidate_start"] += 1
                    counters["candidate_start"] += 1
                    _shadow_start(
                        shadows,
                        counters,
                        reason="seed_prior_carry_candidate_watch",
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
                        "PGG2-V287-SEED-PRIOR-CARRY-CANDIDATE "
                        f"mint={_short(mint)} full_mint={mint} "
                        f"current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                        f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"ttl_ms={_candidate_live_ttl_ms(active[mint])} "
                        "source=current_band_seed_prior_blindness_repair"
                    )
                    continue
                if _v287_seed_prior_only_live_mode():
                    counters["seed_prior_only_non_seed_candidate_block"] += 1
                    if os.environ.get("V287_SEED_PRIOR_ONLY_SHADOW_BLOCKS", "0") != "0":
                        _shadow_start(
                            shadows,
                            counters,
                            reason="seed_prior_only_non_seed_current_band",
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
                    _log(
                        "PGG2-V287-SEED-PRIOR-ONLY-CANDIDATE-BLOCK "
                        f"mint={_short(mint)} full_mint={mint} "
                        f"current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                        "allowed_top_lane=seed_prior_carry_continuation "
                        "source=current_band_non_seed"
                    )
                    continue
                high_current_train_ok = (
                    os.environ.get("V287_ENABLE_HIGH_CURRENT_CLEAN_TRAIN_LANE", "1")
                    != "0"
                    and mint not in active
                    and len(active) < int(args.max_active_candidates)
                    and len(prev_sells) == 0
                    and float(os.environ.get("V287_HIGH_CURRENT_TRAIN_CURRENT_MIN_SOL", "3.30"))
                    <= current_buy_sol
                    <= float(os.environ.get("V287_HIGH_CURRENT_TRAIN_CURRENT_MAX_SOL", "6.00"))
                    and prev_buy_sol
                    <= float(os.environ.get("V287_HIGH_CURRENT_TRAIN_PREV_MAX_SOL", "4.50"))
                    and top_share
                    >= float(os.environ.get("V287_HIGH_CURRENT_TRAIN_TOP_MIN", "0.95"))
                )
                if high_current_train_ok:
                    top_lane = "high_current_clean_train"
                    rearm_min_lamports = int(
                        float(os.environ.get("V287_HIGH_CURRENT_TRAIN_REARM_MIN_SOL", "3.00"))
                        * LAMPORTS_PER_SOL
                    )
                    rearm_max_lamports = int(
                        float(os.environ.get("V287_HIGH_CURRENT_TRAIN_REARM_MAX_SOL", "12.00"))
                        * LAMPORTS_PER_SOL
                    )
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
                        "rearm_min_lamports": rearm_min_lamports,
                        "rearm_max_lamports": rearm_max_lamports,
                        "candidate_ttl_ms": int(
                            os.environ.get("V287_HIGH_CURRENT_TRAIN_TTL_MS", "1000")
                        ),
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
                    counters["high_current_train_candidate_start"] += 1
                    counters["candidate_start"] += 1
                    _shadow_start(
                        shadows,
                        counters,
                        reason="high_current_train_candidate_watch",
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
                        "PGG2-V287-HIGH-CURRENT-TRAIN-CANDIDATE "
                        f"mint={_short(mint)} full_mint={mint} "
                        f"current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                        f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                        "source=current_band_shadow_miss_repair"
                    )
                    continue
                counters["block_current_band"] += 1
                if os.environ.get("V287_SHADOW_CURRENT_BAND_REJECTS", "1") != "0":
                    _shadow_start(
                        shadows,
                        counters,
                        reason="current_band",
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
            if _v287_seed_prior_only_live_mode():
                counters["seed_prior_only_non_seed_candidate_block"] += 1
                if os.environ.get("V287_SEED_PRIOR_ONLY_SHADOW_BLOCKS", "0") != "0":
                    _shadow_start(
                        shadows,
                        counters,
                        reason="seed_prior_only_non_seed_current_ok",
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
                _log(
                    "PGG2-V287-SEED-PRIOR-ONLY-CANDIDATE-BLOCK "
                    f"mint={_short(mint)} full_mint={mint} "
                    f"current_buy_sol={current_buy_sol:.6f} "
                    f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                    f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                    "allowed_top_lane=seed_prior_carry_continuation "
                    "source=current_ok_non_seed"
                )
                continue
            if len(prev_buys) < 3:
                single_prior_ok = (
                    bool(args.enable_single_prior_buy_lane)
                    and mint not in active
                    and len(active) < int(args.max_active_candidates)
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
                    and mint not in active
                    and len(active) < int(args.max_active_candidates)
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
                dust_prior_ok = (
                    os.environ.get("V287_ENABLE_DUST_PRIOR_CONTINUATION_LANE", "1")
                    != "0"
                    and mint not in active
                    and len(active) < int(args.max_active_candidates)
                    and len(prev_buys) == 1
                    and len(prev_sells) == 0
                    and float(os.environ.get("V287_DUST_PRIOR_CURRENT_MIN_SOL", "2.00"))
                    <= current_buy_sol
                    <= float(os.environ.get("V287_DUST_PRIOR_CURRENT_MAX_SOL", "3.25"))
                    and 0.0
                    <= prev_buy_sol
                    <= float(os.environ.get("V287_DUST_PRIOR_PREV_MAX_SOL", "0.50"))
                    and top_share
                    >= float(os.environ.get("V287_DUST_PRIOR_TOP_MIN", "0.95"))
                )
                if dust_prior_ok:
                    top_lane = "dust_prior_clean_continuation"
                    rearm_min_lamports = int(
                        float(os.environ.get("V287_DUST_PRIOR_REARM_MIN_SOL", "0.35"))
                        * LAMPORTS_PER_SOL
                    )
                    rearm_max_lamports = int(
                        float(os.environ.get("V287_DUST_PRIOR_REARM_MAX_SOL", "2.00"))
                        * LAMPORTS_PER_SOL
                    )
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
                        "rearm_min_lamports": rearm_min_lamports,
                        "rearm_max_lamports": rearm_max_lamports,
                        "candidate_ttl_ms": int(
                            os.environ.get("V287_DUST_PRIOR_CANDIDATE_TTL_MS", "1350")
                        ),
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
                    counters["dust_prior_candidate_start"] += 1
                    counters["candidate_start"] += 1
                    _shadow_start(
                        shadows,
                        counters,
                        reason="dust_prior_candidate_watch",
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
                        "PGG2-V287-DUST-PRIOR-CANDIDATE "
                        f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                        f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                        f"prev_sells_1s={len(prev_sells)} top_share_1s={top_share:.4f} "
                        f"rearm_min_sol={rearm_min_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"rearm_max_sol={rearm_max_lamports/LAMPORTS_PER_SOL:.6f} "
                        f"ttl_ms={_candidate_live_ttl_ms(active[mint])} "
                        "source=last_smoke_prev_buys_frequency_repair"
                    )
                    continue
                fresh_impulse_ok = (
                    bool(args.enable_fresh_impulse_lane)
                    and mint not in active
                    and len(active) < int(args.max_active_candidates)
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
                        os.environ.get("V287_FRESH_IMPULSE_ZERO_PREV_MIN_REARM_SOL", "1.35")
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
            edge_top_ok = (
                os.environ.get("V287_EDGE_TOP_ENABLED", "1") != "0"
                and len(prev_buys) >= 3
                and float(os.environ.get("V287_EDGE_TOP_MIN_SHARE", "0.50"))
                <= top_share
                < float(args.top_share_min)
                and float(os.environ.get("V287_EDGE_TOP_PREV_MIN_SOL", "7.50"))
                <= prev_buy_sol
                <= float(os.environ.get("V287_EDGE_TOP_PREV_MAX_SOL", "9.60"))
            )
            if not (float(args.top_share_min) <= top_share <= float(args.top_share_max)):
                if not edge_top_ok:
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
                counters["edge_top_candidate_start"] += 1
                _log(
                    "PGG2-V287-EDGE-TOP-CANDIDATE "
                    f"mint={_short(mint)} full_mint={mint} current_buy_sol={current_buy_sol:.6f} "
                    f"prev_buys_1s={len(prev_buys)} prev_buy_sol_1s={prev_buy_sol:.6f} "
                    f"top_share_1s={top_share:.4f} "
                    f"top_min={float(args.top_share_min):.4f} "
                    "source=near_top_shadow_clean_bucket"
                )
            if edge_top_ok:
                top_lane = "edge_top_strong_prior"
                rearm_min_lamports = int(
                    float(os.environ.get("V287_EDGE_TOP_REARM_MIN_SOL", "1.80"))
                    * LAMPORTS_PER_SOL
                )
                rearm_max_lamports = int(
                    float(os.environ.get("V287_EDGE_TOP_REARM_MAX_SOL", "3.20"))
                    * LAMPORTS_PER_SOL
                )
            elif top_share < float(args.top_share_normal_min):
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
            if mint in active or len(active) >= int(args.max_active_candidates):
                counters["block_active_exists"] += 1
                _shadow_start(
                    shadows,
                    counters,
                    reason="active_exists" if mint in active else "active_capacity",
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
