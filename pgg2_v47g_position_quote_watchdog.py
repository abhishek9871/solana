"""V47G - Position Quote Watchdog.

An independent open-position monitor that quotes the bonding-curve sell-out
every PGG2_V47G_WATCHDOG_INTERVAL_MS milliseconds by polling the bonding-
curve PDA via JSON-RPC `getAccountInfo`. This bypasses the accountSubscribe
push channel entirely, so feed silence on the WS subscriber does not delay
exit decisions.

Design (per spec):
  - Each open position spawns one V47GPositionQuoteWatchdog instance.
  - The watchdog polls bonding_curve PDA via getAccountInfo every 250ms.
  - Curve bytes decoded locally (mirroring _decode_curve_data in
    pgg2_v42_curve_account_subscriber.py).
  - Sell-out computed via local CPMM math (local_sell_quote_sol).
  - Exit decision delegated to pgg2_v47g_watchdog_exit_policy.decide_exit.
  - First non-hold decision sets self.exit_action / self.exit_at_ts and
    stops the loop. Caller polls get_exit_decision() (or awaits the task).

Hard rules:
  - NO sendTransaction-class calls (static-grep enforced).
  - NO Jito / paid feeds.
  - PURE READ via JSON-RPC. No state writes.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re as _re
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple


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
    _src = _self.read()
for _pat in _FORBIDDEN_CALL_PATTERNS:
    if _re.search(_pat, _src):
        sys.stderr.write(
            f"V47G-WATCHDOG-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47g_position_quote_watchdog")


LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_TX_FEE_SOL = 0.0000287

# Pump bonding-curve PDA seed prefix (matches pgg2_direct_pump.py).
PUMP_PROGRAM_ID_STR = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_BONDING_CURVE_SEED = b"bonding-curve"


# ---- env knobs ------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except Exception:
        return default


DEFAULT_INTERVAL_MS = _env_int("PGG2_V47G_WATCHDOG_INTERVAL_MS", 250)
DEFAULT_MAX_QUOTE_AGE_MS = _env_int("PGG2_V47G_MAX_QUOTE_AGE_MS", 500)
DEFAULT_SNAP_SILENCE_MS = _env_int("PGG2_V47G_SNAP_SILENCE_MS", 500)
DEFAULT_REQUEST_TIMEOUT_S = _env_float("PGG2_V47G_RPC_TIMEOUT_S", 1.5)


def _short(m: str) -> str:
    if not m or len(m) <= 10:
        return m or "?"
    return m[:4] + ".." + m[-4:]


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---- bonding-curve PDA derivation -----------------------------------

def _derive_bonding_curve_pda(mint_str: str) -> str:
    """Derive bonding-curve PDA for the given mint pubkey (base58 str).

    Uses solders Pubkey.find_program_address if available; falls back to
    pgg2_direct_pump.pda() helper if exposed; else raises."""
    try:
        from solders.pubkey import Pubkey as _Pk  # type: ignore
        mint_pk = _Pk.from_string(mint_str)
        prog_pk = _Pk.from_string(PUMP_PROGRAM_ID_STR)
        # find_program_address returns (Pubkey, bump)
        pda_pk, _bump = _Pk.find_program_address(
            [PUMP_BONDING_CURVE_SEED, bytes(mint_pk)], prog_pk
        )
        return str(pda_pk)
    except Exception:
        try:
            from pgg2_direct_pump import pda, PUMP_PROGRAM_ID  # type: ignore
            from solders.pubkey import Pubkey as _Pk  # type: ignore
            mint_pk = _Pk.from_string(mint_str)
            curve_pk = pda(PUMP_PROGRAM_ID, PUMP_BONDING_CURVE_SEED, bytes(mint_pk))
            return str(curve_pk)
        except Exception as exc:
            raise RuntimeError(
                f"v47g_bonding_curve_pda_derivation_failed_{type(exc).__name__}_{exc}"
            )


# ---- curve byte decode (mirror of pgg2_v42_curve_account_subscriber) ----

def _decode_curve_account_bytes(raw: bytes) -> Tuple[int, int, int, int, int, bool]:
    """Return (vtok, vsol, real_tok, real_sol, total_supply, complete).

    Layout (anchor): 8-byte disc, then u64 vtok, u64 vsol, u64 real_tok,
    u64 real_sol, u64 total_supply, u8 complete. Same byte offsets as
    `_decode_curve_data` in pgg2_v42_curve_account_subscriber.py.
    """
    if len(raw) < 49:
        raise ValueError(f"curve_account_short_len={len(raw)}")
    vtok = int.from_bytes(raw[8:16], "little")
    vsol = int.from_bytes(raw[16:24], "little")
    rtok = int.from_bytes(raw[24:32], "little")
    rsol = int.from_bytes(raw[32:40], "little")
    tot = int.from_bytes(raw[40:48], "little")
    complete = bool(raw[48])
    return vtok, vsol, rtok, rsol, tot, complete


# ---- RPC client (sync getAccountInfo over HTTP) ---------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _build_rpc_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Match RaptorLiveBroker.headers() pattern: accept, user-agent, x-api-key,
    authorization (Bearer). The api_key is taken from env if not in extra."""
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": os.environ.get("PGG2_HTTP_USER_AGENT", DEFAULT_USER_AGENT),
    }
    api_key = (extra or {}).get("api_key") or os.environ.get(
        "SOLANATRACKER_API_KEY", ""
    )
    if api_key:
        headers["x-api-key"] = str(api_key)
        headers["authorization"] = f"Bearer {api_key}"
    if extra:
        for k, v in extra.items():
            if k != "api_key":
                headers[k] = v
    return headers


