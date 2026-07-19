#!/usr/bin/env python3
"""Sample who is playing each game instance and append it to a presence ledger.

Game servers (Enshrouded included) do not log player identity, so the meter derives it from the
network layer. It supports two interchangeable presence sources; both append the same ledger
record ``{"ts", "instance", "present": [logins]}`` and differ only in how they observe players:

- ``tailscale`` (default, current bobiverse deployment): players reach the game over the tailnet,
  so their packets arrive *inside* the WireGuard tunnel and the kernel's conntrack never sees a
  ``client -> game-port`` flow (verified 2026-07-18 -- see docs/presence-source-conntrack-findings.md).
  How many clients are connected comes from the game itself when we can read it (Enshrouded logs a
  per-machine ``OperatingNormally`` block every ~30s); *who* they are comes from ``tailscale status
  --json``, attributing the reported client count to the busiest tailnet peers. Real per-client game
  traffic is far below any usable bandwidth threshold, so the game's own count -- not a kbps cutoff
  -- is what distinguishes a player from someone merely idling on the tailnet. Games without an
  occupancy reader fall back to the ``--min-kbps`` traffic-rate heuristic. Attribution is gated to
  instances whose systemd unit is active.

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
# When the game itself reports how many clients are connected, we assign identities to the
# top-N tailnet peers by traffic rate. This floor only drops peers with essentially no traffic,
# so a stale count can never invent a phantom player; it is not the player/idle discriminator
# the old min-kbps threshold tried (and failed) to be -- the game's count is that discriminator.
DEFAULT_ATTRIBUTION_FLOOR_KBPS = 1.0
# Weight on the newest 60s sample when smoothing per-peer traffic rate (EWMA). Lower = steadier;
# 0.5 keeps a real player ranked ahead of an idle peer's one-cycle burst or the player's own
# transient tailscale counter reset (both observed misattributing a solo slot on 2026-07-19).
DEFAULT_RATE_SMOOTHING = 0.5


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


def update_rate_ewma(previous_ewma: dict[str, float], current: dict[str, dict[str, Any]], previous_bytes: dict[str, int], dt: float, alpha: float) -> dict[str, float]:
    """Return each login's exponentially-smoothed traffic rate (kbps).

    Identity is assigned by *ranking* peers, so a single noisy 60s delta must not flip a slot to the
    wrong person. A tailscale re-handshake resets a peer's byte counters (negative delta), and an
    idle-but-``Active`` peer can burst for one cycle -- both were observed misattributing a solo
    player's slot to a bystander (hlotyaks). Smoothing over a few cycles fixes both: a reset/miss
    counts as 0 for that cycle (the login decays but is not dropped, so a steady player keeps its
    lead), and a lone burst barely moves an otherwise-idle peer. ``alpha`` is the weight on the
    newest sample (higher = less smoothing). Only currently-present logins are carried forward, so a
    peer that leaves the tailnet ages out.
    """
    ewma: dict[str, float] = {}
    for login, info in current.items():
        prior = previous_bytes.get(login)
        if dt <= 0 or prior is None or int(info["bytes"]) < int(prior):
            instant = 0.0  # unknown/reset this cycle -> decay, don't drop
        else:
            instant = ((int(info["bytes"]) - int(prior)) * 8) / 1000.0 / dt
        ewma[login] = alpha * instant + (1 - alpha) * previous_ewma.get(login, 0.0)
    return ewma


def rank_by_smoothed_rate(ewma: dict[str, float]) -> list[tuple[float, str]]:
    """Rank logins by smoothed traffic rate, highest first."""
    return sorted(((rate, login) for login, rate in ewma.items()), reverse=True)


def attribute_by_count(ranked: list[tuple[float, str]], count: int, floor_kbps: float) -> list[str]:
    """Assign identities to ``count`` connected clients: the top-``count`` peers by (smoothed) rate.

    Peers at or below ``floor_kbps`` are dropped so a stale/lagging count never attributes play to
    an idle peer -- we would rather under-report by one than bill the wrong person. Returned sorted.
    """
    if count <= 0:
        return []
    return sorted(login for rate, login in ranked[:count] if rate > floor_kbps)


def enshrouded_client_count(log_text: str) -> int | None:
    """Connected-client count from the most recent *complete* Enshrouded ``Machines:`` block.

    Enshrouded prints a ``Session``/``Machines:`` block every ~30s. Each connected client is a
    ``m#N(...) ... OperatingNormally`` line; the server's own entry reports ``EstablishingBaseline``
    (ping 0) and is not counted. Returns ``None`` if no complete block is present yet (unknown), or
    an int (0 = the game reports nobody connected). This is the game's own authoritative occupancy,
    which is why it replaces the fragile bandwidth threshold: real per-client game traffic is far
    below any sane kbps cutoff (see docs/presence-source-conntrack-findings.md).
    """
    result: int | None = None
    current: int | None = None
    for line in log_text.splitlines():
        if "Machines:" in line:
            current = 0
        elif current is not None and "m#" in line and "OperatingNormally" in line:
            current += 1
        elif current is not None and "-" * 20 in line:  # block closer (a long dash rule)
            result = current
            current = None
    return result


# template_id -> function(container log text) -> connected client count (or None if unknown).
OCCUPANCY_READERS = {"enshrouded": enshrouded_client_count}


def read_container_logs(container: str, docker_bin: str, since: str = "120s") -> str:
    """Return recent combined stdout+stderr for a container (empty string on any failure)."""
    try:
        result = subprocess.run([docker_bin, "logs", "--since", since, container],
                                capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout + result.stderr


def instance_client_count(template_id: str, container: str, docker_bin: str) -> int | None:
    """Game-authoritative connected-client count for an instance, or None if we can't tell."""
    reader = OCCUPANCY_READERS.get(template_id)
    if reader is None:
        return None
    logs = read_container_logs(container, docker_bin)
    return reader(logs) if logs else None


