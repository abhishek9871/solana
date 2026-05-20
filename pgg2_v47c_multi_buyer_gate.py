"""V47C - Multi-Buyer Flow Quality Gate.

A causal gate that rejects single-buyer "shadow" signals and concentration
risk before the V47B guarded-branch sweep is even run.

API:
  evaluate_multi_buyer_gate(buyer_stats: dict) -> (pass: bool, blocker: str|None)

Required buyer_stats fields:
  - unique_buyers_250ms (int)
  - pending_buy_count_250ms (int)
  - pending_buy_sol_250ms (float)
  - pending_sell_sol_250ms (float)
  - top_buyer_share_250ms (float, 0..1)

Conditions (ALL must hold):
  - unique_buyers_250ms >= 2
  - pending_buy_count_250ms >= 2
  - pending_buy_sol_250ms > pending_sell_sol_250ms
  - top_buyer_share_250ms <= 0.75

PURE LOGIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import re as _re
import sys
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
            f"V47C-MULTI-BUYER-GATE-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47c_multi_buyer_gate")


MULTI_BUYER_MIN_UNIQUE = 2
MULTI_BUYER_MIN_PB_COUNT = 2
TOP_BUYER_SHARE_MAX = 0.75


def evaluate_multi_buyer_gate(
    buyer_stats: Dict[str, Any],
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Tuple[bool, Optional[str]]:
    """Apply V47C multi-buyer quality gate.

    Returns (pass, blocker_reason).
    """
    ub_250 = int(buyer_stats.get("unique_buyers_250ms", 0) or 0)
    pbc_250 = int(buyer_stats.get("pending_buy_count_250ms", 0) or 0)
    pbs_250 = float(buyer_stats.get("pending_buy_sol_250ms", 0.0) or 0.0)
    pss_250 = float(buyer_stats.get("pending_sell_sol_250ms", 0.0) or 0.0)
    tbs_250 = float(buyer_stats.get("top_buyer_share_250ms", 0.0) or 0.0)

    blocker: Optional[str] = None
    pass_ = True

    if ub_250 == 1:
        pass_ = False
        blocker = "single_buyer_shadow_only"
    elif ub_250 < MULTI_BUYER_MIN_UNIQUE:
        pass_ = False
        blocker = "single_buyer_shadow_only"
    elif pbc_250 < MULTI_BUYER_MIN_PB_COUNT:
        pass_ = False
        blocker = "pending_buy_count_lt_2"
    elif pbs_250 <= pss_250:
        pass_ = False
        blocker = "buy_sol_not_above_sell_sol"
    elif tbs_250 > TOP_BUYER_SHARE_MAX:
        pass_ = False
        blocker = "top_buyer_share_too_high"

    if logger is not None:
        try:
            short = (
                (mint_for_log[:4] + ".." + mint_for_log[-4:])
                if mint_for_log and len(mint_for_log) > 10
                else (mint_for_log or "-")
            )
            logger(
                f"PGG2-V47C-MULTI-BUYER-GATE mint={short} "
                f"ub250={ub_250} pbc250={pbc_250} "
                f"pbsol={pbs_250:.6f} pssol={pss_250:.6f} "
                f"tbshare={tbs_250:.3f} "
                f"pass={int(pass_)} "
                f"blocker={blocker or '-'}"
            )
        except Exception:
            pass

    return (bool(pass_), blocker)


__all__ = [
    "evaluate_multi_buyer_gate",
    "MULTI_BUYER_MIN_UNIQUE",
    "MULTI_BUYER_MIN_PB_COUNT",
    "TOP_BUYER_SHARE_MAX",
]
