#!/bin/bash
set +e
cd /root/piggy

echo "=== PHASE 0: LIVE STATE CHECK ==="

tmux kill-session -t v39b_live_mirror 2>/dev/null || true
tmux kill-session -t v39b_live_smoke 2>/dev/null || true
tmux kill-session -t v39b_quote_recheck 2>/dev/null || true
tmux kill-session -t v39b_jito_stageA 2>/dev/null || true
pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
sleep 2

TMUX_COUNT=$(tmux ls 2>/dev/null | wc -l)
BOT_COUNT=$(pgrep -af 'python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py' 2>/dev/null | wc -l)
echo "TMUX_SESSIONS=$TMUX_COUNT"
echo "BOT_PROCS=$BOT_COUNT"

python3 - <<'PY'
import json, urllib.request, sys
pub='Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7'
def rpc(method, params):
    req=urllib.request.Request('https://api.mainnet-beta.solana.com',
        data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),
        headers={'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())
bal_lamports = rpc('getBalance', [pub])['result']['value']
bal = bal_lamports / 1e9
print(f"WALLET_PUB={pub}")
print(f"WALLET_SOL={bal:.9f}")
ta = rpc('getTokenAccountsByOwner', [pub, {'programId':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'}, {'encoding':'jsonParsed'}])
ta2 = rpc('getTokenAccountsByOwner', [pub, {'programId':'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'}, {'encoding':'jsonParsed'}])
total = ta['result']['value'] + ta2['result']['value']
nonzero = 0
for v in total:
    info = v['account']['data']['parsed']['info']
    mint = info['mint']
    amt = info['tokenAmount']
    raw = int(amt.get('amount', '0'))
    ui = float(amt.get('uiAmountString', '0') or '0')
    if raw > 0 or ui > 0:
        nonzero += 1
        print(f"OPEN_TOKEN mint={mint} amount={amt.get('uiAmountString')} raw={raw} decimals={amt.get('decimals')} program={v['account']['owner']} ata={v['pubkey']}")
print(f"TOTAL_TOKEN_ACCOUNTS={len(total)}")
print(f"NONZERO_TOKEN_ACCOUNTS={nonzero}")
if bal < 0.05:
    print("WARN: wallet below 0.05 SOL")
PY

if [ "$TMUX_COUNT" = "0" ] && [ "$BOT_COUNT" = "0" ]; then
    echo "PGG2-LIVE-STATE-CLEAN=true"
else
    echo "PGG2-LIVE-STATE-CLEAN=false"
fi
