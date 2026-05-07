import json, sys, collections, os
runid=sys.argv[1]
raw=f'/root/piggy/data/{runid}_raw.jsonl'
log=f'/root/piggy/logs/{runid}.log'
# first/max from raw
first={}; maxp={}; t0={}; tmax={}; firstrow={}; maxrow={}; counts=collections.Counter()
for line in open(raw, errors='ignore'):
    if not line.strip(): continue
    try: x=json.loads(line)
    except: continue
    m=x.get('mint'); cp=x.get('curve_price') or 0; ts=x.get('ts_ms') or 0
    if not m or cp<=0: continue
    counts[(m,x.get('kind'))]+=1
    if m not in first:
        first[m]=cp; maxp[m]=cp; t0[m]=ts; tmax[m]=ts; firstrow[m]=x; maxrow[m]=x
    elif ts<=t0[m]+300000 and cp>maxp[m]:
        maxp[m]=cp; tmax[m]=ts; maxrow[m]=x
logtxt=open(log, errors='ignore').read() if os.path.exists(log) else ''
opened=set()
dec=f'/root/piggy/data/{runid}_decisions.jsonl'
if os.path.exists(dec):
    for line in open(dec, errors='ignore'):
        if not line.strip(): continue
        try: d=json.loads(line)
        except: continue
        if d.get('kind')=='open': opened.add(d.get('mint'))
rows=[]
for m,fp in first.items():
    mult=maxp[m]/fp if fp else 0
    if mult>=1.5 and m not in opened and m[:4] not in logtxt:
        rows.append((mult,tmax[m]-t0[m],m))
rows.sort(reverse=True)
print('NO_LOG_MISSED_MOONSHOTS', len(rows))
for mult,dt,m in rows[:15]:
    fr=firstrow[m]; mr=maxrow[m]
    print('\nMINT',m,'mult',f'{mult:.2f}','dt_s',f'{dt/1000:.2f}')
    for label,row in [('first',fr),('max',mr)]:
        keys=['kind','mint','signature','slot','ts_ms','is_buy','sol_amount','token_amount','curve_price','vsol_sol','virtual_sol_reserves','virtual_token_reserves','bonding_curve','creator','user','source','program','pool','base_mint','quote_mint']
        mini={k:row.get(k) for k in keys if k in row}
        print(label, mini)
    # count buys/sells before max and buy sol stats
    buys=sells=0; bsol=ssol=0.0; users=set(); last_ts=0
    for line in open(raw, errors='ignore'):
        if not line.strip(): continue
        try: x=json.loads(line)
        except: continue
        if x.get('mint')!=m: continue
        ts=x.get('ts_ms') or 0
        if ts<t0[m] or ts>tmax[m]: continue
        if x.get('is_buy'):
            buys+=1; bsol+=float(x.get('sol_amount') or 0); users.add(x.get('user'))
        else:
            sells+=1; ssol+=float(x.get('sol_amount') or 0)
    print('flow_to_peak',{'buys':buys,'sells':sells,'buy_sol':round(bsol,4),'sell_sol':round(ssol,4),'users':len([u for u in users if u])})
