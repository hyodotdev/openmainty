"""Commit-path repair: conventional subjects, staged evidence, blocked outcome.

Covers the run-7fc8d753 defect (hook rejection of the run-id-prefixed
subject silently discarded as test/plan-only no-code) without hardcoding
production run IDs or dependency names: a real temporary git repo with a
commit-msg hook proves a manifest+lock candidate commits through the
corrected helper. Since M2.8b1 worker commits run hook-free, a forced
non-hook git failure preserves staged files and records an explicit
blocked outcome (never no-code), retrying the commit alone never
touches the fix model, and the staged delta always counts as
dependency evidence.
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import branches, muse, repos, runner, sched, store  # noqa: E402
from openmainty.util import run_cmd  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

# commit-msg hook mimicking the production commitlint gate: the subject
# (first line) must start with a conventional `type[(scope)][!]: summary`
# shape. The old engine's `openmainty <run-id> <step>: ...` prefix fails
# exactly like commitlint's subject-empty/type-empty.
HOOK = """#!/bin/sh
subject=$(head -n1 "$1")
if printf '%s' "$subject" | grep -Eq '^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(\\([^()]*\\))?(!)?: .+'; then
  exit 0
fi
echo "subject may not be empty [subject-empty]" >&2
echo "type may not be empty [type-empty]" >&2
exit 1
"""

REJECT_ALL_HOOK = "#!/bin/sh\necho 'forced hook failure' >&2\nexit 1\n"


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    return Path(tmp.name)


def make_repo(root, hook_text=HOOK):
    work = root / "work"
    work.mkdir()
    (work / "package.json").write_text(
        '{"name": "t", "devDependencies": {"@scope/pkg": "^1.0.0"}}\n')
    (work / "yarn.lock").write_text("# yarn lockfile v1\n")
    assert run_cmd(["git", "-C", str(work), "init", "-q"])[0] == 0
    assert run_cmd(["git", "-C", str(work), "add", "."])[0] == 0
    assert run_cmd(["git", "-C", str(work), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "base"])[0] == 0
    hook = work / ".git" / "hooks" / "commit-msg"
    hook.write_text(hook_text)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    code, base, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
    assert code == 0
    return work, base.strip()


def make_state(title):
    state = store.create("test-repo", "dep-test-1", title, "base0",
                         {"worker": "test"})
    state["branch"] = "test/branch"
    store.save(state)
    return state


def move_into_run_dir(root, state, work):
    dest = store.run_dir(state["id"]) / "work"
    dest.parent.mkdir(parents=True, exist_ok=True)
    work.rename(dest)
    return dest


class CommitSubjectTests(unittest.TestCase):
    def test_conventional_title_used_verbatim_run_id_in_body_only(self):
        state = {"id": "run-test-aaa", "title": "chore(deps): bump pkg to 2.0.0"}
        subject, body = runner.commit_subject_for(state, "fix")
        self.assertEqual(subject, "chore(deps): bump pkg to 2.0.0")
        self.assertFalse(subject.startswith("openmainty"))
        self.assertIn("run-test-aaa", body)
        self.assertIn("fix", body)

    def test_non_conventional_title_gets_safe_conventional_fallback(self):
        state = {"id": "run-test-bbb", "title": "openmainty run-test-bbb fix: stuff"}
        subject, body = runner.commit_subject_for(state, "fix")
        self.assertTrue(runner._CONVENTIONAL_SUBJECT_RE.match(subject))
        self.assertFalse(subject.startswith("openmainty"))
        self.assertIn("run-test-bbb", body)

    def test_old_prefix_subject_rejected_by_hook(self):
        self.root = make_home(self)
        work, _ = make_repo(self.root)
        code, _, err = run_cmd(
            ["git", "-C", str(work), "-c", "user.name=t",
             "-c", "user.email=t@t", "commit", "--allow-empty",
             "-m", "openmainty run-test-ccc fix: chore(deps): bump pkg"])
        self.assertNotEqual(code, 0)
        self.assertIn("type-empty", err)


class CommitThroughHookTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def _candidate(self, hook_text=HOOK):
        work, base = make_repo(self.root, hook_text)
        state = make_state("chore(deps): bump @scope/pkg to 1.0.1")
        work = move_into_run_dir(self.root, state, work)
        (work / "package.json").write_text(
            '{"name": "t", "devDependencies": {"@scope/pkg": "^1.0.1"}}\n')
        (work / "yarn.lock").write_text("# yarn lockfile v1\n# @scope/pkg 1.0.1\n")
        state["baseCommit"] = base
        store.save(state)
        return state, work, base

    def test_real_candidate_gets_committed(self):
        state, work, base = self._candidate()
        got = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(got.get("committed"), got)
        code, head, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        self.assertNotEqual(head.strip(), base)
        self.assertEqual(state["workCommit"], head.strip())
        code, msg, _ = run_cmd(
            ["git", "-C", str(work), "log", "-1", "--format=%s"])
        self.assertEqual(msg.strip(), "chore(deps): bump @scope/pkg to 1.0.1")

    def test_git_error_preserves_staged_and_records_blocked(self):
        # M2.8b1: hooks can no longer fail a worker commit (they
        # never run), so the blocked path is proven with a forced
        # non-hook git failure on the commit call only.
        state, work, base = self._candidate(hook_text=REJECT_ALL_HOOK)
        real_run = runner.run_cmd

        def fail_commit(argv, **kw):
            if "commit" in list(argv):
                return (1, "", "fatal: nothing staged (forced)")
            return real_run(argv, **kw)

        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_commit):
            got = runner.maybe_commit_worktree(state, work, "fix")
        self.assertFalse(got.get("committed"))
        self.assertTrue(got.get("blocked"))
        self.assertNotEqual(got.get("exit"), 0)
        self.assertIn("nothing staged (forced)", got.get("reason", ""))
        # Staged files survive the rejection.
        code, staged, _ = run_cmd(
            ["git", "-C", str(work), "diff", "--cached", "--name-only"])
        self.assertEqual(code, 0)
        self.assertIn("package.json", staged)
        # Explicit failure evidence on the run, never no-code.
        self.assertIn("commitBlocked", state)
        kinds = [i.get("kind") for i in state.get("interventions", [])]
        self.assertIn("commit-blocked", kinds)
        code, head, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(head.strip(), base)
        # The staged manifest delta still counts as dependency evidence.
        evidence = sched._dep_diff_evidence(work, base)
        self.assertIsNotNone(evidence)
        self.assertIn("package.json", evidence)
        self.assertIn("yarn.lock", evidence)

    def test_retry_commit_does_not_call_fix_model_again(self):
        state, work, _ = self._candidate()
        real_run = runner.run_cmd
        calls = []

        def fail_once(argv, **kw):
            if "commit" in list(argv) and not calls:
                calls.append(1)
                return (1, "", "fatal: forced git error")
            return real_run(argv, **kw)

        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_once):
            first = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(first.get("blocked"))
        fix_note_before = dict(state["steps"].get("fix", {})).get("note")
        with mock.patch.object(runner.muse, "exec_prompt",
                               side_effect=AssertionError("model must not run")):
            retry = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(retry.get("committed"), retry)
        self.assertNotIn("commitBlocked", state)
        self.assertEqual(
            dict(store.load(state["id"])["steps"].get("fix", {})).get("note"),
            fix_note_before)

    def test_staged_only_delta_never_false_empty(self):
        # Committed check (base..HEAD) sees nothing, yet the real staged
        # upgrade must still read as dependency evidence.
        state, work, base = self._candidate()
        assert run_cmd(["git", "-C", str(work), "add", "-u"])[0] == 0
        code, names, _ = run_cmd(
            ["git", "-C", str(work), "diff", "--name-only", f"{base}..HEAD", "--"])
        self.assertEqual(code, 0)
        self.assertEqual(names.strip(), "")
        evidence = sched._dep_diff_evidence(work, base)
        self.assertEqual(sorted(evidence or []), ["package.json", "yarn.lock"])

    def test_private_cache_never_enters_source_or_commit(self):
        # Same-launcher private toolchain caches (yarn/npm/corepack under
        # muse.worker_cache_env) are runtime noise, never source: they
        # must not change the content fingerprint, list as untracked
        # candidates, or land in the local work commit.
        state, work, base = self._candidate()
        before = runner.source_snapshot(work)
        cache = Path(muse.worker_cache_env(work)["YARN_CACHE_FOLDER"])
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "diagnostic-cache.zip").write_bytes(
            b"task-only cache bytes, not source")
        after = runner.source_snapshot(work)
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        prefix = muse.CACHE_DIR_NAME + "/"
        self.assertFalse(
            [p for p in after.get("untracked", []) if p.startswith(prefix)])
        got = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(got.get("committed"), got)
        code, out, _ = run_cmd(
            ["git", "-C", str(work), "diff", "--name-only", base, "HEAD"])
        self.assertEqual(code, 0)
        self.assertFalse(
            [p for p in out.splitlines() if p.startswith(prefix)])

    def test_private_cache_does_not_block_exact_recovery(self):
        # With the worker's private cache present, an exact manually
        # committed blocked candidate must still prove committed: the
        # clean-tree gate filters runtime noise like every other gate.
        state, work, base = self._candidate()
        real_run = runner.run_cmd

        def fail_once(argv, **kw):
            if "commit" in list(argv):
                return (1, "", "fatal: forced git error")
            return real_run(argv, **kw)

        with mock.patch.object(runner, "run_cmd",
                               side_effect=fail_once):
            first = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(first.get("blocked"))
        cache = Path(muse.worker_cache_env(work)["YARN_CACHE_FOLDER"])
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "another-cache.zip").write_bytes(b"private cache")
        (work / ".git" / "hooks" / "commit-msg").write_text(HOOK)
        assert run_cmd(
            ["git", "-C", str(work), "add", "package.json", "yarn.lock"])[0] == 0
        code, _, err = run_cmd(
            ["git", "-C", str(work), "-c", "user.name=test",
             "-c", "user.email=test@example.com",
             "commit", "-qm", "chore(deps): bump @scope/pkg to 1.0.1"])
        self.assertEqual(code, 0, err)
        self.assertTrue(runner._candidate_already_committed(state, work))

    def test_truly_empty_delta_stays_empty(self):
        state = make_state("chore(deps): bump @scope/pkg to 1.0.1")
        work, base = make_repo(self.root)
        work = move_into_run_dir(self.root, state, work)
        state["baseCommit"] = base
        store.save(state)
        self.assertEqual(sched._dep_diff_evidence(work, base), [])

    def test_unknown_base_stays_unknown(self):
        state = make_state("chore(deps): bump @scope/pkg to 1.0.1")
        work, _ = make_repo(self.root)
        work = move_into_run_dir(self.root, state, work)
        self.assertIsNone(sched._dep_diff_evidence(work, "unknown"))
        self.assertIsNone(sched._dep_diff_evidence(work, ""))


class ApplyDirtier(runner.FakeExecutor):
    """Fake apply worker that also leaves a real file change behind."""

    def muse_step(self, step, prompt, workdir):
        code, text, usage = super().muse_step(step, prompt, workdir)
        if step == "apply_review" and code == 0:
            (Path(workdir) / "review_fix.txt").write_text("reviewed\n")
        return code, text, usage


PROFILE = {
    "name": "test-repo", "github": "example/test",
    "baseBranch": "test/branch", "knowledge": [],
    "checks": [{"name": "unit", "cmd": ["fixture", "unit"], "requires": []}],
    "e2e": [], "release": {"notes": "CHANGELOG.md"}, "announce": {},
}


def _fail_commit_calls(after=0):
    """Patch runner.run_cmd so git commit calls fail with a non-hook
    git error. The first `after` commit calls still succeed (a fix
    commit can land while a later review commit fails). Worker
    commits run hook-free since M2.8b1, so hook text is irrelevant."""
    real = runner.run_cmd
    seen = []

    def fake(argv, **kw):
        if "commit" in list(argv or []):
            seen.append(1)
            if len(seen) > after:
                return (1, "", "fatal: forced git error (no hooks)")
        return real(argv, **kw)

    return mock.patch.object(runner, "run_cmd", side_effect=fake)


def _pipeline_state(case):
    """Real temp-git candidate + isolated scheduler row, mirroring production."""
    base = CommitThroughHookTests(methodName="test_real_candidate_gets_committed")
    base.root = case.root
    state, work, _ = base._candidate()
    run_cmd(["git", "-C", str(work), "checkout", "-qb", "test/branch"])
    (work / "yarn.lock").write_text(
        "# yarn lockfile v1\n\n\"@scope/pkg@^1.0.1\":\n  version \"1.0.1\"\n")
    key = "test-repo+@scope/pkg+1.0.1+stable"
    state.update(queue_key=key, dep="@scope/pkg", target="1.0.1",
                 declared="^1.0.0", lane="main", scopes=["devDependencies"])
    store.save(state)
    repos.save(dict(PROFILE))
    item = {"key": key, "repo": "test-repo", "kind": "dep-update",
            "dep": "@scope/pkg", "target": "1.0.1", "run_id": state["id"],
            "status": "running", "attempts": 0}
    st = sched.load("test-repo")
    st.update(enabled=True, execute_enabled=True, queue=[item],
              current_job={"id": key, "run_id": state["id"]})
    sched.save(st)
    return state, work, item


class CommitBlockedPipelineTests(unittest.TestCase):
    """A failed worker commit must fail the actual pipeline, not just a helper.

    The helper field alone is insufficient: fix/review execution must
    stop before downstream steps, defensive finalization must refuse a
    direct-ok call, promotion must refuse, and same-run recovery must
    retry ONLY the commit (no fix-model rerun) before fresh HEAD-bound
    review/checks. All with real temp git repos; the failures are
    non-hook git errors (worker commits run hook-free since M2.8b1).
    """

    def setUp(self):
        self.root = make_home(self)

    def test_fix_commit_block_stops_pipeline_and_finalizer_refuses_ok(self):
        state, work, item = _pipeline_state(self)
        run_id = state["id"]
        ex = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(runner.execute(state, PROFILE, ex, work))
        current = store.load(run_id)
        self.assertEqual(current["steps"]["fix"]["status"], "blocked")
        self.assertNotEqual(current["steps"]["fix"]["status"], "done")
        block = current.get("commitBlocked")
        self.assertIsNotNone(block)
        self.assertEqual(block.get("step"), "fix")
        self.assertNotEqual(block.get("exit"), 0)
        self.assertTrue((current.get("waitReason") or "")
                        .startswith("commit-blocked:"))
        # The pipeline stops: no review, no checks, no drafts downstream.
        self.assertEqual(ex.calls, ["triage", "analyze", "fix"])
        self.assertEqual(current["steps"]["review"]["status"], "pending")
        self.assertEqual(current["steps"]["e2e"]["status"], "pending")
        # Defensive finalization refuses even a direct-ok call.
        finished = sched._finish_run("test-repo", item, True,
                                     state["title"], "probe")
        self.assertFalse(finished.get("ok", False))
        q = sched.load("test-repo")["queue"][0]
        self.assertFalse(q.get("verified"))
        self.assertNotEqual(
            (q.get("status"), q.get("verified")), ("waiting-review", True))
        self.assertNotIn("test/plan-only", q.get("note") or "")
        # Promotion refuses the uncommitted candidate.
        promotion = branches.promotion_readiness(store.load(run_id))
        self.assertFalse(promotion["ready"])
        names = {c["name"]: c["ok"] for c in promotion["criteria"]}
        self.assertFalse(names["no hook-blocked local commit"])

    def test_review_apply_commit_block_fails_review(self):
        # The fix commit lands; the applied review change does not
        # (first commit succeeds, later ones fail).
        state, work, item = _pipeline_state(self)
        run_id = state["id"]
        findings = ("- VALID: lock entry missing for target\n"
                    "REVIEW-VERDICT: DIRTY\n")
        ex = ApplyDirtier(script={"review": [(0, findings)]})
        with _fail_commit_calls(after=1):
            self.assertFalse(runner.execute(state, PROFILE, ex, work))
        current = store.load(run_id)
        # The fix itself committed; the applied review change did not.
        self.assertEqual(current["steps"]["fix"]["status"], "done")
        code, head, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        self.assertNotEqual(head.strip(), current.get("baseCommit"))
        self.assertNotEqual(current["steps"]["review"]["status"], "done")
        block = current.get("commitBlocked")
        self.assertIsNotNone(block)
        self.assertEqual(block.get("step"), "review")
        self.assertEqual(current["steps"]["e2e"]["status"], "pending")
        finished = sched._finish_run("test-repo", item, True,
                                     state["title"], "probe")
        self.assertFalse(finished.get("ok", False))
        q = sched.load("test-repo")["queue"][0]
        self.assertFalse(q.get("verified"))
        self.assertFalse(
            branches.promotion_readiness(store.load(run_id))["ready"])

    def test_same_run_commit_only_recovery_no_fix_model_rerun(self):
        state, work, item = _pipeline_state(self)
        run_id = state["id"]
        branch = state.get("branch")
        ex1 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(runner.execute(state, PROFILE, ex1, work))
        self.assertEqual(ex1.calls.count("fix"), 1)
        # A resume while the git error persists retries nothing but
        # the commit: still blocked, and the fix model never runs
        # again.
        ex2 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(
                runner.execute(store.load(run_id), PROFILE, ex2, work))
        self.assertNotIn("fix", ex2.calls)
        self.assertNotIn("review", ex2.calls)
        self.assertIsNotNone(store.load(run_id).get("commitBlocked"))
        # The transient git error clears (no code change); the same
        # run recovers with a commit-only retry, then fresh
        # HEAD-bound review/checks — still no fix-model rerun, no new
        # run/branch.
        ex3 = runner.FakeExecutor()
        self.assertTrue(runner.execute(store.load(run_id), PROFILE, ex3, work))
        self.assertNotIn("fix", ex3.calls)
        self.assertIn("review", ex3.calls)
        self.assertIn("shell:fixture unit", ex3.calls)
        current = store.load(run_id)
        self.assertEqual(current["id"], run_id)
        self.assertEqual(current.get("branch"), branch)
        self.assertNotIn("commitBlocked", current)
        self.assertEqual(current["steps"]["fix"]["status"], "done")
        self.assertEqual(current["steps"]["review"]["status"], "done")
        self.assertEqual(current["steps"]["e2e"]["status"], "done")
        code, head, _ = run_cmd(["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        self.assertNotEqual(head.strip(), current.get("baseCommit"))
        self.assertEqual(current.get("workCommit"), head.strip())
        snap = runner.source_snapshot(work)
        for step in ("review", "e2e"):
            self.assertEqual(
                (current.get("verifiedFingerprints") or {}).get(step),
                snap["fingerprint"])
            self.assertEqual(
                (current.get("verifiedCommits") or {}).get(step), head.strip())
        finished = sched._finish_run("test-repo", item, True,
                                     state["title"], "probe")
        self.assertTrue(finished.get("ok", False))
        q = sched.load("test-repo")["queue"][0]
        self.assertEqual(q.get("status"), "waiting-review")
        self.assertTrue(q.get("verified"))
        self.assertTrue(
            branches.promotion_readiness(store.load(run_id))["ready"])

    def test_discarded_candidate_plus_unrelated_commit_stays_blocked(self):
        """An unrelated manifest commit is not the candidate's resolution.

        Real temp git: fail the 1.0.1 candidate commit, discard it,
        commit only an unrelated package description (dependency
        still 1.0.0). Resume must stay blocked with zero downstream
        model/check calls — any manifest delta is not
        exact-candidate proof.
        """
        import json as _json
        state, work, _ = _pipeline_state(self)
        run_id = state["id"]
        ex1 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(runner.execute(state, PROFILE, ex1, work))
        self.assertIsNotNone(store.load(run_id).get("commitBlocked"))
        # Discard the failed candidate, then commit unrelated metadata.
        hook = work / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\nexit 0\n")
        for name in ("package.json", "yarn.lock"):
            code, raw, _ = run_cmd(
                ["git", "-C", str(work), "show", "HEAD:" + name])
            self.assertEqual(code, 0)
            (work / name).write_text(raw)
        pkg = _json.loads((work / "package.json").read_text())
        pkg["description"] = "unrelated metadata change"
        (work / "package.json").write_text(_json.dumps(pkg) + "\n")
        self.assertEqual(
            run_cmd(["git", "-C", str(work), "add",
                     "package.json", "yarn.lock"])[0], 0)
        code, _, err = run_cmd(
            ["git", "-C", str(work), "-c", "user.name=t",
             "-c", "user.email=t@t", "commit", "-qm",
             "chore: unrelated metadata change"])
        self.assertEqual(code, 0, err)
        # The dependency is still 1.0.0: only metadata moved.
        live = _json.loads((work / "package.json").read_text())
        self.assertEqual(live["devDependencies"]["@scope/pkg"], "^1.0.0")
        ex2 = runner.FakeExecutor()
        self.assertFalse(
            runner.execute(store.load(run_id), PROFILE, ex2, work))
        recovered = store.load(run_id)
        self.assertIsNotNone(recovered.get("commitBlocked"))
        self.assertEqual(ex2.calls, [])
        self.assertNotEqual(recovered["steps"]["fix"]["status"], "done")

    def test_unrelated_new_file_never_auto_committed_on_recovery(self):
        """The identity covers the full changed path SET, not just bytes.

        Real temp git: after a blocked candidate, an unrelated NEW file
        appears. Same-run recovery must NOT sweep the new file into
        a commit-only retry — it stays blocked with zero downstream
        model/check calls.
        """
        state, work, _ = _pipeline_state(self)
        run_id = state["id"]
        ex1 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(runner.execute(state, PROFILE, ex1, work))
        self.assertIsNotNone(store.load(run_id).get("commitBlocked"))
        (work / "unrelated-user-note.txt").write_text(
            "not part of the rejected candidate\n")
        hook = work / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\nexit 0\n")
        ex2 = runner.FakeExecutor()
        self.assertFalse(
            runner.execute(store.load(run_id), PROFILE, ex2, work))
        recovered = store.load(run_id)
        self.assertIsNotNone(recovered.get("commitBlocked"))
        self.assertEqual(ex2.calls, [])
        self.assertNotEqual(recovered["steps"]["fix"]["status"], "done")
        # The unrelated file is preserved, never committed.
        self.assertTrue((work / "unrelated-user-note.txt").is_file())
        code, names, _ = run_cmd(
            ["git", "-C", str(work), "ls-files", "--",
             "unrelated-user-note.txt"])
        self.assertEqual(code, 0)
        self.assertEqual(names.strip(), "")

    def test_index_lock_stage_failure_blocks_without_downstream(self):
        """A real `.git/index.lock` stage failure blocks, never skips.

        Real temp git (no mocks for the error itself): `git add -u`
        fails, so execution must stop with an explicit blocked record
        (stage/exit/stderr preserved) and zero review/check calls.
        """
        state, work, _ = _pipeline_state(self)
        run_id = state["id"]
        lock = work / ".git" / "index.lock"
        lock.write_text("isolated forced lock contention\n")
        try:
            ex = runner.FakeExecutor()
            self.assertFalse(runner.execute(state, PROFILE, ex, work))
            current = store.load(run_id)
            self.assertEqual(ex.calls, ["triage", "analyze", "fix"])
            self.assertEqual(current["steps"]["fix"]["status"], "blocked")
            block = current.get("commitBlocked")
            self.assertIsNotNone(block)
            self.assertTrue(block.get("stage", "").startswith("add"))
            self.assertNotEqual(block.get("exit"), 0)
            self.assertTrue((block.get("stderr") or "").strip())
            self.assertTrue((current.get("waitReason") or "").startswith(
                "commit-blocked:"))
            self.assertEqual(current["steps"]["review"]["status"], "pending")
        finally:
            lock.unlink(missing_ok=True)
        # Barrier continuity: with the lock gone the commit still
        # fails, so the same run stays blocked (still zero fix-model
        # reruns).
        ex2 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(
                runner.execute(store.load(run_id), PROFILE, ex2, work))
        self.assertNotIn("fix", ex2.calls)
        self.assertNotIn("review", ex2.calls)
        self.assertIsNotNone(store.load(run_id).get("commitBlocked"))

    def test_already_committed_resolution_clears_stale_block_truthfully(self):
        state, work, _ = _pipeline_state(self)
        run_id = state["id"]
        ex1 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(runner.execute(state, PROFILE, ex1, work))
        self.assertIsNotNone(store.load(run_id).get("commitBlocked"))
        # The operator commits the exact candidate by hand (raw git,
        # where the conventional subject passes the installed hook):
        # resume must accept that truthful resolution without
        # rerunning the fix model.
        hook = work / ".git" / "hooks" / "commit-msg"
        hook.write_text(HOOK)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        code, _, err = run_cmd(
            ["git", "-C", str(work), "-c", "user.name=t",
             "-c", "user.email=t@t", "commit", "-qm",
             "chore(deps): bump @scope/pkg to 1.0.1"])
        self.assertEqual(code, 0, err)
        ex2 = runner.FakeExecutor()
        self.assertTrue(runner.execute(store.load(run_id), PROFILE, ex2, work))
        self.assertNotIn("fix", ex2.calls)
        current = store.load(run_id)
        self.assertNotIn("commitBlocked", current)
        self.assertEqual(current["steps"]["fix"]["status"], "done")

    def test_legacy_block_without_candidate_identity_waits_without_commit(self):
        """Pre-identity persisted blocks never auto-commit whatever is there.

        Real temp git + actual execute/resume path: after a failed
        candidate, drop the recorded candidate identity (the old
        pre-identity persisted shape). Resume must stay blocked with
        zero downstream model/check calls, the block and the uncommitted
        changes preserved, and HEAD untouched — explicit manual
        reconciliation, never invented proof. An incomplete identity
        (complete False) waits the same way.
        """
        state, work, _ = _pipeline_state(self)
        run_id = state["id"]
        ex1 = runner.FakeExecutor()
        with _fail_commit_calls():
            self.assertFalse(runner.execute(state, PROFILE, ex1, work))
        self.assertIsNotNone(store.load(run_id).get("commitBlocked"))
        code, head_before, _ = run_cmd(
            ["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        # Legacy shape: the persisted block carries no candidate identity.
        legacy = store.load(run_id)
        self.assertIn("candidate", legacy.get("commitBlocked") or {})
        legacy["commitBlocked"].pop("candidate", None)
        store.save(legacy)
        hook = work / ".git" / "hooks" / "commit-msg"
        hook.write_text("#!/bin/sh\nexit 0\n")
        ex2 = runner.FakeExecutor()
        self.assertFalse(
            runner.execute(store.load(run_id), PROFILE, ex2, work))
        recovered = store.load(run_id)
        self.assertIsNotNone(recovered.get("commitBlocked"))
        self.assertNotIn("candidate", recovered.get("commitBlocked") or {})
        self.assertEqual(ex2.calls, [])
        self.assertTrue((recovered.get("waitReason") or "").startswith(
            "commit-blocked:"))
        self.assertIn("identity", recovered.get("waitReason") or "")
        self.assertNotEqual(recovered["steps"]["fix"]["status"], "done")
        code, head_after, _ = run_cmd(
            ["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        self.assertEqual(head_after.strip(), head_before.strip())
        # Incomplete identity waits the same way, before any commit.
        partial = store.load(run_id)
        partial["commitBlocked"]["candidate"] = {
            "complete": False, "paths": ["package.json"], "files": {}}
        store.save(partial)
        ex3 = runner.FakeExecutor()
        self.assertFalse(
            runner.execute(store.load(run_id), PROFILE, ex3, work))
        again = store.load(run_id)
        self.assertIsNotNone(again.get("commitBlocked"))
        self.assertEqual(ex3.calls, [])
        code, head_again, _ = run_cmd(
            ["git", "-C", str(work), "rev-parse", "HEAD"])
        self.assertEqual(code, 0)
        self.assertEqual(head_again.strip(), head_before.strip())


SENTINEL_HOOK = ("#!/bin/sh\n"
                 "echo hooked >> \"$GIT_SENTINEL\"\n"
                 "echo 'hook must never run' >&2\n"
                 "exit 1\n")


class HookFreeWorkerCommitTests(unittest.TestCase):
    # M2.8b1: worker commits never run repo hooks. A rejecting
    # pre-commit AND commit-msg hook (each touching a sentinel
    # file) must not stop the commit, and the sentinel proves
    # neither hook ran.

    def setUp(self):
        self.root = make_home(self)

    def _repo_with_rejecting_hooks(self):
        work, base = make_repo(self.root)
        sentinel = self.root / "hook-ran.txt"
        for name in ("pre-commit", "commit-msg"):
            hook = work / ".git" / "hooks" / name
            text = SENTINEL_HOOK.replace(
                "$GIT_SENTINEL", str(sentinel))
            hook.write_text(text)
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        return work, base, sentinel

    def test_rejecting_hooks_never_run(self):
        work, base, sentinel = self._repo_with_rejecting_hooks()
        state = make_state("chore(deps): bump @scope/pkg to 1.0.1")
        work = move_into_run_dir(self.root, state, work)
        (work / "package.json").write_text(
            '{"name": "t", "devDependencies": {"@scope/pkg": "^1.0.1"}}\n')
        state["baseCommit"] = base
        store.save(state)
        got = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(got.get("committed"), got)
        self.assertFalse(sentinel.exists())
        self.assertNotIn("commitBlocked", state)

    def test_hooks_path_config_never_runs(self):
        # The nitro shape: core.hooksPath points at a dir of
        # rejecting hooks. The worker commit must ignore it too.
        work, base = make_repo(self.root)
        hooks_dir = self.root / "huskyish"
        hooks_dir.mkdir()
        sentinel = self.root / "hook-ran.txt"
        for name in ("pre-commit", "commit-msg"):
            hook = hooks_dir / name
            hook.write_text(SENTINEL_HOOK.replace(
                "$GIT_SENTINEL", str(sentinel)))
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        assert run_cmd(
            ["git", "-C", str(work), "config", "core.hooksPath",
             str(hooks_dir)])[0] == 0
        state = make_state("chore(deps): bump @scope/pkg to 1.0.1")
        work = move_into_run_dir(self.root, state, work)
        (work / "package.json").write_text(
            '{"name": "t", "devDependencies": {"@scope/pkg": "^1.0.1"}}\n')
        state["baseCommit"] = base
        store.save(state)
        got = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(got.get("committed"), got)
        self.assertFalse(sentinel.exists())

    def test_commit_message_shape(self):
        # Quality without hooks: the subject is conventional and
        # every body line stays short.
        for title in ("chore(deps): bump @scope/pkg to 1.0.1",
                      "not a conventional title at all"):
            subject, body = runner.commit_subject_for(
                {"id": "run-shape-1", "title": title}, "fix")
            self.assertTrue(
                runner._CONVENTIONAL_SUBJECT_RE.match(subject),
                subject)
            for line in body.splitlines():
                self.assertLessEqual(len(line), 100, line)


if __name__ == "__main__":
    unittest.main()
