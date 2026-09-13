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


def recent(seconds_ago: float = 1.0) -> str:
    """A LastWrite timestamp ``seconds_ago`` in the past.

    Attribution keys off write-recency, so run_cycle fixtures need a live timestamp rather than a
    frozen literal; ordering between peers is what each test is actually asserting.
    """
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


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
                  "Peer": {"p": {"UserID": 2, "Active": True, "RxBytes": 1_100_000, "TxBytes": 0,
                                 "LastWrite": recent(2)},
                           "a": {"UserID": 9, "Active": True, "RxBytes": 9_000_000, "TxBytes": 0,
                                 "LastWrite": recent(1)}}}
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {"player@ex": 1_000_000, "hlotyaks@github": 1_000_000}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value=json.dumps(status)), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=1), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          attribution="last-write",
                                          exclude_logins=frozenset({"hlotyaks@github"}))
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        primary = [r for r in rows if r["instance"] == "enshrouded-primary"]
        self.assertTrue(primary and primary[0]["present"] == ["player@ex"])
        self.assertNotIn("hlotyaks@github", [u for r in rows for u in r["present"]])


class PerGameExclusionTests(unittest.TestCase):
    def test_load_exclusions_parses_map(self) -> None:
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ex.json"
            path.write_text(json.dumps({"schema_version": 1, "exclusions": {"enshrouded": ["hlotyaks@github"]}}))
            loaded = METER.load_exclusions(path)
        self.assertEqual(loaded, {"enshrouded": frozenset({"hlotyaks@github"})})

    def test_load_exclusions_missing_or_malformed_is_empty(self) -> None:
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "absent.json"
            self.assertEqual(METER.load_exclusions(missing), {})
            bad = Path(d) / "bad.json"
            bad.write_text("{not json")
            self.assertEqual(METER.load_exclusions(bad), {})
            wrong = Path(d) / "wrong.json"
            wrong.write_text(json.dumps({"exclusions": ["not", "a", "map"]}))
            self.assertEqual(METER.load_exclusions(wrong), {})

    def test_per_game_exclusion_only_affects_its_game(self) -> None:
        # hlotyaks out-traffics the real player and is excluded from enshrouded but NOT valheim.
        # The enshrouded slot must go to the player; if valheim had a reader, hlotyaks could still take it.
        import tempfile, json, yaml
        from pathlib import Path
        status = {"User": {"2": {"LoginName": "player@ex"}, "9": {"LoginName": "hlotyaks@github"}},
                  "Peer": {"p": {"UserID": 2, "Active": True, "RxBytes": 1_100_000, "TxBytes": 0,
                                 "LastWrite": recent(2)},
                           "a": {"UserID": 9, "Active": True, "RxBytes": 9_000_000, "TxBytes": 0,
                                 "LastWrite": recent(1)}}}
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {"player@ex": 1_000_000, "hlotyaks@github": 1_000_000}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value=json.dumps(status)), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=1), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          attribution="last-write",
                                          template_exclusions={"enshrouded": frozenset({"hlotyaks@github"})})
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        primary = [r for r in rows if r["instance"] == "enshrouded-primary"]
        self.assertTrue(primary and primary[0]["present"] == ["player@ex"])
        # hlotyaks is not globally dropped -- he remains a ranked peer available to other games.
        self.assertIn("hlotyaks@github", state["bytes"])


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


