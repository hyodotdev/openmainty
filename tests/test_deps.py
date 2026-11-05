"""Durable coverage: drift detection never raises, errors are reported."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import deps  # noqa: E402

PROFILE = {"name": "x", "github": "e/x", "baseBranch": "main",
           "dependencies": [
               {"name": "same", "kind": "npm", "id": "pkg-same",
                "pin_file": "pins.json", "pin_key": "same"},
               {"name": "old", "kind": "npm", "id": "pkg-old",
                "pin_file": "pins.json", "pin_key": "old"},
               {"name": "broken", "kind": "npm", "id": "pkg-broken",
                "pin_file": "pins.json", "pin_key": "missing"},
               {"name": "weird", "kind": "nope", "id": "z",
                "pin_file": "pins.json", "pin_key": "same"},
               {"name": "future", "kind": "npm", "id": "pkg-future",
                "pin_file": "pins.json", "pin_key": "same"},
           ]}


class DepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = Path(self.tmp.name)
        (self.checkout / "pins.json").write_text('{"same": "1.0.0", "old": "1.0.0"}')
        self.orig = deps.latest

        def fake_latest(kind, ident):
            if kind != "npm":
                raise ValueError("unknown kind")
            if ident == "pkg-future":
                raise LookupError("not published")
            return {"pkg-same": "1.0.0", "pkg-old": "2.0.0"}[ident]

        deps.latest = fake_latest
        self.addCleanup(lambda: setattr(deps, "latest", self.orig))

    def test_drift_and_errors_reported_without_raise(self):
        rows = deps.check(PROFILE, self.checkout)
        by_name = {r["name"]: r for r in rows}
        self.assertFalse(by_name["same"]["drift"])
        self.assertEqual(by_name["same"]["latest"], "1.0.0")
        self.assertTrue(by_name["old"]["drift"])
        self.assertIn("1.0.0", deps.drift_body(by_name["old"]))
        self.assertIn("2.0.0", deps.drift_body(by_name["old"]))
        self.assertIsNotNone(by_name["broken"]["error"])   # missing pin key
        self.assertIsNotNone(by_name["weird"]["error"])    # unknown kind
        self.assertTrue(by_name["future"]["unpublished"])  # 404, not an error
        self.assertIsNone(by_name["future"]["error"])
        self.assertFalse(any(r["drift"] for r in rows if r["error"]))

    def test_empty_discovery_on_manifests_is_loud(self):
        from unittest import mock
        profile = {"name": "x", "github": "e/x", "baseBranch": "main"}
        disc = {"deps": [], "sdks": [], "sources": ["package.json"], "warnings": []}
        with mock.patch("openmainty.manifests.discover", return_value=disc):
            rows = deps.check(profile, self.checkout)
        self.assertEqual(len(rows), 1)
        self.assertIn("no dependencies discovered", rows[0]["error"])


class WorkspaceLockTests(unittest.TestCase):
    """Resolver-derived workspace identity (no network in these tests).

    Regression for the real failure: example `react-native-nitro-sound:
    "*"` resolves in yarn berry to `workspace:.` (0.0.0-use.local) but
    was treated as an external npm upgrade to the registry latest. The
    lock resolution — not the declared text — decides local identity,
    so a sibling `*` resolving through the registry stays external.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.co = Path(self.tmp.name)
        (self.co / "package.json").write_text(
            '{"name": "self-pkg", "dependencies": {"left-pad": "*"}}')
        (self.co / "example").mkdir()
        (self.co / "example" / "package.json").write_text(
            '{"dependencies": {"self-pkg": "*", "left-pad": "^1.0.0"}}')
        # Berry-style lock: one shared entry resolves `*` to the
        # workspace, while the exact `^1.0.0` selector resolves to npm.
        # `two-ver` has separate chunks per selector (different versions).
        (self.co / "yarn.lock").write_text(
            '"self-pkg@npm:*, self-pkg@workspace:.":\n'
            "  version: 0.0.0-use.local\n"
            '  resolution: "self-pkg@workspace:."\n'
            '"left-pad@npm:*, left-pad@npm:^1.0.0":\n'
            "  version: 1.3.0\n"
            '  resolution: "left-pad@npm:1.3.0"\n'
            '"two-ver@npm:^1.0.0":\n'
            "  version: 1.0.5\n"
            '  resolution: "two-ver@npm:1.0.5"\n'
            '"two-ver@npm:^2.0.0":\n'
            "  version: 2.1.0\n"
            '  resolution: "two-ver@npm:2.1.0"\n')
        self.orig = deps._latest_fn
        self.calls = []
        def fake_latest(kind, ident):
            self.calls.append((kind, ident))
            return {"left-pad": "1.3.0", "two-ver": "2.1.0"}[ident]
        deps.set_latest_fn(fake_latest)
        self.addCleanup(lambda: deps.set_latest_fn(self.orig))
        self.profile = {"name": "x", "github": "e/x", "baseBranch": "main"}

    def test_workspace_star_never_hits_registry(self):
        from openmainty import manifests
        disc = manifests.discover(self.co)
        selfrows = [d for d in disc["deps"] if d["name"] == "self-pkg"]
        self.assertTrue(selfrows)
        for d in selfrows:
            self.assertEqual(d["declared"], "*")
            self.assertIn("workspace:.", d["lock_resolution"])
        rows = deps.check(self.profile, self.co)
        selfrow = next(r for r in rows if r["name"] == "self-pkg")
        self.assertFalse(selfrow["drift"])
        self.assertEqual(selfrow["update_kind"], "unmanaged")
        self.assertIsNone(selfrow["latest"])
        self.assertIsNone(selfrow["branch"])
        self.assertIn("workspace", selfrow["note"])
        self.assertNotIn(("npm", "self-pkg"), self.calls)

    def test_registry_star_stays_external(self):
        rows = deps.check(self.profile, self.co)
        ext = [r for r in rows if r["name"] == "left-pad"]
        self.assertTrue(ext)
        # Both selectors still go through the registry: not blindly local.
        self.assertIn(("npm", "left-pad"), self.calls)
        self.assertTrue(all(r["update_kind"] != "unmanaged" for r in ext))

    def test_exact_selectors_keep_their_own_resolution(self):
        from openmainty import manifests
        installed = manifests._npm_installed(self.co)
        self.assertEqual(
            manifests.installed_for(installed, "left-pad", "^1.0.0"), "1.3.0")
        self.assertEqual(
            manifests.resolution_for(installed, "left-pad", "*"), "left-pad@npm:1.3.0")
        self.assertEqual(
            manifests.resolution_for(installed, "self-pkg", "*"),
            "self-pkg@workspace:.")
        # Same name, different selectors: each keeps its own version.
        self.assertEqual(
            manifests.installed_for(installed, "two-ver", "^1.0.0"), "1.0.5")
        self.assertEqual(
            manifests.installed_for(installed, "two-ver", "^2.0.0"), "2.1.0")
        self.assertEqual(
            manifests.resolution_for(installed, "two-ver", "^1.0.0"),
            "two-ver@npm:1.0.5")
        self.assertEqual(
            manifests.resolution_for(installed, "two-ver", "^2.0.0"),
            "two-ver@npm:2.1.0")
        self.assertIsNone(
            manifests.local_resolution_reason("left-pad@npm:1.3.0"))
        self.assertIsNotNone(
            manifests.local_resolution_reason("self-pkg@workspace:."))
        self.assertIsNotNone(
            manifests.local_resolution_reason("link:node_modules/self-pkg"))


