import json, sys, math
runid = 'pgg2_attack_drylive_20260506_122011'
dec = f'/root/piggy/data/{runid}_decisions.jsonl'
raw = f'/root/piggy/data/{runid}_raw.jsonl'
# Proposed confirmed spark3 rule: arm -> no deep dump -> break >=1.50 within 6s -> hold 250ms at >=94% of watch.
MIN_BREAK=1.50
MAX_BREAK=2.40
MAX_DELAY=6000
CONFIRM_MS=250
HOLD=0.94
MIN_PRE_HOLD=0.92
arms={}
for line in open(dec):
    if not line.strip(): continue
    x=json.loads(line)
    if x.get('kind')=='spark3_candidate':
        m=x.get('mint')
        f=x.get('features') or {}
        arms[m]={'ts':x.get('ts_ms') or f.get('ts_ms') or 0,'base':float(f.get('price') or 0),'reason':x.get('reason')}
print('arms',len(arms))
series={m:[] for m in arms}
for line in open(raw):
    if not line.strip(): continue
    try: x=json.loads(line)
    except Exception: continue
    m=x.get('mint')
    if m not in arms: continue
    ts=x.get('ts_ms') or 0
    cp=float(x.get('curve_price') or 0)
    if cp<=0: continue
    a=arms[m]
    if ts < a['ts'] or ts > a['ts']+120000: continue
    series[m].append((ts,cp))
selected=[]
for m,a in arms.items():
    base=a['base']
    if base<=0 or not series[m]: continue
    min_pre=1.0
    watch=None
    confirmed=None
    for ts,cp in series[m]:
        dt=ts-a['ts']
        mult=cp/base
        if dt <= MAX_DELAY:
            min_pre=min(min_pre,mult)
        if min_pre < MIN_PRE_HOLD:
            break
        if dt > MAX_DELAY:
            break
        if watch is None and MIN_BREAK <= mult <= MAX_BREAK:
            watch=(ts,cp,mult)
            continue
        if watch and ts-watch[0] >= CONFIRM_MS:
            if cp >= watch[1]*HOLD:
                confirmed=(ts,cp,mult)
            break
    if confirmed:
        after=[(ts,cp) for ts,cp in series[m] if ts>=confirmed[0]]
        max_mult=max(cp/base for ts,cp in after) if after else confirmed[2]
        min_1s=min((cp/base for ts,cp in after if ts<=confirmed[0]+1000), default=confirmed[2])
        selected.append((max_mult,confirmed[2],confirmed[0]-a['ts'],min_pre,min_1s,m[:8],a['reason']))
print('selected',len(selected))
for row in sorted(selected, reverse=True):
    print('max=%5.2f entry=%5.2f dt=%4d premin=%5.3f min1s=%5.3f mint=%s %s'%row)
