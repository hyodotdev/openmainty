"""Restart-cold (261): persistent snapshot cache + honest loading state.

The first /api/graph after a dashboard restart takes ~24 s on live
data because the in-memory per-worktree memo starts empty: every
worktree re-hashes (3 git subprocesses each). This file pins the
fix with real git in isolated homes (scope-listing subprocess always
mocked, never the real home):

(a) restart with a valid disk cache: unchanged worktrees cost 0
    per-worktree git calls (certain path, both env shapes);
(b) a changed worktree recomputes and its disk entry updates;
(c) corrupt / partial / version-mismatched / misshapen cache:
    recompute, nothing green from disk;
(d) unreadable source stays unknown after a restart even with a
    stale green entry on disk;
(e) no prewarm is added (no test; the disk seed covers restart-cold
    and a background thread buys nothing on the measured path);
(f) P1/P1-live/259 suites stay unchanged and green (existing files).
Plus TTL parity for excludes-uncertain entries across restarts,
store hygiene (cap, atomic replace, never breaks a build), and the
graph loading-state UI (elapsed seconds, real functions under node).
Deterministic: subprocess/call counts, never wall-clock assertions.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "openmainty" / "dashboard.html"
BASEDIR = "/tmp/openmainty-supervision-20261018"

sys.path.insert(0, str(ROOT / "src"))

from openmainty import graph, paths, repos, runner, store  # noqa: E402

try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


# No test in this module may run the real scope-listing git
# subprocess (it would read machine-local scope files outside the
# isolated fixtures). Fail it module-wide; tests needing the
# certain default-env path nest their own canned outputs.
_scope_patcher = mock.patch.object(
    runner, "_git_config_origins", return_value=(1, "", "disabled"))
_scope_patcher.start()


def _reset_memo_state():
    runner._SCOPE_CACHE = {"at": 0.0, "env": None, "origins": (),
                           "valid": False}
    graph._SNAP_MEMO.clear()
    graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}


def _iso(test, env=None, drop=("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                               "GIT_CONFIG_NOSYSTEM", "XDG_CONFIG_HOME")):
    """Isolated HOME + OPENMAINTY_HOME; default live env shape."""
    tmp = tempfile.TemporaryDirectory(prefix="muse261-.", dir=BASEDIR)
    test.addCleanup(tmp.cleanup)
    base = Path(tmp.name)
    (base / "home").mkdir()
    use_temp_home(test, str(base / "omh"))
    vals = {"HOME": str(base / "home")}
    vals.update(env or {})
    patcher = mock.patch.dict(os.environ, vals)
    patcher.start()
    test.addCleanup(patcher.stop)
    for var in drop:
        if var not in vals:
            os.environ.pop(var, None)
    _reset_memo_state()
    test.addCleanup(_reset_memo_state)
    return base


def _git(*args, cwd):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@localhost",
         "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
        + list(args), cwd=str(cwd), capture_output=True, text=True,
        timeout=60)
    assert proc.returncode == 0, "git %s failed: %s" % (args, proc.stderr)
    return proc.stdout


def _make_profile(name="t1"):
    repos.save({"name": name, "github": "example/%s" % name,
                "baseBranch": "main", "knowledge": [], "checks": [],
                "e2e": [], "release": {}, "announce": {}})


def _seed_runs(n, repo="t1"):
    """Verified runs with real git worktrees; returns [(state, work)]."""
    runs = []
    for i in range(n):
        st = store.create(repo, str(700 + i), "cold run %02d" % i,
                          "cbase%02d" % i, {"worker": "t261"})
        st["steps"] = {"reproduce": {"status": "done", "exit_code": 0}}
        work = store.run_dir(st["id"]) / "work"
        work.mkdir(parents=True, exist_ok=True)
        _git("init", cwd=work)
        (work / "a.txt").write_text("run %d\n" % i, encoding="utf-8")
        (work / "b.txt").write_text("more %d\n" % i, encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "init %d" % i, cwd=work)
        head = _git("rev-parse", "HEAD", cwd=work).strip()
        fp = runner.source_snapshot(work)["fingerprint"]
        st["workCommit"] = head
        st["verifiedFingerprints"] = {"reproduce": fp}
        st["verifiedCommits"] = {"reproduce": head}
        store.save(st)
        runs.append((st, work))
    return runs


def _counting_build(repo):
    """graph.build(repo) with source_snapshot calls recorded."""
    calls = []
    orig = runner.source_snapshot

    def counting(work):
        calls.append(str(work))
        return orig(work)

    with mock.patch.object(runner, "source_snapshot",
                           side_effect=counting):
        out = graph.build(repo)
    return out, calls


def _verify_ids(out):
    return {n["id"] for n in out["nodes"] if n.get("type") == "verification"}


def _disk_path():
    return paths.home() / "cache" / graph._SNAP_DISK_NAME


class DiskPersistTest(unittest.TestCase):
    # A build persists the memo it filled: versioned file, one entry
    # per memoised worktree, no temp file left behind.

    def test_build_writes_versioned_cache(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        _seed_runs(3)
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        self.assertFalse(_disk_path().exists(), "nothing before a build")
        out = graph.build("t1")
        self.assertEqual(len(_verify_ids(out)), 3)
        raw = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertEqual(raw["v"], graph._SNAP_DISK_VERSION)
        self.assertEqual(len(raw["entries"]), 3)
        for key, entry in raw["entries"].items():
            self.assertTrue(key, "keyed by work path")
            self.assertTrue(entry["sig"], "entry keeps its signature")
            self.assertEqual(entry["snap"]["kind"], "git")
            self.assertTrue(entry["snap"]["fingerprint"])
            self.assertIsInstance(entry["wall"], float)
        leftovers = list(_disk_path().parent.glob("*.tmp*"))
        self.assertEqual(leftovers, [], "atomic replace leaves no tmp")


class RestartZeroCallsTest(unittest.TestCase):
    # (a) restart with a valid cache: unchanged worktrees cost 0
    # per-worktree git calls; identical graph output.

    def _restart_roundtrip(self):
        _make_profile()
        runs = _seed_runs(3)
        first, prime_calls = _counting_build("t1")
        self.assertEqual(len(prime_calls), 3, "cold prime reads each once")
        self.assertEqual(len(_verify_ids(first)), 3)
        # Simulate a process restart: memory gone, disk kept.
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        with mock.patch.object(runner, "run_cmd",
                               side_effect=AssertionError(
                                   "no subprocess after restart")) as rc:
            second, calls = _counting_build("t1")
        self.assertEqual(calls, [], "0 source_snapshot calls on restart")
        self.assertEqual(rc.call_count, 0, "0 subprocesses on restart")
        self.assertEqual(_verify_ids(second), _verify_ids(first))
        self.assertEqual(
            {n["id"]: n.get("status") for n in second["nodes"]},
            {n["id"]: n.get("status") for n in first["nodes"]})
        return runs

    def test_certain_nosystem_shape(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        self._restart_roundtrip()

    def test_default_env_shape_with_canned_scope(self):
        base = _iso(self)
        sysf = base / "sys.conf"
        sysf.write_text("[core]\n\ta = 1\n", encoding="utf-8")
        out = "system\tfile:%s\tcore.a\n" % sysf
        t0 = 30000.0
        with mock.patch.object(runner, "_git_config_origins",
                               return_value=(0, out)), \
                mock.patch.object(runner, "_scope_now",
                                  return_value=t0):
            self._restart_roundtrip()


class ChangedWorktreeTest(unittest.TestCase):
    # (b) a changed worktree recomputes after a restart (new bytes
    # served, never the stale disk entry); the rest cost 0; the
    # next build refreshes that worktree's disk entry.

    def test_dirty_edit_recomputes_only_that_worktree(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(3)
        graph.build("t1")
        old_fps = [runner.worktree_signature(w) for _s, w in runs]
        (runs[1][1] / "a.txt").write_text("run 1 edited\n",
                                          encoding="utf-8")
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        out, calls = _counting_build("t1")
        self.assertEqual(calls, [str(runs[1][1])],
                         "only the edited worktree re-hashes")
        new_fp = runner.source_snapshot(runs[1][1])["fingerprint"]
        served = graph._SNAP_MEMO[str(runs[1][1])][1]["fingerprint"]
        self.assertEqual(served, new_fp, "fresh bytes served, not stale")
        self.assertEqual(len(_verify_ids(out)), 2,
                         "edited run drops its proof until re-verified")
        disk = json.loads(_disk_path().read_text(encoding="utf-8"))
        entry = disk["entries"][str(runs[1][1])]
        self.assertEqual(entry["sig"], runner.worktree_signature(runs[1][1]))
        self.assertNotEqual(entry["sig"], old_fps[1],
                            "disk entry refreshed to the new signature")


class CorruptCacheTest(unittest.TestCase):
    # (c) corrupt / partial / version-mismatched / misshapen cache:
    # every worktree recomputes and correct data is served — nothing
    # green comes from the disk file.

    def _primed(self, n=2):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(n)
        first = graph.build("t1")
        self.assertEqual(len(_verify_ids(first)), n)
        self.assertTrue(_disk_path().exists(), "prime must persist")
        return runs, first

    def _restart_counts(self):
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        return _counting_build("t1")

    def test_garbage_bytes_recompute(self):
        runs, first = self._primed()
        _disk_path().write_bytes(b"\x00\x01not json{{{{")
        out, calls = self._restart_counts()
        self.assertEqual(sorted(calls), sorted(str(w) for _s, w in runs))
        self.assertEqual(_verify_ids(out), _verify_ids(first))

    def test_truncated_json_recomputes(self):
        runs, first = self._primed()
        raw = _disk_path().read_text(encoding="utf-8")
        _disk_path().write_text(raw[: len(raw) // 2], encoding="utf-8")
        out, calls = self._restart_counts()
        self.assertEqual(len(calls), 2, "partial file is never trusted")
        self.assertEqual(_verify_ids(out), _verify_ids(first))

    def test_version_mismatch_recomputes_then_rewrites(self):
        runs, first = self._primed()
        raw = json.loads(_disk_path().read_text(encoding="utf-8"))
        raw["v"] = 9999
        _disk_path().write_text(json.dumps(raw), encoding="utf-8")
        out, calls = self._restart_counts()
        self.assertEqual(len(calls), 2, "future version is never trusted")
        self.assertEqual(_verify_ids(out), _verify_ids(first))
        fixed = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertEqual(fixed["v"], graph._SNAP_DISK_VERSION,
                         "next build rewrites the current version")

    def test_misshapen_entries_recompute(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(2)
        graph.build("t1")
        sig0 = runner.worktree_signature(runs[0][1])
        bad = {"v": graph._SNAP_DISK_VERSION,
               "entries": {
                   str(runs[0][1]): {"sig": sig0, "snap": [1, 2],
                                     "wall": time.time()},
                   str(runs[1][1]): {"sig": "", "snap": {
                       "kind": "git", "head": "h", "fingerprint": "",
                       "files": 1, "untracked": []}, "wall": "now"}}}
        _disk_path().write_text(json.dumps(bad), encoding="utf-8")
        out, calls = self._restart_counts()
        self.assertEqual(len(calls), 2, "misshapen entries never serve")
        self.assertEqual(len(_verify_ids(out)), 2)

    def test_wrong_signature_never_served(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(1)
        graph.build("t1")
        raw = json.loads(_disk_path().read_text(encoding="utf-8"))
        key = str(runs[0][1])
        raw["entries"][key]["sig"] = "0" * 64
        raw["entries"][key]["snap"]["fingerprint"] = "f" * 64
        _disk_path().write_text(json.dumps(raw), encoding="utf-8")
        out, calls = self._restart_counts()
        self.assertEqual(calls, [key], "sig mismatch recomputes")
        self.assertEqual(len(_verify_ids(out)), 1,
                         "recomputed proof, not the planted fingerprint")


class UnreadableAfterRestartTest(unittest.TestCase):
    # (d) unreadable source stays unknown after a restart even when
    # a stale green entry for it exists on disk.

    def test_unreadable_head_reports_unknown(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(2)
        graph.build("t1")
        key = str(runs[0][1])
        raw = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertIn(key, raw["entries"],
                      "stale green entry exists (test is not vacuous)")
        head = runs[0][1] / ".git" / "HEAD"
        os.chmod(head, 0)
        self.addCleanup(os.chmod, head, 0o644)
        self.assertIsNone(runner.worktree_signature(runs[0][1]),
                          "unreadable HEAD has no signature")
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        snap = graph._memo_live_snapshot(runs[0][0], runs[0][1])
        self.assertIsNone(snap, "unknown, never the stale green entry")
        self.assertNotIn(key, graph._SNAP_MEMO, "nothing memoised")
        out = graph.build("t1")
        self.assertEqual(len(_verify_ids(out)), 1,
                         "only the readable run keeps its proof")
        self.assertNotIn("verify:%s:reproduce" % runs[0][0]["id"],
                         _verify_ids(out))


class UncertainTtlAcrossRestartTest(unittest.TestCase):
    # Excludes-uncertain entries keep their TTL across restarts:
    # aged entries re-read, fresh ones hit (memory parity), and a
    # future-dated entry (clock moved back) re-reads fail-closed.

    def test_aged_fresh_and_future_entries(self):
        base = _iso(self)
        (base / "home" / ".gitconfig").write_text(
            '[includeIf "gitdir:/x/**"]\n\tpath = /y\n', encoding="utf-8")
        _make_profile()
        runs = _seed_runs(1)
        work = runs[0][1]
        key = str(work)
        graph.build("t1")
        self.assertTrue(runner.worktree_signature_backstop(work),
                        "includeIf keeps this worktree uncertain")
        ttl = graph._SNAP_MEMO_TTL

        def _set_wall(wall):
            raw = json.loads(_disk_path().read_text(encoding="utf-8"))
            raw["entries"][key]["wall"] = wall
            _disk_path().write_text(json.dumps(raw), encoding="utf-8")

        def _restart_build():
            graph._SNAP_MEMO.clear()
            graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
            return _counting_build("t1")

        aged = time.time() - ttl - 1.0
        self.assertGreater(time.time() - aged, ttl,
                           "F5 guard: the aged entry really exceeds the TTL")
        _set_wall(aged)
        out, calls = _restart_build()
        self.assertEqual(calls, [key], "aged uncertain entry re-reads")
        self.assertEqual(len(_verify_ids(out)), 1)

        _set_wall(time.time() - 10.0)
        out, calls = _restart_build()
        self.assertEqual(calls, [], "fresh uncertain entry hits in-window")
        self.assertEqual(len(_verify_ids(out)), 1)

        _set_wall(time.time() + 3600.0)
        out, calls = _restart_build()
        self.assertEqual(calls, [key],
                         "future-dated entry re-reads (clock moved back)")
        self.assertEqual(len(_verify_ids(out)), 1)


class DiskStoreHygieneTest(unittest.TestCase):
    # The store caps entries, merges without evicting unvisited ones,
    # and never breaks a build when the home is unwritable.

    def test_entries_capped(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        snap = {"kind": "git", "head": "h", "fingerprint": "f" * 64,
                "files": 1, "untracked": []}
        now = time.monotonic()
        for i in range(graph._SNAP_MEMO_CAP + 30):
            graph._SNAP_MEMO["/w/%d" % i] = ("s%d" % i, dict(snap), now)
        graph._snap_disk_store()
        raw = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertLessEqual(len(raw["entries"]), graph._SNAP_MEMO_CAP)

    def test_unvisited_disk_entries_survive_a_build(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(2)
        graph.build("t1")
        stale = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertEqual(len(stale["entries"]), 2)
        # A store with an empty memory (e.g. a filtered build that
        # visits nothing new) must not wipe the disk seed.
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        graph._snap_disk_store()
        merged = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertEqual(len(merged["entries"]), 2,
                         "store merges; it never drops disk-only entries")

    def test_unwritable_home_never_breaks_build(self):
        base = _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        blocker = base / "file-home"
        blocker.write_text("not a dir", encoding="utf-8")
        use_temp_home(self, str(blocker))
        graph._snap_disk_store()  # must not raise
        out = graph.build("")
        self.assertIn("nodes", out)


class DiskWriteSkippingTest(unittest.TestCase):
    # The seed is rewritten only when signatures or snapshots
    # actually change: consecutive quiet builds cost zero writes.
    # Tmp files are unique per writer (pid + random suffix) and a
    # failed replace removes only the tmp just created.

    def _counted_store_builds(self, builds):
        calls = []
        orig = graph._snap_disk_replace

        def rec(src, dst):
            calls.append((str(src), str(dst)))
            return orig(src, dst)

        with mock.patch.object(graph, "_snap_disk_replace",
                               side_effect=rec):
            outs = [graph.build("t1") for _ in range(builds)]
        return outs, calls

    def test_consecutive_builds_write_once_then_skip(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        _seed_runs(2)
        _outs, calls = self._counted_store_builds(2)
        self.assertEqual(len(calls), 1,
                         "first build writes, quiet rebuild skips")
        tmp, dst = calls[0]
        self.assertEqual(dst, str(_disk_path()))
        self.assertIn(str(os.getpid()), os.path.basename(tmp),
                      "tmp name carries the writer pid")
        self.assertEqual(list(_disk_path().parent.glob("*.tmp")), [],
                         "no stray tmp after a successful store")

    def test_changed_worktree_writes_once_more(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(2)
        graph.build("t1")
        (runs[0][1] / "a.txt").write_text("edited\n", encoding="utf-8")
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        _outs, calls = self._counted_store_builds(1)
        self.assertEqual(len(calls), 1,
                         "a signature change triggers one more write")

    def test_failed_replace_cleans_only_its_tmp(self):
        _iso(self, env={"GIT_CONFIG_NOSYSTEM": "1"})
        _make_profile()
        runs = _seed_runs(2)
        graph.build("t1")
        before = json.loads(_disk_path().read_text(encoding="utf-8"))
        (runs[0][1] / "a.txt").write_text("edited\n", encoding="utf-8")
        graph._SNAP_MEMO.clear()
        graph._SNAP_DISK_LOADED = {"key": None, "entries": {}}
        decoy = _disk_path().parent / "unrelated.tmp"
        decoy.write_text("not mine", encoding="utf-8")
        with mock.patch.object(graph, "_snap_disk_replace",
                               side_effect=OSError("disk gone")):
            self.assertFalse(self._store_after_reread(runs),
                             "failed store reports False, never raises")
        after = json.loads(_disk_path().read_text(encoding="utf-8"))
        self.assertEqual(after, before, "failed replace keeps old seed")
        self.assertTrue(decoy.exists(), "only the own tmp is removed")
        leftovers = [p for p in _disk_path().parent.glob("*.tmp")
                     if p != decoy]
        self.assertEqual(leftovers, [], "own tmp removed on failure")

    def _store_after_reread(self, runs):
        # Re-read the edited worktree into memory so the store has a
        # genuine content change to attempt (else it skips by design).
        graph._memo_live_snapshot(runs[0][0], runs[0][1])
        graph._memo_live_snapshot(runs[1][0], runs[1][1])
        return graph._snap_disk_store()


def _script():
    text = HTML.read_text()
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, "expected one inline dashboard script"
    return blocks[0]


def _balanced(src, i):
    """Balanced-brace body starting at src[i] == '{'."""
    depth, q, line, block, esc = 0, None, False, False, False
    for j in range(i, len(src)):
        c = src[j]
        n = src[j + 1] if j + 1 < len(src) else ""
        if line:
            if c == "\n":
                line = False
            continue
        if block:
            if c == "*" and n == "/":
                block = False
            continue
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
            continue
        if c == "/" and n == "/":
            line = True
            continue
        if c == "/" and n == "*":
            block = True
            continue
        if c in ("'", '"', "`"):
            q = c
            continue
        if c == "{":
            depth += 1
        if c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced braces")


def _fn_body(src, name):
    """Balanced-brace body of a top-level (async) function."""
    m = re.search(r"(?:^|\n)(?:async\s+)?function\s+" + re.escape(name)
                  + r"\s*\(", src)
    assert m, f"missing function {name}"
    return _balanced(src, src.index("{", m.start()))


def _decl(src, name):
    m = re.search(
        r"(?:^|\n)((?:async\s+)?function\s+" + re.escape(name)
        + r"\s*\([^)]*\))",
        src,
    )
    assert m, f"missing function {name}"
    return m.group(1) + " " + _fn_body(src, name)


def _run_node(script):
    return subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _const_line(src, name):
    m = re.search(r"(?:^|\n)const\s+" + re.escape(name)
                  + r"\s*=\s*[^;]+;", src)
    assert m, f"missing const {name}"
    return m.group(0).strip()


class GraphLoadingStateTest(unittest.TestCase):
    # While the first graph fetch is in flight the status line shows
    # honest elapsed seconds ("그래프 준비 중 · 12초"), not a frozen
    # label, while the live region hears one stable text and the
    # digits stay aria-hidden. Wiring pinned structurally (start on
    # fetch, stop on every settle path); behavior proven by
    # GraphLoadTickBehaviorTest.

    def test_load_tick_wiring(self):
        # Steer 2 redesign: the live region hears one stable text;
        # the per-second digits live in an aria-hidden span. Pins the
        # wiring; GraphLoadTickBehaviorTest proves the behavior.
        src = _script()
        start = _fn_body(src, "startGraphLoadTick")
        self.assertIn("setInterval", start, "ticks on a 1 s interval")
        self.assertIn("1000", start)
        self.assertIn("setGraphStatus(G_LOAD_BUSY)", start.replace(" ", ""),
                      "start announces the stable text once")
        self.assertIn("graphLoadDigitEl()", start,
                      "digits render through the hidden span")
        self.assertIn("graphLoadSecsText", start,
                      "digits use the pure secs formatter")
        self.assertIn("gGraphFetch", start,
                      "a replaced fetch record stops the stale tick")
        self.assertIn("G_LOAD_LONG", start, "terminal wording past 300 s")
        self.assertIn("G_LOAD_OVER", start, "terminal digits")
        self.assertNotIn("setGraphStatus(graphLoadSecsText",
                         start.replace(" ", ""),
                         "ticks never rewrite the live region per second")
        digit = _fn_body(src, "graphLoadDigitEl")
        self.assertIn('data-gloadsecs', digit, "span marker")
        self.assertIn('aria-hidden', digit, "digits hidden from AT")
        stop = _fn_body(src, "stopGraphLoadTick")
        self.assertIn("clearInterval", stop, "stop clears the timer")
        view = _fn_body(src, "renderGraphView")
        self.assertIn("startGraphLoadTick(myLoad)", view,
                      "fetch start arms the elapsed readout")
        self.assertGreaterEqual(view.count("stopGraphLoadTick(myLoad)"), 2,
                                "success and error paths both stop it")
        self.assertNotIn('setGraphStatus("불러오는 중…")', view,
                         "no redundant pre-fetch announcement")
        self.assertIn("loadTimer", src,
                      "the timer rides the in-flight fetch record")


class GraphLoadSecsFormatterTest(unittest.TestCase):
    # The per-second digits are a pure format (" · N초") rendered in
    # an aria-hidden span; the live region itself hears only the
    # stable texts. Real function and consts under node.

    def test_secs_formatter_and_stable_texts(self):
        src = _script()
        consts = "\n".join(_const_line(src, n) for n in
                           ("G_LOAD_BUSY", "G_LOAD_LONG", "G_LOAD_OVER"))
        driver = consts + "\n" + _decl(src, "graphLoadSecsText") + r'''
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; console.log("PASS " + msg); }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
eq(graphLoadSecsText(0), " · 0초", "zero seconds");
eq(graphLoadSecsText(1), " · 1초", "one second");
eq(graphLoadSecsText(12), " · 12초", "twelve seconds");
eq(graphLoadSecsText(61), " · 61초", "long cold loads stay in seconds");
eq(graphLoadSecsText(1.9), " · 1초", "fractions floor");
eq(graphLoadSecsText(-3), " · 0초", "negatives clamp to zero");
eq(G_LOAD_BUSY, "그래프 준비 중", "stable live text");
eq(G_LOAD_LONG, "그래프 준비가 오래 걸리고 있습니다", "terminal live text");
eq(G_LOAD_OVER, " · 5분 넘음", "terminal digits stay true without ticking");
'''
        proc = _run_node(driver)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"formatter failures: {proc.stdout}")
        self.assertIn("PASS", proc.stdout, "no passing assertions")


_TICK_FAKES = r'''
let nowMs = 1000000;
Date.now = () => nowMs;
let nextTimerId = 1;
const liveTimers = new Map();
const clearedTimerIds = [];
function setInterval(fn, ms) { const id = nextTimerId++; liveTimers.set(id, {fn, ms}); return id; }
function clearInterval(id) { clearedTimerIds.push(id); liveTimers.delete(id); }
function runTimers() { for (const e of Array.from(liveTimers.values())) e.fn(); }
var gGraphFetch = null;
const liveWrites = [];
function makeSpan() {
  return { tag: "span", _attrs: {}, textContent: "",
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return (k in this._attrs) ? this._attrs[k] : null; } };
}
function makeStatusEl() {
  return { _text: "", _kids: [],
    get textContent() { return this._text; },
    set textContent(v) { this._text = v; this._kids = []; liveWrites.push(v); },
    querySelector(sel) {
      if (sel !== "[data-gloadsecs]") return null;
      return this._kids.length ? this._kids[0] : null;
    },
    appendChild(k) { this._kids.push(k); return k; } };
}
var statusEl = makeStatusEl();
function $(id) { return id === "gstatus" ? statusEl : null; }
function setGraphStatus(msg) { const el = $("gstatus"); if (el) el.textContent = msg; }
var document = { createElement(tag) { return makeSpan(); } };
function visibleLine() {
  const span = statusEl.querySelector("[data-gloadsecs]");
  return statusEl._text + (span ? span.textContent : "");
}
let pass = 0;
function eq(a, b, msg) {
  if (a === b) { pass++; }
  else { console.log("FAIL " + msg + " :: " + JSON.stringify(a) + " !== " + JSON.stringify(b)); process.exitCode = 1; }
}
function ok(c, msg) { eq(!!c, true, msg); }
'''


class GraphLoadTickBehaviorTest(unittest.TestCase):
    # Real start/stop/span functions under node with fake timers, a
    # fake clock and a recording status element: sighted users see
    # the per-second counter while the live region hears almost
    # nothing; settle and replacement always stop the tick.

    def _run_checks(self, checks):
        src = _script()
        consts = "\n".join(_const_line(src, n) for n in
                           ("G_LOAD_BUSY", "G_LOAD_LONG", "G_LOAD_OVER"))
        decls = "\n".join(_decl(src, n) for n in
                          ("graphLoadSecsText", "graphLoadDigitEl",
                           "startGraphLoadTick", "stopGraphLoadTick"))
        proc = _run_node(consts + "\n" + decls + "\n"
                         + _TICK_FAKES + "\n" + checks)
        self.assertEqual(proc.returncode, 0,
                         f"node driver failed: {proc.stdout}\n{proc.stderr}")
        self.assertNotIn("FAIL", proc.stdout,
                         f"tick behavior failures: {proc.stdout}")

    def test_start_tick_and_quiet_live_region(self):
        self._run_checks(r'''
// (a) start shows 0 s with a single live write.
var recA = {repo: "", promise: {}};
gGraphFetch = recA;
startGraphLoadTick(recA);
eq(visibleLine(), "그래프 준비 중 · 0초", "(a) start shows 0 s");
eq(liveWrites.length, 1, "(a) one live-region write at start");
eq(liveWrites[0], "그래프 준비 중", "(a) the live text is the stable part");
var spanA = statusEl.querySelector("[data-gloadsecs]");
ok(spanA && spanA.getAttribute("aria-hidden") === "true", "(a) digits are aria-hidden");
// (b) N fake ticks show N s, no new live writes.
nowMs += 5000; runTimers();
eq(visibleLine(), "그래프 준비 중 · 5초", "(b) five fake ticks show 5 s");
eq(liveWrites.length, 1, "(b) ticks add no live writes");
// (c) 25 s sim: almost-silent live region, advancing counter.
nowMs += 20000; for (let i = 0; i < 20; i++) runTimers();
ok(liveWrites.length <= 4, "(c) 25 s sim costs at most 4 live writes, got " + liveWrites.length);
ok(liveWrites.length < 25, "(c) never one per second");
eq(visibleLine(), "그래프 준비 중 · 25초", "(c) visible counter still advances");
// (d) replacing gGraphFetch stops the old tick.
var oldTimer = recA.loadTimer;
gGraphFetch = {repo: "", promise: {}};
runTimers();
ok(clearedTimerIds.indexOf(oldTimer) !== -1, "(d) stale tick clears its interval");
eq(recA.loadTimer, null, "(d) stale record releases its timer");
var writesBefore = liveWrites.length;
runTimers();
eq(liveWrites.length, writesBefore, "(d) stopped tick writes nothing more");
console.log("PASS tickABC");
''')

    def test_settle_terminal_and_rejoin(self):
        self._run_checks(r'''
// (e) success and error settle both clear the interval (same stop).
var recE = {repo: "", promise: {}};
gGraphFetch = recE; statusEl = makeStatusEl(); liveWrites.length = 0;
startGraphLoadTick(recE);
var timerE1 = recE.loadTimer;
stopGraphLoadTick(recE);
ok(clearedTimerIds.indexOf(timerE1) !== -1, "(e) success settle clears the interval");
startGraphLoadTick(recE);
var timerE2 = recE.loadTimer;
ok(timerE2 !== timerE1, "(e) a new fetch arms a fresh interval");
stopGraphLoadTick(recE);
ok(clearedTimerIds.indexOf(timerE2) !== -1, "(e) error settle clears the interval too");
// (f) past 300 s: honest terminal wording, then silence.
var recF = {repo: "", promise: {}};
gGraphFetch = recF; statusEl = makeStatusEl(); liveWrites.length = 0;
startGraphLoadTick(recF);
var timerF = recF.loadTimer;
nowMs += 301000; runTimers();
eq(visibleLine(), "그래프 준비가 오래 걸리고 있습니다 · 5분 넘음", "(f) terminal wording past 300 s");
ok(liveWrites.indexOf("그래프 준비가 오래 걸리고 있습니다") !== -1, "(f) one coarse live update");
ok(clearedTimerIds.indexOf(timerF) !== -1, "(f) terminal path stops the timer");
var wBefore = liveWrites.length, vBefore = visibleLine();
nowMs += 120000; runTimers();
eq(liveWrites.length, wBefore, "(f) frozen line writes nothing more");
eq(visibleLine(), vBefore, "(f) terminal wording stays honest without ticking");
// (g) rejoin keeps the original t0 and arms no second interval.
var recG = {repo: "", promise: {}};
gGraphFetch = recG; statusEl = makeStatusEl(); liveWrites.length = 0;
var timersBefore = liveTimers.size;
startGraphLoadTick(recG);
eq(liveTimers.size, timersBefore + 1, "(g) first start arms one interval");
var timerG = recG.loadTimer;
nowMs += 7000;
startGraphLoadTick(recG);
eq(liveTimers.size, timersBefore + 1, "(g) rejoin arms no second interval");
eq(recG.loadTimer, timerG, "(g) rejoin keeps the original timer");
eq(visibleLine(), "그래프 준비 중 · 7초", "(g) rejoin shows honest elapsed from the original t0");
console.log("PASS tickDEF");
''')
