#!/usr/bin/env python3
"""Restricted Unix-socket controller for registered game service units."""

from __future__ import annotations

import argparse
import grp
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

MAX_REQUEST_BYTES = 16_384
MAX_LOG_LINES = 100
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SECRET_PATTERN = re.compile(r"(?i)(password|pass|secret|token|api[_-]?key|key)\s*([=:])\s*[^\s,;]+")
WRITE_ACTIONS = {"register_instance", "start", "restart"}
READ_ACTIONS = {"list_catalog", "list_instances", "status", "health", "logs", "operation_status"}


class ControllerError(Exception):
    """An expected, safe error returned to the caller."""


class Controller:
    def __init__(self, catalog_path: Path, state_path: Path, audit_path: Path) -> None:
        self.catalog_path = catalog_path
        self.state_path = state_path
        self.audit_path = audit_path
        self.lock = threading.RLock()
        self.operations: dict[str, dict[str, Any]] = {}
        self._ensure_paths()
        self._load_state()

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
        if not isinstance(template_id, str) or not ID_PATTERN.fullmatch(template_id):
            raise ControllerError("invalid template ID")
        if not isinstance(instance_id, str) or not ID_PATTERN.fullmatch(instance_id):
            raise ControllerError("invalid instance ID")
        catalog = self.catalog()
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
        return {
            "key": self.instance_key(template_id, instance_id),
            "template_id": template_id,
            "instance_id": instance_id,
            "display_name": template.get("display_name"),
            "display_label": slot.get("display_label"),
            "ports": slot.get("ports", []),
            "unit": unit,
            "image": template.get("image", {}).get("reference"),
            "image_digest": template.get("image", {}).get("digest"),
            "resources": template.get("resources", {}),
            "startup_timeout_seconds": template.get("startup", {}).get("timeout_seconds", 900),
            "paths": resolved_paths,
            "max_instances": policy.get("max_instances"),
        }

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
            output.append({
                "template_id": template_id,
                "display_name": template.get("display_name"),
                "description": template.get("description"),
                "enabled": template.get("enabled") is True,
                "supported_players": template.get("supported_players"),
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
            self.instances[key] = {
                **resolved,
                "registered_at": self.now(),
                "registration_state": "pending-provisioning",
            }
            self._save_state()
        return self.instances[key]

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
        result = self._systemctl("show", instance["unit"], "--no-page", "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus,ActiveEnterTimestamp")
        details = {"load_state": "unknown", "active_state": "unknown", "sub_state": "unknown", "result": "unknown"}
        if result.returncode == 0:
            keys = {
                "LoadState": "load_state", "ActiveState": "active_state", "SubState": "sub_state",
                "Result": "result", "ExecMainStatus": "exit_status", "ActiveEnterTimestamp": "active_since",
            }
            for line in result.stdout.splitlines():
                name, separator, value = line.partition("=")
                if separator and name in keys:
                    details[keys[name]] = value
        else:
            details["message"] = "service unit is not installed or unavailable"
        return {"template_id": instance["template_id"], "instance_id": instance["instance_id"], "unit": instance["unit"], "registration_state": instance["registration_state"], **details}

    def submit_lifecycle(self, action: str, template_id: Any, instance_id: Any, actor: str) -> dict[str, Any]:
        instance = self._registered(template_id, instance_id)
        status = self.service_status(instance)
        if action == "start" and status["active_state"] in {"active", "activating"}:
            return {"operation_id": None, "state": "already-running", "status": status}
        operation_id = str(uuid.uuid4())
        operation = {
            "operation_id": operation_id,
            "action": action,
            "template_id": instance["template_id"],
            "instance_id": instance["instance_id"],
            "state": "queued",
            "queued_at": self.now(),
        }
        with self.lock:
            self.operations[operation_id] = operation
        thread = threading.Thread(target=self._run_lifecycle, args=(operation_id, instance, actor), daemon=True)
        thread.start()
        return operation

    def _run_lifecycle(self, operation_id: str, instance: dict[str, Any], actor: str) -> None:
        started = time.monotonic()
        with self.lock:
            operation = self.operations[operation_id]
            operation["state"] = "starting" if operation["action"] == "start" else "restarting"
            operation["started_at"] = self.now()
        command = operation["action"]
        result = self._systemctl(command, instance["unit"], "--no-block")
        timeout = min(max(int(instance.get("startup_timeout_seconds", 900)), 1), 1800)
        deadline = time.monotonic() + timeout
        state = "failed"
        message = "systemd rejected the request"
        if result.returncode == 0:
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
        if action not in WRITE_ACTIONS | READ_ACTIONS:
            raise ControllerError("unsupported action")
        actor = request.get("actor", "api-unattributed")
        if not isinstance(actor, str) or len(actor) > 256:
            raise ControllerError("invalid actor")
        template_id, instance_id = request.get("template_id"), request.get("instance_id")
        if action == "list_catalog":
            payload: Any = self.public_catalog()
        elif action == "list_instances":
            payload = list(self.instances.values())
        elif action == "register_instance":
            payload = self.register_instance(template_id, instance_id)
        elif action in {"start", "restart"}:
            payload = self.submit_lifecycle(action, template_id, instance_id, actor)
        elif action == "status":
            payload = self.service_status(self._registered(template_id, instance_id))
        elif action == "health":
            payload = [self.service_status(instance) for instance in self.instances.values()]
        elif action == "logs":
            payload = self.read_logs(self._registered(template_id, instance_id), request.get("tail", 50))
        else:
            operation_id = request.get("operation_id")
            if not isinstance(operation_id, str) or operation_id not in self.operations:
                raise ControllerError("operation is unknown or has expired")
            payload = self.operations[operation_id]
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
