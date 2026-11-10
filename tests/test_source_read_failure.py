"""Fail-closed source identity: read failures are unknown, never green.

Production defect (Root synthetic repro 08:09:23): ``runner._git_z``
mapped a nonzero git exit to ``[]``, so ``source_snapshot`` returned a
successful identity with the valid HEAD, zero files, and the empty-tree
hash — which ``batches._snapshot`` accepted as clean. These tests pin
the fail-closed contract with real temp git repos and injected read
faults (never fixtures asserting the implementation's own strings):

- any critical git list/status/HEAD failure raises
  ``runner.SourceUnreadable``; ``batches._snapshot`` and the serve/graph
  read paths report unknown (None), never a partial identity;
- tracked/untracked file read or stat denial raises; genuine ENOENT and
  ENOTDIR stay real deletions; true edits still change the fingerprint
  and valid trees keep a pinned golden hash;
- worker control flow stops without certifying: refresh/bind preserve
  stored proofs, fix-commit failure blocks with provenance and no model
  rerun, and resume recovery stages/commits nothing while unprovable.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, graph, pcconfig, repos, runner, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

PROFILE = {
    "name": "fakerepo",
    "github": "example/fakerepo",
    "baseBranch": "main",
    "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["make", "unit"], "requires": []}],
    "e2e": [],
    "release": {"notes": "CHANGELOG.md"},
    "announce": {"medium": True, "discord": False, "slack": False},
}

# Golden content fingerprint for the fixture repo below (.gitignore with
# "*.log\nnode_modules/\n" + a.txt with "before\n"): pinned so any change
# to the valid-source hash algorithm fails loudly. Provenance: Root
# independently computed this value with the ORIGINAL frozen59
# source_snapshot (loaded from permanent backup as
# openmainty.runner_frozen59) on the same temp git layout and confirmed
# the current function returns the identical fingerprint (receipt
# root60-independent-frozen59-golden.json) — true before/after parity,
# not a golden generated from the new implementation alone. The
# fingerprint covers content only, never HEAD.
GOLDEN_TWO_FILE_FP = "3eda7b0b4a72baf9b446957dacbc51409a58d690b96213a4ec899c0cfd766c53"


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main", cwd=path)
    git("config", "user.email", "test@localhost", cwd=path)
    git("config", "user.name", "test", cwd=path)
    (path / ".gitignore").write_text("*.log\nnode_modules/\n", encoding="utf-8")
    (path / "a.txt").write_text("before\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "init", cwd=path)
    return path


def fail_z_lists(real_run_cmd):
    """Inject rc=128 for NUL-separated git list reads only; pass through."""

    def router(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
        argv = list(cmd)
        if "-z" in argv:
            return 128, "", "injected read failure"
        return real_run_cmd(cmd, cwd=cwd, timeout=timeout,
                            cancel_event=cancel_event, **kw)

    return router


def git_operation(argv):
    """Git subcommand for hardened ['git', -c ..., (--git-dir X
    --work-tree Y | -C <path>), <op>, ...] style argv.

    Skips the `-c <k=v>` overrides and the pinned `-C <path>` or
    `--git-dir/--work-tree` location pair so the real verb is
    inspected — a filename argument is never mistaken for one.
    """
    argv = list(argv)
    if len(argv) < 2 or argv[0] != "git":
        return None
    i = 1
    while i < len(argv) and argv[i] == "-c":
        i += 2
    if i < len(argv) and argv[i] == "-C":
        i += 2
    elif i < len(argv) and argv[i] == "--git-dir":
        i += 4
    return argv[i] if i < len(argv) else None


class ReadFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.co = make_git_repo(Path(self.tmp.name) / "co")

    def test_git_list_failure_raises_never_empty_identity(self):
        real = runner.run_cmd
        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_z_lists(real)):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)
            # The acceptance gate reports unknown instead of a clean
            # files:0 empty-hash identity.
            self.assertIsNone(batches._snapshot(str(self.co)))

    def test_git_probe_timeout_degrades_fast(self):
        seen = {}

        def router(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            seen.setdefault("timeout", timeout)
            return 124, "", "timed out"

        with mock.patch.object(runner, "run_cmd", side_effect=router):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)
        self.assertEqual(seen["timeout"], runner._GIT_PROBE_TIMEOUT)
        self.assertLessEqual(runner._GIT_PROBE_TIMEOUT, 60)

    def test_git_status_only_failure_still_unavailable(self):
        real = runner.run_cmd

        def router(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            argv = list(cmd)
            if "status" in argv and "-z" in argv:
                return 128, "", "injected status failure"
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=router):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)
            self.assertIsNone(batches._snapshot(str(self.co)))

    def test_head_read_failure_is_unavailable_not_unknown_head(self):
        real = runner.run_cmd

        def router(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            argv = list(cmd)
            if "rev-parse" in argv:
                return 128, "", "injected head failure"
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=router):
            with self.assertRaises(runner.SourceUnreadable) as ctx:
                runner.source_snapshot(self.co)
            self.assertEqual(ctx.exception.op, "head")
            self.assertIsNone(batches._snapshot(str(self.co)))

    def test_empty_head_output_is_unavailable(self):
        real = runner.run_cmd

        def router(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            argv = list(cmd)
            if "rev-parse" in argv:
                return 0, "\n", ""
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=router):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)

    def test_tracked_read_denial_raises_not_marker(self):
        orig = Path.read_bytes

        def denied(self, *args, **kwargs):
            if self.name == "a.txt":
                raise PermissionError(13, "denied")
            return orig(self, *args, **kwargs)

        with mock.patch.object(Path, "read_bytes", denied):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)

    def test_untracked_read_denial_raises_not_silent_skip(self):
        (self.co / "new.py").write_text("print('new')\n", encoding="utf-8")
        before = runner.source_snapshot(self.co)
        self.assertIn("new.py", before["untracked"])
        orig = Path.read_bytes

        def denied(self, *args, **kwargs):
            if self.name == "new.py":
                raise PermissionError(13, "denied")
            return orig(self, *args, **kwargs)

        with mock.patch.object(Path, "read_bytes", denied):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)

    def test_stat_denial_is_not_a_deletion(self):
        # Runtime stat calls may report False instead of raising: the
        # re-stat must surface denial rather than a fake DELETED.
        orig_is_file = Path.is_file
        orig_is_dir = Path.is_dir
        orig_stat = Path.stat
        target = (self.co / "a.txt").resolve()

        def is_file(self):
            if Path(os.path.realpath(self)) == target:
                return False
            return orig_is_file(self)

        def is_dir(self):
            if Path(os.path.realpath(self)) == target:
                return False
            return orig_is_dir(self)

        def stat(self, *args, **kwargs):
            if Path(os.path.realpath(self)) == target:
                raise PermissionError(13, "denied")
            return orig_stat(self, *args, **kwargs)

        with mock.patch.object(Path, "is_file", is_file), \
                mock.patch.object(Path, "is_dir", is_dir), \
                mock.patch.object(Path, "stat", stat):
            with self.assertRaises(runner.SourceUnreadable):
                runner.source_snapshot(self.co)

    def test_parent_becomes_file_counts_as_real_deletion(self):
        sub = self.co / "sub" / "nested"
        sub.mkdir(parents=True)
        (sub / "f.txt").write_text("deep\n", encoding="utf-8")
        git("add", "-A", cwd=self.co)
        git("commit", "-qm", "nested", cwd=self.co)
        before = runner.source_snapshot(self.co)
        # Replace the directory with a plain file: the tracked nested
        # path is unresolvable (ENOTDIR) — genuinely gone, not unreadable.
        import shutil
        shutil.rmtree(self.co / "sub")
        (self.co / "sub").write_text("now a file\n", encoding="utf-8")
        after = runner.source_snapshot(self.co)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])

    def test_noise_stat_and_read_failures_never_block(self):
        # Generated/cache files must never enter source identity: even
        # stat/read-denied noise stays negative-only evidence.
        before = runner.source_snapshot(self.co)
        nm = self.co / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("1\n", encoding="utf-8")
        (self.co / ".DS_Store").write_bytes(b"junk")
        mid = runner.source_snapshot(self.co)
        self.assertEqual(before["fingerprint"], mid["fingerprint"])

        def noisy(self):
            return "node_modules" in self.parts or self.name == ".DS_Store"

        orig_is_file = Path.is_file
        orig_is_dir = Path.is_dir
        orig_stat = Path.stat
        orig_read = Path.read_bytes

        def is_file(self):
            # OS calls that fail report False instead of raising.
            if noisy(self):
                return False
            return orig_is_file(self)

        def is_dir(self):
            if noisy(self):
                return False
            return orig_is_dir(self)

        def stat(self, *args, **kwargs):
            if noisy(self):
                raise PermissionError(13, "denied")
            return orig_stat(self, *args, **kwargs)

        def read(self, *args, **kwargs):
            if noisy(self):
                raise PermissionError(13, "denied")
            return orig_read(self, *args, **kwargs)

        with mock.patch.object(Path, "is_file", is_file), \
                mock.patch.object(Path, "is_dir", is_dir), \
                mock.patch.object(Path, "stat", stat), \
                mock.patch.object(Path, "read_bytes", read):
            after = runner.source_snapshot(self.co)
        self.assertEqual(before["fingerprint"], after["fingerprint"])

    def test_genuine_deletion_is_content_change_and_round_trips(self):
        before = runner.source_snapshot(self.co)
        (self.co / "a.txt").unlink()
        deleted = runner.source_snapshot(self.co)
        self.assertNotEqual(before["fingerprint"], deleted["fingerprint"])
        (self.co / "a.txt").write_text("before\n", encoding="utf-8")
        restored = runner.source_snapshot(self.co)
        self.assertEqual(before["fingerprint"], restored["fingerprint"])

    def test_true_edit_changes_fingerprint(self):
        before = runner.source_snapshot(self.co)
        (self.co / "a.txt").write_text("after\n", encoding="utf-8")
        after = runner.source_snapshot(self.co)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before["head"], after["head"])

    def test_valid_source_golden_parity(self):
        snap = runner.source_snapshot(self.co)
        again = runner.source_snapshot(self.co)
        self.assertEqual(snap["fingerprint"], again["fingerprint"])
        self.assertEqual(snap["fingerprint"], GOLDEN_TWO_FILE_FP)
        self.assertEqual(snap["files"], 2)


class StoreBackedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(self.root / "checkout")
        pcconfig.save(pc)

    def make_run(self, **fields):
        co = self.root / "checkout"
        make_git_repo(co)
        state = store.create("fakerepo", "1", "fake issue",
                             git("rev-parse", "HEAD", cwd=co).strip(),
                             {"worker": "test"})
        for key, val in fields.items():
            state[key] = val
        store.claim("fakerepo", "1", state["id"])
        work = store.run_dir(state["id"]) / "work"
        make_git_repo(work)
        state["branch"] = "codex/test-read-failure"
        # 301: model-tree reads bind to the prep record.
        state["workGit"] = runner.record_git_pointer(work)
        store.save(state)
        return state, work

    def test_refresh_and_bind_preserve_stored_proofs(self):
        state, work = self.make_run()
        runner._bind_verification(state, work, "e2e")
        good_fp = state["workTreeFingerprint"]
        good_proofs = dict(state["verifiedFingerprints"])
        real = runner.run_cmd
        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_z_lists(real)):
            with self.assertRaises(runner.SourceUnreadable):
                runner._refresh_source(state, work)
            with self.assertRaises(runner.SourceUnreadable):
                runner._bind_verification(state, work, "review")
        self.assertEqual(state["workTreeFingerprint"], good_fp)
        self.assertEqual(state["verifiedFingerprints"], good_proofs)

    def test_shell_checks_bind_failure_stops_without_certifying(self):
        from openmainty.runner import FakeExecutor
        state, work = self.make_run()
        runner._bind_verification(state, work, "e2e")
        good_proofs = dict(state["verifiedFingerprints"])
        real = runner.run_cmd
        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_z_lists(real)):
            ok = runner.run_shell_checks(state, "e2e", [],
                                         FakeExecutor(), work, {})
        self.assertFalse(ok)
        self.assertNotEqual(state["steps"]["e2e"]["status"], "done")
        self.assertEqual(state["verifiedFingerprints"], good_proofs)

    def test_execute_resume_after_fix_commit_read_failure(self):
        # End-to-end control flow through the real engine entry point
        # (lease-free `_execute_inner`: the test must never touch the
        # production single-worker OS lease). First pass reaches the fix
        # commit boundary with genuine actionable triage, fails the read
        # there, and blocks with provenance. After an outage mutation,
        # the same-run resume must return False with zero new model
        # calls and zero staging/committing of the unknown tree, while
        # bytes, index, and historical proofs stay exactly as they were.
        from openmainty.runner import FakeExecutor
        state, work = self.make_run()
        state["dep"] = "@scope/pkg"
        state["target"] = "1.0.1"
        store.save(state)
        (work / "a.txt").write_text("fix output\n", encoding="utf-8")
        ex = FakeExecutor()
        real = runner.run_cmd

        # Fault injection targets the actual commit boundary: reads
        # fail only while a maybe_commit_worktree call is in progress
        # (production marks the step done before committing, so step
        # status cannot gate the fault). The hit counter proves the
        # failure path really executed.
        in_commit = []
        injected = [0]
        orig_maybe = runner.maybe_commit_worktree

        def fail_at_fix_commit(cmd, cwd=None, timeout=600,
                               cancel_event=None, **kw):
            argv = list(cmd)
            if "-z" in argv and in_commit:
                injected[0] += 1
                return 128, "", "injected read failure"
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        def counting_maybe(state, work, step):
            in_commit.append(step)
            try:
                return orig_maybe(state, work, step)
            finally:
                in_commit.pop()

        checkout = self.root / "checkout"
        only = ["triage", "reproduce", "analyze", "fix"]
        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_at_fix_commit), \
                mock.patch.object(runner, "maybe_commit_worktree",
                                  side_effect=counting_maybe,
                                  autospec=True):
            first = runner._execute_inner(state, dict(PROFILE), ex,
                                          checkout, "fake body", only=only)
        self.assertFalse(first)
        self.assertGreater(injected[0], 0,
                           "injected read failure never fired at the boundary")
        self.assertEqual(state["steps"]["fix"]["status"], "blocked")
        self.assertEqual((state.get("commitBlocked") or {}).get("reason"),
                         "source-unreadable")
        model_calls = len(ex.calls)
        self.assertGreater(model_calls, 0)
        # Outage mutation: unrelated edits land while unreadable.
        (work / "a.txt").write_text("fix output plus outage edit\n",
                                    encoding="utf-8")
        (work / "rogue.py").write_text("print('rogue')\n", encoding="utf-8")
        before_bytes = (work / "a.txt").read_bytes()
        before_index = git("diff", "--cached", "--name-only", cwd=work)
        before_head = git("rev-parse", "HEAD", cwd=work)
        before_proofs = dict(state.get("verifiedFingerprints") or {})
        recorded = []

        def recording(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            recorded.append(list(cmd))
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=recording):
            second = runner._execute_inner(state, dict(PROFILE), ex,
                                           checkout, "fake body", only=only)
        self.assertFalse(second)
        # No model rerun on resume: the fix output stays recorded.
        self.assertEqual(len(ex.calls), model_calls)
        self.assertEqual(state["steps"]["fix"]["status"], "blocked")
        self.assertEqual((state.get("commitBlocked") or {}).get("reason"),
                         "source-unreadable")
        # Zero staging or committing of the unknown tree: inspect the
        # real verb after any `-C <path>` / `-c <k=v>` globals.
        ops = [git_operation(a) for a in recorded]
        self.assertNotIn("add", ops)
        self.assertNotIn("commit", ops)
        # Bytes, index, HEAD, and historical proofs are preserved as-is;
        # no fresh proof was fabricated from the unknown tree.
        self.assertEqual((work / "a.txt").read_bytes(), before_bytes)
        self.assertTrue((work / "rogue.py").is_file())
        self.assertEqual(git("diff", "--cached", "--name-only", cwd=work),
                         before_index)
        self.assertEqual(git("rev-parse", "HEAD", cwd=work), before_head)
        self.assertEqual(state.get("verifiedFingerprints") or {},
                         before_proofs)
        self.assertIn("source-unreadable", state.get("waitReason") or "")

    def test_candidate_path_set_propagates_unreadable(self):
        state, work = self.make_run()
        (work / "a.txt").write_text("dirty\n", encoding="utf-8")
        real = runner.run_cmd
        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_z_lists(real)):
            with self.assertRaises(runner.SourceUnreadable):
                runner._candidate_path_set(work, state.get("baseCommit"))

    def test_candidate_comparison_fails_closed_on_unreadable(self):
        state, work = self.make_run()
        (work / "a.txt").write_text("dirty\n", encoding="utf-8")
        head = git("rev-parse", "HEAD", cwd=work).strip()
        candidate = runner._blocked_candidate_snapshot(
            {**state, "baseCommit": head}, work)
        self.assertTrue(candidate["complete"])
        orig_read = Path.read_bytes

        def denied_read(self, *args, **kwargs):
            if self.name == "a.txt":
                raise PermissionError(13, "denied")
            return orig_read(self, *args, **kwargs)

        with mock.patch.object(Path, "read_bytes", denied_read):
            self.assertFalse(
                runner._recover_set_matches(work, state, candidate))

    def test_commit_failure_capture_failure_stays_incomplete(self):
        # A non-hook commit failure (worker commits run hook-free)
        # while a tracked noise file is unreadable: the failed
        # candidate must record incomplete (never a complete partial
        # identity), preserve bytes/index, and the resume must
        # stage/commit nothing.
        state, work = self.make_run()
        nm = work / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("1\n", encoding="utf-8")
        git("add", "-f", "node_modules/pkg/index.js", cwd=work)
        git("commit", "-qm", "track noise", cwd=work)
        (nm / "index.js").write_text("2\n", encoding="utf-8")
        (work / "new.py").write_text("print('new')\n", encoding="utf-8")
        base_head = git("rev-parse", "HEAD", cwd=work).strip()
        orig_read = Path.read_bytes

        def denied_read(self, *args, **kwargs):
            if "node_modules" in self.parts:
                raise PermissionError(13, "denied")
            return orig_read(self, *args, **kwargs)

        real = runner.run_cmd

        def fail_commit(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            if "commit" in list(cmd):
                return (1, "", "fatal: forced git error")
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(Path, "read_bytes", denied_read), \
                mock.patch.object(runner, "run_cmd",
                                  side_effect=fail_commit):
            got = runner.maybe_commit_worktree(state, work, "fix")
            candidate = (state.get("commitBlocked") or {}).get("candidate")
        self.assertTrue(got.get("blocked"))
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate["complete"])
        # The unreadable noise file is not certified; only genuinely
        # read bytes may appear, and incomplete never auto-resolves.
        self.assertTrue((work / "new.py").is_file())
        self.assertNotIn("node_modules/pkg/index.js",
                         candidate.get("files") or {})
        # Bytes, HEAD, and the staged set are preserved as-is.
        self.assertEqual((nm / "index.js").read_text(encoding="utf-8"), "2\n")
        self.assertEqual(git("rev-parse", "HEAD", cwd=work).strip(),
                         base_head)
        staged = git("diff", "--cached", "--name-only", cwd=work)
        self.assertIn("node_modules/pkg/index.js", staged)
        # Resume: incomplete identity never auto-resolves or commits.
        real = runner.run_cmd
        recorded = []

        def recording(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            recorded.append(list(cmd))
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=recording):
            outcome = runner._recover_commit_blocked(state, work)
        self.assertEqual(outcome, "blocked")
        ops = [git_operation(a) for a in recorded]
        self.assertNotIn("add", ops)
        self.assertNotIn("commit", ops)
        self.assertEqual(git("rev-parse", "HEAD", cwd=work).strip(),
                         base_head)
        self.assertIn("node_modules/pkg/index.js",
                      git("diff", "--cached", "--name-only", cwd=work))

    def test_pathset_list_failure_stays_unknown(self):
        # Required path-list reads that fail must signal unavailable —
        # never an empty or partial success. With tracked edits plus
        # untracked source on disk, a non-hook commit failure under
        # list failure records an unknown candidate, preserves
        # bytes/index, and the resume stages/commits nothing.
        state, work = self.make_run()
        own_head = git("rev-parse", "HEAD", cwd=work).strip()
        state["baseCommit"] = own_head
        store.save(state)
        (work / "a.txt").write_text("dirty\n", encoding="utf-8")
        (work / "new.py").write_text("print('new')\n", encoding="utf-8")
        real = runner.run_cmd

        def fail_names(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            if "--name-only" in list(cmd):
                return 128, "", "injected list failure"
            if "commit" in list(cmd):
                return (1, "", "fatal: forced git error")
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=fail_names):
            with self.assertRaises(runner.SourceUnreadable):
                runner._candidate_path_set(work, own_head)
            with self.assertRaises(runner.SourceUnreadable):
                runner._candidate_path_set(work, "unknown")
            got = runner.maybe_commit_worktree(state, work, "fix")
            candidate = (state.get("commitBlocked") or {}).get("candidate")
        self.assertTrue(got.get("blocked"))
        self.assertIsNotNone(candidate)
        self.assertFalse(candidate["complete"])
        self.assertEqual(candidate.get("paths"), [])
        self.assertEqual((work / "a.txt").read_text(encoding="utf-8"),
                         "dirty\n")
        self.assertTrue((work / "new.py").is_file())
        self.assertEqual(git("rev-parse", "HEAD", cwd=work).strip(),
                         own_head)
        recorded = []

        def recording(cmd, cwd=None, timeout=600, cancel_event=None, **kw):
            recorded.append(list(cmd))
            return real(cmd, cwd=cwd, timeout=timeout,
                        cancel_event=cancel_event, **kw)

        with mock.patch.object(runner, "run_cmd", side_effect=recording):
            outcome = runner._recover_commit_blocked(state, work)
        self.assertEqual(outcome, "blocked")
        ops = [git_operation(a) for a in recorded]
        self.assertNotIn("add", ops)
        self.assertNotIn("commit", ops)
        self.assertEqual(git("rev-parse", "HEAD", cwd=work).strip(),
                         own_head)

    def test_read_paths_report_unknown_and_stale_stays_distinct(self):
        state, work = self.make_run()
        store.mark(state, "e2e", "done", exit_code=0, evidence=["e2e.x"],
                   note="bound in test")
        runner._bind_verification(state, work, "e2e")
        head = state["workCommit"]
        real = runner.run_cmd
        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_z_lists(real)):
            self.assertIsNone(runner.fresh_snapshot(state))
            self.assertIsNone(serve._live_snapshot(state))
        live_head, live_fp, kind = serve._live_head_fp_kind(None)
        self.assertEqual((live_head, live_fp, kind), (None, None, "unknown"))
        status = store.verification_status(state, current_fingerprint=None,
                                           current_head=None)
        self.assertEqual(status["e2e"], "unknown")
        stale = store.verification_status(state,
                                          current_fingerprint="different",
                                          current_head=head)
        self.assertEqual(stale["e2e"], "stale")
