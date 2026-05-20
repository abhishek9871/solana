"""Find the FREQUENCY vs WR Pareto frontier.

Three new methodologies:
1. GRADIENT EXPANSION: relax 100% pockets one constraint at a time, track WR fall-off
2. 2-FEATURE WR SWEEP: enumerate all 2-feature combos, surface WR>=70% with n>=30
3. ANTI-FILTER: find pure-loser clusters (WR <= 10%), excluding them boosts global WR

Goal: max(WR * trades_per_day) — the actual profit-rate function.
"""
from __future__ import annotations
import itertools
import pandas as pd
import numpy as np
from collections import defaultdict

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"


def quantile_bin(s: pd.Series, n_bins: int = 4) -> pd.Series:
    s_filled = s.fillna(s.median())
    try:
        bins = pd.qcut(s_filled, n_bins, labels=[f"Q{i+1}" for i in range(n_bins)],
                       duplicates="drop")
    except ValueError:
        bins = pd.cut(s_filled, n_bins, labels=[f"Q{i+1}" for i in range(n_bins)])
    return bins.astype(str)


def main():
    df = pd.read_csv(CSV)
    df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)
    df["date"] = pd.to_datetime(df["ts"]).dt.date
    n_days = df["date"].nunique()
    print(f"[+] {len(df)} trades over {n_days} days; baseline WR {df['win'].mean()*100:.1f}%, "
          f"avg {len(df)/n_days:.1f} trades/day")
    print()

    feats_to_bin = [
        "score", "cost", "impact", "roundtrip_loss",
        "sec_since_last_win", "sec_since_last_loss",
        "consec_prior_W", "consec_prior_L",
        "wr_prev_3", "wr_prev_5", "wr_prev_10",
        "other_buys_30s", "other_wins_60s", "concurrent_open",
        "minute_of_day",
    ]
    for f in feats_to_bin:
        df[f"{f}_q"] = quantile_bin(df[f], 4)
    df["hour_s"] = df["hour"].astype(str)
    df["lane_s"] = df["lane"].astype(str)

    bin_feats = [f"{f}_q" for f in feats_to_bin] + ["hour_s", "lane_s"]

    # ============================================================
    # METHODOLOGY 1: GRADIENT EXPANSION of 100% pockets
    # ============================================================
    print("=" * 78)
    print("M1: GRADIENT EXPANSION — relax 100% pockets one constraint at a time")
    print("=" * 78)
    # P1: score=Q2 AND cost=Q3 AND hour=22 (n=7, 100%)
    p1 = {"score_q":"Q2", "cost_q":"Q3", "hour_s":"22"}
    # P3: wr_prev_5=Q4 AND other_wins_60s=Q1 AND hour=22 (n=6, 100%)
    p3 = {"wr_prev_5_q":"Q4", "other_wins_60s_q":"Q1", "hour_s":"22"}

    def evaluate(constraints: dict) -> tuple:
        mask = pd.Series(True, index=df.index)
        for k, v in constraints.items():
            mask &= (df[k] == v)
        sub = df[mask]
        n = len(sub); w = int(sub["win"].sum())
        wr = w/n if n else 0
        return n, w, wr, sub["pnl"].sum()

    for label, pocket in [("P1", p1), ("P3", p3)]:
        print(f"\n  -- pocket {label}: {pocket}")
        n, w, wr, net = evaluate(pocket)
        print(f"     FULL: n={n} W={w} WR={wr*100:.0f}% net={net:+.4f}")
        # Drop each constraint
        keys = list(pocket.keys())
        for drop in keys:
            partial = {k: v for k, v in pocket.items() if k != drop}
            n, w, wr, net = evaluate(partial)
            print(f"     drop[{drop}]: n={n:>4d} W={w:>3d} WR={wr*100:>3.0f}% net={net:+.4f}  ({partial})")
        # Drop 2 (keep 1)
        for keep in keys:
            partial = {keep: pocket[keep]}
            n, w, wr, net = evaluate(partial)
            print(f"     only[{keep}]: n={n:>4d} W={w:>3d} WR={wr*100:>3.0f}% net={net:+.4f}")

    # ============================================================
    # METHODOLOGY 2: 2-FEATURE WR SWEEP at n >= 30 (deployable freq)
    # ============================================================
    print()
    print("=" * 78)
    print("M2: 2-FEATURE COMBOS at n>=30 with WR>=60% (deployable frequency)")
    print("=" * 78)
    results = []
    for f1, f2 in itertools.combinations(bin_feats, 2):
        for v1 in df[f1].unique():
            for v2 in df[f2].unique():
                m = (df[f1]==v1) & (df[f2]==v2)
                n = int(m.sum())
                if n < 30: continue
                w = int(df[m]["win"].sum())
                wr = w/n
                if wr >= 0.60:
                    net = df[m]["pnl"].sum()
                    results.append((wr, n, w, net, f1, v1, f2, v2))
    results.sort(key=lambda r: (-r[0], -r[1]))
    print(f"  Found {len(results)} combos\n")
    print(f"{'WR%':>4} {'n':>4} {'W':>3} {'net':>10}   rule")
    print("-" * 78)
    for wr, n, w, net, f1, v1, f2, v2 in results[:20]:
        print(f"{wr*100:>3.0f}% {n:>4d} {w:>3d} {net:>+10.4f}   {f1}={v1} AND {f2}={v2}")

    # ============================================================
    # METHODOLOGY 3: 3-FEATURE COMBOS at n >= 20 with WR >= 70%
    # ============================================================
    print()
    print("=" * 78)
    print("M3: 3-FEATURE COMBOS at n>=20 with WR>=70%")
    print("=" * 78)
    results3 = []
    for f1, f2, f3 in itertools.combinations(bin_feats, 3):
        # group by all 3 features
        grp = df.groupby([f1, f2, f3])
        for keys, sub in grp:
            n = len(sub)
            if n < 20: continue
            w = int(sub["win"].sum())
            wr = w/n
            if wr >= 0.70:
                results3.append((wr, n, w, sub["pnl"].sum(), f1, f2, f3, keys))
    results3.sort(key=lambda r: (-r[0], -r[1]))
    print(f"  Found {len(results3)} combos\n")
    print(f"{'WR%':>4} {'n':>4} {'W':>3} {'net':>10}   rule")
    print("-" * 78)
    for wr, n, w, net, f1, f2, f3, keys in results3[:15]:
        print(f"{wr*100:>3.0f}% {n:>4d} {w:>3d} {net:>+10.4f}   {f1}={keys[0]} AND {f2}={keys[1]} AND {f3}={keys[2]}")

    # ============================================================
    # METHODOLOGY 4: ANTI-FILTER — find pure-loser clusters
    # ============================================================
    print()
    print("=" * 78)
    print("M4: ANTI-FILTER — find pure-LOSER pockets (WR <= 5%) at n>=15")
    print("=" * 78)
    anti = []
    for f1, f2 in itertools.combinations(bin_feats, 2):
        for v1 in df[f1].unique():
            for v2 in df[f2].unique():
                m = (df[f1]==v1) & (df[f2]==v2)
                n = int(m.sum())
                if n < 15: continue
                w = int(df[m]["win"].sum())
                wr = w/n
                if wr <= 0.05:
                    net = df[m]["pnl"].sum()
                    anti.append((wr, n, w, net, f1, v1, f2, v2))
    anti.sort(key=lambda r: (r[0], -r[1]))
    print(f"  Found {len(anti)} pure-loser pockets\n")
    print(f"{'WR%':>4} {'n':>4} {'W':>3} {'net':>10}   rule")
    print("-" * 78)
    for wr, n, w, net, f1, v1, f2, v2 in anti[:15]:
        print(f"{wr*100:>3.0f}% {n:>4d} {w:>3d} {net:>+10.4f}   EXCLUDE {f1}={v1} AND {f2}={v2}")

    # Compute combined effect: exclude TOP anti-filters, what's the residual WR?
    if anti:
        residual_mask = pd.Series(True, index=df.index)
        excluded = 0
        for wr, n, w, net, f1, v1, f2, v2 in anti[:5]:
            m_excl = (df[f1]==v1) & (df[f2]==v2)
            residual_mask &= ~m_excl
            excluded += int(m_excl.sum())
        residual = df[residual_mask]
        print(f"\n  EXCLUDE top 5 anti-filters: {excluded} trades removed")
        print(f"  Residual: n={len(residual)} W={int(residual['win'].sum())} "
              f"WR={residual['win'].mean()*100:.1f}% net={residual['pnl'].sum():+.4f}")
        print(f"  trades-per-day: {len(residual)/n_days:.2f}")

    # ============================================================
    # METHODOLOGY 5: Sequential — conditional on prior-trade outcome
    # ============================================================
    print()
    print("=" * 78)
    print("M5: SEQUENTIAL — WR conditional on regime states")
    print("=" * 78)
    cuts = [
        ("wr_prev_5 >= 0.6 (hot streak)", df["wr_prev_5"] >= 0.6),
        ("wr_prev_5 >= 0.8 (very hot)", df["wr_prev_5"] >= 0.8),
        ("consec_prior_W >= 2 (2+ wins in a row)", df["consec_prior_W"] >= 2),
        ("consec_prior_W >= 3", df["consec_prior_W"] >= 3),
        ("consec_prior_L == 0 (no recent losses)", df["consec_prior_L"] == 0),
        ("sec_since_last_loss > 600 (10min since loss)", df["sec_since_last_loss"] > 600),
        ("sec_since_last_loss > 1800 (30min since loss)", df["sec_since_last_loss"] > 1800),
        ("sec_since_last_loss > 3600 (1hr since loss)", df["sec_since_last_loss"] > 3600),
    ]
    print(f"{'rule':<55} {'n':>4} {'W':>3} {'WR':>5} {'net':>10} {'tpd':>6}")
    print("-" * 90)
    for label, mask in cuts:
        sub = df[mask]
        n = len(sub); w = int(sub["win"].sum())
        wr = w/n*100 if n else 0
        net = sub["pnl"].sum()
        tpd = n / n_days
        print(f"{label:<55} {n:>4d} {w:>3d} {wr:>4.0f}% {net:>+10.4f} {tpd:>5.1f}")


if __name__ == "__main__":
    main()
