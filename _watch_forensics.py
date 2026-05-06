import json, sys
runid=open('/root/piggy/current_pgg2_runid.txt').read().strip()
dec=f'/root/piggy/data/{runid}_decisions.jsonl'
raw=f'/root/piggy/data/{runid}_raw.jsonl'
watch={}
entered=set(); closed={}
for line in open(dec):
    if not line.strip(): continue
    x=json.loads(line); m=x.get('mint')
    if not m: continue
    k=x.get('kind')
    if k in {'spark3_candidate','spark3_breakout_watch','spark3_breakout_reject','wave_arm'}:
        if m not in watch:
            f=x.get('features') or {}
            watch[m]={'mint':m,'ts':x.get('ts_ms') or f.get('ts_ms') or 0,'base':float(f.get('price') or 0),'kinds':[], 'reason':x.get('reason','')}
        watch[m]['kinds'].append((k,x.get('reason','')))
    if k=='open': entered.add(m)
    if k=='close': closed[m]=x
series={m:[] for m in watch}
for line in open(raw):
    if not line.strip(): continue
    try: x=json.loads(line)
    except Exception: continue
    m=x.get('mint')
    if m not in watch: continue
    ts=x.get('ts_ms') or 0; cp=float(x.get('curve_price') or 0)
    if cp<=0: continue
    w=watch[m]
    if w['ts'] <= ts <= w['ts']+120000:
        series[m].append((ts,cp))
rows=[]
for m,w in watch.items():
    base=w['base']; pts=series[m]
    if base<=0 or not pts: continue
    mx=max(p for _,p in pts)/base
    mn=min(p for _,p in pts)/base
    t15=next((t-w['ts'] for t,p in pts if p/base>=1.5), None)
    t2=next((t-w['ts'] for t,p in pts if p/base>=2.0), None)
    rows.append((mx,mn,t15,t2,m[:8],m in entered,w['kinds'][:4],w['reason']))
print('RUN', runid, 'watched', len(watch), 'with_price', len(rows), 'entered', len(entered))
print('Top unentered candidates/waves by post max:')
for mx,mn,t15,t2,short,ent,kinds,reason in sorted([r for r in rows if not r[5]], reverse=True)[:20]:
    print(f'{short} max={mx:.3f} min={mn:.3f} t1.5={t15} t2={t2} kinds={kinds} reason={reason}')
print('\nEntered:')
for mx,mn,t15,t2,short,ent,kinds,reason in sorted([r for r in rows if r[5]], reverse=True):
    c=closed.get(next(m for m in watch if m.startswith(short)),{})
    print(f'{short} max={mx:.3f} min={mn:.3f} close={c.get("reason")} pnl={c.get("pnl_sol")} kinds={kinds} reason={reason}')
