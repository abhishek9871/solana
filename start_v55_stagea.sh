#!/usr/bin/env bash
set -euo pipefail

cd /root/piggy

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# --------------------------------------------------------------------------
# V55 Stage A = V50B runner code + stage3-lean env config.
#
# Rationale (retro_speed_root_cause_2026_05_15.md): stage3+stage10 hit 5/6
# = 83% live WR on May 14 with three crucial differences from V50A+:
#   1. PGG2_V48_LIVE_SIMULATE_BUY_BEFORE_SEND was UNSET (off)
#   2. PGG2_V48_LIVE_POSTSIM_EXIT_GATE was UNSET (off)
#   3. PGG2_V48_MAX_SOURCE_LEAD_MS was 1200 (not 3500)
# These together added ~1-2 sec of decision-to-buy latency, staling the
# entry signal. V55 reverts them to stage3 values while keeping:
#   - Helius Sender SWQOS execution (paid, authorized policy)
#   - Apr-28 Token-2022 ATA correctness fixes
#   - V47G watchdog + V47F mid-hold dump abort + V47F hold caps
#   - V48 aggressive scratch_positive exit logic
# V55 does NOT apply the V53 SolanaTracker risk veto.
# --------------------------------------------------------------------------

# Idempotency: V55 uses the V50B runner — refuse if either is up.
# FIXED 2026-05-17: previously `pgrep -f` matched the full command line and
# false-positive'd on any SSH/tmux orchestration command that contained the
# script name as a string argument, blocking clean restarts. Now requires
# the process to actually be a python interpreter running the bot script.
_running_pids=$(ps -eo pid,comm,args 2>/dev/null | awk '$2 ~ /^python/ && $0 ~ /pgg2_v50b_stagea_live\.py|pgg2_v54_stagea_live\.py|pgg2_v55_stagea_live\.py/ {print $1}')
if [ -n "$_running_pids" ]; then
  echo "V55-LAUNCH-ABORT bot already running pid(s)=$_running_pids" >&2
  ps -fp $_running_pids >&2 || true
  exit 12
fi
unset _running_pids

# ----------------------------------------------------------------------------
# V67 PRE-FLIGHT: detect stuck token positions before launch (classic + Token-2022)
# ----------------------------------------------------------------------------
# The May-17 V67 run on 7syJ..pump left a Token-2022 ATA stuck because the
# bot's prior pre-flight only checked the classic Token program. This check
# is RUN ON THE LAUNCHER (not inside the harness) so a fresh launch refuses
# to start while there's unrescued inventory. Set PGG2_V67_SKIP_TOKEN_PREFLIGHT=1
# to bypass (only do this if you've manually reconciled the wallet state).
if [[ "${PGG2_V67_SKIP_TOKEN_PREFLIGHT:-0}" != "1" ]]; then
  _wallet_pub="${PGG2_WALLET_PUBKEY:-Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7}"
  _rpc_url=""
  if [[ -n "${HELIUS_RPC_URL:-}" ]]; then
    _rpc_url="$HELIUS_RPC_URL"
  elif [[ -n "${HELIUS_API_KEY:-}" ]]; then
    _rpc_url="https://mainnet.helius-rpc.com/?api-key=$HELIUS_API_KEY"
  elif [[ -n "${SOLANA_RPC_URL:-}" ]]; then
    _rpc_url="$SOLANA_RPC_URL"
  fi
  if [[ -n "$_rpc_url" ]]; then
    _preflight_out=$(python3 - <<PYEOF "$_rpc_url" "$_wallet_pub"
import json, sys, urllib.request
url, wallet = sys.argv[1], sys.argv[2]
total = 0
lines = []
for prog, label in (
    ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "classic"),
    ("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "token-2022"),
):
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        }).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        out = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception as exc:
        print("PREFLIGHT-RPC-ERROR " + label + " " + repr(exc), file=sys.stderr)
        sys.exit(0)
    for it in out.get("result", {}).get("value", []):
        info = it["account"]["data"]["parsed"]["info"]
        amt = int(info["tokenAmount"]["amount"] or 0)
        if amt > 0:
            total += 1
            lines.append(label + " mint=" + info["mint"][:12] + " raw=" + str(amt))
for ln in lines:
    print("STUCK " + ln)
print("TOTAL=" + str(total))
sys.exit(0 if total == 0 else 1)
PYEOF
    ) || _preflight_rc=$?
    _preflight_rc=${_preflight_rc:-0}
    echo "$_preflight_out"
    if [ "$_preflight_rc" -ne 0 ]; then
      echo "V55-LAUNCH-ABORT stuck token positions detected — rescue first or set PGG2_V67_SKIP_TOKEN_PREFLIGHT=1 to bypass" >&2
      exit 13
    fi
    unset _preflight_out _preflight_rc
  else
    echo "V55-LAUNCH-WARN no RPC URL in env (HELIUS_RPC_URL / HELIUS_API_KEY / SOLANA_RPC_URL) — skipping pre-flight token check"
  fi
  unset _wallet_pub _rpc_url
fi

# V55-specific knobs (mirror V50B layer).
export PGG2_V50B_LIVE=1
export PGG2_V50B_SWQOS_TIP_SOL="${PGG2_V50B_SWQOS_TIP_SOL:-0.000005}"
export PGG2_V50B_MAX_TIP_SOL="${PGG2_V50B_MAX_TIP_SOL:-0.000005}"
# Stage3 had failed_buy_budget=0.003; V50A tightened to 0.00025 (12x).
# Restore stage3 value so the bot can retry on transient send failures.
export PGG2_V50B_STAGEA_FEE_BUDGET_SOL="${PGG2_V50B_STAGEA_FEE_BUDGET_SOL:-0.00300}"
export PGG2_V50B_MAX_FAILED_SENDS="${PGG2_V50B_MAX_FAILED_SENDS:-4}"
export PGG2_V50B_MAX_OPEN="${PGG2_V50B_MAX_OPEN:-2}"
# V55 smoke uses target=1 close (proves the config); promote to 3 for stage A and 10 for stage B by overriding.
export PGG2_V50B_MAX_CLOSES="${PGG2_V50B_MAX_CLOSES:-10}"
export PGG2_V48_TARGET_CLOSED_NONNEG="${PGG2_V48_TARGET_CLOSED_NONNEG:-10}"
export PGG2_V48_ALLOW_NEGATIVE_CLOSES="${PGG2_V48_ALLOW_NEGATIVE_CLOSES:-1}"
export PGG2_V50B_MAX_SECONDS="${PGG2_V50B_MAX_SECONDS:-1800}"
export PGG2_V50B_MAX_SECONDS="${PGG2_V50B_MAX_SECONDS:-900}"
export PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL="${PGG2_V50B_MAX_WALLET_DRAWDOWN_SOL:-0.0030}"
export PGG2_V50B_PRIORITY_FEE_MICROLAMPORTS="${PGG2_V50B_PRIORITY_FEE_MICROLAMPORTS:-100000}"
export PGG2_V50B_CU_LIMIT="${PGG2_V50B_CU_LIMIT:-200000}"
# 2026-05-17 REVERTED to proven edge config after micro-scalp lost money in 1 trade.
# Tested: micro-scalp assumed every buy → impulse upswing. Reality: most mints
# show flat-or-down curve in our 500ms hold window. Eat slippage + fees = guaranteed loss.
# Back to: hold 1500ms for actual price discovery, bank only real wins.
# 2026-05-18 REVERTED to V55-proven config (33s+0.020 lost -$1.29 on HDef):
# Hold 1500ms (V55 default — emergency_timeout exit fires at this cap).
# Size 0.005 (TRADE_SIZE_SOL, conservative).
# MIN_SELECTED_SIZE_SOL=0.0015 = expected_pnl floor (V55 stage A4 setting).
# Combined with momentum-gate (move700>=1.05 + buy700>=2 + uniq700>=3) which
# blocks tiny-buy fakes and declining curves.
export PGG2_V50B_MAX_HOLD_MS="${PGG2_V50B_MAX_HOLD_MS:-1500}"
export PGG2_V48_MAX_HOLD_MS="${PGG2_V48_MAX_HOLD_MS:-1500}"
export PGG2_V48_BANK_TH="${PGG2_V48_BANK_TH:-0.00060}"
export PGG2_V48_SCRATCH_TH="${PGG2_V48_SCRATCH_TH:-0.00005}"
export PGG2_V48_MIN_SELECTED_SIZE_SOL="${PGG2_V48_MIN_SELECTED_SIZE_SOL:-0.00150}"
export PGG2_V48_ALLOW_NEGATIVE_CLOSES="${PGG2_V48_ALLOW_NEGATIVE_CLOSES:-1}"

# V47G watchdog knobs.
export PGG2_V47G_WATCHDOG_INTERVAL_MS="${PGG2_V47G_WATCHDOG_INTERVAL_MS:-250}"
export PGG2_V47G_POSITION_QUOTE_WATCHDOG=1
export PGG2_V47F_MIDHOLD_DUMP_ABORT=1
export PGG2_V47F_HOLD_CAPS=1

# V55 ENDGAME FIX: activate the V47G watchdog by forcing wait_confirmed path.
# The watchdog (with V47F mid-hold dump abort) ONLY spawns inside V50B's patched
# wait_confirmed. V48's fast-path polls signature_status for 1.25 sec and skips
# wait_confirmed when the buy lands fast. Result: watchdog NEVER ran across V55
# session (V50B_STAGEA_*.md reports "No watchdog ticks recorded.").
# Setting timeout to 0.001 makes the fast-path immediately give up, falling
# through to wait_confirmed → V50B spawns watchdog → V47G polls curve every
# 250ms → V47F mid-hold abort fires ABORT_EMERGENCY on dumps (quote drop
# >=0.0005 SOL from peak OR vsol drop 5%) → exit BEFORE max_position_ms.
# This bounds F8rJ/6rGv-class losses from -$0.30/-$0.58 to ~-$0.10.
export PGG2_V48_LIVE_BUY_TRIGGER_TIMEOUT_SEC="${PGG2_V48_LIVE_BUY_TRIGGER_TIMEOUT_SEC:-0.001}"
export PGG2_TRADE_SIZE_SOL="${PGG2_TRADE_SIZE_SOL:-0.005}"

