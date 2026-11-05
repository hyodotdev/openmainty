"""Recovery-review (Source17) offline regressions.

Read-only conflict investigation for a BLOCKED typed-issue prepare:
strict registered identity (local repo name vs canonical slug,
40-hex commits vs 64-hex fingerprints) via the SHARED held
validators, pinned-Git evidence with genuine absence vs unreadable
distinction, immutable input/attempt/report linkage, strict
sent-range citation checks, after-model revalidation, lease-then-
op-lock ordering with bounded retries, and storage fail-closed
behavior.

Real temp Git repositories under an isolated OPENMAINTY_HOME; the
only mocked boundary is the model subscription transport, fed real
Muse terminal/delta JSONL. No network, no target writes.
"""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib import error as urlerror
from urllib import request as urlrequest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import batches, m2, sched, serve, store  # noqa: E402
from openmainty.util import write_json  # noqa: E402
from test_integration_prepare_issue import (  # noqa: E402
    IssueFixtureMixin, git)

from openmainty import issue_recovery as ir  # noqa: E402
from openmainty.issue_recovery import IssueRecoveryError  # noqa: E402


REAL_ERROR = (
    "cherry-pick conflicted or failed: error: could not apply CAND "
    "chore(maint): scheduled maintenance work\n"
    'hint: After resolving the conflicts, mark them with\n'
    'hint: "git add/rm <pathspec>", then run\n'
    "hint: \"git cherry-pick --continue\".\n"
    "re-derive refused: candidate touches g.txt besides manifests/locks"
)

CANDIDATE_TOUCHED = "candidate touches example/dist/bundle.4f752431.js"


def canned_report(cand_head):
    return {
        "status": "needs_changes",
        "summary": "후보 f.txt 변경이 기준과 충돌한다",
        "cause": "동일 경로 양쪽 수정",
        "conflict_files": ["f.txt"],
        "citations": [{"path": "f.txt", "ref": cand_head,
                       "start_line": 1, "end_line": 1,
                       "quote": "issue change"}],
        "intended_changes": ["f.txt 충돌 해소"],
        "proposed_paths": ["f.txt"],
        "build_contract": None,
        "questions": [],
        "uncertainties": [],
        "limits": ["생성물은 모델에 보내지 않음"],
        "next_action": "충돌 범위를 검토한 뒤 별도 승인으로 파생 준비",
    }


def canned_jsonl(report_text):
    lines = [json.dumps({"payload": {"kind": "run_output_delta",
                                     "text": "investigating"}}),
             json.dumps({"payload": {"kind": "run_terminal",
                                     "text": report_text}})]
    return "\n".join(lines)


class RecoveryFixtureMixin(IssueFixtureMixin):
    blocked_id = "prep-issue-856-r1"

    def _write_blocked_op(self, status="blocked", conflicted=None,
                          error=None, result_head="", result_fp="",
                          cand_head=None, base_head=None,
                          issue_branch="codex/review-issue-856-test",
                          number=None, repo=None, lane=None,
                          op_id=None):
        number = number or self.number
        repo = repo or self.repo
        lane = lane or self.lane
        op_id = op_id or self.blocked_id
        rec = {
            "op": "prepare", "op_id": op_id, "status": status,
            "repo": self.repo, "run_id": self.run_id, "lane": self.lane,
            "batch_id": self.batch_id,
            "candidate": {
                "head": cand_head or self.cand_head,
                "fingerprint": self.cand_snap["fingerprint"],
                "worktree": str(self.run_work), "branch": self.branch},
            "base": {
                "batch": self.batch_id, "lane": self.lane,
                "head": base_head or self.bar_head,
                "fingerprint": self.bar_snap["fingerprint"],
                "checkout": str(self.base_bar_wt)},
            "issue": {
                "issue": number, "repo": repo, "lane": lane,
                "url": ("https://github.com/" + self.slug +
                        "/issues/" + number),
                "branch": issue_branch},
            "result": {
                "path": str(self.result_wt) + "-gone",
                "head": result_head, "fingerprint": result_fp,
                "branch": issue_branch,
                "conflicted_paths": (["f.txt"] if conflicted is None
                                      else conflicted),
                "proof_note": "fixture"},
            "error": REAL_ERROR if error is None else error,
            "gates": [{"key": "source-prepared", "status": "blocked",
                       "note": "cherry-pick conflict"},
                  {"key": "fastchecks", "status": "pending",
                   "note": "not run in this step"}],
        }
        write_json(batches.op_file(op_id), rec)
        return rec

    def _op_bytes(self, op_id=None):
        return batches.op_file(op_id or self.blocked_id).read_bytes()

    def _locked_request(self, op_id=None, **kw):
        with m2.op_locked(op_id or self.blocked_id):
            return ir.request_review(op_id or self.blocked_id, **kw)

    def _done(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(
                            canned_report(self.cand_head))), {"m": 1})):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 0)
        return req

    def _rewrite_op(self, fn):
        path = batches.op_file(self.blocked_id)
        rec = json.loads(path.read_bytes().decode("utf-8"))
        fn(rec)
        path.write_bytes(json.dumps(rec).encode("utf-8"))
        return path.read_bytes()


class LoaderTests(RecoveryFixtureMixin):
    def test_real_registered_shape_loads(self):
        self._write_blocked_op()
        ctx = ir.load_blocked_op(self.blocked_id)
        self.assertEqual(ctx["issue"]["repo"], self.repo)
        self.assertEqual(ctx["ctx"]["url"],
                         f"https://github.com/{self.slug}/issues/856")
        self.assertEqual(len(ctx["candidate"]["fingerprint"]), 64)
        self.assertEqual(len(ctx["candidate"]["head"]), 40)
        self.assertEqual(len(ctx["base"]["fingerprint"]), 64)
        self.assertEqual(ctx["base_commit"], self.base_head)

    def test_owner_slug_repo_name_rejected(self):
        self._write_blocked_op(repo=self.slug)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 400)

    def test_short_fingerprint_rejected(self):
        rec = self._write_blocked_op()
        rec["candidate"]["fingerprint"] = "ab" * 20
        write_json(batches.op_file(self.blocked_id), rec)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertIn("64-hex", str(cm.exception))

    def test_non_blocked_refused(self):
        self._write_blocked_op(status="done")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 409)

    def test_conflicted_paths_required(self):
        self._write_blocked_op(conflicted=[])
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")

    def test_stale_proof_refused_bytes_preserved(self):
        self._write_blocked_op()
        before = self._op_bytes()
        (self.run_work / "f.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.code, "stale")
        self.assertEqual(self._op_bytes(), before)

    def test_missing_op_404(self):
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op("prep-nope-0000")
        self.assertEqual(cm.exception.http_status, 404)

    def test_bad_op_id_400(self):
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op("../escape")
        self.assertEqual(cm.exception.http_status, 400)

    def test_result_head_present_refused(self):
        self._write_blocked_op(result_head=self.res_head,
                               result_fp=self.res_snap["fingerprint"])
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")


class GrammarTests(RecoveryFixtureMixin):
    def test_exact_refusal_grammar_parses(self):
        self.assertEqual(ir.parse_original_touched(
            "x re-derive refused: candidate touches a/b.js, c/d.js "
            "besides manifests/locks"), ["a/b.js", "c/d.js"])

    def test_trailing_junk_after_suffix_rejected(self):
        self.assertEqual(ir.parse_original_touched(
            "x re-derive refused: candidate touches a/b.js "
            "besides manifests/locks y"), [])
        self.assertEqual(ir.parse_original_touched(
            "x re-derive refused: candidate touches a/b.js"), [])

    def test_prefix_as_filename_rejected(self):
        got = ir.parse_original_touched(REAL_ERROR)
        self.assertEqual(got, ["g.txt"])
        self.assertFalse(any("candidate" in p for p in got))
        frag = ir.parse_original_touched(
            CANDIDATE_TOUCHED + " besides manifests/locks")
        self.assertEqual(frag, [])

    def test_absent_path_via_pinned_tree(self):
        # g.txt was ADDED by the candidate: it has a candidate window
        # only, while the origin/base absences belong in omitted with
        # the proven pinned tree/commit identity — never a fabricated
        # origin/base window.
        self._write_blocked_op()
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        kinds = {(w["path"], w["ref_kind"]) for w in collected["windows"]}
        self.assertIn(("g.txt", "candidate"), kinds)
        self.assertNotIn(("g.txt", "origin"), kinds)
        self.assertNotIn(("g.txt", "base"), kinds)
        absent = [o for o in collected["omitted"]
                  if o["path"] == "g.txt" and o.get("absent")]
        self.assertEqual(len(absent), 2)
        self.assertEqual(sorted(o["ref_kind"] for o in absent),
                         ["base", "origin"])
        self.assertTrue(all("tree" in o for o in absent))
        self.assertTrue(all(o.get("ref") for o in absent))

    def test_realistic_lists_parse_collect_and_counts(self):
        # Real 2-path fixture: f.txt exists at all three refs, g.txt
        # exists only at the candidate head (absent at origin/base).
        err = ("x re-derive refused: candidate touches "
               "g.txt besides manifests/locks")
        self._write_blocked_op(error=err, conflicted=["f.txt"])
        ctx = ir.load_blocked_op(self.blocked_id)
        self.assertEqual(ir.parse_original_touched(err), ["g.txt"])
        cause = ir.human_cause(ctx)
        self.assertEqual(cause["conflicted_paths"], ["f.txt"])
        self.assertEqual(cause["original_touched"], ["g.txt"])
        self.assertIn("1개", cause["next_action"])
        collected = ir.collect_windows(ctx)
        counts = collected["counts"]
        self.assertEqual(counts["requested_paths"], 2)
        self.assertEqual(counts["collected_paths"], 2)
        self.assertEqual(counts["dropped_paths"], 0)
        self.assertEqual(counts["ref_kinds"], 3)
        # f.txt x3 refs + g.txt x1 ref = 4 windows; g.txt absent at
        # origin and base stays an honest absence, not a silent drop.
        self.assertEqual(counts["windows"], 4)
        self.assertEqual(counts["omitted"], 2)
        absent = [o for o in collected["omitted"] if o.get("absent")]
        self.assertEqual(len(absent), 2)
        self.assertTrue(all(o["path"] == "g.txt" for o in absent))
        kinds = sorted({(w["path"], w["ref_kind"]) for w in
                        collected["windows"]})
        self.assertEqual(kinds,
                         [("f.txt", "base"), ("f.txt", "candidate"),
                          ("f.txt", "origin"), ("g.txt", "candidate")])

    def test_scope_cap_drops_paths_with_count(self):
        # Nine paths across three refs exceed the 8-path scope cap: the
        # ninth path is dropped with an explicit count/reason while the
        # first eight keep their full 3-ref windows (24 windows max).
        err = ("x re-derive refused: candidate touches "
               "p1.txt, p2.txt, p3.txt, p4.txt, p5.txt, "
               "p6.txt, p7.txt, p8.txt, p9.txt besides manifests/locks")
        self._write_blocked_op(error=err, conflicted=["f.txt"])
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        counts = collected["counts"]
        self.assertEqual(counts["requested_paths"], 10)
        self.assertEqual(counts["collected_paths"], 8)
        self.assertEqual(counts["dropped_paths"], 2)
        scoped = [o for o in collected["omitted"]
                  if o.get("scope") == "path"]
        self.assertEqual(len(scoped), 2)
        self.assertTrue(all(o.get("ref_kinds") == 3 for o in scoped))
        self.assertEqual(
            sorted(o["path"] for o in scoped), ["p8.txt", "p9.txt"])

    def test_missing_checkout_unreadable(self):
        import shutil
        self._write_blocked_op()
        ctx = ir.load_blocked_op(self.blocked_id)
        shutil.rmtree(str(self.run_work))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.collect_windows(ctx)
        self.assertEqual(cm.exception.code, "unreadable")

    def test_changed_base_head_is_stale(self):
        # A changed recorded base head means the live source moved:
        # the current checkout snapshot no longer matches the
        # recording, so the binding is stale — not unreadable.
        self._write_blocked_op(base_head="0" * 40)
        before = self._op_bytes()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.code, "stale")
        self.assertEqual(self._op_bytes(), before)

    def test_missing_base_checkout_is_unreadable(self):
        # A valid recording whose base checkout is gone is
        # unreadable: the pinned ref cannot be verified, and the
        # loader must never silently heal it or call it stale.
        import shutil
        self._write_blocked_op()
        before = self._op_bytes()
        shutil.rmtree(str(self.base_bar_wt))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.load_blocked_op(self.blocked_id)
        self.assertEqual(cm.exception.code, "unreadable")
        self.assertEqual(self._op_bytes(), before)


