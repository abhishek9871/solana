#!/bin/bash
# RUN5 Steps 2 + 3 — verbatim user sequence
set +e
cd /root/piggy

echo "=== STEP 2: STOP ==="
tmux kill-session -t v39b_live_mirror 2>/dev/null || true
tmux kill-session -t v39b_live_smoke 2>/dev/null || true
tmux kill-session -t v39b_quote_recheck 2>/dev/null || true
pkill -f '[p]ython -u PGG2.py' 2>/dev/null || true
pkill -f '[p]ython3 -u PGG2.py' 2>/dev/null || true

pgrep -af 'python[0-9.]* -u PGG2.py|python[0-9.]* PGG2.py' || echo NO_BOT_PROCS
tmux ls 2>/dev/null || echo NO_TMUX

echo ""
echo "=== STEP 3: PRECHECK ==="
python3 - <<'PY'
import ast
for f in ["PGG2.py","pgg2_live_raptor.py","pgg2_direct_pump.py","birth_first_sniper.py","compare_v39b_success_drylive_vs_live_mirror_env.py"]:
    ast.parse(open(f, "r", encoding="utf-8").read())
    print(f"{f}: OK")
PY

python3 compare_v39b_success_drylive_vs_live_mirror_env.py \
  --drylive start_pgg2_v39b_quote_rescue_drylive.sh \
  --live start_pgg2_v39b_quote_rescue_live_mirror.sh
