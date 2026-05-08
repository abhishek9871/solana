"""For each live run, compute peak vsol per mint and identify moonshots
we observed but didn't enter. Find common features of those missed moonshots."""
import json, os, glob

RUNS = [
    'pgg2_direct_live_20260506_214938',
    'pgg2_direct_live_20260507_160325',
    'pgg2_direct_live_20260507_165911',
    'pgg2_direct_live_20260507_173245',
    'pgg2_direct_live_20260507_174602',
    'pgg2_direct_live_20260507_181635',
    'pgg2_direct_live_20260507_182140',
]

# For each run, find moonshots (mint that hit >=2x mult after first appearance)
# and check whether we entered them
total_missed_moonshots = []
total_entered = 0

for run in RUNS:
    raw_path = f'/root/piggy/data/{run}_raw.jsonl'
    dec_path = f'/root/piggy/data/{run}_decisions.jsonl'
    if not os.path.exists(raw_path):
        print(f'(missing: {run})'); continue

    # Get mints we entered
    entered_mints = set()
    armed_mints = set()
    for line in open(dec_path):
        if not line.strip(): continue
        try: x = json.loads(line)
        except: continue
        m = x.get('mint')
        if x.get('kind') == 'open':
            entered_mints.add(m)
        elif x.get('kind') == 'wave_arm':
            armed_mints.add(m)

    # Track each mint's price trajectory
    mint_trajectory = {}  # mint -> {'first_ts': ts, 'first_vsol': vsol, 'peak_vsol': v, 'first_price': price, 'peak_price': p, 'first_buy_count': n}
    line_count = 0
    for line in open(raw_path):
        if not line.strip(): continue
        line_count += 1
        if line_count % 50000 == 0:
            pass  # progress
        try: x = json.loads(line)
        except: continue
        if x.get('kind') != 'trade': continue
        m = x.get('mint')
        if not m: continue
        sol = float(x.get('sol') or 0)
        vsol = float(x.get('vsol_sol') or 0)
        ts = int(x.get('ts_ms') or 0)
        side = x.get('side')
        curve_price = float(x.get('curve_price') or 0)
        if m not in mint_trajectory:
            mint_trajectory[m] = {
                'first_ts': ts, 'first_vsol': vsol, 'first_price': curve_price,
                'peak_vsol': vsol, 'peak_price': curve_price,
                'buy_count': 0, 'buy_sol_total': 0.0,
            }
        rec = mint_trajectory[m]
        if curve_price > rec['peak_price']:
            rec['peak_price'] = curve_price
        if vsol > rec['peak_vsol']:
            rec['peak_vsol'] = vsol
        if side == 'buy':
            rec['buy_count'] += 1
            rec['buy_sol_total'] += sol

    # Compute mult per mint
    moonshots = []
    for m, rec in mint_trajectory.items():
        if rec['first_price'] <= 0:
            # Use vsol-based mult instead
            if rec['first_vsol'] > 0 and rec['peak_vsol'] / rec['first_vsol'] >= 2.0:
                moonshots.append((m, rec, 'vsol'))
            continue
        mult = rec['peak_price'] / rec['first_price']
        if mult >= 2.0:
            moonshots.append((m, rec, mult))

    # Categorize: were we even aware? did we enter?
    armed_count = 0; entered_count = 0; missed_count = 0
    missed_moonshots_run = []
    for m, rec, mult in moonshots:
        if m in entered_mints:
            entered_count += 1
        elif m in armed_mints:
            armed_count += 1
            missed_moonshots_run.append((m, rec, mult))
        else:
            missed_count += 1

    print(f'{run}:')
    print(f'  Total mints traded: {len(mint_trajectory)}')
    print(f'  Moonshots (>=2x): {len(moonshots)}')
    print(f'    Entered by us: {entered_count}')
    print(f'    Armed but not entered: {armed_count}')
    print(f'    Never armed (escaped scan): {missed_count}')
    if armed_count:
        print(f'  Top armed-but-missed moonshots:')
        for m, rec, mult in sorted(missed_moonshots_run, key=lambda x: -x[2] if isinstance(x[2], (int,float)) else 0)[:5]:
            mult_str = f'{mult:.2f}x' if isinstance(mult, (int,float)) else mult
            print(f'    {m[:8]} mult={mult_str} buys={rec["buy_count"]} buy_sol={rec["buy_sol_total"]:.2f} first_price={rec["first_price"]:.2e} peak={rec["peak_price"]:.2e}')
    total_missed_moonshots.extend(missed_moonshots_run)
    total_entered += entered_count
    print()

print(f'OVERALL: {total_entered} moonshots entered, {len(total_missed_moonshots)} armed but missed')
