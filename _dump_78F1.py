import json
PATH = '/root/piggy/data/pgg2_direct_live_20260506_214938_decisions.jsonl'
for line in open(PATH):
    if '78F1fwpX' not in line: continue
    x = json.loads(line)
    k = x.get('kind')
    f = x.get('features') or {}
    if k in ('wave_arm', 'strike_plan', 'strike_skipped', 'open'):
        lane = x.get('lane') or ''
        rsn = x.get('reason') or ''
        print(f'=== {k} lane={lane} ts={x.get("ts_ms")}')
        print(f'  reason: {rsn}')
        print(f'  age_ms={f.get("age_ms")} price={f.get("price")}')
        print(f'  buy700={f.get("buy700")} buy1500={f.get("buy1500")}')
        print(f'  uniq700={f.get("uniq700")} uniq1500={f.get("uniq1500")}')
        print(f'  top700={f.get("top_share700")} top1500={f.get("top_share1500")}')
        print(f'  hhi700={f.get("buyer_hhi700")}')
        print(f'  sell1500={f.get("sell1500")} first_buy={f.get("first_buy_sol")}')
        print(f'  has_curve={f.get("has_curve")} wave_armed={f.get("wave_armed")} arm_age={f.get("wave_arm_age_ms")}')
        print(f'  slot_buyers={f.get("slot_buyers")} slot_buy_sol={f.get("slot_buy_sol")} slot_top={f.get("slot_top_share")}')
        print(f'  cluster={f.get("cluster_score")} vsol={f.get("vsol_sol")}')
        print(f'  move250={f.get("move250")} move700={f.get("move700")} move1500={f.get("move1500")}')
        print()
