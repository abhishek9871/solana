"""For every (open, close) trade across all live runs, walk the raw.jsonl tape
and compute the ACTUAL post-entry peak the mint reached, then compare to where
the bot exited. Quantifies "profit left on the table."

Approach:
- For each decisions.jsonl, find its matching raw.jsonl (same timestamp prefix).
- For each (open, close) pair, extract from raw all `swap` / price events in
  [open_ts - 1000ms, close_ts + 120_000ms] for that mint.
- Find max(price) in that window. Compute peak_mult = peak_price / open_price.
- Compare to actual realized mult (close_price / open_price).
- Tally: how many trades had peak_mult >= 1.5x, 2x, 3x, 5x post-entry that we DIDN'T capture?
"""
import glob
import json
import os
from collections import defaultdict


DATA_DIR = '/root/piggy/data'


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_decisions(decisions_path):
    """Pair opens and closes."""
    opens = {}
    pairs = []
    for line in open(decisions_path):
        x = safe_json(line)
        if not x:
            continue
        k = x.get('kind')
        m = x.get('mint')
        if k == 'open':
            opens[m] = x
        elif k == 'close':
            o = opens.pop(m, None)
            if o:
                pairs.append((o, x))
    return pairs


def extract_prices_for_mint(raw_path, mint, t_start, t_end):
    """Walk raw.jsonl, return sorted [(ts_ms, price)] for `mint` in window."""
    prices = []
    if not os.path.exists(raw_path):
        return prices
    for line in open(raw_path):
        if mint not in line:
            continue
        x = safe_json(line)
        if not x:
            continue
        if x.get('mint') != mint:
            continue
        ts = int(x.get('ts_ms') or 0)
        if ts < t_start or ts > t_end:
            continue
        # raw events have a price field if curve was loaded
        p = x.get('price') or x.get('curve_price')
        try:
            p = float(p)
        except Exception:
            continue
        if p > 0:
            prices.append((ts, p))
    prices.sort()
    return prices


