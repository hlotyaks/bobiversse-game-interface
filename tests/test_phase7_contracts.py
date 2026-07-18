from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "controller" / "game_controller.py"
SPEC = importlib.util.spec_from_file_location("game_controller_phase7", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PhaseSevenContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.catalog_path = root / "catalog.yaml"
        shutil.copy(Path(__file__).parents[1] / "deploy/etc/game-server-interface/catalog.yaml", self.catalog_path)
        self.controller = MODULE.Controller(self.catalog_path, root / "state/instances.json", root / "log/audit.jsonl")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def catalog(self) -> dict:
        return MODULE.yaml.safe_load(self.catalog_path.read_text(encoding="utf-8"))

    def write_catalog(self, catalog: dict) -> None:
        self.catalog_path.write_text(MODULE.yaml.safe_dump(catalog), encoding="utf-8")

    def test_disabled_template_rejects_registration(self) -> None:
        catalog = self.catalog()
        catalog["templates"]["enshrouded"]["enabled"] = False
        self.write_catalog(catalog)
        with self.assertRaisesRegex(MODULE.ControllerError, "disabled"):
            self.controller.register_instance("enshrouded", "primary")

    def test_template_instance_limit_rejects_second_slot(self) -> None:
        catalog = self.catalog()
        catalog["templates"]["enshrouded"]["instance_policy"]["max_instances"] = 1
        self.write_catalog(catalog)
        self.controller.register_instance("enshrouded", "primary")
        with self.assertRaisesRegex(MODULE.ControllerError, "instance limit"):
            self.controller.register_instance("enshrouded", "secondary")

    def test_memory_and_disk_admission_reject_excess_candidate(self) -> None:
        catalog = self.catalog()
        catalog["capacity_policy"]["admission_limits"]["memory_mib"] = 1
        self.write_catalog(catalog)
        memory_candidate = self.controller.resolve_slot("enshrouded", "primary")
        self.assertFalse(self.controller.admission(memory_candidate)["allowed"])

        catalog = self.catalog()
        catalog["capacity_policy"]["host_safety_reserve"]["disk_gib"] = 10**9
        self.write_catalog(catalog)
        disk_candidate = self.controller.resolve_slot("enshrouded", "primary")
        self.assertFalse(self.controller.admission(disk_candidate)["allowed"])


if __name__ == "__main__":
    unittest.main()
