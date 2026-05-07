import collections
import json
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path


RUNID = sys.argv[1] if len(sys.argv) > 1 else "pgg2_direct_live_20260506_214938"
BASE = Path("/root/piggy")
LOG = BASE / "logs" / f"{RUNID}.log"
DEC = BASE / "data" / f"{RUNID}_decisions.jsonl"
STATE = BASE / "data" / f"{RUNID}_state.json"


def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def q(vals, pct):
    vals = sorted(v for v in vals if v is not None and not math.isnan(v))
    if not vals:
        return None
    idx = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * pct))))
    return vals[idx]


def fmt(x, digits=6):
    if x is None:
        return "n/a"
    return f"{x:+.{digits}f}" if isinstance(x, float) else str(x)


def mint_short(m):
    if not m:
        return "?"
    return f"{m[:4]}..{m[-4:]}"


state = json.load(open(STATE))
session = state.get("session", {})
started = session.get("started_at", 0)
updated = state.get("updated_at", 0)
runtime_s = max(0, updated - started)
runtime_h = runtime_s / 3600 if runtime_s else 0.0

rows = []
by_mint = collections.defaultdict(lambda: {"events": []})
counts = collections.Counter()
for line in open(DEC):
    if not line.strip():
        continue
    x = json.loads(line)
    rows.append(x)
    counts[x.get("kind")] += 1
    m = x.get("mint")
    if m:
        by_mint[m]["events"].append(x)
        if x.get("kind") == "open":
            by_mint[m]["open"] = x
        elif x.get("kind") == "strike_plan":
            by_mint[m]["strike"] = x
        elif x.get("kind") == "close":
            by_mint[m]["close"] = x

trades = []
for m, d in by_mint.items():
    op = d.get("open")
    cl = d.get("close")
    if not op or not cl:
        continue
    strike = d.get("strike") or {}
    of = op.get("features") or {}
    cf = cl.get("features") or {}
    pnl = fnum(cl.get("pnl_sol"))
    trades.append(
        {
            "mint": m,
            "short": mint_short(m),
            "lane": op.get("lane") or strike.get("lane") or "?",
            "reason": cl.get("reason"),
            "pnl": pnl,
            "win": pnl > 0,
            "open_ts": op.get("ts_ms", 0),
            "close_ts": cl.get("ts_ms", 0),
            "hold_s": (cl.get("ts_ms", 0) - op.get("ts_ms", 0)) / 1000.0,
            "open_age_ms": of.get("age_ms"),
            "buy700": of.get("buy700"),
            "buy1500": of.get("buy1500"),
            "uniq700": of.get("uniq700"),
            "uniq1500": of.get("uniq1500"),
            "top_share700": of.get("top_share700"),
            "top_share1500": of.get("top_share1500"),
            "move700": of.get("move700"),
            "move1500": of.get("move1500"),
            "entry_move": of.get("priced_snap_entry_move"),
            "sell700": of.get("sell700"),
            "sell1500": of.get("sell1500"),
            "last_sell_age_ms": of.get("last_sell_age_ms"),
            "score": of.get("score"),
            "cluster_score": of.get("cluster_score"),
            "close_mult": cf.get("mult"),
            "peak_mult": cf.get("peak_mult"),
            "vsol_sol": of.get("vsol_sol"),
            "first_buy_sol": of.get("first_buy_sol"),
        }
    )

lane = collections.defaultdict(list)
reason = collections.defaultdict(list)
skips = []
for t in trades:
    lane[t["lane"]].append(t)
    reason[t["reason"]].append(t)

for x in rows:
    if x.get("kind") == "strike_skipped":
        skips.append(x)

line_type = collections.Counter()
buy_logs = {}
sell_logs = {}
quote_buy = collections.Counter()
quote_sell = collections.Counter()
tx_errs = []
sim_fails = []
profit_checks = []
sell_holds = []
observed_pairs = 0
status_lines = 0
timestamps = []

