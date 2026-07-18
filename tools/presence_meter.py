#!/usr/bin/env python3
"""Sample who is playing each game instance and append it to a presence ledger.

Game servers (Enshrouded included) do not log player identity, so the meter derives it from the
network layer. It supports two interchangeable presence sources; both append the same ledger
record ``{"ts", "instance", "present": [logins]}`` and differ only in how they observe players:

- ``tailscale`` (default, current bobiverse deployment): players reach the game over the tailnet,
  so their packets arrive *inside* the WireGuard tunnel and the kernel's conntrack never sees a
  ``client -> game-port`` flow (verified 2026-07-18 -- see docs/presence-source-conntrack-findings.md).
  Instead we read ``tailscale status --json``: a peer that is ``Active`` and exchanging traffic
  above a rate threshold with the host is playing, and its login is the identity. Attribution is
  gated to instances whose systemd unit is active.

- ``conntrack`` (for a future cloud / public-IP deployment *without* Tailscale): when clients
  connect directly to the published UDP port, ``conntrack -L`` shows their source addresses and we
  map them to identities. Preserved and tested so a move off Tailscale is a config flip, not a
  rewrite. Note the identity map still comes from ``tailscale status`` here; a Tailscale-less cloud
  would need a different IP->identity source (see the findings doc).

The parsing functions are pure and unit-tested; the privileged shell-outs (``tailscale``,
``systemctl``, ``conntrack``) are a thin layer. Runs as a root systemd service. It reads only flow
metadata / tailnet identities and unit states -- never game data or secrets. The ledger is
playtime metadata (who played when); keep it root-owned and private, like the audit log.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

TAILNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
CONNTRACK_SRC = re.compile(r"src=(\d{1,3}(?:\.\d{1,3}){3})")
DEFAULT_MIN_KBPS = 25.0


# --------------------------------------------------------------------------- shared

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


def _run(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def is_unit_active(instance_key: str, systemctl_bin: str) -> bool:
    return _run([systemctl_bin, "is-active", f"game-{instance_key}.service"]).strip() == "active"


# ------------------------------------------------------------------ tailscale source

def parse_status_peers(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collapse ``tailscale status --json`` peers into {login: {bytes, active}} (self excluded).

    Multiple devices under one login are merged: bytes summed, active OR-ed.
    """
    users = status.get("User") or {}
    peers: dict[str, dict[str, Any]] = {}
    for node in (status.get("Peer") or {}).values():
        if not isinstance(node, dict):
            continue
        profile = users.get(str(node.get("UserID"))) if isinstance(users, dict) else None
        login = profile.get("LoginName") if isinstance(profile, dict) else None
        if not login:
            continue
        entry = peers.setdefault(login, {"bytes": 0, "active": False})
        entry["bytes"] += int(node.get("RxBytes") or 0) + int(node.get("TxBytes") or 0)
        entry["active"] = entry["active"] or bool(node.get("Active"))
    return peers


def playing_logins(current: dict[str, dict[str, Any]], previous_bytes: dict[str, int], dt: float, min_kbps: float) -> list[str]:
    """Logins that are Active and whose traffic rate since the last sample exceeds ``min_kbps``.

    The rate is what separates a player (sustained game traffic) from someone merely viewing the
    dashboard or idling on the tailnet. Returns them ordered by rate, highest first. A login with
    no prior sample (first cycle after start, or a reconnect that reset counters) is skipped rather
    than guessed, so we never emit a false positive.
    """
    if dt <= 0:
        return []
    ranked: list[tuple[float, str]] = []
    for login, info in current.items():
        if not info.get("active"):
            continue
        prior = previous_bytes.get(login)
        if prior is None:
            continue
        delta = int(info["bytes"]) - int(prior)
        if delta < 0:
            continue
        kbps = (delta * 8) / 1000.0 / dt
        if kbps >= min_kbps:
            ranked.append((kbps, login))
    ranked.sort(reverse=True)
    return [login for _, login in ranked]


