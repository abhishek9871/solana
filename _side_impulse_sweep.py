import json, sys, collections, os
runid=sys.argv[1]
raw=f'/root/piggy/data/{runid}_raw.jsonl'
rows=[]
for line in open(raw, errors='ignore'):
    if not line.strip(): continue
    try: x=json.loads(line)
    except: continue
    if x.get('kind')!='trade': continue
    cp=float(x.get('curve_price') or 0); ts=int(x.get('ts_ms') or 0)
    if cp<=0 or ts<=0: continue
    x['_cp']=cp; x['_ts']=ts; x['_sol']=float(x.get('sol') or 0); rows.append(x)
by=collections.defaultdict(list)
for x in rows: by[x.get('mint')].append(x)
for m in by: by[m].sort(key=lambda r:r['_ts'])

def future(m, ts, price, horizon=60000):
    mx=price; mn=price
    for x in by[m]:
        if x['_ts']<ts: continue
        if x['_ts']>ts+horizon: break
        mx=max(mx,x['_cp']); mn=min(mn,x['_cp'])
    return mx/price, mn/price

def sweep(allow_side, min_move, max_move, min_vsol, max_vsol, min_sol, max_age=30):
    c=[]; seen=set()
    for m,lst in by.items():
        fp=lst[0]['_cp']; ft=lst[0]['_ts']
        for x in lst:
            if m in seen: break
            if allow_side!='any' and x.get('side')!=allow_side: continue
            age=(x['_ts']-ft)/1000
            move=x['_cp']/fp if fp else 0
            v=float(x.get('vsol_sol') or 0)
            if 3<=age<=max_age and min_move<=move<=max_move and min_vsol<=v<=max_vsol and x['_sol']>=min_sol:
                post,draw=future(m,x['_ts'],x['_cp'])
                c.append((m,x,post,draw,move,age,v)); seen.add(m); break
    w=sum(1 for _,_,p,_,_,_,_ in c if p>=1.20)
    m15=sum(1 for _,_,p,_,_,_,_ in c if p>=1.50)
    b=sum(1 for _,_,p,d,_,_,_ in c if p<1.10 and d<0.90)
    print(f'side={allow_side} move={min_move}-{max_move} vsol={min_vsol}-{max_vsol} minsol={min_sol}: cands={len(c)} win20={w} moon15={m15} bad={b}')
    for mint,x,p,d,move,age,v in sorted(c,key=lambda t:t[2], reverse=True)[:12]:
        print(f"  {mint[:8]} side={x.get('side')} instr={x.get('instruction_kind')} post={p:.2f} draw={d:.2f} age={age:.1f} move={move:.2f} vsol={v:.1f} sol={x['_sol']:.4f}")
for side in ['sell','any']:
  for minmove in [1.5,1.7,2.0]:
    sweep(side,minmove,3.8,30,140,0.0001,35)
print('--- low vsol sell impulse ---')
for mv in [15,20,25]:
  sweep('sell',1.5,3.8,mv,140,0.0001,35)
