#!/bin/bash
# Steps 2 + 3 — Stop + Final Precheck (verbatim user sequence)
set +e
cd /root/piggy || exit 2

echo "=== STEP 2: STOP ==="
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
tmux kill-session -t v39b_live_smoke 2>/dev/null || true
tmux kill-session -t v39b_quote_recheck 2>/dev/null || true
pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython[0-9.]* PGG2.py' 2>/dev/null || true

sleep 2
echo "TMUX=$(tmux ls 2>/dev/null | wc -l)"
echo "BOT_PROCS=$(pgrep -af '[p]ython[0-9.]* -u PGG2.py|[p]ython[0-9.]* PGG2.py' | wc -l)"

echo ""
echo "=== STEP 3: PRECHECK ==="
./venv/bin/python -m py_compile PGG2.py pgg2_live_raptor.py pgg2_direct_pump.py birth_first_sniper.py compare_v39b_success_drylive_vs_live_mirror_env.py

./venv/bin/python compare_v39b_success_drylive_vs_live_mirror_env.py \
  --drylive start_pgg2_v39b_quote_rescue_drylive.sh \
  --live start_pgg2_v39b_quote_rescue_live_mirror.sh \
  --json-out V39B_SUCCESS_DRYLIVE_VS_LIVE_MIRROR_ENV.json

echo "---key_env_vars---"
grep -E '^export PGG2_EXECUTION_MODE=live|^export PGG2_V39_LIVE_MIRROR_ENABLED=1|^export PGG2_V39_TARGET_ENTRIES=10|^export PGG2_V39_MAX_OPEN=5|^export PIGGY_MAX_OPEN_POSITIONS=5|^export PGG2_V39_MAX_SELL_LATENCY_MS=' start_pgg2_v39b_quote_rescue_live_mirror.sh
echo "---disabled_modes---"
grep -E '^export PGG2_DRYLIVE_PILOT_ENABLED=0|^export PGG2_SCALP_ENABLED=0|^export PGG2_ENTRY_SNAPSHOT_BANK_ENABLED=0|^export PGG2_ENTRY_SNAPSHOT_BANK_LIVE_ELIGIBLE=0' start_pgg2_v39b_quote_rescue_live_mirror.sh
