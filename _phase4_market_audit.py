"""Phase 4 dry-live market audit.

Two questions the user wants answered:
1. ENTRY: of all mints in the raw stream, how many pumped 5x+? Of those, which
   did the bot strike? Of those it didn't strike — why? (which feature was the blocker?)
2. EXIT: for the 6 losses, were we slow to kill? Was there an earlier moment
   when the trade clearly went bad and we should have exited?

Inputs: pgg2_phase4_drylive_*_raw.jsonl + _decisions.jsonl
"""

import json
import sys
import os
from collections import defaultdict


RAW = '/root/piggy/data/pgg2_phase4_drylive_20260508_055409_raw.jsonl'
DEC = '/root/piggy/data/pgg2_phase4_drylive_20260508_055409_decisions.jsonl'


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def main():
    # 1. Walk raw.jsonl: track per-mint price tape and create event
    mint_first_price = {}     # mint -> first ever price seen
    mint_first_ts = {}        # mint -> first ts
    mint_max_price = {}       # mint -> max price across all observations
    mint_max_ts = {}
    mint_creates = set()
    mint_first_buy_count = defaultdict(int)
    mint_first_buy_sol = defaultdict(float)
    mint_features_at_peak = {}

    raw_count = 0
    for line in open(RAW):
        x = safe_json(line)
        if not x:
            continue
        raw_count += 1
        m = x.get('mint')
        if not m:
            continue
        ts = int(x.get('ts_ms') or 0)
        is_create = x.get('is_create') or x.get('event_type') == 'create' or x.get('kind') == 'create'
        if is_create:
            mint_creates.add(m)
        p = x.get('price') or x.get('curve_price')
        try:
            p = float(p)
        except Exception:
            continue
        if p <= 0:
            continue
        if m not in mint_first_price:
            mint_first_price[m] = p
            mint_first_ts[m] = ts
        if m not in mint_max_price or p > mint_max_price[m]:
            mint_max_price[m] = p
            mint_max_ts[m] = ts
        if x.get('is_buy'):
            mint_first_buy_count[m] += 1
            mint_first_buy_sol[m] += float(x.get('sol') or 0)

    # Compute peak_mult per mint
    peaks = []
    for m, fp in mint_first_price.items():
        mp = mint_max_price.get(m, fp)
        peak_mult = mp / fp if fp > 0 else 1.0
        first_to_peak_sec = (mint_max_ts[m] - mint_first_ts[m]) / 1000.0
        peaks.append({
            'mint': m,
            'first_price': fp,
            'max_price': mp,
            'peak_mult': peak_mult,
            'time_to_peak_sec': first_to_peak_sec,
            'first_buys': mint_first_buy_count[m],
            'first_buy_sol_total': mint_first_buy_sol[m],
        })

    print(f'Total raw events: {raw_count}')
    print(f'Mints with at least 1 price observation: {len(peaks)}')
    print()

    # Distribution of peak multiples
    n_2x = sum(1 for p in peaks if p['peak_mult'] >= 2.0)
    n_3x = sum(1 for p in peaks if p['peak_mult'] >= 3.0)
    n_5x = sum(1 for p in peaks if p['peak_mult'] >= 5.0)
    n_10x = sum(1 for p in peaks if p['peak_mult'] >= 10.0)
    print(f'Mints that pumped >=2x:  {n_2x}')
    print(f'Mints that pumped >=3x:  {n_3x}')
    print(f'Mints that pumped >=5x:  {n_5x}')
    print(f'Mints that pumped >=10x: {n_10x}')
    print()

    # 2. Walk decisions.jsonl: list mints we struck/opened/closed
    bot_mints = set()
    opens = {}
    closes = []
    strikes = []
    for line in open(DEC):
        x = safe_json(line)
        if not x:
            continue
        m = x.get('mint')
        k = x.get('kind')
        if not m:
            continue
        bot_mints.add(m)
        if k == 'strike_plan':
            strikes.append(x)
        elif k == 'open':
            opens[m] = x
        elif k == 'close':
            closes.append(x)

    bot_struck = set(s['mint'] for s in strikes)
    bot_opened = set(opens.keys())

    # Of 5x+ moonshots, how many did the bot even SEE/STRIKE?
    print('=' * 100)
    print('TOP 30 MOONSHOTS IN THE TAPE (>=3x peak) — DID WE STRIKE?')
    print('=' * 100)
    print(f'{"mint":>10s}  {"peak":>6s}  {"time_to_peak":>12s}  {"bot_struck":>10s}  {"bot_opened":>10s}')
    for p in sorted(peaks, key=lambda x: -x['peak_mult'])[:30]:
        struck = 'YES' if p['mint'] in bot_struck else '---'
        opened = 'YES' if p['mint'] in bot_opened else '---'
        print(f'  {p["mint"][:10]:>10s}  {p["peak_mult"]:>6.2f}  {p["time_to_peak_sec"]:>12.1f}  {struck:>10s}  {opened:>10s}')
    print()

    # Stats: of 3x+ peaks, what fraction did we strike?
    big_peaks = [p for p in peaks if p['peak_mult'] >= 3.0]
    if big_peaks:
        n_struck = sum(1 for p in big_peaks if p['mint'] in bot_struck)
        n_opened = sum(1 for p in big_peaks if p['mint'] in bot_opened)
        print(f'Of {len(big_peaks)} mints that 3x+: bot struck {n_struck}, opened {n_opened}  (capture: {n_opened*100/len(big_peaks):.0f}%)')
    big5 = [p for p in peaks if p['peak_mult'] >= 5.0]
    if big5:
        n5_o = sum(1 for p in big5 if p['mint'] in bot_opened)
        print(f'Of {len(big5)} mints that 5x+: bot opened {n5_o}  (capture: {n5_o*100/len(big5):.0f}%)')
    print()

    # 3. The 6 losses — were they slow to kill?
    print('=' * 100)
    print('LOSSES — entry-to-exit timeline; was hard_break too slow?')
    print('=' * 100)
    losers = [c for c in closes if float(c.get('pnl_sol') or 0) < 0]
    print(f'{"mint":>10s}  {"reason":40s}  {"pnl":>8s}  {"duration":>9s}  {"close_m250":>10s}  {"in_peak":>8s}')
    for c in losers:
        m = c['mint']
        o = opens.get(m, {})
        o_ts = int(o.get('ts_ms') or 0)
        c_ts = int(c.get('ts_ms') or 0)
        duration = (c_ts - o_ts) / 1000.0
        cf = c.get('features') or {}
        # peek into raw tape for the mint to find post-open peak
        peak_obj = next((p for p in peaks if p['mint'] == m), None)
        in_peak = peak_obj['peak_mult'] if peak_obj else 1.0
        print(f'  {m[:10]:>10s}  {c.get("reason","?")[:40]:40s}  {float(c.get("pnl_sol") or 0):>+8.4f}  {duration:>8.1f}s  {cf.get("move250",1):>10.3f}  {in_peak:>8.2f}')
    print()

    # 4. The 3 wins — what made them work?
    print('=' * 100)
    print('WINS — was the moonshot-rider firing correctly?')
    print('=' * 100)
    winners = [c for c in closes if float(c.get('pnl_sol') or 0) > 0]
    print(f'{"mint":>10s}  {"reason":50s}  {"pnl":>8s}  {"duration":>9s}  {"close_m250":>10s}')
    for c in winners:
        m = c['mint']
        o = opens.get(m, {})
        o_ts = int(o.get('ts_ms') or 0)
        c_ts = int(c.get('ts_ms') or 0)
        duration = (c_ts - o_ts) / 1000.0
        cf = c.get('features') or {}
        print(f'  {m[:10]:>10s}  {c.get("reason","?")[:50]:50s}  {float(c.get("pnl_sol") or 0):>+8.4f}  {duration:>8.1f}s  {cf.get("move250",1):>10.3f}')
    print()

    # 5. Of moonshots (3x+) that we DIDN'T strike, what was their first-buy-sol distribution?
    print('=' * 100)
    print('MOONSHOTS WE MISSED (>=3x, NOT struck) — what features did they have?')
    print('=' * 100)
    missed = [p for p in peaks if p['peak_mult'] >= 3.0 and p['mint'] not in bot_struck]
    print(f'count: {len(missed)}')
    if missed:
        first_buys = sorted([p['first_buys'] for p in missed])
        first_sol = sorted([p['first_buy_sol_total'] for p in missed])
        ttp = sorted([p['time_to_peak_sec'] for p in missed])
        print(f'  first_buy COUNT distribution: min={first_buys[0]} med={first_buys[len(first_buys)//2]} max={first_buys[-1]}')
        print(f'  first_buy SOL TOTAL distribution: min={first_sol[0]:.2f} med={first_sol[len(first_sol)//2]:.2f} max={first_sol[-1]:.2f}')
        print(f'  time-to-peak distribution: min={ttp[0]:.1f}s med={ttp[len(ttp)//2]:.1f}s max={ttp[-1]:.1f}s')
        print()
        print('Top 10 missed moonshots:')
        for p in sorted(missed, key=lambda x: -x['peak_mult'])[:10]:
            print(f'  {p["mint"][:12]} peak={p["peak_mult"]:.2f}x time_to_peak={p["time_to_peak_sec"]:.1f}s buys={p["first_buys"]} sol={p["first_buy_sol_total"]:.2f}')


if __name__ == '__main__':
    main()
