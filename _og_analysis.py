"""Comprehensive analysis of OG live runs.

Goal: find pre-trade or exit-mechanism filters that catch losses but don't kill winners.
"""
import json, os

RUNS = [
    'pgg2_direct_live_20260506_214938',  # 2-hour OG winner from yesterday
    'pgg2_direct_live_20260507_160325',  # this evening's OG run
]

trades = []
for run in RUNS:
    path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not os.path.exists(path):
        print(f'(missing: {run})')
        continue
    sps = {}
    opens = {}
    closes = {}
    for line in open(path):
        if not line.strip():
            continue
        try:
            x = json.loads(line)
        except Exception:
            continue
        k = x.get('kind')
        m = x.get('mint')
        if k == 'strike_plan':
            sps[m] = x
        elif k == 'open':
            opens[m] = x
        elif k == 'close':
            closes[m] = x

    for mint, op in opens.items():
        cl = closes.get(mint)
        if not cl:
            continue
        sp = sps.get(mint, {})
        sf = sp.get('features') or {}
        of = op.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        # Pull comprehensive features from strike_plan (pre-trade decision)
        trades.append({
            'run': run.split('_')[-1],
            'mint': mint[:8],
            'pnl': pnl,
            'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'score': float(sp.get('score') or 0.0),
            'target_sol': float(sp.get('target_sol') or 0.0),
            'reason_open': sp.get('reason') or '',
            'reason_close': cl.get('reason') or '',
            # Volume & breadth at trigger
            'buy700': float(sf.get('buy700') or 0),
            'buy1500': float(sf.get('buy1500') or 0),
            'uniq700': int(sf.get('uniq700') or 0),
            'uniq1500': int(sf.get('uniq1500') or 0),
            # Concentration
            'top_share700': float(sf.get('top_share700') or 1.0),
            'top_share1500': float(sf.get('top_share1500') or 1.0),
            'hhi700': float(sf.get('buyer_hhi700') or 1.0),
            # Sells
            'sell700': float(sf.get('sell700') or 0),
            'sell1500': float(sf.get('sell1500') or 0),
            # Move/age
            'move250': float(sf.get('move250') or 1.0),
            'move700': float(sf.get('move700') or 1.0),
            'move1500': float(sf.get('move1500') or 1.0),
            'age_ms': int(sf.get('age_ms') or 0),
            'last_buy_age_ms': int(sf.get('last_buy_age_ms') or 0),
            'last_sell_age_ms': int(sf.get('last_sell_age_ms') or 0),
            # Price & curve
            'price': float(sf.get('price') or 0),
            'has_curve': bool(sf.get('has_curve')),
            'cluster_score': float(sf.get('cluster_score') or 0),
            'vsol_sol': float(sf.get('vsol_sol') or 0),
            # First buy / wave
            'first_buy_sol': float(sf.get('first_buy_sol') or 0),
            'wave_armed': bool(sf.get('wave_armed')),
            'wave_arm_age_ms': int(sf.get('wave_arm_age_ms') or 0),
            'wave_base_move': float(sf.get('wave_base_move') or 1.0),
            # Slot
            'slot_buy_sol': float(sf.get('slot_buy_sol') or 0),
            'slot_buyers': int(sf.get('slot_buyers') or 0),
            'slot_top_share': float(sf.get('slot_top_share') or 1.0),
            # Mayhem
            'is_mayhem': bool(sf.get('is_mayhem')),
            # Raw / 10s window (where available)
            'raw_buy_sol_10s': float(sf.get('raw_buy_sol_10s') or 0),
            'raw_unique_buyers_10s': int(sf.get('raw_unique_buyers_10s') or 0),
            'raw_top_share_10s': float(sf.get('raw_top_share_10s') or 1.0),
            'raw_sell_ratio_10s': float(sf.get('raw_sell_ratio_10s') or 0),
            'raw_entry_move': float(sf.get('raw_entry_move') or 1.0),
            'raw_confirm_mult': float(sf.get('raw_confirm_mult') or 1.0),
            # Curve lag specific
            'entry_move_from_first': float(sf.get('entry_move_from_first') or 0),
            'curve_lag_live_buy700': float(sf.get('curve_lag_live_buy700') or 0),
            'curve_lag_live_unique700': int(sf.get('curve_lag_live_unique700') or 0),
            'curve_lag_live_top700': float(sf.get('curve_lag_live_top700') or 0),
            # Entry size
            'entry_size_reason': sf.get('entry_size_reason') or '',
            # Score-related
        })

# Aggregate by lane
print('=' * 130)
print(f'TOTAL TRADES: {len(trades)} ({sum(1 for t in trades if t["is_win"])} W / {sum(1 for t in trades if not t["is_win"])} L)')
print('=' * 130)

lanes = {}
for t in trades:
    lane = t['lane']
    if lane not in lanes:
        lanes[lane] = []
    lanes[lane].append(t)

print()
print('LANE BREAKDOWN:')
for lane, ts in sorted(lanes.items(), key=lambda kv: -sum(t['pnl'] for t in kv[1])):
    wins = [t for t in ts if t['is_win']]
    losses = [t for t in ts if not t['is_win']]
    net = sum(t['pnl'] for t in ts)
    win_pnl = sum(t['pnl'] for t in wins)
    loss_pnl = sum(t['pnl'] for t in losses)
    print(f'  {lane:>20s}: {len(wins):>3d}W/{len(losses):<3d}  net {net:+.5f}  wins {win_pnl:+.5f}  losses {loss_pnl:+.5f}  ({len(ts)} trades)')

# Aggregate by close reason for losses only
print()
print('LOSS REASONS:')
loss_reasons = {}
for t in trades:
    if t['is_win']:
        continue
    rsn = t['reason_close']
    if rsn not in loss_reasons:
        loss_reasons[rsn] = []
    loss_reasons[rsn].append(t)
for rsn, ts in sorted(loss_reasons.items(), key=lambda kv: sum(t['pnl'] for t in kv[1])):
    print(f'  {rsn:>40s}: {len(ts):>3d} losses, total {sum(t["pnl"] for t in ts):+.5f}')

# All trades sorted by pnl
print()
print('=' * 130)
print('ALL TRADES (sorted by pnl):')
print('=' * 130)
print(f'{"run":>6s} {"mint":>9s} {"lane":>20s} {"target":>6s} {"buy700":>7s} {"u700":>4s} {"top":>5s} {"hhi":>5s} {"move":>5s} {"age":>5s} {"score":>6s} {"pnl":>10s}  {"close_reason":>30s}')
for t in sorted(trades, key=lambda x: x['pnl']):
    print(f'{t["run"][:6]:>6s} {t["mint"]:>9s} {t["lane"][:20]:>20s} {t["target_sol"]:>6.4f} {t["buy700"]:>7.2f} {t["uniq700"]:>4d} {t["top_share700"]:>5.2f} {t["hhi700"]:>5.2f} {t["move700"]:>5.2f} {t["age_ms"]:>5d} {t["score"]:>6.1f} {t["pnl"]:>+10.6f}  {t["reason_close"][:30]:>30s}')
