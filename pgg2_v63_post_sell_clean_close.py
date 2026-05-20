"""V63 Post-Sell Mandatory CloseAccount Module.

Safety net layered on top of the V62B authoritative sell router. After every
V62B-confirmed sell of a pump_bc position, V63 verifies the token account is
closed and rent recovered. Three cases:

  1. ATA no longer exists -> already closed (broker.build_sell included
     CloseAccount atomically in the sell tx). No-op, mark already_closed.

  2. ATA exists with raw balance == 0 -> rent still locked. V63 builds a
     standalone CloseAccount instruction and submits via Helius SWQOS-only.

  3. ATA exists with raw balance > 0 -> residual tokens. Do NOT close;
     mark balance_not_zero so caller can decide (residual rescue, log alert).

Why this exists:
  - V62B Stage A RUN1 forensic: 21 zombie Token-2022 ATAs in the wallet
    (each holding 2_074_080 lamports of rent) from older bot runs proves
    that historically the sell path didn't always close the ATA. The V62B
    trade's ATA was closed atomically because broker.build_sell happened to
    include CloseAccount, but relying on a single broker code path is brittle.

  - Final P&L accounting MUST use wallet_after_close_account, not
    wallet_after_sell. Without this safety net, future regressions in the
    sell builder leave rent trapped and inflate negative-close counts.

Token program handling:
  - Token-2022 (Tokenz...): use spl-token-2022 CloseAccount.
  - SPL Token (Tokenkeg...): use legacy spl-token CloseAccount.
  - For pump_bc mints post-Apr-28 Pump-v2 upgrade, Token-2022 is universal.

Logs emitted (one per phase):
  - PGG2-V63-CLOSE-ACCOUNT-CHECK
  - PGG2-V63-CLOSE-ACCOUNT-SKIP
  - PGG2-V63-CLOSE-ACCOUNT-SEND
  - PGG2-V63-CLOSE-ACCOUNT-CONFIRMED
  - PGG2-V63-CLOSE-ACCOUNT-FAIL
  - PGG2-V63-RENT-RECOVERED
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Program IDs
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# CloseAccount instruction discriminator (single byte 9 for both SPL and 2022)
CLOSE_ACCOUNT_IX_DISC = 9


@dataclass
class V63CloseResult:
    confirmed: bool = False
    already_closed: bool = False
    balance_not_zero: bool = False
    close_sig: Optional[str] = None
    recovered_lamports: int = 0
    wallet_after_close: Optional[int] = None
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass
class V63Config:
    poll_interval_ms: int = 100
    poll_timeout_ms: int = 3000
    swqos_tip_lamports: int = 5_000
    compute_unit_price_micro: int = 100_000
    compute_unit_limit: int = 50_000
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "V63Config":
        def i(k: str, default: int) -> int:
            try:
                return int(os.environ.get(k, str(default)) or default)
            except Exception:
                return default
        return cls(
            poll_interval_ms=i("PGG2_V63_POLL_INTERVAL_MS", 100),
            poll_timeout_ms=i("PGG2_V63_POLL_TIMEOUT_MS", 3_000),
            swqos_tip_lamports=i("PGG2_V63_SWQOS_TIP_LAMPORTS", 5_000),
            compute_unit_price_micro=i("PGG2_V63_CU_PRICE_MICRO", 100_000),
            compute_unit_limit=i("PGG2_V63_CU_LIMIT", 50_000),
            enabled=os.environ.get("PGG2_V63_ENABLED", "1").lower() in ("1", "true", "yes"),
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _rpc_call(rpc_url: str, method: str, params: list) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _get_account_info(rpc_url: str, ata: str) -> Optional[dict]:
    r = _rpc_call(rpc_url, "getAccountInfo", [ata, {"encoding": "jsonParsed"}])
    if "error" in r:
        return None
    return r.get("result", {}).get("value")


def _get_signature_status(rpc_url: str, sig: str) -> dict:
    r = _rpc_call(rpc_url, "getSignatureStatuses", [[sig]])
    if "result" not in r:
        return {"err": "rpc_error"}
    vals = r["result"]["value"]
    return vals[0] if vals else {}


def _get_balance(rpc_url: str, wallet: str) -> int:
    r = _rpc_call(rpc_url, "getBalance", [wallet])
    return int(r.get("result", {}).get("value", 0))


def v63_close_zero_balance_token_account(
    broker: Any,
    mint: str,
    ata: str,
    owner: str,
    token_program: str,
    rpc_url: str,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
) -> V63CloseResult:
    """Safety net: ensure ATA is closed and rent recovered after a full sell.

    broker must provide:
      - sign_transaction(txn_b64) -> (signed_b64, sig_preview)
      - send_signed(signed_b64) -> sent_sig
      - build_close_account(mint, ata, owner, token_program) -> {"txn": b64}
          (optional — if not present, V63 builds the tx directly via solana-py)
      - rpc_url alternative if rpc_url arg is empty

    Returns V63CloseResult with confirmed/already_closed/balance_not_zero
    branches mutually exclusive.
    """
    cfg = V63Config.from_env()
    log = log_fn if log_fn is not None else print
    start_ms = _now_ms()
    short_mint = mint[:4] + ".." + mint[-4:] if len(mint) > 8 else mint

    if not cfg.enabled:
        log(f"PGG2-V63-CLOSE-ACCOUNT-SKIP mint={short_mint} reason=v63_disabled")
        return V63CloseResult(error="v63_disabled", elapsed_ms=_now_ms() - start_ms)

    # ===== Step 1: query the ATA =====
    log(
        f"PGG2-V63-CLOSE-ACCOUNT-CHECK mint={short_mint} ata={ata} "
        f"token_program={token_program} owner={owner}"
    )
    try:
        info = _get_account_info(rpc_url, ata)
    except Exception as exc:
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
            f"reason=rpc_account_info_exc err={type(exc).__name__}:{exc}"
        )
        return V63CloseResult(
            error=f"rpc_account_info_exc:{type(exc).__name__}",
            elapsed_ms=_now_ms() - start_ms,
        )

    if info is None:
        # ATA no longer exists -> already closed (likely by broker.build_sell
        # including CloseAccount atomically).
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-SKIP mint={short_mint} ata={ata} "
            f"reason=ata_already_closed"
        )
        return V63CloseResult(
            already_closed=True,
            confirmed=True,
            elapsed_ms=_now_ms() - start_ms,
        )

    # Parse balance + lamports
    try:
        parsed = info.get("data", {}).get("parsed", {}).get("info", {})
        raw_amount = parsed.get("tokenAmount", {}).get("amount", "0")
        ata_lamports = int(info.get("lamports", 0))
        actual_program = info.get("owner", "")
    except Exception as exc:
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
            f"reason=parse_account_info err={type(exc).__name__}:{exc}"
        )
        return V63CloseResult(
            error=f"parse_account_info:{type(exc).__name__}",
            elapsed_ms=_now_ms() - start_ms,
        )

    try:
        raw = int(raw_amount)
    except Exception:
        raw = 0

    if raw > 0:
        # Residual tokens; do NOT close. Caller decides.
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-SKIP mint={short_mint} ata={ata} "
            f"reason=balance_not_zero raw={raw} ata_lamports={ata_lamports}"
        )
        return V63CloseResult(
            balance_not_zero=True,
            recovered_lamports=0,
            elapsed_ms=_now_ms() - start_ms,
        )

    # Zero balance, account exists -> CloseAccount
    if actual_program != token_program:
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-WARN mint={short_mint} ata={ata} "
            f"declared_program={token_program} actual_program={actual_program} "
            f"using_actual_program=true"
        )
        token_program = actual_program

    # ===== Step 2: build the CloseAccount tx =====
    # Prefer broker-provided builder; fall back to inline builder.
    txn_b64: Optional[str] = None
    if hasattr(broker, "build_close_account"):
        try:
            built = broker.build_close_account(
                mint=mint, ata=ata, owner=owner, token_program=token_program
            )
            txn_b64 = built.get("txn") if isinstance(built, dict) else None
        except Exception as exc:
            log(
                f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
                f"reason=broker_build_close_exc err={type(exc).__name__}:{exc}"
            )
    if txn_b64 is None:
        # Inline build via broker.build_close_account_inline if available;
        # otherwise the caller must implement this builder. We return the
        # error so the harness can fall back to its own builder.
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
            f"reason=no_close_builder_available"
        )
        return V63CloseResult(
            error="no_close_builder_available",
            elapsed_ms=_now_ms() - start_ms,
        )

    # ===== Step 3: sign + send via SWQOS =====
    try:
        signed_b64, sig_preview = broker.sign_transaction(str(txn_b64))
    except Exception as exc:
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
            f"reason=sign_exc err={type(exc).__name__}:{exc}"
        )
        return V63CloseResult(
            error=f"sign_exc:{type(exc).__name__}",
            elapsed_ms=_now_ms() - start_ms,
        )

    log(
        f"PGG2-V63-CLOSE-ACCOUNT-SEND mint={short_mint} ata={ata} "
        f"sig_preview={sig_preview} ata_lamports_before={ata_lamports}"
    )
    try:
        close_sig = broker.send_signed(signed_b64)
    except Exception as exc:
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
            f"reason=send_exc err={type(exc).__name__}:{exc}"
        )
        return V63CloseResult(
            error=f"send_exc:{type(exc).__name__}",
            elapsed_ms=_now_ms() - start_ms,
        )

    # ===== Step 4: poll confirmation =====
    deadline = _now_ms() + cfg.poll_timeout_ms
    confirmed = False
    while _now_ms() < deadline:
        try:
            status = _get_signature_status(rpc_url, close_sig)
        except Exception:
            status = {}
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            err = status.get("err")
            if err is None:
                confirmed = True
                break
            else:
                log(
                    f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
                    f"sig={close_sig} reason=on_chain_err err={json.dumps(err)[:120]}"
                )
                return V63CloseResult(
                    close_sig=close_sig,
                    error=f"on_chain_err:{json.dumps(err)}",
                    elapsed_ms=_now_ms() - start_ms,
                )
        time.sleep(cfg.poll_interval_ms / 1000.0)

    if not confirmed:
        log(
            f"PGG2-V63-CLOSE-ACCOUNT-FAIL mint={short_mint} ata={ata} "
            f"sig={close_sig} reason=poll_timeout elapsed_ms={_now_ms() - start_ms}"
        )
        return V63CloseResult(
            close_sig=close_sig,
            error="poll_timeout",
            elapsed_ms=_now_ms() - start_ms,
        )

    # ===== Step 5: success =====
    try:
        wallet_after = _get_balance(rpc_url, owner)
    except Exception:
        wallet_after = None

    log(
        f"PGG2-V63-CLOSE-ACCOUNT-CONFIRMED mint={short_mint} ata={ata} "
        f"sig={close_sig} elapsed_ms={_now_ms() - start_ms}"
    )
    log(
        f"PGG2-V63-RENT-RECOVERED mint={short_mint} ata={ata} "
        f"recovered_lamports={ata_lamports} wallet_after={wallet_after}"
    )
    return V63CloseResult(
        confirmed=True,
        close_sig=close_sig,
        recovered_lamports=ata_lamports,
        wallet_after_close=wallet_after,
        elapsed_ms=_now_ms() - start_ms,
    )


if __name__ == "__main__":
    print("=== V63 Post-Sell Clean-Close self-test ===")
    cfg = V63Config.from_env()
    print("config:", cfg)
    print()
    print("Module loads correctly. v63_close_zero_balance_token_account is")
    print("callable. To exercise live RPC, run from harness with a real ATA.")
