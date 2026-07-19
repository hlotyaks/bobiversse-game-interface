from __future__ import annotations

import importlib.util
import unittest
import unittest.mock
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


# --- game-authoritative occupancy (Enshrouded 'Machines:' block) -----------------------

def _machines_block(ts: str, *clients: str) -> str:
    lines = [f"[I {ts}] -------------- Session ----------------",
             f"[I {ts}] Machines:",
             f"[I {ts}]   m#0(128): up 0 (0), down 0 (0), remote 0 (0), limit 256, lost 0, ping 0 ms, EstablishingBaseline"]
    lines += [f"[I {ts}]   {c}" for c in clients]
    lines.append(f"[I {ts}] ---------------------------------------")
    return "\n".join(lines)


THREE_PLAYERS = _machines_block(
    "24:57:50,595",
    "m#1(1281): up 149 (171), down 23 (25), remote 149 (167), limit 600, lost 262, ping 53 ms, OperatingNormally",
    "m#2(898): up 138 (150), down 29 (30), remote 139 (149), limit 1,393, lost 49, ping 44 ms, OperatingNormally",
    "m#3(1155): up 136 (151), down 28 (31), remote 135 (149), limit 558, lost 182, ping 47 ms, OperatingNormally",
)
NO_PLAYERS = _machines_block("25:46:21,326")  # only the server's own EstablishingBaseline entry


class EnshroudedOccupancyTests(unittest.TestCase):
    def test_counts_only_operating_clients(self) -> None:
        self.assertEqual(METER.enshrouded_client_count(THREE_PLAYERS), 3)

    def test_empty_block_reports_zero_not_none(self) -> None:
        self.assertEqual(METER.enshrouded_client_count(NO_PLAYERS), 0)

    def test_uses_the_last_complete_block(self) -> None:
        # A player leaves between blocks: the latest complete block wins (3 -> 1).
        one = _machines_block("25:00:20,642", "m#2(898): up 68 (70), down 24 (26), ping 44 ms, OperatingNormally")
        self.assertEqual(METER.enshrouded_client_count(THREE_PLAYERS + "\n" + one), 1)

    def test_incomplete_trailing_block_is_ignored(self) -> None:
        partial = "\n".join(["[I 25:00:20,642] -------------- Session ----------------",
                             "[I 25:00:20,642] Machines:",
                             "[I 25:00:20,642]   m#1(1): up 1 (1), down 1 (1), ping 40 ms, OperatingNormally"])
        # No closing rule yet -> fall back to the last complete block (3), not the partial one.
        self.assertEqual(METER.enshrouded_client_count(THREE_PLAYERS + "\n" + partial), 3)

    def test_no_block_is_unknown(self) -> None:
        self.assertIsNone(METER.enshrouded_client_count("[I 00:00:01,000] [server] Saved\n"))


class AttributionTests(unittest.TestCase):
    def test_ewma_ranks_by_smoothed_rate(self) -> None:
        current = {"a@ex": {"bytes": 5_000_000}, "b@ex": {"bytes": 2_000_000}, "c@ex": {"bytes": 1_010_000}}
        previous = {"a@ex": 1_000_000, "b@ex": 1_000_000, "c@ex": 1_000_000}  # +4MB, +1MB, +10KB
        ewma = METER.update_rate_ewma({}, current, previous, dt=60.0, alpha=0.5)
        self.assertEqual([login for _, login in METER.rank_by_smoothed_rate(ewma)], ["a@ex", "b@ex", "c@ex"])

    def test_ewma_keeps_a_steady_player_ahead_through_a_counter_reset(self) -> None:
        # The regression: a solo player (a@ex) whose tailscale counter resets for one cycle must not
        # yield its slot to an idle-but-active bystander (b@ex) that happens to tick up that cycle.
        steady = {"a@ex": 30.0, "b@ex": 0.0}  # a@ex has a strong smoothed lead
        current = {"a@ex": {"bytes": 500}, "b@ex": {"bytes": 2_000_000}}   # a@ex reset (< prior), b@ex +little
        previous = {"a@ex": 9_000_000, "b@ex": 1_990_000}
        ewma = METER.update_rate_ewma(steady, current, previous, dt=60.0, alpha=0.5)
        top = METER.attribute_by_count(METER.rank_by_smoothed_rate(ewma), 1, floor_kbps=1.0)
        self.assertEqual(top, ["a@ex"])  # steady player retained despite its reset

    def test_ewma_reset_decays_not_drops(self) -> None:
        # A reset counts as 0 for the cycle (halved at alpha=0.5), not removed from the ranking.
        ewma = METER.update_rate_ewma({"a@ex": 20.0}, {"a@ex": {"bytes": 1}}, {"a@ex": 999}, dt=60.0, alpha=0.5)
        self.assertAlmostEqual(ewma["a@ex"], 10.0, places=6)

    def test_attribute_takes_top_n_by_count(self) -> None:
        ranked = [(300.0, "a@ex"), (120.0, "b@ex"), (60.0, "c@ex")]
        self.assertEqual(METER.attribute_by_count(ranked, 2, floor_kbps=1.0), ["a@ex", "b@ex"])
        self.assertEqual(METER.attribute_by_count(ranked, 0, floor_kbps=1.0), [])

    def test_attribute_drops_idle_peers_below_floor(self) -> None:
        # Game says 3 clients but only two peers have real traffic -> under-report, never invent one.
        ranked = [(300.0, "a@ex"), (120.0, "b@ex"), (0.2, "idle@ex")]
        self.assertEqual(METER.attribute_by_count(ranked, 3, floor_kbps=1.0), ["a@ex", "b@ex"])


class ExcludeLoginTests(unittest.TestCase):
    def test_excluded_admin_never_gets_a_game_slot(self) -> None:
        # An active dashboard-only admin (hlotyaks) out-traffics the sole real player this cycle; with
        # exclusion the game's single slot still goes to the player, not the admin.
        import tempfile, json, yaml
        from pathlib import Path
        status = {"User": {"2": {"LoginName": "player@ex"}, "9": {"LoginName": "hlotyaks@github"}},
                  "Peer": {"p": {"UserID": 2, "Active": True, "RxBytes": 1_100_000, "TxBytes": 0},
                           "a": {"UserID": 9, "Active": True, "RxBytes": 9_000_000, "TxBytes": 0}}}
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {"player@ex": 1_000_000, "hlotyaks@github": 1_000_000}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value=json.dumps(status)), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=1), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          exclude_logins=frozenset({"hlotyaks@github"}))
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        primary = [r for r in rows if r["instance"] == "enshrouded-primary"]
        self.assertTrue(primary and primary[0]["present"] == ["player@ex"])
        self.assertNotIn("hlotyaks@github", [u for r in rows for u in r["present"]])


class CatalogPortTests(unittest.TestCase):
    def test_instance_ports_from_real_catalog(self) -> None:
        import yaml
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        ports = METER.instance_ports(catalog)
        self.assertEqual(ports["enshrouded-primary"], 15636)
        self.assertEqual(ports["enshrouded-secondary"], 15640)
        self.assertEqual(ports["valheim-primary"], 2456)

    def test_instance_templates_from_real_catalog(self) -> None:
        import yaml
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        templates = METER.instance_templates(catalog)
        self.assertEqual(templates["enshrouded-primary"], "enshrouded")
        self.assertEqual(templates["valheim-primary"], "valheim")


if __name__ == "__main__":
    unittest.main()
