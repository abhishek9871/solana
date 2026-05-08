"""Phase 6 per-trade autopsy + entry-frequency diagnosis."""
import json
import os
from collections import defaultdict


DEC = '/root/piggy/data/pgg2_phase6_drylive_20260508_065255_decisions.jsonl'
RAW = '/root/piggy/data/pgg2_phase6_drylive_20260508_065255_raw.jsonl'
ILOG = '/root/piggy/logs/pgg2_phase6_drylive_20260508_065255.log'


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_decisions():
    opens, last_strike = {}, {}
    pairs = []
    raw_log = []
    for line in open(DEC):
        x = safe_json(line)
        if not x:
            continue
        m, k = x.get('mint'), x.get('kind')
        raw_log.append(x)
        if not m:
            continue
        if k == 'strike_plan':
            last_strike[m] = x
        elif k == 'open':
            opens[m] = (x, last_strike.get(m))
        elif k == 'close':
            o = opens.pop(m, (None, None))
            if o[0]:
                pairs.append((o[0], o[1], x))
    return pairs, raw_log


def load_mint_tape(mint, t_start, t_end):
    rows = []
    for line in open(RAW):
        if mint not in line:
            continue
        x = safe_json(line)
        if not x or x.get('mint') != mint:
            continue
        ts = int(x.get('ts_ms') or 0)
        if ts < t_start or ts > t_end:
            continue
        p = x.get('price') or x.get('curve_price') or 0
        try:
            p = float(p)
        except Exception:
            p = 0
        is_buy = bool(x.get('is_buy'))
        sol = float(x.get('sol') or 0)
        rows.append((ts, p, is_buy, sol))
    return sorted(rows)


def diagnose(open_ev, strike_ev, close_ev, tape):
    o_ts = int(open_ev.get('ts_ms') or 0)
    c_ts = int(close_ev.get('ts_ms') or 0)
    of = open_ev.get('features') or {}
    cf = close_ev.get('features') or {}
    open_price = float(of.get('price') or 0)
    pnl = float(close_ev.get('pnl_sol') or 0)
    reason = close_ev.get('reason') or ''
    duration_sec = (c_ts - o_ts) / 1000.0

    in_tape = [r for r in tape if o_ts <= r[0] <= c_ts]
    post_tape = [r for r in tape if c_ts < r[0] <= c_ts + 90000]

    in_peak = max((r[1] for r in in_tape if r[1] > 0), default=open_price)
    post_peak = max((r[1] for r in post_tape if r[1] > 0), default=0.0)
    in_peak_mult = in_peak / open_price if open_price > 0 else 1.0
    post_peak_mult = post_peak / open_price if (open_price > 0 and post_peak > 0) else 0.0

    in_peak_ts = next((r[0] for r in in_tape if r[1] >= in_peak * 0.999), o_ts)
    in_peak_age_sec = (in_peak_ts - o_ts) / 1000.0

    # Find when min-hold expired (age 12s) - what was the price at that point?
    min_hold_ts = o_ts + 12000
    price_at_12s = None
    for r in in_tape:
        if r[0] >= min_hold_ts and r[1] > 0:
            price_at_12s = r[1]
            break
    mult_at_12s = price_at_12s / open_price if (price_at_12s and open_price > 0) else None

    pre_close_buys = [r for r in tape if c_ts - 1500 <= r[0] <= c_ts and r[2]]
    buy_count_pre_close = len(pre_close_buys)
    buy_sol_pre_close = sum(r[3] for r in pre_close_buys)

    print(f'  REASON: {reason}')
    print(f'  PnL: {pnl:+.5f} SOL  (~${pnl*90:+.2f})')
    print(f'  Duration: {duration_sec:.2f}s  (min-hold floor was 12s)')
    print(f'  Open price: {open_price:.6e}')
    print(f'  IN-position peak: {in_peak_mult:.3f}x  at age={in_peak_age_sec:.1f}s')
    print(f'  Mult at 12s (min-hold expiry): {mult_at_12s:.3f}x' if mult_at_12s else '  Mult at 12s: (no data)')
    print(f'  POST-close peak (90s): {post_peak_mult:.3f}x')
    print(f'  Pre-close buys (last 1.5s): {buy_count_pre_close} buys, {buy_sol_pre_close:.2f} SOL')
    print(f'  OPEN feat: lane={open_ev.get("lane")} entry_move={of.get("priced_snap_entry_move",0):.3f} top1500={of.get("top_share1500",0):.3f} buy1500={of.get("buy1500",0):.2f} uniq1500={of.get("uniq1500",0)} age_ms={of.get("age_ms",0)} vsol={of.get("vsol_sol",0):.1f}')
    if strike_ev:
        sf = strike_ev.get('features') or {}
        scout = strike_ev.get('scout_sol', 0)
        print(f'  STRIKE: scout={scout:.4f} am_factor={sf.get("anti_martingale_factor",1.0)} am_label={sf.get("anti_martingale_label","")}')

    print(f'  --- DIAGNOSIS ---')
    if duration_sec < 12.0:
        print(f'  > !!! Closed BEFORE min-hold expired (age {duration_sec:.1f}s < 12s)!')
        print(f'  > That means panic_floor (mult <= 0.50) triggered OR migration. Or min-hold logic has a bug.')
    if pnl < 0:
        if duration_sec >= 12.0 and post_peak_mult >= 1.5:
            print(f'  > Held to {duration_sec:.0f}s (past min-hold), still missed the {post_peak_mult:.2f}x post-pump.')
        if mult_at_12s and mult_at_12s < 1.05:
            print(f'  > At 12s (min-hold expiry) we were at {mult_at_12s:.3f}x — flat. Entry was bad.')


