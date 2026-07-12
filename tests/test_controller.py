from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "controller" / "game_controller.py"
SPEC = importlib.util.spec_from_file_location("game_controller", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ControllerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.catalog = root / "catalog.yaml"
        shutil.copy(Path(__file__).parents[1] / "deploy/etc/game-server-interface/catalog.yaml", self.catalog)
        self.controller = MODULE.Controller(self.catalog, root / "state/instances.json", root / "log/audit.jsonl")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registration_derives_only_allowlisted_values(self) -> None:
        instance = self.controller.register_instance("valheim", "primary")
        self.assertEqual(instance["unit"], "game-valheim-primary.service")
        self.assertEqual(instance["paths"]["instance_data"], "/srv/games/valheim-primary")
        self.assertEqual(instance["ports"], [{"protocol": "udp", "host": 2456, "container": 2456}, {"protocol": "udp", "host": 2457, "container": 2457}])
        self.assertEqual(instance["registration_state"], "pending-provisioning")

    def test_duplicate_or_unknown_registration_is_rejected(self) -> None:
        self.controller.register_instance("enshrouded", "primary")
        with self.assertRaises(MODULE.ControllerError):
            self.controller.register_instance("enshrouded", "primary")
        with self.assertRaises(MODULE.ControllerError):
            self.controller.register_instance("enshrouded", "unapproved")

    def test_secret_redaction_removes_common_value_formats(self) -> None:
        self.assertEqual(
            MODULE.SECRET_PATTERN.sub(r"\1\2<redacted>", "SERVER_PASS=correct-horse"),
            "SERVER_PASS=<redacted>",
        )
        self.assertEqual(
            MODULE.SECRET_PATTERN.sub(r"\1\2<redacted>", "token: abc123"),
            "token:<redacted>",
        )


if __name__ == "__main__":
    unittest.main()
