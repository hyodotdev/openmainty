"""Offline regressions for the app-owned derived source repair (Phase A).

Uses synthetic local fixtures only (temp owned home + synthetic git
repos); the ONLY stubbed boundaries are the model transports (the
recovery review path keeps ``openmainty.muse.exec_prompt``; every
derived worker/start path uses the dedicated
``openmainty.muse.exec_source_repair`` with admission_kind="target"
through the real fixture preflight gate),
the subscription-availability gate probe (fresh offline home/PATH is
not a real subscription login environment), and fixture identity
facts for the preflight gate (``OPENMAINTY_PREFLIGHT_FIXTURE_CURRENT``
values plus ``OPENMAINTY_PREFLIGHT_FIXTURE_TRUST`` — same category as
the fixture home: only the VALUES are fixture-supplied, every
validator, gate, job-table, and lease path runs for real). A few
regressions additionally inject narrow one-shot stdlib I/O faults at
the persistence/read boundary (scoped ``os.replace``/``os.fstat``
failures, planted files, FIFOs, moved-aside directories) —
environment fault-injection, not a product mock: FIFO probes join a
bounded owned child so a future blocking open fails instead of
hanging. Source/identity validators are never patched and no real
model/login calls happen. The HTTP envelope regressions serve exact
product handler bytes over an owned 127.0.0.1 loopback server
(ephemeral port, fresh fixture, joined server thread) — local
transport events, never external network — and target repos outside
the temp home are never touched. Tests are AUTHORED, not executed,
in the authoring gate; Root runs them fenced.
"""
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import batches, derived_fix as df, issue_recovery as ir
from openmainty import m2, muse, preflight as pf, sched, serve, store
from test_integration_prepare_issue import IssueFixtureMixin, git
from test_issue_recovery import RecoveryFixtureMixin, canned_jsonl


WEBPACK_V0 = ("// webpack base v0\nmodule.exports = {\n"
             "  // candidate region: entry\n"
             "  entry: \"./src/index.js\",\n"
             "  // base region: output path\n"
             "  output: {path: \"dist\"},\n};\n")
WEBPACK_CAND = WEBPACK_V0.replace(
    "  entry: \"./src/index.js\",",
    "  entry: \"./src/app.js\", // candidate change")
WEBPACK_SENTINEL = "// sentinel-base-evo-1\n"
TEST_V0 = ("// webpack web test v0\nconst a = 1;\nconst b = 2;\n"
            "const c = 3;\nconst d = 4;\n")
TEST_BASE_LINE = "// base-only test line\n"
TEST_CAND_LINE = "// candidate test change\n"
DIST_V0 = "// dist bundle v0\n"
DIST_BASE = DIST_V0 + "// dist base evolution\n"

OP_ERROR = ("cherry-pick conflicted or failed: error: could not apply CAND\n"
            "re-derive refused: candidate touches "
            "example/dist/bundle.c.js besides manifests/locks")


def _proposal(changed, hashes, resolutions=None, ins=1, dels=1):
    # ins/dels default to the TRUE single-line-replace counts of the
    # happy fixtures (one line changed in one file); negative tests
    # that fail before the diff-stat gate keep schema-valid values.
    return {"status": "proposal", "changed_paths": list(changed),
            "per_file_sha256": dict(hashes),
            "diff_stat": {"files": len(changed), "insertions": ins,
                          "deletions": dels},
            "resolutions": list(resolutions or []),
            "notes": "operator hypothesis applied"}


def _worker_jsonl(proposal):
    return canned_jsonl(json.dumps(proposal))


class DerivedFixFixtureMixin(IssueFixtureMixin):
    """Seeded source history with REAL linked-worktree topology.

    B0 (run baseCommit) carries the Phase A sources; B1 (op base
    head) evolves them with a base-only sentinel; C2/C3 (candidate
    heads) introduce candidate changes. Candidate and base checkouts
    are DISTINCT linked worktrees of the same repo (same
    --git-common-dir, different toplevels) — the actual Source22/23
    layout a toplevel comparison would wrongly reject.
    """
    blocked_id = "prep-issue-856-d1"

    def setUp(self):
        super().setUp()
        git("checkout", "-qb", "fixture/src-base", self.base_head,
            cwd=self.origin)
        self._write_file("example/webpack.config.js", WEBPACK_V0)
        self._write_file("src/__tests__/webpack-web.test.ts", TEST_V0)
        self._write_file("example/dist/bundle.c.js", DIST_V0)
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "fixture src base", cwd=self.origin)
        self.base_commit = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-qb", "fixture/src-base-evo", self.base_commit,
            cwd=self.origin)
        self._write_file("example/webpack.config.js",
                         WEBPACK_V0 + WEBPACK_SENTINEL)
        # Base evolution inserts mid-file (after `const a`), while
        # the candidate appends at EOF: disjoint hunks with three
        # unchanged context lines between them, so the ORIGINAL
        # base_commit->candidate patch applies cleanly onto the
        # evolved base and the base-only sentinel survives. A
        # same-spot append (overlapping hunk) is the separate
        # conflict negative, not this positive path.
        self._write_file("src/__tests__/webpack-web.test.ts",
                         TEST_V0.replace(
                             "const a = 1;\n",
                             "const a = 1;\n" + TEST_BASE_LINE, 1))
        self._write_file("example/dist/bundle.c.js", DIST_BASE)
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "fixture base evolution", cwd=self.origin)
        self.base_evo = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-qb", "fixture/src-cand", self.base_commit,
            cwd=self.origin)
        self._write_file("example/webpack.config.js", WEBPACK_CAND)
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "fixture candidate", cwd=self.origin)
        self.cand_src = git("rev-parse", "HEAD", cwd=self.origin)
        git("checkout", "-qb", "fixture/src-cand-test", self.cand_src,
            cwd=self.origin)
        self._write_file("src/__tests__/webpack-web.test.ts",
                         TEST_V0 + TEST_CAND_LINE)
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "fixture candidate test", cwd=self.origin)
        self.cand_src_test = git("rev-parse", "HEAD", cwd=self.origin)
        # C4: child of the BASE evolution, test-only change. Its diff
        # against base head no longer contains the recorded webpack
        # conflict — the scope-drift regression target.
        git("checkout", "-qb", "fixture/src-cand-revert", self.base_evo,
            cwd=self.origin)
        self._write_file("src/__tests__/webpack-web.test.ts",
                         TEST_V0 + TEST_BASE_LINE
                         + "// candidate test only\n")
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "fixture candidate test only",
            cwd=self.origin)
        self.cand_revert = git("rev-parse", "HEAD", cwd=self.origin)
        self.cand_revert_wt = self.root / "home" / "cand-revert-wt"
        git("worktree", "add", "--detach", str(self.cand_revert_wt),
            self.cand_revert, cwd=self.origin)
        self.cand_revert_snap = batches._snapshot(
            str(self.cand_revert_wt))
        git("checkout", "-q", "main", cwd=self.origin)
        self.base_src_wt = self.root / "home" / "base-src-wt"
        git("worktree", "add", "--detach", str(self.base_src_wt),
            self.base_evo, cwd=self.origin)
        # The ORIGINAL candidate lives at the canonical run worktree:
        # non-forced checkout --detach at the existing canonical
        # path moves it to the new candidate commit. The binding
        # gate requires exactly run_dir(run_id)/work, so the run is
        # never pointed at an external home worktree (that was the
        # v2 setup failure). Old objects and proofs are retained;
        # nothing is force-removed or reset.
        before = batches._snapshot(str(self.run_work))
        self.assertTrue(before is not None and before["clean"])
        git("checkout", "--detach", self.cand_src,
            cwd=str(self.run_work))
        self.cand_src_test_wt = self.root / "home" / "cand-src-test-wt"
        git("worktree", "add", "--detach", str(self.cand_src_test_wt),
            self.cand_src_test, cwd=self.origin)
        self.bar_snap = batches._snapshot(str(self.base_src_wt))
        self.cand_snap = batches._snapshot(str(self.run_work))
        self.cand_test_snap = batches._snapshot(
            str(self.cand_src_test_wt))
        self.assertEqual(self.bar_snap["head"], self.base_evo)
        self.assertEqual(self.cand_snap["head"], self.cand_src)
        self.assertTrue(self.cand_snap["clean"])
        # Distinct toplevels, one shared common dir by construction.
        self.assertNotEqual(os.path.realpath(str(self.base_src_wt)),
                            os.path.realpath(str(self.run_work)))
        self.cand_head = self.cand_src
        self.bar_head = self.base_evo
        self.base_bar_wt = self.base_src_wt
        self._point_run_at(self.cand_src, self.cand_snap)
        self._establish_fixture_preflight()

    def _fixture_current_values(self):
        return {
            "binary": {"status": "ok",
                       "path": "/fixture/muse",
                       "real_path": "/fixture/muse",
                       "sha256": "ab" * 32},
            "policy": dict(pf.REQUIRED_POLICY),
            "config": {"status": "established",
                       "files": [{"path": "conf",
                                  "sha256": "cd" * 32}]},
            "source": {"status": "ok", "app_version": "fixture-1",
                       "files": [{"path": name, "sha256": "ef" * 32}
                                 for name in pf._MANAGED_SOURCE_FILES]},
            "env": pf._child_env_shape(),
            "auth": {"meta_api_key_present": False,
                     "contributor_env_present": False,
                     "credential_source": "owned-fixture-observer",
                     "billing_evidence": None,
                     "observed_how": "fixture"},
        }

    def _fixture_observer(self):
        values_auth = self._fixture_current_values()["auth"]

        def _observe(home_path, pfid, inputs, spawn):
            channels = {}
            for name in pf.REQUIRED_CHANNELS:
                channels[name] = {
                    "required": pf.REQUIRED_DIRECTION[name],
                    "observed": pf.REQUIRED_DIRECTION[name],
                    "positive": True,
                    "detail": "fixture observer: controlled marker",
                }
            return channels, dict(values_auth)

        return _observe

    def _establish_fixture_preflight(self, dedupe="fixture-world"):
        # Every worker/start test needs the fail-closed gate to PASS:
        # establish a genuinely verified fixture receipt through the
        # REAL create/run functions with a fake transport (model
        # boundary) and an explicit fake observer (admitted-observer
        # seam). Identity VALUES are fixture-supplied by mocking the
        # I/O boundary (current_identity); every comparison, schema
        # check, and fail-closed branch runs for real. Tests needing
        # a refusal state corrupt/remove this receipt in their body.
        cur = mock.patch("openmainty.preflight.current_identity",
                         return_value=self._fixture_current_values())
        cur.start()
        self.addCleanup(cur.stop)
        created = pf.create_preflight(str(self.root / "home"), dedupe)
        self.assertFalse(created.get("reused"))
        self.pf_id = created["pfid"]

        def _verified_transport(prompt, workdir, timeout, env, cancel):
            return (0, "{}",
                    {"requested_profile": dict(muse.SOURCE_REPAIR_PROFILE)})

        out = pf.run_preflight(str(self.root / "home"), self.pf_id,
                               transport=_verified_transport,
                               observer=self._fixture_observer())
        self.assertEqual(out["state"], "verified")
        authority = pf.check_target_authority(str(self.root / "home"))
        self.assertEqual(authority["pfid"], self.pf_id)
        return self.pf_id

    def _write_file(self, rel, text):
        target = self.origin / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _point_run_at(self, head, snap):
        run = json.loads(store.run_file(
            self.run_id).read_bytes().decode("utf-8"))
        run["workCommit"] = head
        run["workTreeFingerprint"] = snap["fingerprint"]
        run["verifiedCommits"] = {"review": head, "e2e": head}
        run["verifiedFingerprints"] = {
            "review": snap["fingerprint"], "e2e": snap["fingerprint"]}
        run["baseCommit"] = self.base_commit
        store.save(run)

    def _point_candidate_at(self, head, snap, worktree):
        # Canonical-contract move: the ORIGINAL candidate always
        # lives at the canonical run worktree, so re-checkout that
        # already-owned path at the new commit. The passed
        # worktree/snapshot is the intended proof the fresh
        # canonical checkout must agree with (same commit bytes);
        # `worktree` itself never becomes the run binding.
        self.assertTrue(Path(worktree).is_dir())
        before = batches._snapshot(str(self.run_work))
        self.assertTrue(before is not None and before["clean"])
        git("checkout", "--detach", head, cwd=str(self.run_work))
        fresh = batches._snapshot(str(self.run_work))
        self.assertEqual(fresh["head"], head)
        self.assertEqual(fresh["head"], snap["head"])
        self.assertEqual(fresh["fingerprint"], snap["fingerprint"])
        self.cand_head = head
        self.cand_snap = fresh
        self._point_run_at(head, fresh)

    def _write_derived_blocked_op(self, conflicts, cand_head, base_head,
                                  op_id=None):
        return RecoveryFixtureMixin._write_blocked_op(
            self, conflicted=conflicts, error=OP_ERROR,
            cand_head=cand_head, base_head=base_head, op_id=op_id)

    def _derived_canned_report(self, head, conflicts=None):
        paths = ["example/webpack.config.js"] if conflicts is None \
            else list(conflicts)
        if paths == ["example/dist/bundle.c.js"]:
            # Dist-only fixture: the generated bundle is small text,
            # so the real input carries it as sent windows at the
            # pinned refs. The response cites the actual sent line
            # verbatim and never claims unseen code.
            return {
                "status": "needs_changes",
                "summary": "후보 dist 번들이 베이스와 충돌한다",
                "cause": "동일 경로 양쪽 수정(생성물)",
                "conflict_files": ["example/dist/bundle.c.js"],
                "citations": [{"path": "example/dist/bundle.c.js",
                               "ref": head, "start_line": 1,
                               "end_line": 1,
                               "quote": "// dist bundle v0"}],
                "intended_changes": ["dist 충돌 해소"],
                "proposed_paths": ["example/dist/bundle.c.js"],
                "build_contract": None,
                "questions": ["질문1: dist 번들을 재생성하는가?"],
                "uncertainties": ["불확실1"],
                "limits": ["dist 번들 소용량 텍스트 범위만 전송됨"],
                "next_action": "파생 수정을 검토한다",
            }
        return {
            "status": "needs_changes",
            "summary": "후보 webpack 변경이 베이스와 충돌한다",
            "cause": "동일 경로 양쪽 수정",
            "conflict_files": ["example/webpack.config.js"],
            "citations": [{"path": "example/webpack.config.js",
                           "ref": head, "start_line": 1, "end_line": 1,
                           "quote": "// webpack base v0"}],
            "intended_changes": ["webpack 충돌 해소"],
            "proposed_paths": ["example/webpack.config.js"],
            "build_contract": None,
            "questions": ["질문1: 베이스 출력을 유지하는가?",
                          "질문2: 후보 진입점을 적용하는가?"],
            "uncertainties": ["불확실1"],
            "limits": ["생성물은 모델에 보내지 않음"],
            "next_action": "파생 수정을 검토한다",
        }

    def _done_derived_review(self, op_id, cand_head, conflicts=None):
        self._write_derived_blocked_op(
            ["example/webpack.config.js"] if conflicts is None
            else list(conflicts),
            cand_head, self.bar_head, op_id=op_id)
        req = ir.request_review(op_id)
        with mock.patch(
                "openmainty.muse.exec_prompt",
                return_value=(0, canned_jsonl(json.dumps(
                    self._derived_canned_report(cand_head, conflicts))),
                    {"m": 1})):
            rc = ir.execute_review(op_id, req["rid"])
        self.assertEqual(rc, 0)
        return req

    def _origin_refs(self, op_id):
        st = ir.read_status(op_id)
        latest, shas = st["latest"], st["origin_shas"]
        self.assertTrue(st["current"])
        return (latest["input"], latest["report"], shas["input"],
                shas["report"])

    def _create(self, op_id, input_name, report_name, in_sha, rep_sha,
                hyp_id="H-A",
                note="가설 메모: 베이스 유지, 후보 적용",
                retry_of=None, live_jobs=None):
        with m2.op_locked(op_id):
            return df.create_derived_fix(
                op_id, input_name, report_name,
                {"input_sha256": in_sha, "report_sha256": rep_sha},
                {"id": hyp_id, "note": note},
                live_jobs=dict(live_jobs or {}), retry_of=retry_of)

    def _start(self, op_id, dfix):
        with m2.op_locked(op_id):
            return df.start_derived_run(dfix)

    def _run_worker(self, op_id, dfix, attempt_id, **kw):
        # Caller contract: the parent-op exclusion is held around
        # every direct worker call.
        with m2.op_locked(op_id):
            return df.run_repair_worker(dfix, attempt_id, **kw)

    def _read(self, dfix):
        return df._read_record(dfix)

