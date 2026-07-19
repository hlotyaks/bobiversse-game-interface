#!/usr/bin/env bash
# One-time setup for pull-based auto-deploy (GitOps). Run with sudo from the repository root:
#
#     sudo scripts/install-autodeploy.sh
#
# It creates a dedicated deploy clone (separate from your dev checkout, so auto-deploy never
# clobbers in-progress work), installs the stable root wrapper + systemd timer, and starts polling
# origin/<branch>. Override defaults with env vars: DEPLOY_USER, BRANCH, CLONE, ORIGIN_URL.
#
# Credentials: the clone is owned by DEPLOY_USER (who holds the git credential for the private repo);
# the timer runs as root but drops to DEPLOY_USER for every git operation. Root never needs its own
# GitHub credential.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo scripts/install-autodeploy.sh" >&2
    exit 1
fi

DEPLOY_USER=${DEPLOY_USER:-cbrinton}
BRANCH=${BRANCH:-main}
CLONE=${CLONE:-/srv/game-autodeploy/repo}

if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
    echo "deploy user '${DEPLOY_USER}' does not exist" >&2
    exit 1
fi

# Derive the origin URL from this checkout (read it as the deploy user to avoid git's dubious-ownership
# guard on a differently-owned checkout).
ORIGIN_URL=${ORIGIN_URL:-$(sudo -u "${DEPLOY_USER}" -H git -C "${repo_root}" remote get-url origin 2>/dev/null || true)}
if [[ -z ${ORIGIN_URL} ]]; then
    echo "could not determine ORIGIN_URL; pass it explicitly (ORIGIN_URL=... sudo -E scripts/install-autodeploy.sh)" >&2
    exit 1
fi

echo "deploy user : ${DEPLOY_USER}"
echo "branch      : ${BRANCH}"
echo "clone       : ${CLONE}"
echo "origin      : ${ORIGIN_URL}"

# Create the clone (owned by the deploy user so its credential helper authenticates the fetch).
clone_parent=$(dirname -- "${CLONE}")
install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 0755 "${clone_parent}"
if [[ ! -d ${CLONE}/.git ]]; then
    echo "==> cloning ${ORIGIN_URL} to ${CLONE}"
    sudo -u "${DEPLOY_USER}" -H git clone --branch "${BRANCH}" "${ORIGIN_URL}" "${CLONE}"
else
    echo "==> deploy clone already present; fetching"
    sudo -u "${DEPLOY_USER}" -H git -C "${CLONE}" fetch --prune origin
    sudo -u "${DEPLOY_USER}" -H git -C "${CLONE}" checkout --quiet "${BRANCH}"
    sudo -u "${DEPLOY_USER}" -H git -C "${CLONE}" reset --hard --quiet "origin/${BRANCH}"
fi

# Let root run read-only git (the diff in deploy.sh) inside the deploy-user-owned clone.
git config --system --add safe.directory "${CLONE}"

# Config consumed by the wrapper.
install -d -o root -g root -m 0755 /etc/game-server-interface
tmp_conf=$(mktemp)
cat >"${tmp_conf}" <<EOF
# Auto-deploy configuration (read by /usr/local/sbin/game-autodeploy).
CLONE=${CLONE}
DEPLOY_USER=${DEPLOY_USER}
BRANCH=${BRANCH}
EOF
install -o root -g root -m 0644 "${tmp_conf}" /etc/game-server-interface/autodeploy.conf
rm -f "${tmp_conf}"

# Stable root wrapper + units.
install -o root -g root -m 0755 "${repo_root}/deploy/usr/local/sbin/game-autodeploy" /usr/local/sbin/game-autodeploy
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-autodeploy.service" /etc/systemd/system/game-autodeploy.service
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-autodeploy.timer" /etc/systemd/system/game-autodeploy.timer

systemctl daemon-reload
systemctl enable --now game-autodeploy.timer

echo
echo "auto-deploy is installed and polling origin/${BRANCH} every 2 minutes."
echo "  status : systemctl status game-autodeploy.timer"
echo "  logs   : journalctl -u game-autodeploy.service -f"
echo "  once   : sudo systemctl start game-autodeploy.service"
echo "  last   : sudo cat /var/lib/game-server-interface/autodeploy-status.json"
echo
echo "IMPORTANT: protect the ${BRANCH} branch on GitHub (require a PR review) -- anything merged there"
echo "now runs as root on this host automatically."
