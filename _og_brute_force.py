"""Brute-force search for multi-feature filters that catch more losses without
sacrificing winners. We try every 2-feature and 3-feature combination of
threshold cuts."""
import json, os
from itertools import combinations

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
        # ALL features I can think of
        sell1500 = float(sf.get('sell1500') or 0)
        buy1500 = float(sf.get('buy1500') or 0)
        sell_ratio1500 = sell1500 / max(buy1500, 0.001)
        buy700 = float(sf.get('buy700') or 0)
        sell700 = float(sf.get('sell700') or 0)
        sell_ratio700 = sell700 / max(buy700, 0.001)
        vsol = float(sf.get('vsol_sol') or 0.001)
        buy_to_vsol = buy1500 / vsol if vsol > 0 else 0
        first_buy = float(sf.get('first_buy_sol') or 0.001)
        top1500 = float(sf.get('top_share1500') or 1.0)
        top700 = float(sf.get('top_share700') or 1.0)
        slot_buy_sol = float(sf.get('slot_buy_sol') or 0)
        slot_buyers = int(sf.get('slot_buyers') or 0)
        slot_top = float(sf.get('slot_top_share') or 1.0)
        score = float(sp.get('score') or 0)
        # Move metrics
        move250 = float(sf.get('move250') or 1.0)
        move700 = float(sf.get('move700') or 1.0)
        move1500 = float(sf.get('move1500') or 1.0)
        # Derived: ratios
        score_per_buy = score / max(buy700, 0.001)
        first_to_buy = first_buy / max(buy700, 0.001)
        slot_to_total = slot_buy_sol / max(buy1500, 0.001)
        recent_v_total = buy700 / max(buy1500, 0.001)  # 700ms / 1500ms - "freshness"

        trades.append({
            'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'reason_close': cl.get('reason') or '',
            'score': score,
            'buy700': buy700, 'buy1500': buy1500,
            'uniq700': int(sf.get('uniq700') or 0),
            'uniq1500': int(sf.get('uniq1500') or 0),
            'top700': top700, 'top1500': top1500,
            'hhi700': float(sf.get('buyer_hhi700') or 1.0),
            'sell700': sell700, 'sell1500': sell1500,
            'sell_ratio700': sell_ratio700, 'sell_ratio1500': sell_ratio1500,
            'move250': move250, 'move700': move700, 'move1500': move1500,
            'age_ms': int(sf.get('age_ms') or 0),
            'last_sell_age_ms': int(sf.get('last_sell_age_ms') or 999999),
            'cluster_score': float(sf.get('cluster_score') or 0),
            'vsol': vsol,
            'first_buy_sol': first_buy,
            'wave_arm_age_ms': int(sf.get('wave_arm_age_ms') or 0),
            'slot_buy_sol': slot_buy_sol, 'slot_buyers': slot_buyers, 'slot_top': slot_top,
            'has_curve': bool(sf.get('has_curve')),
            # Derived
            'buy_to_vsol': buy_to_vsol,
            'score_per_buy': score_per_buy,
            'first_to_buy': first_to_buy,
            'slot_to_total': slot_to_total,
            'recent_v_total': recent_v_total,
        })

wins = [t for t in trades if t['is_win']]
losses = [t for t in trades if not t['is_win']]
print(f'Total: {len(wins)}W / {len(losses)}L')
print()

FEATURES = [
    'score', 'buy700', 'buy1500', 'uniq700', 'uniq1500',
    'top700', 'top1500', 'hhi700',
    'sell700', 'sell1500', 'sell_ratio700', 'sell_ratio1500',
    'move250', 'move700', 'move1500',
    'age_ms', 'last_sell_age_ms', 'cluster_score', 'vsol', 'first_buy_sol',
    'wave_arm_age_ms', 'slot_buy_sol', 'slot_buyers', 'slot_top',
    'buy_to_vsol', 'score_per_buy', 'first_to_buy', 'slot_to_total', 'recent_v_total',
]

# Generate candidate thresholds for each feature: percentiles of losses (try blocking BELOW or ABOVE)
def percentiles(vals, levels):
    if not vals: return []
    s = sorted(vals)
    n = len(s)
    return [s[int(n * lvl)] for lvl in levels if 0 <= int(n * lvl) < n]

# For each feature, try thresholds at loss-percentiles (10-90)
LEVELS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

def make_predicates(feat):
    """Generate (label, predicate) tuples for cuts on this feature."""
    loss_vals = [t[feat] for t in losses]
    win_vals = [t[feat] for t in wins]
    cuts = sorted(set(percentiles(loss_vals, LEVELS) + percentiles(win_vals, LEVELS)))
    preds = []
    for c in cuts:
        preds.append((f'{feat}>{c}', feat, '>', c))
        preds.append((f'{feat}<{c}', feat, '<', c))
    return preds


def apply_pred(t, p):
    feat, op, c = p[1], p[2], p[3]
    v = t[feat]
    return v > c if op == '>' else v < c


