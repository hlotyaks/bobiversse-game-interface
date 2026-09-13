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
# Ensure TRUSTED_ACTOR_HEADER=1 WITHOUT discarding anything else in the file. This used to
# truncate interface.env on every run, which silently wiped GAME_INTERFACE_ADMIN_LOGINS and
# demoted every administrator to an ordinary viewer -- the dashboard simply stopped showing
# admin controls, with nothing logged and no error. Operator-set values live here; treat the
# file as configuration to be reconciled, never as a file to rewrite.
env_file="${config_root}/interface.env"
if [[ ! -e ${env_file} ]]; then
    install -o root -g root -m 0644 /dev/null "${env_file}"
fi
if grep -q '^TRUSTED_ACTOR_HEADER=' "${env_file}"; then
    sed -i 's/^TRUSTED_ACTOR_HEADER=.*/TRUSTED_ACTOR_HEADER=1/' "${env_file}"
else
    printf 'TRUSTED_ACTOR_HEADER=1\n' >> "${env_file}"
fi
if ! grep -q '^GAME_INTERFACE_ADMIN_LOGINS=' "${env_file}"; then
    # Present but empty denies everyone, which is the safe default and makes the setting
    # discoverable rather than leaving an operator to guess the variable name.
    printf 'GAME_INTERFACE_ADMIN_LOGINS=\n' >> "${env_file}"
fi
chmod 0644 "${env_file}"
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
systemctl enable game-server-interface-serve.service
# restart, not "enable --now": --now will not reload a unit that is already running, leaving the
# previously-started process on the old code after a reinstall.
systemctl restart game-server-interface-serve.service
bash "${repo_root}/scripts/validate-phase5-firewall.sh"
tailscale serve status --json
printf 'Private HTTPS interface: https://bobiverse.tail40344b.ts.net/\n'