class OccupancyUnknownTests(unittest.TestCase):
    """A game that reports its own occupancy must never fall back to the bandwidth heuristic.

    Regression cover for the 2026-08 phantom billing: when ``docker logs`` returned nothing for
    a cycle, ``instance_client_count`` gave None, the meter dropped to ``--min-kbps 25``, and the
    only peer on this host that clears 25 kbps is an admin's SSH or dashboard session -- which
    was then billed as solo play the game never saw.
    """

    STATUS = {"User": {"2": {"LoginName": "player@ex"}, "9": {"LoginName": "admin@ex"}},
              "Peer": {"p": {"UserID": 2, "Active": True, "RxBytes": 1_040_000, "TxBytes": 0,
                             "LastWrite": recent(2)},
                       "a": {"UserID": 9, "Active": True, "RxBytes": 9_000_000, "TxBytes": 0,
                             "LastWrite": recent(1)}}}

    def _cycle(self, count):
        import tempfile, json, yaml
        from pathlib import Path
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {"player@ex": 1_000_000, "admin@ex": 1_000_000}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value=json.dumps(self.STATUS)), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=count), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          attribution="last-write")
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        return [r for r in rows if r["instance"] == "enshrouded-primary"][0]

    def test_unreadable_occupancy_records_unknown_not_the_top_talker(self) -> None:
        record = self._cycle(None)
        self.assertEqual(record["present"], [])
        self.assertIsNone(record["count"])  # explicit unknown, distinct from "count": 0

    def test_a_readable_count_still_attributes(self) -> None:
        record = self._cycle(1)
        self.assertEqual(record["present"], ["admin@ex"])
        self.assertEqual(record["count"], 1)

    def test_ledger_records_the_game_count_alongside_the_names(self) -> None:
        # The game says three; only two peers clear the floor. Both facts must survive to billing.
        record = self._cycle(3)
        self.assertEqual(record["count"], 3)
        self.assertEqual(len(record["present"]), 2)

    def test_a_game_without_a_reader_still_uses_the_fallback(self) -> None:
        self.assertTrue(METER.has_occupancy_reader("enshrouded"))
        # valheim gained readers on 2026-09-13; use a template that genuinely has none.
        self.assertFalse(METER.has_occupancy_reader("no-such-game"))
        import tempfile, json, yaml
        from pathlib import Path
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {"player@ex": 1_000_000, "admin@ex": 1_000_000}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value=json.dumps(self.STATUS)), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0)
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        # Every catalogued template now has readers, so the bandwidth fallback is exercised
        # directly rather than through a slot: run_cycle only reaches it for a template with none.
        self.assertEqual(METER.playing_logins(
            {"admin@ex": {"bytes": 9_000_000, "active": True},
             "player@ex": {"bytes": 1_040_000, "active": True}},
            {"admin@ex": 1_000_000, "player@ex": 1_000_000}, dt=60.0, min_kbps=25.0),
            ["admin@ex"])


class WriteRecencyAttributionTests(unittest.TestCase):
    """Identity by LastWrite -- the signal that survives DERP relaying.

    Byte counters are populated only for peers with a direct path, so a relayed player reads 0 and
    byte-rate ranking cannot see them. That is what emptied the ledger through the 2026-08-23
    session while the game logged three connected clients for 2.3h.
    """

    def _status(self, peers):
        """peers: [(login, seconds_since_last_write, curaddr)] -> a tailscale status --json shape.

        Ages are relative to now, never literal timestamps: attribution compares LastWrite against
        the clock, so hardcoded dates silently stop meaning "recent" as soon as time passes.
        """
        users = {str(i): {"LoginName": login} for i, (login, _, _) in enumerate(peers)}
        nodes = {}
        for i, (_, age_s, curaddr) in enumerate(peers):
            nodes[f"n{i}"] = {"UserID": i, "LastWrite": recent(age_s), "CurAddr": curaddr,
                              "Online": True, "Relay": "nyc", "RxBytes": 0, "TxBytes": 0}
        return {"User": users, "Peer": nodes}

    def test_relayed_players_are_visible_where_byte_rate_saw_nothing(self) -> None:
        # Three connected clients, all DERP-relayed, all with zero byte counters.
        status = self._status([
            ("alice@ex", 1, ""),
            ("bob@ex", 2, ""),
            ("cara@ex", 3, ""),
            ("lurker@ex", 90_000, ""),
        ])
        paths = METER.parse_peer_paths(status)
        named = METER.attribute_by_write_recency(paths, count=3, max_age_s=120.0)
        self.assertEqual(named, ["alice@ex", "bob@ex", "cara@ex"])
        # The superseded signal names nobody: every relayed peer reports zero bytes.
        peers = METER.parse_status_peers(status)
        ewma = METER.update_rate_ewma({}, peers, {login: 0 for login in peers}, dt=60.0, alpha=0.5)
        ranked = METER.rank_by_smoothed_rate(ewma)
        self.assertEqual(METER.attribute_by_count(ranked, 3, floor_kbps=1.0), [])

    def test_a_stale_peer_is_never_selected(self) -> None:
        # Game says two, but only one peer has been written to recently -- under-report, never
        # reach back in time for a second name.
        status = self._status([("alice@ex", 1, ""), ("lurker@ex", 3600, "")])
        paths = METER.parse_peer_paths(status)
        self.assertEqual(METER.attribute_by_write_recency(paths, 2, max_age_s=120.0), ["alice@ex"])

    def test_never_written_peer_is_skipped(self) -> None:
        status = self._status([("alice@ex", 1, "")])
        status["User"]["9"] = {"LoginName": "fresh@ex"}
        status["Peer"]["n9"] = {"UserID": 9, "LastWrite": "0001-01-01T00:00:00Z", "CurAddr": ""}
        paths = METER.parse_peer_paths(status)
        self.assertIsNone(paths["fresh@ex"]["last_write_s"])
        self.assertEqual(METER.attribute_by_write_recency(paths, 2, max_age_s=120.0), ["alice@ex"])

    def test_exclusions_pass_the_slot_to_the_next_real_player(self) -> None:
        # The admin is written to most recently (dashboard traffic) but is excluded from this game,
        # so the single slot must fall through to the player rather than be spent on them.
        status = self._status([("admin@ex", 1, "1.2.3.4:41641"), ("player@ex", 2, "")])
        paths = METER.parse_peer_paths(status)
        named = METER.attribute_by_write_recency(paths, 1, max_age_s=120.0,
                                                 excluded=frozenset({"admin@ex"}))
        self.assertEqual(named, ["player@ex"])

    def test_devices_collapse_to_the_most_recently_written(self) -> None:
        status = {"User": {"1": {"LoginName": "alice@ex"}},
                  "Peer": {"old": {"UserID": 1, "LastWrite": recent(90_000), "CurAddr": ""},
                           "new": {"UserID": 1, "LastWrite": recent(1), "CurAddr": "1.2.3.4:1"}}}
        paths = METER.parse_peer_paths(status)
        self.assertEqual(len(paths), 1)
        self.assertLess(paths["alice@ex"]["last_write_s"], 120.0)
        self.assertTrue(paths["alice@ex"]["direct"])

    def test_zero_count_names_nobody(self) -> None:
        status = self._status([("alice@ex", 1, "")])
        paths = METER.parse_peer_paths(status)
        self.assertEqual(METER.attribute_by_write_recency(paths, 0, max_age_s=120.0), [])


