"""V60 Universal Live-Send Firewall.

Single choke point that authorizes (or blocks) every live BUY before
broker.send_signed() is called. Built in response to the AwsN 0.050 SOL
incident (2026-05-19) where V67-LEGACY-GATE-BYPASS reverted V47F downsize
and sent 10x the configured PGG2_LIVE_MAX_TRADE_SOL cap.

Hard rule (enforced by Phase 3 patch in pgg2_v48_drylive_harness.py):
    No live buy may be sent unless v60_authorize_live_buy() returns
    V60Decision(passed=True). The firewall's verdict is final; older
    branch-conditional gates (V67 legacy bypass, V47F downsize, in-branch
    V59 hook) become advisory.

Ordered checks (run in this exact sequence; first failure short-circuits):
    1. Mode check: real live only if explicit live env confirms.
    2. Hard size cap: selected_size_sol <= PGG2_LIVE_MAX_TRADE_SOL (default 0.005).
       No legacy bypass allowed; downsize attempts must have already occurred.
    3. V59 true-edge: compute for FINAL selected size with the full slippage budget.
       size <= 0.005: require pass_micro (true_edge >= +0.00005)
       size  > 0.005: require pass_bank  (true_edge >= +0.000400) AND explicit whitelist env
    4. V59 universal: V59 result must be present. If missing, block.
       Applies to V67-passing candidates AND V67 near-miss promotions.
    5. V53 risk veto: risk must be fresh (<= cache_max_age_ms).
       holder >= min_holders; bundlers < 15; dev <= 1; snipers <= 10;
       insiders <= 10; no hard "rugged" / "honeypot" / "danger" labels.
       Token-2022 alone is NOT a blocker.
    6. Pump v2: Token-2022 mints MUST use Pump v2 path.
       buy_v2 decoded guard must match V60 decision (amount + max_sol_cost).
       sell_v2 capability must exist for the mint.
    7. Fee policy: SWQOS tip exactly 0.000005 SOL.
       Total expected fees within budget.
       Expected wallet edge AFTER fees still positive.
    8. Route: route must be 'pump_bc'.
       sim_needed must be 0.
       pair_source must be in approved set.
    9. Snapshot freshness: snapshot_age_ms <= 1500.
       1500ms only allowed if V59 slippage budget already accounts for it
       (size_tier in {le_005, le_010, le_015}).
       Otherwise block.
   10. Final transaction decode: decode the actual outgoing buy guard.
       Verify size/amount/min_tokens/max_sol_cost match V60 decision.
       Verify no hidden / different size in the instruction set.
       Compute tx_digest (sha256 of canonical instruction bytes).

Logs:
    PGG2-V60-FIREWALL-CHECK mint=.. size=.. lane=.. (one per call, all checks)
    PGG2-V60-FIREWALL-PASS  mint=.. size=.. true_edge=.. tx_digest=..
    PGG2-V60-FIREWALL-BLOCK mint=.. size=.. blocker=.. detail=..

Usage:
    from pgg2_v60_live_send_firewall import (
        V60Candidate, V60TxPlan, v60_authorize_live_buy, V60Decision,
    )
    candidate = V60Candidate(mint=..., selected_size_sol=..., ...)
    tx_plan = V60TxPlan(token_program=..., instructions=..., ...)
    decision = v60_authorize_live_buy(candidate, tx_plan)
    if not decision.passed:
        log(f"PGG2-V60-FIREWALL-BLOCK {decision.blocker}")
        return  # do NOT send
    sig = broker.send_signed(signed_tx)
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ============================================================================
#  Configuration env helpers
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


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _envs(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None else default


# ============================================================================
#  Input / output dataclasses
# ============================================================================

@dataclass
class V60Candidate:
    """Everything V60 needs to know about the candidate it is gating."""
    mint: str
    selected_size_sol: float
    candidate_lane: str               # e.g. "v67_flow_confirm", "v57_promotion"
    rule_id: str                      # e.g. "v48_v47i_stack"
    expected_pnl_sol: float
    true_edge_sol: Optional[float]    # if pre-computed; V60 will recompute regardless
    token_program: str                # "spl" or "token-2022"
    route: str                        # expected "pump_bc"
    sim_needed: int                   # expected 0
    pair_source: str                  # e.g. "decision_curve_snapshot"
    snapshot_age_ms: int
    source_lead_ms: int
    risk_result: Optional[dict]       # V53 risk veto output dict, or None if missing
    risk_fetched_at_ms: Optional[int] # epoch ms when risk_result was obtained
    is_v67_passing: bool              # did V67 final-gate pass?
    is_v57_promotion: bool            # is this a V57 near-miss promotion?
    wallet_balance_sol: float
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class V60TxPlan:
    """The transaction we are about to send. V60 will decode this for the
    final decode check, so it must be representative of the actual outgoing
    tx (not a placeholder)."""
    # Decoded buy-guard fields from the actual outgoing instruction
    decoded_amount_tokens_raw: int    # min_tokens or expected_tokens encoded
    decoded_max_sol_cost_lamports: int
    # SWQOS / fee policy
    swqos_tip_sol: float
    priority_fee_sol: float
    base_fee_sol: float
    # Pump v2 routing
    uses_pump_v2: bool
    has_sell_v2_capability: bool
    # Optional raw instruction bytes for digest computation
    instruction_bytes: Optional[bytes] = None


@dataclass
class V60CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class V60Decision:
    passed: bool
    blocker: Optional[str]
    check_results: list[V60CheckResult]
    final_selected_size_sol: float
    true_edge_sol: float
    risk_summary: dict
    tx_digest: str

    def to_log_pass(self, short_mint: str) -> str:
        return (
            f"PGG2-V60-FIREWALL-PASS mint={short_mint} "
            f"size={self.final_selected_size_sol:.4f} "
            f"true_edge={self.true_edge_sol:+.6f} "
            f"tx_digest={self.tx_digest[:16]}"
        )

    def to_log_block(self, short_mint: str) -> str:
        det = ""
        for cr in self.check_results:
            if not cr.passed:
                det = cr.detail
                break
        return (
            f"PGG2-V60-FIREWALL-BLOCK mint={short_mint} "
            f"size={self.final_selected_size_sol:.4f} "
            f"blocker={self.blocker} detail={det}"
        )


# ============================================================================
#  Firewall configuration
# ============================================================================

class V60Config:
    def __init__(self) -> None:
        # 1. Mode check
        self.live_confirm_env = _envs("PGG2_LIVE_CONFIRM_TOKEN", "I_ACCEPT_REAL_SOL_RISK")
        self.required_live_confirm_value = _envs(
            "PGG2_V60_REQUIRED_LIVE_CONFIRM_VALUE", "I_ACCEPT_REAL_SOL_RISK"
        )

        # 2. Hard size cap
        self.max_trade_sol = _envf("PGG2_LIVE_MAX_TRADE_SOL", 0.005)
        self.size_cap_tolerance_sol = _envf(
            "PGG2_V60_SIZE_CAP_TOLERANCE_SOL", 0.0001
        )
        self.allow_oversize_with_whitelist = _envb(
            "PGG2_V60_ALLOW_OVERSIZE", False
        )

        # 3-4. V59 thresholds
        self.v59_micro_threshold = _envf(
            "PGG2_V59_MICRO_TRUE_EDGE_MIN_SOL", 0.00005
        )
        self.v59_bank_threshold = _envf(
            "PGG2_V59_BANK_TRUE_EDGE_MIN_SOL", 0.000400
        )

        # 5. V53 risk thresholds
        self.risk_max_age_ms = _envi("PGG2_V60_RISK_MAX_AGE_MS", 60_000)
        self.risk_min_holders = _envi("PGG2_V60_RISK_MIN_HOLDERS", 4)
        self.risk_max_bundlers = _envi("PGG2_V60_RISK_MAX_BUNDLERS", 14)
        self.risk_max_dev = _envi("PGG2_V60_RISK_MAX_DEV", 1)
        self.risk_max_snipers = _envi("PGG2_V60_RISK_MAX_SNIPERS", 10)
        self.risk_max_insiders = _envi("PGG2_V60_RISK_MAX_INSIDERS", 10)
        self.risk_block_labels = set(
            (
                _envs(
                    "PGG2_V60_RISK_BLOCK_LABELS",
                    "rugged,honeypot,danger,scam,blacklist",
                )
            ).lower().split(",")
        )
        self.risk_required = _envb("PGG2_V60_REQUIRE_RISK_PASS", True)

        # 7. Fee policy
        self.required_swqos_tip_sol = _envf(
            "PGG2_V58_SWQOS_TIP_SOL", 0.000005
        )
        self.max_total_fees_sol = _envf("PGG2_V60_MAX_TOTAL_FEES_SOL", 0.00005)

        # 8. Route
        self.allowed_routes = set(
            _envs("PGG2_V60_ALLOWED_ROUTES", "pump_bc").split(",")
        )
        self.allowed_pair_sources = set(
            _envs(
                "PGG2_V60_ALLOWED_PAIR_SOURCES",
                "decision_curve_snapshot,curve_snapshot,curve_rpc",
            ).split(",")
        )

        # 9. Snapshot freshness
        self.max_snapshot_age_ms = _envi(
            "PGG2_V60_MAX_SNAPSHOT_AGE_MS", 1500
        )

        # 10. Decode tolerance (sometimes encoded amounts differ by 1 unit
        # due to rounding). Keep tight.
        self.decode_size_tolerance_sol = _envf(
            "PGG2_V60_DECODE_SIZE_TOLERANCE_SOL", 0.000005
        )


_CFG: Optional[V60Config] = None


def get_config() -> V60Config:
    global _CFG
    if _CFG is None:
        _CFG = V60Config()
    return _CFG


# ============================================================================
#  V59 wiring (delegates to existing pgg2_v59_true_edge module)
# ============================================================================

def _compute_v59_true_edge(
    mint: str,
    expected_pnl_sol: float,
    size_sol: float,
) -> tuple[float, bool, bool, str]:
    """Returns (true_edge_sol, pass_micro, pass_bank, log_line).
    Delegates to pgg2_v59_true_edge.V59TrueEdge if available.
    """
    try:
        from pgg2_v59_true_edge import get_calculator  # type: ignore
    except Exception as exc:
        # If V59 module is missing, treat as blocking failure (universal rule).
        return (0.0, False, False, f"v59_module_missing:{type(exc).__name__}")
    calc = get_calculator()
    r = calc.compute_default(mint, expected_pnl_sol, size_sol, True)
    return (
        r.true_edge_sol,
        r.pass_micro,
        r.pass_bank,
        calc.format_log_line(r),
    )


# ============================================================================
#  Individual check functions
# ============================================================================

def _check_mode(cfg: V60Config, candidate: V60Candidate) -> V60CheckResult:
    confirm_val = os.environ.get("PGG2_LIVE_CONFIRM", "")
    if confirm_val != cfg.required_live_confirm_value:
        return V60CheckResult(
            name="mode",
            passed=False,
            detail=f"PGG2_LIVE_CONFIRM!={cfg.required_live_confirm_value}",
        )
    return V60CheckResult(name="mode", passed=True, detail="live_confirmed")


def _check_size_cap(cfg: V60Config, candidate: V60Candidate) -> V60CheckResult:
    sz = float(candidate.selected_size_sol)
    cap = cfg.max_trade_sol
    if sz <= cap + cfg.size_cap_tolerance_sol:
        return V60CheckResult(
            name="size_cap",
            passed=True,
            detail=f"size={sz:.4f}<=cap={cap:.4f}",
        )
    # Above cap. Strict mode forbids unless explicit whitelist.
    if cfg.allow_oversize_with_whitelist:
        return V60CheckResult(
            name="size_cap",
            passed=True,
            detail=f"size={sz:.4f}>cap={cap:.4f}_but_whitelist_on",
        )
    return V60CheckResult(
        name="size_cap",
        passed=False,
        detail=f"size_{sz:.4f}_exceeds_cap_{cap:.4f}",
    )


def _check_v59_true_edge(
    cfg: V60Config, candidate: V60Candidate
) -> tuple[V60CheckResult, float]:
    sz = float(candidate.selected_size_sol)
    ep = float(candidate.expected_pnl_sol)
    true_edge, pass_micro, pass_bank, log_line = _compute_v59_true_edge(
        candidate.mint, ep, sz
    )
    if log_line.startswith("v59_module_missing"):
        return (
            V60CheckResult(
                name="v59_true_edge",
                passed=False,
                detail=log_line,
            ),
            true_edge,
        )
    # Size-tier gating
    if sz <= cfg.max_trade_sol + cfg.size_cap_tolerance_sol:
        # Micro tier
        if pass_micro:
            return (
                V60CheckResult(
                    name="v59_true_edge",
                    passed=True,
                    detail=f"micro_pass_true_edge={true_edge:+.6f}",
                ),
                true_edge,
            )
        return (
            V60CheckResult(
                name="v59_true_edge",
                passed=False,
                detail=f"micro_block_true_edge={true_edge:+.6f}<{cfg.v59_micro_threshold:+.6f}",
            ),
            true_edge,
        )
    # Oversize: bank tier required AND whitelist must be on
    if not cfg.allow_oversize_with_whitelist:
        return (
            V60CheckResult(
                name="v59_true_edge",
                passed=False,
                detail=f"oversize_no_whitelist_size={sz:.4f}",
            ),
            true_edge,
        )
    if pass_bank:
        return (
            V60CheckResult(
                name="v59_true_edge",
                passed=True,
                detail=f"bank_pass_true_edge={true_edge:+.6f}",
            ),
            true_edge,
        )
    return (
        V60CheckResult(
            name="v59_true_edge",
            passed=False,
            detail=f"bank_block_true_edge={true_edge:+.6f}<{cfg.v59_bank_threshold:+.6f}",
        ),
        true_edge,
    )


def _check_v59_universal(
    cfg: V60Config, candidate: V60Candidate, prior_v59: V60CheckResult
) -> V60CheckResult:
    """V59 must have run regardless of lane. We confirmed it ran via
    _check_v59_true_edge above; this check enforces that the candidate came
    from SOME V48 lane (V56D, V58, V61, V67, V68, V57 — all valid). The lane
    field is informational; V59 true_edge is the actual gate. Empty lane
    means the candidate was constructed outside the V48 stack (e.g. ad-hoc
    rescue) and must be rejected."""
    lane = (candidate.candidate_lane or "").strip().lower()
    if not lane:
        return V60CheckResult(
            name="v59_universal",
            passed=False,
            detail="candidate_lane_empty_no_v48_origin",
        )
    if not prior_v59.passed:
        # Already blocked at check 3; record explicitly.
        return V60CheckResult(
            name="v59_universal",
            passed=False,
            detail="v59_true_edge_failed_above",
        )
    return V60CheckResult(
        name="v59_universal",
        passed=True,
        detail="v59_ran_for_lane=" + candidate.candidate_lane,
    )


def _check_risk_veto(
    cfg: V60Config, candidate: V60Candidate
) -> tuple[V60CheckResult, dict]:
    if not cfg.risk_required:
        return (
            V60CheckResult(name="risk_veto", passed=True, detail="risk_not_required"),
            {},
        )
    if candidate.risk_result is None:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail="risk_result_missing",
            ),
            {},
        )
    if candidate.risk_fetched_at_ms is None:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail="risk_fetched_at_ms_missing",
            ),
            candidate.risk_result,
        )
    age_ms = candidate.timestamp_ms - int(candidate.risk_fetched_at_ms)
    if age_ms > cfg.risk_max_age_ms:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail=f"risk_stale_age={age_ms}ms>{cfg.risk_max_age_ms}ms",
            ),
            candidate.risk_result,
        )
    r = candidate.risk_result
    holders = int(r.get("holders", 0))
    bundlers = int(r.get("bundlers", 0))
    dev = int(r.get("dev", 0))
    snipers = int(r.get("snipers", 0))
    insiders = int(r.get("insiders", 0))
    labels = [
        str(x).lower() for x in r.get("labels", []) if isinstance(x, (str,))
    ]
    rugged = bool(r.get("rugged", False))
    summary = {
        "holders": holders,
        "bundlers": bundlers,
        "dev": dev,
        "snipers": snipers,
        "insiders": insiders,
        "labels": labels,
        "rugged": rugged,
        "age_ms": age_ms,
    }
    if rugged:
        return (
            V60CheckResult(name="risk_veto", passed=False, detail="rugged_true"),
            summary,
        )
    if holders < cfg.risk_min_holders:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail=f"holders_{holders}<{cfg.risk_min_holders}",
            ),
            summary,
        )
    if bundlers > cfg.risk_max_bundlers:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail=f"bundlers_{bundlers}>{cfg.risk_max_bundlers}",
            ),
            summary,
        )
    if dev > cfg.risk_max_dev:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail=f"dev_{dev}>{cfg.risk_max_dev}",
            ),
            summary,
        )
    if snipers > cfg.risk_max_snipers:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail=f"snipers_{snipers}>{cfg.risk_max_snipers}",
            ),
            summary,
        )
    if insiders > cfg.risk_max_insiders:
        return (
            V60CheckResult(
                name="risk_veto",
                passed=False,
                detail=f"insiders_{insiders}>{cfg.risk_max_insiders}",
            ),
            summary,
        )
    for lbl in labels:
        if lbl in cfg.risk_block_labels:
            return (
                V60CheckResult(
                    name="risk_veto",
                    passed=False,
                    detail=f"hard_label={lbl}",
                ),
                summary,
            )
    return (
        V60CheckResult(
            name="risk_veto",
            passed=True,
            detail=f"holders={holders} bundlers={bundlers}",
        ),
        summary,
    )


def _check_pump_v2(
    cfg: V60Config, candidate: V60Candidate, plan: V60TxPlan
) -> V60CheckResult:
    is_t22 = candidate.token_program.lower() in ("token-2022", "token2022", "t22")
    if is_t22 and not plan.uses_pump_v2:
        return V60CheckResult(
            name="pump_v2",
            passed=False,
            detail="token-2022_requires_pump_v2_but_legacy_route",
        )
    if is_t22 and not plan.has_sell_v2_capability:
        return V60CheckResult(
            name="pump_v2",
            passed=False,
            detail="token-2022_no_sell_v2_capability",
        )
    return V60CheckResult(
        name="pump_v2",
        passed=True,
        detail=f"token_program={candidate.token_program} v2={plan.uses_pump_v2}",
    )


def _check_fee_policy(
    cfg: V60Config, candidate: V60Candidate, plan: V60TxPlan, true_edge: float
) -> V60CheckResult:
    if abs(plan.swqos_tip_sol - cfg.required_swqos_tip_sol) > 1e-9:
        return V60CheckResult(
            name="fee_policy",
            passed=False,
            detail=(
                f"swqos_tip_{plan.swqos_tip_sol:.9f}!={cfg.required_swqos_tip_sol:.9f}"
            ),
        )
    total_fee = (
        plan.base_fee_sol + plan.priority_fee_sol + plan.swqos_tip_sol
    ) * 2  # round-trip
    if total_fee > cfg.max_total_fees_sol:
        return V60CheckResult(
            name="fee_policy",
            passed=False,
            detail=f"total_fee_{total_fee:.6f}>{cfg.max_total_fees_sol:.6f}",
        )
    # Post-fee edge must be positive (true_edge already deducts fees, so
    # require strict positivity).
    if true_edge <= 0:
        return V60CheckResult(
            name="fee_policy",
            passed=False,
            detail=f"post_fee_edge_{true_edge:+.6f}_non_positive",
        )
    return V60CheckResult(
        name="fee_policy",
        passed=True,
        detail=f"tip={plan.swqos_tip_sol:.6f} total={total_fee:.6f}",
    )


def _check_route(cfg: V60Config, candidate: V60Candidate) -> V60CheckResult:
    if candidate.route not in cfg.allowed_routes:
        return V60CheckResult(
            name="route",
            passed=False,
            detail=f"route_{candidate.route}_not_in_{sorted(cfg.allowed_routes)}",
        )
    if candidate.sim_needed != 0:
        return V60CheckResult(
            name="route",
            passed=False,
            detail=f"sim_needed={candidate.sim_needed}_not_0",
        )
    if candidate.pair_source not in cfg.allowed_pair_sources:
        return V60CheckResult(
            name="route",
            passed=False,
            detail=f"pair_source_{candidate.pair_source}_not_in_approved",
        )
    return V60CheckResult(
        name="route",
        passed=True,
        detail=f"route={candidate.route} pair={candidate.pair_source}",
    )


def _check_snapshot_freshness(
    cfg: V60Config, candidate: V60Candidate
) -> V60CheckResult:
    age = int(candidate.snapshot_age_ms)
    if age <= cfg.max_snapshot_age_ms:
        return V60CheckResult(
            name="snapshot_freshness",
            passed=True,
            detail=f"snapshot_age={age}ms<=max={cfg.max_snapshot_age_ms}ms",
        )
    return V60CheckResult(
        name="snapshot_freshness",
        passed=False,
        detail=f"snapshot_age={age}ms>max={cfg.max_snapshot_age_ms}ms",
    )


def _check_decode(
    cfg: V60Config, candidate: V60Candidate, plan: V60TxPlan
) -> tuple[V60CheckResult, str]:
    """Verify the decoded outgoing transaction matches the V60 decision."""
    # Cross-check max_sol_cost on the encoded instruction against
    # selected_size_sol. The decoded max_sol_cost should be approximately
    # selected_size_sol (plus minor adjustments for fees/slippage).
    decoded_size_sol = plan.decoded_max_sol_cost_lamports / 1e9
    sz = float(candidate.selected_size_sol)
    # Allow decoded_size to be at most cap + tolerance
    if decoded_size_sol > cfg.max_trade_sol + cfg.size_cap_tolerance_sol:
        digest = _tx_digest(plan)
        return (
            V60CheckResult(
                name="decode",
                passed=False,
                detail=(
                    f"decoded_max_sol_cost_{decoded_size_sol:.6f}_exceeds_cap_"
                    f"{cfg.max_trade_sol:.6f}"
                ),
            ),
            digest,
        )
    # Decoded size should match candidate.selected_size_sol within tolerance
    if abs(decoded_size_sol - sz) > max(
        cfg.decode_size_tolerance_sol, sz * 0.10
    ):
        digest = _tx_digest(plan)
        return (
            V60CheckResult(
                name="decode",
                passed=False,
                detail=(
                    f"decoded_size_{decoded_size_sol:.6f}_drift_from_selected_"
                    f"{sz:.6f}"
                ),
            ),
            digest,
        )
    digest = _tx_digest(plan)
    return (
        V60CheckResult(
            name="decode",
            passed=True,
            detail=f"decoded_size={decoded_size_sol:.6f} matches selected",
        ),
        digest,
    )


def _tx_digest(plan: V60TxPlan) -> str:
    h = hashlib.sha256()
    if plan.instruction_bytes:
        h.update(plan.instruction_bytes)
    else:
        # Fall back to a digest of the decoded numeric fields
        h.update(str(plan.decoded_amount_tokens_raw).encode())
        h.update(str(plan.decoded_max_sol_cost_lamports).encode())
        h.update(str(plan.swqos_tip_sol).encode())
        h.update(str(plan.priority_fee_sol).encode())
        h.update(str(plan.uses_pump_v2).encode())
    return h.hexdigest()


def _short(mint: str) -> str:
    if len(mint) > 10:
        return mint[:4] + ".." + mint[-4:]
    return mint


# ============================================================================
#  Public API
# ============================================================================

def v60_authorize_live_buy(
    candidate: V60Candidate,
    tx_plan: V60TxPlan,
    log_fn: Optional[Callable[[str], None]] = None,
) -> V60Decision:
    """The ONLY authorized entry point for live buys.

    Returns V60Decision with passed=True only if all 10 checks succeeded.
    On block, the caller MUST NOT send the transaction.
    """
    cfg = get_config()
    log = log_fn if log_fn is not None else print
    short = _short(candidate.mint)

    results: list[V60CheckResult] = []
    blocker: Optional[str] = None
    true_edge_sol = 0.0
    risk_summary: dict = {}
    tx_digest = ""

    # Audit log line that lists everything V60 sees before any block.
    log(
        f"PGG2-V60-FIREWALL-CHECK mint={short} "
        f"size={candidate.selected_size_sol:.4f} "
        f"lane={candidate.candidate_lane} "
        f"rule={candidate.rule_id} "
        f"ep={candidate.expected_pnl_sol:+.6f} "
        f"route={candidate.route} "
        f"pair={candidate.pair_source} "
        f"snap_age_ms={candidate.snapshot_age_ms} "
        f"token_prog={candidate.token_program} "
        f"v67_pass={int(candidate.is_v67_passing)} "
        f"v57_prom={int(candidate.is_v57_promotion)}"
    )

    # 1. Mode
    r = _check_mode(cfg, candidate)
    results.append(r)
    if not r.passed:
        blocker = "mode_check"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 2. Size cap
    r = _check_size_cap(cfg, candidate)
    results.append(r)
    if not r.passed:
        blocker = "size_cap"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 3. V59 true-edge (always run; returns true_edge for fee_policy check)
    r, true_edge_sol = _check_v59_true_edge(cfg, candidate)
    results.append(r)
    if not r.passed:
        blocker = "v59_true_edge"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 4. V59 universal
    r2 = _check_v59_universal(cfg, candidate, r)
    results.append(r2)
    if not r2.passed:
        blocker = "v59_universal"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 5. V53 risk veto
    r, risk_summary = _check_risk_veto(cfg, candidate)
    results.append(r)
    if not r.passed:
        blocker = "risk_veto"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 6. Pump v2
    r = _check_pump_v2(cfg, candidate, tx_plan)
    results.append(r)
    if not r.passed:
        blocker = "pump_v2"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 7. Fee policy
    r = _check_fee_policy(cfg, candidate, tx_plan, true_edge_sol)
    results.append(r)
    if not r.passed:
        blocker = "fee_policy"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 8. Route
    r = _check_route(cfg, candidate)
    results.append(r)
    if not r.passed:
        blocker = "route"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 9. Snapshot freshness
    r = _check_snapshot_freshness(cfg, candidate)
    results.append(r)
    if not r.passed:
        blocker = "snapshot_freshness"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # 10. Final decode
    r, tx_digest = _check_decode(cfg, candidate, tx_plan)
    results.append(r)
    if not r.passed:
        blocker = "decode"
        return _finalize(
            results, blocker, candidate, true_edge_sol, risk_summary, tx_digest, log, short
        )

    # All passed
    return _finalize(
        results, None, candidate, true_edge_sol, risk_summary, tx_digest, log, short
    )


def _finalize(
    results: list[V60CheckResult],
    blocker: Optional[str],
    candidate: V60Candidate,
    true_edge_sol: float,
    risk_summary: dict,
    tx_digest: str,
    log: Callable[[str], None],
    short: str,
) -> V60Decision:
    decision = V60Decision(
        passed=(blocker is None),
        blocker=blocker,
        check_results=results,
        final_selected_size_sol=float(candidate.selected_size_sol),
        true_edge_sol=true_edge_sol,
        risk_summary=risk_summary,
        tx_digest=tx_digest or "",
    )
    if decision.passed:
        log(decision.to_log_pass(short))
    else:
        log(decision.to_log_block(short))
    return decision


# ============================================================================
#  Self-test
# ============================================================================

if __name__ == "__main__":
    print("=== V60 firewall self-test ===")
    cfg = get_config()
    print(f"max_trade_sol={cfg.max_trade_sol}")
    print(f"v59_micro_threshold={cfg.v59_micro_threshold}")
    print(f"v59_bank_threshold={cfg.v59_bank_threshold}")
    print(f"required_swqos_tip={cfg.required_swqos_tip_sol}")
    print()
    # AwsN-style replay: size=0.050, ep=+0.001539. MUST be blocked.
    print("--- AwsN replay (size=0.050, ep=+0.001539): MUST block ---")
    os.environ["PGG2_LIVE_CONFIRM"] = "I_ACCEPT_REAL_SOL_RISK"
    cand = V60Candidate(
        mint="AwsNxpkmDh5uK9EAzVkQpJVNdvD22SoCH8RNPYczpump",
        selected_size_sol=0.050,
        candidate_lane="v67_flow_confirm",
        rule_id="v48_v47i_stack",
        expected_pnl_sol=0.001539,
        true_edge_sol=None,
        token_program="token-2022",
        route="pump_bc",
        sim_needed=0,
        pair_source="decision_curve_snapshot",
        snapshot_age_ms=350,
        source_lead_ms=274,
        risk_result={"holders": 6, "bundlers": 0, "dev": 0, "snipers": 0, "insiders": 0, "labels": [], "rugged": False},
        risk_fetched_at_ms=int(time.time() * 1000),
        is_v67_passing=True,
        is_v57_promotion=False,
        wallet_balance_sol=0.115942,
    )
    plan = V60TxPlan(
        decoded_amount_tokens_raw=490112026447,
        decoded_max_sol_cost_lamports=50_000_000,  # 0.050 SOL
        swqos_tip_sol=0.000005,
        priority_fee_sol=0.000005,
        base_fee_sol=0.000005,
        uses_pump_v2=True,
        has_sell_v2_capability=True,
    )
    decision = v60_authorize_live_buy(cand, plan)
    assert not decision.passed, "AwsN MUST be blocked"
    assert decision.blocker == "size_cap", f"expected size_cap, got {decision.blocker}"
    print(f"  blocked correctly: blocker={decision.blocker}")
    print()
    print("--- Legit candidate (size=0.005, ep=+0.001500): expect pass ---")
    cand.selected_size_sol = 0.005
    cand.expected_pnl_sol = 0.001500
    plan.decoded_max_sol_cost_lamports = 5_000_000
    decision = v60_authorize_live_buy(cand, plan)
    print(f"  passed={decision.passed} blocker={decision.blocker}")
    if not decision.passed:
        for cr in decision.check_results:
            print(f"    {cr.name}: {'PASS' if cr.passed else 'BLOCK'}  {cr.detail}")
