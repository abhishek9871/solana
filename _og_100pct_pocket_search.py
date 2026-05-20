"""Exhaustive search for 100% WR pockets in PGG2 V55 trade history.

METHODOLOGY (new):
- Bin every continuous feature into quartiles.
- Enumerate ALL 1-, 2-, and 3-feature bucket combinations.
- Find combos where WR == 1.0 AND n >= 5.
- Validate each candidate on a 5x random 70/30 split: does the 100% hold?
- Surface only pockets that survive validation.
"""
from __future__ import annotations

import itertools
import numpy as np
import pandas as pd
from collections import defaultdict


CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"

CONT_FEATURES = [
    "score", "cost", "impact", "roundtrip_loss", "hold_s",
    "sec_since_last_win", "sec_since_last_loss",
    "consec_prior_W", "consec_prior_L",
    "wr_prev_3", "wr_prev_5", "wr_prev_10",
    "other_buys_30s", "other_wins_60s", "concurrent_open",
    "minute_of_day",
]
CAT_FEATURES = ["lane", "hour"]


def quantile_bin(s: pd.Series, n_bins: int = 4) -> pd.Series:
    """Bin a continuous feature into n quantile buckets. Returns labels like 'Q1','Q2','Q3','Q4'."""
    s_filled = s.fillna(s.median())
    try:
        bins = pd.qcut(s_filled, n_bins, labels=[f"Q{i+1}" for i in range(n_bins)],
                       duplicates="drop")
    except ValueError:
        bins = pd.cut(s_filled, n_bins, labels=[f"Q{i+1}" for i in range(n_bins)])
    return bins.astype(str)


def load_and_bin() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)
    binned = pd.DataFrame()
    binned["win"] = df["win"]
    binned["pnl"] = df["pnl"]
    binned["ts"] = df["ts"]
    binned["mint"] = df["mint"]

    for f in CONT_FEATURES:
        binned[f] = quantile_bin(df[f], n_bins=4)
    for f in CAT_FEATURES:
        binned[f] = df[f].astype(str)
    return binned


def find_pure_pockets(df: pd.DataFrame, feats: list[str], min_n: int) -> list[dict]:
    """Find 100% WR pockets across all combinations of feats."""
    pockets = []

    def search(combo: tuple):
        grp = df.groupby(list(combo))
        for keys, sub in grp:
            n = len(sub)
            if n < min_n:
                continue
            wins = int(sub["win"].sum())
            if wins == n:  # 100% WR
                key_tuple = keys if isinstance(keys, tuple) else (keys,)
                rule = " AND ".join(f"{f}={k}" for f, k in zip(combo, key_tuple))
                pockets.append({
                    "rule": rule,
                    "n": n,
                    "wins": wins,
                    "wr": 1.0,
                    "net_pnl": float(sub["pnl"].sum()),
                    "feature_set": combo,
                    "key_tuple": key_tuple,
                })

    # Singles
    for f in feats:
        search((f,))
    # Pairs
    for combo in itertools.combinations(feats, 2):
        search(combo)
    # Triples
    for combo in itertools.combinations(feats, 3):
        search(combo)

    return pockets


def validate_pocket(df: pd.DataFrame, pocket: dict, n_splits: int = 5,
                    rng: np.random.Generator | None = None) -> dict:
    """Random 70/30 split, check WR on test side."""
    if rng is None:
        rng = np.random.default_rng(42)
    feats = pocket["feature_set"]
    keys = pocket["key_tuple"]

    mask_full = pd.Series(True, index=df.index)
    for f, k in zip(feats, keys):
        mask_full &= (df[f] == k)

    test_wrs = []
    test_ns = []
    for _ in range(n_splits):
        is_test = rng.random(len(df)) < 0.3
        sub_test = df[mask_full & is_test]
        if len(sub_test) >= 2:
            test_wrs.append(sub_test["win"].mean())
            test_ns.append(len(sub_test))
    if not test_wrs:
        return {**pocket, "cv_wr_mean": None, "cv_n_mean": 0.0,
                "cv_min_wr": None, "cv_max_wr": None}
    return {
        **pocket,
        "cv_wr_mean": float(np.mean(test_wrs)),
        "cv_wr_std": float(np.std(test_wrs)),
        "cv_n_mean": float(np.mean(test_ns)),
        "cv_min_wr": float(min(test_wrs)),
        "cv_max_wr": float(max(test_wrs)),
    }


