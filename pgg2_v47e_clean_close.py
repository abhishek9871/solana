"""V47E - Clean-Close pass-criteria evaluator.

V47D dry-live failed because the pass criterion was "10 entries fired".
V47D actually opened 12 entries, closed 6, and left 6 pending at stop.
V47E redefines pass as:

  closed_nonneg >= target_closed_nonneg
  AND closed_neg == 0
  AND pending == 0
  AND open_positions == 0

Called at end-of-run by the V47E no-send and dry-live drivers.

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
from typing import Any, Callable, Dict, Optional


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
            f"V47E-CLEAN-CLOSE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47e_clean_close")


REASON_PASS = "pass"
REASON_BELOW_TARGET = "below_target_closed_nonneg"
REASON_NEGATIVE = "negative_close_occurred"
REASON_PENDING = "pending_positions_remain"
REASON_OPEN = "open_positions_remain"


def evaluate_clean_close(
    entries: int,
    closed_nonneg: int,
    closed_neg: int,
    pending: int,
    open_positions: int,
    target_closed_nonneg: int = 10,
    logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Return {entries, closed_nonneg, closed_neg, pending, open_positions,
    pass, fail_reason}.

    pass = (closed_nonneg >= target_closed_nonneg
            AND closed_neg == 0
            AND pending == 0
            AND open_positions == 0)
    """
    entries_i = int(entries)
    cn = int(closed_nonneg)
    cg = int(closed_neg)
    pd = int(pending)
    op = int(open_positions)
    tg = int(target_closed_nonneg)

    # Order of reasons: negative > open > pending > below_target
    if cg > 0:
        is_pass = False
        reason = REASON_NEGATIVE
    elif op > 0:
        is_pass = False
        reason = REASON_OPEN
    elif pd > 0:
        is_pass = False
        reason = REASON_PENDING
    elif cn < tg:
        is_pass = False
        reason = REASON_BELOW_TARGET
    else:
        is_pass = True
        reason = REASON_PASS

    result = {
        "entries": entries_i,
        "closed_nonneg": cn,
        "closed_neg": cg,
        "pending": pd,
        "open_positions": op,
        "target_closed_nonneg": tg,
        "pass": is_pass,
        "fail_reason": reason,
    }

    if logger is not None:
        try:
            logger(
                f"PGG2-V47E-CLEAN-CLOSE-STATUS entries={entries_i} "
                f"closed_nn={cn} closed_n={cg} pending={pd} "
                f"open={op} pass={int(is_pass)} reason={reason}"
            )
        except Exception:
            pass

    return result


__all__ = [
    "evaluate_clean_close",
    "REASON_PASS", "REASON_BELOW_TARGET", "REASON_NEGATIVE",
    "REASON_PENDING", "REASON_OPEN",
]


# ---- self-test (executed at import) ---------------------------------
def _self_test() -> None:
    # Pass case
    r = evaluate_clean_close(10, 10, 0, 0, 0, 10)
    if not r["pass"] or r["fail_reason"] != REASON_PASS:
        raise RuntimeError(f"clean_close_selftest_pass_failed: {r}")

    # Negative-close fail
    r = evaluate_clean_close(10, 9, 1, 0, 0, 10)
    if r["pass"] or r["fail_reason"] != REASON_NEGATIVE:
        raise RuntimeError(f"clean_close_selftest_neg_failed: {r}")

    # Pending fail
    r = evaluate_clean_close(10, 8, 0, 2, 0, 10)
    if r["pass"] or r["fail_reason"] != REASON_PENDING:
        raise RuntimeError(f"clean_close_selftest_pending_failed: {r}")

    # Open positions fail
    r = evaluate_clean_close(10, 9, 0, 0, 1, 10)
    if r["pass"] or r["fail_reason"] != REASON_OPEN:
        raise RuntimeError(f"clean_close_selftest_open_failed: {r}")

    # Below target
    r = evaluate_clean_close(5, 5, 0, 0, 0, 10)
    if r["pass"] or r["fail_reason"] != REASON_BELOW_TARGET:
        raise RuntimeError(f"clean_close_selftest_below_failed: {r}")

    # V47D scenario: 12 entries, 5 banks, 1 clamp_loss, 6 pending -> NEGATIVE
    r = evaluate_clean_close(12, 5, 1, 6, 0, 10)
    if r["pass"] or r["fail_reason"] != REASON_NEGATIVE:
        raise RuntimeError(f"clean_close_selftest_v47d_failed: {r}")


_self_test()
