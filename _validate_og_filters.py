"""Validate the 2 surgical filters against the 102 historical trades."""
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
        trades.append({
            'mint': mint[:8], 'pnl': pnl, 'is_win': pnl > 0,
            'lane': sp.get('lane') or '',
            'buy700': float(sf.get('buy700') or 0),
            'top700': float(sf.get('top_share700') or 1.0),
            'hhi700': float(sf.get('buyer_hhi700') or 1.0),
            'age_ms': int(sf.get('age_ms') or 0),
            'reason_close': cl.get('reason') or '',
        })

CURVE_LAG_MAX_AGE = 3000
PS_BLOCK_TOP700 = 0.70
PS_BLOCK_BUY700_BELOW = 3.5
PS_BLOCK_HHI700_BELOW = 0.99


def blocked(t):
    if t['lane'] == 'curve_lag_reveal' and t['age_ms'] > CURVE_LAG_MAX_AGE:
        return True, f'curve_lag age={t["age_ms"]}>3000'
    if (t['lane'] == 'priced_snap'
        and t['top700'] > PS_BLOCK_TOP700
        and t['buy700'] < PS_BLOCK_BUY700_BELOW
        and t['hhi700'] < PS_BLOCK_HHI700_BELOW):
        return True, f'priced_snap top={t["top700"]:.2f} buy={t["buy700"]:.2f} hhi={t["hhi700"]:.2f}'
    return False, 'pass'


kept_w = kept_l = blocked_w = blocked_l = 0
kept_w_pnl = kept_l_pnl = blocked_w_pnl = blocked_l_pnl = 0.0
blocked_trades_detail = []

for t in trades:
    is_blk, why = blocked(t)
    if is_blk:
        blocked_trades_detail.append((t, why))
        if t['is_win']:
            blocked_w += 1; blocked_w_pnl += t['pnl']
        else:
            blocked_l += 1; blocked_l_pnl += t['pnl']
    else:
        if t['is_win']:
            kept_w += 1; kept_w_pnl += t['pnl']
        else:
            kept_l += 1; kept_l_pnl += t['pnl']

print('=' * 100)
print(f'Total: {len(trades)} trades   blocked: {blocked_w + blocked_l}   kept: {kept_w + kept_l}')
print('=' * 100)
print(f'KEPT    : {kept_w}W / {kept_l}L  net {kept_w_pnl + kept_l_pnl:+.5f}  (wins {kept_w_pnl:+.5f}, losses {kept_l_pnl:+.5f})')
print(f'BLOCKED : {blocked_w}W / {blocked_l}L  pnl_lost {blocked_w_pnl:+.5f}  pnl_saved {-blocked_l_pnl:+.5f}')
no_filter = sum(t['pnl'] for t in trades)
new_total = kept_w_pnl + kept_l_pnl
print(f'Baseline (no filter): {no_filter:+.5f} SOL across {len(trades)} trades')
print(f'New filter total    : {new_total:+.5f} SOL across {kept_w + kept_l} kept trades')
print(f'Delta               : {new_total - no_filter:+.5f} SOL')
print()
print('BLOCKED TRADES:')
for t, why in sorted(blocked_trades_detail, key=lambda kv: kv[0]['pnl']):
    label = 'WIN ' if t['is_win'] else 'LOSS'
    print(f'  {t["mint"]:>10s} {label} pnl={t["pnl"]:+.5f}  [{why}]  reason_close={t["reason_close"][:30]}')
