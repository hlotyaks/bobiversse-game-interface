from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tools" / "backup_scheduler.py"
SPEC = importlib.util.spec_from_file_location("backup_scheduler", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BackupSchedulerTests(unittest.TestCase):
    def test_status_write_is_atomic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "backup_status.json"
            payload = {"schema_version": 1, "instances": {"enshrouded:primary": {"verification_passed": True}}}
            MODULE.write_status(path, payload)
            self.assertEqual(json.loads(path.read_text()), payload)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_command_result_is_bounded_to_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / "backup"
            command.write_text("#!/bin/sh\nprintf 'backup output'\n", encoding="utf-8")
            command.chmod(0o755)
            succeeded, detail = MODULE.run(str(command), "enshrouded-primary")
            self.assertTrue(succeeded)
            self.assertEqual(detail, "backup output")


if __name__ == "__main__":
    unittest.main()
