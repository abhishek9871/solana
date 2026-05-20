"""V47B - Guarded-Branch Simulator.

Computes 3 branches (expected/partial/adverse) for a given mint+size against
the latest curve state plus pending external buys/sells, and classifies each
branch as WIN / SAFE_BUY_FAIL / UNSAFE_OPEN / UNKNOWN.

The KEY INSIGHT of V47B vs V47: on Pump.fun bonding curve, the `buy`
instruction takes a `min_tokens_out` parameter. If the actual fill would
produce fewer tokens than this floor, the transaction reverts on-chain with
TooMuchSolRequired (or equivalent), costing only the signature fee (~5e-6 SOL)
and consuming NO principal. This is the on-chain safety primitive.

V47B's success criterion is therefore "zero negative CLOSES" rather than
"zero failed buys" - safe failed buys are acceptable so long as the fee
budget is respected.

PURE ARITHMETIC. NO TRANSACTIONS. NO PAID FEEDS. Static-grep enforced.
"""
from __future__ import annotations

import os
import re as _re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


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
            f"V47B-GUARDED-SIM-ABORT forbidden_call_pattern={_pat}\n"
        )
        raise RuntimeError("forbidden_call_pattern_in_v47b_guarded_branch_sim")


# Branch outcome labels.
BRANCH_WIN = "BRANCH_WIN"
BRANCH_SAFE_BUY_FAIL = "BRANCH_SAFE_BUY_FAIL"
BRANCH_UNSAFE_OPEN = "BRANCH_UNSAFE_OPEN"
BRANCH_UNKNOWN = "BRANCH_UNKNOWN"

# Scratch threshold (SOL) above which we classify a branch as WIN.
_SCRATCH_WIN_TH_SOL = 0.00005
# Clamp/loss threshold below which we classify a branch as UNSAFE_OPEN.
_UNSAFE_CLAMP_TH_SOL = -0.00050


def _import_quote_helpers():
    sys.path.insert(0, "/root/piggy")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pgg2_v42h_local_curve_quote import (  # type: ignore
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    )
    return (
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    )


def _short(mint: str) -> str:
    if not mint or len(mint) <= 10:
        return mint or "?"
    return mint[:4] + ".." + mint[-4:]


def _apply_external_buy_to_curve(
    curve_state, sol_in: float, V42HCurveState, LAMPORTS_PER_SOL,
    local_buy_quote_tokens_raw,
):
    """Apply an external buy of sol_in SOL to curve_state. Returns new state."""
    tokens_out, fees = local_buy_quote_tokens_raw(curve_state, float(sol_in))
    spend_lamports = int(round(float(sol_in) * LAMPORTS_PER_SOL))
    total_fee_bps = max(
        0,
        int(curve_state.fee_bps) + int(curve_state.creator_fee_bps),
    )
    net_sol = spend_lamports * 10_000 // (10_000 + total_fee_bps)
    if net_sol + fees > spend_lamports:
        net_sol -= net_sol + fees - spend_lamports
    net_for_curve = max(0, net_sol - 1)
    new_vsol = int(curve_state.virtual_sol_reserves) + int(net_for_curve)
    new_vtok = max(1, int(curve_state.virtual_token_reserves) - int(tokens_out))
    new_real_tok = max(0, int(curve_state.real_token_reserves) - int(tokens_out))
    return V42HCurveState(
        virtual_sol_reserves=int(new_vsol),
        virtual_token_reserves=int(new_vtok),
        real_token_reserves=int(new_real_tok),
        fee_bps=int(curve_state.fee_bps),
        creator_fee_bps=int(curve_state.creator_fee_bps),
    ), int(tokens_out)


