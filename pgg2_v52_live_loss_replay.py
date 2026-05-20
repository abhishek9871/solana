"""V52 retrospective live-loss replay.

Feeds each known live trade through pgg2_v52_unified_gate.evaluate_v52_live_candidate
using the data that was persisted at decision time, plus current Helius RPC
queries for holder data that was preserved in V51 forensic.

Honest about what's NOT replayable: pending-flow window features
(ub_250, tbs, pbc, pbsol, pssol, lbs, ub_500) and the V47B adverse-branch
outcome were not persisted in the V50A/B/C/V51B live logs. Those gates will
report 'data_unavailable' as a blocker, which is the truthful answer.
"""
from __future__ import annotations
import json
import sys
from dataclasses import asdict

sys.path.insert(0, "/root/piggy")
from pgg2_v52_unified_gate import evaluate_v52_live_candidate

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Per-mint reconstructed candidates from V51 forensic + V50A/B/C/V51B reports.
# Pending-flow runtime fields = None (not persisted).
TRADES = [
    {
        "label": "V50A loser",
        "mint": "GXaRd5F1RUUTPDvDeFppTP31u9Dx4UkBsJm7Lz2Fpump",
        "observed_pnl_sol": -0.005237,
        "candidate": {
            "route": "pump_bc",
            "sim_needed": 0,
            "size_sol": 0.005,
            "expected_pnl_sol": 0.000294546,  # V50A predicted_all_in at decision
            "token_program": TOKEN_2022,
            "v2_path_used": False,  # V50A used v1 builder
            "buy_v2_decoded_amount": None,
            "sell_v2_decoded_amount": None,
            "holder_count": 3,
            "holder_top1_pct": 1.53,
            "ub_250": None, "tbs_250": None, "pbc_250": None,
            "pbsol_250": None, "pssol_250": None, "lbs_250": None,
            "ub_500": None, "pbsol_500": None, "pssol_500": None,
            "adverse_branch_outcome": None,
            "holder_check_age_ms": 100,
            "decision_state_age_ms": 800,
            "uses_forbidden_path": False,
            "swqos_tip_sol": 0.000005,
        },
    },
    {
        "label": "V50B winner",
        "mint": "61Ph76cbGL2hMidG1x5fW37DXpJAQ3XEq3psNGwHpump",
        "observed_pnl_sol": +0.000525983,
        "candidate": {
            "route": "pump_bc",
            "sim_needed": 0,
            "size_sol": 0.005,
            "expected_pnl_sol": 0.000537060,
            "token_program": TOKEN_2022,
            "v2_path_used": False,
            "buy_v2_decoded_amount": None,
            "sell_v2_decoded_amount": None,
            "holder_count": 20,
            "holder_top1_pct": 27.41,
            "ub_250": None, "tbs_250": None, "pbc_250": None,
            "pbsol_250": None, "pssol_250": None, "lbs_250": None,
            "ub_500": None, "pbsol_500": None, "pssol_500": None,
            "adverse_branch_outcome": None,
            "holder_check_age_ms": 100,
            "decision_state_age_ms": 300,
            "uses_forbidden_path": False,
            "swqos_tip_sol": 0.000005,
        },
    },
    {
        "label": "V50C stuck (rescued)",
        "mint": "9Cc2QxvPBKJi1GQBQb7ezUCVaFCewUxp6Fd8FDjopump",
        "observed_pnl_sol": -0.007129,
        "candidate": {
            "route": "pump_bc",
            "sim_needed": 0,
            "size_sol": 0.005,
            "expected_pnl_sol": 0.000350,
            "token_program": TOKEN_2022,
            "v2_path_used": False,
            "buy_v2_decoded_amount": None,
            "sell_v2_decoded_amount": None,
            "holder_count": 19,
            "holder_top1_pct": 3.19,
            "ub_250": None, "tbs_250": None, "pbc_250": None,
            "pbsol_250": None, "pssol_250": None, "lbs_250": None,
            "ub_500": None, "pbsol_500": None, "pssol_500": None,
            "adverse_branch_outcome": None,
            "holder_check_age_ms": 100,
            "decision_state_age_ms": 600,
            "uses_forbidden_path": False,
            "swqos_tip_sol": 0.000005,
        },
    },
    {
        "label": "V51B loser",
        "mint": "6z8pZHwmq13kZch2eK8sVGz9GFyagKzHg6tUTgEKpump",
        "observed_pnl_sol": -0.002362,
        "candidate": {
            "route": "pump_bc",
            "sim_needed": 0,
            "size_sol": 0.005,
            "expected_pnl_sol": 0.000400,
            "token_program": TOKEN_2022,
            "v2_path_used": True,  # V51B used v2 builder
            "buy_v2_decoded_amount": 19312468259,
            "buy_v2_decoded_max_sol_cost": 5750000,
            "sell_v2_decoded_amount": 19312468259,
            "sell_v2_decoded_min_sol_output": 1731496,
            "holder_count": 19,
            "holder_top1_pct": 3.32,
            "ub_250": None, "tbs_250": None, "pbc_250": None,
            "pbsol_250": None, "pssol_250": None, "lbs_250": None,
            "ub_500": None, "pbsol_500": None, "pssol_500": None,
            "adverse_branch_outcome": None,
            "holder_check_age_ms": 100,
            "decision_state_age_ms": 0,
            "uses_forbidden_path": False,
            "swqos_tip_sol": 0.000005,
        },
    },
]


