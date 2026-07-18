#!/usr/bin/env python3
"""Create and verify root-owned backups for provisioned game instances.

The scheduler deliberately invokes the existing, administrator-owned backup
commands. It does not read game data or secrets itself. Status is atomically
recorded for the restricted controller to expose read-only to the UI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_STATE = Path("/var/lib/game-server-interface/instances.json")
DEFAULT_STATUS = Path("/var/lib/game-server-interface/backup_status.json")
DEFAULT_CATALOG = Path("/etc/game-server-interface/catalog.yaml")
DEFAULT_BACKUP = "/usr/local/sbin/backup-game-data"
DEFAULT_VERIFY = "/usr/local/sbin/verify-game-backup"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix="backup-status.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def registered_instances(state_path: Path, catalog_path: Path) -> list[dict[str, Any]]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("instances"), dict):
        raise ValueError("instance state is invalid")
    if not isinstance(catalog, dict):
        raise ValueError("catalog is invalid")
    selected = []
    for instance in state["instances"].values():
        if not isinstance(instance, dict):
            continue
        template = catalog.get("templates", {}).get(instance.get("template_id"), {})
        policy = template.get("update_policy", {}) if isinstance(template, dict) else {}
        unit = instance.get("unit")
        if (
            policy.get("require_verified_backup") is True
            and isinstance(unit, str)
            and (Path("/etc/systemd/system") / unit).is_file()
        ):
            selected.append(instance)
    return selected


def run(command: str, instance_name: str) -> tuple[bool, str]:
    result = subprocess.run([command, instance_name], capture_output=True, text=True, timeout=7200, check=False)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail[-1000:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and verify scheduled game backups.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--backup-command", default=DEFAULT_BACKUP)
    parser.add_argument("--verify-command", default=DEFAULT_VERIFY)
    args = parser.parse_args()
    payload: dict[str, Any] = {"schema_version": 1, "generated_at": now(), "instances": {}}
    failed = False
    try:
        instances = registered_instances(args.state, args.catalog)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(json.dumps({"event": "backup_scheduler_failed", "error": str(exc)}), file=sys.stderr)
        return 1
    for instance in instances:
        key = instance["key"]
        name = f"{instance['template_id']}-{instance['instance_id']}"
        created, detail = run(args.backup_command, name)
        verified = False
        if created:
            verified, detail = run(args.verify_command, name)
        payload["instances"][key] = {
            "latest_timestamp": now(),
            "verification_passed": created and verified,
            "last_error": "" if created and verified else detail or "backup or verification command failed",
        }
        print(json.dumps({"event": "backup_completed" if created and verified else "backup_failed", "instance": key, "verified": created and verified}))
        failed = failed or not (created and verified)
    write_status(args.status, payload)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
