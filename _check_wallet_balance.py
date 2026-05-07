import json, os, urllib.request, glob, time
from pathlib import Path
from solders.keypair import Keypair
raw = Path('/root/piggy/live_wallet.key').read_text().strip()
try:
    kp = Keypair.from_base58_string(raw)
except Exception:
    import base58
    kp = Keypair.from_bytes(base58.b58decode(raw))
pub = str(kp.pubkey())
print('wallet', pub)
rpc = None
for p in ['/root/piggy/.env', '/root/.env']:
    if os.path.exists(p):
        for line in open(p, encoding='utf-8', errors='ignore'):
            line=line.strip()
            if line.startswith('SOLANA_TRACKER_RPC_URL=') or line.startswith('PGG2_LIVE_RPC_URL='):
                rpc=line.split('=',1)[1].strip().strip('"\'')
if not rpc:
    rpc='https://api.mainnet-beta.solana.com'
req={'jsonrpc':'2.0','id':1,'method':'getBalance','params':[pub, {'commitment':'confirmed'}]}
headers={'Content-Type':'application/json','User-Agent':'Mozilla/5.0 pgg2-balance-check'}
try:
    out=json.loads(urllib.request.urlopen(urllib.request.Request(rpc, data=json.dumps(req).encode(), headers=headers), timeout=20).read())
    lam=out.get('result',{}).get('value')
    print('balance_sol', lam/1e9 if lam is not None else None)
except Exception as e:
    print('rpc_error', type(e).__name__, str(e))
print('recent_states')
for f in sorted(glob.glob('/root/piggy/data/pgg2_direct_live_*_state.json'), key=os.path.getmtime)[-8:]:
    try:
        s=json.load(open(f))['session']
        print(os.path.basename(f), 'realized', s.get('realized_pnl_sol'), 'W/L', str(s.get('wins'))+'/'+str(s.get('losses')), 'closes', s.get('closes'), 'started', time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(s.get('started_at',0))))
    except Exception as e:
        print('bad_state', os.path.basename(f), type(e).__name__, str(e))
