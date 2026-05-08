"""Deeper search: find more pure-loss filters in the REMAINING population
(43 winners + 34 losses after the 5 deployed filters)."""
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
        # Comprehensive feature extraction including new derived features
        buy700 = float(sf.get('buy700') or 0)
        buy1500 = float(sf.get('buy1500') or 0)
        uniq700 = int(sf.get('uniq700') or 0)
        uniq1500 = int(sf.get('uniq1500') or 0)
        top700 = float(sf.get('top_share700') or 1.0)
        top1500 = float(sf.get('top_share1500') or 1.0)
        hhi700 = float(sf.get('buyer_hhi700') or 1.0)
        sell700 = float(sf.get('sell700') or 0)
        sell1500 = float(sf.get('sell1500') or 0)
        move250 = float(sf.get('move250') or 1.0)
        move700 = float(sf.get('move700') or 1.0)
        move1500 = float(sf.get('move1500') or 1.0)
        age_ms = int(sf.get('age_ms') or 0)
        last_sell_age_ms = int(sf.get('last_sell_age_ms') or 999999)
        cluster_score = float(sf.get('cluster_score') or 0)
        vsol = float(sf.get('vsol_sol') or 0.001)
        first_buy = float(sf.get('first_buy_sol') or 0)
        wave_arm_age_ms = int(sf.get('wave_arm_age_ms') or 0)
        slot_buy_sol = float(sf.get('slot_buy_sol') or 0)
        slot_buyers = int(sf.get('slot_buyers') or 0)
        slot_top = float(sf.get('slot_top_share') or 1.0)
        score = float(sp.get('score') or 0)
        # Derived features
        buy_velocity = (buy700 - max(0, buy1500 - buy700)) / 0.7  # velocity differential
        buy_accel = buy700 - (buy1500 - buy700)  # 700ms vs preceding 800ms
        top_trend = top700 - top1500  # concentration trend
        uniq_trend = uniq700 - uniq1500  # buyer count trend
        move_trend = move700 - move1500  # momentum trend
        slot_to_15 = slot_buy_sol / max(buy1500, 0.001)  # how much of 1500ms is in this slot
        first_to_15 = first_buy / max(buy1500, 0.001)
        recent_freshness = buy700 / max(buy1500, 0.001)  # 0 = no recent, 1 = all recent
        score_per_buyer = score / max(uniq1500, 1)
        cluster_per_buy = cluster_score / max(buy1500, 0.001)
        sell_pressure = sell1500 / max(buy1500, 0.001)
        late_buyers = uniq1500 - uniq700
        late_buy_sol = buy1500 - buy700
        avg_buy_size = buy1500 / max(uniq1500, 1)
        first_pct = first_buy / max(buy1500, 0.001)
        trades.append({
            'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'reason_close': cl.get('reason') or '',
            'score': score, 'buy700': buy700, 'buy1500': buy1500,
            'uniq700': uniq700, 'uniq1500': uniq1500,
            'top700': top700, 'top1500': top1500, 'hhi700': hhi700,
            'sell700': sell700, 'sell1500': sell1500,
            'move250': move250, 'move700': move700, 'move1500': move1500,
            'age_ms': age_ms, 'last_sell_age_ms': last_sell_age_ms,
            'cluster_score': cluster_score, 'vsol': vsol,
            'first_buy': first_buy, 'wave_arm_age_ms': wave_arm_age_ms,
            'slot_buy_sol': slot_buy_sol, 'slot_buyers': slot_buyers, 'slot_top': slot_top,
            # Derived
            'buy_velocity': buy_velocity, 'buy_accel': buy_accel,
            'top_trend': top_trend, 'uniq_trend': uniq_trend, 'move_trend': move_trend,
            'slot_to_15': slot_to_15, 'first_to_15': first_to_15,
            'recent_freshness': recent_freshness,
            'score_per_buyer': score_per_buyer, 'cluster_per_buy': cluster_per_buy,
            'sell_pressure': sell_pressure,
            'late_buyers': late_buyers, 'late_buy_sol': late_buy_sol,
            'avg_buy_size': avg_buy_size, 'first_pct': first_pct,
        })


