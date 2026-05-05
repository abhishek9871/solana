import argparse
import json
import math
import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


WINDOWS_MS = (250, 700, 1500, 3000, 8000)


@dataclass(frozen=True)
class Candidate:
    tape: str
    mint: str
    ts_ms: int
    age_ms: int
    price: float
    buy250: float
    buy700: float
    buy1500: float
    buy3000: float
    buy8000: float
    sell700: float
    sell1500: float
    sell3000: float
    uniq700: int
    uniq1500: int
    uniq3000: int
    top700: float
    top1500: float
    hhi700: float
    hhi1500: float
    move250: float
    move700: float
    move1500: float
    move3000: float
    off_peak8000: float
    last_sell_age_ms: int
    event_sol: float
    future_max60: float
    future_max120: float
    future_min20: float
    hit132_ms: int
    hit155_ms: int
    hit200_ms: int
    stop88_ms: int
    nofollow_ms: int
    pnl_001: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast raw-tape validator for PGG2 moonshot entry rules.")
    parser.add_argument("raw", nargs="+", help="Raw JSONL tape(s) to analyze.")
    parser.add_argument("--max-candidates-per-mint", type=int, default=18)
    parser.add_argument("--max-age-ms", type=int, default=120_000)
    parser.add_argument("--target-mult", type=float, default=1.32)
    parser.add_argument("--stop-mult", type=float, default=0.88)
    parser.add_argument("--stake-sol", type=float, default=0.01)
    parser.add_argument("--random-rules", type=int, default=12_000)
    parser.add_argument("--min-trades", type=int, default=3)
    parser.add_argument("--top", type=int, default=25)
    return parser.parse_args()


def user_of(e: dict[str, Any]) -> str:
    return str(e.get("user") or e.get("signer") or "")


def window_stats(events: list[dict[str, Any]], idx: int, now: int, window_ms: int) -> dict[str, Any]:
    buys: list[dict[str, Any]] = []
    sells: list[dict[str, Any]] = []
    prices: list[float] = []
    for j in range(idx, -1, -1):
        e = events[j]
        if now - int(e["ts_ms"]) > window_ms:
            break
        p = float(e.get("price") or 0.0)
        if p > 0:
            prices.append(p)
        if e.get("side") == "buy":
            buys.append(e)
        else:
            sells.append(e)
    buy_sol = sum(float(e.get("sol") or 0.0) for e in buys)
    sell_sol = sum(float(e.get("sol") or 0.0) for e in sells)
    by_wallet: Counter[str] = Counter()
    for e in buys:
        by_wallet[user_of(e)] += float(e.get("sol") or 0.0)
    uniq = len([k for k in by_wallet if k])
    top = max(by_wallet.values()) / buy_sol if buy_sol > 0 and by_wallet else 0.0
    hhi = sum((v / buy_sol) ** 2 for v in by_wallet.values()) if buy_sol > 0 else 0.0
    earliest = prices[-1] if prices else 0.0
    peak = max(prices) if prices else 0.0
    return {
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "uniq": uniq,
        "top": top,
        "hhi": hhi,
        "earliest_price": earliest,
        "peak_price": peak,
    }


