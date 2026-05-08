"""Validate the new exec_spike_probe filter (with hhi700 + top700 conjunction)
against ALL historical exec_spike_probe trades across all runs."""
import json, os, glob

MIN_BUY700 = 1.0
MIN_UNIQ700 = 2
WHALE_BUY700 = 2.0
MIN_BUY_SOL_10S = 3.5
MIN_CLUSTER = 0.0
MAX_HHI700 = 0.35  # NEW
MAX_TOP_SHARE700 = 0.40  # NEW


def passes_new(t):
    if t['variant'] != 'standard':
        return True, 'pass (late_big_buy unaffected)'
    if t['buy700'] < MIN_BUY700:
        return False, 'buy700<1.0'
    if not (t['uniq700'] >= MIN_UNIQ700 or t['buy700'] >= WHALE_BUY700):
        return False, 'uniq700<2 AND buy700<2.0'
    if t['buy_sol_10s'] < MIN_BUY_SOL_10S:
        return False, f'buy_sol_10s={t["buy_sol_10s"]:.2f}<3.5'
    if t['cluster_score'] < MIN_CLUSTER:
        return False, f'cluster<0'
    if t['hhi700'] >= MAX_HHI700:
        return False, f'hhi700={t["hhi700"]:.3f}>=0.35 (whale-dominated)'
    if t['top_share700'] >= MAX_TOP_SHARE700:
        return False, f'top_share700={t["top_share700"]:.3f}>=0.40'
    return True, 'pass'


all_trades = []
for path in sorted(glob.glob('/root/piggy/data/*_decisions.jsonl')):
    fname = os.path.basename(path).replace('_decisions.jsonl', '')
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
        kind = x.get('kind')
        m = x.get('mint')
        if kind == 'strike_plan':
            # for exec_spike_probe (raw_momentum lane in latest, plus raw_momentum in older)
            sps[m] = x
        elif kind == 'open':
            opens[m] = x
        elif kind == 'close':
            closes[m] = x

    for mint, op in opens.items():
        cl = closes.get(mint)
        if not cl:
            continue
        sp = sps.get(mint, {})
        sf = sp.get('features') or {}
        of = op.get('features') or {}
        is_spike = (
            sf.get('raw_profile') == 'exec_spike_probe'
            or sf.get('entry_size_reason') == 'exec_spike_probe'
            or 'exec_spike_probe' in (sp.get('reason') or '')
        )
        if not is_spike:
            continue
        pnl = float(cl.get('pnl_sol') or 0)
        all_trades.append({
            'run': fname[:30],
            'date': fname.split('_')[-1] if '_' in fname else '',
            'mint': mint[:8],
            'pnl': pnl,
            'is_win': pnl > 0,
            'buy700': float(of.get('buy700') or sf.get('buy700') or 0),
            'uniq700': int(of.get('uniq700') or sf.get('uniq700') or 0),
            'top_share700': float(of.get('top_share700') or sf.get('top_share700') or 1.0),
            'hhi700': float(of.get('buyer_hhi700') or sf.get('buyer_hhi700') or 1.0),
            'cluster_score': float(of.get('cluster_score') or sf.get('cluster_score') or 0.0),
            'buy_sol_10s': float(sf.get('raw_buy_sol_10s') or 0),
            'reason': cl.get('reason') or '',
            'variant': sf.get('raw_exec_variant') or 'standard',
        })

print(f'Total exec_spike_probe trades: {len(all_trades)}')
print()

kept_w = kept_l = blocked_w = blocked_l = 0
kept_w_pnl = kept_l_pnl = blocked_w_pnl = blocked_l_pnl = 0.0

print(f'{"date":>8s} {"mint":>10s} {"variant":>10s} {"buy700":>7s} {"uniq":>4s} {"hhi700":>7s} {"top700":>7s} {"buy_10s":>8s} {"pnl":>10s}  {"verdict":>40s}')
for t in sorted(all_trades, key=lambda x: -x['pnl']):
    ok, why = passes_new(t)
    verdict = 'PASS' if ok else f'BLOCK: {why}'
    print(f'{t["date"]:>8s} {t["mint"]:>10s} {t["variant"]:>10s} {t["buy700"]:>7.3f} {t["uniq700"]:>4d} {t["hhi700"]:>7.3f} {t["top_share700"]:>7.3f} {t["buy_sol_10s"]:>8.3f} {t["pnl"]:>+10.6f}  {verdict[:40]:>40s}')
    if ok:
        if t['is_win']:
            kept_w += 1; kept_w_pnl += t['pnl']
        else:
            kept_l += 1; kept_l_pnl += t['pnl']
    else:
        if t['is_win']:
            blocked_w += 1; blocked_w_pnl += t['pnl']
        else:
            blocked_l += 1; blocked_l_pnl += t['pnl']

print()
print('=' * 130)
print('SUMMARY')
print('=' * 130)
print(f'  KEPT     : {kept_w}W / {kept_l}L  net {kept_w_pnl + kept_l_pnl:+.6f}  (wins {kept_w_pnl:+.6f}, losses {kept_l_pnl:+.6f})')
print(f'  BLOCKED  : {blocked_w}W / {blocked_l}L  avoided losses {-blocked_l_pnl:+.6f}, sacrificed wins {blocked_w_pnl:+.6f}')
no_filter = sum(t['pnl'] for t in all_trades)
new_total = kept_w_pnl + kept_l_pnl
print(f'  Baseline (no filter)              : {no_filter:+.6f} SOL across {len(all_trades)} trades')
print(f'  With new filter                   : {new_total:+.6f} SOL across {kept_w + kept_l} kept trades')
print(f'  Net improvement                   : {new_total - no_filter:+.6f} SOL')

print()
print('=' * 130)
print("TODAY'S RUN 150006 TARGET TRADES:")
print('=' * 130)
target = [t for t in all_trades if t['date'] == '150006']
for t in sorted(target, key=lambda x: -x['pnl']):
    ok, why = passes_new(t)
    verdict = 'PASS (kept)' if ok else f'BLOCKED: {why}'
    label = 'WIN ' if t['is_win'] else 'LOSS'
    print(f'  {t["mint"]:>10s} {label} pnl={t["pnl"]:+.6f}  hhi700={t["hhi700"]:.3f}  top700={t["top_share700"]:.3f}  ->  {verdict}')
