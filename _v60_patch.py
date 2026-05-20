"""V60 firewall hook injector for pgg2_v48_drylive_harness.py.

Applies two surgical edits:
  1. Module import for the V60 firewall (near existing pgg2_v59 import)
  2. Firewall call + size-cap-fatal assertion BEFORE every broker.send_signed
     buy call at the single harness choke point.
"""
import re
import sys

HARNESS = "/root/piggy/pgg2_v48_drylive_harness.py"
src = open(HARNESS).read()

# ===== Patch 1: V60 firewall import =====
v60_import_block = """# === V60 LIVE-SEND FIREWALL IMPORT (2026-05-19) ===
try:
    from pgg2_v60_live_send_firewall import (
        V60Candidate as _V60Candidate,
        V60TxPlan as _V60TxPlan,
        v60_authorize_live_buy as _v60_authorize_live_buy,
    )
    _V60_AVAILABLE = True
except Exception as _v60_exc:
    _V60_AVAILABLE = False
"""

if "_V60_AVAILABLE" not in src:
    anchor = None
    m = re.search(r"^from pgg2_v59_true_edge import .*$", src, re.MULTILINE)
    if m:
        anchor = m.group(0)
    else:
        m = re.search(r"^from pgg2_v59 .*$", src, re.MULTILINE)
        if m:
            anchor = m.group(0)
        else:
            m = re.search(r"^import time$", src, re.MULTILINE)
            if m:
                anchor = m.group(0)
    assert anchor, "could not find import anchor"
    src = src.replace(anchor, anchor + "\n" + v60_import_block, 1)
    print("PATCH-1-IMPORT: applied")
else:
    print("PATCH-1-IMPORT: already present")

# ===== Patch 2: V60 hook + size-cap-fatal before BUY-SEND =====
anchor_block = (
    "            v48_live_buy_sends += 1\n"
    "            log(\n"
    '                f"PGG2-V48-LIVE-BUY-SEND mint={_short(mint)} "\n'
    '                f"size={size_sol:.6f} sig_preview={buy_preview} "\n'
    "                f\"quote_ms={int(buy_quote.get('quote_network_latency_ms') or 0)}\"\n"
    "            )"
)

v60_hook = """            # === V60 LIVE-SEND FIREWALL (2026-05-19) — the only authorized buy gate ===
            if _V60_AVAILABLE:
                try:
                    _signal_lane = str(rec.get("signal_lane", "") or "")
                    _is_v67_pass = ("v67" in _signal_lane.lower())
                    _is_v57_prom = ("v57" in _signal_lane.lower() or "promotion" in _signal_lane.lower())
                    _snap_age = int(rec.get("snapshot_age_ms", 0) or 0)
                    _source_lead = int(rec.get("source_lead_ms", 0) or 0)
                    _ep_sol = float(rec.get("expected_pnl_sol", rec.get("expected_pnl", 0.0)) or 0.0)
                    _pair_src = str(rec.get("pair_source", "decision_curve_snapshot") or "")
                    _route = str(rec.get("route", "pump_bc") or "pump_bc")
                    _token_prog = str(rec.get("token_program", "spl") or "spl")
                    _decoded_max_sol_lamports = int(round(float(size_sol) * 1e9))
                    _decoded_amount_raw = int(buy_quote.get("min_tokens_raw", buy_quote.get("out_tokens_raw", 0)) or 0)
                    _v60_cand = _V60Candidate(
                        mint=mint,
                        selected_size_sol=float(size_sol),
                        candidate_lane=_signal_lane,
                        rule_id=str(rec.get("rule_id", "") or ""),
                        expected_pnl_sol=_ep_sol,
                        true_edge_sol=None,
                        token_program=_token_prog,
                        route=_route,
                        sim_needed=int(rec.get("sim_needed", 0) or 0),
                        pair_source=_pair_src,
                        snapshot_age_ms=_snap_age,
                        source_lead_ms=_source_lead,
                        risk_result=rec.get("risk_result"),
                        risk_fetched_at_ms=rec.get("risk_fetched_at_ms"),
                        is_v67_passing=_is_v67_pass,
                        is_v57_promotion=_is_v57_prom,
                        wallet_balance_sol=float(rec.get("wallet_balance_sol", 0.0) or 0.0),
                    )
                    _v60_plan = _V60TxPlan(
                        decoded_amount_tokens_raw=_decoded_amount_raw,
                        decoded_max_sol_cost_lamports=_decoded_max_sol_lamports,
                        swqos_tip_sol=0.000005,
                        priority_fee_sol=0.000005,
                        base_fee_sol=0.000005,
                        uses_pump_v2=(_token_prog.lower() in ("token-2022", "token2022", "t22")),
                        has_sell_v2_capability=True,
                    )
                    _v60_decision = _v60_authorize_live_buy(_v60_cand, _v60_plan, log_fn=log)
                except Exception as _v60_err:
                    log(f"PGG2-V60-SEND-BLOCKED mint={_short(mint)} blocker=hook_error err={type(_v60_err).__name__}:{_v60_err}")
                    v48_live_buy_safe_failures += 1
                    if live_failed_buy_cooldown_ms > 0:
                        live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                    return False
                if not _v60_decision.passed:
                    log(f"PGG2-V60-SEND-BLOCKED mint={_short(mint)} blocker={_v60_decision.blocker} size={float(size_sol):.4f}")
                    v48_live_buy_safe_failures += 1
                    if live_failed_buy_cooldown_ms > 0:
                        live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                    return False
                # === V60 PHASE 4: Universal size-cap-fatal assertion (defense-in-depth) ===
                import os as _os_v60
                _v60_cap = float(_os_v60.environ.get("PGG2_LIVE_MAX_TRADE_SOL", 0.005))
                _v60_tol = float(_os_v60.environ.get("PGG2_V60_SIZE_CAP_TOLERANCE_SOL", 0.0001))
                if float(size_sol) > _v60_cap + _v60_tol:
                    log(f"PGG2-V60-SIZE-CAP-FATAL mint={_short(mint)} size={float(size_sol):.6f} cap={_v60_cap:.6f} ABORTING_SEND")
                    v48_live_buy_safe_failures += 1
                    if live_failed_buy_cooldown_ms > 0:
                        live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                    return False
                log(f"PGG2-V60-SEND-AUTHORIZED mint={_short(mint)} size={float(size_sol):.4f} true_edge={_v60_decision.true_edge_sol:+.6f} tx_digest={_v60_decision.tx_digest[:16]}")
            else:
                log(f"PGG2-V60-SEND-BLOCKED mint={_short(mint)} blocker=v60_module_unavailable size={float(size_sol):.4f}")
                v48_live_buy_safe_failures += 1
                if live_failed_buy_cooldown_ms > 0:
                    live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                return False
"""

if anchor_block not in src:
    print("PATCH-2-HOOK: anchor not found, aborting", file=sys.stderr)
    sys.exit(1)
if "PGG2-V60-SEND-AUTHORIZED" not in src:
    src = src.replace(anchor_block, v60_hook + anchor_block, 1)
    print("PATCH-2-HOOK: applied")
else:
    print("PATCH-2-HOOK: already present")

open(HARNESS, "w").write(src)
print(f"PATCH-WRITE: harness size now {len(src)} bytes")
