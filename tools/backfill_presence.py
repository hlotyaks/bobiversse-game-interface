#!/usr/bin/env python3
"""Rebuild presence-ledger samples for a past window from the game's own log.

The presence meter samples live, so any stretch where it was misconfigured, stopped, or -- as
through 2026-07..09 -- naming players from a signal that could not see them leaves a ledger that is
wrong in a way no recalculation can fix. The game log can fix it: Enshrouded records a Steam ID for
every peer that connects and a matching event when it leaves, so replaying that log reconstructs
exactly who was present at any instant inside the log's retention.

This tool replays those events, resamples them at the meter's cadence, and splices the result into
the ledger, replacing whatever the meter recorded for the same instance over the same window. It
produces the same record shape the live meter writes, so the two are indistinguishable downstream::

    {"ts", "instance", "present": [names], "count": N}

``count`` comes from the game's own ``Session`` block, exactly as the live meter takes it, rather
than from the number of names recovered. Keeping them independent preserves the property that
matters: when the replay cannot name everyone the game counted, the shortfall reaches the bill as
UNATTRIBUTED instead of quietly shrinking the group size.

    sudo tools/backfill_presence.py --dry-run                       # what would change
    sudo tools/backfill_presence.py --since 2026-09-10T00:00:00Z    # write it

Only rewrites samples inside the reconstructed window; everything outside, and every other
instance, is preserved byte for byte.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import presence_meter as pm  # noqa: E402

DEFAULT_LEDGER = Path("/var/lib/game-server-interface/presence.jsonl")
DEFAULT_IDENTITIES = Path("/var/lib/game-server-interface/player-identities.json")
DEFAULT_EXCLUSIONS = Path("/var/lib/game-server-interface/presence-exclusions.json")
# docker logs -t prefixes each line with an RFC3339 timestamp.
STAMPED = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s(.*)$")


def parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_stamped_log(container: str, docker_bin: str) -> list[tuple[datetime, str]]:
    """Every retained log line as (timestamp, text). Empty if the container or log is gone."""
    try:
        result = subprocess.run([docker_bin, "logs", "-t", container],
                                capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    lines: list[tuple[datetime, str]] = []
    for line in (result.stdout + result.stderr).splitlines():
        matched = STAMPED.match(line)
        if not matched:
            continue
        try:
            lines.append((parse_stamp(matched.group(1)), matched.group(2)))
        except ValueError:
            continue
    lines.sort(key=lambda item: item[0])
    return lines


def presence_timeline(lines: list[tuple[datetime, str]]) -> list[tuple[datetime, frozenset[str]]]:
    """Replay peer add/drop events into [(when, set of connected player IDs)] transitions."""
    live: dict[str, str] = {}
    timeline: list[tuple[datetime, frozenset[str]]] = []
    for when, text in lines:
        added = pm.ENSHROUDED_PEER_ADD.search(text)
        dropped = pm.ENSHROUDED_PEER_DROP.search(text)
        if added:
            live[added.group(1)] = added.group(2)
        elif dropped:
            if dropped.group(1) not in live:
                continue
            live.pop(dropped.group(1), None)
        else:
            continue
        timeline.append((when, frozenset(live.values())))
    return timeline


def occupancy_timeline(lines: list[tuple[datetime, str]]) -> list[tuple[datetime, int]]:
    """Replay the game's Session blocks into [(when, connected client count)].

    Same reading the live meter takes, so a backfilled sample's ``count`` means what a live one's
    does: the game's own occupancy, independent of how many players the replay could name.
    """
    counts: list[tuple[datetime, int]] = []
    current: int | None = None
    started: datetime | None = None
    for when, text in lines:
        if "Machines:" in text:
            current, started = 0, when
        elif current is not None and "m#" in text and "OperatingNormally" in text:
            current += 1
        elif current is not None and "-" * 20 in text:
            counts.append((started or when, current))
            current = None
    return counts


def value_at(timeline: list[tuple[datetime, Any]], when: datetime, default: Any) -> Any:
    """Most recent value at or before ``when`` (timelines are ascending)."""
    found = default
    for stamp, value in timeline:
        if stamp > when:
            break
        found = value
    return found


def build_samples(lines: list[tuple[datetime, str]], instance: str, identities: dict[str, dict[str, str]],
                  excluded: frozenset[str], interval: int,
                  since: datetime | None, until: datetime | None) -> list[dict[str, Any]]:
    """Resample the replayed timelines into ledger records at the meter's cadence."""
    if not lines:
        return []
    presence = presence_timeline(lines)
    occupancy = occupancy_timeline(lines)
    start = since or lines[0][0]
    end = until or lines[-1][0]
    if start > end:
        return []
    samples: list[dict[str, Any]] = []
    step = timedelta(seconds=interval)
    when = start
    while when <= end:
        connected = value_at(presence, when, frozenset())
        names = sorted({
            identities[pid]["name"] for pid in connected
            if pid in identities
            and identities[pid]["name"] not in excluded
            and (not identities[pid]["login"] or identities[pid]["login"] not in excluded)
        })
        samples.append({
            "ts": when.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "instance": instance,
            "present": names,
            "count": value_at(occupancy, when, 0),
        })
        when += step
    return samples