def main():
    pairs, raw_log = load_decisions()
    print(f'Phase 6 closed trades: {len(pairs)}')
    print()

    losses = sorted([(o, s, c) for o, s, c in pairs if float(c.get('pnl_sol') or 0) < 0],
                    key=lambda x: float(x[2].get('pnl_sol') or 0))
    wins = sorted([(o, s, c) for o, s, c in pairs if float(c.get('pnl_sol') or 0) > 0],
                  key=lambda x: -float(x[2].get('pnl_sol') or 0))

    print(f'=' * 100)
    print(f'LOSSES ({len(losses)}):')
    print(f'=' * 100)
    for i, (o, s, c) in enumerate(losses, 1):
        m = c['mint']
        print(f'\n--- LOSS #{i}: mint={m[:14]} ---')
        o_ts, c_ts = int(o.get('ts_ms') or 0), int(c.get('ts_ms') or 0)
        tape = load_mint_tape(m, o_ts - 5000, c_ts + 90000)
        diagnose(o, s, c, tape)

    if wins:
        print(f'\n{"=" * 100}')
        print(f'WINS ({len(wins)}):')
        print(f'{"=" * 100}')
        for i, (o, s, c) in enumerate(wins, 1):
            m = c['mint']
            print(f'\n--- WIN #{i}: mint={m[:14]} ---')
            o_ts, c_ts = int(o.get('ts_ms') or 0), int(c.get('ts_ms') or 0)
            tape = load_mint_tape(m, o_ts - 5000, c_ts + 90000)
            diagnose(o, s, c, tape)

    # Strike rejection / open failure analysis
    strikes = [x for x in raw_log if x.get('kind') == 'strike_plan']
    opens_set = set(x['mint'] for x in raw_log if x.get('kind') == 'open')
    failed_strikes = [s for s in strikes if s.get('mint') not in opens_set]
    print(f'\n{"=" * 100}')
    print(f'STRIKES THAT FAILED TO OPEN ({len(failed_strikes)} of {len(strikes)} total strikes):')
    print(f'{"=" * 100}')
    for s in failed_strikes[:15]:
        sf = s.get('features') or {}
        print(f'  {s.get("mint","")[:14]} reason={s.get("reason","")[:60]}')

    # Moonshots in raw tape
    print(f'\n{"=" * 100}')
    print(f'MOONSHOTS IN PHASE 6 RAW TAPE — DID BOT STRIKE?')
    print(f'{"=" * 100}')
    mint_first_price, mint_first_ts, mint_max_price, mint_max_ts = {}, {}, {}, {}
    for line in open(RAW):
        x = safe_json(line)
        if not x:
            continue
        m = x.get('mint')
        if not m:
            continue
        ts = int(x.get('ts_ms') or 0)
        p = x.get('price') or x.get('curve_price') or 0
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

    bot_mints = set()
    for x in raw_log:
        m = x.get('mint')
        if x.get('kind') in ('strike_plan', 'open', 'close', 'wave_arm') and m:
            bot_mints.add(m)
    bot_struck = set(s['mint'] for s in strikes)

    moonshots = []
    for m, fp in mint_first_price.items():
        mp = mint_max_price.get(m, fp)
        peak = mp / fp if fp > 0 else 1.0
        ttp = (mint_max_ts[m] - mint_first_ts[m]) / 1000.0
        if peak >= 2.0:
            moonshots.append((m, peak, ttp, m in bot_mints, m in bot_struck, m in opens_set))
    moonshots.sort(key=lambda x: -x[1])

    print(f'Mints peak >= 2x: {len(moonshots)}')
    print(f'  bot saw (any kind): {sum(1 for x in moonshots if x[3])}')
    print(f'  bot STRUCK: {sum(1 for x in moonshots if x[4])}')
    print(f'  bot OPENED: {sum(1 for x in moonshots if x[5])}')
    print()
    print(f'{"mint":>14s}  {"peak":>6s}  {"ttp_s":>7s}  {"saw":>5s}  {"strike":>7s}  {"opened":>7s}')
    for m, peak, ttp, saw, struck, opened in moonshots[:30]:
        print(f'  {m[:14]:>14s}  {peak:>6.2f}  {ttp:>7.1f}  {"YES" if saw else "---":>5s}  {"YES" if struck else "---":>7s}  {"YES" if opened else "---":>7s}')


if __name__ == '__main__':
    main()
