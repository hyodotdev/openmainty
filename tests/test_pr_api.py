"""PR API routes: cached reads, guarded POSTs, no network.

Serve functions are the actual route handlers (payload in, dict out).
Offline: gh/model/lease boundaries are canned; no real home/target.
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
from openmainty import pr_reviews as r
from openmainty import serve as serve_mod


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

    def _no_model(*args, **kwargs):
        raise AssertionError("unexpected model call in offline test")

    from openmainty import muse as muse_mod
    net = mock.patch.object(p._util, "run_cmd", side_effect=_no_network)
    net.start()
    test.addCleanup(net.stop)
    mod = mock.patch.object(muse_mod, "exec_prompt", side_effect=_no_model)
    mod.start()
    test.addCleanup(mod.stop)
    # Offline hermeticity: these tests verify list/review/lease
    # behavior, not the subscription gate. The gate reads ambient
    # login/credential state and would 403 under a bare env, so it
    # stays open here (no test in this module asserts the gate).
    from openmainty import chat as chat_mod
    gate = mock.patch.object(chat_mod, "subscription_error",
                             return_value=None)
    gate.start()
    test.addCleanup(gate.stop)
    return Path(tmp.name)


def fake_profile():
    from openmainty import pcconfig, repos
    repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                "knowledge": [], "checks": [], "e2e": [],
                "release": {}, "announce": {}})


def list_row(**kw):
    row = {"number": 857, "title": "T <b>x</b>", "author": "c",
           "bot": False, "is_draft": False, "head": "fix",
           "closes": [344], "additions": 10, "deletions": 2, "files": 1,
           "created_at": "2026-10-01T00:00:00Z",
           "updated_at": "2026-10-20T00:00:00Z"}
    row.update(kw)
    return row


def stored_snapshot(**kw):
    snap = {
        "schema": 1, "repo": "o/r", "kind": "pull-request", "number": 857,
        "head_sha": HEAD40, "base_sha": BASE40,
        "fetched_at": "2026-10-26T00:00:00+00:00",
        "detail": {
            "number": 857, "title": "T", "body": "B",
            "author": "c", "state": "OPEN", "is_draft": False,
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-20T00:00:00Z",
            "closed_at": None, "merged_at": None,
            "head_ref": "fix", "base_ref": "main",
            "head_sha": HEAD40, "base_sha": BASE40,
            "url": "https://github.com/o/r/pull/857",
            "labels": [], "labels_note": None,
            "comment_count": 0, "comments": [], "comments_complete": True,
            "comments_note": None, "reviews": [], "reviews_complete": True,
            "reviews_note": None, "closes": [344],
            "additions": 10, "deletions": 2, "changed_files": 1,
            "files": [{"path": "src/a.ts", "additions": 10,
                       "deletions": 2}],
            "files_note": None,
        },
        "diff": "d", "diff_bytes_total": 1, "diff_truncated": False,
        "checks": [], "checks_truncated": False,
        "threads": [], "partial": [],
    }
    snap.update(kw)
    return snap


class TestPrList(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()

    def test_list_without_snapshot_is_unknown_needing_fetch(self):
        with mock.patch.object(serve_mod.sched, "load",
                               return_value={"last_prs": [list_row()]}):
            out = serve_mod.api_prs({"repo": "r"})
        self.assertEqual(len(out["items"]), 1)
        row = out["items"][0]
        self.assertEqual(row["title"], "T <b>x</b>")
        # No stored snapshot: not a proven-safe unreviewed assessment.
        self.assertEqual(row["review"]["status"], "unknown")
        self.assertTrue(row["review"]["partial"])
        self.assertEqual(row["review"]["job_status"], "idle")
        self.assertIn("model", out)

    def test_list_missing_scan_state_is_empty_not_error(self):
        out = serve_mod.api_prs({"repo": "r"})
        self.assertEqual(out["items"], [])

    def test_list_requires_repo(self):
        with self.assertRaises(serve_mod.ApiError):
            serve_mod.api_prs({"repo": ""})


class TestPrDetail(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()

    def test_detail_returns_snapshot_and_review(self):
        p.store_snapshot("o/r", stored_snapshot())
        out = serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        self.assertEqual(out["snapshot"]["head_sha"], HEAD40)
        self.assertEqual(out["review"]["status"], "unreviewed")

    def test_detail_404_without_snapshot(self):
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        self.assertEqual(ctx.exception.status, 404)

    def test_detail_502_on_corrupt_bytes_kept(self):
        path = p.pr_path("o/r", 857)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(path.read_text(encoding="utf-8"), "{corrupt")

    def test_attempt_roundtrip(self):
        from openmainty.util import write_json as _wj
        # Identity requires the stored record's own rid; an error
        # record carries an explicit reason, never a report.
        _wj(p.attempt_path("o/r", 857, "rid-1"),
            {"repo": "o/r", "number": 857, "rid": "rid-1",
             "outcome": "error", "error": "boom"})
        out = serve_mod.api_prs_attempt(
            {"repo": "r", "number": "857", "artifact": "rid-1"})
        self.assertEqual(out["attempt"]["outcome"], "error")
        self.assertEqual(out["attempt"]["rid"], "rid-1")
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "nope"})
        self.assertEqual(ctx.exception.status, 404)

    def test_attempt_identity_mismatch_is_502(self):
        from openmainty.util import write_json as _wj
        # Otherwise-valid reviewed record: only the stored number
        # differs from the requested PR.
        raw = dict(stored_attempt_doc("rid-x"), number=858)
        _wj(p.attempt_path("o/r", 857, "rid-x"), raw)
        before = p.attempt_path("o/r", 857, "rid-x").read_bytes()
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-x"})
        self.assertEqual(ctx.exception.status, 502)
        # Rejected bytes stay in place, exactly unmodified.
        self.assertEqual(p.attempt_path("o/r", 857, "rid-x").read_bytes(),
                         before)

    def test_attempt_wrong_rid_is_502(self):
        from openmainty.util import write_json as _wj
        raw = {"repo": "o/r", "number": 857, "rid": "rid-else",
               "outcome": "error", "error": "x"}
        _wj(p.attempt_path("o/r", 857, "rid-1"), raw)
        before = p.attempt_path("o/r", 857, "rid-1").read_bytes()
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-1"})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(p.attempt_path("o/r", 857, "rid-1").read_bytes(),
                         before)

    def test_attempt_missing_outcome_is_502(self):
        from openmainty.util import write_json as _wj
        _wj(p.attempt_path("o/r", 857, "rid-y"),
            {"repo": "o/r", "number": 857, "rid": "rid-y"})
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-y"})
        self.assertEqual(ctx.exception.status, 502)

    def test_attempt_invented_outcome_is_502(self):
        from openmainty.util import write_json as _wj
        # A fabricated success shape must be rejected even though it
        # carries a report: only the finite outcome contract counts.
        raw = {"repo": "o/r", "number": 857, "rid": "rid-z",
               "outcome": "invented-success",
               "report": {"summary": "s"}, "attempt_fp": "a" * 64}
        _wj(p.attempt_path("o/r", 857, "rid-z"), raw)
        before = p.attempt_path("o/r", 857, "rid-z").read_bytes()
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-z"})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(p.attempt_path("o/r", 857, "rid-z").read_bytes(),
                         before)

    def test_attempt_reviewed_without_report_is_502(self):
        from openmainty.util import write_json as _wj
        _wj(p.attempt_path("o/r", 857, "rid-w"),
            {"repo": "o/r", "number": 857, "rid": "rid-w",
             "outcome": "reviewed", "attempt_fp": "a" * 64})
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-w"})
        self.assertEqual(ctx.exception.status, 502)

    def test_reviewed_outcome_exposes_success_history(self):
        from openmainty.util import write_json as _wj
        snap = stored_snapshot()
        p.store_snapshot("o/r", snap)
        # Legacy failed attempt already recorded before this success.
        _wj(p.review_path("o/r", 857),
            {"status": "reviewed", "attempt_fp": "a" * 64,
             "semantic_fp": r.content_fp(snap),
             "history": [{"at": "2026-10-26T00:00:00+00:00",
                          "event": "review-failed", "rid": "rid-old"}]})
        rep = {"summary": "s", "findings": ["f"],
               "labels": {"status": ["needs-review"],
                          "platform": ["ios"], "type": ["bug"]},
               "evidence": [{"url": "https://github.com/o/r/pull/857"}],
               "reply_draft": None, "source_files": ["src/a.ts"],
               "recommendation": "needs-info",
               "recommendation_basis": "b"}
        r._finish_job(github="o/r", number=857, outcome="reviewed",
                      error=None, semantic_fp=r.content_fp(snap),
                      attempt_fp="a" * 64, report=rep, usage=None,
                      input_snapshot={"source_census": {
                          "delivered": [{"path": "src/a.ts",
                                         "head_sha256": "h" * 64,
                                         "head_sent": 1,
                                         "head_sent_sha256": "x" * 64,
                                         "base_sha256": "b" * 64,
                                         "base_sent": 1,
                                         "base_sent_sha256": "y" * 64}]}},
                      rid="0826")
        out = serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        self.assertEqual(out["review"]["status"], "reviewed")
        rids = [h.get("rid") for h in out["review"]["history"]]
        self.assertIn("rid-old", rids)
        self.assertIn("0826", rids)


def stored_attempt_doc(rid, outcome="reviewed", afp="b" * 64):
    rep = {"summary": "s", "findings": ["f"],
           "labels": {"status": ["needs-review"], "platform": ["ios"],
                      "type": ["bug"]},
           "evidence": [{"url": "https://github.com/o/r/pull/857"}],
           "reply_draft": None, "source_files": ["src/a.ts"],
           "recommendation": "needs-info", "recommendation_basis": "b"}
    return {"repo": "o/r", "number": 857, "rid": rid, "outcome": outcome,
            "error": None if outcome == "reviewed" else "x",
            "semantic_fp": "s" * 64, "attempt_fp": afp,
            "head_sha": HEAD40, "base_sha": BASE40,
            "requested_paths": ["src/a.ts"], "finished_at": "t",
            "usage": None, "input_snapshot": {}, "prompt": "p",
            "raw_output": "", "raw_output_meta": {}, "partial": [],
            "report": rep}


def reviewed_state_doc(history):
    return {"status": "reviewed", "attempt_fp": "a" * 64,
            "report": stored_attempt_doc("x")["report"],
            "labels": {"status": ["needs-review"], "platform": ["ios"],
                       "type": ["bug"]},
            "partial": False, "history": history}


class TestPrHistory(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()

    def test_overwrite_preserves_old_attempt_bytes(self):
        from openmainty.util import write_json as _wj
        snap = stored_snapshot()
        fp = r.content_fp(snap)
        p.store_snapshot("o/r", snap)
        legacy = stored_attempt_doc("rid-legacy")
        new = stored_attempt_doc("rid-new", afp="c" * 64)
        _wj(p.attempt_path("o/r", 857, "rid-legacy"), legacy)
        _wj(p.attempt_path("o/r", 857, "rid-new"), new)
        _wj(p.review_path("o/r", 857), dict(
            reviewed_state_doc([{"at": "t", "event": "review-reviewed",
                                 "rid": "rid-legacy"}]),
            semantic_fp=fp))
        r._write_job("o/r", 857,
            {"status": "done", "finished_at": "t2", "rid": "rid-new",
             "semantic_fp": fp, "attempt_fp": "c" * 64})
        legacy_before = p.attempt_path(
            "o/r", 857, "rid-legacy").read_bytes()
        out = serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        rids = [h.get("rid") for h in out["review"]["history"]]
        self.assertIn("rid-legacy", rids)
        self.assertIn("rid-new", rids)
        # Original bytes are untouched by the later job.
        self.assertEqual(p.attempt_path(
            "o/r", 857, "rid-legacy").read_bytes(), legacy_before)

    def test_corrupt_artifact_is_error_with_bytes_kept(self):
        from openmainty.util import write_json as _wj
        snap = stored_snapshot()
        p.store_snapshot("o/r", snap)
        p.attempt_path("o/r", 857, "rid-bad").parent.mkdir(
            parents=True, exist_ok=True)
        p.attempt_path("o/r", 857, "rid-bad").write_text(
            "{corrupt", encoding="utf-8")
        _wj(p.review_path("o/r", 857), dict(
            reviewed_state_doc([{"at": "t", "event": "review-reviewed",
                                 "rid": "rid-bad"}]),
            semantic_fp=r.content_fp(snap)))
        r._write_job("o/r", 857,
            {"status": "done", "finished_at": "t", "rid": "rid-bad"})
        out = serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        rows = [h for h in out["review"]["history"]
                if h.get("rid") == "rid-bad"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "attempt-unreadable")
        self.assertEqual(p.attempt_path("o/r", 857, "rid-bad").read_text(
            encoding="utf-8"), "{corrupt")
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-bad"})
        self.assertEqual(ctx.exception.status, 502)

    def test_foreign_artifact_is_error(self):
        from openmainty.util import write_json as _wj
        snap = stored_snapshot()
        p.store_snapshot("o/r", snap)
        foreign = dict(stored_attempt_doc("rid-f"), repo="o/other")
        _wj(p.attempt_path("o/r", 857, "rid-f"), foreign)
        _wj(p.review_path("o/r", 857), dict(
            reviewed_state_doc([{"at": "t", "event": "review-reviewed",
                                 "rid": "rid-f"}]),
            semantic_fp=r.content_fp(snap)))
        r._write_job("o/r", 857,
            {"status": "done", "finished_at": "t", "rid": "rid-f"})
        before = p.attempt_path("o/r", 857, "rid-f").read_bytes()
        out = serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        rows = [h for h in out["review"]["history"]
                if h.get("rid") == "rid-f"]
        self.assertEqual(rows[0]["event"], "attempt-unreadable")
        self.assertEqual(p.attempt_path("o/r", 857, "rid-f").read_bytes(),
                         before)
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": "rid-f"})
        self.assertEqual(ctx.exception.status, 502)


class TestPrMalformedContracts(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()

    def _store(self, rid, mutate):
        from openmainty.util import write_json as _wj
        doc = stored_attempt_doc(rid)
        mutate(doc)
        _wj(p.attempt_path("o/r", 857, rid), doc)
        return p.attempt_path("o/r", 857, rid).read_bytes()

    def _refuses(self, rid, before):
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_attempt(
                {"repo": "r", "number": "857", "artifact": rid})
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(p.attempt_path("o/r", 857, rid).read_bytes(),
                         before)

    def test_untyped_outcome_is_502_not_crash(self):
        before = self._store("rid-e1",
                             lambda d: d.update(outcome=[]))
        self._refuses("rid-e1", before)

    def test_untyped_recommendation_is_502_not_crash(self):
        def mutate(doc):
            doc["report"]["recommendation"] = {}
        before = self._store("rid-e2", mutate)
        self._refuses("rid-e2", before)

    def test_blank_summary_basis_finding_url_rejected(self):
        variants = [
            lambda d: d["report"].update(summary="  "),
            lambda d: d["report"].update(recommendation_basis=" "),
            lambda d: d["report"].update(findings=["  "]),
            lambda d: d["report"].update(evidence=[{"url": "  "}]),
            lambda d: d["report"].update(source_files=[{}]),
            lambda d: d["report"].pop("reply_draft"),
            lambda d: d["report"].update(recommendation_basis=None),
        ]
        for i, mutate in enumerate(variants):
            rid = "rid-b%d" % i
            before = self._store(rid, mutate)
            with self.assertRaises(serve_mod.ApiError) as ctx:
                serve_mod.api_prs_attempt(
                    {"repo": "r", "number": "857", "artifact": rid})
            self.assertEqual(ctx.exception.status, 502, rid)
            self.assertEqual(p.attempt_path("o/r", 857, rid).read_bytes(),
                             before)

    def test_untyped_outcome_in_history_is_error_row(self):
        from openmainty.util import write_json as _wj
        snap = stored_snapshot()
        p.store_snapshot("o/r", snap)
        doc = stored_attempt_doc("rid-eh")
        doc["outcome"] = []
        _wj(p.attempt_path("o/r", 857, "rid-eh"), doc)
        _wj(p.review_path("o/r", 857), dict(
            reviewed_state_doc([{"at": "t", "event": "review-reviewed",
                                 "rid": "rid-eh"}]),
            semantic_fp=r.content_fp(snap)))
        r._write_job("o/r", 857,
                     {"status": "done", "finished_at": "t",
                      "rid": "rid-eh"})
        out = serve_mod.api_prs_detail({"repo": "r", "number": "857"})
        rows = [h for h in out["review"]["history"]
                if h.get("rid") == "rid-eh"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "attempt-unreadable")


class TestPrPostGuards(unittest.TestCase):
    def setUp(self):
        make_home(self)
        fake_profile()

    def test_fetch_requires_confirm(self):
        with self.assertRaises(serve_mod.ApiError) as ctx:
            serve_mod.api_prs_fetch({"repo": "r", "number": "857"})
        self.assertEqual(ctx.exception.status, 400)

    def test_review_requires_owner_and_confirm(self):
        with self.assertRaises(serve_mod.ApiError):
            serve_mod.api_prs_review({"repo": "r", "number": "857",
                                      "by": "worker", "confirm": True})
        with self.assertRaises(serve_mod.ApiError):
            serve_mod.api_prs_review({"repo": "r", "number": "857",
                                      "by": "owner"})

    def test_review_rejects_evidence_shape(self):
        with self.assertRaises(serve_mod.ApiError):
            serve_mod.api_prs_review({"repo": "r", "number": "857",
                                      "by": "owner", "confirm": True,
                                      "evidence": "nope"})
        with self.assertRaises(serve_mod.ApiError):
            serve_mod.api_prs_review({"repo": "r", "number": "857",
                                      "by": "owner", "confirm": True,
                                      "evidence": ["../escape"]})
        with self.assertRaises(serve_mod.ApiError):
            serve_mod.api_prs_review(
                {"repo": "r", "number": "857", "by": "owner",
                 "confirm": True,
                 "evidence": [f"f{i}.ts" for i in range(30)]})

    def test_review_busy_is_409_nothing_started(self):
        import threading as _th
        from openmainty import sched as _sched
        held = _th.Event()
        release = _th.Event()

        def _holder():
            token = _sched.acquire_worker_lease("pr-review:o/r#857")
            held.set()
            release.wait(timeout=30)
            if token is not None:
                _sched.release_worker_lease(token)

        holder = _th.Thread(target=_holder, daemon=True)
        holder.start()
        self.assertTrue(held.wait(timeout=10), "holder never ran")
        try:
            snap = stored_snapshot()
            with mock.patch.object(p, "fetch_pr_detail",
                                   return_value=snap):
                with mock.patch.object(p, "store_snapshot",
                                       side_effect=lambda s, x: x):
                    with mock.patch.object(
                            r, "gather_pr_sources",
                            return_value=[{
                                "path": "src/a.ts", "status": "ok",
                                "head": {"ref": HEAD40, "status": "ok",
                                         "content": "c", "sha256": "h" * 64,
                                         "bytes_total": 1},
                                "base": {"ref": BASE40,
                                         "status": "SourceUnreadable",
                                         "reason": "x"}}]):
                        with self.assertRaises(serve_mod.ApiError) as ctx:
                            serve_mod.api_prs_review(
                                {"repo": "r", "number": "857", "by": "owner",
                                 "confirm": True, "evidence": []})
            self.assertEqual(ctx.exception.status, 409)
            self.assertEqual(r.job_state("o/r", 857)["status"], "idle")
            self.assertFalse(p.job_path("o/r", 857).exists())
        finally:
            release.set()
            holder.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
