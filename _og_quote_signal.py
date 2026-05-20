"""Examine the PGG2-DIRECT-QUOTE BUY signal — `out` (tokens per SOL),
`min` (min slippage gate), and `fee` — as pre-buy entry features I haven't used.

The `out` field represents the number of tokens the bot would receive for its
SOL input — a direct measure of bonding curve maturity. Fresher curves give
MORE tokens per SOL. Maybe high-out trades win more often.
"""
from __future__ import annotations
import re
import pandas as pd
from collections import defaultdict

DUMP = r"C:\Users\VASU\AppData\Local\Temp\quote_buy_dump.txt"

QUOTE_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-DIRECT-QUOTE BUY (?P<mint>\S+) "
    r"route=(?P<route>\S+) in=(?P<inv>[\d.]+) out=(?P<out>[\d.]+) "
    r"min=(?P<minv>[\d.]+) fee_bps=(?P<feebps>\d+) fee=(?P<fee>[\d.]+)"
)
BUY_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-BUY (?P<mint>\S+) lane=(?P<lane>\S+) "
    r"cost=(?P<cost>[\d.]+) wallet_delta=(?P<wd>[-+\d.]+) sig=\S+ score=(?P<score>[\d.]+)"
)
SELL_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\] PGG2-LIVE-SELL (?P<mint>\S+) reason=(?P<reason>\S+) "
    r"sig=\S+ proceeds=[-+\d.]+ wallet_delta=[-+\d.]+ mult=[\d.]+ "
    r"pnl=(?P<pnl>[-+\d.]+) session=[-+\d.]+"
)

trades = []  # collect BUY+SELL+latest_quote per mint
quote_cache = {}  # mint -> latest quote dict
open_buys = {}  # mint -> buy dict

