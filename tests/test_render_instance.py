from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[1]
CATALOG = REPO_ROOT / "deploy/etc/game-server-interface/catalog.yaml"


def _load_module(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDER = _load_module("tools/render_instance.py", "render_instance")


class RenderEnshroudedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = RENDER.render(CATALOG, "enshrouded", "primary", "100.84.161.38")
        self.compose = yaml.safe_load(self.files["compose.yaml"])
        self.service = self.compose["services"]["server"]

    def test_compose_pins_catalog_image_digest(self) -> None:
        self.assertEqual(
            self.service["image"],
            "sknnr/enshrouded-dedicated-server@sha256:269698c5ae61c4cbf01b9ea8473e84b4ff0b98c843842c60ee6a0a22fca0786e",
        )

    def test_compose_uses_fixed_container_uid_and_savegame_bind(self) -> None:
        self.assertEqual(self.service["user"], "10000:10000")
        binds = [v for v in self.service["volumes"] if v["target"] == "/home/steam/enshrouded/savegame"]
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["source"], "/srv/games/enshrouded-primary/savegame")

    def test_ports_are_bound_to_the_tailnet_ip_only(self) -> None:
        self.assertEqual(
            self.service["ports"],
            ["100.84.161.38:15636:15636/udp", "100.84.161.38:15637:15637/udp"],
        )
        # The connect port is the lower reserved port; STEAM_PORT is not consumed by the
        # image, so it must not appear in the environment (Steam query stays on 27015).
        self.assertEqual(self.service["environment"]["PORT"], "15636")
        self.assertNotIn("STEAM_PORT", self.service["environment"])

    def test_resource_limits_come_from_the_catalog(self) -> None:
        self.assertEqual(self.service["mem_limit"], "6144m")
        self.assertEqual(self.service["cpus"], 4.0)
        self.assertEqual(self.service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", self.service["security_opt"])

    def test_secret_is_referenced_by_env_file_not_inlined(self) -> None:
        self.assertEqual(
            self.service["env_file"],
            ["/etc/game-server-interface/instances/enshrouded-primary/enshrouded.env"],
        )
        self.assertNotIn("SERVER_PASSWORD", self.files["compose.yaml"])

    def test_unit_carries_catalog_systemd_limits_and_paths(self) -> None:
        unit = self.files["game-enshrouded-primary.service"]
        self.assertIn("CPUQuota=400%", unit)
        self.assertIn("MemoryMax=6144M", unit)
        self.assertIn("TimeoutStartSec=1200s", unit)
        self.assertIn(
            "--file /etc/game-server-interface/instances/enshrouded-primary/compose.yaml up",
            unit,
        )
        self.assertIn("Requires=docker.service", unit)

    def test_rejects_unknown_or_unadapted_templates(self) -> None:
        with self.assertRaises(ValueError):
            RENDER.render(CATALOG, "enshrouded", "unapproved", "100.84.161.38")
        with self.assertRaises(ValueError):
            RENDER.render(CATALOG, "valheim", "primary", "100.84.161.38")  # no adapter yet


if __name__ == "__main__":
    unittest.main()