class LifecycleTests(RecoveryFixtureMixin):
    def _exec(self, rid, raw_jsonl, exit_code=0, cancel_event=None):
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(exit_code, raw_jsonl, {"m": 1})):
            return ir.execute_review(self.blocked_id, rid,
                                     cancel_event=cancel_event)

    def test_full_readonly_flow(self):
        self._write_blocked_op()
        before = self._op_bytes()
        req = ir.request_review(self.blocked_id)
        self.assertFalse(req["reused"])
        self.assertTrue(req["rid"].startswith("rr-"))
        rc = self._exec(req["rid"],
                        canned_jsonl(json.dumps(
                            canned_report(self.cand_head))))
        self.assertEqual(rc, 0)
        st = ir.read_status(self.blocked_id)
        self.assertTrue(st["current"])
        self.assertEqual(st["report"]["status"], "needs_changes")
        self.assertEqual(st["report"]["citations"][0]["ref"],
                         self.cand_head)
        self.assertEqual(self._op_bytes(), before)
        self.assertEqual(st["has_prepared_source"], False)
        self.assertEqual(st["followup_allowed"], False)
        self.assertEqual(st["fastcheck"]["state"], "not_run")

    def test_identical_fp_reuses_done(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        self._exec(req["rid"],
                   canned_jsonl(json.dumps(canned_report(self.cand_head))))
        before = sorted(p.name for p in ir.rec_root(
            self.blocked_id).glob("attempt-*.json"))
        again = ir.request_review(self.blocked_id)
        self.assertTrue(again["reused"])
        self.assertEqual(again["rid"], req["rid"])
        self.assertEqual(sorted(p.name for p in ir.rec_root(
            self.blocked_id).glob("attempt-*.json")), before)

    def test_running_attach_no_duplicate(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        att = json.loads((root / f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        att["status"] = "running"
        att["job_id"] = "job-live-1"
        serve._JOBS["job-live-1"] = {"id": "job-live-1",
                                     "status": "running"}
        self.addCleanup(lambda: serve._JOBS.pop("job-live-1", None))
        (root / f"attempt-{req['rid']}.json").write_text(
            json.dumps(att), encoding="utf-8")
        again = ir.request_review(self.blocked_id)
        self.assertTrue(again["reused"])
        self.assertTrue(again.get("attached"))
        self.assertEqual(again["rid"], req["rid"])

    def test_dead_running_becomes_interrupted_plus_retry(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        att = json.loads((root / f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        att["status"] = "running"
        att["job_id"] = "job-dead-9"
        (root / f"attempt-{req['rid']}.json").write_text(
            json.dumps(att), encoding="utf-8")
        before = (root / f"attempt-{req['rid']}.json").read_bytes()
        nxt = ir.request_review(self.blocked_id)
        self.assertFalse(nxt.get("attached", False))
        self.assertNotEqual(nxt["rid"], req["rid"])
        old = json.loads((root / f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        self.assertEqual(old["status"], "interrupted")
        self.assertNotEqual(
            (root / f"attempt-{req['rid']}.json").read_bytes(), before)

    def test_retry_exhaustion_409_preserves(self):
        self._write_blocked_op()
        first = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        for n in range(ir._MAX_ATTEMPTS + 1):
            rid = ir._attempt_rid(first["fingerprint"], n)
            (root / f"attempt-{rid}.json").write_text(
                json.dumps({"schema": 1, "kind": "recovery-attempt",
                            "rid": rid, "op": self.blocked_id,
                            "input": first["input"],
                            "fingerprint": first["fingerprint"],
                            "status": "error"}), encoding="utf-8")
        names_before = sorted(p.name for p in root.glob("attempt-*.json"))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.request_review(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(cm.exception.code, "retries-exhausted")
        self.assertEqual(sorted(p.name for p in root.glob("attempt-*.json")),
                         names_before)

    def test_startup_failure_no_phantom(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        ir.mark_startup_failed(self.blocked_id, req["rid"], "no slot")
        att = json.loads((ir.rec_root(self.blocked_id) /
                          f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        self.assertEqual(att["status"], "error")
        self.assertIn("no slot", att["error"])
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.mark_startup_failed(self.blocked_id, "rr-deadbeefcafe",
                                   "x")
        self.assertEqual(cm.exception.http_status, 404)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.mark_startup_failed(self.blocked_id, req["rid"], "again")
        self.assertEqual(cm.exception.http_status, 409)

    def test_model_mutation_marks_stale(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        (self.run_work / "f.txt").write_text("changed mid-run\n",
                                             encoding="utf-8")
        rc = self._exec(req["rid"],
                        canned_jsonl(json.dumps(
                            canned_report(self.cand_head))))
        self.assertEqual(rc, 1)
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])
        self.assertEqual(st["latest"]["status"], "stale")

    def test_stored_run_change_marks_stale(self):
        # The op bytes (and parent SHA) are unchanged; only the stored
        # run record drifted. The current-fingerprint revalidation must
        # refuse, without touching the op bytes.
        from openmainty import store as _store
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        before = self._op_bytes()
        run_path = _store.run_file(self.run_id)
        run_path.write_bytes(run_path.read_bytes() + b"\n")
        rc = self._exec(req["rid"],
                        canned_jsonl(json.dumps(
                            canned_report(self.cand_head))))
        self.assertEqual(rc, 1)
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])
        self.assertEqual(st["latest"]["status"], "stale")
        self.assertEqual(self._op_bytes(), before)

    def test_base_source_change_refused(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        before = self._op_bytes()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.request_review(self.blocked_id)
        self.assertEqual(cm.exception.code, "stale")
        self.assertEqual(self._op_bytes(), before)
        root = ir.rec_root(self.blocked_id)
        self.assertEqual(
            sorted(p.name for p in root.glob("attempt-*.json")),
            [f"attempt-{req['rid']}.json"])

    def test_snapshot_tamper_fails_closed(self):
        # A stored input whose text no longer matches its hash cannot
        # prove its original identity: read_status must raise a typed
        # corrupt error with all bytes preserved, never bless the
        # tampered bytes as inspectable history.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        before = self._op_bytes()
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        doc["snapshot"]["files"][0]["text"] += "tampered"
        inp_path.write_text(json.dumps(doc), encoding="utf-8")
        raw_before = inp_path.read_bytes()
        # Tampered stored text fails the before-transport identity
        # gate: the worker raises a typed corrupt error immediately
        # and never reaches the model subscription transport.
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError) as cm2:
                ir.execute_review(self.blocked_id, req["rid"])
            spy.assert_not_called()
        self.assertEqual(cm2.exception.http_status, 502)
        self.assertEqual(cm2.exception.code, "corrupt")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 502)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(inp_path.read_bytes(), raw_before)
        self.assertEqual(self._op_bytes(), before)

    def test_model_time_mutation_marks_stale(self):
        # The transport seam itself mutates the source mid-model: the
        # pre-model check passed, but the post-model revalidation must
        # still discard the report.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        payload = canned_jsonl(json.dumps(canned_report(self.cand_head)))

        def _run(*args, **kwargs):
            (self.run_work / "f.txt").write_text("mutated mid-model\n",
                                                 encoding="utf-8")
            return (0, payload, {"m": 1})

        with mock.patch("openmainty.muse.exec_prompt",
                        side_effect=_run):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 1)
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])
        self.assertEqual(st["latest"]["status"], "stale")

    def test_prompt_only_tamper_breaks_chain(self):
        # Only the assembled sent_text is tampered (files[] untouched):
        # the prompt-hash link must fail closed with a typed corrupt
        # error and preserved bytes, never replay current or bless the
        # edited record as history.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        before = self._op_bytes()
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        doc["snapshot"]["sent_text"] += "\n추가된 줄"
        inp_path.write_text(json.dumps(doc), encoding="utf-8")
        raw_before = inp_path.read_bytes()
        # Tampered prompt bytes fail the before-transport identity
        # gate: the worker raises a typed corrupt error immediately
        # and never reaches the model subscription transport.
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError) as cm2:
                ir.execute_review(self.blocked_id, req["rid"])
            spy.assert_not_called()
        self.assertEqual(cm2.exception.http_status, 502)
        self.assertEqual(cm2.exception.code, "corrupt")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 502)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(inp_path.read_bytes(), raw_before)
        self.assertEqual(self._op_bytes(), before)

    def test_wrong_line_quote_rejected(self):
        # The quote exists in f.txt, but the declared lines (2..2)
        # lie outside the actually sent 1-line window: citations must
        # name the lines they quote, and the window must exist.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        bad = canned_report(self.cand_head)
        bad["citations"] = [{"path": "f.txt", "ref": self.cand_head,
                             "start_line": 2, "end_line": 2,
                             "quote": "issue change"}]
        rc = self._exec(req["rid"], canned_jsonl(json.dumps(bad)))
        self.assertEqual(rc, 1)
        self.assertEqual(
            ir.read_status(self.blocked_id)["latest"]["status"], "error")

    def test_wrong_quote_rejected(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        bad = canned_report(self.cand_head)
        bad["citations"] = [{"path": "f.txt", "ref": self.cand_head,
                             "start_line": 1, "end_line": 1,
                             "quote": "never sent to the model"}]
        rc = self._exec(req["rid"], canned_jsonl(json.dumps(bad)))
        self.assertEqual(rc, 1)
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertEqual(st["latest"]["status"], "error")

    def test_out_of_range_citation_rejected(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        bad = canned_report(self.cand_head)
        bad["citations"] = [{"path": "f.txt", "ref": self.cand_head,
                             "start_line": 1, "end_line": 999,
                             "quote": "issue change"}]
        rc = self._exec(req["rid"], canned_jsonl(json.dumps(bad)))
        self.assertEqual(rc, 1)
        self.assertEqual(
            ir.read_status(self.blocked_id)["latest"]["status"], "error")

    def test_cancelled_transport_marks_cancelled(self):
        from openmainty import util as _util
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        side_effect=_util.Cancelled("stop")):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 130)
        st = ir.read_status(self.blocked_id)
        self.assertEqual(st["latest"]["status"], "cancelled")
        self.assertIsNone(st["report"])

    def test_never_attempted_reads_empty(self):
        self._write_blocked_op()
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])
        self.assertIsNone(st["latest"])
        self.assertEqual(st["fastcheck"]["state"], "not_run")

    def test_corrupt_report_raises_bytes_preserved(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        self._exec(req["rid"],
                   canned_jsonl(json.dumps(
                       canned_report(self.cand_head))))
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        self.assertEqual(latest["status"], "done")
        rep_path = root / (latest["report"] + ".json")
        bad = b"{broken report"
        rep_path.write_bytes(bad)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(rep_path.read_bytes(), bad)

    def test_missing_report_is_corrupt_not_empty(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        (root / "latest.json").write_text(json.dumps({
            "input": req["input"], "rid": req["rid"],
            "fingerprint": req["fingerprint"], "status": "done",
            "report": "report-deadbeefcafe"}), encoding="utf-8")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")

    def _receipt_path(self):
        from openmainty import fastcheck as _fc
        return _fc.op_fastcheck_path(self.blocked_id)

    def test_fastcheck_present_receipt_is_error(self):
        self._write_blocked_op()
        self._receipt_path().write_bytes(b"{not json",
                                         )
        got = ir.read_status(self.blocked_id)
        self.assertEqual(got["fastcheck"]["state"], "error")

    def test_fastcheck_oversize_receipt_is_error(self):
        self._write_blocked_op()
        self._receipt_path().write_bytes(b"x" * (ir._RECEIPT_MAX_BYTES + 1))
        got = ir.read_status(self.blocked_id)
        self.assertEqual(got["fastcheck"]["state"], "error")

    def test_fastcheck_nonpending_absent_is_unknown(self):
        rec = self._write_blocked_op()
        rec["gates"] = [{"key": "source-prepared", "status": "blocked",
                         "note": "cherry-pick conflict"}]
        from openmainty import batches as _b
        _b.write_json(_b.op_file(self.blocked_id), rec)
        got = ir.read_status(self.blocked_id)
        self.assertEqual(got["fastcheck"]["state"], "unknown")

    def test_cancel_marks_cancelled(self):
        import threading
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        ev = threading.Event()
        ev.set()
        rc = self._exec(req["rid"],
                        canned_jsonl(json.dumps(
                            canned_report(self.cand_head))),
                        cancel_event=ev)
        self.assertEqual(rc, 130)
        st = ir.read_status(self.blocked_id)
        self.assertEqual(st["latest"]["status"], "cancelled")
        self.assertIsNone(st["report"])

    def test_corrupt_store_raises_bytes_preserved(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        bad = b"{broken json"
        (root / f"attempt-{req['rid']}.json").write_bytes(bad)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(
            (root / f"attempt-{req['rid']}.json").read_bytes(), bad)

    def test_stale_parent_keeps_report_as_history(self):
        # A completed review stays readable as history after the
        # parent op bytes change: the stored report is preserved, but
        # it is no longer current. A forged latest pointer (wrong
        # fingerprint for its stored attempt) is instead an explicit
        # linkage error with bytes preserved.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(
                            canned_report(self.cand_head))),
                                        {"m": 1})):
            self.assertEqual(ir.execute_review(self.blocked_id,
                                              req["rid"]), 0)
        op_path = batches.op_file(self.blocked_id)
        doc = json.loads(op_path.read_bytes().decode("utf-8"))
        doc["note"] = "관찰 기록 추가 — 원본 바이트 변경"
        op_path.write_text(json.dumps(doc), encoding="utf-8")
        st = ir.read_status(self.blocked_id)
        self.assertIsNotNone(st["report"])
        self.assertFalse(st["current"])
        self.assertEqual(st["latest"]["status"], "done")
        # Forged fingerprint on the same pointer: linkage error.
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        latest["fingerprint"] = "f" * 64
        (root / "latest.json").write_text(json.dumps(latest),
                                          encoding="utf-8")
        with self.assertRaises(ir.IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 502)

    def test_quota_classification_preserves_raw(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        rc = self._exec(req["rid"], "quota exceeded for today",
                        exit_code=1)
        self.assertEqual(rc, 1)
        att = json.loads((ir.rec_root(self.blocked_id) /
                          f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        self.assertEqual(att["status"], "error")
        self.assertEqual(att["error_kind"], "quota")
        self.assertIn("quota", att.get("raw_stream", ""))


class StorageLinkageTests(RecoveryFixtureMixin):
    def test_traversal_name_refused_bytes_preserved(self):
        self._write_blocked_op()
        root = ir.rec_root(self.blocked_id)
        before = sorted(p.name for p in root.glob("*.json"))
        # A stored pointer naming outside the store is corruption,
        # never a readable record.
        with self.assertRaises(IssueRecoveryError) as cm:
            ir._read_record(root, "../evil.json")
        self.assertEqual(cm.exception.http_status, 502)
        self.assertEqual(sorted(p.name for p in root.glob("*.json")),
                         before)
        # The read-only status path surfaces the same corruption
        # instead of an empty view when latest itself is tampered.
        req = ir.request_review(self.blocked_id)
        op_before = self._op_bytes()
        latest_path = root / "latest.json"
        latest = json.loads(latest_path.read_bytes().decode("utf-8"))
        latest["input"] = "../evil"
        latest_path.write_text(json.dumps(latest), encoding="utf-8")
        with self.assertRaises(IssueRecoveryError) as cm2:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm2.exception.http_status, 502)
        self.assertEqual(self._op_bytes(), op_before)

    def test_symlink_record_refused_on_read_and_write(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        target = root / f"attempt-{req['rid']}.json"
        # req["input"] already carries its "input-" prefix: address
        # the actual stored input file, not a doubled name.
        input_path = root / f"{req['input']}.json"
        input_raw = input_path.read_bytes()
        target.unlink()
        target.symlink_to(input_path)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 502)
        # Writing through the alias is refused as well; the alias
        # and the link target are both left alone for inspection.
        with self.assertRaises(IssueRecoveryError):
            ir._replace_record(root, f"attempt-{req['rid']}.json",
                               {"rid": req["rid"]})
        self.assertTrue(target.is_symlink())
        self.assertEqual(input_path.read_bytes(), input_raw)

    def test_attempt_store_unreadable_is_error(self):
        self._write_blocked_op()
        ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        root.chmod(0o000)
        try:
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.read_status(self.blocked_id)
            self.assertEqual(cm.exception.http_status, 502)
        finally:
            root.chmod(0o755)

    def test_fallback_scan_reuses_done_without_latest(self):
        # The quota-free reuse scan must survive a lost latest
        # pointer: the stored done attempt carries its own input, and
        # the repaired linkage (not a manufactured new model run)
        # restores it.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(
                            canned_report(self.cand_head))),
                                        {"m": 1})):
            self.assertEqual(ir.execute_review(self.blocked_id,
                                              req["rid"]), 0)
        root = ir.rec_root(self.blocked_id)
        (root / "latest.json").unlink()
        again = ir.request_review(self.blocked_id)
        self.assertTrue(again["reused"])
        self.assertEqual(again["rid"], req["rid"])
        self.assertEqual(again["input"], req["input"])
        st = ir.read_status(self.blocked_id)
        self.assertTrue(st["current"])
        self.assertIsNotNone(st["report"])

    def test_corrupt_done_linkage_is_explicit_error(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(
                            canned_report(self.cand_head))),
                                        {"m": 1})):
            self.assertEqual(ir.execute_review(self.blocked_id,
                                              req["rid"]), 0)
        root = ir.rec_root(self.blocked_id)
        op_before = self._op_bytes()
        att_before = (root / f"attempt-{req['rid']}.json").read_bytes()
        latest_raw = (root / "latest.json").read_bytes()
        report_name = json.loads(latest_raw.decode("utf-8"))["report"]
        (root / f"{report_name}.json").unlink()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.http_status, 502)
        self.assertEqual((root / "latest.json").read_bytes(), latest_raw)
        self.assertEqual(
            (root / f"attempt-{req['rid']}.json").read_bytes(),
            att_before)
        self.assertEqual(self._op_bytes(), op_before)

    def test_bad_rid_shape_rejected_before_store(self):
        self._write_blocked_op()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.execute_review(self.blocked_id, "x")
        self.assertEqual(cm.exception.http_status, 400)
        with self.assertRaises(IssueRecoveryError) as cm2:
            ir.mark_startup_failed(self.blocked_id, "x", "boom")
        self.assertEqual(cm2.exception.http_status, 400)

    def test_startup_failure_syncs_latest(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        ir.mark_startup_failed(self.blocked_id, req["rid"], "no slot")
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        self.assertEqual(latest["status"], "error")
        self.assertEqual(latest["rid"], req["rid"])
        self.assertIn("no slot", latest.get("error", ""))
        att = json.loads((root / f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        self.assertEqual(att["status"], "error")

    def test_precancel_spends_no_quota(self):
        # _exec lives in LifecycleTests; StorageLinkageTests must call
        # the actual worker directly under one scoped transport mock.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        event = threading.Event()
        event.set()
        with mock.patch("openmainty.muse.exec_prompt") as mp:
            mp.side_effect = AssertionError("model must not run")
            rc = ir.execute_review(self.blocked_id, req["rid"],
                                   cancel_event=event)
        self.assertEqual(rc, 130)
        mp.assert_not_called()
        st = ir.read_status(self.blocked_id)
        self.assertEqual(st["latest"]["status"], "cancelled")
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])

    def test_readonly_status_under_foreign_lock(self):
        # read_status takes no lock: while an INDEPENDENT live holder
        # thread owns the op exclusion, the read still observes the
        # running job and writes nothing. A same-thread lock would
        # prove nothing, since read_status never takes the lock.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        job_id = "job-live-probe"
        serve._JOBS[job_id] = {"id": job_id, "kind": "recovery-review",
                              "status": "running", "rc": None,
                              "started_at": "t", "ended_at": None,
                              "note": "fixture"}
        self.addCleanup(lambda: serve._JOBS.pop(job_id, None))
        att = json.loads((root / f"attempt-{req['rid']}.json")
                         .read_bytes().decode("utf-8"))
        att["status"] = "running"
        att["job_id"] = job_id
        (root / f"attempt-{req['rid']}.json").write_text(
            json.dumps(att), encoding="utf-8")
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        latest["status"] = "running"
        latest["job_id"] = job_id
        (root / "latest.json").write_text(json.dumps(latest),
                                          encoding="utf-8")
        before = sorted(p.read_bytes() for p in root.glob("*.json")
                        if not p.is_symlink())
        held = threading.Event()
        released = threading.Event()

        def holder():
            with m2.op_locked(self.blocked_id):
                held.set()
                released.wait(timeout=30)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(timeout=30))
        try:
            st = ir.read_status(self.blocked_id)
        finally:
            released.set()
            t.join(timeout=30)
        self.assertEqual(st["latest"]["status"], "running")
        self.assertEqual(st["latest"]["job_id"], job_id)
        self.assertTrue(st["attempts"][0]["job_live"])
        after = sorted(p.read_bytes() for p in root.glob("*.json")
                       if not p.is_symlink())
        self.assertEqual(after, before)

    def test_cross_view_not_run_agrees(self):
        # Recovery truth and the main work report must agree: a
        # valid blocked prepare with no executed receipt is not_run
        # in both views; a done op is never a prepare conflict.
        # The held helper takes (state, run_id, prep=...): supply a
        # legitimate owned run/prep identity whose op-owned receipt
        # is genuinely absent, not a bare positional prep dict.
        self._write_blocked_op()
        prep = {"op_id": self.blocked_id, "run_id": self.run_id,
                "issue": {"issue": "856"},
                "status": "blocked",
                "result": {"path": "", "branch": "",
                           "head": "", "fingerprint": ""},
                "gates": [{"key": "source-prepared",
                           "status": "blocked"},
                          {"key": "fastchecks", "status": "pending",
                           "note": "검사가 실행되지 않음"}]}
        rep = serve.fastcheck_report_for_run({}, self.run_id, prep=prep)
        self.assertEqual(rep["state"], "not_run")
        self.assertIn("준비 충돌", rep["reason"])
        done_prep = dict(prep, status="done")
        rep2 = serve.fastcheck_report_for_run(
            {}, self.run_id, prep=done_prep)
        self.assertEqual(rep2["state"], "not_run")
        self.assertNotIn("충돌", rep2["reason"])


