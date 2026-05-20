"""KEEN-EYE pass: examine features I have NOT deeply analyzed.

UNEXPLORED features:
- chosen (adaptive size classification — what does it mean?)
- impact (in detail, with thresholding)
- roundtrip_loss (pre-trade estimated round-trip cost)
- minute_of_day at FINE granularity (specific minute windows)
- DERIVED ratios: score/cost, impact/cost, etc

For each: find ANY threshold or value that produces 100% WR with deployable n.
"""
from __future__ import annotations
import pandas as pd
import numpy as np

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"

df = pd.read_csv(CSV)
df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)

print(f"=== CSV columns: {list(df.columns)} ===")
print(f"=== {len(df)} trades, baseline WR {df['win'].mean()*100:.1f}% ===\n")

# ============================================================
# 1. The `chosen` field — what IS it?
# ============================================================
print("=" * 78)
print("1. `chosen` field — unique values and WR per value")
print("=" * 78)
print(f"  unique values: {df['chosen'].unique()}")
print(f"  null count: {df['chosen'].isna().sum()}")
print()
cgrp = df.groupby("chosen", dropna=False).agg(n=("win","size"), w=("win","sum"),
                                              pnl_sum=("pnl","sum"))
cgrp["wr"] = cgrp["w"] / cgrp["n"] * 100
cgrp["avg"] = cgrp["pnl_sum"] / cgrp["n"]
cgrp = cgrp.sort_values("wr", ascending=False)
print(f"{'chosen':<25} {'n':>4} {'W':>3} {'WR':>5} {'net':>10} {'avg':>10}")
for v, row in cgrp.iterrows():
    print(f"{str(v)[:25]:<25} {int(row['n']):>4d} {int(row['w']):>3d} "
          f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f} {row['avg']:>+10.5f}")

# ============================================================
# 2. IMPACT — deep distribution analysis
# ============================================================
print()
print("=" * 78)
print("2. IMPACT — distribution + WR by quantile")
print("=" * 78)
imp = df[df["impact"].notna()].copy()
print(f"  impact populated: {len(imp)} of {len(df)} trades")
print(f"  range: {imp['impact'].min():.5f} .. {imp['impact'].max():.5f}")
print(f"  quantiles: {imp['impact'].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()}")

# Threshold sweep: WR if impact <= X
print(f"\n  impact_threshold        n     W   WR   net_pnl")
print("-" * 60)
for thresh in [0.00080, 0.00085, 0.00090, 0.00095, 0.001, 0.0011, 0.0012]:
    sub_le = imp[imp["impact"] <= thresh]
    n = len(sub_le); w = int(sub_le["win"].sum())
    print(f"  impact <= {thresh:.5f}    {n:>4d}  {w:>3d}  {w/n*100 if n else 0:>4.0f}%  "
          f"{sub_le['pnl'].sum():>+10.5f}")
    sub_gt = imp[imp["impact"] > thresh]
    n = len(sub_gt); w = int(sub_gt["win"].sum())
    print(f"  impact >  {thresh:.5f}    {n:>4d}  {w:>3d}  {w/n*100 if n else 0:>4.0f}%  "
          f"{sub_gt['pnl'].sum():>+10.5f}")

# ============================================================
# 3. ROUNDTRIP_LOSS — pre-trade estimate
# ============================================================
print()
print("=" * 78)
print("3. ROUNDTRIP_LOSS — pre-trade estimated round-trip cost")
print("=" * 78)
rl = df[df["roundtrip_loss"].notna()].copy()
print(f"  roundtrip_loss populated: {len(rl)} of {len(df)}")
print(f"  range: {rl['roundtrip_loss'].min():.6f} .. {rl['roundtrip_loss'].max():.6f}")
print(f"  unique values count: {rl['roundtrip_loss'].nunique()}")
print(f"  value-counts (top 10):")
print(rl["roundtrip_loss"].value_counts().head(10))
# WR by exact value
rgrp = rl.groupby("roundtrip_loss").agg(n=("win","size"), w=("win","sum"),
                                        pnl_sum=("pnl","sum"))
