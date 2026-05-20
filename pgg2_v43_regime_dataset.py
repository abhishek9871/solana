#!/usr/bin/env python3
"""
pgg2_v43_regime_dataset.py — V43 Regime Dataset Builder.

For every 1s bucket across all historical raw/jsonl logs on disk, compute
rolling regime features using ONLY data observable at-or-before the bucket's
decision_ts. Bucket windows: 5s, 10s, 30s, 60s, 120s.

Inputs (read-only):
    /root/piggy/data/pgg2_v*_raw.jsonl                    (trade flow)
    /root/piggy/data/pgg2_v*_decisions.jsonl              (sniper decisions, for feed_health & latency proxies)
    /root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl          (per-mint quote snapshots w/ labels)

Output:
    /root/piggy/V43_REGIME_DATASET.jsonl                  (one record per (log, bucket_ts))

STRICT CAUSALITY (features):
    Every feature value's source-timestamp ≤ bucket decision_ts. The leakage
    checker in Phase 2 audits this hard.

Labels (forward-leaking — intended ONLY for evaluation):
    window_has_10w0l_35m_possible  bool
    window_has_10w0l_20m_possible  bool
    window_quote_continuation_rate float (next 60s)
    window_negative_rate           float (next 60s)
    window_net_all_in_pnl          float (sum best-bank label next 60s)
    window_max_loss                float (worst label next 60s)

No live trade, no transaction send. Pure offline replay.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from collections import deque, defaultdict
from typing import Any, Dict, List, Tuple

DATA_DIR = "/root/piggy/data"
V42F_DATASET = "/root/piggy/V42F_INTERSNAPSHOT_DATASET.jsonl"
OUTPUT = "/root/piggy/V43_REGIME_DATASET.jsonl"

# Window sizes in seconds for rolling regime features
WINDOWS_S = [5, 10, 30, 60, 120]

# Bucket cadence in milliseconds (1 bucket every 1000 ms of replay time)
BUCKET_MS = 1000

# Label windows
LABEL_35M_MS = 35 * 60 * 1000
LABEL_20M_MS = 20 * 60 * 1000
LABEL_60S_MS = 60 * 1000

# V42F exit policy (mirrors pgg2_v42f_intersnapshot_dataset.py exactly)
BANK_THRESHOLD = 0.00060
SCRATCH_THRESHOLD = 0.00005
BANK_CLAMP_BONUS = 0.00060

# Continuation breadth threshold (sell quote improvement)
QUOTE_IMPROVE_THRESHOLD = 0.0002


def _logname_from_raw_path(path: str) -> str:
    base = os.path.basename(path)
    if base.endswith("_raw.jsonl"):
        base = base[: -len("_raw.jsonl")]
    return base + ".log"


def _load_v42f_index() -> Dict[str, List[Dict[str, Any]]]:
    """Group V42F intersnapshot rows by log; rows sorted by (mint, snap_idx).

    Each per-log list is sorted by decision_ts_ms ascending for fast windowed scans.
    """
    idx: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    if not os.path.exists(V42F_DATASET):
        return idx
    with open(V42F_DATASET, "r", encoding="utf-8") as fh:
        for ln in fh:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            log = d.get("log")
            if not log:
                continue
            idx[log].append(d)
    for log in idx:
        idx[log].sort(key=lambda r: r.get("decision_ts_ms", 0))
    return idx


def _iter_raw_events(raw_path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(raw_path, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                out.append(d)
    except FileNotFoundError:
        return out
    return out


def _iter_decisions(dec_path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        with open(dec_path, "r", encoding="utf-8") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                out.append(d)
    except FileNotFoundError:
        return out
    return out


def _label_first_bank_or_scratch(
    chain: List[Dict[str, Any]], i: int, horizon_ms: int = 5000
) -> Tuple[float, float]:
    """Return (best_clamped_pnl, realized_pnl) per V42F exit policy.

    chain entries must each contain buy_quote_out_at_i, sell_quote_out_at_i, ts_ms.
    """
    if i >= len(chain) - 1:
        return (0.0, 0.0)

    ts0 = chain[i]["ts_ms"]
    tokens_bought = chain[i].get("buy_quote_out_at_i", 0.0) or 0.0
    if tokens_bought <= 0:
        return (0.0, 0.0)
    amount_sol = chain[i].get("amount_sol", 0.015)

    bank_cap = BANK_THRESHOLD + BANK_CLAMP_BONUS
    best_pnl = float("-inf")
    realized_pnl = float("nan")

    for j in range(i + 1, len(chain)):
        if chain[j]["ts_ms"] - ts0 > horizon_ms:
            break
        sell_per_token = chain[j].get("sell_quote_out_per_token_at_j")
        if sell_per_token is None:
            # Fallback: scale the j-snapshot's sell_quote by ratio of token holdings
            sell_total_j = chain[j].get("sell_quote_out_at_i", 0.0) or 0.0
            tokens_at_j = chain[j].get("buy_quote_out_at_i", 0.0) or 0.0
            if tokens_at_j <= 0:
                continue
            sell_per_token = sell_total_j / tokens_at_j
        gross = sell_per_token * tokens_bought
        pnl = gross - amount_sol - 0.00002  # 2 txfee
        if pnl > best_pnl:
            best_pnl = pnl
        import math
        if math.isnan(realized_pnl) and pnl >= BANK_THRESHOLD:
            realized_pnl = min(pnl, bank_cap)

    import math
    if math.isnan(realized_pnl):
        # try scratch on first cross above SCRATCH_THRESHOLD
        for j in range(i + 1, len(chain)):
            if chain[j]["ts_ms"] - ts0 > horizon_ms:
                break
            sell_per_token = chain[j].get("sell_quote_out_per_token_at_j")
            if sell_per_token is None:
                sell_total_j = chain[j].get("sell_quote_out_at_i", 0.0) or 0.0
                tokens_at_j = chain[j].get("buy_quote_out_at_i", 0.0) or 0.0
                if tokens_at_j <= 0:
                    continue
                sell_per_token = sell_total_j / tokens_at_j
            gross = sell_per_token * tokens_bought
            pnl = gross - amount_sol - 0.00002
            if pnl >= SCRATCH_THRESHOLD:
                realized_pnl = pnl
                break

    if math.isnan(realized_pnl):
        # last-observed PnL
        for j in range(len(chain) - 1, i, -1):
            if chain[j]["ts_ms"] - ts0 > horizon_ms + 5000:
                continue
            sell_per_token = chain[j].get("sell_quote_out_per_token_at_j")
            if sell_per_token is None:
                sell_total_j = chain[j].get("sell_quote_out_at_i", 0.0) or 0.0
                tokens_at_j = chain[j].get("buy_quote_out_at_i", 0.0) or 0.0
                if tokens_at_j <= 0:
                    continue
                sell_per_token = sell_total_j / tokens_at_j
            gross = sell_per_token * tokens_bought
            realized_pnl = gross - amount_sol - 0.00002
            break

    if math.isnan(realized_pnl):
        realized_pnl = 0.0
    if best_pnl == float("-inf"):
        best_pnl = realized_pnl
    return (min(best_pnl, bank_cap), realized_pnl)


def _per_log_mint_chains(v42f_rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group V42F rows into per-mint chains sorted by snap_idx (decision_ts_ms)."""
    by_mint: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in v42f_rows:
        mint = r.get("mint")
        if not mint:
            continue
        # Pre-compute sell_quote_per_token for label fast-paths
        f = r.get("features", {})
        sell_q = r.get("sell_quote_out_at_i", 0.0) or 0.0
        tk = r.get("buy_quote_out_at_i", 0.0) or 0.0
        if tk > 0:
            r["sell_quote_out_per_token_at_j"] = sell_q / tk
        else:
            r["sell_quote_out_per_token_at_j"] = 0.0
        by_mint[mint].append(r)
    for m in by_mint:
        by_mint[m].sort(key=lambda r: r.get("snap_idx", 0))
    return by_mint


