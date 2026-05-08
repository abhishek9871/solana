"""Find the exact feature signature of winning trades across all our live runs.

Goal: identify the SPECIFIC features at strike time that distinguish wins
(pnl > $2 = ~0.022 SOL) from losses. Build a filter that only matches the
signatures of past winners.
"""
import json
import glob
import os
from collections import defaultdict
from statistics import median, mean


DATA_DIR = '/root/piggy/data'


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_all_trades():
    """Walk every decision log, return list of (open, strike, close) triples."""
    trades = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, 'pgg2_*_decisions.jsonl'))):
        opens, last_strike = {}, {}
        for line in open(path):
            x = safe_json(line)
            if not x:
                continue
            k = x.get('kind')
            m = x.get('mint')
            if not m:
                continue
            if k == 'strike_plan':
                last_strike[m] = x
            elif k == 'open':
                opens[m] = (x, last_strike.get(m))
            elif k == 'close':
                o = opens.pop(m, (None, None))
                if o[0]:
                    trades.append((o[0], o[1], x, os.path.basename(path)))
    return trades


def feat(rec, key, default=0.0):
    f = rec.get('features') or {}
    v = f.get(key, default)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def feat_int(rec, key, default=0):
    f = rec.get('features') or {}
    v = f.get(key, default)
    try:
        return int(v) if v is not None else default
    except Exception:
        return default


def main():
    trades = load_all_trades()
    print(f'Total complete trades across all runs: {len(trades)}')

    # Categorize
    big_wins = []      # pnl > 0.022 SOL (~$2)
    small_wins = []    # 0 < pnl <= 0.022
    small_losses = []  # -0.011 <= pnl <= 0
    big_losses = []    # pnl < -0.011
    for o, s, c, run in trades:
        pnl = float(c.get('pnl_sol') or 0)
        if pnl > 0.022:
            big_wins.append((o, s, c, run, pnl))
        elif pnl > 0:
            small_wins.append((o, s, c, run, pnl))
        elif pnl >= -0.011:
            small_losses.append((o, s, c, run, pnl))
        else:
            big_losses.append((o, s, c, run, pnl))
    print(f'  Big wins (>$2):     {len(big_wins)}  total +{sum(t[4] for t in big_wins):.4f} SOL')
    print(f'  Small wins ($0-$2): {len(small_wins)}  total +{sum(t[4] for t in small_wins):.4f} SOL')
    print(f'  Small losses ($0 to -$1): {len(small_losses)}  total {sum(t[4] for t in small_losses):.4f} SOL')
    print(f'  Big losses (<-$1):  {len(big_losses)}  total {sum(t[4] for t in big_losses):.4f} SOL')
    print()

    # Top 30 BIG wins — what features did they share?
    print('=' * 100)
    print('TOP 30 BIG WINS (>$2) — feature signatures at strike time')
    print('=' * 100)
    print(f'{"pnl":>8s} {"mint":>10s} {"lane":>17s} {"top":>6s} {"buy1500":>8s} {"uniq":>5s} {"vsol":>6s} {"first_buy":>9s} {"slot_buyers":>11s} {"slot_buy_sol":>12s} {"top700":>6s} {"move700":>7s} {"age_ms":>7s}')
    for o, s, c, run, pnl in sorted(big_wins, key=lambda t: -t[4])[:30]:
        print(f'{pnl:>+8.4f} {o.get("mint","")[:10]:>10s} {(o.get("lane") or "")[:17]:>17s} '
              f'{feat(o,"top_share1500"):>6.3f} {feat(o,"buy1500"):>8.2f} {feat_int(o,"uniq1500"):>5d} '
              f'{feat(o,"vsol_sol"):>6.1f} {feat(o,"first_buy_sol"):>9.2f} '
              f'{feat_int(o,"slot_buyers"):>11d} {feat(o,"slot_buy_sol"):>12.2f} '
              f'{feat(o,"top_share700"):>6.3f} {feat(o,"move700"):>7.3f} {feat_int(o,"age_ms"):>7d}')
    print()

    # Compute statistical distribution
    def stats(rows, key, is_int=False):
        vals = [feat_int(r[0], key) if is_int else feat(r[0], key) for r in rows]
        if not vals:
            return None
        return {
            'n': len(vals),
            'min': min(vals), 'q1': sorted(vals)[len(vals)//4],
            'med': median(vals), 'q3': sorted(vals)[(3*len(vals))//4], 'max': max(vals),
            'mean': mean(vals),
        }

    print('=' * 100)
    print('FEATURE DISTRIBUTIONS: BIG WINS vs ALL LOSSES')
    print('=' * 100)
    losses = small_losses + big_losses
    print(f'{"feature":18s}  {"BIG WINS min/q1/med/q3/max":>40s}  {"LOSSES min/q1/med/q3/max":>40s}')
    for key, is_int in [
        ('top_share1500', False),
        ('buy1500', False),
        ('uniq1500', True),
        ('top_share700', False),
        ('vsol_sol', False),
        ('first_buy_sol', False),
        ('slot_buyers', True),
        ('slot_buy_sol', False),
        ('move700', False),
        ('move1500', False),
        ('age_ms', True),
    ]:
        ws = stats(big_wins, key, is_int)
        ls = stats(losses, key, is_int)
        if not ws or not ls:
            continue
        if is_int:
            wsr = f"{ws['min']}/{ws['q1']}/{ws['med']:.0f}/{ws['q3']}/{ws['max']}"
            lsr = f"{ls['min']}/{ls['q1']}/{ls['med']:.0f}/{ls['q3']}/{ls['max']}"
        else:
            wsr = f"{ws['min']:.2f}/{ws['q1']:.2f}/{ws['med']:.2f}/{ws['q3']:.2f}/{ws['max']:.2f}"
            lsr = f"{ls['min']:.2f}/{ls['q1']:.2f}/{ls['med']:.2f}/{ls['q3']:.2f}/{ls['max']:.2f}"
        print(f'  {key:16s}  {wsr:>40s}  {lsr:>40s}')
    print()

    # Build a discriminating filter: features where Q1(big_wins) > Q3(losses) or vice versa
    print('=' * 100)
    print('DISCRIMINATING THRESHOLDS — features that meaningfully separate winners')
    print('=' * 100)
    for key, is_int in [
        ('top_share1500', False),
        ('top_share700', False),
        ('uniq1500', True),
        ('buy1500', False),
        ('vsol_sol', False),
        ('slot_buyers', True),
    ]:
        ws = stats(big_wins, key, is_int)
        ls = stats(losses, key, is_int)
        if not ws or not ls:
            continue
        # Find direction of separation
        if ws['med'] < ls['med']:
            # Wins are LOWER — set max threshold
            thresh = ws['q3']  # 75th percentile of wins
            print(f'  {key}: WINS lower. Suggest max <= {thresh:.3f} (catches 75% of past big wins, rejects ~50% of losses)')
        else:
            thresh = ws['q1']  # 25th percentile of wins
            print(f'  {key}: WINS higher. Suggest min >= {thresh:.3f} (catches 75% of past big wins, rejects ~50% of losses)')
    print()

    # Lane breakdown
    print('=' * 100)
    print('BIG WIN BY LANE')
    print('=' * 100)
    by_lane = defaultdict(list)
    for o, s, c, run, pnl in big_wins:
        by_lane[o.get('lane') or '?'].append(pnl)
    for lane, pnls in sorted(by_lane.items(), key=lambda x: -sum(x[1])):
        print(f'  {lane:25s} n={len(pnls)}  total +{sum(pnls):.4f} SOL  avg +{mean(pnls):.4f}')

if __name__ == '__main__':
    main()
