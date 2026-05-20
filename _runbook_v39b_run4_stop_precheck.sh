#!/bin/bash
# RUN4 Steps 2 + 3 — verbatim user sequence
set +e
cd /root/piggy

echo "=== STEP 2: STOP ==="
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true

echo "---pgrep---"
pgrep -af 'python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py' || true
echo "---tmux---"
tmux ls 2>/dev/null || true

echo ""
echo "=== STEP 3: PRECHECK ==="
python3 -m py_compile PGG2.py pgg2_live_raptor.py pgg2_direct_pump.py birth_first_sniper.py
echo "PY_COMPILE_RC=$?"

echo "---env compare---"
python3 compare_v39b_success_drylive_vs_live_mirror_env.py | head -n 5

echo "---grep PRESEND-REQUOTE / LIVE_BUY_MAX_DRIFT / LIVE_LANDED_PNL_FLOOR---"
grep -n 'PRESEND-REQUOTE\|LIVE_BUY_MAX_DRIFT\|LIVE_LANDED_PNL_FLOOR' PGG2.py pgg2_live_raptor.py start_pgg2_v39b_quote_rescue_live_mirror.sh