def _compute_label_bank_realized_for_row(chain: List[Dict[str, Any]], i: int) -> float:
    # Use V42F-precomputed label if available (matches V42F exit policy)
    r = chain[i]
    if "label_first_bank_or_scratch_pnl" in r:
        return r["label_first_bank_or_scratch_pnl"]
    _, realized = _label_first_bank_or_scratch(chain, i)
    return realized


def _build_buckets_for_log(
    log_basename: str,
    raw_events: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
    v42f_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build a list of 1s bucket records for a single log.

    Causality: each bucket's features look back over the rolling windows ending at
    decision_ts (inclusive). Labels look forward (future-leaking — for evaluation only).
    """
    if not raw_events:
        return []

    # Sort everything by time
    raw_events.sort(key=lambda r: r.get("ts_ms", 0))
    decisions.sort(key=lambda r: r.get("ts_ms", 0))
    v42f_rows.sort(key=lambda r: r.get("decision_ts_ms", 0))

    by_mint = _per_log_mint_chains(v42f_rows)

    t_start = (raw_events[0].get("ts_ms", 0) // BUCKET_MS) * BUCKET_MS
    t_end = (raw_events[-1].get("ts_ms", 0) // BUCKET_MS) * BUCKET_MS

    # Pre-index events into per-second bins for efficient rolling sums
    raw_by_sec: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for e in raw_events:
        sec = e.get("ts_ms", 0) // 1000
        raw_by_sec[sec].append(e)

    dec_by_sec: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for d in decisions:
        sec = d.get("ts_ms", 0) // 1000
        dec_by_sec[sec].append(d)

    v42f_by_sec: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for v in v42f_rows:
        sec = v.get("decision_ts_ms", 0) // 1000
        v42f_by_sec[sec].append(v)

    # Pre-compute per-mint snapshot positions for forward-label scans
    mint_snap_pos: Dict[str, List[int]] = {}
    for m, chain in by_mint.items():
        mint_snap_pos[m] = [r.get("decision_ts_ms", 0) for r in chain]

    out: List[Dict[str, Any]] = []
    bucket = t_start
    # Single forward pass — bucket every 1s
    while bucket <= t_end:
        decision_ts = bucket  # right-edge of window
        rec: Dict[str, Any] = {
            "schema_version": "v43_regime_1",
            "log": log_basename,
            "decision_ts_ms": decision_ts,
        }

        # ============ rolling raw flow features ============
        for w in WINDOWS_S:
            w_start_sec = (decision_ts - w * 1000) // 1000
            w_end_sec = decision_ts // 1000
            buys_n = 0
            sells_n = 0
            buys_sol = 0.0
            sells_sol = 0.0
            mints_in_window: set = set()
            creates_in_window: set = set()
            shred_events = 0
            for s in range(w_start_sec, w_end_sec + 1):
                if s not in raw_by_sec:
                    continue
                for e in raw_by_sec[s]:
                    et = e.get("ts_ms", 0)
                    if et < decision_ts - w * 1000 or et > decision_ts:
                        continue
                    shred_events += 1
                    side = e.get("side", "")
                    ik = e.get("instruction_kind", "")
                    m = e.get("mint")
                    if m:
                        mints_in_window.add(m)
                    if ik == "create" or e.get("kind") == "create":
                        if m:
                            creates_in_window.add(m)
                    if side == "buy":
                        buys_n += 1
                        try:
                            buys_sol += float(e.get("sol", 0.0))
                        except Exception:
                            pass
                    elif side == "sell":
                        sells_n += 1
                        try:
                            sells_sol += float(e.get("sol", 0.0))
                        except Exception:
                            pass
            rec[f"total_pump_bc_quoteable_mints_{w}s"] = len(mints_in_window)
            rec[f"total_created_mints_{w}s"] = len(creates_in_window)
            if w <= 60:
                rec[f"total_shred_events_{w}s"] = shred_events
                rec[f"buy_count_{w}s"] = buys_n
                rec[f"sell_count_{w}s"] = sells_n
                rec[f"buy_sol_total_{w}s"] = round(buys_sol, 9)
                rec[f"sell_sol_total_{w}s"] = round(sells_sol, 9)
                denom = sells_n if sells_n > 0 else 1
                rec[f"buy_sell_ratio_{w}s"] = round(buys_n / denom, 6)

        # ============ rolling quote/breadth features (30s) ============
        w30_start = decision_ts - 30000
        # Use V42F rows whose decision_ts_ms ∈ (w30_start, decision_ts]
        rows30 = []
        for s in range((w30_start) // 1000, (decision_ts // 1000) + 1):
            if s not in v42f_by_sec:
                continue
            for r in v42f_by_sec[s]:
                rt = r.get("decision_ts_ms", 0)
                if rt <= w30_start or rt > decision_ts:
                    continue
                rows30.append(r)

        # positive quote gradient — distinct mints
        pos_grad_30 = set()
        sell_improve_30 = set()
        curve_delta_pos_30 = set()
        cont_30 = set()
        improvements = []
        for r in rows30:
            f = r.get("features", {})
            qg = f.get("f_quote_gradient", 0.0) or 0.0
            cd = f.get("f_curve_delta_N_minus_1", 0.0) or 0.0
            qd1 = f.get("f_quote_delta_N_minus_1", 0.0) or 0.0
            m = r.get("mint")
            if qg > 0:
                pos_grad_30.add(m)
            if qd1 > QUOTE_IMPROVE_THRESHOLD:
                sell_improve_30.add(m)
            if cd > 0:
                curve_delta_pos_30.add(m)
            if qd1 > 0:
                cont_30.add(m)
                improvements.append(qd1)
        rec["distinct_mints_with_positive_quote_gradient_30s"] = len(pos_grad_30)
        rec[
            "distinct_mints_with_sell_quote_improvement_gt_0p0002_30s"
        ] = len(sell_improve_30)
        rec["distinct_mints_with_positive_curve_delta_30s"] = len(curve_delta_pos_30)
        rec["distinct_mints_with_quote_n_to_n_plus_1_improvement_30s"] = len(cont_30)
        if improvements:
            improvements.sort()
            mid = improvements[len(improvements) // 2]
            p90 = improvements[max(0, int(len(improvements) * 0.9) - 1)]
        else:
            mid = 0.0
            p90 = 0.0
        rec["median_quote_improvement_across_mints_30s"] = round(mid, 9)
        rec["p90_quote_improvement_across_mints_30s"] = round(p90, 9)

        # ============ prior-window outcome breadth (60s) ============
        # For mints with snapshot decision_ts ∈ (decision_ts - 60s, decision_ts],
        # use the V42F-precomputed label (which itself looked 5s forward from THAT snap).
        # That label uses information AT or BEFORE decision_ts, so it remains causal.
        w60_start = decision_ts - 60000
        banked_60 = set()
        lost_60 = set()
        for s in range(w60_start // 1000, (decision_ts // 1000) + 1):
            if s not in v42f_by_sec:
                continue
            for r in v42f_by_sec[s]:
                rt = r.get("decision_ts_ms", 0)
                if rt <= w60_start or rt > decision_ts:
                    continue
                lbl = r.get("label_first_bank_or_scratch_pnl", 0.0) or 0.0
                m = r.get("mint")
                if lbl >= SCRATCH_THRESHOLD:
                    banked_60.add(m)
                elif lbl < -SCRATCH_THRESHOLD:
                    lost_60.add(m)
        rec["distinct_mints_that_would_have_banked_under_label_60s"] = len(banked_60)
        rec["distinct_mints_that_would_have_lost_under_label_60s"] = len(lost_60)
        denom = (len(banked_60) + len(lost_60)) or 1
        rec["cohort_hit_rate_60s"] = round(len(banked_60) / denom, 6)

        # ============ feed health / latency / pair_source ============
        # Pull from decisions in last 30s
        w_dec_start = decision_ts - 30000
        rec_buy_lats: List[float] = []
        recovered_or_late = 0
        decs_30 = 0
        sim_needed_zero = 0
        pair_cur_sig = 0
        for s in range(w_dec_start // 1000, (decision_ts // 1000) + 1):
            if s not in dec_by_sec:
                continue
            for d in dec_by_sec[s]:
                dt = d.get("ts_ms", 0)
                if dt <= w_dec_start or dt > decision_ts:
                    continue
                decs_30 += 1
                f = d.get("features", {})
                lat = f.get("buy_lat_ms") or f.get("f_buy_lat_ms")
                if isinstance(lat, (int, float)):
                    rec_buy_lats.append(float(lat))
                if f.get("source_late") or f.get("f_source_late"):
                    recovered_or_late += 1
                if (f.get("sim_needed") in (0, "0", False, None)) or (
                    f.get("f_sim_needed") in (0, "0", False, None)
                ):
                    sim_needed_zero += 1
                ps = f.get("pair_source") or f.get("f_pair_source")
                if ps == "current_sig":
                    pair_cur_sig += 1
        # Also include latencies from V42F rows in last 30s
        v42_lats_buy: List[float] = []
        v42_lats_sell: List[float] = []
        v42_total = 0
        v42_sim_zero = 0
        v42_cur_sig = 0
        for r in rows30:
            f = r.get("features", {})
            bl = f.get("f_buy_lat_ms")
            sl = f.get("f_sell_lat_ms")
            if isinstance(bl, (int, float)):
                v42_lats_buy.append(float(bl))
            if isinstance(sl, (int, float)):
                v42_lats_sell.append(float(sl))
            v42_total += 1
            if f.get("f_sim_needed") in (0, "0", False, None):
                v42_sim_zero += 1
            if f.get("f_pair_source") == "current_sig":
                v42_cur_sig += 1
        all_buy_lats = sorted(rec_buy_lats + v42_lats_buy)
        if all_buy_lats:
            p50 = all_buy_lats[len(all_buy_lats) // 2]
            p90 = all_buy_lats[max(0, int(len(all_buy_lats) * 0.9) - 1)]
        else:
            p50 = -1.0
            p90 = -1.0
        rec["quote_latency_p50_30s"] = round(float(p50), 2)
        rec["quote_latency_p90_30s"] = round(float(p90), 2)
        total_60 = max(1, decs_30 + v42_total)
        rec["pair_source_current_sig_rate_60s"] = round(
            (pair_cur_sig + v42_cur_sig) / total_60, 6
        )
        rec["sim_needed_0_rate_60s"] = round(
            (sim_needed_zero + v42_sim_zero) / total_60, 6
        )
        rec["feed_health_active_decisions_30s"] = decs_30 + v42_total
        rec["feed_health_late_count_30s"] = recovered_or_late

        # ============ LABELS (future-leaking, EVAL-ONLY) ============
        # window_quote_continuation_rate / window_negative_rate / pnl over next 60s.
        f60_end = decision_ts + LABEL_60S_MS
        next_pos = 0
        next_neg = 0
        next_total = 0
        sum_bank_pnl = 0.0
        max_loss = 0.0
        # walk forward in V42F rows whose decision_ts ∈ (decision_ts, decision_ts + 60s]
        for s in range(decision_ts // 1000, (f60_end // 1000) + 1):
            if s not in v42f_by_sec:
                continue
            for r in v42f_by_sec[s]:
                rt = r.get("decision_ts_ms", 0)
                if rt <= decision_ts or rt > f60_end:
                    continue
                next_total += 1
                lbl = r.get("label_first_bank_or_scratch_pnl", 0.0) or 0.0
                best = r.get("label_best_causal_bank_pnl", 0.0) or 0.0
                sum_bank_pnl += best
                if lbl >= SCRATCH_THRESHOLD:
                    next_pos += 1
                elif lbl < -SCRATCH_THRESHOLD:
                    next_neg += 1
                if lbl < max_loss:
                    max_loss = lbl
        rec["window_quote_continuation_rate"] = (
            round(next_pos / next_total, 6) if next_total else 0.0
        )
        rec["window_negative_rate"] = (
            round(next_neg / next_total, 6) if next_total else 0.0
        )
        rec["window_net_all_in_pnl"] = round(sum_bank_pnl, 9)
        rec["window_max_loss"] = round(max_loss, 9)

        # window_has_10w0l_35m_possible / 20m_possible:
        # Simulate sequential V42-style entries (one per mint snapshot, banked label),
        # never enter the same mint twice, stop on first NEGATIVE label.
        # Did we accumulate 10 positives (>=SCRATCH_THRESHOLD) without a negative in the window?
        for horiz_ms, key in (
            (LABEL_35M_MS, "window_has_10w0l_35m_possible"),
            (LABEL_20M_MS, "window_has_10w0l_20m_possible"),
        ):
            horiz_end = decision_ts + horiz_ms
            wins = 0
            losses = 0
            already_entered: set = set()
            # walk forward V42F rows in time order
            possible = False
            for s in range(decision_ts // 1000, (horiz_end // 1000) + 1):
                if s not in v42f_by_sec:
                    continue
                rows = sorted(
                    v42f_by_sec[s], key=lambda r: r.get("decision_ts_ms", 0)
                )
                for r in rows:
                    rt = r.get("decision_ts_ms", 0)
                    if rt <= decision_ts or rt > horiz_end:
                        continue
                    m = r.get("mint")
                    if m in already_entered:
                        continue
                    already_entered.add(m)
                    lbl = r.get("label_first_bank_or_scratch_pnl", 0.0) or 0.0
                    if lbl >= SCRATCH_THRESHOLD:
                        wins += 1
                        if wins >= 10 and losses == 0:
                            possible = True
                            break
                    elif lbl < -SCRATCH_THRESHOLD:
                        losses += 1
                if possible or losses > 0:
                    break
            rec[key] = bool(possible)

        out.append(rec)
        bucket += BUCKET_MS

    return out


def main() -> int:
    t0 = time.time()
    print(f"[v43-dataset] starting bucket extraction at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    v42f_index = _load_v42f_index()
    print(f"[v43-dataset] V42F dataset loaded: {sum(len(v) for v in v42f_index.values())} rows across {len(v42f_index)} logs")

    raw_paths = sorted(glob.glob(os.path.join(DATA_DIR, "pgg2_v*_raw.jsonl")))
    print(f"[v43-dataset] {len(raw_paths)} raw.jsonl files to process")

    total_buckets = 0
    with open(OUTPUT, "w", encoding="utf-8") as out_fh:
        for ri, raw_path in enumerate(raw_paths, 1):
            log_basename = _logname_from_raw_path(raw_path)
            dec_path = raw_path.replace("_raw.jsonl", "_decisions.jsonl")
            t1 = time.time()
            raw_events = _iter_raw_events(raw_path)
            decisions = _iter_decisions(dec_path)
            v42f_rows = v42f_index.get(log_basename, [])
            if not raw_events:
                print(f"[v43-dataset] [{ri}/{len(raw_paths)}] {log_basename} — empty raw, skip")
                continue
            buckets = _build_buckets_for_log(log_basename, raw_events, decisions, v42f_rows)
            for b in buckets:
                out_fh.write(json.dumps(b, separators=(",", ":")))
                out_fh.write("\n")
            total_buckets += len(buckets)
            print(
                f"[v43-dataset] [{ri}/{len(raw_paths)}] {log_basename} "
                f"raw={len(raw_events)} dec={len(decisions)} v42f={len(v42f_rows)} "
                f"buckets={len(buckets)} cum={total_buckets} dt={time.time()-t1:.1f}s"
            )

    dt = time.time() - t0
    print(
        f"[v43-dataset] DONE — {total_buckets} buckets written in {dt:.1f}s -> {OUTPUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
