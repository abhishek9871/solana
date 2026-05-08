"""Phase 1: Cross-run validation framework for OG bot live runs.

Goals:
1. Walk-forward validation: train filters on May 6 data, test on May 7
2. Per-hour heatmap of bot performance (UTC)
3. Per-lane breakdown across runs
4. Filter generalization score: does each filter survive cross-run testing?
5. Missed-moonshot cost quantification (raw events analysis)
"""
import json, os, glob
from datetime import datetime, timezone
from collections import defaultdict

# Discover all OG runs
RUN_GLOB = sorted(glob.glob('/root/piggy/data/pgg2_direct_live_*_decisions.jsonl'))
RUNS = [os.path.basename(p).replace('_decisions.jsonl', '') for p in RUN_GLOB]
print(f'Discovered {len(RUNS)} OG live runs')
print()

# Pull all trades with full features
all_trades = []
for run in RUNS:
    path = f'/root/piggy/data/{run}_decisions.jsonl'
    sps={}; opens={}; closes={}
    for line in open(path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        k = x.get('kind'); m = x.get('mint')
        if k == 'strike_plan': sps[m] = x
        elif k == 'open': opens[m] = x
        elif k == 'close': closes[m] = x
    for mint, op in opens.items():
        cl = closes.get(mint)
        if not cl: continue
        sp = sps.get(mint, {}); sf = sp.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        b1500 = float(sf.get('buy1500') or 0); b700 = float(sf.get('buy700') or 0)
        u1500 = int(sf.get('uniq1500') or 0); u700 = int(sf.get('uniq700') or 0)
        cluster = float(sf.get('cluster_score') or 0); score = float(sp.get('score') or 0)
        first = float(sf.get('first_buy_sol') or 0)
        em = float(sf.get('priced_snap_entry_move') or 1.0)
        sb = int(sf.get('slot_buyers') or 0)
        slot = float(sf.get('slot_buy_sol') or 0)
        ts = int(op.get('ts_ms') or 0)
        all_trades.append({
            'run': run, 'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'open_ts': ts, 'close_ts': int(cl.get('ts_ms') or 0),
            'utc_hour': datetime.fromtimestamp(ts/1000, tz=timezone.utc).hour if ts else 0,
            'utc_date': datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d') if ts else '',
            'score': score, 'buy700': b700, 'buy1500': b1500, 'uniq700': u700, 'uniq1500': u1500,
            'top700': float(sf.get('top_share700') or 1.0), 'top1500': float(sf.get('top_share1500') or 1.0),
            'hhi700': float(sf.get('buyer_hhi700') or 1.0), 'sell1500': float(sf.get('sell1500') or 0),
            'age_ms': int(sf.get('age_ms') or 0), 'first_buy': first,
            'first_pct': first / max(b1500, 0.001), 'slot_top': float(sf.get('slot_top_share') or 1.0),
            'slot_buyers': sb, 'slot_to_15': slot / max(b1500, 0.001),
            'b7_b15_ratio': b700 / max(b1500, 0.001),
            'move700': float(sf.get('move700') or 1.0), 'move1500': float(sf.get('move1500') or 1.0),
            'cluster_per_buy': cluster / max(b1500, 0.001), 'score_per_buyer': score / max(u1500, 1),
            'avg_buy_size_7': b700 / max(u700, 1), 'avg_buy_size_15': b1500 / max(u1500, 1),
            'entry_move_priced': em,
        })

print(f'Total trades collected: {len(all_trades)}')
print(f'Wins: {sum(1 for t in all_trades if t["is_win"])}, Losses: {sum(1 for t in all_trades if not t["is_win"])}')
print()

# All my 18 deployed filters
FILTERS = {
    'E1': lambda t: t['lane']=='curve_lag_reveal' and t['age_ms']>3000,
    'E2': lambda t: t['lane']=='priced_snap' and t['top700']>0.70 and t['buy700']<3.5 and t['hhi700']<0.99,
    'N1': lambda t: t['hhi700']<0.14 and t['first_buy']<2.5,
    'N2': lambda t: t['lane']=='priced_snap' and t['move700']<1.0,
    'N3': lambda t: t['lane']=='priced_snap' and t['move1500']<1.04,
    'F6': lambda t: t['lane']=='priced_snap' and t['score']>326.9 and t['move700']>1.653,
    'F7': lambda t: t['lane']=='curve_lag_reveal' and t['buy1500']>15.2 and t['first_pct']<0.110,
    'F8': lambda t: t['lane']=='priced_snap' and t['cluster_per_buy']>44.33 and t['top1500']>0.326,
    'F9': lambda t: t['lane']=='priced_snap' and t['score_per_buyer']>39.25,
    'F10': lambda t: t['lane']=='priced_snap' and t['avg_buy_size_7']<0.611 and t['sell1500']>0.094,
    'F11': lambda t: t['lane']=='priced_snap' and t['avg_buy_size_7']<0.564 and t['avg_buy_size_15']<0.670,
    'F12': lambda t: t['lane']=='curve_lag_reveal' and t['slot_top']>0.40 and t['slot_top']<0.99,
    'F13': lambda t: t['lane']=='curve_lag_reveal' and t['score']<209 and t['uniq700']>10,
    'F14': lambda t: t['lane']=='priced_snap' and t['score']<270 and t['b7_b15_ratio']<0.40,
    'F15v2': lambda t: t['lane']=='priced_snap' and t['top700']>0.60 and t['first_buy']<2.0 and t['slot_to_15']<0.20,
    'F16': lambda t: t['lane']=='priced_snap' and t['entry_move_priced']<1.20 and t['slot_buyers']<=1,
    'F17': lambda t: t['lane']=='priced_snap' and t['age_ms']<2800 and t['slot_buyers']<=1,
    'F18': lambda t: t['lane']=='priced_snap' and t['slot_buyers']>=10 and t['slot_buyers']==t['uniq700'] and t['cluster_per_buy']>45.0,
}

# ============================================================================
# 1. PER-HOUR HEATMAP (across all runs, UTC)
# ============================================================================
print('=' * 130)
print('1. PER-HOUR PERFORMANCE HEATMAP (across all 22 runs, UTC)')
print('=' * 130)
hourly = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'best_win': 0.0, 'worst_loss': 0.0})
for t in all_trades:
    h = t['utc_hour']
    hourly[h]['count'] += 1
    hourly[h]['pnl'] += t['pnl']
    if t['is_win']: hourly[h]['wins'] += 1
    else: hourly[h]['losses'] += 1
    if t['pnl'] > hourly[h]['best_win']: hourly[h]['best_win'] = t['pnl']
    if t['pnl'] < hourly[h]['worst_loss']: hourly[h]['worst_loss'] = t['pnl']

