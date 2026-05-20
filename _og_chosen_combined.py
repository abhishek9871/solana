"""COMBINE `chosen=0.04` signal with other features. Find the high-WR subset.

The adaptive sizer picks 0.04 SOL (premium tier) when it sees a high-quality
setup. These trades alone are +0.202 SOL net at 43% WR. Combined with other
features, what's the achievable WR?
"""
from __future__ import annotations
import pandas as pd
import itertools

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"

df = pd.read_csv(CSV)
df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)
df["is_premium"] = (df["chosen"] == 0.04).astype(int)

premium = df[df["is_premium"] == 1]
print(f"=== PREMIUM TIER (chosen=0.04) ===")
print(f"  n={len(premium)} WR={premium['win'].mean()*100:.1f}% net={premium['pnl'].sum():+.5f}")
print()

# === Combine with score thresholds ===
print("=" * 78)
print("PREMIUM x SCORE THRESHOLD")
print("=" * 78)
for s_min in [260, 270, 280, 285, 290, 295, 300, 310]:
    sub = premium[premium["score"] >= s_min]
    n = len(sub); w = int(sub["win"].sum())
    wr = w/n*100 if n else 0
    print(f"  score >= {s_min}: n={n:>4d}  W={w:>3d}  WR={wr:>3.0f}%  net={sub['pnl'].sum():>+10.5f}")

# === Combine with hour ===
print()
print("=" * 78)
print("PREMIUM x HOUR")
print("=" * 78)
hr = premium.groupby("hour").agg(n=("win","size"), w=("win","sum"),
                                  pnl_sum=("pnl","sum"))
hr["wr"] = hr["w"]/hr["n"]*100
print(f"{'hour':>5} {'n':>4} {'W':>3} {'WR':>5} {'net':>10}")
for h, row in hr.sort_values("wr", ascending=False).iterrows():
    if row["n"] >= 3:
        print(f"{int(h):>5d} {int(row['n']):>4d} {int(row['w']):>3d} "
              f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f}")

# === Combine with impact_per_cost ===
df["impact_per_cost"] = df["impact"] / df["cost"]
premium = df[df["is_premium"] == 1]
print()
print("=" * 78)
print("PREMIUM x IMPACT_PER_COST")
print("=" * 78)
for q in [0.5, 0.4, 0.3, 0.25, 0.2, 0.15, 0.1]:
    thresh = premium["impact_per_cost"].quantile(q)
    sub = premium[premium["impact_per_cost"] <= thresh]
    n = len(sub); w = int(sub["win"].sum())
    print(f"  impact/cost <= q{int(q*100):>2d} ({thresh:.5f}): n={n:>4d} W={w:>3d} "
          f"WR={w/n*100 if n else 0:>3.0f}% net={sub['pnl'].sum():>+10.5f}")

# === FULL combinations: brute-force on premium subset ===
print()
print("=" * 78)
print("PREMIUM + brute force — best combos with n>=20")
print("=" * 78)

# Bin features within premium subset
def qb(s, n_bins=4):
    s_filled = s.fillna(s.median())
    try:
        return pd.qcut(s_filled, n_bins, labels=[f"Q{i+1}" for i in range(n_bins)],
                       duplicates="drop").astype(str)
    except: return pd.cut(s_filled, n_bins, labels=[f"Q{i+1}" for i in range(n_bins)]).astype(str)

cont = ["score", "cost", "impact", "sec_since_last_win", "sec_since_last_loss",
        "wr_prev_5", "consec_prior_L", "other_buys_30s", "minute_of_day"]
prem = premium.copy()
for f in cont:
    prem[f"{f}_q"] = qb(prem[f])
prem["hour_s"] = prem["hour"].astype(str)

bin_feats = [f"{f}_q" for f in cont] + ["hour_s"]
results = []
for f1, f2 in itertools.combinations(bin_feats, 2):
    for v1 in prem[f1].unique():
        for v2 in prem[f2].unique():
            m = (prem[f1]==v1) & (prem[f2]==v2)
            n = int(m.sum())
            if n < 20: continue
            w = int(prem[m]["win"].sum())
            wr = w/n
            if wr >= 0.60:
                net = prem[m]["pnl"].sum()
                results.append((wr, n, w, net, f1, v1, f2, v2))
results.sort(key=lambda r: (-r[0], -r[1]))
print(f"  Found {len(results)} combos within PREMIUM with WR>=60% and n>=20\n")
print(f"{'WR%':>4} {'n':>4} {'W':>3} {'net':>10}   rule")
for wr, n, w, net, f1, v1, f2, v2 in results[:15]:
    print(f"{wr*100:>3.0f}% {n:>4d} {w:>3d} {net:>+10.4f}   PREMIUM + {f1}={v1} AND {f2}={v2}")

# 3-feature search within premium
print()
print("3-FEATURE within PREMIUM, n>=15, WR>=70%:")
results3 = []
for f1, f2, f3 in itertools.combinations(bin_feats, 3):
    grp = prem.groupby([f1, f2, f3])
    for keys, sub in grp:
        n = len(sub)
        if n < 15: continue
        w = int(sub["win"].sum())
        wr = w/n
        if wr >= 0.70:
            results3.append((wr, n, w, sub["pnl"].sum(), f1, f2, f3, keys))
results3.sort(key=lambda r: (-r[0], -r[1]))
print(f"  Found {len(results3)} combos\n")
print(f"{'WR%':>4} {'n':>4} {'W':>3} {'net':>10}   rule")
for wr, n, w, net, f1, f2, f3, keys in results3[:10]:
    print(f"{wr*100:>3.0f}% {n:>4d} {w:>3d} {net:>+10.4f}   PREMIUM + "
          f"{f1}={keys[0]} AND {f2}={keys[1]} AND {f3}={keys[2]}")

# === 100% pockets within PREMIUM ===
print()
print("=" * 78)
print("100% POCKETS WITHIN PREMIUM (n>=5)")
print("=" * 78)
pure = []
for f1, f2 in itertools.combinations(bin_feats, 2):
    for v1 in prem[f1].unique():
        for v2 in prem[f2].unique():
            m = (prem[f1]==v1) & (prem[f2]==v2)
            n = int(m.sum())
            if n < 5: continue
            w = int(prem[m]["win"].sum())
            if w == n:
                pure.append((n, w, prem[m]["pnl"].sum(), f1, v1, f2, v2))
pure.sort(key=lambda r: -r[0])
print(f"  Found {len(pure)} pure 100% pockets within PREMIUM (n>=5)\n")
for n, w, net, f1, v1, f2, v2 in pure[:15]:
    print(f"  n={n:>3d}  W={w:>3d}  WR=100%  net={net:>+8.4f}  "
          f"PREMIUM + {f1}={v1} AND {f2}={v2}")

# 3-feature pure
pure3 = []
for f1, f2, f3 in itertools.combinations(bin_feats, 3):
    grp = prem.groupby([f1, f2, f3])
    for keys, sub in grp:
        n = len(sub)
        if n < 5: continue
        w = int(sub["win"].sum())
        if w == n:
            pure3.append((n, w, sub["pnl"].sum(), f1, f2, f3, keys))
pure3.sort(key=lambda r: -r[0])
print(f"\n  3-feature 100% pockets within PREMIUM (n>=5): {len(pure3)}")
for n, w, net, f1, f2, f3, keys in pure3[:15]:
    print(f"  n={n:>3d}  WR=100%  net={net:>+8.4f}  "
          f"PREMIUM + {f1}={keys[0]} AND {f2}={keys[1]} AND {f3}={keys[2]}")
