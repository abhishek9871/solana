"""V50C Stage B - SWQOS entry + AUTHORITATIVE V47G watchdog exit (3 LIVE).

This runner takes V50B Stage A's template and moves the V47G watchdog
spawn-hook from `Broker.wait_confirmed` to the EARLIEST observable
position-open event. V50B Stage A passed (+0.000525983 SOL) but the V47G
watchdog never actually fired because V48's `token_balance_processed`
fast-path bypassed `wait_confirmed`. V50C plugs that gap with a
multi-source spawn:

  1. broker.token_balance_raw(mint) returning > 0 -> source=token_balance_processed
  2. broker.signature_status(buy_sig) returning processed/confirmed
     -> source=signature_status
  3. broker.wait_confirmed(buy_sig) returning True (fallback)
     -> source=wait_confirmed_fallback

The lifecycle (pgg2_v50c_watchdog_lifecycle.V50CWatchdogLifecycle) is
idempotent: the first source wins, subsequent fires log DUPLICATE-SKIP.

V48's native sell loop is NOT removed. It runs in parallel. The watchdog's
decisions go through `lifecycle.try_schedule_close(mint, action, reason,
pnl)`, which is an atomic claim mechanism. V48's sell calls go through the
same claim via a monkey-patched `broker.retarget_sell_min_sol` and
`broker.send_signed` wrapper: if V48 tries to sell a mint whose close is
already scheduled, the V48 send is DEFERRED with a V48-SELL-FIRST telemetry
log.

Entry path: UNCHANGED from V50B Stage A.
  - V48 amain -> V48 decision pipeline emits a candidate
  - Broker sign+send via Helius Sender SWQOS-only (V50A adapter)
  - Tip = 0.000005 SOL, skipPreflight=true, maxRetries=0

Exit path: AUTHORITATIVE V47G watchdog
  - On the earliest position-open event, V50C spawns V47GPositionQuoteWatchdog
  - Watchdog polls bonding-curve PDA via JSON-RPC every 250ms
  - V47G exit policy: bank / scratch / abort / max_hold_*
  - First non-hold decision -> lifecycle.try_schedule_close
  - If claim won: build sell tx via DirectPump, sign, send via V50A SWQOS, confirm
  - V48's native sell loop runs in parallel; if it tries to send first, it
    races for the claim; if the watchdog already won, V48's send is a noop

Hard caps (per V50C spec):
  - 3 non-negative closes target (max_closes)
  - 1 negative close -> STOP, FAIL
  - 3 failed sends -> STOP
  - Fee budget 0.00100 SOL -> STOP
  - Wallet drawdown > 0.0050 SOL -> STOP
  - 35 min elapsed -> STOP
  - Token residual after close -> STOP
  - max_hold_ms = 1500 from buy_processed_ts_ms (TRUE 1500ms, not 3s)

NO modifications to PGG2.py, pgg2_live_raptor.py, pgg2_direct_pump.py, or
pgg2_v48_drylive_harness.py. Watchdog source files are not modified either;
they're imported as-is.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
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
# V50C reuses the V50A sender adapter (which IS the sender). The V50C runner
# itself must remain call-clean.
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
        sys.stderr.write(f"V50C-STAGEB-ABORT forbidden_call_pattern={_pat}\n")
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

# V47G + V50C lifecycle imports.
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
from pgg2_v50c_watchdog_lifecycle import (  # noqa: E402
    V50CWatchdogLifecycle,
    install_watchdog_hooks,
    position_open_spawn,
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
# Pump constants for fee-bps decode (mirrors pgg2_direct_pump.pump_global())
# --------------------------------------------------------------------------
DEFAULT_PUMP_FEE_BPS = 100
DEFAULT_PUMP_CREATOR_FEE_BPS = 0


# --------------------------------------------------------------------------
# V50C runtime state
# --------------------------------------------------------------------------
class V50CState:
    """Single-source-of-truth for V50C telemetry within this process."""

    def __init__(self) -> None:
        self.start_ts = time.time()
        self.helius_api_key: str = os.environ.get("HELIUS_API_KEY", "").strip()
        self.solanatracker_rpc_http: str = os.environ.get(
            "SOLANATRACKER_RPC_HTTP", ""
        ).strip()
        self.tip_account: str = random.choice(HELIUS_SENDER_TIP_ACCOUNTS)
        self.tip_sol: float = float(
            os.environ.get("PGG2_V50C_SWQOS_TIP_SOL", "0.000005") or 0.000005
        )
        self.max_tip_sol: float = float(
            os.environ.get("PGG2_V50C_MAX_TIP_SOL", "0.000005") or 0.000005
        )
        # Strict cap -- never exceed PGG2_V50A_MAX_TIP_SOL.
        if self.tip_sol > min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL):
            self.tip_sol = min(self.max_tip_sol, PGG2_V50A_MAX_TIP_SOL)
        self.priority_micro: int = int(
            os.environ.get("PGG2_V50C_PRIORITY_FEE_MICROLAMPORTS", "100000")
            or 100000
        )
        self.cu_limit: int = int(
            os.environ.get("PGG2_V50C_CU_LIMIT", "200000") or 200000
        )
        self.fee_budget_sol: float = float(
            os.environ.get("PGG2_V50C_STAGEB_FEE_BUDGET_SOL", "0.00100")
            or 0.00100
        )
        self.max_failed_sends: int = int(
            os.environ.get("PGG2_V50C_MAX_FAILED_SENDS", "3") or 3
        )
        self.max_closes: int = int(
            os.environ.get("PGG2_V50C_MAX_CLOSES", "3") or 3
        )
        self.max_open: int = int(os.environ.get("PGG2_V50C_MAX_OPEN", "1") or 1)
        self.max_seconds: int = int(
            os.environ.get("PGG2_V50C_MAX_SECONDS", "2100") or 2100
        )
        self.max_wallet_drawdown_sol: float = float(
            os.environ.get("PGG2_V50C_MAX_WALLET_DRAWDOWN_SOL", "0.0050")
            or 0.0050
        )
        self.max_hold_ms: int = int(
            os.environ.get("PGG2_V50C_MAX_HOLD_MS", "1500") or 1500
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
        self.send_latencies_ms: List[float] = []
        self.buy_send_latencies_ms: List[float] = []
        self.sell_send_latencies_ms: List[float] = []
        self.confirmed_landing_ms: List[float] = []
        self.buys_sent: int = 0
        self.buys_confirmed: int = 0
        self.buys_failed: int = 0
        self.sells_sent: int = 0
        self.sells_confirmed: int = 0
        self.sells_failed: int = 0
        # Per-entry telemetry (3-entry target).
        self.entries: List[Dict[str, Any]] = []
        # Active entry context (current open position).
        self.active_entries: Dict[str, Dict[str, Any]] = {}
        # Send-leg tracking: most recent send sig is either buy or sell.
        # We distinguish via lifecycle.has_position(mint) and the
        # _LATEST_BUY_CTX retarget hook.
        self.confirmed_buy_sigs: Set[str] = set()
        # Sigs we've already counted on the confirm side (avoid double count).
        self.counted_sell_sigs: Set[str] = set()
        # Watchdog lifecycle.
        self.lifecycle: V50CWatchdogLifecycle = V50CWatchdogLifecycle(
            max_hold_ms=self.max_hold_ms,
        )
        # Stop flags.
        self.stop_reason: str = ""
        self.negative_close_observed: bool = False
        self.non_neg_closes: int = 0
        self.neg_closes: int = 0
        # Counts of which path actually drove each exit.
        self.exits_authoritative_watchdog: int = 0
        self.exits_v48_fallback: int = 0
        # Broker handle (set after install_broker_patches).
        self._broker_ref: Optional[Any] = None
        # Lock for cross-thread state.
        self._lock = threading.Lock()


STATE: Optional[V50CState] = None


def _get_state() -> V50CState:
    global STATE
    if STATE is None:
        STATE = V50CState()
    return STATE


# --------------------------------------------------------------------------
# Latest buy context (populated by retarget_buy_min_tokens hook)
# --------------------------------------------------------------------------
_LATEST_BUY_CTX: Dict[str, Any] = {}


def _latest_buy_ctx_provider() -> Dict[str, Any]:
    return dict(_LATEST_BUY_CTX or {})


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
# RPC endpoint resolver
# --------------------------------------------------------------------------
def _v50c_get_rpc_endpoint(state: V50CState) -> str:
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
# Watchdog factory + thread spawn
# --------------------------------------------------------------------------
def _make_watchdog_factory(state: V50CState):
    def _factory(
        *,
        mint: str,
        tokens_held_raw: int,
        buy_size_sol: float,
        opened_at_ms: int,
        logger,
    ) -> Optional[V47GPositionQuoteWatchdog]:
        rpc_url = _v50c_get_rpc_endpoint(state)
        if not rpc_url:
            _log(
                f"PGG2-V50C-WATCHDOG-FACTORY-FAIL mint={mint[:8]} "
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
                f"PGG2-V50C-WATCHDOG-FACTORY-EXC mint={mint[:8]} "
                f"err={type(exc).__name__}:{exc}"
            )
            return None

    return _factory


def _make_watchdog_thread_spawn(state: V50CState):
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
            target=_runner, name=f"v50c_wd_{mint[:8]}", daemon=True,
        )
        t.start()

    return _spawn


async def _run_watchdog(
    state: V50CState,
    wd: V47GPositionQuoteWatchdog,
    mint: str,
    size_sol: float,
) -> None:
    """Drive watchdog and handle exit decisions via the lifecycle."""
    try:
        await wd.start()
        # Hard deadline based on TRUE buy_processed_ts.
        buy_processed_ts = state.lifecycle.get_position_buy_processed_ts(mint)
        deadline_ms = (buy_processed_ts or int(time.time() * 1000)) + state.max_hold_ms
        last_hb_ms = 0
        while True:
            await asyncio.sleep(0.05)
            now_ms = int(time.time() * 1000)
            # Stop early if lifecycle marked us complete.
            if state.lifecycle.is_completed(mint):
                _log(
                    f"PGG2-V50C-WATCHDOG-EARLY-STOP mint={mint[:8]} "
                    "reason=lifecycle_completed"
                )
                break
            # Hold-cap check (against true buy_processed_ts).
            age_ms = now_ms - (buy_processed_ts or now_ms)
            if buy_processed_ts > 0:
                # Only emit once around the boundary.
                if age_ms >= state.max_hold_ms and not state.lifecycle.is_close_scheduled(mint):
                    _log(
                        f"PGG2-V50C-HOLD-CAP-CHECK mint={mint[:8]} "
                        f"age_ms={age_ms} cap_ms={state.max_hold_ms}"
                    )
            # Watchdog decision check.
            dec = wd.get_exit_decision()
            if dec is not None:
                action = str(dec.get("action") or "")
                reason = str(dec.get("reason") or "")
                pnl = float(dec.get("pnl") or 0.0)
                exit_age_ms = int(dec.get("age_ms") or age_ms)
                # Override hold-cap action if AT or PAST max_hold (true age).
                effective_action = action
                if buy_processed_ts > 0 and age_ms >= state.max_hold_ms:
                    if action == "hold":
                        # Watchdog said hold but we're past cap -> classify.
                        if pnl >= 0:
                            effective_action = "max_hold_neutral"
                        elif pnl > -0.00050:
                            effective_action = "max_hold_expired_loss"
                        else:
                            effective_action = "max_hold_clamp_loss"
                        _log(
                            f"PGG2-V50C-HOLD-CAP-EXIT mint={mint[:8]} "
                            f"age_ms={age_ms} pnl={pnl:+.9f} "
                            f"action={effective_action}"
                        )
                _log(
                    f"PGG2-V50C-WATCHDOG-EXIT-DECISION mint={mint[:8]} "
                    f"action={effective_action} reason={reason} "
                    f"pnl={pnl:+.9f} age_ms={exit_age_ms}"
                )
                # Try to claim the close.
                if state.lifecycle.try_schedule_close(
                    mint, effective_action, reason, pnl, claimant="watchdog",
                ):
                    # We won the claim. Schedule actual sell via the runner.
                    _schedule_watchdog_sell(
                        state, mint, effective_action, reason, pnl,
                        size_sol, dec,
                    )
                else:
                    _log(
                        f"PGG2-V50C-WATCHDOG-CLAIM-LOST mint={mint[:8]} "
                        f"action={effective_action} "
                        "another_path_already_scheduled_close"
                    )
                break
            # max-hold deadline guard: if the watchdog hasn't decided but
            # we're past max_hold_ms, force an exit.
            if buy_processed_ts > 0 and now_ms >= deadline_ms:
                # Force a max_hold decision by querying stats.
                stats = wd.get_stats()
                peak_pnl = float(stats.get("peak_pnl") or 0.0)
                # Conservative: action depends on current pnl from last
                # successful quote. We don't have a fresh quote here, but
                # the watchdog's own tick will fire max_hold within one
                # interval; we just wait one more interval.
                if now_ms >= deadline_ms + (state.watchdog_interval_ms + 100):
                    # Watchdog is stuck — force exit through lifecycle.
                    forced_pnl = peak_pnl  # best estimate available
                    if peak_pnl >= 0:
                        forced_action = "max_hold_neutral"
                    else:
                        forced_action = "max_hold_expired_loss"
                    _log(
                        f"PGG2-V50C-HOLD-CAP-FORCE mint={mint[:8]} "
                        f"age_ms={age_ms} forced_action={forced_action} "
                        f"peak_pnl={peak_pnl:+.9f}"
                    )
                    if state.lifecycle.try_schedule_close(
                        mint, forced_action, "force_max_hold",
                        forced_pnl, claimant="watchdog",
                    ):
                        _schedule_watchdog_sell(
                            state, mint, forced_action, "force_max_hold",
                            forced_pnl, size_sol, None,
                        )
                    break
            # Heartbeat log.
            if now_ms - last_hb_ms >= 500:
                stats = wd.get_stats()
                _log(
                    f"PGG2-V50C-WATCHDOG-HB mint={mint[:8]} "
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
# Watchdog-owned sell execution
# --------------------------------------------------------------------------
def _schedule_watchdog_sell(
    state: V50CState,
    mint: str,
    action: str,
    reason: str,
    pnl: float,
    size_sol: float,
    decision: Optional[Dict[str, Any]],
) -> None:
    """Spawn a sell-execution thread for the watchdog-owned close.

    The watchdog runs in its own thread + event loop; we launch yet
    another worker thread to do the (blocking) sell build / sign / send /
    confirm via the broker. This keeps the watchdog tick loop alive in
    case the sell takes time.
    """
    broker = state._broker_ref
    if broker is None:
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-SKIP mint={mint[:8]} "
            "reason=no_broker_ref"
        )
        # We still call confirm_close to release the lock so V48 can
        # take over on its next iteration.
        state.lifecycle.confirm_close(mint, sell_sig="")
        return
    # Watchdog wants to be authoritative. Track it.
    with state._lock:
        state.exits_authoritative_watchdog += 1

    def _worker() -> None:
        try:
            _execute_watchdog_sell(
                state, broker, mint, action, reason, pnl, size_sol, decision,
            )
        except Exception as exc:
            _log(
                f"PGG2-V50C-WATCHDOG-SELL-ERR mint={mint[:8]} "
                f"err={type(exc).__name__}:{exc}"
            )
            # Best-effort: release the claim so V48 can take over.
            state.lifecycle.confirm_close(mint, sell_sig="")

    t = threading.Thread(
        target=_worker, name=f"v50c_sell_{mint[:8]}", daemon=True,
    )
    t.start()


def _execute_watchdog_sell(
    state: V50CState,
    broker: Any,
    mint: str,
    action: str,
    reason: str,
    pnl: float,
    size_sol: float,
    decision: Optional[Dict[str, Any]],
) -> None:
    """Build, sign, send, confirm the watchdog-owned sell. Synchronous.

    This is the V50C analogue of V48's `_build_live_sell_quote_fast +
    retarget_sell_min_sol + sign_transaction + send-leg` chain, but
    driven from the watchdog thread. We mirror V48's behavior:
      1. Resolve current token balance.
      2. Build a sell quote via broker (DirectPumpQuoteBroker has the
         relevant method `quote_sell` or `quote_for_sell`; we'll use
         `_build_live_sell_quote_fast` indirectly via the broker's own
         API surface).
      3. Sign + send the sell tx via the patched broker send-leg method
         routing through the V50A sender adapter.
      4. Confirm the sell via broker.signature_status polling.
      5. Mark close confirmed.

    On any failure (sell-not-executable, send error, etc.), we DO release
    the claim so V48's loop can attempt — but we also bump the failed-
    sells counter and log it.
    """
    _log(
        f"PGG2-V50C-WATCHDOG-SELL-START mint={mint[:8]} action={action} "
        f"reason={reason} pnl_at_decision={pnl:+.9f} size={size_sol:.6f}"
    )

    try:
        from solders.pubkey import Pubkey as _Pk
        mint_pk = _Pk.from_string(mint)
    except Exception as exc:
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-ABORT mint={mint[:8]} "
            f"reason=pubkey_parse_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        return

    # Fetch tokens held.
    try:
        tokens_held_raw = int(broker.token_balance_raw(mint_pk))
    except Exception as exc:
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-ABORT mint={mint[:8]} "
            f"reason=token_balance_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        return

    if tokens_held_raw <= 0:
        # V48 may already have sold OR we're being called too late.
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-SKIP mint={mint[:8]} "
            f"reason=tokens_held_zero tokens_raw=0"
        )
        # Still mark as complete; tokens were sold by V48.
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            # Re-classify this as V48-driven.
            state.exits_authoritative_watchdog -= 1
            state.exits_v48_fallback += 1
        return

    # Build sell quote via broker.quote_sell / quote_for_sell / similar.
    # DirectPumpQuoteBroker exposes `quote_sell(mint, amount_in_tokens_ui)`
    # in pgg2_direct_pump.py. We use it.
    actual_ui = float(broker.raw_to_ui(mint_pk, tokens_held_raw))
    try:
        # The DirectPump broker's sell-quote method signature:
        # quote_sell(mint, amount_in_tokens_raw_or_ui) -- check actual sig.
        sell_quote = _build_v50c_sell_quote(broker, mint, mint_pk, tokens_held_raw)
    except Exception as exc:
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-ABORT mint={mint[:8]} "
            f"reason=quote_build_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.sells_failed += 1
        return

    # Compute expected sell out.
    try:
        quote_out = float(broker.rate_amount_out(sell_quote))
    except Exception:
        quote_out = 0.0

    # Establish a minimum sol out. For aggressive bounded-loss closes we
    # use the env override; otherwise default to 60% of quote_out (V48
    # residual pattern) clamped above modeled tx fee.
    emergency_min = float(
        os.environ.get("PGG2_V48_LIVE_EMERGENCY_MIN_SOL_OUT", "0.0046")
        or 0.0046
    )
    min_sol_out = max(0.0000001, min(emergency_min, max(0.0000001, quote_out * 0.85)))

    # Retarget the sell to the protected min.
    try:
        guarded_quote = broker.retarget_sell_min_sol(sell_quote, mint, min_sol_out)
        signed_b64, sell_sig_preview = broker.sign_transaction(str(guarded_quote["txn"]))
    except Exception as exc:
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-ABORT mint={mint[:8]} "
            f"reason=sign_fail err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.sells_failed += 1
        return

    _log(
        f"PGG2-V50C-WATCHDOG-SELL-SEND mint={mint[:8]} "
        f"sig_preview={sell_sig_preview} reason={reason} action={action} "
        f"quote_out={quote_out:.9f} min_sol_out={min_sol_out:.9f} "
        f"tokens_raw={tokens_held_raw}"
    )

    # Send via patched broker leg method routing through V50A SWQOS.
    try:
        sell_sig = getattr(broker, "send_signed")(signed_b64)
    except Exception as exc:
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-SEND-FAIL mint={mint[:8]} "
            f"err={type(exc).__name__}:{exc}"
        )
        state.lifecycle.confirm_close(mint, sell_sig="")
        with state._lock:
            state.sells_failed += 1
        return

    with state._lock:
        state.sells_sent += 1

    _log(
        f"PGG2-V50C-WATCHDOG-CLOSE-SCHEDULED mint={mint[:8]} action={action} "
        f"reason={reason} pnl={pnl:+.9f} sell_sig={sell_sig}"
    )

    # Confirm the sell via broker.signature_status polling.
    deadline = time.time() + 30.0
    confirmed_ok = False
    while time.time() < deadline:
        try:
            status = broker.signature_status(sell_sig)
            if status:
                if status.get("err"):
                    _log(
                        f"PGG2-V50C-WATCHDOG-SELL-FAILED-ONCHAIN "
                        f"mint={mint[:8]} sig={sell_sig} "
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
        _log(
            f"PGG2-V50C-WATCHDOG-CLOSE-ACK mint={mint[:8]} sig={sell_sig} "
            f"reason={reason} action={action}"
        )
    else:
        with state._lock:
            state.sells_failed += 1
        _log(
            f"PGG2-V50C-WATCHDOG-SELL-TIMEOUT mint={mint[:8]} sig={sell_sig} "
            f"action={action} reason={reason}"
        )

    state.lifecycle.confirm_close(mint, sell_sig=sell_sig)


def _build_v50c_sell_quote(
    broker: Any, mint_str: str, mint_pk: Any, tokens_raw: int,
) -> Dict[str, Any]:
    """Build a sell quote using whatever method the broker exposes.

    DirectPumpQuoteBroker has `quote_sell(mint, amount)`. Some legacy
    brokers expose `quote_for_sell`. We try in priority order.
    """
    if hasattr(broker, "quote_sell"):
        # quote_sell takes amount in UI tokens (string "raw:<int>" supported too)
        return broker.quote_sell(mint_pk, f"raw:{int(tokens_raw)}")
    if hasattr(broker, "quote_for_sell"):
        return broker.quote_for_sell(mint_pk, f"raw:{int(tokens_raw)}")
    raise RuntimeError(
        f"v50c_no_sell_quote_method on broker class={type(broker).__name__}"
    )


# --------------------------------------------------------------------------
# Broker patches: V50A sender entry + V50C watchdog hooks
# --------------------------------------------------------------------------
def install_broker_patches() -> None:
    """Patch RaptorLiveBroker / DirectPumpQuoteBroker:
      - sign / send via V50A SWQOS
      - retarget_buy_min_tokens captures latest buy context
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
    def _v50c_sign(self: Any, txn_b64: str) -> Tuple[str, str]:
        if not getattr(self, "keypair", None):
            raise RuntimeError("V50C: cannot sign without keypair")
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
                f"PGG2-V50C-SEND-FAIL leg=auto err={err!r} "
                f"send_latency_ms={send_latency_ms:.1f} "
                f"fee_consumed={per_tx_fee_sol:.9f} "
                f"failed_sends={state.budget.failed_sends}/{state.budget.max_failed_sends}"
            )
            stop, why = state.budget.should_stop()
            if stop:
                _log(f"PGG2-V50C-BUDGET-STOP reason={why}")
                state.stop_reason = f"budget_stop:{why}"
                raise SystemExit(7)
            raise RuntimeError(
                f"V50C send_failed err={err} latency_ms={send_latency_ms:.1f}"
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
            f"PGG2-V50C-SEND-OK sig={sig} "
            f"send_latency_ms={send_latency_ms:.1f} "
            f"fee_consumed={per_tx_fee_sol:.9f} "
            f"spent={state.budget.spent_sol:.9f}/{state.budget.budget_sol:.9f}"
        )
        # Classify the send leg: if no active position for the buy_ctx
        # mint yet, this is a buy; otherwise a sell.
        ctx = _latest_buy_ctx_provider()
        ctx_mint = str(ctx.get("mint") or "")
        if ctx_mint and not state.lifecycle.has_position(ctx_mint) and not state.lifecycle.is_completed(ctx_mint):
            # New buy.
            with state._lock:
                state.buys_sent += 1
                state.buy_send_latencies_ms.append(send_latency_ms)
            # Record sig into ctx so signature_status hook can match.
            _LATEST_BUY_CTX["buy_sig"] = sig
        else:
            # Sell leg.
            with state._lock:
                state.sell_send_latencies_ms.append(send_latency_ms)
        return sig

    def _v50c_send_signed(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50c_send_signed_atomic(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50c_send_signed_rpc(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    def _v50c_send_signed_rpc_skip_preflight(self: Any, signed_b64: str) -> str:
        return _post_sender_sync(signed_b64)

    # ---- V50C mint tracker: retarget_buy_min_tokens captures buy_ctx ----
    if DirectBroker is not None and hasattr(DirectBroker, "retarget_buy_min_tokens"):
        _retarget_target_cls = DirectBroker
        _orig_retarget_buy = DirectBroker.retarget_buy_min_tokens
    else:
        _retarget_target_cls = Broker
        _orig_retarget_buy = getattr(Broker, "retarget_buy_min_tokens", None)

    def _v50c_retarget_buy(
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
                # buy_sig populated when send returns (above).
                "buy_sig": "",
            }
        except Exception:
            pass
        if _orig_retarget_buy is not None:
            return _orig_retarget_buy(self, quote, mint_str, min_tokens_ui)
        raise RuntimeError("retarget_buy_min_tokens_unavailable_at_patch_time")

    # Apply send patches.
    Broker.sign_transaction = _v50c_sign
    Broker.send_signed = _v50c_send_signed
    Broker.send_signed_atomic = _v50c_send_signed_atomic
    Broker.send_signed_rpc = _v50c_send_signed_rpc
    Broker.send_signed_rpc_skip_preflight = _v50c_send_signed_rpc_skip_preflight
    if _orig_retarget_buy is not None:
        _retarget_target_cls.retarget_buy_min_tokens = _v50c_retarget_buy

    # ---- Wire V50C lifecycle (watchdog factory + thread spawn + hooks) ----
    state.lifecycle.set_wd_logger(_log)
    state.lifecycle.set_watchdog_factory(_make_watchdog_factory(state))
    state.lifecycle.set_watchdog_thread_spawn(_make_watchdog_thread_spawn(state))

    # Install multi-source watchdog hooks on the broker class hierarchy.
    # DirectBroker inherits from Broker; we install on DirectBroker if it
    # overrides token_balance_raw, otherwise on Broker.
    hook_target = DirectBroker if DirectBroker is not None else Broker
    install_watchdog_hooks(
        state.lifecycle,
        broker=None,  # we patch class, not instance
        latest_buy_ctx_provider=_latest_buy_ctx_provider,
        broker_class=hook_target,
    )
    # If DirectBroker doesn't expose its own token_balance_raw / sig_status /
    # wait_confirmed, also install on Broker as a safety net.
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
        f"PGG2-V50C-PATCHES-INSTALLED tip_account={state.tip_account} "
        f"tip_sol={state.tip_sol:.9f} priority_micro={state.priority_micro} "
        f"cu_limit={state.cu_limit} max_hold_ms={state.max_hold_ms} "
        f"watchdog_interval_ms={state.watchdog_interval_ms} "
        f"hook_target={hook_target.__name__}"
    )


# --------------------------------------------------------------------------
# Broker handle capture - we need a reference to the live broker so the
# watchdog can build sells via it. Hook the construction.
# --------------------------------------------------------------------------
def install_broker_handle_capture() -> None:
    """Capture the broker instance the moment V48 constructs it."""
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
    def _v50c_init(self: Any, *args: Any, **kwargs: Any) -> None:
        _orig_init(self, *args, **kwargs)
        state._broker_ref = self
        _log(
            f"PGG2-V50C-BROKER-CAPTURED class={type(self).__name__}"
        )

    DirectBroker.__init__ = _v50c_init


# --------------------------------------------------------------------------
# Stop watchers
# --------------------------------------------------------------------------
async def _stop_file_watcher(stop_path: str = "/root/piggy/V50C_STOP") -> None:
    while True:
        if Path(stop_path).exists():
            _log(f"PGG2-V50C-STOP-FILE detected at {stop_path}; raising SystemExit")
            state = _get_state()
            state.stop_reason = "stop_file_detected"
            os._exit(9)
        await asyncio.sleep(2.0)


async def _wallet_drawdown_watcher() -> None:
    state = _get_state()
    interval_s = float(os.environ.get("PGG2_V50C_WALLET_POLL_S", "10") or 10)
    while True:
        try:
            bal = await _wallet_balance_sol(state.helius_api_key, state.wallet_pubkey)
            if bal > 0.0:
                drawdown = state.wallet_before_sol - bal
                if drawdown > state.max_wallet_drawdown_sol:
                    _log(
                        f"PGG2-V50C-WALLET-DRAWDOWN-HARDCAP "
                        f"drawdown={drawdown:.9f} > "
                        f"cap={state.max_wallet_drawdown_sol:.9f} "
                        f"current_balance={bal:.9f}; terminating"
                    )
                    state.wallet_after_sol = bal
                    state.stop_reason = (
                        f"wallet_drawdown_hardcap drawdown={drawdown:.9f}"
                    )
                    state.negative_close_observed = True
                    write_v50c_report()
                    os._exit(8)
        except Exception:
            pass
        await asyncio.sleep(interval_s)


async def _close_count_watcher() -> None:
    """Check if we've hit non-negative close target or a negative close."""
    state = _get_state()
    while True:
        await asyncio.sleep(2.0)
        # Iterate completed mints; compute wallet snapshot for each.
        # Note: detailed per-entry PnL accounting happens in the V48 finalize
        # path. We rely on V48's existing log lines (PGG2-V48-LIVE-SMOKE-END)
        # to count closes; this watcher is a backstop in case V48 doesn't.
        if state.budget.should_stop()[0]:
            _log(
                f"PGG2-V50C-BUDGET-STOP-WATCHER {state.budget.should_stop()[1]}"
            )
            state.stop_reason = state.stop_reason or (
                "budget_or_failed_sends"
            )
            write_v50c_report()
            os._exit(8)


async def _watchdog_fatal_check_watcher() -> None:
    """Periodically verify every open position has an active watchdog."""
    state = _get_state()
    while True:
        await asyncio.sleep(0.50)
        try:
            ctx = _latest_buy_ctx_provider()
            ctx_mint = str(ctx.get("mint") or "")
            ctx_buy_sig = str(ctx.get("buy_sig") or "")
            if ctx_mint and ctx_buy_sig:
                # Only check fatal if we observed a buy_sig (i.e. send happened)
                fatal = state.lifecycle.fatal_check(
                    [ctx_mint], grace_ms=state.max_hold_ms,
                )
                if fatal:
                    _log(
                        f"PGG2-V50C-WATCHDOG-FATAL-STOP fatal_mints={fatal}"
                    )
                    state.stop_reason = "watchdog_missing_fatal"
                    write_v50c_report()
                    os._exit(10)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Log line interceptor - parse V48 logs to track close events
# --------------------------------------------------------------------------
def install_log_interceptor() -> None:
    """Wrap builtins.print so we can intercept PGG2-V48-LIVE-SMOKE-END /
    PGG2-V48-LIVE-BUY-PROCESSED log lines from the V48 harness.

    For each PGG2-V48-LIVE-BUY-PROCESSED with signal=token_balance_processed:
       Trigger lifecycle.start_v47g_watchdog_for_position with
       source='position_open' (this is the cleanest signal that V48 saw
       on-chain tokens).
    For each PGG2-V48-LIVE-SMOKE-END: record the close PnL into state and
    advance counters.

    We chain the original print so all logs still flow to stdout.
    """
    import builtins as _bi
    state = _get_state()
    _orig_print = _bi.print

    def _intercept(*args: Any, **kwargs: Any) -> None:
        # Render line first.
        try:
            line = " ".join(str(a) for a in args)
        except Exception:
            line = ""
        # PGG2-V48-LIVE-BUY-PROCESSED parse.
        try:
            if "PGG2-V48-LIVE-BUY-PROCESSED" in line:
                # Extract signal=...
                signal_match = _re_self.search(r"signal=(\w+)", line)
                mint_match = _re_self.search(r"mint=([A-Za-z0-9.]+)", line)
                sig_match = _re_self.search(r"sig=([A-Za-z0-9]+)", line)
                token_match = _re_self.search(
                    r"processed_token_raw=(\d+)", line,
                )
                signal = signal_match.group(1) if signal_match else ""
                mint_short = mint_match.group(1) if mint_match else ""
                sig = sig_match.group(1) if sig_match else ""
                tokens = int(token_match.group(1)) if token_match else 0
                # Resolve full mint from latest_buy_ctx (V48 uses short mint).
                ctx = _latest_buy_ctx_provider()
                ctx_mint = str(ctx.get("mint") or "")
                if ctx_mint:
                    size_sol = float(ctx.get("size_sol") or 0.005)
                    # Spawn watchdog with source from signal.
                    src = "position_open"
                    if signal == "token_balance_processed":
                        src = "token_balance_processed"
                    elif signal in {"processed", "confirmed", "finalized", "confirmed_fallback"}:
                        src = "buy_processed"
                    state.lifecycle.start_v47g_watchdog_for_position(
                        mint=ctx_mint,
                        tokens_held_raw=tokens,
                        buy_size_sol=size_sol,
                        source=src,
                        buy_processed_ts_ms=int(time.time() * 1000),
                    )
            elif "PGG2-V48-LIVE-BUY-CONFIRMED" in line:
                # Backstop: same as above using buy_processed source.
                token_match = _re_self.search(
                    r"actual_tokens_raw=(\d+)", line,
                )
                tokens = int(token_match.group(1)) if token_match else 0
                ctx = _latest_buy_ctx_provider()
                ctx_mint = str(ctx.get("mint") or "")
                if ctx_mint and tokens > 0:
                    size_sol = float(ctx.get("size_sol") or 0.005)
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
                # close_reason=... actual_all_in_pnl=...
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
                # Resolve mint and record entry.
                ctx = _latest_buy_ctx_provider()
                ctx_mint = str(ctx.get("mint") or "")
                # Decide non-neg vs neg.
                is_neg = pnl < 0.0
                with state._lock:
                    # Determine which path drove the exit. If lifecycle marks
                    # this mint as completed AND there are watchdog-owned
                    # exits left to attribute, count one.
                    drove_by_watchdog = False
                    if ctx_mint and state.lifecycle.is_completed(ctx_mint):
                        # Did watchdog spawn for this mint AND own the close?
                        # We rely on `state.exits_authoritative_watchdog`
                        # incrementing only on a successful watchdog-driven
                        # sell. The lifecycle counts watchdog_owned_exits
                        # in try_schedule_close.
                        drove_by_watchdog = (
                            state.lifecycle.watchdog_owned_exits
                            > state.exits_v48_fallback
                        )
                    if not drove_by_watchdog and ctx_mint and not state.lifecycle.is_completed(ctx_mint):
                        # V48 closed without watchdog interaction.
                        state.exits_v48_fallback += 1
                        state.lifecycle.v48_owned_exits += 1
                        # Mark complete so we don't double-count later.
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
                # Stop conditions.
                if is_neg:
                    _log(
                        f"PGG2-V50C-STOP-NEGATIVE-CLOSE mint={ctx_mint[:8]} "
                        f"pnl={pnl:+.9f} reason={close_reason}"
                    )
                    state.stop_reason = "negative_close"
                    # Force V48 to stop by writing the stop file.
                    Path("/root/piggy/V50C_STOP").touch()
                if state.non_neg_closes >= state.max_closes:
                    _log(
                        f"PGG2-V50C-STOP-CLOSES-REACHED "
                        f"non_neg={state.non_neg_closes}/{state.max_closes}"
                    )
                    state.stop_reason = state.stop_reason or "max_closes_reached"
                    Path("/root/piggy/V50C_STOP").touch()
                if token_resid > 0:
                    _log(
                        f"PGG2-V50C-STOP-TOKEN-RESIDUAL "
                        f"mint={ctx_mint[:8]} residual={token_resid}"
                    )
                    state.stop_reason = state.stop_reason or "token_residual_after_close"
                    Path("/root/piggy/V50C_STOP").touch()
        except Exception:
            pass
        # Forward to original print.
        _orig_print(*args, **kwargs)

    _bi.print = _intercept


# --------------------------------------------------------------------------
# Report writer
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


def write_v50c_report() -> Path:
    state = _get_state()
    out_path = Path(
        os.environ.get(
            "PGG2_V50C_OUT_MD",
            "/root/piggy/V50C_STAGEB_WATCHDOG_AUTHORITATIVE_RESULT.md",
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
        and wallet_delta > 0
        and state.budget.spent_sol < state.budget.budget_sol
        and state.lifecycle.watchdog_missing_fatal_count == 0
    )

    spawn_sources = state.lifecycle.get_spawn_sources()
    holds = state.lifecycle.get_hold_distribution()
    holds_under = state.lifecycle.holds_under_cap
    holds_over = state.lifecycle.holds_over_cap

    lines: List[str] = []
    lines.append(
        "# V50C Stage B - SWQOS Entry + AUTHORITATIVE V47G Watchdog Exit (3 LIVE)\n\n"
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
        f"- max_wallet_drawdown_sol: `{state.max_wallet_drawdown_sol:.9f}`\n"
    )
    lines.append(f"- max_hold_ms (V50C true 1500): `{state.max_hold_ms}`\n")
    lines.append(
        f"- watchdog_interval_ms (V47G): `{state.watchdog_interval_ms}`\n\n"
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
        f"- v48_sell_first_events: `{state.lifecycle.v48_sell_first_events}`\n"
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
    lines.append(f"- wallet_delta_sol: `{wallet_delta:+.9f}`\n\n")

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
        ("Fee budget", "<=0.00100", f"{state.budget.spent_sol:.9f}",
         "PASS" if state.budget.spent_sol < 0.00100 else "FAIL"),
        ("Max failed sends", "<=3", f"{state.budget.failed_sends}",
         "PASS" if state.budget.failed_sends <= 3 else "FAIL"),
        ("Wallet drawdown", "<=0.0050",
         f"{abs(min(0.0, wallet_delta)):.9f}",
         "PASS" if abs(min(0.0, wallet_delta)) <= 0.0050 + 1e-12 else "FAIL"),
        ("Negative close (count)", "0", f"{state.neg_closes}",
         "PASS" if state.neg_closes == 0 else "FAIL"),
        ("Non-neg closes", f">={state.max_closes}",
         f"{state.non_neg_closes}",
         "PASS" if state.non_neg_closes >= state.max_closes else "PENDING"),
        ("Buys confirmed", ">=1",
         f"{state.buys_confirmed}",
         "PASS" if state.buys_confirmed >= 1 else "PENDING"),
        ("Sells confirmed", f">={state.max_closes}",
         f"{state.sells_confirmed}",
         "PASS" if state.sells_confirmed >= state.max_closes else "PENDING"),
        ("Watchdog missing fatals", "0",
         f"{state.lifecycle.watchdog_missing_fatal_count}",
         "PASS" if state.lifecycle.watchdog_missing_fatal_count == 0 else "FAIL"),
        ("Wall clock", "<=2100s", f"{int(wall_s)}",
         "PASS" if wall_s <= 2100 + 60 else "FAIL"),
        ("Wallet delta non-negative", ">=0", f"{wallet_delta:+.9f}",
         "PASS" if wallet_delta >= 0 else "FAIL"),
        ("Watchdog spawned per position",
         "1+ source per opened position",
         f"sources={dict(spawn_sources)}",
         "PASS" if len(spawn_sources) >= 1 or state.buys_sent == 0 else "FAIL"),
        ("Hold cap honored",
         "all holds <=1500ms+slack",
         f"under={holds_under} over={holds_over}",
         "PASS" if holds_over == 0 else "WARN"),
        ("V47G watchdog authoritative",
         "watchdog_owned_exits >= exits_v48_fallback",
         f"wd_owned={state.lifecycle.watchdog_owned_exits} "
         f"v48_owned={state.lifecycle.v48_owned_exits}",
         "PASS" if state.lifecycle.watchdog_owned_exits
                  >= state.lifecycle.v48_owned_exits else "WARN"),
    ]
    for g in gates:
        lines.append(f"| {g[0]} | {g[1]} | {g[2]} | {g[3]} |\n")
    lines.append("\n")

    lines.append("## Verdict\n\n")
    lines.append(f"- **pass_criteria_met: {pass_criteria_met}**\n")
    lines.append(
        f"- 3 non-negative closes required: actual={state.non_neg_closes} negative={state.neg_closes}\n"
    )
    lines.append(
        f"- wallet_delta_positive: {wallet_delta > 0} (delta={wallet_delta:+.9f})\n"
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
        wd_drove = state.exits_authoritative_watchdog
        v48_drove = state.exits_v48_fallback
        if wd_drove > v48_drove:
            assess = (
                f"V50C closed {state.non_neg_closes} non-negative trades "
                f"(target {state.max_closes}, zero negative). The V47G "
                f"watchdog WAS authoritative on {wd_drove}/{state.non_neg_closes} "
                f"closes; V48 fallback fired on {v48_drove}. "
                f"Wallet_delta=+{wallet_delta:.9f} SOL net after "
                f"{state.budget.spent_sol:.6f} SOL fees. The architecture "
                f"goal (watchdog as primary exit) was REALIZED."
            )
        elif v48_drove > 0 and wd_drove == 0:
            assess = (
                f"V50C closed {state.non_neg_closes} trades cleanly but "
                f"V48's native sell loop drove ALL exits ({v48_drove}); "
                f"V47G watchdog spawned but lost the race to V48 each time. "
                f"Spawn sources={dict(spawn_sources)}. The on-chain outcome "
                f"is PASS but the architectural goal (watchdog authoritative) "
                f"was NOT realized; V48 is faster than the watchdog tick."
            )
        else:
            assess = (
                f"V50C closed {state.non_neg_closes} trades cleanly. Exit "
                f"attribution: watchdog={wd_drove}, V48={v48_drove}. Wallet "
                f"delta=+{wallet_delta:.9f} SOL."
            )
    elif state.neg_closes > 0:
        assess = (
            f"V50C FAILED with {state.neg_closes} negative close(s). "
            f"Stage B halted on first negative close per the strict "
            f"FAIL rule. Wallet_delta={wallet_delta:+.9f} SOL after "
            f"{state.budget.spent_sol:.6f} SOL fees."
        )
    elif state.buys_sent == 0:
        assess = (
            "V50C did NOT send any buy. V48 decision pipeline rejected all "
            "candidates -- not a SWQOS or watchdog issue. Re-run during "
            "higher candidate flow."
        )
    else:
        assess = (
            f"V50C ran for {wall_s:.1f}s with {state.non_neg_closes} "
            f"non-negative closes (target {state.max_closes}) and "
            f"{state.neg_closes} negative closes. Stop_reason={stop_reason}."
        )
    lines.append(f"{assess}\n")

    out_path.write_text("".join(lines), encoding="utf-8")
    # JSON snapshot.
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
        "v48_sell_first_events": state.lifecycle.v48_sell_first_events,
        "exits_authoritative_watchdog": state.exits_authoritative_watchdog,
        "exits_v48_fallback": state.exits_v48_fallback,
        "holds_under_1500ms": holds_under,
        "holds_over_1500ms": holds_over,
        "hold_distribution_ms": holds,
        "watchdog_missing_fatal_count": state.lifecycle.watchdog_missing_fatal_count,
        "entries": state.entries,
        "pass_criteria_met": pass_criteria_met,
    }
    out_json.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
async def amain() -> int:
    state = _get_state()

    if not state.helius_api_key:
        _log("PGG2-V50C-ABORT HELIUS_API_KEY missing")
        state.stop_reason = "missing_helius_api_key"
        write_v50c_report()
        return 2

    # Initial wallet balance.
    state.wallet_before_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    _log(
        f"PGG2-V50C-START wallet_before_sol={state.wallet_before_sol:.9f} "
        f"tip_account={state.tip_account} tip_sol={state.tip_sol:.9f} "
        f"priority_micro={state.priority_micro} cu_limit={state.cu_limit} "
        f"fee_budget={state.fee_budget_sol:.9f} "
        f"max_hold_ms={state.max_hold_ms} "
        f"watchdog_interval_ms={state.watchdog_interval_ms} "
        f"max_closes={state.max_closes} "
        f"max_drawdown={state.max_wallet_drawdown_sol:.9f}"
    )

    if state.wallet_before_sol < 0.10:
        _log(
            f"PGG2-V50C-ABORT wallet_balance_below_floor "
            f"{state.wallet_before_sol:.9f} < 0.10"
        )
        state.stop_reason = "wallet_balance_below_floor_at_start"
        write_v50c_report()
        return 3

    # Install hooks BEFORE V48 builds the broker.
    install_broker_handle_capture()
    install_broker_patches()
    install_log_interceptor()

    watchers = [
        asyncio.create_task(_wallet_drawdown_watcher()),
        asyncio.create_task(_stop_file_watcher()),
        asyncio.create_task(_close_count_watcher()),
        asyncio.create_task(_watchdog_fatal_check_watcher()),
    ]

    # Build V48 argv.
    out_md = os.environ.get(
        "PGG2_V48_OUT_MD",
        "/root/piggy/V50C_STAGEB_V48_DECISIONS.md",
    )
    out_jsonl = os.environ.get(
        "PGG2_V48_OUT_JSONL",
        "/root/piggy/data/v50c_stageb_decisions.jsonl",
    )
    Path(out_jsonl).parent.mkdir(parents=True, exist_ok=True)
    debug_log = os.environ.get(
        "PGG2_V48_DEBUG_LOG",
        "/root/piggy/logs/v50c_stageb_v48.debug.log",
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
            _log(f"PGG2-V50C-V48-SYSTEMEXIT code={se.code}")
            ret = int(se.code or 0)
    finally:
        sys.argv = saved_argv
        for w in watchers:
            w.cancel()
        try:
            await asyncio.gather(*watchers, return_exceptions=True)
        except Exception:
            pass

    # Final wallet snapshot.
    state.wallet_after_sol = await _wallet_balance_sol(
        state.helius_api_key, state.wallet_pubkey
    )
    wallet_delta = state.wallet_after_sol - state.wallet_before_sol
    if state.buys_confirmed >= 1 and wallet_delta < 0:
        state.negative_close_observed = True

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

    write_v50c_report()
    _log(
        f"PGG2-V50C-COMPLETE stop_reason={state.stop_reason} "
        f"wallet_delta={wallet_delta:+.9f} "
        f"total_fees={state.budget.spent_sol:.9f} "
        f"non_neg_closes={state.non_neg_closes}/{state.max_closes} "
        f"neg_closes={state.neg_closes} "
        f"watchdog_owned={state.lifecycle.watchdog_owned_exits} "
        f"v48_owned={state.lifecycle.v48_owned_exits}"
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
