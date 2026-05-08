"""Deep analysis of run 174602.
Wins: FV4W (+0.0056), EXM8 (+0.0013), HLNJ (+0.0102), 5bwp (+0.0052)
Losses: AjLV (-0.0004), 6rDY (-0.0002), 2RpC (-0.0005), 23eh (-0.0078)
"""
import json, os

PATH = '/root/piggy/data/pgg2_direct_live_20260507_174602_decisions.jsonl'
TARGETS = ['FV4W', 'AjLV', 'EXM8', 'HLNJ', '5bwp', '6rDY', '2RpC', '23eh']
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

# Print compact comparison
print(f'{"mint":>10s} {"R":>4s} {"score":>6s} {"buy7":>6s} {"buy15":>6s} {"b7/15":>6s} {"u7":>3s} {"u15":>3s} {"top7":>5s} {"hhi7":>5s} {"top15":>5s} {"sell15":>6s} {"first":>6s} {"slot":>5s} {"slot/15":>7s} {"age":>6s} {"clstr":>6s} {"vsol":>6s} {"m700":>5s} {"m1500":>5s} {"close":>30s} {"pnl":>10s}')
for t in TARGETS:
    sp = records[t].get('strike_plan', [{}])[0]
    cl_l = records[t].get('close', [])
    cl = cl_l[0] if cl_l else {}
    sf = sp.get('features') or {}
    pnl = float(cl.get('pnl_sol') or 0)
    label = 'WIN' if pnl > 0 else 'LOSS'
    b1500 = float(sf.get('buy1500') or 0)
    slot = float(sf.get('slot_buy_sol') or 0)
    print(f'{t:>10s} {label:>4s} {sp.get("score") or 0:>6.1f} {sf.get("buy700") or 0:>6.2f} {sf.get("buy1500") or 0:>6.2f} {(sf.get("buy700") or 0)/max(b1500,0.001):>6.3f} {sf.get("uniq700") or 0:>3d} {sf.get("uniq1500") or 0:>3d} {sf.get("top_share700") or 1.0:>5.2f} {sf.get("buyer_hhi700") or 1.0:>5.2f} {sf.get("top_share1500") or 1.0:>5.2f} {sf.get("sell1500") or 0:>6.3f} {sf.get("first_buy_sol") or 0:>6.2f} {sf.get("slot_buyers") or 0:>5d} {slot/max(b1500, 0.001):>7.3f} {sf.get("age_ms") or 0:>6d} {sf.get("cluster_score") or 0:>6.1f} {sf.get("vsol_sol") or 0:>6.1f} {sf.get("move700") or 1:>5.2f} {sf.get("move1500") or 1:>5.2f} {(cl.get("reason") or "")[:30]:>30s} {pnl:>+10.5f}')

# Print detailed features for 23eh specifically (the catastrophe)
print()
print('=' * 130)
print('23eh DETAILED:')
print('=' * 130)
sp = records['23eh'].get('strike_plan', [{}])[0]
sf = sp.get('features') or {}
for key in sorted(sf.keys()):
    v = sf[key]
    if isinstance(v, float):
        print(f'  {key}: {v:.6g}')
    elif isinstance(v, dict):
        print(f'  {key}: {v}')
    else:
        print(f'  {key}: {v}')
