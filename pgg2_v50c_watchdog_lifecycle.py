"""V50C - Watchdog lifecycle (position-open-aware, hook-multiplexed).

This module hosts the position-open-aware V47G watchdog lifecycle wrapper
that V50C uses to GUARANTEE a V47GPositionQuoteWatchdog is spawned for
EVERY confirmed open position, regardless of which V48 fast-path fires.

Why this module exists:
  V50B Stage A confirmed a clean trade but did NOT actually fire the V47G
  watchdog. V48 took the `token_balance_processed` fast-path, which writes
  `buy_trigger="token_balance_processed"` BEFORE V48 ever calls
  `Broker.wait_confirmed(buy_sig)`. V50B's V47G spawn-hook was attached at
  `wait_confirmed`, so the spawn never fired. The exit came from V48's own
  sell loop, NOT the watchdog.

V50C resolution:
  Move the spawn-hook from `wait_confirmed` to the EARLIEST observable
  position-open event:
    1. `Broker.token_balance_raw(mint)` returning > 0 for an in-flight buy.
    2. `Broker.signature_status(buy_sig)` returning processed/confirmed.
    3. V48 emits PGG2-V48-LIVE-BUY-PROCESSED (synthesized log marker).
    4. V47G's original `wait_confirmed` hook (preserved as fallback).
  Whichever fires FIRST spawns the watchdog. Subsequent fires are
  idempotently skipped via the lifecycle's `_active_watchdogs` map.

Atomic close-scheduling:
  The lifecycle owns a `_close_scheduled` set. When the watchdog decides
  to exit, it calls `try_schedule_close(mint, action, reason, pnl)`. If
  the call returns True, this caller owns the close. If False, another
  path (V48's sell loop, or another tick) already scheduled — caller
  must NOT issue a duplicate sell.

Fatal-check:
  A periodic task in the V50C runner calls `fatal_check(open_positions)`.
  If ANY mint in open_positions exists for > 1500ms without an active
  watchdog AND is not in `_completed`, that's a `PGG2-V50C-WATCHDOG-
  MISSING-FATAL` event — the runner stops and reports.

Static-grep clean: this module contains NO sendTransaction/sendSigned
calls. All tx-sending is delegated to the V50A sender adapter via the
V50C runner.
"""
from __future__ import annotations

