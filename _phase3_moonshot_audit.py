"""Decode the WINNING perspective from all live runs.

Three questions:
1. Of trades we WON, how short did we cut them? (peak_mult at close vs realized mult)
2. Of trades we struck and lost, was the *mint itself* a moonshot post-exit? (raw tape)
3. Of mints that pumped 5x+ in the raw tape, which did we never strike? Why?

Inputs: all pgg2_direct_live_*_decisions.jsonl + matching _raw.jsonl in /root/piggy/data/.
Outputs: structured findings to stdout.
"""

import glob
import json
import os
from collections import Counter, defaultdict

DATA_DIR = '/root/piggy/data'
DECISION_GLOB = os.path.join(DATA_DIR, 'pgg2_direct_live_*_decisions.jsonl')


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def collect_trades():
    """Walk every decision log, return list of complete (open, close) trade pairs."""
    trades = []
    for path in sorted(glob.glob(DECISION_GLOB)):
        run = os.path.basename(path).replace('_decisions.jsonl', '')
        opens = {}
        for line in open(path):
            x = safe_json(line)
            if not x:
                continue
            k = x.get('kind')
            mint = x.get('mint')
            if k == 'open':
                opens[mint] = x
            elif k == 'close':
                op = opens.pop(mint, None)
                if not op:
                    continue
                of = op.get('features') or {}
                cf = x.get('features') or {}
                trades.append({
                    'run': run,
                    'mint': mint,
                    'open_ts': int(op.get('ts_ms') or 0),
                    'close_ts': int(x.get('ts_ms') or 0),
                    'lane': op.get('lane') or '',
                    'pnl': float(x.get('pnl_sol') or 0),
                    'reason_close': x.get('reason') or '',
                    # entry features
                    'entry_move': float(of.get('priced_snap_entry_move') or 0),
                    'first_buy': float(of.get('first_buy_sol') or 0),
                    'top1500': float(of.get('top_share1500') or 0),
                    'top700': float(of.get('top_share700') or 0),
                    'buy1500': float(of.get('buy1500') or 0),
                    'uniq1500': int(of.get('uniq1500') or 0),
                    'age_ms': int(of.get('age_ms') or 0),
                    'vsol': float(of.get('vsol_sol') or 0),
                    'slot_buyers': int(of.get('slot_buyers') or 0),
                    # close features (what bot saw at exit)
                    'close_move250': float(cf.get('move250') or 1.0),
                    'close_move700': float(cf.get('move700') or 1.0),
                    'close_move1500': float(cf.get('move1500') or 1.0),
                    'close_buy1500': float(cf.get('buy1500') or 0),
                    'close_uniq1500': int(cf.get('uniq1500') or 0),
                    'close_top1500': float(cf.get('top_share1500') or 0),
                    'close_vsol': float(cf.get('vsol_sol') or 0),
                })
    return trades


