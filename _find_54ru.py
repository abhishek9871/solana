"""Find 54ru..pump features and any other exec_spike winners to verify the filter."""
import json, glob, os
target_short = '54ru'
print('=== Searching for trades involving 54ru..pump ===')
for path in sorted(glob.glob('/root/piggy/data/*decisions.jsonl')):
    fname = path.split('/')[-1]
    found = False
    for line in open(path):
        if target_short not in line: continue
        try: x = json.loads(line)
        except: continue
        m = x.get('mint','')
        if not m.startswith(target_short): continue
        if x.get('kind') not in ('open','close'): continue
        if not found:
            print(f'\n--- {fname} ---')
            found = True
        f = x.get('features') or {}
        kind = x.get('kind')
        reason = x.get('reason')
        pnl = x.get('pnl_sol')
        lane = x.get('lane')
        print(f'  kind={kind}  reason={reason}  pnl={pnl}  lane={lane}')
        if kind == 'open':
            for k in ['raw_entry_move','raw_unique_buyers_10s','raw_top_share_10s',
                      'uniq700','top_share700','cluster_score','first_buy_sol',
                      'vsol_sol','buy700','raw_profile','raw_exec_variant',
                      'raw_buy_sol_10s','raw_arm_age_ms']:
                if k in f: print(f'    {k}: {f[k]}')

# Also find ALL exec_spike_probe trades (raw_profile = exec_spike_probe) and show winners vs losers
print()
print('=== ALL exec_spike_probe trades across all runs ===')
all_trades = []
for path in sorted(glob.glob('/root/piggy/data/*decisions.jsonl')):
    fname = path.split('/')[-1]
    opens_in_run = {}
    for line in open(path):
        try: x = json.loads(line)
        except: continue
        if x.get('kind') == 'open':
            opens_in_run[x.get('mint')] = x
        elif x.get('kind') == 'close':
            m = x.get('mint')
            op = opens_in_run.get(m)
            if not op: continue
            of = op.get('features') or {}
            if of.get('raw_profile') != 'exec_spike_probe' and 'exec_spike' not in (op.get('reason') or ''):
                continue
            pnl = float(x.get('pnl_sol') or 0)
            all_trades.append({
                'run': fname,
                'mint': m[:8],
                'pnl': pnl,
                'uniq700': of.get('uniq700', 0),
                'top_share700': of.get('top_share700', 0),
                'cluster_score': of.get('cluster_score', 0),
                'raw_unique_buyers_10s': of.get('raw_unique_buyers_10s', 0),
                'raw_top_share_10s': of.get('raw_top_share_10s', 0),
                'raw_entry_move': of.get('raw_entry_move', 0),
                'first_buy_sol': of.get('first_buy_sol', 0),
                'reason': x.get('reason'),
            })

print(f'\nTotal exec_spike_probe-related trades found: {len(all_trades)}')
wins = [t for t in all_trades if t['pnl'] > 0]
losses = [t for t in all_trades if t['pnl'] < 0]
print(f'WINS: {len(wins)}  LOSSES: {len(losses)}')
print()
print(f'{"mint":10s}  {"pnl":>10s}  {"uniq700":>8s}  {"top_700":>8s}  {"cluster":>8s}  {"raw_uniq10":>10s}  {"raw_top10":>10s}  {"move":>6s}  {"first_buy":>10s}  reason')
for t in sorted(all_trades, key=lambda x: -x['pnl']):
    print(f'{t["mint"]:10s}  {t["pnl"]:>+10.6f}  {t["uniq700"]:>8}  {t["top_share700"]:>8.3f}  {t["cluster_score"]:>+8.1f}  {t["raw_unique_buyers_10s"]:>10}  {t["raw_top_share_10s"]:>10.3f}  {t["raw_entry_move"]:>6.2f}  {t["first_buy_sol"]:>10.4f}  {t["reason"]}')

# Filter analysis
print()
print('=== FILTER ANALYSIS ===')
def test_filter(name, predicate):
    kept = [t for t in all_trades if predicate(t)]
    skipped = [t for t in all_trades if not predicate(t)]
    kept_pnl = sum(t['pnl'] for t in kept)
    skipped_pnl = sum(t['pnl'] for t in skipped)
    kept_w = sum(1 for t in kept if t['pnl'] > 0)
    kept_l = sum(1 for t in kept if t['pnl'] < 0)
    print(f'{name:50s} kept {len(kept)} ({kept_w}W/{kept_l}L, net {kept_pnl:+.6f})  skipped {len(skipped)} (would have lost {skipped_pnl:+.6f})')

test_filter('NO FILTER (current)', lambda t: True)
test_filter('uniq700 >= 2', lambda t: (t['uniq700'] or 0) >= 2)
test_filter('top_share700 <= 0.95', lambda t: (t['top_share700'] or 0) <= 0.95)
test_filter('cluster_score >= 0', lambda t: (t['cluster_score'] or 0) >= 0)
test_filter('uniq700 >= 2 OR raw_unique_buyers_10s >= 2', lambda t: (t['uniq700'] or 0) >= 2 or (t['raw_unique_buyers_10s'] or 0) >= 2)
test_filter('cluster_score > -10', lambda t: (t['cluster_score'] or 0) > -10)
test_filter('top_share700 <= 0.95 AND cluster_score > -10', lambda t: (t['top_share700'] or 0) <= 0.95 and (t['cluster_score'] or 0) > -10)
