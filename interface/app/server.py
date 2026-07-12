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
from urllib.parse import urlparse

CONTROLLER_SOCKET = os.environ.get("CONTROLLER_SOCKET", "/run/game-server-interface/controller.sock")
INTERFACE_SOCKET = os.environ.get("INTERFACE_SOCKET", "")
TRUSTED_ACTOR_HEADER = os.environ.get("TRUSTED_ACTOR_HEADER", "0") == "1"
MAX_BODY_BYTES = 16_384
MAX_CONTROLLER_RESPONSE_BYTES = 1_048_576
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
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
        elif path == "/api/capacity":
            payload = self.controller("capacity")
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