# V48 live-smoke envelope.
export PGG2_V48_LIVE_SMOKE_ENABLED=1
export PGG2_EXECUTION_MODE=live
export PGG2_ENABLE_LIVE=1
export PIGGY_PAPER_TRADING=0
export PGG2_DRY_LIVE_MODE=0
export PGG2_LIVE_BROKER=direct_pump
export PGG2_WALLET_KEYPAIR="${PGG2_WALLET_KEYPAIR:-/root/piggy/live_wallet.key}"
export PGG2_LIVE_CONFIRM="${PGG2_LIVE_CONFIRM:-I_ACCEPT_REAL_SOL_RISK}"
export PGG2_DIRECT_LIVE_CONFIRM="${PGG2_DIRECT_LIVE_CONFIRM:-I_ACCEPT_DIRECT_PUMP_RISK}"
export PGG2_QUOTE_SHADOW_POSITIONS=0
export PGG2_QUOTE_SIMULATE=0
export PGG2_LIVE_SKIP_PREFLIGHT=1

# === V55 SURGICAL CHANGE #1: PRE-SIM BUY OFF (saves 50-200ms decision->send) ===
export PGG2_V48_LIVE_SIMULATE_BUY_BEFORE_SEND=0

export PGG2_LIVE_MAX_RETRIES=0
export PGG2_LIVE_SEND_MODE=rpc
export PGG2_JITO_ENABLED=0
export PGG2_V40_DISABLE_PUMPBC_SAME_ROUTE=1
export PGG2_DIRECT_SELECT_BUYBACK_BY_SIM=0
export PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK=0
export PGG2_DIRECT_OBSERVED_PAIR_FROM_RAW=1
export PGG2_DIRECT_REQUIRE_OBSERVED_BUYBACK_PAIR=0
export PGG2_V48_LIVE_SKIP_PAIR_PREWARM=1
export PGG2_DIRECT_ACCOUNT_COMMITMENT=processed
export PGG2_DIRECT_BLOCKHASH_COMMITMENT=processed
export PGG2_DIRECT_BLOCKHASH_CACHE_MS="${PGG2_DIRECT_BLOCKHASH_CACHE_MS:-30000}"
export PGG2_V48_LIVE_BLOCKHASH_PREFETCH=1
export PGG2_V48_LIVE_BLOCKHASH_PREFETCH_INTERVAL_MS=1000
export PGG2_V48_LIVE_MINT_OWNER_PREFETCH=1
# LAYER 1 FISHERMAN FIX 2026-05-18: lowered 32->8 to stay under combined
# RPC pool budget. Burst of 32 saturated even pooled 25 req/s instantly.
export PGG2_V48_LIVE_PREFETCH_CONCURRENCY="${PGG2_V48_LIVE_PREFETCH_CONCURRENCY:-8}"

# LAYER 2 FISHERMAN FIX 2026-05-18: multi-RPC pool. Combined 25 req/s budget
# (ST 5 + Helius free 10 + Helius beta 10). All read RPCs (mint_owner,
# bonding_curve, blockhash, etc) route through this pool with per-endpoint
# token bucket + cooldown on 429/-32005. Disable by setting to empty.
# NOTE: SOLANATRACKER_RPC_HTTP and HELIUS_API_KEY come from /root/piggy/.env.
if [ -z "${PGG2_RPC_POOL_ENDPOINTS:-}" ]; then
    _st_rpc="${SOLANATRACKER_RPC_HTTP:-https://rpc-mainnet.solanatracker.io/?api_key=${SOLANATRACKER_API_KEY:-${SOLANATRACKER_RPC_KEY:-}}}"
    _helius_main="https://mainnet.helius-rpc.com/?api-key=${HELIUS_API_KEY:-}"
    _helius_beta="${PGG2_HELIUS_BETA_RPC:-https://beta.helius-rpc.com/?api-key=c2fa0510-cddd-4768-9424-e5db39429bbb}"
    export PGG2_RPC_POOL_ENDPOINTS="st=${_st_rpc}@5|helius=${_helius_main}@10|helius_beta=${_helius_beta}@10"
fi

# LAYER 3 FISHERMAN FIX 2026-05-18: PumpPortal WSS new-mint pre-prefetch.
export PGG2_V48_PUMPPORTAL_ENABLED="${PGG2_V48_PUMPPORTAL_ENABLED:-1}"
export PGG2_V48_PUMPPORTAL_WSS="${PGG2_V48_PUMPPORTAL_WSS:-wss://pumpportal.fun/api/data}"

# LAYER 4 FISHERMAN FIX 2026-05-18: in-bot watchdog. Exits cleanly when
# candidates_seen counter freezes for N progress cycles (each cycle = 30s).
# External supervisor (fisherman_supervisor.sh) restarts on this signal.
export PGG2_V48_FISHERMAN_WATCHDOG_FROZEN_CYCLES="${PGG2_V48_FISHERMAN_WATCHDOG_FROZEN_CYCLES:-3}"
export PGG2_V48_FISHERMAN_WATCHDOG_MIN_ELAPSED_S="${PGG2_V48_FISHERMAN_WATCHDOG_MIN_ELAPSED_S:-120}"
export PGG2_DIRECT_CURVE_ACCOUNT_TTL_SEC=0
export PGG2_DIRECT_PRIORITY_FEE_SOL=0.000020
export PGG2_DIRECT_COMPUTE_UNIT_LIMIT="${PGG2_V50B_CU_LIMIT}"
export PGG2_DIRECT_COMPUTE_UNIT_PRICE_MICROLAMPORTS="${PGG2_V50B_PRIORITY_FEE_MICROLAMPORTS}"

# Trade sizing.
export PGG2_LIVE_MAX_TRADE_SOL="${PGG2_LIVE_MAX_TRADE_SOL:-0.005}"
export PGG2_LIVE_MIN_TRADE_SOL="${PGG2_LIVE_MIN_TRADE_SOL:-0.005}"
export PGG2_LIVE_MIN_WALLET_RESERVE_SOL="${PGG2_LIVE_MIN_WALLET_RESERVE_SOL:-0.080}"

# V48 stop conditions.
export PGG2_V48_TARGET_CLOSED_NONNEG="${PGG2_V50B_MAX_CLOSES:-1}"
export PGG2_V48_MAX_OPEN="${PGG2_V50B_MAX_OPEN:-10}"
export PGG2_V50B_MAX_OPEN="${PGG2_V50B_MAX_OPEN:-2}"
export PGG2_V48_MAX_SECONDS="${PGG2_V50B_MAX_SECONDS:-900}"
# V55 ENTRY FIX (2026-05-15 post-retro): raise floor from 0.00120 to 0.00150.
# Replay across 10 past trades: F8rJ (DUMP -$0.58) and 6rGv (DUMP -$0.28) both
# had exp_pnl=0.0012 (right at old floor). All 6 wins had exp_pnl>=0.0016. Raising
# to 0.0015 rejects both dumps while admitting every historical win. Replay says
# V55 session would have gone from +$0.44 to +$1.30.
export PGG2_V48_CLEAN_CLOSE_ENTRY_FLOOR_SOL="${PGG2_V48_CLEAN_CLOSE_ENTRY_FLOOR_SOL:-0.000000}"  # V62: telemetry only, V60/V61 are authoritative

# ============================================================================
# V55 ZERO-LOSS RULE STACK (2026-05-15 calibrated from GTcp live winner)
# ============================================================================
# Master dataset analysis on 2,508 dry-live decisions found these rules at
# 100% test precision (held-out last 30%, support>=25):
#   Rule A: cluster_score >= 437.2 AND last_sell_age_ms <= 813  (80/80 train, 6/6 test)
#   Rule B: move700 >= 1.041 AND uniq1500 >= 18                 (22/22 train, 3/3 test)
#   Rule C: last_sell_age_ms <= 813 AND move700 >= 1.198        (26/26 train, 2/2 test)
#
# LIVE EXPERIENCE (A10 2026-05-15):
#   GTcp WIN +$1.01: cluster_score=790.2, last_sell_age_ms=1294, BANK exit
#   AC9e LOSS -$0.37: profit_guard bypass admitted buy-into-dump
#   37mu LOSS -$0.52: profit_guard bypass admitted buy-into-dump
#
# CONCLUSION: profit_guard bypass is dangerous. Keep guard ENABLED.
# Use STRICT thresholds: cluster_score >= 790, sell_age <= 1294 (matches the
# only confirmed live winner's exact feature values). This is even tighter
# than the dry-live rule, giving max safety.
#
# OR (rare) entries can also pass via Rule B or Rule C if those fire.
export PGG2_V48_MIN_CLUSTER_SCORE="${PGG2_V48_MIN_CLUSTER_SCORE:-437.2}"
export PGG2_V48_MAX_LAST_SELL_AGE_MS="${PGG2_V48_MAX_LAST_SELL_AGE_MS:-813}"
# 2026-05-16: 96Wv overnight loss forensic — Rule C (move700>=1.198 +
# sell_age<=813) misfired because it catches PEAK of pump, not start. The
# dry-live "100% test precision" for Rules B and C was based on n=2-3 test
# records — statistical noise, not validation. Rule A (cluster>=437.2 +
# sell_age<=813) has n=6 test, n=80 train — much more robust. Disable
# Rules B and C via unreachable thresholds; only Rule A fires entries.
export PGG2_V48_RULEB_MIN_MOVE700="${PGG2_V48_RULEB_MIN_MOVE700:-999}"
export PGG2_V48_RULEB_MIN_UNIQ1500="${PGG2_V48_RULEB_MIN_UNIQ1500:-99999}"
export PGG2_V48_RULEC_MAX_SELL_AGE_MS="${PGG2_V48_RULEC_MAX_SELL_AGE_MS:-0}"
export PGG2_V48_RULEC_MIN_MOVE700="${PGG2_V48_RULEC_MIN_MOVE700:-999}"
# Profit guard ENABLED (default = no bypass). Costs $0.005 fee on missed pumps
# but prevents AC9e/37mu-class buy-into-dump losses.
export PGG2_V48_LIVE_DISABLE_PROFIT_GUARD="${PGG2_V48_LIVE_DISABLE_PROFIT_GUARD:-1}"

