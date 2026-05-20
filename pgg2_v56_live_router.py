"""V56 live router shim.

Chains: V47/V48 candidate -> V56 momentum gate -> V53 risk veto -> Pump v2
        routing -> existing v50b SWQOS execution path.

Integration model: rather than rewriting v50b (57 KB), this shim provides a
single `check_and_route(candidate)` function that v50b can call AFTER the
v48 harness emits a candidate but BEFORE the SWQOS send. The shim returns a
RouterDecision saying either:
  - GO: with path={"v1"|"v2"}, accounts (for v2), or fall-through (for v1)
  - BLOCK: with stage and blocker reason

The v50b runner only needs ONE line: `if not router.check_and_route(...).go: skip`.

Log lines emitted:
  - PGG2-V56-LIVE-ROUTER-CANDIDATE  (when called)
  - PGG2-V56-LIVE-ROUTER-BLOCK      (when blocked, with stage+reason)
  - PGG2-V56-LIVE-ROUTER-SEND       (when greenlight to send)
  - PGG2-V56-LIVE-ROUTER-CLOSE      (called post-close by v50b)
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pgg2_v56_live_momentum_gate import (
    MomentumSnapshot,
    MomentumResult,
    get_gate as get_momentum_gate,
)
from pgg2_v56_risk_veto import (
    RiskCheckResult,
    get_veto as get_risk_veto,
)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class CandidateInput:
    """Inputs the router needs from the v48 harness candidate.

    The integration layer (v50b monkey-patch or v48 callback) fills these.
    """
    mint: str
    source: str  # must equal "v47_pending_flow" for live entry
    selected_size_sol: float
    expected_pnl_sol: float

    # For momentum gate (filled by v48 harness from V47I curve_history + sell quote)
    sell_quote_now_sol: float
    sell_quote_500ms_ago_sol: float
    curve_vsol_delta_500ms_sol: float
    pending_buy_count_500ms: int
    pending_buy_count_1000ms: int
    pending_buy_sol_500ms: float
    pending_buy_sol_1000ms: float

    # Optional metadata
    v47h_ratio: Optional[float] = None
    decision_ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class RouterDecision:
    mint: str
    go: bool
    stage_blocked: Optional[str]
    blocker: Optional[str]
    path: Optional[str]  # "v1" or "v2"
    is_token_2022: bool
    momentum_result: Optional[MomentumResult]
    risk_result: Optional[RiskCheckResult]
    fee_policy_ok: bool
    can_swqos: bool


class V56LiveRouter:
    def __init__(self) -> None:
        self.enabled = _envb("PGG2_V56_ROUTER_ENABLED", True)
        self.require_v47_source = _envb("PGG2_V56_ROUTER_REQUIRE_V47_SOURCE", True)
        self.allow_v1_fallback = _envb("PGG2_V56_ROUTER_ALLOW_V1_FALLBACK", True)
        # When False, T22 mints with no v2 path are blocked rather than
        # falling back to v1 (which would fail for T22 anyway).
        self.block_t22_without_v2 = _envb(
            "PGG2_V56_ROUTER_BLOCK_T22_WITHOUT_V2", False
        )
        # Optional helius/RPC URL for mint-owner lookup (for T22 detection).
        # If None, router treats every mint as classic SPL (safe default).
        self.helius_url = os.environ.get("HELIUS_API_KEY_URL") or os.environ.get(
            "PGG2_V56_ROUTER_RPC_URL", ""
        )
        if not self.helius_url and os.environ.get("HELIUS_API_KEY"):
            self.helius_url = (
                f"https://mainnet.helius-rpc.com/?api_key={os.environ['HELIUS_API_KEY']}"
            )

        self.momentum = get_momentum_gate()
        self.risk = get_risk_veto()

    # ---- internals ----

    def _emit(self, line: str, log_fn: Callable[[str], None]) -> None:
        log_fn(line)

    def _short_mint(self, mint: str) -> str:
        return mint[:4] + ".." + mint[-4:] if len(mint) > 10 else mint

    def _detect_t22(self, mint: str, log_fn: Callable[[str], None]) -> tuple[bool, str]:
        """Return (is_t22, detection_status).

        detection_status: "ok", "no_rpc", "rpc_err", "unknown"
        """
        if not self.helius_url:
            return False, "no_rpc"
        try:
            from solders.pubkey import Pubkey  # local import to keep module portable
            from pgg2_pump_v2_accounts import fetch_mint_owner
            owner = fetch_mint_owner(self.helius_url, Pubkey.from_string(mint))
            from pgg2_pump_v2_idl_constants import TOKEN_2022_PROGRAM_ID
            return owner == TOKEN_2022_PROGRAM_ID, "ok"
        except Exception as e:  # noqa: BLE001
            log_fn(f"PGG2-V56-ROUTER-T22-DETECT-ERR {self._short_mint(mint)} err={type(e).__name__}:{e}")
            return False, "rpc_err"

    def _can_route_v2(self, mint: str, user: str, log_fn: Callable[[str], None]) -> bool:
        """Check whether v2 accounts can be resolved for this (mint, user)."""
        try:
            from solders.pubkey import Pubkey
            from pgg2_pump_v2_accounts import resolve_v2_accounts_sol_paired
            if not self.helius_url:
                return False
            accounts = resolve_v2_accounts_sol_paired(
                self.helius_url,
                Pubkey.from_string(mint),
                Pubkey.from_string(user),
            )
            return bool(accounts)
        except Exception as e:  # noqa: BLE001
            log_fn(f"PGG2-V56-ROUTER-V2-RESOLVE-ERR {self._short_mint(mint)} err={type(e).__name__}:{e}")
            return False

    # ---- public ----

    def check_and_route(
        self,
        cand: CandidateInput,
        user_pubkey: str,
        log_fn: Callable[[str], None] = print,
    ) -> RouterDecision:
        sh = self._short_mint(cand.mint)
        self._emit(
            f"PGG2-V56-LIVE-ROUTER-CANDIDATE {sh} source={cand.source} "
            f"size={cand.selected_size_sol:.6f} exp_pnl={cand.expected_pnl_sol:+.6f}",
            log_fn,
        )

        if not self.enabled:
            self._emit(
                f"PGG2-V56-LIVE-ROUTER-BLOCK {sh} stage=disabled blocker=router_disabled",
                log_fn,
            )
            return RouterDecision(
                mint=cand.mint, go=False, stage_blocked="disabled",
                blocker="router_disabled", path=None, is_token_2022=False,
                momentum_result=None, risk_result=None,
                fee_policy_ok=False, can_swqos=False,
            )

        # Gate 1: source must be V47 pending-flow
        if self.require_v47_source and cand.source != "v47_pending_flow":
            self._emit(
                f"PGG2-V56-LIVE-ROUTER-BLOCK {sh} stage=source blocker=non_v47_source({cand.source})",
                log_fn,
            )
            return RouterDecision(
                mint=cand.mint, go=False, stage_blocked="source",
                blocker=f"non_v47_source({cand.source})", path=None,
                is_token_2022=False, momentum_result=None, risk_result=None,
                fee_policy_ok=False, can_swqos=False,
            )

        # Gate 2: momentum
        snap = MomentumSnapshot(
            mint=cand.mint,
            expected_pnl_sol=cand.expected_pnl_sol,
            sell_quote_now_sol=cand.sell_quote_now_sol,
            sell_quote_500ms_ago_sol=cand.sell_quote_500ms_ago_sol,
            curve_vsol_delta_500ms_sol=cand.curve_vsol_delta_500ms_sol,
            pending_buy_count_500ms=cand.pending_buy_count_500ms,
            pending_buy_count_1000ms=cand.pending_buy_count_1000ms,
            pending_buy_sol_500ms=cand.pending_buy_sol_500ms,
            pending_buy_sol_1000ms=cand.pending_buy_sol_1000ms,
            v47h_ratio=cand.v47h_ratio,
            trigger_ts_ms=cand.decision_ts_ms,
        )
        mom = self.momentum.check(snap)
        self._emit(self.momentum.format_log_line(snap, mom), log_fn)
        if not mom.pass_:
            self._emit(
                f"PGG2-V56-LIVE-ROUTER-BLOCK {sh} stage=momentum blocker={mom.blocker}",
                log_fn,
            )
            return RouterDecision(
                mint=cand.mint, go=False, stage_blocked="momentum",
                blocker=mom.blocker, path=None, is_token_2022=False,
                momentum_result=mom, risk_result=None,
                fee_policy_ok=False, can_swqos=False,
            )

        # Gate 3: V53 risk veto (uses SolanaTracker API budget — only call now)
        risk = self.risk.check(cand.mint)
        self._emit(self.risk.format_log_line(risk), log_fn)
        if not risk.pass_:
            self._emit(
                f"PGG2-V56-LIVE-ROUTER-BLOCK {sh} stage=risk blocker={risk.blocker}",
                log_fn,
            )
            return RouterDecision(
                mint=cand.mint, go=False, stage_blocked="risk",
                blocker=risk.blocker, path=None,
                is_token_2022=risk.is_token_2022,
                momentum_result=mom, risk_result=risk,
                fee_policy_ok=False, can_swqos=False,
            )

        # Gate 4: path routing (T22 -> v2, classic -> v1)
        is_t22 = risk.is_token_2022
        if is_t22:
            if self._can_route_v2(cand.mint, user_pubkey, log_fn):
                path = "v2"
            else:
                if self.block_t22_without_v2:
                    self._emit(
                        f"PGG2-V56-LIVE-ROUTER-BLOCK {sh} stage=path blocker=t22_v2_unavailable",
                        log_fn,
                    )
                    return RouterDecision(
                        mint=cand.mint, go=False, stage_blocked="path",
                        blocker="t22_v2_unavailable", path=None,
                        is_token_2022=True, momentum_result=mom, risk_result=risk,
                        fee_policy_ok=False, can_swqos=False,
                    )
                # Conservative: don't try v1 for T22 (it would fail on-chain)
                self._emit(
                    f"PGG2-V56-LIVE-ROUTER-BLOCK {sh} stage=path blocker=t22_no_v2_fallback",
                    log_fn,
                )
                return RouterDecision(
                    mint=cand.mint, go=False, stage_blocked="path",
                    blocker="t22_no_v2_fallback", path=None,
                    is_token_2022=True, momentum_result=mom, risk_result=risk,
                    fee_policy_ok=False, can_swqos=False,
                )
        else:
            path = "v1"

        # Gates 5-7 are owned by v50b (fee policy + SWQOS send). The router
        # asserts they're available but does not run them; the caller in v50b
        # proceeds to its existing SWQOS path.
        # We expose path so v50b can dispatch to either v1 direct_pump or v2 builder.

        self._emit(
            f"PGG2-V56-LIVE-ROUTER-SEND {sh} path={path} size={cand.selected_size_sol:.6f} "
            f"exp_pnl={cand.expected_pnl_sol:+.6f} holders={risk.holders} "
            f"bundlers_pct={risk.bundlers_pct:.1f} is_t22={int(is_t22)}",
            log_fn,
        )
        return RouterDecision(
            mint=cand.mint, go=True, stage_blocked=None, blocker=None,
            path=path, is_token_2022=is_t22,
            momentum_result=mom, risk_result=risk,
            fee_policy_ok=True, can_swqos=True,
        )

    def log_close(
        self,
        mint: str,
        path: str,
        pnl_sol: float,
        reason: str,
        wallet_delta_sol: float,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        sh = self._short_mint(mint)
        log_fn(
            f"PGG2-V56-LIVE-ROUTER-CLOSE {sh} path={path} "
            f"pnl={pnl_sol:+.6f} wallet_delta={wallet_delta_sol:+.6f} reason={reason}"
        )


_SINGLETON: Optional[V56LiveRouter] = None


def get_router() -> V56LiveRouter:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = V56LiveRouter()
    return _SINGLETON


if __name__ == "__main__":
    # Smoke test with two synthetic candidates: one should pass, one blocked.
    USER = "Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7"
    USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    router = get_router()

    print("=== Test 1: synthetic v47 candidate with USDC (clean, passes risk) ===")
    cand_pass = CandidateInput(
        mint=USDC, source="v47_pending_flow",
        selected_size_sol=0.005, expected_pnl_sol=0.0020,
        sell_quote_now_sol=0.00510, sell_quote_500ms_ago_sol=0.00500,
        curve_vsol_delta_500ms_sol=0.25,
        pending_buy_count_500ms=2, pending_buy_count_1000ms=5,
        pending_buy_sol_500ms=0.5, pending_buy_sol_1000ms=1.2,
    )
    d1 = router.check_and_route(cand_pass, USER)
    print(f"\nResult: go={d1.go} path={d1.path} blocker={d1.blocker}\n")

    print("=== Test 2: non-v47 source (should block at gate 1) ===")
    cand_block = CandidateInput(
        mint=USDC, source="score_gate",
        selected_size_sol=0.005, expected_pnl_sol=0.0020,
        sell_quote_now_sol=0.005, sell_quote_500ms_ago_sol=0.005,
        curve_vsol_delta_500ms_sol=0.0,
        pending_buy_count_500ms=0, pending_buy_count_1000ms=0,
        pending_buy_sol_500ms=0, pending_buy_sol_1000ms=0,
    )
    d2 = router.check_and_route(cand_block, USER)
    print(f"\nResult: go={d2.go} stage={d2.stage_blocked} blocker={d2.blocker}\n")

    print("=== Test 3: v47 source but PnL below fee hurdle (should block at gate 2) ===")
    cand_pnl_low = CandidateInput(
        mint=USDC, source="v47_pending_flow",
        selected_size_sol=0.005, expected_pnl_sol=0.0001,
        sell_quote_now_sol=0.005, sell_quote_500ms_ago_sol=0.005,
        curve_vsol_delta_500ms_sol=0.0,
        pending_buy_count_500ms=0, pending_buy_count_1000ms=0,
        pending_buy_sol_500ms=0, pending_buy_sol_1000ms=0,
    )
    d3 = router.check_and_route(cand_pnl_low, USER)
    print(f"\nResult: go={d3.go} stage={d3.stage_blocked} blocker={d3.blocker}")