def load_exclusions(exclusions_path: Path) -> dict[str, frozenset[str]]:
    """Load the admin-managed per-game exclusion map: {template_id: frozenset(logins)}.

    A missing, unreadable, or malformed file yields no exclusions (fail open to *including* players --
    we would rather bill a mis-ranked admin than silently drop a real player). Read fresh every cycle
    so an admin's edit via the interface takes effect on the next sample, with no meter restart.
    """
    try:
        raw = json.loads(exclusions_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    mapping = raw.get("exclusions") if isinstance(raw, dict) else None
    if not isinstance(mapping, dict):
        return {}
    result: dict[str, frozenset[str]] = {}
    for template_id, logins in mapping.items():
        if isinstance(template_id, str) and isinstance(logins, list):
            result[template_id] = frozenset(login for login in logins if isinstance(login, str))
    return result


def instance_templates(catalog: dict[str, Any]) -> dict[str, str]:
    """Return {"<template>-<instance>": template_id} for every allowlisted slot."""
    mapping: dict[str, str] = {}
    templates = catalog.get("templates", {})
    if not isinstance(templates, dict):
        return mapping
    for template_id, template in templates.items():
        slots = template.get("instance_policy", {}).get("slots", {}) if isinstance(template, dict) else {}
        if not isinstance(slots, dict):
            continue
        for instance_id in slots:
            mapping[f"{template_id}-{instance_id}"] = template_id
    return mapping


def run_cycle_tailscale(catalog: dict[str, Any], ledger_path: Path, tailscale_bin: str, systemctl_bin: str, docker_bin: str, state: dict[str, Any], min_kbps: float, floor_kbps: float = DEFAULT_ATTRIBUTION_FLOOR_KBPS, alpha: float = DEFAULT_RATE_SMOOTHING, exclude_logins: frozenset[str] = frozenset(), template_exclusions: dict[str, frozenset[str]] | None = None) -> None:
    status_raw = _run([tailscale_bin, "status", "--json"])
    try:
        peers = parse_status_peers(json.loads(status_raw)) if status_raw else {}
    except json.JSONDecodeError:
        peers = {}
    # Global exclusions (--exclude-login): logins that are never a player of *any* game (e.g. a
    # monitoring bot). Drop them entirely before attribution so they can't be ranked into any slot.
    if exclude_logins:
        peers = {login: info for login, info in peers.items() if login not in exclude_logins}
    now_mono = time.monotonic()
    dt = (now_mono - state["t"]) if state.get("t") is not None else 0.0
    previous_bytes = state.get("bytes", {})
    ewma = update_rate_ewma(state.get("rate_ewma", {}), peers, previous_bytes, dt, alpha)
    ranked = rank_by_smoothed_rate(ewma)
    templates = instance_templates(catalog)
    template_exclusions = template_exclusions or {}
    for key in instance_ports(catalog):
        if not is_unit_active(key, systemctl_bin):
            present: list[str] = []
        else:
            template_id = templates.get(key, "")
            # Per-game exclusions: a login that is a non-player of *this* game (a server admin who
            # never plays Enshrouded but does play others). Applied per instance, not globally, so
            # the same login can still be attributed to a different game. Excluding before selection
            # means the slot passes to the next real player rather than being spent on a non-player.
            excluded = template_exclusions.get(template_id, frozenset())
            count = instance_client_count(template_id, f"game-{key}", docker_bin)
            if count is not None:
                # Game-authoritative occupancy: attribute the reported N clients to the busiest peers.
                ranked_for_instance = [pair for pair in ranked if pair[1] not in excluded]
                present = attribute_by_count(ranked_for_instance, count, floor_kbps)
            else:
                # No game-occupancy reader for this template -- fall back to the rate threshold.
                present = [login for login in playing_logins(peers, previous_bytes, dt, min_kbps) if login not in excluded]
        append_ledger(ledger_path, {"ts": now(), "instance": key, "present": present})
    state["bytes"] = {login: int(info["bytes"]) for login, info in peers.items()}
    state["rate_ewma"] = ewma
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
    parser.add_argument("--exclude-login", action="append", default=[], metavar="LOGIN",
                        help="tailscale login that is never a player of ANY game (e.g. a monitoring bot); "
                             "excluded from attribution globally. Repeatable. For a per-game non-player "
                             "(admin of one game), use the admin-managed --exclusions-file instead.")
    parser.add_argument("--exclusions-file", type=Path, default=Path("/var/lib/game-server-interface/presence-exclusions.json"),
                        help="per-game exclusion map ({template_id: [logins]}), admin-edited via the "
                             "interface and re-read every cycle; missing file means no per-game exclusions.")
    parser.add_argument("--tailscale-bin", default="/usr/bin/tailscale")
    parser.add_argument("--systemctl-bin", default="/usr/bin/systemctl")
    parser.add_argument("--docker-bin", default="/usr/bin/docker")
    parser.add_argument("--conntrack-bin", default="/usr/sbin/conntrack")
    args = parser.parse_args()

    try:
        catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
        if not isinstance(catalog, dict):
            raise ValueError("catalog must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"presence meter failed to load catalog: {exc}", file=sys.stderr)
        return 1

    state: dict[str, Any] = {"bytes": {}, "rate_ewma": {}, "t": None}

    exclude_logins = frozenset(args.exclude_login)

    def cycle() -> None:
        if args.source == "conntrack":
            run_cycle_conntrack(catalog, args.ledger, args.tailscale_bin, args.conntrack_bin)
        else:
            # Read fresh each cycle so admin edits via the interface apply without a restart.
            template_exclusions = load_exclusions(args.exclusions_file)
            run_cycle_tailscale(catalog, args.ledger, args.tailscale_bin, args.systemctl_bin, args.docker_bin, state, args.min_kbps, exclude_logins=exclude_logins, template_exclusions=template_exclusions)

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
