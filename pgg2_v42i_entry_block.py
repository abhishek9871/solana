"""V42I Phase 3 — Anti-late and anti-early entry-block guards.

Sits BETWEEN the V42I rule pass and the candidate emit. Even if a rule
passes, we still block entry if any of these adverse conditions holds:

  - no completed virtual bank yet
  - the active ticket transitioned to a terminal (bank/loss/expired)
    within this same update — the ticket already completed; we are
    chasing a cooled top (this is the V42H/V42HSAFE failure mode)
  - active_ticket_age_ms > 900           (too late)
  - active_ticket_current_pnl < +0.00005 (not positive)
  - active_ticket_pnl_gradient < 0       (deteriorating)
  - completed virtual loss after latest bank (count > 0)
  - current_quote_sol < active_ticket_break_even_quote
       break-even = open-snap_quote_sol + 2 * 0.0000287 tx fee, mapped to
       SOL (we work in PnL space, so check current_pnl > -2*tx_fee_sol)
  - last negative curve update is after latest_completed_bank_time_ms

Returns (block: bool, reason: str | None).
PURE ARITHMETIC. NO TRANSACTIONS. Static-grep enforced at module load.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Dict, Optional, Tuple


# ----- static-grep self-check ---------------------------------------
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
        sys.stderr.write(f"V42I-ENTRY-BLOCK-ABORT forbidden_call_pattern={_pat}\n")
        raise RuntimeError("forbidden_call_pattern_in_v42i_entry_block")


# Mirror engine + V42I rule thresholds.
TX_FEE_SOL = 0.0000287
BREAK_EVEN_PNL_FLOOR = -2.0 * TX_FEE_SOL  # below this means we'd be under cost+fees
ACTIVE_TICKET_AGE_MAX_MS = 900
ACTIVE_POS_THRESHOLD_SOL = 0.00005


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def should_block_entry(
    state_dict: Dict[str, Any],
    ts_ms_now: int,
) -> Tuple[bool, Optional[str]]:
    """Return (block, reason). reason is None when block=False.

    Apply guards IN ORDER (first hit wins). The order is chosen so the
    most-critical structural blocks come first."""
    # 1. No completed virtual bank yet — we want at least one bank as
    #    proof the mint can move.
    cb3k = int(state_dict.get("completed_virtual_banks_last_3000ms") or 0)
    lcbtm = state_dict.get("latest_completed_bank_time_ms")
    if cb3k == 0 and lcbtm is None:
        return True, "no_completed_virtual_bank_yet"

    # 2. Active ticket completed within this same update (cooled top).
    #    Heuristic: state shows no active ticket BUT a bank/loss/expired
    #    fired at ts_ms_now or within ~50ms before.
    if state_dict.get("active_ticket_id") is None:
        if lcbtm is not None and abs(int(ts_ms_now) - int(lcbtm)) <= 50:
            return True, "active_ticket_just_banked_cooled_top"

    age = state_dict.get("active_ticket_age_ms")
    if age is None:
        return True, "no_active_ticket"

    if int(age) > ACTIVE_TICKET_AGE_MAX_MS:
        return True, "active_ticket_age_gt_max"

    cur_pnl = state_dict.get("active_ticket_current_pnl")
    if cur_pnl is None or float(cur_pnl) < ACTIVE_POS_THRESHOLD_SOL:
        return True, "active_ticket_pnl_below_positive_threshold"

    grad = state_dict.get("active_ticket_pnl_gradient")
    if grad is None or float(grad) < 0.0:
        return True, "active_ticket_gradient_negative"

    if int(state_dict.get("completed_virtual_losses_after_latest_bank") or 0) > 0:
        return True, "completed_virtual_loss_after_latest_bank"

    # 7. Below break-even (current quote below open-snap + fees).
    #    We work in PnL space: if current_pnl < -2*tx_fee, the ticket is
    #    structurally underwater (would close negative on entry).
    if float(cur_pnl) < BREAK_EVEN_PNL_FLOOR:
        return True, "active_ticket_below_break_even_pnl"

    # 8. last negative curve update is after latest bank.
    if bool(state_dict.get("negative_curve_after_latest_bank")):
        return True, "negative_curve_update_after_latest_bank"

    return False, None


def format_log_line(
    mint: str,
    rule: str,
    block: bool,
    reason: Optional[str],
) -> str:
    return (
        f"PGG2-V42I-ENTRY-BLOCK mint={_short(mint)} rule={rule or '?'} "
        f"block={bool(block)} block_reason={reason or 'none'}"
    )


__all__ = [
    "should_block_entry",
    "format_log_line",
    "TX_FEE_SOL",
    "BREAK_EVEN_PNL_FLOOR",
    "ACTIVE_TICKET_AGE_MAX_MS",
    "ACTIVE_POS_THRESHOLD_SOL",
]
