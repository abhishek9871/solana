"""V62B Authoritative Sell Router.

OWNS the entire sell loop for pump_bc live positions. Replaces the V48 sell
sub-loop entirely when active. No Jupiter fallback for pump_bc. No V48
emergency sell. No legacy retarget_sell_min_sol with too-tight min_sol that
fails on-chain validation.

Built in response to Fwye sell-router-ownership-failure (2026-05-19):
  - V60 + V61 said BUY (legitimate trade, +22% expected exit edge)
  - V48 bank sell sent but never polled (broker.signature_status missing)
  - V48 emergency sell rejected on-chain with Custom 6023 (min_sol=0.003 too tight)
  - Jupiter fallback engaged on stale balance → trade closed at -$0.22

V62B fixes all three via single authoritative sell loop.

Public API (blocking call):
    result = v62b_close_position(
        broker, mint, raw_balance, cost_basis_sol, expected_sol_out_now,
        reason, swqos_sender, rpc_url, log_fn=...
    )
    if result.confirmed:
        sig = result.confirmed_sig
        actual_sol_out = result.actual_sol_out
    else:
        # router_failed_final
        ...

Returns a V62BResult with:
    confirmed: bool
    confirmed_sig: Optional[str]
    actual_sol_out: float (best-effort from wallet delta or quote)
    attempts: int (number of send attempts)
    elapsed_ms: int
    final_min_sol_used: float
    blocker: Optional[str] (reason for final-fail)

Logs:
    PGG2-V62B-SELL-ROUTER-START mint=.. reason=.. raw_balance=.. cost=..
    PGG2-V62B-RAW-BALANCE mint=.. raw=.. (per attempt)
    PGG2-V62B-SELL-BUILD mint=.. attempt=.. expected=.. min_sol=.. min_sol_policy=..
    PGG2-V62B-SELL-GUARD mint=.. attempt=.. min_sol=.. encoded=.. match=..
    PGG2-V62B-SELL-SEND mint=.. attempt=.. sig=..
    PGG2-V62B-SELL-STATUS mint=.. attempt=.. sig=.. status=processed|confirmed|finalized|None
    PGG2-V62B-SELL-RETRY mint=.. attempt=.. reason=.. elapsed_ms=..
    PGG2-V62B-SELL-CONFIRMED mint=.. sig=.. actual_sol_out=.. attempts=.. elapsed_ms=..
    PGG2-V62B-SELL-ROUTER-FAIL mint=.. blocker=.. attempts=.. elapsed_ms=..
    PGG2-V62B-BANK-MIN-SOL-POLICY mint=.. bank_floor=.. expected_x_085=.. chosen=..
    PGG2-V62B-EMERGENCY-MIN-SOL-POLICY mint=.. min_sol=..
    PGG2-V62B-JUPITER-FALLBACK-BLOCKED mint=.. reason=router_owns_pump_bc

Hard rules:
    - NO Jupiter fallback inside V62B for pump_bc. Caller must NOT call
      jupiter_rescue_one after V62B returns.
    - NO use of broker.signature_status (it doesn't exist on
      DirectPumpQuoteBroker). V62B uses direct JSON-RPC getSignatureStatuses.
    - NO min_sol=0 for bank/scratch. Use cost-protected floor.
    - emergency uses min_sol=0.000020 (rescue-equivalent).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib import request as _urlreq


# ============================================================================
#  Config helpers
# ============================================================================

def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _envi(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _short(mint: str) -> str:
    if len(mint) > 10:
        return mint[:4] + ".." + mint[-4:]
    return mint


# ============================================================================
#  Result + reason types
# ============================================================================

# Exit reasons
REASON_BANK = "bank"
REASON_SCRATCH = "scratch"
REASON_MAX_HOLD = "max_hold"
REASON_DUMP_ABORT = "dump_abort"
REASON_EMERGENCY = "emergency"


@dataclass
class V62BResult:
    confirmed: bool
    confirmed_sig: Optional[str]
    actual_sol_out: float
    attempts: int
    elapsed_ms: int
    final_min_sol_used: float
    blocker: Optional[str]
    used_emergency: bool = False


@dataclass
class V62BConfig:
    # Bank/scratch min-sol policy
    bank_expected_fraction: float = 0.85          # min_sol >= expected * this
    bank_small_profit_floor_sol: float = 0.000200  # min_sol >= cost + fees + this
    fees_estimate_sol: float = 0.000060           # 2 legs of base+priority+tip
    scratch_tolerance_sol: float = 0.000050       # tiny tolerance below cost for scratch
    # Emergency min-sol policy
    emergency_min_sol: float = 0.000020           # rescue-equivalent
    # Resend ladder
    max_attempts: int = 3
    poll_interval_ms: int = 100
    per_attempt_timeout_ms: int = 300
    total_budget_ms: int = 1500
    # Total wait after final attempt for any pending sig to confirm
    final_wait_ms: int = 700
    # JSON-RPC config
    rpc_request_timeout_sec: float = 5.0


def _config() -> V62BConfig:
    cfg = V62BConfig()
    cfg.bank_expected_fraction = _envf("PGG2_V62B_BANK_EXPECTED_FRACTION", 0.85)
    cfg.bank_small_profit_floor_sol = _envf("PGG2_V62B_BANK_SMALL_PROFIT_FLOOR_SOL", 0.000200)
    cfg.fees_estimate_sol = _envf("PGG2_V62B_FEES_ESTIMATE_SOL", 0.000060)
    cfg.scratch_tolerance_sol = _envf("PGG2_V62B_SCRATCH_TOLERANCE_SOL", 0.000050)
    cfg.emergency_min_sol = _envf("PGG2_V62B_EMERGENCY_MIN_SOL", 0.000020)
    cfg.max_attempts = _envi("PGG2_V62B_MAX_ATTEMPTS", 3)
    cfg.poll_interval_ms = _envi("PGG2_V62B_POLL_INTERVAL_MS", 100)
    cfg.per_attempt_timeout_ms = _envi("PGG2_V62B_PER_ATTEMPT_TIMEOUT_MS", 300)
    cfg.total_budget_ms = _envi("PGG2_V62B_TOTAL_BUDGET_MS", 1500)
    cfg.final_wait_ms = _envi("PGG2_V62B_FINAL_WAIT_MS", 700)
    return cfg


# ============================================================================
#  Min-sol policy
# ============================================================================

def _compute_min_sol(
    cfg: V62BConfig,
    reason: str,
    cost_basis_sol: float,
    expected_sol_out_now: float,
    log_fn: Callable[[str], None],
    mint: str,
) -> tuple[float, str]:
    """Returns (min_sol_lamports_as_sol_float, policy_label)."""
    if reason in (REASON_EMERGENCY, REASON_MAX_HOLD, REASON_DUMP_ABORT):
        # Emergency family: clear the position, accept-any-price-but-not-zero
        log_fn(
            f"PGG2-V62B-EMERGENCY-MIN-SOL-POLICY mint={_short(mint)} "
            f"min_sol={cfg.emergency_min_sol:.6f} reason={reason}"
        )
        return cfg.emergency_min_sol, "emergency"

    if reason == REASON_BANK:
        bank_floor = cost_basis_sol + cfg.fees_estimate_sol + cfg.bank_small_profit_floor_sol
        expected_x_frac = expected_sol_out_now * cfg.bank_expected_fraction
        chosen = max(bank_floor, expected_x_frac)
        log_fn(
            f"PGG2-V62B-BANK-MIN-SOL-POLICY mint={_short(mint)} "
            f"bank_floor={bank_floor:.6f} expected_x_{cfg.bank_expected_fraction:.2f}={expected_x_frac:.6f} "
            f"chosen={chosen:.6f}"
        )
        return chosen, "bank"

    if reason == REASON_SCRATCH:
        scratch = max(0.000005, cost_basis_sol + cfg.fees_estimate_sol - cfg.scratch_tolerance_sol)
        log_fn(
            f"PGG2-V62B-BANK-MIN-SOL-POLICY mint={_short(mint)} "
            f"policy=scratch scratch={scratch:.6f}"
        )
        return scratch, "scratch"

    # Unknown reason — default to emergency to be safe
    log_fn(
        f"PGG2-V62B-MIN-SOL-UNKNOWN-REASON mint={_short(mint)} "
        f"reason={reason} fallback_to_emergency"
    )
    return cfg.emergency_min_sol, "unknown_fallback_emergency"


# ============================================================================
#  Direct RPC signature status (broker has no signature_status)
# ============================================================================

def _rpc_get_signature_statuses(
    rpc_url: str, sig: str, cfg: V62BConfig, log_fn: Callable[[str], None]
) -> Optional[dict]:
    """Returns the value for sig, or None on RPC error / unknown."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[sig], {"searchTransactionHistory": False}],
        }
        req = _urlreq.Request(
            rpc_url,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=cfg.rpc_request_timeout_sec) as resp:
            body = json.loads(resp.read())
        result = body.get("result") or {}
        values = result.get("value") or [None]
        return values[0]  # may be None if not yet seen
    except Exception as exc:
        log_fn(f"PGG2-V62B-RPC-ERR method=getSignatureStatuses err={type(exc).__name__}:{exc}")
        return None


