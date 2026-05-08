"""Per-trade autopsy on Phase 5 dry-live.

For each closed trade: print full feature signature, the price tape during the
position AND for 90s after exit, and a structural diagnosis of what failed.
"""
import json
import os
import sys
from collections import defaultdict


DEC = '/root/piggy/data/pgg2_phase5_drylive_20260508_063401_decisions.jsonl'
RAW = '/root/piggy/data/pgg2_phase5_drylive_20260508_063401_raw.jsonl'
INTERNAL_LOG = '/root/piggy/logs/pgg2_phase5_drylive_20260508_063401.log'


def safe_json(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_decisions():
    opens = {}
    last_strike = {}
    pairs = []
    raw_log = []  # ALL events keyed by mint
    for line in open(DEC):
        x = safe_json(line)
        if not x:
            continue
        m = x.get('mint')
        k = x.get('kind')
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
                pairs.append((o[0], o[1], x))  # (open, strike_plan, close)
    return pairs, raw_log


def load_mint_tape(mint, t_start, t_end):
    """Return sorted [(ts_ms, price, is_buy, sol)] for mint in [t_start, t_end]."""
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
    """Return a list of diagnostic lines about what happened."""
    lines = []
    o_ts = int(open_ev.get('ts_ms') or 0)
    c_ts = int(close_ev.get('ts_ms') or 0)
    of = open_ev.get('features') or {}
    cf = close_ev.get('features') or {}
    open_price = float(of.get('price') or 0)
    pnl = float(close_ev.get('pnl_sol') or 0)
    reason = close_ev.get('reason') or ''
    duration_sec = (c_ts - o_ts) / 1000.0

    # In-position window
    in_tape = [r for r in tape if o_ts <= r[0] <= c_ts]
    # Post-close window (90s)
    post_tape = [r for r in tape if c_ts < r[0] <= c_ts + 90000]

    in_peak = max((r[1] for r in in_tape if r[1] > 0), default=open_price)
    post_peak = max((r[1] for r in post_tape if r[1] > 0), default=0.0)
    in_peak_mult = in_peak / open_price if open_price > 0 else 1.0
    post_peak_mult = post_peak / open_price if (open_price > 0 and post_peak > 0) else 0.0

    # When did peak occur (in-position)?
    in_peak_ts = next((r[0] for r in in_tape if r[1] >= in_peak * 0.999), o_ts)
    in_peak_age_sec = (in_peak_ts - o_ts) / 1000.0

    # Buy activity in last 1500ms before close
    pre_close_window_buys = [r for r in tape if c_ts - 1500 <= r[0] <= c_ts and r[2]]
    buy_count_pre_close = len(pre_close_window_buys)
    buy_sol_pre_close = sum(r[3] for r in pre_close_window_buys)

    lines.append(f'  REASON: {reason}')
    lines.append(f'  PnL: {pnl:+.5f} SOL  (~${pnl*90:+.2f})')
    lines.append(f'  Duration: {duration_sec:.2f}s')
    lines.append(f'  Open price: {open_price:.6e}')
    lines.append(f'  IN-position peak: {in_peak_mult:.3f}x  at age={in_peak_age_sec:.1f}s')
    lines.append(f'  POST-close peak (next 90s): {post_peak_mult:.3f}x')
    lines.append(f'  Pre-close buyer activity (last 1500ms): {buy_count_pre_close} buys totaling {buy_sol_pre_close:.2f} SOL')

    # Open feature snapshot
    lines.append(f'  OPEN feat: lane={open_ev.get("lane")} entry_move={of.get("priced_snap_entry_move",0):.3f} top1500={of.get("top_share1500",0):.3f} buy1500={of.get("buy1500",0):.2f} uniq1500={of.get("uniq1500",0)} age_ms={of.get("age_ms",0)} vsol={of.get("vsol_sol",0):.1f} first_buy_sol={of.get("first_buy_sol",0):.2f}')
    if strike_ev:
        sf = strike_ev.get('features') or {}
        scout = strike_ev.get('scout_sol', 0)
        target = strike_ev.get('target_sol', 0)
        am_factor = sf.get('anti_martingale_factor', 1.0)
        am_label = sf.get('anti_martingale_label', '')
        lines.append(f'  STRIKE: scout={scout:.4f} target={target:.4f} am_factor={am_factor} am_label={am_label}')

    # Diagnosis
    lines.append(f'  --- DIAGNOSIS ---')
    if pnl < 0:
        if 'hard_break' in reason:
            lines.append(f'  > Hard_break fired at age {duration_sec:.1f}s.')
            lines.append(f'  > In-position peak was {in_peak_mult:.3f}x — never crossed 1.18 latch trigger.')
            if post_peak_mult >= 1.5:
                lines.append(f'  > !!! POST-CLOSE peak hit {post_peak_mult:.2f}x. We exited before the pump.')
            if buy_count_pre_close > 0:
                lines.append(f'  > !!! Buyers were ACTIVE in last 1500ms ({buy_count_pre_close} buys, {buy_sol_pre_close:.2f} SOL) — grace SHOULD have deferred this kill.')
            else:
                lines.append(f'  > No buyer flow in last 1500ms; grace correctly NOT firing.')
        elif 'quote_loss_clamp' in reason:
            lines.append(f'  > quote_loss_clamp on cost-model PnL threshold.')
            if post_peak_mult >= 1.5:
                lines.append(f'  > !!! Mint then ran to {post_peak_mult:.2f}x post-close.')
        elif 'no_follow' in reason or 'no_pop' in reason:
            lines.append(f'  > No-follow timeout — entry happened but no continuation buyers showed up.')
            if post_peak_mult >= 1.5:
                lines.append(f'  > !!! Post-close peak {post_peak_mult:.2f}x — pump came too late for our window.')
    else:
        if 'moonshot_trail' in reason:
            lines.append(f'  > Moonshot rider fired — peak {in_peak_mult:.3f}x captured.')
            if post_peak_mult >= in_peak_mult * 1.3:
                lines.append(f'  > Mint ran further to {post_peak_mult:.3f}x post-close — trail was tighter than runner.')
        elif 'quote_profit_bank' in reason:
            lines.append(f'  > Banked at cost-model threshold; close_move250={cf.get("move250",1):.3f}.')
            if post_peak_mult >= 1.5:
                lines.append(f'  > Mint ran to {post_peak_mult:.2f}x post-close — banked too early.')
    return lines


def main():
    pairs, raw_log = load_decisions()
    print(f'Phase 5 closed trades: {len(pairs)}')
    print()

    # Group by win/loss
    losses = sorted([(o, s, c) for o, s, c in pairs if float(c.get('pnl_sol') or 0) < 0],
                    key=lambda x: float(x[2].get('pnl_sol') or 0))
    wins = sorted([(o, s, c) for o, s, c in pairs if float(c.get('pnl_sol') or 0) > 0],
                  key=lambda x: -float(x[2].get('pnl_sol') or 0))

    print('=' * 100)
    print(f'LOSSES ({len(losses)}) — sorted by worst PnL')
    print('=' * 100)
    for i, (o, s, c) in enumerate(losses, 1):
        m = c['mint']
        print(f'\n--- LOSS #{i}: mint={m[:14]} ---')
        o_ts = int(o.get('ts_ms') or 0)
        c_ts = int(c.get('ts_ms') or 0)
        # Pull tape from o_ts - 5s to c_ts + 90s
        tape = load_mint_tape(m, o_ts - 5000, c_ts + 90000)
        for line in diagnose(o, s, c, tape):
            print(line)

    print()
    print('=' * 100)
    print(f'WINS ({len(wins)}) — sorted by best PnL')
    print('=' * 100)
    for i, (o, s, c) in enumerate(wins, 1):
        m = c['mint']
        print(f'\n--- WIN #{i}: mint={m[:14]} ---')
        o_ts = int(o.get('ts_ms') or 0)
        c_ts = int(c.get('ts_ms') or 0)
        tape = load_mint_tape(m, o_ts - 5000, c_ts + 90000)
        for line in diagnose(o, s, c, tape):
            print(line)

    # Strikes that did NOT open (rejected by broker)
    print()
    print('=' * 100)
    strikes_rejected = []
    for x in raw_log:
        if x.get('kind') == 'strike_skipped':
            strikes_rejected.append(x)
    print(f'STRIKES REJECTED BY BROKER ({len(strikes_rejected)}):')
    for x in strikes_rejected[:10]:
        print(f'  mint={x.get("mint","")[:12]} reason={x.get("reason","")[:80]}')

    # Mints that pumped 5x+ in raw tape — did we strike them?
    print()
    print('=' * 100)
    print('MOONSHOTS IN PHASE 5 RAW TAPE — DID BOT STRIKE?')
    print('=' * 100)
    mint_first_price = {}
    mint_first_ts = {}
    mint_max_price = {}
    mint_max_ts = {}
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
        if x.get('kind') in ('strike_plan', 'open', 'close', 'strike_skipped') and m:
            bot_mints.add(m)
    moonshots = []
    for m, fp in mint_first_price.items():
        mp = mint_max_price.get(m, fp)
        peak = mp / fp if fp > 0 else 1.0
        ttp = (mint_max_ts[m] - mint_first_ts[m]) / 1000.0
        if peak >= 3.0:
            moonshots.append((m, peak, ttp, m in bot_mints))
    moonshots.sort(key=lambda x: -x[1])
    print(f'Mints peak >= 3x: {len(moonshots)}, of which bot saw/considered: {sum(1 for m in moonshots if m[3])}')
    print(f'{"mint":>14s}  {"peak":>6s}  {"ttp_s":>6s}  {"bot_saw":>8s}')
    for m, peak, ttp, saw in moonshots:
        print(f'  {m[:14]:>14s}  {peak:>6.2f}  {ttp:>6.1f}  {"YES" if saw else "---":>8s}')

if __name__ == '__main__':
    main()
