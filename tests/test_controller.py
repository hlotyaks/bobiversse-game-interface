from __future__ import annotations

import importlib.util
import shutil
import subprocess
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
        self.assertEqual(instance["resource_limits"]["systemd"]["MemoryMax"], "4096M")

    def test_public_catalog_exposes_connection_without_password_guidance(self) -> None:
        enshrouded = next(item for item in self.controller.public_catalog() if item["template_id"] == "enshrouded")
        self.assertEqual(enshrouded["connection"], {"hostname": "100.84.161.38", "protocol": "udp"})
        self.assertNotIn("password_guidance", enshrouded["connection"])

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

    def test_capacity_policy_rejects_excess_cpu_reservation(self) -> None:
        catalog = MODULE.yaml.safe_load(self.catalog.read_text())
        catalog["capacity_policy"]["admission_limits"]["cpu_cores"] = 1
        self.catalog.write_text(MODULE.yaml.safe_dump(catalog))
        candidate = self.controller.resolve_slot("valheim", "primary")
        admission = self.controller.admission(candidate)
        self.assertFalse(admission["allowed"])
        self.assertTrue(any("CPU reservation" in reason for reason in admission["reasons"]))

    def test_operations_persist_across_controller_restart(self) -> None:
        operation = {"operation_id": "test-operation", "state": "healthy", "completed_at": self.controller.now()}
        self.controller.operations[operation["operation_id"]] = operation
        self.controller._save_operations()
        restored = MODULE.Controller(self.catalog, self.controller.state_path, self.controller.audit_path)
        self.assertEqual(restored.operations["test-operation"]["state"], "healthy")

    def test_failed_service_with_restart_limit_is_crash_loop(self) -> None:
        instance = self.controller.register_instance("enshrouded", "primary")
        original = self.controller._systemctl
        self.controller._systemctl = lambda *_args: subprocess.CompletedProcess(
            _args, 0,
            "LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nNRestarts=3\n",
            "",
        )
        try:
            status = self.controller.service_status(instance)
        finally:
            self.controller._systemctl = original
        self.assertTrue(status["crash_loop"])
        self.assertEqual(status["restart_count_recent"], 3)


if __name__ == "__main__":
    unittest.main()