def main():
    trades = collect_trades()
    print(f'Total complete (open,close) trade pairs: {len(trades)}')
    if not trades:
        return

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    print(f'Wins:   {len(wins)}  total +{sum(w["pnl"] for w in wins):+.5f} SOL')
    print(f'Losses: {len(losses)}  total {sum(l["pnl"] for l in losses):+.5f} SOL')
    print(f'NET:    {sum(t["pnl"] for t in trades):+.5f} SOL')
    print()

    # 1. How short did we cut winners? Group by close reason.
    print('=' * 90)
    print('1. WINNER EXIT PATHWAYS — what reason captured most $, what cut early?')
    print('=' * 90)
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t['reason_close']].append(t)
    rows = []
    for r, ts in by_reason.items():
        n = len(ts)
        net = sum(t['pnl'] for t in ts)
        n_win = sum(1 for t in ts if t['pnl'] > 0)
        avg_pnl = net / n
        # what mult was the bot AT when it closed?
        avg_close_move250 = sum(t['close_move250'] for t in ts) / n
        max_close_move250 = max(t['close_move250'] for t in ts)
        max_pnl = max(t['pnl'] for t in ts)
        rows.append((r, n, n_win, net, avg_pnl, avg_close_move250, max_close_move250, max_pnl))
    rows.sort(key=lambda r: -r[3])
    print(f'{"reason":40s} {"n":>4s} {"W":>4s} {"net":>9s} {"avg":>8s} {"avg_m250":>9s} {"max_m250":>9s} {"max_pnl":>8s}')
    for r in rows[:20]:
        print(f'{r[0][:40]:40s} {r[1]:>4d} {r[2]:>4d} {r[3]:>+9.4f} {r[4]:>+8.4f} {r[5]:>9.3f} {r[6]:>9.3f} {r[7]:>+8.4f}')
    print()

    # 2. Top 20 BIGGEST WINS — decode them.
    print('=' * 90)
    print('2. TOP 20 BIGGEST WINS — features at entry, peak at close, exit reason')
    print('=' * 90)
    print(f'{"pnl":>8s} {"reason":35s} {"em":>5s} {"top1500":>7s} {"buy1500":>7s} {"uniq":>5s} {"age_s":>5s} {"vsol":>6s} {"close_m250":>10s}')
    for t in sorted(wins, key=lambda t: -t['pnl'])[:20]:
        age_s = t['age_ms'] / 1000.0
        print(f'{t["pnl"]:>+8.4f} {t["reason_close"][:35]:35s} {t["entry_move"]:>5.2f} {t["top1500"]:>7.3f} {t["buy1500"]:>7.2f} {t["uniq1500"]:>5d} {age_s:>5.1f} {t["vsol"]:>6.1f} {t["close_move250"]:>10.3f}')
    print()

    # 3. Top 20 BIGGEST LOSSES
    print('=' * 90)
    print('3. TOP 20 BIGGEST LOSSES')
    print('=' * 90)
    print(f'{"pnl":>8s} {"reason":35s} {"em":>5s} {"top1500":>7s} {"buy1500":>7s} {"uniq":>5s} {"age_s":>5s} {"vsol":>6s} {"close_m250":>10s}')
    for t in sorted(losses, key=lambda t: t['pnl'])[:20]:
        age_s = t['age_ms'] / 1000.0
        print(f'{t["pnl"]:>+8.4f} {t["reason_close"][:35]:35s} {t["entry_move"]:>5.2f} {t["top1500"]:>7.3f} {t["buy1500"]:>7.2f} {t["uniq1500"]:>5d} {age_s:>5.1f} {t["vsol"]:>6.1f} {t["close_move250"]:>10.3f}')
    print()

    # 4. WINNERS' ENTRY FEATURE distributions vs LOSERS'
    print('=' * 90)
    print('4. ENTRY-FEATURE STATS: WINNERS vs LOSERS (priced_snap only)')
    print('=' * 90)
    pw = [t for t in wins if t['lane'] == 'priced_snap']
    pl = [t for t in losses if t['lane'] == 'priced_snap']

    def stats(rows, key):
        vals = sorted([r[key] for r in rows])
        if not vals:
            return (0, 0, 0, 0, 0)
        n = len(vals)
        return (
            min(vals),
            vals[n // 4],
            vals[n // 2],
            vals[(3 * n) // 4],
            max(vals),
        )

    print(f'priced_snap winners n={len(pw)}, losers n={len(pl)}')
    print(f'{"feature":18s}  {"WIN min/q1/med/q3/max":>40s}     {"LOSS min/q1/med/q3/max":>40s}')
    for key in ['entry_move', 'top1500', 'buy1500', 'uniq1500', 'age_ms', 'vsol', 'first_buy', 'slot_buyers']:
        w_s = stats(pw, key)
        l_s = stats(pl, key)
        if key == 'age_ms':
            fmt = lambda v: f'{v/1000:.1f}s'
        elif key in ('entry_move', 'top1500'):
            fmt = lambda v: f'{v:.3f}'
        elif key in ('buy1500', 'vsol', 'first_buy'):
            fmt = lambda v: f'{v:.2f}'
        else:
            fmt = lambda v: f'{int(v)}'
        w_str = '/'.join(fmt(v) for v in w_s)
        l_str = '/'.join(fmt(v) for v in l_s)
        print(f'  {key:16s}  {w_str:>40s}     {l_str:>40s}')
    print()

    # 5. The "moonshots" by close_move250 — trades where price moved a LOT after entry (that the bot saw).
    print('=' * 90)
    print('5. TRADES WHERE close_move250 >= 1.30 (ride happened — did we capture it?)')
    print('=' * 90)
    big_moves = [t for t in trades if t['close_move250'] >= 1.30]
    print(f'count: {len(big_moves)}')
    print(f'{"pnl":>8s} {"reason":35s} {"em":>5s} {"close_m250":>10s} {"close_m700":>10s} {"close_m1500":>11s}')
    for t in sorted(big_moves, key=lambda t: -t['close_move250'])[:30]:
        print(f'{t["pnl"]:>+8.4f} {t["reason_close"][:35]:35s} {t["entry_move"]:>5.2f} {t["close_move250"]:>10.3f} {t["close_move700"]:>10.3f} {t["close_move1500"]:>11.3f}')
    print()

    # 6. quote_profit_bank trades — what mult were we at when we banked?
    print('=' * 90)
    print('6. quote_profit_bank EXITS — average peak captured (cut early?)')
    print('=' * 90)
    qpb = [t for t in trades if t['reason_close'] == 'quote_profit_bank']
    if qpb:
        avg_pnl = sum(t['pnl'] for t in qpb) / len(qpb)
        avg_close_m250 = sum(t['close_move250'] for t in qpb) / len(qpb)
        print(f'  n={len(qpb)} avg_pnl={avg_pnl:+.4f} avg_close_move250={avg_close_m250:.3f}')
        # distribution of close_move250
        sorted_m = sorted([t['close_move250'] for t in qpb])
        n = len(sorted_m)
        if n:
            print(f'  close_move250 quartiles: min={sorted_m[0]:.3f} q1={sorted_m[n//4]:.3f} med={sorted_m[n//2]:.3f} q3={sorted_m[(3*n)//4]:.3f} max={sorted_m[-1]:.3f}')
            n_above_15 = sum(1 for v in sorted_m if v >= 1.5)
            n_above_20 = sum(1 for v in sorted_m if v >= 2.0)
            print(f'  trades cashed BEFORE close_move250 hit 1.50: {len(qpb) - n_above_15}/{len(qpb)} ({(len(qpb)-n_above_15)*100/len(qpb):.0f}%)')
            print(f'  trades cashed BEFORE close_move250 hit 2.00: {len(qpb) - n_above_20}/{len(qpb)} ({(len(qpb)-n_above_20)*100/len(qpb):.0f}%)')
    print()


if __name__ == '__main__':
    main()
