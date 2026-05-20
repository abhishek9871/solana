"""v36c-3 Live Entry-Snapshot-Bank Executor — Jito atomic buy+sell bundle.

The dry-live `risk_worker_entry_snapshot_bank` succeeded because the broker
never sent the locked sell quote on chain — both the buy quote and the
locked sell quote were just objects. In live mode, a sequential buy →
confirm → sell sequence takes 1.5–4 s round-trip, during which the price
can drift arbitrarily; that is NOT equivalent to dry-live ESB.

This module implements the only live-equivalent path: submit the buy AND
sell as a single Jito bundle so both transactions are guaranteed to land
in the same slot, in order, atomically (or both are dropped together).

Public entry point:
    JitoESBExecutor(broker, ...).execute_esb(mint, plan, locked_buy_quote,
                                             locked_sell_amount_tokens,
                                             predicted_all_in_pnl,
                                             tip_lamports) -> dict

Returns a result dict with the bundle ID, buy/sell tx signatures, actual
wallet delta, and reconciliation outcome. Caller logs PGG2-LIVE-ESB-*
events at each stage; this module emits them internally too so the
callsite can be thin.

This file is the LIVE-EQUIVALENCE ENABLER. Without it (and a configured
Jito tip account + block-engine endpoint), Entry Snapshot Bank in live
mode must remain blocked by the broker's `PGG2-LIVE-EQUIVALENCE-BLOCK`.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _log(line: str) -> None:
    """Emit a log line in the same canonical format the bot uses."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