class TestDerivedCreate(DerivedFixFixtureMixin):
    def test_create_rejects_malformed_origin(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        with m2.op_locked(self.blocked_id):
            with self.assertRaises(df.DerivedFixError) as cm:
                df.create_derived_fix(
                    self.blocked_id, "input-xyz", names[1],
                    {"input_sha256": names[2],
                     "report_sha256": names[3]},
                    {"id": "H-A", "note": "n"}, live_jobs={})
            self.assertEqual(cm.exception.http_status, 400)
            with self.assertRaises(df.DerivedFixError) as cm:
                df.create_derived_fix(
                    self.blocked_id, names[0], names[1],
                    {"input_sha256": "zz", "report_sha256": names[3]},
                    {"id": "H-A", "note": "n"}, live_jobs={})
            self.assertEqual(cm.exception.http_status, 400)
            with self.assertRaises(df.DerivedFixError) as cm:
                df.create_derived_fix(
                    self.blocked_id, names[0], names[1],
                    {"input_sha256": names[2],
                     "report_sha256": names[3]},
                    {"id": "H-Z", "note": "n"}, live_jobs={})
            self.assertEqual(cm.exception.code, "bad-hypothesis")

    def test_create_needs_current_origin(self):
        self._write_derived_blocked_op(
            ["example/webpack.config.js"], self.cand_head,
            self.bar_head)
        with m2.op_locked(self.blocked_id):
            with self.assertRaises(df.DerivedFixError) as cm:
                df.create_derived_fix(
                    self.blocked_id, "input-" + "0" * 16,
                    "report-" + "0" * 16,
                    {"input_sha256": "0" * 64,
                     "report_sha256": "0" * 64},
                    {"id": "H-A", "note": "n"}, live_jobs={})
        self.assertEqual(cm.exception.code, "no-current-origin")

    def test_create_rejects_stale_expected_revision(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        self.assertFalse(out["reused"])
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(self.blocked_id, names[0], names[1], "0" * 64,
                         names[3])
        self.assertEqual(cm.exception.code, "stale-origin")
        self.assertEqual(cm.exception.http_status, 409)

    def test_same_key_open_record_continues_without_duplicate(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        first = self._create(self.blocked_id, *names)
        second = self._create(self.blocked_id, *names)
        self.assertFalse(second["reused"])
        self.assertEqual(second["dfix"], first["dfix"])
        self.assertTrue(second.get("reconciled"))
        recs = df._scan_derived(self.blocked_id)
        self.assertEqual(len(recs), 1)

    def test_changed_hypothesis_starts_new_record(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        first = self._create(self.blocked_id, *names)
        other = self._create(self.blocked_id, *names, hyp_id="H-B")
        self.assertNotEqual(other["dfix"], first["dfix"])
        self.assertEqual(len(df._scan_derived(self.blocked_id)), 2)

    def _run_to_terminal_error(self, op_id, names, make_stream):
        out = self._create(op_id, *names)
        started = self._start(op_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = rec["workspace"]["path"]
        proposal = _proposal(["example/webpack.config.js"],
                             {"example/webpack.config.js": "0" * 64},
                             [{"path": "example/webpack.config.js",
                               "conflict_id": "example/webpack.config.js",
                               "choice": "H-A"}])
        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=make_stream(work, proposal)):
            rc = self._run_worker(op_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        return out["dfix"]

    def test_failed_record_needs_explicit_retry(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)

        def boom_stream(work, proposal):
            def _run(*a, **k):
                raise RuntimeError("transport down")
            return _run

        dfix = self._run_to_terminal_error(self.blocked_id, names,
                                           boom_stream)
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(self.blocked_id, *names)
        self.assertEqual(cm.exception.code, "retry-required")
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(self.blocked_id, *names,
                         retry_of="dfix-" + "0" * 32)
        self.assertEqual(cm.exception.code, "bad-retry")
        retried = self._create(self.blocked_id, *names, retry_of=dfix)
        self.assertTrue(retried.get("retried"))
        self.assertEqual(retried["dfix"], dfix)

    def test_retry_attempt_cap(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        with m2.op_locked(self.blocked_id):
            for _ in range(5):
                df.start_source_attempt(out["dfix"])
                rec = self._read(out["dfix"])
                att = dict(rec["source_attempts"][-1])
                att["status"] = "source-error"
                att["ended_at"] = "2999-01-01T00:00:00+00:00"
                df._store_attempt(rec, att, record_status="source-error")
            with self.assertRaises(df.DerivedFixError) as cm:
                df.start_source_attempt(out["dfix"])
        self.assertEqual(cm.exception.code, "too-many-attempts")

class TestDerivedReplayBinding(DerivedFixFixtureMixin):
    def test_revised_run_bytes_before_create_are_stale(self):
        # Complete a valid review, then revise parent run bytes
        # under the SAME ids (display-only title). Old input/report
        # SHAs still match, but the unchanged full replay no longer
        # reproduces the stored fingerprint: the origin is stale, and
        # no record, git, or transport may happen.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        root = ir.rec_root(self.blocked_id)
        before_in = hashlib.sha256(
            (root / f"{names[0]}.json").read_bytes()).hexdigest()
        run_path = store.run_file(self.run_id)
        run_doc = json.loads(run_path.read_bytes().decode("utf-8"))
        run_doc["title"] = run_doc.get("title", "") + " revised"
        store.save(run_doc)
        with m2.op_locked(self.blocked_id):
            with self.assertRaises(df.DerivedFixError) as cm:
                df.create_derived_fix(
                    self.blocked_id, names[0], names[1],
                    {"input_sha256": names[2],
                     "report_sha256": names[3]},
                    {"id": "H-A", "note": "n"}, live_jobs={})
        self.assertEqual(cm.exception.code, "stale-origin")
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(df._scan_derived(self.blocked_id), [])
        after_in = hashlib.sha256(
            (root / f"{names[0]}.json").read_bytes()).hexdigest()
        self.assertEqual(before_in, after_in)
        self.assertEqual(after_in, names[2])

    def test_forged_candidate_descriptor_refused(self):
        # Same head+fingerprint but a swapped branch/worktree in the
        # saved record is not the originally verified source identity:
        # the lifecycle must refuse before any git, and the original
        # op/review bytes stay untouched.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        doc_path = df.derived_path(out["dfix"])
        before_op = batches.op_file(self.blocked_id).read_bytes()
        before_in = hashlib.sha256(
            (ir.rec_root(self.blocked_id) /
             f"{names[0]}.json").read_bytes()).hexdigest()
        raw = json.loads(doc_path.read_bytes().decode("utf-8"))
        raw["candidate"]["branch"] = "codex/next/issue-856-evil"
        raw["candidate"]["worktree"] = "/tmp/evil-worktree"
        doc_path.write_bytes(json.dumps(
            raw, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        with m2.op_locked(self.blocked_id):
            with self.assertRaises(df.DerivedFixError) as cm:
                df.start_derived_run(out["dfix"])
        self.assertEqual(cm.exception.code, "stale-origin")
        self.assertFalse(Path(df._expected_work_path(
            out["dfix"])).exists())
        self.assertEqual(batches.op_file(self.blocked_id).read_bytes(),
                         before_op)
        after_in = hashlib.sha256(
            (ir.rec_root(self.blocked_id) /
             f"{names[0]}.json").read_bytes()).hexdigest()
        self.assertEqual(after_in, before_in)


class TestDerivedPrepare(DerivedFixFixtureMixin):
    def _prepare(self, op_id, cand_head):
        self._done_derived_review(op_id, cand_head)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        with m2.op_locked(op_id):
            return out["dfix"], df.start_derived_run(out["dfix"]), names

    def test_prepare_linked_worktrees_and_sentinel(self):
        # Candidate and base are distinct linked worktrees (not one
        # checkout reused): preparation must accept the shared common
        # dir. Recorded webpack conflict stays at base (with the
        # base-only sentinel intact); base-only test/dist evolution is
        # retained, never replayed away.
        dfix, started, _names = self._prepare(self.blocked_id,
                                              self.cand_head)
        rec = self._read(dfix)
        ws = rec["workspace"]
        self.assertEqual(ws["base_head"], self.bar_head)
        self.assertEqual(ws["prep_head"], ws["head"])
        self.assertTrue(ws["clean"])
        scope = ws["scope"]
        self.assertEqual(scope["conflicts"],
                         ["example/webpack.config.js"])
        self.assertEqual(scope["original_touched"],
                         ["example/dist/bundle.c.js"])
        paths = {e["path"] for e in ws["conflict_reset"]}
        self.assertEqual(paths, {"example/webpack.config.js"})
        reset = ws["conflict_reset"][0]
        self.assertTrue(reset["base_blob"])
        self.assertTrue(reset["base_blob_sha256"])
        self.assertFalse(reset["absent_at_base"])
        retained = {e["path"]: e for e in ws["retained_base"]}
        self.assertIn("src/__tests__/webpack-web.test.ts", retained)
        self.assertIn("example/dist/bundle.c.js", retained)
        self.assertIn("base-side evolution",
                      retained["src/__tests__/webpack-web.test.ts"]
                      ["note"])
        self.assertIn("regenerated",
                      retained["example/dist/bundle.c.js"]["note"])
        # Prepared tree == base tree here (no clean applies): the
        # sentinel and the base-only test line survive byte-identical.
        work = Path(ws["path"])
        webpack = (work / "example/webpack.config.js").read_text(
            encoding="utf-8")
        self.assertIn(WEBPACK_SENTINEL, webpack)
        self.assertNotIn("candidate change", webpack)
        test_text = (work / "src/__tests__/webpack-web.test.ts"
                     ).read_text(encoding="utf-8")
        self.assertIn(TEST_BASE_LINE, test_text)
        self.assertEqual(ws["allowed_changed_paths"],
                         list(df._ALLOWED_SOURCE_PATHS))
        # The prep commit carries the app automation identity, never
        # test-fixture data or the operator's global config.
        ident = git("log", "-1", "--format=%an:%ae", cwd=str(work))
        self.assertTrue(ident.startswith("openmainty-derived-fix:"))

    def test_prepare_applies_original_patch_preserving_base(self):
        # Candidate touches the test file too (C3): the ORIGINAL
        # base_commit->candidate patch applies cleanly and the
        # base-only test line survives alongside the candidate line.
        self._point_candidate_at(self.cand_src_test,
                                 self.cand_test_snap,
                                 self.cand_src_test_wt)
        op_id = "prep-issue-856-d2"
        dfix, _started, _names = self._prepare(op_id,
                                              self.cand_src_test)
        rec = self._read(dfix)
        ws = rec["workspace"]
        applied = {e["path"] for e in ws["applied"]}
        self.assertEqual(
            applied, {"src/__tests__/webpack-web.test.ts"})
        text = (Path(ws["path"]) /
                "src/__tests__/webpack-web.test.ts").read_text(
                    encoding="utf-8")
        self.assertIn(TEST_BASE_LINE, text)
        self.assertIn(TEST_CAND_LINE, text)
        entry = ws["applied"][0]
        self.assertEqual(entry["bytes"], len(text.encode("utf-8")))

    def test_sent_prompt_carries_server_hypothesis_definition(self):
        # The worker never sees the UI: the exact sent prompt must
        # carry the SERVER-owned meaning of the selected opaque id,
        # for both choices, with the unselected meaning absent and
        # the operator-experiment (never owner-answer) labeling.
        # Markers come from the real canonical table, never copied
        # literals: a drift between the table and the prompt builder
        # fails here.
        defs = df._HYPOTHESIS_DEFS
        self.assertEqual(set(defs), {"H-A", "H-B"})
        cases = (("H-A", defs["H-A"], defs["H-B"]),
                 ("H-B", defs["H-B"], defs["H-A"]))
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        for hyp_id, marker, other in cases:
            out = self._create(self.blocked_id, *names,
                               hyp_id=hyp_id,
                               note=f"운용자 메모 {hyp_id}")
            started = self._start(self.blocked_id, out["dfix"])
            captured = []

            def _run(prompt, workdir=None, **k):
                captured.append(prompt)
                target = Path(workdir) / "example/webpack.config.js"
                target.write_text(
                    target.read_text(encoding="utf-8").replace(
                        "// candidate region: entry",
                        f"// worker resolution {hyp_id}"),
                    encoding="utf-8")
                raw = target.read_bytes()
                return (0, _worker_jsonl(_proposal(
                    ["example/webpack.config.js"],
                    {"example/webpack.config.js":
                     hashlib.sha256(raw).hexdigest()},
                    [{"path": "example/webpack.config.js",
                      "conflict_id": "example/webpack.config.js",
                      "choice": hyp_id}])), {"m": 1})

            with mock.patch("openmainty.muse.exec_source_repair",
                            side_effect=_run):
                rc = self._run_worker(self.blocked_id, out["dfix"],
                                      started["attempt_id"])
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured), 1)
            self.assertIn(marker, captured[0])
            self.assertNotIn(other, captured[0])
            self.assertIn("서버 소유", captured[0])
            self.assertIn("소유자 정책 답변이 아님", captured[0])
            self.assertIn(f"운용자 메모 {hyp_id}", captured[0])

    def test_prepare_overlapping_append_is_conflict_reset(self):
        # The TRUE overlapping case: the candidate inserts at the
        # same mid-file spot where the base evolution inserted its
        # sentinel. The ORIGINAL patch cannot apply cleanly, so the
        # path lands in conflict_reset with the exact reason — never
        # silently applied, never retained as if untouched, and the
        # sentinel survives byte-identical for a future worker.
        git("checkout", "-qb", "fixture/overlap-neg", self.base_commit,
            cwd=self.origin)
        self._write_file(
            "src/__tests__/webpack-web.test.ts",
            TEST_V0.replace(
                "const a = 1;\n",
                "const a = 1;\n// overlapping candidate change\n", 1))
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "fixture overlapping candidate",
            cwd=self.origin)
        overlap_head = git("rev-parse", "HEAD", cwd=self.origin)
        overlap_wt = self.root / "home" / "overlap-wt"
        git("worktree", "add", "--detach", str(overlap_wt),
            overlap_head, cwd=self.origin)
        overlap_snap = batches._snapshot(str(overlap_wt))
        self._point_candidate_at(overlap_head, overlap_snap,
                                 overlap_wt)
        op_id = "prep-issue-856-d3"
        dfix, _started, _names = self._prepare(op_id, overlap_head)
        rec = self._read(dfix)
        ws = rec["workspace"]
        applied = {e["path"] for e in ws["applied"]}
        reset = {e["path"]: e for e in ws["conflict_reset"]}
        retained = {e["path"] for e in ws["retained_base"]}
        self.assertNotIn("src/__tests__/webpack-web.test.ts",
                         applied)
        self.assertNotIn("src/__tests__/webpack-web.test.ts",
                         retained)
        self.assertIn("src/__tests__/webpack-web.test.ts", reset)
        self.assertIn("does not apply cleanly",
                      reset["src/__tests__/webpack-web.test.ts"]
                      ["reason"])
        text = (Path(ws["path"]) /
                "src/__tests__/webpack-web.test.ts").read_text(
                    encoding="utf-8")
        self.assertIn(TEST_BASE_LINE, text)
        self.assertNotIn("overlapping candidate change", text)

    def test_prepare_rejects_foreign_repo(self):
        # A full clone carries the SAME commit SHAs, so live binding
        # (snapshot + pins) passes — but its common dir differs. The
        # same-repo gate must refuse before any worktree or merge.
        clone = self.root / "home" / "clone-repo"
        git("clone", str(self.origin), str(clone), cwd=self.root)
        clone_wt = self.root / "home" / "clone-wt"
        git("worktree", "add", "--detach", str(clone_wt), self.base_evo,
            cwd=clone)
        clone_snap = batches._snapshot(str(clone_wt))
        self.bar_head = self.base_evo
        self.bar_snap = clone_snap
        self.base_bar_wt = clone_wt
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        with m2.op_locked(self.blocked_id):
            with self.assertRaises(df.DerivedFixError) as cm:
                df.start_derived_run(out["dfix"])
        self.assertEqual(cm.exception.code, "bad-parent")
        self.assertFalse(Path(df._expected_work_path(
            out["dfix"])).exists())

    def test_common_dir_accepts_linked_worktrees(self):
        # Direct regression for the real layout: distinct toplevels,
        # one shared common dir. A toplevel comparison would reject
        # this; the common-dir gate accepts it.
        def _common(path):
            out = git("rev-parse", "--git-common-dir", cwd=str(path))
            full = out if os.path.isabs(out) else os.path.normpath(
                os.path.join(str(path), out))
            return os.path.realpath(full)

        self.assertNotEqual(
            os.path.realpath(str(self.base_src_wt)),
            os.path.realpath(str(self.run_work)))
        self.assertEqual(_common(self.base_src_wt),
                         _common(self.run_work))

    def test_prepare_failure_preserves_workspace(self):
        # Recorded conflicts leave the CURRENT diff (scope drift):
        # the candidate reverted to base content, so the recorded
        # webpack conflict is gone from base..candidate. Preparation
        # refuses AFTER the worktree exists and the partial workspace
        # is preserved, never force-removed.
        self._point_candidate_at(self.cand_revert,
                                 self.cand_revert_snap,
                                 self.cand_revert_wt)
        self._done_derived_review(self.blocked_id, self.cand_revert)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        with m2.op_locked(self.blocked_id):
            with self.assertRaises(df.DerivedFixError) as cm:
                df.start_derived_run(out["dfix"])
        self.assertEqual(cm.exception.code, "scope-drift")
        self.assertTrue(Path(df._expected_work_path(
            out["dfix"])).is_dir())

    def test_prepare_needs_exact_origin_names(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        bad = (names[0].replace("input-", "input_"), names[1],
               names[2], names[3])
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(self.blocked_id, *bad)
        self.assertEqual(cm.exception.http_status, 400)

class TestDerivedWorker(DerivedFixFixtureMixin):
    def _terminal_ready(self, op_id, cand_head, edit_fn, resolutions):
        self._done_derived_review(op_id, cand_head)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        started = self._start(op_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            # Worker edits happen HERE, inside the transport: the
            # pre-transport proof must see a clean prep workspace,
            # and these mutations represent the worker's own output
            # after its precondition passed.
            changed = edit_fn(work)
            hashes = {}
            for rel in sorted(changed):
                raw = (work / rel).read_bytes()
                hashes[rel] = hashlib.sha256(raw).hexdigest()
            return (0, _worker_jsonl(_proposal(
                sorted(changed), hashes, resolutions)), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(op_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 0)
        return out["dfix"], started["attempt_id"]

    def test_worker_terminal_ready_with_raw_accounting(self):
        def _edit(work):
            target = work / "example/webpack.config.js"
            text = target.read_text(encoding="utf-8")
            target.write_text(
                text.replace("// candidate region: entry",
                             "// worker resolution H-A"),
                encoding="utf-8")
            return {"example/webpack.config.js"}

        dfix, att = self._terminal_ready(
            self.blocked_id, self.cand_head, _edit,
            [{"path": "example/webpack.config.js",
              "conflict_id": "example/webpack.config.js",
              "choice": "H-A"}])
        rec = self._read(dfix)
        self.assertEqual(rec["status"], "source-ready-unverified")
        attempt = next(a for a in rec["source_attempts"]
                       if a["id"] == att)
        self.assertEqual(attempt["status"], "source-ready-unverified")
        self.assertTrue(attempt["prompt_sha256"])
        self.assertTrue(attempt["log_bytes"] > 0)
        self.assertEqual(attempt["raw_total_chars"],
                         len(attempt["raw_head"]))
        self.assertEqual(attempt["raw_total_bytes"],
                         attempt["log_bytes"])
        self.assertFalse(attempt["truncated"])
        self.assertEqual(
            attempt["raw_head_sha256"],
            hashlib.sha256(
                attempt["raw_head"].encode("utf-8")).hexdigest())
        verified = attempt["verified_paths"]
        self.assertEqual(set(verified),
                         {"example/webpack.config.js"})
        ws = rec["workspace"]
        self.assertTrue(ws["result_head"])
        self.assertEqual(ws["result_tree"],
                         git("rev-parse", f"{ws['result_head']}^{{tree}}",
                             cwd=str(Path(ws["path"]))))
        snap = batches._snapshot(str(Path(ws["path"])))
        self.assertEqual(snap["fingerprint"], ws["fingerprint"])

    def test_worker_rc0_without_terminal_is_failure(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        only_delta = json.dumps({"payload": {"kind": "run_output_delta",
                                               "text": "investigating"}})
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(0, only_delta, {"m": 1})):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["status"], "source-error")
        self.assertIn("rc0 is not success", attempt["error"])
        self.assertTrue(attempt["raw_total_chars"] > 0)
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_timeout_and_cancel(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(124, "partial out", {"m": 1})):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertIn("timed out",
                      rec["source_attempts"][-1]["error"])
        out2 = self._create(self.blocked_id, *names, hyp_id="H-B")
        started2 = self._start(self.blocked_id, out2["dfix"])
        ev = threading.Event()
        ev.set()
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(0, canned_jsonl("{}"),
                                      {"m": 1})):
            rc = self._run_worker(self.blocked_id, out2["dfix"],
                                      started2["attempt_id"],
                                      cancel_event=ev)
        self.assertEqual(rc, 130)
        rec2 = self._read(out2["dfix"])
        self.assertEqual(rec2["status"], "cancelled")

    def test_worker_out_of_scope_dist_refused(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            target = work / "example/dist/bundle.c.js"
            target.write_text(target.read_text(encoding="utf-8")
                              + "// worker evil\n", encoding="utf-8")
            wp = work / "example/webpack.config.js"
            raw = wp.read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()})), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_deleted_path_refused(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        calls = []

        def _run(*a, **k):
            calls.append(1)
            (work / "example/webpack.config.js").unlink()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js": "0" * 64})), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        rec = self._read(out["dfix"])
        # The reason lives on the exact full attempt — resolved from
        # history by the pointer id/status (asserted unique), never
        # from the pointer itself — with no result committed and the
        # single attempt preserved as the refused terminal.
        self.assertEqual(rec["status"], "refused")
        latest = rec["latest_attempt"]
        self.assertEqual(latest, {"id": started["attempt_id"],
                                 "status": "refused"})
        full = [a for a in rec["source_attempts"]
                if a["id"] == latest["id"]
                and a["status"] == latest["status"]]
        self.assertEqual(len(full), 1)
        self.assertIn("deleted", full[0]["error"])
        self.assertEqual(len(rec["source_attempts"]), 1)
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_symlink_attack_refused(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            target = work / "example/webpack.config.js"
            text = target.read_text(encoding="utf-8")
            target.unlink()
            os.symlink("/etc/hostname", target)
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(text.encode("utf-8")).hexdigest()})),
                {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_edits_retained_allowed_path_and_cache_ignored(self):
        # The test file is base-side evolution (prep retained it), but
        # it stays inside the allowlist: worker edits verify and
        # commit normally. Regular worker cache noise never enters
        # the result commit.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])
        rel = "src/__tests__/webpack-web.test.ts"
        retained = {e["path"] for e in rec["workspace"]["retained_base"]}
        self.assertIn(rel, retained)

        def _run(*a, **k):
            node = work / rel
            node.write_text(node.read_text(encoding="utf-8")
                            + "// worker test fix\n", encoding="utf-8")
            cache = work / ".openmainty-cache" / "worker-note.txt"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text("noise\n", encoding="utf-8")
            raw = node.read_bytes()
            return (0, _worker_jsonl(_proposal(
                [rel],
                {rel: hashlib.sha256(raw).hexdigest()},
                [{"path": rel,
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}], ins=1, dels=0)), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 0)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "source-ready-unverified")
        self.assertIn(rel, rec["source_attempts"][-1]["verified_paths"])
        self.assertIn(TEST_BASE_LINE,
                      (work / rel).read_text(encoding="utf-8"))
        tree = git("ls-tree", "-r", "--name-only",
                   rec["workspace"]["result_head"], cwd=str(work))
        self.assertNotIn(".openmainty-cache", tree.splitlines())

    def test_worker_cache_symlink_refused(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            cache = work / ".openmainty-cache"
            cache.mkdir(parents=True, exist_ok=True)
            os.symlink("/etc/hostname", cache / "evil-link")
            # Schema-valid claim so the run reaches the worktree
            # gate, where the cache escape refuses first.
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js": "0" * 64})), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_drift_during_transport_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        root = ir.rec_root(self.blocked_id)
        target = root / f"{names[0]}.json"
        pristine = target.read_bytes()
        # Restore ONLY in outer cleanup, after the worker returns:
        # the mutated bytes must stay changed through the worker's
        # post-proof, never restored before it.
        self.addCleanup(lambda: target.write_bytes(pristine))
        calls = []

        def _run(*a, **k):
            # The mutation happens INSIDE the transport window
            # (after the clean pre-transport proof, before the
            # post-transport proof): a schema-valid claim reaches
            # post-proof, where the drifted origin refuses as
            # stale — the pre-transport gate never sees it.
            calls.append(1)
            target.write_bytes(pristine + b" ")
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js": "0" * 64})),
                {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "source-error")
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["id"], started["attempt_id"])
        self.assertEqual(attempt["status"], "source-error")
        self.assertIn("[stale-origin]", attempt["error"])

    def test_worker_prompt_drift_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])

        calls = []

        def _run(*a, **k):
            calls.append(1)
            rec = self._read(out["dfix"])
            ws = dict(rec["workspace"])
            ws["applied"] = [{"path": "example/webpack.config.js",
                              "bytes": 1,
                              "blob_sha256": "0" * 64}]
            rec = dict(rec)
            rec["workspace"] = ws
            df._write_record(rec)
            # Schema-valid claim so the run reaches the recompute
            # gate, where the forged manifest fails the sent pin.
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js": "0" * 64})), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        # The recomputed prompt no longer matches the sent pin, so
        # the run cannot record a result from forged state.
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)
        rec = self._read(out["dfix"])
        # The reason lives on the exact full attempt — resolved from
        # history by the pointer id/status (asserted unique), never
        # from the pointer itself — with no result committed and the
        # single attempt preserved as the failed terminal.
        self.assertEqual(rec["status"], "source-error")
        latest = rec["latest_attempt"]
        self.assertEqual(latest, {"id": started["attempt_id"],
                                 "status": "source-error"})
        full = [a for a in rec["source_attempts"]
                if a["id"] == latest["id"]
                and a["status"] == latest["status"]]
        self.assertEqual(len(full), 1)
        self.assertIn("prompt-drift", full[0]["error"])
        self.assertEqual(len(rec["source_attempts"]), 1)
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_commit_during_transport_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            # A worker-side commit moves HEAD off the prep head: the
            # post-transport proof must refuse before any staging.
            git("add", "-A", cwd=str(work))
            git("-c", "user.name=t", "-c", "user.email=t@t",
                "commit", "-qm", "worker commit", cwd=str(work))
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "source-error")
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["id"], started["attempt_id"])
        self.assertEqual(attempt["status"], "source-error")
        self.assertIn("[workspace-changed]", attempt["error"])
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_extra_claim_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            # Claims the second allowed file without changing it.
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js",
                 "src/__tests__/webpack-web.test.ts"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest(),
                 "src/__tests__/webpack-web.test.ts": "0" * 64},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["id"], started["attempt_id"])
        self.assertEqual(attempt["status"], "refused")
        self.assertIn("[changed-mismatch]", attempt["error"])
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_hypothesis_mismatch_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            # Valid edit and hashes, but answers the OTHER
            # hypothesis: the resolution must name this attempt's.
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-B"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["id"], started["attempt_id"])
        self.assertEqual(attempt["status"], "refused")
        self.assertIn("[invalid-resolution]", attempt["error"])
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_worker_diff_stat_mismatch_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _run(*a, **k):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            # True edit, true hashes, true changed set — but an
            # invented insertion count the real staged diff refutes.
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}], ins=999)), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["id"], started["attempt_id"])
        self.assertEqual(attempt["status"], "refused")
        self.assertIn("[diff-mismatch]", attempt["error"])
        self.assertIsNone(rec["workspace"]["result_head"])

    def test_terminal_result_reuse_and_stale(self):
        def _edit(work):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            return {"example/webpack.config.js"}

        dfix, _att = self._terminal_ready(
            self.blocked_id, self.cand_head, _edit,
            [{"path": "example/webpack.config.js",
              "conflict_id": "example/webpack.config.js",
              "choice": "H-A"}])
        names = self._origin_refs(self.blocked_id)
        before = self._read(dfix)
        n_attempts = len(before["source_attempts"])

        def _boom(*a, **k):
            raise AssertionError("reuse must not transport")
        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_boom):
            reused = self._create(self.blocked_id, *names)
            # The public create/start reuse invariant: create reuses
            # the verified terminal record without transport, while a
            # direct internal restart of the completed record refuses
            # 409 — prep identity is checked before startability (the
            # result commit moved HEAD past prep_head), so no new
            # attempt and no worker call. The code is the ordering
            # outcome, not a branch contract: only the 409 and the
            # immutable history below are asserted. There is no
            # already-recorded start error in the product contract.
            with self.assertRaises(df.DerivedFixError) as cm:
                self._start(self.blocked_id, dfix)
        self.assertEqual(cm.exception.http_status, 409)
        self.assertTrue(reused.get("reused"))
        self.assertEqual(reused["dfix"], dfix)
        after = self._read(dfix)
        self.assertEqual(len(after["source_attempts"]), n_attempts)
        self.assertEqual(after["latest_attempt"],
                         before["latest_attempt"])
        rec = self._read(dfix)
        work = Path(rec["workspace"]["path"])
        target = work / "example/webpack.config.js"
        target.write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(self.blocked_id, *names)
        self.assertEqual(cm.exception.code, "stale-result")

    def test_stale_record_projects_stale_with_admin_reason(self):
        # Genuine ready result → workspace tamper → reuse refuses
        # stale. The READ-ONLY projection must show the current
        # administrative stale verdict and reason — never the
        # retained ready banner — while the historical ready attempt
        # and its artifacts survive byte-identical.
        def _edit(work):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            return {"example/webpack.config.js"}

        dfix, att = self._terminal_ready(
            self.blocked_id, self.cand_head, _edit,
            [{"path": "example/webpack.config.js",
              "conflict_id": "example/webpack.config.js",
              "choice": "H-A"}])
        before = self._read(dfix)
        ready_head = before["workspace"]["result_head"]
        ready_tree = before["workspace"]["result_tree"]
        verified = dict(
            before["source_attempts"][0]["verified_paths"])
        work = Path(before["workspace"]["path"])
        (work / "example/webpack.config.js").write_text(
            "tampered\n", encoding="utf-8")
        names = self._origin_refs(self.blocked_id)
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(self.blocked_id, *names)
        self.assertEqual(cm.exception.code, "stale-result")
        # The projection must equal the ACTUAL stored administrative
        # reason — no invented wording. Read it from the record the
        # refusal just marked, then require identity (and non-empty).
        admin = self._read(dfix)["error"]
        self.assertTrue(admin)
        self.assertIn("no longer verifies", admin)
        items = df.describe_derived(self.blocked_id, set())["derived"]
        shown = [d for d in items if d["dfix"] == dfix][0]
        # Current stale verdict + administrative reason win.
        self.assertEqual(shown["status"], "stale")
        self.assertEqual(shown["recorded_status"], "stale")
        self.assertEqual(shown["error"], admin)
        # Historical ready attempt retained with its own identity;
        # its (empty) reason is NOT the top-level one.
        self.assertEqual(len(shown["attempts"]), 1)
        self.assertEqual(shown["attempts"][0]["id"], att)
        self.assertEqual(shown["attempts"][0]["status"],
                         "source-ready-unverified")
        self.assertEqual(shown["attempts"][0]["error"], "")
        self.assertEqual(shown["latest_attempt"], att)
        # Original artifacts survive: the pinned blob's RAW bytes
        # (read with real Git, never through the stripping text
        # helper) still SHA256 to the recorded value. Commit, tree,
        # and blob OID are verified separately in the Git domain —
        # never compared across hash domains.
        proc = subprocess.run(
            ["git", "-C", str(work), "cat-file", "blob",
             f"{ready_head}:example/webpack.config.js"],
            capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(hashlib.sha256(proc.stdout).hexdigest(),
                         verified["example/webpack.config.js"])
        self.assertEqual(
            git("rev-parse", ready_head + "^{commit}", cwd=str(work)),
            ready_head)
        self.assertEqual(
            git("rev-parse", f"{ready_head}^{{tree}}", cwd=str(work)),
            ready_tree)
        blob_oid = git(
            "rev-parse", f"{ready_head}:example/webpack.config.js",
            cwd=str(work))
        self.assertRegex(blob_oid, "^[0-9a-f]{40}$")
        kept = self._read(dfix)
        self.assertEqual(kept["workspace"]["result_head"], ready_head)
        self.assertEqual(kept["workspace"]["result_tree"], ready_tree)
        self.assertEqual(
            kept["source_attempts"][0]["verified_paths"], verified)

    def test_prior_record_reason_does_not_mask_current_failure(self):
        # Real lifecycle: a preparation refusal stores the record
        # reason first; an explicit retry then runs a worker that
        # fails. The read-only projection must show the CURRENT
        # failed attempt's cause — not the older record reason —
        # with stored history unchanged and no read-side writes.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        df.refuse_derived(out["dfix"], "prep failed")
        again = self._create(self.blocked_id, *names,
                             retry_of=out["dfix"])
        self.assertTrue(again.get("retried"))
        self.assertEqual(again["dfix"], out["dfix"])
        retry = self._start(self.blocked_id, out["dfix"])

        def _run(*a, **k):
            work = Path(self._read(out["dfix"])["workspace"]["path"])
            (work / "example/webpack.config.js").unlink()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js": "0" * 64})), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  retry["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        # Stored history is unchanged: the old record reason stays
        # on the record, the fresh cause only on the attempt.
        self.assertEqual(rec["error"], "prep failed")
        latest = rec["latest_attempt"]
        self.assertEqual(latest, {"id": retry["attempt_id"],
                                 "status": "refused"})
        full = [a for a in rec["source_attempts"]
                if a["id"] == latest["id"]
                and a["status"] == latest["status"]]
        self.assertEqual(len(full), 1)
        self.assertIn("deleted", full[0]["error"])
        raw_before = df.derived_path(out["dfix"]).read_bytes()
        items = df.describe_derived(self.blocked_id, set())["derived"]
        shown = [d for d in items if d["dfix"] == out["dfix"]][0]
        self.assertEqual(df.derived_path(out["dfix"]).read_bytes(),
                         raw_before)
        self.assertEqual(shown["status"], "refused")
        self.assertIn("deleted", shown["error"])
        self.assertNotIn("prep failed", shown["error"])

    def test_clean_failure_retry_starts_fresh_attempt(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        df.note_job(out["dfix"],
                    {"id": "job-clean-1", "kind": "derived-fix",
                     "label": started["attempt_id"]})
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(1, "", {"m": 0})):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        old = self._read(out["dfix"])
        self.assertEqual(old["status"], "source-error")
        # Explicit retry on a clean workspace proceeds: the failed
        # attempt keeps its job metadata in history, current
        # ownership is None until the new attempt binds.
        retried = self._create(self.blocked_id, *names,
                               retry_of=out["dfix"])
        self.assertTrue(retried.get("retried"))
        new = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        self.assertEqual(len(rec["source_attempts"]), 2)
        self.assertEqual(rec["source_attempts"][0]["job"]["id"],
                         "job-clean-1")
        self.assertEqual(rec["source_attempts"][0]["job"]["label"],
                         started["attempt_id"])
        self.assertIsNone(rec["job"])
        self.assertEqual(rec["latest_attempt"]["id"],
                         new["attempt_id"])

    def test_retry_on_dirty_workspace_is_typed_non_retriable(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])

        def _dirty(*a, **k):
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              + "// partial worker edit\n",
                              encoding="utf-8")
            return (1, "crashed with edits", {"m": 0})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_dirty):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        failed = self._read(out["dfix"])
        self.assertEqual(failed["status"], "source-error")
        retried = self._create(self.blocked_id, *names,
                               retry_of=out["dfix"])
        self.assertTrue(retried.get("retried"))
        # The dirty workspace refuses with a typed reason naming
        # the supported fresh derivation — never an overwrite,
        # reset, or rebind of the failed evidence.
        with self.assertRaises(df.DerivedFixError) as cm:
            self._start(self.blocked_id, out["dfix"])
        self.assertEqual(cm.exception.code, "retry-workspace-dirty")
        self.assertIn("fresh", cm.exception.message)
        kept = self._read(out["dfix"])
        self.assertEqual(kept["status"], "source-error")
        self.assertEqual(len(kept["source_attempts"]), 1)
        self.assertIn("// partial worker edit",
                      (work / "example/webpack.config.js").read_text(
                          encoding="utf-8"))

    def test_orphan_live_attempt_reconciles_and_clears_job(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        df.note_job(out["dfix"],
                    {"id": "job-orphan-1", "kind": "derived-fix",
                     "label": started["attempt_id"]})
        # Owner job died without reconciling (absent from the live
        # table): same-key start closes the orphan as a preserved
        # terminal and a fresh attempt follows on explicit start.
        again = self._create(self.blocked_id, *names, live_jobs={})
        self.assertTrue(again.get("reconciled"))
        new = self._start(self.blocked_id, out["dfix"])
        rec = self._read(out["dfix"])
        self.assertEqual(len(rec["source_attempts"]), 2)
        self.assertEqual(rec["source_attempts"][0]["status"],
                         "source-error")
        self.assertEqual(rec["source_attempts"][0]["job"]["id"],
                         "job-orphan-1")
        self.assertEqual(rec["source_attempts"][0]["job"]["label"],
                         started["attempt_id"])
        self.assertIsNone(rec["job"])
        self.assertEqual(rec["latest_attempt"]["id"],
                         new["attempt_id"])

    def test_ancestor_swap_after_clean_prep_refuses(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        container = Path(df._expected_work_path(
            out["dfix"])).parent
        backup = container.with_name(container.name + ".bak")
        os.rename(container, backup)
        self.addCleanup(lambda: (
            container.unlink() if container.is_symlink() else None,
            os.rename(backup, container)
            if backup.exists() else None))
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        os.symlink(str(elsewhere), str(container))

        def _boom(*a, **k):
            raise AssertionError("no transport after ancestor swap")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_boom):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["id"], started["attempt_id"])
        self.assertEqual(attempt["status"], "refused")
        self.assertIn("[ancestor-replaced]", attempt["error"])
        # Suspect path and original evidence preserved: the swap is
        # untouched and the genuine workspace still sits in backup.
        self.assertTrue(container.is_symlink())
        self.assertTrue(
            (backup / "work" / "example" / "webpack.config.js").exists()
            or (backup / "work").exists())

    def test_ancestor_swap_at_retry_refuses_before_git_reads(self):
        # R8 ordering: start_derived_run over a CARRIED workspace
        # must reprove the owned chain before any prep-identity Git
        # read. A swap after a genuine clean prep refuses here —
        # not later at transport — with no transport and the
        # suspect/evidence preserved.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        container = Path(df._expected_work_path(
            out["dfix"])).parent
        backup = container.with_name(container.name + ".bak")
        os.rename(container, backup)
        self.addCleanup(lambda: (
            container.unlink() if container.is_symlink() else None,
            os.rename(backup, container)
            if backup.exists() else None))
        elsewhere = Path(self.tmp.name) / "elsewhere-retry"
        elsewhere.mkdir(exist_ok=True)
        os.symlink(str(elsewhere), str(container))

        def _boom(*a, **k):
            raise AssertionError("no transport after ancestor swap")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_boom):
            with self.assertRaises(df.DerivedFixError) as cm:
                self._start(self.blocked_id, out["dfix"])
        self.assertEqual(cm.exception.code, "ancestor-replaced")
        rec = self._read(out["dfix"])
        # The carried attempt is untouched (still queued, never
        # rebound) and the suspect swap was never followed.
        self.assertEqual(rec["latest_attempt"]["id"],
                         started["attempt_id"])
        self.assertEqual(rec["latest_attempt"]["status"], "queued")
        self.assertTrue(container.is_symlink())

    def test_preparation_refusal_without_attempt_is_valid(self):
        # Same foreign-checkout setup as the prepare gate test: live
        # binding passes on identical SHAs, but the common-dir gate
        # refuses before any worktree or merge.
        clone = self.root / "home" / "clone-repo"
        git("clone", str(self.origin), str(clone), cwd=self.root)
        clone_wt = self.root / "home" / "clone-wt"
        git("worktree", "add", "--detach", str(clone_wt), self.base_evo,
            cwd=clone)
        self.bar_head = self.base_evo
        self.bar_snap = batches._snapshot(str(clone_wt))
        self.base_bar_wt = clone_wt
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        with self.assertRaises(df.DerivedFixError) as cm:
            self._start(self.blocked_id, out["dfix"])
        self.assertEqual(cm.exception.code, "bad-parent")
        # Serve-style refusal: a valid refused record with no
        # attempt, no job, no workspace — nothing fabricated.
        df.refuse_derived(out["dfix"], "prep failed")
        rec = self._read(out["dfix"])
        self.assertEqual(rec["status"], "refused")
        self.assertIsNone(rec["latest_attempt"])
        self.assertEqual(rec["source_attempts"], [])
        self.assertIsNone(rec["workspace"])
        self.assertIsNone(rec["job"])

    def _remove_fixture_receipt(self):
        # Hide the genuinely verified fixture receipt (restore in
        # cleanup): the gate must refuse without it, never invent
        # authority. Uses os.rename only — no new imports. The
        # fixture preflight home is OPENMAINTY_HOME (same idiom as
        # the serve start-refusal test below); there is no
        # self._home() in this mixin.
        preflight_dir = os.path.join(os.environ["OPENMAINTY_HOME"],
                                     "preflight")
        hidden = preflight_dir + ".hidden-bak"
        os.rename(preflight_dir, hidden)
        self.addCleanup(lambda: os.rename(hidden, preflight_dir)
                        if os.path.isdir(hidden)
                        and not os.path.exists(preflight_dir) else None)

    def test_start_refuses_without_preflight_receipt(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        before = self._read(out["dfix"])
        self.assertIsNone(before["workspace"])
        self._remove_fixture_receipt()

        def _boom(*a, **k):
            raise AssertionError("no transport without authority")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_boom):
            with self.assertRaises(df.DerivedFixError) as cm:
                self._start(self.blocked_id, out["dfix"])
        self.assertEqual(cm.exception.code, "no-preflight")
        after = self._read(out["dfix"])
        self.assertEqual(after["status"], before["status"])
        self.assertIsNone(after["workspace"])
        self.assertEqual(after["source_attempts"], [])
        self.assertIsNone(after["latest_attempt"])

    def test_worker_refuses_without_preflight_receipt(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        before = self._read(out["dfix"])
        self.assertEqual(before["latest_attempt"]["status"], "queued")
        self._remove_fixture_receipt()

        def _boom(*a, **k):
            raise AssertionError("no transport without authority")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_boom):
            with self.assertRaises(df.DerivedFixError) as cm:
                self._run_worker(self.blocked_id, out["dfix"],
                                started["attempt_id"])
        self.assertEqual(cm.exception.code, "no-preflight")
        after = self._read(out["dfix"])
        self.assertEqual(after["latest_attempt"]["status"], "queued")
        self.assertEqual(after["latest_attempt"]["id"],
                        started["attempt_id"])
        self.assertIsNone(after["workspace"]["result_head"])

    def test_orphan_queued_renders_unknown_then_reconciles(self):
        # A queued record with no job link is honestly queued
        # (absent link is not unknown, never idle). Once its link
        # names a dead owner it renders unknown. An explicit start
        # reconciles it into a real run with history preserved.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])

        def _shown(live):
            items = df.describe_derived(self.blocked_id, live)["derived"]
            return [d for d in items if d["dfix"] == out["dfix"]][0]

        self.assertEqual(_shown(set())["status"], "queued")
        df.note_job(out["dfix"],
                    {"id": "job-dead-9", "kind": "derived-fix",
                     "label": started["attempt_id"]})
        self.assertEqual(_shown(set())["status"], "unknown")
        # The unknown rendering is read-only: the record, the
        # queued attempt, and its dead link are all preserved.
        kept = self._read(out["dfix"])
        self.assertEqual(kept["latest_attempt"]["status"], "queued")
        self.assertEqual(kept["job"]["id"], "job-dead-9")
        # Reconciliation is the create live-orphan branch (never a
        # bare second start): the same key closes the dead queued
        # attempt as a preserved source-error terminal — its id,
        # label, and dead job survive in history — then an explicit
        # start opens the fresh attempt.
        again = self._create(self.blocked_id, *names, live_jobs={})
        self.assertTrue(again.get("reconciled"))
        self.assertEqual(again["dfix"], out["dfix"])
        closed = self._read(out["dfix"])
        self.assertEqual(closed["latest_attempt"]["id"],
                         started["attempt_id"])
        self.assertEqual(closed["latest_attempt"]["status"],
                         "source-error")
        self.assertEqual(closed["source_attempts"][0]["job"]["id"],
                         "job-dead-9")
        retry = self._start(self.blocked_id, out["dfix"])

        def _run(*a, **k):
            work = Path(self._read(out["dfix"])["workspace"]["path"])
            target = work / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  retry["attempt_id"])
        self.assertEqual(rc, 0)
        done = self._read(out["dfix"])
        self.assertEqual(done["status"], "source-ready-unverified")
        self.assertEqual(len(done["source_attempts"]), 2)
        # The reconciled orphan stays closed as source-error with
        # its id and dead job preserved — never re-queued, never
        # rebound.
        self.assertEqual(done["source_attempts"][0]["status"],
                         "source-error")
        self.assertEqual(done["source_attempts"][0]["id"],
                         started["attempt_id"])
        self.assertEqual(done["source_attempts"][0]["job"]["id"],
                         "job-dead-9")
        self.assertEqual(_shown(set())["status"],
                         "source-ready-unverified")

    def test_record_write_never_follows_temp_symlink(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        rec = self._read(out["dfix"])
        before = json.loads(df.derived_path(
            out["dfix"]).read_bytes().decode("utf-8"))
        doc = df.derived_path(out["dfix"])
        plant = doc.with_name(
            f"{doc.name}.{os.getpid()}."
            f"{threading.get_ident()}.0.tmp")
        elsewhere = doc.parent / "elsewhere.txt"
        elsewhere.write_text("planted\n", encoding="utf-8")
        os.symlink(str(elsewhere), str(plant))
        self.addCleanup(lambda: plant.unlink()
                        if plant.is_symlink() else None)
        # The exclusive create skips the planted symlink and
        # succeeds on a fresh temp: the foreign file is never
        # traversed, the planted link is never deleted, and no
        # real temp litter remains behind.
        df._write_record(rec)
        self.assertEqual(elsewhere.read_text(encoding="utf-8"),
                         "planted\n")
        self.assertTrue(plant.is_symlink())
        leftovers = [p for p in doc.parent.iterdir()
                     if p.name.startswith(doc.name + ".")
                     and p.name.endswith(".tmp")
                     and not p.is_symlink()]
        self.assertEqual(leftovers, [])
        reread = self._read(out["dfix"])
        self.assertEqual(reread["latest_attempt"],
                         before["latest_attempt"])

    def test_attempts_without_latest_pointer_corrupt(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        self._start(self.blocked_id, out["dfix"])
        doc = df.derived_path(out["dfix"])
        raw = json.loads(doc.read_bytes().decode("utf-8"))
        self.assertTrue(raw["source_attempts"])
        raw["latest_attempt"] = None
        doc.write_bytes(json.dumps(
            raw, ensure_ascii=False,
            sort_keys=True).encode("utf-8"))
        with self.assertRaises(df.DerivedFixError) as cm:
            df._read_record(out["dfix"])
        self.assertEqual(cm.exception.code, "corrupt")

    def test_tree_entry_state_absence_and_present(self):
        self.assertEqual(df._tree_entry_state(
            str(self.origin), self.cand_head,
            "example/webpack.config.js"), "present")
        self.assertEqual(df._tree_entry_state(
            str(self.origin), self.cand_head,
            "example/does-not-exist.js"), "absent")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._tree_entry_state(str(self.origin), "0" * 40,
                                 "example/webpack.config.js")
        self.assertEqual(cm.exception.code, "unreadable")

    def test_nonblob_pinned_entry_fails_closed(self):
        # A gitlink at an allowed path is present in the tree but
        # is not file bytes: absence is never claimed, reads fail
        # closed with the original unreadable semantics.
        git("update-index", "--add", "--cacheinfo", "160000",
            self.base_commit, "example/webpack.config.js",
            cwd=self.origin)
        git("commit", "-qm", "gitlink", cwd=self.origin)
        link_head = git("rev-parse", "HEAD", cwd=self.origin)
        self.assertEqual(df._tree_entry_state(
            str(self.origin), link_head,
            "example/webpack.config.js"), "present")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._pinned_blob_oid(str(self.origin), link_head,
                                "example/webpack.config.js")
        self.assertEqual(cm.exception.code, "unreadable")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._pinned_evidence(str(self.origin), link_head,
                                "candidate",
                                "example/webpack.config.js")
        self.assertEqual(cm.exception.code, "unreadable")

    def test_oversized_prep_identity_accepted_without_sha(self):
        # SCHEMA-SHAPE case only: this mutates stored metadata and
        # re-validates the shape (OID + size, no invented SHA). It
        # proves no statement about a real oversized blob object —
        # see the pinned-blob case below for actual size/hash proof.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        self._start(self.blocked_id, out["dfix"])
        doc = df.derived_path(out["dfix"])
        raw = json.loads(doc.read_bytes().decode("utf-8"))
        entry = raw["workspace"]["retained_base"][0]
        entry["size"] = df._WORKER_READ_MAX + 1
        entry["base_blob_sha256"] = None
        doc.write_bytes(json.dumps(
            raw, ensure_ascii=False,
            sort_keys=True).encode("utf-8"))
        # Honest oversized identity (OID + size, no invented SHA)
        # validates; the same entry WITH a sha refuses.
        rec = df._read_record(out["dfix"])
        kept = [e for e in rec["workspace"]["retained_base"]
                if e["path"] == entry["path"]][0]
        self.assertIsNone(kept["base_blob_sha256"])
        self.assertEqual(kept["size"], df._WORKER_READ_MAX + 1)
        raw["workspace"]["retained_base"][0][
            "base_blob_sha256"] = "f" * 64
        doc.write_bytes(json.dumps(
            raw, ensure_ascii=False,
            sort_keys=True).encode("utf-8"))
        with self.assertRaises(df.DerivedFixError) as cm:
            df._read_record(out["dfix"])
        self.assertEqual(cm.exception.code, "corrupt")

    def test_oversized_pinned_blob_keeps_oid_and_size_without_sha(self):
        # REAL oversized blob object: an actual synthetic commit
        # carries more than _WORKER_FILE_CAP bytes at an allowed
        # path. The pinned evidence keeps the true blob OID and the
        # true byte size, but never claims a computed raw SHA.
        big = b"z" * (df._WORKER_FILE_CAP + 100)
        target = self.origin / "example/webpack.config.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(big)
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "oversized fixture", cwd=self.origin)
        head = git("rev-parse", "HEAD", cwd=self.origin)
        ev = df._pinned_evidence(str(self.origin), head, "candidate",
                                 "example/webpack.config.js")
        self.assertEqual(ev["status"], "too-large")
        self.assertEqual(ev["size"], len(big))
        self.assertEqual(ev["omitted_bytes"], len(big))
        self.assertEqual(ev["blob_oid"],
                         git("rev-parse",
                             f"{head}:example/webpack.config.js",
                             cwd=self.origin))
        self.assertIsNone(ev["raw_sha256"])
        self.assertIsNone(ev["text"])

    def test_evidence_head_carries_full_identities(self):
        ev = df._pinned_evidence(str(self.origin), self.cand_head,
                                 "candidate",
                                 "example/webpack.config.js")
        self.assertEqual(ev["status"], "ok")
        head = df._render_evidence_head(ev)
        # Full identities travel in the prompt: the model cannot
        # derive a digest from a prefix.
        self.assertIn(self.cand_head, head)
        self.assertIn(ev["blob_oid"], head)
        self.assertIn(ev["raw_sha256"], head)
        self.assertEqual(len(ev["raw_sha256"]), 64)
        blocks, used = df._fit_evidence_blocks([ev], 10 ** 6)
        self.assertTrue(blocks[0].startswith(head))
        self.assertLessEqual(used, 10 ** 6)

    def test_lossy_window_refuses_truncation_but_passes_fitting(self):
        # Origin is left on main (no example/ dir there): create the
        # coherent owned path before writing bytes — never assume a
        # fixture identity that is not checked out.
        target = self.origin / "example/webpack.config.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xfe\x00binary\n" + b"x" * 100)
        git("add", "-A", cwd=self.origin)
        git("commit", "-qm", "binary", cwd=self.origin)
        head = git("rev-parse", "HEAD", cwd=self.origin)
        ev = df._pinned_evidence(str(self.origin), head, "candidate",
                                 "example/webpack.config.js")
        self.assertTrue(ev["lossy"])
        rendered = df._render_evidence_head(ev)
        self.assertIn(ev["raw_sha256"], rendered)
        blocks, _ = df._fit_evidence_blocks([ev], 10 ** 6)
        self.assertIn("대체 문자", blocks[0])
        # Fixed part fits but the lossy body does not: honest
        # refusal instead of a raw claim from display text.
        budget = len(rendered.encode("utf-8")) + 1 + 5
        with self.assertRaises(df.DerivedFixError) as cm:
            df._fit_evidence_blocks([ev], budget)
        self.assertEqual(cm.exception.code, "over-budget")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._fit_evidence_blocks([ev], 0)
        self.assertEqual(cm.exception.code, "over-budget")

    def test_eligibility_grounds_without_questions(self):
        # A valid needs_changes verdict over eligible code
        # conflicts is itself the repair request: no fabricated
        # question is required for the record to be created.
        df._check_eligibility(["example/webpack.config.js"],
                              {"report_status": "needs_changes",
                               "questions": []})
        with self.assertRaises(df.DerivedFixError) as cm:
            df._check_eligibility(["example/webpack.config.js"],
                                  {"report_status": "ready_for_derived",
                                   "questions": ["q"]})
        self.assertEqual(cm.exception.code, "not-eligible")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._check_eligibility(["example/dist/bundle.c.js"],
                                  {"report_status": "needs_changes",
                                   "questions": ["q"]})
        self.assertEqual(cm.exception.code, "not-eligible")

    def test_create_refuses_unrelated_conflicts_without_record(self):
        # A genuine unrelated chain: a fresh op whose RECORDED
        # conflicts are dist-only, carrying a real current valid
        # review. The create path itself must refuse before any
        # record, Git, or transport — no identity/current/source
        # validator mocks and no eligibility relaxation.
        op_id = "prep-issue-856-unrelated"
        self._done_derived_review(
            op_id, self.cand_head, ["example/dist/bundle.c.js"])
        names = self._origin_refs(op_id)
        op = json.loads(batches.op_file(op_id).read_bytes()
                        .decode("utf-8"))
        self.assertEqual(op["result"]["conflicted_paths"],
                         ["example/dist/bundle.c.js"])
        # Preconditions on the REAL immutable input: the dist path
        # is genuinely sent (not omitted) at the candidate ref with
        # the exact quoted line, so the canned response is grounded
        # in actual evidence and the review below is truly valid.
        st = ir.read_status(op_id)
        self.assertTrue(st["current"])
        sent = [f for f in st["input_files"]
                if f.get("path") == "example/dist/bundle.c.js"
                and f.get("ref") == self.cand_head]
        self.assertTrue(sent)
        self.assertIn("// dist bundle v0", sent[0].get("text") or "")
        with self.assertRaises(df.DerivedFixError) as cm:
            self._create(op_id, *names)
        self.assertEqual(cm.exception.code, "not-eligible")
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(df._scan_derived(op_id), [])