def _apply_external_sell_to_curve(
    curve_state, tokens_in: int, V42HCurveState,
    local_sell_quote_sol,
):
    """Apply an external sell of tokens_in tokens. Returns new state."""
    gross_sol_net, fees = local_sell_quote_sol(curve_state, int(tokens_in))
    new_vtok = int(curve_state.virtual_token_reserves) + int(tokens_in)
    if gross_sol_net + fees > 0:
        gross_sol = int(gross_sol_net) + int(fees)
    else:
        gross_sol = int(gross_sol_net)
    new_vsol = max(1, int(curve_state.virtual_sol_reserves) - int(gross_sol))
    new_real_tok = (
        int(curve_state.real_token_reserves) + int(tokens_in)
    )
    return V42HCurveState(
        virtual_sol_reserves=int(new_vsol),
        virtual_token_reserves=int(new_vtok),
        real_token_reserves=int(new_real_tok),
        fee_bps=int(curve_state.fee_bps),
        creator_fee_bps=int(curve_state.creator_fee_bps),
    )


def _classify_branch(
    pnl_sol: float, tokens_received: int, guard_min_tokens: Optional[int],
) -> str:
    """Branch outcome classification.

    - If `guard_min_tokens` is provided and tokens_received < guard_min_tokens,
      the on-chain buy would revert -> BRANCH_SAFE_BUY_FAIL.
    - Else compare pnl to thresholds:
        pnl >= _SCRATCH_WIN_TH_SOL  -> BRANCH_WIN
        pnl <= _UNSAFE_CLAMP_TH_SOL -> BRANCH_UNSAFE_OPEN (position would open
          and lose principal beyond the clamp)
        between                     -> BRANCH_UNKNOWN (small range; effectively
          break-even, treated as informational)
    """
    if guard_min_tokens is not None and int(tokens_received) < int(guard_min_tokens):
        return BRANCH_SAFE_BUY_FAIL
    if pnl_sol >= _SCRATCH_WIN_TH_SOL:
        return BRANCH_WIN
    if pnl_sol <= _UNSAFE_CLAMP_TH_SOL:
        return BRANCH_UNSAFE_OPEN
    return BRANCH_UNKNOWN


def _simulate_one_branch(
    base_state, pending_buys: List[Tuple[int, float, str, int]],
    pending_sells: List[Tuple[int, int, str, int]],
    our_buy_sol: float, we_land_first: bool,
    V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
    local_buy_quote_tokens_raw, local_sell_quote_sol,
) -> Tuple[float, int, int]:
    """Compute (pnl_sol, tokens_received_by_us, sell_lamports_back).

    - we_land_first=True: OUR buy applied to base_state, THEN pending flow
      applied to advance the curve, THEN our sell at the final state.
    - we_land_first=False: pending flow applied to base_state FIRST, then our
      buy at the post-flow state, then our sell at the resulting state.
    """
    if we_land_first:
        # OUR buy first.
        tokens_we_got, _ = local_buy_quote_tokens_raw(
            base_state, float(our_buy_sol)
        )
        if tokens_we_got <= 0:
            return (-float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL), 0, 0)
        state_after_us, _ = _apply_external_buy_to_curve(
            base_state, float(our_buy_sol),
            V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
        )
        cur = state_after_us
        for ts, sol_in, _sig, _slot in sorted(
            pending_buys, key=lambda x: x[0]
        ):
            if sol_in <= 0:
                continue
            cur, _ = _apply_external_buy_to_curve(
                cur, float(sol_in),
                V42HCurveState, LAMPORTS_PER_SOL,
                local_buy_quote_tokens_raw,
            )
        for ts, tok_in, _sig, _slot in sorted(
            pending_sells, key=lambda x: x[0]
        ):
            if tok_in <= 0:
                continue
            cur = _apply_external_sell_to_curve(
                cur, int(tok_in), V42HCurveState, local_sell_quote_sol,
            )
        sell_lams, _ = local_sell_quote_sol(cur, int(tokens_we_got))
        sell_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)
        pnl = sell_sol - float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL)
        return (float(pnl), int(tokens_we_got), int(sell_lams))
    else:
        # Pending flow first.
        cur = base_state
        for ts, sol_in, _sig, _slot in sorted(
            pending_buys, key=lambda x: x[0]
        ):
            if sol_in <= 0:
                continue
            cur, _ = _apply_external_buy_to_curve(
                cur, float(sol_in),
                V42HCurveState, LAMPORTS_PER_SOL,
                local_buy_quote_tokens_raw,
            )
        for ts, tok_in, _sig, _slot in sorted(
            pending_sells, key=lambda x: x[0]
        ):
            if tok_in <= 0:
                continue
            cur = _apply_external_sell_to_curve(
                cur, int(tok_in), V42HCurveState, local_sell_quote_sol,
            )
        # Now OUR buy at the post-flow state.
        tokens_we_got, _ = local_buy_quote_tokens_raw(
            cur, float(our_buy_sol)
        )
        if tokens_we_got <= 0:
            return (-float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL), 0, 0)
        state_after_us, _ = _apply_external_buy_to_curve(
            cur, float(our_buy_sol),
            V42HCurveState, LAMPORTS_PER_SOL, local_buy_quote_tokens_raw,
        )
        sell_lams, _ = local_sell_quote_sol(state_after_us, int(tokens_we_got))
        sell_sol = float(sell_lams) / float(LAMPORTS_PER_SOL)
        pnl = sell_sol - float(our_buy_sol) - 2.0 * float(DEFAULT_TX_FEE_SOL)
        return (float(pnl), int(tokens_we_got), int(sell_lams))


