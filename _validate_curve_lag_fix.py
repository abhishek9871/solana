"""Simulate the curve_lag_reveal fix vs no-filter baseline.
  Cap A: PGG2_CURVE_LAG_MAX_ENTRY_MOVE = 1.20 (was 1.25)
  Cap B: PGG2_CURVE_LAG_SOL = 0.025 (was 0.05)
"""
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
        emff = float(sf.get('entry_move_from_first') or of.get('entry_move_from_first') or 1.0)
        target = float(sp.get('target_sol') or 0)
        trades.append({'mint': m[:8], 'pnl': pnl, 'win': pnl > 0, 'entry_move': emff, 'target': target, 'fname': fname})

NEW_MAX_ENTRY_MOVE = 1.20
OLD_STAKE = 0.05
NEW_STAKE = 0.025
SCALE = NEW_STAKE / OLD_STAKE  # 0.5

# Baseline: no fix
base_trades = trades
base_w = [t for t in base_trades if t['win']]
base_l = [t for t in base_trades if not t['win']]
base_net = sum(t['pnl'] for t in base_trades)

# After max_entry_move filter only
filt_trades = [t for t in trades if t['entry_move'] <= NEW_MAX_ENTRY_MOVE]
blocked_by_filt = [t for t in trades if t['entry_move'] > NEW_MAX_ENTRY_MOVE]
filt_w = [t for t in filt_trades if t['win']]
filt_l = [t for t in filt_trades if not t['win']]
filt_net = sum(t['pnl'] for t in filt_trades)

# After max_entry_move filter + half stake (assume PnL scales linearly with stake)
# This is an approximation - actual slippage isn't perfectly linear, but for analytical
# comparison it's the cleanest model.
both_trades = []
for t in filt_trades:
    if t['target'] >= 0.04:  # only scale trades that were at full stake
        both_trades.append({**t, 'scaled_pnl': t['pnl'] * SCALE})
    else:
        both_trades.append({**t, 'scaled_pnl': t['pnl']})  # already small stake
both_w = [t for t in both_trades if t['scaled_pnl'] > 0]
both_l = [t for t in both_trades if t['scaled_pnl'] < 0]
both_net = sum(t['scaled_pnl'] for t in both_trades)

print('=' * 100)
print('CURVE_LAG_REVEAL FIX VALIDATION (160 historical trades)')
print('=' * 100)
print()
print(f'{"scenario":>40s} {"trades":>7s} {"W/L":>10s} {"net_SOL":>11s} {"max_loss":>10s} {"max_win":>10s}')
print('-' * 100)
print(f'{"BASELINE (no fix)":>40s} {len(base_trades):>7d} {len(base_w):>4d}/{len(base_l):<5d} {base_net:>+11.6f} {min(t["pnl"] for t in base_trades):>+10.6f} {max(t["pnl"] for t in base_trades):>+10.6f}')

print(f'{"+ entry_move<=1.20 only":>40s} {len(filt_trades):>7d} {len(filt_w):>4d}/{len(filt_l):<5d} {filt_net:>+11.6f} {min(t["pnl"] for t in filt_trades):>+10.6f} {max(t["pnl"] for t in filt_trades):>+10.6f}')

if both_l:
    max_loss_both = min(t['scaled_pnl'] for t in both_trades)
else:
    max_loss_both = 0.0
if both_w:
    max_win_both = max(t['scaled_pnl'] for t in both_trades)
else:
    max_win_both = 0.0
print(f'{"+ entry_move<=1.20 + stake 0.025":>40s} {len(both_trades):>7d} {len(both_w):>4d}/{len(both_l):<5d} {both_net:>+11.6f} {max_loss_both:>+10.6f} {max_win_both:>+10.6f}')

print()
print('=' * 100)
print(f'BLOCKED trades (entry_move>1.20): {len(blocked_by_filt)}')
for t in sorted(blocked_by_filt, key=lambda x: x['pnl']):
    label = 'WIN ' if t['win'] else 'LOSS'
    print(f'  {t["mint"]:>10s} {label} pnl={t["pnl"]:+.6f} entry_move={t["entry_move"]:.3f}')

print()
print('=' * 100)
print('CAPITAL IMPACT (assuming both fixes deployed):')
print('=' * 100)
print(f'  Baseline net (160 trades, 0.05 stake)              : {base_net:+.6f} SOL')
print(f'  After fix net (153 trades, 0.025 effective stake)  : {both_net:+.6f} SOL')
print(f'  Net change                                          : {both_net - base_net:+.6f} SOL')
print()
print(f'  Worst single loss baseline                          : {min(t["pnl"] for t in trades):+.6f} SOL')
print(f'  Worst single loss after fixes                       : {max_loss_both:+.6f} SOL')
print(f'  Loss-cap improvement                                : {max_loss_both - min(t["pnl"] for t in trades):+.6f} SOL')

# 56jN specific
print()
print('=' * 100)
print('TODAY 56jN trade (target case):')
print('=' * 100)
today = [t for t in trades if t['mint'] == '56jNFnhA']
for t in today:
    blocked = t['entry_move'] > NEW_MAX_ENTRY_MOVE
    new_pnl = t['pnl'] * SCALE if t['target'] >= 0.04 else t['pnl']
    if blocked:
        print(f'  56jN: BLOCKED by entry_move filter (entry_move={t["entry_move"]:.3f} > 1.20)')
    else:
        print(f'  56jN: KEPT (entry_move={t["entry_move"]:.3f}), but with stake 0.025 the loss would be {new_pnl:+.6f} SOL (was {t["pnl"]:+.6f})')