class JitoESBExecutor:
    """Live-equivalent Entry Snapshot Bank executor.

    Builds + signs a buy transaction from the locked quote, builds + signs
    a sell transaction for the EXACT same token amount, optionally adds a
    Jito tip transfer, simulates each transaction, then submits the pair
    as a Jito bundle. Polls for bundle status. Reconciles actual wallet
    delta against the predicted route-aware PnL.

    Failure modes are exhaustive: bundle build, simulate, submit, land,
    reconcile. ANY of them failing aborts the ESB attempt and logs the
    specific blocker. The executor does NOT silently fall back to a
    non-atomic sequential path.
    """

    def __init__(
        self,
        broker: Any,
        jito_block_engine_url: str,
        jito_tip_account: str,
        jito_tip_lamports: int = 10_000,
        bundle_simulate_first: bool = True,
        bundle_poll_timeout_sec: float = 12.0,
        bundle_poll_interval_sec: float = 0.5,
        max_pnl_drift_sol: float = 0.00050,
    ) -> None:
        if not jito_block_engine_url:
            raise RuntimeError("PGG2_JITO_BLOCK_ENGINE_URL is required for live ESB")
        if not jito_tip_account:
            raise RuntimeError("PGG2_JITO_TIP_ACCOUNT is required for live ESB")
        self.broker = broker
        self.block_engine_url = jito_block_engine_url.rstrip("/")
        self.tip_account = jito_tip_account
        self.tip_lamports = int(jito_tip_lamports)
        self.bundle_simulate_first = bool(bundle_simulate_first)
        self.bundle_poll_timeout_sec = float(bundle_poll_timeout_sec)
        self.bundle_poll_interval_sec = float(bundle_poll_interval_sec)
        self.max_pnl_drift_sol = float(max_pnl_drift_sol)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def execute_esb(
        self,
        mint: str,
        locked_buy_quote: dict[str, Any],
        locked_sell_amount_tokens: float,
        predicted_all_in_pnl: float,
        slippage_pct: float,
        token_invariant_buy_tokens: float,
    ) -> dict[str, Any]:
        """Execute a one-shot live ESB cycle.

        Args:
            mint: target SPL mint.
            locked_buy_quote: the broker quote object used at decision time.
            locked_sell_amount_tokens: the EXACT token amount the sell must
                target. Must equal token_invariant_buy_tokens to honor the
                quote-token / live-token invariant.
            predicted_all_in_pnl: route-aware PnL prediction from dry-live
                quote_all_in_pnl(). Used for reconciliation.
            slippage_pct: sell slippage in percent.
            token_invariant_buy_tokens: the token amount the broker quote
                said the buy will deliver. Must match the sell amount.

        Returns dict with keys:
            success (bool), bundle_id, buy_sig, sell_sig, actual_wallet_delta_sol,
            predicted_all_in_pnl, drift_sol, reason_if_failed.
        """
        out: dict[str, Any] = {
            "success": False,
            "bundle_id": None,
            "buy_sig": None,
            "sell_sig": None,
            "predicted_all_in_pnl": float(predicted_all_in_pnl),
            "actual_wallet_delta_sol": None,
            "drift_sol": None,
            "reason_if_failed": None,
        }
        try:
            self._enforce_token_invariant(
                mint, locked_sell_amount_tokens, token_invariant_buy_tokens
            )
        except Exception as exc:
            out["reason_if_failed"] = f"token_invariant: {exc}"
            _log(
                f"PGG2-LIVE-ESB-TOKEN-INVARIANT mint={mint} status=fail "
                f"sell_tokens={locked_sell_amount_tokens} expected={token_invariant_buy_tokens} "
                f"err={exc}"
            )
            return out
        _log(
            f"PGG2-LIVE-ESB-TOKEN-INVARIANT mint={mint} status=pass "
            f"tokens={locked_sell_amount_tokens}"
        )

        # --- 1. Sign buy from locked quote ---
        try:
            buy_signed_b64 = self._sign_locked_quote(locked_buy_quote)
        except Exception as exc:
            out["reason_if_failed"] = f"buy_sign: {exc}"
            _log(f"PGG2-LIVE-ESB-BUNDLE-FAILED mint={mint} stage=buy_sign err={exc}")
            return out

        # --- 2. Build + sign sell for the same locked tokens ---
        try:
            sell_quote = self.broker.build_sell(mint, float(locked_sell_amount_tokens), float(slippage_pct))
            sell_signed_b64 = self._sign_locked_quote(sell_quote)
        except Exception as exc:
            out["reason_if_failed"] = f"sell_build_or_sign: {exc}"
            _log(f"PGG2-LIVE-ESB-BUNDLE-FAILED mint={mint} stage=sell_build err={exc}")
            return out

        # --- 3. Simulate both legs if configured ---
        if self.bundle_simulate_first:
            sim_ok, sim_err = self._simulate_pair(buy_signed_b64, sell_signed_b64)
            if not sim_ok:
                out["reason_if_failed"] = f"simulate: {sim_err}"
                _log(f"PGG2-LIVE-ESB-SIM-FAIL mint={mint} err={sim_err}")
                return out
            _log(f"PGG2-LIVE-ESB-SIM-PASS mint={mint}")

        # --- 4. Build + sign tip tx (separate transaction, paid by user) ---
        try:
            tip_signed_b64 = self._build_tip_tx_signed()
        except Exception as exc:
            out["reason_if_failed"] = f"tip_build: {exc}"
            _log(f"PGG2-LIVE-ESB-BUNDLE-FAILED mint={mint} stage=tip_build err={exc}")
            return out

        # --- 5. Submit bundle ---
        _log(
            f"PGG2-LIVE-ESB-BUNDLE-BUILD mint={mint} "
            f"txs=3 buy_b64_len={len(buy_signed_b64)} sell_b64_len={len(sell_signed_b64)} "
            f"tip_lamports={self.tip_lamports}"
        )
        try:
            bundle_id = self._submit_bundle([buy_signed_b64, sell_signed_b64, tip_signed_b64])
            out["bundle_id"] = bundle_id
            _log(f"PGG2-LIVE-ESB-BUNDLE-SUBMIT mint={mint} bundle_id={bundle_id}")
        except Exception as exc:
            out["reason_if_failed"] = f"bundle_submit: {exc}"
            _log(f"PGG2-LIVE-ESB-BUNDLE-FAILED mint={mint} stage=submit err={exc}")
            return out

        # --- 6. Poll for bundle status / tx signatures ---
        try:
            landed = self._poll_bundle(bundle_id)
        except Exception as exc:
            out["reason_if_failed"] = f"bundle_poll: {exc}"
            _log(f"PGG2-LIVE-ESB-BUNDLE-FAILED mint={mint} stage=poll err={exc}")
            return out
        if not landed.get("landed"):
            out["reason_if_failed"] = f"bundle_did_not_land: {landed}"
            _log(
                f"PGG2-LIVE-ESB-BUNDLE-FAILED mint={mint} stage=land "
                f"reason={landed.get('reason')}"
            )
            return out
        buy_sig = landed.get("buy_sig")
        sell_sig = landed.get("sell_sig")
        out["buy_sig"] = buy_sig
        out["sell_sig"] = sell_sig
        _log(
            f"PGG2-LIVE-ESB-BUNDLE-LANDED mint={mint} bundle_id={bundle_id} "
            f"buy_sig={buy_sig} sell_sig={sell_sig}"
        )

        # --- 7. Reconcile actual wallet delta vs predicted PnL ---
        try:
            actual_delta = self._compute_wallet_delta(buy_sig, sell_sig)
        except Exception as exc:
            out["reason_if_failed"] = f"wallet_delta: {exc}"
            _log(f"PGG2-LIVE-ESB-WALLET-DELTA mint={mint} status=unavailable err={exc}")
            return out
        out["actual_wallet_delta_sol"] = float(actual_delta)
        drift = float(actual_delta) - float(predicted_all_in_pnl)
        out["drift_sol"] = drift
        _log(
            f"PGG2-LIVE-ESB-PNL-PREDICTED mint={mint} predicted_all_in_pnl={predicted_all_in_pnl:+.6f}"
        )
        _log(
            f"PGG2-LIVE-ESB-PNL-ACTUAL mint={mint} actual_wallet_delta={actual_delta:+.6f}"
        )
        _log(
            f"PGG2-LIVE-ESB-PNL-DELTA mint={mint} drift_sol={drift:+.6f} "
            f"tolerance_sol={self.max_pnl_drift_sol:.6f}"
        )
        if abs(drift) > self.max_pnl_drift_sol:
            out["reason_if_failed"] = f"pnl_drift_exceeded: drift={drift:+.6f}"
            _log(
                f"PGG2-LIVE-ESB-RECONCILE mint={mint} status=fail drift={drift:+.6f} "
                f"tolerance={self.max_pnl_drift_sol:.6f}"
            )
            return out
        _log(
            f"PGG2-LIVE-ESB-RECONCILE mint={mint} status=pass drift={drift:+.6f}"
        )
        out["success"] = True
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _enforce_token_invariant(mint: str, sell_tokens: float, buy_tokens: float) -> None:
        if abs(float(sell_tokens) - float(buy_tokens)) > 1e-6:
            raise RuntimeError(
                f"sell_tokens={sell_tokens:.9f} != buy_tokens={buy_tokens:.9f}"
            )

    def _sign_locked_quote(self, quote: dict[str, Any]) -> str:
        """Use the broker's signer (already required for live quote mode)."""
        if not hasattr(self.broker, "sign_transaction"):
            raise RuntimeError("broker has no sign_transaction()")
        if not quote or "txn" not in quote:
            raise RuntimeError("quote missing txn field")
        signed_b64, _signed_b58 = self.broker.sign_transaction(str(quote["txn"]))
        return signed_b64

    def _simulate_pair(self, buy_b64: str, sell_b64: str) -> tuple[bool, str]:
        """Simulate the two transactions via the broker's RPC. Returns
        (ok, err)."""
        if not hasattr(self.broker, "simulate_signed"):
            return False, "broker has no simulate_signed()"
        try:
            self.broker.simulate_signed(buy_b64)
        except Exception as exc:
            return False, f"buy_simulate: {exc}"
        try:
            self.broker.simulate_signed(sell_b64)
        except Exception as exc:
            return False, f"sell_simulate: {exc}"
        return True, ""

    def _build_tip_tx_signed(self) -> str:
        """Build a base58-encoded signed tip transfer.

        We construct a System Program transfer from the broker's pubkey to
        the configured Jito tip account. The broker's wallet must already
        be loaded (it is, since `mode=live` requires the keypair).

        TODO (operator action required): the exact tip-tx construction
        depends on which Solana SDK is in the broker's environment. The
        broker exposes `self.keypair` and `self.rpc_url` per
        pgg2_live_raptor.py:62+. Implementations using solders.Transaction
        or solana-py both work; the relevant change is to build a single
        instruction (system_program::transfer with `lamports=tip_lamports`)
        and sign with the same keypair.
        """
        if not hasattr(self.broker, "keypair") or self.broker.keypair is None:
            raise RuntimeError("broker.keypair is required for tip tx")
        if not hasattr(self.broker, "build_tip_transaction"):
            # The broker doesn't yet expose a tip-tx builder. Raise so the
            # operator knows the integration step is missing — DO NOT
            # silently skip the tip (Jito requires a tip for bundle
            # priority).
            raise RuntimeError(
                "broker.build_tip_transaction(tip_account, lamports) not "
                "implemented — wire a system_program::transfer builder "
                "before enabling live ESB"
            )
        signed_b64, _signed_b58 = self.broker.build_tip_transaction(
            self.tip_account, self.tip_lamports
        )
        return signed_b64

    def _submit_bundle(self, signed_txs_b64: list[str]) -> str:
        """POST the bundle to the Jito block engine. Returns the bundle ID.

        Jito Block Engine API: POST /api/v1/bundles
          body: { jsonrpc, id, method:"sendBundle", params:[ [<base64 txs>] ] }
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [signed_txs_b64],
        }
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = f"{self.block_engine_url}/api/v1/bundles"
        req = Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=8.0) as resp:
                raw = resp.read().decode("utf-8")
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"block_engine_post: {type(exc).__name__}: {exc}")
        parsed = json.loads(raw) if raw else {}
        if "error" in parsed:
            raise RuntimeError(f"block_engine_error: {parsed['error']}")
        bundle_id = parsed.get("result")
        if not bundle_id:
            raise RuntimeError(f"block_engine_no_result: {parsed}")
        return str(bundle_id)

    def _poll_bundle(self, bundle_id: str) -> dict[str, Any]:
        """Poll the block engine for bundle status.

        Returns { landed: bool, buy_sig, sell_sig, reason }.
        """
        deadline = time.time() + self.bundle_poll_timeout_sec
        while time.time() < deadline:
            try:
                status = self._fetch_bundle_status(bundle_id)
            except Exception:
                status = None
            if status:
                state = str(status.get("status", "")).lower()
                if state == "landed":
                    txs = status.get("transactions") or []
                    if len(txs) >= 2:
                        return {"landed": True, "buy_sig": txs[0], "sell_sig": txs[1]}
                    return {"landed": False, "reason": "landed_but_insufficient_signatures"}
                if state in ("failed", "rejected", "dropped"):
                    return {"landed": False, "reason": state}
            time.sleep(self.bundle_poll_interval_sec)
        return {"landed": False, "reason": "poll_timeout"}

    def _fetch_bundle_status(self, bundle_id: str) -> Optional[dict[str, Any]]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getInflightBundleStatuses",
            "params": [[bundle_id]],
        }
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        url = f"{self.block_engine_url}/api/v1/bundles"
        req = Request(url, data=data, headers={"content-type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=4.0) as resp:
                raw = resp.read().decode("utf-8")
        except (HTTPError, URLError):
            return None
        parsed = json.loads(raw) if raw else {}
        result = parsed.get("result") or {}
        value = result.get("value") or []
        if not value:
            return None
        return value[0]

    def _compute_wallet_delta(self, buy_sig: str, sell_sig: str) -> float:
        """Compute the actual SOL wallet delta from buy + sell tx confirms.

        Implementation note: this needs the broker's RPC to read pre/post
        balances. The simplest path is to query getTransaction for each
        signature and sum the `meta.preBalances[0] - meta.postBalances[0]`
        for the wallet's account. We expose a hook on the broker if it
        already implements this.
        """
        if hasattr(self.broker, "wallet_delta_from_signatures"):
            return float(self.broker.wallet_delta_from_signatures(buy_sig, sell_sig))
        raise RuntimeError(
            "broker.wallet_delta_from_signatures(buy_sig, sell_sig) not "
            "implemented — wire it via getTransaction JSON-RPC before "
            "enabling live ESB"
        )
