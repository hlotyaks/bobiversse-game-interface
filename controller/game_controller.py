#!/usr/bin/env python3
"""Restricted Unix-socket controller for registered game service units."""

from __future__ import annotations

import argparse
import grp
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

MAX_REQUEST_BYTES = 16_384
MAX_LOG_LINES = 100
OPERATION_RETENTION = timedelta(hours=24)
CRASH_LOOP_RESTART_THRESHOLD = 5
CRASH_LOOP_AUTO_RESTART_THRESHOLD = max(1, CRASH_LOOP_RESTART_THRESHOLD - 2)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SECRET_PATTERN = re.compile(r"(?i)(password|pass|secret|token|api[_-]?key|key)\s*([=:])\s*[^\s,;]+")
WRITE_ACTIONS = {"register_instance", "start", "restart"}
READ_ACTIONS = {"list_catalog", "list_instances", "status", "health", "logs", "operation_status", "capacity", "backup_status", "billing"}


class ControllerError(Exception):
    """An expected, safe error returned to the caller."""


def resolve_slot_from_catalog(catalog: dict[str, Any], template_id: Any, instance_id: Any) -> dict[str, Any]:
    """Resolve one catalog slot into its derived paths, ports, image, and limits.

    This is the single source of truth for turning a (template, instance) pair into the
    allowlisted, catalog-derived values. It is shared by the controller and by the
    out-of-band instance renderer so both agree on ports, image digest, resource limits,
    and paths. It performs no I/O and enforces the same invariants the controller relies on.
    """
    if not isinstance(template_id, str) or not ID_PATTERN.fullmatch(template_id):
        raise ControllerError("invalid template ID")
    if not isinstance(instance_id, str) or not ID_PATTERN.fullmatch(instance_id):
        raise ControllerError("invalid instance ID")
    template = catalog["templates"].get(template_id)
    if not isinstance(template, dict):
        raise ControllerError("unknown template")
    if template.get("enabled") is not True:
        raise ControllerError("template is disabled")
    policy = template.get("instance_policy")
    if not isinstance(policy, dict) or instance_id not in policy.get("allowed_instance_ids", []):
        raise ControllerError("instance ID is not allowlisted for this template")
    slot = policy.get("slots", {}).get(instance_id)
    if not isinstance(slot, dict):
        raise ControllerError("instance slot is invalid")
    paths = catalog["path_templates"]
    try:
        resolved_paths = {name: value.format(template=template_id, instance=instance_id) for name, value in paths.items()}
    except (AttributeError, KeyError, ValueError) as exc:
        raise ControllerError("catalog path template is invalid") from exc
    unit = resolved_paths.get("systemd_unit")
    expected_unit = f"game-{template_id}-{instance_id}.service"
    if unit != expected_unit:
        raise ControllerError("catalog systemd unit is invalid")
    resources = template.get("resources", {})
    return {
        "key": f"{template_id}:{instance_id}",
        "template_id": template_id,
        "instance_id": instance_id,
        "display_name": template.get("display_name"),
        "display_label": slot.get("display_label"),
        "ports": slot.get("ports", []),
        "unit": unit,
        "image": template.get("image", {}).get("reference"),
        "image_digest": template.get("image", {}).get("digest"),
        "resources": resources,
        "resource_limits": {
            "compose": {"cpus": str(resources.get("cpu_cores")), "mem_limit": f"{resources.get('memory_mib')}m"},
            "systemd": {"CPUQuota": f"{float(resources.get('cpu_cores', 0)) * 100:g}%", "MemoryMax": f"{resources.get('memory_mib')}M"},
        },
        "startup_timeout_seconds": template.get("startup", {}).get("timeout_seconds", 900),
        "paths": resolved_paths,
        "max_instances": policy.get("max_instances"),
    }


