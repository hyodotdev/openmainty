"""Discussions ingestion + typed labels + app-owned readonly review.

Offline: gh and the model are mocked at the product boundary
(_gh_graphql/_gh_rest/fetch_node/muse.exec_prompt). No test in this
module may touch the real network, the real home, or target repos.
Py3.9-safe.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import discussions as d
from openmainty import sched


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    prev = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(Path(tmp.name) / "home")
    test.addCleanup(lambda: os.environ.pop("OPENMAINTY_HOME", None)
                    if prev is None else os.environ.__setitem__(
                        "OPENMAINTY_HOME", prev))
    # Fail-fast guards: no test in this module may reach the real
    # network or a real model. Tests override exactly one boundary
    # (_gh_graphql/_gh_rest/fetch_node/muse.exec_prompt) with canned
    # data; anything else hitting gh or the model fails loudly.
    from openmainty import muse as muse_mod

    def _no_network(*args, **kwargs):
        raise AssertionError("unexpected gh/network call in offline test")

    def _no_model(*args, **kwargs):
        raise AssertionError("unexpected model call in offline test")

    net = mock.patch.object(d._util, "run_cmd", side_effect=_no_network)
    net.start()
    test.addCleanup(net.stop)
    mod = mock.patch.object(muse_mod, "exec_prompt", side_effect=_no_model)
    mod.start()
    test.addCleanup(mod.stop)
    return Path(tmp.name)


def list_node(i, updated="2026-10-24T00:00:00+00:00"):
    return {"id": f"D{i}", "number": i, "title": f"t{i}",
            "updatedAt": updated,
            "category": {"name": "Ideas"},
            "comments": {"totalCount": i % 3}}


def list_page(nodes, cursor, has_next, total):
    return {"repository": {
        "hasDiscussionsEnabled": True,
        "discussions": {
            "totalCount": total,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": nodes}}}


def norm_node(number=858, updated="2026-10-24T00:00:00+00:00",
              reply_body="same", **kw):
    node = {
        "schema": 1, "repo": "o/r", "kind": "discussion",
        "id": "D858", "number": number,
        "title": "Opus support for Android", "body": "body text",
        "url": "https://github.com/o/r/discussions/858",
        "created_at": "2026-10-24T00:00:00+00:00",
        "updated_at": updated, "closed": False, "closed_at": None,
        "upvotes": 3, "category": "Ideas", "author": "someone",
        "upstream_labels": [], "labels_complete": True,
        "proposed_labels": [],
        "comment_count": 1,
        "comments": [{
            "id": "C1", "database_id": 1, "author": "a",
            "body": reply_body,
            "created_at": "2026-10-24T01:00:00+00:00",
            "updated_at": updated,
            "url": "https://github.com/o/r/discussions/858#discussioncomment-1",
            "is_answer": False, "replies": [], "reply_count": 0,
            "replies_complete": True}],
        "comments_complete": True,
        "answer": None, "answer_chosenAt": None,
        "fetched_at": "2026-10-25T00:00:00+00:00",
    }
    node.update(kw)
    return node


def gh_report(**kw):
    rep = {"summary": "short summary",
           "findings": ["one finding"],
           "labels": {"platform": ["android"], "type": ["feature"]},
           "evidence": [],
           "reply_draft": None,
           "source_files": []}
    rep.update(kw)
    return rep


def model_stream(report, terminal=True):
    """json.dumps-built multi-event worker stream (real worker shape)."""
    lines = [
        json.dumps({"type": "muse.run",
                    "payload": {"kind": "run_model_configured",
                                "model": "m"}}),
        json.dumps({"type": "muse.run",
                    "payload": {"kind": "run_output_delta",
                                "text": '{"summ'}}),
        json.dumps({"type": "muse.run",
                    "payload": {"kind": "run_output_delta",
                                "text": 'ary": "x"}'}}),
    ]
    if terminal:
        lines.append(json.dumps(
            {"type": "muse.run",
             "payload": {"kind": "run_terminal",
                         "text": json.dumps(report)}}))
    return "\n".join(lines) + "\n"


class IdentityTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_good_paths_stay_in_namespace(self):
        base = d.discussions_dir()
        for path in (d.snapshot_path("o/r"), d.node_path("o/r", 858),
                     d.review_path("o/r", "858"),
                     d.job_path("o/r", 858),
                     d.catalog_path("o/r")):
            self.assertTrue(str(path).startswith(str(base)), path)

    def test_bad_slugs_raise(self):
        for slug in ("", "noslash", "a/b/c", "../x", "o/../p",
                     "o/r!", "o r/x"):
            with self.assertRaises(d.DiscussionError, msg=slug):
                d.snapshot_path(slug)

    def test_bad_numbers_raise(self):
        for number in ("D_123", "../../1", "0", "-5", "abc", "", None,
                       "12x", 0, -3):
            with self.assertRaises(d.DiscussionError, msg=str(number)):
                d.node_path("o/r", number)
        with self.assertRaises(d.DiscussionError):
            d.review_path("o/r", "D_1")
        with self.assertRaises(d.DiscussionError):
            d.job_path("o/r", "http://x")

    def test_valid_number_forms(self):
        self.assertTrue(str(d.node_path("o/r", 858)).endswith("858.json"))
        self.assertTrue(str(d.node_path("o/r", "858")).endswith("858.json"))

    def test_valid_number_helper(self):
        self.assertEqual(d.valid_number("858"), "858")
        self.assertIsNone(d.valid_number("D_858"))
        self.assertIsNone(d.valid_number(""))


class PagedGh:
    """Canned GraphQL list pages keyed by `after` cursor."""

    def __init__(self, pages):
        # pages: list of (key, nodes, has_next, next_key, total)
        self.pages = pages
        self.calls = []

    def __call__(self, query, variables=None):
        variables = variables or {}
        if "discussion(number" in query:
            raise AssertionError("node query reached list mock")
        after = variables.get("after")
        for key, nodes, has_next, end, total in self.pages:
            if key == after:
                self.calls.append(after)
                return list_page(nodes, end, has_next, total)
        raise AssertionError(f"unexpected after={after!r}")


def make_26_pages():
    p1 = [list_node(i) for i in range(1, 26)]
    p2 = [list_node(26, updated="2026-10-26T00:00:00+00:00")]
    return PagedGh([
        (None, p1, True, "C1", 26),
        ("C1", p2, False, "C2", 26),
    ])


class ScanTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_26_actual_shape_across_two_pages(self):
        with mock.patch.object(d, "_gh_graphql",
                               side_effect=make_26_pages()):
            out = d.scan_source("o/r", limit=75)
        self.assertEqual(len(out["items"]), 26)
        by_num = {i["number"]: i for i in out["items"]}
        self.assertEqual(by_num[26]["updated_at"],
                         "2026-10-26T00:00:00+00:00")
        status = out["status"]
        self.assertFalse(status["partial"])
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["watermark"], "2026-10-26T00:00:00+00:00")

    def test_over_cap_backfills_across_two_ticks(self):
        pages = []
        nums = list(range(1, 81))
        chunks = [nums[0:25], nums[25:50], nums[50:75], nums[75:80]]
        keys = [None, "C1", "C2", "C3"]
        ends = ["C1", "C2", "C3", "C4"]
        for k, chunk, nxt, end in zip(keys, chunks,
                                      [True, True, True, False], ends):
            pages.append((k, [list_node(i) for i in chunk], nxt, end, 80))
        gh = PagedGh(pages)
        with mock.patch.object(d, "_gh_graphql", side_effect=gh):
            first = d.scan_source("o/r", limit=75)
        self.assertEqual(len(first["items"]), 75)
        self.assertTrue(first["status"]["partial"])
        snap = d._read_snapshot("o/r")
        self.assertEqual(snap["backfill_after"], "C3")
        # Tick 2 resumes from the stored cursor and completes.
        with mock.patch.object(d, "_gh_graphql", side_effect=gh):
            second = d.scan_source("o/r", limit=75)
        self.assertEqual(len(second["items"]), 80)
        self.assertFalse(second["status"]["partial"])
        self.assertIsNone(second["status"]["backfill_after"])
        # Equal timestamps never drop: all ids present exactly once.
        ids = [i["id"] for i in second["items"]]
        self.assertEqual(len(set(ids)), 80)

    def test_has_next_without_cursor_is_partial(self):
        gh = PagedGh([(None, [list_node(1)], True, None, 9)])
        with mock.patch.object(d, "_gh_graphql", side_effect=gh):
            out = d.scan_source("o/r", limit=75)
        self.assertTrue(out["status"]["partial"])
        self.assertIsNotNone(out["status"]["note"])

    def test_error_preserves_prior_items_and_stays_partial(self):
        with mock.patch.object(d, "_gh_graphql",
                               side_effect=make_26_pages()):
            d.scan_source("o/r", limit=75)
        before = d._read_snapshot("o/r")
        self.assertEqual(len(before["items"]), 26)

        def _down(query, variables=None):
            raise d.DiscussionError("gh exploded")

        with mock.patch.object(d, "_gh_graphql", side_effect=_down):
            with self.assertRaises(d.DiscussionError):
                d.scan_source("o/r", limit=75)
        after = d._read_snapshot("o/r")
        self.assertEqual(len(after["items"]), 26)
        status = d.snapshot_status("o/r")
        self.assertEqual(status["state"], "error")
        self.assertTrue(status["partial"])

    def test_disabled_is_distinct_not_error_zero(self):
        payload = {"repository": {"hasDiscussionsEnabled": False,
                                  "discussions": {
                                      "totalCount": 0,
                                      "pageInfo": {"hasNextPage": False,
                                                   "endCursor": None},
                                      "nodes": []}}}
        with mock.patch.object(d, "_gh_graphql", return_value=payload):
            with self.assertRaises(d.DiscussionError) as ctx:
                d.scan_source("o/r")
        self.assertEqual(str(ctx.exception), "disabled")
        status = d.snapshot_status("o/r")
        self.assertTrue(status["disabled"])
        self.assertEqual(status["state"], "disabled")
        self.assertIsNone(status["error"])

    def test_disabled_refresh_returns_clean_status_and_keeps_items(self):
        # The live calendar shape: a manual refresh on a disabled
        # repo answers 200 with the disabled capability state (never a
        # 502), preserves cached items, and releases the worker lease.
        from openmainty import serve as serve_mod
        from openmainty import repos as repos_mod
        repos_mod.save({"name": "r", "github": "o/r",
                        "baseBranch": "main", "knowledge": [],
                        "checks": [], "e2e": [], "release": {},
                        "announce": {}})
        kept = [{"id": "D1", "type": "discussion", "number": 1,
                 "title": "old", "category": "Ideas", "comments": 0,
                 "updated_at": "2026-10-24T00:00:00+00:00"}]
        d._write_snapshot("o/r", kept, "2026-10-24T00:00:00+00:00")
        payload = {"repository": {"hasDiscussionsEnabled": False,
                                  "discussions": {
                                      "totalCount": 0,
                                      "pageInfo": {"hasNextPage": False,
                                                   "endCursor": None},
                                      "nodes": []}}}
        with mock.patch.object(d, "_gh_graphql",
                               return_value=payload), \
                mock.patch.object(d, "_gh_rest", return_value=[]):
            out = serve_mod.api_discussions_refresh(
                {"repo": "r", "by": "owner", "confirm": True})
        self.assertEqual(out["status"]["state"], "disabled")
        self.assertTrue(out["status"]["disabled"])
        self.assertIsNone(out["status"]["error"])
        self.assertEqual(out["items"], kept)
        # Lease released on the disabled path: a new acquire succeeds.
        from openmainty import sched as sched_mod
        token = sched_mod.acquire_worker_lease("probe")
        self.assertTrue(token)
        sched_mod.release_worker_lease(token)

    def test_disabled_refresh_genuine_error_still_502(self):
        from openmainty import serve as serve_mod
        from openmainty import repos as repos_mod
        repos_mod.save({"name": "r", "github": "o/r",
                        "baseBranch": "main", "knowledge": [],
                        "checks": [], "e2e": [], "release": {},
                        "announce": {}})
        with mock.patch.object(
                d, "_gh_graphql",
                side_effect=d.DiscussionError("gh exploded",
                                             status=502)):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_refresh(
                    {"repo": "r", "by": "owner", "confirm": True})
        self.assertEqual(ctx.exception.status, 502)
        from openmainty import sched as sched_mod
        token = sched_mod.acquire_worker_lease("probe")
        self.assertTrue(token)
        sched_mod.release_worker_lease(token)

    def test_hung_gh_finishes_as_source_error(self):
        with mock.patch.object(d._util, "run_cmd",
                               side_effect=TimeoutError("hang")):
            with self.assertRaises(d.DiscussionError):
                d._gh_graphql("query { x }", {})
        # run_cmd itself surfaces timeouts as exit 124, not a raise.
        with mock.patch.object(d._util, "run_cmd",
                               return_value=(124, "", "timed out")):
            with self.assertRaises(d.DiscussionError):
                d._gh_graphql("query { x }", {})

    def test_graphql_timeout_is_20s(self):
        seen = {}

        def _fake(argv, timeout=None):
            seen["timeout"] = timeout
            return 0, '{"data": {}}', ""

        with mock.patch.object(d._util, "run_cmd", side_effect=_fake):
            d._gh_graphql("query { x }", {})
        self.assertEqual(seen.get("timeout"), 20)


class ThroughSchedTests(unittest.TestCase):
    """Default-26/archive pagination and honest scan_ok via sched.scan."""

    def setUp(self):
        self.root = make_home(self)
        self.co = self.root / "co"
        self.co.mkdir()
        from openmainty import deps, pcconfig, repos
        self._repos = repos
        repos.save({"name": "dt", "github": "o/dt", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        (self.co / "package.json").write_text(json.dumps(
            {"dependencies": {"left-pad": "1.0.0"}}))
        pc = pcconfig.load()
        pc["checkouts"]["dt"] = str(self.co)
        pcconfig.save(pc)
        orig = deps.set_latest_fn(lambda kind, ident: None)
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def test_scan_26_through_sched_ok(self):
        from openmainty import cli, sched
        with mock.patch.object(d, "_gh_graphql",
                               side_effect=make_26_pages()), \
                mock.patch.object(d, "_gh_rest", return_value=[]), \
                mock.patch.object(cli, "_open_issues", return_value=[]), \
                mock.patch.object(cli, "_open_prs", return_value=[]):
            out = sched.scan("dt")
        self.assertTrue(out["scan_ok"], out.get("sources"))
        self.assertEqual(out["discussions_open"], 26)
        self.assertIsNone(out["discussions_error"])
        self.assertFalse(out["discussions_disabled"])
        kinds = {q.get("kind") for q in
                 sched.load("dt").get("queue", [])}
        self.assertNotIn("discussion-triage", kinds)

    def test_scan_disabled_calendar_shape_through_sched(self):
        from openmainty import cli, sched
        payload = {"repository": {"hasDiscussionsEnabled": False,
                                  "discussions": {
                                      "totalCount": 0,
                                      "pageInfo": {"hasNextPage": False,
                                                   "endCursor": None},
                                      "nodes": []}}}
        with mock.patch.object(d, "_gh_graphql",
                               return_value=payload), \
                mock.patch.object(d, "_gh_rest", return_value=[]), \
                mock.patch.object(cli, "_open_issues", return_value=[]), \
                mock.patch.object(cli, "_open_prs", return_value=[]):
            out = sched.scan("dt")
        self.assertTrue(out["scan_ok"], out.get("sources"))
        self.assertTrue(out["discussions_disabled"])
        self.assertIsNone(out["discussions_error"])

    def test_scan_gh_down_through_sched_not_ok(self):
        from openmainty import cli, sched

        def _down(*a, **k):
            raise d.DiscussionError("gh exploded")

        with mock.patch.object(d, "_gh_graphql", side_effect=_down), \
                mock.patch.object(d, "_gh_rest", side_effect=_down), \
                mock.patch.object(cli, "_open_issues", return_value=[]), \
                mock.patch.object(cli, "_open_prs", return_value=[]):
            out = sched.scan("dt")
        self.assertFalse(out["scan_ok"])
        self.assertIsNotNone(out["discussions_error"])
        self.assertIn("error", out["sources"].get("discussions", ""))


class CorruptTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_corrupt_snapshot_repeated_poll_stays_error(self):
        d.snapshot_path("o/r").parent.mkdir(parents=True, exist_ok=True)
        d.snapshot_path("o/r").write_text("{not json",
                                          encoding="utf-8")
        first = d.snapshot_status("o/r")
        second = d.snapshot_status("o/r")
        for status in (first, second):
            self.assertEqual(status["state"], "error")
            self.assertTrue(status["partial"])
        # Historical bytes preserved in place, never healthy zeros.
        self.assertEqual(d.snapshot_path("o/r").read_text(
            encoding="utf-8"), "{not json")

    def test_corrupt_node_review_job_never_systemexit(self):
        for path in (d.node_path("o/r", 858), d.review_path("o/r", 858),
                     d.job_path("o/r", 858)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{bad", encoding="utf-8")
        out = d.review_outcome("o/r", 858)
        self.assertEqual(out["status"], "error")
        self.assertTrue(out["source_unavailable"])
        self.assertEqual(d.job_state("o/r", 858)["status"], "error")

    def test_missing_node_with_stored_review_is_unknown(self):
        node = norm_node()
        fp = d.semantic_fingerprint(node)
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed", "fingerprint": fp,
                      "report": {"summary": "s", "findings": ["f"]},
                      "labels": ["android"],
                      "label_facets": {"platform": ["android"]}})
        d._write_review("o/r", 858, state)
        out = d.review_outcome("o/r", 858)
        self.assertEqual(out["status"], "unknown")
        self.assertTrue(out["source_unavailable"])
        self.assertEqual(out["report"], {"summary": "s",
                                         "findings": ["f"]})
        self.assertEqual(out["label_facets"],
                         {"platform": ["android"]})


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.node = norm_node()
        d.write_json(d.node_path("o/r", 858), self.node)
        self.fp = d.semantic_fingerprint(self.node)

    def _store_reviewed(self, **kw):
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed", "fingerprint": self.fp,
                      "report": {"summary": "s", "findings": ["f"]},
                      "labels": [], "label_facets": {}}, **kw)
        d._write_review("o/r", 858, state)
        return state

    def test_fresh_review_without_sources(self):
        self._store_reviewed()
        out = d.review_outcome("o/r", 858)
        self.assertEqual(out["status"], "reviewed")
        self.assertFalse(out["stale"])

    def test_stored_sources_without_root_is_unknown(self):
        self._store_reviewed(source_evidence=[{"path": "a.py",
                                               "sha256": "x"}])
        out = d.review_outcome("o/r", 858)
        self.assertEqual(out["status"], "unknown")
        self.assertTrue(out["source_unavailable"])
        self.assertFalse(out["source_checked"])

    def test_linked_unchanged_source_is_fresh(self):
        root = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        root.mkdir()
        (root / "a.py").write_text("print(1)\n")
        digest = d.hash_source_files(root, ["a.py"])[0]["sha256"]
        self._store_reviewed(source_evidence=[{"path": "a.py",
                                               "sha256": digest}])
        out = d.review_outcome("o/r", 858, str(root))
        self.assertEqual(out["status"], "reviewed")
        self.assertTrue(out["source_checked"])

    def test_linked_changed_source_is_stale(self):
        root = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        root.mkdir()
        (root / "a.py").write_text("print(1)\n")
        self._store_reviewed(source_evidence=[{"path": "a.py",
                                               "sha256": "old"}])
        out = d.review_outcome("o/r", 858, str(root))
        self.assertEqual(out["status"], "stale")
        self.assertTrue(out["source_changed"])

    def test_scan_newer_list_row_marks_stale(self):
        snap = d._write_snapshot("o/r", [{
            "id": "D858", "type": "discussion", "number": 858,
            "title": "t", "category": "Ideas", "comments": 1,
            "updated_at": "2099-01-01T00:00:00+00:00"}], "stamp")
        self._store_reviewed()
        out = d.review_outcome("o/r", 858)
        self.assertEqual(out["status"], "stale")

    def test_detail_complete_flag(self):
        self._store_reviewed()
        out = d.review_outcome("o/r", 858)
        self.assertTrue(out["detail_complete"])
        node = dict(self.node, comments_complete=False)
        d.write_json(d.node_path("o/r", 858), node)
        self.assertFalse(d.review_outcome("o/r", 858)["detail_complete"])


class LabelsTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_label_pages_paginate_with_complete_flag(self):
        calls = []

        def _rest(path):
            page = int(path.rsplit("page=", 1)[1])
            calls.append(page)
            if page == 1:
                return [{"name": f"l{i}"} for i in range(100)]
            if page == 2:
                return [{"name": f"m{i}"} for i in range(50)]
            return []

        with mock.patch.object(d, "_gh_rest", side_effect=_rest):
            rec = d.fetch_labels("o/r")
        self.assertEqual(len(rec["labels"]), 150)
        self.assertTrue(rec["labels_complete"])
        self.assertEqual(rec["pages"], 2)
        stored = d.label_census("o/r")
        self.assertEqual(len(stored["labels"]), 150)

    def test_label_overflow_marks_partial(self):
        with mock.patch.object(
                d, "_gh_rest",
                return_value=[{"name": f"l{i}"} for i in range(100)]):
            rec = d.fetch_labels("o/r")
        self.assertFalse(rec["labels_complete"])
        self.assertEqual(rec["pages"], 3)

    def test_label_nonlist_is_source_error(self):
        with mock.patch.object(d, "_gh_rest", return_value={"a": 1}):
            with self.assertRaises(d.DiscussionError):
                d.fetch_labels("o/r")

    def test_catalog_facets_include_web_docs_unknown(self):
        catalog = d.label_catalog()
        self.assertIn("web", catalog["platform"])
        self.assertIn("docs", catalog["type"])
        self.assertIn("unknown", catalog["platform"])
        self.assertIn("unknown", catalog["type"])


class ValidateTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.node = norm_node()

    def test_ok_report_validates(self):
        rep = gh_report()
        self.assertEqual(d.validate_report(self.node, rep, [], None), [])

    def test_malformed_falsy_labels_rejected_missing_and_empty_ok(self):
        # null, [], "", 0 must not coerce into a valid empty object.
        for bad in (None, [], "", 0):
            rep = gh_report(labels=bad)
            problems = d.validate_report(self.node, rep, [], None)
            self.assertTrue(any("labels" in p for p in problems),
                            (bad, problems))
        # {} stays legal (unclassified, quiet 미분류); a missing key
        # is still caught by the pre-existing required-keys gate.
        self.assertEqual(
            d.validate_report(self.node, gh_report(labels={}), [],
                              None), [])
        rep = gh_report()
        del rep["labels"]
        self.assertEqual(d.validate_report(self.node, rep, [], None),
                         ["report missing key: labels"])

    def test_platform_self_cert_rejected(self):
        rep = gh_report(labels={"platform": ["reviewed"]})
        self.assertTrue(d.validate_report(self.node, rep, [], None))

    def test_nested_label_object_rejected_without_raise(self):
        rep = gh_report(labels={"platform": [["nested"]]})
        problems = d.validate_report(self.node, rep, [], None)
        self.assertTrue(problems)

    def test_nonlist_labels_rejected_without_raise(self):
        rep = gh_report(labels={"platform": "android"})
        problems = d.validate_report(self.node, rep, [], None)
        self.assertTrue(problems)

    def test_dict_source_path_rejected_without_raise(self):
        rep = gh_report(source_files=[{}])
        problems = d.validate_report(self.node, rep, [], None)
        self.assertTrue(problems)

    def test_evidence_missing_note_rejected(self):
        rep = gh_report(evidence=[{"url": self.node["url"]}])
        problems = d.validate_report(self.node, rep, [], None)
        self.assertTrue(any("note" in p for p in problems))

    def test_evidence_nonobject_rejected_without_raise(self):
        rep = gh_report(evidence=["http://x"])
        problems = d.validate_report(self.node, rep, [], None)
        self.assertTrue(problems)

    def test_empty_findings_and_summary_rejected(self):
        rep = gh_report(summary="  ", findings=[])
        problems = d.validate_report(self.node, rep, [], None)
        self.assertEqual(len(problems), 2)

    def test_status_only_needs_review_or_action(self):
        rep = gh_report(labels={"status": ["stale-review"]})
        self.assertTrue(d.validate_report(self.node, rep, [], None))
        rep2 = gh_report(labels={"status": ["needs-action"]})
        self.assertEqual(d.validate_report(self.node, rep2, [], None), [])

    def test_web_docs_unknown_accepted(self):
        rep = gh_report(labels={"platform": ["web", "unknown"],
                                "type": ["docs", "unknown"]})
        self.assertEqual(d.validate_report(self.node, rep, [], None), [])

    def test_prompt_example_parses_and_validates(self):
        # The prompt's complete example must be loadable as ONE JSON
        # document (no trailing bytes) and pass the real validator.
        built = d._build_review_prompt(self.node, [])
        seg = built["prompt"].split("verbatim):\n", 1)[1]
        seg = seg.split("Honesty rules:", 1)[0]
        rep = json.loads(seg)
        self.assertEqual(d.validate_report(self.node, rep, [], None),
                         [])

    def test_permitted_urls_listed_and_body_link_rejected(self):
        # A link pasted inside a comment body is mentioned data, not
        # evidence: the prompt must list exactly the validator's
        # structural set (discussion, comment, nested reply, answer),
        # and the real validator must still reject the body link
        # while accepting a listed structural url.
        body = "see https://github.com/o/r/issues/437 for background"
        node = norm_node(reply_body=body)
        node["comments"][0]["replies"] = [{
            "id": "R1", "author": "b", "body": "reply text",
            "created_at": "2026-10-24T02:00:00+00:00",
            "updated_at": "2026-10-24T02:00:00+00:00",
            "url": ("https://github.com/o/r/discussions/858"
                    "#discussioncomment-2")}]
        node["comments"][0]["reply_count"] = 1
        node["answer"] = {
            "id": "A1", "author": "c", "body": "answer text",
            "created_at": "2026-10-24T03:00:00+00:00",
            "updated_at": "2026-10-24T03:00:00+00:00",
            "url": ("https://github.com/o/r/discussions/858"
                    "#discussioncomment-3"),
            "is_answer": True}
        before = json.dumps(node, sort_keys=True)
        fp0 = d.semantic_fingerprint(node)
        built = d._build_review_prompt(node, [])
        self.assertEqual(json.dumps(node, sort_keys=True), before)
        self.assertEqual(d.semantic_fingerprint(node), fp0)
        permitted = built["input_snapshot"]["permitted_evidence_urls"]
        expected = sorted({
            node["url"],
            node["comments"][0]["url"],
            node["comments"][0]["replies"][0]["url"],
            node["answer"]["url"]})
        self.assertEqual(permitted, expected)
        self.assertEqual(permitted, sorted(permitted))
        self.assertIn("PERMITTED EVIDENCE URLS", built["prompt"])
        seg = built["prompt"].split(
            "PERMITTED EVIDENCE URLS (complete", 1)[1]
        rendered, _ = json.JSONDecoder().raw_decode(
            seg[seg.index("["):])
        self.assertEqual(rendered, permitted)
        self.assertIn(body, built["prompt"])
        self.assertFalse(built["truncated"])
        self.assertEqual(built["input_snapshot"]["sources"], [])
        for url in expected:
            ok = gh_report(evidence=[{"url": url, "note": "why"}])
            self.assertEqual(d.validate_report(node, ok, [], None), [],
                             url)
        bad = gh_report(evidence=[{"url": "https://github.com/o/r/issues/437",
                                   "note": "why"}])
        problems = d.validate_report(node, bad, [], None)
        self.assertTrue(any(p.startswith("invented evidence url")
                            for p in problems), problems)

    def test_scalar_labels_are_schema_not_citation(self):
        # The real 858 failure: scalar facet values are malformed
        # output, not invented citations.
        rep = gh_report(labels={"status": "needs-review",
                                "platform": "android",
                                "type": "feature"})
        problems = d.validate_report(self.node, rep, [], None)
        self.assertTrue(problems)
        schema, citation = d._split_report_problems(problems)
        self.assertTrue(schema)
        self.assertEqual(citation, [])

    def test_genuine_unsent_cite_stays_citation(self):
        rep = gh_report(source_files=["nope.py"])
        problems = d.validate_report(self.node, rep, [], None)
        schema, citation = d._split_report_problems(problems)
        self.assertEqual(schema, [])
        self.assertTrue(citation)


class StreamTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_terminal_wins_over_deltas(self):
        rep = gh_report()
        stream = model_stream(rep)
        text, kind = d._extract_model_text(stream)
        self.assertEqual(kind, "terminal")
        self.assertEqual(json.loads(text)["summary"], "short summary")

    def test_delta_fallback_without_terminal(self):
        stream = "\n".join([
            json.dumps({"type": "x.run",
                        "payload": {"kind": "run_output_delta",
                                    "text": '{"a": 1}'}}),
        ]) + "\n"
        text, kind = d._extract_model_text(stream)
        self.assertEqual(kind, "delta")
        self.assertEqual(json.loads(text), {"a": 1})

    def test_garbage_stream_is_none(self):
        text, kind = d._extract_model_text("not json\n{bad}\n")
        self.assertEqual((text, kind), ("", "none"))

    def test_non_json_report_rejected(self):
        rep, err = d._extract_report_json("plain words, no braces")
        self.assertIsNone(rep)
        self.assertTrue(err)


class HashSourcesTests(unittest.TestCase):
    def setUp(self):
        self.root = make_home(self)

    def test_symlink_escape_rejected_and_unread(self):
        repo = Path(str(self.root)) / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        # Sentinel lives OUTSIDE the repo root: the resolve gate must
        # reject the link before any byte is read.
        outside = Path(str(self.root)) / "secret.txt"
        outside.write_text("SENTINEL-DO-NOT-READ", encoding="utf-8")
        link = repo / "link.py"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        out = d.hash_source_files(repo, ["link.py"])
        self.assertEqual(out, [{"path": "link.py", "sha256": None,
                                "rejected": True}])

    def test_caps_and_rejected_shapes(self):
        out = d.hash_source_files(self.root, ["/abs", "../x", "..", ""])
        self.assertTrue(all(e["sha256"] is None for e in out))
        big = Path(str(self.root)) / "big.py"
        big.write_bytes(b"x" * (d._SOURCE_BYTES_CAP + 1))
        out2 = d.hash_source_files(self.root, ["big.py"])
        self.assertEqual(out2[0].get("reason"), "too-large")


class ReviewJobTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.node = norm_node()
        self.fp = d.semantic_fingerprint(self.node)

    def _persisting(self, *nodes):
        """Fetch mock honoring the real fetch contract: persist, return.

        The real fetch_node persists the node file and the worker
        re-reads node_path (it does not trust the start-time object),
        so a mock that only returns without persisting hides the
        heal/re-verify path. Pass several nodes for race sequences.
        """
        seq = list(nodes) or [self.node]
        state = {"i": 0}

        def _fetch(github, number):
            node = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            d.write_json(d.node_path("o/r", number), node)
            return node

        return _fetch

    def _wait_job(self, number=858, timeout=30.0):
        deadline = time.time() + timeout
        status = "running"
        while time.time() < deadline:
            status = d.job_state("o/r", number).get("status")
            if status != "running":
                break
            time.sleep(0.1)
        # Join the worker before HOME cleanup: the thread only
        # releases the lease after the job record is final, so no
        # post-cleanup write can escape into the next home.
        for thread in threading.enumerate():
            if thread is threading.main_thread() or not thread.is_alive():
                continue
            thread.join(timeout=10)
        return status

    def test_refresh_failure_is_error_not_cached(self):
        def _down(github, number):
            raise d.DiscussionError("gh exploded", status=502)

        with mock.patch.object(d, "fetch_node", side_effect=_down):
            with self.assertRaises(d.DiscussionError) as ctx:
                d.start_review("o/r", 858, by="owner")
        self.assertIn("refresh failed", str(ctx.exception))

    def test_worker_by_rejected(self):
        with self.assertRaises(d.DiscussionError) as ctx:
            d.start_review("o/r", 858, by="worker")
        self.assertEqual(ctx.exception.http_status, 400)

    def test_busy_lease_is_409(self):
        # flock is per-process: a second real acquire in this process
        # would succeed, so busy is simulated at the acquire boundary
        # (the lease mechanism itself is covered by sched's own tests).
        from openmainty import sched as sched_mod
        with mock.patch.object(d, "fetch_node",
                               return_value=self.node), \
                mock.patch.object(sched_mod, "acquire_worker_lease",
                                  return_value=None):
            with self.assertRaises(d.DiscussionError) as ctx:
                d.start_review("o/r", 858, by="owner")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_already_reviewed_fresh_starts_nothing(self):
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed", "fingerprint": self.fp})
        d._write_review("o/r", 858, state)
        with mock.patch.object(d, "fetch_node",
                               return_value=self.node):
            out = d.start_review("o/r", 858, by="owner")
        self.assertFalse(out["started"])
        self.assertEqual(out["reason"], "already reviewed (fresh)")

    def test_success_end_to_end_with_facets(self):
        from openmainty import muse as muse_mod
        rep = gh_report(labels={"platform": ["android"],
                                "type": ["feature"]},
                        evidence=[{"url": self.node["url"],
                                   "note": "title"}],
                        reply_draft="hello")
        stream = model_stream(rep)
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persisting()), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(0, stream, {"ok": 1})) \
                as fake_exec:
            out = d.start_review("o/r", 858, by="owner")
            self.assertTrue(out["started"])
            # Drain inside the patch scope: the worker must finish
            # while the inner fetch/model mocks are installed.
            self.assertEqual(self._wait_job(), "done")
            _, kwargs = fake_exec.call_args
        state = d.review_state("o/r", 858)
        self.assertEqual(state["status"], "reviewed")
        self.assertEqual(state["fingerprint"], self.fp)
        self.assertEqual(state["label_facets"],
                         {"platform": ["android"],
                          "type": ["feature"]})
        self.assertFalse(state.get("partial"))
        # Node file carries the proposed labels for list rendering.
        node = d.read_json(d.node_path("o/r", 858))
        self.assertEqual(sorted(node["proposed_labels"]),
                         ["android", "feature"])
        # The audit actor survives finalization into the job record.
        self.assertEqual(d.job_state("o/r", 858).get("by"), "owner")
        # Attempt artifact holds the EXACT model input + raw stream.
        art_name = state.get("artifact")
        self.assertTrue(art_name)
        art = d.read_json(d._slug_dir("o/r") / art_name)
        self.assertIn("Opus support for Android", art["prompt"])
        self.assertIn("run_terminal", art["raw_output"])
        self.assertEqual(art["report"]["summary"], "short summary")
        self.assertEqual(art["raw_output_meta"]["capped"], False)
        # Env scrub + sandbox flags reached exec_prompt.
        self.assertNotIn("META_API_KEY", kwargs["env"])
        self.assertIn("--disable-write", kwargs["extra"])
        self.assertIn("--disable-shell", kwargs["extra"])
        self.assertIn("--disable-web-tools", kwargs["extra"])
        self.assertIn("--max-model-steps", kwargs["extra"])

    def _failed_job(self, code, text, number=858, node=None):
        from openmainty import muse as muse_mod
        use = node if node is not None else self.node

        def _fetch(github, num):
            d.write_json(d.node_path("o/r", num), use)
            return use

        # The worker must complete/drain while the inner mocks are
        # still installed: _wait_job stays inside the patch scope so a
        # late worker can never fall back past the fixture sentinels.
        with mock.patch.object(d, "fetch_node",
                               side_effect=_fetch), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(code, text, {})):
            out = d.start_review("o/r", number, by="owner")
            self.assertTrue(out["started"])
            self.assertEqual(self._wait_job(number), "error")
        return d.job_state("o/r", number)

    def test_failed_job_helper_holds_mocks_through_wait(self):
        # The helper must keep its inner fetch/model mocks installed
        # until its own wait drains the worker. The worker is
        # deliberately deferred at a test-owned gate before reaching
        # transport; the observing wait wrapper records whether the
        # helper's INNER model mock (not the outer fixture sentinel)
        # is active at helper-wait time, then releases and delegates
        # to the preserved real wait/drain. This fails if ONLY the
        # helper's wait moves back outside its mocks. Bounded Events
        # synchronize; the gate is released in finally before worker
        # patches are restored, even on assertion failure.
        from openmainty import muse as muse_mod
        sentinel_exec = muse_mod.exec_prompt
        real_run_review = d._run_review
        worker_at_gate = threading.Event()
        gate_open = threading.Event()
        observed = {}

        def _gated_run_review(*args, **kwargs):
            worker_at_gate.set()
            if not gate_open.wait(timeout=30):
                raise AssertionError("deferred worker gate never released")
            return real_run_review(*args, **kwargs)

        def _observing_wait(number=858, timeout=30.0):
            observed["inner_mock"] = muse_mod.exec_prompt
            observed["inner_active"] = \
                muse_mod.exec_prompt is not sentinel_exec
            self.assertTrue(worker_at_gate.wait(timeout=30),
                            "worker never reached the deferred gate")
            gate_open.set()
            return ReviewJobTests._wait_job(self, number,
                                            timeout=timeout)

        with mock.patch.object(d, "_run_review",
                               side_effect=_gated_run_review), \
                mock.patch.object(self, "_wait_job",
                                  side_effect=_observing_wait):
            try:
                job = self._failed_job(
                    1, "Error: quota exceeded, rate limited (429)")
                self.assertTrue(worker_at_gate.is_set())
                self.assertTrue(observed.get("inner_active"),
                                "helper wait ran past its inner mocks")
                self.assertEqual(job.get("error_kind"), "quota")
                self.assertIn("한도 회복", job.get("error", ""))
                self.assertNotIn("다시 로그인한 뒤", job.get("error", ""))
            finally:
                # Release and drain while the worker patches are still
                # active, even if an assertion above raised; the
                # original failure propagates unchanged.
                gate_open.set()
                ReviewJobTests._wait_job(self)
        inner = observed.get("inner_mock")
        self.assertIsNotNone(inner)
        self.assertEqual(inner.call_count, 1)

    def test_old_helper_lifetime_detected_without_real_transport(self):
        # Negative control for the pre-v9 helper shape: inner mocks
        # exit BEFORE the helper's wait. The observing wait must see
        # the test-owned OUTER fallback mock (not an inner mock) at
        # helper-wait time, and the released worker must hit only
        # test-owned sentinels — never a real transport.
        from openmainty import muse as muse_mod
        real_run_review = d._run_review
        worker_at_gate = threading.Event()
        gate_open = threading.Event()
        observed = {}

        def _fetch(github, num):
            d.write_json(d.node_path("o/r", num), self.node)
            return self.node

        def _gated_run_review(*args, **kwargs):
            worker_at_gate.set()
            if not gate_open.wait(timeout=30):
                raise AssertionError("deferred worker gate never released")
            return real_run_review(*args, **kwargs)

        def _observing_wait(number=858, timeout=30.0):
            observed["at_outer_fallback"] = \
                muse_mod.exec_prompt is outer_exec_mock
            self.assertTrue(worker_at_gate.wait(timeout=30),
                            "worker never reached the deferred gate")
            gate_open.set()
            return ReviewJobTests._wait_job(self, number,
                                            timeout=timeout)

        def _no_real_fetch(*args, **kwargs):
            raise AssertionError("negative control reached real fetch")

        def _no_real_model(*args, **kwargs):
            raise AssertionError("negative control reached real model")

        with mock.patch.object(d, "_run_review",
                               side_effect=_gated_run_review), \
                mock.patch.object(self, "_wait_job",
                                  side_effect=_observing_wait), \
                mock.patch.object(d, "fetch_node",
                                  side_effect=_no_real_fetch), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  side_effect=_no_real_model) \
                as outer_exec_mock:
            try:
                # Pre-v9 shape: mocks exit before the wait.
                with mock.patch.object(d, "fetch_node",
                                       side_effect=_fetch), \
                        mock.patch.object(
                            muse_mod, "exec_prompt",
                            return_value=(1, "Error: quota exceeded (429)",
                                          {})):
                    out = d.start_review("o/r", 858, by="owner")
                self.assertTrue(out["started"])
                status = self._wait_job()
                self.assertTrue(worker_at_gate.is_set())
                self.assertTrue(observed.get("at_outer_fallback"),
                                "helper wait did not fall back to the "
                                "test-owned sentinel")
                self.assertEqual(status, "error")
            finally:
                # Release and drain while the fallback patches are
                # still active, even if an assertion above raised;
                # the original failure propagates unchanged.
                gate_open.set()
                ReviewJobTests._wait_job(self)
        job = d.job_state("o/r", 858)
        self.assertIn("negative control", job.get("error", ""))

    def test_quota_only_kind_with_recovery_action(self):
        job = self._failed_job(
            1, "Error: quota exceeded, rate limited (429)")
        self.assertEqual(job.get("error_kind"), "quota")
        self.assertIn("한도 회복", job.get("error", ""))
        # A quota reset never asks for another sign-in (the auth-only
        # action phrase must not appear).
        self.assertNotIn("다시 로그인한 뒤", job.get("error", ""))

    def test_auth_only_kind_with_signin_action(self):
        job = self._failed_job(1, "Error: unauthorized (401)")
        self.assertEqual(job.get("error_kind"), "auth")
        self.assertIn("다시 로그인", job.get("error", ""))
        self.assertNotIn("한도", job.get("error", ""))

    def test_mixed_auth_quota_prefers_auth(self):
        # Explicit 401/unauthorized beats quota words: fix auth first.
        job = self._failed_job(
            1, "Error: auth failed, quota exceeded (401)")
        self.assertEqual(job.get("error_kind"), "auth")

    def test_billing_kind_never_suggests_api_key(self):
        job = self._failed_job(
            1, "Error: payment required, billing past due")
        self.assertEqual(job.get("error_kind"), "billing")
        self.assertIn("구독/결제", job.get("error", ""))
        self.assertIn("API 키 구매가 아님", job.get("error", ""))

    def test_offline_kind_with_connection_action(self):
        job = self._failed_job(1, "getaddrinfo ENOTFOUND api.example")
        self.assertEqual(job.get("error_kind"), "offline")
        self.assertIn("연결", job.get("error", ""))

    def test_timeout_kind(self):
        job = self._failed_job(124, "anything at all")
        self.assertEqual(job.get("error_kind"), "timeout")
        self.assertIn("제한 시간", job.get("error", ""))

    def test_unknown_failure_kind(self):
        job = self._failed_job(1, "weird boom, no known token")
        self.assertEqual(job.get("error_kind"), "failed")

    def test_error_keeps_attempt_and_prior_report(self):
        from openmainty import muse as muse_mod
        rep = gh_report()
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persisting()), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(0, model_stream(rep),
                                                {})):
            self.assertTrue(d.start_review("o/r", 858, by="owner")
                            ["started"])
            self.assertEqual(self._wait_job(), "done")
        first_report = d.review_state("o/r", 858)["report"]
        # A changed discussion forces a new attempt (not a fresh
        # dedup); the model failure must not clobber the old report.
        edited = norm_node(body="changed text")
        job = self._failed_job(1, "Error: quota exceeded (429)",
                               node=edited)
        self.assertEqual(job.get("error_kind"), "quota")
        # The prior verified report is untouched; the failed attempt
        # kept its own artifact with the raw stream.
        self.assertEqual(d.review_state("o/r", 858)["report"],
                         first_report)
        arts = sorted(d._slug_dir("o/r").glob("review-858-*.json"))
        self.assertTrue(any("quota exceeded" in
                            (d.read_json(a).get("raw_output") or "")
                            for a in arts))

    def test_update_during_model_race_keeps_history_only(self):
        from openmainty import muse as muse_mod
        edited = norm_node(updated="2026-10-26T00:00:00+00:00",
                           reply_body="changed meanwhile")
        old_fp = d.semantic_fingerprint(norm_node(body="older text"))
        old = d._blank_review("o/r", 858)
        old.update({"status": "reviewed", "fingerprint": old_fp})
        d._write_review("o/r", 858, old)
        stream = model_stream(gh_report())
        # Start refresh persists the node (real fetch contract), so the
        # worker re-reads the file and the completion re-verify is the
        # only later fetch — it sees the meanwhile edit.
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persisting(
                                   self.node, edited)), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(0, stream, {})):
            out = d.start_review("o/r", 858, by="owner")
            self.assertTrue(out["started"])
            self.assertEqual(self._wait_job(), "stale-input")
        state = d.review_state("o/r", 858)
        self.assertEqual(state["status"], "reviewed")
        self.assertEqual(state["fingerprint"], old_fp)
        self.assertTrue(any(e.get("event") == "job-stale-input"
                            for e in state.get("history", [])))

    def test_invented_citations_rejected(self):
        from openmainty import muse as muse_mod
        rep = gh_report(evidence=[{"url": "https://evil.example/x",
                                   "note": "lies"}])
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persisting()), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(0, model_stream(rep),
                                                {})):
            self.assertTrue(d.start_review(
                "o/r", 858, by="owner")["started"])
            self.assertEqual(self._wait_job(), "error")
        self.assertNotEqual(
            d.review_state("o/r", 858).get("status"), "reviewed")
        job = d.job_state("o/r", 858)
        self.assertTrue(job.get("error", "").startswith(
            "rejected invented citations: "))
        self.assertNotIn("error_kind", job)

    def test_scalar_labels_rejected_as_invalid_report(self):
        # The real 858 shape: scalar facet values are malformed
        # output, surfaced as invalid-report — never as an invented
        # citation, and never promoted to reviewed.
        from openmainty import muse as muse_mod
        rep = gh_report(labels={"status": "needs-review",
                                "platform": "android",
                                "type": "feature"})
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persisting()), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(0, model_stream(rep),
                                                {})):
            self.assertTrue(d.start_review(
                "o/r", 858, by="owner")["started"])
            self.assertEqual(self._wait_job(), "error")
        self.assertNotEqual(
            d.review_state("o/r", 858).get("status"), "reviewed")
        job = d.job_state("o/r", 858)
        self.assertTrue(job.get("error", "").startswith(
            "model output failed validation: "))
        self.assertEqual(job.get("error_kind"), "invalid-report")

    def test_interrupted_derived_and_retry_marks_history(self):
        from openmainty import muse as muse_mod
        d._write_job("o/r", 858, {"status": "running", "by": "owner",
                                  "fingerprint": "fp",
                                  "rid": "r1", "lease_owner": "dead",
                                  "started_at": "x"})
        derived = d.job_state("o/r", 858)
        self.assertEqual(derived["status"], "interrupted")
        self.assertTrue(derived["derived"])
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persisting()), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(1, "offline", {})):
            out = d.start_review("o/r", 858, by="owner")
            self.assertTrue(out["started"])
            self.assertEqual(self._wait_job(), "error")
        state = d.review_state("o/r", 858)
        self.assertTrue(any(e.get("event") == "job-interrupted"
                            for e in state.get("history", [])))

    def test_write_job_failure_releases_lease(self):
        from openmainty import sched
        with mock.patch.object(d, "fetch_node",
                               return_value=self.node), \
                mock.patch.object(d, "write_json",
                                  side_effect=OSError("disk full")):
            with self.assertRaises(d.DiscussionError):
                d.start_review("o/r", 858, by="owner")
        try:
            status = sched.global_lock_status() or {}
        except (Exception, SystemExit):
            status = {}
        self.assertFalse(status.get("live"))

    def test_too_many_evidence_paths_rejected_before_worker(self):
        many = [f"f{i}.ts" for i in range(26)]
        with mock.patch.object(d, "fetch_node",
                               return_value=self.node):
            with self.assertRaises(d.DiscussionError) as ctx:
                d.start_review("o/r", 858, by="owner",
                               evidence_paths=many)
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertIn("too many", str(ctx.exception))
        # Zero model, zero worker: no job record was created.
        self.assertIsNone(d._read_job_record("o/r", 858))

    def test_thread_failure_records_error_and_releases(self):
        from openmainty import sched
        with mock.patch.object(d, "fetch_node",
                               return_value=self.node), \
                mock.patch.object(d, "_run_review",
                                  side_effect=RuntimeError("boom")):
            out = d.start_review("o/r", 858, by="owner")
            self.assertTrue(out["started"])
            self.assertEqual(self._wait_job(), "error")
        job = d.job_state("o/r", 858)
        self.assertIn("boom", job.get("error", ""))
        try:
            status = sched.global_lock_status() or {}
        except (Exception, SystemExit):
            status = {}
        self.assertFalse(status.get("live"))

    def test_rid_collision_never_overwrites(self):
        d._finish_job("o/r", 858, "error", "e1", "fp", None, None,
                      None, rid="fixed")
        d._finish_job("o/r", 858, "error", "e2", "fp", None, None,
                      None, rid="fixed")
        arts = sorted((d._slug_dir("o/r")).glob("review-858-fixed*.json"))
        self.assertEqual(len(arts), 2)
        texts = {a.read_text(encoding="utf-8") for a in arts}
        self.assertEqual(len(texts), 2)

    def test_artifact_failure_does_not_promote(self):
        rep = gh_report()
        with mock.patch.object(d, "_artifact_path",
                               side_effect=OSError("nope")):
            ok = d._finish_job("o/r", 858, "done", None, "fp", rep,
                               {"exit_code": 0}, rep,
                               rid="r-noart",
                               input_snapshot={"discussion": "x"},
                               prompt="p", raw_output="r")
        self.assertFalse(ok)
        state = d.review_state("o/r", 858)
        self.assertNotEqual(state.get("status"), "reviewed")
        job = d._read_job_record("o/r", 858)
        self.assertEqual(job["status"], "error")
        self.assertIn("artifact", job.get("error", ""))


class NoTargetMaintenanceTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_discussions_never_touches_issue_targets(self):
        import inspect
        src = inspect.getsource(d)
        for token in ("store.create", "ensure_branch_workdir",
                      "issue858", "issues/858", "/repos/",
                      "subprocess", "os.system", "Popen"):
            self.assertNotIn(token, src, token)
        # The single model call explicitly runs with NO workdir.
        self.assertIn("workdir=None", src)

    def test_no_public_mutation_imports(self):
        import inspect
        src = inspect.getsource(d)
        for token in ("requests.post", "urllib.request", "gh api -X POST",
                      "gh pr ", "gh issue ", "gh release create"):
            self.assertNotIn(token, src, token)


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_same_content_new_poll_same_fingerprint(self):
        first = norm_node(fetched_at="2026-10-25T00:00:00+00:00")
        second = norm_node(fetched_at="2026-10-25T01:30:00+00:00")
        self.assertEqual(d.semantic_fingerprint(first),
                         d.semantic_fingerprint(second))

    def test_votes_do_not_invalidate(self):
        first = norm_node(upvotes=3)
        second = norm_node(upvotes=412)
        self.assertEqual(d.semantic_fingerprint(first),
                         d.semantic_fingerprint(second))

    def test_edited_same_count_reply_invalidates(self):
        first = norm_node(updated="2026-10-24T02:00:00+00:00",
                          reply_body="same")
        second = norm_node(updated="2026-10-24T03:00:00+00:00",
                           reply_body="edited text here")
        self.assertNotEqual(d.semantic_fingerprint(first),
                            d.semantic_fingerprint(second))

    def test_completeness_flag_invalidates(self):
        first = norm_node(comments_complete=True)
        second = norm_node(comments_complete=False)
        self.assertNotEqual(d.semantic_fingerprint(first),
                            d.semantic_fingerprint(second))

    def test_body_change_invalidates(self):
        first = norm_node()
        second = norm_node(body="different body")
        self.assertNotEqual(d.semantic_fingerprint(first),
                            d.semantic_fingerprint(second))


class EvidenceIdentityProbeTests(unittest.TestCase):
    """P1: evidence context is review input (preflight probe 1).

    [none -> spec -> same spec -> different file]: the first review
    with a context starts a job; repeating it with the SAME context
    is a fresh-dedup no-op; a DIFFERENT context is new input that
    starts a new bounded attempt while the old report is preserved.
    """

    def setUp(self):
        make_home(self)
        self.node = norm_node()
        self.node["repo"] = "o/dt"
        self.node["url"] = "https://github.com/o/dt/discussions/858"
        self.co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        self.co.mkdir(parents=True, exist_ok=True)
        (self.co / "spec.ts").write_text("v1\n", encoding="utf-8")
        (self.co / "other.ts").write_text("w1\n", encoding="utf-8")
        from openmainty import pcconfig, repos
        repos.save({"name": "dt", "github": "o/dt",
                    "baseBranch": "main", "knowledge": [], "checks": [],
                    "e2e": [], "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["dt"] = str(self.co)
        pcconfig.save(pc)

    def _persist(self):
        def _fetch(github, number):
            d.write_json(d.node_path("o/dt", number), self.node)
            return self.node
        return _fetch

    def _lifetime_mocks(self):
        # Lifetime-wide (whole test): the worker thread runs after
        # start_review returns, so per-call `with` mocks would exit
        # before the model/fetch boundaries are hit and the worker
        # would escape into the fail-fast guards. These stay until
        # cleanup, after the worker is joined.
        from openmainty import muse as muse_mod
        fetch = mock.patch.object(d, "fetch_node",
                                  side_effect=self._persist())
        fetch.start()
        self.addCleanup(fetch.stop)
        model = mock.patch.object(
            muse_mod, "exec_prompt",
            return_value=(0, model_stream(gh_report()), {}))
        model.start()
        self.addCleanup(model.stop)

    def _start(self, paths):
        return d.start_review("o/dt", 858, by="owner",
                              repo_root=str(self.co),
                              evidence_paths=paths)

    def _join(self, number=858):
        deadline = time.time() + 30.0
        status = "running"
        while time.time() < deadline:
            status = d.job_state("o/dt", number).get("status")
            if status != "running":
                break
            time.sleep(0.1)
        for thread in threading.enumerate():
            if thread is threading.main_thread() or not thread.is_alive():
                continue
            thread.join(timeout=10)
        return status

    def test_none_then_spec_then_same_then_different(self):
        self._lifetime_mocks()
        first = self._start([])
        self.assertTrue(first["started"])
        self.assertEqual(self._join(), "done")
        # Same discussion + SAME context: a no-op, not a new model run.
        again = self._start(["spec.ts"])
        self.assertTrue(again["started"])  # new context: new input
        self.assertEqual(self._join(), "done")
        state = d.review_state("o/dt", 858)
        self.assertEqual(
            [e["path"] for e in state.get("source_evidence", [])],
            ["spec.ts"])
        same = self._start(["spec.ts"])
        self.assertFalse(same["started"])
        self.assertEqual(same["reason"], "already reviewed (fresh)")
        # A DIFFERENT file is new input: a new attempt that preserves
        # the old reviewed state until it finishes.
        other = self._start(["other.ts"])
        self.assertTrue(other["started"])
        self.assertEqual(self._join(), "done")
        state = d.review_state("o/dt", 858)
        self.assertEqual(
            [e["path"] for e in state.get("source_evidence", [])],
            ["other.ts"])
        self.assertTrue(any(
            e.get("event") == "job-done" for e in state.get("history",
                                                            [])))

    def test_request_without_checkout_is_409_not_fresh(self):
        with mock.patch.object(d, "fetch_node",
                               side_effect=self._persist()):
            with self.assertRaises(d.DiscussionError) as ctx:
                d.start_review("o/dt", 858, by="owner", repo_root=None,
                               evidence_paths=["spec.ts"])
        self.assertEqual(ctx.exception.http_status, 409)
        # And a stored review with unverifiable sources never reads
        # fresh from a rootless check.
        state = d._blank_review("o/dt", 858)
        state.update({"status": "reviewed",
                      "fingerprint": d.semantic_fingerprint(self.node),
                      "report": {"summary": "s", "findings": ["f"]},
                      "labels": [], "label_facets": {},
                      "source_evidence": [{"path": "spec.ts",
                                           "sha256": "x"}]})
        d._write_review("o/dt", 858, state)
        d.write_json(d.node_path("o/dt", 858), self.node)
        out = d.review_outcome("o/dt", 858, None)
        self.assertEqual(out["status"], "unknown")
        self.assertTrue(out["source_unavailable"])


class MalformedUpstreamProbeTests(unittest.TestCase):
    """P1: a non-object GraphQL envelope is a source error, kept data."""

    def setUp(self):
        make_home(self)
        from openmainty import cli
        self._cli = cli
        from openmainty import repos
        repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})
        d._write_snapshot("o/r", [{
            "id": "D858", "type": "discussion", "number": 858,
            "title": "kept", "category": "Ideas", "comments": 1,
            "updated_at": "2026-10-24T00:00:00+00:00"}], "old")

    def test_list_envelope_list_is_error_not_crash(self):
        with mock.patch.object(d, "_gh_graphql", return_value=["x"]), \
                mock.patch.object(d, "_gh_rest", return_value=[]), \
                mock.patch.object(self._cli, "_open_issues",
                                  return_value=[]), \
                mock.patch.object(self._cli, "_open_prs",
                                  return_value=[]):
            with self.assertRaises(d.DiscussionError) as ctx:
                d.scan_source("o/r")
        self.assertNotIsInstance(ctx.exception, AttributeError)
        # Prior items kept, degraded status surfaced.
        status = d.snapshot_status("o/r")
        self.assertEqual(status["state"], "error")
        snap = d._read_snapshot("o/r")
        titles = [i["title"] for i in snap.get("items", [])]
        self.assertIn("kept", titles)

    def test_raw_stdout_shapes_are_typed_errors_kept_data(self):
        # Probe-6: patch the RAW runner (not _gh_graphql) with a bare
        # list / string / scalar stdout. The boundary must raise a
        # typed DiscussionError — never AttributeError — and keep the
        # prior snapshot with a degraded status.
        # d._util IS openmainty.util (late binding): one patch on the
        # raw runner proves the product path, not the mock layer.
        for raw in ("[1]", '"str"', "42", "null"):
            with mock.patch.object(d._util, "run_cmd",
                                   return_value=(0, raw, "")), \
                    mock.patch.object(d, "_gh_rest", return_value=[]), \
                    mock.patch.object(self._cli, "_open_issues",
                                      return_value=[]), \
                    mock.patch.object(self._cli, "_open_prs",
                                      return_value=[]):
                with self.assertRaises(d.DiscussionError,
                                       msg=raw) as ctx:
                    d.scan_source("o/r")
            self.assertNotIsInstance(ctx.exception, AttributeError)
            self.assertEqual(ctx.exception.http_status, 500)
        self.assertEqual(d.snapshot_status("o/r")["state"], "error")
        snap = d._read_snapshot("o/r")
        self.assertIn("kept", [i["title"]
                               for i in snap.get("items", [])])

    def test_null_repository_is_error_kept_data(self):
        with mock.patch.object(
                d, "_gh_graphql",
                return_value={"data": None, "repository": None}), \
                mock.patch.object(d, "_gh_rest", return_value=[]), \
                mock.patch.object(self._cli, "_open_issues",
                                  return_value=[]), \
                mock.patch.object(self._cli, "_open_prs",
                                  return_value=[]):
            with self.assertRaises(d.DiscussionError):
                d.scan_source("o/r")
        self.assertEqual(d.snapshot_status("o/r")["state"], "error")

    def test_node_identity_mismatch_is_source_error(self):
        from openmainty import serve as serve_mod
        with mock.patch.object(d, "_gh_graphql",
                               return_value={"repository": {
                                   "discussion": {
                                       "id": "D999", "number": 999,
                                       "title": "other"}}}):
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_discussions_detail(
                    {"repo": "r", "number": "858", "confirm": True})
        self.assertEqual(ctx.exception.status, 502)


class MalformedCacheProbeTests(unittest.TestCase):
    """P1: a valid-JSON wrong-shape record is a persistent source error."""

    def setUp(self):
        make_home(self)
        from openmainty import serve as serve_mod
        self._serve = serve_mod
        from openmainty import pcconfig, repos
        repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                    "knowledge": [], "checks": [], "e2e": [],
                    "release": {}, "announce": {}})

    def test_wrong_shape_lists_no_items_with_error(self):
        d.write_json(d.snapshot_path("o/r"),
                     {"kind": "discussion-list", "items": 7})
        for _ in range(2):
            out = self._serve.api_discussions({"repo": "r"})
            self.assertEqual(out["status"]["state"], "error")
            self.assertEqual(out["items"], [])
        # Bytes preserved; still an error, never a healthy zero.
        self.assertEqual(
            d.read_json(d.snapshot_path("o/r"))["items"], 7)

    def test_successful_refresh_replaces_and_recovers(self):
        from openmainty import cli
        d.write_json(d.snapshot_path("o/r"),
                     {"kind": "discussion-list", "items": "bad"})
        page = {"repository": {
            "hasDiscussionsEnabled": True,
            "discussions": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [list_node(1)]}}}
        with mock.patch.object(d, "_gh_graphql", return_value=page), \
                mock.patch.object(d, "_gh_rest", return_value=[]), \
                mock.patch.object(cli, "_open_issues",
                                  return_value=[]), \
                mock.patch.object(cli, "_open_prs", return_value=[]):
            d.scan_source("o/r")
        out = self._serve.api_discussions({"repo": "r"})
        self.assertEqual(out["status"]["state"], "ok")
        self.assertEqual(len(out["items"]), 1)

    def test_catalog_wrong_shape_is_error_not_empty(self):
        d.write_json(d.catalog_path("o/r"), {"labels": "nope"})
        got = d.label_census("o/r")
        self.assertIn("error", got)
        self.assertEqual(got["labels"], [])


class ArtifactCollisionProbeTests(unittest.TestCase):
    """P2: an exhausted artifact namespace fails closed, never clobbers."""

    def setUp(self):
        make_home(self)

    def test_exhausted_namespace_raises_and_keeps_bytes(self):
        slug = d._slug_dir("o/r")
        slug.mkdir(parents=True, exist_ok=True)
        base = slug / "review-858-fixed.json"
        base.write_text('{"keep": true}', encoding="utf-8")
        for suffix in range(2, 100):
            (slug / f"review-858-fixed-{suffix}.json").write_text(
                '{"keep": true}', encoding="utf-8")
        with self.assertRaises(d.DiscussionError):
            d._artifact_path("o/r", 858, "fixed")
        self.assertEqual(base.read_text(encoding="utf-8"),
                         '{"keep": true}')


class CorruptBytesProbeTests(unittest.TestCase):
    """P2: a successful refresh preserves the raw corrupt input once."""

    def setUp(self):
        make_home(self)
        from openmainty import cli
        self._cli = cli

    def test_corrupt_bytes_survive_recovery_without_spam(self):
        path = d.snapshot_path("o/r")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        page = {"repository": {
            "hasDiscussionsEnabled": True,
            "discussions": {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [list_node(1)]}}}
        with mock.patch.object(d, "_gh_graphql", return_value=page), \
                mock.patch.object(d, "_gh_rest", return_value=[]), \
                mock.patch.object(self._cli, "_open_issues",
                                  return_value=[]), \
                mock.patch.object(self._cli, "_open_prs",
                                  return_value=[]):
            d.scan_source("o/r")
            d.scan_source("o/r")
        backups = sorted(path.parent.glob("snapshot.json.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"),
                         "{corrupt")


class HeartbeatProbeTests(unittest.TestCase):
    """OPERATIONAL: the model job renews the heartbeat; idle after."""

    def setUp(self):
        make_home(self)
        self.node = norm_node()
        from openmainty import sched as sched_mod
        self._sched = sched_mod

    def test_heartbeat_runs_during_job_idle_after(self):
        from openmainty import muse as muse_mod
        seen = {}
        real_start = self._sched._start_heartbeat
        real_stop = self._sched._stop_heartbeat

        def _start(owner, interval_s=15.0):
            seen["started"] = owner
            return real_start(owner, interval_s)

        def _stop(owner):
            seen["stopped"] = owner
            return real_stop(owner)

        def _fetch(github, number):
            d.write_json(d.node_path("o/r", number), self.node)
            return self.node

        with mock.patch.object(self._sched, "_start_heartbeat",
                               side_effect=_start), \
                mock.patch.object(self._sched, "_stop_heartbeat",
                                  side_effect=_stop), \
                mock.patch.object(d, "fetch_node",
                                  side_effect=_fetch), \
                mock.patch.object(muse_mod, "exec_prompt",
                                  return_value=(
                                      0, model_stream(gh_report()),
                                      {})):
            out = d.start_review("o/r", 858, by="owner")
            self.assertTrue(out["started"])
            token = out["job"]["lease_owner"]
            self.assertEqual(seen.get("started"), token)
            deadline = time.time() + 30.0
            while time.time() < deadline:
                if d.job_state("o/r", 858).get("status") != "running":
                    break
                time.sleep(0.1)
            for thread in threading.enumerate():
                if thread is threading.main_thread() or not thread.is_alive():
                    continue
                thread.join(timeout=10)
        self.assertEqual(d.job_state("o/r", 858).get("status"), "done")
        self.assertEqual(seen.get("stopped"), token)
        try:
            live = (self._sched.global_lock_status() or {}).get("live")
        except (Exception, SystemExit):
            live = False
        self.assertFalse(live)


class SentBoundaryProbeTests(unittest.TestCase):
    """Root probe-3: citations are checked against SENT bytes only.

    Four files gathered, prompt budget sends fewer: a report citing an
    unsent file is rejected (real worker, json.dumps-built stream), a
    report citing a sent file passes, and the artifact preserves the
    requested-vs-sent-vs-excluded scope.
    """

    def setUp(self):
        make_home(self)
        self.node = norm_node()
        self.node["repo"] = "o/dt"
        self.node["url"] = "https://github.com/o/dt/discussions/858"
        self.co = Path(os.environ["OPENMAINTY_HOME"]) / "co"
        self.co.mkdir(parents=True, exist_ok=True)
        for name in ("f1.ts", "f2.ts", "f3.ts", "f4.ts"):
            (self.co / name).write_text("x" * 20 + "\n",
                                        encoding="utf-8")
        from openmainty import pcconfig, repos
        repos.save({"name": "dt", "github": "o/dt",
                    "baseBranch": "main", "knowledge": [], "checks": [],
                    "e2e": [], "release": {}, "announce": {}})
        pc = pcconfig.load()
        pc["checkouts"]["dt"] = str(self.co)
        pcconfig.save(pc)
        fetch = mock.patch.object(
            d, "fetch_node",
            side_effect=self._persisting_fetch())
        fetch.start()
        self.addCleanup(fetch.stop)

    def _persisting_fetch(self):
        def _fetch(github, number):
            d.write_json(d.node_path("o/dt", number), self.node)
            return self.node
        return _fetch

    def _start(self, report, paths):
        # Lifetime-wide for the whole attempt: the worker runs after
        # start returns, so cap/model/fetch mocks are stopped only at
        # cleanup, after the join below.
        from openmainty import muse as muse_mod
        cap = mock.patch.object(d, "_PROMPT_SOURCE_TOTAL_CAP", 25)
        cap.start()
        self.addCleanup(cap.stop)
        model = mock.patch.object(
            muse_mod, "exec_prompt",
            return_value=(0, model_stream(report), {}))
        model.start()
        self.addCleanup(model.stop)
        return d.start_review("o/dt", 858, by="owner",
                              repo_root=str(self.co),
                              evidence_paths=paths)

    def _join(self, number=858):
        deadline = time.time() + 30.0
        status = "running"
        while time.time() < deadline:
            status = d.job_state("o/dt", number).get("status")
            if status != "running":
                break
            time.sleep(0.1)
        for thread in threading.enumerate():
            if thread is threading.main_thread() or not thread.is_alive():
                continue
            thread.join(timeout=10)
        return status

    def test_validate_rejects_unsent_file_directly(self):
        sent = [{"path": "f1.ts", "sha256": "s1",
                 "bytes_sent": 21, "bytes_total": 21}]
        rep = gh_report(source_files=["f4.ts"])
        problems = d.validate_report(self.node, rep, sent, None)
        self.assertTrue(any("not sent" in p for p in problems),
                        problems)

    def test_worker_rejects_unsent_citation(self):
        out = self._start(gh_report(source_files=["f4.ts"]),
                          ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        self.assertTrue(out["started"])
        self.assertEqual(self._join(), "error")
        job = d.job_state("o/dt", 858)
        self.assertIn("not sent", job.get("error", ""))
        # Nothing promoted: no verified review from an unsent cite.
        self.assertNotEqual(
            d.review_state("o/dt", 858).get("status"), "reviewed")
        # The attempt artifact preserves what the model saw: sent
        # files only, with the excluded remainder listed + reason.
        arts = sorted(d._slug_dir("o/dt").glob("review-858-*.json"))
        self.assertTrue(arts)
        art = d.read_json(arts[-1])
        snap = art["input_snapshot"]
        self.assertLessEqual(len(snap["sources"]), 3)
        self.assertNotIn("f4.ts", [s.get("path")
                                   for s in snap["sources"]])
        self.assertIn("f4.ts", [s.get("path")
                                for s in snap["sources_excluded"]])
        self.assertEqual(sorted(snap["sources_requested"]),
                         ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        self.assertNotIn("f4.ts", art["prompt"])
        self.assertTrue(art["partial"])

    def test_worker_accepts_sent_citation_with_scope(self):
        out = self._start(gh_report(source_files=["f1.ts"]),
                          ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        self.assertTrue(out["started"])
        self.assertEqual(self._join(), "done")
        state = d.review_state("o/dt", 858)
        self.assertEqual(state["status"], "reviewed")
        self.assertEqual(state["source_cited"], ["f1.ts"])
        scope = state["source_scope"]
        self.assertEqual(sorted(scope["requested"]),
                         ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        self.assertIn("f1.ts", scope["sent"])
        self.assertNotIn("f4.ts", scope["sent"])
        self.assertIn("f4.ts", [s.get("path")
                                for s in scope["excluded"]])
        out = d.review_outcome("o/dt", 858, str(self.co))
        self.assertEqual(out["status"], "reviewed")
        self.assertEqual(out["source_cited"], ["f1.ts"])
        self.assertTrue(out["source_checked"])
        self.assertFalse(out["source_changed"])
        self.assertTrue(any(e.get("path") == "f1.ts" and e.get("sha256")
                            for e in out["source_current"]))

    def test_duplicate_partial_request_dedups_without_model(self):
        # Probe-5: the same requested [f1..f4] (f3/f4 excluded by the
        # byte budget) with unchanged hashes dedups — no second model
        # call — while a changed excluded file is new input.
        from openmainty import muse as muse_mod
        out = self._start(gh_report(source_files=["f1.ts"]),
                          ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        self.assertTrue(out["started"])
        self.assertEqual(self._join(), "done")
        state = d.review_state("o/dt", 858)
        self.assertEqual(
            sorted(e["path"] for e in state.get("source_requested",
                                                [])),
            ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        # Any model call here would spend quota on identical input:
        # fail loudly instead of counting.
        with mock.patch.object(
                muse_mod, "exec_prompt",
                side_effect=AssertionError(
                    "duplicate partial input must not run the model")):
            same = d.start_review("o/dt", 858, by="owner",
                                  repo_root=str(self.co),
                                  evidence_paths=["f1.ts", "f2.ts",
                                                  "f3.ts", "f4.ts"])
        self.assertFalse(same["started"])
        self.assertEqual(same["reason"], "already reviewed (fresh)")
        # Changing an excluded file is new input: a new attempt runs.
        (self.co / "f3.ts").write_text("y" * 20 + "\n",
                                       encoding="utf-8")
        out = self._start(gh_report(source_files=["f1.ts"]),
                          ["f1.ts", "f2.ts", "f3.ts", "f4.ts"])
        self.assertTrue(out["started"])
        self.assertEqual(self._join(), "done")
        # Removing a requested file is new input too.
        out = self._start(gh_report(source_files=["f1.ts"]),
                          ["f1.ts", "f2.ts", "f4.ts"])
        self.assertTrue(out["started"])
        self.assertEqual(self._join(), "done")

    def test_legacy_record_without_requested_identity_stays_fresh(self):
        # A legacy reviewed record (sent proof only, no requested
        # identity) with the identical sent context still dedups.
        node = norm_node()
        fp = d.semantic_fingerprint(node)
        d.write_json(d.node_path("o/r", 858), node)
        state = d._blank_review("o/r", 858)
        state.update({"status": "reviewed", "fingerprint": fp,
                      "report": {"summary": "s", "findings": ["f"]},
                      "labels": [], "label_facets": {},
                      "source_evidence": []})
        d._write_review("o/r", 858, state)
        with mock.patch.object(d, "fetch_node", return_value=node):
            out = d.start_review("o/r", 858, by="owner")
        self.assertFalse(out["started"])
        self.assertEqual(out["reason"], "already reviewed (fresh)")


class UnicodeBytePromptTests(unittest.TestCase):
    """Probe-4: byte-exact prompt accounting for multibyte content.

    One 22,000-char Korean source (66,000 UTF-8 bytes) against the
    20,000-byte per-file cap: exactly 20,000 bytes are sent, the review
    is marked partial with the exact included context, and every byte
    label is a real UTF-8 byte count.
    """

    def setUp(self):
        make_home(self)
        self.node = norm_node()

    def test_single_unicode_file_truncated_marks_partial(self):
        body = "가" * 22000
        total = len(body.encode("utf-8"))
        self.assertEqual(total, 66000)
        built = d._build_review_prompt(
            self.node, [{"path": "u.ts", "sha256": "s" * 64,
                         "content": body}])
        snap = built["input_snapshot"]
        self.assertEqual(len(snap["sources"]), 1)
        sent = snap["sources"][0]
        self.assertEqual(sent["bytes_total"], 66000)
        # 20,000 raw bytes hold 6,666 whole chars; the cut tail's 2
        # partial bytes are dropped, so 19,998 honest sent bytes.
        self.assertEqual(sent["bytes_sent"], 19998)
        self.assertTrue(sent["truncated"])
        # The excerpt is exactly the sent bytes, cleanly decoded.
        self.assertEqual(len(sent["excerpt"].encode("utf-8")), 19998)
        self.assertEqual(sent["excerpt"], "가" * 6666)
        self.assertIn("u.ts", built["prompt"])
        self.assertNotIn("가" * 20000, built["prompt"])
        self.assertTrue(built["partial"])
        self.assertTrue(any("u.ts" in p and "19998" in p and "66000" in p
                            for p in built["partial"]),
                        built["partial"])
        self.assertEqual(snap["discussion_bytes_total"],
                         len(json.dumps(
                             d.semantic_input(self.node),
                             ensure_ascii=False,
                             indent=1).encode("utf-8")))
        self.assertEqual(snap["sources_requested"], ["u.ts"])
        self.assertEqual(snap["sources_excluded"], [])