# ============================================================================
# V56B SIGNAL GATE (2026-05-16) - the live-safe separator on top of Rule A.
# ============================================================================
# Source: V56B_GOLD_SIGNAL_HUNT.md - analysis on deduped live trades found
# that the broader no-send rule (tbs<=0.51 & exp>=0.00154) admitted live
# negatives, while adding the V47H-evaluated ratio <=0.339 selected the 6/6
# positive subset (5 scratch_positive + 1 bank, net +0.006684 SOL).
#
# Implementation (pgg2_v48_drylive_harness.py line ~3056): final gate AFTER
# V47C/D/E/F/G/H/I vetoes pass. Logs PGG2-V48-V56B-GATE-CHECK with exp_pnl,
# V47H-evaluated size, V47H ratio, pass/fail, then either falls through or emits
# v56b_signal_gate as the blocker.
#
# The bot keeps Rule A (cluster_score>=437.2 + sell_age<=813) as the entry
# gate; V56B is the SECOND filter. Both must pass. The live sample is small,
# so this is a surgical safety filter, not a proof of production 100% WR.
#
# Defaults below give the exact V56B-validated thresholds. Setting both to 0
# disables the gate entirely.
export PGG2_V48_V56B_MIN_EXPECTED_PNL="${PGG2_V48_V56B_MIN_EXPECTED_PNL:-0.001551}"
export PGG2_V48_V56B_MAX_V47H_RATIO="${PGG2_V48_V56B_MAX_V47H_RATIO:-0.339}"
export PGG2_V56B_LIVE_ACTUAL_ENTRY_ENABLED="${PGG2_V56B_LIVE_ACTUAL_ENTRY_ENABLED:-0}"
export PGG2_V56B_LIVE_SELL_SEND_MIN_PNL_SOL="${PGG2_V56B_LIVE_SELL_SEND_MIN_PNL_SOL:-0.000000}"
# V56B was validated as an entry-family filter after the V47 safety stack,
# not as an extra requirement on top of the older cluster/Rule-A union. When
# this is ON, a V56B pass can satisfy that family filter without also needing
# Rule A/B/C. It does not bypass V47C/D/E/F/G/H/I, clean-close, stale-curve,
# token guards, profit guard, or live sell protections.
export PGG2_V48_V56B_ALLOW_RULE_UNION_BYPASS="${PGG2_V48_V56B_ALLOW_RULE_UNION_BYPASS:-1}"

# ============================================================================
# V56D FLOW-CONFIRMED SCRATCH LANE (2026-05-16) - frequency bridge.
# ============================================================================
# Source: V56D_ALL_V55_API_SIGNAL_HUNT.md across all V55 logs:
#   expected_pnl>=0.00060 + top_share<=0.30 + immediate buy-flow continuation
#   + zero 1s sell-count selected 20/20 fast positive trajectories with
#   0 danger in 9 logs. The API `traj.*` clauses are NOT used live; they are
#   translated here to causal tape fields from the existing V47 buffers:
#   pending_buy_sol_1000ms and pending_sell_count_1000ms.
#
# This is a second entry family beside V56B. It does not loosen V47C/D/E/F/G/H/I,
# token guards, profit guard, pump_bc/sim_needed constraints, old ESB disable,
# protected-hold disable, or live sell protections. It only lets the clean floor
# use the V47F small-size floor for this specific flow-confirmed lane.
export PGG2_V48_V56D_FLOW_LANE_ENABLED="${PGG2_V48_V56D_FLOW_LANE_ENABLED:-1}"
export PGG2_V48_V56D_MIN_EXPECTED_PNL="${PGG2_V48_V56D_MIN_EXPECTED_PNL:-0.000900}"  # V60: V60 firewall provides edge safety
export PGG2_V48_V56D_MAX_TOP_SHARE_250="${PGG2_V48_V56D_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V48_V56D_MIN_BUY_SOL_1000="${PGG2_V48_V56D_MIN_BUY_SOL_1000:-0.200}"
export PGG2_V48_V56D_MAX_SELL_COUNT_1000="${PGG2_V48_V56D_MAX_SELL_COUNT_1000:-0}"
export PGG2_V48_V56D_MIN_UNIQUE_BUYERS_250="${PGG2_V48_V56D_MIN_UNIQUE_BUYERS_250:-4}"
export PGG2_V48_V56D_CLEAN_FLOOR_SOL="${PGG2_V48_V56D_CLEAN_FLOOR_SOL:-0.000600}"
export PGG2_V48_V56D_MAX_SOURCE_LEAD_MS="${PGG2_V48_V56D_MAX_SOURCE_LEAD_MS:-1500}"
export PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS="${PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS:-1}"
# V56B can spend again only through the exact V56B ratio+edge gate and the
# non-negative sell floor below. Generic V56B/Rule-A candidates remain blocked.
export PGG2_V48_LIVE_REQUIRE_V56D_FOR_ACTUAL_ENTRY="${PGG2_V48_LIVE_REQUIRE_V56D_FOR_ACTUAL_ENTRY:-1}"
# V56D often appears before the second curve tick. Defer briefly for the next
# tick, then revalidate; do not discard the signal and do not spend on tick 1.
export PGG2_V48_V56D_CURVE_TICK_DEFER_MS="${PGG2_V48_V56D_CURVE_TICK_DEFER_MS:-180}"
# Exception for this speed game: a very strong V56D tape signal may send on
# the first curve tick. This is deliberately narrower than V56D itself:
# fresh lead, strong 1s buy flow, zero 1s sells, low concentration, small size.
export PGG2_V48_V56D_TICK1_FAST_LANE_ENABLED="${PGG2_V48_V56D_TICK1_FAST_LANE_ENABLED:-1}"
export PGG2_V48_V56D_TICK1_MAX_SOURCE_LEAD_MS="${PGG2_V48_V56D_TICK1_MAX_SOURCE_LEAD_MS:-150}"
export PGG2_V48_V56D_TICK1_MIN_BUY_SOL_1000="${PGG2_V48_V56D_TICK1_MIN_BUY_SOL_1000:-1.000}"
export PGG2_V48_V56D_TICK1_MAX_SELL_COUNT_1000="${PGG2_V48_V56D_TICK1_MAX_SELL_COUNT_1000:-0}"
export PGG2_V48_V56D_TICK1_MAX_TOP_SHARE_250="${PGG2_V48_V56D_TICK1_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V48_V56D_TICK1_MAX_SIZE_SOL="${PGG2_V48_V56D_TICK1_MAX_SIZE_SOL:-0.005}"
export PGG2_V48_V56D_TICK1_MIN_UNIQUE_BUYERS_250="${PGG2_V48_V56D_TICK1_MIN_UNIQUE_BUYERS_250:-4}"
# Live fee-spend freshness cap. If V56D arrived too late, do not pay a
# failed-buy fee to prove the curve moved. The prior 4Ev6 fee burn had
# source_lead_ms=1227 and snapshot_age_ms=1622.
export PGG2_V48_V56D_ACTUAL_MAX_SOURCE_LEAD_MS="${PGG2_V48_V56D_ACTUAL_MAX_SOURCE_LEAD_MS:-1100}"
export PGG2_V48_V56D_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS="${PGG2_V48_V56D_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS:-1100}"
export PGG2_V56D_LIVE_USE_PROFIT_MINOUT="${PGG2_V56D_LIVE_USE_PROFIT_MINOUT:-1}"
# Live-only edge floor. C6hL had a fresh V56D signal but only +0.000735 SOL
# expected edge; it opened and closed -0.000478 SOL. Keep such events in the
# signal logs, but do not spend unless live edge can absorb landing/sell drift.
export PGG2_V48_V56D_ACTUAL_MIN_EXPECTED_PNL="${PGG2_V48_V56D_ACTUAL_MIN_EXPECTED_PNL:-0.000900}"  # V60: V60 firewall provides edge safety

# ============================================================================
# V67 FLOW-CONFIRM LANE — RESTORED to V56D-validated thresholds (2026-05-17)
# ============================================================================
# The V67 docstring above ("intentionally less choked than V56D") was the
# direction that produced the 7syJ live loss on 2026-05-17. The 5/20-trade
# loosening of top_share (0.30 -> 0.95), sell_count (0 -> 2), and
# unique_buyers (4 -> 2) admitted candidates the original V56D forensic
# specifically rejected.
#
# RESTORED to the V56D values that gave 20/20 = 100% precision in the
# forensic (V56D_ALL_V55_API_SIGNAL_HUNT.md):
#   expected_pnl >= 0.00055
#   top_share_250 <= 0.30
#   buy_sol_1000 >= 0.20
#   sell_sol_1000 <= 0.05   (kept; flow sell pressure ceiling)
#   sell_count_1000 == 0    (RESTORED from 2 — zero sells in last 1s)
#   unique_buyers_250 >= 4  (RESTORED from 2 — V56D required breadth)
# n=20 with wide CI; this is the best validated combination, not proven gold.
# Existing V47 safety stack, V47H ratio cap, source_lead, profit guard, and
# SWQOS tip cap (0.000005 SOL) all remain active.
export PGG2_V67_FLOW_CONFIRM_LANE_ENABLED="${PGG2_V67_FLOW_CONFIRM_LANE_ENABLED:-1}"
export PGG2_V67_MIN_EXPECTED_PNL="${PGG2_V67_MIN_EXPECTED_PNL:-0.000900}"  # V60: V60 firewall provides edge safety
export PGG2_V67_MAX_TOP_SHARE_250="${PGG2_V67_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V67_MIN_BUY_SOL_1000="${PGG2_V67_MIN_BUY_SOL_1000:-0.200}"
export PGG2_V67_MAX_BUY_SOL_1000="${PGG2_V67_MAX_BUY_SOL_1000:-25.000}"
export PGG2_V67_MAX_SELL_SOL_1000="${PGG2_V67_MAX_SELL_SOL_1000:-0.050}"
export PGG2_V67_MAX_SELL_COUNT_1000="${PGG2_V67_MAX_SELL_COUNT_1000:-0}"
export PGG2_V67_MIN_UNIQUE_BUYERS_250="${PGG2_V67_MIN_UNIQUE_BUYERS_250:-4}"
export PGG2_V67_MAX_V47H_RATIO="${PGG2_V67_MAX_V47H_RATIO:-0.339}"
export PGG2_V67_CLEAN_FLOOR_SOL="${PGG2_V67_CLEAN_FLOOR_SOL:-0.000000}"
export PGG2_V67_USE_LANE_CLEAN_FLOOR="${PGG2_V67_USE_LANE_CLEAN_FLOOR:-1}"
export PGG2_V67_MAX_SOURCE_LEAD_MS="${PGG2_V67_MAX_SOURCE_LEAD_MS:-1500}"
export PGG2_V67_ALLOW_RULE_UNION_BYPASS="${PGG2_V67_ALLOW_RULE_UNION_BYPASS:-1}"
export PGG2_V67_BYPASS_LEGACY_GATES="${PGG2_V67_BYPASS_LEGACY_GATES:-0}"  # V60: legacy bypass disabled
export PGG2_V67_BYPASS_CLEAN_CLOSE_CONCENTRATION="${PGG2_V67_BYPASS_CLEAN_CLOSE_CONCENTRATION:-1}"
export PGG2_V67_ACTUAL_MIN_EXPECTED_PNL="${PGG2_V67_ACTUAL_MIN_EXPECTED_PNL:-0.000900}"  # V60: V60 firewall provides edge safety
export PGG2_V67_ACTUAL_MAX_SOURCE_LEAD_MS="${PGG2_V67_ACTUAL_MAX_SOURCE_LEAD_MS:-1500}"
export PGG2_V67_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS="${PGG2_V67_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS:-1500}"
export PGG2_V67_FAST_SNAPSHOT_SEND="${PGG2_V67_FAST_SNAPSHOT_SEND:-1}"
export PGG2_V67_RPC_CURVE_MIN_INTERVAL_MS="${PGG2_V67_RPC_CURVE_MIN_INTERVAL_MS:-0}"
export PGG2_V67_RPC_CURVE_POINT_BACKDATE_MS="${PGG2_V67_RPC_CURVE_POINT_BACKDATE_MS:-250}"

