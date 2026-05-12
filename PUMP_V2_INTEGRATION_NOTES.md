# Pump.fun v2 instruction integration notes

Status: **probe-only, NOT built**. Created 2026-05-11 as part of v30 workstream.

## What Pump announced

Per Pump.fun's recent public documentation, three new bonding-curve
instructions have been added:

- `buy_v2`
- `sell_v2`
- `buy_exact_quote_in_v2`

Documented properties:

- "No optional accounts" — mandatory accounts only, same order for all coins.
- This is positioned as a replacement for the legacy `buy_exact_sol_in` /
  `sell_exact_in` path, which currently requires the dynamic Pump fee
  buyback/social PDA discovery that the v30 quote-coverage repair patches
  (`prewarm_pump_buyback_pair_from_sig` + sim-select waterfall in
  `pgg2_direct_pump.py`).

## Why we are not building yet

To build a v2 instruction safely from this repository we need the following,
from an **authoritative source** (official Pump IDL or SDK release):

1. **Anchor discriminator bytes** (8 bytes each) for:
   - `buy_v2`
   - `sell_v2`
   - `buy_exact_quote_in_v2`
2. **Mandatory account list and order** for each instruction. Specifically:
   - Whether the legacy buyback fee recipient + social fee PDA are still
     present, dropped, or replaced by canonical PDAs derived from the program.
   - Whether the `fee_config` PDA / `pump_amm_fee_config` PDA participates.
   - Whether `user_volume_accumulator` / `global_volume_accumulator` PDAs
     remain.
   - Whether `creator_vault` / `creator_vault_authority` accounts remain.
3. **Instruction data layout**:
   - `buy_v2(amount: u64, max_sol_cost: u64)` vs `(max_sol_cost: u64, amount: u64)` etc.
   - `buy_exact_quote_in_v2(quote_in_lamports: u64, min_tokens_out: u64)` — exact field order and types.
   - `sell_v2(token_amount: u64, min_sol_output: u64)` — exact field order.
4. **Compute unit guidance** — the legacy `set_compute_unit_limit` of
   220_000 may be obsolete; v2 likely has different CU profile.

Without all four of these, attempting to construct a v2 transaction would
require guessing layout. Guessing in real-live mode would either be silently
rejected (best case) or commit malformed instructions (worst case).
**Refused under v30 hard-safety rules.**

## What the probe currently does

`DirectPumpQuoteBroker.probe_pump_v2_buy(mint, amount_sol)` in
`pgg2_direct_pump.py:447` (approximate line):

- Honors `PGG2_DIRECT_PUMP_V2_PROBE` env (default off in production launchers,
  on in the v30 dry-live shadow-lab launcher).
- Hard-refuses in real live mode (`self.mode == "live"` → returns
  `v2_probe_refused_live_mode`).
- Otherwise returns:
  ```python
  {
    "v2_probe_attempted": True,
    "v2_probe_build_ok": False,
    "v2_probe_sim_ok": False,
    "v2_probe_error": "v2_idl_unavailable: need authoritative discriminator + account list ...",
  }
  ```
- Emits log line `PGG2-DIRECT-V2-PROBE blocked mint=... reason=v2_idl_unavailable`.

The shadow lab includes these fields in every record so the v2 probe state is
auditable in the report:

- `v2_probe_attempted`
- `v2_probe_build_ok`
- `v2_probe_sim_ok`
- `v2_probe_error`

The report's `## 0. QUOTE COVERAGE` section surfaces:
```
Pump v2 probe:
  attempted=N build_ok=0 sim_ok=0
    err=v2_idl_unavailable: need authoritative discriminator + accou cnt=N
```

## Plan once IDL is available

1. Place the official IDL JSON in `idl/pump_v2.json` (gitignored or
   versioned — TBD).
2. Generate Anchor discriminators with the standard formula:
   `sha256("global:<name>")[0:8]` and store them as constants in
   `pgg2_direct_pump.py`.
3. Add `build_v2_buy(mint, amount_sol, slippage)` and matching
   `build_v2_sell(mint, token_amount, slippage)` with the new account list.
4. Replace the body of `probe_pump_v2_buy` with a real build → sim path:
   - Build v2 tx with the canonical account list (no optional remaining).
   - `sign_transaction` + `simulate_signed`.
   - Record `v2_probe_build_ok` + `v2_probe_sim_ok` + `v2_probe_quote_equivalent_out`.
5. Compare to the legacy `build_buy` quote_tokens: if v2 sim consistently
   produces equivalent or better output AND no missing-pair failures, add an
   env switch `PGG2_DIRECT_PUMP_USE_V2=1` (dry-live only). Real-live still
   requires a separate later workstream and a fresh canary.

## What to do if you learn the IDL out-of-band

Add the constants and the build method, but keep `PGG2_DIRECT_PUMP_V2_PROBE`
the only switch that runs them initially. Validate via shadow lab for ≥30
candidates and confirm `v2_probe_sim_ok` is consistently `True` before
proposing the dry-live actual-entry switch.

Do NOT enable real live with v2 until:
- v2 probe sim pass rate >= 99% over 100+ samples
- v2 dry-live actual-entry rule has been validated by the causal rule miner
  with the same gates as legacy rules.
