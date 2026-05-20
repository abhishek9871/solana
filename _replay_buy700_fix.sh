#!/bin/bash
set +e
cd /root/piggy

run_replay() {
  local label=$1
  local raw=$2
  local min_buy700=$3
  local stamp=replay_buy700_${label}_$(date -u +%H%M%S)
  PIGGY_PAPER_TRADING=0 \
  PGG2_EXECUTION_MODE=quote \
  PGG2_DRY_LIVE_MODE=1 \
  PGG2_LIVE_BROKER=direct_pump \
  PGG2_QUOTE_SHADOW_POSITIONS=1 \
  PGG2_LIVE_CONFIRM=I_ACCEPT_REAL_SOL_RISK \
  PGG2_DIRECT_LIVE_CONFIRM=I_ACCEPT_DIRECT_PUMP_RISK \
  PGG2_EXEC_SPIKE_PROBE_MIN_BUY700_SOL=${min_buy700} \
  PGG2_STATE_FILE=/root/piggy/data/${stamp}_state.json \
  PGG2_DECISIONS_FILE=/root/piggy/data/${stamp}_decisions.jsonl \
  PGG2_RAW_EVENTS_FILE=/dev/null \
  ./venv/bin/python -u experimentalji.py --replay-raw "${raw}" 2>&1 | tail -3 > /tmp/${stamp}.log
  echo "DONE: ${label}"
}

# Source the dry-live launcher (without exec) to inherit all the lane configs
source <(sed 's|^exec.*|: noexec|' /root/piggy/start_experimentalji_direct_drylive.sh)

# Replay the dry-live tape WITHOUT the new filter (control)
echo "=== Control: dry-live tape, MIN_BUY700_SOL=0.0 (no filter) ==="
run_replay drylive_ctrl /root/piggy/data/experimentalji_direct_drylive_20260507_120204_raw.jsonl 0.0

# Replay the dry-live tape WITH the new filter
echo "=== Patched: dry-live tape, MIN_BUY700_SOL=1.0 ==="
run_replay drylive_fix /root/piggy/data/experimentalji_direct_drylive_20260507_120204_raw.jsonl 1.0

# Replay the live tape WITHOUT the new filter (control)
echo "=== Control: LIVE tape, MIN_BUY700_SOL=0.0 ==="
run_replay live_ctrl /root/piggy/data/experimentalji_direct_live_20260507_132452_raw.jsonl 0.0

# Replay the live tape WITH the new filter
echo "=== Patched: LIVE tape, MIN_BUY700_SOL=1.0 ==="
run_replay live_fix /root/piggy/data/experimentalji_direct_live_20260507_132452_raw.jsonl 1.0

echo
echo "=== Comparison ==="
python3 - <<'PYEND'
import json, glob, os
files = sorted(glob.glob('/root/piggy/data/replay_buy700_*_decisions.jsonl'))
def stats(path):
    closes=[]
    opens={}
    spike_count=0
    for line in open(path):
        if not line.strip(): continue
        try: x=json.loads(line)
        except: continue
        k=x.get('kind')
        if k=='open':
            opens[x.get('mint')]=x
            f=x.get('features') or {}
            if f.get('raw_profile')=='exec_spike_probe' or 'exec_spike' in (x.get('reason') or ''):
                spike_count+=1
        elif k=='close':
            closes.append(x)
    wins=[c for c in closes if float(c.get('pnl_sol') or 0)>0]
    losses=[c for c in closes if float(c.get('pnl_sol') or 0)<0]
    gw=sum(float(c.get('pnl_sol') or 0) for c in wins)
    gl=sum(float(c.get('pnl_sol') or 0) for c in losses)
    spike_w=spike_l=spike_pnl=0
    for c in closes:
        m=c.get('mint')
        op=opens.get(m, {})
        of=op.get('features') or {}
        if of.get('raw_profile')=='exec_spike_probe' or 'exec_spike' in (op.get('reason') or ''):
            pnl=float(c.get('pnl_sol') or 0)
            spike_pnl+=pnl
            if pnl>0: spike_w+=1
            elif pnl<0: spike_l+=1
    return len(closes), len(wins), len(losses), gw, gl, gw+gl, spike_w, spike_l, spike_pnl

print(f'{"replay":35s} {"trades":>7s} {"W/L":>9s} {"gross_W":>10s} {"gross_L":>10s} {"net":>10s} {"spike_W/L":>11s} {"spike_pnl":>11s}')
print('-'*120)
for f in files:
    name = os.path.basename(f).replace('_decisions.jsonl','').replace('replay_buy700_','')
    s = stats(f)
    print(f'{name:35s} {s[0]:>7d} {s[1]:>3d}/{s[2]:<3d}  {s[3]:>+10.4f} {s[4]:>+10.4f} {s[5]:>+10.4f}  {s[6]:>4d}/{s[7]:<4d}  {s[8]:>+11.6f}')
PYEND