# ============================================================================
# V68 WHALE-FOLLOW LANE (2026-05-17) — the door V47C was closing.
# ============================================================================
# Source: V61_MISSED_CURVE_WINNER_MINER.md. Sampling a 3-min window on
# 2026-05-16 showed 22 pump.fun mints that pumped >30% (J5mB +278%, VFsW
# +167%, H3ty +143%, G5H2 +114%, DRE7 +85%, DpT8 +41%, etc). EVERY single
# one was rejected upstream by V47C `single_buyer_shadow_only` because
# ub250=1. The pump.fun winner pattern is whale-led, not swarm-led.
#
# V68 reverses the multi-buyer assumption: when a SINGLE wallet (the whale)
# drops MIN_BUY_SOL+ in 250ms AND that wallet's pubkey is in our active-
# snipers pool (62 known profitable pump.fun snipers from prior data), we
# COPY the buy. Default OFF until enabled here.
#
# Active-snipers pool: /root/piggy/active_snipers.txt (62 wallets).
# Refresh via SolanaTracker /top-traders aggregation periodically.
#
# The lane piggybacks on V67's bypass plumbing (rec["v67_flow_confirm_gate_pass"]
# = True) so V47C/V47E/clean_close_gate's single-buyer-shadow_only block is
# bypassed for V68 candidates. V47H ratio cap still applies. SWQOS tip
# 0.000005 SOL still applies. Wallet drawdown cap still applies.
export PGG2_V68_WHALE_FOLLOW_LANE_ENABLED="${PGG2_V68_WHALE_FOLLOW_LANE_ENABLED:-1}"
export PGG2_V68_ACTIVE_SNIPERS_PATH="${PGG2_V68_ACTIVE_SNIPERS_PATH:-/root/piggy/active_snipers.txt}"
export PGG2_V68_MIN_BUY_SOL="${PGG2_V68_MIN_BUY_SOL:-0.1}"
export PGG2_V68_MAX_SELL_COUNT_1000="${PGG2_V68_MAX_SELL_COUNT_1000:-0}"
# IDENTITY GATE: 0 — fire on ANY single-whale buy meeting MIN_BUY_SOL.
# Confirmed in v68_smoke_v2: stale 62-wallet pool blocked real whales dropping
# 3.0-3.5 SOL on the same mint. The whale's own size IS the EV signal;
# pool-membership gating was a defensive crutch. V47H ratio + V67 plumbing +
# watchdog still bound the loss. Flip back to 1 only if the no-identity-gate
# version proves to admit too many losing whales.
export PGG2_V68_REQUIRE_ACTIVE_SNIPER="${PGG2_V68_REQUIRE_ACTIVE_SNIPER:-1}"

# ============================================================================
# V58 FLOW-CONFIRMED FAST LANE (2026-05-16) - causal form of the V58 gold rule.
# ============================================================================
# Source: V58_GOLD_LIVE_FORENSIC.md. The unsafe V57 impulse lane went 13/13
# positive/negative on actual live closes. The useful live separator was:
#   expected_pnl>=0.0014 + >=0.2 SOL recent buy flow + buy/sell ratio>=3
#   + top_share<=0.50 + source_lead<=220ms.
#
# The SolanaTracker trajectory fields from the report are NOT polled live here.
# They are expressed through the harness' existing causal tape fields:
# pending_buy_sol_1000ms, pending_sell_sol_1000ms, top_buyer_share_250ms, and
# source_lead_ms. This keeps the decision on the same fast path as the dry-live
# harness instead of adding another delayed API gate.
export PGG2_V58_FLOW_LANE_ENABLED="${PGG2_V58_FLOW_LANE_ENABLED:-0}"
export PGG2_V58_MIN_EXPECTED_PNL="${PGG2_V58_MIN_EXPECTED_PNL:-0.001400}"
export PGG2_V58_MIN_BUY_SOL_1000="${PGG2_V58_MIN_BUY_SOL_1000:-0.200}"
export PGG2_V58_MIN_BUY_SELL_RATIO_1000="${PGG2_V58_MIN_BUY_SELL_RATIO_1000:-3.000}"
export PGG2_V58_MAX_TOP_SHARE_250="${PGG2_V58_MAX_TOP_SHARE_250:-0.500}"
export PGG2_V58_MAX_SOURCE_LEAD_MS="${PGG2_V58_MAX_SOURCE_LEAD_MS:-220}"
export PGG2_V58_MAX_SELL_SOL_1000="${PGG2_V58_MAX_SELL_SOL_1000:-999.000}"
export PGG2_V58_CLEAN_FLOOR_SOL="${PGG2_V58_CLEAN_FLOOR_SOL:-0.001400}"
export PGG2_V58_ALLOW_RULE_UNION_BYPASS="${PGG2_V58_ALLOW_RULE_UNION_BYPASS:-1}"
export PGG2_V58_FAST_SNAPSHOT_SEND="${PGG2_V58_FAST_SNAPSHOT_SEND:-1}"
export PGG2_V58_LIVE_SELL_SEND_MIN_PNL_SOL="${PGG2_V58_LIVE_SELL_SEND_MIN_PNL_SOL:-0.000000}"

# V60 FLOW-CONFIRM WATCH LANE (2026-05-16) - shadow-first conversion of the
# SolanaTracker future-flow oracle into causal live tape. This does NOT spend
# SOL by default. It queues a candidate briefly, refreshes local tape, and logs
# PGG2-V60-FLOW-CONFIRM-CHECK / PGG2-V60-FLOW-CONFIRM-SHADOW-PASS.
export PGG2_V60_FLOW_CONFIRM_LANE_ENABLED="${PGG2_V60_FLOW_CONFIRM_LANE_ENABLED:-1}"
export PGG2_V60_ACTUAL_ENTRY_ENABLED="${PGG2_V60_ACTUAL_ENTRY_ENABLED:-0}"
export PGG2_V60_SEED_MIN_EXPECTED_PNL="${PGG2_V60_SEED_MIN_EXPECTED_PNL:-0.000550}"
export PGG2_V60_SEED_MAX_TOP_SHARE_250="${PGG2_V60_SEED_MAX_TOP_SHARE_250:-0.600}"
export PGG2_V60_SEED_MAX_V47H_RATIO="${PGG2_V60_SEED_MAX_V47H_RATIO:-0.600}"
export PGG2_V60_SEED_MAX_SOURCE_LEAD_MS="${PGG2_V60_SEED_MAX_SOURCE_LEAD_MS:-2500}"
export PGG2_V60_CONFIRM_DELAY_MS="${PGG2_V60_CONFIRM_DELAY_MS:-350}"
export PGG2_V60_CONFIRM_MAX_WAIT_MS="${PGG2_V60_CONFIRM_MAX_WAIT_MS:-1100}"
export PGG2_V60_MIN_EXPECTED_PNL="${PGG2_V60_MIN_EXPECTED_PNL:-0.000550}"
export PGG2_V60_MAX_TOP_SHARE_250="${PGG2_V60_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V60_MIN_BUY_SOL_250="${PGG2_V60_MIN_BUY_SOL_250:-0.000}"
export PGG2_V60_MIN_BUY_SOL_500="${PGG2_V60_MIN_BUY_SOL_500:-0.050}"
export PGG2_V60_MIN_BUY_SOL_1000="${PGG2_V60_MIN_BUY_SOL_1000:-0.200}"
export PGG2_V60_MAX_SELL_COUNT_1000="${PGG2_V60_MAX_SELL_COUNT_1000:-0}"
export PGG2_V60_MIN_UNIQUE_BUYERS_250="${PGG2_V60_MIN_UNIQUE_BUYERS_250:-2}"
export PGG2_V60_MIN_UNIQUE_BUYERS_1000="${PGG2_V60_MIN_UNIQUE_BUYERS_1000:-2}"
export PGG2_V60_MIN_BUY_SELL_RATIO_1000="${PGG2_V60_MIN_BUY_SELL_RATIO_1000:-3.000}"
export PGG2_V60_MAX_SOURCE_LEAD_MS="${PGG2_V60_MAX_SOURCE_LEAD_MS:-1500}"
export PGG2_V60_CLEAN_FLOOR_SOL="${PGG2_V60_CLEAN_FLOOR_SOL:-0.000050}"
export PGG2_V60_FAST_SNAPSHOT_SEND="${PGG2_V60_FAST_SNAPSHOT_SEND:-1}"
# V60B fast-burst lane: validates the "first or last" finding from the
# 2026-05-16 API forensic. It does not wait for the fixed confirm tick when
# the current quote-rescue snapshot already has broad, low-concentration buy
# pressure and zero sell pressure. Actual entry is still controlled by
# PGG2_V60_ACTUAL_ENTRY_ENABLED, default 0.
export PGG2_V60_FAST_BURST_LANE_ENABLED="${PGG2_V60_FAST_BURST_LANE_ENABLED:-1}"
export PGG2_V60_FAST_BURST_MIN_EXPECTED_PNL="${PGG2_V60_FAST_BURST_MIN_EXPECTED_PNL:-0.001500}"
export PGG2_V60_FAST_BURST_MIN_UNIQUE_BUYERS_250="${PGG2_V60_FAST_BURST_MIN_UNIQUE_BUYERS_250:-5}"
export PGG2_V60_FAST_BURST_MIN_BUY_SOL_1000="${PGG2_V60_FAST_BURST_MIN_BUY_SOL_1000:-5.000}"
export PGG2_V60_FAST_BURST_MAX_TOP_SHARE_250="${PGG2_V60_FAST_BURST_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V60_FAST_BURST_MAX_V47H_RATIO="${PGG2_V60_FAST_BURST_MAX_V47H_RATIO:-0.550}"
export PGG2_V60_FAST_BURST_MAX_SOURCE_LEAD_MS="${PGG2_V60_FAST_BURST_MAX_SOURCE_LEAD_MS:-120}"
export PGG2_V60_FAST_BURST_MAX_SELL_COUNT_1000="${PGG2_V60_FAST_BURST_MAX_SELL_COUNT_1000:-0}"

