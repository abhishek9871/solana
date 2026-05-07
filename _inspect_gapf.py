import json, sys, subprocess, os
runid=sys.argv[1]
raw=f'/root/piggy/data/{runid}_raw.jsonl'
log=f'/root/piggy/logs/{runid}.log'
target='GaPF'
print('--- LOG ---')
for i,line in enumerate(open(log, errors='ignore'),1):
    if target in line: print(f'{i}:{line.rstrip()}')
print('--- RAW ---')
rows=[]
for line in open(raw, errors='ignore'):
    if target in line:
        x=json.loads(line)
        if x.get('kind')=='trade' and (x.get('curve_price') or 0)>0: rows.append(x)
if rows:
    first=rows[0]['curve_price']; t0=rows[0]['ts_ms']
    for x in rows[:50]:
        print(f"dt={(x['ts_ms']-t0)/1000:6.2f}s side={x.get('side',''):4s} instr={x.get('instruction_kind',''):18s} sol={float(x.get('sol') or 0):7.4f} vsol={float(x.get('vsol_sol') or 0):7.2f} move={x['curve_price']/first:5.2f} user={str(x.get('user'))[:4]}")
