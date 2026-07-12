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

    def test_untrusted_header_is_not_used_as_actor(self) -> None:
        previous = MODULE.TRUSTED_ACTOR_HEADER
        MODULE.TRUSTED_ACTOR_HEADER = False
        try:
            self.assertEqual(MODULE.request_actor({"X-Game-Interface-Actor": "spoofed"}), "local-loopback")
        finally:
            MODULE.TRUSTED_ACTOR_HEADER = previous

    def test_trusted_actor_header_is_bounded(self) -> None:
        previous = MODULE.TRUSTED_ACTOR_HEADER
        MODULE.TRUSTED_ACTOR_HEADER = True
        try:
            self.assertEqual(MODULE.request_actor({"X-Game-Interface-Actor": "alice@example.test"}), "alice@example.test")
            self.assertEqual(MODULE.request_actor({"X-Game-Interface-Actor": "x" * 257}), "tailnet-unattributed")
        finally:
            MODULE.TRUSTED_ACTOR_HEADER = previous


if __name__ == "__main__":
    unittest.main()
