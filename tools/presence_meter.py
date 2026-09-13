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
  --json``, attributing that count to the peers the engine wrote to most recently (``LastWrite``).
  Write-recency replaced byte-rate ranking on 2026-08-27: ``RxBytes``/``TxBytes`` are populated only
  for peers with a direct path, so every DERP-relayed player read 0 and was invisible to
  attribution. Games without an occupancy reader still fall back to the ``--min-kbps`` traffic-rate
  heuristic. Attribution is gated to instances whose systemd unit is active.

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
# Identity signal.
#   "game-log"   -- the game names its own connected players (Enshrouded logs a Steam ID per peer).
#                   Exact, and the only source that works at all here: players connect over Steam's
#                   relay network, so the tailnet never carries their game traffic.
#   "last-write" / "byte-rate" -- superseded tailnet heuristics, kept so the old behaviour stays
#                   reachable and testable. Both are blind to DERP-relayed peers, and neither ever
#                   had a chance against Steam-relayed play. See docs/usage-metering.md.
DEFAULT_ATTRIBUTION = "game-log"
# A peer not written to within this many seconds is not exchanging traffic with the host and cannot
# be one of the game's connected clients. Two meter cycles, so a single jittery sample cannot drop
# a player who is plainly still connected.
DEFAULT_MAX_WRITE_AGE_S = 120.0
# How far back to replay the game log when reconstructing who is connected. Must comfortably exceed
# the longest plausible single session: a player whose "Added peer" fell outside the window is not
# named (and shows up as UNATTRIBUTED), never misattributed.
DEFAULT_IDENTITY_WINDOW = "24h"


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


