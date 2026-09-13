from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADMIN = _load("tools/ledger_admin.py", "ledger_admin")


class RemoveLoginTests(unittest.TestCase):
    def _lines(self, *present_lists):
        return [json.dumps({"ts": f"t{i}", "instance": "enshrouded-primary", "present": p})
                for i, p in enumerate(present_lists)]

    def test_removes_login_and_counts_affected(self) -> None:
        lines = self._lines(["a@ex", "hlotyaks@github"], ["a@ex"], ["hlotyaks@github"])
        out, changed = ADMIN.remove_login(lines, "hlotyaks@github")
        self.assertEqual(changed, 2)
        presents = [json.loads(l)["present"] for l in out]
        self.assertEqual(presents, [["a@ex"], ["a@ex"], []])  # emptied sample kept as []

    def test_no_op_when_login_absent(self) -> None:
        lines = self._lines(["a@ex"], ["b@ex"])
        out, changed = ADMIN.remove_login(lines, "hlotyaks@github")
        self.assertEqual(changed, 0)
        self.assertEqual([json.loads(l)["present"] for l in out], [["a@ex"], ["b@ex"]])

    def test_atomic_rewrite_preserves_other_fields_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "presence.jsonl"
            ledger.write_text("\n".join(self._lines(["a@ex", "x@ex"], ["x@ex"])) + "\n")
            ledger.chmod(0o600)
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_main(ADMIN, ["--remove-login", "x@ex", "--ledger", str(ledger)])
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in ledger.read_text().splitlines()]
            self.assertEqual([r["present"] for r in rows], [["a@ex"], []])
            self.assertEqual(rows[0]["instance"], "enshrouded-primary")  # untouched field survives
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)


def _run_main(module, argv):
    import sys
    saved = sys.argv
    sys.argv = ["ledger_admin.py", *argv]
    try:
        return module.main()
    finally:
        sys.argv = saved


if __name__ == "__main__":
    unittest.main()


class ClearMonthTests(unittest.TestCase):
    """Retiring a month of untrustworthy capture, without asserting the server sat idle."""

    LINES = [
        json.dumps({"ts": "2026-07-31T23:59:00Z", "instance": "enshrouded-primary", "present": ["a@ex"]}),
        json.dumps({"ts": "2026-08-23T19:11:00Z", "instance": "enshrouded-primary", "present": ["a@ex"], "count": 1}),
        json.dumps({"ts": "2026-08-23T19:12:00Z", "instance": "enshrouded-primary", "present": []}),
        json.dumps({"ts": "2026-08-23T19:13:00Z", "instance": "valheim-primary", "present": ["b@ex"]}),
        json.dumps({"ts": "2026-09-01T00:00:00Z", "instance": "enshrouded-primary", "present": ["a@ex"]}),
    ]

    def test_marks_only_the_named_instance_month_blind(self) -> None:
        out, changed = ADMIN.clear_month(self.LINES, "2026-08", "enshrouded-primary")
        self.assertEqual(changed, 2)
        rows = [json.loads(line) for line in out]
        # August enshrouded rows are blind: unknown, not "nobody played".
        self.assertEqual([r["present"] for r in rows[1:3]], [[], []])
        self.assertEqual([r["count"] for r in rows[1:3]], [None, None])
        # July, September, and the other game are untouched.
        self.assertEqual(rows[0]["present"], ["a@ex"])
        self.assertEqual(rows[3]["present"], ["b@ex"])
        self.assertEqual(rows[4]["present"], ["a@ex"])

    def test_no_line_is_deleted(self) -> None:
        out, _ = ADMIN.clear_month(self.LINES, "2026-08", "enshrouded-primary")
        self.assertEqual(len(out), len(self.LINES))

    def test_rerun_is_a_no_op(self) -> None:
        once, first = ADMIN.clear_month(self.LINES, "2026-08", "enshrouded-primary")
        _, second = ADMIN.clear_month(once, "2026-08", "enshrouded-primary")
        self.assertEqual((first, second), (2, 0))

    def test_month_prefix_does_not_match_a_neighbouring_month(self) -> None:
        # "2026-08" must not match 2026-08 only by string prefix of e.g. "2026-080"; the trailing
        # dash anchors it to a real date component.
        lines = [json.dumps({"ts": "2026-08-01T00:00:00Z", "instance": "i", "present": ["a"]}),
                 json.dumps({"ts": "2026-081-01T00:00:00Z", "instance": "i", "present": ["a"]})]
        out, changed = ADMIN.clear_month(lines, "2026-08", "i")
        self.assertEqual(changed, 1)
        self.assertEqual(json.loads(out[1])["present"], ["a"])