def _status_summary(status: Optional[dict]) -> str:
    if status is None:
        return "unknown"
    if status.get("err"):
        return f"err:{status['err']}"
    cs = status.get("confirmationStatus") or status.get("status")
    if cs in ("processed", "confirmed", "finalized"):
        return cs
    return "pending"


# ============================================================================
#  Main entry point
# ============================================================================

def v62b_close_position(
    broker: Any,
    mint: str,
    raw_balance: int,
    cost_basis_sol: float,
    expected_sol_out_now: float,
    reason: str,
    rpc_url: str,
    *,
    sell_quote_existing: Optional[dict] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    token_program: str = "token-2022",
) -> V62BResult:
    """Authoritative sell. Blocks until confirmed or final-fail.

    broker must provide:
      - build_sell(mint_str, amount, slippage) -> quote dict
      - retarget_sell_min_sol(quote, mint_str, min_sol) -> quote dict
      - rate_amount_out(quote) -> float (expected SOL out)
      - sign_transaction(txn_str) -> (signed_b64, sig_preview)
      - send_signed(signed_b64) -> sent_sig
      - sell_slippage -> float (default sell slippage)
      - token_balance_raw(mint_pk) -> int (raw balance re-query)
    """
    cfg = _config()
    log = log_fn if log_fn is not None else print
    short = _short(mint)
    start_ms = _now_ms()

    # Track confirmation across attempts
    sent_sigs: list[str] = []

    log(
        f"PGG2-V62B-SELL-ROUTER-START mint={short} reason={reason} "
        f"raw_balance={raw_balance} cost_basis_sol={cost_basis_sol:.6f} "
        f"expected_sol_out={expected_sol_out_now:.6f} token_program={token_program}"
    )

    # Determine min_sol for this exit reason
    min_sol, min_sol_policy = _compute_min_sol(
        cfg, reason, cost_basis_sol, expected_sol_out_now, log, mint
    )

    # Sanity: cannot proceed if raw_balance is zero
    if raw_balance <= 0:
        log(f"PGG2-V62B-SELL-ROUTER-FAIL mint={short} blocker=raw_balance_zero attempts=0 elapsed_ms=0")
        return V62BResult(
            confirmed=False, confirmed_sig=None, actual_sol_out=0.0,
            attempts=0, elapsed_ms=0, final_min_sol_used=min_sol,
            blocker="raw_balance_zero",
        )

    # Import broker helpers lazily (mint pubkey)
    try:
        from pgg2_direct_pump import as_pubkey
    except Exception as exc:
        log(f"PGG2-V62B-SELL-ROUTER-FAIL mint={short} blocker=as_pubkey_import_err err={exc}")
        return V62BResult(
            confirmed=False, confirmed_sig=None, actual_sol_out=0.0,
            attempts=0, elapsed_ms=0, final_min_sol_used=min_sol,
            blocker=f"import_err:{exc}",
        )

    mint_pk = as_pubkey(mint)

    # Retry loop
    attempts = 0
    used_emergency = False
    last_attempt_sig: Optional[str] = None
    final_quote_expected: float = expected_sol_out_now

    while attempts < cfg.max_attempts:
        attempts += 1
        attempt_elapsed = _now_ms() - start_ms
        if attempt_elapsed >= cfg.total_budget_ms:
            log(
                f"PGG2-V62B-SELL-RETRY mint={short} attempt={attempts} "
                f"reason=total_budget_exhausted elapsed_ms={attempt_elapsed}"
            )
            break

        # Re-query raw balance (V62B always uses fresh balance)
        try:
            cur_raw = int(broker.token_balance_raw(mint_pk))
        except Exception as exc:
            log(f"PGG2-V62B-RAW-BALANCE-ERR mint={short} attempt={attempts} err={exc}")
            cur_raw = raw_balance  # fall back to passed-in value

        log(f"PGG2-V62B-RAW-BALANCE mint={short} attempt={attempts} raw={cur_raw}")

        # === V65 duplicate-safe pre-retry state check ===
        # Before sending a new attempt, also poll status of all previously-sent
        # sigs. If any has reached processed/confirmed/finalized status, the
        # position has already cleared via that earlier sig — sending another
        # retry now would race the same ATA-already-closed state and waste a
        # tx fee on a Custom 3012 reject (V63 4rzH forensic, 50 000 lamports
        # wasted on attempts 1 + 3 in V62B Stage A RUN1).
        if attempts > 1 and sent_sigs:
            for _prior in reversed(sent_sigs):
                _ps = _rpc_get_signature_statuses(rpc_url, _prior, cfg, log)
                if _ps is None or _ps.get("err"):
                    continue
                _pcs = _ps.get("confirmationStatus") or _ps.get("status")
                if _pcs in ("processed", "confirmed", "finalized"):
                    try:
                        _actual = float(broker.transaction_wallet_delta_sol(_prior))
                    except Exception:
                        _actual = 0.0
                    log(
                        f"PGG2-V62B-SELL-RESOLVED-BY-STATE mint={short} "
                        f"sig={_prior} actual_sol_out={_actual:.6f} attempts={attempts} "
                        f"elapsed_ms={_now_ms()-start_ms} "
                        f"reason=prior_sig_confirmed_skip_retry"
                    )
                    return V62BResult(
                        confirmed=True, confirmed_sig=_prior,
                        actual_sol_out=_actual, attempts=attempts - 1,
                        elapsed_ms=_now_ms()-start_ms,
                        final_min_sol_used=min_sol, blocker=None,
                        used_emergency=used_emergency,
                    )

        if cur_raw <= 0:
            # Position is gone — a prior send probably landed. Stop, mark success.
            log(
                f"PGG2-V62B-SELL-CONFIRMED mint={short} sig=prior_attempt "
                f"actual_sol_out=unknown_from_prior attempts={attempts} "
                f"elapsed_ms={_now_ms()-start_ms} reason=balance_zero_likely_prior_landed"
            )
            # Best-effort: scan our sent_sigs to find a confirmed one
            for prior_sig in reversed(sent_sigs):
                status = _rpc_get_signature_statuses(rpc_url, prior_sig, cfg, log)
                if status is not None and not status.get("err"):
                    cs = status.get("confirmationStatus") or status.get("status")
                    if cs in ("processed", "confirmed", "finalized"):
                        try:
                            actual_out = float(
                                broker.transaction_wallet_delta_sol(prior_sig)
                            )
                        except Exception:
                            actual_out = 0.0
                        return V62BResult(
                            confirmed=True, confirmed_sig=prior_sig,
                            actual_sol_out=actual_out, attempts=attempts,
                            elapsed_ms=_now_ms()-start_ms,
                            final_min_sol_used=min_sol, blocker=None,
                            used_emergency=used_emergency,
                        )
            # No identifiable prior confirm but balance is zero → still treat as success
            return V62BResult(
                confirmed=True, confirmed_sig=None,
                actual_sol_out=0.0, attempts=attempts,
                elapsed_ms=_now_ms()-start_ms,
                final_min_sol_used=min_sol,
                blocker="balance_zero_unknown_sig",
                used_emergency=used_emergency,
            )

        # Build / requote sell tx
        try:
            sell_slippage = float(getattr(broker, "sell_slippage", 0.30))
            sell_quote = broker.build_sell(mint, f"raw:{cur_raw}", sell_slippage)
            expected_now = float(broker.rate_amount_out(sell_quote))
            final_quote_expected = expected_now
        except Exception as exc:
            log(
                f"PGG2-V62B-SELL-BUILD-ERR mint={short} attempt={attempts} "
                f"err={type(exc).__name__}:{exc}"
            )
            # Try next attempt
            time.sleep(0.050)
            continue

        log(
            f"PGG2-V62B-SELL-BUILD mint={short} attempt={attempts} "
            f"expected_sol_out={expected_now:.6f} min_sol={min_sol:.6f} "
            f"policy={min_sol_policy} raw_in={cur_raw}"
        )

        # Apply min_sol guard
        try:
            guarded = broker.retarget_sell_min_sol(sell_quote, mint, min_sol)
        except Exception as exc:
            log(
                f"PGG2-V62B-SELL-RETARGET-ERR mint={short} attempt={attempts} "
                f"err={type(exc).__name__}:{exc}"
            )
            time.sleep(0.050)
            continue

        log(
            f"PGG2-V62B-SELL-GUARD mint={short} attempt={attempts} "
            f"min_sol_target={min_sol:.6f}"
        )

        # Sign + send
        try:
            signed, sig_preview = broker.sign_transaction(str(guarded["txn"]))
        except Exception as exc:
            log(
                f"PGG2-V62B-SIGN-ERR mint={short} attempt={attempts} "
                f"err={type(exc).__name__}:{exc}"
            )
            time.sleep(0.050)
            continue

        send_t0 = _now_ms()
        try:
            sent_sig = broker.send_signed(signed)
            sent_sigs.append(sent_sig)
            last_attempt_sig = sent_sig
        except Exception as exc:
            log(
                f"PGG2-V62B-SEND-ERR mint={short} attempt={attempts} "
                f"err={type(exc).__name__}:{exc}"
            )
            time.sleep(0.050)
            continue

        log(
            f"PGG2-V62B-SELL-SEND mint={short} attempt={attempts} sig={sent_sig} "
            f"sig_preview={sig_preview[:32]} expected={expected_now:.6f} "
            f"min_sol={min_sol:.6f}"
        )

        # Poll for confirmation
        poll_deadline = send_t0 + cfg.per_attempt_timeout_ms
        confirmed_this_attempt = False
        while _now_ms() < poll_deadline:
            time.sleep(cfg.poll_interval_ms / 1000.0)
            status = _rpc_get_signature_statuses(rpc_url, sent_sig, cfg, log)
            summary = _status_summary(status)
            log(
                f"PGG2-V62B-SELL-STATUS mint={short} attempt={attempts} "
                f"sig={sent_sig[:32]} status={summary}"
            )
            if status is None:
                continue
            if status.get("err"):
                # On-chain error (e.g. Custom 6023). Stop this attempt; loop will retry.
                log(
                    f"PGG2-V62B-SELL-RETRY mint={short} attempt={attempts} "
                    f"reason=on_chain_err:{status['err']} elapsed_ms={_now_ms()-start_ms}"
                )
                break
            cs = status.get("confirmationStatus") or status.get("status")
            if cs in ("processed", "confirmed", "finalized"):
                confirmed_this_attempt = True
                break

        if confirmed_this_attempt:
            # Resolve actual wallet delta if broker supports it
            try:
                actual_out = float(broker.transaction_wallet_delta_sol(sent_sig))
            except Exception:
                actual_out = expected_now  # best-effort
            log(
                f"PGG2-V62B-SELL-CONFIRMED mint={short} sig={sent_sig} "
                f"actual_sol_out={actual_out:.6f} attempts={attempts} "
                f"elapsed_ms={_now_ms()-start_ms} reason={reason}"
            )
            return V62BResult(
                confirmed=True, confirmed_sig=sent_sig,
                actual_sol_out=actual_out, attempts=attempts,
                elapsed_ms=_now_ms()-start_ms, final_min_sol_used=min_sol,
                blocker=None, used_emergency=used_emergency,
            )

        # Not confirmed this attempt — retry log
        log(
            f"PGG2-V62B-SELL-RETRY mint={short} attempt={attempts} "
            f"reason=not_confirmed_in_{cfg.per_attempt_timeout_ms}ms "
            f"elapsed_ms={_now_ms()-start_ms}"
        )

    # All bank/scratch attempts exhausted (or budget exceeded). Escalate to emergency
    # IF we weren't already in emergency mode.
    if reason not in (REASON_EMERGENCY, REASON_MAX_HOLD, REASON_DUMP_ABORT):
        log(
            f"PGG2-V62B-SELL-RETRY mint={short} attempt=escalate-to-emergency "
            f"reason=bank_exhausted elapsed_ms={_now_ms()-start_ms}"
        )
        # Recurse with emergency policy. Budget resets but max_attempts stays.
        return v62b_close_position(
            broker, mint, raw_balance, cost_basis_sol,
            final_quote_expected, REASON_EMERGENCY,
            rpc_url, log_fn=log, token_program=token_program,
        )

    # Final fail: even emergency couldn't clear
    # One more attempt to verify any sent sig confirmed late
    if sent_sigs:
        time.sleep(cfg.final_wait_ms / 1000.0)
        for prior_sig in reversed(sent_sigs):
            status = _rpc_get_signature_statuses(rpc_url, prior_sig, cfg, log)
            summary = _status_summary(status)
            if status is not None and not status.get("err"):
                cs = status.get("confirmationStatus") or status.get("status")
                if cs in ("processed", "confirmed", "finalized"):
                    try:
                        actual_out = float(broker.transaction_wallet_delta_sol(prior_sig))
                    except Exception:
                        actual_out = 0.0
                    log(
                        f"PGG2-V62B-SELL-CONFIRMED mint={short} sig={prior_sig} "
                        f"actual_sol_out={actual_out:.6f} attempts={attempts} "
                        f"elapsed_ms={_now_ms()-start_ms} reason=late_confirm"
                    )
                    return V62BResult(
                        confirmed=True, confirmed_sig=prior_sig,
                        actual_sol_out=actual_out, attempts=attempts,
                        elapsed_ms=_now_ms()-start_ms,
                        final_min_sol_used=min_sol, blocker=None,
                        used_emergency=used_emergency,
                    )

    # JUPITER FALLBACK IS NOT INVOKED HERE. Caller must NOT call Jupiter for pump_bc.
    log(
        f"PGG2-V62B-JUPITER-FALLBACK-BLOCKED mint={short} "
        f"reason=router_owns_pump_bc attempts={attempts} elapsed_ms={_now_ms()-start_ms}"
    )

    log(
        f"PGG2-V62B-SELL-ROUTER-FAIL mint={short} blocker=router_final_fail "
        f"attempts={attempts} elapsed_ms={_now_ms()-start_ms} "
        f"last_sig={last_attempt_sig or 'none'}"
    )
    return V62BResult(
        confirmed=False, confirmed_sig=None, actual_sol_out=0.0,
        attempts=attempts, elapsed_ms=_now_ms()-start_ms,
        final_min_sol_used=min_sol, blocker="router_final_fail",
        used_emergency=(reason in (REASON_EMERGENCY, REASON_MAX_HOLD, REASON_DUMP_ABORT)),
    )


