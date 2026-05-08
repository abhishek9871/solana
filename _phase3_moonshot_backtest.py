"""Replay every trade with the moonshot-rider exit logic. Compare to actual.

For each (open, close) pair:
1. Walk raw.jsonl tape from open_ts to (close_ts + 60s).
2. Simulate the new exit logic tick-by-tick:
   - Track peak_mult, last_mult.
   - LATCH moonshot_mode when peak >= 1.30 within 30s of open.
   - If in moonshot_mode: exit when last_mult <= peak_mult * 0.50 OR after 90s OR at end of price tape.
   - If never latched: respect the actual close (use real exit price).
3. Compute simulated PnL using the simulated exit price.

Cost model: pump.fun / pumpswap drag ~1.8% buy-side + ~1% protocol = ~2.8% round-trip drag.
We use simple proportional drag: simulated_pnl = position_sol * (exit_mult - 1) - position_sol * drag_round_trip.
"""

import glob
import json
import os
from collections import defaultdict


DATA_DIR = '/root/piggy/data'
ARM_PEAK = 1.30
ARM_WINDOW_SEC = 30.0
HARD_TIMEOUT_SEC = 90.0
DRAG_ROUND_TRIP = 0.028  # ~1.8% buy + ~1% sell drag (cost-conservative)


def trail_for_peak(peak: float) -> float:
    """Tiered trail: tighter for low peaks (cut whipsaw), wider for high peaks (ride moonshot)."""
    if peak >= 3.0:
        return 0.40   # lock 60%+ — ride 3x+ moonshots hard
    if peak >= 2.0:
        return 0.50   # lock 50%
    if peak >= 1.60:
        return 0.70   # lock 30%
    return 0.85       # lock 15% — tight cut for low-peak whipsaws (1.30-1.60 range)


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_decisions(decisions_path):
    """Pair opens and closes; carry strike_plan scout_sol onto the open."""
    opens = {}
    last_strike = {}  # most recent strike_plan scout_sol per mint
    pairs = []
    for line in open(decisions_path):
        x = safe_json(line)
        if not x:
            continue
        k = x.get('kind')
        m = x.get('mint')
        if k == 'strike_plan':
            last_strike[m] = float(x.get('scout_sol') or 0)
        elif k == 'open':
            x['_strike_scout_sol'] = last_strike.get(m, 0.0)
            opens[m] = x
        elif k == 'close':
            o = opens.pop(m, None)
            if o:
                pairs.append((o, x))
    return pairs


def simulate_exit(prices, open_price, open_ts, real_close_ts):
    """Walk the price tape and simulate the moonshot-rider exit.

    Returns (sim_exit_mult, sim_exit_ts, latched, exit_reason).
    """
    if not prices:
        return (1.0, real_close_ts, False, 'no_tape')

    peak = 1.0
    last = 1.0
    latched = False
    arm_ts = 0
    arm_peak = 1.0
    last_ts = open_ts

    for ts, p in prices:
        if ts < open_ts:
            continue
        last_ts = ts
        m = p / open_price
        last = m
        if m > peak:
            peak = m
        age_sec = (ts - open_ts) / 1000.0

        # Latch check
        if not latched and peak >= ARM_PEAK and age_sec <= ARM_WINDOW_SEC:
            latched = True
            arm_ts = ts
            arm_peak = peak

        # In moonshot mode: trail or timeout
        if latched:
            trail_floor = peak * trail_for_peak(peak)
            if last <= trail_floor:
                return (last, ts, latched, f'moonshot_trail (peak={peak:.2f} trail={trail_for_peak(peak):.2f})')
            if age_sec >= HARD_TIMEOUT_SEC:
                return (last, ts, latched, f'moonshot_hard_timeout (peak={peak:.2f})')
        # Pre-latch: simulate continuing — for backtest fairness, if not latched
        # use the bot's actual exit (passed in as real_close_ts). We don't simulate
        # the unlatched branch here; we just track until we hit real_close_ts.
        if not latched and ts >= real_close_ts:
            return (last, ts, latched, 'real_close (no_latch)')

    # Tape ran out without a trail trigger — use last seen
    if latched:
        return (last, last_ts, latched, f'moonshot_tape_end (peak={peak:.2f})')
    return (last, last_ts, latched, 'tape_end_no_latch')


