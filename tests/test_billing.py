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
    """rows: list of (minute_offset, [logins]) -> ledger sample dicts."""
    return [{"ts_dt": BASE + timedelta(minutes=m), "instance": "enshrouded-primary", "present": sorted(p)} for m, p in rows]


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


if __name__ == "__main__":
    unittest.main()
