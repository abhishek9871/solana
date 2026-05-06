"""Funnel analysis of the LIVE run vs historical 7h tape, no bot impact."""
import json, time, collections, sys

LIVE = '/root/piggy/data/piggy_lossfix_hetzner_20260505_074017_decisions.jsonl'
LIVE_RAW = '/root/piggy/data/piggy_lossfix_hetzner_20260505_074017_raw.jsonl'
LIVE_STATE = '/root/piggy/data/piggy_lossfix_hetzner_20260505_074017_state.json'
HIST = '/root/piggy/data/piggy_hetzner_hard088_1h_20260504_184706_decisions.jsonl'

state = json.load(open(LIVE_STATE))['session']
runtime_h = (time.time() - state['started_at']) / 3600

# Live funnel
live_arms = 0
live_disarms = 0
live_strikes = 0
live_skips = 0
live_disarm_reasons = collections.Counter()
live_skip_reasons = collections.Counter()
live_arm_features = []
live_armed_mints = {}
live_struck_mints = set()
arm_ts = {}
for line in open(LIVE):
    if not line.strip(): continue
    x = json.loads(line)
    k = x.get('kind')
    m = x.get('mint')
    if k == 'wave_arm':
        live_arms += 1
        if m and m not in live_armed_mints:
            live_armed_mints[m] = x.get('features') or {}
            arm_ts[m] = x.get('ts_ms', 0)
            live_arm_features.append(x.get('features') or {})
    elif k == 'wave_disarm':
        live_disarms += 1
        live_disarm_reasons[x.get('reason','?')] += 1
    elif k == 'strike_plan':
        live_strikes += 1
        if m: live_struck_mints.add(m)
    elif k == 'strike_skipped':
        live_skips += 1
        live_skip_reasons[x.get('reason','?')] += 1

# Historical funnel for comparison
hist_arms = 0
hist_strikes = 0
hist_disarms = 0
hist_skips = 0
hist_disarm_reasons = collections.Counter()
for line in open(HIST):
    if not line.strip(): continue
    x = json.loads(line)
    k = x.get('kind')
    if k == 'wave_arm': hist_arms += 1
    elif k == 'wave_disarm':
        hist_disarms += 1
        hist_disarm_reasons[x.get('reason','?')] += 1
    elif k == 'strike_plan': hist_strikes += 1
    elif k == 'strike_skipped': hist_skips += 1

# Stats
print('=== FUNNEL COMPARISON: LIVE (current 31min) vs HISTORICAL (7h tape) ===')
print()
print(f'{"":18s} {"LIVE":>15} {"HIST":>15} {"LIVE/h":>10} {"HIST/h":>10}')
hist_h = 7.16
print(f'{"runtime":18s} {f"{runtime_h:.2f}h":>15} {f"{hist_h:.2f}h":>15}')
print(f'{"creates":18s} {state["creates"]:>15,} {11523:>15,} {state["creates"]/runtime_h:>10.0f} {11523/hist_h:>10.0f}')
print(f'{"trades":18s} {state["trades"]:>15,} {261825:>15,} {state["trades"]/runtime_h:>10.0f} {261825/hist_h:>10.0f}')
print(f'{"shreds":18s} {state["shreds"]:>15,} {549300:>15,} {state["shreds"]/runtime_h:>10.0f} {549300/hist_h:>10.0f}')
print()
print(f'{"wave_arms":18s} {live_arms:>15} {hist_arms:>15} {live_arms/runtime_h:>10.1f} {hist_arms/hist_h:>10.1f}')
print(f'{"wave_disarms":18s} {live_disarms:>15} {hist_disarms:>15} {live_disarms/runtime_h:>10.1f} {hist_disarms/hist_h:>10.1f}')
print(f'{"strike_plans":18s} {live_strikes:>15} {hist_strikes:>15} {live_strikes/runtime_h:>10.2f} {hist_strikes/hist_h:>10.1f}')
print(f'{"strike_skipped":18s} {live_skips:>15} {hist_skips:>15} {live_skips/runtime_h:>10.2f} {hist_skips/hist_h:>10.1f}')
print()
print(f'creates -> arm conversion:  LIVE {live_arms*100/max(state["creates"],1):.1f}%   HIST {hist_arms*100/11523:.1f}%')
print(f'arms -> strike conversion:  LIVE {live_strikes*100/max(live_arms,1):.1f}%   HIST {hist_strikes*100/hist_arms:.1f}%')

print()
print('=== LIVE wave_disarm reasons ===')
for r, n in live_disarm_reasons.most_common():
    print(f'  {r}: {n}')

print()
print('=== LIVE strike_skip reasons ===')
for r, n in live_skip_reasons.most_common():
    print(f'  {r}: {n}')

# Now check: are there MOONSHOTS in live run that armed but never struck?
print()
print('=== Live armed-but-not-struck post-arm trajectory ===')
armed_only = {m: f for m, f in live_armed_mints.items() if m not in live_struck_mints}
print(f'Mints armed but not struck: {len(armed_only)}')

# Read raw to find post-arm max
mint_arm_price = {}
mint_post_max = {}
mint_post_max_age = {}
for line in open(LIVE_RAW):
    if not line.strip(): continue
    try: x = json.loads(line)
    except: continue
    if x.get('kind') != 'trade': continue
    m = x.get('mint')
    if m not in armed_only: continue
    cp = x.get('curve_price', 0)
    if cp <= 0: continue
    ts = x.get('ts_ms', 0)
    a = arm_ts.get(m, 0)
    if ts < a: continue
    if ts > a + 60000: continue
    if m not in mint_arm_price:
        mint_arm_price[m] = cp
    if cp > mint_post_max.get(m, 0):
        mint_post_max[m] = cp
        mint_post_max_age[m] = ts - a

# Compute multipliers
moonshots = []
for m in armed_only:
    bp = mint_arm_price.get(m, 0)
    pm = mint_post_max.get(m, 0)
    if bp <= 0: continue
    mult = pm / bp
    if mult >= 1.32:
        moonshots.append((m[:8], mult, mint_post_max_age.get(m, 0), armed_only[m]))

moonshots.sort(key=lambda x: -x[1])
print(f'Of {len(armed_only)} armed-but-not-struck, {len(moonshots)} pumped to >=1.32x within 60s:')
print(f'  >=2x: {sum(1 for m in moonshots if m[1] >= 2.0)}')
print(f'  >=3x: {sum(1 for m in moonshots if m[1] >= 3.0)}')
print(f'  >=5x: {sum(1 for m in moonshots if m[1] >= 5.0)}')
print()
print('Top 15 missed moonshots in LIVE run:')
print(f'  {"mint":10s} {"mult":>8s} {"age_to_peak":>12s} {"buy700":>8s} {"uniq700":>8s} {"top_share":>10s}')
for m, mult, age, f in moonshots[:15]:
    print(f'  {m:10s} {mult:>7.2f}x {age/1000:>10.1f}s   {f.get("buy700",0):>8.2f} {f.get("uniq700",0):>8d} {f.get("top_share700",0):>10.3f}')
