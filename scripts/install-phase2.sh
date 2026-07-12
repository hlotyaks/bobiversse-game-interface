#!/usr/bin/env bash
# Install the root-owned Phase 2 controller. Run with sudo from the repository root.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_root=/usr/local/libexec/game-server-interface
config_root=/etc/game-server-interface
state_root=/var/lib/game-server-interface
log_root=/var/log/game-server-interface
api_user=game-interface-api

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo ./scripts/install-phase2.sh" >&2
    exit 1
fi

python3 "${repo_root}/tools/validate_catalog.py" "${repo_root}/deploy/etc/game-server-interface/catalog.yaml"

if ! getent group "${api_user}" >/dev/null; then
    groupadd --system "${api_user}"
fi
if ! getent passwd "${api_user}" >/dev/null; then
    useradd --system --gid "${api_user}" --home-dir /nonexistent --shell /usr/sbin/nologin --no-create-home "${api_user}"
fi

install -d -o root -g root -m 0755 "${install_root}" "${config_root}"
install -d -o root -g root -m 0700 "${state_root}" "${log_root}"
install -o root -g root -m 0755 "${repo_root}/controller/game_controller.py" "${install_root}/game_controller.py"
install -o root -g root -m 0755 "${repo_root}/controller/controller_client.py" "${install_root}/controller_client.py"
install -o root -g root -m 0755 "${repo_root}/tools/validate_catalog.py" "${install_root}/validate_catalog.py"
install -o root -g root -m 0644 "${repo_root}/deploy/etc/game-server-interface/catalog.yaml" "${config_root}/catalog.yaml"
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface-controller.service" /etc/systemd/system/game-server-interface-controller.service

# An append-only audit file permits additions but blocks truncation/removal.
if command -v chattr >/dev/null; then
    chattr -a "${log_root}/audit.jsonl" 2>/dev/null || true
fi
touch "${log_root}/audit.jsonl"
chown root:root "${log_root}/audit.jsonl"
chmod 0600 "${log_root}/audit.jsonl"
if command -v chattr >/dev/null; then
    chattr +a "${log_root}/audit.jsonl"
fi

systemctl daemon-reload
systemctl enable --now game-server-interface-controller.service
systemctl is-active --quiet game-server-interface-controller.service
"${install_root}/validate_catalog.py" "${config_root}/catalog.yaml"
printf 'Phase 2 controller is active. Socket: /run/game-server-interface/controller.sock\n'
