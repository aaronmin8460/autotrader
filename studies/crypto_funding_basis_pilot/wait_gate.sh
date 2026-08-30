#!/bin/sh
# Wait for the Equity study to release the machine, then run the Crypto pilot.
#
# The pilot may not launch sustained scoring while the Equity 10-symbol study
# is in heavy compute. This gate polls at low frequency (no busy-loop) and
# requires several CONSECUTIVE clean checks before launching, so it cannot
# start during a brief lull between two Equity stages.
#
# Clean means: no `studies.equity_10_full` process of any kind, and the
# 1-minute load average below the threshold. Nothing is ever killed.

set -u

WORKTREE="/Volumes/AUTOTRADER_QA/worktrees/crypto-funding-basis-pilot"
VENV_PY="/Users/byeongilmin/dev/autotrader/.venv/bin/python"
LOG="/Volumes/AUTOTRADER_QA/logs/crypto-funding-basis-pilot.log"

POLL_SECONDS=120
REQUIRED_CLEAN=3          # 3 consecutive clean polls = 6 minutes quiet
LOAD_CEILING=5.0          # 1-min load average below this counts as quiet

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG"
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"
}

equity_running() {
    pgrep -f "studies.equity_10_full" > /dev/null 2>&1
}

load_1m() {
    uptime | sed 's/.*load averages*: *//' | awk '{print $1}' | tr -d ','
}

free_gb() {
    # Pages free + inactive + speculative, in GiB.
    vm_stat | awk '
        /page size of/ { for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) { ps=$i; break } }
        /Pages free/ { gsub(/\./,"",$3); f=$3 }
        /Pages inactive/ { gsub(/\./,"",$3); i=$3 }
        /Pages speculative/ { gsub(/\./,"",$3); s=$3 }
        END { printf "%.1f", (f+i+s)*ps/1073741824 }'
}

log "PHASE=wait-gate armed poll=${POLL_SECONDS}s required_clean=${REQUIRED_CLEAN} load_ceiling=${LOAD_CEILING}"

clean=0
while : ; do
    if equity_running; then
        [ "$clean" -gt 0 ] && log "PHASE=wait-gate equity reappeared, clean streak reset"
        clean=0
        state="EQUITY_HEAVY"
    else
        load=$(load_1m)
        if awk "BEGIN{exit !($load < $LOAD_CEILING)}"; then
            clean=$((clean + 1))
            state="QUIET"
        else
            clean=0
            state="BUSY_OTHER"
        fi
    fi
    log "PHASE=wait-gate state=${state} load1m=$(load_1m) free_gb=$(free_gb) clean=${clean}/${REQUIRED_CLEAN}"
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

log "PHASE=wait-gate heavy scoring process exited rc=$?"