def main():
    decision_paths = sorted(glob.glob(os.path.join(DATA_DIR, 'pgg2_direct_live_*_decisions.jsonl')))
    all_rows = []
    for dpath in decision_paths:
        prefix = dpath.replace('_decisions.jsonl', '')
        rpath = prefix + '_raw.jsonl'
        if not os.path.exists(rpath):
            continue
        pairs = load_decisions(dpath)
        if not pairs:
            continue

        # Group pairs by mint, single pass through raw to extract prices.
        by_mint = defaultdict(list)
        for o, c in pairs:
            by_mint[o['mint']].append((o, c))

        # Build window per mint: earliest open - 1s, latest close + 120s
        windows = {}
        for m, items in by_mint.items():
            t_min = min(int(o.get('ts_ms') or 0) for o, _ in items) - 1000
            t_max = max(int(c.get('ts_ms') or 0) for _, c in items) + 120000
            windows[m] = (t_min, t_max)

        # Now scan raw once
        prices_by_mint = defaultdict(list)
        for line in open(rpath):
            x = safe_json(line)
            if not x:
                continue
            m = x.get('mint')
            if m not in windows:
                continue
            ts = int(x.get('ts_ms') or 0)
            tmin, tmax = windows[m]
            if ts < tmin or ts > tmax:
                continue
            p = x.get('price') or x.get('curve_price')
            try:
                p = float(p)
            except Exception:
                continue
            if p > 0:
                prices_by_mint[m].append((ts, p))

        # For each (open, close) compute the trajectory analysis
        for o, c in pairs:
            m = o['mint']
            o_ts = int(o.get('ts_ms') or 0)
            c_ts = int(c.get('ts_ms') or 0)
            of = o.get('features') or {}
            open_price = float(of.get('price') or 0)
            if open_price <= 0:
                continue

            mint_prices = sorted(prices_by_mint.get(m, []))
            # in-trade peak
            in_trade = [(ts, p) for ts, p in mint_prices if o_ts <= ts <= c_ts]
            # post-close window (60 seconds after close)
            post_close = [(ts, p) for ts, p in mint_prices if c_ts < ts <= c_ts + 60000]
            # Plus 30s before open to confirm pre-entry move
            pre_open = [(ts, p) for ts, p in mint_prices if o_ts - 30000 <= ts <= o_ts]

            in_peak = max((p for _, p in in_trade), default=open_price)
            post_peak = max((p for _, p in post_close), default=0.0)

            close_price = mint_prices[-1][1] if mint_prices else open_price
            # Try: use close ts to find nearest price
            close_actual = open_price
            for ts, p in mint_prices:
                if ts <= c_ts:
                    close_actual = p
                else:
                    break

            in_peak_mult = in_peak / open_price if open_price > 0 else 1.0
            post_peak_mult = post_peak / open_price if (open_price > 0 and post_peak > 0) else 0.0
            close_mult = close_actual / open_price if open_price > 0 else 1.0

            all_rows.append({
                'run': os.path.basename(prefix),
                'mint': m[:8],
                'lane': o.get('lane') or '',
                'reason_close': c.get('reason') or '',
                'pnl': float(c.get('pnl_sol') or 0),
                'open_price': open_price,
                'in_peak_mult': in_peak_mult,
                'close_mult': close_mult,
                'post_peak_mult': post_peak_mult,
                'duration_sec': (c_ts - o_ts) / 1000.0,
            })

    if not all_rows:
        print('no rows extracted')
        return

    # Summary
    print(f'Trade-with-trajectory rows: {len(all_rows)}')
    n_3x_in = sum(1 for r in all_rows if r['in_peak_mult'] >= 3.0)
    n_2x_in = sum(1 for r in all_rows if r['in_peak_mult'] >= 2.0)
    n_15x_in = sum(1 for r in all_rows if r['in_peak_mult'] >= 1.5)
    n_3x_post = sum(1 for r in all_rows if r['post_peak_mult'] >= 3.0)
    n_2x_post = sum(1 for r in all_rows if r['post_peak_mult'] >= 2.0)
    n_15x_post = sum(1 for r in all_rows if r['post_peak_mult'] >= 1.5)
    print()
    print('IN-TRADE peak (price during the position):')
    print(f'  >=1.5x: {n_15x_in}  >=2.0x: {n_2x_in}  >=3.0x: {n_3x_in}')
    print('POST-CLOSE peak (price within 60s AFTER bot exited):')
    print(f'  >=1.5x: {n_15x_post}  >=2.0x: {n_2x_post}  >=3.0x: {n_3x_post}')
    print()

    # Trades where in-trade peak >= 1.5x (we COULD have ridden to 1.5x+)
    print('=' * 100)
    print('TRADES THAT PEAKED >= 1.5x DURING THE POSITION (sorted by missed profit)')
    print('=' * 100)
    print(f'{"run":>30s}  {"mint":>8s}  {"reason":35s}  {"in_peak":>7s}  {"close":>7s}  {"post_peak":>9s}  {"pnl":>8s}')
    rides = sorted([r for r in all_rows if r['in_peak_mult'] >= 1.5], key=lambda r: -r['in_peak_mult'])
    for r in rides[:25]:
        print(f'  {r["run"][-20:]:>30s}  {r["mint"]:>8s}  {r["reason_close"][:35]:35s}  {r["in_peak_mult"]:>7.3f}  {r["close_mult"]:>7.3f}  {r["post_peak_mult"]:>9.3f}  {r["pnl"]:>+8.4f}')
    print()

    # Trades where post-close peak >= 2x (we exited and the mint then ran)
    print('=' * 100)
    print('TRADES WHERE PRICE RAN >=2x AFTER BOT EXITED (60s window)')
    print('=' * 100)
    print(f'{"run":>30s}  {"mint":>8s}  {"reason":35s}  {"in_peak":>7s}  {"close":>7s}  {"post_peak":>9s}  {"pnl":>8s}')
    runaways = sorted([r for r in all_rows if r['post_peak_mult'] >= 2.0], key=lambda r: -r['post_peak_mult'])
    for r in runaways[:25]:
        print(f'  {r["run"][-20:]:>30s}  {r["mint"]:>8s}  {r["reason_close"][:35]:35s}  {r["in_peak_mult"]:>7.3f}  {r["close_mult"]:>7.3f}  {r["post_peak_mult"]:>9.3f}  {r["pnl"]:>+8.4f}')
    print()

    # By close reason: avg in_peak vs close
    print('=' * 100)
    print('BY CLOSE REASON: avg in_peak_mult and close_mult (the gap = profit cut short)')
    print('=' * 100)
    by_reason = defaultdict(list)
    for r in all_rows:
        by_reason[r['reason_close']].append(r)
    print(f'{"reason":40s}  {"n":>4s}  {"avg_in_peak":>12s}  {"avg_close":>10s}  {"avg_post":>9s}  {"net_pnl":>9s}')
    rows = []
    for r, items in by_reason.items():
        n = len(items)
        avg_in = sum(x['in_peak_mult'] for x in items) / n
        avg_close = sum(x['close_mult'] for x in items) / n
        post_vals = [x['post_peak_mult'] for x in items if x['post_peak_mult'] > 0]
        avg_post = sum(post_vals) / len(post_vals) if post_vals else 0.0
        net = sum(x['pnl'] for x in items)
        rows.append((r, n, avg_in, avg_close, avg_post, net))
    rows.sort(key=lambda r: -r[1])
    for r in rows:
        print(f'  {r[0][:40]:40s}  {r[1]:>4d}  {r[2]:>12.3f}  {r[3]:>10.3f}  {r[4]:>9.3f}  {r[5]:>+9.4f}')


if __name__ == '__main__':
    main()
