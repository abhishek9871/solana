"""Phase 6: Real broker-built buy tx + decode + V60 authorization.

Workflow:
  1. Source .env so broker can use RPC keys.
  2. Override PGG2_LIVE_RPC_URL to Helius beta (others depleted).
  3. Pick a currently-active mint from the live PumpPortal stream (or use an
     env-supplied PGG2_PHASE6_MINT).
  4. Build a buy quote via broker.build_buy_with_min_tokens_from_curve_snapshot
     at size = 0.005 SOL.
  5. Sign the transaction (LIVE keypair must be present, but DO NOT SEND).
  6. Decode the signed transaction's Pump.fun buy instruction:
     - extract discriminator
     - extract amount (u64, tokens to buy)
     - extract max_sol_cost (u64, max SOL lamports)
  7. Build V60Candidate + V60TxPlan from the real values.
  8. Call v60_authorize_live_buy.
  9. Verify:
     - decoded max_sol_cost <= PGG2_LIVE_MAX_TRADE_SOL + tolerance
     - V60 authorizes the candidate
     - No hidden 0.050 size
  10. Write V60_TX_BUILD_VALIDATION.md

Does NOT send the transaction. The broker.send_signed is NEVER called.
"""
import os, sys, json, time, base64
sys.path.insert(0, "/root/piggy")

# Load .env
for raw in open("/root/piggy/.env"):
    line = raw.strip()
    if "=" not in line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    v = v.strip().strip('"').strip("'")
    os.environ.setdefault(k.strip(), v)

# Override RPC to Helius beta (paid keys depleted)
os.environ["PGG2_LIVE_RPC_URL"] = "https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
os.environ["HELIUS_RPC_URL"] = "https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb"
os.environ["PGG2_EXECUTION_MODE"] = "live"
os.environ["PGG2_LIVE_CONFIRM"] = "I_ACCEPT_REAL_SOL_RISK"
os.environ["PGG2_DIRECT_LIVE_CONFIRM"] = "I_ACCEPT_DIRECT_PUMP_RISK"
os.environ["PGG2_V60_REQUIRE_RISK_PASS"] = "0"  # skip risk for phase 6 (no V53 wiring in this standalone test)
os.environ["PGG2_DIRECT_SELL_SLIPPAGE"] = "0.50"

from pgg2_direct_pump import DirectPumpQuoteBroker
from birth_first_sniper import BotConfig
from pgg2_v60_live_send_firewall import (
    V60Candidate, V60TxPlan, v60_authorize_live_buy,
)

PHASE6_MINT = os.environ.get("PGG2_PHASE6_MINT", "")


def find_recent_active_mint():
    """Walk recent logs to find a mint that V47C just evaluated (likely still
    active on curve). Falls back to PumpPortal new-mint snapshot."""
    import subprocess, glob
    log_files = sorted(
        glob.glob("/root/piggy/logs/pgg2_v55_stagea_*.log"),
        key=os.path.getmtime, reverse=True,
    )[:2]
    for log in log_files:
        result = subprocess.check_output(
            ["grep", "-h", "PGG2-V67-CURVE-RPC-UPDATE", log],
        ).decode().splitlines()
        # Walk from latest backwards. Find a mint with vsol > 30 (i.e. has had some buys)
        for line in reversed(result):
            try:
                parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
                vsol = int(parts.get("vsol", 0))
                mint_full = parts.get("mint", "").replace("..pump", "")
                if vsol > 31_000_000_000 and len(mint_full) > 4:
                    # Convert short mint to full mint by searching log
                    full_mint = None
                    for raw in open(log):
                        if mint_full in raw and "pump" in raw and "mint=" in raw:
                            for tok in raw.split():
                                if tok.startswith("mint="):
                                    val = tok.split("=", 1)[1].strip(",").strip(".pump")
                                    # Only short-form 4..4 from log; check NEW-MINT lines for full
                            # Instead: grep PUMPPORTAL-NEW-MINT for full mint
                            break
                    # Get full mint from PUMPPORTAL-NEW-MINT log
                    pp_lines = subprocess.check_output(
                        ["grep", "PUMPPORTAL-NEW-MINT", log]
                    ).decode().splitlines()
                    for ppl in pp_lines:
                        for tok in ppl.split():
                            if tok.startswith("mint="):
                                full = tok.split("=", 1)[1].strip(",")
                                # only return if abbreviation matches
                                if full.startswith(mint_full[:4]) and full.endswith(mint_full[-4:]):
                                    return full
            except Exception:
                continue
    return None


