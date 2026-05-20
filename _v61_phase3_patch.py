"""Patch pgg2_v48_drylive_harness.py: wire V61 continuation oracle after V60.

Inserts after the existing V60-SEND-AUTHORIZED log line, before the BUY-SEND
sequence. Awaits 250ms (or until ≥1 fresh curve update arrives, max 500ms),
builds V61Inputs from the V67RpcCurveOracle's per-mint state, plus a fresh
sell quote, and calls v61_check_continuation. Block on fail.
"""
import re, sys

HARNESS = "/root/piggy/pgg2_v48_drylive_harness.py"
src = open(HARNESS).read()

# ===== Patch 1: import =====
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
    anchor = (
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
    if anchor not in src:
        print("PATCH-V61-IMPORT: V60 anchor not found", file=sys.stderr); sys.exit(1)
    src = src.replace(anchor, anchor + "\n" + v61_import, 1)
    print("PATCH-V61-IMPORT: applied")
else:
    print("PATCH-V61-IMPORT: already present")

# ===== Patch 2: V61 oracle call after V60-SEND-AUTHORIZED =====
v60_authorize_log = 'log(f"PGG2-V60-SEND-AUTHORIZED mint={_short(mint)} size={float(size_sol):.4f} true_edge={_v60_decision.true_edge_sol:+.6f} tx_digest={_v60_decision.tx_digest[:16]}")'

if v60_authorize_log not in src:
    print("PATCH-V61-HOOK: V60-SEND-AUTHORIZED anchor not found", file=sys.stderr); sys.exit(1)

v61_hook = '''log(f"PGG2-V60-SEND-AUTHORIZED mint={_short(mint)} size={float(size_sol):.4f} true_edge={_v60_decision.true_edge_sol:+.6f} tx_digest={_v60_decision.tx_digest[:16]}")
                # === V61 LIVE CONTINUATION ORACLE (post-V60 confirmation, 2026-05-19) ===
                if _V61_AVAILABLE:
                    try:
                        import asyncio as _asyncio_v61
                        _v61_v60_pass_ts_ms = int(time.time() * 1000)
                        _v61_wait_ms = int(os.environ.get("PGG2_V61_POST_V60_WAIT_MS", "250"))
                        _v61_max_wait_ms = int(os.environ.get("PGG2_V61_MAX_WAIT_MS", "500"))
                        # Wait for ≥1 fresh curve update or max wait
                        _v61_st = getattr(oracle, "_states", {}).get(mint)
                        _v61_baseline_pt_count = len(_v61_st.points) if _v61_st is not None else 0
                        _v61_wait_start = _v61_v60_pass_ts_ms
                        _v61_done = False
                        while True:
                            await _asyncio_v61.sleep(0.025)
                            _v61_now = int(time.time() * 1000)
                            _v61_st = getattr(oracle, "_states", {}).get(mint)
                            _v61_cur_pt_count = len(_v61_st.points) if _v61_st is not None else 0
                            if _v61_cur_pt_count > _v61_baseline_pt_count and (_v61_now - _v61_wait_start) >= _v61_wait_ms:
                                _v61_done = True
                                break
                            if (_v61_now - _v61_wait_start) >= _v61_max_wait_ms:
                                break
                        _v61_actual_wait_ms = int(time.time() * 1000) - _v61_v60_pass_ts_ms
                        # Build curve history from oracle state
                        _v61_curve_pts = []
                        if _v61_st is not None and _v61_st.points:
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
                        # Build a fresh sell quote NOW + one snapshot quote at V60 time
                        _v61_quote_pts = []
                        try:
                            # Snapshot quote: derived from initial buy_quote out_tokens + current sell quote
                            _v61_expected_tokens = float(buy_quote.get("out_tokens", buy_quote.get("expected_tokens", 0.0)) or 0.0)
                            if _v61_expected_tokens > 0:
                                _v61_snap_sell = broker.build_sell(mint, f"{_v61_expected_tokens:.6f}", 0.30)
                                _v61_snap_out = float(broker.rate_amount_out(_v61_snap_sell))
                                _v61_quote_pts.append(_V61QuotePoint(
                                    timestamp_ms=_v61_v60_pass_ts_ms - 50,
                                    sell_quote_sol_out=_v61_snap_out,
                                ))
                                _v61_fresh_sell = broker.build_sell(mint, f"{_v61_expected_tokens:.6f}", 0.30)
                                _v61_fresh_out = float(broker.rate_amount_out(_v61_fresh_sell))
                                _v61_quote_pts.append(_V61QuotePoint(
                                    timestamp_ms=int(time.time() * 1000),
                                    sell_quote_sol_out=_v61_fresh_out,
                                ))
                        except Exception as _v61_quote_err:
                            log(f"PGG2-V61-QUOTE-BUILD-ERR mint={_short(mint)} err={type(_v61_quote_err).__name__}:{_v61_quote_err}")
                        # Pending flow snapshot
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
                            signal_age_ms=int(rec.get("snapshot_age_ms", 0) or 0) + _v61_actual_wait_ms,
                        )
                        log(f"PGG2-V61-PRECHECK mint={_short(mint)} wait_ms={_v61_actual_wait_ms} curve_pts={len(_v61_curve_pts)} quote_pts={len(_v61_quote_pts)}")
                        _v61_decision = _v61_check_continuation(_v61_inputs, log_fn=log)
                    except Exception as _v61_err:
                        log(f"PGG2-V61-CONTINUATION-ERR mint={_short(mint)} err={type(_v61_err).__name__}:{_v61_err}")
                        # Fail-closed on V61 error
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
                    log(f"PGG2-V61-SEND-AUTHORIZED mint={_short(mint)} score={_v61_decision.continuation_score:.3f} curve_slope={_v61_decision.curve_slope:+.10f} quote_slope={_v61_decision.quote_slope:+.10f}")
                else:
                    # V61 unavailable but required for live entry — fail-closed
                    log(f"PGG2-V61-SEND-BLOCKED mint={_short(mint)} blocker=v61_module_unavailable")
                    v48_live_buy_safe_failures += 1
                    if live_failed_buy_cooldown_ms > 0:
                        live_failed_buy_mint_until_ms[mint] = _now_ms() + live_failed_buy_cooldown_ms
                    return False'''

if "PGG2-V61-SEND-AUTHORIZED" not in src:
    src = src.replace(v60_authorize_log, v61_hook, 1)
    print("PATCH-V61-HOOK: applied")
else:
    print("PATCH-V61-HOOK: already present")

open(HARNESS, "w").write(src)
print(f"PATCH-V61: harness size now {len(src)} bytes")