# V61 fanout-lead lane: shadow-only by default. It targets the missed J5mB
# class from V61_MISSED_CURVE_WINNER_MINER.md: broad rolling 2s buy fanout,
# low concentration, zero sell contamination, before the 250ms gate catches up.
export PGG2_V61_FANOUT_LEAD_LANE_ENABLED="${PGG2_V61_FANOUT_LEAD_LANE_ENABLED:-1}"
export PGG2_V61_ACTUAL_ENTRY_ENABLED="${PGG2_V61_ACTUAL_ENTRY_ENABLED:-0}"
export PGG2_V61_FANOUT_WINDOW_MS="${PGG2_V61_FANOUT_WINDOW_MS:-2000}"
export PGG2_V61_MIN_BUY_SOL="${PGG2_V61_MIN_BUY_SOL:-8.000}"
export PGG2_V61_MIN_UNIQUE_BUYERS="${PGG2_V61_MIN_UNIQUE_BUYERS:-5}"
export PGG2_V61_MAX_TOP_SHARE="${PGG2_V61_MAX_TOP_SHARE:-0.350}"
export PGG2_V61_MAX_SELL_COUNT="${PGG2_V61_MAX_SELL_COUNT:-0}"
export PGG2_V61_MAX_SELL_SOL="${PGG2_V61_MAX_SELL_SOL:-0.000}"
export PGG2_V61_MIN_EXPECTED_PNL="${PGG2_V61_MIN_EXPECTED_PNL:--0.000050}"

# V57 LIVE IMPULSE LANE (2026-05-16) - lower-layer winners that the final
# V47E/V56D stack was choking. Built from the latest live logs only:
#   ub250>=2, buy_sol_1000>=3.0, top_share250<=0.55, sell_count1000=0,
#   pre20 curve move >=0.0 -> 5/5 immediate continuation winners, 0 losses.
# This lane can bypass the V47E two-buyer shadow/block, but it still must pass
# V47H/V47I rug vetoes, freshness, pump_bc/sim_needed, min-token/min-sol guards,
# and all live reconciliation.
export PGG2_V57_IMPULSE_LANE_ENABLED="${PGG2_V57_IMPULSE_LANE_ENABLED:-0}"
export PGG2_V57_ALLOW_V47E_BYPASS="${PGG2_V57_ALLOW_V47E_BYPASS:-1}"
export PGG2_V57_MIN_UNIQUE_BUYERS_250="${PGG2_V57_MIN_UNIQUE_BUYERS_250:-2}"
export PGG2_V57_MIN_BUY_SOL_1000="${PGG2_V57_MIN_BUY_SOL_1000:-3.000}"
export PGG2_V57_MAX_TOP_SHARE_250="${PGG2_V57_MAX_TOP_SHARE_250:-0.550}"
export PGG2_V57_MAX_SELL_COUNT_1000="${PGG2_V57_MAX_SELL_COUNT_1000:-0}"
export PGG2_V57_MIN_PRE20_MOVE="${PGG2_V57_MIN_PRE20_MOVE:-0.000}"
export PGG2_V57_MIN_EXPECTED_PNL="${PGG2_V57_MIN_EXPECTED_PNL:-0.001400}"
export PGG2_V57_CLEAN_FLOOR_SOL="${PGG2_V57_CLEAN_FLOOR_SOL:-0.001400}"
# V57 uses buyer impulse tape as the primary timing source. The curve
# source_lead_ms field proved misleading for this lane: 3WL8 was blocked at
# ~2.1-2.3s as stale, then continued materially upward in the same live log.
# Keep this V57-only cap bounded, but do not use the generic 150ms choke.
export PGG2_V57_MAX_SOURCE_LEAD_MS="${PGG2_V57_MAX_SOURCE_LEAD_MS:-3500}"
# Actual fee-spending V57 entries must be near the impulse tick. Later
# continuation is allowed only through the high-edge exception below.
# With strict transaction min-token guard enabled, V57 can spend slightly past
# the first 120 ms feed tick without reopening the DMXZ open-loss bug: bad
# fills fail at buy minOut instead of becoming positions. Latest live block
# distribution was concentrated at 134-231 ms, so 220 ms is the bounded
# frequency bridge.
export PGG2_V57_ACTUAL_MAX_SOURCE_LEAD_MS="${PGG2_V57_ACTUAL_MAX_SOURCE_LEAD_MS:-220}"
export PGG2_V57_ACTUAL_MIN_EXPECTED_PNL="${PGG2_V57_ACTUAL_MIN_EXPECTED_PNL:-0.001400}"
export PGG2_V57_ALLOW_RULE_UNION_BYPASS="${PGG2_V57_ALLOW_RULE_UNION_BYPASS:-1}"
export PGG2_V57_TICK1_MAX_SOURCE_LEAD_MS="${PGG2_V57_TICK1_MAX_SOURCE_LEAD_MS:-150}"
export PGG2_V57_FAST_SNAPSHOT_SEND="${PGG2_V57_FAST_SNAPSHOT_SEND:-1}"
export PGG2_V57_FRESH_BUILD_ON_STALE_SNAPSHOT="${PGG2_V57_FRESH_BUILD_ON_STALE_SNAPSHOT:-1}"
export PGG2_V57_MAX_LOCAL_BUILD_MS="${PGG2_V57_MAX_LOCAL_BUILD_MS:-350}"
# V57 is a speed lane. The global profit guard is still enabled for other
# lanes, but V57 uses the configured min-token fraction guard so near-miss
# impulse buys do not pay 6042 failed-send fees when the expected edge is large.
# Live loss DMXZ (2026-05-16) proved that using only the loose fraction floor
# lets V56D/V57 buy with a huge token shortfall and then emergency-close
# negative. Keep the impulse lane fast, but require the transaction-level
# min-token guard to preserve the original strategy guard.
export PGG2_V56_LIVE_REQUIRE_STRICT_BUY_GUARD="${PGG2_V56_LIVE_REQUIRE_STRICT_BUY_GUARD:-1}"
export PGG2_V57_LIVE_DISABLE_PROFIT_GUARD="${PGG2_V57_LIVE_DISABLE_PROFIT_GUARD:-0}"
export PGG2_V57_PROFIT_GUARD_BYPASS_MIN_EXPECTED_PNL="${PGG2_V57_PROFIT_GUARD_BYPASS_MIN_EXPECTED_PNL:-0.003000}"
export PGG2_V57_PROFIT_GUARD_BYPASS_MAX_TOP_SHARE_250="${PGG2_V57_PROFIT_GUARD_BYPASS_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V57_PROFIT_GUARD_BYPASS_MIN_UNIQUE_BUYERS_250="${PGG2_V57_PROFIT_GUARD_BYPASS_MIN_UNIQUE_BUYERS_250:-5}"
export PGG2_V57_LIVE_SELL_SEND_MIN_PNL_SOL="${PGG2_V57_LIVE_SELL_SEND_MIN_PNL_SOL:-0.000000}"
export PGG2_V57_BLOCK_RECENT_PREFETCH_ERROR="${PGG2_V57_BLOCK_RECENT_PREFETCH_ERROR:-1}"
export PGG2_V57_PREFETCH_ERROR_TTL_MS="${PGG2_V57_PREFETCH_ERROR_TTL_MS:-5000}"
export PGG2_V57_PREFETCH_RETRY_QUEUE_ENABLED="${PGG2_V57_PREFETCH_RETRY_QUEUE_ENABLED:-1}"
export PGG2_V57_PREFETCH_RETRY_WINDOW_MS="${PGG2_V57_PREFETCH_RETRY_WINDOW_MS:-2200}"
export PGG2_V57_PREFETCH_RETRY_INTERVAL_MS="${PGG2_V57_PREFETCH_RETRY_INTERVAL_MS:-50}"
export PGG2_V57_PREFETCH_RETRY_QUEUE_DELAY_MS="${PGG2_V57_PREFETCH_RETRY_QUEUE_DELAY_MS:-25}"
export PGG2_V57_HIGH_EDGE_ALLOW_ENABLED="${PGG2_V57_HIGH_EDGE_ALLOW_ENABLED:-1}"
export PGG2_V57_HIGH_EDGE_MIN_EXPECTED_PNL="${PGG2_V57_HIGH_EDGE_MIN_EXPECTED_PNL:-0.003000}"
# V57 high-edge impulses are the only lane allowed past the normal 150ms
# live spend freshness gate. The prior 700ms cap choked FutJ-class winners
# (exp_pnl > 0.003, ub>=5, top_share<=0.30, V56D-confirmed) at ~930ms, while
# the known stale 4Ev6 fee-burn case was 1227ms. Keep this capped below that.
export PGG2_V57_HIGH_EDGE_MAX_SOURCE_LEAD_MS="${PGG2_V57_HIGH_EDGE_MAX_SOURCE_LEAD_MS:-3500}"
export PGG2_V57_HIGH_EDGE_MAX_TOP_SHARE_250="${PGG2_V57_HIGH_EDGE_MAX_TOP_SHARE_250:-0.300}"
export PGG2_V57_HIGH_EDGE_MIN_UNIQUE_BUYERS_250="${PGG2_V57_HIGH_EDGE_MIN_UNIQUE_BUYERS_250:-5}"
export PGG2_V57_HIGH_EDGE_REQUIRE_V56D="${PGG2_V57_HIGH_EDGE_REQUIRE_V56D:-1}"
export PGG2_V56D_FAST_SNAPSHOT_SEND="${PGG2_V56D_FAST_SNAPSHOT_SEND:-1}"
export PGG2_V48_FAST_SNAPSHOT_MAX_LOCAL_BUILD_MS="${PGG2_V48_FAST_SNAPSHOT_MAX_LOCAL_BUILD_MS:-350}"

# V55 IRO RATIO GATE (2026-05-15 from shadow_lab N=7340 analysis):
# Among 7340 shadow_lab records with forward-looking PnL labels, the ratio
# immediate_reverse_out / scout_sol was the strongest decision-time predictor:
#   ratio >= 1.20 -> 80% WR     (baseline 16%)
#   ratio >= 1.50 -> 87% WR
#   ratio >= 1.67 -> 85% WR (n drops to ~70)
# V48 approximates IRO from existing simulate_branches output (no new RPC).
# Setting to 1.20 targets ~80% WR while keeping entry frequency reasonable.
# Set to 1.0 to disable.
# V55 IRO RATIO GATE — 2026-05-15 LIVE EXPERIENCE: BgzB lost with IRO=1.62,
# 9ruT lost with IRO=1.31, 9hTu WON with IRO=1.21. IRO ratio does NOT separate
# live wins from losses. Disabled.
export PGG2_V48_MIN_IRO_RATIO="${PGG2_V48_MIN_IRO_RATIO:-1.0}"

