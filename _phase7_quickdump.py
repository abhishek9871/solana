"""Quick dump of Phase 7 close + open events with feature details."""
import json
import sys

LOG = '/root/piggy/data/pgg2_phase7_drylive_20260508_071332_decisions.jsonl'

opens = []
closes = []
for line in open(LOG):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    if x.get('kind') == 'open':
        opens.append(x)
    elif x.get('kind') == 'close':
        closes.append(x)

print('=== OPENS ===')
for x in opens:
    f = x.get('features') or {}
    mint = x.get('mint', '')[:14]
    lane = x.get('lane', '')
    ts = x.get('ts_ms', 0)
    print(f'mint={mint} lane={lane} ts={ts}')
    print(f'  buy1500={f.get("buy1500",0):.2f} uniq1500={f.get("uniq1500",0)} top1500={f.get("top_share1500",0):.3f} age_ms={f.get("age_ms",0)} vsol={f.get("vsol_sol",0):.1f}')
    print(f'  move250={f.get("move250",1):.3f} move700={f.get("move700",1):.3f} move1500={f.get("move1500",1):.3f}')
    print(f'  first_buy_sol={f.get("first_buy_sol",0):.2f} slot_buy_sol={f.get("slot_buy_sol",0):.2f} slot_buyers={f.get("slot_buyers",0)}')

print()
print('=== CLOSES ===')
for x in closes:
    f = x.get('features') or {}
    mint = x.get('mint', '')[:14]
    reason = x.get('reason', '')
    pnl = x.get('pnl_sol', 0)
    ts = x.get('ts_ms', 0)
    print(f'mint={mint} reason={reason[:80]} pnl={pnl:+.5f} ts={ts}')
    print(f'  move250={f.get("move250",1):.3f} move700={f.get("move700",1):.3f}')
    print(f'  last_buy_age_ms={f.get("last_buy_age_ms",0)} last_sell_age_ms={f.get("last_sell_age_ms",0)} sell1500={f.get("sell1500",0):.3f}')