def future_outcome(
    events: list[dict[str, Any]],
    idx: int,
    entry_price: float,
    target_mult: float,
    stop_mult: float,
    stake_sol: float,
) -> dict[str, Any]:
    entry_ts = int(events[idx]["ts_ms"])
    max60 = 1.0
    max120 = 1.0
    min20 = 1.0
    hit132_ms = hit155_ms = hit200_ms = stop88_ms = nofollow_ms = 0
    nofollow_checked = False
    final18 = 1.0
    final60 = 1.0
    for j in range(idx, len(events)):
        e = events[j]
        dt = int(e["ts_ms"]) - entry_ts
        if dt < 0:
            continue
        if dt > 120_000:
            break
        p = float(e.get("price") or 0.0)
        if p <= 0:
            continue
        mult = p / entry_price
        if dt <= 60_000:
            max60 = max(max60, mult)
            final60 = mult
        max120 = max(max120, mult)
        if dt <= 20_000:
            min20 = min(min20, mult)
        if dt <= 18_000:
            final18 = mult
        if not nofollow_checked and dt >= 4_000:
            nofollow_checked = True
            if max60 < 1.06 and mult <= 0.99:
                nofollow_ms = dt
        if not hit132_ms and mult >= 1.32:
            hit132_ms = dt
        if not hit155_ms and mult >= 1.55:
            hit155_ms = dt
        if not hit200_ms and mult >= 2.0:
            hit200_ms = dt
        if not stop88_ms and mult <= stop_mult:
            stop88_ms = dt

    # Lightweight paper approximation for ranking rules, not final accounting.
    if nofollow_ms and (not hit132_ms or nofollow_ms < hit132_ms):
        pnl = -0.00055 * (stake_sol / 0.01)
    elif stop88_ms and (not hit132_ms or stop88_ms < hit132_ms):
        pnl = (stop_mult - 1.0) * stake_sol
    elif hit132_ms:
        pnl = (target_mult - 1.0) * stake_sol
    else:
        pnl = (final18 - 1.0) * stake_sol
    return {
        "future_max60": max60,
        "future_max120": max120,
        "future_min20": min20,
        "hit132_ms": hit132_ms,
        "hit155_ms": hit155_ms,
        "hit200_ms": hit200_ms,
        "stop88_ms": stop88_ms,
        "nofollow_ms": nofollow_ms,
        "pnl_001": pnl,
    }


def load_candidates(path: str, args: argparse.Namespace) -> list[Candidate]:
    tape = Path(path).stem
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    create_ts: dict[str, int] = {}
    total_rows = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            total_rows += 1
            try:
                x = json.loads(line)
            except Exception:
                continue
            m = x.get("mint")
            if not m:
                continue
            ts = int(x.get("ts_ms") or 0)
            if x.get("kind") == "create" and ts and m not in create_ts:
                create_ts[m] = ts
                continue
            if x.get("kind") != "trade":
                continue
            p = float(x.get("curve_price") or 0.0)
            if p <= 0:
                continue
            by_mint[m].append(
                {
                    "ts_ms": ts,
                    "side": x.get("side"),
                    "sol": float(x.get("sol") or 0.0),
                    "user": user_of(x),
                    "price": p,
                }
            )
            if m not in create_ts:
                create_ts[m] = ts

    candidates: list[Candidate] = []
    for mint, events in by_mint.items():
        events.sort(key=lambda e: int(e["ts_ms"]))
        used_for_mint = 0
        last_sell_ts = 0
        for idx, e in enumerate(events):
            ts = int(e["ts_ms"])
            if e.get("side") != "buy":
                last_sell_ts = ts
                continue
            if used_for_mint >= args.max_candidates_per_mint:
                continue
            age = ts - create_ts.get(mint, ts)
            if age < 0 or age > args.max_age_ms:
                continue
            price = float(e["price"])
            stats = {w: window_stats(events, idx, ts, w) for w in WINDOWS_MS}
            s250, s700, s1500, s3000, s8000 = (stats[w] for w in WINDOWS_MS)
            if s700["buy_sol"] <= 0:
                continue
            out = future_outcome(events, idx, price, args.target_mult, args.stop_mult, args.stake_sol)
            last_sell_age = ts - last_sell_ts if last_sell_ts else 999999
            candidates.append(
                Candidate(
                    tape=tape,
                    mint=mint,
                    ts_ms=ts,
                    age_ms=age,
                    price=price,
                    buy250=s250["buy_sol"],
                    buy700=s700["buy_sol"],
                    buy1500=s1500["buy_sol"],
                    buy3000=s3000["buy_sol"],
                    buy8000=s8000["buy_sol"],
                    sell700=s700["sell_sol"],
                    sell1500=s1500["sell_sol"],
                    sell3000=s3000["sell_sol"],
                    uniq700=s700["uniq"],
                    uniq1500=s1500["uniq"],
                    uniq3000=s3000["uniq"],
                    top700=s700["top"],
                    top1500=s1500["top"],
                    hhi700=s700["hhi"],
                    hhi1500=s1500["hhi"],
                    move250=price / s250["earliest_price"] if s250["earliest_price"] > 0 else 1.0,
                    move700=price / s700["earliest_price"] if s700["earliest_price"] > 0 else 1.0,
                    move1500=price / s1500["earliest_price"] if s1500["earliest_price"] > 0 else 1.0,
                    move3000=price / s3000["earliest_price"] if s3000["earliest_price"] > 0 else 1.0,
                    off_peak8000=price / s8000["peak_price"] if s8000["peak_price"] > 0 else 1.0,
                    last_sell_age_ms=last_sell_age,
                    event_sol=float(e.get("sol") or 0.0),
                    **out,
                )
            )
            used_for_mint += 1
    print(f"loaded {path}: rows={total_rows:,} mints={len(by_mint):,} candidates={len(candidates):,}")
    return candidates


