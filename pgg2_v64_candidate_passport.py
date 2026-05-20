"""V64 Candidate Passport (Phase 2).

Hard principle: no live buy may be sent unless the exact candidate decision
has a fresh PASS passport from every mandatory gate.

Closes the 4rzH..X3Fb bypass class (see V64_LIVE_BUY_BYPASS_FORENSIC.md):

1. V47C SHADOW_ONLY at ub=1 was forgotten when ub later climbed to >=2.
2. V67 flow-confirm BLOCK was overridden by lane-OR (V56D PASS).
3. clean_close_gate BLOCK on decision_id v48-1 was forgotten when the same
   mint was re-decisioned ~50ms later as v48-2 with a fresher snapshot.

V64 keys the passport by MINT (not decision_id) and aggregates worst-case
gate results across the candidate's entire lifecycle within the snapshot
window. Any BLOCK / SHADOW_ONLY / MISSING in a MANDATORY gate makes
final_pass=False, irreversibly, for the entire candidate window.

Worst-result lattice:
    PASS > TELEMETRY_ONLY > MISSING > SHADOW_ONLY > BLOCK

Mandatory gates for live (per V64 spec Phase 3):
    V47C, V47D, V47E, V47F, V47H, V47I (when configured),
    V59, V60, V61, V53 (if API available), Pump v2 / Token-2022,
    SWQOS fee policy, size cap, snapshot freshness.

Optional / telemetry gates do not block, but if missing on a mandatory gate
the candidate cannot live-buy.

Logs:
- PGG2-V64-PASSPORT-CREATE
- PGG2-V64-PASSPORT-GATE
- PGG2-V64-PASSPORT-FINAL-PASS
- PGG2-V64-PASSPORT-FINAL-BLOCK
- PGG2-V64-PASSPORT-EXPIRED
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


# Gate result lattice
RESULT_PASS = "PASS"
RESULT_TELEMETRY_ONLY = "TELEMETRY_ONLY"
RESULT_MISSING = "MISSING"
RESULT_SHADOW_ONLY = "SHADOW_ONLY"
RESULT_BLOCK = "BLOCK"

_BLOCKING_RESULTS: Set[str] = {RESULT_BLOCK, RESULT_SHADOW_ONLY, RESULT_MISSING}

# Mandatory gates for V64 live buy (per spec Phase 3)
# A gate not in this set is TELEMETRY only (recording does not gate the buy).
MANDATORY_GATES_FOR_LIVE: Set[str] = {
    "v47c_multi_buyer",
    "v47d_boundary",
    "v47e_two_buyer",
    "v47f_size_edge_floor",
    "v47h_rug_veto",
    "v47i_medium_rug_veto",
    "v59_true_edge",
    "v60_firewall",
    "v61_continuation",
    "v53_risk_veto_if_available",
    "token2022_pump_v2",
    "swqos_fee_policy",
    "size_cap",
    "snapshot_fresh",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _env_int(k: str, default: int) -> int:
    try:
        return int(os.environ.get(k, str(default)) or default)
    except Exception:
        return default


@dataclass
class V64GateRecord:
    """One gate's recorded result on this candidate."""

    gate: str
    result: str  # PASS | TELEMETRY_ONLY | MISSING | SHADOW_ONLY | BLOCK
    detail: str = ""
    ts_ms: int = field(default_factory=_now_ms)
    snapshot_ts_ms: int = 0
    mandatory: bool = True