def splice(existing: list[str], samples: list[dict[str, Any]], instance: str,
           start: datetime, end: datetime) -> tuple[list[str], int]:
    """Replace this instance's rows inside [start, end] with ``samples``; keep everything else."""
    kept: list[str] = []
    replaced = 0
    for line in existing:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)
            continue
        if record.get("instance") == instance and isinstance(record.get("ts"), str):
            try:
                stamp = parse_stamp(record["ts"])
            except ValueError:
                kept.append(stripped)
                continue
            if start <= stamp <= end:
                replaced += 1
                continue
        kept.append(stripped)
    rebuilt = kept + [json.dumps(s, separators=(",", ":"), sort_keys=True) for s in samples]

    def order(line: str) -> str:
        # A line we could not parse is kept rather than dropped -- never discard ledger content we
        # do not understand -- so the sort key has to tolerate it too.
        try:
            value = json.loads(line).get("ts")
        except (json.JSONDecodeError, AttributeError):
            return ""
        return value if isinstance(value, str) else ""

    rebuilt.sort(key=order)
    return rebuilt, replaced


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", default="enshrouded-primary")
    parser.add_argument("--template", default="enshrouded")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--identities-file", type=Path, default=DEFAULT_IDENTITIES)
    parser.add_argument("--exclusions-file", type=Path, default=DEFAULT_EXCLUSIONS)
    parser.add_argument("--docker-bin", default="/usr/bin/docker")
    parser.add_argument("--interval", type=int, default=60, help="sample cadence; match the meter's")
    parser.add_argument("--since", help="ISO-8601 start (default: first retained log line)")
    parser.add_argument("--until", help="ISO-8601 end (default: last retained log line)")
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args()

    if not pm.has_identity_reader(args.template):
        print(f"no identity reader for template '{args.template}'", file=sys.stderr)
        return 1

    lines = read_stamped_log(f"game-{args.instance}", args.docker_bin)
    if not lines:
        print("no retained game log to replay", file=sys.stderr)
        return 1

    try:
        since = parse_stamp(args.since) if args.since else None
        until = parse_stamp(args.until) if args.until else None
    except ValueError as exc:
        print(f"invalid timestamp: {exc}", file=sys.stderr)
        return 1

    identities = pm.load_player_identities(args.identities_file)
    excluded = pm.load_exclusions(args.exclusions_file).get(args.template, frozenset())
    samples = build_samples(lines, args.instance, identities, excluded, args.interval, since, until)
    if not samples:
        print("nothing to rebuild in that window")
        return 0

    start, end = parse_stamp(samples[0]["ts"]), parse_stamp(samples[-1]["ts"])
    named = sorted({name for s in samples for name in s["present"]})
    with_players = sum(1 for s in samples if s["count"] > 0)
    unnamed = sum(1 for s in samples if s["count"] > len(s["present"]))
    print(f"rebuilt {len(samples)} sample(s) for {args.instance}")
    print(f"  window   : {samples[0]['ts']} .. {samples[-1]['ts']}")
    print(f"  occupied : {with_players} sample(s) with someone connected "
          f"({with_players * args.interval / 3600:.2f}h)")
    print(f"  players  : {', '.join(named) if named else '(none named)'}")
    if unnamed:
        print(f"  WARNING  : {unnamed} sample(s) count more clients than the replay could name "
              f"(they will bill as UNATTRIBUTED)")

    try:
        existing = args.ledger.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing = []
    rebuilt, replaced = splice(existing, samples, args.instance, start, end)
    print(f"  replacing {replaced} existing sample(s) in that window; "
          f"ledger goes from {len(existing)} to {len(rebuilt)} line(s)")

    if args.dry_run:
        print("dry run -- no changes written")
        return 0

    payload = ("\n".join(rebuilt) + "\n") if rebuilt else ""
    descriptor, temporary = tempfile.mkstemp(prefix="presence.", dir=args.ledger.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, args.ledger)
    except BaseException:
        os.unlink(temporary)
        raise
    print(f"rewrote {args.ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
