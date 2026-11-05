"""PR reviews: input binding, validation, worker lifecycle.

Offline: gh/model/lease boundaries are canned per test; failures are
preserved by assertions, never retried. No real home/target use.
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


def snapshot(**kw):
    snap = {
        "schema": 1, "repo": "o/r", "kind": "pull-request", "number": 857,
        "head_sha": HEAD40, "base_sha": BASE40,
        "detail": {
            "number": 857, "title": "T", "body": "B body text",
            "author": "c", "state": "OPEN", "is_draft": False,
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-20T00:00:00Z",
            "closed_at": None, "merged_at": None,
            "head_ref": "fix", "base_ref": "main",
            "url": "https://github.com/o/r/pull/857",
            "labels": ["bug"], "labels_note": None,
            "comment_count": 0, "comments": [],
            "comments_complete": True, "comments_note": None,
            "reviews": [], "reviews_complete": True, "reviews_note": None,
            "closes": [344], "additions": 10, "deletions": 2,
            "changed_files": 1,
            "files": [{"path": "src/a.ts", "additions": 10,
                       "deletions": 2}],
            "files_note": None,
        },
        "diff": "--- a/src/a.ts\n+++ b/src/a.ts\n",
        "diff_bytes_total": 26, "diff_truncated": False,
        "checks": [{"name": "ci", "state": "SUCCESS", "description": "",
                    "link": ""}],
        "checks_truncated": False,
        "threads": [], "partial": [],
    }
    snap.update(kw)
    return snap


def ok_sources(paths=("src/a.ts",), head_sha=HEAD40, base_sha=BASE40):
    return [{"path": rel, "status": "ok",
             "head": {"ref": head_sha, "status": "ok",
                      "content": "head-code", "sha256": "h" * 64,
                      "bytes_total": 9},
             "base": {"ref": base_sha, "status": "ok",
                      "content": "base-code", "sha256": "b" * 64,
                      "bytes_total": 9}}
            for rel in paths]


def good_report(url="https://github.com/o/r/pull/857", path="src/a.ts",
                rec="needs-changes"):
    return {"summary": "s", "findings": ["f"],
            "labels": {"status": ["needs-review"], "platform": ["ios"],
                       "type": ["bug"]},
            "evidence": [{"url": url}], "reply_draft": None,
            "source_files": [path], "recommendation": rec,
            "recommendation_basis": "basis citing delivered code"}


class TestGather(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def _contents(self, mapping):
        def fake_run_cmd(argv, timeout=60):
            self.assertEqual(argv[:3], ["gh", "api", "--method"])
            self.assertEqual(argv[3], "GET")
            endpoint = argv[4]
            self.assertTrue(endpoint.startswith("repos/o/r/contents/"))
            import base64
            rel = endpoint.split("/contents/", 1)[1]
            if rel not in mapping:
                return 0, json.dumps({"message": "Not Found"}), ""
            import hashlib
            blob = mapping[rel].encode("utf-8")
            return 0, json.dumps({"content": base64.b64encode(blob).decode(),
                                  "sha": "x", "encoding": "base64"}), ""
        m = mock.patch.object(p._util, "run_cmd", side_effect=fake_run_cmd)
        m.start()
        self.addCleanup(m.stop)

    def test_head_and_base_delivered(self):
        self._contents({"src/a.ts": "code!"})
        out = r.gather_pr_sources("o/r", HEAD40, BASE40, ["src/a.ts"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[0]["head"]["content"], "code!")
        self.assertEqual(out[0]["base"]["content"], "code!")
        self.assertEqual(len(out[0]["head"]["sha256"]), 64)

    def test_every_identity_recorded(self):
        self._contents({})
        out = r.gather_pr_sources(
            "o/r", HEAD40, BASE40,
            ["src/a.ts", "/abs", "src/a.ts", "missing.ts"])
        by_path = {}
        for e in out:
            by_path.setdefault(e.get("path"), []).append(e)
        self.assertEqual(len(out), 4)
        self.assertEqual(by_path["/abs"][0]["status"], "SourceUnreadable")
        self.assertEqual(by_path["src/a.ts"][1]["status"], "duplicate")
        self.assertEqual(by_path["missing.ts"][0]["status"],
                         "SourceUnreadable")

    def test_no_silent_skip(self):
        self._contents({})
        out = r.gather_pr_sources("o/r", HEAD40, BASE40,
                                  [f"f{i}.ts" for i in range(30)])
        self.assertEqual(len([e for e in out if e.get("path")]), 30)
        self.assertTrue(any(e.get("path") is None for e in out))


class TestGatherBounds(unittest.TestCase):
    def setUp(self):
        make_home(self)
        import base64 as _b64

        def fake_run_cmd(argv, timeout=60):
            endpoint = argv[4]
            rel = endpoint.split("/contents/", 1)[1]
            bodies = {"big.ts": "x" * 21000, "huge.ts": "y" * 600000,
                      "new.ts": "new-code", "old.ts": "old-code",
                      "gone.ts": "gone-code", "src/a.ts": "code!"}
            ref = None
            for part in argv:
                if str(part).startswith("ref="):
                    ref = str(part)[4:]
            if rel == "new.ts" and ref == BASE40:
                return 0, json.dumps({"message": "Not Found"}), ""
            if rel in ("old.ts", "gone.ts") and ref == HEAD40:
                return 0, json.dumps({"message": "Not Found"}), ""
            if rel not in bodies:
                return 0, json.dumps({"message": "Not Found"}), ""
            import hashlib
            blob = bodies[rel].encode("utf-8")
            return 0, json.dumps(
                {"content": _b64.b64encode(blob).decode(),
                 "sha": hashlib.sha256(blob).hexdigest(),
                 "encoding": "base64"}), ""

        m = mock.patch.object(p._util, "run_cmd", side_effect=fake_run_cmd)
        m.start()
        self.addCleanup(m.stop)

    def test_valid_21kb_is_readable_but_partially_delivered(self):
        out = r.gather_pr_sources("o/r", HEAD40, BASE40, ["big.ts"])
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[0]["head"]["bytes_total"], 21000)
        built = r.build_pr_prompt(snapshot(), out)
        census = built["input_snapshot"]["source_census"]
        self.assertEqual(census["delivered"][0]["head_sent"], 20000)
        self.assertTrue(census["delivered"][0]["head_truncated"])
        self.assertTrue(built["truncated"])
        self.assertTrue(any("truncat" in note for note in built["partial"]))

    def test_over_readable_cap_refuses(self):
        out = r.gather_pr_sources("o/r", HEAD40, BASE40, ["huge.ts"])
        self.assertEqual(out[0]["status"], "SourceUnreadable")
        self.assertIn("readable-file cap", out[0]["reason"])

    def test_base_missing_with_adds_only_still_refuses(self):
        meta = {"new.ts": {"additions": 5, "deletions": 0}}
        out = r.gather_pr_sources("o/r", HEAD40, BASE40, ["new.ts"], meta)
        self.assertEqual(out[0]["status"], "SourceUnreadable")
        self.assertIn("lifecycle unknown", out[0]["reason"])
        built = r.build_pr_prompt(snapshot(), out)
        delivered = built["input_snapshot"]["source_census"]["delivered"]
        self.assertEqual(delivered, [])
        omitted = built["input_snapshot"]["source_census"]["omitted"]
        self.assertTrue(any(o.get("path") == "new.ts" for o in omitted))

    def test_head_missing_with_dels_only_still_refuses(self):
        meta = {"gone.ts": {"additions": 0, "deletions": 4}}
        out = r.gather_pr_sources("o/r", HEAD40, BASE40, ["gone.ts"], meta)
        self.assertEqual(out[0]["status"], "SourceUnreadable")
        self.assertIn("lifecycle unknown", out[0]["reason"])

    def test_start_refuses_when_requested_side_missing(self):
        with mock.patch.object(p, "fetch_pr_detail",
                               return_value=snapshot()):
            with mock.patch.object(p, "store_snapshot",
                                   side_effect=lambda s, x: x):
                def fake_gather(github, head, base, paths, meta=None):
                    return [{"path": "ghost.ts", "status":
                             "SourceUnreadable", "reason": "x",
                             "head": {"ref": head}, "base": {"ref": base}}]
                with mock.patch.object(r, "gather_pr_sources",
                                       side_effect=fake_gather):
                    with self.assertRaises(r.PrReviewError) as ctx:
                        r.start_pr_review("o/r", 857, by="owner",
                                          evidence_paths=["ghost.ts"])
        self.assertEqual(ctx.exception.http_status, 400)

    def test_missing_both_sides_without_meta_refuses(self):
        out = r.gather_pr_sources("o/r", HEAD40, BASE40, ["ghost.ts"])
        self.assertEqual(out[0]["status"], "SourceUnreadable")


class TestPrompt(unittest.TestCase):
    def test_delivered_content_and_urls(self):
        snap = snapshot()
        built = r.build_pr_prompt(snap, ok_sources())
        snap_in = built["input_snapshot"]
        self.assertIn("head-code", built["prompt"])
        self.assertIn("base-code", built["prompt"])
        self.assertIn("https://github.com/o/r/pull/857",
                      snap_in["permitted_evidence_urls"])
        self.assertEqual(snap_in["source_census"]["delivered_count"], 1)
        delivered = snap_in["source_census"]["delivered"][0]
        self.assertEqual(delivered["head_content"], "head-code")
        self.assertEqual(delivered["head_sha256"], "h" * 64)

    def test_truncation_is_partial(self):
        snap = snapshot()
        big = [{"path": "big.ts", "status": "ok",
                "head": {"ref": HEAD40, "status": "ok",
                         "content": "x" * 90000, "sha256": "h" * 64,
                         "bytes_total": 90000},
                "base": {"ref": BASE40, "status": "SourceUnreadable",
                         "reason": "not a file"}}]
        built = r.build_pr_prompt(snap, big)
        self.assertTrue(built["truncated"])
        self.assertTrue(any("truncat" in note
                            for note in built["partial"]))
        self.assertTrue(built["input_snapshot"]["truncated"])

    def test_section_brief_trims_body_with_note(self):
        import json as _json
        snap = snapshot(detail={**snapshot()["detail"],
                                "body": "z" * 70000})
        built = r.build_pr_prompt(snap, ok_sources())
        snap_in = built["input_snapshot"]
        self.assertTrue(snap_in["truncated"])
        self.assertTrue(any("shortened" in note
                            for note in snap_in["partial"]))
        # Whole-section trim keeps the delivered brief valid JSON.
        parsed = _json.loads(snap_in["brief"])
        self.assertEqual(parsed["number"], 857)

    def test_bodylink_not_permitted(self):
        snap = snapshot(detail={**snapshot()["detail"],
                                "body": "see https://example.com/evil"})
        built = r.build_pr_prompt(snap, ok_sources())
        self.assertNotIn("https://example.com/evil",
                         built["input_snapshot"]["permitted_evidence_urls"])


class TestValidate(unittest.TestCase):
    def test_good_report_passes(self):
        snap = snapshot()
        built = r.build_pr_prompt(snap, ok_sources())
        problems = r.validate_pr_report(
            snap, good_report(),
            [{"path": "src/a.ts"}],
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertEqual(problems, [])

    def test_adoption_without_evidence_rejected(self):
        snap = snapshot()
        built = r.build_pr_prompt(snap, ok_sources())
        rep = good_report(rec="adopt-external")
        rep["evidence"] = []
        rep["source_files"] = []
        problems = r.validate_pr_report(
            snap, rep, [],
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertTrue(any("adopt-external" in p for p in problems))

    def test_unhashable_recommendation_safe(self):
        snap = snapshot()
        rep = good_report()
        rep["recommendation"] = ["adopt-external"]
        problems = r.validate_pr_report(snap, rep, [{"path": "src/a.ts"}],
                                        permitted=[])
        self.assertTrue(any("recommendation" in p for p in problems))

    def test_missing_label_facet_rejected(self):
        snap = snapshot()
        rep = good_report()
        del rep["labels"]["platform"]
        problems = r.validate_pr_report(snap, rep, [{"path": "src/a.ts"}],
                                        permitted=[])
        self.assertTrue(any("platform" in p for p in problems))

    def test_bodylink_url_rejected(self):
        snap = snapshot()
        built = r.build_pr_prompt(snap, ok_sources())
        rep = good_report(url="https://example.com/evil")
        problems = r.validate_pr_report(
            snap, rep, [{"path": "src/a.ts"}],
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertTrue(any("permitted" in p for p in problems))

    def test_omitted_source_rejected(self):
        snap = snapshot()
        built = r.build_pr_prompt(snap, ok_sources())
        rep = good_report(path="src/unseen.ts")
        problems = r.validate_pr_report(
            snap, rep, [{"path": "src/a.ts"}],
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertTrue(any("not sent" in p for p in problems))


class TestFingerprints(unittest.TestCase):
    def test_fetched_at_ignored(self):
        a = snapshot(fetched_at="t1")
        b = snapshot(fetched_at="t2")
        self.assertEqual(r.content_fp(a), r.content_fp(b))

    def test_body_edit_changes(self):
        a = snapshot()
        b = snapshot()
        b["detail"] = dict(b["detail"], body="changed")
        self.assertNotEqual(r.content_fp(a), r.content_fp(b))

    def test_source_set_changes_attempt_only(self):
        a = snapshot()
        ca = {"requested": ["x"], "requested_count": 1,
              "delivered": [{"path": "x", "head_sha256": "h" * 64,
                             "head_sent": 1, "base_sha256": None,
                             "base_sent": 0}],
              "delivered_count": 1, "omitted": [], "omitted_count": 0,
              "auto_selected": False}
        cb = dict(ca, delivered=[])
        self.assertNotEqual(r.attempt_fp_for(r.content_fp(a), ca),
                            r.attempt_fp_for(r.content_fp(a), cb))


class TestStart(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.snap = snapshot()
        fetch = mock.patch.object(p, "fetch_pr_detail",
                                  return_value=self.snap)
        store = mock.patch.object(p, "store_snapshot",
                                  side_effect=lambda s, x: x)
        fetch.start()
        store.start()
        self.addCleanup(fetch.stop)
        self.addCleanup(store.stop)
        from openmainty import muse as muse_mod
        deny = mock.patch.object(
            muse_mod, "exec_prompt",
            side_effect=AssertionError("model must be mocked per test"))
        deny.start()
        self.addCleanup(deny.stop)
        self._threads = []
        real_thread = __import__("threading").Thread

        def _recording(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            self._threads.append(thread)
            return thread

        rec = mock.patch("threading.Thread", side_effect=_recording)
        rec.start()
        self.addCleanup(rec.stop)

    def _join_started(self):
        for thread in self._threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(),
                             "worker thread leaked after test")

    def test_requested_unreadable_fails_closed(self):
        def fake_gh(argv, timeout=60):
            self.assertEqual(argv[1], "api")
            self.assertIn("repos/o/r/contents/nope.ts", argv[4])
            return json.dumps({"message": "Not Found"})

        with mock.patch.object(p, "_run_gh", side_effect=fake_gh):
            with self.assertRaises(r.PrReviewError) as ctx:
                r.start_pr_review("o/r", 857, by="owner",
                                  evidence_paths=["nope.ts"])
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertIn("unreadable", str(ctx.exception))
        self.assertFalse(p.pr_path("o/r", 857).exists())
        self.assertFalse(p.job_path("o/r", 857).exists())
        self.assertEqual(self._threads, [])

    def test_auto_selects_changed_files(self):
        seen = {}

        def fake_gather(github, head, base, paths, meta=None):
            seen["paths"] = list(paths)
            return ok_sources(paths, head_sha=head, base_sha=base)

        with mock.patch.object(r, "gather_pr_sources",
                               side_effect=fake_gather):
            with mock.patch.object(r, "_run_pr_review",
                                   return_value=None):
                out = r.start_pr_review("o/r", 857, by="owner",
                                        evidence_paths=[])
                self._join_started()
        self.assertTrue(out["started"])
        self.assertEqual(seen["paths"], ["src/a.ts"])
        auto = r._read_json_safe(p.job_path("o/r", 857))
        self.assertTrue(auto[1].get("auto_selected"))

    def test_auto_empty_with_changed_files_refuses(self):
        snap = snapshot()
        snap["detail"] = dict(snap["detail"], files=[])
        with mock.patch.object(p, "fetch_pr_detail", return_value=snap):
            with self.assertRaises(r.PrReviewError) as ctx:
                r.start_pr_review("o/r", 857, by="owner", evidence_paths=[])
        self.assertEqual(ctx.exception.http_status, 400)

    def test_bad_by_refuses(self):
        with self.assertRaises(r.PrReviewError):
            r.start_pr_review("o/r", 857, by="worker", evidence_paths=["x"])

    def test_corrupt_review_record_refuses(self):
        p.review_path("o/r", 857).parent.mkdir(parents=True, exist_ok=True)
        p.review_path("o/r", 857).write_text("{corrupt",
                                             encoding="utf-8")
        with self.assertRaises(r.PrReviewError) as ctx:
            r.start_pr_review("o/r", 857, by="owner",
                              evidence_paths=["src/a.ts"])
        self.assertEqual(ctx.exception.http_status, 502)
        self.assertEqual(p.review_path("o/r", 857).read_text(
            encoding="utf-8"), "{corrupt")

    def test_busy_lease_is_409(self):
        import threading as _th
        from openmainty import sched as _sched
        held = _th.Event()
        release = _th.Event()
        errors = []

        def _holder():
            token = _sched.acquire_worker_lease("pr-review:o/r#857")
            if token is None:
                errors.append("holder could not acquire free lease")
                held.set()
                return
            held.set()
            release.wait(timeout=30)
            _sched.release_worker_lease(token)

        holder = _th.Thread(target=_holder, daemon=True)
        holder.start()
        self.assertTrue(held.wait(timeout=10), "holder never acquired")
        self.assertEqual(errors, [])
        threads_before = len(self._threads)
        try:
            with mock.patch.object(r, "gather_pr_sources",
                                   side_effect=lambda *a: ok_sources()):
                with self.assertRaises(r.PrReviewError) as ctx:
                    r.start_pr_review("o/r", 857, by="owner",
                                      evidence_paths=["src/a.ts"])
            self.assertEqual(ctx.exception.http_status, 409)
            # Nothing started: no job record, no worker thread, idle.
            self.assertFalse(p.job_path("o/r", 857).exists())
            self.assertEqual(len(self._threads), threads_before)
            self.assertEqual(r.job_state("o/r", 857)["status"], "idle")
        finally:
            release.set()
            holder.join(timeout=10)

    def test_changed_head_starts_new_attempt(self):
        started = []

        def fake_gather(github, head, base, paths, meta=None):
            return ok_sources(paths, head_sha=head, base_sha=base)

        with mock.patch.object(r, "gather_pr_sources",
                               side_effect=fake_gather):
            with mock.patch.object(r, "_run_pr_review",
                                   return_value=None):
                out1 = r.start_pr_review("o/r", 857, by="owner",
                                         evidence_paths=["src/a.ts"])
                self._join_started()
                started.append(r.job_state("o/r", 857)["rid"])
                moved = snapshot(head_sha="c" * 40)
                with mock.patch.object(p, "fetch_pr_detail",
                                       return_value=moved):
                    out2 = r.start_pr_review("o/r", 857, by="owner",
                                             evidence_paths=["src/a.ts"])
                    self._join_started()
                    started.append(r.job_state("o/r", 857)["rid"])
        self.assertTrue(out1["started"])
        self.assertTrue(out2["started"])
        self.assertNotEqual(started[0], started[1])

    def test_post_acquire_write_failure_releases_lease(self):
        from openmainty import sched as _sched
        with mock.patch.object(r, "gather_pr_sources",
                               side_effect=lambda *a: ok_sources()):
            with mock.patch.object(
                    r, "_write_job",
                    side_effect=OSError("disk full")):
                with self.assertRaises(r.PrReviewError) as ctx:
                    r.start_pr_review("o/r", 857, by="owner",
                                      evidence_paths=["src/a.ts"])
        self.assertEqual(ctx.exception.http_status, 500)
        # Lease must be free again: a fresh acquire succeeds.
        token = _sched.acquire_worker_lease("pr-review:o/r#probe")
        self.assertIsNotNone(token)
        _sched.release_worker_lease(token)
        self.assertFalse(p.job_path("o/r", 857).exists())

    def test_preservation_failure_releases_lease_no_false_run(self):
        from openmainty import sched as _sched
        from openmainty.util import write_json as _wj
        rep = good_report()
        # Previous current attempt: valid immutable artifact, job
        # points at it, state carries no history entry yet.
        _wj(p.attempt_path("o/r", 857, "rid-old"), {
            "repo": "o/r", "number": 857, "rid": "rid-old",
            "outcome": "reviewed", "error": None,
            "semantic_fp": "s" * 64, "attempt_fp": "0e" * 32,
            "head_sha": HEAD40, "base_sha": BASE40,
            "requested_paths": ["src/a.ts"], "finished_at": "t",
            "usage": None, "input_snapshot": {}, "prompt": "p",
            "raw_output": "", "raw_output_meta": {},
            "partial": [], "report": rep})
        # The previous current job must be a correctly stored done
        # job (with kind), or job_state rejects it before the
        # preservation branch is ever reached.
        r._write_job("o/r", 857, {
            "status": "done", "rid": "rid-old", "finished_at": "t",
            "semantic_fp": "s" * 64, "attempt_fp": "0e" * 32})
        _wj(p.review_path("o/r", 857), {
            "status": "reviewed", "attempt_fp": "0e" * 32,
            "semantic_fp": r.content_fp(self.snap), "report": rep,
            "labels": {"status": ["needs-review"], "platform": ["ios"],
                       "type": ["bug"]},
            "partial": False, "history": []})
        review_before = p.review_path("o/r", 857).read_bytes()
        attempt_before = p.attempt_path("o/r", 857,
                                        "rid-old").read_bytes()
        with mock.patch.object(r, "gather_pr_sources",
                               side_effect=lambda *a: ok_sources()):
            with mock.patch.object(
                    r, "_write_review",
                    side_effect=OSError("disk full")):
                with self.assertRaises(r.PrReviewError) as ctx:
                    r.start_pr_review("o/r", 857, by="owner",
                                      evidence_paths=["src/a.ts"])
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertIn("prior attempt preservation failed",
                      str(ctx.exception))
        # No worker started, no live lease, no false running job;
        # prior review and attempt bytes are unchanged.
        self.assertEqual(self._threads, [])
        token = _sched.acquire_worker_lease("pr-review:o/r#probe2")
        self.assertIsNotNone(token)
        _sched.release_worker_lease(token)
        job = r.job_state("o/r", 857)
        self.assertEqual(job["status"], "error")
        self.assertIn("prior attempt preservation failed",
                      job.get("error") or "")
        self.assertEqual(p.review_path("o/r", 857).read_bytes(),
                         review_before)
        self.assertEqual(p.attempt_path("o/r", 857,
                                        "rid-old").read_bytes(),
                         attempt_before)

    def test_completed_run_preserves_old_attempt_link(self):
        import json as _json
        from openmainty.util import write_json as _wj
        rep = good_report()
        legacy_report = good_report()
        # Legacy current attempt: EMPTY history, finished done job,
        # one genuinely stored valid immutable artifact.
        _wj(p.attempt_path("o/r", 857, "rid-old"), {
            "repo": "o/r", "number": 857, "rid": "rid-old",
            "outcome": "reviewed", "error": None,
            "semantic_fp": r.content_fp(self.snap),
            "attempt_fp": "0d" * 32,
            "head_sha": HEAD40, "base_sha": BASE40,
            "requested_paths": ["src/a.ts"], "finished_at": "t",
            "usage": None, "input_snapshot": {}, "prompt": "p",
            "raw_output": "", "raw_output_meta": {},
            "partial": [], "report": legacy_report})
        r._write_job("o/r", 857, {
            "status": "done", "finished_at": "t", "rid": "rid-old",
            "semantic_fp": r.content_fp(self.snap),
            "attempt_fp": "0d" * 32})
        _wj(p.review_path("o/r", 857), {
            "status": "reviewed", "attempt_fp": "0d" * 32,
            "semantic_fp": r.content_fp(self.snap), "report": legacy_report,
            "labels": {"status": ["needs-review"], "platform": ["ios"],
                       "type": ["bug"]},
            "partial": False, "history": []})
        old_bytes = p.attempt_path("o/r", 857, "rid-old").read_bytes()
        # Persist the REAL coherent snapshot file: TestStart.setUp stubs
        # p.store_snapshot to a passthrough (no product import), so mirror
        # its normalization explicitly (repo slug, fetched_at, full SHAs).
        from openmainty.util import utcnow as _utcnow
        real_snap = dict(self.snap)
        real_snap["repo"] = "o/r"
        real_snap["fetched_at"] = _utcnow()
        _wj(p.pr_path("o/r", 857), real_snap)
        # Minimal valid repo/profile so the served detail/attempt routes
        # resolve the "r" -> "o/r" slug exactly like production.
        from openmainty import repos as _repos
        _repos.save({"name": "r", "github": "o/r", "baseBranch": "main",
                     "knowledge": [], "checks": [], "e2e": [],
                     "release": {}, "announce": {}})

        def fake_exec(prompt, **kw):
            stream = _json.dumps({"payload": {
                "kind": "run_terminal", "text": _json.dumps(rep)}})
            return 0, stream + "\n", {"model": "m"}

        from openmainty import muse as muse_mod
        with mock.patch.object(r, "gather_pr_sources",
                               side_effect=lambda *a: ok_sources()):
            with mock.patch.object(muse_mod, "exec_prompt",
                                   side_effect=fake_exec):
                out = r.start_pr_review("o/r", 857, by="owner",
                                        evidence_paths=["src/a.ts"])
                self._join_started()
        self.assertTrue(out["started"])
        # The real transition retains the old link after the job
        # overwrite, and the old bytes are byte-identical.
        new_rid = r.job_state("o/r", 857).get("rid")
        self.assertNotEqual(new_rid, "rid-old")
        hist = r.review_state("o/r", 857).get("history") or []
        rids = [h.get("rid") for h in hist]
        self.assertIn("rid-old", rids)
        self.assertIn(new_rid, rids)
        self.assertEqual(p.attempt_path("o/r", 857,
                                        "rid-old").read_bytes(), old_bytes)
        self.assertEqual(r.review_state("o/r", 857)["status"], "reviewed")
        # The same completed output through the ACTUAL served contract:
        # api_prs_detail exposes both links, and api_prs_attempt serves
        # both immutable records through the shared checker. Calling
        # review_outcome alone would not prove the served contract.
        from openmainty import serve as serve_mod
        detail = serve_mod.api_prs_detail({"repo": "r", "number": 857})
        served = detail["review"]
        self.assertEqual(served["status"], "reviewed")
        served_rids = [h.get("rid") for h in served["history"]]
        self.assertIn("rid-old", served_rids)
        self.assertIn(new_rid, served_rids)
        for rid in ("rid-old", new_rid):
            att = serve_mod.api_prs_attempt(
                {"repo": "r", "number": 857, "artifact": rid})
            self.assertEqual(att["attempt"]["rid"], rid)


class TestWorker(unittest.TestCase):
    def setUp(self):
        make_home(self)
        self.snap = snapshot()
        p.store_snapshot("o/r", self.snap)
        ident = mock.patch.object(p, "_read_identity",
                                  return_value=(857, HEAD40, BASE40))
        ident.start()
        self.addCleanup(ident.stop)
        self.sources = ok_sources()
        self.built = r.build_pr_prompt(self.snap, self.sources)
        self.job = {"semantic_fp": self.built["content_fp"],
                    "attempt_fp": self.built["attempt_fp"],
                    "head_sha": HEAD40, "base_sha": BASE40,
                    "input_snapshot": self.built["input_snapshot"],
                    "prompt": self.built["prompt"]}

    def _model(self, report):
        import json as _json

        def fake_exec(prompt, **kw):
            self.assertIn("--disable-write", kw.get("extra", []))
            self.assertIn("--disable-shell", kw.get("extra", []))
            self.assertIn("--disable-web-tools", kw.get("extra", []))
            self.assertNotIn("META_API_KEY", kw.get("env", {}))
            stream = _json.dumps({"payload": {
                "kind": "run_terminal", "text": _json.dumps(report)}})
            return 0, stream + "\n", {"model": "m"}
        return fake_exec

    def test_success_persists_review_and_attempt(self):
        rep = good_report()
        with mock.patch.object(p, "fetch_pr_detail",
                               return_value=self.snap):
            from openmainty import muse as muse_mod
            with mock.patch.object(muse_mod, "exec_prompt",
                                   side_effect=self._model(rep)):
                r._run_pr_review("o/r", 857, dict(self.job), "rid-t1")
        state = r.review_state("o/r", 857)
        self.assertEqual(state["status"], "reviewed")
        self.assertEqual(state["attempt_fp"], self.built["attempt_fp"])
        self.assertEqual(state["report"]["recommendation"], "needs-changes")
        job = r.job_state("o/r", 857)
        self.assertEqual(job["rid"], "rid-t1")
        att = p._read_json_safe(p.attempt_path("o/r", 857, "rid-t1"))
        self.assertEqual(att[0], "ok")
        self.assertEqual(att[1]["outcome"], "reviewed")
        self.assertIn("head-code", att[1]["prompt"])

    def test_moved_head_is_stale_not_reviewed(self):
        rep = good_report()
        moved = snapshot(head_sha="c" * 40)
        with mock.patch.object(p, "fetch_pr_detail", return_value=moved):
            from openmainty import muse as muse_mod
            with mock.patch.object(muse_mod, "exec_prompt",
                                   side_effect=self._model(rep)):
                r._run_pr_review("o/r", 857, dict(self.job), "rid-t2")
        state = r.review_state("o/r", 857)
        self.assertNotEqual(state.get("status"), "reviewed")
        self.assertIn("upstream", (state.get("error") or ""))

    def test_after_run_change_is_stale_model_called_once(self):
        rep = good_report()
        calls = []

        def fake_exec(prompt, **kw):
            calls.append(prompt)
            return self._model(rep)(prompt, **kw)

        from openmainty import muse as muse_mod
        moved = snapshot(head_sha="c" * 40)
        with mock.patch.object(p, "_read_identity",
                               return_value=(857, HEAD40, BASE40)):
            with mock.patch.object(p, "fetch_pr_detail",
                                   return_value=moved):
                with mock.patch.object(muse_mod, "exec_prompt",
                                       side_effect=fake_exec):
                    r._run_pr_review("o/r", 857, dict(self.job),
                                     "rid-after")
        self.assertEqual(len(calls), 1)
        self.assertIn("head-code", calls[0])
        state = r.review_state("o/r", 857)
        self.assertNotEqual(state.get("status"), "reviewed")
        self.assertIn("changed during", (state.get("error") or ""))
        att = p._read_json_safe(p.attempt_path("o/r", 857, "rid-after"))
        self.assertEqual(att[0], "ok")
        self.assertEqual(att[1]["outcome"], "error")
        # Original bound input and the model output are preserved.
        self.assertIn("head-code", att[1]["prompt"])
        self.assertIn("needs-changes", att[1]["raw_output"])

    def test_invalid_report_is_error(self):
        rep = good_report(url="https://example.com/evil")
        with mock.patch.object(p, "fetch_pr_detail",
                               return_value=self.snap):
            from openmainty import muse as muse_mod
            with mock.patch.object(muse_mod, "exec_prompt",
                                   side_effect=self._model(rep)):
                r._run_pr_review("o/r", 857, dict(self.job), "rid-t3")
        state = r.review_state("o/r", 857)
        self.assertNotEqual(state.get("status"), "reviewed")

    def test_finish_collision_links_final_rid(self):
        from openmainty.util import write_json as _wj
        _wj(p.attempt_path("o/r", 857, "rid-x"), {"taken": True})
        out = r._finish_job(github="o/r", number=857, outcome="error",
                            error="e", semantic_fp="s", attempt_fp="a",
                            report=None, usage=None, input_snapshot={},
                            rid="rid-x")
        self.assertNotEqual(out, "rid-x")
        job = r.job_state("o/r", 857)
        self.assertEqual(job["rid"], out)

    def test_attempt_write_failure_leaves_no_reviewed_link(self):
        rep = good_report()
        before = r.review_state("o/r", 857)
        with mock.patch.object(
                r, "_write_exclusive",
                side_effect=OSError("disk full")):
            out = r._finish_job(
                github="o/r", number=857, outcome="reviewed", error=None,
                semantic_fp=self.built["content_fp"],
                attempt_fp=self.built["attempt_fp"], report=rep, usage=None,
                input_snapshot=self.built["input_snapshot"], rid="rid-fail")
        self.assertEqual(out, "rid-fail")
        # Nothing is marked reviewed: the new rid stays out of history,
        # the job records the write failure, prior bytes are unchanged.
        state = r.review_state("o/r", 857)
        self.assertNotEqual(state.get("status"), "reviewed")
        hist = state.get("history") or []
        self.assertFalse(any(h.get("rid") == "rid-fail" for h in hist))
        job = r.job_state("o/r", 857)
        self.assertEqual(job["status"], "error")
        self.assertIn("attempt evidence write failed",
                      job.get("error") or "")
        self.assertFalse(p.attempt_path("o/r", 857, "rid-fail").exists())
        self.assertEqual(r.review_state("o/r", 857).get("status"),
                        before.get("status"))


DIFF_TWO_HUNKS = (
    "diff --git a/ios/Sound.swift b/ios/Sound.swift\n"
    "index 1111111..2222222 100644\n"
    "--- a/ios/Sound.swift\n"
    "+++ b/ios/Sound.swift\n"
    "@@ -420,14 +420,20 @@ func stop() {\n"
    " context here\n"
    "-    old stop line\n"
    "+    new helper line releaseAudioSessionIfIdle\n"
    "@@ -560,10 +566,14 @@ func play() {\n"
    "-    leaky old start\n"
    "+    guarded new start\n"
)


def swift_source(marker=True):
    lines = ["// filler line %d" % i for i in range(1, 601)]
    if marker:
        lines[424] = "    new helper line releaseAudioSessionIfIdle"
        lines[569] = "    guarded new start"
    return "\n".join(lines)


def diff_sources(head_text=None, base_text=None):
    head = head_text if head_text is not None else swift_source(True)
    base = base_text if base_text is not None else swift_source(False)
    return [{
        "path": "ios/Sound.swift", "status": "ok", "auto": True,
        "head": {"status": "ok", "ref": HEAD40, "content": head,
                 "sha256": "h" * 64,
                 "bytes_total": len(head.encode("utf-8"))},
        "base": {"status": "ok", "ref": BASE40, "content": base,
                 "sha256": "b" * 64,
                 "bytes_total": len(base.encode("utf-8"))},
    }]


class TestDiffDelivery(unittest.TestCase):
    def setUp(self):
        make_home(self)

    def test_complete_small_diff_sent_verbatim(self):
        snap = snapshot(diff=DIFF_TWO_HUNKS)
        built = r.build_pr_prompt(snap, diff_sources())
        snap_in = built["input_snapshot"]
        self.assertEqual(snap_in["diff"], DIFF_TWO_HUNKS)
        delivery = snap_in["source_census"]["diff_delivery"]
        self.assertTrue(delivery["complete"])
        self.assertEqual(delivery["hunks"], 2)
        self.assertEqual(snap_in["delivery_version"], 2)
        self.assertIn("diff --git", built["prompt"])
        self.assertIn("releaseAudioSessionIfIdle", built["prompt"])

    def test_windows_cover_changed_method_not_prefix(self):
        snap = snapshot(diff=DIFF_TWO_HUNKS)
        built = r.build_pr_prompt(snap, diff_sources())
        sent = built["input_snapshot"]["source_census"]["delivered"][0]
        self.assertTrue(sent["head_line_windows"])
        self.assertTrue(sent["base_line_windows"])
        self.assertIn("releaseAudioSessionIfIdle", sent["head_content"])
        # Blind file prefixes are not sent: the far-away filler head
        # of the file must stay out of the delivered model input.
        self.assertNotIn("// filler line 1\n", sent["head_content"])
        self.assertNotIn("// filler line 1\n", sent["base_content"])
        self.assertIn("hunk-anchored", built["prompt"])

    def test_large_diff_truncated_with_note(self):
        big = "x" * 25000
        built = r.build_pr_prompt(snapshot(diff=big), ok_sources())
        snap_in = built["input_snapshot"]
        self.assertTrue(snap_in["diff_truncated"])
        delivery = snap_in["source_census"]["diff_delivery"]
        self.assertFalse(delivery["complete"])
        self.assertTrue(any("PR diff shortened" in note
                           for note in built["partial"]))
        self.assertTrue(built["truncated"])

    def test_unparseable_hunk_header_falls_back(self):
        bad = ("--- a/src/a.ts\n+++ b/src/a.ts\n"
               "@@ not a hunk header @@\n+new\n")
        built = r.build_pr_prompt(snapshot(diff=bad), ok_sources())
        self.assertTrue(any("line windows omitted" in note
                           for note in built["partial"]))
        # Exact diff text is still delivered; source falls back to
        # plain prefixes instead of fabricated windows.
        self.assertIn("head-code", built["prompt"])
        sent = built["input_snapshot"]["source_census"]["delivered"]
        self.assertFalse(any(d.get("head_line_windows") for d in sent))

    def test_identical_corrected_delivery_dedups(self):
        snap = snapshot(diff=DIFF_TWO_HUNKS)
        first = r.build_pr_prompt(snap, diff_sources())
        second = r.build_pr_prompt(snapshot(diff=DIFF_TWO_HUNKS),
                                   diff_sources())
        self.assertEqual(first["attempt_fp"], second["attempt_fp"])
        self.assertEqual(first["content_fp"], second["content_fp"])

    def test_diff_only_change_keeps_content_fp(self):
        snap = snapshot(diff=DIFF_TWO_HUNKS)
        other = snapshot(diff=DIFF_TWO_HUNKS + "\n")
        first = r.build_pr_prompt(snap, diff_sources())
        second = r.build_pr_prompt(other, diff_sources())
        # Upstream content identity ignores delivery bytes; the
        # attempt identity must not.
        self.assertEqual(first["content_fp"], second["content_fp"])
        self.assertNotEqual(first["attempt_fp"], second["attempt_fp"])

    def test_brief_only_change_requires_new_attempt(self):
        base = r.build_pr_prompt(snapshot(), diff_sources())
        altered = dict(r.build_pr_prompt(snapshot(), diff_sources()))
        census = dict(altered["input_snapshot"]["source_census"])
        census["brief_sha256"] = "f" * 64
        fp_same = r.attempt_fp_for(base["content_fp"],
                                   base["input_snapshot"]["source_census"])
        fp_diff = r.attempt_fp_for(base["content_fp"], census)
        self.assertEqual(base["attempt_fp"], fp_same)
        self.assertNotEqual(base["attempt_fp"], fp_diff)

    def test_contract_change_requires_new_attempt(self):
        built = r.build_pr_prompt(snapshot(), diff_sources())
        census = dict(built["input_snapshot"]["source_census"])
        census["delivery_version"] = 3
        fp_new = r.attempt_fp_for(built["content_fp"], census)
        self.assertNotEqual(built["attempt_fp"], fp_new)

    def test_small_file_fits_with_headers_excluded(self):
        # The probe bug: an 11-byte file must arrive whole because only
        # source bytes consume the budget, never annotation headers.
        text, sent, omitted, part, cut, total = r._select_line_windows(
            "alpha\nbeta\n", [(1, 3)], 15, 20000)
        self.assertFalse(cut)
        self.assertIsNone(part)
        self.assertEqual(omitted, [])
        self.assertEqual(sent, [[1, 3]])
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertEqual(total, 3)

    def test_multibyte_window_round_trips(self):
        text = "한글줄\n두번째줄\n세번째줄\n"
        out, sent, omitted, part, cut, total = r._select_line_windows(
            text, [(1, 3)], 15, 20000)
        self.assertFalse(cut)
        self.assertIsNone(part)
        self.assertEqual(sent, [[1, 4]])
        for line in ("한글줄", "두번째줄", "세번째줄"):
            self.assertIn(line, out)

    def test_zero_budget_sends_nothing_honestly(self):
        out, sent, omitted, part, cut, total = r._select_line_windows(
            "alpha\nbeta\n", [(1, 3)], 15, 0)
        self.assertTrue(cut)
        self.assertEqual(sent, [])
        self.assertEqual(omitted, [[1, 3]])
        self.assertIsNone(part)

    def test_oversize_first_window_partial_tail_exact(self):
        lines = ["line-%02d" % i for i in range(1, 21)]
        content = "\n".join(lines)
        out, sent, omitted, part, cut, total = r._select_line_windows(
            content, [(1, 20)], 0, 30)
        self.assertTrue(cut)
        self.assertIsNotNone(part)
        # Sent lines are whole and exactly accounted: 3 lines of 7
        # bytes plus 2 newlines fit the 30-byte cap; the 4th (31
        # bytes) does not. The partial tail is never listed as fully
        # sent, and the unsent tail is explicit omitted coverage.
        self.assertEqual(sent, [[1, 3]])
        self.assertEqual(part["lines"], [1, 20])
        self.assertEqual(part["lines_sent"], [1, 3])
        self.assertEqual(part["bytes_sent"], 23)
        self.assertNotIn([1, 20], sent)
        self.assertNotIn([1, 20], omitted)
        self.assertTrue(all(iv != part["lines"] for iv in sent))
        self.assertIn([4, 20], omitted)
        for lo, hi in sent:
            for a, b in omitted:
                self.assertTrue(hi < a or b < lo)

    def test_out_of_range_spans_yield_no_code(self):
        out, sent, omitted, part, cut, total = r._select_line_windows(
            "a\nb\n", [(50, 10)], 2, 20000)
        self.assertEqual(sent, [])
        self.assertFalse(cut)
        self.assertEqual(out, "")

    def test_upstream_diff_truncation_kept_distinct(self):
        snap = snapshot(diff="ab", diff_bytes_total=5000,
                        diff_truncated=True)
        built = r.build_pr_prompt(snap, ok_sources())
        delivery = built["input_snapshot"]["source_census"]["diff_delivery"]
        self.assertTrue(delivery["upstream_truncated"])
        self.assertEqual(delivery["upstream_total"], 5000)
        self.assertEqual(delivery["collected_bytes"], 2)
        self.assertTrue(built["truncated"])
        self.assertTrue(any("already truncated at collection" in note
                           for note in built["partial"]))

    def test_success_records_history_with_rid(self):
        snap = snapshot(diff=DIFF_TWO_HUNKS)
        p.store_snapshot("o/r", snap)
        ident = mock.patch.object(p, "_read_identity",
                                  return_value=(857, HEAD40, BASE40))
        ident.start()
        self.addCleanup(ident.stop)
        built = r.build_pr_prompt(snap, ok_sources())
        job = {"semantic_fp": built["content_fp"],
               "attempt_fp": built["attempt_fp"],
               "head_sha": HEAD40, "base_sha": BASE40,
               "input_snapshot": built["input_snapshot"],
               "prompt": built["prompt"]}
        rep = good_report()

        def fake_exec(prompt, **kw):
            stream = json.dumps({"payload": {
                "kind": "run_terminal", "text": json.dumps(rep)}})
            return 0, stream + "\n", {"model": "m"}

        from openmainty import muse as muse_mod
        with mock.patch.object(p, "fetch_pr_detail", return_value=snap):
            with mock.patch.object(muse_mod, "exec_prompt",
                                   side_effect=fake_exec):
                r._run_pr_review("o/r", 857, dict(job), "rid-hist")
        state = r.review_state("o/r", 857)
        self.assertEqual(state["status"], "reviewed")
        hist = state.get("history") or []
        self.assertTrue(any(h.get("rid") == "rid-hist" and
                           h.get("event") == "review-reviewed"
                           for h in hist))
        out = r.review_outcome("o/r", 857)
        self.assertEqual(out["status"], "reviewed")
        self.assertTrue(any(h.get("rid") == "rid-hist"
                           for h in out["history"]))


OUT_OF_RANGE_DIFF = (
    "diff --git a/src/a.ts b/src/a.ts\n"
    "--- a/src/a.ts\n"
    "+++ b/src/a.ts\n"
    "@@ -50,3 +50,3 @@\n"
    "-old\n"
    "+new\n"
)

PARTIAL_SPAN_DIFF = (
    "diff --git a/src/a.ts b/src/a.ts\n"
    "--- a/src/a.ts\n"
    "+++ b/src/a.ts\n"
    "@@ -1,20 +1,20 @@\n"
    "-l1\n"
    "+l1\n"
)


def tiny_sources(head_text, base_text=None):
    base = head_text if base_text is None else base_text
    return [{"path": "src/a.ts", "status": "ok",
             "head": {"status": "ok", "ref": HEAD40, "content": head_text,
                      "sha256": "h" * 64,
                      "bytes_total": len(head_text.encode("utf-8"))},
             "base": {"status": "ok", "ref": BASE40, "content": base,
                      "sha256": "b" * 64,
                      "bytes_total": len(base.encode("utf-8"))}}]


class TestWindowDeliveryHonesty(unittest.TestCase):
    """Zero-code and partial windows at the real build boundary.

    Selector-only tests cannot prove the census: these drive
    build_pr_prompt and assert the delivered/omitted census, the
    prompt text, and citation validation together.
    """

    def setUp(self):
        make_home(self)

    def test_out_of_range_hunks_exclude_both_sides_at_build(self):
        src = tiny_sources("a\nb\n")
        snap = snapshot(diff=OUT_OF_RANGE_DIFF)
        built = r.build_pr_prompt(snap, src)
        census = built["input_snapshot"]["source_census"]
        self.assertEqual(census["delivered"], [])
        self.assertEqual(census["delivered_count"], 0)
        omitted = [o for o in census["omitted"]
                   if o.get("path") == "src/a.ts"]
        self.assertEqual(len(omitted), 2)
        self.assertTrue(all("no deliverable" in (o.get("reason") or "")
                            for o in omitted))
        self.assertTrue(any("src/a.ts" in note and "HEAD" in note
                            for note in built["partial"]))
        # A zero-code path must not validate a citation.
        rep = good_report(path="src/a.ts")
        problems = r.validate_pr_report(
            snap, rep, census["delivered"],
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertTrue(any("not sent" in problem for problem in problems))

    def test_oversize_first_line_excluded_at_build(self):
        long_line = "x" * 25000
        content = long_line + "\nshort\n"
        diff = ("diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n"
                "+++ b/src/a.ts\n@@ -1,2 +1,2 @@\n-old\n+new\n")
        src = tiny_sources(content)
        built = r.build_pr_prompt(snapshot(diff=diff), src)
        census = built["input_snapshot"]["source_census"]
        self.assertEqual(census["delivered"], [])
        omitted = [o for o in census["omitted"]
                   if o.get("path") == "src/a.ts"]
        self.assertTrue(omitted)
        self.assertTrue(all("no deliverable" in (o.get("reason") or "")
                            for o in omitted))
        self.assertNotIn(long_line[:100], built["prompt"])

    def test_partial_tail_survives_build_census(self):
        lines = ["line-%02d" % i for i in range(1, 21)]
        content = "\n".join(lines)
        src = tiny_sources(content)
        with mock.patch.object(r, "_PROMPT_SOURCE_EACH_CAP", 30):
            built = r.build_pr_prompt(snapshot(diff=PARTIAL_SPAN_DIFF),
                                      src)
        entries = built["input_snapshot"]["source_census"]["delivered"]
        self.assertEqual(len(entries), 1)
        head = entries[0]
        self.assertEqual(head["head_line_windows"], [[1, 3]])
        self.assertEqual(head["head_omitted_windows"], [[4, 20]])
        pw = head["head_partial_window"]
        self.assertEqual(pw["lines"], [1, 20])
        self.assertEqual(pw["lines_sent"], [1, 3])
        self.assertEqual(pw["bytes_sent"], 23)
        for lo, hi in head["head_line_windows"]:
            for a, b in head["head_omitted_windows"]:
                self.assertTrue(hi < a or b < lo)
        self.assertTrue(any("4-20" in note for note in built["partial"]))
        self.assertIn("line-03", head["head_content"])
        self.assertNotIn("line-04", head["head_content"])
        rep = good_report(path="src/a.ts")
        problems = r.validate_pr_report(
            snapshot(diff=PARTIAL_SPAN_DIFF), rep, entries,
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertEqual(problems, [])

    def test_multibyte_source_round_trips_at_build(self):
        content = "한글줄\n두번째줄\n세번째줄\n"
        diff = ("diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n"
                "+++ b/src/a.ts\n@@ -1,4 +1,4 @@\n-old\n+new\n")
        src = tiny_sources(content)
        built = r.build_pr_prompt(snapshot(diff=diff), src)
        entries = built["input_snapshot"]["source_census"]["delivered"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["head_sent"],
                         len(content.encode("utf-8")))
        self.assertIn("한글줄", entries[0]["head_content"])
        self.assertIsNone(entries[0].get("head_partial_window"))

    def test_zero_budget_sends_nothing_at_build(self):
        src = tiny_sources("alpha\nbeta\n")
        with mock.patch.object(r, "_PROMPT_SOURCE_TOTAL_CAP", 0):
            built = r.build_pr_prompt(snapshot(diff=OUT_OF_RANGE_DIFF),
                                      src)
        census = built["input_snapshot"]["source_census"]
        self.assertEqual(census["delivered"], [])
        omitted = [o for o in census["omitted"]
                   if o.get("path") == "src/a.ts"]
        self.assertTrue(omitted)
        self.assertTrue(all("budget exhausted" in (o.get("reason") or "")
                            for o in omitted))

    def test_full_delivery_has_no_partial_metadata(self):
        snap = snapshot(diff=DIFF_TWO_HUNKS)
        built = r.build_pr_prompt(snap, diff_sources())
        entries = built["input_snapshot"]["source_census"]["delivered"]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].get("head_partial_window"))
        self.assertIsNone(entries[0].get("base_partial_window"))
        self.assertEqual(entries[0].get("head_omitted_windows"), [])

    def test_both_sides_empty_excluded_at_build(self):
        empty = {"status": "ok", "ref": HEAD40, "content": "",
                 "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649"
                           "b934ca495991b7852b855",
                 "bytes_total": 0}
        src = [{"path": "src/a.ts", "status": "ok",
                "head": dict(empty),
                "base": dict(empty, ref=BASE40)}]
        built = r.build_pr_prompt(snapshot(diff=PARTIAL_SPAN_DIFF), src)
        census = built["input_snapshot"]["source_census"]
        self.assertEqual(census["delivered"], [])
        self.assertEqual(census["delivered_count"], 0)
        omitted = [o for o in census["omitted"]
                   if o.get("path") == "src/a.ts"]
        self.assertEqual(len(omitted), 2)
        self.assertTrue(all(o.get("bytes_total") == 0 for o in omitted))
        self.assertTrue(all("empty side" in (o.get("reason") or "")
                            for o in omitted))
        rep = good_report(path="src/a.ts")
        problems = r.validate_pr_report(
            snapshot(diff=PARTIAL_SPAN_DIFF), rep, census["delivered"],
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertTrue(any("not sent" in problem for problem in problems))

    def test_one_empty_side_keeps_valid_side_citable(self):
        diff = ("diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n"
                "+++ b/src/a.ts\n@@ -1,2 +1,2 @@\n-old\n+new\n")
        src = [{"path": "src/a.ts", "status": "ok",
                "head": {"status": "ok", "ref": HEAD40,
                         "content": "a\nb\n",
                         "sha256": "h" * 64, "bytes_total": 4},
                "base": {"status": "ok", "ref": BASE40, "content": "",
                         "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae"
                                   "41e4649b934ca495991b7852b855",
                         "bytes_total": 0}}]
        built = r.build_pr_prompt(snapshot(diff=diff), src)
        census = built["input_snapshot"]["source_census"]
        entries = census["delivered"]
        self.assertEqual(len(entries), 1)
        # Hunk context expands (1,2) to the full 3-line file.
        self.assertEqual(entries[0]["head_line_windows"], [[1, 3]])
        base_omitted = [o for o in census["omitted"]
                        if o.get("path") == "src/a.ts"
                        and o.get("side") == "base"]
        self.assertEqual(len(base_omitted), 1)
        self.assertIn("empty side", base_omitted[0].get("reason") or "")
        rep = good_report(path="src/a.ts")
        problems = r.validate_pr_report(
            snapshot(diff=diff), rep, entries,
            permitted=built["input_snapshot"]["permitted_evidence_urls"])
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
