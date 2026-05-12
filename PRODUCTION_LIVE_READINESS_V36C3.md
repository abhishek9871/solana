# PRODUCTION_LIVE_READINESS_V36C3

This document audits whether the v36c-3 release is ready to start real-live trading. **It is NOT a production-live launcher; it is a gate audit.** The conclusion drives whether the one-entry smoke can run today.

## Gate audit

| # | Gate | Required | Actual | Pass/Fail | Evidence |
|---|---|---|---|:---:|---|
| 1 | Bot stopped before release | yes | `pgrep -af "python -u PGG2.py"` returns nothing; no tmux sessions | ✅ PASS | shell check after stop |
| 2 | Release files checksummed (local) | yes | 22 files in `RELEASE_V36C3_CHECKSUMS.txt` | ✅ PASS | file present |
| 3 | Release files checksummed (remote) | yes | Remote `sha256sum` returned exact match for `PGG2.py, pgg2_live_raptor.py, pgg2_direct_pump.py, birth_first_sniper.py, pgg2_v33_verify_pilot.sh, start_pgg2_v30_shadowlab_drylive.sh, start_pgg2_attack_paper.sh, data/v33_*.json` | ✅ PASS | recorded in Phase 1 output |
| 4 | AST parse local + remote | yes | `ast.parse` clean both ends for all 4 bot source files | ✅ PASS | run during v36c-3 sync |
| 5 | Git release commit on main | yes | pending Phase 3 commit | ⏳ TODO | next step |
| 6 | Release tag exists | yes | pending Phase 3 tag | ⏳ TODO | next step |
| 7 | Real-live confirm env absent | yes | `PGG2_LIVE_CONFIRM` not in `/proc/<pid>/environ` of any process in v36c-3 run | ✅ PASS | log analysis |
| 8 | Wallet file exists | yes | `/root/piggy/live_wallet.key` (per `PGG2_WALLET_KEYPAIR`) — operator must verify | ⚠️ OPERATOR | operator step |
| 9 | Public key printed | yes | `PGG2-LIVE: ... wallet=Cw4G..L7M7` in v36c-3 boot log | ✅ PASS | log line |
| 10 | Wallet balance checked | yes | operator must run `solana balance` against pubkey before live | ⚠️ OPERATOR | operator step |
| 11 | Route limited to pump_bc | yes | `cost_model_route=pump_bc` on every v36c-3 SELL; broker route check `if curve.complete: return self.build_pumpswap_sell(...)` else direct pump_bc | ✅ PASS | log lines + code |
| 12 | sim_needed=0 actual entries only | yes | every v36c-3 ENTRY-SNAPSHOT-BANK had `pair_source ∈ {current_sig, observed_raw_rpc}` (no `sim_selected:` prefix); scalp explicitly rejects `pair_source.startswith("sim_selected:")` | ✅ PASS | code + log |
| 13 | cost_model_confidence=proven | yes | every v36c-3 SELL shows `cost_model_confidence=proven` | ✅ PASS | log lines |
| 14 | Entry Snapshot Bank live eligibility audited | yes | `PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0` by default; broker `mode=="live"` path emits `PGG2-LIVE-EQUIVALENCE-BLOCK` and skips the bank | ✅ PASS | code path verified |
| 15 | Max real entries for smoke = 1 | yes | `PGG2_DRYLIVE_PILOT_MAX_ENTRIES=1, PGG2_SCALP_MAX_ENTRIES=0` will be set in smoke launcher | ⏳ TODO | launcher step |
| 16 | Live max open positions = 1 | yes | `PIGGY_MAX_OPEN_POSITIONS=1` will be set in smoke launcher | ⏳ TODO | launcher step |
| 17 | Live trade size = 0.015 SOL | yes | `PGG2_LIVE_MAX_TRADE_SOL=0.015, PGG2_LIVE_MIN_TRADE_SOL=0.015` will be set in smoke launcher | ⏳ TODO | launcher step |
| 18 | Live session loss cap tight | yes | `PGG2_LIVE_MAX_SESSION_LOSS_SOL=0.005` will be set in smoke launcher (single-entry smoke) | ⏳ TODO | launcher step |
| 19 | Stop on any negative all_in | yes | risk worker's clamp threshold = `-0.00030` (scalp) / `-0.00075` (primary); session cap also caps | ✅ PASS | policy values |
| 20 | Stop on close-fail | yes | `_do_close` finally block clears risk_owned + remove_position; any `PGG2-RISK-CLOSE-FAIL` increments a counter; smoke launcher will exit on first fail | ⏳ TODO | requires smoke kill-on-event hook |
| 21 | Stop on token mismatch | yes | `PGG2-POSITION-TOKEN-MISMATCH-FATAL` already kills the entry (returns None); 0 in v36c-3 | ✅ PASS | code |
| 22 | Stop on stale quote | yes | `PGG2-PILOT-PREENTRY-BLOCK blocker=stale_quote` + `PGG2-SCALP-PREENTRY-BLOCK blocker=stale_quote` already implemented | ✅ PASS | code |
| 23 | Logs streaming | yes | `stream_logs.bat` available for local tail | ✅ PASS | file present |
| 24 | Kill command ready | yes | `tmux kill-session -t bot && pkill -9 -f "python -u PGG2.py"` documented | ✅ PASS | runbook |