class RouteTests(RecoveryFixtureMixin):
    def setUp(self):
        super().setUp()
        self._gate = mock.patch.object(serve, "_require_gate",
                                       lambda: None)
        self._gate.start()
        self.addCleanup(self._gate.stop)

    def _drain_job(self, jid, timeout=30.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            job = serve._JOBS.get(jid)
            if job is not None and job.get("status") != "running":
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {jid} still running")

    def tearDown(self):
        for jid, job in list(serve._JOBS.items()):
            if isinstance(job, dict) and job.get("kind") == "recovery-review":
                serve._JOBS.pop(jid, None)
                serve._JOB_CANCEL.pop(jid, None)

    def test_get_unknown_op_404(self):
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_integration_recovery({"op": "prep-nope-x9"})
        self.assertEqual(cm.exception.status, 404)

    def test_post_unknown_op_404(self):
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_integration_recovery_review(
                {"op": "prep-nope-x9", "confirm": True})
        self.assertEqual(cm.exception.status, 404)

    def test_post_needs_confirm(self):
        self._write_blocked_op()
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_integration_recovery_review(
                {"op": self.blocked_id})
        self.assertEqual(cm.exception.status, 400)

    def _store_names(self):
        root = ir.rec_root(self.blocked_id)
        return sorted(p.name for p in root.glob("*.json")) \
            if root.is_dir() else []

    def test_busy_lease_refuses_before_write(self):
        # The lease is reentrant on the SAME thread, so a same-thread
        # hold cannot prove refusal: a distinct live holder is required.
        self._write_blocked_op()
        held = threading.Event()
        released = threading.Event()
        box = {}

        def holder():
            box["token"] = sched.acquire_worker_lease("route-busy-probe")
            held.set()
            released.wait(timeout=30)
            try:
                if box.get("token") is not None:
                    sched.release_worker_lease(box["token"])
            finally:
                box["token"] = None

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(timeout=30))
        self.assertIsNotNone(box.get("token"))
        try:
            before = self._store_names()
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_integration_recovery_review(
                    {"op": self.blocked_id, "confirm": True})
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(self._store_names(), before)
        finally:
            released.set()
            t.join(timeout=30)

    def test_op_busy_refuses_before_write(self):
        self._write_blocked_op()
        held = threading.Event()
        released = threading.Event()

        def holder():
            with m2.op_locked(self.blocked_id):
                held.set()
                released.wait(timeout=30)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(timeout=30))
        try:
            before = self._store_names()
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_integration_recovery_review(
                    {"op": self.blocked_id, "confirm": True})
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(self._store_names(), before)
        finally:
            released.set()
            t.join(timeout=30)

    def test_post_reuses_done_without_job(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(
                            canned_report(self.cand_head))),
                                        {"m": 1})):
            import threading
            with m2.op_locked(self.blocked_id):
                rc = ir.execute_review(self.blocked_id, req["rid"])
            self.assertEqual(rc, 0)
        out = serve.api_integration_recovery_review(
            {"op": self.blocked_id, "confirm": True})
        self.assertTrue(out.get("reused"))
        self.assertNotIn("job", out)
        got = serve.api_integration_recovery({"op": self.blocked_id})
        self.assertTrue(got["current"])
        self.assertEqual(got["fastcheck"]["state"], "not_run")
        self.assertFalse(got["followup_allowed"])
        self.assertFalse(got["has_prepared_source"])

    def test_post_starts_job_and_reports_terminal(self):
        self._write_blocked_op()
        patcher = mock.patch("openmainty.muse.exec_prompt",
                             return_value=(0, canned_jsonl(json.dumps(
                                 canned_report(self.cand_head))),
                                             {"m": 1}))
        patcher.start()
        self.addCleanup(patcher.stop)
        out = serve.api_integration_recovery_review(
            {"op": self.blocked_id, "confirm": True})
        self.assertIn("job", out)
        job = self._drain_job(out["job"]["id"])
        self.assertEqual(job.get("status"), "done")
        got = serve.api_integration_recovery({"op": self.blocked_id})
        self.assertTrue(got["current"])

    def test_post_running_refuses_and_get_attaches_same_job(self):
        # While the first job genuinely runs (it holds the worker
        # lease and the op lock), a second POST must refuse with 409
        # and write nothing — never queue a duplicate model run.
        # The same running job is then attached through the lock-free
        # read-only GET, which carries its identity and liveness.
        self._write_blocked_op()
        gate = threading.Event()
        entered = threading.Event()
        payload = canned_jsonl(json.dumps(
            canned_report(self.cand_head)))
        calls = []

        def _slow(*args, **kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(gate.wait(timeout=30))
            return (0, payload, {"m": 1})

        patcher = mock.patch("openmainty.muse.exec_prompt",
                             side_effect=_slow)
        patcher.start()
        self.addCleanup(patcher.stop)
        first = serve.api_integration_recovery_review(
            {"op": self.blocked_id, "confirm": True})
        self.assertIn("job", first)
        # Wait until the worker has entered the model call: by then
        # execute_review has set latest=running under the op lock, so
        # the second POST below meets a genuine busy holder instead
        # of racing the queued-to-running transition.
        self.assertTrue(entered.wait(timeout=30))
        try:
            before = self._store_names()
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_integration_recovery_review(
                    {"op": self.blocked_id, "confirm": True})
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(self._store_names(), before)
            got = serve.api_integration_recovery({"op": self.blocked_id})
            self.assertEqual(got["latest"]["status"], "running")
            self.assertEqual(got["latest"]["rid"], first["rid"])
            self.assertEqual(got["latest"]["job_id"], first["job"]["id"])
            self.assertTrue(got["attempts"][0]["job_live"])
            self.assertEqual(got["attempts"][0]["job_id"],
                             first["job"]["id"])
        finally:
            gate.set()
        self._drain_job(first["job"]["id"])
        self.assertEqual(len(calls), 1)

    def test_get_nojob_reports_no_running(self):
        self._write_blocked_op()
        ir.request_review(self.blocked_id)
        got = serve.api_integration_recovery({"op": self.blocked_id})
        self.assertEqual(got["latest"]["status"], "queued")
        self.assertIsNone(got.get("running"))
        self.assertIsNone(got.get("report"))
        self.assertFalse(got.get("current"))

    def test_no_slot_marks_startup_failed_and_releases(self):
        self._write_blocked_op()
        serve._JOBS["job-blocker"] = {"id": "job-blocker",
                                      "kind": "other",
                                      "status": "running"}
        self.addCleanup(lambda: serve._JOBS.pop("job-blocker", None))
        before = self._store_names()
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_integration_recovery_review(
                {"op": self.blocked_id, "confirm": True})
        self.assertEqual(cm.exception.status, 409)
        names = self._store_names()
        # input + attempt + latest(terminal error): the queued
        # latest pointer follows its attempt, leaving no phantom
        # queued record behind.
        self.assertEqual(len(names), len(before) + 3)
        root = ir.rec_root(self.blocked_id)
        atts = [json.loads(p.read_bytes().decode("utf-8"))
                for p in root.glob("attempt-*.json")]
        self.assertTrue(all(a["status"] == "error" for a in atts))
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        self.assertEqual(latest["status"], "error")
        # Lease must be free again for a later retry: a distinct
        # holder can acquire it now, and after release the review
        # can start a fresh job.
        serve._JOBS.pop("job-blocker", None)
        token = sched.acquire_worker_lease("route-retry-probe")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)
        # The retry mock must stay installed for the ENTIRE worker
        # lifetime — including the drain — or the background worker
        # escapes into the real model transport. A finished job is
        # only success with rc 0, a current view, and a stored
        # report; done+rc1 would be a failure, not success.
        entered = threading.Event()
        payload = canned_jsonl(json.dumps(
            canned_report(self.cand_head)))

        def _enter(*args, **kwargs):
            entered.set()
            return (0, payload, {"m": 1})

        with mock.patch("openmainty.muse.exec_prompt",
                        side_effect=_enter) as mp:
            out = serve.api_integration_recovery_review(
                {"op": self.blocked_id, "confirm": True})
            self.assertIn("job", out)
            self.assertTrue(entered.wait(timeout=30))
            job = self._drain_job(out["job"]["id"])
        self.assertEqual(job.get("status"), "done")
        self.assertEqual(job.get("rc"), 0)
        self.assertEqual(mp.call_count, 1)
        got = serve.api_integration_recovery({"op": self.blocked_id})
        self.assertTrue(got["current"])
        self.assertIsNotNone(got["report"])

    def test_missing_cancel_event_closes_job_terminal(self):
        # A missing _JOB_CANCEL entry after _job_new allocated the
        # slot must still close that slot terminally (done/rc1) and
        # mark the attempt/latest as startup errors — never leave a
        # phantom running job or spend model quota.
        self._write_blocked_op()
        real_new = serve._job_new

        def _new_no_cancel(label, kind):
            job = real_new(label, kind)
            if job is not None:
                serve._JOB_CANCEL.pop(job["id"], None)
            return job

        payload = canned_jsonl(json.dumps(
            canned_report(self.cand_head)))
        with mock.patch.object(serve, "_job_new",
                               side_effect=_new_no_cancel):
            with mock.patch("openmainty.muse.exec_prompt",
                            return_value=(0, payload, {"m": 1})) as mp:
                with self.assertRaises(serve.ApiError) as cm:
                    serve.api_integration_recovery_review(
                        {"op": self.blocked_id, "confirm": True})
        self.assertEqual(cm.exception.status, 500)
        mp.assert_not_called()
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        self.assertEqual(latest["status"], "error")
        jobs = [j for j in serve._JOBS.values()
                if isinstance(j, dict)
                and j.get("kind") == "recovery-review"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].get("status"), "done")
        self.assertEqual(jobs[0].get("rc"), 1)
        self.assertIsNotNone(jobs[0].get("ended_at"))

    def test_thread_start_failure_closes_job_terminal(self):
        # A thread-start exception after _job_new allocated the slot
        # must close it terminally and release the lease, with the
        # attempt/latest preserved as startup errors.
        self._write_blocked_op()
        payload = canned_jsonl(json.dumps(
            canned_report(self.cand_head)))

        def _boom(job, fn):
            raise RuntimeError("no thread for you")

        with mock.patch.object(serve, "_job_run",
                               side_effect=_boom):
            with mock.patch("openmainty.muse.exec_prompt",
                            return_value=(0, payload, {"m": 1})) as mp:
                with self.assertRaises(serve.ApiError) as cm:
                    serve.api_integration_recovery_review(
                        {"op": self.blocked_id, "confirm": True})
        self.assertEqual(cm.exception.status, 500)
        mp.assert_not_called()
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        self.assertEqual(latest["status"], "error")
        jobs = [j for j in serve._JOBS.values()
                if isinstance(j, dict)
                and j.get("kind") == "recovery-review"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].get("status"), "done")
        self.assertEqual(jobs[0].get("rc"), 1)
        self.assertIsNotNone(jobs[0].get("ended_at"))
        token = sched.acquire_worker_lease("startup-fault-probe")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)

    def _loopback(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def test_http_get_dispatch(self):
        self._write_blocked_op()
        server = self._loopback()
        port = server.server_address[1]
        url = (f"http://127.0.0.1:{port}/api/integration/recovery"
               f"?op={self.blocked_id}")
        with urlrequest.urlopen(url, timeout=15) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertIn("has_prepared_source", body)
        self.assertFalse(body["has_prepared_source"])

    def test_http_post_review_reuse(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(
                            canned_report(self.cand_head))),
                                        {"m": 1})):
            with m2.op_locked(self.blocked_id):
                self.assertEqual(
                    ir.execute_review(self.blocked_id, req["rid"]), 0)
        server = self._loopback()
        port = server.server_address[1]
        data = json.dumps({"op": self.blocked_id,
                           "confirm": True}).encode("utf-8")
        http_req = urlrequest.Request(
            f"http://127.0.0.1:{port}/api/integration/recovery/review",
            data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with urlrequest.urlopen(http_req, timeout=15) as resp:
            status = resp.status
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("reused"))

    def _done_with_questions_for_route(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        rep = canned_report(self.cand_head)
        rep["questions"] = [
            "첫 번째 질문: 베이스 수정을 유지할 수 있는가?",
            "두 번째 질문: dist를 소스 확정 후 재생성할 수 있는가?",
        ]
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(rep)),
                                      {"m": 1})):
            with m2.op_locked(self.blocked_id):
                self.assertEqual(
                    ir.execute_review(self.blocked_id, req["rid"]), 0)
        return serve.api_integration_recovery({"op": self.blocked_id})

    def test_route_context_requires_displayed_revision(self):
        # A NEW contextual POST that does not echo the displayed
        # exact revision must refuse 400 with no writes and no model
        # call — even when the ids name the current chain.
        got = self._done_with_questions_for_route()
        latest = got["latest"]
        shas = got["origin_shas"]
        base_ctx = {"text": "운용자 메모", "question_refs": [0],
                    "input": latest["input"], "report": latest["report"]}
        variants = [
            dict(base_ctx),
            dict(base_ctx, expected=None),
            dict(base_ctx, expected={}),
            dict(base_ctx,
                 expected={"input_sha256": shas["input"]}),
            dict(base_ctx,
                 expected={"input_sha256": "zz",
                           "report_sha256": shas["report"]}),
            dict(base_ctx, expected="seen"),
        ]
        for ctx in variants:
            before = self._store_names()
            with mock.patch("openmainty.muse.exec_prompt") as mp:
                with self.assertRaises(serve.ApiError) as cm:
                    serve.api_integration_recovery_review(
                        {"op": self.blocked_id, "confirm": True,
                         "context": ctx})
            self.assertEqual(cm.exception.status, 400)
            mp.assert_not_called()
            self.assertEqual(self._store_names(), before)

    def test_route_context_stale_revision_refuses_without_write(self):
        got = self._done_with_questions_for_route()
        latest = got["latest"]
        before = self._store_names()
        with mock.patch("openmainty.muse.exec_prompt") as mp:
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_integration_recovery_review(
                    {"op": self.blocked_id, "confirm": True,
                     "context": {"text": "운용자 메모",
                                 "question_refs": [0],
                                 "input": latest["input"],
                                 "report": latest["report"],
                                 "expected": {"input_sha256": "0" * 64,
                                              "report_sha256":
                                              "0" * 64}}})
        self.assertEqual(cm.exception.status, 409)
        mp.assert_not_called()
        self.assertEqual(self._store_names(), before)

    def test_route_context_with_revision_reuses_without_job(self):
        # A fully-revisioned contextual POST for an identical
        # already-completed continuation reuses with no job, no
        # transport, and no new artifacts.
        got = self._done_with_questions_for_route()
        latest = got["latest"]
        shas = got["origin_shas"]
        expected = {"input_sha256": shas["input"],
                    "report_sha256": shas["report"]}
        with m2.op_locked(self.blocked_id):
            rec = ir.record_recovery_context(
                self.blocked_id, "운용자 메모", question_refs=[0],
                origin_input=latest["input"],
                origin_report=latest["report"],
                expected_input_sha=shas["input"],
                expected_report_sha=shas["report"])
            req = ir.request_review(self.blocked_id,
                                    context_id=rec["context"])
        rep = canned_report(self.cand_head)
        rep["questions"] = [
            "첫 번째 질문: 베이스 수정을 유지할 수 있는가?",
            "두 번째 질문: dist를 소스 확정 후 재생성할 수 있는가?",
        ]
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(0, canned_jsonl(json.dumps(rep)),
                                      {"m": 1})):
            with m2.op_locked(self.blocked_id):
                self.assertEqual(
                    ir.execute_review(self.blocked_id, req["rid"]), 0)
        before = self._store_names()
        with mock.patch("openmainty.muse.exec_prompt") as mp:
            out = serve.api_integration_recovery_review(
                {"op": self.blocked_id, "confirm": True,
                 "context": {"text": "운용자 메모",
                             "question_refs": [0],
                             "input": latest["input"],
                             "report": latest["report"],
                             "expected": expected}})
        mp.assert_not_called()
        self.assertTrue(out.get("reused"))
        self.assertNotIn("job", out)
        self.assertEqual(out.get("rid"), req["rid"])
        self.assertTrue(out.get("context", {}).get("reused"))
        self.assertEqual(self._store_names(), before)