with open(DUMP, encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        m = QUOTE_RE.search(line)
        if m:
            d = m.groupdict()
            mint = d["mint"]
            quote_cache[mint] = {
                "in": float(d["inv"]), "out": float(d["out"]),
                "min": float(d["minv"]), "fee": float(d["fee"]),
                "tokens_per_sol": float(d["out"]) / float(d["inv"]) if float(d["inv"]) > 0 else 0,
                "min_ratio": float(d["minv"]) / float(d["out"]) if float(d["out"]) > 0 else 0,
            }
            continue
        m = BUY_RE.search(line)
        if m:
            d = m.groupdict()
            mint = d["mint"]
            q = quote_cache.get(mint, {})
            open_buys[mint] = {
                "ts": d["ts"], "lane": d["lane"], "cost": float(d["cost"]),
                "score": float(d["score"]),
                "tokens_per_sol": q.get("tokens_per_sol"),
                "min_ratio": q.get("min_ratio"),
                "quote_out": q.get("out"),
                "quote_in": q.get("in"),
                "quote_fee": q.get("fee"),
            }
            continue
        m = SELL_RE.search(line)
        if m:
            d = m.groupdict()
            mint = d["mint"]
            buy = open_buys.pop(mint, None)
            if not buy: continue
            pnl = float(d["pnl"])
            trades.append({**buy, "reason": d["reason"], "pnl": pnl,
                           "win": 1 if pnl > 0 else 0})

df = pd.DataFrame(trades)
print(f"[+] {len(df)} trades parsed with quote data")
print(f"[+] {df['tokens_per_sol'].notna().sum()} have quote info")
print(f"[+] baseline WR: {df['win'].mean()*100:.1f}%")
print()

# tokens_per_sol distribution
ts = df[df["tokens_per_sol"].notna()].copy()
print("=" * 78)
print("tokens_per_sol (bonding curve maturity proxy)")
print("=" * 78)
print(f"  range: {ts['tokens_per_sol'].min():.0f} .. {ts['tokens_per_sol'].max():.0f}")
print(f"  quartiles: {ts['tokens_per_sol'].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()}")

# WR by quartile
print()
ts["tps_q"] = pd.qcut(ts["tokens_per_sol"], 4, labels=["Q1", "Q2", "Q3", "Q4"])
grp = ts.groupby("tps_q", observed=True).agg(n=("win","size"), w=("win","sum"),
                                              pnl_sum=("pnl","sum"))
grp["wr"] = grp["w"]/grp["n"]*100
print(f"{'tokens_per_sol Q':<20} {'n':>4} {'W':>3} {'WR':>5} {'net':>10}")
for q, row in grp.iterrows():
    print(f"{str(q):<20} {int(row['n']):>4d} {int(row['w']):>3d} "
          f"{row['wr']:>4.0f}% {row['pnl_sum']:>+10.5f}")

# Now combine tokens_per_sol with other features
print()
print("=" * 78)
print("tokens_per_sol × score combinations — find 100% pockets")
print("=" * 78)
ts["score_q"] = pd.qcut(ts["score"], 4, labels=["Q1","Q2","Q3","Q4"])
ts["cost_q"] = pd.qcut(ts["cost"], 4, labels=["Q1","Q2","Q3","Q4"], duplicates="drop")
ts["ts_dt"] = pd.to_datetime(ts["ts"])
ts["hour"] = ts["ts_dt"].dt.hour

for sq in ["Q1","Q2","Q3","Q4"]:
    for tq in ["Q1","Q2","Q3","Q4"]:
        sub = ts[(ts["score_q"]==sq) & (ts["tps_q"]==tq)]
        n = len(sub); w = int(sub["win"].sum())
        if n >= 10:
            wr = w/n*100
            print(f"  score={sq} tokens_per_sol={tq}: n={n:>3d} W={w:>3d} WR={wr:>3.0f}% "
                  f"net={sub['pnl'].sum():>+8.4f}")

# Cross with lane
print()
print("=" * 78)
print("tokens_per_sol × LANE × hour")
print("=" * 78)
for ln in ts["lane"].unique():
    for tq in ["Q1","Q2","Q3","Q4"]:
        sub = ts[(ts["lane"]==ln) & (ts["tps_q"]==tq)]
        n = len(sub); w = int(sub["win"].sum())
        if n >= 10:
            print(f"  lane={ln} tps={tq}: n={n:>3d} W={w:>3d} WR={w/n*100:>3.0f}% "
                  f"net={sub['pnl'].sum():>+8.4f}")

# Cross with sell_reason — what's the quote signal of the BIG WINNERS?
print()
print("=" * 78)
print("tokens_per_sol distribution of QUOTE_PROFIT_BANK trades vs others")
print("=" * 78)
bank = ts[ts["reason"] == "quote_profit_bank"]
non = ts[ts["reason"] != "quote_profit_bank"]
print(f"  bank: n={len(bank)}, tps mean={bank['tokens_per_sol'].mean():.0f}, "
      f"median={bank['tokens_per_sol'].median():.0f}, "
      f"min={bank['tokens_per_sol'].min():.0f}, max={bank['tokens_per_sol'].max():.0f}")
print(f"  other: n={len(non)}, tps mean={non['tokens_per_sol'].mean():.0f}, "
      f"median={non['tokens_per_sol'].median():.0f}, "
      f"min={non['tokens_per_sol'].min():.0f}, max={non['tokens_per_sol'].max():.0f}")

# Pure 100% pockets at small n
print()
print("=" * 78)
print("100% pockets using quote signals at n>=5")
print("=" * 78)
pure = []
for sq in ["Q1","Q2","Q3","Q4"]:
    for tq in ["Q1","Q2","Q3","Q4"]:
        for cq in ["Q1","Q2","Q3","Q4"]:
            sub = ts[(ts["score_q"]==sq) & (ts["tps_q"]==tq) & (ts["cost_q"]==cq)]
            n = len(sub); w = int(sub["win"].sum())
            if n >= 5 and w == n:
                pure.append((n, w, sub["pnl"].sum(), sq, tq, cq))
pure.sort(key=lambda x: -x[0])
print(f"  found {len(pure)}\n")
for n, w, net, sq, tq, cq in pure[:10]:
    print(f"  n={n} W={w} WR=100% net={net:+.4f}  "
          f"score={sq} tokens_per_sol={tq} cost={cq}")
