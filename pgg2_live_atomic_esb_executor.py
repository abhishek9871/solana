"""v36c-3 Live Entry-Snapshot-Bank Executor — single-transaction atomic path.

Mirrors the dry-live ESB guarantee in real-live mode WITHOUT requiring a
Jito tip. The entire buy + sell + cleanup is packed into ONE Solana
transaction. If any instruction fails, ALL of them revert — so a buy
without a matching sell cannot occur. Slippage on the sell is enforced
by the Pump bonding-curve sell instruction itself (`min_sol_output`),
so a price move between buy execution and sell execution inside the
same tx either preserves the entry-snapshot edge or aborts the trade.

Falls back to nothing. The executor does NOT silently substitute a
sequential or protected-hold path. If the atomic tx cannot be built or
simulation does not prove a positive wallet delta, the entry is blocked.

Public entry point:
    AtomicSingleTxESBExecutor(broker, ...).execute_esb(
        mint, amount_sol, locked_buy_quote, locked_quote_tokens,
        predicted_all_in_pnl, sell_slippage_pct,
        send_real=False) -> dict

Set `send_real=True` only inside the v36c3 live-smoke launcher after the
sim-only validation gate (Phase 6) has passed.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional


def _log(line: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}", flush=True)


class AtomicSingleTxESBExecutor:
    """Atomic single-transaction Entry Snapshot Bank.

    Pipeline:
        1. broker.build_atomic_buy_sell_close(mint, amount_sol, slippage)
           → signed tx (legacy or v0), instruction summary
        2. simulate via broker.simulate_signed
        3. parse simulation logs / pre+post balances → predicted wallet delta
        4. verify token invariant (sell input tokens == buy output tokens)
        5. verify simulated wallet delta >= predicted_all_in_pnl - drift
        6. (only if send_real=True) submit + poll for confirmation
        7. reconcile via broker.wallet_delta_from_signatures
    """

    def __init__(
        self,
        broker: Any,
        max_pnl_drift_sol: float = 0.00050,
        compute_unit_limit: int = 600_000,
        compute_unit_price_micro_lamports: int = 22_700,
        confirm_timeout_sec: float = 8.0,
        confirm_poll_interval_sec: float = 0.4,
    ) -> None:
        self.broker = broker
        self.max_pnl_drift_sol = float(max_pnl_drift_sol)
        self.compute_unit_limit = int(compute_unit_limit)
        self.compute_unit_price = int(compute_unit_price_micro_lamports)
        self.confirm_timeout_sec = float(confirm_timeout_sec)
        self.confirm_poll_interval_sec = float(confirm_poll_interval_sec)

    # ------------------------------------------------------------------
    def execute_esb(
        self,
        mint: str,
        amount_sol: float,
        locked_buy_quote: dict[str, Any],
        locked_quote_tokens: float,
        predicted_all_in_pnl: float,
        sell_slippage_pct: float,
        send_real: bool = False,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": False,
            "tx_sig": None,
            "predicted_all_in_pnl": float(predicted_all_in_pnl),
            "sim_wallet_delta_sol": None,
            "actual_wallet_delta_sol": None,
            "drift_sol": None,
            "reason_if_failed": None,
            "send_real": bool(send_real),
        }

        # --- 1. Build the atomic transaction ---
        if not hasattr(self.broker, "build_atomic_buy_sell_close"):
            out["reason_if_failed"] = (
                "broker.build_atomic_buy_sell_close not implemented — "
                "atomic ESB path is not yet wired in pgg2_direct_pump.py"
            )
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-BUILD mint={mint} status=fail "
                f"reason=broker_method_missing"
            )
            return out
        try:
            built = self.broker.build_atomic_buy_sell_close(
                mint_str=mint,
                amount_sol=float(amount_sol),
                sell_slippage_pct=float(sell_slippage_pct),
                compute_unit_limit=self.compute_unit_limit,
                compute_unit_price_micro_lamports=self.compute_unit_price,
                expected_quote_tokens=float(locked_quote_tokens),
            )
        except Exception as exc:
            out["reason_if_failed"] = f"build: {exc}"
            _log(f"PGG2-LIVE-ATOMIC-ESB-BUILD mint={mint} status=fail err={exc}")
            return out

        signed_b64 = built.get("signed_b64")
        token_invariant_buy_tokens = float(built.get("expected_buy_tokens") or 0.0)
        sell_input_tokens = float(built.get("sell_input_tokens") or 0.0)
        ix_summary = built.get("ix_summary") or {}
        _log(
            f"PGG2-LIVE-ATOMIC-ESB-BUILD mint={mint} status=ok "
            f"buy_tokens={token_invariant_buy_tokens:.6f} "
            f"sell_tokens={sell_input_tokens:.6f} "
            f"ixs={list(ix_summary.keys()) if isinstance(ix_summary, dict) else '?'}"
        )

        # --- 2. Token invariant ---
        if abs(token_invariant_buy_tokens - sell_input_tokens) > 1e-6:
            out["reason_if_failed"] = (
                f"token_invariant: sell={sell_input_tokens:.9f} != buy={token_invariant_buy_tokens:.9f}"
            )
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-BUILD mint={mint} status=fail "
                f"reason=token_invariant"
            )
            return out

        # --- 3. Simulate ---
        try:
            sim = self._simulate(signed_b64)
        except Exception as exc:
            out["reason_if_failed"] = f"simulate: {exc}"
            _log(f"PGG2-LIVE-ATOMIC-ESB-SIM-FAIL mint={mint} err={exc}")
            return out
        if not sim.get("ok"):
            out["reason_if_failed"] = f"simulate_failed: {sim.get('err')}"
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-SIM-FAIL mint={mint} "
                f"err={sim.get('err')} logs_tail={sim.get('logs_tail')}"
            )
            return out
        sim_wallet_delta = sim.get("wallet_delta_sol")
        out["sim_wallet_delta_sol"] = sim_wallet_delta
        _log(
            f"PGG2-LIVE-ATOMIC-ESB-SIM-PASS mint={mint} "
            f"wallet_delta_sol={sim_wallet_delta:+.6f} "
            f"cu_consumed={sim.get('cu_consumed')}"
        )

        # --- 4. Drift gate (simulated delta vs predicted) ---
        if sim_wallet_delta is None:
            out["reason_if_failed"] = "simulate_no_wallet_delta"
            return out
        drift_sim = float(sim_wallet_delta) - float(predicted_all_in_pnl)
        if abs(drift_sim) > self.max_pnl_drift_sol:
            out["reason_if_failed"] = (
                f"sim_drift_exceeded: drift={drift_sim:+.6f} "
                f"tolerance={self.max_pnl_drift_sol:.6f}"
            )
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-RECONCILE mint={mint} stage=sim_only status=fail "
                f"drift={drift_sim:+.6f}"
            )
            return out
        if sim_wallet_delta <= 0:
            out["reason_if_failed"] = f"sim_wallet_delta_not_positive: {sim_wallet_delta:+.6f}"
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-RECONCILE mint={mint} stage=sim_only status=fail "
                f"reason=not_positive"
            )
            return out

        # --- 5. If sim-only mode, success here ---
        if not send_real:
            out["success"] = True
            out["drift_sol"] = drift_sim
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-RECONCILE mint={mint} stage=sim_only status=pass "
                f"drift={drift_sim:+.6f}"
            )
            return out

        # --- 6. Send for real ---
        try:
            tx_sig = self._send(signed_b64)
        except Exception as exc:
            out["reason_if_failed"] = f"send: {exc}"
            _log(f"PGG2-LIVE-ATOMIC-ESB-SEND mint={mint} status=fail err={exc}")
            return out
        out["tx_sig"] = tx_sig
        _log(f"PGG2-LIVE-ATOMIC-ESB-SEND mint={mint} status=submitted tx_sig={tx_sig}")

        # --- 7. Confirm ---
        try:
            confirmed = self._confirm(tx_sig)
        except Exception as exc:
            out["reason_if_failed"] = f"confirm: {exc}"
            _log(f"PGG2-LIVE-ATOMIC-ESB-CONFIRMED mint={mint} status=fail err={exc}")
            return out
        if not confirmed:
            out["reason_if_failed"] = "confirm_timeout"
            _log(f"PGG2-LIVE-ATOMIC-ESB-CONFIRMED mint={mint} status=timeout")
            return out
        _log(f"PGG2-LIVE-ATOMIC-ESB-CONFIRMED mint={mint} tx_sig={tx_sig}")

        # --- 8. Reconcile actual wallet delta ---
        try:
            actual = self._actual_wallet_delta(tx_sig)
        except Exception as exc:
            out["reason_if_failed"] = f"actual_wallet_delta: {exc}"
            _log(f"PGG2-LIVE-ATOMIC-ESB-WALLET-DELTA mint={mint} status=unavailable err={exc}")
            return out
        out["actual_wallet_delta_sol"] = float(actual)
        drift_actual = float(actual) - float(predicted_all_in_pnl)
        out["drift_sol"] = drift_actual
        _log(
            f"PGG2-LIVE-ATOMIC-ESB-WALLET-DELTA mint={mint} "
            f"actual_wallet_delta_sol={actual:+.6f}"
        )
        if abs(drift_actual) > self.max_pnl_drift_sol:
            out["reason_if_failed"] = (
                f"actual_drift_exceeded: drift={drift_actual:+.6f} "
                f"tolerance={self.max_pnl_drift_sol:.6f}"
            )
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-RECONCILE mint={mint} stage=actual status=fail "
                f"drift={drift_actual:+.6f}"
            )
            return out
        if actual < 0:
            out["reason_if_failed"] = f"actual_wallet_delta_negative: {actual:+.6f}"
            _log(
                f"PGG2-LIVE-ATOMIC-ESB-RECONCILE mint={mint} stage=actual status=fail "
                f"reason=negative_delta"
            )
            return out
        out["success"] = True
        _log(
            f"PGG2-LIVE-ATOMIC-ESB-RECONCILE mint={mint} stage=actual status=pass "
            f"drift={drift_actual:+.6f}"
        )
        return out

    # ------------------------------------------------------------------
    # Internals — broker delegates
    # ------------------------------------------------------------------
    def _simulate(self, signed_b64: str) -> dict[str, Any]:
        """Simulate the atomic tx via the broker's RPC.

        Returns dict with `ok` (bool), `wallet_delta_sol` (float|None),
        `cu_consumed` (int|None), `logs_tail` (list[str]), `err` (str|None).
        """
        if not hasattr(self.broker, "simulate_signed_atomic"):
            # Fall back to simulate_signed (existing method) but the
            # caller may not know to compute wallet delta from pre/post.
            if not hasattr(self.broker, "simulate_signed"):
                return {"ok": False, "err": "broker has no simulate_signed*"}
            try:
                sim_raw = self.broker.simulate_signed(signed_b64)
            except Exception as exc:
                return {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
            if sim_raw is None:
                return {"ok": True, "wallet_delta_sol": None, "cu_consumed": None, "logs_tail": [], "err": None}
            return self._parse_sim_result(sim_raw)
        # Preferred: broker exposes a method that returns the full meta.
        try:
            sim_raw = self.broker.simulate_signed_atomic(signed_b64)
        except Exception as exc:
            return {"ok": False, "err": f"{type(exc).__name__}: {exc}"}
        return self._parse_sim_result(sim_raw)

    @staticmethod
    def _parse_sim_result(sim_raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(sim_raw, dict):
            return {"ok": False, "err": f"unexpected_sim_shape: {type(sim_raw).__name__}"}
        if sim_raw.get("err"):
            return {
                "ok": False,
                "err": sim_raw["err"],
                "logs_tail": (sim_raw.get("logs") or [])[-10:],
            }
        pre = sim_raw.get("preBalances") or []
        post = sim_raw.get("postBalances") or []
        wallet_delta = None
        if pre and post and len(pre) > 0 and len(post) > 0:
            try:
                wallet_delta = (int(post[0]) - int(pre[0])) / 1_000_000_000
            except Exception:
                wallet_delta = None
        return {
            "ok": True,
            "wallet_delta_sol": wallet_delta,
            "cu_consumed": sim_raw.get("unitsConsumed"),
            "logs_tail": (sim_raw.get("logs") or [])[-10:],
            "err": None,
        }

    def _send(self, signed_b64: str) -> str:
        if not hasattr(self.broker, "send_signed_atomic"):
            if not hasattr(self.broker, "send_signed_transaction"):
                raise RuntimeError("broker has no send_signed_atomic / send_signed_transaction")
            return self.broker.send_signed_transaction(signed_b64)
        return self.broker.send_signed_atomic(signed_b64)

    def _confirm(self, tx_sig: str) -> bool:
        if not hasattr(self.broker, "confirm_signature"):
            return False
        deadline = time.time() + self.confirm_timeout_sec
        while time.time() < deadline:
            try:
                if self.broker.confirm_signature(tx_sig):
                    return True
            except Exception:
                pass
            time.sleep(self.confirm_poll_interval_sec)
        return False

    def _actual_wallet_delta(self, tx_sig: str) -> float:
        if not hasattr(self.broker, "wallet_delta_from_signatures"):
            raise RuntimeError(
                "broker.wallet_delta_from_signatures(buy_sig, sell_sig=None) not implemented"
            )
        # Atomic path: both legs are in the same tx, so we pass the same
        # signature for both. broker.wallet_delta_from_signatures must
        # tolerate this.
        return float(self.broker.wallet_delta_from_signatures(tx_sig, tx_sig))
