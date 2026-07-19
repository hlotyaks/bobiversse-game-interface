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
