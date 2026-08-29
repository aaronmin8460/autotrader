#!/bin/sh
# Operator front-end for the V4 prep integration orchestrator.
#
# A thin wrapper: every decision lives in orchestrator.py, and this file only
# spells the commands so nobody has to remember them.
#
#   integration-ctl.sh status         every pipeline stage, gate and revision
#   integration-ctl.sh check          fetch and report source readiness only
#   integration-ctl.sh prep-status    the v4-prep integration on its own
#   integration-ctl.sh step           advance the pipeline now
#   integration-ctl.sh run-once       run the v4-prep integration only
#   integration-ctl.sh agent-check    prove the coding agent runs unattended
#   integration-ctl.sh clear-stop     resume after a person resolved a stop
#   integration-ctl.sh enable         start the five-minute LaunchAgent
#   integration-ctl.sh disable        stop it
#   integration-ctl.sh log            tail the orchestrator log
#   integration-ctl.sh report         show the most recent integration report
#   integration-ctl.sh agent          show the LaunchAgent's state

set -u

LABEL="com.autotrader.integration-orchestrator"
QA_ROOT="${AUTOTRADER_QA:-/Volumes/AUTOTRADER_QA}"
INSTALL_DIR="${QA_ROOT}/integration-orchestrator"
ORCHESTRATOR="${INSTALL_DIR}/orchestrator.py"
PIPELINE="${INSTALL_DIR}/pipeline.py"
LOG_DIR="${QA_ROOT}/logs/integration-orchestrator"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

die() { echo "$*" >&2; exit 1; }

require_volume() {
    /sbin/mount | grep -q " on ${QA_ROOT} (apfs" \
        || die "${QA_ROOT} is not mounted; nothing to do."
}

find_python() {
    for candidate in \
        "${AUTOTRADER_INTEGRATION_PYTHON:-}" \
        /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3
    do
        [ -n "${candidate}" ] || continue
        [ -x "${candidate}" ] || continue
        if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)'
        then
            echo "${candidate}"
            return 0
        fi
    done
    return 1
}

orchestrate() {
    require_volume
    [ -f "${ORCHESTRATOR}" ] || die "orchestrator is not installed at ${ORCHESTRATOR}"
    PYTHON="$(find_python)" || die "no python 3.11 or newer found"
    exec "${PYTHON}" "${ORCHESTRATOR}" "$@"
}

pipeline() {
    require_volume
    [ -f "${PIPELINE}" ] || die "pipeline is not installed at ${PIPELINE}"
    PYTHON="$(find_python)" || die "no python 3.11 or newer found"
    exec "${PYTHON}" "${PIPELINE}" "$@"
}

case "${1:-status}" in
    status)      pipeline status ;;
    step)        pipeline step ;;
    agent-check) pipeline agent-check ;;
    clear-stop)  pipeline clear-hard-stop --confirm ;;
    prep-status) orchestrate status ;;
    check)       orchestrate check ;;
    run-once)    orchestrate run-once ;;
    watch)     shift; orchestrate watch "$@" ;;
    enable)
        [ -f "${PLIST}" ] || die "no LaunchAgent installed at ${PLIST}"
        launchctl bootstrap "${DOMAIN}" "${PLIST}" 2>/dev/null
        launchctl enable "${DOMAIN}/${LABEL}"
        echo "enabled ${LABEL}"
        ;;
    disable)
        launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null
        echo "disabled ${LABEL} (the orchestrator itself is untouched)"
        ;;
    agent)
        launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null \
            | sed -n '1,/^$/p;/state = /p;/last exit code/p;/run interval/p' \
            || echo "${LABEL} is not loaded"
        ;;
    log)
        require_volume
        for name in orchestrator.log launchd-run.log; do
            [ -f "${LOG_DIR}/${name}" ] || continue
            echo "=== ${LOG_DIR}/${name} ==="
            tail -n "${2:-40}" "${LOG_DIR}/${name}"
        done
        [ -f "${LOG_DIR}/latest-status.txt" ] && {
            echo "=== ${LOG_DIR}/latest-status.txt ==="
            cat "${LOG_DIR}/latest-status.txt"
        }
        ;;
    report)
        require_volume
        latest="$(ls -1t "${QA_ROOT}"/reports/development-pipeline-*.md \
                          "${QA_ROOT}"/reports/v4-prep-integration-*.md 2>/dev/null | head -1)"
        [ -n "${latest}" ] || die "no integration report has been written yet."
        echo "=== ${latest} ==="
        cat "${latest}"
        ;;
    *)
        sed -n '2,24p' "$0"
        exit 2
        ;;
esac
