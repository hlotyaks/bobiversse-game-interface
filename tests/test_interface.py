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


class BillingRouteTests(unittest.TestCase):
    def test_billing_params_require_valid_ids_and_optional_month(self) -> None:
        self.assertEqual(
            MODULE.billing_request_params("/api/billing?template_id=enshrouded&instance_id=primary"),
            {"template_id": "enshrouded", "instance_id": "primary"},
        )
        self.assertEqual(
            MODULE.billing_request_params("/api/billing?template_id=enshrouded&instance_id=primary&month=2026-07"),
            {"template_id": "enshrouded", "instance_id": "primary", "month": "2026-07"},
        )
        self.assertIsNone(MODULE.billing_request_params("/api/billing?template_id=../x&instance_id=primary"))
        self.assertIsNone(MODULE.billing_request_params("/api/billing?template_id=enshrouded&instance_id=primary&month=2026-13"))
        self.assertIsNone(MODULE.billing_request_params("/api/billing?instance_id=primary"))

    def test_non_admin_sees_only_their_own_line(self) -> None:
        report = {
            "instance": "enshrouded-primary", "month": "2026-07", "available_months": ["2026-07"],
            "currency": "USD", "run_cost_per_hour": 0.18,
            "users": {"alice@ex": {"hours": 2.0, "charge": 0.2}, "bob@ex": {"hours": 1.0, "charge": 0.1}},
            "totals": {"kitty": 0.05, "actual_cost": 0.25},
        }
        view = MODULE.filter_billing_for_actor(report, "alice@ex", is_admin=False)
        self.assertEqual(view["you"], {"hours": 2.0, "charge": 0.2})
        self.assertFalse(view["is_admin"])
        self.assertNotIn("users", view)   # cannot see bob
        self.assertNotIn("totals", view)  # cannot see the aggregate kitty
        self.assertEqual(view["available_months"], ["2026-07"])

    def test_admin_sees_everyone_and_totals(self) -> None:
        report = {
            "instance": "enshrouded-primary", "month": "2026-07", "available_months": ["2026-07"],
            "currency": "USD", "run_cost_per_hour": 0.18,
            "users": {"alice@ex": {"charge": 0.2}, "bob@ex": {"charge": 0.1}},
            "totals": {"kitty": 0.05},
        }
        view = MODULE.filter_billing_for_actor(report, "admin@ex", is_admin=True)
        self.assertTrue(view["is_admin"])
        self.assertEqual(set(view["users"]), {"alice@ex", "bob@ex"})
        self.assertEqual(view["totals"]["kitty"], 0.05)
        self.assertIsNone(view["you"])  # admin themselves did not play


if __name__ == "__main__":
    unittest.main()
