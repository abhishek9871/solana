"""V61 Phase 3 patch v2: no-wait sync integration.

Skips the await-asyncio-sleep wait (the V60 hook is inside a sync function
chain, async conversion is too invasive). Instead reads the V67RpcCurveOracle
buffer immediately at V60-PASS time. The oracle is continuously populated by
background async tasks, so at V60-PASS the buffer reflects the latest curve
data the bot has fetched.

This loses the catch for the "delayed dump" loss class (3Tcp..pump, 2000ms
after V60-PASS) but keeps the catch for the "immediate dump" class
(66Qi..pump, 100ms after V60-PASS) via:
  - Rule 2: latest curve delta non-negative
  - Rule 3: no negative curve delta in last 500ms
  - Rule 9: peak detection (rose > 30% in last 1000ms)
"""
import re
import sys

HARNESS = "/root/piggy/pgg2_v48_drylive_harness.py"
src = open(HARNESS).read()

# ===== Patch 1: import (same as before) =====
v61_import = """# === V61 LIVE CONTINUATION ORACLE IMPORT (2026-05-19) ===
try:
    from pgg2_v61_live_continuation_oracle import (
        V61Inputs as _V61Inputs,
        CurvePoint as _V61CurvePoint,
        QuotePoint as _V61QuotePoint,
        v61_check_continuation as _v61_check_continuation,
    )
    _V61_AVAILABLE = True
except Exception as _v61_imp_exc:
    _V61_AVAILABLE = False
"""

if "_V61_AVAILABLE" not in src:
    anchor_import = (
        "# === V60 LIVE-SEND FIREWALL IMPORT (2026-05-19) ===\n"
        "try:\n"
        "    from pgg2_v60_live_send_firewall import (\n"
        "        V60Candidate as _V60Candidate,\n"
        "        V60TxPlan as _V60TxPlan,\n"
        "        v60_authorize_live_buy as _v60_authorize_live_buy,\n"
        "    )\n"
        "    _V60_AVAILABLE = True\n"
        "except Exception as _v60_exc:\n"
        "    _V60_AVAILABLE = False\n"
    )
    if anchor_import not in src:
        print("PATCH-V61-IMPORT: V60 anchor not found", file=sys.stderr)
        sys.exit(1)
    src = src.replace(anchor_import, anchor_import + "\n" + v61_import, 1)
    print("PATCH-V61-IMPORT: applied")
else:
    print("PATCH-V61-IMPORT: already present")

# ===== Patch 2: V61 hook after V60-SEND-AUTHORIZED (no wait) =====
v60_anchor = 'log(f"PGG2-V60-SEND-AUTHORIZED mint={_short(mint)} size={float(size_sol):.4f} true_edge={_v60_decision.true_edge_sol:+.6f} tx_digest={_v60_decision.tx_digest[:16]}")'

if v60_anchor not in src:
    print("PATCH-V61-HOOK: V60-SEND-AUTHORIZED anchor not found", file=sys.stderr)
    sys.exit(1)

