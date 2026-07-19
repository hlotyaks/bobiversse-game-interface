from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(enshrouded["connection"], {"hostname": "bobiverse.tail40344b.ts.net", "ip": "100.84.161.38", "protocol": "udp"})
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

    def test_log_reader_bounds_tail_and_redacts_journal_output(self) -> None:
        instance = self.controller.register_instance("valheim", "primary")
        completed = subprocess.CompletedProcess([], 0, "2026-07-17 password=not-safe\nready\n", "")
        with patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            logs = self.controller.read_logs(instance, 999)
        self.assertEqual(logs["lines"], ["2026-07-17 password=<redacted>", "ready"])
        self.assertEqual(run.call_args.args[0][-1], str(MODULE.MAX_LOG_LINES))
        self.assertEqual(run.call_args.kwargs["timeout"], 20)

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

    def test_billing_action_returns_per_user_report(self) -> None:
        root = Path(self.temporary.name)
        ledger = root / "presence.jsonl"
        ledger.write_text(
            '{"ts":"2026-07-10T20:00:00Z","instance":"enshrouded-primary","present":["alice@ex"]}\n'
            '{"ts":"2026-07-10T20:01:00Z","instance":"enshrouded-primary","present":["alice@ex","bob@ex"]}\n'
        )
        billing_config = root / "billing.yaml"
        billing_config.write_text(MODULE.yaml.safe_dump({
            "currency": "USD",
            "instances": {"enshrouded-primary": {"run_cost_per_hour": 0.18}},
            "multiplier_schedule": {1: 1.5, 2: 1.2},
            "default_multiplier": 0.75,
        }))
        controller = MODULE.Controller(
            self.catalog, root / "state2/instances.json", root / "log2/audit.jsonl",
            presence_ledger=ledger, billing_config=billing_config,
            billing_module=Path(__file__).parents[1] / "tools" / "billing.py",
        )
        response = controller.dispatch(
            {"action": "billing", "actor": "alice@ex", "template_id": "enshrouded", "instance_id": "primary", "month": "2026-07"},
            peer_uid=995,
        )
        report = response["result"]
        self.assertEqual(report["instance"], "enshrouded-primary")
        self.assertEqual(report["month"], "2026-07")
        self.assertEqual(set(report["users"]), {"alice@ex", "bob@ex"})

    def test_billing_rejects_a_malformed_month(self) -> None:
        with self.assertRaises(MODULE.ControllerError):
            self.controller.billing_report("enshrouded", "primary", "2026-99")

    def test_game_request_is_audited_without_creating_instance_state(self) -> None:
        response = self.controller.dispatch(
            {"action": "create_game_request", "actor": "admin@example.test", "steam_app_id": 1203620, "requested_slug": "enshrouded"},
            peer_uid=995,
        )
        self.assertEqual(response["result"]["steam_app_id"], 1203620)
        self.assertEqual(self.controller.instances, {})
        audit = self.controller.audit_path.read_text(encoding="utf-8")
        self.assertIn('"action":"create_game_request"', audit)
        self.assertIn('"result":"accepted"', audit)

    def test_game_request_rejects_unsafe_values(self) -> None:
        with self.assertRaises(MODULE.ControllerError):
            self.controller.create_game_request(True, "enshrouded")
        with self.assertRaises(MODULE.ControllerError):
            self.controller.create_game_request(1203620, "Enshrouded")


if __name__ == "__main__":
    unittest.main()
