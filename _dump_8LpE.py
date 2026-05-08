import json
path = '/root/piggy/data/experimentalji_direct_live_20260507_144914_decisions.jsonl'
for line in open(path):
    if not line.strip(): continue
    try:
        x = json.loads(line)
    except: continue
    if not (x.get('mint') or '').startswith('8LpE'):
        continue
    k = x.get('kind')
    f = x.get('features') or {}
    if k in ('strike_plan', 'open', 'close', 'wave_arm', 'strike_skipped'):
        rsn = x.get('reason') or ''
        pnl = x.get('pnl_sol') or ''
        lane = x.get('lane') or ''
        print(f"=== {k} lane={lane} ts={x.get('ts_ms')} reason={rsn} pnl={pnl}")
        print(f"  age_ms={f.get('age_ms')} buy700={f.get('buy700')} uniq700={f.get('uniq700')} top_share700={f.get('top_share700')}")
        print(f"  cluster_score={f.get('cluster_score')} buyer_hhi700={f.get('buyer_hhi700')}")
        print(f"  buy1500={f.get('buy1500')} uniq1500={f.get('uniq1500')} sell1500={f.get('sell1500')}")
        print(f"  raw_profile={f.get('raw_profile')} raw_exec_variant={f.get('raw_exec_variant')} raw_buy_sol_10s={f.get('raw_buy_sol_10s')} raw_unique_buyers_10s={f.get('raw_unique_buyers_10s')}")
        print(f"  vsol_sol={f.get('vsol_sol')} last_buy_age_ms={f.get('last_buy_age_ms')} last_sell_age_ms={f.get('last_sell_age_ms')} buy_stall={f.get('buy_stall')} flow_live={f.get('flow_live')}")
        print(f"  move250={f.get('move250')} move700={f.get('move700')} move1500={f.get('move1500')}")
        print(f"  has_curve={f.get('has_curve')} price={f.get('price')} score={f.get('score')}")
        print()