class GameLogIdentityTests(unittest.TestCase):
    """Identity from the game's own log -- the source every network heuristic was standing in for.

    Enshrouded logs a Steam ID per connected peer. The premise the earlier design rested on ("the
    server does not log player identity") was wrong, and no network source could have worked anyway:
    players reach the server over Steam's relay network, so the published UDP port sees no traffic.
    """

    LOG = "\n".join([
        "[I 385:29:18,005] [online] Session accepted with peer (steamid:111)",
        "[I 385:29:18,005] [online] Added peer 0(23) (steamid:111)",
        "[E 385:29:18,405] [online] Begin auth session with peer 0(23)",
        "[I 385:29:19,698] [online] Client '111' authenticated by steam",
        "[I 385:40:00,000] [online] Added peer 0(24) (steamid:222)",
        "[I 385:50:00,000] [online] Added peer 1(3) (steamid:333)",
        "[I 386:31:52,952] [online] Disconnecting peer 0(23)",
        "[I 386:31:52,952] [online] Removed peer 0(23)",
        "[I 386:40:00,000] [online] Timeout for peer 1(3)",
    ])

    def test_replays_add_and_drop_to_who_is_connected_now(self) -> None:
        self.assertEqual(METER.enshrouded_connected_players(self.LOG), ["222"])

    def test_timeout_drops_a_peer_like_removal(self) -> None:
        self.assertNotIn("333", METER.enshrouded_connected_players(self.LOG))

    def test_empty_log_names_nobody(self) -> None:
        self.assertEqual(METER.enshrouded_connected_players(""), [])

    def test_the_same_player_reconnecting_is_counted_once(self) -> None:
        log = "\n".join([
            "[online] Added peer 0(1) (steamid:111)",
            "[online] Removed peer 0(1)",
            "[online] Added peer 0(2) (steamid:111)",
        ])
        self.assertEqual(METER.enshrouded_connected_players(log), ["111"])

    def test_two_devices_of_one_player_collapse(self) -> None:
        log = "\n".join(["[online] Added peer 0(1) (steamid:111)",
                         "[online] Added peer 0(2) (steamid:111)"])
        self.assertEqual(METER.enshrouded_connected_players(log), ["111"])

    def test_identity_map_loading(self) -> None:
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ids.json"
            path.write_text(json.dumps({"identities": {
                "111": {"name": "Alice", "login": "alice@ex"},
                "222": {"name": "Bob"},          # no login: billed, but no personal dashboard line
                "333": "Cara",                   # bare string: name only, back-compatible
                "444": {"login": "d@ex"},        # no name: unusable, must be dropped
                "555": 5,
            }}))
            loaded = METER.load_player_identities(path)
        self.assertEqual(loaded, {"111": {"name": "Alice", "login": "alice@ex", "characters": {}},
                                  "222": {"name": "Bob", "login": "", "characters": {}},
                                  "333": {"name": "Cara", "login": "", "characters": {}}})

    def test_missing_identity_map_is_empty_not_an_error(self) -> None:
        from pathlib import Path
        self.assertEqual(METER.load_player_identities(Path("/nonexistent/ids.json")), {})

    def test_unmapped_player_is_counted_but_not_named(self) -> None:
        # The game says two connected; only one Steam ID is mapped. The named one is billed at the
        # group rate for two, and the other surfaces as UNATTRIBUTED rather than being guessed.
        import tempfile, json, yaml
        from pathlib import Path
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value="{}"), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=2), \
                 unittest.mock.patch.object(METER, "instance_connected_players", return_value=["111", "999"]), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          identities={"111": {"name": "Alice", "login": "alice@ex"}})
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        primary = [r for r in rows if r["instance"] == "enshrouded-primary"][0]
        self.assertEqual(primary["present"], ["Alice"])
        self.assertEqual(primary["count"], 2)

    def test_exclusion_by_login_still_works_against_game_names(self) -> None:
        # The dashboard's Exclusions page only accepts tailnet logins, so excluding by login must
        # drop the matching player even though the ledger names them by their in-game name.
        import tempfile, json, yaml
        from pathlib import Path
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value="{}"), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=2), \
                 unittest.mock.patch.object(METER, "instance_connected_players", return_value=["111", "222"]), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          identities={"111": {"name": "Alice", "login": "alice@ex"},
                                                      "222": {"name": "Admin", "login": "admin@ex"}},
                                          template_exclusions={"enshrouded": frozenset({"admin@ex"})})
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        primary = [r for r in rows if r["instance"] == "enshrouded-primary"][0]
        self.assertEqual(primary["present"], ["Alice"])
        self.assertEqual(primary["count"], 2)  # still counted, just not named

    def test_exclusions_still_apply_to_game_log_identity(self) -> None:
        import tempfile, json, yaml
        from pathlib import Path
        catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            state = {"bytes": {}, "rate_ewma": {}, "t": 0.0}
            with unittest.mock.patch.object(METER, "_run", return_value="{}"), \
                 unittest.mock.patch.object(METER, "is_unit_active", return_value=True), \
                 unittest.mock.patch.object(METER, "instance_client_count", return_value=2), \
                 unittest.mock.patch.object(METER, "instance_connected_players", return_value=["111", "222"]), \
                 unittest.mock.patch.object(METER.time, "monotonic", return_value=60.0):
                METER.run_cycle_tailscale(catalog, ledger, "ts", "sc", "dk", state, 25.0,
                                          identities={"111": {"name": "Alice", "login": "alice@ex"},
                                                      "222": {"name": "Admin", "login": "admin@ex"}},
                                          template_exclusions={"enshrouded": frozenset({"Admin"})})
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
        primary = [r for r in rows if r["instance"] == "enshrouded-primary"][0]
        self.assertEqual(primary["present"], ["Alice"])


