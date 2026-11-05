"""Bounded fastcheck report projection served with /api/work.

Covers serve.fastcheck_report_for_run: a worker pass binds the actual
receipt bytes (sha256 vs the gate's receipt_sha256), the recorded
source pins (ordinary profile step head/fingerprint vs the op result
identity) and the live source (current_qa.source head/fingerprint /
clean / worktree). Anything missing, malformed, unreadable,
hash-mismatched, op-mismatched or drifted reports explicit
unknown/stale/missing — never a current pass. A hold-driven stale QA
is approval state, not byte drift. The projection is strictly
read-only: receipts and op records are never written.
Run with: python3 -m unittest tests.test_work_fastcheck_report
"""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import serve  # noqa: E402

try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402

HEAD = "a" * 40
FP = "b" * 64
PATH = "/tmp/prep-wt"


def _step(name="unit", head=HEAD, fp=FP, rc=0, tail="ok\n", extra=None):
    s = {"name": name, "gate": "unit " + name, "rc": rc,
         "timed_out": False, "elapsed_s": 1.5, "argv": ["make", name],
         "head": head, "fingerprint": fp, "log_tail": tail,
         "log_truncated": False, "network_sandbox": "loopback-only",
         "sandbox_profile": "profile", "file_sandbox": "files",
         "write_allow": [], "children_verified_gone": True,
         "log_bytes": 100, "log_sha256": "c" * 64}
    if extra:
        s.update(extra)
    return s


def _turbo_substep(name, argv, tail):
    return {"name": name, "argv": argv, "rc": 0, "timed_out": False,
            "elapsed_s": 0.4, "tail": tail, "log_truncated": False,
            "network_sandbox": False, "sandbox_profile": "profile",
            "write_allow": ["/tmp/x"], "children_verified_gone": True,
            "log_bytes": 200, "log_sha256": "d" * 64}


def _consumer_turbo(status="pass"):
    steps = [
        _turbo_substep("turbo version", ["turbo", "--version"],
                       "2.11.10\n"),
        _turbo_substep("turbo dry", ["turbo", "run", "build:web",
                                     "--dry=json"],
                       '{"tasks": ["example#build:web"]}\n'),
        _turbo_substep("turbo run", ["turbo", "run", "build:web",
                                     "--force"],
                       'warning "example#build:web" '
                       "no output files found for task\n"
                       "Compiled successfully\n"),
    ]
    return {"status": status, "script": "Turbo Web (turbo)",
            "name": "consumer (turbo: build:web)", "manager": "yarn",
            "argv": ["turbo", "run", "build:web", "--force"],
            "rc": 0 if status == "pass" else 1,
            "timed_out": False, "elapsed_s": 11.7,
            "label": "Turbo Web 작업 실행 검증 통과",
            "turbo_version_expected": "2.11.10",
            "turbo_version_actual": "2.11.10",
            "turbo_task_count": 1, "turbo_scope": "example",
            "turbo_tasks": ["example#build:web"],
            "turbo_steps": steps if status == "pass" else [],
            "note": "Android/네이티브 CI 미검증 — Web 소비 검증만 해당",
            "reason": ""}


def _consumer_generic(status="pass"):
    return {"status": status, "script": "webpack build",
            "name": "consumer (webpack)", "manager": "yarn",
            "argv": ["yarn", "workspace", "example", "build"],
            "rc": 0 if status == "pass" else 3,
            "timed_out": False, "elapsed_s": 5.0,
            "label": "소비 검증 통과",
            "log_tail": "asset bundle.js 100 KiB\n",
            "log_truncated": False,
            "receipt": {"network_sandbox": "loopback-only",
                        "sandbox_profile": "profile",
                        "write_allow": [], "children_verified_gone": True,
                        "log_bytes": 50, "log_sha256": "e" * 64},
            "reason": ""}