def passes(c: Candidate, r: dict[str, float]) -> bool:
    return (
        r["age_min"] <= c.age_ms <= r["age_max"]
        and c.buy1500 >= r["buy1500_min"]
        and c.buy1500 <= r["buy1500_max"]
        and c.buy700 >= r["buy700_min"]
        and c.uniq1500 >= r["uniq1500_min"]
        and c.top1500 <= r["top1500_max"]
        and c.hhi1500 <= r["hhi1500_max"]
        and c.move700 >= r["move700_min"]
        and c.move1500 >= r["move1500_min"]
        and c.off_peak8000 >= r["off_peak_min"]
        and c.sell1500 <= max(0.010, c.buy1500 * r["sell1500_ratio_max"])
        and c.last_sell_age_ms >= r["last_sell_min_age"]
    )


def eval_rule(candidates: list[Candidate], rule: dict[str, float]) -> dict[str, Any]:
    selected: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for c in candidates:
        key = (c.tape, c.mint)
        if key in seen:
            continue
        if passes(c, rule):
            selected.append(c)
            seen.add(key)
    by_tape: dict[str, list[Candidate]] = defaultdict(list)
    for c in selected:
        by_tape[c.tape].append(c)
    pnl = sum(c.pnl_001 for c in selected)
    wins = sum(1 for c in selected if c.pnl_001 > 0)
    losses = sum(1 for c in selected if c.pnl_001 < 0)
    moon132 = sum(1 for c in selected if c.future_max60 >= 1.32)
    moon200 = sum(1 for c in selected if c.future_max60 >= 2.0)
    worst_tape = min((sum(x.pnl_001 for x in xs) for xs in by_tape.values()), default=0.0)
    return {
        "n": len(selected),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
        "avg": pnl / len(selected) if selected else 0.0,
        "moon132": moon132,
        "moon200": moon200,
        "worst_tape": worst_tape,
        "by_tape": {k: (len(v), sum(c.pnl_001 for c in v)) for k, v in by_tape.items()},
    }


def build_columns(candidates: list[Candidate]) -> dict[str, Any]:
    tape_names = sorted({c.tape for c in candidates})
    tape_to_id = {t: i for i, t in enumerate(tape_names)}
    key_to_id: dict[tuple[str, str], int] = {}
    keys: list[int] = []
    for c in candidates:
        key = (c.tape, c.mint)
        if key not in key_to_id:
            key_to_id[key] = len(key_to_id)
        keys.append(key_to_id[key])

    def arr(name: str, dtype: Any = float) -> np.ndarray:
        return np.array([getattr(c, name) for c in candidates], dtype=dtype)

    return {
        "tape_names": tape_names,
        "key": np.array(keys, dtype=np.int32),
        "tape_id": np.array([tape_to_id[c.tape] for c in candidates], dtype=np.int16),
        "age_ms": arr("age_ms"),
        "buy1500": arr("buy1500"),
        "buy700": arr("buy700"),
        "uniq1500": arr("uniq1500"),
        "top1500": arr("top1500"),
        "hhi1500": arr("hhi1500"),
        "move700": arr("move700"),
        "move1500": arr("move1500"),
        "off_peak8000": arr("off_peak8000"),
        "sell1500": arr("sell1500"),
        "last_sell_age_ms": arr("last_sell_age_ms"),
        "pnl": arr("pnl_001"),
        "future_max60": arr("future_max60"),
    }


