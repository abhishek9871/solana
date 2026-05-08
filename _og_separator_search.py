"""For every feature, find threshold cuts that separate wins from losses cleanly."""
import json, os
import statistics

RUNS = [
    'pgg2_direct_live_20260506_214938',
    'pgg2_direct_live_20260507_160325',
]

trades = []
for run in RUNS:
    path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not os.path.exists(path):
        continue
    sps = {}; opens = {}; closes = {}
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
        of = op.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        trades.append({
            'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'score': float(sp.get('score') or 0),
            'reason_close': cl.get('reason') or '',
            'buy700': float(sf.get('buy700') or 0),
            'buy1500': float(sf.get('buy1500') or 0),
            'uniq700': int(sf.get('uniq700') or 0),
            'uniq1500': int(sf.get('uniq1500') or 0),
            'top700': float(sf.get('top_share700') or 1.0),
            'top1500': float(sf.get('top_share1500') or 1.0),
            'hhi700': float(sf.get('buyer_hhi700') or 1.0),
            'sell1500': float(sf.get('sell1500') or 0),
            'move250': float(sf.get('move250') or 1.0),
            'move700': float(sf.get('move700') or 1.0),
            'move1500': float(sf.get('move1500') or 1.0),
            'age_ms': int(sf.get('age_ms') or 0),
            'last_buy_age_ms': int(sf.get('last_buy_age_ms') or 0),
            'last_sell_age_ms': int(sf.get('last_sell_age_ms') or 0),
            'cluster_score': float(sf.get('cluster_score') or 0),
            'vsol_sol': float(sf.get('vsol_sol') or 0),
            'first_buy_sol': float(sf.get('first_buy_sol') or 0),
            'wave_armed': bool(sf.get('wave_armed')),
            'wave_arm_age_ms': int(sf.get('wave_arm_age_ms') or 0),
            'slot_buyers': int(sf.get('slot_buyers') or 0),
            'slot_top_share': float(sf.get('slot_top_share') or 1.0),
            'is_mayhem': bool(sf.get('is_mayhem')),
            'has_curve': bool(sf.get('has_curve')),
        })

wins = [t for t in trades if t['is_win']]
losses = [t for t in trades if not t['is_win']]
print(f'Total: {len(wins)}W / {len(losses)}L')

# Score every feature: how many losses can we block while keeping all wins?
print()
print('=' * 130)
print('FEATURE-BY-FEATURE SEPARATOR SEARCH (find threshold that blocks losses, keeps ALL wins):')
print('=' * 130)

NUMERIC_FEATURES = [
    'score', 'buy700', 'buy1500', 'uniq700', 'uniq1500', 'top700', 'top1500',
    'hhi700', 'sell1500', 'move250', 'move700', 'move1500', 'age_ms',
    'last_buy_age_ms', 'last_sell_age_ms', 'cluster_score', 'vsol_sol',
    'first_buy_sol', 'wave_arm_age_ms', 'slot_buyers', 'slot_top_share',
]

for feat in NUMERIC_FEATURES:
    win_vals = sorted([t[feat] for t in wins])
    loss_vals = sorted([t[feat] for t in losses])
    if not win_vals or not loss_vals:
        continue
    win_min, win_max = win_vals[0], win_vals[-1]
    loss_min, loss_max = loss_vals[0], loss_vals[-1]
    win_med = statistics.median(win_vals)
    loss_med = statistics.median(loss_vals)
    # Try blocking trades BELOW the min winner value (catches losses below win range)
    blocked_below = [t for t in losses if t[feat] < win_min]
    # Try blocking trades ABOVE the max winner value
    blocked_above = [t for t in losses if t[feat] > win_max]
    print(f'  {feat:>20s}: win[{win_min:>8.2f}, med={win_med:>8.2f}, max={win_max:>8.2f}]  loss[{loss_min:>8.2f}, med={loss_med:>8.2f}, max={loss_max:>8.2f}]  block_below={len(blocked_below):>2d} losses,  block_above={len(blocked_above):>2d} losses')

