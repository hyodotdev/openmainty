"""Graph grounding revision (core7): own issue links, chat mentions from
persisted cache only, planned vs created branches, disposition-aware run
status, numstat file paths. Isolated fixtures only (temp OPENMAINTY_HOME,
real local git, stubbed gh). No real network, no public writes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import chat, graph, pcconfig, refs, repos, runner, sched, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


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
    return Path(tmp.name)


def make_profile(name="t1", github="example/t1"):
    repos.save({"name": name, "github": github, "baseBranch": "main",
                "knowledge": [], "checks": [], "e2e": [],
                "release": {}, "announce": {}})


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


def link_checkout(name, co: Path):
    pc = pcconfig.load()
    pc["checkouts"][name] = str(co)
    pcconfig.save(pc)


def write_refs_cache(github, number, kind="pr", title="T", url=""):
    dest = refs.cache_file(github, str(number))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(
        {"number": str(number), "kind": kind, "title": title,
         "state": "closed", "url": url or f"https://example.test/{number}",
         "fetched_at": "2026-10-18T00:00:00+00:00", "source": "gh-api",
         "fresh": True, "stale": False, "error": None}),
        encoding="utf-8")


def _pin_memo_clock(test, key, at):
    """Pin the memo clock, proving the stored entry is older than TTL.

    F5: Apple python3.9 starts time.monotonic() at 0.0, so aging an
    entry by writing ts=0.0 is vacuous there. Tests pin
    graph._mono instead; the guard assert fails the test if the pin
    does not actually exceed _SNAP_MEMO_TTL past the stored entry.
    """
    stored = graph._SNAP_MEMO[key][2]
    test.assertGreater(at - stored, graph._SNAP_MEMO_TTL,
                       "aging must actually exceed the TTL (F5 guard)")
    return mock.patch.object(graph, "_mono", return_value=at)


def setUpModule():
    # P1-live: no test in this module may run the real scope-listing
    # git subprocess (it would read machine-local scope files
    # outside the isolated fixtures). Fail it module-wide so every
    # pre-existing test keeps its legacy backstop behavior; the
    # P1ScopeCacheTest tests nest their own canned outputs.
    patcher = mock.patch.object(
        runner, "_git_config_origins", return_value=(1, "", "disabled"))
    patcher.start()


class ValidIssueNumberTests(unittest.TestCase):
    def test_numeric_only(self):
        self.assertEqual(graph.valid_issue_number("381"), "381")
        self.assertEqual(graph.valid_issue_number("#381"), "381")
        self.assertEqual(graph.valid_issue_number(766), "766")

    def test_synthetic_and_junk_rejected(self):
        for raw in ("dep-xyz", "dep-123", "", "0", "-5", "abc",
                    "3.5", None, "run-40857b36"):
            self.assertIsNone(graph.valid_issue_number(raw), raw)


class OwnIssueLinkTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        make_profile()
        link_checkout("t1", self.co)

    def test_run_owned_issue_links(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "381", "Record audio in background",
                             base, {"worker": "test"})
        store.save(state)
        g = graph.build("t1")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("issue:example/t1#381", ids)
        node = next(n for n in g["nodes"] if n["id"] == "issue:example/t1#381")
        # Actual title is the main label; technical id stays secondary.
        self.assertEqual(node["label"], "Record audio in background")
        self.assertEqual(node["technical_label"], "example/t1#381")
        self.assertEqual(node["title"], "Record audio in background")
        self.assertEqual(node["repo"], "t1")
        self.assertEqual(node["issue"], "381")
        about = [e for e in g["edges"]
                 if e["from"] == "issue:example/t1#381"
                 and e["to"] == f"run:{state['id']}" and e["kind"] == "about"]
        self.assertEqual(len(about), 1)
        self.assertTrue(about[0]["provenance"])
        run_node = next(n for n in g["nodes"] if n["id"] == f"run:{state['id']}")
        self.assertEqual(run_node["run_id"], state["id"])
        self.assertEqual(run_node["issue"], "381")
        self.assertEqual(run_node["label"], "Record audio in background")
        self.assertIn(state["id"], run_node["technical_label"])

    def test_queue_owned_issue_dedups_with_run(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "766", "Release recording setup fails",
                             base, {"worker": "test"})
        store.save(state)
        st = sched.load("t1")
        st["queue"].append({"kind": "issue-triage", "repo": "t1",
                            "issue": "766", "title": "Release recording setup fails",
                            "status": "queued", "attempts": 0,
                            "why": "upstream open issue #766",
                            "created_at": "t",
                            "key": "t1#issue-766"})
        sched.save(st)
        g = graph.build("t1")
        own = [n for n in g["nodes"] if n["id"] == "issue:example/t1#766"]
        self.assertEqual(len(own), 1)
        kinds = {(e["from"], e["kind"]) for e in g["edges"]
                 if e["from"] == "issue:example/t1#766"}
        self.assertIn(("issue:example/t1#766", "about"), kinds)
        targets = {e["to"] for e in g["edges"]
                   if e["from"] == "issue:example/t1#766" and e["kind"] == "about"}
        self.assertIn(f"run:{state['id']}", targets)
        self.assertIn("queue:t1:t1#issue-766", targets)

    def test_synthetic_run_issue_creates_no_issue_node(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "dep-xyz", "dep job", base, {"worker": "test"})
        store.save(state)
        g = graph.build("t1")
        self.assertEqual([n for n in g["nodes"] if n["type"] == "issue"], [])

    def test_same_number_different_repos_stays_separate(self):
        make_profile("t2", "example/t2")
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        for repo in ("t1", "t2"):
            store.save(store.create(repo, "5", f"title {repo}", base,
                                    {"worker": "test"}))
        g = graph.build()
        ids = {n["id"] for n in g["nodes"] if n["type"] == "issue"}
        self.assertIn("issue:example/t1#5", ids)
        self.assertIn("issue:example/t2#5", ids)


class ChatMentionsTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        make_profile()
        link_checkout("t1", self.co)

    def _reply_session(self, issue, title, draft_rel, draft_text):
        from openmainty import paths
        d = paths.home() / "drafts"
        d.mkdir(parents=True, exist_ok=True)
        (d / draft_rel).write_text(draft_text, encoding="utf-8")
        return chat.sync_reply_session("t1", issue, title, draft_rel)

    def test_chat_owned_issue_mentions_cached_refs(self):
        write_refs_cache("example/t1", "774", kind="pr",
                         title="fix: safe conversion",
                         url="https://example.test/pull/774")
        self._reply_session("766", "release recording fails", "ns-766.md",
                            "Guards shipped in 0.2.16 (#774, #832).")
        g = graph.build("t1")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("issue:example/t1#766", ids)
        self.assertIn("issue:example/t1#774", ids)
        self.assertIn("issue:example/t1#832", ids)
        ref = next(n for n in g["nodes"] if n["id"] == "issue:example/t1#774")
        self.assertEqual(ref["object_kind"], "pr")
        self.assertEqual(ref["title"], "fix: safe conversion")
        self.assertEqual(ref["url"], "https://example.test/pull/774")
        self.assertEqual(ref["label"], "fix: safe conversion")
        # Uncached #832 renders unknown truthfully, never invented.
        ref832 = next(n for n in g["nodes"] if n["id"] == "issue:example/t1#832")
        self.assertEqual(ref832["object_kind"], "unknown")
        self.assertEqual(ref832["title"], "")
        mentions = [e for e in g["edges"]
                    if e["from"] == "issue:example/t1#766"
                    and e["to"] == "issue:example/t1#774"]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0]["kind"], "mentions")
        self.assertIn("persisted chat", mentions[0]["provenance"])
        # Mere mention never becomes a fixes relationship.
        self.assertFalse(any(e["kind"] == "fixes" for e in g["edges"]))
        own = next(n for n in g["nodes"] if n["id"] == "issue:example/t1#766")
        self.assertTrue(own.get("source_chat", "").startswith("chat-"))
        # Referenced nodes also carry the proving chat (first wins).
        self.assertTrue(ref.get("source_chat", "").startswith("chat-"))

    def test_graph_never_fetches_network(self):
        self._reply_session("766", "t", "ns-766.md", "see #774.")
        with mock.patch("openmainty.refs.lookup",
                        side_effect=AssertionError("network in graph poll")), \
             mock.patch("openmainty.refs._gh_api_issue",
                        side_effect=AssertionError("network in graph poll")):
            g = graph.build("t1")
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("issue:example/t1#766", ids)
        self.assertIn("issue:example/t1#774", ids)


class BranchStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        make_profile()
        link_checkout("t1", self.co)

    def _queue_branch(self, branch):
        st = sched.load("t1")
        st["queue"].append({"kind": "dep-update", "repo": "t1", "dep": "left-pad",
                            "target": "1.1.0", "lane": "next", "status": "queued",
                            "attempts": 0, "branch": branch, "created_at": "t",
                            "key": f"t1+left-pad+{branch}"})
        sched.save(st)

    def test_scan_plan_is_planned_not_created(self):
        self._queue_branch("codex/next/plan-only")
        g = graph.build("t1")
        node = next(n for n in g["nodes"]
                    if n["id"] == "branch:t1:codex/next/plan-only")
        self.assertTrue(node["planned"])
        self.assertEqual(node["status"], "planned")
        self.assertFalse(node["exists"])
        self.assertTrue(node["source"])

    def test_actual_run_branch_is_created(self):
        git("branch", "codex/next/real-work", cwd=self.co)
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "9", "real work", base, {"worker": "test"})
        state["branch"] = "codex/next/real-work"
        store.save(state)
        g = graph.build("t1")
        node = next(n for n in g["nodes"]
                    if n["id"] == "branch:t1:codex/next/real-work")
        self.assertFalse(node["planned"])
        self.assertEqual(node["status"], "created")
        self.assertTrue(node["exists"])

    def test_plan_upgraded_on_actual_creation(self):
        self._queue_branch("codex/next/both")
        before = graph.build("t1")
        planned = next(n for n in before["nodes"]
                       if n["id"] == "branch:t1:codex/next/both")
        self.assertEqual(planned["status"], "planned")
        git("branch", "codex/next/both", cwd=self.co)
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "9", "real work", base, {"worker": "test"})
        state["branch"] = "codex/next/both"
        store.save(state)
        after = graph.build("t1")
        same = [n for n in after["nodes"]
                if n["id"] == "branch:t1:codex/next/both"]
        # add() must not freeze the earlier planned metadata: one node,
        # upgraded to created.
        self.assertEqual(len(same), 1)
        self.assertEqual(same[0]["status"], "created")
        self.assertFalse(same[0]["planned"])
        self.assertTrue(same[0]["exists"])

    def test_unreadable_checkout_is_unknown(self):
        pc = pcconfig.load()
        pc["checkouts"]["t1"] = str(self.root / "missing")
        pcconfig.save(pc)
        self._queue_branch("codex/next/plan-only")
        g = graph.build("t1")
        node = next(n for n in g["nodes"]
                    if n["id"] == "branch:t1:codex/next/plan-only")
        self.assertIsNone(node["exists"])
        self.assertEqual(node["status"], "unknown")


class DispositionStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        make_profile()
        link_checkout("t1", self.co)

    def test_duplicate_renders_terminal_not_active(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "5", "dup issue", base, {"worker": "test"})
        state["disposition"] = {"kind": "DUPLICATE", "detail": "of #4",
                                "ref": "#4", "at": "t"}
        state["waitReason"] = "duplicate: of #4"
        store.save(state)
        g = graph.build("t1")
        node = next(n for n in g["nodes"] if n["id"] == f"run:{state['id']}")
        self.assertEqual(node["status"], "duplicate")
        self.assertIn("duplicate", node["detail"])

    def test_info_needed_renders_waiting(self):
        base = git("rev-parse", "HEAD", cwd=self.co).strip()
        state = store.create("t1", "6", "need info", base, {"worker": "test"})
        state["disposition"] = {"kind": "INFO-NEEDED", "detail": "ask",
                                "ref": None, "at": "t"}
        state["waitReason"] = "question: what shape?"
        store.save(state)
        g = graph.build("t1")
        node = next(n for n in g["nodes"] if n["id"] == f"run:{state['id']}")
        self.assertEqual(node["status"], "waiting")


class QueueWatchStatusTests(unittest.TestCase):
    """An actively-watching queue item renders as an external wait.

    Mirrors the real babel-core record (stored status waiting-review
    with an active watch payload): the graph work node must read watch
    instead of human review, keep the stored raw status, and never
    mutate the scheduler row. A cancelled historical record carrying a
    stale watch payload stays cancelled.
    """

    def setUp(self):
        self.root = make_home(self)
        self.co = make_git_repo(self.root)
        make_profile()
        link_checkout("t1", self.co)

    def _enqueue(self, items):
        st = sched.load("t1")
        st["queue"] = items
        sched.save(st)

    @staticmethod
    def _watch_item(status, key="t1+pkg+2.0.0+stable"):
        return {"key": key, "repo": "t1", "kind": "dep-update",
                "dep": "pkg", "target": "2.0.0", "status": status,
                "lane": "next", "attempts": 2,
                "why": "dep 1.0.0 -> 2.0.0 is MAJOR",
                "watch": {"reason": "wait for peer toolchain",
                           "trigger": "compatible release",
                           "next_check": "t", "last_check": "t",
                           "active": True}}

    def test_active_watch_queue_item_renders_watch(self):
        self._enqueue([self._watch_item("waiting-review")])
        g = graph.build("t1")
        node = next(n for n in g["nodes"]
                    if n["id"] == "queue:t1:t1+pkg+2.0.0+stable")
        self.assertEqual(node["status"], "watch")
        self.assertEqual(node["raw_status"], "waiting-review")
        self.assertIn("wait for peer toolchain", node["detail"])
        # Stored scheduler row is untouched (readonly display fix).
        row = sched.load("t1")["queue"][0]
        self.assertEqual(row["status"], "waiting-review")
        self.assertNotIn("raw_status", row)

    def test_cancelled_historical_watch_stays_cancelled(self):
        self._enqueue([self._watch_item("cancelled")])
        g = graph.build("t1")
        node = next(n for n in g["nodes"]
                    if n["id"] == "queue:t1:t1+pkg+2.0.0+stable")
        self.assertEqual(node["status"], "cancelled")

    def test_plain_queue_item_keeps_stored_status(self):
        item = self._watch_item("queued")
        item.pop("watch")
        self._enqueue([item])
        g = graph.build("t1")
        node = next(n for n in g["nodes"]
                    if n["id"] == "queue:t1:t1+pkg+2.0.0+stable")
        self.assertEqual(node["status"], "queued")


class NumstatPathTests(unittest.TestCase):
    def test_parse_full_paths_and_real_counts(self):
        text = ("12\t3\ta/b/c.txt\n"
                "205\t17\tpkg/sub dir/deep module/file name with spaces.ts\n"
                "5\t2\told name.txt => new name.txt\n"
                "-\t-\tassets/img.png\n")
        files = serve._parse_numstat_text(text)
        by_path = {f["path"]: f for f in files}
        self.assertEqual(by_path["a/b/c.txt"]["additions"], 12)
        self.assertEqual(by_path["a/b/c.txt"]["deletions"], 3)
        self.assertEqual(by_path["a/b/c.txt"]["changes"], 15)
        deep = "pkg/sub dir/deep module/file name with spaces.ts"
        self.assertIn(deep, by_path)
        self.assertEqual(by_path[deep]["changes"], 222)
        self.assertIn("new name.txt", by_path)
        self.assertNotIn("old name.txt", by_path)
        self.assertIn("assets/img.png", by_path)
        self.assertFalse(any("..." in f["path"] for f in files))

    def test_live_changes_keep_deep_paths_and_counts(self):
        co = make_git_repo(self.root if hasattr(self, "root") else
                           make_home(self), name="deep")
        deep = co / "packages" / "sub dir" / "deep module"
        deep.mkdir(parents=True)
        target = deep / "file name with spaces.ts"
        target.write_text("line1\n", encoding="utf-8")
        git("add", "-A", cwd=co)
        git("commit", "-qm", "add deep file", cwd=co)
        base = git("rev-parse", "HEAD", cwd=co).strip()
        lines = [f"added {i}\n" for i in range(60)] + ["line1\n"]
        target.write_text("".join(lines), encoding="utf-8")
        state = {"id": "run-x", "base_branch": "main", "branch": None,
                 "lane": "main", "baseCommit": base, "workCommit": base}
        live_head = git("rev-parse", "HEAD", cwd=co).strip()
        sc = serve._live_source_changes(state, co, base, base, live_head)
        rel = "packages/sub dir/deep module/file name with spaces.ts"
        entry = next((f for f in sc["fileDiffs"] if f["path"] == rel), None)
        self.assertIsNotNone(entry, sc["fileDiffs"])
        self.assertEqual(entry["additions"], 60)
        self.assertEqual(entry["deletions"], 0)
        self.assertEqual(entry["changes"], 60)
        self.assertEqual(sc["filesSource"], "live-worktree-diff-vs-base")


class GraphSingleScanTest(unittest.TestCase):
    def test_build_scans_home_once_per_call(self):
        # L1(243): the per-repo loop must not rescan the whole home
        # (runs + chat sessions) once per repo. L10h: the runs scan
        # also yields the truncation total from the same pass.
        make_home(self)
        make_profile("t1")
        make_profile("t2")
        with mock.patch.object(store, "list_runs_with_total",
                               side_effect=lambda limit=20: ([], 0)
                               ) as m_runs, \
                mock.patch.object(chat, "list_sessions",
                                  side_effect=lambda limit=500: []) as m_sess:
            graph.build("")
        self.assertEqual(m_runs.call_count, 1)
        self.assertEqual(m_sess.call_count, 1)


class GraphSnapshotMemoTest(unittest.TestCase):
    # L1c(245): per-run snapshots are memoised keyed by an exact cheap
    # signature (HEAD content + per-file size/mtime/ctime over the same
    # relevant set). Every input that changes the snapshot output must
    # invalidate, proved below by output (verify nodes appear/vanish)
    # and by source_snapshot call counts.

    def _verified_run(self, files=3, git_init=False):
        make_home(self)
        make_profile()
        st = store.create("t1", "381", "memo run", "base0",
                          {"worker": "test"})
        st["steps"] = {s: {"status": "done"}
                       for s in ("reproduce", "review", "e2e")}
        work = store.run_dir(st["id"]) / "work"
        work.mkdir(parents=True, exist_ok=True)
        for i in range(files):
            (work / f"f{i}.txt").write_text(f"content {i}\n",
                                            encoding="utf-8")
        if git_init:
            git("init", "-b", "main", cwd=work)
            git("config", "user.email", "t@localhost", cwd=work)
            git("config", "user.name", "t", cwd=work)
            git("add", "-A", cwd=work)
            git("commit", "-qm", "init", cwd=work)
            head = git("rev-parse", "HEAD", cwd=work).strip()
            st["workCommit"] = head
            commits = {s: head for s in ("reproduce", "review", "e2e")}
        else:
            st["workCommit"] = "c0"
            commits = {s: "c0" for s in ("reproduce", "review", "e2e")}
        fp = runner.source_snapshot(work)["fingerprint"]
        st["verifiedFingerprints"] = {
            s: fp for s in ("reproduce", "review", "e2e")}
        st["verifiedCommits"] = commits
        store.save(st)
        return st, work

    def _counting_builds(self, n):
        calls = []
        orig = runner.source_snapshot

        def counting(work, **kw):
            calls.append(str(work))
            return orig(work, **kw)

        outs = []
        with mock.patch.object(runner, "source_snapshot",
                               side_effect=counting):
            for _ in range(n):
                outs.append(graph.build("t1"))
        return outs, calls

    def _has_verify(self, out, run_id, step="reproduce"):
        want = f"verify:{run_id}:{step}"
        return any(n.get("id") == want for n in out["nodes"])

    def test_second_build_reuses_snapshot(self):
        st, _work = self._verified_run()
        outs, calls = self._counting_builds(2)
        self.assertEqual(len(calls), 1,
                         "unchanged worktree reads its snapshot once")
        self.assertTrue(self._has_verify(outs[0], st["id"]))
        self.assertEqual(outs[1], outs[0],
                         "memoised build is byte-identical")

    def test_byte_edit_invalidates(self):
        st, work = self._verified_run()
        outs, _calls = self._counting_builds(1)
        self.assertTrue(self._has_verify(outs[0], st["id"]))
        # Same-size rewrite: size alone must not keep the proof.
        (work / "f0.txt").write_text("CONTENT 0\n", encoding="utf-8")
        outs2, calls2 = self._counting_builds(1)
        self.assertEqual(len(calls2), 1, "edited worktree re-reads")
        self.assertFalse(self._has_verify(outs2[0], st["id"]),
                         "edited bytes drop the fresh proof")

    def test_add_and_remove_invalidate(self):
        st, work = self._verified_run()
        outs, _calls = self._counting_builds(1)
        self.assertTrue(self._has_verify(outs[0], st["id"]))
        (work / "new.txt").write_text("new\n", encoding="utf-8")
        outs2, _calls2 = self._counting_builds(1)
        self.assertFalse(self._has_verify(outs2[0], st["id"]),
                         "added file drops the fresh proof")
        (work / "new.txt").unlink()
        (work / "f1.txt").unlink()
        outs3, _calls3 = self._counting_builds(1)
        self.assertFalse(self._has_verify(outs3[0], st["id"]),
                         "removed file drops the fresh proof")

    def test_mtime_forgery_still_invalidates(self):
        st, work = self._verified_run()
        target = work / "f0.txt"
        before = target.stat()
        outs, _calls = self._counting_builds(1)
        self.assertTrue(self._has_verify(outs[0], st["id"]))
        # Rewrite same-size bytes, then forge the old mtime back: ctime
        # still changes, so the memo must miss.
        target.write_text("CONTENT 0\n", encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        forged = target.stat()
        self.assertEqual((forged.st_size, forged.st_mtime_ns),
                         (before.st_size, before.st_mtime_ns))
        _outs2, calls2 = self._counting_builds(1)
        self.assertEqual(len(calls2), 1,
                         "mtime forgery must still re-read (ctime)")

    def test_head_move_invalidates(self):
        st, work = self._verified_run(git_init=True)
        outs, _calls = self._counting_builds(1)
        self.assertTrue(self._has_verify(outs[0], st["id"]))
        (work / "f0.txt").write_text("v2\n", encoding="utf-8")
        git("add", "-A", cwd=work)
        git("commit", "-qm", "two", cwd=work)
        outs2, calls2 = self._counting_builds(1)
        self.assertEqual(len(calls2), 1, "moved HEAD re-reads")
        self.assertFalse(self._has_verify(outs2[0], st["id"]),
                         "moved HEAD drops the fresh proof")

    def test_missing_worktree_is_unknown_not_memoised(self):
        st, work = self._verified_run()
        outs, _calls = self._counting_builds(1)
        self.assertTrue(self._has_verify(outs[0], st["id"]))
        shutil.rmtree(work)
        outs2, calls2 = self._counting_builds(1)
        self.assertFalse(self._has_verify(outs2[0], st["id"]))
        run_node = next(n for n in outs2[0]["nodes"]
                        if n.get("id") == f"run:{st['id']}")
        self.assertIn("unreadable", run_node.get("detail", ""),
                      "missing worktree reports unknown, never green")


class LinkedWorktreeSignatureTest(unittest.TestCase):
    # L1c-steer(245b): every real run worktree is a LINKED worktree
    # (.git is a "gitdir:" pointer file). The first L1c signature read
    # work/.git/HEAD directly (NotADirectoryError -> ""), so HEAD moves,
    # index staging, and ref packing were all invisible to the memo and
    # stale head/fingerprint pairs were served. The signature must
    # resolve the gitdir, follow symref HEAD through the common dir
    # (loose ref, else packed-refs), and cover the per-worktree index —
    # fail-closed (None) on anything unreadable.
    #
    # Real git in temp dirs only. Identity comes from -c flags; the
    # global/system git configs resolve inside the temp dir so neither
    # the real home nor any target repo is touched.

    def _linked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        (base / "gitconfig").write_text("", encoding="utf-8")
        (base / "nosystem").write_text("", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {
            "HOME": str(home),
            "GIT_CONFIG_GLOBAL": str(base / "gitconfig"),
            "GIT_CONFIG_SYSTEM": str(base / "nosystem")})
        patcher.start()
        self.addCleanup(patcher.stop)

        def gitl(*args, cwd):
            proc = subprocess.run(
                ["git", "-C", str(cwd)] + list(args),
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise AssertionError(
                    f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
            return proc.stdout

        main = base / "main"
        main.mkdir()
        gitl("init", "-b", "main", cwd=main)
        (main / "code.txt").write_text("v1\n", encoding="utf-8")
        gitl("add", "-A", cwd=main)
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", "init", cwd=main)
        rundir = base / "rundir"
        wt = rundir / "work"
        gitl("worktree", "add", "-b", "feature", str(wt), cwd=main)
        self.assertTrue((wt / ".git").is_file(),
                        "linked worktree carries a .git pointer file")
        return gitl, wt, rundir

    def _commit(self, gitl, wt, msg="change"):
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", msg, cwd=wt)

    def test_git_add_of_present_file_changes_signature(self):
        gitl, wt, _rundir = self._linked()
        before = runner.worktree_signature(wt)
        self.assertIsNotNone(before)
        (wt / "extra.txt").write_text("x\n", encoding="utf-8")
        untracked = runner.worktree_signature(wt)
        self.assertNotEqual(untracked, before,
                            "new untracked file changes the signature")
        gitl("add", "extra.txt", cwd=wt)
        staged = runner.worktree_signature(wt)
        self.assertNotEqual(staged, untracked,
                            "staging must move the index-covered signature")

    def test_commit_changes_signature(self):
        gitl, wt, _rundir = self._linked()
        (wt / "code.txt").write_text("v2\n", encoding="utf-8")
        gitl("add", "-A", cwd=wt)
        staged = runner.worktree_signature(wt)
        self.assertIsNotNone(staged)
        self._commit(gitl, wt)
        after = runner.worktree_signature(wt)
        self.assertNotEqual(after, staged,
                            "commit must move the HEAD-covered signature")

    def test_branch_switch_changes_signature(self):
        gitl, wt, _rundir = self._linked()
        before = runner.worktree_signature(wt)
        gitl("checkout", "-qb", "other", cwd=wt)
        after = runner.worktree_signature(wt)
        self.assertNotEqual(after, before,
                            "branch switch must move the symref signature")

    def test_pack_refs_then_commit_changes_signature(self):
        gitl, wt, _rundir = self._linked()
        before = runner.worktree_signature(wt)
        gitl("pack-refs", "--all", cwd=wt)
        packed = runner.worktree_signature(wt)
        self.assertIsNotNone(packed,
                              "packed-refs fallback must still resolve")
        self.assertNotEqual(packed, before,
                            "packing refs must change the signature")
        (wt / "code.txt").write_text("v2\n", encoding="utf-8")
        gitl("add", "-A", cwd=wt)
        staged = runner.worktree_signature(wt)
        self.assertIsNotNone(staged)
        self._commit(gitl, wt)
        after = runner.worktree_signature(wt)
        self.assertNotEqual(after, staged,
                            "commit after packing must re-resolve and move")

    def test_broken_gitdir_pointer_returns_none(self):
        _gitl, wt, _rundir = self._linked()
        (wt / ".git").write_text("gitdir: /nonexistent-xyz-245b\n",
                                 encoding="utf-8")
        self.assertIsNone(runner.worktree_signature(wt),
                          "dangling gitdir pointer must fail closed")
        (wt / ".git").write_text("garbage\n", encoding="utf-8")
        self.assertIsNone(runner.worktree_signature(wt),
                          "unparsable .git file must fail closed")

    def test_memo_returns_new_snapshot_after_add_and_commit(self):
        gitl, wt, rundir = self._linked()
        run = {"id": "run-linked01"}
        with mock.patch.object(store, "run_dir", return_value=rundir):
            first = graph._memo_live_snapshot(run, wt)
            self.assertIsNotNone(first)
            (wt / "extra.txt").write_text("x\n", encoding="utf-8")
            untracked = graph._memo_live_snapshot(run, wt)
            gitl("add", "extra.txt", cwd=wt)
            added = graph._memo_live_snapshot(run, wt)
            self.assertNotEqual(added["fingerprint"],
                                untracked["fingerprint"],
                                "memo must serve the post-add fingerprint")
            (wt / "code.txt").write_text("v2\n", encoding="utf-8")
            gitl("add", "-A", cwd=wt)
            staged = graph._memo_live_snapshot(run, wt)
            self._commit(gitl, wt)
            committed = graph._memo_live_snapshot(run, wt)
            self.assertNotEqual(committed["head"], staged["head"],
                                "memo must serve the post-commit head")
            self.assertEqual(
                committed,
                runner.source_snapshot(wt),
                "memoised snapshot equals a fresh read after commit")


class P1SignatureRevalidationTest(unittest.TestCase):
    # P1(249): the memo revalidated by signature instead of expiring on
    # a 300 s timer. An unchanged worktree is served without re-hashing
    # no matter how old the entry is; any covered change (files, HEAD,
    # refs, index, global excludes/config) re-reads immediately.
    # Real git in temp dirs only; identity via -c flags; global and
    # system git configs resolve inside the temp dir.

    def _work(self, extra=None, drop=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        (base / "gitconfig").write_text("", encoding="utf-8")
        (base / "nosystem").write_text("", encoding="utf-8")
        env = {"HOME": str(home),
               "GIT_CONFIG_GLOBAL": str(base / "gitconfig"),
               "GIT_CONFIG_SYSTEM": str(base / "nosystem")}
        for key in drop:
            env.pop(key, None)
        if extra:
            env.update(extra)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in drop:
            os.environ.pop(key, None)

        def gitl(*args, cwd):
            proc = subprocess.run(
                ["git", "-C", str(cwd)] + list(args),
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise AssertionError(
                    f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
            return proc.stdout

        main = base / "main"
        main.mkdir()
        gitl("init", "-b", "main", cwd=main)
        (main / "code.txt").write_text("v1\n", encoding="utf-8")
        gitl("add", "-A", cwd=main)
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", "init", cwd=main)
        wt = base / "work"
        gitl("worktree", "add", "-b", "feature", str(wt), cwd=main)
        rundir = base / "rundir"
        rundir.mkdir()
        (rundir / "work").symlink_to(wt, target_is_directory=True)
        return gitl, wt, rundir

    def _memo(self, rundir, wt, calls):
        run = {"id": "run-p1-01"}
        orig = runner.source_snapshot

        def counting(work, **kw):
            calls.append(str(work))
            return orig(work, **kw)

        with mock.patch.object(store, "run_dir", return_value=rundir), \
                mock.patch.object(runner, "source_snapshot",
                                  side_effect=counting):
            return graph._memo_live_snapshot(run, wt)

    def test_unchanged_served_without_rehash_past_ttl(self):
        # F1 certain path: only a truthy GIT_CONFIG_NOSYSTEM skips
        # the system scope (Apple Git reads an unknown-scope file
        # even when GIT_CONFIG_SYSTEM is set).
        _gitl, wt, rundir = self._work(
            extra={"GIT_CONFIG_NOSYSTEM": "1"})
        calls = []
        t0 = 10000.0
        with mock.patch.object(graph, "_mono", return_value=t0):
            first = self._memo(rundir, wt, calls)
        self.assertEqual(len(calls), 1, "cold read hashes once")
        key = str(wt)
        try:
            calls.clear()
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                second = self._memo(rundir, wt, calls)
        finally:
            graph._SNAP_MEMO.pop(key, None)
        self.assertEqual(calls, [],
                         "aged entry with matching signature must not re-hash")
        self.assertEqual(second, first)

    def test_changed_worktree_reread_immediately(self):
        gitl, wt, rundir = self._work()
        calls = []
        first = self._memo(rundir, wt, calls)
        (wt / "code.txt").write_text("v2\n", encoding="utf-8")
        calls.clear()
        second = self._memo(rundir, wt, calls)
        self.assertEqual(len(calls), 1, "edit must re-read immediately")
        self.assertNotEqual(second["fingerprint"], first["fingerprint"])
        gitl("add", "-A", cwd=wt)
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", "two", cwd=wt)
        calls.clear()
        third = self._memo(rundir, wt, calls)
        graph._SNAP_MEMO.pop(str(wt), None)
        self.assertEqual(len(calls), 1, "commit must re-read immediately")
        self.assertNotEqual(third["head"], second["head"])

    def test_global_ignore_edit_invalidates(self):
        _gitl, wt, rundir = self._work()
        (wt / "junk.tmp").write_text("x\n", encoding="utf-8")
        calls = []
        first = self._memo(rundir, wt, calls)
        self.assertIn("junk.tmp", first["untracked"])
        ignore = Path.home() / ".config" / "git" / "ignore"
        ignore.parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text("*.tmp\n", encoding="utf-8")
        calls.clear()
        second = self._memo(rundir, wt, calls)
        graph._SNAP_MEMO.pop(str(wt), None)
        self.assertEqual(len(calls), 1,
                         "global ignore edit must re-read immediately")
        self.assertNotEqual(second["fingerprint"], first["fingerprint"],
                            "ignored file must leave the fingerprint")

    def _excludes_case(self, kind, abs_target=None):
        # Custom core.excludesFile; editing ONLY the target must
        # re-read immediately. Returns (first, second, calls).
        _gitl, wt, rundir = self._work()
        if kind == "tilde":
            setting = "~/t249-custom-ignore"
            target = Path.home() / "t249-custom-ignore"
        else:
            setting = str(abs_target)
            target = abs_target
        (wt / "junk.tmp").write_text("x\n", encoding="utf-8")
        target.write_text("# nothing yet\n", encoding="utf-8")
        gcfg = Path(os.environ["GIT_CONFIG_GLOBAL"])
        gcfg.write_text(f"[core]\n\texcludesfile = {setting}\n",
                        encoding="utf-8")
        calls = []
        first = self._memo(rundir, wt, calls)
        self.assertIn("junk.tmp", first["untracked"])
        target.write_text("*.tmp\n", encoding="utf-8")
        calls.clear()
        second = self._memo(rundir, wt, calls)
        graph._SNAP_MEMO.pop(str(wt), None)
        return first, second, calls

    def test_custom_excludes_target_edit_invalidates(self):
        tmp = tempfile.TemporaryDirectory(
            prefix="muse249-exclt.",
            dir="/tmp/openmainty-supervision-20261018")
        self.addCleanup(tmp.cleanup)
        abs_target = Path(tmp.name) / "custom-ignore"
        for kind in ("absolute", "tilde"):
            with self.subTest(kind=kind):
                first, second, calls = self._excludes_case(
                    kind, abs_target if kind == "absolute" else None)
                self.assertEqual(
                    len(calls), 1,
                    "editing only the excludes target must re-read")
                self.assertNotEqual(
                    second["fingerprint"], first["fingerprint"])
                self.assertNotIn("junk.tmp", second["untracked"],
                                 "ignored file must leave the snapshot")

    def test_relative_excludes_resolves_to_worktree(self):
        # Empirical git rule (probed): a relative core.excludesFile
        # resolves against the worktree. In-tree targets are already
        # covered by the file walk; the resolver must just agree.
        _gitl, wt, _rundir = self._work()
        got = runner._resolve_excludes_target(
            "rel249-excl", wt, Path.home())
        self.assertEqual(got, wt / "rel249-excl")
        got = runner._resolve_excludes_target(
            "~/t249-x", wt, Path.home())
        self.assertEqual(got, Path.home() / "t249-x")
        got = runner._resolve_excludes_target(
            "/abs/t249-x", wt, Path.home())
        self.assertEqual(got, Path("/abs/t249-x"))

    def test_include_config_keeps_backstop(self):
        # F1: the plain-config phase is the certain path, so the
        # whole fixture skips the system scope (the include phase
        # keeps its backstop either way).
        _gitl, wt, rundir = self._work(
            extra={"GIT_CONFIG_NOSYSTEM": "1"})
        inc = Path(os.environ["HOME"]) / "inc.conf"
        inc.write_text("[core]\n", encoding="utf-8")
        gcfg = Path(os.environ["GIT_CONFIG_GLOBAL"])
        gcfg.write_text("[include]\n\tpath = ~/inc.conf\n",
                        encoding="utf-8")
        calls = []
        t0 = 10000.0
        with mock.patch.object(graph, "_mono", return_value=t0):
            first = self._memo(rundir, wt, calls)
        self.assertEqual(len(calls), 1)
        key = str(wt)
        try:
            calls.clear()
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                second = self._memo(rundir, wt, calls)
        finally:
            graph._SNAP_MEMO.pop(key, None)
        self.assertEqual(
            len(calls), 1,
            "include-config entry must keep the 300 s backstop")
        self.assertEqual(second, first)
        # Same worktree with a plain config: aged entries serve.
        gcfg.write_text("", encoding="utf-8")
        calls.clear()
        with mock.patch.object(graph, "_mono", return_value=t0):
            third = self._memo(rundir, wt, calls)
        self.assertEqual(len(calls), 1, "config rewrite re-reads once")
        try:
            calls.clear()
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                fourth = self._memo(rundir, wt, calls)
        finally:
            graph._SNAP_MEMO.pop(key, None)
        self.assertEqual(calls, [],
                         "plain config serves aged entries without re-hash")
        self.assertEqual(fourth, third)

    def test_unreadable_still_fails_closed(self):
        _gitl, wt, rundir = self._work()
        calls = []
        first = self._memo(rundir, wt, calls)
        self.assertIsNotNone(first)
        (wt / ".git").write_text("garbage\n", encoding="utf-8")
        calls.clear()
        second = self._memo(rundir, wt, calls)
        self.assertIsNone(second,
                          "broken git pointer reports unknown, never stale")
        self.assertEqual(len(calls), 1, "unreadable signature must re-read")
        self.assertNotIn(str(wt), graph._SNAP_MEMO,
                         "failed re-read must not be stored")


class P1ConfigScopesTest(unittest.TestCase):
    # P1(249-steer): the excludes scan honours the env scopes git
    # honours (all probed against real git in isolation).
    # GIT_CONFIG_GLOBAL replaces the default global files (even
    # when empty); GIT_CONFIG_SYSTEM names the system file;
    # GIT_CONFIG_NOSYSTEM (boolean) skips the system scope. An
    # unlocated system default keeps the 300 s backstop. Real git
    # in temp dirs only; identity via -c flags.

    def _work(self, extra=None, drop=()):
        tmp = tempfile.TemporaryDirectory(
            prefix="muse249-scope.",
            dir="/tmp/openmainty-supervision-20261018")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        (base / "gitconfig").write_text("", encoding="utf-8")
        (base / "sysconfig").write_text("", encoding="utf-8")
        env = {"HOME": str(home),
               "XDG_CONFIG_HOME": str(base / "xdg"),
               "GIT_CONFIG_GLOBAL": str(base / "gitconfig"),
               "GIT_CONFIG_SYSTEM": str(base / "sysconfig")}
        for key in drop:
            env.pop(key, None)
        if extra:
            env.update(extra)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in drop:
            # mock.patch.dict restores the original mapping (deleted
            # keys included) when the patcher stops.
            os.environ.pop(key, None)

        def gitl(*args, cwd):
            proc = subprocess.run(
                ["git", "-C", str(cwd)] + list(args),
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise AssertionError(
                    f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
            return proc.stdout

        main = base / "main"
        main.mkdir()
        gitl("init", "-b", "main", cwd=main)
        (main / "code.txt").write_text("v1\n", encoding="utf-8")
        gitl("add", "-A", cwd=main)
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", "init", cwd=main)
        wt = base / "work"
        gitl("worktree", "add", "-b", "feature", str(wt), cwd=main)
        rundir = base / "rundir"
        rundir.mkdir()
        (rundir / "work").symlink_to(wt, target_is_directory=True)
        return gitl, wt, rundir, base

    def _memo(self, rundir, wt, calls):
        run = {"id": "run-p1-scope-01"}
        orig = runner.source_snapshot

        def counting(work, **kw):
            calls.append(str(work))
            return orig(work, **kw)

        with mock.patch.object(store, "run_dir", return_value=rundir), \
                mock.patch.object(runner, "source_snapshot",
                                  side_effect=counting):
            return graph._memo_live_snapshot(run, wt)

    def test_system_config_target_edit_invalidates(self):
        _gitl, wt, rundir, base = self._work()
        syscfg = base / "sysconfig"
        target = base / "sys-ignore"
        (wt / "junk.tmp").write_text("x\n", encoding="utf-8")
        target.write_text("# nothing yet\n", encoding="utf-8")
        syscfg.write_text("[core]\n\texcludesfile = %s\n" % target,
                         encoding="utf-8")
        # F1: a set GIT_CONFIG_SYSTEM keeps its coverage (the file
        # is statted and scanned) but stays uncertain, because
        # Apple Git also reads an unknown-scope developer-dir file.
        self.assertTrue(
            runner.worktree_signature_backstop(wt),
            "set GIT_CONFIG_SYSTEM keeps the backstop (F1)")
        calls = []
        t0 = 10000.0
        with mock.patch.object(graph, "_mono", return_value=t0):
            first = self._memo(rundir, wt, calls)
        self.assertIn("junk.tmp", first["untracked"])
        target.write_text("*.tmp\n", encoding="utf-8")
        calls.clear()
        with mock.patch.object(graph, "_mono", return_value=t0):
            second = self._memo(rundir, wt, calls)
        self.assertEqual(
            len(calls), 1,
            "editing only the system-named target must re-read")
        self.assertNotEqual(second["fingerprint"], first["fingerprint"])
        self.assertNotIn("junk.tmp", second["untracked"],
                         "ignored file must leave the snapshot")
        key = str(wt)
        try:
            calls.clear()
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                third = self._memo(rundir, wt, calls)
        finally:
            graph._SNAP_MEMO.pop(key, None)
        self.assertEqual(
            len(calls), 1, "uncertain aged entry must re-read (F1)")
        self.assertEqual(third, second)

    def test_global_replaces_default_global_files(self):
        _gitl, wt, rundir, base = self._work()
        home = Path(os.environ["HOME"])
        target = base / "home-ignore"
        target.write_text("*.tmp\n", encoding="utf-8")
        (home / ".gitconfig").write_text(
            "[core]\n\texcludesfile = %s\n" % target, encoding="utf-8")
        (wt / "junk.tmp").write_text("x\n", encoding="utf-8")
        calls = []
        first = self._memo(rundir, wt, calls)
        self.assertIn(
            "junk.tmp", first["untracked"],
            "a set GIT_CONFIG_GLOBAL replaces ~/.gitconfig end to end")
        graph._SNAP_MEMO.pop(str(wt), None)
        before = runner.worktree_signature(wt)
        (home / ".gitconfig").write_text(
            "# rewritten\n[core]\n\texcludesfile = %s\n" % target,
            encoding="utf-8")
        self.assertEqual(
            runner.worktree_signature(wt), before,
            "shadowed ~/.gitconfig edits must not move the signature")
        gcfg = Path(os.environ["GIT_CONFIG_GLOBAL"])
        gcfg.write_text("[core]\n\texcludesfile = %s\n" % target,
                        encoding="utf-8")
        self.assertNotEqual(
            runner.worktree_signature(wt), before,
            "the replacing global file is covered (control)")
        calls.clear()
        second = self._memo(rundir, wt, calls)
        graph._SNAP_MEMO.pop(str(wt), None)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("junk.tmp", second["untracked"],
                         "global-named target is honoured end to end")

    def test_nosystem_makes_plain_config_certain(self):
        _gitl, wt, rundir, _base = self._work(
            drop=("GIT_CONFIG_SYSTEM",),
            extra={"GIT_CONFIG_NOSYSTEM": "1"})
        self.assertFalse(
            runner.worktree_signature_backstop(wt),
            "skipped system + plain global needs no backstop")
        calls = []
        t0 = 10000.0
        with mock.patch.object(graph, "_mono", return_value=t0):
            first = self._memo(rundir, wt, calls)
        self.assertEqual(len(calls), 1)
        key = str(wt)
        try:
            calls.clear()
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                second = self._memo(rundir, wt, calls)
        finally:
            graph._SNAP_MEMO.pop(key, None)
        self.assertEqual(calls, [],
                         "certain aged entry serves without re-hash")
        self.assertEqual(second, first)

    def test_unlocated_system_keeps_backstop(self):
        _gitl, wt, rundir, _base = self._work(
            drop=("GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"))
        # No config file exists anywhere here; the strict rule keeps
        # the backstop anyway because the build default is unknowable
        # from Python. Machine-independent by construction.
        self.assertTrue(
            runner.worktree_signature_backstop(wt),
            "unlocated system scope must keep the 300 s backstop")
        self.assertIsNotNone(
            runner.worktree_signature(wt),
            "uncertain is bounded, not broken")
        calls = []
        t0 = 10000.0
        with mock.patch.object(graph, "_mono", return_value=t0):
            first = self._memo(rundir, wt, calls)
        self.assertEqual(len(calls), 1)
        key = str(wt)
        try:
            calls.clear()
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                second = self._memo(rundir, wt, calls)
        finally:
            graph._SNAP_MEMO.pop(key, None)
        self.assertEqual(
            len(calls), 1, "uncertain aged entry must re-read")
        self.assertEqual(second, first)

    def test_aging_guard_rejects_unaged_entry(self):
        # F5 guard: the aging helper itself must refuse a pin that
        # does not actually exceed _SNAP_MEMO_TTL past the stored
        # entry, so no aged-memo assertion can pass vacuously.
        _gitl, wt, rundir, _base = self._work()
        calls = []
        t0 = 10000.0
        with mock.patch.object(graph, "_mono", return_value=t0):
            self._memo(rundir, wt, calls)
        key = str(wt)
        try:
            with self.assertRaises(AssertionError):
                _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL - 1.0)
            with _pin_memo_clock(
                    self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                pass
        finally:
            graph._SNAP_MEMO.pop(key, None)

    def test_inline_section_header_key_is_parsed(self):
        # F2: git honours a key on the same line as the section
        # header (probed by Root for both spellings).
        vals, unc = runner._scan_core_excludes(
            "[core] excludesfile = /tmp/zz-inline-target\n")
        self.assertEqual(vals, ["/tmp/zz-inline-target"])
        self.assertFalse(unc)
        vals, unc = runner._scan_core_excludes(
            "[core]excludesfile=/tmp/zz-inline2\n")
        self.assertEqual(vals, ["/tmp/zz-inline2"])
        self.assertFalse(unc)

    def test_unclosed_section_header_is_uncertain(self):
        vals, unc = runner._scan_core_excludes(
            "[core\n\texcludesfile = /tmp/zz-x\n")
        self.assertEqual(vals, [])
        self.assertTrue(
            unc, "a header without ] cannot be parsed exactly")

    def test_header_comment_remainder_ignored(self):
        vals, unc = runner._scan_core_excludes(
            "[core] ; just a comment\n")
        self.assertEqual(vals, [])
        self.assertFalse(unc)

    def test_inline_excludes_in_common_config_rereads(self):
        # F2 integration: the only excludesfile is inline in the
        # worktree's common config; NOSYSTEM=1, isolated GLOBAL.
        # Editing ONLY the target must re-read immediately, on the
        # certain path (no backstop).
        _gitl, wt, rundir, base = self._work(
            drop=("GIT_CONFIG_SYSTEM",),
            extra={"GIT_CONFIG_NOSYSTEM": "1"})
        target = base / "common-inline-ignore"
        target.write_text("# nothing yet\n", encoding="utf-8")
        common_cfg = base / "main" / ".git" / "config"
        with open(common_cfg, "a", encoding="utf-8") as fh:
            fh.write("[core] excludesfile = %s\n" % target)
        (wt / "junk.tmp").write_text("x\n", encoding="utf-8")
        self.assertFalse(
            runner.worktree_signature_backstop(wt),
            "inline excludes resolves on the certain path")
        calls = []
        first = self._memo(rundir, wt, calls)
        self.assertIn("junk.tmp", first["untracked"])
        target.write_text("*.tmp\n", encoding="utf-8")
        calls.clear()
        second = self._memo(rundir, wt, calls)
        graph._SNAP_MEMO.pop(str(wt), None)
        self.assertEqual(
            len(calls), 1,
            "editing only the inline-named target must re-read")
        self.assertNotIn("junk.tmp", second["untracked"],
                         "ignored file must leave the snapshot")

    def test_nosystem_boolean_vocabulary(self):
        for val in ("1", "2", "yes", "YES", "on", "ON", "true",
                    "True", "TRUE", " 1", " 1 ", "+1", "-1", "01"):
            with self.subTest(val=val):
                with mock.patch.dict(os.environ,
                                     {"GIT_CONFIG_NOSYSTEM": val}):
                    self.assertTrue(runner._env_skips_system(), val)
        for val in ("", "0", "00", "false", "False", "FALSE", "no",
                    "off", "foo", "t", "y", "n", "1.5"):
            with self.subTest(val=val):
                with mock.patch.dict(os.environ,
                                     {"GIT_CONFIG_NOSYSTEM": val}):
                    self.assertFalse(runner._env_skips_system(), val)
        with mock.patch.dict(os.environ):
            os.environ.pop("GIT_CONFIG_NOSYSTEM", None)
            self.assertFalse(runner._env_skips_system(),
                             "unset keeps the system scope active")

    def test_unresolvable_env_paths_keep_backstop(self):
        _gitl, wt, _rundir, _base = self._work()
        # F1: the certain control skips the system scope (a set
        # GIT_CONFIG_SYSTEM alone no longer suffices).
        with mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertFalse(
                runner.worktree_signature_backstop(wt),
                "skipped system + located global starts certain")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": ""}):
                self.assertFalse(
                    runner.worktree_signature_backstop(wt),
                    "present-but-empty global reads no file (probed)")
        for var, val in (("GIT_CONFIG_SYSTEM", ""),
                         ("GIT_CONFIG_GLOBAL", "relative/path"),
                         ("GIT_CONFIG_GLOBAL", "~/x"),
                         ("GIT_CONFIG_SYSTEM", "relative/sys"),
                         ("XDG_CONFIG_HOME", "relative/xdg"),
                         ("HOME", "")):
            with self.subTest(var=var, val=val):
                with mock.patch.dict(os.environ, {var: val}):
                    self.assertTrue(
                        runner.worktree_signature_backstop(wt),
                        "unresolvable scope keeps the backstop")

    def test_scope_flip_invalidates_signature(self):
        _gitl, wt, _rundir, base = self._work()
        before = runner.worktree_signature(wt)
        self.assertIsNotNone(before)
        # Both files are absent: only the digested env value moves.
        with mock.patch.dict(os.environ,
                             {"GIT_CONFIG_GLOBAL": str(base / "g2")}):
            flipped = runner.worktree_signature(wt)
        self.assertNotEqual(flipped, before,
                            "global scope flip must invalidate")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1"}):
            skipped = runner.worktree_signature(wt)
        self.assertNotEqual(skipped, before,
                            "system scope flip must invalidate")


_SCOPE_VARS = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
               "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME")


class P1ScopeCacheTest(unittest.TestCase):
    # P1-live(251): process-level git config scope cache. In the
    # default env shape (no GIT_CONFIG_* vars, the live-app shape)
    # a fresh, valid cache locates the system/global/unknown scope
    # files (including Apple's unknown-scope file and plain include
    # targets, which git lists as origins), so aged entries for
    # unchanged worktrees serve with 0 re-hashes. Any failure,
    # timeout, unparseable output, non-file origin, or includeIf in
    # a scope file keeps the 300 s backstop. The scope-listing
    # subprocess is always mocked here (canned outputs); real git
    # in temp dirs only for the worktrees. All aging uses the F5
    # guard helper.

    def setUp(self):
        runner._SCOPE_CACHE = {"at": 0.0, "env": None, "origins": (),
                               "valid": False}

    def tearDown(self):
        runner._SCOPE_CACHE = {"at": 0.0, "env": None, "origins": (),
                               "valid": False}

    def _work(self, extra=None, drop=()):
        tmp = tempfile.TemporaryDirectory(
            prefix="muse251-cache.",
            dir="/tmp/openmainty-supervision-20261018")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        (base / "gitconfig").write_text("", encoding="utf-8")
        (base / "sysconfig").write_text("", encoding="utf-8")
        env = {"HOME": str(home),
               "XDG_CONFIG_HOME": str(base / "xdg"),
               "GIT_CONFIG_GLOBAL": str(base / "gitconfig"),
               "GIT_CONFIG_SYSTEM": str(base / "sysconfig")}
        for key in drop:
            env.pop(key, None)
        if extra:
            env.update(extra)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in drop:
            os.environ.pop(key, None)

        def gitl(*args, cwd):
            proc = subprocess.run(
                ["git", "-C", str(cwd)] + list(args),
                capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise AssertionError(
                    f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
            return proc.stdout

        main = base / "main"
        main.mkdir()
        gitl("init", "-b", "main", cwd=main)
        (main / "code.txt").write_text("v1\n", encoding="utf-8")
        gitl("add", "-A", cwd=main)
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", "init", cwd=main)
        wt = base / "work"
        gitl("worktree", "add", "-b", "feature", str(wt), cwd=main)
        rundir = base / "rundir"
        rundir.mkdir()
        (rundir / "work").symlink_to(wt, target_is_directory=True)
        return gitl, wt, rundir, base

    def _memo(self, rundir, wt, calls):
        run = {"id": "run-p1-cache-01"}
        orig = runner.source_snapshot

        def counting(work, **kw):
            calls.append(str(work))
            return orig(work, **kw)

        with mock.patch.object(store, "run_dir", return_value=rundir), \
                mock.patch.object(runner, "source_snapshot",
                                  side_effect=counting):
            return graph._memo_live_snapshot(run, wt)

    def _many(self, n):
        tmp = tempfile.TemporaryDirectory(
            prefix="muse251-cachemany.",
            dir="/tmp/openmainty-supervision-20261018")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        home = base / "home"
        home.mkdir()
        env = {"HOME": str(home)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _SCOPE_VARS:
            os.environ.pop(key, None)

        def gitl(*args, cwd):
            proc = subprocess.run(
                ["git", "-C", str(cwd)] + list(args),
                capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise AssertionError(
                    f"git {' '.join(args)} failed: {proc.stderr[-300:]}")
            return proc.stdout

        main = base / "main"
        main.mkdir()
        gitl("init", "-b", "main", cwd=main)
        (main / "code.txt").write_text("v1\n" * 200, encoding="utf-8")
        gitl("add", "-A", cwd=main)
        gitl("-c", "user.email=t@localhost", "-c", "user.name=t",
             "commit", "-qm", "init", cwd=main)
        wts = []
        rds = {}
        for i in range(n):
            wt = base / ("wt%03d" % i)
            gitl("worktree", "add", "--detach", str(wt), cwd=main)
            wts.append(wt)
            rd = base / ("rd-%03d" % i)
            rd.mkdir()
            (rd / "work").symlink_to(wt, target_is_directory=True)
            rds["run-p1c-%03d" % i] = rd
        return wts, rds, base

    def test_scope_cache_ttl_matches_memo_ttl(self):
        self.assertEqual(runner._SCOPE_CACHE_TTL, graph._SNAP_MEMO_TTL,
                         "the scope cache window is the memo backstop")

    def test_default_env_cached_origins_serve_aged(self):
        _gitl, wt, rundir, base = self._work(drop=_SCOPE_VARS)
        sysf = base / "sysX"
        unkf = base / "xcodeX"
        sysf.write_text("[core]\n\ta = 1\n", encoding="utf-8")
        unkf.write_text("[core]\n\tb = 2\n", encoding="utf-8")
        out = ("system\tfile:%s\tcore.a\n" % sysf
               + "unknown\tfile:%s\tcore.b\n" % unkf)
        t0 = 30000.0
        with mock.patch.object(
                runner, "_git_config_origins",
                return_value=(0, out)) as origins, \
                mock.patch.object(runner, "_scope_now",
                                  return_value=t0):
            runner.refresh_scope_cache()
            self.assertEqual(origins.call_count, 1)
            self.assertFalse(
                runner.worktree_signature_backstop(wt),
                "fresh valid cache locates the default-env scopes")
            calls = []
            with mock.patch.object(graph, "_mono", return_value=t0):
                first = self._memo(rundir, wt, calls)
            self.assertEqual(len(calls), 1)
            key = str(wt)
            try:
                calls.clear()
                with _pin_memo_clock(
                        self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                    second = self._memo(rundir, wt, calls)
            finally:
                graph._SNAP_MEMO.pop(key, None)
            self.assertEqual(
                calls, [],
                "cached aged entry serves with 0 re-hashes")
            self.assertEqual(second, first)
            self.assertEqual(origins.call_count, 1,
                             "one scope call per window")

    def test_cached_unknown_target_edit_rereads(self):
        _gitl, wt, rundir, base = self._work(drop=_SCOPE_VARS)
        unkf = base / "xcodeX"
        target = base / "xcode-ignore"
        target.write_text("# nothing yet\n", encoding="utf-8")
        unkf.write_text("[core]\n\texcludesfile = %s\n" % target,
                        encoding="utf-8")
        out = "unknown\tfile:%s\tcore.excludesfile\n" % unkf
        (wt / "junk.tmp").write_text("x\n", encoding="utf-8")
        t0 = 30000.0
        with mock.patch.object(
                runner, "_git_config_origins",
                return_value=(0, out)), \
                mock.patch.object(runner, "_scope_now",
                                  return_value=t0):
            runner.refresh_scope_cache()
            self.assertFalse(
                runner.worktree_signature_backstop(wt),
                "cached unknown resolves on the certain path")
            calls = []
            with mock.patch.object(graph, "_mono", return_value=t0):
                first = self._memo(rundir, wt, calls)
            # Mock boundary, documented: real git never reads mocked
            # origins, so the junk file stays visible; coverage is
            # proven by the signature moving on target/file edits.
            self.assertIn("junk.tmp", first["untracked"])
            target.write_text("*.tmp\n", encoding="utf-8")
            calls.clear()
            with mock.patch.object(graph, "_mono", return_value=t0):
                second = self._memo(rundir, wt, calls)
            try:
                self.assertEqual(
                    len(calls), 1,
                    "editing only the cached target must re-read")
                self.assertIn("junk.tmp", second["untracked"])
                unkf.write_text(
                    "[core]\n\texcludesfile = %s\n# c\n" % target,
                    encoding="utf-8")
                calls.clear()
                with mock.patch.object(graph, "_mono", return_value=t0):
                    third = self._memo(rundir, wt, calls)
                self.assertEqual(
                    len(calls), 1,
                    "editing the cached origin itself must re-read")
                self.assertEqual(third["fingerprint"],
                                 second["fingerprint"])
            finally:
                graph._SNAP_MEMO.pop(str(wt), None)

    def test_scope_git_failure_keeps_backstop(self):
        _gitl, wt, rundir, _base = self._work(drop=_SCOPE_VARS)
        t0 = 30000.0
        cases = (("rc1", (1, "", "boom")),
                 ("timeout124", (124, "", "timed out")))
        for label, ret in cases:
            with self.subTest(label):
                runner._SCOPE_CACHE = {"at": 0.0, "env": None,
                                       "origins": (), "valid": False}
                with mock.patch.object(
                        runner, "_git_config_origins",
                        return_value=ret), \
                        mock.patch.object(runner, "_scope_now",
                                          return_value=t0):
                    runner.refresh_scope_cache()
                    self.assertTrue(
                        runner.worktree_signature_backstop(wt),
                        "failed scope call keeps the backstop")
                    calls = []
                    with mock.patch.object(graph, "_mono",
                                           return_value=t0):
                        first = self._memo(rundir, wt, calls)
                    self.assertEqual(len(calls), 1)
                    key = str(wt)
                    try:
                        calls.clear()
                        with _pin_memo_clock(
                                self, key,
                                t0 + graph._SNAP_MEMO_TTL + 1.0):
                            second = self._memo(rundir, wt, calls)
                    finally:
                        graph._SNAP_MEMO.pop(key, None)
                    self.assertEqual(
                        len(calls), 1, "uncertain aged entry re-reads")
                    self.assertEqual(second, first)

    def test_scope_git_exception_keeps_backstop(self):
        _gitl, wt, rundir, _base = self._work(drop=_SCOPE_VARS)
        t0 = 30000.0
        with mock.patch.object(
                runner, "_git_config_origins",
                side_effect=RuntimeError("boom")), \
                mock.patch.object(runner, "_scope_now",
                                  return_value=t0):
            runner.refresh_scope_cache()
            self.assertTrue(
                runner.worktree_signature_backstop(wt),
                "scope exception keeps the backstop")

    def test_includeif_in_cached_origin_keeps_backstop(self):
        _gitl, wt, rundir, base = self._work(drop=_SCOPE_VARS)
        globf = base / "globI"
        globf.write_text('[includeIf "gitdir:/x/**"]\n\tpath = /y\n',
                         encoding="utf-8")
        out = ("global\tfile:%s\t"
               "includeif.gitdir:/x/**.path\n" % globf)
        t0 = 30000.0
        with mock.patch.object(
                runner, "_git_config_origins",
                return_value=(0, out)), \
                mock.patch.object(runner, "_scope_now",
                                  return_value=t0):
            runner.refresh_scope_cache()
            self.assertTrue(
                runner.worktree_signature_backstop(wt),
                "includeIf in a cached origin keeps the backstop")
            calls = []
            with mock.patch.object(graph, "_mono", return_value=t0):
                first = self._memo(rundir, wt, calls)
            self.assertEqual(len(calls), 1)
            key = str(wt)
            try:
                calls.clear()
                with _pin_memo_clock(
                        self, key, t0 + graph._SNAP_MEMO_TTL + 1.0):
                    second = self._memo(rundir, wt, calls)
            finally:
                graph._SNAP_MEMO.pop(key, None)
            self.assertEqual(
                len(calls), 1, "uncertain aged entry re-reads")
            self.assertEqual(second, first)

    def test_new_origin_at_refresh_changes_signature(self):
        _gitl, wt, _rundir, base = self._work(drop=_SCOPE_VARS)
        f1 = base / "o1"
        f2 = base / "o2"
        f1.write_text("[core]\n\ta = 1\n", encoding="utf-8")
        f2.write_text("[core]\n\tb = 2\n", encoding="utf-8")
        out1 = "system\tfile:%s\tcore.a\n" % f1
        out2 = out1 + "unknown\tfile:%s\tcore.b\n" % f2
        s0 = 30000.0
        with mock.patch.object(
                runner, "_git_config_origins",
                side_effect=[(0, out1), (0, out2)]) as origins:
            with mock.patch.object(runner, "_scope_now",
                                   return_value=s0):
                runner.refresh_scope_cache()
                sig_a = runner.worktree_signature(wt)
                self.assertFalse(
                    runner.worktree_signature_backstop(wt))
            later = s0 + runner._SCOPE_CACHE_TTL + 1.0
            with mock.patch.object(runner, "_scope_now",
                                   return_value=later):
                runner.refresh_scope_cache()
                sig_b = runner.worktree_signature(wt)
                self.assertFalse(
                    runner.worktree_signature_backstop(wt))
            self.assertEqual(origins.call_count, 2)
        self.assertIsNotNone(sig_a)
        self.assertNotEqual(sig_b, sig_a,
                            "a new origin changes the signature")

    def test_one_scope_call_per_window_across_96_worktrees(self):
        wts, rds, _base = self._many(96)
        runs = [{"id": "run-p1c-%03d" % i} for i in range(96)]
        t0 = 30000.0
        with mock.patch.object(
                runner, "_git_config_origins",
                return_value=(0, "")) as origins, \
                mock.patch.object(runner, "_scope_now",
                                  return_value=t0), \
                mock.patch.object(graph, "_mono", return_value=t0):
            runner.refresh_scope_cache()
            self.assertFalse(
                runner.worktree_signature_backstop(wts[0]),
                "valid empty cache is certain here")
            calls = []
            orig = runner.source_snapshot

            def counting(work, **kw):
                calls.append(str(work))
                return orig(work, **kw)

            with mock.patch.object(
                    store, "run_dir",
                    side_effect=lambda rid: rds[rid]), \
                    mock.patch.object(
                        runner, "source_snapshot",
                        side_effect=counting):
                [graph._memo_live_snapshot(r, w)
                 for r, w in zip(runs, wts)]
            self.assertEqual(len(calls), 96)
            self.assertEqual(origins.call_count, 1,
                             "one scope call per window")
            for w in wts:
                graph._SNAP_MEMO.pop(str(w), None)


if __name__ == "__main__":
    unittest.main()
