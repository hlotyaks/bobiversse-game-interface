#!/usr/bin/env python3
"""Read-only live observer for the presence meter's identity attribution.

Run as root DURING a real multiplayer session. Each cycle it prints, side by side:

- the game's own connected-client count (what the meter trusts for *how many*),
- every tailnet peer's Rx+Tx delta and the kbps / EWMA the meter derives from it,
- who the meter **would** attribute, via the production code path itself,
- the conntrack UDP flows to the game port -- the alternative identity source.

It exists because the meter's *who* half failed silently: on 2026-08-23 Enshrouded logged three
connected clients for ~2.3h while the ledger recorded nobody, and nothing in the journal said so.
The one thing that can settle why is a look at the peer byte counters with real players connected.
Comparing the last two lines of each cycle also answers the open question in
docs/presence-source-conntrack-findings.md: whether conntrack sees the game's UDP flows.

Reads only; it never touches the ledger or any game state. Ctrl-C to stop.

    sudo scripts/observe-presence.py                       # interactive, human-readable
    sudo scripts/observe-presence.py --instance enshrouded-primary --port 15636 --interval 20

With ``--log-jsonl`` it runs as a daemon instead (``game-presence-observer.service``), appending one
structured record per interesting cycle to a diagnostic log. "Interesting" means the game reports
anyone connected, the occupancy read failed, or the meter could not name every client -- plus a
periodic heartbeat so a silent log is distinguishable from a dead observer. Idle cycles are not
recorded, which is what keeps a permanent observer cheap.

The point is post-hoc diagnosis: each of this meter's three silent failures was only discoverable
while it was happening, and by the time anyone read the bill the evidence was gone. This log keeps
the evidence -- peer byte counters, the game's count, and the conntrack flows -- for whoever reads
it next week.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Prefer the checked-out meter so the observer matches the code under test; fall back to the
# installed copy when run from outside the repo.
for candidate in (Path(__file__).resolve().parents[1] / "tools", Path("/usr/local/libexec/game-server-interface")):
    if (candidate / "presence_meter.py").exists():
        sys.path.insert(0, str(candidate))
        break
import presence_meter as pm  # noqa: E402


DEFAULT_LOG = Path("/var/lib/game-server-interface/presence-observer.jsonl")


def append_jsonl(path: Path, record: dict) -> None:
    """Append one record at mode 0600 -- it holds playtime metadata, like the ledger itself."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def parse_peer_paths(status: dict) -> dict[str, dict]:
    """Per-login connection path and write-recency from ``tailscale status --json``.

    Collapses a login's devices the way the meter does, keeping the *most recently written* one.
    ``RxBytes``/``TxBytes`` are populated only for peers with a direct path -- a DERP-relayed peer
    reads 0 however hard it is playing -- which is why byte-rate ranking cannot see half the
    players. ``LastWrite`` has no such gap: it is when the local engine last sent this peer a
    packet, and it is *not* refreshed by mere presence (an online but idle peer sits hours stale).
    """
    now_dt = datetime.now(UTC)

    def age(value) -> float | None:
        if not isinstance(value, str) or value.startswith("0001"):
            return None
        try:
            return (now_dt - datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            return None

    users = status.get("User") or {}
    paths: dict[str, dict] = {}
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
            "last_write_s": age(node.get("LastWrite")),
            "last_handshake_s": age(node.get("LastHandshake")),
        }
        current = paths.get(login)
        if current is None:
            paths[login] = entry
            continue
        # Keep the device we most recently wrote to; None (never) always loses.
        old_age, new_age = current["last_write_s"], entry["last_write_s"]
        if old_age is None or (new_age is not None and new_age < old_age):
            paths[login] = entry
    return paths


