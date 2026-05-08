"""Analytical validation: apply the buy700>=1.0 filter to ALL captured exec_spike_probe trades."""
import json, glob, os
RUNS = [
    '/root/piggy/data/experimentalji_direct_drylive_20260507_120204_decisions.jsonl',
    '/root/piggy/data/experimentalji_direct_live_20260507_132452_decisions.jsonl',
]
all_trades = []
for path in RUNS:
    fname = os.path.basename(path).replace('_decisions.jsonl','')
    if not os.path.exists(path):
        print(f'(missing: {path})')
        continue
    opens={}; closes_by_mint={}
    for line in open(path):
        if not line.strip(): continue
        try: x=json.loads(line)
        except: continue
        if x.get('kind')=='open': opens[x.get('mint')]=x
        elif x.get('kind')=='close': closes_by_mint[x.get('mint')]=x
    for m, op in opens.items():
        cl = closes_by_mint.get(m)
        if not cl: continue
        of = op.get('features') or {}
        # Identify EXEC-SPIKE-PROBE trades
        is_exec_spike = (
            of.get('raw_profile') == 'exec_spike_probe' or
            of.get('entry_size_reason') == 'exec_spike_probe' or
            'exec_spike' in (op.get('reason') or '')
        )
        if not is_exec_spike: continue
        pnl = float(cl.get('pnl_sol') or 0)
        all_trades.append({
            'run': fname,
            'mint': m[:8],
            'pnl': pnl,
            'is_win': pnl > 0,
            'buy700': float(of.get('buy700') or 0),
            'buy1500': float(of.get('buy1500') or 0),
            'uniq700': of.get('uniq700', 0),
            'top_share700': of.get('top_share700', 1.0),
            'cluster_score': of.get('cluster_score', 0),
            'first_buy_sol': of.get('first_buy_sol', 0),
            'reason': cl.get('reason'),
            'variant': of.get('raw_exec_variant', '?'),
        })

print('=' * 120)
print(f'TOTAL EXEC-SPIKE-PROBE trades found: {len(all_trades)}')
print('=' * 120)
print(f'{"run":42s} {"mint":10s}  {"buy700":>8s}  {"variant":>14s}  {"pnl":>10s}  {"reason":>22s}')
for t in sorted(all_trades, key=lambda x: -x['pnl']):
    print(f'{t["run"]:42s} {t["mint"]:10s}  {t["buy700"]:>8.4f}  {t["variant"]:>14s}  {t["pnl"]:>+10.6f}  {t["reason"]:>22s}')

# Apply the filter
print()
print('=' * 120)
print('FILTER VALIDATION: PGG2_EXEC_SPIKE_PROBE_MIN_BUY700_SOL = 1.0')
print('=' * 120)
def evaluate(threshold):
    kept = [t for t in all_trades if t['buy700'] >= threshold]
    skipped = [t for t in all_trades if t['buy700'] < threshold]
    kept_wins = [t for t in kept if t['is_win']]
    kept_losses = [t for t in kept if not t['is_win']]
    skipped_wins = [t for t in skipped if t['is_win']]
    skipped_losses = [t for t in skipped if not t['is_win']]
    kept_pnl = sum(t['pnl'] for t in kept)
    skipped_pnl = sum(t['pnl'] for t in skipped)
    return kept_wins, kept_losses, skipped_wins, skipped_losses, kept_pnl, skipped_pnl

for thresh in [0.0, 0.5, 0.7, 1.0, 1.2, 1.5]:
    kw, kl, sw, sl, kp, sp = evaluate(thresh)
    label = '(no filter)' if thresh == 0.0 else f'>={thresh}'
    blocked_loss_dollars = -sum(t['pnl'] for t in sl)
    sacrificed_win_dollars = sum(t['pnl'] for t in sw)
    print(f'  buy700 {label:>10s}: keeps {len(kw):>2d}W/{len(kl):>2d}L (net {kp:+.6f})  '
          f'skips {len(sw):>2d}W/{len(sl):>2d}L  '
          f'(blocks {blocked_loss_dollars:+.6f} SOL loss, sacrifices {sacrificed_win_dollars:+.6f} SOL wins)')

print()
print('=== With filter at 1.0 (the deployed value) ===')
kw, kl, sw, sl, kp, sp = evaluate(1.0)
print(f'KEPT trades (would still fire):')
for t in kw + kl:
    print(f'  {t["mint"]:10s} buy700={t["buy700"]:>7.4f} pnl={t["pnl"]:+.6f} {"WIN" if t["is_win"] else "LOSS"}')
print(f'BLOCKED trades (would be skipped):')
for t in sw + sl:
    print(f'  {t["mint"]:10s} buy700={t["buy700"]:>7.4f} pnl={t["pnl"]:+.6f} {"WIN" if t["is_win"] else "LOSS"}')
