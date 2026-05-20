"""Move V60 shadow hook earlier — fire at V47C-MULTI-BUYER-GATE log site.

Without this patch, V60 shadow only fires after V48-CANDIDATE-DECISION emits,
which requires gate_pass=true. With V67-LEGACY-BYPASS disabled, V47C single-
buyer blocks 100% of candidates upstream, so V60 never sees real data.

This patch injects a V60 shadow call immediately AFTER `evaluate_multi_buyer_gate`
returns, BEFORE the early-return that blocks single-buyer candidates. Result:
V60 fires on every candidate that reached V47C with at least a curve snapshot.

To avoid the v59_universal check tripping (which requires v67_passing/v57_prom),
the shadow flags candidate.is_v67_passing=True. This is only valid in observe
mode (PGG2_V60_OBSERVE_MODE=1) where no real send fires regardless.
"""
import re
import sys

HARNESS = "/root/piggy/pgg2_v48_drylive_harness.py"
src = open(HARNESS).read()

# Anchor: the exact lines right after evaluate_multi_buyer_gate returns
anchor = (
    "        mb_pass, mb_blocker = evaluate_multi_buyer_gate(\n"
    "            buyer_stats_for_gate, logger=log, mint_for_log=mint,\n"
    "        )\n"
)

if anchor not in src:
    print("PATCH-V60-V47C-SHADOW: anchor not found", file=sys.stderr)
    sys.exit(1)

v60_v47c_shadow = """        # ---- V60 EARLY SHADOW (fires on EVERY candidate at V47C log time) ----
        if _env_flag("PGG2_V60_OBSERVE_MODE", "0"):
            try:
                from pgg2_v60_live_send_firewall import (
                    V60Candidate as _V60Cand_v47c,
                    V60TxPlan as _V60Plan_v47c,
                    v60_authorize_live_buy as _v60_auth_v47c,
                )
                _v60v_size = 0.005  # observe shadow uses the cap as canonical size
                # Estimate ep from pending_buy_sol_1000ms (rough proxy at V47C time)
                _v60v_pbs1k = float(buyer_stats_for_gate.get("pending_buy_sol_1000ms", 0.0) or 0.0)
                _v60v_pss1k = float(buyer_stats_for_gate.get("pending_sell_sol_1000ms", 0.0) or 0.0)
                _v60v_ub = int(buyer_stats_for_gate.get("unique_buyers_250ms", 0) or 0)
                # Very rough ep proxy: positive flow imbalance scaled small (observe only)
                _v60v_ep_est = max(-0.005, min(0.005, (_v60v_pbs1k - _v60v_pss1k) * 0.0005))
                _v60v_cand = _V60Cand_v47c(
                    mint=mint,
                    selected_size_sol=_v60v_size,
                    candidate_lane="v47c_early_shadow",
                    rule_id="v47c_early_shadow",
                    expected_pnl_sol=_v60v_ep_est,
                    true_edge_sol=None,
                    token_program="spl",
                    route="pump_bc",
                    sim_needed=0,
                    pair_source="decision_curve_snapshot",
                    snapshot_age_ms=0,
                    source_lead_ms=0,
                    risk_result=None,
                    risk_fetched_at_ms=None,
                    is_v67_passing=True,   # observe-only: bypass v59_universal lane check
                    is_v57_promotion=False,
                    wallet_balance_sol=0.0,
                )
                _v60v_plan = _V60Plan_v47c(
                    decoded_amount_tokens_raw=0,
                    decoded_max_sol_cost_lamports=int(round(_v60v_size * 1e9)),
                    swqos_tip_sol=0.000005,
                    priority_fee_sol=0.000005,
                    base_fee_sol=0.000005,
                    uses_pump_v2=False,
                    has_sell_v2_capability=True,
                )
                _v60v_dec = _v60_auth_v47c(_v60v_cand, _v60v_plan, log_fn=log)
                if _v60v_dec.passed:
                    log(f"PGG2-V60-OBSERVE-PASS mint={_short(mint)} stage=v47c_early size={_v60v_size:.4f} ep_est={_v60v_ep_est:+.6f} true_edge={_v60v_dec.true_edge_sol:+.6f} v47c_pass={int(mb_pass)} ub250={_v60v_ub}")
                else:
                    _v60v_det = ""
                    for _cr in _v60v_dec.check_results:
                        if not _cr.passed:
                            _v60v_det = _cr.detail
                            break
                    log(f"PGG2-V60-OBSERVE-BLOCK mint={_short(mint)} stage=v47c_early size={_v60v_size:.4f} ep_est={_v60v_ep_est:+.6f} blocker={_v60v_dec.blocker} detail={_v60v_det} v47c_pass={int(mb_pass)} ub250={_v60v_ub}")
            except Exception as _v60v_err:
                log(f"PGG2-V60-OBSERVE-ERR stage=v47c_early err={type(_v60v_err).__name__}:{_v60v_err}")
"""

if "stage=v47c_early" not in src:
    src = src.replace(anchor, anchor + v60_v47c_shadow, 1)
    print("PATCH-V60-V47C-SHADOW: applied")
else:
    print("PATCH-V60-V47C-SHADOW: already present")

open(HARNESS, "w").write(src)
print(f"PATCH-V60-V47C-SHADOW: harness size now {len(src)} bytes")
