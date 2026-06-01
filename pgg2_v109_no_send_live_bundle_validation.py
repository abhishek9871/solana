#!/usr/bin/env python3
"""V109 live-opportunity no-send bundle validation.

Listens to the raw UDP shred feed, decodes pump.fun buys, and tries to build
the exact V108 atomic bundle before the external transaction lands. It never
sends the bundle.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from pgg2_v108_external_tx_decoder import decode_external_pump_buy  # type: ignore
from pgg2_v108_bundle_profit_model import select_best_size  # type: ignore
from pgg2_v108_bundle_builder import build_bundle_plan_no_send, plan_to_json  # type: ignore
from pgg2_v108_jito_bundle_sender import get_tip_accounts, send_bundle  # type: ignore


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_ALT_ACCOUNT_CACHE: dict[str, list[Any]] = {}


def _bind_low_latency_shred_listener(port: int) -> Any:
    """Bind ShredStream with lowest-latency transaction delivery settings."""
    from shredstream import AccumulatorConfig, ListenerOptions, ShredListener  # type: ignore

    options = ListenerOptions(
        recv_buf=int(os.environ.get("V109_SHRED_RECV_BUF", str(128 * 1024 * 1024))),
        max_age=int(os.environ.get("V109_SHRED_MAX_AGE", "1")),
        busy_poll_us=int(os.environ.get("V109_SHRED_BUSY_POLL_US", "1000")),
        pool_size=int(os.environ.get("V109_SHRED_POOL_SIZE", "8192")),
        enable_fec=os.environ.get("V109_SHRED_ENABLE_FEC", "0").lower() in {"1", "true", "yes"},
        disable_salvage_delivery=os.environ.get("V109_SHRED_DISABLE_SALVAGE", "1").lower() in {"1", "true", "yes"},
        accumulator=AccumulatorConfig(
            max_fec_sets_per_slot=int(os.environ.get("V109_SHRED_MAX_FEC_SETS", "8")),
            stuck_batch_timeout_ms=int(os.environ.get("V109_SHRED_STUCK_BATCH_TIMEOUT_MS", "1")),
        ),
    )
    listener = ShredListener.bind_with_options(int(port), options)
    _log(f"PGG2-V109-SHRED-LOWLATENCY-BIND port={port} options={options!r}")
    return listener


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _load_env() -> None:
    p = Path("/root/piggy/.env")
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _rpc_url() -> str:
    api = os.environ.get("HELIUS_API_KEY", "")
    return (
        os.environ.get("V109_READ_RPC_URL")
        or os.environ.get("PGG2_READ_RPC_URL")
        or os.environ.get("HELIUS_RPC_URL")
        or (f"https://mainnet.helius-rpc.com/?api-key={api}" if api else "")
        or os.environ.get("SOLANA_RPC_URL", "")
        or "https://api.mainnet-beta.solana.com"
    )


def _rpc_urls() -> list[str]:
    out: list[str] = []

    def add(url: str | None) -> None:
        clean = str(url or "").strip()
        if clean and clean not in out:
            out.append(clean)

    add(os.environ.get("V109_READ_RPC_URL"))
    add(os.environ.get("PGG2_READ_RPC_URL"))
    add(os.environ.get("HELIUS_RPC_URL"))
    api = os.environ.get("HELIUS_API_KEY", "")
    if api:
        add(f"https://mainnet.helius-rpc.com/?api-key={api}")
    add(os.environ.get("SOLANATRACKER_RPC_HTTP"))
    add(os.environ.get("RPCFAST_HTTP_URL"))
    add(os.environ.get("RPCFAST_RPC_URL"))
    rpcfast_key = os.environ.get("RPCFAST_API_KEY", "")
    if rpcfast_key:
        add(f"https://solana-rpc.rpcfast.com/?api_key={rpcfast_key}")
    add(os.environ.get("SHYFT_RPC_HTTP"))
    shyft_key = os.environ.get("SHYFT_API_KEY", "")
    if shyft_key:
        add(f"https://rpc.shyft.to?api_key={shyft_key}")
    add(os.environ.get("SOLANA_RPC_URL"))
    add("https://api.mainnet-beta.solana.com")
    return out


def _rpc_call(method: str, params: list[Any], timeout: float = 4.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    last_exc: Exception | None = None
    for url in _rpc_urls():
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            if parsed.get("error"):
                raise RuntimeError(parsed["error"])
            return parsed.get("result")
        except Exception as exc:
            last_exc = exc
            status = getattr(exc, "code", "")
            msg = str(exc)
            if status not in {403, 429} and "max_usage" not in msg and "credits" not in msg.lower():
                # Try the next endpoint for transient network/provider errors,
                # but avoid noisy logs on the hot path.
                continue
            continue
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("rpc_call_no_endpoint")


def _sig_status(sig: str) -> Optional[dict[str, Any]]:
    try:
        result = _rpc_call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
        vals = (result or {}).get("value") or []
        return vals[0] if vals else None
    except Exception:
        return None


def _address_lookup_table_addresses(table_key: str) -> list[Any]:
    """Fetch and decode a Solana address lookup table account.

    Raw SOF transactions frequently use v0 address lookup tables. The account
    indices inside compiled instructions are against the fully-resolved key
    list, not only tx.message.account_keys. Without resolving ALTs we miss the
    Pump buyback/social-fee accounts and incorrectly block bundle candidates.
    """
    cached = _ALT_ACCOUNT_CACHE.get(table_key)
    if cached is not None:
        return cached
    from solders.pubkey import Pubkey  # type: ignore

    result = _rpc_call(
        "getAccountInfo",
        [table_key, {"encoding": "base64", "commitment": "processed"}],
        timeout=2.0,
    )
    value = (result or {}).get("value")
    if not value:
        raise RuntimeError("alt_account_not_found")
    data_field = value.get("data") or []
    if not data_field or not isinstance(data_field, list):
        raise RuntimeError("alt_account_data_missing")
    raw = base64.b64decode(str(data_field[0]))
    # Solana lookup table account layout has a 56-byte metadata prefix followed
    # by packed 32-byte addresses.
    if len(raw) < 56 or (len(raw) - 56) % 32 != 0:
        raise RuntimeError(f"alt_account_bad_len:{len(raw)}")
    addresses = [
        Pubkey.from_bytes(raw[i : i + 32])
        for i in range(56, len(raw), 32)
    ]
    _ALT_ACCOUNT_CACHE[table_key] = addresses
    return addresses


def _resolved_tx_keys(tx: Any) -> tuple[list[Any], int]:
    """Return message keys resolved with v0 address lookup tables."""
    msg = tx.message
    keys = list(msg.account_keys)
    lookups = list(getattr(msg, "address_table_lookups", []) or [])
    if not lookups:
        return keys, 0
    writable: list[Any] = []
    readonly: list[Any] = []
    for lookup in lookups:
        table_key = str(lookup.account_key)
        table_addresses = _address_lookup_table_addresses(table_key)
        for raw_idx in list(lookup.writable_indexes):
            idx = int(raw_idx)
            if idx >= len(table_addresses):
                raise RuntimeError(f"alt_writable_index_oob:{table_key}:{idx}")
            writable.append(table_addresses[idx])
        for raw_idx in list(lookup.readonly_indexes):
            idx = int(raw_idx)
            if idx >= len(table_addresses):
                raise RuntimeError(f"alt_readonly_index_oob:{table_key}:{idx}")
            readonly.append(table_addresses[idx])
    return keys + writable + readonly, len(writable) + len(readonly)


def _make_broker() -> Any:
    from birth_first_sniper import BotConfig  # type: ignore
    from pgg2_direct_pump import DirectPumpQuoteBroker  # type: ignore

    os.environ.setdefault("PGG2_EXECUTION_MODE", "live")
    os.environ.setdefault("PGG2_LIVE_CONFIRM", "I_ACCEPT_REAL_SOL_RISK")
    os.environ.setdefault("PGG2_DIRECT_LIVE_CONFIRM", "I_ACCEPT_DIRECT_PUMP_RISK")
    # V109 atomic bundle path must not inherit Helius Sender/SWQOS tip
    # mutation from the directional V102 runner. This broker signs local
    # guarded txs for bundle construction only; pgg2_v108_jito_bundle_sender
    # is the only module allowed to submit a bundle.
    api = os.environ.get("HELIUS_API_KEY", "")
    helius_rpc = os.environ.get("V109_READ_RPC_URL") or os.environ.get("PGG2_READ_RPC_URL") or os.environ.get("HELIUS_RPC_URL") or (
        f"https://mainnet.helius-rpc.com/?api-key={api}" if api else ""
    )
    if helius_rpc:
        os.environ["HELIUS_RPC_URL"] = helius_rpc
        os.environ["PGG2_LIVE_RPC_URL"] = helius_rpc
    cfg = BotConfig()
    return DirectPumpQuoteBroker(cfg)


def _ensure_tip_account() -> str:
    acct = os.environ.get("PGG2_JITO_TIP_ACCOUNT") or os.environ.get("JITO_TIP_ACCOUNT") or ""
    if acct:
        return acct
    tips = get_tip_accounts()
    if not tips:
        raise RuntimeError("no_jito_tip_accounts")
    os.environ["PGG2_JITO_TIP_ACCOUNT"] = str(tips[0])
    _log(f"PGG2-V109-JITO-TIP-READY account={str(tips[0])[:4]}..")
    return str(tips[0])


def _extract_pump_buy(raw_tx: bytes, slot: int, source: str) -> Optional[Any]:
    try:
        return decode_external_pump_buy(
            base64.b64encode(raw_tx).decode("ascii"),
            source=source,
            slot=slot,
        )
    except Exception:
        return None


def _force_buyback_pair_from_external(broker: Any, decoded: Any) -> bool:
    """Use the external buy's own remaining accounts for our guarded buy.

    This avoids a late getTransaction/current-sig lookup while the opportunity
    is still prelanding.
    """
    try:
        from solders.transaction import VersionedTransaction  # type: ignore
        from pgg2_direct_pump import (  # type: ignore
            PUMP_FEE_PROGRAM_ID,
            PUMP_PROGRAM_ID,
            KNOWN_PUMP_SOCIAL_FEE_PDAS,
        )

        tx = VersionedTransaction.from_bytes(base64.b64decode(str(decoded.raw_tx_b64)))
        keys, alt_loaded_count = _resolved_tx_keys(tx)
        for ix in tx.message.instructions:
            pid_idx = int(ix.program_id_index)
            if pid_idx >= len(keys) or str(keys[pid_idx]) != str(PUMP_PROGRAM_ID):
                continue
            accounts = list(ix.accounts)
            fee_positions = [
                pos for pos, account_idx in enumerate(accounts)
                if int(account_idx) < len(keys)
                and str(keys[int(account_idx)]) == str(PUMP_FEE_PROGRAM_ID)
            ]
            if not fee_positions:
                continue
            extras = [
                keys[int(account_idx)]
                for account_idx in accounts[fee_positions[-1] + 1:]
                if int(account_idx) < len(keys)
            ]
            social_positions = [
                idx for idx, key in enumerate(extras)
                if str(key) in KNOWN_PUMP_SOCIAL_FEE_PDAS
            ]
            if not social_positions:
                continue
            social_idx = social_positions[0]
            recipients = [str(key) for key in extras[:social_idx] if str(key) not in KNOWN_PUMP_SOCIAL_FEE_PDAS]
            if not recipients:
                continue
            os.environ["PGG2_DIRECT_PUMP_BUYBACK_FEE_RECIPIENT"] = recipients[0]
            os.environ["PGG2_DIRECT_PUMP_SOCIAL_FEE_PDA"] = str(extras[social_idx])
            _log(
                f"PGG2-V109-BUYBACK-PAIR-FROM-EXTERNAL mint={decoded.mint[:4]}.. "
                f"recipient={recipients[0][:4]}.. social={str(extras[social_idx])[:4]}.. "
                f"alt_loaded={alt_loaded_count}"
            )
            return True
    except Exception as exc:
        _log(f"PGG2-V109-BUYBACK-PAIR-DECODE-BLOCK err={type(exc).__name__}:{exc}")
    return False


def _build_validation_for_raw(*, broker: Any, decoded: Any, event_ts_ms: int) -> tuple[bool, str]:
    from pgg2_direct_pump import as_pubkey  # type: ignore

    # Status RPC calls cost precious milliseconds. For the no-send validator,
    # the only status check that matters is after the exact bundle is built:
    # if the external tx is still unlanded then, the build path is fast enough
    # to be usable. Earlier checks are available for debugging but off by
    # default because they can create the failure they are trying to measure.
    if os.environ.get("V109_STATUS_CHECK_MODE", "final_only").lower() != "final_only":
        status = _sig_status(decoded.signature)
        if status is not None:
            return False, f"external_tx_already_landed_at_decode status={status.get('confirmationStatus')}"

    t0 = _now_ms()
    try:
        curve = broker.bonding_curve(as_pubkey(decoded.mint))
    except Exception as exc:
        return False, f"curve_read_failed:{type(exc).__name__}"
    t_curve = _now_ms()

    best = select_best_size(
        mint=decoded.mint,
        vsol_lamports=int(curve.virtual_sol_reserves),
        vtok_raw=int(curve.virtual_token_reserves),
        external_sol_lamports=max(1, int(decoded.sol_lamports)),
    )
    if not best or not best.passed:
        return False, "bundle_profit_negative_or_tip_exceeds_edge"
    if not _force_buyback_pair_from_external(broker, decoded):
        return False, "no_buyback_pair_in_external_raw_tx"

    vsol_after_our = int(curve.virtual_sol_reserves) + int(best.size_lamports)
    vtok_after_our = max(1, int(curve.virtual_token_reserves) - int(best.our_tokens_raw))
    vsol_after_external = vsol_after_our + max(1, int(decoded.sol_lamports))
    vtok_after_external = max(1, vtok_after_our - int(best.external_tokens_raw))
    try:
        plan = build_bundle_plan_no_send(
            broker=broker,
            decoded_external=decoded,
            profit_result=best,
            vsol_after_external=vsol_after_external,
            vtok_after_external=vtok_after_external,
            buy_curve_snapshot=curve,
            snapshot_ts_ms=t_curve,
            creator=str(getattr(curve, "creator", "")),
        )
    except Exception as exc:
        return False, f"bundle_build_failed:{type(exc).__name__}"
    status = _sig_status(decoded.signature)
    if status is not None:
        return False, f"external_tx_landed_before_final_validation build_ms={_now_ms() - t_curve}"
    send_bundle(
        [plan.our_buy_b64, plan.external_buy_b64, plan.our_sell_close_b64, plan.tip_b64],
        dry_run=True,
    )
    _log(
        f"PGG2-V109-NO-SEND-BUNDLE-READY elapsed_ms={_now_ms() - event_ts_ms} "
        f"{plan_to_json(plan)}"
    )
    return True, "bundle_ready"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("V109_RAW_UDP_PORT", "8001")))
    ap.add_argument("--max-seconds", type=int, default=300)
    ap.add_argument("--out-jsonl", default="/root/piggy/data/v109_no_send_bundle_validation.jsonl")
    args = ap.parse_args()
    _load_env()
    out = Path(args.out_jsonl)
    out.parent.mkdir(parents=True, exist_ok=True)
    fp = out.open("w", encoding="utf-8")

    def emit_json(rec: dict[str, Any]) -> None:
        fp.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
        fp.flush()

    _log("PGG2-V109-NO-SEND-VALIDATION-START source=raw_udp_shredstream")
    _ensure_tip_account()
    broker = _make_broker()
    min_external_lamports = int(os.environ.get("V109_MIN_EXTERNAL_LAMPORTS_FOR_BUILD", "0") or 0)
    if min_external_lamports > 0:
        _log(f"PGG2-V109-MIN-EXTERNAL-FOR-BUILD lamports={min_external_lamports}")
    # V117 proved the SDK's supported path is the bound iterable listener:
    #   for slot, transactions in ShredListener.bind(port): ...
    # The previous offline socket path could miss the live UDP stream even
    # while tcpdump showed packets arriving.
    listener = _bind_low_latency_shred_listener(int(args.port))
    deadline = time.time() + max(1, int(args.max_seconds))
    packets = decoded_txs = pump_buys = blocks = 0
    top_blockers: dict[str, int] = {}
    try:
        for out_batch in listener:
            if time.time() >= deadline:
                break
            packets += 1
            if not out_batch:
                continue
            try:
                slot, txs = out_batch
            except Exception:
                continue
            for raw_tx in txs:
                decoded_txs += 1
                event_ts = _now_ms()
                decoded = _extract_pump_buy(bytes(raw_tx), int(slot or 0), "raw_udp_shredstream")
                if not decoded:
                    continue
                pump_buys += 1
                _log(
                    f"PGG2-V109-RAW-TX-SEEN source=raw_udp_shredstream mint={decoded.mint[:4]}.. "
                    f"sig={decoded.signature[:16]} sol_lamports={decoded.sol_lamports}"
                )
                emit_json({"kind": "candidate", "event_ts_ms": event_ts, **decoded.__dict__})
                if min_external_lamports > 0 and int(decoded.sol_lamports) < min_external_lamports:
                    reason = "external_size_below_fast_bundle_threshold"
                    blocks += 1
                    top_blockers[reason] = top_blockers.get(reason, 0) + 1
                    _log(f"PGG2-V109-NO-SEND-BUNDLE-BLOCK reason={reason}")
                    continue
                ok, reason = _build_validation_for_raw(broker=broker, decoded=decoded, event_ts_ms=event_ts)
                if ok:
                    fp.close()
                    return 0
                blocks += 1
                top_blockers[reason] = top_blockers.get(reason, 0) + 1
                _log(f"PGG2-V109-NO-SEND-BUNDLE-BLOCK reason={reason}")
        _log(
            f"PGG2-V109-NO-SEND-VALIDATION-FINAL packets={packets} decoded_txs={decoded_txs} "
            f"pump_buys={pump_buys} blocks={blocks} top_blockers={top_blockers}"
        )
        return 1
    finally:
        close = getattr(listener, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        fp.close()


if __name__ == "__main__":
    raise SystemExit(main())