print(f'{"hour":>5s}  {"trades":>7s}  {"W/L":>10s}  {"win%":>5s}  {"avg_pnl":>10s}  {"total":>10s}  {"best":>9s}  {"worst":>9s}')
for h in sorted(hourly.keys()):
    d = hourly[h]
    avg_pnl = d['pnl']/max(d['count'], 1)
    win_pct = d['wins']*100//max(d['count'], 1)
    print(f'  {h:02d}:00  {d["count"]:>7d}  {d["wins"]:>3d}/{d["losses"]:<5d}  {win_pct:>4d}%  {avg_pnl:>+10.5f}  {d["pnl"]:>+10.5f}  {d["best_win"]:>+9.4f}  {d["worst_loss"]:>+9.4f}')

# ============================================================================
# 2. PER-RUN BASELINE (compare runs that won vs lost)
# ============================================================================
print()
print('=' * 130)
print('2. PER-RUN PERFORMANCE')
print('=' * 130)
run_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0})
for t in all_trades:
    run_stats[t['run']]['count'] += 1
    run_stats[t['run']]['pnl'] += t['pnl']
    if t['is_win']: run_stats[t['run']]['wins'] += 1
    else: run_stats[t['run']]['losses'] += 1
print(f'{"run":>40s}  {"trades":>7s}  {"W/L":>10s}  {"win%":>5s}  {"net":>10s}')
for run in sorted(run_stats.keys()):
    d = run_stats[run]
    win_pct = d['wins']*100//max(d['count'], 1)
    print(f'  {run:>40s}  {d["count"]:>7d}  {d["wins"]:>3d}/{d["losses"]:<5d}  {win_pct:>4d}%  {d["pnl"]:>+10.5f}')

