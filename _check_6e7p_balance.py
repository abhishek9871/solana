import argparse, os, json, urllib.request
from solders.pubkey import Pubkey
from PGG2 import piggy_config
from pgg2_direct_pump import DirectPumpQuoteBroker
MINT='6E7PSDv84zV3m1wQusGPRHhZh5nn7WfsLzTfYknHpump'
os.environ['PGG2_EXECUTION_MODE']='live'
os.environ['PGG2_ENABLE_LIVE']='1'
os.environ['PIGGY_PAPER_TRADING']='0'
os.environ['PGG2_LIVE_CONFIRM']='I_ACCEPT_REAL_SOL_RISK'
os.environ['PGG2_DIRECT_LIVE_CONFIRM']='I_ACCEPT_DIRECT_PUMP_RISK'
os.environ['PGG2_WALLET_KEYPAIR']='/root/piggy/live_wallet.key'
args=argparse.Namespace(ws='', state='', raw_log='', decisions='', snipers='', run_seconds=0.0, print_events=False, replay_raw='')
b=DirectPumpQuoteBroker(piggy_config(args))
print('wallet', b.public_key)
try:
 print('token_raw', b.token_balance_raw(Pubkey.from_string(MINT)))
 print('token_ui', b.raw_to_ui(Pubkey.from_string(MINT), b.token_balance_raw(Pubkey.from_string(MINT))))
except Exception as e:
 print('token_balance_error', type(e).__name__, e)
req={'jsonrpc':'2.0','id':1,'method':'getBalance','params':[b.public_key, {'commitment':'confirmed'}]}
out=json.loads(urllib.request.urlopen(urllib.request.Request(b.rpc_url, data=json.dumps(req).encode(), headers={'Content-Type':'application/json','User-Agent':'Mozilla/5.0'}), timeout=20).read())
print('sol_balance', out.get('result',{}).get('value')/1e9)
