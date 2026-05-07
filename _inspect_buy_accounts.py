import argparse, os, json
from PGG2 import piggy_config
from pgg2_direct_pump import DirectPumpQuoteBroker, PUMP_PROGRAM_ID, PUMP_FEE_PROGRAM_ID
os.environ['PGG2_EXECUTION_MODE']='live'; os.environ['PGG2_ENABLE_LIVE']='1'; os.environ['PIGGY_PAPER_TRADING']='0'; os.environ['PGG2_LIVE_CONFIRM']='I_ACCEPT_REAL_SOL_RISK'; os.environ['PGG2_DIRECT_LIVE_CONFIRM']='I_ACCEPT_DIRECT_PUMP_RISK'; os.environ['PGG2_WALLET_KEYPAIR']='/root/piggy/live_wallet.key'
args=argparse.Namespace(ws='', state='', raw_log='', decisions='', snipers='', run_seconds=0.0, print_events=False, replay_raw='')
b=DirectPumpQuoteBroker(piggy_config(args))
for sig in ['2X6JdjLfVc7HyHDw3a7jzwPJVC7bk5s8wAiac2h3vDGbznZtp4z9RbZYP5uUJgX1koLNZt3pUVYAskqM6RpNDXtj','RADHXnhVMqTWveT29KNzjwjFaUxDV9HENY72C3RX45niN8ks4sPeqCf333rCw6qnGNSQF13nUCUWSEFantajyuf'] :
    print('\nSIG', sig)
    tx=b.rpc('getTransaction',[sig,{'encoding':'json','commitment':'confirmed','maxSupportedTransactionVersion':0}])
    if not tx:
        print('no tx'); continue
    print('err', (tx.get('meta') or {}).get('err'))
    msg=(tx.get('transaction') or {}).get('message') or {}
    keys=list(msg.get('accountKeys') or [])
    loaded=(tx.get('meta') or {}).get('loadedAddresses') or {}
    keys.extend(loaded.get('writable') or []); keys.extend(loaded.get('readonly') or [])
    for ix in msg.get('instructions') or []:
        pid=keys[ix.get('programIdIndex')] if isinstance(ix.get('programIdIndex'),int) and ix.get('programIdIndex')<len(keys) else None
        if pid==str(PUMP_PROGRAM_ID):
            accounts=ix.get('accounts') or []
            print('pump_accounts_len', len(accounts))
            for pos, idx in enumerate(accounts):
                print(pos, keys[idx] if isinstance(idx,int) and idx<len(keys) else idx)