class RepositoryPrivacyTests(unittest.TestCase):
    """Real player identities are host state, never repository content.

    This repository is public. The tracked identity file is a template: the live mapping lives at
    /var/lib/game-server-interface/player-identities.json, root-owned 0600. Committing real Steam
    IDs or tailnet logins here would publish other people's accounts and email addresses
    irreversibly, so guard it rather than relying on remembering.
    """

    SEED = REPO_ROOT / "deploy/var/lib/game-server-interface/player-identities.json"

    def test_the_tracked_identity_map_carries_no_real_players(self) -> None:
        import json
        payload = json.loads(self.SEED.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("identities"), {},
                         "real identities must live on the host, not in this public repository")

    def test_no_real_looking_credentials_in_tracked_files(self) -> None:
        import re, subprocess
        # A Steam ID that is not one of the documented example values, or a personal-looking
        # address, in anything tracked here.
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                                 capture_output=True, text=True).stdout.split()
        steam = re.compile(r"7656119\d{10}")
        offenders = []
        for name in tracked:
            path = REPO_ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for found in steam.findall(text):
                # Example IDs are the 7656119000000000N block reserved for documentation.
                if not found.startswith("765611900000000"):
                    offenders.append(f"{name}: {found}")
        self.assertEqual(offenders, [], f"real-looking Steam IDs in tracked files: {offenders}")