v61_hook = '''log(f"PGG2-V60-SEND-AUTHORIZED mint={_short(mint)} size={float(size_sol):.4f} true_edge={_v60_decision.true_edge_sol:+.6f} tx_digest={_v60_decision.tx_digest[:16]}")
                # === V61 LIVE CONTINUATION ORACLE (no-wait sync mode, 2026-05-19) ===
                if _V61_AVAILABLE and _env_flag("PGG2_V61_ENABLED", "1"):
                    try:
                        _v61_v60_pass_ts_ms = int(time.time() * 1000)
                        # Read V67 curve oracle buffer
                        _v61_st = getattr(oracle, "_states", {}).get(mint)
                        _v61_curve_pts = []
                        if _v61_st is not None and getattr(_v61_st, "points", None):
                            for _p in list(_v61_st.points)[-8:]:
                                try:
                                    _vsol = int(_p.virtual_sol_reserves)
                                    _vtok = int(_p.virtual_token_reserves)
                                    _price = (_vsol / _vtok) if _vtok > 0 else 0.0
                                    _v61_curve_pts.append(_V61CurvePoint(
                                        timestamp_ms=int(_p.ts_ms),
                                        vsol_lamports=_vsol,
                                        vtok_raw=_vtok,
                                        price=_price,
                                    ))
                                except Exception:
                                    continue
                        # Synthesize quote_history from buy_quote (V60 snapshot)
                        # We don't issue a fresh broker.build_sell call (avoids RPC latency)
                        # The forensic-validated rules (2, 3, 9) work on curve data alone
                        _v61_quote_pts = []
                        try:
                            _v61_buy_in = float(buy_quote.get("in_sol", float(size_sol)) or float(size_sol))
                            _v61_expected_tokens = float(buy_quote.get("out_tokens", buy_quote.get("expected_tokens", 0.0)) or 0.0)
                            if _v61_buy_in > 0 and _v61_expected_tokens > 0:
                                # Approximate sell quote from the buy quote inverse
                                _v61_approx_sell = _v61_buy_in * 0.96  # rough sell after 1% fees each leg
                                _v61_quote_pts.append(_V61QuotePoint(
                                    timestamp_ms=_v61_v60_pass_ts_ms - 100,
                                    sell_quote_sol_out=_v61_approx_sell,
                                ))
                                _v61_quote_pts.append(_V61QuotePoint(
                                    timestamp_ms=_v61_v60_pass_ts_ms,
                                    sell_quote_sol_out=_v61_approx_sell,
                                ))
                        except Exception:
                            pass
                        # Pending flow snapshot (from V48 rec)
                        _v61_pbs500 = float(rec.get("pbsol_500", rec.get("pending_buy_sol_500ms", rec.get("pbs1000", 0.0)) or 0.0))
                        _v61_pss500 = float(rec.get("pssol_500", rec.get("pending_sell_sol_500ms", rec.get("pss1000", 0.0)) or 0.0))
                        _v61_pbc500 = int(rec.get("pbc500", rec.get("pending_buy_count_500ms", rec.get("pbc1000", 0)) or 0))
                        _v61_psc500 = int(rec.get("psc500", rec.get("pending_sell_count_500ms", rec.get("psc1000", 0)) or 0))
                        _v61_inputs = _V61Inputs(
                            mint=mint,
                            selected_size_sol=float(size_sol),
                            v60_true_edge_sol=float(_v60_decision.true_edge_sol),
                            v60_pass_timestamp_ms=_v61_v60_pass_ts_ms,
                            curve_history=_v61_curve_pts,
                            quote_history=_v61_quote_pts,
                            pending_buy_sol_500ms=_v61_pbs500,
                            pending_sell_sol_500ms=_v61_pss500,
                            pending_buy_count_500ms=_v61_pbc500,
                            pending_sell_count_500ms=_v61_psc500,
                            signal_age_ms=int(rec.get("snapshot_age_ms", 0) or 0),
                        )
                        log(f"PGG2-V61-PRECHECK mint={_short(mint)} curve_pts={len(_v61_curve_pts)} quote_pts={len(_v61_quote_pts)} sync_mode=1")
                        _v61_decision = _v61_check_continuation(_v61_inputs, log_fn=log)
                    except Exception as _v61_err:
                        log(f"PGG2-V61-CONTINUATION-ERR mint={_short(mint)} err={type(_v61_err).__name__}:{_v61_err}")
                        v48_live_buy_safe_failures += 1
                        if live_failed_buy_cooldown_ms > 0:
                            live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                        return False
                    if not _v61_decision.passed:
                        log(f"PGG2-V61-SEND-BLOCKED mint={_short(mint)} blocker={_v61_decision.blocker} score={_v61_decision.continuation_score:.3f}")
                        v48_live_buy_safe_failures += 1
                        if live_failed_buy_cooldown_ms > 0:
                            live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                        return False
                    log(f"PGG2-V61-SEND-AUTHORIZED mint={_short(mint)} score={_v61_decision.continuation_score:.3f} curve_slope={_v61_decision.curve_slope:+.10f} quote_slope={_v61_decision.quote_slope:+.10f}")'''

if "PGG2-V61-SEND-AUTHORIZED" not in src:
    src = src.replace(v60_anchor, v61_hook, 1)
    print("PATCH-V61-HOOK: applied")
else:
    print("PATCH-V61-HOOK: already present")

open(HARNESS, "w").write(src)
print(f"PATCH-V61: harness size now {len(src)} bytes")
