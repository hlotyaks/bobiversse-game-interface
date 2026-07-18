#!/usr/bin/env python3
"""Create a non-deployable game-review package from an exported dashboard request.

This operator tool intentionally runs outside the deployed interface. Steam metadata is
advisory only: it never chooses a container image, digest, ports, resources, or adapter.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_048_576
MAX_TEXT_LENGTH = 500
ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
TAG_PATTERN = re.compile(r"<[^>]*>")
STEAM_DETAILS_URL = "https://store.steampowered.com/api/appdetails"


class SteamMetadataError(Exception):
    """A safe operator-facing Steam metadata error."""


def plain_text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    text = TAG_PATTERN.sub(" ", html.unescape(value))
    text = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(text.split()))
    return text[:limit]


def load_request(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_REQUEST_BYTES:
            raise SteamMetadataError("request file is too large")
        request_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SteamMetadataError("request file is invalid") from exc
    if not isinstance(request_data, dict) or request_data.get("schema_version") != 1:
        raise SteamMetadataError("request file has an unsupported schema")
    app_id = request_data.get("steam_app_id")
    slug = request_data.get("requested_slug")
    steam_url = request_data.get("steam_url")
    if not isinstance(app_id, int) or isinstance(app_id, bool) or not 1 <= app_id <= 2_147_483_647:
        raise SteamMetadataError("request contains an invalid Steam app ID")
    if not isinstance(slug, str) or not ID_PATTERN.fullmatch(slug):
        raise SteamMetadataError("request contains an invalid catalog slug")
    if steam_url != f"https://store.steampowered.com/app/{app_id}/":
        raise SteamMetadataError("request contains a non-canonical Steam Store URL")
    return request_data


def fetch_app_details(app_id: int, opener: Any = urlopen) -> dict[str, Any]:
    url = f"{STEAM_DETAILS_URL}?{urlencode({'appids': app_id})}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "bobiverse-game-catalog-review/1"})
    try:
        with opener(request, timeout=10) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise SteamMetadataError("Steam returned an unexpected content type")
            content_length = response.headers.get("Content-Length")
            if content_length and (not content_length.isdigit() or int(content_length) > MAX_RESPONSE_BYTES):
                raise SteamMetadataError("Steam response is too large")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError) as exc:
        raise SteamMetadataError("Steam metadata is unavailable") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SteamMetadataError("Steam response is too large")
    try:
        payload = json.loads(raw)
        record = payload.get(str(app_id), {})
        data = record.get("data") if isinstance(record, dict) and record.get("success") is True else None
    except (ValueError, AttributeError) as exc:
        raise SteamMetadataError("Steam returned invalid metadata") from exc
    if not isinstance(data, dict) or data.get("type") != "game":
        raise SteamMetadataError("Steam app is unavailable or is not a game")
    return data


def review_package(request_data: dict[str, Any], details: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    categories = details.get("categories", [])
    category_names = [plain_text(item.get("description"), 100) for item in categories if isinstance(item, dict)]
    return {
        "schema_version": 1,
        "request": request_data,
        "steam_metadata": {
            "source_url": request_data["steam_url"],
            "fetched_at": fetched_at,
            "name": plain_text(details.get("name")),
            "short_description": plain_text(details.get("short_description")),
            "categories": [name for name in category_names if name],
            "supported_languages": plain_text(details.get("supported_languages")),
        },
        "review_required": [
            "Review a Linux/amd64 dedicated-server image and pin an immutable digest.",
            "Implement and test a game-specific renderer adapter and secret contract.",
            "Allocate unique ports and slots; set health checks and conservative resources.",
            "Review persistent-data ownership, backup/restore, firewall, Tailscale policy, and rollback.",
        ],
    }


def yaml_skeleton(package: dict[str, Any]) -> str:
    metadata = package["steam_metadata"]
    slug = package["request"]["requested_slug"]
    name = metadata["name"] or slug
    return "\n".join([
        "# REVIEW_REQUIRED: This is not a deployable catalog entry.",
        f"{slug}:",
        f"  display_name: {json.dumps(name)}",
        f"  description: {json.dumps(metadata['short_description'] or 'REVIEW_REQUIRED')}",
        f"  documentation_url: {json.dumps(metadata['source_url'])}",
        "  enabled: false",
        "  supported_players: REVIEW_REQUIRED",
        "  connection: REVIEW_REQUIRED",
        "  image: REVIEW_REQUIRED",
        "  startup: REVIEW_REQUIRED",
        "  resources: REVIEW_REQUIRED",
        "  dependencies: []",
        "  incompatibilities: []",
        "  instance_policy: REVIEW_REQUIRED",
        "  deployment: REVIEW_REQUIRED",
        "  update_policy: REVIEW_REQUIRED",
        "",
    ])


def create_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(content)
    except FileExistsError as exc:
        raise SteamMetadataError(f"refusing to overwrite {path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="exported game-request JSON")
    parser.add_argument("--output-dir", type=Path, required=True, help="empty or existing review-artifact directory")
    args = parser.parse_args()
    try:
        request_data = load_request(args.input)
        details = fetch_app_details(request_data["steam_app_id"])
        package = review_package(request_data, details, datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        slug = request_data["requested_slug"]
        create_exclusive(args.output_dir / f"{slug}-steam-review.json", json.dumps(package, indent=2, sort_keys=True) + "\n")
        create_exclusive(args.output_dir / f"{slug}-catalog-draft.yaml", yaml_skeleton(package))
    except SteamMetadataError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Created review package for {slug} in {args.output_dir}.")
    print("Next: complete the review-required fields, implement the adapter and tests, then open a pull request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
