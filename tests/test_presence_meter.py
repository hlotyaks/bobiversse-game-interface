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


# --- tailscale source (the default): identity + traffic-rate presence ------------------

TAILSCALE_STATUS = {
    "Self": {"UserID": 1, "TailscaleIPs": ["100.84.161.38"]},
    "User": {
        "1": {"LoginName": "chris@ex"},
        "2": {"LoginName": "gamer@ex"},
        "3": {"LoginName": "viewer@ex"},
        "4": {"LoginName": "offline@ex"},
    },
    "Peer": {
        "a": {"UserID": 2, "Active": True, "RxBytes": 7_000_000, "TxBytes": 11_000_000, "TailscaleIPs": ["100.84.161.40"]},
        "b": {"UserID": 3, "Active": True, "RxBytes": 400_000, "TxBytes": 100_000, "TailscaleIPs": ["100.84.161.55"]},
        "c": {"UserID": 4, "Active": False, "RxBytes": 0, "TxBytes": 0, "TailscaleIPs": ["100.84.161.99"]},
    },
}


class TailscaleSourceTests(unittest.TestCase):
    def test_parse_status_peers_merges_and_flags_active(self) -> None:
        peers = METER.parse_status_peers(TAILSCALE_STATUS)
        self.assertEqual(peers["gamer@ex"], {"bytes": 18_000_000, "active": True})
        self.assertEqual(peers["viewer@ex"], {"bytes": 500_000, "active": True})
        self.assertEqual(peers["offline@ex"]["active"], False)
        self.assertNotIn("chris@ex", peers)  # Self is excluded

    def test_playing_uses_rate_not_just_active(self) -> None:
        current = METER.parse_status_peers(TAILSCALE_STATUS)
        # Over the last 60s the gamer added ~2 MB (~266 kbps -> playing) while the dashboard viewer
        # added ~20 KB (~2.7 kbps -> not playing), even though both are Active.
        previous = {"gamer@ex": 16_000_000, "viewer@ex": 480_000}
        playing = METER.playing_logins(current, previous, dt=60.0, min_kbps=25.0)
        self.assertEqual(playing, ["gamer@ex"])

    def test_playing_orders_by_rate_desc(self) -> None:
        current = {"a@ex": {"bytes": 5_000_000, "active": True}, "b@ex": {"bytes": 2_000_000, "active": True}}
        previous = {"a@ex": 1_000_000, "b@ex": 1_000_000}  # +4MB vs +1MB
        self.assertEqual(METER.playing_logins(current, previous, dt=60.0, min_kbps=10.0), ["a@ex", "b@ex"])

    def test_first_sample_reset_and_zero_dt_emit_nobody(self) -> None:
        current = METER.parse_status_peers(TAILSCALE_STATUS)
        self.assertEqual(METER.playing_logins(current, {}, dt=60.0, min_kbps=25.0), [])                     # no prior sample
        self.assertEqual(METER.playing_logins(current, {"gamer@ex": 99_000_000}, dt=60.0, min_kbps=1.0), [])  # counter reset -> negative delta
        self.assertEqual(METER.playing_logins(current, {"gamer@ex": 0}, dt=0.0, min_kbps=1.0), [])            # no elapsed time


# --- conntrack source (preserved for a future non-Tailscale deployment) ----------------

CONNTRACK = "\n".join([
    "udp 17 29 src=100.84.161.40 dst=100.84.161.38 sport=51000 dport=15636 [UNREPLIED] src=172.19.0.2 dst=172.19.0.1 sport=15636 dport=51000 mark=0 use=1",
    "udp 17 25 src=100.84.161.55 dst=100.84.161.38 sport=52000 dport=15636 mark=0 use=1",
    "udp 17 10 src=8.8.8.8 dst=100.84.161.38 sport=40000 dport=15636 mark=0 use=1",
    "udp 17 20 src=100.84.161.40 dst=100.84.161.38 sport=51000 dport=27015 mark=0 use=1",
])


class ConntrackSourceTests(unittest.TestCase):
    def test_extracts_only_tailnet_peers_on_the_game_port(self) -> None:
        self.assertEqual(METER.parse_conntrack_peers(CONNTRACK, 15636), {"100.84.161.40", "100.84.161.55"})
        self.assertEqual(METER.parse_conntrack_peers(CONNTRACK, 27015), {"100.84.161.40"})

    def test_tailnet_range_check(self) -> None:
        self.assertTrue(METER.is_tailnet_ip("100.84.161.40"))
        self.assertFalse(METER.is_tailnet_ip("8.8.8.8"))
        self.assertFalse(METER.is_tailnet_ip("nonsense"))

    def test_ip_login_map_and_resolve(self) -> None:
        mapping = METER.build_ip_login_map(TAILSCALE_STATUS)
        self.assertEqual(mapping["100.84.161.40"], "gamer@ex")
        present = METER.resolve_present(METER.parse_conntrack_peers(CONNTRACK, 15636), mapping)
        self.assertEqual(present, ["gamer@ex", "viewer@ex"])


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
