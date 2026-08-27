#!/usr/bin/env bash
# Install the read-only usage-metering diagnostic wrapper and its scoped NOPASSWD sudoers rule.
# Run with sudo from the repository root. See deploy/usr/local/sbin/gsi-diagnose for what it exposes.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (sudo)" >&2
  exit 1
fi

install -o root -g root -m 0755 "${repo_root}/deploy/usr/local/sbin/gsi-diagnose" /usr/local/sbin/gsi-diagnose

# Validate the sudoers fragment in isolation before installing it -- a broken file in
# /etc/sudoers.d/ can lock out sudo entirely.
tmp_sudoers="$(mktemp)"
trap 'rm -f "$tmp_sudoers"' EXIT
cp "${repo_root}/deploy/etc/sudoers.d/gsi-diagnose" "$tmp_sudoers"
if ! visudo -cf "$tmp_sudoers"; then
  echo "sudoers fragment failed validation; not installing" >&2
  exit 1
fi
install -o root -g root -m 0440 "${repo_root}/deploy/etc/sudoers.d/gsi-diagnose" /etc/sudoers.d/gsi-diagnose

echo "diagnostics installed."
echo "try:  sudo /usr/local/sbin/gsi-diagnose all"
