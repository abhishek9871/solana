"""Inspect Phase 3 dry-live decision log for the wired-in markers."""
import json
import sys
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else '/root/piggy/data/pgg2_phase3_drylive_20260508_051250_decisions.jsonl'

opens = []
closes = []
strikes = []
for line in open(PATH):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    k = x.get('kind')
    if k == 'open':
        opens.append(x)
    elif k == 'close':
        closes.append(x)
    elif k == 'strike_plan':
        strikes.append(x)

print(f'strikes={len(strikes)} opens={len(opens)} closes={len(closes)}')
print()

print('=== CLOSE REASONS ===')
c = Counter()
for x in closes:
    c[x.get('reason', '?')] += 1
for r, n in c.most_common():
    print(f'  {r}: {n}')
print()

print('=== INDIVIDUAL CLOSES ===')
for x in closes:
    f = x.get('features') or {}
    pnl = float(x.get('pnl_sol') or 0)
    print(f"  mint={x.get('mint','')[:8]} reason={x.get('reason')} pnl={pnl:+.5f} last_mult={f.get('move250',0):.3f}")
print()

print('=== STRIKE PLAN AM SCALING ===')
seen_factors = Counter()
for x in strikes:
    f = x.get('features') or {}
    fac = f.get('anti_martingale_factor', 1.0)
    seen_factors[round(fac, 2)] += 1
print(f'  am_factor histogram: {dict(seen_factors)}')
print()

print('=== INDIVIDUAL STRIKES (last 8) ===')
for x in strikes[-8:]:
    f = x.get('features') or {}
    mint = x.get('mint', '')[:8]
    fac = f.get('anti_martingale_factor', 1.0)
    lab = f.get('anti_martingale_label', '')
    cl = f.get('consecutive_losses', 0)
    cw = f.get('consecutive_wins', 0)
    scout = x.get('scout_sol', 0)
    print(f"  {mint} am_factor={fac} label='{lab}' cl={cl} cw={cw} scout={scout:.4f}")
print()

print('=== PEAK-LOCK FIRINGS ===')
peak_lock_count = sum(1 for x in closes if 'peak_lock' in (x.get('reason') or ''))
print(f'  peak_lock close count: {peak_lock_count}')

print()
print('=== HARD_BREAK FIRINGS ===')
hard_break_count = sum(1 for x in closes if 'hard_break' in (x.get('reason') or ''))
print(f'  hard_break close count: {hard_break_count}')
for x in closes:
    if 'hard_break' in (x.get('reason') or ''):
        f = x.get('features') or {}
        print(f"  hard_break mint={x.get('mint','')[:8]} pnl={float(x.get('pnl_sol') or 0):+.5f} last_move250={f.get('move250',0):.3f}")