rx_ts = re.compile(r"^\[(.*?)\]")
rx_buy = re.compile(r"PGG2-LIVE-BUY\s+(\S+).*cost=([0-9.]+).*wallet_delta=([-+0-9.]+).*score=([0-9.]+)")
rx_sell = re.compile(r"PGG2-LIVE-SELL\s+(\S+).*reason=(\S+).*proceeds=([0-9.]+).*wallet_delta=([-+0-9.]+).*mult=([0-9.]+).*pnl=([-+0-9.]+).*session=([-+0-9.]+)")
rx_quote_buy = re.compile(r"PGG2-DIRECT-QUOTE BUY\s+(\S+).*in=([0-9.]+).*out=([0-9.]+).*min=([0-9.]+).*fee_bps=([0-9.]+).*fee=([0-9.]+)")
rx_quote_sell = re.compile(r"PGG2-DIRECT-QUOTE SELL\s+(\S+).*out=([0-9.]+).*fee_bps=([0-9.]+).*fee=([0-9.]+)")
rx_profit = re.compile(r"PGG2-LIVE-QUOTE-PROFIT-CHECK\s+(\S+).*mult=([0-9.]+).*peak=([0-9.]+).*quote_out=([0-9.]+).*pnl=([-+0-9.]+).*need=([0-9.]+)")
rx_hold = re.compile(r"PGG2-LIVE-SELL-HOLD\s+(\S+).*reason=(\S+).*quote_out=([0-9.]+).*cost=([0-9.]+).*pnl=([-+0-9.]+).*need=([0-9.]+)")

for line in open(LOG, errors="replace"):
    line = line.rstrip("\n")
    m_ts = rx_ts.search(line)
    if m_ts:
        timestamps.append(m_ts.group(1))
    if "PIGGY-STATUS" in line:
        line_type["PIGGY-STATUS"] += 1
        status_lines += 1
    if "PGG2-DIRECT-OBSERVED-PAIR" in line:
        line_type["PGG2-DIRECT-OBSERVED-PAIR"] += 1
        observed_pairs += 1
    if "PGG2-DIRECT-QUOTE BUY" in line:
        line_type["PGG2-DIRECT-QUOTE BUY"] += 1
        m = rx_quote_buy.search(line)
        if m:
            quote_buy[m.group(1)] += 1
    if "PGG2-DIRECT-QUOTE SELL" in line:
        line_type["PGG2-DIRECT-QUOTE SELL"] += 1
        m = rx_quote_sell.search(line)
        if m:
            quote_sell[m.group(1)] += 1
    if "PGG2-LIVE-BUY " in line:
        line_type["PGG2-LIVE-BUY"] += 1
        m = rx_buy.search(line)
        if m:
            buy_logs[m.group(1)] = {"cost": float(m.group(2)), "wallet_delta": float(m.group(3)), "score": float(m.group(4))}
    if "PGG2-LIVE-SELL " in line:
        line_type["PGG2-LIVE-SELL"] += 1
        m = rx_sell.search(line)
        if m:
            sell_logs[m.group(1)] = {
                "reason": m.group(2),
                "proceeds": float(m.group(3)),
                "wallet_delta": float(m.group(4)),
                "mult": float(m.group(5)),
                "pnl": float(m.group(6)),
                "session": float(m.group(7)),
            }
    if "PGG2-LIVE-TX-ERR" in line or "PGG2-LIVE-BUY-FAIL" in line or "PGG2-LIVE-SELL-FAIL" in line:
        line_type["TX_OR_SEND_ERROR"] += 1
        tx_errs.append(line)
    if "PGG2-LIVE-SIM-FAIL" in line:
        line_type["SIM_FAIL"] += 1
        sim_fails.append(line)
    if "PGG2-LIVE-QUOTE-PROFIT-CHECK" in line:
        line_type["QUOTE_PROFIT_CHECK"] += 1
        m = rx_profit.search(line)
        if m:
            profit_checks.append(
                {
                    "mint": m.group(1),
                    "mult": float(m.group(2)),
                    "peak": float(m.group(3)),
                    "quote_out": float(m.group(4)),
                    "pnl": float(m.group(5)),
                    "need": float(m.group(6)),
                }
            )
    if "PGG2-LIVE-SELL-HOLD" in line:
        line_type["SELL_HOLD"] += 1
        m = rx_hold.search(line)
        if m:
            sell_holds.append(
                {
                    "mint": m.group(1),
                    "reason": m.group(2),
                    "quote_out": float(m.group(3)),
                    "cost": float(m.group(4)),
                    "pnl": float(m.group(5)),
                    "need": float(m.group(6)),
                }
            )

wins = [t for t in trades if t["pnl"] > 0]
losses = [t for t in trades if t["pnl"] <= 0]