def eval_rule_columns(cols: dict[str, Any], rule: dict[str, float]) -> dict[str, Any]:
    buy1500 = cols["buy1500"]
    mask = (
        (cols["age_ms"] >= rule["age_min"])
        & (cols["age_ms"] <= rule["age_max"])
        & (buy1500 >= rule["buy1500_min"])
        & (buy1500 <= rule["buy1500_max"])
        & (cols["buy700"] >= rule["buy700_min"])
        & (cols["uniq1500"] >= rule["uniq1500_min"])
        & (cols["top1500"] <= rule["top1500_max"])
        & (cols["hhi1500"] <= rule["hhi1500_max"])
        & (cols["move700"] >= rule["move700_min"])
        & (cols["move1500"] >= rule["move1500_min"])
        & (cols["off_peak8000"] >= rule["off_peak_min"])
        & (cols["sell1500"] <= np.maximum(0.010, buy1500 * rule["sell1500_ratio_max"]))
        & (cols["last_sell_age_ms"] >= rule["last_sell_min_age"])
    )
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "avg": 0.0,
            "moon132": 0,
            "moon200": 0,
            "worst_tape": 0.0,
            "by_tape": {},
        }

    # Candidates are sorted chronologically per tape before this is called.
    # Keep the first passing entry per tape+mint.
    seen: set[int] = set()
    keep: list[int] = []
    keys = cols["key"]
    for i in idxs.tolist():
        k = int(keys[i])
        if k in seen:
            continue
        seen.add(k)
        keep.append(i)
    if not keep:
        return {
            "n": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "avg": 0.0,
            "moon132": 0,
            "moon200": 0,
            "worst_tape": 0.0,
            "by_tape": {},
        }
    sel = np.array(keep, dtype=np.int32)
    pnl = cols["pnl"][sel]
    fmax = cols["future_max60"][sel]
    tapes = cols["tape_id"][sel]
    by_tape: dict[str, tuple[int, float]] = {}
    tape_pnls: list[float] = []
    for tid, name in enumerate(cols["tape_names"]):
        tm = tapes == tid
        if not np.any(tm):
            continue
        tape_pnl = float(np.sum(pnl[tm]))
        tape_pnls.append(tape_pnl)
        by_tape[name] = (int(np.sum(tm)), tape_pnl)
    total = float(np.sum(pnl))
    n = int(sel.size)
    return {
        "n": n,
        "wins": int(np.sum(pnl > 0)),
        "losses": int(np.sum(pnl < 0)),
        "pnl": total,
        "avg": total / n if n else 0.0,
        "moon132": int(np.sum(fmax >= 1.32)),
        "moon200": int(np.sum(fmax >= 2.0)),
        "worst_tape": min(tape_pnls) if tape_pnls else 0.0,
        "by_tape": by_tape,
    }


