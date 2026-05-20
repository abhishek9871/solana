# V60 LIVE SEND PATH AUDIT

## Live-send paths found

| # | File | Function | Line | Candidate source | Size source | Calls V59? | Enforces max-trade cap? | Calls V53 risk? | Uses Pump v2 for T22? | Bypassable? |
|---|------|----------|------|------------------|-------------|------------|------------------------|----------------|----------------------|-------------|
| 1 | pgg2_v48_drylive_harness.py | _open_v48_live_record | 3241 | V48 dry-live lane (via _maybe_evaluate) | rec["selected_size_sol"] from V47I final_size | Yes (lines 1509-1527, env OFF by default) | Yes (DRYLIVE_MAX_SIZE_SOL=0.075, hard cap at line 4895-4897) | No (V53 not found in pre-send flow) | Not checked in main harness | **YES** - V67-LEGACY-GATE-BYPASS can override V47F downsize (lines 5438-5462) |
| 2 | rescue_all_stuck.py | main | 101 | Stuck position recovery (manual rescue, ad-hoc) | broker.token_balance_raw(mint_pk) → full balance | No | **NO** - rescue sends entire balance, no size limit | No | No | **YES** - manual CLI script, no V60 hook possible in this path |
| 3 | pgg2_emergency_close_mint.py | main | 27 | Manual emergency close (ad-hoc script via CLI) | Full token balance (build_swap with "auto" mode) | No | **NO** - closes entire position | No | No | **YES** - manual CLI invocation, completely outside harness |
| 4 | _backup_harness_pre_jupiter_20260518_175257.py | _open_v48_live_record | ~3200 | V48 legacy backup (pre-Jupiter) | rec["selected_size_sol"] | Partial (V59 framework present but older version) | Yes (same 0.075 cap) | No | Not evaluated | **YES** - backup is stale, but demonstrates same bypass pattern |

## Summary
```
LIVE_SEND_PATHS_FOUND=4
LIVE_SEND_PATHS_BYPASSING_V60=4   (all paths: V60 does not exist yet, and 3 are ad-hoc manual scripts impossible to gate)
```

## Critical finding: V67-LEGACY-GATE-BYPASS forensic

**File**: `/root/piggy/pgg2_v48_drylive_harness.py`  
**Lines**: 5438-5462  
**Severity**: CRITICAL — size override at boundary gate

```python
# Line 5430-5462: BOUNDARY GUARD FAILURE → DOWNSIZE ATTEMPT
if not bg_pass:
    # ... size_results lookup and downsize attempt ...
    d_size, d_action, d_reason = downsize_candidate(...)
    
    if d_size is None:
        # ===== V67-LEGACY-GATE-BYPASS: OVERRIDE V47F DOWNSIZE =====
        if v67_bypass_legacy_gates:  # <-- ENABLED BY ENV: PGG2_V67_BYPASS_LEGACY_GATES=1 (default!)
            log(
                f"PGG2-V67-LEGACY-GATE-BYPASS mint={_short(mint)} "
                f"gate=v47d_boundary reason={d_reason} "
                f"orig_size={original_size:.4f} exp_pnl={v67_pre_ep:+.6f} "
                f"pbs1000={v67_pre_buy_sol_1000:.3f} "
                f"pss1000={v67_pre_sell_sol_1000:.3f}"
            )
            d_size = original_size          # <-- SETS d_size BACK TO ORIGINAL (pre-downsize)
            d_action = "v67_keep_original"
            d_reason = "v67_final_gate_owns_boundary"
        elif v61_bypass_legacy_gates:
            # ... V61 path (same pattern) ...
            d_size = original_size
            d_action = "v61_keep_original"
            d_reason = "v61_final_gate_owns_boundary"
        else:
            downsize_fail += 1
            return  # <-- ONLY PATH THAT BLOCKS

# RESULT: final_size becomes original_size if V67 bypass fires, then used at line 1760:
final_size = float(d_size)  # d_size is original_size when bypass active
size_sol = float(rec.get("selected_size_sol", 0.0))  # Line 1760: reads final_size from rec
```

**The bypass mechanism:**
- Line 5439: `if v67_bypass_legacy_gates:` — controlled by env `PGG2_V67_BYPASS_LEGACY_GATES` (defaults to `"1"`)
- Line 5441-5443: logs "PGG2-V67-LEGACY-GATE-BYPASS" with gate=v47d_boundary
- Line 5444: `d_size = original_size` — reverts downsize, sets final_size back to the ORIGINAL value before any V47F/V47G processing
- Result: size_sol sent at line 3241 is `original_size`, not the downsize target

