"""Combine ALL 100%-WR pockets discovered across analyses into a single
union filter. If the bot fires when ANY pocket matches, what's the combined
coverage, frequency, and historical WR?
"""
from __future__ import annotations
import pandas as pd

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"


def qb(s, n=4):
    s_filled = s.fillna(s.median())
    try:
        return pd.qcut(s_filled, n, labels=[f"Q{i+1}" for i in range(n)],
                       duplicates="drop").astype(str)
    except: return pd.cut(s_filled, n, labels=[f"Q{i+1}" for i in range(n)]).astype(str)


df = pd.read_csv(CSV)
df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)
df["is_premium"] = (df["chosen"] == 0.04).astype(int)
df["ts_dt"] = pd.to_datetime(df["ts"])
df["date"] = df["ts_dt"].dt.date

# Bin features used in pockets
for f in ["score", "cost", "consec_prior_W", "wr_prev_5",
          "other_wins_60s", "sec_since_last_loss", "wr_prev_10", "minute_of_day"]:
    df[f"{f}_q"] = qb(df[f])
df["hour_s"] = df["hour"].astype(str)

# ALL 100% pockets from prior analyses (validated, n>=5)
pockets = [
    ("P1: score=Q2 AND cost=Q3 AND hour=22",
     (df["score_q"]=="Q2") & (df["cost_q"]=="Q3") & (df["hour_s"]=="22")),
    ("P2: cost=Q3 AND consec_prior_W=Q2 AND wr_prev_5=Q3",
     (df["cost_q"]=="Q3") & (df["consec_prior_W_q"]=="Q2") & (df["wr_prev_5_q"]=="Q3")),
    ("P3: wr_prev_5=Q4 AND other_wins_60s=Q1 AND hour=22",
     (df["wr_prev_5_q"]=="Q4") & (df["other_wins_60s_q"]=="Q1") & (df["hour_s"]=="22")),
    ("P4: score=Q2 AND hold_s=Q4 AND wr_prev_5=Q3",
     None),  # hold_s is target-leakage, skip
    ("P5: cost=Q1 AND hold_s=Q4 AND hour=2", None),  # leakage
    ("P6: impact=Q2 AND hold_s=Q4 AND hour=2", None),  # leakage
    ("P7: hold_s=Q4 AND other_buys_30s=Q1 AND hour=17", None),  # leakage
    ("P8: sec_since_last_loss=Q4 AND wr_prev_10=Q3 AND minute_of_day=Q2",
     (df["sec_since_last_loss_q"]=="Q4") & (df["wr_prev_10_q"]=="Q3") & (df["minute_of_day_q"]=="Q2")),
    ("P9: PREMIUM + score=Q1 + hour=22",
     (df["is_premium"]==1) & (df["score_q"]=="Q1") & (df["hour_s"]=="22")),
    ("P10: PREMIUM + score=Q1 + cost=Q4 + hour=22",
     (df["is_premium"]==1) & (df["score_q"]=="Q1") & (df["cost_q"]=="Q4") & (df["hour_s"]=="22")),
]

print(f"=== Individual pocket verification ===")
print(f"{'pocket':<60} {'n':>4} {'W':>3} {'WR':>5} {'net':>10}")
print("-" * 95)
valid_pockets = []
for name, mask in pockets:
    if mask is None:
        print(f"{name:<60}  SKIPPED (target leakage via hold_s)")
        continue
    sub = df[mask]
    n = len(sub); w = int(sub["win"].sum())
    wr = w/n*100 if n else 0
    print(f"{name:<60} {n:>4d} {w:>3d} {wr:>4.0f}% {sub['pnl'].sum():>+10.5f}")
    if n >= 5 and w == n:  # only include verified 100% pockets
        valid_pockets.append((name, mask))

# UNION
print()
print(f"=== UNION of {len(valid_pockets)} valid 100% pockets ===")
union = pd.Series(False, index=df.index)
for name, mask in valid_pockets:
    union |= mask
u = df[union]
n_days = df["date"].nunique()
print(f"  Fires on {len(u)}/{len(df)} = {len(u)/len(df)*100:.2f}% of historical trades")
print(f"  Wins: {int(u['win'].sum())} / Losses: {int(len(u) - u['win'].sum())}")
print(f"  WR: {u['win'].mean()*100:.1f}%")
print(f"  Net SOL: {u['pnl'].sum():+.5f}")
print(f"  Trades-per-day: {len(u)/n_days:.2f}")

# Cross-validate union on 5x random 70/30 splits
print()
print("=== Validate UNION on 5x random 70/30 splits ===")
import numpy as np
rng = np.random.default_rng(42)
for i in range(5):
    test_mask = rng.random(len(df)) < 0.3
    sub = df[union & test_mask]
    if len(sub) > 0:
        print(f"  fold {i+1}: n_test={len(sub):>3d}  W={int(sub['win'].sum()):>3d}  "
              f"WR={sub['win'].mean()*100:.0f}%  net={sub['pnl'].sum():+.5f}")
    else:
        print(f"  fold {i+1}: 0 trades match in test split")

# TIME-FORWARD split
print()
print("=== TIME-FORWARD split ===")
df_sorted = df.sort_values("ts_dt")
mid = len(df_sorted) // 2
train_idx = df_sorted.index[:mid]
test_idx = df_sorted.index[mid:]

train_u = union & df.index.isin(train_idx)
test_u = union & df.index.isin(test_idx)

train_sub = df[train_u]
test_sub = df[test_u]
print(f"  train union: n={len(train_sub)}  W={int(train_sub['win'].sum())}  "
      f"WR={train_sub['win'].mean()*100 if len(train_sub) else 0:.0f}%  "
      f"net={train_sub['pnl'].sum():+.5f}")
print(f"  test  union: n={len(test_sub)}  W={int(test_sub['win'].sum())}  "
      f"WR={test_sub['win'].mean()*100 if len(test_sub) else 0:.0f}%  "
      f"net={test_sub['pnl'].sum():+.5f}")

# Per-pocket detailed breakdown
print()
print("=== Per-pocket contribution ===")
for name, mask in valid_pockets:
    sub = df[mask]
    print(f"  {name}: contributes {len(sub)} trades, {int(sub['win'].sum())} wins")
