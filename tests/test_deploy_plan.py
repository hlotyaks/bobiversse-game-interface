from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DEPLOY_SH = Path(__file__).parents[1] / "scripts" / "deploy.sh"


class DeployPlanTests(unittest.TestCase):
    """The path -> installer mapping in scripts/deploy.sh, tested in a hermetic throwaway repo.

    deploy.sh --dry-run only computes `git diff --name-only` between two refs and prints the plan,
    so we build a tiny git repo with a copy of the script, commit crafted file changes, and assert
    the plan. This keeps the mapping (which decides what runs as root on the host) under test.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "scripts").mkdir()
        shutil.copy(DEPLOY_SH, self.repo / "scripts" / "deploy.sh")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@test")
        self._git("config", "user.name", "t")
        self._commit("README.md", "base")  # a base commit that touches nothing deployable

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def _commit(self, relpath: str, content: str) -> str:
        target = self.repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"touch {relpath}")
        return self._git("rev-parse", "HEAD").strip()

    def _plan(self, old: str, new: str) -> str:
        result = subprocess.run(["bash", str(self.repo / "scripts" / "deploy.sh"), "--dry-run", old, new],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_controller_change_runs_phase6_and_suppresses_phase3(self) -> None:
        old = self._git("rev-parse", "HEAD").strip()
        new = self._commit("controller/game_controller.py", "x")
        plan = self._plan(old, new)
        self.assertIn("install-phase6.sh", plan)
        self.assertNotIn("install-phase3.sh", plan)  # phase6 reinstalls the interface itself

    def test_interface_only_runs_phase3(self) -> None:
        old = self._git("rev-parse", "HEAD").strip()
        new = self._commit("interface/app/static/app.js", "x")
        plan = self._plan(old, new)
        self.assertIn("install-phase3.sh", plan)
        self.assertNotIn("install-phase6.sh", plan)

    def test_meter_and_exclusion_seed_run_metering(self) -> None:
        old = self._git("rev-parse", "HEAD").strip()
        new = self._commit("deploy/var/lib/game-server-interface/presence-exclusions.json", "{}")
        self.assertIn("install-usage-metering.sh", self._plan(old, new))

    def test_catalog_only_runs_catalog(self) -> None:
        old = self._git("rev-parse", "HEAD").strip()
        new = self._commit("deploy/etc/game-server-interface/catalog.yaml", "schema_version: 1")
        plan = self._plan(old, new)
        self.assertIn("catalog", plan)
        self.assertNotIn("phase6", plan)

    def test_docs_only_is_a_noop(self) -> None:
        old = self._git("rev-parse", "HEAD").strip()
        new = self._commit("docs/usage-metering.md", "words")
        self.assertIn("touches nothing", self._plan(old, new))

    def test_identical_refs_are_a_noop(self) -> None:
        head = self._git("rev-parse", "HEAD").strip()
        self.assertIn("touches nothing", self._plan(head, head))


if __name__ == "__main__":
    unittest.main()


class DeployTriggerCoverageTests(unittest.TestCase):
    """Every installed artefact must have a path that triggers its installer.

    A file with no trigger deploys only by accident -- when something else in the same commit
    happens to match -- and otherwise sits stale on the host indefinitely. That is how the
    diagnostics wrapper drifted: it had no trigger at all, so gsi-diagnose stayed at whatever
    version was last installed by hand.
    """

    DEPLOY = DEPLOY_SH.read_text(encoding="utf-8")

    def test_metering_artefacts_trigger_the_meter_installer(self) -> None:
        for path in ("tools/presence_meter.py", "tools/billing.py", "tools/ledger_admin.py",
                     "tools/backfill_presence.py", "scripts/observe-presence.py",
                     "deploy/etc/systemd/system/game-presence-meter.service",
                     "deploy/etc/systemd/system/game-presence-observer.service",
                     "deploy/var/lib/game-server-interface/player-identities.json",
                     "scripts/install-usage-metering.sh"):
            self.assertIn(path, self.DEPLOY, f"{path} has no deploy trigger")

    def test_diagnostics_artefacts_trigger_their_installer(self) -> None:
        for path in ("deploy/usr/local/sbin/gsi-diagnose", "deploy/etc/sudoers.d/gsi-diagnose",
                     "scripts/install-diagnostics.sh"):
            self.assertIn(path, self.DEPLOY, f"{path} has no deploy trigger")

    def test_every_installer_script_is_reachable_from_deploy(self) -> None:
        import re
        installers = {p.name for p in DEPLOY_SH.parent.glob("install-*.sh")}
        referenced = set(re.findall(r"install-[a-z0-9-]+\.sh", self.DEPLOY))
        # install-phase2 and install-autodeploy bootstrap the host itself; they are run by hand
        # before auto-deploy exists and cannot deploy themselves.
        bootstrap = {"install-phase2.sh", "install-autodeploy.sh"}
        self.assertEqual(installers - referenced - bootstrap, set(),
                         "installer(s) unreachable from deploy.sh")
