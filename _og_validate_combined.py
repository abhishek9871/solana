"""Test combining the strongest pure-loss filters with the existing ones.

Existing (deployed):
  E1: curve_lag_reveal AND age_ms > 3000
  E2: priced_snap AND top700>0.70 AND buy700<3.5 AND hhi700<0.99

New candidates (from brute force):
  N1: hhi700 < 0.14 AND first_buy_sol < 2.5    (8L, $2.08)
  N2: priced_snap AND move700 < 1.0            (4L, $0.77)
  N3: priced_snap AND move1500 < 1.04          (4L, $1.55)
  N4: curve_lag_reveal AND buy_to_vsol > 0.55  (3L, $0.86)
  N5: sell1500 > 0.006 AND cluster_score < 206 (7L, $1.81)
"""
import json, os

RUNS = [
    'pgg2_direct_live_20260506_214938',
    'pgg2_direct_live_20260507_160325',
]

trades = []
for run in RUNS:
    path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not os.path.exists(path): continue
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
        sp = sps.get(mint, {})
        sf = sp.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        buy1500 = float(sf.get('buy1500') or 0)
        vsol = float(sf.get('vsol_sol') or 0.001)
        trades.append({
            'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'reason_close': cl.get('reason') or '',
            'buy700': float(sf.get('buy700') or 0),
            'buy1500': buy1500,
            'top700': float(sf.get('top_share700') or 1.0),
            'hhi700': float(sf.get('buyer_hhi700') or 1.0),
            'sell1500': float(sf.get('sell1500') or 0),
            'move700': float(sf.get('move700') or 1.0),
            'move1500': float(sf.get('move1500') or 1.0),
            'age_ms': int(sf.get('age_ms') or 0),
            'cluster_score': float(sf.get('cluster_score') or 0),
            'first_buy_sol': float(sf.get('first_buy_sol') or 0),
            'buy_to_vsol': buy1500 / vsol if vsol > 0 else 0,
        })

# Filter set (combinations to test)
FILTERS = {
    'E1': lambda t: t['lane'] == 'curve_lag_reveal' and t['age_ms'] > 3000,
    'E2': lambda t: t['lane'] == 'priced_snap' and t['top700'] > 0.70 and t['buy700'] < 3.5 and t['hhi700'] < 0.99,
    'N1': lambda t: t['hhi700'] < 0.14 and t['first_buy_sol'] < 2.5,
    'N2': lambda t: t['lane'] == 'priced_snap' and t['move700'] < 1.0,
    'N3': lambda t: t['lane'] == 'priced_snap' and t['move1500'] < 1.04,
    'N4': lambda t: t['lane'] == 'curve_lag_reveal' and t['buy_to_vsol'] > 0.55,
    'N5': lambda t: t['sell1500'] > 0.006 and t['cluster_score'] < 206,
}


def evaluate_filterset(active_set):
    """Apply OR of all active filters (any one matches = blocked)."""
    blocked = []
    kept = []
    for t in trades:
        if any(FILTERS[name](t) for name in active_set):
            blocked.append(t)
        else:
            kept.append(t)
    kw = sum(1 for t in kept if t['is_win'])
    kl = sum(1 for t in kept if not t['is_win'])
    bw = sum(1 for t in blocked if t['is_win'])
    bl = sum(1 for t in blocked if not t['is_win'])
    kp = sum(t['pnl'] for t in kept)
    bp = sum(t['pnl'] for t in blocked)
    return kw, kl, bw, bl, kp, bp, blocked


# Try various combinations
COMBINATIONS = [
    {'E1', 'E2'},  # existing
    {'E1', 'E2', 'N1'},
    {'E1', 'E2', 'N2'},
    {'E1', 'E2', 'N3'},
    {'E1', 'E2', 'N4'},
    {'E1', 'E2', 'N5'},
    {'E1', 'E2', 'N1', 'N4'},
    {'E1', 'E2', 'N1', 'N2', 'N4'},
    {'E1', 'E2', 'N1', 'N3', 'N4'},
    {'E1', 'E2', 'N1', 'N2', 'N3', 'N4'},
    {'E1', 'E2', 'N1', 'N2', 'N3', 'N4', 'N5'},
]

print('=' * 130)
print(f'Baseline: 43W/59L, +0.23187 SOL across 102 trades')
print('=' * 130)
print(f'{"filter set":>50s}  {"kept":>10s}  {"blocked":>10s}  {"kept_pnl":>12s}  {"blocked_W_lost":>14s}  {"DELTA":>11s}')
print('-' * 130)

baseline_pnl = sum(t['pnl'] for t in trades)
for combo in COMBINATIONS:
    kw, kl, bw, bl, kp, bp, _ = evaluate_filterset(combo)
    label = ' + '.join(sorted(combo))
    delta = kp - baseline_pnl
    win_lost = sum(t['pnl'] for t in trades if t['is_win'] and any(FILTERS[name](t) for name in combo))
    print(f'  {label[:48]:>50s}  {kw:>3d}W/{kl:>2d}L  {bw:>3d}W/{bl:>2d}L  {kp:>+12.5f}  {win_lost:>+14.5f}  {delta:>+11.5f}')

# Detail: best combination
best_combo = {'E1', 'E2', 'N1', 'N2', 'N3', 'N4'}  # likely strongest with no winner sacrifice
print()
print('=' * 130)
print(f'BEST COMBINATION: {sorted(best_combo)}')
print('=' * 130)
kw, kl, bw, bl, kp, bp, blocked = evaluate_filterset(best_combo)
print(f'Kept    : {kw}W / {kl}L  net {kp:+.5f}')
print(f'Blocked : {bw}W / {bl}L  pnl {bp:+.5f}')
print(f'Delta   : {kp - baseline_pnl:+.5f} SOL ({(kp-baseline_pnl)*89.87:+.2f} USD at $89.87/SOL)')
print()
print('BLOCKED TRADES:')
for t in sorted(blocked, key=lambda x: x['pnl']):
    label = 'WIN ' if t['is_win'] else 'LOSS'
    triggered = [n for n in best_combo if FILTERS[n](t)]
    print(f'  {t["mint"]:>10s} {label} pnl={t["pnl"]:+.5f}  reason={t["reason_close"][:25]:>25s}  triggered={"+".join(triggered)}')