class Controller:
    def __init__(
        self,
        catalog_path: Path,
        state_path: Path,
        audit_path: Path,
        presence_ledger: Path | None = None,
        billing_config: Path | None = None,
        billing_module: Path | None = None,
    ) -> None:
        self.catalog_path = catalog_path
        self.state_path = state_path
        self.audit_path = audit_path
        self.operations_path = state_path.with_name("operations.json")
        self.backup_status_path = state_path.with_name("backup_status.json")
        # Usage-metering read paths. The billing calculator (tools/billing.py) is installed next
        # to this controller in the deployed layout, so default to a sibling module.
        self.presence_ledger = presence_ledger or Path("/var/lib/game-server-interface/presence.jsonl")
        self.billing_config_path = billing_config or Path("/etc/game-server-interface/billing.yaml")
        self.billing_module_path = billing_module or Path(__file__).with_name("billing.py")
        self._billing_module: Any = None
        self.lock = threading.RLock()
        self.operations: dict[str, dict[str, Any]] = {}
        self._ensure_paths()
        self._load_state()
        self._load_operations()

    def _billing(self) -> Any:
        """Load the billing calculator module once, on first use."""
        if self._billing_module is None:
            spec = importlib.util.spec_from_file_location("billing", self.billing_module_path)
            if not spec or not spec.loader:
                raise ControllerError("billing calculator is unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._billing_module = module
        return self._billing_module

    def billing_report(self, template_id: Any, instance_id: Any, month: Any) -> dict[str, Any]:
        """Compute the usage/cost-share report for one instance and month (dry run, no money).

        Returns the full report (all users). The unprivileged interface is responsible for
        showing a caller only their own line unless they are an administrator.
        """
        if not isinstance(template_id, str) or not ID_PATTERN.fullmatch(template_id):
            raise ControllerError("invalid template ID")
        if not isinstance(instance_id, str) or not ID_PATTERN.fullmatch(instance_id):
            raise ControllerError("invalid instance ID")
        if month is not None and (not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month)):
            raise ControllerError("invalid month")
        billing = self._billing()
        try:
            config = yaml.safe_load(self.billing_config_path.read_text(encoding="utf-8")) if self.billing_config_path.exists() else {}
        except (OSError, yaml.YAMLError) as exc:
            raise ControllerError("billing config is unavailable") from exc
        if not isinstance(config, dict):
            raise ControllerError("billing config is invalid")
        instance_key = f"{template_id}-{instance_id}"
        return billing.build_report(self.presence_ledger, config, instance_key, month)

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def _ensure_paths(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.audit_path.exists():
            self.audit_path.touch(mode=0o600)
            os.chmod(self.audit_path, 0o600)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            self.instances: dict[str, dict[str, Any]] = {}
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.instances = payload.get("instances", {})
            if not isinstance(self.instances, dict):
                raise ValueError("instances is not a mapping")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load controller state: {exc}") from exc

    def _save_state(self) -> None:
        payload = json.dumps({"schema_version": 1, "instances": self.instances}, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix="instances.", dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.state_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _save_operations(self) -> None:
        self._atomic_json_write(self.operations_path, {"schema_version": 1, "operations": self.operations})

    def _atomic_json_write(self, path: Path, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f"{path.stem}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _load_operations(self) -> None:
        if not self.operations_path.exists():
            return
        try:
            payload = json.loads(self.operations_path.read_text(encoding="utf-8"))
            operations = payload.get("operations", {})
            if not isinstance(operations, dict):
                raise ValueError("operations is not a mapping")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load operation state: {exc}") from exc
        changed = False
        cutoff = datetime.now(UTC) - OPERATION_RETENTION
        for operation_id, operation in operations.items():
            if not isinstance(operation_id, str) or not isinstance(operation, dict):
                changed = True
                continue
            completed_at = self._parse_timestamp(operation.get("completed_at"))
            if completed_at and completed_at < cutoff:
                changed = True
                continue
            if operation.get("state") in {"queued", "starting", "restarting"}:
                operation["state"] = "failed"
                operation["completed_at"] = self.now()
                operation["message"] = "controller restarted before the operation completed"
                changed = True
            self.operations[operation_id] = operation
        if changed:
            self._save_operations()

    def catalog(self) -> dict[str, Any]:
        try:
            catalog = yaml.safe_load(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ControllerError("catalog is unavailable") from exc
        if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
            raise ControllerError("catalog is invalid")
        if not isinstance(catalog.get("templates"), dict) or not isinstance(catalog.get("path_templates"), dict):
            raise ControllerError("catalog is invalid")
        return catalog

    @staticmethod
    def instance_key(template_id: str, instance_id: str) -> str:
        return f"{template_id}:{instance_id}"

    def resolve_slot(self, template_id: Any, instance_id: Any) -> dict[str, Any]:
        return resolve_slot_from_catalog(self.catalog(), template_id, instance_id)

    def audit(self, event: dict[str, Any]) -> None:
        record = {"timestamp": self.now(), **event}
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            descriptor = os.open(self.audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as audit_file:
                audit_file.write(line)
                audit_file.flush()
                os.fsync(audit_file.fileno())
        except OSError:
            logging.exception("audit write failed")
            raise ControllerError("audit log is unavailable")

    def public_catalog(self) -> list[dict[str, Any]]:
        catalog = self.catalog()
        output = []
        for template_id, template in sorted(catalog["templates"].items()):
            connection = template.get("connection", {})
            public_connection = {
                "hostname": connection.get("hostname"),
                "ip": connection.get("ip"),
                "protocol": connection.get("protocol"),
            } if isinstance(connection, dict) else {}
            output.append({
                "template_id": template_id,
                "display_name": template.get("display_name"),
                "description": template.get("description"),
                "enabled": template.get("enabled") is True,
                "supported_players": template.get("supported_players"),
                "connection": public_connection,
                "resources": template.get("resources", {}),
                "instance_ids": template.get("instance_policy", {}).get("allowed_instance_ids", []),
            })
        return output

    def register_instance(self, template_id: Any, instance_id: Any) -> dict[str, Any]:
        resolved = self.resolve_slot(template_id, instance_id)
        key = resolved["key"]
        with self.lock:
            if key in self.instances:
                raise ControllerError("instance is already registered")
            same_template = [item for item in self.instances.values() if item.get("template_id") == template_id]
            if len(same_template) >= resolved["max_instances"]:
                raise ControllerError("template instance limit reached")
            admission = self.admission(resolved)
            if not admission["allowed"]:
                raise ControllerError("registration rejected: " + "; ".join(admission["reasons"]))
            self.instances[key] = {
                **resolved,
                "registered_at": self.now(),
                "registration_state": "pending-provisioning",
                "admission": admission,
            }
            self._save_state()
        return self.instances[key]

    def create_game_request(self, steam_app_id: Any, requested_slug: Any) -> dict[str, Any]:
        """Audit a bounded proposal without creating deployable host state."""
        if not isinstance(steam_app_id, int) or isinstance(steam_app_id, bool) or not 1 <= steam_app_id <= 2_147_483_647:
            raise ControllerError("invalid Steam app ID")
        if not isinstance(requested_slug, str) or not ID_PATTERN.fullmatch(requested_slug):
            raise ControllerError("invalid requested catalog slug")
        return {"steam_app_id": steam_app_id, "requested_slug": requested_slug, "created_at": self.now()}

    def _registered(self, template_id: Any, instance_id: Any) -> dict[str, Any]:
        if not isinstance(template_id, str) or not isinstance(instance_id, str):
            raise ControllerError("template_id and instance_id are required")
        key = self.instance_key(template_id, instance_id)
        instance = self.instances.get(key)
        if instance is None:
            raise ControllerError("instance is not registered")
        return instance

    @staticmethod
    def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/usr/bin/systemctl", *arguments], capture_output=True, text=True, timeout=30, check=False)

    def service_status(self, instance: dict[str, Any]) -> dict[str, Any]:
        result = self._systemctl("show", instance["unit"], "--no-page", "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus,ActiveEnterTimestamp,MemoryCurrent,CPUUsageNSec,NRestarts")
        details = {"load_state": "unknown", "active_state": "unknown", "sub_state": "unknown", "result": "unknown"}
        if result.returncode == 0:
            keys = {
                "LoadState": "load_state", "ActiveState": "active_state", "SubState": "sub_state",
                "Result": "result", "ExecMainStatus": "exit_status", "ActiveEnterTimestamp": "active_since",
                "MemoryCurrent": "memory_current_bytes", "CPUUsageNSec": "cpu_usage_nsec",
                "NRestarts": "restart_count_recent",
            }
            for line in result.stdout.splitlines():
                name, separator, value = line.partition("=")
                if separator and name in keys:
                    details[keys[name]] = value
            memory_current = details.get("memory_current_bytes")
            if isinstance(memory_current, str) and memory_current.isdigit():
                details["memory_current_mib"] = round(int(memory_current) / 1024 / 1024, 1)
            restart_count = details.get("restart_count_recent", "0")
            if not isinstance(restart_count, str) or not restart_count.isdigit():
                restart_count = "0"
            details["restart_count_recent"] = int(restart_count)
            details["crash_loop"] = details.get("active_state") == "failed" and (
                details.get("result") == "start-limit-hit"
                or int(restart_count) >= CRASH_LOOP_AUTO_RESTART_THRESHOLD
            )
            if details["crash_loop"]:
                details["last_failure_reason"] = "systemd restart limit reached; manual retry required"
        else:
            details["message"] = "service unit is not installed or unavailable"
        return {"template_id": instance["template_id"], "instance_id": instance["instance_id"], "unit": instance["unit"], "registration_state": instance["registration_state"], "backup": self.backup_status_for(instance), **details}

    def backup_status_for(self, instance: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = json.loads(self.backup_status_path.read_text(encoding="utf-8"))
            entries = payload.get("instances", {})
            status = entries.get(instance["key"], {}) if isinstance(entries, dict) else {}
            return status if isinstance(status, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def backup_status(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.backup_status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "instances": {}, "message": "no automated backup has run yet"}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ControllerError("backup status is unavailable") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("instances", {}), dict):
            raise ControllerError("backup status is invalid")
        return payload

    @staticmethod
    def memory_info() -> dict[str, int]:
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition(":")
                fields = value.split()
                if fields and fields[0].isdigit():
                    values[name] = int(fields[0]) // 1024
        except OSError:
            pass
        return values

    def capacity_policy(self) -> dict[str, Any]:
        policy = self.catalog().get("capacity_policy")
        if not isinstance(policy, dict):
            raise ControllerError("capacity policy is invalid")
        return policy

    def disk_capacity(self, paths: list[Any]) -> list[dict[str, Any]]:
        capacities: list[dict[str, Any]] = []
        seen_devices: set[int] = set()
        for configured_path in paths:
            if not isinstance(configured_path, str):
                continue
            path = Path(configured_path)
            while not path.exists() and path != path.parent:
                path = path.parent
            try:
                usage = shutil.disk_usage(path)
                device = path.stat().st_dev
            except OSError:
                capacities.append({"path": configured_path, "available_gib": 0, "error": "path unavailable"})
                continue
            if device in seen_devices:
                continue
            seen_devices.add(device)
            capacities.append({"path": str(path), "available_gib": round(usage.free / 1024**3, 1), "total_gib": round(usage.total / 1024**3, 1)})
        return capacities

    def admission(self, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        policy = self.capacity_policy()
        limits = policy["admission_limits"]
        reserve = policy["host_safety_reserve"]
        running: list[dict[str, Any]] = []
        reservation = {"cpu_cores": 0.0, "memory_mib": 0.0, "disk_gib": 0.0}
        candidate_key = candidate.get("key") if candidate else None
        for instance in self.instances.values():
            if instance.get("key") == candidate_key:
                continue
            status = self.service_status(instance)
            if status.get("active_state") in {"active", "activating", "reloading"}:
                resources = instance.get("resources", {})
                for field in reservation:
                    reservation[field] += float(resources.get(field, 0))
                running.append({"template_id": instance["template_id"], "instance_id": instance["instance_id"], "state": status.get("active_state")})
        requested = candidate.get("resources", {}) if candidate else {}
        projected = {field: reservation[field] + float(requested.get(field, 0)) for field in reservation}
        reasons: list[str] = []
        if projected["cpu_cores"] > float(limits["cpu_cores"]):
            reasons.append(f"CPU reservation {projected['cpu_cores']:g} exceeds {limits['cpu_cores']:g} core admission limit")
        if projected["memory_mib"] > float(limits["memory_mib"]):
            reasons.append(f"memory reservation {projected['memory_mib']:g} MiB exceeds {limits['memory_mib']:g} MiB admission limit")
        disks = self.disk_capacity(policy.get("disk_paths", []))
        required_free = projected["disk_gib"] + float(reserve["disk_gib"])
        for disk in disks:
            if "error" in disk:
                reasons.append(f"disk path {disk['path']} is unavailable")
            elif disk["available_gib"] < required_free:
                reasons.append(f"disk at {disk['path']} has {disk['available_gib']:g} GiB free; {required_free:g} GiB is required for reservations and safety reserve")
        memory = self.memory_info()
        swap_free = memory.get("SwapFree", 0)
        if swap_free < float(reserve["swap_free_mib"]):
            reasons.append(f"swap free {swap_free:g} MiB is below the {reserve['swap_free_mib']:g} MiB safety threshold")
        return {
            "allowed": not reasons,
            "reasons": reasons,
            "limits": limits,
            "host_safety_reserve": reserve,
            "running_instances": running,
            "running_reservation": reservation,
            "requested_reservation": requested,
            "projected_reservation": projected,
            "disk": disks,
            "host_memory_available_mib": memory.get("MemAvailable", 0),
            "host_swap_free_mib": swap_free,
        }

    def capacity_summary(self) -> dict[str, Any]:
        return self.admission()

    def submit_lifecycle(self, action: str, template_id: Any, instance_id: Any, actor: str) -> dict[str, Any]:
        instance = self._registered(template_id, instance_id)
        status = self.service_status(instance)
        if action == "start" and status["active_state"] in {"active", "activating"}:
            return {"operation_id": None, "state": "already-running", "status": status}
        admission = self.admission(instance)
        if not admission["allowed"]:
            raise ControllerError("start rejected: " + "; ".join(admission["reasons"]))
        operation_id = str(uuid.uuid4())
        operation = {
            "operation_id": operation_id,
            "action": action,
            "template_id": instance["template_id"],
            "instance_id": instance["instance_id"],
            "state": "queued",
            "queued_at": self.now(),
            "admission": admission,
        }
        with self.lock:
            self.operations[operation_id] = operation
            self._save_operations()
        thread = threading.Thread(target=self._run_lifecycle, args=(operation_id, instance, actor), daemon=True)
        thread.start()
        return operation

    def _run_lifecycle(self, operation_id: str, instance: dict[str, Any], actor: str) -> None:
        started = time.monotonic()
        with self.lock:
            operation = self.operations[operation_id]
            operation["state"] = "starting" if operation["action"] == "start" else "restarting"
            operation["started_at"] = self.now()
            self._save_operations()
        command = operation["action"]
        result: subprocess.CompletedProcess[str] | None = None
        state = "failed"
        message = "systemd rejected the request"
        crash_loop = self.service_status(instance).get("crash_loop") is True
        reset_failed = False
        if crash_loop:
            reset_failed = self._systemctl("reset-failed", instance["unit"]).returncode != 0
        admission = self.admission(instance)
        if reset_failed:
            message = "could not reset systemd crash-loop protection"
        elif not admission["allowed"]:
            state, message = "failed", "start rejected after queue: " + "; ".join(admission["reasons"])
        else:
            result = self._systemctl(command, instance["unit"], "--no-block")
        timeout = min(max(int(instance.get("startup_timeout_seconds", 900)), 1), 1800)
        deadline = time.monotonic() + timeout
        if result is not None:
            state = "failed"
            message = "systemd rejected the request"
        if result is not None and result.returncode == 0:
            while time.monotonic() < deadline:
                status = self.service_status(instance)
                if status["active_state"] == "active":
                    state, message = "healthy", "service is active"
                    break
                if status["active_state"] == "failed":
                    message = "service entered failed state"
                    break
                time.sleep(2)
            else:
                message = "startup timed out"
        with self.lock:
            operation["state"] = state
            operation["completed_at"] = self.now()
            operation["duration_ms"] = round((time.monotonic() - started) * 1000)
            operation["message"] = message
            self._save_operations()
        self.audit({
            "action": command, "template_id": instance["template_id"], "instance_id": instance["instance_id"],
            "actor": actor, "result": state, "message": message, "duration_ms": operation["duration_ms"],
        })

    def read_logs(self, instance: dict[str, Any], tail: Any) -> dict[str, Any]:
        if not isinstance(tail, int) or isinstance(tail, bool):
            tail = 50
        tail = max(1, min(tail, MAX_LOG_LINES))
        result = subprocess.run(
            ["/usr/bin/journalctl", "--no-pager", "--output=short-iso", "-u", instance["unit"], "-n", str(tail)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        lines = [SECRET_PATTERN.sub(r"\1\2<redacted>", line) for line in result.stdout.splitlines()]
        return {"template_id": instance["template_id"], "instance_id": instance["instance_id"], "lines": lines}

    def dispatch(self, request: dict[str, Any], peer_uid: int) -> dict[str, Any]:
        action = request.get("action")
        if action not in WRITE_ACTIONS | READ_ACTIONS | {"create_game_request"}:
            raise ControllerError("unsupported action")
        actor = request.get("actor", "api-unattributed")
        if not isinstance(actor, str) or len(actor) > 256:
            raise ControllerError("invalid actor")
        template_id, instance_id = request.get("template_id"), request.get("instance_id")
        try:
            if action == "list_catalog":
                payload: Any = self.public_catalog()
            elif action == "list_instances":
                payload = list(self.instances.values())
            elif action == "register_instance":
                payload = self.register_instance(template_id, instance_id)
            elif action == "create_game_request":
                payload = self.create_game_request(request.get("steam_app_id"), request.get("requested_slug"))
            elif action in {"start", "restart"}:
                payload = self.submit_lifecycle(action, template_id, instance_id, actor)
            elif action == "status":
                payload = self.service_status(self._registered(template_id, instance_id))
            elif action == "health":
                payload = [self.service_status(instance) for instance in self.instances.values()]
            elif action == "capacity":
                payload = self.capacity_summary()
            elif action == "backup_status":
                payload = self.backup_status()
            elif action == "billing":
                payload = self.billing_report(template_id, instance_id, request.get("month"))
            elif action == "logs":
                payload = self.read_logs(self._registered(template_id, instance_id), request.get("tail", 50))
            else:
                operation_id = request.get("operation_id")
                if not isinstance(operation_id, str) or operation_id not in self.operations:
                    raise ControllerError("operation is unknown or has expired")
                payload = self.operations[operation_id]
        except ControllerError as exc:
            self.audit({"action": action, "template_id": template_id, "instance_id": instance_id, "actor": actor, "peer_uid": peer_uid, "result": "rejected", "error": str(exc)})
            raise
        self.audit({"action": action, "template_id": template_id, "instance_id": instance_id, "actor": actor, "peer_uid": peer_uid, "result": "accepted"})
        return {"ok": True, "result": payload}


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer_uid = self.server.peer_uid(self.request)  # type: ignore[attr-defined]
        if peer_uid != self.server.api_uid:  # type: ignore[attr-defined]
            self.wfile.write(b'{"ok":false,"error":"unauthorized local caller"}\n')
            return
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            self.wfile.write(b'{"ok":false,"error":"invalid request size"}\n')
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
            response = self.server.controller.dispatch(request, peer_uid)  # type: ignore[attr-defined]
        except (ValueError, json.JSONDecodeError, ControllerError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:
            logging.exception("controller request failed")
            response = {"ok": False, "error": "controller error"}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))


class UnixControllerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    @staticmethod
    def peer_uid(connection: socket.socket) -> int:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("/etc/game-server-interface/catalog.yaml"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/game-server-interface/instances.json"))
    parser.add_argument("--audit", type=Path, default=Path("/var/log/game-server-interface/audit.jsonl"))
    parser.add_argument("--socket", type=Path, default=Path("/run/game-server-interface/controller.sock"))
    parser.add_argument("--api-user", default="game-interface-api")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.geteuid() != 0:
        print("controller must run as root", file=sys.stderr)
        return 2
    try:
        api = __import__("pwd").getpwnam(args.api_user)
        api_group = grp.getgrgid(api.pw_gid)
    except KeyError:
        print(f"API user {args.api_user!r} is missing", file=sys.stderr)
        return 2
    args.socket.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(args.socket.parent, 0, api_group.gr_gid)
    os.chmod(args.socket.parent, 0o750)
    if args.socket.exists():
        args.socket.unlink()
    controller = Controller(args.catalog, args.state, args.audit)
    with UnixControllerServer(str(args.socket), RequestHandler) as server:
        server.controller = controller  # type: ignore[attr-defined]
        server.api_uid = api.pw_uid  # type: ignore[attr-defined]
        os.chown(args.socket, 0, api_group.gr_gid)
        os.chmod(args.socket, 0o660)
        logging.info("controller listening on %s", args.socket)
        server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