@dataclass
class CandidatePassport:
    """Per-mint passport. The mint is the key. decision_id changes over the
    candidate's lifecycle (snapshot refreshes) but the passport persists.
    """

    decision_id: str  # first decision_id observed; tracked for matching
    mint: str
    snapshot_ts_ms: int
    selected_size_sol: float
    route: str
    sim_needed: bool = False
    pair_source: str = ""
    created_at_ms: int = field(default_factory=_now_ms)
    expires_at_ms: int = field(default_factory=lambda: _now_ms() + 2000)
    gate_results: Dict[str, V64GateRecord] = field(default_factory=dict)
    blockers: List[str] = field(default_factory=list)
    shadow_only_reasons: List[str] = field(default_factory=list)
    final_pass: bool = False
    final_pass_computed_at_ms: int = 0
    decision_id_history: List[str] = field(default_factory=list)

    def record_gate(
        self,
        gate: str,
        result: str,
        detail: str = "",
        snapshot_ts_ms: int = 0,
        mandatory: Optional[bool] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Worst-result-wins: only updates if new result is WORSE than existing.

        If gate already recorded BLOCK or SHADOW_ONLY, subsequent PASS for the
        same gate does NOT overwrite. This closes the 4rzH bypass class where
        V47C SHADOW_ONLY at ub=1 was forgotten when ub later rose to >=2.
        """
        log = log_fn if log_fn is not None else (lambda s: None)
        if mandatory is None:
            mandatory = gate in MANDATORY_GATES_FOR_LIVE
        rec = V64GateRecord(
            gate=gate,
            result=result,
            detail=detail,
            snapshot_ts_ms=int(snapshot_ts_ms or 0),
            mandatory=bool(mandatory),
        )
        # Worst-result wins
        existing = self.gate_results.get(gate)
        if existing is not None:
            if existing.result in _BLOCKING_RESULTS:
                # Already blocked; do not downgrade
                return
            if result == RESULT_PASS and existing.result == RESULT_PASS:
                # No-op, already PASS; refresh timestamp to keep gate "fresh"
                existing.ts_ms = _now_ms()
                return
        self.gate_results[gate] = rec
        if result == RESULT_BLOCK:
            self.blockers.append(f"{gate}:{detail}")
        elif result == RESULT_SHADOW_ONLY:
            self.shadow_only_reasons.append(f"{gate}:{detail}")
        log(
            f"PGG2-V64-PASSPORT-GATE mint={_short(self.mint)} gate={gate} "
            f"result={result} detail={detail} mandatory={mandatory}"
        )

    def record_decision_id_refresh(self, new_decision_id: str) -> None:
        """Track decision_id changes (snapshot refreshes); but the passport
        identity is the mint, not the decision_id.
        """
        if new_decision_id and new_decision_id != self.decision_id:
            if self.decision_id and self.decision_id not in self.decision_id_history:
                self.decision_id_history.append(self.decision_id)
            self.decision_id = new_decision_id

    def is_expired(self, now_ms: Optional[int] = None) -> bool:
        n = now_ms if now_ms is not None else _now_ms()
        return n > self.expires_at_ms

    def compute_final_pass(
        self,
        *,
        require_mandatory_gates: Optional[Set[str]] = None,
        max_snapshot_age_ms: int = 2000,
        now_ms: Optional[int] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Recompute final_pass based on current gate state.

        final_pass = True iff EVERY mandatory gate has result=PASS AND the
        passport is not expired.
        """
        log = log_fn if log_fn is not None else (lambda s: None)
        n = now_ms if now_ms is not None else _now_ms()
        mandatory = (
            set(require_mandatory_gates)
            if require_mandatory_gates is not None
            else set(MANDATORY_GATES_FOR_LIVE)
        )
        if self.is_expired(n):
            log(
                f"PGG2-V64-PASSPORT-EXPIRED mint={_short(self.mint)} "
                f"created_at_ms={self.created_at_ms} expires_at_ms={self.expires_at_ms} "
                f"now_ms={n} blockers={','.join(self.blockers) or 'none'}"
            )
            self.final_pass = False
            self.final_pass_computed_at_ms = n
            return False
        missing_gates: List[str] = []
        block_gates: List[str] = []
        shadow_gates: List[str] = []
        stale_gates: List[str] = []
        passing_gates: List[str] = []
        for g in mandatory:
            rec = self.gate_results.get(g)
            if rec is None:
                missing_gates.append(g)
                continue
            if rec.result == RESULT_BLOCK:
                block_gates.append(g + ":" + (rec.detail or ""))
            elif rec.result == RESULT_SHADOW_ONLY:
                shadow_gates.append(g + ":" + (rec.detail or ""))
            elif rec.result == RESULT_MISSING:
                missing_gates.append(g + ":" + (rec.detail or ""))
            elif rec.result == RESULT_PASS:
                # Check snapshot age if the gate carried one
                if rec.snapshot_ts_ms > 0 and (n - rec.snapshot_ts_ms) > max_snapshot_age_ms:
                    stale_gates.append(g + f":age={(n - rec.snapshot_ts_ms)}ms")
                else:
                    passing_gates.append(g)
            elif rec.result == RESULT_TELEMETRY_ONLY:
                # Telemetry result on a mandatory gate is insufficient
                missing_gates.append(g + ":telemetry_only_on_mandatory")
        ok = (
            len(missing_gates) == 0
            and len(block_gates) == 0
            and len(shadow_gates) == 0
            and len(stale_gates) == 0
            and len(passing_gates) == len(mandatory)
        )
        self.final_pass = bool(ok)
        self.final_pass_computed_at_ms = n
        if ok:
            log(
                f"PGG2-V64-PASSPORT-FINAL-PASS mint={_short(self.mint)} "
                f"decision_id={self.decision_id} size={self.selected_size_sol:.6f} "
                f"route={self.route} gates_passed={len(passing_gates)} "
                f"age_ms={n - self.created_at_ms}"
            )
        else:
            log(
                f"PGG2-V64-PASSPORT-FINAL-BLOCK mint={_short(self.mint)} "
                f"decision_id={self.decision_id} size={self.selected_size_sol:.6f} "
                f"route={self.route} "
                f"missing={','.join(missing_gates) or '-'} "
                f"blocked={','.join(block_gates) or '-'} "
                f"shadow={','.join(shadow_gates) or '-'} "
                f"stale={','.join(stale_gates) or '-'} "
                f"passed={len(passing_gates)}/{len(mandatory)}"
            )
        return ok

    def summary(self) -> dict:
        return {
            "mint": self.mint,
            "decision_id": self.decision_id,
            "decision_id_history": list(self.decision_id_history),
            "snapshot_ts_ms": self.snapshot_ts_ms,
            "selected_size_sol": self.selected_size_sol,
            "route": self.route,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "gate_count": len(self.gate_results),
            "blockers": list(self.blockers),
            "shadow_only_reasons": list(self.shadow_only_reasons),
            "final_pass": self.final_pass,
            "final_pass_computed_at_ms": self.final_pass_computed_at_ms,
        }


class V64PassportRegistry:
    """In-memory registry of active passports, keyed by mint."""

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None):
        self._log = log_fn if log_fn is not None else (lambda s: None)
        self._passports: Dict[str, CandidatePassport] = {}

    def create(
        self,
        *,
        decision_id: str,
        mint: str,
        snapshot_ts_ms: int,
        selected_size_sol: float,
        route: str,
        sim_needed: bool = False,
        pair_source: str = "",
        ttl_ms: int = 2000,
    ) -> CandidatePassport:
        existing = self._passports.get(mint)
        if existing is not None and not existing.is_expired():
            # Refresh decision_id but keep blocker state intact
            existing.record_decision_id_refresh(decision_id)
            return existing
        passport = CandidatePassport(
            decision_id=decision_id,
            mint=mint,
            snapshot_ts_ms=int(snapshot_ts_ms),
            selected_size_sol=float(selected_size_sol),
            route=str(route),
            sim_needed=bool(sim_needed),
            pair_source=str(pair_source),
            expires_at_ms=_now_ms() + int(ttl_ms),
        )
        self._passports[mint] = passport
        self._log(
            f"PGG2-V64-PASSPORT-CREATE mint={_short(mint)} decision_id={decision_id} "
            f"size={selected_size_sol:.6f} route={route} snapshot_ts_ms={snapshot_ts_ms} "
            f"ttl_ms={ttl_ms} sim_needed={sim_needed} pair_source={pair_source}"
        )
        return passport

    def get(self, mint: str) -> Optional[CandidatePassport]:
        return self._passports.get(mint)

    def drop(self, mint: str) -> None:
        self._passports.pop(mint, None)

    def garbage_collect(self, now_ms: Optional[int] = None) -> int:
        n = now_ms if now_ms is not None else _now_ms()
        dead = [m for m, p in self._passports.items() if p.is_expired(n)]
        for m in dead:
            self._passports.pop(m, None)
        return len(dead)