def main() -> int:
    print("=" * 90)
    print("V52 retrospective live-loss replay — full unified-gate stack")
    print("=" * 90)
    results = []
    for trade in TRADES:
        gr = evaluate_v52_live_candidate(trade["candidate"])
        print(f"\n--- {trade['label']} mint={trade['mint'][:14]}.. observed_pnl={trade['observed_pnl_sol']:+.6f} ---")
        print(f"  PASS={gr.pass_}  first_blocker={gr.first_blocker}")
        per_gate = []
        for g in gr.gate_outputs:
            mark = "✓" if g["pass"] else "✗"
            bl = g["blocker"] or ""
            print(f"    {mark} {g['name']:<32} {bl}")
            per_gate.append({"name": g["name"], "pass": g["pass"], "blocker": g["blocker"]})
        results.append({
            "label": trade["label"],
            "mint": trade["mint"],
            "observed_pnl_sol": trade["observed_pnl_sol"],
            "v52_pass": gr.pass_,
            "v52_first_blocker": gr.first_blocker,
            "per_gate": per_gate,
        })
    print("\n" + "=" * 90)
    print("SUMMARY (intent: blocked-loser = good, blocked-winner = bad)")
    print("=" * 90)
    print(f"{'label':<28} {'observed':>10} {'v52_pass':>10}  {'first_blocker'}")
    for r in results:
        print(f"{r['label']:<28} {r['observed_pnl_sol']:+.6f}  {str(r['v52_pass']):>10}  {r['v52_first_blocker']}")

    # Honest verdict
    print("\n" + "=" * 90)
    print("VERDICT (honest):")
    losers_blocked = sum(1 for r in results if r["observed_pnl_sol"] < 0 and not r["v52_pass"])
    losers_total = sum(1 for r in results if r["observed_pnl_sol"] < 0)
    winners_preserved = sum(1 for r in results if r["observed_pnl_sol"] >= 0 and r["v52_pass"])
    winners_total = sum(1 for r in results if r["observed_pnl_sol"] >= 0)
    print(f"  Losers blocked: {losers_blocked}/{losers_total}")
    print(f"  Winners preserved (passed): {winners_preserved}/{winners_total}")

    # Distinguishability check
    v51b_passed = next((r["v52_pass"] for r in results if r["label"] == "V51B loser"), None)
    v50b_passed = next((r["v52_pass"] for r in results if r["label"] == "V50B winner"), None)
    if v51b_passed is not None and v50b_passed is not None:
        if v51b_passed == v50b_passed:
            print("  V52 cannot distinguish V51B loser from V50B winner (both pass={!s}).".format(v51b_passed))
            print("  Per spec: STOP. Current data cannot separate winner from loser.")
        else:
            print("  V52 distinguishes V51B loser from V50B winner (loser={}, winner={}).".format(v51b_passed, v50b_passed))

    with open("/root/piggy/V52_LIVE_LOSS_REPLAY.md", "w") as f:
        f.write("# V52 Live-Loss Replay\n\n")
        f.write("| label | mint | observed_pnl_sol | v52_pass | first_blocker |\n")
        f.write("|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['label']} | `{r['mint'][:14]}..` | {r['observed_pnl_sol']:+.6f} | {r['v52_pass']} | `{r['v52_first_blocker']}` |\n")
        f.write(f"\nLosers blocked: {losers_blocked}/{losers_total}.  Winners preserved: {winners_preserved}/{winners_total}.\n\n")
        f.write("## Per-gate detail (each trade)\n\n")
        for r in results:
            f.write(f"### {r['label']} ({r['mint'][:14]}..)\n\n")
            for g in r["per_gate"]:
                mark = "PASS" if g["pass"] else "BLOCK"
                f.write(f"- {mark} `{g['name']}` blocker=`{g['blocker'] or ''}`\n")
            f.write("\n")
        f.write("## Honest verdict\n\n")
        if v51b_passed == v50b_passed:
            f.write(f"V52 unified gate stack **cannot** distinguish V51B loser from V50B winner — both report pass={v51b_passed}.\n\n")
            f.write("Per user spec: STOP — current data cannot separate winner from loser.\n\n")
            f.write("The retrospective replay is also limited by missing runtime data: pending-flow window\n")
            f.write("features (ub_250, tbs, pbc, pbsol, pssol, lbs) and V47B adverse-branch outcome were\n")
            f.write("not persisted in V50A/B/C/V51B live logs. Those gates report 'data_unavailable'.\n")
        else:
            f.write(f"V52 distinguishes V51B loser ({v51b_passed}) from V50B winner ({v50b_passed}).\n")
    print("\nReport written: /root/piggy/V52_LIVE_LOSS_REPLAY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
