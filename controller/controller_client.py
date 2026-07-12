#!/usr/bin/env python3
"""Small diagnostic client; intended only for the future API service account."""

from __future__ import annotations

import argparse
import json
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", help="JSON object containing one controller request")
    parser.add_argument("--socket", default="/run/game-server-interface/controller.sock")
    args = parser.parse_args()
    try:
        payload = json.loads(args.request)
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(args.socket)
            client.sendall(encoded)
            response = client.makefile("rb").readline()
        print(json.dumps(json.loads(response), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"controller request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