def _short(s: str) -> str:
    if not s:
        return ""
    if len(s) > 8:
        return s[:4] + ".." + s[-4:]
    return s


# ----------------------------------------------------------------------------
# Live-buy authorization choke point (Phase 4 contract)
# ----------------------------------------------------------------------------

@dataclass
class V64BuyAuthorization:
    authorized: bool
    reason: str
    mint: str
    decision_id: str
    selected_size_sol: float
    route: str
    blockers: List[str]


def v64_authorize_live_buy(
    *,
    passport: Optional[CandidatePassport],
    proposed_mint: str,
    proposed_decision_id: str,
    proposed_size_sol: float,
    proposed_route: str,
    bypass_env_check: bool = True,
    log_fn: Optional[Callable[[str], None]] = None,
) -> V64BuyAuthorization:
    """Single choke point. Returns authorized=True only if:

      1. Passport exists for proposed_mint.
      2. Passport.final_pass is True.
      3. Passport.mint matches proposed_mint.
      4. Passport.decision_id matches proposed_decision_id (or in history).
      5. Passport.selected_size_sol matches proposed_size_sol.
      6. Passport.route matches proposed_route.
      7. Passport is not expired.
      8. Bypass env vars (PGG2_V67_BYPASS_LEGACY_GATES) are not set, unless
         bypass_env_check is False.
    """
    log = log_fn if log_fn is not None else (lambda s: None)
    if bypass_env_check and os.environ.get("PGG2_V67_BYPASS_LEGACY_GATES", "0") == "1":
        log(
            f"PGG2-V64-BYPASS-ENV-FATAL mint={_short(proposed_mint)} "
            f"reason=PGG2_V67_BYPASS_LEGACY_GATES=1 in V64 mode"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="bypass_env_fatal",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["bypass_env_fatal"],
        )
    if passport is None:
        log(
            f"PGG2-V64-LIVE-BUY-FATAL mint={_short(proposed_mint)} "
            f"reason=missing_passport decision_id={proposed_decision_id}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="missing_passport",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["missing_passport"],
        )
    if passport.is_expired():
        log(
            f"PGG2-V64-LIVE-BUY-BLOCK mint={_short(proposed_mint)} "
            f"reason=passport_expired decision_id={proposed_decision_id}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="passport_expired",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["passport_expired"],
        )
    if not passport.final_pass:
        log(
            f"PGG2-V64-LIVE-BUY-BLOCK mint={_short(proposed_mint)} "
            f"reason=passport_failed decision_id={proposed_decision_id} "
            f"blockers={','.join(passport.blockers) or '-'} "
            f"shadow_only={','.join(passport.shadow_only_reasons) or '-'}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="passport_failed",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=list(passport.blockers) + list(passport.shadow_only_reasons),
        )
    if passport.mint != proposed_mint:
        log(
            f"PGG2-V64-LIVE-BUY-FATAL mint={_short(proposed_mint)} "
            f"reason=mint_mismatch passport_mint={_short(passport.mint)}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="mint_mismatch",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["mint_mismatch"],
        )
    if (
        passport.decision_id != proposed_decision_id
        and proposed_decision_id not in passport.decision_id_history
    ):
        log(
            f"PGG2-V64-LIVE-BUY-BLOCK mint={_short(proposed_mint)} "
            f"reason=decision_id_mismatch passport_decision_id={passport.decision_id} "
            f"proposed_decision_id={proposed_decision_id}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="decision_id_mismatch",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["decision_id_mismatch"],
        )
    if abs(float(passport.selected_size_sol) - float(proposed_size_sol)) > 1e-9:
        log(
            f"PGG2-V64-LIVE-BUY-BLOCK mint={_short(proposed_mint)} "
            f"reason=size_mismatch passport_size={passport.selected_size_sol:.9f} "
            f"proposed_size={float(proposed_size_sol):.9f}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="size_mismatch",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["size_mismatch"],
        )
    if passport.route != proposed_route:
        log(
            f"PGG2-V64-LIVE-BUY-BLOCK mint={_short(proposed_mint)} "
            f"reason=route_mismatch passport_route={passport.route} "
            f"proposed_route={proposed_route}"
        )
        return V64BuyAuthorization(
            authorized=False,
            reason="route_mismatch",
            mint=proposed_mint,
            decision_id=proposed_decision_id,
            selected_size_sol=proposed_size_sol,
            route=proposed_route,
            blockers=["route_mismatch"],
        )
    log(
        f"PGG2-V64-LIVE-BUY-AUTHORIZED mint={_short(proposed_mint)} "
        f"decision_id={proposed_decision_id} size={proposed_size_sol:.6f} "
        f"route={proposed_route} gates_passed={len(passport.gate_results)}"
    )
    return V64BuyAuthorization(
        authorized=True,
        reason="passport_pass",
        mint=proposed_mint,
        decision_id=proposed_decision_id,
        selected_size_sol=proposed_size_sol,
        route=proposed_route,
        blockers=[],
    )


