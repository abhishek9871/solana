"""Phase 2B: What if we'd done X differently?
For every LOSING trade, reconstruct the token's full price trajectory and answer:
1. Held-to-peak: what was the peak mult BEFORE we exited?  Did we sell too early?
2. Held-to-end: what was the peak mult AFTER we exited? Did the token recover?
3. Sandwich detection: did our buy itself trigger a dump (we paid the top)?
4. Stake sizing: what would different stake sizes have netted?
5. Per-lane economics
6. Buyer wallet reuse (any "smart money" patterns?)
"""
import json, os, glob
from collections import defaultdict
from datetime import datetime, timezone

RUN_GLOB = sorted(glob.glob('/root/piggy/data/pgg2_direct_live_*_decisions.jsonl'))
RUNS = [os.path.basename(p).replace('_decisions.jsonl', '') for p in RUN_GLOB]


def load_run(run):
    """Returns (trades, raw_events_by_mint)."""
    dec_path = f'/root/piggy/data/{run}_decisions.jsonl'
    raw_path = f'/root/piggy/data/{run}_raw.jsonl'
    if not (os.path.exists(dec_path) and os.path.exists(raw_path)):
        return [], {}

    sps={}; opens={}; closes={}
    for line in open(dec_path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        k = x.get('kind'); m = x.get('mint')
        if k == 'strike_plan': sps[m] = x
        elif k == 'open': opens[m] = x
        elif k == 'close': closes[m] = x

    trades = []
    for mint, op in opens.items():
        cl = closes.get(mint)
        if not cl: continue
        sp = sps.get(mint, {}); sf = sp.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        trades.append({
            'run': run,
            'mint': mint,
            'mint_short': mint[:8],
            'open_ts': int(op.get('ts_ms') or 0),
            'close_ts': int(cl.get('ts_ms') or 0),
            'lane': sp.get('lane') or '',
            'pnl': pnl,
            'is_win': pnl > 0,
            'reason_close': cl.get('reason') or '',
            'entry_price': float(sf.get('price') or 0),
            'entry_score': float(sp.get('score') or 0),
            'entry_buy1500': float(sf.get('buy1500') or 0),
            'entry_top700': float(sf.get('top_share700') or 1.0),
            'entry_first_buy': float(sf.get('first_buy_sol') or 0),
            'entry_age_ms': int(sf.get('age_ms') or 0),
            'entry_em': float(sf.get('priced_snap_entry_move') or 1.0),
            'entry_slot_buyers': int(sf.get('slot_buyers') or 0),
        })

    # Raw events for each mint we traded
    traded_mints = {t['mint'] for t in trades}
    events_by_mint = defaultdict(list)
    for line in open(raw_path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        m = x.get('mint')
        if m not in traded_mints: continue
        if x.get('kind') != 'trade': continue
        events_by_mint[m].append({
            'ts': int(x.get('ts_ms') or 0),
            'side': x.get('side'),
            'sol': float(x.get('sol') or 0),
            'price': float(x.get('curve_price') or 0),
            'vsol': float(x.get('vsol_sol') or 0),
            'user': x.get('user') or '',
        })

    for m in events_by_mint:
        events_by_mint[m].sort(key=lambda e: e['ts'])

    return trades, events_by_mint


# Process all runs
all_trades = []
all_events = {}
for run in RUNS:
    trades, events = load_run(run)
    all_trades.extend(trades)
    for m, evs in events.items():
        all_events[(run, m)] = evs

print(f'Processed {len(RUNS)} runs, {len(all_trades)} trades.')
print()

# For each trade compute peak-during-hold and peak-after-exit
analysis_results = []
for t in all_trades:
    key = (t['run'], t['mint'])
    events = all_events.get(key, [])
    if not events or t['entry_price'] <= 0:
        continue
    open_ts = t['open_ts']
    close_ts = t['close_ts']
    entry_p = t['entry_price']

    peak_during = entry_p
    peak_during_ts = open_ts
    peak_after = 0.0
    peak_after_ts = 0
    final_p = entry_p
    sells_during_hold = 0
    sells_total = 0
    sandwich_check = False  # was there a big sell right after our buy?

    for e in events:
        if e['ts'] < open_ts: continue
        if e['side'] == 'sell':
            sells_total += 1
            if e['ts'] <= close_ts:
                sells_during_hold += 1
                # Sandwich detection: large sell within 500ms after buy
                if e['ts'] - open_ts < 500 and e['sol'] > 1.0:
                    sandwich_check = True
        if e['price'] > 0:
            if e['ts'] <= close_ts:
                if e['price'] > peak_during:
                    peak_during = e['price']
                    peak_during_ts = e['ts']
            else:
                if e['price'] > peak_after:
                    peak_after = e['price']
                    peak_after_ts = e['ts']
        if e['ts'] <= close_ts:
            final_p = e['price'] or final_p

    peak_during_mult = peak_during / entry_p
    peak_after_mult = peak_after / entry_p if peak_after > 0 else 0.0
    final_mult = final_p / entry_p if final_p > 0 else 0.0
    analysis_results.append({
        **t,
        'peak_during_mult': peak_during_mult,
        'peak_during_age_ms': peak_during_ts - open_ts,
        'peak_after_mult': peak_after_mult,
        'peak_after_age_ms': peak_after_ts - close_ts if peak_after_ts > 0 else 0,
        'final_mult_at_close': final_mult,
        'sells_during_hold': sells_during_hold,
        'sandwich_check': sandwich_check,
    })

# ============================================================================
# 1. HELD-TO-PEAK: Did we sell too early?
# ============================================================================
print('=' * 130)
print('1. HELD-TO-PEAK ANALYSIS — for each LOSING trade, did we have a chance to exit at profit?')
print('=' * 130)
losses_with_peak = [t for t in analysis_results if not t['is_win']]
exit_too_early = [t for t in losses_with_peak if t['peak_during_mult'] >= 1.05]
real_rugs = [t for t in losses_with_peak if t['peak_during_mult'] < 1.05]
print(f'Total losses analyzed: {len(losses_with_peak)}')
print(f'  EXIT-TOO-EARLY (peak during hold >= 1.05x): {len(exit_too_early)} losses ({len(exit_too_early)*100//max(len(losses_with_peak),1)}%)')
print(f'  REAL-RUG (peak during hold < 1.05x):       {len(real_rugs)} losses ({len(real_rugs)*100//max(len(losses_with_peak),1)}%)')
print()
print(f'If we had exited at peak instead of bot-exit:')
total_actual_loss = sum(t['pnl'] for t in losses_with_peak)
# Estimated value if we'd taken peak: assume could capture 90% of peak
hypothetical_pnl = sum(0 if t['peak_during_mult'] < 1.05 else (t['peak_during_mult'] - 1.0) * 0.9 * (-t['pnl'] / max(1.0 - t['final_mult_at_close'], 0.001)) for t in losses_with_peak)
# Actually simpler — assume entry cost = -pnl / (1 - final_mult), peak proceeds = entry_cost * peak_during_mult * 0.9 (slippage)
# Just show the peak mult distribution
print()
print('Peak-during-hold distribution for LOSING trades:')
peak_buckets = {'<1.0': 0, '1.0-1.05': 0, '1.05-1.20': 0, '1.20-1.50': 0, '1.50-2.0': 0, '>=2.0': 0}
for t in losses_with_peak:
    p = t['peak_during_mult']
    if p < 1.0: peak_buckets['<1.0'] += 1
    elif p < 1.05: peak_buckets['1.0-1.05'] += 1
    elif p < 1.20: peak_buckets['1.05-1.20'] += 1
    elif p < 1.50: peak_buckets['1.20-1.50'] += 1
    elif p < 2.0: peak_buckets['1.50-2.0'] += 1
    else: peak_buckets['>=2.0'] += 1
for k, v in peak_buckets.items():
    pct = v * 100 // max(len(losses_with_peak), 1)
    bar = '#' * (pct // 2)
    print(f'  peak {k:>10s}: {v:>3d} ({pct:>2d}%) {bar}')

# ============================================================================
# 2. HELD-TO-END: For each loss, what happened AFTER we exited?
# ============================================================================
print()
print('=' * 130)
print('2. HELD-TO-END — Did the token recover after our exit?')
print('=' * 130)
recovery_buckets = {'no_recovery': 0, 'recovered_to_entry': 0, '1.05-1.5x_after_exit': 0, '1.5-2x_after_exit': 0, '>=2x_after_exit': 0}
for t in losses_with_peak:
    p = t['peak_after_mult']
    if p == 0 or p < 1.0: recovery_buckets['no_recovery'] += 1
    elif p < 1.05: recovery_buckets['recovered_to_entry'] += 1
    elif p < 1.5: recovery_buckets['1.05-1.5x_after_exit'] += 1
    elif p < 2.0: recovery_buckets['1.5-2x_after_exit'] += 1
    else: recovery_buckets['>=2x_after_exit'] += 1
for k, v in recovery_buckets.items():
    pct = v * 100 // max(len(losses_with_peak), 1)
    bar = '#' * (pct // 2)
    print(f'  {k:>30s}: {v:>3d} ({pct:>2d}%) {bar}')

# ============================================================================
# 3. SANDWICH DETECTION
# ============================================================================
print()
print('=' * 130)
print('3. SANDWICH/MEV DETECTION (large sell within 500ms of our buy)')
print('=' * 130)
sandwiched = [t for t in analysis_results if t.get('sandwich_check')]
sandwich_losses = [t for t in sandwiched if not t['is_win']]
sandwich_wins = [t for t in sandwiched if t['is_win']]
print(f'Trades with potential sandwich: {len(sandwiched)} ({len(sandwich_losses)} losses, {len(sandwich_wins)} wins)')
if sandwiched:
    print(f'Total pnl from sandwiched trades: {sum(t["pnl"] for t in sandwiched):+.5f} SOL')

# ============================================================================
# 4. PER-LANE ECONOMICS
# ============================================================================
print()
print('=' * 130)
print('4. PER-LANE ECONOMICS (across all 22 runs)')
print('=' * 130)
lane_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'best': 0.0, 'worst': 0.0, 'gross_wins': 0.0, 'gross_losses': 0.0})
for t in all_trades:
    s = lane_stats[t['lane']]
    s['count'] += 1
    s['pnl'] += t['pnl']
    if t['is_win']:
        s['wins'] += 1
        s['gross_wins'] += t['pnl']
    else:
        s['losses'] += 1
        s['gross_losses'] += t['pnl']
    if t['pnl'] > s['best']: s['best'] = t['pnl']
    if t['pnl'] < s['worst']: s['worst'] = t['pnl']
print(f'{"lane":>20s}  {"trades":>7s}  {"W/L":>10s}  {"win%":>5s}  {"avg":>9s}  {"net":>10s}  {"win/loss_ratio":>14s}  {"best":>8s}  {"worst":>8s}')
for lane in sorted(lane_stats.keys(), key=lambda l: -lane_stats[l]['pnl']):
    s = lane_stats[lane]
    avg = s['pnl'] / max(s['count'], 1)
    win_pct = s['wins'] * 100 // max(s['count'], 1)
    avg_win = s['gross_wins'] / max(s['wins'], 1)
    avg_loss = -s['gross_losses'] / max(s['losses'], 1)
    ratio = avg_win / max(avg_loss, 0.0001)
    print(f'  {lane:>20s}  {s["count"]:>7d}  {s["wins"]:>3d}/{s["losses"]:<5d}  {win_pct:>4d}%  {avg:>+9.5f}  {s["pnl"]:>+10.5f}  {ratio:>13.2f}x  {s["best"]:>+8.4f}  {s["worst"]:>+8.4f}')

# ============================================================================
# 5. STAKE SIZING SIMULATION — what if we'd halved stake?
# ============================================================================
print()
print('=' * 130)
print('5. STAKE SIZING SIMULATION (across all 22 runs)')
print('=' * 130)
# All positions used 0.05 max. Simulate 0.025 stake (proportional reduction)
total_pnl = sum(t['pnl'] for t in all_trades)
half_stake_pnl = total_pnl * 0.5
print(f'Actual total pnl @ 0.05 stake: {total_pnl:+.5f} SOL ({total_pnl*89.87:+.2f} USD)')
print(f'If we"d halved stake to 0.025: {half_stake_pnl:+.5f} SOL — but losses also halved')
print(f'  Losses go from {sum(t["pnl"] for t in all_trades if not t["is_win"]):+.5f} to {sum(t["pnl"] for t in all_trades if not t["is_win"])*0.5:+.5f}')
print(f'  Wins go from {sum(t["pnl"] for t in all_trades if t["is_win"]):+.5f} to {sum(t["pnl"] for t in all_trades if t["is_win"])*0.5:+.5f}')

# Anti-martingale: smaller after losses, larger after wins
print()
print('What if we used ANTI-MARTINGALE (stake scales with recent W/L)?')
print('  For each trade, if last_3_trades_pnl > 0, use 0.05; else use 0.025:')
sim_pnl_anti = 0.0
last3 = []
for t in sorted(all_trades, key=lambda x: x['close_ts']):
    if len(last3) >= 3:
        recent = sum(last3[-3:])
        scale = 1.0 if recent > 0 else 0.5
    else:
        scale = 0.5  # cautious start
    sim_pnl_anti += t['pnl'] * scale
    last3.append(t['pnl'])
print(f'  Anti-martingale total: {sim_pnl_anti:+.5f} SOL')

# ============================================================================
# 6. EXIT REASON DEEP DIVE — which reasons leave money on the table?
# ============================================================================
print()
print('=' * 130)
print('6. EXIT REASONS: which exit mechanisms leave money on the table?')
print('=' * 130)
reason_stats = defaultdict(lambda: {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0, 'avg_peak_during': 0.0, 'count_with_peak': 0})
for t in analysis_results:
    r = t['reason_close']
    reason_stats[r]['count'] += 1
    reason_stats[r]['pnl'] += t['pnl']
    if t['is_win']: reason_stats[r]['wins'] += 1
    else: reason_stats[r]['losses'] += 1
    if t.get('peak_during_mult'):
        reason_stats[r]['avg_peak_during'] += t['peak_during_mult']
        reason_stats[r]['count_with_peak'] += 1
print(f'{"reason":>40s}  {"count":>5s}  {"W/L":>10s}  {"win%":>5s}  {"avg_pnl":>9s}  {"net":>10s}  {"avg_peak_during":>15s}')
for r in sorted(reason_stats.keys(), key=lambda x: -reason_stats[x]['pnl']):
    s = reason_stats[r]
    avg = s['pnl'] / max(s['count'], 1)
    win_pct = s['wins'] * 100 // max(s['count'], 1)
    avg_peak = s['avg_peak_during'] / max(s['count_with_peak'], 1)
    print(f'  {r[:40]:>40s}  {s["count"]:>5d}  {s["wins"]:>3d}/{s["losses"]:<5d}  {win_pct:>4d}%  {avg:>+9.5f}  {s["pnl"]:>+10.5f}  {avg_peak:>14.3f}x')

# ============================================================================
# 7. BUYER WALLET REUSE — are there smart money wallets to follow?
# ============================================================================
print()
print('=' * 130)
print('7. BUYER WALLET ANALYSIS — do specific wallets repeatedly hit moonshots?')
print('=' * 130)
# For each mint, find the FIRST 3 buyers. Track which wallets appear in winning vs losing trades.
wallet_outcomes = defaultdict(lambda: {'wins': 0, 'losses': 0, 'win_pnl': 0.0, 'loss_pnl': 0.0, 'mints': []})
for t in analysis_results:
    key = (t['run'], t['mint'])
    events = all_events.get(key, [])
    early_buyers = []
    for e in events:
        if e['side'] == 'buy' and e['user'] and e['user'] not in early_buyers:
            early_buyers.append(e['user'])
            if len(early_buyers) >= 3:
                break
    for buyer in early_buyers:
        if t['is_win']:
            wallet_outcomes[buyer]['wins'] += 1
            wallet_outcomes[buyer]['win_pnl'] += t['pnl']
        else:
            wallet_outcomes[buyer]['losses'] += 1
            wallet_outcomes[buyer]['loss_pnl'] += t['pnl']
        wallet_outcomes[buyer]['mints'].append(t['mint_short'])

# Wallets that appear in 5+ trades
multi_appearance = [(w, d) for w, d in wallet_outcomes.items() if d['wins'] + d['losses'] >= 5]
multi_appearance.sort(key=lambda x: -(x[1]['win_pnl'] + x[1]['loss_pnl']))
print(f'Wallets appearing in 5+ trades (top 20 by net pnl):')
print(f'{"wallet":>12s}  {"appearances":>11s}  {"W/L":>10s}  {"win_pnl":>9s}  {"loss_pnl":>9s}  {"net":>9s}')
for w, d in multi_appearance[:20]:
    net = d['win_pnl'] + d['loss_pnl']
    appearances = d['wins'] + d['losses']
    print(f'  {w[:8]:>12s}  {appearances:>11d}  {d["wins"]:>3d}/{d["losses"]:<5d}  {d["win_pnl"]:>+9.5f}  {d["loss_pnl"]:>+9.5f}  {net:>+9.5f}')
