#!/usr/bin/env python3
"""Unprivileged HTTP interface for the restricted host controller."""

from __future__ import annotations

import json
import os
import re
import socket
import socketserver
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

CONTROLLER_SOCKET = os.environ.get("CONTROLLER_SOCKET", "/run/game-server-interface/controller.sock")
INTERFACE_SOCKET = os.environ.get("INTERFACE_SOCKET", "")
TRUSTED_ACTOR_HEADER = os.environ.get("TRUSTED_ACTOR_HEADER", "0") == "1"
GAME_INTERFACE_ADMIN_LOGINS = frozenset(
    item.strip() for item in os.environ.get("GAME_INTERFACE_ADMIN_LOGINS", "").split(",") if item.strip()
)
MAX_BODY_BYTES = 16_384
MAX_CONTROLLER_RESPONSE_BYTES = 1_048_576
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")
MAX_EXCLUSIONS_PER_TEMPLATE = 64
STEAM_APP_ID_PATTERN = re.compile(r"^(?:https://store\.steampowered\.com/app/)?([1-9][0-9]{0,9})(?:/[^?#]*)?/?(?:[?#].*)?$")
MAX_PURPOSE_LENGTH = 500
STATIC_ROOT = Path(__file__).parent / "static"


class ControllerUnavailable(Exception):
    pass


def controller_request(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(CONTROLLER_SOCKET)
            client.sendall(encoded)
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(min(4096, MAX_CONTROLLER_RESPONSE_BYTES - len(response)))
                if not chunk:
                    break
                response += chunk
                if len(response) >= MAX_CONTROLLER_RESPONSE_BYTES:
                    raise ControllerUnavailable("controller response is too large")
        decoded = json.loads(response)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ControllerUnavailable("controller is unavailable") from exc
    if not isinstance(decoded, dict):
        raise ControllerUnavailable("controller returned an invalid response")
    return decoded


def request_actor(headers: Any) -> str:
    if not TRUSTED_ACTOR_HEADER:
        return "local-loopback"
    value = headers.get("Tailscale-User-Login", "")
    if not isinstance(value, str) or not value or len(value) > 256 or any(character in value for character in "\r\n"):
        return "tailnet-unattributed"
    return value


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.fullmatch(value))


def steam_app_id(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) > 512:
        return None
    match = STEAM_APP_ID_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    app_id = int(match.group(1))
    return app_id if app_id <= 2_147_483_647 else None


def game_request_params(body: dict[str, Any]) -> dict[str, Any] | None:
    source = body.get("steam_url")
    requested_slug = body.get("requested_slug")
    purpose = body.get("purpose", "")
    app_id = steam_app_id(source)
    if app_id is None or not valid_id(requested_slug):
        return None
    if not isinstance(purpose, str) or len(purpose) > MAX_PURPOSE_LENGTH or any(character in purpose for character in "\r\n"):
        return None
    return {"steam_app_id": app_id, "steam_url": f"https://store.steampowered.com/app/{app_id}/", "requested_slug": requested_slug, "purpose": purpose.strip()}


