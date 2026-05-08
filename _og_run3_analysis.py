"""Detailed analysis of run 165911 (this evening's OG+7 run) + the prior 102.

Total dataset: 116 trades. Goal: find filters that catch the 7 new losses
(plus Cx3j -$0.61) without sacrificing any winner across all 116 trades.
"""
import json, os
from itertools import combinations

RUNS = [
    'pgg2_direct_live_20260506_214938',
    'pgg2_direct_live_20260507_160325',
    'pgg2_direct_live_20260507_165911',  # THIS evening's run
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
        sp = sps.get(mint, {}); sf = sp.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        # Comprehensive feature extraction
        b1500 = float(sf.get('buy1500') or 0)
        b700 = float(sf.get('buy700') or 0)
        s1500 = float(sf.get('sell1500') or 0)
        s700 = float(sf.get('sell700') or 0)
        u1500 = int(sf.get('uniq1500') or 0)
        u700 = int(sf.get('uniq700') or 0)
        top1500 = float(sf.get('top_share1500') or 1.0)
        top700 = float(sf.get('top_share700') or 1.0)
        hhi700 = float(sf.get('buyer_hhi700') or 1.0)
        first_buy = float(sf.get('first_buy_sol') or 0)
        slot = float(sf.get('slot_buy_sol') or 0)
        slot_buyers = int(sf.get('slot_buyers') or 0)
        slot_top = float(sf.get('slot_top_share') or 1.0)
        m250 = float(sf.get('move250') or 1.0)
        m700 = float(sf.get('move700') or 1.0)
        m1500 = float(sf.get('move1500') or 1.0)
        age = int(sf.get('age_ms') or 0)
        last_sell = int(sf.get('last_sell_age_ms') or 999999)
        cluster = float(sf.get('cluster_score') or 0)
        vsol = float(sf.get('vsol_sol') or 0.001)
        wave_age = int(sf.get('wave_arm_age_ms') or 0)
        score = float(sp.get('score') or 0)
        trades.append({
            'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0, 'lane': sp.get('lane') or '',
            'reason_close': cl.get('reason') or '',
            'run': run.split('_')[-1],
            'score': score,
            'buy700': b700, 'buy1500': b1500,
            'uniq700': u700, 'uniq1500': u1500,
            'top700': top700, 'top1500': top1500,
            'hhi700': hhi700,
            'sell700': s700, 'sell1500': s1500,
            'sell_pressure': s1500 / max(b1500, 0.001),
            'move250': m250, 'move700': m700, 'move1500': m1500,
            'age_ms': age, 'last_sell_age_ms': last_sell,
            'cluster_score': cluster, 'vsol': vsol,
            'first_buy': first_buy,
            'first_pct': first_buy / max(b1500, 0.001),
            'wave_arm_age_ms': wave_age,
            'slot_buy_sol': slot, 'slot_buyers': slot_buyers, 'slot_top': slot_top,
            'slot_to_15': slot / max(b1500, 0.001),
            # Derived
            'buy_accel': b700 - (b1500 - b700),  # 700ms vs preceding 800ms
            'top_trend': top700 - top1500,
            'uniq_trend': u700 - u1500,
            'move_trend': m700 - m1500,
            'recent_freshness': b700 / max(b1500, 0.001),
            'avg_buy_size_15': b1500 / max(u1500, 1),
            'avg_buy_size_7': b700 / max(u700, 1),
            'late_buyers': u1500 - u700,
            'late_buy_sol': b1500 - b700,
            'cluster_per_buy': cluster / max(b1500, 0.001),
            'score_per_buyer': score / max(u1500, 1),
            'score_per_buy_sol': score / max(b1500, 0.001),
            'first_to_700': first_buy / max(b700, 0.001),
            'mass_share': (slot + first_buy) / max(b1500, 0.001),  # how much is "anchor + slot"
        })


# Apply existing 7 deployed filters
def existing_blocks(t):
    if t['lane'] == 'curve_lag_reveal' and t['age_ms'] > 3000: return True
    if t['lane'] == 'priced_snap' and t['top700']>0.70 and t['buy700']<3.5 and t['hhi700']<0.99: return True
    if t['hhi700']<0.14 and t['first_buy']<2.5: return True
    if t['lane'] == 'priced_snap' and t['move700']<1.0: return True
    if t['lane'] == 'priced_snap' and t['move1500']<1.04: return True
    if t['lane'] == 'curve_lag_reveal' and t['buy1500']>15.2 and t['first_pct']<0.110: return True
    if t['lane'] == 'priced_snap' and t['score']>326.9 and t['move700']>1.653: return True
    return False


remaining = [t for t in trades if not existing_blocks(t)]
remaining_w = [t for t in remaining if t['is_win']]
remaining_l = [t for t in remaining if not t['is_win']]
_total_w = sum(1 for t in trades if t['is_win'])
_total_l = sum(1 for t in trades if not t['is_win'])
print(f'Total trades: {len(trades)} ({_total_w}W / {_total_l}L)')
print(f'After existing 7 filters: {len(remaining_w)}W / {len(remaining_l)}L remaining')
print()

# Print all REMAINING losses (the ones we still need to deal with)
print('=' * 140)
print('REMAINING LOSSES TO ELIMINATE:')
print('=' * 140)
print(f'{"mint":>10s} {"lane":>20s} {"pnl":>10s} {"buy700":>7s} {"u700":>4s} {"top":>4s} {"hhi":>4s} {"m700":>5s} {"m1500":>5s} {"age":>6s} {"clstr":>6s} {"first":>6s} {"slot_b":>6s} {"slot_top":>8s} {"score":>6s} {"reason":>30s}')
for t in sorted(remaining_l, key=lambda x: x['pnl']):
    print(f'{t["mint"]:>10s} {t["lane"][:20]:>20s} {t["pnl"]:>+10.5f} {t["buy700"]:>7.2f} {t["uniq700"]:>4d} {t["top700"]:>4.2f} {t["hhi700"]:>4.2f} {t["move700"]:>5.2f} {t["move1500"]:>5.2f} {t["age_ms"]:>6d} {t["cluster_score"]:>6.0f} {t["first_buy"]:>6.2f} {t["slot_buyers"]:>6d} {t["slot_top"]:>8.2f} {t["score"]:>6.1f} {t["reason_close"][:30]:>30s}')

# Print remaining WINNERS for reference
print()
print('=' * 140)
print('REMAINING WINNERS (must NOT be blocked):')
print('=' * 140)
print(f'{"mint":>10s} {"lane":>20s} {"pnl":>10s} {"buy700":>7s} {"u700":>4s} {"top":>4s} {"hhi":>4s} {"m700":>5s} {"m1500":>5s} {"age":>6s} {"clstr":>6s} {"first":>6s} {"slot_b":>6s} {"slot_top":>8s} {"score":>6s} {"reason":>30s}')
for t in sorted(remaining_w, key=lambda x: -x['pnl']):
    print(f'{t["mint"]:>10s} {t["lane"][:20]:>20s} {t["pnl"]:>+10.5f} {t["buy700"]:>7.2f} {t["uniq700"]:>4d} {t["top700"]:>4.2f} {t["hhi700"]:>4.2f} {t["move700"]:>5.2f} {t["move1500"]:>5.2f} {t["age_ms"]:>6d} {t["cluster_score"]:>6.0f} {t["first_buy"]:>6.2f} {t["slot_buyers"]:>6d} {t["slot_top"]:>8.2f} {t["score"]:>6.1f} {t["reason_close"][:30]:>30s}')

# Now do brute-force search for ANY pure-loss filter
print()
print('=' * 140)
print('PURE-LOSS FILTER SEARCH (single, double, triple feature) WITH FLOAT-SAFE THRESHOLDS:')
print('=' * 140)

ALL_FEATURES = [
    'score', 'buy700', 'buy1500', 'uniq700', 'uniq1500',
    'top700', 'top1500', 'hhi700',
    'sell700', 'sell1500', 'sell_pressure',
    'move250', 'move700', 'move1500',
    'age_ms', 'last_sell_age_ms', 'cluster_score', 'vsol', 'first_buy',
    'first_pct', 'first_to_700', 'wave_arm_age_ms',
    'slot_buy_sol', 'slot_buyers', 'slot_top', 'slot_to_15',
    'buy_accel', 'top_trend', 'uniq_trend', 'move_trend',
    'recent_freshness', 'avg_buy_size_15', 'avg_buy_size_7',
    'late_buyers', 'late_buy_sol', 'cluster_per_buy',
    'score_per_buyer', 'score_per_buy_sol', 'mass_share',
]

# Use SAFE thresholds: midpoint between adjacent winner/loser values (no ties)
def safe_thresholds(feat, min_loss_count=2):
    win_vals = sorted({t[feat] for t in remaining_w})
    loss_vals = sorted({t[feat] for t in remaining_l})
    cuts = set()
    # For each loss value, threshold = midpoint between it and nearest higher winner value (for `<` predicate)
    # and nearest lower winner value (for `>` predicate)
    sorted_w = sorted(set(win_vals))
    for lv in loss_vals:
        # For `<` cut: threshold should be > lv but < any winner that has same/lower value
        # Find smallest winner value > lv
        higher_w = [w for w in sorted_w if w > lv]
        if higher_w:
            mid = (lv + higher_w[0]) / 2
            cuts.add(('<', mid))
        # For `>` cut: threshold should be < lv but > any winner ≤ lv
        lower_w = [w for w in sorted_w if w < lv]
        if lower_w:
            mid = (lower_w[-1] + lv) / 2
            cuts.add(('>', mid))
    return cuts


def apply_pred(t, feat, op, c):
    return t[feat] > c if op == '>' else t[feat] < c


# Single feature pure-loss search
print('\n--- Single feature pure-loss filters (>=2 losses, 0 winners) ---')
single_results = []
for feat in ALL_FEATURES:
    for op, c in safe_thresholds(feat):
        bl = sum(1 for t in remaining_l if apply_pred(t, feat, op, c))
        bw = sum(1 for t in remaining_w if apply_pred(t, feat, op, c))
        if bw == 0 and bl >= 2:
            ls = -sum(t['pnl'] for t in remaining_l if apply_pred(t, feat, op, c))
            single_results.append((feat, op, c, bl, ls))
single_results.sort(key=lambda x: -x[4])
for f, op, c, bl, ls in single_results[:30]:
    print(f'  {f}{op}{c:.6g}  blocks {bl}L, saved {ls:+.5f} SOL')

# 2-feature pure-loss search (lane-aware)
print('\n--- 2-feature pure-loss filters (>=3 losses, 0 winners) ---')
double_results = []
seed_preds = []
for feat in ALL_FEATURES:
    for op, c in safe_thresholds(feat):
        bl = sum(1 for t in remaining_l if apply_pred(t, feat, op, c))
        bw = sum(1 for t in remaining_w if apply_pred(t, feat, op, c))
        if bl >= 2 and bw <= 4:
            seed_preds.append((feat, op, c, bl, bw))
seed_preds.sort(key=lambda x: (-x[3], x[4]))
seed_preds = seed_preds[:80]
for i, (f1, op1, c1, _, _) in enumerate(seed_preds):
    for (f2, op2, c2, _, _) in seed_preds[i+1:]:
        if f1 == f2: continue
        bl = [t for t in remaining_l if apply_pred(t, f1, op1, c1) and apply_pred(t, f2, op2, c2)]
        bw = [t for t in remaining_w if apply_pred(t, f1, op1, c1) and apply_pred(t, f2, op2, c2)]
        if not bw and len(bl) >= 3:
            ls = -sum(t['pnl'] for t in bl)
            double_results.append(((f1, op1, c1, f2, op2, c2), len(bl), ls, [t['mint'] for t in bl]))
double_results.sort(key=lambda x: -x[2])
for (f1, op1, c1, f2, op2, c2), lc, ls, mints in double_results[:25]:
    print(f'  saved={ls:+.5f}  blocks {lc}L  [{f1}{op1}{c1:.4g} AND {f2}{op2}{c2:.4g}]  mints={",".join(m[:8] for m in mints)}')

# Per-lane pure-loss search
print('\n--- Per-lane single-feature pure-loss filters ---')
for lane in ['priced_snap', 'curve_lag_reveal']:
    lane_l = [t for t in remaining_l if t['lane'] == lane]
    lane_w = [t for t in remaining_w if t['lane'] == lane]
    if not lane_l: continue
    print(f'\n  {lane} ({len(lane_w)}W / {len(lane_l)}L)')
    lane_single = []
    for feat in ALL_FEATURES:
        win_vals = sorted({t[feat] for t in lane_w})
        loss_vals = sorted({t[feat] for t in lane_l})
        for lv in loss_vals:
            higher_w = [w for w in win_vals if w > lv]
            if higher_w:
                mid = (lv + higher_w[0]) / 2
                bl = [t for t in lane_l if t[feat] < mid]
                bw = [t for t in lane_w if t[feat] < mid]
                if not bw and len(bl) >= 2:
                    ls = -sum(t['pnl'] for t in bl)
                    lane_single.append((feat, '<', mid, len(bl), ls, [t['mint'] for t in bl]))
            lower_w = [w for w in win_vals if w < lv]
            if lower_w:
                mid = (lower_w[-1] + lv) / 2
                bl = [t for t in lane_l if t[feat] > mid]
                bw = [t for t in lane_w if t[feat] > mid]
                if not bw and len(bl) >= 2:
                    ls = -sum(t['pnl'] for t in bl)
                    lane_single.append((feat, '>', mid, len(bl), ls, [t['mint'] for t in bl]))
    lane_single.sort(key=lambda x: -x[4])
    seen_keys = set()
    for f, op, c, bl, ls, mints in lane_single[:15]:
        k = (f, op, frozenset(mints))
        if k in seen_keys: continue
        seen_keys.add(k)
        print(f'    {f}{op}{c:.6g}  blocks {bl}L, saved {ls:+.5f}  mints={",".join(m[:8] for m in mints)}')