# ============================================================================
#  Self-test (no broker — just validate config + min_sol math)
# ============================================================================

if __name__ == "__main__":
    print("=== V62B sell router config + min_sol policy self-test ===")
    cfg = _config()
    print(f"bank_expected_fraction = {cfg.bank_expected_fraction}")
    print(f"bank_small_profit_floor_sol = {cfg.bank_small_profit_floor_sol}")
    print(f"emergency_min_sol = {cfg.emergency_min_sol}")
    print(f"max_attempts = {cfg.max_attempts}")
    print(f"per_attempt_timeout_ms = {cfg.per_attempt_timeout_ms}")
    print(f"total_budget_ms = {cfg.total_budget_ms}")
    print()

    # Fwye replay numbers
    cost_basis = 0.005000
    expected = 0.005956  # original bank quote
    print(f"--- Fwye bank policy (cost={cost_basis}, expected={expected}) ---")
    min_sol, policy = _compute_min_sol(cfg, REASON_BANK, cost_basis, expected, print, "Fwye..pump")
    print(f"  min_sol = {min_sol:.6f} SOL  (policy={policy})")
    print()
    print(f"--- Fwye emergency policy ---")
    min_sol, policy = _compute_min_sol(cfg, REASON_EMERGENCY, cost_basis, 0.006131, print, "Fwye..pump")
    print(f"  min_sol = {min_sol:.6f} SOL  (policy={policy})")
    print()
    print(f"--- Fwye scratch policy ---")
    min_sol, policy = _compute_min_sol(cfg, REASON_SCRATCH, cost_basis, 0.005100, print, "Fwye..pump")
    print(f"  min_sol = {min_sol:.6f} SOL  (policy={policy})")
    print()
    print("=== self-test complete ===")
