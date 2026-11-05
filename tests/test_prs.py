"""M3.5 read-only PR awareness: scan, link, parse, failure, argv.

Offline only: gh is stubbed, git runs only in fixture dirs under a
temp OPENMAINTY_HOME. Py3.9-safe.
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

from openmainty import batches, cli, prs, repos, sched, store
from openmainty import deps, pcconfig
try:
    from tests._home import use_temp_home, offline_discussions
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_discussions


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    # C2: the Discussions scan source runs on every sched.scan; fixture
    # repos must never reach real gh/network.
    offline_discussions(test)
    return Path(tmp.name)


def pr857():
    """The live #857 shape (human PR closing issue 344)."""
    return prs.sanitize_pr({
        "number": 857, "title": "fix(ios): release audio session on stop",
        "author": {"login": "ryuhzk"}, "isDraft": False,
        "headRefName": "fix-release-audio", "createdAt": "2026-10-23T03:04:00Z",
        "updatedAt": "2026-10-23T03:05:00Z",
        "closingIssuesReferences": [{"number": 344}],
        "additions": 22, "deletions": 2, "changedFiles": 1})


def bot_pr(number, title):
    return prs.sanitize_pr({
        "number": number, "title": title,
        # Live gh shape for dependabot rows.
        "author": {"login": "app/dependabot", "is_bot": True},
        "isDraft": False,
        "headRefName": f"dep-{number}", "createdAt": "2026-10-20T00:00:00Z",
        "updatedAt": "2026-10-20T00:00:00Z",
        "closingIssuesReferences": [],
        "additions": 2, "deletions": 2, "changedFiles": 2})


class DependabotParseTests(unittest.TestCase):
    def test_same_dep_same_target(self):
        self.assertEqual(
            prs.parse_dependabot("chore(deps): bump turbo from 2.10.8 to 2.11.10"),
            {"dep": "turbo", "from": "2.10.8", "to": "2.11.10"})

    def test_scoped_dep(self):
        out = prs.parse_dependabot("bump @babel/runtime from 7.0.0 to 8.0.7")
        self.assertEqual(out["dep"], "@babel/runtime")

    def test_comment_or_url_never_matches(self):
        self.assertIsNone(prs.parse_dependabot(
            "# see turbo 2.11.10 https://example.com/turbo"))
        self.assertIsNone(prs.parse_dependabot("fix(ios): release audio"))

    def test_unparseable_title(self):
        self.assertIsNone(prs.parse_dependabot("chore(deps): weekly update"))
        self.assertIsNone(prs.parse_dependabot(""))


class BotDetectTests(unittest.TestCase):
    def test_bot_logins(self):
        self.assertTrue(prs.is_bot({"login": "dependabot[bot]"}))
        self.assertTrue(prs.is_bot({"login": "renovate[bot]"}))
        self.assertTrue(prs.is_bot({"login": "dependabot"}))
        # Live gh shape: explicit flag plus app/ login.
        self.assertTrue(prs.is_bot({"login": "app/dependabot",
                                    "is_bot": True}))
        self.assertTrue(prs.is_bot({"login": "app/dependabot"}))
        self.assertFalse(prs.is_bot({"login": "ryuhzk",
                                     "is_bot": False}))
        self.assertFalse(prs.is_bot({"login": "ryuhzk"}))
        self.assertFalse(prs.is_bot({}))
        self.assertFalse(prs.is_bot(None))

    def test_sanitize_bounds_untrusted_text(self):
        row = prs.sanitize_pr({"number": 1, "title": "x" * 500,
                               "author": {"login": "y" * 100},
                               "closingIssuesReferences": [{"number": "a"},
                                                           {"number": 7}]})
        self.assertEqual(len(row["title"]), 300)
        self.assertEqual(len(row["author"]), 80)
        self.assertEqual(row["closes"], [7])


class BoardDecisionTests(unittest.TestCase):
    def setUp(self):
        self.closers = {344: pr857()}

    def test_closing_pr_no_run_defers(self):
        self.assertEqual(
            prs.board_decision("start", "344", self.closers, False),
            "skip-external-pr")

    def test_run_exists_keeps_decision(self):
        self.assertEqual(
            prs.board_decision("start", "344", self.closers, True), "start")
        self.assertEqual(
            prs.board_decision("resume", "344", self.closers, False),
            "resume")

    def test_no_closer_keeps_start(self):
        self.assertEqual(
            prs.board_decision("start", "999", self.closers, False), "start")
        self.assertEqual(
            prs.board_decision("start", "nan", self.closers, False), "start")


class PrLinkTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.wt = Path(os.environ["OPENMAINTY_HOME"]) / "runs" / "run-pr1" / "work"
        self.wt.mkdir(parents=True)
        self._git("init")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.wt / "ios.txt").write_text("v1")
        self._git("add", ".")
        self._git("commit", "-m", "base")
        self.base = self._git("rev-parse", "HEAD")
        (self.wt / "ios.txt").write_text("v1\nv2\n")
        self.run = {"id": "run-pr1", "repo": "fakerepo", "issue": "344",
                    "baseCommit": self.base,
                    "created_at": "2026-10-23T00:00:00+00:00",
                    "updated_at": "2026-10-23T00:00:00+00:00"}

    def _git(self, *args):
        proc = subprocess.run(["git", "--no-optional-locks", *args],
                              cwd=str(self.wt), capture_output=True,
                              text=True, timeout=60)
        if args[0] == "rev-parse":
            return proc.stdout.strip()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ""

    def test_external_pr_entry_names_both_with_diffstats(self):
        items, counts = batches._m1_pr_items(
            "fakerepo", [self.run],
            {"last_prs": [pr857()], "prs_error": None, "queue": []},
            1761177600.0)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["kind"], "external-pr")
        self.assertEqual(item["ref"], "run-pr1")
        self.assertIn("#857", item["question"])
        self.assertIn("ryuhzk", item["question"])
        self.assertIn("#344", item["question"])
        self.assertIn("run-pr1", item["question"])
        self.assertIn("PR: +22/-2", item["why"])
        self.assertIn("ios.txt", item["run_stat"])
        self.assertEqual(counts["open"], 1)
        self.assertEqual(counts["external"], 1)
        self.assertEqual(counts["bots"], 0)

    def test_bot_lines_match_and_count(self):
        state = {"last_prs": [pr857(),
                              bot_pr(843, "chore(deps): bump turbo from 2.10.8 to 2.11.10"),
                              bot_pr(844, "chore(deps): bump leftpad from 1.0.0 to 1.0.1")],
                 "prs_error": None,
                 "queue": [{"dep": "turbo", "target": "^2.11.10",
                            "repo": "fakerepo"}]}
        items, counts = batches._m1_pr_items("fakerepo", [], state, 0.0)
        self.assertEqual(items, [])
        self.assertEqual(counts["open"], 3)
        self.assertEqual(counts["bots"], 2)
        notes = {line["pr"]: line["note"] for line in counts["bot_lines"]}
        self.assertIn("같은 목표", notes[843])
        self.assertEqual(notes[844], "1.0.0 -> 1.0.1")

    def test_same_dep_different_target(self):
        state = {"last_prs": [bot_pr(843, "chore(deps): bump turbo from 2.10.8 to 2.11.9")],
                 "prs_error": None,
                 "queue": [{"dep": "turbo", "target": "^2.11.10",
                            "repo": "fakerepo"}]}
        _, counts = batches._m1_pr_items("fakerepo", [], state, 0.0)
        self.assertIn("다른 목표", counts["bot_lines"][0]["note"])
        self.assertIn("^2.11.10", counts["bot_lines"][0]["note"])

    def test_error_counts_show_error_not_zero(self):
        _, counts = batches._m1_pr_items(
            "fakerepo", [], {"last_prs": [], "prs_error": "gh down",
                             "queue": []}, 0.0)
        self.assertEqual(counts["error"], "gh down")


class PrScanTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        (self.co / "package.json").write_text(json.dumps({
            "dependencies": {"left-pad": "1.0.0"}}))
        repos.save({"name": "t1", "github": "e/t1", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        from openmainty import pcconfig as _pc
        pc = _pc.load()
        pc["checkouts"]["t1"] = str(self.co)
        _pc.save(pc)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: "1.1.0")
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def test_closing_pr_skips_triage_no_run_created(self):
        issues = [{"number": 344, "title": "audio session"}]
        prs_raw = [{"number": 857, "title": "fix",
                    "author": {"login": "ryuhzk"}, "isDraft": False,
                    "headRefName": "x", "createdAt": "", "updatedAt": "",
                    "closingIssuesReferences": [{"number": 344}],
                    "additions": 1, "deletions": 1, "changedFiles": 1}]
        with mock.patch("openmainty.cli._open_issues",
                        return_value=issues), \
             mock.patch("openmainty.cli._open_prs", return_value=prs_raw):
            sched.configure("t1", enabled=True, interval_s=60)
            out = sched.scan("t1")
        self.assertEqual(out["prs_deferred"], 1)
        self.assertIn("deferred", out["sources"]["issues"])
        st = sched.load("t1")
        triage = [i for i in st["queue"]
                  if i.get("kind") == "issue-triage"
                  and str(i.get("issue")) == "344"]
        self.assertEqual(triage, [])
        self.assertEqual(len(st["last_prs"]), 1)
        self.assertIsNone(store.latest_run("t1", "344"))

    def test_pr_failure_keeps_last_and_records_error(self):
        with mock.patch("openmainty.cli._open_issues",
                        return_value=[]), \
             mock.patch("openmainty.cli._open_prs",
                        side_effect=RuntimeError("gh down")):
            sched.configure("t1", enabled=True, interval_s=60)
            out = sched.scan("t1")
        self.assertEqual(out["prs_open"], -1)
        self.assertIn("gh down", out["prs_error"])
        st = sched.load("t1")
        self.assertEqual(st["last_prs"], [])
        self.assertIn("gh down", st["prs_error"])
        # A previous good scan's data survives the next failure.
        good = [prs.sanitize_pr({"number": 1, "title": "t",
                                 "author": {"login": "a"}})]
        st["last_prs"] = good
        sched.save(st)
        with mock.patch("openmainty.cli._open_issues",
                        return_value=[]), \
             mock.patch("openmainty.cli._open_prs",
                        side_effect=RuntimeError("gh down")):
            sched.scan("t1")
        st = sched.load("t1")
        self.assertEqual(st["last_prs"], good)


class PrArgvAllowListTests(unittest.TestCase):
    def test_pr_list_argv_exact(self):
        self.assertEqual(
            prs.pr_list_argv("o/r", 30),
            ["gh", "pr", "list", "--repo", "o/r", "--state", "open",
             "--limit", "30", "--json", prs.PR_LIST_FIELDS])

    def test_diffstat_argv_exact(self):
        self.assertEqual(
            prs.GIT_DIFFSTAT_ARGV,
            ["git", "--no-optional-locks", "diff", "--stat"])

    def test_docstring_leads_future_import(self):
        src = (Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "prs.py").read_text(encoding="utf-8")
        self.assertTrue(
            src.startswith('"""'),
            "module docstring must lead the from __future__ import")

    def test_no_other_pr_process_shapes(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "openmainty"
        hits = []
        for path in sorted(src.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if '"pr", "list"' in text or "'pr', 'list'" in text:
                hits.append(path.name)
            if "pull/" in text and "refs/pull" not in text:
                hits.append(path.name + ":pull/")
        self.assertEqual(hits, ["prs.py"])
        prs_src = (src / "prs.py").read_text(encoding="utf-8")
        self.assertEqual(prs_src.count("subprocess.run"), 1)
        cli_src = (src / "cli.py").read_text(encoding="utf-8")
        self.assertIn("prs.pr_list_argv", cli_src)


class DiffstatRefGuardTests(unittest.TestCase):
    """M2.15: work_diffstat takes only a hex sha or HEAD as the ref."""

    def test_safe_ref(self):
        self.assertEqual(prs._safe_ref("HEAD"), "HEAD")
        self.assertEqual(prs._safe_ref("abc1234"), "abc1234")
        self.assertEqual(prs._safe_ref("  0123456789abcdef  "),
                         "0123456789abcdef")
        for bad in ("", "main", "origin/main", "refs/heads/main",
                    "--stat", "-p", "HEAD~1", "abc",
                    "ABC1234", "https://x", "v1.2.3",
                    "abc1234; rm -rf /", "HEAD " + "x" * 50):
            self.assertEqual(prs._safe_ref(bad), "HEAD", bad)

    def test_bad_ref_runs_head(self):
        seen = {}

        def _fake_run(*args, **kwargs):
            seen["argv"] = kwargs.get("args", args[0] if args
                                      else None)
            seen["cwd"] = kwargs.get("cwd")
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = " f.txt | 2 +-\n"
            return proc

        with mock.patch("openmainty.prs.subprocess.run",
                        side_effect=_fake_run):
            out = prs.work_diffstat("/tmp/om66-ward", "evil-branch")
        self.assertIn("f.txt", out)
        self.assertEqual(seen["argv"], prs.GIT_DIFFSTAT_ARGV
                         + ["HEAD"])

    def test_good_sha_passes_through(self):
        seen = {}

        def _fake_run(*args, **kwargs):
            seen["argv"] = kwargs.get("args", args[0] if args
                                      else None)
            proc = mock.Mock()
            proc.returncode = 0
            proc.stdout = " f.txt | 2 +-\n"
            return proc

        with mock.patch("openmainty.prs.subprocess.run",
                        side_effect=_fake_run):
            prs.work_diffstat("/tmp/om66-ward", "abc1234")
        self.assertEqual(seen["argv"], prs.GIT_DIFFSTAT_ARGV
                         + ["abc1234"])


if __name__ == "__main__":
    unittest.main()