# Apply existing 5 filters to get REMAINING set
def existing_blocks(t):
    if t['lane'] == 'curve_lag_reveal' and t['age_ms'] > 3000: return True
    if (t['lane'] == 'priced_snap' and t['top700'] > 0.70 and t['buy700'] < 3.5 and t['hhi700'] < 0.99): return True
    if t['hhi700'] < 0.14 and t['first_buy'] < 2.5: return True
    if t['lane'] == 'priced_snap' and t['move700'] < 1.0: return True
    if t['lane'] == 'priced_snap' and t['move1500'] < 1.04: return True
    return False


remaining = [t for t in trades if not existing_blocks(t)]
remaining_w = [t for t in remaining if t['is_win']]
remaining_l = [t for t in remaining if not t['is_win']]
print(f'After existing 5 filters: {len(remaining_w)}W / {len(remaining_l)}L remain')
print()

# Print all remaining LOSSES with full features, sorted by pnl
print('=' * 130)
print('REMAINING LOSSES:')
print('=' * 130)
print(f'{"mint":>10s} {"lane":>20s} {"pnl":>10s} {"buy700":>7s} {"u700":>4s} {"top":>4s} {"hhi":>4s} {"m700":>5s} {"m1500":>5s} {"age":>6s} {"cluster":>7s} {"firstbuy":>9s} {"reason":>30s}')
for t in sorted(remaining_l, key=lambda x: x['pnl']):
    print(f'{t["mint"]:>10s} {t["lane"][:20]:>20s} {t["pnl"]:>+10.6f} {t["buy700"]:>7.2f} {t["uniq700"]:>4d} {t["top700"]:>4.2f} {t["hhi700"]:>4.2f} {t["move700"]:>5.2f} {t["move1500"]:>5.2f} {t["age_ms"]:>6d} {t["cluster_score"]:>7.1f} {t["first_buy"]:>9.2f} {t["reason_close"][:30]:>30s}')

# Brute force: 3-feature pure-loss filters
print()
print('=' * 130)
print('3-FEATURE PURE-LOSS SEARCH (all winner-preserving filters):')
print('=' * 130)

ALL_FEATURES = [
    'score', 'buy700', 'buy1500', 'uniq700', 'uniq1500',
    'top700', 'top1500', 'hhi700',
    'sell700', 'sell1500',
    'move250', 'move700', 'move1500',
    'age_ms', 'last_sell_age_ms', 'cluster_score', 'vsol', 'first_buy',
    'wave_arm_age_ms', 'slot_buy_sol', 'slot_buyers', 'slot_top',
    'buy_velocity', 'buy_accel', 'top_trend', 'uniq_trend', 'move_trend',
    'slot_to_15', 'first_to_15', 'recent_freshness',
    'score_per_buyer', 'cluster_per_buy', 'sell_pressure',
    'late_buyers', 'late_buy_sol', 'avg_buy_size', 'first_pct',
]


def percentiles(vals, levels):
    if not vals: return []
    s = sorted(vals)
    n = len(s)
    return [s[int(n * lvl)] for lvl in levels if 0 <= int(n * lvl) < n]

LEVELS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95]


def make_predicates(feat):
    loss_vals = [t[feat] for t in remaining_l]
    win_vals = [t[feat] for t in remaining_w]
    cuts = sorted(set(percentiles(loss_vals, LEVELS) + percentiles(win_vals, LEVELS)))
    preds = []
    for c in cuts:
        preds.append((f'{feat}>{c:.4g}', feat, '>', c))
        preds.append((f'{feat}<{c:.4g}', feat, '<', c))
    return preds


def apply_pred(t, p):
    return t[p[1]] > p[3] if p[2] == '>' else t[p[1]] < p[3]


# Generate all single-feature predicates and find ones with high "loss density"
all_preds = []
for f in ALL_FEATURES:
    all_preds.extend(make_predicates(f))