print(f"RUN {RUNID}")
print(f"started_utc={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(started))}")
print(f"updated_utc={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(updated))}")
print(f"runtime={int(runtime_s//3600)}h {int((runtime_s%3600)//60)}m {int(runtime_s%60)}s ({runtime_h:.3f}h)")
print(f"mode={'LIVE' if not state.get('paper_trading') else 'PAPER'} open_positions={len(state.get('positions') or {})} pending={len(state.get('pending') or {})}")
print()

print("STATE_TOTALS")
for k in ["creates", "trades", "buys", "sells", "shreds", "curve_updates", "strike_plans", "scouts", "closes", "wins", "losses", "kills", "best_mult", "realized_pnl_sol", "reconnects"]:
    print(f"{k}={session.get(k)}")
print(f"plans_per_hour={session.get('strike_plans', 0)/runtime_h:.2f}")
print(f"closes_per_hour={len(trades)/runtime_h:.2f}")
print(f"wins_per_hour={len(wins)/runtime_h:.2f}")
print(f"losses_per_hour={len(losses)/runtime_h:.2f}")
print(f"pnl_per_hour={sum(t['pnl'] for t in trades)/runtime_h:+.6f}")
print()

print("DECISION_KIND_COUNTS")
for k, v in counts.most_common():
    print(f"{k}={v}")
print()

print("LOG_LINE_COUNTS")
for k, v in line_type.most_common():
    print(f"{k}={v}")
print(f"observed_pairs={observed_pairs}")
print(f"buy_logs={len(buy_logs)} sell_logs={len(sell_logs)} tx_errors={len(tx_errs)} sim_fails={len(sim_fails)}")
print()

print("STRIKE_SKIPS")
skip_counts = collections.Counter(x.get("reason") for x in skips)
for k, v in skip_counts.most_common():
    print(f"{k}={v}")
for x in skips[:20]:
    f = x.get("features") or {}
    print(
        f"{mint_short(x.get('mint')):12s} lane={x.get('lane')} reason={x.get('reason')} "
        f"b1500={f.get('buy1500')} u1500={f.get('uniq1500')} top1500={f.get('top_share1500')} "
        f"score={f.get('score')} entry_probe={f.get('entry_probe_sol')}"
    )
print()

print("LIVE_WALLET_ACCOUNTING")
buy_cost_sum = sum(v["cost"] for v in buy_logs.values())
sell_proceeds_sum = sum(v["proceeds"] for v in sell_logs.values())
buy_quote_inputs = []
for line in open(LOG, errors="replace"):
    m = re.search(r"PGG2-DIRECT-QUOTE BUY\s+(\S+).* route=\S+ in=([0-9.]+) out=.* fee_bps=([0-9.]+) fee=([0-9.]+)", line)
    if m and m.group(1) in buy_logs:
        buy_quote_inputs.append((m.group(1), float(m.group(2)), float(m.group(4))))
input_by_mint = {}
fee_by_mint = {}
for short, inp, fee in buy_quote_inputs:
    input_by_mint[short] = inp
    fee_by_mint[short] = fee
extras = []
inputs = []
for short, b in buy_logs.items():
    inp = input_by_mint.get(short)
    if inp is not None:
        inputs.append(inp)
        extras.append(b["cost"] - inp)
print(f"buy_cost_sum={buy_cost_sum:+.6f} sell_proceeds_sum={sell_proceeds_sum:+.6f} net_wallet_delta={sell_proceeds_sum - buy_cost_sum:+.6f}")
if inputs:
    print(f"trade_input_unique={sorted(set(round(x, 6) for x in inputs))}")
    print(f"buy_extra_over_trade_input median={statistics.median(extras):.6f} avg={statistics.mean(extras):.6f} min={min(extras):.6f} max={max(extras):.6f}")
    print(f"estimated_buy_extra_total={sum(extras):+.6f}")
print()

print("LANE_PNL")
for name, ts in sorted(lane.items(), key=lambda kv: sum(x["pnl"] for x in kv[1]), reverse=True):
    pnl = sum(t["pnl"] for t in ts)
    w = sum(1 for t in ts if t["pnl"] > 0)
    l = len(ts) - w
    avgw = statistics.mean([t["pnl"] for t in ts if t["pnl"] > 0]) if w else 0
    avgl = statistics.mean([t["pnl"] for t in ts if t["pnl"] <= 0]) if l else 0
    print(f"{name:24s} n={len(ts):3d} W/L={w:2d}/{l:2d} pnl={pnl:+.6f} avg_win={avgw:+.6f} avg_loss={avgl:+.6f}")
