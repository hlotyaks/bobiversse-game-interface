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

# The default 'tailscale' source needs the tailscale CLI (identity) and docker (the game's own
# connected-client count). conntrack is only needed if switched to '--source conntrack'.
command -v tailscale >/dev/null 2>&1 || echo "WARN: tailscale CLI not found -- the default presence source needs it" >&2
command -v docker >/dev/null 2>&1 || echo "WARN: docker CLI not found -- the default source reads game client counts via 'docker logs'" >&2
command -v conntrack >/dev/null 2>&1 || echo "note: conntrack not installed (only needed for --source conntrack)" >&2

install -d -o root -g root -m 0700 /var/lib/game-server-interface
install -o root -g root -m 0755 "${repo_root}/tools/presence_meter.py" "${install_root}/presence_meter.py"
install -o root -g root -m 0755 "${repo_root}/tools/billing.py" "${install_root}/billing.py"
install -o root -g root -m 0755 "${repo_root}/tools/ledger_admin.py" "${install_root}/ledger_admin.py"
install -o root -g root -m 0755 "${repo_root}/scripts/observe-presence.py" "${install_root}/observe-presence.py"
install -o root -g root -m 0755 "${repo_root}/tools/backfill_presence.py" "${install_root}/backfill_presence.py"

# Root-owned billing config (nominal dry-run rates); do not overwrite an edited copy.
install -d -o root -g root -m 0755 /etc/game-server-interface
if [[ ! -e /etc/game-server-interface/billing.yaml ]]; then
  install -o root -g root -m 0644 "${repo_root}/deploy/etc/game-server-interface/billing.yaml" /etc/game-server-interface/billing.yaml
else
  echo "keeping existing /etc/game-server-interface/billing.yaml"
fi

# Per-game presence exclusions. Seed the initial map (Enshrouded excludes the non-playing admin) but
# never overwrite the live copy: after install it is owned by the controller and edited by admins via
# the interface. The controller (running as root) rewrites it atomically; the meter re-reads it each
# cycle. Root-owned and 0600 like the ledger -- it is playtime/config metadata, not world data.
# Player identity map (in-game ID -> tailnet login). Seeded with the IDs seen so far and blank
# logins for an admin to fill in; never overwrite a live copy, which holds real mappings.
if [[ ! -e /var/lib/game-server-interface/player-identities.json ]]; then
  install -o root -g root -m 0600 "${repo_root}/deploy/var/lib/game-server-interface/player-identities.json" /var/lib/game-server-interface/player-identities.json
else
  echo "keeping existing /var/lib/game-server-interface/player-identities.json"
fi

if [[ ! -e /var/lib/game-server-interface/presence-exclusions.json ]]; then
  install -o root -g root -m 0600 "${repo_root}/deploy/var/lib/game-server-interface/presence-exclusions.json" /var/lib/game-server-interface/presence-exclusions.json
else
  echo "keeping existing /var/lib/game-server-interface/presence-exclusions.json"
fi

install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-presence-meter.service" /etc/systemd/system/game-presence-meter.service

# Diagnostic observer: records what attribution SAW, not just what it concluded. Read-only; it
# never writes the ledger. See docs/usage-metering.md ("Counting and naming are separate problems").
install -o root -g root -m 0644 "${repo_root}/deploy/etc/systemd/system/game-presence-observer.service" /etc/systemd/system/game-presence-observer.service
install -o root -g root -m 0644 "${repo_root}/deploy/etc/logrotate.d/game-server-interface" /etc/logrotate.d/game-server-interface

systemctl daemon-reload
systemctl enable game-presence-meter.service
# restart, not "enable --now": --now is a no-op when the unit is already running, so a reinstall
# would leave the OLD python process serving the OLD code from memory while the new file sat on
# disk. That silently swallowed a meter fix for four weeks (2026-07-28 .. 2026-08-27).
systemctl restart game-presence-meter.service
systemctl enable game-presence-observer.service
systemctl restart game-presence-observer.service

echo "presence meter installed and started."
echo "observer: sudo /usr/local/sbin/gsi-diagnose observer"
echo "report:  sudo /usr/local/libexec/game-server-interface/billing.py --instance enshrouded-primary"