def attribute_by_last_write(paths: dict[str, dict], count: int, max_age_s: float,
                            excluded: frozenset[str] = frozenset()) -> list[str]:
    """Candidate attribution: the ``count`` peers written to most recently.

    Shadow-run beside the production byte-rate path so the two can be compared on real sessions
    before anything is switched. A peer we have not written to within ``max_age_s`` is not
    exchanging traffic with this host and cannot be one of the game's connected clients.
    """
    if count <= 0:
        return []
    fresh = [(info["last_write_s"], login) for login, info in paths.items()
             if login not in excluded and info["last_write_s"] is not None and info["last_write_s"] <= max_age_s]
    fresh.sort()
    return sorted(login for _, login in fresh[:count])


def conntrack_flows(port: int, conntrack_bin: str) -> list[str]:
    try:
        result = subprocess.run([conntrack_bin, "-L", "-p", "udp", "--dport", str(port)],
                                capture_output=True, text=True, timeout=15, check=False)
        return [line for line in result.stdout.splitlines() if "src=" in line]
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"ERR {exc}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", default="enshrouded-primary")
    parser.add_argument("--template", default="enshrouded")
    parser.add_argument("--port", type=int, default=15636, help="game UDP port, for the conntrack cross-check")
    parser.add_argument("--interval", type=int, default=20)
    parser.add_argument("--tailscale-bin", default="/usr/bin/tailscale")
    parser.add_argument("--systemctl-bin", default="/usr/bin/systemctl")
    parser.add_argument("--docker-bin", default="/usr/bin/docker")
    parser.add_argument("--conntrack-bin", default="/usr/sbin/conntrack")
    parser.add_argument("--log-jsonl", type=Path, nargs="?", const=DEFAULT_LOG, default=None,
                        metavar="PATH",
                        help=f"append structured records to PATH (default {DEFAULT_LOG}) instead of "
                             "printing a table; this is the daemon mode used by the systemd unit")
    parser.add_argument("--heartbeat", type=int, default=20, metavar="N",
                        help="in --log-jsonl mode, record one idle cycle every N cycles so a quiet "
                             "log stays distinguishable from a stopped observer (default 20)")
    args = parser.parse_args()

    state: dict[str, object] = {"bytes": {}, "rate_ewma": {}, "t": None}
    logging = args.log_jsonl is not None
    if logging:
        print(f"observing {args.instance} every {args.interval}s -> {args.log_jsonl}", flush=True)
    else:
        print(f"observing {args.instance} every {args.interval}s -- Ctrl-C to stop\n", flush=True)

    cycle = 0
    while True:
        cycle += 1
        status_raw = pm._run([args.tailscale_bin, "status", "--json"])
        try:
            peers = pm.parse_status_peers(json.loads(status_raw)) if status_raw else {}
        except json.JSONDecodeError:
            peers = {}
        now_mono = time.monotonic()
        dt = (now_mono - state["t"]) if state["t"] is not None else 0.0
        previous = state["bytes"]
        ewma = pm.update_rate_ewma(state["rate_ewma"], peers, previous, dt, pm.DEFAULT_RATE_SMOOTHING)
        ranked = pm.rank_by_smoothed_rate(ewma)
        count = pm.instance_client_count(args.template, f"game-{args.instance}", args.docker_bin)
        active = pm.is_unit_active(args.instance, args.systemctl_bin)

        try:
            paths = parse_peer_paths(json.loads(status_raw)) if status_raw else {}
        except json.JSONDecodeError:
            paths = {}
        peer_rows = []
        for login, info in sorted(peers.items(), key=lambda kv: -ewma.get(kv[0], 0.0)):
            total = int(info["bytes"])
            prior = previous.get(login)
            delta = (total - prior) if prior is not None else None
            kbps = (delta * 8 / 1000.0 / dt) if (delta is not None and delta >= 0 and dt > 0) else None
            path = paths.get(login, {})
            peer_rows.append({"login": login, "bytes": total, "delta": delta,
                              "kbps": round(kbps, 2) if kbps is not None else None,
                              "ewma": round(ewma.get(login, 0.0), 2), "active": bool(info.get("active")),
                              "direct": path.get("direct"), "relay": path.get("relay"),
                              "online": path.get("online"),
                              "last_write_s": (round(path["last_write_s"]) if path.get("last_write_s") is not None else None),
                              "last_handshake_s": (round(path["last_handshake_s"]) if path.get("last_handshake_s") is not None else None)})

        would = [] if count is None else pm.attribute_by_count(ranked, count, pm.DEFAULT_ATTRIBUTION_FLOOR_KBPS)
        unnamed = (count - len(would)) if count is not None else None
        # Shadow candidate: rank by write-recency instead of byte rate. Recorded, never acted on --
        # the comparison across a real multi-player session is what decides whether to switch.
        by_write = [] if count is None else attribute_by_last_write(paths, count, max_age_s=args.interval * 2)

        flows = conntrack_flows(args.port, args.conntrack_bin)
        ip_login: dict[str, str] = {}
        try:
            ip_login = pm.build_ip_login_map(json.loads(status_raw)) if status_raw else {}
        except json.JSONDecodeError:
            pass
        sources = {match.group(1) for line in flows if (match := pm.CONNTRACK_SRC.search(line))}
        named = sorted(ip_login.get(source, source) for source in sources)

        if logging:
            # Record only cycles that carry information: someone connected, the occupancy read
            # failed, we could not name everyone, or a periodic heartbeat. Idle cycles are the
            # overwhelming majority and recording them would bury the ones that matter.
            reason = None
            if cycle == 1:
                # Always record the first cycle. Otherwise nothing is written until the first
                # heartbeat, leaving a window after every restart where a working observer and a
                # broken one look identical. This also captures the peer roster at startup.
                reason = "startup"
            elif count is None:
                reason = "occupancy_unknown"
            elif unnamed:
                reason = "unnamed_players"
            elif count > 0:
                reason = "players_connected"
            elif args.heartbeat > 0 and cycle % args.heartbeat == 0:
                reason = "heartbeat"
            if reason is not None:
                append_jsonl(args.log_jsonl, {
                    "ts": pm.now(), "instance": args.instance, "reason": reason,
                    "count": count, "attributed": would, "unnamed": unnamed,
                    "attributed_by_last_write": by_write,
                    "unit_active": active, "dt": round(dt, 1),
                    "peers": peer_rows,
                    "conntrack": {"flows": len(flows), "logins": named},
                })
        else:
            print("=" * 78)
            print(f"{time.strftime('%H:%M:%S')}  game_client_count={count}  unit_active={active}  dt={dt:.0f}s")
            print(f"  {'login':<38}{'bytes':>12}{'delta':>10}{'kbps':>9}{'ewma':>9}  {'path':<7}{'wrote':>9}")
            for row in peer_rows:
                delta = row["delta"] if row["delta"] is not None else "-"
                kbps = f"{row['kbps']:.2f}" if row["kbps"] is not None else "-"
                path = "direct" if row["direct"] else "DERP"
                wrote = f"{row['last_write_s']}s" if row["last_write_s"] is not None else "never"
                flag = "  ACTIVE" if row["active"] else ""
                print(f"  {row['login']:<38}{row['bytes']:>12}{delta:>10}{kbps:>9}{row['ewma']:>9.2f}"
                      f"  {path:<7}{wrote:>9}{flag}")
            if count is None:
                fallback = pm.playing_logins(peers, previous, dt, 25.0)
                print(f"  -> occupancy UNKNOWN (no complete log block); legacy fallback would say: {fallback}")
            else:
                short = f"   *** {unnamed} PLAYER(S) UNNAMED ***" if unnamed else ""
                print(f"  -> meter records: count={count} present={would}{short}")
                agree = "same" if by_write == would else "DIFFERS"
                print(f"  -> by last-write:  count={count} present={by_write}   ({agree})")
            print(f"  conntrack udp dport {args.port}: {len(flows)} flow(s) -> {named if named else 'none'}")
            for line in flows[:6]:
                print(f"      {line}")

        state["bytes"] = {login: int(info["bytes"]) for login, info in peers.items()}
        state["rate_ewma"] = ewma
        state["t"] = now_mono
        sys.stdout.flush()
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
