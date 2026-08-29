#!/bin/sh
# Install the integration orchestrator as a user-level LaunchAgent.
#
# Three places are written, and only these three:
#
#   ${AUTOTRADER_QA}/integration-orchestrator/     the stable copy the agent
#                                                  runs, plus the commit it
#                                                  came from
#   ~/Library/Application Support/AutoTrader/      the bootstrap launchd starts
#   ~/Library/LaunchAgents/                        the agent definition
#
# No sudo, no root, no system domain, and no shell startup file is touched.
#
# The installed copy is taken from a committed tree: the agent must never run
# source that only exists in somebody's working directory.

set -eu

LABEL="com.autotrader.integration-orchestrator"
QA_ROOT="${AUTOTRADER_QA:-/Volumes/AUTOTRADER_QA}"
HERE="$(cd "$(dirname "$0")" && pwd)"

INSTALL_DIR="${QA_ROOT}/integration-orchestrator"
SUPPORT_DIR="${HOME}/Library/Application Support/AutoTrader/integration-orchestrator"
BOOTSTRAP="${SUPPORT_DIR}/bootstrap.sh"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
PLIST="${LAUNCH_AGENTS}/${LABEL}.plist"
LAUNCHD_LOG="${HOME}/Library/Logs/autotrader-integration-orchestrator.log"
DOMAIN="gui/$(id -u)"

die() { echo "install: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your own user, never as root."

/sbin/mount | grep -q " on ${QA_ROOT} (apfs" \
    || die "${QA_ROOT} is not mounted; refusing to install."

# ---------------------------------------------------------------------------
# The agent must run committed source.
# ---------------------------------------------------------------------------

SOURCE_COMMIT="$(git -C "${HERE}" rev-parse HEAD)"
SOURCE_BRANCH="$(git -C "${HERE}" rev-parse --abbrev-ref HEAD)"
if [ -n "$(git -C "${HERE}" status --porcelain -- "${HERE}")" ]; then
    die "tools/integration has uncommitted changes; commit them before installing."
fi

# ---------------------------------------------------------------------------
# The stable external copy
# ---------------------------------------------------------------------------

mkdir -p "${INSTALL_DIR}"
for name in orchestrator.py pipeline.py bootstrap.sh integration-ctl.sh; do
    cp "${HERE}/${name}" "${INSTALL_DIR}/${name}"
done
rm -rf "${INSTALL_DIR}/specs"
cp -R "${HERE}/specs" "${INSTALL_DIR}/specs"
chmod +x "${INSTALL_DIR}/bootstrap.sh" "${INSTALL_DIR}/integration-ctl.sh"

cat > "${INSTALL_DIR}/INSTALLED_FROM.json" <<JSON
{
  "installed_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "source_repository": "$(git -C "${HERE}" rev-parse --show-toplevel)",
  "source_branch": "${SOURCE_BRANCH}",
  "source_commit": "${SOURCE_COMMIT}",
  "label": "${LABEL}"
}
JSON

# ---------------------------------------------------------------------------
# The internal bootstrap
#
# On the internal disk on purpose: launchd needs a program that exists whether
# or not the external volume is mounted, so that an unmounted workspace is a
# clean no-op instead of a spawn failure every five minutes.
# ---------------------------------------------------------------------------

mkdir -p "${SUPPORT_DIR}" "${LAUNCH_AGENTS}" "$(dirname "${LAUNCHD_LOG}")"
cp "${HERE}/bootstrap.sh" "${BOOTSTRAP}"
chmod +x "${BOOTSTRAP}"

# ---------------------------------------------------------------------------
# The agent definition
# ---------------------------------------------------------------------------

TEMPLATE="${HERE}/${LABEL}.plist"
[ -f "${TEMPLATE}" ] || die "missing plist template at ${TEMPLATE}"

sed \
    -e "s|__BOOTSTRAP__|${BOOTSTRAP}|g" \
    -e "s|__LAUNCHD_LOG__|${LAUNCHD_LOG}|g" \
    -e "s|__QA_ROOT__|${QA_ROOT}|g" \
    "${TEMPLATE}" > "${PLIST}.new"

plutil -lint "${PLIST}.new" >/dev/null || die "the rendered plist is not valid."
mv "${PLIST}.new" "${PLIST}"

# ---------------------------------------------------------------------------
# Load it into the user domain. No sudo; gui/<uid> is this login session.
# ---------------------------------------------------------------------------

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "${PLIST}"
launchctl enable "${DOMAIN}/${LABEL}"

# ---------------------------------------------------------------------------
# The operator shortcut, which only forwards to the installed control script.
# ---------------------------------------------------------------------------

cat > "${QA_ROOT}/integration-status.sh" <<SHIM
#!/bin/sh
# Shortcut to the installed integration orchestrator control script.
exec "${INSTALL_DIR}/integration-ctl.sh" "\${@:-status}"
SHIM
chmod +x "${QA_ROOT}/integration-status.sh"

echo "installed ${LABEL}"
echo "  from      ${SOURCE_BRANCH} ${SOURCE_COMMIT}"
echo "  runs      ${INSTALL_DIR}/pipeline.py step"
echo "  via       ${BOOTSTRAP}"
echo "  agent     ${PLIST}"
echo "  operator  ${QA_ROOT}/integration-status.sh"
