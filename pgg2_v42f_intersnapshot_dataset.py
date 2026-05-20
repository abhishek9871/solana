#!/usr/bin/env python3
"""V42F Phase 1 — inter-snapshot CAUSAL dataset extractor.

Parses every PGG2-V39-LEAD-SNAPSHOT chain from historical drylive logs and
emits one row per snapshot i with future-snapshot LABELS (i+1, i+2,
first_after_250ms, first_after_500ms, first_after_1000ms) plus an exit-policy
banked/scratch PnL realization.

NO LIVE TX. NO NETWORK CALLS. Reads existing log files only.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PIGGY_ROOT = Path("/root/piggy")
LOGS_DIR = PIGGY_ROOT / "logs"
OUT_PATH = PIGGY_ROOT / "V42F_INTERSNAPSHOT_DATASET.jsonl"

# Source logs with abundant PGG2-V39-LEAD-SNAPSHOT chains.
# These are the dry-live runs where the bot ran the V39/V39B quote-rescue path
# and captured both the buy quote AND the corresponding sell quote at every
# snapshot.
SOURCE_LOGS = [
    "pgg2_v39b_quote_rescue_drylive_20260512_125638.log",
    "pgg2_v39_online_drylive_20260512_115029.log",
    "pgg2_v39b_quote_rescue_drylive_20260512_132357.log",
    "pgg2_v39b_quote_rescue_drylive_20260512_133527.log",
    "pgg2_v39b_quote_rescue_drylive_20260512_125135.log",
    "pgg2_v39_online_drylive_20260512_114911.log",
    "pgg2_v39_online_drylive_20260512_114736.log",
    "pgg2_v39b_quote_rescue_drylive_20260513_114607.log",
    "pgg2_v42b_drylive_20260513_125600.log",
    "pgg2_v42b_drylive_20260513_124957.log",
    "pgg2_v42b_drylive_20260513_124352.log",
    "pgg2_v42_capture_20260513_114903.log",
    "pgg2_v42_capture_20260513_120330.log",
]

# Policy: pump_bc fee model. quote_out already includes protocol fees;
# overhead = 2 * tx_fee. ATA recoverable so rent_cost = 0.
TX_FEE_SOL = 0.000010
ATA_RECOVERABLE = True
ATA_RENT_SOL = 0.002039280
EXTRA_OVERHEAD = (2.0 * TX_FEE_SOL) + (0.0 if ATA_RECOVERABLE else ATA_RENT_SOL)
AMOUNT_SOL = 0.015

# Exit-policy thresholds for label_first_bank_or_scratch_pnl & friends.
BANK_THRESHOLD = 0.00060  # default v42 bank
SCRATCH_THRESHOLD = 0.00005
BANK_CLAMP_BONUS = 0.00060  # cap best_causal at bank + this on the high side


# Parser regexes
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
LEAD_SNAP_MARKER = "PGG2-V39-LEAD-SNAPSHOT "
KV_RE = re.compile(r"(\w+)=([+-]?[0-9.eE+-]+|\S+)")
BUY_BUCKET_RE = re.compile(r"^(\d+)/([0-9.]+)$")
SINCE_PREV_RE = re.compile(r"^(\d+)/([0-9.]+):(\d+)/([0-9.]+)$")

DQ_BUY_RE = re.compile(
    r"PGG2-DIRECT-QUOTE BUY (?P<mint>\S+) route=(?P<route>\S+) in=(?P<in_sol>[0-9.]+) "
    r"out=(?P<out_tokens>[0-9.]+) min=(?P<min_tok>[0-9.]+) "
    r"fee_bps=(?P<fee_bps>\d+) fee=(?P<fee>[0-9.]+)"
)
DQ_SELL_RE = re.compile(
    r"PGG2-DIRECT-QUOTE SELL (?P<mint>\S+) route=(?P<route>\S+) in_tokens=(?P<in_tokens>[0-9.]+) "
    r"out=(?P<out_sol>[0-9.]+) min=(?P<min_sol>[0-9.]+) "
    r"fee_bps=(?P<fee_bps>\d+) fee=(?P<fee>[0-9.]+)"
)


def parse_ts_ms(line: str) -> Optional[int]:
    m = TS_RE.match(line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        # Assume UTC (logs are UTC on Hetzner). Resolution is 1 s — fine enough
        # for 250/500/1000 ms label windows when chained with surrounding lines.
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def quote_all_in_pnl_pump_bc(quote_out_sol: float, cost_sol: float) -> float:
    """Replicates pgg2_live_raptor.quote_all_in_pnl for route=pump_bc."""
    return float(quote_out_sol) - float(cost_sol) - EXTRA_OVERHEAD


def get_sell_quote_out_at_snap(snap: Dict[str, Any]) -> float:
    """Return the gross sell-quote SOL output at a snapshot."""
    return float(snap["sell_out"])


def get_sell_quote_pnl_at_snap(snap: Dict[str, Any]) -> float:
    """Return the route-aware all-in PnL if we entered/exited at this snap.

    NOTE: This is the *single-snapshot* round-trip PnL — used as the snapshot's
    own quote-state, NOT as the future label. The future label re-prices the
    same tokens_bought_at_i against a future snap's sell_out.
    """
    return quote_all_in_pnl_pump_bc(get_sell_quote_out_at_snap(snap), AMOUNT_SOL)


def label_future_sell_pnl(
    chain: List[Dict[str, Any]],
    i: int,
    j: int,
) -> Optional[float]:
    """PnL of: BUY at snap_i tokens, SELL at snap_j curve state.

    Uses snap_j.sell_out_per_token_at_buyer_basis if available; we approximate
    by assuming the sell quote at snap_j was computed for `buy_tokens` from
    snap_j, while we actually bought tokens at snap_i. Both snaps already
    quote a 0.015 SOL buy round-trip, so reading off snap_j.sell_out gives
    the SOL we'd recover if the curve at j is the price at which we are
    re-quoting the SAME ~ buy_tokens_at_i.

    To stay faithful to the bot's own metric used during dry-live, we use
    snap_j.sell_out directly (this is what the bot logged as
    confirmed_live_equiv_all_in_pnl, which is the metric the V39B winner
    realized PnL was computed from).
    """
    snap_j = chain[j]
    sell_out_j = float(snap_j["sell_out"])
    if sell_out_j <= 0:
        return None
    return quote_all_in_pnl_pump_bc(sell_out_j, AMOUNT_SOL)


def first_after_ms(
    chain: List[Dict[str, Any]],
    i: int,
    delta_ms: int,
) -> Optional[int]:
    """Index of first snap j>i whose ts is >= chain[i].ts + delta_ms."""
    target = chain[i]["ts_ms"] + delta_ms
    for j in range(i + 1, len(chain)):
        if chain[j]["ts_ms"] >= target:
            return j
    return None


def best_causal_bank_pnl(
    chain: List[Dict[str, Any]],
    i: int,
    horizon_ms: int = 5000,
) -> Tuple[float, Optional[int], float, Optional[int]]:
    """Max future PnL in a horizon, plus exit-policy (bank/scratch) realization.

    Returns: (best_max_pnl_clamped, j_at_max, first_realized_pnl, j_at_realize)
    """
    best_pnl = float("-inf")
    best_j: Optional[int] = None
    realized_pnl = float("nan")
    realized_j: Optional[int] = None

    bank_cap = BANK_THRESHOLD + BANK_CLAMP_BONUS  # absolute upper cap on best

    ts0 = chain[i]["ts_ms"]
    for j in range(i + 1, len(chain)):
        if chain[j]["ts_ms"] - ts0 > horizon_ms:
            break
        p = label_future_sell_pnl(chain, i, j)
        if p is None:
            continue
        if p > best_pnl:
            best_pnl = p
            best_j = j
        # exit-policy realization rule:
        #   if p >= BANK_THRESHOLD -> bank exit
        #   elif p >= SCRATCH_THRESHOLD AND we've been negative once -> scratch
        if math.isnan(realized_pnl) and p >= BANK_THRESHOLD:
            realized_pnl = min(p, bank_cap)
            realized_j = j

    # If never banked: simulate scratch on first crossing of zero from below.
    if math.isnan(realized_pnl):
        # If best is positive but below bank, realize at scratch threshold.
        for j in range(i + 1, len(chain)):
            if chain[j]["ts_ms"] - ts0 > horizon_ms:
                break
            p = label_future_sell_pnl(chain, i, j)
            if p is None:
                continue
            if p >= SCRATCH_THRESHOLD:
                realized_pnl = p
                realized_j = j
                break

    # If still nothing positive — treat as the last observed PnL in horizon
    if math.isnan(realized_pnl):
        last_p = None
        last_j = None
        for j in range(i + 1, len(chain)):
            if chain[j]["ts_ms"] - ts0 > horizon_ms:
                break
            p = label_future_sell_pnl(chain, i, j)
            if p is not None:
                last_p = p
                last_j = j
        if last_p is not None:
            realized_pnl = last_p
            realized_j = last_j

    if best_pnl == float("-inf"):
        best_pnl = float("nan")
    if math.isnan(realized_pnl):
        realized_pnl = float("nan")

    return (
        min(best_pnl, bank_cap) if not math.isnan(best_pnl) else best_pnl,
        best_j,
        realized_pnl,
        realized_j,
    )


def max_adverse_before_bank(
    chain: List[Dict[str, Any]],
    i: int,
    horizon_ms: int = 5000,
) -> float:
    """Worst PnL observed BEFORE the bank-trigger or end-of-horizon."""
    ts0 = chain[i]["ts_ms"]
    worst = 0.0
    for j in range(i + 1, len(chain)):
        if chain[j]["ts_ms"] - ts0 > horizon_ms:
            break
        p = label_future_sell_pnl(chain, i, j)
        if p is None:
            continue
        if p >= BANK_THRESHOLD:
            break
        if p < worst:
            worst = p
    return worst


def parse_kv_line(rest: str) -> Dict[str, str]:
    """Tokenise a kv-style log tail into a dict of strings.

    Each token is `key=value`. Tokens are whitespace-separated. Some values
    are compound (e.g. `buy100=2/0.750` or `since_prev=0/0.000:0/0.000`); we
    just store them as raw strings and let callers parse.
    """
    out: Dict[str, str] = {}
    for tok in rest.split():
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        out[k] = v
    return out


def parse_log(log_path: Path) -> List[Dict[str, Any]]:
    """Return list of LEAD-SNAPSHOT rows (one per match), in file order."""
    rows: List[Dict[str, Any]] = []
    if not log_path.exists():
        return rows

    # Track inter-arrival of accountSubscribe-equivalent updates per mint
    last_snap_ts_per_mint: Dict[str, int] = {}
    snap_history_per_mint: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            ts_ms = parse_ts_ms(line)
            if ts_ms is None:
                continue
            idx = line.find(LEAD_SNAP_MARKER)
            if idx < 0:
                continue
            tail = line[idx + len(LEAD_SNAP_MARKER):]
            kv = parse_kv_line(tail)
            try:
                mint = kv["mint"]
                sell_out = float(kv["sell_out"])
            except (KeyError, ValueError):
                continue
            if sell_out <= 0:
                continue

            def _f(name: str, default: float = 0.0) -> float:
                try:
                    return float(kv.get(name, default))
                except (TypeError, ValueError):
                    return default

            def _i(name: str, default: int = 0) -> int:
                try:
                    return int(kv.get(name, default))
                except (TypeError, ValueError):
                    return default

            def _buy_bucket(name: str):
                v = kv.get(name, "0/0.000")
                m2 = BUY_BUCKET_RE.match(v)
                if not m2:
                    return 0, 0.0
                return int(m2.group(1)), float(m2.group(2))

            since_prev = kv.get("since_prev", "0/0.000:0/0.000")
            m3 = SINCE_PREV_RE.match(since_prev)
            if m3:
                spbn, spbs, spsn, spss = (
                    int(m3.group(1)),
                    float(m3.group(2)),
                    int(m3.group(3)),
                    float(m3.group(4)),
                )
            else:
                spbn = spbs = spsn = spss = 0
                if "/" in since_prev:
                    pass
            b100_n, b100_s = _buy_bucket("buy100")
            b250_n, b250_s = _buy_bucket("buy250")
            b500_n, b500_s = _buy_bucket("buy500")
            b1000_n, b1000_s = _buy_bucket("buy1000")

            row = {
                "log": log_path.name,
                "mint": mint,
                "ts_ms": ts_ms,
                "delay_ms": _i("delay_ms"),
                "buy_tokens": _f("buy_tokens"),
                "sell_out": sell_out,
                "buy_lat_ms": _i("buy_lat_ms"),
                "sell_lat_ms": _i("sell_lat_ms"),
                "route": kv.get("route", "pump_bc"),
                "pair_source": kv.get("pair_source", "unknown"),
                "sim_needed": _i("sim_needed"),
                "curve_price": _f("curve_price"),
                "quote_gradient": _f("quote_gradient"),
                "curve_gradient": _f("curve_gradient"),
                "buy100_n": b100_n,
                "buy100_sol": b100_s,
                "buy250_n": b250_n,
                "buy250_sol": b250_s,
                "buy500_n": b500_n,
                "buy500_sol": b500_s,
                "buy1000_n": b1000_n,
                "buy1000_sol": b1000_s,
                "since_prev_buy_n": spbn,
                "since_prev_buy_sol": spbs,
                "since_prev_sell_n": spsn,
                "since_prev_sell_sol": spss,
                "processed_live_equiv_all_in_pnl": _f("processed_live_equiv_all_in_pnl"),
                "confirmed_live_equiv_all_in_pnl": _f("confirmed_live_equiv_all_in_pnl"),
                "prefetched_sell_used": _i("prefetched_sell_used"),
                "prefetched_quote_age_ms": _i("prefetched_quote_age_ms"),
                "prefetched_orig_sell_lat_ms": _i("prefetched_original_sell_latency_ms"),
            }
            prev_ts = last_snap_ts_per_mint.get(mint)
            row["inter_arrival_ms"] = (ts_ms - prev_ts) if prev_ts is not None else -1
            last_snap_ts_per_mint[mint] = ts_ms

            # Causal deltas: vs prior snapshots in same mint chain
            prev_chain = snap_history_per_mint[mint]
            if prev_chain:
                p1 = prev_chain[-1]
                row["curve_delta_N_minus_1"] = row["curve_price"] - p1["curve_price"]
                row["quote_delta_N_minus_1"] = row["sell_out"] - p1["sell_out"]
            else:
                row["curve_delta_N_minus_1"] = 0.0
                row["quote_delta_N_minus_1"] = 0.0
            if len(prev_chain) >= 2:
                p2 = prev_chain[-2]
                row["curve_delta_N_minus_2"] = row["curve_price"] - p2["curve_price"]
                row["quote_delta_N_minus_2"] = row["sell_out"] - p2["sell_out"]
            else:
                row["curve_delta_N_minus_2"] = 0.0
                row["quote_delta_N_minus_2"] = 0.0

            snap_history_per_mint[mint].append(row)
            rows.append(row)
    return rows


def build_chains(snaps: List[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Group snapshots by (log, mint) into time-ordered chains."""
    chains: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for s in snaps:
        chains[(s["log"], s["mint"])].append(s)
    for k in chains:
        chains[k].sort(key=lambda r: r["ts_ms"])
    return chains