print()

print("CLOSE_REASON_PNL")
for name, ts in sorted(reason.items(), key=lambda kv: sum(x["pnl"] for x in kv[1]), reverse=True):
    pnl = sum(t["pnl"] for t in ts)
    w = sum(1 for t in ts if t["pnl"] > 0)
    l = len(ts) - w
    print(f"{name:34s} n={len(ts):3d} W/L={w:2d}/{l:2d} pnl={pnl:+.6f} avg={pnl/len(ts):+.6f}")
print()

print("TOP_WINS")
for t in sorted(wins, key=lambda x: x["pnl"], reverse=True)[:15]:
    print(
        f"{t['short']:12s} lane={t['lane']:18s} reason={t['reason']:28s} "
        f"pnl={t['pnl']:+.6f} hold={t['hold_s']:.2f}s peak={t['peak_mult']} close_mult={t['close_mult']} "
        f"b1500={t['buy1500']} u1500={t['uniq1500']} top1500={t['top_share1500']} entry_move={t['entry_move']}"
    )
print()

print("TOP_LOSSES")
for t in sorted(losses, key=lambda x: x["pnl"])[:15]:
    print(
        f"{t['short']:12s} lane={t['lane']:18s} reason={t['reason']:28s} "
        f"pnl={t['pnl']:+.6f} hold={t['hold_s']:.2f}s peak={t['peak_mult']} close_mult={t['close_mult']} "
        f"b1500={t['buy1500']} u1500={t['uniq1500']} top1500={t['top_share1500']} entry_move={t['entry_move']}"
    )
print()

print("FEATURE_MEDIANS")
features = [
    "open_age_ms",
    "hold_s",
    "buy700",
    "buy1500",
    "uniq700",
    "uniq1500",
    "top_share700",
    "top_share1500",
    "move700",
    "move1500",
    "entry_move",
    "sell700",
    "sell1500",
    "last_sell_age_ms",
    "score",
    "cluster_score",
    "vsol_sol",
    "first_buy_sol",
]
for k in features:
    wv = [fnum(t.get(k), math.nan) for t in wins]
    lv = [fnum(t.get(k), math.nan) for t in losses]
    print(f"{k:18s} win_med={q(wv,0.5)} loss_med={q(lv,0.5)} win_p25={q(wv,0.25)} loss_p25={q(lv,0.25)} win_p75={q(wv,0.75)} loss_p75={q(lv,0.75)}")
print()

print("QUOTING_GUARDRAIL")
if profit_checks:
    pos_checks = [p for p in profit_checks if p["pnl"] >= p["need"]]
    neg_checks = [p for p in profit_checks if p["pnl"] < p["need"]]
    print(f"quote_profit_checks={len(profit_checks)} pass={len(pos_checks)} fail_or_wait={len(neg_checks)}")
    print(f"profit_check_pnl_med={q([p['pnl'] for p in profit_checks],0.5)} p75={q([p['pnl'] for p in profit_checks],0.75)} p95={q([p['pnl'] for p in profit_checks],0.95)}")
if sell_holds:
    print(f"sell_holds={len(sell_holds)}")
    print(f"sell_hold_pnl_med={q([h['pnl'] for h in sell_holds],0.5)} min={min(h['pnl'] for h in sell_holds):+.6f} max={max(h['pnl'] for h in sell_holds):+.6f}")
print()

print("ERRORS")
print(f"tx_errors={len(tx_errs)} sim_fails={len(sim_fails)} reconnects={session.get('reconnects')}")
for line in tx_errs[:10]:
    print(line)
for line in sim_fails[:5]:
    print(line)
print()

print("LOSS_ATTACK_SURFACE")
loss_by_reason = sorted(reason.items(), key=lambda kv: sum(x["pnl"] for x in kv[1]))
for name, ts in loss_by_reason[:8]:
    pnl = sum(t["pnl"] for t in ts)
    if pnl >= 0:
        continue
    losses_only = [t for t in ts if t["pnl"] <= 0]
    print(
        f"{name:34s} loss_n={len(losses_only):3d} loss_sum={sum(t['pnl'] for t in losses_only):+.6f} "
        f"median_loss={q([t['pnl'] for t in losses_only],0.5)} median_hold={q([t['hold_s'] for t in losses_only],0.5)} "
        f"median_peak={q([fnum(t['peak_mult'], math.nan) for t in losses_only],0.5)}"
    )
