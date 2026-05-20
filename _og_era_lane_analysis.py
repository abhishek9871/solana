"""NEW PERSPECTIVE — angles I have NOT analyzed before:
1. TIME-ERA shift: did WR improve as bot evolved? Recent vs old.
2. LANE breakdown: maybe non-priced_snap lanes have higher WR.
3. SELL_REASON fingerprint: which exit reasons are profitable?
4. SIZE-BUCKET analysis: which adaptive-size choices win?
5. REPEAT-MINT: did the same mint get traded multiple times with patterns?
6. POSITIVE-PNL CONCENTRATION: of total profit, what % came from top trades?
"""
from __future__ import annotations
import pandas as pd
import numpy as np

CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"


def main():
    df = pd.read_csv(CSV)
    df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)
    df["ts_dt"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts_dt"].dt.date
    print(f"[+] {len(df)} trades, baseline WR {df['win'].mean()*100:.1f}%, "
          f"net SOL {df['pnl'].sum():+.4f}")
    print()

    # ============================================================
    # 1. TIME-ERA SHIFT — has the bot been improving?
    # ============================================================
    print("=" * 78)
    print("1. TIME-ERA: WR by date — is the bot evolving toward higher WR?")
    print("=" * 78)
    daily = df.groupby("date").agg(n=("win","size"), w=("win","sum"),
                                   net=("pnl","sum"))
    daily["wr"] = daily["w"] / daily["n"] * 100
    print(f"{'date':<12} {'n':>4} {'W':>3} {'WR':>5} {'net SOL':>10}")
    for d, row in daily.iterrows():
        print(f"{d!s:<12} {int(row['n']):>4d} {int(row['w']):>3d} "
              f"{row['wr']:>4.0f}% {row['net']:>+10.5f}")

    # Split: last 2 days vs first 3 days
    df_sorted = df.sort_values("ts_dt")
    cutoff = df_sorted["ts_dt"].quantile(0.6)  # last 40% by time
    old = df_sorted[df_sorted["ts_dt"] < cutoff]
    new = df_sorted[df_sorted["ts_dt"] >= cutoff]
    print()
    print(f"  OLD ERA (< {cutoff}): n={len(old)} WR={old['win'].mean()*100:.1f}% "
          f"net={old['pnl'].sum():+.4f}")
    print(f"  NEW ERA (>= {cutoff}): n={len(new)} WR={new['win'].mean()*100:.1f}% "
          f"net={new['pnl'].sum():+.4f}")

    # ============================================================
    # 2. LANE BREAKDOWN — what's the best lane?
    # ============================================================
    print()
    print("=" * 78)
    print("2. LANE BREAKDOWN")
    print("=" * 78)
    lane = df.groupby("lane").agg(n=("win","size"), w=("win","sum"),
                                  net=("pnl","sum"))
    lane["wr"] = lane["w"] / lane["n"] * 100
    lane = lane.sort_values("wr", ascending=False)
    print(f"{'lane':<25} {'n':>4} {'W':>3} {'WR':>5} {'net SOL':>10} {'avg pnl':>10}")
    for ln, row in lane.iterrows():
        n = int(row['n'])
        w = int(row['w'])
        avg = row['net'] / n if n else 0
        print(f"{ln:<25} {n:>4d} {w:>3d} {row['wr']:>4.0f}% {row['net']:>+10.5f} {avg:>+10.5f}")

    # Lane x recent
    print("\n  -- LANE × RECENT-ERA (post 60th percentile time) --")
    recent_lane = new.groupby("lane").agg(n=("win","size"), w=("win","sum"),
                                          net=("pnl","sum"))
    recent_lane["wr"] = recent_lane["w"] / recent_lane["n"] * 100
    print(f"{'lane':<25} {'n':>4} {'W':>3} {'WR':>5} {'net SOL':>10}")
    for ln, row in recent_lane.iterrows():
        print(f"{ln:<25} {int(row['n']):>4d} {int(row['w']):>3d} "
              f"{row['wr']:>4.0f}% {row['net']:>+10.5f}")

    # ============================================================
    # 3. SELL_REASON BREAKDOWN — what does winning look like?
    # ============================================================
    print()
    print("=" * 78)
    print("3. SELL_REASON FINGERPRINT (sort by % of total positive PnL contributed)")
    print("=" * 78)
    sr = df.groupby("sell_reason").agg(n=("win","size"), w=("win","sum"),
                                       pnl_sum=("pnl","sum"))
    sr["wr"] = sr["w"] / sr["n"] * 100
    sr["avg"] = sr["pnl_sum"] / sr["n"]
    sr = sr.sort_values("pnl_sum", ascending=False)
    print(f"{'reason':<35} {'n':>4} {'WR':>5} {'pnl_sum':>10} {'avg':>10}")
    for r, row in sr.iterrows():
        print(f"{r:<35} {int(row['n']):>4d} {row['wr']:>4.0f}% "
              f"{row['pnl_sum']:>+10.5f} {row['avg']:>+10.5f}")

    # ============================================================
    # 4. SIZE-BUCKET / CHOSEN — adaptive-sizer outcomes
    # ============================================================
    print()
    print("=" * 78)
    print("4. SIZE-BUCKET — what does the adaptive sizer choose, and does it win?")
    print("=" * 78)
    df["cost_bucket"] = pd.cut(df["cost"],
                               bins=[0, 0.0172, 0.020, 0.025, 0.035, 0.045, 0.060],
                               labels=["≤0.0172", "0.017-0.020", "0.020-0.025",
                                       "0.025-0.035", "0.035-0.045", "0.045+"])
    cb = df.groupby("cost_bucket", observed=True).agg(n=("win","size"), w=("win","sum"),
                                                        pnl_sum=("pnl","sum"))
    cb["wr"] = cb["w"] / cb["n"] * 100
    cb["avg"] = cb["pnl_sum"] / cb["n"]
    print(f"{'bucket':<15} {'n':>4} {'W':>3} {'WR':>5} {'net':>10} {'avg':>10}")
    for b, row in cb.iterrows():
        print(f"{b!s:<15} {int(row['n']):>4d} {int(row['w']):>3d} "
              f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f} {row['avg']:>+10.5f}")

    # ============================================================
    # 5. REPEAT MINT — did the bot trade the same mint multiple times?
    # ============================================================
    print()
    print("=" * 78)
    print("5. REPEAT-MINT — same mint traded multiple times?")
    print("=" * 78)
    mc = df.groupby("mint").agg(n=("win","size"), w=("win","sum"),
                                pnl_sum=("pnl","sum"))
    mc["wr"] = mc["w"] / mc["n"] * 100
    repeats = mc[mc["n"] >= 2].sort_values("n", ascending=False)
    print(f"  unique mints traded: {len(mc)}")
    print(f"  mints traded 2+ times: {len(repeats)}")
    if len(repeats):
        print(f"\n  Top repeats:")
        print(f"{'mint':<48} {'n':>4} {'W':>3} {'WR':>5} {'pnl_sum':>10}")
        for m, row in repeats.head(15).iterrows():
            print(f"{m[:48]:<48} {int(row['n']):>4d} {int(row['w']):>3d} "
                  f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f}")
        # WR on repeat trades
        repeat_trades = df[df["mint"].isin(repeats.index)]
        print(f"\n  WR of ALL repeat-mint trades: {repeat_trades['win'].mean()*100:.1f}% "
              f"(n={len(repeat_trades)}, net={repeat_trades['pnl'].sum():+.5f})")
        single_trades = df[~df["mint"].isin(repeats.index)]
        print(f"  WR of single-trade mints: {single_trades['win'].mean()*100:.1f}% "
              f"(n={len(single_trades)}, net={single_trades['pnl'].sum():+.5f})")

    # ============================================================
    # 6. PROFIT CONCENTRATION — Pareto check
    # ============================================================
    print()
    print("=" * 78)
    print("6. PROFIT CONCENTRATION — where does the SOL come from?")
    print("=" * 78)
    winners = df[df["pnl"] > 0].sort_values("pnl", ascending=False)
    total_pos = winners["pnl"].sum()
    print(f"  winners: {len(winners)}, total positive pnl: {total_pos:+.5f}")
    for pct in [1, 5, 10, 25, 50]:
        k = max(1, int(len(winners) * pct / 100))
        top_k_pnl = winners.head(k)["pnl"].sum()
        print(f"  Top {pct:>2}% of winners ({k:>3d} trades): contributed "
              f"{top_k_pnl:+.5f} SOL = {top_k_pnl/total_pos*100:.1f}% of all positive pnl")
    losers = df[df["pnl"] <= 0]
    total_neg = losers["pnl"].sum()
    print(f"\n  losers: {len(losers)}, total negative pnl: {total_neg:+.5f}")
    losers_sorted = losers.sort_values("pnl")  # most negative first
    for pct in [5, 10, 25]:
        k = max(1, int(len(losers_sorted) * pct / 100))
        top_k_loss = losers_sorted.head(k)["pnl"].sum()
        print(f"  Worst {pct:>2}% of losers ({k:>3d} trades): contributed "
              f"{top_k_loss:+.5f} SOL = {top_k_loss/total_neg*100:.1f}% of all losses")


if __name__ == "__main__":
    main()