def main() -> int:
    all_rows: List[Dict[str, Any]] = []
    for log_name in SOURCE_LOGS:
        path = LOGS_DIR / log_name
        rows = parse_log(path)
        sys.stdout.write(f"[V42F-DATASET] log={log_name} snapshots={len(rows)}\n")
        all_rows.extend(rows)
    sys.stdout.write(f"[V42F-DATASET] total raw snaps={len(all_rows)}\n")

    chains = build_chains(all_rows)
    sys.stdout.write(f"[V42F-DATASET] mint-chains={len(chains)}\n")

    out_rows = 0
    with OUT_PATH.open("w", encoding="utf-8") as out:
        for (log_name, mint), chain in chains.items():
            if len(chain) < 2:
                continue  # cannot label without a future snap
            for i in range(len(chain) - 1):
                snap_i = chain[i]

                # Future label snaps
                buy_quote_out_at_i = float(snap_i["buy_tokens"])
                sell_quote_out_at_i = float(snap_i["sell_out"])
                tokens_bought_at_i = buy_quote_out_at_i

                # i+1, i+2
                j1 = i + 1 if i + 1 < len(chain) else None
                j2 = i + 2 if i + 2 < len(chain) else None
                pnl_next = label_future_sell_pnl(chain, i, j1) if j1 is not None else None
                pnl_next2 = label_future_sell_pnl(chain, i, j2) if j2 is not None else None

                j250 = first_after_ms(chain, i, 250)
                j500 = first_after_ms(chain, i, 500)
                j1000 = first_after_ms(chain, i, 1000)
                pnl_250 = label_future_sell_pnl(chain, i, j250) if j250 is not None else None
                pnl_500 = label_future_sell_pnl(chain, i, j500) if j500 is not None else None
                pnl_1000 = label_future_sell_pnl(chain, i, j1000) if j1000 is not None else None

                best_clamped, jmax, realized, jr = best_causal_bank_pnl(
                    chain, i, horizon_ms=5000
                )
                mad = max_adverse_before_bank(chain, i, horizon_ms=5000)

                # Causal features: snapshot i state + deltas vs i-1 and i-2
                feats: Dict[str, Any] = {
                    "f_curve_price": snap_i["curve_price"],
                    "f_quote_gradient": snap_i["quote_gradient"],
                    "f_curve_gradient": snap_i["curve_gradient"],
                    "f_curve_delta_N_minus_1": snap_i["curve_delta_N_minus_1"],
                    "f_curve_delta_N_minus_2": snap_i["curve_delta_N_minus_2"],
                    "f_quote_delta_N_minus_1": snap_i["quote_delta_N_minus_1"],
                    "f_quote_delta_N_minus_2": snap_i["quote_delta_N_minus_2"],
                    "f_buy100_n": snap_i["buy100_n"],
                    "f_buy100_sol": snap_i["buy100_sol"],
                    "f_buy250_n": snap_i["buy250_n"],
                    "f_buy250_sol": snap_i["buy250_sol"],
                    "f_buy500_n": snap_i["buy500_n"],
                    "f_buy500_sol": snap_i["buy500_sol"],
                    "f_buy1000_n": snap_i["buy1000_n"],
                    "f_buy1000_sol": snap_i["buy1000_sol"],
                    "f_since_prev_buy_n": snap_i["since_prev_buy_n"],
                    "f_since_prev_buy_sol": snap_i["since_prev_buy_sol"],
                    "f_since_prev_sell_n": snap_i["since_prev_sell_n"],
                    "f_since_prev_sell_sol": snap_i["since_prev_sell_sol"],
                    "f_buy_lat_ms": snap_i["buy_lat_ms"],
                    "f_sell_lat_ms": snap_i["sell_lat_ms"],
                    "f_pair_source": snap_i["pair_source"],
                    "f_sim_needed": snap_i["sim_needed"],
                    "f_inter_arrival_ms": snap_i["inter_arrival_ms"],
                    "f_processed_pnl_self": snap_i["processed_live_equiv_all_in_pnl"],
                    "f_confirmed_pnl_self": snap_i["confirmed_live_equiv_all_in_pnl"],
                    "f_prefetched_sell_used": snap_i["prefetched_sell_used"],
                    "f_prefetched_quote_age_ms": snap_i["prefetched_quote_age_ms"],
                    "f_source_late": int(snap_i["pair_source"] == "observed_raw_rpc"),
                    "f_recovered_quote": int(snap_i["pair_source"] in ("cache", "observed_raw_rpc")),
                    "f_fresh_quote": int(snap_i["pair_source"] == "current_sig"),
                    # Each feature's effective timestamp <= decision_ts
                    "f_feature_ts_ms": snap_i["ts_ms"],
                }

                row = {
                    "schema_version": "v42f_intersnap_1",
                    "log": log_name,
                    "mint": mint,
                    "snap_idx": i,
                    "decision_ts_ms": snap_i["ts_ms"],
                    "amount_sol": AMOUNT_SOL,
                    "buy_quote_out_at_i": buy_quote_out_at_i,
                    "sell_quote_out_at_i": sell_quote_out_at_i,
                    "tokens_bought_at_i": tokens_bought_at_i,
                    # Labels — all use future-snapshot prices (snap j > i)
                    "label_pnl_next_snapshot": pnl_next,
                    "label_pnl_next2_snapshot": pnl_next2,
                    "label_pnl_250ms": pnl_250,
                    "label_pnl_500ms": pnl_500,
                    "label_pnl_1000ms": pnl_1000,
                    "label_best_causal_bank_pnl": best_clamped,
                    "label_first_bank_or_scratch_pnl": realized,
                    "label_max_adverse_before_bank": mad,
                    "label_first_bank_idx": jr,
                    "label_best_max_idx": jmax,
                    # CAUSAL features (every f_* must have ts <= decision_ts_ms)
                    "features": feats,
                }
                out.write(json.dumps(row) + "\n")
                out_rows += 1

    sys.stdout.write(f"[V42F-DATASET] rows_emitted={out_rows} out={OUT_PATH}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
