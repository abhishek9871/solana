#!/usr/bin/env bash
# V42 — Phase 5 runbook. Starts a fresh quote-mode dry-live PGG2 run (no
# real tx) so it produces raw.jsonl, then launches the V42 no-send capture
# observer on the actual raw.jsonl path. Runs up to 10 minutes, then stops.

set -euo pipefail
cd /root/piggy

echo "=== STOP_OLD ==="
tmux kill-session -t v42_capture 2>/dev/null || true
tmux kill-session -t v42_bot 2>/dev/null || true
pkill -f "[p]ython3 -u PGG2.py" 2>/dev/null || true
pkill -f "[p]ython.*PGG2.py" 2>/dev/null || true
pkill -f "[p]ython3 _v42_no_send_capture.py" 2>/dev/null || true
sleep 2

# Source .env so SOLANATRACKER_RPC_HTTP and friends are exported.
set -a
. ./.env
set +a

# Launch the dry-live (PGG2_EXECUTION_MODE=quote) bot. start_pgg2_attack_paper.sh
# sets RUNID = ${PGG2_RUN_PREFIX}_$(date -u +%Y%m%d_%H%M%S) and writes
# /root/piggy/current_pgg2_runid.txt with the final RUNID — we poll that.
export PGG2_RUN_PREFIX="pgg2_v42_capture"

tmux new -ds v42_bot \
  "cd /root/piggy && set -a; . ./.env; set +a; export PGG2_RUN_PREFIX='pgg2_v42_capture'; timeout 720 ./start_pgg2_v39b_quote_rescue_drylive.sh"
sleep 4
tmux ls | grep v42_bot || { echo "BOT_TMUX_MISSING"; exit 2; }

# Poll current_pgg2_runid.txt for the actual RUNID.
i=0
RUNID=""
while [ $i -lt 30 ]; do
  if [ -f current_pgg2_runid.txt ]; then
    CANDID=$(cat current_pgg2_runid.txt)
    case "$CANDID" in
      pgg2_v42_capture_*) RUNID="$CANDID"; break ;;
    esac
  fi
  sleep 1; i=$((i+1))
done
if [ -z "$RUNID" ]; then
  echo "RUNID_NOT_PRODUCED"
  tmux kill-session -t v42_bot 2>/dev/null || true
  exit 2
fi

RAW_JSONL=/root/piggy/data/${RUNID}_raw.jsonl
BOT_LOG=/root/piggy/logs/${RUNID}.log
CAP_LOG=/root/piggy/logs/${RUNID}_v42cap.log
echo "RUNID=$RUNID"
echo "BOT_LOG=$BOT_LOG"
echo "RAW_JSONL=$RAW_JSONL"
echo "CAP_LOG=$CAP_LOG"

# Wait up to 30s for raw.jsonl to start filling.
i=0
while [ ! -s "$RAW_JSONL" ] && [ $i -lt 30 ]; do
  sleep 1; i=$((i+1))
done
if [ ! -s "$RAW_JSONL" ]; then
  echo "RAW_JSONL_NOT_FILLING file=$RAW_JSONL"
  tmux kill-session -t v42_bot 2>/dev/null || true
  exit 2
fi
echo "RAW_JSONL_READY at $RAW_JSONL (size=$(stat -c%s $RAW_JSONL))"

# Start V42 capture in foreground (blocks until 10-minute window or target).
/root/piggy/venv/bin/python -u /root/piggy/_v42_no_send_capture.py \
  --raw-path "$RAW_JSONL" \
  --out-md /root/piggy/V42_NO_SEND_PENDING_FLOW_REPORT.md \
  --max-seconds 600 \
  --target-pass 10 \
  --amount-sol 0.015 \
  --pnl-floor 0.00060 \
  --stress-pnl-floor 0.00020 \
  --stress-latency-ms 500 \
  --feed-source shred \
  --debug-log "$CAP_LOG" 2>&1 | tee -a "$CAP_LOG"

CAP_RC=${PIPESTATUS[0]}
echo "V42_CAPTURE_RC=$CAP_RC"

echo "=== STOP ==="
tmux kill-session -t v42_bot 2>/dev/null || true
pkill -f "[p]ython.*PGG2.py" 2>/dev/null || true
sleep 2

echo "=== SUMMARY ==="
ls -la /root/piggy/V42_NO_SEND_PENDING_FLOW_REPORT.md 2>&1 | head -5
ls -la "$RAW_JSONL" 2>&1
wc -l "$RAW_JSONL" 2>&1
grep -c "PGG2-V42-PENDING-FLOW" "$CAP_LOG" 2>&1 | head -3 || true
grep -c "V42-CAND-PASS" "$CAP_LOG" 2>&1 || true
grep -c "V42-CAND-REJECT" "$CAP_LOG" 2>&1 || true
