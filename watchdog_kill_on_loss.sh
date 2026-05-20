#!/usr/bin/env bash
# Watchdog: kill og_winner_live tmux session if realized PnL < -0.022 SOL (~-$2)
# Polls the bot's own PIGGY-STATUS log line every 3 seconds.

THRESHOLD_SOL="${WATCHDOG_THRESHOLD_SOL:--0.022}"
SESSION="${WATCHDOG_SESSION:-og_winner_live}"
LOG_GLOB="${WATCHDOG_LOG_GLOB:-/root/piggy/logs/og_winner_live_*.log}"
POLL_SEC="${WATCHDOG_POLL_SEC:-3}"

echo "WATCHDOG: monitoring session=$SESSION threshold=$THRESHOLD_SOL SOL log=$LOG_GLOB poll=${POLL_SEC}s"
echo "WATCHDOG: started at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

while true; do
    # Make sure session is still alive — exit if not
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "WATCHDOG: tmux session $SESSION is gone — exiting"
        break
    fi
    # Latest realized SOL from PIGGY-STATUS
    realized=$(grep "PIGGY-STATUS" $LOG_GLOB 2>/dev/null | tail -1 | grep -oP "realized=\K[+-][0-9.]+")
    if [ -z "$realized" ]; then
        sleep "$POLL_SEC"
        continue
    fi
    # Compare via python (bash can't do float comparison cleanly)
    is_below=$(python3 -c "import sys; sys.exit(0 if float('$realized') < float('$THRESHOLD_SOL') else 1)" && echo 1 || echo 0)
    if [ "$is_below" = "1" ]; then
        usd=$(python3 -c "print(f'{float(\"$realized\")*90:.2f}')")
        echo "WATCHDOG: realized=$realized SOL (~$$usd) < threshold=$THRESHOLD_SOL SOL — KILLING"
        tmux kill-session -t "$SESSION" 2>&1
        # Also pkill the python process directly in case tmux doesn't propagate
        pkill -f "python.*PGG2.py" 2>&1 || true
        echo "WATCHDOG: bot killed at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
        echo "WATCHDOG: final realized = $realized SOL (~$$usd)"
        break
    fi
    sleep "$POLL_SEC"
done

echo "WATCHDOG: exited at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
