"""V75 Sender-compliant transaction builder.

V74 sent transactions to `https://sender.helius-rpc.com/fast?swqos_only=true`
and got HTTP 500. Root cause: the tx is missing the SWQOS tip transfer
instruction that Helius Sender SWQOS-only mode requires.

V75 fixes this by monkey-patching the broker's `compile_tx(instructions)`
method (the single chokepoint where ALL tx instruction lists are turned
into a signed transaction). The patched version APPENDS a
`SystemProgram::Transfer` from the wallet to a Helius / Jito tip
account just before compile.

Hard rule: tip account list and lamport amount are env-configurable but
default to 5000 lamports (0.000005 SOL) and the well-known Jito/Helius
SWQOS tip accounts.

This is a fresh-build approach (no decoding/re-signing already-signed
txs): we hook in BEFORE the broker's compile step, so the tip ix becomes
part of the message the broker signs natively.

Public API:
  * `make_tip_builder(...)` — factory.
  * `V75TipBuilder.install_into_broker(broker)` — monkey-patches
    `broker.compile_tx`. Returns an `uninstall` callable.
  * Idempotent: re-installing keeps a single layer of patching.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Callable

from solders.system_program import TransferParams, transfer  # type: ignore


# Helius / Jito SWQOS tip accounts (well-known canonical list).
# Helius Sender uses these accounts as legitimate SWQOS tip destinations.
DEFAULT_TIP_ACCOUNTS: list[str] = [
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
]


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


class V75TipBuilder:
    """Monkey-patches broker.compile_tx to inject a tip transfer ix.

    Each call to broker.compile_tx(instructions) becomes:
        broker.compile_tx(instructions + [tip_transfer_ix(...)])
    The patched method continues to sign + return base64 as before.
    """

    def __init__(
        self,
        tip_accounts: list[str] | None = None,
        tip_lamports: int = 5000,
        log_fn: Callable[[str], None] = print,
        sticky_account: bool = False,
    ) -> None:
        self.tip_accounts = tip_accounts or list(DEFAULT_TIP_ACCOUNTS)
        if not self.tip_accounts:
            raise ValueError("V75TipBuilder requires at least one tip account")
        self.tip_lamports = int(tip_lamports)
        if self.tip_lamports <= 0:
            raise ValueError("V75TipBuilder tip_lamports must be > 0")
        self.log_fn = log_fn
        self._original_compile_tx = None  # restored on uninstall
        self._broker = None
        self._tx_count = 0
        # If sticky_account=True, always use tip_accounts[0]. Otherwise
        # round-robin per tx for better dispersion.
        self.sticky_account = bool(sticky_account)
        # Pubkey objects cached on install
        self._tip_pubkeys = None
        self._payer_pk = None

    def install_into_broker(self, broker: Any) -> Callable[[], None]:
        """Replace broker.compile_tx with a tip-injecting version.

        Returns an `uninstall` callable that restores the original.
        Idempotent: stashes the original at
        `broker._pgg2_v75_original_compile_tx`.
        """
        from solders.pubkey import Pubkey  # type: ignore

        # If already patched, use the previously stashed original (avoid
        # nested wrapping if install is called twice).
        original = getattr(broker, "_pgg2_v75_original_compile_tx", None)
        if original is None:
            original = broker.compile_tx
            setattr(
                broker, "_pgg2_v75_original_compile_tx", original
            )

        self._original_compile_tx = original
        self._broker = broker
        self._tip_pubkeys = [Pubkey.from_string(s) for s in self.tip_accounts]
        # payer pubkey: broker.public_key may be string or Pubkey-like.
        pk_raw = broker.public_key
        if isinstance(pk_raw, str):
            self._payer_pk = Pubkey.from_string(pk_raw)
        else:
            self._payer_pk = pk_raw

        builder = self
        log_fn = self.log_fn

        def patched_compile_tx(instructions: list) -> str:
            return builder._inject_and_compile(instructions)

        broker.compile_tx = patched_compile_tx
        log_fn(
            f"PGG2-V75-TIP-IX-INSTALLED into_broker_id={id(broker)} "
            f"tip_lamports={self.tip_lamports} "
            f"tip_accounts={len(self.tip_accounts)} "
            f"sticky={self.sticky_account}"
        )

        def uninstall() -> None:
            if hasattr(broker, "_pgg2_v75_original_compile_tx"):
                broker.compile_tx = broker._pgg2_v75_original_compile_tx
                log_fn("PGG2-V75-TIP-IX-UNINSTALLED")

        return uninstall

    def _pick_tip_account(self) -> Any:
        if self.sticky_account or len(self._tip_pubkeys) == 1:
            return self._tip_pubkeys[0]
        return self._tip_pubkeys[self._tx_count % len(self._tip_pubkeys)]

    def _make_tip_ix(self) -> Any:
        tip_pk = self._pick_tip_account()
        ix = transfer(
            TransferParams(
                from_pubkey=self._payer_pk,
                to_pubkey=tip_pk,
                lamports=self.tip_lamports,
            )
        )
        self.log_fn(
            f"PGG2-V75-SENDER-TIP-IX from={str(self._payer_pk)[:8]} "
            f"to={str(tip_pk)} lamports={self.tip_lamports}"
        )
        return ix

    def _inject_and_compile(self, instructions: list) -> str:
        if not instructions:
            raise RuntimeError("V75TipBuilder refusing to compile empty ix list")
        t0 = time.time()
        tip_ix = self._make_tip_ix()
        t1 = time.time()
        # Append tip ix at end so it processes after compute-budget +
        # pump-bc instructions. Order is irrelevant for SystemProgram
        # transfer (it just lands), but appending keeps the pump-bc
        # instructions at the original indexes for any decoder.
        new_instructions = list(instructions) + [tip_ix]
        self._tx_count += 1
        signed_b64 = self._original_compile_tx(new_instructions)
        t2 = time.time()
        self.log_fn(
            f"PGG2-V75-SENDER-TX-BUILD tx_count={self._tx_count} "
            f"original_ix_count={len(instructions)} "
            f"final_ix_count={len(new_instructions)} "
            f"signed_len={len(signed_b64)}"
        )
        self.log_fn(
            f"PGG2-V75-SENDER-TX-BUILD-TIMING tx_count={self._tx_count} "
            f"tip_ms={int((t1 - t0) * 1000)} "
            f"compile_ms={int((t2 - t1) * 1000)} "
            f"total_ms={int((t2 - t0) * 1000)}"
        )
        return signed_b64


def make_tip_builder(
    log_fn: Callable[[str], None] = print,
) -> V75TipBuilder:
    """Factory reading env: PGG2_V75_TIP_LAMPORTS, PGG2_V75_TIP_ACCOUNTS,
    PGG2_V75_TIP_STICKY.
    """
    tip_lamports = _envi("PGG2_V75_TIP_LAMPORTS", 5000)
    tip_accounts_env = os.environ.get("PGG2_V75_TIP_ACCOUNTS", "").strip()
    if tip_accounts_env:
        tip_accounts = [a.strip() for a in tip_accounts_env.split(",") if a.strip()]
    else:
        tip_accounts = list(DEFAULT_TIP_ACCOUNTS)
    sticky_env = os.environ.get("PGG2_V75_TIP_STICKY", "0").lower()
    sticky = sticky_env in ("1", "true", "yes", "on")
    return V75TipBuilder(
        tip_accounts=tip_accounts,
        tip_lamports=tip_lamports,
        log_fn=log_fn,
        sticky_account=sticky,
    )
