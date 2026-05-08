"""Validate buy700 filter using LOG-derived exec_spike_probe identification."""
import json, re, glob, os

RUNS = [
    'experimentalji_direct_drylive_20260507_120204',
    'experimentalji_direct_live_20260507_132452',
]

all_spike_trades = []
for run in RUNS:
    log_path = f'/root/piggy/logs/{run}.log'
    dec_path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not (os.path.exists(log_path) and os.path.exists(dec_path)):
        continue
    # Find all mints that hit EXEC-SPIKE-PROBE in the log AND opened
    spike_mints = {}  # mint_short -> list of (buy, b1500, variant)
    rx = re.compile(r'PGG2-EXEC-SPIKE-PROBE (\S+) move=([\d.]+)x age=([\d.]+)s vsol=([\d.]+) buy=([\d.]+) b1500=([\d.]+)/(\d+) sellr=[\d.]+ variant=(\S+)')
    for line in open(log_path):
        m = rx.search(line)
        if not m: continue
        short = m.group(1).split('..')[0]
        spike_mints.setdefault(short, []).append({
            'move': float(m.group(2)),
            'buy_event_sol': float(m.group(5)),
            'b1500_sol': float(m.group(6)),
            'b1500_uniq': int(m.group(7)),
            'variant': m.group(8),
        })

    # Now find OPEN/CLOSE pairs for those mints
    opens = {}
    closes_by_mint = {}
    for line in open(dec_path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        m = x.get('mint','')
        if x.get('kind')=='open': opens[m] = x
        elif x.get('kind')=='close': closes_by_mint[m] = x
    # Cross-reference
    for mint, op in opens.items():
        short = mint[:8].split('..')[0]
        # Match on first 4 chars
        short4 = mint[:4]
        if short4 not in spike_mints: continue
        cl = closes_by_mint.get(mint)
        if not cl: continue
        of = op.get('features') or {}
        spike_data = spike_mints[short4][0]  # take first trigger info
        pnl = float(cl.get('pnl_sol') or 0)
        all_spike_trades.append({
            'run': run.replace('experimentalji_direct_','').replace('_20260507_120204','/dry').replace('_20260507_132452','/live'),
            'mint': mint[:8],
            'pnl': pnl,
            'is_win': pnl > 0,
            'buy700_at_open': float(of.get('buy700') or 0),
            'buy1500_at_open': float(of.get('buy1500') or 0),
            'uniq700_at_open': of.get('uniq700', 0),
            'cluster_score': float(of.get('cluster_score', 0)),
            'spike_buy_event_sol': spike_data['buy_event_sol'],
            'spike_b1500_sol': spike_data['b1500_sol'],
            'spike_b1500_uniq': spike_data['b1500_uniq'],
            'spike_move': spike_data['move'],
            'variant': spike_data['variant'],
            'reason': cl.get('reason'),
        })

print(f'Total EXEC-SPIKE-PROBE trades found across runs: {len(all_spike_trades)}')
if not all_spike_trades:
    print('NONE FOUND. Check regex / file paths.')
    exit()

print()
print(f'{"run":12s} {"mint":10s}  {"buy700":>8s}  {"variant":>14s}  {"cluster":>8s}  {"pnl":>10s}  {"reason":>22s}')
for t in sorted(all_spike_trades, key=lambda x: -x['pnl']):
    print(f'{t["run"]:12s} {t["mint"]:10s}  {t["buy700_at_open"]:>8.4f}  {t["variant"]:>14s}  {t["cluster_score"]:>+8.1f}  {t["pnl"]:>+10.6f}  {(t["reason"] or "")[:22]:>22s}')

print()
print('=== FILTER VALIDATION ===')
def evaluate(threshold):
    kept = [t for t in all_spike_trades if t['buy700_at_open'] >= threshold]
    skipped = [t for t in all_spike_trades if t['buy700_at_open'] < threshold]
    kept_w = sum(1 for t in kept if t['is_win'])
    kept_l = sum(1 for t in kept if not t['is_win'])
    skipped_w = sum(1 for t in skipped if t['is_win'])
    skipped_l = sum(1 for t in skipped if not t['is_win'])
    kept_pnl = sum(t['pnl'] for t in kept)
    sk_pnl = sum(t['pnl'] for t in skipped)
    return kept, skipped, kept_w, kept_l, skipped_w, skipped_l, kept_pnl, sk_pnl

print(f'{"threshold":>11s}  {"kept_W/L":>11s}  {"skipped_W/L":>13s}  {"kept_net":>10s}  {"avoided":>11s}  {"sacrificed_wins":>15s}')
for thresh in [0.0, 0.4, 0.5, 0.7, 1.0, 1.2, 1.5]:
    k, s, kw, kl, sw, sl, kpnl, spnl = evaluate(thresh)
    avoided = -sum(t['pnl'] for t in s if not t['is_win'])
    sacrificed = sum(t['pnl'] for t in s if t['is_win'])
    label = 'no filter' if thresh == 0.0 else f'>={thresh}'
    print(f'  {label:>9s}  {kw:>4d}/{kl:<5d}  {sw:>4d}/{sl:<7d}  {kpnl:>+10.6f}  {avoided:>+11.6f}  {sacrificed:>+15.6f}')

print()
print('=== With filter buy700 >= 1.0 (deployed) ===')
k, s, kw, kl, sw, sl, kpnl, spnl = evaluate(1.0)
print('KEPT (would still fire):')
for t in k:
    print(f'  {t["run"]:12s} {t["mint"]:10s}  buy700={t["buy700_at_open"]:>7.4f}  pnl={t["pnl"]:+.6f}  {"WIN" if t["is_win"] else "LOSS"}')
print('BLOCKED (would be skipped):')
for t in s:
    print(f'  {t["run"]:12s} {t["mint"]:10s}  buy700={t["buy700_at_open"]:>7.4f}  pnl={t["pnl"]:+.6f}  {"WIN" if t["is_win"] else "LOSS"}')
