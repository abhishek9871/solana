"""V52 unified live gate evaluator.

Evaluates a candidate dict through every gate in exact spec order. Gates that
need data not present return evaluated=False; the gate is treated as a BLOCK
('data_unavailable_<name>') in that case. Pass requires ALL gates evaluated and
all pass.

The candidate dict shape (fields used by various gates):
- mint: str
- route: str  (must == 'pump_bc')
- sim_needed: int (must == 0)
- size_sol: float
- expected_pnl_sol: float
- token_program: str (Token-2022 program id allowed only if v2_path_used)
- v2_path_used: bool
- buy_v2_decoded_amount: int (None ok if not built)
- buy_v2_decoded_max_sol_cost: int
- sell_v2_decoded_amount: int
- sell_v2_decoded_min_sol_output: int
- holder_count: int
- holder_top1_pct: float
- ub_250: int          (unique_buyers_250ms; None = unknown)
- tbs_250: float       (top_buyer_share_250ms; None = unknown)
- pbc_250: int         (pending_buy_count_250ms; None = unknown)
- pbsol_250: float     (pending_buy_sol_250ms; None = unknown)
- pssol_250: float     (pending_sell_sol_250ms; None = unknown)
- lbs_250: float       (largest_buy_share_250ms; None = unknown)
- ub_500: int          (None = unknown)
- adverse_branch_outcome: str ('BRANCH_SAFE_BUY_FAIL'|'BRANCH_WIN'|'BRANCH_UNSAFE_OPEN'|None)
- holder_check_age_ms: int
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class GateResult:
    pass_: bool
    first_blocker: Optional[str]
    gate_outputs: list = field(default_factory=list)
    selected_size: Optional[float] = None
    expected_pnl: Optional[float] = None
    notes: list = field(default_factory=list)


def _g(name, ok, blocker=None, data=None):
    return {"name": name, "pass": ok, "blocker": blocker, "data": data or {}}


def evaluate_v52_live_candidate(c: dict) -> GateResult:
    """Run every gate in order; first failure becomes first_blocker but we
    continue to record all gate outputs."""
    out = []
    first_blocker = None

    def _check(g):
        nonlocal first_blocker
        out.append(g)
        if not g["pass"] and first_blocker is None:
            first_blocker = g["name"] + ":" + (g["blocker"] or "fail")

    # 1. route = pump_bc
    _check(_g("01_route_pump_bc", c.get("route") == "pump_bc", blocker="route_not_pump_bc"))

    # 2. sim_needed = 0
    _check(_g("02_sim_needed_zero", c.get("sim_needed") == 0, blocker="sim_needed_nonzero"))

    # 3. V47B guarded branch — adverse safe-fail or non-negative
    ab = c.get("adverse_branch_outcome")
    if ab is None:
        _check(_g("03_v47b_guarded_branch", False, blocker="data_unavailable"))
    else:
        ok = ab in ("BRANCH_SAFE_BUY_FAIL", "BRANCH_WIN")
        _check(_g("03_v47b_guarded_branch", ok, blocker=f"adverse_outcome:{ab}", data={"ab": ab}))

    # 4. V47C multi-buyer gate
    ub = c.get("ub_250"); pbc = c.get("pbc_250"); pbsol = c.get("pbsol_250"); pssol = c.get("pssol_250"); tbs = c.get("tbs_250")
    if any(v is None for v in (ub, pbc, pbsol, pssol)):
        _check(_g("04_v47c_multi_buyer", False, blocker="data_unavailable"))
    else:
        cond1 = ub >= 2
        cond2 = pbc >= 2
        cond3 = pbsol > pssol
        cond4 = (tbs is None) or (tbs <= 0.75)
        ok = cond1 and cond2 and cond3 and cond4
        bl = None if ok else ("ub_lt_2" if not cond1 else "pbc_lt_2" if not cond2 else "buy_sol_le_sell_sol" if not cond3 else "tbs_gt_075")
        _check(_g("04_v47c_multi_buyer", ok, blocker=bl, data={"ub": ub, "pbc": pbc, "pbsol": pbsol, "pssol": pssol, "tbs": tbs}))

    # 5. V47D boundary guard — size>=0.020 requires ub>=3 + tbs<=0.55 + pbsol>=size*5 + exp_pnl>=0.0008 + lbs<=0.55
    size = c.get("size_sol")
    exp_pnl = c.get("expected_pnl_sol")
    lbs = c.get("lbs_250")
    if size is None:
        _check(_g("05_v47d_boundary", False, blocker="data_unavailable"))
    elif size >= 0.020:
        if any(v is None for v in (ub, tbs, pbsol, exp_pnl, lbs)):
            _check(_g("05_v47d_boundary", False, blocker="data_unavailable_for_size_ge_020"))
        else:
            ok = ub >= 3 and tbs <= 0.55 and pbsol >= size * 5 and exp_pnl >= 0.0008 and lbs <= 0.55
            _check(_g("05_v47d_boundary", ok, blocker="boundary_fail_size_ge_020", data={"ub": ub, "tbs": tbs}))
    else:
        _check(_g("05_v47d_boundary", True, data={"size_lt_020": True}))

    # 6. V47E two-buyer concentration — ub==2 requires tbs<=0.55 (actual)
    if ub is None:
        _check(_g("06_v47e_two_buyer", False, blocker="data_unavailable"))
    elif ub == 2:
        if tbs is None:
            _check(_g("06_v47e_two_buyer", False, blocker="data_unavailable_tbs"))
        else:
            ok = tbs <= 0.55
            _check(_g("06_v47e_two_buyer", ok, blocker="ub_2_tbs_gt_055"))
    else:
        _check(_g("06_v47e_two_buyer", True, data={"ub_ne_2": True}))

    # 7. V47F size-tiered exp_pnl floor
    if size is None or exp_pnl is None:
        _check(_g("07_v47f_size_floor", False, blocker="data_unavailable"))
    else:
        if size <= 0.010:
            floor = 0.0006
        elif size <= 0.020:
            floor = 0.0010
        elif size <= 0.030:
            floor = 0.0020
        else:
            floor = 0.0030
        ok = exp_pnl >= floor
        _check(_g("07_v47f_size_floor", ok, blocker=f"exp_pnl_lt_floor_{floor}", data={"floor": floor, "exp_pnl": exp_pnl}))

    # 8. V47F hold-cap eligibility — only check size-tier sanity (cap applies post-buy)
    _check(_g("08_v47f_hold_cap_eligible", size is not None and size <= 0.075, blocker="size_gt_075_dryliv_only"))

    # 9. V47H rug veto — requires holder + sell-side data
    pssol_check = c.get("pssol_250")
    if any(v is None for v in (ub, pbsol, pssol_check, exp_pnl)):
        _check(_g("09_v47h_rug_veto", False, blocker="data_unavailable"))
    else:
        # blow-off anomaly: exp_pnl/size >= 2.0 → block
        ratio = (exp_pnl / size) if size else 0
        cond_a = pssol_check < pbsol * 0.35
        cond_b_blowoff = ratio < 2.0
        cond_c_thin = not (ub == 2 and exp_pnl < 0.001)
        ok = cond_a and cond_b_blowoff and cond_c_thin
        bl = None if ok else "sell_pressure_or_blowoff_or_thin"
        _check(_g("09_v47h_rug_veto", ok, blocker=bl, data={"ratio": ratio}))

    # 10. V47I medium-rug veto — module exists; uses 500ms windows we don't have
    pbsol_500 = c.get("pbsol_500"); pssol_500 = c.get("pssol_500"); ub_500 = c.get("ub_500")
    if pbsol_500 is None or pssol_500 is None:
        _check(_g("10_v47i_medium_rug", False, blocker="data_unavailable"))
    else:
        ok = pssol_500 < pbsol_500 * 0.25
        _check(_g("10_v47i_medium_rug", ok, blocker="medium_sell_pressure"))

    # 11. V51B holder breadth gate
    hc = c.get("holder_count"); top1 = c.get("holder_top1_pct")
    if hc is None or top1 is None:
        _check(_g("11_v51b_holder_breadth", False, blocker="data_unavailable"))
    else:
        cond1 = hc >= 4
        cond2 = not (top1 > 45.0 and hc < 8)
        ok = cond1 and cond2
        bl = None if ok else ("holder_count_lt_4" if not cond1 else "extreme_top1_with_thin_breadth")
        _check(_g("11_v51b_holder_breadth", ok, blocker=bl, data={"holder_count": hc, "top1_pct": top1}))

    # 12. Token-2022 requires v2 path
    tp = c.get("token_program"); v2 = c.get("v2_path_used", False)
    if tp is None:
        _check(_g("12_token2022_v2_only", False, blocker="data_unavailable"))
    elif tp == TOKEN_2022_PROGRAM:
        _check(_g("12_token2022_v2_only", bool(v2), blocker="token_2022_without_v2_path", data={"v2": v2}))
    else:
        _check(_g("12_token2022_v2_only", True, data={"is_token_2022": False}))

    # 13. buy_v2 / sell_v2 guard decode pass
    buy_dec = c.get("buy_v2_decoded_amount") is not None
    sell_dec = c.get("sell_v2_decoded_amount") is not None
    _check(_g("13_buyv2_sellv2_decode", buy_dec and sell_dec, blocker="v2_decode_unavailable_or_failed"))

    # 14. SWQOS fee policy: tip<=0.000005 and edge after fees >= required floor (size-normalized)
    tip = c.get("swqos_tip_sol", 0.000005)
    if size is None or exp_pnl is None:
        _check(_g("14_swqos_fee_policy", False, blocker="data_unavailable"))
    else:
        round_trip_fees = 2 * (0.0000287 + 0.0000200 + tip) + size * 2 * 0.0105
        edge_after = exp_pnl - round_trip_fees
        floor = max(0.00010, size * 0.0006)
        ok = tip <= 0.000005 and edge_after >= floor
        _check(_g("14_swqos_fee_policy", ok, blocker=f"edge_after_fees_lt_{floor}", data={"tip": tip, "rt_fees": round_trip_fees, "edge_after": edge_after}))

    # 15. No stale data
    age = c.get("holder_check_age_ms"); ds = c.get("decision_state_age_ms")
    age_ok = (age is None) or (age <= 5000)
    ds_ok = (ds is None) or (ds <= 1500)
    _check(_g("15_no_stale_data", age_ok and ds_ok, blocker="stale_holder_or_decision_state", data={"age_holder": age, "age_decision": ds}))

    # 16. No old ESB / protected-hold / recovery actual-entry path used
    forbidden = c.get("uses_forbidden_path", False)
    _check(_g("16_no_forbidden_path", not forbidden, blocker="forbidden_execution_path_in_candidate"))

    all_pass = all(g["pass"] for g in out)
    return GateResult(pass_=all_pass, first_blocker=first_blocker, gate_outputs=out, selected_size=size, expected_pnl=exp_pnl)
