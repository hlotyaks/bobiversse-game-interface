#!/usr/bin/env python3
"""Corrective edits to the presence ledger (root-run).

The presence ledger is append-only in normal operation, but occasionally a bad capture needs
removing -- e.g. a non-player who was mis-attributed a game slot before an exclusion was configured.
This performs one narrow correction: strip a login from every sample's ``present`` list. Samples
that become empty are kept (as ``present: []``) so the sample count and timeline are preserved; only
the erroneous attribution is removed. The rewrite is atomic (temp file + rename) and preserves file
mode ``0600`` -- the ledger is private playtime metadata.

    sudo tools/ledger_admin.py --remove-login hlotyaks@github            # default ledger path
    sudo tools/ledger_admin.py --remove-login bob@ex --ledger /path --dry-run

A second correction retires a stretch of untrustworthy capture: ``--clear-month`` marks every
sample for one instance in one calendar month as *meter-blind* (``present: []``, ``count: null``).
It does not delete the lines. Blind is the honest record when attribution was broken -- we do not
know who played, which is different from knowing nobody did -- and billing already skips blind
samples and totals them separately, where deleting the lines would quietly assert an idle server.

    sudo tools/ledger_admin.py --clear-month 2026-08 --instance enshrouded-primary --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_LEDGER = Path("/var/lib/game-server-interface/presence.jsonl")


def remove_login(lines: list[str], login: str) -> tuple[list[str], int]:
    """Return (rewritten lines, number of samples changed) with ``login`` removed from ``present``."""
    out: list[str] = []
    changed = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        present = record.get("present")
        if isinstance(present, list) and login in present:
            record["present"] = [entry for entry in present if entry != login]
            changed += 1
        out.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return out, changed


def clear_month(lines: list[str], month: str, instance: str) -> tuple[list[str], int]:
    """Return (rewritten lines, samples changed) with one instance-month marked meter-blind.

    ``month`` is ``YYYY-MM``, matched against the ``ts`` prefix, so it is UTC calendar month --
    the same basis billing uses to scope a report.
    """
    out: list[str] = []
    changed = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        if (record.get("instance") == instance
                and isinstance(record.get("ts"), str)
                and record["ts"].startswith(f"{month}-")
                and not (record.get("present") == [] and record.get("count", 0) is None)):
            record["present"] = []
            record["count"] = None
            changed += 1
        out.append(json.dumps(record, separators=(",", ":"), sort_keys=True))
    return out, changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Corrective edits to the presence ledger.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--remove-login", metavar="LOGIN",
                        help="strip this login from every sample's present list")
    action.add_argument("--clear-month", metavar="YYYY-MM",
                        help="mark every sample for --instance in this UTC month as meter-blind "
                             "(present: [], count: null); requires --instance")
    parser.add_argument("--instance", metavar="ID", help="instance id, e.g. enshrouded-primary")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = parser.parse_args()

    if args.clear_month and not args.instance:
        parser.error("--clear-month requires --instance (never clear every game at once)")

    try:
        text = args.ledger.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"no ledger at {args.ledger}", file=sys.stderr)
        return 1

    if args.clear_month:
        rewritten, changed = clear_month(text.splitlines(), args.clear_month, args.instance)
        print(f"{args.instance} {args.clear_month}: {changed} sample(s) marked meter-blind "
              f"out of {len(rewritten)}")
    else:
        rewritten, changed = remove_login(text.splitlines(), args.remove_login)
        print(f"{args.remove_login}: {changed} sample(s) affected out of {len(rewritten)}")
    if args.dry_run:
        print("dry run -- no changes written")
        return 0
    if changed == 0:
        print("nothing to change")
        return 0

    payload = ("\n".join(rewritten) + "\n") if rewritten else ""
    directory = args.ledger.parent
    descriptor, temporary_name = tempfile.mkstemp(prefix="presence.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, args.ledger)
    except BaseException:
        os.unlink(temporary_name)
        raise
    print(f"rewrote {args.ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
