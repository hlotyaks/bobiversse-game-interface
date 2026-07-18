#!/usr/bin/env bash
# Install Stage 1 usage metering (presence meter + billing calculator). Dry-run only: this
# measures playtime and computes a hypothetical cost-share bill. It moves no money and stores no
# payment credentials. Run with sudo from the repository root. See docs/usage-metering.md.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install_root=/usr/local/libexec/game-server-interface

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (sudo)" >&2
  exit 1
fi

# The default 'tailscale' source needs the tailscale CLI. conntrack is only needed if the meter
# is switched to '--source conntrack' (a future non-Tailscale deployment).
command -v tailscale >/dev/null 2>&1 || echo "WARN: tailscale CLI not found -- the default presence source needs it" >&2
command -v conntrack >/dev/null 2>&1 || echo "note: conntrack not installed (only needed for --source conntrack)" >&2

install -d -o root -g root -m 0700 /var/lib/game-server-interface
install -o root -g root -m 0755 "${repo_root}/tools/presence_meter.py" "${install_root}/presence_meter.py"
install -o root -g root -m 0755 "${repo_root}/tools/billing.py" "${install_root}/billing.py"

# Root-owned billing config (nominal dry-run rates); do not overwrite an edited copy.
install -d -o root -g root -m 0755 /etc/game-server-interface
if [[ ! -e /etc/game-server-interface/billing.yaml ]]; then
  install -o root -g root -m 0644 "${repo_root}/deploy/etc/game-server-interface/billing.yaml" /etc/game-server-interface/billing.yaml
else
  echo "keeping existing /etc/game-server-interface/billing.yaml"
fi

install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-presence-meter.service" /etc/systemd/system/game-presence-meter.service

systemctl daemon-reload
systemctl enable --now game-presence-meter.service

echo "presence meter installed and started."
echo "report:  sudo /usr/local/libexec/game-server-interface/billing.py --instance enshrouded-primary"
