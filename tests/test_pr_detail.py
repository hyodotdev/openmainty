"""PR detail collection: argv pinning, real-shape parsing, coherence.

Offline: gh/network fail loudly unless a test installs a canned boundary
mock. No real home use outside the test's own temp HOME.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import pr_detail as p


HEAD40 = "a" * 40
BASE40 = "b" * 40


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    prev = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(Path(tmp.name) / "home")
    test.addCleanup(lambda: os.environ.pop("OPENMAINTY_HOME", None)
                    if prev is None else os.environ.__setitem__(
                        "OPENMAINTY_HOME", prev))

    def _no_network(*args, **kwargs):
        raise AssertionError("unexpected gh/network call in offline test")

    net = mock.patch.object(p._util, "run_cmd", side_effect=_no_network)
    net.start()
    test.addCleanup(net.stop)
    return Path(tmp.name)


def view_row(**kw):
    row = {
        "number": 857, "title": "fix(ios): release audio session on stop",
        "body": "Stops the session.",
        "author": {"login": "contributor"},
        "state": "OPEN", "isDraft": False,
        "createdAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-20T00:00:00Z",
        "closedAt": None, "mergedAt": None,
        "headRefName": "fix-stop", "headRefOid": HEAD40,
        "baseRefName": "main", "baseRefOid": BASE40,
        "url": "https://github.com/o/r/pull/857",
        "labels": [{"name": "iOS"}, {"name": "bug"}],
        "comments": [{"author": {"login": "a"}, "body": "c1",
                      "createdAt": "2026-10-02T00:00:00Z",
                      "updatedAt": "2026-10-02T00:00:00Z",
                      "url": "https://github.com/o/r/pull/857#issue-1"}],
        "reviews": [{"author": {"login": "r"}, "body": "lgtm",
                     "state": "APPROVED",
                     "createdAt": "2026-10-03T00:00:00Z",
                     "updatedAt": "2026-10-03T00:00:00Z",
                     "url": "https://github.com/o/r/pull/857#review-1"}],
        "closingIssuesReferences": [{"number": 344}],
        "additions": 10, "deletions": 2, "changedFiles": 1,
        "files": [{"path": "src/a.ts", "additions": 10,
                   "deletions": 2}],
    }
    row.update(kw)
    return row


class TestArgvAllowList(unittest.TestCase):
    def test_view_argv_pinned(self):
        self.assertEqual(
            p.pr_view_argv("o/r", 857),
            ["gh", "pr", "view", "--repo", "o/r", "857", "--json",
             p.PR_VIEW_FIELDS])

    def test_diff_argv_pinned(self):
        self.assertEqual(p.pr_diff_argv("o/r", 857),
                         ["gh", "pr", "diff", "--repo", "o/r", "857"])

    def test_checks_argv_pinned(self):
        argv = p.pr_checks_argv("o/r", 857)
        self.assertEqual(argv[:6],
                         ["gh", "pr", "checks", "--repo", "o/r", "857"])
        self.assertIn("--json", argv)

    def test_contents_argv_get_and_encoded(self):
        argv = p.contents_argv("o/r", "a b/c?.ts", HEAD40)
        self.assertEqual(argv[:3], ["gh", "api", "--method"])
        self.assertEqual(argv[3], "GET")
        endpoint = argv[4]
        self.assertTrue(endpoint.startswith("repos/o/r/contents/"))
        self.assertNotIn("?", endpoint.split("/contents/", 1)[1])
        self.assertNotIn(" ", endpoint)
        self.assertIn("ref=" + HEAD40, argv)

    def test_contents_argv_rejects_shape(self):
        for bad in ("/abs", "a/../b", "", "a//b", "x" * 501):
            with self.assertRaises(p.PrError):
                p.contents_argv("o/r", bad, HEAD40)
        with self.assertRaises(p.PrError):
            p.contents_argv("o/r", "a.ts", "abc1234")
        with self.assertRaises(p.PrError):
            p.contents_argv("no-slug", "a.ts", HEAD40)

    def test_graphql_transport_rejects_mutation(self):
        with self.assertRaises(p.PrError):
            p._assert_readonly_graphql("mutation { x }")
        p._assert_readonly_graphql(p._THREADS_QUERY)


class TestSanitize(unittest.TestCase):
    def test_number_and_full_sha(self):
        d = p.sanitize_pr_detail(view_row())
        self.assertEqual(d["number"], 857)
        self.assertEqual(d["head_sha"], HEAD40)
        self.assertEqual(d["base_sha"], BASE40)
        self.assertEqual(d["author"], "contributor")

    def test_short_sha_is_unresolved(self):
        d = p.sanitize_pr_detail(view_row(headRefOid="abc1234"))
        self.assertEqual(d["head_sha"], "unresolved")

    def test_labels_objects_not_dicts(self):
        d = p.sanitize_pr_detail(view_row())
        self.assertEqual(d["labels"], ["iOS", "bug"])
        self.assertIsNone(d["labels_note"])

    def test_comments_and_reviews_kept_with_bodies(self):
        d = p.sanitize_pr_detail(view_row())
        self.assertTrue(d["comments_complete"])
        self.assertEqual(len(d["comments"]), 1)
        self.assertEqual(d["comments"][0]["body"], "c1")
        self.assertEqual(len(d["reviews"]), 1)
        self.assertEqual(d["reviews"][0]["state"], "APPROVED")

    def test_closes_parsed(self):
        d = p.sanitize_pr_detail(view_row())
        self.assertEqual(d["closes"], [344])

    def test_files_mismatch_noted(self):
        d = p.sanitize_pr_detail(view_row(changedFiles=5))
        self.assertIsNotNone(d["files_note"])
        self.assertIn("5", d["files_note"])

    def test_empty_files_with_changed_is_unavailable(self):
        d = p.sanitize_pr_detail(view_row(files=[], changedFiles=3))
        self.assertIsNotNone(d["files_note"])
        self.assertIn("unavailable", d["files_note"])

    def test_body_truncated_by_bytes(self):
        d = p.sanitize_pr_detail(view_row(body="é" * 30000))
        self.assertTrue(d["body_truncated"])
        self.assertGreater(d["body_bytes_total"], p._BODY_MAX_BYTES)
        self.assertLessEqual(len(d["body"].encode("utf-8")),
                             p._BODY_MAX_BYTES)


class TestCommentParsing(unittest.TestCase):
    def test_unknown_pageinfo_is_not_complete(self):
        items, complete, note = p._comment_list(
            {"nodes": [], "pageInfo": {}}, 50, 100)
        self.assertFalse(complete)
        self.assertIn("unknown", note)

    def test_byte_cap_never_splits_multibyte(self):
        items, complete, note = p._comment_list(
            [{"body": "é" * 5000}], 50, 100)
        self.assertFalse(complete)
        body = items[0]["body"]
        body.encode("utf-8")
        self.assertLessEqual(len(body.encode("utf-8")), 100)


class TestFetchCoherence(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.calls = []

    def _run(self, view, diff="d", checks="[]", threads_conn="missing",
             ident=None):
        from openmainty import discussions as disc

        def fake_run_cmd(argv, timeout=60):
            self.calls.append(argv)
            if argv[1:3] == ["pr", "view"] and "--json" in argv:
                fields = argv[argv.index("--json") + 1]
                if fields == p.PR_IDENTITY_FIELDS:
                    payload = ident if ident is not None else {
                        "number": 857, "headRefOid": HEAD40,
                        "baseRefOid": BASE40,
                        "updatedAt": "2026-10-20T00:00:00Z"}
                    return 0, json.dumps(payload), ""
                return 0, json.dumps(view), ""
            if argv[1:3] == ["pr", "diff"]:
                return 0, diff, ""
            if argv[1:3] == ["pr", "checks"]:
                return 8, checks, ""
            raise AssertionError(f"unexpected argv {argv}")

        def fake_gql(query, variables=None):
            if threads_conn == "missing":
                return {"repository": {"pullRequest": {"id": "x"}}}
            return {"repository": {"pullRequest": {
                "reviewThreads": threads_conn}}}

        run = mock.patch.object(p._util, "run_cmd",
                                side_effect=fake_run_cmd)
        gql = mock.patch.object(disc, "_gh_graphql", side_effect=fake_gql)
        run.start()
        gql.start()
        self.addCleanup(run.stop)
        self.addCleanup(gql.stop)

    def test_coherent_fetch_stores_nothing_but_returns(self):
        self._run(view_row())
        snap = p.fetch_pr_detail("o/r", 857)
        self.assertEqual(snap["head_sha"], HEAD40)
        text = " ".join(str(note) for note in snap["partial"])
        self.assertIn("threads section unavailable", text)
        self.assertIn("not returned", text)
        verbs = [(c[1], c[2]) for c in self.calls]
        self.assertIn(("pr", "view"), verbs)
        self.assertIn(("pr", "diff"), verbs)
        self.assertIn(("pr", "checks"), verbs)
        self.assertTrue(all(c[0] == "gh" for c in self.calls))

    def test_moved_head_refuses(self):
        self._run(view_row(), ident={
            "number": 857, "headRefOid": "c" * 40,
            "baseRefOid": BASE40, "updatedAt": "2026-10-21T00:00:00Z"})
        with self.assertRaises(p.PrError):
            p.fetch_pr_detail("o/r", 857)
        self.assertFalse(p.pr_path("o/r", 857).exists())

    def test_missing_full_sha_refuses(self):
        row = view_row(headRefOid="abc1234")
        self._run(row, ident={"number": 857, "headRefOid": "abc1234",
                              "baseRefOid": BASE40,
                              "updatedAt": "2026-10-21T00:00:00Z"})
        with self.assertRaises(p.PrError):
            p.fetch_pr_detail("o/r", 857)

    def test_exit8_checks_are_data_not_unavailable(self):
        self._run(view_row(), checks=json.dumps(
            [{"name": "ci", "state": "PENDING", "description": "",
              "link": ""}]))
        snap = p.fetch_pr_detail("o/r", 857)
        self.assertEqual(snap["checks"][0]["state"], "PENDING")
        self.assertTrue(any("pending" in note.lower()
                            for note in snap["partial"]))

    def test_nonzero_exit_with_valid_json_kept(self):
        from openmainty import discussions as disc

        def fake_run_cmd(argv, timeout=60):
            if argv[1:3] == ["pr", "view"] and "--json" in argv:
                fields = argv[argv.index("--json") + 1]
                if fields == p.PR_IDENTITY_FIELDS:
                    return 0, json.dumps({
                        "number": 857, "headRefOid": HEAD40,
                        "baseRefOid": BASE40, "updatedAt": "t"}), ""
                return 0, json.dumps(view_row()), ""
            if argv[1:3] == ["pr", "diff"]:
                return 0, "d", ""
            if argv[1:3] == ["pr", "checks"]:
                return 1, json.dumps([{"name": "ci", "state": "FAILURE",
                                       "description": "x", "link": ""}]), \
                    "boom"
            raise AssertionError(f"unexpected argv {argv}")

        run = mock.patch.object(p._util, "run_cmd",
                                side_effect=fake_run_cmd)
        gql = mock.patch.object(disc, "_gh_graphql",
                                return_value={"repository": {}})
        run.start()
        gql.start()
        self.addCleanup(run.stop)
        self.addCleanup(gql.stop)
        snap = p.fetch_pr_detail("o/r", 857)
        self.assertEqual(snap["checks"][0]["state"], "FAILURE")
        self.assertTrue(any("exited 1" in note
                            for note in snap["partial"]))


if __name__ == "__main__":
    unittest.main()
