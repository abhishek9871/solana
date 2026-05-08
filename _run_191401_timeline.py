"""Precise chronological dissection of run 191401."""
import json
from datetime import datetime, timezone

PATH = '/root/piggy/data/pgg2_direct_live_20260507_191401_decisions.jsonl'

trades = []
opens = {}

for line in open(PATH):
    if not line.strip(): continue
    try:
        x = json.loads(line)
    except:
        continue
    k = x.get('kind')
    m = x.get('mint')
    if k == 'open':
        opens[m] = x
    elif k == 'close':
        op = opens.get(m, {})
        sp_features = op.get('features') or {}
        pnl = float(x.get('pnl_sol') or 0)
        ts = int(x.get('ts_ms') or 0)
        trades.append({
            'mint': m[:8],
            'open_ts': int(op.get('ts_ms') or 0),
            'close_ts': ts,
            'lane': op.get('lane') or '',
            'pnl': pnl,
            'reason_close': x.get('reason') or '',
            'first_buy': float(sp_features.get('first_buy_sol') or 0),
            'top700': float(sp_features.get('top_share700') or 0),
            'top1500': float(sp_features.get('top_share1500') or 0),
            'buy1500': float(sp_features.get('buy1500') or 0),
            'uniq1500': int(sp_features.get('uniq1500') or 0),
            'age_ms': int(sp_features.get('age_ms') or 0),
        })

trades.sort(key=lambda t: t['close_ts'])
total = 0.0
peak = 0.0
peak_ts = 0
trough = 0.0
trough_ts = 0

# Track buckets per hour
hours = {}

print(f'Total trades: {len(trades)}')
print(f'First trade: {datetime.fromtimestamp(trades[0]["close_ts"]/1000, tz=timezone.utc).strftime("%H:%M:%S UTC")}')
print(f'Last trade : {datetime.fromtimestamp(trades[-1]["close_ts"]/1000, tz=timezone.utc).strftime("%H:%M:%S UTC")}')
print()

# Find peak/trough chronologically
prev = 0.0
print('Cumulative PnL trajectory (every 25 trades):')
for i, t in enumerate(trades):
    total += t['pnl']
    if total > peak:
        peak = total
        peak_ts = t['close_ts']
        peak_idx = i
    if total < trough:
        trough = total
        trough_ts = t['close_ts']
        trough_idx = i
    hour_key = datetime.fromtimestamp(t['close_ts']/1000, tz=timezone.utc).strftime('%H')
    if hour_key not in hours:
        hours[hour_key] = {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
    hours[hour_key]['count'] += 1
    if t['pnl'] > 0: hours[hour_key]['wins'] += 1
    else: hours[hour_key]['losses'] += 1
    hours[hour_key]['pnl'] += t['pnl']

    if i % 25 == 0 or i == len(trades) - 1:
        ts_s = datetime.fromtimestamp(t['close_ts']/1000, tz=timezone.utc).strftime('%H:%M:%S')
        print(f'  Trade #{i+1:>3d} [{ts_s}]: cumulative={total:+.5f} SOL  this_trade={t["pnl"]:+.5f} {"WIN" if t["pnl"]>0 else "LOSS"} mint={t["mint"]}')

print()
print('=' * 100)
print('KEY MILESTONES:')
print('=' * 100)
peak_str = datetime.fromtimestamp(peak_ts/1000, tz=timezone.utc).strftime('%H:%M:%S UTC')
trough_str = datetime.fromtimestamp(trough_ts/1000, tz=timezone.utc).strftime('%H:%M:%S UTC')
print(f'PEAK profit: {peak:+.5f} SOL at {peak_str} (trade #{peak_idx+1})')
print(f'TROUGH:      {trough:+.5f} SOL at {trough_str} (trade #{trough_idx+1})')
print(f'FINAL:       {total:+.5f} SOL ({datetime.fromtimestamp(trades[-1]["close_ts"]/1000, tz=timezone.utc).strftime("%H:%M:%S UTC")})')
print()
print(f'Peak-to-trough drawdown: {peak - trough:.5f} SOL')
print(f'Trough-to-final recovery: {total - trough:.5f} SOL')

# Hourly summary
print()
print('=' * 100)
print('HOURLY SUMMARY (UTC):')
print('=' * 100)
print(f'{"hour":>6s}  {"trades":>7s}  {"W/L":>10s}  {"win%":>5s}  {"hour_pnl":>10s}  {"cum_pnl":>10s}')
running = 0.0
for hr in sorted(hours.keys()):
    h = hours[hr]
    running += h['pnl']
    win_pct = h['wins']*100//max(h['count'], 1)
    print(f'  {hr}:00  {h["count"]:>7d}  {h["wins"]:>3d}/{h["losses"]:<5d}  {win_pct:>4d}%  {h["pnl"]:>+10.5f}  {running:>+10.5f}')

# Top wins and losses
print()
print('=' * 100)
print('TOP 10 WINS:')
print('=' * 100)
for t in sorted(trades, key=lambda x: -x['pnl'])[:10]:
    ts_s = datetime.fromtimestamp(t['close_ts']/1000, tz=timezone.utc).strftime('%H:%M:%S')
    print(f'  [{ts_s}] {t["mint"]} {t["lane"]:>17s} pnl={t["pnl"]:+.5f}  reason={t["reason_close"][:30]}')

print()
print('TOP 10 LOSSES:')
for t in sorted(trades, key=lambda x: x['pnl'])[:10]:
    ts_s = datetime.fromtimestamp(t['close_ts']/1000, tz=timezone.utc).strftime('%H:%M:%S')
    print(f'  [{ts_s}] {t["mint"]} {t["lane"]:>17s} pnl={t["pnl"]:+.5f}  reason={t["reason_close"][:30]}')
