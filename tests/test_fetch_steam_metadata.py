from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tools" / "fetch_steam_metadata.py"
SPEC = importlib.util.spec_from_file_location("fetch_steam_metadata", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self.body


class SteamMetadataTests(unittest.TestCase):
    def request(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "steam_app_id": 1203620,
            "steam_url": "https://store.steampowered.com/app/1203620/",
            "requested_slug": "enshrouded",
            "purpose": "Test world",
            "requester": "admin@example.test",
            "created_at": "2026-07-17T00:00:00Z",
        }

    def test_request_requires_canonical_bounded_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request.json"
            path.write_text(json.dumps(self.request()), encoding="utf-8")
            self.assertEqual(MODULE.load_request(path)["requested_slug"], "enshrouded")
            request = self.request()
            request["steam_url"] = "https://example.test/app/1203620/"
            path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaises(MODULE.SteamMetadataError):
                MODULE.load_request(path)

    def test_fetch_and_package_strip_html_without_deployment_defaults(self) -> None:
        response = {
            "1203620": {
                "success": True,
                "data": {
                    "type": "game",
                    "name": "<b>Enshrouded</b>",
                    "short_description": "Build <em>together</em>.",
                    "categories": [{"description": "Multi-player"}],
                    "supported_languages": "English, <strong>German</strong>",
                },
            },
        }
        details = MODULE.fetch_app_details(1203620, opener=lambda *_args, **_kwargs: FakeResponse(json.dumps(response).encode("utf-8")))
        package = MODULE.review_package(self.request(), details, "2026-07-17T00:00:00Z")
        self.assertEqual(package["steam_metadata"]["name"], "Enshrouded")
        self.assertEqual(package["steam_metadata"]["short_description"], "Build together.")
        skeleton = MODULE.yaml_skeleton(package)
        self.assertIn("enabled: false", skeleton)
        self.assertIn("image: REVIEW_REQUIRED", skeleton)
        self.assertNotIn("sha256:", skeleton)

    def test_refuses_to_overwrite_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "review.json"
            MODULE.create_exclusive(target, "first\n")
            with self.assertRaises(MODULE.SteamMetadataError):
                MODULE.create_exclusive(target, "second\n")


if __name__ == "__main__":
    unittest.main()