def random_rule(rng: random.Random) -> dict[str, float]:
    age_min = rng.choice([0, 800, 1500, 1800, 2500, 4000, 8000])
    age_max = rng.choice([4500, 6000, 9000, 15000, 30000, 60000, 120000])
    if age_max < age_min:
        age_min, age_max = 0, age_max
    buy_min = rng.choice([0.25, 0.45, 0.75, 1.0, 1.5, 2.0, 3.0])
    buy_max = rng.choice([2.5, 3.5, 5.5, 8.0, 12.0, 25.0, 999.0])
    if buy_max < buy_min:
        buy_max = 999.0
    return {
        "age_min": age_min,
        "age_max": age_max,
        "buy1500_min": buy_min,
        "buy1500_max": buy_max,
        "buy700_min": rng.choice([0.10, 0.25, 0.50, 1.0, 1.5, 2.5]),
        "uniq1500_min": rng.choice([2, 3, 4, 5, 6, 8, 10]),
        "top1500_max": rng.choice([0.25, 0.32, 0.40, 0.50, 0.62, 0.74, 1.01]),
        "hhi1500_max": rng.choice([0.16, 0.22, 0.30, 0.40, 0.55, 1.01]),
        "move700_min": rng.choice([1.0, 1.03, 1.06, 1.10, 1.20, 1.35, 1.55]),
        "move1500_min": rng.choice([1.0, 1.04, 1.08, 1.12, 1.20, 1.35, 1.55]),
        "off_peak_min": rng.choice([0.70, 0.80, 0.88, 0.93, 0.98]),
        "sell1500_ratio_max": rng.choice([0.03, 0.06, 0.10, 0.16, 0.24, 0.35]),
        "last_sell_min_age": rng.choice([0, 250, 600, 1200, 3000, 999999]),
    }


def describe_rule(rule: dict[str, float]) -> str:
    keys = [
        "age_min",
        "age_max",
        "buy1500_min",
        "buy1500_max",
        "buy700_min",
        "uniq1500_min",
        "top1500_max",
        "hhi1500_max",
        "move700_min",
        "move1500_min",
        "sell1500_ratio_max",
        "last_sell_min_age",
    ]
    return " ".join(f"{k}={rule[k]}" for k in keys)


def main() -> None:
    args = parse_args()
    started = time.time()
    candidates: list[Candidate] = []
    for raw in args.raw:
        candidates.extend(load_candidates(raw, args))
    candidates.sort(key=lambda c: (c.tape, c.ts_ms))
    cols = build_columns(candidates)
    print(f"candidate build seconds={time.time() - started:.2f}")
    print(
        "population:",
        f"n={len(candidates):,}",
        f"hit132={sum(1 for c in candidates if c.future_max60 >= 1.32):,}",
        f"hit200={sum(1 for c in candidates if c.future_max60 >= 2.0):,}",
    )

    rng = random.Random(42)
    rules: list[dict[str, float]] = []
    # Seed with the strict late rule tested in PGG2.
    rules.append(
        {
            "age_min": 1800,
            "age_max": 6000,
            "buy1500_min": 1.5,
            "buy1500_max": 5.5,
            "buy700_min": 1.0,
            "uniq1500_min": 6,
            "top1500_max": 0.35,
            "hhi1500_max": 0.22,
            "move700_min": 1.20,
            "move1500_min": 1.35,
            "off_peak_min": 0.70,
            "sell1500_ratio_max": 0.10,
            "last_sell_min_age": 0,
        }
    )
    for _ in range(args.random_rules):
        rules.append(random_rule(rng))

    scored: list[tuple[float, dict[str, Any], dict[str, float]]] = []
    for rule in rules:
        stats = eval_rule_columns(cols, rule)
        if stats["n"] < args.min_trades:
            continue
        if stats["pnl"] <= 0 or stats["worst_tape"] < -0.003:
            continue
        # Prioritize rules that produce real net with low loss count and some moonshot hits.
        score = stats["pnl"] + stats["avg"] * 12.0 + stats["moon132"] * 0.00015 - stats["losses"] * 0.00008
        scored.append((score, stats, rule))
    scored.sort(key=lambda x: x[0], reverse=True)

    print(f"rules_tested={len(rules):,} viable={len(scored):,}")
    for rank, (_score, stats, rule) in enumerate(scored[: args.top], 1):
        print(
            f"\n#{rank} n={stats['n']} W/L={stats['wins']}/{stats['losses']} "
            f"pnl={stats['pnl']:+.6f} avg={stats['avg']:+.6f} "
            f"moon132={stats['moon132']} moon200={stats['moon200']} worst_tape={stats['worst_tape']:+.6f}"
        )
        print("by_tape:", stats["by_tape"])
        print(describe_rule(rule))

    print(f"\ntotal seconds={time.time() - started:.2f}")


if __name__ == "__main__":
    main()