class TestDerivedServe(DerivedFixFixtureMixin):
    def _drain(self, jid, timeout=30.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            job = serve._JOBS.get(jid)
            if job is not None and job.get("status") != "running":
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {jid} still running")

    def _gate(self):
        gate = mock.patch.object(
            serve.chat, "subscription_error", return_value=None)
        gate.start()
        self.addCleanup(gate.stop)

    def _valid_proposal_stream(self):
        def _run(prompt, workdir=None, **k):
            target = Path(workdir) / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})
        return _run

    def test_serve_start_runs_job_to_terminal_ready(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        patcher = mock.patch(
            "openmainty.muse.exec_source_repair",
            side_effect=self._valid_proposal_stream())
        patcher.start()
        self.addCleanup(patcher.stop)
        out = serve.api_derived_fix_start(
            {"op": self.blocked_id, "confirm": True,
             "hypothesis": {"id": "H-A",
                            "note": "가설 메모: 베이스 유지, 후보 적용"},
             "context": {"input": names[0], "report": names[1],
                         "expected": {"input_sha256": names[2],
                                      "report_sha256": names[3]}}})
        self.assertTrue(out.get("started"))
        self.assertTrue(out.get("attempt"))
        self.assertIn("job", out)
        job = self._drain(out["job"]["id"])
        self.assertEqual(job.get("status"), "done")
        got = serve.api_derived_fix({"op": self.blocked_id})
        self.assertTrue(got["derived_allowed"])
        self.assertEqual(got["origin"]["input"], names[0])
        self.assertEqual(got["origin_shas"]["input_sha256"], names[2])
        items = [d for d in got["derived"]
                 if d["dfix"] == out["dfix"]]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "source-ready-unverified")
        self.assertEqual(items[0]["recorded_status"],
                         "source-ready-unverified")

    def test_serve_start_rejects_stale_expected(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_start(
                {"op": self.blocked_id, "confirm": True,
                 "hypothesis": {"id": "H-A", "note": "n"},
                 "context": {"input": names[0], "report": names[1],
                             "expected": {"input_sha256": "0" * 64,
                                          "report_sha256": names[3]}}})
        self.assertEqual(cm.exception.status, 409)

    def test_serve_cancel_strict_owner(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        # No live attempt and no job link: honest no-op, no signal.
        got = serve.api_derived_fix_cancel({"dfix": out["dfix"],
                                            "confirm": True})
        self.assertFalse(got.get("cancelled"))
        started = self._start(self.blocked_id, out["dfix"])
        df.note_job(out["dfix"],
                    {"id": "owned-9", "kind": "derived-fix",
                     "label": started["attempt_id"]})
        # A foreign TABLE kind must never be signaled: the stored
        # link is real, but the live entry is not a derived-fix job.
        serve._JOBS["owned-9"] = {"id": "owned-9",
                                  "kind": "recovery-review",
                                  "label": started["attempt_id"],
                                  "status": "running"}
        serve._JOB_CANCEL["owned-9"] = threading.Event()
        try:
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix_cancel({"dfix": out["dfix"],
                                              "confirm": True})
            self.assertEqual(cm.exception.status, 409)
            self.assertFalse(
                serve._JOB_CANCEL["owned-9"].is_set())
        finally:
            serve._JOBS.pop("owned-9", None)
            serve._JOB_CANCEL.pop("owned-9", None)
        # note_job itself refuses foreign kinds and wrong labels:
        # ownership is constructed, never claimed.
        with self.assertRaises(df.DerivedFixError):
            df.note_job(out["dfix"], {"id": "foreign-1",
                                      "kind": "recovery-review",
                                      "label": started["attempt_id"]})
        with self.assertRaises(df.DerivedFixError):
            df.note_job(out["dfix"], {"id": "owned-9",
                                      "kind": "derived-fix",
                                      "label": "da-" + "0" * 16})
        # The owned live job signals exactly its own event.
        serve._JOBS["owned-9"] = {"id": "owned-9",
                                  "kind": "derived-fix",
                                  "label": started["attempt_id"],
                                  "status": "running"}
        serve._JOB_CANCEL["owned-9"] = threading.Event()
        try:
            got = serve.api_derived_fix_cancel({"dfix": out["dfix"],
                                                "confirm": True})
            self.assertTrue(got.get("cancelled"))
            self.assertTrue(serve._JOB_CANCEL["owned-9"].is_set())
        finally:
            serve._JOBS.pop("owned-9", None)
            serve._JOB_CANCEL.pop("owned-9", None)

    def test_serve_start_refuses_when_foreign_job_running(self):
        # The single-worker guard: a live foreign job refuses slot
        # allocation with 409 BEFORE any link write. This is why the
        # binding-failure fixture must use a terminal foreign entry —
        # a running foreign entry can never reach that boundary.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        serve._JOBS["foreign-busy"] = {
            "id": "foreign-busy", "kind": "recovery-review",
            "label": "da-" + "0" * 16, "status": "running", "rc": None,
            "started_at": "2026-10-29T23:00:00+00:00",
            "ended_at": None, "note": ""}
        serve._JOB_CANCEL["foreign-busy"] = threading.Event()
        self.addCleanup(lambda: (
            serve._JOBS.pop("foreign-busy", None),
            serve._JOB_CANCEL.pop("foreign-busy", None)))
        before_jobs = set(serve._JOBS)
        self.addCleanup(lambda: [
            serve._JOBS.pop(j, None)
            for j in set(serve._JOBS) - before_jobs])
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_start(
                {"op": self.blocked_id, "confirm": True,
                 "hypothesis": {
                     "id": "H-A",
                     "note": "가설 메모: 베이스 유지, 후보 적용"},
                 "context": {
                     "input": names[0], "report": names[1],
                     "expected": {"input_sha256": names[2],
                                  "report_sha256": names[3]}}})
        self.assertEqual(cm.exception.status, 409)
        # No new slot allocated; the foreign job still runs and was
        # never signaled; its queued attempt is preserved as a
        # truthful startup-failed terminal, never a phantom run.
        self.assertEqual(set(serve._JOBS) - before_jobs, set())
        self.assertEqual(serve._JOBS["foreign-busy"]["status"],
                         "running")
        self.assertFalse(
            serve._JOB_CANCEL["foreign-busy"].is_set())
        # _scan_derived returns validated record dicts, not ids:
        # assert exactly one parent-bound record, then re-read it
        # from disk by op_id for the terminal assertions.
        found = df._scan_derived(self.blocked_id)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["derived_from"], self.blocked_id)
        rec = self._read(found[0]["op_id"])
        self.assertEqual(rec["status"], "source-error")
        self.assertEqual(len(rec["source_attempts"]), 1)
        self.assertEqual(rec["source_attempts"][0]["status"],
                         "source-error")
        self.assertIn("no dashboard job slot",
                      rec["source_attempts"][0]["error"])

    def test_serve_start_refuses_without_preflight_receipt(self):
        # Fresh authority is required for NEW spawn/mutation only: an
        # existing live worker still attaches (covered separately),
        # but a start with no receipt refuses 409 with no lease, no
        # record, no job, and no transport.
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        preflight_dir = Path(os.environ["OPENMAINTY_HOME"]) / "preflight"
        hidden = preflight_dir.with_name("preflight.hidden-bak")
        os.rename(preflight_dir, hidden)
        self.addCleanup(lambda: os.rename(hidden, preflight_dir)
                        if hidden.is_dir()
                        and not preflight_dir.exists() else None)
        before_jobs = set(serve._JOBS)
        self.addCleanup(lambda: [
            serve._JOBS.pop(j, None)
            for j in set(serve._JOBS) - before_jobs])
        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=AssertionError(
                            "no transport without authority")):
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix_start(
                    {"op": self.blocked_id, "confirm": True,
                     "hypothesis": {
                         "id": "H-A",
                         "note": "가설 메모: 베이스 유지, 후보 적용"},
                     "context": {
                         "input": names[0], "report": names[1],
                         "expected": {"input_sha256": names[2],
                                      "report_sha256": names[3]}}})
        # No verified receipt: the product refuses 404 no-preflight
        # (nothing to authorize with), holding no lease and writing
        # no record, job, or transport.
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(set(serve._JOBS), before_jobs)
        self.assertEqual(df._scan_derived(self.blocked_id), [])

    def test_serve_live_duplicate_attaches_while_lease_held(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        release = threading.Event()
        self.addCleanup(release.set)

        def _run(prompt, workdir=None, **k):
            target = Path(workdir) / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            if not release.wait(timeout=30):
                raise AssertionError("worker not released")
            raw = target.read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        patcher = mock.patch("openmainty.muse.exec_source_repair",
                             side_effect=_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        payload = {"op": self.blocked_id, "confirm": True,
                   "hypothesis": {
                       "id": "H-A",
                       "note": "가설 메모: 베이스 유지, 후보 적용"},
                   "context": {"input": names[0], "report": names[1],
                               "expected": {
                                   "input_sha256": names[2],
                                   "report_sha256": names[3]}}}
        before_jobs = set(serve._JOBS)
        out = serve.api_derived_fix_start(dict(payload))
        self.addCleanup(lambda: [
            serve._JOBS.pop(j, None)
            for j in set(serve._JOBS) - before_jobs])
        self.assertTrue(out.get("started"))
        jid = out["job"]["id"]
        self.assertEqual(out["job"]["kind"], "derived-fix")
        self.assertEqual(out["job"]["label"], out["attempt"])
        # GET shows the exact running record with the real link.
        got = serve.api_derived_fix({"op": self.blocked_id})
        live = [d for d in got["derived"]
                if d["dfix"] == out["dfix"]]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["status"], "source-running")
        self.assertEqual(live[0]["job"]["id"], jid)
        self.assertEqual(live[0]["job"]["label"], out["attempt"])
        # The duplicate start arrives while the first job still
        # owns the worker lease: it attaches (exact union) instead
        # of 409ing as foreign-busy — no lease is acquired for this.
        dup = serve.api_derived_fix_start(dict(payload))
        self.assertFalse(dup.get("started"))
        self.assertTrue(dup.get("reused"))
        self.assertTrue(dup.get("attached"))
        self.assertEqual(dup["job"]["id"], jid)
        self.assertEqual(dup["job"]["kind"], "derived-fix")
        self.assertEqual(dup["job"]["label"], out["attempt"])
        # Cancel signals only the owned event; a foreign event is
        # never touched.
        serve._JOBS["foreign-e2e"] = {
            "id": "foreign-e2e", "kind": "recovery-review",
            "label": "da-" + "0" * 16, "status": "running"}
        serve._JOB_CANCEL["foreign-e2e"] = threading.Event()
        self.addCleanup(lambda: (
            serve._JOBS.pop("foreign-e2e", None),
            serve._JOB_CANCEL.pop("foreign-e2e", None)))
        cancelled = serve.api_derived_fix_cancel(
            {"dfix": out["dfix"], "confirm": True})
        self.assertTrue(cancelled.get("cancelled"))
        self.assertFalse(
            serve._JOB_CANCEL["foreign-e2e"].is_set())
        # Release the worker: the owned cancellation reconciles to
        # a terminal record and the worker lease is freed.
        release.set()
        job = self._drain(jid)
        self.assertNotEqual(job.get("status"), "running")
        term = serve.api_derived_fix({"op": self.blocked_id})
        items = [d for d in term["derived"]
                 if d["dfix"] == out["dfix"]]
        self.assertEqual(items[0]["status"], "cancelled")
        token = sched.acquire_worker_lease("probe-after-cancel")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)

    def test_serve_binding_failure_terminalizes_job_and_releases(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        before_jobs = set(serve._JOBS)
        # Cleanup is registered BEFORE the start call, so every
        # later assertion below is reached and no job leaks even
        # when an early assertion fails.
        self.addCleanup(lambda: [
            (serve._JOBS.pop(j, None), serve._JOB_CANCEL.pop(j, None))
            for j in set(serve._JOBS) - before_jobs])
        # A terminal foreign entry the failure must never touch. It
        # is deliberately NOT running: _job_new refuses ANY running
        # job (single worker), so a live foreign entry would 409
        # before the link-write boundary is ever reached — that
        # guard has its own test below.
        foreign = {"id": "foreign-f1", "kind": "recovery-review",
                   "label": "da-" + "0" * 16, "status": "done",
                   "rc": 0, "started_at": "2026-10-29T23:00:00+00:00",
                   "ended_at": "2026-10-29T23:00:01+00:00",
                   "note": "done"}
        serve._JOBS["foreign-f1"] = dict(foreign)
        self.addCleanup(lambda: (
            serve._JOBS.pop("foreign-f1", None),
            serve._JOB_CANCEL.pop("foreign-f1", None)))
        real_replace = os.replace
        link_faults = []

        def _fail_link(src, dst):
            # Environment fault-injection at the persistence
            # boundary (no product logic is mocked): only the
            # job-link write fails, as if the disk refused it.
            # Every validator, gate, and cleanup runs for real.
            try:
                data = Path(src).read_bytes()
            except OSError:
                data = b""
            if b'"job": {"id"' in data:
                link_faults.append(1)
                raise OSError("simulated link-write failure")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=_fail_link):
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix_start(
                    {"op": self.blocked_id, "confirm": True,
                     "hypothesis": {
                         "id": "H-A",
                         "note": "가설 메모: 베이스 유지, 후보 적용"},
                     "context": {
                         "input": names[0], "report": names[1],
                         "expected": {"input_sha256": names[2],
                                      "report_sha256": names[3]}}})
        # Truthful typed failure, not magic 500: _write_record wraps
        # the refused os.replace as DerivedFixError(502/unreadable)
        # and the serve layer preserves that exact status (ApiError
        # carries .status only, never .http_status).
        # Storage/SourceUnreadable semantics are unchanged.
        self.assertEqual(cm.exception.status, 502)
        fresh = [serve._JOBS[j] for j in set(serve._JOBS)
                 - before_jobs
                 if isinstance(serve._JOBS.get(j), dict)
                 and serve._JOBS[j].get("kind") == "derived-fix"]
        # The new slot is terminal (never stuck running) with a
        # truthful reason.
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].get("status"), "done")
        self.assertEqual(fresh[0].get("rc"), 1)
        self.assertIn("job link", fresh[0].get("note") or "")
        # The queued attempt closed as a preserved terminal.
        got = serve.api_derived_fix({"op": self.blocked_id})
        self.assertEqual(len(got["derived"]), 1)
        self.assertEqual(got["derived"][0]["status"],
                         "source-error")
        attempts = got["derived"][0]["attempts"]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "source-error")
        self.assertIn("job link", attempts[0]["error"])
        # The exact owner lease is released: a fresh acquire works.
        token = sched.acquire_worker_lease("probe-after-failure")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)
        # The intended os.replace boundary actually faulted — and
        # exactly once: no silent pass, no repeated half-writes.
        self.assertEqual(len(link_faults), 1)
        # The terminal foreign entry is untouched byte-for-byte, and
        # no cancel event was manufactured for it.
        self.assertEqual(serve._JOBS["foreign-f1"], foreign)
        self.assertNotIn("foreign-f1", serve._JOB_CANCEL)

    def test_serve_cancel_refuses_foreign_table_identity(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        df.note_job(out["dfix"],
                    {"id": "job-table-1", "kind": "derived-fix",
                     "label": started["attempt_id"]})
        try:
            # Foreign label in the live table: not our attempt,
            # never signaled.
            serve._JOBS["job-table-1"] = {
                "id": "job-table-1", "kind": "derived-fix",
                "label": "da-" + "9" * 16, "status": "running"}
            serve._JOB_CANCEL["job-table-1"] = threading.Event()
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix_cancel({"dfix": out["dfix"],
                                              "confirm": True})
            self.assertEqual(cm.exception.status, 409)
            self.assertFalse(
                serve._JOB_CANCEL["job-table-1"].is_set())
            # Unknown id (entry vanished): never signal the
            # leftover event, never claim success.
            serve._JOBS.pop("job-table-1", None)
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix_cancel({"dfix": out["dfix"],
                                              "confirm": True})
            self.assertEqual(cm.exception.status, 409)
            self.assertFalse(
                serve._JOB_CANCEL["job-table-1"].is_set())
        finally:
            serve._JOBS.pop("job-table-1", None)
            serve._JOB_CANCEL.pop("job-table-1", None)

    def test_serve_clean_retry_runs_to_ready(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        self._gate()
        before_jobs = set(serve._JOBS)
        self.addCleanup(lambda: [
            serve._JOBS.pop(j, None)
            for j in set(serve._JOBS) - before_jobs])
        calls = []

        def _run(prompt, workdir=None, **k):
            calls.append(1)
            if len(calls) == 1:
                return (1, "boom with no edits", {"m": 0})
            target = Path(workdir) / "example/webpack.config.js"
            target.write_text(target.read_text(encoding="utf-8")
                              .replace("// candidate region: entry",
                                       "// worker resolution H-A"),
                              encoding="utf-8")
            raw = target.read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        patcher = mock.patch("openmainty.muse.exec_source_repair",
                             side_effect=_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        payload = {"op": self.blocked_id, "confirm": True,
                   "hypothesis": {
                       "id": "H-A",
                       "note": "가설 메모: 베이스 유지, 후보 적용"},
                   "context": {"input": names[0], "report": names[1],
                               "expected": {
                                   "input_sha256": names[2],
                                   "report_sha256": names[3]}}}
        out1 = serve.api_derived_fix_start(dict(payload))
        self.assertTrue(out1.get("started"))
        job1 = self._drain(out1["job"]["id"])
        self.assertEqual(job1.get("status"), "done")
        got1 = serve.api_derived_fix({"op": self.blocked_id})
        self.assertEqual(got1["derived"][0]["status"],
                         "source-error")
        # Explicit retry through the real lifecycle: the failed
        # attempt keeps its job metadata, the new job binds the
        # new attempt, and the run settles source-ready-unverified.
        payload["retry_of"] = out1["dfix"]
        out2 = serve.api_derived_fix_start(dict(payload))
        self.assertTrue(out2.get("started"))
        self.assertNotEqual(out2["attempt"], out1["attempt"])
        self.assertEqual(out2["job"]["label"], out2["attempt"])
        job2 = self._drain(out2["job"]["id"])
        self.assertEqual(job2.get("status"), "done")
        self.assertEqual(job2.get("rc"), 0)
        got2 = serve.api_derived_fix({"op": self.blocked_id})
        item = got2["derived"][0]
        self.assertEqual(item["status"], "source-ready-unverified")
        self.assertEqual(len(calls), 2)
        rec = self._read(out1["dfix"])
        self.assertEqual(len(rec["source_attempts"]), 2)
        self.assertEqual(rec["source_attempts"][0]["job"]["id"],
                         out1["job"]["id"])
        self.assertEqual(rec["job"]["id"], out2["job"]["id"])
        self.assertEqual(rec["job"]["label"], out2["attempt"])
        token = sched.acquire_worker_lease("probe-after-retry")
        self.assertIsNotNone(token)
        sched.release_worker_lease(token)

    def test_describe_healthy_empty_and_corrupt(self):
        self._write_derived_blocked_op(
            ["example/webpack.config.js"], self.cand_head,
            self.bar_head)
        got = serve.api_derived_fix({"op": self.blocked_id})
        self.assertFalse(got["derived_allowed"])
        self.assertIsNone(got["origin"])
        self.assertEqual(got["derived"], [])
        # Corrupt namespace member: fail closed, never an empty view.
        ops = batches.integration_ops_dir()
        (ops / "dfix-zz-not-hex.json").write_text("{}\n",
                                                 encoding="utf-8")
        try:
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix({"op": self.blocked_id})
            self.assertEqual(cm.exception.status, 502)
        finally:
            (ops / "dfix-zz-not-hex.json").unlink()


class TestFullInputSidecar(DerivedFixFixtureMixin):
    """Stage2A immutable app-sent input: publish/verify/history/view.

    Owned temp home + synthetic git only. The ONLY stubbed
    boundaries are the model transports (recovery review keeps
    openmainty.muse.exec_prompt; worker/start paths use
    openmainty.muse.exec_source_repair with admission_kind="target")
    and, in three fault tests, a one-shot stdlib I/O fault at the
    persistence/read boundary (os.replace / os.fstat / planted
    files) — the established environment fault-injection pattern.
    No validator, identity, current-proof, or source helper is
    ever patched; no real model, network, home, or target writes.
    """

    def _home(self):
        return Path(os.environ["OPENMAINTY_HOME"])

    def _leaf(self, dfix, att):
        return (self._home() / "derived" / dfix / "full-input"
                / (att + ".json"))

    def _tree(self):
        out = {}
        for root, _ds, fs in os.walk(self._home()):
            for name in fs:
                path = Path(root) / name
                out[str(path.relative_to(self._home()))] = (
                    path.stat().st_size)
        return out

    def _edit_single_line(self, work):
        target = work / "example/webpack.config.js"
        target.write_text(target.read_text(encoding="utf-8").replace(
            "// candidate region: entry", "// worker resolution H-A"),
            encoding="utf-8")
        return {"example/webpack.config.js"}

    def _capture_run(self, op_id, edit_fn=None, resolutions=None):
        """Full review->create->start->worker with transport capture.

        Returns (dfix, attempt, seen) where seen holds the EXACT
        prompt object handed to transport plus its kwargs.
        """
        self._done_derived_review(op_id, self.cand_head)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        started = self._start(op_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])
        seen = {"calls": 0}

        def _run(prompt, workdir=None, **kw):
            seen["calls"] += 1
            seen["prompt"] = prompt
            seen["kwargs"] = dict(kw)
            seen["workdir"] = workdir
            changed = (edit_fn or self._edit_single_line)(work)
            hashes = {}
            for rel in sorted(changed):
                raw = (work / rel).read_bytes()
                hashes[rel] = hashlib.sha256(raw).hexdigest()
            return (0, _worker_jsonl(_proposal(
                sorted(changed), hashes,
                resolutions if resolutions is not None else [{
                    "path": "example/webpack.config.js",
                    "conflict_id": "example/webpack.config.js",
                    "choice": "H-A"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            rc = self._run_worker(op_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 0)
        return out["dfix"], started["attempt_id"], seen

    def _pristine(self, dfix, att):
        raw = self._leaf(dfix, att).read_bytes()
        rec_raw = df._checked_derived_file(
            df.derived_path(dfix)).read_bytes()
        return raw, rec_raw

    def _restore(self, dfix, att, raw, rec_raw):
        self._leaf(dfix, att).write_bytes(raw)
        df._checked_derived_file(
            df.derived_path(dfix)).write_bytes(rec_raw)

    def _mutate_leaf(self, dfix, att, fn):
        raw = self._leaf(dfix, att).read_bytes()
        obj = json.loads(raw.decode("utf-8"))
        fn(obj)
        self._leaf(dfix, att).write_bytes(
            df._serialize_full_input(obj))

    def test_exact_bytes_pin_and_model(self):
        dfix, att, seen = self._capture_run(self.blocked_id)
        got = df.describe_full_input(self.blocked_id, dfix, att)
        self.assertEqual(got["status"], "stored")
        # The stored text IS the dispatched object, byte for byte.
        self.assertIsInstance(seen["prompt"], str)
        self.assertEqual(got["text"], seen["prompt"])
        blob = seen["prompt"].encode("utf-8")
        self.assertEqual(got["prompt_bytes"], len(blob))
        self.assertEqual(got["prompt_sha256"],
                         hashlib.sha256(blob).hexdigest())
        # The independent pin binds the exact published file bytes.
        file_raw = self._leaf(dfix, att).read_bytes()
        self.assertEqual(got["pin_bytes"], len(file_raw))
        self.assertEqual(got["pin_sha256"],
                         hashlib.sha256(file_raw).hexdigest())
        rec = self._read(dfix)
        attempt = next(a for a in rec["source_attempts"]
                       if a.get("id") == att)
        self.assertEqual(attempt["prompt_sha256"],
                         got["prompt_sha256"])
        self.assertEqual(attempt["full_input"]["sha256"],
                         got["pin_sha256"])
        self.assertEqual(attempt["full_input"]["bytes"],
                         got["pin_bytes"])
        # Dedicated admission end to end, never a generic model
        # kwarg: the worker calls exec_source_repair WITHOUT a
        # caller-supplied model (the dedicated constructor pins
        # muse-spark-1.3 itself), but WITH admission_kind="target".
        # The exact model invariant lives in the stored profile.
        self.assertEqual(seen["kwargs"].get("admission_kind"),
                         "target")
        self.assertNotIn("model", seen["kwargs"])
        self.assertEqual(got["profile"]["model"], "muse-spark-1.3")
        self.assertEqual(got["profile"]["argv_state"], "requested")
        self.assertEqual(seen["workdir"],
                         os.path.realpath(rec["workspace"]["path"]))
        # No secret material is smuggled into the prompt.
        self.assertNotIn("META_API_KEY", got["text"])
        # Honest empty omission set on the happy path.
        self.assertEqual(got["omitted"], [])
        self.assertEqual(got["questions"]["total"], 2)
        self.assertEqual(got["questions"]["sent"], 2)
        self.assertEqual(got["questions"]["omitted"], 0)
        self.assertEqual(got["questions"]["clipped"], 0)
        self.assertEqual(got["questions"]["clipped_bytes"], 0)

    def test_publish_failure_means_no_transport(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        # Plant a regular FILE where the container must be: publish
        # fails before any write, deterministically, no mocks.
        container = (self._home() / "derived" / out["dfix"]
                     / "full-input")
        container.write_text("planted blocker", encoding="utf-8")
        seen = {"calls": 0}

        def _never(prompt, workdir=None, **kw):
            seen["calls"] += 1
            raise AssertionError("transport must never run")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_never):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        self.assertEqual(seen["calls"], 0)
        rec = self._read(out["dfix"])
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["status"], "source-error")
        self.assertTrue(attempt["error"])
        self.assertIsNone(attempt.get("full_input"))
        got = df.describe_full_input(
            self.blocked_id, out["dfix"], started["attempt_id"])
        self.assertEqual(got["status"], "pre-input-failure")
        self.assertTrue(got["error"])

    def test_pin_store_failure_means_no_transport(self):
        dfix, att, seen = self._capture_run(self.blocked_id)
        del seen, att
        # Second lifecycle on a fresh op: fail exactly the pin-store
        # record write (first record JSON carrying a full_input pin),
        # then let the terminal write through.
        op_id = "prep-issue-856-pin-store-fault"
        self._done_derived_review(op_id, self.cand_head)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        started = self._start(op_id, out["dfix"])
        real_replace = os.replace
        fired = []
        calls = {"n": 0}

        def _fail_pin_once(src, dst):
            calls["n"] += 1
            if not fired:
                try:
                    data = Path(src).read_bytes()
                except OSError:
                    data = b""
                if b'"full_input": {"bytes"' in data:
                    fired.append(1)
                    raise OSError("simulated pin-store failure")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=_fail_pin_once):
            with mock.patch("openmainty.muse.exec_source_repair",
                            side_effect=AssertionError(
                                "transport must never run")):
                rc = self._run_worker(op_id, out["dfix"],
                                      started["attempt_id"])
        self.assertEqual(rc, 1)
        self.assertEqual(fired, [1])
        rec = self._read(out["dfix"])
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["status"], "source-error")
        self.assertTrue(attempt["error"])
        # The published artifact survived as failure evidence even
        # though its pin write faulted.
        leaf = self._leaf(out["dfix"], started["attempt_id"])
        self.assertTrue(leaf.stat().st_size > 0)

    def test_tamper_variants_refuse(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        try:
            # Canonical re-serialization is byte-stable.
            self.assertEqual(
                df._serialize_full_input(
                    json.loads(raw.decode("utf-8"))), raw)

            def _flip_text(obj):
                obj["prompt_text"] = obj["prompt_text"] + " "

            self._restore(dfix, att, raw, rec_raw)
            self._mutate_leaf(dfix, att, _flip_text)
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "corrupt")

            def _coordinated(obj):
                obj["prompt_text"] = obj["prompt_text"] + " "
                blob = obj["prompt_text"].encode("utf-8")
                obj["prompt_bytes"] = len(blob)
                obj["prompt_sha256"] = hashlib.sha256(blob).hexdigest()

            self._restore(dfix, att, raw, rec_raw)
            self._mutate_leaf(dfix, att, _coordinated)
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "corrupt")

            def _metadata(obj):
                obj["conflicts"] = obj["conflicts"] + ["evil.js"]

            self._restore(dfix, att, raw, rec_raw)
            self._mutate_leaf(dfix, att, _metadata)
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "corrupt")
        finally:
            self._restore(dfix, att, raw, rec_raw)

    def test_duplicate_and_unknown_keys_refuse(self):
        with self.assertRaises(df.DerivedFixError) as cm:
            df._loads_strict(b'{"a": 1, "a": 2}')
        self.assertEqual(cm.exception.code, "corrupt")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._loads_strict(b'[1, 2]')
        self.assertEqual(cm.exception.code, "corrupt")
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        try:
            def _extra_top(obj):
                obj["zz_unknown"] = 1

            self._mutate_leaf(dfix, att, _extra_top)
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "corrupt")

            def _extra_profile(obj):
                obj["profile"]["zz_unknown"] = "x"

            self._restore(dfix, att, raw, rec_raw)
            self._mutate_leaf(dfix, att, _extra_profile)
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "corrupt")
        finally:
            self._restore(dfix, att, raw, rec_raw)

    def test_historical_read_after_retry(self):
        op_id = "prep-issue-856-history-retry"
        self._done_derived_review(op_id, self.cand_head)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        first = self._start(op_id, out["dfix"])
        only_delta = json.dumps({"payload": {
            "kind": "run_output_delta", "text": "investigating"}})
        seen1 = {}

        def _fail(prompt, workdir=None, **kw):
            seen1["prompt"] = prompt
            return (0, only_delta, {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_fail):
            rc = self._run_worker(op_id, out["dfix"],
                                  first["attempt_id"])
        self.assertEqual(rc, 1)
        retried = self._create(op_id, *names, retry_of=out["dfix"])
        self.assertTrue(retried.get("retried"))
        second = self._start(op_id, out["dfix"])
        seen2 = {}

        def _succeed(prompt, workdir=None, **kw):
            seen2["prompt"] = prompt
            target = Path(workdir) / "example/webpack.config.js"
            target.write_text(target.read_text(
                encoding="utf-8").replace(
                    "// candidate region: entry",
                    "// worker resolution H-A"), encoding="utf-8")
            raw = target.read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_succeed):
            rc = self._run_worker(op_id, out["dfix"],
                                  second["attempt_id"])
        self.assertEqual(rc, 0)
        rec = self._read(out["dfix"])
        self.assertEqual(rec["latest_attempt"]["id"],
                         second["attempt_id"])
        # The FIRST attempt stays readable by its own id after the
        # pointer moved on: exact original bytes, failed status kept.
        # Distinct attempts legitimately carry byte-identical prompts
        # when the builder input did not change, so per-attempt
        # identity (not prompt inequality) is the assertion: each
        # attempt binds its own recorded pin, the old text equals the
        # exact captured prompt, and the latest pointer moved.
        old = df.describe_full_input(
            op_id, out["dfix"], first["attempt_id"])
        self.assertEqual(old["status"], "stored")
        self.assertEqual(old["text"], seen1["prompt"])
        self.assertEqual(
            old["prompt_sha256"],
            hashlib.sha256(
                seen1["prompt"].encode("utf-8")).hexdigest())
        new = df.describe_full_input(
            op_id, out["dfix"], second["attempt_id"])
        self.assertEqual(new["status"], "stored")
        self.assertEqual(new["text"], seen2["prompt"])
        self.assertNotEqual(first["attempt_id"],
                            second["attempt_id"])
        by_id = {a["id"]: a for a in rec["source_attempts"]}
        self.assertIn(first["attempt_id"], by_id)
        self.assertIn(second["attempt_id"], by_id)
        self.assertEqual(
            old["pin_sha256"],
            by_id[first["attempt_id"]]["full_input"]["sha256"])
        self.assertEqual(
            new["pin_sha256"],
            by_id[second["attempt_id"]]["full_input"]["sha256"])

    def test_missing_file_and_legacy(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        before = self._tree()
        try:
            self._leaf(dfix, att).unlink()
            with self.assertRaises(df.DerivedFixError) as cm:
                df.describe_full_input(self.blocked_id, dfix, att)
            self.assertEqual(cm.exception.code, "missing-required")
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "missing-required")
            # A legacy record (pin key absent) reports legacy, and
            # the read changes no bytes on disk.
            rec = json.loads(rec_raw.decode("utf-8"))
            for attempt in rec["source_attempts"]:
                attempt.pop("full_input", None)
            df._write_record(df._validate_derived_record(rec))
            snap = self._read(dfix)
            self.assertTrue(all("full_input" not in a
                                for a in snap["source_attempts"]))
            mid = self._tree()
            got = df.describe_full_input(self.blocked_id, dfix, att)
            self.assertEqual(got["status"], "legacy-not-recorded")
            self.assertEqual(self._tree(), mid)
        finally:
            self._restore(dfix, att, raw, rec_raw)
        self.assertEqual(self._tree(), before)

    def test_null_pin_live_reports_not_yet_dispatched(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])
        got = df.describe_full_input(
            self.blocked_id, out["dfix"], started["attempt_id"])
        self.assertEqual(got["status"], "not-yet-dispatched")

    def _prepared_publish_args(self, op_id):
        """Real builder output for the live queued attempt."""
        self._done_derived_review(op_id, self.cand_head)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        started = self._start(op_id, out["dfix"])
        rec = self._read(out["dfix"])
        ws = rec["workspace"]
        proof = df._pre_transport_proof(rec, ws)
        prompt, info = df._build_worker_prompt(
            rec, proof["repo_dir"], proof["cur"])
        attempt = next(a for a in rec["source_attempts"]
                       if a.get("id") == started["attempt_id"])
        return out["dfix"], attempt, prompt, info, ws, rec

    def test_eexist_identical_confirms_differing_refuses(self):
        dfix, attempt, prompt, info, ws, rec = (
            self._prepared_publish_args(self.blocked_id))
        pin1 = df._publish_full_input(
            dfix, attempt, prompt, info, ws, rec)
        first = self._leaf(dfix, attempt["id"]).read_bytes()
        pin2 = df._publish_full_input(
            dfix, attempt, prompt, info, ws, rec)
        # Identical bytes confirm the stored artifact: same pin,
        # same file, and never a dispatch excuse on their own.
        self.assertEqual(pin1, pin2)
        self.assertEqual(
            self._leaf(dfix, attempt["id"]).read_bytes(), first)
        tampered = first + b"\n# tampered\n"
        self._leaf(dfix, attempt["id"]).write_bytes(tampered)
        with self.assertRaises(df.DerivedFixError) as cm:
            df._publish_full_input(
                dfix, attempt, prompt, info, ws, rec)
        self.assertEqual(cm.exception.code, "duplicate-conflict")
        # Differing bytes refuse; the existing file is untouched.
        self.assertEqual(
            self._leaf(dfix, attempt["id"]).read_bytes(), tampered)

    def test_no_transport_after_terminal(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        calls = {"n": 0}

        def _count(prompt, workdir=None, **kw):
            calls["n"] += 1
            raise AssertionError("terminal rerun must not dispatch")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_count):
            with self.assertRaises(df.DerivedFixError) as cm:
                self._run_worker(self.blocked_id, dfix, att)
        self.assertEqual(cm.exception.code, "busy")
        self.assertEqual(calls["n"], 0)

    def test_size_and_surrogate_boundaries(self):
        # An over-budget prompt refuses AT PUBLICATION — the prompt
        # cap is enforced before any directory creation: no file, no
        # pin, no unintended mkdir. The raw serializer enforces only
        # the larger raw JSON cap, so the wrong layer cannot prove
        # the dispatch budget.
        dfix0, attempt0, _p0, info0, ws0, rec0 = (
            self._prepared_publish_args("prep-issue-856-size-cap"))
        before_pub = self._tree()
        with self.assertRaises(df.DerivedFixError) as cm:
            df._publish_full_input(
                dfix0, attempt0,
                "x" * (df._WORKER_PROMPT_CAP + 1),
                info0, ws0, rec0)
        self.assertEqual(cm.exception.code, "over-budget")
        self.assertEqual(self._tree(), before_pub)
        # The legal worst-escape prompt (every byte expands 6x)
        # still fits the adequate raw cap with real metadata room.
        worst = ""
        raw = df._serialize_full_input(
            {"prompt_text": worst * df._WORKER_PROMPT_CAP,
             "pad": "y" * 4096})
        self.assertLessEqual(len(raw), df._FULL_INPUT_CAP)
        # Lone surrogates are typed corrupt, never a 500 and never
        # silently replaced — on write and on read.
        with self.assertRaises(df.DerivedFixError) as cm:
            df._serialize_full_input({"prompt_text": "a\ud800b"})
        self.assertEqual(cm.exception.code, "corrupt")
        with self.assertRaises(df.DerivedFixError) as cm:
            df._loads_strict(b'{"prompt_text": "\xff\xfe"}')
        self.assertEqual(cm.exception.code, "corrupt")
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        try:
            obj = json.loads(raw.decode("utf-8"))
            rec = self._read(dfix)
            attempt = next(a for a in rec["source_attempts"]
                           if a.get("id") == att)
            obj["prompt_text"] = "ok\ud800bad"
            with self.assertRaises(df.DerivedFixError) as cm:
                df._validate_full_input(obj, dfix, att, attempt)
            self.assertEqual(cm.exception.code, "corrupt")
            # An oversized leaf refuses by size-first proof without
            # reading the whole body for content.
            big = b"x" * (df._FULL_INPUT_CAP + 1024)
            self._leaf(dfix, att).write_bytes(big)
            cfd = df._open_full_input_dir(dfix, create=False)
            try:
                with self.assertRaises(df.DerivedFixError) as cm:
                    df._read_full_input_bytes(
                        cfd, att + ".json")
                self.assertEqual(cm.exception.code, "over-budget")
            finally:
                os.close(cfd)
        finally:
            self._restore(dfix, att, raw, rec_raw)

    def _closed_key_mutations(self):
        """(name, leaf-mutator or None, record-mutator or None)."""
        cases = []

        def _leaf_mut(name, fn):
            cases.append((name, fn, None))

        def _rec_mut(name, fn):
            cases.append((name, None, fn))

        _leaf_mut("model-none",
                  lambda o: o["profile"].update(model=None))
        _leaf_mut("model-other",
                  lambda o: o["profile"].update(model="other-x"))
        _leaf_mut("contributor-evil",
                  lambda o: o["profile"].update(contributor="evil"))
        _leaf_mut("subscription-absent",
                  lambda o: o["profile"].update(subscription=None))
        _leaf_mut("profile-extra-key",
                  lambda o: o["profile"].update(zz_extra="x"))
        _leaf_mut("profile-missing-timeout",
                  lambda o: o["profile"].pop("timeout_s"))
        _leaf_mut("timeout-bool",
                  lambda o: o["profile"].update(timeout_s=True))
        _leaf_mut("ref-kind-weird",
                  lambda o: o["evidence"][0].update(ref_kind="weird"))
        _leaf_mut("ref-nonhex",
                  lambda o: o["evidence"][0].update(ref="zz"))
        _leaf_mut("sent-false-with-bytes",
                  lambda o: o["evidence"][0].update(sent=False))
        _leaf_mut("sent-sha-nonhex",
                  lambda o: o["evidence"][0].update(
                      sent_sha256="zz" * 32))
        _leaf_mut("omitted-reason-bogus",
                  lambda o: o["evidence"][0].update(
                      omitted_reason="bogus"))
        _leaf_mut("partial-without-reason",
                  lambda o: o["evidence"][0].update(partial=True))
        _leaf_mut("absent-with-size",
                  lambda o: (o["evidence"][0].update(status="absent"),
                             o["evidence"][0].update(size=3)))
        _leaf_mut("toolarge-without-reason",
                  lambda o: (o["evidence"][0].update(
                      status="too-large", omitted_reason=None)))
        _leaf_mut("questions-sent-over-total",
                  lambda o: o["questions"].update(
                      sent=o["questions"]["total"] + 1))
        _leaf_mut("questions-clipped-over-sent",
                  lambda o: o["questions"].update(
                      clipped=o["questions"]["sent"] + 1,
                      clipped_bytes=9))
        _leaf_mut("conflicts-nonstring",
                  lambda o: o["conflicts"].append(7))
        _leaf_mut("evidence-duplicate-pair",
                  lambda o: o["evidence"].__setitem__(
                      1, dict(o["evidence"][0])))
        _leaf_mut("evidence-path-outside-allowlist",
                  lambda o: o["evidence"][0].update(
                      path="src/evil.js"))
        _leaf_mut("top-extra-key",
                  lambda o: o.update(zz_top=1))
        _rec_mut("pin-bytes-bool",
                 lambda a: a["full_input"].update(bytes=True))
        _rec_mut("pin-sha-nonhex",
                 lambda a: a["full_input"].update(sha256="zz"))
        _rec_mut("pin-extra-key",
                 lambda a: a["full_input"].update(zz_pin=1))
        # Zero passes the record gate (>= 0) but fails the strict
        # positive verify bound: the read-side pin check is covered.
        _rec_mut("pin-bytes-zero",
                 lambda a: a["full_input"].update(bytes=0))
        return cases

    def test_nested_schema_direct_validation(self):
        # File tampering only proves the independent raw pin. These
        # cases drive the REAL nested validator directly with parsed
        # objects plus the valid attempt — no file, no pin, no
        # validator mocks — so bool/int confusion, duplicate
        # coverage, malformed fields, and every nested cap must each
        # fail in the schema itself with the exact typed error.
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw = self._leaf(dfix, att).read_bytes()
        rec = self._read(dfix)
        attempt = next(a for a in rec["source_attempts"]
                       if a.get("id") == att)

        def _mut(name, fn):
            with self.subTest(schema=name):
                obj = json.loads(raw.decode("utf-8"))
                fn(obj)
                with self.assertRaises(df.DerivedFixError) as cm:
                    df._validate_full_input(obj, dfix, att, attempt)
                self.assertEqual(cm.exception.code, "corrupt", name)

        _mut("model-none",
             lambda o: o["profile"].update(model=None))
        _mut("contributor-evil",
             lambda o: o["profile"].update(contributor=5))
        _mut("timeout-bool",
             lambda o: o["profile"].update(timeout_s=True))
        _mut("size-bool",
             lambda o: o["evidence"][0].update(size=True))
        _mut("size-negative",
             lambda o: o["evidence"][0].update(size=-2))
        _mut("sent-int-one",
             lambda o: o["evidence"][0].update(sent=1))
        _mut("partial-int-zero",
             lambda o: o["evidence"][0].update(partial=0))
        _mut("sha-short",
             lambda o: o["evidence"][0].update(sent_sha256="ab"))
        _mut("omitted-unknown",
             lambda o: o["evidence"][0].update(omitted_reason="?"))
        _mut("ok-partial-without-reason",
             lambda o: o["evidence"][0].update(partial=True))
        _mut("questions-total-negative",
             lambda o: o["questions"].update(total=-1))
        _mut("questions-sent-over-cap",
             lambda o: o["questions"].update(
                 sent=df._PROMPT_QUESTIONS + 1, omitted=0))
        _mut("questions-clipped-bytes-without-clipped",
             lambda o: o["questions"].update(
                 clipped=0, clipped_bytes=7))
        _mut("conflicts-nonstring",
             lambda o: o["conflicts"].append(None))
        _mut("missing-evidence-key",
             lambda o: o["evidence"][0].pop("sent_sha256"))
        _mut("dup-pair-middle",
             lambda o: o["evidence"].__setitem__(
                 3, dict(o["evidence"][2])))
        # The pristine object keeps validating on its own.
        self.assertEqual(
            df._validate_full_input(
                json.loads(raw.decode("utf-8")), dfix, att,
                attempt)["prompt_sha256"],
            json.loads(raw.decode("utf-8"))["prompt_sha256"])

    def test_nested_closed_keys_reject(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        for name, leaf_fn, rec_fn in self._closed_key_mutations():
            with self.subTest(mutation=name):
                self._restore(dfix, att, raw, rec_raw)
                if leaf_fn is not None:
                    self._mutate_leaf(dfix, att, leaf_fn)
                else:
                    rec = json.loads(rec_raw.decode("utf-8"))
                    for attempt in rec["source_attempts"]:
                        if attempt.get("id") == att:
                            rec_fn(attempt)
                    try:
                        checked = df._validate_derived_record(rec)
                    except df.DerivedFixError as cm:
                        # The record gate refuses the malformed pin
                        # before anything is written.
                        self.assertEqual(cm.code, "corrupt", name)
                        continue
                    df._write_record(checked)
                with self.assertRaises(df.DerivedFixError) as cm:
                    df._verify_full_input(dfix, att)
                self.assertEqual(cm.exception.code, "corrupt",
                                 name)
        self._restore(dfix, att, raw, rec_raw)
        # An honestly relabeled oversize window keeps a valid shape
        # (so the shape gate passes) but the recorded pin no longer
        # matches the edited file: binding still refuses.
        self._mutate_leaf(
            dfix, att,
            lambda o: (o["evidence"][0].update(
                status="too-large", omitted_reason="blob-over-file-cap",
                sent=False, partial=False, sent_bytes=0,
                sent_sha256=None)))
        with self.assertRaises(df.DerivedFixError) as cm:
            df._verify_full_input(dfix, att)
        self.assertEqual(cm.exception.code, "corrupt")
        self._restore(dfix, att, raw, rec_raw)

    def _long_question_report(self, head):
        report = self._derived_canned_report(head)
        report = dict(report)
        # A valid-but-clippable question: over the 500-char display
        # cap yet within the 512-char question validation cap, so the
        # failure is clipping metadata (12 bytes), not refusal.
        report["questions"] = ["짧은 질문?", "Q" * 512]
        return report

    def test_evidence_honesty_and_clipped_questions(self):
        op_id = "prep-issue-856-clipped-q"
        self._write_derived_blocked_op(
            ["example/webpack.config.js"], self.cand_head,
            self.bar_head, op_id=op_id)
        req = ir.request_review(op_id)
        with mock.patch(
                "openmainty.muse.exec_prompt",
                return_value=(0, canned_jsonl(json.dumps(
                    self._long_question_report(self.cand_head))),
                    {"m": 1})):
            self.assertEqual(ir.execute_review(op_id, req["rid"]), 0)
        names = self._origin_refs(op_id)
        out = self._create(op_id, *names)
        started = self._start(op_id, out["dfix"])
        rec = self._read(out["dfix"])
        work = Path(rec["workspace"]["path"])
        seen = {}

        def _run(prompt, workdir=None, **kw):
            seen["prompt"] = prompt
            self._edit_single_line(work)
            raw = (work / "example/webpack.config.js").read_bytes()
            return (0, _worker_jsonl(_proposal(
                ["example/webpack.config.js"],
                {"example/webpack.config.js":
                 hashlib.sha256(raw).hexdigest()},
                [{"path": "example/webpack.config.js",
                  "conflict_id": "example/webpack.config.js",
                  "choice": "H-A"}])), {"m": 1})

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_run):
            self.assertEqual(self._run_worker(
                op_id, out["dfix"], started["attempt_id"]), 0)
        got = df.describe_full_input(
            op_id, out["dfix"], started["attempt_id"])
        self.assertEqual(got["status"], "stored")
        # A sent question is never claimed fully delivered when only
        # its prefix is in the prompt: exact clipped counts.
        self.assertEqual(got["questions"]["total"], 2)
        self.assertEqual(got["questions"]["sent"], 2)
        self.assertEqual(got["questions"]["omitted"], 0)
        self.assertEqual(got["questions"]["clipped"], 1)
        self.assertEqual(got["questions"]["clipped_bytes"], 12)
        self.assertIn("짧은 질문?", got["text"])
        self.assertIn("Q" * 500, got["text"])
        self.assertNotIn("Q" * 512, got["text"])
        self.assertIn("바이트 생략", got["text"])
        # Every window is honestly labeled: full delivery has no
        # reason, sent bodies are non-empty with bound digests.
        self.assertEqual(got["omitted"], [])
        leaf_obj = json.loads(
            self._leaf(out["dfix"],
                       started["attempt_id"]).read_bytes())
        self.assertEqual(len(leaf_obj["evidence"]), 6)
        pairs = {(e["path"], e["ref_kind"])
                 for e in leaf_obj["evidence"]}
        self.assertEqual(len(pairs), 6)
        for ev in leaf_obj["evidence"]:
            self.assertIsNone(ev["omitted_reason"])
            self.assertFalse(ev["partial"])
            self.assertTrue(ev["sent"])
            self.assertTrue(ev["sent_bytes"] > 0)
            self.assertTrue(df._is_hex(ev["sent_sha256"], 64))

    def test_fit_blocks_partial_and_refusal(self):
        base = {"path": "example/webpack.config.js",
                "ref_kind": "candidate", "ref": "a" * 40,
                "blob_oid": "b" * 40, "size": 100,
                "raw_sha256": "c" * 64, "status": "ok",
                "text": None, "omitted_bytes": 0, "lossy": False}
        evs = [dict(base, text="x" * 100),
               dict(base, ref_kind="base", text="y" * 100)]
        heads = [df._render_evidence_head(ev) for ev in evs]
        fixed = sum(len(h.encode("utf-8")) + 1 for h in heads) + 1
        # A budget with room past the omission markers truncates
        # both bodies to real nonempty prefixes with honest markers
        # and stays within budget. Each 100-byte body needs its worst
        # marker first: "\n(…100바이트 생략; …)" is 57 bytes, so an
        # 80-byte share keeps 23 bytes and reports 100-23=77 omitted —
        # all counted from the ORIGINAL body bytes, never from
        # transformed text. Full pinned digests survive in the head.
        fitted, _used = df._fit_evidence_blocks(evs, fixed + 160)
        total = sum(len(b.encode("utf-8")) for b in fitted) + 1
        self.assertLessEqual(total, fixed + 160)
        for block, ch in zip(fitted, ("x", "y")):
            self.assertIn(ch * 23, block)
            self.assertNotIn(ch * 24, block)
            self.assertIn("77바이트 생략", block)
            self.assertIn("a" * 40, block)
            self.assertIn("b" * 40, block)
            self.assertIn("c" * 64, block)
        # An impossible budget refuses instead of looping or
        # cutting headers: typed over-budget, never a silent drop.
        with self.assertRaises(df.DerivedFixError) as cm:
            df._fit_evidence_blocks(evs, fixed - 1)
        self.assertEqual(cm.exception.code, "over-budget")
        # A lossy window that does not fit refuses honestly: no
        # omission count is ever computed from transformed text.
        lossy = [dict(base, text="z" * 100, lossy=True,
                      raw_sha256="d" * 64)]
        with self.assertRaises(df.DerivedFixError) as cm:
            df._fit_evidence_blocks(lossy, fixed - 60)
        self.assertEqual(cm.exception.code, "over-budget")

    def _census(self):
        # Content SHA + mode + full directory census: proves no
        # write, no mkdir, and no byte change — sizes alone could
        # hide equal-length rewrites and miss new empty dirs.
        files, dirs = {}, set()
        for root, _ds, fs in os.walk(self._home()):
            rel = os.path.relpath(root, str(self._home()))
            dirs.add(rel)
            for name in fs:
                path = Path(root) / name
                try:
                    digest = hashlib.sha256(
                        path.read_bytes()).hexdigest()
                    mode = stat.S_IMODE(os.lstat(str(path)).st_mode)
                except OSError:
                    digest, mode = None, None
                files[os.path.join(rel, name)] = (digest, mode)
        return files, dirs

    def test_no_mkdir_on_absent_layout(self):
        # Establish a real owned record first — the fixture setup
        # leaves integration_ops absent, and renaming a missing dir
        # would fail before exercising anything. With the owned
        # layout present, moving it aside must neither recreate it
        # nor write anything on the read-only GET; restore returns
        # the exact census.
        self._capture_run(self.blocked_id)
        before = self._census()
        ops = self._home() / "integration_ops"
        aside = self._home() / "integration_ops.aside"
        self.assertTrue(ops.is_dir())
        self.assertFalse(aside.exists())
        ops.rename(aside)
        try:
            mid = self._census()
            self.assertNotIn("integration_ops", mid[1])
            with self.assertRaises(df.DerivedFixError) as cm:
                df.describe_full_input(
                    self.blocked_id, "dfix-" + "0" * 32,
                    "da-" + "0" * 16)
            self.assertEqual(cm.exception.code, "unknown-derived")
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(
                    "dfix-" + "0" * 32, "da-" + "0" * 16)
            # No missing directory was recreated and no file was
            # written: the read-only GET creates nothing.
            self.assertEqual(self._census(), mid)
            self.assertFalse(ops.exists())
        finally:
            aside.rename(ops)
        self.assertEqual(self._census(), before)

    def test_record_chain_hardening(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        rec_path = df._checked_derived_file(df.derived_path(dfix))
        real = rec_path.read_bytes()
        try:
            # An absurd record refuses by size-first proof without
            # reading the whole body for content.
            rec_path.write_bytes(b"x" * (df._RECORD_READ_CAP + 1024))
            with self.assertRaises(df.DerivedFixError) as cm:
                df._read_record_ro(dfix)
            self.assertEqual(cm.exception.code, "over-budget")
            # A swapped integration_ops ancestor refuses instead of
            # being followed; the suspect layout is preserved.
            rec_path.write_bytes(real)
            ops = self._home() / "integration_ops"
            shadow = self._home() / "shadow-ops"
            shadow.mkdir(exist_ok=True)
            ops.rename(shadow / "real-ops")
            try:
                os.symlink(str(shadow), str(ops))
                with self.assertRaises(df.DerivedFixError) as cm:
                    df._read_record_ro(dfix)
                self.assertEqual(
                    cm.exception.code, "ancestor-replaced")
                self.assertTrue(os.path.islink(str(ops)))
            finally:
                os.unlink(str(ops))
                (shadow / "real-ops").rename(ops)
            # A plain non-directory ancestor refuses as replaced too
            # (proven by lstat, not by errno alone); the suspect path
            # is preserved untouched for inspection.
            moved = self._home() / "integration_ops.real"
            ops.rename(moved)
            try:
                ops.write_bytes(b"not-a-directory")
                with self.assertRaises(df.DerivedFixError) as cm:
                    df._read_record_ro(dfix)
                self.assertEqual(
                    cm.exception.code, "ancestor-replaced")
                self.assertTrue(ops.is_file())
            finally:
                ops.unlink(missing_ok=True)
                moved.rename(ops)
            # A one-shot I/O failure stays unreadable: EIO is never
            # rebranded as a replaced ancestor. The fault targets the
            # store open only; every other open delegates to the real
            # call, and the pinned home descriptor is still closed.
            real_open = os.open
            fired = []

            def _eio_once(path, *args, **kwargs):
                if not fired and path == "integration_ops":
                    fired.append(1)
                    raise OSError(errno.EIO, "simulated I/O error")
                return real_open(path, *args, **kwargs)

            before_io = self._fds()
            with mock.patch("os.open", side_effect=_eio_once):
                with self.assertRaises(df.DerivedFixError) as cm:
                    df._read_record_ro(dfix)
            self.assertEqual(fired, [1])
            self.assertEqual(cm.exception.code, "unreadable")
            after_io = self._fds()
            if before_io is not None and after_io is not None:
                self.assertEqual(before_io, after_io)
            # An unprovable race stays unreadable: when the no-follow
            # proof lstat itself fails, ENOTDIR alone is never treated
            # as proof of a symlink.
            ops.rename(shadow / "real-ops")
            try:
                os.symlink(str(shadow), str(ops))
                real_lstat = os.lstat

                def _lstat_race(path, *args, **kwargs):
                    if path == "integration_ops":
                        raise OSError(errno.ENOENT,
                                      "simulated proof race")
                    return real_lstat(path, *args, **kwargs)

                with mock.patch("os.lstat",
                                side_effect=_lstat_race):
                    with self.assertRaises(df.DerivedFixError) as cm:
                        df._read_record_ro(dfix)
                self.assertEqual(cm.exception.code, "unreadable")
                self.assertTrue(os.path.islink(str(ops)))
            finally:
                os.unlink(str(ops))
                (shadow / "real-ops").rename(ops)
            # A record-leaf FIFO refuses promptly in a bounded owned
            # child (never a hanging open), with no fd leak; on a
            # regression the owned drain unblocks, reaps, then fails.
            rec_path.unlink()
            os.mkfifo(str(rec_path))
            thread, out, errors, before = self._verify_threaded(
                dfix, att)
            stuck = self._join_probe(
                thread, out, errors, before,
                lambda: self._drain_fifo(rec_path))
            if stuck:
                self.fail("verify blocked opening a record FIFO: "
                          "O_NONBLOCK missing")
            self.assertEqual(out, [])
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], df.DerivedFixError)
            self.assertEqual(errors[0].code, "corrupt")
        finally:
            if rec_path.is_symlink() or not rec_path.is_file():
                rec_path.unlink(missing_ok=True)
            rec_path.write_bytes(real)

    def _fds(self):
        try:
            return set(os.listdir("/dev/fd"))
        except OSError:
            return None

    def _fstat_ino_fault(self, leaf, mode):
        # Inode-bound one-shot stdlib I/O fault: only the fstat for
        # the owned leaf inode is synthesized, exactly once; every
        # other descriptor (container dirs, closing re-stat) reads
        # the real kernel state. No product code is patched and the
        # fault cannot fire in an unrelated read. "raise" proves a
        # raw stat I/O failure lands typed (no escaping OSError).
        real_fstat = os.fstat
        target = os.stat(str(leaf)).st_ino
        fired = []

        def _fake(fd):
            st = real_fstat(fd)
            if st.st_ino == target and not fired:
                fired.append(1)
                if mode == "raise":
                    raise OSError("simulated fstat I/O failure")
                if mode == "replace":
                    return os.stat_result(
                        (st.st_mode, st.st_ino + 99999, st.st_dev,
                         st.st_nlink, st.st_uid, st.st_gid,
                         st.st_size, st.st_atime, st.st_mtime,
                         st.st_ctime))
                delta = -10 if mode == "growth" else 100
                return os.stat_result(
                    (st.st_mode, st.st_ino, st.st_dev,
                     st.st_nlink, st.st_uid, st.st_gid,
                     st.st_size + delta, st.st_atime, st.st_mtime,
                     st.st_ctime))
            return st

        return _fake

    def _mutating_read(self, leaf, mode):
        # REAL owned-file mutation bound to the owned target
        # identity: the hook inspects each os.read fd with the real
        # fstat and fires exactly once, only for the fd whose
        # (device, inode) is the owned leaf's. On ANY other fd it
        # never fires, so the fault cannot couple to an unrelated
        # read's order. When it fires it genuinely appends to /
        # truncates / same-size replaces the owned leaf, then
        # delegates to the real read. This proves growth, early EOF,
        # and name-identity replacement against true kernel state —
        # never synthesized stat fields — with no product patching.
        # Callers must assert hook.fired == [1]: a silent no-fire
        # would pass without exercising the race.
        real_read = os.read
        real_fstat = os.fstat
        try:
            target = os.stat(str(leaf))
            target_id = (target.st_dev, target.st_ino)
            real_size = target.st_size
        except OSError:
            target_id = None
            real_size = 0
        fired = []

        def _hook(fd, n):
            if not fired and target_id is not None:
                try:
                    cur = real_fstat(fd)
                except OSError:
                    cur = None
                if cur is not None and \
                        (cur.st_dev, cur.st_ino) == target_id:
                    fired.append(1)
                    if mode == "growth":
                        with open(str(leaf), "ab") as handle:
                            handle.write(b"GROWN-BY-OWNED-RACE")
                    elif mode == "shrink":
                        os.truncate(str(leaf),
                                    max(0, real_size - 10))
                    elif mode == "replace":
                        other = leaf.parent / (leaf.name + ".other")
                        other.write_bytes(b"R" * real_size)
                        os.replace(str(other), str(leaf))
            return real_read(fd, n)

        _hook.fired = fired
        return _hook

    def _drain_fifo(self, path):
        # Unblock a thread stuck in a FIFO open (regression cleanup
        # only): a blocking WRONLY open pairs with the stuck RDONLY
        # open so both return; the verifier then refuses the
        # non-regular leaf and exits. The drain thread is always
        # joined by the caller — no leaked thread even when the
        # product regresses.
        def _w():
            try:
                wfd = os.open(str(path), os.O_WRONLY)
                try:
                    os.close(wfd)
                except OSError:
                    pass
            except OSError:
                pass

        t = threading.Thread(target=_w, daemon=True)
        t.start()
        return t

    def _verify_threaded(self, dfix, att):
        out, errors = [], []

        def _probe():
            try:
                out.append(df._verify_full_input(dfix, att))
            except Exception as exc:  # noqa: BLE001 - collected
                errors.append(exc)

        thread = threading.Thread(target=_probe, daemon=True)
        before = self._fds()
        thread.start()
        return thread, out, errors, before

    def _join_probe(self, thread, out, errors, before, unblock,
                    timeout=30):
        # Bounded join with regression cleanup: if a future change
        # drops the nonblocking open, the probe thread stays stuck
        # in the FIFO open after the timeout. The owned unblock
        # callback then pairs the open so the verifier can refuse
        # and exit; both threads are joined (no leaked daemon or
        # child) and the caller fails the test for the regression.
        # Returns True when the unblock path ran.
        thread.join(timeout=timeout)
        stuck = thread.is_alive()
        drain = None
        if stuck and unblock is not None:
            drain = unblock()
            thread.join(timeout=60)
        self.assertFalse(thread.is_alive())
        if drain is not None:
            drain.join(timeout=60)
            self.assertFalse(drain.is_alive())
        after = self._fds()
        if before is not None and after is not None:
            self.assertEqual(before, after)
        return stuck

    def test_fifo_leaf_refuses_promptly(self):
        # A FIFO leaf must refuse as a non-regular file. The probe
        # runs in a bounded owned child: if a future change drops
        # the nonblocking open, the join times out, the owned
        # drain unblocks the open so the verifier can refuse and
        # exit, both threads are reaped, and the test fails for
        # the regression instead of hanging the suite.
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        try:
            self._leaf(dfix, att).unlink()
            os.mkfifo(str(self._leaf(dfix, att)))
            thread, out, errors, before = self._verify_threaded(
                dfix, att)
            stuck = self._join_probe(
                thread, out, errors, before,
                lambda: self._drain_fifo(self._leaf(dfix, att)))
            if stuck:
                self.fail("verify blocked opening a FIFO leaf: "
                          "O_NONBLOCK missing")
            self.assertEqual(out, [])
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], df.DerivedFixError)
            self.assertEqual(errors[0].code, "corrupt")
        finally:
            leaf = self._leaf(dfix, att)
            if leaf.is_symlink() or not leaf.is_file():
                leaf.unlink(missing_ok=True)
            self._restore(dfix, att, raw, rec_raw)

    def test_symlink_growth_shrink_replace_refuse(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        sentinel = self._home() / "outside-sentinel.txt"
        sentinel.write_text("SENTINEL-OUTSIDE-BYTES", encoding="utf-8")
        try:
            # A swapped-in symlink never resolves to outside bytes:
            # no-follow open fails before any read.
            self._leaf(dfix, att).unlink()
            os.symlink(str(sentinel), str(self._leaf(dfix, att)))
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            # A swapped link refuses as tampering (corrupt), never
            # as a generic I/O failure, and leaks no outside bytes.
            self.assertEqual(cm.exception.code, "corrupt")
            self.assertNotIn("SENTINEL",
                             str(cm.exception.message))
            # Post-fstat growth, early EOF, and same-size
            # replacement each refuse instead of accepting a prefix
            # as immutable. The mutation is REAL kernel state (an
            # actual append/truncate/replace of the owned leaf
            # between the true initial fstat and the read), bound to
            # the owned (device, inode) so it cannot couple to an
            # unrelated read; descriptor hygiene holds. The replace
            # subcase is the sharp one: the open fd still serves the
            # old pinned bytes, so only the name-identity proof
            # catches the swapped publication.
            for mode in ("growth", "shrink", "replace"):
                leaf = self._leaf(dfix, att)
                if leaf.is_symlink() or not leaf.is_file():
                    leaf.unlink(missing_ok=True)
                self._restore(dfix, att, raw, rec_raw)
                before = self._fds()
                hook = self._mutating_read(
                    self._leaf(dfix, att), mode)
                with mock.patch("os.read", side_effect=hook):
                    with self.assertRaises(
                            df.DerivedFixError) as cm:
                        df._verify_full_input(dfix, att)
                self.assertEqual(hook.fired, [1],
                                 "race fault never fired: " + mode)
                self.assertEqual(cm.exception.code, "corrupt",
                                 mode)
                after = self._fds()
                if before is not None and after is not None:
                    self.assertEqual(before, after)
        finally:
            leaf = self._leaf(dfix, att)
            if leaf.is_symlink() or not leaf.is_file():
                leaf.unlink(missing_ok=True)
            self._restore(dfix, att, raw, rec_raw)

    def test_initial_fstat_failure_is_typed_unreadable(self):
        # A one-shot raw fstat I/O failure on the owned leaf inode
        # must land as typed DerivedFixError(unreadable) through
        # the real read helpers — never an escaping OSError — with
        # no descriptor leak. No product code is patched; only the
        # stdlib boundary faults once, at the target inode.
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        try:
            before = self._fds()
            with mock.patch(
                    "os.fstat",
                    side_effect=self._fstat_ino_fault(
                        self._leaf(dfix, att), "raise")):
                with self.assertRaises(
                        df.DerivedFixError) as cm:
                    df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "unreadable")
            after = self._fds()
            if before is not None and after is not None:
                self.assertEqual(before, after)
        finally:
            self._restore(dfix, att, raw, rec_raw)

    def test_record_initial_fstat_failure_is_typed_unreadable(self):
        # Same translation on the record chain: a one-shot raw
        # fstat I/O failure on the owned record inode surfaces as
        # typed unreadable (not raw OSError), descriptors clean.
        dfix, att, _seen = self._capture_run(self.blocked_id)
        rec_path = df._checked_derived_file(df.derived_path(dfix))
        real = rec_path.read_bytes()
        try:
            before = self._fds()
            with mock.patch(
                    "os.fstat",
                    side_effect=self._fstat_ino_fault(
                        rec_path, "raise")):
                with self.assertRaises(
                        df.DerivedFixError) as cm:
                    df._read_record_ro(dfix)
            self.assertEqual(cm.exception.code, "unreadable")
            after = self._fds()
            if before is not None and after is not None:
                self.assertEqual(before, after)
        finally:
            if rec_path.is_symlink() or not rec_path.is_file():
                rec_path.unlink(missing_ok=True)
            rec_path.write_bytes(real)

    def test_ancestor_swap_refuses(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        dfix_dir = self._home() / "derived" / dfix
        shadow = self._home() / "shadow-derived"
        shadow.mkdir(exist_ok=True)
        real = shadow / "real-derived"
        dfix_dir.rename(real)
        try:
            os.symlink(str(shadow), str(dfix_dir))
            with self.assertRaises(df.DerivedFixError) as cm:
                df._verify_full_input(dfix, att)
            self.assertEqual(cm.exception.code, "ancestor-replaced")
            # The suspect layout is preserved: nothing deleted or
            # rebound by the refusal.
            self.assertTrue(os.path.islink(str(dfix_dir)))
        finally:
            os.unlink(str(dfix_dir))
            real.rename(dfix_dir)

    def test_concurrent_identical_publish(self):
        dfix, attempt, prompt, info, ws, rec = (
            self._prepared_publish_args(self.blocked_id))
        results, errors = [], []

        def _pub():
            try:
                results.append(df._publish_full_input(
                    dfix, attempt, prompt, info, ws, rec))
            except Exception as exc:  # noqa: BLE001 - collected
                errors.append(exc)

        threads = [threading.Thread(target=_pub) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            # No stuck or leaked publisher thread may remain.
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        file_raw = self._leaf(dfix, attempt["id"]).read_bytes()
        self.assertEqual(hashlib.sha256(file_raw).hexdigest(),
                         results[0]["sha256"])
        try:
            leftovers = sorted(
                os.listdir(str(self._leaf(dfix,
                                          attempt["id"]).parent)))
        except OSError as exc:
            self.fail(f"cannot list sidecar dir: {exc}")
        # Concurrent identical publication leaves exactly one
        # artifact: the winner's link plus no temp residue.
        self.assertEqual(leftovers, [attempt["id"] + ".json"])

    def test_serve_route_shapes(self):
        dfix, att, _seen = self._capture_run(self.blocked_id)
        before = self._tree()
        got = serve.api_derived_fix_input(
            {"op": self.blocked_id, "dfix": dfix, "attempt": att})
        self.assertEqual(got["status"], "stored")
        self.assertTrue(got["text"])
        self.assertEqual(got["profile"]["model"], "muse-spark-1.3")
        self.assertEqual(len(got["evidence"]), 6)
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_input(
                {"op": "bad op!", "dfix": dfix, "attempt": att})
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.code, "bad-op")
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_input(
                {"op": self.blocked_id, "dfix": "nope",
                 "attempt": att})
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, "bad-id")
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_input(
                {"op": self.blocked_id, "dfix": "dfix-" + "0" * 32,
                 "attempt": att})
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.code, "unknown-derived")
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_input(
                {"op": "prep-issue-856-t1", "dfix": dfix,
                 "attempt": att})
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_input(
                {"op": self.blocked_id, "dfix": dfix,
                 "attempt": "da-" + "0" * 16})
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.code, "unknown-attempt")
        raw, rec_raw = self._pristine(dfix, att)
        self._leaf(dfix, att).unlink()
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_derived_fix_input(
                {"op": self.blocked_id, "dfix": dfix,
                 "attempt": att})
        self.assertEqual(cm.exception.status, 502)
        # Typed transport codes let the viewer tell a lost
        # artifact from corruption from a fetch failure.
        self.assertEqual(cm.exception.code, "missing-required")
        try:
            self._restore(dfix, att, raw, rec_raw)
            self._mutate_leaf(
                dfix, att,
                lambda o: o.update(prompt_text=o["prompt_text"] + " "))
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_derived_fix_input(
                    {"op": self.blocked_id, "dfix": dfix,
                     "attempt": att})
            self.assertEqual(cm.exception.status, 502)
            self.assertEqual(cm.exception.code, "corrupt")
        finally:
            self._restore(dfix, att, raw, rec_raw)
        # The read-only route created nothing and wrote nothing.
        current = self._tree()
        before.pop(str(Path("derived") / dfix / "full-input"
                        / (att + ".json")), None)
        current.pop(str(Path("derived") / dfix / "full-input"
                         / (att + ".json")), None)
        self.assertEqual(current, before)

    def test_input_route_http_envelope(self):
        # The REAL HTTP envelope through the actual app Handler
        # (``serve.Handler`` — no copied route, no monkeypatched
        # registration): stored reads arrive as 200 JSON, while
        # missing/corrupt artifacts arrive as non-2xx JSON carrying
        # the SAME typed {error, code} the registration sends — so
        # real fetch clients can distinguish lost input from
        # corruption from a fetch failure. Served over an owned
        # 127.0.0.1 loopback server (ephemeral port, fresh fixture);
        # every response is closed and the server is always shut
        # down, closed, and joined.
        dfix, att, _seen = self._capture_run(self.blocked_id)
        raw, rec_raw = self._pristine(dfix, att)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        thread = threading.Thread(target=srv.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            base = ("http://127.0.0.1:%d"
                    "/api/integration/derived-fix/input"
                    % srv.server_address[1])
            q = ("?op=%s&dfix=%s&attempt=%s"
                 % (urllib.parse.quote(self.blocked_id, safe=""),
                    urllib.parse.quote(dfix, safe=""),
                    urllib.parse.quote(att, safe="")))
            with urllib.request.urlopen(
                    base + q, timeout=30) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body["status"], "stored")
            self.assertTrue(body["text"])
            self.assertTrue(body["prompt_bytes"])
            self.assertTrue(body["pin_sha256"])
            # Present pin + missing file: 502 + missing-required
            # over real HTTP, with a human-readable error.
            self._leaf(dfix, att).unlink()
            try:
                urllib.request.urlopen(base + q, timeout=30)
                self.fail("missing input must fail closed over HTTP")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 502)
                try:
                    env = json.loads(exc.read().decode("utf-8"))
                finally:
                    exc.close()
                self.assertEqual(env.get("code"),
                                 "missing-required")
                self.assertTrue(env.get("error"))
            # Tampered bytes: 502 + corrupt over real HTTP.
            try:
                self._restore(dfix, att, raw, rec_raw)
                self._mutate_leaf(
                    dfix, att,
                    lambda o: o.update(
                        prompt_text=o["prompt_text"] + " "))
                try:
                    urllib.request.urlopen(base + q, timeout=30)
                    self.fail("corrupt input must fail closed "
                              "over HTTP")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 502)
                    try:
                        env = json.loads(exc.read().decode("utf-8"))
                    finally:
                        exc.close()
                    self.assertEqual(env.get("code"), "corrupt")
                    self.assertTrue(env.get("error"))
            finally:
                self._restore(dfix, att, raw, rec_raw)
        finally:
            self._restore(dfix, att, raw, rec_raw)
            srv.shutdown()
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive())
            srv.server_close()

    def test_transport_crash_and_format_preserved(self):
        self._done_derived_review(self.blocked_id, self.cand_head)
        names = self._origin_refs(self.blocked_id)
        out = self._create(self.blocked_id, *names)
        started = self._start(self.blocked_id, out["dfix"])

        def _boom(prompt, workdir=None, **kw):
            raise OSError("simulated offline transport")

        with mock.patch("openmainty.muse.exec_source_repair",
                        side_effect=_boom):
            rc = self._run_worker(self.blocked_id, out["dfix"],
                                  started["attempt_id"])
        self.assertEqual(rc, 1)
        rec = self._read(out["dfix"])
        attempt = rec["source_attempts"][-1]
        self.assertEqual(attempt["status"], "source-error")
        self.assertIn("OSError", attempt["error"])
        # No result was recorded: extractor never ran stays None,
        # distinct from an actually extracted empty string.
        self.assertIsNone(rec["workspace"]["result_head"])
        self.assertIsNone(attempt["raw_head"])
        self.assertEqual(attempt["raw_total_chars"], 0)
        self.assertEqual(attempt["raw_total_bytes"], 0)
        with mock.patch("openmainty.muse.exec_source_repair",
                        return_value=(3, "", {"m": 0})):
            out2 = self._create(self.blocked_id, *names,
                                hyp_id="H-B")
            started2 = self._start(self.blocked_id, out2["dfix"])
            rc = self._run_worker(self.blocked_id, out2["dfix"],
                                  started2["attempt_id"])
        self.assertEqual(rc, 1)
        rec2 = self._read(out2["dfix"])
        self.assertIn("exit 3",
                      rec2["source_attempts"][-1]["error"])


if __name__ == "__main__":
    unittest.main()
