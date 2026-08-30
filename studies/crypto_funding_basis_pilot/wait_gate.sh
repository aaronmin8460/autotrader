#!/bin/sh
# Wait for the Equity study to release the machine, then run the Crypto pilot.
#
# The pilot may not launch sustained scoring while the Equity 10-symbol study
# is in heavy compute. This gate polls at low frequency (no busy-loop) and
# requires several CONSECUTIVE clean checks before launching, so it cannot
# start during a brief lull between two Equity stages. Nothing is ever killed.
#
# WHY NOT LOAD AVERAGE. An earlier version of this gate required
# `load1m < 5.0`. That was wrong on this machine: with zero research processes
# running, the ambient load from the GUI stack and eight concurrent agent
# sessions is already 6-7, so the condition could never have been satisfied and
# the gate would have waited forever. The meaningful signal is CPU-bound
# *research* processes, which is what "no other sustained heavy study" actually
# means. Load average is kept only as a loose sanity guard against a heavy
# non-Python job, set well above the measured ambient.

set -u

WORKTREE="/Volumes/AUTOTRADER_QA/worktrees/crypto-funding-basis-pilot"
VENV_PY="/Users/byeongilmin/dev/autotrader/.venv/bin/python"
LOG="/Volumes/AUTOTRADER_QA/logs/crypto-funding-basis-pilot.log"

POLL_SECONDS=120
REQUIRED_CLEAN=3          # 3 consecutive clean polls = 6 minutes quiet
BUSY_CPU_PERCENT=40       # a python process above this is doing real work
LOAD_SANITY_CEILING=9.0   # measured ambient is 6-7; this only catches a spike

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG"
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
}

equity_running() {
    pgrep -f "studies.equity_10_full" > /dev/null 2>&1
}

# CPU-bound python processes that are not this pilot's own.
foreign_busy_python() {
    ps -Ao pcpu,args |
        grep -i python |
        grep -v grep |
        grep -v "crypto_funding_basis_pilot" |
        awk -v t="$BUSY_CPU_PERCENT" '$1 > t' |
        wc -l | tr -d ' '
}

load_1m() {
    uptime | sed 's/.*load averages*: *//' | awk '{print $1}' | tr -d ','
}

free_gb() {
    vm_stat | awk '
        /page size of/ { for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) { ps=$i; break } }
        /Pages free/ { gsub(/\./,"",$3); f=$3 }
        /Pages inactive/ { gsub(/\./,"",$3); i=$3 }
        /Pages speculative/ { gsub(/\./,"",$3); s=$3 }
        END { printf "%.1f", (f+i+s)*ps/1073741824 }'
}

log "PHASE=wait-gate armed poll=${POLL_SECONDS}s required_clean=${REQUIRED_CLEAN} busy_cpu=${BUSY_CPU_PERCENT}% load_sanity=${LOAD_SANITY_CEILING}"

clean=0
while : ; do
    busy=$(foreign_busy_python)
    load=$(load_1m)
    if equity_running; then
        [ "$clean" -gt 0 ] && log "PHASE=wait-gate equity reappeared, clean streak reset"
        clean=0
        state="EQUITY_HEAVY"
    elif [ "$busy" -gt 0 ]; then
        clean=0
        state="OTHER_HEAVY_PYTHON"
    elif awk "BEGIN{exit !($load >= $LOAD_SANITY_CEILING)}"; then
        clean=0
        state="LOAD_SPIKE"
    else
        clean=$((clean + 1))
        state="QUIET"
    fi
    log "PHASE=wait-gate state=${state} busy_python=${busy} load1m=${load} free_gb=$(free_gb) clean=${clean}/${REQUIRED_CLEAN}"
    [ "$clean" -ge "$REQUIRED_CLEAN" ] && break
    sleep "$POLL_SECONDS"
done

FREE=$(free_gb)
if awk "BEGIN{exit !($FREE >= 3.0)}"; then
    WORKERS=6
else
    WORKERS=4
fi
log "PHASE=wait-gate CLEARED free_gb=${FREE} -> workers=${WORKERS}; starting heavy scoring"

cd "$WORKTREE" || exit 1
export PYTHONPYCACHEPREFIX="/Volumes/AUTOTRADER_QA/caches/pycache/crypto-funding-basis"
export PYTHONPATH=src
"$VENV_PY" -m studies.crypto_funding_basis_pilot.run_pilot \
    --workers "$WORKERS" --arms baseline,augmented --tag main \
    >> /Volumes/AUTOTRADER_QA/logs/crypto-funding-basis-stdout.log 2>&1
rc=$?

log "PHASE=wait-gate heavy scoring process exited rc=${rc}"
