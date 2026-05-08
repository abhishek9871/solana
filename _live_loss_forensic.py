"""Deep forensic on the live run losses."""
import json, sys
RUNID = sys.argv[1] if len(sys.argv) > 1 else 'experimentalji_direct_live_20260507_132452'
DEC = f'/root/piggy/data/{RUNID}_decisions.jsonl'
LOG = f'/root/piggy/logs/{RUNID}.log'
RAW = f'/root/piggy/data/{RUNID}_raw.jsonl'

opens = {}
closes_by_mint = {}
all_closes = []
for line in open(DEC):
    if not line.strip(): continue
    x = json.loads(line)
    if x.get('kind') == 'open':
        opens[x.get('mint')] = x
    elif x.get('kind') == 'close':
        closes_by_mint[x.get('mint')] = x
        all_closes.append(x)

print(f'Total closes: {len(all_closes)}')
print()

# Sort by ts
all_closes.sort(key=lambda c: c.get('ts_ms', 0))

for i, c in enumerate(all_closes, 1):
    m = c.get('mint', '')
    op = opens.get(m, {})
    pnl = float(c.get('pnl_sol') or 0)
    is_win = pnl > 0
    print(f'### TRADE {i}: {m[:8]}  {"WIN" if is_win else "LOSS"}  pnl={pnl:+.6f} SOL  reason={c.get("reason")}')
    print(f'  open ts: {op.get("ts_ms")} -> close ts: {c.get("ts_ms")}  duration={(c.get("ts_ms",0)-op.get("ts_ms",0))/1000:.2f}s')
    print(f'  lane: {op.get("lane")}')
    print(f'  scout: {op.get("scout_sol")}, target: {op.get("target_sol")}, score: {op.get("score")}')
    print(f'  entry reason: {op.get("reason")}')

    of = op.get('features') or {}
    print(f'  ENTRY FEATURES:')
    feats_keys = ['raw_entry_move', 'raw_arm_age_ms', 'raw_buy_sol_10s', 'raw_unique_buyers_10s',
                  'raw_top_share_10s', 'raw_sell_ratio_10s', 'raw_sell_sol_10s', 'raw_confirm_mult',
                  'raw_exec_variant', 'raw_profile', 'vsol_sol', 'first_buy_sol', 'is_mayhem',
                  'wave_armed', 'cluster_score', 'price', 'age_ms', 'buy700', 'buy1500',
                  'uniq700', 'uniq1500', 'sell1500', 'top_share700', 'top_share1500',
                  'last_buy_age_ms', 'last_sell_age_ms', 'flow_live', 'slot_buyers',
                  'slot_buy_sol', 'slot_top_share']
    for k in feats_keys:
        if k in of: print(f'    {k}: {of[k]}')

    cf = c.get('features') or {}
    print(f'  EXIT FEATURES:')
    for k in ['mult', 'peak_mult', 'price', 'last_buy_age_ms', 'last_sell_age_ms',
              'sell1500', 'flow_live']:
        if k in cf: print(f'    {k}: {cf[k]}')
    print()

# Now pull log entries showing the buy + sell trajectory for each
print()
print('=== LIVE LOG TRAJECTORIES ===')
mint_short_to_full = {}
for c in all_closes:
    m = c.get('mint', '')
    if m: mint_short_to_full[m[:4]] = m

for c in all_closes:
    m = c.get('mint', '')
    short = m[:4]
    if not m: continue
    print(f'\n--- {m[:8]} ---')
    found = []
    with open(LOG) as f:
        for line in f:
            if short in line and any(tag in line for tag in [
                'EXEC-SPIKE', 'RAW-MOMENTUM', 'LIVE-BUY', 'LIVE-SELL', 'COMMIT',
                'EXECUTION-BLOCK', 'STABILITY', 'ROUNDTRIP', 'QUOTE-LOSS', 'RUNNER',
                'PARTIAL', 'REUSE-EXIT', 'SHADOW']):
                found.append(line.rstrip())
    for line in found:
        print(line)
