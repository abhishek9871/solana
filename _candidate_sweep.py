import json, sys, collections, os, math
runid=sys.argv[1]
raw=f'/root/piggy/data/{runid}_raw.jsonl'
# load trade rows
rows=[]
for line in open(raw, errors='ignore'):
    if not line.strip(): continue
    try: x=json.loads(line)
    except: continue
    if x.get('kind')!='trade' or x.get('side')!='buy': continue
    cp=float(x.get('curve_price') or 0)
    ts=int(x.get('ts_ms') or 0)
    if cp<=0 or ts<=0: continue
    x['_cp']=cp; x['_ts']=ts; x['_sol']=float(x.get('sol') or 0); rows.append(x)
by=collections.defaultdict(list)
for x in rows: by[x.get('mint')].append(x)
for m in by: by[m].sort(key=lambda r:r['_ts'])

def stats(m, start, end):
    buys=sells=0; bsol=ssol=0.0; buyers=collections.Counter()
    for x in by.get(m,[]):
        ts=x['_ts']
        if ts<start or ts>end: continue
        if x.get('side')=='buy':
            buys+=1; bsol+=x['_sol']; buyers[x.get('user')]+=x['_sol']
        else:
            sells+=1; ssol+=float(x.get('sol') or 0)
    top=max(buyers.values())/bsol if bsol>0 and buyers else 1.0
    return bsol, ssol, len(buyers), top

def future_mult(m, ts, price, horizon=60000):
    mx=price; mn=price
    for x in by.get(m,[]):
        if x['_ts']<ts: continue
        if x['_ts']>ts+horizon: break
        mx=max(mx,x['_cp']); mn=min(mn,x['_cp'])
    return mx/price if price else 0, mn/price if price else 0

def simulate(min_buy, min_vsol=30, max_vsol=125, min_move=1.70, max_move=3.50):
    cands=[]; seen=set()
    for m,lst in by.items():
        first=lst[0]
        first_price=first['_cp']; first_ts=first['_ts']
        for x in lst:
            if m in seen: break
            age=(x['_ts']-first_ts)/1000
            move=x['_cp']/first_price if first_price>0 else 0
            v=float(x.get('vsol_sol') or 0)
            if not (3.0<=age<=30.0 and min_move<=move<=max_move and min_vsol<=v<=max_vsol and min_buy<=x['_sol']<=0.55):
                continue
            b1500,s1500,u1500,top1500=stats(m,x['_ts']-1500,x['_ts'])
            sellr=s1500/max(b1500,0.001)
            if sellr>0.08: continue
            post, draw=future_mult(m,x['_ts'],x['_cp'],60000)
            cands.append((m,x,post,draw,b1500,u1500,top1500,sellr))
            seen.add(m)
    wins=sum(1 for _,_,post,_,*rest in cands if post>=1.20)
    moons=sum(1 for _,_,post,_,*rest in cands if post>=1.50)
    big=sum(1 for _,_,post,_,*rest in cands if post>=2.0)
    bad=sum(1 for _,_,post,draw,*rest in cands if post<1.10 and draw<0.90)
    print(f'CONFIG min_buy={min_buy} min_vsol={min_vsol}: cands={len(cands)} win>=1.20={wins} moon>=1.5={moons} big>=2={big} bad(post<1.1&draw<.9)={bad}')
    for m,x,post,draw,b,u,top,sellr in sorted(cands, key=lambda t:t[2], reverse=True)[:12]:
        print(f"  {m[:8]} post={post:.2f}x draw={draw:.2f} age={(x['_ts']-by[m][0]['_ts'])/1000:.1f}s move={x['_cp']/by[m][0]['_cp']:.2f} vsol={float(x.get('vsol_sol') or 0):.1f} buy={x['_sol']:.3f} b1500={b:.3f}/{u} top={top:.2f}")
    return cands
for mb in [0.008,0.006,0.005,0.003]:
    simulate(mb,30)
print('--- lower vsol for micro buys ---')
for mv in [20,17,15]:
    simulate(0.005,mv)