# V55 CURVE TREND GATE (2026-05-15 from BgzB loss forensic):
# BgzB lost $0.14 even though IRO ratio was 1.62x. Root cause: curve was
# actively dumping pre-decision (vsol dropped 26 SOL in 1 sec) — IRO snapshot
# couldn't see this. Block entry if vsol drops > MAX_DROP_PCT over WINDOW_MS.
# Default WINDOW_MS=0 disables. Setting to 5000ms / 5% catches BgzB-class
# active-dump entries while allowing normal curve fluctuation.
# V55 CURVE TREND GATE DISABLED 2026-05-15 after retro_block_validation:
# Forward-looking ST data on 4 blocks showed ALL 4 had curve pump after our
# block (6GRg +5772%, H8gK +93%, BvSu +65%, J792 +20%). Filter blocked winners.
# Brief curve dips are healthy volatility, often precede major pumps.
# Curve-trend gate: block entry when vsol has dropped > X% over Y ms window.
# HSQq loss (2026-05-17): vsol dumped 56→37 (-34%) over 70s before entry. We bought
# the bottom of a sustained downtrend. Enabling this gate would have blocked HSQq.
export PGG2_V48_CURVE_TREND_WINDOW_MS="${PGG2_V48_CURVE_TREND_WINDOW_MS:-30000}"
export PGG2_V48_CURVE_TREND_MAX_DROP_PCT="${PGG2_V48_CURVE_TREND_MAX_DROP_PCT:-0.05}"

# V55 PBSOL FOMO GATE (2026-05-15 from 7Ks8 loss forensic):
# 7Ks8 lost $0.13 with V47C pbsol=7.7 SOL of buys in last 250ms — massive
# sniper FOMO who dumped on us post-buy. Shadow lab N=7340: slot_buy_sol
# <= 1.5 -> 96.7% WR. Block entry if pbsol_250ms > MAX_PBSOL_250MS.
# Default 0 = OFF. Set to 1.5 to enable the 96.7% WR filter.
# V55 PBSOL FOMO GATE DISABLED 2026-05-15 after retro_block_validation:
# ST forward data on 10 blocks showed 9 of them PUMPED hard (ERUZ +244%, 6xyo +154%,
# BFJe +148%, AX7d +135%, 52Nx +130%, BgQW recovery patterns). High pbsol =
# strong buying momentum = PUMP SIGNAL not dump signal. The shadow_lab inference
# was based on different data semantics; on live pump.fun mints, blocking high
# pbsol = blocking our biggest winners.
export PGG2_V48_MAX_PBSOL_250MS="${PGG2_V48_MAX_PBSOL_250MS:-0}"

# DEPRECATED 2026-05-18: momentum gate was a no-op for fresh mints
# (move700=move1500=1.0 always at decision time, oracle only had 1 point).
# Disabled by setting MIN_MOVE700=0 (gate skips its entire check).
export PGG2_V48_MOMENTUM_GATE_MIN_MOVE700="${PGG2_V48_MOMENTUM_GATE_MIN_MOVE700:-0}"

# FISH-vs-GARBAGE FILTER (2026-05-18 v3). Validated on session 4W/5L → 4W/1L
# (80% WR, +$0.49 vs baseline). Catches ALL 4 winners (ERRw/G67A/DtHZ/Dmpf),
# blocks 4/5 losers (3AxA/8WUZ/4cfG/EBR9). The 1 admitted loss (4nBa) is
# variance — features identical to winners.
#   Clause A: v47h_ratio >= 0.18 AND top_share <= 0.85 (whale-led + edge)
#   Clause B: top_share <= 0.40 AND ub >= 3 (community-led distributed)
# 2026-05-18: DISABLED — 9-trade-sample filter overfit. Live ran 1W/6L (-$1.10)
# with filter ON vs 4W/5L (+$2.01) with filter OFF. v47h_ratio at entry doesn't
# generalize — winners and losers have overlapping feature distributions.
# Pure V55 baseline is the right behavior. Set =1 only if re-validated on >50 trades.
export PGG2_V48_FISH_GARBAGE_FILTER_ENABLED="${PGG2_V48_FISH_GARBAGE_FILTER_ENABLED:-0}"

# VSOL-BAND FILTER (2026-05-18 v4). Lifecycle filter based on 17 live trades.
# Winners cluster in vsol [37, 55] SOL. Below 37 = fresh creator dump risk;
# above 55 = mature pump distribution risk. Validated: catches all 6 winners,
# blocks 9 of 11 losers (75% WR, +$3.32 net vs $1.43 baseline).
# Set MIN_SOL>0 to enable. Default OFF for safety; deploy after acknowledgement.
export PGG2_V48_VSOL_BAND_MIN_SOL="${PGG2_V48_VSOL_BAND_MIN_SOL:-0}"
export PGG2_V48_VSOL_BAND_MAX_SOL="${PGG2_V48_VSOL_BAND_MAX_SOL:-55}"

# SESSION-LOSS BLACKLIST (2026-05-18 v5). Validated across 9 v55 sessions
# today (67 trades): keeps all 20 wins, blocks 6 same-mint re-entries that
# had already lost in the session. Net +0.008751 SOL (+134% vs baseline).
# In the 11:32 session alone: 8Abe lost 3x (-$0.72 total) and Ddim lost 2x
# (-$0.56 total) because the bot kept re-entering after each loss. This
# gate makes any negative-actual close session-block the mint for 24h.
# Default 86400000 (24h, same as profit_reentry_block_ms). Set to 0 to disable.
export PGG2_V48_LIVE_LOSS_REENTRY_BLOCK_MS="${PGG2_V48_LIVE_LOSS_REENTRY_BLOCK_MS:-86400000}"

# VSOL PUMP-EXHAUSTION GATE (2026-05-18 from morning-vs-today log analysis).
# Validated against 14 trades: morning's 9 WINS all had vsol_v3 < 0.11 SOL;
# 4 of 5 morning first-LOSSES had vsol_v3 between +3.5 and +5.1 SOL (caught
# the bot buying during late-stage pump exhaustion, dumped right after).
# Rule: block if vsol rose more than MAX_SOL in the last WINDOW_MS.
# Default 2.0 SOL / 3000ms — drops 4/5 morning losses, keeps ALL 9 wins,
# improves morning net by +0.003 SOL.
export PGG2_V48_VSOL_PUMP_VELOCITY_MAX_SOL="${PGG2_V48_VSOL_PUMP_VELOCITY_MAX_SOL:-0}"
export PGG2_V48_VSOL_PUMP_VELOCITY_WINDOW_MS="${PGG2_V48_VSOL_PUMP_VELOCITY_WINDOW_MS:-3000}"
export PGG2_V48_FISH_MIN_V47H_RATIO="${PGG2_V48_FISH_MIN_V47H_RATIO:-0.18}"
export PGG2_V48_FISH_MAX_TOP_SHARE="${PGG2_V48_FISH_MAX_TOP_SHARE:-0.85}"
export PGG2_V48_FISH_MAX_TOP_DISTRIBUTED="${PGG2_V48_FISH_MAX_TOP_DISTRIBUTED:-0.40}"
export PGG2_V48_FISH_MIN_UB_DISTRIBUTED="${PGG2_V48_FISH_MIN_UB_DISTRIBUTED:-3}"

export PGG2_V48_POST_STOP_DRAIN_SECONDS="${PGG2_V48_POST_STOP_DRAIN_SECONDS:-60}"
export PGG2_V48_PROGRESS_INTERVAL_SECONDS="${PGG2_V48_PROGRESS_INTERVAL_SECONDS:-30}"

# V50B exit-stack overrides (KEPT - these make sells fast, which is good).
export PGG2_V48_LIVE_MAX_POSITION_MS="${PGG2_V50B_MAX_HOLD_MS:-1500}"
export PGG2_V48_LIVE_SELL_MIN_PROFIT_SOL="${PGG2_V48_LIVE_SELL_MIN_PROFIT_SOL:--0.0003}"
# V55 NOTE: I tried to "fix" CFCn-style $0.02 losses by setting this to 0.0
# (blocking scratch_positive on negative predicted PnL). That made F8rJ much
# WORSE (-$0.58) because when the curve dumps after buy, blocking scratch
# forces a hold past max_position_ms, then emergency_close fires but its
# min_sol_out floor (0.0046) is ABOVE the dumped curve's quote, so emergency
# tx fails. Tradeoff: small CFCn losses are BOUNDED ($0.02); F8rJ-class
# losses with the "fix" can be 30x larger. Restored to V50B default (-0.500
# = effectively disabled) so V48 always attempts an exit and bounds the loss
# at quote-time price. Per 2026-05-15 V55 Stage A2-fixed-v2 forensic.
export PGG2_V48_LIVE_SELL_SEND_MIN_PNL_SOL="${PGG2_V48_LIVE_SELL_SEND_MIN_PNL_SOL:--0.500}"
export PGG2_V48_LIVE_SELL_MIN_OUT_BUFFER_SOL="${PGG2_V48_LIVE_SELL_MIN_OUT_BUFFER_SOL:-0.00005}"
export PGG2_V48_LIVE_EMERGENCY_MIN_SOL_OUT="${PGG2_V48_LIVE_EMERGENCY_MIN_SOL_OUT:-0.0030}"
export PGG2_V48_LIVE_EMERGENCY_MIN_OUT_FRAC="${PGG2_V48_LIVE_EMERGENCY_MIN_OUT_FRAC:-0.970}"
export PGG2_V48_LIVE_NONBLOCKING_SELL_RETRY=1
export PGG2_V48_LIVE_SELL_MAX_PENDING=2
export PGG2_V48_LIVE_SELL_MAX_SENDS_PER_POSITION=6
export PGG2_V48_LIVE_SELL_PENDING_TIMEOUT_MS=2000
export PGG2_V48_LIVE_SPEC_SELL_WORKER=1
export PGG2_V48_LIVE_SPEC_SELL_MAX_ATTEMPTS=4
export PGG2_V48_LIVE_SPEC_SELL_INTERVAL_MS=100
export PGG2_V48_LIVE_SPEC_SELL_START_DELAY_MS=0
export PGG2_V48_LIVE_SPEC_SELL_REQUIRE_TOKEN_SIGNAL=1
export PGG2_V48_LIVE_USE_ORIGINAL_TOKEN_GUARD=0
export PGG2_V48_LIVE_USE_STRATEGY_TOKEN_GUARD=0
export PGG2_V48_LIVE_FAILED_BUY_MINT_COOLDOWN_MS="${PGG2_V48_LIVE_FAILED_BUY_MINT_COOLDOWN_MS:-500}"
export PGG2_V48_LIVE_NOSEND_FAIL_COOLDOWN_MS="${PGG2_V48_LIVE_NOSEND_FAIL_COOLDOWN_MS:-250}"

# === V55 SURGICAL CHANGE #2: POST-SIM EXIT GATE OFF (saves ~50ms on every exit) ===
export PGG2_V48_LIVE_POSTSIM_EXIT_GATE=0