def parse_peer_paths(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-login connection path and write-recency from ``tailscale status --json``.

    Devices under one login collapse to the *most recently written* one, matching how
    ``parse_status_peers`` merges them.

    ``LastWrite`` is when the local engine last sent this peer a packet. It is the identity signal
    because it is populated for every peer, where ``RxBytes``/``TxBytes`` are populated **only for
    peers with an established direct path** -- a DERP-relayed peer reads 0 however much game traffic
    it is exchanging. Ranking by byte rate therefore could not see a relayed player at all, which is
    why 2026-08-23 logged three connected clients for 2.3h against a ledger that named nobody. It is
    also not refreshed by mere presence: an online but idle peer measured 35 hours stale while a
    player read 0s, so recency genuinely separates playing from lurking.
    """
    now_dt = datetime.now(UTC)

    def age_seconds(value: Any) -> float | None:
        # Tailscale writes the zero time as "0001-01-01T00:00:00Z" for "never".
        if not isinstance(value, str) or value.startswith("0001"):
            return None
        try:
            return (now_dt - datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            return None

    users = status.get("User") or {}
    paths: dict[str, dict[str, Any]] = {}
    for node in (status.get("Peer") or {}).values():
        if not isinstance(node, dict):
            continue
        profile = users.get(str(node.get("UserID"))) if isinstance(users, dict) else None
        login = profile.get("LoginName") if isinstance(profile, dict) else None
        if not login:
            continue
        entry = {
            "direct": bool(node.get("CurAddr")),
            "relay": node.get("Relay") or None,
            "online": bool(node.get("Online")),
            "last_write_s": age_seconds(node.get("LastWrite")),
            "last_handshake_s": age_seconds(node.get("LastHandshake")),
        }
        current = paths.get(login)
        if current is None:
            paths[login] = entry
            continue
        previous_age, new_age = current["last_write_s"], entry["last_write_s"]
        if previous_age is None or (new_age is not None and new_age < previous_age):
            paths[login] = entry
    return paths


def attribute_by_write_recency(paths: dict[str, dict[str, Any]], count: int, max_age_s: float,
                               excluded: frozenset[str] = frozenset()) -> list[str]:
    """Assign the game's ``count`` connected clients to the peers written to most recently.

    A peer the engine has not written to within ``max_age_s`` is not exchanging traffic with this
    host and cannot be one of the connected clients, so it is never selected -- we would rather
    under-report than bill the wrong person. Ranking is purely by recency: byte rate is deliberately
    *not* used as a tiebreak, because a DERP-relayed player's byte counters read 0 and would lose
    every tie to a direct-path peer with an idle SSH session.

    Where this is still ambiguous -- an administrator whose dashboard or SSH traffic is written just
    as recently as a player's game traffic -- the per-game exclusion list is the remedy, applied by
    the caller before selection.
    """
    if count <= 0:
        return []
    fresh = [(info["last_write_s"], login) for login, info in paths.items()
             if login not in excluded
             and info.get("last_write_s") is not None
             and info["last_write_s"] <= max_age_s]
    fresh.sort()
    return sorted(login for _, login in fresh[:count])


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



# Enshrouded's online subsystem logs an identity for every client, which is the identity source:
#   [online] Added peer 0(23) (steamid:76561190000000005)
#   [online] Removed peer 0(23)          / [online] Timeout for peer 0(23)
# Peer handles are unique per session (verified over a full container log: no handle is reused), so
# replaying add/drop events yields exactly who is connected now.
ENSHROUDED_PEER_ADD = re.compile(r"\[online\] Added peer (\S+) \(steamid:(\d+)\)")
ENSHROUDED_PEER_DROP = re.compile(r"\[online\] (?:Removed peer|Timeout for peer) (\S+)")


def enshrouded_connected_players(log_text: str) -> list[str]:
    """Steam IDs currently connected, by replaying the game's own peer add/drop events.

    This is the identity source the earlier network heuristics were a substitute for. The premise
    they were built on -- "Enshrouded does not log player identity" -- was simply wrong: it logs a
    Steam ID on every connect. Players reach the server over Steam's relay network, not the tailnet
    (the published UDP port receives no packets at all), so no network-layer source could ever have
    seen them; see docs/presence-source-conntrack-findings.md.

    Replay is over whatever window the caller read, so a session that began before that window looks
    absent. That under-reports rather than misattributes, and the occupancy count -- read separately
    from the game's own Session block -- still reflects them, so the shortfall surfaces on the bill
    as UNATTRIBUTED instead of hiding.
    """
    live: dict[str, str] = {}
    for line in log_text.splitlines():
        added = ENSHROUDED_PEER_ADD.search(line)
        if added:
            live[added.group(1)] = added.group(2)
            continue
        dropped = ENSHROUDED_PEER_DROP.search(line)
        if dropped:
            live.pop(dropped.group(1), None)
    return sorted(set(live.values()))


# --- valheim -------------------------------------------------------------------------------
# Valheim names a player only as they arrive, never as they leave, so identity has to be carried
# across three lines rather than read from one:
#
#   PlayFab socket with remote ID playfab/4DD2… received local Platform ID Steam_7656…  <- who
#   Got character ZDOID from Rhad : 643715480:1                                          <- handle
#   Player joined server "W" that has join code 079236, now 1 player(s)                  <- count
#
# and on the way out, the same handle reappears while the Steam ID does not:
#
#   Destroying abandoned non persistent zdo 643715480:4 owner 643715480                  <- handle
#   Player connection lost server "W" that has join code 079236, now 0 player(s)          <- count
#
# The ZDO owner id is therefore the only thing linking a departure to an arrival.
VALHEIM_PLAYER_COUNT = re.compile(r", now (\d+) player\(s\)")
VALHEIM_PLATFORM_ID = re.compile(r"received local Platform ID Steam_(\d+)")
VALHEIM_CHARACTER = re.compile(r"Got character ZDOID from (.+?) : (\d+):\d+")
VALHEIM_ZDO_DESTROY = re.compile(r"Destroying abandoned [^:]*zdo \d+:\d+ owner (\d+)")


def valheim_client_count(log_text: str) -> int | None:
    """Connected-player count from Valheim's own running total, or None if no line carries one.

    Every join and departure restates the total ("now N player(s)"), and so does the session
    registration at startup, so the most recent one is the server's current view. Read separately
    from identity on purpose: when the two disagree the shortfall reaches the bill as UNATTRIBUTED
    rather than quietly shrinking the group.
    """
    found = None
    for line in log_text.splitlines():
        match = VALHEIM_PLAYER_COUNT.search(line)
        if match:
            found = int(match.group(1))
    return found


def valheim_connected_players(log_text: str, characters: dict[str, str] | None = None) -> list[str]:
    """Steam IDs currently connected, by replaying Valheim's arrival and departure lines.

    A departure names only the ZDO owner id, so an arrival's Steam ID has to be bound to the owner
    id of the character that follows it. That window is wide -- ~20s measured, since it spans the
    client loading the world -- so two people starting a session together will routinely have their
    arrivals interleave, which is the *normal* case for a group rather than an edge case.

    ``characters`` resolves that outright: a ``{character name: game id}`` map, from the recorded
    identities, names the player straight off the character line however simultaneously they
    joined. Recognising one player also disambiguates the rest, since their arrival is struck from
    the pending list and may leave exactly one candidate for the next character.

    Only where a character is unrecognised *and* more than one arrival is outstanding does the
    reader decline to name anyone, rather than pairing by arrival order and risking a
    transposition. The game's own count still reports them, so they surface as UNATTRIBUTED.

    "now 0 player(s)" clears the set outright, which makes the reader self-correcting: drift from
    an ambiguous cycle cannot outlive a session.
    """
    characters = characters or {}
    by_owner: dict[str, str] = {}      # zdo owner id -> game id
    pending: list[str] = []            # game ids that have arrived but have no character yet
    for line in log_text.splitlines():
        platform = VALHEIM_PLATFORM_ID.search(line)
        if platform:
            pending.append(platform.group(1))
            continue
        character = VALHEIM_CHARACTER.search(line)
        if character:
            name, owner = character.group(1), character.group(2)
            known = characters.get(name)
            if known is not None:
                by_owner[owner] = known
                if known in pending:
                    pending.remove(known)   # narrows the field for the next character
            elif len(pending) == 1:
                by_owner[owner] = pending.pop()
            else:
                pending.clear()  # ambiguous and unrecognised: decline rather than guess
            continue
        destroyed = VALHEIM_ZDO_DESTROY.search(line)
        if destroyed:
            by_owner.pop(destroyed.group(1), None)
            continue
        count = VALHEIM_PLAYER_COUNT.search(line)
        if count and int(count.group(1)) == 0:
            by_owner.clear()
            pending.clear()
    return sorted(set(by_owner.values()))


# template_id -> function(container log text) -> connected client count (or None if unknown).
OCCUPANCY_READERS = {"enshrouded": enshrouded_client_count,
                     "valheim": valheim_client_count}

# template_id -> function(container log text) -> list of in-game player IDs currently connected.
IDENTITY_READERS = {"enshrouded": enshrouded_connected_players,
                    "valheim": valheim_connected_players}


def has_identity_reader(template_id: str) -> bool:
    """Whether this game names its own connected players in its log."""
    return template_id in IDENTITY_READERS


def instance_connected_players(template_id: str, container: str, docker_bin: str, window: str,
                               characters: dict[str, str] | None = None) -> list[str] | None:
    """In-game player IDs currently connected, or None if the game log could not be read."""
    reader = IDENTITY_READERS.get(template_id)
    if reader is None:
        return None
    logs = read_container_logs(container, docker_bin, since=window)
    if not logs:
        return None
    try:
        return reader(logs, characters or {})
    except TypeError:
        # Readers for games that name players outright take the log alone.
        return reader(logs)


def load_player_identities(path: Path) -> dict[str, dict[str, str]]:
    """Load the admin-managed player map: ``{"identities": {game_id: {"name", "login"}}}``.

    ``name`` is the billing identity -- the in-game name the group knows each other by, which is
    what appears on the bill. ``login`` is that person's tailnet login, carried only so the
    dashboard can tell which line belongs to the viewer (it identifies people by the
    ``Tailscale-User-Login`` header); it is optional, and a player without one is billed normally
    but cannot be shown their own line.

    A value may also be a bare string, taken as the name with no login. A missing or malformed file
    yields no mapping, which leaves every player unattributed rather than guessed -- the count still
    comes from the game, so the bill reports the gap instead of charging the wrong person. Read
    fresh each cycle so an admin's edit applies on the next sample with no restart.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    mapping = raw.get("identities") if isinstance(raw, dict) else None
    if not isinstance(mapping, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for game_id, value in mapping.items():
        raw_characters = None
        if isinstance(value, str):
            name, login = value, ""
        elif isinstance(value, dict):
            name = value.get("name") if isinstance(value.get("name"), str) else ""
            login = value.get("login") if isinstance(value.get("login"), str) else ""
            raw_characters = value.get("characters")
        else:
            continue
        if not name:
            continue
        entry: dict[str, Any] = {"name": name, "login": login, "characters": {}}
        # {template: [in-game character names]} -- how a game's own log refers to this player.
        if isinstance(value, dict) and isinstance(raw_characters, dict):
            for template, names in raw_characters.items():
                if isinstance(template, str) and isinstance(names, list):
                    entry["characters"][template] = [n for n in names if isinstance(n, str) and n]
        result[str(game_id)] = entry
    return result


def character_index(identities: dict[str, dict[str, Any]], template_id: str) -> dict[str, str]:
    """Return ``{character name: game id}`` for one game, from the recorded identities.

    Lets a game that names players only by their character -- Valheim on departure, and on arrival
    whenever two people join at once -- resolve them without guessing from ordering.
    """
    index: dict[str, str] = {}
    for game_id, entry in identities.items():
        for name in (entry.get("characters") or {}).get(template_id, []):
            index[name] = game_id
    return index


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
    """Game-authoritative connected-client count for an instance, or None if we can't tell.

    ``None`` here means *unknown*, and callers must not confuse it with *nobody*. Note the two
    distinct reasons it can be None: the template has no occupancy reader at all (see
    ``has_occupancy_reader``), or it has one but the read failed / the logs held no complete
    block. Only the first justifies falling back to the bandwidth heuristic.
    """
    reader = OCCUPANCY_READERS.get(template_id)
    if reader is None:
        return None
    logs = read_container_logs(container, docker_bin)
    return reader(logs) if logs else None


def has_occupancy_reader(template_id: str) -> bool:
    """Whether this game reports its own connected-client count.

    Gates the ``--min-kbps`` fallback. For a game that *does* report occupancy, a failed read is
    an unknown, not an invitation to guess by bandwidth: the fallback ranks whoever is pushing the
    most tailnet traffic, which on this host is an admin's SSH or dashboard session, and it was
    silently billing them for solo play the game never saw (observed 2026-08, see
    docs/usage-metering.md).
    """
    return template_id in OCCUPANCY_READERS


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


def run_cycle_tailscale(catalog: dict[str, Any], ledger_path: Path, tailscale_bin: str, systemctl_bin: str, docker_bin: str, state: dict[str, Any], min_kbps: float, floor_kbps: float = DEFAULT_ATTRIBUTION_FLOOR_KBPS, alpha: float = DEFAULT_RATE_SMOOTHING, exclude_logins: frozenset[str] = frozenset(), template_exclusions: dict[str, frozenset[str]] | None = None, attribution: str = DEFAULT_ATTRIBUTION, max_write_age_s: float = DEFAULT_MAX_WRITE_AGE_S, identities: dict[str, str] | None = None, identity_window: str = DEFAULT_IDENTITY_WINDOW) -> None:
    status_raw = _run([tailscale_bin, "status", "--json"])
    try:
        status = json.loads(status_raw) if status_raw else {}
    except json.JSONDecodeError:
        status = {}
    if not isinstance(status, dict):
        status = {}
    peers = parse_status_peers(status)
    paths = parse_peer_paths(status)
    # Global exclusions (--exclude-login): logins that are never a player of *any* game (e.g. a
    # monitoring bot). Drop them entirely before attribution so they can't be ranked into any slot.
    if exclude_logins:
        peers = {login: info for login, info in peers.items() if login not in exclude_logins}
        paths = {login: info for login, info in paths.items() if login not in exclude_logins}
    now_mono = time.monotonic()
    dt = (now_mono - state["t"]) if state.get("t") is not None else 0.0
    previous_bytes = state.get("bytes", {})
    ewma = update_rate_ewma(state.get("rate_ewma", {}), peers, previous_bytes, dt, alpha)
    ranked = rank_by_smoothed_rate(ewma)
    templates = instance_templates(catalog)
    template_exclusions = template_exclusions or {}
    identities = identities or {}
    for key in instance_ports(catalog):
        present: list[str] = []
        count: int | None = 0
        if is_unit_active(key, systemctl_bin):
            template_id = templates.get(key, "")
            # Per-game exclusions: a login that is a non-player of *this* game (a server admin who
            # never plays Enshrouded but does play others). Applied per instance, not globally, so
            # the same login can still be attributed to a different game. Excluding before selection
            # means the slot passes to the next real player rather than being spent on a non-player.
            excluded = template_exclusions.get(template_id, frozenset())
            if has_occupancy_reader(template_id):
                # Game-authoritative occupancy: how many clients the game itself reports. A failed
                # read is recorded as an explicit unknown (count None) so the billing pass can skip
                # the sample instead of reading an empty list as "nobody was playing".
                count = instance_client_count(template_id, f"game-{key}", docker_bin)
                if count is not None:
                    if attribution == "game-log" and has_identity_reader(template_id):
                        # The game names its own players. Translate its in-game IDs to tailnet
                        # logins; an unmapped ID stays unnamed, so the shortfall against the game's
                        # count reaches the bill as UNATTRIBUTED rather than being guessed at.
                        connected = instance_connected_players(
                            template_id, f"game-{key}", docker_bin, identity_window,
                            character_index(identities, template_id))
                        # Match exclusions against the login as well as the in-game name: the
                        # dashboard's Exclusions page validates entries as tailnet logins (they must
                        # contain an "@"), so a name-only comparison would never match anything an
                        # administrator can actually enter there.
                        present = sorted({
                            identities[pid]["name"] for pid in (connected or [])
                            if pid in identities
                            and identities[pid]["name"] not in excluded
                            and (not identities[pid]["login"] or identities[pid]["login"] not in excluded)
                        })
                    elif attribution == "byte-rate":
                        ranked_for_instance = [pair for pair in ranked if pair[1] not in excluded]
                        present = attribute_by_count(ranked_for_instance, count, floor_kbps)
                    else:
                        present = attribute_by_write_recency(paths, count, max_write_age_s, excluded)
            else:
                # No game-occupancy reader for this template -- fall back to the rate threshold.
                present = [login for login in playing_logins(peers, previous_bytes, dt, min_kbps) if login not in excluded]
                count = len(present)
        # ``count`` is the game's own player count; ``present`` is only who we could put a name to.
        # Recording both keeps the two separable downstream: billing must charge the group rate for
        # a group of N even when it could only identify one of them, and must never apply the solo
        # premium to a sample the game says had three people in it.
        append_ledger(ledger_path, {"ts": now(), "instance": key, "present": present, "count": count})
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
                        help="tailscale source: minimum per-peer traffic rate to count as playing "
                             "(only the fallback for games with no occupancy reader)")
    parser.add_argument("--attribution", choices=["game-log", "last-write", "byte-rate"], default=DEFAULT_ATTRIBUTION,
                        help="which signal names the game's connected clients. 'game-log' (default) "
                             "reads the identities the game itself logs and is exact; 'last-write' "
                             "and 'byte-rate' are superseded tailnet heuristics that cannot see "
                             "players at all when the game relays through Steam.")
    parser.add_argument("--max-write-age", type=float, default=DEFAULT_MAX_WRITE_AGE_S, metavar="SECONDS",
                        help="last-write attribution: a peer not written to within this many seconds "
                             "is never selected (default two meter cycles)")
    parser.add_argument("--interval", type=int, default=60, help="seconds between samples (0 = one cycle then exit)")
    parser.add_argument("--exclude-login", action="append", default=[], metavar="LOGIN",
                        help="tailscale login that is never a player of ANY game (e.g. a monitoring bot); "
                             "excluded from attribution globally. Repeatable. For a per-game non-player "
                             "(admin of one game), use the admin-managed --exclusions-file instead.")
    parser.add_argument("--identities-file", type=Path, default=Path("/var/lib/game-server-interface/player-identities.json"),
                        help="game-log attribution: map of in-game player ID (Steam ID) to the "
                             "player's in-game name (the billing identity) and optional tailnet "
                             "login, re-read every cycle. An unmapped player is counted but not "
                             "named, so their share is reported as unattributed rather than guessed.")
    parser.add_argument("--identity-window", default=DEFAULT_IDENTITY_WINDOW, metavar="DURATION",
                        help="how far back to replay the game log to reconstruct who is connected "
                             "(default 24h); must exceed the longest plausible session")
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
            identities = load_player_identities(args.identities_file)
            run_cycle_tailscale(catalog, args.ledger, args.tailscale_bin, args.systemctl_bin, args.docker_bin, state, args.min_kbps, exclude_logins=exclude_logins, template_exclusions=template_exclusions, attribution=args.attribution, max_write_age_s=args.max_write_age, identities=identities, identity_window=args.identity_window)

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
