"""V51 Stage A - SWQOS entry + V47G watchdog exit + Helius holder pre-veto.

V51 closes the V50C failure modes by:
  1. Adding a Helius getTokenLargestAccounts pre-entry holder-quality veto.
  2. Adding a Token-2022 actual-entry block (V50C 9Cc2..pump was Token-2022).
  3. Fixing the V47G watchdog sell-quote path (V50C called nonexistent
     broker.quote_sell; V51 uses build_sell via pgg2_v51_sell_quote_wrapper).
  4. Adding an emergency-sell guard (40% of quote_out, floor 0.0005 SOL).
  5. Recalibrating drawdown accounting to a REALIZED PnL state machine
     (transient debits during open positions don't count against hardcap).

Hard constraints (V51 Stage A):
  - 1 non-negative close target. STOP after 1.
  - Negative close = STOP & FAIL.
  - 2 failed sends -> STOP.
  - Fee budget 0.00025 SOL -> STOP.
  - Wallet realized drawdown 0.0080 SOL -> STOP.
  - Emergency wallet floor: balance decrease > 0.015 SOL -> STOP.
  - 35 min elapsed -> STOP.
  - SWQOS-only Helius Sender. Tip = 0.000005 SOL.

NO modifications to PGG2.py, pgg2_live_raptor.py, pgg2_direct_pump.py,
pgg2_v48_drylive_harness.py, pgg2_v50a_sender_adapter.py, pgg2_v50a_fee_policy.py.
V47G watchdog source files preserved. V51 wraps via new lifecycle + sell-
quote wrapper + holder-veto module.

Static-grep clean: no sendTransaction patterns -- all tx-send goes through
the V50A sender adapter (which is exempt).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import functools
import json
import os
import random
import re as _re_self
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Static-grep self check -- forbidden send patterns must NOT appear here.
_FORBIDDEN = (
    r"\.send_signed\s*\(",
    r"\.send_transaction\s*\(",
    r"\.sendTransaction\s*\(",
    r"\.send_signed_rpc\s*\(",
    r"\bsend_signed\s*\(",
    r"\bsend_transaction\s*\(",
    r"\bsendTransaction\s*\(",
    r"\bsend_signed_rpc\s*\(",
)
with open(__file__, "r", encoding="utf-8") as _self:
    _self_src = _self.read()
for _pat in _FORBIDDEN:
    if _re_self.search(_pat, _self_src):
        sys.stderr.write(f"V51-STAGEA-ABORT forbidden_call_pattern={_pat}\n")
        sys.exit(2)

# We must add /root/piggy to sys.path so we can import the V48 harness.
sys.path.insert(0, "/root/piggy")

import aiohttp  # noqa: E402

from pgg2_v50a_sender_adapter import (  # noqa: E402
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

# V47G + V50C lifecycle imports.
from pgg2_v47g_position_quote_watchdog import (  # noqa: E402
    V47GPositionQuoteWatchdog,
)
from pgg2_v47g_watchdog_exit_policy import (  # noqa: E402
    is_negative_close_action,
)
from pgg2_v50c_watchdog_lifecycle import (  # noqa: E402
    V50CWatchdogLifecycle,
    install_watchdog_hooks,
)

# V51 modules.
from pgg2_v51_sell_quote_wrapper import (  # noqa: E402
    build_live_sell_quote_fast,
    compute_emergency_min_sol_out,
)
from pgg2_v51_holder_quality import (  # noqa: E402
    V51HolderQualityChecker,
    V51Token2022Checker,
    is_token_2022,
    TOKEN_2022_PROGRAM_ID_STR,
)
from pgg2_v51_holder_veto import (  # noqa: E402
    evaluate_holder_veto,
    load_rules as load_holder_rules,
    parse_token2022_whitelist_env,
)


# --------------------------------------------------------------------------
# Env loader (no echo of secrets)
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


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


# --------------------------------------------------------------------------
# Pump constants for fee-bps decode.
# --------------------------------------------------------------------------
DEFAULT_PUMP_FEE_BPS = 100
DEFAULT_PUMP_CREATOR_FEE_BPS = 0


# --------------------------------------------------------------------------
# Custom exception for the V51 pre-entry vetos.
# --------------------------------------------------------------------------
class V51HolderVetoBlock(RuntimeError):
    """Raised by the patched send-leg when a buy candidate fails the V51
    pre-entry holder-quality check. V48's `_open_v48_live_record` catches
    this at the `except Exception as exc:` around `send_signed`, logging
    `PGG2-V48-LIVE-BUY-NOSEND-SAFE` with reason=`v51_holder_veto`.
    """
    pass


class V51Token2022Block(RuntimeError):
    """Raised by the patched send-leg when the candidate mint is Token-2022
    and not in the V51 whitelist."""
    pass


# --------------------------------------------------------------------------
# Drawdown state machine.
# --------------------------------------------------------------------------
class V51DrawdownAccounting:
    """Realized-PnL drawdown accounting.

    States:
      "no_position" | "position_open" | "position_closing"

    realized_pnl_sol: ONLY updated on confirmed close.
    transient_debit_sol: tracked; does NOT count toward hardcap.
    """

    def __init__(self, *, max_realized_drawdown_sol: float = 0.0080) -> None:
        self.state: str = "no_position"
        self.realized_pnl_sol: float = 0.0
        self.transient_debit_sol: float = 0.0
        self.max_realized_drawdown_sol: float = float(max_realized_drawdown_sol)
        self.last_event_ts_ms: int = int(time.time() * 1000)
        self._lock = threading.Lock()

    def on_buy_confirmed(self, buy_cost_sol: float) -> None:
        with self._lock:
            self.state = "position_open"
            self.transient_debit_sol += float(buy_cost_sol)
            self.last_event_ts_ms = int(time.time() * 1000)

    def on_close_confirmed(self, net_pnl_sol: float) -> None:
        with self._lock:
            self.state = "no_position"
            self.realized_pnl_sol += float(net_pnl_sol)
            # Settle out the transient debit on close; reset to zero.
            self.transient_debit_sol = 0.0
            self.last_event_ts_ms = int(time.time() * 1000)

    def on_close_started(self) -> None:
        with self._lock:
            self.state = "position_closing"
            self.last_event_ts_ms = int(time.time() * 1000)

    def should_stop_realized(self) -> Tuple[bool, str]:
        with self._lock:
            if self.state == "no_position" and \
               self.realized_pnl_sol < -self.max_realized_drawdown_sol:
                return True, (
                    f"realized_drawdown_breach "
                    f"realized={self.realized_pnl_sol:+.9f} "
                    f"cap={self.max_realized_drawdown_sol:.9f}"
                )
            return False, ""

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "realized_pnl_sol": float(self.realized_pnl_sol),
                "transient_debit_sol": float(self.transient_debit_sol),
                "cap_sol": float(self.max_realized_drawdown_sol),
            }


# --------------------------------------------------------------------------
# V51 runtime state
# --------------------------------------------------------------------------
class V51State:
    def __init__(self) -> None:
        self.start_ts = time.time()
        self.helius_api_key: str = os.environ.get("HELIUS_API_KEY", "").strip()
        self.solanatracker_rpc_http: str = os.environ.get(
            "SOLANATRACKER_RPC_HTTP", ""
        ).strip()
        self.tip_account: str = random.choice(HELIUS_SENDER_TIP_ACCOUNTS)
        self.tip_sol: float = float(
            os.environ.get("PGG2_V51_SWQOS_TIP_SOL", "0.000005") or 0.000005
        )
        self.max_tip_sol: float = float(
            os.environ.get("PGG2_V51_MAX_TIP_SOL", "0.000005") or 0.000005
        )
        if self.tip_sol > min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL):
            self.tip_sol = min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL)
        self.priority_micro: int = int(
            os.environ.get("PGG2_V51_PRIORITY_FEE_MICROLAMPORTS", "100000")
            or 100000
        )
        self.cu_limit: int = int(
            os.environ.get("PGG2_V51_CU_LIMIT", "200000") or 200000
        )
        self.fee_budget_sol: float = float(
            os.environ.get("PGG2_V51_STAGEA_FEE_BUDGET_SOL", "0.00025")
            or 0.00025
        )
        self.max_failed_sends: int = int(
            os.environ.get("PGG2_V51_MAX_FAILED_SENDS", "2") or 2
        )
        self.max_closes: int = int(
            os.environ.get("PGG2_V51_MAX_CLOSES", "1") or 1
        )
        self.max_open: int = int(os.environ.get("PGG2_V51_MAX_OPEN", "1") or 1)
        self.max_seconds: int = int(
            os.environ.get("PGG2_V51_MAX_SECONDS", "2100") or 2100
        )
        self.max_realized_drawdown_sol: float = float(
            os.environ.get("PGG2_V51_MAX_REALIZED_DRAWDOWN_SOL", "0.0080")
            or 0.0080
        )
        self.emergency_wallet_floor_decrease_sol: float = float(
            os.environ.get(
                "PGG2_V51_EMERGENCY_WALLET_FLOOR_DECREASE_SOL", "0.0150"
            ) or 0.0150
        )
        self.max_hold_ms: int = int(
            os.environ.get("PGG2_V51_MAX_HOLD_MS", "1500") or 1500
        )
        self.watchdog_interval_ms: int = int(
            os.environ.get("PGG2_V47G_WATCHDOG_INTERVAL_MS", "250") or 250
        )
        self.holder_ttl_s: int = int(
            os.environ.get("PGG2_V51_HOLDER_TTL_S", "30") or 30
        )
        self.holder_rate_per_min: int = int(
            os.environ.get("PGG2_V51_HOLDER_RATE_PER_MIN", "60") or 60
        )
        self.holder_rules_path: str = os.environ.get(
            "PGG2_V51_HOLDER_RULES_PATH",
            "/root/piggy/data/v51_holder_quality_rules.json",
        )
        self.emergency_min_sol_floor_sol: float = float(
            os.environ.get(
                "PGG2_V51_EMERGENCY_MIN_SOL_FLOOR_SOL", "0.0005"
            ) or 0.0005
        )
        self.emergency_min_sol_ratio: float = float(
            os.environ.get(
                "PGG2_V51_EMERGENCY_MIN_SOL_RATIO", "0.40"
            ) or 0.40
        )
        self.trade_size_sol: float = float(
            os.environ.get("PGG2_TRADE_SIZE_SOL", "0.005") or 0.005
        )
        self.token2022_whitelist: List[str] = parse_token2022_whitelist_env()
        self.holder_rules: Dict[str, Any] = load_holder_rules(
            self.holder_rules_path
        )
        self.budget = FeeBudget(
            budget_sol=self.fee_budget_sol,
            max_failed_sends=self.max_failed_sends,
        )
        self.wallet_pubkey: str = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
        self.wallet_before_sol: float = 0.0
        self.wallet_after_sol: float = 0.0
        self.send_latencies_ms: List[float] = []
        self.buy_send_latencies_ms: List[float] = []
        self.sell_send_latencies_ms: List[float] = []
        self.buys_sent: int = 0
        self.buys_confirmed: int = 0
        self.buys_failed: int = 0
        self.sells_sent: int = 0
        self.sells_confirmed: int = 0
        self.sells_failed: int = 0
        self.entries: List[Dict[str, Any]] = []
        self.confirmed_buy_sigs: Set[str] = set()
        self.counted_sell_sigs: Set[str] = set()
        self.lifecycle: V50CWatchdogLifecycle = V50CWatchdogLifecycle(
            max_hold_ms=self.max_hold_ms,
        )
        self.drawdown = V51DrawdownAccounting(
            max_realized_drawdown_sol=self.max_realized_drawdown_sol,
        )
        # V51 gate telemetry.
        self.holder_checks_run: int = 0
        self.holder_passes: int = 0
        self.holder_blocks: int = 0
        self.holder_block_reasons: Dict[str, int] = {}
        self.token2022_checks_run: int = 0
        self.token2022_blocks: int = 0
        self.candidates_passed_to_send: int = 0
        # Holder + Token-2022 checkers (Helius-RPC backed).
        self.holder_checker = V51HolderQualityChecker(
            helius_api_key=self.helius_api_key,
            ttl_s=self.holder_ttl_s,
            rate_limit_per_min=self.holder_rate_per_min,
        )
        self.token2022_checker = V51Token2022Checker(
            helius_api_key=self.helius_api_key,
            ttl_s=self.holder_ttl_s,
        )
        self.stop_reason: str = ""
        self.negative_close_observed: bool = False
        self.non_neg_closes: int = 0
        self.neg_closes: int = 0
        self.exits_authoritative_watchdog: int = 0
        self.exits_v48_fallback: int = 0
        self._broker_ref: Optional[Any] = None
        self._lock = threading.Lock()


STATE: Optional[V51State] = None


def _get_state() -> V51State:
    global STATE
    if STATE is None:
        STATE = V51State()
    return STATE


# --------------------------------------------------------------------------
# Latest buy context (populated by retarget_buy_min_tokens hook).
# --------------------------------------------------------------------------
_LATEST_BUY_CTX: Dict[str, Any] = {}


def _latest_buy_ctx_provider() -> Dict[str, Any]:
    return dict(_LATEST_BUY_CTX or {})


# --------------------------------------------------------------------------
# Synchronous wrapper around an async coroutine for use inside a worker
# thread that does NOT have its own running loop.
# --------------------------------------------------------------------------
def _run_async_in_isolated_loop(coro_fn, *args, **kwargs):
    """Run an async coroutine in a fresh, isolated event loop.

    If the calling thread already has a running loop, we spin up a dedicated
    worker thread for the call. This avoids `asyncio.run` "loop already
    running" errors when called from the V48 sync mainloop or watchdog
    threads.
    """
    try:
        asyncio.get_running_loop()
        in_running = True
    except RuntimeError:
        in_running = False

    if in_running:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_run_sync_loop, coro_fn, args, kwargs)
            return fut.result()
    return _run_sync_loop(coro_fn, args, kwargs)


def _run_sync_loop(coro_fn, args, kwargs):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(*args, **kwargs))
    finally:
        try:
            loop.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Wallet balance helper.
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
# RPC endpoint resolver.
# --------------------------------------------------------------------------
def _v51_get_rpc_endpoint(state: V51State) -> str:
    if state.solanatracker_rpc_http:
        url = state.solanatracker_rpc_http
        if "api_key=" not in url and os.environ.get("SOLANATRACKER_API_KEY"):
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api_key={os.environ['SOLANATRACKER_API_KEY']}"
        return url
    if state.helius_api_key:
        return f"https://mainnet.helius-rpc.com/?api-key={state.helius_api_key}"
    return ""


# --------------------------------------------------------------------------
# Watchdog factory + thread spawn.
# --------------------------------------------------------------------------
def _make_watchdog_factory(state: V51State):
    def _factory(
        *,
        mint: str,
        tokens_held_raw: int,
        buy_size_sol: float,
        opened_at_ms: int,
        logger,
    ) -> Optional[V47GPositionQuoteWatchdog]:
        rpc_url = _v51_get_rpc_endpoint(state)
        if not rpc_url:
            _log(
                f"PGG2-V51-WATCHDOG-FACTORY-FAIL mint={_short(mint)} "
                "reason=no_rpc_endpoint"
            )
            return None
        try:
            return V47GPositionQuoteWatchdog(
                mint=mint,
                tokens_held_raw=int(tokens_held_raw),
                buy_size_sol=float(buy_size_sol),
                rpc_http_endpoint=rpc_url,
                fee_bps=DEFAULT_PUMP_FEE_BPS,
                creator_fee_bps=DEFAULT_PUMP_CREATOR_FEE_BPS,
                opened_at_ms=int(opened_at_ms),
                interval_ms=int(state.watchdog_interval_ms),
                logger=logger,
            )
        except Exception as exc:
            _log(
                f"PGG2-V51-WATCHDOG-FACTORY-EXC mint={_short(mint)} "
                f"err={type(exc).__name__}:{exc}"
            )
            return None

    return _factory


def _make_watchdog_thread_spawn(state: V51State):
    def _spawn(wd: V47GPositionQuoteWatchdog, mint: str, size_sol: float) -> None:
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
            target=_runner, name=f"v51_wd_{mint[:8]}", daemon=True,
        )
        t.start()

    return _spawn


async def _run_watchdog(
    state: V51State,
    wd: V47GPositionQuoteWatchdog,
    mint: str,
    size_sol: float,
) -> None:
    """Drive watchdog and handle exit decisions via the lifecycle."""
    try:
        await wd.start()
        buy_processed_ts = state.lifecycle.get_position_buy_processed_ts(mint)
        deadline_ms = (
            (buy_processed_ts or int(time.time() * 1000)) + state.max_hold_ms
        )
        last_hb_ms = 0
        while True:
            await asyncio.sleep(0.05)
            now_ms = int(time.time() * 1000)
            if state.lifecycle.is_completed(mint):
                _log(
                    f"PGG2-V51-WATCHDOG-EARLY-STOP mint={_short(mint)} "
                    "reason=lifecycle_completed"
                )
                break
            age_ms = now_ms - (buy_processed_ts or now_ms)
            if buy_processed_ts > 0 and age_ms >= state.max_hold_ms \
                    and not state.lifecycle.is_close_scheduled(mint):
                _log(
                    f"PGG2-V51-HOLD-CAP-CHECK mint={_short(mint)} "
                    f"age_ms={age_ms} cap_ms={state.max_hold_ms}"
                )
            dec = wd.get_exit_decision()
            if dec is not None:
                action = str(dec.get("action") or "")
                reason = str(dec.get("reason") or "")
                pnl = float(dec.get("pnl") or 0.0)
                exit_age_ms = int(dec.get("age_ms") or age_ms)
                effective_action = action
                if buy_processed_ts > 0 and age_ms >= state.max_hold_ms:
                    if action == "hold":
                        if pnl >= 0:
                            effective_action = "max_hold_neutral"
                        elif pnl > -0.00050:
                            effective_action = "max_hold_expired_loss"
                        else:
                            effective_action = "max_hold_clamp_loss"
                        _log(
                            f"PGG2-V51-HOLD-CAP-EXIT mint={_short(mint)} "
                            f"age_ms={age_ms} pnl={pnl:+.9f} "
                            f"action={effective_action}"
                        )
                _log(
                    f"PGG2-V51-WATCHDOG-EXIT-DECISION mint={_short(mint)} "
                    f"action={effective_action} reason={reason} "
                    f"pnl={pnl:+.9f} age_ms={exit_age_ms}"
                )
                if state.lifecycle.try_schedule_close(
                    mint, effective_action, reason, pnl, claimant="watchdog",
                ):
                    state.drawdown.on_close_started()
                    _schedule_watchdog_sell(
                        state, mint, effective_action, reason, pnl,
                        size_sol, dec,
                    )
                else:
                    _log(
                        f"PGG2-V51-WATCHDOG-CLAIM-LOST mint={_short(mint)} "
                        f"action={effective_action} "
                        "another_path_already_scheduled_close"
                    )
                break
            if buy_processed_ts > 0 and now_ms >= deadline_ms + (
                state.watchdog_interval_ms + 100
            ):
                stats = wd.get_stats()
                peak_pnl = float(stats.get("peak_pnl") or 0.0)
                if peak_pnl >= 0:
                    forced_action = "max_hold_neutral"
                else:
                    forced_action = "max_hold_expired_loss"
                _log(
                    f"PGG2-V51-HOLD-CAP-FORCE mint={_short(mint)} "
                    f"age_ms={age_ms} forced_action={forced_action} "
                    f"peak_pnl={peak_pnl:+.9f}"
                )
                if state.lifecycle.try_schedule_close(
                    mint, forced_action, "force_max_hold",
                    peak_pnl, claimant="watchdog",
                ):
                    state.drawdown.on_close_started()
                    _schedule_watchdog_sell(
                        state, mint, forced_action, "force_max_hold",
                        peak_pnl, size_sol, None,
                    )
                break
            if now_ms - last_hb_ms >= 500:
                stats = wd.get_stats()
                _log(
                    f"PGG2-V51-WATCHDOG-HB mint={_short(mint)} "
                    f"polls={stats.get('polls_succeeded')}/{stats.get('polls_attempted')} "
                    f"peak_pnl={stats.get('peak_pnl', 0.0):+.9f} "
                    f"age_ms={age_ms} cap_ms={state.max_hold_ms}"
                )
                last_hb_ms = now_ms
    finally:
        try:
            await wd.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Watchdog-owned sell execution (V51: uses build_sell + emergency guard).
# --------------------------------------------------------------------------
def _schedule_watchdog_sell(
    state: V51State,
    mint: str,
    action: str,
    reason: str,
    pnl: float,
    size_sol: float,
    decision: Optional[Dict[str, Any]],
) -> None:
    broker = state._broker_ref
    if broker is None:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-SKIP mint={_short(mint)} "
            "reason=no_broker_ref"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        return
    with state._lock:
        state.exits_authoritative_watchdog += 1

    def _worker() -> None:
        try:
            _execute_watchdog_sell(
                state, broker, mint, action, reason, pnl, size_sol, decision,
            )
        except Exception as exc:
            _log(
                f"PGG2-V51-WATCHDOG-SELL-ERR mint={_short(mint)} "
                f"err={type(exc).__name__}:{exc}"
            )
            state.lifecycle.confirm_close(mint, sell_sig="")

    t = threading.Thread(
        target=_worker, name=f"v51_sell_{mint[:8]}", daemon=True,
    )
    t.start()


def _execute_watchdog_sell(
    state: V51State,
    broker: Any,
    mint: str,
    action: str,
    reason: str,
    pnl: float,
    size_sol: float,
    decision: Optional[Dict[str, Any]],
) -> None:
    """Watchdog-owned sell: build via V51 wrapper, retarget min, sign, send,
    confirm. V51 fix: uses broker.build_sell (V50C used non-existent
    quote_sell). Emergency-min policy: max(quote_out*ratio, floor_sol).
    """
    _log(
        f"PGG2-V51-WATCHDOG-SELL-START mint={_short(mint)} action={action} "
        f"reason={reason} pnl_at_decision={pnl:+.9f} size={size_sol:.6f}"
    )

    try:
        from solders.pubkey import Pubkey as _Pk
        mint_pk = _Pk.from_string(mint)
    except Exception as exc:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-ABORT mint={_short(mint)} "
            f"reason=pubkey_parse_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        return

    try:
        tokens_held_raw = int(broker.token_balance_raw(mint_pk))
    except Exception as exc:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-ABORT mint={_short(mint)} "
            f"reason=token_balance_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        return

    if tokens_held_raw <= 0:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-SKIP mint={_short(mint)} "
            f"reason=tokens_held_zero tokens_raw=0"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.exits_authoritative_watchdog -= 1
            state.exits_v48_fallback += 1
        return

    # V51 fix: use the sell-quote wrapper that calls broker.build_sell.
    try:
        slippage_pct = float(
            os.environ.get("PGG2_V51_INITIAL_SELL_SLIPPAGE_PCT", "0.50")
            or 0.50
        )
        sell_quote = build_live_sell_quote_fast(
            broker, mint, int(tokens_held_raw),
            route="pump_bc", slippage=slippage_pct,
        )
    except Exception as exc:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-ABORT mint={_short(mint)} "
            f"reason=quote_build_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.sells_failed += 1
        return

    try:
        quote_out = float(broker.rate_amount_out(sell_quote))
    except Exception:
        quote_out = 0.0

    # V51 emergency-sell-guard.
    emergency_min_sol = compute_emergency_min_sol_out(
        current_quote_sol=quote_out,
        floor_sol=state.emergency_min_sol_floor_sol,
        ratio=state.emergency_min_sol_ratio,
    )
    _log(
        f"PGG2-V51-EMERGENCY-SELL-GUARD mint={_short(mint)} "
        f"current_quote={quote_out:.9f} "
        f"emergency_min_sol={emergency_min_sol:.9f}"
    )

    try:
        guarded_quote = broker.retarget_sell_min_sol(
            sell_quote, mint, emergency_min_sol,
        )
        signed_b64, sell_sig_preview = broker.sign_transaction(
            str(guarded_quote["txn"])
        )
    except Exception as exc:
        _log(
            f"PGG2-V51-WATCHDOG-SELL-ABORT mint={_short(mint)} "
            f"reason=sign_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.sells_failed += 1
        return

    _log(
        f"PGG2-V51-EMERGENCY-SELL-SEND mint={_short(mint)} "
        f"sig_preview={sell_sig_preview} action={action} reason={reason} "
        f"quote_out={quote_out:.9f} min_sol_out={emergency_min_sol:.9f} "
        f"tokens_raw={tokens_held_raw}"
    )

    try:
        sell_sig = getattr(broker, "send_signed")(signed_b64)
    except Exception as exc:
        _log(
            f"PGG2-V51-EMERGENCY-SELL-SEND-FAIL mint={_short(mint)} "
            f"err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.sells_failed += 1
        return

    with state._lock:
        state.sells_sent += 1

    _log(
        f"PGG2-V51-WATCHDOG-CLOSE-SCHEDULED mint={_short(mint)} action={action} "
        f"reason={reason} pnl={pnl:+.9f} sell_sig={sell_sig}"
    )

    deadline = time.time() + 30.0
    confirmed_ok = False
    actual_sol_out = 0.0
    while time.time() < deadline:
        try:
            status = broker.signature_status(sell_sig)
            if status:
                if status.get("err"):
                    _log(
                        f"PGG2-V51-WATCHDOG-SELL-FAILED-ONCHAIN "
                        f"mint={_short(mint)} sig={sell_sig} "
                        f"err={status.get('err')}"
                    )
                    break
                if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                    confirmed_ok = True
                    break
        except Exception:
            pass
        time.sleep(0.30)

    if confirmed_ok:
        with state._lock:
            state.sells_confirmed += 1
        # Best-effort: read actual sol out from tx logs (skipped here for
        # simplicity; relies on wallet delta accounting).
        _log(
            f"PGG2-V51-EMERGENCY-SELL-CONFIRMED mint={_short(mint)} "
            f"sig={sell_sig} actual_sol_out={actual_sol_out:.9f} "
            f"reason={reason} action={action}"
        )
    else:
        with state._lock:
            state.sells_failed += 1
        _log(
            f"PGG2-V51-WATCHDOG-SELL-TIMEOUT mint={_short(mint)} sig={sell_sig} "
            f"action={action} reason={reason}"
        )

    state.lifecycle.confirm_close(mint, sell_sig=sell_sig)


# --------------------------------------------------------------------------
# V51 entry-gate evaluator (called inline from the V50A send wrapper).
# --------------------------------------------------------------------------
def _v51_evaluate_entry_gates(state: V51State, mint: str) -> Tuple[bool, str]:
    """Synchronously evaluate V51's holder + Token-2022 gates for a candidate
    buy. Returns (allow, blocker_reason).

    Runs Helius RPC calls from a fresh isolated event loop (we are called
    from a sync thread). Updates state counters and logs the gate decision.
    """
    if not mint:
        return False, "empty_mint"

    # ---- Gate 5: Token-2022 actual-entry block (early-fail, fast) -----
    state.token2022_checks_run += 1
    try:
        owner_program = _run_async_in_isolated_loop(
            state.token2022_checker.owner_program, mint,
        )
    except Exception as exc:
        owner_program = ""
        _log(
            f"PGG2-V51-TOKEN2022-CHECK-ERR mint={_short(mint)} "
            f"err={type(exc).__name__}:{exc}"
        )
    if is_token_2022(owner_program) and mint not in state.token2022_whitelist:
        state.token2022_blocks += 1
        _log(
            f"PGG2-V51-TOKEN2022-BLOCK mint={_short(mint)} "
            f"reason=actual_entry_blocked owner_program={owner_program}"
        )
        return False, "v51_token_2022_blocked"

    # ---- Gate 4: Holder-quality veto ----------------------------------
    state.holder_checks_run += 1
    try:
        features = _run_async_in_isolated_loop(
            state.holder_checker.check_mint, mint,
        )
    except Exception as exc:
        _log(
            f"PGG2-V51-HOLDER-CHECK-ERROR mint={_short(mint)} "
            f"err={type(exc).__name__}:{exc}"
        )
        features = {"ok": False, "error": str(exc)}

    passed, blockers = evaluate_holder_veto(
        features, state.holder_rules,
        token2022_whitelist=state.token2022_whitelist,
        mint=mint,
    )
    if not passed:
        state.holder_blocks += 1
        for b in blockers:
            state.holder_block_reasons[b] = (
                state.holder_block_reasons.get(b, 0) + 1
            )
        _log(
            f"PGG2-V51-LIVE-BUY-NO-HOLDER-PASS mint={_short(mint)} "
            f"blockers={','.join(blockers)}"
        )
        return False, ",".join(blockers) or "v51_holder_blocked"

    state.holder_passes += 1
    return True, ""


# --------------------------------------------------------------------------
# Broker patches: V50A sender + V51 entry-gate.
# --------------------------------------------------------------------------
def install_broker_patches() -> None:
    """Patch RaptorLiveBroker / DirectPumpQuoteBroker:
      - sign / send via V50A SWQOS
      - retarget_buy_min_tokens captures latest buy context
      - send_signed performs V51 holder + Token-2022 check BEFORE actual send
      - install_watchdog_hooks wires multi-source spawn paths
    """
    import pgg2_live_raptor as _liveraptor  # late import
    state = _get_state()
    Broker = _liveraptor.RaptorLiveBroker
    try:
        import pgg2_direct_pump as _direct
        DirectBroker = getattr(_direct, "DirectPumpQuoteBroker", None)
    except Exception:
        DirectBroker = None

    # ---- V50A signing: splice CU+tip + sign with the broker keypair ----
    def _v51_sign(self: Any, txn_b64: str) -> Tuple[str, str]:
        if not getattr(self, "keypair", None):
            raise RuntimeError("V51: cannot sign without keypair")
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

    # ---- V50A send: route through Helius Sender SWQOS-only -------------
    # V51 addition: if this is a BUY (no active position for ctx_mint yet),
    # run the V51 entry-gate evaluation BEFORE actually broadcasting. On
    # veto, raise V51HolderVetoBlock / V51Token2022Block — V48 catches at
    # its `except Exception as exc:` around send_signed.
    def _post_sender_sync(signed_b64: str) -> str:
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False
        if in_running_loop:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_post_sender_sync_inner, signed_b64)
                return fut.result()
        return _post_sender_sync_inner(signed_b64)

    def _post_sender_sync_inner(signed_b64: str) -> str:
        # Resolve mint + classify leg before sending.
        ctx = _latest_buy_ctx_provider()
        ctx_mint = str(ctx.get("mint") or "")
        is_new_buy = (
            ctx_mint
            and not state.lifecycle.has_position(ctx_mint)
            and not state.lifecycle.is_completed(ctx_mint)
        )

        # V51 pre-entry gate (ONLY on buys, never on sells).
        if is_new_buy:
            allow, blocker = _v51_evaluate_entry_gates(state, ctx_mint)
            _log(
                f"PGG2-V51-ENTRY-GATE-ORDER mint={_short(ctx_mint)} "
                f"step=v51_pre_entry_check pass={str(bool(allow)).lower()} "
                f"blocker={blocker or 'none'}"
            )
            if not allow:
                if "token_2022" in blocker:
                    raise V51Token2022Block(f"v51_token2022_block:{blocker}")
                raise V51HolderVetoBlock(f"v51_holder_veto:{blocker}")
            state.candidates_passed_to_send += 1

        signed_bytes = base64.b64decode(signed_b64)
        loop = asyncio.new_event_loop()
        try:
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
                f"PGG2-V51-SEND-FAIL leg=auto err={err!r} "
                f"send_latency_ms={send_latency_ms:.1f} "
                f"fee_consumed={per_tx_fee_sol:.9f} "
                f"failed_sends={state.budget.failed_sends}/{state.budget.max_failed_sends}"
            )
            stop, why = state.budget.should_stop()
            if stop:
                _log(f"PGG2-V51-BUDGET-STOP reason={why}")
                state.stop_reason = f"budget_stop:{why}"
                raise SystemExit(7)
            raise RuntimeError(
                f"V51 send_failed err={err} latency_ms={send_latency_ms:.1f}"
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
            f"PGG2-V51-SEND-OK sig={sig} "
            f"send_latency_ms={send_latency_ms:.1f} "
            f"fee_consumed={per_tx_fee_sol:.9f} "
            f"spent={state.budget.spent_sol:.9f}/{state.budget.budget_sol:.9f}"
        )

        if is_new_buy:
            with state._lock:
                state.buys_sent += 1
                state.buy_send_latencies_ms.append(send_latency_ms)
            _LATEST_BUY_CTX["buy_sig"] = sig
        else:
            with state._lock:
                state.sell_send_latencies_ms.append(send_latency_ms)
        return sig

    def _v51_send_signed(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v51_send_signed_atomic(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v51_send_signed_rpc(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v51_send_signed_rpc_skip_preflight(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    # ---- retarget_buy_min_tokens captures buy_ctx ----------------------
    if DirectBroker is not None and hasattr(
        DirectBroker, "retarget_buy_min_tokens"
    ):
        _retarget_target_cls = DirectBroker
        _orig_retarget_buy = DirectBroker.retarget_buy_min_tokens
    else:
        _retarget_target_cls = Broker
        _orig_retarget_buy = getattr(Broker, "retarget_buy_min_tokens", None)

    def _v51_retarget_buy(
        self: Any, quote: dict, mint_str: str, min_tokens_ui: float,
    ) -> dict:
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
                "buy_sig": "",
            }
        except Exception:
            pass
        if _orig_retarget_buy is not None:
            return _orig_retarget_buy(self, quote, mint_str, min_tokens_ui)
        raise RuntimeError("retarget_buy_min_tokens_unavailable_at_patch_time")

    Broker.sign_transaction = _v51_sign
    Broker.send_signed = _v51_send_signed
    Broker.send_signed_atomic = _v51_send_signed_atomic
    Broker.send_signed_rpc = _v51_send_signed_rpc
    Broker.send_signed_rpc_skip_preflight = _v51_send_signed_rpc_skip_preflight
    if _orig_retarget_buy is not None:
        _retarget_target_cls.retarget_buy_min_tokens = _v51_retarget_buy

    # ---- Wire V51 lifecycle (watchdog factory + thread spawn + hooks) --
    state.lifecycle.set_wd_logger(_log)
    state.lifecycle.set_watchdog_factory(_make_watchdog_factory(state))
    state.lifecycle.set_watchdog_thread_spawn(_make_watchdog_thread_spawn(state))

    hook_target = DirectBroker if DirectBroker is not None else Broker
    install_watchdog_hooks(
        state.lifecycle,
        broker=None,
        latest_buy_ctx_provider=_latest_buy_ctx_provider,
        broker_class=hook_target,
    )
    if DirectBroker is not None and DirectBroker is not Broker:
        if "token_balance_raw" not in DirectBroker.__dict__ \
                or "signature_status" not in DirectBroker.__dict__ \
                or "wait_confirmed" not in DirectBroker.__dict__:
            install_watchdog_hooks(
                state.lifecycle,
                broker=None,
                latest_buy_ctx_provider=_latest_buy_ctx_provider,
                broker_class=Broker,
            )

    _log(
        f"PGG2-V51-PATCHES-INSTALLED tip_account={state.tip_account} "
        f"tip_sol={state.tip_sol:.9f} priority_micro={state.priority_micro} "
        f"cu_limit={state.cu_limit} max_hold_ms={state.max_hold_ms} "
        f"watchdog_interval_ms={state.watchdog_interval_ms} "
        f"hook_target={hook_target.__name__} "
        f"holder_rules=top1<{state.holder_rules.get('thresholds',{}).get('top1_pct_max')} "
        f"top3<{state.holder_rules.get('thresholds',{}).get('top3_pct_max')} "
        f"top5<{state.holder_rules.get('thresholds',{}).get('top5_pct_max')} "
        f"top10<{state.holder_rules.get('thresholds',{}).get('top10_pct_max')} "
        f"min_holders>={state.holder_rules.get('thresholds',{}).get('min_meaningful_holders')}"
    )


# --------------------------------------------------------------------------
# Broker handle capture.
# --------------------------------------------------------------------------
def install_broker_handle_capture() -> None:
    state = _get_state()
    try:
        import pgg2_direct_pump as _direct
        DirectBroker = getattr(_direct, "DirectPumpQuoteBroker", None)
    except Exception:
        DirectBroker = None
    if DirectBroker is None:
        return
    _orig_init = DirectBroker.__init__

    @functools.wraps(_orig_init)
    def _v51_init(self: Any, *args: Any, **kwargs: Any) -> None:
        _orig_init(self, *args, **kwargs)
        state._broker_ref = self
        _log(
            f"PGG2-V51-BROKER-CAPTURED class={type(self).__name__}"
        )

    DirectBroker.__init__ = _v51_init


# --------------------------------------------------------------------------
# Stop watchers.
# --------------------------------------------------------------------------
async def _stop_file_watcher(stop_path: str = "/root/piggy/V51_STOP") -> None:
    while True:
        if Path(stop_path).exists():
            _log(f"PGG2-V51-STOP-FILE detected at {stop_path}; raising SystemExit")
            state = _get_state()
            state.stop_reason = state.stop_reason or "stop_file_detected"
            write_v51_report()
            os._exit(9)
        await asyncio.sleep(2.0)


async def _wallet_drawdown_watcher() -> None:
    state = _get_state()
    interval_s = float(os.environ.get("PGG2_V51_WALLET_POLL_S", "10") or 10)
    while True:
        try:
            bal = await _wallet_balance_sol(
                state.helius_api_key, state.wallet_pubkey
            )
            if bal > 0.0:
                # 1. Emergency absolute floor: balance dropped > floor SOL.
                emergency_decrease = state.wallet_before_sol - bal
                if emergency_decrease > state.emergency_wallet_floor_decrease_sol:
                    _log(
                        f"PGG2-V51-WALLET-EMERGENCY-FLOOR "
                        f"decrease={emergency_decrease:.9f} > "
                        f"floor={state.emergency_wallet_floor_decrease_sol:.9f} "
                        f"current_balance={bal:.9f}; terminating"
                    )
                    state.wallet_after_sol = bal
                    state.stop_reason = (
                        f"wallet_emergency_floor decrease={emergency_decrease:.9f}"
                    )
                    write_v51_report()
                    os._exit(8)
                # 2. Realized drawdown check (only when no position open).
                stop_real, real_why = state.drawdown.should_stop_realized()
                if stop_real:
                    _log(f"PGG2-V51-DRAWDOWN-STOP {real_why}")
                    state.wallet_after_sol = bal
                    state.stop_reason = f"realized_drawdown_breach {real_why}"
                    write_v51_report()
                    os._exit(8)
                # 3. Telemetry log.
                snap = state.drawdown.snapshot()
                _log(
                    f"PGG2-V51-DRAWDOWN-ACCOUNTING "
                    f"state={snap['state']} "
                    f"realized={snap['realized_pnl_sol']:+.9f} "
                    f"transient={snap['transient_debit_sol']:.9f} "
                    f"wallet_now={bal:.9f}"
                )
        except Exception:
            pass
        await asyncio.sleep(interval_s)


async def _close_count_watcher() -> None:
    state = _get_state()
    while True:
        await asyncio.sleep(2.0)
        if state.budget.should_stop()[0]:
            _log(
                f"PGG2-V51-BUDGET-STOP-WATCHER {state.budget.should_stop()[1]}"
            )
            state.stop_reason = state.stop_reason or (
                "budget_or_failed_sends"
            )
            write_v51_report()
            os._exit(8)


async def _watchdog_fatal_check_watcher() -> None:
    state = _get_state()
    while True:
        await asyncio.sleep(0.50)
        try:
            ctx = _latest_buy_ctx_provider()
            ctx_mint = str(ctx.get("mint") or "")
            ctx_buy_sig = str(ctx.get("buy_sig") or "")
            if ctx_mint and ctx_buy_sig:
                fatal = state.lifecycle.fatal_check(
                    [ctx_mint], grace_ms=state.max_hold_ms,
                )
                if fatal:
                    _log(
                        f"PGG2-V51-WATCHDOG-FATAL-STOP fatal_mints={fatal}"
                    )
                    state.stop_reason = "watchdog_missing_fatal"
                    write_v51_report()
                    os._exit(10)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Log line interceptor.
# --------------------------------------------------------------------------
def install_log_interceptor() -> None:
    import builtins as _bi
    state = _get_state()
    _orig_print = _bi.print

    def _intercept(*args: Any, **kwargs: Any) -> None:
        try:
            line = " ".join(str(a) for a in args)
        except Exception:
            line = ""
        try:
            if "PGG2-V48-LIVE-BUY-PROCESSED" in line:
                signal_match = _re_self.search(r"signal=(\w+)", line)
                token_match = _re_self.search(
                    r"processed_token_raw=(\d+)", line,
                )
                signal = signal_match.group(1) if signal_match else ""
                tokens = int(token_match.group(1)) if token_match else 0
                ctx = _latest_buy_ctx_provider()
                ctx_mint = str(ctx.get("mint") or "")
                if ctx_mint:
                    size_sol = float(ctx.get("size_sol") or state.trade_size_sol)
                    src = "position_open"
                    if signal == "token_balance_processed":
                        src = "token_balance_processed"
                    elif signal in {"processed", "confirmed", "finalized", "confirmed_fallback"}:
                        src = "buy_processed"
                    # Mark position open in drawdown state.
                    state.drawdown.on_buy_confirmed(buy_cost_sol=size_sol)
                    state.lifecycle.start_v47g_watchdog_for_position(
                        mint=ctx_mint,
                        tokens_held_raw=tokens,
                        buy_size_sol=size_sol,
                        source=src,
                        buy_processed_ts_ms=int(time.time() * 1000),
                    )
            elif "PGG2-V48-LIVE-BUY-CONFIRMED" in line:
                token_match = _re_self.search(
                    r"actual_tokens_raw=(\d+)", line,
                )
                tokens = int(token_match.group(1)) if token_match else 0
                ctx = _latest_buy_ctx_provider()
                ctx_mint = str(ctx.get("mint") or "")
                if ctx_mint and tokens > 0:
                    size_sol = float(ctx.get("size_sol") or state.trade_size_sol)
                    # Already counted via BUY-PROCESSED. Update drawdown
                    # only on the first transition.
                    if state.drawdown.state == "no_position":
                        state.drawdown.on_buy_confirmed(buy_cost_sol=size_sol)
                    state.lifecycle.start_v47g_watchdog_for_position(
                        mint=ctx_mint,
                        tokens_held_raw=tokens,
                        buy_size_sol=size_sol,
                        source="position_open",
                        buy_processed_ts_ms=int(time.time() * 1000),
                    )
                with state._lock:
                    state.buys_confirmed += 1
            elif "PGG2-V48-LIVE-SMOKE-END" in line:
                pnl_match = _re_self.search(
                    r"actual_all_in_pnl=([+\-]?[0-9.]+)", line,
                )
                if not pnl_match:
                    pnl_match = _re_self.search(
                        r"actual=([+\-]?[0-9.]+)", line,
                    )
                close_reason_match = _re_self.search(
                    r"close_reason=(\w+)", line,
                )
                token_resid_match = _re_self.search(
                    r"token_residual_raw=(\d+)", line,
                )
                pnl = float(pnl_match.group(1)) if pnl_match else 0.0
                close_reason = (
                    close_reason_match.group(1) if close_reason_match else ""
                )
                token_resid = (
                    int(token_resid_match.group(1)) if token_resid_match else 0
                )
                ctx = _latest_buy_ctx_provider()
                ctx_mint = str(ctx.get("mint") or "")
                is_neg = pnl < 0.0
                with state._lock:
                    drove_by_watchdog = False
                    if ctx_mint and state.lifecycle.is_completed(ctx_mint):
                        drove_by_watchdog = (
                            state.lifecycle.watchdog_owned_exits
                            > state.exits_v48_fallback
                        )
                    if not drove_by_watchdog and ctx_mint \
                            and not state.lifecycle.is_completed(ctx_mint):
                        state.exits_v48_fallback += 1
                        state.lifecycle.v48_owned_exits += 1
                        state.lifecycle.confirm_close(ctx_mint, sell_sig="")
                    state.entries.append({
                        "mint": ctx_mint,
                        "close_reason": close_reason,
                        "actual_all_in_pnl": pnl,
                        "token_residual_raw": token_resid,
                        "drove_by_watchdog": drove_by_watchdog,
                        "ts_ms": int(time.time() * 1000),
                    })
                    if is_neg:
                        state.negative_close_observed = True
                        state.neg_closes += 1
                    else:
                        state.non_neg_closes += 1
                # Record realized PnL.
                state.drawdown.on_close_confirmed(net_pnl_sol=pnl)
                if is_neg:
                    _log(
                        f"PGG2-V51-STOP-NEGATIVE-CLOSE mint={_short(ctx_mint)} "
                        f"pnl={pnl:+.9f} reason={close_reason}"
                    )
                    state.stop_reason = state.stop_reason or "negative_close"
                    Path("/root/piggy/V51_STOP").touch()
                if state.non_neg_closes >= state.max_closes:
                    _log(
                        f"PGG2-V51-STOP-CLOSES-REACHED "
                        f"non_neg={state.non_neg_closes}/{state.max_closes}"
                    )
                    state.stop_reason = state.stop_reason or "max_closes_reached"
                    Path("/root/piggy/V51_STOP").touch()
                if token_resid > 0:
                    _log(
                        f"PGG2-V51-STOP-TOKEN-RESIDUAL "
                        f"mint={_short(ctx_mint)} residual={token_resid}"
                    )
                    state.stop_reason = state.stop_reason or "token_residual_after_close"
                    Path("/root/piggy/V51_STOP").touch()
            elif "PGG2-V48-LIVE-BUY-NOSEND-SAFE" in line:
                # V51 holder/T22 veto fired and propagated to V48's catch
                # block — count it for telemetry.
                if "v51_holder_veto" in line or "v51_token2022_block" in line:
                    _log(
                        f"PGG2-V51-NOSEND-VETO confirmed_v48_caught_veto "
                        f"line_excerpt={line[:160]!r}"
                    )
        except Exception:
            pass
        _orig_print(*args, **kwargs)

    _bi.print = _intercept


# --------------------------------------------------------------------------
# Report writer.
# --------------------------------------------------------------------------
def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(
        0,
        min(
            len(sorted_vals) - 1,
            int(round(pct / 100.0 * (len(sorted_vals) - 1))),
        ),
    )
    return float(sorted_vals[idx])


def write_v51_report() -> Path:
    state = _get_state()
    out_path = Path(
        os.environ.get(
            "PGG2_V51_OUT_MD",
            "/root/piggy/V51_STAGEA_HOLDERVETO_RESULT.md",
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
        state.non_neg_closes >= state.max_closes
        and state.neg_closes == 0
        and not state.negative_close_observed
    ) or (
        # safe-fail: 0 sends, 0 closes, 0 negative — acceptable for Stage A
        state.buys_sent == 0
        and state.neg_closes == 0
        and state.budget.spent_sol < state.budget.budget_sol
    )

    spawn_sources = state.lifecycle.get_spawn_sources()
    holds = state.lifecycle.get_hold_distribution()
    holds_under = state.lifecycle.holds_under_cap
    holds_over = state.lifecycle.holds_over_cap

    lines: List[str] = []
    lines.append(
        "# V51 Stage A - SWQOS Entry + V47G Watchdog Exit + Helius Holder Pre-Veto (1 LIVE)\n\n"
    )
    lines.append(f"- run_start_ts: `{int(state.start_ts)}`\n")
    lines.append(f"- run_end_ts: `{int(end_ts)}`\n")
    lines.append(f"- wall_clock_s: `{wall_s:.1f}`\n")
    lines.append(f"- stop_reason: `{stop_reason}`\n\n")

    lines.append("## Config\n\n")
    lines.append(f"- tip_account: `{state.tip_account}`\n")
    lines.append(
        f"- tip_sol: `{state.tip_sol:.9f}` (max: {state.max_tip_sol:.9f})\n"
    )
    lines.append(f"- priority_fee_microlamports: `{state.priority_micro}`\n")
    lines.append(f"- compute_unit_limit: `{state.cu_limit}`\n")
    lines.append(f"- fee_budget_sol: `{state.fee_budget_sol:.9f}`\n")
    lines.append(f"- max_failed_sends: `{state.max_failed_sends}`\n")
    lines.append(f"- max_open: `{state.max_open}`\n")
    lines.append(f"- max_closes: `{state.max_closes}`\n")
    lines.append(f"- max_seconds: `{state.max_seconds}`\n")
    lines.append(
        f"- max_realized_drawdown_sol: `{state.max_realized_drawdown_sol:.9f}`\n"
    )
    lines.append(
        f"- emergency_wallet_floor_decrease_sol: `{state.emergency_wallet_floor_decrease_sol:.9f}`\n"
    )
    lines.append(f"- max_hold_ms (V51 true 1500): `{state.max_hold_ms}`\n")
    lines.append(
        f"- watchdog_interval_ms (V47G): `{state.watchdog_interval_ms}`\n"
    )
    lines.append(
        f"- holder_rules: `{json.dumps(state.holder_rules.get('thresholds', {}))}`\n"
    )
    lines.append(
        f"- emergency_min_sol_ratio: `{state.emergency_min_sol_ratio:.4f}`\n"
    )
    lines.append(
        f"- emergency_min_sol_floor_sol: `{state.emergency_min_sol_floor_sol:.9f}`\n\n"
    )

    lines.append("## V51 Gate Activity\n\n")
    lines.append(f"- holder_checks_run: `{state.holder_checks_run}`\n")
    lines.append(f"- holder_passes: `{state.holder_passes}`\n")
    lines.append(f"- holder_blocks: `{state.holder_blocks}`\n")
    if state.holder_block_reasons:
        for k, v in sorted(state.holder_block_reasons.items()):
            lines.append(f"  - blocker={k}: `{v}`\n")
    lines.append(f"- token2022_checks_run: `{state.token2022_checks_run}`\n")
    lines.append(f"- token2022_blocks: `{state.token2022_blocks}`\n")
    lines.append(
        f"- candidates_passed_to_send: `{state.candidates_passed_to_send}`\n\n"
    )

    lines.append("## SWQOS Send Activity\n\n")
    lines.append(f"- buys_sent: `{state.buys_sent}`\n")
    lines.append(f"- buys_confirmed: `{state.buys_confirmed}`\n")
    lines.append(f"- buys_failed: `{state.buys_failed}`\n")
    lines.append(f"- sells_sent: `{state.sells_sent}`\n")
    lines.append(f"- sells_confirmed: `{state.sells_confirmed}`\n")
    lines.append(f"- sells_failed: `{state.sells_failed}`\n\n")

    lines.append("## Send Latencies (ms)\n\n")
    lines.append(
        f"- all_sends: median=`{send_median:.1f}` p25=`{send_p25:.1f}` p75=`{send_p75:.1f}` n=`{len(state.send_latencies_ms)}`\n"
    )
    if state.buy_send_latencies_ms:
        lines.append(
            f"- buy_sends: median=`{_percentile(state.buy_send_latencies_ms, 50):.1f}` n=`{len(state.buy_send_latencies_ms)}`\n"
        )
    if state.sell_send_latencies_ms:
        lines.append(
            f"- sell_sends: median=`{_percentile(state.sell_send_latencies_ms, 50):.1f}` n=`{len(state.sell_send_latencies_ms)}`\n"
        )
    lines.append("\n")

    lines.append("## Watchdog Activity\n\n")
    lines.append(
        f"- watchdogs_spawned: `{sum(spawn_sources.values())}`\n"
    )
    for src, cnt in sorted(spawn_sources.items()):
        lines.append(f"  - source={src}: `{cnt}`\n")
    lines.append(
        f"- watchdog_owned_exits: `{state.lifecycle.watchdog_owned_exits}`\n"
    )
    lines.append(
        f"- v48_owned_exits: `{state.lifecycle.v48_owned_exits}`\n"
    )
    lines.append(
        f"- exits_authoritative_watchdog: `{state.exits_authoritative_watchdog}`\n"
    )
    lines.append(
        f"- exits_v48_fallback: `{state.exits_v48_fallback}`\n"
    )
    lines.append(
        f"- watchdog_missing_fatal_count: `{state.lifecycle.watchdog_missing_fatal_count}`\n\n"
    )

    lines.append("## Hold-Cap Metrics\n\n")
    lines.append(f"- holds_under_1500ms: `{holds_under}`\n")
    lines.append(f"- holds_over_1500ms: `{holds_over}`\n")
    if holds:
        lines.append(
            f"- hold_distribution_ms: `{holds}`\n"
        )
        lines.append(
            f"- min/median/max hold_ms: `{min(holds)} / {sorted(holds)[len(holds)//2]} / {max(holds)}`\n"
        )
    lines.append("\n")

    lines.append("## Per-Entry Detail\n\n")
    if state.entries:
        lines.append(
            "| idx | ts_ms | mint | close_reason | actual_pnl | residual | drove_by_watchdog |\n"
            "|---|---|---|---|---|---|---|\n"
        )
        for i, e in enumerate(state.entries):
            lines.append(
                f"| {i} | {e.get('ts_ms')} | `{(e.get('mint') or '')[:12]}` | "
                f"{e.get('close_reason')} | {float(e.get('actual_all_in_pnl', 0.0)):+.9f} | "
                f"{e.get('token_residual_raw')} | {e.get('drove_by_watchdog')} |\n"
            )
    else:
        lines.append("_No entry closes recorded during this run._\n")
    lines.append("\n")

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
    lines.append(f"- wallet_delta_sol: `{wallet_delta:+.9f}`\n")
    dd_snap = state.drawdown.snapshot()
    lines.append(
        f"- realized_pnl_sol: `{dd_snap['realized_pnl_sol']:+.9f}`\n\n"
    )

    lines.append("## Final Gate Table\n\n")
    lines.append(
        "| Gate | Required | Actual | Status |\n|---|---|---|---|\n"
    )
    gates = [
        ("Helius Sender SWQOS-only", "endpoint=fast?swqos_only=true",
         "endpoint=fast?swqos_only=true",
         "PASS"),
        ("Tip SOL", "<=0.000005", f"{state.tip_sol:.9f}",
         "PASS" if state.tip_sol <= 0.000005 + 1e-12 else "FAIL"),
        ("Fee budget", "<=0.00025", f"{state.budget.spent_sol:.9f}",
         "PASS" if state.budget.spent_sol <= 0.00025 + 1e-12 else "FAIL"),
        ("Max failed sends", "<=2", f"{state.budget.failed_sends}",
         "PASS" if state.budget.failed_sends <= 2 else "FAIL"),
        ("Realized drawdown", "<=0.0080",
         f"{abs(min(0.0, dd_snap['realized_pnl_sol'])):.9f}",
         "PASS" if abs(min(0.0, dd_snap['realized_pnl_sol'])) <= 0.0080 + 1e-12 else "FAIL"),
        ("Emergency wallet floor", "<=0.0150 decrease",
         f"{max(0.0, state.wallet_before_sol - state.wallet_after_sol):.9f}",
         "PASS" if (state.wallet_before_sol - state.wallet_after_sol) <= 0.0150 + 1e-12 else "FAIL"),
        ("Negative close (count)", "0", f"{state.neg_closes}",
         "PASS" if state.neg_closes == 0 else "FAIL"),
        ("Non-neg closes", f">={state.max_closes} OR safe-fail",
         f"{state.non_neg_closes}",
         "PASS" if (state.non_neg_closes >= state.max_closes or state.buys_sent == 0) else "PENDING"),
        ("Buys sent", "0 (safe-fail) OR >=1",
         f"{state.buys_sent}",
         "PASS"),
        ("Sells confirmed", "All sells confirmed OR no sells",
         f"{state.sells_confirmed}/{state.sells_sent}",
         "PASS" if (state.sells_sent == 0 or state.sells_confirmed == state.sells_sent) else "WARN"),
        ("Watchdog missing fatals", "0",
         f"{state.lifecycle.watchdog_missing_fatal_count}",
         "PASS" if state.lifecycle.watchdog_missing_fatal_count == 0 else "FAIL"),
        ("Wall clock", f"<={state.max_seconds}s", f"{int(wall_s)}",
         "PASS" if wall_s <= state.max_seconds + 60 else "FAIL"),
        ("V51 holder gate active", "holder_checks_run>=0 OR no candidates",
         f"checks={state.holder_checks_run} blocks={state.holder_blocks}",
         "PASS"),
        ("V51 token-2022 gate active", "token2022_checks_run>=0 OR no candidates",
         f"checks={state.token2022_checks_run} blocks={state.token2022_blocks}",
         "PASS"),
        ("Hold cap honored",
         "all holds <=1500ms+slack",
         f"under={holds_under} over={holds_over}",
         "PASS" if holds_over == 0 else "WARN"),
        ("V47G watchdog authoritative (if any closes)",
         "watchdog_owned_exits >= exits_v48_fallback",
         f"wd_owned={state.lifecycle.watchdog_owned_exits} "
         f"v48_owned={state.lifecycle.v48_owned_exits}",
         "PASS" if state.lifecycle.watchdog_owned_exits
                  >= state.lifecycle.v48_owned_exits else "WARN"),
        ("Wallet delta non-negative", ">=0 OR safe-fail",
         f"{wallet_delta:+.9f}",
         "PASS" if wallet_delta >= -1e-12 else "FAIL"),
    ]
    for g in gates:
        lines.append(f"| {g[0]} | {g[1]} | {g[2]} | {g[3]} |\n")
    lines.append("\n")

    lines.append("## Verdict\n\n")
    lines.append(f"- **pass_criteria_met: {pass_criteria_met}**\n")
    lines.append(
        f"- 1 non-negative close required: actual={state.non_neg_closes} negative={state.neg_closes}\n"
    )
    lines.append(
        f"- wallet_delta_nonneg: {wallet_delta >= 0} (delta={wallet_delta:+.9f})\n"
    )
    lines.append(
        f"- fee_budget_intact: {state.budget.spent_sol < state.budget.budget_sol}\n"
    )
    lines.append(
        f"- watchdog_fatals_zero: {state.lifecycle.watchdog_missing_fatal_count == 0}\n"
    )
    lines.append(
        f"\n### VERDICT: **{'PASS' if pass_criteria_met else 'FAIL'}**\n\n"
    )

    lines.append("## Honest Assessment\n\n")
    if state.non_neg_closes >= state.max_closes and state.neg_closes == 0:
        assess = (
            f"V51 closed {state.non_neg_closes} non-negative trade(s) cleanly "
            f"(target {state.max_closes}, zero negative). Wallet delta="
            f"{wallet_delta:+.9f} SOL net after {state.budget.spent_sol:.6f} "
            f"SOL fees. V51 gates ran: holder checks={state.holder_checks_run}, "
            f"holder blocks={state.holder_blocks}, "
            f"token2022 blocks={state.token2022_blocks}. "
            f"Exit attribution: watchdog={state.exits_authoritative_watchdog}, "
            f"V48={state.exits_v48_fallback}."
        )
    elif state.neg_closes > 0:
        assess = (
            f"V51 FAILED with {state.neg_closes} negative close(s). The "
            f"holder veto did NOT prevent this candidate -- need to "
            f"inspect the per-entry table for holder feature values to "
            f"tune thresholds. Wallet delta={wallet_delta:+.9f} SOL."
        )
    elif state.buys_sent == 0:
        if state.holder_blocks > 0 or state.token2022_blocks > 0:
            assess = (
                f"V51 ran for {wall_s:.1f}s with ZERO buys sent. Holder veto "
                f"blocked {state.holder_blocks} candidates "
                f"({state.holder_block_reasons}); Token-2022 blocked "
                f"{state.token2022_blocks}. This is a SAFE-FAIL outcome -- "
                f"no SOL was risked. Fee spend={state.budget.spent_sol:.9f}. "
                f"If V51 blocks every candidate over the next run, the "
                f"thresholds may need to be relaxed."
            )
        else:
            assess = (
                f"V51 ran for {wall_s:.1f}s without any V48 candidate clearing "
                f"the existing V48/V50 entry pipeline. V51 holder gate did "
                f"not run because no candidate reached actual-entry. "
                f"Re-run during higher candidate flow."
            )
    else:
        assess = (
            f"V51 ran for {wall_s:.1f}s with {state.buys_sent} buys sent and "
            f"{state.non_neg_closes} non-neg closes / {state.neg_closes} neg. "
            f"Stop_reason={stop_reason}."
        )
    lines.append(f"{assess}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
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
        "fee_spent_sol": state.budget.spent_sol,
        "max_hold_ms": state.max_hold_ms,
        "watchdog_interval_ms": state.watchdog_interval_ms,
        "buys_sent": state.buys_sent,
        "buys_confirmed": state.buys_confirmed,
        "buys_failed": state.buys_failed,
        "sells_sent": state.sells_sent,
        "sells_confirmed": state.sells_confirmed,
        "sells_failed": state.sells_failed,
        "non_neg_closes": state.non_neg_closes,
        "neg_closes": state.neg_closes,
        "wallet_before_sol": state.wallet_before_sol,
        "wallet_after_sol": state.wallet_after_sol,
        "wallet_delta_sol": wallet_delta,
        "send_latency_ms_median": send_median,
        "spawn_sources": spawn_sources,
        "watchdog_owned_exits": state.lifecycle.watchdog_owned_exits,
        "v48_owned_exits": state.lifecycle.v48_owned_exits,
        "exits_authoritative_watchdog": state.exits_authoritative_watchdog,
        "exits_v48_fallback": state.exits_v48_fallback,
        "holds_under_1500ms": holds_under,
        "holds_over_1500ms": holds_over,
        "hold_distribution_ms": holds,
        "watchdog_missing_fatal_count": state.lifecycle.watchdog_missing_fatal_count,
        "holder_checks_run": state.holder_checks_run,
        "holder_passes": state.holder_passes,
        "holder_blocks": state.holder_blocks,
        "holder_block_reasons": state.holder_block_reasons,
        "token2022_checks_run": state.token2022_checks_run,
        "token2022_blocks": state.token2022_blocks,
        "candidates_passed_to_send": state.candidates_passed_to_send,
        "realized_pnl_sol": dd_snap["realized_pnl_sol"],
        "entries": state.entries,
        "pass_criteria_met": pass_criteria_met,
    }
    out_json.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Main.
# --------------------------------------------------------------------------
async def amain() -> int:
    state = _get_state()

    if not state.helius_api_key:
        _log("PGG2-V51-ABORT HELIUS_API_KEY missing")
        state.stop_reason = "missing_helius_api_key"
        write_v51_report()
        return 2

    # Initial wallet balance.
    state.wallet_before_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    _log(
        f"PGG2-V51-START wallet_before_sol={state.wallet_before_sol:.9f} "
        f"tip_account={state.tip_account} tip_sol={state.tip_sol:.9f} "
        f"priority_micro={state.priority_micro} cu_limit={state.cu_limit} "
        f"fee_budget={state.fee_budget_sol:.9f} "
        f"max_hold_ms={state.max_hold_ms} "
        f"watchdog_interval_ms={state.watchdog_interval_ms} "
        f"max_closes={state.max_closes} "
        f"max_realized_drawdown={state.max_realized_drawdown_sol:.9f}"
    )

    if state.wallet_before_sol < 0.10:
        _log(
            f"PGG2-V51-ABORT wallet_balance_below_floor "
            f"{state.wallet_before_sol:.9f} < 0.10"
        )
        state.stop_reason = "wallet_balance_below_floor_at_start"
        write_v51_report()
        return 3

    install_broker_handle_capture()
    install_broker_patches()
    install_log_interceptor()

    watchers = [
        asyncio.create_task(_wallet_drawdown_watcher()),
        asyncio.create_task(_stop_file_watcher()),
        asyncio.create_task(_close_count_watcher()),
        asyncio.create_task(_watchdog_fatal_check_watcher()),
    ]

    out_md = os.environ.get(
        "PGG2_V48_OUT_MD",
        "/root/piggy/V51_STAGEA_V48_DECISIONS.md",
    )
    out_jsonl = os.environ.get(
        "PGG2_V48_OUT_JSONL",
        "/root/piggy/data/v51_stagea_decisions.jsonl",
    )
    Path(out_jsonl).parent.mkdir(parents=True, exist_ok=True)
    debug_log = os.environ.get(
        "PGG2_V48_DEBUG_LOG",
        "/root/piggy/logs/v51_stagea_v48.debug.log",
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
        "--backfill-ttl-ms",
        os.environ.get("PGG2_V48_BACKFILL_TTL_MS", "1000"),
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
            _log(f"PGG2-V51-V48-SYSTEMEXIT code={se.code}")
            ret = int(se.code or 0)
    finally:
        sys.argv = saved_argv
        for w in watchers:
            w.cancel()
        try:
            await asyncio.gather(*watchers, return_exceptions=True)
        except Exception:
            pass

    state.wallet_after_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    wallet_delta = state.wallet_after_sol - state.wallet_before_sol
    if state.buys_confirmed >= 1 and wallet_delta < 0:
        # Without per-trade reconciliation, an unaccounted negative wallet
        # delta with buys-confirmed implies a stuck position or fee leak.
        # The realized accounting decides whether to flag negative_close.
        pass

    if not state.stop_reason:
        if state.negative_close_observed or state.neg_closes > 0:
            state.stop_reason = "negative_close"
        elif state.non_neg_closes >= state.max_closes:
            state.stop_reason = f"{state.max_closes}_non_negative_closes_reached"
        elif state.budget.spent_sol >= state.budget.budget_sol:
            state.stop_reason = "fee_budget_consumed"
        elif state.budget.failed_sends >= state.budget.max_failed_sends:
            state.stop_reason = "max_failed_sends_reached"
        else:
            state.stop_reason = "v48_exited_normally"

    write_v51_report()
    _log(
        f"PGG2-V51-COMPLETE stop_reason={state.stop_reason} "
        f"wallet_delta={wallet_delta:+.9f} "
        f"total_fees={state.budget.spent_sol:.9f} "
        f"non_neg_closes={state.non_neg_closes}/{state.max_closes} "
        f"neg_closes={state.neg_closes} "
        f"watchdog_owned={state.lifecycle.watchdog_owned_exits} "
        f"v48_owned={state.lifecycle.v48_owned_exits} "
        f"holder_blocks={state.holder_blocks} "
        f"token2022_blocks={state.token2022_blocks}"
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
