"""Verify the 100% WR pockets using EXACT quartile-label matching.
Then propose deployment thresholds.
"""
from __future__ import annotations
import pandas as pd

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

    pockets = [
        ("P1", (df["score_q"]=="Q2") & (df["cost_q"]=="Q3") & (df["hour_s"]=="22"),
         "score=Q2 AND cost=Q3 AND hour=22"),
        ("P2", (df["cost_q"]=="Q3") & (df["consec_prior_W_q"]=="Q2") & (df["wr_prev_5_q"]=="Q3"),
         "cost=Q3 AND consec_prior_W=Q2 AND wr_prev_5=Q3"),
        ("P3", (df["wr_prev_5_q"]=="Q4") & (df["other_wins_60s_q"]=="Q1") & (df["hour_s"]=="22"),
         "wr_prev_5=Q4 AND other_wins_60s=Q1 AND hour=22"),
    ]

    print("=== INDIVIDUAL POCKET VERIFICATION ===")
    print(f"{'id':<3} {'n':>4} {'W':>3} {'WR':>5} {'net':>10}   rule")
    print("-" * 80)
    masks = []
    for name, mask, rule in pockets:
        sub = df[mask]
        n = len(sub); w = int(sub["win"].sum())
        wr = w/n if n else 0
        net = sub["pnl"].sum()
        print(f"{name:<3} {n:>4d} {w:>3d} {wr*100:>4.0f}% {net:>+10.5f}   {rule}")
        masks.append(mask)

    print()
    print("=== UNION (any-of-3) ===")
    union = masks[0] | masks[1] | masks[2]
    u = df[union]
    print(f"  fires on {len(u)}/{len(df)} = {len(u)/len(df)*100:.1f}% of trades")
    print(f"  WR: {u['win'].mean()*100:.1f}% ({int(u['win'].sum())}/{len(u)})")
    print(f"  net SOL: {u['pnl'].sum():+.5f}")

    # Date span
    df["date"] = pd.to_datetime(df["ts"]).dt.date
    n_days = df["date"].nunique()
    print(f"  date span: {df['date'].min()} to {df['date'].max()} ({n_days} days)")
    print(f"  trades-per-day if deployed: {len(u)/n_days:.2f}")

    print()
    print("=== TIME-FORWARD VALIDATION (split at median date) ===")
    df_sorted = df.sort_values("ts").reset_index(drop=True)
    mid = len(df_sorted) // 2
    train = df_sorted.iloc[:mid]
    test = df_sorted.iloc[mid:]
    print(f"  train: {len(train)} trades  test: {len(test)} trades")

    # Re-bin on train only, then apply train bins to test
    train_bins = {}
    for f in feats_to_bin:
        s = train[f].fillna(train[f].median())
        edges = s.quantile([0, 0.25, 0.5, 0.75, 1.0]).tolist()
        train_bins[f] = edges

    def apply_bin(s, edges):
        labels = []
        for v in s.fillna(s.median()):
            if v <= edges[1]: labels.append("Q1")
            elif v <= edges[2]: labels.append("Q2")
            elif v <= edges[3]: labels.append("Q3")
            else: labels.append("Q4")
        return pd.Series(labels, index=s.index)

    for f in feats_to_bin:
        train[f"{f}_qx"] = apply_bin(train[f], train_bins[f])
        test[f"{f}_qx"] = apply_bin(test[f], train_bins[f])

    print()
    print(f"{'id':<3} {'tr_n':>5} {'tr_W':>5} {'tr_WR':>6}  {'te_n':>5} {'te_W':>5} {'te_WR':>6}   rule")
    print("-" * 95)
    for name, _, rule in pockets:
        if name == "P1":
            tr_m = (train["score_qx"]=="Q2") & (train["cost_qx"]=="Q3") & (train["hour"].astype(str)=="22")
            te_m = (test["score_qx"]=="Q2") & (test["cost_qx"]=="Q3") & (test["hour"].astype(str)=="22")
        elif name == "P2":
            tr_m = (train["cost_qx"]=="Q3") & (train["consec_prior_W_qx"]=="Q2") & (train["wr_prev_5_qx"]=="Q3")
            te_m = (test["cost_qx"]=="Q3") & (test["consec_prior_W_qx"]=="Q2") & (test["wr_prev_5_qx"]=="Q3")
        else:
            tr_m = (train["wr_prev_5_qx"]=="Q4") & (train["other_wins_60s_qx"]=="Q1") & (train["hour"].astype(str)=="22")
            te_m = (test["wr_prev_5_qx"]=="Q4") & (test["other_wins_60s_qx"]=="Q1") & (test["hour"].astype(str)=="22")
        tr_sub = train[tr_m]; te_sub = test[te_m]
        tn, tw = len(tr_sub), int(tr_sub["win"].sum())
        en, ew = len(te_sub), int(te_sub["win"].sum())
        twr = (tw/tn*100) if tn else 0
        ewr = (ew/en*100) if en else 0
        print(f"{name:<3} {tn:>5d} {tw:>5d} {twr:>5.0f}% {en:>5d} {ew:>5d} {ewr:>5.0f}%   {rule}")


if __name__ == "__main__":
    main()