## Honest Entry Snapshot Bank live-equivalence audit (Phase 5)

This is the gate that determines whether **today's** smoke can use Entry Snapshot Bank or must use the protected-hold primary fallback only.

### Dry-live behaviour (what we observed in v36c-3)

In `mode=quote` with `quote_shadow_positions=1`, the broker `RaptorLiveBroker.close()` path is:
1. Read `quote_shadow_tokens[mint]` (set at open from the locked quote).
2. Check `is_risk_owned(mint)` → True for pilot/scalp positions.
3. Read `_recent_sell_quotes[mint]` (populated by the prior shadow-lab sell-quote build, age usually < 1000 ms).
4. Compute `expected_out` = the cached quote's amount-out.
5. **No tx is signed or sent.** The position is popped from `self.positions`, ledger updated, `PGG2-QUOTE-SHADOW-SELL` emitted.

The dry-live entry-snapshot-bank is therefore zero-latency relative to the locked quote — there is no on-chain race because nothing went on chain.

### Live behaviour (what would actually happen with `mode=live`)

The broker.close path in live (`self.mode == "live"`, `quote_only=False`):
1. Build an actual sell swap tx via Solana Tracker (or direct pump).
2. Sign it with the keypair.
3. Submit to the network.
4. Wait for confirmation (`PGG2_LIVE_CONFIRM_TIMEOUT_SEC=8.0`).
5. The buy tx that opened the position was a separate submission earlier — between buy confirmation and sell submission, the market can move arbitrarily.

The "entry snapshot" used to decide the bank exists in dry-live but **does not exist in real-live** because:
- The live buy tx has not been confirmed at the moment ESB would fire.
- Even if buy is confirmed instantly, the sell tx still has to be built, signed, sent, and confirmed.
- The locked sell quote used in dry-live was never on chain — its price guarantee evaporates the moment a real buy moves the pool.

### Verdict

**ESB live-equivalence: NOT PROVEN.**

Concrete reasoning:
- The dry-live bank relies on the broker's cached sell quote, which in live mode would be stale by the time both txs confirm (typical 2-tx Solana latency 1.5–4 s).
- No atomic buy+sell bundle implementation exists in the current codebase.
- No Jito / sequential atomic execution wrapper is wired up.
- A live buy + naive live sell will not reproduce dry-live's instantaneous bank.

### Action

Per the release spec's recommendation logic:
> If live-equivalence fails: do not live trade; implement live-equivalent Entry Snapshot Bank or Jito/atomic execution path.

**Live smoke today is BLOCKED for any rule whose safety depends on Entry Snapshot Bank** (both scalp and the post-v36c-2 primary path). The smoke launcher (Phase 6) must therefore:
- disable Entry Snapshot Bank in live mode (the `PGG2-LIVE-EQUIVALENCE-BLOCK` guard already enforces this with the default `PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0`),
- disable the scalp path entirely (it cannot operate safely without ESB in live),
- run only the **protected-hold primary rule** (`v33_quote_edge_150_C` with policy `C_moonshot_hold_protected_clamp_v33`) for a single one-entry smoke,
- size 0.015 SOL, max_entries=1, session_loss_cap=0.005 SOL.

The smoke is then a one-entry test of the original v33 primary, which has been holdout-validated independently (10W/0L in the v33 holdout). It is NOT a test of ESB.

To unblock ESB in live later, one of the following must be implemented + replayed in a separate session:
1. Atomic buy+sell bundle (Jito) so both txs land in the same block.
2. A live wrapper that confirms buy → reads actual tokens received → fires sell with the locked sell quote AND verifies the sell quote's age < `PGG2_RISK_OWNED_CLOSE_QUOTE_MAX_AGE_MS` after buy confirmation; if older, skip ESB and fall through to the risk-worker hold.

Both options require additional code + a fresh dry-live smoke before they can be cleared.

## Conclusion

- Release validation: **PASS** for dry-live.
- Live smoke for **protected-hold primary only**: gates 5/6/15/16/17/18/20 still to be completed in the smoke-launcher creation step (Phase 6).
- Live smoke for **ESB-dependent rules**: **BLOCKED** until live-equivalence is implemented.
