from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]


def _load_module(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BILLING = _load_module("tools/billing.py", "billing")

SCHEDULE = {1: 1.5, 2: 1.2, 3: 1.0, 4: 0.85}
DEFAULT_M = 0.75
BASE = datetime(2026, 7, 18, 20, 0, 0, tzinfo=UTC)


def samples(rows):
    """rows: (minute_offset, [logins]) or (minute_offset, [logins], count) -> ledger sample dicts.

    A 2-tuple omits ``count`` entirely, standing in for a pre-2026-08 ledger record; a 3-tuple
    supplies the game's own client count, with ``None`` meaning the meter was blind that cycle.
    """
    out = []
    for row in rows:
        m, present = row[0], row[1]
        sample = {"ts_dt": BASE + timedelta(minutes=m), "instance": "enshrouded-primary", "present": sorted(present)}
        if len(row) > 2:
            sample["count"] = row[2]
            sample["count_known"] = row[2] is not None
        out.append(sample)
    return out


def report(rows, rate=3600.0, interval=60, max_gap=150):
    # rate 3600/hr => exactly $1.00 per second of run time, making arithmetic transparent.
    return BILLING.compute_report(
        samples(rows), rate_per_hour=rate, schedule=SCHEDULE, default_multiplier=DEFAULT_M,
        sample_interval_s=interval, max_gap_s=max_gap,
    )


class MultiplierTests(unittest.TestCase):
    def test_solo_is_a_premium_and_groups_are_discounted(self) -> None:
        self.assertEqual(BILLING.multiplier(1, SCHEDULE, DEFAULT_M), 1.5)
        self.assertEqual(BILLING.multiplier(3, SCHEDULE, DEFAULT_M), 1.0)
        self.assertEqual(BILLING.multiplier(4, SCHEDULE, DEFAULT_M), 0.85)

    def test_beyond_schedule_uses_the_floor(self) -> None:
        self.assertEqual(BILLING.multiplier(9, SCHEDULE, DEFAULT_M), 0.75)

    def test_zero_players_costs_nothing(self) -> None:
        self.assertEqual(BILLING.multiplier(0, SCHEDULE, DEFAULT_M), 0.0)


class SoloVsGroupTests(unittest.TestCase):
    def test_solo_player_pays_full_premium(self) -> None:
        # One 60s interval, alice alone: 60s * $1/s * m(1)=1.5 = $90.
        result = report([(0, ["alice"]), (1, [])])
        self.assertAlmostEqual(result["users"]["alice"]["charge"], 90.0, places=2)
        self.assertEqual(result["users"]["alice"]["solo_pct"], 100.0)
        self.assertAlmostEqual(result["totals"]["kitty"], 30.0, places=2)  # charged 90 vs 60 actual

    def test_four_players_split_and_are_subsidized(self) -> None:
        result = report([(0, ["a", "b", "c", "d"]), (1, [])])
        # each: 60 * 1 * 0.85/4 = 12.75; group total 51 < 60 actual cost.
        for login in ("a", "b", "c", "d"):
            self.assertAlmostEqual(result["users"][login]["charge"], 12.75, places=2)
            self.assertEqual(result["users"][login]["solo_pct"], 0.0)
        self.assertAlmostEqual(result["totals"]["charged"], 51.0, places=2)
        self.assertAlmostEqual(result["totals"]["kitty"], -9.0, places=2)

    def test_solo_share_of_playtime_reported(self) -> None:
        # alice solo for one interval, then alice+bob for one interval.
        result = report([(0, ["alice"]), (1, ["alice", "bob"]), (2, [])])
        # player-hours: alice 120s + bob 60s = 180s; solo 60s -> 33.3%.
        self.assertEqual(result["totals"]["solo_share_pct"], 33.3)


class SessionAndTimeTests(unittest.TestCase):
    def test_hours_and_session_windows(self) -> None:
        rows = [(0, ["alice"]), (1, ["alice"]), (2, ["alice"]), (3, [])]
        result = report(rows)
        self.assertAlmostEqual(result["users"]["alice"]["hours"], 3 * 60 / 3600, places=4)
        sessions = result["users"]["alice"]["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["start"], "2026-07-18T20:00:00Z")
        self.assertEqual(sessions[0]["end"], "2026-07-18T20:03:00Z")

    def test_rejoining_creates_two_sessions(self) -> None:
        rows = [(0, ["alice"]), (1, []), (2, ["alice"]), (3, [])]
        result = report(rows)
        self.assertEqual(len(result["users"]["alice"]["sessions"]), 2)

    def test_max_group_recorded_per_session(self) -> None:
        rows = [(0, ["alice"]), (1, ["alice", "bob", "cara"]), (2, ["alice"]), (3, [])]
        result = report(rows)
        session = result["users"]["alice"]["sessions"][0]
        self.assertEqual(session["max_group"], 3)


class GapAndEdgeTests(unittest.TestCase):
    def test_long_gap_is_capped_not_counted_as_continuous_play(self) -> None:
        # 60 min gap between two alice samples must not bill an hour of phantom play.
        rows = [(0, ["alice"]), (60, ["alice"]), (61, [])]
        result = report(rows, max_gap=150)
        # first sample capped at 150s, second at 60s interval => 210s total (not a phantom hour).
        self.assertAlmostEqual(result["users"]["alice"]["hours"], round(210 / 3600, 3), places=3)

    def test_empty_ledger_is_safe(self) -> None:
        result = report([])
        self.assertEqual(result["users"], {})
        self.assertEqual(result["totals"]["charged"], 0.0)
        self.assertIsNone(result["period"]["start"])


class LedgerParsingTests(unittest.TestCase):
    def test_load_ledger_filters_instance_and_bad_lines(self) -> None:
        import tempfile
        content = "\n".join([
            '{"ts":"2026-07-18T20:00:00Z","instance":"enshrouded-primary","present":["alice"]}',
            'not json',
            '{"instance":"enshrouded-primary"}',  # missing ts
            '{"ts":"2026-07-18T20:01:00Z","instance":"valheim-primary","present":["bob"]}',
            '{"ts":"2026-07-18T20:02:00Z","instance":"enshrouded-primary","present":["a","a","b"]}',
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write(content)
            path = Path(handle.name)
        rows = BILLING.load_ledger(path, instance="enshrouded-primary")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["present"], ["a", "b"])  # deduped + sorted


class MonthTests(unittest.TestCase):
    def test_month_bounds_including_december_rollover(self) -> None:
        start, end = BILLING.month_bounds("2026-07")
        self.assertEqual((start.year, start.month, start.day), (2026, 7, 1))
        self.assertEqual((end.year, end.month), (2026, 8))
        _, dec_end = BILLING.month_bounds("2026-12")
        self.assertEqual((dec_end.year, dec_end.month), (2027, 1))

    def test_available_months_and_filter(self) -> None:
        rows = samples([(0, ["alice"])])
        rows[0]["ts_dt"] = datetime(2026, 6, 30, 23, 59, tzinfo=UTC)
        rows.append({"ts_dt": datetime(2026, 7, 1, 0, 1, tzinfo=UTC), "instance": "enshrouded-primary", "present": ["bob"]})
        self.assertEqual(BILLING.available_months(rows), ["2026-06", "2026-07"])
        july = BILLING.filter_by_month(rows, "2026-07")
        self.assertEqual([s["present"] for s in july], [["bob"]])

    def test_build_report_scopes_to_month_and_lists_months(self) -> None:
        import json
        import tempfile
        lines = [
            {"ts": "2026-06-15T20:00:00Z", "instance": "enshrouded-primary", "present": ["alice"]},
            {"ts": "2026-07-10T20:00:00Z", "instance": "enshrouded-primary", "present": ["alice", "bob"]},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write("\n".join(json.dumps(row) for row in lines))
            ledger = Path(handle.name)
        config = {"currency": "USD", "instances": {"enshrouded-primary": {"run_cost_per_hour": 0.18}}, "multiplier_schedule": {1: 1.5, 2: 1.2}, "default_multiplier": 0.75}
        report = BILLING.build_report(ledger, config, "enshrouded-primary", month="2026-06")
        self.assertEqual(report["month"], "2026-06")
        self.assertIn("2026-06", report["available_months"])
        self.assertIn("2026-07", report["available_months"])
        self.assertIn("alice", report["users"])
        self.assertNotIn("bob", report["users"])  # bob only played in July

    def test_missing_ledger_is_empty_not_an_error(self) -> None:
        self.assertEqual(BILLING.load_ledger(Path("/nonexistent/presence.jsonl")), [])


if __name__ == "__main__":
    unittest.main()


class AuthoritativeCountTests(unittest.TestCase):
    """The game's own client count -- not how many of them we named -- sets the group size.

    Regression cover for the 2026-08 mis-billing: Enshrouded logged three connected clients for
    ~2.3h on 2026-08-23 while the meter could name at most one, and billing read the short
    ``present`` list as solo play and applied the m(1)=1.5 premium.
    """

    def test_group_of_three_is_not_billed_as_solo_when_only_one_is_named(self) -> None:
        result = report([(0, ["alice"], 3), (1, [], 0)])
        alice = result["users"]["alice"]
        # 60s in a group of three: m(3)=1.0 split three ways, not m(1)=1.5 to alice alone.
        self.assertAlmostEqual(alice["charge"], 60.0 * 1.0 / 3, places=2)
        self.assertEqual(alice["solo_hours"], 0.0)
        self.assertAlmostEqual(alice["group_hours"], 60.0 / 3600.0, places=3)

    def test_unnamed_players_are_reported_not_hidden(self) -> None:
        totals = report([(0, ["alice"], 3), (1, [], 0)])["totals"]
        # Two of the three clients went unnamed for the full 60s interval.
        self.assertAlmostEqual(totals["unattributed_player_hours"], 2 * 60.0 / 3600.0, places=3)
        self.assertAlmostEqual(totals["unbilled"], 2 * 60.0 * 1.0 / 3, places=2)

    def test_a_blind_cycle_is_not_read_as_nobody_playing(self) -> None:
        totals = report([(0, [], None), (1, [], 0)])["totals"]
        self.assertAlmostEqual(totals["meter_blind_hours"], 60.0 / 3600.0, places=3)
        self.assertEqual(totals["actual_cost"], 0.0)
        self.assertEqual(totals["charged"], 0.0)

    def test_legacy_records_without_a_count_keep_the_old_reading(self) -> None:
        # A 2-tuple writes no "count" key at all, as pre-2026-08 ledger lines have.
        alice = report([(0, ["alice"]), (1, [], 0)])["users"]["alice"]
        self.assertAlmostEqual(alice["charge"], 60.0 * 1.5, places=2)
        self.assertAlmostEqual(alice["solo_hours"], 60.0 / 3600.0, places=3)

    def test_count_never_shrinks_the_group_below_who_was_named(self) -> None:
        # ledger_admin --remove-login can leave count > len(present); the reverse would be a bug,
        # and must not divide a charge by fewer people than the ledger actually names.
        alice = report([(0, ["alice", "bob"], 1), (1, [], 0)])["users"]["alice"]
        self.assertAlmostEqual(alice["charge"], 60.0 * 1.2 / 2, places=2)


class LedgerCountFieldTests(unittest.TestCase):
    def test_load_ledger_reads_and_validates_count(self) -> None:
        import json, tempfile
        rows = [
            {"ts": "2026-08-23T19:11:00Z", "instance": "i", "present": ["a"], "count": 3},
            {"ts": "2026-08-23T19:12:00Z", "instance": "i", "present": [], "count": None},
            {"ts": "2026-08-23T19:13:00Z", "instance": "i", "present": ["a"]},
            {"ts": "2026-08-23T19:14:00Z", "instance": "i", "present": [], "count": "3"},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
            path = Path(fh.name)
        loaded = BILLING.load_ledger(path, instance="i")
        path.unlink()
        self.assertEqual([s["count"] for s in loaded], [3, None, None, None])
        # Only the explicit null and the malformed string are "blind"; a missing key is legacy.
        self.assertEqual([s["count_known"] for s in loaded], [True, False, True, False])


class CombinedBillingTests(unittest.TestCase):
    """One bill across several games, with each player's hours broken out per game."""

    CONFIG = {"currency": "USD", "sample_interval_seconds": 60, "max_gap_seconds": 150,
              "multiplier_schedule": SCHEDULE, "default_multiplier": DEFAULT_M,
              "instances": {"enshrouded-primary": {"run_cost_per_hour": 3600.0},
                            "valheim-primary": {"run_cost_per_hour": 3600.0}}}

    def _ledger(self, rows):
        import json, tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for minute, instance, present, count in rows:
            ts = (BASE + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
            handle.write(json.dumps({"ts": ts, "instance": instance,
                                     "present": present, "count": count}) + "\n")
        handle.close()
        return Path(handle.name)

    ROWS = [(0, "enshrouded-primary", ["alice"], 1), (1, "enshrouded-primary", [], 0),
            (0, "valheim-primary", ["alice", "bob"], 2), (1, "valheim-primary", [], 0)]

    def test_a_player_gets_a_row_per_game_plus_a_total(self) -> None:
        path = self._ledger(self.ROWS)
        report = BILLING.build_combined_report(path, self.CONFIG, "2026-07")
        path.unlink()
        alice = report["users"]["alice"]
        self.assertEqual(set(alice["per_game"]), {"enshrouded-primary", "valheim-primary"})
        self.assertAlmostEqual(alice["hours"], alice["per_game"]["enshrouded-primary"]["hours"]
                               + alice["per_game"]["valheim-primary"]["hours"], places=3)

    def test_each_game_is_costed_on_its_own_multiplier(self) -> None:
        # alice solos enshrouded (m(1)=1.5) and duos valheim (m(2)=1.2 split two ways). Her combined
        # charge is the sum of the two, never a re-derivation from combined hours.
        path = self._ledger(self.ROWS)
        report = BILLING.build_combined_report(path, self.CONFIG, "2026-07")
        path.unlink()
        alice = report["users"]["alice"]
        self.assertAlmostEqual(alice["per_game"]["enshrouded-primary"]["charge"], 60.0 * 1.5, places=2)
        self.assertAlmostEqual(alice["per_game"]["valheim-primary"]["charge"], 60.0 * 1.2 / 2, places=2)
        self.assertAlmostEqual(alice["charge"],
                               alice["per_game"]["enshrouded-primary"]["charge"]
                               + alice["per_game"]["valheim-primary"]["charge"], places=2)

    def test_idle_slots_are_left_out(self) -> None:
        # The meter writes a sample per configured slot every cycle whether or not it is running.
        rows = self.ROWS + [(0, "valheim-secondary", [], 0), (1, "valheim-secondary", [], 0)]
        path = self._ledger(rows)
        report = BILLING.build_combined_report(path, self.CONFIG, "2026-07")
        path.unlink()
        self.assertNotIn("valheim-secondary", report["instances"])

    def test_totals_sum_across_games(self) -> None:
        path = self._ledger(self.ROWS)
        report = BILLING.build_combined_report(path, self.CONFIG, "2026-07")
        path.unlink()
        self.assertEqual(report["totals"]["game_count"], 2)
        self.assertEqual(report["totals"]["player_count"], 2)
        per_game_cost = sum(sub["totals"]["actual_cost"] for sub in report["instances"].values())
        self.assertAlmostEqual(report["totals"]["actual_cost"], per_game_cost, places=2)

    def test_a_game_with_no_configured_rate_is_flagged_not_silently_free(self) -> None:
        config = {**self.CONFIG, "instances": {"enshrouded-primary": {"run_cost_per_hour": 3600.0}}}
        path = self._ledger(self.ROWS)
        report = BILLING.build_combined_report(path, config, "2026-07")
        rendered = BILLING.render_combined_text(report)
        path.unlink()
        self.assertFalse(report["instances"]["valheim-primary"]["rate_configured"])
        self.assertTrue(report["instances"]["enshrouded-primary"]["rate_configured"])
        self.assertIn("valheim-primary", rendered)
        self.assertIn("WARNING", rendered)

    def test_every_catalog_slot_has_a_rate_in_the_shipped_config(self) -> None:
        import yaml
        catalog = yaml.safe_load((REPO_ROOT / "deploy/etc/game-server-interface/catalog.yaml").read_text())
        config = yaml.safe_load((REPO_ROOT / "deploy/etc/game-server-interface/billing.yaml").read_text())
        priced = set(config.get("instances") or {})
        slots = {f"{template}-{slot}"
                 for template, body in (catalog.get("templates") or {}).items()
                 for slot in ((body.get("instance_policy") or {}).get("slots") or {})}
        self.assertEqual(slots - priced, set(), "catalog slot(s) with no run_cost_per_hour")
