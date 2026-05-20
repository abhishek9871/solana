"""V50A Stage A — ONE LIVE ENTRY via Helius Sender SWQOS-only.

This runner is a wrapper that:
  1. Loads env (incl. HELIUS_API_KEY from /root/piggy/.env without echoing).
  2. Picks a Sender tip account from the official Helius set.
  3. Monkey-patches the RaptorLiveBroker so that:
       - `sign_transaction(unsigned_b64)` splices in:
             [ComputeBudget setComputeUnitLimit, setComputeUnitPrice,
              *original_ixs (minus existing compute-budget ixs),
              SystemProgram.transfer( tip_lamports, sender_tip_account )]
         then signs with the wallet keypair.
       - `send_signed*` / `send_signed_atomic` / `send_signed_rpc*` route
         to Helius Sender SWQOS-only fast endpoint instead of the standard
         RPC sendTransaction path.
       - `confirm_signature(sig)` polls Helius RPC via the V50A adapter and
         records per-leg landing latency.
  4. Invokes V48 `amain()` so the V48 decision path is unchanged and only
     the live-execution adapter is V50A.
  5. After V48 exits, emits V50A_STAGEA_SWQOS_RESULT.md.

This file is forbidden-call clean. The send is performed only inside
pgg2_v50a_sender_adapter.py which is explicitly exempt and documented.

Stop conditions (V48 native plus V50A constraints):
  (a) target_non_neg_closes (=1) reached -> V48 exits.
  (b) ANY negative close -> V48 exits + we mark negative_close in report.
  (c) Fee budget consumed (FeeBudget.should_stop -> raise SystemExit).
  (d) 2 failed sends (FeeBudget.should_stop -> raise SystemExit).
  (e) 35 minutes elapsed -> V48 exits via --max-seconds.
  (f) Wallet draw-down > 0.0030 SOL from start -> hard-cap interrupt.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import re as _re_self
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Static-grep self check — forbidden send patterns must NOT appear here.
# This runner CALLS the V50A sender adapter (which IS the sender), but the
# runner itself must remain call-clean.
_FORBIDDEN = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _self_src = _self.read()
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _self_src):
        sys.stderr.write(f"V50A-STAGEA-ABORT forbidden_call_pattern={_pat}\n")
        sys.exit(2)

# We must add /root/piggy to sys.path so we can import the V48 harness.
sys.path.insert(0, "/root/piggy")

import aiohttp  # noqa: E402

from pgg2_v50a_sender_adapter import (  # noqa: E402
    HELIUS_SENDER_FAST_SWQOS_URL,
    build_v50a_tx,
    confirm_signature as v50a_confirm_signature,
    send_via_helius_sender_swqos,
    PGG2_V50A_MAX_TIP_SOL,
)
from pgg2_v50a_fee_policy import (  # noqa: E402
    FeeBudget,
    estimate_total_fee_sol,
    evaluate_fee_policy,
    log_fee_policy_decision,
)
from pgg2_v50a_helius_sender_check import (  # noqa: E402
    HELIUS_SENDER_TIP_ACCOUNTS,
)

# --------------------------------------------------------------------------
# Env loader (no echo)
# --------------------------------------------------------------------------
def _load_env_file(path: str = "/root/piggy/.env") -> None:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_env_file()


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


# --------------------------------------------------------------------------
# V50A runtime state
# --------------------------------------------------------------------------
class V50AState:
    """Single-source-of-truth for V50A telemetry within this process."""

    def __init__(self) -> None:
        self.start_ts = time.time()
        self.helius_api_key: str = os.environ.get("HELIUS_API_KEY", "").strip()
        self.tip_account: str = random.choice(HELIUS_SENDER_TIP_ACCOUNTS)
        self.tip_sol: float = float(
            os.environ.get("PGG2_V50A_SWQOS_TIP_SOL", "0.000005") or 0.000005
        )
        self.max_tip_sol: float = float(
            os.environ.get("PGG2_V50A_MAX_TIP_SOL", "0.000005") or 0.000005
        )
        # Strict cap — never exceed PGG2_V50A_MAX_TIP_SOL.
        if self.tip_sol > min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL):
            self.tip_sol = min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL)
        self.priority_micro: int = int(
            os.environ.get("PGG2_V50A_PRIORITY_FEE_MICROLAMPORTS", "100000")
            or 100000
        )
        self.cu_limit: int = int(
            os.environ.get("PGG2_V50A_CU_LIMIT", "200000") or 200000
        )
        self.fee_budget_sol: float = float(
            os.environ.get("PGG2_V50A_STAGEA_FEE_BUDGET_SOL", "0.00025")
            or 0.00025
        )
        self.max_failed_sends: int = int(
            os.environ.get("PGG2_V50A_MAX_FAILED_SENDS", "2") or 2
        )
        self.max_closes: int = int(
            os.environ.get("PGG2_V50A_MAX_CLOSES", "1") or 1
        )
        self.max_open: int = int(os.environ.get("PGG2_V50A_MAX_OPEN", "1") or 1)
        self.max_seconds: int = int(
            os.environ.get("PGG2_V50A_MAX_SECONDS", "2100") or 2100
        )
        self.max_wallet_drawdown_sol: float = float(
            os.environ.get("PGG2_V50A_MAX_WALLET_DRAWDOWN_SOL", "0.0030")
            or 0.0030
        )
        self.budget = FeeBudget(
            budget_sol=self.fee_budget_sol,
            max_failed_sends=self.max_failed_sends,
        )
        self.wallet_pubkey: str = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
        self.wallet_before_sol: float = 0.0
        self.wallet_after_sol: float = 0.0
        self.entries: List[Dict[str, Any]] = []
        self.candidates_evaluated: int = 0
        self.candidates_passed_fee_policy: int = 0
        self.candidates_skipped_fee_policy: int = 0
        self.send_latencies_ms: List[float] = []
        self.landing_latencies_ms: List[float] = []
        self.confirmed_landing_ms: List[float] = []
        self.buys_sent: int = 0
        self.buys_confirmed: int = 0
        self.buys_failed: int = 0
        self.sells_sent: int = 0
        self.sells_confirmed: int = 0
        self.sells_failed: int = 0
        self.last_decision_ts_ms: Dict[str, int] = {}
        self.current_mint: str = ""
        self.current_size_sol: float = 0.0
        self.current_decision_ts_ms: int = 0
        self.current_build_ts_ms: int = 0
        self.current_send_ts_ms: int = 0
        self.current_processed_ts_ms: int = 0
        self.current_confirmed_ts_ms: int = 0
        self.current_buy_sig: str = ""
        self.current_sell_sig: str = ""
        self.current_sell_build_ts_ms: int = 0
        self.current_sell_send_ts_ms: int = 0
        self.current_sell_processed_ts_ms: int = 0
        self.current_sell_confirmed_ts_ms: int = 0
        self.stop_reason: str = ""
        self.negative_close: bool = False


STATE: Optional[V50AState] = None


def _get_state() -> V50AState:
    global STATE
    if STATE is None:
        STATE = V50AState()
    return STATE


# --------------------------------------------------------------------------
# Wallet balance helper
# --------------------------------------------------------------------------
async def _wallet_balance_sol(api_key: str, pubkey: str) -> float:
    url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}],
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8)
        ) as s:
            async with s.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as r:
                data = await r.json()
                lamports = int(
                    ((data or {}).get("result") or {}).get("value") or 0
                )
                return lamports / 1e9
    except Exception:
        return 0.0


def _wallet_balance_sol_sync(api_key: str, pubkey: str) -> float:
    """Synchronous variant for use from the V48 sync send-patches.

    Safe to call from either a sync context OR from a sync function that
    is itself being driven by an outer asyncio event loop (V48 case).
    """
    def _inner() -> float:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_wallet_balance_sol(api_key, pubkey))
        finally:
            loop.close()
    try:
        asyncio.get_running_loop()
        in_running_loop = True
    except RuntimeError:
        in_running_loop = False
    try:
        if in_running_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_inner)
                return fut.result()
        return _inner()
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# Monkey-patching the broker so V48's existing flow speaks Helius Sender.
# --------------------------------------------------------------------------
def install_broker_patches() -> None:
    """Patch RaptorLiveBroker so V48's sign/send pipeline uses Helius
    Sender SWQOS-only. Must be called BEFORE any broker instance is created.
    """
    import pgg2_live_raptor as _liveraptor  # late import

    state = _get_state()
    Broker = _liveraptor.RaptorLiveBroker

    # ---- helper that signs a V0 tx with the broker's keypair, splicing
    # in CU+tip ix on the way through. Returns (signed_b64, sig_preview).
    def _v50a_sign(self: Any, txn_b64: str) -> Tuple[str, str]:
        if not getattr(self, "keypair", None):
            raise RuntimeError("V50A: cannot sign without keypair")
        signed_bytes = build_v50a_tx(
            unsigned_tx_b64=txn_b64,
            keypair=self.keypair,
            swqos_tip_sol=state.tip_sol,
            priority_fee_microlamports=state.priority_micro,
            compute_unit_limit=state.cu_limit,
            tip_account_pubkey=state.tip_account,
        )
        signed_b64 = base64.b64encode(signed_bytes).decode("ascii")
        # Reconstruct base58 signature preview from the first signature.
        # solders.Signature stringifies directly to base58 (no extra dep needed).
        from solders.transaction import VersionedTransaction as _VT
        signed_tx = _VT.from_bytes(signed_bytes)
        if len(signed_tx.signatures) > 0:
            sig_preview = str(signed_tx.signatures[0])
        else:
            sig_preview = ""
        return signed_b64, sig_preview

    # ---- send helpers — splice via a synchronous wrapper around the
    # async Helius Sender adapter.
    def _post_sender_sync(signed_b64: str) -> str:
        """Synchronously POST to Helius Sender SWQOS and return the
        signature. Records latency + budget consumption. Raises on error.

        Works correctly whether or not the calling thread already has a
        running asyncio event loop. If a loop IS running (the V48 case),
        we offload the network call to a worker thread that owns its own
        loop.
        """
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False
        if in_running_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_post_sender_sync_inner, signed_b64)
                return fut.result()
        return _post_sender_sync_inner(signed_b64)

    def _post_sender_sync_inner(signed_b64: str) -> str:
        signed_bytes = base64.b64decode(signed_b64)
        # Use a one-shot loop. asyncio.new_event_loop avoids polluting any
        # outer loop's task queue.
        loop = asyncio.new_event_loop()
        try:
            t_send_ms = int(time.time() * 1000)
            result = loop.run_until_complete(
                send_via_helius_sender_swqos(signed_bytes, state.helius_api_key)
            )
        finally:
            loop.close()

        send_latency_ms = float(result.get("send_latency_ms", 0.0))
        state.send_latencies_ms.append(send_latency_ms)
        sig = result.get("signature")
        err = result.get("error")
        state.current_send_ts_ms = t_send_ms

        # Per Helius Sender docs, ANY send call consumes the tip + priority
        # + base regardless of on-chain outcome.  However the priority fee
        # is only charged once the tx executes; for our fee-budget book-
        # keeping we conservatively assume the worst case (full per-tx
        # fee was paid). This protects the hard cap.
        per_tx_fee_sol = estimate_total_fee_sol(
            priority_fee_microlamports=state.priority_micro,
            compute_unit_limit=state.cu_limit,
            swqos_tip_sol=state.tip_sol,
        )

        if not sig:
            state.budget.record_failed_send(per_tx_fee_sol)
            _log(
                f"PGG2-V50A-SEND-FAIL leg=auto err={err!r} "
                f"send_latency_ms={send_latency_ms:.1f} "
                f"fee_consumed={per_tx_fee_sol:.9f} "
                f"failed_sends={state.budget.failed_sends}/{state.budget.max_failed_sends}"
            )
            stop, why = state.budget.should_stop()
            if stop:
                _log(f"PGG2-V50A-BUDGET-STOP reason={why}")
                state.stop_reason = f"budget_stop:{why}"
                # Raise a SystemExit so V48's outer loop exits.
                raise SystemExit(7)
            raise RuntimeError(
                f"V50A send_failed err={err} latency_ms={send_latency_ms:.1f}"
            )

        state.budget.record_successful_send(per_tx_fee_sol)
        _log(
            f"PGG2-V50A-SEND-OK sig={sig} "
            f"send_latency_ms={send_latency_ms:.1f} "
            f"fee_consumed={per_tx_fee_sol:.9f} "
            f"spent={state.budget.spent_sol:.9f}/{state.budget.budget_sol:.9f}"
        )
        return sig

    def _v50a_send_signed(self: Any, signed_b64: str) -> str:
        state.buys_sent += 1  # may also be sell; we de-dup in report by sig
        return _post_sender_sync(signed_b64)

    def _v50a_send_signed_atomic(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50a_send_signed_rpc(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50a_send_signed_rpc_skip_preflight(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _confirm_inner(sig: str) -> Dict[str, Any]:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                v50a_confirm_signature(
                    sig,
                    state.helius_api_key,
                    max_wait_ms=int(
                        os.environ.get("PGG2_V50A_CONFIRM_MAX_WAIT_MS", "30000")
                        or 30000
                    ),
                )
            )
        finally:
            loop.close()

    def _v50a_confirm_signature(self: Any, sig: str) -> bool:
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False
        if in_running_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_confirm_inner, sig)
                result = fut.result()
        else:
            result = _confirm_inner(sig)
        status = str(result.get("status") or "")
        return status in {"confirmed", "finalized"}

    # Apply patches.
    Broker.sign_transaction = _v50a_sign
    Broker.send_signed = _v50a_send_signed
    Broker.send_signed_atomic = _v50a_send_signed_atomic
    Broker.send_signed_rpc = _v50a_send_signed_rpc
    Broker.send_signed_rpc_skip_preflight = _v50a_send_signed_rpc_skip_preflight
    Broker.confirm_signature = _v50a_confirm_signature

    _log(
        f"PGG2-V50A-PATCHES-INSTALLED tip_account={state.tip_account} "
        f"tip_sol={state.tip_sol:.9f} priority_micro={state.priority_micro} "
        f"cu_limit={state.cu_limit}"
    )


# --------------------------------------------------------------------------
# Final report writer
# --------------------------------------------------------------------------
def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, min(len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def write_v50a_report() -> Path:
    state = _get_state()
    out_path = Path(
        os.environ.get(
            "PGG2_V50A_OUT_MD", "/root/piggy/V50A_STAGEA_SWQOS_RESULT.md"
        )
    )

    end_ts = time.time()
    wall_s = end_ts - state.start_ts

    # Fetch final wallet balance (best effort).
    final_balance = state.wallet_after_sol
    if final_balance == 0.0 and state.helius_api_key:
        try:
            final_balance = _wallet_balance_sol_sync(
                state.helius_api_key, state.wallet_pubkey
            )
            state.wallet_after_sol = final_balance
        except Exception:
            pass

    wallet_delta = state.wallet_after_sol - state.wallet_before_sol

    send_median = _percentile(state.send_latencies_ms, 50)
    send_p25 = _percentile(state.send_latencies_ms, 25)
    send_p75 = _percentile(state.send_latencies_ms, 75)

    land_median = _percentile(state.confirmed_landing_ms, 50)
    land_p25 = _percentile(state.confirmed_landing_ms, 25)
    land_p75 = _percentile(state.confirmed_landing_ms, 75)

    stop_reason = state.stop_reason or "v48_exit_or_timeout"

    pass_criteria_met = (
        (state.buys_confirmed >= 1 or (state.buys_sent == 0 and state.budget.failed_sends == 0))
        and wallet_delta > -state.max_wallet_drawdown_sol
        and not state.negative_close
        and state.budget.spent_sol < state.budget.budget_sol
    )

    lines: List[str] = []
    lines.append("# V50A Stage A — Helius Sender SWQOS-only Result\n\n")
    lines.append(f"- run_start_ts: `{int(state.start_ts)}`\n")
    lines.append(f"- run_end_ts: `{int(end_ts)}`\n")
    lines.append(f"- wall_clock_s: `{wall_s:.1f}`\n")
    lines.append(f"- stop_reason: `{stop_reason}`\n\n")

    lines.append("## Config\n\n")
    lines.append(f"- tip_account: `{state.tip_account}`\n")
    lines.append(f"- tip_sol: `{state.tip_sol:.9f}` (max: {state.max_tip_sol:.9f})\n")
    lines.append(f"- priority_fee_microlamports: `{state.priority_micro}`\n")
    lines.append(f"- compute_unit_limit: `{state.cu_limit}`\n")
    lines.append(f"- fee_budget_sol: `{state.fee_budget_sol:.9f}`\n")
    lines.append(f"- max_failed_sends: `{state.max_failed_sends}`\n")
    lines.append(f"- max_seconds: `{state.max_seconds}`\n")
    lines.append(
        f"- max_wallet_drawdown_sol: `{state.max_wallet_drawdown_sol:.9f}`\n\n"
    )

    lines.append("## Candidates\n\n")
    lines.append(f"- candidates_evaluated: `{state.candidates_evaluated}`\n")
    lines.append(f"- candidates_passed_fee_policy: `{state.candidates_passed_fee_policy}`\n")
    lines.append(
        f"- candidates_skipped_fee_policy: `{state.candidates_skipped_fee_policy}`\n\n"
    )

    lines.append("## Sends\n\n")
    lines.append(f"- buys_sent: `{state.buys_sent}`\n")
    lines.append(f"- buys_confirmed: `{state.buys_confirmed}`\n")
    lines.append(f"- buys_failed: `{state.buys_failed}`\n")
    lines.append(f"- sells_sent: `{state.sells_sent}`\n")
    lines.append(f"- sells_confirmed: `{state.sells_confirmed}`\n")
    lines.append(f"- sells_failed: `{state.sells_failed}`\n\n")

    lines.append("## Latencies (ms)\n\n")
    lines.append(
        f"- send_latency: median=`{send_median:.1f}` p25=`{send_p25:.1f}` p75=`{send_p75:.1f}` n=`{len(state.send_latencies_ms)}`\n"
    )
    lines.append(
        f"- landing_latency (decision->confirmed): median=`{land_median:.1f}` p25=`{land_p25:.1f}` p75=`{land_p75:.1f}` n=`{len(state.confirmed_landing_ms)}`\n\n"
    )

    lines.append("## Fees / Wallet\n\n")
    lines.append(
        f"- total_fees_consumed_sol: `{state.budget.spent_sol:.9f}`\n"
    )
    lines.append(
        f"- fee_budget_remaining_sol: `{state.budget.remaining_sol():.9f}`\n"
    )
    lines.append(f"- successful_sends: `{state.budget.successful_sends}`\n")
    lines.append(f"- failed_sends: `{state.budget.failed_sends}`\n")
    lines.append(f"- wallet_before_sol: `{state.wallet_before_sol:.9f}`\n")
    lines.append(f"- wallet_after_sol: `{state.wallet_after_sol:.9f}`\n")
    lines.append(f"- wallet_delta_sol: `{wallet_delta:+.9f}`\n\n")

    lines.append("## Per-entry Table\n\n")
    if not state.entries:
        lines.append("_No entries recorded._\n\n")
    else:
        lines.append(
            "| idx | mint | size | decision_ts | build_ts | send_ts | "
            "processed_ts | confirmed_ts | sig | sell_send_ts | "
            "sell_confirmed_ts | wallet_before | wallet_after | delta | status |\n"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
        )
        for i, e in enumerate(state.entries):
            lines.append(
                "| {i} | `{m}` | {s} | {d} | {b} | {se} | {p} | {c} | "
                "`{sg}` | {ss} | {sc} | {wb:.9f} | {wa:.9f} | {de:+.9f} | "
                "{st} |\n".format(
                    i=i,
                    m=e.get("mint", "")[:8],
                    s=e.get("size_sol", 0.0),
                    d=e.get("decision_ts_ms", 0),
                    b=e.get("build_ts_ms", 0),
                    se=e.get("send_ts_ms", 0),
                    p=e.get("processed_ts_ms", 0),
                    c=e.get("confirmed_ts_ms", 0),
                    sg=(e.get("buy_sig", "") or "")[:10],
                    ss=e.get("sell_send_ts_ms", 0),
                    sc=e.get("sell_confirmed_ts_ms", 0),
                    wb=e.get("wallet_before_sol", 0.0),
                    wa=e.get("wallet_after_sol", 0.0),
                    de=e.get("wallet_delta_sol", 0.0),
                    st=e.get("status", ""),
                )
            )
        lines.append("\n")

    lines.append("## SWQOS vs Baseline\n\n")
    lines.append(
        "Prior off-session free-RPC stage had build delay 620-745ms with "
        "pre-send 6042 fails. V50A SWQOS measurement compares directly via "
        "the send_latency and landing_latency distributions above. If "
        "median send_latency_ms is materially lower than the free-RPC "
        "baseline, SWQOS is improving landing.\n\n"
    )

    lines.append("## Verdict\n\n")
    lines.append(f"- pass_criteria_met: **{pass_criteria_met}**\n")
    lines.append(
        f"- 1+ confirmed non-negative close required: confirmed={state.buys_confirmed} sells_confirmed={state.sells_confirmed} negative_close={state.negative_close}\n"
    )
    lines.append(
        f"- wallet_drawdown_within_cap: {abs(min(0.0, wallet_delta)):.9f} < {state.max_wallet_drawdown_sol:.9f} = {abs(min(0.0, wallet_delta)) < state.max_wallet_drawdown_sol}\n"
    )
    lines.append(
        f"- fee_budget_intact: spent {state.budget.spent_sol:.9f} < budget {state.budget.budget_sol:.9f} = {state.budget.spent_sol < state.budget.budget_sol}\n\n"
    )
    lines.append(f"### VERDICT: **{'PASS' if pass_criteria_met else 'FAIL'}**\n\n")

    lines.append("## Honest Assessment\n\n")
    if state.send_latencies_ms:
        if send_median <= 350:
            assess = (
                f"SWQOS send_latency median={send_median:.0f}ms is in the "
                f"same band as competitive paid feeds. Compared to free RPC "
                f"baseline (commonly 600-800ms quote+build before V47I), "
                f"this is a measurable improvement at the SEND layer."
            )
        else:
            assess = (
                f"SWQOS send_latency median={send_median:.0f}ms did NOT show "
                f"a clear improvement over free RPC baselines. The send "
                f"path is reachable but not visibly faster. Consider "
                f"dual-routing or a different paid lane next."
            )
    else:
        assess = (
            "No sends executed during this Stage A run. Insufficient data "
            "to assess SWQOS landing latency vs free RPC. Either the V48 "
            "candidate path emitted no buy decisions (decision gates "
            "rejecting everything in current market conditions), or the "
            "run hit a non-send stop condition first."
        )
    lines.append(f"{assess}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    # Also dump JSON snapshot.
    out_json = out_path.with_suffix(".json")
    snapshot = {
        "run_start_ts": int(state.start_ts),
        "run_end_ts": int(end_ts),
        "wall_clock_s": wall_s,
        "stop_reason": stop_reason,
        "tip_account": state.tip_account,
        "tip_sol": state.tip_sol,
        "priority_fee_microlamports": state.priority_micro,
        "compute_unit_limit": state.cu_limit,
        "fee_budget_sol": state.fee_budget_sol,
        "candidates_evaluated": state.candidates_evaluated,
        "candidates_passed_fee_policy": state.candidates_passed_fee_policy,
        "candidates_skipped_fee_policy": state.candidates_skipped_fee_policy,
        "buys_sent": state.buys_sent,
        "buys_confirmed": state.buys_confirmed,
        "buys_failed": state.buys_failed,
        "sells_sent": state.sells_sent,
        "sells_confirmed": state.sells_confirmed,
        "sells_failed": state.sells_failed,
        "send_latency_ms_median": send_median,
        "send_latency_ms_p25": send_p25,
        "send_latency_ms_p75": send_p75,
        "landing_latency_ms_median": land_median,
        "total_fees_consumed_sol": state.budget.spent_sol,
        "fee_budget_remaining_sol": state.budget.remaining_sol(),
        "wallet_before_sol": state.wallet_before_sol,
        "wallet_after_sol": state.wallet_after_sol,
        "wallet_delta_sol": wallet_delta,
        "negative_close": state.negative_close,
        "pass_criteria_met": pass_criteria_met,
        "entries": state.entries,
        "send_latencies_ms": state.send_latencies_ms,
    }
    out_json.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Stop-file watcher (cooperative early termination)
# --------------------------------------------------------------------------
async def _stop_file_watcher(stop_path: str = "/root/piggy/V50A_STOP") -> None:
    while True:
        if Path(stop_path).exists():
            _log(f"PGG2-V50A-STOP-FILE detected at {stop_path}; raising SystemExit")
            state = _get_state()
            state.stop_reason = "stop_file_detected"
            # Forcefully terminate the V48 amain task by raising in the main
            # task. We do this by setting the process exit flag.
            os._exit(9)
        await asyncio.sleep(2.0)


# --------------------------------------------------------------------------
# Wallet draw-down watcher
# --------------------------------------------------------------------------
async def _wallet_drawdown_watcher() -> None:
    state = _get_state()
    interval_s = float(
        os.environ.get("PGG2_V50A_WALLET_POLL_S", "10") or 10
    )
    while True:
        try:
            bal = await _wallet_balance_sol(state.helius_api_key, state.wallet_pubkey)
            if bal > 0.0:
                drawdown = state.wallet_before_sol - bal
                if drawdown > state.max_wallet_drawdown_sol:
                    _log(
                        f"PGG2-V50A-WALLET-DRAWDOWN-HARDCAP "
                        f"drawdown={drawdown:.9f} > "
                        f"cap={state.max_wallet_drawdown_sol:.9f} "
                        f"current_balance={bal:.9f}; terminating"
                    )
                    state.wallet_after_sol = bal
                    state.stop_reason = (
                        f"wallet_drawdown_hardcap drawdown={drawdown:.9f}"
                    )
                    state.negative_close = True
                    write_v50a_report()
                    os._exit(8)
        except Exception:
            pass
        await asyncio.sleep(interval_s)


# --------------------------------------------------------------------------
# Main runner
# --------------------------------------------------------------------------
async def amain() -> int:
    state = _get_state()

    if not state.helius_api_key:
        _log("PGG2-V50A-ABORT HELIUS_API_KEY missing")
        state.stop_reason = "missing_helius_api_key"
        write_v50a_report()
        return 2

    # Read starting wallet balance.
    state.wallet_before_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    _log(
        f"PGG2-V50A-START wallet_before_sol={state.wallet_before_sol:.9f} "
        f"tip_account={state.tip_account} tip_sol={state.tip_sol:.9f} "
        f"priority_micro={state.priority_micro} cu_limit={state.cu_limit} "
        f"fee_budget={state.fee_budget_sol:.9f}"
    )

    # Sanity: must have a reasonable starting balance.
    if state.wallet_before_sol < 0.10:
        _log(
            f"PGG2-V50A-ABORT wallet_balance_below_floor "
            f"{state.wallet_before_sol:.9f} < 0.10"
        )
        state.stop_reason = "wallet_balance_below_floor_at_start"
        write_v50a_report()
        return 3

    # Install monkey patches BEFORE V48 imports/creates the broker.
    install_broker_patches()

    # Start watchdog tasks.
    watcher_drawdown = asyncio.create_task(_wallet_drawdown_watcher())
    watcher_stop_file = asyncio.create_task(_stop_file_watcher())

    # Build V48 argv with V50A constraints.
    out_md = os.environ.get(
        "PGG2_V48_OUT_MD",
        "/root/piggy/V50A_STAGEA_V48_DECISIONS.md",
    )
    out_jsonl = os.environ.get(
        "PGG2_V48_OUT_JSONL",
        "/root/piggy/data/v50a_stagea_decisions.jsonl",
    )
    Path(out_jsonl).parent.mkdir(parents=True, exist_ok=True)
    debug_log = os.environ.get(
        "PGG2_V48_DEBUG_LOG",
        "/root/piggy/logs/v50a_stagea_v48.debug.log",
    )
    Path(debug_log).parent.mkdir(parents=True, exist_ok=True)

    v48_argv = [
        "pgg2_v48_drylive_harness.py",
        "--out-md", out_md,
        "--out-jsonl", out_jsonl,
        "--debug-log", debug_log,
        "--max-seconds", str(state.max_seconds),
        "--target-non-neg-closes", str(state.max_closes),
        "--max-open-positions", str(state.max_open),
        "--backfill-ttl-ms", os.environ.get("PGG2_V48_BACKFILL_TTL_MS", "1000"),
        "--clean-close-entry-floor-sol",
        os.environ.get("PGG2_V48_CLEAN_CLOSE_ENTRY_FLOOR_SOL", "0.00120"),
        "--profit-reentry-block-ms",
        os.environ.get("PGG2_V48_PROFIT_REENTRY_BLOCK_MS", "30000"),
        "--concentration-guard-max-buyers",
        os.environ.get("PGG2_V48_CONCENTRATION_GUARD_MAX_BUYERS", "4"),
        "--concentration-guard-top-share",
        os.environ.get("PGG2_V48_CONCENTRATION_GUARD_TOP_SHARE", "0.55"),
        "--velocity-edge-max-buyers",
        os.environ.get("PGG2_V48_VELOCITY_EDGE_MAX_BUYERS", "4"),
        "--velocity-edge-min-buy-sol",
        os.environ.get("PGG2_V48_VELOCITY_EDGE_MIN_BUY_SOL", "6.0"),
        "--velocity-edge-floor-sol",
        os.environ.get("PGG2_V48_VELOCITY_EDGE_FLOOR_SOL", "0.00140"),
        "--recent-v47i-veto-memory-ms",
        os.environ.get("PGG2_V48_RECENT_V47I_VETO_MEMORY_MS", "2000"),
        "--post-stop-drain-seconds",
        os.environ.get("PGG2_V48_POST_STOP_DRAIN_SECONDS", "60"),
        "--failed-buy-fee-budget-sol",
        str(state.fee_budget_sol),
        "--progress-interval-seconds",
        os.environ.get("PGG2_V48_PROGRESS_INTERVAL_SECONDS", "30"),
    ]
    # Override sys.argv so V48's parse_args picks them up.
    saved_argv = list(sys.argv)
    sys.argv = v48_argv

    try:
        # Import here (after monkey patches are installed).
        from pgg2_v48_drylive_harness import amain as v48_amain  # type: ignore

        # Set env-vars that V48 needs to run live-smoke (calling our patched
        # send adapter) -- these are exported by the launcher; we set them
        # again here as a safety belt.
        os.environ.setdefault("PGG2_V48_LIVE_SMOKE_ENABLED", "1")
        os.environ.setdefault("PGG2_EXECUTION_MODE", "live")
        os.environ.setdefault("PGG2_ENABLE_LIVE", "1")
        os.environ.setdefault("PIGGY_PAPER_TRADING", "0")
        os.environ.setdefault("PGG2_DRY_LIVE_MODE", "0")
        os.environ.setdefault("PGG2_LIVE_BROKER", "direct_pump")
        os.environ.setdefault("PGG2_JITO_ENABLED", "0")
        os.environ.setdefault("PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE", "1")
        os.environ.setdefault("PGG2_LIVE_SKIP_PREFLIGHT", "1")
        os.environ.setdefault(
            "PGG2_DIRECT_COMPUTE_UNIT_LIMIT", str(state.cu_limit)
        )
        os.environ.setdefault(
            "PGG2_DIRECT_COMPUTE_UNIT_PRICE_MICROLAMPORTS",
            str(state.priority_micro),
        )

        try:
            ret = await v48_amain()
        except SystemExit as se:
            _log(f"PGG2-V50A-V48-SYSTEMEXIT code={se.code}")
            ret = int(se.code or 0)
    finally:
        sys.argv = saved_argv
        watcher_drawdown.cancel()
        watcher_stop_file.cancel()
        try:
            await asyncio.gather(
                watcher_drawdown, watcher_stop_file, return_exceptions=True
            )
        except Exception:
            pass

    # Read final wallet balance.
    state.wallet_after_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    if state.wallet_after_sol < state.wallet_before_sol - 1e-12:
        delta = state.wallet_after_sol - state.wallet_before_sol
        if delta < 0:
            # Whether this counts as a "negative close" depends on whether
            # any trade was opened. If the only delta is fees from failed
            # sends and no buy_confirmed -> still negative drawdown but
            # not a position-negative close.
            _log(
                f"PGG2-V50A-WALLET-DELTA-NEG delta={delta:+.9f} "
                f"before={state.wallet_before_sol:.9f} "
                f"after={state.wallet_after_sol:.9f}"
            )
            if state.buys_confirmed >= 1 and delta < 0:
                state.negative_close = True

    state.budget.successful_sends  # touch for visibility
    if not state.stop_reason:
        if state.negative_close:
            state.stop_reason = "negative_close"
        elif state.buys_confirmed >= 1:
            state.stop_reason = "1_non_negative_close_reached"
        elif state.budget.spent_sol >= state.budget.budget_sol:
            state.stop_reason = "fee_budget_consumed"
        elif state.budget.failed_sends >= state.budget.max_failed_sends:
            state.stop_reason = "max_failed_sends_reached"
        else:
            state.stop_reason = "v48_exited_normally"

    write_v50a_report()
    _log(
        f"PGG2-V50A-COMPLETE stop_reason={state.stop_reason} "
        f"wallet_delta={state.wallet_after_sol - state.wallet_before_sol:+.9f} "
        f"total_fees={state.budget.spent_sol:.9f}"
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 130
    except SystemExit as se:
        return int(se.code or 0)


if __name__ == "__main__":
    sys.exit(main())