class HistoricalAttributionTests(RecoveryFixtureMixin):
    """A5: original-history attribution and record-chain negatives.

    Structural failures (wrong-kind/not-blocked/bad-identity) never
    become history; live identity drift cannot reattribute an old
    report; missing or foreign chain links stay errors even under
    drift; snapshot tampering is rejected in historical mode; a
    legitimately stale history stays inspectable with stored labels
    and unchanged bytes. All fixtures use the real temp profile,
    queue, run, proof, and Git checkouts; the only mocked boundary is
    the model subscription transport.
    """

    def test_wrong_kind_parent_is_not_history(self):
        self._done()
        after = self._rewrite_op(lambda r: r.update(op="refresh"))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "wrong-kind")
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(batches.op_file(
            self.blocked_id).read_bytes(), after)

    def test_not_blocked_parent_is_not_history(self):
        self._done()
        after = self._rewrite_op(lambda r: r.update(status="done"))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "not-blocked")
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(batches.op_file(
            self.blocked_id).read_bytes(), after)

    def test_bad_identity_parent_is_not_history(self):
        self._done()
        after = self._rewrite_op(
            lambda r: r["issue"].update(issue="not-a-number!"))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "bad-identity")
        self.assertEqual(cm.exception.http_status, 400)
        self.assertEqual(batches.op_file(
            self.blocked_id).read_bytes(), after)

    def test_live_issue_drift_keeps_stored_labels(self):
        req = self._done()
        after = self._rewrite_op(lambda r: r["issue"].update(
            issue="857",
            url=r["issue"]["url"].replace("/856", "/857")))
        st = ir.read_status(self.blocked_id)
        self.assertFalse(st["current"])
        self.assertEqual(st["binding_error"]["code"], "stale")
        # The stored 856 labels win; the drifted 857 parent is never
        # presented as the reviewed source.
        self.assertEqual(st["issue"]["issue"], "856")
        self.assertIsNotNone(st["report"])
        self.assertEqual(st["report"]["summary"],
                         canned_report(self.cand_head)["summary"])
        self.assertEqual(st["report"]["citations"][0]["ref"],
                         self.cand_head)
        self.assertEqual(st["latest"]["status"], "stale")
        self.assertEqual(st["latest"]["rid"], req["rid"])
        self.assertEqual(batches.op_file(
            self.blocked_id).read_bytes(), after)

    def test_base_drift_history_stays_inspectable(self):
        req = self._done()
        op_after = self._op_bytes()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        st = ir.read_status(self.blocked_id)
        self.assertFalse(st["current"])
        self.assertEqual(st["binding_error"]["code"], "stale")
        self.assertEqual(st["issue"]["issue"], "856")
        self.assertEqual(st["candidate"]["head"], self.cand_head)
        self.assertIsNotNone(st["report"])
        self.assertEqual(st["report"]["summary"],
                         canned_report(self.cand_head)["summary"])
        self.assertEqual(st["latest"]["status"], "stale")
        self.assertEqual(st["latest"]["rid"], req["rid"])
        self.assertEqual(st["attempt_total"], 1)
        self.assertEqual(len(st["attempts"]), 1)
        self.assertEqual(self._op_bytes(), op_after)

    def test_run_drift_keeps_completed_report_but_not_current(self):
        req = self._done()
        from openmainty import store as _store
        run_path = _store.run_file(self.run_id)
        run_path.write_bytes(run_path.read_bytes() + b"\n")
        st = ir.read_status(self.blocked_id)
        self.assertFalse(st["current"])
        self.assertEqual(st["issue"]["issue"], "856")
        self.assertIsNotNone(st["report"])
        self.assertEqual(st["latest"]["status"], "done")
        self.assertEqual(st["latest"]["rid"], req["rid"])

    def test_missing_owner_link_stays_corrupt_under_drift(self):
        req = self._done()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        root = ir.rec_root(self.blocked_id)
        att_path = root / f"attempt-{req['rid']}.json"
        att_path.unlink()
        latest_raw = (root / "latest.json").read_bytes()
        inp_path = root / f"{req['input']}.json"
        inp_raw = inp_path.read_bytes()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual((root / "latest.json").read_bytes(), latest_raw)
        self.assertEqual(inp_path.read_bytes(), inp_raw)
        # Only the deliberately removed attempt is gone; nothing was
        # healed or rebound.
        self.assertFalse(att_path.exists())

    def test_foreign_report_link_rejected_under_drift(self):
        req = self._done()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        rep_path = root / f"{latest['report']}.json"
        rep_before = rep_path.read_bytes()
        doc = json.loads(rep_before.decode("utf-8"))
        doc["rid"] = "rr-foreign000000000000000000000000000000"
        rep_path.write_bytes(json.dumps(doc).encode("utf-8"))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(rep_path.read_bytes(),
                         json.dumps(doc).encode("utf-8"))
        self.assertNotEqual(rep_path.read_bytes(), rep_before)

    def test_snapshot_text_tamper_rejected_in_historical_mode(self):
        req = self._done()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        doc["snapshot"]["files"][0]["text"] += "tampered"
        inp_path.write_bytes(json.dumps(doc).encode("utf-8"))
        tampered = inp_path.read_bytes()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        # Tampered bytes are preserved for inspection, never healed.
        self.assertEqual(inp_path.read_bytes(), tampered)

    def test_snapshot_ref_tamper_rejected_in_historical_mode(self):
        req = self._done()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        doc["snapshot"]["files"][0]["ref"] = "d" * 40
        inp_path.write_bytes(json.dumps(doc).encode("utf-8"))
        tampered = inp_path.read_bytes()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(inp_path.read_bytes(), tampered)

    def test_report_quote_tamper_rejected_in_historical_mode(self):
        req = self._done()
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        root = ir.rec_root(self.blocked_id)
        latest = json.loads((root / "latest.json")
                            .read_bytes().decode("utf-8"))
        rep_path = root / f"{latest['report']}.json"
        doc = json.loads(rep_path.read_bytes().decode("utf-8"))
        doc["report"]["citations"][0]["quote"] = "never sent to model"
        rep_path.write_bytes(json.dumps(doc).encode("utf-8"))
        tampered = rep_path.read_bytes()
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "invalid-report")
        self.assertEqual(rep_path.read_bytes(), tampered)

    def test_within_store_op_alias_refused(self):
        self._write_blocked_op()
        from openmainty import paths as _paths
        store = _paths.home() / "issue_recovery"
        sibling = store / "sibling-target"
        sibling.mkdir(parents=True, exist_ok=True)
        alias = store / self.blocked_id
        alias.symlink_to(sibling, target_is_directory=True)
        try:
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.read_status(self.blocked_id)
            self.assertEqual(cm.exception.code, "corrupt")
        finally:
            alias.unlink()
        self.assertTrue(sibling.is_dir())

    def test_store_ancestor_alias_refused(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        from openmainty import paths as _paths
        store = _paths.home() / "issue_recovery"
        real = _paths.home() / "issue_recovery_real"
        store.rename(real)
        try:
            store.symlink_to(real, target_is_directory=True)
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.read_status(self.blocked_id)
            self.assertEqual(cm.exception.code, "corrupt")
            # The review written before the alias is untouched.
            self.assertTrue((real / self.blocked_id /
                             f"attempt-{req['rid']}.json").is_file())
        finally:
            store.unlink()
            real.rename(_paths.home() / "issue_recovery")
        self.assertTrue((store / self.blocked_id /
                         f"attempt-{req['rid']}.json").is_file())


class SharedChainValidationTests(RecoveryFixtureMixin):
    """Source17v7: shared original-chain binding for the five probes.

    The canonical refs map, the displayed original failure/conflict
    scope, every lifecycle owner link, the pre-transport worker gate,
    and new-input reuse are all bound to the SAME canonical
    fingerprint and the SAME owner validator — never a weaker copied
    checker. Real temp fixtures; only the model subscription
    transport is mocked. Authored UNEXECUTED for the Root fresh-home
    diagnostic gate.
    """

    def _stored_input(self, req):
        root = ir.rec_root(self.blocked_id)
        path = root / f"{req['input']}.json"
        return root, path, json.loads(path.read_bytes().decode("utf-8"))

    def _rewrite_input(self, req, fn):
        root, path, _doc = self._stored_input(req)
        doc = json.loads(path.read_bytes().decode("utf-8"))
        fn(doc)
        path.write_bytes(json.dumps(doc).encode("utf-8"))
        return root, path.read_bytes()

    def test_snapshot_refs_map_is_bound_to_original_fingerprint(self):
        req = self._done()
        _root, path, doc = self._stored_input(req)
        other = "e" * 40
        self.assertNotEqual(doc["snapshot"]["refs"]["candidate"], other)
        # Change ONLY the canonical refs map: every file head, the
        # prompt text, and all hashes stay exactly as collected.
        _root, tampered = self._rewrite_input(
            req, lambda d: d["snapshot"]["refs"].update(candidate=other))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(path.read_bytes(), tampered)

    def test_full_candidate_and_base_descriptors_are_bound(self):
        req = self._done()
        _root, path, doc = self._stored_input(req)
        self.assertNotEqual(doc["candidate"]["branch"],
                            "never-reviewed-original-branch")
        # Change ONLY the full descriptors: candidate branch and base
        # checkout. Heads, fingerprints, refs map, prompt, hashes,
        # error, and conflicts stay exactly as collected.
        def tamper(d):
            d["candidate"]["branch"] = "never-reviewed-original-branch"
            d["base"]["checkout"] = str(
                Path(self.base_bar_wt).parent / "elsewhere-checkout")
        _root, tampered = self._rewrite_input(req, tamper)
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(path.read_bytes(), tampered)

    def test_original_error_and_conflict_scope_are_bound(self):
        req = self._done()
        def tamper(doc):
            doc["op_error"] = "never-investigated"
            doc["conflicted_paths"] = ["never-original.py"]
        _root, tampered = self._rewrite_input(req, tamper)
        # Owned live drift: base checkout changes, so the live
        # binding is stale and history is served from stored
        # originals — which no longer prove their identity.
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        _check = ir.rec_root(self.blocked_id) / f"{req['input']}.json"
        self.assertEqual(_check.read_bytes(), tampered)

    def test_nonterminal_owner_identity_checked_under_live_drift(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        self.assertFalse(req["reused"])
        root = ir.rec_root(self.blocked_id)
        att_path = root / f"attempt-{req['rid']}.json"
        att = json.loads(att_path.read_bytes().decode("utf-8"))
        att["op"] = "foreign-operation"
        att_path.write_bytes(json.dumps(att).encode("utf-8"))
        mutated = att_path.read_bytes()
        # Owned base drift forces the historical view; the queued
        # owner link is still validated, not rendered as history.
        (self.base_bar_wt / "bar.txt").write_text("changed\n",
                                                  encoding="utf-8")
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(att_path.read_bytes(), mutated)

    def test_worker_rejects_wrong_input_kind_before_transport(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        doc["kind"] = "foreign-input-kind"
        inp_path.write_bytes(json.dumps(doc).encode("utf-8"))
        tampered = inp_path.read_bytes()
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.execute_review(self.blocked_id, req["rid"])
            spy.assert_not_called()
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(inp_path.read_bytes(), tampered)

    def test_changed_input_does_not_corrupt_previous_completed_review(self):
        req = self._done()
        old_input, old_rid = req["input"], req["rid"]
        root = ir.rec_root(self.blocked_id)
        old_input_raw = (root / f"{old_input}.json").read_bytes()
        old_att_raw = (root / f"attempt-{old_rid}.json").read_bytes()
        # Harmless parent note: new parent bytes, source proof valid.
        noted = self._rewrite_op(
            lambda r: r.update(note="harmless operator note"))
        req2 = ir.request_review(self.blocked_id)
        self.assertFalse(req2["reused"])
        self.assertFalse(req2.get("attached", False))
        self.assertNotEqual(req2["input"], old_input)
        # The old completed chain is preserved byte-for-byte, and the
        # parent still carries the note (no healing or rebinding).
        self.assertEqual((root / f"{old_input}.json").read_bytes(),
                         old_input_raw)
        self.assertEqual((root / f"attempt-{old_rid}.json").read_bytes(),
                         old_att_raw)
        self.assertEqual(self._op_bytes(), noted)


class CitationDiagnosticsTests(RecoveryFixtureMixin):
    """Source19 narrow slice: explicit TOTAL/SAVED failure diagnostics.

    Offline only: synthetic local fixtures, mocked model transport,
    isolated OPENMAINTY_HOME from the mixin. No network, no target
    writes. Tests are AUTHORED but NOT EXECUTED in this turn.
    """

    def _exec(self, rid, raw_jsonl, exit_code=0, cancel_event=None):
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(exit_code, raw_jsonl, {"m": 1})):
            return ir.execute_review(self.blocked_id, rid,
                                     cancel_event=cancel_event)

    def _read_attempt(self, rid):
        root = ir.rec_root(self.blocked_id)
        return json.loads((root / f"attempt-{rid}.json")
                          .read_bytes().decode("utf-8"))

    def _sent_two_lines(self):
        ref = "a" * 40
        text = "issue change\nonly here: line-two-marker"
        sent = [{"path": "f.txt", "ref": ref,
                 "text": text, "line_count": 2}]
        return ref, sent

    def _base_report(self, ref):
        return {
            "status": "needs_changes",
            "summary": "후보 f.txt 변경이 기준과 충돌한다",
            "cause": "동일 경로 양쪽 수정",
            "conflict_files": ["f.txt"],
            "citations": [{"path": "f.txt", "ref": ref,
                           "start_line": 2, "end_line": 2,
                           "quote": "only here: line-two-marker"}],
            "intended_changes": ["f.txt 충돌 해소"],
            "proposed_paths": ["f.txt"],
            "build_contract": None,
            "questions": [],
            "uncertainties": [],
            "limits": ["생성물은 모델에 보내지 않음"],
            "next_action": "충돌 범위를 검토한 뒤 별도 승인으로 파생 준비",
        }

    def test_omitted_numbers_default_range_rejects_outside_quote(self):
        # Same sent path/ref carries two actual lines; the exact target
        # quote lives only on line 2. Omitted start/end default to 1/1,
        # so the declared line-1 window must reject even though a
        # whole-evidence substring search would accept.
        ref, sent = self._sent_two_lines()
        self.assertIn("only here: line-two-marker", sent[0]["text"])
        bad = self._base_report(ref)
        bad["citations"] = [{"path": "f.txt", "ref": ref,
                             "quote": "only here: line-two-marker"}]
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.validate_report(bad, sent, [])
        self.assertEqual(cm.exception.code, "invalid-report")
        self.assertIn("citation quote not found in declared sent lines",
                      str(cm.exception))

    def test_explicit_accurate_range_accepts(self):
        # The SAME evidence and SAME quote with explicit
        # start_line=end_line=2 must accept; validator/default 1/1 is
        # unchanged for omitted numbers.
        ref, sent = self._sent_two_lines()
        good = self._base_report(ref)
        good["citations"] = [{"path": "f.txt", "ref": ref,
                              "start_line": 2, "end_line": 2,
                              "quote": "only here: line-two-marker"}]
        self.assertEqual(ir.validate_report(good, sent, []), good)

    def test_prompt_requires_both_numbers_and_declared_range(self):
        # The real incident failed on omitted citation line numbers, so
        # bind BOTH concrete required-number contracts and the exact
        # declared-range instruction — in the schema and in the actual
        # stored prompt/sent_text — not a generic single 필수 token.
        schema = ir._REPORT_SCHEMA
        self.assertIn('"start_line": int(>=1, 필수)', schema)
        self.assertIn('"end_line": int(>=start_line, 필수)', schema)
        self.assertIn("각 citation은 start_line/end_line을 "
                      "1부터 시작하는 실제 전송 줄 번호로", schema)
        self.assertIn("반드시 기재하라(생략·추측 금지)", schema)
        self.assertIn("선언한 줄 범위 안의 실제", schema)
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        stored = json.loads((root / f"{req['input']}.json")
                            .read_bytes().decode("utf-8"))
        sent_text = stored["snapshot"]["sent_text"]
        self.assertIn('"start_line": int(>=1, 필수)', sent_text)
        self.assertIn('"end_line": int(>=start_line, 필수)', sent_text)
        self.assertIn("반드시 기재하라(생략·추측 금지)", sent_text)
        self.assertIn("선언한 줄 범위 안의 실제", sent_text)

    def test_long_sdk_prefix_after_rawcap_keeps_final_diagnostic(self):
        # A long synthetic SDK prefix pushes the final visible response
        # past the stored raw-stream prefix cap. canned_jsonl emits an
        # 'investigating' delta plus a run_terminal holding the supplied
        # text; the expected final extraction is exactly that terminal
        # text. The failed validation must be the declared-range error
        # (not empty/unparseable), with exact TOTAL/SAVED metadata, and
        # the final response must survive separately beyond the capped
        # raw prefix.
        self._write_blocked_op()
        before = self._op_bytes()
        req = ir.request_review(self.blocked_id)
        bad = canned_report(self.cand_head)
        bad["citations"] = [{"path": "f.txt", "ref": self.cand_head,
                             "start_line": 1, "end_line": 1,
                             "quote": "never sent to the model"}]
        expected_terminal = json.dumps(bad)
        filler = "P" * (ir._RAW_STREAM_CAP + 5000)
        raw = filler + "\n" + canned_jsonl(expected_terminal)
        rc = self._exec(req["rid"], raw)
        self.assertEqual(rc, 1)
        att = self._read_attempt(req["rid"])
        self.assertEqual(att["status"], "error")
        self.assertIn("citation quote not found in declared sent lines",
                      att.get("error") or "")
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertEqual(st["latest"]["status"], "error")
        self.assertEqual(st["latest"]["report"], None)
        # Raw stream: exact TOTAL vs SAVED with unambiguous units.
        self.assertEqual(att["raw_stream_total_chars"], len(raw))
        self.assertEqual(att["raw_stream_total_bytes"],
                         len(raw.encode("utf-8")))
        self.assertEqual(att["raw_stream_saved_chars"],
                         ir._RAW_STREAM_CAP)
        self.assertEqual(att["raw_stream_saved_bytes"],
                         len(att["raw_stream"].encode("utf-8")))
        self.assertEqual(att["raw_stream_cap_chars"],
                         ir._RAW_STREAM_CAP)
        self.assertTrue(att["raw_stream_truncated"])
        self.assertEqual(len(att["raw_stream"]), ir._RAW_STREAM_CAP)
        # The final response sits beyond the capped raw prefix yet
        # survives separately as the extracted visible diagnostic.
        self.assertNotIn("never sent to the model",
                         att.get("raw_stream") or "")
        self.assertEqual(att["model_text"], expected_terminal)
        self.assertEqual(att["model_text_total_chars"],
                         len(expected_terminal))
        self.assertEqual(att["model_text_total_bytes"],
                         len(expected_terminal.encode("utf-8")))
        self.assertEqual(att["model_text_saved_chars"],
                         len(expected_terminal))
        self.assertEqual(att["model_text_saved_bytes"],
                         len(expected_terminal.encode("utf-8")))
        self.assertEqual(att["model_text_cap_chars"],
                         ir._MODEL_TEXT_CAP)
        self.assertFalse(att["model_text_truncated"])
        self.assertIn("never sent to the model",
                      att.get("model_text") or "")
        self.assertEqual(self._op_bytes(), before)

    def test_excessive_multibyte_final_response_stays_bounded(self):
        # Precisely "한" * 25000: the expected final extraction is
        # exactly that terminal text (25000 chars / 75000 UTF-8 bytes),
        # saved bounded to the 20000-char cap (60000 bytes).
        self._write_blocked_op()
        before = self._op_bytes()
        req = ir.request_review(self.blocked_id)
        expected_terminal = "한" * 25000
        raw = canned_jsonl(expected_terminal)
        rc = self._exec(req["rid"], raw)
        self.assertEqual(rc, 1)
        att = self._read_attempt(req["rid"])
        self.assertEqual(att["status"], "error")
        self.assertIn("unparseable model output",
                      att.get("error") or "")
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertEqual(st["latest"]["status"], "error")
        self.assertEqual(st["latest"]["report"], None)
        self.assertEqual(att["model_text"], "한" * 20000)
        self.assertEqual(att["model_text_total_chars"], 25000)
        self.assertEqual(att["model_text_total_bytes"], 75000)
        self.assertEqual(att["model_text_saved_chars"],
                         ir._MODEL_TEXT_CAP)
        self.assertEqual(len(att["model_text"]), ir._MODEL_TEXT_CAP)
        self.assertEqual(att["model_text_saved_bytes"], 60000)
        self.assertEqual(len(att["model_text"].encode("utf-8")), 60000)
        self.assertTrue(att["model_text_truncated"])
        self.assertEqual(att["model_text_cap_chars"],
                         ir._MODEL_TEXT_CAP)
        self.assertEqual(self._op_bytes(), before)

    def test_parse_empty_vs_no_extractor_stays_truthful(self):
        # Extractor-ran empty output records the exact whitespace text
        # with consistent zero-scale totals; a transport failure that
        # never ran the extractor records no model_text fields at all.
        self._write_blocked_op()
        before = self._op_bytes()
        req_empty = ir.request_review(self.blocked_id)
        raw_empty = canned_jsonl("   ")
        rc = self._exec(req_empty["rid"], raw_empty)
        self.assertEqual(rc, 1)
        empty_att = self._read_attempt(req_empty["rid"])
        self.assertEqual(empty_att["status"], "error")
        self.assertIn("model produced no output text",
                      empty_att.get("error") or "")
        self.assertEqual(empty_att.get("model_text"), "   ")
        self.assertEqual(empty_att.get("model_text_total_chars"), 3)
        self.assertEqual(empty_att.get("model_text_total_bytes"), 3)
        self.assertEqual(empty_att.get("model_text_saved_chars"), 3)
        self.assertEqual(empty_att.get("model_text_saved_bytes"), 3)
        self.assertEqual(empty_att.get("model_text_cap_chars"),
                         ir._MODEL_TEXT_CAP)
        self.assertFalse(empty_att.get("model_text_truncated"))
        self.assertEqual(empty_att.get("raw_stream_total_chars"),
                         len(raw_empty))
        self.assertEqual(empty_att.get("raw_stream_total_bytes"),
                         len(raw_empty.encode("utf-8")))
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertEqual(st["latest"]["status"], "error")
        self.assertEqual(st["latest"]["report"], None)
        # Second attempt slot: transport failure never extracts text.
        req_quota = ir.request_review(self.blocked_id)
        self.assertNotEqual(req_quota["rid"], req_empty["rid"])
        raw_quota = "quota exceeded for today"
        rc2 = self._exec(req_quota["rid"], raw_quota,
                         exit_code=1)
        self.assertEqual(rc2, 1)
        quota_att = self._read_attempt(req_quota["rid"])
        self.assertEqual(quota_att["status"], "error")
        self.assertEqual(quota_att.get("error_kind"), "quota")
        self.assertEqual(quota_att.get("raw_stream_total_chars"),
                         len(raw_quota))
        self.assertEqual(quota_att.get("raw_stream_total_bytes"),
                         len(raw_quota.encode("utf-8")))
        for key in ("model_text", "model_text_total_chars",
                    "model_text_total_bytes", "model_text_saved_chars",
                    "model_text_saved_bytes", "model_text_cap_chars",
                    "model_text_truncated", "model_text_chars",
                    "model_text_bytes"):
            self.assertNotIn(key, quota_att)
        st2 = ir.read_status(self.blocked_id)
        self.assertIsNone(st2["report"])
        self.assertEqual(st2["latest"]["status"], "error")
        self.assertEqual(st2["latest"]["report"], None)
        self.assertEqual(self._op_bytes(), before)

    def test_guard_failure_preserves_bytes_without_transport(self):
        # A stored-input guard failure must fail closed before the
        # model transport, with all bytes preserved.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        before = self._op_bytes()
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        doc["snapshot"]["sent_text"] += "\n추가된 줄"
        inp_path.write_text(json.dumps(doc), encoding="utf-8")
        raw_before = inp_path.read_bytes()
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.execute_review(self.blocked_id, req["rid"])
            spy.assert_not_called()
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(inp_path.read_bytes(), raw_before)
        self.assertEqual(self._op_bytes(), before)

    def test_post_model_mutation_retains_bounded_diagnostics(self):
        # Genuine POST-extraction guard: the transport itself mutates a
        # local synthetic source mid-model. The report would otherwise
        # validate, but post-model revalidation must mark stale with no
        # valid report while retaining the exact bounded final-response
        # diagnostic. The intended mutation stays as evidence.
        self._write_blocked_op()
        before_op = self._op_bytes()
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        input_before = (root / f"{req['input']}.json").read_bytes()
        good = canned_report(self.cand_head)
        expected_terminal = json.dumps(good)
        payload = canned_jsonl(expected_terminal)
        mutated = "mutated mid-model\n"

        def _run(*args, **kwargs):
            (self.run_work / "f.txt").write_text(mutated,
                                                 encoding="utf-8")
            return (0, payload, {"m": 1})

        with mock.patch("openmainty.muse.exec_prompt",
                        side_effect=_run):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 1)
        att = self._read_attempt(req["rid"])
        self.assertEqual(att["status"], "stale")
        self.assertTrue(att.get("error"))
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])
        self.assertEqual(st["latest"]["status"], "stale")
        self.assertEqual(st["latest"]["report"], None)
        self.assertEqual(att.get("model_text"), expected_terminal)
        self.assertEqual(att.get("model_text_total_chars"),
                         len(expected_terminal))
        self.assertEqual(att.get("model_text_total_bytes"),
                         len(expected_terminal.encode("utf-8")))
        self.assertEqual(att.get("model_text_saved_chars"),
                         len(expected_terminal))
        self.assertEqual(att.get("model_text_saved_bytes"),
                         len(expected_terminal.encode("utf-8")))
        self.assertEqual(att.get("model_text_cap_chars"),
                         ir._MODEL_TEXT_CAP)
        self.assertFalse(att.get("model_text_truncated"))
        self.assertEqual(att.get("raw_stream_total_chars"), len(payload))
        self.assertEqual(att.get("raw_stream_total_bytes"),
                         len(payload.encode("utf-8")))
        self.assertEqual(att.get("raw_stream_saved_chars"), len(payload))
        self.assertFalse(att.get("raw_stream_truncated"))
        self.assertEqual((self.run_work / "f.txt")
                         .read_text(encoding="utf-8"), mutated)
        self.assertEqual((root / f"{req['input']}.json").read_bytes(),
                         input_before)
        self.assertEqual(self._op_bytes(), before_op)


