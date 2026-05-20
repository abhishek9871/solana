"""Replay the 78zB..pump V56D-lane candidate through V60 with the fix applied."""
import os, time, sys
sys.path.insert(0, "/root/piggy")
os.environ["PGG2_LIVE_CONFIRM"] = "I_ACCEPT_REAL_SOL_RISK"
os.environ["PGG2_V60_REQUIRE_RISK_PASS"] = "1"  # exercise risk too

from pgg2_v60_live_send_firewall import (
    v60_authorize_live_buy, V60Candidate, V60TxPlan,
)

# Replay 78zB at decision_id=v48-9: ub=5, top_share=0.339, ep=+0.004948, V56D lane
cand = V60Candidate(
    mint="78zBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump",
    selected_size_sol=0.005,
    candidate_lane="v56d_flow_scratch",
    rule_id="v48_v47i_stack",
    expected_pnl_sol=0.004948,
    true_edge_sol=None,
    token_program="spl",
    route="pump_bc",
    sim_needed=0,
    pair_source="decision_curve_snapshot",
    snapshot_age_ms=300,
    source_lead_ms=279,
    risk_result={
        "holders": 5,
        "bundlers": 0,
        "dev": 0,
        "snipers": 0,
        "insiders": 0,
        "labels": [],
        "rugged": False,
    },
    risk_fetched_at_ms=int(time.time() * 1000),
    is_v67_passing=False,
    is_v57_promotion=False,
    wallet_balance_sol=0.1,
)
plan = V60TxPlan(
    decoded_amount_tokens_raw=0,
    decoded_max_sol_cost_lamports=5_000_000,
    swqos_tip_sol=0.000005,
    priority_fee_sol=0.000005,
    base_fee_sol=0.000005,
    uses_pump_v2=False,
    has_sell_v2_capability=True,
)
d = v60_authorize_live_buy(cand, plan)
print("---")
print(f"passed={d.passed}")
print(f"blocker={d.blocker}")
print(f"true_edge_sol={d.true_edge_sol:+.6f}")
print(f"tx_digest={d.tx_digest[:16]}")
print()
for r in d.check_results:
    status = "PASS" if r.passed else "BLOCK"
    print(f"  {r.name:<22} {status:<5}  {r.detail}")
