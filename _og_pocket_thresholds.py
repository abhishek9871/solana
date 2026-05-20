"""Extract numeric quartile boundaries for the 100% WR pockets found.
Convert Q-labels to deployable env-var thresholds.
"""
from __future__ import annotations
import pandas as pd

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"

df = pd.read_csv(CSV)
df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)

cont_feats = [
    "score", "cost", "impact", "roundtrip_loss",
    "sec_since_last_win", "sec_since_last_loss",
    "consec_prior_W", "consec_prior_L",
    "wr_prev_3", "wr_prev_5", "wr_prev_10",
    "other_buys_30s", "other_wins_60s", "concurrent_open",
    "minute_of_day",
]

print("=== Quartile boundaries (Q1/Q2/Q3/Q4 = quartile ranges) ===")
for f in cont_feats:
    s = df[f].fillna(df[f].median())
    q = s.quantile([0, 0.25, 0.5, 0.75, 1.0])
    print(f"{f:<22} Q1=[{q[0]:.4f}..{q[0.25]:.4f}] Q2=[{q[0.25]:.4f}..{q[0.5]:.4f}] Q3=[{q[0.5]:.4f}..{q[0.75]:.4f}] Q4=[{q[0.75]:.4f}..{q[1.0]:.4f}]")

print()
print("=== TOP POCKETS — converted to numeric thresholds ===")
print()

# Pocket 1: score=Q2 AND cost=Q3 AND hour=22 -- n=7
s_q = df["score"].quantile([0.25, 0.5])
c_q = df["cost"].quantile([0.5, 0.75])
print("POCKET #1 (n=7, all-5-holdouts 100%):")
print(f"  score in [{s_q[0.25]:.1f} .. {s_q[0.5]:.1f}]   (Q2 = mid-range score)")
print(f"  cost  in [{c_q[0.5]:.4f} .. {c_q[0.75]:.4f}]  (Q3 = mid-high cost)")
print(f"  hour = 22 (UTC) = 10pm UTC")
print()

cw_q = df["consec_prior_W"].quantile([0.25, 0.5])
wp5_q = df["wr_prev_5"].quantile([0.5, 0.75])
print("POCKET #2 (n=6):")
print(f"  cost in [{c_q[0.5]:.4f} .. {c_q[0.75]:.4f}]")
print(f"  consec_prior_W in [{cw_q[0.25]:.1f} .. {cw_q[0.5]:.1f}]")
print(f"  wr_prev_5 in [{wp5_q[0.5]:.4f} .. {wp5_q[0.75]:.4f}]")
print()

wp5_q4 = df["wr_prev_5"].quantile([0.75, 1.0])
ow_q1 = df["other_wins_60s"].quantile([0, 0.25])
print("POCKET #3 (n=6):")
print(f"  wr_prev_5 in [{wp5_q4[0.75]:.4f} .. {wp5_q4[1.0]:.4f}]  (top quartile recent WR)")
print(f"  other_wins_60s in [{ow_q1[0]:.0f} .. {ow_q1[0.25]:.0f}]  (lowest quartile - quiet market)")
print(f"  hour = 22 (UTC)")
print()

# UNION: How many historical trades would fire if we OR'd the top 4 pockets?
def pocket1(r):
    return (s_q[0.25] <= r["score"] <= s_q[0.5]
            and c_q[0.5] <= r["cost"] <= c_q[0.75]
            and r["hour"] == 22)

def pocket2(r):
    return (c_q[0.5] <= r["cost"] <= c_q[0.75]
            and cw_q[0.25] <= r["consec_prior_W"] <= cw_q[0.5]
            and wp5_q[0.5] <= r["wr_prev_5"] <= wp5_q[0.75])

def pocket3(r):
    return (wp5_q4[0.75] <= r["wr_prev_5"] <= wp5_q4[1.0]
            and ow_q1[0] <= r["other_wins_60s"] <= ow_q1[0.25]
            and r["hour"] == 22)

df["fires"] = df.apply(lambda r: pocket1(r) or pocket2(r) or pocket3(r), axis=1)
fires = df[df["fires"]]
print("=== UNION OF TOP 3 POCKETS (fire-if-any) ===")
print(f"  trades that fire: {len(fires)} / {len(df)} = {len(fires)/len(df)*100:.1f}%")
print(f"  wins: {int(fires['win'].sum())}")
print(f"  WR: {fires['win'].mean()*100:.1f}%")
print(f"  net SOL: {fires['pnl'].sum():+.5f}")
print(f"  avg pnl: {fires['pnl'].mean():+.5f}")
