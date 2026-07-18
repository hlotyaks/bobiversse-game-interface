#!/usr/bin/env python3
"""Sample who is currently playing each game instance and append it to a presence ledger.

Enshrouded (and most game servers) do not log player identity -- the server stdout only reports
an anonymous connected-machine count. But every player reaches the tailnet-bound game port from
their own Tailscale node, so we identify players by observing the live UDP flows to the game port
and resolving each peer's tailnet IP to its Tailscale login. This is game-agnostic: it works for
any UDP game server without relying on that server's logging.

Each cycle, for every registered instance the meter:
  1. lists active UDP conntrack flows to the instance's game port,
  2. keeps peers whose source is a tailnet address (100.64.0.0/10),
  3. maps each tailnet IP to a login via ``tailscale status --json``,
  4. appends one occupancy sample ``{"ts", "instance", "present": [logins]}`` to the ledger.

The parsing functions below are pure and unit-tested; the privileged shell-outs (``conntrack``,
``tailscale``) are kept in a thin layer. ``conntrack`` needs root, so this runs as a root
systemd service (see deploy/etc/systemd/system/game-presence-meter.service). It reads only flow
metadata and tailnet identities -- never game data or secrets. The ledger it writes is playtime
metadata (who played when); treat it as private and keep it root-owned like the audit log.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
CONNTRACK_SRC = re.compile(r"src=(\d{1,3}(?:\.\d{1,3}){3})")


def is_tailnet_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in TAILNET_CGNAT
    except ValueError:
        return False


def parse_conntrack_peers(conntrack_output: str, game_port: int) -> set[str]:
    """Extract distinct tailnet peer IPs from ``conntrack -L`` output for one game port.

    Only lines whose destination port is the game port are considered, and only the *original*
    direction source (the first ``src=``) is taken -- that is the real client address before any
    Docker NAT rewrites it. Peers outside the tailnet range are dropped.
    """
    port_token = f"dport={game_port} "
    peers: set[str] = set()
    for line in conntrack_output.splitlines():
        if "dport=" not in line or port_token not in f"{line} ":
            continue
        match = CONNTRACK_SRC.search(line)
        if match and is_tailnet_ip(match.group(1)):
            peers.add(match.group(1))
    return peers


def build_ip_login_map(tailscale_status: dict[str, Any]) -> dict[str, str]:
    """Map each tailnet IPv4 to its owner login from ``tailscale status --json``."""
    mapping: dict[str, str] = {}
    profiles = tailscale_status.get("User") or {}

    def login_for(user_id: Any) -> str | None:
        profile = profiles.get(str(user_id)) if isinstance(profiles, dict) else None
        if isinstance(profile, dict):
            return profile.get("LoginName")
        return None

    nodes: list[dict[str, Any]] = []
    if isinstance(tailscale_status.get("Self"), dict):
        nodes.append(tailscale_status["Self"])
    peer = tailscale_status.get("Peer")
    if isinstance(peer, dict):
        nodes.extend(node for node in peer.values() if isinstance(node, dict))
    for node in nodes:
        login = login_for(node.get("UserID"))
        if not login:
            continue
        for address in node.get("TailscaleIPs") or []:
            if isinstance(address, str) and ":" not in address:
                mapping[address] = login
    return mapping


def resolve_present(peers: set[str], ip_login: dict[str, str]) -> list[str]:
    """Resolve peer IPs to sorted, de-duplicated logins (unknown IPs fall back to the raw IP)."""
    return sorted({ip_login.get(ip, f"ip:{ip}") for ip in peers})


def _run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    return result.stdout if result.returncode == 0 else ""


def sample_instance(game_port: int, ip_login: dict[str, str], conntrack_bin: str) -> list[str]:
    output = _run([conntrack_bin, "-L", "-p", "udp", "--dport", str(game_port)])
    return resolve_present(parse_conntrack_peers(output, game_port), ip_login)


def append_ledger(ledger_path: Path, record: dict[str, Any]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    descriptor = os.open(ledger_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as ledger:
        ledger.write(line)
        ledger.flush()
        os.fsync(ledger.fileno())


def instance_ports(catalog: dict[str, Any]) -> dict[str, int]:
    """Return {"<template>-<instance>": game_port} for every allowlisted slot in the catalog."""
    ports: dict[str, int] = {}
    templates = catalog.get("templates", {})
    if not isinstance(templates, dict):
        return ports
    for template_id, template in templates.items():
        slots = template.get("instance_policy", {}).get("slots", {}) if isinstance(template, dict) else {}
        if not isinstance(slots, dict):
            continue
        for instance_id, slot in slots.items():
            udp = sorted(p["host"] for p in slot.get("ports", []) if isinstance(p, dict) and p.get("protocol") == "udp" and isinstance(p.get("host"), int))
            if udp:
                ports[f"{template_id}-{instance_id}"] = udp[0]
    return ports


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def run_cycle(catalog: dict[str, Any], ledger_path: Path, tailscale_bin: str, conntrack_bin: str) -> int:
    status_raw = _run([tailscale_bin, "status", "--json"])
    try:
        ip_login = build_ip_login_map(json.loads(status_raw)) if status_raw else {}
    except json.JSONDecodeError:
        ip_login = {}
    written = 0
    for instance, port in instance_ports(catalog).items():
        present = sample_instance(port, ip_login, conntrack_bin)
        append_ledger(ledger_path, {"ts": now(), "instance": instance, "present": present})
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample tailnet player presence per game instance into a ledger.")
    parser.add_argument("--catalog", type=Path, default=Path("/etc/game-server-interface/catalog.yaml"))
    parser.add_argument("--ledger", type=Path, default=Path("/var/lib/game-server-interface/presence.jsonl"))
    parser.add_argument("--interval", type=int, default=60, help="seconds between samples (0 = one cycle then exit)")
    parser.add_argument("--tailscale-bin", default="/usr/bin/tailscale")
    parser.add_argument("--conntrack-bin", default="/usr/sbin/conntrack")
    args = parser.parse_args()

    try:
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict):
            raise ValueError("catalog must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"presence meter failed to load catalog: {exc}", file=sys.stderr)
        return 1

    if args.interval <= 0:
        run_cycle(catalog, args.ledger, args.tailscale_bin, args.conntrack_bin)
        return 0
    while True:
        try:
            run_cycle(catalog, args.ledger, args.tailscale_bin, args.conntrack_bin)
        except Exception as exc:  # keep the daemon alive across transient failures
            print(f"presence meter cycle error: {exc}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
