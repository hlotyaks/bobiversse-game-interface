#!/usr/bin/env bash
# Install the unprivileged, loopback-only Phase 3 interface. Run with sudo from repository root.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_root=/opt/game-server-interface

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo bash ./scripts/install-phase3.sh" >&2
    exit 1
fi

if ! id -u game-interface-api >/dev/null 2>&1; then
    echo "Install Phase 2 before Phase 3; game-interface-api is missing." >&2
    exit 1
fi
python3 "${repo_root}/tools/validate_catalog.py" "${repo_root}/deploy/etc/game-server-interface/catalog.yaml"

install -d -o root -g root -m 0755 "${install_root}" "${install_root}/app" "${install_root}/app/static"
install -o root -g root -m 0644 "${repo_root}/interface/Dockerfile" "${install_root}/Dockerfile"
install -o root -g root -m 0644 "${repo_root}/interface/app/server.py" "${install_root}/app/server.py"
install -o root -g root -m 0644 "${repo_root}/interface/app/static/index.html" "${install_root}/app/static/index.html"
install -o root -g root -m 0644 "${repo_root}/interface/app/static/app.css" "${install_root}/app/static/app.css"
install -o root -g root -m 0644 "${repo_root}/interface/app/static/app.js" "${install_root}/app/static/app.js"
install -o root -g root -m 0644 "${repo_root}/deploy/opt/game-server-interface/compose.yaml" "${install_root}/compose.yaml"
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface.service" /etc/systemd/system/game-server-interface.service

/usr/bin/docker compose --project-directory "${install_root}" --file "${install_root}/compose.yaml" build --pull
systemctl daemon-reload
systemctl enable game-server-interface.service
systemctl restart game-server-interface.service
systemctl is-active --quiet game-server-interface.service
for _ in $(seq 1 30); do
    if curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null; then
        break
    fi
    sleep 1
done
curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null
printf 'Phase 3 interface is active on 127.0.0.1:8080. Do not publish it beyond loopback until Phase 5.\n'
