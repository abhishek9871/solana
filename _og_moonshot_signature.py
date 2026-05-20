"""Find the entry-time signature of MOONSHOT trades.

These 7 trades hit 100% WR with average +$3/trade. If they have a clean
entry fingerprint, that fingerprint = the high-EV signal.
"""
from __future__ import annotations
import pandas as pd

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"

df = pd.read_csv(CSV)
df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)

moon = df[df["sell_reason"] == "moonshot_pop_after_sell"]
bank = df[df["sell_reason"] == "quote_profit_bank"]
big_winners = pd.concat([moon, bank])  # 52 trades, 49 wins
non_big = df[~df.index.isin(big_winners.index)]

print(f"=== BIG WINNERS (quote_profit_bank + moonshot_pop_after_sell): n={len(big_winners)} ===")
print(f"    WR: {big_winners['win'].mean()*100:.1f}%")
print(f"    Total pnl: {big_winners['pnl'].sum():+.5f} SOL")
print()

feats = ["score", "cost", "impact", "roundtrip_loss",
         "sec_since_last_win", "sec_since_last_loss",
         "consec_prior_W", "consec_prior_L",
         "wr_prev_3", "wr_prev_5", "wr_prev_10",
         "other_buys_30s", "other_wins_60s", "concurrent_open"]

print(f"{'feature':<22} {'BIG_W mean':>12} {'others mean':>12} {'big_W min':>10} {'big_W max':>10}")
print("-" * 78)
for f in feats:
    bv = big_winners[f].dropna()
    ov = non_big[f].dropna()
    if len(bv) == 0 or len(ov) == 0: continue
    print(f"{f:<22} {bv.mean():>12.4f} {ov.mean():>12.4f} {bv.min():>10.4f} {bv.max():>10.4f}")

# Look at MOONSHOTS specifically (n=7, 100% WR, biggest avg pnl)
print()
print(f"=== MOONSHOT_POP_AFTER_SELL — individual trades (n=7) ===")
moon_cols = ["ts", "mint", "lane", "cost", "score", "impact",
             "consec_prior_W", "consec_prior_L", "wr_prev_5",
             "hour", "minute_of_day", "pnl"]
for c in moon_cols:
    if c not in moon.columns: moon_cols.remove(c)
print(moon[moon_cols].to_string())

# Look at hour pattern of big_winners
print()
print("=== HOUR PATTERN of big winners ===")
hp = big_winners.groupby("hour").size().rename("big_winners")
op = non_big.groupby("hour").size().rename("others")
hr = pd.concat([hp, op], axis=1).fillna(0).astype(int)
hr["big_pct"] = hr["big_winners"] / (hr["big_winners"] + hr["others"]) * 100
hr_sorted = hr[hr["big_winners"] > 0].sort_values("big_pct", ascending=False)
print(hr_sorted.to_string())

# Mints traded multiple times — were the winners DIFFERENT mints?
print()
print("=== UNIQUE MINTS in big_winners vs others ===")
big_mints = set(big_winners["mint"])
non_big_mints = set(non_big["mint"])
print(f"  big_winners unique mints: {len(big_mints)}")
print(f"  non_big unique mints: {len(non_big_mints)}")
print(f"  overlap: {len(big_mints & non_big_mints)}")
print(f"  big_winners ONLY (mints that always won): {len(big_mints - non_big_mints)}")

# Score+cost+hour combos with big_winners
print()
print("=== DETECTABLE entry-time score range for BIG_WINNERS ===")
print(f"  Score range:  {big_winners['score'].min():.1f} .. {big_winners['score'].max():.1f}")
print(f"  Score median: {big_winners['score'].median():.1f}")
print(f"  Cost range:   {big_winners['cost'].min():.4f} .. {big_winners['cost'].max():.4f}")
print(f"  Hours present: {sorted(big_winners['hour'].unique().tolist())}")

# What % of trades in score range [median ±1 stddev] are big winners?
ms = big_winners["score"].median()
ss = big_winners["score"].std()
band_lo, band_hi = ms - ss, ms + ss
in_band = df[(df["score"] >= band_lo) & (df["score"] <= band_hi)]
big_in_band = big_winners[(big_winners["score"] >= band_lo) & (big_winners["score"] <= band_hi)]
print(f"\n  Score band [{band_lo:.1f}..{band_hi:.1f}]: {len(in_band)} trades, "
      f"{len(big_in_band)} are big_winners = {len(big_in_band)/len(in_band)*100:.1f}% concentration")