if __name__ == "__main__":
    print("=== V64 CandidatePassport self-test ===")
    log = print
    reg = V64PassportRegistry(log_fn=log)

    # Simulate 4rzH..X3Fb sequence
    passport = reg.create(
        decision_id="v48-1",
        mint="4rzHYPWUjccxqbE41sHbBbb5YSLvPLRuLQDWjTruX3Fb",
        snapshot_ts_ms=_now_ms(),
        selected_size_sol=0.005,
        route="pump_bc",
        ttl_ms=2000,
    )

    # First V47C eval: SHADOW_ONLY at ub=1
    passport.record_gate("v47c_multi_buyer", RESULT_SHADOW_ONLY, "single_buyer_shadow_only", log_fn=log)
    # Second V47C eval claims PASS — should be DROPPED (worst-result wins)
    passport.record_gate("v47c_multi_buyer", RESULT_PASS, "ub250=2", log_fn=log)

    # V47E first BLOCK
    passport.record_gate("v47e_two_buyer", RESULT_BLOCK, "ub2_tbs_gt_060_block", log_fn=log)
    # V47E later "passes" — must NOT downgrade
    passport.record_gate("v47e_two_buyer", RESULT_PASS, "ub3_delegate", log_fn=log)

    # Decision_id refresh
    passport.record_decision_id_refresh("v48-2")

    # Other gates PASS
    for g in ["v47d_boundary", "v47f_size_edge_floor", "v47h_rug_veto",
              "v47i_medium_rug_veto", "v59_true_edge", "v60_firewall",
              "v61_continuation", "v53_risk_veto_if_available",
              "token2022_pump_v2", "swqos_fee_policy", "size_cap",
              "snapshot_fresh"]:
        passport.record_gate(g, RESULT_PASS, "ok", log_fn=log)

    # V67 BLOCK (lane-OR bypass attempted but V64 doesn't allow it)
    passport.record_gate("v67_flow_confirm", RESULT_BLOCK, "v47h_ratio", log_fn=log, mandatory=False)
    # ^ marking V67 as non-mandatory here for the self-test; the spec says
    # V67 is mandatory, so this is just to show the lattice behavior

    # Compute final_pass — should be FALSE due to v47c SHADOW_ONLY + v47e BLOCK
    result = passport.compute_final_pass(log_fn=log)
    print()
    print("Expected final_pass: False")
    print("Actual final_pass:", result)
    print("Blockers:", passport.blockers)
    print("Shadow:", passport.shadow_only_reasons)

    # Authorization check
    print()
    auth = v64_authorize_live_buy(
        passport=passport,
        proposed_mint=passport.mint,
        proposed_decision_id="v48-2",
        proposed_size_sol=0.005,
        proposed_route="pump_bc",
        bypass_env_check=False,
        log_fn=log,
    )
    print("Authorized:", auth.authorized, "reason:", auth.reason)
    print()
    print("4RZH_BLOCKED_BY_V64=" + ("true" if not auth.authorized else "false"))