# Single-feature predicates that catch losses with low winner contamination
print(f'Total candidate cuts: {len(all_preds)}')
single_seed = []
for p in all_preds:
    bl = sum(1 for t in remaining_l if apply_pred(t, p))
    bw = sum(1 for t in remaining_w if apply_pred(t, p))
    if bl >= 3 and bw <= 2:  # at least 3 losses, at most 2 winners
        single_seed.append((p, bl, bw))
single_seed.sort(key=lambda x: (-x[1], x[2]))
print(f'Top 30 candidate cuts (high loss capture, low winner contamination):')
for p, bl, bw in single_seed[:30]:
    print(f'  {p[0]:>40s}  losses={bl:>2d}  winners={bw}')

# Build 3-feature combinations using the top single-seed predicates
print()
print('Searching 3-feature pure-loss combinations (zero winners)...')
seeds = [p for p, _, _ in single_seed[:60]]  # top 60 seeds
triple_results = []
for combo in combinations(seeds, 3):
    feats = {p[1] for p in combo}
    if len(feats) < 3:
        continue
    bl = [t for t in remaining_l if all(apply_pred(t, p) for p in combo)]
    bw = [t for t in remaining_w if all(apply_pred(t, p) for p in combo)]
    if not bw and len(bl) >= 4:
        ls = -sum(t['pnl'] for t in bl)
        triple_results.append((list(combo), len(bl), ls, [t['mint'] for t in bl]))
triple_results.sort(key=lambda x: -x[2])
print(f'Found {len(triple_results)} pure 3-feature filters with >=4 losses caught')
for preds, lc, ls, mints in triple_results[:25]:
    labels = ' AND '.join(p[0] for p in preds)
    print(f'  saved={ls:+.5f} blocks {lc:>2d}L: {",".join(m[:8] for m in mints[:6])}')
    print(f'    => {labels}')

# Also check per-lane 2-feature combinations (per-lane more lenient)
print()
print('=' * 130)
print('PER-LANE 2-FEATURE PURE-LOSS SEARCH:')
print('=' * 130)
for lane in ['priced_snap', 'curve_lag_reveal']:
    lane_l = [t for t in remaining_l if t['lane'] == lane]
    lane_w = [t for t in remaining_w if t['lane'] == lane]
    print(f'\n--- Lane: {lane} ({len(lane_w)}W / {len(lane_l)}L remaining) ---')
    lane_preds = []
    for f in ALL_FEATURES:
        loss_vals = [t[f] for t in lane_l]
        win_vals = [t[f] for t in lane_w]
        cuts = sorted(set(percentiles(loss_vals, LEVELS) + percentiles(win_vals, LEVELS)))
        for c in cuts:
            lane_preds.append((f'{f}>{c:.4g}', f, '>', c))
            lane_preds.append((f'{f}<{c:.4g}', f, '<', c))
    pair_results = []
    seeds_lane = []
    for p in lane_preds:
        bl = sum(1 for t in lane_l if apply_pred(t, p))
        bw = sum(1 for t in lane_w if apply_pred(t, p))
        if bl >= 2 and bw <= 2:
            seeds_lane.append(p)
    for i, p1 in enumerate(seeds_lane):
        for p2 in seeds_lane[i+1:]:
            if p1[1] == p2[1]: continue
            bl = [t for t in lane_l if apply_pred(t, p1) and apply_pred(t, p2)]
            bw = [t for t in lane_w if apply_pred(t, p1) and apply_pred(t, p2)]
            if not bw and len(bl) >= 3:
                ls = -sum(t['pnl'] for t in bl)
                pair_results.append((p1, p2, len(bl), ls, [t['mint'] for t in bl]))
    pair_results.sort(key=lambda x: -x[3])
    for p1, p2, lc, ls, mints in pair_results[:15]:
        print(f'  saved={ls:+.5f} blocks {lc:>2d}L  [{p1[0]} AND {p2[0]}]')
        print(f'    mints: {",".join(m[:8] for m in mints)}')