def main():
    decision_paths = sorted(glob.glob(os.path.join(DATA_DIR, 'pgg2_direct_live_*_decisions.jsonl')))

    sim_rows = []
    for dpath in decision_paths:
        prefix = dpath.replace('_decisions.jsonl', '')
        rpath = prefix + '_raw.jsonl'
        if not os.path.exists(rpath):
            continue
        pairs = load_decisions(dpath)
        if not pairs:
            continue

        # Build mint windows (open_ts - 1s, real_close_ts + 90s)
        windows = {}
        by_mint = defaultdict(list)
        for o, c in pairs:
            m = o['mint']
            o_ts = int(o.get('ts_ms') or 0)
            c_ts = int(c.get('ts_ms') or 0)
            by_mint[m].append((o, c))
            t_min = o_ts - 1000
            t_max = c_ts + int(HARD_TIMEOUT_SEC * 1000) + 1000
            if m not in windows:
                windows[m] = (t_min, t_max)
            else:
                wmin, wmax = windows[m]
                windows[m] = (min(wmin, t_min), max(wmax, t_max))

        # Single-pass scan raw
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
        for m in prices_by_mint:
            prices_by_mint[m].sort()

        # Simulate each pair
        for o, c in pairs:
            m = o['mint']
            o_ts = int(o.get('ts_ms') or 0)
            c_ts = int(c.get('ts_ms') or 0)
            of = o.get('features') or {}
            open_price = float(of.get('price') or 0)
            if open_price <= 0:
                continue
            position_sol = float(o.get('_strike_scout_sol') or 0)
            if position_sol <= 0:
                position_sol = 0.04  # fallback typical scout
            actual_pnl = float(c.get('pnl_sol') or 0)
            actual_reason = c.get('reason') or ''

            mint_prices = prices_by_mint.get(m, [])
            sim_mult, sim_ts, latched, sim_reason = simulate_exit(mint_prices, open_price, o_ts, c_ts)

            if latched:
                # Use position_sol from strike_plan as a baseline; this UNDER-estimates wins on
                # trades that scaled up, but is consistent across all latched trades.
                sim_pnl = position_sol * (sim_mult - 1) - position_sol * DRAG_ROUND_TRIP
            else:
                sim_pnl = actual_pnl

            sim_rows.append({
                'mint': m[:8],
                'lane': o.get('lane') or '',
                'actual_reason': actual_reason,
                'sim_reason': sim_reason,
                'latched': latched,
                'position_sol': position_sol,
                'open_price': open_price,
                'actual_pnl': actual_pnl,
                'sim_mult': sim_mult,
                'sim_pnl': sim_pnl,
                'duration_sec': (c_ts - o_ts) / 1000.0,
            })

    if not sim_rows:
        print('no rows')
        return

    print(f'Total trades simulated: {len(sim_rows)}')
    n_latched = sum(1 for r in sim_rows if r['latched'])
    print(f'Trades that LATCHED moonshot_mode: {n_latched}  ({n_latched*100/len(sim_rows):.1f}%)')
    print()

    actual_total = sum(r['actual_pnl'] for r in sim_rows)
    sim_total = sum(r['sim_pnl'] for r in sim_rows)
    delta = sim_total - actual_total
    print(f'Actual NET P&L: {actual_total:+.5f} SOL  (~${actual_total*90:+.2f})')
    print(f'Simulated NET P&L: {sim_total:+.5f} SOL  (~${sim_total*90:+.2f})')
    print(f'DELTA:            {delta:+.5f} SOL  (~${delta*90:+.2f})')
    print()

    # Of latched trades, breakdown
    latched_rows = [r for r in sim_rows if r['latched']]
    if latched_rows:
        actual_lat = sum(r['actual_pnl'] for r in latched_rows)
        sim_lat = sum(r['sim_pnl'] for r in latched_rows)
        print(f'LATCHED trades only (n={len(latched_rows)}):')
        print(f'  Actual: {actual_lat:+.5f}  Simulated: {sim_lat:+.5f}  Delta: {sim_lat - actual_lat:+.5f}')
        print()

    # Top 25 biggest improvements
    deltas = [(r, r['sim_pnl'] - r['actual_pnl']) for r in sim_rows]
    deltas.sort(key=lambda x: -x[1])
    print('=' * 100)
    print('TOP 25 BIGGEST IMPROVEMENTS (sim_pnl - actual_pnl)')
    print('=' * 100)
    print(f'{"mint":>8s}  {"lane":>15s}  {"actual_reason":35s}  {"sim_reason":35s}  {"actual":>9s}  {"sim":>9s}  {"delta":>9s}')
    for r, d in deltas[:25]:
        print(f'  {r["mint"]:>8s}  {r["lane"][:15]:>15s}  {r["actual_reason"][:35]:35s}  {r["sim_reason"][:35]:35s}  {r["actual_pnl"]:>+9.4f}  {r["sim_pnl"]:>+9.4f}  {d:>+9.4f}')
    print()

    # Top 15 regressions (cases where the new logic LOSES money vs actual)
    print('=' * 100)
    print('TOP 15 BIGGEST REGRESSIONS (cases the moonshot-rider would HURT)')
    print('=' * 100)
    deltas.sort(key=lambda x: x[1])
    print(f'{"mint":>8s}  {"lane":>15s}  {"actual_reason":35s}  {"sim_reason":35s}  {"actual":>9s}  {"sim":>9s}  {"delta":>9s}')
    for r, d in deltas[:15]:
        if d >= 0:
            break
        print(f'  {r["mint"]:>8s}  {r["lane"][:15]:>15s}  {r["actual_reason"][:35]:35s}  {r["sim_reason"][:35]:35s}  {r["actual_pnl"]:>+9.4f}  {r["sim_pnl"]:>+9.4f}  {d:>+9.4f}')
    print()


if __name__ == '__main__':
    main()
