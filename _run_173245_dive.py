"""Deep analysis of run 173245: 7M2C (WIN), 73m2 (LOSS), 7rPC (LOSS).
Pull EVERY field for each trade and compare side-by-side."""
import json
PATH = '/root/piggy/data/pgg2_direct_live_20260507_173245_decisions.jsonl'
TARGETS = ['7M2C', '73m2', '7rPC']
records = {t: {} for t in TARGETS}
for line in open(PATH):
    if not line.strip(): continue
    try: x = json.loads(line)
    except: continue
    m = x.get('mint') or ''
    for t in TARGETS:
        if m.startswith(t):
            k = x.get('kind')
            records[t].setdefault(k, []).append(x)
            break

# Print all records sorted by ts
for t in TARGETS:
    print('=' * 130)
    print(f'TRADE: {t}')
    print('=' * 130)
    all_rec = []
    for k, items in records[t].items():
        for item in items:
            all_rec.append(item)
    all_rec.sort(key=lambda x: x.get('ts_ms', 0))
    for r in all_rec:
        kind = r.get('kind')
        ts = r.get('ts_ms')
        lane = r.get('lane') or ''
        rsn = r.get('reason') or ''
        pnl = r.get('pnl_sol')
        score = r.get('score')
        f = r.get('features') or {}
        print(f'\n  {kind} ts={ts} lane={lane} score={score} pnl={pnl}')
        print(f'    reason: {rsn}')
        # Print every feature
        for key in sorted(f.keys()):
            v = f[key]
            if isinstance(v, float):
                print(f'    {key}: {v:.6g}')
            elif isinstance(v, dict):
                print(f'    {key}: {v}')
            else:
                print(f'    {key}: {v}')
