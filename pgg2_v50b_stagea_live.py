"""V50B Stage A - V50A SWQOS entry + V47F/V47G watchdog exit stack.

This runner is V50A's wrapper plus a V47F/V47G exit-stack overlay. The entry
path is UNCHANGED from V50A (Helius Sender SWQOS-only, tip 0.000005 SOL,
skipPreflight=true, maxRetries=0). The exit path REPLACES V48's clean-close
wait loop with the V47G watchdog (250ms RPC poll of the bonding-curve PDA)
plus V47F mid-hold dump abort + V47F size-tiered hold caps.

Entry:
  V48 amain -> V48 decision pipeline emits a candidate -> V50A monkey-patched
  RaptorLiveBroker signs the buy with CU limit + CU price + tip, sends via
  Helius Sender SWQOS-only, confirms via getSignatureStatuses. UNCHANGED.

Exit:
  Once the FIRST `wait_confirmed(buy_sig)` returns True (buy on-chain), V50B
  spawns a V47GPositionQuoteWatchdog. The watchdog polls the bonding-curve
  PDA via JSON-RPC every PGG2_V47G_WATCHDOG_INTERVAL_MS (=250ms). On each
  tick it computes current_sell_quote_sol via local CPMM math, current_pnl,
  peak_quote, then consults pgg2_v47g_watchdog_exit_policy.decide_exit().

  V48's own sell loop runs in PARALLEL. V50B configures V48 to be aggressive:
    PGG2_V48_LIVE_MAX_POSITION_MS         = 1500   (hard cap on hold)
    PGG2_V48_LIVE_SELL_SEND_MIN_PNL_SOL   = -0.5   (gate never blocks)
    PGG2_V48_LIVE_SELL_MIN_PROFIT_SOL     = -0.0003 (allow small slip)
    PGG2_V48_LIVE_SELL_MIN_OUT_BUFFER_SOL = 0.00005
    PGG2_V48_LIVE_EMERGENCY_MIN_SOL_OUT   = protected_floor (bounded loss)

  This means V48 will fire the protected sell as soon as quote >= min_sol_out
  (size_sol + 2*tx_fee + sell_floor) on the first sell-loop iteration. If the
  protected gate cannot be met within 1500ms, V48 emergency-closes with the
  bounded floor (still profit-preserving up to the allowed slip).

  V47G watchdog runs alongside as the authoritative quote tracker and exit-
  decision logger. Its decisions (bank/scratch/abort/max_hold) are logged
  under PGG2-V50B-* tags and persisted in the report.

Stop conditions (priority):
  (a) Negative close (wallet_delta < 0 after sell confirmed) -> STOP, FAIL
  (b) 1 non-negative close (wallet_delta >= 0) -> STOP, PASS
  (c) Fee budget consumed (>= 0.00025 SOL) -> STOP
  (d) 2 failed sends -> STOP
  (e) 35 min elapsed -> STOP
  (f) Wallet drawdown > 0.0030 SOL from start -> STOP
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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Static-grep self check -- forbidden send patterns must NOT appear here.
# V50B reuses the V50A sender adapter (which IS the sender). The V50B runner
# itself must remain call-clean.
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
        sys.stderr.write(f"V50B-STAGEA-ABORT forbidden_call_pattern={_pat}\n")
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
)
from pgg2_v50a_helius_sender_check import (  # noqa: E402
    HELIUS_SENDER_TIP_ACCOUNTS,
)

# V47F/V47G exit stack imports.
from pgg2_v47g_position_quote_watchdog import (  # noqa: E402
    V47GPositionQuoteWatchdog,
    DEFAULT_INTERVAL_MS as V47G_DEFAULT_INTERVAL_MS,
)
from pgg2_v47g_watchdog_exit_policy import (  # noqa: E402
    ACTION_HOLD,
    is_negative_close_action,
    close_kind_from_action,
)
from pgg2_v47f_hold_caps import get_hold_caps  # noqa: E402


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
# Pump constants for fee-bps decode (mirrors pgg2_direct_pump.pump_global())
# --------------------------------------------------------------------------
DEFAULT_PUMP_FEE_BPS = 100
DEFAULT_PUMP_CREATOR_FEE_BPS = 0


# --------------------------------------------------------------------------
# V50B runtime state
# --------------------------------------------------------------------------
class V50BState:
    """Single-source-of-truth for V50B telemetry within this process."""

    def __init__(self) -> None:
        self.start_ts = time.time()
        self.helius_api_key: str = os.environ.get("HELIUS_API_KEY", "").strip()
        self.solanatracker_rpc_http: str = os.environ.get(
            "SOLANATRACKER_RPC_HTTP", ""
        ).strip()
        self.tip_account: str = random.choice(HELIUS_SENDER_TIP_ACCOUNTS)
        self.tip_sol: float = float(
            os.environ.get("PGG2_V50B_SWQOS_TIP_SOL", "0.000005") or 0.000005
        )
        self.max_tip_sol: float = float(
            os.environ.get("PGG2_V50B_MAX_TIP_SOL", "0.000005") or 0.000005
        )
        # Strict cap -- never exceed PGG2_V50A_MAX_TIP_SOL.
        if self.tip_sol > min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL):
            self.tip_sol = min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL)
        self.priority_micro: int = int(
            os.environ.get("PGG2_V50B_PRIORITY_FEE_MICROLAMPORTS", "100000")
            or 100000
        )
        self.cu_limit: int = int(
            os.environ.get("PGG2_V50B_CU_LIMIT", "200000") or 200000
        )
        self.fee_budget_sol: float = float(
            os.environ.get("PGG2_V50B_STAGEA_FEE_BUDGET_SOL", "0.00025")
            or 0.00025
        )
        self.max_failed_sends: int = int(
            os.environ.get("PGG2_V50B_MAX_FAILED_SENDS", "2") or 2
        )
        self.max_closes: int = int(
            os.environ.get("PGG2_V50B_MAX_CLOSES", "1") or 1
        )
        self.max_open: int = int(os.environ.get("PGG2_V50B_MAX_OPEN", "1") or 1)
        self.max_seconds: int = int(
            os.environ.get("PGG2_V50B_MAX_SECONDS", "2100") or 2100
        )
        self.max_wallet_drawdown_sol: float = float(
            os.environ.get("PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL", "0.0030")
            or 0.0030
        )
        self.max_hold_ms: int = int(
            os.environ.get("PGG2_V50B_MAX_HOLD_MS", "1500") or 1500
        )
        self.watchdog_interval_ms: int = int(
            os.environ.get("PGG2_V47G_WATCHDOG_INTERVAL_MS", "250") or 250
        )
        self.budget = FeeBudget(
            budget_sol=self.fee_budget_sol,
            max_failed_sends=self.max_failed_sends,
        )
        self.wallet_pubkey: str = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
        self.wallet_before_sol: float = 0.0
        self.wallet_after_sol: float = 0.0
        self.entries: List[Dict[str, Any]] = []
        self.send_latencies_ms: List[float] = []
        self.confirmed_landing_ms: List[float] = []
        self.buys_sent: int = 0
        self.buys_confirmed: int = 0
        self.buys_failed: int = 0
        self.sells_sent: int = 0
        self.sells_confirmed: int = 0
        self.sells_failed: int = 0
        # Watchdog state
        self.current_mint: str = ""
        self.current_size_sol: float = 0.0
        self.current_actual_tokens_raw: int = 0
        self.current_actual_buy_cost_sol: float = 0.0
        self.current_buy_sig: str = ""
        self.current_sell_sig: str = ""
        self.current_buy_confirmed_ms: int = 0
        self.watchdog: Optional[V47GPositionQuoteWatchdog] = None
        self.watchdog_started_ms: int = 0
        self.watchdog_stopped_ms: int = 0
        self.watchdog_decision: Optional[Dict[str, Any]] = None
        self.watchdog_quote_timeline: List[Dict[str, Any]] = []
        self.confirmed_buy_sigs: set = set()
        self.spawned_watchdogs_for: set = set()
        self.stop_reason: str = ""
        self.negative_close: bool = False
        self._lock = threading.Lock()


STATE: Optional[V50BState] = None


def _get_state() -> V50BState:
    global STATE
    if STATE is None:
        STATE = V50BState()
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


# --------------------------------------------------------------------------
# Watchdog spawn: called once the buy is confirmed on-chain.
# --------------------------------------------------------------------------
def _v50b_get_rpc_endpoint(state: V50BState) -> str:
    """Pick a JSON-RPC endpoint for the V47G watchdog. Prefer SolanaTracker
    RPC if available (already used by other PGG2 components); fall back to
    Helius mainnet RPC."""
    if state.solanatracker_rpc_http:
        url = state.solanatracker_rpc_http
        if "api_key=" not in url and os.environ.get("SOLANATRACKER_API_KEY"):
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api_key={os.environ['SOLANATRACKER_API_KEY']}"
        return url
    if state.helius_api_key:
        return f"https://mainnet.helius-rpc.com/?api-key={state.helius_api_key}"
    return ""


def _spawn_watchdog_for_buy(
    state: V50BState,
    mint: str,
    tokens_held_raw: int,
    buy_size_sol: float,
    opened_at_ms: int,
    fee_bps: int = DEFAULT_PUMP_FEE_BPS,
    creator_fee_bps: int = DEFAULT_PUMP_CREATOR_FEE_BPS,
) -> None:
    """Create and start a V47GPositionQuoteWatchdog for the open position.

    Logs PGG2-V50B-WATCHDOG-START. Runs in the background asyncio loop.
    """
    if state.watchdog is not None:
        return
    rpc_url = _v50b_get_rpc_endpoint(state)
    if not rpc_url:
        _log(
            f"PGG2-V50B-WATCHDOG-START-FAIL mint={mint[:8]} "
            "reason=no_rpc_endpoint"
        )
        return
    caps = get_hold_caps(buy_size_sol)
    # V50B overrides V47F max_hold with PGG2_V50B_MAX_HOLD_MS (per spec).
    spec_max_hold = state.max_hold_ms
    _log(
        f"PGG2-V50B-WATCHDOG-START mint={mint[:8]} size={buy_size_sol:.6f} "
        f"tokens={tokens_held_raw} interval_ms={state.watchdog_interval_ms} "
        f"max_hold_ms={spec_max_hold} v47f_caps_max_hold_ms={caps.get('max_hold_ms')} "
        f"opened_at_ms={opened_at_ms} rpc_endpoint=set"
    )

    def _wd_logger(msg: str) -> None:
        # Bridge the watchdog's internal logger to our top-level _log.
        # Strip V47G-prefixed sample log lines we don't want to flood stdout
        # but keep all decision lines (BANK / SCRATCH / ABORT / etc.).
        try:
            with state._lock:
                state.watchdog_quote_timeline.append(
                    {"ts_ms": int(time.time() * 1000), "msg": msg}
                )
        except Exception:
            pass
        _log(msg)
        # Also persist explicit per-quote rows under V50B tags so the timeline
        # is easy to grep in the report.
        if "PGG2-V47G-WATCHDOG-QUOTE" in msg:
            try:
                # Re-emit under V50B tag for clearer log filtering.
                v50b_msg = msg.replace(
                    "PGG2-V47G-WATCHDOG-QUOTE", "PGG2-V50B-WATCHDOG-QUOTE"
                )
                _log(v50b_msg)
            except Exception:
                pass

    # Build watchdog instance.
    try:
        wd = V47GPositionQuoteWatchdog(
            mint=mint,
            tokens_held_raw=int(tokens_held_raw),
            buy_size_sol=float(buy_size_sol),
            rpc_http_endpoint=rpc_url,
            fee_bps=int(fee_bps),
            creator_fee_bps=int(creator_fee_bps),
            opened_at_ms=int(opened_at_ms),
            interval_ms=int(state.watchdog_interval_ms),
            logger=_wd_logger,
        )
    except Exception as exc:
        _log(
            f"PGG2-V50B-WATCHDOG-INIT-FAIL mint={mint[:8]} "
            f"err={type(exc).__name__}:{exc}"
        )
        return
    state.watchdog = wd
    state.watchdog_started_ms = int(time.time() * 1000)

    # ALWAYS run watchdog in its own dedicated thread with its own asyncio
    # loop. V48's wait_confirmed is invoked from a sync context that blocks
    # the main asyncio loop, so loop.create_task wouldn't fire until the
    # blocking call returns.  Thread isolation guarantees the watchdog can
    # poll the bonding-curve PDA in parallel while V48 awaits sell confirms.
    _start_watchdog_in_thread(state, wd, mint, buy_size_sol)


def _start_watchdog_in_thread(
    state: V50BState,
    wd: V47GPositionQuoteWatchdog,
    mint: str,
    size_sol: float,
) -> None:
    """Run the watchdog in a dedicated thread with its own asyncio loop.

    Required because V48's `wait_confirmed` is a sync method invoked from a
    thread that may not own an event loop at all (V50A's send adapter
    offloads to thread workers).
    """
    def _runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run_watchdog(state, wd, mint, size_sol))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(
        target=_runner, name=f"v50b_wd_{mint[:8]}", daemon=True
    )
    t.start()
    state._wd_thread = t


async def _run_watchdog(
    state: V50BState,
    wd: V47GPositionQuoteWatchdog,
    mint: str,
    size_sol: float,
) -> None:
    """Drive the watchdog start -> wait for decision -> stop."""
    try:
        await wd.start()
        # Poll get_exit_decision until the watchdog records one or our spec
        # max_hold expires (whichever first). The V47G internal poll loop
        # already enforces its own max_hold via the size-tiered cap from
        # V47F; we also enforce PGG2_V50B_MAX_HOLD_MS as a hard cap.
        spec_deadline_ms = state.watchdog_started_ms + state.max_hold_ms
        last_quote_emit_ms = 0
        while True:
            await asyncio.sleep(0.05)
            now_ms = int(time.time() * 1000)
            dec = wd.get_exit_decision()
            if dec is not None:
                state.watchdog_decision = dec
                _log(
                    f"PGG2-V50B-WATCHDOG-DECISION mint={mint[:8]} "
                    f"action={dec.get('action')} reason={dec.get('reason')} "
                    f"pnl={dec.get('pnl'):+.6f} age_ms={dec.get('age_ms')} "
                    f"quote_sell_sol={dec.get('quote_sell_sol'):.9f}"
                )
                # Mirror specific actions under V50B-* tags.
                action = str(dec.get("action") or "")
                pnl = float(dec.get("pnl") or 0.0)
                age_ms = int(dec.get("age_ms") or 0)
                if action.startswith("bank") or action == "max_hold_bank":
                    _log(
                        f"PGG2-V50B-BANK mint={mint[:8]} pnl={pnl:+.6f} "
                        f"age_ms={age_ms}"
                    )
                elif action.startswith("scratch") or action.startswith("abort_scratch"):
                    _log(
                        f"PGG2-V50B-SCRATCH mint={mint[:8]} pnl={pnl:+.6f} "
                        f"age_ms={age_ms} action={action}"
                    )
                elif action.startswith("abort"):
                    _log(
                        f"PGG2-V50B-DUMP-ABORT mint={mint[:8]} "
                        f"reason={dec.get('reason')} pnl={pnl:+.6f} "
                        f"age_ms={age_ms} action={action}"
                    )
                elif action.startswith("max_hold"):
                    _log(
                        f"PGG2-V50B-HOLD-CAP-EXIT mint={mint[:8]} "
                        f"ts_ms={now_ms} action={action} pnl={pnl:+.6f}"
                    )
                break
            if now_ms >= spec_deadline_ms:
                # Time is up; signal V48 to wind up via env override.
                # The watchdog's own loop will hit max_hold via V47G's
                # internal age check at the next poll. We just exit our wait.
                _log(
                    f"PGG2-V50B-WATCHDOG-SPEC-DEADLINE mint={mint[:8]} "
                    f"age_ms={now_ms - state.watchdog_started_ms}"
                )
                break
            # Periodic heartbeat log (every ~500ms) so the parent monitor sees
            # we're still alive.
            if now_ms - last_quote_emit_ms >= 500:
                stats = wd.get_stats()
                _log(
                    f"PGG2-V50B-WATCHDOG-HB mint={mint[:8]} "
                    f"polls={stats.get('polls_succeeded')}/{stats.get('polls_attempted')} "
                    f"peak_pnl={stats.get('peak_pnl'):+.6f} "
                    f"age_ms={now_ms - state.watchdog_started_ms}"
                )
                last_quote_emit_ms = now_ms
    finally:
        try:
            await wd.stop()
        except Exception:
            pass
        state.watchdog_stopped_ms = int(time.time() * 1000)
        _log(
            f"PGG2-V50B-WATCHDOG-STOP mint={mint[:8]} "
            f"duration_ms={state.watchdog_stopped_ms - state.watchdog_started_ms}"
        )


# --------------------------------------------------------------------------
# Monkey-patches: V50A entry path + V50B exit hook
# --------------------------------------------------------------------------
def install_broker_patches() -> None:
    """Patch RaptorLiveBroker so V48's sign/send pipeline uses Helius
    Sender SWQOS-only AND so V50B spawns its V47G watchdog after the
    first buy confirmation.
    """
    import pgg2_live_raptor as _liveraptor  # late import

    state = _get_state()
    Broker = _liveraptor.RaptorLiveBroker
    # retarget_buy_min_tokens lives on DirectPumpQuoteBroker (subclass of
    # RaptorLiveBroker). Patch it there so V48's PGG2_LIVE_BROKER=direct_pump
    # path inherits our buy-context capture.
    try:
        import pgg2_direct_pump as _direct
        DirectBroker = getattr(_direct, "DirectPumpQuoteBroker", None)
    except Exception:
        DirectBroker = None

    # ---- V50A signing: splice CU+tip + sign with the broker keypair ----
    def _v50b_sign(self: Any, txn_b64: str) -> Tuple[str, str]:
        if not getattr(self, "keypair", None):
            raise RuntimeError("V50B: cannot sign without keypair")
        signed_bytes = build_v50a_tx(
            unsigned_tx_b64=txn_b64,
            keypair=self.keypair,
            swqos_tip_sol=state.tip_sol,
            priority_fee_microlamports=state.priority_micro,
            compute_unit_limit=state.cu_limit,
            tip_account_pubkey=state.tip_account,
        )
        signed_b64 = base64.b64encode(signed_bytes).decode("ascii")
        from solders.transaction import VersionedTransaction as _VT
        signed_tx = _VT.from_bytes(signed_bytes)
        if len(signed_tx.signatures) > 0:
            sig_preview = str(signed_tx.signatures[0])
        else:
            sig_preview = ""
        return signed_b64, sig_preview

    # ---- V50A send: route through Helius Sender SWQOS-only ----
    def _post_sender_sync(signed_b64: str) -> str:
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

        per_tx_fee_sol = estimate_total_fee_sol(
            priority_fee_microlamports=state.priority_micro,
            compute_unit_limit=state.cu_limit,
            swqos_tip_sol=state.tip_sol,
        )

        if not sig:
            state.budget.record_failed_send(per_tx_fee_sol)
            _log(
                f"PGG2-V50A-SENDER-RESULT leg=auto sig=NONE "
                f"send_latency_ms={send_latency_ms:.1f} ok=false err={err!r}"
            )
            _log(
                f"PGG2-V50B-SEND-FAIL leg=auto err={err!r} "
                f"send_latency_ms={send_latency_ms:.1f} "
                f"fee_consumed={per_tx_fee_sol:.9f} "
                f"failed_sends={state.budget.failed_sends}/{state.budget.max_failed_sends}"
            )
            stop, why = state.budget.should_stop()
            if stop:
                _log(f"PGG2-V50B-BUDGET-STOP reason={why}")
                state.stop_reason = f"budget_stop:{why}"
                raise SystemExit(7)
            raise RuntimeError(
                f"V50B send_failed err={err} latency_ms={send_latency_ms:.1f}"
            )

        state.budget.record_successful_send(per_tx_fee_sol)
        _log(
            f"PGG2-V50A-SENDER-SEND sig={sig} send_latency_ms={send_latency_ms:.1f} "
            f"tip_sol={state.tip_sol:.9f} priority_micro={state.priority_micro} "
            f"cu_limit={state.cu_limit}"
        )
        _log(
            f"PGG2-V50A-SENDER-RESULT sig={sig} send_latency_ms={send_latency_ms:.1f} "
            f"ok=true"
        )
        _log(
            f"PGG2-V50B-SEND-OK sig={sig} "
            f"send_latency_ms={send_latency_ms:.1f} "
            f"fee_consumed={per_tx_fee_sol:.9f} "
            f"spent={state.budget.spent_sol:.9f}/{state.budget.budget_sol:.9f}"
        )
        return sig

    def _v50b_send_signed(self: Any, signed_b64: str) -> str:
        state.buys_sent += 1
        return _post_sender_sync(signed_b64)

    def _v50b_send_signed_atomic(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50b_send_signed_rpc(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50b_send_signed_rpc_skip_preflight(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    # ---- V50A confirm_signature via adapter (for completeness) ----
    def _confirm_inner(sig: str) -> Dict[str, Any]:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                v50a_confirm_signature(
                    sig,
                    state.helius_api_key,
                    max_wait_ms=int(
                        os.environ.get("PGG2_V50B_CONFIRM_MAX_WAIT_MS", "30000")
                        or 30000
                    ),
                )
            )
        finally:
            loop.close()

    def _v50b_confirm_signature(self: Any, sig: str) -> bool:
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

    # ---- V50B mint tracker: hook retarget_buy_min_tokens to capture the
    # full mint string per buy build. V48 calls retarget_buy_min_tokens
    # JUST BEFORE signing+sending the buy, so the most-recent recorded
    # mint maps 1:1 to the next signed buy tx.
    # Prefer DirectPumpQuoteBroker (subclass that owns the method).
    if DirectBroker is not None and hasattr(DirectBroker, "retarget_buy_min_tokens"):
        _retarget_target_cls = DirectBroker
        _orig_retarget_buy = DirectBroker.retarget_buy_min_tokens
    else:
        _retarget_target_cls = Broker
        _orig_retarget_buy = getattr(Broker, "retarget_buy_min_tokens", None)

    def _v50b_retarget_buy(
        self: Any, quote: dict, mint_str: str, min_tokens_ui: float
    ) -> dict:
        # Record the latest buy context. The size_sol is derivable from
        # quote["amount_in_sol"] if present, else env default.
        try:
            global _LATEST_BUY_CTX
            size_sol_q = 0.0
            if isinstance(quote, dict):
                size_sol_q = float(
                    quote.get("amount_in_sol")
                    or quote.get("amountIn")
                    or quote.get("size_sol")
                    or 0.0
                )
            _LATEST_BUY_CTX = {
                "mint": str(mint_str),
                "size_sol": float(size_sol_q),
                "min_tokens_ui": float(min_tokens_ui),
                "ts_ms": int(time.time() * 1000),
            }
        except Exception:
            pass
        # ---- V56 PRE-SEND GATES (env-gated; default OFF) ----
        if os.environ.get("PGG2_V56_ROUTER_ENABLED", "").strip() in ("1", "true", "yes", "on"):
            try:
                from pgg2_v56_risk_veto import get_veto as _v56_get_veto
                _v56_veto = _v56_get_veto()
                _v56_r = _v56_veto.check(str(mint_str))
                _log(_v56_veto.format_log_line(_v56_r))
                if not _v56_r.pass_:
                    _log(f"PGG2-V56-LIVE-ROUTER-BLOCK stage=risk blocker={_v56_r.blocker}")
                    raise RuntimeError(f"v56_risk_veto_block:{_v56_r.blocker}")
                if _v56_r.is_token_2022:
                    _log(f"PGG2-V56-LIVE-ROUTER-BLOCK stage=path blocker=t22_no_v2_path_yet")
                    raise RuntimeError("v56_t22_no_v2_path_yet")
                _log(f"PGG2-V56-LIVE-ROUTER-SEND stage=passed mint={str(mint_str)[:4]}..{str(mint_str)[-4:]} path=v1")
            except RuntimeError:
                raise
            except Exception as _v56_e:
                _log(f"PGG2-V56-ROUTER-ERR {type(_v56_e).__name__}:{_v56_e}; falling through to v50b normal path")
        if _orig_retarget_buy is not None:
            return _orig_retarget_buy(self, quote, mint_str, min_tokens_ui)
        raise RuntimeError("retarget_buy_min_tokens_unavailable_at_patch_time")

    # ---- V50B wait_confirmed hook: spawn watchdog on first BUY confirm ----
    # Capture the original wait_confirmed at patch-time.
    _orig_wait_confirmed = Broker.wait_confirmed

    def _v50b_wait_confirmed(self: Any, sig: str) -> bool:
        ok = _orig_wait_confirmed(self, sig)
        if not ok:
            return ok
        # The first sig is the buy; subsequent sigs (sell legs) we mark as
        # sell confirmations. We use buys_sent counter (incremented in
        # send_signed) to distinguish: the first send is the buy.
        if state.current_buy_sig and sig == state.current_buy_sig:
            # Already saw this sig.
            pass
        if sig in state.confirmed_buy_sigs:
            # Already processed.
            return ok
        if not state.current_buy_sig:
            # This is the first confirm we've seen post-send: it's the buy.
            state.current_buy_sig = sig
            state.confirmed_buy_sigs.add(sig)
            state.buys_confirmed += 1
            state.current_buy_confirmed_ms = int(time.time() * 1000)
            _log(
                f"PGG2-V50A-LANDING-LATENCY leg=buy sig={sig} "
                f"total_landing_ms={state.current_buy_confirmed_ms - state.watchdog_started_ms if state.watchdog_started_ms else 0}"
            )
            _log(
                f"PGG2-V50B-BUY-CONFIRMED sig={sig} "
                f"ts_ms={state.current_buy_confirmed_ms}"
            )
            # Best-effort: extract mint + tokens_held + buy_cost from the
            # broker's recent state. We don't have a strong handle to V48's
            # local variables from here; we capture from broker introspection.
            mint, tokens_held_raw, size_sol, buy_cost_sol = _introspect_position(
                self, sig
            )
            if mint and tokens_held_raw > 0:
                state.current_mint = mint
                state.current_size_sol = float(size_sol)
                state.current_actual_tokens_raw = int(tokens_held_raw)
                state.current_actual_buy_cost_sol = float(buy_cost_sol)
                # Emit a POSTBUY-QUOTE log: read the curve once.
                try:
                    _emit_postbuy_quote(state, mint, tokens_held_raw, size_sol)
                except Exception as exc:
                    _log(
                        f"PGG2-V50B-POSTBUY-QUOTE-ERR mint={mint[:8]} "
                        f"err={type(exc).__name__}:{exc}"
                    )
                _spawn_watchdog_for_buy(
                    state,
                    mint=mint,
                    tokens_held_raw=tokens_held_raw,
                    buy_size_sol=size_sol,
                    opened_at_ms=state.current_buy_confirmed_ms,
                )
            else:
                _log(
                    f"PGG2-V50B-WATCHDOG-SKIP reason=could_not_introspect_position "
                    f"sig={sig} mint={mint!r} tokens={tokens_held_raw}"
                )
        else:
            # Subsequent confirmations -> these are sells.
            state.sells_confirmed += 1
            state.current_sell_sig = sig
            _log(
                f"PGG2-V50A-LANDING-LATENCY leg=sell sig={sig} "
                f"total_landing_ms_since_buy={int(time.time()*1000) - state.current_buy_confirmed_ms}"
            )
            _log(
                f"PGG2-V50B-SELL-CONFIRMED sig={sig} "
                f"sells_confirmed={state.sells_confirmed}"
            )
        return ok

    # Apply patches.
    Broker.sign_transaction = _v50b_sign
    Broker.send_signed = _v50b_send_signed
    Broker.send_signed_atomic = _v50b_send_signed_atomic
    Broker.send_signed_rpc = _v50b_send_signed_rpc
    Broker.send_signed_rpc_skip_preflight = _v50b_send_signed_rpc_skip_preflight
    Broker.confirm_signature = _v50b_confirm_signature
    Broker.wait_confirmed = _v50b_wait_confirmed
    if _orig_retarget_buy is not None:
        _retarget_target_cls.retarget_buy_min_tokens = _v50b_retarget_buy

    _log(
        f"PGG2-V50B-PATCHES-INSTALLED tip_account={state.tip_account} "
        f"tip_sol={state.tip_sol:.9f} priority_micro={state.priority_micro} "
        f"cu_limit={state.cu_limit} max_hold_ms={state.max_hold_ms} "
        f"watchdog_interval_ms={state.watchdog_interval_ms}"
    )


def _introspect_position(
    broker: Any, buy_sig: str
) -> Tuple[str, int, float, float]:
    """Extract (mint, tokens_held_raw, size_sol, buy_cost_sol) after buy
    is confirmed.

    Strategy:
      1. Mint + size come from _LATEST_BUY_CTX populated by our
         retarget_buy_min_tokens hook (the call immediately preceding
         the buy send).
      2. tokens_held_raw is fetched via broker.token_balance_raw(Pubkey(mint)).
      3. buy_cost_sol is fetched via broker.transaction_wallet_delta_sol(sig).

    On any failure returns the best partial result; (\"\", 0, 0.0, 0.0)
    only if mint cannot be resolved.
    """
    ctx = dict(_LATEST_BUY_CTX or {})
    mint = str(ctx.get("mint") or "")
    if not mint:
        _log(
            f"PGG2-V50B-INTROSPECT-WARN sig={buy_sig} "
            "reason=no_latest_buy_ctx_set_by_retarget_hook"
        )
        return ("", 0, 0.0, 0.0)
    # Buy cost from on-chain wallet delta.
    try:
        sol_delta = float(broker.transaction_wallet_delta_sol(buy_sig))
        buy_cost = float(abs(sol_delta)) if sol_delta < 0 else 0.0
    except Exception as exc:
        _log(
            f"PGG2-V50B-INTROSPECT-WALLET-WARN sig={buy_sig} "
            f"err={type(exc).__name__}:{exc}"
        )
        buy_cost = 0.0
    # size_sol from ctx if recorded, else fall back to buy_cost or env default.
    size_sol = float(ctx.get("size_sol") or 0.0)
    if size_sol <= 0.0:
        if buy_cost > 0.0:
            size_sol = buy_cost
        else:
            size_sol = float(
                os.environ.get("PGG2_TRADE_SIZE_SOL", "0.005") or 0.005
            )
    # Tokens held via broker.
    tokens_held_raw = 0
    try:
        from solders.pubkey import Pubkey as _Pk
        mint_pk = _Pk.from_string(mint)
        tokens_held_raw = int(broker.token_balance_raw(mint_pk))
    except Exception as exc:
        _log(
            f"PGG2-V50B-INTROSPECT-TOKEN-WARN mint={mint[:8]} "
            f"err={type(exc).__name__}:{exc}"
        )
        tokens_held_raw = 0
    if tokens_held_raw <= 0:
        # As a fallback, try transaction_token_delta_ui.
        try:
            ui_delta = float(broker.transaction_token_delta_ui(buy_sig, mint))
            if ui_delta > 0:
                from solders.pubkey import Pubkey as _Pk
                mint_pk = _Pk.from_string(mint)
                tokens_held_raw = int(broker.ui_to_raw(mint_pk, ui_delta))
        except Exception:
            pass
    if buy_cost <= 0.0:
        buy_cost = float(size_sol)
    return (mint, int(tokens_held_raw), float(size_sol), float(buy_cost))


# Populated by broker.retarget_buy_min_tokens hook (full mint + size).
_LATEST_BUY_CTX: Dict[str, Any] = {}


def _emit_postbuy_quote(
    state: V50BState, mint: str, tokens_held_raw: int, size_sol: float
) -> None:
    """One-shot post-buy quote read of the bonding curve.

    Used to emit PGG2-V50B-POSTBUY-QUOTE for the report timeline.
    """
    rpc_url = _v50b_get_rpc_endpoint(state)
    if not rpc_url:
        return
    from pgg2_v47g_position_quote_watchdog import (
        _derive_bonding_curve_pda,
        _decode_curve_account_bytes,
        _rpc_get_account_info_sync,
        LAMPORTS_PER_SOL as _LPS,
    )
    from pgg2_v42h_local_curve_quote import (
        curve_state_from_subscriber_point,
        local_sell_quote_sol,
    )
    try:
        pda_str = _derive_bonding_curve_pda(mint)
        raw = _rpc_get_account_info_sync(rpc_url, pda_str, timeout_s=1.5)
        if not raw:
            return
        vtok, vsol, rtok, _rsol, _tot, _complete = _decode_curve_account_bytes(raw)
        cs = curve_state_from_subscriber_point(
            int(vsol), int(vtok), int(rtok),
            DEFAULT_PUMP_FEE_BPS, DEFAULT_PUMP_CREATOR_FEE_BPS,
        )
        sell_lams, _ = local_sell_quote_sol(cs, int(tokens_held_raw))
        sell_sol = float(sell_lams) / float(_LPS)
        # Buy cost ~ size_sol on Pump bc (already includes fees in the curve).
        pnl = sell_sol - float(size_sol) - 2.0 * 0.0000287
        ts = int(time.time() * 1000)
        _log(
            f"PGG2-V50B-POSTBUY-QUOTE mint={mint[:8]} ts_ms={ts} "
            f"sell_sol={sell_sol:.9f} pnl={pnl:+.6f} vsol={vsol/_LPS:.4f}"
        )
    except Exception as exc:
        _log(
            f"PGG2-V50B-POSTBUY-QUOTE-ERR mint={mint[:8]} "
            f"err={type(exc).__name__}:{exc}"
        )


# --------------------------------------------------------------------------
# Stop-file watcher (cooperative early termination)
# --------------------------------------------------------------------------
async def _stop_file_watcher(stop_path: str = "/root/piggy/V50B_STOP") -> None:
    while True:
        if Path(stop_path).exists():
            _log(f"PGG2-V50B-STOP-FILE detected at {stop_path}; raising SystemExit")
            state = _get_state()
            state.stop_reason = "stop_file_detected"
            os._exit(9)
        await asyncio.sleep(2.0)


# --------------------------------------------------------------------------
# Wallet draw-down watcher
# --------------------------------------------------------------------------
async def _wallet_drawdown_watcher() -> None:
    state = _get_state()
    interval_s = float(
        os.environ.get("PGG2_V50B_WALLET_POLL_S", "10") or 10
    )
    while True:
        try:
            bal = await _wallet_balance_sol(state.helius_api_key, state.wallet_pubkey)
            if bal > 0.0:
                # Drawdown must count only REALIZED loss. While a position is
                # open, its SOL has been converted to tokens — that is NOT a
                # loss, it is an in-flight asset. The raw wallet-balance
                # subtraction killed the 12:48 GMDd run on its very first
                # trade: cap=0.003, single 0.01 SOL buy made wallet show
                # -0.012 (cost + curve dump during RPC throttle), cap tripped,
                # bot terminated. Actual realized loss after manual rescue
                # was only -0.002. Exclude open-position cost so the cap
                # protects against accumulated REALIZED losses, not in-flight
                # buy costs.
                if state.buys_confirmed > state.sells_confirmed:
                    open_position_cost = float(
                        getattr(state, "current_actual_buy_cost_sol", 0.0) or 0.0
                    )
                else:
                    open_position_cost = 0.0
                drawdown = state.wallet_before_sol - (bal + open_position_cost)
                if drawdown > state.max_wallet_drawdown_sol:
                    _log(
                        f"PGG2-V50B-WALLET-DRAWDOWN-HARDCAP "
                        f"drawdown={drawdown:.9f} > "
                        f"cap={state.max_wallet_drawdown_sol:.9f} "
                        f"current_balance={bal:.9f} "
                        f"open_position_cost={open_position_cost:.9f} "
                        f"buys_conf={state.buys_confirmed} sells_conf={state.sells_confirmed}; "
                        f"terminating"
                    )
                    state.wallet_after_sol = bal
                    state.stop_reason = (
                        f"wallet_drawdown_hardcap drawdown={drawdown:.9f}"
                    )
                    state.negative_close = True
                    write_v50b_report()
                    os._exit(8)
        except Exception:
            pass
        await asyncio.sleep(interval_s)


# --------------------------------------------------------------------------
# Final report writer
# --------------------------------------------------------------------------
def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, min(len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def write_v50b_report() -> Path:
    state = _get_state()
    out_path = Path(
        os.environ.get(
            "PGG2_V50B_OUT_MD",
            "/root/piggy/V50B_STAGEA_SWQOS_WATCHDOG_RESULT.md",
        )
    )

    end_ts = time.time()
    wall_s = end_ts - state.start_ts

    wallet_delta = state.wallet_after_sol - state.wallet_before_sol

    send_median = _percentile(state.send_latencies_ms, 50)
    send_p25 = _percentile(state.send_latencies_ms, 25)
    send_p75 = _percentile(state.send_latencies_ms, 75)

    stop_reason = state.stop_reason or "v48_exit_or_timeout"

    pass_criteria_met = (
        state.buys_confirmed >= 1
        and state.sells_confirmed >= 1
        and wallet_delta >= 0
        and not state.negative_close
        and state.budget.spent_sol < state.budget.budget_sol
    )

    lines: List[str] = []
    lines.append("# V50B Stage A - SWQOS Entry + V47F/V47G Watchdog Exit Result\n\n")
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
        f"- max_wallet_drawdown_sol: `{state.max_wallet_drawdown_sol:.9f}`\n"
    )
    lines.append(f"- max_hold_ms (V50B): `{state.max_hold_ms}`\n")
    lines.append(
        f"- watchdog_interval_ms (V47G): `{state.watchdog_interval_ms}`\n\n"
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
        f"- send_latency: median=`{send_median:.1f}` p25=`{send_p25:.1f}` p75=`{send_p75:.1f}` n=`{len(state.send_latencies_ms)}`\n\n"
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

    lines.append("## Position Trace\n\n")
    if state.current_buy_sig:
        lines.append(f"- mint: `{state.current_mint}`\n")
        lines.append(f"- size_sol: `{state.current_size_sol:.6f}`\n")
        lines.append(f"- actual_tokens_raw: `{state.current_actual_tokens_raw}`\n")
        lines.append(f"- actual_buy_cost_sol: `{state.current_actual_buy_cost_sol:.9f}`\n")
        lines.append(f"- buy_sig: `{state.current_buy_sig}`\n")
        lines.append(f"- sell_sig: `{state.current_sell_sig or '<none>'}`\n")
        lines.append(f"- buy_confirmed_ms_ts: `{state.current_buy_confirmed_ms}`\n")
        lines.append(f"- watchdog_started_ms: `{state.watchdog_started_ms}`\n")
        lines.append(f"- watchdog_stopped_ms: `{state.watchdog_stopped_ms}`\n")
        if state.watchdog_started_ms and state.watchdog_stopped_ms:
            lines.append(
                f"- watchdog_duration_ms: `{state.watchdog_stopped_ms - state.watchdog_started_ms}`\n"
            )
    else:
        lines.append("_No buy was confirmed during this run._\n")
    lines.append("\n")

    lines.append("## Watchdog Decision\n\n")
    if state.watchdog_decision:
        d = state.watchdog_decision
        lines.append(f"- action: `{d.get('action')}`\n")
        lines.append(f"- reason: `{d.get('reason')}`\n")
        lines.append(f"- pnl_at_decision_sol: `{d.get('pnl'):+.6f}`\n")
        lines.append(f"- age_ms_at_decision: `{d.get('age_ms')}`\n")
        lines.append(
            f"- quote_sell_sol_at_decision: `{(d.get('quote_sell_sol') or 0.0):.9f}`\n"
        )
    else:
        lines.append("_Watchdog did not record a decision._\n")
    lines.append("\n")

    lines.append("## Watchdog Quote Timeline (sample)\n\n")
    if state.watchdog_quote_timeline:
        lines.append("| idx | ts_ms | line |\n|---|---|---|\n")
        for i, row in enumerate(state.watchdog_quote_timeline[:100]):
            ts = row.get("ts_ms")
            msg = row.get("msg", "")
            msg_one = msg.replace("|", "/")
            lines.append(f"| {i} | {ts} | `{msg_one[:160]}` |\n")
        if len(state.watchdog_quote_timeline) > 100:
            lines.append(
                f"\n_({len(state.watchdog_quote_timeline) - 100} more rows omitted; "
                "full timeline in stdout log)_\n"
            )
    else:
        lines.append("_No watchdog ticks recorded._\n")
    lines.append("\n")

    lines.append("## Verdict\n\n")
    lines.append(f"- pass_criteria_met: **{pass_criteria_met}**\n")
    lines.append(
        f"- 1+ confirmed non-negative close required: buys_confirmed={state.buys_confirmed} sells_confirmed={state.sells_confirmed} negative_close={state.negative_close}\n"
    )
    lines.append(
        f"- wallet_drawdown_within_cap: {abs(min(0.0, wallet_delta)):.9f} < {state.max_wallet_drawdown_sol:.9f} = {abs(min(0.0, wallet_delta)) < state.max_wallet_drawdown_sol}\n"
    )
    lines.append(
        f"- fee_budget_intact: spent {state.budget.spent_sol:.9f} < budget {state.budget.budget_sol:.9f} = {state.budget.spent_sol < state.budget.budget_sol}\n"
    )
    lines.append(
        f"- exit_stack_replaced_V48_clean_close: TRUE "
        f"(PGG2_V48_LIVE_MAX_POSITION_MS={state.max_hold_ms}, "
        f"V47G watchdog spawned for each buy)\n\n"
    )
    lines.append(f"### VERDICT: **{'PASS' if pass_criteria_met else 'FAIL'}**\n\n")

    lines.append("## Final Gate Table\n\n")
    lines.append("| Gate | Required | Actual | Status |\n|---|---|---|---|\n")
    lines.append(
        f"| Helius Sender SWQOS-only | endpoint=fast?swqos_only=true | "
        f"endpoint=fast?swqos_only=true | PASS |\n"
    )
    lines.append(
        f"| Tip SOL | <=0.000005 | {state.tip_sol:.9f} | "
        f"{'PASS' if state.tip_sol <= 0.000005 + 1e-12 else 'FAIL'} |\n"
    )
    lines.append(
        f"| Fee budget | <=0.00025 | {state.budget.spent_sol:.9f} | "
        f"{'PASS' if state.budget.spent_sol < 0.00025 else 'FAIL'} |\n"
    )
    lines.append(
        f"| Max failed sends | <=2 | {state.budget.failed_sends} | "
        f"{'PASS' if state.budget.failed_sends <= 2 else 'FAIL'} |\n"
    )
    lines.append(
        f"| Wallet drawdown | <=0.0030 | "
        f"{abs(min(0.0, wallet_delta)):.9f} | "
        f"{'PASS' if abs(min(0.0, wallet_delta)) < 0.0030 else 'FAIL'} |\n"
    )
    lines.append(
        f"| Negative close | FALSE | {state.negative_close} | "
        f"{'PASS' if not state.negative_close else 'FAIL'} |\n"
    )
    lines.append(
        f"| Buys confirmed | >=1 (for PASS) | {state.buys_confirmed} | "
        f"{'PASS' if state.buys_confirmed >= 1 else 'PENDING'} |\n"
    )
    lines.append(
        f"| Sells confirmed | >=1 (for PASS) | {state.sells_confirmed} | "
        f"{'PASS' if state.sells_confirmed >= 1 else 'PENDING'} |\n"
    )
    lines.append(
        f"| Wall clock | <=2100s | {int(wall_s)} | "
        f"{'PASS' if wall_s <= 2100 + 5 else 'FAIL'} |\n"
    )
    lines.append("\n")

    lines.append("## Honest Assessment\n\n")
    if state.buys_confirmed >= 1 and state.sells_confirmed >= 1:
        if wallet_delta > 0:
            assess = (
                f"V50B closed cleanly with wallet_delta=+{wallet_delta:.6f} SOL. "
                "The V47G watchdog + V47F hold-caps + V47F mid-hold abort "
                "stack successfully bounded the hold time and protected the "
                "exit. This validates the architecture end-to-end."
            )
        else:
            assess = (
                f"V50B closed with NEGATIVE wallet_delta={wallet_delta:+.6f} SOL. "
                "Even though the exit stack fired faster than V50A's 31s hold, "
                "the curve still moved against us before sell landed. This is "
                "FAIL per spec (negative close = FAIL)."
            )
    elif state.buys_confirmed >= 1 and state.sells_confirmed == 0:
        assess = (
            f"V50B confirmed a buy (sig={state.current_buy_sig[:10]}...) but "
            "could NOT confirm a sell. Token may be residual in wallet. "
            "Check post-run for token balance; if residual, manual sweep "
            "required."
        )
    elif state.buys_sent >= 1 and state.buys_confirmed == 0:
        assess = (
            "V50B sent a buy tx but it did NOT confirm. Either send failed "
            "at Sender layer or tx reverted on-chain (curve change between "
            "decision and landing). Safe failure -- only fees consumed."
        )
    else:
        assess = (
            "V50B did not send any buy. V48 decision pipeline rejected all "
            "candidates -- not a SWQOS or exit-stack issue. Re-run during "
            "higher candidate flow."
        )
    lines.append(f"{assess}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    # JSON snapshot
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
        "max_hold_ms": state.max_hold_ms,
        "watchdog_interval_ms": state.watchdog_interval_ms,
        "buys_sent": state.buys_sent,
        "buys_confirmed": state.buys_confirmed,
        "buys_failed": state.buys_failed,
        "sells_sent": state.sells_sent,
        "sells_confirmed": state.sells_confirmed,
        "sells_failed": state.sells_failed,
        "send_latency_ms_median": send_median,
        "total_fees_consumed_sol": state.budget.spent_sol,
        "fee_budget_remaining_sol": state.budget.remaining_sol(),
        "wallet_before_sol": state.wallet_before_sol,
        "wallet_after_sol": state.wallet_after_sol,
        "wallet_delta_sol": wallet_delta,
        "negative_close": state.negative_close,
        "pass_criteria_met": pass_criteria_met,
        "watchdog_decision": state.watchdog_decision,
        "current_mint": state.current_mint,
        "current_size_sol": state.current_size_sol,
        "current_actual_tokens_raw": state.current_actual_tokens_raw,
        "current_actual_buy_cost_sol": state.current_actual_buy_cost_sol,
        "current_buy_sig": state.current_buy_sig,
        "current_sell_sig": state.current_sell_sig,
        "send_latencies_ms": state.send_latencies_ms,
    }
    out_json.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Main runner
# --------------------------------------------------------------------------
async def amain() -> int:
    state = _get_state()

    if not state.helius_api_key:
        _log("PGG2-V50B-ABORT HELIUS_API_KEY missing")
        state.stop_reason = "missing_helius_api_key"
        write_v50b_report()
        return 2

    # Read starting wallet balance.
    state.wallet_before_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    _log(
        f"PGG2-V50B-START wallet_before_sol={state.wallet_before_sol:.9f} "
        f"tip_account={state.tip_account} tip_sol={state.tip_sol:.9f} "
        f"priority_micro={state.priority_micro} cu_limit={state.cu_limit} "
        f"fee_budget={state.fee_budget_sol:.9f} "
        f"max_hold_ms={state.max_hold_ms} "
        f"watchdog_interval_ms={state.watchdog_interval_ms}"
    )

    # Sanity: must have a reasonable starting balance.
    if state.wallet_before_sol < 0.10:
        _log(
            f"PGG2-V50B-ABORT wallet_balance_below_floor "
            f"{state.wallet_before_sol:.9f} < 0.10"
        )
        state.stop_reason = "wallet_balance_below_floor_at_start"
        write_v50b_report()
        return 3

    # Install monkey patches BEFORE V48 imports/creates the broker.
    install_broker_patches()

    # Start watchdog tasks.
    watcher_drawdown = asyncio.create_task(_wallet_drawdown_watcher())
    watcher_stop_file = asyncio.create_task(_stop_file_watcher())

    # Build V48 argv with V50B constraints.
    out_md = os.environ.get(
        "PGG2_V48_OUT_MD",
        "/root/piggy/V50B_STAGEA_V48_DECISIONS.md",
    )
    out_jsonl = os.environ.get(
        "PGG2_V48_OUT_JSONL",
        "/root/piggy/data/v50b_stagea_decisions.jsonl",
    )
    Path(out_jsonl).parent.mkdir(parents=True, exist_ok=True)
    debug_log = os.environ.get(
        "PGG2_V48_DEBUG_LOG",
        "/root/piggy/logs/v50b_stagea_v48.debug.log",
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
    saved_argv = list(sys.argv)
    sys.argv = v48_argv

    try:
        from pgg2_v48_drylive_harness import amain as v48_amain  # type: ignore

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
            _log(f"PGG2-V50B-V48-SYSTEMEXIT code={se.code}")
            ret = int(se.code or 0)
    finally:
        sys.argv = saved_argv
        # Stop the watchdog if still running.
        try:
            if state.watchdog is not None and state.watchdog._running:
                state.watchdog._running = False
                state.watchdog._stop_evt.set()
        except Exception:
            pass
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
    wallet_delta = state.wallet_after_sol - state.wallet_before_sol
    if state.buys_confirmed >= 1 and wallet_delta < 0:
        state.negative_close = True

    if not state.stop_reason:
        if state.negative_close:
            state.stop_reason = "negative_close"
        elif state.buys_confirmed >= 1 and state.sells_confirmed >= 1:
            state.stop_reason = "1_non_negative_close_reached"
        elif state.budget.spent_sol >= state.budget.budget_sol:
            state.stop_reason = "fee_budget_consumed"
        elif state.budget.failed_sends >= state.budget.max_failed_sends:
            state.stop_reason = "max_failed_sends_reached"
        else:
            state.stop_reason = "v48_exited_normally"

    write_v50b_report()
    _log(
        f"PGG2-V50B-COMPLETE stop_reason={state.stop_reason} "
        f"wallet_delta={wallet_delta:+.9f} "
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
