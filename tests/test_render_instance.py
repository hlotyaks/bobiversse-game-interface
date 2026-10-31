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
            "mornedhels/enshrouded-server@sha256:689dcb30037b0c63e6929b2fe5951eaf8eca52a43792b2764440baeb012b2aa1",
        )

    def test_runs_as_root_and_drops_to_the_data_owning_uid(self) -> None:
        # This image's bootstrap runs groupmod/usermod and chown -R before supervisord drops
        # privileges, so it must start as root -- a `user:` override breaks it at startup. The
        # uid it drops to has to stay 10000:10000, which owns the existing instance data.
        self.assertNotIn("user", self.service)
        self.assertEqual(self.service["environment"]["PUID"], "10000")
        self.assertEqual(self.service["environment"]["PGID"], "10000")

    def test_savegame_bind_targets_the_image_save_directory(self) -> None:
        binds = [v for v in self.service["volumes"]
                 if v["target"] == "/opt/enshrouded/server/savegame"]
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0]["source"], "/srv/games/enshrouded-primary/savegame")

    def test_ports_are_bound_to_the_tailnet_ip_only(self) -> None:
        self.assertEqual(
            self.service["ports"],
            ["100.84.161.38:15636:15636/udp", "100.84.161.38:15637:15637/udp"],
        )
        # The connect port is the lower reserved port, and it reaches the game only through
        # SERVER_QUERYPORT: this image's SERVER_PORT is deprecated and warns, and the previous
        # image's PORT is not read at all, so neither may appear.
        self.assertEqual(self.service["environment"]["SERVER_QUERYPORT"], "15636")
        self.assertNotIn("SERVER_PORT", self.service["environment"])
        self.assertNotIn("PORT", self.service["environment"])

    def test_player_cap_comes_from_the_catalog_not_the_secret_file(self) -> None:
        self.assertEqual(self.service["environment"]["SERVER_SLOT_COUNT"], "16")
        self.assertNotIn("SERVER_SLOT_COUNT", RENDER.PROVISIONING["enshrouded"]["secret_keys"])

    def test_resource_limits_come_from_the_catalog(self) -> None:
        self.assertEqual(self.service["mem_limit"], "6144m")
        self.assertEqual(self.service["cpus"], 6.0)
        self.assertEqual(self.service["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", self.service["security_opt"])

    def test_only_the_capabilities_bootstrap_and_shutdown_need_are_added_back(self) -> None:
        # Verified against the pinned image's own bootstrap: groupmod/usermod (SETGID, SETUID),
        # chown -R of the data tree (CHOWN, FOWNER, DAC_OVERRIDE). KILL is for stopping, not
        # starting -- see the regression test below. Nothing else.
        self.assertEqual(sorted(self.service["cap_add"]),
                         ["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"])

    def test_secret_is_referenced_by_env_file_not_inlined(self) -> None:
        self.assertEqual(
            self.service["env_file"],
            ["/etc/game-server-interface/instances/enshrouded-primary/enshrouded.env"],
        )
        self.assertNotIn("SERVER_PASSWORD", self.files["compose.yaml"])

    def test_unit_carries_catalog_systemd_limits_and_paths(self) -> None:
        unit = self.files["game-enshrouded-primary.service"]
        self.assertIn("CPUQuota=600%", unit)
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
        self.assertEqual(sorted(service["cap_add"]),
                         ["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"])
        self.assertIn("no-new-privileges:true", service["security_opt"])

    def test_joins_by_code_over_a_relay_not_by_address_or_browser(self) -> None:
        # The ports are published only on the tailnet IP, so Steam's master servers cannot query
        # the server and would never list it -- SERVER_PUBLIC=1 registers a server nobody can find.
        # Crossplay needs no inbound reachability: the server dials out and issues a join code.
        service = self._service()
        self.assertEqual(service["environment"]["CROSSPLAY"], "true")
        self.assertEqual(service["environment"]["SERVER_PUBLIC"], "0")

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


class EnshroudedInstallPersistenceTests(unittest.TestCase):
    """The game install must outlive the container.

    The image ships no install and its entrypoint fetches the whole server from Steam on every
    start. Unmounted, that lands in the container's writable layer, which ExecStop's
    `compose down` destroys, so every restart re-downloaded 8.1GB (measured 2026-10-15).
    """

    def setUp(self) -> None:
        self.service = yaml.safe_load(
            RENDER.render(CATALOG, "enshrouded", "primary", "100.84.161.38")["compose.yaml"]
        )["services"]["server"]

    def _mount(self, target):
        return next((v for v in self.service["volumes"] if v["target"] == target), None)

    def test_the_install_directory_is_persisted(self) -> None:
        install = self._mount("/opt/enshrouded/server")
        self.assertIsNotNone(install, "game install is not bind-mounted; every restart re-downloads")
        self.assertTrue(install["source"].endswith("/server"))
        self.assertFalse(install["read_only"])

    def test_the_savegame_stays_separately_mounted_inside_it(self) -> None:
        # Nested on purpose: the world must survive wiping the install to force a clean re-download.
        savegame = self._mount("/opt/enshrouded/server/savegame")
        self.assertIsNotNone(savegame)
        self.assertTrue(savegame["source"].endswith("/savegame"))

    def test_parent_is_declared_before_the_nested_child(self) -> None:
        # Docker mounts parents first regardless, but declaration order reflects the dependency
        # and keeps the rendered file readable.
        targets = [v["target"] for v in self.service["volumes"]]
        self.assertLess(targets.index("/opt/enshrouded/server"),
                        targets.index("/opt/enshrouded/server/savegame"))

    def test_the_proton_prefix_is_not_mounted_over(self) -> None:
        # Proton creates its prefix under the install path, inside the install mount, so it
        # persists without a mount of its own. Giving it one is what broke the server on
        # 2026-10-15: a bind mount at compatdata hid the directory Proton creates pfx.lock in,
        # and Proton died with FileNotFoundError: .../compatdata/2278520/pfx.lock.
        self.assertIsNone(self._mount("/opt/enshrouded/server/steamapps/compatdata"))
        self.assertIsNone(self._mount("/opt/enshrouded/server/steamapps"))

    def test_provisioning_creates_and_owns_the_install_directory(self) -> None:
        # Without this the mount would land on a root-owned directory the container cannot write.
        profile = RENDER.PROVISIONING["enshrouded"]
        for required in ("server", "savegame"):
            self.assertIn(required, profile["data_dirs"])
        self.assertNotIn("compatdata", profile["data_dirs"])
        self.assertEqual(profile["uid_gid"], "10000:10000")


class SignalCapabilityTests(unittest.TestCase):
    """Both images keep supervisord as root and run the game as an unprivileged uid.

    Signalling across that uid boundary is governed by CAP_KILL, so a capability set chosen by
    asking only "what does startup need?" produces a server that starts fine and can never be
    stopped. That is what shipped on 2026-10-21: `supervisorctl stop` failed with

        CRIT unknown problem killing enshrouded-server (144):
        PermissionError: [Errno 1] Operation not permitted

    which left the program in UNKNOWN state, so the updater's stop -> update -> start sequence
    installed a new build and then could not restart onto it, and `docker compose down` degraded
    to a SIGKILL of the container instead of a clean world save. Enshrouded surfaced it; Valheim
    had the same hole silently. Keep both templates in one test so neither drifts.
    """

    TEMPLATES = ("enshrouded", "valheim")

    def _cap_add(self, template):
        rendered = RENDER.render(CATALOG, template, "primary", "100.84.161.38")
        return yaml.safe_load(rendered["compose.yaml"])["services"]["server"]["cap_add"]

    def test_supervisord_can_signal_the_game_process_it_owns(self) -> None:
        for template in self.TEMPLATES:
            with self.subTest(template=template):
                self.assertIn("KILL", self._cap_add(template))

    def test_the_privilege_drop_capabilities_are_still_present(self) -> None:
        # KILL is an addition, not a replacement: bootstrap still has to chown the data tree and
        # drop to PUID:PGID before the game starts.
        for template in self.TEMPLATES:
            with self.subTest(template=template):
                cap_add = self._cap_add(template)
                for required in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"):
                    self.assertIn(required, cap_add)

    def test_nothing_broader_than_kill_was_added(self) -> None:
        # CAP_KILL only reaches processes in the container's own PID namespace. The temptation
        # when a stop fails with EPERM is SYS_ADMIN or privileged: true -- neither belongs here.
        for template in self.TEMPLATES:
            with self.subTest(template=template):
                cap_add = self._cap_add(template)
                for forbidden in ("SYS_ADMIN", "SYS_PTRACE", "SYS_RESOURCE", "ALL"):
                    self.assertNotIn(forbidden, cap_add)
