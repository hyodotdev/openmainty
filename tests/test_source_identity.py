"""Source verification identity: verification binds to content, not just HEAD.

Codex reproduced the acceptance gap with a temp real git repo: commit
a.txt='before', record runner.work_commit -> HEAD; edit tracked a.txt to
'after' (workSeq+1), runner.work_commit -> the SAME HEAD. So verifiedCommits,
promotion readiness, and graph green evidence could certify different dirty
source versions. These tests pin the stronger contract with actual git
repos (tracked edits, untracked source, ignored/cache files, reviewer edits,
feature-branch work commits, resume invalidation, legacy runs) instead of
HEAD-only assertions.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import branches, graph, pcconfig, repos, runner, serve, store  # noqa: E402
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


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


def make_git_repo(root: Path, name="co") -> Path:
    co = root / name
    co.mkdir()
    git("init", "-b", "main", cwd=co)
    git("config", "user.email", "test@localhost", cwd=co)
    git("config", "user.name", "test", cwd=co)
    (co / ".gitignore").write_text("*.log\nnode_modules/\n", encoding="utf-8")
    (co / "a.txt").write_text("before\n", encoding="utf-8")
    git("add", "-A", cwd=co)
    git("commit", "-qm", "init", cwd=co)
    return co


class HomeMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.co = make_git_repo(self.root)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["fakerepo"] = str(self.co)
        pcconfig.save(pc)

    def make_run(self, **fields):
        state = store.create("fakerepo", "1", "fake issue",
                             git("rev-parse", "HEAD", cwd=self.co).strip(),
                             {"worker": "test"})
        for key, val in fields.items():
            state[key] = val
        store.claim("fakerepo", "1", state["id"])
        return state

    def bind_done(self, state, work, *steps):
        for step in steps:
            store.mark(state, step, "done", exit_code=0, evidence=[f"{step}.x"],
                       note="bound in test")
            runner._bind_verification(state, work, step)


class FingerprintTests(HomeMixin):
    def test_tracked_edit_keeps_head_but_changes_fingerprint(self):
        before = runner.source_snapshot(self.co)
        head_before = runner.work_commit(self.co, {})
        (self.co / "a.txt").write_text("after\n", encoding="utf-8")
        head_after = runner.work_commit(self.co, {})
        after = runner.source_snapshot(self.co)
        # The Codex reproduction: HEAD cannot tell the dirty tree apart.
        self.assertEqual(head_before, head_after)
        # The fix: the content fingerprint does.
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before["head"], after["head"])

    def test_new_untracked_source_file_changes_fingerprint(self):
        before = runner.source_snapshot(self.co)
        (self.co / "b.py").write_text("print('new')\n", encoding="utf-8")
        after = runner.source_snapshot(self.co)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertIn("b.py", after["untracked"])

    def test_ignored_generated_and_cache_files_do_not_change_fingerprint(self):
        before = runner.source_snapshot(self.co)
        (self.co / "debug.log").write_text("junk\n", encoding="utf-8")  # ignored
        nm = self.co / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("1\n", encoding="utf-8")  # ignored+noise
        (self.co / "build").mkdir()
        (self.co / "build" / "out.js").write_text("1\n", encoding="utf-8")
        after = runner.source_snapshot(self.co)
        self.assertEqual(before["fingerprint"], after["fingerprint"])

    def test_commit_preserves_content_identity_across_representations(self):
        (self.co / "a.txt").write_text("after\n", encoding="utf-8")
        dirty = runner.source_snapshot(self.co)
        git("add", "-A", cwd=self.co)
        git("commit", "-qm", "work", cwd=self.co)
        committed = runner.source_snapshot(self.co)
        # Same bytes, committed vs dirty: one stable identity, distinct head.
        self.assertEqual(dirty["fingerprint"], committed["fingerprint"])
        self.assertNotEqual(dirty["head"], committed["head"])


class BindingTests(HomeMixin):
    def test_reviewer_modification_invalidates_prior_checks(self):
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main", branch=None)
        self.bind_done(state, work, "reproduce", "review", "e2e")
        self.assertEqual(
            sorted(graph._fresh_verifications(store.load(state["id"]))),
            ["e2e", "reproduce", "review"])
        # Later content change with the SAME head (the acceptance gap).
        (work / "a.txt").write_text("reviewer edit\n", encoding="utf-8")
        self.assertEqual(runner.work_commit(work, state), state["workCommit"])
        # Detection runs on a fresh snapshot, exactly as execute/resume does
        # before invalidating: the recorded proof no longer matches reality.
        runner._refresh_source(state, work)
        state = store.load(state["id"])
        self.assertEqual(store.verification_status(state)["review"], "stale")
        self.assertEqual(graph._fresh_verifications(state), [])
        self.assertFalse(branches.promotion_readiness(state)["ready"])
        reset = store.invalidate_stale_verifications(state)
        self.assertEqual(sorted(reset), ["e2e", "reproduce", "review"])

    def test_no_change_case_stays_fresh(self):
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main", branch=None)
        self.bind_done(state, work, "reproduce", "review", "e2e")
        state["artifacts"] = {"release_notes": "release_notes.md"}
        store.save(state)
        fresh = runner.source_snapshot(work)
        self.assertEqual(
            store.invalidate_stale_verifications(
                state, current_fingerprint=fresh["fingerprint"]), [])
        state = store.load(state["id"])
        self.assertEqual(
            sorted(graph._fresh_verifications(state)),
            ["e2e", "reproduce", "review"])
        self.assertTrue(branches.promotion_readiness(state)["ready"])

    def test_legacy_missing_proof_is_never_green(self):
        state = self.make_run()
        for step in ("reproduce", "review", "e2e"):
            store.mark(state, step, "done", exit_code=0, note="legacy")
            # HEAD-only proof, as recorded before fingerprints existed.
            state["verifiedCommits"][step] = state["workCommit"]
        state["artifacts"] = {"release_notes": "release_notes.md"}
        store.save(state)
        state = store.load(state["id"])
        self.assertEqual(set(store.verification_status(state).values()), {"legacy"})
        self.assertEqual(graph._fresh_verifications(state), [])
        promo = branches.promotion_readiness(state)
        self.assertFalse(promo["ready"])
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(body["source"]["proof"], "legacy")
        self.assertTrue(all(b["proof"] == "legacy" for b in body["verification"]))


class FeatureCommitTests(HomeMixin):
    def test_named_branch_work_gets_local_commit_base_head_distinct(self):
        state = self.make_run(branch="codex/maint/widget",
                              base_branch="main", title="widget fix")
        base = state["baseCommit"]
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=state["branch"])
        self.assertEqual(work, store.run_dir(state["id"]) / "work")
        (work / "a.txt").write_text("maintained\n", encoding="utf-8")
        (work / "new.py").write_text("print('new')\n", encoding="utf-8")
        res = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(res["committed"], res)
        self.assertEqual(res["branch"], "codex/maint/widget")
        runner._refresh_source(state, work)
        state = store.load(state["id"])
        # Exact base/head commits stay distinct fields; the branch holds work.
        self.assertEqual(state["baseCommit"], base)
        self.assertNotEqual(state["workCommit"], base)
        self.assertEqual(state["workCommit"], res["head"])
        self.assertIn("work-commit",
                      [i["kind"] for i in state["interventions"]])
        self.assertEqual(git("branch", "--show-current", cwd=work).strip(),
                         "codex/maint/widget")
        self.assertIn("maintained",
                      git("show", "HEAD:a.txt", cwd=work))
        # The user's checkout is untouched; nothing published anywhere.
        self.assertEqual((self.co / "a.txt").read_text(encoding="utf-8"),
                         "before\n")
        self.assertEqual(git("remote", cwd=work).strip(), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.co).strip(), base)

    def test_no_commit_without_named_branch_or_foreign_tree(self):
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main", branch=None)
        (work / "a.txt").write_text("x\n", encoding="utf-8")
        res = runner.maybe_commit_worktree(state, work, "fix")
        self.assertFalse(res["committed"])
        foreign = self.root / "elsewhere"
        foreign.mkdir()
        state["branch"] = "codex/maint/widget"
        store.save(state)
        res2 = runner.maybe_commit_worktree(state, foreign, "fix")
        self.assertFalse(res2["committed"])

    def test_resume_reruns_stale_checks_after_tracked_edit(self):
        state = self.make_run(branch="codex/maint/widget", base_branch="main")
        profile = repos.load("fakerepo")
        self.assertTrue(runner.execute(state, profile, runner.FakeExecutor(),
                                       self.co))
        work = store.run_dir(state["id"]) / "work"
        # Same HEAD, different bytes: the exact stale-content scenario.
        (work / "README.md").write_text("# changed\n", encoding="utf-8")
        ex = runner.FakeExecutor()
        state = store.load(state["id"])
        self.assertTrue(runner.execute(state, profile, ex, self.co))
        for step in ("reproduce", "review"):
            self.assertIn(step, ex.calls, f"{step} must re-run")
        self.assertIn("shell:make unit", ex.calls)
        self.assertNotIn("triage", ex.calls)
        self.assertNotIn("fix", ex.calls)


class PromotionScopeTests(HomeMixin):
    def _fresh_next_state(self, e2e_cmds):
        state = self.make_run(lane="next", update_kind="major")
        work = runner.ensure_branch_workdir(state, self.co, "main", branch=None)
        store.mark(state, "review", "done", exit_code=0, note="reviewed")
        runner._bind_verification(state, work, "review")
        store.mark(state, "e2e", "done", exit_code=0,
                   command=e2e_cmds, note="checks passed")
        runner._bind_verification(state, work, "e2e")
        state["artifacts"] = {"release_notes": "release_notes.md"}
        store.save(state)
        return store.load(state["id"])

    def test_typecheck_only_is_not_promotion_ready_for_breaking(self):
        state = self._fresh_next_state([["yarn", "typecheck"]])
        promo = branches.promotion_readiness(state)
        self.assertFalse(promo["ready"])
        names = [c["name"] for c in promo["criteria"] if not c["ok"]]
        self.assertTrue(any("beyond typecheck" in n for n in names),
                        promo["criteria"])

    def test_unit_coverage_makes_breaking_promotion_ready(self):
        state = self._fresh_next_state([["yarn", "typecheck"], ["yarn", "test"]])
        self.assertTrue(branches.promotion_readiness(state)["ready"])


class ReadTimeValidationTests(HomeMixin):
    """Reads validate the LIVE bytes — proof must not stay CURRENT after
    an external worktree edit just because no resume ran yet. Actual git
    fixture (tracked edit keeps HEAD), read-only checks, and an
    unreadable worktree reports unknown — never a fabricated green."""

    def _bound_state(self):
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=None)
        self.bind_done(state, work, "reproduce", "review", "e2e")
        state["artifacts"] = {"release_notes": "release_notes.md"}
        store.save(state)
        return store.load(state["id"]), work

    def test_external_edit_is_stale_at_read_before_resume(self):
        state, work = self._bound_state()
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(body["source"]["proof"], "current")
        (work / "a.txt").write_text("external edit\n", encoding="utf-8")
        self.assertEqual(runner.work_commit(work, state),
                         state["workCommit"])
        run_file = store.run_file(state["id"])
        before = run_file.read_bytes()
        body = serve.api_work({"run": state["id"]})
        g = graph.build("fakerepo")
        # The read itself mutates nothing: no resume, no silent repair.
        self.assertEqual(run_file.read_bytes(), before)
        self.assertEqual((self.co / "a.txt").read_text(encoding="utf-8"),
                         "before\n")
        # ... but nothing reports CURRENT anymore.
        self.assertEqual(body["source"]["proof"], "stale")
        self.assertTrue(all(b["proof"] == "stale"
                            for b in body["verification"]))
        self.assertFalse(body["promotion"]["ready"])
        self.assertFalse(body["outcome"]["promotion_ready"])
        verified = [n for n in g["nodes"] if n["type"] == "verification"]
        self.assertEqual(verified, [])

    def test_recorded_scope_stays_history_after_live_edit(self):
        state, work = self._bound_state()
        rdir = store.run_dir(state["id"])
        (rdir / "review.scope.json").write_text(json.dumps({
            "base_commit": state["baseCommit"], "base_ref": "main",
            "head": state["workCommit"],
            "fingerprint": state["workTreeFingerprint"],
            "lane": "main", "branch": "x", "base_stat": "a.txt | 1 +",
            "diff_capped": False,
            "bare_git_diff_empty": True}), encoding="utf-8")
        (rdir / "review.diff.txt").write_text("diff", encoding="utf-8")
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(body["source"]["proof"], "current")
        rev = next(b for b in body["verification"]
                   if b["step"] == "review")
        self.assertIn(rev["proof"], ("fresh", "current"))
        self.assertEqual(body["report"]["review_scope"]["status"],
                         "recorded")
        scope_before = (rdir / "review.scope.json").read_bytes()
        names_before = sorted(p.name for p in rdir.iterdir()
                              if p.is_file())
        (work / "a.txt").write_text("external edit\n", encoding="utf-8")
        run_file = store.run_file(state["id"])
        before = run_file.read_bytes()
        body = serve.api_work({"run": state["id"]})
        # The read mutates nothing: no resume, no model/job artifacts.
        self.assertEqual(run_file.read_bytes(), before)
        self.assertEqual((rdir / "review.scope.json").read_bytes(),
                         scope_before)
        self.assertEqual(sorted(p.name for p in rdir.iterdir()
                                if p.is_file()), names_before)
        # Live proof goes stale, but the recorded scope stays honest
        # history — never a new current certification.
        self.assertIn(body["source"]["proof"], ("stale", "unknown"))
        rev = next(b for b in body["verification"]
                   if b["step"] == "review")
        self.assertIn(rev["proof"], ("stale", "unknown"))
        self.assertEqual(body["report"]["review_scope"]["status"],
                         "recorded")

    def test_missing_worktree_reports_unknown_never_green(self):
        import shutil
        state, work = self._bound_state()
        shutil.rmtree(work)
        run_file = store.run_file(state["id"])
        before = run_file.read_bytes()
        body = serve.api_work({"run": state["id"]})
        g = graph.build("fakerepo")
        self.assertEqual(run_file.read_bytes(), before)
        self.assertEqual(body["source"]["proof"], "unknown")
        self.assertTrue(all(b["proof"] == "unknown"
                            for b in body["verification"]))
        self.assertFalse(body["promotion"]["ready"])
        self.assertFalse(body["outcome"]["promotion_ready"])
        verified = [n for n in g["nodes"] if n["type"] == "verification"]
        self.assertEqual(verified, [])
        unknown = [n for n in g["nodes"]
                   if n["type"] == "run"][0]["detail"]
        self.assertIn("unverified", unknown)


class ApiWorkTests(HomeMixin):
    def test_enriched_fields_from_recorded_artifacts(self):
        state = self.make_run()
        rdir = store.run_dir(state["id"])
        (rdir / "fix.diffstat.txt").write_text(
            " src/a.ts | 3 ++-\n src/b.ts | 1 +\n 2 files changed, 3 insertions(+)\n",
            encoding="utf-8")
        (rdir / "triage.log.txt").write_text("ACTIONABLE: bump\n",
                                             encoding="utf-8")
        (rdir / "pr.md").write_text("Fixes #1.\n", encoding="utf-8")
        state["artifacts"] = {"pr": "pr.md"}
        store.mark(state, "e2e", "done", exit_code=0,
                   command=[["yarn", "typecheck"]], evidence=["e2e.log.txt"],
                   note="1 check(s) passed")
        store.save(state)
        body = serve.api_work({"run": state["id"]})
        # Backwards compatibility: existing keys stay.
        for key in ("state", "commands", "tests", "artifacts", "promotion",
                    "related_events", "links"):
            self.assertIn(key, body)
        # Grounded new detail: parsed (not invented) changed files.
        self.assertEqual(
            [f["path"] for f in body["changed_files"]],
            ["src/a.ts", "src/b.ts"])
        self.assertEqual(body["changed_files_source"], "fix.diffstat.txt")
        self.assertIn("2 files changed", body["changed_files_total"])
        # Shell-vs-model honesty: e2e here is types-only, not runtime proof.
        e2e = next(b for b in body["verification"] if b["step"] == "e2e")
        self.assertEqual(e2e["kind"], "shell")
        self.assertEqual(e2e["scopes"], ["types"])
        self.assertEqual(e2e["shell_commands"], ["yarn typecheck"])
        self.assertEqual(body["outcome"]["triage"], "ACTIONABLE")
        self.assertIsNone(body["outcome"]["summary"])
        self.assertTrue(any(f["name"] == "pr.md" for f in body["evidence_files"]))
        self.assertEqual(body["source"]["base_commit"], state["baseCommit"])


class DraftInvalidationTests(HomeMixin):
    """Same-run forward correction must not leave stale drafts behind.

    Real git fixture mirroring the Nitro 647fecf gap: the old fix log
    names the removed pin test, the forward commit deletes it, and the
    same-run resume must archive the stale drafts and regenerate
    lock-only drafts at the new head with no added-test claim.
    """

    def test_forward_removal_invalidates_and_regenerates_lock_only(self):
        state = self.make_run(branch="codex/maint/slider-5-2-1",
                              base_branch="main", title="slider 5.2.1")
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=state["branch"])
        old_test = work / "sliderDependency.test.ts"
        old_test.write_text("EXPECTED slider 5.2.1 pin\n", encoding="utf-8")
        (work / "yarn.lock").write_text("slider 5.2.1\n", encoding="utf-8")
        git("add", "-A", cwd=work)
        git("commit", "-qm", "old fix with pin test", cwd=work)
        runner._refresh_source(state, work)
        state = store.load(state["id"])
        for step in ("review", "e2e"):
            store.mark(state, step, "done", exit_code=0,
                       evidence=[f"{step}.x"], note=f"{step} passed")
            runner._bind_verification(state, work, step)
        rdir = store.run_dir(state["id"])
        (rdir / "fix.log.txt").write_text(
            "CHANGED sliderDependency.test.ts added pin test\n",
            encoding="utf-8")
        for step in ("release", "announce"):
            store.mark(state, step, "done", exit_code=0, evidence=[],
                       note="drafts written")
            store.mark(state, step, "running",
                       command=["openmainty", f"draft:{step}"])
            store.mark(state, step, "done", exit_code=0, evidence=[],
                       note="drafts written (dry-run only; nothing posted)")
        (rdir / "pr.md").write_text(
            "sliderDependency.test.ts added pin test\n", encoding="utf-8")
        state["artifacts"] = {"pr": "pr.md",
                              "release_notes": "release_notes.md"}
        (rdir / "release_notes.md").write_text(
            "sliderDependency.test.ts added pin test\n", encoding="utf-8")
        store.save(state)
        old_head = store.load(state["id"])["workCommit"]
        # Forward correction on the OWNED worktree: remove the pin test,
        # keep only the lock change, commit forward (no amend).
        (work / "sliderDependency.test.ts").unlink()
        git("add", "-A", cwd=work)
        git("commit", "-qm", "remove pin test, lock only", cwd=work)
        state = store.load(state["id"])
        state["workCommit"] = git("rev-parse", "HEAD", cwd=work).strip()
        store.save(state)
        runner._refresh_source(store.load(state["id"]), work)
        state = store.load(state["id"])
        fresh = runner.source_snapshot(work)
        reset = store.invalidate_stale_verifications(
            state, current_fingerprint=fresh["fingerprint"])
        self.assertIn("review", reset)
        self.assertIn("e2e", reset)
        self.assertIsNone(state["steps"]["review"]["started_at"])
        runner._invalidate_stale_drafts(state, work, reset)
        state = store.load(state["id"])
        self.assertEqual(state["steps"]["release"]["status"], "pending")
        self.assertEqual(state["steps"]["announce"]["status"], "pending")
        self.assertIsNone(state["steps"]["release"]["started_at"])
        hist = [p.name for p in rdir.glob("pr.historical-*.md")]
        self.assertTrue(hist, "superseded draft must be archived")
        self.assertIn("sliderDependency.test.ts",
                      (rdir / hist[0]).read_text(encoding="utf-8"))
        ctx = runner._draft_context(state, work)
        self.assertIn("yarn.lock", ctx)
        self.assertIn(state["workCommit"][:12], ctx)
        runner.write_drafts(state, repos.load("fakerepo"), context=ctx)
        new_pr = (rdir / "pr.md").read_text(encoding="utf-8")
        self.assertIn(state["workCommit"][:12], new_pr)
        self.assertNotEqual(old_head, state["workCommit"])
        # The regenerated draft carries the current head, not the old
        # removed-test wording from the stale fix log alone.
        self.assertNotIn("sliderDependency.test.ts added pin test", new_pr)


class SkippedReproduceProofTests(HomeMixin):
    """Dep-maintenance reproduce skips are not-applicable, not legacy.

    Fresh review+e2e with a correctly skipped dep reproduce must read
    current; a real legacy reproduce stays legacy; stale/unknown on a
    required step still blocks green. Read-only: run.json bytes outside
    the step marks stay unchanged.
    """

    def _dep_state(self):
        state = self.make_run()
        state["dep"] = "slider"
        state["target"] = "5.2.1"
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=None)
        store.mark(state, "reproduce", "done", exit_code=0,
                   note="dependency maintenance: no upstream bug to "
                        "reproduce; target slider->5.2.1")
        for step in ("review", "e2e"):
            store.mark(state, step, "done", exit_code=0,
                       evidence=[f"{step}.x"], note=f"{step} passed")
            runner._bind_verification(state, work, step)
        return store.load(state["id"])

    def test_skipped_reproduce_with_fresh_checks_is_current(self):
        state = self._dep_state()
        proof = store.verification_status(state)
        self.assertEqual(proof["reproduce"], "skipped")
        self.assertEqual(proof["review"], "fresh")
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(body["source"]["per_step"]["reproduce"], "skipped")
        self.assertEqual(body["source"]["proof"], "current")
        repro = next(b for b in body["verification"]
                     if b["step"] == "reproduce")
        self.assertEqual(repro["proof"], "skipped")

    def test_real_legacy_reproduce_stays_legacy(self):
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=None)
        for step in ("reproduce", "review", "e2e"):
            store.mark(state, step, "done", exit_code=0, note="legacy")
            state["verifiedCommits"][step] = state["workCommit"]
        store.save(state)
        proof = store.verification_status(store.load(state["id"]))
        self.assertEqual(proof["reproduce"], "legacy")
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(body["source"]["proof"], "legacy")

    def test_stale_required_proof_still_blocks_green(self):
        state = self._dep_state()
        work = store.run_dir(state["id"]) / "work"
        (work / "a.txt").write_text("edit\n", encoding="utf-8")
        runner._refresh_source(state, work)
        state = store.load(state["id"])
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(body["source"]["proof"], "stale")
        self.assertFalse(body["promotion"]["ready"])


class ForwardCorrectionReadTests(HomeMixin):
    """Same-run forward correction must read the ACTUAL live source.

    Real git fixture mirroring the Babel live-head gap: a DRAFT commit is
    recorded as the run source with legacy review/fix diffstat artifacts,
    then a forward commit removes the DRAFT file leaving an EMPTY net
    diff vs base while the run metadata stays at the old commit. The work
    API must show the actual HEAD/fingerprint with empty current files
    and Changes, keep the old identifiers as explicitly recorded history,
    never report green, and never mutate run.json on GET.
    """

    def _draft_then_forward_empty(self):
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=None)
        draft = work / "DRAFT-note.md"
        draft.write_text("draft line 1\ndraft line 2\n", encoding="utf-8")
        git("add", "-A", cwd=work)
        git("commit", "-qm", "old fix with draft note", cwd=work)
        runner._refresh_source(state, work)
        state = store.load(state["id"])
        for step in ("review", "e2e"):
            store.mark(state, step, "done", exit_code=0,
                       evidence=[f"{step}.x"], note=f"{step} passed")
            runner._bind_verification(state, work, step)
        # The recorded engine source is what the binds just stored: the
        # DRAFT commit and its fingerprint (captured after binding).
        state = store.load(state["id"])
        old_head = state["workCommit"]
        old_fp = state["workTreeFingerprint"]
        rdir = store.run_dir(state["id"])
        for name in ("review.diffstat.txt", "fix.diffstat.txt"):
            (rdir / name).write_text(
                " DRAFT-note.md | 2 ++\n 1 file changed, 2 insertions(+)\n",
                encoding="utf-8")
        # Forward correction on the owned worktree: remove the draft file
        # and commit forward. The engine metadata is NOT refreshed here,
        # so it still names the old commit (the recorded history).
        draft.unlink()
        git("add", "-A", cwd=work)
        git("commit", "-qm", "remove draft note, no net change", cwd=work)
        live_head = git("rev-parse", "HEAD", cwd=work).strip()
        self.assertNotEqual(live_head, old_head)
        # Sanity: the net diff vs base really is empty now.
        self.assertEqual(
            git("diff", "--numstat",
                f"{state['baseCommit']}..HEAD", "--", cwd=work).strip(), "")
        live_fp = runner.source_snapshot(work)["fingerprint"]
        self.assertNotEqual(live_fp, old_fp)
        return store.load(state["id"]), work, old_head, old_fp, live_head, live_fp

    def test_forward_removal_reads_live_head_and_empty_changes(self):
        state, work, old_head, old_fp, live_head, live_fp = (
            self._draft_then_forward_empty())
        run_file = store.run_file(state["id"])
        before = run_file.read_bytes()
        body = serve.api_work({"run": state["id"]})
        # The read itself mutates nothing and preserves history files.
        self.assertEqual(run_file.read_bytes(), before)
        rdir = store.run_dir(state["id"])
        self.assertIn(
            "DRAFT-note.md",
            (rdir / "review.diffstat.txt").read_text(encoding="utf-8"))
        # Current source fields are the actual readable worktree state.
        src = body["source"]
        self.assertEqual(src["head_commit"], live_head)
        self.assertEqual(src["fingerprint"], live_fp)
        self.assertEqual(src["kind"], "git")
        # ... while the old engine identifiers stay explicitly recorded.
        self.assertEqual(src["recorded_head_commit"], old_head)
        self.assertEqual(src["recorded_fingerprint"], old_fp)
        self.assertEqual(src["head_match"], False)
        self.assertTrue(src["worktree_available"])
        # True live empty is authoritative: no stale 2-line addition.
        self.assertEqual(body["changed_files"], [])
        self.assertEqual(body["changed_files_total"], "")
        self.assertIn("live", body["changed_files_source"])
        sc = body["sourceChanges"]
        self.assertEqual(sc["availability"], "live")
        self.assertEqual(sc["fileDiffs"], [])
        self.assertEqual(sc["liveHead"], live_head)
        self.assertEqual(sc["headMatch"], False)
        # Old proof bound to removed bytes is stale — never green.
        self.assertEqual(src["proof"], "stale")
        self.assertTrue(all(b["proof"] == "stale"
                            for b in body["verification"]
                            if b["step"] in ("review", "e2e")))
        self.assertFalse(body["promotion"]["ready"])
        self.assertFalse(body["outcome"]["promotion_ready"])
        self.assertEqual(graph._fresh_verifications(
            store.load(state["id"]),
            live_fp, live_head), [])

    def test_moved_head_with_identical_bytes_is_stale_not_fresh(self):
        """Strict commit+content contract: an empty forward commit moves
        HEAD with identical bytes, so old proof is stale even though the
        fingerprint still matches. The stored commit can never keep false
        current proof alive after the HEAD moved."""
        state = self.make_run()
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=None)
        self.bind_done(state, work, "reproduce", "review", "e2e")
        state = store.load(state["id"])
        old_head = state["workCommit"]
        old_fp = state["workTreeFingerprint"]
        git("commit", "-qm", "empty forward marker",
            "--allow-empty", cwd=work)
        live_head = git("rev-parse", "HEAD", cwd=work).strip()
        self.assertNotEqual(live_head, old_head)
        # Same bytes: the fingerprint genuinely still matches the proof.
        self.assertEqual(runner.source_snapshot(work)["fingerprint"], old_fp)
        run_file = store.run_file(state["id"])
        before = run_file.read_bytes()
        body = serve.api_work({"run": state["id"]})
        self.assertEqual(run_file.read_bytes(), before)
        src = body["source"]
        self.assertEqual(src["head_commit"], live_head)
        self.assertEqual(src["fingerprint"], old_fp)
        self.assertEqual(src["recorded_head_commit"], old_head)
        self.assertEqual(src["head_match"], False)
        self.assertEqual(src["proof"], "stale")
        self.assertTrue(all(b["proof"] == "stale"
                            for b in body["verification"]))
        self.assertFalse(body["promotion"]["ready"])
        self.assertFalse(body["outcome"]["promotion_ready"])


if __name__ == "__main__":
    unittest.main()
