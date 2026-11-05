"""S16: refresh runs build on the base batch integration head.

All offline: a fixture git checkout (main @ A, candidate @ B), a base
batch written through the production batches.import_batch (so the
lane/head field names are the real ones, never hand-copied), and a
temp OPENMAINTY_HOME on every command. Py3.9 portable.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, pcconfig, repos, runner, sched, store  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home, offline_muse_version  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_muse_version  # noqa: E402


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout.strip()


def snap_of(worktree):
    snap = runner.source_snapshot(Path(worktree))
    return {"head": snap["head"], "fingerprint": snap["fingerprint"]}


class RefreshBaseFixture(unittest.TestCase):
    """main @ A; candidate branch @ B; base batch imported for real."""

    REPO = "fakerepo"

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        # Generic refresh fixture has no real CLI version: stub metadata.
        offline_muse_version(self, "fixture-muse-0.0.0")
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"left-pad": "1.0.0", "bar": "1.0.0",
                              "foo": "9.9.8"}}))
        git("init", "-b", "main", cwd=self.co)
        git("config", "user.email", "test@localhost", cwd=self.co)
        git("config", "user.name", "test", cwd=self.co)
        git("add", "-A", cwd=self.co)
        git("commit", "-qm", "base", cwd=self.co)
        self.head_a = git("rev-parse", "HEAD", cwd=self.co)
        git("checkout", "-qb", "codex/maint/foo-9-9-9", cwd=self.co)
        (self.co / "cand.txt").write_text("candidate\n", encoding="utf-8")
        git("add", "-A", cwd=self.co)
        git("commit", "-qm", "candidate foo 9.9.9", cwd=self.co)
        self.head_b = git("rev-parse", "HEAD", cwd=self.co)
        git("checkout", "-q", "main", cwd=self.co)
        repos.save({"name": self.REPO, "github": "o/r",
                    "baseBranch": "main"})
        pc = pcconfig.load()
        pc["checkouts"][self.REPO] = str(self.co)
        pcconfig.save(pc)
        sched.configure(self.REPO, enabled=True, interval_s=60)
        # Primary binding for the base batch: a run record plus its
        # owned queue item, on a live worktree of the owned branch.
        run = store.create(self.REPO, "dep-foo-9-9-9",
                           "chore(deps): foo 9.9.8 -> 9.9.9",
                           self.head_a, {"by": "t"})
        self.base_run_id = run["id"]
        cand_wt = store.run_dir(self.base_run_id) / "work"
        cand_wt.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", str(cand_wt), "codex/maint/foo-9-9-9",
            cwd=self.co)
        cand_snap = snap_of(cand_wt)
        qst = sched.load(self.REPO)
        qst.setdefault("queue", []).append(
            {"key": "k0", "repo": self.REPO, "dep": "foo",
             "target": "9.9.9", "lane": "main",
             "branch": "codex/maint/foo-9-9-9",
             "status": "waiting-review", "run_id": self.base_run_id})
        sched.save(qst)
        self.base_wt = self.root / "base-wt"
        git("worktree", "add", "--detach", str(self.base_wt),
            self.head_b, cwd=self.co)
        base_snap = snap_of(self.base_wt)
        payload = {
            "id": "refresh-base-1", "repo": self.REPO,
            "target_branch": "main", "purpose": "S16 fixture base",
            "origin": {"checkout": str(self.co), "base_branch": "main",
                       "base_head": self.head_a},
            "primaries": [{
                "dep": "foo", "target": "9.9.9", "lane": "main",
                "run_id": self.base_run_id,
                "source": {"worktree": str(cand_wt),
                           "branch": "codex/maint/foo-9-9-9",
                           "head": cand_snap["head"],
                           "fingerprint": cand_snap["fingerprint"]},
                "review": {"verdict": "pending", "at": "2026-01-01T00:00:00Z",
                           "findings": []}}],
            "inherited": [],
            "integrations": [{
                "lane": "main", "worktree": str(self.base_wt),
                "status": "testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "inputs": {"head": base_snap["head"],
                               "fingerprint": base_snap["fingerprint"]},
                    "evidence": [{"label": "fixture note", "kind": "note",
                                  "text": "offline fixture"}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }
        # Production write path: head/fingerprint come from a live
        # snapshot of the worktree, never from hand-copied fields.
        self.batch = batches.import_batch(payload)
        self.batch_id = self.batch["id"]
        on_disk = json.loads(
            batches.batch_file(self.batch_id).read_text(encoding="utf-8"))
        integs = on_disk.get("integrations") or []
        self.assertEqual(len(integs), 1)
        # Field-name guard: the driver reads exactly these keys.
        self.assertEqual(integs[0].get("lane"), "main")
        self.assertEqual(integs[0].get("head"), self.head_b)

    def queue_refresh(self, **over):
        item = {"key": "rk1", "kind": "dep-update", "repo": self.REPO,
                "dep": "bar", "target": "2.0.0", "lane": "main",
                "channel": "stable", "status": "queued", "attempts": 0,
                "origin": "m2-refresh", "supersedes": "run-old",
                "refresh_base": self.batch_id,
                "note": "새 기준에서 다시 실행 요청",
                "created_at": "2026-10-21T00:00:00Z"}
        item.update(over)
        st = sched.load(self.REPO)
        st.setdefault("queue", []).append(item)
        sched.save(st)
        return item["key"]


class RefreshBaseTests(RefreshBaseFixture):
    def test_refresh_run_basecommit_is_batch_head(self):
        self.queue_refresh()
        out = sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertTrue(out["ran"], out)
        st = sched.load(self.REPO)
        item = next(i for i in st["queue"] if i.get("key") == "rk1")
        run_id = item.get("run_id")
        self.assertTrue(run_id)
        state = store.load(run_id)
        # Exact sha: the base batch integration head, not main @ A.
        self.assertEqual(state.get("baseCommit"), self.head_b)
        self.assertEqual(state.get("refresh_base"), self.batch_id)
        work = store.run_dir(run_id) / "work"
        self.assertTrue((work / ".git").exists() or work.is_dir())
        base_of_work = git("merge-base", "HEAD", self.head_b, cwd=work)
        self.assertEqual(base_of_work, self.head_b)

    def test_unresolvable_base_defers_without_block(self):
        self.queue_refresh(refresh_base="no-such-batch")
        before = dict((sched.load(self.REPO).get("m2_consec_blocks") or {}))
        runs_before = {r["id"] for r in store.list_runs(100)}
        out = sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertFalse(out["ran"])
        self.assertIn("deferred", str(out.get("reason") or ""))
        self.assertEqual({r["id"] for r in store.list_runs(100)},
                         runs_before)
        st = sched.load(self.REPO)
        item = next(i for i in st["queue"] if i.get("key") == "rk1")
        self.assertEqual(item.get("status"), "queued")
        self.assertIn("새로고침 실행을 보류함", item.get("note") or "")
        self.assertTrue(item.get("refresh_deferred"))
        after = dict(st.get("m2_consec_blocks") or {})
        self.assertEqual(before, after)
        # Visible on the glance through the pure need_you gate.
        from datetime import datetime, timezone
        now = int(datetime.now(timezone.utc).timestamp())
        shown = batches.refresh_deferred_item(st, now)
        self.assertIsNotNone(shown)
        self.assertEqual(shown.get("kind"), "refresh-deferred")
        st2 = dict(st)
        st2["queue"] = [dict(i, refresh_deferred=False)
                        for i in st["queue"]]
        self.assertIsNone(batches.refresh_deferred_item(st2, now))

    def test_normal_run_untouched(self):
        st = sched.load(self.REPO)
        st.setdefault("queue", []).append(
            {"key": "plain1", "kind": "dep-update", "repo": self.REPO,
             "dep": "bar", "target": "2.0.0", "lane": "main",
             "channel": "stable", "status": "queued", "attempts": 0})
        sched.save(st)
        seen = {}
        with mock.patch("openmainty.cli._start_run_with",
                        return_value=(1, None)) as m:
            def capture(ns, executor, run_fields=None, **kw):
                seen.update(dict(run_fields or {}))
                return (1, None)
            m.side_effect = capture
            sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertEqual(seen.get("base_branch"), "main")
        self.assertIsNone(seen.get("refresh_base"))

    def test_helper_lane_mismatch_and_unknown(self):
        self.assertEqual(
            sched._refresh_base_head("next", self.batch_id, self.co), "")
        self.assertEqual(
            sched._refresh_base_head("main", "no-such-batch", self.co), "")
        self.assertEqual(
            sched._refresh_base_head("main", self.batch_id, self.co),
            self.head_b)

    def test_deferred_head_never_blocks_queue(self):
        # S16-R1: a deferred refresh at the head must not starve the
        # items behind it. The flag persists from an earlier tick's
        # deferral; three consecutive dispatches execute the normal
        # item once while the deferred item stays parked.
        key = self.queue_refresh(refresh_base="no-such-batch")
        st = sched.load(self.REPO)
        head = next(i for i in st["queue"] if i.get("key") == key)
        head["refresh_deferred"] = True
        head["note"] = "새 기준을 찾지 못해 보류함"
        st.setdefault("queue", []).append(
            {"key": "plain2", "kind": "dep-update", "repo": self.REPO,
             "dep": "left-pad", "target": "1.1.0", "lane": "main",
             "channel": "stable", "status": "queued", "attempts": 0})
        sched.save(st)
        out1 = sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertTrue(out1["ran"], out1)
        st = sched.load(self.REPO)
        plain = next(i for i in st["queue"] if i.get("key") == "plain2")
        self.assertTrue(plain.get("run_id"))
        deferred = next(i for i in st["queue"] if i.get("key") == "rk1")
        self.assertEqual(deferred.get("status"), "queued")
        self.assertTrue(deferred.get("refresh_deferred"))
        out2 = sched.run_due(self.REPO, runner.FakeExecutor())
        out3 = sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertFalse(out2["ran"])
        self.assertFalse(out3["ran"])
        st = sched.load(self.REPO)
        deferred = next(i for i in st["queue"] if i.get("key") == "rk1")
        self.assertTrue(deferred.get("refresh_deferred"))
        # The deferred head never earned a run; the item behind it
        # ran exactly once (same run resumed, never forked).
        self.assertIsNone(deferred.get("run_id"))
        runs = [r for r in store.list_runs(100)
                if r.get("issue") == "dep-left-pad-1.1.0"]
        self.assertEqual(len(runs), 1)

    def test_deferred_only_reports_honest_reason(self):
        key = self.queue_refresh(refresh_base="no-such-batch")
        st = sched.load(self.REPO)
        head = next(i for i in st["queue"] if i.get("key") == key)
        head["refresh_deferred"] = True
        sched.save(st)
        out = sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertFalse(out["ran"])
        self.assertIn("확인 필요", out.get("reason") or "")
        self.assertIn("no-such-batch", out.get("reason") or "")

    def test_resolved_deferral_rejoins_pick(self):
        # A deferred refresh whose base batch appears rejoins the
        # automatic pick at the next dispatch (flag off, plain note).
        key = self.queue_refresh(refresh_base=self.batch_id)
        st = sched.load(self.REPO)
        item = next(i for i in st["queue"] if i.get("key") == key)
        item["refresh_deferred"] = True
        item["note"] = "새 기준을 찾지 못해 보류함"
        sched.save(st)
        out = sched.run_due(self.REPO, runner.FakeExecutor())
        self.assertTrue(out["ran"], out)
        st = sched.load(self.REPO)
        item = next(i for i in st["queue"] if i.get("key") == key)
        self.assertNotIn("refresh_deferred", item)
        self.assertIn("대기열로 복귀", item.get("note") or "")
        self.assertEqual(store.load(item["run_id"])["baseCommit"],
                         self.head_b)


class ProbeNextTests(RefreshBaseFixture):
    """branches.probe_next: True/False only on a successful listing."""

    def test_probe_false_on_main_only(self):
        from openmainty import branches
        self.assertTrue(branches.probe_next(str(self.co)) is False)

    def test_probe_true_when_next_exists(self):
        from openmainty import branches
        git("branch", "next", cwd=self.co)
        try:
            self.assertTrue(branches.probe_next(str(self.co)) is True)
        finally:
            git("branch", "-D", "next", cwd=self.co)

    def test_probe_none_on_empty_missing_and_failure(self):
        from openmainty import branches
        self.assertIsNone(branches.probe_next(str(self.root / "nope")))
        with mock.patch("openmainty.util.run_cmd",
                        return_value=(1, "", "boom")):
            self.assertIsNone(branches.probe_next(str(self.co)))
        with mock.patch("openmainty.util.run_cmd",
                        side_effect=OSError("no git")):
            self.assertIsNone(branches.probe_next(str(self.co)))

    def test_probe_rejects_nonabsolute_without_git(self):
        from pathlib import Path
        from openmainty import branches
        # Successful `* next` output: only a pre-git rejection
        # keeps this unknown, so git must never be invoked.
        with mock.patch("openmainty.util.run_cmd",
                        return_value=(0, "* next\n", "")) as m:
            self.assertIsNone(branches.probe_next(""))
            self.assertIsNone(branches.probe_next(Path("")))
            self.assertIsNone(branches.probe_next("relative/path"))
            self.assertIsNone(branches.probe_next(None))
        m.assert_not_called()

    def test_probe_uses_bounded_timeout(self):
        from openmainty import branches
        with mock.patch("openmainty.util.run_cmd",
                        return_value=(0, "  main\n", "")) as m:
            self.assertTrue(branches.probe_next(str(self.co)) is False)
        self.assertEqual(m.call_args.kwargs.get("timeout"), 10)


class BranchObservationTests(RefreshBaseFixture):
    """Scan persists the tri-state branch observation atomically.

    A successful scan stores True/False from a real probe of the
    current checkout; any unsuccessful terminal stores None with
    the scan's own stamps, so a prior True cannot survive a
    failure or an interruption.
    """

    def _offline_scan_mocks(self, with_deps=True):
        # with_deps keeps one supported row so the scan can succeed;
        # without it the real deps.check runs against the checkout,
        # which is how the unsupported-manifest path is exercised.
        patches = []
        if with_deps:
            row = {"name": "foo", "kind": "npm", "declared": "9.9.8",
                   "installed": "9.9.8", "latest": "9.9.8", "drift": False,
                   "lane": "next"}
            patches.append(
                mock.patch("openmainty.deps.check",
                           return_value=[dict(row)]))
        patches += [
            mock.patch("openmainty.cli._open_issues", return_value=[]),
            mock.patch("openmainty.cli._open_prs", return_value=[]),
            mock.patch("openmainty.discussions.scan_source",
                       return_value={"items": [], "status": {}}),
            mock.patch("openmainty.discussions.fetch_labels",
                       return_value={"labels": [], "pages": 0,
                                     "labels_complete": True}),
            mock.patch("openmainty.sched.check_repo_watches",
                       return_value={"checked": 0, "changed": [],
                                     "failed": []}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _seed_prior_true(self):
        from openmainty.util import utcnow
        now = utcnow()
        st = sched.load(self.REPO)
        st.update({"branch_next": True, "branch_next_at": now,
                   "last_scan": now,
                   "branch_next_checkout": str(self.co)})
        sched.save(st)

    def test_scan_success_persists_false_observation(self):
        self._offline_scan_mocks()
        out = sched.scan(self.REPO)
        self.assertTrue(out["scan_ok"])
        st = sched.load(self.REPO)
        self.assertTrue(st["branch_next"] is False)
        self.assertEqual(st["branch_next_at"], st["last_scan"])
        self.assertEqual(st["branch_next_checkout"], str(self.co))

    def test_scan_success_persists_true_on_next_branch(self):
        self._offline_scan_mocks()
        git("branch", "next", cwd=self.co)
        try:
            out = sched.scan(self.REPO)
        finally:
            git("branch", "-D", "next", cwd=self.co)
        self.assertTrue(out["scan_ok"])
        st = sched.load(self.REPO)
        self.assertTrue(st["branch_next"] is True)
        self.assertEqual(st["branch_next_checkout"], str(self.co))

    def test_failed_scan_persists_none(self):
        self._offline_scan_mocks()
        self._seed_prior_true()
        with mock.patch("openmainty.deps.check",
                        side_effect=RuntimeError("boom")):
            out = sched.scan(self.REPO)
        self.assertFalse(out["scan_ok"])
        st = sched.load(self.REPO)
        self.assertIsNone(st["branch_next"])
        self.assertIsNotNone(st["branch_next_at"])

    def test_interrupted_scan_cannot_leave_prior_true(self):
        self._seed_prior_true()
        with mock.patch("openmainty.deps.check",
                        side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                sched.scan(self.REPO)
        st = sched.load(self.REPO)
        self.assertIsNone(st["branch_next"])
        self.assertIsNone(st.get("branch_next_at"))
        self.assertIsNone(st.get("branch_next_checkout"))

    def test_unsupported_manifest_persists_none(self):
        # Real deps.check against an owned checkout with no
        # manifest: the actual unsupported-summary late path runs,
        # not a mocked row set.
        self._offline_scan_mocks(with_deps=False)
        self._seed_prior_true()
        empty = self.root / "empty-co"
        empty.mkdir()
        pc = pcconfig.load()
        pc["checkouts"][self.REPO] = str(empty)
        pcconfig.save(pc)
        out = sched.scan(self.REPO)
        self.assertFalse(out["scan_ok"])
        self.assertEqual(out["checked"], 0)
        self.assertIn("no supported manifest", out["error"])
        st = sched.load(self.REPO)
        self.assertIsNone(st["branch_next"])
        self.assertEqual(st["branch_next_checkout"], str(empty))


if __name__ == "__main__":
    unittest.main()
