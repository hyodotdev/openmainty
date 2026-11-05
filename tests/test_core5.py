"""Core revision5 gates: review scope, issue honesty, lock-refresh, fresh
bases, triage disposition, headless flags, reference cache, UI contract.

Isolated fixtures only (temp OPENMAINTY_HOME, fake registry/executor,
mocked gh). No real network, no public writes.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import cli, deps, muse, pcconfig, planner, repos, runner, sched, store  # noqa: E402
try:
    from tests._home import (use_temp_home, offline_discussions,  # noqa: E402
                             offline_prs, offline_muse_version)
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import (use_temp_home, offline_discussions,  # noqa: E402
                       offline_prs, offline_muse_version)

PROFILE = {
    "name": "c5", "github": "example/c5", "baseBranch": "main",
    "knowledge": [], "checks": [], "e2e": [],
    "release": {}, "announce": {},
}


def git(*args, cwd):
    proc = subprocess.run(["git", "-C", str(cwd)] + list(args),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
    return proc.stdout


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


def make_git_repo(root: Path, name="co") -> Path:
    co = root / name
    co.mkdir()
    git("init", "-b", "main", cwd=co)
    git("config", "user.email", "test@localhost", cwd=co)
    git("config", "user.name", "test", cwd=co)
    (co / "code.txt").write_text("v1\n", encoding="utf-8")
    git("add", "-A", cwd=co)
    git("commit", "-qm", "init", cwd=co)
    return co


class Rec(runner.FakeExecutor):
    """Fake executor that records worker prompts by step."""

    def __init__(self, script=None):
        super().__init__(script)
        self.prompts = {}

    def muse_step(self, step, prompt, workdir):
        self.prompts[step] = prompt
        return super().muse_step(step, prompt, workdir)


class ReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(self.co)
        pcconfig.save(pc)

    def test_reviewer_gets_committed_diff_not_empty_git_diff(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("c5", "1", "fix it", base, {"worker": "test"})
        state["branch"] = "codex/next/fix-it"
        state["base_branch"] = "main"
        state["lane"] = "next"
        store.claim("c5", "1", state["id"])
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=state["branch"])
        (work / "code.txt").write_text("v2 fixed\n", encoding="utf-8")
        done = runner.maybe_commit_worktree(state, work, "fix")
        self.assertTrue(done.get("committed"), done)
        # The reported bug: after the local fix commit, bare git diff is empty.
        code, out, _ = runner.run_cmd(["git", "-C", str(work), "diff", "--stat"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")
        ex = Rec({"review": [(0, "VALID: none\nno issues found")]})
        profile = repos.load("c5")
        self.assertTrue(runner.run_review(state, profile, ex, work, "body"))
        prompt = ex.prompts["review"]
        self.assertIn(base[:12], prompt)
        self.assertIn("v1", prompt)
        self.assertIn("v2 fixed", prompt)
        self.assertIn("code.txt", prompt)
        rdir = store.run_dir(state["id"])
        scope = json.loads((rdir / "review.scope.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["base_commit"], base)
        self.assertEqual(scope["base_ref"], "main")
        self.assertTrue(scope["bare_git_diff_empty"])
        self.assertIn("code.txt", scope.get("base_stat", ""))
        diff = (rdir / "review.diff.txt").read_text(encoding="utf-8")
        self.assertIn("v2 fixed", diff)
        entry = store.load(state["id"])["steps"]["review"]
        self.assertIn("review.scope.json", entry.get("evidence", []))
        self.assertIn("review.diff.txt", entry.get("evidence", []))


class IssueScanFailureTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"left-pad": "1.0.0"}}))
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def test_open_issues_raises_on_nonzero_exit(self):
        with mock.patch("openmainty.util.run_cmd", return_value=(1, "", "offline")):
            with self.assertRaises(RuntimeError):
                cli._open_issues("example/c5")

    def test_open_issues_raises_on_malformed_json(self):
        with mock.patch("openmainty.util.run_cmd", return_value=(0, "{{{", "")):
            with self.assertRaises(ValueError):
                cli._open_issues("example/c5")

    def test_scan_records_failure_and_keeps_last_issues(self):
        sched.configure("c5", enabled=True, interval_s=60)
        st = sched.load("c5")
        st["last_issues"] = [{"number": "9", "title": "kept",
                             "fetched_at": "t"}]
        sched.save(st)
        with mock.patch("openmainty.util.run_cmd", return_value=(1, "", "offline")):
            out = sched.scan("c5")
        self.assertFalse(out["scan_ok"])
        self.assertTrue(out["issues_error"])
        self.assertEqual(out["issues_open"], -1)
        st2 = sched.load("c5")
        self.assertTrue(st2["issues_error"])
        self.assertTrue(st2["sources"]["issues"].startswith("error:"))
        self.assertEqual(st2["last_issues"],
                         [{"number": "9", "title": "kept", "fetched_at": "t"}])


class MissingWorkdirRecreateTests(unittest.TestCase):
    # G5: resume on a run whose work directory vanished recreates it
    # from the checkout instead of failing — the failed step is then
    # retried against the fresh tree (runner skips only done steps).
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(self.co)
        pcconfig.save(pc)

    def _state(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("c5", "1", "fix it", base,
                             {"worker": "test"})
        state["base_branch"] = "main"
        return state

    def test_missing_work_dir_is_recreated_from_git_checkout(self):
        state = self._state()
        work = runner.ensure_branch_workdir(state, self.co, "main",
                                            branch=None)
        self.assertTrue((work / "code.txt").is_file())
        # The run's work folder vanishes (demo run-demomiss01 shape).
        git("worktree", "remove", "--force", str(work), cwd=self.co)
        self.assertFalse(work.exists())
        # Resume recreates it from the checkout; nothing to rebind.
        again = runner.ensure_branch_workdir(state, self.co, "main",
                                             branch=None)
        self.assertEqual(again, work)
        self.assertEqual((again / "code.txt").read_text(encoding="utf-8"),
                         "v1\n")

    def test_missing_work_dir_is_copied_when_checkout_has_no_git(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "app.txt").write_text("a\n", encoding="utf-8")
        state = self._state()
        work = runner.ensure_branch_workdir(state, plain, "main",
                                            branch=None)
        self.assertTrue((work / "app.txt").is_file())


class IssueContentTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(self.co)
        pcconfig.save(pc)

    def test_fetch_carries_provenance_and_body(self):
        payload = {"number": 7, "title": "T", "body": "steps", "state": "OPEN",
                   "url": "https://github.com/example/c5/issues/7"}
        with mock.patch("openmainty.util.run_cmd",
                        return_value=(0, json.dumps(payload), "")):
            got = cli.fetch_issue_content("example/c5", 7)
        self.assertEqual(got["body"], "steps")
        self.assertIn("gh issue view example/c5#7", got["provenance"])
        self.assertTrue(got["fetched_at"])
        self.assertIsNone(got["error"])

    def test_fetch_failure_is_explicit_not_empty(self):
        with mock.patch("openmainty.util.run_cmd", return_value=(1, "", "down")):
            got = cli.fetch_issue_content("example/c5", 7)
        self.assertTrue(got["error"])
        self.assertEqual(got["body"], "")

    def _enqueue_issue(self):
        st = sched.configure("c5", enabled=True, interval_s=60)
        item = {"kind": "issue-triage", "repo": "c5", "issue": "7",
                "title": "queued title", "status": "queued", "attempts": 0,
                "next_retry_at": None, "run_id": None,
                "why": "upstream open issue #7", "created_at": "t"}
        item["key"] = sched.dedup_key(item)
        st["queue"].append(item)
        sched.save(st)
        return item["key"]

    def test_closed_issue_cancels_without_execution(self):
        key = self._enqueue_issue()
        runs_before = len(store.list_runs(200))
        with mock.patch("openmainty.cli.fetch_issue_content",
                        return_value={"number": "7", "title": "T", "body": "B",
                                      "state": "CLOSED", "url": "U",
                                      "error": None, "fetched_at": "t",
                                      "provenance": "p"}):
            out = sched.run_due("c5", runner.FakeExecutor())
        self.assertFalse(out["ran"])
        st = sched.load("c5")
        item = next(i for i in st["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "cancelled")
        self.assertIn("CLOSED", item.get("note", ""))
        self.assertTrue(any(h["key"] == key for h in st["history"]))
        self.assertIsNone(st.get("current_job"))
        self.assertEqual(len(store.list_runs(200)), runs_before)

    def test_unreadable_issue_defers_without_fabrication(self):
        key = self._enqueue_issue()
        with mock.patch("openmainty.cli.fetch_issue_content",
                        return_value={"number": "7", "title": "", "body": "",
                                      "state": "unknown", "url": "",
                                      "error": "gh issue view failed (exit 1): down",
                                      "fetched_at": "t", "provenance": "p"}):
            out = sched.run_due("c5", runner.FakeExecutor())
        self.assertFalse(out.get("ok", True))
        item = next(i for i in sched.load("c5")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["attempts"], 1)


class LockRefreshTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "2.0.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def _write(self, declared, installed):
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"alpha": declared}}))
        (self.co / "yarn.lock").write_text(
            f'"alpha@npm:{declared}":\n'
            f'  version: {installed}\n'
            f'  resolution: "alpha@npm:{installed}"\n')

    def test_allowed_range_older_lock_is_actionable(self):
        self._write(">=1.0.0 <3.0.0", "1.0.0")
        rows = deps.check(repos.load("c5"), self.co)
        row = next(r for r in rows if r["name"] == "alpha")
        self.assertFalse(row["drift"])
        self.assertTrue(row.get("outdated"))
        self.assertEqual(row["update_kind"], "lock-refresh")
        self.assertEqual(row["latest"], "2.0.0")

    def test_installed_latest_is_noop(self):
        self._write(">=1.0.0 <3.0.0", "2.0.0")
        rows = deps.check(repos.load("c5"), self.co)
        row = next(r for r in rows if r["name"] == "alpha")
        self.assertFalse(row["drift"])
        self.assertFalse(row.get("outdated", False))

    def test_unmanaged_spec_never_queues(self):
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"alpha": "file:../alpha"}}))
        with mock.patch("openmainty.cli._open_issues", return_value=[]):
            sched.configure("c5", enabled=True, interval_s=60)
            out = sched.scan("c5")
        self.assertEqual(out["queued_added"], 0)

    def test_scan_queues_lock_refresh_proposal(self):
        self._write(">=1.0.0 <3.0.0", "1.0.0")
        with mock.patch("openmainty.cli._open_issues", return_value=[]):
            sched.configure("c5", enabled=True, interval_s=60)
            out = sched.scan("c5")
        self.assertEqual(out["queued_added"], 1)
        item = sched.load("c5")["queue"][0]
        self.assertEqual(item["update_kind"], "lock-refresh")
        self.assertEqual(item["target"], "2.0.0")


class FreshBaseTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main",
                        str(self.remote)],
                       check=True, capture_output=True, timeout=60)
        self.co = make_git_repo(self.root)
        git("remote", "add", "origin", str(self.remote), cwd=self.co)
        git("push", "-qu", "origin", "main", cwd=self.co)
        # A second clone advances origin/main; the fixture checkout stays.
        w2 = self.root / "w2"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(w2)],
                       check=True, capture_output=True, timeout=60)
        git("config", "user.email", "test@localhost", cwd=w2)
        git("config", "user.name", "test", cwd=w2)
        (w2 / "new.txt").write_text("upstream work\n", encoding="utf-8")
        git("add", "-A", cwd=w2)
        git("commit", "-qm", "upstream advance", cwd=w2)
        git("push", "-q", "origin", "main", cwd=w2)

    def test_fetch_succeeds_without_origin_next(self):
        fetched = sched._fetch_remote_refs(self.co)
        self.assertTrue(fetched["ok"], fetched)
        code, _, _ = runner.run_cmd(
            ["git", "-C", str(self.co), "rev-parse", "--verify", "origin/next"])
        self.assertNotEqual(code, 0)

    def test_new_main_run_prefers_fresh_origin_ref(self):
        head_before = git("rev-parse", "HEAD", cwd=self.co).strip()
        self.assertTrue(sched._fetch_remote_refs(self.co)["ok"])
        base = sched._resolve_main_base(self.co, "main")
        self.assertEqual(base, "origin/main")
        origin_head = git("rev-parse", "origin/main", cwd=self.co).strip()
        self.assertNotEqual(origin_head, head_before)
        # The user's checkout is untouched: still on local main at old HEAD.
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.co).strip(), head_before)
        self.assertEqual(
            git("branch", "--show-current", cwd=self.co).strip(), "main")


class NoKeyRecheckTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"alpha": "1.0.0", "beta": "1.0.0"}}))
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(self.co)
        pcconfig.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: {"alpha": "2.0.0",
                                                "beta": "3.0.0"}[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))
        with mock.patch("openmainty.cli._open_issues", return_value=[]):
            sched.configure("c5", enabled=True, interval_s=60)
            out = sched.scan("c5")
        self.assertEqual(out["queued_added"], 2)

    def test_stale_head_superseded_next_eligible_runs(self):
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"beta": "1.0.0"}}))
        with mock.patch("openmainty.cli._open_issues", return_value=[]):
            out = sched.run_due("c5", runner.FakeExecutor())
        self.assertTrue(out["ran"], out)
        self.assertEqual(out["item"]["dep"], "beta")
        by_dep = {i["dep"]: i for i in sched.load("c5")["queue"]}
        self.assertEqual(by_dep["alpha"]["status"], "cancelled")
        self.assertIn("no longer declared", by_dep["alpha"].get("note", ""))

    def test_all_stale_reports_without_running(self):
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {}}))
        runs_before = len(store.list_runs(200))
        with mock.patch("openmainty.cli._open_issues", return_value=[]):
            out = sched.run_due("c5", runner.FakeExecutor())
        self.assertFalse(out["ran"])
        self.assertIn("stale", out["reason"])
        self.assertEqual(len(store.list_runs(200)), runs_before)


class TriageDispositionTests(unittest.TestCase):
    def test_parse_kinds(self):
        self.assertEqual(
            planner.parse_triage_disposition("DISPOSITION: DUPLICATE of #5 (same crash)"),
            ("DUPLICATE", "of #5 (same crash)"))
        self.assertEqual(
            planner.parse_triage_disposition("disposition: info-needed: exact log lines")[0],
            "INFO-NEEDED")
        self.assertEqual(
            planner.parse_triage_disposition("DISPOSITION: ACTIONABLE: bump lock")[0],
            "ACTIONABLE")
        self.assertEqual(planner.parse_triage_disposition("plain prose")[0], "UNKNOWN")

    def _run_with_triage(self, triage_text, **fields):
        root = make_home(self)
        co = make_git_repo(root)
        git("branch", "next", cwd=co)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(co)
        pcconfig.save(pc)
        state = store.create("c5", "1", "t",
                             git("rev-parse", "HEAD", cwd=co).strip(),
                             {"worker": "test"})
        for key, val in fields.items():
            state[key] = val
        store.claim("c5", "1", state["id"])
        ex = Rec({"triage": [(0, triage_text)]})
        profile = repos.load("c5")
        ok = runner.execute(state, profile, ex, co, body="untrusted body")
        return ok, ex, store.load(state["id"])

    def test_duplicate_blocks_without_code_changes(self):
        ok, ex, state = self._run_with_triage(
            "DISPOSITION: DUPLICATE of #5 (same crash)\nDUPLICATE: yes #5")
        self.assertFalse(ok)
        self.assertTrue(state.get("waitReason", "").startswith("duplicate:"))
        self.assertNotIn("fix", ex.calls)
        self.assertNotIn("reproduce", ex.calls)

    def test_info_needed_asks_operator_with_same_identity(self):
        ok, ex, state = self._run_with_triage(
            "DISPOSITION: INFO-NEEDED: exact failing command and log")
        self.assertFalse(ok)
        self.assertTrue(state.get("waitReason", "").startswith("question:"))
        self.assertNotIn("fix", ex.calls)

    def test_dep_update_skips_bug_reproduce_keeps_intent(self):
        ok, ex, state = self._run_with_triage(
            "DISPOSITION: ACTIONABLE: refresh lock to 2.0.0",
            dep="alpha", target="2.0.0", scopes=["dependencies"],
            lane="next", branch="codex/next/alpha-2-0-0",
            base_branch="next", update_kind="lock-refresh")
        _ = ok
        entry = state["steps"]["reproduce"]
        self.assertEqual(entry["status"], "done")
        self.assertIn("no upstream bug", entry.get("note", ""))
        self.assertNotIn("reproduce", ex.calls)
        self.assertIn("fix", ex.calls)
        self.assertEqual(state.get("target"), "2.0.0")

    def test_run_context_reaches_worker_prompts(self):
        _ok, ex, _state = self._run_with_triage(
            "DISPOSITION: ACTIONABLE: refresh lock to 2.0.0",
            dep="alpha", target="2.0.0", scopes=["dependencies"],
            lane="next", branch="codex/next/alpha-2-0-0",
            base_branch="next", update_kind="lock-refresh")
        self.assertIn("base_ref: next", ex.prompts.get("fix", ""))
        self.assertIn("codex/next/alpha-2-0-0", ex.prompts.get("fix", ""))
        self.assertIn("untrusted body", ex.prompts.get("fix", ""))


class HeadlessFlagsTests(unittest.TestCase):
    def test_build_cmd_trusts_owned_worktree_without_prompts(self):
        cmd = muse.build_cmd("hi", workdir="/tmp/w")
        self.assertIn("--trust-workspace", cmd)
        self.assertIn("--approval-mode", cmd)
        self.assertIn("never", cmd)
        self.assertIn(muse.DEFAULT_MODEL, cmd)

    def test_worker_cache_env_is_per_run_and_machine_independent(self):
        # Home caches are not writable under the worker filesystem sandbox;
        # the launcher must redirect the JS toolchain at per-run dirs, never
        # machine-local overrides (run-34e91bec follow-up).
        import tempfile
        env = muse.worker_cache_env("/tmp/run-work")
        self.assertEqual(env["YARN_ENABLE_GLOBAL_CACHE"], "false")
        for key in ("COREPACK_HOME", "YARN_CACHE_FOLDER",
                    "YARN_GLOBAL_FOLDER", "npm_config_cache"):
            self.assertTrue(env[key].startswith("/tmp/run-work" + "/"),
                            key)
            self.assertIn(muse.CACHE_DIR_NAME, env[key])
        fallback = muse.worker_cache_env(None)
        self.assertTrue(fallback["YARN_CACHE_FOLDER"].startswith(
            tempfile.gettempdir()))

    def test_worker_cache_env_nests_under_node_modules(self):
        # run-71c1de11 follow-up: downloaded toolchain JS (corepack yarn
        # shims) landed directly under .openmainty-cache/ and ESLint's
        # `**/*.{js,ts,tsx}` traversal linted it (4 errors). The per-run
        # dirs must nest under a node_modules segment — ignored by
        # default JS tool traversal at any depth — while keeping the
        # reserved prefix (fingerprint/commit-noise isolation) and never
        # the target repo's real node_modules (yarn would delete its own
        # active package manager there).
        import tempfile
        env = muse.worker_cache_env("/tmp/run-work")
        self.assertEqual(env["YARN_ENABLE_GLOBAL_CACHE"], "false")
        for key in ("COREPACK_HOME", "YARN_CACHE_FOLDER",
                    "YARN_GLOBAL_FOLDER", "npm_config_cache"):
            parts = env[key].replace("\\", "/").split("/")
            self.assertIn(muse.CACHE_DIR_NAME, parts, key)
            self.assertIn("node_modules", parts, key)
            cache_idx = parts.index(muse.CACHE_DIR_NAME)
            self.assertEqual(parts[cache_idx + 1], "node_modules", key)
            self.assertTrue(env[key].startswith("/tmp/run-work" + "/"), key)
            self.assertNotIn(os.path.expanduser("~"), env[key], key)
        fallback = muse.worker_cache_env(None)
        self.assertTrue(fallback["YARN_CACHE_FOLDER"].startswith(
            tempfile.gettempdir()))
        self.assertIn("node_modules", fallback["COREPACK_HOME"].split(os.sep))

    # NOTE (revision40): an earlier lint-traversal unit test defined its
    # own Python selected() to mimic ESLint and only proved its own
    # invention. It was removed; actual ESLint traversal is covered by
    # Root's real Node/ESLint integration receipt
    # (root-revision39-real-eslint-traversal.json), not by a Python
    # mirror. The nested-layout guard above and the noise-isolation
    # guard below stay as the maintained source-side regression tests.
    def test_nested_cache_js_fixture_stays_source_noise(self):
        # A realistic downloaded-shim fixture (eslint-disable header, the
        # shape that produced the 4 lint errors) under the nested cache
        # must not change the content fingerprint nor list as untracked
        # source, and must not enter a local work commit.
        root = make_home(self)
        work = make_git_repo(root)
        before = runner.source_snapshot(work)
        env = muse.worker_cache_env(str(work))
        shim = Path(env["COREPACK_HOME"]) / "v1" / "yarn" / "4.18.0" / "yarn.js"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(
            "/* eslint-disable */\nmodule.exports = function broken( {\n",
            encoding="utf-8")
        after = runner.source_snapshot(work)
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        prefix = muse.CACHE_DIR_NAME + "/"
        self.assertFalse(
            [p for p in after.get("untracked", []) if p.startswith(prefix)])

    def test_build_cmd_enables_worker_network_without_disabling_sandbox(self):
        # run-34e91bec timed out when worker node DNS failed under the CLI
        # default proxy-only network mode while the host was healthy.
        # Workers need direct registry/gh egress; the filesystem sandbox
        # must stay ON.
        for cmd in (muse.build_cmd("hi"), muse.build_cmd("hi", workdir="/tmp/w")):
            self.assertIn("--sandbox-network", cmd)
            self.assertEqual(
                cmd[cmd.index("--sandbox-network") + 1], "enabled")
            self.assertNotIn("--disable-sandbox", cmd)
            self.assertNotIn("--yolo", cmd)


class RefsCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def test_lookup_caches_and_serves_stale_on_failure(self):
        from openmainty import refs
        payload = {"number": 774, "title": "Real fix title", "state": "closed",
                   "html_url": "https://github.com/example/c5/issues/774"}
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(0, json.dumps(payload), "")):
            first = refs.lookup("example/c5", 774)
        self.assertEqual(first["kind"], "issue")
        self.assertEqual(first["title"], "Real fix title")
        self.assertEqual(first["source"], "gh-api")
        # Age the cache past its TTL: a failed refresh must serve the
        # stored metadata explicitly stale, never invent or drop it.
        from openmainty import refs as _refs_mod
        cache = _refs_mod.cache_file("example/c5", 774)
        entry = json.loads(cache.read_text(encoding="utf-8"))
        entry["fetched_at"] = "2000-01-01T00:00:00+00:00"
        cache.write_text(json.dumps(entry), encoding="utf-8")
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(1, "", "offline")):
            second = refs.lookup("example/c5", 774)
        self.assertEqual(second["title"], "Real fix title")
        self.assertEqual(second["source"], "cache")
        self.assertFalse(second["fresh"])
        self.assertIn("offline", second.get("fetch_error", ""))

    def test_pr_kind_detected(self):
        from openmainty import refs
        payload = {"number": 12, "title": "PR title", "state": "open",
                   "html_url": "https://github.com/example/c5/pull/12",
                   "pull_request": {"url": "x"}}
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(0, json.dumps(payload), "")):
            got = refs.lookup("example/c5", 12)
        self.assertEqual(got["kind"], "pr")

    def test_unknown_when_never_stored_and_fetch_fails(self):
        from openmainty import refs
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(1, "", "offline")):
            got = refs.lookup("example/c5", 99999)
        self.assertEqual(got["kind"], "unknown")
        self.assertEqual(got["title"], "")
        self.assertTrue(got["error"])


class ServeContractTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        repos.save(dict(PROFILE))

    def test_work_carries_stable_source_changes(self):
        from openmainty import serve
        state = store.create("c5", "1", "t", "unknown", {"worker": "test"})
        out = serve.api_work({"run": state["id"]})
        for key in ("changed_files", "changed_files_total",
                    "changed_files_source", "sourceChanges"):
            self.assertIn(key, out)
        sc = out["sourceChanges"]
        for key in ("baseRef", "baseCommit", "head", "fileDiffs",
                    "untracked", "dirtyStatus", "hasChanges", "planned",
                    "localOnly", "diffCapped"):
            self.assertIn(key, sc)
        self.assertTrue(sc["localOnly"])
        self.assertTrue(sc["planned"])
        self.assertNotIn("commit_url", json.dumps(sc))

    def test_changes_endpoint_planned_state(self):
        from openmainty import serve
        state = store.create("c5", "2", "t", "unknown", {"worker": "test"})
        out = serve.api_source_changes({"run": state["id"]})
        self.assertIn("sourceChanges", out)
        self.assertFalse(out["sourceChanges"]["hasChanges"])

    def test_refs_endpoint_shape(self):
        from openmainty import serve
        with mock.patch("openmainty.refs.lookup_many",
                        return_value=[{"number": "774", "kind": "pr",
                                       "title": "T", "state": "merged",
                                       "url": "U", "fetched_at": "t",
                                       "source": "gh-api", "fresh": True,
                                       "error": None}]):
            out = serve.api_refs({"repo": "c5", "numbers": "774"})
        self.assertEqual(out["repo"], "c5")
        ref = out["references"][0]
        for key in ("number", "kind", "title", "state", "url",
                    "fetched_at", "source", "fresh", "error"):
            self.assertIn(key, ref)


class DuplicateResumeTests(unittest.TestCase):
    """Durable duplicate/waiting lifecycle: INITIAL result, exact SAME-run
    resume, CLI resume, scheduler/daemon settle, operator-answer resume.

    Regression for the independently reproduced barrier break where a
    SAME-state resume ran reproduce/analyze/fix/review after a
    `duplicate: of #5` triage (see duplicate-resume-reproduction.txt).
    """

    def _repo(self):
        root = make_home(self)
        co = make_git_repo(root)
        git("branch", "next", cwd=co)
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(co)
        pcconfig.save(pc)
        return root, co

    def _run_until(self, co, triage_text, **fields):
        state = store.create("c5", "1", "t",
                             git("rev-parse", "HEAD", cwd=co).strip(),
                             {"worker": "test"})
        for key, val in fields.items():
            state[key] = val
        store.claim("c5", "1", state["id"])
        ex = Rec({"triage": [(0, triage_text)]})
        profile = repos.load("c5")
        ok = runner.execute(state, profile, ex, co, body="untrusted body")
        return ok, ex, store.load(state["id"])

    def test_initial_duplicate_persists_structured_disposition(self):
        ok, ex, state = self._run_until(
            self._repo()[1],
            "DISPOSITION: DUPLICATE of #5 (same crash)\nDUPLICATE: yes #5")
        self.assertFalse(ok)
        self.assertEqual(ex.calls, ["triage"])
        disp = state.get("disposition")
        self.assertIsNotNone(disp)
        self.assertEqual(disp.get("kind"), "DUPLICATE")
        self.assertTrue(state.get("waitReason", "").startswith("duplicate:"))
        steps = state["steps"]
        self.assertEqual(steps["triage"]["status"], "done")
        for name in ("reproduce", "analyze", "fix", "review", "e2e",
                     "release", "announce"):
            self.assertNotEqual(steps[name]["status"], "done", name)

    def test_exact_same_run_resume_makes_no_worker_calls(self):
        _root, co = self._repo()
        _ok, _ex, state = self._run_until(
            co, "DISPOSITION: DUPLICATE of #5 (same crash)\nDUPLICATE: yes")
        run_id = state["id"]
        fresh = Rec()
        ok2 = runner.execute(store.load(run_id), repos.load("c5"), fresh,
                             co, body="untrusted body")
        self.assertFalse(ok2)
        self.assertEqual(fresh.calls, [])
        self.assertEqual(fresh.prompts, {})
        again = store.load(run_id)
        self.assertTrue(again.get("waitReason", "").startswith("duplicate:"))
        self.assertEqual(again.get("disposition", {}).get("kind"), "DUPLICATE")

    def test_cli_resume_evaluates_gate_before_any_pop(self):
        import argparse
        _root, co = self._repo()
        _ok, _ex, state = self._run_until(
            co, "DISPOSITION: DUPLICATE of #5 (same crash)\nDUPLICATE: yes")
        with mock.patch("openmainty.runner.execute",
                        side_effect=AssertionError("must not execute")):
            rc = cli.cmd_resume(argparse.Namespace(
                run=state["id"], executor="fake", model=None, only=None))
        self.assertEqual(rc, 1)
        again = store.load(state["id"])
        self.assertTrue(again.get("waitReason", "").startswith("duplicate:"))

    def test_unknown_verdict_blocks_without_code(self):
        _root, co = self._repo()
        ok, ex, state = self._run_until(
            co, "DISPOSITION: MAYBE LATER (unclear)\nno verdict here")
        self.assertFalse(ok)
        self.assertEqual(ex.calls, ["triage"])
        self.assertEqual(state.get("disposition", {}).get("kind"), "UNKNOWN")
        self.assertTrue(state.get("waitReason", "").startswith("question:"))
        self.assertNotIn("fix", ex.calls)
        # An unanswered UNKNOWN resume also makes no worker calls.
        fresh = Rec()
        self.assertFalse(runner.execute(store.load(state["id"]),
                                        repos.load("c5"), fresh, co, body="b"))
        self.assertEqual(fresh.calls, [])

    def test_info_needed_survives_resume_without_answer(self):
        _root, co = self._repo()
        ok, ex, state = self._run_until(
            co, "DISPOSITION: INFO-NEEDED: exact failing command and log")
        self.assertFalse(ok)
        self.assertEqual(state.get("disposition", {}).get("kind"), "INFO-NEEDED")
        fresh = Rec()
        self.assertFalse(runner.execute(store.load(state["id"]),
                                        repos.load("c5"), fresh, co, body="b"))
        self.assertEqual(fresh.calls, [])
        self.assertTrue(store.load(state["id"]).get(
            "waitReason", "").startswith("question:"))

    def test_operator_answer_resumes_same_identity(self):
        _root, co = self._repo()
        state = store.create("c5", "1", "t",
                             git("rev-parse", "HEAD", cwd=co).strip(),
                             {"worker": "test"})
        store.claim("c5", "1", state["id"])
        ex = Rec({"triage": [(0, "DISPOSITION: INFO-NEEDED: exact log lines"),
                             (0, "DISPOSITION: ACTIONABLE: bump lock")]})
        profile = repos.load("c5")
        self.assertFalse(runner.execute(state, profile, ex, co, body="b"))
        waiting = store.load(state["id"])
        self.assertTrue(waiting.get("waitReason", "").startswith("question:"))
        store.record_answer(state["id"], "the exact log lines are attached")
        answered = store.load(state["id"])
        self.assertIsNone(answered.get("waitReason"))
        self.assertIsNone(answered.get("disposition"))
        runner.execute(store.load(state["id"]), profile, ex, co, body="b")
        # Same run identity resumed into real steps — no fork, no new run.
        self.assertIn("fix", ex.calls)
        self.assertEqual(len(store.list_runs(200)), 1)
        self.assertEqual(store.load(state["id"])["id"], state["id"])


class SchedDispositionTests(unittest.TestCase):
    """Actual scheduler/daemon integration: duplicate and waiting runs
    settle without retries, forks, attempts, or backoff."""

    def _repo(self):
        root = make_home(self)
        co = make_git_repo(root)
        git("branch", "next", cwd=co)
        (co / "package.json").write_text(json.dumps(
            {"dependencies": {"alpha": "1.0.0"}}))
        repos.save(dict(PROFILE))
        pc = pcconfig.load()
        pc["checkouts"]["c5"] = str(co)
        pcconfig.save(pc)
        return root, co

    def _enqueue_dep(self):
        st = sched.configure("c5", enabled=True, interval_s=60)
        item = {"kind": "dep-update", "repo": "c5", "dep": "alpha",
                "target": "2.0.0", "installed": "1.0.0",
                "declared": "1.0.0", "lane": "next",
                "branch": "codex/next/alpha-2-0-0", "base_branch": "next",
                "update_kind": "minor", "scopes": ["dependencies"],
                "status": "queued", "attempts": 0, "next_retry_at": None,
                "run_id": None, "why": "test drift", "created_at": "t"}
        item["key"] = sched.dedup_key(item)
        st["queue"].append(item)
        sched.save(st)
        return item["key"]

    def test_sched_duplicate_is_no_code_without_retry_or_fork(self):
        _root, co = self._repo()
        key = self._enqueue_dep()
        ex = Rec({"triage": [(0, "DISPOSITION: DUPLICATE of #5 (same crash)")]})
        out = sched.run_due("c5", ex)
        self.assertTrue(out["ran"], out)
        self.assertEqual(out.get("outcome"), "no-code")
        item = next(i for i in sched.load("c5")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["attempts"], 0)
        self.assertIsNone(item["next_retry_at"])
        self.assertIn("duplicate", (item.get("note") or "").lower())
        run_id = item.get("run_id")
        self.assertTrue(run_id)
        run = store.load(run_id)
        self.assertEqual(run.get("disposition", {}).get("kind"), "DUPLICATE")
        self.assertNotIn("fix", ex.calls)
        # A daemon retry of the same item never forks or retries into code.
        ex2 = Rec()
        out2 = sched.run_due("c5", ex2, key=key)
        self.assertFalse(out2.get("ran", True) and "fix" in ex2.calls)
        self.assertEqual(ex2.calls, [])
        self.assertEqual(len(store.list_runs(200)), 1)
        item2 = next(i for i in sched.load("c5")["queue"] if i["key"] == key)
        self.assertEqual(item2["attempts"], 0)
        self.assertEqual(item2["run_id"], run_id)

    def test_sched_question_waits_without_attempts_or_backoff(self):
        _root, _co = self._repo()
        key = self._enqueue_dep()
        ex = Rec({"triage": [(0, "DISPOSITION: INFO-NEEDED: exact log lines")]})
        out = sched.run_due("c5", ex)
        self.assertFalse(out.get("ran", True))
        self.assertIn("operator", (out.get("reason") or "").lower())
        item = next(i for i in sched.load("c5")["queue"] if i["key"] == key)
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["attempts"], 0)
        self.assertIsNone(item["next_retry_at"])
        self.assertIn("operator", (item.get("blocker") or "").lower())
        run = store.load(item["run_id"])
        self.assertTrue(run.get("waitReason", "").startswith("question:"))
        self.assertEqual(run.get("disposition", {}).get("kind"), "INFO-NEEDED")
        self.assertNotIn("fix", ex.calls)

    def test_drive_fake_same_run_resume_makes_no_calls(self):
        _root, co = self._repo()
        state = store.create("c5", "1", "t",
                             git("rev-parse", "HEAD", cwd=co).strip(),
                             {"worker": "test"})
        store.claim("c5", "1", state["id"])
        ex = Rec({"triage": [(0, "DISPOSITION: DUPLICATE of #5 (same crash)")]})
        ok = runner.execute(state, repos.load("c5"), ex, co, body="b")
        self.assertFalse(ok)
        fresh = Rec()
        rc, rid = sched._drive_fake("c5", "1", "t", "b", fresh, {}, state["id"])
        self.assertEqual(rc, 1)
        self.assertEqual(rid, state["id"])
        self.assertEqual(fresh.calls, [])
        self.assertTrue(store.load(state["id"]).get(
            "waitReason", "").startswith("duplicate:"))


class SourceChangesDirtyTests(unittest.TestCase):
    """Inspectable dirty/staged/untracked diffs from a real git fixture."""

    def _repo(self):
        root = make_home(self)
        co = make_git_repo(root)
        repos.save(dict(PROFILE))
        return root, co

    def _state_with_work(self, co, branch="codex/next/t-1"):
        base = git("rev-parse", "HEAD", cwd=co).strip()
        state = store.create("c5", "1", "t", base, {"worker": "test"})
        state["branch"] = branch
        state["base_branch"] = "main"
        state["lane"] = "next"
        store.save(state)
        store.claim("c5", "1", state["id"])
        work = runner.ensure_branch_workdir(state, co, "main", branch=branch)
        return state, work

    def _git(self, work, *args):
        proc = __import__("subprocess").run(
            ["git", "-C", str(work)] + list(args),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-200:])
        return proc.stdout

    def test_committed_staged_unstaged_untracked_all_visible(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        base = state["baseCommit"]
        # Tracked files for the staged/unstaged sections.
        (work / "staged.txt").write_text("s1\n")
        (work / "unstaged.txt").write_text("u1\n")
        self._git(work, "add", "-A")
        self._git(work, "-c", "user.name=t", "-c", "user.email=t@l",
                  "commit", "-qm", "track extras")
        # Committed section.
        (work / "code.txt").write_text("v2 committed fix\n")
        self._git(work, "add", "code.txt")
        self._git(work, "-c", "user.name=t", "-c", "user.email=t@l",
                  "commit", "-qm", "committed fix")
        # Staged (uncommitted) section.
        (work / "staged.txt").write_text("s2 staged change\n")
        self._git(work, "add", "staged.txt")
        # Unstaged section.
        (work / "unstaged.txt").write_text("u2 unstaged change\n")
        # Relevant untracked source + ignored/generated junk.
        (work / "fresh.py").write_text("print('new untracked source')\n")
        (work / "node_modules").mkdir(exist_ok=True)
        (work / "node_modules" / "junk.js").write_text("x\n")
        sc = serve._source_changes(store.load(state["id"]))
        for key in ("baseRef", "baseCommit", "head", "fileDiffs",
                    "untracked", "dirtyStatus", "hasChanges", "planned",
                    "localOnly", "diffCapped"):
            self.assertIn(key, sc)
        self.assertEqual(sc["availability"], "live")
        self.assertFalse(sc["historical"])
        self.assertTrue(sc["localOnly"])
        self.assertTrue(sc["hasChanges"])
        self.assertFalse(sc["planned"])
        paths = {f["path"] for f in sc["fileDiffs"]}
        for name in ("code.txt", "staged.txt", "unstaged.txt", "fresh.py"):
            self.assertIn(name, paths, paths)
        for f in sc["fileDiffs"]:
            if f["path"] != "fresh.py":
                self.assertGreater(f["changes"], 0, f)
        by_path = {f["path"]: f for f in sc["fileDiffs"]}
        self.assertTrue(by_path["code.txt"].get("committed"))
        self.assertTrue(by_path["staged.txt"].get("dirty"))
        self.assertTrue(by_path["fresh.py"].get("untracked"))
        self.assertIn("v2 committed fix", sc["diff"])
        self.assertIn("s2 staged change", sc["diff"])
        self.assertIn("u2 unstaged change", sc["diff"])
        self.assertIn("new untracked source", sc["diff"])
        self.assertIn("committed(base..HEAD)", sc["patchSource"])
        self.assertIn("fresh.py", sc["untracked"])
        self.assertNotIn("node_modules/junk.js", sc["untracked"])
        self.assertGreaterEqual(sc["untrackedSkipped"], 1)
        contents = {c["path"]: c for c in sc["untrackedContents"]}
        self.assertIn("new untracked source", contents["fresh.py"]["content"])
        self.assertNotIn("commit_url", json.dumps(sc))
        self.assertNotIn("github.com/commit", json.dumps(sc))
        # Live HEAD moved past the recorded commit: reported distinctly.
        self.assertEqual(sc["liveHead"],
                         git("rev-parse", "HEAD", cwd=work).strip())
        self.assertEqual(sc["head"], base)
        self.assertFalse(sc["headMatch"])

    def test_legacy_base_equals_head_dirty_shows_real_patch(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        head = git("rev-parse", "HEAD", cwd=work).strip()
        state["baseCommit"] = head
        state["workCommit"] = head
        store.save(state)
        (work / "code.txt").write_text("legacy dirty edit\n")
        (work / "added.py").write_text("legacy untracked\n")
        sc = serve._source_changes(store.load(state["id"]))
        self.assertTrue(sc["hasChanges"])
        self.assertFalse(sc["planned"])
        self.assertTrue(sc["diff"].strip(), "dirty patch must not be empty")
        self.assertIn("legacy dirty edit", sc["diff"])
        self.assertIn("legacy untracked", sc["diff"])
        self.assertTrue(sc["headMatch"])

    def test_missing_worktree_uses_historical_patch_explicitly(self):
        from openmainty import serve
        _root, _co = self._repo()
        state = store.create("c5", "9", "t", "abc123", {"worker": "test"})
        state["steps"]["triage"]["status"] = "done"
        store.save(state)
        rdir = store.run_dir(state["id"])
        (rdir / "review.diff.txt").write_text(
            "diff --git a/old.py b/old.py\n+historical line\n",
            encoding="utf-8")
        (rdir / "fix.diffstat.txt").write_text(
            " old.py | 1 +\n 1 file changed, 1 insertion(+)\n",
            encoding="utf-8")
        sc = serve._source_changes(store.load(state["id"]))
        self.assertEqual(sc["availability"], "historical")
        self.assertTrue(sc["historical"])
        self.assertFalse(sc["planned"])
        self.assertIn("historical line", sc["diff"])
        self.assertIn("historical", sc["note"].lower())

    def test_missing_worktree_without_artifact_is_unavailable(self):
        from openmainty import serve
        _root, _co = self._repo()
        state = store.create("c5", "9", "t", "abc123", {"worker": "test"})
        state["steps"]["triage"]["status"] = "done"
        store.save(state)
        sc = serve._source_changes(store.load(state["id"]))
        self.assertEqual(sc["availability"], "unavailable")
        self.assertFalse(sc["planned"])
        self.assertFalse(sc["hasChanges"])
        self.assertTrue(sc["error"])
        self.assertIn("not a no-changes state", sc["note"])


class FilePatchTests(unittest.TestCase):
    """Per-file lazy patch reader over real git fixtures (serve.py).

    The aggregate `sourceChanges.diff` is capped; `filePatch` must serve
    the EXACT selected changed file in full — never another file, never
    an invented patch, never a broad file reader.
    """

    def _repo(self):
        root = make_home(self)
        co = make_git_repo(root)
        repos.save(dict(PROFILE))
        return root, co

    def _state_with_work(self, co, branch="codex/next/fp-1"):
        base = git("rev-parse", "HEAD", cwd=co).strip()
        state = store.create("c5", "1", "t", base, {"worker": "test"})
        state["branch"] = branch
        state["base_branch"] = "main"
        state["lane"] = "next"
        store.save(state)
        store.claim("c5", "1", state["id"])
        work = runner.ensure_branch_workdir(state, co, "main", branch=branch)
        return state, work

    def _git(self, work, *args):
        proc = __import__("subprocess").run(
            ["git", "-C", str(work)] + list(args),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-200:])
        return proc.stdout

    def _commit(self, work, *names):
        self._git(work, "add", "-A")
        self._git(work, "-c", "user.name=t", "-c", "user.email=t@l",
                  "commit", "-qm", "fp change")

    def test_run_only_shape_backward_compatible(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        (work / "code.txt").write_text("v2\n")
        self._commit(work)
        got = serve.api_source_changes({"run": state["id"]})
        self.assertEqual(got["run"], state["id"])
        self.assertIn("diff", got["sourceChanges"])
        self.assertIn("fileDiffs", got["sourceChanges"])
        self.assertIsNone(got["filePatch"])
        self.assertIn("code.txt",
                      {f["path"] for f in got["sourceChanges"]["fileDiffs"]})

    def test_aggregate_capped_but_selected_last_file_full(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        names = [f"mod{i:02d}.txt" for i in range(12)]
        for i, name in enumerate(names):
            marker = f"MARKER-{name}-UNIQUE-{i}\n"
            (work / name).write_text(marker + ("x" * 9000) + "\n")
        self._commit(work, *names)
        sc = serve._source_changes(store.load(state["id"]))
        self.assertEqual(sc["availability"], "live")
        self.assertTrue(sc["diffCapped"],
                        "aggregate patch must actually be capped here")
        last = names[-1]
        fp = serve.api_source_changes(
            {"run": state["id"], "path": last})["filePatch"]
        self.assertEqual(fp["path"], last)
        self.assertFalse(fp["historical"])
        self.assertEqual(fp["availability"], "live")
        self.assertFalse(fp["capped"])
        self.assertIn(f"MARKER-{last}-UNIQUE-11", fp["diff"])
        self.assertIn(f"MARKER-{last}-UNIQUE-11", fp["diffCommitted"])
        # Never another file's patch: sibling markers stay out.
        self.assertNotIn("MARKER-mod00.txt-UNIQUE-0", fp["diff"])
        self.assertEqual(fp["untrackedContents"], [])
        self.assertEqual(fp["error"], "")

    def test_untracked_exact_file_contents(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        (work / "fresh_test.py").write_text(
            "UNTRACKED-MARKER-77\nprint('hello')\n")
        (work / "node_modules").mkdir(exist_ok=True)
        (work / "node_modules" / "junk.js").write_text("x\n")
        fp = serve.api_source_changes(
            {"run": state["id"], "path": "fresh_test.py"})["filePatch"]
        self.assertEqual(fp["path"], "fresh_test.py")
        self.assertEqual(fp["availability"], "live")
        self.assertEqual(fp["diffCommitted"], "")
        self.assertEqual(fp["diffStaged"], "")
        self.assertEqual(fp["diffUnstaged"], "")
        self.assertEqual(len(fp["untrackedContents"]), 1)
        entry = fp["untrackedContents"][0]
        self.assertEqual(entry["path"], "fresh_test.py")
        self.assertIn("UNTRACKED-MARKER-77", entry["content"])
        self.assertFalse(entry["binary"])
        self.assertEqual(fp["error"], "")

    def test_staged_and_unstaged_same_file(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        (work / "both.txt").write_text("base\n")
        self._commit(work)
        (work / "both.txt").write_text("base\nSTAGED-MARKER-1\n")
        self._git(work, "add", "both.txt")
        (work / "both.txt").write_text(
            "base\nSTAGED-MARKER-1\nUNSTAGED-MARKER-2\n")
        fp = serve.api_source_changes(
            {"run": state["id"], "path": "both.txt"})["filePatch"]
        self.assertIn("+STAGED-MARKER-1", fp["diffStaged"])
        self.assertNotIn("UNSTAGED-MARKER-2", fp["diffStaged"])
        self.assertIn("+UNSTAGED-MARKER-2", fp["diffUnstaged"])
        # The staged line may appear as context, but never as an
        # unstaged addition.
        self.assertNotIn("+STAGED-MARKER-1", fp["diffUnstaged"])
        self.assertIn("STAGED-MARKER-1", fp["diff"])
        self.assertIn("UNSTAGED-MARKER-2", fp["diff"])
        self.assertEqual(fp["untrackedContents"], [])

    def test_unknown_traversal_escape_refused(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        (work / "code.txt").write_text("v2\n")
        self._commit(work)
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_source_changes({"run": state["id"], "path": "nope.txt"})
        self.assertEqual(ctx.exception.status, 404)
        for bad, status in (("../evil.txt", 400), ("a/../../b", 400),
                            ("/abs/path.txt", 400)):
            with self.assertRaises(serve.ApiError) as ctx:
                serve.api_source_changes({"run": state["id"], "path": bad})
            self.assertEqual(ctx.exception.status, status, bad)
        # Symlink escape: listed as untracked but points outside the
        # worktree — refused, never read through.
        import tempfile as _tf
        _outside = _tf.NamedTemporaryFile(
            delete=False, suffix=".txt", mode="w")
        _outside.write("OUTSIDE-SECRET\n")
        _outside.close()
        self.addCleanup(lambda: os.unlink(_outside.name))
        import pathlib as _pl
        link = _pl.Path(work) / "evil-link.txt"
        try:
            link.symlink_to(_outside.name)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_source_changes(
                {"run": state["id"], "path": "evil-link.txt"})
        self.assertEqual(ctx.exception.status, 400)
        self.assertNotIn("OUTSIDE-SECRET",
                         json.dumps(serve.api_source_changes(
                             {"run": state["id"]})))

    def test_deep_path_with_spaces_preserved(self):
        from openmainty import serve
        _root, co = self._repo()
        state, work = self._state_with_work(co)
        rel = "deep dir/nested path/file one.txt"
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(f"SPACED-LINE-{i}\n" for i in range(7))
        p.write_text(lines)
        self._commit(work)
        sc = serve._source_changes(store.load(state["id"]))
        entry = {f["path"]: f for f in sc["fileDiffs"]}.get(rel)
        self.assertIsNotNone(entry, "full relative path must survive")
        self.assertEqual(entry["additions"], 7)
        self.assertEqual(entry["changes"], 7)
        fp = serve.api_source_changes(
            {"run": state["id"], "path": rel})["filePatch"]
        self.assertEqual(fp["path"], rel)
        self.assertIn("SPACED-LINE-0", fp["diffCommitted"])
        self.assertIn("SPACED-LINE-6", fp["diffCommitted"])

    def test_historical_exact_path_and_missing_section(self):
        from openmainty import serve
        _root, _co = self._repo()
        state = store.create("c5", "9", "t", "abc123", {"worker": "test"})
        state["steps"]["triage"]["status"] = "done"
        store.save(state)
        rdir = store.run_dir(state["id"])
        (rdir / "review.diff.txt").write_text(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n-old\n+HIST-A-MARKER\n"
            "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
            "@@ -1 +1 @@\n-old\n+HIST-B-MARKER\n",
            encoding="utf-8")
        (rdir / "fix.diffstat.txt").write_text(
            " a.py | 2 +-\n b.py | 2 +-\n ghost.py | 3 +++\n"
            " 3 files changed, 4 insertions(+), 2 deletions(-)\n",
            encoding="utf-8")
        sc = serve._source_changes(store.load(state["id"]))
        self.assertEqual(sc["availability"], "historical")
        fp = serve.api_source_changes(
            {"run": state["id"], "path": "a.py"})["filePatch"]
        self.assertEqual(fp["path"], "a.py")
        self.assertEqual(fp["availability"], "historical")
        self.assertTrue(fp["historical"])
        self.assertIn("HIST-A-MARKER", fp["diff"])
        self.assertNotIn("HIST-B-MARKER", fp["diff"])
        self.assertEqual(fp["error"], "")
        # Listed in the diffstat but with no patch section: explicitly
        # unavailable — never planned, never another file.
        ghost = serve.api_source_changes(
            {"run": state["id"], "path": "ghost.py"})["filePatch"]
        self.assertEqual(ghost["availability"], "unavailable")
        self.assertFalse(ghost["historical"])
        self.assertEqual(ghost["diff"], "")
        self.assertTrue(ghost["error"])
        self.assertNotIn("HIST-A-MARKER", ghost["error"])
        # Never recorded anywhere: 404, not a fallback patch.
        with self.assertRaises(serve.ApiError) as ctx:
            serve.api_source_changes({"run": state["id"], "path": "zzz.py"})
        self.assertEqual(ctx.exception.status, 404)


class OutcomeTriageTests(unittest.TestCase):
    """outcome.triage authority: disposition > DISPOSITION line > strict
    legacy positive > unknown. Header words alone are never a verdict."""

    RUN381 = ("## DUPLICATE\n\nNo known duplicate identified from local code "
              "and issue text alone; treat as standalone feature request.\n\n"
              "## INFO-NEEDED\n\nNone. Operator decisions already settle "
              "prior ambiguities.\n\n"
              "## ACTIONABLE\n\nYes. Implement opt-in mix mode: concurrent "
              "backing-track playback while recording voice.\n")

    def _run_with_log(self, text=None, disposition=None):
        root = make_home(self)
        co = make_git_repo(root)
        repos.save(dict(PROFILE))
        state = store.create("c5", "1", "t", "base0", {"worker": "test"})
        if disposition is not None:
            state["disposition"] = disposition
            store.save(state)
        if text is not None:
            (store.run_dir(state["id"]) / "triage.log.txt").write_text(
                text, encoding="utf-8")
        return state

    def test_disposition_is_authority_first(self):
        from openmainty import serve
        state = self._run_with_log(
            self.RUN381,
            {"kind": "DUPLICATE", "detail": "duplicate of #5",
             "ref": "#5", "at": "2026-10-18T00:00:00+00:00"})
        out = serve._run_outcome(store.load(state["id"]))
        self.assertEqual(out["triage"], "DUPLICATE")
        self.assertEqual(out["triageSource"], "disposition")
        state2 = self._run_with_log(
            "## DUPLICATE\n\nYes, duplicate of #12 (same crash).\n\n"
            "## INFO-NEEDED\n\nNone.\n\n## ACTIONABLE\n\nNo.\n",
            {"kind": "ACTIONABLE", "detail": "bump lock",
             "ref": None, "at": "2026-10-18T00:00:00+00:00"})
        out2 = serve._run_outcome(store.load(state2["id"]))
        self.assertEqual(out2["triage"], "ACTIONABLE")
        self.assertEqual(out2["triageSource"], "disposition")

    def test_legacy_run381_example_is_actionable_not_duplicate(self):
        from openmainty import serve
        state = self._run_with_log(self.RUN381)
        out = serve._run_outcome(store.load(state["id"]))
        self.assertEqual(out["triage"], "ACTIONABLE")
        self.assertEqual(out["triageSource"], "legacy-positive")
        self.assertIsNone(store.load(state["id"]).get("disposition"))

    def test_legacy_all_negative_is_unknown(self):
        from openmainty import serve
        state = self._run_with_log(
            "## DUPLICATE\n\nNone.\n\n## INFO-NEEDED\n\nNone.\n\n"
            "## ACTIONABLE\n\nNo.\n")
        out = serve._run_outcome(store.load(state["id"]))
        self.assertIsNone(out["triage"])
        self.assertEqual(out["triageSource"], "unknown")

    def test_legacy_ambiguous_positives_are_unknown(self):
        from openmainty import serve
        state = self._run_with_log(
            "## DUPLICATE\n\nYes, duplicate of #5 (same crash).\n\n"
            "## INFO-NEEDED\n\nNone.\n\n"
            "## ACTIONABLE\n\nYes. Implement the fix now.\n")
        out = serve._run_outcome(store.load(state["id"]))
        self.assertIsNone(out["triage"])
        self.assertEqual(out["triageSource"], "unknown")

    def test_legacy_confirmed_duplicate_names_issue(self):
        from openmainty import serve
        state = self._run_with_log(
            "## DUPLICATE\n\nYes, duplicate of #12 (same crash).\n\n"
            "## INFO-NEEDED\n\nNone.\n\n## ACTIONABLE\n\nNo.\n")
        out = serve._run_outcome(store.load(state["id"]))
        self.assertEqual(out["triage"], "DUPLICATE")
        self.assertEqual(out["triageSource"], "legacy-positive")

    def test_disposition_line_beats_legacy_headers(self):
        from openmainty import serve
        state = self._run_with_log(
            "DISPOSITION: INFO-NEEDED: exact log lines\n\n"
            "## DUPLICATE\n\nDuplicate?\n\n## ACTIONABLE\n\nMaybe.\n")
        out = serve._run_outcome(store.load(state["id"]))
        self.assertEqual(out["triage"], "INFO-NEEDED")
        self.assertEqual(out["triageSource"], "disposition-line")

    def test_malformed_disposition_line_stays_unknown(self):
        from openmainty import serve
        state = self._run_with_log(
            "DISPOSITION: MAYBE LATER\n\n" + self.RUN381)
        out = serve._run_outcome(store.load(state["id"]))
        self.assertIsNone(out["triage"])
        self.assertEqual(out["triageSource"], "unknown")

    def test_bare_verdict_line_with_detail_counts(self):
        from openmainty import serve
        state = self._run_with_log("ACTIONABLE: bump\n")
        out = serve._run_outcome(store.load(state["id"]))
        self.assertEqual(out["triage"], "ACTIONABLE")
        self.assertEqual(out["triageSource"], "legacy-positive")
        # A bare kind with no detail is a header word, not a verdict.
        state2 = self._run_with_log("ACTIONABLE\n")
        out2 = serve._run_outcome(store.load(state2["id"]))
        self.assertIsNone(out2["triage"])
        self.assertEqual(out2["triageSource"], "unknown")
        # A bare duplicate verdict without a target issue stays unknown.
        state3 = self._run_with_log("DUPLICATE: looks the same\n")
        out3 = serve._run_outcome(store.load(state3["id"]))
        self.assertIsNone(out3["triage"])
        state4 = self._run_with_log("DUPLICATE: of #5 (same crash)\n")
        out4 = serve._run_outcome(store.load(state4["id"]))
        self.assertEqual(out4["triage"], "DUPLICATE")

    def test_missing_log_reports_none_source(self):
        from openmainty import serve
        state = self._run_with_log(None)
        out = serve._run_outcome(store.load(state["id"]))
        self.assertIsNone(out["triage"])
        self.assertEqual(out["triageSource"], "none")

    def test_outcome_never_mutates_historical_run(self):
        from openmainty import serve
        state = self._run_with_log(self.RUN381)
        before = (store.run_dir(state["id"]) / "run.json").read_bytes()
        serve._run_outcome(store.load(state["id"]))
        serve.api_work({"run": state["id"]})
        after = (store.run_dir(state["id"]) / "run.json").read_bytes()
        self.assertEqual(before, after)
        self.assertIsNone(store.load(state["id"]).get("disposition"))


class RefsNegativeCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def test_invalid_numbers_never_touch_network(self):
        from openmainty import refs
        with mock.patch("openmainty.refs.run_cmd",
                        side_effect=AssertionError("no network")) as m:
            for bad in ("abc", "0", "-5", "#x", "", "12.5", "  "):
                got = refs.lookup("example/c5", bad)
                self.assertEqual(got["kind"], "unknown")
                self.assertTrue(got["error"])
                self.assertEqual(got["title"], "")
            self.assertEqual(m.call_count, 0)

    def test_unknown_failure_persists_bounded_negative_cache(self):
        from openmainty import refs
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(1, "", "offline")):
            first = refs.lookup("example/c5", 99991)
        self.assertEqual(first["kind"], "unknown")
        self.assertTrue(first["error"])
        self.assertFalse(first["fresh"])
        self.assertTrue(first["next_retry_at"])
        self.assertTrue(refs.cache_file("example/c5", 99991).is_file())
        # Repeated poll inside the window: zero network, same failure.
        with mock.patch("openmainty.refs.run_cmd",
                        side_effect=AssertionError("must not hammer")):
            second = refs.lookup("example/c5", 99991)
        self.assertEqual(second["kind"], "unknown")
        self.assertFalse(second["fresh"])
        # Past the window: exactly one retry attempt, then quiet again.
        entry = json.loads(
            refs.cache_file("example/c5", 99991).read_text(encoding="utf-8"))
        entry["next_retry_at"] = "2000-01-01T00:00:00+00:00"
        refs.cache_file("example/c5", 99991).write_text(
            json.dumps(entry), encoding="utf-8")
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(1, "", "still offline")) as m:
            third = refs.lookup("example/c5", 99991)
        self.assertEqual(m.call_count, 1)
        self.assertTrue(third["error"])
        with mock.patch("openmainty.refs.run_cmd",
                        side_effect=AssertionError("quiet again")):
            fourth = refs.lookup("example/c5", 99991)
        self.assertEqual(fourth["kind"], "unknown")

    def test_stale_refresh_quiet_window_then_retry(self):
        from openmainty import refs
        payload = {"number": 775, "title": "Real title", "state": "open",
                   "html_url": "https://github.com/example/c5/issues/775"}
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(0, json.dumps(payload), "")):
            first = refs.lookup("example/c5", 775)
        self.assertTrue(first["last_success_at"])
        cache = refs.cache_file("example/c5", 775)
        entry = json.loads(cache.read_text(encoding="utf-8"))
        entry["fetched_at"] = "2000-01-01T00:00:00+00:00"
        cache.write_text(json.dumps(entry), encoding="utf-8")
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(1, "", "offline")) as m:
            second = refs.lookup("example/c5", 775)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(second["title"], "Real title")
        self.assertFalse(second["fresh"])
        self.assertTrue(second["stale"])
        self.assertTrue(second["next_retry_at"])
        with mock.patch("openmainty.refs.run_cmd",
                        side_effect=AssertionError("quiet window")):
            third = refs.lookup("example/c5", 775)
        self.assertEqual(third["title"], "Real title")
        self.assertFalse(third["fresh"])

    def test_gh_read_is_time_bounded(self):
        from openmainty import refs
        with mock.patch("openmainty.refs.run_cmd",
                        return_value=(1, "", "offline")) as m:
            refs.lookup("example/c5", 99992)
        _args, kwargs = m.call_args
        self.assertLessEqual(kwargs.get("timeout", 10**9), 60)


if __name__ == "__main__":
    unittest.main()