def _summary(op, steps, consumer=None, ok=True, failed=None,
            root_attested=False):
    summary = {"op": op, "ok": ok, "at": "2026-10-25T17:37:47",
               "wall_s": 66.8, "steps": steps,
               "worker_attested": True, "root_attested": root_attested}
    if consumer is not None:
        summary["consumer"] = consumer
    if failed is not None:
        summary["failed"] = failed
    return summary


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = None
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _write_receipt(self, op, summary, raw=None):
        from openmainty import fastcheck
        path = fastcheck.op_fastcheck_path(op)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = raw if raw is not None else json.dumps(
            summary, ensure_ascii=False).encode("utf-8")
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), path

    def _prep(self, op="prep-x", run="run-x", head=HEAD, fp=FP,
              path=PATH, status="pass", sha="", attested="worker"):
        return {"op_id": op, "run_id": run,
                "gates": [{"key": "fastchecks", "status": status,
                           "receipt_sha256": sha,
                           "attested_by": attested}],
                "result": {"head": head, "fingerprint": fp,
                           "path": path}}

    def _qa(self, head=HEAD, fp=FP, clean=True, wt=PATH,
            pending=(), batch=None):
        gates = [{"key": "fastchecks", "label": "빠른 검사",
                  "status": "pass"}]
        for i, label in enumerate(pending):
            gates.append({"key": "hold-%d" % i, "label": label,
                          "status": "pending"})
        qa = {"state": "ready",
              "source": {"head": head, "fingerprint": fp,
                         "clean": clean, "worktree": wt},
              "gates": gates}
        if batch is not None:
            qa["batch"] = batch
        return qa

    def _write_batch(self, batch_id, hold=None):
        from openmainty import batches
        batches.batches_dir().mkdir(parents=True, exist_ok=True)
        rec = {"id": batch_id, "repo": "fakerepo"}
        if hold is not None:
            rec["hold"] = hold
        (batches.batches_dir() / (batch_id + ".json")).write_text(
            json.dumps(rec), encoding="utf-8")

    def _good(self, **kw):
        op = kw.get("op", "prep-x")
        run = kw.get("run", "run-x")
        steps = kw.get("steps", [_step()])
        consumer = kw.get("consumer", _consumer_turbo())
        summary = _summary(op, steps, consumer,
                           root_attested=kw.get("root_attested", False))
        sha, _path = self._write_receipt(op, summary)
        prep = self._prep(op=op, run=run, sha=sha)
        qa = self._qa(
            pending=kw.get("pending", ("최종 검수 확인", "보류 결정")))
        return serve.fastcheck_report_for_run(
            {"repo": "r"}, run, prep=prep, current_qa=qa)

    def _snapshot(self):
        from openmainty import fastcheck
        ops = fastcheck.op_fastcheck_path("prep-x").parent
        if not ops.is_dir():
            return {}
        return {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
                for p in sorted(ops.iterdir())}

    def test_pass_worker_full(self):
        rep = self._good()
        self.assertEqual(rep["state"], "pass-worker")
        self.assertTrue(rep["worker_attested"])
        self.assertFalse(rep["root_attested"])
        self.assertEqual(rep["receipt_sha256"], self._sha())
        cons = rep["consumer"]
        self.assertTrue(cons["turbo"])
        self.assertEqual(cons["turbo_version_actual"], "2.11.10")
        self.assertEqual(cons["turbo_task_count"], 1)
        self.assertEqual(len(cons["turbo_steps"]), 3)
        self.assertEqual(rep["pending_gates"]["count"], 2)
        # Consumer evidence first: the Turbo outputs warning leads.
        self.assertGreaterEqual(len(rep["warnings"]), 1)
        self.assertEqual(rep["warnings"][0]["step"], "consumer")
        self.assertIn("no output files", rep["warnings"][0]["line"])
        self.assertEqual(len(rep["steps"]), 1)

    def test_pass_worker_reason_stays_review_neutral(self):
        cases = [
            {"root_attested": False,
             "pending": ("최종 검수 확인", "보류 결정")},
            {"root_attested": True, "pending": ()},
        ]
        for kw in cases:
            rep = self._good(**kw)
            self.assertEqual(rep["state"], "pass-worker")
            self.assertEqual(
                rep["reason"], "앱 자동 검사 통과 · 최종 판단은 별도 기록 참조")
            self.assertNotIn("독립 검수", rep["reason"])
            self.assertNotIn("검수 완료", rep["reason"])

    def _sha(self):
        from openmainty import fastcheck
        return hashlib.sha256(
            fastcheck.op_fastcheck_path("prep-x").read_bytes()
        ).hexdigest()

    def test_read_only(self):
        rep = self._good()
        self.assertEqual(rep["state"], "pass-worker")
        before = self._snapshot()
        before_bytes = {}
        from openmainty import fastcheck
        ops = fastcheck.op_fastcheck_path("prep-x").parent
        for name in before:
            before_bytes[name] = (ops / name).read_bytes()
        # Project twice more without rewriting the receipt: the
        # projection itself must not touch any file.
        prep = self._prep(sha=self._sha())
        qa = self._qa(pending=("최종 검수 확인", "보류 결정"))
        for _i in range(2):
            again = serve.fastcheck_report_for_run(
                {"repo": "r"}, "run-x", prep=prep, current_qa=qa)
            self.assertEqual(again["state"], "pass-worker")
        self.assertEqual(self._snapshot(), before)
        for name, data in before_bytes.items():
            self.assertEqual((ops / name).read_bytes(), data)

    def test_missing_gate(self):
        prep = self._prep()
        prep["gates"] = []
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "missing")

    def test_no_receipt_file(self):
        prep = self._prep(sha="0" * 64)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_malformed_json(self):
        self._write_receipt("prep-x", None, raw=b"{not json")
        prep = self._prep(sha="0" * 64)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_oversize_receipt(self):
        self._write_receipt(
            "prep-x", None,
            raw=b" " * (serve._FASTCHECK_REPORT_MAX_BYTES + 10))
        prep = self._prep(sha="0" * 64)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")
        self.assertIn("oversize", rep["reason"])

    def test_sha_mismatch(self):
        summary = _summary("prep-x", [_step()], _consumer_turbo())
        self._write_receipt("prep-x", summary)
        prep = self._prep(sha="f" * 64)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_receipt_op_empty_and_mismatch(self):
        for op_value in ("", "prep-other"):
            summary = _summary(op_value, [_step()], _consumer_turbo())
            sha, _p = self._write_receipt("prep-x", summary)
            prep = self._prep(sha=sha)
            rep = serve.fastcheck_report_for_run(
                {"repo": "r"}, "run-x", prep=prep,
                current_qa=self._qa())
            self.assertEqual(rep["state"], "unknown", op_value)

    def test_pins_drift_is_stale(self):
        steps = [_step(head="d" * 40)]
        summary = _summary("prep-x", steps, _consumer_turbo())
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "stale")

    def test_live_drift_is_stale(self):
        for qa in (self._qa(head="e" * 40),
                   self._qa(fp="e" * 64),
                   self._qa(clean=False)):
            op = "prep-x"
            summary = _summary(op, [_step()], _consumer_turbo())
            sha, _p = self._write_receipt(op, summary)
            prep = self._prep(sha=sha)
            rep = serve.fastcheck_report_for_run(
                {"repo": "r"}, "run-x", prep=prep, current_qa=qa)
            self.assertEqual(rep["state"], "stale")

    def test_live_unreadable_never_passes(self):
        # clean=true alone must not pass when identity is missing.
        for qa in ({"state": "unknown"},
                   self._qa(head="", clean=True),
                   self._qa(fp="", clean=True),
                   self._qa(clean=None)):
            op = "prep-x"
            summary = _summary(op, [_step()], _consumer_turbo())
            sha, _p = self._write_receipt(op, summary)
            prep = self._prep(sha=sha)
            rep = serve.fastcheck_report_for_run(
                {"repo": "r"}, "run-x", prep=prep, current_qa=qa)
            self.assertEqual(rep["state"], "unknown")

    def test_worktree_mismatch_is_unknown(self):
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(wt="/other/tree"))
        self.assertEqual(rep["state"], "unknown")

    def test_run_mismatch_is_unknown(self):
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(run="run-other", sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_attestation_required(self):
        summary = _summary("prep-x", [_step()], _consumer_turbo())
        del summary["worker_attested"]
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")
        # Gate claiming another attestor contradicts the receipt.
        summary2 = _summary("prep-x", [_step()], _consumer_turbo())
        sha2, _p = self._write_receipt("prep-x", summary2)
        prep2 = self._prep(sha=sha2, attested="root")
        rep2 = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep2, current_qa=self._qa())
        self.assertEqual(rep2["state"], "unknown")

    def test_strict_types(self):
        summary = _summary("prep-x", [_step()], _consumer_turbo())
        summary["ok"] = "true"
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_ok_true_with_red_step_is_contradiction(self):
        steps = [_step(rc=1, tail="boom\n")]
        summary = _summary("prep-x", steps, _consumer_turbo(),
                           ok=True)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")
        self.assertIn("모순", rep["reason"])

    def test_ok_true_with_failed_consumer_is_contradiction(self):
        summary = _summary("prep-x", [_step()],
                           _consumer_turbo(status="fail"), ok=True,
                           failed={"argv": ["turbo"], "rc": 1,
                                   "timed_out": False})
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_generic_consumer_pass(self):
        summary = _summary("prep-x", [_step()], _consumer_generic())
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "pass-worker")
        cons = rep["consumer"]
        self.assertFalse(cons["turbo"])
        self.assertIn("yarn", cons["argv"])
        self.assertEqual(cons["rc"], 0)
        self.assertEqual(cons["isolation"]["network"], "loopback-only")

    def test_generic_consumer_skip_with_ok(self):
        gen = _consumer_generic(status="skip")
        gen["rc"] = None
        gen["timed_out"] = None
        summary = _summary("prep-x", [_step()], gen, ok=True)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "pass-worker")

    def test_turbo_pass_without_substeps_is_unknown(self):
        cons = _consumer_turbo(status="pass")
        cons["turbo_steps"] = []
        summary = _summary("prep-x", [_step()], cons, ok=True)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_install_isolation_preserved(self):
        steps = [_step("install", extra={"install": True,
                                         "network_sandbox": False})]
        summary = _summary("prep-x", steps, _consumer_turbo())
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "pass-worker")
        iso = rep["steps"][0]["isolation"]
        self.assertTrue(iso["install"])
        self.assertFalse(iso["network"])

    def test_warnings_consumer_first_dedupe_omitted(self):
        dup = "warning: same line here\n"
        steps = [_step("install", tail="warning: install note\n" + dup
                       + "warning: extra 1\nwarning: extra 2\n"
                         "warning: extra 3\nwarning: extra 4\n"
                         "warning: extra 5\n")]
        cons = _consumer_turbo()
        cons["turbo_steps"][2]["tail"] = (
            'warning "example#build" no output files\n' + dup)
        summary = _summary("prep-x", steps, cons)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        warns = rep["warnings"]
        self.assertEqual(warns[0]["step"], "consumer")
        lines = [w["line"] for w in warns]
        self.assertEqual(len(lines), len(set(lines)))
        self.assertLessEqual(len(warns),
                             serve._FASTCHECK_REPORT_MAX_WARNINGS)
        self.assertGreater(rep["warnings_omitted"], 0)

    def test_tail_and_step_caps(self):
        long_tail = "".join("line %d\n" % i for i in range(200))
        steps = [_step("s%d" % i, tail=long_tail) for i in range(10)]
        summary = _summary("prep-x", steps, _consumer_turbo())
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "pass-worker")
        self.assertEqual(len(rep["steps"]), serve._FASTCHECK_REPORT_MAX_STEPS)
        tail = rep["steps"][0]["tail"]
        self.assertLessEqual(len(tail), serve._FASTCHECK_REPORT_TAIL_CHARS)
        self.assertLessEqual(len(tail.splitlines()),
                             serve._FASTCHECK_REPORT_TAIL_LINES)
        self.assertTrue(rep["steps"][0]["truncated"])

    def test_fail_worker(self):
        failed = {"argv": ["turbo", "run", "build:web"], "rc": 1,
                  "timed_out": False}
        steps = [_step("unit"),
                 _step("consumer", rc=1, tail="boom\n")]
        cons = _consumer_turbo(status="fail")
        summary = _summary("prep-x", steps, cons, ok=False,
                           failed=failed)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha, status="fail")
        # Even drifted live source stays a failure, not a rebind.
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(head="e" * 40))
        self.assertEqual(rep["state"], "fail-worker")
        self.assertEqual(rep["failed"]["rc"], 1)
        self.assertIn("turbo", rep["failed"]["argv"])

    def test_symlink_escape_fails_closed(self):
        from openmainty import fastcheck
        outside = Path(self.tmp.name) / "outside.json"
        summary = _summary("prep-x", [_step()], _consumer_turbo())
        data = json.dumps(summary).encode("utf-8")
        outside.write_bytes(data)
        target = fastcheck.op_fastcheck_path("prep-x")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(outside)
        prep = self._prep(sha=hashlib.sha256(data).hexdigest())
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_malicious_op_id_never_escapes(self):
        prep = self._prep(op="../../evil", run="run-x", sha="0" * 64)
        before = self._snapshot()
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")
        from openmainty import fastcheck
        self.assertFalse(
            (fastcheck.op_fastcheck_path("prep-x").parent / ".."
             ).resolve().joinpath("evil.fastcheck.json").exists())
        self.assertEqual(self._snapshot(), before)

    def test_gate_status_type(self):
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha, status=1)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_non_string_pin_rejected(self):
        steps = [_step()]
        steps[0]["head"] = 12345
        summary = _summary("prep-x", steps, _consumer_turbo())
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")

    def test_pending_labels_capped(self):
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        qa = self._qa(pending=tuple("대기-%d" % i for i in range(8)))
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=qa)
        self.assertEqual(rep["state"], "pass-worker")
        self.assertEqual(rep["pending_gates"]["count"], 8)
        self.assertEqual(len(rep["pending_gates"]["labels"]), 6)

    def test_no_prep_is_missing(self):
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=None, current_qa=self._qa())
        # No batches registered in this home: no op found.
        self.assertIn(rep["state"], ("missing", "unknown"))

    def test_fail_with_drift_is_historical(self):
        failed = {"argv": ["turbo", "run", "build:web"], "rc": 1,
                  "timed_out": False}
        steps = [_step("unit"),
                 _step("consumer", rc=1, tail="boom\n")]
        cons = _consumer_turbo(status="fail")
        summary = _summary("prep-x", steps, cons, ok=False,
                           failed=failed)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha, status="fail")
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(head="e" * 40))
        self.assertEqual(rep["state"], "fail-worker")
        self.assertTrue(rep["historical"])
        self.assertIn("과거", rep["reason"])

    def test_fail_with_current_source_not_historical(self):
        failed = {"argv": ["turbo"], "rc": 1, "timed_out": False}
        steps = [_step("unit"),
                 _step("consumer", rc=1, tail="boom\n")]
        cons = _consumer_turbo(status="fail")
        summary = _summary("prep-x", steps, cons, ok=False,
                           failed=failed)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha, status="fail")
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa())
        self.assertEqual(rep["state"], "fail-worker")
        self.assertFalse(rep["historical"])

    def test_hold_held_verbatim_bounded(self):
        self._write_batch(
            "batch-1",
            {"by": "root", "reason": " only CIcons, no turbo " * 40,
             "at": "2026-10-19T00:00:00"})
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(batch="batch-1"))
        self.assertEqual(rep["state"], "pass-worker")
        hold = rep["hold"]
        self.assertEqual(hold["state"], "held")
        self.assertEqual(hold["by"], "root")
        self.assertIn("CIcons", hold["reason"])
        self.assertLessEqual(len(hold["reason"]), 500)
        self.assertTrue(hold["reason_truncated"])
        self.assertEqual(hold["at"], "2026-10-19T00:00:00")

    def test_hold_short_reason_not_truncated(self):
        self._write_batch("batch-1", {"by": "root",
                                      "reason": "짧은 사유",
                                      "at": "2026-10-19T00:00:00"})
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(batch="batch-1"))
        self.assertEqual(rep["hold"]["state"], "held")
        self.assertFalse(rep["hold"]["reason_truncated"])
        self.assertEqual(rep["hold"]["reason"], "짧은 사유")

    def test_hold_none_when_batch_has_no_hold(self):
        self._write_batch("batch-1")
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(batch="batch-1"))
        self.assertEqual(rep["state"], "pass-worker")
        self.assertEqual(rep["hold"]["state"], "none")

    def test_hold_unknown_cases(self):
        op = "prep-x"
        summary = _summary(op, [_step()], _consumer_turbo())
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        # No batch key at all: unknown, never an invented hold.
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["hold"]["state"], "unknown")
        # Unknown batch id: unknown.
        rep2 = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep,
            current_qa=self._qa(batch="batch-missing"))
        self.assertEqual(rep2["hold"]["state"], "unknown")
        # QA none-state (nothing registered): none, not unknown.
        qa = self._qa(batch="batch-missing")
        qa["state"] = "none"
        rep3 = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=qa)
        self.assertEqual(rep3["hold"]["state"], "none")

    def test_ok_true_with_bool_rc_is_contradiction(self):
        steps = [_step()]
        steps[0]["rc"] = False
        summary = _summary("prep-x", steps, _consumer_turbo(), ok=True)
        sha, _p = self._write_receipt("prep-x", summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "unknown")
        self.assertIn("모순", rep["reason"])

    def test_turbo_pass_with_bad_substep_is_contradiction(self):
        for bad in ({"rc": 2}, {"rc": None}, {"rc": False},
                    {"timed_out": True}):
            cons = _consumer_turbo(status="pass")
            cons["turbo_steps"][1].update(bad)
            summary = _summary("prep-x", [_step()], cons, ok=True)
            sha, _p = self._write_receipt("prep-x", summary)
            prep = self._prep(sha=sha)
            rep = serve.fastcheck_report_for_run(
                {"repo": "r"}, "run-x", prep=prep,
                current_qa=self._qa())
            self.assertEqual(rep["state"], "unknown", bad)
            self.assertIn("consumer", rep["reason"], bad)

    def test_turbo_substep_labels_from_argv(self):
        op = "prep-x"
        cons = _consumer_turbo()
        for entry in cons["turbo_steps"]:
            entry.pop("name", None)
        summary = _summary(op, [_step()], cons)
        sha, _p = self._write_receipt(op, summary)
        prep = self._prep(sha=sha)
        rep = serve.fastcheck_report_for_run(
            {"repo": "r"}, "run-x", prep=prep, current_qa=self._qa())
        self.assertEqual(rep["state"], "pass-worker")
        labels = [s["label"] for s in rep["consumer"]["turbo_steps"]]
        self.assertEqual(labels, ["버전 확인", "그래프 확인(dry-run)",
                                  "실제 실행(force)"])

    def test_api_work_exposes_bounded_report(self):
        # Actual /api/work wiring: isolated fixture state, the real
        # function exposes the bounded report field, and nothing is
        # read or written outside the test home.
        from openmainty import batches, fastcheck, store
        state = store.create("fakerepo", "1", "t", "base0", {})
        run_id = state["id"]
        op = "prep-wire-1"
        steps = [_step()]
        consumer = _consumer_turbo()
        summary = _summary(op, steps, consumer)
        raw = json.dumps(summary, ensure_ascii=False).encode("utf-8")
        opf = fastcheck.op_fastcheck_path(op)
        opf.parent.mkdir(parents=True, exist_ok=True)
        before_dirs = sorted(p.name for p in opf.parent.iterdir())
        opf.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        rec_path = batches.integration_ops_dir() / (op + ".json")
        rec = {"op_id": op, "run_id": run_id, "status": "done",
               "updated_at": "2026-10-25T17:37:47",
               "gates": [{"key": "fastchecks", "status": "pass",
                          "receipt_sha256": sha,
                          "attested_by": "worker"}],
               "result": {"head": HEAD, "fingerprint": FP,
                          "path": PATH}}
        rec_path.write_text(json.dumps(rec), encoding="utf-8")
        before = {p.name: p.read_bytes()
                  for p in sorted(opf.parent.iterdir())}
        out = serve.api_work({"run": run_id})
        self.assertIn("fastcheck_report", out)
        rep = out["fastcheck_report"]
        self.assertIsInstance(rep, dict)
        self.assertEqual(rep.get("op"), op)
        # No registered batch in this home: live source unreadable,
        # so the report is unknown — never a borrowed pass.
        self.assertEqual(rep.get("state"), "unknown")
        after = {p.name: p.read_bytes()
                 for p in sorted(opf.parent.iterdir())}
        self.assertEqual(after, before)
        self.assertEqual(set(after),
                         set(before_dirs) | {op + ".json",
                                             op + ".fastcheck.json"})


if __name__ == "__main__":
    unittest.main()