class ValheimReaderTests(unittest.TestCase):
    """Valheim names a player on arrival but not on departure.

    Only the ZDO owner id appears on both sides, so an arrival's Steam ID has to be bound to the
    owner id that follows it. Patterns taken from a real session log, not assumed.
    """

    JOIN = [
        "PlayFab listen socket child connected to remote player 4DD2042DF9031F97",
        'Player joined server "RhadWorld" that has join code 079236, now 1 player(s)',
        "PlayFab socket with remote ID playfab/4DD2042DF9031F97 received local Platform ID Steam_111",
        "Got character ZDOID from Rhad : 643715480:1",
    ]
    LEAVE = [
        "RPC_Disconnect",
        "Destroying abandoned non persistent zdo 643715480:4 owner 643715480",
        'Player connection lost server "RhadWorld" that has join code 079236, now 0 player(s)',
    ]

    def test_count_comes_from_the_servers_own_running_total(self) -> None:
        self.assertEqual(METER.valheim_client_count("\n".join(self.JOIN)), 1)
        self.assertEqual(METER.valheim_client_count("\n".join(self.JOIN + self.LEAVE)), 0)

    def test_count_is_unknown_when_no_line_carries_one(self) -> None:
        self.assertIsNone(METER.valheim_client_count("Placed location WoodHouse6 in zone 0,-8"))

    def test_a_connected_player_is_named_by_steam_id(self) -> None:
        self.assertEqual(METER.valheim_connected_players("\n".join(self.JOIN)), ["111"])

    def test_departure_is_matched_through_the_zdo_owner(self) -> None:
        self.assertEqual(METER.valheim_connected_players("\n".join(self.JOIN + self.LEAVE)), [])

    def test_reconnecting_under_a_new_owner_id_still_resolves(self) -> None:
        rejoin = [
            "PlayFab socket with remote ID playfab/4DD2042DF9031F97 received local Platform ID Steam_111",
            "Got character ZDOID from Rhad : 999:1",
            'Player joined server "RhadWorld" that has join code 079236, now 1 player(s)',
        ]
        self.assertEqual(METER.valheim_connected_players("\n".join(self.JOIN + self.LEAVE + rejoin)), ["111"])

    SIMULTANEOUS = [
        "PlayFab socket with remote ID playfab/AAA received local Platform ID Steam_111",
        "PlayFab socket with remote ID playfab/BBB received local Platform ID Steam_222",
        "Got character ZDOID from Rhad : 500:1",
        "Got character ZDOID from Gronk : 501:1",
        'Player joined server "W" that has join code 1, now 2 player(s)',
    ]

    def test_recorded_characters_resolve_simultaneous_arrivals(self) -> None:
        # The ~20s gap between an arrival and its character means a group starting together
        # interleaves as a matter of course. Knowing the character names settles it outright.
        named = METER.valheim_connected_players(
            "\n".join(self.SIMULTANEOUS), {"Rhad": "111", "Gronk": "222"})
        self.assertEqual(named, ["111", "222"])

    def test_recognising_one_player_disambiguates_the_other(self) -> None:
        # Only Rhad is recorded. Striking his arrival leaves exactly one candidate for Gronk's
        # character, so both end up named without any ordering assumption.
        named = METER.valheim_connected_players("\n".join(self.SIMULTANEOUS), {"Rhad": "111"})
        self.assertEqual(named, ["111", "222"])

    def test_a_recorded_character_is_matched_on_departure_too(self) -> None:
        lines = self.SIMULTANEOUS + ["Destroying abandoned non persistent zdo 500:2 owner 500"]
        named = METER.valheim_connected_players("\n".join(lines), {"Rhad": "111", "Gronk": "222"})
        self.assertEqual(named, ["222"])

    def test_an_unrecorded_character_does_not_borrow_anothers_identity(self) -> None:
        # A stranger joining alongside a known player must not be named as anyone.
        lines = ["PlayFab socket with remote ID playfab/AAA received local Platform ID Steam_111",
                 "PlayFab socket with remote ID playfab/ZZZ received local Platform ID Steam_999",
                 "Got character ZDOID from Stranger : 700:1",
                 "Got character ZDOID from Rhad : 500:1"]
        named = METER.valheim_connected_players("\n".join(lines), {"Rhad": "111"})
        self.assertEqual(named, ["111"])

    def test_simultaneous_arrivals_are_left_unnamed_rather_than_transposed(self) -> None:
        # Two arrivals pending when a character appears: the pairing is ambiguous, and billing the
        # wrong person is worse than billing nobody. The game's count still reports them, so they
        # reach the bill as UNATTRIBUTED.
        text = "\n".join(self.SIMULTANEOUS)
        self.assertEqual(METER.valheim_connected_players(text), [])
        self.assertEqual(METER.valheim_client_count(text), 2)  # still counted

    def test_an_empty_server_clears_any_drift(self) -> None:
        # "now 0 player(s)" is authoritative, so a stuck entry cannot outlive the session.
        stuck = ["PlayFab socket with remote ID playfab/AAA received local Platform ID Steam_111",
                 "Got character ZDOID from Rhad : 500:1",
                 'Player connection lost server "W" that has join code 1, now 0 player(s)']
        self.assertEqual(METER.valheim_connected_players("\n".join(stuck)), [])

    def test_two_players_joining_separately_are_both_named(self) -> None:
        lines = [
            "PlayFab socket with remote ID playfab/AAA received local Platform ID Steam_111",
            "Got character ZDOID from Rhad : 500:1",
            'Player joined server "W" that has join code 1, now 1 player(s)',
            "PlayFab socket with remote ID playfab/BBB received local Platform ID Steam_222",
            "Got character ZDOID from Gronk : 501:1",
            'Player joined server "W" that has join code 1, now 2 player(s)',
        ]
        self.assertEqual(METER.valheim_connected_players("\n".join(lines)), ["111", "222"])

    def test_one_of_two_leaving_removes_only_that_player(self) -> None:
        lines = [
            "PlayFab socket with remote ID playfab/AAA received local Platform ID Steam_111",
            "Got character ZDOID from Rhad : 500:1",
            "PlayFab socket with remote ID playfab/BBB received local Platform ID Steam_222",
            "Got character ZDOID from Gronk : 501:1",
            "Destroying abandoned non persistent zdo 500:2 owner 500",
            'Player connection lost server "W" that has join code 1, now 1 player(s)',
        ]
        self.assertEqual(METER.valheim_connected_players("\n".join(lines)), ["222"])


