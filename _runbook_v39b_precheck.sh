#!/bin/bash
# Step 2 — Final Precheck (verbatim from LIVE_SMOKE_V39B_TEAM_RUNBOOK.md)
cd /root/piggy || exit 2
set -e

tmux kill-session -t v39b_live_smoke 2>/dev/null || true
pkill -f 'python -u PGG2.py' 2>/dev/null || true
pkill -f 'python3 -u PGG2.py' 2>/dev/null || true
sleep 2

echo "TMUX_SESSIONS=$(tmux ls 2>/dev/null | wc -l)"
echo "BOT_PROCS=$(pgrep -af 'python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py' | wc -l)"

./venv/bin/python -m py_compile PGG2.py pgg2_live_raptor.py pgg2_direct_pump.py birth_first_sniper.py compare_v39b_drylive_vs_live_env.py
./venv/bin/python compare_v39b_drylive_vs_live_env.py \
  --drylive start_pgg2_v39b_quote_rescue_drylive.sh \
  --live start_pgg2_v39b_quote_rescue_live_smoke.sh \
  --json-out LIVE_V39B_ENV_MIRROR.json

grep -q '^export PGG2_EXECUTION_MODE=live' start_pgg2_v39b_quote_rescue_live_smoke.sh
grep -q '^export PGG2_ENABLE_LIVE=1' start_pgg2_v39b_quote_rescue_live_smoke.sh
grep -q 'I_ACCEPT_REAL_SOL_RISK' start_pgg2_v39b_quote_rescue_live_smoke.sh
grep -q '^export PGG2_V39_LIVE_SMOKE_ENABLED=1' start_pgg2_v39b_quote_rescue_live_smoke.sh
grep -q '^export PGG2_V39_TARGET_ENTRIES=1' start_pgg2_v39b_quote_rescue_live_smoke.sh
grep -q '^export PIGGY_MAX_OPEN_POSITIONS=1' start_pgg2_v39b_quote_rescue_live_smoke.sh
grep -q '^export PGG2_V39_QUOTE_RESCUE_ENABLED=1' start_pgg2_v39b_quote_rescue_live_smoke.sh

test -s /root/piggy/live_wallet.key

./venv/bin/python - <<'PY'
import json, urllib.request, sys
pub='Cw4G8XLcw89VJp734U6noPpfQbTosvQQuaDKu9jdL7M7'
req=urllib.request.Request(
    'https://api.mainnet-beta.solana.com',
    data=json.dumps({'jsonrpc':'2.0','id':1,'method':'getBalance','params':[pub]}).encode(),
    headers={'Content-Type':'application/json'},
)
data=json.loads(urllib.request.urlopen(req, timeout=10).read())
bal=data['result']['value']/1_000_000_000
print(f'WALLET={pub}')
print(f'BALANCE_SOL={bal:.9f}')
if bal < 0.03:
    sys.exit('INSUFFICIENT_BALANCE')
PY

if grep -Ei 'JITO|PROTECTED_HOLD|ENTRY_SNAPSHOT_BANK_ENABLED=1|ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=1' start_pgg2_v39b_quote_rescue_live_smoke.sh; then
  echo "CHECK ABOVE: allowed only if disabled/0. Do not run if anything enables Jito/protected-hold/old ESB."
fi

echo "PRECHECK_PASS=1"
