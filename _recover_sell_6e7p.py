import argparse, os, json, time, sys
from PGG2 import piggy_config
from pgg2_direct_pump import DirectPumpQuoteBroker
from birth_first_sniper import SOL_MINT, short_addr

MINT = '6E7PSDv84zV3m1wQusGPRHhZh5nn7WfsLzTfYknHpump'
# Explicit live recovery env. This script simulates each candidate before send.
os.environ['PGG2_EXECUTION_MODE'] = 'live'
os.environ['PGG2_ENABLE_LIVE'] = '1'
os.environ['PIGGY_PAPER_TRADING'] = '0'
os.environ['PGG2_DRY_LIVE_MODE'] = '0'
os.environ['PGG2_LIVE_BROKER'] = 'direct_pump'
os.environ['PGG2_LIVE_CONFIRM'] = 'I_ACCEPT_REAL_SOL_RISK'
os.environ['PGG2_DIRECT_LIVE_CONFIRM'] = 'I_ACCEPT_DIRECT_PUMP_RISK'
os.environ['PGG2_WALLET_KEYPAIR'] = '/root/piggy/live_wallet.key'
os.environ['PGG2_LIVE_SIMULATE_BEFORE_SEND'] = '1'
os.environ['PGG2_LIVE_SKIP_PREFLIGHT'] = '0'
os.environ['PGG2_LIVE_SELL_SLIPPAGE_PCT'] = '80'
os.environ['PGG2_QUOTE_SIMULATE'] = '1'
os.environ['PGG2_DIRECT_SELECT_BUYBACK_BY_SIM'] = '0'
os.environ['PGG2_DIRECT_REQUIRE_SIM_SELECTED_BUYBACK'] = '0'
os.environ['PGG2_DIRECT_OBSERVED_PAIR_FROM_RAW'] = '1'
os.environ['PGG2_DIRECT_PUMP_REMAINING_CACHE'] = '/root/piggy/data/pgg2_pump_remaining_cache.json'

args = argparse.Namespace(ws='', state='', raw_log='', decisions='', snipers='', run_seconds=0.0, print_events=False, replay_raw='')
config = piggy_config(args)
b = DirectPumpQuoteBroker(config)
print('wallet', b.public_key)
raw_bal = b.token_balance_raw(b.as_pubkey(MINT) if hasattr(b, 'as_pubkey') else __import__('solders.pubkey').pubkey.Pubkey.from_string(MINT))
print('raw_balance', raw_bal)
if raw_bal <= 0:
    print('no_token_balance')
    raise SystemExit(0)
# Try full auto first with close account. If that fails, try fractions without closing ATA.
tries = [('auto', True), (0.995, False), (0.99, False), (0.95, False), (0.90, False), (0.75, False), (0.50, False)]
for amount, close_ata in tries:
    os.environ['PGG2_DIRECT_CLOSE_TOKEN_ATA_ON_SELL'] = '1' if close_ata else '0'
    if amount == 'auto':
        sell_amount = 'auto'
    else:
        sell_amount = b.raw_to_ui(__import__('solders.pubkey').pubkey.Pubkey.from_string(MINT), int(raw_bal * amount))
    print('TRY', amount, 'close_ata', close_ata, 'sell_amount', sell_amount)
    try:
        quote = b.build_swap(MINT, SOL_MINT, sell_amount, 80.0)
        signed_b64, _ = b.sign_transaction(str(quote['txn']))
        ok = b.simulate_signed(signed_b64)
        print('simulate', ok, 'amountOut', quote.get('rate',{}).get('amountOut'))
        if not ok:
            continue
        sig = b.send_signed(signed_b64)
        print('sent', sig)
        confirmed = b.wait_confirmed(sig)
        print('confirmed', confirmed)
        if confirmed:
            delta = b.transaction_wallet_delta_sol(sig)
            print('wallet_delta_sol', delta)
        raise SystemExit(0)
    except Exception as exc:
        print('try_error', amount, type(exc).__name__, exc)
print('NO_SUCCESS')
raise SystemExit(2)