def exclusion_params(body: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a set-exclusions request: a valid template ID and a bounded list of tailnet logins."""
    template_id = body.get("template_id")
    logins = body.get("logins")
    if not valid_id(template_id):
        return None
    if not isinstance(logins, list) or len(logins) > MAX_EXCLUSIONS_PER_TEMPLATE:
        return None
    cleaned: list[str] = []
    for login in logins:
        if not isinstance(login, str) or len(login) > 256 or not LOGIN_PATTERN.fullmatch(login):
            return None
        if login not in cleaned:
            cleaned.append(login)
    return {"template_id": template_id, "logins": cleaned}


def is_game_administrator(actor: str) -> bool:
    return TRUSTED_ACTOR_HEADER and actor not in {"local-loopback", "tailnet-unattributed"} and actor in GAME_INTERFACE_ADMIN_LOGINS


def log_request_params(path: str) -> dict[str, Any] | None:
    query = parse_qs(urlparse(path).query, keep_blank_values=True)
    template_ids = query.get("template_id", [])
    instance_ids = query.get("instance_id", [])
    tails = query.get("tail", [])
    if len(template_ids) != 1 or len(instance_ids) != 1 or len(tails) > 1:
        return None
    template_id, instance_id = template_ids[0], instance_ids[0]
    if not valid_id(template_id) or not valid_id(instance_id):
        return None
    payload: dict[str, Any] = {"template_id": template_id, "instance_id": instance_id}
    if tails:
        try:
            payload["tail"] = int(tails[0])
        except ValueError:
            return None
    return payload


def billing_request_params(path: str) -> dict[str, Any] | None:
    query = parse_qs(urlparse(path).query, keep_blank_values=True)
    template_ids = query.get("template_id", [])
    instance_ids = query.get("instance_id", [])
    months = query.get("month", [])
    if len(template_ids) != 1 or len(instance_ids) != 1 or len(months) > 1:
        return None
    template_id, instance_id = template_ids[0], instance_ids[0]
    if not valid_id(template_id) or not valid_id(instance_id):
        return None
    params: dict[str, Any] = {"template_id": template_id, "instance_id": instance_id}
    if months and months[0]:
        if not MONTH_PATTERN.fullmatch(months[0]):
            return None
        params["month"] = months[0]
    return params


def filter_billing_for_actor(report: dict[str, Any], actor: str, is_admin: bool) -> dict[str, Any]:
    """Reduce a full billing report to what one viewer may see.

    Everyone sees their own line (``you``) and the month/selector metadata. Only administrators
    receive the full per-user breakdown and the aggregate totals (kitty, actual cost).
    """
    users = report.get("users") or {}
    view: dict[str, Any] = {
        "instance": report.get("instance"),
        "month": report.get("month"),
        "available_months": report.get("available_months", []),
        "currency": report.get("currency"),
        "run_cost_per_hour": report.get("run_cost_per_hour"),
        "viewer": actor,
        "is_admin": bool(is_admin),
        "you": users.get(actor),
    }
    if is_admin:
        view["users"] = users
        view["totals"] = report.get("totals", {})
    return view


class InterfaceHandler(SimpleHTTPRequestHandler):
    server_version = "GameServerInterface/1"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid request headers and request bodies in logs.
        address = self.client_address[0] if isinstance(self.client_address, tuple) else "unix"
        print(f"{address} - {format % args}", flush=True)

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header or not length_header.isdigit() or int(length_header) > MAX_BODY_BYTES:
            raise ValueError("invalid request body")
        decoded = json.loads(self.rfile.read(int(length_header)))
        if not isinstance(decoded, dict):
            raise ValueError("request body must be an object")
        return decoded

    def controller(self, action: str, **payload: Any) -> dict[str, Any]:
        response = controller_request({"action": action, "actor": request_actor(self.headers), **payload})
        if response.get("ok") is not True:
            error = response.get("error", "controller rejected request")
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return {}
        result = response.get("result")
        if not isinstance(result, (dict, list)):
            self.send_json(HTTPStatus.BAD_GATEWAY, {"error": "controller returned an invalid result"})
            return {}
        return {"result": result}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/api/catalog":
            payload = self.controller("list_catalog")
        elif path == "/api/game-requests/policy":
            payload = {"result": {"allowed": is_game_administrator(request_actor(self.headers)), "max_purpose_length": MAX_PURPOSE_LENGTH}}
        elif path == "/api/capacity":
            payload = self.controller("capacity")
        elif path == "/api/backup-status":
            payload = self.controller("backup_status")
        elif path == "/api/billing":
            params = billing_request_params(self.path)
            if params is None:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid billing request"})
                return
            result = self.controller("billing", **params)
            if not result:
                return
            actor = request_actor(self.headers)
            payload = {"result": filter_billing_for_actor(result["result"], actor, is_game_administrator(actor))}
        elif path == "/api/exclusions":
            actor = request_actor(self.headers)
            if not is_game_administrator(actor):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "managing player exclusions requires an authorized tailnet administrator"})
                return
            payload = self.controller("list_exclusions")
        elif path == "/api/instances":
            instances = self.controller("list_instances")
            if not instances:
                return
            records = []
            for instance in instances["result"]:
                if not isinstance(instance, dict):
                    continue
                status = self.controller("status", template_id=instance.get("template_id"), instance_id=instance.get("instance_id"))
                if not status:
                    return
                records.append({"instance": instance, "status": status["result"]})
            payload = {"result": records}
        elif path == "/api/logs":
            params = log_request_params(self.path)
            if params is None:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid log request"})
                return
            payload = self.controller("logs", **params)
        elif path.startswith("/api/operations/"):
            operation_id = path.removeprefix("/api/operations/")
            payload = self.controller("operation_status", operation_id=operation_id)
        else:
            self.serve_static(path)
            return
        if payload:
            self.send_json(HTTPStatus.OK, payload)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.body()
        except (ValueError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON request"})
            return
        if path == "/api/game-requests":
            actor = request_actor(self.headers)
            if not is_game_administrator(actor):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "game requests require an authorized tailnet administrator"})
                return
            params = game_request_params(body)
            if params is None:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "provide a Steam Store URL or app ID, a valid catalog slug, and a short single-line purpose"})
                return
            payload = self.controller("create_game_request", steam_app_id=params["steam_app_id"], requested_slug=params["requested_slug"])
            status = HTTPStatus.CREATED
            if payload:
                payload["result"] = {**payload["result"], **params, "schema_version": 1, "requester": actor}
        elif path == "/api/exclusions":
            actor = request_actor(self.headers)
            if not is_game_administrator(actor):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "managing player exclusions requires an authorized tailnet administrator"})
                return
            params = exclusion_params(body)
            if params is None:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "provide a valid template ID and a list of tailnet logins"})
                return
            payload = self.controller("set_exclusions", template_id=params["template_id"], logins=params["logins"])
            status = HTTPStatus.OK
        else:
            template_id, instance_id = body.get("template_id"), body.get("instance_id")
            if not valid_id(template_id) or not valid_id(instance_id):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid template or instance ID"})
                return
            if path == "/api/instances":
                payload = self.controller("register_instance", template_id=template_id, instance_id=instance_id)
                status = HTTPStatus.CREATED
            elif path == "/api/actions/start":
                payload = self.controller("start", template_id=template_id, instance_id=instance_id)
                status = HTTPStatus.ACCEPTED
            elif path == "/api/actions/restart":
                payload = self.controller("restart", template_id=template_id, instance_id=instance_id)
                status = HTTPStatus.ACCEPTED
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
        if payload:
            self.send_json(status, payload)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT not in candidate.parents and candidate != STATIC_ROOT:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not candidate.is_file() or candidate.suffix not in {".html", ".css", ".js"}:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
        encoded = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types[candidate.suffix])
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    if INTERFACE_SOCKET:
        socket_path = Path(INTERFACE_SOCKET)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()

        class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        server = UnixHTTPServer(str(socket_path), InterfaceHandler)
        os.chmod(socket_path, 0o660)
        print(f"game interface listening on unix:{socket_path}", flush=True)
        server.serve_forever()
        return
    host = os.environ.get("INTERFACE_HOST", "0.0.0.0")
    port = int(os.environ.get("INTERFACE_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), InterfaceHandler)
    server.daemon_threads = True
    print(f"game interface listening on {host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