def _extract_api_key_from_endpoint(endpoint: str) -> str:
    """SolanaTracker endpoint typically has ?api_key=<uuid>; extract it."""
    if not endpoint or "api_key=" not in endpoint:
        return ""
    try:
        tail = endpoint.split("api_key=", 1)[1]
        return tail.split("&", 1)[0].strip()
    except Exception:
        return ""


def _rpc_get_account_info_sync(
    rpc_http_endpoint: str,
    account_pubkey: str,
    timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
) -> Optional[bytes]:
    """Blocking JSON-RPC getAccountInfo. Returns raw account-data bytes,
    or None on any error (network, parse, missing). Uses requests if
    installed, else urllib.

    NOTE: This is intentionally synchronous; the watchdog calls it inside
    a thread executor to keep the asyncio loop unblocked.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            account_pubkey,
            {"encoding": "base64", "commitment": "processed"},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    api_key = _extract_api_key_from_endpoint(rpc_http_endpoint)
    headers = _build_rpc_headers({"api_key": api_key} if api_key else None)
    try:
        try:
            import requests  # type: ignore
            r = requests.post(
                rpc_http_endpoint, data=body, headers=headers,
                timeout=float(timeout_s),
            )
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception:
            import urllib.request as _ur
            req = _ur.Request(
                rpc_http_endpoint, data=body, headers=headers, method="POST"
            )
            with _ur.urlopen(req, timeout=float(timeout_s)) as resp:
                txt = resp.read().decode("utf-8", errors="replace")
                data = json.loads(txt)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    data_field = value.get("data")
    if not (isinstance(data_field, list) and len(data_field) >= 2):
        return None
    encoded, enc_type = data_field[0], data_field[1]
    if enc_type != "base64":
        return None
    try:
        return base64.b64decode(encoded)
    except Exception:
        return None


# ---- watchdog -------------------------------------------------------

class V47GPositionQuoteWatchdog:
    """Independent open-position watchdog.

    Polls bonding-curve PDA via JSON-RPC every interval_ms; runs local
    CPMM math; consults pgg2_v47g_watchdog_exit_policy.decide_exit; first
    non-hold decision is final and stops the loop.

    Public API:
      __init__(mint, tokens_held_raw, buy_size_sol, ...)
      async start() -> spawns background task
      async stop()  -> cancels task and cleans up
      get_exit_decision() -> dict or None (None while still polling)
      get_stats() -> dict telemetry
    """

    def __init__(
        self,
        mint: str,
        tokens_held_raw: int,
        buy_size_sol: float,
        rpc_http_endpoint: str,
        fee_bps: int,
        creator_fee_bps: int,
        opened_at_ms: int,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        snap_silence_ms: int = DEFAULT_SNAP_SILENCE_MS,
        max_quote_age_ms: int = DEFAULT_MAX_QUOTE_AGE_MS,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        tx_fee_sol: float = DEFAULT_TX_FEE_SOL,
        logger: Optional[Callable[[str], None]] = None,
        last_subscriber_update_provider: Optional[Callable[[], int]] = None,
        pending_flow_provider: Optional[
            Callable[[int], Tuple[float, float]]
        ] = None,
    ) -> None:
        self.mint = str(mint)
        self.tokens_held_raw = int(tokens_held_raw)
        self.buy_size_sol = float(buy_size_sol)
        self.rpc_http_endpoint = str(rpc_http_endpoint or "")
        self.fee_bps = int(fee_bps)
        self.creator_fee_bps = int(creator_fee_bps)
        self.opened_at_ms = int(opened_at_ms)
        self.interval_ms = int(max(50, interval_ms))
        self.snap_silence_ms = int(snap_silence_ms)
        self.max_quote_age_ms = int(max_quote_age_ms)
        self.request_timeout_s = float(request_timeout_s)
        self.tx_fee_sol = float(tx_fee_sol)
        self.expected_fees = 2.0 * float(tx_fee_sol)
        self._logger = logger
        self._last_subscriber_update_provider = last_subscriber_update_provider
        self._pending_flow_provider = pending_flow_provider

        try:
            self.curve_pda = _derive_bonding_curve_pda(self.mint)
        except Exception as exc:
            self._log(
                f"PGG2-V47G-WATCHDOG-INIT-ERROR mint={_short(self.mint)} "
                f"err={type(exc).__name__}:{exc}"
            )
            self.curve_pda = ""

        # State.
        self._running = False
        self._stopped_at_ms = 0
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self.exit_action: Optional[str] = None
        self.exit_reason: Optional[str] = None
        self.exit_pnl: Optional[float] = None
        self.exit_at_ts: Optional[int] = None
        self.exit_age_ms: Optional[int] = None
        self.exit_quote_sell_sol: Optional[float] = None

        # Trackers (per-position rolling state).
        self._peak_quote = 0.0
        self._peak_pnl = -1e18
        self._peak_vsol = 0.0
        self._last_quote = 0.0
        self._neg_grad_count = 0
        self._had_been_above = False
        self._last_poll_ts_ms = 0
        self._last_successful_poll_ts_ms = 0

        # Telemetry.
        self.polls_attempted = 0
        self.polls_succeeded = 0
        self.polls_rpc_error = 0
        self.polls_decode_error = 0
        self.snap_silence_events = 0
        self.decisions_made = 0

    # ---- public API -------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if not self.rpc_http_endpoint:
            self._log(
                f"PGG2-V47G-WATCHDOG-START-FAIL mint={_short(self.mint)} "
                "reason=no_rpc_http_endpoint"
            )
            return
        if not self.curve_pda:
            self._log(
                f"PGG2-V47G-WATCHDOG-START-FAIL mint={_short(self.mint)} "
                "reason=no_curve_pda"
            )
            return
        self._running = True
        self._stop_evt.clear()
        self._task = asyncio.get_event_loop().create_task(self._poll_loop())
        self._log(
            f"PGG2-V47G-WATCHDOG-START mint={_short(self.mint)} "
            f"pos_tokens={self.tokens_held_raw} "
            f"size={self.buy_size_sol:.4f} "
            f"interval={self.interval_ms}ms "
            f"curve_pda={_short(self.curve_pda)}"
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_evt.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except Exception:
                try:
                    self._task.cancel()
                except Exception:
                    pass
        self._stopped_at_ms = _now_ms()
        dur = max(0, self._stopped_at_ms - self.opened_at_ms)
        self._log(
            f"PGG2-V47G-WATCHDOG-STOP mint={_short(self.mint)} "
            f"duration_ms={dur} polls={self.polls_succeeded}/"
            f"{self.polls_attempted} snap_silence={self.snap_silence_events} "
            f"exit_action={self.exit_action or 'none'}"
        )

    def get_exit_decision(self) -> Optional[Dict[str, Any]]:
        """Return decision dict if an exit has been chosen, else None."""
        if self.exit_action is None:
            return None
        return {
            "action": self.exit_action,
            "reason": self.exit_reason,
            "pnl": self.exit_pnl,
            "ts": self.exit_at_ts,
            "age_ms": self.exit_age_ms,
            "quote_sell_sol": self.exit_quote_sell_sol,
        }

    def get_stats(self) -> Dict[str, Any]:
        return {
            "mint": self.mint,
            "polls_attempted": self.polls_attempted,
            "polls_succeeded": self.polls_succeeded,
            "polls_rpc_error": self.polls_rpc_error,
            "polls_decode_error": self.polls_decode_error,
            "snap_silence_events": self.snap_silence_events,
            "decisions_made": self.decisions_made,
            "peak_quote": self._peak_quote,
            "peak_pnl": self._peak_pnl,
            "exit_action": self.exit_action,
            "exit_pnl": self.exit_pnl,
            "exit_at_ts": self.exit_at_ts,
        }

    # ---- internals --------------------------------------------------

    def _log(self, msg: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger(msg)
        except Exception:
            pass

    async def _poll_loop(self) -> None:
        """Poll bonding-curve PDA every interval_ms, evaluate exit policy.

        Strict guarantees:
          - We sleep BEFORE the first poll so that age_ms > 0 on first
            decision (avoids spurious immediate-bank on tokens_held_raw
            that already reflect post-buy state at curve_state_buy).
            Actually we DO poll first, but only emit a decision once we
            have at least one quote sample. Bank/scratch checks operate
            on PnL which is from quote-vs-cost so first poll IS valid.
          - Loop exits on stop OR first non-hold decision.
        """
        from pgg2_v47g_watchdog_exit_policy import (  # type: ignore
            decide_exit, ACTION_HOLD,
        )
        try:
            from pgg2_v42h_local_curve_quote import (  # type: ignore
                curve_state_from_subscriber_point,
                local_sell_quote_sol,
            )
        except Exception as exc:
            self._log(
                f"PGG2-V47G-WATCHDOG-IMPORT-ERROR mint={_short(self.mint)} "
                f"err={type(exc).__name__}:{exc}"
            )
            self._running = False
            return

        loop = asyncio.get_event_loop()
        while self._running and not self._stop_evt.is_set():
            # Sleep first to space polls; on cancel, exit fast.
            try:
                await asyncio.wait_for(
                    self._stop_evt.wait(), timeout=self.interval_ms / 1000.0
                )
                # If we get here, stop was signalled.
                break
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            self.polls_attempted += 1
            poll_ts = _now_ms()
            self._last_poll_ts_ms = poll_ts
            # snap_silence detection: if accountSubscribe is silent for >
            # snap_silence_ms, log it. This is independent of polling -
            # we poll regardless.
            if self._last_subscriber_update_provider is not None:
                try:
                    last_sub_ts = int(self._last_subscriber_update_provider())
                    if last_sub_ts > 0:
                        silence_age = poll_ts - last_sub_ts
                        if silence_age >= self.snap_silence_ms:
                            self.snap_silence_events += 1
                            if self.snap_silence_events <= 4 or (
                                self.snap_silence_events % 8 == 0
                            ):
                                self._log(
                                    "PGG2-V47G-SNAP-SILENCE-DETECTED "
                                    f"mint={_short(self.mint)} "
                                    f"last_snap_age_ms={silence_age} "
                                    "action=fallback_to_watchdog"
                                )
                except Exception:
                    pass
            # Blocking RPC call inside thread pool.
            try:
                raw = await loop.run_in_executor(
                    None,
                    _rpc_get_account_info_sync,
                    self.rpc_http_endpoint,
                    self.curve_pda,
                    self.request_timeout_s,
                )
            except Exception:
                raw = None
            if raw is None:
                self.polls_rpc_error += 1
                continue
            try:
                vtok, vsol, rtok, _rsol, _tot, _complete = (
                    _decode_curve_account_bytes(raw)
                )
            except Exception:
                self.polls_decode_error += 1
                continue
            self.polls_succeeded += 1
            self._last_successful_poll_ts_ms = poll_ts
            try:
                cs = curve_state_from_subscriber_point(
                    int(vsol), int(vtok), int(rtok),
                    int(self.fee_bps), int(self.creator_fee_bps),
                )
                if int(self.tokens_held_raw) <= 0:
                    sell_lams = 0
                else:
                    sell_lams, _ = local_sell_quote_sol(
                        cs, int(self.tokens_held_raw)
                    )
            except Exception as exc:
                self.polls_decode_error += 1
                self._log(
                    f"PGG2-V47G-WATCHDOG-QUOTE-ERROR mint={_short(self.mint)} "
                    f"err={type(exc).__name__}:{exc}"
                )
                continue
            sell_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)
            pnl = sell_sol - self.buy_size_sol - self.expected_fees
            cur_vsol = float(vsol) / float(LAMPORTS_PER_SOL)
            # Update peaks.
            new_peak_q = max(self._peak_quote, sell_sol)
            new_peak_p = max(self._peak_pnl, pnl)
            new_peak_v = max(self._peak_vsol, cur_vsol)
            grad = sell_sol - self._last_quote if self._last_quote > 0 else 0.0
            new_neg_g = self._neg_grad_count + 1 if grad < 0.0 else 0
            had_above = self._had_been_above or (pnl >= 0.00005)
            self._peak_quote = new_peak_q
            self._peak_pnl = new_peak_p
            self._peak_vsol = new_peak_v
            self._last_quote = sell_sol
            self._neg_grad_count = new_neg_g
            self._had_been_above = had_above

            # Pending flow snapshot (optional provider).
            pbs = 0.0
            pss = 0.0
            if self._pending_flow_provider is not None:
                try:
                    pbs, pss = self._pending_flow_provider(poll_ts)
                except Exception:
                    pbs = 0.0
                    pss = 0.0

            age_ms = max(0, poll_ts - self.opened_at_ms)
            break_even_q = self.buy_size_sol + self.expected_fees
            pos_state = {
                "mint": self.mint,
                "size_sol": self.buy_size_sol,
                "current_all_in_pnl": float(pnl),
                "peak_all_in_pnl": float(new_peak_p),
                "current_local_sell_quote": float(sell_sol),
                "peak_local_sell_quote": float(new_peak_q),
                "current_curve_vsol": float(cur_vsol),
                "peak_curve_vsol": float(new_peak_v),
                "latest_pending_buy_sol_250ms": float(pbs),
                "latest_pending_sell_sol_250ms": float(pss),
                "latest_quote_gradient": float(grad),
                "consecutive_negative_gradient_count": int(new_neg_g),
                "had_been_above_bank_or_scratch": bool(had_above),
                "break_even_sell_quote": float(break_even_q),
                "position_age_ms": int(age_ms),
            }
            # Sample-emit quote log (every 4 polls).
            if self.polls_succeeded <= 4 or self.polls_succeeded % 4 == 0:
                self._log(
                    f"PGG2-V47G-WATCHDOG-QUOTE mint={_short(self.mint)} "
                    f"ts={poll_ts} sell_sol={sell_sol:.9f} "
                    f"pnl={pnl:+.9f} peak_pnl={new_peak_p:+.9f} "
                    f"age_ms={age_ms} neg_grad={new_neg_g}"
                )
            action, reason = decide_exit(pos_state, logger=self._log)
            self.decisions_made += 1
            if action != ACTION_HOLD:
                # Emit explicit per-action log lines (one of the spec'd set).
                if action == "bank":
                    self._log(
                        f"PGG2-V47G-WATCHDOG-BANK mint={_short(self.mint)} "
                        f"pnl={pnl:+.6f} age_ms={age_ms}"
                    )
                elif action.startswith("scratch") or action == "abort_scratch" \
                        or action == "max_hold_scratch":
                    self._log(
                        f"PGG2-V47G-WATCHDOG-SCRATCH mint={_short(self.mint)} "
                        f"pnl={pnl:+.6f} age_ms={age_ms} action={action}"
                    )
                elif action.startswith("abort") or action.startswith("max_hold"):
                    self._log(
                        f"PGG2-V47G-WATCHDOG-ABORT mint={_short(self.mint)} "
                        f"reason={reason} pnl={pnl:+.6f} age_ms={age_ms} "
                        f"action={action}"
                    )
                self.exit_action = str(action)
                self.exit_reason = str(reason) if reason else None
                self.exit_pnl = float(pnl)
                self.exit_at_ts = int(poll_ts)
                self.exit_age_ms = int(age_ms)
                self.exit_quote_sell_sol = float(sell_sol)
                self._running = False
                break

        # Loop exited.
        if not self._running and self.exit_action is None:
            # Stopped without a decision (caller stop()'d us).
            pass


__all__ = [
    "V47GPositionQuoteWatchdog",
    "DEFAULT_INTERVAL_MS",
    "DEFAULT_SNAP_SILENCE_MS",
    "DEFAULT_MAX_QUOTE_AGE_MS",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "LAMPORTS_PER_SOL",
    "PUMP_PROGRAM_ID_STR",
    "_derive_bonding_curve_pda",
    "_decode_curve_account_bytes",
    "_rpc_get_account_info_sync",
]


# ---- self-test at import (offline, no network) ----
def _self_test() -> None:
    # Decode test: build a synthetic 100-byte curve account.
    raw = bytearray(100)
    # discriminator [0:8] arbitrary
    raw[0:8] = bytes(8)
    # vtok @ 8:16
    raw[8:16] = int(700_000_000_000_000).to_bytes(8, "little")
    # vsol @ 16:24
    raw[16:24] = int(30_000_000_000).to_bytes(8, "little")
    # rtok @ 24:32
    raw[24:32] = int(500_000_000_000_000).to_bytes(8, "little")
    raw[32:40] = int(0).to_bytes(8, "little")
    raw[40:48] = int(1_000_000_000_000_000).to_bytes(8, "little")
    raw[48] = 0
    vtok, vsol, rtok, _rsol, _tot, complete = _decode_curve_account_bytes(bytes(raw))
    assert vtok == 700_000_000_000_000, vtok
    assert vsol == 30_000_000_000, vsol
    assert rtok == 500_000_000_000_000, rtok
    assert complete is False
    # PDA derivation test (requires solders): try, accept failure quietly.
    try:
        from solders.pubkey import Pubkey as _Pk  # type: ignore
        # Random valid pubkey for test.
        test_mint = "2FE4G98yTbzGnYNLm8wS9gFWwH2EuJ15gC9vGNFxwBSS"
        pda_str = _derive_bonding_curve_pda(test_mint)
        if not pda_str or len(pda_str) < 32:
            raise RuntimeError(f"v47g_watchdog_pda_self_test_bad_len: {pda_str}")
    except RuntimeError:
        raise
    except Exception:
        # solders not available in self-test env; skip.
        pass


_self_test()
