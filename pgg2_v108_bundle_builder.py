#!/usr/bin/env python3
"""V108 no-send Jito bundle builder.

This module is intentionally fail-closed. It can assemble and validate a bundle
plan, but it never submits anything. Live submission belongs only in
`pgg2_v108_jito_bundle_sender.py`.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from solders.system_program import TransferParams, transfer  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore


LAMPORTS_PER_SOL = 1_000_000_000


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _log(line: str) -> None:
    if os.environ.get("V108_BUNDLE_BUILD_LOG", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


def _short(s: str) -> str:
    return s[:4] + ".." + s[-4:] if s and len(s) > 10 else (s or "?")


@dataclass
class V108BundlePlan:
    mint: str
    selected_size_lamports: int
    projected_profit_lamports: int
    jito_tip_lamports: int
    tx_count: int
    our_buy_b64: str
    external_buy_b64: str
    our_sell_close_b64: str
    tip_b64: str
    our_buy_min_tokens_raw: int
    our_sell_min_sol_lamports: int
    external_signature: str


def _load_env() -> None:
    p = "/root/piggy/.env"
    if not os.path.exists(p):
        return
    with open(p, "r", encoding="utf-8") as fp:
        for raw in fp:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _decode_guarded_tx(tx_b64: str) -> VersionedTransaction:
    raw = base64.b64decode(str(tx_b64))
    return VersionedTransaction.from_bytes(raw)


def _verify_raw_external(raw_external_b64: str, expected_sig: str) -> None:
    tx = _decode_guarded_tx(raw_external_b64)
    sig = str(tx.signatures[0]) if tx.signatures else ""
    if not sig:
        raise RuntimeError("external_tx_missing_signature")
    if expected_sig and sig != expected_sig:
        raise RuntimeError(f"external_tx_signature_mismatch expected={expected_sig} actual={sig}")


def _build_guarded_sell_from_curve_snapshot(
    *,
    broker: Any,
    mint: str,
    sell_tokens_ui: float,
    min_sell_lamports: int,
    virtual_token_reserves: int,
    virtual_sol_reserves: int,
    real_token_reserves: int,
    real_sol_reserves: int,
    creator: str,
) -> dict[str, Any]:
    from solders.instruction import AccountMeta, Instruction  # type: ignore

    from pgg2_direct_pump import (  # type: ignore
        DISC_PUMP_SELL,
        PUMP_FEE_PROGRAM_ID,
        PUMP_PROGRAM_ID,
        SYSTEM_PROGRAM_ID,
        PumpBondingCurve,
        as_pubkey,
        close_token_account,
        get_associated_token_address,
        pda,
        u64,
    )

    mint_pk = as_pubkey(mint)
    curve_key = pda(PUMP_PROGRAM_ID, b"bonding-curve", bytes(mint_pk))
    creator_pk = as_pubkey(creator) if creator else as_pubkey("11111111111111111111111111111111")
    curve = PumpBondingCurve(
        key=curve_key,
        virtual_token_reserves=int(virtual_token_reserves),
        virtual_sol_reserves=int(virtual_sol_reserves),
        real_token_reserves=int(real_token_reserves),
        real_sol_reserves=int(real_sol_reserves),
        token_total_supply=0,
        complete=False,
        creator=creator_pk,
        is_mayhem=False,
        cashback_enabled=False,
    )
    global_cfg = broker.pump_global()
    token_amount = broker.ui_to_raw(mint_pk, sell_tokens_ui)
    expected_lamports, fee_lamports = broker.quote_pump_sell_sol(token_amount, curve, global_cfg)
    token_program = broker.mint_owner(mint_pk)
    user = as_pubkey(broker.public_key)
    user_ata = get_associated_token_address(user, mint_pk, token_program)
    associated_curve = get_associated_token_address(curve.key, mint_pk, token_program)
    creator_vault = pda(PUMP_PROGRAM_ID, b"creator-vault", bytes(curve.creator))
    fee_recipient = broker.pump_fee_recipient(global_cfg, curve)
    data = DISC_PUMP_SELL + u64(token_amount) + u64(min_sell_lamports)
    metas = [
        AccountMeta(broker.pump_global_key, False, False),
        AccountMeta(fee_recipient, False, True),
        AccountMeta(mint_pk, False, False),
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
        *broker.pump_sell_remaining_metas(mint_pk, curve, user),
    ]
    txn = broker.compile_tx([
        *broker.compute_budget_ixs(),
        Instruction(PUMP_PROGRAM_ID, data, metas),
        close_token_account(token_program, user_ata, user, user),
    ])
    return {
        "txn": txn,
        "route": "pump_bc",
        "quote_source": "v211_direct_guarded_curve_snapshot",
        "rate": {
            "amountOut": expected_lamports / LAMPORTS_PER_SOL,
            "minAmountOut": min_sell_lamports / LAMPORTS_PER_SOL,
            "fee": fee_lamports / LAMPORTS_PER_SOL,
            "feeBps": int(global_cfg.fee_bps + global_cfg.creator_fee_bps),
        },
    }


def _require_tip_account() -> str:
    tip_account = (
        os.environ.get("PGG2_JITO_TIP_ACCOUNT")
        or os.environ.get("JITO_TIP_ACCOUNT")
        or ""
    ).strip()
    if not tip_account:
        raise RuntimeError("missing_jito_tip_account")
    return tip_account


def _build_tip_tx_b64(*, broker: Any, tip_account: str, lamports: int) -> str:
    # Uses the existing broker compiler only to create a signed transaction
    # locally. This function never sends.
    from pgg2_direct_pump import as_pubkey  # type: ignore

    ix = transfer(
        TransferParams(
            from_pubkey=as_pubkey(broker.public_key),
            to_pubkey=as_pubkey(tip_account),
            lamports=max(1, int(lamports)),
        )
    )
    return str(broker.compile_tx([ix]))


def build_bundle_plan_no_send(
    *,
    broker: Any,
    decoded_external: Any,
    profit_result: Any,
    vsol_after_external: int,
    vtok_after_external: int,
    buy_curve_snapshot: Any = None,
    snapshot_ts_ms: int = 0,
    creator: str = "",
) -> V108BundlePlan:
    """Build unsigned/signed local bundle txs and validate all hard guards.

    The sell tx is intentionally built from the modeled post-external curve
    state. If that model cannot produce a guarded sell, this function blocks.
    """
    _load_env()
    if not getattr(profit_result, "passed", False):
        raise RuntimeError(f"profit_model_blocked:{getattr(profit_result, 'reason', 'unknown')}")
    raw_external_b64 = str(getattr(decoded_external, "raw_tx_b64", "") or "")
    prebuild_mode = not raw_external_b64 and os.environ.get("V108_ALLOW_EMPTY_EXTERNAL_FOR_PREBUILD", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not raw_external_b64 and not prebuild_mode:
        raise RuntimeError("missing_raw_external_tx")

    if raw_external_b64:
        _verify_raw_external(raw_external_b64, str(getattr(decoded_external, "signature", "") or ""))
    tip_account = _require_tip_account()

    mint = str(decoded_external.mint)
    size_lamports = int(profit_result.size_lamports)
    size_sol = size_lamports / LAMPORTS_PER_SOL
    min_tokens_raw = int(profit_result.our_tokens_raw)
    min_tokens_ui = min_tokens_raw / 1_000_000.0
    fee_floor_lamports = int(
        getattr(profit_result, "components", {}).get("fee_total_lamports", 0) or 0
    )
    # The sell guard must protect final wallet delta, not just the scout
    # amount. The profit model's fee_total includes our base/priority fees,
    # Jito tip, and projection buffer; requiring at least this much over the
    # buy size keeps the bundle fail-closed if the modeled edge is not actually
    # executable at the encoded min-out.
    min_sell_lamports = max(1, int(size_lamports + fee_floor_lamports))
    sell_tokens_ui = min_tokens_ui

    _log(
        f"PGG2-V108-BUNDLE-BUILD mint={_short(mint)} "
        f"size_lamports={size_lamports} projected_profit_lamports={int(profit_result.bundle_profit_lamports):+}"
    )
    _log(
        f"PGG2-V108-BUNDLE-BUY-GUARD mint={_short(mint)} "
        f"min_tokens_raw={min_tokens_raw} size_lamports={size_lamports}"
    )
    buy_snapshot_kwargs = (
        {
            "virtual_token_reserves": int(getattr(buy_curve_snapshot, "virtual_token_reserves")),
            "virtual_sol_reserves": int(getattr(buy_curve_snapshot, "virtual_sol_reserves")),
            "real_token_reserves": int(getattr(buy_curve_snapshot, "real_token_reserves", 0)),
            "real_sol_reserves": int(getattr(buy_curve_snapshot, "real_sol_reserves", 0)),
            "token_total_supply": int(getattr(buy_curve_snapshot, "token_total_supply", 0)),
            "complete": bool(getattr(buy_curve_snapshot, "complete", False)),
            "creator": str(getattr(buy_curve_snapshot, "creator", "") or creator or ""),
            "is_mayhem": bool(getattr(buy_curve_snapshot, "is_mayhem", False)),
            "cashback_enabled": bool(getattr(buy_curve_snapshot, "cashback_enabled", False)),
            "snapshot_ts_ms": int(snapshot_ts_ms or 0),
        }
        if buy_curve_snapshot is not None
        else {}
    )
    if (
        buy_curve_snapshot is not None
        and _truthy_env("V108_USE_FAST_PREBUILT_BUY", "0")
        and hasattr(broker, "build_fast_signed_buy_with_min_tokens_from_curve_snapshot")
    ):
        try:
            buy_quote = broker.build_fast_signed_buy_with_min_tokens_from_curve_snapshot(
                mint,
                size_sol,
                min_tokens_ui,
                **buy_snapshot_kwargs,
            )
        except Exception as exc:
            if not _truthy_env("V108_FAST_BUY_FALLBACK_TO_SLOW", "0"):
                raise RuntimeError(f"fast_prebuilt_buy_failed:{type(exc).__name__}:{exc}") from exc
            _log(
                f"PGG2-V108-FAST-BUY-FALLBACK mint={_short(mint)} "
                f"reason={type(exc).__name__}:{str(exc)[:120]}"
            )
            buy_quote = broker.build_buy_with_min_tokens_from_curve_snapshot(
                mint,
                size_sol,
                min_tokens_ui,
                **buy_snapshot_kwargs,
            )
    elif buy_curve_snapshot is not None and hasattr(broker, "build_buy_with_min_tokens_from_curve_snapshot"):
        buy_quote = broker.build_buy_with_min_tokens_from_curve_snapshot(
            mint,
            size_sol,
            min_tokens_ui,
            **buy_snapshot_kwargs,
        )
    else:
        buy_quote = broker.build_buy_with_min_tokens(mint, size_sol, min_tokens_ui)
    try:
        fresh_buy_out_raw = int(float((buy_quote.get("rate") or {}).get("amountOut") or 0.0) * 1_000_000)
    except Exception:
        fresh_buy_out_raw = 0
    if fresh_buy_out_raw and fresh_buy_out_raw < min_tokens_raw:
        raise RuntimeError(
            f"fresh_buy_quote_below_model_min expected_raw={fresh_buy_out_raw} min_raw={min_tokens_raw}"
        )
    buy_guard = broker.decode_pump_buy_guard_from_tx_b64(str(buy_quote["txn"]))
    if int(buy_guard.get("encoded_spend_lamports") or 0) != size_lamports:
        raise RuntimeError("buy_size_guard_mismatch")
    if int(buy_guard.get("encoded_min_tokens_raw") or 0) < min_tokens_raw:
        raise RuntimeError("buy_min_token_guard_weaker_than_model")

    if raw_external_b64:
        _log(
            f"PGG2-V108-BUNDLE-EXTERNAL-TX mint={_short(mint)} "
            f"sig={str(getattr(decoded_external, 'signature', ''))[:16]} raw_bytes={len(base64.b64decode(raw_external_b64))}"
        )
    else:
        _log(f"PGG2-V108-BUNDLE-EXTERNAL-TX-PREBUILD mint={_short(mint)} raw_bytes=0")
    if os.environ.get("V108_DIRECT_GUARDED_SELL", "1").strip().lower() in {"1", "true", "yes", "on"}:
        guarded_sell = _build_guarded_sell_from_curve_snapshot(
            broker=broker,
            mint=mint,
            sell_tokens_ui=sell_tokens_ui,
            min_sell_lamports=min_sell_lamports,
            virtual_token_reserves=int(vtok_after_external),
            virtual_sol_reserves=int(vsol_after_external),
            real_token_reserves=max(1, int(vtok_after_external)),
            real_sol_reserves=0,
            creator=creator,
        )
    else:
        sell_quote = broker.build_sell_from_curve_snapshot(
            mint,
            sell_tokens_ui,
            0.0,
            virtual_token_reserves=int(vtok_after_external),
            virtual_sol_reserves=int(vsol_after_external),
            real_token_reserves=max(1, int(vtok_after_external)),
            real_sol_reserves=0,
            creator=creator,
            include_close_token_ata=True,
        )
        guarded_sell = broker.retarget_sell_min_sol(dict(sell_quote), mint, min_sell_lamports / LAMPORTS_PER_SOL)
    sell_guard = broker.decode_pump_sell_guard_from_tx_b64(str(guarded_sell["txn"]))
    encoded_min = int(sell_guard.get("encoded_min_sol_lamports") or 0)
    if encoded_min <= 0:
        raise RuntimeError("sell_min_zero")
    if encoded_min < min_sell_lamports:
        raise RuntimeError("sell_min_sol_guard_weaker_than_model")
    _log(
        f"PGG2-V108-BUNDLE-SELL-GUARD mint={_short(mint)} "
        f"tokens_raw={min_tokens_raw} min_sol_lamports={encoded_min} "
        f"fee_floor_lamports={fee_floor_lamports}"
    )

    tip_b64 = _build_tip_tx_b64(
        broker=broker,
        tip_account=tip_account,
        lamports=int(profit_result.jito_tip_lamports),
    )
    _log(
        f"PGG2-V108-BUNDLE-TIP tip_account={_short(tip_account)} "
        f"lamports={int(profit_result.jito_tip_lamports)}"
    )

    txs = [str(buy_quote["txn"]), raw_external_b64, str(guarded_sell["txn"]), tip_b64]
    if len(txs) > 5:
        raise RuntimeError("bundle_too_large")
    if not txs[0] or (not txs[1] and not prebuild_mode) or not txs[2]:
        raise RuntimeError("unhedged_bundle_missing_leg")
    return V108BundlePlan(
        mint=mint,
        selected_size_lamports=size_lamports,
        projected_profit_lamports=int(profit_result.bundle_profit_lamports),
        jito_tip_lamports=int(profit_result.jito_tip_lamports),
        tx_count=len(txs),
        our_buy_b64=txs[0],
        external_buy_b64=txs[1],
        our_sell_close_b64=txs[2],
        tip_b64=txs[3],
        our_buy_min_tokens_raw=min_tokens_raw,
        our_sell_min_sol_lamports=encoded_min,
        external_signature=str(getattr(decoded_external, "signature", "") or ""),
    )


def plan_to_json(plan: V108BundlePlan) -> str:
    safe = asdict(plan)
    for key in ("our_buy_b64", "external_buy_b64", "our_sell_close_b64", "tip_b64"):
        safe[key] = f"<base64:{len(getattr(plan, key))}>"
    return json.dumps(safe, sort_keys=True)
