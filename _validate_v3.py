"""Validate the new EXEC-SPIKE-PROBE conjunction filter against captured runs.

New filter rules (all must pass on top of buy700>=1.0):
  (uniq700>=2 OR buy700>=2.0)  AND  buy_sol_10s>=3.5  AND  cluster_score>=0
"""
import json, os

RUNS = [
    'experimentalji_direct_drylive_20260507_120204',
    'experimentalji_direct_live_20260507_132452',
    'experimentalji_direct_live_20260507_140643',
]

MIN_BUY700 = 1.0
MIN_UNIQ700 = 2
WHALE_BUY700 = 2.0
MIN_BUY_SOL_10S = 3.5
MIN_CLUSTER = 0.0


def passes_new(t):
    # The new conjunction only applies to the `standard` exec_spike_probe variant.
    # The `late_big_buy` variant has its own gate and is NOT affected by these env vars.
    if t['variant'] != 'standard':
        return True, 'pass (late_big_buy unaffected)'
    if t['buy700'] < MIN_BUY700:
        return False, 'buy700<1.0'
    if not (t['uniq700'] >= MIN_UNIQ700 or t['buy700'] >= WHALE_BUY700):
        return False, 'uniq700<2 AND buy700<2.0'
    if t['buy_sol_10s'] < MIN_BUY_SOL_10S:
        return False, f'buy_sol_10s={t["buy_sol_10s"]:.2f}<3.5 (stale)'
    if t['cluster_score'] < MIN_CLUSTER:
        return False, f'cluster={t["cluster_score"]:.1f}<0 (toxic)'
    return True, 'pass'


all_trades = []
for run in RUNS:
    dec_path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not os.path.exists(dec_path):
        print(f'(missing: {run})')
        continue

    strike_plans = {}     # mint -> latest strike_plan features
    opens = {}            # mint -> open record
    closes = {}           # mint -> close record

    for line in open(dec_path):
        if not line.strip():
            continue
        try:
            x = json.loads(line)
        except Exception:
            continue
        kind = x.get('kind')
        m = x.get('mint')
        if kind == 'strike_plan':
            strike_plans[m] = x
        elif kind == 'open':
            opens[m] = x
        elif kind == 'close':
            closes[m] = x

    for mint, op in opens.items():
        cl = closes.get(mint)
        if not cl:
            continue
        sp = strike_plans.get(mint, {})
        sf = sp.get('features') or {}
        of = op.get('features') or {}
        # Identify exec_spike_probe trades via strike_plan (canonical) or close reason
        is_spike = (
            sf.get('raw_profile') == 'exec_spike_probe'
            or sf.get('entry_size_reason') == 'exec_spike_probe'
            or 'exec_spike_probe' in (sp.get('reason') or '')
        )
        if not is_spike:
            continue
        pnl = float(cl.get('pnl_sol') or 0)
        all_trades.append({
            'run': run.replace('experimentalji_direct_', '').split('_2026')[0],
            'date': run.split('_')[-1],
            'mint': mint[:8],
            'pnl': pnl,
            'is_win': pnl > 0,
            'buy700': float(of.get('buy700') or sf.get('buy700') or 0),
            'uniq700': int(of.get('uniq700') or sf.get('uniq700') or 0),
            'top_share700': float(of.get('top_share700') or sf.get('top_share700') or 1.0),
            'cluster_score': float(of.get('cluster_score') or sf.get('cluster_score') or 0.0),
            'buy_sol_10s': float(sf.get('raw_buy_sol_10s') or 0),
            'reason': cl.get('reason') or '',
            'variant': sf.get('raw_exec_variant') or 'standard',
        })

print('=' * 140)
print(f'EXEC-SPIKE-PROBE trades found: {len(all_trades)}')
print('=' * 140)
print(f'{"run":>10s} {"date":>8s} {"mint":10s} {"variant":>10s} {"buy700":>7s} {"uniq":>4s} {"buy_10s":>8s} {"cluster":>8s} {"pnl":>10s}  {"verdict":>32s}  {"close_reason":>22s}')

kept_w = kept_l = blocked_w = blocked_l = 0
kept_w_pnl = kept_l_pnl = blocked_w_pnl = blocked_l_pnl = 0.0

for t in sorted(all_trades, key=lambda x: -x['pnl']):
    ok, why = passes_new(t)
    verdict = 'PASS' if ok else f'BLOCK: {why}'
    print(f'{t["run"]:>10s} {t["date"]:>8s} {t["mint"]:10s} {t["variant"]:>10s} {t["buy700"]:>7.3f} {t["uniq700"]:>4d} {t["buy_sol_10s"]:>8.3f} {t["cluster_score"]:>+8.1f} {t["pnl"]:>+10.6f}  {verdict:>32s}  {t["reason"][:22]:>22s}')
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
print('=' * 140)
print('NEW FILTER OUTCOME:')
print('=' * 140)
print(f'  KEPT     : {kept_w}W / {kept_l}L  (net {kept_w_pnl + kept_l_pnl:+.6f} SOL; wins {kept_w_pnl:+.6f}, losses {kept_l_pnl:+.6f})')
print(f'  BLOCKED  : {blocked_w}W / {blocked_l}L  (avoided losses {-blocked_l_pnl:+.6f}; sacrificed wins {blocked_w_pnl:+.6f})')
no_filter_total = sum(t['pnl'] for t in all_trades)
new_total = kept_w_pnl + kept_l_pnl
print(f'  Baseline (no filter) : {no_filter_total:+.6f} SOL across all {len(all_trades)} trades')
print(f'  With new filter      : {new_total:+.6f} SOL across {kept_w + kept_l} kept trades')
print(f'  Delta                : {new_total - no_filter_total:+.6f} SOL')

print()
print('=' * 140)
print("TODAY'S 3 LIVE TRADES (target cases):")
print('=' * 140)
target = [t for t in all_trades if t['date'] == '140643']
for t in sorted(target, key=lambda x: -x['pnl']):
    ok, why = passes_new(t)
    verdict = 'PASS (kept)' if ok else f'BLOCKED: {why}'
    label = 'WIN ' if t['is_win'] else 'LOSS'
    print(f'  {t["mint"]:10s} {label} pnl={t["pnl"]:+.6f}  buy700={t["buy700"]:>6.2f}  uniq700={t["uniq700"]:>2d}  buy_10s={t["buy_sol_10s"]:>6.2f}  cluster={t["cluster_score"]:>+7.1f}  ->  {verdict}')