rgrp["wr"] = rgrp["w"]/rgrp["n"]*100
print(f"\n{'roundtrip_loss':<18} {'n':>5} {'W':>4} {'WR':>5} {'pnl':>10}")
for v, row in rgrp.sort_values("wr", ascending=False).head(15).iterrows():
    print(f"{v:<18.6f} {int(row['n']):>5d} {int(row['w']):>4d} "
          f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f}")

# ============================================================
# 4. MINUTE_OF_DAY — fine-grained search for 100% WR windows
# ============================================================
print()
print("=" * 78)
print("4. MINUTE_OF_DAY — specific minute windows with 100% WR")
print("=" * 78)
# Aggregate into 5-minute, 10-minute, 30-minute windows
for win_size in [5, 10, 15, 30, 60]:
    df["min_win"] = (df["minute_of_day"] // win_size) * win_size
    grp = df.groupby("min_win").agg(n=("win","size"), w=("win","sum"),
                                    pnl_sum=("pnl","sum"))
    grp["wr"] = grp["w"]/grp["n"]*100
    pure = grp[(grp["wr"] == 100) & (grp["n"] >= 5)]
    high = grp[(grp["wr"] >= 80) & (grp["n"] >= 5)]
    print(f"\n  Window size {win_size:>3} min: {len(pure)} pockets at 100% WR (n>=5), "
          f"{len(high)} at >=80% WR (n>=5)")
    if len(pure) > 0:
        for k, row in pure.head(5).iterrows():
            h, m = int(k) // 60, int(k) % 60
            print(f"    min {int(k):>4} [{h:02d}:{m:02d}+{win_size}min]: "
                  f"n={int(row['n']):>3d} W={int(row['w']):>3d} "
                  f"pnl={row['pnl_sum']:>+8.4f}")

# ============================================================
# 5. DERIVED ratios — interaction features
# ============================================================
print()
print("=" * 78)
print("5. DERIVED features — interaction ratios")
print("=" * 78)
df["score_per_cost"] = df["score"] / df["cost"]
df["impact_per_cost"] = df["impact"] / df["cost"]
df["score_x_hour"] = df["score"] * df["hour"]

for f in ["score_per_cost", "impact_per_cost"]:
    s = df[df[f].notna()][f]
    if len(s) == 0: continue
    print(f"\n  {f} range: {s.min():.2f} .. {s.max():.2f}")
    # Quantile thresholds
    for q in [0.1, 0.25, 0.5, 0.75, 0.9, 0.95]:
        thresh = s.quantile(q)
        sub_lo = df[(df[f].notna()) & (df[f] <= thresh)]
        sub_hi = df[(df[f].notna()) & (df[f] > thresh)]
        wr_lo = sub_lo["win"].mean()*100 if len(sub_lo) else 0
        wr_hi = sub_hi["win"].mean()*100 if len(sub_hi) else 0
        print(f"    q{int(q*100):>2d} ({thresh:>10.3f}): below n={len(sub_lo):>3d} WR={wr_lo:>3.0f}%, "
              f"above n={len(sub_hi):>3d} WR={wr_hi:>3.0f}%")

# ============================================================
# 6. CONCURRENT_OPEN — does the bot do better solo?
# ============================================================
print()
print("=" * 78)
print("6. CONCURRENT_OPEN — does WR change when bot has multiple open positions?")
print("=" * 78)
cgrp = df.groupby("concurrent_open").agg(n=("win","size"), w=("win","sum"),
                                          pnl_sum=("pnl","sum"))
cgrp["wr"] = cgrp["w"]/cgrp["n"]*100
print(f"{'co':>3} {'n':>4} {'W':>3} {'WR':>5} {'net':>10}")
for c, row in cgrp.iterrows():
    print(f"{int(c):>3d} {int(row['n']):>4d} {int(row['w']):>3d} "
          f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f}")