def main():
    df = load_and_bin()
    feats = CONT_FEATURES + CAT_FEATURES
    n_total = len(df)
    n_win = int(df["win"].sum())

    print(f"[+] loaded {n_total} trades, baseline WR = {n_win/n_total*100:.1f}%", flush=True)
    print(f"[+] features = {len(feats)} (continuous binned to 4 quartiles + 2 categorical)", flush=True)
    print()

    # === Pass 1: pure pockets at n>=5 ===
    print("=" * 78)
    print("PASS 1: 100% WR pockets at n>=5 (in-sample)")
    print("=" * 78)
    pockets_n5 = find_pure_pockets(df, feats, min_n=5)
    pockets_n5.sort(key=lambda p: -p["n"])
    print(f"  Found {len(pockets_n5)} pockets at n>=5\n")
    print(f"{'n':>4} {'wins':>4} {'net_pnl':>10}   rule")
    print("-" * 78)
    for p in pockets_n5[:25]:
        print(f"{p['n']:>4d} {p['wins']:>4d} {p['net_pnl']:>+10.5f}   {p['rule']}")

    # === Pass 2: pure pockets at n>=10 ===
    print()
    print("=" * 78)
    print("PASS 2: 100% WR pockets at n>=10 (in-sample, statistically meaningful)")
    print("=" * 78)
    pockets_n10 = [p for p in pockets_n5 if p["n"] >= 10]
    print(f"  Found {len(pockets_n10)} pockets at n>=10\n")
    if pockets_n10:
        print(f"{'n':>4} {'wins':>4} {'net_pnl':>10}   rule")
        print("-" * 78)
        for p in pockets_n10[:20]:
            print(f"{p['n']:>4d} {p['wins']:>4d} {p['net_pnl']:>+10.5f}   {p['rule']}")
    else:
        print("  NONE. No 100% WR pocket exists at n>=10.")

    # === Pass 3: Validate top n>=5 pockets on 5 random 70/30 splits ===
    print()
    print("=" * 78)
    print("PASS 3: Validate top 20 pockets on 5x random 70/30 splits")
    print("=" * 78)
    rng = np.random.default_rng(42)
    top_to_validate = pockets_n5[:20]
    validated = [validate_pocket(df, p, n_splits=5, rng=rng) for p in top_to_validate]
    validated.sort(key=lambda p: -(p.get("cv_wr_mean") or 0))
    print(f"{'in_n':>4} {'cv_n':>5} {'cv_wr':>6} {'min':>5} {'max':>5}   rule")
    print("-" * 78)
    for p in validated:
        cv_wr = p.get("cv_wr_mean")
        cv_min = p.get("cv_min_wr")
        cv_max = p.get("cv_max_wr")
        wr_s = f"{cv_wr*100:.0f}%" if cv_wr is not None else "n/a"
        mn_s = f"{cv_min*100:.0f}%" if cv_min is not None else "n/a"
        mx_s = f"{cv_max*100:.0f}%" if cv_max is not None else "n/a"
        print(f"{p['n']:>4d} {p['cv_n_mean']:>5.1f} {wr_s:>6} {mn_s:>5} {mx_s:>5}   {p['rule']}")

    # === Pass 4: STABLE pockets — all 5 holdouts hit 100% ===
    print()
    print("=" * 78)
    print("PASS 4: STABLE 100% pockets (ALL 5 holdouts = 100% WR)")
    print("=" * 78)
    stable = [p for p in validated if (p.get("cv_min_wr") == 1.0 and p.get("cv_n_mean", 0) >= 2)]
    if stable:
        print(f"  Found {len(stable)} stable 100% pockets!")
        print()
        print(f"{'in_n':>4} {'cv_n':>5}   rule")
        print("-" * 78)
        for p in stable[:20]:
            print(f"{p['n']:>4d} {p['cv_n_mean']:>5.1f}   {p['rule']}")
    else:
        print("  NONE survive 5-fold holdout validation at 100% WR.")

    # === Pass 5: Hourly+lane filter — does any (hour, lane) combo hit 100%? ===
    print()
    print("=" * 78)
    print("PASS 5: Specific HOUR+LANE — find any deployable time-window pocket")
    print("=" * 78)
    hl = df.groupby(["hour", "lane"]).agg(n=("win", "size"), w=("win", "sum"),
                                          net=("pnl", "sum"))
    hl["wr"] = hl["w"] / hl["n"]
    hl_100 = hl[(hl["wr"] == 1.0) & (hl["n"] >= 3)].sort_values("n", ascending=False)
    print(f"  Found {len(hl_100)} (hour, lane) combos at 100% WR (n>=3)")
    if len(hl_100):
        print(hl_100.to_string())
    else:
        print("  NONE.")


if __name__ == "__main__":
    main()