class PubExampleLockTests(unittest.TestCase):
    """Ignored root pubspec.lock lag is not a source job when the declared
    range already admits latest and every TRACKED lock is current.

    Real-git fixture: root pubspec.lock is git-ignored (library dev lock),
    example/pubspec.lock is tracked (consumer lock). Latest versions are
    still checked and shown; genuine tracked lag and outside-range
    constraints stay actionable; unknown (non-git) provenance stays
    conservative.
    """

    ROOT_YAML = ("environment:\n  sdk: '>=3.8.0 <4.0.0'\n"
                 "dependencies:\n  intl: '%s'\n  flutter:\n    sdk: flutter\n")
    EXAMPLE_YAML = ("environment:\n  sdk: '>=3.8.0 <4.0.0'\n"
                    "dependencies:\n  flutter:\n    sdk: flutter\n"
                    "  intl: %s\n"
                    "  flutter_calendar_carousel:\n    path: ../\n"
                    "  cupertino_icons: ^1.0.8\n")
    LOCK = ("packages:\n  intl:\n    dependency: \"direct main\"\n"
            "    description:\n      name: intl\n"
            "      sha256: \"abc\"\n      url: \"https://pub.dev\"\n"
            "    source: hosted\n    version: \"%s\"\n"
            "  cupertino_icons:\n    dependency: \"direct main\"\n"
            "    description:\n      name: cupertino_icons\n"
            "      sha256: \"def\"\n      url: \"https://pub.dev\"\n"
            "    source: hosted\n    version: \"1.0.8\"\n")

    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.co = Path(self.tmp.name)
        self.calls = []
        self.orig = deps._latest_fn

        def fake_latest(kind, ident):
            self.calls.append((kind, ident))
            if kind == "sdk":
                return "3.10.0"
            return {"intl": "0.20.3", "cupertino_icons": "1.0.8"}[ident]

        deps.set_latest_fn(fake_latest)
        self.addCleanup(lambda: deps.set_latest_fn(self.orig))
        self.profile = {"name": "x", "github": "e/x", "baseBranch": "main"}
        self._git = lambda *a: subprocess.run(
            ["git", *a], cwd=self.tmp.name, capture_output=True, text=True)

    def _fixture(self, root_constraint, root_ver, example_constraint,
                 example_ver, git=True):
        (self.co / "pubspec.yaml").write_text(
            self.ROOT_YAML % root_constraint)
        (self.co / "pubspec.lock").write_text(self.LOCK % root_ver)
        (self.co / ".gitignore").write_text("/pubspec.lock\n")
        (self.co / "example").mkdir(exist_ok=True)
        (self.co / "example" / "pubspec.yaml").write_text(
            self.EXAMPLE_YAML % example_constraint)
        (self.co / "example" / "pubspec.lock").write_text(
            self.LOCK % example_ver)
        if not git:
            return
        self.assertEqual(self._git("init", "-b", "main").returncode, 0)
        self.assertEqual(self._git("config", "user.email", "t@t").returncode, 0)
        self.assertEqual(self._git("config", "user.name", "t").returncode, 0)
        self.assertEqual(self._git("add", ".").returncode, 0)
        self.assertEqual(self._git("commit", "-m", "init").returncode, 0)

    def _intl_rows(self):
        rows = deps.check(self.profile, self.co)
        intl = [r for r in rows if r["name"] == "intl"]
        self.assertEqual(len(intl), 2)
        return {r["scope"]: r for r in intl}

    def test_ignored_root_lag_is_not_a_source_job(self):
        self._fixture('>=0.19.0 <0.21.0', '0.19.0', '^0.20.3', '0.20.3')
        by_scope = self._intl_rows()
        # Latest is still checked and shown on every row.
        for row in by_scope.values():
            self.assertEqual(row["latest"], "0.20.3")
            self.assertFalse(row.get("drift", False))
            self.assertFalse(row.get("outdated", False))
        root = by_scope["dependencies"]
        self.assertEqual(root["lock_file"], "pubspec.lock")
        self.assertIs(root["lock_tracked"], False)
        self.assertIn("not a source job", root.get("note", ""))
        example = by_scope["example:dependencies"]
        self.assertEqual(example["lock_file"], "example/pubspec.lock")
        self.assertIs(example["lock_tracked"], True)
        # The example path self-dependency stays local, never queried.
        self.assertNotIn(("pub", "flutter_calendar_carousel"), self.calls)
        self.assertIn(("pub", "intl"), self.calls)

    def test_tracked_example_lag_is_one_actionable_proposal(self):
        self._fixture('>=0.19.0 <0.21.0', '0.19.0', '^0.20.3', '0.19.0')
        by_scope = self._intl_rows()
        actionable = [r for r in by_scope.values()
                      if r.get("drift") or r.get("outdated")]
        self.assertEqual(len(actionable), 1)
        only = actionable[0]
        self.assertEqual(only["scope"], "example:dependencies")
        # Installed 0.19.0 violates declared ^0.20.3: real drift, not a
        # mere lock refresh — still exactly one correct proposal.
        self.assertTrue(only.get("drift"))
        self.assertEqual(only["latest"], "0.20.3")

    def test_tracked_example_lock_lag_is_lock_refresh(self):
        self._fixture('>=0.19.0 <0.21.0', '0.19.0', '>=0.19.0 <0.21.0',
                      '0.19.0')
        by_scope = self._intl_rows()
        actionable = [r for r in by_scope.values()
                      if r.get("drift") or r.get("outdated")]
        self.assertEqual(len(actionable), 1)
        only = actionable[0]
        self.assertEqual(only["scope"], "example:dependencies")
        self.assertTrue(only.get("outdated"))
        self.assertEqual(only["update_kind"], "lock-refresh")
        self.assertFalse(only.get("drift", False))

    def test_outside_range_constraint_stays_actionable(self):
        self._fixture('>=0.19.0 <0.20.0', '0.19.0', '^0.20.3', '0.20.3')
        by_scope = self._intl_rows()
        self.assertTrue(by_scope["dependencies"]["drift"])
        self.assertFalse(by_scope["example:dependencies"].get("drift", False))

    def test_unknown_provenance_stays_conservative(self):
        self._fixture('>=0.19.0 <0.21.0', '0.19.0', '^0.20.3', '0.20.3',
                      git=False)
        rows = deps.check(self.profile, self.co)
        intl = [r for r in rows if r["name"] == "intl"]
        self.assertTrue(intl)
        # No suppression without git provenance: the lag stays actionable.
        self.assertTrue(any(r.get("drift") or r.get("outdated")
                            for r in intl))


