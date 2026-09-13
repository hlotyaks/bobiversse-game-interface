from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKFILL = _load("tools/backfill_presence.py", "backfill_presence")


def stamp(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 12, 20, minute, second, tzinfo=UTC)


def block(count: int) -> list[str]:
    """A game Session block reporting `count` connected clients (m#0 is the server itself)."""
    lines = ["-------------- Session ----------------", "Machines:",
             "  m#0(128): up 0 (0), ping 0 ms, EstablishingBaseline"]
    lines += [f"  m#{i}(1): up 1 (1), ping 40 ms, OperatingNormally" for i in range(1, count + 1)]
    return lines + ["---------------------------------------"]


IDS = {"111": {"name": "Alice", "login": "alice@ex"},
       "222": {"name": "Bob", "login": "bob@ex"}}


class TimelineTests(unittest.TestCase):
    def test_replays_joins_and_leaves_in_order(self) -> None:
        lines = [
            (stamp(0), "[online] Added peer 0(1) (steamid:111)"),
            (stamp(5), "[online] Added peer 0(2) (steamid:222)"),
            (stamp(9), "[online] Removed peer 0(1)"),
        ]
        timeline = BACKFILL.presence_timeline(lines)
        self.assertEqual([sorted(s) for _, s in timeline],
                         [["111"], ["111", "222"], ["222"]])

    def test_a_drop_for_an_unknown_handle_is_ignored(self) -> None:
        # A session that began before the retained log would otherwise emit a spurious transition.
        lines = [(stamp(0), "[online] Removed peer 9(9)"),
                 (stamp(1), "[online] Added peer 0(1) (steamid:111)")]
        self.assertEqual([sorted(s) for _, s in BACKFILL.presence_timeline(lines)], [["111"]])

    def test_occupancy_comes_from_the_session_block(self) -> None:
        lines = [(stamp(0), text) for text in block(2)]
        self.assertEqual([c for _, c in BACKFILL.occupancy_timeline(lines)], [2])

    def test_value_at_uses_the_most_recent_prior_entry(self) -> None:
        timeline = [(stamp(0), "a"), (stamp(10), "b")]
        self.assertEqual(BACKFILL.value_at(timeline, stamp(5), None), "a")
        self.assertEqual(BACKFILL.value_at(timeline, stamp(15), None), "b")
        self.assertIsNone(BACKFILL.value_at(timeline, datetime(2026, 9, 12, 19, tzinfo=UTC), None))


class SampleBuildingTests(unittest.TestCase):
    def _lines(self):
        lines = [(stamp(0), t) for t in block(0)]
        lines.append((stamp(1), "[online] Added peer 0(1) (steamid:111)"))
        lines.append((stamp(1), "[online] Added peer 0(2) (steamid:222)"))
        lines += [(stamp(2), t) for t in block(2)]
        lines.append((stamp(8), "[online] Removed peer 0(2)"))
        lines += [(stamp(9), t) for t in block(1)]
        return lines

    def test_samples_follow_the_meter_cadence_and_shape(self) -> None:
        samples = BACKFILL.build_samples(self._lines(), "enshrouded-primary", IDS,
                                         frozenset(), 60, None, None)
        self.assertEqual(len(samples), 10)  # 20:00..20:09 inclusive, one per minute
        self.assertEqual(set(samples[0]), {"ts", "instance", "present", "count"})
        self.assertEqual(samples[5]["present"], ["Alice", "Bob"])
        self.assertEqual(samples[5]["count"], 2)

    def test_count_is_the_games_own_not_the_number_named(self) -> None:
        # Bob is unmapped: the group is still two, so billing reports the gap rather than
        # shrinking the group and overcharging Alice at the solo rate.
        samples = BACKFILL.build_samples(self._lines(), "enshrouded-primary",
                                         {"111": {"name": "Alice", "login": ""}},
                                         frozenset(), 60, None, None)
        self.assertEqual(samples[5]["present"], ["Alice"])
        self.assertEqual(samples[5]["count"], 2)

    def test_exclusions_apply_by_login_or_name(self) -> None:
        for excluded in (frozenset({"bob@ex"}), frozenset({"Bob"})):
            samples = BACKFILL.build_samples(self._lines(), "enshrouded-primary", IDS,
                                             excluded, 60, None, None)
            self.assertEqual(samples[5]["present"], ["Alice"], excluded)

    def test_a_since_bound_trims_the_window(self) -> None:
        samples = BACKFILL.build_samples(self._lines(), "enshrouded-primary", IDS,
                                         frozenset(), 60, stamp(5), None)
        self.assertEqual(samples[0]["ts"], "2026-09-12T20:05:00Z")

    def test_departure_is_reflected_in_later_samples(self) -> None:
        samples = BACKFILL.build_samples(self._lines(), "enshrouded-primary", IDS,
                                         frozenset(), 60, None, None)
        self.assertEqual(samples[9]["present"], ["Alice"])
        self.assertEqual(samples[9]["count"], 1)


