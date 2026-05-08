"""For each missed moonshot, analyze what its EARLY tape looked like.
Find common features so we can adjust scanner to catch them."""
import json, os
from collections import defaultdict

RUNS = [
    'pgg2_direct_live_20260506_214938',
    'pgg2_direct_live_20260507_160325',
    'pgg2_direct_live_20260507_165911',
    'pgg2_direct_live_20260507_173245',
    'pgg2_direct_live_20260507_174602',
    'pgg2_direct_live_20260507_181635',
    'pgg2_direct_live_20260507_182140',
]

# For each run, build mint trajectories
all_missed = []
all_armed_but_missed = []
all_entered = []

for run in RUNS:
    raw_path = f'/root/piggy/data/{run}_raw.jsonl'
    dec_path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not os.path.exists(raw_path): continue

    # Track entered/armed
    entered_mints = set()
    armed_mints = set()
    for line in open(dec_path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        m = x.get('mint')
        if x.get('kind') == 'open': entered_mints.add(m)
        elif x.get('kind') == 'wave_arm': armed_mints.add(m)

    # Build mint trajectories (more complete)
    mint_data = defaultdict(lambda: {
        'first_ts': None, 'first_price': 0.0, 'peak_price': 0.0,
        'first_buyer': None, 'first_buy_sol': 0.0, 'first_buy_ts': None,
        'buy_count_2s': 0, 'buy_sol_2s': 0.0, 'unique_buyers_2s': set(),
        'buy_count_5s': 0, 'buy_sol_5s': 0.0, 'unique_buyers_5s': set(),
        'buy_count_total': 0, 'buy_sol_total': 0.0,
        'peak_vsol': 0.0, 'final_vsol': 0.0,
    })
    for line in open(raw_path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        if x.get('kind') != 'trade': continue
        m = x.get('mint')
        if not m: continue
        sol = float(x.get('sol') or 0)
        ts = int(x.get('ts_ms') or 0)
        side = x.get('side')
        cp = float(x.get('curve_price') or 0)
        vs = float(x.get('vsol_sol') or 0)
        user = x.get('user') or ''

        rec = mint_data[m]
        if rec['first_ts'] is None:
            rec['first_ts'] = ts
            rec['first_price'] = cp
        if cp > rec['peak_price']:
            rec['peak_price'] = cp
        if vs > rec['peak_vsol']:
            rec['peak_vsol'] = vs
        rec['final_vsol'] = vs
        if side == 'buy':
            if rec['first_buyer'] is None:
                rec['first_buyer'] = user
                rec['first_buy_sol'] = sol
                rec['first_buy_ts'] = ts
            rec['buy_count_total'] += 1
            rec['buy_sol_total'] += sol
            age_ms = ts - rec['first_ts']
            if age_ms <= 2000:
                rec['buy_count_2s'] += 1
                rec['buy_sol_2s'] += sol
                rec['unique_buyers_2s'].add(user)
            if age_ms <= 5000:
                rec['buy_count_5s'] += 1
                rec['buy_sol_5s'] += sol
                rec['unique_buyers_5s'].add(user)

    # Find moonshots
    for m, rec in mint_data.items():
        if rec['first_price'] <= 0: continue
        mult = rec['peak_price'] / rec['first_price']
        if mult < 2.0: continue
        info = {
            'run': run, 'mint': m[:10], 'mult': mult,
            'first_price': rec['first_price'], 'peak_price': rec['peak_price'],
            'first_buy_sol': rec['first_buy_sol'],
            'buy_count_2s': rec['buy_count_2s'], 'buy_sol_2s': rec['buy_sol_2s'],
            'unique_buyers_2s': len(rec['unique_buyers_2s']),
            'buy_count_5s': rec['buy_count_5s'], 'buy_sol_5s': rec['buy_sol_5s'],
            'unique_buyers_5s': len(rec['unique_buyers_5s']),
            'buy_count_total': rec['buy_count_total'], 'buy_sol_total': rec['buy_sol_total'],
            'peak_vsol': rec['peak_vsol'], 'final_vsol': rec['final_vsol'],
        }
        if m in entered_mints:
            all_entered.append(info)
        elif m in armed_mints:
            all_armed_but_missed.append(info)
        else:
            all_missed.append(info)

print(f'Total entered moonshots: {len(all_entered)}')
print(f'Total armed-but-missed moonshots: {len(all_armed_but_missed)}')
print(f'Total never-armed moonshots: {len(all_missed)}')
print()

print('=' * 130)
print('NEVER ARMED MOONSHOTS (sorted by mult):')
print('=' * 130)
print(f'{"mint":>10s} {"mult":>6s} {"first_buy":>9s} {"buys2s":>7s} {"sol2s":>6s} {"u2s":>4s} {"buys5s":>7s} {"sol5s":>6s} {"u5s":>4s} {"total_sol":>9s} {"vsol_pk":>7s}')
for info in sorted(all_missed, key=lambda x: -x['mult']):
    print(f'{info["mint"]:>10s} {info["mult"]:>6.2f}x {info["first_buy_sol"]:>9.2f} {info["buy_count_2s"]:>7d} {info["buy_sol_2s"]:>6.2f} {info["unique_buyers_2s"]:>4d} {info["buy_count_5s"]:>7d} {info["buy_sol_5s"]:>6.2f} {info["unique_buyers_5s"]:>4d} {info["buy_sol_total"]:>9.2f} {info["peak_vsol"]:>7.2f}')

print()
print('=' * 130)
print('ARMED BUT NOT STRUCK MOONSHOTS:')
print('=' * 130)
for info in sorted(all_armed_but_missed, key=lambda x: -x['mult']):
    print(f'{info["mint"]:>10s} {info["mult"]:>6.2f}x first_buy={info["first_buy_sol"]:.2f} 2s_buys={info["buy_count_2s"]} 2s_sol={info["buy_sol_2s"]:.2f} 2s_uniq={info["unique_buyers_2s"]} 5s_buys={info["buy_count_5s"]} 5s_sol={info["buy_sol_5s"]:.2f} 5s_uniq={info["unique_buyers_5s"]} run={info["run"][-15:]}')

print()
print('=' * 130)
print('ENTERED MOONSHOTS (for comparison):')
print('=' * 130)
for info in sorted(all_entered, key=lambda x: -x['mult']):
    print(f'{info["mint"]:>10s} {info["mult"]:>6.2f}x first_buy={info["first_buy_sol"]:.2f} 2s_buys={info["buy_count_2s"]} 2s_sol={info["buy_sol_2s"]:.2f} 2s_uniq={info["unique_buyers_2s"]} 5s_buys={info["buy_count_5s"]} 5s_sol={info["buy_sol_5s"]:.2f} 5s_uniq={info["unique_buyers_5s"]} run={info["run"][-15:]}')