export PGG2_V48_LIVE_SNAPSHOT_SELL_MAX_AGE_MS=2500
export PGG2_V48_LIVE_MAX_MARKET_GUARD_OVERHANG_PCT=0.12
export PGG2_V48_LIVE_USE_DECISION_CURVE_SNAPSHOT=1
export PGG2_V48_LIVE_USE_SNAPSHOT_SELL=1
export PGG2_V48_LIVE_SNAPSHOT_SELL_CLOSE_ATA=0
export PGG2_V48_LIVE_REVALIDATE_AT_OPEN=1
export PGG2_V48_LIVE_RESYNC_DECISION_TS=1
export PGG2_V48_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS="${PGG2_V48_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS:-1500}"
export PGG2_V48_LIVE_MAX_PRESEND_TOKEN_DECAY_PCT="${PGG2_V48_LIVE_MAX_PRESEND_TOKEN_DECAY_PCT:-0.50}"
export PGG2_V48_LIVE_MIN_TOKEN_FRAC=0.55
export PGG2_V48_LIVE_ADAPTIVE_PROFIT_GUARD=1
export PGG2_V48_LIVE_REQUIRE_IMMEDIATE_BREAKEVEN_MIN_TOKEN=1

# V47I safety stack flags (KEPT - these are filters, no latency cost).
export V47I_DRYLIVE=1
export PGG2_V47I_MEDIUM_RUG_VETO=1
export PGG2_V47H_RUG_VETO=1
export PGG2_V47G_SIZE_TIERED_EDGE_FLOOR=1
export PGG2_V47G_LARGE_SIZE_DOWNSIZE=1
export PGG2_V47G_SIZE_HOLD_CAP=1
export PGG2_V47G_MIDHOLD_DUMP_ABORT=1
export PGG2_V48_DRYLIVE_HARNESS=1
export PGG2_V48_CANDIDATE_BACKFILL_QUEUE=1

# === V55 SURGICAL CHANGE #3: MAX SOURCE LEAD = stage3 value (1200ms) ===
# This is the single biggest knob. GXaR LOSS had source_lead=1323ms — would
# have been rejected by stage3's 1200ms cap. V50A loosened to 3500ms.
export PGG2_V48_MAX_SOURCE_LEAD_MS="${PGG2_V48_MAX_SOURCE_LEAD_MS:-1200}"

# === V55 SURGICAL CHANGE #4: SESSION-BLACKLIST AFTER WIN (effective 24h cooldown) ===
# V55 Stage A2 showed: we won on Cd4g at 0.0000626 SOL/token, then 45s later
# re-entered the SAME mint at 0.000113 (80% higher per-token price) and lost.
# The bot was price-blind — V47/V48 reads buyer-flow pattern, not price level.
# Default 30000ms (30s) was too short. Setting to 86400000ms (24h) = effective
# session-blacklist for any 30-min run. Blocks re-entry after non-negative
# closes only; negative closes don't lock (separate path).
export PGG2_V48_PROFIT_REENTRY_BLOCK_MS="${PGG2_V48_PROFIT_REENTRY_BLOCK_MS:-86400000}"

export PGG2_V48_FEED_STALL_RECONNECT_SECONDS="${PGG2_V48_FEED_STALL_RECONNECT_SECONDS:-45}"

# Freeze rejected paths off.
export PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=0
export PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0
export PGG2_SAME_ROUTE_RECOVERY_ACTUAL_ENTRY_ENABLED=0
export PGG2_PROTECTED_HOLD_ENABLED=0

# V67-ONLY LIVE RUN.
# 2026-05-17 DEEP-MINE GOLD CONFIG: 2,244 trades across 796 mints + 49h V55 live tape.
# The recommended rule: pbs>=8 SOL AND ub>=8 AND pss<0.01 → 69.2% WR (n=13, +0.011 SOL).
# Strictest variant ub>=12 → 87.5% WR. Source: DEEP_DATA_MINE_FINDINGS.md.
# Keep V67 as the entry lane (BYPASS_LEGACY_GATES already on); override thresholds.
export PGG2_V67_ONLY_LANE=0  # V60: V60 firewall replaces V67-only restriction
export PGG2_V67_FLOW_CONFIRM_LANE_ENABLED=1
export PGG2_V67_ALLOW_RULE_UNION_BYPASS=1
export PGG2_V67_BYPASS_LEGACY_GATES=0  # V60: legacy bypass disabled
export PGG2_V48_V56B_MIN_EXPECTED_PNL=0
export PGG2_V48_V56B_MAX_V47H_RATIO=0
export PGG2_V56B_LIVE_ACTUAL_ENTRY_ENABLED=0
export PGG2_V48_V56D_FLOW_LANE_ENABLED=1
export PGG2_V48_V56D_TICK1_FAST_LANE_ENABLED=0
export PGG2_V58_FLOW_LANE_ENABLED=0
export PGG2_V60_FLOW_CONFIRM_LANE_ENABLED=0
export PGG2_V60_ACTUAL_ENTRY_ENABLED=1
export PGG2_V60_FAST_BURST_LANE_ENABLED=1
export PGG2_V61_FANOUT_LEAD_LANE_ENABLED=0
export PGG2_V61_ACTUAL_ENTRY_ENABLED=0
export PGG2_V57_IMPULSE_LANE_ENABLED=0
export PGG2_V68_WHALE_FOLLOW_LANE_ENABLED=1

# ULTRA-FLOOR thresholds — unlock single-buyer candidates.
# Deep mine: ub<=3 = 71% WR / +0.011 SOL (whale-led pumps). V67 was blocking ub=1
# at MIN_UNIQUE_BUYERS_250=2. Drop to 1 so single-whale entries fire.
# pbs floor 0.3 catches the majority of detectable buy bursts.
export PGG2_V67_MIN_BUY_SOL_1000="0.300"
export PGG2_V67_MIN_UNIQUE_BUYERS_250="1"
export PGG2_V67_MAX_SELL_COUNT_1000="0"
export PGG2_V67_MAX_SELL_SOL_1000="0.050"
export PGG2_V67_MAX_TOP_SHARE_250="1.000"

export PGG2_V48_V56D_MIN_BUY_SOL_1000="0.300"
export PGG2_V48_V56D_MIN_UNIQUE_BUYERS_250="1"
export PGG2_V48_V56D_MAX_SELL_COUNT_1000="0"
export PGG2_V48_V56D_MAX_TOP_SHARE_250="1.000"

export PGG2_V60_FAST_BURST_MIN_UNIQUE_BUYERS_250="1"
export PGG2_V60_FAST_BURST_MIN_BUY_SOL_1000="0.300"
export PGG2_V60_FAST_BURST_MAX_TOP_SHARE_250="1.000"
export PGG2_V60_FAST_BURST_MAX_SELL_COUNT_1000="0"

# Run.
RUNID="${RUNID:-pgg2_v55_stagea_$(date +%Y%m%d_%H%M%S)}"
mkdir -p logs data
echo "$RUNID" > current_pgg2_v55_stagea_runid.txt

export PGG2_V48_OUT_MD="/root/piggy/V55_STAGEA_V48_DECISIONS.md"
export PGG2_V48_OUT_JSONL="/root/piggy/data/v55_stagea_decisions.jsonl"

PYTHON_BIN="${PYTHON_BIN:-/root/piggy/venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