def run_cycle_tailscale(catalog: dict[str, Any], ledger_path: Path, tailscale_bin: str, systemctl_bin: str, state: dict[str, Any], min_kbps: float) -> None:
    status_raw = _run([tailscale_bin, "status", "--json"])
    try:
        peers = parse_status_peers(json.loads(status_raw)) if status_raw else {}
    except json.JSONDecodeError:
        peers = {}
    now_mono = time.monotonic()
    dt = (now_mono - state["t"]) if state.get("t") is not None else 0.0
    playing = playing_logins(peers, state.get("bytes", {}), dt, min_kbps)
    for key in instance_ports(catalog):
        present = playing if is_unit_active(key, systemctl_bin) else []
        append_ledger(ledger_path, {"ts": now(), "instance": key, "present": present})
    state["bytes"] = {login: int(info["bytes"]) for login, info in peers.items()}
    state["t"] = now_mono


# ------------------------------------------------------------------ conntrack source

def is_tailnet_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in TAILNET_CGNAT
    except ValueError:
        return False


def parse_conntrack_peers(conntrack_output: str, game_port: int) -> set[str]:
    """Extract distinct tailnet peer IPs from ``conntrack -L`` output for one game port.

    Only lines whose destination port is the game port count, and only the *original* direction
    source (the first ``src=``) -- the real client before any Docker NAT rewrites it.
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
        return profile.get("LoginName") if isinstance(profile, dict) else None

    nodes: list[dict[str, Any]] = []
    if isinstance(tailscale_status.get("Self"), dict):
        nodes.append(tailscale_status["Self"])
    if isinstance(tailscale_status.get("Peer"), dict):
        nodes.extend(node for node in tailscale_status["Peer"].values() if isinstance(node, dict))
    for node in nodes:
        login = login_for(node.get("UserID"))
        if not login:
            continue
        for address in node.get("TailscaleIPs") or []:
            if isinstance(address, str) and ":" not in address:
                mapping[address] = login
    return mapping


def resolve_present(peers: set[str], ip_login: dict[str, str]) -> list[str]:
    """Resolve peer IPs to sorted logins (unknown IPs fall back to the raw IP)."""
    return sorted({ip_login.get(ip, f"ip:{ip}") for ip in peers})


def run_cycle_conntrack(catalog: dict[str, Any], ledger_path: Path, tailscale_bin: str, conntrack_bin: str) -> None:
    status_raw = _run([tailscale_bin, "status", "--json"])
    try:
        ip_login = build_ip_login_map(json.loads(status_raw)) if status_raw else {}
    except json.JSONDecodeError:
        ip_login = {}
    for key, port in instance_ports(catalog).items():
        output = _run([conntrack_bin, "-L", "-p", "udp", "--dport", str(port)])
        present = resolve_present(parse_conntrack_peers(output, port), ip_login)
        append_ledger(ledger_path, {"ts": now(), "instance": key, "present": present})


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Sample player presence per game instance into a ledger.")
    parser.add_argument("--catalog", type=Path, default=Path("/etc/game-server-interface/catalog.yaml"))
    parser.add_argument("--ledger", type=Path, default=Path("/var/lib/game-server-interface/presence.jsonl"))
    parser.add_argument("--source", choices=["tailscale", "conntrack"], default="tailscale",
                        help="presence source (default tailscale; conntrack is for a future non-Tailscale deployment)")
    parser.add_argument("--min-kbps", type=float, default=DEFAULT_MIN_KBPS,
                        help="tailscale source: minimum per-peer traffic rate to count as playing")
    parser.add_argument("--interval", type=int, default=60, help="seconds between samples (0 = one cycle then exit)")
    parser.add_argument("--tailscale-bin", default="/usr/bin/tailscale")
    parser.add_argument("--systemctl-bin", default="/usr/bin/systemctl")
    parser.add_argument("--conntrack-bin", default="/usr/sbin/conntrack")
    args = parser.parse_args()

    try:
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict):
            raise ValueError("catalog must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"presence meter failed to load catalog: {exc}", file=sys.stderr)
        return 1

    state: dict[str, Any] = {"bytes": {}, "t": None}

    def cycle() -> None:
        if args.source == "conntrack":
            run_cycle_conntrack(catalog, args.ledger, args.tailscale_bin, args.conntrack_bin)
        else:
            run_cycle_tailscale(catalog, args.ledger, args.tailscale_bin, args.systemctl_bin, state, args.min_kbps)

    if args.interval <= 0:
        cycle()
        return 0
    while True:
        try:
            cycle()
        except Exception as exc:  # keep the daemon alive across transient failures
            print(f"presence meter cycle error: {exc}", file=sys.stderr, flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
