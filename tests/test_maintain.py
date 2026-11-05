"""Maintainer vertical: discovery, classification, routing, scheduler, graph.

Isolated fixtures only (temp OPENMAINTY_HOME, fake registry/executor).
No real network, no target checkouts.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import branches, deps, events, graph, manifests, repos, sched, versions  # noqa: E402
from openmainty import pcconfig, runner, store  # noqa: E402
try:
    from tests._home import (use_temp_home, offline_discussions,  # noqa: E402
                             offline_prs, offline_muse_version)
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import (use_temp_home, offline_discussions,  # noqa: E402
                       offline_prs, offline_muse_version)


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    # C2: the Discussions scan source runs on every sched.scan; fixture
    # repos must never reach real gh/network.
    offline_discussions(test)
    # Generic scan fixture has no PR input: stub the PR boundary too.
    offline_prs(test, [])
    # Generic behavior fixture has no real CLI version: stub metadata.
    offline_muse_version(test, "fixture-muse-0.0.0")
    return Path(tmp.name)


class VersionTests(unittest.TestCase):
    def test_rn_minor_is_breaking(self):
        self.assertEqual(versions.update_kind("0.86.2", "0.87.0"), "rn-minor")
        self.assertEqual(versions.safe_lane("rn-minor"), "next")
        self.assertEqual(versions.update_kind("0.86.2", "0.86.3"), "patch")
        self.assertEqual(versions.safe_lane("patch"), "main")

    def test_major_minor_patch(self):
        self.assertEqual(versions.update_kind("1.2.3", "2.0.0"), "major")
        self.assertEqual(versions.update_kind("1.2.3", "1.3.0"), "minor")
        self.assertEqual(versions.update_kind("1.2.3", "1.2.4"), "patch")
        self.assertEqual(versions.update_kind("1.2.3", "1.2.3"), "none")
        self.assertEqual(versions.safe_lane("minor"), "next")
        self.assertEqual(versions.safe_lane("major"), "next")

    def test_prerelease_and_downgrade(self):
        self.assertEqual(versions.update_kind("1.0.0", "2.0.0-next.1"), "prerelease")
        self.assertEqual(versions.update_kind("2.0.0", "1.9.9"), "downgrade")
        self.assertEqual(versions.update_kind("bogus", "1.0.0"), "unknown")

    def test_ranges(self):
        self.assertTrue(versions.satisfies("^0.86.2", "0.86.2"))
        self.assertFalse(versions.satisfies("^0.86.2", "0.87.0"))
        self.assertTrue(versions.satisfies(">=3.8.0 <4.0.0", "3.9.0"))
        self.assertFalse(versions.satisfies(">=3.8.0 <4.0.0", "4.0.0"))
        self.assertTrue(versions.satisfies("^19.2.0", "19.2.8"))
        self.assertIsNone(versions.satisfies("github:user/repo#frag", "1.0.0"))

    def test_no_false_drift_for_range_covering_latest(self):
        # ^19.2.18 covers 19.2.8? No: 19.2.8 < 19.2.18 floor. Must be False.
        self.assertFalse(versions.satisfies("^19.2.18", "19.2.8"))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.co = Path(self.tmp.name)
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"react-native": "0.86.2"},
            "devDependencies": {"jest": "^29.7.0"},
            "peerDependencies": {"react": "*"}}))
        (self.co / "yarn.lock").write_text(
            '"jest@npm:^29.7.0":\n  version: 29.7.0\n'
            '"react-native@npm:0.86.2":\n  version: 0.86.2\n')
        (self.co / "example").mkdir()
        (self.co / "example" / "package.json").write_text(json.dumps({
            "dependencies": {"react-native": "0.86.2"}}))

    def test_npm_discovery(self):
        disc = manifests.discover(self.co)
        self.assertIn("package.json", disc["sources"])
        self.assertIn("example/package.json", disc["sources"])
        rn = [d for d in disc["deps"] if d["name"] == "react-native"]
        self.assertTrue(rn)
        self.assertEqual(rn[0]["installed"], "0.86.2")

    def test_pub_discovery(self):
        co2 = self.co / "pub"
        co2.mkdir()
        (co2 / "pubspec.yaml").write_text(
            "environment:\n  sdk: '>=3.8.0 <4.0.0'\n"
            "dependencies:\n  intl: '>=0.19.0 <0.21.0'\n"
            "  flutter:\n    sdk: flutter\n")
        disc = manifests.discover(co2)
        names = {d["name"] for d in disc["deps"]}
        self.assertIn("intl", names)
        self.assertTrue(any(s["name"] == "dart-sdk" for s in disc["sdks"]))

    def test_empty_dir_warns_honestly(self):
        co3 = self.co / "empty"
        co3.mkdir()
        disc = manifests.discover(co3)
        self.assertEqual(disc["deps"], [])
        self.assertTrue(disc["warnings"])

    def test_real_registered_manifests_parse(self):
        """Current real manifests as read-only parser samples (no writes,
        no network): the registered targets must discover with locks."""
        import re as _re
        semver = _re.compile(r"^\d+\.\d+\.\d+")
        nitro = pcconfig.checkout_for("react-native-nitro-sound")
        cal = pcconfig.checkout_for("flutter_calendar_carousel")
        if not nitro or not Path(nitro).is_dir():
            self.skipTest("nitro checkout not linked on this machine")
        disc = manifests.discover(Path(nitro))
        rn = [d for d in disc["deps"] if d["name"] == "react-native"]
        self.assertTrue(rn, "react-native must be discovered")
        locked = [d for d in rn if d.get("installed")
                  and semver.match(d["installed"])]
        self.assertTrue(locked, "yarn.lock must resolve installed versions")
        if not cal or not Path(cal).is_dir():
            self.skipTest("calendar checkout not linked on this machine")
        disc2 = manifests.discover(Path(cal))
        intl = [d for d in disc2["deps"] if d["name"] == "intl"]
        self.assertTrue(intl, "intl must be discovered")
        self.assertTrue(any(s["name"] == "dart-sdk" for s in disc2["sdks"]))


class BranchTests(unittest.TestCase):
    def test_rn_minor_goes_next(self):
        r = branches.route("react-native", "0.86.2", "0.87.0")
        self.assertEqual(r["lane"], "next")
        self.assertTrue(r["branch"].startswith("codex/next/"))

    def test_patch_goes_main(self):
        r = branches.route("intl", "0.19.0", "0.19.1")
        self.assertEqual(r["lane"], "main")
        self.assertTrue(r["branch"].startswith("codex/maint/"))

    def test_unknown_has_no_branch(self):
        r = branches.route("x", None, None)
        self.assertIsNone(r["branch"])

    def test_promotion_readiness(self):
        # Stronger contract: HEAD equality alone is not proof. Readiness
        # needs commit+fingerprint binding to the current bytes; a done step
        # with no content proof (legacy) never qualifies.
        state = {"steps": {"review": {"status": "done"},
                           "e2e": {"status": "done"}},
                 "verifiedCommits": {"review": "abc", "e2e": "abc"},
                 "verifiedFingerprints": {"review": "fp1", "e2e": "fp1"},
                 "workTreeFingerprint": "fp1",
                 "workCommit": "abc",
                 "waitReason": None,
                 "artifacts": {"release_notes": "release_notes.md"}}
        self.assertTrue(branches.promotion_readiness(state)["ready"])
        state["steps"]["e2e"]["status"] = "failed"
        self.assertFalse(branches.promotion_readiness(state)["ready"])
        state["steps"]["e2e"]["status"] = "done"
        del state["verifiedFingerprints"]
        self.assertFalse(branches.promotion_readiness(state)["ready"])


class SchedTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        repos.save({"name": "t1", "github": "e/t1", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["t1"] = str(self.co)
        pcconfig.save(pc)
        self.orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(self.orig))
        # Deterministic: never touch real gh/network in fixture scans.
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)

    def test_scan_enqueues_with_dedup(self):
        sched.configure("t1", enabled=True, interval_s=60)
        r1 = sched.scan("t1")
        self.assertEqual(r1["queued_added"], 1)
        r2 = sched.scan("t1")
        self.assertEqual(r2["queued_added"], 0)
        self.assertEqual(r2["queued_existing"], 1)
        st = sched.load("t1")
        self.assertEqual(len([i for i in st["queue"] if i["status"] == "queued"]), 1)

    def test_overlap_blocks_second_worker(self):
        sched.configure("t1", enabled=True, interval_s=60)
        sched.scan("t1")
        sched._acquire_global_lock()
        try:
            out = sched.run_due("t1", runner.FakeExecutor())
            self.assertFalse(out["ran"])
        finally:
            sched._release_global_lock()

    def test_restart_recovery_requeues(self):
        st = sched.configure("t1", enabled=True, interval_s=60)
        snap = {"key": "t1+left-pad+1.1.0+stable", "kind": "dep-update",
                "repo": "t1", "dep": "left-pad", "target": "1.1.0",
                "lane": "next", "status": "running", "attempts": 1,
                "run_id": "run-deadbeef"}
        st["current_job"] = {"id": snap["key"], "run_id": "run-deadbeef",
                             "started_at": "2020-01-01T00:00:00+00:00",
                             "item": snap}
        sched.save(st)
        try:
            Path(sched._lock_file()).unlink()
        except OSError:
            pass
        sched.scan("t1")
        st2 = sched.load("t1")
        # Same identity recovery: fields + run id + attempts preserved,
        # no generic placeholder item.
        found = [i for i in st2["queue"] if i.get("key") == snap["key"]]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "queued")
        self.assertEqual(found[0]["run_id"], "run-deadbeef")
        self.assertEqual(found[0]["attempts"], 1)
        self.assertEqual(found[0]["target"], "1.1.0")
        self.assertFalse(any(i.get("kind") == "recovered" for i in st2["queue"]))

    def test_bounded_retries_block(self):
        sched.configure("t1", enabled=True, interval_s=60)
        sched.scan("t1")
        # Force failures through the worker entry; retries keep firing
        # (backoff cleared between rounds to simulate time passing).
        from unittest import mock
        with mock.patch("openmainty.cli._start_run_with",
                        return_value=(1, None)):
            for _ in range(3):
                sched.run_due("t1", runner.FakeExecutor())
                st = sched.load("t1")
                for item in st["queue"]:
                    item["next_retry_at"] = None
                sched.save(st)
        st = sched.load("t1")
        blocked = [i for i in st["queue"] if i["status"] == "blocked"]
        self.assertTrue(blocked)
        self.assertIn("retries exhausted", blocked[0].get("blocker", ""))

    def test_old_state_compat(self):
        f = sched._file("t1")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"repo": "t1", "enabled": True}))
        st = sched.load("t1")
        self.assertEqual(st["queue"], [])
        self.assertEqual(st["failures"], 0)

    def test_malformed_rows_never_raise(self):
        rows = deps.check({"name": "t1", "github": "e/t1", "baseBranch": "main"},
                          self.co)
        self.assertTrue(isinstance(rows, list))


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        repos.save({"name": "g1", "github": "e/g1", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})

    def test_empty_graph_honest(self):
        g = graph.build("g1")
        self.assertEqual(g["nodes"], [{"id": "repo:g1", "type": "repo",
                                       "label": "g1", "status": "ok",
                                       "detail": "e/g1"}])
        self.assertEqual(g["edges"], [])

    def test_issue_edges_need_provenance(self):
        edges = graph.issue_edges_from_text("fixes e/g1#12 and #13", "event:scan")
        self.assertEqual(len(edges), 2)
        self.assertTrue(all("explicit reference" in e["provenance"] for e in edges))
        self.assertEqual(graph.issue_edges_from_text("no refs here", "s"), [])


class DedupLifecycleTests(unittest.TestCase):
    """waiting-review/done-history must never re-enqueue; coupled scopes coalesce."""

    def setUp(self):
        self.root = make_home(self)
        repos.save({"name": "d1", "github": "e/d1", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        sched.configure("d1", enabled=True, interval_s=60)

    def _item(self, **over):
        base = {"kind": "dep-update", "repo": "d1", "dep": "react",
                "target": "19.3.0", "channel": "stable", "lane": "next",
                "status": "queued", "attempts": 0, "created_at": "t",
                "run_id": None, "run_issue": None, "verified": None}
        base.update(over)
        base["key"] = sched.dedup_key(base)
        return base

    def test_waiting_review_never_requeues(self):
        st = sched.load("d1")
        item = self._item(status="waiting-review")
        self.assertTrue(sched._enqueue(st, item))
        again = self._item()
        self.assertFalse(sched._enqueue(sched.load("d1"), again))
        self.assertEqual(len([i for i in sched.load("d1")["queue"]
                              if i["key"] == item["key"]]), 1)

    def test_history_key_never_requeues(self):
        st = sched.load("d1")
        item = self._item()
        self.assertTrue(sched._enqueue(st, item))
        st2 = sched.load("d1")
        st2["queue"] = [i for i in st2["queue"] if i["key"] != item["key"]]
        st2["history"].append({"key": item["key"], "at": "t",
                               "result": "waiting-review"})
        sched.save(st2)
        self.assertFalse(sched._enqueue(sched.load("d1"), self._item()))
        self.assertEqual(sched.load("d1")["queue"], [])

    def test_coupled_scopes_coalesce(self):
        st = sched.load("d1")
        root = self._item(scopes=["dependencies"], file="package.json")
        self.assertTrue(sched._enqueue(st, root))
        example = self._item(scopes=["example:dependencies"],
                             file="example/package.json")
        st2 = sched.load("d1")
        self.assertFalse(sched._enqueue(st2, example))
        items = sched.load("d1")["queue"]
        self.assertEqual(len(items), 1)
        self.assertEqual(sorted(items[0].get("scopes", [])),
                         ["dependencies", "example:dependencies"])

    def test_newer_target_queues_fresh_row(self):
        """A newer release never rewrites the bound row in place.

        19.3.0 stays exactly as queued; 19.4.0 arrives as a second,
        run-free queued candidate. (The old wins-coalesce behavior is the
        21.2.3/21.2.4 identity defect: it is removed, not preserved.)
        """
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(st, self._item(target="19.3.0")))
        st2 = sched.load("d1")
        self.assertTrue(sched._enqueue(st2, self._item(target="19.4.0")))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 2)
        old = [r for r in rows if r["target"] == "19.3.0"][0]
        new = [r for r in rows if r["target"] == "19.4.0"][0]
        self.assertEqual(old["key"], sched.dedup_key(self._item(target="19.3.0")))
        self.assertEqual(new["status"], "queued")
        self.assertIsNone(new.get("run_id"))
        self.assertIsNone(new.get("run_issue"))
        self.assertIsNone(new.get("verified"))

    def _bound(self, **over):
        base = self._item(target="21.2.3", lane="main",
                          branch="codex/maint/commitlint-cli-21-2-3",
                          status="waiting-review", run_id="run-7fc8d753",
                          run_issue="dep-@commitlint-cli-21.2.3",
                          verified=True, created_at="2026-10-17T17:10:48+00:00")
        base.update(over)
        base["key"] = sched.dedup_key(base)
        return base

    def test_new_version_never_retargets_bound_row(self):
        """waiting-review + run binding survives a newer scan untouched."""
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(st, self._bound()))
        st2 = sched.load("d1")
        scanned = self._item(target="21.2.4", lane="main",
                             branch="codex/maint/commitlint-cli-21-2-4")
        self.assertTrue(sched._enqueue(st2, scanned))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 2)
        bound = [r for r in rows if r.get("run_id") == "run-7fc8d753"][0]
        for f in ("target", "lane", "branch", "key", "run_issue",
                  "verified", "status", "created_at"):
            self.assertEqual(bound[f], self._bound()[f],
                             f"bound identity field {f} must not move")
        fresh = [r for r in rows if r["target"] == "21.2.4"][0]
        self.assertEqual(fresh["status"], "queued")
        self.assertEqual(fresh["attempts"], 0)
        self.assertIsNone(fresh.get("run_id"))
        self.assertIsNone(fresh.get("run_issue"))
        self.assertIsNone(fresh.get("verified"))

    def test_new_version_after_running_row(self):
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(st, self._bound(status="running")))
        st2 = sched.load("d1")
        scanned = self._item(target="21.2.4", lane="main",
                             branch="codex/maint/commitlint-cli-21-2-4")
        self.assertTrue(sched._enqueue(st2, scanned))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 2)
        bound = [r for r in rows if r.get("run_id") == "run-7fc8d753"][0]
        self.assertEqual((bound["target"], bound["status"]), ("21.2.3", "running"))
        self.assertNotIn("run_id", [r for r in rows if r["target"] == "21.2.4"][0])

    def test_issue_triage_refresh_invents_nothing(self):
        """Generic rows keep legacy semantics: a duplicate refresh merges
        evidence but never invents lane/branch nor touches lifecycle."""
        st = sched.load("d1")
        row = {"kind": "issue-triage", "repo": "d1", "issue": "277",
               "title": "t", "status": "queued", "attempts": 0,
               "created_at": "t", "branch": "codex/issue/277",
               "key": "d1#issue-277"}
        st["queue"].append(dict(row))
        sched.save(st)
        st2 = sched.load("d1")
        dup = dict(row)
        dup["title"] = "t2"
        self.assertFalse(sched._enqueue(st2, dup))
        out = sched.load("d1")["queue"][0]
        self.assertEqual(out["title"], "t2")
        self.assertEqual(out["branch"], "codex/issue/277")
        self.assertNotIn("lane", out)
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["attempts"], 0)

    def test_repeated_scans_dedup_same_release(self):
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(st, self._item(target="19.3.0")))
        st2 = sched.load("d1")
        self.assertFalse(sched._enqueue(st2, self._item(target="19.3.0")))
        self.assertEqual(len(sched.load("d1")["queue"]), 1)

    def test_same_release_contexts_coalesce_safely(self):
        st = sched.load("d1")
        bound = self._bound(target="19.3.0", lane="next", branch="b-19-3-0",
                            status="queued", run_id="run-x",
                            run_issue="dep-r-19.3.0", verified=False,
                            scopes=["dependencies"])
        self.assertTrue(sched._enqueue(st, bound))
        st2 = sched.load("d1")
        ctx = self._item(target="19.3.0", lane="next", branch="b-19-3-0",
                         scopes=["example:dependencies"])
        self.assertFalse(sched._enqueue(st2, ctx))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]["scopes"]),
                         ["dependencies", "example:dependencies"])
        self.assertEqual(rows[0]["run_id"], "run-x")
        self.assertEqual(rows[0]["target"], "19.3.0")

    def test_lane_main_then_next_resolves_next(self):
        """Unbound rows resolve conservatively: any breaking (next) context
        wins, so manifest order cannot hide it. Branch follows the lane."""
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(
            st, self._item(target="19.3.0", lane="main",
                           branch="codex/maint/react-19-3-0")))
        st2 = sched.load("d1")
        self.assertFalse(sched._enqueue(
            st2, self._item(target="19.3.0", lane="next",
                            branch="codex/next/react-19-3-0")))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lane"], "next")
        self.assertEqual(rows[0]["branch"], "codex/next/react-19-3-0")

    def test_lane_next_then_main_stays_next(self):
        """Reverse order, same outcome: next is sticky, never downgraded."""
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(
            st, self._item(target="19.3.0", lane="next",
                           branch="codex/next/react-19-3-0")))
        st2 = sched.load("d1")
        self.assertFalse(sched._enqueue(
            st2, self._item(target="19.3.0", lane="main",
                            branch="codex/maint/react-19-3-0")))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lane"], "next")
        self.assertEqual(rows[0]["branch"], "codex/next/react-19-3-0")

    def test_bound_lane_conflict_blocks_honestly(self):
        """A bound main proof plus a next-lane scan must not graft proof or
        pretend the next review complete: honest blocked conflict instead."""
        st = sched.load("d1")
        bound = self._bound(target="19.3.0", lane="main",
                            branch="codex/maint/react-19-3-0")
        self.assertTrue(sched._enqueue(st, bound))
        st2 = sched.load("d1")
        scanned = self._item(target="19.3.0", lane="next",
                             branch="codex/next/react-19-3-0")
        self.assertFalse(sched._enqueue(st2, scanned))
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "blocked")
        self.assertIn("lane conflict", row.get("blocker", ""))
        self.assertIn("main", row.get("blocker", ""))
        self.assertIn("next", row.get("blocker", ""))
        for f in ("target", "lane", "branch", "key", "run_id", "run_issue",
                  "verified"):
            self.assertEqual(row[f], bound[f],
                             f"bound proof field {f} must not move")

    def test_refresh_preserves_run_binding(self):
        st = sched.load("d1")
        self.assertTrue(sched._enqueue(st, self._bound()))
        st2 = sched.load("d1")
        again = self._bound(why="new scan evidence")
        self.assertFalse(sched._enqueue(st2, again))
        row = sched.load("d1")["queue"][0]
        for f in ("target", "lane", "branch", "key", "run_id", "run_issue",
                  "verified", "status", "attempts", "created_at"):
            self.assertEqual(row[f], self._bound()[f])
        self.assertEqual(row["why"], "new scan evidence")


class RepairBindingsTests(unittest.TestCase):
    """Cross-version binding repair: dry-run plan, locked CAS apply.

    A corrupted row (run proved 19.3.0, row retargeted to 19.4.0) is
    restored from its immutable run record, and the newer release is
    owed a fresh run-free queued candidate. Missing run records are
    reported skipped, never touched.
    """

    def setUp(self):
        self.root = make_home(self)
        repos.save({"name": "d1", "github": "e/d1", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        sched.configure("d1", enabled=True, interval_s=60)
        run = {"id": "run-abc123", "repo": "d1",
               "issue": "dep-react-19.3.0", "dep": "react",
               "target": "19.3.0", "lane": "main",
               "branch": "codex/maint/react-19-3-0",
               "queue_key": "d1+react+19.3.0+stable"}
        rf = store.run_file("run-abc123")
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text(json.dumps(run), encoding="utf-8")
        st = sched.load("d1")
        st["queue"].append({
            "kind": "dep-update", "repo": "d1", "dep": "react",
            "target": "19.4.0", "channel": "stable", "lane": "main",
            "branch": "codex/maint/react-19-4-0", "update_kind": "patch",
            "scopes": ["dependencies"], "file": "package.json",
            "why": "19.3.0 -> 19.4.0", "status": "waiting-review",
            "attempts": 0, "next_retry_at": None, "run_id": "run-abc123",
            "run_issue": "dep-react-19.3.0", "verified": True,
            "created_at": "t",
            "key": "d1+react+19.4.0+stable"})
        sched.save(st)

    def test_dry_run_plans_without_mutation(self):
        before = sched._queue_fingerprint(sched.load("d1"))
        out = sched.repair_version_bindings("d1")
        self.assertTrue(out["ok"])
        self.assertFalse(out["applied"])
        self.assertEqual(len(out["repairs"]), 1)
        self.assertEqual(out["repairs"][0]["drift"],
                         ["branch", "key", "target"])
        self.assertEqual(out["repairs"][0]["to"]["target"], "19.3.0")
        self.assertEqual(len(out["enqueues"]), 1)
        self.assertEqual(out["enqueues"][0]["target"], "19.4.0")
        self.assertEqual(out["enqueues"][0]["key"], "d1+react+19.4.0+stable")
        self.assertEqual(sched._queue_fingerprint(sched.load("d1")), before)

    def test_apply_restores_and_queues_fresh(self):
        dry = sched.repair_version_bindings("d1")
        out = sched.repair_version_bindings(
            "d1", apply=True, fingerprint=dry["fingerprint"])
        self.assertTrue(out["ok"])
        self.assertTrue(out["applied"])
        self.assertEqual(out["restored"], ["d1+react+19.4.0+stable"])
        self.assertEqual(out["queued"], ["d1+react+19.4.0+stable"])
        rows = sched.load("d1")["queue"]
        self.assertEqual(len(rows), 2)
        bound = [r for r in rows if r.get("run_id") == "run-abc123"][0]
        self.assertEqual(
            (bound["target"], bound["branch"], bound["key"],
             bound["status"], bound["verified"]),
            ("19.3.0", "codex/maint/react-19-3-0", "d1+react+19.3.0+stable",
             "waiting-review", True))
        fresh = [r for r in rows if r["target"] == "19.4.0"][0]
        self.assertEqual(fresh["status"], "queued")
        self.assertIsNone(fresh.get("run_id"))
        self.assertIsNone(fresh.get("verified"))

    def test_apply_aborts_on_moved_state(self):
        dry = sched.repair_version_bindings("d1")
        st = sched.load("d1")
        st["queue"].append({"key": "d1+other", "status": "queued"})
        sched.save(st)
        out = sched.repair_version_bindings(
            "d1", apply=True, fingerprint=dry["fingerprint"])
        self.assertFalse(out["ok"])
        self.assertFalse(out["applied"])
        self.assertIn("re-plan", out["reason"])

    def test_missing_run_record_skipped(self):
        st = sched.load("d1")
        st["queue"].append({"kind": "dep-update", "repo": "d1", "dep": "zz",
                            "target": "9.9.9", "channel": "stable",
                            "lane": "main", "status": "queued",
                            "run_id": "run-nope",
                            "key": "d1+zz+9.9.9+stable"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        self.assertTrue(out["ok"])
        self.assertTrue(any(s["run_id"] == "run-nope"
                            for s in out["skipped"]))

    def test_apply_requires_fingerprint(self):
        out = sched.repair_version_bindings("d1", apply=True)
        self.assertFalse(out["ok"])
        self.assertFalse(out["applied"])
        self.assertIn("fingerprint", out["reason"])

    def test_apply_single_atomic_save(self):
        from openmainty import sched as _s
        calls = []
        real = _s.save
        def counting(st):
            calls.append(1)
            return real(st)
        _s.save = counting
        try:
            dry = sched.repair_version_bindings("d1")
            out = sched.repair_version_bindings(
                "d1", apply=True, fingerprint=dry["fingerprint"])
        finally:
            _s.save = real
        self.assertTrue(out["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIn("last_binding_repair", sched.load("d1"))

    def test_apply_is_idempotent(self):
        dry = sched.repair_version_bindings("d1")
        first = sched.repair_version_bindings(
            "d1", apply=True, fingerprint=dry["fingerprint"])
        self.assertTrue(first["ok"])
        hist_before = sched.load("d1")["binding_repair_history"]
        last_before = sched.load("d1")["last_binding_repair"]
        self.assertEqual(len(hist_before), 1)
        self.assertIn("19.4.0", hist_before[0]["repairs"][0]["from"]["why"])
        dry2 = sched.repair_version_bindings("d1")
        second = sched.repair_version_bindings(
            "d1", apply=True, fingerprint=dry2["fingerprint"])
        self.assertTrue(second["ok"])
        # No-op: no save effect, real audit record preserved as-is.
        self.assertFalse(second["applied"])
        st = sched.load("d1")
        self.assertEqual(st["binding_repair_history"], hist_before)
        self.assertEqual(st["last_binding_repair"], last_before)
        self.assertEqual(st["last_binding_repair"]["history_ref"], 0)
        self.assertEqual(st["last_binding_repair"]["restored"],
                         ["d1+react+19.4.0+stable"])

    def _plant_run(self, rid, **over):
        run = {"id": rid, "repo": "d1",
               "issue": f"dep-zz-{over.get('target', '1.0.0')}",
               "dep": "zz", "target": "1.0.0", "lane": "main",
               "branch": "codex/maint/zz-1-0-0",
               "queue_key": "d1+zz+1.0.0+stable"}
        run.update(over)
        rf = store.run_file(rid)
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text(json.dumps(run), encoding="utf-8")

    def test_foreign_run_record_skipped(self):
        self._plant_run("run-foreign", repo="other-repo")
        st = sched.load("d1")
        st["queue"].append({"kind": "dep-update", "repo": "d1", "dep": "zz",
                            "target": "2.0.0", "channel": "stable",
                            "lane": "main", "branch": "b2", "status": "queued",
                            "run_id": "run-foreign",
                            "key": "d1+zz+2.0.0+stable"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        self.assertTrue(any("foreign" in s.get("reason", "")
                            for s in out["skipped"]))
        self.assertTrue(all(r["run_id"] != "run-foreign"
                            for r in out["repairs"]))
        self.assertTrue(any(r["run_id"] == "run-abc123"
                            for r in out["repairs"]))

    def test_incomplete_run_record_skipped(self):
        self._plant_run("run-partial", lane=None, branch=None, issue=None)
        st = sched.load("d1")
        st["queue"].append({"kind": "dep-update", "repo": "d1", "dep": "zz",
                            "target": "2.0.0", "channel": "stable",
                            "lane": "main", "branch": "b2", "status": "queued",
                            "run_id": "run-partial",
                            "key": "d1+zz+2.0.0+stable"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        self.assertTrue(any("incomplete" in s.get("reason", "")
                            for s in out["skipped"]))
        self.assertTrue(all(r["run_id"] != "run-partial"
                            for r in out["repairs"]))

    def test_non_dep_row_with_run_id_untouched(self):
        self._plant_run("run-issue9")
        st = sched.load("d1")
        st["queue"].append({"kind": "issue-triage", "repo": "d1",
                            "issue": "9", "title": "t", "status": "queued",
                            "run_id": "run-issue9",
                            "key": "d1#issue-9"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        row = [r for r in sched.load("d1")["queue"]
               if r["key"] == "d1#issue-9"][0]
        self.assertNotIn("target", row)
        self.assertTrue(any(s.get("run_id") == "run-issue9"
                            for s in out["skipped"]))

    def test_non_newer_recorded_release_not_queued(self):
        self._plant_run("run-old", target="1.0.0")
        st = sched.load("d1")
        st["queue"].append({"kind": "dep-update", "repo": "d1", "dep": "zz",
                            "target": "0.9.0", "channel": "stable",
                            "lane": "main", "branch": "b-old",
                            "status": "waiting-review", "run_id": "run-old",
                            "run_issue": "dep-zz-1.0.0", "verified": True,
                            "key": "d1+zz+0.9.0+stable"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        self.assertEqual(len(out["repairs"]), 2)
        self.assertEqual(len(out["enqueues"]), 1)
        self.assertTrue(all(e["target"] != "0.9.0" for e in out["enqueues"]))
        self.assertTrue(any("not newer" in s.get("reason", "")
                            for s in out["skipped"]))

    def test_three_row_reason_subject_consistency(self):
        """Each restored row carries run evidence plus a repair note naming
        the old subject and the separate new candidate; the corrupt newer
        reason survives only in the audit (from.why); fresh candidates keep
        the actual latest scan reason."""
        for dep in ("aa", "bb", "cc"):
            self._plant_run(
                f"run-{dep}", dep=dep, target="1.0.0", lane="main",
                branch=f"codex/maint/{dep}-1-0-0",
                issue=f"dep-{dep}-1.0.0",
                queue_key=f"d1+{dep}+1.0.0+stable",
                scopes=["devDependencies"], update_kind="patch",
                base_branch="origin/main")
        st = sched.load("d1")
        st["queue"] = [r for r in st["queue"]
                       if r.get("run_id") != "run-abc123"]
        for dep in ("aa", "bb", "cc"):
            st["queue"].append({
                "kind": "dep-update", "repo": "d1", "dep": dep,
                "target": "2.0.0", "channel": "stable", "lane": "main",
                "branch": f"codex/maint/{dep}-2-0-0", "update_kind": "major",
                "scopes": ["example"], "file": "package.json",
                "why": f"{dep} 0.9.0 -> 2.0.0", "status": "waiting-review",
                "attempts": 0, "next_retry_at": None,
                "run_id": f"run-{dep}", "run_issue": f"dep-{dep}-1.0.0",
                "verified": True, "created_at": "t",
                "key": f"d1+{dep}+2.0.0+stable"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        self.assertEqual(len(out["repairs"]), 3)
        self.assertEqual(len(out["enqueues"]), 3)
        for rep in out["repairs"]:
            dep = rep["to"]["dep"]
            self.assertEqual(rep["to"]["target"], "1.0.0")
            self.assertEqual(rep["to"]["scopes"], ["devDependencies"])
            self.assertEqual(rep["to"]["update_kind"], "patch")
            self.assertEqual(rep["to"]["base_branch"], "origin/main")
            self.assertEqual(rep["to"]["why"],
                             f"실행 기록 기준 바인딩 복원: {dep} 1.0.0")
            self.assertNotIn("run-", rep["to"]["why"])
            self.assertNotIn("2.0.0", rep["to"]["why"])
            self.assertEqual(rep["from"]["why"], f"{dep} 0.9.0 -> 2.0.0")
        for cand in out["enqueues"]:
            self.assertTrue(cand["why"].endswith("-> 2.0.0"))

    def _plant_issue(self, issue, **over):
        row = {"kind": "issue-triage", "repo": "d1", "issue": issue,
               "title": f"triage {issue}", "status": "queued",
               "attempts": 0, "created_at": "t",
               "key": f"d1#issue-{issue}"}
        row.update(over)
        st = sched.load("d1")
        st["queue"].append(row)
        sched.save(st)
        return row["key"]

    def test_issue_signature_planned_and_unset(self):
        key = self._plant_issue("277", lane="main",
                                branch="codex/maint/none-none")
        out = sched.repair_version_bindings("d1")
        self.assertEqual(len(out["issue_repairs"]), 1)
        self.assertEqual(out["issue_repairs"][0]["row_key"], key)
        dry = sched.repair_version_bindings("d1")
        applied = sched.repair_version_bindings(
            "d1", apply=True, fingerprint=dry["fingerprint"])
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["issue_fixed"], [key])
        row = [r for r in sched.load("d1")["queue"]
               if r["key"] == key][0]
        self.assertNotIn("lane", row)
        self.assertNotIn("branch", row)
        self.assertEqual((row["title"], row["status"]), ("triage 277", "queued"))
        # Second pass: nothing left to fix, true no-op.
        dry2 = sched.repair_version_bindings("d1")
        self.assertEqual(dry2["issue_repairs"], [])
        noop = sched.repair_version_bindings(
            "d1", apply=True, fingerprint=dry2["fingerprint"])
        self.assertTrue(noop["ok"])
        self.assertFalse(noop["applied"])

    def test_issue_repair_leaves_others_alone(self):
        bound = self._plant_issue("431", lane="next",
                                  branch="codex/next/issue-431",
                                  status="waiting-review",
                                  run_id="run-bound431")
        clean = self._plant_issue("100")
        other = self._plant_issue("101", lane="main",
                                  branch="codex/maint/something-else")
        running = self._plant_issue("102", lane="main",
                                    branch="codex/maint/none-none",
                                    status="running")
        out = sched.repair_version_bindings("d1")
        self.assertEqual(out["issue_repairs"], [])
        for key in (bound, clean, other, running):
            row = [r for r in sched.load("d1")["queue"]
                   if r["key"] == key][0]
            self.assertIn("key", row)

    def test_restored_key_collision_blocks(self):
        st = sched.load("d1")
        st["queue"].append({"kind": "dep-update", "repo": "d1", "dep": "react",
                            "target": "19.3.0", "channel": "stable",
                            "lane": "main", "branch": "codex/maint/react-19-3-0",
                            "status": "queued",
                            "key": "d1+react+19.3.0+stable"})
        sched.save(st)
        out = sched.repair_version_bindings("d1")
        self.assertTrue(any("ambiguous duplicate" in s.get("reason", "")
                            for s in out["skipped"]))
        self.assertEqual(out["repairs"], [])


class SchedulerGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        repos.save({"name": "g2", "github": "e/g2", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["g2"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)

    def test_paused_repo_never_executes(self):
        sched.configure("g2", enabled=True, interval_s=60)
        sched.scan("g2")
        sched.configure("g2", enabled=False)
        out = sched.run_due("g2", runner.FakeExecutor())
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "paused")

    def test_retry_backoff_delays(self):
        from unittest import mock
        sched.configure("g2", enabled=True, interval_s=60)
        sched.scan("g2")
        with mock.patch("openmainty.cli._start_run_with",
                        return_value=(1, None)):
            first = sched.run_due("g2", runner.FakeExecutor())
        self.assertTrue(first["ran"] and not first["ok"])
        st = sched.load("g2")
        self.assertIsNotNone(st["queue"][0].get("next_retry_at"))
        out = sched.run_due("g2", runner.FakeExecutor())
        self.assertFalse(out["ran"])
        self.assertTrue(out["reason"].startswith("backoff"))

    def test_quota_truthful_and_recovers(self):
        import os as _os
        sched.configure("g2", enabled=True, interval_s=60)
        sched.scan("g2")
        _os.environ["OPENMAINTY_FAKE_QUOTA"] = "triage"
        self.addCleanup(lambda: _os.environ.pop("OPENMAINTY_FAKE_QUOTA", None))
        out = sched.run_due("g2", runner.FakeExecutor())
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "quota wait")
        st = sched.load("g2")
        self.assertIsNone(st.get("current_job"))
        item = st["queue"][0]
        self.assertEqual(item["status"], "queued")
        self.assertIn("quota", (item.get("blocker") or "").lower())

    def test_uncaught_failure_clears_state(self):
        sched.configure("g2", enabled=True, interval_s=60)
        sched.scan("g2")

        class Boom(runner.FakeExecutor):
            def muse_step(self, step, prompt, workdir):
                raise RuntimeError("simulated worker crash")

        out = sched.run_due("g2", Boom())
        self.assertFalse(out["ran"])
        st = sched.load("g2")
        self.assertIsNone(st.get("current_job"))
        self.assertNotEqual(st["queue"][0].get("status"), "running")

    def test_cancel_running_flags(self):
        st = sched.configure("g2", enabled=True, interval_s=60)
        item = {"key": "k1", "kind": "dep-update", "repo": "g2",
                "status": "running"}
        st["queue"].append(item)
        sched.save(st)
        out = sched.cancel("g2", "k1")
        self.assertEqual(out["cancelled"], "k1")
        self.assertTrue(sched.load("g2")["queue"][0].get("cancel_requested"))

    def test_same_run_resumed_on_retry(self):
        sched.configure("g2", enabled=True, interval_s=60)
        sched.scan("g2")
        first = sched.run_due("g2", runner.FakeExecutor())
        self.assertTrue(first["ran"])
        run_id = first["item"]["run_id"]
        self.assertTrue(run_id)
        # Simulate a crash: item back to queued with the same run identity.
        st = sched.load("g2")
        item = next(i for i in st["queue"] if i["key"] == first["item"]["key"])
        item["status"] = "queued"
        item["next_retry_at"] = None
        sched.save(st)
        runs_before = {r["id"] for r in store.list_runs(100)}
        second = sched.run_due("g2", runner.FakeExecutor())
        self.assertTrue(second["ran"])
        self.assertEqual(second["item"]["run_id"], run_id)
        runs_after = {r["id"] for r in store.list_runs(100)}
        self.assertEqual(runs_before, runs_after)

    def test_lock_owner_and_stale_reclaim(self):
        try:
            Path(sched._lock_file()).unlink()
        except OSError:
            pass
        mine = sched._acquire_global_lock("owner-a")
        self.assertEqual(mine, "owner-a")
        self.assertIsNone(sched._acquire_global_lock("owner-b"))
        # Non-owner cannot release.
        sched._release_global_lock("owner-b")
        self.assertIsNone(sched._acquire_global_lock("owner-b"))
        sched._release_global_lock("owner-a")
        self.assertEqual(sched._acquire_global_lock("owner-b"), "owner-b")
        self.assertTrue(sched.renew_global_lock("owner-b"))
        self.assertFalse(sched.renew_global_lock("owner-a"))
        sched._release_global_lock("owner-b")

    def test_separate_process_overlap(self):
        import subprocess as _sp
        try:
            Path(sched._lock_file()).unlink()
        except OSError:
            pass
        home = os.environ["OPENMAINTY_HOME"]
        src = str(Path(__file__).resolve().parent.parent / "src")
        holder = _sp.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r);"
             "from openmainty import sched;"
             "assert sched._acquire_global_lock('holder', ttl_s=600);"
             "import time; time.sleep(20)" % src],
            env={"PATH": "/usr/bin:/bin", "OPENMAINTY_HOME": home})
        try:
            import time as _t
            _t.sleep(1.5)
            self.assertIsNone(sched._acquire_global_lock("parent"))
        finally:
            holder.terminate()
            holder.wait(timeout=15)
        # After the holder dies the lock file is stale only after TTL;
        # reclaim path is covered by stale test below.
        try:
            Path(sched._lock_file()).unlink()
        except OSError:
            pass

    def test_daemon_tick_respects_execute_mode(self):
        sched.configure("g2", enabled=True, interval_s=60,
                        execute_enabled=False)
        sched.scan("g2")
        out = sched.daemon_tick(runner.FakeExecutor())
        self.assertEqual(out["executed"], {})
        sched.configure("g2", enabled=True, interval_s=60,
                        execute_enabled=True)
        st = sched.load("g2")
        for item in st["queue"]:
            item["next_retry_at"] = None
        sched.save(st)
        out2 = sched.daemon_tick(runner.FakeExecutor())
        self.assertIn("g2", out2["executed"])


class KeyedRunTests(unittest.TestCase):
    """Truthful dependency action: optional `key` selects exactly one
    current proposal, or refuses with a reason. Two distinct proposals
    prove exactness; every reject path must run nothing (no fallback to
    the next unrelated job). No network in these tests."""

    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        self._write_manifest({"alpha": "1.0.0", "beta": "1.0.0"})
        repos.save({"name": "kk", "github": "e/kk", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["kk"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: {"alpha": "2.0.0",
                                                "beta": "3.0.0"}[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)

    def _write_manifest(self, dependencies: dict):
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": dependencies}))

    def _scan2(self):
        sched.configure("kk", enabled=True, interval_s=60)
        out = sched.scan("kk")
        self.assertEqual(out["queued_added"], 2)
        st = sched.load("kk")
        keys = {i["dep"]: i["key"] for i in st["queue"]}
        self.assertEqual(set(keys), {"alpha", "beta"})
        return keys

    def _runs_for(self):
        return [r for r in store.list_runs(200) if r.get("repo") == "kk"]

    def test_key_runs_exactly_that_proposal(self):
        keys = self._scan2()
        runs_before = len(self._runs_for())
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["beta"])
        self.assertTrue(out["ran"], out)
        self.assertEqual(out["item"]["key"], keys["beta"])
        run_id = out["item"]["run_id"]
        self.assertTrue(run_id)
        self.assertIn("beta", store.load(run_id)["title"])
        st = sched.load("kk")
        by_dep = {i["dep"]: i for i in st["queue"]}
        # The chosen proposal owns the new run ...
        self.assertEqual(by_dep["beta"]["run_id"], run_id)
        # ... and the other distinct proposal is untouched, never executed.
        self.assertEqual(by_dep["alpha"]["status"], "queued")
        self.assertIsNone(by_dep["alpha"].get("run_id"))
        self.assertEqual(len(self._runs_for()), runs_before + 1)

    def test_unknown_key_refuses_without_fallback(self):
        self._scan2()
        out = sched.run_due("kk", runner.FakeExecutor(),
                            key="kk+nope+9.9.9+stable")
        self.assertFalse(out["ran"])
        self.assertIn("unknown key", out["reason"])
        self.assertEqual(out["key"], "kk+nope+9.9.9+stable")
        st = sched.load("kk")
        self.assertTrue(all(i["status"] == "queued" for i in st["queue"]))
        self.assertEqual(self._runs_for(), [])

    def test_nonqueued_key_refuses_without_fallback(self):
        keys = self._scan2()
        sched.cancel("kk", keys["beta"])
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["beta"])
        self.assertFalse(out["ran"])
        self.assertIn("cancelled", out["reason"])
        by_dep = {i["dep"]: i for i in sched.load("kk")["queue"]}
        self.assertEqual(by_dep["alpha"]["status"], "queued")
        self.assertEqual(self._runs_for(), [])

    def test_finished_key_is_stale(self):
        keys = self._scan2()
        st = sched.load("kk")
        alpha = next(i for i in st["queue"] if i["dep"] == "alpha")
        st["queue"].remove(alpha)
        st["history"].append({"key": alpha["key"], "at": "t",
                              "result": "waiting-review", "run_id": None})
        sched.save(st)
        out = sched.run_due("kk", runner.FakeExecutor(), key=alpha["key"])
        self.assertFalse(out["ran"])
        self.assertIn("stale", out["reason"])
        self.assertEqual(self._runs_for(), [])

    def test_backoff_key_refuses_without_fallback(self):
        import datetime as _dt
        keys = self._scan2()
        st = sched.load("kk")
        alpha = next(i for i in st["queue"] if i["dep"] == "alpha")
        alpha["next_retry_at"] = (
            _dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(hours=1)).isoformat(timespec="seconds")
        sched.save(st)
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        self.assertFalse(out["ran"])
        self.assertIn("backoff", out["reason"])
        beta = next(i for i in sched.load("kk")["queue"]
                    if i["dep"] == "beta")
        self.assertEqual(beta["status"], "queued")
        self.assertEqual(self._runs_for(), [])

    def test_paused_repo_refuses_keyed_run(self):
        keys = self._scan2()
        sched.configure("kk", enabled=False)
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "paused")
        self.assertEqual(out["key"], keys["alpha"])
        self.assertEqual(self._runs_for(), [])

    def test_local_declaration_is_stale_cancelled_not_executed(self):
        keys = self._scan2()
        self._write_manifest({"alpha": "file:../alpha", "beta": "1.0.0"})
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        self.assertFalse(out["ran"])
        self.assertIn("local/workspace", out["reason"])
        st = sched.load("kk")
        by_dep = {i["dep"]: i for i in st["queue"]}
        # Non-destructive: the record stays cancelled with reason + event.
        self.assertEqual(by_dep["alpha"]["status"], "cancelled")
        self.assertIn("local/workspace", by_dep["alpha"].get("note", ""))
        self.assertTrue(any(h["key"] == keys["alpha"]
                            for h in st["history"]))
        self.assertTrue(any(e.get("ref") == keys["alpha"]
                            and e.get("kind") == "cancelled"
                            for e in events.list_events("kk", limit=50)))
        self.assertEqual(by_dep["beta"]["status"], "queued")
        self.assertEqual(self._runs_for(), [])

    def test_workspace_lock_resolution_is_stale_cancelled(self):
        keys = self._scan2()
        # Declared text stays `*`, but the resolver now says workspace —
        # the exact self-upgrade failure, proven by the lock, not text.
        self._write_manifest({"alpha": "*", "beta": "1.0.0"})
        (self.co / "yarn.lock").write_text(
            '"alpha@npm:*, alpha@workspace:.":\n'
            "  version: 0.0.0-use.local\n"
            '  resolution: "alpha@workspace:."\n')
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        self.assertFalse(out["ran"], out)
        self.assertIn("local/workspace", out["reason"])
        by_dep = {i["dep"]: i for i in sched.load("kk")["queue"]}
        self.assertEqual(by_dep["alpha"]["status"], "cancelled")
        self.assertEqual(by_dep["beta"]["status"], "queued")
        self.assertEqual(self._runs_for(), [])

    def test_up_to_date_is_stale_cancelled(self):
        keys = self._scan2()
        (self.co / "yarn.lock").write_text(
            '"alpha@npm:1.0.0":\n  version: 2.0.0\n')
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        self.assertFalse(out["ran"])
        self.assertIn("already at 2.0.0", out["reason"])
        by_dep = {i["dep"]: i for i in sched.load("kk")["queue"]}
        self.assertEqual(by_dep["alpha"]["status"], "cancelled")
        self.assertEqual(self._runs_for(), [])

    def test_removed_declaration_is_stale_cancelled(self):
        keys = self._scan2()
        self._write_manifest({"beta": "1.0.0"})
        out = sched.run_due("kk", runner.FakeExecutor(), key=keys["alpha"])
        self.assertFalse(out["ran"])
        self.assertIn("no longer declared", out["reason"])
        by_dep = {i["dep"]: i for i in sched.load("kk")["queue"]}
        self.assertEqual(by_dep["alpha"]["status"], "cancelled")
        self.assertEqual(self._runs_for(), [])

    def test_api_key_ignores_payload_target_and_branch(self):
        from openmainty import serve
        keys = self._scan2()
        out = serve.api_sched_run_due({"repo": "kk", "fake": True,
                                       "key": keys["alpha"],
                                       "target": "9.9.9-evil",
                                       "branch": "evil/branch"})
        self.assertTrue(out["ran"], out)
        self.assertEqual(out["item"]["key"], keys["alpha"])
        # The stored proposal wins; arbitrary payload is never trusted.
        self.assertEqual(out["item"]["target"], "2.0.0")
        self.assertTrue(out["item"]["branch"].startswith("codex/"))


class VersionRangeTests(unittest.TestCase):
    def test_stable_range_excludes_prerelease(self):
        self.assertFalse(versions.satisfies("^1.0.0", "1.5.0-rc.1"))
        self.assertFalse(versions.satisfies(">=1.0.0", "2.0.0-rc.1"))
        self.assertFalse(versions.satisfies("~1.2.0", "1.2.5-beta.0"))
        self.assertTrue(versions.satisfies("^2.0.0-next.1", "2.0.0-next.2"))

    def test_floor_of_range(self):
        self.assertEqual(versions.floor_of_range("^6.0.0"), "6.0.0")
        self.assertEqual(versions.floor_of_range(">=0.19.0 <0.21.0"), "0.19.0")
        self.assertEqual(versions.floor_of_range("~1.2.3"), "1.2.3")
        self.assertIsNone(versions.floor_of_range("workspace:*"))
        self.assertIsNone(versions.floor_of_range("hyochan/repo#v1"))
        self.assertIsNone(versions.floor_of_range("*"))

    def test_no_lock_range_still_drifts(self):
        cls = deps._classify_row("^6.0.0", None, "7.0.0")
        self.assertTrue(cls["drift"])
        self.assertEqual(cls["update_kind"], "major")
        self.assertEqual(cls["lane"], "next")

    def test_unmanaged_never_hits_registry(self):
        def _boom(kind, ident):
            raise AssertionError(f"registry must not be queried: {kind}:{ident}")
        orig = deps._latest_fn
        deps.set_latest_fn(_boom)
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            co = Path(tmp)
            (co / "package.json").write_text(json.dumps({
                "dependencies": {"local-pkg": "file:../local-pkg",
                                 "ws-pkg": "workspace:*"}}))
            rows = deps.check({"name": "x", "github": "e/x",
                               "baseBranch": "main"}, co)
        by_name = {r["name"]: r for r in rows}
        self.assertFalse(by_name["local-pkg"]["drift"])
        self.assertFalse(by_name["ws-pkg"]["drift"])
        self.assertIsNone(by_name["local-pkg"]["latest"])

    def test_berry_selectors_and_scoped(self):
        parsed = manifests._parse_yarn_lock(
            '"a@npm:^1.0.0":\n  version: 1.0.5\n  resolution: "a@npm:1.0.5"\n'
            '"a@npm:^2.0.0":\n  version: 2.1.0\n  resolution: "a@npm:2.1.0"\n'
            '"@scope/p@npm:^3.0.0":\n  version: 3.2.1\n')
        self.assertEqual(
            manifests.installed_for(
                parsed, "a", "^2.0.0"), "2.1.0")
        self.assertEqual(
            manifests.installed_for(parsed, "@scope/p", "^3.0.0"), "3.2.1")
        name, sel = manifests._split_key_name_selector(
            "@scope/p@npm:^3.0.0")
        self.assertEqual((name, sel), ("@scope/p", "^3.0.0"))


class BranchWorktreeTests(unittest.TestCase):
    """Real git: divergent main/next, named branch, dirty checkout safety."""

    def setUp(self):
        self.root = make_home(self)
        from openmainty.util import run_cmd as _run
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.assertEqual(_run(["git", "init", "-b", "main", str(self.origin)])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "config",
                               "user.email", "t@t"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "config",
                               "user.name", "t"])[0], 0)
        (self.origin / "f.txt").write_text("main\n")
        self.assertEqual(_run(["git", "-C", str(self.origin), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "commit",
                               "-m", "main1"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "branch",
                               "next", "main"])[0], 0)
        (self.origin / "f.txt").write_text("main-second\n")
        self.assertEqual(_run(["git", "-C", str(self.origin), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "commit",
                               "-m", "main2", "--", "."])[0], 0)
        # Diverge next from main1: next gets its own commit.
        self.assertEqual(_run(["git", "-C", str(self.origin), "checkout",
                               "next", "--quiet"])[0], 0)
        (self.origin / "g.txt").write_text("next-only\n")
        self.assertEqual(_run(["git", "-C", str(self.origin), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "commit",
                               "-m", "next2"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "checkout",
                               "main", "--quiet"])[0], 0)
        # Dirty the checkout: must stay untouched by worktree creation.
        (self.origin / "dirty.txt").write_text("uncommitted\n")

    def test_next_lane_branch_from_next_base(self):
        from openmainty.util import run_cmd as _run
        state = store.create("r", "1", "t", "base",
                             {"model": "m"})
        try:
            work = runner.ensure_branch_workdir(
                state, self.origin, "next", branch="codex/next/probe-1")
            code, out, _ = _run(["git", "-C", str(work), "symbolic-ref",
                                 "--short", "HEAD"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "codex/next/probe-1")
            code, base_out, _ = _run(["git", "-C", str(self.origin),
                                      "rev-parse", "next"])
            code2, merge_out, _ = _run(
                ["git", "-C", str(work), "merge-base", "HEAD", "next"])
            self.assertEqual(code2, 0)
            self.assertEqual(merge_out.strip(), base_out.strip())
            # Original dirty checkout unchanged.
            code, status, _ = _run(["git", "-C", str(self.origin),
                                    "status", "--porcelain"])
            self.assertIn("dirty.txt", status)
            code, head, _ = _run(["git", "-C", str(self.origin),
                                  "branch", "--show-current"])
            self.assertEqual(head.strip(), "main")
            # Isolated: no symlink escaping to the checkout.
            self.assertFalse((work / "node_modules").is_symlink())
        finally:
            _run(["git", "-C", str(self.origin), "worktree", "remove",
                  "--force", str(store.run_dir(state["id"]) / "work")])
            _run(["git", "-C", str(self.origin), "branch", "-D",
                  "codex/next/probe-1"])

    def test_missing_ref_fails_explicitly(self):
        state = store.create("r", "2", "t", "base",
                             {"model": "m"})
        with self.assertRaises(SystemExit) as ctx:
            runner.ensure_branch_workdir(state, self.origin, "no-such-ref",
                                         branch="codex/next/x")
        self.assertIn("missing base ref", str(ctx.exception))

    def test_isolation_removes_escaping_symlink(self):
        work = self.root / "w"
        work.mkdir()
        outside = self.root / "outside_nm"
        outside.mkdir()
        (work / "node_modules").symlink_to(outside, target_is_directory=True)
        note = runner._isolate_node_modules(work)
        self.assertIsNotNone(note)
        self.assertFalse((work / "node_modules").is_symlink())
        self.assertTrue(outside.is_dir())  # checkout itself untouched


class DivergentLaneExecutionTests(unittest.TestCase):
    """End-to-end on a git fixture with divergent main/next: lane routing,
    ensure_next, named branch worktree, persisted run association,
    restart + review-state dedup. Fake executor/registry only."""

    def setUp(self):
        self.root = make_home(self)
        from openmainty.util import run_cmd as _run
        self.co = self.root / "co"
        self.co.mkdir()
        self.assertEqual(_run(["git", "init", "-b", "main", str(self.co)])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "config",
                               "user.email", "t@t"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "config",
                               "user.name", "t"])[0], 0)
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        self.assertEqual(_run(["git", "-C", str(self.co), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "commit",
                               "-m", "main1"])[0], 0)
        # Divergent next: branched at main1, main moves on to main2.
        self.assertEqual(_run(["git", "-C", str(self.co), "branch",
                               "next"])[0], 0)
        (self.co / "other.txt").write_text("diverge\n")
        self.assertEqual(_run(["git", "-C", str(self.co), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "commit",
                               "-m", "main2"])[0], 0)
        repos.save({"name": "e2e", "github": "e/e2e", "baseBranch": "main",
                    "knowledge": [],
                    "checks": [{"name": "t", "cmd": ["echo", "hi"],
                                "requires": []}],
                    "e2e": [], "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["e2e"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)

    def tearDown(self):
        from openmainty.util import run_cmd as _run
        for run in store.list_runs(200):
            work = store.run_dir(run["id"]) / "work"
            if work.exists():
                _run(["git", "-C", str(self.co), "worktree", "remove",
                      "--force", str(work)])
        for branch in branches.local_branches(self.co):
            if branch.startswith("codex/"):
                _run(["git", "-C", str(self.co), "branch", "-D", branch])
        if branches.has_next(self.co):
            _run(["git", "-C", str(self.co), "branch", "-D", "next"])

    def test_next_lane_executes_on_next_with_named_branch(self):
        from openmainty.util import run_cmd as _run
        sched.configure("e2e", enabled=True, interval_s=60)
        scan = sched.scan("e2e")
        self.assertEqual(scan["queued_added"], 1)
        out = sched.run_due("e2e", runner.FakeExecutor())
        self.assertTrue(out["ran"] and out["ok"])
        item = out["item"]
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["lane"], "next")
        self.assertTrue(item["branch"].startswith("codex/next/"))
        # Local next was created (no push), run records the exact strategy.
        self.assertTrue(branches.has_next(self.co))
        run = store.load(item["run_id"])
        self.assertEqual(run["lane"], "next")
        self.assertEqual(run["branch"], item["branch"])
        self.assertEqual(run.get("base_branch"), "next")
        self.assertEqual(run["queue_key"], item["key"])
        # Worktree really sits on the named branch off next.
        work = store.run_dir(run["id"]) / "work"
        code, sym, _ = _run(["git", "-C", str(work), "symbolic-ref",
                             "--short", "HEAD"])
        self.assertEqual(sym.strip(), item["branch"])
        code, next_rev, _ = _run(["git", "-C", str(self.co),
                                  "rev-parse", "next"])
        code, main_rev, _ = _run(["git", "-C", str(self.co),
                                  "rev-parse", "main"])
        self.assertNotEqual(next_rev.strip(), main_rev.strip())
        code, mb, _ = _run(["git", "-C", str(work), "merge-base",
                            "HEAD", "next"])
        self.assertEqual(mb.strip(), next_rev.strip())
        # Original checkout untouched (still main, no new branches checked).
        code, head, _ = _run(["git", "-C", str(self.co),
                              "branch", "--show-current"])
        self.assertEqual(head.strip(), "main")
        # Restart + review-state dedup: rescan adds nothing.
        again = sched.scan("e2e")
        self.assertEqual(again["queued_added"], 0)
        st = sched.load("e2e")
        keys = [i["key"] for i in st["queue"]]
        self.assertEqual(len(keys), len(set(keys)))
        # Graph links queue -> run -> branch with live endpoints.
        g = graph.build("e2e")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn(f"run:{run['id']}", ids)
        self.assertIn(f"branch:e2e:{item['branch']}", ids)
        self.assertTrue(any(e["kind"] == "executed-as" for e in g["edges"]))


class GraphIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        repos.save({"name": "h1", "github": "owner/h1", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["h1"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)

    def test_all_edges_have_endpoints_and_scoped_ids(self):
        sched.configure("h1", enabled=True, interval_s=60)
        sched.scan("h1")
        sched.run_due("h1", runner.FakeExecutor())
        g = graph.build("h1")
        ids = {n["id"] for n in g["nodes"]}
        for e in g["edges"]:
            self.assertIn(e["from"], ids, f"missing endpoint {e['from']}")
            self.assertIn(e["to"], ids, f"missing endpoint {e['to']}")
            self.assertTrue(e.get("provenance"), "edge without provenance")
        branches_seen = [n["id"] for n in g["nodes"] if n["type"] == "branch"]
        for b in branches_seen:
            self.assertTrue(b.startswith("branch:h1:"),
                            f"branch id not repo-scoped: {b}")
        issues = [n["id"] for n in g["nodes"] if n["type"] == "issue"]
        for i in issues:
            self.assertTrue(i.startswith("issue:owner/h1#") or i.startswith("issue:"),
                            f"issue id not scoped: {i}")

    def test_no_stale_verified_or_answered_blocked(self):
        sched.configure("h1", enabled=True, interval_s=60)
        sched.scan("h1")
        out = sched.run_due("h1", runner.FakeExecutor())
        run_id = out["item"]["run_id"]
        state = store.load(run_id)
        # The fixture run verified review against the live worktree bytes:
        # prove the test is not vacuous before aging the source.
        fresh_ids = {n["id"] for n in graph.build("h1")["nodes"]
                     if n["type"] == "verification"}
        self.assertTrue(any(":review" in i for i in fresh_ids),
                        "review must render verified before the edit")
        # Age the verification the honest way: edit REAL bytes in the run
        # worktree (same HEAD, different content). Read paths validate the
        # live bytes, so the graph must drop the proof before any resume.
        work = store.run_dir(run_id) / "work"
        victim = work / "package.json"
        victim.write_text(victim.read_text(encoding="utf-8") + "\n",
                          encoding="utf-8")
        g = graph.build("h1")
        verified = [n for n in g["nodes"] if n["type"] == "verification"]
        for v in verified:
            self.assertNotIn("review", v["id"],
                             "stale review must not render verified")
        # Answered question history must not render blocked.
        state["interventions"].append({"at": "t", "kind": "question",
                                       "detail": "old q"})
        state.setdefault("answers", []).append({"q": "old q", "a": "done"})
        store.save(state)
        g2 = graph.build("h1")
        blocked_q = [n for n in g2["nodes"] if n["type"] == "question"]
        self.assertEqual(blocked_q, [])


class EventsTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def test_append_and_list_survive(self):
        events.append("r1", "scan", "", "scan done", "3 deps")
        evs = events.list_events("r1")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["kind"], "scan")

    def test_malformed_lines_skipped(self):
        f = events._file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write('{"at":"t","repo":"r1","kind":"ok"}\nnot json\n')
        self.assertEqual(len(events.list_events("r1")), 1)


class GlobalFlockTests(unittest.TestCase):
    """Advisory flock held during execution: no steal of a live lock,
    crash releases immediately, heartbeat renews, contention re-queues."""

    def setUp(self):
        self.root = make_home(self)
        repos.save({"name": "fl", "github": "e/fl", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        pc = pcconfig.load()
        pc["checkouts"]["fl"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)
        sched.configure("fl", enabled=True, interval_s=60)

    def tearDown(self):
        sched._release_global_lock(None)

    def test_empty_lockfile_never_stealable(self):
        mine = sched._acquire_global_lock("holder-a")
        self.assertEqual(mine, "holder-a")
        try:
            # Freshly created lock, metadata not yet written (or wiped):
            # a live holder must still refuse the contender.
            sched._lock_file().write_text("", encoding="utf-8")
            self.assertIsNone(sched._acquire_global_lock("second-worker"))
        finally:
            sched._release_global_lock("holder-a")

    def test_racing_reclaimers_single_winner(self):
        import threading as _th
        try:
            sched._lock_file().unlink()
        except OSError:
            pass
        try:
            sched._flock_file().unlink()
        except OSError:
            pass
        winners = []
        barrier = _th.Barrier(2)

        def _grab(name):
            barrier.wait(timeout=10)
            if sched._acquire_global_lock(name) is not None:
                winners.append(name)

        threads = [_th.Thread(target=_grab, args=(f"racer-{i}",))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(len(winners), 1)
        sched._release_global_lock(winners[0])

    def test_killed_holder_releases_immediately(self):
        import subprocess as _sp
        src = str(Path(__file__).resolve().parent.parent / "src")
        home = os.environ["OPENMAINTY_HOME"]
        holder = _sp.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, %r);"
             "from openmainty import sched;"
             "assert sched._acquire_global_lock('holder');"
             "time.sleep(60)" % src],
            env={"PATH": "/usr/bin:/bin", "OPENMAINTY_HOME": home})
        try:
            import time as _t
            _t.sleep(1.5)
            self.assertIsNone(sched._acquire_global_lock("parent"))
        finally:
            holder.kill()
            holder.wait(timeout=15)
        import time as _t
        got = None
        for _ in range(50):
            got = sched._acquire_global_lock("parent")
            if got is not None:
                break
            _t.sleep(0.2)
        self.assertEqual(got, "parent")
        sched._release_global_lock("parent")

    def test_heartbeat_renews_during_hold(self):
        import time as _t
        owner = sched._acquire_global_lock("beats")
        self.assertEqual(owner, "beats")
        try:
            before = sched._read_lock()["heartbeat"]
            sched._start_heartbeat("beats", interval_s=0.2)
            _t.sleep(0.7)
            after = sched._read_lock()["heartbeat"]
            self.assertGreater(after, before)
            self.assertTrue(sched.renew_global_lock("beats"))
            self.assertFalse(sched.renew_global_lock("someone-else"))
        finally:
            sched._release_global_lock("beats")

    def test_busy_worker_stays_queued_not_blocked(self):
        sched.scan("fl")
        sched._acquire_global_lock("other-holder")
        try:
            out = sched.run_due("fl", runner.FakeExecutor())
        finally:
            sched._release_global_lock("other-holder")
        self.assertFalse(out["ran"])
        self.assertIn("re-queued", out["reason"])
        st = sched.load("fl")
        item = st["queue"][0]
        self.assertEqual(item["status"], "queued")
        self.assertIsNotNone(item.get("next_retry_at"))


class CrashIdentityTests(unittest.TestCase):
    """run.json identity persists BEFORE the first worker step, so a
    kill mid-step resumes the same run/worktree/branch/attempts."""

    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        repos.save({"name": "ci", "github": "e/ci", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["ci"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)
        sched.configure("ci", enabled=True, interval_s=60)

    def tearDown(self):
        sched._release_global_lock(None)

    def test_crash_during_first_step_keeps_identity(self):
        sched.scan("ci")

        class CrashFirst(runner.FakeExecutor):
            def muse_step(self, step, prompt, workdir):
                raise RuntimeError("killed during first worker step")

        out = sched.run_due("ci", CrashFirst())
        self.assertFalse(out["ran"])
        st = sched.load("ci")
        item = st["queue"][0]
        # Identity survived the crash even though execute never returned.
        self.assertIsNotNone(item.get("run_id"))
        run_id = item["run_id"]
        state = store.load(run_id)
        self.assertEqual(state["branch"], item["branch"])
        self.assertIsNotNone(item.get("run_issue"))
        # Restart resumes the SAME run/worktree/branch, no duplicate.
        runs_before = {r["id"] for r in store.list_runs(200)}
        item["next_retry_at"] = None
        sched.save(st)
        second = sched.run_due("ci", runner.FakeExecutor())
        self.assertTrue(second["ran"])
        self.assertEqual(second["item"]["run_id"], run_id)
        self.assertEqual({r["id"] for r in store.list_runs(200)},
                         runs_before)

    def test_quota_keeps_identity_on_real_path(self):
        """Quota through the non-fake executor keeps the same run id."""
        sched.scan("ci")

        class RealQuota(runner.Executor):
            kind = "muse"

            def muse_step_stream(self, step, prompt, workdir, on_delta,
                                 timeout=1200):
                raise runner.QuotaHit("fake quota exhausted")

        out = sched.run_due("ci", RealQuota())
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "quota wait")
        st = sched.load("ci")
        item = st["queue"][0]
        self.assertEqual(item["status"], "queued")
        self.assertIsNotNone(item.get("run_id"))


class ExactLockTests(unittest.TestCase):
    """Worktree lock evidence is exact selector/version, never substring."""

    def setUp(self):
        self.root = make_home(self)

    def _run_with_work(self, files: dict) -> str:
        state = store.create("lr", "1", "t", "base", {"model": "m"})
        work = store.run_dir(state["id"]) / "work"
        work.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (work / name).write_text(text, encoding="utf-8")
        return state["id"]

    def test_substring_version_is_not_evidence(self):
        run_id = self._run_with_work({
            "package.json": json.dumps({
                "dependencies": {"left-pad": "^1.0.0"}}),
            # Target string appears for an UNRELATED package only.
            "yarn.lock": ('"unrelated@npm:^9.9.9":\n  version: 1.1.0\n'
                          '"left-pad@npm:^1.0.0":\n  version: 1.0.0\n'),
        })
        bad = sched._lock_mismatch(run_id, "left-pad", "1.1.0", "^1.0.0")
        self.assertIsNotNone(bad)
        self.assertIn("1.0.0", bad)

    def test_missing_lock_is_blocked_not_success(self):
        run_id = self._run_with_work({
            "package.json": json.dumps({
                "dependencies": {"left-pad": "^1.0.0"}}),
        })
        bad = sched._lock_mismatch(run_id, "left-pad", "1.1.0", "^1.0.0")
        self.assertIsNotNone(bad)
        self.assertIn("no lockfile entry", bad)

    def test_exact_selector_match_passes(self):
        run_id = self._run_with_work({
            "package.json": json.dumps({
                "dependencies": {"left-pad": "^1.0.0"}}),
            "yarn.lock": '"left-pad@npm:^1.0.0":\n  version: 1.1.0\n',
        })
        self.assertIsNone(sched._lock_mismatch(run_id, "left-pad", "1.1.0",
                                               "^1.0.0"))

    def test_undeclared_dep_is_mismatch(self):
        run_id = self._run_with_work({
            "package.json": json.dumps({"dependencies": {}}),
            "yarn.lock": '"left-pad@npm:^1.0.0":\n  version: 1.1.0\n',
        })
        bad = sched._lock_mismatch(run_id, "left-pad", "1.1.0", "^1.0.0")
        self.assertIsNotNone(bad)
        self.assertIn("not declared", bad)

    def test_scoped_name_requires_full_name(self):
        run_id = self._run_with_work({
            "package.json": json.dumps({
                "dependencies": {"@scope/pkg": "^3.0.0"}}),
            # Bare 'pkg' entry must not satisfy '@scope/pkg'.
            "yarn.lock": '"pkg@npm:^3.0.0":\n  version: 3.2.1\n',
        })
        bad = sched._lock_mismatch(run_id, "@scope/pkg", "3.2.1", "^3.0.0")
        self.assertIsNotNone(bad)


class CancelRetryTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        repos.save({"name": "cr", "github": "e/cr", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["cr"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)
        sched.configure("cr", enabled=True, interval_s=60)

    def tearDown(self):
        sched._release_global_lock(None)

    def test_uncaught_error_increments_and_blocks_bounded(self):
        sched.scan("cr")

        class Boom(runner.FakeExecutor):
            def muse_step(self, step, prompt, workdir):
                raise RuntimeError("boom")

        for _ in range(3):
            sched.run_due("cr", Boom())
            st = sched.load("cr")
            for item in st["queue"]:
                if item.get("status") == "queued":
                    item["next_retry_at"] = None
            sched.save(st)
        st = sched.load("cr")
        item = st["queue"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertEqual(item["attempts"], 3)

    def test_cancel_flag_honored_at_finalize(self):
        sched.scan("cr")
        st = sched.load("cr")
        key = st["queue"][0]["key"]
        st["queue"][0]["status"] = "running"
        st["queue"][0]["cancel_requested"] = True
        st["current_job"] = {"id": key, "run_id": None,
                             "started_at": "t", "item": dict(st["queue"][0])}
        sched.save(st)
        out = sched._finalize("cr", key, "error", detail="late failure")
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "cancelled by operator")
        self.assertEqual(sched.load("cr")["queue"][0]["status"], "cancelled")

    def test_dispatch_tick_recorded_separately_from_scan(self):
        out = sched.daemon_tick(runner.FakeExecutor())
        self.assertIn("dispatch", out)
        self.assertIsNotNone(out["dispatch"]["last_tick"])
        disp = sched.dispatch_status()
        self.assertEqual(disp["last_tick"], out["dispatch"]["last_tick"])


class PeerBranchTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def test_peer_conflict_forces_next_lane(self):
        rows = [
            {"name": "react", "kind": "npm", "id": "react",
             "scope": "dependencies", "declared": "^19.2.0",
             "installed": "19.2.0", "latest": "19.3.0",
             "drift": True, "update_kind": "patch", "lane": "main"},
            {"name": "react", "kind": "npm", "id": "react",
             "scope": "peerDependencies", "declared": "19.2.0",
             "installed": None, "latest": None,
             "drift": False, "update_kind": "unknown", "lane": "main"},
        ]
        deps._apply_peer_conflicts(rows)
        main_row = next(r for r in rows if "peer" not in r["scope"])
        self.assertEqual(main_row["lane"], "next")
        self.assertIn("peer", main_row["reason"].lower())

    def test_ensure_next_prefers_remote(self):
        from openmainty.util import run_cmd as _run
        origin = self.root / "origin"
        origin.mkdir()
        self.assertEqual(_run(["git", "init", "-b", "main", str(origin)])[0], 0)
        self.assertEqual(_run(["git", "-C", str(origin), "config",
                               "user.email", "t@t"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(origin), "config",
                               "user.name", "t"])[0], 0)
        (origin / "f.txt").write_text("v1\n")
        self.assertEqual(_run(["git", "-C", str(origin), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(origin), "commit",
                               "-m", "init"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(origin), "branch",
                               "next"])[0], 0)
        clone = self.root / "clone"
        self.assertEqual(_run(["git", "clone", str(origin),
                               str(clone)])[0], 0)
        made = branches.ensure_next(clone, base="main")
        self.assertTrue(made["created"])
        self.assertIn("origin/next", made["reason"])
        code, rev, _ = _run(["git", "-C", str(clone), "rev-parse", "next"])
        self.assertEqual(code, 0)
        code, orev, _ = _run(["git", "-C", str(clone), "rev-parse",
                              "origin/next"])
        self.assertEqual(rev.strip(), orev.strip())


class PubIgnoredLockRecheckTests(unittest.TestCase):
    """Tracked consumer lock already at target + ignored root lag: the
    queued proposal is proven stale and cancelled (same identity, reason
    kept, run association kept) — never executed, never retried."""

    LOCK = ("packages:\n  intl:\n    dependency: \"direct main\"\n"
            "    description:\n      name: intl\n"
            "      sha256: \"abc\"\n      url: \"https://pub.dev\"\n"
            "    source: hosted\n    version: \"%s\"\n")

    def setUp(self):
        self.root = make_home(self)
        from openmainty.util import run_cmd as _run
        self._run = _run
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "pubspec.yaml").write_text(
            "environment:\n  sdk: '>=3.8.0 <4.0.0'\n"
            "dependencies:\n  intl: '>=0.19.0 <0.21.0'\n")
        (self.co / "pubspec.lock").write_text(self.LOCK % "0.19.0")
        (self.co / ".gitignore").write_text("/pubspec.lock\n")
        (self.co / "example").mkdir()
        (self.co / "example" / "pubspec.yaml").write_text(
            "dependencies:\n  intl: ^0.20.3\n")
        (self.co / "example" / "pubspec.lock").write_text(
            self.LOCK % "0.20.3")
        self.assertEqual(_run(["git", "init", "-b", "main",
                               str(self.co)])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "config",
                               "user.email", "t@t"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "config",
                               "user.name", "t"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "commit",
                               "-m", "init"])[0], 0)
        repos.save({"name": "cal", "github": "e/cal", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["cal"] = str(self.co)
        pcconfig.save(pc)
        sched.configure("cal", enabled=True, interval_s=60)

    def _queue_intl(self):
        key = "flutter_calendar_carousel+intl+0.20.3+stable"
        st = sched.load("cal")
        st["queue"].append({"kind": "dep-update", "repo": "cal",
                            "dep": "intl", "target": "0.20.3",
                            "lane": "next",
                            "branch": "codex/next/intl-0-20-3",
                            "update_kind": "lock-refresh",
                            "status": "queued", "attempts": 0,
                            "next_retry_at": None, "run_id": None,
                            "key": key, "created_at": "t"})
        sched.save(st)
        return key

    def test_tracked_current_ignored_lag_is_stale_cancelled(self):
        key = self._queue_intl()
        out = sched.run_due("cal", runner.FakeExecutor(), key=key)
        self.assertFalse(out["ran"], out)
        self.assertIn("tracked locks", out["reason"])
        st = sched.load("cal")
        item = next(i for i in st["queue"] if i.get("key") == key)
        self.assertEqual(item["status"], "cancelled")
        self.assertIn("tracked locks", item.get("note", ""))
        self.assertTrue(any(h["key"] == key for h in st["history"]))
        self.assertEqual([r for r in store.list_runs(200)
                          if r.get("repo") == "cal"], [])

    def test_scan_queues_no_source_job_for_ignored_lag(self):
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "0.20.3"
                           if ident == "intl" else "3.10.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)
        out = sched.scan("cal")
        queued = [i for i in sched.load("cal")["queue"]
                  if (i.get("dep") or "") == "intl"]
        self.assertEqual(queued, [])
        self.assertTrue(any(r["name"] == "intl" and r.get("latest") == "0.20.3"
                            for r in out["rows"]))


class NextSeedFreshTests(unittest.TestCase):
    """NEW next seeds from the freshly fetched origin/main, never the
    stale local main. An existing next with no unique commits fast-forwards
    only when safe (not checked out); unique history is preserved, the
    user's main checkout is untouched, nothing is force-rewritten."""

    def setUp(self):
        self.root = make_home(self)
        from openmainty.util import run_cmd as _run
        self._run = _run
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.assertEqual(_run(["git", "init", "-b", "main",
                               str(self.origin)])[0], 0)
        for key, val in (("user.email", "t@t"), ("user.name", "t")):
            self.assertEqual(_run(["git", "-C", str(self.origin), "config",
                                   key, val])[0], 0)
        (self.origin / "f.txt").write_text("v1\n")
        self.assertEqual(_run(["git", "-C", str(self.origin), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.origin), "commit",
                               "-m", "v1"])[0], 0)
        self.clone = self.root / "clone"
        self.assertEqual(_run(["git", "clone", str(self.origin),
                               str(self.clone)])[0], 0)
        for key, val in (("user.email", "t@t"), ("user.name", "t")):
            self.assertEqual(_run(["git", "-C", str(self.clone), "config",
                                   key, val])[0], 0)

    def _rev(self, repo, ref):
        code, out, _ = self._run(["git", "-C", str(repo), "rev-parse", ref])
        self.assertEqual(code, 0)
        return out.strip()

    def _advance_origin(self, text="v2\n"):
        (self.origin / "f.txt").write_text(text)
        self.assertEqual(self._run(["git", "-C", str(self.origin), "add",
                                    "."])[0], 0)
        self.assertEqual(self._run(["git", "-C", str(self.origin), "commit",
                                    "-m", "v2"])[0], 0)
        self.assertEqual(self._run(["git", "-C", str(self.clone), "fetch",
                                    "origin", "--prune", "--quiet"])[0], 0)

    def test_new_next_seeds_from_fresh_origin_main(self):
        self._advance_origin()
        stale_main = self._rev(self.clone, "main")
        fresh = self._rev(self.clone, "origin/main")
        self.assertNotEqual(stale_main, fresh)
        made = branches.ensure_next(self.clone, base="main")
        self.assertTrue(made["created"])
        self.assertIn("origin/main", made["reason"])
        self.assertEqual(self._rev(self.clone, "next"), fresh)
        # The user's local main checkout is untouched.
        self.assertEqual(self._rev(self.clone, "main"), stale_main)

    def test_existing_next_without_unique_commits_fast_forwards(self):
        made = branches.ensure_next(self.clone, base="main")
        self.assertTrue(made["created"])
        before = self._rev(self.clone, "next")
        self.assertEqual(before, self._rev(self.clone, "main"))
        # Owned feature branch off the old base (history that must survive).
        self.assertEqual(self._run(["git", "-C", str(self.clone), "branch",
                                    "codex/next/feat", "next"])[0], 0)
        feat = self._rev(self.clone, "codex/next/feat")
        self._advance_origin()
        fresh = self._rev(self.clone, "origin/main")
        ff = branches.maybe_fast_forward_next(self.clone,
                                              main_branch="main")
        self.assertTrue(ff["advanced"])
        self.assertEqual(ff["before"], before)
        self.assertEqual(ff["after"], fresh)
        self.assertEqual(self._rev(self.clone, "next"), fresh)
        # No rewrite: the feature branch and the user's main are untouched.
        self.assertEqual(self._rev(self.clone, "codex/next/feat"), feat)
        self.assertEqual(self._rev(self.clone, "main"), before)

    def test_diverged_next_is_preserved_with_integration_need(self):
        self.assertEqual(self._run(["git", "-C", str(self.clone), "branch",
                                    "next", "main"])[0], 0)
        self.assertEqual(self._run(["git", "-C", str(self.clone), "checkout",
                                    "-q", "next"])[0], 0)
        (self.clone / "local.txt").write_text("unique\n")
        self.assertEqual(self._run(["git", "-C", str(self.clone), "add",
                                    "."])[0], 0)
        self.assertEqual(self._run(["git", "-C", str(self.clone), "commit",
                                    "-m", "unique"])[0], 0)
        self.assertEqual(self._run(["git", "-C", str(self.clone), "checkout",
                                    "-q", "main"])[0], 0)
        unique = self._rev(self.clone, "next")
        self._advance_origin()
        fresh = self._rev(self.clone, "origin/main")
        ff = branches.maybe_fast_forward_next(self.clone,
                                              main_branch="main")
        self.assertFalse(ff["advanced"])
        self.assertTrue(ff.get("diverged"))
        self.assertIn("needs integration", ff["reason"])
        self.assertEqual(self._rev(self.clone, "next"), unique)
        self.assertEqual(self._rev(self.clone, "origin/main"), fresh)

    def test_checked_out_next_is_never_modified(self):
        self.assertEqual(self._run(["git", "-C", str(self.clone), "branch",
                                    "next", "main"])[0], 0)
        before = self._rev(self.clone, "next")
        wt = self.root / "wt-next"
        self.assertEqual(self._run(["git", "-C", str(self.clone), "worktree",
                                    "add", str(wt), "next"])[0], 0)
        self.addCleanup(lambda: self._run(
            ["git", "-C", str(self.clone), "worktree", "remove", "--force",
             str(wt)]))
        self._advance_origin()
        ff = branches.maybe_fast_forward_next(self.clone,
                                              main_branch="main")
        self.assertFalse(ff["advanced"])
        self.assertIn("checked out", ff["reason"])
        self.assertEqual(self._rev(self.clone, "next"), before)


