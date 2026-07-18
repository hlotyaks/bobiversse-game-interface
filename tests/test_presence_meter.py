from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
CATALOG = REPO_ROOT / "deploy/etc/game-server-interface/catalog.yaml"


def _load_module(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


METER = _load_module("tools/presence_meter.py", "presence_meter")

# Two tailnet clients (100.x) talking to the game port, plus noise: a non-tailnet source, and a
# flow to a different port that must be ignored.
CONNTRACK = "\n".join([
    "udp      17 29 src=100.84.161.40 dst=100.84.161.38 sport=51000 dport=15636 [UNREPLIED] src=172.19.0.2 dst=172.19.0.1 sport=15636 dport=51000 mark=0 use=1",
    "udp      17 25 src=100.84.161.55 dst=100.84.161.38 sport=52000 dport=15636 src=172.19.0.2 dst=100.84.161.38 sport=15636 dport=52000 mark=0 use=1",
    "udp      17 10 src=8.8.8.8 dst=100.84.161.38 sport=40000 dport=15636 mark=0 use=1",
    "udp      17 20 src=100.84.161.40 dst=100.84.161.38 sport=51000 dport=27015 mark=0 use=1",
])

TAILSCALE_STATUS = {
    "Self": {"UserID": 1, "TailscaleIPs": ["100.84.161.38", "fd7a::1"]},
    "User": {
        "1": {"LoginName": "chris@github"},
        "2": {"LoginName": "alice@github"},
        "3": {"LoginName": "bob@github"},
    },
    "Peer": {
        "nodeA": {"UserID": 2, "TailscaleIPs": ["100.84.161.40", "fd7a::2"]},
        "nodeB": {"UserID": 3, "TailscaleIPs": ["100.84.161.55"]},
    },
}


class ConntrackParsingTests(unittest.TestCase):
    def test_extracts_only_tailnet_peers_on_the_game_port(self) -> None:
        peers = METER.parse_conntrack_peers(CONNTRACK, 15636)
        self.assertEqual(peers, {"100.84.161.40", "100.84.161.55"})

    def test_other_ports_are_ignored(self) -> None:
        self.assertEqual(METER.parse_conntrack_peers(CONNTRACK, 27015), {"100.84.161.40"})

    def test_tailnet_range_check(self) -> None:
        self.assertTrue(METER.is_tailnet_ip("100.84.161.40"))
        self.assertFalse(METER.is_tailnet_ip("8.8.8.8"))
        self.assertFalse(METER.is_tailnet_ip("nonsense"))


class IdentityMapTests(unittest.TestCase):
    def test_maps_ipv4_to_login_for_self_and_peers(self) -> None:
        mapping = METER.build_ip_login_map(TAILSCALE_STATUS)
        self.assertEqual(mapping["100.84.161.40"], "alice@github")
        self.assertEqual(mapping["100.84.161.55"], "bob@github")
        self.assertEqual(mapping["100.84.161.38"], "chris@github")
        self.assertNotIn("fd7a::2", mapping)  # IPv6 skipped

    def test_resolve_present_sorts_and_falls_back_for_unknown_ip(self) -> None:
        mapping = METER.build_ip_login_map(TAILSCALE_STATUS)
        present = METER.resolve_present({"100.84.161.40", "100.84.161.55", "100.99.99.99"}, mapping)
        self.assertEqual(present, ["alice@github", "bob@github", "ip:100.99.99.99"])

    def test_end_to_end_sample_shape(self) -> None:
        mapping = METER.build_ip_login_map(TAILSCALE_STATUS)
        present = METER.resolve_present(METER.parse_conntrack_peers(CONNTRACK, 15636), mapping)
        self.assertEqual(present, ["alice@github", "bob@github"])


class CatalogPortTests(unittest.TestCase):
    def test_instance_ports_from_real_catalog(self) -> None:
        import yaml
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        ports = METER.instance_ports(catalog)
        self.assertEqual(ports["enshrouded-primary"], 15636)
        self.assertEqual(ports["enshrouded-secondary"], 15640)
        self.assertEqual(ports["valheim-primary"], 2456)


if __name__ == "__main__":
    unittest.main()