class ContextContinuationTests(RecoveryFixtureMixin):
    """Source21: typed supplemental operator context -> explicitly
    requested NEW immutable review input -> existing review lifecycle.

    Offline only: synthetic local fixtures, mocked model transport,
    isolated OPENMAINTY_HOME from the mixin. No network, no target
    writes. Tests are AUTHORED but NOT EXECUTED in this turn.
    """

    def _exec(self, rid, raw_jsonl, exit_code=0):
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(exit_code, raw_jsonl, {"m": 1})):
            return ir.execute_review(self.blocked_id, rid)

    def _report_with_questions(self, cand_head):
        rep = canned_report(cand_head)
        rep["questions"] = [
            "첫 번째 질문: 베이스 수정을 유지할 수 있는가?",
            "두 번째 질문: dist를 소스 확정 후 재생성할 수 있는가?",
        ]
        return rep

    def _done_with_questions(self):
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        rep = self._report_with_questions(self.cand_head)
        rc = self._exec(req["rid"], canned_jsonl(json.dumps(rep)))
        self.assertEqual(rc, 0)
        return req

    def _record_ctx(self, text, refs, origin_input, origin_report,
                    exp_in=None, exp_rep=None):
        with m2.op_locked(self.blocked_id):
            return ir.record_recovery_context(
                self.blocked_id, text, question_refs=refs,
                origin_input=origin_input, origin_report=origin_report,
                expected_input_sha=exp_in, expected_report_sha=exp_rep)

    def _request_ctx(self, cid):
        with m2.op_locked(self.blocked_id):
            return ir.request_review(self.blocked_id, context_id=cid)

    def _read_json(self, name):
        root = ir.rec_root(self.blocked_id)
        return json.loads((root / name).read_bytes().decode("utf-8"))

    def _names(self, *globs):
        root = ir.rec_root(self.blocked_id)
        out = []
        for glob in globs:
            out.extend(sorted(p.name for p in root.glob(glob)))
        return out

    def test_context_binds_exact_origin_bytes_and_new_identity(self):
        # The context record binds exact raw origin SHAs plus the
        # actual referenced question texts; same source + new context
        # yields a different fingerprint; the operator note and the
        # real question text reach the exact transmitted prompt.
        req_a = self._done_with_questions()
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        shas = st["origin_shas"]
        self.assertIsNotNone(shas)
        root = ir.rec_root(self.blocked_id)
        self.assertEqual(
            shas["input"],
            hashlib.sha256(
                (root / f"{latest['input']}.json").read_bytes()
            ).hexdigest())
        self.assertEqual(
            shas["report"],
            hashlib.sha256(
                (root / f"{latest['report']}.json").read_bytes()
            ).hexdigest())
        stored_a = self._read_json(f"{latest['input']}.json")
        self.assertNotIn("context", stored_a)
        rec = self._record_ctx(
            "운용자 메모: 베이스 유지 가능", [0],
            latest["input"], latest["report"],
            exp_in=shas["input"], exp_rep=shas["report"])
        cid = rec["context"]
        self.assertTrue(cid.startswith("context-"))
        self.assertFalse(rec["reused"])
        doc = self._read_json(f"{cid}.json")
        self.assertEqual(doc["actor"], "operator")
        self.assertEqual(doc["origin"]["input_sha256"], shas["input"])
        self.assertEqual(doc["origin"]["report_sha256"], shas["report"])
        self.assertEqual(doc["origin"]["questions"],
                         [{"index": 0,
                           "text": "첫 번째 질문: 베이스 수정을 유지할 수 있는가?"}])
        self.assertEqual(doc["id"], cid)
        req_b = self._request_ctx(cid)
        self.assertNotEqual(req_b["input"], latest["input"])
        self.assertNotEqual(req_b["fingerprint"],
                            stored_a["fingerprint"])
        stored_b = self._read_json(f"{req_b['input']}.json")
        sent_text = stored_b["snapshot"]["sent_text"]
        self.assertIn("[1] 첫 번째 질문: 베이스 수정을 유지할 수 있는가?",
                      sent_text)
        self.assertIn("운용자 메모: 베이스 유지 가능", sent_text)
        self.assertIn("운용자 제공 추가 맥락", sent_text)
        rep_b = self._report_with_questions(self.cand_head)
        rc = self._exec(req_b["rid"], canned_jsonl(json.dumps(rep_b)))
        self.assertEqual(rc, 0)
        st2 = ir.read_status(self.blocked_id)
        self.assertTrue(st2["current"])
        self.assertEqual(st2["latest"]["input"], req_b["input"])
        self.assertTrue(st2["context_allowed"])
        self.assertEqual(len(st2["contexts"]), 1)
        self.assertEqual(st2["contexts"][0]["text"],
                         "운용자 메모: 베이스 유지 가능")
        self.assertEqual(st2["contexts"][0]["origin"]["report"],
                         latest["report"])

    def test_context_idempotent_across_latest_moves(self):
        # A -> contextB done -> contextC done -> resubmit B returns
        # the already-created B result WITHOUT duplicating and WITHOUT
        # re-anchoring latest (which stays C).
        req_a = self._done_with_questions()
        st_a = ir.read_status(self.blocked_id)
        latest_a = st_a["latest"]
        rec_b = self._record_ctx(
            "메모B: 두 번째 질문 확인", [1],
            latest_a["input"], latest_a["report"],
            exp_in=st_a["origin_shas"]["input"],
            exp_rep=st_a["origin_shas"]["report"])
        req_b = self._request_ctx(rec_b["context"])
        rc = self._exec(req_b["rid"], canned_jsonl(json.dumps(
            self._report_with_questions(self.cand_head))))
        self.assertEqual(rc, 0)
        st_b = ir.read_status(self.blocked_id)
        latest_b = st_b["latest"]
        self.assertEqual(latest_b["input"], req_b["input"])
        rec_c = self._record_ctx(
            "메모C: 첫 번째 질문 확인", [0],
            latest_b["input"], latest_b["report"],
            exp_in=st_b["origin_shas"]["input"],
            exp_rep=st_b["origin_shas"]["report"])
        req_c = self._request_ctx(rec_c["context"])
        rc = self._exec(req_c["rid"], canned_jsonl(json.dumps(
            self._report_with_questions(self.cand_head))))
        self.assertEqual(rc, 0)
        st_c = ir.read_status(self.blocked_id)
        self.assertEqual(st_c["latest"]["input"], req_c["input"])
        before = self._names("input-*.json", "attempt-*.json",
                             "context-*.json")
        # Identical resubmission of B after latest moved to C.
        rec_b2 = self._record_ctx(
            "메모B: 두 번째 질문 확인", [1],
            latest_a["input"], latest_a["report"])
        self.assertTrue(rec_b2["reused"])
        self.assertEqual(rec_b2["context"], rec_b["context"])
        resub = self._request_ctx(rec_b["context"])
        self.assertTrue(resub["reused"])
        self.assertFalse(resub["attached"])
        self.assertEqual(resub["rid"], req_b["rid"])
        self.assertEqual(resub["input"], req_b["input"])
        self.assertIsNotNone(resub.get("report"))
        latest_now = self._read_json("latest.json")
        self.assertEqual(latest_now["input"], req_c["input"])
        self.assertEqual(self._names("input-*.json", "attempt-*.json",
                                     "context-*.json"), before)

    def test_orphan_context_refused_before_any_write(self):
        # A recorded context consumed for the FIRST time after latest
        # moved elsewhere refuses 409 with no new input/attempt write.
        self._done_with_questions()
        st_a = ir.read_status(self.blocked_id)
        latest_a = st_a["latest"]
        orphan = self._record_ctx(
            "고아 메모", [0], latest_a["input"], latest_a["report"])
        other = self._record_ctx(
            "선행 메모", [1], latest_a["input"], latest_a["report"])
        req_other = self._request_ctx(other["context"])
        rc = self._exec(req_other["rid"], canned_jsonl(json.dumps(
            self._report_with_questions(self.cand_head))))
        self.assertEqual(rc, 0)
        before = self._names("input-*.json", "attempt-*.json",
                             "context-*.json")
        with self.assertRaises(IssueRecoveryError) as cm:
            self._request_ctx(orphan["context"])
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(cm.exception.code, "stale-context-origin")
        self.assertEqual(self._names("input-*.json", "attempt-*.json",
                                     "context-*.json"), before)

    def test_expected_revision_mismatch_refuses_no_write(self):
        # GET old report -> schema-valid report text change -> POST old
        # revision refuses 409 with no write; a fresh revision binds
        # the LIVE question text, never the caller's stale view.
        self._done_with_questions()
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        old_in, old_rep = (st["origin_shas"]["input"],
                           st["origin_shas"]["report"])
        root = ir.rec_root(self.blocked_id)
        rep_path = root / f"{latest['report']}.json"
        rep_doc = json.loads(rep_path.read_bytes().decode("utf-8"))
        rep_doc["report"]["questions"][0] = "바뀐 첫 번째 질문?"
        rep_path.write_bytes(json.dumps(rep_doc).encode("utf-8"))
        before = self._names("context-*.json")
        with self.assertRaises(IssueRecoveryError) as cm:
            self._record_ctx("낡은 질문에 대한 메모", [0],
                             latest["input"], latest["report"],
                             exp_in=old_in, exp_rep=old_rep)
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(self._names("context-*.json"), before)
        fresh_in = hashlib.sha256(
            (root / f"{latest['input']}.json").read_bytes()).hexdigest()
        fresh_rep = hashlib.sha256(
            rep_path.read_bytes()).hexdigest()
        rec = self._record_ctx("바뀐 질문에 대한 메모", [0],
                               latest["input"], latest["report"],
                               exp_in=fresh_in, exp_rep=fresh_rep)
        doc = self._read_json(f"{rec['context']}.json")
        self.assertEqual(doc["origin"]["questions"],
                         [{"index": 0, "text": "바뀐 첫 번째 질문?"}])

    def test_origin_drift_blocks_persist_and_currentness(self):
        # A schema-valid origin report edit after the context-linked
        # input exists must fail replay, status projection, and
        # pre-transport persistence — never done/current.
        self._done_with_questions()
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        rec = self._record_ctx(
            "운용자 메모", [0], latest["input"], latest["report"],
            exp_in=st["origin_shas"]["input"],
            exp_rep=st["origin_shas"]["report"])
        req_b = self._request_ctx(rec["context"])
        root = ir.rec_root(self.blocked_id)
        stored_b = self._read_json(f"{req_b['input']}.json")
        input_before = (root / f"{req_b['input']}.json").read_bytes()
        rep_path = root / f"{latest['report']}.json"
        rep_doc = json.loads(rep_path.read_bytes().decode("utf-8"))
        rep_doc["report"]["questions"][0] = "전송 중 바뀐 질문?"
        rep_path.write_bytes(json.dumps(rep_doc).encode("utf-8"))
        with self.assertRaises(IssueRecoveryError) as cm:
            ir._validate_stored_identity(self.blocked_id, stored_b)
        self.assertEqual(cm.exception.code, "corrupt")
        with self.assertRaises(IssueRecoveryError):
            ir._projection_contexts(root, self.blocked_id)
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError):
                ir.execute_review(self.blocked_id, req_b["rid"])
            spy.assert_not_called()
        with self.assertRaises(IssueRecoveryError) as cm2:
            ir.read_status(self.blocked_id)
        self.assertEqual(cm2.exception.code, "corrupt")
        self.assertEqual((root / f"{req_b['input']}.json").read_bytes(),
                         input_before)

    def test_malformed_oversize_blank_context_refused(self):
        self._done_with_questions()
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        before = self._names("context-*.json")
        bad_texts = ["", "   ", "x" * (ir._CONTEXT_TEXT_CAP + 1)]
        for text in bad_texts:
            with self.assertRaises(IssueRecoveryError):
                self._record_ctx(text, [0],
                                 latest["input"], latest["report"])
        for refs in (["0"], [-1], [True], "0"):
            with self.assertRaises(IssueRecoveryError):
                self._record_ctx("메모", refs,
                                 latest["input"], latest["report"])
        with self.assertRaises(IssueRecoveryError) as cm:
            self._record_ctx("메모", [0],
                             latest["input"], latest["report"],
                             exp_in="zz", exp_rep="zz")
        self.assertEqual(cm.exception.code, "bad-context")
        with self.assertRaises(IssueRecoveryError):
            self._record_ctx("메모", [0],
                             "input-" + "0" * 16,
                             "report-" + "0" * 16)
        self.assertEqual(self._names("context-*.json"), before)

    def test_tampered_context_record_fails_closed(self):
        # Editing a recorded note without updating its digest must
        # refuse on next use; nothing is healed or rebound.
        self._done_with_questions()
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        rec = self._record_ctx(
            "원본 메모", [0], latest["input"], latest["report"])
        root = ir.rec_root(self.blocked_id)
        rec_path = root / f"{rec['context']}.json"
        doc = json.loads(rec_path.read_bytes().decode("utf-8"))
        doc["text"] = "변조된 메모"
        rec_path.write_bytes(json.dumps(doc).encode("utf-8"))
        before = self._names("input-*.json", "attempt-*.json")
        with self.assertRaises(IssueRecoveryError) as cm:
            self._request_ctx(rec["context"])
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(self._names("input-*.json", "attempt-*.json"),
                         before)

    def test_current_context_survives_latest_move(self):
        # A done -> context recorded on A -> new input B done: the
        # note ACTUALLY consumed by B must still project, even though
        # its origin chain (A) differs from latest (B). A
        # context-free chain projects no current context.
        self._write_blocked_op()
        req_a = ir.request_review(self.blocked_id)
        rep_a = self._report_with_questions(self.cand_head)
        rc = self._exec(req_a["rid"], canned_jsonl(json.dumps(rep_a)))
        self.assertEqual(rc, 0)
        st_a = ir.read_status(self.blocked_id)
        self.assertIsNone(st_a.get("current_context"))
        latest_a = st_a["latest"]
        note = "운용자 메모 MY-CONSUMED-NOTE-k7"
        rec = self._record_ctx(
            note, [0], latest_a["input"], latest_a["report"],
            exp_in=st_a["origin_shas"]["input"],
            exp_rep=st_a["origin_shas"]["report"])
        req_b = self._request_ctx(rec["context"])
        rep_b = self._report_with_questions(self.cand_head)
        rc = self._exec(req_b["rid"], canned_jsonl(json.dumps(rep_b)))
        self.assertEqual(rc, 0)
        st = ir.read_status(self.blocked_id)
        latest_b = st["latest"]
        self.assertNotEqual(latest_b["input"], latest_a["input"])
        cur = st.get("current_context")
        self.assertIsNotNone(cur)
        self.assertEqual(cur["text"], note)
        self.assertEqual(cur["id"], rec["context"])
        self.assertEqual(cur["origin"]["input"], latest_a["input"])
        self.assertEqual(cur["origin"]["report"], latest_a["report"])
        self.assertNotEqual(latest_b["input"], cur["origin"]["input"])
        self.assertEqual(cur["consumed_by"], req_b["fingerprint"])
        self.assertEqual(len(st["contexts"]), 1)
        # The consumed input's own block echoes the same linkage —
        # consumption is derived from the validated input, never
        # from record existence alone.
        stored_b = self._read_json(f"{req_b['input']}.json")
        self.assertEqual(stored_b["context"]["id"], rec["context"])
        doc = self._read_json(f"{rec['context']}.json")
        self.assertEqual(stored_b["context"]["digest"], doc["digest"])
        self.assertEqual(cur["origin"], doc["origin"])

    def test_context_job_failure_preserves_report_null(self):
        # Transport failure on a context-linked input: error status,
        # null report, bounded diagnostics, and the context record
        # itself still verifies afterwards.
        self._done_with_questions()
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        rec = self._record_ctx(
            "운용자 메모", [0], latest["input"], latest["report"])
        req_b = self._request_ctx(rec["context"])
        rc = self._exec(req_b["rid"], "quota exceeded for today",
                        exit_code=1)
        self.assertEqual(rc, 1)
        st2 = ir.read_status(self.blocked_id)
        self.assertIsNone(st2["report"])
        self.assertEqual(st2["latest"]["status"], "error")
        self.assertEqual(st2["latest"]["report"], None)
        doc = self._read_json(f"{rec['context']}.json")
        self.assertEqual(doc["text"], "운용자 메모")
        stored = self._read_json(f"{req_b['input']}.json")
        ir._validate_stored_identity(self.blocked_id, stored)


