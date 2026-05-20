#!/usr/bin/env bash
# Fisherman supervisor — keeps the bot fishing 24/7 by auto-restarting on:
#   1. Watchdog exit (PGG2-V48-FISHERMAN-WATCHDOG-EXIT in log)
#   2. Process death (any reason)
#   3. Scheduled rollover (every PGG2_SUPERVISOR_ROLLOVER_S seconds, default 1500s = 25min)
#      - Even with RPC pool, restarts clear any accumulated state and refresh
#        endpoint cooldowns. Cheap insurance against ceiling drift.
#
# Layer 4 of the FISHER fix (2026-05-18).
#
# Usage: nohup bash /root/piggy/fisherman_supervisor.sh &
# Stop:  pkill -f fisherman_supervisor.sh; pkill -f start_v55_stagea.sh
set -uo pipefail

PGG2_DIR="${PGG2_DIR:-/root/piggy}"
LAUNCHER="${LAUNCHER:-${PGG2_DIR}/start_v55_stagea.sh}"
TMUX_SESSION="${TMUX_SESSION:-netv2_stagea}"
ROLLOVER_S="${PGG2_SUPERVISOR_ROLLOVER_S:-1500}"
SUPERVISOR_LOG="${PGG2_DIR}/supervisor_$(date +%Y%m%d).log"
MIN_RESTART_INTERVAL_S=30  # don't thrash if launcher exits immediately

log() {
    # Write to log file only — never stdout. start_bot() returns the logfile
    # path via stdout, so we must not pollute it with log lines.
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] supervisor: $*" >>"${SUPERVISOR_LOG}"
}

start_bot() {
    local logfile="${PGG2_DIR}/v55_stagea_netv2_$(date +%H%M%S).log"
    log "starting bot — log=${logfile} session=${TMUX_SESSION}"
    tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null
    sleep 1
    tmux new-session -d -s "${TMUX_SESSION}" \
        "cd ${PGG2_DIR} && bash ${LAUNCHER} 2>&1 | tee ${logfile}"
    sleep 2
    if tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        log "started OK — tmux session active"
        echo "${logfile}"
    else
        log "FAIL: tmux session not active after start"
        echo ""
    fi
}

is_bot_alive() {
    # Bot is alive if tmux session exists. During the first 60s after start
    # we tolerate "no python yet" (launcher is still initialising — preflight,
    # env loading, etc. can take 30+ seconds). After 60s we require the python
    # process to be present.
    if ! tmux has-session -t "${TMUX_SESSION}" 2>/dev/null; then
        return 1
    fi
    local age=$(( $(date +%s) - last_restart ))
    if [ "${age}" -lt 120 ]; then
        return 0  # grace period — tmux alive is enough (launcher init can take 60s+)
    fi
    if ! pgrep -f 'pgg2_v48_drylive_harness' >/dev/null; then
        return 1
    fi
    return 0
}

watchdog_fired() {
    local logfile="$1"
    [ -z "${logfile}" ] && return 1
    [ ! -f "${logfile}" ] && return 1
    if grep -q 'PGG2-V48-FISHERMAN-WATCHDOG-EXIT' "${logfile}" 2>/dev/null; then
        return 0
    fi
    if grep -q 'PGG2-V48-V50B-COMPLETE\|PGG2-V48-V50B-WALLET-DRAWDOWN\|PGG2-V48-V50B-STOP' "${logfile}" 2>/dev/null; then
        return 0
    fi
    return 1
}

log "===== supervisor starting (rollover_s=${ROLLOVER_S}) ====="
current_log="$(start_bot)"
cycle_start=$(date +%s)
last_restart=$(date +%s)

while true; do
    sleep 15
    now=$(date +%s)
    cycle_age=$((now - cycle_start))

    # Reason 1: scheduled rollover
    if [ "${cycle_age}" -ge "${ROLLOVER_S}" ]; then
        log "rollover after ${cycle_age}s — graceful restart"
        tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null
        sleep 3
        pkill -f 'pgg2_v48_drylive_harness' 2>/dev/null
        sleep 2
        if [ $((now - last_restart)) -lt "${MIN_RESTART_INTERVAL_S}" ]; then
            log "throttling restart — sleeping to avoid thrash"
            sleep "${MIN_RESTART_INTERVAL_S}"
        fi
        current_log="$(start_bot)"
        cycle_start=$(date +%s)
        last_restart=$(date +%s)
        continue
    fi

    # Reason 2: watchdog fired (fisherman sleeping detected by bot)
    if watchdog_fired "${current_log}"; then
        log "watchdog/completion signal — restarting"
        tmux kill-session -t "${TMUX_SESSION}" 2>/dev/null
        sleep 3
        pkill -f 'pgg2_v48_drylive_harness' 2>/dev/null
        sleep 2
        if [ $((now - last_restart)) -lt "${MIN_RESTART_INTERVAL_S}" ]; then
            log "throttling restart"
            sleep "${MIN_RESTART_INTERVAL_S}"
        fi
        current_log="$(start_bot)"
        cycle_start=$(date +%s)
        last_restart=$(date +%s)
        continue
    fi

    # Reason 3: process death
    if ! is_bot_alive; then
        log "bot process gone (tmux or python missing) — restarting"
        sleep 3
        if [ $((now - last_restart)) -lt "${MIN_RESTART_INTERVAL_S}" ]; then
            log "throttling restart"
            sleep "${MIN_RESTART_INTERVAL_S}"
        fi
        current_log="$(start_bot)"
        cycle_start=$(date +%s)
        last_restart=$(date +%s)
        continue
    fi
done
