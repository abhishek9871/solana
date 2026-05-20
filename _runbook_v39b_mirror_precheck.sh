#!/bin/bash
# Step 3 — Fast Precheck (verbatim user sequence)
cd /root/piggy || exit 2

./venv/bin/python -m py_compile PGG2.py pgg2_live_raptor.py pgg2_direct_pump.py birth_first_sniper.py compare_v39b_success_drylive_vs_live_mirror_env.py

./venv/bin/python compare_v39b_success_drylive_vs_live_mirror_env.py \
  --drylive start_pgg2_v39b_quote_rescue_drylive.sh \
  --live start_pgg2_v39b_quote_rescue_live_mirror.sh \
  --json-out V39B_SUCCESS_DRYLIVE_VS_LIVE_MIRROR_ENV.json

echo "---key_env_vars---"
grep -E '^export PGG2_EXECUTION_MODE=live|^export PGG2_V39_TARGET_ENTRIES=10|^export PGG2_V39_MAX_OPEN=5|^export PIGGY_MAX_OPEN_POSITIONS=5|^export PGG2_V39_MAX_SELL_LATENCY_MS=' start_pgg2_v39b_quote_rescue_live_mirror.sh
echo "---disabled_modes---"
grep -E '^export PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=0|^export PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0|^export PGG2_SCALP_ENABLED=0|^export PGG2_DRYLIVE_PILOT_ENABLED=0' start_pgg2_v39b_quote_rescue_live_mirror.sh
echo "---wallet---"
test -s /root/piggy/live_wallet.key && echo "WALLET_KEY=OK"