class SpliceTests(unittest.TestCase):
    EXISTING = [
        json.dumps({"ts": "2026-09-12T19:59:00Z", "instance": "enshrouded-primary", "present": ["Old"], "count": 1}),
        json.dumps({"ts": "2026-09-12T20:03:00Z", "instance": "enshrouded-primary", "present": ["Wrong"], "count": 1}),
        json.dumps({"ts": "2026-09-12T20:03:00Z", "instance": "valheim-primary", "present": ["Keep"], "count": 1}),
        json.dumps({"ts": "2026-09-12T21:00:00Z", "instance": "enshrouded-primary", "present": ["Later"], "count": 1}),
    ]
    NEW = [{"ts": "2026-09-12T20:03:00Z", "instance": "enshrouded-primary", "present": ["Alice"], "count": 1}]

    def _spliced(self):
        return BACKFILL.splice(self.EXISTING, self.NEW, "enshrouded-primary", stamp(2), stamp(4))

    def test_replaces_only_this_instance_inside_the_window(self) -> None:
        rebuilt, replaced = self._spliced()
        self.assertEqual(replaced, 1)
        rows = [json.loads(line) for line in rebuilt]
        present = {(r["instance"], r["ts"]): r["present"] for r in rows}
        self.assertEqual(present[("enshrouded-primary", "2026-09-12T20:03:00Z")], ["Alice"])

    def test_other_instances_and_times_outside_the_window_survive(self) -> None:
        rebuilt, _ = self._spliced()
        rows = [json.loads(line) for line in rebuilt]
        self.assertIn(["Keep"], [r["present"] for r in rows if r["instance"] == "valheim-primary"])
        stamps = {r["ts"] for r in rows if r["instance"] == "enshrouded-primary"}
        self.assertIn("2026-09-12T19:59:00Z", stamps)
        self.assertIn("2026-09-12T21:00:00Z", stamps)

    def test_output_is_sorted_by_timestamp(self) -> None:
        rebuilt, _ = self._spliced()
        stamps = [json.loads(line)["ts"] for line in rebuilt]
        self.assertEqual(stamps, sorted(stamps))

    def test_unparseable_lines_are_preserved_rather_than_dropped(self) -> None:
        rebuilt, _ = BACKFILL.splice(self.EXISTING + ["{not json"], self.NEW,
                                     "enshrouded-primary", stamp(2), stamp(4))
        self.assertIn("{not json", rebuilt)


class RoundTripTests(unittest.TestCase):
    def test_backfilled_records_are_shaped_like_live_meter_records(self) -> None:
        import presence_meter as pm
        lines = [(stamp(0), "[online] Added peer 0(1) (steamid:111)")] + [(stamp(1), t) for t in block(1)]
        samples = BACKFILL.build_samples(lines, "enshrouded-primary", IDS, frozenset(), 60, None, None)
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "p.jsonl"
            for sample in samples:
                pm.append_ledger(ledger, sample)
            billing = _load("tools/billing.py", "billing")
            loaded = billing.load_ledger(ledger, instance="enshrouded-primary")
        self.assertTrue(loaded)
        self.assertTrue(all(s["count_known"] for s in loaded))
        self.assertEqual(loaded[-1]["present"], ["Alice"])