# ============================================================================
# 3. FILTER GENERALIZATION: per-run impact of each filter
# ============================================================================
print()
print('=' * 130)
print('3. FILTER GENERALIZATION (does each filter help on individual runs?)')
print('=' * 130)
print(f'For each filter, show: across runs how many it helped vs hurt')
print()

filter_run_impact = defaultdict(list)
for fname, pred in FILTERS.items():
    for run in run_stats.keys():
        run_trades = [t for t in all_trades if t['run'] == run]
        if not run_trades: continue
        baseline_pnl = sum(t['pnl'] for t in run_trades)
        kept_pnl = sum(t['pnl'] for t in run_trades if not pred(t))
        delta = kept_pnl - baseline_pnl
        # Did filter help (delta > 0 meaning kept_pnl > baseline_pnl, i.e., losses removed)
        # Did it hurt (delta < 0, blocked winners)
        winners_blocked = [t for t in run_trades if t['is_win'] and pred(t)]
        losses_blocked = [t for t in run_trades if not t['is_win'] and pred(t)]
        filter_run_impact[fname].append({
            'run': run, 'delta': delta,
            'winners_blocked': len(winners_blocked),
            'losses_blocked': len(losses_blocked),
            'winner_loss': sum(t['pnl'] for t in winners_blocked),
            'loss_avoid': -sum(t['pnl'] for t in losses_blocked),
        })

print(f'{"filter":>6s}  {"runs_helped":>11s}  {"runs_neutral":>12s}  {"runs_hurt":>9s}  {"total_delta":>12s}  {"total_W_blocked":>15s}  {"total_L_blocked":>15s}')
for fname, impacts in FILTERS.items():
    impacts_data = filter_run_impact[fname]
    helped = sum(1 for i in impacts_data if i['delta'] > 0.0001)
    hurt = sum(1 for i in impacts_data if i['delta'] < -0.0001)
    neutral = sum(1 for i in impacts_data if abs(i['delta']) <= 0.0001)
    total_delta = sum(i['delta'] for i in impacts_data)
    total_W = sum(i['winners_blocked'] for i in impacts_data)
    total_L = sum(i['losses_blocked'] for i in impacts_data)
    print(f'  {fname:>6s}  {helped:>11d}  {neutral:>12d}  {hurt:>9d}  {total_delta:>+12.5f}  {total_W:>15d}  {total_L:>15d}')

# ============================================================================
# 4. WALK-FORWARD: train on May 6, test on May 7
# ============================================================================
print()
print('=' * 130)
print('4. WALK-FORWARD VALIDATION (train on May 6 (13 runs), test on May 7 (9 runs))')
print('=' * 130)
may6 = [t for t in all_trades if t['utc_date'] == '2026-05-06']
may7 = [t for t in all_trades if t['utc_date'] == '2026-05-07']
may8 = [t for t in all_trades if t['utc_date'] == '2026-05-08']
print(f'May 6: {len(may6)} trades  May 7: {len(may7)} trades  May 8: {len(may8)} trades')

