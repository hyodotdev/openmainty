"""Human attestation of batch gates (M2.4b, product feature).

Owner or Root attests through the same path (dashboard review
screen, `openmainty attest`, or the API): owner/root attestor,
evidence under ~/.openmainty/qa, receipt-review with the worker
receipt sha, N/A only with a reason. A worker can never attest,
and readiness accepts either human attestor. Offline, temp HOME,
Py3.9 portable.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import batches, events, repos, sched, store  # noqa: E402
from openmainty.util import write_json  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


class AttestFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_temp_home(self, str(self.root / "home"))
        self.origin = self.root / "origin"
        self.origin.mkdir()
        (self.origin / "a.txt").write_text("a\n", encoding="utf-8")
        import subprocess as _sp
        for args in (["init", "-b", "main"],
                     ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"],
                     ["add", "-A"], ["commit", "-qm", "x"]):
            _sp.run(["git", "-C", str(self.origin)] + args, check=True,
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    stdin=_sp.DEVNULL, timeout=60)
        _sp.run(["git", "-C", str(self.origin), "checkout", "-qb",
                 "codex/cand-1"], check=True, stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL, stdin=_sp.DEVNULL, timeout=60)
        out = _sp.run(["git", "-C", str(self.origin), "rev-parse",
                       "HEAD"], check=True, capture_output=True,
                      text=True, timeout=60)
        self.head = out.stdout.strip()
        from openmainty import runner as _runner
        self.fp = _runner.source_snapshot(self.origin)["fingerprint"]
        self.run_id = "run-att1"
        repos.save({"name": "fakerepo", "github": "o/r",
                    "baseBranch": "main"})
        qst = sched.load("fakerepo")
        qst["queue"] = [{"run_id": "run-att1", "dep": "bar",
                         "target": "9.9.9", "lane": "main",
                         "branch": "codex/cand-1"}]
        sched.save(qst)
        state = store.create("fakerepo", "dep-bar-9.9.9",
                             "chore(deps): bar 9.9.8 -> 9.9.9",
                             self.head, {})
        state["id"] = self.run_id
        state["workCommit"] = self.head
        state["workTreeFingerprint"] = self.fp
        state["verifiedCommits"] = {"review": self.head,
                                    "e2e": self.head}
        state["verifiedFingerprints"] = {"review": self.fp,
                                         "e2e": self.fp}
        state["steps"] = {"review": {"status": "done", "exit_code": 0},
                          "e2e": {"status": "done", "exit_code": 0}}
        store.save(state)
        self.evidence = self.root / "ev"
        self.evidence.mkdir()
        self.proof = self.evidence / "proof.txt"
        self.proof.write_text("proof\n", encoding="utf-8")
        self.sha = batches.sha256_file(self.proof)
        self.bid = "auto-att-1"

    def payload(self):
        return {
            "id": self.bid, "repo": "fakerepo",
            "target_branch": "main", "purpose": "auto",
            "origin": {"checkout": str(self.origin),
                       "base_branch": "main", "base_head": self.head},
            "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": "main",
                "run_id": self.run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.origin),
                           "branch": "codex/cand-1",
                           "head": self.head,
                           "fingerprint": self.fp}}],
            "inherited": [],
            "integrations": [{
                "lane": "main", "worktree": str(self.origin),
                "head": self.head, "fingerprint": self.fp,
                "status": "integration-testing",
                "gates": [
                    {"key": "worker:unit", "status": "pass",
                     "root_attested": False,
                     "inputs": {"head": self.head,
                                "fingerprint": self.fp},
                     "evidence": [{
                         "label": "unit log", "kind": "file",
                         "origin": str(self.proof)}]},
                    {"key": "unit", "status": "pending",
                     "root_attested": False, "note": "Root 확인 대기",
                     "evidence": []},
                    {"key": "root-qa", "status": "pending",
                     "root_attested": False, "note": "Root 확인 대기",
                     "evidence": []}]}],
            "policy": {"required_gates": ["unit", "root-qa"],
                       "gates": {"unit": {"root": True},
                                 "root-qa": {"root": True},
                                 "worker:unit": {}}},
        }

    def imported(self):
        return batches.import_batch(self.payload())

    def gate(self, batch_id=None, key="unit"):
        batch = batches.load_batch(batch_id or self.bid)
        for integ in batch["integrations"]:
            for gate in integ["gates"]:
                if gate["key"] == key:
                    return gate
        self.fail(f"gate {key} missing")


class RefreshFlagTests(AttestFixture):
    """M2.16c: the API path must not pay for a synchronous snapshot
    on every attest (live: 10 serial calls took 15-30 s each).
    attest_gate(refresh=False) records the attestation without
    calling refresh_all_snapshots; the default keeps it."""

    def test_refresh_false_skips_inline_snapshot(self):
        self.imported()
        from unittest import mock as _mock
        with _mock.patch.object(batches, "refresh_all_snapshots",
                                side_effect=AssertionError("inline")):
            out = batches.attest_gate(self.bid, "unit", "owner",
                                      "확인", "receipt",
                                      receipt_sha=self.sha,
                                      refresh=False)
        self.assertEqual(out["attestor"], "owner")

    def test_default_still_refreshes(self):
        self.imported()
        from unittest import mock as _mock
        with _mock.patch.object(batches, "refresh_all_snapshots",
                                return_value={}) as snap:
            batches.attest_gate(self.bid, "unit", "owner", "확인",
                                "receipt", receipt_sha=self.sha)
        self.assertTrue(snap.called)


class ReceiptModeTests(AttestFixture):
    def test_receipt_review_passes_with_owner(self):
        self.imported()
        out = batches.attest_gate(self.bid, "unit", "owner",
                                  "로그 확인, 버전 일치", "receipt",
                                  receipt_sha=self.sha)
        self.assertEqual(out["attestor"], "owner")
        gate = self.gate()
        self.assertEqual(gate["status"], "pass")
        self.assertTrue(gate["owner_attested"])
        self.assertFalse(gate.get("root_attested"))
        self.assertEqual(gate["attestor"], "owner")
        self.assertEqual(gate["inputs"]["head"], self.head)

    def test_receipt_sha_mismatch_refused(self):
        self.imported()
        with self.assertRaises(batches.BatchError):
            batches.attest_gate(self.bid, "unit", "owner", "확인",
                                "receipt", receipt_sha="0" * 64)

    def test_worker_can_never_attest(self):
        self.imported()
        with self.assertRaises(batches.BatchError) as ctx:
            batches.attest_gate(self.bid, "unit", "worker", "확인",
                                "receipt", receipt_sha=self.sha)
        self.assertIn("worker", str(ctx.exception))

    def test_empty_attestor_or_note_refused(self):
        self.imported()
        with self.assertRaises(batches.BatchError):
            batches.attest_gate(self.bid, "unit", "", "확인",
                                "receipt", receipt_sha=self.sha)
        with self.assertRaises(batches.BatchError):
            batches.attest_gate(self.bid, "unit", "owner", "  ",
                                "receipt", receipt_sha=self.sha)


class DirectModeTests(AttestFixture):
    def test_direct_root_attestation_lands_qa(self):
        self.imported()
        out = batches.attest_gate(self.bid, "root-qa", "root",
                                  "시뮬레이터 기동 확인", "direct",
                                  log_text="simulator booted\n")
        self.assertEqual(out["mode"], "direct")
        gate = self.gate(key="root-qa")
        self.assertTrue(gate["root_attested"])
        self.assertFalse(gate.get("owner_attested"))
        qdir = Path(os.environ["OPENMAINTY_HOME"]) / "qa" / self.bid
        self.assertTrue((qdir / "root-qa.log").is_file())
        meta = json.loads((qdir / "root-qa.json").read_text(
            encoding="utf-8"))
        self.assertEqual(meta["attestor"], "root")
        self.assertEqual(meta["inputs"]["head"], self.head)

    def test_direct_empty_log_refused(self):
        self.imported()
        with self.assertRaises(batches.BatchError):
            batches.attest_gate(self.bid, "root-qa", "root", "확인",
                                "direct", log_text="  ")


class NaModeTests(AttestFixture):
    def test_na_needs_reason(self):
        self.imported()
        with self.assertRaises(batches.BatchError) as ctx:
            batches.attest_gate(self.bid, "root-qa", "owner",
                                "네이티브 없음", "na", na_reason=" ")
        self.assertIn("사유", str(ctx.exception))

    def test_na_recorded_and_shown(self):
        self.imported()
        batches.attest_gate(self.bid, "root-qa", "owner",
                            "순수 JS라 기기 검증 불필요", "na",
                            na_reason="순수 개발 의존성, 네이티브 없음")
        gate = self.gate(key="root-qa")
        self.assertEqual(gate["na"]["reason"],
                         "순수 개발 의존성, 네이티브 없음")
        self.assertEqual(gate["na"]["by"], "owner")
        names = batches._root_pending_names(
            batches.load_batch(self.bid))
        self.assertNotIn("root-qa", names)


class ReadinessTests(AttestFixture):
    def live(self, batch):
        run = {"id": "run-att-live", "repo": "fakerepo",
               "branch": "codex/cand-1", "workCommit": self.head}
        write_json(Path(os.environ["OPENMAINTY_HOME"]) / "runs" /
                   "run-att-live.json", run)
        live, _ = batches.live_map_with_diag([batch])
        return live

    def test_worker_passes_alone_never_ready(self):
        batch = self.imported()
        res = batches.evaluate(batch, self.live(batch))
        self.assertFalse(res["results"][0]["ready"])
        self.assertTrue(any("pending" in r
                            for r in res["results"][0]["reasons"]))

    def test_pass_without_human_names_attestation(self):
        # Legacy shape: a root gate passed with no human attestor.
        # Hand-built (import refuses this for new batches).
        home = Path(os.environ["OPENMAINTY_HOME"])
        dest = batches.evidence_dir("auto-att-legacy")
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / (self.sha + "-proof.txt")
        target.write_bytes(self.proof.read_bytes())
        batch = {
            "id": "auto-att-legacy", "repo": "fakerepo",
            "target_branch": "main",
            "primaries": [{
                "dep": "bar", "target": "9.9.9", "lane": "main",
                "run_id": self.run_id,
                "review": {"verdict": "accept"},
                "required_gates": ["unit"],
                "source": {"worktree": str(self.origin),
                           "branch": "codex/cand-1",
                           "head": self.head,
                           "fingerprint": self.fp}}],
            "inherited": [],
            "origin": {"checkout": str(self.origin),
                       "base_branch": "main", "base_head": self.head},
            "integrations": [{
                "lane": "main", "worktree": str(self.origin),
                "head": self.head, "fingerprint": self.fp,
                "status": "integration-testing",
                "gates": [{
                    "key": "unit", "status": "pass",
                    "root_attested": False,
                    "inputs": {"head": self.head,
                               "fingerprint": self.fp},
                    "evidence": [{
                        "label": "unit log", "kind": "file",
                        "stored": True,
                        "stored_path": "evidence/" + target.name,
                        "sha256": self.sha,
                        "bytes": len(b"proof\n")}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {"root": True}}},
        }
        write_json(batches.batch_file("auto-att-legacy"), batch)
        res = batches.evaluate(batch, self.live(batch))
        self.assertFalse(res["results"][0]["ready"])
        self.assertTrue(any("attestation" in r
                            for r in res["results"][0]["reasons"]))

    def test_full_human_attestation_is_ready(self):
        self.imported()
        batches.attest_gate(self.bid, "unit", "owner", "로그 확인",
                            "receipt", receipt_sha=self.sha)
        batches.attest_gate(self.bid, "root-qa", "root", "기기 확인",
                            "direct", log_text="ok\n")
        batch = batches.load_batch(self.bid)
        res = batches.evaluate(batch, self.live(batch))
        self.assertTrue(res["results"][0]["ready"])

    def test_owner_attestation_counts_as_human(self):
        self.imported()
        batches.attest_gate(self.bid, "unit", "owner", "로그 확인",
                            "receipt", receipt_sha=self.sha)
        batches.attest_gate(self.bid, "root-qa", " Quin", "직접 확인",
                            "direct", log_text="ok\n")
        batch = batches.load_batch(self.bid)
        res = batches.evaluate(batch, self.live(batch))
        gate = [g for i in batch["integrations"]
                for g in i["gates"] if g["key"] == "root-qa"][0]
        self.assertTrue(gate["owner_attested"])
        self.assertFalse(gate.get("root_attested"))
        self.assertTrue(res["results"][0]["ready"])

    def test_na_gate_ready(self):
        self.imported()
        batches.attest_gate(self.bid, "unit", "owner", "로그 확인",
                            "receipt", receipt_sha=self.sha)
        batches.attest_gate(self.bid, "root-qa", "owner", "N/A 확인",
                            "na", na_reason="순수 개발 의존성")
        batch = batches.load_batch(self.bid)
        res = batches.evaluate(batch, self.live(batch))
        self.assertTrue(res["results"][0]["ready"])


class EventTests(AttestFixture):
    """M24b-F1: every attestation is a timeline event naming who
    approved what (N/A carries its reason)."""

    def titles(self):
        from openmainty import events as _events
        return [(e.get("title"), e.get("detail"), e.get("actor"))
                for e in _events.list_events("fakerepo")]

    def test_receipt_event(self):
        self.imported()
        batches.attest_gate(self.bid, "unit", "owner", "로그 확인",
                            "receipt", receipt_sha=self.sha)
        rows = self.titles()
        self.assertTrue(any(
            title == "확인 기록: unit — owner (receipt)"
            and self.bid in detail and actor == "owner"
            for title, detail, actor in rows))

    def test_na_event_carries_reason(self):
        self.imported()
        batches.attest_gate(self.bid, "root-qa", "root", "N/A 확인",
                            "na", na_reason="순수 개발 의존성")
        rows = self.titles()
        self.assertTrue(any(
            "해당 없음: 순수 개발 의존성" in detail
            and actor == "root"
            for _, detail, actor in rows))


class CliTests(AttestFixture):
    def args(self, **kw):
        import argparse as _ap
        ns = _ap.Namespace(batch=self.bid, gate="unit",
                           attestor="owner", note="로그 확인",
                           receipt="", log_file="", na_reason="")
        for key, value in kw.items():
            setattr(ns, key, value)
        return ns

    def run_cli(self, ns, typed=None):
        from unittest import mock as _mock
        from openmainty import cli as _cli
        with _mock.patch.object(sys.stdin, "isatty",
                                return_value=True), \
             _mock.patch("builtins.input",
                         return_value=(ns.batch if typed is None
                                       else typed)):
            return _cli.cmd_attest(ns)

    def test_cli_receipt_mode(self):
        self.imported()
        rc = self.run_cli(self.args(receipt=self.sha))
        self.assertEqual(rc, 0)
        self.assertTrue(self.gate()["owner_attested"])

    def test_cli_refusal_rc2(self):
        self.imported()
        rc = self.run_cli(self.args(receipt="0" * 64))
        self.assertEqual(rc, 2)

    def test_cli_na_mode(self):
        self.imported()
        rc = self.run_cli(self.args(
            gate="root-qa", na_reason="순수 개발 의존성"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.gate(key="root-qa")["na"]["reason"],
                         "순수 개발 의존성")

    def test_cli_refuses_non_tty(self):
        # M24b-F2: an agent shell (non-TTY stdin) can never attest.
        from unittest import mock as _mock
        from openmainty import cli as _cli
        self.imported()
        with _mock.patch.object(sys.stdin, "isatty",
                                return_value=False):
            rc = _cli.cmd_attest(self.args(receipt=self.sha))
        self.assertEqual(rc, 2)
        self.assertFalse(self.gate().get("owner_attested"))

    def test_cli_refuses_wrong_typed_id(self):
        self.imported()
        rc = self.run_cli(self.args(receipt=self.sha),
                          typed="other-batch")
        self.assertEqual(rc, 2)
        self.assertFalse(self.gate().get("owner_attested"))


class HoldTests(AttestFixture):
    """M2.5b: owner/root hold on an auto batch."""

    def _titles(self):
        return [e.get("title") for e in events.list_events("fakerepo")]

    def test_owner_hold_sets_flag_and_event(self):
        self.imported()
        out = batches.hold_batch(self.bid, True, "owner",
                                 "metro lockstep — RN 0.88과 함께")
        self.assertTrue(out["hold"])
        held = batches.load_batch(self.bid)["hold"]
        self.assertEqual(held["by"], "owner")
        self.assertIn("lockstep", held["reason"])
        self.assertTrue(any(t.startswith("보류 설정")
                            for t in self._titles()))

    def test_hold_refusals(self):
        self.imported()
        for attestor, reason in (("worker", "x"), ("", "x"),
                                 ("someone", "x"), ("owner", ""),
                                 ("root", "   ")):
            with self.assertRaises(batches.BatchError,
                                   msg=f"{attestor!r}/{reason!r}"):
                batches.hold_batch(self.bid, True, attestor, reason)
        with self.assertRaises(batches.BatchError):
            batches.hold_batch("no-such-batch", True, "root", "x")
        self.assertNotIn("hold", batches.load_batch(self.bid))

    def test_release_restores_gates_byte_identical(self):
        self.imported()
        before = json.dumps(
            batches.load_batch(self.bid)["integrations"], sort_keys=True)
        batches.hold_batch(self.bid, True, "root", "확인 전 대기")
        batches.hold_batch(self.bid, False, "root")
        after_batch = batches.load_batch(self.bid)
        self.assertNotIn("hold", after_batch)
        self.assertEqual(json.dumps(after_batch["integrations"],
                                    sort_keys=True), before)
        self.assertTrue(any(t.startswith("보류 해제")
                            for t in self._titles()))

    def test_evaluate_never_ready_while_held(self):
        self.imported()
        batches.hold_batch(self.bid, True, "root", "metro lockstep")
        res = batches.evaluate(batches.load_batch(self.bid))
        self.assertFalse(res["results"][0]["ready"])
        self.assertTrue(any("보류:" in r
                            for r in res["results"][0]["reasons"]))

    def test_hold_cli_refuses_non_tty(self):
        # Agents must never hold: no TTY means no hold, the same
        # stdin gate as attest (StringIO is never a TTY).
        import io
        from openmainty import cli as _cli
        ns = type("NS", (), {"batch": self.bid, "attestor": "owner",
                             "reason": "x", "release": False})()
        old = sys.stdin
        sys.stdin = io.StringIO("x\n")
        try:
            rc = _cli.cmd_hold(ns)
        finally:
            sys.stdin = old
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
