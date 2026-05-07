import json, re, sys, collections, math, os
runid=sys.argv[1]
base='/root/piggy'
raw=f'{base}/data/{runid}_raw.jsonl'
dec=f'{base}/data/{runid}_decisions.jsonl'
log=f'{base}/logs/{runid}.log'

# raw moonshot table from first curve price to max curve price in first 5m seen by file
mint_first={}; mint_max={}; mint_t0={}; mint_tmax={}; kinds=collections.Counter()
for line in open(raw, errors='ignore'):
    if not line.strip(): continue
    try: x=json.loads(line)
    except Exception: continue
    kinds[x.get('kind')]+=1
    m=x.get('mint')
    cp=x.get('curve_price') or 0
    ts=x.get('ts_ms') or 0
    if not m or not cp or cp<=0: continue
    if m not in mint_first:
        mint_first[m]=cp; mint_t0[m]=ts; mint_max[m]=cp; mint_tmax[m]=ts
    elif ts <= mint_t0[m] + 300000 and cp > mint_max[m]:
        mint_max[m]=cp; mint_tmax[m]=ts

opened=set(); closes={}
for line in open(dec, errors='ignore') if os.path.exists(dec) else []:
    if not line.strip(): continue
    try: x=json.loads(line)
    except Exception: continue
    if x.get('kind')=='open': opened.add(x.get('mint'))
    if x.get('kind')=='close': closes[x.get('mint')]=x

moon=[]
for m,fp in mint_first.items():
    mx=mint_max.get(m,0)
    if fp>0 and mx/fp>=1.5:
        moon.append((mx/fp, mint_tmax[m]-mint_t0[m], m))
moon.sort(reverse=True)

# collect log lines per mint prefix
logtxt=open(log, errors='ignore').read() if os.path.exists(log) else ''
lines=logtxt.splitlines()

print('RAW_KINDS', dict(kinds), 'OPENED', len(opened), 'CLOSED', len(closes))
print('TOP_MOONSHOT_REASON_MAP')
for mult,dt,m in moon[:25]:
    pref=m[:4]
    related=[ln for ln in lines if pref in ln]
    status='CAPTURED' if m in opened else 'MISSED'
    close=closes.get(m) or {}
    pnl=close.get('pnl_sol')
    reason=close.get('reason')
    print(f'\n{status} {m[:8]} mult={mult:.2f}x dt={dt/1000:.2f}s pnl={pnl} reason={reason}')
    if related:
        # Show only meaningful lifecycle/block lines, compact
        keep=[]
        for ln in related:
            if any(s in ln for s in ['EXEC-SPIKE','RAW-MOMENTUM','QUOTE-BLOCK','EXECUTION-BLOCK','EDGE-SKIP','QUOTE-SHADOW-BUY','QUOTE-SHADOW-SELL','LIVE-QUOTE','BIRTH-SCOUT','BIRTH-CLOSE','DIRECT-QUOTE BUY','DIRECT-QUOTE SELL']):
                keep.append(ln)
        for ln in keep[-8:]:
            print('  '+ln)
    else:
        print('  no log lines for prefix')