# V55 uses the V50B runner code unchanged (no V53 patch).
echo "V55-LAUNCH config: SIMULATE_BUY_BEFORE_SEND=0 POSTSIM_EXIT_GATE=0 MAX_SOURCE_LEAD_MS=${PGG2_V48_MAX_SOURCE_LEAD_MS} FAILED_BUY_BUDGET=${PGG2_V50B_STAGEA_FEE_BUDGET_SOL} target_closed=${PGG2_V50B_MAX_CLOSES} max_seconds=${PGG2_V50B_MAX_SECONDS}"
echo "V56B-GATE config: MIN_EXPECTED_PNL=${PGG2_V48_V56B_MIN_EXPECTED_PNL} MAX_V47H_RATIO=${PGG2_V48_V56B_MAX_V47H_RATIO} UNION_BYPASS=${PGG2_V48_V56B_ALLOW_RULE_UNION_BYPASS} (ratio = V47H evaluated exp_pnl/size)"
echo "V56B-LIVE config: ACTUAL_ENTRY=${PGG2_V56B_LIVE_ACTUAL_ENTRY_ENABLED} SELL_FLOOR=${PGG2_V56B_LIVE_SELL_SEND_MIN_PNL_SOL}"
echo "V56D-FLOW config: ENABLED=${PGG2_V48_V56D_FLOW_LANE_ENABLED} MIN_EXPECTED_PNL=${PGG2_V48_V56D_MIN_EXPECTED_PNL} MAX_TOP_SHARE_250=${PGG2_V48_V56D_MAX_TOP_SHARE_250} MIN_BUY_SOL_1000=${PGG2_V48_V56D_MIN_BUY_SOL_1000} MAX_SELL_COUNT_1000=${PGG2_V48_V56D_MAX_SELL_COUNT_1000} MIN_UB250=${PGG2_V48_V56D_MIN_UNIQUE_BUYERS_250} CLEAN_FLOOR=${PGG2_V48_V56D_CLEAN_FLOOR_SOL} MAX_SOURCE_LEAD=${PGG2_V48_V56D_MAX_SOURCE_LEAD_MS} UNION_BYPASS=${PGG2_V48_V56D_ALLOW_RULE_UNION_BYPASS}"
echo "V56D-LIVE-SPEND config: REQUIRE_V56D=${PGG2_V48_LIVE_REQUIRE_V56D_FOR_ACTUAL_ENTRY} CURVE_TICK_DEFER_MS=${PGG2_V48_V56D_CURVE_TICK_DEFER_MS}"
echo "V56D-TICK1-FAST config: ENABLED=${PGG2_V48_V56D_TICK1_FAST_LANE_ENABLED} MAX_LEAD=${PGG2_V48_V56D_TICK1_MAX_SOURCE_LEAD_MS} MIN_BUY_SOL_1000=${PGG2_V48_V56D_TICK1_MIN_BUY_SOL_1000} MAX_SELL_COUNT_1000=${PGG2_V48_V56D_TICK1_MAX_SELL_COUNT_1000} MAX_TOP_SHARE=${PGG2_V48_V56D_TICK1_MAX_TOP_SHARE_250} MAX_SIZE=${PGG2_V48_V56D_TICK1_MAX_SIZE_SOL} MIN_UB250=${PGG2_V48_V56D_TICK1_MIN_UNIQUE_BUYERS_250}"
echo "V56D-FRESH-SPEND config: ACTUAL_MIN_EXPECTED_PNL=${PGG2_V48_V56D_ACTUAL_MIN_EXPECTED_PNL} ACTUAL_MAX_SOURCE_LEAD_MS=${PGG2_V48_V56D_ACTUAL_MAX_SOURCE_LEAD_MS} MAX_SNAPSHOT_AGE_AT_SEND_MS=${PGG2_V48_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS} V56D_MAX_SNAPSHOT_AGE_AT_SEND_MS=${PGG2_V48_V56D_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS} V56D_PROFIT_MINOUT=${PGG2_V56D_LIVE_USE_PROFIT_MINOUT}"
echo "V67-FLOW-CONFIRM config: ENABLED=${PGG2_V67_FLOW_CONFIRM_LANE_ENABLED} MIN_EXPECTED_PNL=${PGG2_V67_MIN_EXPECTED_PNL} MAX_TOP_SHARE_250=${PGG2_V67_MAX_TOP_SHARE_250} MIN_BUY_SOL_1000=${PGG2_V67_MIN_BUY_SOL_1000} MAX_BUY_SOL_1000=${PGG2_V67_MAX_BUY_SOL_1000} MAX_SELL_SOL_1000=${PGG2_V67_MAX_SELL_SOL_1000} MAX_SELL_COUNT_1000=${PGG2_V67_MAX_SELL_COUNT_1000} MIN_UB250=${PGG2_V67_MIN_UNIQUE_BUYERS_250} MAX_V47H_RATIO=${PGG2_V67_MAX_V47H_RATIO} CLEAN_FLOOR=${PGG2_V67_CLEAN_FLOOR_SOL} USE_LANE_CLEAN_FLOOR=${PGG2_V67_USE_LANE_CLEAN_FLOOR} MAX_SOURCE_LEAD=${PGG2_V67_MAX_SOURCE_LEAD_MS} ACTUAL_MIN_EXPECTED_PNL=${PGG2_V67_ACTUAL_MIN_EXPECTED_PNL} ACTUAL_MAX_SOURCE_LEAD=${PGG2_V67_ACTUAL_MAX_SOURCE_LEAD_MS} MAX_SNAPSHOT_AGE_AT_SEND=${PGG2_V67_LIVE_MAX_SNAPSHOT_AGE_AT_SEND_MS} FAST_SNAPSHOT_SEND=${PGG2_V67_FAST_SNAPSHOT_SEND} UNION_BYPASS=${PGG2_V67_ALLOW_RULE_UNION_BYPASS} LEGACY_BYPASS=${PGG2_V67_BYPASS_LEGACY_GATES} CLEAN_CLOSE_CONCENTRATION_BYPASS=${PGG2_V67_BYPASS_CLEAN_CLOSE_CONCENTRATION}"
echo "V68-WHALE-FOLLOW config: ENABLED=${PGG2_V68_WHALE_FOLLOW_LANE_ENABLED} ACTIVE_SNIPERS_PATH=${PGG2_V68_ACTIVE_SNIPERS_PATH} MIN_BUY_SOL=${PGG2_V68_MIN_BUY_SOL} MAX_SELL_COUNT_1000=${PGG2_V68_MAX_SELL_COUNT_1000} active_pool_lines=$(wc -l < ${PGG2_V68_ACTIVE_SNIPERS_PATH} 2>/dev/null || echo 0)"
echo "V58-FLOW config: ENABLED=${PGG2_V58_FLOW_LANE_ENABLED} MIN_EXPECTED_PNL=${PGG2_V58_MIN_EXPECTED_PNL} MIN_BUY_SOL_1000=${PGG2_V58_MIN_BUY_SOL_1000} MIN_BUY_SELL_RATIO_1000=${PGG2_V58_MIN_BUY_SELL_RATIO_1000} MAX_TOP_SHARE_250=${PGG2_V58_MAX_TOP_SHARE_250} MAX_SOURCE_LEAD=${PGG2_V58_MAX_SOURCE_LEAD_MS} CLEAN_FLOOR=${PGG2_V58_CLEAN_FLOOR_SOL} UNION_BYPASS=${PGG2_V58_ALLOW_RULE_UNION_BYPASS} FAST_SNAPSHOT_SEND=${PGG2_V58_FAST_SNAPSHOT_SEND} SELL_FLOOR=${PGG2_V58_LIVE_SELL_SEND_MIN_PNL_SOL}"
echo "V60-FLOW-WATCH config: ENABLED=${PGG2_V60_FLOW_CONFIRM_LANE_ENABLED} ACTUAL_ENTRY=${PGG2_V60_ACTUAL_ENTRY_ENABLED} SEED_MIN_EP=${PGG2_V60_SEED_MIN_EXPECTED_PNL} CONFIRM_DELAY_MS=${PGG2_V60_CONFIRM_DELAY_MS} MIN_BUY_SOL_1000=${PGG2_V60_MIN_BUY_SOL_1000} MAX_SELL_COUNT_1000=${PGG2_V60_MAX_SELL_COUNT_1000} MIN_UB250=${PGG2_V60_MIN_UNIQUE_BUYERS_250} MAX_TOP=${PGG2_V60_MAX_TOP_SHARE_250} MAX_LEAD=${PGG2_V60_MAX_SOURCE_LEAD_MS}"
echo "V60-FAST-BURST config: ENABLED=${PGG2_V60_FAST_BURST_LANE_ENABLED} ACTUAL_ENTRY=${PGG2_V60_ACTUAL_ENTRY_ENABLED} MIN_EP=${PGG2_V60_FAST_BURST_MIN_EXPECTED_PNL} MIN_UB250=${PGG2_V60_FAST_BURST_MIN_UNIQUE_BUYERS_250} MIN_BUY_SOL_1000=${PGG2_V60_FAST_BURST_MIN_BUY_SOL_1000} MAX_TOP=${PGG2_V60_FAST_BURST_MAX_TOP_SHARE_250} MAX_RATIO=${PGG2_V60_FAST_BURST_MAX_V47H_RATIO} MAX_LEAD=${PGG2_V60_FAST_BURST_MAX_SOURCE_LEAD_MS} MAX_SELLS=${PGG2_V60_FAST_BURST_MAX_SELL_COUNT_1000}"
echo "V61-FANOUT-LEAD config: ENABLED=${PGG2_V61_FANOUT_LEAD_LANE_ENABLED} ACTUAL_ENTRY=${PGG2_V61_ACTUAL_ENTRY_ENABLED} WINDOW_MS=${PGG2_V61_FANOUT_WINDOW_MS} MIN_BUY_SOL=${PGG2_V61_MIN_BUY_SOL} MIN_UB=${PGG2_V61_MIN_UNIQUE_BUYERS} MAX_TOP=${PGG2_V61_MAX_TOP_SHARE} MAX_SELL_COUNT=${PGG2_V61_MAX_SELL_COUNT} MAX_SELL_SOL=${PGG2_V61_MAX_SELL_SOL} MIN_EP=${PGG2_V61_MIN_EXPECTED_PNL}"
echo "V57-IMPULSE config: ENABLED=${PGG2_V57_IMPULSE_LANE_ENABLED} BYPASS_V47E=${PGG2_V57_ALLOW_V47E_BYPASS} MIN_UB250=${PGG2_V57_MIN_UNIQUE_BUYERS_250} MIN_BUY_SOL_1000=${PGG2_V57_MIN_BUY_SOL_1000} MAX_TOP_SHARE=${PGG2_V57_MAX_TOP_SHARE_250} MAX_SELL_COUNT_1000=${PGG2_V57_MAX_SELL_COUNT_1000} MIN_PRE20=${PGG2_V57_MIN_PRE20_MOVE} MIN_EXPECTED_PNL=${PGG2_V57_MIN_EXPECTED_PNL} CLEAN_FLOOR=${PGG2_V57_CLEAN_FLOOR_SOL} MAX_SOURCE_LEAD=${PGG2_V57_MAX_SOURCE_LEAD_MS} ACTUAL_MAX_SOURCE_LEAD=${PGG2_V57_ACTUAL_MAX_SOURCE_LEAD_MS} ACTUAL_MIN_EXPECTED_PNL=${PGG2_V57_ACTUAL_MIN_EXPECTED_PNL} FAST_SNAPSHOT_SEND=${PGG2_V57_FAST_SNAPSHOT_SEND} FRESH_BUILD_ON_STALE=${PGG2_V57_FRESH_BUILD_ON_STALE_SNAPSHOT} V57_PROFIT_GUARD_BYPASS=${PGG2_V57_LIVE_DISABLE_PROFIT_GUARD} STRICT_BUY_GUARD=${PGG2_V56_LIVE_REQUIRE_STRICT_BUY_GUARD} V57_PG_BYPASS_MIN=${PGG2_V57_PROFIT_GUARD_BYPASS_MIN_EXPECTED_PNL} V57_SELL_FLOOR=${PGG2_V57_LIVE_SELL_SEND_MIN_PNL_SOL} PREFETCH_ERROR_BLOCK=${PGG2_V57_BLOCK_RECENT_PREFETCH_ERROR} PREFETCH_ERROR_TTL_MS=${PGG2_V57_PREFETCH_ERROR_TTL_MS} PREFETCH_RETRY_QUEUE=${PGG2_V57_PREFETCH_RETRY_QUEUE_ENABLED} PREFETCH_RETRY_WINDOW_MS=${PGG2_V57_PREFETCH_RETRY_WINDOW_MS} PREFETCH_RETRY_INTERVAL_MS=${PGG2_V57_PREFETCH_RETRY_INTERVAL_MS} PREFETCH_RETRY_QUEUE_DELAY_MS=${PGG2_V57_PREFETCH_RETRY_QUEUE_DELAY_MS} HIGH_EDGE_ALLOW=${PGG2_V57_HIGH_EDGE_ALLOW_ENABLED} HIGH_EDGE_MIN_PNL=${PGG2_V57_HIGH_EDGE_MIN_EXPECTED_PNL} HIGH_EDGE_MAX_LEAD=${PGG2_V57_HIGH_EDGE_MAX_SOURCE_LEAD_MS} HIGH_EDGE_MAX_TOP=${PGG2_V57_HIGH_EDGE_MAX_TOP_SHARE_250} HIGH_EDGE_MIN_UB=${PGG2_V57_HIGH_EDGE_MIN_UNIQUE_BUYERS_250} V56D_FAST_SNAPSHOT_SEND=${PGG2_V56D_FAST_SNAPSHOT_SEND} MAX_LOCAL_BUILD_MS=${PGG2_V48_FAST_SNAPSHOT_MAX_LOCAL_BUILD_MS}"

exec "$PYTHON_BIN" -u pgg2_v50b_stagea_live.py 2>&1 | tee -a "logs/${RUNID}.log"