# Multi-feature search: try every combo of 2-3 features for best loss capture without winner loss
print()
print('=' * 130)
print('PURE-LOSS REGIONS (regions where ZERO winners exist):')
print('=' * 130)

# For each loss, find the unique combination that no winner has
# This is hard - let's do it heuristically

def block_count(losses_set, wins_set, predicate):
    """Return (losses_blocked, wins_blocked, total_loss_pnl_avoided, total_win_pnl_lost)."""
    bl = [t for t in losses_set if predicate(t)]
    bw = [t for t in wins_set if predicate(t)]
    return len(bl), len(bw), -sum(t['pnl'] for t in bl), sum(t['pnl'] for t in bw)

# Test specific candidate filters
candidates = [
    ('priced_snap layered_no_follow + hhi700>0.50',
     lambda t: t['lane'] == 'priced_snap' and 'layered_no_follow' in t['reason_close'] and t['hhi700'] > 0.50),
    ('top700>0.70 AND buy700<3.5 AND hhi700<0.99 (the conc-low-vol non-lottery)',
     lambda t: t['top700'] > 0.70 and t['buy700'] < 3.5 and t['hhi700'] < 0.99),
    ('priced_snap + score<260',
     lambda t: t['lane'] == 'priced_snap' and t['score'] < 260),
    ('curve_lag_reveal + buy700<10',
     lambda t: t['lane'] == 'curve_lag_reveal' and t['buy700'] < 10),
    ('curve_lag_reveal + age_ms>3000',
     lambda t: t['lane'] == 'curve_lag_reveal' and t['age_ms'] > 3000),
    ('priced_snap + buy700<3 AND uniq700<5',
     lambda t: t['lane'] == 'priced_snap' and t['buy700'] < 3 and t['uniq700'] < 5),
    ('priced_snap + sell1500>0.5',
     lambda t: t['lane'] == 'priced_snap' and t['sell1500'] > 0.5),
    ('move700<0.95 (entry below first price)',
     lambda t: t['move700'] < 0.95),
    ('priced_snap + last_sell_age_ms<300 (recent sell pressure)',
     lambda t: t['lane'] == 'priced_snap' and t['last_sell_age_ms'] < 300),
    ('priced_snap + age_ms>30000 (very old token)',
     lambda t: t['lane'] == 'priced_snap' and t['age_ms'] > 30000),
    ('curve_lag_reveal + last_sell_age_ms<2000',
     lambda t: t['lane'] == 'curve_lag_reveal' and t['last_sell_age_ms'] < 2000),
]
print(f'{"filter":>70s}  {"losses_blocked":>14s}  {"wins_blocked":>12s}  {"loss_pnl_saved":>14s}  {"win_pnl_lost":>13s}  {"net":>10s}')
for label, pred in candidates:
    bl, bw, loss_saved, win_lost = block_count(losses, wins, pred)
    net = loss_saved - win_lost
    print(f'  {label[:68]:>70s}  {bl:>14d}  {bw:>12d}  {loss_saved:>+14.5f}  {win_lost:>+13.5f}  {net:>+10.5f}')

# Detailed: combine multi-feature criteria
print()
print('=' * 130)
print('TRYING TIGHT PURE-LOSS FILTERS:')
print('=' * 130)

# One specific powerful filter: priced_snap with score < 270 AND top700 > 0.5 AND uniq700 < 5
def f1(t):
    return t['lane'] == 'priced_snap' and t['score'] < 270 and t['top700'] > 0.50 and t['uniq700'] < 5

bl1 = [t for t in losses if f1(t)]
bw1 = [t for t in wins if f1(t)]
print(f'priced_snap + score<270 + top700>0.5 + uniq700<5: blocks {len(bl1)} losses, {len(bw1)} winners (pnl saved: {-sum(t["pnl"] for t in bl1):+.5f})')
for t in bl1: print(f'  LOSS BLOCKED: {t["mint"]} pnl={t["pnl"]:+.5f}  features: score={t["score"]:.0f}, top700={t["top700"]:.2f}, uniq700={t["uniq700"]}')
for t in bw1: print(f'  WIN BLOCKED: {t["mint"]} pnl={t["pnl"]:+.5f}')