class ProgressTests(unittest.TestCase):
    """progress_fn streams each finished row instead of hanging silently.

    Regression for the real failure: sequential registry checks stalled
    (flaky reads timing out one package at a time) while `watch` printed
    nothing until every check finished — minutes of zero output that
    looked wedged. Errors must stream as they complete.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = Path(self.tmp.name)
        (self.checkout / "pins.json").write_text('{"same": "1.0.0", "old": "1.0.0"}')
        self.emitted = []

    def test_legacy_path_emits_every_row_in_order(self):
        orig = deps.latest

        def fake_latest(kind, ident):
            if kind != "npm":
                raise ValueError("unknown kind")
            if ident == "pkg-future":
                raise LookupError("not published")
            return {"pkg-same": "1.0.0", "pkg-old": "2.0.0"}[ident]

        deps.latest = fake_latest
        self.addCleanup(lambda: setattr(deps, "latest", orig))
        rows = deps.check(PROFILE, self.checkout, progress_fn=self.emitted.append)
        self.assertEqual(len(self.emitted), len(rows))
        for got, row in zip(self.emitted, rows):
            self.assertIs(got, row)
        # Error rows stream too — not just the successes.
        by_name = {r["name"]: r for r in self.emitted}
        self.assertIn("broken", by_name)
        self.assertIn("weird", by_name)
        self.assertIsNotNone(by_name["broken"]["error"])

    def test_discovered_path_streams_registry_errors_live(self):
        from unittest import mock
        profile = {"name": "x", "github": "e/x", "baseBranch": "main"}
        disc = {"deps": [{"name": "aaa-ok", "manager": "npm", "scope": "dependencies",
                          "file": "package.json", "declared": "^1.0.0",
                          "installed": "1.0.0"},
                         {"name": "bbb-flaky", "manager": "npm", "scope": "dependencies",
                          "file": "package.json", "declared": "^2.0.0",
                          "installed": "2.0.0"}],
                "sdks": [], "sources": ["package.json"], "warnings": []}
        seen_during_check = []

        def fake_latest(kind, ident):
            if ident == "bbb-flaky":
                raise TimeoutError("The read operation timed out")
            return "1.0.0"

        orig = deps._latest_fn
        deps.set_latest_fn(fake_latest)
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        with mock.patch("openmainty.manifests.discover", return_value=disc):
            rows = deps.check(profile, self.checkout,
                              progress_fn=seen_during_check.append)
        self.assertEqual(len(seen_during_check), len(rows))
        for got, row in zip(seen_during_check, rows):
            self.assertIs(got, row)
        flaky = next(r for r in seen_during_check if r["name"] == "bbb-flaky")
        self.assertIn("timed out", flaky["error"])

    def test_discovery_failure_row_is_emitted(self):
        from unittest import mock
        profile = {"name": "x", "github": "e/x", "baseBranch": "main"}
        with mock.patch("openmainty.manifests.discover",
                        side_effect=RuntimeError("boom")):
            rows = deps.check(profile, self.checkout,
                              progress_fn=self.emitted.append)
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.emitted, rows)
        self.assertIn("boom", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