class CharacterIndexTests(unittest.TestCase):
    """Per-game character names in the identity map, used to name players a game only names by
    their character."""

    IDS = {
        "111": {"name": "Rhadamanthus", "login": "a@ex",
                "characters": {"valheim": ["Rhad"], "enshrouded": ["Rhadamanthus"]}},
        "222": {"name": "Gronk", "login": "b@ex", "characters": {"valheim": ["Gronk", "Gronk2"]}},
        "333": {"name": "NoChars", "login": "c@ex", "characters": {}},
    }

    def test_index_is_per_game(self) -> None:
        self.assertEqual(METER.character_index(self.IDS, "valheim"),
                         {"Rhad": "111", "Gronk": "222", "Gronk2": "222"})
        self.assertEqual(METER.character_index(self.IDS, "enshrouded"), {"Rhadamanthus": "111"})

    def test_a_player_may_have_several_characters_in_one_game(self) -> None:
        index = METER.character_index(self.IDS, "valheim")
        self.assertEqual(index["Gronk"], index["Gronk2"])

    def test_a_game_with_no_recorded_characters_yields_an_empty_index(self) -> None:
        self.assertEqual(METER.character_index(self.IDS, "no-such-game"), {})

    def test_characters_survive_loading_from_disk(self) -> None:
        import json, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ids.json"
            path.write_text(json.dumps({"identities": {
                "111": {"name": "Rhadamanthus", "login": "a@ex",
                        "characters": {"valheim": ["Rhad", 5], "enshrouded": "notalist"}},
                "222": "BareString",
            }}))
            loaded = METER.load_player_identities(path)
        self.assertEqual(loaded["111"]["characters"], {"valheim": ["Rhad"]})
        self.assertEqual(loaded["222"], {"name": "BareString", "login": "", "characters": {}})