def simulate_branches(
    latest_curve_state,
    size_sol: float,
    pending_buys: List[Tuple[int, float, str, int]],
    pending_sells: List[Tuple[int, int, str, int]],
    exec_delay_ms: int = 250,
    guard_min_tokens: Optional[int] = None,
    logger: Optional[Callable[[str], None]] = None,
    mint_for_log: str = "",
) -> Dict[str, Any]:
    """Compute V47B three branches:

    - expected (A): OUR buy lands FIRST, then all pending flow.
    - partial  (B): OUR buy lands FIRST, then only 50% of pending buys land
                    (deterministic: keep even-indexed pending buys); all
                    pending sells still land.
    - adverse  (C): all pending flow lands FIRST, OUR buy lands LAST.

    For each branch, returns: pnl_sol, tokens_received, sell_lamports_back.

    If `guard_min_tokens` is provided, each branch outcome is classified with
    the safe-fail guard predicate applied: tokens_received < guard_min_tokens
    => SAFE_BUY_FAIL (revert, only ~5e-6 SOL sig fee lost). The reported pnl
    for SAFE_BUY_FAIL branches is the cost-only path: -2x signature fee
    (no principal change).

    Returns dict per spec.
    """
    (
        V42HCurveState,
        LAMPORTS_PER_SOL,
        DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw,
        local_sell_quote_sol,
    ) = _import_quote_helpers()

    # Deterministic 50% partial: keep even-indexed pending buys.
    partial_buys = [
        b for i, b in enumerate(
            sorted(pending_buys or [], key=lambda x: x[0])
        ) if i % 2 == 0
    ]

    # Branch A: expected continuation (OUR buy first, then all pending).
    exp_pnl, exp_tokens, exp_sell_lams = _simulate_one_branch(
        latest_curve_state, list(pending_buys or []),
        list(pending_sells or []),
        float(size_sol), True,
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )

    # Branch B: partial 50% continuation.
    par_pnl, par_tokens, par_sell_lams = _simulate_one_branch(
        latest_curve_state, list(partial_buys),
        list(pending_sells or []),
        float(size_sol), True,
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )

    # Branch C: adverse (pending flow first, our buy last).
    adv_pnl, adv_tokens, adv_sell_lams = _simulate_one_branch(
        latest_curve_state, list(pending_buys or []),
        list(pending_sells or []),
        float(size_sol), False,
        V42HCurveState, LAMPORTS_PER_SOL, DEFAULT_TX_FEE_SOL,
        local_buy_quote_tokens_raw, local_sell_quote_sol,
    )

    # If guard is provided, override PnL on SAFE_BUY_FAIL branches to
    # cost-only (only 2x signature fee, no principal).
    safe_fail_cost = -2.0 * float(DEFAULT_TX_FEE_SOL)

    exp_outcome = _classify_branch(exp_pnl, exp_tokens, guard_min_tokens)
    par_outcome = _classify_branch(par_pnl, par_tokens, guard_min_tokens)
    adv_outcome = _classify_branch(adv_pnl, adv_tokens, guard_min_tokens)

    # PnL adjustment: on SAFE_BUY_FAIL branches the realized PnL is only the
    # signature fee; the trade reverts so principal is never spent and we
    # receive no tokens.
    if exp_outcome == BRANCH_SAFE_BUY_FAIL:
        exp_pnl_eff = float(safe_fail_cost)
    else:
        exp_pnl_eff = float(exp_pnl)
    if par_outcome == BRANCH_SAFE_BUY_FAIL:
        par_pnl_eff = float(safe_fail_cost)
    else:
        par_pnl_eff = float(par_pnl)
    if adv_outcome == BRANCH_SAFE_BUY_FAIL:
        adv_pnl_eff = float(safe_fail_cost)
    else:
        adv_pnl_eff = float(adv_pnl)

    # Pass criteria:
    # expected_branch  == BRANCH_WIN
    # partial_branch   in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL)
    # adverse_branch   in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL)
    # NO branch is BRANCH_UNSAFE_OPEN
    blocker = None
    pass_ = True
    if exp_outcome == BRANCH_UNSAFE_OPEN or par_outcome == BRANCH_UNSAFE_OPEN \
            or adv_outcome == BRANCH_UNSAFE_OPEN:
        pass_ = False
        blocker = "branch_unsafe_open"
    elif exp_outcome != BRANCH_WIN:
        pass_ = False
        blocker = f"expected_branch_not_win:{exp_outcome}"
    elif par_outcome not in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL):
        pass_ = False
        blocker = f"partial_branch_not_acceptable:{par_outcome}"
    elif adv_outcome not in (BRANCH_WIN, BRANCH_SAFE_BUY_FAIL):
        pass_ = False
        blocker = f"adverse_branch_not_acceptable:{adv_outcome}"

    out = {
        "size_sol": float(size_sol),
        "exec_delay_ms": int(exec_delay_ms),
        "expected_pnl": float(exp_pnl_eff),
        "partial_pnl": float(par_pnl_eff),
        "adverse_pnl": float(adv_pnl_eff),
        "expected_pnl_raw": float(exp_pnl),
        "partial_pnl_raw": float(par_pnl),
        "adverse_pnl_raw": float(adv_pnl),
        "expected_tokens": int(exp_tokens),
        "partial_tokens": int(par_tokens),
        "adverse_tokens": int(adv_tokens),
        "expected_sell_lams": int(exp_sell_lams),
        "partial_sell_lams": int(par_sell_lams),
        "adverse_sell_lams": int(adv_sell_lams),
        "expected_branch_outcome": exp_outcome,
        "partial_branch_outcome": par_outcome,
        "adverse_branch_outcome": adv_outcome,
        "pass": bool(pass_),
        "blocker": blocker,
        "guard_min_tokens": (
            int(guard_min_tokens) if guard_min_tokens is not None else None
        ),
    }

    if logger is not None:
        try:
            logger(
                f"PGG2-V47B-GUARDED-BRANCH mint={_short(mint_for_log)} "
                f"size={float(size_sol):.4f} "
                f"exp_pnl={out['expected_pnl']:+.6f} "
                f"par_pnl={out['partial_pnl']:+.6f} "
                f"adv_pnl={out['adverse_pnl']:+.6f} "
                f"exp_tok={out['expected_tokens']} "
                f"adv_tok={out['adverse_tokens']} "
                f"exp={out['expected_branch_outcome']} "
                f"par={out['partial_branch_outcome']} "
                f"adv={out['adverse_branch_outcome']} "
                f"pass={out['pass']} blocker={out['blocker']}"
            )
        except Exception:
            pass

    return out


__all__ = [
    "simulate_branches",
    "BRANCH_WIN",
    "BRANCH_SAFE_BUY_FAIL",
    "BRANCH_UNSAFE_OPEN",
    "BRANCH_UNKNOWN",
]