# For each filter, see if "training" on May 6 (i.e. checking if it would have helped May 6)
# AND the same logic applied to May 7 — does it still help on May 7?
print()
print('For each filter, train on May 6+5/7-AM, test on May 7 evening + May 8 (UTC):')
def block_total(pred, dataset):
    base = sum(t['pnl'] for t in dataset)
    kept = sum(t['pnl'] for t in dataset if not pred(t))
    return kept - base, sum(1 for t in dataset if t['is_win'] and pred(t)), sum(1 for t in dataset if not t['is_win'] and pred(t))

train = may6
test = may7 + may8
print(f'{"filter":>6s}  {"train_delta":>12s}  {"test_delta":>12s}  {"train_W_blocked":>15s}  {"test_W_blocked":>14s}  {"generalizes":>12s}')
for fname, pred in FILTERS.items():
    train_delta, train_w, train_l = block_total(pred, train)
    test_delta, test_w, test_l = block_total(pred, test)
    # Generalizes if: train delta and test delta have same sign AND both positive
    gen = '✓ helps both' if train_delta > 0 and test_delta > 0 else ('overfits' if train_delta > 0 and test_delta < 0 else ('hurts both' if train_delta < 0 and test_delta < 0 else 'mixed'))
    print(f'  {fname:>6s}  {train_delta:>+12.5f}  {test_delta:>+12.5f}  {train_w:>15d}  {test_w:>14d}  {gen:>12s}')

# ============================================================================
# 5. CRITICAL: which filters block winners ACROSS RUNS?
# ============================================================================
print()
print('=' * 130)
print('5. WINNERS BLOCKED ACROSS RUNS (filter sacrificed real winners)')
print('=' * 130)
for fname, pred in FILTERS.items():
    winners_blocked = [t for t in all_trades if t['is_win'] and pred(t)]
    if winners_blocked:
        print(f'\n{fname}: {len(winners_blocked)} winners blocked, sacrificed +{sum(t["pnl"] for t in winners_blocked):.5f} SOL')
        for t in sorted(winners_blocked, key=lambda x: -x['pnl'])[:5]:
            ts_s = datetime.fromtimestamp(t['close_ts']/1000, tz=timezone.utc).strftime('%H:%M:%S') if t['close_ts'] else ''
            print(f'  {t["mint"]} ({t["utc_date"]} {ts_s} UTC) pnl=+{t["pnl"]:.5f}')

# ============================================================================
# 6. CONSECUTIVE LOSS PATTERN (drawdown circuit-breaker analysis)
# ============================================================================
print()
print('=' * 130)
print('6. DRAWDOWN PATTERNS — could a circuit breaker have helped?')
print('=' * 130)

# For the 191401 run specifically, find consecutive-loss windows
big_run = [t for t in all_trades if t['run'] == 'pgg2_direct_live_20260507_191401']
big_run.sort(key=lambda t: t['close_ts'])
print(f'Big run 191401: {len(big_run)} trades. Looking for streaks of N consecutive losses:')
streaks = []
cur = 0
cur_pnl = 0.0
for i, t in enumerate(big_run):
    if not t['is_win']:
        cur += 1
        cur_pnl += t['pnl']
        if cur >= 3 and (i+1 == len(big_run) or big_run[i+1]['is_win']):
            streaks.append((cur, cur_pnl, i-cur+1, i))
        elif i+1 == len(big_run):
            streaks.append((cur, cur_pnl, i-cur+1, i))
    else:
        cur = 0
        cur_pnl = 0.0
top_streaks = sorted(streaks, key=lambda s: s[1])[:10]
for n, pnl, start_i, end_i in top_streaks:
    start_t = big_run[start_i]
    end_t = big_run[end_i]
    start_ts = datetime.fromtimestamp(start_t['close_ts']/1000, tz=timezone.utc).strftime('%H:%M')
    end_ts = datetime.fromtimestamp(end_t['close_ts']/1000, tz=timezone.utc).strftime('%H:%M')
    print(f'  {n} consecutive losses from {start_ts} to {end_ts} UTC: {pnl:+.5f} SOL')