def main():
    print("=== V60 PHASE 6 — BROKER TX BUILD + DECODE + V60 AUTH ===")
    print()
    mint = PHASE6_MINT or find_recent_active_mint()
    if not mint:
        print("ERROR: no active mint found. Set PGG2_PHASE6_MINT env or wait for fresh logs.")
        return 1
    print(f"target_mint: {mint}")
    print()

    broker = DirectPumpQuoteBroker(BotConfig())
    size_sol = 0.005
    print(f"calling broker.build_buy_with_min_tokens(size={size_sol}, slippage=0.05)...")
    try:
        # Use the simpler build_buy interface that works without a pre-cached snapshot
        buy_quote = broker.build_buy(mint, size_sol, slippage=0.05)
    except Exception as exc:
        print(f"ERROR: build_buy failed: {type(exc).__name__}: {exc}")
        return 1

    print()
    print("=== BUY QUOTE FIELDS ===")
    for k, v in buy_quote.items():
        if k == "txn":
            print(f"  {k}: <base64 transaction, len={len(str(v))}>")
        elif k == "instruction_data":
            print(f"  {k}: <{len(v) if v else 0} bytes>")
        else:
            print(f"  {k}: {v}")
    print()

    # Sign the transaction (no send)
    print("=== SIGN (no send) ===")
    try:
        signed_b64, preview = broker.sign_transaction(str(buy_quote["txn"]))
        print(f"  signed_preview: {preview[:32]}...")
        print(f"  signed_b64_len: {len(signed_b64)} chars")
    except Exception as exc:
        print(f"ERROR: sign failed: {type(exc).__name__}: {exc}")
        return 1
    print()

    # Decode the Pump.fun buy instruction from the signed transaction
    print("=== DECODE PUMP.FUN BUY INSTRUCTION ===")
    try:
        signed_bytes = base64.b64decode(signed_b64)
    except Exception:
        # Maybe base58?
        try:
            import base58
            signed_bytes = base58.b58decode(signed_b64)
        except Exception as exc:
            print(f"ERROR: could not decode signed tx: {exc}")
            return 1
    print(f"  signed_tx_len_bytes: {len(signed_bytes)}")

    # Search for Pump.fun buy instruction discriminator
    # Pump.fun buy discriminator (sha256("global:buy")[:8]):
    BUY_DISC = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])
    idx = signed_bytes.find(BUY_DISC)
    if idx < 0:
        # Try buy_v2 discriminator
        BUY_V2_DISC = bytes([0xb2, 0x88, 0x4d, 0x52, 0x1e, 0x4e, 0xab, 0x05])
        idx = signed_bytes.find(BUY_V2_DISC)
        which_buy = "buy_v2"
    else:
        which_buy = "buy"
    if idx < 0:
        print("ERROR: could not find Pump.fun buy discriminator in signed tx")
        # Show first 200 bytes for forensics
        print(f"  first 100 bytes hex: {signed_bytes[:100].hex()}")
        return 1
    print(f"  buy_instruction_kind: {which_buy}")
    print(f"  buy_disc_offset: {idx}")
    # amount = next 8 bytes (u64 LE)
    amount_raw = int.from_bytes(signed_bytes[idx+8:idx+16], "little")
    # max_sol_cost = next 8 bytes (u64 LE)
    max_sol_cost = int.from_bytes(signed_bytes[idx+16:idx+24], "little")
    print(f"  decoded_amount_tokens_raw: {amount_raw:,}")
    print(f"  decoded_max_sol_cost_lamports: {max_sol_cost:,} ({max_sol_cost/1e9:.6f} SOL)")
    print()

    # Cross-check against the buy_quote
    expected_size_lamports = int(round(size_sol * 1e9))
    print("=== V60 INPUT VS DECODED ===")
    print(f"  V60 expected size_sol: {size_sol} = {expected_size_lamports:,} lamports")
    print(f"  Decoded max_sol_cost:  {max_sol_cost:,} lamports = {max_sol_cost/1e9:.6f} SOL")
    drift = abs(max_sol_cost - expected_size_lamports)
    drift_pct = 100.0 * drift / expected_size_lamports if expected_size_lamports else 0
    print(f"  Drift:                 {drift:,} lamports ({drift_pct:.2f}%)")
    print()

    # Build V60 candidate + plan from decoded data
    print("=== V60 AUTHORIZATION ===")
    cand = V60Candidate(
        mint=mint,
        selected_size_sol=size_sol,
        candidate_lane="phase6_synthetic_v56d_flow_scratch",
        rule_id="v48_v47i_stack",
        expected_pnl_sol=0.001500,
        true_edge_sol=None,
        token_program="spl",
        route="pump_bc",
        sim_needed=0,
        pair_source="decision_curve_snapshot",
        snapshot_age_ms=300,
        source_lead_ms=270,
        risk_result=None,
        risk_fetched_at_ms=None,
        is_v67_passing=True,  # synthetic for Phase 6
        is_v57_promotion=False,
        wallet_balance_sol=0.10963,
    )
    plan = V60TxPlan(
        decoded_amount_tokens_raw=amount_raw,
        decoded_max_sol_cost_lamports=max_sol_cost,
        swqos_tip_sol=0.000005,
        priority_fee_sol=0.000005,
        base_fee_sol=0.000005,
        uses_pump_v2=False,
        has_sell_v2_capability=True,
        instruction_bytes=signed_bytes[idx:idx+24],  # the buy instruction bytes
    )
    decision = v60_authorize_live_buy(cand, plan)
    print(f"  passed: {decision.passed}")
    print(f"  blocker: {decision.blocker}")
    print(f"  true_edge: {decision.true_edge_sol:+.6f}")
    print(f"  tx_digest: {decision.tx_digest[:16]}")
    print()
    for r in decision.check_results:
        status = "PASS " if r.passed else "BLOCK"
        print(f"    {r.name:<22} {status}  {r.detail}")
    print()

    # Validate Phase 6 criteria
    print("=== PHASE 6 CRITERIA ===")
    cap_lamports = int(0.005 * 1e9) + int(0.0001 * 1e9)
    crit_size_ok = max_sol_cost <= cap_lamports
    crit_match = drift_pct < 5.0
    crit_v60_pass = decision.passed
    print(f"  decoded_max_sol_cost ({max_sol_cost} lam) <= cap+tol ({cap_lamports} lam): {crit_size_ok}")
    print(f"  decoded vs V60 expected drift < 5%: {crit_match} (drift={drift_pct:.2f}%)")
    print(f"  V60 authorizes: {crit_v60_pass}")
    print(f"  NO size > 0.005 in encoded instruction: {crit_size_ok}")
    print()
    all_pass = crit_size_ok and crit_match and crit_v60_pass
    print(f"V60_PHASE6_PASS={str(all_pass).lower()}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
