"""V42H-SAFE Phase 4 — Redesigned late-entry blocker.

Replaces the V42H late-entry allow path `current_at_or_above_last_bank`
(which let all 10 V42H Phase-7 candidates through — including all 4 losses).

V42H-SAFE late-entry rule: ALLOW entry IF AND ONLY IF ALL of:
  1. current_local_quote >= break_even_quote + 0.00020 SOL
  2. latest_quote_gradient >= 0  (current minus previous slot, on local-quote series)
  3. no_virtual_loss_after_last_bank
     (= zero virtual losses with ts > last_virtual_bank_ts on this mint)
  4. time_since_last_virtual_bank_ms <= 350

Explicit non-conditions (do NOT enforce these):
  - Do NOT require current_local_quote > last_bank_quote (that was V42H — too
    aggressive on rising tops).
  - Do NOT allow stale pullbacks: anything with last_virtual_bank older than
    350ms is BLOCKED.

PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Dict, Iterable, List, Optional


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
        sys.stderr.write(f"V42HSAFE-LATE-ENTRY-ABORT forbidden_call_pattern={_pat}\n")
        raise RuntimeError("forbidden_call_pattern_in_v42hsafe_late_entry")


def late_entry_decision(
    mint: str,
    ticket_history: Iterable[Dict[str, Any]],
    current_quote_sol: float,
    break_even_quote: float,
    latest_quote_gradient: float,
    last_virtual_bank_ts_ms: Optional[int],
    last_virtual_loss_ts_ms: Optional[int],
    ts_ms_now: int,
) -> Dict[str, Any]:
    """Return {allowed: bool, reason: str, fields: {...}}.

    `reason` is either an <allowed_reason> when allowed=True or a <block_reason>
    when allowed=False. First failing condition wins.

    `ticket_history` is filtered causally to outcome_ts_ms <= ts_ms_now so the
    caller can pass the full mint ticket list without pre-filtering.
    """
    th: List[Dict[str, Any]] = [
        t for t in ticket_history
        if t.get("outcome_ts_ms") is None
        or int(t.get("outcome_ts_ms") or 0) <= int(ts_ms_now)
    ]

    fields: Dict[str, Any] = {
        "mint": mint,
        "ts_ms_now": int(ts_ms_now),
        "current_local_quote": float(current_quote_sol),
        "break_even_quote": float(break_even_quote),
        "be_plus_safety": float(break_even_quote) + 0.00020,
        "latest_quote_gradient": float(latest_quote_gradient),
        "last_virtual_bank_ts_ms": (
            None if last_virtual_bank_ts_ms is None else int(last_virtual_bank_ts_ms)
        ),
        "last_virtual_loss_ts_ms": (
            None if last_virtual_loss_ts_ms is None else int(last_virtual_loss_ts_ms)
        ),
        "time_since_last_virtual_bank_ms": None,
        "virtual_loss_after_last_bank": False,
    }
    if last_virtual_bank_ts_ms is not None:
        fields["time_since_last_virtual_bank_ms"] = int(ts_ms_now) - int(last_virtual_bank_ts_ms)

    # No bank yet -> we have nothing to gate against; block (the strict subset
    # never enters before at least one bank exists).
    if last_virtual_bank_ts_ms is None:
        return {"allowed": False, "reason": "no_virtual_bank_yet", "fields": fields}

    # 1. break-even + safety
    if float(current_quote_sol) < float(break_even_quote) + 0.00020:
        return {"allowed": False, "reason": "current_quote_below_break_even_plus_safety", "fields": fields}

    # 2. quote gradient
    if float(latest_quote_gradient) < 0.0:
        return {"allowed": False, "reason": "latest_quote_gradient_negative", "fields": fields}

    # 3. no virtual loss after the last bank
    if (
        last_virtual_loss_ts_ms is not None
        and int(last_virtual_loss_ts_ms) > int(last_virtual_bank_ts_ms)
    ):
        fields["virtual_loss_after_last_bank"] = True
        return {"allowed": False, "reason": "virtual_loss_after_last_bank", "fields": fields}

    # Defensive recompute from ticket_history in case caller's last_*_ts_ms
    # disagree with the engine state.
    losses_after_bank = [
        int(t.get("outcome_ts_ms") or 0) for t in th
        if t.get("outcome") == "virtual_loss"
        and t.get("outcome_ts_ms") is not None
        and int(t.get("outcome_ts_ms") or 0) > int(last_virtual_bank_ts_ms)
    ]
    if losses_after_bank:
        fields["virtual_loss_after_last_bank"] = True
        return {"allowed": False, "reason": "virtual_loss_after_last_bank_th", "fields": fields}

    # 4. last bank not stale
    tslb = int(ts_ms_now) - int(last_virtual_bank_ts_ms)
    fields["time_since_last_virtual_bank_ms"] = tslb
    if tslb > 350:
        return {"allowed": False, "reason": "time_since_last_virtual_bank_gt_350ms", "fields": fields}

    return {
        "allowed": True,
        "reason": "v42hsafe_strict_late_entry_allowed",
        "fields": fields,
    }


def format_log_line(decision: Dict[str, Any]) -> str:
    f = decision.get("fields", {}) or {}
    mint = str(f.get("mint", ""))
    mshort = (mint[:4] + ".." + mint[-4:]) if len(mint) > 10 else mint
    return (
        f"PGG2-V42HSAFE-LATE-ENTRY mint={mshort} "
        f"cq={f.get('current_local_quote',0.0):.9f} "
        f"be_safety={f.get('be_plus_safety',0.0):.9f} "
        f"grad={f.get('latest_quote_gradient',0.0):+.9f} "
        f"tslb={f.get('time_since_last_virtual_bank_ms','?')} "
        f"allowed={bool(decision.get('allowed'))} "
        f"reason={decision.get('reason') or '?'}"
    )


__all__ = ["late_entry_decision", "format_log_line"]
