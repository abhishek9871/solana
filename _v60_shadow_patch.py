"""Inject a V60 shadow/observe hook at _emit_v48_candidate_decision.

This hook runs the full V60 firewall against every candidate the V48 decision
engine emits. Fires in both dry and live mode. Lets observe-mode show real
V60 verdicts on real candidates without any live send.

Env-gated by PGG2_V60_OBSERVE_MODE=1 so it doesn't add overhead unless wanted.

Logs:
    PGG2-V60-OBSERVE-CHECK (one per candidate, via firewall's normal CHECK)
    PGG2-V60-OBSERVE-PASS  mint=.. size=.. true_edge=.. tx_digest=..
    PGG2-V60-OBSERVE-BLOCK mint=.. blocker=.. detail=..
"""
import re
import sys

HARNESS = "/root/piggy/pgg2_v48_drylive_harness.py"
src = open(HARNESS).read()

# Anchor: the existing V59 hook in _emit_v48_candidate_decision.
# Insert V60 shadow hook AFTER the V59 block ends (after the `log(f"PGG2-V59-TRUE-EDGE-ERR ...")` line).
anchor = (
    "            except Exception as _v59c_e:\n"
    "                log(f\"PGG2-V59-TRUE-EDGE-ERR err={type(_v59c_e).__name__}:{_v59c_e}\")\n"
)

if anchor not in src:
    print("PATCH-V60-SHADOW: anchor not found", file=sys.stderr)
    sys.exit(1)

v60_shadow = """        # ---- V60 SHADOW / OBSERVE HOOK (fires dry + live when PGG2_V60_OBSERVE_MODE=1) ----
        if gate_pass and _env_flag("PGG2_V60_OBSERVE_MODE", "0"):
            try:
                from pgg2_v60_live_send_firewall import (
                    V60Candidate as _V60Candidate_shadow,
                    V60TxPlan as _V60TxPlan_shadow,
                    v60_authorize_live_buy as _v60_authorize_shadow,
                )
                _v60s_mint = str(rec.get("mint") or "")
                _v60s_size = float(rec.get("selected_size_sol", 0.005) or 0.005)
                _v60s_ep = float(rec.get("exp_pnl", 0.0) or 0.0)
                _v60s_lane = str(rec.get("signal_lane", "") or "")
                _v60s_token_prog = "spl"  # candidate-decision time may not know yet
                _v60s_cand = _V60Candidate_shadow(
                    mint=_v60s_mint,
                    selected_size_sol=_v60s_size,
                    candidate_lane=_v60s_lane,
                    rule_id=str(rec.get("rule_id", "") or ""),
                    expected_pnl_sol=_v60s_ep,
                    true_edge_sol=None,
                    token_program=_v60s_token_prog,
                    route="pump_bc",
                    sim_needed=0,
                    pair_source="decision_curve_snapshot",
                    snapshot_age_ms=int(rec.get("snapshot_age_ms", 0) or 0),
                    source_lead_ms=int(rec.get("source_lead_ms", 0) or 0),
                    risk_result=rec.get("risk_result"),
                    risk_fetched_at_ms=rec.get("risk_fetched_at_ms"),
                    is_v67_passing=("v67" in _v60s_lane.lower()),
                    is_v57_promotion=("v57" in _v60s_lane.lower() or "promotion" in _v60s_lane.lower()),
                    wallet_balance_sol=0.0,
                )
                _v60s_plan = _V60TxPlan_shadow(
                    decoded_amount_tokens_raw=0,
                    decoded_max_sol_cost_lamports=int(round(_v60s_size * 1e9)),
                    swqos_tip_sol=0.000005,
                    priority_fee_sol=0.000005,
                    base_fee_sol=0.000005,
                    uses_pump_v2=False,
                    has_sell_v2_capability=True,
                )
                _v60s_decision = _v60_authorize_shadow(_v60s_cand, _v60s_plan, log_fn=log)
                if _v60s_decision.passed:
                    log(f"PGG2-V60-OBSERVE-PASS mint={_short(_v60s_mint)} size={_v60s_size:.4f} true_edge={_v60s_decision.true_edge_sol:+.6f} tx_digest={_v60s_decision.tx_digest[:16]}")
                else:
                    _v60s_det = ""
                    for _cr in _v60s_decision.check_results:
                        if not _cr.passed:
                            _v60s_det = _cr.detail
                            break
                    log(f"PGG2-V60-OBSERVE-BLOCK mint={_short(_v60s_mint)} size={_v60s_size:.4f} blocker={_v60s_decision.blocker} detail={_v60s_det}")
            except Exception as _v60s_err:
                log(f"PGG2-V60-OBSERVE-ERR err={type(_v60s_err).__name__}:{_v60s_err}")

"""

if "PGG2-V60-OBSERVE-PASS" not in src:
    src = src.replace(anchor, anchor + v60_shadow, 1)
    print("PATCH-V60-SHADOW: applied")
else:
    print("PATCH-V60-SHADOW: already present")

open(HARNESS, "w").write(src)
print(f"PATCH-V60-SHADOW: harness size now {len(src)} bytes")