class BuildEvidenceTests(RecoveryFixtureMixin):
    """Source22 E1: pinned package.json build metadata as NEW immutable
    review input, never reinterpreting legacy bytes.

    The two allowlisted manifests at the three pinned refs are read
    through the real Git object store into a bounded labeled section
    of the exact transmitted prompt; the shared canonical projection
    binds writer and stored replay; the structured declaration link
    binds a non-null contract command to one transmitted entry.
    All fixtures are synthetic local commits in the isolated worktree
    checkouts. Mocked boundaries are the model subscription transport
    everywhere, plus — for the public-route test only — the
    subscription-availability gate probe (`serve.chat.
    subscription_error`), because a fresh offline home/PATH is not a
    real subscription login environment: no real CLI execution,
    login, or network, and source/identity validators are never
    patched. AUTHORED but NOT EXECUTED in this turn.
    """

    PKG = json.dumps({"name": "demo",
                      "scripts": {"build": "webpack --config x.js",
                                  "test": "jest"}})
    EXAMPLE = json.dumps({"name": "example",
                          "scripts": {"build": "expo export",
                                      "start": "expo start"}})

    def _exec(self, rid, raw_jsonl, exit_code=0):
        with mock.patch("openmainty.muse.exec_prompt",
                        return_value=(exit_code, raw_jsonl, {"m": 1})):
            return ir.execute_review(self.blocked_id, rid)

    def _write_manifests(self, cand_manifests=None,
                           base_manifests=None):
        # Commit synthetic manifests in the isolated fixture
        # worktrees, then rebind the owned run/proof descriptors, the
        # self heads/snapshots, and the new blocked op to the moved
        # heads — BEFORE any collection, and before constructing the
        # op. The unchanged production binding validator compares the
        # op candidate against the exact stored run and its fresh
        # review proof, so a manifests-only op rewrite would fail
        # stale before ever exercising build metadata. Negative
        # tamper/drift tests below always mutate only AFTER this
        # valid full baseline (a successful request) is established.
        # Returns (cand_head, base_head).
        if cand_manifests is not None:
            for rel, body in cand_manifests.items():
                target = self.run_work / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(body, bytes):
                    target.write_bytes(body)
                else:
                    target.write_text(body, encoding="utf-8")
            git("add", "-A", cwd=self.run_work)
            git("commit", "-qm", "manifests", cwd=self.run_work)
            self.cand_head = git("rev-parse", "HEAD", cwd=self.run_work)
            self.cand_snap = batches._snapshot(str(self.run_work))
            self.assertIsNotNone(self.cand_snap)
            self.assertEqual(self.cand_snap["head"], self.cand_head)
            self.assertTrue(self.cand_snap["clean"])
            state = store.load(self.run_id)
            state["workCommit"] = self.cand_head
            state["workTreeFingerprint"] = \
                self.cand_snap["fingerprint"]
            state["verifiedCommits"] = {"review": self.cand_head,
                                        "e2e": self.cand_head}
            state["verifiedFingerprints"] = {
                "review": self.cand_snap["fingerprint"],
                "e2e": self.cand_snap["fingerprint"]}
            store.save(state)
        if base_manifests is not None:
            for rel, body in base_manifests.items():
                target = self.base_bar_wt / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(body, bytes):
                    target.write_bytes(body)
                else:
                    target.write_text(body, encoding="utf-8")
            git("add", "-A", cwd=self.base_bar_wt)
            git("commit", "-qm", "manifests", cwd=self.base_bar_wt)
            self.bar_head = git("rev-parse", "HEAD",
                                cwd=self.base_bar_wt)
            self.bar_snap = batches._snapshot(str(self.base_bar_wt))
            self.assertIsNotNone(self.bar_snap)
            self.assertEqual(self.bar_snap["head"], self.bar_head)
            self.assertTrue(self.bar_snap["clean"])
        self._write_blocked_op(cand_head=self.cand_head,
                               base_head=self.bar_head)
        return self.cand_head, self.bar_head

    def _stored(self, name):
        root = ir.rec_root(self.blocked_id)
        return json.loads((root / name).read_bytes().decode("utf-8"))

    def _requested_with_pkg(self):
        self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        return ir.request_review(self.blocked_id)

    def test_absent_manifests_record_absence(self):
        # Default fixtures carry no manifests: all six slots are
        # proven absences with the pinned tree as evidence — never
        # fabricated windows, never a null success.
        self._write_blocked_op()
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        blk = collected["build_evidence"]
        self.assertEqual(blk["version"], 1)
        self.assertEqual(blk["files"], [])
        self.assertEqual(len(blk["omitted"]), 6)
        for entry in blk["omitted"]:
            self.assertTrue(entry.get("absent"))
            self.assertEqual(entry.get("tree"), entry.get("ref"))
            self.assertIn(entry["path"],
                          ("package.json", "example/package.json"))
            self.assertIn(entry["ref_kind"],
                          ("origin", "candidate", "base"))
        self.assertEqual(collected["counts"]["build_files"], 0)
        self.assertEqual(collected["counts"]["build_omitted"], 6)
        built = ir.build_prompt(ctx, collected)
        self.assertIn("빌드 근거", built["prompt"])
        self.assertIn("트리에 없음", built["prompt"])
        self.assertIn("build_evidence", built["snapshot"])
        proj = ir._project_build_meta(blk)
        self.assertEqual(proj["files"], [])
        self.assertEqual(len(proj["omitted"]), 6)

    def test_present_manifests_transmit_scripts(self):
        # Real commits in both checkouts: transmitted scripts, raw
        # digests, and byte sizes bind the pinned bytes; the prompt
        # carries the exact declaration text.
        pkg_bytes = self.PKG.encode("utf-8")
        ex_bytes = self.EXAMPLE.encode("utf-8")
        self._write_manifests(
            cand_manifests={"package.json": self.PKG},
            base_manifests={"example/package.json": self.EXAMPLE})
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        blk = collected["build_evidence"]
        self.assertEqual(
            sorted((e["path"], e["ref_kind"]) for e in blk["files"]),
            [("example/package.json", "base"),
             ("package.json", "candidate")])
        by_path = {e["path"]: e for e in blk["files"]}
        self.assertEqual(
            by_path["package.json"]["raw_sha256"],
            hashlib.sha256(pkg_bytes).hexdigest())
        self.assertEqual(
            by_path["example/package.json"]["raw_sha256"],
            hashlib.sha256(ex_bytes).hexdigest())
        self.assertEqual(by_path["package.json"]["scripts"],
                         {"build": "webpack --config x.js",
                          "test": "jest"})
        self.assertEqual(by_path["example/package.json"]["scripts"],
                         {"build": "expo export", "start": "expo start"})
        for entry in blk["files"]:
            self.assertEqual(entry["omitted_scripts"], [])
            self.assertFalse(entry["scripts_truncated"])
            self.assertIn("raw_sha256: " + entry["raw_sha256"],
                          entry["text"])
        built = ir.build_prompt(ctx, collected)
        self.assertIn("build = webpack --config x.js", built["prompt"])
        self.assertIn('cwd ""', built["prompt"])
        self.assertIn('cwd "example"', built["prompt"])
        self.assertIn(
            "raw_sha256: " + by_path["package.json"]["raw_sha256"],
            built["prompt"])

    def test_prompt_section_equals_canonical_render(self):
        # The transmitted build section is exactly the shared
        # canonical render (global header + entry texts + omission
        # lines) — no divergent inline copy in the prompt builder.
        # Legacy inputs without the evidence key keep a prompt with
        # no build section at all.
        self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        blk = collected["build_evidence"]
        built = ir.build_prompt(ctx, collected)
        section = ir._render_build_section(
            [{"text": e["text"]} for e in blk["files"]], blk["omitted"])
        self.assertIn(section, built["prompt"])
        self.assertIn("그대로 복사하라", built["prompt"])
        legacy = json.loads(json.dumps(collected))
        del legacy["build_evidence"]
        legacy_built = ir.build_prompt(ctx, legacy)
        self.assertNotIn(section, legacy_built["prompt"])
        self.assertNotIn("그대로 복사하라", legacy_built["prompt"])
        self.assertNotIn("build_evidence", legacy_built["snapshot"])

    def test_oversize_manifest_omitted_without_raw_sha(self):
        # A blob over the raw cap keeps size/blob identity with the
        # exact byte-unit reason, but no computed raw SHA and no
        # transmitted text.
        big = "x" * (ir._BUILD_RAW_CAP + 1024)
        doc = json.dumps({"name": "big", "scripts": {"b": big}})
        self._write_manifests(cand_manifests={"package.json": doc})
        ctx = ir.load_blocked_op(self.blocked_id)
        blk = ir.collect_windows(ctx)["build_evidence"]
        self.assertEqual(blk["files"], [])
        over = [o for o in blk["omitted"]
                if o.get("blob_oid") and not o.get("absent")]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0]["path"], "package.json")
        self.assertEqual(over[0]["ref_kind"], "candidate")
        self.assertNotIn("raw_sha256", over[0])
        self.assertGreater(over[0]["size"], ir._BUILD_RAW_CAP)
        self.assertIn("바이트", over[0]["reason"])
        self.assertNotIn("선언 사실 없음", over[0]["reason"])

    def test_invalid_manifests_are_corrupt(self):
        # Present-but-unreadable manifests fail closed: binary,
        # non-JSON, wrong shapes, and malformed scripts are corrupt —
        # never silent successful absence or omission.
        shapes = {
            "bad-json": "{not json",
            "binary": b'{"scripts": {"a": "\x00"}}',
            "list-top": "[1, 2]",
            "scripts-nondict": '{"scripts": ["x"]}',
            "cmd-nonstring": '{"scripts": {"a": 3}}',
            "empty-name": '{"scripts": {"": "x"}}',
        }
        for label, body in shapes.items():
            with self.subTest(shape=label):
                self._write_manifests(
                    cand_manifests={"package.json": body})
                ctx = ir.load_blocked_op(self.blocked_id)
                with self.assertRaises(IssueRecoveryError) as cm:
                    ir.collect_windows(ctx)
                self.assertEqual(cm.exception.code, "corrupt")

    def test_too_many_scripts_omits_whole_file(self):
        # More scripts than the cap omits the whole file with the
        # exact count; no script is transmitted, no raw SHA is lost
        # (the blob identity stays), and the reason never claims a
        # proven absence of declarations.
        scripts = {f"s{i:02d}": "echo ok" for i in range(
            ir._BUILD_SCRIPTS_CAP + 1)}
        doc = json.dumps({"name": "many", "scripts": scripts})
        self._write_manifests(cand_manifests={"package.json": doc})
        ctx = ir.load_blocked_op(self.blocked_id)
        blk = ir.collect_windows(ctx)["build_evidence"]
        self.assertEqual(blk["files"], [])
        whole = [o for o in blk["omitted"]
                 if o.get("blob_oid") and not o.get("absent")]
        self.assertEqual(len(whole), 1)
        self.assertIn("스크립트 수 초과", whole[0]["reason"])
        self.assertNotIn("선언 사실 없음", whole[0]["reason"])

    def test_long_commands_omit_per_script_with_taxonomy_kind(self):
        # Over-long commands omit only their own script with a
        # taxonomy kind and the exact stored reason; the short
        # script is still transmitted.
        scripts = {"ok": "jest", "big": "x" * 2001}
        doc = json.dumps({"name": "cut", "scripts": scripts})
        self._write_manifests(cand_manifests={"package.json": doc})
        ctx = ir.load_blocked_op(self.blocked_id)
        blk = ir.collect_windows(ctx)["build_evidence"]
        self.assertEqual(len(blk["files"]), 1)
        entry = blk["files"][0]
        self.assertEqual(entry["scripts"], {"ok": "jest"})
        self.assertTrue(entry["scripts_truncated"])
        self.assertEqual(len(entry["omitted_scripts"]), 1)
        cut = entry["omitted_scripts"][0]
        self.assertEqual(cut["name"], "big")
        self.assertEqual(cut["kind"], "long-command")
        self.assertIn("2001", cut["reason"])
        self.assertIn("ok = jest", entry["text"])
        self.assertNotIn("x" * 2001, entry["text"])
        self.assertIn("긴 명령어 제외", entry["text"])

    def test_worst_case_varying_lengths_fit_budget_with_taxonomy(self):
        # Root's adversarial case: 14 scripts with commands of
        # lengths 2001..2014 in both manifests at both worktree
        # refs. Exact lengths differ per script, but the transmitted
        # section groups by stable taxonomy kind — no unique
        # rendered reason line — and the global byte cap holds.
        scripts = {f"s{i:02d}": "x" * (2001 + i) for i in range(14)}
        doc = json.dumps({"name": "adversarial", "scripts": scripts})
        self._write_manifests(
            cand_manifests={"package.json": doc,
                            "example/package.json": doc},
            base_manifests={"package.json": doc,
                            "example/package.json": doc})
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        blk = collected["build_evidence"]
        self.assertEqual(len(blk["files"]), 4)
        for entry in blk["files"]:
            self.assertEqual(entry["scripts"], {})
            self.assertEqual(len(entry["omitted_scripts"]), 14)
            for cut in entry["omitted_scripts"]:
                self.assertEqual(cut["kind"], "long-command")
        section = ir._render_build_section(
            [{"text": e["text"]} for e in blk["files"]], blk["omitted"])
        self.assertLessEqual(len(section.encode("utf-8")),
                             ir._BUILD_SECTION_CAP)
        self.assertEqual(section.count("14건: 긴 명령어 제외"), 4)
        for i in range(14):
            self.assertNotIn(f"길이 초과({2001 + i}자)", section)
        # Exact per-script reasons stay bound in stored metadata.
        reasons = [c["reason"] for e in blk["files"]
                   for c in e["omitted_scripts"]]
        self.assertEqual(len(reasons), 56)
        self.assertIn("길이 초과(2001자)", reasons[0])
        self.assertIn("길이 초과(2014자)", reasons[-1])

    def test_multibyte_budget_is_byte_exact(self):
        # Multi-byte commands near the cap: the global section is
        # budgeted in UTF-8 bytes (never chars), over-budget scripts
        # drop with the budget taxonomy kind, and stored byte counts
        # match the exact encoded text.
        scripts = {"a": "한" * 1900, "b": "한" * 1900, "c": "echo ok"}
        doc = json.dumps({"name": "wide", "scripts": scripts})
        self._write_manifests(cand_manifests={"package.json": doc})
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        blk = collected["build_evidence"]
        section = ir._render_build_section(
            [{"text": e["text"]} for e in blk["files"]], blk["omitted"])
        self.assertLessEqual(len(section.encode("utf-8")),
                             ir._BUILD_SECTION_CAP)
        kinds = [c["kind"] for e in blk["files"]
                 for c in e["omitted_scripts"]]
        self.assertIn("budget", kinds)
        for entry in blk["files"]:
            self.assertEqual(entry["sent_bytes"],
                             len(entry["text"].encode("utf-8")))
        req = ir.request_review(self.blocked_id)
        stored = self._stored(f"{req['input']}.json")
        ir._validate_stored_identity(self.blocked_id, stored)

    def test_legacy_snapshot_without_block_replays_exact(self):
        # A stored input without the build_evidence key replays the
        # exact prior meta (no added key): the missing-block path is
        # legacy, while a present block must validate.
        self._write_blocked_op()
        req = ir.request_review(self.blocked_id)
        stored = self._stored(f"{req['input']}.json")
        legacy = json.loads(json.dumps(stored))
        del legacy["snapshot"]["build_evidence"]
        _sent, _scope, _psha, meta = ir._stored_sent_scope_meta(legacy)
        self.assertEqual(set(meta.keys()), {"files", "omitted", "refs"})
        self.assertNotIn("build_evidence", meta)
        _sent2, _scope2, _psha2, meta2 = ir._stored_sent_scope_meta(
            stored)
        self.assertIn("build_evidence", meta2)
        self.assertEqual(meta2["build_evidence"]["version"], 1)

    def test_evidence_change_yields_new_fingerprint(self):
        # Same source with new build evidence is a NEW immutable
        # input: the fingerprint must change, both inputs stay
        # stored, and both still validate. The evidence commit moves
        # the fixture head through the coherent run/op rebind, so the
        # original head is captured before the move.
        self._write_blocked_op()
        old_cand = self.cand_head
        req_a = ir.request_review(self.blocked_id)
        rc = self._exec(req_a["rid"], canned_jsonl(
            json.dumps(canned_report(old_cand))))
        self.assertEqual(rc, 0)
        new_cand, _new_base = self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        req_b = ir.request_review(self.blocked_id)
        self.assertNotEqual(req_a["fingerprint"], req_b["fingerprint"])
        self.assertNotEqual(req_a["input"], req_b["input"])
        self.assertNotEqual(new_cand, old_cand)
        self.assertEqual(new_cand, self.cand_head)
        ir._validate_stored_identity(
            self.blocked_id, self._stored(f"{req_a['input']}.json"))
        ir._validate_stored_identity(
            self.blocked_id, self._stored(f"{req_b['input']}.json"))

    def _tampered_request(self, mutate):
        req = self._requested_with_pkg()
        before = self._op_bytes()
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        doc = json.loads(inp_path.read_bytes().decode("utf-8"))
        mutate(doc)
        inp_path.write_bytes(json.dumps(doc).encode("utf-8"))
        tampered = inp_path.read_bytes()
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.execute_review(self.blocked_id, req["rid"])
            spy.assert_not_called()
        self.assertEqual(cm.exception.code, "corrupt")
        # Tampered bytes are preserved for inspection, never healed.
        self.assertEqual(inp_path.read_bytes(), tampered)
        self.assertEqual(self._op_bytes(), before)

    def test_build_scripts_map_tamper_refuses(self):
        # Transmitted script text changed without a new collection:
        # stored text no longer matches its metadata.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["scripts"]["build"] = "tampered --evil"
        self._tampered_request(mutate)

    def test_build_text_tamper_refuses(self):
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["text"] += "\n# tampered"
        self._tampered_request(mutate)

    # Each adversarial variant below gets an independent fresh owned
    # fixture/input baseline (one _tampered_request per test method):
    # reusing one fixture across mutations would leave a corrupt
    # stored input and a queued attempt behind, and recommitting an
    # identical manifest has no changes — later variants would then
    # fail at setup instead of at their intended mutation. No prior
    # tampered record is healed; the real validator is never weakened.
    def test_build_ref_tamper_refuses(self):
        # Ref is bound: any drift breaks replay equality.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["ref"] = "d" * 40
        self._tampered_request(mutate)

    def test_build_raw_digest_tamper_refuses(self):
        # The raw digest is embedded in the canonical text: drift
        # breaks text equality as well as the fingerprint.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["raw_sha256"] = "e" * 64
        self._tampered_request(mutate)

    def test_build_raw_len_tamper_refuses(self):
        # Byte size is bound in the header line: drift breaks text
        # equality.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["raw_len"] += 1
        self._tampered_request(mutate)

    def test_build_sent_bytes_tamper_refuses(self):
        # Stored byte count must match the exact encoded text.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["sent_bytes"] += 1
        self._tampered_request(mutate)

    def test_build_version_tamper_refuses(self):
        # Unknown versions refuse — never migrate or heal.
        def mutate(doc):
            doc["snapshot"]["build_evidence"]["version"] = 2
        self._tampered_request(mutate)

    def test_build_present_null_refuses(self):
        # A present null block is not a legacy missing key: it
        # refuses as corrupt, never bypasses as legacy.
        def mutate(doc):
            doc["snapshot"]["build_evidence"] = None
        self._tampered_request(mutate)

    def test_build_extra_key_tamper_refuses(self):
        # Exact key sets: unknown fields refuse.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["extra"] = "nope"
        self._tampered_request(mutate)

    def test_build_truncation_flag_tamper_refuses(self):
        # The flag must agree with the omission list.
        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            files[0]["scripts_truncated"] = True
        self._tampered_request(mutate)

    def test_build_omission_kind_tamper_refuses(self):
        # Omission taxonomy kinds are bound too: inventing a kind
        # that never renders breaks canonical text validation.
        scripts = {"ok": "jest", "big": "x" * 2001}
        doc = json.dumps({"name": "cut", "scripts": scripts})
        self._write_manifests(cand_manifests={"package.json": doc})
        req = ir.request_review(self.blocked_id)

        def mutate(doc):
            files = doc["snapshot"]["build_evidence"]["files"]
            cut = files[0]["omitted_scripts"][0]
            cut["kind"] = "invented-kind"
        before = self._op_bytes()
        root = ir.rec_root(self.blocked_id)
        inp_path = root / f"{req['input']}.json"
        stored = json.loads(inp_path.read_bytes().decode("utf-8"))
        mutate(stored)
        inp_path.write_bytes(json.dumps(stored).encode("utf-8"))
        with mock.patch("openmainty.muse.exec_prompt") as spy:
            with self.assertRaises(IssueRecoveryError) as cm:
                ir.execute_review(self.blocked_id, req["rid"])
            spy.assert_not_called()
        self.assertEqual(cm.exception.code, "corrupt")
        self.assertEqual(self._op_bytes(), before)

    def test_candidate_checkout_drift_during_model_refuses_done(self):
        # Genuine mid-model source move: the pinned candidate head
        # still resolves, but the live checkout no longer matches the
        # recorded evidence. The valid-looking report must not
        # persist as done; the stored input bytes stay preserved.
        new_cand, _new_base = self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        req = ir.request_review(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        input_before = (root / f"{req['input']}.json").read_bytes()
        good = canned_report(new_cand)
        payload = canned_jsonl(json.dumps(good))

        def _run(*args, **kwargs):
            (self.run_work / "package.json").write_text(
                json.dumps({"name": "demo",
                            "scripts": {"build": "changed"}}),
                encoding="utf-8")
            return (0, payload, {"m": 1})

        with mock.patch("openmainty.muse.exec_prompt",
                        side_effect=_run):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 1)
        st = ir.read_status(self.blocked_id)
        self.assertIsNone(st["report"])
        self.assertFalse(st["current"])
        self.assertEqual(st["latest"]["status"], "stale")
        self.assertIsNone(st["latest"]["report"])
        self.assertEqual((root / f"{req['input']}.json").read_bytes(),
                         input_before)

    def test_context_input_keeps_evidence_and_reports_current(self):
        # A context continuation on an evidence-bearing input keeps
        # the build block, links the context, and stays current with
        # the full transmitted scripts visible in status.
        new_cand, _new_base = self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        req_a = ir.request_review(self.blocked_id)
        rep_a = canned_report(new_cand)
        rep_a["questions"] = ["빌드 명령이 무엇인가?"]
        self.assertEqual(self._exec(
            req_a["rid"], canned_jsonl(json.dumps(rep_a))), 0)
        st = ir.read_status(self.blocked_id)
        latest = st["latest"]
        with m2.op_locked(self.blocked_id):
            rec = ir.record_recovery_context(
                self.blocked_id, "운용자 메모", question_refs=[0],
                origin_input=latest["input"],
                origin_report=latest["report"])
            req_b = ir.request_review(self.blocked_id,
                                      context_id=rec["context"])
        rep_b = canned_report(new_cand)
        self.assertEqual(self._exec(
            req_b["rid"], canned_jsonl(json.dumps(rep_b))), 0)
        stored_b = self._stored(f"{req_b['input']}.json")
        self.assertEqual(stored_b["context"]["id"], rec["context"])
        self.assertEqual(
            stored_b["snapshot"]["build_evidence"]["version"], 1)
        st2 = ir.read_status(self.blocked_id)
        self.assertTrue(st2["current"])
        self.assertIsNotNone(st2.get("current_context"))
        self.assertEqual(st2["current_context"]["id"], rec["context"])
        self.assertTrue(st2["input_build"])
        scripts = {}
        for entry in st2["input_build"]:
            scripts.update(entry.get("scripts") or {})
        self.assertEqual(scripts.get("build"), "webpack --config x.js")

    def _evidence_block(self):
        self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        entry = next(e for e in collected["build_evidence"]["files"]
                     if e["path"] == "package.json"
                     and e["ref_kind"] == "candidate")
        return collected, entry

    def _decl(self, entry, script):
        return {"manifest": entry["path"],
                "ref_kind": entry["ref_kind"], "ref": entry["ref"],
                "script": script,
                "cwd": ir._BUILD_CWD[entry["path"]],
                "raw_sha256": entry["raw_sha256"]}

    def _contract_report(self, head, contract):
        rep = canned_report(head)
        rep["build_contract"] = contract
        return rep

    def test_contract_runner_invocation_accepts(self):
        # "npm run <script>" names the declared script exactly: valid
        # sound trace, still an unexecuted proposal.
        collected, entry = self._evidence_block()
        head = collected["refs"]["candidate"]
        rep = self._contract_report(head, {
            "builder": "npm", "command": "npm run build",
            "outputs": ["dist"],
            "declaration": self._decl(entry, "build")})
        out = ir.validate_report(rep,
                                 [w for w in collected["windows"]],
                                 collected["omitted"],
                                 collected["build_evidence"])
        self.assertEqual(out, rep)

    def test_contract_exact_raw_text_accepts(self):
        # The exact declared raw script text is authoritative
        # regardless of runner: no invocation syntax is assumed.
        collected, entry = self._evidence_block()
        head = collected["refs"]["candidate"]
        rep = self._contract_report(head, {
            "command": entry["scripts"]["build"],
            "declaration": self._decl(entry, "build")})
        out = ir.validate_report(rep,
                                 [w for w in collected["windows"]],
                                 collected["omitted"],
                                 collected["build_evidence"])
        self.assertEqual(out, rep)

    def test_contract_unsound_invocations_refused(self):
        # Bare `npm <script>`, native aliases, flags, extra args,
        # npx/pnpm, cd chains, wrong scripts, missing declarations,
        # and mismatched provenance are all refused — never
        # silently corrected.
        collected, entry = self._evidence_block()
        head = collected["refs"]["candidate"]
        sent = [w for w in collected["windows"]]
        blk = collected["build_evidence"]
        good_decl = self._decl(entry, "build")
        bad_decl = dict(good_decl)
        bad_decl["ref"] = "f" * 40
        cases = {
            "bare-npm": ("npm build", good_decl),
            "bare-yarn": ("yarn build", good_decl),
            "with-flags": ("npm run build -- --mode prod", good_decl),
            "npx": ("npx webpack", good_decl),
            "cd-chain": ("cd sub && npm run build", good_decl),
            "wrong-script": ("npm run test", good_decl),
            "missing-declaration": ("npm run build", None),
            "wrong-ref": ("npm run build", bad_decl),
            "invented-script": (
                "npm run deploy",
                dict(good_decl, script="deploy")),
        }
        for label, (command, decl) in cases.items():
            with self.subTest(case=label):
                contract = {"builder": "npm", "command": command,
                            "outputs": ["dist"]}
                if decl is not None:
                    contract["declaration"] = decl
                rep = self._contract_report(head, contract)
                with self.assertRaises(IssueRecoveryError) as cm:
                    ir.validate_report(rep, sent, collected["omitted"],
                                       blk)
                self.assertEqual(cm.exception.code, "invalid-report")

    def test_contract_declaration_provenance_refused(self):
        # Same script name at a different manifest/ref, a stale raw
        # digest, or a wrong working directory never stands in for
        # the selected transmitted source.
        collected, entry = self._evidence_block()
        head = collected["refs"]["candidate"]
        sent = [w for w in collected["windows"]]
        blk = collected["build_evidence"]
        base = self._decl(entry, "build")
        variants = {
            "wrong-manifest": dict(base,
                                  manifest="example/package.json"),
            "wrong-kind": dict(base, ref_kind="base"),
            "wrong-raw": dict(base, raw_sha256="0" * 64),
            "wrong-cwd": dict(base, cwd="elsewhere"),
            "undeclared-script": dict(base, script="nope"),
            "malformed-decl": "not-an-object",
        }
        for label, decl in variants.items():
            with self.subTest(case=label):
                rep = self._contract_report(head, {
                    "command": "npm run build", "declaration": decl})
                with self.assertRaises(IssueRecoveryError) as cm:
                    ir.validate_report(rep, sent, collected["omitted"],
                                       blk)
                self.assertEqual(cm.exception.code, "invalid-report")

    def test_contract_empty_and_commandless_refused(self):
        # An empty object or a builder/outputs object without a
        # valid command is never accepted as new proof; an absent or
        # null contract stays the honest "no contract" form, and a
        # minimal command+declaration needs no invented builder.
        collected, entry = self._evidence_block()
        head = collected["refs"]["candidate"]
        sent = [w for w in collected["windows"]]
        blk = collected["build_evidence"]
        decl = self._decl(entry, "build")
        bad_contracts = [
            {},
            {"builder": "npm", "outputs": ["dist"]},
            {"builder": "npm", "command": None,
             "declaration": decl},
            {"command": "", "declaration": decl},
            {"command": "   ", "declaration": decl},
            {"command": 123, "declaration": decl},
            {"command": "npm run build", "declaration": decl,
             "builder": ""},
            {"command": "npm run build", "declaration": decl,
             "outputs": ["ok", 123]},
            {"command": "x" * (ir._BUILD_CMD_CAP + 1),
             "declaration": decl},
        ]
        for contract in bad_contracts:
            with self.subTest(contract=str(contract)[:60]):
                rep = self._contract_report(head, contract)
                with self.assertRaises(IssueRecoveryError) as cm:
                    ir.validate_report(rep, sent, collected["omitted"],
                                       blk)
                self.assertEqual(cm.exception.code, "invalid-report")
        minimal = self._contract_report(
            head, {"command": "npm run build", "declaration": decl})
        self.assertEqual(ir.validate_report(
            minimal, sent, collected["omitted"], blk), minimal)
        null_contract = self._contract_report(head, None)
        self.assertEqual(ir.validate_report(
            null_contract, sent, collected["omitted"], blk),
            null_contract)

    def test_contract_legacy_without_block_keeps_prior(self):
        # Legacy inputs without the evidence block keep the exact
        # prior validator: a command without a declaration is not
        # re-judged by the new link rule.
        self._write_blocked_op()
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        head = collected["refs"]["candidate"]
        rep = self._contract_report(head, {
            "builder": "npm", "command": "npm run build",
            "outputs": ["dist"]})
        out = ir.validate_report(rep,
                                 [w for w in collected["windows"]],
                                 collected["omitted"], None)
        self.assertEqual(out, rep)

    def test_contract_leading_dash_selector_refused(self):
        # A script literally named "-h" is recorded as data, but
        # `npm run -h` is an option/help flag to the runner, never a
        # script invocation. The exact raw text stays valid as a
        # source-declaration trace only.
        scripts = {"-h": "node real.js", "build": "webpack"}
        doc = json.dumps({"name": "dash", "scripts": scripts})
        self._write_manifests(cand_manifests={"package.json": doc})
        ctx = ir.load_blocked_op(self.blocked_id)
        collected = ir.collect_windows(ctx)
        entry = next(e for e in collected["build_evidence"]["files"]
                     if e["path"] == "package.json"
                     and e["ref_kind"] == "candidate")
        head = collected["refs"]["candidate"]
        sent = [w for w in collected["windows"]]
        blk = collected["build_evidence"]
        rep = self._contract_report(head, {
            "command": "npm run -h",
            "declaration": self._decl(entry, "-h")})
        with self.assertRaises(IssueRecoveryError) as cm:
            ir.validate_report(rep, sent, collected["omitted"], blk)
        self.assertEqual(cm.exception.code, "invalid-report")
        raw = self._contract_report(head, {
            "command": "node real.js",
            "declaration": self._decl(entry, "-h")})
        self.assertEqual(ir.validate_report(
            raw, sent, collected["omitted"], blk), raw)

    def test_prompt_carries_declaration_fields_to_model(self):
        # The eligible entry raw_sha256 and the allowlisted cwd for
        # BOTH manifests must be present in the EXACT prompt bytes
        # the mocked transport receives — the model cannot derive
        # them from blob SHAs or script subsets. The prompt must
        # equal the stored sent_text byte for byte.
        self._write_manifests(
            cand_manifests={"package.json": self.PKG},
            base_manifests={"example/package.json": self.EXAMPLE})
        req = ir.request_review(self.blocked_id)
        ctx = ir.load_blocked_op(self.blocked_id)
        head = ctx["candidate"]["head"]
        good = canned_report(head)
        seen = {}

        def _run(prompt, **kwargs):
            seen["prompt"] = prompt
            return (0, canned_jsonl(json.dumps(good)), {"m": 1})

        with mock.patch("openmainty.muse.exec_prompt",
                        side_effect=_run):
            rc = ir.execute_review(self.blocked_id, req["rid"])
        self.assertEqual(rc, 0)
        stored = self._stored(f"{req['input']}.json")
        self.assertEqual(seen["prompt"],
                         stored["snapshot"]["sent_text"])
        blk = stored["snapshot"]["build_evidence"]
        self.assertTrue(blk["files"])
        for entry in blk["files"]:
            self.assertIn("raw_sha256: " + entry["raw_sha256"],
                          seen["prompt"])
        self.assertIn('cwd ""', seen["prompt"])
        self.assertIn('cwd "example"', seen["prompt"])

    def tearDown(self):
        for jid, job in list(serve._JOBS.items()):
            if isinstance(job, dict) and job.get("kind") \
                    == "recovery-review":
                serve._JOBS.pop(jid, None)
                serve._JOB_CANCEL.pop(jid, None)

    def _drain(self, jid, timeout=30.0):
        end = time.time() + timeout
        while time.time() < end:
            job = serve._JOBS.get(jid)
            if job is not None and job.get("status") != "running":
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {jid} still running")

    def test_route_post_context_starts_job_and_stores_valid_report(self):
        # Fully-shaped public route with valid origin revision: the
        # worker job runs the mocked transport to a stored valid
        # report on the evidence-bearing input, and status shows the
        # full transmitted build disclosure. Mocked boundaries are the
        # model transport AND the subscription-availability gate probe
        # (fresh offline home/PATH is not a real subscription login
        # environment): no real CLI execution, login, or network, and
        # source/identity validators are never patched. The fixture
        # commits only candidate package.json: exactly 1 transmitted
        # file and 5 omitted slots.
        new_cand, _new_base = self._write_manifests(
            cand_manifests={"package.json": self.PKG})
        req_a = ir.request_review(self.blocked_id)
        rep_a = canned_report(new_cand)
        rep_a["questions"] = ["빌드 명령이 무엇인가?"]
        self.assertEqual(self._exec(
            req_a["rid"], canned_jsonl(json.dumps(rep_a))), 0)
        st = ir.read_status(self.blocked_id)
        latest, shas = st["latest"], st["origin_shas"]
        rep_b = canned_report(new_cand)
        gate = mock.patch.object(
            serve.chat, "subscription_error", return_value=None)
        gate.start()
        self.addCleanup(gate.stop)
        patcher = mock.patch(
            "openmainty.muse.exec_prompt",
            return_value=(0, canned_jsonl(json.dumps(rep_b)),
                          {"m": 1}))
        patcher.start()
        self.addCleanup(patcher.stop)
        out = serve.api_integration_recovery_review(
            {"op": self.blocked_id, "confirm": True,
             "context": {"text": "운용자 메모", "question_refs": [0],
                         "input": latest["input"],
                         "report": latest["report"],
                         "expected": {
                             "input_sha256": shas["input"],
                             "report_sha256": shas["report"]}}})
        self.assertTrue(out.get("started"))
        self.assertIn("job", out)
        self.assertTrue(out.get("context", {}).get("id"))
        job = self._drain(out["job"]["id"])
        self.assertEqual(job.get("status"), "done")
        got = serve.api_integration_recovery({"op": self.blocked_id})
        self.assertTrue(got["current"])
        self.assertTrue(got["input_build"])
        by_key = {(f.get("path"), f.get("ref_kind")): f
                  for f in got["input_build"]}
        entry = by_key[("package.json", "candidate")]
        self.assertEqual(entry["scripts"]["build"],
                         "webpack --config x.js")
        self.assertTrue(entry["raw_sha256"])
        self.assertEqual(len(got["input_build_omitted"]), 5)
