"""Dump full feature snapshots for all trades from run 150006."""
import json

PATH = '/root/piggy/data/experimentalji_direct_live_20260507_150006_decisions.jsonl'
TARGETS = ['AAtu', '2rnw', '4y3S', 'FxZv', 'HBnX']

records = {t: {'wave_arm': None, 'strike_plan': None, 'open': None, 'close': None} for t in TARGETS}

for line in open(PATH):
    if not line.strip():
        continue
    try:
        x = json.loads(line)
    except Exception:
        continue
    mint = x.get('mint') or ''
    for t in TARGETS:
        if mint.startswith(t):
            k = x.get('kind')
            if k in records[t]:
                records[t][k] = x

for tag, recs in records.items():
    print('=' * 130)
    print(f'TRADE: {tag}')
    print('=' * 130)
    for stage in ('wave_arm', 'strike_plan', 'open', 'close'):
        rec = recs.get(stage)
        if not rec:
            continue
        f = rec.get('features') or {}
        lane = rec.get('lane') or ''
        rsn = rec.get('reason') or ''
        pnl = rec.get('pnl_sol')
        score = rec.get('score')
        print(f'\n  --- {stage} (lane={lane}) ts={rec.get("ts_ms")} score={score} pnl={pnl} ---')
        print(f'  reason: {rsn}')
        # All numeric features
        keys_to_show = [
            'age_ms', 'price', 'has_curve', 'flow_live', 'is_mayhem',
            'buy250', 'buy700', 'buy1500',
            'uniq250', 'uniq700', 'uniq1500',
            'top_share700', 'top_share1500',
            'buyer_hhi700',
            'sell700', 'sell1500',
            'cluster_score',
            'move250', 'move700', 'move1500',
            'first_buy_sol', 'last_buy_age_ms', 'last_sell_age_ms',
            'buy_age_ms', 'buy_stall',
            'slot_buy_sol', 'slot_buyers', 'slot_top_share',
            'vsol_sol',
            'wave_armed', 'wave_arm_age_ms', 'wave_base_move',
            'raw_profile', 'raw_exec_variant',
            'raw_buy_sol_10s', 'raw_unique_buyers_10s', 'raw_sell_sol_10s', 'raw_sell_ratio_10s',
            'raw_top_share_10s', 'raw_entry_move', 'raw_arm_age_ms', 'raw_confirm_mult',
            'entry_move_from_first', 'curve_lag_live_buy700', 'curve_lag_live_unique700', 'curve_lag_live_top700',
            'curve_lag_follow', 'arm_first_price', 'arm_first_price_ts_ms',
            'entry_size_reason', 'entry_probe_sol', 'create_version',
        ]
        for k in keys_to_show:
            if k in f:
                v = f[k]
                if isinstance(v, float):
                    print(f'    {k}: {v:.6f}')
                else:
                    print(f'    {k}: {v}')
    print()
