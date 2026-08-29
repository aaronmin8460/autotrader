#!/bin/sh
# LaunchAgent entry point for the V4 prep integration orchestrator.
#
# This file is installed on the internal disk so launchd always has something
# it can start, even while the external workspace is unmounted. It performs no
# integration work of its own: it checks that the workspace is really mounted,
# picks an interpreter new enough to run the orchestrator, and hands over.
#
# Everything it cannot do without the external volume, it declines to do.

set -u

QA_ROOT="${AUTOTRADER_QA:-/Volumes/AUTOTRADER_QA}"
INSTALL_DIR="${QA_ROOT}/integration-orchestrator"
ORCHESTRATOR="${INSTALL_DIR}/orchestrator.py"
PIPELINE="${INSTALL_DIR}/pipeline.py"
LOG_DIR="${QA_ROOT}/logs/integration-orchestrator"

# The external workspace must be a real mounted APFS volume. A path that merely
# exists is not enough: an unmounted mount point is an empty directory on the
# boot disk, and writing caches or logs into it silently fills the internal SSD.
if ! /sbin/mount | grep -q " on ${QA_ROOT} (apfs"; then
    exit 0
fi

[ -f "${ORCHESTRATOR}" ] || exit 0
[ -f "${PIPELINE}" ] || exit 0

# The orchestrator needs 3.11 or newer. macOS ships 3.9 at /usr/bin/python3, so
# an explicit search beats whatever `python3` happens to resolve to.
PYTHON=""
for candidate in \
    "${AUTOTRADER_INTEGRATION_PYTHON:-}" \
    /usr/local/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/bin/python3
do
    [ -n "${candidate}" ] || continue
    [ -x "${candidate}" ] || continue
    if "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
    then
        PYTHON="${candidate}"
        break
    fi
done

if [ -z "${PYTHON}" ]; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') no python 3.11+ available; skipping" >&2
    exit 0
fi

mkdir -p "${LOG_DIR}" || exit 0

# Caches and temporary files stay on the external workspace, exactly as
# session-env.sh arranges them for an interactive session.
export AUTOTRADER_QA="${QA_ROOT}"
export TMPDIR="${QA_ROOT}/tmp"
export npm_config_cache="${QA_ROOT}/caches/npm"
export PIP_CACHE_DIR="${QA_ROOT}/caches/pip"
export UV_CACHE_DIR="${QA_ROOT}/caches/uv"
export PYTHONPYCACHEPREFIX="${QA_ROOT}/caches/pycache"
export PLAYWRIGHT_BROWSERS_PATH="${QA_ROOT}/caches/playwright"
mkdir -p "${TMPDIR}" "${npm_config_cache}" "${PIP_CACHE_DIR}" "${UV_CACHE_DIR}" \
         "${PYTHONPYCACHEPREFIX}" 2>/dev/null

# One entry point, one lock. The pipeline runs the v4-prep integration itself
# and then advances the development stages, so there is no second watcher and
# no way for two invocations to overlap.
#
# Bulk output belongs on the external volume, never on the internal disk.
exec "${PYTHON}" "${PIPELINE}" step >>"${LOG_DIR}/launchd-run.log" 2>&1