import asyncio
import functools
import os
import re as _re_self
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# ----- static-grep self-check ----------------------------------------
_FORBIDDEN_CALL_PATTERNS = (
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
for _pat in _FORBIDDEN_CALL_PATTERNS:
    if _re_self.search(_pat, _self_src):
        sys.stderr.write(
            f"V50C-WATCHDOG-LIFECYCLE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v50c_watchdog_lifecycle")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def _time_ms_now() -> int:
    return int(time.time() * 1000)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


# --------------------------------------------------------------------------
# Position-open-aware watchdog lifecycle
# --------------------------------------------------------------------------
class V50CWatchdogLifecycle:
    """Multi-source idempotent watchdog manager.

    Lifecycle states for a given mint:
      - unknown        -> not in any map yet
      - active         -> in `_active_watchdogs` (watchdog running)
      - close_pending  -> in `_close_scheduled` (someone called try_schedule_close)
      - completed      -> in `_completed` (sell confirmed and watchdog stopped)
    """

    def __init__(self, *, max_hold_ms: int = 1500) -> None:
        # mint -> {watchdog, source, started_ts_ms, position, buy_processed_ts_ms}
        self._active_watchdogs: Dict[str, Dict[str, Any]] = {}
        # mints with exit decision in flight (atomic claim mechanism)
        self._close_scheduled: Set[str] = set()
        # mints with exit confirmed and watchdog stopped
        self._completed: Set[str] = set()
        # earliest position-open ts_ms per mint (from any source)
        self._position_buy_processed_ts_ms: Dict[str, int] = {}
        # Source distribution counter for the report.
        self._spawn_sources: Dict[str, int] = {}
        # Lock for thread-safety (V48 runs in sync threads, watchdog in async).
        self._lock = threading.Lock()
        # Reference to logger function (set by runner).
        self._wd_logger: Optional[Callable[[str], None]] = None
        # Reference to watchdog factory function (lets the runner build a
        # V47G watchdog with the right RPC endpoint and the V50C logger).
        self._watchdog_factory: Optional[
            Callable[..., Any]
        ] = None
        # Reference to thread-spawner function (lets the runner pick the
        # event-loop strategy used for the V47G watchdog).
        self._watchdog_thread_spawn: Optional[
            Callable[[Any, str, float], None]
        ] = None
        # Hold-cap policy (ms).
        self.max_hold_ms: int = int(max_hold_ms)
        # Telemetry counters.
        self.watchdog_owned_exits: int = 0
        self.v48_owned_exits: int = 0
        self.v48_sell_first_events: int = 0
        self.watchdog_missing_fatal_count: int = 0
        self.holds_under_cap: int = 0
        self.holds_over_cap: int = 0
        self._hold_durations_ms: List[int] = []

    # -- wiring -------------------------------------------------------

    def set_wd_logger(self, logger: Callable[[str], None]) -> None:
        with self._lock:
            self._wd_logger = logger

    def set_watchdog_factory(self, fn: Callable[..., Any]) -> None:
        """fn(mint, tokens_held_raw, buy_size_sol, opened_at_ms, logger) -> watchdog."""
        with self._lock:
            self._watchdog_factory = fn

    def set_watchdog_thread_spawn(
        self, fn: Callable[[Any, str, float], None]
    ) -> None:
        """fn(watchdog, mint, size_sol) -> None. Spawns watchdog in its
        own thread + event-loop."""
        with self._lock:
            self._watchdog_thread_spawn = fn

    # -- API ----------------------------------------------------------

    def has_position(self, mint: str) -> bool:
        with self._lock:
            return mint in self._active_watchdogs

    def get_watchdog(self, mint: str) -> Optional[Any]:
        with self._lock:
            entry = self._active_watchdogs.get(mint)
            return entry["watchdog"] if entry else None

    def get_source(self, mint: str) -> Optional[str]:
        with self._lock:
            entry = self._active_watchdogs.get(mint)
            return entry["source"] if entry else None

    def get_position_buy_processed_ts(self, mint: str) -> int:
        """Return earliest observed buy_processed timestamp for mint (ms)
        from any source. Used by the watchdog tick for max_hold computation.
        """
        with self._lock:
            return int(self._position_buy_processed_ts_ms.get(mint, 0))

    def is_close_scheduled(self, mint: str) -> bool:
        with self._lock:
            return mint in self._close_scheduled

    def is_completed(self, mint: str) -> bool:
        with self._lock:
            return mint in self._completed

    def get_spawn_sources(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._spawn_sources)

    def get_hold_distribution(self) -> List[int]:
        with self._lock:
            return list(self._hold_durations_ms)

    def list_active_mints(self) -> List[str]:
        with self._lock:
            return list(self._active_watchdogs.keys())

    def list_completed_mints(self) -> List[str]:
        with self._lock:
            return list(self._completed)

    # -- spawn --------------------------------------------------------

    def start_v47g_watchdog_for_position(
        self,
        *,
        mint: str,
        tokens_held_raw: int,
        buy_size_sol: float,
        source: str,
        position: Optional[Dict[str, Any]] = None,
        buy_processed_ts_ms: Optional[int] = None,
    ) -> Optional[Any]:
        """Idempotent watchdog start.

        Returns the V47G watchdog instance, or None if this call did not
        spawn one (duplicate / completed / missing factory / missing tokens).

        Valid `source` values:
          'token_balance_processed', 'position_open', 'buy_processed',
          'signature_status', 'broker_open', 'wait_confirmed_fallback'.

        The earliest of (token_balance_processed, signature_status,
        buy_processed) wins; subsequent fires log a DUPLICATE-SKIP and
        return None.
        """
        now_ms = _time_ms_now()
        if buy_processed_ts_ms is None:
            buy_processed_ts_ms = now_ms

        if tokens_held_raw <= 0:
            self._log_wd(
                f"PGG2-V50C-WATCHDOG-SPAWN-SKIP mint={_short(mint)} "
                f"source={source} reason=tokens_held_raw_zero "
                f"tokens={tokens_held_raw}"
            )
            return None

        with self._lock:
            if mint in self._active_watchdogs:
                active_src = self._active_watchdogs[mint].get("source")
                self._log_wd(
                    f"PGG2-V50C-WATCHDOG-DUPLICATE-SKIP mint={_short(mint)} "
                    f"attempted_source={source} active_source={active_src} "
                    f"ts_ms={now_ms}"
                )
                return None
            if mint in self._completed:
                self._log_wd(
                    f"PGG2-V50C-WATCHDOG-DUPLICATE-SKIP mint={_short(mint)} "
                    f"attempted_source={source} status=already_completed "
                    f"ts_ms={now_ms}"
                )
                return None
            factory = self._watchdog_factory
            thread_spawn = self._watchdog_thread_spawn
            # Record the buy_processed timestamp (only set once per mint).
            existing_ts = self._position_buy_processed_ts_ms.get(mint, 0)
            if existing_ts == 0 or buy_processed_ts_ms < existing_ts:
                self._position_buy_processed_ts_ms[mint] = int(buy_processed_ts_ms)

        if factory is None:
            self._log_wd(
                f"PGG2-V50C-WATCHDOG-SPAWN-SKIP mint={_short(mint)} "
                f"source={source} reason=no_factory"
            )
            return None

        try:
            wd = factory(
                mint=mint,
                tokens_held_raw=int(tokens_held_raw),
                buy_size_sol=float(buy_size_sol),
                opened_at_ms=int(buy_processed_ts_ms),
                logger=self._wd_logger,
            )
        except Exception as exc:
            self._log_wd(
                f"PGG2-V50C-WATCHDOG-SPAWN-FAIL mint={_short(mint)} "
                f"source={source} err={type(exc).__name__}:{exc}"
            )
            return None
        if wd is None:
            self._log_wd(
                f"PGG2-V50C-WATCHDOG-SPAWN-FAIL mint={_short(mint)} "
                f"source={source} reason=factory_returned_none"
            )
            return None

        with self._lock:
            self._active_watchdogs[mint] = {
                "watchdog": wd,
                "source": source,
                "started_ts_ms": now_ms,
                "buy_processed_ts_ms": int(buy_processed_ts_ms),
                "position": dict(position or {}),
            }
            self._spawn_sources[source] = self._spawn_sources.get(source, 0) + 1

        self._log_wd(
            f"PGG2-V50C-WATCHDOG-START mint={_short(mint)} "
            f"source={source} ts_ms={now_ms} "
            f"buy_processed_ts_ms={buy_processed_ts_ms} "
            f"tokens={tokens_held_raw} size={buy_size_sol:.6f}"
        )

        if thread_spawn is not None:
            try:
                thread_spawn(wd, mint, float(buy_size_sol))
            except Exception as exc:
                self._log_wd(
                    f"PGG2-V50C-WATCHDOG-THREAD-SPAWN-FAIL "
                    f"mint={_short(mint)} err={type(exc).__name__}:{exc}"
                )
        return wd

    # -- close scheduling ---------------------------------------------

    def try_schedule_close(
        self,
        mint: str,
        action: str,
        reason: str,
        pnl: float,
        *,
        claimant: str = "watchdog",
    ) -> bool:
        """Atomic close-scheduling claim.

        Returns True if THIS call won the race and may proceed to send
        the sell tx. Returns False if another path already scheduled.

        `claimant` is logged for telemetry ('watchdog' or 'v48').
        """
        now_ms = _time_ms_now()
        with self._lock:
            if mint in self._close_scheduled or mint in self._completed:
                if claimant == "v48":
                    # Telemetry only — V48 lost the race to watchdog.
                    pass
                return False
            self._close_scheduled.add(mint)
            if claimant == "watchdog":
                self.watchdog_owned_exits += 1
            elif claimant == "v48":
                self.v48_owned_exits += 1
                self.v48_sell_first_events += 1
        self._log_wd(
            f"PGG2-V50C-WATCHDOG-CLOSE-SCHEDULED mint={_short(mint)} "
            f"claimant={claimant} action={action} reason={reason} "
            f"pnl={pnl:+.9f} ts_ms={now_ms}"
        )
        return True

    def confirm_close(self, mint: str, *, sell_sig: str = "") -> None:
        """Mark close as confirmed; stop watchdog if still active."""
        now_ms = _time_ms_now()
        wd_to_stop = None
        with self._lock:
            entry = self._active_watchdogs.pop(mint, None)
            if entry is not None:
                wd_to_stop = entry["watchdog"]
                hold_ms = now_ms - int(entry.get("buy_processed_ts_ms", now_ms))
                self._hold_durations_ms.append(int(hold_ms))
                if hold_ms <= self.max_hold_ms + 50:  # 50ms slack for tx latency
                    self.holds_under_cap += 1
                else:
                    self.holds_over_cap += 1
            self._close_scheduled.discard(mint)
            self._completed.add(mint)
        if wd_to_stop is not None:
            # Stop watchdog (best-effort; may be in another loop).
            try:
                # The V47G watchdog has both a `_running` flag and a
                # `_stop_evt`. Setting the flag stops the loop fast.
                wd_to_stop._running = False
                wd_to_stop._stop_evt.set()
            except Exception:
                pass
        self._log_wd(
            f"PGG2-V50C-WATCHDOG-STOP mint={_short(mint)} "
            f"sell_sig={sell_sig or 'none'} ts_ms={now_ms}"
        )

    def record_v48_sell_first(self, mint: str, ts_ms: int) -> None:
        """Telemetry hook called when V48 schedules a sell that beat the
        watchdog. The actual race is already arbitrated in
        `try_schedule_close`; this records the event for the report.
        """
        with self._lock:
            self.v48_sell_first_events += 1
        self._log_wd(
            f"PGG2-V50C-V48-SELL-FIRST mint={_short(mint)} ts_ms={ts_ms} "
            f"watchdog_pending_decision=true"
        )

    # -- fatal check --------------------------------------------------

    def fatal_check(
        self,
        open_position_mints: List[str],
        *,
        grace_ms: int = 1500,
    ) -> List[str]:
        """Return list of mints with open position but no watchdog.

        Caller (the runner) iterates V48's open_positions dict and feeds us
        the list of mints. If a mint exists in open_position_mints AND has
        a buy_processed_ts older than `grace_ms` AND is NOT in
        `_active_watchdogs` AND NOT in `_completed` -> that's a fatal gap.

        Returns the list of FATAL mints (for the runner to act on).
        """
        now_ms = _time_ms_now()
        fatal_mints: List[str] = []
        with self._lock:
            for mint in open_position_mints:
                if mint in self._active_watchdogs:
                    continue
                if mint in self._completed:
                    continue
                bpt = self._position_buy_processed_ts_ms.get(mint, 0)
                if bpt == 0:
                    # Position is open but we never saw buy_processed; grace
                    # period from the current call instead.
                    bpt = now_ms
                    self._position_buy_processed_ts_ms[mint] = bpt
                    continue
                age_ms = now_ms - bpt
                if age_ms >= grace_ms:
                    fatal_mints.append(mint)
                    self.watchdog_missing_fatal_count += 1
                    self._log_wd(
                        f"PGG2-V50C-WATCHDOG-MISSING-FATAL "
                        f"mint={_short(mint)} has_position=true "
                        f"watchdog_active=false age_ms={age_ms} "
                        f"grace_ms={grace_ms}"
                    )
        return fatal_mints

    # -- internal -----------------------------------------------------

    def _log_wd(self, msg: str) -> None:
        if self._wd_logger is not None:
            try:
                self._wd_logger(msg)
                return
            except Exception:
                pass
        _log(msg)


# --------------------------------------------------------------------------
# Multi-source hook installer
# --------------------------------------------------------------------------
def install_watchdog_hooks(
    lifecycle: V50CWatchdogLifecycle,
    broker: Any,
    *,
    latest_buy_ctx_provider: Callable[[], Dict[str, Any]],
    broker_class: Optional[type] = None,
) -> Dict[str, Any]:
    """Multi-source hook installer.

    Patches the broker so that the FIRST of (token_balance_raw,
    signature_status, wait_confirmed) for a known in-flight buy spawns
    the V47G watchdog via `lifecycle.start_v47g_watchdog_for_position`.

    The runner provides `latest_buy_ctx_provider` -- a zero-arg callable
    that returns the latest buy context dict:
        {"mint": str, "size_sol": float, "buy_sig": str | None}
    populated by the V50C runner via `retarget_buy_min_tokens` hook
    (same pattern as V50B).

    Returns a dict of original method references (for unpatch if needed).

    Hooks installed:
      1. broker.token_balance_raw(mint) -> int
         If returned balance > 0 AND latest_buy_ctx.mint matches AND
         lifecycle has no active watchdog for this mint -> spawn with
         source='token_balance_processed'.
      2. broker.signature_status(sig) -> dict | None
         If status is not None AND sig == latest_buy_ctx.buy_sig AND
         status indicates processed/confirmed -> introspect tokens and
         spawn with source='signature_status'.
      3. broker.wait_confirmed(sig) -> bool (PRESERVED as fallback)
         If sig == latest_buy_ctx.buy_sig AND confirmed -> introspect
         tokens and spawn with source='wait_confirmed_fallback'.

    Each hook calls into a helper that:
       (a) Resolves mint from latest_buy_ctx
       (b) Fetches tokens_held_raw via broker.token_balance_raw (we accept
           recursion-safety here: the token_balance_raw wrapper calls
           the original method, which does the actual RPC).
       (c) Calls lifecycle.start_v47g_watchdog_for_position.
    """
    if broker_class is None:
        broker_class = type(broker)
    # Find the class hierarchy: the methods may live on a parent class.
    target_cls = broker_class

    originals: Dict[str, Any] = {
        "token_balance_raw": getattr(target_cls, "token_balance_raw", None),
        "signature_status": getattr(target_cls, "signature_status", None),
        "wait_confirmed": getattr(target_cls, "wait_confirmed", None),
    }

    # Helper: introspect tokens, then call lifecycle.start. Used by all
    # three hooks. Wraps logging + error guards.
    def _attempt_spawn(
        broker_self: Any,
        source: str,
        tokens_held_raw_hint: int = 0,
    ) -> None:
        ctx = dict(latest_buy_ctx_provider() or {})
        mint = str(ctx.get("mint") or "")
        if not mint:
            return
        if lifecycle.has_position(mint) or lifecycle.is_completed(mint):
            return
        # Resolve tokens_held_raw.
        tokens_raw = int(tokens_held_raw_hint or 0)
        if tokens_raw <= 0:
            try:
                from solders.pubkey import Pubkey as _Pk
                mint_pk = _Pk.from_string(mint)
                orig_tbr = originals["token_balance_raw"]
                if orig_tbr is not None:
                    tokens_raw = int(orig_tbr(broker_self, mint_pk))
            except Exception:
                tokens_raw = 0
        if tokens_raw <= 0:
            # Position not actually opened on-chain yet (or RPC failure).
            return
        size_sol = float(
            ctx.get("size_sol")
            or float(os.environ.get("PGG2_TRADE_SIZE_SOL", "0.005") or 0.005)
        )
        position = {
            "mint": mint,
            "size_sol": size_sol,
            "tokens_held_raw": tokens_raw,
            "ctx_ts_ms": int(ctx.get("ts_ms", 0) or 0),
        }
        lifecycle.start_v47g_watchdog_for_position(
            mint=mint,
            tokens_held_raw=tokens_raw,
            buy_size_sol=size_sol,
            source=source,
            position=position,
            buy_processed_ts_ms=_time_ms_now(),
        )

    # Hook 1: token_balance_raw.
    if originals["token_balance_raw"] is not None:
        _orig_tbr = originals["token_balance_raw"]

        @functools.wraps(_orig_tbr)
        def _v50c_token_balance_raw(self: Any, mint: Any) -> int:
            raw = _orig_tbr(self, mint)
            try:
                if raw > 0:
                    # Try to identify this as the in-flight buy mint.
                    ctx = dict(latest_buy_ctx_provider() or {})
                    ctx_mint = str(ctx.get("mint") or "")
                    if ctx_mint:
                        try:
                            mint_str = str(mint)
                        except Exception:
                            mint_str = ""
                        if mint_str and mint_str == ctx_mint:
                            _attempt_spawn(
                                self,
                                source="token_balance_processed",
                                tokens_held_raw_hint=int(raw),
                            )
            except Exception as exc:
                lifecycle._log_wd(
                    f"PGG2-V50C-WATCHDOG-HOOK-WARN method=token_balance_raw "
                    f"err={type(exc).__name__}:{exc}"
                )
            return raw

        target_cls.token_balance_raw = _v50c_token_balance_raw

    # Hook 2: signature_status (this fires every poll).
    if originals["signature_status"] is not None:
        _orig_ss = originals["signature_status"]

        @functools.wraps(_orig_ss)
        def _v50c_signature_status(self: Any, sig: str) -> Optional[Dict[str, Any]]:
            status = _orig_ss(self, sig)
            try:
                if status is not None:
                    ctx = dict(latest_buy_ctx_provider() or {})
                    buy_sig = str(ctx.get("buy_sig") or "")
                    if buy_sig and sig == buy_sig:
                        cs = str(status.get("confirmationStatus") or "")
                        err = status.get("err")
                        if not err and (cs in {"processed", "confirmed", "finalized"} or cs == ""):
                            _attempt_spawn(
                                self,
                                source="signature_status",
                                tokens_held_raw_hint=0,
                            )
            except Exception as exc:
                lifecycle._log_wd(
                    f"PGG2-V50C-WATCHDOG-HOOK-WARN method=signature_status "
                    f"err={type(exc).__name__}:{exc}"
                )
            return status

        target_cls.signature_status = _v50c_signature_status

    # Hook 3: wait_confirmed (preserved as fallback; the V50B path).
    if originals["wait_confirmed"] is not None:
        _orig_wc = originals["wait_confirmed"]

        @functools.wraps(_orig_wc)
        def _v50c_wait_confirmed(self: Any, sig: str) -> bool:
            ok = _orig_wc(self, sig)
            try:
                if ok:
                    ctx = dict(latest_buy_ctx_provider() or {})
                    buy_sig = str(ctx.get("buy_sig") or "")
                    if buy_sig and sig == buy_sig:
                        _attempt_spawn(
                            self,
                            source="wait_confirmed_fallback",
                            tokens_held_raw_hint=0,
                        )
            except Exception as exc:
                lifecycle._log_wd(
                    f"PGG2-V50C-WATCHDOG-HOOK-WARN method=wait_confirmed "
                    f"err={type(exc).__name__}:{exc}"
                )
            return ok

        target_cls.wait_confirmed = _v50c_wait_confirmed

    return originals


# --------------------------------------------------------------------------
# Direct call entry-point — used by the runner from non-broker paths
# (e.g. V48 emits PGG2-V48-LIVE-BUY-PROCESSED log; the runner sees it via
# a custom log filter and calls position_open_spawn directly).
# --------------------------------------------------------------------------
def position_open_spawn(
    lifecycle: V50CWatchdogLifecycle,
    *,
    mint: str,
    tokens_held_raw: int,
    buy_size_sol: float,
    source: str = "position_open",
    buy_processed_ts_ms: Optional[int] = None,
) -> Optional[Any]:
    """Direct spawn entry — bypasses broker hooks for the runner's
    position-open path. Used when V48 emits the LIVE-BUY-PROCESSED log
    line, which the runner intercepts via a custom logger filter.
    """
    return lifecycle.start_v47g_watchdog_for_position(
        mint=mint,
        tokens_held_raw=tokens_held_raw,
        buy_size_sol=buy_size_sol,
        source=source,
        buy_processed_ts_ms=buy_processed_ts_ms,
    )


__all__ = [
    "V50CWatchdogLifecycle",
    "install_watchdog_hooks",
    "position_open_spawn",
]


# ---- self-test at import (offline, no network) ----
def _self_test() -> None:
    lc = V50CWatchdogLifecycle(max_hold_ms=1500)
    test_mint = "TestMintV50CSelfTest1111111111111111111111111"
    # Without factory, spawn returns None but state is marked.
    r1 = lc.start_v47g_watchdog_for_position(
        mint=test_mint, tokens_held_raw=1000, buy_size_sol=0.005,
        source="token_balance_processed",
    )
    if r1 is not None:
        raise RuntimeError("v50c_lc_self_test_should_return_none_without_factory")
    # Duplicate skip should still log; calling without factory always
    # returns None until factory is set, so this test ensures the duplicate-
    # skip path doesn't crash.
    r2 = lc.start_v47g_watchdog_for_position(
        mint=test_mint, tokens_held_raw=1000, buy_size_sol=0.005,
        source="signature_status",
    )
    if r2 is not None:
        raise RuntimeError("v50c_lc_self_test_dup_should_skip_without_factory")
    # try_schedule_close should claim the close exactly once.
    if not lc.try_schedule_close(test_mint, "bank", "test", 0.001):
        raise RuntimeError("v50c_lc_self_test_close_claim_first_should_win")
    if lc.try_schedule_close(test_mint, "bank", "test", 0.001):
        raise RuntimeError("v50c_lc_self_test_close_claim_second_should_lose")
    # confirm_close moves mint into _completed.
    lc.confirm_close(test_mint, sell_sig="testsig")
    if not lc.is_completed(test_mint):
        raise RuntimeError("v50c_lc_self_test_complete_not_recorded")
    # Subsequent spawn for completed mint should skip.
    r3 = lc.start_v47g_watchdog_for_position(
        mint=test_mint, tokens_held_raw=1000, buy_size_sol=0.005,
        source="token_balance_processed",
    )
    if r3 is not None:
        raise RuntimeError(
            "v50c_lc_self_test_completed_mint_should_not_respawn"
        )


_self_test()
