"""Pull every curve_lag_reveal trade's discriminating features and pnl."""
import json, os, glob

trades = []
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
        k = x.get('kind')
        m = x.get('mint')
        lane = x.get('lane') or ''
        if k == 'strike_plan' and lane == 'curve_lag_reveal':
            sps[m] = x
        elif k == 'open' and lane == 'curve_lag_reveal':
            opens[m] = x
        elif k == 'close':
            closes[m] = x
    for m, op in opens.items():
        cl = closes.get(m)
        if not cl:
            continue
        sp = sps.get(m, {})
        sf = sp.get('features') or {}
        of = op.get('features') or {}
        pnl = float(cl.get('pnl_sol') or 0)
        # Pull key features (some may be missing in older runs)
        emff = float(sf.get('entry_move_from_first') or of.get('entry_move_from_first') or 0)
        cl_buy700 = float(sf.get('curve_lag_live_buy700') or of.get('curve_lag_live_buy700') or 0)
        cl_uniq700 = int(sf.get('curve_lag_live_unique700') or of.get('curve_lag_live_unique700') or 0)
        cl_top700 = float(sf.get('curve_lag_live_top700') or of.get('curve_lag_live_top700') or 0)
        follow = sf.get('curve_lag_follow') or of.get('curve_lag_follow') or {}
        follow_buy = float(follow.get('buy_sol') or 0)
        follow_uniq = int(follow.get('unique_buyers') or 0)
        follow_top = float(follow.get('top_buy_share') or 0)
        score = float(sp.get('score') or 0)
        target = float(sp.get('target_sol') or 0)
        cluster = float(sf.get('cluster_score') or of.get('cluster_score') or 0)
        vsol = float(sf.get('vsol_sol') or of.get('vsol_sol') or 0)
        hhi = float(sf.get('buyer_hhi700') or of.get('buyer_hhi700') or 0)
        trades.append({
            'run': fname,
            'mint': m[:8],
            'pnl': pnl,
            'win': pnl > 0,
            'entry_move': emff,
            'cl_buy700': cl_buy700,
            'cl_uniq700': cl_uniq700,
            'cl_top700': cl_top700,
            'follow_buy': follow_buy,
            'follow_uniq': follow_uniq,
            'follow_top': follow_top,
            'score': score,
            'target': target,
            'vsol': vsol,
            'hhi700': hhi,
            'reason': (cl.get('reason') or '')[:25],
        })

trades.sort(key=lambda t: -t['pnl'])
print(f'Total curve_lag_reveal trades: {len(trades)}')
print()

# Show data with entry_move available
have_emff = [t for t in trades if t['entry_move'] > 0]
print(f'Trades with entry_move_from_first feature recorded: {len(have_emff)}')
print()

# Print all trades with key features
print(f'{"run":>40s} {"mint":>10s} {"target":>6s} {"pnl":>10s} {"entry_mv":>9s} {"cl_b700":>8s} {"cl_u700":>7s} {"follow":>10s} {"vsol":>6s} {"hhi":>5s} {"score":>6s}  reason')
for t in have_emff[:60]:
    print(f'{t["run"][:40]:>40s} {t["mint"]:>10s} {t["target"]:>6.4f} {t["pnl"]:>+10.6f} {t["entry_move"]:>9.4f} {t["cl_buy700"]:>8.2f} {t["cl_uniq700"]:>7d} {t["follow_buy"]:>5.2f}/{t["follow_uniq"]:>2d}({t["follow_top"]:.2f}) {t["vsol"]:>6.1f} {t["hhi700"]:>5.3f} {t["score"]:>6.1f}  {t["reason"]}')

# Group by entry_move ranges
print()
print('=' * 120)
print('Win rate by entry_move_from_first bands (only trades with feature recorded):')
print('=' * 120)
bands = [(0.99, 1.001, 'flat (<=1.0)'), (1.001, 1.05, '1.0-1.05'), (1.05, 1.10, '1.05-1.10'), (1.10, 1.20, '1.10-1.20'), (1.20, 999, '>=1.20')]
for lo, hi, label in bands:
    in_band = [t for t in have_emff if lo <= t['entry_move'] < hi]
    wins = [t for t in in_band if t['win']]
    losses = [t for t in in_band if not t['win']]
    total_pnl = sum(t['pnl'] for t in in_band)
    win_pnl = sum(t['pnl'] for t in wins)
    loss_pnl = sum(t['pnl'] for t in losses)
    if in_band:
        print(f'  {label:>16s}: {len(wins):>3d}W / {len(losses):>3d}L  ({len(wins)*100//max(1,len(in_band))}%)  net {total_pnl:+.6f}  wins {win_pnl:+.6f}  losses {loss_pnl:+.6f}  ({len(in_band)} trades)')

# All loss-by-loss breakdown
print()
print('=' * 120)
print('ALL LOSSES with entry_move feature (sorted by worst):')
print('=' * 120)
losses = [t for t in have_emff if not t['win']]
losses.sort(key=lambda t: t['pnl'])
for t in losses:
    print(f'  {t["mint"]:>10s} pnl={t["pnl"]:+.6f}  entry_move={t["entry_move"]:>5.3f}  cl_b700={t["cl_buy700"]:>6.2f}  cl_u700={t["cl_uniq700"]:>3d}  follow_buy={t["follow_buy"]:>5.2f}  score={t["score"]:>5.1f}')

# Today's loss
print()
print('=' * 120)
print('TODAY 56jN target case:')
print('=' * 120)
today = [t for t in trades if t['mint'].startswith('56jN')]
for t in today:
    print(f'  {t["mint"]} pnl={t["pnl"]:+.6f} entry_move={t["entry_move"]:.3f} cl_buy700={t["cl_buy700"]:.2f} cl_uniq700={t["cl_uniq700"]} follow_buy={t["follow_buy"]:.2f} target_sol={t["target"]:.4f}')