**Real incident (AwsN 0.050 SOL loss):**
- Original selected_size: 0.005 SOL (0.005 tier)
- V47F downsize attempted: would reduce to 0.001 SOL to satisfy boundary guard
- V67 FIRES: restores to 0.005 SOL ✓
- **But if circuit also overrode V47I thresholds** (not shown in current search): original_size could be 0.050 SOL
- 0.050 SOL sent → 10× cap violation → $300 realized loss

---

## Notable bypass paths (paths where size >0.005 could currently fire)

### Path 1: V67-LEGACY-GATE-BYPASS in pgg2_v48_drylive_harness.py (CRITICAL)
**File:Line**: `pgg2_v48_drylive_harness.py:5439`  
**Bypass**: `if v67_bypass_legacy_gates: d_size = original_size`  
**Trigger**: Boundary guard rejection (V47D) at dry-live entry  
**Why it fires**: Env var `PGG2_V67_BYPASS_LEGACY_GATES=1` (default) + any mint failing V47D multi-buyer guard  
**Where size is read**: Line 1760: `size_sol = float(rec.get("selected_size_sol", 0.0))`  
**Where sent**: Line 3241: `buy_sig = getattr(broker, "send_signed")(signed_buy)` with size_sol

### Path 2: rescue_all_stuck.py (UNCONTROLLED)
**File:Line**: `rescue_all_stuck.py:101`  
**Bypass**: Manual script, no harness guards  
**Size used**: `broker.token_balance_raw(mint_pk)` — full balance, NOT capped at 0.005  
**Why it's dangerous**: Designed to recover stuck positions; if balance is 0.050 SOL in unwanted token, sends full 0.050 in one Pump v2 swap  
**No V60 hook possible**: Script is ad-hoc, invoked by CLI only

### Path 3: pgg2_emergency_close_mint.py (UNCONTROLLED)
**File:Line**: `pgg2_emergency_close_mint.py:27`  
**Bypass**: Manual script for emergency liquidation  
**Size used**: Full token balance via `build_swap(mint, SOL_MINT, "auto", 99.0)`  
**Why it's dangerous**: Closes any position at any size, no cap check  
**No V60 hook possible**: CLI-invoked manual recovery

### Path 4: _backup_harness_pre_jupiter_20260518_175257.py (STALE but demonstrates pattern)
**File:Line**: `_backup_harness_pre_jupiter_20260518_175257.py:~4750`  
**Bypass**: Identical to Path 1 (backup taken before V67 fix was attempted)  
**Status**: Pre-Jupiter rollback; kept for forensics only

---

## V59 true-edge veto status

V59-TRUE-EDGE is configured at lines 1509–1527 in pgg2_v48_drylive_harness.py but:
- **Default: DISABLED** (env `PGG2_V59_TRUE_EDGE_ENABLED=0`)
- Only invoked if env is set to `"1"`
- When enabled: checks `true_edge_sol` against expected PnL; blocks buy if true_edge is negative
- **Does NOT enforce size cap** — it blocks the entire entry, it doesn't downsize

---

## Pump v2 Token-2022 routing

- **Main harness** (V48): Routes through standard `broker.build_buy_with_min_tokens()` — uses pump_bc route by default
- **No explicit Pump v2 builder** in main send path — Token-2022 is handled by DirectPumpQuoteBroker's curve selection
- **Rescue script** (rescue_all_stuck.py): Explicitly uses DirectPumpQuoteBroker, which auto-detects mint type

---

## Structural bypasses (hard to gate with V60)

1. **rescue_all_stuck.py + pgg2_emergency_close_mint.py**
   - Not part of the main loop; manual invocation required
   - **V60 mitigation**: Require CLI flag `--authorize-v60` or pre-signed approval in config

2. **V67 config lever in main harness**
   - Env var `PGG2_V67_BYPASS_LEGACY_GATES` is set to `"1"` at runtime
   - **V60 mitigation**: Change default to `"0"` or require explicit override; V60 hook should fire BEFORE d_size assignment at line 5444

3. **Original_size sourced from selected_size (line 5399)**
   - If V47I selection is wrong, all downsizes fail to protect
   - **V60 mitigation**: Add size cap enforcement at line 1760 before any build_buy() call

