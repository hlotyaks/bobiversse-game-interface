from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "interface" / "app" / "server.py"
SPEC = importlib.util.spec_from_file_location("game_interface", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InterfaceInputTests(unittest.TestCase):
    def test_only_catalog_style_ids_are_accepted(self) -> None:
        self.assertTrue(MODULE.valid_id("valheim"))
        self.assertTrue(MODULE.valid_id("secondary-2"))
        self.assertFalse(MODULE.valid_id("../../etc"))
        self.assertFalse(MODULE.valid_id("VALHEIM"))
        self.assertFalse(MODULE.valid_id(None))

    def test_log_request_requires_valid_single_instance_identifiers(self) -> None:
        self.assertEqual(
            MODULE.log_request_params("/api/logs?template_id=valheim&instance_id=primary&tail=25"),
            {"template_id": "valheim", "instance_id": "primary", "tail": 25},
        )
        self.assertEqual(
            MODULE.log_request_params("/api/logs?template_id=valheim&instance_id=primary"),
            {"template_id": "valheim", "instance_id": "primary"},
        )
        self.assertIsNone(MODULE.log_request_params("/api/logs?template_id=../etc&instance_id=primary"))
        self.assertIsNone(MODULE.log_request_params("/api/logs?template_id=valheim&instance_id=primary&tail=not-a-number"))
        self.assertIsNone(MODULE.log_request_params("/api/logs?template_id=valheim&template_id=enshrouded&instance_id=primary"))

    def test_untrusted_header_is_not_used_as_actor(self) -> None:
        previous = MODULE.TRUSTED_ACTOR_HEADER
        MODULE.TRUSTED_ACTOR_HEADER = False
        try:
            self.assertEqual(MODULE.request_actor({"Tailscale-User-Login": "spoofed@example.test"}), "local-loopback")
        finally:
            MODULE.TRUSTED_ACTOR_HEADER = previous

    def test_trusted_actor_header_is_bounded(self) -> None:
        previous = MODULE.TRUSTED_ACTOR_HEADER
        MODULE.TRUSTED_ACTOR_HEADER = True
        try:
            self.assertEqual(MODULE.request_actor({"Tailscale-User-Login": "alice@example.test"}), "alice@example.test")
            self.assertEqual(MODULE.request_actor({"Tailscale-User-Login": "x" * 257}), "tailnet-unattributed")
        finally:
            MODULE.TRUSTED_ACTOR_HEADER = previous

    def test_game_request_normalizes_only_canonical_steam_inputs(self) -> None:
        self.assertEqual(
            MODULE.game_request_params({"steam_url": "https://store.steampowered.com/app/1203620/Enshrouded/", "requested_slug": "enshrouded", "purpose": "Test world"}),
            {"steam_app_id": 1203620, "steam_url": "https://store.steampowered.com/app/1203620/", "requested_slug": "enshrouded", "purpose": "Test world"},
        )
        self.assertEqual(MODULE.steam_app_id("1203620"), 1203620)
        self.assertIsNone(MODULE.steam_app_id("http://store.steampowered.com/app/1203620"))
        self.assertIsNone(MODULE.game_request_params({"steam_url": "1203620", "requested_slug": "Not-safe"}))
        self.assertIsNone(MODULE.game_request_params({"steam_url": "1203620", "requested_slug": "safe", "purpose": "two\nlines"}))

    def test_game_request_administrator_requires_trusted_allowlisted_actor(self) -> None:
        previous_trusted = MODULE.TRUSTED_ACTOR_HEADER
        previous_logins = MODULE.GAME_INTERFACE_ADMIN_LOGINS
        MODULE.TRUSTED_ACTOR_HEADER = True
        MODULE.GAME_INTERFACE_ADMIN_LOGINS = frozenset({"admin@example.test"})
        try:
            self.assertTrue(MODULE.is_game_administrator("admin@example.test"))
            self.assertFalse(MODULE.is_game_administrator("member@example.test"))
            self.assertFalse(MODULE.is_game_administrator("tailnet-unattributed"))
        finally:
            MODULE.TRUSTED_ACTOR_HEADER = previous_trusted
            MODULE.GAME_INTERFACE_ADMIN_LOGINS = previous_logins


if __name__ == "__main__":
    unittest.main()