# Existing filters (already in PGG2.py) — exclude these from the search
def existing_blocks(t):
    if t['lane'] == 'curve_lag_reveal' and t['age_ms'] > 3000:
        return True
    if (t['lane'] == 'priced_snap'
        and t['top700'] > 0.70 and t['buy700'] < 3.5 and t['hhi700'] < 0.99):
        return True
    return False


# Pre-filter: remaining losses & winners after existing filter
remaining_w = [t for t in wins if not existing_blocks(t)]
remaining_l = [t for t in losses if not existing_blocks(t)]
print(f'After existing filters: {len(remaining_w)}W / {len(remaining_l)}L remaining')
print()

# Build candidate predicates
all_preds = []
for f in FEATURES:
    all_preds.extend(make_predicates(f))
print(f'Total candidate cuts to test: {len(all_preds)}')


def evaluate(preds_list):
    """Apply combined predicates (AND) to remaining trades, return (loss_blocked, win_blocked, loss_pnl_saved, win_pnl_lost)."""
    bl = [t for t in remaining_l if all(apply_pred(t, p) for p in preds_list)]
    bw = [t for t in remaining_w if all(apply_pred(t, p) for p in preds_list)]
    return len(bl), len(bw), -sum(t['pnl'] for t in bl), sum(t['pnl'] for t in bw)


# Stage 1: single-feature pure-loss filters (zero winners caught)
print()
print('=' * 130)
print('STAGE 1: 1-feature pure-loss filters (after existing)')
print('=' * 130)
single_results = []
for p in all_preds:
    lc, wc, ls, wl = evaluate([p])
    if wc == 0 and lc >= 2:
        single_results.append((p, lc, ls))
single_results.sort(key=lambda x: -x[2])  # by saved pnl
for p, lc, ls in single_results[:15]:
    print(f'  {p[0]:>40s}  blocks {lc:>3d}L (saved {ls:+.5f})')

# Stage 2: 2-feature combos
print()
print('=' * 130)
print('STAGE 2: 2-feature pure-loss filters (after existing)')
print('=' * 130)
double_results = []
# To keep this tractable, only consider single-feature predicates that already block at least 1 loss alone
seed = [p for p in all_preds if evaluate([p])[0] >= 2 and evaluate([p])[1] <= len(remaining_w) // 4]
print(f'(seed predicates: {len(seed)})')
for i, p1 in enumerate(seed):
    for p2 in seed[i+1:]:
        if p1[1] == p2[1]:
            continue  # same feature
        lc, wc, ls, wl = evaluate([p1, p2])
        if wc == 0 and lc >= 3:
            double_results.append(([p1, p2], lc, ls))
double_results.sort(key=lambda x: -x[2])
for preds, lc, ls in double_results[:20]:
    labels = ' AND '.join(p[0] for p in preds)
    print(f'  {labels:>70s}  blocks {lc:>3d}L (saved {ls:+.5f})')

# Stage 3: 3-feature combos (most powerful)
print()
print('=' * 130)
print('STAGE 3: 3-feature pure-loss filters (after existing)')
print('=' * 130)
triple_results = []
# Use top single-feature predicates as seeds for triples
top_seeds = [p for p, lc, ls in single_results[:30]]
top_seeds += [p for p in all_preds if evaluate([p])[0] >= 4 and evaluate([p])[1] <= 2][:50]
# unique
seen_names = set()
unique_seeds = []
for p in top_seeds:
    if p[0] not in seen_names:
        unique_seeds.append(p); seen_names.add(p[0])
print(f'(triple seed: {len(unique_seeds)})')
for combo in combinations(unique_seeds, 3):
    feats = {p[1] for p in combo}
    if len(feats) < 3:
        continue
    lc, wc, ls, wl = evaluate(list(combo))
    if wc == 0 and lc >= 5:
        triple_results.append((list(combo), lc, ls))
triple_results.sort(key=lambda x: -x[2])
for preds, lc, ls in triple_results[:25]:
    labels = ' AND '.join(p[0] for p in preds)
    print(f'  {labels[:90]:>92s}  blocks {lc:>3d}L (saved {ls:+.5f})')

# Stage 4: lane-restricted searches
print()
print('=' * 130)
print('STAGE 4: Per-lane analysis')
print('=' * 130)
for lane in ['priced_snap', 'curve_lag_reveal']:
    print(f'\n--- Lane: {lane} ---')
    lane_l = [t for t in remaining_l if t['lane'] == lane]
    lane_w = [t for t in remaining_w if t['lane'] == lane]
    print(f'  remaining {len(lane_w)}W / {len(lane_l)}L on this lane')
    for p in all_preds:
        bl = [t for t in lane_l if apply_pred(t, p)]
        bw = [t for t in lane_w if apply_pred(t, p)]
        if not bw and len(bl) >= 2:
            print(f'    {p[0]:>40s}  blocks {len(bl):>2d}L  saved {-sum(t["pnl"] for t in bl):+.5f}')
