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
        # A template in the catalog with no Compose adapter must refuse rather than render
        # something generic. (valheim gained an adapter on 2026-09-13; enshrouded and valheim are
        # now both adapted, so this uses a template that is not in the catalog at all.)
        with self.assertRaises(ValueError):
            RENDER.render(CATALOG, "no-such-game", "primary", "100.84.161.38")


if __name__ == "__main__":
    unittest.main()


class ValheimAdapterTests(unittest.TestCase):
    """The Valheim image has a different privilege and storage contract from Enshrouded's.

    It deliberately starts as root -- its bootstrap runs groupmod, rewrites /etc/passwd and
    chowns the data tree before supervisord drops to PUID:PGID -- so a blanket cap_drop:[ALL]
    kills it at startup. These assertions are taken from the pinned image's own bootstrap script.
    """

    def _service(self, instance="primary"):
        rendered = RENDER.render(CATALOG, "valheim", instance, "100.84.161.38")
        return yaml.safe_load(rendered["compose.yaml"])["services"]["server"]

    def test_drops_all_capabilities_then_adds_back_only_what_bootstrap_needs(self) -> None:
        service = self._service()
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(sorted(service["cap_add"]), ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"])
        self.assertIn("no-new-privileges:true", service["security_opt"])

    def test_is_discoverable_so_players_can_actually_join(self) -> None:
        # SERVER_PUBLIC=0 with no crossplay leaves the server unqueryable and undiscoverable:
        # Valheim carries gameplay over Steam's relay rather than the tailnet, so the tailnet-only
        # port binding never gated access in the first place. Entry is gated by SERVER_PASS.
        self.assertEqual(self._service()["environment"]["SERVER_PUBLIC"], "1")

    def test_ports_are_published_only_on_the_tailnet_ip(self) -> None:
        for published in self._service()["ports"]:
            self.assertTrue(str(published).startswith("100.84.161.38:"), published)

    def test_server_port_matches_the_catalog_reservation(self) -> None:
        service = self._service()
        self.assertEqual(service["environment"]["SERVER_PORT"], "2456")
        self.assertIn("100.84.161.38:2456:2456/udp", [str(p) for p in service["ports"]])

    def test_world_directories_stay_traversable(self) -> None:
        # The image chmods entries in worlds_local with WORLDS_FILE_PERMISSIONS (default 0644).
        # Valheim stores each world as a directory, and 0644 on a directory removes the execute
        # bit, so the server cannot open the files inside and hangs before binding its sockets.
        self.assertEqual(self._service()["environment"]["WORLDS_FILE_PERMISSIONS"], "0755")

    def test_both_persistent_paths_are_bound(self) -> None:
        targets = {volume["target"] for volume in self._service()["volumes"]}
        self.assertEqual(targets, {"/config", "/opt/valheim"})

    def test_non_consecutive_ports_are_refused(self) -> None:
        # The server derives its query port as SERVER_PORT+1 and cannot be told otherwise, so a
        # gapped reservation would publish a port nothing listens on.
        resolved = {"template_id": "valheim", "instance_id": "primary",
                    "paths": {"instance_data": "/srv/games/valheim-primary",
                              "compose_project": "game-valheim-primary"},
                    "resource_limits": {"compose": {"mem_limit": "4096m", "cpus": 2}},
                    "image": "img", "image_digest": "sha256:x",
                    "ports": [{"protocol": "udp", "host": 2456}, {"protocol": "udp", "host": 2460}]}
        with self.assertRaises(ValueError):
            RENDER.render_valheim(resolved, "100.84.161.38")

    def test_secondary_slot_renders_on_its_own_ports(self) -> None:
        service = self._service("secondary")
        self.assertEqual(service["environment"]["SERVER_PORT"], "2460")


class ProvisioningProfileTests(unittest.TestCase):
    """Per-template provisioning facts live beside the adapters so the two cannot drift."""

    def test_every_adapter_has_a_provisioning_profile(self) -> None:
        self.assertEqual(set(RENDER.ADAPTERS), set(RENDER.PROVISIONING))

    def test_profiles_name_directories_owner_and_secret_keys(self) -> None:
        for template, profile in RENDER.PROVISIONING.items():
            self.assertTrue(profile["data_dirs"], template)
            self.assertRegex(profile["uid_gid"], r"^\d+:\d+$")
            self.assertTrue(profile["secret_keys"], template)

    def test_valheim_profile_matches_the_paths_the_adapter_binds(self) -> None:
        rendered = RENDER.render(CATALOG, "valheim", "primary", "100.84.161.38")
        sources = {volume["source"].rsplit("/", 1)[-1]
                   for volume in yaml.safe_load(rendered["compose.yaml"])["services"]["server"]["volumes"]}
        self.assertEqual(sources, set(RENDER.PROVISIONING["valheim"]["data_dirs"]))
