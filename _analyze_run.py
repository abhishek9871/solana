import json, time, collections, sys
RUNID = sys.argv[1] if len(sys.argv) > 1 else 'piggy_lossfix_hetzner_20260505_074017'
state_path = f'/root/piggy/data/{RUNID}_state.json'
dec_path = f'/root/piggy/data/{RUNID}_decisions.jsonl'

s = json.load(open(state_path))['session']
now = time.time()
runtime_sec = now - s['started_at']
print('=== RUN STATE ===')
print(f'started:    {time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s["started_at"]))} UTC')
print(f'now:        {time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))} UTC')
print(f'runtime:    {int(runtime_sec//60)}m {int(runtime_sec%60)}s')
print()
print(f'realized:   {s["realized_pnl_sol"]:+.6f} SOL  (paper)')
print(f'closes:     {s["closes"]}    W/L: {s["wins"]}/{s["losses"]}    kills: {s["kills"]}')
print(f'best_mult:  {s["best_mult"]:.3f}x')
print(f'creates:    {s["creates"]:,}    trades_seen: {s["trades"]:,}')
print(f'shreds:     {s["shreds"]:,}    bc_updates: {s["curve_updates"]:,}')
print(f'reconnects: {s["reconnects"]}')

print()
print('=== TRADE BREAKDOWN ===')
opens=[]; closes=[]
mint_to_open={}
lanes_open=collections.Counter()
reasons=collections.Counter()
try:
    for line in open(dec_path):
        if not line.strip(): continue
        x=json.loads(line)
        k=x.get('kind')
        if k=='open':
            opens.append(x); mint_to_open[x.get('mint')]=x
            lanes_open[x.get('lane','?')]+=1
        elif k=='close':
            closes.append(x); reasons[x.get('reason')]+=1
except FileNotFoundError:
    print('No decisions file yet')

if closes:
    wins=[c for c in closes if float(c.get('pnl_sol') or 0)>0]
    losses=[c for c in closes if float(c.get('pnl_sol') or 0)<0]
    gw=sum(float(c.get('pnl_sol') or 0) for c in wins)
    gl=sum(float(c.get('pnl_sol') or 0) for c in losses)
    print(f'Opens: {len(opens)}  Closes: {len(closes)}  W/L: {len(wins)}/{len(losses)}')
    print(f'Gross wins:   {gw:+.6f} SOL')
    print(f'Gross losses: {gl:+.6f} SOL')
    print(f'Net:          {gw+gl:+.6f} SOL')
    print()
    print('Lane distribution:')
    for lane, n in lanes_open.most_common():
        print(f'  {lane}: {n}')
    print()
    print('Close reasons:')
    for r, n in reasons.most_common():
        pnl = sum(float(c.get('pnl_sol') or 0) for c in closes if c.get('reason')==r)
        print(f'  {r:32s} n={n:2d}  pnl={pnl:+.6f}')
    print()
    print('Per-trade detail (chronological):')
    print(f'  {"#":>3s} {"time":8s}  {"mint":10s} {"lane":28s} {"reason":32s} {"pnl_SOL":>10}')
    for i, c in enumerate(sorted(closes, key=lambda x: x.get('ts_ms',0)), 1):
        m = (c.get('mint','') or '')[:8]
        op = mint_to_open.get(c.get('mint'),{})
        lane = (op.get('lane','?') or '?')[:28]
        reason = (c.get('reason','?') or '?')[:32]
        pnl = float(c.get('pnl_sol') or 0)
        ts = c.get('ts_ms',0) / 1000
        tstr = time.strftime("%H:%M:%S", time.gmtime(ts))
        print(f'  {i:>3d} {tstr}  {m:10s} {lane:28s} {reason:32s} {pnl:>+10.4f}')
else:
    print('No closes yet — still warming up')

print()
print('=== PROJECTIONS ===')
hours = runtime_sec/3600
if hours > 0.01:
    n_closes = len(closes) if closes else 0
    sol_per_hour = s["realized_pnl_sol"] / hours
    print(f'Trade rate:    {n_closes/hours:.2f} closes/hr  (creates: {s["creates"]/hours:.0f}/hr)')
    print(f'PnL rate:      {sol_per_hour:+.6f} SOL/hr  (= ${sol_per_hour*170:+.2f}/hr at $170/SOL)')
    print(f'Projected 7h:  {sol_per_hour*7:+.4f} SOL  (= ${sol_per_hour*7*170:+.2f})')
