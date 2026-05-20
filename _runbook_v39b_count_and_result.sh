#!/bin/bash
# Steps 5 + 6 — Counts + Result Markdown (verbatim from runbook)
cd /root/piggy || exit 2
RUNID=$(cat current_pgg2_live_smoke_runid.txt)
LOG="logs/${RUNID}.log"
OUT="LIVE_SMOKE_V39B_RESULT.md"

echo "RUNID=$RUNID"
echo "BUY_SEND=$(grep -c 'PGG2-V39-LIVE-BUY-SEND' "$LOG" || true)"
echo "BUY_CONFIRMED=$(grep -c 'PGG2-V39-LIVE-BUY-CONFIRMED' "$LOG" || true)"
echo "SELL_SEND=$(grep -c 'PGG2-V39-LIVE-SELL-SEND' "$LOG" || true)"
echo "SELL_CONFIRMED=$(grep -c 'PGG2-V39-LIVE-SELL-CONFIRMED' "$LOG" || true)"
echo "SMOKE_END=$(grep -c 'PGG2-V39-LIVE-SMOKE-END' "$LOG" || true)"
echo "TOKEN_MISMATCH=$(grep -c 'PGG2-POSITION-TOKEN-MISMATCH-FATAL' "$LOG" || true)"
echo "CLOSE_FAIL=$(grep -c 'PGG2-RISK-CLOSE-FAIL' "$LOG" || true)"
echo "STALE=$(grep -ci 'stale' "$LOG" || true)"
echo "---tail80---"
tail -n 80 "$LOG"

{
  echo "# LIVE SMOKE V39B RESULT"
  echo
  echo "- run_id: ${RUNID}"
  echo "- log_path: /root/piggy/${LOG}"
  echo "- generated_at_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "## Counts"
  echo "- buy_send: $(grep -c 'PGG2-V39-LIVE-BUY-SEND' "$LOG" || true)"
  echo "- buy_confirmed: $(grep -c 'PGG2-V39-LIVE-BUY-CONFIRMED' "$LOG" || true)"
  echo "- sell_send: $(grep -c 'PGG2-V39-LIVE-SELL-SEND' "$LOG" || true)"
  echo "- sell_confirmed: $(grep -c 'PGG2-V39-LIVE-SELL-CONFIRMED' "$LOG" || true)"
  echo "- smoke_end: $(grep -c 'PGG2-V39-LIVE-SMOKE-END' "$LOG" || true)"
  echo "- token_mismatch: $(grep -c 'PGG2-POSITION-TOKEN-MISMATCH-FATAL' "$LOG" || true)"
  echo "- close_fail: $(grep -c 'PGG2-RISK-CLOSE-FAIL' "$LOG" || true)"
  echo "- stale_mentions: $(grep -ci 'stale' "$LOG" || true)"
  echo
  echo "## Key Live Lines"
  grep -E 'PGG2-LIVE-SMOKE-START|PGG2-V39-ENTRY-ROUTER|PGG2-V39-LIVE-BUY|PGG2-V39-LIVE-TOKEN-RECONCILE|PGG2-V39-LIVE-SELL|PGG2-V39-LIVE-WALLET-DELTA|PGG2-V39-LIVE-PNL-RECONCILE|PGG2-V39-LIVE-SMOKE-END' "$LOG" || true
} > "$OUT"

echo "RESULT=/root/piggy/$OUT"