class DepUpgradeEvidenceTests(unittest.TestCase):
    """A dep-update run whose own diff holds no lock/manifest change is
    test/plan-only noise: it settles no-code (same identity, history
    kept), never as a verified done-ok upgrade."""

    def setUp(self):
        self.root = make_home(self)
        from openmainty.util import run_cmd as _run
        self._run = _run
        self.co = self.root / "co"
        self.co.mkdir()
        self.assertEqual(_run(["git", "init", "-b", "main", str(self.co)])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "config",
                               "user.email", "t@t"])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "config",
                               "user.name", "t"])[0], 0)

    def _commit(self, relpath, text, message):
        _run = self._run
        target = self.co / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.assertEqual(_run(["git", "-C", str(self.co), "add", "."])[0], 0)
        self.assertEqual(_run(["git", "-C", str(self.co), "commit",
                               "-m", message])[0], 0)
        code, rev, _ = _run(["git", "-C", str(self.co), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        return rev.strip()

    def tearDown(self):
        from openmainty.util import run_cmd as _run
        for run in store.list_runs(200):
            work = store.run_dir(run["id"]) / "work"
            if work.exists():
                _run(["git", "-C", str(self.co), "worktree", "remove",
                      "--force", str(work)])

    def test_lock_only_diff_is_evidence(self):
        base = self._commit("package.json",
                            json.dumps({"dependencies": {"left-pad": "1.0.0"}}),
                            "base")
        self._commit("yarn.lock",
                     '"left-pad@npm:1.0.0":\n  version: 1.0.1\n', "lock bump")
        self.assertEqual(sched._dep_diff_evidence(self.co, base), ["yarn.lock"])

    def test_test_only_diff_is_no_evidence(self):
        base = self._commit("package.json",
                            json.dumps({"dependencies": {"left-pad": "1.0.0"}}),
                            "base")
        self._commit("src/__tests__/pin.test.ts", "test('x', () => {});\n",
                     "noise test")
        self._commit("verification-plan-x.md", "# plan\n", "noise plan")
        self.assertEqual(sched._dep_diff_evidence(self.co, base), [])

    def test_unprovable_diff_stays_actionable(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(sched._dep_diff_evidence(plain, "abc123"))
        base = self._commit("package.json", "{}", "base")
        self.assertIsNone(sched._dep_diff_evidence(self.co, None))
        self.assertIsNone(sched._dep_diff_evidence(self.co, "unknown"))

    def test_test_only_upgrade_settles_no_code(self):
        _run = self._run
        self._commit("package.json",
                     json.dumps({"dependencies": {"left-pad": "1.0.0"}}),
                     "base")
        repos.save({"name": "ev", "github": "e/ev", "baseBranch": "main",
                    "knowledge": [],
                    "checks": [{"name": "t", "cmd": ["echo", "hi"],
                                "requires": []}],
                    "e2e": [], "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["ev"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.0.1")
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        from unittest import mock as _mock
        _patcher = _mock.patch("openmainty.cli._open_issues", return_value=[])
        _patcher.start()
        self.addCleanup(_patcher.stop)
        sched.configure("ev", enabled=True, interval_s=60)
        scan = sched.scan("ev")
        self.assertEqual(scan["queued_added"], 1)
        # The fixture worker changes no files by construction: no lock or
        # manifest change can appear in the run diff.
        out = sched.run_due("ev", runner.FakeExecutor())
        self.assertTrue(out["ran"], out)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("outcome"), "no-code")
        item = out["item"]
        self.assertEqual(item["status"], "waiting-review")
        self.assertFalse(item.get("verified", False))
        self.assertIn("test/plan-only", item.get("note", ""))
        self.assertEqual(item.get("attempts"), 0)
        st = sched.load("ev")
        self.assertTrue(any(h.get("result") == "no-code"
                            and h.get("run_id") == item.get("run_id")
                            for h in st["history"]))
        self.assertTrue(any(e.get("kind") == "run-no-code"
                            for e in events.list_events("ev", limit=50)))


class ScanWatchDeadlockTests(unittest.TestCase):
    """scan() must complete while a watch-gated run exists.

    Regression for the runtime21 wedge: scan() held locked(repo) while
    calling check_repo_watches(), which opens the SAME repo mutex again.
    POSIX flock on a second fd self-blocks even in the same process, so
    both the isolated fixture scan and the production daemon wedged with
    two .<repo>.mtx fds (current_job null, no model work). This test runs
    the ENTIRE workload — fixture setup, scan, fake execute, watch
    settle, and the ACTUAL daemon scan path (daemon_tick -> tick ->
    scan -> check_repo_watches) — in a real subprocess under a short
    bound: pre-fix the child wedges (even the first scan self-deadlocks,
    so no lock-touching fixture step may run in the parent) and the
    bounded wait fails the test; post-fix it completes in seconds and
    preserves the watch identity. The repo mutex itself is never mocked
    — only registry/network fixtures.
    """

    def test_daemon_tick_completes_with_watch_present(self):
        import shutil
        import subprocess
        src = str(Path(__file__).resolve().parent.parent / "src")
        root = Path(tempfile.mkdtemp(prefix="scan-deadlock-"))
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        child = (
            "import json, sys\n"
            f"sys.path.insert(0, {src!r})\n"
            "from pathlib import Path\n"
            "from unittest import mock\n"
            "from openmainty import cli, deps, discussions, muse, pcconfig, repos, runner, sched, store\n"
            f"root = Path({str(root)!r})\n"
            "co = root / 'co'\n"
            "co.mkdir(exist_ok=True)\n"
            "(co / 'package.json').write_text(json.dumps("
            "{'dependencies': {'alpha': '1.0.0'}}))\n"
            "repos.save({'name': 'dl', 'github': 'e/dl', 'baseBranch': 'main', "
            "'knowledge': [], 'checks': [], 'e2e': [], "
            "'release': {}, 'announce': {}})\n"
            "pc = pcconfig.load()\n"
            "pc['checkouts']['dl'] = str(co)\n"
            "pcconfig.save(pc)\n"
            "deps.set_latest_fn(lambda kind, ident: '2.0.0')\n"
            "_nopatch = mock.patch.object(cli, '_open_issues', return_value=[])\n"
            "_nopatch.start()\n"
            "_pprs = mock.patch.object(cli, '_open_prs', return_value=[])\n"
            "_pprs.start()\n"
            "_mver = mock.patch.object(muse, 'version', "
            "return_value='fixture-muse-0.0.0')\n"
            "_mver.start()\n"
            "_d1 = mock.patch.object(discussions, 'scan_source', "
            "return_value={'items': [], 'status': {}})\n"
            "_d1.start()\n"
            "_d2 = mock.patch.object(discussions, 'fetch_labels', "
            "return_value={'labels': [], 'labels_complete': True, 'pages': 1})\n"
            "_d2.start()\n"
            "sched.configure('dl', enabled=True, interval_s=60)\n"
            "sched.scan('dl')\n"
            "key = sched.load('dl')['queue'][0]['key']\n"
            "out = sched.run_due('dl', runner.FakeExecutor(), key=key)\n"
            "assert out['ran'] or out.get('item'), out\n"
            "with sched.locked('dl'):\n"
            "    st = sched.load('dl')\n"
            "    item = next(i for i in st['queue'] if i['key'] == key)\n"
            "    item['attempts'] = 2\n"
            "    item['status'] = 'queued'\n"
            "    item['next_retry_at'] = None\n"
            "    sched.save(st)\n"
            "settled = sched.set_external_watch('dl', key, "
            "reason='wait for compatible peer release', "
            "evidence='fix confirmed incompatibility; manifests unchanged', "
            "trigger='compatible peer release', "
            "inputs={'registry': {'alpha': '2.0.0'}})\n"
            "assert settled['ok'], settled\n"
            "with sched.locked('dl'):\n"
            "    st = sched.load('dl')\n"
            "    st['next_scan'] = '2000-01-01T00:00:00+00:00'\n"
            "    sched.save(st)\n"
            "tick = sched.daemon_tick()\n"
            "_mver.stop()\n"
            "_d2.stop()\n"
            "_d1.stop()\n"
            "_pprs.stop()\n"
            "_nopatch.stop()\n"
            "scanned = (tick.get('scanned') or {}).get('dl') or {}\n"
            "st = sched.load('dl')\n"
            "item = next(i for i in st['queue'] if i['key'] == key)\n"
            "run = store.load(settled['run_id'])\n"
            "print(json.dumps({'watches': scanned.get('watches'), "
            "'last_scan': scanned.get('last_scan'), "
            "'key': key, 'run_id': settled['run_id'], "
            "'status': item.get('status'), 'attempts': item.get('attempts'), "
            "'item_run': item.get('run_id'), "
            "'watch_active': bool((run.get('watch') or {}).get('active'))}))\n"
        )
        env = dict(os.environ, OPENMAINTY_HOME=str(root / "home"))
        try:
            proc = subprocess.run(
                [sys.executable, "-c", child], capture_output=True, text=True,
                env=env, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("scan/daemon path wedged with a watch present "
                      "(nested repo flock self-deadlock): bounded 10s wait "
                      "expired with no completion")
            return
        self.assertEqual(proc.returncode, 0,
                         f"daemon scan path failed: {proc.stderr[-2000:]}")
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        watches = payload.get("watches") or {}
        self.assertGreaterEqual(watches.get("checked", 0), 1,
                                "the daemon scan must reconcile the watch")
        self.assertTrue(payload.get("last_scan"), "scan must record last_scan")
        # The watch reconciliation must survive the scan bookkeeping: same
        # run identity, attempts preserved, still deferred (no stale-state
        # overwrite of the queue).
        self.assertEqual(payload.get("item_run"), payload.get("run_id"))
        self.assertEqual(payload.get("attempts"), 2)
        self.assertEqual(payload.get("status"), "waiting-review")
        self.assertTrue(payload.get("watch_active"))


if __name__ == "__main__":
    unittest.main()
