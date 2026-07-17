#!/usr/bin/env bash
# Verify that published game UDP ports are exposed only on the host's Tailscale IP, and
# remove the obsolete DOCKER-USER source-filter rules if they are present.
#
# Why this is a verifier and not a DOCKER-USER filter: Docker here runs with the userland
# proxy (docker-proxy), which relays inbound packets to the container with their source
# rewritten to the bridge gateway (e.g. 172.19.0.1). A DOCKER-USER rule that accepts only
# tailnet sources therefore (a) drops all real game traffic and (b) provides no real
# protection, because it can no longer see the original client address. The effective,
# working control is publishing the port on the tailnet IP only (100.x, which exists solely
# on tailscale0). render_instance.py already binds published ports to that IP.
#
# Usage: sudo ./scripts/game-firewall.sh <udp-port> [<udp-port> ...]
#   e.g. sudo ./scripts/game-firewall.sh 15636 15637
set -Eeuo pipefail

comment_prefix="game-server-interface"
legacy_applier=/usr/local/sbin/apply-game-firewall
legacy_unit=/etc/systemd/system/game-firewall.service

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo ./scripts/game-firewall.sh <udp-port> ..." >&2
    exit 1
fi
if [[ $# -lt 1 ]]; then
    echo "Usage: sudo ./scripts/game-firewall.sh <udp-port> [<udp-port> ...]" >&2
    exit 2
fi
for port in "$@"; do
    if ! [[ $port =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
        echo "Error: invalid UDP port '${port}'." >&2
        exit 2
    fi
done

# 1. Remove the obsolete source-filter rules and their boot applier if a prior version left
#    them behind. Delete by rule index (recomputed each pass, since indices shift), which
#    avoids the quoting pitfalls of round-tripping `iptables -S` output back into a delete.
removed=0
while :; do
    line=$(iptables -S DOCKER-USER 2>/dev/null | grep -n -- "${comment_prefix}" | head -1 | cut -d: -f1 || true)
    [[ -z ${line} ]] && break
    iptables -D DOCKER-USER "$((line - 1))"
    removed=$((removed + 1))
done
[[ ${removed} -gt 0 ]] && echo "Removed ${removed} obsolete DOCKER-USER game rule(s)."
if [[ -e ${legacy_unit} ]]; then
    systemctl disable --now game-firewall.service >/dev/null 2>&1 || true
    rm -f "${legacy_unit}"
    systemctl daemon-reload
fi
rm -f "${legacy_applier}"

# 2. Verify each port is published only on the tailnet IP and never on 0.0.0.0 / :: .
tailnet_ip=$(tailscale ip -4 2>/dev/null | head -1 || true)
if [[ -z ${tailnet_ip} ]]; then
    echo "WARNING: could not determine the tailnet IP (is tailscaled up?); skipping bind check." >&2
    exit 0
fi

status=0
for port in "$@"; do
    listen=$(ss -ulnH "( sport = :${port} )" 2>/dev/null | awk '{print $5}')
    if [[ -z ${listen} ]]; then
        echo "NOTE  udp/${port}: nothing is listening yet (start the instance first)."
        continue
    fi
    if grep -qE '(^|[^0-9])(0\.0\.0\.0|\[::\]|\*):' <<<"${listen}"; then
        echo "FAIL  udp/${port}: exposed on a wildcard address (${listen//$'\n'/ }); it must bind only to ${tailnet_ip}." >&2
        status=1
    elif grep -q "${tailnet_ip}:" <<<"${listen}"; then
        echo "PASS  udp/${port}: bound to the tailnet IP ${tailnet_ip} only."
    else
        echo "WARN  udp/${port}: bound to ${listen//$'\n'/ } (not the tailnet IP ${tailnet_ip}); confirm this is intended." >&2
        status=1
    fi
done
exit "${status}"
