#!/usr/bin/env bash
# Configure private Tailscale Serve publishing after the tailnet ACL is reviewed.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config_root=/etc/game-server-interface

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo bash ./scripts/install-phase5.sh" >&2
    exit 1
fi

if ! systemctl is-active --quiet game-server-interface.service; then
    echo "Phase 3 interface must be active before publishing." >&2
    exit 1
fi
if ! systemctl is-active --quiet tailscaled.service; then
    echo "tailscaled is not active." >&2
    exit 1
fi

install -d -o root -g root -m 0755 "${config_root}"
install -o root -g root -m 0644 /dev/null "${config_root}/interface.env"
printf 'TRUSTED_ACTOR_HEADER=1\n' > "${config_root}/interface.env"
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface.service" /etc/systemd/system/game-server-interface.service
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-server-interface-serve.service" /etc/systemd/system/game-server-interface-serve.service

# Remove stale, Internet-wide game firewall rules. No game instance is deployed yet.
if ufw status | grep -q '15636/udp.*Anywhere'; then ufw --force delete allow 15636/udp; fi
if ufw status | grep -q '15637/udp.*Anywhere'; then ufw --force delete allow 15637/udp; fi

systemctl daemon-reload
systemctl restart game-server-interface.service
for _ in $(seq 1 30); do
    if [[ -S /run/game-server-interface/web/interface.sock ]]; then
        break
    fi
    sleep 1
done
[[ -S /run/game-server-interface/web/interface.sock ]]
systemctl enable --now game-server-interface-serve.service
bash "${repo_root}/scripts/validate-phase5-firewall.sh"
tailscale serve status --json
printf 'Private HTTPS interface: https://bobiverse.tail40344b.ts.net/\n'
