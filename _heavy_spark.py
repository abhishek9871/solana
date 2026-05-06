import json,re
runid=open('/root/piggy/current_pgg2_runid.txt').read().strip()
dec=f'/root/piggy/data/{runid}_decisions.jsonl'; raw=f'/root/piggy/data/{runid}_raw.jsonl'
arms={}
for line in open(dec):
    if not line.strip(): continue
    x=json.loads(line)
    if x.get('kind')=='wave_arm':
        f=x.get('features') or {}; m=x.get('mint')
        if m and m not in arms:
            arms[m]={'ts':x.get('ts_ms') or 0,'base':float(f.get('price') or 0),'f':f,'reason':x.get('reason')}
series={m:[] for m in arms}
for line in open(raw):
    if not line.strip(): continue
    try:x=json.loads(line)
    except Exception: continue
    m=x.get('mint')
    if m not in arms: continue
    ts=x.get('ts_ms') or 0; cp=float(x.get('curve_price') or 0)
    if cp<=0: continue
    a=arms[m]
    if a['ts'] <= ts <= a['ts']+120000: series[m].append((ts,cp))
rows=[]
for m,a in arms.items():
    f=a['f']; base=a['base']; pts=series[m]
    if base<=0 or base>0.01 or not pts: continue
    b=float(f.get('buy700') or 0); u=int(f.get('uniq700') or 0); top=float(f.get('top_share700') or 1); move=float(f.get('move700') or 1); sell=float(f.get('sell700') or 0)+float(f.get('sell1500') or 0)
    # heavy spark candidate: the exact missed bucket above normal spark cap
    qualifies = (7.5 < b <= 12.0 and u==3 and 0.30<=top<=0.66 and sell<=0.001 and move>=1.15)
    if qualifies:
        mx=max(p for _,p in pts)/base; mn=min(p for _,p in pts)/base
        t15=next((t-a['ts'] for t,p in pts if p/base>=1.5), None)
        t2=next((t-a['ts'] for t,p in pts if p/base>=2.0), None)
        rows.append((mx,mn,t15,t2,m[:8],b,u,top,move,a['reason']))
print('heavy_spark_qualifiers',len(rows))
for r in sorted(rows, reverse=True):
    print('max=%.3f min=%.3f t1.5=%s t2=%s mint=%s b=%.3f u=%d top=%.2f move=%.3f reason=%s'%r)
