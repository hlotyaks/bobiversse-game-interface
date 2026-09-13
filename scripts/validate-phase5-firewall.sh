#!/usr/bin/env bash
# Validate that the interface remains local and no unapproved public game rules exist.
set -euo pipefail

failures=0

if ss -ltnH '( sport = :8080 )' | grep -q .; then
    echo 'FAIL: interface port 8080 is listening; the backend must be reachable only through its protected Unix socket.' >&2
    failures=1
else
    echo 'PASS: interface port 8080 is not listening.'
fi

if [[ ! -S /run/game-server-interface/web/interface.sock ]]; then
    echo 'FAIL: protected interface Unix socket is unavailable.' >&2
    failures=1
else
    echo 'PASS: protected interface Unix socket is available.'
fi

if tailscale serve status --json | python3 -c 'import json, sys; config = json.load(sys.stdin); print(any(bool(value) for key, value in config.items() if "funnel" in key.lower()))' | grep -qx true; then
    echo 'FAIL: Tailscale Funnel appears configured; remove it before publishing this interface.' >&2
    failures=1
else
    echo 'PASS: no Tailscale Funnel configuration detected.'
fi

if sudo ufw status | grep -Eq '(^|[[:space:]])8080(/tcp)?[[:space:]].*ALLOW'; then
    echo 'FAIL: UFW permits direct inbound access to port 8080.' >&2
    failures=1
else
    echo 'PASS: UFW has no direct inbound interface-port rule.'
fi

if sudo ufw status | grep -Eq '1563[67]/udp.*Anywhere'; then
    echo 'FAIL: a game UDP port is open to Anywhere; use a Tailscale-scoped Docker firewall rule only when an instance is deployed.' >&2
    failures=1
else
    echo 'PASS: no currently configured game port is open to Anywhere.'
fi

exit "${failures}"
